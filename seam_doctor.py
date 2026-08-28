"""接缝体检（Seam Doctor）：只测不治——把拼接缝上到底发生了什么逐项量化进报告。

用户侧「迷之跳变」排查用：不动任何生成逻辑，接采样器「图像/音频/分段图像」
即可输出每条缝的细粒度诊断。核心判别（四类跳变各用一个探针区分）：

- 时间断层：E(k)=best_shift(段尾倒数第k帧, 段首).残差 曲线，k*>0 且 E(k*) << E(0)
  说明「时间轴往前倒 k* 帧反而更衔接」= 跳过了 k*-1 帧内容（尾切对齐差 token）；
- 位移瞬移：全平移搜索 (dx,dy) 后残差骤降 = 画面整体挪了几像素（运动不连续），
  与断层互斥判别（断层探针已用 best-shift 残差，不会被位移骗到）；
- 颜色/内容跳变：窗口 RGB 均值差 ΔE + 直方图分布差 + 结构 NCC
  （NCC 高=同一画面在漂移；NCC 低=内容真的换了）；
- 清晰度骤降：前后窗 Laplacian 方差对比（255 域，与桥帧门控同标定）。

音频通道：接缝两侧 RMS/dB、频谱质心、峰值爆音、样本级跳变。
拼装一致性：完整链接缝两帧 vs 分段原始帧逐像素比对（验证接缝处理是否生效
——潜空间精修/smoothstep 混合都会改动段首帧、主输出与分段是否同源），并核对
Σ段长 == 链帧数（帧数守恒）。

无分段输入时自动检测：全链相邻帧差找孤立峰（d > ratio×中位）定位疑似跳变点。

纯 torch 数学 + PIL 画对比图（ComfyUI 自带），无新依赖。
"""

import math
import os
import time

import torch
import torch.nn.functional as F

try:
    from . import metrics as _metrics
except ImportError:
    import metrics as _metrics

SEP = "─" * 74


def _gray(frame):
    """[H,W,3] 0-1 -> [H,W] 灰度。"""
    rgb = frame[..., :3].float()
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _down(frame, max_side=96):
    """[H,W,(3)] -> 长边 ≤ max_side 的灰度（位移搜索用，小图抗噪）。"""
    g = _gray(frame) if frame.shape[-1] >= 3 else frame.float()
    h, w = g.shape[-2:]
    scale = max_side / max(h, w)
    if scale < 1.0:
        g = F.interpolate(g[None, None], scale_factor=scale, mode="bilinear",
                          align_corners=False)[0, 0]
    return g


def _l1(a, b):
    return float((a.float() - b.float()).abs().mean())


def frame_diffs(video):
    """[N,H,W,3] -> 长度 N-1 的逐帧 L1 差 list（低显存：逐对计算不留中间张量）。"""
    return [ _l1(video[i], video[i - 1]) for i in range(1, video.shape[0]) ]


def locate_seams_from_segments(segs):
    """分段帧数 -> 接缝右侧全局帧下标 [c1, c2, ...]（缝在 c-1 与 c 之间）。"""
    out, acc = [], 0
    for s in segs:
        acc += int(s.shape[0])
        out.append(acc)
    return out[:-1] if len(out) > 1 else []


def auto_detect_seams(diffs, ratio, min_gap=3):
    """帧差曲线 -> 疑似跳变帧下标（d[i] 是 i->i+1 的差，返回右侧帧号）。"""
    if not diffs:
        return []
    ds = torch.tensor(diffs, dtype=torch.float64)
    med = float(ds.median())
    if med <= 1e-9:
        med = float(ds.mean()) or 1e-9
    hits = [i + 1 for i, d in enumerate(diffs) if d > ratio * med]
    merged = []
    for i in hits:                                   # 相邻 min_gap 帧内取最大
        if merged and i - merged[-1] < min_gap:
            if diffs[i - 1] > diffs[merged[-1] - 1]:
                merged[-1] = i
        else:
            merged.append(i)
    return merged


