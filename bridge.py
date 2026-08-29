"""桥区软着陆：把段首钉成「上一段的真实尾巴」，而不是「重新生成一遍再裁掉」。

## 现状（钉桥 + 裁头）

每段多采样 `引导帧数`(ctx) 帧，把上段尾 latent 作为 keyframe 钉在条件里，
采样后把这 ctx 帧整个裁掉 —— 它们只是「预热」，成片用的是后面重新生成的画面。
代价：① 白算 ctx 帧（默认 22/146 ≈ 15%）；② 接缝第一帧是模型自由生成的，
与条件里的上段尾只有软关联，缝差全靠重摇兜。

## 软桥（本模块）

ComfyUI v0.34.0 起，H3 的 target 流支持**逐 token 的软噪声掩码**
（`denoise_mask` / `audio_denoise_mask`，掩码值 m 令该行 timestep = 1 - m·sigma，
m=0 的行被钉在 cond timestep 0.999 / 1.0）。配合官方 `latent["noise_mask"]`
入口（`SetLatentNoiseMask` 同款协议），可以：

1. 把上段尾 latent 直接写进本段初始 latent 的头部（`build_initial_latent`）；
2. 用逐 token 掩码把这段标记为「已有内容」（`video_hold_mask` / `audio_hold_mask`）。

于是采样器每步把钉住区拉回上段尾，模型也把它当干净行看待——本段是从上段
真实结尾**接着画**，而不是照着条件重画一遍。

## 为什么默认不是「斜坡」而是整窗钉死

钉住区之外 `latent_image` 是空的，掩码 m<1 会把行拉向全零 latent（黑帧/静音），
所以「钉住区外做斜坡」是错的。钉住区内部若提前释放，模型看到的结尾就不是
上段的真实最后一帧，衔接反而变差。故默认 curve="hold"：整窗 m=0（严格钉住），
钉住区之外一律 m=1（完全生成）。linear/smoothstep 仅供 A/B 对照——它们让
钉住窗尾部提前放开，代价是「即时上文」不再逐帧精确。

## 兼容

`probe_soft_bridge()` 在进程内探测一次，返回等级：
0=不可用（回退现状路径）、1=仅采样器掩码（保留生效）、2=采样器+模型逐行
timestep（质量最佳）。任何一步探测失败都降级，绝不抛异常到主循环。
"""
import inspect

import torch

from . import grid

LEVEL_OFF = 0
LEVEL_SAMPLER = 1
LEVEL_FULL = 2

_LEVEL_TEXT = {
    LEVEL_OFF: "不可用",
    LEVEL_SAMPLER: "仅采样器噪声掩码（保留生效，模型侧无逐行 timestep）",
    LEVEL_FULL: "采样器 + 模型逐行 timestep（完整软桥）",
}

CURVES = ("hold", "linear", "smoothstep", "ease_in")

try:  # ComfyUI 环境才有；单测环境为 None（由 tests 注入替身）
    from comfy.nested_tensor import NestedTensor
except Exception:  # pragma: no cover
    NestedTensor = None

_probe_cache = None


def nested_pair(video, audio):
    """把 (video, audio) 两个张量打包成 H3 用的 NestedTensor。"""
    if NestedTensor is None:
        raise RuntimeError("当前环境没有 comfy.nested_tensor.NestedTensor")
    return NestedTensor((video, audio))


# --------------------------------------------------------------------------
# 能力探测
# --------------------------------------------------------------------------
def _source(obj):
    try:
        return inspect.getsource(obj)
    except Exception:
        return ""


def _probe():
    """返回 (等级, 原因)。只读、不加载权重。"""
    try:
        import comfy.samplers as _sp
        src = _source(_sp.CFGGuider.sample)
        if "denoise_mask.is_nested" not in src:
            return LEVEL_OFF, "CFGGuider.sample 不支持 NestedTensor 掩码（ComfyUI 过旧）"
    except Exception as e:
        return LEVEL_OFF, f"读取 comfy.samplers.CFGGuider.sample 失败：{e}"

    try:
        import nodes as _cn
        src = _source(_cn.common_ksampler)
        if not ('"noise_mask" in latent' in src or "'noise_mask' in latent" in src):
            return LEVEL_OFF, "common_ksampler 不从 latent['noise_mask'] 取掩码"
    except Exception as e:
        return LEVEL_OFF, f"读取 nodes.common_ksampler 失败：{e}"

    try:
        import comfy.sampler_helpers as _sh
        mv = _sh.prepare_mask(torch.zeros(1, 1, 2, 2, 2), (1, 24, 2, 2, 2), "cpu")
        ma = _sh.prepare_mask(torch.zeros(1, 1, 2, 4), (1, 32, 2, 4), "cpu")
        if tuple(mv.shape) != (1, 24, 2, 2, 2) or tuple(ma.shape) != (1, 32, 2, 4):
            return LEVEL_OFF, f"掩码整形结果异常 {tuple(mv.shape)}/{tuple(ma.shape)}"
    except Exception as e:
        return LEVEL_OFF, f"掩码整形探针失败：{e}"

    if NestedTensor is None:
        return LEVEL_OFF, "无法导入 comfy.nested_tensor.NestedTensor"

    # 以下只影响生成质量，不影响「保留」是否成立 —— 失败降级到 LEVEL_SAMPLER
    try:
        from comfy.ldm.minimax.model import MiniMaxH3Model
        params = inspect.signature(MiniMaxH3Model.forward).parameters
        if "denoise_mask" not in params or "audio_denoise_mask" not in params:
            return LEVEL_SAMPLER, "模型 forward 缺少逐 token 掩码参数"
        import comfy.model_base as _mb
        cls = getattr(_mb, "MiniMaxH3", None)
        src = _source(cls.extra_conds) if cls is not None else ""
        if "audio_denoise_mask" in src:
            return LEVEL_FULL, "完整软桥"
        return LEVEL_SAMPLER, "模型有掩码参数但 extra_conds 未透传"
    except Exception as e:
        return LEVEL_SAMPLER, f"保留生效（模型层掩码不可用：{e}）"


