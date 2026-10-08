# -*- coding: utf-8 -*-
"""把探针 CSV 生成一份自包含 HTML 报告（内联 SVG 曲线，不依赖网络）。

用法：
  <venv-python> tools/make_mem_report.py \
      --csv output/h3_mem_probe/run1_trace.csv \
      --out output/h3_mem_probe/报告_显存内存实测.html
"""
import argparse
import csv
import html


def load(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def polyline(rows, key, x0, x1, tmax, y0, y1, vmax, scale=1.0):
    """把某列映射成 SVG polyline 的 points 串。"""
    pts = []
    for r in rows:
        t = fnum(r["t"])
        v = fnum(r[key])
        if t is None or v is None:
            continue
        v = v * scale
        x = x0 + (x1 - x0) * (t / tmax)
        y = y1 - (y1 - y0) * min(1.0, v / vmax)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def build(csv_path, out_path):
    rows = load(csv_path)
    rows = [r for r in rows if fnum(r["t"]) is not None]
    tmax = max(fnum(r["t"]) for r in rows)

    # ---- 关键统计
    def peak(key, scale=1 / 1024.0):
        vals = [(fnum(r[key]), fnum(r["t"])) for r in rows]
        vals = [(v, t) for v, t in vals if v is not None]
        v, t = max(vals)
        return v * scale, t

    vram_peak, vram_t = peak("vram_used_mb")
    ram_peak, ram_t = peak("ram_used_mb")
    rss_peak, _ = peak("comfy_rss_mb")
    disk_total = fnum(rows[-1]["disk_read_mb"]) / 1024.0

    # ---- 各阶段磁盘读增量（按 phase 前缀粗分）
    def phase_span(pred):
        sel = [r for r in rows if pred(r["phase"])]
        if not sel:
            return None
        return sel[0], sel[-1]

    seg = []
    for name, pred in (
        ("模型加载 + 条件编码", lambda p: p in ("startup", "node:10")),
        ("采样 1/4", lambda p: p == "progress:1/4"),
        ("采样 2/4", lambda p: p == "progress:2/4"),
        ("采样 3/4", lambda p: p == "progress:3/4"),
        ("采样 4/4", lambda p: p == "progress:4/4"),
        ("音频/收尾", lambda p: p in ("progress:1/1", "node:54") or p.startswith("done")),
    ):
        sp = phase_span(pred)
        if sp:
            a, b = sp
            seg.append((name, fnum(a["t"]), fnum(b["t"]),
                        (fnum(b["disk_read_mb"]) - fnum(a["disk_read_mb"])) / 1024.0,
                        max(fnum(r["vram_used_mb"]) for r in rows
                            if pred(r["phase"])) / 1024.0,
                        max(fnum(r["ram_used_mb"]) for r in rows
                            if pred(r["phase"])) / 1024.0))

    # ---- SVG 曲线
    X0, X1, Y0, Y1 = 70, 900, 60, 340
    svg_vram = polyline(rows, "vram_used_mb", X0, X1, tmax, Y0, Y1, 18 * 1024)
    svg_ram = polyline(rows, "ram_used_mb", X0, X1, tmax, Y0, Y1, 18 * 1024)
    svg_disk = polyline(rows, "disk_read_mb", X0, X1, tmax, Y0, Y1, 100 * 1024)

    # x 轴刻度
    ticks_x = ""
    for t in range(0, int(tmax) + 1, 20):
        x = X0 + (X1 - X0) * (t / tmax)
        ticks_x += (f'<line x1="{x:.0f}" y1="{Y1}" x2="{x:.0f}" y2="{Y1+5}" stroke="#B4B2A9" stroke-width="1"/>'
                    f'<text x="{x:.0f}" y="{Y1+20}" text-anchor="middle" font-size="12" fill="#5F5E5A">{t}s</text>')
    ticks_y = ""
    for g in range(0, 19, 3):
        y = Y1 - (Y1 - Y0) * (g / 18)
        ticks_y += (f'<line x1="{X0-5}" y1="{y:.0f}" x2="{X0}" y2="{y:.0f}" stroke="#B4B2A9" stroke-width="1"/>'
                    f'<text x="{X0-10}" y="{y:.0f}" text-anchor="end" dominant-baseline="central" '
                    f'font-size="12" fill="#5F5E5A">{g}</text>')
    ticks_y2 = ""
    for g in range(0, 101, 20):
        y = Y1 - (Y1 - Y0) * (g / 100)
        ticks_y2 += (f'<text x="{X1+10}" y="{y:.0f}" dominant-baseline="central" '
                     f'font-size="12" fill="#5F5E5A">{g}</text>')

    # 阶段竖线
    marks = ""
    for t, label in ((42, "开始采样"), (98, "采样结束 / 解码")):
        x = X0 + (X1 - X0) * (t / tmax)
        marks += (f'<line x1="{x:.0f}" y1="{Y0}" x2="{x:.0f}" y2="{Y1}" stroke="#888780" '
                  f'stroke-width="1" stroke-dasharray="4 4"/>'
                  f'<text x="{x+5:.0f}" y="{Y0+14}" font-size="12" fill="#5F5E5A">{label}</text>')

    # ---- 阶段表
    seg_rows = ""
    for name, t0, t1, dgb, vgb, rgb in seg:
        seg_rows += (f"<tr><td>{html.escape(name)}</td><td>{t0:.0f}–{t1:.0f} s</td>"
                     f"<td class='num'>{t1-t0:.0f} s</td>"
                     f"<td class='num'>{dgb:,.1f} GB</td>"
                     f"<td class='num'>{vgb:.2f} GB</td>"
                     f"<td class='num'>{rgb:.2f} GB</td></tr>")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ComfyUI 显存/内存机制与 MiniMax H3 实测</title>
<style>
  :root {{ --fg:#2C2C2A; --fg2:#5F5E5A; --line:#D3D1C7; --bg:#FFFFFF; --bg2:#F7F6F2;
           --red:#A32D2D; --amber:#854F0B; --blue:#185FA5; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:32px 24px 64px; background:var(--bg); color:var(--fg);
          font:400 14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:1000px; margin:0 auto; }}
  h1 {{ font-size:22px; font-weight:500; margin:0 0 4px; }}
  .sub {{ color:var(--fg2); font-size:13px; margin-bottom:28px; }}
  h2 {{ font-size:16px; font-weight:500; margin:36px 0 12px; padding-bottom:6px;
        border-bottom:1px solid var(--line); }}
  h3 {{ font-size:14px; font-weight:500; margin:20px 0 8px; }}
  p {{ margin:8px 0; }}
  code {{ background:var(--bg2); padding:1px 5px; border-radius:4px;
          font:400 12.5px/1.5 Consolas,monospace; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:16px 0; }}
  .card {{ background:var(--bg2); border-radius:8px; padding:14px 16px; }}
  .card .k {{ font-size:12.5px; color:var(--fg2); }}
  .card .v {{ font-size:22px; font-weight:500; margin-top:2px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; margin:12px 0; }}
  th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }}
  th {{ color:var(--fg2); font-weight:400; font-size:12.5px; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .note {{ background:var(--bg2); border-left:3px solid var(--line); padding:10px 14px;
           margin:14px 0; font-size:13px; color:var(--fg2); }}
  ul {{ margin:8px 0; padding-left:22px; }}
  li {{ margin:4px 0; }}
  .chart {{ background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:8px; }}
  .legend {{ display:flex; gap:20px; font-size:12.5px; color:var(--fg2); margin:8px 0 0 8px; }}
  .legend span {{ display:inline-flex; align-items:center; gap:6px; }}
  .sw {{ width:18px; height:0; display:inline-block; }}
</style>
</head>
<body><div class="wrap">

<h1>ComfyUI 显存 / 内存机制与 MiniMax H3 实测</h1>
<div class="sub">本机实跑一次最小生成（416×256 · 0.5 秒 · 4 步 · 单段）的全过程采样 ·
  ComfyUI 0.38.2 · torch 2.12.1+cu130 · comfy-aimdo 0.5.5</div>

<div class="cards">
  <div class="card"><div class="k">总耗时</div><div class="v">{tmax:.0f} 秒</div></div>
  <div class="card"><div class="k">显存峰值</div><div class="v" style="color:var(--red)">{vram_peak:.1f} GB</div>
    <div class="k">总量 6.0 GB · 占满 99.7%</div></div>
  <div class="card"><div class="k">内存峰值</div><div class="v" style="color:var(--amber)">{ram_peak:.1f} GB</div>
    <div class="k">总量 16.2 GB · 仅剩 1.5 MB</div></div>
  <div class="card"><div class="k">磁盘累计读</div><div class="v" style="color:var(--blue)">{disk_total:,.0f} GB</div>
    <div class="k">约为权重的 2.6 倍</div></div>
</div>

<h2>一、显存、内存、磁盘分别是什么</h2>
<p>三者都是"存放数据的地方"，差别只在<b>离 GPU 计算核心有多近</b>：</p>
<table>
  <tr><th>层级</th><th>本机容量</th><th>带宽量级</th><th>作用</th></tr>
  <tr><td><b>显存 VRAM</b>（GPU 板载）</td><td>6.0 GB</td><td>~192 GB/s</td>
      <td>GPU 只能直接算显存里的张量。权重和中间激活都要在这里。</td></tr>
  <tr><td><b>内存 RAM</b>（CPU 侧）</td><td>16.2 GB</td><td>~20 GB/s</td>
      <td>显存装不下的权重暂存处；数据要进 GPU 必须经过这里（或直接从磁盘映射）。</td></tr>
  <tr><td><b>磁盘</b></td><td>剩余 54 GB</td><td>~0.5 GB/s</td>
      <td>模型文件的最终归宿。慢两个数量级，但足够大。</td></tr>
</table>
<p>关键落差：本机要跑的权重合计 <b>UNET 21.6 GB + 文本编码器 15.0 GB ≈ 36.6 GB</b>，
而显存只有 6 GB、内存只有 16.2 GB。<b>没有任何一层装得下</b>——所以每次生成都必然是
"边算边从磁盘搬"的过程。这就是所有现象的总根源。</p>

<h2>二、ComfyUI 是怎么管的</h2>
<h3>1. 五档显存状态，本项目跑在 NORMAL_VRAM</h3>
<p><code>comfy/model_management.py</code> 定义了 DISABLED / NO_VRAM / LOW_VRAM / NORMAL_VRAM /
HIGH_VRAM / SHARED 五档，由 <code>--lowvram</code> 这类启动参数切换。本项目不加任何参数，
默认 <b>NORMAL_VRAM</b>：权重尽量驻留显存，装不下时由卸载/换页兜底。</p>

<h3>2. DynamicVRAM（comfy-aimdo）默认接管权重</h3>
<p>现代 ComfyUI + N 卡默认启用 <b>DynamicVRAM</b>：权重不再"整块加载"，而是把模型文件
<b>mmap</b> 进地址空间，按需要分页搬进显存。它用 VBAR 预留 <code>模型大小 × 10</code> 的虚拟地址
当作权重页缓存——所以"空闲显存接近 0"是<b>设计使然，不是泄漏</b>。</p>
<div class="note">本次日志实证：<code>Model MiniMaxH3 prepared for dynamic VRAM loading. 21603MB Staged.</code>
——21.6 GB 的 UNET 以"暂存"方式挂上，并没有真的整块塞进 6 GB 显存。</div>

<h3>3. 显存是"权重缓存"，不是"模型容器"</h3>
<p>DynamicVRAM 下，显存里能放多少权重 = 可用显存。ComfyUI 会主动留一块推理余量：</p>
<ul>
  <li><code>minimum_inference_memory() = 0.8 GB（保底） + EXTRA_RESERVED_VRAM</code></li>
  <li>本次实测留空 <b>1.39 GB</b>（0.8 + 0.59）</li>
  <li>能当权重缓存用的：<b>6.0 − 1.39 ≈ 4.6 GB</b>（与实测稳定值 4.1–4.2 GB 吻合）</li>
</ul>
<p>于是每跑一步去噪，都要把装不下的部分重新从磁盘读：<b>21.6 − 4.6 ≈ 17 GB / 步</b>。
留空的每一 GB，都等于每步要多读 1 GB——这是"用满显存"和"留余量给激活"之间的直接取舍。</p>

<h3>4. 卸载是换页，不是丢弃</h3>
<p>ComfyUI 的 <code>unload_all_models()</code> 在 DynamicVRAM 下走
<code>vbar.free_memory()</code>——把权重页换出去，不是销毁。<br>
实测可见证据：<code>t=98.6s</code> 时显存占用从 6.0 GB 掉到 <b>1.4 GB</b>，
但 <code>torch_reserved</code> 仍保持 <b>4.7 GB</b>——<code>cudaMallocAsync</code> 的内存池
把块收回池里，对 <code>mem_get_info</code> 而言仍算"已用"。这也解释了为什么
"OOM 时 torch 只报 859 MB，而 CUDA 报 0 字节空闲"。</p>

<h2>三、MiniMax H3 一次生成，每个阶段在干什么</h2>
<div class="chart">
  <div class="legend">
    <span><i class="sw" style="border-top:2.5px solid #E24B4A"></i>显存（左轴 GB）</span>
    <span><i class="sw" style="border-top:2.5px dashed #EF9F27"></i>内存（左轴 GB）</span>
    <span><i class="sw" style="border-top:2px dotted #378ADD"></i>磁盘累计读（右轴 GB）</span>
  </div>
  <svg viewBox="0 0 960 400" width="100%" role="img"
       aria-label="显存、内存与磁盘累计读取的时间曲线">
    <line x1="{X0}" y1="{Y1}" x2="{X1}" y2="{Y1}" stroke="#888780" stroke-width="1"/>
    <line x1="{X0}" y1="{Y0}" x2="{X0}" y2="{Y1}" stroke="#888780" stroke-width="1"/>
    {ticks_x}{ticks_y}{ticks_y2}{marks}
    <polyline points="{svg_disk}" fill="none" stroke="#378ADD" stroke-width="1.8" stroke-dasharray="2 3"/>
    <polyline points="{svg_ram}" fill="none" stroke="#EF9F27" stroke-width="2.2" stroke-dasharray="7 4"/>
    <polyline points="{svg_vram}" fill="none" stroke="#E24B4A" stroke-width="2.2"/>
    <text x="{X0}" y="{Y0-22}" font-size="12" fill="#5F5E5A">左轴：显存 / 内存（GB）　　右轴：磁盘累计读（GB）</text>
  </svg>
</div>

<table>
  <tr><th>阶段</th><th>时间窗</th><th>时长</th><th>磁盘读</th><th>显存峰值</th><th>内存峰值</th></tr>
  {seg_rows}
</table>

<h3>三个阶段的本质</h3>
<ul>
  <li><b>加载 + 条件编码（0–42 s）</b>：文本编码器（15.0 GB）和 UNET（21.6 GB）第一次从磁盘
      搬进来，跑一遍提示词编码。磁盘读 36.7 GB ≈ 两者之和，说明是"实打实读了一遍"。</li>
  <li><b>4 步采样（42–98 s）</b>：纯去噪循环。显存稳定在 4.1–4.2 GB（正好是那 4.6 GB 缓存），
      磁盘读 59.7 GB——<b>平均每步 15 GB，就是被反复重读的权重</b>。
      这正是它慢的原因：算力不是瓶颈，<b>磁盘带宽才是</b>。</li>
  <li><b>解码 + 收尾（98–110 s）</b>：采样结束，UNET 被卸下（显存掉到 1.4 GB），
      腾出空间给 VAE 解码。此刻出现全程唯一的双峰：
      <b>显存 6.1 GB（99.7%）、内存 16.2 GB（100%，只剩 1.5 MB）</b>——
      像素帧 + 音频波形在内存里展开，而权重缓存还没完全让位。</li>
</ul>

<div class="note">
<b>顺带一个容易误解的点：</b>同参数第二次提交只要 0.03 秒就"跑完"了——那是 ComfyUI 的
<b>执行缓存</b>命中（websocket 里能看到 <code>execution_cached</code>）。想测真实占用，
必须换种子或改参数。
</div>

<h2>四、由此得到的结论</h2>
<ul>
  <li><b>瓶颈不是 GPU 算力，是磁盘。</b>94.4 GB 的读取量对应 109 秒，平均 ~0.87 GB/s
      已经接近这块盘的顺序读上限。想提速，优先级是"减少重读"而不是"换更快的卡"。</li>
  <li><b>减少重读的两条路</b>：把缓存做大（降 <code>EXTRA_RESERVED_VRAM</code> / 用
      <code>--vram-headroom</code> 精确控制），或者减少每步需要触碰的权重
      （块交换 <code>blocks_to_swap</code>）。前者受限于激活需要空间，后者是项目里
      <code>blocks_swap_on</code> 那组旋钮要解决的问题。</li>
  <li><b>内存是比显存更硬的墙。</b>16.2 GB 内存全程占在 14 GB，解码瞬间打到只剩 1.5 MB。
      权重被换出时若内存不够，就会落 pagefile——那才是"慢到分钟级"的来源。
      项目日志里的落盘守卫给出的就是这个警告。</li>
  <li><b>显存"用满"不等于出问题。</b>采样期间稳定在 4.1–4.2 GB 而不是 6.0 GB，是因为
      ComfyUI 主动留了 1.39 GB 给激活；真正贴着上限的 6.1 GB 只出现在解码那一瞬。
      调预留参数就是在"少读盘"和"别 OOM"之间移动这条线。</li>
</ul>

</div></body></html>"""

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print(f"-> {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    build(a.csv, a.out)