def best_shift(a_frame, b_frame, radius):
    """两帧 -> 最优整体平移及残差。返回 (dx, dy, e0, eb, err_map)。

    全平移暴力搜索（降采样 96px 灰度）：e0=零位移残差，eb=最优残差。
    eb << e0 且位移可观 => 跳变主要是「画面挪了」，不是内容换了。
    """
    a = _down(a_frame)
    b = _down(b_frame)
    h, w = a.shape
    e0 = float((a - b).abs().mean())
    best = (0, 0, e0)
    errs = {}
    for dy in range(-radius, radius + 1):
        ay0, ay1 = max(0, dy), min(h, h + dy)
        by0, by1 = max(0, -dy), min(h, h - dy)
        for dx in range(-radius, radius + 1):
            ax0, ax1 = max(0, dx), min(w, w + dx)
            bx0, bx1 = max(0, -dx), min(w, w - dx)
            da = a[ay0:ay1, ax0:ax1]
            db = b[by0:by1, bx0:bx1]
            e = float((da - db).abs().mean())
            errs[(dx, dy)] = e
            if e < best[2]:
                best = (dx, dy, e)
    # 返回「内容位移量」语义（对齐平移的相反数）：报告直读「画面挪了 +6px」
    return -best[0], -best[1], e0, best[2], errs


def jump_verdict(ratio, shift_fix, ncc_v, ratio_th):
    """时间断层（超前型：内容跳过若干帧）三角判别。

    单一信号都不可靠，组合才判：缝差达阈值（≈N 帧演化量）+ 平移消不掉
    （排除整体瞬移）+ NCC 高（排除镜头/内容切换）。倒带型（缝后重播过去）
    由 dup_probe 单独覆盖。返回 (是否断层, 估计跳过帧数)。
    """
    th = max(3.0, ratio_th)
    if ratio >= th and not shift_fix and ncc_v >= 0.5:
        return True, max(1, round(ratio) - 1)
    return False, 0


def dup_probe(video, c, kmax, radius):
    """重复探针：E(k) = best_shift(f[c-1], f[c+k]) 残差——缝后第 k 帧反而
    与缝前完全重合 => 同内容重播（卡顿/stutter）。k>=1 即算（stutter 常从
    缝后第 1 帧就开始回放），但要求残差 < 30%（近零重合）防误报。"""
    n = video.shape[0]
    errs = []
    for k in range(0, kmax + 1):
        b = video[c + k] if c + k < n else video[n - 1]
        _, _, _, eb, _ = best_shift(video[c - 1], b, radius)
        errs.append(eb)
    k_star = min(range(len(errs)), key=lambda i: errs[i])
    dup = (k_star >= 1 and errs[k_star] < 0.3 * errs[0]) if errs[0] > 1e-9 else False
    return errs, k_star, dup


def window_color(video, lo, hi):
    """[lo,hi) 帧窗口 -> (RGB 均值[3], RGB 标准差[3])。"""
    w = video[lo:hi, ..., :3].float().flatten(0, 2)   # [N*H*W, 3]
    return w.mean(0).tolist(), w.std(0, unbiased=False).tolist()


def hist_distance(a_frame, b_frame):
    """两帧 RGB 直方图分布差（每通道 32bin L1 的均值，0=同分布 1=完全不同）。"""
    out = []
    for ch in range(3):
        ha = torch.histc(a_frame[..., ch].float(), bins=32, min=0.0, max=1.0)
        hb = torch.histc(b_frame[..., ch].float(), bins=32, min=0.0, max=1.0)
        ha = ha / (ha.sum() + 1e-9)
        hb = hb / (hb.sum() + 1e-9)
        out.append(float((ha - hb).abs().sum() / 2.0))
    return sum(out) / 3.0


def sharpness(video, lo, hi):
    """窗口平均清晰度（Laplacian 方差，255 域，与桥帧门控 qc 同标定）。"""
    tot, n = 0.0, 0
    for i in range(lo, hi):
        gray = _gray(video[i])[None, None] * 255.0
        lap_k = gray.new_tensor([[0, 1, 0], [1, -4, 1], [0, 1, 0]]).view(1, 1, 3, 3)
        lap = F.conv2d(gray, lap_k, padding=1)
        tot += float(lap.var(unbiased=False))
        n += 1
    return tot / max(n, 1)