def probe_soft_bridge(force=False):
    """进程内缓存的能力探测，返回 (等级, 原因)。永不抛异常。"""
    global _probe_cache
    if _probe_cache is not None and not force:
        return _probe_cache
    try:
        res = _probe()
    except Exception as e:  # 探测本身出错一律按不可用处理
        res = (LEVEL_OFF, f"探测异常：{e}")
    _probe_cache = res
    return res


def level_text(level, reason):
    return f"软桥等级 {level}（{_LEVEL_TEXT.get(level, '未知')}）：{reason}"


# --------------------------------------------------------------------------
# 钉住计划与掩码
# --------------------------------------------------------------------------
def hold_plan(hold_frames):
    """「钉住帧数」→ (对齐后的帧数, 视频 token 数, 音频 token 数)。

    latent 只能整 token 切，而 token↔帧映射不等距（首 token 1 帧、其后 4 帧），
    故用户给的帧数必须先落到真实 token 网格上。
    """
    hf = grid.snap_frames_to_tokens(hold_frames, up=False) if hold_frames > 0 else 0
    if hf <= 0:
        return 0, 0, 0
    return hf, grid.frames_to_latent_t(hf, up=True), grid.audio_tokens_for_frames(hf)


def resolve_crop(soft_hold, ctx, has_source):
    """决定本段的**裁头帧数**与**采样额外帧数**。返回 (skip_f, extra)。

    - `has_source`：本段是否有上一段可接续（事实首段与独立镜头段为 False）。
    - 现状路径（钉桥+裁头）：`skip_f = extra = ctx`；无可接续源时两者皆为 0。
    - 软桥路径：`skip_f = extra = soft_hold`（通常远小于 ctx）。

    **为什么 skip_f 必须在这里一次定死**：回放分支（读存档 latent 直接解码）
    与生成分支（现算）共用同一个 `skip_f`。软桥生成的段存下的是「段长 +
    soft_hold」帧的 latent，若回放时按 ctx 裁，会**多裁 (ctx - soft_hold) 帧**——
    成片每段凭空变短、时间线错乱，且不报任何错。故两条分支一律走本函数。
    """
    if not has_source:
        return 0, 0
    if int(soft_hold) > 0:
        h = int(soft_hold)
        return h, h
    return int(ctx), int(ctx)


def curve_value(u, curve="hold"):
    """归一化位置 u∈[0,1] → 生成权重 m（0=完全钉住，1=完全生成）。"""
    u = min(1.0, max(0.0, float(u)))
    if curve == "linear":
        return u
    if curve == "smoothstep":
        return u * u * (3.0 - 2.0 * u)
    if curve == "ease_in":
        return u * u
    return 0.0                      # hold：整窗钉死


def video_hold_mask(latent_t, lat_h, lat_w, hold_frames, curve="hold", dtype=torch.float32):
    """视频流掩码 [1,1,T,lat_h,lat_w]：钉住窗内按 curve 取 m，窗外一律 1（完全生成）。

    时间维按 token 的**中心帧**归一化（token 承载帧数不等距，按 token 序号线性
    取会让首帧被当成 4 帧，斜坡头部突变）。
    """
    w = torch.ones(int(latent_t), dtype=dtype)
    if hold_frames > 0:
        centers = grid.token_center_frames(int(latent_t))
        for k, c in enumerate(centers):
            u = c / float(hold_frames)
            if u >= 1.0:
                break
            w[k] = curve_value(u, curve)
    return w.view(1, 1, int(latent_t), 1, 1).expand(
        1, 1, int(latent_t), int(lat_h), int(lat_w)).contiguous()


def audio_hold_mask(audio_t, hold_tokens, curve="hold", dtype=torch.float32):
    """音频流掩码 [1,1,2,audio_t]：立体声 channel-major（ch0 的 t0..T-1，再 ch1）。

    两份声道写同一条斜坡，与 `pack_audio` 的行序一致；模型侧 reshape(-1) 后
    正好是长度 audio_t*2 的逐行掩码。
    """
    t = int(audio_t)
    idx = torch.arange(t, dtype=dtype) + 0.5
    if hold_tokens > 0:
        u = (idx / float(hold_tokens)).clamp(0.0, 1.0)
        w = torch.ones(t, dtype=dtype)
        for i in range(t):
            if u[i] < 1.0:
                w[i] = curve_value(float(u[i]), curve)
    else:
        w = torch.ones(t, dtype=dtype)
    return w.view(1, 1, 1, t).expand(1, 1, 2, t).contiguous()


