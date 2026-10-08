# -*- coding: utf-8 -*-
"""把探针 CSV + websocket 事件汇总成「阶段账」：每阶段的显存/内存/磁盘读峰值。

用法：
  <venv-python> tools/analyze_h3_trace.py output/h3_mem_probe/run1_trace.csv \
      output/h3_mem_probe/run1_events.tsv [--json out.json]
"""
import argparse
import csv
import json
import sys


def load(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summarize(rows):
    keys = ["vram_used_mb", "ram_used_mb", "ram_free_mb", "comfy_rss_mb",
            "disk_read_mb", "disk_write_mb", "torch_reserved_mb"]
    out = {}
    for k in keys:
        vals = [fnum(r[k]) for r in rows]
        vals = [v for v in vals if v is not None]
        if vals:
            out[k] = {"min": min(vals), "max": max(vals), "last": vals[-1],
                      "argmax_t": rows[[fnum(r[k]) for r in rows].index(max(vals))]["t"]}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("events", nargs="?")
    ap.add_argument("--json")
    a = ap.parse_args()

    rows = load(a.csv)
    print(f"采样 {len(rows)} 行，覆盖 {rows[-1]['t']}s")
    s = summarize(rows)
    print("\n=== 全程峰值 ===")
    for k, v in s.items():
        print(f"  {k:18} min={v['min']:>9.1f} max={v['max']:>9.1f} "
              f"last={v['last']:>9.1f} (max@{v['argmax_t']}s)")

    # 事件
    ev = []
    if a.events:
        try:
            with open(a.events, encoding="utf-8") as fh:
                for line in fh:
                    p = line.rstrip("\n").split("\t")
                    if len(p) >= 2:
                        ev.append({"t": float(p[0]), "type": p[1],
                                   "val": p[2] if len(p) > 2 else ""})
        except FileNotFoundError:
            pass
    if ev:
        print("\n=== 事件 ===")
        for e in ev[:80]:
            print(f"  {e['t']:>8.2f}s  {e['type']:<20} {e['val'][:60]}")

    # 粗粒度阶段：按 phase 字段分桶
    print("\n=== 按 phase 分桶 ===")
    buckets = {}
    for r in rows:
        buckets.setdefault(r["phase"], []).append(r)
    for ph, rs in buckets.items():
        v = [fnum(x["vram_used_mb"]) for x in rs]
        v = [x for x in v if x is not None]
        m = [fnum(x["comfy_rss_mb"]) for x in rs]
        m = [x for x in m if x is not None]
        dr = [fnum(x["disk_read_mb"]) for x in rs]
        dr = [x for x in dr if x is not None]
        print(f"  {ph:<28} n={len(rs):>4} "
              f"vram_max={max(v) if v else 0:>8.0f}MB "
              f"rss_max={max(m) if m else 0:>8.0f}MB "
              f"diskread_last={max(dr) if dr else 0:>8.0f}MB "
              f"t=[{rs[0]['t']}..{rs[-1]['t']}]")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"summary": s, "events": ev,
                       "rows": rows}, fh, ensure_ascii=False, indent=1)
        print(f"\n-> {a.json}")


if __name__ == "__main__":
    main()