def ncc(a_frame, b_frame):
    """去均值归一化互相关（降采样 256 灰度）：1=同一画面，0=无关内容。"""
    a = _down(a_frame, 256).flatten()
    b = _down(b_frame, 256).flatten()
    a = a - a.mean()
    b = b - b.mean()
    denom = float(a.norm() * b.norm())
    return float((a * b).sum() / denom) if denom > 1e-9 else 0.0


def audio_probe(wav, sr, sample_i, win_s=0.5):
    """接缝两侧音频体检。wav [C,T] 0-1；返回 dict（缺数据处为 None）。"""
    out = {"rms_db": None, "peak": None, "zcr": None, "centroid": None,
           "sample_jump": None, "clip_ratio": None}
    if wav is None or wav.numel() < 64 or not sr:
        return out
    mono = wav.float().mean(0).cpu()                       # [T]
    n = max(256, int(sr * win_s))
    pre = mono[max(0, sample_i - n):sample_i]
    post = mono[sample_i:min(mono.shape[0], sample_i + n)]
    if pre.numel() < 256 or post.numel() < 256:
        return out
    rp = float(pre.pow(2).mean().sqrt())
    rq = float(post.pow(2).mean().sqrt())
    if rp > 1e-6 and rq > 1e-6:
        out["rms_db"] = 20.0 * math.log10(rq / rp)
    both = torch.cat([pre, post])
    out["peak"] = float(both.abs().max())
    out["clip_ratio"] = float((both.abs() > 0.99).float().mean())

    def _zcr(x):
        s = torch.sign(x)
        return float((s[1:] != s[:-1]).float().mean())

    out["zcr"] = (_zcr(pre) + _zcr(post)) / 2.0

    def _centroid(x):
        w = torch.hann_window(x.numel())
        spec = torch.fft.rfft(x * w).abs()
        freqs = torch.fft.rfftfreq(x.numel(), 1.0 / sr)
        return float((spec * freqs).sum() / (spec.sum() + 1e-9))

    out["centroid"] = (_centroid(pre), _centroid(post))    # (前Hz, 后Hz)
    edge = mono[max(0, sample_i - 64):sample_i + 64]
    if edge.numel() >= 2:
        out["sample_jump"] = float((edge[1:] - edge[:-1]).abs().max())
    return out


def _fmt_row(vals, peak_idx):
    """差值曲线打印，峰位加 ▮▮ 标记。"""
    parts = []
    for i, v in enumerate(vals):
        s = f"{v:.3f}"
        parts.append(f"▮{s}▮" if i == peak_idx else s)
    return " ".join(parts)


def _tc(frame, fps):
    s = frame / max(fps, 1)
    if s < 60:
        return f"{s:.2f}s"
    return f"{int(s // 60):d}m{s % 60:04.1f}s"


