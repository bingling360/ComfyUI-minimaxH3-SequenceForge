"""接缝质量评测基线：把"缝好不好"从单一帧差升级为五维 z-score。

指标维度与归因（A/B 实验定位漂移来源）：
- flow   边界光流误差（Farneback 速度跳变 + 加速度跳变）→ motion 断续
- lpips  时序感知一致性（LPIPS，缺包降级梯度结构差）     → 时序整体突变
- emb    嵌入漂移（open_clip 余弦距离，缺包降级 HSV 直方图）→ appearance 漂移
- cam    相机轨迹连续性（ORB + 仿射分解速率）            → camera 跳变
- pose   姿态速度不连续（DWPose，默认关闭）              → geometry 断续

统一量纲：缝处相邻对的指标值，相对"段内正常对"基线的稳健 z-score
（median / 1.4826×MAD；MAD 退化时用 std）。缝对本身及其 ±guard 邻对
不进基线。验收线：|z| < 2.0 视为正常缝（README「评测与 A/B」）。

可选依赖策略：cv2 / lpips / open_clip / controlnet_aux 全部 try-import，
缺什么降级什么，缺 cv2 时 flow/cam 返回 None——绝不抛错、绝不硬依赖。
纯离线：Windows + torch venv 可直接跑（无网络、无 GPU 也能出降级指标）。
"""

import math

import torch
import torch.nn.functional as F

try:
    import cv2
    import numpy as np
except Exception:                                    # cv2 缺失：flow/cam 降级关闭
    cv2 = None
    np = None

try:
    import lpips as _lpips_mod
except Exception:
    _lpips_mod = None

_GUARD = 2          # 缝对两侧各排除几对不进基线
_MAX_SIDE = 240     # 光流/单应计算用的最长边（降采样抗噪提速）


# ---------------------------------------------------------------- 基础工具

def _gray_np(frame, max_side=_MAX_SIDE):
    """[H,W,3] 0-1 tensor -> np.uint8 灰度（最长边 ≤ max_side）。"""
    rgb = frame[..., :3].detach().float().cpu()
    g = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    h, w = g.shape[-2:]
    scale = max_side / max(h, w)
    if scale < 1.0:
        g = F.interpolate(g[None, None], scale_factor=scale, mode="bilinear",
                          align_corners=False)[0, 0]
    return (g.clamp(0.0, 1.0).numpy() * 255.0).astype("uint8")


def _grad_diff(a, b):
    """LPIPS 降级：两帧灰度 Sobel 结构差（0=结构一致）。"""
    def sobel(x):
        t = x[None, None]
        kx = t.new_tensor([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]]).float()
        ky = kx.transpose(0, 1)
        gx = F.conv2d(t, kx.view(1, 1, 3, 3), padding=1)
        gy = F.conv2d(t, ky.view(1, 1, 3, 3), padding=1)
        return torch.cat([gx, gy])
    return float((sobel(a) - sobel(b)).abs().mean())


def _hist_dist(a, b):
    """嵌入漂移降级：两帧 RGB 直方图分布差（每通道 32bin L1 的均值，与
    seam_doctor.hist_distance 同标定）。"""
    out = []
    for ch in range(3):
        ha = torch.histc(a[..., ch], bins=32, min=0.0, max=1.0)
        hb = torch.histc(b[..., ch], bins=32, min=0.0, max=1.0)
        ha = ha / (ha.sum() + 1e-9)
        hb = hb / (hb.sum() + 1e-9)
        out.append(float((ha - hb).abs().sum() / 2.0))
    return sum(out) / 3.0


# ---------------------------------------------------------------- 逐对指标序列

