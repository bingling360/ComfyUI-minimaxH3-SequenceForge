"""桥帧质量打分：解码像素上的 Laplacian 清晰度 + 曝光/对比度（纯 torch 数学）。

打分对象是「将成为下段重叠桥」的尾部像素帧（解码产物现成，零额外开销）；
分数低于阈值判定为坏尾，自动回退档位固定 0 / 17 / 34 帧——
17 帧 = 5 个 latent token，天然踩 H3 的 17k+5 帧网格，切片 token 数不变。
"""

import math

import torch
import torch.nn.functional as F

BACKTRACK_STEPS = (17, 34)


def frame_scores(frames):
    """frames: [N,H,W,3] 0-1 float（任意设备）。返回每帧总分张量 [N]。

    总分 = log1p(Laplacian方差×1000) + 对比度×2 − 曝光裁剪占比×2.5
    （与 LtoJ 总控台选帧打分同形，便于阈值互相参考；灰度、最长边 ≤512 计算）

    灰度换算到 0-255 域再计算：LtoJ 阈值 30 标定在 255 域（清晰帧约
    90-130、糊帧 <30）；若在 0-1 域算，清晰帧只有 1-3，阈值 30 会把
    所有帧全部误报为坏尾（0-1 域方差缩小 255² 倍，log1p 压不住差值）。
    """
    rgb = frames[..., :3].float().movedim(-1, 1)                 # [N,3,H,W]
    gray = (rgb[:, 0] * 0.299 + rgb[:, 1] * 0.587 + rgb[:, 2] * 0.114) * 255.0
    h, w = gray.shape[-2:]
    scale = 512 / max(h, w)
    if scale < 1.0:
        gray = F.interpolate(gray.unsqueeze(1), scale_factor=scale,
                             mode="bilinear", align_corners=False).squeeze(1)
    lap_k = gray.new_tensor([[0.0, 1.0, 0.0], [1.0, -4.0, 1.0],
                             [0.0, 1.0, 0.0]]).view(1, 1, 3, 3)
    lap = F.conv2d(gray.unsqueeze(1), lap_k, padding=1).squeeze(1)
    sharp = lap.flatten(1).var(dim=1, unbiased=False)
    contrast = gray.flatten(1).std(dim=1, unbiased=False)
    clipped = ((gray < 5.0) | (gray > 250.0)).float().flatten(1).mean(dim=1)
    return torch.log1p(sharp * 1000.0) + contrast * 2.0 - clipped * 2.5


def pick_backtrack(score, limit: int, threshold: float):
    """尾部窗口总分 -> (回退帧数, 回退点得分)。

    尾帧达标 -> 不回退；否则依序尝试 17 / 34 帧（受 limit 截断），
    取首个达标档位；全部不达标 -> 不硬剪，返回 (0, 尾帧分) 由调用方警告。
    """
    tail = float(score[-1])
    if tail >= threshold:
        return 0, tail
    for back in BACKTRACK_STEPS:
        if back > limit:
            break
        idx = len(score) - 1 - back
        if idx < 0:
            break
        s = float(score[idx])
        if s >= threshold:
            return back, s
    return 0, tail


def loudness_align_head(wav, prev_wav, rate=44100, max_db=6.0, fade_s=1.0):
    """段首响度对齐：增益匹配上段尾部 RMS（±max_db 钳制），fade_s 内线性渐出回 1。

    分镜段间是镜头切换，画面允许跳变，但响度跳变听感突兀；对齐只作用于段首
    fade_s 窗（之后恢复段自身动态），且增益不沿链累积（每段独立相对上段计算）。
    返回 (wav, 实际增益 dB 或 None——上段静音/样本过短时不干预)。
    """
    tail = prev_wav[..., -max(1, int(rate * 0.25)):]
    n = min(tail.shape[-1], wav.shape[-1])
    if n < 32:
        return wav, None
    ra = float(tail[..., :n].pow(2).mean().sqrt())
    rb = float(wav[..., :n].pow(2).mean().sqrt())
    if ra < 1e-6 or rb < 1e-6:
        return wav, None
    db = max(-max_db, min(max_db, 20.0 * math.log10(ra / rb)))
    fade = min(wav.shape[-1], max(1, int(rate * fade_s)))
    ramp = torch.linspace(0.0, 1.0, fade, device=wav.device, dtype=wav.dtype)
    gain = (10.0 ** (db / 20.0)) * (1.0 - ramp) + ramp
    out = wav.clone()
    out[..., :fade] = wav[..., :fade] * gain
    return out, db


def seam_metrics(prev_frame, head_frame, prev_wav=None, head_wav=None, rate=44100, tail_s=0.25):
    """接缝后验测量（测而不干预）：上一段最后可见帧 vs 本段首帧。

    prev_frame / head_frame: [H,W,3] 0-1（任意设备，内部搬 CPU）；
    prev_wav / head_wav: [C,samples]，调用方已截取接缝两侧约 tail_s 秒。
    返回 (帧差 0-1 均绝对差, 响度跳变 dB 或 None——wav 缺失/过短时 None)。
    dB > 0 表示接缝后比接缝前响。
    """
    a = prev_frame.detach().float().cpu()
    b = head_frame.detach().float().cpu()
    diff = float((a - b).abs().mean())
    db = None
    if prev_wav is not None and head_wav is not None:
        pa = prev_wav.detach().float().cpu()
        pb = head_wav.detach().float().cpu()
        n = min(pa.shape[-1], pb.shape[-1])
        if n >= max(2, int(rate * tail_s * 0.2)):        # 至少约 50ms 才有意义
            ra = float(pa[..., :n].pow(2).mean().sqrt())
            rb = float(pb[..., :n].pow(2).mean().sqrt())
            db = 20.0 * math.log10((ra + 1e-5) / (rb + 1e-5))
    return diff, db
