"""H3 Seamless Chain —— MiniMax H3 多段视频段间引导（无缝续拍）单节点。

原理：生成第 N+1 段时，把第 N 段结尾 context_frames 帧从其采样输出的 AV latent
里直接切片（不解码不重编码，零颜色漂移），作为 keyframe 钉在第 N+1 段头部
（官方 conditioning 协议 minimax_keyframes，锚 resolved_frame_index=0），
采样每步重注入，顺着上一段尾部的运动继续画；解码后裁掉头部重叠桥再拼接。

支持逐段审片（每次运行只生成一段新内容即返回，重新运行继续）与任意段重跑
（改某段提示词自动从该段重做；「重跑起始段」+ 换种子可从指定段重摇），
进度存于 latent 存档（v2 schema：逐段种子 / 提示词哈希 / 裁剪量）。

输入形态与官方 MiniMaxH3ReferenceToVideo 对齐（autogrow）：
- 提示词_1..N：每段一个输入框（或接 PrimitiveStringMultiline）
- 参考图片_0..9（<Picture i>）/ 参考视频_0..3（<Video k>）
- 参考视频音轨_0..3（与同号视频配对）/ 参考音频_0..3（<Audio j>）
- 起始视频 + 起始视频音轨：可选序章——上传视频编码为第 0 段（进存档可回放），
  成片以它开头，后续生成段从其结尾续拍（24fps 约定，经一次 VAE 重编码）

「自动保存=分段」时每段 mp4 自动落盘项目文件夹，完整成片由「自动成片」开关控制
output/h3_projects/<项目名>/（游戏式存档：一项目一文件夹，导演台可读档/删除）。

兼容性：不 monkey-patch；conditioning/latent 构造直接调用官方
MiniMaxH3ImageToVideo / MiniMaxH3ReferenceToVideo 节点类，采样走官方
common_ksampler。多 token 桥与 keyframe 音频需要 ComfyUI 含 PR #15439
（2026-08-09 之后构建）；旧版 PackedLayout 每个 keyframe 只分配单帧 latent
行数，钉多 token 桥会形状错位，运行时自动探测并降级为单帧桥（报告说明）。
"""

import gc
import os
import re
import time
import json

import torch
import nodes
import node_helpers
import folder_paths
import comfy.utils
import comfy.samplers
from comfy_extras.nodes_minimax_h3 import MiniMaxH3ImageToVideo, MiniMaxH3ReferenceToVideo
from comfy_api.latest import io

from . import checkpoint
from . import metrics
from . import experiments
from . import transition
from . import bridge
from . import guides
from .grid import (video_latent_t, latent_t_to_frames, frames_to_latent_t,
                   audio_tokens_for_frames, align_frame_count_down)

try:
    import comfy.ldm.minimax.model as _minimax_model
    # PR #15439 引入的模块级函数；存在即代表支持 keyframe 音频与 refs/keyframes 合并
    KEYFRAME_AUDIO_SUPPORTED = hasattr(_minimax_model, "_ref_t_span")
except Exception:
    _minimax_model = None
    KEYFRAME_AUDIO_SUPPORTED = False

_full_bridge_cache = None


def cond_audio_rows_guard(dit):
    """安装 _cond_audio_rows 兜底 patch，返回恢复函数（try/finally 调用）。

    ComfyUI 0.33+ 的 PackedLayout 按 keyframe 的 audio_latent 声明 cond_audio
    段；共存 H3 插件（如 H3-Motion-Context 的 keyframe/ref 共存 patch）会
    重建 payload 并丢弃 keyframe 音频，导致 audio_embed 行数不足形状错位。
    行数与 keyframes+refs 声明不符时用 payload 里完好的素材在线重建
    （layout 段顺序：kf 音频在前、refs 音频在后）。续拍/分镜两节点共用。
    """
    orig = dit._cond_audio_rows

    def fixed(payload, device, _orig=orig):
        rows = _orig(payload, device)
        want = [z for z in (kf.get("audio_latent")
                            for kf in (payload.get("keyframes") or [])) if z is not None]
        want += [z for z in (r.get("audio_latent")
                             for r in (payload.get("refs") or [])) if z is not None]
        expected = sum(int(z.shape[-1]) * 2 for z in want)
        if want and (0 if rows is None else int(rows.shape[0])) != expected:
            rows = _orig({"cond_audio_latents": want,
                          "audio_cond_noise_aug": payload.get("audio_cond_noise_aug"),
                          "seed": payload.get("seed")}, device)
        return rows

    dit._cond_audio_rows = fixed
    return lambda: setattr(dit, "_cond_audio_rows", orig)


def cond_video_rows_guard(dit):
    """安装 _cond_video_rows 兜底 patch，返回恢复函数（try/finally 调用）。

    ComfyUI 0.33.0~0.33.4 的 MiniMaxH3.extra_conds 在 refs 分支用「=」覆盖了
    keyframes 分支刚写入的 cond_video_latents（0.34.2 起才改成 +=）：

        keyframes -> payload["cond_video_latents"] = [kf latent...]
        refs      -> payload["cond_video_latents"] = [ref latent...]   # 覆盖！

    于是「段间桥 keyframe + 参考图」同段共存时，keyframe 的 latent 被整个丢掉，
    而 PackedLayout 仍按 keyframes+refs 两者预留行数 —— _forward 里
    all_video_rows[~img_update] = cond_video_rows 直接形状错位：
        shape mismatch: [405, 96] cannot be broadcast to [810, 96]
    （405 = 864×480 基础画幅一帧的 2×2 patch 行数，810 = 桥锚 + 参考图两份）

    触发面：非首段且本段挂了参考素材（首段无桥所以不挂 keyframe，反而平安）；
    与二采无关（关掉二采照样崩）。与 cond_audio_rows_guard 同机制，按 layout
    段顺序（keyframe 在前、refs 在后）用 payload 里完好的素材在线重建。
    """
    orig = dit._cond_video_rows

    def fixed(payload, device, _orig=orig):
        want = [kf["latent"] for kf in (payload.get("keyframes") or [])
                if kf.get("latent") is not None]
        want += [r["latent"] for r in (payload.get("refs") or []) if "latent" in r]
        supplied = payload.get("cond_video_latents") or []
        # 身份比较：命中官方正确实现（0.34.2+）时零改动，只在被覆盖/被别的
        # 插件重建过时才接管；不比较张量内容，避免无谓的 GPU 同步
        if want and [id(z) for z in supplied] != [id(z) for z in want]:
            payload = dict(payload)
            payload["cond_video_latents"] = want
        return _orig(payload, device)

    dit._cond_video_rows = fixed
    return lambda: setattr(dit, "_cond_video_rows", orig)


def step_cond_noise_guard(dit, aug_start, aug_end, fade_ratio):
    """递减锚定：visual_cond_noise_aug 随采样进度从强到弱递减到消失。

    patch dit.forward，每步读 sigma_v 计算进度，修改 minimax_payload 里的
    visual/audio cond_noise_aug。同时影响 _cond_video_rows（噪声混合比）和
    _forward（seg_t["cond"] 时间步钳位）——两者都从 payload 读同一个值。

    sigma_v: 1.0（采样开始）→ 0.0（采样结束）
    progress = (1 − sigma_v) / fade_ratio，clamp [0, 1]
    dynamic_aug = aug_start + (aug_end − aug_start) × progress

    aug_start: 初始值（接近 1.0 = 硬锚定，接缝吻合）
    aug_end: 终点值（0.0 = 纯噪声，锚定完全消失）
    fade_ratio: 递减占总步数的比例（0.3 = 前 30% 步数内递减完毕）
    """
    orig_forward = dit.forward

    def patched_forward(x, timestep, context, transformer_options={}, **kwargs):
        sigma_v = float((timestep.flatten()[0] / 1000.0).clamp(min=1e-6))
        progress = min((1.0 - sigma_v) / fade_ratio, 1.0) if fade_ratio > 0 else 1.0
        dynamic_aug = aug_start + (aug_end - aug_start) * progress

        payload = kwargs.get("minimax_payload")
        if payload is not None:
            payload = dict(payload)
            payload["visual_cond_noise_aug"] = dynamic_aug
            payload["audio_cond_noise_aug"] = max(0.0, dynamic_aug - 0.5)
            kwargs["minimax_payload"] = payload

        return orig_forward(x, timestep, context, transformer_options, **kwargs)

    dit.forward = patched_forward
    return lambda: setattr(dit, "forward", orig_forward)


def full_bridge_supported():
    """多 token keyframe 探测（进程内缓存一次）。

    旧版 PackedLayout（PR #15439 之前）对每个 keyframe 固定只分配 1 帧 latent
    的行数且仅认首/尾锚点，钉 22 帧桥（7 token，2835 行）会形状错位
    （[2835,96] 无法广播到 [405,96]）。构造一个 2 token 的微型 keyframe 布局
    实测 cond 行数：翻倍即支持完整桥，否则调用方降级为单帧桥。
    """
    global _full_bridge_cache
    if _full_bridge_cache is not None:
        return _full_bridge_cache
    ok = False
    if _minimax_model is not None:
        try:
            import torch
            layout = _minimax_model.PackedLayout(
                1, 2, 8, 8, 4,
                keyframes=[{"resolved_frame_index": 0,
                            "latent": torch.zeros(1, 24, 2, 8, 8)}])
            ok = int((~layout.img_update).sum()) == 32  # 2 token × 16 行（8×8 latent）
        except Exception:
            ok = False
    _full_bridge_cache = ok
    return ok


def _apply_anchor_noise(cond, aug):
    """锚定加噪：写入 H3 cond 噪声增强 kwargs（模型侧 aug<1 时按比例混噪）。

    SkyReels-V2 addnoise_condition 思路：干净锚定帧让模型逐帧复现（段首刹车/
    内容重演），加噪后锚定退化为软参考。音频加噪减半（音频桥窗口短，过噪伤听感）。
    续拍（桥锚定）与分镜（首尾帧锚定）两节点共用。
    """
    if aug <= 0.0:
        return cond
    return node_helpers.conditioning_set_values(cond, {
        "minimax_visual_cond_noise_aug": round(1.0 - aug, 4),
        "minimax_audio_cond_noise_aug": round(1.0 - aug * 0.5, 4),
    })


def _decode_audio(audio_vae, audio_latent, norm_skip_frac=0.0):
    # 与官方 VAEDecodeAudio（comfy_extras/nodes_audio.vae_decode_audio）保持一致；
    # norm_skip_frac>0 时归一化 std 只统计保留区——锚定区音频随后整段裁掉，
    # 若计入会抬高归一化分母、系统性压低本段响度，接缝处即响度跳变
    audio = audio_vae.decode(audio_latent).movedim(-1, 1)
    if 0.0 < norm_skip_frac < 1.0:
        body = audio[..., round(audio.shape[-1] * norm_skip_frac):]
    else:
        body = audio
    std = torch.std(body, dim=[1, 2], keepdim=True) * 5.0
    std[std < 1.0] = 1.0
    audio = audio / std
    sample_rate = getattr(audio_vae, "audio_sample_rate_output",
                           getattr(audio_vae, "audio_sample_rate", 44100))
    return audio, sample_rate


def _tail_keyframe(video_t, audio_t, ctx_frames, with_audio, end_tokens=None, full_bridge=True):
    """上段尾部 ctx 帧 latent 直切为 keyframe；end_tokens 为输出末端 token 边界。

    end_tokens=None（序章等外部源）取原始尾部；生成段必须传 kept 末端对齐值，
    保证锚定末端 == 输出末端——否则下段续拍点落在本段从未输出的网格填充帧上，
    每个接缝跳过最多 16 帧内容（观感即"接缝跳变"）。
    旧版 ComfyUI keyframe 协议只收单帧 latent：full_bridge=False 时只钉
    尾部最后 1 个 token（承载上段末尾画面），且不附音频。
    """
    vt = video_latent_t(ctx_frames) if full_bridge else 1
    end = video_t.shape[2] if end_tokens is None else min(end_tokens, video_t.shape[2])
    kf = {
        "resolved_frame_index": 0,
        "latent": video_t[:, :, end - vt:end, :, :].clone(),
    }
    if with_audio and full_bridge and audio_t is not None:
        at = audio_tokens_for_frames(ctx_frames)
        aend = min(audio_tokens_for_frames(latent_t_to_frames(end)), audio_t.shape[-1])
        if 0 < at <= aend:
            kf["audio_latent"] = audio_t[..., aend - at:aend].clone()
    return kf


def _center_cover(frames, width, height):
    """[F,H,W,3] -> [F,height,width,3]，与官方 _resize(..., "center") 同语义的 cover-crop。"""
    x = frames[..., :3].movedim(-1, 1).float()
    x = comfy.utils.common_upscale(x, width, height, "lanczos", "center")
    return x.movedim(1, -1)


_FRAME_REF_KINDS = ("首帧图", "尾帧图")


def _default_frame_refs(i, n, has_first, has_end):
    """段级首尾帧图引用的缺省值（旧档行为）：首段参考首帧图、末段参考尾帧图。"""
    refs = []
    if has_first and i == 0:
        refs.append("首帧图")
    if has_end and i == n - 1:
        refs.append("尾帧图")
    return refs


def _parse_frame_refs(segments, n, has_first, has_end):
    """解析段级首尾帧图引用（segments[i].frame_refs，仅首帧模式有意义）。

    返回 (picked, explicit)：
    picked[i] = 该段有效引用列表（未知标签忽略、去重、保序）；
    explicit[i] = 该段 JSON 是否显式写了 frame_refs 数组——缺省段沿用默认行为，
    哈希与旧存档完全兼容；显式勾选可给中段注入首帧图头部身份锚 / 任意段注入
    尾帧图尾部锚，也可对首末段关闭默认参考（改动进逐段哈希，从该段起重做）。
    """
    picked, explicit = [], []
    for i in range(n):
        seg = segments[i] if i < len(segments) and isinstance(segments[i], dict) else {}
        raw = seg.get("frame_refs")
        if isinstance(raw, list):
            explicit.append(True)
            sel = []
            for x in raw:
                k = str(x).strip()
                if k in _FRAME_REF_KINDS and k not in sel:
                    sel.append(k)
            picked.append(sel)
        else:
            explicit.append(False)
            picked.append(_default_frame_refs(i, n, has_first, has_end))
    return picked, explicit


_REDO_MODES = ("双锚", "仅锚上段", "仅锚下段", "无锚")


def _parse_redo_segs(ds, done, exec_kinds, off, disabled=None):
    """导演台重摇标记 ds.redo_segs -> 合法 (slot, mode) 列表（去重，按提交序）。

    slot=全局槽位 0-based（含序章位，与二采 include 同口径）；合法性：
    已完成（slot < done，未完成段本来就要生成）、非序章（slot >= off）、
    提示词段（插入段/序章各有专用操作，不可重摇）、mode 合法、非禁用段
    （禁用段不执行不进成片，重摇无意义——前端 redoSlotValid 同口径拦截，
    这里防御性复验）。无有效标记返回空列表（普通续跑/回放，行为与现状完全一致）。
    disabled=按 exec_items 位置的禁用布尔表（与 exec_kinds 同口径；
    None=全部启用，兼容旧调用）。
    """
    raw = ds.get("redo_segs") if isinstance(ds, dict) else None
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for x in raw:
        if not isinstance(x, dict):
            continue
        try:
            slot = int(x.get("slot"))
        except (TypeError, ValueError):
            continue
        mode = str(x.get("mode") or "双锚")
        if mode not in _REDO_MODES:
            continue
        if slot in seen or not off <= slot < done:
            continue
        i = slot - off
        k = exec_kinds[i] if 0 <= i < len(exec_kinds) else ""
        if k != "prompt":
            continue
        if disabled is not None and 0 <= i < len(disabled) and disabled[i]:
            continue
        out.append((slot, mode))
        seen.add(slot)
    return out


def _redo_seed(ctrl_seed, i, archived):
    """重摇段种子：控件种子等差序列（与「重跑起始段」同规则），再与该段存档
    种子相同时 +1（bump）——保证每次重摇种子必变，驱动 base_hash 变化，
    二采记录自动失效重渲（重摇换种子 = 高清必须跟着重做）。
    连续多次重摇同段：本段存档种子已被上次更新，bump 天然交替不出死循环。
    """
    s = (int(ctrl_seed) + i) % 0xffffffffffffffff
    if archived is not None:
        try:
            if s == int(archived) % 0xffffffffffffffff:
                s = (s + 1) % 0xffffffffffffffff
        except (TypeError, ValueError):
            pass
    return s


def _merged_list(new_seq, old_seq, upto):
    """选择性重做的 manifest 列表合并：新积累前缀 + 旧记录补尾。

    审片中段 break 时循环内积累的列表长度 < done，直接写 [:done] 会丢后续
    保留段记录；正常顺序推进（len >= upto）时纯截断，与现状逐位一致。
    旧列表长度不足时切片自动安全（补不满的部分维持缺失，与现状同）。"""
    new_seq = list(new_seq or [])
    old_seq = list(old_seq or [])
    if len(new_seq) >= upto:
        return new_seq[:upto]
    return new_seq + old_seq[len(new_seq):upto]


def _seam_tail_kf(video_t, skip_frames):
    """下一段保留段存档 latent -> 重摇段尾锚 latent（裁头后首个可见 token 直切）。

    帧归属 token：帧0→token0，帧 f>=1 → token (f+3)//4（token t 含帧
    [4t-3, 4t]）。锚「裁头后首帧所在 token」= 接缝区（token 内含少量上段
    桥锚定帧 + 下段首帧，正是接缝过渡语义）。无可见内容（越界）返回 None。
    单 token 视觉锚，与每段尾帧锚定同形态（不带音频）。
    """
    k = (int(skip_frames) + 3) // 4
    if k < 0 or k >= video_t.shape[2]:
        return None
    return video_t[:, :, k:k + 1, :, :].clone()


def _autosave_final(root, frames, wav, sample_rate, fps=24, crf=20, preset="veryfast",
                    aq_mode=None, dither=False):
    """完整链（或审片已确认部分）PyAV 编码成片到项目文件夹，成功返回路径。

    失败时返回 (None, error_msg) 供调用方写入报告；成功返回 (path, None)。
    """
    from . import media
    try:
        path = os.path.join(root, f"final_{time.strftime('%Y%m%d_%H%M%S')}.mp4")
        print(f"[H3自动保存] 编码完整成片：{int(frames.shape[0])} 帧 → {path}（编码期间 CPU 升高属正常）")
        t0 = time.time()
        ok = media.save_av_mp4(path, frames, wav, sample_rate, fps,
                               crf=crf, preset=preset, aq_mode=aq_mode, dither=dither)
        print(f"[H3自动保存] 成片编码{'完成' if ok else '失败'}：{time.time() - t0:.0f}s")
        return (path, None) if ok else (None, media.last_error)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[H3自动保存] 成片编码异常：{err}")
        return None, err


def _encode_audio_latent(audio_vae, audio, tokens):
    """AUDIO dict -> [1,32,2,T] latent，裁到与画面等长的 token 数。

    仿官方 _encode_ref_audio 语义：重采样到音频 VAE 采样率后整段编码。
    torchaudio 延迟导入（ComfyUI 自带，无 ComfyUI 的结构单测不触达）。
    """
    import torchaudio

    waveform = audio["waveform"]
    sr = int(audio.get("sample_rate") or 0)
    vae_sr = getattr(audio_vae, "audio_sample_rate", 32000)
    if sr and sr != vae_sr:
        waveform = torchaudio.functional.resample(waveform, sr, vae_sr)
    z = audio_vae.encode(waveform[:1].movedim(1, -1))
    return z[..., :tokens].clone()


