"""潜空间放大二次采样（二采）——主循环内渲染通道。

上游两个仓库的整合：
- 放大网络：LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler——H3 24 通道 latent
  神经放大（2D 残差骨干 / 纯 3D 卷积，网络与权重加载见 upscale_net.py）。
  时间维 T 绝对不变（17k+5 token 网格硬约束），只放大 H×W，偶数对齐
  （latent 偶数 = 像素 32 倍数，官方画布口径）。
- 二采范式：wjluoxiao/ComfyUI-JZL-MiniMax-H3 的 CondSync 思路——latent 放大后
  用官方 common_ksampler 以 denoise<1 低强度重去噪补回高频细节；条件里的
  keyframe（生成期桥锚）同步放大到目标尺寸，避免按原尺寸重编码。

与主链的关系：每段采样完成后（重摇定稿、latent 存档之后、分段 mp4 落盘之前）
立即「神经放大 → 低强度重采样 → 解码」——分段视频 seg_NNN.mp4 与成片直接
保存二采后的高清结果，不再产出 upseg_* 双份副本（旧版独立后处理通道已废弃）。
段内机制（重摇/门控/智能切镜/接缝精修/指标）仍跑在基础分辨率帧上，语义零漂移；
二采只接管「落盘的帧」。音频不重采样：分段与成片音轨=原轨（零音频回归）。

两种触发模式（导演台面板）：
- 跟随生成：每次运行对待做段（新增/失效）二采，逐段审片时即"生成一段二采一段"
- 手动选择：勾选任意段（含插入视频段/序章）随下次运行二采

重做规则（manifest.upscale 记录，二采参数不进 ckpt_params 指纹）：
- 二采参数变（hash 变）→ include 范围内段全部重渲染高清，基础 latent 不动
- 基础链从段 k 重做（truncate）→ ≥ k 的记录与锚文件自动清除，重跑时补渲染
- 某段基础 prompt/seed 变（base_hash 变）→ 仅该段高清记录失效
- 全链记录齐且尺寸一致 → 成片改为流式拼接高清分段（单份产物）
"""

import gc
import hashlib
import os
import time

import torch

from . import checkpoint
from . import grid
from . import perf

MODES = ("跟随生成", "手动选择")
PRECISIONS = ("fp32", "fp16", "bf16")
# 编码画质开关（原「标准 / 高清 / 极致」三档，2026-09-23 本轮压成**一个开关**，
# 用户拍板）：
#   关（默认）= 现状口径：veryfast · 无 aq · 无抖动
#   开         = medium preset（率失真比 veryfast 好约 5–15%，编码层的二次模糊更少）
#                + aq-mode 3（暗部自适应量化，保暗场细节）
#                + Bayer 有序抖动（打散 8bit 量化台阶 → 平滑渐变区的横向色带）
#
# 为什么砍掉「极致」档：crf 由 `perf.x264_crf` 独立接管之后，「极致」与「高清」
# **只差 preset 速度档**（slow vs medium），不值得单列一档。crf 与 preset / 抖动
# 是**两个正交旋钮** —— 想要极致档的画质，把 crf 往下压就是了。
_ENCODE_SETTINGS = {
    False: ("veryfast", None, False),
    True: ("medium", 3, True),
}


def resolve_encode_quad(hq=False, x264_crf=None):
    """(画质开关, crf) -> (crf, preset, aq_mode, dither)。

    `hq` 只管 preset + aq + 抖动；crf **只**由 `x264_crf` 给（缺省 20）。
    两者正交：「切档位」不会再悄悄改 crf，改 crf 也不会动档位。
    """
    preset, aq, dither = _ENCODE_SETTINGS[bool(hq)]
    crf = 20
    try:
        if x264_crf is not None:
            crf = int(x264_crf)
    except (TypeError, ValueError):
        pass
    return crf, preset, aq, dither
_PARAM_KEYS = ("model", "arch", "scale", "denoise", "steps", "cfg", "precision")
# 放大目标尺寸模式：倍率（现状）/ 目标尺寸（像素）/ 百万像素
SIZE_MODES = ("倍率", "目标尺寸", "百万像素")


def _norm_arch(v):
    """面板架构值 -> "2D" / "3D" / "auto"（空或无法识别 = auto，由权重判定）。"""
    s = str(v or "").strip().upper()
    return s if s in ("2D", "3D") else "auto"


# ---- 状态解析与重做判定（纯逻辑，无 ComfyUI 依赖，可单测） ----

def parse_state(ds):
    """导演台状态 ds -> 归一化二采配置；关闭/缺失返回 None。

    mode: 关闭（None）/ 跟随生成 / 手动选择；include 仅手动模式有值
    （全局槽位 0-based 列表，空列表=本次无可做段）；跟随生成 include=None=全部段。
    """
    if not isinstance(ds, dict):
        return None
    up = ds.get("upscale")
    if not isinstance(up, dict) or up.get("on") is False:
        return None
    mode = str(up.get("mode") or "").strip()
    if mode in ("", "关闭", "off"):
        return None
    if mode not in MODES:
        mode = "跟随生成"

    def _num(key, default, lo, hi):
        try:
            v = float(up.get(key))
        except (TypeError, ValueError):
            v = default
        if v != v:            # NaN 兜底
            v = default
        return min(max(v, lo), hi)

    # 参数 schema v2：steps 语义从「调度总步数 N」改为「尾段精化步数 n」（denoise 语义
    # 同步明确为尾段起始 σ）。v1 旧 JSON 字段级迁移：显式存过的 steps 按旧口径
    # int(N×强度) 折算保行为（旧默认 15×0.45=6 恰为新默认）；没存过的字段直接
    # 落 v2 新默认（从未配置=用最新推荐值，而不是旧默认）。
    try:
        schema = int(up.get("schema"))
    except (TypeError, ValueError):
        schema = 1

    precision = str(up.get("precision") or "fp16")
    if precision not in PRECISIONS:
        precision = "fp16"
    include = None
    if mode == "手动选择":
        vals = set()
        if isinstance(up.get("include"), list):
            for x in up["include"]:
                try:
                    vals.add(int(x))
                except (TypeError, ValueError):
                    continue
        include = sorted(vals)
    denoise = _num("denoise", 0.35, 0.05, 1.0)
    if schema < 2:
        has_steps = up.get("steps") not in (None, "")
        steps = (min(max(int(_num("steps", 15, 1, 100) * denoise), 1), 100)
                 if has_steps else 6)
    else:
        steps = int(_num("steps", 6, 1, 100))
    # 抗糊武器库（全部默认关：STG=0 / passes=1 / 锐化=0 / 采样器沿用
    # 主链 / retry 关——启用任一项才进指纹，既有二采记录不失效）
    stg_block = int(_num("stg_block", 25, 0, 49))
    # 「神经放大」开关：关闭 = 只做低强度重采样精化，完全不加载放大网络（产物仍是
    # 原分辨率）。旧 JSON 缺键默认开，行为不变。
    # 关闭时强制 model="" / scale=1.0：这两项本就在 _PARAM_KEYS 里进指纹，切换开关
    # 自然会触发该段重做——故 enlarge 本身不进指纹（否则既有二采记录会全量失效）。
    enlarge = up.get("enlarge") is not False
    device = str(up.get("device") or "").strip().lower()
    if device not in ("", "auto", "cuda", "rocm", "cpu"):
        device = "auto"
    size_mode = str(up.get("size_mode") or "").strip()
    if size_mode not in SIZE_MODES:
        size_mode = "倍率"
    return {
        "mode": mode,
        "enlarge": enlarge,
        "device": device,
        "model": (str(up.get("model") or "").strip() if enlarge else ""),
        "arch": _norm_arch(up.get("arch")),
        "scale": (_num("scale", 2.0, 1.0, 4.0) if enlarge else 1.0),
        "size_mode": size_mode,
        "target_w": int(_num("target_w", 1280, 64, 8192)),
        "target_h": int(_num("target_h", 704, 64, 8192)),
        "megapixels": _num("megapixels", 1.0, 0.1, 16.0),
        "denoise": denoise,
        "steps": steps,
        "cfg": _num("cfg", 1.0, 0.0, 100.0),
        "precision": precision,
        "time_bias": _num("time_bias", 0.0, 0.0, 0.2),
        "mix": _num("mix", 0.0, 0.0, 1.0),
        "adaptive": up.get("adaptive") is True,
        "shift": _num("shift", 0.0, 0.0, 100.0),
        "stg": _num("stg", 0.0, 0.0, 2.0),
        "stg_block": stg_block,
        "passes": int(_num("passes", 1, 1, 3)),
        "decay": _num("decay", 0.5, 0.2, 0.8),
        "sharpen": _num("sharpen", 0.0, 0.0, 1.0),
        "pixel_sharpen": _num("pixel_sharpen", 0.0, 0.0, 1.0),
        "sampler": str(up.get("sampler") or "").strip(),
        "scheduler": str(up.get("scheduler") or "").strip(),
        "retry": up.get("retry") is True,
        "retry_target": _num("retry_target", 0.15, 0.05, 1.0),
        "include": include,
        # ⛔ `chunk` / `force_unload` / `encode` **不在这里解析**（2026-09-23 迁走）。
        #
        # 这三项是**机器级**设置，与作品无关：
        #   · 时序分块 `chunk`        -> perf.upscale_temporal_chunk（+ 帧数 / overlap）
        #   · 强制卸载 `force_unload` -> perf.keep_upscaler_resident（取反面；另有余量档）
        #   · 编码画质开关 `encode_hq` -> perf.encode_hq（与 encoder / x264_crf 并排）
        # 它们以前随项目存档走，导致「同一台卡换个项目就得重设」，且关掉分块会进
        # 二采指纹、把既有高清段判失效重做 —— 那是拿显存手段当画质手段。
        # 运行时由 nodes.py 把 perf 的值合并进 up_cfg（见该处注释）。
        # 别再往这个 dict 里加回来。
    }


def tail_refine_args(sigma_start, tail_steps):
    """(尾段起始σ, 精化步数 n) -> common_ksampler 的 (steps=n, denoise=σ)。

    ComfyUI ≥0.3x 的 KSampler.set_steps 对 denoise<1 的内部行为 = 先生成
    int(steps/denoise) 步完整调度、再取尾 steps 步——steps 恒为实际执行步数，
    denoise 只定 σ 起点（simple 线性调度下 σ₀≈denoise；非 simple/带 shift 调度
    为网格近似，报告行打印实际生效值兜底）。两个直接语义一一映射即可：
    - flow-matching 下初始加噪 (1-σ₀)x0+σ₀ε 落在截断后的 σ₀——本就是
      sigma 尾段精化；
    - σ 钳到 n/(n+1)：σ₀ 贴近 1 时 int(n/σ)≤n 会取满整个调度（σ 起点=σ_max
      =意外全量重采，denoise=1 会完全丢弃放大后的 latent），钳后调度至少截
      一刀、部分精化档永不撞全量；σ≥0.95 视为明确请求全量重采才返回 1.0。
    """
    n = min(max(int(tail_steps), 1), 100)
    s = min(max(float(sigma_start), 0.05), 1.0)
    if s >= 0.95:
        return n, 1.0
    return n, min(s, n / (n + 1))


