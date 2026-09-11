"""H3 后台转码队列（P1）：job 表 + 进度 + 取消。

定位：VAE 编码只能发生在 Comfy 队列执行内（VAE 实例由执行上下文提供），
本模块不碰 VAE、不加载模型——只做三件事：

1. 任务登记：前端“转 latent”按钮调 /h3chain/transcode_submit 随时提交，
   不再手写 ds.transcode_jobs、不再教用户连 H3MediaToLatent 线；
2. 进度/取消：执行侧（主节点 _execute_transcode_only / H3MediaToLatent 垫片）
   claim -> begin -> progress -> complete/fail 推进；取消是协作式的，
   running 任务置 cancel_requested，执行侧的 _interrupted 回调感知后抛中断；
3. 证据对账：产物登记仍走 manifest.latents（run_transcode_job 内完成），
   前端凭 latents 证据判定 done，前凭 job 状态显示进度条。

状态机：queued -> running -> done | error | cancelled；
queued --cancel--> cancelled；running --cancel--> cancel_requested=True，
执行侧下一次 _interrupted() 即中断，收尾记 cancelled。
进程内内存表（单 Comfy 进程单队列够用，与 checkpoint._BUSY 同口径）；
重启丢表不丢产物（latents 证据在 manifest 里）。

无 torch 依赖，无 ComfyUI 可单测。
"""

import threading
import time
import uuid

STATUSES = ("queued", "running", "done", "error", "cancelled")
BRANCHES = ("图像+音频", "仅图像", "仅音频")
SPLITS = ("否", "是")

_JOBS = {}
_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def _clean_save_name(name) -> str:
    s = str(name or "").strip().replace("\\", "/").split("/")[-1]
    if not s or not s.endswith(".pt") or s.startswith(".") or ".." in s \
            or "/" in s or ":" in s:
        raise ValueError(f"非法保存名：{name!r}（须以 .pt 结尾的文件名）")
    return s


def validate_params(project, src, start_s=0.0, end_s=0.0,
                    branch="图像+音频", split="否", save_name="") -> dict:
    """形状校验（不碰磁盘，执行侧再查文件存在性）-> 归一化参数。"""
    proj = str(project or "").strip()
    if not proj:
        raise ValueError("项目名不能为空")
    if "/" in proj or "\\" in proj or proj.startswith(".") or ".." in proj:
        raise ValueError(f"非法项目名：{project!r}")
    s = str(src or "").strip().replace("\\", "/")
    if not s:
        raise ValueError("源文件不能为空")
    br = str(branch or "图像+音频")
    if br not in BRANCHES:
        raise ValueError(f"非法分支：{branch!r}（须为 图像+音频/仅图像/仅音频）")
    sp = str(split or "否")
    if sp not in SPLITS:
        raise ValueError(f"非法分开保存：{split!r}（须为 否/是）")
    try:
        ss = max(0.0, float(start_s or 0.0))
    except (TypeError, ValueError):
        raise ValueError("起始秒须为数字") from None
    try:
        es = float(end_s or 0.0)
    except (TypeError, ValueError):
        raise ValueError("结束秒须为数字") from None
    if es > 0 and es <= ss:
        raise ValueError(f"秒窗为空（[{ss}s, {es}s)，须 0 <= start < end）")
    return {"project": proj, "src": s, "start_s": ss, "end_s": es,
            "branch": br, "split": sp, "save_name": _clean_save_name(save_name)}


def submit(project, src, start_s=0.0, end_s=0.0,
           branch="图像+音频", split="否", save_name="") -> dict:
    """提交任务 -> job（queued）。参数非法抛 ValueError（路由层映射 400）。"""
    params = validate_params(project, src, start_s, end_s, branch, split, save_name)
    now = _now()
    job = {"id": "tq_" + uuid.uuid4().hex[:12], "status": "queued",
           "progress": 0.0, "note": "", "report": "", "error": "",
           "cancel_requested": False, "created_at": now, "updated_at": now,
           **params}
    with _LOCK:
        _JOBS[job["id"]] = job
    return dict(job)


def get(job_id) -> dict | None:
    with _LOCK:
        job = _JOBS.get(str(job_id or ""))
        return dict(job) if job else None


def list_jobs(project=None) -> list:
    with _LOCK:
        jobs = [dict(j) for j in _JOBS.values()
                if project is None or j.get("project") == project]
    jobs.sort(key=lambda j: j.get("created_at") or 0)
    return jobs


def _set(job_id, **fields) -> dict | None:
    with _LOCK:
        job = _JOBS.get(str(job_id or ""))
        if job is None:
            return None
        job.update(fields)
        job["updated_at"] = _now()
        return dict(job)


def claim(project=None) -> dict | None:
    """执行侧取最早的 queued 任务 -> running（无可用返回 None）。"""
    with _LOCK:
        cands = [j for j in _JOBS.values() if j["status"] == "queued"
                 and (project is None or j.get("project") == project)]
        if not cands:
            return None
        cands.sort(key=lambda j: j.get("created_at") or 0)
        job = cands[0]
        job["status"] = "running"
        job["progress"] = 0.0
        job["updated_at"] = _now()
        return dict(job)


def has_queued(project=None) -> bool:
    """有无排队任务（主节点入口用：有则进转码专跑，无需手写 ds 任务）。"""
    with _LOCK:
        return any(j["status"] == "queued"
                   and (project is None or j.get("project") == project)
                   for j in _JOBS.values())


def begin(job_id) -> dict | None:
    return _set(job_id, status="running")


def progress(job_id, frac, note="") -> dict | None:
    try:
        f = max(0.0, min(1.0, float(frac)))
    except (TypeError, ValueError):
        f = 0.0
    fields = {"progress": f}
    if note:
        fields["note"] = str(note)[:200]
    return _set(job_id, **fields)


def complete(job_id, report="") -> dict | None:
    return _set(job_id, status="done", progress=1.0, report=str(report or "")[:2000])


def fail(job_id, error="") -> dict | None:
    return _set(job_id, status="error", error=str(error or "")[:2000])


def cancel(job_id) -> dict | None:
    """取消：queued 直接 cancelled；running 置 cancel_requested（执行侧中断收尾）。

    不存在的 id 返回 None（路由层映射 404）。
    """
    with _LOCK:
        job = _JOBS.get(str(job_id or ""))
        if job is None:
            return None
        if job["status"] == "queued":
            job["status"] = "cancelled"
        elif job["status"] == "running":
            job["cancel_requested"] = True
        job["updated_at"] = _now()
        return dict(job)


def finish_cancelled(job_id, note="") -> dict | None:
    """执行侧响应取消后收尾 -> cancelled。"""
    fields = {"status": "cancelled"}
    if note:
        fields["note"] = str(note)[:200]
    return _set(job_id, **fields)


def is_cancel_requested(job_id) -> bool:
    with _LOCK:
        job = _JOBS.get(str(job_id or ""))
        return bool(job and job.get("cancel_requested"))


def interrupted_checker(job_id):
    """给 run_transcode_job 的 _interrupted 回调：job 被取消即 True。"""
    def _chk():
        return is_cancel_requested(job_id)
    return _chk


def reset():
    """清空全表（单测用）。"""
    with _LOCK:
        _JOBS.clear()