# --------------------------------------------------------------------------
# 初始 latent 装配
# --------------------------------------------------------------------------
def build_initial_latent(video_t, audio_t, bridge_video, bridge_audio):
    """把上段尾 latent 写入本段初始 latent 头部，其余留空（采样器按掩码补噪声）。"""
    v = torch.zeros_like(video_t)
    if bridge_video is not None:
        n = min(int(bridge_video.shape[2]), int(video_t.shape[2]))
        if n > 0:
            v[:, :, :n, :, :] = bridge_video[:, :, :n, :, :].to(
                device=video_t.device, dtype=video_t.dtype)
    a = torch.zeros_like(audio_t)
    if bridge_audio is not None:
        n = min(int(bridge_audio.shape[-1]), int(audio_t.shape[-1]))
        if n > 0:
            a[..., :n] = bridge_audio[..., :n].to(device=audio_t.device, dtype=audio_t.dtype)
    return nested_pair(v, a)


def plan_for_guide(guide, hold_frames):
    """把「钉住帧数」与**可用桥长度**取交集，返回最终 (帧数, 视频 token, 音频 token)。

    必须取交集：上一段很短（或插入段被截断）时，桥 latent 可能不足计划长度。
    若照计划钉满，多出来的行在 `latent_image` 里是空的，掩码 m=0 会把它们钉到
    **全零 latent**——段首出现黑帧 / 静音。宁可钉得短一点也不钉空行。
    """
    hf, ht, ha = hold_plan(int(hold_frames))
    if ht <= 0:
        return 0, 0, 0
    v = guide.get("latent") if isinstance(guide, dict) else None
    a = guide.get("audio_latent") if isinstance(guide, dict) else None
    if v is None:        # 没有视频 latent 就没有可钉的内容
        return 0, 0, 0
    ht = min(ht, int(v.shape[2]))
    hf = grid.latent_t_to_frames(ht)
    ha = grid.audio_tokens_for_frames(hf)
    if a is not None:
        ha = min(ha, int(a.shape[-1]))
    return hf, ht, ha


def hold_slices(guide, hold_tokens, hold_audio_tokens):
    """从段间桥 keyframe 里截取钉住区（桥的尾部 == 上一段的尾部）。"""
    v = guide.get("latent") if isinstance(guide, dict) else None
    a = guide.get("audio_latent") if isinstance(guide, dict) else None
    vv = None
    if v is not None and hold_tokens > 0:
        n = min(int(hold_tokens), int(v.shape[2]))
        vv = v[:, :, int(v.shape[2]) - n:, :, :]
    aa = None
    if a is not None and hold_audio_tokens > 0:
        n = min(int(hold_audio_tokens), int(a.shape[-1]))
        aa = a[..., int(a.shape[-1]) - n:]
    return vv, aa


def apply_soft_bridge(latent, guide, hold_frames, curve="hold"):
    """装配软桥：返回 (新 latent 字典, 掩码 NestedTensor, info)。

    - `latent`：官方节点产出的本段空 latent（{"samples": NestedTensor}），本函数不改动它。
    - `guide`：`_tail_keyframe(...)` 产出的上段尾桥 keyframe（含 latent / audio_latent）。
    - 失败一律抛 RuntimeError，由调用方降级回「钉桥 + 裁头」路径。
    """
    samples = latent["samples"]
    video_t, audio_t = samples.unbind()
    # 与预检同一函数：保证 seg_len 用的帧数与这里落地的帧数完全一致
    hf, ht, ha = plan_for_guide(guide, int(hold_frames))
    # 再按本段长度收窄一层：钉住窗不能超过本段本身，否则整段被钉死 = 整段是上段尾的复制
    if ht > int(video_t.shape[2]):
        ht = int(video_t.shape[2])
        hf = grid.latent_t_to_frames(ht)
        ha = grid.audio_tokens_for_frames(hf)
        ha = min(ha, int(audio_t.shape[-1]))
    if ht <= 0:
        raise RuntimeError(f"钉住帧数 {hold_frames} 不足 1 个视频 token（或上段尾桥为空）")
    bv, ba = hold_slices(guide, ht, ha)
    if bv is None or int(bv.shape[2]) != ht:
        raise RuntimeError("上段尾桥没有可用的视频 latent，无法装配软桥")
    v_mask = video_hold_mask(video_t.shape[2], video_t.shape[3], video_t.shape[4], hf, curve)
    a_mask = audio_hold_mask(audio_t.shape[-1], ha, curve)
    out = dict(latent)
    out["samples"] = build_initial_latent(video_t, audio_t, bv, ba)
    mask = nested_pair(v_mask, a_mask)
    out["noise_mask"] = mask
    return out, mask, {"hold_frames": hf, "hold_tokens": ht, "hold_audio_tokens": ha}
