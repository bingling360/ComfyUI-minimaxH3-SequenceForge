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
import os
import time

import torch

from . import checkpoint
from . import grid

MODES = ("跟随生成", "手动选择")
PRECISIONS = ("fp32", "fp16", "bf16")
ENCODE_PROFILES = ("标准", "高清", "极致")
# HQ 编码档 → (crf, preset, aq-mode, 抖动)：x264 率失真 + 自适应量化 + 有序抖动。
# 「标准」= 现状（crf20 veryfast 无抖动，兼容）；「高清/极致」为抗糊抗条纹档——
# veryfast 比 medium 率失真差约 5-15%（编码层二次模糊），crf 20→16/13 保高清
# 细节；aq-mode=3 暗部自适应量化（保暗场细节）；Bayer 抖动打散 8bit 量化
# ——平滑渐变区的横向色带（条纹主源）。
_ENCODE_SETTINGS = {
    "标准": (20, "veryfast", None, False),
    "高清": (16, "medium", 3, True),
    "极致": (13, "slow", 3, True),
}
_PARAM_KEYS = ("model", "arch", "scale", "denoise", "steps", "cfg", "precision")


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
    encode = str(up.get("encode") or "").strip()
    if encode not in ENCODE_PROFILES:
        encode = "标准"
    # 抗糊武器库（全部默认关：STG=0 / passes=1 / 锐化=0 / 标准编码 / 采样器沿用
    # 主链 / retry 关——启用任一项才进指纹，既有二采记录不失效）
    stg_block = int(_num("stg_block", 25, 0, 49))
    return {
        "mode": mode,
        "model": str(up.get("model") or "").strip(),
        "arch": "3D" if str(up.get("arch") or "").strip().upper() == "3D" else "2D",
        "scale": _num("scale", 2.0, 1.0, 4.0),
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
        "encode": encode,
        "sampler": str(up.get("sampler") or "").strip(),
        "scheduler": str(up.get("scheduler") or "").strip(),
        "retry": up.get("retry") is True,
        "retry_target": _num("retry_target", 0.15, 0.05, 1.0),
        "include": include,
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
    """进指纹/记录的参数字典（params_hash 与 write_record.params 同口径）。"""
    keys = {k: cfg[k] for k in _PARAM_KEYS}
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
    if cfg.get("encode") and cfg["encode"] != "标准":
        keys["encode"] = cfg["encode"]
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
    """记录有效 = hash/base_hash 匹配且产物文件齐全（高清分段 mp4 + 缩略图 + 尾帧锚）。"""
    rec = segs[g] if g < len(segs) else None
    if not (isinstance(rec, dict) and rec.get("done")):
        return False
    if rec.get("hash") != ph or rec.get("base_hash") != bh:
        return False
    files = rec.get("files") or checkpoint.upscale_files(g)
    for key in ("mp4", "thumb", "last"):
        f = files.get(key)
        if not f or not os.path.isfile(os.path.join(root, f)):
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
    return w2 * 16, h2 * 16


def upscale_video(video_t, net, scale, arch="2D"):
    """[B,24,T,H,W] latent -> 神经放大（T 不变，H/W×scale 偶数对齐），float32。

    归一化口径与上游训练一致：放大前 (x-μ)/σ，放大后反变换；网络精度由
    load_model 的 precision 决定，输入输出统一 float32。scale 使目标等于
    原尺寸时原样返回克隆（等价纯二采不放大）。
    """
    h2, w2 = target_hw(video_t.shape[-2], video_t.shape[-1], scale)
    if (h2, w2) == (video_t.shape[-2], video_t.shape[-1]):
        return video_t.detach().to(torch.float32).clone()
    # 输入对齐网络设备：魔改 DynamicVRAM 运行时采样输出 latent 可能滞留 CPU，
    # 网络 @ cuda 时直接前向 = addmm 设备不匹配（mat1 on cpu）崩溃
    net_dev = next(net.parameters()).device
    x = video_t.detach().to(net_dev, torch.float32)
    nd = next(net.parameters()).dtype
    mean, std = _norm_tensors(x.device, nd)
    xn = (x.to(nd) - mean) / std
    if arch == "3D":
        y = net(xn, scale=float(scale), target_size=(x.shape[2], h2, w2))
    else:
        y = net(xn, scale=float(scale), target_hw=(h2, w2))
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

    if not cfg["model"]:
        raise ValueError("未选择放大模型——请从 HuggingFace "
                         "LBH-123-AI/Minimax_h3_latent_Upscaler 下载权重放入 "
                         "models/latent_upscale_models/，刷新导演台后在二采面板选择")
    dev = comfy.model_management.intermediate_device()
    if dev.type == "cpu":
        dev2 = _cuda_if_room()
        if dev2 is not None:
            print(f"[H3二采] intermediate_device 返回 CPU 但空闲显存充足"
                  f"——放大模型强制上 {dev2}", flush=True)
            dev = dev2
    print(f"[H3二采] 放大模型目标设备：{dev}", flush=True)
    try:
        return upscale_net.load_model(cfg["model"], dev, cfg["precision"], cfg["arch"])
    except FileNotFoundError as e:
        raise ValueError(f"放大模型加载失败：{e}") from None


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


def _vram_gb():
    """当前空闲显存（GB，comfy 语义；失败回退 torch，再失败返回 0）。"""
    try:
        import comfy.model_management as mm
        return mm.get_free_memory() / (1024 ** 3)
    except Exception:
        try:
            import torch
            return torch.cuda.mem_get_info()[0] / (1024 ** 3)
        except Exception:
            return 0.0


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


_CANVAS_ABORT_MP = 12.0    # 二采画布超此（MP）→ 硬停（VAE 解码 + 显存双重爆点）
_ACTIVATION_FACTOR = 4.0   # 采样峰值激活 ≈ 初始高清 latent 体积 × 系数（经验折中）
_SAFE_MARGIN_GB = 1.0      # 显存账目安全余量（避免贴着上沿静默崩）


def _dynamic_vram_active():
    """comfy-aimdo DynamicVRAM 是否在管权重。该机制把显存当权重缓存故意填满、
    按需换页（空闲显存小是常态而非异常），权重占用是弹性的——显存账目对它
    只作参考不作硬约束。"""
    try:
        import sys
        if any("aimdo" in k for k in sys.modules):
            return True
        import importlib.util
        return importlib.util.find_spec("aimdo") is not None
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

    # 1) 放大模型就绪
    try:
        dev = next(net.parameters()).device
        lines.append(f"✓ 放大模型就绪：{cfg.get('arch', '2D')} @ {dev}")
    except (StopIteration, AttributeError):
        lines.append("✗ 放大模型不可用——请先在二采面板选择有效权重")
        fail.append("放大模型不可用")

    # 2) 二采画布（latent 偶数 -> 像素 ·32）
    h, w = video_t.shape[-2], video_t.shape[-1]
    scale = float(cfg.get("scale", 2.0) or 2.0)
    h2, w2 = target_hw(h, w, scale)
    tw, th = w2 * 16, h2 * 16
    mp = tw * th / 1e6
    lines.append(f"… 画布：基础 {w * 16}×{h * 16} → 二采 {tw}×{th}"
                 f"（{mp:.1f}MP，×{scale:g}）")
    if mp > _CANVAS_ABORT_MP:
        cap = _calc_scale_cap(h, w)
        lines.append(f"✗ 二采画布 {tw}×{th}（{mp:.1f}MP）超上限 {_CANVAS_ABORT_MP:.0f}MP"
                     f"——VAE 解码与显存双重爆点；请把放大倍率降到 ≤{cap:g}×")
        fail.append(f"画布超限 {mp:.1f}MP")
    elif mp > 2.5:
        lines.append(f"⚠ 高清画布 {mp:.1f}MP 超 2.5MP 安全解码区，注意 fp16 高频溢出"
                     f"（出花屏/色块就降倍率或提采样精度）")

    # 3) 显存账目：对比【重采样时刻】可用 vs 高清新增峰值。
    # 预检时 TE/VAE/UNET 常全驻留（DynamicVRAM 更会把显存当权重缓存填满，
    # 空闲趋近 0 是常态），但 render_latent 在采样前 unload_all_models()
    # 全卸、只回载 UNET——可用 = 当前空闲 + 其他模型权重（确定性腾挪）。
    # DynamicVRAM 下权重本身可按需换页（弹性），账面紧张只 ⚠ 不硬停，
    # 真 OOM 由运行时降级链（自动卸载重试/LOW_VRAM 分块）兜底。
    free = _vram_gb()
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
                  采样器, 调度器, report=None, _timing=None, seg_no=None):
    """基础段 AV latent -> 高清视频 latent（放大 + 低强度重采样）。

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

    dev = next(net.parameters()).device
    if dev.type == 'cpu':
        # 上段二采异常后放大网络留在 CPU：优先纠偏回 GPU（intermediate_device
        # 在魔改运行时账面紧张时可能仍返回 CPU——此时 CPU 前向是分钟级）
        dev = _cuda_if_room() or comfy.model_management.intermediate_device()
        if dev.type == 'cuda':
            print(f"[H3二采] 段{seg_no}：放大网络在 CPU，已挪回 {dev}", flush=True)
        net.to(dev)
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
    tw, th = target_pixels(h, w, cfg["scale"])
    length = grid.latent_t_to_frames(video_t.shape[2])

    # 前置健康预检：在放大/分配高清内存【之前】把显存不足、画布越界、模型缺失
    # 一次暴露——任一 ✗ 抛 UpscaleAbortError，由主循环 re-raise 终止整链（不降级）。
    preflight(模型, cfg, net, video_t, audio_t, report)

    # 放大网络放大视频 latent 到 scale×（常驻 GPU，放大完卸回 CPU 腾给高清重采样）；
    # hf_up = 纯放大（无精化）的高频能量基线——细节增益度量的「前」
    _t = time.perf_counter()
    up_v = upscale_video(video_t, net, cfg["scale"], cfg["arch"])
    hf_up = latent_hf_energy(up_v)
    # 频域细节混合启用时保留纯放大 latent 的 CPU 副本（避开采样期显存峰值，
    # 精化后作低频锚；关闭时零开销不复制）
    mix = float(cfg.get("mix") or 0.0)
    up_v_base = up_v.detach().to("cpu", torch.float32) if mix > 0.0 else None
    _tmark("up", _t)
    print(f"[H3二采] 段{seg_no}：神经放大完成 → 高清条件构建"
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
                                               net, cfg["scale"], cfg["arch"])
            bridged = True
        up_tail = None
        if tail_kf_latent is not None:
            up_tail = upscale_video(tail_kf_latent.to(dev, torch.float32),
                                    net, cfg["scale"], cfg["arch"])
        up_head = None
        if head_kf_latent is not None:
            up_head = upscale_video(head_kf_latent.to(dev, torch.float32),
                                    net, cfg["scale"], cfg["arch"])
        cond = plugin_nodes.H3SeamlessChainSampler._apply_guide(
            cond, up_guide, length, tail_kf_latent=up_tail, head_kf_latent=up_head)

    # 放大网络工作完毕，卸回 CPU 释放显存给高清重采样
    net.cpu()
    _tmark("cond", _t)
    # CachedClipProxy 在位时标注本段高清条件构建的 TE 是否命中缓存
    if _timing is not None:
        _timing["te_hit"] = getattr(clip, "last_encode_hit", None)
    _te_hit = None if _timing is None else _timing.get("te_hit")
    _te_note = "" if _te_hit is None else \
        ("（TE命中缓存，无文本编码器前向）" if _te_hit is True
         else "（TE未命中——文本编码器前向中，魔改分页环境下可能分钟级）")
    print(f"[H3二采] 段{seg_no}：放大+高清条件就绪{_te_note}"
          + " → 显存腾挪（卸载驻留模型）…", flush=True)
    # 抢占式显存腾挪（README 既定设计，be43db8 重构时随三级降级一起被误删，
    # 2026-08-25 1.4× 精化实测 OOM 后恢复）：cond 已建好，TE/videoVAE/audioVAE
    # 精化阶段均用不到——全卸到 CPU（含 UNET），common_ksampler 原生机制只回载
    # UNET；否则 32GB 卡上 TE 驻留 + UNET + 高清激活三头挤兑，精化必 OOM。
    # gc.collect：跨段累积的 Python 侧 GPU 张量（上段精化输出/条件中间量/
    # STG 克隆）只有引用回收后显存块才真正归还——多段链后段比首段更易 OOM 的
    # 主因即在此（引用挂着的 CUDA 块 empty_cache 也收不回）
    comfy.model_management.unload_all_models()
    gc.collect()
    torch.cuda.empty_cache()
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
    ks_sampler = ks_sampler or 采样器
    ks_scheduler = ks_scheduler or 调度器

    def _is_dev_err(e):
        return "to be on the same device" in str(e)

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
                    return comfy_nodes.common_ksampler(
                        ks_model, cur_seed, ks_steps, cfg["cfg"], ks_sampler,
                        ks_scheduler, cond, negative, cur_latent,
                        denoise=ks_denoise)[0]

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
                               cfg["scale"], _vram_gb(), ks_steps, ks_denoise,
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

    # 多轮递降精化（cascade，抗糊 N2）：passes=1 即现状单轮；σ 序列由
    # cascade_sigmas 决定论派生（同参数同序列，重放一致）；种子逐轮 +1。
    # restore_rows 的 finally 覆盖整条精化链（任一轮异常都恢复守卫再上抛）
    sigmas = cascade_sigmas(sigma0, passes, decay)
    up_out_v, hf_gain, retried = None, 0.0, False
    _t = time.perf_counter()
    try:
        cur = latent
        for k, sk in enumerate(sigmas):
            cur = _refine_once(sk, cur, (seed + k) % 0xffffffffffffffff,
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
                cur2 = _refine_once(sk, cur2,
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
    _tmark("refine", _t)
    print(f"[H3二采] 段{seg_no}：精化重采样完成（{(_timing or {}).get('refine', 0.0):.0f}s）"
          "→ 高清解码…", flush=True)
    del cond, latent
    torch.cuda.empty_cache()
    del up_v_base
    return up_out_v, tw, th, seed, bridged, hf_gain, retried


def render_segment(模型, clip, video_vae, audio_vae, negative, cfg, net,
                   root, g, video_t, audio_t, kind, idx,
                   seg_prompts, seg_label_orders, pool_tensors, refs,
                   first_frame, guide, tail_kf_latent, head_kf_latent, cur_seed,
                   skip_f, vis_len,
                   wav, sample_rate, bh, report, 采样器, 调度器):
    """基础段 AV latent -> 高清分段直接落盘（放大→重采样→解码→裁剪）。

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
    _tw, _th = target_pixels(_h, _w, float(cfg.get("scale") or 2.0))
    print(f"[H3二采] 段{g + 1}：开始渲染（{cfg['arch']} {cfg['scale']:g}× → {_tw}×{_th} 像素，"
          f"放大网络 @ {next(net.parameters()).device}）", flush=True)
    _timing = {}
    up_v, tw, th, up_seed, bridged, hf_gain, retried = render_latent(
        模型, clip, video_vae, audio_vae, negative, cfg, net,
        video_t, audio_t, kind, idx, seg_prompts, seg_label_orders,
        pool_tensors, refs, first_frame, guide, tail_kf_latent, head_kf_latent, cur_seed,
        采样器, 调度器, report=report, _timing=_timing, seg_no=g + 1)
    # 解码高清 latent -> 高清帧（官方 VAE.decode 自带 OOM→tiled 降级，无需干预）
    _t = time.perf_counter()
    frames = video_vae.decode(up_v)
    del up_v
    torch.cuda.empty_cache()
    if len(frames.shape) == 5:
        frames = frames.reshape(-1, frames.shape[-3], frames.shape[-2], frames.shape[-1])
    frames = frames[skip_f:skip_f + vis_len]
    _timing["decode"] = time.perf_counter() - _t
    # 像素域锐化（抗糊 N4）：解码后、编码前——连 VAE 解码的软化一起补偿；
    # CPU 分块零显存，amount=0 原对象直通
    _t = time.perf_counter()
    _psp = float(cfg.get("pixel_sharpen") or 0.0)
    if _psp > 0.0:
        frames = pixel_sharpen_frames(frames, _psp)
    # 像素域清晰度度量（抗糊 N6）：跨参数可比的绝对清晰度锚点（记录用，
    # 不进指纹）；HQ 编码档（抗糊 N5）解析成具体参数透传编码层
    sharp = round(pixel_sharpness(frames), 2)
    _enc = str(cfg.get("encode") or "标准")
    _ecrf, _epreset, _eaq, _edith = _ENCODE_SETTINGS.get(
        _enc, _ENCODE_SETTINGS["标准"])
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
                            hf_gain=hf_gain, motion=_mo, sharp=sharp)
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
    _enc = str(cfg.get("encode") or "标准")
    _sam = str(cfg.get("sampler") or "").strip()
    _sch = str(cfg.get("scheduler") or "").strip()
    report.append(f"段{g + 1} 二采：{tw}×{th} · {cfg['arch']} {cfg['scale']:g}× · "
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
                  + (f" · {_enc}编码" if _enc != "标准" else "")
                  + ((f" · {(_sam + '/' + _sch).rstrip('/')}")
                     if (_sam or _sch) else "")
                  + (" · 重试取优" if retried else ""))
    _te = _timing.get("te_hit")
    _te_txt = "" if _te is None else ("（TE命中）" if _te else "（TE未命中）")
    report.append(f"⏱ 段{g + 1} 二采分解：神经放大 {_timing.get('up', 0.0):.0f}s · "
                  f"高清条件 {_timing.get('cond', 0.0):.0f}s{_te_txt} · "
                  f"精化重采 {_timing.get('refine', 0.0):.0f}s · "
                  f"高清解码 {_timing.get('decode', 0.0):.0f}s · "
                  f"编码落盘 {_timing.get('store', 0.0):.0f}s")
    # 收尾：释放二采残留（放大 latent / 解码帧 / 重采样缓存），给下段基础采样腾显存
    torch.cuda.empty_cache()
    return frames[-1].detach().float().cpu(), up_state


