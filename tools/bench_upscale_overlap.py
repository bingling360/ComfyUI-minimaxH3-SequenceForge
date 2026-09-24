# -*- coding: utf-8 -*-
"""探针：放大网络「块间重叠帧」(upscale_overlap) 的画质收益与耗时代价。

回答两个问题：
  ① overlap 到底补回多少画质？
  ② 补到多少才够？

⚠ 本机没有放大网络权重（models/latent_upscale_models/ 是占位目录），所以用
**随机权重**跑。随机 conv 核不衰减 → 远处帧的贡献不会自然衰减 → 测出的
「归零所需 overlap」是**保守上界**，真实训练权重会更早归零。

★ 关键：放大网络里有两个**互相独立**的误差源，必须拆开测，否则会得出
「调大 overlap 没用」的错误结论：
  (a) 时序卷积的上下文缺失  ← overlap 能修，这是本参数存在的意义
  (b) GroupNorm 的跨帧统计量 ← overlap **修不了**（每块统计量基于本块帧算）
  所以本探针跑两组：raw（原网络）与 nonorm（把 GroupNorm 换成 Identity，
  隔离出纯卷积上下文的效果）。

用法（必须用 ComfyUI 的 venv，只有它装了 torch/einops）：
  <ComfyUI>/.venv/Scripts/python.exe tools/bench_upscale_overlap.py
"""
import importlib.util
import os
import sys
import time

import torch
import torch.nn as nn

HERE = os.path.dirname(os.path.abspath(__file__))
NET_PY = os.path.join(os.path.dirname(HERE), "upscale_net.py")


