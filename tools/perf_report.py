#!/usr/bin/env python3
"""回读 H3 性能探测日志（JSONL），打印跨段对照报告。

**这个工具存在的唯一理由**：云端显存不足的下场是 OOM killer 发 SIGKILL ——
进程连 except 都跑不到，堆在内存里等段末统一打印的汇总行必然丢失。
探测点每采一个就 append + flush 到 `logs/h3_perf_probe.jsonl`，
崩了之后跑本脚本就能看到**崩之前**的全部数据。

用法：
    python tools/perf_report.py              # 最后一次运行
    python tools/perf_report.py --runs       # 列出文件里所有运行
    python tools/perf_report.py --run 20260917-203900-1234
    python tools/perf_report.py --all        # 不按 run 过滤，全量
    python tools/perf_report.py --path /some/h3_perf_probe.jsonl
    python tools/perf_report.py --follow     # 跟着跑（每 5 秒刷新，另开窗口用）

怎么看：
    同一**列**往下有没有往上爬。段1「解码后」vs 段9「解码后」
    —— 爬就是累积（内存泄漏 / 缓存只增），平就是稳定。
    显存看「卸载后」有没有真掉下来：不掉说明腾挪没回收干净。

零第三方依赖（只用标准库），可在云端机器上直接跑。
"""

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import perf  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="回读 H3 性能探测日志并打印跨段对照报告")
    ap.add_argument("--path", default=None, help="JSONL 路径（默认 logs/h3_perf_probe.jsonl）")
    ap.add_argument("--run", default=None, help="指定 run id（默认最后一次运行）")
    ap.add_argument("--all", action="store_true", help="不过滤 run，输出全部")
    ap.add_argument("--runs", action="store_true", help="列出所有 run 后退出")
    ap.add_argument("--raw", action="store_true", help="额外逐条打印原始事件")
    ap.add_argument("--follow", action="store_true", help="跟随模式：每 5 秒重画一次")
    ap.add_argument("--interval", type=float, default=5.0, help="跟随模式间隔秒数")
    args = ap.parse_args(argv)

    path = args.path or perf.log_path()

    if args.runs:
        runs = perf.list_runs(path)
        if not runs:
            print(f"（日志文件不存在或为空：{path}）")
            return 1
        print(f"日志文件：{path}")
        for r in runs:
            print("  " + r)
        return 0

    if not os.path.exists(path):
        print(f"（还没有探测日志：{path}）")
        print("先在 ComfyUI 里跑一次带二采的链，本脚本才有东西可读。")
        print(f"可用 H3_PERF_LOG=<路径> 环境变量改落盘位置；设 H3_PERF_LOG=0 关闭。")
        return 1

    while True:
        events = perf.read_events(path, run=None if args.all else args.run)
        if args.follow:
            # 清屏后重画，避免刷屏
            os.system("cls" if os.name == "nt" else "clear")
        print(f"# 探测日志：{path}")
        if not args.all:
            rid = args.run or (events[0].get("run") if events else None)
            print(f"# 运行：{rid}")
        print(f"# 事件数：{len(events)}")
        print()
        for line in perf.summarize(events):
            print(line)
        if args.raw:
            print("\n---- 原始事件 ----")
            for e in events:
                print(e)
        if not args.follow:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
