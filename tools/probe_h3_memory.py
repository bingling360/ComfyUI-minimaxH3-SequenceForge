# -*- coding: utf-8 -*-
"""实跑一次 H3 生成并采样显存/内存 —— 外部零侵入探针。

为什么要"外部"探针：
  本机 `nvidia-smi` 不可用（NVML 挂了），而 ComfyUI 进程内读 `torch.cuda` 又只能
  拿到 **自己的** 分配器口径。外部进程只要不 import torch，就不会创建 CUDA
  context、不占显存，读到的数据才是被测对象的真实状态。

三个数据源：
  1. ComfyUI `/system_stats`  -> 设备级 vram_free（= torch.cuda.mem_get_info 口径，
     含 torch 缓存块；DynamicVRAM 下 aimdo 占住的权重页**不**计入 free）
  2. psutil 进程指标          -> ComfyUI 进程 RSS、系统可用 RAM、进程磁盘读字节
     （磁盘读字节是 mmap 分页的直接证据：36GB 权重不可能全进 RAM）
  3. websocket `/ws?client_id=` -> 执行事件（节点起止、采样进度），给曲线打阶段标记

⚠ 两个必须踩对的点（都踩过）：
  - websocket 必须带 `?client_id=`，否则 ComfyUI 把事件推给别的连接，一个都收不到；
  - 消息体是 `{"type": ..., "data": {...}}`，节点号在 `data.node` 里，不在顶层。

CSV 边采边写（行缓冲），中途被 kill 也有数据。

用法：
  <venv-python> tools/probe_h3_memory.py --prompt tools/_h3_probe_min.json \
      --out output/h3_mem_probe/run1_trace.csv --events output/h3_mem_probe/run1_events.tsv
  <venv-python> tools/probe_h3_memory.py --dry-run      # 只采样 5s 看基线，不提交
"""
import argparse
import asyncio
import csv
import json
import os
import sys
import time
import urllib.request

import psutil

HOST = "127.0.0.1"
PORT = 8188
BASE = f"http://{HOST}:{PORT}"
CLIENT_ID = "h3-mem-probe"
# ⚠ 参数名是驼峰 `clientId`（server.py:276 `query.get('clientId')`）。
# 写成 `client_id` 不会报错，但服务端会另发一个随机 sid，事件全推给那个没人听的
# key —— 表现就是"连上了、一个事件也收不到"。
WS = f"ws://{HOST}:{PORT}/ws?clientId={CLIENT_ID}"


# ---------------------------------------------------------------- 进程发现

def find_comfy_procs():
    """只挑 ComfyUI 的 server 进程链（launcher shim + 真 server），返回 [psutil.Process]。

    ⚠ 不能用 `comfyui in cmdline` 这种宽条件：Desktop 的 GUI/助手进程叫
    `Comfy Desktop.exe`，路径里也含 comfyui，会把 10 个 GUI 进程的 RSS 加进来，
    "进程内存"直接虚高一倍。真正的加载发生在 `python -s ComfyUI\\main.py` 那一支。
    """
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            cmd = " ".join(p.info.get("cmdline") or [])
        except Exception:
            continue
        if "python" not in name:
            continue
        if "main.py" in cmd.replace("\\", "/"):
            out.append(p)
    return out


def port_owner_pid(port=PORT):
    """通过 psutil.net_connections 找监听该端口的 PID（免 netstat 解析）。"""
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
                return c.pid
    except Exception:
        pass
    return None


