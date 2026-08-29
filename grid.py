"""MiniMax H3 帧网格纯数学，不依赖 ComfyUI 运行环境，可独立单元测试。

token 与像素帧的映射、音频窗换算全部由官方常量推导，不写死比例。
"""

try:
    from comfy.ldm.minimax.model import FRAME_PER_TOKEN, FRAME_RESCALE
except Exception:
    # 无 ComfyUI 环境（单元测试）时使用官方当前值
    FRAME_PER_TOKEN = (1, 4, 4, 4, 4)
    FRAME_RESCALE = 5.0 / 3.0


def align_frame_count(n):
    """像素帧数向上对齐到 H3 的 17k+5 网格（5/22/39/56/73...）。"""
    while n % 17 != 5:
        n += 1
    return n


def align_frame_count_down(n):
    """像素帧数向下对齐到 17k+5 网格；不足 5 帧返回 0（调用方自行报错）。"""
    if n < 5:
        return 0
    while n % 17 != 5:
        n -= 1
    return n


def video_latent_t(frame_count):
    """像素帧数 -> 视频 latent token 数（官方 temporal_shape 同式）。"""
    return 2 if frame_count <= 5 else ((frame_count - 5) // 17) * 5 + 2


def latent_t_to_frames(latent_t):
    """视频 latent token 数 -> 像素帧数（官方 AddGuide 同式）。"""
    return sum(FRAME_PER_TOKEN[k % 5] for k in range(latent_t))


def frames_to_latent_t(frames, up=True):
    """像素帧数 -> token 数：up=True 最小可覆盖（不丢帧），False 最大不超出。

    latent_t_to_frames 严格递增（FRAME_PER_TOKEN 全正），逆映射唯一；
    用于把输出末端对齐到 token 边界——latent 只能整 token 切，
    keyframe 锚定末端与输出末端必须重合，否则续拍点落进从未输出的填充帧。
    """
    if frames <= 0:
        return 0
    t = (frames // sum(FRAME_PER_TOKEN)) * len(FRAME_PER_TOKEN)
    while latent_t_to_frames(t) < frames:
        t += 1
    if not up and latent_t_to_frames(t) > frames:
        t -= 1
    return t


def audio_tokens_for_frames(frames):
    """像素帧数 -> 音频 latent token 数（1 视频帧 = FRAME_RESCALE 个音频 latent 帧）。"""
    return round(frames * FRAME_RESCALE)


def token_start_frames(latent_t):
    """各视频 latent token 的起始像素帧（长度 latent_t）。

    首 token 承载 1 帧、其后每个 4 帧（FRAME_PER_TOKEN 循环），故 token 边界
    不等距——逐 token 掩码必须按真实帧位计算，不能简单按 token 序号线性插值
    （否则前 1 帧会被当成 4 帧，斜坡头部出现突变）。
    """
    out = []
    f = 0
    for k in range(int(latent_t)):
        out.append(f)
        f += FRAME_PER_TOKEN[k % len(FRAME_PER_TOKEN)]
    return out


def token_center_frames(latent_t):
    """各视频 latent token 中心像素帧（斜坡采样点，语义同 token_start_frames）。"""
    return [f + FRAME_PER_TOKEN[k % len(FRAME_PER_TOKEN)] / 2.0
            for k, f in enumerate(token_start_frames(latent_t))]


def snap_frames_to_tokens(frames, up=True):
    """把「钉住帧数」对齐到 token 边界（向下/向上取到真实 token 网格）。

    钉住区是整 token 切的（latent 不能切半个 token），而 token↔帧映射不等距
    （首 token 1 帧、其后 4 帧、每 5 token 循环），可达帧数是
    1/5/9/13/17/18/22/26/30/34/35/39…——不在其上的值按 up/down 就近落位。
    """
    if frames <= 0:
        return 0
    t = frames_to_latent_t(frames, up=up)
    return latent_t_to_frames(t)
