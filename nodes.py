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

from . import anchors
from . import checkpoint
from . import prompts as _P
from .prompts import L2VA_HEAD, FL2VA_HEAD
from . import metrics
from . import guides
from . import grid
from . import perf
from . import semantic_bridge
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


# 自定义 sigma 表（少步蒸馏 LoRA：HyperFlow 8 步 / H3 Turbo）走 sigmas_adapter：
# 单独成模块一是可脱离 ComfyUI 单测，二是这部分逻辑与续拍/接缝无关。
try:
    from . import sigmas_adapter as _sigmas_adapter
except ImportError:      # 兜底：作为顶层脚本被直接 import 时
    import sigmas_adapter as _sigmas_adapter


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


def _tail_keyframe(video_t, audio_t, ctx_frames, with_audio, end_tokens=None,
                   full_bridge=True, with_video=True):
    """上段尾部 ctx 帧 latent 直切为 keyframe；end_tokens 为输出末端 token 边界。

    end_tokens=None（序章等外部源）取原始尾部；生成段必须传 kept 末端对齐值，
    保证锚定末端 == 输出末端——否则下段续拍点落在本段从未输出的网格填充帧上，
    每个接缝跳过最多 16 帧内容（观感即"接缝跳变"）。
    旧版 ComfyUI keyframe 协议只收单帧 latent：full_bridge=False 时只钉
    尾部最后 1 个 token（承载上段末尾画面），且不附音频。

    with_video=False：anchor 的「仅音频」取用态——只挂 audio_latent 分支，
    不放 latent（官方 keyframe 按键预留行，缺分支即不钉）。此时若音频也不可用
    则整条 keyframe 没有任何分支，返回 None 让调用方跳过。
    """
    vt = video_latent_t(ctx_frames) if full_bridge else 1
    kf = {"resolved_frame_index": 0}
    if with_video:
        # 仅音频锚（src.kind="audio" 或分支=仅音频）没有视频 latent，走不到这里
        end = video_t.shape[2] if end_tokens is None else min(end_tokens, video_t.shape[2])
        kf["latent"] = video_t[:, :, end - vt:end, :, :].clone()
    if with_audio and full_bridge and audio_t is not None:
        at = audio_tokens_for_frames(ctx_frames)
        if with_video:
            aend = min(audio_tokens_for_frames(latent_t_to_frames(end)), audio_t.shape[-1])
        else:
            # 无视频分支（纯音频锚）：音频从源尾部取窗，不依赖视频末端对齐
            aend = audio_t.shape[-1]
        if 0 < at <= aend:
            kf["audio_latent"] = audio_t[..., aend - at:aend].clone()
    return kf if len(kf) > 1 else None


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


def _clean_frame_file(rel):
    """段级首尾帧参考图的文件名归一：项目内相对路径（最多两级，拒穿越）。"""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p and p != "."]
    if not parts or len(parts) > 2 or ".." in parts:
        return ""
    if any((":" in p) or p.startswith(".") for p in parts):
        return ""
    return "/".join(parts)


def _parse_frame_imgs(segments, n):
    """段级首尾帧参考图：segments[i]["frame_img"] = {"first": 文件, "end": 文件}。

    与「素材库打标」的旧链路（chain_head_label / chain_tail_label，链级）区分：
    这是**每段各自指定**的首/尾帧参考图（提示词框资产引用栏的「首帧图/尾帧图」
    按钮选出来的项目内图片），首段的首帧图同时充当 i2v 起手帧、末段的尾帧图
    充当 FL2VA 剧情终点锚；中段则作为段头/段尾身份锚注入。
    """
    out = []
    for i in range(n):
        seg = segments[i] if i < len(segments) and isinstance(segments[i], dict) else {}
        raw = seg.get("frame_img")
        raw = raw if isinstance(raw, dict) else {}
        out.append((_clean_frame_file(raw.get("first")), _clean_frame_file(raw.get("end"))))
    return out


_REDO_MODES = ("双锚", "仅锚上段", "仅锚下段", "无锚")