def load_net_module():
    spec = importlib.util.spec_from_file_location("_h3_upscale_net", NET_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_h3_upscale_net"] = mod
    spec.loader.exec_module(mod)
    return mod


class StatPinGN(nn.Module):
    """GroupNorm 包装：可选「钉住整段统计量」而不是用本块统计量。

    ★ 为什么必须这么做：nn.GroupNorm(32, C) 是在 (C, T, H, W) 上求统计量的，
    **跨帧**。分块后每块只拿自己那几帧求均值/方差 → 每块的输出整体尺度不同。
    这个差异 overlap 修不了，会盖过卷积上下文的差异。钉住统计量后，块间
    唯一的差异就只剩「卷积看不到邻块」这一项 —— 才是本参数的真正作用面。

    注意不能简单换成 Identity：随机权重下失去归一化会逐层放大到 1e8（实测）。
    """

    def __init__(self, gn):
        super().__init__()
        self.gn = gn
        self.pinned = None        # (mean, var)，形状 (N, G, 1)
        self.record = False

    def forward(self, x):
        N, C = x.shape[0], x.shape[1]
        G = self.gn.num_groups
        xr = x.reshape(N, G, -1)
        mean = xr.mean(2, keepdim=True)
        var = xr.var(2, unbiased=False, keepdim=True)
        if self.record:
            self.pinned = (mean.detach().clone(), var.detach().clone())
        elif self.pinned is not None:
            mean, var = self.pinned
        xr = (xr - mean) / torch.sqrt(var + self.gn.eps)
        xr = xr.reshape(N, C, *x.shape[2:])
        if self.gn.affine:
            sh = (1, -1) + (1,) * (x.dim() - 2)
            xr = xr * self.gn.weight.reshape(sh) + self.gn.bias.reshape(sh)
        return xr


def pin_norms(module, out):
    """递归把 GroupNorm 换成 StatPinGN，收集到 out 列表里。"""
    for name, child in list(module.named_children()):
        if isinstance(child, nn.GroupNorm):
            w = StatPinGN(child)
            setattr(module, name, w)
            out.append(w)
        else:
            pin_norms(child, out)


def temporal_radius(net):
    """串联时序卷积的**理论**感受野半径：每层 (k-1)//2 逐层累加。

    conv_in / conv_out 也是 nn.Conv3d(核 3, padding 1)，在 T 上各贡献半径 1。
    """
    rad = 0
    for blocks in (net.in_blocks, net.out_blocks):
        for b in blocks:
            w = getattr(getattr(b, "dwconv", None), "weight", None)
            if w is not None and w.dim() == 5:
                rad += (w.shape[2] - 1) // 2
    return rad + 2


def build(un, seed, mode):
    torch.manual_seed(seed)
    # 与真实配置同结构（12+12 block / 每 2 层一个时序卷积 / 核宽 5），
    # 只把 channels 降到 64 让 CPU 跑得动 —— 感受野只由「层数 × 核宽」决定。
    net = un.LatentResizer3D(
        in_channels=24, in_blocks=12, out_blocks=12, channels=64,
        dropout=0.0, attn=False, temporal_every=2, temporal_kernel=5)
    for p in net.parameters():
        p.data.normal_(0.0, 0.05)
    pins = []
    if mode == "pinned":
        pin_norms(net, pins)
    net.eval()
    return net, pins


def run(un, mode, T, CHUNK, ovs):
    tag = ("raw（原网络，GroupNorm 用本块统计量）" if mode == "raw"
           else "pinned（GroupNorm 钉住整段统计量 → 只剩卷积上下文差异）")
    print(f"\n{'=' * 84}\n### {tag}\n### T={T} chunk={CHUNK} 块数={-(-T // CHUNK)}\n{'=' * 84}")
    net, pins = build(un, 20260924, mode)
    x = torch.randn(1, 24, T, 8, 8, dtype=torch.float32)
    tgt = (T, 16, 16)
    rad = temporal_radius(net)
    print(f"理论感受野半径 = {rad} 帧  -> 严格等价需 overlap >= 半径/2 = {rad / 2:.1f}")

    if pins:
        for w in pins:
            w.record = True
    t0 = time.perf_counter()
    ref = net(x, scale=2.0, target_size=tgt, enable_chunking=False)
    t_ref = time.perf_counter() - t0
    if pins:
        for w in pins:
            w.record = False          # 之后的分块一律用整段统计量
        print(f"已钉住 {len(pins)} 个 GroupNorm 的整段统计量")
    amp = float(ref.abs().max())

    seams = [c for c in range(CHUNK, T, CHUNK)]
    band = torch.zeros(T, dtype=torch.bool)
    for s in seams:
        band[max(0, s - 1):min(T, s + 1)] = True

    hdr = (f"{'ov':>4} {'生效':>4} {'块内帧':>6} {'耗时s':>7} {'vs整段':>7} "
           f"{'全帧maxErr':>11} {'接缝maxErr':>11} {'非接缝max':>11} {'相对%':>8}")
    print(hdr)
    print("-" * len(hdr))
    for ov in ovs:
        t0 = time.perf_counter()
        y = net(x, scale=2.0, target_size=tgt, enable_chunking=True,
                chunk_frames=CHUNK, overlap_frames=ov)
        dt = time.perf_counter() - t0
        eff = max(ov, 5)                     # 代码里只增不减：< 核宽夹回 tk=5
        n_frames = CHUNK + 4 * eff           # 中间块的真实前向帧数
        err = (y - ref).abs()
        per_t = err.amax(dim=(0, 1, 3, 4))   # 逐帧最大偏差
        mx = float(per_t.max())
        se = float(err[:, :, band].max())
        ne = float(err[:, :, ~band].max())
        top = torch.topk(per_t, 5).indices.tolist()
        print(f"{ov:>4} {eff:>4} {n_frames:>6} {dt:>7.2f} {dt / t_ref:>6.2f}x "
              f"{mx:>11.3e} {se:>11.3e} {ne:>11.3e} {mx / amp * 100:>7.3f}%"
              f"   最差帧={sorted(top)}")
    return t_ref


def main():
    un = load_net_module()
    torch.set_grad_enabled(False)
    print(f"torch {torch.__version__}  device=cpu  dtype=float32")
    for mode in ("raw", "pinned"):
        run(un, mode, T=64, CHUNK=16, ovs=(0, 5, 8, 12, 16, 24, 32))
    print("\n读法：")
    print("  · pinned 组才是 overlap 的真实作用面 —— 看它什么时候归零（≈1e-6）。")
    print("  · raw 组比 pinned 组多出来的那截，是 GroupNorm 跨帧统计量差异，")
    print("    **调 overlap 修不掉**；只能靠加大 chunk（块越长，块内统计量越接近整段）。")
    print("  · 「块内帧」= 中间块实际前向的帧数（= chunk + 4×生效overlap）。")


if __name__ == "__main__":
    main()