def latent_hf_energy(video_t, max_frames=12):
    """latent 高频能量（细节代理指标，纯 torch 可单测）。

    逐帧空间 3×3 均值池化残差的 RMS（≈空域拉普拉斯能量），均匀抽样 ≤max_frames
    帧、CPU 计算（避开二采峰值期的显存）。用于「细节增益」对比：放大 latent
    （纯神经放大、无精化）vs 精化后 latent——增益为正说明尾段精化补回了高频。
    输入 [B,C,T,H,W]；非 5D 或空间 <3 返回 0.0。
    """
    if video_t is None or video_t.dim() != 5 \
            or video_t.shape[-2] < 3 or video_t.shape[-1] < 3:
        return 0.0
    t = int(video_t.shape[2])
    step = max(1, t // max_frames)
    idx = torch.arange(0, t, step)[:max_frames]
    x = video_t.detach().to("cpu", torch.float32)[0, :, idx]     # [C,k,H,W]
    smooth = torch.nn.functional.avg_pool2d(x, 3, stride=1, padding=1)
    return float((x - smooth).pow(2).mean().sqrt())


def hf_gain_ratio(hf_before, hf_after):
    """细节增益百分比（小数）：after 相对 before 的高频能量增幅；before≈0 视为 0。"""
    if hf_before < 1e-8:
        return 0.0
    return (hf_after - hf_before) / hf_before


def freq_mix_latents(base_v, refined_v, ratio, chunk=8):
    """频域细节混合（拉普拉斯分层，纯 torch、CPU 分帧块可单测）。

    out = 精化输出 + ratio·(低频(放大 latent) − 低频(精化输出))，低频 = 逐帧
    3×3 均值池化、replicate 边界填充（与细节增益度量同核；填充不用零填充
    ——零填充会把边界低频拉向 0，r=1 时角点偏差大、画面边缘出晕影，
    replicate 下常量场严格守恒）：
    - ratio=0 原样返回精化输出（关——现行为不变，且不克隆零开销）；
    - ratio=1 = 低频全走放大 latent + 高频全走精化输出：结构/段间接缝锚在
      与基础段同构的放大 latent 上（零漂移），只保留精化补回的细节增益——
      天然对冲「精化带花/内容漂移」，多段链「段间一致性优先」的取向。
    ratio 即「把低频换回放大 latent 的程度」，中间值线性过渡。
    输入 [B,C,T,H,W]（池化逐帧独立，按 T 分块无边界效应）；CPU float32
    计算（避开二采峰值期显存，同 latent_hf_energy 口径）。退化输入
    （None / 形状不一致 / 空间 <3）原样返回 refined_v——混合不可定义时
    保交付产物（精化输出），不报错阻断。
    """
    if base_v is None or refined_v is None or ratio <= 0.0:
        return refined_v
    if base_v.dim() != 5 or base_v.shape != refined_v.shape \
            or base_v.shape[-2] < 3 or base_v.shape[-1] < 3:
        return refined_v
    r = min(max(float(ratio), 0.0), 1.0)
    b = base_v.detach().to("cpu", torch.float32)
    out = refined_v.detach().to("cpu", torch.float32)
    if out.data_ptr() == refined_v.data_ptr():   # 无拷贝路径（detach/同设备 to 共享存储）→ 克隆防改入参
        out = out.clone()

    def _lp(x):
        xp = torch.nn.functional.pad(x, (1, 1, 1, 1), mode="replicate")
        return torch.nn.functional.avg_pool2d(xp, 3, stride=1)

    for t0 in range(0, out.shape[2], chunk):
        sl = slice(t0, t0 + chunk)
        out[0, :, sl] += r * (_lp(b[0, :, sl]) - _lp(out[0, :, sl]))
    return out


# ---- 抗糊武器库：多轮递降精化 / 锐化 / STG / 重试（纯函数，可单测） ----

def cascade_sigmas(sigma_start, passes, decay):
    """多轮递降精化的 σ 起点序列（路线⑥ cascade 化）：[σ₀, σ₀·decay, σ₀·decay², …]。

    passes=1 → [σ₀]（关，现行为）；每多一轮以更小 σ 在上一轮输出上再精化——
    「先修结构、再抠细节」的递进口径（T8 尾段细分的通用化）。decay 钳
    [0.2, 0.8] 保证域内严格递减；防御：若衰减后不降（decay 异常）强制减半；
    每轮钳 [0.05, 0.95]（与 tail_refine_args 同界）——衰减到 0.05 下限后
    序列钳住在下限重复（重复档=换种子的低噪声再抠一遍，仍有细节意义）。
    """
    n = min(max(int(passes), 1), 3)
    d = min(max(float(decay), 0.2), 0.8)
    out = []
    s = min(max(float(sigma_start), 0.05), 0.95)
    for _ in range(n):
        if out:
            s2 = s * d
            if s2 >= out[-1]:               # 衰减后仍不降（decay 异常域）→ 强制减半
                s2 = out[-1] / 2.0
            s = s2
        s = min(max(s, 0.05), 0.95)
        out.append(round(s, 4))
    return out


def sharpen_latents(video_t, amount, chunk=8):
    """latent 域 unsharp 锐化（抗糊 N3，纯 torch、CPU 分帧块可单测）。

    out = x + amount·(x − 低频(x))，低频 = 逐帧 3×3 均值池化 + replicate
    边界（与细节增益度量/频域混合同核同口径）。精化补回的高频再乘 (1+amount)
    放大一档——零模型前向、零显存（CPU 分块，同 freq_mix_latents 模式）。
    amount=0 原样返回（关，不克隆零开销）；退化输入（None/非 5D/空间<3）
    原样返回。钳制输出与输入同界 ±（VAE 解码前不再二次钳制——latent 无界，
    过量锐化由 UI 幅度参数自担）。
    """
    if video_t is None or amount is None or float(amount) <= 0.0:
        return video_t
    if video_t.dim() != 5 or video_t.shape[-2] < 3 or video_t.shape[-1] < 3:
        return video_t
    a = float(amount)
    out = video_t.detach().to("cpu", torch.float32)
    if out.data_ptr() == video_t.data_ptr():
        out = out.clone()

    def _lp(x):
        xp = torch.nn.functional.pad(x, (1, 1, 1, 1), mode="replicate")
        return torch.nn.functional.avg_pool2d(xp, 3, stride=1)

    for t0 in range(0, out.shape[2], chunk):
        sl = slice(t0, t0 + chunk)
        x = out[0, :, sl]
        out[0, :, sl] = x + a * (x - _lp(x))
    return out


def pixel_sharpen_frames(frames, amount, chunk=16):
    """像素域逐帧 unsharp 锐化（抗糊 N4，解码后、编码前）。

    frames [N,H,W,3] float 0-1（GPU/CPU 均可）→ CPU float32 分块逐帧
    out = img + amount·(img − blur₃(img))，blur = 3×3 均值（replicate 边界）；
    钳 [0,1]。amount=0 或退化输入（None/ndim≠4/H|W<3）原样返回原对象。
    与 latent 锐化正交：一个作用于精化输出（生成侧），一个作用于解码帧
    （交付侧，连 VAE 解码的软化也一起补偿）。
    """
    if frames is None or amount is None or float(amount) <= 0.0:
        return frames
    if frames.dim() != 4 or frames.shape[1] < 3 or frames.shape[2] < 3:
        return frames
    a = float(amount)
    n = int(frames.shape[0])
    out = torch.empty((n, frames.shape[1], frames.shape[2], frames.shape[3]),
                      dtype=torch.float32)
    for s in range(0, n, chunk):
        blk = frames[s:s + chunk].detach().to("cpu", torch.float32)   # [k,H,W,3]
        k = blk.shape[0]
        x = blk.permute(0, 3, 1, 2)                                   # [k,3,H,W]
        xp = torch.nn.functional.pad(x, (1, 1, 1, 1), mode="replicate")
        blur = torch.nn.functional.avg_pool2d(xp, 3, stride=1)
        y = (x + a * (x - blur)).clamp(0.0, 1.0).permute(0, 2, 3, 1)
        out[s:s + k] = y
    return out


def pixel_sharpness(frames, max_frames=8):
    """像素域清晰度度量（抗糊 N6）：抽样帧灰度 Laplacian 方差均值（0-255 域）。

    与 latent 域 HF 增益互补——HF 只反映精化前后相对变化，本值给人眼口径的
    绝对清晰度锚点（跨参数/跨项目可横向比较；糊帧 <30、清晰帧 90-130 量级，
    同 qc.frame_scores 口径）。纯 CPU；退化输入返回 0.0。
    """
    if frames is None or frames.dim() != 4 or frames.shape[0] < 1 \
            or frames.shape[1] < 3 or frames.shape[2] < 3:
        return 0.0
    n = int(frames.shape[0])
    step = max(1, n // max_frames)
    vals = []
    for i in range(0, n, step)[:max_frames]:
        img = frames[i].detach().to("cpu", torch.float32)
        gray = (img[..., 0] * 0.299 + img[..., 1] * 0.587
                + img[..., 2] * 0.114) * 255.0                        # [H,W]
        x = gray[None, None]                                          # [1,1,H,W]
        xp = torch.nn.functional.pad(x, (1, 1, 1, 1), mode="replicate")
        blur = torch.nn.functional.avg_pool2d(xp, 3, stride=1)
        lap = (x - blur).pow(2)
        vals.append(float(lap.mean()))
    return sum(vals) / len(vals) if vals else 0.0


def refine_progress(sigma_v, sigma_start):
    """精化局部进度 p = 1 − σ/σ₀（钳 [0,1]）：本段精化进行到哪了。

    与 time_bias_sigma 同口径（不做 shift 逆变换——σ₀ 与喂给模型的 timestep
    同在 model_sampling 域，比值即进度，自洽且与 shift 取值无关）。
    STG 激活窗与重试判定共用。
    """
    s0 = float(sigma_start)
    if s0 <= 0.0:
        return 1.0
    p = 1.0 - float(sigma_v) / s0
    return min(max(p, 0.0), 1.0)


def should_retry(hf_gain, target, sigma_start, max_sigma=0.90):
    """增益自适应重试判定（抗糊 N7）：增益不达标且 σ 还有上调空间 → 重试。

    重试 = 以 σ₀+0.1 重跑整条精化链一次、按细节增益取优（成本一整轮精化，
    只在明确开且确实不达标时发生）。max_sigma 与 adaptive_sigma 上界一致
    （0.95 再留 0.05 头寸防撞全量重采）。
    """
    return float(hf_gain) < float(target) and float(sigma_start) < float(max_sigma)


# 段自适应 σ：latent 域相对运动量 -> 档位 -> σ 起点偏移（路线④）
# 阈值口径 = 原始逐帧意图 × 17/5：latent 相邻 token 隔约 3.4 个像素帧（H3 的
# 17k+5 帧 ↔ 5k+2 token 压缩），合成张量标的 0.10/0.22 在真实 latent 上全饱和
# ——首个真实项目 8 段全部 0.47–0.60 被误判「高运动」统一压 σ。×3.4 重标定后
# 该样本全落中档（σ 不动），真静态/真高运动仍可分；报告行照打「运动量→档位」，
# 后续真实项目数据可继续校准这两个常数。
_ADAPTIVE_VERSION = "v2"     # 阈值口径版本：进指纹，重标定后 adaptive 旧记录按新档重做
_MOTION_STATIC_MAX = 0.34   # < 此值=静态（对话/特写）→ 升 σ 抠脸
_MOTION_ACTION_MIN = 0.75   # > 此值=高运动（打斗/运镜）→ 降 σ 防拖影鬼影
_SIGMA_OFFSETS = {"静态": 0.05, "中": 0.0, "高运动": -0.075}


def latent_motion(video_t, max_pairs=12):
    """latent 域相对运动量（尺度不变，纯 torch 可单测）。

    均匀抽样 ≤max_pairs 个相邻帧对，motion = mean( ‖x[t+1]−x[t]‖_rms /
    (‖x[t]‖_rms+ε) )——逐帧相对变化率，与 latent 幅值无关。比 metrics.py
    的像素域 Farneback 光流（需解码帧）零成本：直接在【基础段 latent】上
    CPU 计算，对 prompt/insert/prologue 段全适用；同 latent 同值（决定论，
    重放不串档）。退化输入（None/非 5D/T<2）返回 0.0（=静态档，安全侧）。
    """
    if video_t is None or video_t.dim() != 5 or video_t.shape[2] < 2:
        return 0.0
    t = int(video_t.shape[2])
    step = max(1, (t - 1) // max_pairs)
    idx = torch.arange(0, t - 1, step)[:max_pairs]
    x = video_t.detach().to("cpu", torch.float32)[0]         # [C,T,H,W]
    diffs, bases = [], []
    for i in idx.tolist():
        diffs.append(x[:, i + 1] - x[:, i])
        bases.append(x[:, i])
    d = torch.stack(diffs).pow(2).mean().sqrt()
    b = torch.stack(bases).pow(2).mean().sqrt()
    if float(b) < 1e-8:
        return 0.0                                            # 全零/常量段=无运动
    return float(d / b)


def motion_tier(motion):
    """运动量 -> 档位标签（阈值常量见上，档位进报告行/记录供复盘校准）。"""
    if motion < _MOTION_STATIC_MAX:
        return "静态"
    if motion > _MOTION_ACTION_MIN:
        return "高运动"
    return "中"


def adaptive_sigma(sigma_start, motion):
    """(σ起点, 运动量) -> (档内生效σ, 档位)：静态 +0.05 抠脸 / 高运动 −0.075
    防拖影鬼影 / 中档不动；钳 [0.05, 0.95]（与 tail_refine_args 同界）。
    默认 σ=0.35 时三档 ≈ 0.40 / 0.35 / 0.275（对齐路线：0.4 / 0.25-0.3）。"""
    tier = motion_tier(motion)
    s = min(max(float(sigma_start) + _SIGMA_OFFSETS[tier], 0.05), 0.95)
    return s, tier


def resolve_refine_sigma(cfg, video_t):
    """cfg + 基础段 latent -> (生效σ起点, 档位|None, 运动量|None)：
    adaptive 关=原样 σ 起点（不算运动量）；开=按段运动档位偏移。
    render_latent（实际采样）/ render_segment（报告行）/ write_record
    （manifest 复盘）同用本函数，三处永远同口径。"""
    if cfg.get("adaptive") is not True:
        return float(cfg["denoise"]), None, None
    motion = latent_motion(video_t)
    sigma, tier = adaptive_sigma(cfg["denoise"], motion)
    return sigma, tier, motion


def time_bias_sigma(sigma_v, sigma_start, bias, start_progress=0.70,
                    end_progress=1.0):
    """尾段时间偏置核心数学（Detail-Daemon 类，T8mars 机制移植，纯函数可单测）。

    精化进度 p = 1 − σ/σ₀（0=精化开始，1=收尾）；在 [start_progress,
    end_progress] 尾窗内以 smoothstep 渐入，把模型「看到的 σ」向更干净方向
    （更小）偏置 bias。不加噪声、不改积分 σ 与前向次数——只是模型时间观感。
    bias≤0 或 σ₀≤0 时原样返回（关）。σ clamp ≥0（精化网格最小评估 σ 远大于
    bias，钳 0 仅兜底）。
    """
    if bias <= 0.0 or sigma_start <= 0.0:
        return sigma_v
    p = 1.0 - min(sigma_v / sigma_start, 1.0)
    span = max(end_progress - start_progress, 1e-6)
    w = min(max((p - start_progress) / span, 0.0), 1.0)
    w = w * w * (3.0 - 2.0 * w)             # smoothstep 渐入
    return max(sigma_v - w * bias, 0.0)


def _hash_params(cfg):
    """进指纹/记录的参数字典（params_hash 与 write_record.params 同口径）。

    time_bias / mix / shift 仅在 >0、adaptive 仅在开启时进指纹——默认全关
    不改变哈希，既有二采记录不因新增参数而失效重做。adaptive 开启后每段
    生效 σ 由该段基础 latent（经 base_hash 指纹）决定论派生，无需逐段进
    指纹也不会串档。size_mode 始终进指纹（切换模式改变目标尺寸）；目标
    尺寸/百万像素模式不进 scale（该值此时无效，避免无谓重做）。
    """
    keys = {k: cfg[k] for k in _PARAM_KEYS}
    # 目标尺寸模式：仅非倍率模式进指纹（target_w/h、megapixels 两组键与
    # scale 键天然互斥，模式切换必然变 hash）；倍率模式指纹与旧版逐位一致
    # ——既有二采记录不因新增 size_mode 字段而失效重做（项目铁律）。
    sm = str(cfg.get("size_mode") or "倍率")
    if cfg.get("enlarge", True) and sm != "倍率":
        if sm == "目标尺寸":
            keys["target_w"] = cfg["target_w"]
            keys["target_h"] = cfg["target_h"]
        else:                                   # 百万像素
            keys["megapixels"] = cfg["megapixels"]
        keys.pop("scale", None)                 # 该模式下 scale 无效，不进指纹
    if cfg.get("time_bias"):
        keys["time_bias"] = cfg["time_bias"]
    if cfg.get("mix"):
        keys["mix"] = cfg["mix"]
    if cfg.get("adaptive"):
        keys["adaptive"] = _ADAPTIVE_VERSION   # 策略位含阈值口径版本；重标定即升版重做
    if cfg.get("shift"):
        keys["shift"] = cfg["shift"]
    # 抗糊武器库（全部条件化：默认关不进指纹，既有二采记录不失效）
    if cfg.get("stg"):
        keys["stg"] = cfg["stg"]
        keys["stg_block"] = cfg["stg_block"]
    if int(cfg.get("passes") or 1) > 1:
        keys["passes"] = cfg["passes"]
        keys["decay"] = cfg["decay"]
    if cfg.get("sharpen"):
        keys["sharpen"] = cfg["sharpen"]
    if cfg.get("pixel_sharpen"):
        keys["pixel_sharpen"] = cfg["pixel_sharpen"]
    # 画质开关「开了才进指纹」—— 它改变输出内容（preset + 暗部 aq + 抖动），
    # 与旧「非标准档才进指纹」语义等价。真源从 up.parse_state 换成 perf.encode_hq。
    # crf 不进指纹：它由 perf.x264_crf 单独给，且本来就不随项目存档走。
    if cfg.get("encode_hq"):
        keys["encode_hq"] = True
    # ⛔ `chunk` / `force_unload` **不再进指纹**（2026-09-23）：它们已迁到机器级
    # 性能设置，不再随项目存档走。以前「关掉分块」会把既有高清段全部判失效重做 ——
    # 那是拿显存手段当画质手段，代价还不小。分块口径变化对输出只有数值噪声级影响，
    # 不值得付全量重做的代价。
    dev = str(cfg.get("device") or "")
    if dev and dev != "auto":           # 设备：默认自动不进指纹，显式指定才进
        keys["device"] = dev
    if cfg.get("sampler"):
        keys["sampler"] = cfg["sampler"]
    if cfg.get("scheduler"):
        keys["scheduler"] = cfg["scheduler"]
    if cfg.get("retry"):
        keys["retry"] = cfg["retry_target"]
    return keys


def params_hash(cfg):
    """二采参数 -> 8 位指纹（mode/include 不进：不影响单段输出）。

    time_bias / mix / shift 仅在 >0、adaptive 仅在开启时进指纹——默认全关
    不改变哈希，既有二采记录不因新增参数而失效重做。adaptive 开启后每段
    生效 σ 由该段基础 latent（经 base_hash 指纹）决定论派生，无需逐段进
    指纹也不会串档。
    """
    return checkpoint.fingerprint(_hash_params(cfg))


def base_hash(manifest, g):
    """基础段身份指纹：prompt_hashes[g] + seeds[g]（基础链改词/换种子即失效）。"""
    hashes = list(manifest.get("prompt_hashes") or [])
    seeds = list(manifest.get("seeds") or [])
    return f"{hashes[g] if g < len(hashes) else ''}|{seeds[g] if g < len(seeds) else ''}"


def _records(manifest):
    up = manifest.get("upscale")
    segs = up.get("segs") if isinstance(up, dict) else None
    return list(segs) if isinstance(segs, list) else []


def _record_valid(segs, root, g, ph, bh):
    """记录有效 = hash/base_hash 匹配且产物文件齐全（高清分段 mp4 + 缩略图 + 尾帧锚）。

    四库兼容：files 里的 finals/ 前缀与旧根目录裸名双路径查找。
    """
    rec = segs[g] if g < len(segs) else None
    if not (isinstance(rec, dict) and rec.get("done")):
        return False
    if rec.get("hash") != ph or rec.get("base_hash") != bh:
        return False
    files = rec.get("files") or checkpoint.upscale_files(g)
    for key in ("mp4", "thumb", "last"):
        f = files.get(key)
        if not f or not os.path.isfile(checkpoint.resolve_project_file(root, f)):
            return False
    return True


def record_stale(manifest, root, cfg, g, bh=None):
    """段 g 的高清渲染是否待做（供主循环逐段判定；manifest 可为 None=全待做）。"""
    if cfg is None:
        return False
    if not in_scope(cfg, g):
        return False
    if manifest is None:
        return True
    ph = params_hash(cfg)
    if bh is None:
        bh = base_hash(manifest, g)
    return not _record_valid(_records(manifest), root, g, ph, bh)


def in_scope(cfg, g):
    """段 g 是否在二采范围内（跟随生成=全部段；手动选择=勾选段）。"""
    if cfg is None:
        return False
    if cfg["include"] is None:
        return True
    return g in cfg["include"]


# ---- latent 放大数学（纯 torch，可单测） ----

VAE_DOWNSAMPLE = 16


def _even(n):
    """整数向上取偶（latent 偶数 = 像素 32 对齐，官方画布口径）。"""
    n = max(2, int(n))
    return n + n % 2


def target_hw(h, w, scale):
    """latent 目标尺寸：round(H×scale) 向上取偶（latent 偶数 = 像素 32 对齐）。"""
    h2 = max(2, int(round(h * float(scale))))
    w2 = max(2, int(round(w * float(scale))))
    return h2 + h2 % 2, w2 + w2 % 2


def target_pixels(h, w, scale):
    """latent (H, W) -> 像素 (宽, 高)：与官方节点 width/height 语义同向（宽在前）。

    历史 bug：render_latent 曾按 target_hw(...)[0]/[1] 直接命名 tw/th——[0] 是
    latent 高、[1] 是宽，导致 cond 构造 width/height 对调（非方画幅时官方节点把
    空画布/首帧 keyframe 编成转置构图）、manifest size 与成片拼接目标也跟着反
    （拼接被喂 960×1728 这类竖参，Linux 实测 EINVAL 22）。统一走本函数杜绝复发。
    """
    h2, w2 = target_hw(h, w, scale)
    return w2 * VAE_DOWNSAMPLE, h2 * VAE_DOWNSAMPLE


def resolve_target_hw(h, w, cfg):
    """基础 latent (H, W) + 配置 -> (目标 latent H, W, 生效倍率)。

    三种模式（对齐上游 3d.py:541-556 的 UpscaleMode）：
    - 倍率：latent = round(H×scale) 向上取偶（现状）；
    - 目标尺寸：像素 (W,H) -> latent = 向上取偶(floor(px/16))——像素 32 对齐，
      目标可能略小于用户输入（最多差 30px，对齐到 32 倍数）；
    - 百万像素：按基础宽高比 aspect=w/h，h_px=sqrt(target_px/aspect)、w_px=h_px*aspect，
      latent 同样向上取偶。
    enlarge=False（纯精化）直接原尺寸直通——尺寸模式此时无意义（面板已隐藏
    对应字段），但旧存档可能残留目标尺寸设置，不做直通会误判下采样而炸链。
    生效倍率 = (w2/w + h2/h)/2（用于 scale embedding；目标尺寸/百万像素模式也
    需要告诉网络「放大了多少」）。下采样（生效倍率 < 1.0 且目标真小于基础）抛
    ValueError——放大网络只支持放大。
    """
    if not cfg.get("enlarge", True):
        return h, w, 1.0
    mode = str(cfg.get("size_mode") or "倍率")
    if mode == "目标尺寸":
        tw = max(64, int(float(cfg.get("target_w") or 1280)))
        th = max(64, int(float(cfg.get("target_h") or 704)))
        w2 = _even(tw // VAE_DOWNSAMPLE)
        h2 = _even(th // VAE_DOWNSAMPLE)
        scale = (w2 / w + h2 / h) / 2.0
    elif mode == "百万像素":
        mp = float(cfg.get("megapixels") or 1.0)
        target_px = mp * 1024 * 1024
        aspect = w / max(h, 1)
        h_px = (target_px / aspect) ** 0.5
        w_px = h_px * aspect
        w2 = _even(int(w_px) // VAE_DOWNSAMPLE)
        h2 = _even(int(h_px) // VAE_DOWNSAMPLE)
        scale = (w2 / w + h2 / h) / 2.0
    else:
        scale = float(cfg.get("scale") or 2.0)
        h2, w2 = target_hw(h, w, scale)
    if scale < 1.0 and (w2 < w or h2 < h):
        raise ValueError(
            f"放大网络只支持放大（生效倍率 {scale:.3f} < 1.0，目标 "
            f"{w2 * VAE_DOWNSAMPLE}×{h2 * VAE_DOWNSAMPLE} 小于基础 "
            f"{w * VAE_DOWNSAMPLE}×{h * VAE_DOWNSAMPLE}）——请调大目标或倍率")
    return h2, w2, scale


def upscale_video(video_t, net, scale, arch="auto", hw=None, chunk=True,
                  chunk_frames=0, overlap=0):
    """[B,24,T,H,W] latent -> 神经放大（T 不变，H/W 到目标尺寸偶数对齐），float32。

    归一化口径与上游训练一致：放大前 (x-μ)/σ，放大后反变换；网络精度由
    load_model 的 precision 决定，输入输出统一 float32。目标等于原尺寸时
    原样返回克隆（等价纯二采不放大）。
    arch 为 "auto"（或不认识的值）时按网络对象实际类型判定——架构自动判定后
    cfg["arch"] 可能仍是 auto，调用口径只认网络的真身，不认面板值。
    hw：显式目标 latent (H,W)（目标尺寸/百万像素模式）；给了就不再按 scale 换算
    尺寸，但 scale 仍进 scale embedding（网络需要知道「放大了多少」）。
    chunk / chunk_frames / overlap：3D 时序分块口径，由导演台性能设置下发
    （机器级、全局生效、不进二采指纹）。chunk=False 或 chunk_frames<=0 时
    upscale_net 落回内建默认（32 帧）；overlap=0 表示自动取时序卷积核宽。
    """
    if net is None:
        # 放大关闭（enlarge=False）：精化画布 = 基础画布，视频 latent 与桥/尾/头锚
        # latent 一律原尺寸直通——一处守卫覆盖 render_latent 的四个调用点。
        return video_t.detach().to(torch.float32).clone()
    from . import upscale_net
    ak = arch if arch in ("2D", "3D") else upscale_net.kind_of(net)
    h2, w2 = target_hw(video_t.shape[-2], video_t.shape[-1], scale) if hw is None else hw
    if (h2, w2) == (video_t.shape[-2], video_t.shape[-1]):
        return video_t.detach().to(torch.float32).clone()
    # 输入对齐网络设备：魔改 DynamicVRAM 运行时采样输出 latent 可能滞留 CPU，
    # 网络 @ cuda 时直接前向 = addmm 设备不匹配（mat1 on cpu）崩溃
    net_dev = next(net.parameters()).device
    nd = next(net.parameters()).dtype
    # ★ 必须包 no_grad（上游 3d.py:584 用 inference_mode，移植时丢了——见 UPSTREAM.md D11）。
    # 不包 = 每次放大前向都建 autograd 图并保存全部中间激活，而该图会被扣押到
    # 「精化采样结束」才释放（up_v 一路交给采样器、其间不 detach），与 UNET +
    # 精化激活正面相撞。放大网络全 F16 659MB，但 512 通道 × 高清 latent 的
    # 每 ResBlock 两张满尺寸特征图才是大头。
    # 用 no_grad 而非 inference_mode：后者产生 "inference tensor"，出了块再做
    # 原地操作会报 `Inplace update to inference tensor outside InferenceMode`，
    # 而 up_v 要交给采样器，风险不可控；no_grad 同样阻止建图且无此限制。
    with torch.no_grad():
        x = video_t.detach().to(net_dev, torch.float32)
        mean, std = _norm_tensors(x.device, nd)
        xn = (x.to(nd) - mean) / std
        if ak == "3D":
            y = net(xn, scale=float(scale), target_size=(x.shape[2], h2, w2),
                    enable_chunking=chunk, chunk_frames=chunk_frames,
                    overlap_frames=overlap)
        else:
            y = net(xn, scale=float(scale), target_hw=(h2, w2))
        # 反归一化也在块内：否则这一步又会把 y 挂回计算图
        mean32, std32 = _norm_tensors(x.device, torch.float32)
        return y.to(torch.float32) * std32 + mean32


def _norm_tensors(device, dtype):
    from . import upscale_net
    return upscale_net._make_norm_tensors(device, dtype)


def resize_latent_bilinear(z, h, w):
    """latent 空间双线性缩放（CondSync 兜底路径，JZL 同款）：T/C 不变。

    5D -> 4D reshape -> bilinear -> 还原；目标等于原尺寸时原样返回。
    """
    b, c, t, hh, ww = z.shape
    if (h, w) == (hh, ww):
        return z
    flat = z.reshape(b * t, c, hh, ww)
    out = torch.nn.functional.interpolate(flat, size=(h, w), mode="bilinear",
                                          align_corners=False)
    return out.reshape(b, c, t, h, w)


# ---- 运行期（需 ComfyUI 环境，单测不触达） ----

def _iter_tensors(obj, path="root"):
    """递归枚举 (路径, 张量)：cond/negative 设备审计用（纯数据结构遍历）。"""
    if isinstance(obj, torch.Tensor):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_tensors(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for j, v in enumerate(obj):
            yield from _iter_tensors(v, f"{path}[{j}]")


def _device_mismatches(obj, dev):
    """递归收集 obj 内不在 dev 上的张量路径（发现不对齐时面包屑定位用）。"""
    return [f"{p}@{t.device}" for p, t in _iter_tensors(obj) if t.device != dev]


def _tensors_to_device(obj, dev):
    """递归把 obj 内张量挪到 dev（cond/negative 写时复制对齐，结构保持）。"""
    if isinstance(obj, torch.Tensor):
        return obj.to(dev) if obj.device != dev else obj
    if isinstance(obj, dict):
        return {k: _tensors_to_device(v, dev) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return tuple(_tensors_to_device(v, dev) for v in obj)
    if isinstance(obj, list):
        return [_tensors_to_device(v, dev) for v in obj]
    return obj


def _cuda_if_room(min_free_gb=2.0):
    """intermediate_device 返回 CPU 时的纠偏探测：CUDA 可用且空闲显存充足则返回
    cuda 设备。魔改运行时（DynamicVRAM）账面紧张会把 intermediate_device 打回
    CPU——放大网络仅 ~0.7GB 且 3D 卷积 CPU 前向是分钟级（autodl 32G 卡实测
    疑似触发：单线程 99.9% 转 25 分钟、GPU 0%），显存真放不下时仍回落 CPU。"""
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        free, _total = torch.cuda.mem_get_info()
        if free >= min_free_gb * (1 << 30):
            return torch.device("cuda")
    except Exception:
        return None
    return None


def load_net(cfg):
    """按配置加载放大网络（upscale_net 按 名称+架构+设备+精度 缓存）。

    未选模型 / 权重文件缺失 / 架构不匹配 -> ValueError（带可操作的指引），
    调用方（nodes.py）在主循环前调用一次：当场报错降级为基础分辨率整链运行，
    不让每段反复撞同一个错。
    """
    import comfy.model_management

    from . import upscale_net

    if not cfg.get("enlarge", True):
        # 纯精化模式：不加载放大网络（省显存、省加载时间），latent 走原尺寸直通
        print("[H3二采] 神经放大已关闭：只做低强度重采样精化，不加载放大网络", flush=True)
        return None
    if not cfg["model"]:
        raise ValueError("未选择放大模型——请从 HuggingFace "
                         "LBH-123-AI/Minimax_h3_latent_Upscaler 下载权重放入 "
                         "models/latent_upscale_models/，刷新导演台后在二采面板选择"
                         "（只想精化不想放大的话，取消勾选「神经放大」即可）")
    dev = upscale_net.resolve_device(cfg)
    if dev is None:
        dev = comfy.model_management.intermediate_device()
        if dev.type == "cpu":
            dev2 = _cuda_if_room()
            if dev2 is not None:
                print(f"[H3二采] intermediate_device 返回 CPU 但空闲显存充足"
                      f"——放大模型强制上 {dev2}", flush=True)
                dev = dev2
    print(f"[H3二采] 放大模型目标设备：{upscale_net._backend_label(dev)}", flush=True)
    try:
        net = upscale_net.load_model(cfg["model"], dev, cfg["precision"], cfg["arch"])
    except FileNotFoundError as e:
        raise ValueError(f"放大模型加载失败：{e}") from None
    # 把解析出的真身架构写回 cfg：① auto 判定结果固化，后续 params_hash /
    # write_record 记录的是确定值（旧版存档的 arch 恒与权重匹配——旧加载代码
    # 强制一致——故 auto 解析后 hash 与旧记录兼容，既有二采记录不失效）；
    # ② upscale_video 的调用口径直接拿到 2D/3D，不必再问网络真身。
    cfg["arch"] = upscale_net.kind_of(net)
    return net


def _build_seg_refs(i, seg_label_orders, pool_tensors, refs):
    """段级参考素材压实（与主循环同式：状态池勾选 + 未接管画布接线类别接编号）。"""
    seg_refs = {"ref_images": {}, "ref_videos": {}, "ref_video_audios": {}, "ref_audios": {}}
    n = {"image": 0, "video": 0, "audio": 0}
    for k, lbl in seg_label_orders[i]:
        if lbl not in pool_tensors.get(k, {}):
            continue
        if k == "image":
            seg_refs["ref_images"][f"ref_image_{n['image']}"] = pool_tensors["image"][lbl]
        elif k == "video":
            imgs, aud = pool_tensors["video"][lbl]
            seg_refs["ref_videos"][f"ref_video_{n['video']}"] = imgs
            seg_refs["ref_video_audios"][f"ref_video_audio_{n['video']}"] = aud
        else:
            seg_refs["ref_audios"][f"ref_audio_{n['audio']}"] = pool_tensors["audio"][lbl]
        n[k] += 1
    for j, v in enumerate(refs["ref_videos"].values()):
        seg_refs["ref_videos"][f"ref_video_{n['video'] + j}"] = v
    for j, v in enumerate(refs["ref_video_audios"].values()):
        seg_refs["ref_video_audios"][f"ref_video_audio_{n['video'] + j}"] = v
    for j, v in enumerate(refs["ref_audios"].values()):
        seg_refs["ref_audios"][f"ref_audio_{n['audio'] + j}"] = v
    return seg_refs


class UpscaleAbortError(RuntimeError):
    """二采致命问题（显存不足 / 画布越界 / 放大模型缺失）——发现即在真正占显存前
    输出报告并终止整链。主循环单独 catch 它并 re-raise（不上报告且不降级），
    避免产出"混一段高清一段基础"的不一致结果。"""


def _diff_model(model):
    """从 ComfyUI 模型包装器取底层 diffusion model（拿不到就原样返回）。"""
    dm = getattr(model, "model", None)
    if dm is not None:
        return getattr(dm, "diffusion_model", dm)
    return model


def _vram_gb(unet_model=None):
    """当前空闲显存（GB；失败回退 torch，再失败返回 0）。

    口径（关键）：DynamicVRAM（comfy-aimdo）默认启用时，`mm.get_free_memory()`
    = `mem_free_cuda + mem_free_torch`，**不含** aimdo 可换出的权重页——该机制故意
    把显存当权重缓存填满，于是这个值严重低估「真实可动用显存」（常趋近 0）。
    `ModelPatcher.get_free_memory(device)`（model_patcher.py:417，基类上）在其上
    加了 `comfy_aimdo.model_vbar.vbars_analyze()`，才是官方自己解码路径用的口径
    （对照 `comfy/sd.py:1242`）。

    给了 `unet_model`（ModelPatcher）且它带 `get_free_memory` 时优先走该口径；
    拿不到就退回 `mm.get_free_memory()`（非 DynamicVRAM 环境两者等价）。
    """
    try:
        import comfy.model_management as mm
        if unet_model is not None:
            fn = getattr(unet_model, "get_free_memory", None)
            if callable(fn):
                try:
                    return fn(mm.get_torch_device()) / (1024 ** 3)
                except Exception:
                    pass
        return mm.get_free_memory() / (1024 ** 3)
    except Exception:
        try:
            import torch
            return torch.cuda.mem_get_info()[0] / (1024 ** 3)
        except Exception:
            return 0.0


def _vram_probe():
    """显存探针快照 `(torch 侧分配 GB, 设备级占用 GB)`；无 CUDA / 异常 -> `(None, None)`。

    ⚠ 第二个数原来是 `torch.cuda.max_memory_allocated()`，**在 DynamicVRAM /
    aimdo 环境下是废数**：H3 的 UNET/TE/VAE 权重由 ComfyUI 自己的池子（vbar）
    分配，不进 torch 的 caching allocator，于是 24GB 卡跑 32GB 模型、736×1312
    高清 latent 的精化阶段能报出 **0.43GB** 这种数（2026-09-22 真机）。
    只有裸 `nn.Module`（放大网络）走 torch 分配，所以「放大后 1.92GB」反而是真的。

    现改设备级口径 `total - free`（`torch.cuda.mem_get_info()`）：含权重池、
    激活、驱动与其它进程，量级真实。代价是它包含别的进程——共享卡上要按
    「跨段增量」看，不按绝对值看（与 RSS 行同一个看法）。

    本机 `nvidia-smi` 不可用（`Failed to initialize NVML`），只能用 torch 计数器。
    第一个数（torch 侧分配）保留：区分「权重池」与「普通张量」时仍有参考价值。
    """
    try:
        if not torch.cuda.is_available():
            return None, None
        alloc = torch.cuda.memory_allocated() / (1024 ** 3)
        free, total = torch.cuda.mem_get_info()
        used = (total - free) / (1024 ** 3)
        return (alloc, max(used, alloc))   # 设备占用恒 ≥ torch 侧分配，保序
    except Exception:
        return None, None


def _vram_probe_reset():
    """重置 CUDA 峰值统计（埋点用；无 CUDA / 异常静默跳过）。"""
    try:
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:
        pass


def _rss_probe():
    """进程常驻内存 RSS（GB）；量不到 -> None。

    显存账上完全看不见内存侧的累积（放大网络 CPU 副本 / cond 缓存 / 解码帧 /
    ComfyUI 节点输出缓存…），而「多段越跑越卡」的成因多半在这边。与显存同批采样，
    两条曲线一起看才分得清是显存真不够，还是内存被吃光导致换页（§5.5）。
    """
    try:
        return perf.rss_gb()
    except Exception:
        return None


def _vram_report(seg_no, v):
    """显存占用埋点汇总行（阶段 0 诊断；无数据返回 ""，调用方据此跳过打印）。

    六段**一律报设备级当时占用**（`_vram_probe` 第二个数）。

    为什么不再区分「当时占用 / 阶段峰值」：那个区分建立在
    `max_memory_allocated()` 上，而它在 DynamicVRAM 下量不到权重池（详见
    `_vram_probe`），「峰值」那一列长期是废数。统一成设备级占用后，
    这条线的原始目的才能真正达成——**卸载后那个数是不是真的掉下来了**。
    """
    if not v or v.get("up") is None:
        return ""

    def _f(key):
        t = v.get(key)
        return "n/a" if t is None or t[1] is None else f"{t[1]:.2f}"

    return (f"[H3二采] 段{seg_no} 显存占用（设备级）：放大前{_f('base')}GB · "
            f"放大后{_f('up')}GB · cond后{_f('cond')}GB · "
            f"卸载后{_f('unload')}GB · 精化后{_f('refine')}GB · "
            f"解码后{_f('decode')}GB")


def _rss_report(seg_no, r):
    """RSS 曲线行（阶段 0 诊断；无数据返回 ""，调用方据此跳过打印）。

    与显存行分开成两行而不是拼进去：显存行已经六个数字，再塞 RSS 会看不清。
    看点是**跨段同一位置的数字有没有往上爬**（段1 解码后 vs 段9 解码后），
    绝对值不重要——起步就有 ComfyUI 自身的常驻。
    """
    if not r or r.get("up") is None:
        return ""

    def _f(key):
        v = r.get(key)
        return "n/a" if v is None else f"{v:.2f}"

    return (f"[H3二采] 段{seg_no} 内存RSS：放大前{_f('base')} · 放大后{_f('up')} · "
            f"cond后{_f('cond')} · 卸载后{_f('unload')} · 精化后{_f('refine')} · "
            f"解码后{_f('decode')} GB")


_UNET_SIZE_CACHE = {}


def _unet_size_gb(model):
    """UNET 权重字节（GB，按 id 缓存——跟随生成每段预检不用重算）。"""
    key = id(model)
    v = _UNET_SIZE_CACHE.get(key)
    if v is None:
        try:
            dm = _diff_model(model)
            v = sum(p.numel() * max(p.element_size(), 2)
                    for p in dm.parameters()) / (1024 ** 3)
        except Exception:
            v = 0.0
        _UNET_SIZE_CACHE[key] = v
    return v


def _patch_digest(obj, n=256):
    """patch 张量轻量校验和：取前 n 个元素求和（区分同名 LoRA 的不同权重）。

    只读极小切片，成本可忽略；非张量/异常一律返回 ""（退化为不计入签名）。
    """
    try:
        if isinstance(obj, torch.Tensor):
            v = obj.detach().reshape(-1)[:n].float().sum().item()
            return f"{v:.6g}"
    except Exception:
        pass
    return ""


def model_tag(model):
    """二采模型结构签名（8 位 hex）——供报告标注与「换过模型」提示使用。

    刻意不缓存：按 id 缓存有 id 复用隐患（_UNET_SIZE_CACHE 那份是历史包袱，
    此处不复制），而每段算一次的开销只有一次参数量求和 + patches 切片，可忽略。

    只取结构性特征，不依赖 ModelPatcher 暴露权重文件名（ComfyUI 并不保存）：
    类 / 参数总量 / dtype 集合（区分 fp16·bf16·fp8·GGUF 等量化）/ 首张量形状 /
    patches 键集合与其强度与轻量校验和（区分 LoRA 种类与强度）/ object_patches 键。
    任一环节异常都返回 ""（退化为无标注，绝不因此中断二采）。
    """
    try:
        dm = model.model.diffusion_model
        ps = list(dm.parameters())
        if not ps:
            return ""
        parts = [
            type(dm).__name__,
            str(sum(int(p.numel()) for p in ps)),
            ",".join(sorted({str(p.dtype) for p in ps})),
            str(tuple(ps[0].shape)),
        ]
        patches = getattr(model, "patches", None) or {}
        try:
            items = sorted(((str(k), v) for k, v in patches.items()),
                           key=lambda kv: kv[0])
        except Exception:
            items = []
        parts.append(f"{len(items)}:{items[0][0] if items else ''}"
                     f":{items[-1][0] if items else ''}")
        for _k, _lst in items[:8]:
            try:
                if not _lst:
                    continue
                it = _lst[0]
                # ComfyUI patch 项形如 [strength, weight, strength_model]
                parts.append(f"{float(it[0]):.6g}")
                if len(it) > 1:
                    parts.append(_patch_digest(it[1]))
                if len(it) > 2:
                    parts.append(f"{float(it[2]):.6g}")
            except Exception:
                continue
        obj_patches = getattr(model, "object_patches", None) or {}
        try:
            parts.append(",".join(sorted(str(k) for k in obj_patches.keys())[:4]))
        except Exception:
            pass
        return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:8]
    except Exception:
        return ""


def models_distinct(a, b):
    """两个 ModelPatcher 是否是「真·两份权重」（True → 切换时值得先卸载对方）。

    二采接了独立模型时，每段要「卸 A 载 B / 卸 B 载 A」两次 UNET 级换页；
    在二采入口先卸一采，只是为了让空闲显存成一整块、且让 preflight 量到真实
    空闲——**换页次数不变**。所以同源时做这件事是纯浪费，必须判掉：

    - 同一对象（`a is b`，未接「二采模型」槽）：False；
    - 同一份权重的不同壳（`clone_base_uuid` 相同，典型 = 只差 LoRA 的克隆）：False
      ——它们共享底层权重，卸 a 等于把 b 也搬走，紧接着又要装回来；
    - 拿不到 `clone_base_uuid`（老版本 ComfyUI / 非 ModelPatcher）：保守返回 True
      ——此时至多多一次换页，不会出错；判成 False 才会让显存碎片问题留着。

    纯函数，不触碰权重、不需要 torch。
    """
    if a is None or b is None or a is b:
        return False
    ua = getattr(a, "clone_base_uuid", None)
    ub = getattr(b, "clone_base_uuid", None)
    if ua is None or ub is None:
        return True
    try:
        return ua != ub
    except Exception:
        return True


def model_mismatch_note(manifest, g, tag):
    """段 g 的存档高清产物由另一个二采模型生成时的提示行（None=无冲突无需提示）。

    只提示，不参与 record_stale 判定——按既定口径，模型身份不进 params_hash，
    换二采模型不会自动重做已有高清分段；要重做请手动设「重跑起始段」。
    """
    if not tag or not isinstance(manifest, dict):
        return None
    segs = _records(manifest)
    rec = segs[g] if g < len(segs) else None
    if not isinstance(rec, dict):
        return None
    old = rec.get("model")
    if not old or old == tag:
        return None
    return (f"⚠ 段{g + 1} 存档高清产物由另一个二采模型生成（{old}→{tag}）——"
            f"沿用旧产物，不自动重做；要重做请设「重跑起始段」")


_CANVAS_ABORT_MP = 12.0    # 二采画布超此（MP）→ 硬停（VAE 解码 + 显存双重爆点）
_ACTIVATION_FACTOR = 4.0   # 采样峰值激活 ≈ 初始高清 latent 体积 × 系数（经验折中）
_SAFE_MARGIN_GB = 1.0      # 显存账目安全余量（避免贴着上沿静默崩）


def _dynamic_vram_active():
    """comfy-aimdo DynamicVRAM 是否在管权重。该机制把显存当权重缓存故意填满、
    按需换页（空闲显存小是常态而非异常），权重占用是弹性的——显存账目对它
    只作参考不作硬约束。

    读**真开关** `comfy.memory_management.aimdo_enabled`（`main.py:301` 在
    aimdo 可用时置 True）。

    历史坑（别改回去）：旧实现查 `"aimdo" in sys.modules` / `find_spec("aimdo")`。
    那来自首次提交 `6eee9d3`，写的时候 aimdo 还不是默认启用，探测合理；但
    `comfy/model_management.py` 顶层就 `import comfy_aimdo.*`，而 render_latent
    必然 import 它 → 探测信号与真实开关彻底脱钩，**恒为 True**，预检的硬停
    分支因此永久失效。探测"包装是否装了"≠"开关是否开着"。
    """
    try:
        import comfy.memory_management as cmm
        return bool(getattr(cmm, "aimdo_enabled", False))
    except Exception:
        return False


def _reclaimable_gb(unet_model):
    """重采样前 unload_all_models() 必然释放的其他模型权重（TE/videoVAE/audioVAE，GB）。
    预检在 cond 构建之前测量——此刻它们仍驻留显存；真正采样发生在全卸之后、
    只回载 UNET，账目须把这部分确定性腾挪加回。DynamicVRAM 分页下 model_size
    是总量上界（实际驻留更少），只会放宽预检；真不够时由运行时 OOM 降级链兜底。"""
    try:
        import comfy.model_management as mm
        target = id(unet_model)
        total = 0
        for lm in mm.current_loaded_models():
            mp = getattr(lm, "model", None)
            size_fn = getattr(mp, "model_size", None) if mp is not None else None
            if size_fn is None or id(mp) == target:
                continue
            total += size_fn()
        return total / (1024 ** 3)
    except Exception:
        return 0.0


def _calc_scale_cap(h_latent, w_latent):
    """画布不超 _CANVAS_ABORT_MP 的最大放大倍率（向下取整到 0.5）。"""
    cur = (w_latent * 16) * (h_latent * 16)
    if cur <= 0:
        return 1.0
    cap = (_CANVAS_ABORT_MP * 1e6 / cur) ** 0.5
    return max(1.0, int(cap * 2) / 2.0)


def _vram_scale_cap(latent_gb, scale, free_gb):
    """显存允许的最大放大倍率（latent 体积 ∝ 倍率²，向下取整到 0.5）。"""
    base = latent_gb / (scale * scale)
    if base <= 0 or free_gb <= _SAFE_MARGIN_GB:
        return 1.0
    cap = ((free_gb - _SAFE_MARGIN_GB) / (_ACTIVATION_FACTOR * base)) ** 0.5
    return max(1.0, int(cap * 2) / 2.0)


def preflight(模型, cfg, net, video_t, audio_t, report=None):
    """二采前置健康预检——在【放大 / 分配高清内存之前】跑，把问题一次暴露：

    - 放大模型就绪；
    - 二采画布（target_hw ×16）超上限即停并提示可用的最大倍率；
      >2.5MP 提示 fp16 高频溢出花屏风险（不硬停，不挡正当高清需求）；
    - 显存账目 = 重采样时刻可用（当前空闲 + 重采样前必卸载的 TE/VAE 权重）
      对比 高清重采样新增峰值；非 DynamicVRAM 环境连最小需求都盖不住才停，
      DynamicVRAM（权重弹性换页）环境账面紧张只 ⚠ 不硬停。

    每行 "✓/⚠/✗ …"，任一 ✗ -> 完整报告 append 进 report 并抛 UpscaleAbortError，
    终止整链。正常返回报告行（供调用方登记，非致命 ⚠ 已含）。
    """
    lines = []
    fail = []

    # 1) 放大模型就绪（关闭放大时跳过——纯精化不需要放大网络，也不该因此判失败）
    if net is None:
        lines.append("… 神经放大：关闭（仅低强度重采样精化，不加载放大网络）")
    else:
        try:
            dev = next(net.parameters()).device
            from . import upscale_net
            lines.append(f"✓ 放大模型就绪：{upscale_net.kind_of(net)} @ {dev}")
        except (StopIteration, AttributeError, ImportError):
            lines.append("✓ 放大模型就绪")

    # 2) 二采画布（latent 偶数 -> 像素 ·32）：按 size_mode 解析目标 + 生效倍率
    h, w = video_t.shape[-2], video_t.shape[-1]
    try:
        h2, w2, scale = resolve_target_hw(h, w, cfg)
    except ValueError as e:
        lines.append(f"✗ {e}")
        fail.append("目标尺寸非法")
        h2, w2, scale = h, w, 1.0
    tw, th = w2 * VAE_DOWNSAMPLE, h2 * VAE_DOWNSAMPLE
    mp = tw * th / 1e6
    sm = str(cfg.get("size_mode") or "倍率")
    lines.append(f"… 画布：基础 {w * 16}×{h * 16} → 二采 {tw}×{th}"
                 f"（{mp:.1f}MP，生效 ×{scale:g}"
                 f"{'' if sm == '倍率' else f'，{sm}'}）")
    if mp > _CANVAS_ABORT_MP:
        cap = _calc_scale_cap(h, w)
        lines.append(f"✗ 二采画布 {tw}×{th}（{mp:.1f}MP）超上限 {_CANVAS_ABORT_MP:.0f}MP"
                     f"——VAE 解码与显存双重爆点；请调小目标尺寸/倍率（≤{cap:g}×）")
        fail.append(f"画布超限 {mp:.1f}MP")
    elif mp > 2.5:
        lines.append(f"⚠ 高清画布 {mp:.1f}MP 超 2.5MP 安全解码区，注意 fp16 高频溢出"
                     f"（出花屏/色块就降目标尺寸或提采样精度）")

    # 3) 显存账目：对比【重采样时刻】可用 vs 高清新增峰值。
    # 预检时 TE/VAE/UNET 常全驻留（DynamicVRAM 更会把显存当权重缓存填满，
    # 空闲趋近 0 是常态），但 render_latent 在采样前 unload_all_models()
    # 全卸、只回载 UNET——可用 = 当前空闲 + 其他模型权重（确定性腾挪）。
    # DynamicVRAM 下权重本身可按需换页（弹性），账面紧张只 ⚠ 不硬停，
    # 真 OOM 由运行时降级链（自动卸载重试/LOW_VRAM 分块）兜底。
    # 空闲显存走 patcher 口径（含 aimdo 可换出权重）——`模型` 就是二采 UNET 的
    # ModelPatcher，与下面 _reclaimable_gb / _unet_size_gb 同源，口径一致
    free = _vram_gb(模型)
    reclaim = _reclaimable_gb(模型)
    free_eff = free + reclaim
    dyn = _dynamic_vram_active()
    unet_gb = _unet_size_gb(模型)
    frames = grid.latent_t_to_frames(video_t.shape[2])
    latent_gb = (int(video_t.shape[1]) * frames * h2 * w2 * 4.0) / (1024 ** 3)
    need = latent_gb * _ACTIVATION_FACTOR + _SAFE_MARGIN_GB
    lines.append(f"… 显存账目：UNET 权重≈{unet_gb:.1f}GB · 高清重采样需新增≈"
                 f"{need:.1f}GB（含 {_SAFE_MARGIN_GB:.1f}GB 余量）· 可用≈{free_eff:.1f}GB"
                 f"（空闲 {free:.1f} + 卸载腾挪 {reclaim:.1f}）"
                 + ("· DynamicVRAM 权重可换页" if dyn else ""))
    if free_eff > 0 and free_eff < need:
        cap = _vram_scale_cap(latent_gb, scale, free_eff)
        if dyn:
            lines.append(f"⚠ 显存账面紧张（需 {need:.1f}GB / 可用≈{free_eff:.1f}GB）"
                         "——DynamicVRAM 会按需换出权重页，本段继续执行；"
                         "若真 OOM 由运行时降级（自动卸载重试/LOW_VRAM 分块）兜底。")
        else:
            lines.append(f"✗ 显存不足：放大后重采样至少还需 {need:.1f}GB，"
                         f"可用仅≈{free_eff:.1f}GB——请把放大倍率降到 ≤{cap:g}×、"
                         "缩短该段帧数或降低基础分辨率（精化步数/起始σ不影响峰值显存），"
                         "或增大显存。本报告在分配任何高清内存前生成。")
            fail.append("显存不足")

    if fail:
        msg = "二采健康预检未通过（已停止运行）：\n" + "\n".join(lines)
        if report is not None:
            report.append(msg)
        raise UpscaleAbortError(msg)
    return lines


def time_bias_guard(dit, sigma_start, bias):
    """尾段时间偏置守卫：patch dit.forward，返回恢复函数（try/finally 调用）。

    Detail-Daemon 类技巧（T8mars/comfyui-minimax-h3-audio-T8 机制移植，GPL）：
    在精化尾窗内只把喂给共享 AV Transformer 的 timestep（=σ×1000）向更干净方向
    偏置——模型「看到稍干净的时间」从而多抠细节；不加噪声、NFE 不变。二采
    输出的音频 latent 被丢弃，偏置波及音频 token 的时间嵌入也不影响产物。
    与 cond_audio_rows_guard（挂 _cond_audio_rows 属性）不冲突；bias≤0 不安装。
    """
    orig_forward = dit.forward

    def patched_forward(x, timestep, context, transformer_options={}, **kwargs):
        sigma_v = float((timestep.flatten()[0] / 1000.0).clamp(min=1e-6))
        seen = time_bias_sigma(sigma_v, sigma_start, bias)
        if seen != sigma_v:
            timestep = torch.full_like(timestep, seen * 1000.0)
        return orig_forward(x, timestep, context, transformer_options, **kwargs)

    dit.forward = patched_forward
    return lambda: setattr(dit, "forward", orig_forward)


def _shifted_model(model, shift_video):
    """二采专用 shift 模型（镜像官方 MiniMaxH3SigmaShift 节点，路线⑤）。

    clone + add_object_patch 只喂给二采的 common_ksampler——主链模型对象
    零改动、无需恢复（比原地 patch+回滚安全）。video shift 驱动采样器
    sigma 网格（ModelSamplingAV），并同步写 transformer_options 的
    minimax_h3_sigma_shift_video/audio（DiT 反演共享基网格要用，官方节点
    同款契约）；audio_shift 原样透传主链现值（默认 3.0）。
    T8 实证：高分辨率二采档 shift 12→6 调度更线性、细节合成更充分。
    """
    import comfy.model_sampling

    if not hasattr(comfy.model_sampling, "ModelSamplingAV"):
        raise RuntimeError(
            "二采调度偏移需要含 ModelSamplingAV 的新版 ComfyUI；请升级 ComfyUI 后重试")
    if not hasattr(comfy.model_sampling, "CONST"):
        raise RuntimeError(
            "二采调度偏移需要 ComfyUI 的 CONST 采样预测类型；请升级 ComfyUI 后重试")

    m = model.clone()

    class ModelSamplingAdvanced(comfy.model_sampling.ModelSamplingAV,
                                comfy.model_sampling.CONST):
        pass

    model_config = getattr(getattr(model, "model", None), "model_config", None)
    if model_config is None:
        raise RuntimeError("二采调度偏移无法读取 H3 model_config；请确认输入为原生 MiniMax H3 模型")
    original = m.get_model_object("model_sampling")
    ms = ModelSamplingAdvanced(model_config)
    audio_shift = getattr(original, "audio_shift", None)
    if audio_shift is None:
        audio_shift = 3.0
    ms.set_parameters(shift=float(shift_video), audio_shift=audio_shift)
    if getattr(original, "noise_scale", None) is not None:
        ms.set_noise_scale(original.noise_scale)
    m.add_object_patch("model_sampling", ms)
    to = m.model_options["transformer_options"] = dict(
        m.model_options.get("transformer_options", {}))
    to["minimax_h3_sigma_shift_video"] = float(shift_video)
    to["minimax_h3_sigma_shift_audio"] = float(audio_shift) \
        if audio_shift is not None else 3.0
    return m


# H3 共享 AV Transformer 的 double block 总数（T8 源码 H3_DOUBLE_BLOCK_COUNT=50）
_H3_DOUBLE_BLOCK_COUNT = 50


def _stg_model(model, sigma_start, scale, block, lo=0.15, hi=0.85):
    """STG 细节引导（抗糊 N1）：跳块差分引导，CFG=1.0 下有效（无 CFG 分支依赖）。

    机制出处：T8mars/comfyui-minimax-h3-audio-T8 的
    apply_h3_spatiotemporal_guidance（GPL-3.0，机制移植非逐行拷贝）。本实现
    与上游的两点口径差异：
    - 激活窗口用【本段精化的局部进度】refine_progress = 1 − σ/σ₀（上游用
      shift 逆变换的全程 flow progress——那是全量生成的口径；二采是尾段
      精化，σ 已从 σ₀ 起步，局部进度才与「精化进行到哪」对齐，且与
      shift 取值解耦、无需逆变换）；
    - 差分弱分支经 calc_cond_batch 复用完整 cond（含桥锚 keyframe）——
      引导方向与主精化条件一致，不会把段首拉离桥锚。

    每激活步多跑一次「跳掉第 block 个 double block」的弱前向（返回输入
    不变的 no-op patch），输出 = denoised + (cond_denoised − weak)·scale：
    完整前向比弱前向「多出来」的细节被显式放大——CFG=1.0 下的自差分引导。
    scale=0 不安装（关）；clone 挂 sampler_post_cfg_function + dit
    patches_replace，主链模型对象零改动。
    """
    if float(scale) <= 0.0:
        return model

    import comfy.model_patcher
    import comfy.samplers

    block = int(block)
    source_options = getattr(model, "model_options", {}) or {}
    if source_options.get("sampler_post_cfg_function"):
        raise ValueError(
            "二采 STG 检测到已有 sampler_post_cfg_function，拒绝静默叠加引导回调；"
            "请关闭其他采样后引导或使用显式组合器")
    source_replacements = (source_options.get("transformer_options", {})
                           .get("patches_replace", {}).get("dit", {}))
    patch_key = ("double_block", block)
    if patch_key in source_replacements:
        raise ValueError(
            f"二采 STG 检测到 double block {block} 已有 replacement，拒绝覆盖其他模型补丁")

    m = model.clone()

    def _skip_block(args, _extra_args):
        return args                                   # no-op：跳过该 double block

    def _post_cfg(args):
        sigma_v = float(args["sigma"].flatten()[0].detach().cpu())
        p = refine_progress(sigma_v, sigma_start)
        if not (lo <= p <= hi):
            return args["denoised"]
        cond = args.get("cond")
        if cond is None:
            raise RuntimeError("二采 STG 需要正向 conditioning，当前采样回调未提供 cond")
        runtime_replacements = (args.get("model_options", {})
                                .get("transformer_options", {})
                                .get("patches_replace", {}).get("dit", {}))
        if patch_key in runtime_replacements:
            raise RuntimeError(
                f"二采 STG 运行时检测到 double block {block} replacement 冲突，已停止弱分支")
        stg_options = comfy.model_patcher.create_model_options_clone(
            args["model_options"])
        stg_options = comfy.model_patcher.set_model_options_patch_replace(
            stg_options, _skip_block, "dit", "double_block", block)
        (weak,) = comfy.samplers.calc_cond_batch(
            args["model"], [cond], args["input"], args["sigma"],
            stg_options)
        return args["denoised"] + (args["cond_denoised"] - weak) * float(scale)

    m.set_model_sampler_post_cfg_function(_post_cfg)
    return m


def render_latent(模型, clip, video_vae, audio_vae, negative, cfg, net,
                  video_t, audio_t, kind, idx, seg_prompts, seg_label_orders,
                  pool_tensors, refs, first_frame, guide, tail_kf_latent, head_kf_latent,
                  cur_seed,
                  采样器, 调度器, report=None, _timing=None, seg_no=None,
                  _vram=None, _rss=None):
    """基础段 AV latent -> 高清视频 latent（放大 + 低强度重采样）。

    _vram / _rss：阶段 0 诊断埋点的**出参容器**（dict，由 render_segment 建好后
    传进来，函数内只往里写）。默认 None = 零开销不采样。
    ⚠ 这两个形参之前漏了，而 render_segment 一直在传 `_vram=_vram` ——
    每次二采都会在调用处 TypeError（埋点从未在真机跑通过，故一直没暴露）。

    kind: "prompt"（提示词段，cond 带本段提示词/参考素材/首帧）
          / "insert"|"prologue"（外部素材段，空提示词轻精修）。
    guide: 本段生成时用的上段尾帧桥（None=首段/断链/插入段）——视频 latent
    神经放大后注入重采样 cond（CondSync：锚住段首与上段高清尾的连续性）。
    tail_kf_latent: 尾帧身份锚定的基础 latent（与主循环同语义，同步放大注入）。
    head_kf_latent: 段级首帧图引用的头锚基础 latent（中段勾首帧图时，同步放大注入）。
    cfg.mix>0 时精化输出做频域细节混合（低频锚回纯放大 latent，见
    freq_mix_latents）；cfg.sharpen>0 时再做 latent 域锐化（sharpen_latents）；
    细节增益在混合/锐化后的交付 latent 上度量。精化链支持多轮递降 cascade
    （passes）、STG 细节引导（stg）、独立采样器/调度器（sampler/scheduler）
    与增益自适应重试（retry）——全部默认关，详见各参数 docstring。
    返回 (up_out_v 高清视频 latent, tw, th, 二采种子, 是否桥锚, 细节增益小数,
    是否重试取优)。
    """
    import comfy.model_management
    import comfy.nested_tensor
    import nodes as comfy_nodes
    from comfy_extras.nodes_minimax_h3 import MiniMaxH3ImageToVideo, MiniMaxH3ReferenceToVideo

    from . import nodes as plugin_nodes

    def _tmark(key, t0):
        # 耗时账（render_segment 的 ⏱ 分解行数据源）；_timing=None 时零开销
        if _timing is not None:
            _timing[key] = _timing.get(key, 0.0) + (time.perf_counter() - t0)

    def _mark(key, label):
        """一次采齐显存 + RSS：**立即打印并落盘**，不等段末汇总。

        为什么必须逐点输出而不是段末统一打：云端显存不足的下场是
        **OOM killer 发 SIGKILL** —— 没有 except、没有 finally 的机会，
        段末那行汇总一定丢失；而最需要看数据的恰恰就是崩掉的那一段。
        三条保险（从最不丢到最丢）：JSONL 落盘 > 逐点 print > 段末汇总。
        _vram/_rss 都为 None 时零开销，不影响生产路径。
        """
        if _vram is None and _rss is None:
            return
        if _vram is not None:
            _vram[key] = _vram_probe()
            _vram_probe_reset()
        if _rss is not None:
            _rss[key] = _rss_probe()
        _v = (_vram or {}).get(key)
        _r = (_rss or {}).get(key)
        _alloc = None if _v is None else _v[0]
        _used = None if _v is None else _v[1]
        # 一律报设备级占用：torch 侧分配（_alloc）量不到 DynamicVRAM 权重池，
        # 拿它当「显存」会读出 0.05GB 这种假象（2026-09-22 真机）
        _vb = "n/a" if _used is None else f"{_used:.2f}"
        _rb = "n/a" if _r is None else f"{_r:.2f}"
        print(f"[H3二采] 段{seg_no} · {label} 显存{_vb}GB · RSS{_rb}GB", flush=True)
        perf.emit({"kind": "mark", "seg": seg_no, "stage": key, "label": label,
                   "vram_alloc": _alloc, "vram_peak": _used, "vram_used": _used,
                   "rss": _r})

    if net is not None:
        dev = next(net.parameters()).device
        if dev.type == 'cpu':
            # 上轮二采尾部 net.cpu() / 缓存命中残留都可能让网络留在 CPU。
            # 纠偏必须在腾挪之后：基础采样刚结束时 UNET/TE/VAE 全驻留，
            # 账面 free 极小、_cuda_if_room 必失败，原地纠偏必回落 CPU。
            # 先 unload_all_models 腾出真空闲再抢 GPU；cond 构建随后按需回载
            # TE/VAE（Comfy 原生机制），精化前本就还有一次全卸，只多一次换页。
            _t_pre = time.perf_counter()
            try:
                comfy.model_management.unload_all_models()
                gc.collect()
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                print(f"[H3二采] 段{seg_no}：放大前显存腾挪（放大网络在 CPU，先卸驻留模型再抢 GPU）…",
                      flush=True)
            except Exception:
                pass
            # 把这笔交易记清楚：**为了使多少显存腾走多少**。放大网络是裸 nn.Module
            # （ComfyUI 的显存账看不见它，也无法按需挤占），所以只能手工上卡；
            # 而手工上卡就得先手工腾地方——这里用的却是 `unload_all_models()`
            # 这个**全卸**，把 TE/VAE 也一起赶走了，而 cond 构建马上就要用 TE。
            # 打出来才知道这笔交换划不划算（`net.cpu()` 那一侧的 659MB 与之相比
            # 微不足道，真正的大头是这次全卸）。
            try:
                _nbytes = getattr(net, "_h3_bytes", None)
                if _nbytes is None:
                    _nbytes = sum(int(p.numel()) * int(p.element_size())
                                  for p in net.parameters())
                    net._h3_bytes = _nbytes
                _t_pre_el = time.perf_counter() - _t_pre
                print(f"[H3二采] 段{seg_no}：为把放大网络 {_nbytes / (1024 ** 2):.0f}MB "
                      f"搬上卡，全卸腾挪耗时 {_t_pre_el:.1f}s", flush=True)
                perf.emit({"kind": "pre_unload", "seg": seg_no,
                           "net_mb": _nbytes / (1024 ** 2),
                           "seconds": round(_t_pre_el, 2)})
            except Exception:
                pass
            # 上段二采异常后放大网络留在 CPU：优先纠偏回 GPU（intermediate_device
            # 在魔改运行时账面紧张时可能仍返回 CPU——此时 CPU 前向是分钟级）
            dev = _cuda_if_room() or comfy.model_management.intermediate_device()
            if dev.type == 'cuda':
                print(f"[H3二采] 段{seg_no}：放大网络在 CPU，已挪回 {dev}", flush=True)
            else:
                print(f"[H3二采] 段{seg_no}：⚠ 放大网络仍在 CPU（腾挪后空闲仍不足 2GB）——"
                      f"3D 卷积 CPU 前向是分钟级（非卡死，块进度会逐块打印）；"
                      f"可把「设备」设为 cuda、调小放大倍率/目标尺寸，或开强制卸载", flush=True)
            net.to(dev)
    else:
        # 放大关闭：无放大网络可问设备，退回 ComfyUI 中间设备（dev 仍需有值——
        # 下面桥/尾/头锚 latent 与 video_t 都按它对齐；精化执行设备另见 _edev）
        dev = comfy.model_management.intermediate_device()
    if dev.type == "cuda" and video_t.device != dev:
        # 魔改 DynamicVRAM 运行时采样输出可能滞留 CPU：统一对齐后再进放大，
        # 防止「输入 CPU × 网络 cuda」的 addmm 设备崩溃
        video_t = video_t.to(dev)

    # 二采真语义：先由放大网络把 latent 超分到 scale×，再在【高清 latent】上低强度重采样。
    # 放大网络的逐段输出在此被使用（重采样初始 latent），不存在「白放大」。
    # 显存账：H3 UNET fp16 常驻 ~26GB，2× 高清激活 ~5.4GB（=基础 4×），26+5.4≈31.4GB，
    # 正好贴着 32GB 卡的 31.36 限制上沿——故必须把联动的 CLIP + video/audio VAE 一起腾干净：
    # 重采样前 unload_all_models()（全卸到 CPU，含 UNET），再由 common_ksampler 原生机制
    # 只回载 UNET 到 GPU——CLIP/VAE 不再占显存，26GB UNET + 高清激活即可放下。
    # 未触发 OOM 则原样跑；一级降级=全卸只回 UNET；二级降级=LOW_VRAM 分块兜底。
    h, w = video_t.shape[-2], video_t.shape[-1]
    length = grid.latent_t_to_frames(video_t.shape[2])

    # 前置健康预检：在放大/分配高清内存【之前】把显存不足、画布越界、模型缺失、
    # 目标尺寸非法（下采样等）一次暴露——任一 ✗ 抛 UpscaleAbortError，由主循环
    # re-raise 终止整链（不降级）。必须先于 resolve_target_hw：尺寸配置错误经由
    # preflight 转成 UpscaleAbortError（防「一段高清一段基础」混合产物），而不是
    # 裸 ValueError 被 nodes.py 按普通异常逐段降级。
    preflight(模型, cfg, net, video_t, audio_t, report)

    # 预检已通过（同一纯函数、同参数），此处解析不会再抛
    h2, w2, eff_scale = resolve_target_hw(h, w, cfg)
    tw, th = w2 * VAE_DOWNSAMPLE, h2 * VAE_DOWNSAMPLE

    # 放大网络放大视频 latent 到 scale×（常驻 GPU，放大完卸回 CPU 腾给高清重采样）；
    # hf_up = 纯放大（无精化）的高频能量基线——细节增益度量的「前」
    _vram_probe_reset()
    _mark("base", "放大前")     # 埋点①放大前
    _t = time.perf_counter()
    up_v = upscale_video(video_t, net, eff_scale, cfg["arch"],
                         hw=(h2, w2), chunk=cfg.get("chunk", True),
                         chunk_frames=cfg.get("upscale_chunk_frames", 0),
                         overlap=cfg.get("upscale_overlap", 0))
    hf_up = latent_hf_energy(up_v)
    # 频域细节混合启用时保留纯放大 latent 的 CPU 副本（避开采样期显存峰值，
    # 精化后作低频锚；关闭时零开销不复制）
    mix = float(cfg.get("mix") or 0.0)
    up_v_base = up_v.detach().to("cpu", torch.float32) if mix > 0.0 else None
    _tmark("up", _t)
    _mark("up", "放大后")      # 埋点②放大后（autograd 建图带来的额外激活在此可见）
    _up_done = "神经放大完成" if net is not None else "跳过神经放大（纯精化，原分辨率）"
    print(f"[H3二采] 段{seg_no}：{_up_done} → 高清条件构建"
          "（挂了参考素材时需按高清画幅重编码，分钟级属正常，此间 GPU 应有占用）…",
          flush=True)

    # cond 构造：按【高清目标分辨率】（重采样画布），refs/首帧由官方节点编码到高清尺寸
    _t = time.perf_counter()
    if _timing is not None:
        _timing["te_hit"] = None
        if hasattr(clip, "last_encode_hit"):
            clip.last_encode_hit = None   # 清残留：官方节点未走拦截路径时保持「未知」而非误报命中
    if kind == "prompt":
        has_refs = any(pool_tensors.values()) or any(refs.values())
        if has_refs:
            out = MiniMaxH3ReferenceToVideo.execute(
                clip=clip, vae=video_vae, audio_vae=audio_vae,
                prompt=seg_prompts[idx], width=tw, height=th, length=length,
                ref_image_size="match",
                **_build_seg_refs(idx, seg_label_orders, pool_tensors, refs))
        else:
            out = MiniMaxH3ImageToVideo.execute(
                clip=clip, vae=video_vae, prompt=seg_prompts[idx],
                width=tw, height=th, length=length,
                first_frame=first_frame if idx == 0 else None)
    else:
        out = MiniMaxH3ImageToVideo.execute(
            clip=clip, vae=video_vae, prompt="", width=tw, height=th, length=length)
    cond, latent = out[0], out[1]

    # 桥锚 CondSync：上段尾帧桥/尾帧锚/首帧图头锚的 latent 同步神经放大后注入
    # 重采样 cond——锚住段首与上段高清尾的连续性（只依赖上段基础 latent + scale，
    # 可独立重做）
    bridged = False
    if kind == "prompt" and (guide is not None or tail_kf_latent is not None
                             or head_kf_latent is not None):
        up_guide = None
        if guide is not None:
            up_guide = dict(guide)
            up_guide["latent"] = upscale_video(guide["latent"].to(dev, torch.float32),
                                               net, eff_scale, cfg["arch"],
                                               hw=(h2, w2), chunk=cfg.get("chunk", True),
                                               chunk_frames=cfg.get("upscale_chunk_frames", 0),
                                               overlap=cfg.get("upscale_overlap", 0))
            bridged = True
        up_tail = None
        if tail_kf_latent is not None:
            up_tail = upscale_video(tail_kf_latent.to(dev, torch.float32),
                                    net, eff_scale, cfg["arch"],
                                    hw=(h2, w2), chunk=cfg.get("chunk", True),
                                    chunk_frames=cfg.get("upscale_chunk_frames", 0),
                                    overlap=cfg.get("upscale_overlap", 0))
        up_head = None
        if head_kf_latent is not None:
            up_head = upscale_video(head_kf_latent.to(dev, torch.float32),
                                    net, eff_scale, cfg["arch"],
                                    hw=(h2, w2), chunk=cfg.get("chunk", True),
                                    chunk_frames=cfg.get("upscale_chunk_frames", 0),
                                    overlap=cfg.get("upscale_overlap", 0))
        # 汇总成 keyframe 列表再注入（_apply_guide 现在只收列表）：桥 → 头锚@0 → 尾锚@末
        _up_kfs = []
        if up_guide is not None:
            _up_kfs.append(up_guide)
        if up_head is not None:
            _up_kfs.append({"resolved_frame_index": 0, "latent": up_head})
        if up_tail is not None:
            _up_kfs.append({"resolved_frame_index": length - 1, "latent": up_tail})
        if _up_kfs:
            cond = plugin_nodes.H3SeamlessChainSampler._apply_guide(cond, _up_kfs, length)

    # 放大网络工作完毕，卸回 CPU 释放显存给高清重采样（纯精化模式无网络可卸）
    if net is not None:
        net.cpu()
    _tmark("cond", _t)
    _mark("cond", "cond后")    # 埋点③高清条件后（TE/VAE 重编码的峰值在此可见）
    # CachedClipProxy 在位时标注本段高清条件构建的 TE 是否命中缓存
    if _timing is not None:
        _timing["te_hit"] = getattr(clip, "last_encode_hit", None)
    _te_hit = None if _timing is None else _timing.get("te_hit")
    _te_note = "" if _te_hit is None else \
        ("（TE命中缓存，无文本编码器前向）" if _te_hit is True
         else "（TE未命中——文本编码器前向中，魔改分页环境下可能分钟级）")
    print(f"[H3二采] 段{seg_no}：{'放大+' if net is not None else ''}高清条件就绪{_te_note}"
          + " → 显存腾挪（卸载驻留模型）…", flush=True)
    # 抢占式显存腾挪（README 既定设计，be43db8 重构时随三级降级一起被误删，
    # 2026-08-25 1.4× 精化实测 OOM 后恢复）：cond 已建好，TE/videoVAE/audioVAE
    # 精化阶段均用不到——全卸到 CPU（含 UNET），common_ksampler 原生机制只回载
    # UNET；否则 32GB 卡上 TE 驻留 + UNET + 高清激活三头挤兑，精化必 OOM。
    # gc.collect：跨段累积的 Python 侧 GPU 张量（上段精化输出/条件中间量/
    # STG 克隆）只有引用回收后显存块才真正归还——多段链后段比首段更易 OOM 的
    # 主因即在此（引用挂着的 CUDA 块 empty_cache 也收不回）
    #
    # ⚠ 2026-09-22 真机实测留下的 A/B 待办（**未改行为**，改前必须先实测）：
    # 在 24GB 卡 / 32GB 模型上，模型本来就装不下、本来就在流式分页，这次全卸
    # 的实测代价是 RSS 4.98 → 36.96GB（+32GB 页面换入），精化 3 步共 69s
    # （进度条本身只 61s）——即「赶走主模型 → 精化时原样搬回」付了一次完整
    # 32GB 传输。待验的两个替代方案：
    #   ① 只卸 TE/VAE、保留 UNET（本段马上要用它，卸了立刻装回，纯亏一次传输）
    #   ② 干脆不卸，交给 common_ksampler 内部的 load_models_gpu 按需挤占
    #      （本函数上面那句注释自己就写了「原生机制只回载 UNET」）
    # 判据：同一段重跑，比 69s 短且不 OOM 才算赢。32GB 卡上的既有 OOM 实测
    # 仍然有效——换卡前不要凭这台 24GB 的数据改默认行为。
    _t_unload = time.perf_counter()
    # 腾挪强度（性能设置 `vram_shuffle`）：这是「节点端适配」替代启动参数
    # `--vram-headroom` 的那一档——0.35 把显存吃到 ~100% 是官方有意拉高 H3
    # 利用率，我们不给启动参数建议，只在自己要腾挪时按用户的卡选强度。
    #   off  = 不卸（只回收残留 + 归还缓存块）
    #   soft = 只卸放大网络（它是裸 nn.Module，ComfyUI 的账看不见它）
    #   full/auto = 全卸驻留模型（现状，32GB 卡上有过 OOM 实测才加的）
    # ⚠ auto 沿用 full：24GB 卡上疑似净亏（RSS 4.98→36.96GB），但换卡前只做
    # A/B 不改默认——这条待办见 docs/性能优化_下一轮计划_2026-09-23.md §3。
    _shuffle = str(cfg.get("vram_shuffle") or "auto").lower()
    _blocked = bool(cfg.get("guard_block"))
    if _blocked and _shuffle in ("auto", "full"):
        # 落盘守卫=block 且判定 critical：全卸会把权重挤出到 swap，Linux 无
        # swap 时不是变慢而是**进程被 SIGKILL**。宁可不卸，也不冒这个险。
        _shuffle = "off"
        print(f"[H3二采] 段{seg_no}：落盘守卫判定 critical —— 本次跳过全卸"
              "（无 swap 机器上全卸会被 OOM killer 杀掉）", flush=True)
    if _shuffle == "soft" and net is not None:
        try:
            from . import upscale_net
            upscale_net.force_unload(net)
        except Exception:
            pass
    elif _shuffle in ("", "auto", "full"):
        comfy.model_management.unload_all_models()
    # off：什么都不卸，只回收残留引用 + 归还空闲缓存块
    gc.collect()
    torch.cuda.empty_cache()
    _tmark("unload", _t_unload)   # 卸载本身花了多久（腾挪的分子成本）
    _mark("unload", "卸载后")   # 埋点④卸载后（报当时占用——腾挪到底回收了多少，就看它）
    # 显式打出**回收量**：它是「这次腾挪值不值」的分子。以前要读者自己拿
    # 「cond后 − 卸载后」去减，而这两个数在两行日志里隔了好几行，没人会去减。
    _c = (_vram or {}).get("cond")
    _u = (_vram or {}).get("unload")
    if _c and _u and _c[1] is not None and _u[1] is not None:
        print(f"[H3二采] 段{seg_no}：腾挪回收显存 {_c[1] - _u[1]:+.2f}GB"
              f"（cond后 {_c[1]:.2f} → 卸载后 {_u[1]:.2f}）"
              f" · 卸载耗时 {(_timing or {}).get('unload', 0.0):.1f}s", flush=True)
    # 面包屑：本行之后应立刻出现「Requested to load MiniMaxH3」+ 精化进度条；
    # 长时间没有 = 主模型回载（DynamicVRAM 分页加载）或上面的卸载调用本身卡死
    print(f"[H3二采] 段{seg_no}：显存已腾挪，精化重采样回载主模型…", flush=True)

    # 低强度重采样（高清 latent）：初始 = 放大后的视频 latent + 原音频 latent；
    # 采样输出的音频丢弃，分段/成片音轨 = 原轨（零音频回归）。
    # 参数即「sigma 尾段精化」直接语义：denoise=尾段起始 σ（默认 0.35 低噪声区间）、
    # steps=精化步数（默认 6，建议 3-8；参考工作流即「放大后 3-5 步低噪声 refine」）。
    # 提分辨率靠放大网络、二采只补细节，显存开销小；tail_refine_args 把两个
    # 直接语义映射回 common_ksampler(steps=n, denoise=σ₀)——ComfyUI ≥0.3x 的
    # steps 即实际执行步数、denoise 定 σ 起点。
    seed = (int(cur_seed) + 1) % 0xffffffffffffffff if cur_seed is not None else 1
    # 精化执行设备 = 主模型运行设备（与放大网络设备无关：net 回 CPU 腾显存后
    # dev 仍是 cuda，但 CPU 兜底路径下 up_v 会滞留 CPU → UNET cuda 前向崩溃）
    _edev = comfy.model_management.get_torch_device()
    latent["samples"] = comfy.nested_tensor.NestedTensor(
        (up_v.to(_edev, torch.float32), audio_t.to(_edev, torch.float32)))
    del up_v
    # cond/negative 设备对齐：CachedClipProxy 缓存的 cond 张量可能滞留异设备
    # （魔改运行时 TE 前向设备不稳定），不对齐就是精化第一步 addmm 的 mat1
    _mis = _device_mismatches(cond, _edev) + _device_mismatches(negative, _edev)
    if _mis:
        _mis_txt = "; ".join(_mis[:3]) + ("…" if len(_mis) > 3 else "")
        print(f"[H3二采] 段{seg_no}：cond/negative 存在异设备张量"
              f"（{_mis_txt}）——已对齐到 {_edev}", flush=True)
        cond = _tensors_to_device(cond, _edev)
        negative = _tensors_to_device(negative, _edev)

    restore_rows = plugin_nodes.cond_audio_rows_guard(模型.model.diffusion_model)
    # 参考图与段间桥/锚点同段共存时，官方 0.33.x extra_conds 会丢 keyframe latent
    # （cond_video_latents 被 refs 分支覆盖）——二采同样要兜底，否则段2 高清
    # 精化的第一步就形状错位
    restore_video_rows = plugin_nodes.cond_video_rows_guard(模型.model.diffusion_model)

    def _is_oom(e):
        return "out of memory" in str(e).lower()

    # ⚠ 显存不足直接报错停止（用户明确要求，不做静默降级）：二采在高清 latent 上
    # 采样需要额外显存，若 32GB 卡放不下 26GB UNET + 高清激活，当场抛出明确错误，
    # 前端/报告会终止整链而不是降级基础分辨率继续（避免产出不一致的混合清晰度）。
    # σ 起点先过段自适应（路线④：基础 latent 运动档位偏移，与 render_segment
    # 报告行同口径）；shift>0 时采样器换二采专用 shift 克隆模型（路线⑤）。
    sigma0, _tier, _motion = resolve_refine_sigma(cfg, video_t)
    tb = float(cfg.get("time_bias") or 0.0)
    sh = float(cfg.get("shift") or 0.0)
    stg = float(cfg.get("stg") or 0.0)
    stg_block = int(cfg.get("stg_block") if cfg.get("stg_block") is not None else 25)
    stg_block = min(max(stg_block, 0), _H3_DOUBLE_BLOCK_COUNT - 1)
    passes = int(cfg.get("passes") or 1)
    decay = float(cfg.get("decay") or 0.5)
    retry_on = cfg.get("retry") is True
    retry_target = float(cfg.get("retry_target") or 0.15)
    # 二采独立采样器/调度器（抗糊 N8：空 = 沿用主链 res_multistep/simple）；
    # 名字不在 ComfyUI 注册表时回退主链值并报告（打错字不至于废一段）
    ks_sampler = str(cfg.get("sampler") or "").strip()
    ks_scheduler = str(cfg.get("scheduler") or "").strip()
    if ks_sampler or ks_scheduler:
        import comfy.samplers as _cs
        if ks_sampler and ks_sampler not in _cs.KSampler.SAMPLERS:
            if report is not None:
                report.append(f"⚠ 二采采样器「{ks_sampler}」不存在，沿用主链「{采样器}」")
            ks_sampler = ""
        if ks_scheduler and ks_scheduler not in _cs.KSampler.SCHEDULERS:
            if report is not None:
                report.append(f"⚠ 二采调度器「{ks_scheduler}」不存在，沿用主链「{调度器}」")
            ks_scheduler = ""
        # 换调度器 = 连「实际起始 σ」一起换：denoise 只定步数比例
        # （new_steps = int(steps/denoise)），尾部那几步的 σ 落点由调度器形状决定。
        # 例（H3 shift=12 表，σ₀=0.25 / 3 步）：simple 走 0.800→0.706→0.524→0，
        # beta 走 0.719→0.549→0.271→0 —— 同样的 denoise，beta 起点更低、收尾更细，
        # 也就是精化更温和。所以换调度器后必须重调 σ₀，否则强度悄悄变了。
        if ks_scheduler and report is not None:
            report.append("提示：二采换调度器会连「实际起始 σ」一起变（denoise 只定步数比例，"
                          "尾部几步的 σ 落点由调度器形状决定），换后请重调 σ₀")
    ks_sampler = ks_sampler or 采样器
    ks_scheduler = ks_scheduler or 调度器

    def _is_dev_err(e):
        return "to be on the same device" in str(e)

    # 精化期间的设备级显存峰值（GB）；由 _ksample 内的采样线程回填。
    # 用单元素列表是因为它在嵌套函数里写、在外层读（Python 闭包不支持 nonlocal
    # 跨两层时的简洁写法，且外层还要把它交给 _mark / report）。
    _refine_peak = [None]

    def _inventory():
        """设备错误时的张量设备清单——把 mat1 在哪钉到报告里，不再猜。"""
        def _first(obj):
            for _p, t in _iter_tensors(obj):
                return f"{_p}@{t.device}"
            return "无张量"
        try:
            _lat = ",".join(str(t.device) for t in latent["samples"].tensors)
        except Exception:
            _lat = "?"
        try:
            _unet = str(next(模型.model.diffusion_model.parameters()).device)
        except Exception:
            _unet = "?"
        return (f"latent[{_lat}] · cond {_first(cond)} · neg {_first(negative)}"
                f" · UNET权重 {_unet}")

    def _refine_once(sigma_start, cur_latent, cur_seed, round_desc=""):
        """单轮尾段精化：STG / time-bias / shift 各开关在此拼装（每轮 σ₀ 不同，
        time-bias 窗口与 STG 窗口跟着本轮 σ₀ 走）。OOM → UpscaleAbortError；
        设备不匹配（魔改分页运行时下克隆模型易触发）→ 去掉全部克隆/补丁用
        原模型重试一次，仍失败才上抛（带设备清单）。"""
        # 轮间清理（2026-08-25 实测：passes≥2 时首轮 σ₀ 成功、次轮 σ₀·decay
        # OOM——单轮峰值与 σ 无关，差的正是轮间账）：上一轮采样结束只丢函数
        # 局部引用，分配器缓存块与不可达对象全部滞留显存；每轮开跑前回收
        # 引用 + 归还空闲缓存块，次轮起点与首轮对齐。round_desc 进报错定位轮次
        gc.collect()
        torch.cuda.empty_cache()
        ks_steps, ks_denoise = tail_refine_args(sigma_start, cfg["steps"])

        def _attempt(plain):
            restore_tb = time_bias_guard(模型.model.diffusion_model, ks_denoise, tb) \
                if (tb > 0.0 and not plain) else None
            try:
                ks_model = 模型
                if not plain:
                    if sh > 0.0:
                        ks_model = _shifted_model(模型, sh)
                    if stg > 0.0:
                        # STG 挂在 shift 克隆之上（两次 clone 共享权重，近零成本）；
                        # 弱分支同样过 time_bias patch 的 dit.forward——两边同偏置，
                        # 差分语义保持
                        ks_model = _stg_model(ks_model, ks_denoise, stg, stg_block)

                def _ksample():
                    # 采设备级显存峰值：这是「精化到底需要多少显存」的唯一硬数，
                    # 也是判断「精化前那次全卸该不该做」的依据（详见 1629 行待办）。
                    # torch 的 max_memory_allocated 量不到权重池，必须走采样线程。
                    with perf.watch_vram_peak() as _w:
                        _out = comfy_nodes.common_ksampler(
                            ks_model, cur_seed, ks_steps, cfg["cfg"], ks_sampler,
                            ks_scheduler, cond, negative, cur_latent,
                            denoise=ks_denoise)[0]
                    if _w["peak"] is not None:
                        _refine_peak[0] = _w["peak"] if _refine_peak[0] is None \
                            else max(_refine_peak[0], _w["peak"])
                    return _out

                try:
                    return _ksample()
                except RuntimeError as e:
                    if not _is_oom(e):
                        raise
                    # 一级自救（fee12e1 机制回归）：全卸驻留模型 + 回收 Python 残留引用 +
                    # 清缓存后【原参】重试一次——参数零改动、产物零降级；仍 OOM 才按
                    # 既定语义硬停整链。gc.collect 先于 empty_cache：引用不回收，
                    # CUDA 块归不了还
                    comfy.model_management.unload_all_models()
                    gc.collect()
                    torch.cuda.empty_cache()
                    try:
                        return _ksample()
                    except RuntimeError as e2:
                        if not _is_oom(e2):
                            raise
                        e = e2
                    msg = ("二采显存不足（放大后 {0:g}× 高清 latent 上的重采样超出当前可回收显存 "
                           "{1:g}GB，已自动卸载驻留模型并回收残留后原参重试仍失败）："
                           "峰值显存由画布×帧数决定——请降低放大倍率或缩短该段帧数"
                           "（精化步数/起始σ只影响耗时、不影响峰值），或增大显存。"
                           "当前精化 {2} 步 @ σ≈{3:g}{4}。本段尚未落盘二采产物。".format(
                               cfg["scale"], _vram_gb(模型), ks_steps, ks_denoise,
                               round_desc))
                    if report is not None:
                        report.append(msg)
                    raise UpscaleAbortError(msg) from e
            finally:
                if restore_tb is not None:
                    restore_tb()

        try:
            return _attempt(plain=False)
        except RuntimeError as e:
            # 设备不匹配兜底：克隆模型（shift/STG）与魔改 DynamicVRAM 分页互锁时，
            # 权重被二次 stage、部分层激活滞留 CPU（mat1 on cpu）。shift/时间偏置
            # 只是画质微调，砍掉换「这一段能出高清」——重试仍失败才真正上抛
            if _is_dev_err(e) and (sh > 0.0 or stg > 0.0 or tb > 0.0):
                if report is not None:
                    report.append(f"⚠ 段{seg_no} 精化张量设备不匹配（疑似运行时分页与"
                                  "克隆模型互锁）——已去除 shift/STG/时间偏置，用原模型重试")
                print(f"[H3二采] 段{seg_no}：精化设备不匹配，去除克隆/补丁用原模型重试…",
                      flush=True)
                comfy.model_management.unload_all_models()
                gc.collect()
                torch.cuda.empty_cache()
                try:
                    return _attempt(plain=True)
                except RuntimeError as e2:
                    if _is_dev_err(e2):
                        raise RuntimeError(
                            f"二采精化张量设备不匹配（原模型重试仍失败）：{e2}｜"
                            f"设备清单：{_inventory()}") from e2
                    raise
            raise
        except (ValueError, AttributeError, TypeError, NotImplementedError) as e:
            # 二采独立模型（不同量化 / 上游挂过第三方补丁节点）时，模型构造类补丁
            # 会抛构造类异常：_stg_model 检测到已有 sampler_post_cfg_function 或
            # double_block replacement 直接抛错（拒绝叠加），_shifted_model 读
            # model_config 也可能失败。这些都不是致命错误——shift/STG/时间偏置只是
            # 画质微调，砍掉换「这一段能出高清」。
            # UpscaleAbortError 是 RuntimeError 子类、不在此列，OOM 与预检硬停
            # 仍按既定语义上抛，不会被这里吞掉。
            if not (sh > 0.0 or stg > 0.0 or tb > 0.0):
                raise
            if report is not None:
                report.append(f"⚠ 段{seg_no} 精化补丁与本二采模型不兼容"
                              f"（{type(e).__name__}: {e}）——"
                              "已去除 shift/STG/时间偏置，用原模型重试")
            print(f"[H3二采] 段{seg_no}：{type(e).__name__}（精化补丁与本模型不兼容），"
                  "去除克隆/补丁用原模型重试…", flush=True)
            gc.collect()
            torch.cuda.empty_cache()
            return _attempt(plain=True)

    def _deliver(sampled_dict):
        """精化输出 → 交付 latent：频域细节混合（mix）→ latent 锐化（sharpen）
        → 细节增益度量。顺序固定：mix 动低频（结构锚回放大 latent）、sharpen
        动高频（细节再放大一档），两者正交可叠加。"""
        v = sampled_dict["samples"].unbind()[0]
        if up_v_base is not None:
            v = freq_mix_latents(up_v_base, v, mix).to(v.device, torch.float32)
        _shp = float(cfg.get("sharpen") or 0.0)
        if _shp > 0.0:
            v = sharpen_latents(v, _shp).to(v.device, torch.float32)
        return v, hf_gain_ratio(hf_up, latent_hf_energy(v))

    # ── 精化时序分块（2026-09-23 批次 3）───────────────────────────────────
    #
    # 动机：精化画布的激活是整条二采链里最大的一笔（与分辨率**平方**相关）。
    # 把高清 latent 沿时间切成段、**每段独立跑 N 步去噪**，峰值只与本段 token 数
    # 相关 → 显存需求按段数线性下降。
    #
    # ⚠ 与「放大网络 3D 时序分块」是**两码事**（务必别混）：
    #   · 放大网络那个是纯前馈（一次 forward，切开算再融合即可，数学上可分离）
    #   · 这个是**扩散采样循环** —— 每段独立收敛，段与段之间没有注意力交互，
    #     两侧会落到不同的局部解 → 接缝有差异。所以必须：
    #       ① 段间留 overlap（单位 = latent token，建议 ≥8）
    #       ② 融合只在 core 区落盘、overlap 区按羽化权重过渡
    #     且 overlap 太小会**逐帧闪烁**（不是一条静止的缝 —— 每段噪声轨迹不同）。
    #
    # 音频不参与分块：音轨沿用原声、二采不重采样（与放大网络分块同一口径），
    # 所以段切只切视频 latent，音频整条带着走。
    _rc_on = bool(cfg.get("refine_temporal_on", False))
    _rc_frames = int(cfg.get("refine_temporal_chunk") or 0)
    _rc_ov = int(cfg.get("refine_temporal_overlap") or 0)
    # 单位换算：设置里「每段帧数」→ latent token。两者非线性（grid 的官方映射），
    # 用 frames_to_latent_t 取最小可覆盖，保证段边界落在真实 token 网格上。
    _rc_tokens = 0
    if _rc_on and _rc_frames > 0:
        _rc_tokens = max(2, grid.frames_to_latent_t(_rc_frames))
    # 生效 overlap 必须与 `plan_refine_chunks` **同一个算法**：那边按 `c//2` 夹，
    # 这里羽化 ramp 必须取同一个值，两侧权重才严格互补（取请求值会不互补）。
    _rc_eff_ov = max(0, min(_rc_ov, _rc_tokens // 2)) if _rc_tokens > 0 else 0

    # ── 精化空间 tile（2026-09-23 批次 4）──────────────────────────────────
    #
    # 动机：时序分块把峰值压到「本段 token 数」，但**段内的 H×W 还是整幅**——
    # 画幅大时，段内那一笔激活仍是显存主项。tile 把 H/W 再切网格，峰值与本块
    # 画幅成正比 → 与时序分块**正交**，两层可以叠加（先切时间、每段内再切空间）。
    #
    # ⚠ 这是三类分块里**画质风险最高**的一种，务必清醒：
    #   · 全局注意力被切断 —— 每块只看得见自己那一小块，构图/光照/运动全局关系
    #     断链（时序分块至少每段还是完整画幅，全局关系还在）
    #   · 视频尤其严重：块与块各自收敛 → 块边**帧间抖动**（不是一条静止的缝）
    #   所以：默认不推荐、档位以 2×2 为上限（每块仍有 1/4 画幅）、必须留 overlap
    #   + 二维羽化融合。**画质优先的场景请关掉它**。
    #
    # 融合不变量与 `_refine_chunked` 同口径：core 区不重不漏拼满整幅；overlap
    # 区按二维羽化权重过渡。二维权重 = 行向权重 × 列向权重（可分离，省一次 2D 场
    # 构造，且天然是「角上最轻、中心最重」的合理形状）。
    _rt_on = bool(cfg.get("refine_tile_on", False))
    _rt_mode = str(cfg.get("refine_tile") or "off")
    _rt_ov = int(cfg.get("refine_tile_overlap") or 0)
    _rt_feather = int(cfg.get("refine_tile_feather") or 0)

    def _refine_whole(sigma_start, cur_latent, cur_seed, round_desc=""):
        """不分时序：直接交给空间层（tile 关 = 原 `_refine_once`，开 = 空间切块）。

        tile 是**比时序更内层**的分块：时序层切完时间后，段内要做的仍是「一次
        精化」，那正是空间层。所以「不分时序」不等于「不分块」——tile 单独开启
        时走这条路。
        """
        return _refine_tiled(sigma_start, cur_latent, cur_seed, round_desc)

    def _refine_tiled(sigma_start, cur_latent, cur_seed, round_desc=""):
        """空间 tile 精化：把每帧 H/W 切成网格 → 逐块采样 → core 写回 + 二维羽化融合。

        返回的 latent 与不分块路径**同构**（NestedTensor 的 (video, audio)），
        故可被时序层 `_refine_chunked` 当作「段内的 refiner」调用 —— 时间先切、
        段内再切空间，两层**自然叠加**。

        ⚠ 维度约定：视频 latent 为 `[B, 24, T, H, W]`，切的是 H(轴3)/W(轴4)。
          音频 latent 不参与分块（沿用原声，与其它分块同一口径），整条带着走。
        ⚠ 档位 off / 画幅太小切不动时，本函数**恒等透传**（等价不分块）。
        """
        _src = cur_latent["samples"]
        _vid, _aud = (_src.tensors[0], _src.tensors[1]) if hasattr(_src, "tensors") \
            else (_src[0], _src[1])
        H = int(_vid.shape[3])
        W = int(_vid.shape[4])
        plan = perf.plan_tiles(H, W, _rt_mode, _rt_ov)
        if len(plan) <= 1:
            # 档位 off / 画幅太小切不动 → 恒等透传（与不分块逐位相同）
            return _refine_once(sigma_start, cur_latent, cur_seed, round_desc)
        print(f"[H3二采] 段{seg_no}：精化空间 tile {W}×{H} → {len(plan)} 块"
              f"（档位 {_rt_mode} · overlap {_rt_ov}px · 羽化 {_rt_feather}px）"
              f"{round_desc}", flush=True)
        acc = torch.zeros_like(_vid)
        wsum = torch.zeros((1, 1, 1, H, W), device=_vid.device, dtype=torch.float32)
        for ti, (hs, he, ws, we, chs, che, cws, cwe, eff_ov) in enumerate(plan):
            piece = _vid[:, :, :, hs:he, ws:we]
            sub = comfy.nested_tensor.NestedTensor((piece.contiguous(), _aud))
            out = _refine_once(sigma_start, {"samples": sub},
                               (cur_seed + ti * 104729) % 0xffffffffffffffff,
                               f"{round_desc}·空间块{ti + 1}/{len(plan)}")
            o = out["samples"]
            sh = (o.tensors[0] if hasattr(o, "tensors") else o[0]).to(acc.dtype)
            # 二维羽化权重：行向 × 列向（可分离）——省一次 2D 场构造。
            # ⚠ 权重算在**写回区（core）**坐标系上（`chs..che` / `cws..cwe`）：
            # 融合只发生在写回区里。ramp 取 `min(_rt_feather, eff_ov)` —— 上限是
            # **实际生效**的 overlap（eff_ov，可能被 min_side 压小），取请求值会
            # 与真实咬合带宽度不符 → 两侧权重不互补（踩过：3x3 用 16 而实际 10）。
            # ⚠ 外缘（core 顶到画布边）恒 1：那里没有邻居补权重，羽化会让外缘
            # 被归一化放大、噪声显式放大。
            rh = min(_rt_feather, eff_ov)
            rw = min(_rt_feather, eff_ov)
            wh = perf.edge_weights(che - chs, rh, chs > 0, che < H)
            ww = perf.edge_weights(cwe - cws, rw, cws > 0, cwe < W)
            wv = (torch.tensor(wh, device=acc.device, dtype=torch.float32)
                  .view(1, 1, 1, -1, 1)
                  * torch.tensor(ww, device=acc.device, dtype=torch.float32)
                  .view(1, 1, 1, 1, -1))
            ah, bh = chs - hs, che - hs      # 本块 core 区在块内坐标
            aw, bw = cws - ws, cwe - ws
            acc[:, :, :, chs:che, cws:cwe] = (
                acc[:, :, :, chs:che, cws:cwe]
                + sh[:, :, :, ah:bh, aw:bw] * wv)
            wsum[:, :, :, chs:che, cws:cwe] = (
                wsum[:, :, :, chs:che, cws:cwe] + wv)
        _bad = (wsum <= 0).any().item()
        if _bad:
            # 各块 core 的**并集**必须铺满整幅 H×W、且咬合带内权重和 >0
            # （plan_tiles 的不变量）。真出现空洞 = 有像素没人写 → 黑洞。
            raise UpscaleAbortError(
                f"精化空间 tile 的 core 并集未覆盖全部 {W}×{H}（plan 有空洞）——"
                f"本段未落盘二采产物。这是实现 bug，请把本行反馈给开发者。")
        merged = (acc / wsum).to(_vid.dtype)
        return {"samples": comfy.nested_tensor.NestedTensor((merged, _aud))}

    def _refine_chunked(sigma_start, cur_latent, cur_seed, round_desc=""):
        """分块精化：切时间轴 → 逐段采样 → 按 core 区写回 + overlap 羽化融合。

        返回的 latent 与不分块路径**同构**（NestedTensor 的 (video, audio)），
        故下游 `_deliver` / cascade / retry 全部无需改动。
        """
        _src = cur_latent["samples"]
        _vid, _aud = (_src.tensors[0], _src.tensors[1]) if hasattr(_src, "tensors") \
            else (_src[0], _src[1])
        T = int(_vid.shape[2])
        plan = perf.plan_refine_chunks(T, _rc_tokens, _rc_ov)
        if len(plan) <= 1:
            return _refine_tiled(sigma_start, cur_latent, cur_seed, round_desc)
        print(f"[H3二采] 段{seg_no}：精化时序分块 {T} token → {len(plan)} 段"
              f"（每段 {_rc_tokens} token · overlap {min(_rc_ov, _rc_tokens // 2)}）"
              f"{round_desc}", flush=True)
        # 累加器：core 区写回，overlap 区按权重混合（两次都没覆盖到的地方 = 不该有）
        acc = torch.zeros_like(_vid)
        wsum = torch.zeros((1, 1, T, 1, 1), device=_vid.device, dtype=torch.float32)
        for ci, (s, e, cs, ce) in enumerate(plan):
            piece = _vid[:, :, s:e]
            sub = comfy.nested_tensor.NestedTensor(
                (piece.contiguous(), _aud))
            # 每段独立采样（种子逐段偏移，避免所有段拿到同一条噪声轨迹）；
            # 段内交给空间层（tile 关 = 直通 _refine_once，开 = 段内再切块）
            out = _refine_tiled(sigma_start, {"samples": sub},
                               (cur_seed + ci * 7919) % 0xffffffffffffffff,
                               f"{round_desc}·分段{ci + 1}/{len(plan)}")
            o = out["samples"]
            sh = (o.tensors[0] if hasattr(o, "tensors") else o[0]).to(acc.dtype)
            # core == span（段只写自己采样过的），映射回本段内部坐标 = 全段。
            # 权重算在**写回区**坐标系上；ramp 必须恰等于咬合带宽度（相邻段
            # 喂入区自身的重叠 = 生效 overlap），两侧才严格互补成 1。
            # 链两端（首/末段）没有邻居补权重 → 外缘恒 1（否则被归一化放大）。
            core_len = ce - cs
            w = perf.edge_weights(core_len, _rc_eff_ov, cs > 0, ce < T)
            wv = torch.tensor(w, device=acc.device, dtype=torch.float32) \
                .view(1, 1, core_len, 1, 1)
            acc[:, :, cs:ce] = acc[:, :, cs:ce] + sh * wv
            wsum[:, :, cs:ce] = wsum[:, :, cs:ce] + wv
        _bad = (wsum <= 0).any().item()
        if _bad:
            # 各段 core 的**并集**必须铺满 [0,T)、且咬合带内权重和 >0
            # （plan_refine_chunks 的不变量）。真出现空洞 = 有 token 没人写。
            raise UpscaleAbortError(
                f"精化时序分块的 core 并集未覆盖全部 {T} 个 token（plan 有空洞）——"
                f"本段未落盘二采产物。这是实现 bug，请把本行反馈给开发者。")
        merged = (acc / wsum).to(_vid.dtype)
        return {"samples": comfy.nested_tensor.NestedTensor((merged, _aud))}

    # 外层 = 时序分块（切时间）。不开则 `_refine_whole` → 空间层（tile 独立生效）。
    _refine = _refine_chunked if _rc_tokens > 0 else _refine_whole
    if _rc_tokens > 0:
        print(f"[H3二采] 段{seg_no}：精化时序分块已开启"
              f"（每段 {_rc_frames} 帧 ≈ {_rc_tokens} token · "
              f"段间 overlap {_rc_ov} token）", flush=True)
    if _rt_on:
        print(f"[H3二采] 段{seg_no}：精化空间 tile 已开启"
              f"（档位 {_rt_mode} · overlap {_rt_ov}px · 羽化 {_rt_feather}px）"
              f"——⚠ 全局注意力被切断，块边可能出现帧间抖动，画质优先请关掉",
              flush=True)

    # 多轮递降精化（cascade，抗糊 N2）：passes=1 即现状单轮；σ 序列由
    # cascade_sigmas 决定论派生（同参数同序列，重放一致）；种子逐轮 +1。
    # restore_rows 的 finally 覆盖整条精化链（任一轮异常都恢复守卫再上抛）
    sigmas = cascade_sigmas(sigma0, passes, decay)
    up_out_v, hf_gain, retried = None, 0.0, False
    _t = time.perf_counter()
    try:
        cur = latent
        for k, sk in enumerate(sigmas):
            cur = _refine(sk, cur, (seed + k) % 0xffffffffffffffff,
                          f"（第{k + 1}轮/共{len(sigmas)}轮）")

        up_out_v, hf_gain = _deliver(cur)
        cur = None
        if retry_on and should_retry(hf_gain, retry_target, sigma0):
            # 增益自适应重试（抗糊 N7）：同一起始放大 latent、σ₀+0.1 重跑整条
            # 精化链一次，按细节增益取优——「一轮抠不动就再深一轮」的自动档
            sigma_r = min(sigma0 + 0.1, 0.90)
            sigmas_r = cascade_sigmas(sigma_r, passes, decay)
            cur2 = latent
            for k, sk in enumerate(sigmas_r):
                cur2 = _refine(sk, cur2,
                               (seed + len(sigmas) + k) % 0xffffffffffffffff,
                               f"（增益重试链第{k + 1}轮/共{len(sigmas_r)}轮）")
            v2, g2 = _deliver(cur2)
            cur2 = None
            if g2 > hf_gain:
                up_out_v, hf_gain = v2, g2
                retried = True
            else:
                v2 = None
            if report is not None:
                report.append(f"… 段二采增益 {hf_gain:+.0%} 未达目标 {retry_target:+.0%}"
                              f"——重试 σ≈{sigma_r:g} 取优"
                              f"（{'采纳' if retried else '保留原轮'}）")
    finally:
        restore_rows()
        restore_video_rows()
    _tmark("refine", _t)
    _mark("refine", "精化后")   # 埋点⑤精化重采样后（与放大后对比即可量化 autograd 图的影响）
    # 精化期间的**设备级峰值**——「不腾挪 / 少腾挪会不会 OOM」的判定依据。
    # 与「腾挪回收量」配着看即可定案：峰值 ≤ 卸载前可用 ⇒ 那次腾挪是白卸。
    if _refine_peak[0] is not None:
        print(f"[H3二采] 段{seg_no}：精化显存峰值（设备级）{_refine_peak[0]:.2f}GB",
              flush=True)
        # 「有没有把显存用满」的实测答案：总量 − 峰值 = 真正没被用上的那块。
        # 只在明显偏松/偏紧时给一句，别每段都唠叨。
        try:
            _tot = torch.cuda.mem_get_info()[1] / (1024 ** 3)
        except Exception:
            _tot = None
        _slack = perf.vram_slack_hint(_refine_peak[0], _tot) if _tot else None
        if _slack:
            print(f"[H3二采] 段{seg_no}：{_slack}", flush=True)
        perf.emit({"kind": "refine_peak", "seg": seg_no,
                   "vram_used": _refine_peak[0], "vram_total": _tot,
                   "hint": _slack or ""})
    print(f"[H3二采] 段{seg_no}：精化重采样完成（{(_timing or {}).get('refine', 0.0):.0f}s）"
          "→ 高清解码…", flush=True)
    del cond, latent
    torch.cuda.empty_cache()
    del up_v_base
    return up_out_v, tw, th, seed, bridged, hf_gain, retried


def _attn_head_selfcheck(attn, n_chunks):
    """头分块前后是否**逐元素一致**（装之前必须自测，不通过就别装）。

    头之间独立是**架构事实**（H3 的 `Attention.forward` 里每个 head 共享 qkv_proj
    的切片、各自做 norm+rope+attention，最后 concat 回 out_proj），但这份 forward
    可能被第三方 patch 过——只要掺了任何**跨 head** 的操作（例如把 head 维揉进
    序列做的稀疏注意力重排），按 head 切组就不再等价。与其赌，不如拿一份小输入
    跑「整段 vs 分组」对比。用 CPU 小张量（S=8）跑，代价可忽略。
    """
    try:
        heads = int(getattr(attn, "heads", 0) or 0)
        head_dim = int(getattr(attn, "head_dim", 0) or 0)
        if heads <= 1 or head_dim <= 0 or n_chunks <= 1 or n_chunks > heads:
            return False
        dev = None
        for p in attn.parameters():
            dev = p.device
            break
        if dev is None:
            return False
        s = 8
        x = torch.randn((s, heads * head_dim), device=dev, dtype=torch.float32)
        with torch.no_grad():
            full = _attn_forward_reference(attn, x, None)
            chunked = _attn_forward_chunked(attn, x, None, n_chunks)
        if full is None or chunked is None:
            return False
        if full.shape != chunked.shape:
            return False
        return bool(torch.allclose(full.detach().float(), chunked.detach().float(),
                                   atol=1e-3, rtol=1e-3))
    except Exception:
        return False


def _attn_forward_reference(attn, x, rope_freqs, transformer_options=None):
    """上游原样路径（不分头）：直接调 attn 自己，等价于未 patch 的行为。"""
    return attn(x, rope_freqs=rope_freqs, transformer_options=transformer_options or {})


def _attn_forward_chunked(attn, x, rope_freqs, n_chunks, transformer_options=None):
    """按 head 分组逐组算 attention —— **head 之间独立，结果精确无损**。

    为什么能省显存：kernel 内部那些随 head 数增长的临时量（int8 q/k 副本、
    fp32 累加器）按组数缩小；同时这里比上游多释放两处内存：
      1. qkv GEMM 之后**立刻**丢掉输入 x（normed hidden，S×hidden 的大头）
      2. out_proj 分配输出**之前**丢掉融合的 (S, 3*inner) qkv buffer
    这两处才是它真正省显存的地方，分组本身是次要的。

    移植自 KJNodes `MiniMaxLowVRAMAttention`（nodes/minimax_nodes.py:89-138）。
    ⚠ 与 KJ 的差别：KJ 走 `add_object_patch`（Model 类节点的官方机制），本项目
    是**在采样节点内部直接改 nn.Module 的方法**（与 install_ff_chunking 同风格），
    因为这里的模型是裸 nn.Module、没有 ModelPatcher 可用。
    """
    from comfy.ldm.modules.attention import optimized_attention
    if isinstance(x, list):
        x = x.pop()
    s = x.shape[0]
    heads = attn.heads
    head_dim = attn.head_dim
    n = min(int(n_chunks), heads)
    q, k, v = attn.qkv_proj(x).split(heads * head_dim, dim=-1)
    del x                                   # ← 释放①：normed hidden 用完即丢
    v = v.view(s, heads, head_dim)
    if rope_freqs is not None:
        import comfy.model_management as _mm
        import comfy.quant_ops as _qo
        q = q.view(1, s, heads, head_dim)
        k = k.view(1, s, heads, head_dim)
        qw = _mm.cast_to(attn.q_norm.weight, device=v.device)
        kw = _mm.cast_to(attn.k_norm.weight, device=v.device)
        rot = rope_freqs.shape[-3] * 2
        if _mm.in_training:
            q, k = _qo.ck.rms_rope_split_half(q, k, rope_freqs, qw, kw,
                                              epsilon=attn.q_norm.eps, rot_dim=rot)
        else:
            q, k = _qo.ck.rms_rope_split_half_(q, k, rope_freqs, qw, kw,
                                               epsilon=attn.q_norm.eps, rot_dim=rot)
        q = q[0]
        k = k[0]
    else:
        q = attn.q_norm(q.view(s, heads, head_dim))
        k = attn.k_norm(k.view(s, heads, head_dim))
    q = q.transpose(0, 1).unsqueeze(0)
    k = k.transpose(0, 1).unsqueeze(0)
    v = v.transpose(0, 1).unsqueeze(0)
    # 分组大小：前 heads%n 组各多 1 个 head（尽量均分，别全挤在最后一组）
    sizes = [heads // n + (1 if i < heads % n else 0) for i in range(n)]
    out = torch.empty((s, heads * head_dim), dtype=q.dtype, device=q.device)
    hs = 0
    for size in sizes:
        he = hs + size
        o = optimized_attention(q[:, hs:he], k[:, hs:he], v[:, hs:he], size,
                                mask=None, skip_reshape=True,
                                transformer_options=transformer_options or {})
        out[:, hs * head_dim:he * head_dim] = o.squeeze(0).to(out.dtype)
        hs = he
    del q, k, v
    return attn.out_proj(out)               # ← 释放②：out_proj 分配前 qkv 已丢


def install_low_vram_attention(model, head_chunks, targets=None):
    """给 MiniMax H3 的每个 `blocks[*].attn` 装 head 分块 + 提前释放。返回 (安装数, 说明)。

    与 `install_ff_chunking` 同风格：**先自测、不通过就不装**。头分块依赖上游
    `Attention.forward` 的内部结构（`qkv_proj` / `q_norm` / `k_norm` / `out_proj`
    / `heads` / `head_dim`），跨 ComfyUI 版本可能变——所以这里先做**结构探测**，
    字段不全就整段不装并报出缺哪一项。

    ⚠ 与 KJ 的取舍差异：KJ 还顺手 patch 了 `DiTBlock.forward`（把 h 包进 list 让
    attn 能提前释放）。本项目**不 patch block** —— 那会与上游 DiTBlock 的签名耦合
    （`attention=None` 参数是较新版本才有的），而提前释放的收益已经在 attn 内部
    拿到（release ①）。少改一处 = 少一处跨版本风险。
    """
    try:
        n = int(head_chunks or 1)
    except (TypeError, ValueError):
        return 0, "attn_head_chunks 不是整数"
    if n <= 1:
        return 0, ""
    diff = None
    for path in ("model.diffusion_model", "diffusion_model"):
        obj = model
        try:
            for p in path.split("."):
                obj = getattr(obj, p)
            diff = obj
            break
        except Exception:
            continue
    if diff is None:
        return 0, "取不到 diffusion_model"
    blocks = getattr(diff, "blocks", None)
    if not blocks:
        return 0, "模型里没有 diffusion_model.blocks（不是 H3 结构？）"
    first = blocks[0]
    attn0 = getattr(first, "attn", None)
    # 结构探测：字段不全就整段不装，别让它跑到一半炸
    need = ("qkv_proj", "q_norm", "k_norm", "out_proj")
    if attn0 is None:
        return 0, "blocks[*] 里没有 attn"
    missing = [a for a in need if not hasattr(attn0, a)]
    if missing:
        return 0, f"attn 结构不匹配（缺 {', '.join(missing)}），本版 ComfyUI 不支持"
    heads = int(getattr(attn0, "heads", 0) or 0)
    if heads <= 1:
        return 0, "attn.heads <= 1，无从分块"
    eff = min(n, heads)
    # 幂等：**已经装过同一分组数就直接返回**，别再自测、别重复包一层。
    # ⚠ 这一步必须在自测**之前**：装过之后 attn.forward 已经是本模块的分组函数，
    # `_attn_forward_reference` 再调 attn(…) 会进那个普通函数（self 位置收错），
    # 自测必然失败 → 第二次调用会误报「含跨 head 操作」并把已装的也判为不该装。
    if getattr(attn0, "_h3_attn_head_chunks", 0) == eff:
        n_done = sum(1 for b in blocks
                     if getattr(getattr(b, "attn", None),
                                "_h3_attn_head_chunks", 0) == eff)
        return n_done, ""
    # 只对第一个 block 自测（同一份代码、同一结构，逐个测纯浪费）
    if not _attn_head_selfcheck(attn0, eff):
        return 0, ("注意力头分块自测不通过（attn.forward 里含跨 head 操作，"
                   "不是纯分组可分离）——本次不装，输出保持原样")
    installed = 0
    for blk in blocks:
        attn = getattr(blk, "attn", None)
        if attn is None:
            continue

        # ⚠ 赋**普通函数**（不是 MethodType），签名里**不能有 self**：
        # `attn.forward = _fwd` 是给**实例**挂属性，`nn.Module.__call__` 取到
        # `self.forward` 后直接 `forward_call(*args)` —— 只有业务参数，没有 self。
        # 多写一个 self 形参 = 必崩 `missing 1 required positional argument`
        # （与上面 install_ff_chunking 的 _fwd 同一个道理/同一个坑）。
        # 因此用默认参数把 attn 绑死（__sa=attn）—— 既补回 self，又避免闭包
        # 晚绑定（循环里所有 _fwd 都指向最后一个 attn）的经典坑。
        def _fwd(x, rope_freqs=None, transformer_options={},
                 __n=eff, __sa=attn, **k):
            return _attn_forward_chunked(__sa, x, rope_freqs, __n,
                                         transformer_options=transformer_options)

        try:
            attn.forward = _fwd
            attn._h3_attn_head_chunks = eff
            installed += 1
        except Exception:
            continue
    if installed == 0:
        return 0, "一个 attn 也没装上"
    return installed, ""


def _ff_chunk_selfcheck(sub, orig, chunk_tokens):
    """分块前后是否**逐位一致**（装之前必须自测，不通过就别装）。

    `nn.Linear` 沿行（token）可分离是**数学事实**，但 `fc1` / `fc2` 上可能挂着
    bypass adapter（HyperFlow 的 `F.linear(F.linear(x, down), up)`）、
    LayerNorm、甚至第三方补丁——只要里面掺了任何**跨 token** 的操作，切块
    就不再等价。与其赌，不如拿一份比 chunk 更大的随机输入跑一遍对比。
    """
    try:
        w = getattr(sub, "weight", None)
        if w is None or getattr(w, "dim", lambda: 0)() < 2:
            return False
        inf = int(w.shape[1])
        rows = max(int(chunk_tokens) + 1, 4)
        x = torch.randn((rows, inf), device=w.device, dtype=w.dtype)
        a = orig(x)
        if not torch.is_tensor(a):
            return False
        parts = [orig(x[s:e]) for s, e in perf.plan_ff_chunks(rows, chunk_tokens)]
        b = parts[0] if len(parts) == 1 else torch.cat(parts, dim=0)
        if a.shape != b.shape:
            return False
        return bool(torch.allclose(a.detach().float(), b.detach().float(),
                                   atol=1e-4, rtol=1e-3))
    except Exception:
        return False


def install_block_prefetch(model, on):
    """把**官方**的块级预取钉开 / 钉关 —— 返回 (是否改动, 说明)。

    官方 H3 的 block 循环里已经有预取队列（`comfy/ldm/minimax/model.py:751-765` 的
    `make_prefetch_queue` / `prefetch_queue_pop`），总闸是 `comfy/model_base.py:250`
    **每次前向**写下的 `transformer_options["prefetch_dynamic_vbars"]
    = patcher.is_dynamic()`。预取深度由队列结构写死为「提前 1 块」
    （`[None] + blocks + [None]`，每轮只 pin `queue[0]`）→ 本项**只能是开关**，
    不能是「预取 N 块」。

    `on=True`（默认）**不干预**：官方在 DynamicVRAM 下本来就是开的，装一层包装去
    「确认它开着」只是白搭每前向一次字典写。只有 `on=False` 才装包装，把那个 flag
    钉成 False → 每次前向退回「用到哪块换哪块」。
    ⚠ 关预取 ≠ 关块级流动：op 时的基线换入（`comfy/ops.py:167` 的 `vbar_fault`）照旧，
    去掉的只是 lookahead（少占一块显存、慢一点）。

    ⚠ 包的是 `_forward`（**内层**，block 循环在那里），不是 `forward`：外层 `forward`
    要跑音频 carry 与 `WrapperExecutor`，别的节点（Hyperflow / Spectrum）挂在
    `_forward` 上，我们只往前包一层、不碰别人的 wrapper。默认参数把原函数绑死
    （实例属性赋普通函数**不会**自动获得 self，这是本项目踩过的坑）。
    """
    diff = None
    for path in ("model.diffusion_model", "diffusion_model"):
        obj = model
        for p in path.split("."):
            obj = getattr(obj, p, None)
            if obj is None:
                break
        if obj is not None:
            diff = obj
            break
    if diff is None or not hasattr(diff, "_forward"):
        return 0, "取不到 diffusion_model._forward"
    installed = getattr(diff, "_h3_block_prefetch", None)
    if on:
        if installed is None:
            return 0, ""
        diff._forward = installed[0]           # 拆掉包装 = 交回官方默认
        try:
            del diff._h3_block_prefetch
        except AttributeError:
            pass
        return 1, "已交回官方默认（不干预 → 预取照旧开着）"
    if installed is not None:
        return 0, ""                           # 幂等：已经关着了
    orig = diff._forward
    try:
        import inspect
        idx = list(inspect.signature(orig).parameters).index("transformer_options")
    except (TypeError, ValueError):
        return 0, "上游 _forward 签名里找不到 transformer_options（版本变了）→ 未干预"

    def _fwd(*a, __idx=idx, __orig=orig, **k):
        if len(a) > __idx and isinstance(a[__idx], dict):
            a[__idx]["prefetch_dynamic_vbars"] = False
        else:
            _to = k.get("transformer_options")
            if isinstance(_to, dict):
                _to["prefetch_dynamic_vbars"] = False
        return __orig(*a, **k)

    diff._forward = _fwd
    diff._h3_block_prefetch = (orig, idx)
    return 1, "官方块级预取已关闭（每块用到才换，少占一块显存、更慢）"


def install_ff_chunking(model, chunk_tokens, targets=("fc1", "fc2"), min_tokens=0):
    """给 FFN 的 `fc1`/`fc2` 装 token 分块。返回 (安装数, 跳过原因)。

    为什么它能救 OOM：主干 OOM 实测崩在 FFN 里 —— `F.linear(F.linear(x, down), up)`
    **单次请求 6.13GB**（D2，占 24GB 卡的 26%）。`nn.Linear` 沿 token（行）
    完全可分离：切块逐块算再拼回，结果与不分块**逐位相同**，是本项目少有的
    「零画质损失」优化（这也是它排在分块清单第一位的原因）。

    ⚠ 三道保险，缺一不可：
      1. **装前自测**（`_ff_chunk_selfcheck`）：不一致就整段不装，宁可这次
         不生效，也不能悄悄改输出；
      2. **默认关**（`ff_chunk_tokens=0`）：只有用户显式开才装；
      3. **短序列直通**（`min_tokens`）：序列 token 数低于此值就整段走原路径 ——
         切块只增加 kernel 启动开销、省不了多少显存。对齐 KJNodes 的 seq_threshold 语义。

    ⚠ `min_tokens` 是**每次前向**按实际 token 数判的（不是装的时候），所以它
    存进被包装的 forward 里，不参与「装/不装」的决策。
    """
    try:
        ct = int(chunk_tokens or 0)
    except (TypeError, ValueError):
        return 0, "ff_chunk_tokens 不是整数"
    if ct <= 0:
        return 0, ""
    try:
        mt = int(min_tokens or 0)
    except (TypeError, ValueError):
        mt = 0
    diff = None
    for path in ("model.diffusion_model", "diffusion_model"):
        obj = model
        try:
            for p in path.split("."):
                obj = getattr(obj, p)
            diff = obj
            break
        except Exception:
            continue
    if diff is None:
        return 0, "取不到 diffusion_model"
    n = 0
    skipped = 0
    for m in diff.modules():
        for name in targets:
            sub = getattr(m, name, None)
            if sub is None or not hasattr(sub, "forward"):
                continue
            if getattr(sub, "_h3_ff_chunk", 0) == ct:
                n += 1
                continue
            orig = sub.forward
            if not _ff_chunk_selfcheck(sub, orig, ct):
                # 自测不过就跳过**这一个**，不提前 return：别的模块可能是干净的。
                # 半装本身不破坏正确性（每个装上的都自测过、彼此等价），
                # 但报告必须说清「装了几个 / 跳了几个」，别让用户以为全没生效。
                skipped += 1
                continue
            plan = perf.plan_ff_chunks  # 纯函数，零 torch 依赖

            def _fwd(x, *a, __orig=orig, __ct=ct, __plan=plan, __mt=mt, **k):
                try:
                    # 短序列直通：token 数低于 min_tokens 时切块只增开销，不值得切
                    # （对齐 KJNodes 的 seq_threshold 语义）。__mt<=0 = 不设阈值。
                    if not torch.is_tensor(x) or x.dim() < 2 or int(x.shape[0]) <= __ct:
                        return __orig(x, *a, **k)
                    if __mt > 0 and int(x.shape[0]) < __mt:
                        return __orig(x, *a, **k)
                except Exception:
                    return __orig(x, *a, **k)
                outs = [__orig(x[s:e], *a, **k)
                        for s, e in __plan(int(x.shape[0]), __ct)]
                return outs[0] if len(outs) == 1 else torch.cat(outs, dim=0)

            # 赋**普通函数**（不是 MethodType）：nn.Module.__call__ 取 `self.forward`
            # 后直接 `forward_call(*args)`，普通函数正好只收业务参数。
            sub.forward = _fwd
            try:
                sub._h3_ff_chunk = ct
            except Exception:
                pass
            n += 1
    if skipped and not n:
        return 0, (f"{skipped} 个 Linear 分块自测全部不通过（模块里含跨 token 操作，"
                   "不是纯逐行 FFN）——本次不装，输出保持原样")
    if skipped:
        return n, f"{skipped} 个 Linear 自测不通过已跳过（其余 {n} 个已装）"
    return n, ""


def render_segment(模型, clip, video_vae, audio_vae, negative, cfg, net,
                   root, g, video_t, audio_t, kind, idx,
                   seg_prompts, seg_label_orders, pool_tensors, refs,
                   first_frame, guide, tail_kf_latent, head_kf_latent, cur_seed,
                   skip_f, vis_len,
                   wav, sample_rate, bh, report, 采样器, 调度器, model_tag=None,
                   _up_swap=False):
    """基础段 AV latent -> 高清分段直接落盘（放大→重采样→解码→裁剪）。

    model_tag：二采模型结构签名（model_tag()），仅用于报告标注与写进
    manifest.upscale.segs[g].model 留痕——不进 params_hash，换模型不会
    触发既有高清分段重做（要重做请设「重跑起始段」）。

    _up_swap：本次二采用的 `模型` 是否与一采模型是**两份不同权重**
    （`upscale.models_distinct` 判定，nodes.py 传入）。为真时解码前会先卸掉
    精化 UNET（P1-2）：那时它反正要被换出去给下段一采让位，卸载是零额外代价；
    A==B 时会多一次回载，所以不卸。

    主循环逐段调用（采样定稿/回放载入之后、基础段落盘之前）：分段视频与
    缩略图沿用基础段同名（单份产物——seg_NNN.mp4 即高清结果），另存尾帧锚
    uplast_NNN.png 供下游使用，并原子写 manifest.upscale 记录。
    裁剪口径与主循环 _decode_crop 完全一致：skip_f/vis_len 由调用方按基础
    分辨率帧的门控/切镜决策传入（机制零漂移，二采只接管落盘的帧）；
    音频沿用基础段原轨（零音频回归）。跨缝连续性由生成期桥锚 keyframe
    （_apply_guide）兜底，二采路径不再做任何像素级平滑。
    返回 (高清尾帧 CPU tensor, 最新 upscale 存档状态)；异常向上抛，由调用方
    降级为基础分辨率保存（基础链产物不受影响）。
    """
    import comfy.model_management

    t0 = time.perf_counter()
    purge_legacy(root, g)
    _h, _w = int(video_t.shape[-2]), int(video_t.shape[-1])
    try:
        _h2, _w2, _eff = resolve_target_hw(_h, _w, cfg)
        _tw, _th = _w2 * VAE_DOWNSAMPLE, _h2 * VAE_DOWNSAMPLE
    except ValueError as e:
        # 目标尺寸非法（下采样等）：日志行占位，真正的 ✗ 报告与整链终止由
        # render_latent 里的 preflight 统一产出（同一纯函数、同参数必复现）
        _tw = _th = 0
        _eff = 1.0
        print(f"[H3二采] 段{g + 1}：目标尺寸非法（{e}）——交给预检统一报告", flush=True)
    if net is None:
        print(f"[H3二采] 段{g + 1}：开始渲染（纯精化不放大 → {_tw}×{_th} 像素，"
              f"未加载放大网络）", flush=True)
    else:
        from . import upscale_net
        try:
            _ndev = next(net.parameters()).device
        except StopIteration:
            _ndev = "?"
        _heal_note = "" if str(_ndev).startswith("cuda") \
            else "（放大前腾挪显存后自愈回 GPU，见下行）"
        print(f"[H3二采] 段{g + 1}：开始渲染（{upscale_net.kind_of(net)} → {_tw}×{_th} 像素，"
              f"放大网络 @ {_ndev}{_heal_note}）", flush=True)
    _timing = {}
    _vram = {}
    _rss = {}

    def _dump_partial(where):
        """中断时把**已采到的**埋点吐出来（段末汇总没机会跑时的唯一线索）。"""
        for line in (_vram_report(g + 1, _vram), _rss_report(g + 1, _rss)):
            if line:
                print(line + f"（{where}前已采到）", flush=True)
        perf.emit({"kind": "fail", "seg": g + 1, "where": where})

    try:
        up_v, tw, th, up_seed, bridged, hf_gain, retried = render_latent(
            模型, clip, video_vae, audio_vae, negative, cfg, net,
            video_t, audio_t, kind, idx, seg_prompts, seg_label_orders,
            pool_tensors, refs, first_frame, guide, tail_kf_latent, head_kf_latent, cur_seed,
            采样器, 调度器, report=report, _timing=_timing, seg_no=g + 1,
            _vram=_vram, _rss=_rss)
    except BaseException:
        _dump_partial("二采渲染中断")
        raise
    # P1-2：解码前卸掉精化 UNET（仅在 A≠B 时——见 _up_swap 说明）。
    # 为什么值得做（三条都可核实）：
    #   ① 官方 decode 会先 load_models_gpu([vae_patcher], memory_required=…)，
    #      UNET 驻留时这一步要腾挪/换页（comfy/sd.py:1241）；
    #   ② batch_number = get_free_memory / memory_used（sd.py:1242）——可用小则退化；
    #   ③ 0.36 新增的 tile 批次优化（vae.py:590）也用同一个低估口径，可用小则
    #      batch 恒为 1，「每批多块 tile 提速 ~7%」拿不到。
    # 注意 H3 视频 VAE 声明 handles_tiling=True，本来就内部分块解码，
    # 所以收益是「解码更快」，不是「避免降级到 tiled」。
    # 用 unload_model_and_clones 而非 unload_all_models：前者按 clone_base_uuid
    # 只卸同源模型，解码要用的 VAE 会保留（不用立刻重载）。
    # 顺序同项目其它腾挪点：unload → gc → empty_cache（反序收不回）。
    if _up_swap:
        try:
            comfy.model_management.unload_model_and_clones(模型)
            gc.collect()
            torch.cuda.empty_cache()
        except Exception:
            pass
    # 解码高清 latent -> 高清帧。注意：H3 视频 VAE 声明 handles_tiling=True
    # （comfy/sd.py:1020），**它本来就内部分块解码**（256px 空间 tile + 17 帧时序块），
    # 不存在「OOM 才降级到 tiled」这个动作——这里无需也不该做显存干预。
    _vram_probe_reset()
    _t = time.perf_counter()
    frames = video_vae.decode(up_v)
    del up_v
    torch.cuda.empty_cache()
    if len(frames.shape) == 5:
        frames = frames.reshape(-1, frames.shape[-3], frames.shape[-2], frames.shape[-1])
    frames = frames[skip_f:skip_f + vis_len]
    _timing["decode"] = time.perf_counter() - _t
    _vram["decode"] = _vram_probe()
    _vram_probe_reset()
    _rss["decode"] = _rss_probe()
    _alloc, _used = _vram["decode"] or (None, None)
    _vb = "n/a" if _used is None else f"{_used:.2f}"
    _rb = "n/a" if _rss["decode"] is None else f"{_rss['decode']:.2f}"
    print(f"[H3二采] 段{g + 1} · 解码后 显存{_vb}GB · RSS{_rb}GB", flush=True)
    perf.emit({"kind": "mark", "seg": g + 1, "stage": "decode", "label": "解码后",
               "vram_alloc": _alloc, "vram_peak": _used, "vram_used": _used,
               "rss": _rss["decode"]})
    # 耗时账：「多段变卡」归根结底是**时间**现象，只记字节看不出来。
    # 每段四个阶段，跨段对比即可定位到底是哪一阶段在爬。
    perf.emit({"kind": "timing", "seg": g + 1,
               **{k: round(float(_timing.get(k) or 0.0), 2)
                  for k in perf.TIMING_KEYS}})
    # 段末汇总（六段一行，便于对比；逐点行已经先打过了，这里只是 recap）
    _vram_line = _vram_report(g + 1, _vram)
    if _vram_line:
        print(_vram_line, flush=True)
    _rss_line = _rss_report(g + 1, _rss)
    if _rss_line:
        print(_rss_line, flush=True)
    # 像素域锐化（抗糊 N4）：解码后、编码前——连 VAE 解码的软化一起补偿；
    # CPU 分块零显存，amount=0 原对象直通
    _t = time.perf_counter()
    _psp = float(cfg.get("pixel_sharpen") or 0.0)
    if _psp > 0.0:
        frames = pixel_sharpen_frames(frames, _psp)
    # 像素域清晰度度量（抗糊 N6）：跨参数可比的绝对清晰度锚点（记录用，
    # 不进指纹）；HQ 编码档（抗糊 N5）解析成具体参数透传编码层
    sharp = round(pixel_sharpness(frames), 2)
    _ehq = bool(cfg.get("encode_hq"))
    _ecrf, _epreset, _eaq, _edith = resolve_encode_quad(_ehq, cfg.get("x264_crf"))
    if not checkpoint.save_segment_mp4(root, g, frames, wav, sample_rate,
                                       fresh=True, crf=_ecrf, preset=_epreset,
                                       aq_mode=_eaq, dither=_edith):
        raise RuntimeError("高清分段编码失败（save_av_mp4 返回失败，详见 ComfyUI 控制台输出）")
    checkpoint.save_thumb(root, g, frames[0])
    files = checkpoint.upscale_files(g)
    _save_png(os.path.join(root, files["last"]), frames[-1])
    _timing["store"] = time.perf_counter() - _t
    print(f"[H3二采] 段{g + 1}：完成，seg_{g:03d}.mp4 已更新为高清"
          f"（总耗时 {time.perf_counter() - t0:.0f}s）", flush=True)
    _sig, _tier, _mo = resolve_refine_sigma(cfg, video_t)
    up_state = write_record(root, g, cfg, up_seed, (tw, th), bh,
                            hf_gain=hf_gain, motion=_mo, sharp=sharp,
                            model_tag=model_tag)
    _n, _d = tail_refine_args(_sig, cfg["steps"])   # _n=执行步数，_d=σ 起点
    _tb = float(cfg.get("time_bias") or 0.0)
    _mix = float(cfg.get("mix") or 0.0)
    _sh = float(cfg.get("shift") or 0.0)
    _stg = float(cfg.get("stg") or 0.0)
    _stgb = int(cfg.get("stg_block") if cfg.get("stg_block") is not None else 25)
    _passes = int(cfg.get("passes") or 1)
    _sigmas = cascade_sigmas(_sig, _passes, float(cfg.get("decay") or 0.5))
    _sig_str = "/".join(f"{s:g}" for s in _sigmas)
    _shp = float(cfg.get("sharpen") or 0.0)
    _ehq = bool(cfg.get("encode_hq"))
    _sam = str(cfg.get("sampler") or "").strip()
    _sch = str(cfg.get("scheduler") or "").strip()
    _kind = "—"
    if net is not None:
        from . import upscale_net
        _kind = upscale_net.kind_of(net)
    _sm_lbl = str(cfg.get("size_mode") or "倍率")
    _mode_bit = "" if _sm_lbl == "倍率" else f"（{_sm_lbl}）"
    report.append(f"段{g + 1} 二采：{tw}×{th} · {_kind} ×{_eff:g}{_mode_bit} · "
                  f"精化 {('×'.join([str(_n)] * _passes))} 步 @ σ≈{_sig_str} · "
                  f"细节 {hf_gain:+.0%} · 清晰 {sharp:.1f} · "
                  f"{time.perf_counter() - t0:.0f}s"
                  + (" · 桥锚" if bridged else "")
                  + (f" · 偏置{_tb:g}" if _tb > 0 else "")
                  + (f" · 混合{_mix:g}" if _mix > 0 else "")
                  + (f" · 自适应σ{_tier}({_mo:.2f})" if _tier else "")
                  + (f" · shift{_sh:g}" if _sh > 0 else "")
                  + (f" · STG{_stg:g}(b{_stgb})" if _stg > 0 else "")
                  + (f" · 锐化{_shp:g}" if _shp > 0 else "")
                  + (f" · 像素锐{_psp:g}" if _psp > 0 else "")
                  + (" · 高清编码档" if _ehq else "")
                  + ((f" · {(_sam + '/' + _sch).rstrip('/')}")
                     if (_sam or _sch) else "")
                  + (" · 重试取优" if retried else "")
                  + (f" · 二采模型{model_tag}" if model_tag else ""))
    _te = _timing.get("te_hit")
    _te_txt = "" if _te is None else ("（TE命中）" if _te else "（TE未命中）")
    report.append(f"⏱ 段{g + 1} 二采分解：神经放大 {_timing.get('up', 0.0):.0f}s · "
                  f"高清条件 {_timing.get('cond', 0.0):.0f}s{_te_txt} · "
                  f"精化重采 {_timing.get('refine', 0.0):.0f}s · "
                  f"高清解码 {_timing.get('decode', 0.0):.0f}s · "
                  f"编码落盘 {_timing.get('store', 0.0):.0f}s")
    # 收尾：释放二采残留（放大 latent / 解码帧 / 重采样缓存），给下段基础采样腾显存
    #
    # 放大网络是否**强制卸载**由机器级性能设置 `keep_upscaler_resident` 决定：
    #   · True（默认）= 不强制卸载 → 网络留在 MODEL_CACHE，段间零加载（现状口径）
    #   · False       = force_unload → 删缓存 + soft_empty_cache，下段重载（省内存）
    # ⚠ 2026-09-23 修：此前条件是 `cfg.get("force_unload") and not keep_...` —— 而
    #   `force_unload` 这个键**已无任何写入端**（前端控件删了、`upscale.parse_state`
    #   不再输出、nodes 也不再写）→ `cfg.get(...)` 恒 None → 整个分支**永假**，
    #   `upscale_net.force_unload()` 成了不可达代码，而新开关还标着「已接线」。
    #   现在把正面条件归还给 keep_upscaler_resident（默认 True = 与旧默认同行为）。
    if net is not None and not cfg.get("keep_upscaler_resident"):
        # 强制卸载：把放大网络从缓存里删掉 + soft_empty_cache（下段重新从磁盘加载，
        # 换取最大显存/内存头寸——多段链后段比首段更易 OOM 的主因即 CPU 侧权重副本）
        from . import upscale_net
        upscale_net.force_unload(net)
    else:
        torch.cuda.empty_cache()
    return frames[-1].detach().float().cpu(), up_state


def write_record(root, g, cfg, seed, size, bh, hf_gain=None, motion=None,
                 sharp=None, model_tag=None):
    """段 g 高清渲染记录原子写盘（重读 manifest 防竞态覆盖并发进度）。

    bh=该段基础身份指纹（调用方用本地 full_hashes/seeds 现算，不依赖磁盘
    manifest 的写入时机）；hf_gain=细节增益 / motion=段运动量 / sharp=像素域
    清晰度（均仅记录供 ab_report 复盘与自适应阈值校准，不参与重做判定）；
    model_tag=二采模型结构签名，**仅留痕不进 hash**——_record_valid 只比
    hash/base_hash，故换二采模型不会让既有记录失效（不自动重做）。
    返回最新 upscale dict（调用方回填 proj_upscale，后续主循环的 manifest
    快照写盘才不会把记录冲掉）。
    """
    ph = params_hash(cfg)
    rec = {"hash": ph, "base_hash": bh, "seed": seed, "done": True,
           "files": checkpoint.upscale_files(g), "size": list(size)}
    if model_tag:
        rec["model"] = model_tag
    if hf_gain is not None:
        rec["hf_gain"] = round(float(hf_gain), 4)
    if motion is not None:
        rec["motion"] = round(float(motion), 4)
    if sharp is not None:
        rec["sharp"] = round(float(sharp), 2)
    fresh = checkpoint.load_manifest(root) or {}
    up_state = dict(fresh["upscale"]) if isinstance(fresh.get("upscale"), dict) else {}
    segs = list(up_state.get("segs") or [])
    while len(segs) <= g:
        segs.append(None)
    segs[g] = rec
    up_state["segs"] = segs
    up_state["hash"] = ph
    up_state["params"] = _hash_params(cfg)
    fresh["upscale"] = up_state
    fresh["updated_at"] = time.time()
    checkpoint.save_manifest(root, fresh)
    return up_state


def purge_legacy(root, g):
    """清掉旧版二采独立产物（upseg_*/upthumb_*，防新旧文件混淆）。"""
    for f in checkpoint.upscale_legacy_files(g):
        try:
            os.remove(os.path.join(root, f))
        except OSError:
            pass


def _save_png(path, frame, long_edge=None):
    """单帧落盘 PNG（失败返回 False，不阻断主流程）。"""
    try:
        from PIL import Image
        arr = (frame.detach().float().clamp(0.0, 1.0).cpu().numpy() * 255.0).astype("uint8")
        img = Image.fromarray(arr)
        if long_edge:
            w, h = img.size
            s = float(long_edge) / max(w, h)
            if s < 1.0:
                img = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
        img.save(path)
        return True
    except Exception:
        return False


def _load_frame_png(path, device=None):
    """读回 PNG 锚帧 -> [H,W,3] float 0-1 tensor（失败返回 None）。"""
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(path).convert("RGB")
        arr = np.asarray(img).astype("float32") / 255.0
        t = torch.from_numpy(arr)
        return t.to(device) if device is not None else t
    except Exception:
        return None


def try_final(root, cfg, report, skip_slots=None):
    """全链段高清记录齐时流式拼接 seg_*.mp4 -> final_时间戳.mp4。

    skip_slots=不进成片的槽位集合（段禁用「不上链」）：跳过这些段的记录
    校验与拼接——禁用段无记录不阻塞全片高清，有旧记录也不混进成片
    （与基础分辨率成片口径一致：禁用段不进成片）。
    外部素材段（序章/插入视频）本版起不做二采：无记录时按基础分辨率分段
    直通，拼接时统一 reformat 缩放到目标画幅；旧版存档里已二采过的记录
    仍沿用（记录有效=文件已是高清）。生成段（提示词段）必须有有效高清
    记录，缺任一段回退主循环内存帧编码基础分辨率成片（分段高清不受影响）。
    返回 True 表示已产出高清成片（调用方跳过基础分辨率成片编码，单份产物）。
    """
    skip = {int(s) for s in (skip_slots or [])}
    mf = checkpoint.load_manifest(root) or {}
    total = int(mf.get("total") or 0)
    done = int(mf.get("done") or 0)
    if total <= 0 or done < total:
        return False
    ph = params_hash(cfg)
    segs = _records(mf)
    use = [g for g in range(total) if g not in skip]
    if not use:
        report.append("二采成片：所有段均已禁用（不上链），无成片可拼")
        return False
    ins_slots = {int(x.get("slot", -1)) for x in (mf.get("inserts") or [])
                 if isinstance(x, dict)}
    if mf.get("has_prologue"):
        ins_slots.add(0)
    sources, rec_sizes, first_rec = [], [], -1
    for g in use:
        if g < len(segs) and _record_valid(segs, root, g, ph, base_hash(mf, g)):
            sources.append(checkpoint.resolve_project_file(root, segs[g]["files"]["mp4"]))
            rec_sizes.append(segs[g].get("size") or [None, None])
            if first_rec < 0:
                first_rec = len(sources) - 1
        elif g in ins_slots:
            # 外部素材段直通：高清产物与基础分段同名（finals/seg_NNN.mp4），
            # 无有效记录时该文件就是基础分辨率版本（旧根目录裸名双兼容）
            basic = checkpoint.resolve_project_file(root, f"seg_{g:03d}.mp4")
            if not os.path.isfile(basic):
                report.append(f"二采成片：段{g + 1} 基础分段缺失（seg_{g:03d}.mp4），"
                              "成片按基础分辨率编码")
                return False
            sources.append(basic)
        else:
            report.append("二采成片：部分生成段无有效高清记录，成片按基础分辨率编码")
            return False
    if rec_sizes and any(s != rec_sizes[0] for s in rec_sizes):
        report.append("二采成片：分段高清尺寸不一致，成片按基础分辨率编码")
        return False
    out_name = f"final_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    out_rel = f"finals/{out_name}"
    try:
        from . import media
    except ImportError:
        import media
    # 目标画幅取首个带记录分段【实测】尺寸（记录 size 修复前存的是转置 [高, 宽]），
    # 显式传给 concat：直通的基础分辨率外部素材段按此缩放对齐；实测失败不传，
    # concat 退回首源画幅（首个带记录源优先排在前时两者一致）
    target_wh = media.probe_video_size(sources[first_rec]) if first_rec >= 0 else None
    _crf, _preset, _aq, _dither = resolve_encode_quad(
        cfg.get("encode_hq"), cfg.get("x264_crf"))
    if media.concat_av_mp4(sources, os.path.join(checkpoint.finals_dir(root), out_name),
                           width=target_wh[0] if target_wh else None,
                           height=target_wh[1] if target_wh else None,
                           crf=_crf, preset=_preset, aq_mode=_aq,
                           dither=_dither):
        fresh = checkpoint.load_manifest(root) or dict(mf)
        fresh.setdefault("finals", []).append(out_rel)
        up_state = dict(fresh.get("upscale") or {})
        up_state.setdefault("finals", []).append(out_rel)
        fresh["upscale"] = up_state
        fresh["updated_at"] = time.time()
        checkpoint.save_manifest(root, fresh)
        sz = target_wh or media.probe_video_size(sources[0]) or ("?", "?")
        _ins_n = sum(1 for g in use if g in ins_slots and (g >= len(segs)
                    or not _record_valid(segs, root, g, ph, base_hash(mf, g))))
        report.append(f"二采成片：{len(use)} 段流式拼接 → {out_name}"
                      f"（{sz[0]}×{sz[1]}，音轨沿用原声）"
                      + (f"，剔除禁用段 {len(skip)} 段" if len(use) < total else "")
                      + (f"，外部素材段 {_ins_n} 段按基础分辨率缩放对齐" if _ins_n else ""))
        return True
    report.append(f"二采成片编码失败（{media.last_error}）——回退编码基础分辨率成片，"
                  "分段高清不受影响")
    return False
