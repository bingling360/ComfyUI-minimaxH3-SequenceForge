"""官方 MiniMaxH3AddGuide 锚点语义对齐（纯函数，无 torch / ComfyUI 依赖）。

对齐 ComfyUI v0.34.0 新增的官方节点 `MiniMaxH3AddGuide`：

- `frame_idx` 任意帧锚定，负值自尾部计数（负索引 = frame_count + frame_idx）
- 多帧片段按 17k+5 向下对齐；不足 5 帧只取首帧（1 帧锚）
- 越界校验：`resolved_frame_index + guide_frames > frame_count` 即非法
- 音频按剩余时长裁剪：`max_rt = floor(audio_t - FRAME_RESCALE * resolved_idx)`
- 多个 AddGuide 串联 = `minimax_keyframes` 里多条 keyframe（多锚点）

本模块把所有锚点构造收敛到同一套语义与守卫：插件内部无论是段首桥、段尾锚、
记忆锚还是段中锚，都先过一遍校验再写进 conditioning，杜绝越界锚点把采样
带进形状错位（上游 AddGuide 对越界是直接抛 ValueError 的）。
"""
import math

from . import grid

MIN_CLIP_FRAMES = 5


def resolve_frame_index(frame_idx, frame_count):
    """负索引自尾部计数（官方：frame_idx < 0 时取 frame_count + frame_idx）。"""
    i = int(frame_idx)
    return i if i >= 0 else int(frame_count) + i


def clip_guide_frames(n):
    """锚定片段的合法帧数：<5 帧 -> 1 帧（单帧锚）；否则向下对齐 17k+5。"""
    n = int(n)
    if n < MIN_CLIP_FRAMES:
        return 1
    while n % 17 != 5:
        n -= 1
        if n < MIN_CLIP_FRAMES:
            return 1
    return n


def latent_frames_of(video_latent):
    """视频 latent [B,C,T,H,W] -> 承载的像素帧数（用于越界校验）。"""
    if video_latent is None:
        return 0
    return grid.latent_t_to_frames(int(video_latent.shape[2]))


def validate_anchor(frame_idx, guide_frames, frame_count):
    """官方越界校验（不抛异常）：返回 (ok, 原因)。"""
    idx = resolve_frame_index(frame_idx, frame_count)
    gf = clip_guide_frames(guide_frames)
    if idx < 0:
        return False, (f"锚点帧位 {frame_idx} 解析为 {idx}（负索引超出视频 {frame_count} 帧）")
    if idx + gf > int(frame_count):
        return False, (f"{gf} 帧引导片段钉在帧 {idx}，超出视频 {frame_count} 帧")
    return True, ""


def crop_audio_for_anchor(audio_latent, resolved_idx, audio_t, frame_rescale=None):
    """按锚点剩余时长裁剪音频 latent（官方 AddGuide 同式）。

    max_rt = floor(audio_t - FRAME_RESCALE * resolved_idx)
    返回 None = 剩余时长不足，此处不该挂音频锚（调用方丢弃音频分支即可）。
    """
    if audio_latent is None:
        return None
    rescale = grid.FRAME_RESCALE if frame_rescale is None else float(frame_rescale)
    max_rt = math.floor(float(audio_t) - rescale * float(resolved_idx))
    if max_rt < 1:
        return None
    if int(audio_latent.shape[-1]) > max_rt:
        return audio_latent[..., :max_rt].clone()
    return audio_latent


def build_keyframe(resolved_frame_index, video_latent=None, audio_latent=None):
    """按官方 keyframe 字典结构构造：只放非 None 的分支（模型层按键预留行）。"""
    kf = {"resolved_frame_index": int(resolved_frame_index)}
    if video_latent is not None:
        kf["latent"] = video_latent
    if audio_latent is not None:
        kf["audio_latent"] = audio_latent
    return kf


def mid_anchor_index(frame_count, ratio=0.5):
    """段中锚点帧位：按比例定位并夹在 [0, frame_count-1]。

    单帧锚不要求落在 17k+5 网格上（官方 AddGuide 对 1 帧锚只校验不越界）；
    ratio 由实验参数给出，默认段正中。
    """
    fc = int(frame_count)
    if fc <= 1:
        return -1
    return max(0, min(fc - 1, int(round(fc * float(ratio)))))


def audit_keyframes(keyframes, frame_count):
    """只读体检：返回越界锚点的告警列表（不改 keyframes）。

    `PackedLayout` 本身不校验锚点是否落在时间轴内（cond 行是独立段），但官方
    AddGuide 明确判越界。这里**只报不拦**：既给出排查线索，又不改变既有链的
    生成行为——历史上已有锚点若被静默丢弃，旧存档续跑就不再逐帧一致了。
    """
    out = []
    for kf in keyframes or ():
        if not isinstance(kf, dict):
            continue
        ok, why = validate_anchor(kf.get("resolved_frame_index", 0),
                                  latent_frames_of(kf.get("latent")), frame_count)
        if not ok:
            out.append(f"锚点越界告警：{why}（模型仍按 PackedLayout 预留行处理）")
    return out


def prepare_anchor(frame_idx, frame_count, video_latent=None, audio_latent=None,
                   audio_t=None, label=""):
    """一站式锚点构造：解析帧位 → 越界校验 → 音频按剩余时长裁剪 → 组装 keyframe。

    返回 (keyframe|None, 说明)：
    - 越界 / 无素材 => (None, 原因)，调用方把说明写进报告并跳过该锚，
      单个锚点失败不中断整条链（上游 AddGuide 是抛 ValueError，插件选择软降级）。
    - 正常 => (keyframe, "")；音频被裁剪时说明里给出裁剪量，便于报告回看。
    """
    name = f"{label}锚" if label else "锚点"
    # 官方：guide_frames 默认 1（纯音频锚不占视频帧），只有挂了图像/片段才按其实
    # 际帧数参与越界校验。
    gf = latent_frames_of(video_latent) if video_latent is not None else 1
    ok, why = validate_anchor(frame_idx, gf, frame_count)
    if not ok:
        return None, f"{name}跳过：{why}"
    idx = resolve_frame_index(frame_idx, frame_count)
    note = ""
    a_lat = None
    if audio_latent is not None and audio_t is not None:
        a_lat = crop_audio_for_anchor(audio_latent, idx, audio_t)
        if a_lat is None:
            note = f"{name}（帧 {idx}）音频剩余时长不足，已丢弃音频分支"
        elif a_lat is not audio_latent:
            note = (f"{name}（帧 {idx}）音频按剩余时长裁剪 "
                    f"{int(audio_latent.shape[-1])}→{int(a_lat.shape[-1])} token")
    if video_latent is None and a_lat is None:
        return None, f"{name}跳过：无可用素材（帧 {idx}）"
    return build_keyframe(idx, video_latent=video_latent, audio_latent=a_lat), note
