"""断点续跑：共享参数指纹 + 逐段提示词哈希 + manifest 原子读写 + 段 AV latent 落盘。

断点只存采样输出的 latent（约 5 MB/段），不存解码像素（约 300 MB/段）：
续跑时重解码秒级回放，结果与一次跑完逐帧一致（种子序列由 manifest 权威记录）。

v2：指纹只覆盖共享参数（不含提示词、不含种子），改某段提示词仍指向同一条链；
提示词按段存哈希，改了第 N 段 -> reroll_start 找到首个不一致段，从该段起重做
（段 N 的锚定依赖段 N-1 尾帧，其后段必然级联重做）。

v3：存档根目录迁至 output/h3_projects/<项目名>/（游戏式一项目一文件夹），
manifest 增加 title/created_at/updated_at/finals 键；旧 checkpoints 目录不读不写。

torch / folder_paths 延迟导入：指纹与 manifest 逻辑在无 ComfyUI 环境下可单测。
"""

import hashlib
import json
import os
import re
import tempfile

SCHEMA = "h3seamless/ckpt-v3"


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


def reroll_start(old_hashes: list, new_hashes: list, done: int) -> int:
    """已完成段的提示词哈希 vs 当前提示词哈希 -> 应重做的首段下标。

    返回值 >= done 表示无需重做（调用方仅在返回值 < done 时截断，返回 0 = 整链重做）；
    首个不一致处即起点；提示词变少（done > len(new_hashes)）则截到新数量；
    仅末尾追加新段不影响已完成前缀。
    """
    for i, (old, new) in enumerate(zip(old_hashes[:done], new_hashes)):
        if old != new:
            return i
    return min(done, len(new_hashes))


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
    # 链结构变化（提示词改/手动重跑/调序）使重摇标记的槽位语义作废，一并清空
    if out.get("redo_queue"):
        out["redo_queue"] = []
    for key in ("seeds", "trims", "prompt_hashes", "thumbs", "videos", "prompts",
                "seams", "bridge_scores", "anchors", "seam_metrics"):
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
    for name in os.listdir(root):
        m = pat.match(name) or thumb_pat.match(name) or up_pat.match(name) or up_thumb_pat.match(name)
        if m and int(m.group(1)) >= start:
            os.remove(os.path.join(root, name))
    return out


def ckpt_dir(params: dict, custom: str = "") -> str:
    """项目根目录：output/h3_projects/<自定义名> 或按参数指纹自动命名。"""
    from folder_paths import get_output_directory

    if custom:
        root = os.path.join(get_output_directory(), "h3_projects", custom)
    else:
        name = (f"h3chain_{params['width']}x{params['height']}"
                f"_{params['length']}f_ctx{params['ctx']}_{fingerprint(params)}")
        root = os.path.join(get_output_directory(), "h3_projects", name)
    os.makedirs(root, exist_ok=True)
    return root


def _atomic_write(path: str, data: bytes):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


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
    """严格校验存档参数；不一致直接报错（种子例外，由调用方以存档为准）。

    为什么单段重摇也要参数一致：重摇段与链上其余段共用同一批锚定/桥接与
    采样配置，参数不同 = 新段与旧段画风不一致的链混搭，且存档 latent 的
    回放与续接假设同配置。前端提交重摇时已自动套用存档参数
    （applyChainParams），走到这里仍不一致的多为链结构（模式/首帧/参考素材
    组合，即 chain 键）或实验性功能开关变了——需要人工决策，不能静默改。
    旧存档缺少的新增参数键（如 smart_cut_max/drop_budget）按"沿用当前值"
    处理不算不一致——旧段生成时该功能不存在，当前值只影响后续段。
    唯 `experiments`（实验性功能组合指纹）例外：缺失按空串（=禁用基线）处理并
    双向严格比对——开关组合任何变化（关->开、开->关、组合替换、参数调整）都判
    不一致 -> 触发整链重做，杜绝不同实验复用同一缓存污染 A/B 结果。
    """
    exp_old = (old or {}).get("experiments", "")
    exp_new = (new or {}).get("experiments", "")
    diffs = [k for k in new if old.get(k, new[k]) != new[k]]
    if exp_old != exp_new:
        diffs.append("experiments")
    if diffs:
        detail = "; ".join(f"{k}: 存档={old.get(k, '')!r} 当前={new.get(k, '')!r}" for k in diffs)
        structural = any(k in diffs for k in ("chain", "experiments"))
        raise ValueError(
            f"存档参数与当前不一致（{detail}）。"
            + ("同一条链的续拍/重摇必须沿用原参数——重摇段与其余段共享锚定与采样配置，"
               "参数不同会导致新旧段画风不一致。" if not structural else
               "链结构（生成模式/首帧/参考素材组合）或实验性功能开关与存档不同，"
               "重摇前请先把这些切回存档时的状态。")
            + "数值类参数可在项目列表点「套用参数」一键还原；"
            "要换参数开新链，请在「存档目录」里填一个新名字")


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

    mp4=seg_NNN.mp4（高清直接覆盖基础分段名——段视频就是二采结果）；
    thumb=thumb_NNN.png；last=uplast_NNN.png（尾帧锚，下一段高清接缝平滑用）。
    """
    return {
        "mp4": f"seg_{idx:03d}.mp4",
        "thumb": f"thumb_{idx:03d}.png",
        "last": f"uplast_{idx:03d}.png",
    }


def upscale_legacy_files(idx: int) -> tuple:
    """旧版二采独立产物文件名（重渲染该段时清理，防新旧文件混淆）。"""
    return (f"upseg_{idx:03d}.pt", f"upseg_{idx:03d}.mp4", f"upthumb_{idx:03d}.png")


def projects_root() -> str:
    from folder_paths import get_output_directory

    return os.path.join(get_output_directory(), "h3_projects")


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
    """段可见首帧 -> thumb_NNN.png（长边 256）。Pillow 缺失时返回空串（面板降级占位）。"""
    name = f"thumb_{idx:03d}.png"
    try:
        from PIL import Image

        arr = (frame.detach().float().clamp(0.0, 1.0).cpu().numpy() * 255.0).astype("uint8")
        img = Image.fromarray(arr)
        w, h = img.size
        scale = 256.0 / max(w, h)
        if scale < 1.0:
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        img.save(os.path.join(seg_dir, name))
        return name
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
    """
    name = f"seg_{idx:03d}.mp4"
    path = os.path.join(seg_dir, name)
    if not fresh and os.path.exists(path):
        return name
    try:
        from .media import save_av_mp4    # 包内（ComfyUI 运行时）
    except ImportError:
        from media import save_av_mp4     # 顶层导入（无 ComfyUI 的单测环境）
    return name if save_av_mp4(path, frames, wav, sample_rate, fps,
                               crf=crf, preset=preset, aq_mode=aq_mode,
                               dither=dither) else ""