def diagnose_seam(video, c, idx, diffs, median_d, window, radius, kmax,
                  fps, ratio_th=3.0, wav=None, sr=None, seg_pair=None):
    """单接缝全量体检。seg_pair=(段尾帧, 段首帧) 原始帧（拼装一致性用）。

    返回 dict：帧号/各项指标/findings 判定列表/建议列表。
    """
    n = video.shape[0]
    lo_w = max(0, c - window)
    hi_w = min(n, c + window)
    d_peak = diffs[c - 1]                                   # 缝差（c-1 -> c）
    ratio = d_peak / median_d if median_d > 1e-9 else 0.0
    curve = diffs[max(0, c - 1 - (window - 1)):min(len(diffs), c - 1 + window)]
    peak_off = min(window - 1, len(curve) - 1)

    mean_a, std_a = window_color(video, lo_w, c)
    mean_b, std_b = window_color(video, c, hi_w)
    d_e = math.sqrt(sum((a - b) ** 2 for a, b in zip(mean_a, mean_b)))
    hist_d = hist_distance(video[c - 1], video[c])
    sharp_a = sharpness(video, lo_w, c)
    sharp_b = sharpness(video, c, hi_w)
    sharp_drop = (sharp_a - sharp_b) / sharp_a if sharp_a > 1e-9 else 0.0
    ncc_v = ncc(video[c - 1], video[c])
    dx, dy, e0, eb, _ = best_shift(video[c - 1], video[c], radius)
    shift_fix = eb < 0.4 * e0 and max(abs(dx), abs(dy)) >= 2
    is_jump, jump_frames = jump_verdict(ratio, shift_fix, ncc_v, ratio_th)

    dup_errs, k_dup, is_dup = dup_probe(video, c, kmax, radius)

    sample_i = int(round(c / max(fps, 1) * (sr or 0))) if sr else 0
    audio = audio_probe(wav, sr, sample_i) if wav is not None else {}

    join = None
    if seg_pair is not None:
        tail, head = seg_pair
        join = (float((video[c - 1].float() - tail.float()).abs().max()),
                float((video[c].float() - head.float()).abs().max()))

    findings, hints = [], []
    if is_jump:
        findings.append(f"时间断层：缝差 {d_peak:.3f} ≈ {ratio:.1f} 帧演化量"
                        f"（中位 {median_d:.4f}），平移补不掉且同场景延续"
                        f" → 疑似跳过约 {jump_frames} 帧内容")
        hints.append("桥锚定末端与段输出末端没对齐（尾切差 token），接缝处时间轴缺帧")
    if is_dup:
        findings.append(f"内容重复：缝后第 {k_dup} 帧与缝前最像"
                        f"（残差 {dup_errs[k_dup]:.3f}）≈ 同内容回退重播")
        hints.append("疑似同帧/相邻帧重复进入拼接")
    if shift_fix:
        findings.append(f"位移瞬移：整体平移 ({dx:+d},{dy:+d}px) 后残差 "
                        f"{eb:.3f}（零位移 {e0:.3f}，↓{1 - eb / max(e0, 1e-9):.0%}）")
        hints.append("运动矢量断裂：考虑加大引导帧数 / 0.2 锚定加噪 / 重摇该段")
    if d_e >= 0.05:
        findings.append(f"颜色漂移：窗口 RGB 均值差 ΔE={d_e:.3f}")
        hints.append("接缝两侧色调不一致（解码漂移或内容本身换色）")
    if ncc_v < 0.5:
        findings.append(f"内容切换：结构相关 NCC={ncc_v:.2f}（缝两侧不是同一画面）")
        hints.append("若是镜头切换属正常；预期连续则该段生成偏了，建议重摇")
    if sharp_drop >= 0.4:
        findings.append(f"清晰度骤降：{sharp_a:.0f} → {sharp_b:.0f}（↓{sharp_drop:.0%}）")
        hints.append("缝后明显变糊（长链漂移或该段质量差）")
    if audio.get("rms_db") is not None and abs(audio["rms_db"]) >= 6:
        findings.append(f"响度跳变：{audio['rms_db']:+.1f}dB"
                        f"（{'缝后更响' if audio['rms_db'] > 0 else '缝后更轻'}）")
    if audio.get("peak") is not None and audio["peak"] > 0.99:
        findings.append(f"疑似爆音：峰值样本 {audio['peak']:.3f}（削波）")

    return {
        "idx": idx, "c": c, "ratio": ratio, "d_peak": d_peak, "curve": curve,
        "peak_off": peak_off, "mean_a": mean_a, "mean_b": mean_b, "d_e": d_e,
        "hist_d": hist_d, "sharp_a": sharp_a, "sharp_b": sharp_b,
        "sharp_drop": sharp_drop, "ncc": ncc_v, "dx": dx, "dy": dy,
        "e0": e0, "eb": eb, "shift_fix": shift_fix,
        "is_jump": is_jump, "jump_frames": jump_frames,
        "dup_errs": dup_errs, "k_dup": k_dup, "is_dup": is_dup,
        "audio": audio, "join": join, "findings": findings, "hints": hints,
    }


def _fmt_rgb(t):
    return "(" + ",".join(f"{v:.2f}" for v in t) + ")"


