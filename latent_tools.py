"""H3 latent 库内转码（run_transcode_job）：输入视频/成片现抽 latent 存档。

P4g（2026-09-20）：H3LatentExtract（现抽存档）与 H3LatentUpscale（库内放大）两个
画布节点已删除。它们没有执行入口（非 OUTPUT_NODE，且「报告」输出未接任何下游，
即使 mode=0 也不会进执行队列），全部前端代码零引用，属资产库时代残留。

转码能力保留在本模块：run_transcode_job 供主节点内联转码专跑调用（nodes.py）。
latent 库现存两源：主节点自动存档 + latent_slice 切片（latent/<名>.pt）。

编码复用 nodes.py 序章同式：video_vae.encode(center_cover(frames))，
音频 _encode_audio_latent 同式按画面等长 token 裁。纯函数部分（帧窗钳制/命名）
无 Comfy 也可单测。
"""

import os
import time

# 单次 VAE 编码帧数上限：唯一定义在 grid.MAX_WINDOW_FRAMES（H3 训练长度约 124–362 帧）。
# 超限直接报错指引缩小窗口，而不是整窗单次前向把显存/内存顶爆（转码卡死根因）。
# 双导入与本文件其余 grid 引用同口径：本模块在无 ComfyUI 环境下按文件路径单测，
# 那时没有包上下文，相对导入会失败。
try:
    from .grid import MAX_WINDOW_FRAMES as MAX_ENCODE_FRAMES
except ImportError:
    from grid import MAX_WINDOW_FRAMES as MAX_ENCODE_FRAMES


def _transcode_env_note(videoVAE, frames):
    """转码前诊断：只打印，不改行为。

    H3 视频 VAE 约 5GB（fp32 口径）；6-8GB 显存卡只能逐层从内存串流，
    24 帧也可能十几分钟。下次看到编码慢，先看这行判定是不是显存墙：
    是 → 启动参数加 --fp16-vae（VAE 减半，全链同对象，结果一致），
    而不是反复重试或调小窗口（24 帧已是最小可用窗）。
    """
    try:
        import torch
        shape = tuple(getattr(frames, "shape", ()))
        dev = getattr(videoVAE, "device", None)
        vram_gb, dtype_s = None, None
        try:
            if dev is not None and getattr(dev, "type", "") == "cuda":
                vram_gb = torch.cuda.get_device_properties(dev).total_memory / (1024 ** 3)
        except Exception:
            pass
        try:
            inner = getattr(videoVAE, "first_stage_model", videoVAE)
            for p in inner.parameters():
                dtype_s = str(getattr(p, "dtype", "")).replace("torch.", "")
                break
        except Exception:
            pass
        print(f"[H3库转存档] 输入 {shape} -> {dev}（VAE 精度 {dtype_s or '?'}"
              + (f"，本机显存 {vram_gb:.1f}GB" if vram_gb else "") + "）", flush=True)
        if vram_gb is not None and vram_gb < 8.0 and (dtype_s or "").startswith("float32"):
            print("[H3库转存档] 提示：显存 <8GB 且 VAE 为 fp32，编码会逐层串流很慢；"
                  "Comfy 启动参数加 --fp16-vae（VAE 减半、全链同对象，结果一致）可提速数倍。",
                  flush=True)
    except Exception:
        pass


def clamp_window(start_f, end_f, n):
    """像素帧窗钳制 -> (s0, s1)；切空抛 ValueError。"""
    try:
        s0, s1 = int(start_f), int(end_f)
    except (TypeError, ValueError):
        raise ValueError("start_f/end_f 须为整数像素帧") from None
    s0 = max(0, s0)
    s1 = min(max(0, int(n)), s1)
    if s1 <= s0:
        raise ValueError(f"帧窗为空（[{s0}, {s1})，源共 {n} 帧）")
    return s0, s1


