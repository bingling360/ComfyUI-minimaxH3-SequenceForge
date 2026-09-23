"""块交换（blockswap）的 PCIe 前置基准实测 —— **没数据不开工**。

为什么必须有这个脚本：块交换**不是**分块计算，是把 block 权重在 GPU/CPU 之间搬。
它能不能赚，取决于一条硬账：

    搬一块的耗时  vs  算一块的耗时

搬 > 算 → 开了纯亏（PCIe 成了瓶颈）。这个比值只能在目标机器上实测，猜不出来。
计划（`docs/性能优化_分块全量接线与阶段化前端_方案_2026-09-23.md` §C）把这条
列为块交换的**唯一硬前置**。本脚本就是那条前置。

跑法：
    D:/.../ComfyUI/ComfyUI/.venv/Scripts/python.exe tools/bench_blockswap_pcie.py
可选参数：
    --block-gb 0.4     单块权重大小（H3 int8 20GB / 50 块 ≈ 0.4GB）
    --tokens 8192      单块前向的 token 数（估「算一块」的量级）
    --hidden 3072      单块 hidden 维（估「算一块」的量级）
    --reps 20          每项重复次数（取中位数，抗抖动）

退出码 0 = 测出来了（不代表「该开」，只代表有数）；非 0 = 环境不满足（无 CUDA）。
⚠ 结果是**本机实测**，换机器必须重跑 —— 别把这里的数字当结论抄走。
"""

import argparse
import os
import statistics
import sys
import time

try:
    import torch
except ImportError:
    print("没有 torch，必须在 <ComfyUI根>/.venv 里跑本脚本", file=sys.stderr)
    sys.exit(2)

if not torch.cuda.is_available():
    print("没有可用 CUDA 设备 —— 块交换基准无从谈起", file=sys.stderr)
    sys.exit(2)


def _median_ms(fn, reps):
    """跑 reps 次取中位数（单位 ms）。首轮预热丢弃（首轮含分配/编译开销）。"""
    fn()                                    # warmup
    torch.cuda.synchronize()
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        torch.cuda.synchronize()
        ts.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(ts), min(ts)


def bench_bandwidth(block_bytes, reps):
    """H2D / D2H 有效带宽（GB/s）—— 用一块的真实字节大小测，比理论值可信。"""
    n = max(1, block_bytes // 4)            # float32
    cpu = torch.empty(n, dtype=torch.float32, pin_memory=True)
    gpu = torch.empty(n, dtype=torch.float32, device="cuda")
    h2d, _ = _median_ms(lambda: gpu.copy_(cpu, non_blocking=False), reps)
    d2h, _ = _median_ms(lambda: cpu.copy_(gpu, non_blocking=False), reps)
    nonblk, _ = _median_ms(lambda: gpu.copy_(cpu, non_blocking=True), reps)
    gb = block_bytes / (1024 ** 3)
    return gb, h2d, d2h, nonblk


def bench_move(block_bytes, reps):
    """搬一块的**总代价**：H2D + 计算前必须同步（D2H 只在换出时发生）。

    返回 (h2d_ms, d2h_ms, in_out_ms)。真实 block swap 一次前向要
    (换入 + 换出) 两块的钱，故 in_out = h2d + d2h 是更诚实的「一块的来往成本」。
    """
    gb, h2d, d2h, _ = bench_bandwidth(block_bytes, reps)
    return h2d, d2h, h2d + d2h, gb


def bench_compute(tokens, hidden, reps, device="cuda"):
    """算一块的量级：两个 hidden×hidden 的 Linear（FFN 的 fc1+fc2 近似）。

    为什么用这个估：H3 double block 的主要算力就在 attention 的两个投影 + FFN 的
    两个投影。这里取 hidden→hidden→hidden 两次 GEMM 作**保守下界**（不含 attention
    的 S×S 项），所以实测「算」只会比这里更慢 → 若连这个下界都比「搬」快，
    块交换**必亏**；若这里就已经比「搬」慢，才有继续评估的价值。
    """
    dev = device
    x = torch.randn(tokens, hidden, device=dev, dtype=torch.bfloat16)
    w1 = torch.randn(hidden, hidden, device=dev, dtype=torch.bfloat16)
    w2 = torch.randn(hidden, hidden, device=dev, dtype=torch.bfloat16)

    def fwd():
        y = x @ w1
        return y @ w2

    with torch.no_grad():
        med, _ = _median_ms(fwd, reps)
    return med


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--block-gb", type=float, default=0.4)
    ap.add_argument("--tokens", type=int, default=8192)
    ap.add_argument("--hidden", type=int, default=3072)
    ap.add_argument("--reps", type=int, default=20)
    a = ap.parse_args()

    name = torch.cuda.get_device_name(0)
    total_gb = torch.cuda.mem_get_info()[1] / (1024 ** 3)
    block_bytes = int(a.block_gb * (1024 ** 3))

    print()
    print("=" * 68)
    print("  块交换 PCIe 前置基准")
    print("=" * 68)
    print(f"  设备        : {name}")
    print(f"  显存        : {total_gb:.1f} GB")
    print(f"  单块权重    : {a.block_gb:.2f} GB（H3 int8 20GB / 50 块 ≈ 0.4GB）")
    print(f"  单块 token  : {a.tokens}    hidden: {a.hidden}")
    print(f"  重复        : {a.reps} 次（取中位数）")
    print("-" * 68)

    print("  [1/2] 测「搬一块」…")
    h2d, d2h, in_out, gb = bench_move(block_bytes, a.reps)
    bw_h2d = gb / (h2d / 1000.0)
    bw_d2h = gb / (d2h / 1000.0)
    print(f"        H2D  {h2d:7.2f} ms  （{bw_h2d:6.2f} GB/s）")
    print(f"        D2H  {d2h:7.2f} ms  （{bw_d2h:6.2f} GB/s）")
    print(f"        来访成本（换入+换出）= {in_out:7.2f} ms / 块")

    print("  [2/2] 测「算一块」（保守下界：两次 hidden² GEMM，不含 attention）…")
    compute_ms = bench_compute(a.tokens, a.hidden, a.reps)
    print(f"        计算  {compute_ms:7.2f} ms / 块（下界）")

    ratio = in_out / compute_ms if compute_ms > 0 else float("inf")
    print("-" * 68)
    print(f"  搬/算 比 = {in_out:.2f} / {compute_ms:.2f} = {ratio:.3f}")
    if ratio >= 1.0:
        print("  ✗ 结论：**搬比算还慢** → 块交换在本机纯亏，不要开。")
    elif ratio >= 0.5:
        print("  ⚠ 结论：搬/算 在 0.5–1.0 —— 只有「装不下」时才有意义（换生存不换速度），")
        print("          预取（prefetch）能掩盖一部分，但收益有限。")
    else:
        print("  ✓ 结论：搬明显快于算 —— 块交换有可行性（需再验显存碎片与锁页内存账）。")
    print()
    print("  ⚠ 本结果只对**本机**成立；换卡/换 PCIe 代次必须重跑。")
    print("  ⚠ 「算一块」是下界（未含 attention 的 S² 项）→ 真实比值只会比上面更小，")
    print("     即真实场景比这里更有利于块交换；但下界若已 ≥1，则结论已足够。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