def render_seam_section(m):
    """单接缝 dict -> 报告文本段。"""
    lines = [f"── 接缝 #{m['idx']}：全局帧 {m['c'] - 1}→{m['c']}（{_tc(m['c'] - 1, m['fps'])}）──"]
    lines.append(f"[曲线] 缝±帧差: {_fmt_row(m['curve'], m['peak_off'])}"
                 f"  （▮=缝位）")
    lines.append(f"[强度] 缝差 {m['d_peak']:.3f} = 全链中位的 {m['ratio']:.1f} 倍"
                 + ("  → 跳变 ⚠" if m["ratio"] >= m["ratio_th"] else "  → 正常"))
    lines.append(f"[颜色] 前窗 {_fmt_rgb(m['mean_a'])} → 后窗 {_fmt_rgb(m['mean_b'])}"
                 f"，ΔE={m['d_e']:.3f}；分布差 {m['hist_d']:.2f}")
    lines.append(f"[清晰] {m['sharp_a']:.0f} → {m['sharp_b']:.0f}"
                 + (f"（↓{m['sharp_drop']:.0%}）" if m["sharp_drop"] >= 0.4 else ""))
    lines.append(f"[结构] NCC={m['ncc']:.2f}"
                 + ("（同一画面延续）" if m["ncc"] >= 0.5 else "（内容切换）"))
    lines.append(f"[位移] 零位移残差 {m['e0']:.3f} → 平移({m['dx']:+d},{m['dy']:+d}) "
                 f"残差 {m['eb']:.3f}"
                 + ("  → 瞬移型" if m["shift_fix"] else ""))
    lines.append(f"[断层] 缝差 ≈ {m['ratio']:.1f} 帧演化量；平移"
                 + ("可消" if m["shift_fix"] else "不可消")
                 + f"、NCC {m['ncc']:.2f}"
                 + (f" → 疑似跳过约 {m['jump_frames']} 帧" if m["is_jump"] else ""))
    if m["is_dup"]:
        lines.append(f"[重复] 缝后第 {m['k_dup']} 帧最像缝前"
                     f"（残差 {m['dup_errs'][m['k_dup']]:.3f}）→ 疑似内容回退重播")
    a = m["audio"]
    if a and a.get("rms_db") is not None:
        cen = a.get("centroid") or (0, 0)
        lines.append(f"[音频] RMS {a['rms_db']:+.1f}dB；峰值 {a['peak']:.2f}"
                     f"（削波 {a['clip_ratio']:.1%}）；质心 {cen[0] / 1000:.1f}k→{cen[1] / 1000:.1f}k Hz；"
                     f"缝点样本跳变 {a.get('sample_jump') or 0:.2f}")
    elif a:
        lines.append("[音频] 样本不足，未分析")
    if m["join"] is not None:
        j0, j1 = m["join"]
        lines.append(f"[拼装] 链缝前帧 vs 段尾帧 Δmax={j0:.3f}"
                     f"（{'一致' if j0 < 1e-4 else '不一致!'}）；"
                     f"链缝后帧 vs 段首帧 Δmax={j1:.3f}"
                     f"（接缝处理{'生效' if j1 > 1e-4 else '未见改动'}）")
    if m.get("z"):
        ztxt = _metrics.fmt_seam_z(m["z"])
        if "无可用" not in ztxt:
            lines.append(f"[基准] {ztxt}（|z|<2 合格；对照段内 P95 基线）")
    if m["findings"]:
        for i, f in enumerate(m["findings"], 1):
            lines.append(f"[判定{i}] {f}")
        for h in dict.fromkeys(m["hints"]):               # 去重保序
            lines.append(f"       ↳ {h}")
    else:
        lines.append("[判定] 各项指标均在正常范围")
    return "\n".join(lines)