def _parse_redo_segs(ds, done, exec_kinds, off, disabled=None):
    """导演台重摇标记 ds.redo_segs -> 合法 (slot, mode) 列表（去重，按提交序）。

    slot=全局槽位 0-based（含序章位，与二采 include 同口径）；合法性：
    已完成（slot < done，未完成段本来就要生成）、非序章（slot >= off）、
    提示词段（序章另有专用操作，不可重摇）、mode 合法、非禁用段
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
    """完整链（或审片已确认部分）PyAV 编码成片到项目文件夹 finals/，成功返回路径。

    失败时返回 (None, error_msg) 供调用方写入报告；成功返回 (path, None)。
    """
    from . import media
    try:
        from . import checkpoint as _ckpt
    except ImportError:
        import checkpoint as _ckpt
    try:
        path = os.path.join(_ckpt.finals_dir(root), f"final_{time.strftime('%Y%m%d_%H%M%S')}.mp4")
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


def _vram_used_gb():
    """当前**设备级**显存占用（GB）；量不到 → None。

    用 `mem_get_info`（total − free）而不是 `torch.cuda.memory_allocated()`：
    后者只统计 torch 的 caching allocator，量不到 aimdo / DynamicVRAM 的权重池
    ——在 24GB 卡跑 32GB 模型时它会报 0.43GB 这种假象（既有口径，见 perf 模块）。
    """
    try:
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            return float(total - free) / (1024.0 ** 3)
    except Exception:
        pass
    return None


def _vram_oom_cleanup():
    """OOM 自救的腾挪动作：卸载驻留模型 → 回收 Python 残留 → 归还缓存块。

    ⚠ 顺序不能反：`gc.collect()` 必须在 `empty_cache()` **之前**——引用还挂着
    的 CUDA 块，empty_cache 收不回去（这正是「多段链后段比首段更容易 OOM」
    的根因之一）。与 upscale 那套腾挪点同口径。
    """
    try:
        import comfy.model_management as _mm

        _mm.unload_all_models()
    except Exception:
        pass
    gc.collect()
    try:
        torch.cuda.empty_cache()
    except Exception:
        pass


def _frames_to_uint8(frames_f):
    """[N,H,W,3] float 0-1 -> uint8（帧存内存 ×¼）。"""
    return frames_f.clamp(0.0, 1.0).mul(255.0).round().to(torch.uint8)


def _build_chain_images(parts, height, width, uint8=False):
    """全链帧分片 -> 单张 [N,H,W,3] float32 IMAGE（**预分配 + 逐段填充**）。

    为什么不用 `torch.cat`：cat 会新开一整块连续缓冲，而 `parts` 里的分片还
    全被引用着 → 峰值 = **2× 全链帧**（D3 实测：8 段 ×107 帧 1312×736 时
    9.9GB 变 19.8GB，再叠 31.7GB 常驻权重就是悬崖）。

    预分配输出 + 逐段 `copy_` 的峰值是「输出 + 一个分片」。单看这一改只是
    把 2× 变成 (1 + 1段/全链)×，真正的量级差在 `uint8` 分片：分片只占 ¼，
    于是峰值 ≈ **1.25×** 而不是 2×。
    """
    n = sum(int(f.shape[0]) for f in parts)
    if n <= 0 or not parts:
        return torch.zeros(1, height, width, 3)
    h, w = int(parts[0].shape[1]), int(parts[0].shape[2])
    out = torch.empty((n, h, w, 3), dtype=torch.float32)
    i = 0
    for f in parts:
        k = int(f.shape[0])
        if not k:
            continue
        if uint8:
            src = f.to(torch.float32)
            src.div_(255.0)
        else:
            src = f          # 已 float32 且 0-1：直接 copy_，不做原地除法
        out[i:i + k].copy_(src)
        i += k
    return out


def _stream_final_basic(root, videos, skip_slots, crf=20, preset="veryfast",
                        aq_mode=None, dither=False):
    """基础分辨率成片：直接流式拼接分段 mp4 —— **全程不碰内存帧**。

    返回 (path|None, error|None)。这是 D3 的正解：NLE 从不把整条时间线的
    全分辨率 RGB 帧同时放在内存里，它们用「渲染结果落盘 + 流式拼接」。
    `media.concat_av_mp4` 已经是流式的（帧不进 Python 累积，峰值≈单帧），
    本函数只负责两个前置判据：分段齐不齐、文件在不在盘。

    ⚠ 别指望 GPU 编码救内存：帧最终仍要落 CPU 侧进编码器。实测 557 帧编码
    只要 13 秒——**编码从来不是瓶颈，内存才是**。
    """
    from . import media
    try:
        from . import checkpoint as _ckpt
    except ImportError:
        import checkpoint as _ckpt
    skip = {int(s) for s in (skip_slots or [])}
    srcs = []
    for i, rel in enumerate(videos or []):
        if i in skip:
            continue
        if not rel:
            return None, f"段 {i + 1} 无分段 mp4（未开自动存档？）"
        p = _ckpt.resolve_project_file(root, rel)
        if not os.path.isfile(p):
            return None, f"段 {i + 1} 的分段 mp4 缺失（{rel}）"
        srcs.append(p)
    if not srcs:
        return None, "没有可拼接的分段 mp4"
    out_name = f"final_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    out_path = os.path.join(_ckpt.finals_dir(root), out_name)
    if media.concat_av_mp4(srcs, out_path, crf=crf, preset=preset,
                           aq_mode=aq_mode, dither=dither):
        return out_path, None
    return None, media.last_error


def _encode_audio_latent(audio_vae, audio, tokens):
    """AUDIO dict -> [1,32,2,T] latent，裁到与画面等长的 token 数。

    仿官方 _encode_ref_audio 语义：重采样到音频 VAE 采样率后整段编码。
    输入归一到 [B,C,T]（LoadAudio 口径）：decode_av 给的是 [C,T]，
    单声道文件可能是 [T]；缺 batch 维时 [:1] 会切到声道导致 VAE 内
    tuple index out of range——此处先补齐（已是 3D 则零改动）。
    torchaudio 延迟导入（ComfyUI 自带，无 ComfyUI 的结构单测不触达）。
    """
    import torchaudio

    waveform = audio["waveform"]
    while waveform.dim() < 3:
        waveform = waveform.unsqueeze(0)
    sr = int(audio.get("sample_rate") or 0)
    vae_sr = getattr(audio_vae, "audio_sample_rate", 32000)
    if sr and sr != vae_sr:
        waveform = torchaudio.functional.resample(waveform, sr, vae_sr)
    z = audio_vae.encode(waveform[:1].movedim(1, -1))
    return z[..., :tokens].clone()


# P4d：_autogrow_items 已删除（画布 autogrow 入口全部下线，无调用方）。


def _resolve_media_path(filename):
    """素材文件 -> 绝对路径：assets/... 走项目文件夹（入库拷贝），其余走 input 目录。

    project_root_fn 为可选回调 () -> 项目根目录绝对路径（无项目时返回 None，
    此时 assets/... 解析失败并报清晰错误）。
    """
    norm = str(filename or "").strip().replace("\\", "/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if parts and parts[0] == "assets":
        return ("project", "/".join(parts))
    return ("input", norm)


def _decode_image_file(abs_path):
    """绝对路径图片 -> IMAGE 张量（解码部分，与 _load_input_image 同格式）。"""
    from PIL import Image, ImageOps
    import numpy as np
    img = node_helpers.pillow(Image.open, abs_path)
    img = node_helpers.pillow(ImageOps.exif_transpose, img)
    if img.mode != 'RGB':
        img = img.convert('RGB')
    return torch.from_numpy(np.array(img).astype(np.float32) / 255.0).unsqueeze(0)


def _load_input_image(filename, _project_root=None):
    """图片 -> IMAGE 张量（与 LoadImage 节点同格式：[1,H,W,C] float32 0-1）。

    filename 为 assets/... 时从项目文件夹取（_project_root 必给），否则走 input 目录。
    P2：store 三级寻址命中的全局库文件由调用方经 _decode_image_file 直接解码，
    本函数兼容路径不变。
    """
    scope, norm = _resolve_media_path(filename)
    if scope == "project":
        if not _project_root:
            raise ValueError(f"素材「{filename}」在项目资产库内，但当前无法定位项目文件夹："
                             "请开启自动保存/审片/自动存档之一，或在「存档目录」填写项目名")
        image_path = os.path.join(_project_root, norm)
    else:
        image_path = folder_paths.get_annotated_filepath(norm)
    return _decode_image_file(image_path)


def _decode_video_file(abs_path, filename=""):
    """绝对路径视频 -> (帧张量, 音轨)，与 _load_input_video 同格式（含静音兜底）。"""
    from comfy_api.latest import InputImpl
    comps = InputImpl.VideoFromFile(abs_path).get_components()
    if comps.images is None or not getattr(comps.images, "shape", None):
        raise ValueError(f"参考视频「{filename or abs_path}」解不出画面帧：请确认是可解码的 24fps 视频（2-15 秒）")
    if comps.audio is None:
        import torch as _t
        comps.audio = {"waveform": _t.zeros(1, 1, 0), "sample_rate": 24000}
    return comps.images, comps.audio


def _load_input_video(filename, _project_root=None):
    """视频 -> (帧张量, 音轨)：与 LoadVideo+GetVideoComponents 画布链同格式。

    音轨与帧同源自同一文件，即「参考视频音轨」自动配对，无需单独上传。
    assets/... 走项目文件夹（需 _project_root），其余走 input 目录。
    """
    scope, norm = _resolve_media_path(filename)
    if scope == "project":
        if not _project_root:
            raise ValueError(f"素材「{filename}」在项目资产库内，但当前无法定位项目文件夹："
                             "请开启自动保存/审片/自动存档之一，或在「存档目录」填写项目名")
        video_path = os.path.join(_project_root, norm)
    else:
        video_path = folder_paths.get_annotated_filepath(norm)
    return _decode_video_file(video_path, filename)


def _decode_audio_file(abs_path):
    """绝对路径音频 -> AUDIO dict，与 _load_input_audio 同格式。"""
    from comfy_extras.nodes_audio import load
    waveform, sample_rate = load(abs_path)
    return {"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate}


def _load_input_audio(filename, _project_root=None):
    """音频 -> AUDIO dict（与 LoadAudio 节点输出同格式）。assets/... 走项目文件夹。"""
    scope, norm = _resolve_media_path(filename)
    if scope == "project":
        if not _project_root:
            raise ValueError(f"素材「{filename}」在项目资产库内，但当前无法定位项目文件夹："
                             "请开启自动保存/审片/自动存档之一，或在「存档目录」填写项目名")
        audio_path = os.path.join(_project_root, norm)
    else:
        audio_path = folder_paths.get_annotated_filepath(norm)
    return _decode_audio_file(audio_path)


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
_REF_BRACKET = re.compile(r"\[\[([^\[\]]{1,24})\]\]")
# 负向后顾：`@` 前不能是字母数字下划线，免得把 a@b.com 里的 b 当成素材标签
_REF_AT = re.compile(
    r"(?<![0-9A-Za-z_])@([^\s@\[\]{}<>()（）,，.。;；:：!！?？\"'`|/\\]{1,24})")


def _find_refs(text, labels=None):
    """提示词里引用的素材标签：新写法 `@标签` + 旧写法 `[[标签]]`（老存档还能跑）。

    **池标签（labels）优先做最长前缀匹配**，与前端 `h3_director.refTokens`、
    `library.compute_refs` 同口径。理由：`@引用` 没有天然终止符 —— 中文句子里
    `@图片让这张图动起来` 不打空格，纯正则会把后文一起吃进标签
    （`微信图片_20260730155838_638` 后面跟个「让」就变成 24 字的假标签，还正好
    顶到字符上限）→ 校验时误报"未知素材标签"。池里没有的才退回正则，
    `@2x`、邮箱之类普通文本照旧不被误伤。
    """
    s = str(text or "")
    out = [m.group(1) for m in _REF_BRACKET.finditer(s)]
    labs = sorted({str(l) for l in (labels or ()) if l}, key=len, reverse=True)
    i = 0
    while i < len(s):
        m = _REF_AT.match(s, i) if s[i] == "@" else None
        if m is None:
            i += 1
            continue
        hit = next((l for l in labs if s.startswith(l, i + 1)), None)
        if hit is not None:              # 池内最长优先：标签到哪儿为止由池说了算
            out.append(hit)
            i += len(hit) + 1
        else:
            out.append(m.group(1))
            i = m.end()
    return out


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
    """[(kind, 标签)] -> 按类别独立编号的 {标签: token}（图 <Picture k> / 视 <Video k> / 音 <Audio j>）。

    同一标签重复出现（一段内引用多次）**只编号一次**：编号是素材的身份，不是
    出现次数；重复引用靠正文里多写几次 @标签（编译后同一个 <Picture k> 出现多次）。
    """
    counters = {"image": 0, "video": 0, "audio": 0}
    mapping = {}
    for kind, lbl in label_order:
        if lbl in mapping:
            continue
        counters[kind] = counters.get(kind, 0) + 1
        token = {"image": "<Picture {}>", "video": "<Video {}>", "audio": "<Audio {}>"}.get(kind)
        if token is None:
            raise ValueError(f"未知素材类别「{kind}」：必须是 image / video / audio")
        mapping[lbl] = token.format(counters[kind])
    return mapping


def _apply_label_tokens(prompt, label_order):
    """把提示词里的 `@标签` 替换成该段压实编号后的 <Picture k>/<Video k>/<Audio j>。

    label_order 为该段按序引用的 (kind, 标签)（或旧格式纯标签=image）；各类别独立从 1
    编号；长标签先替换，防「角色1」吃掉「角色10」前缀。
    旧写法 `[[标签]]` 一并认（老项目存档还能跑）；`[[..]]` 是引用专用写法，
    未知即报错；`@` 太常见（邮箱、@2x 之类），未知的不拦，避免误伤普通文本。
    """
    mapping = _kind_tokens(_normalize_order(label_order))
    out = prompt
    for lbl in sorted(mapping, key=len, reverse=True):
        out = out.replace(f"@{lbl}", mapping[lbl])
        out = out.replace(f"[[{lbl}]]", mapping[lbl])
    unknown = _REF_BRACKET.findall(out)
    if unknown:
        raise ValueError(f"提示词引用了未知素材标签「{unknown[0].strip()}」：可用标签 {list(mapping)}")
    return out


def _reference_tags_minimal(label_order):
    """段首最小引用声明：每资源一行**官方 subject_definitions 句式**，不写散文。

    官方 skill 的引用声明是「<Picture 1> is ...」这种定义句（`<Picture N>` 自己
    就是主语，后接它是什么 / 扮演什么角色），不是 `token = 别名` 的赋值表 ——
    赋值表是自造格式，模型只能当普通文本读。这里按官方句式补最小声明：
    `<Picture 1> is the reference image "女主".`（类 noun 由 token 类别决定）。

    H3 模型硬约束：软引用必须在文本里出现 <Picture k> 等 tag，模型才会真正
    使用对应素材（官方模板亦然）。勾选了引用但正文没写 tag 时，只补这几行声明、
    不再自动生成英文长文；写了 tag 的段保持原样直通。
    无 ComfyUI 可单测（纯函数，仅依赖 _normalize_order/_kind_tokens）。
    """
    # 官方句式里 <Picture>/<Video>/<Audio> 各有一个固定的英文名词
    noun = {"Picture": "reference image", "Video": "reference video",
            "Audio": "reference audio"}
    mapping = _kind_tokens(_normalize_order(label_order))
    lines = []
    for lbl, tok in mapping.items():
        cls = tok[1:].split(" ")[0] if tok.startswith("<") else ""
        lines.append(f'{tok} is the {noun.get(cls, "reference asset")} "{lbl}".')
    return "\n".join(lines)


def _uncovered_tags(full, mapping):
    """已写 tag 精确差集：mapping 中文本里没有的标签。

    注：token 匹配必须精确（"<Picture 1>" 是 "<Picture 10>" 的子串，
    直接 in 会误判覆盖），故用正则逐个提取已写 tag。纯函数可单测。
    """
    covered = {m.group(0) for m in
               re.finditer(r"<(?:Picture|Video|Audio) \d+>", full or "")}
    return [lbl for lbl in mapping if mapping[lbl] not in covered]


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
                # P4f：模型/文本编码器改为可选——转码专跑最小图（双 VAE + 本节点短路）
                # 不需要它们；缺省不接不报错，生成链照常连线。
                io.Model.Input("模型", optional=True,
                               tooltip="生成用 UNET（转码专跑不需要，可空）"),
                io.Clip.Input("文本编码器", optional=True,
                              tooltip="生成用 CLIP（转码专跑不需要，可空）"),
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
                               advanced=True,
                               tooltip="已废弃占位（旧工作流兼容保留，已折叠隐藏）：链路改由后端按实际引用自动推导——"
                                       "有段引用素材即走 ref conditioning，首帧标注即首段起手，首帧可与引用共存。"
                                       "实际 UNET 由「模型」输入决定；权重混用机械可跑，效果看权重本身。"),
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
                                        "refs（本段引用的素材标签，缺省=只用提示词文本[[标签]]出现的）/ frame_refs（段级首尾帧图引用："
                                       "首帧图/尾帧图，缺省=首段参考首帧、末段参考尾帧；中段勾首帧图=头部身份锚，"
                                       "任意段勾尾帧图=该段尾锚），提示词用 [[标签]] 引用素材，"
                                       "后端按官方 h3-prompt-writing 三字段结构组装（含对白「」→<d> 自动转换）。"
                                       "空值或旧工作流走原有画布输入（同样获得官方结构组装，向后兼容）。"
                                       "只存相对输入文件名，不存媒体内容、密钥或绝对路径。"),
                io.Image.Input("起始视频", optional=True,
                               tooltip="序章：上传视频（≥5 帧、24fps，超长只取前「每段时长」内）编码为第 1 段存入存档，"
                                       "成片以它开头，生成段从其结尾续拍；经一次 VAE 重编码，不能与首帧图同用"),
                io.Audio.Input("起始视频音轨", optional=True,
                               tooltip="序章原声（与起始视频配对，建议同源 LoadVideo 拆出；不接则序章按静音处理）"),
                io.Model.Input("二采模型", optional=True,
                               tooltip="高清精化二采专用 UNET（神经放大 → 高清 latent 低强度重采样）。"
                                       "不接=沿用一采「模型」（旧行为）。只作用于高清精化二采；"
                                       "一采与接缝重摇仍用一采模型。"
                                       "可接不同量化 + 不同 LoRA 的独立链（UNETLoader → LoraLoader → 本槽）；"
                                       "t2v/i2v 用 fl2va、r2v 用 ref2va，别混接。"
                                       "换二采模型不会自动重做已有高清分段——要重做请设「重跑起始段」"),
                # P4d：画布媒体/提示词入口已删除（首帧/尾帧/尾锚图片、提示词组 autogrow）——
                # P4g：连「资产包」输入也一并删除（H3AssetBundle 下线），素材与提示词只走导演台状态。
                # 起始视频（序章）是唯一的画布媒体入口（无导演台等价字段，保留）。
                # 旧工作流残留连线加载时自动忽略。
                # —— 新控件一律加在**本列表末尾**（= widgets_values 末尾）：旧工作流值不足时
                #    按默认值补齐；插进中间会顶掉它之后所有控件的既有取值。
                io.Combo.Input("参考图像尺寸", options=["match", "max"], default="match",
                               tooltip="参考图缩放口径（官方 Reference to Video 同款）："
                                       "match=每张参考图按本次生成画幅的像素面积等比缩小（只缩不放，省显存与时间）；"
                                       "max=走参考管线的 2048 短边，身份保真最好，"
                                       "但参考 token 每步采样都参与，可能慢数倍。"),
                io.Float.Input("响度对齐强度", default=1.0, min=0.0, max=1.0, step=0.1,
                               tooltip="段首响度对齐强度：把本段开头的增益去匹配上一段尾的 RMS"
                                       "（±6dB 钳制 + 1s 渐出；增益不沿链累积，不会越对越响）。"
                                       "1.0=全量对齐（默认）；0=完全不对齐（保留内容本身的响度差）；"
                                       "0.2-0.4 常用于只兜内容本身的残留响度差。"
                                       "段间隔链（勾了「跳过自动引用上段」）本就不做对齐。"),
                # 纯连线槽：没有 widget，不进 widgets_values（不影响控件值布局）。
                io.Sigmas.Input("自定义Sigmas", optional=True,
                                tooltip="可选：接自定义 sigma 表（少步蒸馏 LoRA 用），接了就覆盖「步数」与「调度器」，"
                                        "实际步数 = sigma 点数 - 1。典型用法：ApplyHyperFlow / ManualSigmas / "
                                        "BasicScheduler 的输出接进来（HyperFlow 官方 8 步表 = "
                                        "1.0, 0.931506, 0.839236, 0.703462, 0.5, 0.296538, 0.160764, 0.068494, 0.0）。"
                                        "不接则完全沿用「步数 + 采样器 + 调度器」，与旧行为逐字节一致。"
                                        "注意：只作用于本节点的一采（基础链），二采（潜空间放大高清）仍用自己的参数。"),
            ],
            outputs=[
                io.Image.Output("图像"),
                io.Audio.Output("音频"),
                io.Int.Output("帧率"),
                io.String.Output("报告"),
                # P4d：分段图像/分段音频列表输出已删除（分段落盘由自动保存完成，
                # 在项目文件夹直接看片）。旧工作流残留连线加载时自动忽略。
            ],
        )

    @classmethod
    def _execute_inner(cls, 模型, 文本编码器, 视频VAE, 音频VAE, 宽高比, 百万像素, 宽度, 高度, 每段时长, 引导帧数,
                种子, 步数, CFG, 采样器, 调度器, 起始视频=None, 起始视频音轨=None,
                自动存档="关闭", 存档目录="", 桥帧门控="标注", 清晰度阈值=30.0, 回退上限=34,
                锚定加噪=0.0,
                审片模式="关闭", 自动保存="分段", 自动成片="开启", 重跑起始段=0,
                接缝重摇="自动", 重摇阈值=0.06, 重摇上限=1,
                递减锚定="关闭", 生成模式="文生视频", 导演台状态="",
                二采模型=None, 参考图像尺寸="match", 响度对齐强度=1.0, 自定义Sigmas=None):
        # P4d：画布媒体/提示词/参考入口已从 schema 删除，对应形参一并移除；
        # 起始视频（序章）是唯一的画布媒体入口，保留。
        # 运行期路由兜底：导入期注册因时序失败时，首次执行后前端删除/列表即可用
        try:
            from .routes import ensure_registered
            ensure_registered()
        except Exception:
            pass
        # ---- 导演台状态驱动：有 JSON 状态时优先于画布接线 ----
        ds = _parse_director_state(导演台状态)
        # P4g：画布「资产包」输入已删除（H3AssetBundle 一并下线）——素材唯一来源是
        # ds.ref_assets（导演台前端 poolFromManifest 写入）。
        ds_used = bool(ds)
        # ---- 性能设置（**渲染期**消费的那一批）----
        # 与 ds.perf 分开：性能设置是**机器相关**的（这台卡多大、内存多少），
        # 全局落盘、跨项目共用，不进导演台状态。整链读一次（mtime 缓存），别每段读盘。
        _pf = perf.current_settings()
        # 语义桥（ds.bridge）：逐项目的条件增强，与性能设置分开 —— 那个是机器相关、
        # 全局落盘；这个改的是画出来的东西，必须跟着作品走。
        _bridge_cfg = semantic_bridge.config(ds)
        _oom_autoretry = bool(_pf.get("oom_autoretry", True))
        _act_peak_probe = bool(_pf.get("act_peak_probe", True))
        _frames_uint8 = str(_pf.get("frames_dtype") or "float32").lower() == "uint8"
        _final_mode = str(_pf.get("final_mode") or "auto").lower()
        _act_first = [True]   # 激活峰值只在首段采样时量一次
        # LoRA 权重的那一半账：ComfyUI 的 `model_size()` 把 patches 算进去了，
        # 这里独立再算一遍是为了**交叉验证**（两边对不上说明有补丁没走 patches）。
        try:
            _lf0 = perf.probe_lora_footprint(模型)
            if _lf0["patch_count"]:
                print(f"[H3性能] {perf.lora_account_line(_lf0['lora_weight_gb'], _lf0['patch_count'])}",
                      flush=True)
        except Exception:
            pass
        # ---- 内联转码任务（资产库"转latent"）：ds.transcode_jobs 非空则只跑转码，
        # 不跑链、不耗种子。任务由资产库面板写入 ds，提交即自动排队执行；
        # 队列中断在任务间隙生效（见 run_transcode_job 的 _interrupted）。
        # 完成后前端凭 manifest.latents 证据自动清理已完成任务。
        # P4+：后台表有本项目排队任务时同样进专跑（提交即自动排队执行，前端
        # 提交后自动 queuePrompt，本条件负责接住执行，无需手写 ds 任务）。
        _jobs = [j for j in (ds.get("transcode_jobs") or [])
                 if isinstance(j, dict) and j.get("status", "queued") == "queued"]
        _server_pending = False
        try:
            from . import transcode_queue as _tq_entry
        except ImportError:
            try:
                import transcode_queue as _tq_entry
            except ImportError:
                _tq_entry = None
        if _tq_entry is not None:
            try:
                _hint = str(存档目录 or "").strip()
                _server_pending = bool(_hint and _tq_entry.has_queued(_hint))
            except Exception:
                _server_pending = False
        if _jobs or _server_pending:
            return cls._execute_transcode_only(
                ds, _jobs, 视频VAE, 音频VAE, 存档目录)
        if ds.get("mode"):
            _m = str(ds["mode"])
            if _m not in ("文生视频", "首帧视频", "多参视频"):
                raise ValueError(f"导演台状态中的模式「{_m}」无效：必须是「文生视频」「首帧视频」或「多参视频」")
            生成模式 = _m
        # 提示词：只走导演台 JSON（画布提示词组入口已删除）
        if ds.get("prompts") and isinstance(ds["prompts"], list):
            seg_prompts = [str(p).strip() for p in ds["prompts"] if str(p).strip()]
        else:
            seg_prompts = []
        if not seg_prompts:
            raise ValueError("提示词不能为空：请在导演台填写提示词后重试")

        # 素材池（标签->文件，分 图/视/音 三类）：ref_assets 为 v2 真源；旧 ref_images 视为图片。
        # 资产库无模式门槛：总量不限；每条目可带 roles 标注（首帧图/尾帧图，见下校验）。
        # 文件定位：assets/... 走项目文件夹（入库拷贝），其余走 ComfyUI input 目录（兼容旧链）。
        _ASSET_ROLES = ("首帧图", "尾帧图")
        pool_files = []   # [(kind, label, file, roles)]
        pool_ids = []     # P3：与 pool_files 同下标的 asset_id（""=无，ds 活池自带）
        pool_refs = []    # 与 pool_files 同下标的**引用名**（含格式后缀，如 女主.png）
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
                _roles = [str(r).strip() for r in (item.get("roles") or [])
                          if str(r).strip() in _ASSET_ROLES] if isinstance(item.get("roles"), list) else []
                # 引用名：显式字段 > 落盘文件名 basename > 别名（旧档兜底，无后缀）。
                # 正文里的 `@xxx` 按它解析 —— 用别名解析会把 `.png` 留在正文里。
                _rn = str(item.get("ref_name") or "").strip()
                if not _rn:
                    _rn = str(item["file"]).replace("\\", "/").split("/")[-1].strip()
                pool_files.append((_k, label[:24], str(item["file"]), _roles))
                pool_ids.append(str(item.get("asset_id") or "").strip())
                pool_refs.append(_rn or label[:24])
        elif ds.get("ref_images") and isinstance(ds["ref_images"], list):
            pool_files = [("image", f"图片{i + 1}", str(fn), []) for i, fn in enumerate(ds["ref_images"]) if fn]
            pool_ids = [""] * len(pool_files)
            pool_refs = [str(fn).replace("\\", "/").split("/")[-1] for _, _, fn, _ in pool_files]
        _seen = set()
        for _i, (_k, _lbl, _fn, _rl) in enumerate(pool_files):
            _base, _n = _lbl, 2
            while _lbl in _seen:
                _lbl = f"{_base}{_n}"
                _n += 1
            _seen.add(_lbl)
            pool_files[_i] = (_k, _lbl, _fn, _rl)
        pool_labels = [lbl for _, lbl, _, _ in pool_files]
        pool_kind = {lbl: k for k, lbl, _, _ in pool_files}
        pool_file_of = {lbl: fn for _, lbl, fn, _ in pool_files}
        pool_roles_of = {lbl: list(rl) for _, lbl, _, rl in pool_files}
        # 引用名匹配表：**引用名优先，别名兜底**（旧档正文里写的是 `@别名`）。
        # `_find_refs` 按池内最长前缀匹配，两份名字都给它才能新旧正文都解析得出来。
        ref_names_for_match = [x for x in dict.fromkeys(
            [str(r).strip() for r in pool_refs if str(r).strip()]
            + [str(l).strip() for l in pool_labels if str(l).strip()])]
        # P3：活池 asset_id 随去重后的 label 对齐（去重只改名不换序，下标对齐天然保持）
        pool_id_of = {lbl: aid for lbl, aid in zip(pool_labels, pool_ids) if aid}
        pool_ref_name_of = {lbl: str(rn or "").strip()
                            for lbl, rn in zip(pool_labels, pool_refs)}
        # P2：执行期资产注册表（store 双层）：pool 转 legacy + 项目 asset_links + 全局库。
        # refs 元素兼容四形态：旧 label / alias / asset_id / {asset,use} dict；
        # 校验核走 compile_refs，报错文案映射回旧链逐字格式。best-effort：
        # store 不可用时回落旧 inline 逻辑（行为与旧版一致）。
        def _project_root_early():
            """组装期项目根（仅自定义名；指纹自动命名根此时未知，见下注释）。"""
            try:
                _custom = str(存档目录 or "").strip()
                if _custom:
                    return os.path.join(checkpoint.projects_root(), _custom)
            except Exception:
                pass
            # 已知边界：存档目录为空（指纹自动命名）且项目 manifest 里已有
            # asset_links 时，组装期注册表带不上项目链接，asset_id 引用会报未知；
            # label 引用不受影响（pool 自带）。导演台流程恒写存档目录，不触发。
            return None

        try:
            from . import asset_store as _AS
        except ImportError:
            try:
                import asset_store as _AS
            except ImportError:
                _AS = None
        _asset_reg_cache = {}

        def _asset_registry():
            """(registry, libroot)：按当前项目根缓存；三源汇合，永不抛错。"""
            _proot = _project_root_early()
            if _proot in _asset_reg_cache:
                return _asset_reg_cache[_proot]
            _links, _lib, _libroot = [], [], None
            if _AS is not None:
                # P3：ds 活池自带的 asset_id 优先（未保存的新链接也能执行），
                # 再叠 manifest 已存链接（旧 label 命名空间同口径）。
                try:
                    for _lbl, _aid in pool_id_of.items():
                        _links.append({"asset_id": _aid, "alias": _lbl,
                                       "kind": pool_kind.get(_lbl, "image"),
                                       "ref_name": pool_ref_name_of.get(_lbl, "")})
                except Exception:
                    pass
                if _proot:
                    try:
                        _mf = checkpoint.load_manifest(_proot)
                        if isinstance(_mf, dict) and isinstance(_mf.get("asset_links"), list):
                            _links.extend(_mf["asset_links"])
                    except Exception:
                        pass
                try:
                    _libroot = _AS.try_library_root()
                    if _libroot:
                        _lib = _AS.load_library(_libroot).get("assets") or []
                except Exception:
                    _lib, _libroot = [], None
                try:
                    _reg = _AS.build_execution_registry(
                        [(k, lbl, fn) for k, lbl, fn, _r in pool_files], _links, _lib)
                except Exception:
                    _reg = {"by_id": {}, "by_alias": {}, "warnings": {}}
            else:
                _reg = {"by_id": {}, "by_alias": {}, "warnings": {}}
            _asset_reg_cache[_proot] = (_reg, _libroot)
            return _reg, _libroot

        def _store_short(lbl):
            """报告用来源显示：pool 内走文件名，store 解析走全局库名，否则画布。"""
            if lbl in pool_file_of:
                return pool_file_of[lbl]
            try:
                _rg, _ = _asset_registry()
                _rc = (_rg.get("by_id") or {}).get(lbl)
                if _rc is not None:
                    _f = _rc.get("file") or _rc.get("legacy_file") or ""
                    return "全局库/" + str(_f).split("/")[-1] if _f else "全局库"
            except Exception:
                pass
            return "画布"
        # roles 校验：首帧图/尾帧图各只允许一张（多标即报错点名，不静默取首张）；
        # 且只收图片素材（视频/音频不可做帧锚）。
        for _role in _ASSET_ROLES:
            _holders = [lbl for lbl in pool_labels if _role in pool_roles_of.get(lbl, [])]
            if len(_holders) > 1:
                raise ValueError(f"资产标注「{_role}」标了 {len(_holders)} 张（{ '、'.join(_holders) }）：请只保留一张")
            for _h in _holders:
                if pool_kind.get(_h) != "image":
                    raise ValueError(f"资产标注「{_role}」落在非图片素材「{_h}」上：首尾帧标注只收图片")
        chain_head_label = next((lbl for lbl in pool_labels if "首帧图" in pool_roles_of.get(lbl, [])), None)
        chain_tail_label = next((lbl for lbl in pool_labels if "尾帧图" in pool_roles_of.get(lbl, [])), None)

        segments = ds.get("segments") if isinstance(ds.get("segments"), list) else []

        # 分段处理中心：官方三字段组装（场景/角色/环境音/配乐）；[[标签]] -> <Picture>/<Video>/<Audio>；
        # 缺 tag 的引用只补最小映射行（_reference_tags_minimal，不写散文）。
        seg_composed = 0
        seg_custom_refs = 0
        seg_dialogues = 0
        seg_label_orders = [[] for _ in seg_prompts]   # 每段 [(kind, 标签)]，按勾选顺序
        composed_prompts = []
        # 首尾帧锚**复用素材池编号**：帧图被选为锚时前端已给它分配 图N 标注，
        # 所以它和普通参考素材共用一套 <Picture k> 编号，只是固定排在最前
        # （帧锚=1/2，参考素材顺延）。以前帧锚完全不进编号池 → 正文里手写的
        # <Picture 1> 与帧锚撞号，前端还把对齐句的 <Picture 1> 标成"悬空"红框。
        # 文件 -> 池标签反查（frame_img 存的是项目内相对路径）。
        _file_to_label = {}
        for _lbl, _fn in (pool_file_of or {}).items():
            if _fn:
                _file_to_label[str(_fn).replace("\\", "/").lstrip("./")] = _lbl
        _frame_imgs_early = _parse_frame_imgs(segments, len(seg_prompts))

        def _anchors_of(pi):
            """第 pi 段的首尾帧锚在素材池里的标签（按 首帧→尾帧 顺序，去重）。"""
            _fi = _frame_imgs_early[pi] if 0 <= pi < len(_frame_imgs_early) else ("", "")
            _out = []
            for _ff in (_fi[0], _fi[1]):
                if not _ff:
                    continue
                _l = _file_to_label.get(str(_ff).replace("\\", "/").lstrip("./"))
                if _l and _l not in _out:
                    _out.append(_l)
            return _out
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
                _reg, _ = _asset_registry()
                refs_sel = seg.get("refs")
                _keys = []   # 按序去重的引用键（label/alias/asset_id/dict 归一）
                if isinstance(refs_sel, list) and refs_sel:
                    for r in refs_sel:
                        _kk = _AS.ref_key(r)[0] if _AS is not None else str(r).strip()
                        if _kk and _kk not in _keys:
                            _keys.append(_kk)
                    seg_custom_refs += 1
                    # 提示词里 @标签 提到但没勾选的素材：按出现顺序并入，防勾选/文本失配报错
                    # （传池：标签边界由池内最长匹配决定，`@图片让这张图动起来` 不会吞掉「让」）
                    for lbl in _find_refs(full, ref_names_for_match):
                        lbl = lbl.strip()
                        if lbl and lbl not in _keys:
                            _keys.append(lbl)
                else:
                    # 缺省 = 只用提示词文本 @标签 里出现的素材（按出现顺序）；
                    # 没出现 = 本段无引用（纯文本段）。资产库总量不限，只卡单段上
                    # 限——库再大也不会逼每段显式勾选。
                    for lbl in _find_refs(full, ref_names_for_match):
                        lbl = lbl.strip()
                        if lbl and lbl not in _keys:
                            _keys.append(lbl)
                # P2 校验核：compile_refs（四形态统一），报错映射回旧链逐字格式；
                # store 不可用时走旧 inline 逻辑（旧链行为逐字保留）。
                order = []
                if _AS is not None:
                    try:
                        _res = _AS.compile_refs(_reg, _keys, i + 1)
                    except Exception:
                        _res = {"ok": False, "errors": [{"code": "E_REF_UNKNOWN"}],
                                "blocks": []}
                    if not _res.get("ok"):
                        _errs = _res.get("errors") or [{}]
                        _code = _errs[0].get("code") if _errs else ""
                        if _code == "E_MEDIA_LIMIT":
                            for _k, _cap in REF_CAPS.items():
                                _picked = [b["alias"] or b["asset_id"]
                                           for b in _res.get("blocks", []) if b["kind"] == _k]
                                if len(_picked) > _cap:
                                    raise ValueError(
                                        f"段{i + 1} 引用{_KIND_NAME[_k]}素材 {len(_picked)} 个，"
                                        f"超过官方单段上限 {_cap} 个（{_picked}）：请在段卡片少勾几个")
                            raise ValueError(_errs[0].get("message") or f"段{i + 1} 素材引用超限")
                        _bad = None
                        for _kk in _keys:
                            if _kk not in _reg["by_id"] and _kk not in _reg["by_alias"]:
                                _bad = _kk
                                break
                        raise ValueError(f"段{i + 1} 引用了未知素材标签「{_bad}」：可用标签 {pool_labels}")
                    order = [(b["kind"], b["alias"] or b["asset_id"])
                             for b in _res.get("blocks", [])]
                else:
                    for lbl in _keys:
                        if lbl not in pool_labels:
                            raise ValueError(f"段{i + 1} 引用了未知素材标签「{lbl}」：可用标签 {pool_labels}")
                        item = (pool_kind[lbl], lbl)
                        if item not in order:
                            order.append(item)
                    # 官方单段上限：图 9 / 视 3 / 音 3（超了报错并指出勾选明细）
                    for _k, _cap in REF_CAPS.items():
                        _picked = [lbl for k, lbl in order if k == _k]
                        if len(_picked) > _cap:
                            raise ValueError(f"段{i + 1} 引用{_KIND_NAME[_k]}素材 {len(_picked)} 个，"
                                             f"超过官方单段上限 {_cap} 个（{_picked}）：请在段卡片少勾几个")
                # 帧锚编号前置：帧图占本段最前的 <Picture k>（首帧=1，再尾帧=2），
                # 参考素材顺延。同一张图既是帧锚又被 @引用时只占一个号。
                _anchors = _anchors_of(i)
                if _anchors:
                    # 去重按**文件**而不只是标签字符串：正文里写 @别名、池里存
                    # 全名（或反过来）时两边的字符串不相等，只比字符串会让同一张
                    # 图既当帧锚又当参考素材、占掉两个 <Picture k> 号。
                    def _norm_file(lbl):
                        return str(pool_file_of.get(lbl, "") or "").replace("\\", "/").lstrip("./")
                    _an_files = {_norm_file(l) for l in _anchors if _norm_file(l)}
                    _rest = [(k, l) for k, l in order
                             if l not in _anchors and _norm_file(l) not in _an_files]
                    order = [("image", l) for l in _anchors] + _rest
                    _np = len([1 for k, _ in order if k == "image"])
                    if _np > REF_CAPS["image"]:
                        raise ValueError(
                            f"段{i + 1} 首尾帧锚 + 参考素材共 {_np} 张图，超过官方单段"
                            f"上限 {REF_CAPS['image']} 张（帧锚 {_anchors}）：请少勾几个素材")
                seg_label_orders[i] = order
                if order:
                    # 显性语义：tag 必须落进最终文本模型才用素材；正文已写 tag 则直通，
                    # 缺的只补最小映射行（不写散文）。未知 [[..]] 由替换函数点名报错。
                    mapping = _kind_tokens(_normalize_order(order))
                    full = _apply_label_tokens(full, order)
                    uncovered = _uncovered_tags(full, mapping)
                    if uncovered:
                        sub = [(k, lbl) for k, lbl in _normalize_order(order)
                               if lbl in uncovered]
                        full = _reference_tags_minimal(sub) + "\n" + full
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

        # 段级双按钮（分段优先，null=跟随全局默认 true）：
        # - 自动引用上段（auto_ref=false 即旧 unlink=true）：与上段毫无关联的镜头
        #   一键全断——不接上段桥、不裁头、不做桥帧门控/接缝测量/响度对齐（硬切）。
        #   该键的解析已收敛到 anchors.migrate_legacy_seg（见下方锚定块），这里不再重复。
        # - 自动按序生成（auto_seq=false 即旧 disabled=true）：槽位稳定跳过——
        #   不采样/不解码/不进成片，下段锚定跨接到最近已执行段尾帧。
        # 旧键 disabled 仍兼容（projects._clean_seg_field 已回填新键）。
        def _seg_auto(seg, new_key, old_key):
            if isinstance(seg, dict) and seg.get(new_key) is not None:
                return bool(seg.get(new_key))
            if isinstance(seg, dict) and seg.get(old_key) is not None:
                return not bool(seg.get(old_key))
            return True
        seg_disabled = []
        for i in range(len(seg_prompts)):
            seg = segments[i] if i < len(segments) and isinstance(segments[i], dict) else {}
            seg_disabled.append(not _seg_auto(seg, "auto_seq", "disabled"))

        # 段级 latent 保存（保存策略透存，分段优先）：mode all|range|tail|off +
        # split_av（图像/音频分开存）+ save_seg（本段总开关）。主循环采样定稿后
        # 自动切片落盘（见 _auto_latent_save），manual 切片走 /h3chain/latent_slice。
        seg_latent_save = []
        for i in range(len(seg_prompts)):
            seg = segments[i] if i < len(segments) and isinstance(segments[i], dict) else {}
            ls = seg.get("latent_save") if isinstance(seg.get("latent_save"), dict) else None
            seg_latent_save.append(dict(ls) if ls else None)

        # 潜空间放大二采（导演台面板控制）：每段采样定稿后立即「神经放大 latent →
        # 低强度重采样 → 解码」，分段视频与成片直接保存二采结果（同名覆盖，不再
        # 产出 upseg_* 副本）。模型在主循环前预加载：缺失/不匹配当场降级为基础
        # 分辨率整链运行（报告注明），不让每段反复撞同一个错。关闭时 up_cfg=None
        from . import upscale
        up_cfg = upscale.parse_state(ds)
        if up_cfg:
            # 性能设置 -> 二采 cfg（**只加不进指纹的键**：_hash_params 只认
            # _PARAM_KEYS + 条件项，新增键不会让既有高清记录失效重做）。
            #
            # ⚠ 这批键以前住在**项目存档**（ds.upscale）里，2026-09-23 统一迁到
            # 机器级 perf.json——理由见 upscale.parse_state 的注释块。这里做一次性
            # 合并：项目存档不再持有它们，perf 是唯一真源。
            up_cfg["chunk"] = bool(_pf.get("upscale_temporal_chunk", True))
            up_cfg["upscale_chunk_frames"] = int(_pf.get("upscale_chunk_frames") or 32)
            # overlap=0 表示「自动取时序卷积核宽」（upscale_net 内部判定，只增不减）
            up_cfg["upscale_overlap"] = int(_pf.get("upscale_overlap") or 0)
            # 放大网络是否常驻：True（默认）= 段间保留缓存、零加载（**现状口径**，
            # 与旧项目存档 `force_unload: false` 同义）；False = 每段二采收尾强制卸载
            # （删缓存 + soft_empty_cache，下段重载 ~1s，换 CPU 侧 659MB 权重副本）。
            # ⚠ 消费端在 upscale.render_segment 收尾处，条件只认这一个键
            # （旧键 `force_unload` 已随项目存档退场，别再写回 cfg）。
            up_cfg["keep_upscaler_resident"] = bool(_pf.get("keep_upscaler_resident", True))
            # 编码画质开关（preset + 暗部 aq + 抖动）与 crf 数值**分开**：
            # 开关管「哪套 preset/抖动」，crf 单独给（面板上按编码器显隐）。
            # 两者正交 —— 开关不会带着改 crf，改 crf 也不会动开关。
            up_cfg["encode_hq"] = bool(_pf.get("encode_hq", False))
            up_cfg["x264_crf"] = _pf.get("x264_crf")
            # 精化二采分块（机器级，不进指纹）：时序切段 / 空间 tile。
            # 值从 perf 直下 —— upscale.parse_state 不再解析它们（见那里的注释）。
            up_cfg["refine_temporal_on"] = bool(_pf.get("refine_temporal_on", False))
            up_cfg["refine_temporal_chunk"] = int(_pf.get("refine_temporal_chunk") or 0)
            up_cfg["refine_temporal_overlap"] = int(_pf.get("refine_temporal_overlap") or 8)
            up_cfg["refine_tile_on"] = bool(_pf.get("refine_tile_on", False))
            up_cfg["refine_tile"] = str(_pf.get("refine_tile") or "off")
            up_cfg["refine_tile_overlap"] = int(_pf.get("refine_tile_overlap") or 32)
            up_cfg["refine_tile_feather"] = int(_pf.get("refine_tile_feather") or 16)
        up_net = None
        _up_err = None
        if up_cfg:
            try:
                up_net = upscale.load_net(up_cfg)
            except ValueError as e:
                _up_err = str(e)
                up_cfg = None
            # 加载完立刻卸回 CPU：load_net 提到主循环前只为 fail-fast（权重缺失当场
            # 降级，不让每段反复撞同一个错），不是要让放大网络常驻 GPU——它落在 GPU
            # 只是 resolve_device 的副产品。放大网络是裸 nn.Module，ComfyUI 的
            # free_memory 看不见它，留在 GPU 会白白挤占一采显存（首段最明显：
            # 一采 UNET + net + 高清 latent 同时驻留 = 全链峰值）。
            # 二采通道（upscale.render_latent）发现它在 CPU 会自行腾挪并搬回 GPU，
            # 首段因此与后续段走同一条路径——否则首段是全链唯一不腾挪的一段。
            # 代价：每段一次 net 回搬（实测权重 345,280,216 参数 / 全 F16 / **659MB**
            # = 6GB 卡的 11%；旧注释写"约 200-300MB"已过时，别照那个数估），
            # PCIe 毫秒级，后续段本来就有。
            if up_net is not None:
                up_net.cpu()

        # 一采编码：与二采**同一真源**（perf 的 `encode_hq` + `x264_crf`）。
        # 原先节点上有个「一采编码」下拉，与性能设置里的「画质档位」是同一张表 ——
        # 两个入口改同一个旋钮，已删除（2026-09-23）。二采开启时其高清产物同名覆盖，
        # 本档只作用于二采未覆盖的段与基础成片。
        _bhq = bool(_pf.get("encode_hq", False))
        _bcrf, _bpreset, _baq, _bdith = upscale.resolve_encode_quad(
            _bhq, _pf.get("x264_crf"))

        # 执行序列 = 提示词段按序排列。段类型只剩两种：prompt 段（本条）与 prologue
        # 序章（链首已有视频，走节点接线，不在这个列表里）。
        # 旧的「插入视频段」已移除——它变成 src.kind="video" 的**手动锚定**，
        # 且不进成片（成片拼接属剪辑范畴）。旧存档直接报错，不静默降级、不做迁移。
        if ds.get("inserts"):
            raise ValueError(
                "该存档使用了已移除的「插入视频」功能（段 N 为插入槽）。插入视频已改为"
                "「手动锚定」，不再作为独立段，且不进成片。请新建项目重跑。")
        exec_items = [("prompt", _pi) for _pi in range(len(seg_prompts))]

        # 首帧/尾帧/尾锚：只走导演台 JSON 文件名（画布图片入口已删除）
        首帧图片 = None
        if ds.get("first_frame"):
            首帧图片 = _load_input_image(ds["first_frame"])

        # 尾帧图片（FL2VA 剧情终点）：只走导演台 JSON 文件名
        尾帧图片 = None
        if ds.get("end_frame"):
            尾帧图片 = _load_input_image(ds["end_frame"])

        # 每段尾帧锚定（身份锚点）：只走导演台 JSON 文件名
        # （旧槽位，兼容保留；新链请用资产标注 + 段 tail_src，本槽缺省即跟随标注）
        每段尾帧锚定 = None
        if ds.get("last_frame"):
            每段尾帧锚定 = _load_input_image(ds["last_frame"])

        # 资产标注来源：库内标「首帧图」/「尾帧图」的资产即链首/链尾锚来源
        # （旧三槽位为空时生效；三槽位有值则旧槽位优先，保证旧链零变化）。
        # 张量在 _ensure_pool_tensors 里随池子一起加载（可能需要项目目录），
        # 此处只定布尔语义供下文排布。
        _head_lib = chain_head_label if (首帧图片 is None and chain_head_label) else None
        _tail_lib = chain_tail_label if (尾帧图片 is None and chain_tail_label) else None
        # 段级首尾帧参考图（提示词框「首帧图/尾帧图」按钮：项目内图片，按段指定）。
        # 首段的首帧图 = i2v 起手帧、末段的尾帧图 = FL2VA 剧情终点锚（与旧链级语义
        # 对齐），因此这里优先把它们提升为链级 首帧图片/尾帧图片（旧槽位为空时）。
        seg_frame_imgs = _parse_frame_imgs(segments, len(seg_prompts))
        _fi_first = seg_frame_imgs[0][0] if seg_frame_imgs else ""
        _fi_end = seg_frame_imgs[-1][1] if seg_frame_imgs else ""
        # 段级（新入口）优先于库内旧标注：用户既然在段卡里显式选了图，就以它为准
        _head_seg = _fi_first if (首帧图片 is None and _fi_first) else None
        _tail_seg = _fi_end if (尾帧图片 is None and _fi_end) else None
        # 库级 roles 标的首尾帧图，若与段级锚指向**同一文件** → 库级那张失效。
        # 否则同一张图会被同时挂到链级首帧 + 链级尾帧，AI 看到 subject_definitions
        # 里 <Picture 1> 和 <Picture 2> 都指向它，于是产出"以 X 为首帧与结尾定格"
        # ——用户只想要尾帧时这就是 bug。roles 是用户之前手动打的标，跨场景
        # 复用同一张图很常见（库内标过"首帧图"的图以后用作尾帧图也合理），库级
        # 不能强压段级意图。
        def _norm(s):
            return str(s or "").replace("\\", "/").lstrip("./")
        if _tail_seg and _head_lib \
                and _norm(_tail_seg) == _norm(pool_file_of.get(_head_lib)):
            _head_lib = None
        if _head_seg and _tail_lib \
                and _norm(_head_seg) == _norm(pool_file_of.get(_tail_lib)):
            _tail_lib = None
        has_first_eff = 首帧图片 is not None or _head_lib is not None or _head_seg is not None
        has_end_eff = 尾帧图片 is not None or _tail_lib is not None or _tail_seg is not None

        # 段级首尾帧图引用（类似多参段级素材勾选）：决定每段是否参考首帧/尾帧图片。
        # 缺省=旧行为（首段 i2v 参考首帧图、末段参考尾帧图终点锚）；显式勾选可为
        # 中段注入首帧图头部身份锚 / 任意段注入尾帧图尾部锚，也可关闭首末段默认参考
        seg_frame_refs, seg_fr_explicit = _parse_frame_refs(
            segments, len(seg_prompts), has_first_eff, has_end_eff)
        seg_first_on = ["首帧图" in fr for fr in seg_frame_refs]
        seg_end_on = ["尾帧图" in fr for fr in seg_frame_refs]

        # 素材池：决策期只算引用集合，張量加载推迟到项目目录确定后
        # （_ensure_pool_tensors，见 root 确定后）。只加载各段实际引用的标签
        # （每标签全链只解码一次）；库内未被引用的文件本次不碰（不校验存在性、
        # 不占显存）。资产库总量不限，只卡官方单段上限（组装期已按段卡）。
        # 缺文件/坏文件只对真正引用它的段早爆，并点名标签。
        _needed = set()
        for _order in seg_label_orders:
            _needed.update(_order)
        _needed_kinds = {k for k, _ in _needed}
        _pool_skipped = max(0, len(pool_files) - len(_needed))
        _store_hits = []   # P2：经 store 三级寻址命中的标签（报告用，不进指纹）
        # 张量占位：全部由 _ensure_pool_tensors 按引用集填充（P4d：画布回退分支已删除）
        pool_tensors = {"image": {}, "video": {}, "audio": {}}
        _pool_loaded = set()   # 已加载标签（_ensure_pool_tensors 幂等守卫）
        refs = {"ref_videos": {}, "ref_video_audios": {}, "ref_audios": {}}
        # i2v 首段官方指令行（官方 I2VA 固定格式：声明首帧 = <Picture 1> 锚）；
        # 已是官方格式的段不注入（用户自管的完整结构里可能自带指令行）。
        # 纯 i2v 起手（首段无素材引用，引用集合已最终确定）才写指令行；
        # 首段同时有引用时走 r2v + 头锚 keyframe（_apply_guide 叠加），
        # 此处不写指令行避免编号错位。
        # 帧锚**不算**"参考素材"：判定首段是不是混合模式（帧锚+素材）时要把帧锚
        # 排除掉，否则帧锚一进池就把本段判成 Ref2VA、永远不生成对齐句。
        _anchors0 = set(_anchors_of(0)) if seg_label_orders else set()
        _seg0_has_refs = bool([1 for k, l in (seg_label_orders[0] if seg_label_orders else [])
                               if l not in _anchors0])
        if has_first_eff and seg_first_on[0] and not _seg0_has_refs and seg_prompts \
                and not _OFFICIAL_FIELD_RE.search(seg_prompts[0]):
            seg_prompts[0] = ("For the target video, at 0.00 seconds into the target video, "
                              "<Picture 1> (from [Shot 1]) is fully referenced.\n" + seg_prompts[0])
        # 状态池是唯一的引用来源（P4d：画布接线已删除，无需覆盖逻辑）
        has_refs = bool(_needed)
        # 链路自动推导（无手动模式）：有有效引用即走 ref conditioning；首帧可与
        # 引用共存（头锚 keyframe 叠加，不再互斥报错）。「生成模式」控件已废弃，
        # 仅为旧工作流占位，后端不再读取。
        if 起始视频 is not None and (首帧图片 is not None or chain_head_label is not None
                                     or _head_seg):
            raise ValueError("起始视频（序章）与首帧图（i2v 起始）不能同时使用：两者都定义第 1 段的视觉起点")

        if str(宽高比) != "自定义":
            宽度, 高度 = _resolve_canvas(宽高比, 百万像素)
        width, height, seed = int(宽度), int(高度), int(种子)
        length = _snap_seconds(每段时长)
        seg_lengths = [length if s is None else s for s in seg_secs]
        # 末段尾帧锚：官方 L2VA 句式（逐字照抄 h3-dialect.md §1.2）。
        # **时间点由段长帧数换算**（frames_to_seconds），与前端锚定栏预览同口径 ——
        # 以前这里写 `seg_lengths[-1] / 24.0`、前端写 `seg.seconds`，同一个锚两边
        # 算出 5.17 与 5.00 两个值。Shot N 取本段最后一镜（官方要求），不是硬编码 1。
        if has_end_eff and seg_end_on[-1] and seg_prompts \
                and not _OFFICIAL_FIELD_RE.search(seg_prompts[-1]):
            _end_s = _P.frames_to_seconds(seg_lengths[-1])
            _last_shot = len(re.findall(r"\[Shot\s+\d+\]", seg_prompts[-1])) or 1
            seg_prompts[-1] = (L2VA_HEAD.format(shot=_last_shot, t=_end_s) + "\n"
                               + seg_prompts[-1])
        ctx = int(引导帧数)
        gate_limit = max(0, int(回退上限) // 17 * 17)
        full_bridge0 = full_bridge_supported()
        # 段级注入解析（分段优先，null/0=跟随全局 ctx）：返回 (帧数, 含视频, 含音频)。
        # snap 到 token 网格可达帧（grid.snap_frames_to_tokens 下取整，不超源）。
        # 段首桥的三态解析：**显式 head anchor 优先**，没有才按默认（上段尾、全局 ctx 帧）。
        # 「沉默 = 默认」与旧行为逐字一致；unlink（旧 auto_ref=false）仍是「不生成默认
        # head anchor」的 UI 快捷开关，不引入第二套语义（规划 §7）。
        def _head_anchor(pi):
            for _a in (seg_anchors[pi] if 0 <= pi < len(seg_anchors) else []):
                if _a["on"] and _a["at"]["mode"] == "head":
                    return _a
            return None
        def _eff_inject(pi):
            """段首桥的 (帧数, 含视频, 含音频)；帧数 0 = 本段不挂桥。

            帧数取 anchor.window（已是 17k+5 档位，就是视频 latent 实际能切出的帧数）；
            取用态三态真实生效——「仅音频」的 anchor 只挂 audio_latent 分支。
            """
            _a = _head_anchor(pi)
            if _a is None:
                if 0 <= pi < len(seg_unlink) and seg_unlink[pi]:
                    return 0, False, False
                return grid.snap_window_down(ctx), True, True
            _av = _a["branches"]["av"]
            return _a["window"], _av in ("both", "video"), _av in ("both", "audio")
        def _takes_bridge(pi):
            """段 pi 是否会取用「段首接片」——与 `_inject_guide` 的产物**同口径**。

            画面或音频任一要接就算：`_tail_keyframe(with_video=False)` 明明为「仅音频」
            写了专门分支（见其文档字符串），「仅音频」段首锚是真接片，不是"不用桥"。

            交棒（上段算 `guide`）与取用（本段拿 `eff_guide`）必须都问这一个函数。
            此前交棒借的是 `next_wants_bridge`（**门控/裁帧**口径，只看视频分支），
            于是「仅音频」段拿到的是上一轮残留的旧接片——设了锚却按别的源钉，
            且不报错（详见 2026-09-20 日志）。
            """
            fr, want_v, want_a = _eff_inject(pi)
            return bool(fr > 0 and (want_v or want_a))
        def _inject_guide(video_t, audio_t, for_pi, end_tokens=None):
            """段首桥 keyframe；src 非 prev_tail 时改取该外源（库/段/视频/图片）。

            外源由 _resolve_anchor_latent 归一（形状不匹配走先裁后编，取不到直接抛），
            所以这里不再有「静默回落上段尾」——那正是这次重构要根除的行为。
            """
            fr, want_v, want_a = _eff_inject(for_pi)
            if fr <= 0 or not (want_v or want_a):
                return None
            _a = _head_anchor(for_pi)
            ev, ea = video_t, audio_t
            if _a is not None and _a["src"]["kind"] != "prev_tail":
                ev, ea = _resolve_anchor_latent(_a)
            return _tail_keyframe(
                ev, ea, fr,
                bool(want_a) and ea is not None and KEYFRAME_AUDIO_SUPPORTED and full_bridge0,
                end_tokens=end_tokens, full_bridge=full_bridge0, with_video=bool(want_v))
        from . import qc  # 桥帧打分 + 接缝测量共用（延迟导入：无 ComfyUI 环境下结构单测不触达）
        clip, video_vae, audio_vae = 文本编码器, 视频VAE, 音频VAE

        def _load_library_latent(file):
            """latent 库文件 -> (video_cpu, audio_cpu|None)；**取不到直接抛**。

            旧实现「加载/形状失败就静默回落上段尾」已删除——设了源却不生效还不说，
            是最坏的一类 bug（规划 §1.3）。只认 latent/<名>.pt（防穿越）；
            形状与「能不能重编」的判断交给 _resolve_anchor_latent 里的
            resolve_anchor_source（它要区分裸 latent 与可重编素材，报错文案不同）。
            """
            f = str(file or "").strip().replace("\\", "/")
            parts = [p for p in f.split("/") if p and p != "."]
            if len(parts) != 2 or parts[0] != "latent" or not parts[1].endswith(".pt"):
                raise ValueError(f"latent 源路径非法：{f!r}（须为 latent/<名>.pt）")
            cand = os.path.join(root, "latent", parts[1]) if root else None
            if cand is None or not os.path.isfile(cand):
                raise ValueError(f"latent 源缺失：{f}（项目 latent/ 下没有这个文件）")
            with open(cand, "rb") as fh:
                payload = torch.load(fh, map_location="cpu", weights_only=True)
            ev = payload.get("video")
            if ev is None or getattr(ev, "dim", lambda: 0)() != 5:
                raise ValueError(f"latent 源无视频分支：{f}")
            ea = payload.get("audio")
            if ea is not None and getattr(ea, "dim", lambda: 0)() != 4:
                ea = None
            return ev, ea

        def _anchor_asset(label, want_kind):
            """锚点素材标签 -> (绝对路径 | None, pool 文件名 | None)。

            标签寻址走 P2 三级寻址（asset_store 注册表 by_alias / by_id -> 绝对路径），
            注册表未命中才回落 pool 文件名（input 目录 / 项目 assets）。图片与视频共用
            这一条通路：原先只有图片有，视频被写死走 `_load_input_video`（只认 ComfyUI
            input 目录），于是项目 assets/ 与素材库里的视频根本接不进来。

            未知标签、类型不符一律硬抛——手动锚是显式意图，静默换源等于「设了锚不生效」。
            """
            _rec = None
            if label not in pool_labels and _AS is not None:
                _reg, _ = _asset_registry()
                _rec = ((_reg.get("by_alias") or {}).get(label)
                        or (_reg.get("by_id") or {}).get(label))
            if label not in pool_labels and _rec is None:
                raise ValueError(f"锚点引用了未知素材标签「{label}」：可用标签 {pool_labels}")
            _kind = pool_kind.get(label, (_rec or {}).get("kind"))
            if _kind != want_kind:
                raise ValueError(f"锚点「{label}」的素材类型是 {_kind or '未知'}，"
                                 f"不是 {want_kind}（源类型与素材类型必须一致）")
            _abs = None
            if _AS is not None and _rec is not None:
                _reg2, _libroot = _asset_registry()
                _abs = _AS.resolve_absolute(_rec, _project_root_for_assets(), _libroot)
            return _abs, pool_file_of.get(label)

        def _anchor_image(label):
            """素材标签 -> 单帧图片张量（[1,H,W,3]）。未知标签/非图片直接抛。"""
            _abs, _fn = _anchor_asset(label, "image")
            _ensure_pool_tensors()
            img = pool_tensors["image"].get(label)
            if img is None:
                if _abs is None and _fn is None:
                    raise ValueError(f"锚点「{label}」找不到对应文件")
                img = (_decode_image_file(_abs) if _abs is not None
                       else _load_input_image(_fn, _project_root_for_assets()))
                pool_tensors["image"][label] = img
            return img

        def _anchor_video(label):
            """素材标签 -> (帧张量, 音轨)。未知标签/非视频直接抛。

            与 _anchor_image 同一条寻址通路，所以素材库（项目 assets/ 、全局库）里的
            视频都能作锚源；_decode_video_file 只收绝对路径，不经过 input 目录。
            """
            _abs, _fn = _anchor_asset(label, "video")
            _ensure_pool_tensors()
            hit = pool_tensors["video"].get(label)
            if hit is not None:
                return hit
            if _abs is None and _fn is None:
                raise ValueError(f"锚点「{label}」找不到对应视频文件")
            return (_decode_video_file(_abs, label) if _abs is not None
                    else _load_input_video(_fn, _project_root_for_assets()))

        def _anchor_audio(label):
            """素材标签 -> AUDIO dict。纯音频锚（src.kind="audio"）专用通路：
            音频参考不走视频 latent，也就没有 C/H/W 约束。未知标签/非音频直接抛。
            """
            _abs, _fn = _anchor_asset(label, "audio")
            if _abs is not None:
                return _decode_audio_file(_abs)
            if _fn is not None:
                return _load_input_audio(_fn, _project_root_for_assets())
            raise ValueError(f"锚点「{label}」找不到对应音频文件")

        def _resolve_anchor_latent(a):
            """anchor 源 -> (video_dev, audio_dev|None)，已确保 C/H/W 与本链一致。

            决策归 `anchors.resolve_anchor_source`（匹配直用 / 有源先裁后编 / 无源硬报错，
            报错自带转档指引）；这里只负责把决策落成实际张量。手动锚不静默降级：
            取不到就抛，点名是哪一条锚——绝不回落上段尾。

            形状基准取 `_chain_ref[0]`（本链任意一个已存在的 latent）：C/H/W 是硬约束，
            没有基准就无法判定能否拼接，此时宁可报错也不赌——赌输是模型层整链崩。
            """
            chain_ref = _chain_ref[0]
            kind, ref = a["src"]["kind"], a["src"]["ref"]
            # 分支取用态在此**统一生效**：不需要的视频分支不编码、不校验（省一次 VAE），
            # 不需要的音频分支直接置 None——此前只有段首桥走 _eff_inject 尊重三态，
            # 段中/段尾锚的「仅图像/仅音频」会被无视、两路分支全量注入。
            want_v = a["branches"]["av"] in ("both", "video")
            want_a = a["branches"]["av"] in ("both", "audio")
            # 纯音频源：不占视频行，无 C/H/W 约束，也不需要链分辨率基准
            if kind == "audio":
                _aud = _anchor_audio(ref)
                ea = _encode_audio_latent(audio_vae, _aud,
                                          audio_tokens_for_frames(int(a["window"])))
                if not want_a:
                    raise ValueError(f"锚点 {a['id']}：音频源只支持「仅音频」取用分支")
                return None, ea
            if chain_ref is None:
                raise ValueError(
                    f"锚点 {a['id']} 无法解析：本链还没有任何 latent 作为分辨率基准"
                    "（首段无上段尾、也无序章）。请先跑完一段，或改选不需要形状比对的源")
            chain_chw = (int(chain_ref.shape[0]), int(chain_ref.shape[-2]),
                         int(chain_ref.shape[-1]))
            ea = None
            hw_src = None          # 需要先裁后编时用（像素帧 + 音频）
            if kind == "prev_tail":
                # prev_tail 是**「上段尚未生成」的哨兵**（anchors.SRC_KINDS 默认源），
                # 不是可解析的张量 —— anchors._REENCODE_HINT 给它的指引就是
                # 「上段尚未生成；先跑完上段，或改用别的源」。
                # 本函数开头也写明「手动锚不静默降级……绝不回落上段尾」：
                # 段首桥的 prev_tail 由 eff_guide 承担（_anchor_head 已先行排除该 kind），
                # 能走到这里的 mid/tail 锚带 prev_tail 属配置矛盾 —— 点名报错，
                # 而不是返回一个不存在的上段尾（原代码引用了从未定义的 ref_video_t）。
                raise ValueError(f"锚点 {a['id']}：源类型「上段尾」没有可用的上段"
                                 "（上段尚未生成或被禁用/跳过）——先跑完上段，或改用别的源")
            if kind == "library":
                ev, ea = _load_library_latent(ref)
                if want_v:
                    anchors.resolve_anchor_source(
                        a, chain_chw,
                        {"shape": tuple(ev.shape),
                         "frames": latent_t_to_frames(int(ev.shape[2])),
                         "fps": a["src"]["src_fps"], "reencodable": False})
            elif kind == "segment":
                # 段源 ref 的规范形式是 `seg_NNN`（anchors.py §3 文档、routes.anchor_sources、
                # tests/test_anchors.py 三处一致）。此前这里多要一个 ".pt" 后缀、按
                # `_pn[4:-3]` 切片取段号，于是面板里选出来的段源**必然**报错。
                _pn = str(ref or "").strip()
                _num = _pn[4:] if _pn.startswith("seg_") else ""
                if not _num.isdigit() or not root:
                    raise ValueError(f"段源引用非法：{ref!r}（须为 seg_NNN 形式，如 seg_003）")
                ev, ea = (t.to("cpu") for t in checkpoint.load_segment(root, int(_num)))
                if want_v:
                    anchors.resolve_anchor_source(
                        a, chain_chw,
                        {"shape": tuple(ev.shape),
                         "frames": latent_t_to_frames(int(ev.shape[2])),
                         "fps": 24.0, "reencodable": False})
            elif kind in ("video", "image"):
                if kind == "video":
                    _imgs, _aud = _anchor_video(ref)
                else:
                    _imgs, _aud = _anchor_image(ref), None
                _total = int(_imgs.shape[0])
                plan = anchors.resolve_anchor_source(
                    a, chain_chw,
                    {"shape": (3, _total, int(_imgs.shape[1]), int(_imgs.shape[2])),
                     "frames": _total, "fps": a["src"]["src_fps"], "reencodable": True})
                _imgs = _imgs[plan["start_f"]:plan["end_f"]]
                if int(_imgs.shape[0]) < 1:
                    raise ValueError(f"锚点 {a['id']} 源帧窗为空："
                                     f"[{plan['start_f']},{plan['end_f']})")
                if plan["note"]:
                    report.append(plan["note"])
                if not a["src"]["meta_ok"]:
                    report.append(f"锚点 {a['id']}：源帧率未知，按 {a['window']} 帧钉住"
                                  "（时长以帧计，不折秒）")
                if want_a and _aud is None:
                    raise ValueError(f"锚点 {a['id']}：图片源没有音轨，"
                                     "「图像+音频/仅音频」的音频分支无从取用"
                                     "（请改用带音频的视频/latent，或改「仅图像」）")
                if not want_v:
                    hw_src = (None, _aud)   # 只要音频：跳过视频 VAE 编码
                else:
                    hw_src = (_imgs, _aud)
            else:
                raise ValueError(f"锚点 {a['id']} 源类型非法：{kind!r}")

            if hw_src is not None:      # 先裁后编：帧窗 → center-cover → VAE
                _imgs, _aud = hw_src
                ev = (video_vae.encode(_center_cover(_imgs, width, height))
                      if _imgs is not None else None)
                ea = None
                _wav = _aud.get("waveform") if isinstance(_aud, dict) else None
                if _wav is not None and int(_wav.shape[-1]) > 0:
                    # 音频按取用窗对齐（不是全曲砸进来）：音频窗 = 窗宽帧数对应的 token 数，
                    # 与段首桥 _tail_keyframe 的 at=audio_tokens_for_frames(ctx_frames) 同一契约
                    _at = audio_tokens_for_frames(int(a["window"]))
                    ea = _encode_audio_latent(audio_vae, _aud, _at)
            elif not want_v:
                ev = None               # 仅音频：库/段 latent 的视频分支不注入
            # window 是取用契约：所有源统一裁到 window 对应的 token 数（取尾部），
            # 否则「window=1 的单帧身份锚」会退化成"整段 latent 砸在末帧上"。
            _vt = frames_to_latent_t(int(a["window"]), up=False)
            if ev is not None and int(ev.shape[2]) > _vt:
                ev = ev[:, :, ev.shape[2] - _vt:, :, :]
            if ea is not None and want_a:
                _at = audio_tokens_for_frames(int(a["window"]))
                if int(ea.shape[-1]) > _at:
                    ea = ea[..., -_at:].clone()
            return (ev.to(video_vae.device) if ev is not None else None,
                    (ea.to(audio_vae.device) if ea is not None else None))
        # 链路自动推导（无手动模式）：有有效引用即走 ref conditioning；
        # 首帧可与引用共存（头锚 keyframe 叠加）。均匀链的推导值与旧版逐字一致，
        # 旧存档续跑指纹不受影响。
        if has_refs:
            chain = "r2v（ref2va UNET）"
        elif has_first_eff and has_end_eff:
            chain = "FL2VA 首尾帧（首帧开场 → 尾帧终点，fl2va UNET）"
        elif has_end_eff:
            chain = "L2VA 尾帧终点（fl2va UNET）"
        elif has_first_eff:
            chain = "i2v 首段 + t2v 续段（fl2va UNET）"
        else:
            chain = "t2v（fl2va UNET）"

        # cond 文本编码缓存代理：一采/二采/重摇全链共用同一实例——TE 的
        # tokenize+encode 只依赖提示词文本，同一段提示词（含二采高清条件重建、
        # negative 空串）只前向一次；参考图/视频的 VAE 编码与画布其他节点不受影响
        # ⚠ 容量用构造默认（32），**不再从 perf 读**：`cond_cache_size` 已删除。
        # 原因不是没人读（确实有人读），而是实测证明那个旋钮对真实路径毫无作用 ——
        # 官方 H3 节点固定给 tokenize 传一个 list 参数，而缓存的键要求参数可哈希，
        # 于是全部旁路、命中恒为 0，容量 1/8/32 结果逐字相同（见 perf.py 同名注释）。
        from .cond_cache import CachedClipProxy
        clip = CachedClipProxy(clip)
        negative = clip.encode_from_tokens_scheduled(clip.tokenize(""))

        _mode = str(生成模式)   # 旧控件占位：后端不再读取，仅报告回显
        report = [f"H3 Seamless Chain：{len(seg_prompts)} 段，链路 {chain}（按实际引用自动推导），上下文 {ctx} 帧"]

        # ---- 手动锚定（Anchor Studio）：唯一入口 ----
        # 所有「把某段 latent / 素材钉到本段某位置」的诉求统一走 seg.anchors[]（规划 §2/§3）。
        # 旧档的 latent_ref / tail_src / auto_ref 与全局 last_frame 由 migrate_legacy_seg
        # 一次性迁移——迁移函数保留下来就是为了读旧档（规划 §7「保留做参考」）。
        # auto_ref 的解析也收敛到那里，故 seg_unlink 在本块产出，不再单独解析。
        _lf_label = str(ds.get("last_frame") or "").strip()
        _chain_ref = [None]   # 本链任取一个 latent，供 anchor 形状比对（C/H/W 是硬约束）
        seg_anchors, seg_unlink = [], []
        for _ai in range(len(seg_prompts)):
            _seg_raw = segments[_ai] if _ai < len(segments) \
                and isinstance(segments[_ai], dict) else {}
            _mig = anchors.migrate_legacy_seg(_seg_raw, ctx, last_frame=_lf_label or None)
            seg_unlink.append(bool(_mig.get("unlink")))
            _anc = _mig.get("anchors") or []
            if _anc:
                # 手动锚**硬校验**（不软降级）：设了锚不生效还不说，是最坏的一类 bug。
                # 自动锚（段首默认桥 / 尾帧图）仍走 guides 的「只报不拦」，保证旧链逐帧一致。
                anchors.validate_anchors(_anc, seg_lengths[_ai], label=f"段{_ai + 1} ")
            seg_anchors.append(_anc)
        _ANCHOR_MODE_CN = {"head": "段首", "mid": "段中", "tail": "段尾"}
        _ANCHOR_AV_CN = {"both": "图像+音频", "video": "仅图像", "audio": "仅音频"}
        for _ai, _lst in enumerate(seg_anchors):
            for _a in _lst:
                _am = _ANCHOR_MODE_CN[_a["at"]["mode"]]
                if _a["at"]["mode"] == "mid":
                    _am = f"帧{_a['at']['frame_idx']}"
                report.append(
                    f"锚点：段{_ai + 1} ← 源 {_a['src']['kind']}"
                    + (f"({_a['src']['ref']})" if _a["src"]["ref"] else "")
                    + f" {_a['window']} 帧窗 → 本段{_am}，{_ANCHOR_AV_CN[_a['branches']['av']]}"
                    + ("" if _a["on"] else "（已关闭，不注入）"))
        # 二采模型来源与结构签名（一次算好，逐段复用）：未接「二采模型」槽时沿用
        # 一采模型——此时签名即一采模型的签名。签名只用于报告标注与 manifest 留痕，
        # 不进 params_hash：换二采模型不会自动重做已有高清分段。
        _up_model = 模型 if 二采模型 is None else 二采模型
        _up_tag = upscale.model_tag(_up_model) if _up_model is not None else ""
        # 一采/二采是否是两份独立权重：True 时每段二采入口先卸一采（见 _up_hi）。
        # 同 base 的克隆（只差 LoRA）不卸——卸了等于把二采自己搬走，纯浪费。
        _up_swap = upscale.models_distinct(模型, _up_model)
        _up_swap_logged = [False]
        # 阶段 0 只读探测：同 base 只差 LoRA 时到底有没有真的回载权重（§5.3）。
        # 只打一次，避免每段刷屏。
        _reload_logged = [False]
        _perf_logged = [False]
        # 落盘守卫**接行为**：critical 且 guard_action=block → 禁止二采精化前的全卸
        # （Linux 无 swap 时一次全卸就是 OOM killer SIGKILL，连 except 都跑不到）。
        _guard_block = False
        # 阶段 0：硬件判据一行流（只在本次运行打一次）。
        # 放这儿而不是插件导入时：UNET / TE 的真实体量**只有拿到模型才算得出**，
        # 而 R_v / R_m 全靠它俩。量不到就显示 "?"，不编数字。
        if not _perf_logged[0]:
            _perf_logged[0] = True
            try:
                _hw = perf.probe_hardware(unet_bytes=perf.weight_bytes(模型),
                                          te_bytes=perf.weight_bytes(clip))
                _line = perf.report_line(_hw)
                print(_line, flush=True)
                # 守卫系数按 perf 的 `offload_guard_ratio` 走。遗留修补（2026-09-23）：
                # `offload_guard` / `guard_allows_unload` 的签名**本来就收 ratio**，
                # 这里却一直没传 → 键改了不生效，系数恒等于常量 1.2。
                try:
                    _gratio = float(_pf.get("offload_guard_ratio"))
                except (TypeError, ValueError):
                    _gratio = perf.DEFAULT_GUARD_RATIO
                _gd = perf.offload_guard(_hw, _gratio)
                _guard_msg = _gd["message"] if not _gd["ok"] else ""
                if _guard_msg:
                    print(f"[H3性能] 落盘守卫：{_guard_msg}", flush=True)
                # 接行为：把「不能卸」这个结论交给二采（upscale.render_latent）
                if str(_pf.get("guard_action") or "warn").lower() == "block":
                    _allowed, _gd2 = perf.guard_allows_unload(_hw, "block", _gratio)
                    _guard_block = not _allowed
                    if _guard_block:
                        print("[H3性能] 落盘守卫=block：本次运行的二采精化前全卸已禁用"
                              "（无 swap 机器上全卸会被 OOM killer 杀掉）", flush=True)
                # 余量策略：0.35 起 DynamicVRAM 会把显存吃到接近 100%，
                # 二采这种「同卡再插一段高清采样」最先炸——先把根因说清楚
                _hr_msg = perf.vram_headroom_advice(_hw, _hw.get("vram_policy")) or ""
                if _hr_msg:
                    print(f"[H3性能] 余量策略：{_hr_msg}", flush=True)
                # 显存预算账：主动留空多少 / 能给权重缓存多少 / 每步要重读多少。
                # 「有没有把显存用满」的静态一半答案（实测一半看段末的精化峰值）。
                _bd = perf.vram_budget(_hw, _hw.get("vram_policy"))
                _bd_line = perf.vram_budget_line(_bd)
                if _bd_line:
                    print(f"[H3性能] {_bd_line}", flush=True)
                # 放大网络是**裸 nn.Module**（不是 ModelPatcher）：ComfyUI 的显存账
                # 看不见它，也不会按需挤占它 —— 所以它必须手工上卡 / 下卡，而手工
                # 上卡就得先手工腾地方（每段一次全卸的根因）。把这笔「幽灵占用」讲明。
                if up_net is not None:
                    try:
                        _nb = getattr(up_net, "_h3_bytes", None)
                        if _nb is None:
                            _nb = sum(int(p.numel()) * int(p.element_size())
                                      for p in up_net.parameters())
                            up_net._h3_bytes = _nb
                        print(f"[H3性能] 放大网络 {_nb / (1024 ** 2):.0f}MB 是裸 nn.Module："
                              "ComfyUI 的显存账看不见它、也不会按需挤占 → 只能手工上/下卡，"
                              "这是每段一次全卸腾挪的根因", flush=True)
                    except Exception:
                        pass
                print(f"[H3性能] {perf.blockswap_line(perf.probe_blockswap(), None)}",
                      flush=True)
                # 块交换：把面板那三项接到**官方**的块级权重流动上（本插件不自己搬
                # 权重 —— 理由与证据见 perf.apply_blockswap）。放在这里是因为**只有
                # 拿到模型才算得出 UNET 真实体量**，`blocks_to_swap=-1`（跟随档位）
                # 才能按 suggest_blocks 反解出真块数；面板打开那次拿不到体量，会
                # 明确「不干预」而不是瞎设一个值。模型侧的预取开关在下面与 FFN /
                # 头分块一起装。
                _bs = perf.apply_blockswap(_pf, _hw)
                _bs_note = ""
                if _bs["on"]:
                    _bs_note = _bs["note"]
                    print(f"[H3性能] 块交换：{_bs_note}", flush=True)
                # 落盘：被 OOM kill 时 stdout 可能一起没了，JSONL 是唯一留存
                perf.emit({"kind": "overview", "line": _line,
                           "guard": _guard_msg, "headroom": _hr_msg,
                           "budget": _bd_line, "hw": _hw, "blockswap": _bs_note})
            except Exception:
                pass
        if up_cfg:
            _up_src = "独立「二采模型」槽" if 二采模型 is not None else "沿用一采「模型」"
            report.append(f"二采模型：{_up_src}" + (f"（{_up_tag}）" if _up_tag else "")
                          + ("；与一采非同源，每段二采前先卸一采（换页次数不变，"
                             "只把空闲显存并成整块）" if _up_swap else ""))
        _unlink_pos = [str(item_i + 1) for item_i, it in enumerate(exec_items)
                       if it[0] == "prompt" and seg_unlink[it[1]]]
        if _unlink_pos:
            report.append(f"关闭自动引用上段：{len(_unlink_pos)} 段与上段断链（无桥接/硬切，段 {'、'.join(_unlink_pos)}）")
        _off_pos = [str(item_i + 1) for item_i, it in enumerate(exec_items)
                    if it[0] == "prompt" and seg_disabled[it[1]]]
        if _off_pos:
            report.append(f"关闭自动按序生成：{len(_off_pos)} 段跳过执行不进成片（段 {'、'.join(_off_pos)}；"
                          "随时恢复零成本）")
        if up_cfg:
            # ③ 落盘守卫接行为 + ④ 精化前腾挪强度（vram_shuffle）——都是「不进
            # 指纹」的行为开关，交给 upscale.render_latent 消费。
            up_cfg["guard_block"] = bool(_guard_block)
            up_cfg["vram_shuffle"] = str(_pf.get("vram_shuffle") or "auto")
        # FFN 分块：主干 OOM 正崩在 FFN 的 LoRA bypass 上（单次 6.13GB）。按 token
        # 切块算 MLP，**数学等价、零画质损失**；装之前先自测，不通过就整段不装。
        #
        # 两件套（2026-09-23）：`ff_chunk_on` 是总开关，`ff_chunk_tokens` 是「切多大」，
        # `ff_chunk_min_tokens` 是「多短就不切」。开关关掉时参数保号不生效 —— 用户
        # 反复开关不必重填。历史默认是「tokens>0 即装」，现在改成认开关、且 tokens
        # 缺省给 4096（见 perf.DEFAULT_PERF），关掉开关就走原路径。
        _ff_on = bool(_pf.get("ff_chunk_on", False))
        _ff_tokens = int(_pf.get("ff_chunk_tokens") or 0) if _ff_on else 0
        _ff_min = int(_pf.get("ff_chunk_min_tokens") or 0)
        if _ff_tokens > 0:
            try:
                _ff_n, _ff_why = upscale.install_ff_chunking(模型, _ff_tokens,
                                                             min_tokens=_ff_min)
                if _ff_n:
                    report.append(f"性能：FFN 分块已启用（{_ff_n} 个 Linear，"
                                  f"每块 {_ff_tokens} token · 数学等价，画质零损失）"
                                  + (f"；{_ff_why}" if _ff_why else ""))
                elif _ff_why:
                    report.append(f"性能：FFN 分块未启用（{_ff_why}）")
                if 二采模型 is not None and _up_model is not 模型:
                    _ff_n2, _ = upscale.install_ff_chunking(_up_model, _ff_tokens,
                                                            min_tokens=_ff_min)
                    if _ff_n2:
                        report.append(f"性能：二采模型同样装上 FFN 分块（{_ff_n2} 个 Linear）")
            except Exception:
                pass
        # 注意力头分块（attn_head_on + attn_head_chunks）：把 attention 按 head 分组
        # 逐组算 —— head 之间独立 → **精确、无损**；同时提前释放 normed hidden 与
        # 融合 qkv buffer。依赖上游 Attention.forward 的内部结构，装不上就静默跳过
        # （见 upscale.install_low_vram_attention 的结构探测 + 装前自测）。
        _ah_on = bool(_pf.get("attn_head_on", False))
        _ah_n = int(_pf.get("attn_head_chunks") or 1) if _ah_on else 0
        if _ah_n > 1:
            try:
                _ah_k, _ah_why = upscale.install_low_vram_attention(模型, _ah_n)
                if _ah_k:
                    report.append(f"性能：注意力头分块已启用（{_ah_k} 个 attn，"
                                  f"分 {_ah_n} 组 · head 独立，画质零损失）")
                elif _ah_why:
                    report.append(f"性能：注意力头分块未启用（{_ah_why}）")
                if 二采模型 is not None and _up_model is not 模型:
                    _ah_k2, _ = upscale.install_low_vram_attention(_up_model, _ah_n)
                    if _ah_k2:
                        report.append(f"性能：二采模型同样装上注意力头分块（{_ah_k2} 个 attn）")
            except Exception:
                pass
        # 块交换的**模型侧**那半边：官方块级预取的开关。总闸是
        # `transformer_options["prefetch_dynamic_vbars"]`（官方每次前向按
        # patcher.is_dynamic() 写下的），预取深度由队列结构写死为「提前 1 块」→
        # 只能是开关，见 upscale.install_block_prefetch。
        # 默认（开）**不干预**：官方在 DynamicVRAM 下本来就是开的，装一层包装去
        # 「确认它开着」只是白搭每前向一次字典写；只有关掉才真包一层。这里照样
        # 调一次（幂等），因为用户可能在同一次运行里把它从关拨回开 —— 那一侧要
        # 把包装拆掉。
        _bp_on = bool(_pf.get("blocks_prefetch", True))
        try:
            _bp_k, _bp_why = upscale.install_block_prefetch(模型, _bp_on)
            if _bp_why:
                report.append(f"性能：{_bp_why}")
            if 二采模型 is not None and _up_model is not 模型:
                upscale.install_block_prefetch(_up_model, _bp_on)
        except Exception:
            pass
        # ★ 这里必须重新起一个 `if up_cfg:` —— 它不是新加的条件，是**修一处既有
        # 缩进 bug**：二采报告这一段（_tb … 以及它的 `elif _up_err:`）本来就该挂在
        # `if up_cfg:` 下，但历史上某个「在它前面插入 if 块」的改动把这两个分块安装
        # 块塞进了守卫与它的函数体之间，于是这段变成了「只有开了分块才生成二采报告」，
        # 而 `elif _up_err:` 改挂到了 `if _ff_tokens > 0:` / `if _ah_n > 1:` 上
        # （HEAD 里就已经是这样）。后果：关着分块时报告里整段二采参数消失、二采跳过
        # 原因挂到无关条件上。此处一行复原，不改任何行为语义之外的判定。
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
            _ehq = bool(up_cfg.get("encode_hq"))
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
                          + (" · 高清编码档" if _ehq else "")
                          + (f" · 独立采样 {_sam}/{_sch}".rstrip("/")
                             if (_sam or _sch) else "")
                          + (" · 增益重试" if up_cfg.get("retry") is True else "")
                          + "——每段采样定稿后立即渲染高清，分段视频与成片直接保存二采结果")
        elif _up_err:
            report.append(f"潜空间放大二采：面板已开启但本次跳过——{_up_err}")
        if _bhq:
            report.append(f"编码：高清档({_bpreset} · crf{_bcrf})"
                          f"（来自 ⚡ 性能优化设置）——二采未覆盖的段与基础成片按此档落盘")
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
                _stat = "、".join(f"{_k_short[k]}×{sum(1 for kk, _, _, _ in pool_files if kk == k)}"
                                  for k in ("image", "video", "audio")
                                  if any(kk == k for kk, _, _, _ in pool_files))
                _names = "、".join(f"{_k_short[k]}·{lbl}（{_store_short(lbl)}）"
                                   for k, lbl, _f, _r in pool_files)
                _parts.append(f"池 {_stat}（{_names}）")
            if seg_custom_refs:
                _parts.append(f"段级子集 {seg_custom_refs}/{len(seg_prompts)} 段自定义")
            if _pool_skipped > 0:
                _parts.append(f"库内 {_pool_skipped} 个未引用素材本次未加载（总量不限、按段按需）")
            if _store_hits:
                _parts.append(f"全局库×{len(_store_hits)}（{'、'.join(_store_hits)}）")
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
        # 自定义 sigma 表（少步蒸馏 LoRA 用：HyperFlow 8 步 / Turbo 等）
        _sig_tag = _sigmas_adapter.fingerprint_tag(自定义Sigmas)
        if _sig_tag is not None:
            _sig_steps = _sigmas_adapter.sigmas_steps(自定义Sigmas) or 0
            report.append(f"自定义 Sigmas：{_sig_steps} 步"
                          + f"（「步数 {int(步数)}」与「调度器 {调度器}」已忽略，采样器仍为「{采样器}」）"
                          + "；只作用于一采，二采沿用自身参数")
            if fade_ratio > 0:
                report.append(f"注意：递减锚定按 sigma 进度计算，{_sig_steps} 步下曲线只有 {_sig_steps} 个采样点（阶梯化）")
            if 采样器 != "euler":
                report.append(f"提示：少步蒸馏配方一般用 euler，当前是「{采样器}」——"
                              "每步吃 (t,r) 区间端点的蒸馏假设与多步高阶采样器不一致，属未验证组合")
            if 二采模型 is None and up_cfg:
                report.append("提示：二采未接独立模型，仍沿用一采模型——即二采会带着这份少步蒸馏权重跑，"
                              "但用的是二采自己的步数/调度器（不是蒸馏表），轨迹对不上；"
                              "不想让二采用这个 LoRA，请在「二采模型」另接一份基础（非蒸馏）权重")
            if up_cfg:
                _miss = [k for k in ("sampler", "scheduler") if not str(up_cfg.get(k) or "").strip()]
                if _miss:
                    report.append("提示：二采未指定" + "、".join(_miss)
                                  + f"，会沿用主链「{采样器}/{调度器}」——该组值对一采已被 sigma 表覆盖、"
                                    "对二采**仍然生效**；要区分请在二采面板显式填写")

        # 身份锚张量占位：真实编码在 root 确定后（见 pbar 前「资产加载与锚解析」）。
        # 旧三槽位（首帧/尾帧/每段尾帧图）与资产标注在此汇合：旧槽位优先，空则用标注。
        tail_anchor_latent = None
        end_frame_latent = None
        head_frame_latent = None

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
        # 自定义 sigma 表进指纹（改表即整链重做）。未接时不加键——json.dumps(sort_keys)
        # 下「多一个空键」也是新指纹，会让既有项目的存档全部续不上。
        if _sig_tag is not None:
            ckpt_params["sigmas"] = _sig_tag
        # 语义桥进指纹（**只在开启时加键**，与 sigmas 同一个理由：未启用时不加键，
        # 既有项目存档续跑零影响）。它改的是每段的 cond 张量 —— 不进指纹就会出现
        # 「前几段带桥、后几段不带」的半条链，那比整链重做糟得多。
        if _bridge_cfg["enabled"]:
            ckpt_params["bridge"] = {
                "adapter": _bridge_cfg["adapter"],
                "alpha": _bridge_cfg["alpha"],
                "scope": _bridge_cfg["scope"],
            }
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
            # 注意：seg_disabled（自动按序生成=false）故意不进哈希——跳过/恢复
            # 零重做成本（旧语义保留）。
            # 手动锚定进哈希：anchor 直接影响生成结果，等同内容变更 → 改 anchor 就
            # 只重建本段（区间引擎接手，不下游级联）。序列化只取影响结果的字段——
            # id 是 UI 标识、meta_ok 是展示态，都不该让同一份配置算出两个哈希。
            for _a in (seg_anchors[i] if 0 <= i < len(seg_anchors) else []):
                tag = ("anc:"
                       f"{_a['src']['kind']}:{_a['src']['ref'][:64]}:"
                       f"{_a['src']['start_f']}-{_a['src']['end_f']}:"
                       f"{_a['at']['mode']}:{_a['at']['frame_idx']}:"
                       f"{_a['window']}:{_a['branches']['av']}:"
                       f"{int(bool(_a['on']))}|{tag}")
            _ls = seg_latent_save[i] if 0 <= i < len(seg_latent_save) else None
            if isinstance(_ls, dict) and (_ls.get("mode") not in (None, "all")
                                         or (_ls.get("tail_f") or 0) > 0
                                         or (_ls.get("start_f") or 0) > 0
                                         or (_ls.get("end_f") or 0) > 0
                                         or _ls.get("split_av") is True):
                tag = (f"ls:{_ls.get('mode')}:{_ls.get('start_f')}-{_ls.get('end_f')}"
                       f":{_ls.get('tail_f')}:{'s' if _ls.get('split_av') else 'a'}|{tag}")
            # 段级首尾帧图引用：显式设置且与默认值不同才进哈希（显式但等于默认=行为
            # 不变不重做；未设置段哈希不变，旧存档续跑零影响）。默认值按有效
            # 首尾来源判定（含资产标注）。
            if seg_fr_explicit[i] and seg_frame_refs[i] != _default_frame_refs(
                    i, len(seg_prompts), has_first_eff, has_end_eff):
                tag = f"fr:{','.join(seg_frame_refs[i])}|{tag}"
            # 段级首尾帧参考图：换了图就该重做本段（旧存档无该键=零影响）
            _fi = seg_frame_imgs[i] if i < len(seg_frame_imgs) else ("", "")
            if _fi[0] or _fi[1]:
                tag = f"fimg:{_fi[0]}:{_fi[1]}|{tag}"
            seg_hashes.append(checkpoint.prompt_hash(tag))

        exec_hashes = list(seg_hashes)   # 执行序列 = 提示词段按序，没有插入槽要单独指纹
        prologue_hash = None
        if 起始视频 is not None:
            f0 = 起始视频[0].detach().float().cpu()
            prologue_hash = checkpoint.prompt_hash(
                f"prologue:{int(起始视频.shape[0])}:{float(f0.mean()):.4f}:{float(f0.std()):.4f}")
        root, manifest, done, seeds = None, None, 0, []
        proj_title, proj_created, proj_finals = "", None, []
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
            if manifest is not None:
                if manifest.get("schema") != checkpoint.SCHEMA:
                    raise ValueError(f"存档目录格式不认识（{manifest.get('schema')}），请换一个目录名；"
                                     "旧版 v1 存档不兼容本版本，请清空旧目录或换新名字")
                _pnotes = checkpoint.assert_match(manifest["params"], ckpt_params)
                if _pnotes:
                    report.append("参数已变更（" + "；".join(_pnotes) + "）——"
                                  "仅影响此后新生成的段；已有段是盘上张量、与采样参数无耦合，"
                                  "不会因此重做")
                if 起始视频 is None and manifest.get("has_prologue"):
                    off = 1  # 输入已断开仍沿用存档序章（LoadVideo 可 bypass），哈希校验跳过
                elif 起始视频 is not None and not manifest.get("has_prologue"):
                    manifest = checkpoint.truncate(root, manifest, 0)
                    report.append("存档续跑：检测到新接入的序章视频，整链重做")
                elif 起始视频 is not None and prologue_hash is not None:
                    stored = list(manifest.get("prompt_hashes", []))
                    if stored and stored[0] != prologue_hash:
                        manifest = checkpoint.truncate(root, manifest, 0)
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
                # 「重跑起始段」为 1-based 段号（与 tooltip 一致）：N=从第 N 段起重做。
                # 语义 = **用户主动指定的显式区间 [N, 末尾)**，级联重做（唯一还走 truncate
                # 的路径之一；另两条是分辨率变更与序章变更）。
                #
                # 内容变更（提示词/时长/段级引用/unlink/anchor/素材）走**区间模型**：
                # 重做最小单位 = 单段，用双锚对齐上下邻居（上锚=段 N-1 存档 latent 尾部，
                # 下锚=段 N+1 存档 latent 头部），所以**不截断整链、下游不动**。
                # 落地方式复用既有的重摇通道（redo_map）：把变更段标成"双锚"即可，
                # 不必新造一套执行机制——重摇本来就是"重建某段 + 锚定邻居"。
                if reroll > 0:
                    start = min(max(reroll - 1, 0), done)
                    if start < done:
                        manifest = checkpoint.truncate(root, manifest, start)
                        done = start
                        # 截断后 done 变小：越界的重摇标记槽位作废（truncate 已联动清队列）
                        _redo_ds = [x for x in _redo_ds if x[0] < done]
                        report.append(f"存档续跑：手动指定「重跑起始段」，从段 {start + 1} 起重新生成"
                                      + (f"（段 1-{start} 沿用存档）" if start else "（整链重做）"))
                else:
                    # 段数变少（删了段）→ 多余槽位作废，交给 truncate
                    if len(full_hashes) < done:
                        manifest = checkpoint.truncate(root, manifest, len(full_hashes))
                        done = len(full_hashes)
                        _redo_ds = [x for x in _redo_ds if x[0] < done]
                        report.append(f"存档续跑：段数由 {len(hashes)} 减为 "
                                      f"{len(full_hashes)}，尾部槽位作废")
                    _ivs = anchors.change_intervals(hashes, full_hashes, done)
                    if _ivs:
                        _marked = {off + _s for _a, _b in _ivs
                                   for _s in range(_a, _b + 1)}
                        _redo_ds.extend((s, "双锚") for s in sorted(_marked))
                        report.append("存档续跑：检测到内容变更 → 只重建 " + "、".join(
                            (f"段{_a + 1}" if _a == _b else f"段{_a + 1}-{_b + 1}")
                            for _a, _b in _ivs)
                            + "（双锚对齐上下邻居，其余段与下游均沿用存档）")
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

        # ---- 资产加载与锚解析（root 已确定）：引用集张量 + 标注锚 + 身份锚编码 ----
        def _project_root_for_assets():
            custom = 存档目录.strip()
            if custom:
                return os.path.join(checkpoint.projects_root(), custom)
            if use_ckpt and root is not None:
                return root
            return None

        def _ensure_pool_tensors():
            """加载引用集张量（幂等：已加载标签跳过）。assets/ 文件需项目目录。

            P2 三级寻址 + P3 修正：注册表优先（by_id 命中 asset_id 标签、
            by_alias 命中 label/alias），三级 resolve 命中则经 _decode_*_file
            直接解码；未命中才走 pool 文件名旧路径。命中只影响文件来源，
            不改变张量格式与报错点名。
            """
            _proot = _project_root_for_assets()
            _reg_ld, _libroot = _asset_registry()
            for _k, _lbl in sorted(_needed):
                if _lbl in _pool_loaded or _lbl in pool_tensors.get(_k, {}):
                    _pool_loaded.add(_lbl)
                    continue
                _fn = pool_file_of.get(_lbl)
                _abs = None
                if _AS is not None:
                    _rec = ((_reg_ld.get("by_id") or {}).get(_lbl)
                            or (_reg_ld.get("by_alias") or {}).get(_lbl))
                    if _rec is not None:
                        _abs = _AS.resolve_absolute(_rec, _proot, _libroot)
                if _abs is None and _fn is None:
                    continue   # 防御：组装期未知标签已报错，到这里不可能缺失
                try:
                    if _abs is not None:
                        if _k == "image":
                            pool_tensors["image"][_lbl] = _decode_image_file(_abs)
                        elif _k == "video":
                            pool_tensors["video"][_lbl] = _decode_video_file(_abs, _lbl)
                        else:
                            pool_tensors["audio"][_lbl] = _decode_audio_file(_abs)
                        if _lbl not in _store_hits:
                            _store_hits.append(_lbl)
                    elif _k == "image":
                        pool_tensors["image"][_lbl] = _load_input_image(_fn, _proot)
                    elif _k == "video":
                        pool_tensors["video"][_lbl] = _load_input_video(_fn, _proot)
                    else:
                        pool_tensors["audio"][_lbl] = _load_input_audio(_fn, _proot)
                except ValueError:
                    raise
                except Exception as e:
                    _src_hint = "（全局库文件请确认仍在原位）" if _abs is not None else (
                        "（项目资产库文件请确认项目名正确）" if str(_fn).startswith("assets/") else
                        "（input 目录文件请确认仍在原位）")
                    raise ValueError(f"素材「{_lbl}」加载失败（文件 {_abs or _fn}）：{e}——请确认文件可解码"
                                     + _src_hint)
                _pool_loaded.add(_lbl)

        def _load_anchor_image_file(fn, lbl=""):
            if lbl and _AS is not None:
                try:
                    _reg_a, _libroot_a = _asset_registry()
                    _rec_a = ((_reg_a.get("by_alias") or {}).get(lbl)
                              or (_reg_a.get("by_id") or {}).get(lbl))
                    if _rec_a is not None:
                        _abs_a = _AS.resolve_absolute(
                            _rec_a, _project_root_for_assets(), _libroot_a)
                        if _abs_a is not None:
                            if lbl not in _store_hits:
                                _store_hits.append(lbl)
                            return _decode_image_file(_abs_a)
                except Exception:
                    pass
            return _load_input_image(fn, _project_root_for_assets())

        _ensure_pool_tensors()
        # 段级首尾帧参考图（提示词框按钮，新入口）优先：首段首帧图 = i2v 起手帧、
        # 末段尾帧图 = FL2VA 剧情终点锚；其次才是库内旧标注锚（旧链零变化）
        if 首帧图片 is None and _head_seg:
            首帧图片 = _load_input_image(_head_seg, _project_root_for_assets())
            report.append(f"段1 首帧图「{_store_short(_head_seg)}」→ i2v 起手帧")
        if 尾帧图片 is None and _tail_seg:
            尾帧图片 = _load_input_image(_tail_seg, _project_root_for_assets())
            report.append(f"末段尾帧图「{_store_short(_tail_seg)}」→ FL2VA 剧情终点锚")
        # 库内旧标注锚汇入旧槽位（槽位有值则优先）
        if 首帧图片 is None and _head_lib is not None:
            首帧图片 = _load_anchor_image_file(pool_file_of[_head_lib], _head_lib)
            report.append(f"资产标注「首帧图」→「{_store_short(_head_lib)}」（链首锚来源）")
        if 尾帧图片 is None and _tail_lib is not None:
            尾帧图片 = _load_anchor_image_file(pool_file_of[_tail_lib], _tail_lib)
            report.append(f"资产标注「尾帧图」→「{_store_short(_tail_lib)}」（链尾锚来源）")
        # 其余段（含首/末段自身的段头/段尾锚）：按文件缓存编码，各文件只编一次
        _frm_cache = {}

        def _frame_img_latent(fn):
            if not fn:
                return None
            if fn not in _frm_cache:
                try:
                    _im = _load_input_image(fn, _project_root_for_assets())
                except Exception as e:
                    raise ValueError(f"段级首尾帧参考图「{fn}」加载失败：{e}")
                _frm_cache[fn] = video_vae.encode(_center_cover(_im[:1], width, height))
            return _frm_cache[fn]

        seg_head_img_latent = [None] * len(seg_prompts)
        seg_end_img_latent = [None] * len(seg_prompts)
        for _i, (_f, _e) in enumerate(seg_frame_imgs):
            if _i >= len(seg_prompts):
                break
            if _f and not (_i == 0 and _head_seg):
                seg_head_img_latent[_i] = _frame_img_latent(_f)
            if _e and not (_i == len(seg_prompts) - 1 and _tail_seg):
                seg_end_img_latent[_i] = _frame_img_latent(_e)
        if any(seg_head_img_latent) or any(seg_end_img_latent):
            _fs = [str(i + 1) for i, v in enumerate(seg_head_img_latent) if v is not None]
            _es = [str(i + 1) for i, v in enumerate(seg_end_img_latent) if v is not None]
            report.append("段级首尾帧参考图："
                          + ("、".join(f"段{n}头锚" for n in _fs) if _fs else "")
                          + (" · " if _fs and _es else "")
                          + ("、".join(f"段{n}尾锚" for n in _es) if _es else "")
                          + "（身份锚 keyframe 注入）")

        if 每段尾帧锚定 is not None:
            tail_anchor_latent = video_vae.encode(_center_cover(每段尾帧锚定[:1], width, height))
            report.append(f"每段尾帧锚定：身份锚点注入末帧 keyframe（{'视觉保真 ' + format(1.0 - aug, '.2f') if aug > 0 else '硬锚定'}）")

        # 尾帧图片（FL2VA 剧情终点）：编码后注入勾选了尾帧图的段末帧 keyframe；
        # 勾了尾帧图的段不再叠加尾锚（同一位置只有一个锚，剧情终点优先）
        if 尾帧图片 is not None:
            end_frame_latent = video_vae.encode(_center_cover(尾帧图片[:1], width, height))
            _end_segs = [str(i + 1) for i in range(len(seg_prompts)) if seg_end_on[i]]
            report.append(f"尾帧图片：FL2VA 剧情终点 → 段{'、'.join(_end_segs)} 末帧 keyframe"
                          + ("（这些段不叠加段尾锚）" if tail_anchor_latent is not None else ""))

        # 首帧图片（段级引用）：任一勾了首帧图的段都编码为头锚 latent —— 注入段头
        # keyframe 作为身份锚（同 E2 记忆锚段首注入模式），抑制长链漂移。
        # 段 0 也一样要编：它走 Ref2VA（段里有素材引用）时官方入口**没有
        # first_frame 参数**，不补这个 keyframe 首帧图就彻底失效（老 bug）。
        if 首帧图片 is not None and any(seg_first_on):
            head_frame_latent = video_vae.encode(_center_cover(首帧图片[:1], width, height))

        if any(seg_fr_explicit):
            _fr_txt = []
            if has_first_eff and any(seg_first_on):
                _fr_txt.append("首帧图→段" + "、".join(
                    str(i + 1) for i in range(len(seg_prompts)) if seg_first_on[i])
                    + "（首段=i2v 起手，中段=头部身份锚）")
            if has_end_eff and any(seg_end_on):
                _fr_txt.append("尾帧图→段" + "、".join(
                    str(i + 1) for i in range(len(seg_prompts)) if seg_end_on[i]))
            report.append("首尾帧图段级引用：" + " · ".join(_fr_txt)
                          + "；未勾段尾部锚回落段 tail_src/旧尾帧锚定")
        def _anchor_tail(pi):
            """段 pi 的尾锚 latent：本段 tail anchor（src.kind image / library）解析结果。

            执行期优先级仍是「段级尾帧图 > 链级尾帧图 > 尾锚」——调用方按这个顺序取用
            （见主循环 _tail_kf）。没有尾锚返回 None，**不再回落旧全局尾帧锚定**：
            那个全局槽已被迁移展开成各段的 tail anchor（规划 §7）。解析失败直接抛。
            """
            for _a in (seg_anchors[pi] if 0 <= pi < len(seg_anchors) else []):
                if _a["on"] and _a["at"]["mode"] == "tail":
                    _ev, _ = _resolve_anchor_latent(_a)
                    return _ev
            return None

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

        prev_end_t = None   # 上一处理生成段的输出末端 token 边界（重摇 unlink 段上锚对齐用；序章重置 None）

        # 三库携带：主循环的 manifest 全量覆写必须带上三库/文本键，否则一次运行
        # 就把资产库/latent库/剪辑登记冲掉（旧 bug）。auto_latents 为本轮内存表。
        _CARRY_KEYS = ("assets", "latents", "clips", "merges", "seg_fields")
        _carry = {k: list((manifest.get(k) or []))
                  for k in _CARRY_KEYS} if isinstance(manifest, dict) else \
            {k: [] for k in _CARRY_KEYS}
        auto_latents = list(_carry.get("latents") or [])

        def _auto_latent_save(g, pi, video_t, audio_t, frames=None):
            """分段 latent 自动保存（内存切片直存，无需回读 seg .pt）。

            策略 seg_latent_save[pi]：None=默认全存（mode=all）；mode=off 或
            save_seg=false=跳过；range=[start_f,end_f) 钳到本段可见窗；
            tail=可见窗尾部 tail_f 帧；split_av=true 落 _v/_a 两份。
            幂等覆盖同名文件；登记进 auto_latents（主循环 manifest 写盘时带回）。
            """
            if not use_ckpt or root is None:
                return
            ls = seg_latent_save[pi] if 0 <= pi < len(seg_latent_save) else None
            mode = str((ls or {}).get("mode") or "all")
            if (ls or {}).get("save_seg") is False or mode == "off":
                return
            try:
                import torch as _t
            except Exception:
                return
            try:
                total_t = int(video_t.shape[2])
                sampled_fc = latent_t_to_frames(total_t)
                # 本段可见窗（采样坐标系）：skip 与 vis 由调用方传入——此处重算
                # 简化为全采样窗（调用方在裁剪后传 vis 起点？保持简单：全窗）。
                # 精确窗由调用方参数给出，见下方调用点注释。
                w0, w1 = 0, sampled_fc
                if mode == "tail":
                    try:
                        tf = max(0, int((ls or {}).get("tail_f") or 0))
                    except (TypeError, ValueError):
                        tf = 0
                    if tf > 0:
                        w0 = max(0, w1 - tf)
                elif mode == "range":
                    try:
                        rs = max(0, int((ls or {}).get("start_f") or 0))
                        re = max(0, int((ls or {}).get("end_f") or 0))
                    except (TypeError, ValueError):
                        rs, re = 0, 0
                    if re > rs:
                        w0, w1 = max(0, rs), min(sampled_fc, re)
                        if w1 <= w0:
                            return
                t0 = max(0, frames_to_latent_t(w0, up=False))
                t1 = min(total_t, frames_to_latent_t(w1, up=True))
                if t1 <= t0:
                    t0 = max(0, min(total_t - 1, frames_to_latent_t(w0, up=True) - 1))
                    t1 = min(total_t, t0 + 1)
                cut_v = video_t[:, :, t0:t1].detach().cpu().contiguous().clone()
                cut_a = None
                if audio_t is not None and getattr(audio_t, "dim", lambda: 0)() == 4:
                    a_total = int(audio_t.shape[-1])
                    a0 = max(0, int(round(w0 / max(1, sampled_fc) * a_total)))
                    a1 = max(a0 + 1, min(a_total, int(round(w1 / max(1, sampled_fc) * a_total))))
                    cut_a = audio_t[..., a0:a1].detach().cpu().contiguous().clone()
                split = bool((ls or {}).get("split_av"))
                ldir = os.path.join(root, "latent")
                os.makedirs(ldir, exist_ok=True)
                stamps = []
                if split and cut_a is not None:
                    stamps = [(f"seg{g:03d}_auto_v.pt", {"video": cut_v}, "video"),
                              (f"seg{g:03d}_auto_a.pt", {"audio": cut_a}, "audio")]
                else:
                    pay = {"video": cut_v}
                    if cut_a is not None:
                        pay["audio"] = cut_a
                    stamps = [(f"seg{g:03d}_auto.pt", pay, "av")]
                for fn, pay, kd in stamps:
                    _t.save(pay, os.path.join(ldir, fn))
                    auto_latents[:] = [x for x in auto_latents
                                       if not (isinstance(x, dict)
                                               and x.get("file") == f"latent/{fn}")]
                    # contact sheet 同步生成（规划 §9）：此刻源帧已在内存，抽 12 张拼长条
                    # 几乎不耗时；时间线只需加载这一张小图就能秒开，不必加载 VAE、不必
                    # 解码 latent。生成失败就留空 → 前端自动降到「刻度条 + 生成预览」。
                    _sheet_rel = None
                    _tiles = 0
                    if frames is not None:
                        from . import library as _h3lib
                        _sp = _h3lib.sheet_path_for(os.path.join(ldir, fn))
                        if _h3lib.make_sheet(frames, _sp):
                            _sheet_rel = f"latent/{os.path.basename(_sp)}"
                            _tiles = _h3lib.SHEET_TILES
                    auto_latents.append({"file": f"latent/{fn}", "src": f"seg_{g:03d}.pt",
                                         "start_f": w0, "end_f": w1, "kind": kd,
                                         "tokens": [t0, t1], "auto": True,
                                         "fps": 24.0, "w": int(width), "h": int(height),
                                         "frames": max(0, int(w1) - int(w0)),
                                         "sheet": _sheet_rel, "tiles": _tiles,
                                         "updated_at": time.time()})
            except Exception as e:
                report.append(f"段{g + 1} latent 自动保存跳过（{type(e).__name__}: {e}）")

        def _redo_prev_bridge(g):
            """重摇独立镜头段的上锚现算：前段（g-1）存档 latent 尾部切桥。

            unlink 段的前段没为它留 guide（next_wants_bridge=False，guide 变量
            陈旧），需直读前段存档；前段也在重摇列表时其新 latent 已先落盘
            （顺序处理），载入即最新。前段是序章时 prev_end_t=None
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
            # 重摇段本体的注入帧数（分段优先）：for_pi=g 对应的提示词段
            _fpi = exec_items[g - off][1] if 0 <= g - off < len(exec_items) \
                and exec_items[g - off][0] == "prompt" else -1
            return _inject_guide(pv.to(video_vae.device), pa.to(audio_vae.device),
                                 _fpi, end_tokens=prev_end_t)

        def _redo_next_anchor(g):
            """重摇段的下锚现算：下一段保留段存档 latent 裁头后首 token。

            下段不存在/unlink 段/禁用段/未完成/也在重摇列表 → None（该侧
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
            _nfr, _, _ = _eff_inject(idx)
            skip2 = 0 if _is_fact_first(ni) else _nfr
            kf = _seam_tail_kf(nv, skip2)
            return kf.to(video_vae.device) if kf is not None else None

        def _up_hi(g, video_t, audio_t, kind, idx, guide_kf, tail_kf, head_kf, cur_seed,
                   skip_f, vis_len, wav, rate):
            """二采渲染段 g 并落盘高清产物（仅生成段；序章等外部素材段跳过）。

            返回 (ready, tried)：ready=True 表示高清产物已在盘（本次渲染成功或
            记录有效沿用——调用方跳过基础分辨率保存，段视频就是二采结果）；
            ready=False 且 tried=True 表示渲染失败（调用方基础保存强制重编码，
            覆盖可能写坏的 mp4 自愈）；范围外/关闭/外部素材段 (False, False) 正常基础保存。
            proj_upscale 原地更新（写盘最新 upscale 存档状态）。
            """
            nonlocal proj_upscale
            if not (up_cfg and use_ckpt):
                return False, False
            # 外部素材段（序章）不做二采：内容本就是成品视频，VAE 重编码
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
            # 换过二采模型时提示（只提示，不参与上面的 record_stale 判定）
            _mm = upscale.model_mismatch_note(manifest, g, _up_tag)
            if _mm:
                report.append(_mm)
            if not upscale.record_stale(manifest, root, up_cfg, g, bh):
                return True, False
            if _up_swap:
                # 二采是另一份权重：先把一采模型（连带 CLIP/VAE）整块卸回 CPU，
                # 再交给 render_segment。换页次数并不因此减少（下段一采仍要载回），
                # 收益只在两处：①空闲显存是一整块，ComfyUI「够用就停」不会留下
                # 一采残部造成碎片；②preflight 量到的空闲对应真实状态，账目可信。
                # 顺序同其它腾挪点：unload → gc → empty_cache（反序收不回）。
                try:
                    import comfy.model_management
                    comfy.model_management.unload_all_models()
                    gc.collect()
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                    if not _up_swap_logged[0]:
                        _up_swap_logged[0] = True
                        print(f"[H3二采] 段{g + 1}：二采为独立模型，已先卸载一采模型腾整块显存"
                              "（每段 2 次 UNET 级换页，属预期代价）", flush=True)
                except Exception:
                    pass
            try:
                # 返回 (高清尾帧 CPU tensor, 最新存档状态)：尾帧暂无消费方
                # （跨段连续性仍走基础 latent 桥），只回填存档状态进 manifest
                # 外层再套一层「权重回载计数」：验证同 base 只差 LoRA 是否真的零换页
                with perf.watch_model_loads() as _loads:
                    _, proj_upscale = upscale.render_segment(
                        _up_model, clip, video_vae, audio_vae, negative, up_cfg, up_net,
                        root, g, video_t, audio_t, kind, idx,
                        seg_prompts, seg_label_orders, pool_tensors, refs,
                        首帧图片 if (kind == "prompt" and idx == 0 and seg_first_on[0]) else None,
                        guide_kf, tail_kf, head_kf, cur_seed,
                        skip_f, vis_len,
                        wav, rate, bh, report, 采样器, 调度器, model_tag=_up_tag,
                        bridge=_bridge_cfg, _up_swap=_up_swap)
                if not _reload_logged[0]:
                    _reload_logged[0] = True
                    # ⚠ 窗口包住的是整个 render_segment，n 里混着 TE（建高清 cond）
                    # 与 VAE（解码）的回载——那两次是代码主动触发的既定流程，
                    # 不是「换 LoRA 导致重传」的证据。判定只看 UNET 回载次数：
                    # 第 1 次是精化前 unload_all_models() 之后必然的回载，
                    # ≥2 次才是真的在重传。
                    _unet_cls = getattr(getattr(_up_model, "model", None),
                                        "__class__", None)
                    _cls = perf.classify_loads(
                        _loads, getattr(_unet_cls, "__name__", None))
                    n = _cls["total"]
                    _un = _cls["unet_loads"]
                    _detail = "，".join(f"{k}×{v}" for k, v in _cls["by_model"].items())
                    if _up_swap:
                        _verdict = (f"A≠B：UNET 级换页属已知代价（观测 {n} 次：{_detail}）"
                                    f"「两遍式」有价值（2N → 1）")
                    elif _un >= 2:
                        _verdict = (f"UNET 回载 {_un} 次（>1）→ 同 base 换 LoRA 确实在重传，"
                                    f"「两遍式」重新有价值（2N → 1）")
                    else:
                        _verdict = (f"观测 {n} 次回载（{_detail}），其中 UNET 仅 {_un} 次"
                                    f"——TE/VAE 与首次 UNET 回载均为本段既定流程，"
                                    f"**不构成**重传证据，「两遍式」仍需单独实测")
                    print(f"[H3性能] 段{g + 1} 二采入口换页探测（A≠B={_up_swap}）：{_verdict}",
                          flush=True)
                    for _m in _loads[:4]:
                        print(f"[H3性能]   ↳ {_m}", flush=True)
                    perf.emit({"kind": "swap_probe", "seg": g + 1,
                               "up_swap": bool(_up_swap), "loads": n,
                               "unet_loads": _un, "by_model": _cls["by_model"],
                               "verdict": _verdict})
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
                # 放大网络可能已在 CPU（render_latent 内 net.cpu()），下段二采自愈装回 GPU。
                # 注意 soft_empty_cache() 只归还缓存块、**不卸载权重**：A≠B 时精化用的
                # B 会滞留在场，紧接着下段一采要载 A → 两个 UNET 同时在册（ComfyUI
                # load_models_gpu 会兜底，不会出错，但多一次换页 + 显存碎片）。
                # 故 A≠B 时补一次 unload_all_models()；A==B 时 B 就是 A，滞留正是想要的，
                # 卸了反而白付一次回载。顺序同其它腾挪点：unload → gc → empty_cache。
                try:
                    import comfy.model_management
                    if _up_swap:
                        comfy.model_management.unload_all_models()
                    comfy.model_management.soft_empty_cache()
                    gc.collect()
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                return False, True

        # ---- 主循环状态容器 ----
        # ⚠ 这一整块在 cc93c7d「手动锚定步骤 6 主编排切换」重构中被整体删除过，
        # 导致 `total` / `prompt_list` / `thumbs` / `pbar` 等十余个名字全部 NameError ——
        # 任何带存档的运行都必然崩在下面第一行 save_state（`total` 未定义），
        # 且修完一个还会撞上下一个。已加 tests/test_no_undefined_names.py 守住。
        # total 口径 = 执行序列 + 序章槽，与「存档续跑」报告行、_off_slots 三处一致。
        pbar = comfy.utils.ProgressBar(len(exec_items))
        total = len(exec_items) + off
        prompt_list = (["「序章（上传视频）」"] if off else []) + [
            seg_prompts[it[1]] for it in exec_items]
        thumbs, videos, seams, bridge_scores = [], [], [], []
        all_frames = []
        seg_frames = []
        seg_wavs = []
        trims = []
        seam_metrics_rows = []   # 每缝五维 z-score（与 seams 列表对齐；无缝/指标不可用为 None）
        # 段首接片的**交棒位**：开局摆一块写着「无」的牌子。链首（没接起始视频、
        # 当然也没有上段）本来就无片可接，但这个名字**必须存在**——否则第一段取片
        # 时直接 UnboundLocalError、整链停摆（`cc93c7d` 删掉过这一行，见 2026-09-20 日志）。
        # 之后只由「本段收工、下一个会上链的段要取片」那一处覆写。
        guide = None
        # 段间衔接的三件「上段尾」快照 + 成片音轨累积：`cc93c7d` 把这几行**一起删过**
        # （于是只剩序章分支里赋值）→ 没接起始视频时第 1 段一读就崩：
        # 段循环里两处读它：prev_tail_frame（接缝帧差/响度）与 prev_tail_clip
        # （接缝五维 z 观测）。语义与 guide 一样：链首本来就"没有上段"。
        prev_tail_frame = None   # 上段末帧（成片顺序）
        prev_tail_clip = None    # 上段末 24 帧（接缝五维 z 用）
        prev_tail_wav = None     # 上段末 0.25s 音频（接缝测量/响度对齐用）
        all_wav = None           # 成片音轨累积（首段直接取本段）

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
                _chain_ref[0] = pv
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
                        "seam_metrics": [None], "seam_refine": seam_refine,
                        "upscale": proj_upscale,
                        "redo_queue": list(redo_queue),
                        "assets": list(_carry.get("assets") or []),
                        "latents": list(auto_latents),
                        "clips": list(_carry.get("clips") or []),
                        "merges": list(_carry.get("merges") or []),
                        "seg_fields": list(_carry.get("seg_fields") or [])})
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
            all_frames.append(_frames_to_uint8(_cf) if _frames_uint8 else _cf)
            seg_frames.append(_cf)
            seg_wavs.append({"waveform": pwav.cpu(), "sample_rate": sample_rate})
            all_wav = pwav.cpu()
            prev_tail_frame = pframes[-1].cpu()
            prev_tail_clip = pframes[-24:].cpu()
            seam_n0 = max(1, int(sample_rate * 0.25))
            prev_tail_wav = pwav.cpu()[..., -seam_n0:]
            _profpi = exec_items[0][1] if exec_items and exec_items[0][0] == "prompt" else -1
            guide = _inject_guide(pv, pa, _profpi)
            report.append(f"段1/{total}：{prologue_origin} 序章 留{pframes.shape[0]}帧 · 种子 — | guide=无（序章）")

        # 每段耗时账（⏱ 报告行数据源）：cond=一采条件构建（TE/参考编码）、
        # sample=一采采样（重摇各次尝试累计）、decode=基础解码裁剪（含重摇重复解码）
        _seg_t = {"cond": 0.0, "sample": 0.0, "decode": 0.0}

        def _decode_crop(i, video_t, audio_t, skip_f, gi, next_bridge):
            """解码 → 桥帧门控 → 尾切 token 对齐 → 裁剪到保留区（重摇与正常路径共用）。

            gi=全局段号（混排链位+off，报告用）；next_bridge=下一段是否接收本段尾帧桥
            （下段是独立镜头/末段时为 False：门控是为下段桥服务的，
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
            序章存在或前方有启用的提示词段 → 非事实首段（有锚定来源）；
            禁用段不算已执行段——禁用段 0 时段 1 成为事实首段（不裁头不锚定）。"""
            if off:
                return False
            for j in range(ni):
                if not seg_disabled[exec_items[j][1]]:
                    return False
            return True

        def _next_consumer(item_i):
            """item_i 之后第一个真正会上链执行的提示词段索引；没有 → -1。

            交棒对象是**下一个会取用接片的段**，而不是"紧邻的下一个 item"：
            中间夹着禁用段时，接片要由本段的 latent 直接交给禁用段之后那一段
            （否则窗宽会按禁用段的配置算，接片宽度与实际相邻段对不上）。
            """
            for _it in exec_items[item_i + 1:]:
                if _it[0] == "prompt" and not seg_disabled[_it[1]]:
                    return _it[1]
            return -1

        def _next_wants_bridge(item_i):
            """下一段是否接收本段尾帧桥——**只用于桥帧门控与裁帧**（视频口径）。

            裁头/门控/接缝测量都按视频帧算，故纯音频锚不算「要桥」。
            ⚠ 接片交棒（算 `guide`）**不要**用它，用 `_next_consumer` + `_takes_bridge`：
            那才是取用端的口径，两者不是同一个问题。
            """
            nxt = exec_items[item_i + 1] if item_i + 1 < len(exec_items) else None
            if not (nxt and nxt[0] == "prompt" and not seg_unlink[nxt[1]]):
                return False
            fr, want_v, _w = _eff_inject(nxt[1])
            return bool(fr > 0 and want_v)

        for item_i, item in enumerate(exec_items):
            i = item[1]   # 提示词段索引（seg_lengths/seg_unlink/seg_disabled/seg_label_orders 均按此索引）
            prompt = seg_prompts[i]
            g = item_i + off  # 全局段下标（有序章时序章占 0 号）
            # 段禁用（不上链）：槽位占位跳过——不采样/不解码/不进成片；manifest
            # 记录类列表沿用旧记录（无则空占位）保槽位不错位；done 照常推进
            #（禁用段视作已处理；段文件缺失由下方 replay 存在性守卫兜底，重新
            # 上链时自动采样）；禁用段整轮 continue、不经手接片——交棒交给
            # `_next_consumer` 越过它、直接算给下一个会执行的段（与成片实际顺序一致）
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
                    seeds.append(0)   # 种子占位（不执行不消费）
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
            # 本段是否接收上段桥：事实首段没有上源；_eff_inject 已内含 unlink（返回 0）。
            _eff_fr, _has_v, _has_a = _eff_inject(i)
            _has_src = not _is_fact_first(item_i) and _eff_fr > 0 and (_has_v or _has_a)
            # skip_f 与采样额外帧数一次定死：回放分支与生成分支共用同一个值
            # （有源 = 整段 ctx 帧烧进引导桥、可见帧从第 ctx 帧起算；无源 = 两者皆为 0）。
            # 分段优先：本段注入帧数（latent_ref.frames 覆盖全局 ctx）。
            skip_f = _seg_extra = int(_eff_fr) if _has_src else 0
            # 首帧图段级引用（中段）：头锚 latent——生成段注入 cond，回放段二采补渲染同用
            #（定义在 replay 分支之前：两路都消费；独立镜头段照常注入，本段主动锚）
            # 段级首帧参考图优先（本段自己指定的图），否则回落到链级首帧图头锚
            _head_kf = (seg_head_img_latent[i] if i < len(seg_head_img_latent) else None) \
                or (head_frame_latent
                    if (seg_first_on[i] and head_frame_latent is not None
                        # 段 0 无素材引用时走官方 first_frame= 参数（I2VA 语义最正），
                        # 别再叠一个帧 0 keyframe；有素材（Ref2VA）才补 keyframe，
                        # 否则首帧图在混合模式里根本不生效。
                        and (i > 0 or _seg0_has_refs)) else None)
            # 段级尾帧参考图（本段自己指定的图）：与 `_head_kf` 同款——**定义在 replay
            # 分支之前，两路都消费**。生成段拿它当尾锚；回放段二采补渲染时也必须取同一
            # 片，否则二采尾锚与基础链不一致，基础/高清接缝行为漂移（与下方二采取用
            # 口径同一条规矩）。原先只在生成分支里赋值，回放分支读它就是 UnboundLocal。
            _seg_end_img = seg_end_img_latent[i] if i < len(seg_end_img_latent) else None
            _seg_t.update(cond=0.0, sample=0.0, decode=0.0)
            if replay:
                video_t, audio_t = checkpoint.load_segment(root, g)
                video_t = video_t.to(video_vae.device)
                _chain_ref[0] = video_t
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
                # 本段采样长度 = 可见段长 + 引导桥额外帧数（无源时 _seg_extra=0）
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
                    # P4d：画布接线已删除，无未被接管类别（旧合并循环已删）
                    _t = time.perf_counter()
                    out = MiniMaxH3ReferenceToVideo.execute(
                        clip=clip, vae=video_vae, audio_vae=audio_vae,
                        prompt=prompt, width=width, height=height, length=seg_len,
                        ref_image_size=参考图像尺寸, **seg_refs)
                else:
                    _t = time.perf_counter()
                    out = MiniMaxH3ImageToVideo.execute(
                        clip=clip, vae=video_vae, prompt=prompt,
                        width=width, height=height, length=seg_len,
                        first_frame=首帧图片 if (i == 0 and seg_first_on[0]) else None)
                _seg_t["cond"] += time.perf_counter() - _t

                cond, latent = out[0], out[1]
                # 语义桥（FourBunny BUNNY H3 Conditioning Bridge 内联版）：只改 cond 的
                # 文本张量、不碰元数据 —— 与下面「锚定来源」那段注入正交，所以放在注入
                # 之前还是之后结果逐字节相同（放这里只是读起来离 cond 诞生点最近）。
                cond = semantic_bridge.apply(cond, _bridge_cfg)
                _chain_ref[0] = latent
                # 锚定来源：普通段 = 段属性（unlink 屏蔽上桥、尾帧图/每段尾帧锚定收尾）；
                # 重摇段 = 四种锚定模式（本次重做的临时策略，独立于段属性 unlink）——
                # 显式身份锚（尾帧图/每段尾帧锚定/首帧图）不受模式影响，模式只控制接缝锚
                # 段级尾帧参考图优先（本段自己指定的图），其次链级尾帧图，再回落段尾锚
                # （`_seg_end_img` 已在上方 replay 分支之前取好，两路共用，此处不再重复赋值）
                if _redo_mode is not None:
                    _user_tail = _seg_end_img or (
                        end_frame_latent
                        if (seg_end_on[i] and end_frame_latent is not None) else _anchor_tail(i))
                    if _redo_mode in ("双锚", "仅锚上段"):
                        # 断链（且无显式头锚）的段交棒端判「不取片」→ 手上没有为它
                        # 现算的新接片，需直读前段存档；其余段用本段该取的那一片。
                        eff_guide = _redo_prev_bridge(g) if seg_unlink[i] else (
                            guide if _takes_bridge(i) else None)
                    else:
                        eff_guide = None
                    if _user_tail is not None:
                        _tail_kf = _user_tail
                    elif _redo_mode in ("双锚", "仅锚下段"):
                        _tail_kf = _redo_next_anchor(g)   # 下段不可锚自动缺省 None
                    else:
                        _tail_kf = None
                else:
                    # 取用口径与交棒口径同一个（`_takes_bridge`）：`guide` 里那一片
                    # 就是**为这一段算的**（交棒端判过"它会取片"才更新）。所以这里
                    # 不再二次判 seg_unlink —— 那会把显式 head 锚一起屏蔽掉
                    # （unlink 只是"不自动引用上段"，`_eff_inject` 里显式锚优先）。
                    # 每段尾帧锚定是用户主动设定的本段结尾身份锚点，不受断链影响。
                    eff_guide = guide if _takes_bridge(i) else None
                    # 尾帧图片（FL2VA 剧情终点/段级尾锚）：勾了尾帧图的段末帧 keyframe
                    # = 尾帧图 latent（同位置唯一锚，优先于段尾锚）；未勾段回落
                    # 段 tail_src（资产图/latent），再回退旧全局尾帧锚定
                    _tail_kf = _seg_end_img or (
                        end_frame_latent if (seg_end_on[i] and end_frame_latent is not None)
                        else _anchor_tail(i))
                if eff_guide is not None or _tail_kf is not None or _head_kf is not None \
                        or any(a["on"] for a in (seg_anchors[i] if 0 <= i < len(seg_anchors)
                                                 else [])):

                    # 手动锚（seg.anchors）与自动锚（段首默认桥 / 首帧头锚 / 尾锚）在这里汇成
                    # 一张 keyframe 列表，交给 _apply_guide 一次性注入。落点解析统一走
                    # grid.anchor_frame_index + guides.prepare_anchor（负值自尾部计数只在
                    # guides 定义一处），越界在这里硬拦——不再有"只报不拦"的软降级。
                    _kfs = []
                    _kf_fc = latent_t_to_frames(latent["samples"].tensors[0].shape[2])
                    if eff_guide is not None:
                        _kfs.append(eff_guide)
                    if _head_kf is not None:
                        _kfs.append({"resolved_frame_index": 0, "latent": _head_kf})
                    if _tail_kf is not None:
                        _kfs.append({"resolved_frame_index": _kf_fc - 1, "latent": _tail_kf})
                    for _a in (seg_anchors[i] if 0 <= i < len(seg_anchors) else []):
                        # head 模式已由 eff_guide（显式 head anchor）承担，这里只处理 mid/tail，
                        # 否则同一条锚会被注入两次。
                        if not _a["on"] or _a["at"]["mode"] == "head":
                            continue
                        _av, _aa = _resolve_anchor_latent(_a)
                        # 分支三态在段中/段尾同样生效（此前只在段首桥尊重，
                        # 「仅图像」会把音频也钉进去、「仅音频」会连视频一起钉）
                        _want_v = _a["branches"]["av"] in ("both", "video")
                        _want_a = _a["branches"]["av"] in ("both", "audio")
                        _ai = grid.anchor_frame_index(_a["at"]["mode"], _a["window"], _kf_fc,
                                                      _a["at"]["frame_idx"])
                        _kf, _why = guides.prepare_anchor(
                            _ai, _kf_fc,
                            video_latent=_av if _want_v else None,
                            audio_latent=_aa if _want_a else None,
                            audio_t=None if (_aa is None or not _want_a) else int(_aa.shape[-1]),
                            label=f"锚点 {_a['id']}")
                        if _kf is None:
                            raise ValueError(f"段{g + 1} {_why}")
                        _kfs.append(_kf)
                        report.append(f"段{g + 1} {_a['id']} 落位 帧{_kf['resolved_frame_index']}"
                                      f"/{_kf_fc}"
                                      + ("" if "latent" not in _kf else "+V")
                                      + ("" if "audio_latent" not in _kf else "+A"))
                    for _kf in _kfs:
                        _ok, _why = guides.validate_anchor(
                            _kf["resolved_frame_index"],
                            guides.latent_frames_of(_kf.get("latent")), _kf_fc)
                        if not _ok:
                            raise ValueError(f"段{g + 1} 锚点越界：{_why}")
                    if _kfs:
                        cond = cls._apply_guide(cond, _kfs, _kf_fc)
                        # 锚定加噪（SkyReels-V2 addnoise_condition 思路）：H3 模型 payload
                        # 原生支持 cond 噪声增强（extra_conds 从 cond dict 任意键取参），
                        # aug=1.0 即不加噪；值越小锚定越「软」，缓解段首刹车/内容重演
                        if aug > 0.0:
                            cond = _apply_anchor_noise(cond, aug)

                # 初始 latent 直用官方空 latent（段首内容由引导桥 cond 承载）。
                _sampling_latent = latent

                # 接缝自动重摇：本段生成后若缝差超阈值，换种子重采本段（上限内），
                # 排除抽卡坏段（同参数下缝差 0.02-0.17 波动大）。cond/latent 与种子
                # 无关只构造一次；回放段不参与。各次尝试取缝差最小的一组（CPU 快照，
                # 不占显存；末次反而更差时回切）。最终种子进 manifest（重放可复现）
                reroll_max = max(0, int(重摇上限)) if (接缝重摇 == "自动" and skip_f) else 0
                attempt = 0
                d_raw = None
                best = None   # (缝差, 结果快照)
                while True:
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
                    def _sample_once():
                        # 初始 latent 是官方空 latent（段首内容由引导桥 cond 承载），
                        # 与旧版逐字节一致
                        return _sigmas_adapter.ksampler_with_sigmas(
                            模型, cur_seed, 步数, CFG,
                            采样器, 调度器, cond, negative, _sampling_latent,
                            sigmas=自定义Sigmas, denoise=1.0)[0]

                    def _sample_rescue():
                        """主干采样 + OOM 自救。返回 (结果, 自救次数)。

                        为什么必须**原参**重试（不改步数/画布/帧数）：自救是要分清
                        「这次 OOM 是碎片/残留导致的一次性偶发」还是「规格真的超了」。
                        降规格重试能救活的那一次会把后者盖住——用户下一段照样炸，
                        而且不知道是自己规格开大了。救不回来时给可行动的中文报错，
                        那才是正解（改画布/改帧数/关 LoRA）。

                        为什么 ComfyUI 自己的 `Got an OOM, unloading all loaded
                        models` 救不回来：它救的是**权重**，而 bypass 型 LoRA 的
                        前向**激活**不在它的账上（日志3 实测：torch 报只分配了
                        13.48GiB，但 CUDA 只剩 23.69MiB——中间 ~10GB 是权重池）。
                        所以这里自己再腾一次。
                        """
                        if not _oom_autoretry:
                            return _sample_once(), 0

                        def _on_retry(i, e):
                            print(f"[H3性能] 段{g + 1} 主干采样显存不足（第 {i} 次）——"
                                  "自动卸载驻留模型 + 回收残留引用 + empty_cache 后原参重试…",
                                  flush=True)

                        try:
                            return perf.oom_retry(_sample_once, cleanup=_vram_oom_cleanup,
                                                  tries=2, on_retry=_on_retry)
                        except Exception as _oe:
                            if not perf.is_oom_error(_oe):
                                raise
                            raise RuntimeError(
                                f"段{g + 1} 主干采样显存不足（已自动卸载驻留模型、回收残留引用并"
                                "empty_cache 后**原参**重试仍失败）：峰值由 画布×帧数 决定"
                                "（步数只影响耗时）——请缩短该段帧数或降低画布，或关闭 LoRA /"
                                "降低放大倍率。本段未产出，已落盘的分段不受影响。") from _oe

                    try:
                        t0 = time.perf_counter()
                        # 首段采样顺带量一次**激活峰值**：LoRA 权重 ComfyUI 自己算得到
                        # （进了 model_size），但 bypass adapter 的前向激活不在任何账上
                        # ——那正是主干 OOM 的真凶（单次 6.13GB）。区间设备级峰值
                        # 减采样前基线 = 采样引入的显存增量（激活 + 回载权重）。
                        if _act_peak_probe and _act_first[0] and not replay:
                            _act_first[0] = False
                            _base_v = _vram_used_gb()
                            with perf.watch_vram_peak() as _w:
                                sampled, _resc = _sample_rescue()
                            _peak_v = _w["peak"]
                            if _peak_v is not None and _base_v is not None:
                                _act = max(0.0, _peak_v - _base_v)
                                try:
                                    _lf = perf.probe_lora_footprint(模型)
                                    print(f"[H3性能] {perf.lora_account_line(_lf['lora_weight_gb'], _lf['patch_count'], _act)}",
                                          flush=True)
                                    perf.emit({"kind": "lora_account", "seg": g + 1,
                                               "lora_weight_gb": _lf["lora_weight_gb"],
                                               "patches": _lf["patch_count"],
                                               "act_peak_gb": round(_act, 2)})
                                except Exception:
                                    pass
                        else:
                            sampled, _resc = _sample_rescue()
                        if _resc:
                            report.append(f"段{g + 1} 主干采样 OOM：已自动腾挪并原参重试成功"
                                          "（参数与产物均无降级）")
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
                    # 重摇退出条件：帧差达标，或已用满重摇上限。
                    if d_raw is None or d_raw <= float(重摇阈值) or attempt >= reroll_max:
                        break
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
                    if not replay:
                        _auto_latent_save(g, i, video_t, audio_t, frames)
            report.extend(gate_lines)
            bridge_scores.append(seg_bridge_score)
            trims.append(seg_lengths[i] - vis_len)

            # 段首响度对齐（与分镜链同款）：增益匹配上段尾 RMS（±6dB 钳制 + 1s 渐出），
            # 增益不沿链累积；归一化已排除锚定区，此处兜住内容本身的响度差。
            # 独立镜头段跳过（独立镜头常配独立声音设计）
            if (item_i > 0 or off) and prev_tail_wav is not None and not seg_unlink[i]:
                # 段首响度对齐：增益去匹配上段尾 RMS（±6dB 钳制 + 1s 渐出），
                # 不沿链累积；归一化已排除锚定区，此处兜住内容本身的响度差。
                # 强度由控件的「响度对齐强度」给（1.0=全量，0=完全不对齐）。
                # 独立镜头/间隔链的段进不来（seg_unlink 已挡）。
                wav, gain_db = qc.loudness_align_head(wav, prev_tail_wav, rate=sample_rate,
                                                      strength=float(响度对齐强度))
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
            # 帧存 uint8（内存 ×¼）：全链帧 tensor 是内存峰值的大头（D3：
            # 8 段 ×107 帧 1312×736 = 9.9GB）。接缝测量用的是同一份 float 帧
            # （prev_tail_* / metrics），不受影响——uint8 只进「等拼成片」的那一份。
            all_frames.append(_frames_to_uint8(_cf) if _frames_uint8 else _cf)
            seg_frames.append(_cf)
            seg_wav = wav.cpu()
            seg_wavs.append({"waveform": seg_wav, "sample_rate": sample_rate})
            all_wav = seg_wav if all_wav is None else torch.cat([all_wav, seg_wav], dim=-1)
            prev_tail_frame = frames[-1].cpu()
            prev_tail_clip = frames[-24:].cpu()
            prev_tail_wav = seg_wav[..., -seam_n:]
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
                    # 与基础链同一取用口径（同一个 `_takes_bridge`）：回放段补渲染时
                    # 也要用本段该取的那一片，而不是"只要没断链就吃 guide"。
                    _up_guide_kf = guide if _takes_bridge(i) else None
                    _up_tail_kf = _seg_end_img or (
                        end_frame_latent
                        if (seg_end_on[i] and end_frame_latent is not None)
                        else _anchor_tail(i))
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

            # 本段 latent 注入开关（旧字段 latent_ref.on）：旧档才带，现由
            # migrate_legacy_seg 迁走、新状态里已无此概念 —— 故取空字典，
            # 等价于原实现「越界/无记录时的 {}」分支（不改写 note）。
            _lr_cur = {}
            # 报告按「本段是否取片」+「那一片里到底有什么」说，不按段属性反推：
            # 以前按 seg_unlink 与 audio_latent 猜，于是断链段上的显式头锚被说成
            # "guide=无"（其实钉了）、「仅音频」锚被说成"上段尾N帧+音频"（其实没钉画面）。
            # 回放段不注入接片，这里仍读交棒位（与旧报告口径一致）。
            if guide is None or not _takes_bridge(i):
                note = ("guide=无（关闭自动引用上段·断链）" if seg_unlink[i]
                        else "guide=无（本段不挂段首桥）")
            elif "latent" not in guide:
                note = f"guide=仅音频{_eff_fr}帧" if full_bridge else "guide=仅音频（单帧桥降级）"
            else:
                note = (f"guide=上段尾{_eff_fr}帧" if full_bridge else "guide=单帧桥(旧协议降级)") \
                    + ("+音频" if "audio_latent" in guide else "")
            if _lr_cur.get("on") is False:   # _lr_cur 见上方「本段 latent 注入开关」
                note = "guide=无（本段关闭 latent 注入）"
            if _redo_mode is not None:
                note = f"重摇（{_redo_mode}）：首锚{'上段尾帧桥' if eff_guide is not None else '无'}" \
                       f" · 尾锚{'接下段' if _tail_kf is not None else '无'} · 其余段保留存档"
            origin = "存档载入" if replay else f"采样{latent_t_to_frames(video_t.shape[2])}帧"
            seed_txt = cur_seed if cur_seed is not None else "—"
            report.append(f"段{g + 1}/{total}：{origin} 裁头{skip_f}帧 留{frames.shape[0]}帧({frames.shape[0] / 24:.1f}s)"
                          f" · 种子 {seed_txt}" + ("" if replay else f" · 采样 {_seg_t['sample']:.0f}s") + f" | {note}"
                          + (" · 关闭自动引用上段（断链）" if seg_unlink[i] else "")
                          + (" · 首帧图头锚" if _head_kf is not None else "")
                          + (" · 尾帧图尾锚" if (seg_end_on[i] and end_frame_latent is not None) else ""))
            report.append(f"⏱ 段{g + 1}：条件 {_seg_t['cond']:.0f}s · 一采 {_seg_t['sample']:.0f}s"
                          f" · 基础解码 {_seg_t['decode']:.0f}s"
                          + (f" · TE缓存 命中{clip.hits}/未中{clip.misses}" if clip.hits or clip.misses else ""))

            # 生成段输出末端 token 边界（重摇 unlink 段上锚现算对齐用）
            prev_end_t = end_t

            # 接片交棒：只问「下一个会上链的段是不是真的要取片」——与取用端同一个
            # `_takes_bridge`，且用 `_next_consumer` 越过禁用段算给真正相邻的那一段。
            # 不再借用 next_wants_bridge（那是门控/裁帧口径，只看视频分支）：那会让
            # 「仅音频」段首锚拿不到新接片、只能读上一轮残留的旧接片。
            # end_tokens=kept 末端：锚定末端与输出末端重合（回退量已含在 vis_len 里）。
            # 分段优先：按下段的 latent_ref.frames/分支取桥。
            _nxt_pi = _next_consumer(item_i)
            if _nxt_pi >= 0 and _takes_bridge(_nxt_pi):
                guide = _inject_guide(video_t, audio_t, _nxt_pi, end_tokens=end_t)

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
                    "params": ckpt_params, "seam_refine": seam_refine,
                    "title": proj_title, "created_at": proj_created,
                    "updated_at": time.time(), "finals": list(proj_finals),
                    "upscale": proj_upscale,
                    "redo_queue": list(redo_queue),
                    "assets": list(_carry.get("assets") or []),
                    "latents": list(auto_latents),
                    "clips": list(_carry.get("clips") or []),
                    "merges": list(_carry.get("merges") or []),
                    "seg_fields": list(_carry.get("seg_fields") or []),
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
                "params": ckpt_params, "seam_refine": seam_refine,
                "title": proj_title, "created_at": proj_created,
                "updated_at": time.time(), "finals": list(proj_finals),
                "upscale": proj_upscale,
                "redo_queue": list(redo_queue),
                "assets": list(_carry.get("assets") or []),
                "latents": list(auto_latents),
                "clips": list(_carry.get("clips") or []),
                "merges": list(_carry.get("merges") or []),
                "seg_fields": list(_carry.get("seg_fields") or []),
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
            images = _build_chain_images(all_frames, height, width, _frames_uint8)
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
                    # 基础分辨率成片优先走**流式拼接分段 mp4**：全程不碰全链内存帧。
                    # 这是 D3 的正解（NLE 从不把整条时间线的 RGB 帧同时放内存里），
                    # 也是二采高清路径早就在用的同一套（upscale.try_final）。
                    final_name, enc_err = None, None
                    if _final_mode in ("auto", "stream"):
                        final_name, enc_err = _stream_final_basic(
                            root, videos, _off_slots, crf=_bcrf, preset=_bpreset,
                            aq_mode=_baq, dither=_bdith)
                        if final_name:
                            report.append("成片：流式拼接分段 mp4（未经全链内存帧，"
                                          "内存峰值不随段数翻倍）")
                        elif _final_mode == "stream":
                            report.append(f"成片：final_mode=stream 但分段 mp4 不齐"
                                          f"（{enc_err}）——回退内存帧编码")
                    if final_name is None:
                        final_name, enc_err = _autosave_final(root, images, all_wav, sample_rate,
                                                       crf=_bcrf, preset=_bpreset, aq_mode=_baq,
                                                       dither=_bdith)
                    if final_name:
                        try:
                            _frel = os.path.relpath(final_name, root).replace("\\", "/")
                        except ValueError:
                            _frel = os.path.basename(final_name)
                        proj_finals.append(_frel)
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
            # P4d：分段列表输出已删除（分段落盘由自动保存完成，去项目文件夹看片）
        )

    @classmethod
    def IS_CHANGED(cls, 审片模式="关闭", 自动存档="关闭", 自动保存="分段", 自动成片="开启", **kwargs):
        if 审片模式 == "逐段确认" or 自动存档 in ("自动存档", "自动续跑") \
                or 自动保存 in ("分段", "分段+成片"):   # 旧三态「分段+成片」也强制重跑（读档兼容）
            return float("nan")   # 存档/审片/自动保存激活时输入不变也强制真正执行（重读最新 manifest）
        return ""

    @classmethod
    def execute(cls, *args, **kwargs):
        """生成互斥外壳：采样期间剪辑/转码/移动路由遇忙回 423，前端置灰。

        内层为 _execute_inner（原 execute 本体，零改动）；异常也必解忙。
        """
        try:
            from . import checkpoint as _ckpt
        except ImportError:
            import checkpoint as _ckpt
        _ckpt.mark_busy()
        try:
            # P4f：转码最小图省略模型/文本编码器（optional），缺省补 None；
            # 转码专跑不碰它们，生成链照常连线不受影响。
            kwargs.setdefault("模型", None)
            kwargs.setdefault("文本编码器", None)
            kwargs.setdefault("二采模型", None)
            return cls._execute_inner(*args, **kwargs)
        finally:
            _ckpt.unmark_busy()

    @classmethod
    def _execute_transcode_only(cls, ds, jobs, 视频VAE, 音频VAE, 存档目录):
        """内联转码专跑：ds.transcode_jobs 非空时只跑转码（有 VAE 上下文），不跑链。

        资产库"转latent"写入 ds 任务，前端提示用户按一次开始生成即执行；
        任务间隙响应队列取消；结果登记进 manifest.latents，前端凭证据自动清理。
        P2：收尾排空后台队列（transcode_submit 进件）同项目任务，直连 job 表。
        """
        try:
            from .latent_tools import run_transcode_job
        except ImportError:
            from latent_tools import run_transcode_job
        try:
            from . import transcode_queue as _tq
        except ImportError:
            try:
                import transcode_queue as _tq
            except ImportError:
                _tq = None
        try:
            import comfy.model_management as _mm
            _interrupted = _mm.processing_interrupted
        except Exception:
            _interrupted = None
        try:
            import comfy.utils as _cu
            _pbar = _cu.ProgressBar(len(jobs))
        except Exception:
            _pbar = None
        proj_default = str(存档目录 or "").strip()
        lines = ["H3 内联转码（只跑转码，不跑链、不耗种子）"]
        done = 0
        _drained = 0
        for k, job in enumerate(jobs):
            if callable(_interrupted):
                try:
                    if _interrupted():
                        lines.append(f"任务 {k + 1} 起中断：剩余 {len(jobs) - k} 个任务保留排队")
                        break
                except Exception:
                    pass
            proj = str(job.get("project") or proj_default).strip()
            if proj_default and proj != proj_default:
                lines.append(f"任务 {k + 1} 跳过：任务项目「{proj}」与当前存档目录「{proj_default}」"
                             "不一致（请切换项目后重排，或清空任务）")
                continue
            if not proj:
                lines.append(f"任务 {k + 1} 跳过：无项目名（请填写存档目录）")
                continue
            try:
                # P1：每段 ds 任务配一个后台 job，前端可轮询进度/取消（失败只记行）
                _tjob = None
                if _tq is not None:
                    try:
                        _tjob = _tq.submit(
                            proj, job.get("src"), job.get("start_s", 0.0),
                            job.get("end_s", 0.0), job.get("branch", "图像+音频"),
                            job.get("split", "否"), job.get("save_name", ""))
                        _tq.begin(_tjob["id"])
                    except Exception:
                        _tjob = None
                _job_stop = (_tq.interrupted_checker(_tjob["id"])
                             if (_tq is not None and _tjob) else None)

                def _job_interrupted(_outer=_interrupted, _chk=_job_stop):
                    if callable(_chk):
                        try:
                            if _chk():
                                return True
                        except Exception:
                            pass
                    if callable(_outer):
                        try:
                            return bool(_outer())
                        except Exception:
                            return False
                    return False

                class _JobPbar:
                    def __init__(self):
                        self.n = 0

                    def update(self, kk=1):
                        try:
                            self.n += int(kk)
                        except (TypeError, ValueError):
                            self.n += 1
                        if _tq is not None and _tjob:
                            _tq.progress(_tjob["id"], min(0.99, self.n / 4.0))

                rep = run_transcode_job(
                    proj, job.get("src"), 视频VAE, 音频VAE,
                    job.get("start_s", 0.0), job.get("end_s", 0.0),
                    job.get("branch", "图像+音频"), job.get("split", "否"),
                    job.get("save_name", ""), _pbar=_JobPbar(),
                    _interrupted=_job_interrupted)
                if _tq is not None and _tjob:
                    _tq.complete(_tjob["id"], rep)
                lines.append(f"任务 {k + 1} 完成：{rep}")
                done += 1
            except Exception as e:
                if type(e).__name__ in ("InterruptProcessingException", "_Interrupted"):
                    raise
                # 堆栈只打控制台（报告里只留一行，前端可读；定位靠这份堆栈）
                try:
                    import traceback as _tb
                    _tb.print_exc()
                except Exception:
                    pass
                if _tq is not None and _tjob:
                    try:
                        _tq.fail(_tjob["id"], f"{type(e).__name__}: {e}")
                    except Exception:
                        pass
                lines.append(f"任务 {k + 1} 失败：{type(e).__name__}: {e}")
            if _pbar is not None:
                try:
                    _pbar.update(1)
                except Exception:
                    pass
        # P2 直连：排空后台队列里同项目的已提交任务（/h3chain/transcode_submit 进件，
        # 无需再写 ds.transcode_jobs）。proj_default 为空时全排空——本次本就是转码专跑。
        if _tq is not None:
            while True:
                try:
                    _claimed = _tq.claim(proj_default or None)
                except Exception:
                    break
                if _claimed is None:
                    break
                _cid = _claimed["id"]
                if _tq.is_cancel_requested(_cid):
                    _tq.finish_cancelled(_cid, "执行前已取消")
                    lines.append(f"队列任务 {_claimed.get('save_name') or _cid} 已取消，未执行")
                    continue
                if callable(_interrupted):
                    try:
                        if _interrupted():
                            lines.append(f"队列任务起中断：剩余队列任务保留，下次转码专跑继续")
                            break
                    except Exception:
                        pass
                _chk_c = _tq.interrupted_checker(_cid)

                def _interrupted_c(_outer=_interrupted, _chk=_chk_c):
                    if callable(_chk):
                        try:
                            if _chk():
                                return True
                        except Exception:
                            pass
                    if callable(_outer):
                        try:
                            return bool(_outer())
                        except Exception:
                            return False
                    return False

                class _ClaimPbar:
                    def __init__(self):
                        self.n = 0

                    def update(self, kk=1):
                        try:
                            self.n += int(kk)
                        except (TypeError, ValueError):
                            self.n += 1
                        try:
                            _tq.progress(_cid, min(0.99, self.n / 4.0))
                        except Exception:
                            pass

                try:
                    _rep_c = run_transcode_job(
                        _claimed["project"], _claimed["src"], 视频VAE, 音频VAE,
                        _claimed.get("start_s", 0.0), _claimed.get("end_s", 0.0),
                        _claimed.get("branch", "图像+音频"), _claimed.get("split", "否"),
                        _claimed.get("save_name", ""), _pbar=_ClaimPbar(),
                        _interrupted=_interrupted_c)
                    _tq.complete(_cid, _rep_c)
                    lines.append(f"队列任务完成：{_rep_c}")
                    _drained += 1
                    done += 1
                except Exception as e:
                    if type(e).__name__ in ("InterruptProcessingException", "_Interrupted"):
                        try:
                            _tq.finish_cancelled(_cid, "队列中断")
                        except Exception:
                            pass
                        raise
                    try:
                        import traceback as _tb
                        _tb.print_exc()
                    except Exception:
                        pass
                    try:
                        _tq.fail(_cid, f"{type(e).__name__}: {e}")
                    except Exception:
                        pass
                    lines.append(f"队列任务失败：{type(e).__name__}: {e}")
            if _drained:
                lines.append(f"后台队列排空：{_drained} 个提交任务已执行")
        lines.append(f"转码结束：共完成 {done} 个（面板 {len(jobs)}＋后台 {_drained}）。"
                     "latent 库已登记，面板自动清理已完成任务。")
        print("[H3内联转码] " + " | ".join(lines), flush=True)
        images = torch.zeros(1, 64, 64, 3)
        silence = {"waveform": torch.zeros(1, 1, 0), "sample_rate": 24000}
        return io.NodeOutput(images, silence, 24, "\n".join(lines))

    @staticmethod
    def _apply_guide(cond, keyframes, sampled_fc):
        """把关键帧列表注入 conditioning（官方 minimax_keyframes 协议）。

        `keyframes` 是**已解析好落点**的 keyframe 字典列表
        （`{resolved_frame_index, latent?, audio_latent?}`），由调用方按
        「自动锚（段首默认桥 / 头锚 / 尾锚）+ 手动锚（seg.anchors）」组装。
        本方法只做两件事：合并进 cond 已有的 keyframes（如 i2v 首帧图的
        first_frame），并写回 minimax_frame_count。

        为什么不再在这里收 guide/head/tail/mid/e1/e2 六个参数：那是七处碎片
        在调用点上的投影。落点解析（含负值自尾部计数）与越界校验都在组装侧
        集中完成，这里做第二遍判断只会造出第二个真相。
        """
        merged = list(cond[0][1].get("minimax_keyframes", [])) + list(keyframes or ())
        if not merged:
            return cond
        return node_helpers.conditioning_set_values(cond, {
            "minimax_keyframes": merged,
            "minimax_frame_count": sampled_fc,
        })