def _autogrow_items(group, prefix):
    """autogrow dict -> 按序号排序的非 None 值，压实为从 0 连续编号的官方 ref_* 键名。

    压实保证 <Picture 1>/<Video 1>/<Audio 1> 标签始终对应"按连接顺序的第 1 个"，
    与官方 note 的 "in the exact order they were connected" 语义一致，
    即使用户中间留了空输入框也不会错位。
    """
    if not group:
        return {}
    def num(k):
        try:
            return int(str(k).rsplit("_", 1)[-1])
        except ValueError:
            return 0
    vals = [v for _, v in sorted(group.items(), key=lambda kv: num(kv[0])) if v is not None]
    return {f"{prefix}{i}": v for i, v in enumerate(vals)}


def _load_input_image(filename):
    """从 ComfyUI input 目录加载图片为 IMAGE 张量（与 LoadImage 节点同格式：[1,H,W,C] float32 0-1）。"""
    from PIL import Image, ImageOps
    import numpy as np
    image_path = folder_paths.get_annotated_filepath(str(filename))
    img = node_helpers.pillow(Image.open, image_path)
    img = node_helpers.pillow(ImageOps.exif_transpose, img)
    if img.mode != 'RGB':
        img = img.convert('RGB')
    return torch.from_numpy(np.array(img).astype(np.float32) / 255.0).unsqueeze(0)


def _load_input_video(filename):
    """input 目录视频 -> (帧张量, 音轨)：与 LoadVideo+GetVideoComponents 画布链同格式。

    音轨与帧同源自同一文件，即「参考视频音轨」自动配对，无需单独上传。
    """
    from comfy_api.latest import InputImpl
    video_path = folder_paths.get_annotated_filepath(str(filename))
    comps = InputImpl.VideoFromFile(video_path).get_components()
    if comps.images is None or not getattr(comps.images, "shape", None):
        raise ValueError(f"参考视频「{filename}」解不出画面帧：请确认是可解码的 24fps 视频（2-15 秒）")
    if comps.audio is None:
        import torch as _t
        comps.audio = {"waveform": _t.zeros(1, 1, 0), "sample_rate": 24000}
    return comps.images, comps.audio