def build_report(video, wav, sr, fps, seams, segs, used_seg, median_d, diffs, metrics,
                 window, radius, kmax, ratio_th):
    """汇总各接缝体检 -> 完整报告文本。"""
    n = int(video.shape[0])
    h, w = video.shape[1:3]
    if used_seg:
        mode = f"分段定位（{len(segs)} 段 / {len(seams)} 个接缝）"
    elif segs:
        seg_n = [int(s.shape[0]) for s in segs]
        mode = (f"分段帧数不守恒（Σ段 {sum(seg_n)} ≠ 链 {n}），已转自动检测"
                f"（{len(seams)} 个疑似点）—— 主输出与分段疑似不同源/丢段 ⚠")
    else:
        mode = f"自动检测（阈值 {ratio_th:.1f}×中位，{len(seams)} 个疑似点）"
    lines = ["══ H3 接缝体检报告 ══", SEP,
             f"模式：{mode}",
             f"链信息：{n} 帧 {w}x{h} @ {fps}fps ≈ {n / max(fps, 1):.1f}s"
             + (f"；音频 {wav.shape[-1] / sr:.1f}s @{sr}Hz" if wav is not None and sr else ""),
             f"全链相邻帧差：中位 {median_d:.4f} / P90 "
             f"{float(torch.tensor(diffs).float().quantile(0.9)):.4f} / "
             f"最大 {max(diffs):.4f}（帧 {diffs.index(max(diffs)) + 1}）"]
    if used_seg:
        seg_n = [int(s.shape[0]) for s in segs]
        lines.append(f"帧数守恒：{' + '.join(map(str, seg_n))} = {sum(seg_n)} ✓")
    lines.append(SEP)
    for m in metrics:
        m["fps"] = fps
        m["ratio_th"] = ratio_th
        lines.append(render_seam_section(m))
        lines.append("")
    lines.append(SEP)
    lines.append("── 总结 ──")
    ranked = sorted(metrics, key=lambda m: -m["ratio"])
    if ranked:
        lines.append("接缝强度排名：" + " > ".join(
            f"#{m['idx']}({m['ratio']:.1f}x)" for m in ranked[:8]))
        types = {"时间断层": 0, "内容重复": 0, "位移瞬移": 0, "颜色漂移": 0,
                 "内容切换": 0, "清晰度骤降": 0}
        for m in metrics:
            for key, hit in (("时间断层", m["is_jump"]), ("内容重复", m["is_dup"]),
                             ("位移瞬移", m["shift_fix"]), ("颜色漂移", m["d_e"] >= 0.05),
                             ("内容切换", m["ncc"] < 0.5), ("清晰度骤降", m["sharp_drop"] >= 0.4)):
                if hit:
                    types[key] += 1
        lines.append("类型分布：" + "｜".join(f"{k} {v}" for k, v in types.items() if v)
                     if any(types.values()) else "类型分布：全部正常")
        fz = [m["z"]["flow_z"] for m in metrics if m.get("z") and m["z"].get("flow_z") is not None]
        if fz:
            lines.append(f"基准汇总：光流 z 均值 {sum(fz) / len(fz):+.1f} / 最差 {max(fz):+.1f}σ"
                         f"（|z|<2 合格；与存档 manifest.seam_metrics 同标定）")
        worst = ranked[0]
        lines.append(f"最差接缝：#{worst['idx']}（帧 {worst['c'] - 1}→{worst['c']}，"
                     f"{_tc(worst['c'] - 1, fps)}）")
        lines.append("排查建议：把本报告完整发回给开发，重点带上最差接缝的"
                     "[曲线][位移][断层] 三行数值。")
    else:
        lines.append("未检出接缝/跳变点：视频只有一段，或相邻帧差全在阈值内。"
                     "若仍观察到跳变，把「跳变阈值倍率」降到 2.0 再跑一次。")
    return "\n".join(lines)


_JET = [(0.0, (0, 0, 143)), (0.25, (0, 110, 255)), (0.5, (0, 230, 118)),
        (0.75, (255, 180, 0)), (1.0, (160, 0, 0))]


def _jet_map(gray01):
    """[H,W] 0-1 灰度 -> [H,W,3] jet 伪彩 uint8 tensor。"""
    import torch as T
    v = gray01.clamp(0, 1)
    out = T.zeros(v.shape[0], v.shape[1], 3, dtype=T.uint8)
    for (p0, c0), (p1, c1) in zip(_JET, _JET[1:]):
        m = (v >= p0) & (v <= p1) if p1 == 1.0 else (v >= p0) & (v < p1)
        t = ((v - p0) / (p1 - p0 + 1e-9)).clamp(0, 1)
        for ch in range(3):
            val = c0[ch] + (c1[ch] - c0[ch]) * t
            out[..., ch] = T.where(m, (val * 255).to(T.uint8), out[..., ch])
    return out


