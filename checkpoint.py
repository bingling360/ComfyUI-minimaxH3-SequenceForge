"""断点续跑：共享参数指纹 + 逐段提示词哈希 + manifest 原子读写 + 段 AV latent 落盘。

断点只存采样输出的 latent（约 5 MB/段），不存解码像素（约 300 MB/段）：
续跑时重解码秒级回放，结果与一次跑完逐帧一致（种子序列由 manifest 权威记录）。

v2：指纹只覆盖共享参数（不含提示词、不含种子），改某段提示词仍指向同一条链；
提示词按段存哈希，改了第 N 段 -> 找到变更区间重建（见下）。

v4（2026-09-16 修正旧注释）：**重做不需要级联。** 段 N 可用双锚对齐邻居——
上锚取段 N-1 存档 latent 的尾部，下锚取段 N+1 存档 latent 的头部（`_redo_next_anchor`）。
所以下游段已经生成好的前提下，改某段只重建该段即可，不必连带其后的段。
真·唯一硬约束只有分辨率（C/H/W 不同则桥拼不上）。

v5（2026-09-16）：变更检测改**区间模型**（重做最小单位 = 单段，离散区间各自双锚）：
- 区间计算迁到 `anchors.change_intervals`，本模块的 `reroll_start`（返回单一 start、
  隐含级联语义）已删除——级联不是默认行为，留着就是第二个真相。
- `assert_match` 收窄为**只校验 width/height**，其余参数变更只回报不报错、不触发重做。

v3：存档根目录迁至 output/h3_projects/<项目名>/（游戏式一项目一文件夹），
manifest 增加 title/created_at/updated_at/finals 键；旧 checkpoints 目录不读不写。

torch / folder_paths 延迟导入：指纹与 manifest 逻辑在无 ComfyUI 环境下可单测。
"""

import hashlib
import json
import os
import re
import tempfile
import threading

SCHEMA = "h3seamless/ckpt-v3"

# ---- 生成互斥锁（剪辑/转码与采样不能同时执行） ----
# nodes.execute 进入时 +1、退出时 -1；trim/slice/move/split 等路由遇忙回 423，
# 前端据此置灰按钮。进程内计数（单 Comfy 进程单队列，够用；多进程部署时
# 前端再以 Comfy /queue 运行态做第二道 pre-check）。
_BUSY = 0
_BUSY_LOCK = threading.Lock()


def mark_busy() -> int:
    """标记一次生成开始，返回当前忙计数。"""
    global _BUSY
    with _BUSY_LOCK:
        _BUSY += 1
        return _BUSY


def unmark_busy() -> int:
    """标记一次生成结束，返回当前忙计数（防负）。"""
    global _BUSY
    with _BUSY_LOCK:
        _BUSY = max(0, _BUSY - 1)
        return _BUSY


def is_busy() -> bool:
    """是否正在生成（剪辑/转码/移动入口用，遇忙回 423）。"""
    with _BUSY_LOCK:
        return _BUSY > 0


def fingerprint(params: dict) -> str:
    """共享参数 -> 8 位十六进制指纹（sha256，跨进程稳定）。

    params 由调用方保证不含提示词与种子：种子控件开着 control_after_generate
    每次运行自动 +1，提示词改动走逐段哈希校验而非换链。
    """
    blob = json.dumps(params, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:8]