def queue_depth():
    """返回 (running, pending)；失败返回 None。"""
    try:
        with urllib.request.urlopen(f"{BASE}/queue", timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
        return len(d.get("queue_running") or []), len(d.get("queue_pending") or [])
    except Exception:
        return None


def history_status(prompt_id):
    """查 /history/<id>：任务是否已收尾。返回 'done' | 'error' | None（未完成/查不到）。

    比"队列归零"可靠得多：队列空只说明排完了，不代表这个 id 的结果已经落进
    history（中间还有落盘、assets 扫描等尾巴）。而且 websocket 掉事件时它是唯一
    还能拿到结论的通道。
    """
    try:
        with urllib.request.urlopen(f"{BASE}/history/{prompt_id}", timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if not d:
        return None
    entry = d.get(prompt_id)
    if not entry:
        return None
    st = entry.get("status") or {}
    if st.get("status_str") == "error":
        return "error"
    if st.get("completed"):
        return "done"
    return None


# ---------------------------------------------------------------- 采样

def read_system_stats():
    """读 ComfyUI 的 /system_stats；失败返回 None（不抛）。"""
    try:
        with urllib.request.urlopen(f"{BASE}/system_stats", timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    sysd = d.get("system") or {}
    dev = (d.get("devices") or [{}])[0]
    return {
        "vram_free": dev.get("vram_free"),
        "vram_total": dev.get("vram_total"),
        "torch_vram_free": dev.get("torch_vram_free"),
        "torch_vram_total": dev.get("torch_vram_total"),
        "ram_total": sysd.get("ram_total"),
        "ram_free": sysd.get("ram_free"),
    }


class Sampler:
    """边采边写：每 tick 追加一行 CSV（行缓冲，被 kill 也不丢已采数据）。"""

    FIELDS = ["t", "vram_used_mb", "vram_free_mb", "vram_total_mb",
              "torch_reserved_mb", "ram_used_mb", "ram_free_mb",
              "comfy_rss_mb", "disk_read_mb", "disk_write_mb",
              "phase", "note"]

    def __init__(self, out_path, interval=1.0, procs=None):
        self.out_path = out_path
        self.interval = interval
        self.procs = procs or []
        self.rows = []
        self.t0 = time.time()
        self._io0 = None
        self.phase = "startup"
        self.note = ""
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        self._fh = open(out_path, "w", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=self.FIELDS)
        self._w.writeheader()

    def _io(self):
        rb = wb = 0
        for p in self.procs:
            try:
                c = p.io_counters()
                rb += c.read_bytes
                wb += c.write_bytes
            except Exception:
                pass
        return rb, wb

    def _rss(self):
        rss = 0
        for p in self.procs:
            try:
                rss += p.memory_info().rss
            except Exception:
                pass
        return rss

    def tick(self):
        st = read_system_stats()
        rss = self._rss()
        rb, wb = self._io()
        if self._io0 is None:
            self._io0 = (rb, wb)
        vm = psutil.virtual_memory()
        vram_total = (st or {}).get("vram_total") or 0
        vram_free = (st or {}).get("vram_free") or 0
        row = {
            "t": round(time.time() - self.t0, 2),
            "vram_used_mb": round((vram_total - vram_free) / 2 ** 20, 1) if vram_total else "",
            "vram_free_mb": round(vram_free / 2 ** 20, 1) if vram_free else "",
            "vram_total_mb": round(vram_total / 2 ** 20, 1) if vram_total else "",
            "torch_reserved_mb": round(((st or {}).get("torch_vram_total") or 0) / 2 ** 20, 1),
            "ram_used_mb": round((vm.total - vm.available) / 2 ** 20, 1),
            "ram_free_mb": round(vm.available / 2 ** 20, 1),
            "comfy_rss_mb": round(rss / 2 ** 20, 1),
            "disk_read_mb": round((rb - self._io0[0]) / 2 ** 20, 1),
            "disk_write_mb": round((wb - self._io0[1]) / 2 ** 20, 1),
            "phase": self.phase,
            "note": self.note,
        }
        self.rows.append(row)
        self._w.writerow(row)
        self._fh.flush()
        return row

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


async def run(args):
    import aiohttp

    pid = port_owner_pid()
    procs = find_comfy_procs()
    if pid:
        try:
            procs = [psutil.Process(pid)] + [p for p in procs if p.pid != pid]
        except Exception:
            pass
    seen, uniq = set(), []
    for p in procs:
        if p.pid not in seen:
            seen.add(p.pid)
            uniq.append(p)
    procs = uniq
    print(f"[probe] 被测进程: {[(p.pid, p.name()) for p in procs]}", flush=True)

    smp = Sampler(args.out, interval=args.interval, procs=procs)
    base = smp.tick()
    print(f"[probe] 基线: vram_used={base['vram_used_mb']}MB "
          f"ram_used={base['ram_used_mb']}MB comfy_rss={base['comfy_rss_mb']}MB", flush=True)

    if args.dry_run:
        for _ in range(5):
            await asyncio.sleep(args.interval)
            smp.tick()
        smp.close()
        print(f"[probe] dry-run 完成 -> {smp.out_path}")
        return

    with open(args.prompt, encoding="utf-8") as fh:
        graph = json.load(fh)
    if args.seed is not None:
        # ComfyUI 有执行缓存：同图同参第二次提交 0.03s 秒回，什么都测不到。
        # 换种子是最省事的破缓存手段。
        for nid, node in graph.items():
            if isinstance(node, dict) and "种子" in (node.get("inputs") or {}):
                node["inputs"]["种子"] = int(args.seed)
                print(f"[probe] 节点 {nid} 种子 -> {args.seed}", flush=True)
    payload = {"prompt": graph, "client_id": CLIENT_ID}
    pid_box = {"v": None}

    events = []
    done = asyncio.Event()
    stop = asyncio.Event()
    started = {"v": False}
    seen_node = {"v": False}   # 真的执行过节点（而非被 ComfyUI 执行缓存命中秒回）

    async def sampler_loop():
        while not stop.is_set():
            await asyncio.sleep(args.interval)
            smp.tick()

    async def ws_loop(session):
        async with session.ws_connect(WS, heartbeat=30) as ws:
            print("[probe] websocket 已连接", flush=True)
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    d = json.loads(msg.data)
                except Exception:
                    continue
                ty = d.get("type")
                data = d.get("data") or {}
                t = round(time.time() - smp.t0, 2)
                if ty == "execution_start":
                    events.append((t, "execution_start", ""))
                    smp.phase = "exec_start"
                elif ty == "executing":
                    node = data.get("node")
                    if node is None:
                        events.append((t, "execution_end", ""))
                        smp.phase = "done"
                        done.set()
                    else:
                        seen_node["v"] = True
                        events.append((t, "executing", str(node)))
                        smp.phase = f"node:{node}"
                elif ty == "execution_cached":
                    events.append((t, "execution_cached",
                                   ",".join(str(n) for n in (data.get("nodes") or []))[:120]))
                elif ty == "progress":
                    v, m = data.get("value"), data.get("max")
                    events.append((t, "progress", f"{v}/{m}"))
                    smp.phase = f"progress:{v}/{m}"
                    smp.note = f"node={data.get('node')}"
                elif ty in ("execution_error", "execution_interrupted"):
                    events.append((t, ty, json.dumps(data.get("exception_message", ""))[:200]))
                    smp.phase = ty
                    done.set()
                elif ty == "executed":
                    events.append((t, "executed", str(data.get("node"))))
                    out = data.get("output") or {}
                    if out:
                        events.append((t, "output_keys", ",".join(sorted(out.keys()))))
                elif ty == "status":
                    q = ((data.get("status") or {}).get("exec_info") or {})
                    if q.get("queue_remaining") == 0 and started["v"]:
                        pass

    async def queue_watch(pid_box):
        """兜底判完成：优先看 /history/<id>，其次"队列归零且确实执行过节点"。

        只有 websocket 时是脆的 —— 掉一条事件就永远等下去（踩过）。
        """
        while not stop.is_set():
            await asyncio.sleep(3.0)
            pid = pid_box.get("v")
            if not pid:
                continue
            hs = history_status(pid)
            if hs in ("done", "error"):
                events.append((round(time.time() - smp.t0, 2), f"history_{hs}", ""))
                smp.phase = f"done({hs})"
                done.set()
                return
            q = queue_depth()
            if q is None:
                continue
            running, pending = q
            if seen_node["v"] and running == 0 and pending == 0:
                await asyncio.sleep(3.0)
                if queue_depth() == (0, 0):
                    events.append((round(time.time() - smp.t0, 2), "queue_empty_end", ""))
                    smp.phase = "done(queue)"
                    done.set()
                    return

    async with aiohttp.ClientSession() as session:
        ws_task = asyncio.create_task(ws_loop(session))
        q_task = asyncio.create_task(queue_watch(pid_box))
        await asyncio.sleep(1.0)
        smp.phase = "submitted"
        async with session.post(f"{BASE}/prompt", json=payload) as resp:
            body = await resp.text()
            if resp.status != 200:
                print(f"[probe] 提交失败 HTTP {resp.status}: {body[:800]}", flush=True)
                stop.set()
                ws_task.cancel()
                q_task.cancel()
                smp.close()
                return
            r = json.loads(body)
            started["v"] = True
            pid_box["v"] = r.get("prompt_id")
            print(f"[probe] 已提交 prompt_id={r.get('prompt_id')} "
                  f"number={r.get('number')} node_errors={r.get('node_errors')}", flush=True)

        smp_task = asyncio.create_task(sampler_loop())
        try:
            await asyncio.wait_for(done.wait(), timeout=args.timeout)
        except asyncio.TimeoutError:
            print(f"[probe] 超时 {args.timeout}s 未收到结束事件", flush=True)
        stop.set()
        for tk in (smp_task, ws_task, q_task):
            tk.cancel()
        await asyncio.sleep(0.3)
        smp.tick()
        smp.close()

    with open(args.events, "w", encoding="utf-8") as fh:
        for t, ty, val in events:
            fh.write(f"{t}\t{ty}\t{val}\n")

    rows = smp.rows
    peak_v = max((r["vram_used_mb"] for r in rows if r["vram_used_mb"] != ""), default=0)
    peak_r = max((r["comfy_rss_mb"] for r in rows), default=0)
    peak_ram = max((r["ram_used_mb"] for r in rows), default=0)
    print("\n[probe] === 汇总 ===")
    print(f"  时长          : {rows[-1]['t']:.1f}s")
    print(f"  显存峰值      : {peak_v:.0f} MB / {rows[0]['vram_total_mb']:.0f} MB")
    print(f"  进程 RSS 峰值 : {peak_r:.0f} MB")
    print(f"  系统内存峰值  : {peak_ram:.0f} MB")
    print(f"  磁盘累计读    : {rows[-1]['disk_read_mb']:.0f} MB")
    print(f"  CSV -> {smp.out_path}")
    print(f"  事件 -> {args.events}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", help="API 格式工作流 JSON")
    ap.add_argument("--out", default="output/h3_mem_probe/trace.csv")
    ap.add_argument("--events", default="output/h3_mem_probe/events.tsv")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--seed", type=int, help="覆盖工作流里的「种子」（破 ComfyUI 执行缓存）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.dry_run and not args.prompt:
        sys.exit("需要 --prompt 或 --dry-run")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