def build_gallery(video, metrics, max_rows=12):
    """每接缝一行（缝前帧 | 缝后帧 | 残差伪彩×4）-> 单张体检图 tensor [H,W,3]。

    PIL 画文字标签（接缝号/帧号/强度），无接缝时返回 None。
    """
    if not metrics:
        return None
    import numpy
    from PIL import Image, ImageDraw

    thumb_h = 150
    rows = []
    for m in metrics[:max_rows]:
        a = video[m["c"] - 1]
        b = video[m["c"]]

        def to_img(t):
            arr = (t[..., :3].float().clamp(0, 1).cpu().numpy() * 255).astype("uint8")
            img = Image.fromarray(arr)
            w = max(1, round(img.width * thumb_h / img.height))
            return img.resize((w, thumb_h), Image.LANCZOS)

        diff = (b[..., :3].float() - a[..., :3].float()).abs().mean(-1)
        diff = _jet_map((diff * 4.0).clamp(0, 1))          # 残差×4 放大弱差异
        dimg = to_img(diff.float() / 255.0)
        ia, ib = to_img(a), to_img(b)
        rows.append((m, ia, ib, dimg))

    label_w = 210
    row_gap, pad = 6, 8
    img_w = max(label_w + pad + sum(r[1].width + r[2].width + r[3].width + 18 for r in rows)
                + pad, 640)
    total_h = pad + sum(thumb_h + row_gap for _ in rows) + pad
    canvas = Image.new("RGB", (img_w, total_h), (24, 24, 28))
    draw = ImageDraw.Draw(canvas)
    y = pad
    for m, ia, ib, dimg in rows:
        x = pad
        tag = (f"#{m['idx']}  帧{m['c'] - 1}->{m['c']}  {m['ratio']:.1f}x\n"
               f"跳移({m['dx']:+d},{m['dy']:+d}) NCC {m['ncc']:.2f}\n"
               + ("断层↓" if m["is_jump"] else "")
               + ("瞬移↓" if m["shift_fix"] else "")
               + ("正常" if not (m["is_jump"] or m["shift_fix"]) else ""))
        draw.multiline_text((x + 4, y + 8), tag.strip(), fill=(230, 230, 235))
        x = label_w
        canvas.paste(ia, (x, y))
        canvas.paste(ib, (x + ia.width + 6, y))
        canvas.paste(dimg, (x + ia.width + ib.width + 12, y))
        y += thumb_h + row_gap
    arr = numpy.array(canvas).astype("float32") / 255.0
    return torch.from_numpy(arr)[None]                     # [1,H,W,3]


def save_report(report, subdir="h3_seam_doctor"):
    """报告存盘 output/<subdir>/report_时间戳.txt -> 相对路径；无 ComfyUI 环境返回 None。"""
    try:
        import folder_paths
        root = folder_paths.get_output_directory()
    except Exception:
        return None
    try:
        d = os.path.join(root, subdir)
        os.makedirs(d, exist_ok=True)
        name = f"report_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(report)
        return os.path.join(subdir, name)
    except Exception:
        return None


def analyze(video, wav, sr, fps, segs, window, radius, kmax, ratio_th):
    """体检入口：定位接缝 -> 逐缝诊断 -> (报告, 体检图, metrics)。"""
    video = video.detach()
    diffs = frame_diffs(video)
    if not diffs:
        return "接缝体检：视频只有 1 帧，无接缝可查。", None, []
    med = float(torch.tensor(diffs).float().median())
    med = med if med > 1e-9 else (float(torch.tensor(diffs).mean()) or 1e-9)

    seams, seg_pair_map, used_seg = [], {}, False
    if segs and sum(int(s.shape[0]) for s in segs) == int(video.shape[0]):
        seams = locate_seams_from_segments(segs)
        for i, c in enumerate(seams):
            seg_pair_map[c] = (segs[i][segs[i].shape[0] - 1], segs[i + 1][0])
        used_seg = len(seams) > 0
    if not used_seg:
        seams = auto_detect_seams(diffs, ratio_th)

    metrics = []
    for idx, c in enumerate(seams, 1):
        if not (1 <= c < video.shape[0]):
            continue
        m = diagnose_seam(video, c, idx, diffs, med, window, radius, kmax,
                          fps, ratio_th, wav, sr, seg_pair_map.get(c))
        m["fps"] = fps
        m["ratio_th"] = ratio_th
        metrics.append(m)
    # 五维 z-score 基准（光流/加速度/LPIPS/嵌入/相机）：与采样器 manifest
    # seam_metrics 同源同标定，A/B 对比时两处数字可直接对照
    try:
        zres = _metrics.evaluate_seams(video, seams)
        zmap = {r["c"]: r for r in zres.get("seams", [])}
    except Exception:
        zmap = {}
    for m in metrics:
        m["z"] = zmap.get(m["c"])
    report = build_report(video, wav, sr, fps, seams, segs, used_seg, med, diffs,
                          metrics, window, radius, kmax, ratio_th)
    gallery = build_gallery(video, metrics)
    return report, gallery, metrics


