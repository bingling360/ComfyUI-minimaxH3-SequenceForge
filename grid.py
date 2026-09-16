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


# ---- 手动锚定（Anchor Studio）：落点与窗宽档位 ----
#
# 窗宽只能取 17k+5（5/22/39/56…）。这不是模型层硬约束（模型只认整 token），
# 而是官方 MiniMaxH3AddGuide 对多帧引导片段的裁剪约定，也正是 video_latent_t
# 的整数反函数：video_latent_t(17k+5) = 5k+2 token，而 latent_t_to_frames(5k+2)
# 恰好还原 17k+5。所以「锁档位」不会丢帧——吸附后的宽度就是引导桥实际能切出的
# latent 帧数，不存在「UI 显示 18 帧、实际只钉 5 帧」这类错位。
# 锁档位的意义在于宽度与 token 数一一对应，UI 才能把那段不等距的 token 刻度
# 画出来自解释（见 docs/手动锚定_分段latent参考规范化_实施规划.md §5）。
AT_MODES = ("head", "mid", "tail")
MIN_WINDOW_FRAMES = 5
# 窗宽上限 = 单次 VAE 编码帧数护栏（H3 训练长度约 124–362 帧，整窗单次前向
# 超限会顶爆显存）。唯一定义点：latent_tools.MAX_ENCODE_FRAMES 从这里取。
MAX_WINDOW_FRAMES = 362
SNAP_WINDOWS = tuple(range(MIN_WINDOW_FRAMES, MAX_WINDOW_FRAMES + 1, 17))


def snap_window_down(frames):
    """引导窗宽向下吸附到 17k+5 档位；不足 5 帧退化为 1（单帧锚，官方同款）。

    与 guides.clip_guide_frames 同结果，但由 video_latent_t / latent_t_to_frames
    推导而非另写一遍取模——两个函数共用同一套常量，不会各自漂移。
    """
    n = int(frames)
    if n < MIN_WINDOW_FRAMES:
        return 1
    return latent_t_to_frames(video_latent_t(n))


def anchor_frame_index(mode, window, frame_count, frame_idx=0):
    """anchor 落点 -> 目标段内帧位（尚未解析负值，交 guides.resolve_frame_index）。

    head -> 段首 0
    tail -> frame_count - window（与向下对齐后的终端重合，不越界）
    mid  -> 用户指定 frame_idx（任意整数，负值自尾部计数）

    返回原始帧位而非「已解析」值：负值语义只在 guides 里定义一处，
    这里重复实现会造出第二个真相。
    """
    if mode == "head":
        return 0
    if mode == "tail":
        return int(frame_count) - int(window)
    if mode == "mid":
        return int(frame_idx)
    raise ValueError(f"未知锚点落点 {mode!r}（合法值：{'/'.join(AT_MODES)}）")