def write_record(root, g, cfg, seed, size, bh, hf_gain=None, motion=None,
                 sharp=None):
    """段 g 高清渲染记录原子写盘（重读 manifest 防竞态覆盖并发进度）。

    bh=该段基础身份指纹（调用方用本地 full_hashes/seeds 现算，不依赖磁盘
    manifest 的写入时机）；hf_gain=细节增益 / motion=段运动量 / sharp=像素域
    清晰度（均仅记录供 ab_report 复盘与自适应阈值校准，不参与重做判定）；
    返回最新 upscale dict（调用方回填 proj_upscale，后续主循环的 manifest
    快照写盘才不会把记录冲掉）。
    """
    ph = params_hash(cfg)
    rec = {"hash": ph, "base_hash": bh, "seed": seed, "done": True,
           "files": checkpoint.upscale_files(g), "size": list(size)}
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
            sources.append(os.path.join(root, segs[g]["files"]["mp4"]))
            rec_sizes.append(segs[g].get("size") or [None, None])
            if first_rec < 0:
                first_rec = len(sources) - 1
        elif g in ins_slots:
            # 外部素材段直通：高清产物与基础分段同名（seg_NNN.mp4），
            # 无有效记录时该文件就是基础分辨率版本
            basic = os.path.join(root, f"seg_{g:03d}.mp4")
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
    try:
        from . import media
    except ImportError:
        import media
    # 目标画幅取首个带记录分段【实测】尺寸（记录 size 修复前存的是转置 [高, 宽]），
    # 显式传给 concat：直通的基础分辨率外部素材段按此缩放对齐；实测失败不传，
    # concat 退回首源画幅（首个带记录源优先排在前时两者一致）
    target_wh = media.probe_video_size(sources[first_rec]) if first_rec >= 0 else None
    _crf, _preset, _aq, _dither = _ENCODE_SETTINGS.get(
        str(cfg.get("encode") or "标准"), _ENCODE_SETTINGS["标准"])
    if media.concat_av_mp4(sources, os.path.join(root, out_name),
                           width=target_wh[0] if target_wh else None,
                           height=target_wh[1] if target_wh else None,
                           crf=_crf, preset=_preset, aq_mode=_aq,
                           dither=_dither):
        fresh = checkpoint.load_manifest(root) or dict(mf)
        fresh.setdefault("finals", []).append(out_name)
        up_state = dict(fresh.get("upscale") or {})
        up_state.setdefault("finals", []).append(out_name)
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