try:
    from comfy_api.latest import io

    class H3SeamDoctor(io.ComfyNode):
        """接缝体检节点：接采样器 图像/音频/分段图像，输出细粒度诊断报告。"""

        @classmethod
        def define_schema(cls):
            return io.Schema(
                node_id="H3SeamDoctor",
                display_name="H3 Seam Doctor (接缝体检)",
                category="MiniMaxH3",
                description="只测不治：逐接缝量化跳变根因（时间断层/位移瞬移/颜色漂移/"
                            "内容切换/清晰度/音频爆音），输出详尽报告 + 残差伪彩对比图。"
                            "接采样器的 图像/音频/帧率，可选接「分段图像」精确定位接缝。",
                is_output_node=True,
                inputs=[
                    io.Image.Input("图像", tooltip="完整链可见帧（采样器「图像」输出）"),
                    io.Audio.Input("音频", tooltip="完整链音轨（采样器「音频」输出）"),
                    io.Image.Input("分段图像", optional=True,
                                   tooltip="接采样器「分段图像」输出：精确定位每个接缝并"
                                           "校验拼装一致性；不接则自动检测跳变点"),
                    io.Int.Input("帧率", default=24, min=1, max=120),
                    io.Int.Input("窗口帧数", default=8, min=2, max=32,
                                 tooltip="每个接缝前后各统计多少帧（颜色/清晰度/曲线窗口）"),
                    io.Int.Input("位移搜索半径", default=16, min=0, max=48,
                                 tooltip="平移搜索 ±N 像素（降采样图上）：检测画面整体挪动"),
                    io.Int.Input("断层探测深度", default=8, min=0, max=32,
                                 tooltip="时间断层探针最多往回试多少帧"),
                    io.Float.Input("跳变阈值倍率", default=3.0, min=1.5, max=20.0, step=0.5,
                                   tooltip="自动检测模式下，相邻帧差超过 中位×N 判为疑似接缝"),
                ],
                outputs=[
                    io.String.Output("报告", tooltip="右键预览：每条缝的曲线/颜色/位移/断层/音频诊断"),
                    io.Image.Output("对比图", tooltip="每接缝一行：缝前帧|缝后帧|残差伪彩(×4)"),
                ],
            )

        @classmethod
        def IS_CHANGED(cls, **kwargs):
            return float("nan")    # 诊断工具：同输入重跑也强制重新体检

        @classmethod
        def execute(cls, 图像, 音频, 分段图像=None, 帧率=24, 窗口帧数=8,
                    位移搜索半径=16, 断层探测深度=8, 跳变阈值倍率=3.0):
            def scalar(v, dft):
                return v[0] if isinstance(v, list) and v else (v if v is not None else dft)

            fps = int(scalar(帧率, 24))
            segs_raw = 分段图像 if isinstance(分段图像, (list, tuple)) else (
                [] if 分段图像 is None else [分段图像])
            segs = [s for s in segs_raw
                    if s is not None and getattr(s, "shape", None) and s.shape[0] > 0]
            wav = 音频.get("waveform") if isinstance(音频, dict) else 音频
            sr = int(音频.get("sample_rate", 0)) if isinstance(音频, dict) else 0
            report, gallery, _ = analyze(
                图像, wav, sr, fps, segs,
                int(scalar(窗口帧数, 8)), int(scalar(位移搜索半径, 16)),
                int(scalar(断层探测深度, 8)), float(scalar(跳变阈值倍率, 3.0)))
            rel = save_report(report)
            if rel:
                report = (f"{report}\n\n报告已存盘：{rel}"
                          f"\n（ComfyUI 的 output 目录下，记事本打开即读）")
                print(f"[H3SeamDoctor] 接缝体检报告已存盘：{rel}")
            return io.NodeOutput(report, gallery if gallery is not None else 图像[:1])
except ImportError:
    pass                                              # 无 ComfyUI 的单测环境：纯逻辑照常可用