def _load_input_audio(filename):
    """input 目录音频 -> AUDIO dict（与 LoadAudio 节点输出同格式）。"""
    from comfy_extras.nodes_audio import load
    audio_path = folder_paths.get_annotated_filepath(str(filename))
    waveform, sample_rate = load(audio_path)
    return {"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate}


def _parse_director_state(raw):
    """解析导演台状态 JSON。空/非法时返回空 dict，不影响原有画布接线工作流。"""
    if not raw:
        return {}
    try:
        d = json.loads(str(raw)) if isinstance(raw, str) else raw
        return d if isinstance(d, dict) else {}
    except (TypeError, ValueError):
        return {}


# ---- 官方 Resolution Selector 同款画布换算 + 时长网格吸附 ----

_AR_RATIO = {"21:9": 21 / 9, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "1:1": 1.0}
_MP_OPTIONS = [round(i * 0.1, 1) for i in range(1, 21)]   # 0.1–2.0MP，0.1 步进（官方箭头微调同款）
_LABEL_TOKEN = re.compile(r"\[\[([^\[\]]{1,24})\]\]")


def _resolve_canvas(ar, mp):
    """宽高比+百万像素 -> 画布（官方 Resolution Selector 公式逐位移植，comfy_extras/nodes_resolution.py）。

    1MP = 1024×1024 = 1048576 px（官方口径，非 1e6）；两侧各自 round 对齐 32 倍数，无上限收敛。
    16:9 档位对照官方节点：0.2→608×352 / 0.5→960×544 / 0.98→1344×768（H3 原生）/ 1.0→1376×768 /
    1.2→1504×832 / 1.5→1664×928 / 2.0→1920×1088；9:16+1.0 → 768×1376。
    """
    r = _AR_RATIO[str(ar)]
    total = float(mp) * 1024 * 1024
    w0, h0 = (total * r) ** 0.5, (total / r) ** 0.5
    return round(w0 / 32) * 32, round(h0 / 32) * 32


def _snap_seconds(seconds):
    """秒 -> 就近的 17k+5 帧网格（@24fps，≥5 帧）。5.0s→124，与旧默认帧数一致。"""
    f = max(5, int(round(float(seconds) * 24)))
    k = max(0, round((f - 5) / 17))
    return 17 * k + 5


REF_CAPS = {"image": 9, "video": 3, "audio": 3}   # 官方单段参考上限（nodes_minimax_h3 autogrow max）
_KIND_NAME = {"image": "图片", "video": "视频", "audio": "音频"}


def _normalize_order(label_order):
    """段引用顺序 -> [(kind, 标签)]；纯字符串元素按旧格式视为 image（兼容旧调用/旧测试）。"""
    out = []
    for x in label_order:
        out.append(("image", x) if isinstance(x, str) else (str(x[0]), str(x[1])))
    return out


def _kind_tokens(label_order):
    """[(kind, 标签)] -> 按类别独立编号的 {标签: token}（图 <Picture k> / 视 <Video k> / 音 <Audio j>）。"""
    counters = {"image": 0, "video": 0, "audio": 0}
    mapping = {}
    for kind, lbl in label_order:
        counters[kind] = counters.get(kind, 0) + 1
        token = {"image": "<Picture {}>", "video": "<Video {}>", "audio": "<Audio {}>"}.get(kind)
        if token is None:
            raise ValueError(f"未知素材类别「{kind}」：必须是 image / video / audio")
        mapping[lbl] = token.format(counters[kind])
    return mapping


def _apply_label_tokens(prompt, label_order):
    """把提示词中的 [[标签]] 替换为该段压实编号后的 <Picture k>/<Video k>/<Audio j>。

    label_order 为该段按序引用的 (kind, 标签)（或旧格式纯标签=image）；各类别独立从 1
    编号；长标签先替换，防「角色1」吃掉「角色10」前缀；剩余未知 [[..]] 报错并列出可用标签。
    """
    mapping = _kind_tokens(_normalize_order(label_order))
    out = prompt
    for lbl in sorted(mapping, key=len, reverse=True):
        out = out.replace(f"[[{lbl}]]", mapping[lbl])
    unknown = _LABEL_TOKEN.findall(out)
    if unknown:
        raise ValueError(f"提示词引用了未知素材标签「{unknown[0].strip()}」：可用标签 {list(mapping)}")
    return out


def _reference_header(label_order):
    """段首自动注入的参考定义（官方 Ref2VA subject_definitions 风格），三类 token 各自编号。"""
    mapping = _kind_tokens(_normalize_order(label_order))
    role = {"Picture": "reference image", "Video": "reference video", "Audio": "reference audio"}
    lines = [f"{tok} is the {role[tok.split()[0][1:].capitalize()]} for {lbl}, "
             f"used as a generation anchor of this segment."
             for lbl, tok in mapping.items()]
    return "subject_definitions:\n" + "\n".join(lines)


# 官方 h3-prompt-writing 三字段标签：主提示词含任一即视为已是官方格式，直通不包装
_OFFICIAL_FIELD_RE = re.compile(
    r"integrated_multimodal_description\s*:|overall_soundscape\s*:|detailed_description\s*:")


def _wrap_dialogue(text):
    """中文对白「……」→ 官方 <d>[中文] ……</d>。

    只动「」（中文对白惯例）；英文双引号是官方可见屏幕文字记法，不动。
    """
    return re.sub(r"「([^「」]*)」", r"<d>[中文] \1</d>", text)


def _compose_official(scene, char, prompt, soundscape, music):
    """按官方 h3-prompt-writing 结构组装单段提示词。

    - prompt 已是官方格式（三字段任一标签）→ 原样直通（含 [[标签]] 等待后续替换）
    - 否则组装三字段：描述体 = 场景。角色。主提示词（句号连接，各剥尾部标点），
      主提示词已以 [Shot 开头时不重复加 [Shot 1] 前缀
    - 环境音/配乐留空则整个字段省略（不写 N/A——官方 N/A = 明确请求无声，
      省略 = 不约束）
    - 主提示词未含 <d> 时自动把「……」转 <d>[中文] …</d>
    返回 (组装文本, 对白转换次数)。
    """
    prompt = prompt or ""
    if _OFFICIAL_FIELD_RE.search(prompt):
        return prompt, 0
    wrapped = prompt
    dialogues = 0
    if "<d>" not in prompt:
        wrapped = _wrap_dialogue(prompt)
        dialogues = len(re.findall(r"<d>", wrapped))
    parts = [str(p).strip().rstrip("。.；;，, ") for p in (scene, char, wrapped) if str(p).strip()]
    # 主提示词已带 [Shot N] 开头标签：剥掉防重复，统一在描述行首放一个前缀
    shot_tag = re.match(r"\s*(\[Shot\s*\d+\])\s*", str(wrapped))
    if shot_tag:
        parts = [str(p).strip().rstrip("。.；;，, ") for p in (scene, char) if str(p).strip()]
        parts.append(wrapped[shot_tag.end():].strip().rstrip("。.；;，, "))
        parts = [p for p in parts if p]
    body = "。".join(parts)
    lines = []
    if body:
        prefix = f"{shot_tag.group(1)} " if shot_tag else "[Shot 1] "
        lines.append(f"integrated_multimodal_description: {prefix}{body}")
    if str(soundscape or "").strip():
        lines.append(f"overall_soundscape: {str(soundscape).strip()}")
    if str(music or "").strip():
        lines.append(f"non_diegetic_music: {str(music).strip()}")
    return "\n".join(lines), dialogues


class H3SeamlessChainSampler(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3SeamlessChainSampler",
            display_name="H3 Seamless Chain (段间引导续拍)",
            category="MiniMaxH3",
            description="MiniMax H3 多段视频段间引导：每段提示词单独输入，上段尾帧 latent 直切钉入下段头部，"
                        "无缝续拍后裁剪拼接，输出完整视频+音频（可另出每段分镜）。"
                        "参考素材用法与官方 Reference to Video 一致：<Picture i> / <Video k> / <Audio j>。",
            inputs=[
                io.Model.Input("模型"),
                io.Clip.Input("文本编码器"),
                io.Vae.Input("视频VAE"),
                io.Vae.Input("音频VAE"),
                io.Combo.Input("宽高比", options=["自定义", "21:9", "16:9", "9:16", "4:3", "3:4", "1:1"], default="16:9",
                               tooltip="官方 Resolution Selector 同款：与「百万像素」共同换算画布（1MP=1024×1024，32 倍数对齐）。"
                                       "选「自定义」时直接用下方宽度/高度"),
                io.Float.Input("百万像素", default=0.5, min=0.1, max=2.0, step=0.1,
                               tooltip="目标总像素（MP），0.1–2.0 步进 0.1，箭头微调（官方 Resolution Selector 同款口径）："
                                       "0.2 草稿（608×352）/ 0.5 快速预览（960×544）/ 0.98 H3 官方原生（1344×768）/ "
                                       "1.0（1376×768）/ 2.0 超采样（1920×1088）"),
                io.Int.Input("宽度", default=864, min=32, max=16384, step=32, advanced=True,
                             tooltip="「宽高比=自定义」时直接生效；其余模式由 宽高比+百万像素 换算覆盖"),
                io.Int.Input("高度", default=480, min=32, max=16384, step=32, advanced=True,
                             tooltip="「宽高比=自定义」时直接生效；其余模式由 宽高比+百万像素 换算覆盖"),
                io.Float.Input("每段时长", default=5.0, min=0.5, max=15.0, step=0.1,
                               tooltip="每段可见时长（秒）@24fps，内部自动吸附 H3 的 17k+5 帧网格："
                                       "5.0s→124帧、6.0s→141帧。全链默认值，导演台每段可单独覆盖"),
                io.Combo.Input("引导帧数", options=["5", "22", "39", "56"], default="22",
                               tooltip="段间引导重叠桥：钉入下段头部的上段尾帧数。越大衔接越顺、越慢越吃显存"),
                io.Int.Input("种子", default=0, min=0, max=0xffffffffffffffff, control_after_generate=True,
                             tooltip="第 i 段实际使用 种子+i"),
                io.Int.Input("步数", default=25, min=1, max=100),
                io.Float.Input("CFG", default=1.0, min=0.0, max=100.0, step=0.1),
                io.Combo.Input("采样器", options=comfy.samplers.KSampler.SAMPLERS, default="res_multistep"),
                io.Combo.Input("调度器", options=comfy.samplers.KSampler.SCHEDULERS, default="simple"),
                io.Combo.Input("自动存档", options=["关闭", "自动存档"], default="关闭", advanced=True,
                               tooltip="已并入「自动保存」的「分段」档（功能等价）。本开关仅兼容旧工作流："
                                       "旧「自动存档=自动存档」自动映射为「自动保存=分段」且不成片。"
                                       "新工作流请直接使用「自动保存」"),
                io.String.Input("存档目录", default="",
                                tooltip="项目名：output/h3_projects/<项目名>/ 一个项目一个文件夹（视频/提示词/成片/latent 全在内）。"
                                        "空=按参数指纹自动命名；填了名字即固定项目：中断重跑、改词重跑都续在这个文件夹"),
                io.Combo.Input("桥帧门控", options=["关闭", "标注", "自动回退"], default="标注",
                               tooltip="对将成为重叠桥的尾帧打分（Laplacian清晰度+曝光）：标注=只写报告；自动回退=尾帧低于阈值时向前回退17/34帧取好帧续拍（该段可见帧数随之减少）"),
                io.Float.Input("清晰度阈值", default=30.0, min=0.0, max=100.0, step=0.5,
                               tooltip="桥帧总分阈值，低于判定为坏尾。建议先跑「标注」档看报告里的分数分布再定"),
                io.Int.Input("回退上限", default=34, min=0, max=68, step=17,
                             tooltip="自动回退最多向前多少帧（17 的倍数，踩 17k+5 网格）"),
                io.Float.Input("锚定加噪", default=0.0, min=0.0, max=0.5, step=0.05,
                               tooltip="下阶段基建（默认关闭——实测对画质是净伤害，保留为技术储备）。"
                                       "对桥锚定帧注入噪声的比例（SkyReels-V2 addnoise_condition 思路）："
                                       "干净锚定帧会让模型起步「刹车」并在可见部分重演锚定内容；加噪让模型"
                                       "把锚定当「参考」而非「必须逐帧复现」。0=关闭（默认）；"
                                       "0.2 标准（SkyReels 同值）；0.3+ 干预强但画面细节会变软。"
                                       "仅影响带引导桥的段；不进存档指纹，改参数不触发重跑"),
                io.Combo.Input("审片模式", options=["关闭", "逐段确认"], default="关闭",
                               tooltip="逐段确认：每次运行只生成一个新的段落即返回，预览「分段图像」或项目文件夹里的"
                                       "分段视频后重新运行继续下一段；不满意可改该段提示词（自动从该段重跑）或设「重跑起始段」重摇。"
                                       "开启后存档自动启用"),
                io.Combo.Input("自动保存", options=["关闭", "分段"], default="分段",
                               tooltip="开启后无需任何下游接线：每段生成完自动落盘 latent 存档与分段 mp4，"
                                       "全部落在项目文件夹 output/h3_projects/<项目名>/ 内，报告注明路径。"
                                       "「分段」=落存档+分段视频（等价旧「自动存档」，可续跑）；"
                                       "是否拼完整成片由「自动成片」开关独立控制。开启后存档自动启用"),
                io.Int.Input("重跑起始段", default=0, min=0, max=63,
                             tooltip="0=自动（沿用存档进度，改过提示词的段自动重做）；N=从第 N 段起丢弃存档重新生成"
                                     "（有序章时序章为第 1 段），配合改「种子」即可重摇该段及之后。用完记得改回 0"),
                io.Combo.Input("接缝重摇", options=["关闭", "自动"], default="自动",
                               tooltip="自动：本段生成后若接缝帧差 > 重摇阈值，换种子重采本段（最多「重摇上限」次），"
                                       "排除抽卡坏段（同参数下缝差 0.02-0.17 波动大，重摇取达标结果）；"
                                       "回放段（存档载入）不参与重摇。坏段触发时每次重摇=一次完整段采样时长"),
                io.Float.Input("重摇阈值", default=0.06, min=0.02, max=0.3, step=0.01,
                               tooltip="接缝帧差超过此值触发自动重摇（实测好缝约 0.02-0.03，坏缝 0.08+）。"
                                       "调低更严格但更耗时"),
                io.Int.Input("重摇上限", default=1, min=0, max=3,
                             tooltip="自动重摇的额外尝试次数（0=等于关闭重摇）"),
                io.Combo.Input("递减锚定", options=["关闭", "0.3", "0.5", "0.7"], default="关闭",
                               tooltip="下阶段基建（默认关闭——实测对画质是净伤害，保留为技术储备）。"
                                       "锚定约束随采样进度递减：开始强（接缝吻合）→ 逐渐减弱（模型自然过渡）"
                                       "→ 完全消失（自由生成）。数值=递减占总步数比例（0.3=前30%步数递减完毕）。"
                                       "开启后「锚定加噪」的值作为递减起点，终点为 0（锚定消失）"),
                io.Combo.Input("生成模式", options=["文生视频", "首帧视频", "多参视频"], default="文生视频",
                               tooltip="导演台一体化控制用：文生=纯文本（fl2va UNET，不接任何图片）；"
                                       "首帧=首帧图片起手（fl2va UNET，仅接「首帧图片」）；"
                                       "多参=参考图片（ref2va UNET，仅接「参考图片」组）。"
                                       "UI 据此互斥首帧/参考图；后端校验模式与素材一致性，不匹配时报错。"
                                       "实际 UNET 由「模型」输入决定，本控件只做模式声明与素材互斥。"),
                # 注意：新控件一律加在「生成模式」之后（widgets_values 末尾），
                # 旧工作流值不足时按默认值补齐，绝不插入中间破坏既有顺序（见 example_workflows）
                io.Combo.Input("自动成片", options=["关闭", "开启"], default="开启", advanced=True,
                               tooltip="独立控制完整成片编码：开启=无论一采/二采生成完毕都自动拼成片"
                                       "（二采开且高清记录齐时流式拼接高清分段，否则编码基础分辨率成片），"
                                       "直接落在项目文件夹；关闭=只落分段视频、不编码成片（需手动「合并导出」）。"
                                       "需开启自动保存/自动存档/审片之一建立项目存档才有挂载点"),
                io.String.Input("导演台状态", multiline=True, default="", socketless=True, advanced=True,
                               tooltip="导演台前端写入的 JSON 状态（模式/提示词/素材文件名/分段处理）。"
                                       "有值时优先于画布接线：提示词从 JSON 读取，图片从 input 目录加载。"
                                       "first_frame=首帧图片、end_frame=尾帧图片（FL2VA 剧情终点，仅首帧模式）、"
                                       "last_frame=每段尾帧锚定（身份锚点，任意模式可用）；"
                                       "ref_assets 为标签素材池 [{file,label}]；segments 数组为每段提供 "
                                       "scene_prompt / character_prompt / soundscape（环境音）/"
                                       "music（配乐，均留空省略）/ seconds（本段秒数）/"
                                       "refs（本段引用的素材标签，缺省=全部）/ frame_refs（段级首尾帧图引用："
                                       "首帧图/尾帧图，缺省=首段参考首帧、末段参考尾帧；中段勾首帧图=头部身份锚，"
                                       "任意段勾尾帧图=该段尾锚），提示词用 [[标签]] 引用素材，"
                                       "后端按官方 h3-prompt-writing 三字段结构组装（含对白「」→<d> 自动转换）。"
                                       "空值或旧工作流走原有画布输入（同样获得官方结构组装，向后兼容）。"
                                       "只存相对输入文件名，不存媒体内容、密钥或绝对路径。"),
                io.Combo.Input("一采编码", options=["标准", "高清", "极致"], default="标准",
                               tooltip="基础链（≈一采）分段视频与成片的 mp4 编码质量——二采关闭时直接决定正片清晰度："
                                       "标准=crf20 veryfast（现状兼容）；高清=crf16 medium + 暗部自适应量化 + Bayer 抖动；"
                                       "极致=crf13 slow + 同上（编码明显变慢）。二采开启时其高清产物同名覆盖，此档自动失效"),
                io.Image.Input("首帧图片", optional=True,
                               tooltip="第一段的起始帧（i2v）。用了它请用 fl2va UNET，且不能同时用任何参考素材"),
                io.Image.Input("尾帧图片", optional=True,
                               tooltip="FL2VA 官方首尾帧：整链最后一段的末帧 keyframe（剧情终点画面）。"
                                       "仅「首帧视频」模式；末段提示词写「如何走到这个画面」的过程，"
                                       "不复述图内静态内容；设了它，末段不再叠加每段尾帧锚定"),
                io.Image.Input("每段尾帧锚定", optional=True,
                               tooltip="身份锚定帧（last_frame keyframe）：注入每段末尾位置作为人物/场景参考，"
                                       "与段首引导桥形成「隧道」——模型去噪全程被首尾双锚点约束，过了桥窗口"
                                       "（约2秒）也不会漂移。建议用角色正面清晰帧（导演台素材池可上传，"
                                       "任意模式可用）。不传则不锚定尾帧（保持现状）"),
                io.Image.Input("起始视频", optional=True,
                               tooltip="序章：上传视频（≥5 帧、24fps，超长只取前「每段时长」内）编码为第 1 段存入存档，"
                                       "成片以它开头，生成段从其结尾续拍；经一次 VAE 重编码，不能与首帧图片同用"),
                io.Audio.Input("起始视频音轨", optional=True,
                               tooltip="序章原声（与起始视频配对，建议同源 LoadVideo 拆出；不接则序章按静音处理）"),
                io.Autogrow.Input("提示词组", optional=True,
                                  template=io.Autogrow.TemplatePrefix(
                                      input=io.String.Input("提示词", multiline=True,
                                                            placeholder="这一段的画面描述，第 N 段会顺着第 N-1 段结尾继续"),
                                      prefix="提示词_", min=1, max=64)),
                io.Autogrow.Input("参考图片组", optional=True,
                                  template=io.Autogrow.TemplatePrefix(
                                      input=io.Image.Input("参考图片",
                                                           tooltip="参考主体图，提示词用 <Picture i> 引用（i 从 1 开始按序号）。用了请接 ref2va UNET"),
                                      prefix="参考图片_", min=0, max=9)),
                io.Autogrow.Input("参考视频组", optional=True,
                                  template=io.Autogrow.TemplatePrefix(
                                      input=io.Image.Input("参考视频",
                                                           tooltip="参考视频帧（24fps，2-15 秒），提示词用 <Video k> 引用"),
                                      prefix="参考视频_", min=0, max=3)),
                io.Autogrow.Input("参考视频音轨组", optional=True,
                                  template=io.Autogrow.TemplatePrefix(
                                      input=io.Audio.Input("参考视频音轨",
                                                           tooltip="同号参考视频的原声（参考视频音轨_1 配 参考视频_1），自动获得 <Audio j> 标签"),
                                      prefix="参考视频音轨_", min=0, max=3)),
                io.Autogrow.Input("参考音频组", optional=True,
                                  template=io.Autogrow.TemplatePrefix(
                                      input=io.Audio.Input("参考音频",
                                                           tooltip="独立参考音频（配乐/音效），提示词用 <Audio j> 引用"),
                                      prefix="参考音频_", min=0, max=3)),
            ],
            outputs=[
                io.Image.Output("图像"),
                io.Audio.Output("音频"),
                io.Int.Output("帧率"),
                io.String.Output("报告"),
                io.Image.Output("分段图像", is_output_list=True,
                                tooltip="每段裁剪后的可见帧（逐段展开，接 Create Video 可单独保存）"),
                io.Audio.Output("分段音频", is_output_list=True,
                                tooltip="与分段图像逐段配对的音轨"),
            ],
        )

    @classmethod
    def execute(cls, 模型, 文本编码器, 视频VAE, 音频VAE, 宽高比, 百万像素, 宽度, 高度, 每段时长, 引导帧数,
                种子, 步数, CFG, 采样器, 调度器, 首帧图片=None, 尾帧图片=None, 每段尾帧锚定=None, 起始视频=None, 起始视频音轨=None,
                提示词组=None,
                参考图片组=None, 参考视频组=None, 参考视频音轨组=None, 参考音频组=None,
                自动存档="关闭", 存档目录="", 桥帧门控="标注", 清晰度阈值=30.0, 回退上限=34,
                锚定加噪=0.0,
                审片模式="关闭", 自动保存="分段", 自动成片="开启", 重跑起始段=0,
                接缝重摇="自动", 重摇阈值=0.06, 重摇上限=1,
                递减锚定="关闭", 生成模式="文生视频", 导演台状态="", 一采编码="标准"):
        # 运行期路由兜底：导入期注册因时序失败时，首次执行后前端删除/列表即可用
        try:
            from .routes import ensure_registered
            ensure_registered()
        except Exception:
            pass
        # ---- 导演台状态驱动：有 JSON 状态时优先于画布接线 ----
        ds = _parse_director_state(导演台状态)
        ds_used = bool(ds)
        # 实验性功能开关：从 ds.experiments 归一化；全关/FORCE_DISABLED => 空 context，
        # 后续所有实验分支以 exp.has(...) 包裹，关闭时逐字节走现状路径
        exp = experiments.resolve(ds if isinstance(ds, dict) else None)
        if ds.get("mode"):
            _m = str(ds["mode"])
            if _m not in ("文生视频", "首帧视频", "多参视频"):
                raise ValueError(f"导演台状态中的模式「{_m}」无效：必须是「文生视频」「首帧视频」或「多参视频」")
            生成模式 = _m
        # 提示词：导演台 JSON 优先，空则回退画布 autogrow 输入
        if ds.get("prompts") and isinstance(ds["prompts"], list):
            seg_prompts = [str(p).strip() for p in ds["prompts"] if str(p).strip()]
        else:
            prompts = _autogrow_items(提示词组, "p")
            seg_prompts = [str(v).strip() for v in prompts.values() if str(v).strip()]
        if not seg_prompts:
            raise ValueError("提示词不能为空：请在导演台填写提示词，或在「提示词组」里每段添加一个输入框并填写内容")

        # 素材池（标签->文件，分 图/视/音 三类）：ref_assets 为 v2 真源；旧 ref_images 视为图片
        pool_files = []   # [(kind, label, file)]
        _kind_n = {"image": 0, "video": 0, "audio": 0}
        if isinstance(ds.get("ref_assets"), list) and ds["ref_assets"]:
            for item in ds["ref_assets"]:
                if not (isinstance(item, dict) and item.get("file")):
                    continue
                _k = str(item.get("kind") or "image")
                if _k not in _KIND_NAME:
                    _k = "image"
                label = str(item.get("label") or "").strip()
                if not label:
                    # 缺省标签只对未命名素材按类别计数（与前端 getDs 一致）
                    _kind_n[_k] += 1
                    label = f"{_KIND_NAME[_k]}{_kind_n[_k]}"
                pool_files.append((_k, label[:24], str(item["file"])))
        elif ds.get("ref_images") and isinstance(ds["ref_images"], list):
            pool_files = [("image", f"图片{i + 1}", str(fn)) for i, fn in enumerate(ds["ref_images"]) if fn]
        _seen = set()
        for _i, (_k, _lbl, _fn) in enumerate(pool_files):
            _base, _n = _lbl, 2
            while _lbl in _seen:
                _lbl = f"{_base}{_n}"
                _n += 1
            _seen.add(_lbl)
            pool_files[_i] = (_k, _lbl, _fn)
        pool_labels = [lbl for _, lbl, _ in pool_files]
        pool_kind = {lbl: k for k, lbl, _ in pool_files}
        pool_file_of = {lbl: fn for _, lbl, fn in pool_files}

        segments = ds.get("segments") if isinstance(ds.get("segments"), list) else []

        # 分段处理中心：官方三字段组装（场景/角色/环境音/配乐）；[[标签]] -> <Picture>/<Video>/<Audio>；
        # 段首自动 subject_definitions。画布模式（无 JSON 状态）同样获得官方结构。
        seg_composed = 0
        seg_custom_refs = 0
        seg_dialogues = 0
        seg_label_orders = [[] for _ in seg_prompts]   # 每段 [(kind, 标签)]，按勾选顺序
        composed_prompts = []
        for i, prompt in enumerate(seg_prompts):
            seg = segments[i] if i < len(segments) and isinstance(segments[i], dict) else {}
            scene = str(seg.get("scene_prompt", "")).strip()
            char = str(seg.get("character_prompt", "")).strip()
            soundscape = str(seg.get("soundscape", "")).strip()   # 旧 JSON 无此键 = 空
            music = str(seg.get("music", "")).strip()
            full, dlg = _compose_official(scene, char, prompt, soundscape, music)
            seg_dialogues += dlg
            if scene or char or soundscape or music:
                seg_composed += 1
            if pool_files:
                refs_sel = seg.get("refs")
                order = []
                if isinstance(refs_sel, list) and refs_sel:
                    for r in refs_sel:
                        lbl = str(r).strip()
                        if lbl not in pool_labels:
                            raise ValueError(f"段{i + 1} 引用了未知素材标签「{lbl}」：可用标签 {pool_labels}")
                        item = (pool_kind[lbl], lbl)
                        if item not in order:
                            order.append(item)
                    seg_custom_refs += 1
                    # 提示词里 [[标签]] 提到但没勾选的素材：按出现顺序并入，防勾选/文本失配报错
                    for tok in _LABEL_TOKEN.findall(full):
                        lbl = tok.strip()
                        if lbl in pool_labels and (pool_kind[lbl], lbl) not in order:
                            order.append((pool_kind[lbl], lbl))
                else:
                    # refs 缺省/显式空 = 引用全部素材（与旧行为一致，避免 ref2va 链空参考段）
                    order = [(k, lbl) for k, lbl, _ in pool_files]
                # 官方单段上限：图 9 / 视 3 / 音 3（超了报错并指出勾选明细）
                for _k, _cap in REF_CAPS.items():
                    _picked = [lbl for k, lbl in order if k == _k]
                    if len(_picked) > _cap:
                        raise ValueError(f"段{i + 1} 引用{_KIND_NAME[_k]}素材 {len(_picked)} 个，"
                                         f"超过官方单段上限 {_cap} 个（{_picked}）：请在段卡片少勾几个")
                seg_label_orders[i] = order
                if order:
                    has_pic = "<Picture" in full or "<Video" in full or "<Audio" in full
                    full = _apply_label_tokens(full, order)
                    if not has_pic:
                        full = _reference_header(order) + "\n" + full
            composed_prompts.append(full)
        seg_prompts = composed_prompts

        # 每段时长原始值：seg.seconds（None=跟随全局默认），吸附网格在 length 确定后统一做
        seg_secs = []
        for i in range(len(seg_prompts)):
            seg = segments[i] if i < len(segments) and isinstance(segments[i], dict) else {}
            try:
                v = seg.get("seconds")
                seg_secs.append(None if v in (None, "") else _snap_seconds(float(v)))
            except (TypeError, ValueError):
                seg_secs.append(None)

        # 段级独立镜头（断链）：与上段毫无关联的镜头一键全断——不接上段桥、不裁头、
        # 独立镜头（硬切转场）：不注入上段尾帧引导桥、不做桥帧门控/接缝测量/响度对齐
        seg_unlink = []
        for i in range(len(seg_prompts)):
            seg = segments[i] if i < len(segments) and isinstance(segments[i], dict) else {}
            seg_unlink.append(bool(seg.get("unlink")))

        # 段级禁用（不上链）：槽位稳定方案——禁用段保留在执行序列与哈希序列中
        # （禁用/启用不触发任何重做），仅执行时跳过：不采样/不解码/不进成片；
        # 下段锚定跨接到最近一个已执行段的尾帧。插入段不可禁用（有专用移除操作）。
        seg_disabled = []
        for i in range(len(seg_prompts)):
            seg = segments[i] if i < len(segments) and isinstance(segments[i], dict) else {}
            seg_disabled.append(bool(seg.get("disabled")))

        # 潜空间放大二采（导演台面板控制）：每段采样定稿后立即「神经放大 latent →
        # 低强度重采样 → 解码」，分段视频与成片直接保存二采结果（同名覆盖，不再
        # 产出 upseg_* 副本）。模型在主循环前预加载：缺失/不匹配当场降级为基础
        # 分辨率整链运行（报告注明），不让每段反复撞同一个错。关闭时 up_cfg=None
        from . import upscale
        up_cfg = upscale.parse_state(ds)
        up_net = None
        _up_err = None
        if up_cfg:
            try:
                up_net = upscale.load_net(up_cfg)
            except ValueError as e:
                _up_err = str(e)
                up_cfg = None

        # 一采编码档位：基础链分段/成片的编码质量（二采开启时其高清产物同名覆盖，本档自然失效）
        _benc = str(一采编码 or "").strip()
        if _benc not in upscale.ENCODE_PROFILES:
            _benc = "标准"
        _bcrf, _bpreset, _baq, _bdith = upscale._ENCODE_SETTINGS.get(
            _benc, upscale._ENCODE_SETTINGS["标准"])

        # 插入视频段（导演台状态 inserts）：按链位混排进执行序列——画面+原声进成片，
        # 尾帧 latent 桥接指导下一段生成（序章机制的任意段间推广）。
        # 链位 pos = 提示词段+插入段混排后的 1-based 位置（不含序章），与前端 planFromDs 一致
        insert_specs = []
        _raw_inserts = ds.get("inserts") if isinstance(ds.get("inserts"), list) else []
        for it in _raw_inserts:
            if not (isinstance(it, dict) and str(it.get("file") or "").strip()):
                continue
            try:
                pos = int(it.get("pos"))
            except (TypeError, ValueError):
                raise ValueError(f"插入视频位置无效：{it.get('pos')!r}（须为正整数链位）")
            if not 1 <= pos <= len(seg_prompts) + len(_raw_inserts):
                raise ValueError(f"插入视频链位 {pos} 越界：有效范围 1-{len(seg_prompts) + len(_raw_inserts)}")
            insert_specs.append((pos, str(it["file"]).strip()))
        _seen_pos = set()
        for pos, _f in insert_specs:
            if pos in _seen_pos:
                raise ValueError(f"插入视频链位 {pos} 重复：每个链位只能插入一个视频")
            _seen_pos.add(pos)
        insert_specs.sort(key=lambda x: x[0])
        exec_items = []   # ("prompt", 提示词段索引) | ("insert", 文件名)，链位序
        _pi = 0
        for pos, fname in insert_specs:
            while len(exec_items) + 1 < pos and _pi < len(seg_prompts):
                exec_items.append(("prompt", _pi))
                _pi += 1
            exec_items.append(("insert", fname))
        while _pi < len(seg_prompts):
            exec_items.append(("prompt", _pi))
            _pi += 1

        # 首帧图片：导演台 JSON 优先，空则用画布连接
        if ds.get("first_frame"):
            首帧图片 = _load_input_image(ds["first_frame"])

        # 尾帧图片（FL2VA 剧情终点，仅首帧模式）：导演台 JSON 优先，空则用画布连接
        if ds.get("end_frame"):
            尾帧图片 = _load_input_image(ds["end_frame"])

        # 每段尾帧锚定（身份锚点，任意模式可用）：导演台 JSON 优先，空则用画布连接
        if ds.get("last_frame"):
            每段尾帧锚定 = _load_input_image(ds["last_frame"])

        # 段级首尾帧图引用（类似多参段级素材勾选）：决定每段是否参考首帧/尾帧图片。
        # 缺省=旧行为（首段 i2v 参考首帧图、末段参考尾帧图终点锚）；显式勾选可为
        # 中段注入首帧图头部身份锚 / 任意段注入尾帧图尾部锚，也可关闭首末段默认参考
        seg_frame_refs, seg_fr_explicit = _parse_frame_refs(
            segments, len(seg_prompts), 首帧图片 is not None, 尾帧图片 is not None)
        seg_first_on = ["首帧图" in fr for fr in seg_frame_refs]
        seg_end_on = ["尾帧图" in fr for fr in seg_frame_refs]

        # i2v 首段官方指令行（官方 I2VA 固定格式：声明首帧 = <Picture 1> 锚）；
        # 已是官方格式的段不注入（用户自管的完整结构里可能自带指令行）
        if 首帧图片 is not None and seg_first_on[0] and seg_prompts \
                and not _OFFICIAL_FIELD_RE.search(seg_prompts[0]):
            seg_prompts[0] = ("For the target video, at 0.00 seconds into the target video, "
                              "<Picture 1> (from [Shot 1]) is fully referenced.\n" + seg_prompts[0])

        # 素材池张量（按类别）：JSON 池各类别独立加载；图片池空时回退画布 autogrow（全段共用）
        pool_tensors = {"image": {}, "video": {}, "audio": {}}
        for _k, _lbl, _fn in pool_files:
            if _k == "image":
                pool_tensors["image"][_lbl] = _load_input_image(_fn)
            elif _k == "video":
                pool_tensors["video"][_lbl] = _load_input_video(_fn)   # (帧张量, 同源音轨)
            else:
                pool_tensors["audio"][_lbl] = _load_input_audio(_fn)
        refs = {
            "ref_videos": _autogrow_items(参考视频组, "ref_video_"),
            "ref_video_audios": _autogrow_items(参考视频音轨组, "ref_video_audio_"),
            "ref_audios": _autogrow_items(参考音频组, "ref_audio_"),
        }
        if not pool_tensors["image"]:
            _canvas_imgs = _autogrow_items(参考图片组, "ref_image_")
            pool_tensors["image"] = {f"图片{j + 1}": v for j, v in enumerate(_canvas_imgs.values())}
            if pool_tensors["image"]:
                pool_labels = list(pool_tensors["image"].keys())
                seg_label_orders = [[("image", l) for l in pool_labels] for _ in seg_prompts]
        # 状态池某类非空时该类覆盖画布接线（视频/音频与图片同规则），另一类画布接线仍生效
        if pool_tensors["video"]:
            refs["ref_videos"], refs["ref_video_audios"] = {}, {}
        if pool_tensors["audio"]:
            refs["ref_audios"] = {}
        has_refs = any(pool_tensors.values()) or any(refs.values())
        if 首帧图片 is not None and has_refs:
            raise ValueError("首帧图片（i2v，fl2va UNET）与参考素材（r2v，ref2va UNET）不能同时使用")
        if 起始视频 is not None and 首帧图片 is not None:
            raise ValueError("起始视频（序章）与首帧图片（i2v）不能同时使用：两者都定义第 1 段的视觉起点")
        # 生成模式一致性校验：导演台据此互斥首帧/参考图，后端复验，不匹配直接报错（避免跑废）
        _mode = str(生成模式)
        if _mode == "文生视频" and (首帧图片 is not None or has_refs):
            raise ValueError("「文生视频」模式下不能接首帧图片或参考素材：请在导演台切到首帧/多参模式后再接图")
        if _mode == "首帧视频" and has_refs:
            raise ValueError("「首帧视频」模式下不能接参考素材（首帧与多参互斥）：请切到「多参视频」模式用参考图")
        if _mode == "多参视频" and 首帧图片 is not None:
            raise ValueError("「多参视频」模式下不能接首帧图片（多参与首帧互斥）：请切到「首帧视频」模式用首帧")
        if 尾帧图片 is not None and _mode != "首帧视频":
            raise ValueError("尾帧图片（FL2VA 官方首尾帧）只在「首帧视频」模式可用：请在导演台切到首帧模式")

        if str(宽高比) != "自定义":
            宽度, 高度 = _resolve_canvas(宽高比, 百万像素)
        width, height, seed = int(宽度), int(高度), int(种子)
        length = _snap_seconds(每段时长)
        seg_lengths = [length if s is None else s for s in seg_secs]
        # FL2VA 末段官方指令行（镜像 I2VA 首段行的官方句式：声明末帧 = <Picture k> 锚，
        # 时间点=末段时长；首尾都接时尾帧是 <Picture 2>，只接尾帧时为 <Picture 1>）；
        # 已是官方格式的段不注入。段级勾选关掉末段尾帧图时不注入；
        # 首段关掉首帧图时 <Picture> 编号前移
        if 尾帧图片 is not None and seg_end_on[-1] and seg_prompts \
                and not _OFFICIAL_FIELD_RE.search(seg_prompts[-1]):
            _pic = 2 if (首帧图片 is not None and seg_first_on[0]) else 1
            _end_s = seg_lengths[-1] / 24.0
            seg_prompts[-1] = (f"For the target video, at {_end_s:.2f} seconds into the target video, "
                               f"<Picture {_pic}> (from [Shot 1]) is fully referenced.\n" + seg_prompts[-1])
        ctx = int(引导帧数)
        gate_limit = max(0, int(回退上限) // 17 * 17)
        from . import qc  # 桥帧打分 + 接缝测量共用（延迟导入：无 ComfyUI 环境下结构单测不触达）
        clip, video_vae, audio_vae = 文本编码器, 视频VAE, 音频VAE
        if has_refs:
            chain = "r2v（ref2va UNET）"
        elif 首帧图片 is not None and 尾帧图片 is not None:
            chain = "FL2VA 首尾帧（首帧开场 → 尾帧终点，fl2va UNET）"
        elif 尾帧图片 is not None:
            chain = "L2VA 尾帧终点（fl2va UNET）"
        elif 首帧图片 is not None:
            chain = "i2v 首段 + t2v 续段（fl2va UNET）"
        else:
            chain = "t2v（fl2va UNET）"

        # cond 文本编码缓存代理：一采/二采/重摇/E4 全链共用同一实例——TE 的
        # tokenize+encode 只依赖提示词文本，同一段提示词（含二采高清条件重建、
        # negative 空串）只前向一次；参考图/视频的 VAE 编码与画布其他节点不受影响
        from .cond_cache import CachedClipProxy
        clip = CachedClipProxy(clip)
        negative = clip.encode_from_tokens_scheduled(clip.tokenize(""))

        report = [f"H3 Seamless Chain：{len(seg_prompts)} 段，链路 {chain}，模式 {_mode}，上下文 {ctx} 帧"]
        if experiments.FORCE_DISABLED:
            report.append("实验性功能：后端已强制关闭（H3_EXPERIMENTS=0）")
        elif exp.enabled:
            report.append(exp.describe())
        # 软桥能力探测：只在开关打开时探测并报告（全关时报告与旧版逐行一致）。
        # 等级 0 = 本机 ComfyUI 不支持逐 token 掩码 -> 整链自动回退现状路径。
        _sb_level, _sb_reason = 0, "未启用"
        if exp.has("soft_bridge"):
            _sb_level, _sb_reason = bridge.probe_soft_bridge()
            report.append("软桥：" + ("开" if _sb_level else "关") + " · "
                          + bridge.level_text(_sb_level, _sb_reason))
            if _sb_level == bridge.LEVEL_OFF:
                report.append("软桥不可用，本次按现状「钉桥 + 裁头」生成（行为与关闭开关时一致）")
        if insert_specs:
            _ins_txt = "、".join(f"链位{p}={f}" for p, f in insert_specs)
            report.append(f"插入视频：{len(insert_specs)} 段（{_ins_txt}）——画面+原声进成片，尾帧桥指导下一段")
        _unlink_pos = [str(item_i + 1) for item_i, it in enumerate(exec_items)
                       if it[0] == "prompt" and seg_unlink[it[1]]]
        if _unlink_pos:
            report.append(f"独立镜头：{len(_unlink_pos)} 段与上段断链（无桥接/硬切，段 {'、'.join(_unlink_pos)}）")
        _off_pos = [str(item_i + 1) for item_i, it in enumerate(exec_items)
                    if it[0] == "prompt" and seg_disabled[it[1]]]
        if _off_pos:
            report.append(f"段禁用：{len(_off_pos)} 段不上链（跳过执行不进成片，段 {'、'.join(_off_pos)}；"
                          "已禁用段与启用段可混排，随时恢复零成本）")
        if up_cfg:
            _tb = float(up_cfg.get("time_bias") or 0.0)
            _mix = float(up_cfg.get("mix") or 0.0)
            _sh = float(up_cfg.get("shift") or 0.0)
            _stg = float(up_cfg.get("stg") or 0.0)
            _stgb = int(up_cfg.get("stg_block") if up_cfg.get("stg_block") is not None else 25)
            _passes = int(up_cfg.get("passes") or 1)
            _sig_list = upscale.cascade_sigmas(float(up_cfg["denoise"]), _passes,
                                               float(up_cfg.get("decay") or 0.5))
            _shp = float(up_cfg.get("sharpen") or 0.0)
            _psp = float(up_cfg.get("pixel_sharpen") or 0.0)
            _enc = str(up_cfg.get("encode") or "标准")
            _sam = str(up_cfg.get("sampler") or "").strip()
            _sch = str(up_cfg.get("scheduler") or "").strip()
            report.append(f"潜空间放大二采：{up_cfg['mode']} · {up_cfg['arch']} {up_cfg['scale']:g}× · "
                          f"精化 {up_cfg['steps']} 步"
                          + (f"×{_passes}轮" if _passes > 1 else "")
                          + " @ σ≈" + "/".join(f"{s:g}" for s in _sig_list)
                          + (f" · 时间偏置 {_tb:g}" if _tb > 0 else "")
                          + (f" · 细节混合 {_mix:g}" if _mix > 0 else "")
                          + (" · 段自适应σ" if up_cfg.get("adaptive") is True else "")
                          + (f" · 二采shift {_sh:g}" if _sh > 0 else "")
                          + (f" · STG引导 {_stg:g}(块{_stgb})" if _stg > 0 else "")
                          + (f" · latent锐化 {_shp:g}" if _shp > 0 else "")
                          + (f" · 像素锐化 {_psp:g}" if _psp > 0 else "")
                          + (f" · {_enc}编码" if _enc != "标准" else "")
                          + (f" · 独立采样 {_sam}/{_sch}".rstrip("/")
                             if (_sam or _sch) else "")
                          + (" · 增益重试" if up_cfg.get("retry") is True else "")
                          + "——每段采样定稿后立即渲染高清，分段视频与成片直接保存二采结果")
        elif _up_err:
            report.append(f"潜空间放大二采：面板已开启但本次跳过——{_up_err}")
        if _benc != "标准":
            report.append(f"一采编码：{_benc}(crf{_bcrf}/{_bpreset})——二采未覆盖的段与基础成片按此档落盘")
        if str(宽高比) != "自定义":
            report.append(f"画布：{宽高比} · {float(百万像素):g}MP → {width}×{height}（官方换算，1MP=1024×1024，32 倍数对齐）")
        else:
            report.append(f"画布：自定义 {width}×{height}")
        _custom_len = [i for i in range(len(seg_lengths)) if seg_lengths[i] != length]
        if _custom_len:
            report.append("每段时长：" + " ".join(f"段{i + 1}={seg_lengths[i] / 24:.1f}s({seg_lengths[i]}帧)" for i in _custom_len)
                          + f"（默认 {length / 24:.1f}s={length}帧）")
        if ds_used:
            report.append("素材来源：导演台状态（JSON）")
        if seg_composed:
            report.append(f"分段处理：{seg_composed}/{len(seg_prompts)} 段含场景/角色/声音提示词（官方三字段组装）")
        if seg_dialogues:
            report.append(f"对白格式：{seg_dialogues} 处「」已转 <d>[中文]")
        if has_refs:
            _k_short = {"image": "图", "video": "视", "audio": "音"}
            _parts = []
            if pool_files:
                _stat = "、".join(f"{_k_short[k]}×{sum(1 for kk, _, _ in pool_files if kk == k)}"
                                  for k in ("image", "video", "audio")
                                  if any(kk == k for kk, _, _ in pool_files))
                _names = "、".join(f"{_k_short[k]}·{lbl}（{pool_file_of.get(lbl, '画布')}）"
                                   for k, lbl, _f in pool_files)
                _parts.append(f"池 {_stat}（{_names}）")
            counts = " ".join(f"{k}={len(v)}" for k, v in refs.items() if v)
            if counts:
                _parts.append(f"画布接线 {counts}")
            if seg_custom_refs:
                _parts.append(f"段级子集 {seg_custom_refs}/{len(seg_prompts)} 段自定义")
            report.append("参考素材：" + " | ".join(_parts))
        if not KEYFRAME_AUDIO_SUPPORTED:
            report.append("注意：当前 ComfyUI 不含 PR #15439（Add Guide 协议），段间引导降级为仅视频锚定，音频不锚定"
                          + ("；r2v 链上引导会与参考素材冲突失效，强烈建议升级 ComfyUI" if has_refs else ""))
        full_bridge = full_bridge_supported()
        if not full_bridge:
            report.append("注意：当前 ComfyUI 的 keyframe 协议仅支持单帧锚定，段间引导已自动降级为单帧桥，"
                          "接缝质量受限；升级 ComfyUI 后无需改参数即自动恢复完整引导帧数")
        # 衔接参数直接读下方控件：锚定加噪/递减锚定保留（下阶段基建）。
        # 潜空间精修 / smoothstep 像素混合 / 智能切镜已剔除，不再有 seam profile。
        aug = min(max(float(锚定加噪), 0.0), 0.5)
        if aug > 0.0:
            report.append(f"锚定加噪 {aug:.2f}：桥锚定帧按参考而非逐帧复现注入（视觉 {1.0 - aug:.2f} / 音频 {1.0 - aug * 0.5:.2f} 保真）")
            if aug > 0.25:
                report.append(f"注意：锚定加噪 {aug:.2f} 偏高（>0.25 锚定偏软，段首偏差方差增大，建议 0.15-0.20）")
        fade_ratio = 0.0 if 递减锚定 == "关闭" else float(递减锚定)
        if fade_ratio > 0:
            aug_start = 1.0 - aug if aug > 0 else 0.999
            report.append(f"递减锚定：前 {fade_ratio*100:.0f}% 步数内锚定 {aug_start:.2f} → 0 递减消失"
                          + (f"（起点=锚定加噪 {aug:.2f}）" if aug > 0 else "（硬锚定起点）"))

        tail_anchor_latent = None
        if 每段尾帧锚定 is not None:
            tail_anchor_latent = video_vae.encode(_center_cover(每段尾帧锚定[:1], width, height))
            report.append(f"每段尾帧锚定：身份锚点注入末帧 keyframe（{'视觉保真 ' + format(1.0 - aug, '.2f') if aug > 0 else '硬锚定'}）")

        # 尾帧图片（FL2VA 剧情终点）：编码后注入勾选了尾帧图的段末帧 keyframe；
        # 勾了尾帧图的段不再叠加每段尾帧锚定（同一位置只有一个锚，剧情终点优先）
        end_frame_latent = None
        if 尾帧图片 is not None:
            end_frame_latent = video_vae.encode(_center_cover(尾帧图片[:1], width, height))
            _end_segs = [str(i + 1) for i in range(len(seg_prompts)) if seg_end_on[i]]
            report.append(f"尾帧图片：FL2VA 剧情终点 → 段{'、'.join(_end_segs)} 末帧 keyframe"
                          + ("（这些段不叠加每段尾帧锚定）" if tail_anchor_latent is not None else ""))

        # 首帧图片（中段段级引用）：任一中段勾了首帧图时编码为头锚 latent——
        # 注入段头 keyframe 作为身份锚（同 E2 记忆锚段首注入模式），抑制长链漂移
        head_frame_latent = None
        if 首帧图片 is not None and any(seg_first_on[i] and i > 0
                                         for i in range(len(seg_prompts))):
            head_frame_latent = video_vae.encode(_center_cover(首帧图片[:1], width, height))

        if any(seg_fr_explicit):
            _fr_txt = []
            if 首帧图片 is not None and any(seg_first_on):
                _fr_txt.append("首帧图→段" + "、".join(
                    str(i + 1) for i in range(len(seg_prompts)) if seg_first_on[i])
                    + "（首段=i2v 起手，中段=头部身份锚）")
            if 尾帧图片 is not None and any(seg_end_on):
                _fr_txt.append("尾帧图→段" + "、".join(
                    str(i + 1) for i in range(len(seg_prompts)) if seg_end_on[i]))
            report.append("首尾帧图段级引用：" + " · ".join(_fr_txt)
                          + "；未勾段尾部锚回落到每段尾帧锚定")

        # 存档指纹只覆盖共享参数（不含提示词、不含种子）：改某段提示词仍指向同一条链，
        # 重跑起点由逐段提示词哈希比对定位；种子控件开着 control_after_generate 每次运行
        # 自动 +1，真正的种子序列由 manifest 权威记录（见下方续跑载入逻辑）
        # 兼容旧三态「自动保存=分段+成片」：成片已由「自动成片」独立接管，映射为「分段」
        if 自动保存 == "分段+成片":
            自动保存 = "分段"
        # 兼容旧「自动存档」：旧值映射为「自动保存=分段」且不成片（保持旧行为）
        if 自动存档 in ("自动存档", "自动续跑") and 自动保存 == "关闭":
            自动保存 = "分段"
            自动成片 = "关闭"
        resume = 自动存档 in ("自动存档", "自动续跑")  # 自动续跑=旧版工作流里的值，读档兼容
        review = 审片模式 == "逐段确认"
        autosave = 自动保存 == "分段"   # 落盘存档 + 分段 mp4（旧「分段+成片」已映射为「分段」）
        reroll = max(0, int(重跑起始段))
        use_ckpt = resume or review or autosave   # 审片须落盘续接；自动保存须段落盘 mp4
        autosave_final = use_ckpt and 自动成片 == "开启"   # 成片由「自动成片」独立控制
        if 自动成片 == "开启" and not use_ckpt:
            report.append("自动成片：需开启自动保存/自动存档/审片之一建立项目存档，本次跳过成片")
        if up_cfg and not use_ckpt:
            # 二采产物落项目文件夹（manifest 记录 + seg mp4 覆盖），无存档就没有挂载点
            report.append("潜空间放大二采：需开启自动保存（或审片/自动存档）建立项目存档，本次跳过")
            up_cfg = None
        if review and not (resume or autosave):
            report.append("审片模式：存档自动启用（每段落盘 latent，跨次运行续接）")
        if autosave and not (resume or review):
            report.append("自动保存：存档自动启用（分段视频每段落盘）")
        elif reroll > 0 and not use_ckpt:
            report.append("注意：「重跑起始段」仅在自动存档/审片模式下生效，本次已忽略")
        ckpt_params = {
            "width": width, "height": height,
            "length": length, "ctx": ctx, "steps": int(步数), "cfg": float(CFG),
            "sampler": 采样器, "scheduler": 调度器, "chain": chain,
            "fade_ratio": fade_ratio,
            "gate": {"mode": 桥帧门控, "threshold": float(清晰度阈值), "limit": gate_limit},
        }
        # 实验性功能：仅在开启时并入指纹——切换实验组合即判参数不一致触发整链重做，
        # 杜绝不同实验复用同一缓存污染结果；全关时不加该键，旧档续跑行为与现状一致。
        if exp.enabled:
            ckpt_params["experiments"] = exp.fingerprint()
        # 衔接诊断参数（下阶段基建：重摇/锚定）只记录不进指纹（改值不触发重跑；报告回看用）
        seam_refine = {"reroll": 接缝重摇, "reroll_th": float(重摇阈值),
                       "reroll_max": int(重摇上限), "anchor_aug": aug}
        # 逐段哈希：默认时长且未自定义段级引用的段哈希与旧公式一致（旧存档续跑不受影响）；
        # 改过时长/引用/断链开关的段把对应标记并入哈希 → 自动从该段重做（否则只改勾选不触发重做）
        _hash_refs = bool(pool_files)   # 仅状态池段级引用进哈希；画布回退不进（保旧档兼容）
        seg_hashes = []
        for i, p in enumerate(seg_prompts):
            tag = p
            if seg_lengths[i] != length:
                tag = f"{seg_lengths[i]}|{tag}"
            if _hash_refs and seg_label_orders[i]:
                tag = f"{','.join(lbl for _k, lbl in seg_label_orders[i])}|{tag}"
            if seg_unlink[i]:
                tag = f"unlink|{tag}"
            # 段级首尾帧图引用：显式设置且与默认值不同才进哈希（显式但等于默认=行为
            # 不变不重做；未设置段哈希不变，旧存档续跑零影响）
            if seg_fr_explicit[i] and seg_frame_refs[i] != _default_frame_refs(
                    i, len(seg_prompts), 首帧图片 is not None, 尾帧图片 is not None):
                tag = f"fr:{','.join(seg_frame_refs[i])}|{tag}"
            seg_hashes.append(checkpoint.prompt_hash(tag))

        # 插入段哈希 = 文件指纹（mtime+size）：换文件/同名覆盖上传 → 从该段起重跑
        def _insert_hash(fname):
            try:
                st = os.stat(folder_paths.get_annotated_filepath(fname))
                return checkpoint.prompt_hash(f"insert|{fname}|{st.st_mtime_ns}|{st.st_size}")
            except OSError:
                return checkpoint.prompt_hash(f"insert|{fname}|missing")

        exec_hashes = [seg_hashes[it[1]] if it[0] == "prompt" else _insert_hash(it[1])
                       for it in exec_items]
        prologue_hash = None
        if 起始视频 is not None:
            f0 = 起始视频[0].detach().float().cpu()
            prologue_hash = checkpoint.prompt_hash(
                f"prologue:{int(起始视频.shape[0])}:{float(f0.mean()):.4f}:{float(f0.std()):.4f}")
        root, manifest, done, seeds = None, None, 0, []
        proj_title, proj_created, proj_finals = "", None, []
        proj_inserts = []
        # 选择性重做（重摇标记）：{全局槽位: 锚定模式}，队列 = 未跑完的标记快照
        redo_map, redo_queue, _redo_started = {}, [], False
        off = 1 if 起始视频 is not None else 0
        if use_ckpt:
            root = checkpoint.ckpt_dir(ckpt_params, 存档目录.strip())
            manifest = checkpoint.load_manifest(root)
            # 项目元数据：title 首次=目录名、created_at 首次=本次、finals/inserts 沿用存档
            proj_title = os.path.basename(root)
            proj_created = time.time()
            if manifest is not None:
                proj_title = str(manifest.get("title") or proj_title)
                proj_created = manifest.get("created_at") or proj_created
                proj_finals = list(manifest.get("finals") or [])
                proj_inserts = [dict(x) for x in (manifest.get("inserts") or [])
                                if isinstance(x, dict)]
            if manifest is not None:
                if manifest.get("schema") != checkpoint.SCHEMA:
                    raise ValueError(f"存档目录格式不认识（{manifest.get('schema')}），请换一个目录名；"
                                     "旧版 v1 存档不兼容本版本，请清空旧目录或换新名字")
                checkpoint.assert_match(manifest["params"], ckpt_params)
                if 起始视频 is None and manifest.get("has_prologue"):
                    off = 1  # 输入已断开仍沿用存档序章（LoadVideo 可 bypass），哈希校验跳过
                elif 起始视频 is not None and not manifest.get("has_prologue"):
                    manifest = checkpoint.truncate(root, manifest, 0)
                    proj_inserts = []
                    report.append("存档续跑：检测到新接入的序章视频，整链重做")
                elif 起始视频 is not None and prologue_hash is not None:
                    stored = list(manifest.get("prompt_hashes", []))
                    if stored and stored[0] != prologue_hash:
                        manifest = checkpoint.truncate(root, manifest, 0)
                        proj_inserts = []
                        report.append("存档续跑：序章视频已更换，整链重做")
                done = checkpoint.contiguous_done(root, int(manifest.get("done", 0)))
                full_hashes = ([prologue_hash] if off else []) + exec_hashes
                hashes = list(manifest.get("prompt_hashes", []))
                # 选择性重做（重摇标记）：redo 优先于「重跑起始段」（互斥——redo=只重做
                # 标记段其余保留；reroll=从某段起全部级联重做，语义不同不叠加）
                exec_kinds = [it[0] for it in exec_items]
                _exec_disabled = [seg_disabled[it[1]] if it[0] == "prompt" else False
                                  for it in exec_items]
                _redo_ds = _parse_redo_segs(ds, done, exec_kinds, off, _exec_disabled)
                if _redo_ds and reroll > 0:
                    report.append("注意：已标记重摇段，「重跑起始段」本次忽略——"
                                  "重摇=只重做标记段（其余保留），与级联重做互斥")
                    reroll = 0
                # 「重跑起始段」为 1-based 段号（与 tooltip 一致）：N=从第 N 段起重做
                start = min(max(reroll - 1, 0), done) if reroll > 0 else min(
                    checkpoint.reroll_start(hashes, full_hashes, done), done)
                if start < done:
                    manifest = checkpoint.truncate(root, manifest, start)
                    done = start
                    # 截断后 done 变小：越界的重摇标记槽位作废（truncate 已联动清队列）
                    _redo_ds = [x for x in _redo_ds if x[0] < done]
                    proj_inserts = [x for x in proj_inserts
                                    if int(x.get("slot", -1)) < start]
                    原因 = "手动指定「重跑起始段」" if reroll > 0 else "检测到该段提示词已修改"
                    report.append(f"存档续跑：{原因}，从段 {start + 1} 起重新生成"
                                  + (f"（段 1-{start} 沿用存档）" if start else "（整链重做）"))
                seeds = [int(s) for s in manifest.get("seeds", [])]
                # 合并上次未跑完的重摇队列（审片逐段推进场景：ds 只带本次新标记，
                # 残留队列在 manifest；链结构变化时已随 truncate 清空，此处防御性复验）
                redo_map = dict(_redo_ds)
                for x in (manifest.get("redo_queue") or []):
                    try:
                        _s, _m = int(x[0]), str(x[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    # 防御性复验：槽位/类型/模式/禁用——段标记后被禁用时条目在此
                    # 滤掉（禁用分支先于队列消费，不滤会永久滞留每次运行都报却永不执行）
                    if _s not in redo_map and _m in _REDO_MODES \
                            and off <= _s < done and 0 <= _s - off < len(exec_kinds) \
                            and exec_kinds[_s - off] == "prompt" \
                            and not _exec_disabled[_s - off]:
                        redo_map[_s] = _m
                if redo_map:
                    redo_queue = [[s, redo_map[s]] for s in sorted(redo_map)]
                    _redo_started = True
                    report.append("选择性重做：" + "、".join(
                        f"段{s + 1}（{m}）" for s, m in redo_queue)
                        + "——只重做以上标记段，其余段沿用存档")
                report.append(f"存档续跑：载入已完成 {done}/{len(exec_items) + off} 段，目录 {root}")
                if reroll == 0 and seeds and not redo_map:
                    report.append("控件种子仅在「重跑起始段」> 0 时生效，当前沿用存档种子序列")
        full_hashes = ([prologue_hash] if off else []) + exec_hashes
        # 二采记录沿用：主循环的全量 manifest 覆写必须带上 upscale 键，
        # 否则每次段落盘都会把之前清扫写入的二采记录清掉（truncate 已联动截断）
        proj_upscale = (manifest.get("upscale") if isinstance(manifest, dict) else None) \
            if use_ckpt else None
        if review and autosave:
            report.append(f"审片：分段视频每段落盘 → output/h3_projects/{os.path.basename(root) or '…'}/seg_XXX.mp4，"
                          "成片为运行结束时的已确认部分")

        pbar = comfy.utils.ProgressBar(len(exec_items))
        total = len(exec_items) + off
        prompt_list = (["「序章（上传视频）」"] if off else []) + [
            (f"[插入视频] {it[1]}" if it[0] == "insert" else seg_prompts[it[1]]) for it in exec_items]
        thumbs, videos, seams, bridge_scores = [], [], [], []
        all_frames = []
        all_wav = None
        seg_frames = []
        seg_wavs = []
        trims = []
        guide = None
        prev_tail_frame = None
        prev_tail_wav = None
        prev_tail_clip = None   # 上段尾 24 帧（接缝指标局部基线用）
        sample_rate = None
        seam_metrics_rows = []   # 每缝五维 z-score（与 seams 列表对齐；无缝/指标不可用为 None）
        memory_anchor_rec = None   # 实验 E2：首段落盘的全局记忆锚记录串，回写 manifest memory_anchor 键

        # 选择性重做支撑：旧记录快照（审片中段 break 时循环内积累的列表短于 done，
        # 直接写 [:done] 会丢保留段记录，写盘前用 _merged_list 补尾）
        _old_lists = ({k: list(manifest.get(k) or [])
                       for k in ("thumbs", "videos", "seams", "bridge_scores",
                                 "seam_metrics", "trims")}
                      if (use_ckpt and manifest is not None) else None)

        def _ml(new_lists, upto):
            """manifest 增量键合并写入：新积累 + 旧记录补尾。"""
            if _old_lists is None:
                return {k: list(v)[:upto] for k, v in new_lists.items()}
            return {k: _merged_list(v, _old_lists.get(k, []), upto)
                    for k, v in new_lists.items()}

        prev_end_t = None   # 上一处理生成段的输出末端 token 边界（重摇 unlink 段上锚对齐用；序章/插入段重置 None）

        def _redo_prev_bridge(g):
            """重摇独立镜头段的上锚现算：前段（g-1）存档 latent 尾部切桥。

            unlink 段的前段没为它留 guide（next_wants_bridge=False，guide 变量
            陈旧），需直读前段存档；前段也在重摇列表时其新 latent 已先落盘
            （顺序处理），载入即最新。前段是序章/插入段时 prev_end_t=None
            → 原始尾部（与两处的 guide 更新规则一致）。不可锚返回 None。
            """
            prev_g = g - 1
            if prev_g < 0 or root is None:
                return None
            if prev_g >= off and prev_g - off >= len(exec_items):
                return None
            try:
                pv, pa = checkpoint.load_segment(root, prev_g)
            except Exception:
                return None
            return _tail_keyframe(pv.to(video_vae.device), pa.to(audio_vae.device),
                                  ctx, KEYFRAME_AUDIO_SUPPORTED and full_bridge,
                                  end_tokens=prev_end_t, full_bridge=full_bridge)

        def _redo_next_anchor(g):
            """重摇段的下锚现算：下一段保留段存档 latent 裁头后首 token。

            下段不存在/插入段/unlink 段/禁用段/未完成/也在重摇列表 → None（该侧
            锚自动缺省——转场本就硬切、禁用段不进成片、或由下次重摇自己衔接）。"""
            nxt_g, ni = g + 1, g + 1 - off
            if root is None or ni < 0 or ni >= len(exec_items):
                return None
            if nxt_g >= done or nxt_g in redo_map:
                return None
            kind, idx = exec_items[ni]
            if kind != "prompt" or seg_unlink[idx] or seg_disabled[idx]:
                return None
            try:
                nv, _na = checkpoint.load_segment(root, nxt_g)
            except Exception:
                return None
            skip2 = 0 if _is_fact_first(ni) else ctx
            kf = _seam_tail_kf(nv, skip2)
            return kf.to(video_vae.device) if kf is not None else None

        def _up_hi(g, video_t, audio_t, kind, idx, guide_kf, tail_kf, head_kf, cur_seed,
                   skip_f, vis_len, wav, rate):
            """二采渲染段 g 并落盘高清产物（仅生成段；序章/插入视频等外部素材段跳过）。

            返回 (ready, tried)：ready=True 表示高清产物已在盘（本次渲染成功或
            记录有效沿用——调用方跳过基础分辨率保存，段视频就是二采结果）；
            ready=False 且 tried=True 表示渲染失败（调用方基础保存强制重编码，
            覆盖可能写坏的 mp4 自愈）；范围外/关闭/外部素材段 (False, False) 正常基础保存。
            proj_upscale 原地更新（写盘最新 upscale 存档状态）。
            """
            nonlocal proj_upscale
            if not (up_cfg and use_ckpt):
                return False, False
            # 外部素材段（序章/插入视频）不做二采：内容本就是成品视频，VAE 重编码
            # 后再做神经放大+低强度重采样只引入二次损失；成片拼接时这些段按需
            # reformat 缩放对齐（upscale.try_final 混合源处理）
            if kind != "prompt":
                return False, False
            if not upscale.in_scope(up_cfg, g):
                return False, False
            if g < len(full_hashes) and cur_seed is not None:
                bh = f"{full_hashes[g]}|{cur_seed}"   # 本段当前身份（回放段与 manifest 记录一致）
            else:
                bh = upscale.base_hash(manifest, g) if isinstance(manifest, dict) else ""
            if not upscale.record_stale(manifest, root, up_cfg, g, bh):
                return True, False
            try:
                # 返回 (高清尾帧 CPU tensor, 最新存档状态)：尾帧暂无消费方
                # （跨段连续性仍走基础 latent 桥），只回填存档状态进 manifest
                _, proj_upscale = upscale.render_segment(
                    模型, clip, video_vae, audio_vae, negative, up_cfg, up_net,
                    root, g, video_t, audio_t, kind, idx,
                    seg_prompts, seg_label_orders, pool_tensors, refs,
                    首帧图片 if (kind == "prompt" and idx == 0 and seg_first_on[0]) else None,
                    guide_kf, tail_kf, head_kf, cur_seed,
                    skip_f, vis_len,
                    wav, rate, bh, report, 采样器, 调度器)
                return True, False
            except upscale.UpscaleAbortError:
                raise   # 预检/二采显存致命：报告已 append，终止整链，不降级
            except Exception as e:   # 单段偶发失败只降级该段，整链照常
                oom = "out of memory" in str(e).lower()
                report.append(f"段{g + 1} 二采失败：{type(e).__name__}: {e}"
                              "——本段按基础分辨率保存（基础链产物不受影响）"
                              + ("；显存不够：可把放大倍率调低或缩短该段帧数"
                                 "（精化步数/σ不影响峰值显存）"
                                 "（本段会自动补渲染，基础链不重做）" if oom else ""))
                # 释放二采残留（放大 latent / 解码帧 / 推理缓存）给后续基础采样腾空间；
                # 放大网络可能已在 CPU（render_latent 内 net.cpu()），下段二采自愈装回 GPU
                try:
                    import comfy.model_management
                    comfy.model_management.soft_empty_cache()
                    gc.collect()
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                return False, True

        if use_ckpt:  # 运行起点状态（面板据此定位当前链）
            checkpoint.save_state({"dir": os.path.basename(root), "total": total, "done": done,
                                   "review": bool(review), "reroll": reroll, "report": "",
                                   "updated_at": time.time()})

        if off:
            prologue_fresh = False
            if use_ckpt and done >= 1:
                pv, pa = checkpoint.load_segment(root, 0)
                pv = pv.to(video_vae.device)
                pa = pa.to(audio_vae.device)
                prologue_origin = "存档载入"
            else:
                raw_fc = int(起始视频.shape[0])
                fc = align_frame_count_down(min(raw_fc, length))
                if fc < 5:
                    raise ValueError("起始视频至少需要 5 帧（约 0.2 秒 @24fps），请换更长的视频")
                pv = video_vae.encode(_center_cover(起始视频[:fc], width, height))
                if 起始视频音轨 is not None:
                    pa = _encode_audio_latent(audio_vae, 起始视频音轨, audio_tokens_for_frames(fc))
                else:
                    pa = torch.zeros(1, 32, 2, audio_tokens_for_frames(fc), device=video_vae.device)
                prologue_origin = f"编码{fc}帧"
                prologue_fresh = True
                if use_ckpt:
                    checkpoint.save_segment(root, 0, pv, pa)
                    seeds = [0]
                    done = 1
                    checkpoint.save_manifest(root, {
                        "schema": checkpoint.SCHEMA, "done": 1, "has_prologue": True,
                        "seeds": [0], "trims": [0], "prompt_hashes": [prologue_hash],
                        "total": total, "thumbs": [], "videos": [], "prompts": prompt_list,
                        "seams": [None], "bridge_scores": [None], "params": ckpt_params,
                        "seam_metrics": [None], "seam_refine": seam_refine, "experiments": exp.describe(),
                        "inserts": list(proj_inserts), "upscale": proj_upscale,
                        "redo_queue": list(redo_queue)})
                report.append(f"序章：上传视频编码为段 1/{total}（{fc} 帧"
                              + ("，超长仅取前段" if raw_fc > fc else "")
                              + "，经一次 VAE 重编码，按 24fps 处理"
                              + ("，未接音轨按静音处理" if 起始视频音轨 is None else "") + "）")
            pframes = video_vae.decode(pv)
            if len(pframes.shape) == 5:
                pframes = pframes.reshape(-1, pframes.shape[-3], pframes.shape[-2], pframes.shape[-1])
            pwav, sample_rate = _decode_audio(audio_vae, pa)
            _hi_ready, _hi_tried = _up_hi(0, pv, pa, "prologue", None, None, None, None, 0,
                                          0, pframes.shape[0], pwav, sample_rate)
            if not _hi_ready:
                thumbs.append(checkpoint.save_thumb(root, 0, pframes[0]) if use_ckpt else "")
                videos.append(checkpoint.save_segment_mp4(root, 0, pframes, pwav, sample_rate,
                                                          fresh=prologue_fresh or _hi_tried,
                                                          crf=_bcrf, preset=_bpreset, aq_mode=_baq,
                                                          dither=_bdith) if use_ckpt else "")
            else:
                _u_files = checkpoint.upscale_files(0)
                thumbs.append(_u_files["thumb"])
                videos.append(_u_files["mp4"])
            seams.append(None)
            bridge_scores.append(None)
            seam_metrics_rows.append(None)
            # 双列表共享同一份 CPU 帧（全程只读），长链累积内存减半
            _cf = pframes.cpu()
            all_frames.append(_cf)
            seg_frames.append(_cf)
            seg_wavs.append({"waveform": pwav.cpu(), "sample_rate": sample_rate})
            all_wav = pwav.cpu()
            prev_tail_frame = pframes[-1].cpu()
            prev_tail_clip = pframes[-24:].cpu()
            seam_n0 = max(1, int(sample_rate * 0.25))
            prev_tail_wav = pwav.cpu()[..., -seam_n0:]
            guide = _tail_keyframe(pv, pa, ctx, KEYFRAME_AUDIO_SUPPORTED and full_bridge,
                                   full_bridge=full_bridge)
            report.append(f"段1/{total}：{prologue_origin} 序章 留{pframes.shape[0]}帧 · 种子 — | guide=无（序章）")

        # 每段耗时账（⏱ 报告行数据源）：cond=一采条件构建（TE/参考编码）、
        # sample=一采采样（重摇各次尝试累计）、decode=基础解码裁剪（含重摇重复解码）
        _seg_t = {"cond": 0.0, "sample": 0.0, "decode": 0.0}

        def _decode_crop(i, video_t, audio_t, skip_f, gi, next_bridge):
            """解码 → 桥帧门控 → 尾切 token 对齐 → 裁剪到保留区（重摇与正常路径共用）。

            gi=全局段号（混排链位+off，报告用）；next_bridge=下一段是否接收本段尾帧桥
            （下段是独立镜头/插入视频/末段时为 False：门控是为下段桥服务的，
            下段不要桥就不必牺牲本段帧数）。
            返回 (frames, wav, sample_rate, end_t, vis_len, 桥帧总分, 门控报告行)；
            报告行只取最终采用的尝试（重摇的中间尝试整组丢弃）。
            """
            lines = []
            _t = time.perf_counter()
            sampled_fc = latent_t_to_frames(video_t.shape[2])
            vis_len = seg_lengths[i]
            frames = video_vae.decode(video_t)
            if len(frames.shape) == 5:
                frames = frames.reshape(-1, frames.shape[-3], frames.shape[-2], frames.shape[-1])
            # 音频归一化只统计保留区（见 _decode_audio）：锚定区音频不计入 std
            wav, sample_rate = _decode_audio(audio_vae, audio_t,
                                             norm_skip_frac=skip_f / sampled_fc if skip_f else 0.0)
            seg_bridge_score = None
            if 桥帧门控 != "关闭" and next_bridge:
                window = frames[max(skip_f, frames.shape[0] - (ctx + gate_limit)):]
                score = qc.frame_scores(window)
                tail_score = float(score[-1])
                seg_bridge_score = round(tail_score, 2)
                if 桥帧门控 == "标注":
                    flag = " ↓ 低于阈值" if tail_score < 清晰度阈值 else ""
                    lines.append(f"段{gi + 1} 桥帧总分 {tail_score:.1f}{flag}")
                else:  # 自动回退
                    back, hit = qc.pick_backtrack(score, gate_limit, float(清晰度阈值))
                    if back:
                        vis_len = seg_lengths[i] - back
                        lines.append(f"段{gi + 1} 尾帧低质（{tail_score:.1f} < {清晰度阈值:g}），"
                                     f"回退 {back} 帧续拍（回退点 {hit:.1f}）")
            # 尾切对齐 token 网格：kept 末端必须与 guide 锚定末端重合。此前 guide
            # 取采样 latent 原始尾部（含 17k+5 网格填充帧，从未输出），下段续拍点
            # 落在本段输出末尾之后——ctx=56 时每个接缝跳过 15 帧（0.6s）内容。
            # 门控回退时向下对齐（不回吐坏帧），否则向上（不丢内容，每段至多多留几帧）
            end_t = frames_to_latent_t(skip_f + vis_len, up=(seg_lengths[i] - vis_len) == 0)
            vis_len = latent_t_to_frames(end_t) - skip_f
            frames = frames[skip_f:skip_f + vis_len]
            wav_total = wav.shape[-1]
            skip_s = round(wav_total * skip_f / sampled_fc)
            take_s = round(wav_total * vis_len / sampled_fc)
            wav = wav[..., skip_s:skip_s + take_s]
            _seg_t["decode"] += time.perf_counter() - _t
            return frames, wav, sample_rate, end_t, vis_len, seg_bridge_score, lines

        def _is_fact_first(ni):
            """事实首段判定：ni 位置之前（含序章位）没有任何已执行段。
            序章存在或前方有插入段/启用提示词段 → 非事实首段（有锚定来源）；
            禁用段不算已执行段——禁用段 0 时段 1 成为事实首段（不裁头不锚定）。"""
            if off:
                return False
            for j in range(ni):
                it = exec_items[j]
                if it[0] == "insert" or not seg_disabled[it[1]]:
                    return False
            return True

        def _next_wants_bridge(item_i):
            """混排序列中 item_i 的下一段是否接收本段尾帧桥：下段不存在/是插入段/
            是独立镜头提示词段 → False（插入段不吃 guide，独立镜头不要桥）"""
            nxt = exec_items[item_i + 1] if item_i + 1 < len(exec_items) else None
            return bool(nxt) and nxt[0] == "prompt" and not seg_unlink[nxt[1]]

        # E2 记忆锚来源段：第一个启用的提示词段——段 0 禁用时锚从首个执行段取，
        # 否则全链无锚文件、E2 静默失效（禁用不触发重做，回放段不重落锚是设计内）
        _anchor_src = next((it[1] for it in exec_items
                            if it[0] == "prompt" and not seg_disabled[it[1]]), None)

        for item_i, item in enumerate(exec_items):
            # ---- 插入视频段：画面+原声进成片，尾帧 latent 桥接指导下一段 ----
            if item[0] == "insert":
                ins_file = item[1]
                g = item_i + off
                ins_replay = use_ckpt and g < done
                if ins_replay:
                    pv, pa = checkpoint.load_segment(root, g)
                    pv = pv.to(video_vae.device)
                    pa = pa.to(audio_vae.device)
                    report.append(f"段{g + 1}/{total}：插入视频「{ins_file}」存档载入 · guide=尾帧桥（指导下段）")
                else:
                    imgs, aud = _load_input_video(ins_file)
                    raw_fc = int(imgs.shape[0])
                    fc = align_frame_count_down(min(raw_fc, length))
                    if fc < 5:
                        raise ValueError(f"插入视频「{ins_file}」至少需要 5 帧（约 0.2 秒 @24fps），请换更长的视频")
                    pv = video_vae.encode(_center_cover(imgs[:fc], width, height))
                    _wav_in = aud.get("waveform") if isinstance(aud, dict) else None
                    if _wav_in is not None and int(_wav_in.shape[-1]) > 0:
                        pa = _encode_audio_latent(audio_vae, aud, audio_tokens_for_frames(fc))
                        _mute = ""
                    else:
                        pa = torch.zeros(1, 32, 2, audio_tokens_for_frames(fc), device=video_vae.device)
                        _mute = "，无音轨按静音处理"
                    if use_ckpt:
                        checkpoint.save_segment(root, g, pv, pa)
                    report.append(f"段{g + 1}/{total}：插入视频「{ins_file}」编码 {fc} 帧"
                                  + ("，超长仅取前段" if raw_fc > fc else "") + _mute
                                  + " · guide=尾帧桥（指导下段）")
                pframes = video_vae.decode(pv)
                if len(pframes.shape) == 5:
                    pframes = pframes.reshape(-1, pframes.shape[-3], pframes.shape[-2], pframes.shape[-1])
                pwav, sample_rate = _decode_audio(audio_vae, pa)
                _hi_ready, _hi_tried = _up_hi(g, pv, pa, "insert", None, None, None, None, 0,
                                              0, pframes.shape[0], pwav, sample_rate)
                if not _hi_ready:
                    thumbs.append(checkpoint.save_thumb(root, g, pframes[0]) if use_ckpt else "")
                    videos.append(checkpoint.save_segment_mp4(root, g, pframes, pwav, sample_rate,
                                                              fresh=not ins_replay or _hi_tried,
                                                              crf=_bcrf, preset=_bpreset, aq_mode=_baq,
                                                              dither=_bdith) if use_ckpt else "")
                else:
                    _u_files = checkpoint.upscale_files(g)
                    thumbs.append(_u_files["thumb"])
                    videos.append(_u_files["mp4"])
                # 插入段与上段硬切（外部素材画面固定）：指标记 None
                seams.append(None)
                bridge_scores.append(None)
                seam_metrics_rows.append(None)
                if g < len(seeds):
                    seeds[g] = 0
                else:
                    seeds.append(0)
                trims.append(0)
                _cf = pframes.cpu()
                all_frames.append(_cf)
                seg_frames.append(_cf)
                seg_wavs.append({"waveform": pwav.cpu(), "sample_rate": sample_rate})
                all_wav = pwav.cpu() if all_wav is None else torch.cat([all_wav, pwav.cpu()], dim=-1)
                prev_tail_frame = pframes[-1].cpu()
                prev_tail_clip = pframes[-24:].cpu()
                _seam_n0 = max(1, int(sample_rate * 0.25))
                prev_tail_wav = pwav.cpu()[..., -_seam_n0:]
                guide = _tail_keyframe(pv, pa, ctx, KEYFRAME_AUDIO_SUPPORTED and full_bridge,
                                       full_bridge=full_bridge)
                if not ins_replay:
                    proj_inserts.append({"slot": g, "file": ins_file})
                pbar.update(1)
                if use_ckpt and not ins_replay:
                    done = g + 1
                    checkpoint.save_manifest(root, {
                        "schema": checkpoint.SCHEMA, "done": done, "has_prologue": bool(off),
                        "seeds": list(seeds[:done]),
                        "prompt_hashes": full_hashes[:done],
                        "total": total,
                        "prompts": prompt_list[:done],
                        "params": ckpt_params, "seam_refine": seam_refine, "experiments": exp.describe(),
                        "memory_anchor": memory_anchor_rec,
                        "inserts": list(proj_inserts),
                        "title": proj_title, "created_at": proj_created,
                        "updated_at": time.time(), "finals": list(proj_finals),
                        "upscale": proj_upscale,
                        "redo_queue": list(redo_queue),
                        **_ml({"thumbs": thumbs, "videos": videos, "seams": seams,
                               "bridge_scores": bridge_scores, "seam_metrics": seam_metrics_rows,
                               "trims": trims}, done),
                    })
                prev_end_t = None   # 插入段无 token 对齐末端（重摇上锚回落原始尾部）
                if review and not ins_replay and item_i + 1 < len(exec_items):
                    report.append(f"审片：段 {g + 1}（插入视频）已完成并落盘 → seg_{g:03d}.mp4；"
                                  "满意请直接重新运行继续下一段")
                    break
                continue

            i = item[1]   # 提示词段索引（seg_lengths/seg_unlink/seg_disabled/seg_label_orders 均按此索引）
            prompt = seg_prompts[i]
            g = item_i + off  # 全局段下标（有序章时序章占 0 号）
            # 段禁用（不上链）：槽位占位跳过——不采样/不解码/不进成片；manifest
            # 记录类列表沿用旧记录（无则空占位）保槽位不错位；done 照常推进
            #（禁用段视作已处理；段文件缺失由下方 replay 存在性守卫兜底，重新
            # 上链时自动采样）；guide/prev_tail 不更新 → 下段锚定最近一个已执行
            # 段的尾帧（与成片实际顺序一致）
            if seg_disabled[i]:
                def _old_rec(k, default):
                    seq = (_old_lists or {}).get(k) or []
                    return seq[g] if g < len(seq) else default
                thumbs.append(_old_rec("thumbs", ""))
                videos.append(_old_rec("videos", ""))
                seams.append(_old_rec("seams", None))
                bridge_scores.append(_old_rec("bridge_scores", None))
                seam_metrics_rows.append(_old_rec("seam_metrics", None))
                trims.append(_old_rec("trims", 0))
                if g >= len(seeds):
                    seeds.append(0)   # 种子占位（与插入段同规则；不执行不消费）
                if g + 1 > done:
                    done = g + 1
                report.append(f"段{g + 1}/{total}：跳过（已禁用，不上链——不采样不进成片，"
                              "「重新上链」随时恢复）")
                pbar.update(1)
                continue
            # 重摇段不走回放（重新采样）；其余已完成段照常直读存档。
            # 段文件存在性守卫：禁用段从未生成过（done 却已推进越过它）或存档
            # 被手动删除时，落回采样分支重新生成——replay 不再因缺文件崩溃
            _redo_mode = redo_map.get(g)
            replay = (use_ckpt and g < done and _redo_mode is None
                      and os.path.exists(checkpoint.seg_path(root, g)))
            next_wants_bridge = _next_wants_bridge(item_i)
            # 桥区软着陆（实验）预检：段首钉住上段尾，只烧「钉住帧数」而不是整段 ctx 帧。
            # 预检必须放在 cond 构造之前——seg_len 一旦按某档定死就不能中途改，
            # 否则 cond 的帧数与裁剪量对不上。任一条件不满足即整段走现状路径。
            _has_src = not (_is_fact_first(item_i) or seg_unlink[i])
            _soft_hold = 0
            if (exp.has("soft_bridge") and _sb_level > 0 and _has_src
                    and guide is not None
                    and _redo_mode not in ("仅锚下段", "无锚")):
                _hf, _ht, _ha = bridge.plan_for_guide(
                    guide, int(exp.param("soft_bridge", "钉住帧数", 9)))
                if _ht > 0:
                    _soft_hold = _hf
            # skip_f 与采样额外帧数一次定死：回放分支与生成分支共用同一个值。
            # 软桥段存下的是「段长+hold」帧的 latent，回放时若按 ctx 裁会多裁帧。
            skip_f, _seg_extra = bridge.resolve_crop(_soft_hold, ctx, _has_src)
            # 首帧图段级引用（中段）：头锚 latent——生成段注入 cond，回放段二采补渲染同用
            #（定义在 replay 分支之前：两路都消费；独立镜头段照常注入，本段主动锚）
            _head_kf = head_frame_latent \
                if (i > 0 and seg_first_on[i] and head_frame_latent is not None) else None
            _seg_t.update(cond=0.0, sample=0.0, decode=0.0)
            _soft_note = ""   # 软桥报告后缀（只有采样段装配成功才非空；回放段为空）
            if replay:
                video_t, audio_t = checkpoint.load_segment(root, g)
                video_t = video_t.to(video_vae.device)
                audio_t = audio_t.to(audio_vae.device)
                dt = 0.0
                cur_seed = seeds[g] if g < len(seeds) else None
                frames, wav, sample_rate, end_t, vis_len, seg_bridge_score, gate_lines = \
                    _decode_crop(i, video_t, audio_t, skip_f, gi=g, next_bridge=next_wants_bridge)
            else:
                # 种子规则：重摇段用控件种子（与存档相同则 bump +1，保证每次重摇必变
                # ——base_hash 随种子变化驱动二采自动重渲）；重摇（重跑起始段>0）用
                # 控件种子；否则延续断点种子序列的等差，使审片多轮运行与一次跑完
                # 逐帧一致；断点无生成段种子（仅序章/新链）才用控件种子
                if _redo_mode is not None:
                    cur_seed = _redo_seed(seed, i, seeds[g] if g < len(seeds) else None)
                elif reroll > 0:
                    cur_seed = (seed + i) % 0xffffffffffffffff
                elif len(seeds) > off:
                    cur_seed = (seeds[-1] + g - len(seeds) + 1) % 0xffffffffffffffff
                else:
                    cur_seed = (seed + i) % 0xffffffffffffffff
                # 软桥：只补「钉住帧数」；现状路径仍补整段 ctx 帧（钉桥 + 裁头）
                seg_len = seg_lengths[i] + _seg_extra
                if has_refs:
                    # 段级注入：只把本段勾选的素材压实进 conditioning（未勾选的根本不进本段），
                    # 三类各自独立编号 ref_image_/ref_video_/ref_audio_（<Picture>/<Video>/<Audio>）
                    seg_refs = {"ref_images": {}, "ref_videos": {}, "ref_video_audios": {}, "ref_audios": {}}
                    _n = {"image": 0, "video": 0, "audio": 0}
                    for _k, _lbl in seg_label_orders[i]:
                        if _lbl not in pool_tensors.get(_k, {}):
                            continue
                        if _k == "image":
                            seg_refs["ref_images"][f"ref_image_{_n['image']}"] = pool_tensors["image"][_lbl]
                        elif _k == "video":
                            _imgs, _aud = pool_tensors["video"][_lbl]
                            seg_refs["ref_videos"][f"ref_video_{_n['video']}"] = _imgs
                            seg_refs["ref_video_audios"][f"ref_video_audio_{_n['video']}"] = _aud
                        else:
                            seg_refs["ref_audios"][f"ref_audio_{_n['audio']}"] = pool_tensors["audio"][_lbl]
                        _n[_k] += 1
                    # 未被状态池接管的画布接线类别：沿用旧行为全段共用，编号接在状态素材之后
                    for j, v in enumerate(refs["ref_videos"].values()):
                        seg_refs["ref_videos"][f"ref_video_{_n['video'] + j}"] = v
                    for j, v in enumerate(refs["ref_video_audios"].values()):
                        seg_refs["ref_video_audios"][f"ref_video_audio_{_n['video'] + j}"] = v
                    for j, v in enumerate(refs["ref_audios"].values()):
                        seg_refs["ref_audios"][f"ref_audio_{_n['audio'] + j}"] = v
                    _t = time.perf_counter()
                    out = MiniMaxH3ReferenceToVideo.execute(
                        clip=clip, vae=video_vae, audio_vae=audio_vae,
                        prompt=prompt, width=width, height=height, length=seg_len,
                        ref_image_size="match", **seg_refs)
                else:
                    _t = time.perf_counter()
                    out = MiniMaxH3ImageToVideo.execute(
                        clip=clip, vae=video_vae, prompt=prompt,
                        width=width, height=height, length=seg_len,
                        first_frame=首帧图片 if (i == 0 and seg_first_on[0]) else None)
                _seg_t["cond"] += time.perf_counter() - _t

                cond, latent = out[0], out[1]
                # 锚定来源：普通段 = 段属性（unlink 屏蔽上桥、尾帧图/每段尾帧锚定收尾）；
                # 重摇段 = 四种锚定模式（本次重做的临时策略，独立于段属性 unlink）——
                # 显式身份锚（尾帧图/每段尾帧锚定/首帧图）不受模式影响，模式只控制接缝锚
                if _redo_mode is not None:
                    _user_tail = end_frame_latent \
                        if (seg_end_on[i] and end_frame_latent is not None) else tail_anchor_latent
                    if _redo_mode in ("双锚", "仅锚上段"):
                        # unlink 前段没为它留 guide（陈旧），需直读前段存档现算尾桥
                        eff_guide = _redo_prev_bridge(g) if seg_unlink[i] else guide
                    else:
                        eff_guide = None
                    if _user_tail is not None:
                        _tail_kf = _user_tail
                    elif _redo_mode in ("双锚", "仅锚下段"):
                        _tail_kf = _redo_next_anchor(g)   # 下段不可锚自动缺省 None
                    else:
                        _tail_kf = None
                else:
                    # 独立镜头段：上段桥不注入（guide 屏蔽为 None）；每段尾帧锚定是用户主动
                    # 设定的本段结尾身份锚点，与段间衔接无关，不受断链影响
                    eff_guide = None if seg_unlink[i] else guide
                    # 尾帧图片（FL2VA 剧情终点/段级尾锚）：勾了尾帧图的段末帧 keyframe
                    # = 尾帧图 latent（同位置唯一锚，优先于每段尾帧锚定）；未勾段回落尾锚
                    _tail_kf = end_frame_latent if (seg_end_on[i] and end_frame_latent is not None) \
                        else tail_anchor_latent
                if eff_guide is not None or _tail_kf is not None or _head_kf is not None:
                    # 实验 E1：强化引导桥——把单桥展开为滑窗/重叠 keyframe 序列。
                    # 全关时 e1_kfs=None，_apply_guide 走现状单桥路径（零影响）。
                    e1_kfs = None
                    if exp.has("e1_bridge_shard") and eff_guide is not None:
                        win_t = int(exp.param("e1_bridge_shard", "滑窗token", 0))
                        ov_t = int(exp.param("e1_bridge_shard", "重叠token", 0))
                        shard_frames = int(exp.param("e1_bridge_shard", "子片帧数", 0))
                        if shard_frames > 0:  # 子片帧数细化滑窗粒度（token 化后取更小窗）
                            win_t = min(win_t, frames_to_latent_t(shard_frames, up=True)) if win_t > 0 \
                                else frames_to_latent_t(shard_frames, up=True)
                        e1_kfs = experiments.e1_window_kf(eff_guide, win_t, ov_t, latent_t_to_frames)
                    # 实验 E2：全局记忆锚——读回首段开头 latent 裁出的 reference，
                    # 按注入位置（段首=0 / 全程=段首+段中）追加 keyframe，与段间桥、
                    # 尾帧锚叠加沿链恒定注入，抑制长链逐段累积漂移。无 root/无锚不注入。
                    memory_kfs = None
                    if exp.has("e2_memory_anchor") and root:
                        _ma = checkpoint.load_memory_anchor(root)
                        if _ma is not None:
                            _ma = _ma.to(video_vae.device)
                            _pos_mode = exp.param("e2_memory_anchor", "注入位置", "段首")
                            _sampled_fc = latent_t_to_frames(latent["samples"].tensors[0].shape[2])
                            memory_kfs = [
                                {"resolved_frame_index": fi, "latent": _ma}
                                for fi in experiments.memory_anchor_positions(_pos_mode, _sampled_fc)
                            ]
                    # 多锚点（实验）：段中锚点——复用本段已有的头锚/记忆锚素材，在段中部
                    # 再钉一个锚抑制长段内部漂移。走 guides 的官方语义校验（越界/放不下就跳过）。
                    mid_kfs = None
                    if exp.has("mid_anchor"):
                        _mid_fc = latent_t_to_frames(latent["samples"].tensors[0].shape[2])
                        # 优先用记忆锚（首段**视频** latent，动态、与链同源）；头锚是
                        # 静态首帧图，钉在段中会让画面构图突然回落——仅作退路并明确提示。
                        _mid_is_still = False
                        if memory_kfs:
                            _mid_src = memory_kfs[0]["latent"]
                        elif _head_kf is not None:
                            _mid_src, _mid_is_still = _head_kf, True
                        else:
                            _mid_src = None
                        if _mid_src is None:
                            report.append(f"段{g + 1} 段中锚点跳过：本段没有头锚/记忆锚素材可复用")
                        else:
                            _mid_kf, _mid_why = guides.prepare_anchor(
                                guides.mid_anchor_index(
                                    _mid_fc, float(exp.param("mid_anchor", "锚点位置", 0.5))),
                                _mid_fc, video_latent=_mid_src, label="段中")
                            if _mid_kf is not None:
                                mid_kfs = [_mid_kf]
                                report.append(f"段{g + 1} 段中锚点：帧 {_mid_kf['resolved_frame_index']}"
                                              f"/{_mid_fc}（抑制段内漂移）"
                                              + ("，素材=静态首帧图（可能使构图回落，建议改用 E2 记忆锚）"
                                                 if _mid_is_still else "，素材=E2 记忆锚"))
                            else:
                                report.append(f"段{g + 1} {_mid_why}")
                    cond = cls._apply_guide(
                        cond, eff_guide, latent_t_to_frames(latent["samples"].tensors[0].shape[2]),
                        tail_kf_latent=_tail_kf, e1_windows=e1_kfs, memory_kfs=memory_kfs,
                        head_kf_latent=_head_kf, mid_kfs=mid_kfs)
                    # 锚点越界体检（只报不拦）：官方 AddGuide 明确判越界，这里给出排查线索
                    for _w in guides.audit_keyframes(
                            cond[0][1].get("minimax_keyframes", []),
                            latent_t_to_frames(latent["samples"].tensors[0].shape[2])):
                        report.append(f"段{g + 1} {_w}")
                    # 锚定加噪（SkyReels-V2 addnoise_condition 思路）：H3 模型 payload
                    # 原生支持 cond 噪声增强（extra_conds 从 cond dict 任意键取参），
                    # aug=1.0 即不加噪；值越小锚定越「软」，缓解段首刹车/内容重演
                    if aug > 0.0:
                        cond = _apply_anchor_noise(cond, aug)

                # 桥区软着陆装配：初始 latent 头部写入上段尾 + 逐 token 掩码（video/audio 各一份）。
                # 装配失败不重来——仍按钉住帧数裁头（形状一致），只是没有钉住效果。
                _sampling_latent = latent
                _soft_note = ""
                if _soft_hold:
                    _sb_curve = str(exp.param("soft_bridge", "释放曲线", "hold"))
                    if _sb_curve not in bridge.CURVES:
                        _sb_curve = "hold"
                    try:
                        _sampling_latent, _sb_mask, _soft_info = bridge.apply_soft_bridge(
                            latent, eff_guide, _soft_hold, _sb_curve)
                        skip_f = int(_soft_info["hold_frames"])
                        _soft_note = (f" · 软桥钉住上段尾 {_soft_info['hold_frames']}帧"
                                      f"/{_soft_info['hold_tokens']}token"
                                      f"（音频 {_soft_info['hold_audio_tokens']}token"
                                      f" · {_sb_curve}）")
                    except Exception as e:
                        if "out of memory" in str(e).lower():
                            raise   # 显存致命：不静默降级，交上层终止并报告
                        _sampling_latent = latent
                        skip_f = _soft_hold
                        _soft_note = f" · 软桥装配失败（{e}），按 {_soft_hold} 帧裁头"

                # 接缝自动重摇：本段生成后若缝差超阈值，换种子重采本段（上限内），
                # 排除抽卡坏段（同参数下缝差 0.02-0.17 波动大）。cond/latent 与种子
                # 无关只构造一次；回放段不参与。各次尝试取缝差最小的一组（CPU 快照，
                # 不占显存；末次反而更差时回切）。最终种子进 manifest（重放可复现）
                reroll_max = max(0, int(重摇上限)) if (接缝重摇 == "自动" and skip_f) else 0
                attempt = 0
                d_raw = None
                best = None   # (缝差, 结果快照)
                # 实验 E3：运动感知闭环门控——首采后算一次 flow_z/cam_z，
                # 若超阈值则触发一次额外重采（动作=重摇/重锚）。全关时 motion_extra
                # = 0，重摇上限与现状完全一致（零影响）。
                motion_extra = 0    # 额外触发次数（0 或 1）
                motion_action = "重摇"
                e3_z_row = None     # 首采后填入，避免重复计算
                if exp.has("e3_motion_gate"):
                    motion_action = str(exp.param("e3_motion_gate", "触发动作", "重摇"))
                    if motion_action not in ("重摇", "重锚"):
                        motion_action = "重摇"
                while True:
                    # 实验 E3：触发动作=重锚时，第二次采样（attempt==1，首次额外重采）
                    # 用更硬的锚定（aug 减半），等价于"把段首桥换成更密的受控锚再采"。
                    # 全关 / 动作非重锚 / attempt==0 时不改动 cond。
                    if (exp.has("e3_motion_gate") and motion_action == "重锚"
                            and attempt == 1 and aug > 0.0 and (eff_guide is not None
                                                               or tail_anchor_latent is not None)):
                        cond = _apply_anchor_noise(cond, max(0.0, aug * 0.5))
                    # 共存 H3 插件可能丢 keyframe/refs 音频导致 cond_audio 行数错位，
                    # 采样期挂模型层兜底（见 cond_audio_rows_guard），完成后恢复
                    restore_audio_rows = cond_audio_rows_guard(模型.model.diffusion_model)
                    # 官方 0.33.x extra_conds 用「=」覆盖 cond_video_latents：
                    # 参考图与段间桥/锚点同段共存时丢 keyframe latent（见
                    # cond_video_rows_guard），同样在采样期兜底
                    restore_video_rows = cond_video_rows_guard(模型.model.diffusion_model)
                    # 递减锚定：visual_cond_noise_aug 随采样进度从强到弱递减到消失。
                    # 开启时覆盖 _apply_anchor_noise 写入的固定值——每步动态计算
                    restore_step_aug = None
                    if fade_ratio > 0 and (eff_guide is not None or tail_anchor_latent is not None):
                        aug_start = 1.0 - aug if aug > 0 else 0.999
                        restore_step_aug = step_cond_noise_guard(
                            模型.model.diffusion_model, aug_start, 0.0, fade_ratio)
                    try:
                        t0 = time.perf_counter()
                        # 软桥开启时传的是装好钉住内容与 noise_mask 的 latent；
                        # 未开启就是官方空 latent（与现状逐字节一致）
                        sampled = nodes.common_ksampler(
                            模型, cur_seed, 步数, CFG,
                            采样器, 调度器, cond, negative, _sampling_latent, denoise=1.0)[0]
                    finally:
                        restore_audio_rows()
                        restore_video_rows()
                        if restore_step_aug:
                            restore_step_aug()
                    dt = time.perf_counter() - t0
                    _seg_t["sample"] += dt
                    video_t, audio_t = sampled["samples"].unbind()
                    frames, wav, sample_rate, end_t, vis_len, seg_bridge_score, gate_lines = \
                        _decode_crop(i, video_t, audio_t, skip_f, gi=g, next_bridge=next_wants_bridge)
                    d_raw = None
                    if prev_tail_frame is not None and not seg_unlink[i]:
                        d_raw = qc.seam_metrics(prev_tail_frame, frames[0])[0]
                    if best is None or (d_raw is not None and (best[0] is None or d_raw < best[0])):
                        best = (d_raw, (frames.cpu(), wav.cpu(), sample_rate,
                                        video_t, audio_t, end_t, vis_len,
                                        seg_bridge_score, list(gate_lines), cur_seed))
                    # 实验 E4：双向过渡重生成——首次采样后（attempt==0）若缝差超阈值，
                    # 对缝区做 past|transition|future 三窗双锚 + 缝区独占噪声的定向重采样。
                    # 只改 transition 窗，前后帧不动；E4 优先于整段重摇，E4 后仍不合格再回退。
                    # 全关 / 独立镜头 / 无缝 / 过渡窗过小 → 跳过（零影响）。
                    # 软桥段跳过 E4：E4 用「缝区独占纯噪声」重采过渡窗，会把刚钉住的
                    # 上段尾衔接一并冲掉，与软桥目标相反；软桥缝差本就更小，E4 触发
                    # 概率也低。两者不叠加，E4 仍是现状路径（无软桥）的兜底。
                    if (attempt == 0 and exp.has("e4_transition_res")
                            and not _soft_hold
                            and prev_tail_frame is not None and not seg_unlink[i]
                            and d_raw is not None and d_raw > float(重摇阈值)):
                        _e4_tf = int(exp.param("e4_transition_res", "过渡窗帧数", 17))
                        _e4_steps = int(exp.param("e4_transition_res", "重生成步数", 20))
                        _e4_strength = float(exp.param("e4_transition_res", "双锚强度", 1.0))
                        _e4_result = cls._e4_transition_resample(
                            模型, clip, cond, negative, latent, video_t, audio_t,
                            采样器, 调度器, 步数, CFG, cur_seed,
                            i, skip_f, g, next_wants_bridge,
                            _decode_crop, prev_tail_frame, qc,
                            transition_frames=_e4_tf,
                            resample_steps=_e4_steps,
                            dual_anchor_strength=_e4_strength,
                            video_vae=video_vae, audio_vae=audio_vae,
                            aug=aug, fade_ratio=fade_ratio,
                            eff_guide=eff_guide, tail_anchor_latent=tail_anchor_latent,
                            cond_audio_rows_guard=cond_audio_rows_guard,
                            cond_video_rows_guard=cond_video_rows_guard,
                            step_cond_noise_guard=step_cond_noise_guard,
                            _apply_anchor_noise=_apply_anchor_noise,
                            latent_t_to_frames=latent_t_to_frames,
                            frames_to_latent_t=frames_to_latent_t,
                        )
                        if _e4_result is not None:
                            frames, wav, sample_rate, video_t, audio_t, end_t, vis_len, \
                                seg_bridge_score, gate_lines, _e4_d, _e4_lines = _e4_result
                            d_raw = _e4_d
                            report.extend(_e4_lines)
                            report.append(
                                f"段{g + 1} 过渡重生成：缝差 {d_raw:.3f}（重生成{_e4_steps}步，"
                                f"过渡窗{_e4_tf}帧，双锚强度{_e4_strength:g}）")
                    # 实验 E3：首采后算一次运动 z（仅 attempt==0，避免重复开销）。
                    # 帧差本身未超阈值但 flow_z/cam_z 异常 → 额外触发 1 次重采（动作由参数决定）。
                    # 全关时 motion_extra 恒为 0，不进入此分支。
                    if (attempt == 0 and exp.has("e3_motion_gate")
                            and prev_tail_clip is not None and not seg_unlink[i]):
                        try:
                            e3_z_row = metrics.evaluate_local(prev_tail_clip, frames[:48].cpu())
                        except Exception:
                            e3_z_row = None
                        _mz_th = float(exp.param("e3_motion_gate", "运动z阈值", 2.0))
                        _mtrig, _maction = experiments.e3_motion_trigger(
                            e3_z_row, _mz_th, motion_action)
                        if _mtrig and d_raw is not None and d_raw <= float(重摇阈值):
                            motion_extra = 1
                            motion_action = _maction
                            report.append(
                                f"段{g + 1} 运动门控：{metrics.fmt_seam_z(e3_z_row) or '指标缺失'} "
                                f"（|z|>{_mz_th:g}σ）→ 触发一次{_maction}")
                    # 重摇退出条件：帧差达标 且 运动门控无需额外次数（motion_extra 耗尽）
                    _effective_max = reroll_max + motion_extra
                    if d_raw is None or (d_raw <= float(重摇阈值) and motion_extra <= 0) \
                            or attempt >= _effective_max:
                        break
                    if motion_extra > 0:
                        motion_extra -= 1
                    attempt += 1
                    cur_seed = (cur_seed + 7919) % 0xffffffffffffffff
                    report.append(f"段{g + 1} 接缝 {d_raw:.3f} > {重摇阈值:g}，自动换种子重摇（{attempt}/{reroll_max}）")
                if attempt:
                    if best[0] is not None and (d_raw is None or best[0] < d_raw):
                        (frames, wav, sample_rate, video_t, audio_t, end_t, vis_len,
                         seg_bridge_score, gate_lines, cur_seed) = best[1]
                        report.append(f"段{g + 1} 重摇 {attempt} 次取缝差最小（{best[0]:.3f} < 末次 {d_raw:.3f}）")
                        d_raw = best[0]
                    report.append(f"段{g + 1} 重摇后接缝 {d_raw:.3f} · 种子 {cur_seed}")
                # 维持不变量 seeds[g] = 该段种子（段文件缺失导致 done 回退时，
                # 重做段的种子可能与 manifest 残留记录相同，按下标赋值避免列表重复错位）
                if g < len(seeds):
                    seeds[g] = cur_seed
                else:
                    seeds.append(cur_seed)
                if use_ckpt:
                    checkpoint.save_segment(root, g, video_t, audio_t)
            report.extend(gate_lines)
            bridge_scores.append(seg_bridge_score)
            trims.append(seg_lengths[i] - vis_len)

            # 段首响度对齐（与分镜链同款）：增益匹配上段尾 RMS（±6dB 钳制 + 1s 渐出），
            # 增益不沿链累积；归一化已排除锚定区，此处兜住内容本身的响度差。
            # 独立镜头段跳过（独立镜头常配独立声音设计）
            if (item_i > 0 or off) and prev_tail_wav is not None and not seg_unlink[i]:
                # 软桥下音频头部与上段尾同帧钉住，接缝已逐帧连续：默认不再叠增益
                # （强度 0 = 不干预），残留响度差由「音频接缝软过渡」开关按需要回加。
                _la_strength = 1.0
                if exp.has("audio_seam"):
                    _la_strength = float(exp.param("audio_seam", "响度对齐强度", 0.0))
                wav, gain_db = qc.loudness_align_head(wav, prev_tail_wav, rate=sample_rate,
                                                      strength=_la_strength)
                if gain_db is not None:
                    report.append(f"段{g + 1} 响度对齐：段首 {gain_db:+.1f} dB（1s 渐出）")

            # 接缝后验测量（测而不干预）：上一段最后可见帧 vs 本段首帧
            seam_n = int(sample_rate * 0.25)
            seam_d, seam_db = None, None
            if (item_i > 0 or off) and prev_tail_frame is not None and not seg_unlink[i]:
                seam_d, seam_db = qc.seam_metrics(prev_tail_frame, frames[0],
                                                  prev_tail_wav, wav[..., :seam_n], rate=sample_rate)
                db_txt = f"{seam_db:+.1f} dB" if seam_db is not None else "—"
                flag = " ↑ 建议人工检查" if seam_d > 0.08 or (seam_db is not None and abs(seam_db) > 6.0) else ""
                report.append(f"段{g + 1} 接缝：帧差 {seam_d:.3f} · 响度跳变 {db_txt}{flag}")

            if (item_i > 0 or off) and prev_tail_frame is not None and not seg_unlink[i]:
                seams.append([round(seam_d, 4), None if seam_db is None else round(seam_db, 2)])
            else:
                seams.append(None)
            # 接缝五维 z-score（光流/加速度/LPIPS/嵌入/相机）：上段尾 24 帧 +
            # 本段头 48 帧的局部基线评测，在最终成帧上测。
            # 写入 manifest seam_metrics（tools/ab_report.py 的 A/B 对比数据源）
            z_row = None
            if (item_i > 0 or off) and prev_tail_clip is not None and not seg_unlink[i]:
                try:
                    z_row = metrics.evaluate_local(prev_tail_clip, frames[:48].cpu())
                    z_txt = metrics.fmt_seam_z(z_row)
                    if z_txt and "无可用" not in z_txt:
                        report.append(f"段{g + 1} 接缝基准：{z_txt}（|z|<2 合格）")
                except Exception:
                    z_row = None
            seam_metrics_rows.append(z_row if ((item_i > 0 or off) and not seg_unlink[i]) else None)

            _cf = frames.cpu()
            all_frames.append(_cf)
            seg_frames.append(_cf)
            seg_wav = wav.cpu()
            seg_wavs.append({"waveform": seg_wav, "sample_rate": sample_rate})
            all_wav = seg_wav if all_wav is None else torch.cat([all_wav, seg_wav], dim=-1)
            prev_tail_frame = frames[-1].cpu()
            prev_tail_clip = frames[-24:].cpu()
            prev_tail_wav = seg_wav[..., -seam_n:]
            # 实验 E2：首段（第一个 prompt 生成段，非回放）采样定稿后，把开头 mem_t 个
            # token 的视频 latent 落盘为全局记忆锚（供后续段 load_memory_anchor 注入）。
            # 整链重做/首段重做即重落（truncate(0) 已清旧锚文件）。沿用 latent 数值均值/方差
            # 构造哈希（同 prologue_hash 思路），参与 manifest memory_anchor 键回写。
            if (exp.has("e2_memory_anchor") and root and not replay and i == _anchor_src
                    and not seg_unlink[i]):
                _mem_t = experiments.memory_tokens(
                    int(exp.param("e2_memory_anchor", "记忆帧数", 2)), frames_to_latent_t)
                _mem_v = video_t[:, :, :_mem_t, :, :].float()
                _mem_hash = checkpoint.prompt_hash(
                    f"mem:{_mem_t}:{float(_mem_v.mean()):.4f}:{float(_mem_v.std()):.4f}")
                memory_anchor_rec = checkpoint.save_memory_anchor(root, video_t, _mem_t, _mem_hash)
                report.append(f"段{g + 1} 记忆锚：首段开头 {_mem_t} token 落盘为全局 reference")
            if use_ckpt:
                # 二采在段视频落盘前接管：成功/记录沿用则段视频即高清结果（同名覆盖），
                # 失败才回退基础分辨率保存（fresh 强制重编码，覆盖可能写坏的 mp4）
                # 尾锚/头锚与基础链同规则：尾帧图段=尾帧图（同位置唯一锚不叠加）、
                # 其余段=每段尾帧锚定；中段首帧图=头锚——回放段也可能补渲染，就地重算；
                # 重摇段直接复用采样的 eff 锚（eff_guide/_tail_kf）——二采重采样锚与
                # 基础链采样锚一致，基础/高清接缝行为不漂移
                if _redo_mode is not None:
                    _up_guide_kf, _up_tail_kf = eff_guide, _tail_kf
                else:
                    _up_guide_kf = None if seg_unlink[i] else guide
                    _up_tail_kf = end_frame_latent \
                        if (seg_end_on[i] and end_frame_latent is not None) \
                        else tail_anchor_latent
                hi_ready, hi_tried = _up_hi(g, video_t, audio_t, "prompt", i,
                                            _up_guide_kf, _up_tail_kf, _head_kf,
                                            cur_seed, skip_f, frames.shape[0],
                                            wav, sample_rate)
                if not hi_ready:
                    thumbs.append(checkpoint.save_thumb(root, g, frames[0]))
                    videos.append(checkpoint.save_segment_mp4(root, g, frames, wav, sample_rate,
                                                              fresh=not replay or hi_tried,
                                                              crf=_bcrf, preset=_bpreset, aq_mode=_baq,
                                                              dither=_bdith))
                else:
                    _u_files = checkpoint.upscale_files(g)
                    thumbs.append(_u_files["thumb"])
                    videos.append(_u_files["mp4"])
            else:
                thumbs.append("")
                videos.append("")

            if guide is not None:
                note = (f"guide=上段尾{ctx}帧" if full_bridge else "guide=单帧桥(旧协议降级)") \
                    + ("+音频" if "audio_latent" in guide else "")
            else:
                note = "guide=无（首段）"
            if seg_unlink[i]:
                note = "guide=无（独立镜头断链）"
            if _redo_mode is not None:
                note = f"重摇（{_redo_mode}）：首锚{'上段尾帧桥' if eff_guide is not None else '无'}" \
                       f" · 尾锚{'接下段' if _tail_kf is not None else '无'} · 其余段保留存档"
            origin = "存档载入" if replay else f"采样{latent_t_to_frames(video_t.shape[2])}帧"
            seed_txt = cur_seed if cur_seed is not None else "—"
            report.append(f"段{g + 1}/{total}：{origin} 裁头{skip_f}帧 留{frames.shape[0]}帧({frames.shape[0] / 24:.1f}s)"
                          f" · 种子 {seed_txt}" + ("" if replay else f" · 采样 {_seg_t['sample']:.0f}s") + f" | {note}"
                          + (" · 独立镜头（断链）" if seg_unlink[i] else "")
                          + (" · 首帧图头锚" if _head_kf is not None else "")
                          + (" · 尾帧图尾锚" if (seg_end_on[i] and end_frame_latent is not None) else "")
                          + _soft_note)
            report.append(f"⏱ 段{g + 1}：条件 {_seg_t['cond']:.0f}s · 一采 {_seg_t['sample']:.0f}s"
                          f" · 基础解码 {_seg_t['decode']:.0f}s"
                          + (f" · TE缓存 命中{clip.hits}/未中{clip.misses}" if clip.hits or clip.misses else ""))

            # 生成段输出末端 token 边界（重摇 unlink 段上锚现算对齐用）
            prev_end_t = end_t

            if next_wants_bridge:
                # end_tokens=kept 末端：锚定末端与输出末端重合（回退量已含在 vis_len 里）
                guide = _tail_keyframe(video_t, audio_t, ctx, KEYFRAME_AUDIO_SUPPORTED and full_bridge,
                                       end_tokens=end_t, full_bridge=full_bridge)

            if use_ckpt and not replay:
                # 选择性重做不倒退进度：重摇段完成后 done 保持全链完成数
                #（保留段的 done 记录与新 latent 同为有效进度）
                if g + 1 > done:
                    done = g + 1
                if _redo_mode is not None:
                    # 队列逐段消费（原子落盘：审片中段 break 后剩余队列仍在 manifest，
                    # 前端据此显示「待重摇」徽章与「继续重摇剩余 N 段」入口）
                    redo_queue = [x for x in redo_queue if int(x[0]) != g]
                checkpoint.save_manifest(root, {
                    "schema": checkpoint.SCHEMA, "done": done, "has_prologue": bool(off),
                    "seeds": list(seeds[:done]),
                    "prompt_hashes": full_hashes[:done],
                    "total": total,
                    "prompts": prompt_list[:done],
                    "params": ckpt_params, "seam_refine": seam_refine, "experiments": exp.describe(),
                    "memory_anchor": memory_anchor_rec,
                    "inserts": list(proj_inserts),
                    "title": proj_title, "created_at": proj_created,
                    "updated_at": time.time(), "finals": list(proj_finals),
                    "upscale": proj_upscale,
                    "redo_queue": list(redo_queue),
                    **_ml({"thumbs": thumbs, "videos": videos, "seams": seams,
                           "bridge_scores": bridge_scores, "seam_metrics": seam_metrics_rows,
                           "trims": trims}, done),
                })

            pbar.update(1)

            if review and not replay and item_i + 1 < len(exec_items):
                if _redo_mode is not None:
                    _left = len(redo_queue)
                    report.append(f"重摇：段 {g + 1} 已重做并落盘 → seg_{g:03d}.mp4"
                                  + (f"；剩余 {_left} 段待重摇，满意请重新运行继续重摇下一段"
                                     if _left else "；重摇队列已全部完成")
                                  + "；全部确认后运行一次自动重拼全片（或用合并导出）")
                else:
                    report.append(f"审片：段 {g + 1} 已完成并落盘 → 看项目文件夹里的 seg_{g:03d}.mp4；"
                                  f"满意请直接重新运行继续段 {g + 2}；不满意：改该段提示词后运行（自动从本段重跑），"
                                  f"或设「重跑起始段={g + 1}」+ 换种子重摇")
                break

        # 禁用段（不上链）全局槽位集：成片与二采拼接同口径剔除（执行跳过的段不进片）
        _off_slots = {item_i + off for item_i, it in enumerate(exec_items)
                      if it[0] == "prompt" and seg_disabled[it[1]]}
        # 成片应含段数 = 序章（如有）+ 实际执行段（禁用段不执行不进成片）
        _final_segs = total - len(_off_slots)

        if use_ckpt:
            # 兜底回写：纯回放运行（无新段）也会重解码全部段，把缩略图/分段视频/指标
            # 等增量键补齐——旧版本存档的 manifest 缺这些键时由此自愈，无需重跑整链
            checkpoint.save_manifest(root, {
                "schema": checkpoint.SCHEMA, "done": done, "has_prologue": bool(off),
                "seeds": list(seeds[:done]),
                "prompt_hashes": full_hashes[:done],
                "total": total,
                "prompts": prompt_list[:done],
                "params": ckpt_params, "seam_refine": seam_refine, "experiments": exp.describe(),
                "inserts": list(proj_inserts),
                "title": proj_title, "created_at": proj_created,
                "updated_at": time.time(), "finals": list(proj_finals),
                "upscale": proj_upscale,
                "redo_queue": list(redo_queue),
                **_ml({"thumbs": thumbs, "videos": videos, "seams": seams,
                       "bridge_scores": bridge_scores, "seam_metrics": seam_metrics_rows,
                       "trims": trims}, done),
            })
            if review:
                if len(seg_frames) == _final_segs:
                    report.append("审片：本链已全部完成")
                if reroll > 0:
                    report.append(f"注意：「重跑起始段」={reroll} 已生效，确认无误后请改回 0")
            report.append(f"项目文件夹（视频/提示词/latent 全在此，删项目=删整个文件夹）：{root}")
            checkpoint.save_state({"dir": os.path.basename(root), "total": total, "done": done,
                                   "review": bool(review), "reroll": reroll,
                                   "report": "\n".join(report), "updated_at": time.time()})

        if all_frames:
            images = torch.cat(all_frames, dim=0)
        else:
            # 全禁用链：无帧可拼——单帧黑场占位保下游节点不崩，报告说明（无成片内容）
            images = torch.zeros(1, height, width, 3)
            if all_wav is None:
                all_wav = torch.zeros(1, 2, 24000)
                sample_rate = 24000
            report.append("注意：所有段均已禁用（不上链）——输出为单帧黑场占位，无成片内容")
        # 自动成片：完整链（或审片已确认部分）编码成片，直接落项目文件夹。
        # 二采开启且全链高清记录齐时优先流式拼接高清分段（单份产物，成片=二采结果）；
        # 链未完成/记录不齐/尺寸混排/拼接失败回退内存帧编码基础分辨率成片。
        # 禁用段两路同口径剔除：高清拼接传 skip_slots，内存帧本就不含禁用段
        if autosave_final:
            if not (up_cfg and upscale.try_final(root, up_cfg, report, skip_slots=_off_slots)):
                if _redo_started and len(seg_frames) < _final_segs:
                    # 重摇审片中段 break：内存帧只有已处理段，编码会产出半截成片污染
                    # finals；重摇段 mp4 已覆盖、保留段沿用，全部分段都在盘——下次
                    # 全链运行（redo 队列清空）自动重拼全片，或用合并导出即时取片
                    report.append("重摇：本次为部分重做（审片中段），跳过自动成片——"
                                  "剩余段全部确认后运行一次自动重拼全片，或用合并导出")
                else:
                    final_name, enc_err = _autosave_final(root, images, all_wav, sample_rate,
                                                   crf=_bcrf, preset=_bpreset, aq_mode=_baq,
                                                   dither=_bdith)
                    if final_name:
                        proj_finals.append(os.path.basename(final_name))
                        mf = checkpoint.load_manifest(root) or {}
                        mf["finals"] = list(proj_finals)
                        checkpoint.save_manifest(root, mf)
                        report.append(f"自动保存：分段与成片已就绪 → {root}"
                                      f"（seg_XXX.mp4 逐段，{os.path.basename(final_name)} 完整成片）")
                    else:
                        err_detail = f"（{enc_err}）" if enc_err else ""
                        report.append(f"自动保存：成片编码失败{err_detail}，分段 mp4 不受影响")
            # 状态指针重写：带上自动保存/二采成片的报告行（此前的 save_state 在其之前）
            checkpoint.save_state({"dir": os.path.basename(root), "total": total, "done": done,
                                   "review": bool(review), "reroll": reroll,
                                   "report": "\n".join(report), "updated_at": time.time()})
        # 链路总结：接缝指标一览（seams[i] = [帧差, 响度dB] 或 None）
        measured = [(g, s[0], s[1]) for g, s in enumerate(seams) if s]
        if measured:
            avg_d = sum(m[1] for m in measured) / len(measured)
            worst = max(measured, key=lambda m: m[1])
            line = f"链路完成：{total} 段 · 接缝平均帧差 {avg_d:.3f} · 最差接缝 段{worst[0] + 1}（{worst[1]:.3f}"
            if worst[2] is not None:
                line += f"，{worst[2]:+.1f} dB"
            report.append(line + "）")
        z_rows = [z for z in seam_metrics_rows if z]
        if z_rows and any(v is not None for z in z_rows for v in
                          (z.get("flow_z"), z.get("lpips_z"), z.get("emb_z"), z.get("cam_z"))):
            fz = [z["flow_z"] for z in z_rows if z.get("flow_z") is not None]
            if fz:
                report.append(f"接缝基准汇总：光流 z 均值 {sum(fz) / len(fz):+.1f} · 最差 {max(fz):+.1f}σ"
                              f"（|z|<2 合格；明细 manifest.seam_metrics）")
        drop_total = sum(t for t in trims if t)
        if drop_total > 0:
            chain_frames = sum(f.shape[0] for f in all_frames)
            share = drop_total / max(1, chain_frames + drop_total) * 100.0
            report.append(f"累计丢弃 {drop_total} 帧（{drop_total / 24:.1f}s，约占计划时长 {share:.1f}%）"
                          f"——含门控回退/网格对齐，逐段明细见 manifest trims")
        return io.NodeOutput(
            images,
            {"waveform": all_wav, "sample_rate": sample_rate},
            24,
            "\n".join(report),
            seg_frames,
            seg_wavs,
        )

    @classmethod
    def IS_CHANGED(cls, 审片模式="关闭", 自动存档="关闭", 自动保存="分段", 自动成片="开启", **kwargs):
        if 审片模式 == "逐段确认" or 自动存档 in ("自动存档", "自动续跑") \
                or 自动保存 in ("分段", "分段+成片"):   # 旧三态「分段+成片」也强制重跑（读档兼容）
            return float("nan")   # 存档/审片/自动保存激活时输入不变也强制真正执行（重读最新 manifest）
        return ""

    @staticmethod
    def _apply_guide(cond, guide, sampled_fc, tail_kf_latent=None, e1_windows=None,
                     memory_kfs=None, head_kf_latent=None, mid_kfs=None):
        """把 keyframe 注入 conditioning（官方 minimax_keyframes 协议）。

        guide: 首帧引导桥 keyframe（上段尾 latent 切片），None=首段无桥。
        tail_kf_latent: 尾帧身份锚定的 VAE latent，注入到 resolved_frame_index=
        sampled_fc-1 位置——与首帧桥形成「隧道」，模型去噪全程被首尾双锚点约束。
        合并 cond 里已有的 keyframes（如 i2v 首帧图片的 first_frame keyframe）。
        e1_windows: 实验 E1 展开后的引导桥 keyframe 列表（滑窗/重叠）；None=现状单桥。
        memory_kfs: 实验 E2 全局记忆锚 keyframe 列表（首段 latent 裁的 reference，
        注入位置由调用方算好）；None=不注入。与 guide/尾锚叠加、互不干扰。
        head_kf_latent: 段级首帧图引用的头锚 latent，注入段头（resolved_frame_index
        =0，与段首桥同位叠加）；None=不注入。
        mid_kfs: 段中锚点 keyframe 列表（多锚点实验，官方 AddGuide 语义——同一
        cond 里可挂多条 keyframe，位置由调用方算好并过 guides 校验）；None=不注入。
        """
        existing = cond[0][1].get("minimax_keyframes", [])
        keyframes = list(existing)
        if guide is not None:
            if e1_windows:
                keyframes.extend(e1_windows)
            else:
                keyframes.append(guide)
        if head_kf_latent is not None:
            keyframes.append({"resolved_frame_index": 0, "latent": head_kf_latent})
        if memory_kfs:
            keyframes.extend(memory_kfs)
        if mid_kfs:
            keyframes.extend(mid_kfs)
        if tail_kf_latent is not None:
            keyframes.append({"resolved_frame_index": sampled_fc - 1,
                              "latent": tail_kf_latent})
        if not keyframes:
            return cond
        return node_helpers.conditioning_set_values(cond, {
            "minimax_keyframes": keyframes,
            "minimax_frame_count": sampled_fc,
        })

    @staticmethod
    def _e4_transition_resample(模型, clip, cond, negative, base_latent, video_t, audio_t,
                                 采样器, 调度器, total_steps, CFG, seed,
                                 seg_idx, skip_f, gi, next_wants_bridge,
                                 _decode_crop, prev_tail_frame, qc,
                                 transition_frames=17, resample_steps=20,
                                 dual_anchor_strength=1.0,
                                 video_vae=None, audio_vae=None,
                                 aug=0.0, fade_ratio=0.0,
                                 eff_guide=None, tail_anchor_latent=None,
                                 cond_audio_rows_guard=None,
                                 cond_video_rows_guard=None,
                                 step_cond_noise_guard=None,
                                 _apply_anchor_noise=None,
                                 latent_t_to_frames=None,
                                 frames_to_latent_t=None):
        """实验 E4：双向过渡重生成（定向重采样，只改缝区 transition 窗）。

        对超阈值缝区构造 past|transition|future 三窗，用双锚（缝前锚+缝后锚）+
        缝区独占噪声对 transition 窗做低步数重采样。只替换 transition 窗的 latent，
        前后帧保持不动。

        返回 (frames, wav, sample_rate, video_t, audio_t, end_t, vis_len,
              seg_bridge_score, gate_lines, new_d, lines)；失败返回 None。
        失败原因：过渡窗过小、网格对齐失败、past/future 不足等（调用方跳过 E4）。
        """
        import torch

        lines = []
        sampled_fc = latent_t_to_frames(video_t.shape[2])
        # 三窗划分：seam_index = skip_f（段首裁掉的帧数 = 接缝位置在本段帧坐标系）
        wins = transition.transition_windows(sampled_fc, skip_f, transition_frames)
        if wins is None:
            return None
        ps, pe, ts, te, fs, fe = wins
        # 像素帧 -> token 索引（video_t 的 temporal 维度）
        ps_t = frames_to_latent_t(ps, up=False)
        pe_t = frames_to_latent_t(pe, up=True)
        ts_t = frames_to_latent_t(ts, up=False)
        te_t = frames_to_latent_t(te, up=True)
        fs_t = frames_to_latent_t(fs, up=False)
        fe_t = frames_to_latent_t(fe, up=True)
        T = video_t.shape[2]
        if ts_t >= te_t or te_t > T or pe_t < ps_t:
            return None

        # 切出三窗 latent
        past_lat = video_t[:, :, ps_t:pe_t, :, :] if pe_t > ps_t else None
        trans_lat = video_t[:, :, ts_t:te_t, :, :]
        fut_lat = video_t[:, :, fs_t:fe_t, :, :] if fe_t > fs_t else None
        trans_frames = latent_t_to_frames(te_t - ts_t)
        if trans_frames < 5:
            return None

        # 构造双锚 cond：缝前锚=past 末帧（或 eff_guide 本身），缝后锚=future 首帧
        # 锚定强度通过 aug 控制（dual_anchor_strength 越大锚越硬 = aug 越小）
        trans_cond = node_helpers.conditioning_set_values(
            [c[:] for c in cond], {})   # 深拷贝一份 cond，避免污染原 cond
        dual_kfs = transition.dual_anchor_keyframes(
            past_lat, fut_lat, ts, te, latent_t_to_frames, frames_to_latent_t)
        if not dual_kfs:
            return None
        # 把双锚 keyframe 的 frame_index 映射到 transition 局部坐标系
        local_kfs = []
        for kf in dual_kfs:
            kf_copy = dict(kf)
            # resolved_frame_index 是在 transition 窗内的局部帧索引
            local_idx = max(0, min(trans_frames - 1, int(kf["resolved_frame_index"]) - ts))
            kf_copy["resolved_frame_index"] = local_idx
            local_kfs.append(kf_copy)
        trans_cond = node_helpers.conditioning_set_values(trans_cond, {
            "minimax_keyframes": local_kfs,
            "minimax_frame_count": trans_frames,
        })

        # 双锚强度：用 aug 控制（值越小锚越硬），dual_anchor_strength 越大 → aug 越小
        if dual_anchor_strength > 0 and _apply_anchor_noise is not None:
            # strength=1.0 → aug 不变；strength>1 → 锚更硬（aug 减小）
            e4_aug = max(0.0, aug / max(0.01, dual_anchor_strength))
            trans_cond = _apply_anchor_noise(trans_cond, e4_aug)

        # 缝区独占噪声：transition 窗用全新随机噪声初始化（与原 latent 完全独立）
        # 形状对齐 transition 窗的 latent 形状 [B,C,T_trans,H,W]
        noise_shape = list(trans_lat.shape)
        trans_noise = torch.randn(noise_shape, device=video_t.device, dtype=video_t.dtype)
        trans_input = {"samples": trans_noise.clone()}

        # 过渡重采样（低步数）
        restore_rows = cond_audio_rows_guard(模型.model.diffusion_model) if cond_audio_rows_guard else None
        # 双锚 keyframe 与参考图共存时同样会撞 0.33.x 的 cond_video_latents 覆盖
        restore_video_rows = cond_video_rows_guard(模型.model.diffusion_model) \
            if cond_video_rows_guard else None
        restore_step = None
        if fade_ratio > 0 and step_cond_noise_guard is not None and dual_anchor_strength > 0:
            e4_aug_start = 1.0 - (aug / max(0.01, dual_anchor_strength)) if aug > 0 else 0.999
            restore_step = step_cond_noise_guard(
                模型.model.diffusion_model, e4_aug_start, 0.0, fade_ratio)
        try:
            sampled = nodes.common_ksampler(
                模型, seed, resample_steps, CFG,
                采样器, 调度器, trans_cond, negative, trans_input, denoise=1.0)[0]
        finally:
            if restore_rows:
                restore_rows()
            if restore_video_rows:
                restore_video_rows()
            if restore_step:
                restore_step()

        # 把重采样后的 transition latent 拼回原 video_t
        new_video_t = video_t.clone()
        new_trans = sampled["samples"]
        # 确保 token 数对齐（采样器可能调整尺寸，取较小值）
        nt = min(new_trans.shape[2], te_t - ts_t)
        new_video_t[:, :, ts_t:ts_t + nt, :, :] = new_trans[:, :, :nt, :, :]

        # 重新解码并走桥帧门控（门控结果可能改变 vis_len/end_t）
        frames, wav, sr, end_t, vis_len, seg_bridge_score, gate_lines = _decode_crop(
            seg_idx, new_video_t, audio_t, skip_f, gi=gi, next_bridge=next_wants_bridge)
        new_d = None
        if prev_tail_frame is not None:
            new_d = qc.seam_metrics(prev_tail_frame, frames[0])[0]
        lines.append(
            f"段{gi + 1} E4：过渡窗 {trans_frames} 帧（token {ts_t}-{te_t}），"
            f"重采样 {resample_steps} 步，缝差 {new_d:.3f}" if new_d is not None
            else f"段{gi + 1} E4：过渡窗 {trans_frames} 帧，重采样 {resample_steps} 步")
        return (frames, wav, sr, new_video_t, audio_t, end_t, vis_len,
                seg_bridge_score, gate_lines, new_d, lines)