def prompt_hash(prompt: str) -> str:
    """单段提示词 -> 8 位十六进制哈希（与共享参数指纹同法）。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]


def image_hash(frame) -> str:
    """单帧图 [H,W,3] 0-1 tensor -> 8 位十六进制像素哈希（分镜关键帧失效判定用）。

    uint8 量化后哈希：VAE 解码的微小浮点抖动（<1/255）不改变哈希，
    断点续跑重解码同一 latent 不会误触发重跑。
    """
    import torch

    arr = (frame.detach().float().clamp(0.0, 1.0).cpu().numpy() * 255.0).astype("uint8")
    return hashlib.sha1(arr.tobytes()).hexdigest()[:8]


def save_keyframe(root: str, idx: int, frame) -> str:
    """分镜关键帧原图副本 -> keyframes/kf_NNN.png（长边 512，断点自包含、面板可显示）。"""
    name = f"kf_{idx:03d}.png"
    try:
        from PIL import Image

        arr = (frame.detach().float().clamp(0.0, 1.0).cpu().numpy() * 255.0).astype("uint8")
        img = Image.fromarray(arr)
        w, h = img.size
        scale = 512.0 / max(w, h)
        if scale < 1.0:
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        kdir = os.path.join(root, "keyframes")
        os.makedirs(kdir, exist_ok=True)
        img.save(os.path.join(kdir, name))
        return name
    except Exception:
        return ""


def truncate(root: str, manifest: dict, start: int) -> dict:
    """丢弃第 start 段起的进度：截断 manifest 各列表并立即原子落盘，删除被弃段文件。

    截断即时落盘：截断后立刻崩溃也不会复活旧进度。段文件含 latent(.pt)、
    分段视频(.mp4) 与缩略图(.png)，三者同下标一起清，防止重跑后残留旧画面
    （二采渲染与基础段同名——seg_NNN.mp4 被清即高清记录自然失效）。
    二采记录 manifest.upscale.segs 同段号联动截断，旧版独立产物
    （upseg_*/upthumb_*/uplast_*）一并清理；基础段重做后其二采必然失效，
    重跑时按新 base_hash 自动补渲染。
    返回更新后的 manifest。
    """
    out = dict(manifest)
    out["done"] = start
    # 重摇标记只清「被重做区间覆盖」的槽位：slot >= start 的段本来就要重做，
    # 标记无意义 → 清；slot < start 的段保留存档、标记仍然有效 → 必须留
    #（与调用方对 inserts 的过滤同口径，见 nodes.py 存档续跑处）。
    # 旧实现无条件清空：改靠后的段会误杀靠前的重摇标记（10 段全完成 → 标段 3
    # 重摇 → 改段 8 提示词使 start=7 → 段 3 标记被连带清掉）。
    if out.get("redo_queue"):
        _kept = []
        for _x in out["redo_queue"]:
            try:
                _s = int(_x[0])
            except (TypeError, ValueError, IndexError):
                continue          # 顺带滤掉畸形条目
            if _s < start:
                _kept.append(_x)
        out["redo_queue"] = _kept
    # 注意：manifest 里没有 "anchors" 键——anchor 是**请求侧**字段（ds.segments[i].anchors），
    # 只按段并进 prompt_hashes，不落盘成 manifest 列表（旧实现这里留了个永不命中的键，已删）。
    for key in ("seeds", "trims", "prompt_hashes", "thumbs", "videos", "prompts",
                "seams", "bridge_scores", "seam_metrics"):
        if key in out:
            out[key] = list(out[key])[:start]
    up = out.get("upscale")
    if isinstance(up, dict) and len(up.get("segs") or []) > start:
        up2 = dict(up)
        up2["segs"] = list(up.get("segs"))[:start]
        out["upscale"] = up2
    # 全局记忆锚来自首段：整链重做（start==0）时旧锚必然失效，连同存档记录一并清除；
    # start>0 时首段保留，记忆锚沿用（由 E2 注入逻辑负责按新链首段重建）。
    if start == 0:
        out.pop("memory_anchor", None)
        old_ma = os.path.join(root, "memory_anchor.pt")
        if os.path.exists(old_ma):
            os.remove(old_ma)
    save_manifest(root, out)
    pat = re.compile(r"seg_(\d{3,})\.(?:pt|mp4)$")
    thumb_pat = re.compile(r"thumb_(\d{3,})\.png$")
    up_pat = re.compile(r"upseg_(\d{3,})\.(?:pt|mp4)$")
    up_thumb_pat = re.compile(r"up(?:thumb|last)_(\d{3,})\.png$")
    for sub in ("", "finals"):
        d = root if sub == "" else os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            m = pat.match(name) or thumb_pat.match(name) or up_pat.match(name) or up_thumb_pat.match(name)
            if m and int(m.group(1)) >= start:
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass
    return out


def ckpt_dir(params: dict, custom: str = "") -> str:
    """项目根目录：output/h3_projects/<自定义名> 或按参数指纹自动命名。

    四库规范：顺手建好 assets/finals/latent/texts（幂等，旧项目懒建）。
    """
    from folder_paths import get_output_directory

    if custom:
        root = os.path.join(get_output_directory(), "h3_projects", custom)
    else:
        name = (f"h3chain_{params['width']}x{params['height']}"
                f"_{params['length']}f_ctx{params['ctx']}_{fingerprint(params)}")
        root = os.path.join(get_output_directory(), "h3_projects", name)
    os.makedirs(root, exist_ok=True)
    ensure_project_dirs(root)
    return root


def _atomic_write(path: str, data: bytes):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        # 清理同目录残留 .part（24h 以上，避免崩溃堆积）
        try:
            d = os.path.dirname(path)
            now = __import__("time").time()
            for n in os.listdir(d):
                if n.startswith(".tmp_") and n.endswith(".part"):
                    p = os.path.join(d, n)
                    try:
                        if now - os.path.getmtime(p) > 86400:
                            os.remove(p)
                    except OSError:
                        pass
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _json_fallback(obj):
    """manifest 兜底：意外混入的 torch/numpy 0 维标量转 Python 数值再序列化，
    防一次类型疏漏炸掉整条链的存档写入；多元素张量仍报错——存档里不该有整块张量。"""
    item = getattr(obj, "item", None)
    if item is not None:
        try:
            return item()
        except (ValueError, RuntimeError, TypeError):
            pass
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def save_manifest(root: str, manifest: dict):
    _atomic_write(os.path.join(root, "manifest.json"),
                  json.dumps(manifest, ensure_ascii=False, indent=1,
                             default=_json_fallback).encode("utf-8"))


def load_manifest(root: str):
    path = os.path.join(root, "manifest.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def assert_match(old: dict, new: dict):
    """只校验**分辨率**（唯一硬约束）；其余参数变更只回报，不报错、不触发重做。

    为什么只有分辨率是硬约束：`PackedLayout` 按行预留，C/H/W 不同则桥拼不上，
    链根本走不通，只能整链重做或新建链。其余参数（steps/cfg/采样器/调度器/模型
    /fade_ratio/gate/实验开关）都只影响**此后要采样的段**——已完成段是盘上的
    张量，与参数没有任何耦合，改参数不会让它们"画风不一致"。
    （旧 docstring 声称"参数不同 = 链混搭画风不一致"，是伪问题，已证伪。）

    佐证：模型权重根本不在 params 里——换模型连检测都没有，而它对画风影响最大。

    返回：变了但不影响已有段的参数说明列表（调用方写进报告），无变化返回 []。
    分辨率不一致直接抛 ValueError（这是硬约束）。
    """
    old, new = old or {}, new or {}
    for key in ("width", "height"):
        if key in new and old.get(key, new[key]) != new[key]:
            raise ValueError(
                f"存档分辨率与当前不一致（{key}: 存档={old.get(key)!r} 当前={new[key]!r}）。"
                "分辨率是唯一硬约束——C/H/W 不同则 latent 桥拼不上，链无法延续。"
                "请把分辨率改回存档值，或换个新存档目录开新链")
    return [f"{k}: 存档={old.get(k)!r} 当前={new[k]!r}"
            for k in new if old.get(k, new[k]) != new[k]]


def seg_path(root: str, idx: int) -> str:
    return os.path.join(root, f"seg_{idx:03d}.pt")


def contiguous_done(root: str, done: int) -> int:
    """manifest 记录的完成段数，遇到缺失的段文件向前截断（该段起重新采样）。"""
    while done > 0 and not os.path.exists(seg_path(root, done - 1)):
        done -= 1
    return done


def save_segment(root: str, idx: int, video_t, audio_t):
    """段 AV latent 原子落盘（CPU 副本，不动显存里的原张量）。"""
    import torch

    payload = {
        "video": video_t.detach().cpu().clone(),
        "audio": audio_t.detach().cpu().clone(),
    }
    buf = tempfile.SpooledTemporaryFile(max_size=64 << 20)
    torch.save(payload, buf)
    buf.seek(0)
    _atomic_write(seg_path(root, idx), buf.read())
    buf.close()


def load_segment(root: str, idx: int):
    """读回段 AV latent（CPU 张量，调用方按需 .to(device)）。"""
    import torch

    with open(seg_path(root, idx), "rb") as f:
        payload = torch.load(f, map_location="cpu", weights_only=True)
    return payload["video"], payload["audio"]


def memory_anchor_path(root: str) -> str:
    """E2 全局记忆锚文件：整链首段开头 mem_t 个 token 的视频 latent 的 CPU 落盘。"""
    return os.path.join(root, "memory_anchor.pt")


def save_memory_anchor(root: str, video_t, mem_t: int, mem_hash: str) -> str:
    """E2：首段视频 latent 开头 mem_t token 落盘为全局记忆锚，返回记录串。

    mem_hash 由调用方以该段 latent 计算（参与 manifest 新键 memory_anchor，
    首段重做/实验指纹变化即整链重做，anchor 自动重建）。
    """
    import torch

    mt = max(1, min(int(mem_t), video_t.shape[2]))
    # 用 SpooledTemporaryFile 原子写（与 save_segment 同法）
    payload = video_t[:, :, :mt, :, :].detach().cpu().clone()
    buf = tempfile.SpooledTemporaryFile(max_size=64 << 20)
    torch.save(payload, buf)
    buf.seek(0)
    _atomic_write(memory_anchor_path(root), buf.read())
    buf.close()
    return json.dumps({"hash": mem_hash, "mem_t": mt}, ensure_ascii=False)


def load_memory_anchor(root: str):
    """读回首段记忆锚视频 latent（CPU 张量，调用方按需 .to(device)）。文件缺失返回 None。"""
    import torch

    p = memory_anchor_path(root)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return torch.load(f, map_location="cpu", weights_only=True)


def upscale_files(idx: int) -> dict:
    """二采渲染产物文件名（与基础段同名合并存储，不再有 upseg_* 副本族）。

    四库规范：mp4=finals/seg_NNN.mp4（高清直接覆盖基础分段名——段视频就是
    二采结果）；thumb=finals/thumb_NNN.png；last=finals/uplast_NNN.png
    （尾帧锚，下一段高清接缝平滑用）。旧根目录裸名读走 resolve 双兼容。
    """
    return {
        "mp4": f"finals/seg_{idx:03d}.mp4",
        "thumb": f"finals/thumb_{idx:03d}.png",
        "last": f"finals/uplast_{idx:03d}.png",
    }


def upscale_legacy_files(idx: int) -> tuple:
    """旧版二采独立产物文件名（重渲染该段时清理，防新旧文件混淆）。"""
    return (f"upseg_{idx:03d}.pt", f"upseg_{idx:03d}.mp4", f"upthumb_{idx:03d}.png")


def projects_root() -> str:
    from folder_paths import get_output_directory

    return os.path.join(get_output_directory(), "h3_projects")


# ---- 四库文件夹规范（资产库/成片库/latent库/文本库，物理子文件夹） ----
PROJECT_SUBDIRS = ("assets", "finals", "latent", "texts")


def ensure_project_dirs(root: str) -> dict:
    """建好 assets/finals/latent/texts 四个子文件夹，返回 {名: 绝对路径}。

    幂等；旧项目缺目录时懒建，不搬旧文件（读走双路径兼容，写走新路径）。
    """
    out = {}
    for name in PROJECT_SUBDIRS:
        p = os.path.join(root, name)
        os.makedirs(p, exist_ok=True)
        out[name] = p
    return out


def assets_dir(root: str) -> str:
    p = os.path.join(root, "assets")
    os.makedirs(p, exist_ok=True)
    return p


def finals_dir(root: str) -> str:
    p = os.path.join(root, "finals")
    os.makedirs(p, exist_ok=True)
    return p


def texts_dir(root: str) -> str:
    p = os.path.join(root, "texts")
    os.makedirs(p, exist_ok=True)
    return p


def resolve_project_file(root: str, filename: str) -> str:
    """项目内媒体文件名 -> 实际存在的绝对路径（双路径兼容）。

    查找序：原样（含子目录前缀）> finals/<basename> > 根目录 > assets/<basename>。
    都不存在时返回新规范路径（finals/<basename>，调用方按需新建）。
    filename 允许 "seg_000.mp4"（旧）或 "finals/seg_000.mp4"（新）。
    """
    norm = str(filename or "").strip().replace("\\", "/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts:
        return os.path.join(finals_dir(root), "")
    if len(parts) >= 2:
        cand = os.path.join(root, *parts)
        if os.path.isfile(cand):
            return cand
        # 前缀目录不对时回退按 basename 找
        base = parts[-1]
    else:
        base = parts[0]
        cand = os.path.join(root, base)
        if os.path.isfile(cand):
            return cand
    for d in ("finals", "assets"):
        cand2 = os.path.join(root, d, base)
        if os.path.isfile(cand2):
            return cand2
    return os.path.join(finals_dir(root), base)


def save_state(state: dict):
    """链状态指针写到 h3_projects/h3chain_state.json（审片面板据此定位当前链）。

    固定路径 + /api/view 端点：面板无需自建 HTTP 路由，也不必复刻指纹算法。
    """
    root = projects_root()
    os.makedirs(root, exist_ok=True)
    _atomic_write(os.path.join(root, "h3chain_state.json"),
                  json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8"))


def load_state():
    path = os.path.join(projects_root(), "h3chain_state.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_thumb(seg_dir: str, idx: int, frame) -> str:
    """段可见首帧 -> finals/thumb_NNN.png（长边 256）。Pillow 缺失时返回空串（面板降级占位）。

    四库规范：新文件一律写 finals/，返回 "finals/thumb_NNN.png"；
    旧根目录文件读走 resolve_project_file 双路径兼容，不主动搬。
    """
    name = f"thumb_{idx:03d}.png"
    rel = f"finals/{name}"
    try:
        from PIL import Image

        arr = (frame.detach().float().clamp(0.0, 1.0).cpu().numpy() * 255.0).astype("uint8")
        img = Image.fromarray(arr)
        w, h = img.size
        scale = 256.0 / max(w, h)
        if scale < 1.0:
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        img.save(os.path.join(finals_dir(seg_dir), name))
        return rel
    except Exception:
        return ""


def save_segment_mp4(seg_dir: str, idx: int, frames, wav, sample_rate: int,
                     fps: int = 24, fresh: bool = True, crf: int = 20,
                     preset: str = "veryfast", aq_mode=None, dither=False) -> str:
    """段可见帧 + 音轨 -> seg_NNN.mp4（编码实现在 media.save_av_mp4，与成片保存共用）。

    crf/preset/aq_mode/dither 为编码质量旋钮（默认值=现状兼容；二采 HQ 档
    由 upscale.render_segment 按编码档位解析后传入，基础链仍走默认）。

    缺失或编码失败返回空串（上游回退为缩略图，不影响主流程）。
    fresh=False（存档回放）且文件已存在时直接沿用，避免每次续跑全链重编码。

    四库规范：新文件写 finals/seg_NNN.mp4，返回 "finals/seg_NNN.mp4"；
    fresh=False 时 finals/ 与旧根目录双路径命中即沿用（返回实际命中的相对路径）。
    """
    name = f"seg_{idx:03d}.mp4"
    rel = f"finals/{name}"
    path = os.path.join(finals_dir(seg_dir), name)
    if not fresh:
        hit = resolve_project_file(seg_dir, name)
        if os.path.isfile(hit):
            return os.path.relpath(hit, seg_dir).replace("\\", "/")
    try:
        from .media import save_av_mp4    # 包内（ComfyUI 运行时）
    except ImportError:
        from media import save_av_mp4     # 顶层导入（无 ComfyUI 的单测环境）
    return name if save_av_mp4(path, frames, wav, sample_rate, fps,
                               crf=crf, preset=preset, aq_mode=aq_mode,
                               dither=dither) else ""
