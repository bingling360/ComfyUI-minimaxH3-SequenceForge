"""E4 双向过渡重生成：纯数学/纯逻辑工具（不依赖 torch，可独立单测）。

把缝区划分为 past|transition|future 三窗，构造双锚 keyframe（缝前锚=上段尾末帧、
缝后锚=本段缝后帧），transition 窗用缝区独占噪声低步数重采样——只改 transition
窗，前后帧不动。三窗长度均对齐 17k+5 网格（H3 生成的最小单位）。

设计原则：
- 本模块只做"布局与 keyframe 构造"的纯逻辑；真正的模型采样在 nodes.py 里集成。
- 所有函数无 torch 依赖，可在 stub 环境下单测。
- 互斥优先级（E4 优先于整段重摇）由调用方（nodes.py）保证，本模块不感知。
"""

from . import grid


def transition_windows(total_frames, seam_index, transition_frames,
                       past_frames=None, future_frames=None):
    """把单段视频（total_frames 像素帧）划分为 past|transition|future 三窗。

    参数：
    - total_frames: 本段像素帧总数（_decode_crop 输出的 vis_len）。
    - seam_index: 接缝在本段中的像素帧索引（通常为 0，即段首就是缝）。
    - transition_frames: 用户指定的过渡窗帧数（参数"过渡窗帧数"）。
    - past_frames/future_frames: 双锚各侧参考帧数；None 时默认取 transition_frames 同长。

    返回 (past_start, past_end, trans_start, trans_end, future_start, future_end)，
    均为 [start, end) 半开区间，单位像素帧。

    约束：
    - transition 窗居中在 seam_index，左右对称（不对称时缝侧多取）。
    - 三窗长度分别向下对齐 17k+5 网格（对齐失败时至少留 5 帧最小单元）。
    - 越界时自动收缩，保证不超出 [0, total_frames)。
    - 过渡窗至少 5 帧（最小可生成单元），否则返回 None 表示不可用（调用方跳过 E4）。
    """
    T = int(total_frames)
    si = int(seam_index)
    if T <= 0 or si < 0 or si >= T:
        return None
    tf = max(5, int(transition_frames))
    pf = max(5, int(past_frames)) if past_frames else tf
    ff = max(5, int(future_frames)) if future_frames else tf

    # transition 窗中心在缝：左侧=缝前，右侧=缝后
    half = tf // 2
    ts = max(0, si - half)
    te = min(T, ts + tf)
    ts = max(0, te - tf)   # 右端贴边时左端再缩
    trans_len = te - ts
    if trans_len < 5:
        return None   # 过渡窗过小，不可用

    # 网格对齐：把 transition 长度向下对齐到 17k+5
    trans_len_aligned = grid.align_frame_count_down(trans_len)
    if trans_len_aligned < 5:
        return None
    # 重新居中：以缝为中心，向两侧取对齐后长度
    half2 = trans_len_aligned // 2
    ts = max(0, si - half2)
    te = min(T, ts + trans_len_aligned)
    ts = max(0, te - trans_len_aligned)

    # past 窗：过渡窗左侧，长度对齐网格
    past_len = min(ts, pf)
    past_len_aligned = grid.align_frame_count_down(past_len)
    if past_len_aligned < 5:
        past_len_aligned = 0   # past 可以为空（过渡窗贴着段首）
    ps = ts - past_len_aligned
    pe = ts

    # future 窗：过渡窗右侧，长度对齐网格
    future_len = min(T - te, ff)
    future_len_aligned = grid.align_frame_count_down(future_len)
    if future_len_aligned < 5:
        future_len_aligned = 0   # future 可以为空（过渡窗贴着段尾）
    fs = te
    fe = te + future_len_aligned

    return (ps, pe, ts, te, fs, fe)


def dual_anchor_keyframes(past_latent, future_latent, trans_frame_start, trans_frame_end,
                          latent_t_to_frames, frames_to_latent_t):
    """构造双锚 keyframe 列表：缝前锚 = past 末帧，缝后锚 = future 首帧。

    参数：
    - past_latent: past 窗的视频 latent [B,C,T_past,H,W]（可为 None 表示无 past 锚）。
    - future_latent: future 窗的视频 latent [B,C,T_fut,H,W]（可为 None 表示无 future 锚）。
    - trans_frame_start/end: transition 窗在**本段帧坐标系**中的像素帧范围 [start, end)。
    - latent_t_to_frames / frames_to_latent_t: 帧↔token 映射函数（注入，可单测）。

    返回 keyframe 列表（顺序：past 锚在前，future 锚在后），每个元素形如
    {"resolved_frame_index": int, "latent": latent_slice}。
    至少返回一个锚；两个都 None 时返回空列表（调用方应跳过 E4）。

    锚点位置：
    - past 锚：resolved_frame_index = trans_frame_start（transition 窗左端 = 缝前侧）
    - future 锚：resolved_frame_index = trans_frame_end - 1（transition 窗右端 = 缝后侧）
      注意：H3 keyframe 的 resolved_frame_index 是帧索引（0-based），取过渡窗最后一帧。
    """
    kfs = []
    if past_latent is not None and hasattr(past_latent, "shape") and past_latent.shape[2] > 0:
        # 取 past 最后 1 token 的 latent 作为缝前锚
        past_anchor = past_latent[:, :, -1:, :, :]
        kfs.append({"resolved_frame_index": int(trans_frame_start),
                    "latent": past_anchor})
    if future_latent is not None and hasattr(future_latent, "shape") and future_latent.shape[2] > 0:
        # 取 future 最前 1 token 的 latent 作为缝后锚
        fut_anchor = future_latent[:, :, :1, :, :]
        kfs.append({"resolved_frame_index": max(int(trans_frame_start),
                                                int(trans_frame_end) - 1),
                    "latent": fut_anchor})
    return kfs


def transition_noise_mask(latent_shape, trans_token_start, trans_token_end, device=None):
    """构造 transition 窗的"独占噪声"掩码（纯形状计算，无 torch 时返回 numpy 描述）。

    思路：transition 区用全新随机噪声初始化（与原 latent 完全独立），
    past/future 区保留原 latent 不变——重采样只改变 transition 窗，前后帧不动。

    本函数只做"哪些 token 是 transition 区"的范围计算，真正的随机数生成
    由调用方在 torch 环境完成。返回 (start_token, end_token)。
    """
    T = int(latent_shape[2]) if len(latent_shape) >= 3 else 0
    ts = max(0, min(T, int(trans_token_start)))
    te = max(ts, min(T, int(trans_token_end)))
    return ts, te


# ---- E4 与整段重摇的互斥优先级判定（纯逻辑，可单测） ----

def e4_should_try_first(e4_enabled, seam_bad, reroll_available):
    """E4 与整段重摇的互斥优先级判定。

    返回 (try_e4: bool, fallback_reroll: bool)：
    - try_e4: 是否先走 E4 过渡重生成。
    - fallback_reroll: E4 失败后是否回退整段重摇。

    规则：
    - E4 关 → 不试 E4，直接走重摇（或不摇）。
    - E4 开且缝超阈值 → 先试 E4；E4 后仍不合格再回退整段重摇。
    - 缝没超阈值 → 都不试。
    """
    if not e4_enabled or not seam_bad:
        return False, False
    return True, bool(reroll_available)