def clean_latent_name(name):
    s = str(name or "").strip().replace("\\", "/").split("/")[-1]
    if not s or s.startswith(".") or "/" in s or "\\" in s or ":" in s or ".." in s:
        return ""
    if not os.path.splitext(s)[1]:
        return ""
    if not s.endswith(".pt"):
        return ""
    return s


def run_transcode_job(项目名, 源文件, 视频VAE, 音频VAE=None,
                      起始秒=0.0, 结束秒=0.0, 分支="图像+音频", 分开保存="否", 保存名="",
                      _pbar=None, _interrupted=None):
    """库内 mp4 -> latent 纯函数（供主节点转码任务调用）。

    有界：秒窗超 362 帧直接报错；解码最多读上限+1 帧。_pbar 有 ProgressBar 则
    分步推进；_interrupted() 为真则抛 Interrupted（主节点转码任务支持队列取消）。
    成功返回报告串；失败抛 ValueError/RuntimeError。
    """
    class _Interrupted(Exception):
        pass

    try:
        from comfy.model_management import InterruptProcessingException as _IPE
    except Exception:
        _IPE = None

    def _chk():
        if not callable(_interrupted):
            return
        try:
            stop = bool(_interrupted())
        except Exception:
            return
        if stop:
            raise (_IPE() if _IPE is not None else _Interrupted())

    import torch
    proj = str(项目名 or "").strip()
    if not proj:
        raise ValueError("项目名不能为空")
    try:
        from . import checkpoint as _ckpt
    except ImportError:
        import checkpoint as _ckpt
    try:
        from . import projects as _proj
    except ImportError:
        import projects as _proj
    from folder_paths import get_output_directory
    if _proj.safe_name(proj) != proj:
        raise ValueError(f"非法项目名：{proj!r}")
    root = os.path.join(get_output_directory(), "h3_projects", proj)
    manifest = _ckpt.load_manifest(root)
    if manifest is None:
        raise ValueError(f"项目不存在：{proj}（先新建或跑一段）")
    src_path = _ckpt.resolve_project_file(root, str(源文件 or ""))
    if not os.path.isfile(src_path):
        raise ValueError(f"源文件不存在：{源文件}（须为项目内 mp4）")
    try:
        from . import media as _media
    except ImportError:
        import media as _media
    if _pbar is None:
        try:
            import comfy.utils as _cu
            _pbar = _cu.ProgressBar(4)
        except Exception:
            _pbar = None
    real_fps = _media.probe_fps(src_path, 24.0)
    try:
        ss = max(0.0, float(起始秒 or 0.0))
    except (TypeError, ValueError):
        ss = 0.0
    try:
        es = float(结束秒 or 0.0)
    except (TypeError, ValueError):
        es = 0.0
    s0 = max(0, int(round(ss * real_fps)))
    s1 = int(round(es * real_fps)) if es > 0 else None
    if s1 is not None and s1 <= s0:
        raise ValueError(f"秒窗为空（[{ss}s, {es}s) -> 帧[{s0}, {s1})）")
    if s1 is not None and s1 - s0 > MAX_ENCODE_FRAMES:
        raise ValueError(
            f"秒窗 {s1 - s0} 帧超出单次编码上限 {MAX_ENCODE_FRAMES} 帧（约 15 秒，模型训练长度）："
            "请缩小窗口（转 keyframe 一般只取尾部几秒），分多次转存")
    print(f"[H3库转存档] 解码帧窗 [{s0}, {s1 if s1 is not None else '尾'}）@{real_fps:.2f}fps…",
          flush=True)
    # 解码本身也有界：最多读到上限+1 帧，多 1 帧即判定超限（到片尾模式
    # 不会把整片解进内存——之前卡死的另一半原因）
    decode_end = s1 if s1 is not None else s0 + MAX_ENCODE_FRAMES + 1
    frames, wav, sr = _media.decode_av(src_path, s0, decode_end, real_fps)
    n = int(frames.shape[0])
    if n > MAX_ENCODE_FRAMES:
        raise ValueError(
            f"窗口共 {n} 帧，超出单次编码上限 {MAX_ENCODE_FRAMES} 帧（约 15 秒）："
            "请填写结束秒缩小窗口（转 keyframe 一般只取尾部几秒），分多次转存")
    s1 = s0 + n
    _chk()
    if _pbar is not None:
        _pbar.update(1)
    want_v = str(分支 or "") != "仅音频"
    want_a = str(分支 or "") != "仅图像"
    cut = frames
    _transcode_env_note(视频VAE, cut)
    print(f"[H3库转存档] 视频VAE 编码 {n} 帧…", flush=True)
    v_lat = 视频VAE.encode(cut) if want_v else None
    _chk()
    if _pbar is not None:
        _pbar.update(1)
    a_lat = None
    if want_a:
        if 音频VAE is None:
            raise ValueError("含音频分支时必须接「音频VAE」")
        try:
            from .nodes import _encode_audio_latent as _enc_a
        except ImportError:
            from nodes import _encode_audio_latent as _enc_a
        try:
            from .grid import audio_tokens_for_frames as _atok
        except ImportError:
            from grid import audio_tokens_for_frames as _atok
        rate = int(sr or 44100)
        if wav is not None and getattr(wav, "numel", lambda: 0)() > 0:
            a0 = max(0, int(round(s0 / real_fps * rate)))
            a1 = max(a0, int(round(s1 / real_fps * rate)))
            sub = wav[..., a0:a1] if a1 > a0 else wav
            audio = {"waveform": sub, "sample_rate": rate}
        else:
            import torch as _t
            audio = {"waveform": _t.zeros(1, 1, 0), "sample_rate": rate}
        print(f"[H3库转存档] 音频VAE 编码 {_atok(s1 - s0)} tokens…", flush=True)
        a_lat = _enc_a(音频VAE, audio, _atok(s1 - s0))
        if _pbar is not None:
            _pbar.update(1)
    sname = clean_latent_name(保存名)
    if not sname:
        raise ValueError(f"非法保存名：{保存名!r}（须以 .pt 结尾）")
    ldir = os.path.join(root, "latent")
    os.makedirs(ldir, exist_ok=True)
    stem = sname[:-3]
    outs = []
    if want_v and want_a and str(分开保存) == "是":
        payloads = [("_v", {"video": v_lat.detach().cpu().clone()}, "video"),
                    ("_a", {"audio": a_lat.detach().cpu().clone()}, "audio")]
    else:
        payload = {}
        if v_lat is not None:
            payload["video"] = v_lat.detach().cpu().clone()
        if a_lat is not None:
            payload["audio"] = a_lat.detach().cpu().clone()
        kd = "av" if (v_lat is not None and a_lat is not None) \
            else ("video" if v_lat is not None else "audio")
        payloads = [("", payload, kd)]
    import tempfile
    for suffix, payload, kd in payloads:
        fn = f"{stem}{suffix}.pt"
        tmp = os.path.join(ldir, fn + ".part")
        buf = tempfile.SpooledTemporaryFile(max_size=64 << 20)
        torch.save(payload, buf)
        buf.seek(0)
        with open(tmp, "wb") as fh:
            fh.write(buf.read())
        buf.close()
        os.replace(tmp, os.path.join(ldir, fn))
        manifest.setdefault("latents", [])
        manifest["latents"] = [x for x in manifest["latents"]
                               if isinstance(x, dict) and x.get("file") != f"latent/{fn}"]
        manifest["latents"].append({"file": f"latent/{fn}", "src": str(源文件),
                                    "start_f": s0, "end_f": s1, "kind": kd,
                                    "updated_at": time.time()})
        outs.append(f"latent/{fn}({kd})")
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    _ckpt.save_manifest(root, manifest)
    if _pbar is not None:
        _pbar.update(1)
    rep = (f"库转存档：{os.path.basename(src_path)} 帧[{s0}, {s1}) "
           f"{s1 - s0}帧@{real_fps:.2f}fps -> {', '.join(outs)}")
    print(f"[H3库转存档] {rep}", flush=True)
    return rep