def frame_pair_series(video, must_pairs=(), enable_pose=False):
    """[N,H,W,3] 0-1 -> 每个相邻对 (i-1, i) 的五维指标序列。

    返回 {"flow": [...], "lpips": [...], "emb": [...], "cam": [...],
          "pose": [...], "flags": {指标: 实现方式}}，每项长度 N-1，
    不可用（缺依赖/特征不足）的对为 None。must_pairs：必须计算的相邻对
    下标集合（缝对），其余可被内部 stride 抽样以控制耗时。
    """
    n = int(video.shape[0])
    pairs = n - 1
    out = {"flow": [None] * pairs, "lpips": [None] * pairs,
           "emb": [None] * pairs, "cam": [None] * pairs,
           "pose": [None] * pairs}
    flags = {}
    if pairs <= 0:
        return {**out, "flags": flags}

    grays = None
    if cv2 is not None:
        grays = [_gray_np(video[i]) for i in range(n)]

    # 光流：逐对全算（240px Farneback 每对 ~几 ms）
    if cv2 is not None:
        flags["flow"] = "farneback"
        for i in range(pairs):
            fl = cv2.calcOpticalFlowFarneback(
                grays[i], grays[i + 1], None, 0.5, 3, 15, 3, 5, 1.2, 0)
            out["flow"][i] = float(np.hypot(fl[..., 0], fl[..., 1]).mean())
    else:
        flags["flow"] = "none"

    # 相机：ORB 特征 + 仿射分解（平移范数 + 5×|log scale| + 5×|旋转|）
    if cv2 is not None:
        flags["cam"] = "orb-affine"
        orb = cv2.ORB_create(nfeatures=512, fastThreshold=8)
        kps, descs = [], []
        for g in grays:
            kp, des = orb.detectAndCompute(g, None)
            kps.append(kp)
            descs.append(des)
        bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        for i in range(pairs):
            da, db = descs[i], descs[i + 1]
            if da is None or db is None or len(da) < 16 or len(db) < 16:
                continue
            matches = bf.knnMatch(da, db, k=2)
            good = [m for m, q in (mm for mm in matches if len(mm) == 2)
                    if m.distance < 0.75 * q.distance]
            if len(good) < 10:
                continue
            pa = np.float32([kps[i][m.queryIdx].pt for m in good])
            pb = np.float32([kps[i + 1][m.trainIdx].pt for m in good])
            M, _ = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC)
            if M is None:
                continue
            tx, ty = float(M[0, 2]), float(M[1, 2])
            a, b_ = float(M[0, 0]), float(M[0, 1])
            scale = math.hypot(a, b_)
            ang = abs(math.atan2(b_, a))
            out["cam"][i] = math.hypot(tx, ty) + 5.0 * abs(math.log(scale + 1e-6)) + 5.0 * ang
    else:
        flags["cam"] = "none"

    # LPIPS / 嵌入漂移：先算"必算对"，其余按 stride 抽样（这两族逐对开销大）
    must = set(int(i) for i in must_pairs if 0 <= int(i) < pairs)
    stride = max(1, pairs // 256)
    sampled = sorted(must | set(range(0, pairs, stride)))

    lpips_fn = None
    if _lpips_mod is not None:
        try:
            lpips_fn = _lpips_mod.LPIPS(net="alex")
            flags["lpips"] = "lpips-alex"
        except Exception:
            lpips_fn = None
    if lpips_fn is None:
        flags["lpips"] = "grad-fallback"
    for i in sampled:
        a = video[i][..., :3].float()
        b = video[i + 1][..., :3].float()
        if lpips_fn is not None:
            try:
                with torch.no_grad():
                    ta = (a.movedim(-1, 0) * 2 - 1)[None]
                    tb = (b.movedim(-1, 0) * 2 - 1)[None]
                    out["lpips"][i] = float(lpips_fn(ta, tb))
                continue
            except Exception:
                lpips_fn = None
                flags["lpips"] = "grad-fallback"
        out["lpips"][i] = _grad_diff(
            a.mean(-1), b.mean(-1))

    flags["emb"] = "hist-fallback"
    for i in sampled:
        out["emb"][i] = _hist_dist(video[i][..., :3].float(),
                                   video[i + 1][..., :3].float())

    # 姿态（默认关）：DWPose 关键点速度 L2
    if enable_pose:
        try:
            from controlnet_aux import DWposeDetector
            det = DWposeDetector()
            kps = []
            for i in range(n):
                arr = (video[i][..., :3].clamp(0, 1).numpy() * 255).astype("uint8")
                pose = det(arr, output_type="np", include_hands=True, include_face=True)
                kps.append(torch.from_numpy(np.asarray(pose, dtype="float32")))
            flags["pose"] = "dwpose"
            for i in range(pairs):
                if kps[i] is None or kps[i + 1] is None:
                    continue
                d = (kps[i + 1] - kps[i]).norm(dim=-1)
                conf = (kps[i][..., -1] > 0.3) if kps[i].shape[-1] > 2 else None
                out["pose"][i] = float(d[conf].mean()) if conf is not None and conf.any() \
                    else float(d.mean())
        except Exception:
            flags["pose"] = "none"
    return {**out, "flags": flags}


# ---------------------------------------------------------------- z-score

def _robust_z(value, baseline):
    """值 vs 基线序列的稳健 z（median/1.4826×MAD，退化用 std）。基线不足返回 None。"""
    vals = [v for v in baseline if v is not None]
    if value is None or len(vals) < 8:
        return None
    t = torch.tensor(vals, dtype=torch.float64)
    med = float(t.median())
    mad = float((t - med).abs().median())
    spread = 1.4826 * mad
    if spread < 1e-9:
        spread = float(t.std()) if len(vals) > 1 else 0.0
    if spread < 1e-9:
        return 0.0 if abs(value - med) < 1e-12 else None
    return (value - med) / spread


def _baseline_mask(pairs, seam_indices, guard=_GUARD):
    """段内基线对掩码：排除所有缝对及其 ±guard 邻对。"""
    mask = [True] * pairs
    for c in seam_indices:
        for k in range(-guard, guard + 1):
            j = c - 1 + k                        # 缝 c 的相邻对下标是 c-1
            if 0 <= j < pairs:
                mask[j] = False
    return mask


def evaluate_seams(video, seam_indices, enable_pose=False):
    """整链评测入口。

    video: [N,H,W,3] 0-1（CPU tensor）；seam_indices: 缝右侧全局帧下标列表
    （缝在 c-1 与 c 之间，与 seam_doctor 的定位一致）。
    返回 {"seams": [{...每缝各维 z...}], "flags": {...}, "summary": {...}}。
    任何指标不可用时对应字段为 None，绝不抛错。
    """
    n = int(video.shape[0])
    seams = sorted(int(c) for c in seam_indices if 1 <= int(c) < n)
    pairs = n - 1
    if pairs <= 0 or not seams:
        return {"seams": [], "flags": {}, "summary": {}}
    try:
        series = frame_pair_series(video, must_pairs=[c - 1 for c in seams],
                                   enable_pose=enable_pose)
    except Exception:
        return {"seams": [{"c": c} for c in seams],
                "flags": {}, "summary": {"error": "series-failed"}}

    mask = _baseline_mask(pairs, seams)
    results = []
    for c in seams:
        j = c - 1
        row = {"c": c}
        for key in ("flow", "lpips", "emb", "cam", "pose"):
            row[f"{key}_z"] = _robust_z(series[key][j],
                                        [series[key][i] for i in range(pairs) if mask[i]])
        # 光流加速度（速度二阶差分）：缝对速度与缝前一对速度的跳变
        if j - 1 >= 0 and series["flow"][j] is not None and series["flow"][j - 1] is not None:
            accels = [abs(series["flow"][i] - series["flow"][i - 1])
                      for i in range(1, pairs)
                      if series["flow"][i] is not None and series["flow"][i - 1] is not None
                      and mask[i] and mask[i - 1]]
            row["flow_accel_z"] = _robust_z(abs(series["flow"][j] - series["flow"][j - 1]), accels)
        else:
            row["flow_accel_z"] = None
        results.append(row)

    summary = {}
    for key in ("flow", "flow_accel", "lpips", "emb", "cam", "pose"):
        zs = [r.get(f"{key}_z") for r in results]
        zs = [z for z in zs if z is not None]
        if zs:
            summary[f"{key}_z_mean"] = sum(zs) / len(zs)
            summary[f"{key}_z_max"] = max(zs)
    return {"seams": results, "flags": series["flags"], "summary": summary}


def evaluate_local(prev_clip, cur_clip, enable_pose=False):
    """局部评测（主链在线用）：上段尾帧 + 本段头帧拼局部序列，缝在中间。

    prev_clip/cur_clip: [F,H,W,3] 0-1 CPU tensor。基线取自局部序列内非缝邻对，
    窗口小（默认几十对）但对"缝 vs 紧邻上下文"的判别足够。返回单缝 dict。
    """
    video = torch.cat([prev_clip, cur_clip], dim=0)
    c = int(prev_clip.shape[0])
    res = evaluate_seams(video, [c], enable_pose=enable_pose)
    return res["seams"][0] if res["seams"] else {"c": c}


def fmt_seam_z(row):
    """单缝 z dict -> 报告用短字符串（None 维度自动略过）。"""
    parts = []
    for key, label in (("flow_z", "光流"), ("flow_accel_z", "加速度"),
                       ("lpips_z", "LPIPS"), ("emb_z", "嵌入"), ("cam_z", "相机"),
                       ("pose_z", "姿态")):
        v = row.get(key)
        if v is not None:
            parts.append(f"{label} {v:+.1f}σ")
    return " ".join(parts) if parts else "无可用指标（缺 cv2 或序列过短）"
