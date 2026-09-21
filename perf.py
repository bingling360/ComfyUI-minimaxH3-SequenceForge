"""MiniMax H3 性能工程：硬件判据与场景策略 —— **纯函数，零 ComfyUI 依赖**。

与 grid.py 同级：**必须能在没有 torch / comfy / psutil 的环境里 import**，
这样 managed Python（只有 pytest）也能单测，这是本模块的设计前提。

三条判据（docs/性能优化工程_改造计划_2026-09-17.md §2.2）

| 判据 | 公式 | 决定什么 |
|---|---|---|
| **R_v** | UNET 权重 ÷ 显存总量 | 模型能不能**常驻**显存 |
| **R_m** | (UNET + TE) ÷ 内存总量 | 卸载是落**内存**，还是被 OS 换出到 **swap / pagefile** |
| **R_d** | 可用磁盘 ÷ (UNET + TE) | 能不能承受"落盘"这件事 |

两种真实场景（§2.1）

- **场景 A `cloud`**：云端 Linux，32GB 显存 / 60–90GB 内存 / **几乎没有磁盘**
  → R_v<1 装得下，R_m<1 落内存。取向是**守住「绝不落盘」**（无 swap 会被 OOM kill，
  不是变慢）。优化重点是**速度**。
- **场景 B `local`**：本地 Windows，16GB 显存 / 32GB 内存 / **大量磁盘**
  → R_v>1 装不下，R_m>1 必落盘（好在盘够）。取向是**让 26GB 有序流动**。

`probe_hardware()` 负责**问环境**（best-effort：量不到给 None，绝不抛）；
下面的 `ratio_*` / `resolve_profile` / `resolve_perf` / `offload_guard` 全是**纯函数**，
只吃 dict、吐 dict/标量——判据的唯一真源在后端，前端只显示不计算
（既有铁律：「参数单一真源」）。
"""

import contextlib
import json
import logging
import math
import os
import threading
import time

GB = float(1024 ** 3)

# ComfyUI 权重回载的日志标记（comfy/model_management.py 里 logging.info 打的）
_MODEL_LOAD_MARK = "Requested to load"

# ---- 场景 ----

PROFILE_CLOUD = "cloud"     # 场景 A：云端高显存 / 大内存 / 无磁盘
PROFILE_LOCAL = "local"     # 场景 B：本地小显存 / 小内存 / 大磁盘
PROFILE_CUSTOM = "custom"   # 用户手动覆盖，不套任何自动表

PROFILES = (PROFILE_CLOUD, PROFILE_LOCAL, PROFILE_CUSTOM)

PROFILE_LABELS = {
    PROFILE_CLOUD: "云端高显存",
    PROFILE_LOCAL: "本地小显存",
    PROFILE_CUSTOM: "自定义",
}

# ---- 块交换（§5.2）----
#
# H3 = 50 个 double block（upscale._H3_DOUBLE_BLOCK_COUNT；T8 源码同名常量）。
# 这里复制常量而不是 import upscale：本模块必须零 ComfyUI 依赖，
# 而 upscale 顶层就 import torch。upscale 那边是权威值，改动时两处同步。
H3_DOUBLE_BLOCK_COUNT = 50

# 换块数模型的两个系数（§5.2 参数表的反解，见 suggest_blocks 注释）
#
# RESERVE_GB：留给定住部分之外的激活 + VAE + 碎片的固定余量。
# VRAM_USABLE：显存可用比例（驱动 / 显示 / 上下文占用，16GB 卡实际 ~15.2GB）。
RESERVE_GB = 2.0
VRAM_USABLE = 0.95

# 驱动保留比例：torch 报告的可用显存 ≈ 总量 × 0.98（32GB 卡 31.36GB，与计划实测一致）。
# R_v 用它而不是裸总量 —— 那 2% 是真实拿不到的，算进分母会让 R_v 偏乐观。
VRAM_DRIVER_RESERVE = 0.98

# ---- 落盘守卫（§3.3）----
#
# 卸载需要 内存+swap ≥ 权重总和 × 此系数（1.2 = 20% 余量给碎片与临时副本）
DEFAULT_GUARD_RATIO = 1.2

# ---- ds.perf 契约（§6.2）----
#
# 键名与前端一致；"auto" 表示跟随 profile（真源在 resolve_perf，不在前端）。
DEFAULT_PERF = {
    # 场景
    "profile": "auto",
    "preset": "auto",

    # 显存：权重流动
    "blocks_to_swap": -1,          # -1 = 跟随 profile；0..50
    "prefetch_blocks": 2,
    "non_blocking": True,

    # 显存：常驻
    "unload_unet_seg": "auto",
    "unload_before_decode": "auto",

    # 显存：峰值
    "decode_chunk_frames": 0,      # 0 = 不额外分块（H3 VAE 本就内部分块）
    "frames_to_cpu": "auto",
    "max_upscale_scale": 0,        # 0 = 不限

    # 内存
    "unload_upscaler_cache": "auto",
    "cond_cache_size": "auto",

    # 磁盘 / swap 守卫（场景 A 必备）
    "guard_offload_target": True,
    "offload_guard_ratio": DEFAULT_GUARD_RATIO,

    # 诊断
    "probe": False,
}

# 自动策略表（§7）—— 只列两场景有差异的项，其余沿用 DEFAULT_PERF。
# ⚠ 这是**建议值不是强制值**：面板必须允许逐项覆盖（resolve_perf 的 overrides）。
PROFILE_TABLE = {
    PROFILE_CLOUD: {
        "blocks_to_swap": 0,
        "prefetch_blocks": 0,
        "non_blocking": False,
        "unload_unet_seg": False,
        "unload_before_decode": "auto",   # 跟随 _up_swap
        "decode_chunk_frames": 0,
        "frames_to_cpu": False,
        "max_upscale_scale": 4.0,
        "unload_upscaler_cache": False,
        "cond_cache_size": 32,
        "guard_offload_target": True,     # 场景 A 的**关键**项（无 swap 会 OOM kill）
    },
    PROFILE_LOCAL: {
        "blocks_to_swap": 25,
        "prefetch_blocks": 2,
        "non_blocking": True,
        "unload_unet_seg": True,
        "unload_before_decode": "auto",
        "decode_chunk_frames": 32,
        "frames_to_cpu": True,
        "max_upscale_scale": 1.5,
        "unload_upscaler_cache": True,
        "cond_cache_size": 8,
        "guard_offload_target": True,
    },
    PROFILE_CUSTOM: {},   # 全沿用 DEFAULT_PERF，等用户逐项覆盖
}

_AUTO_KEYS = tuple(k for k, v in DEFAULT_PERF.items() if v == "auto")

# 字段类型表 —— parse_state 的**唯一**校验依据。
#
# 为什么不从 DEFAULT_PERF 的值反推类型：`cond_cache_size` / `frames_to_cpu` 这类
# 默认值是字符串 "auto"，反推会得出「只收 str」，于是前端写回的整数 8 被当成
# 非法值丢掉（自动档字段永远改不动）。「auto」是这些字段的合法取值，不是它们的类型。
# 元组里的字符串 "auto" 表示**字面量** "auto" 合法（与 Python 类型并列）。
PERF_TYPES = {
    # 场景
    "profile": (str,),
    "preset": (str,),
    # 显存：权重流动
    "blocks_to_swap": (int,),
    "prefetch_blocks": (int,),
    "non_blocking": (bool,),
    # 显存：常驻
    "unload_unet_seg": (bool, "auto"),
    "unload_before_decode": (bool, "auto"),
    # 显存：峰值
    "decode_chunk_frames": (int,),
    "frames_to_cpu": (bool, "auto"),
    "max_upscale_scale": (int, float),
    # 内存
    "unload_upscaler_cache": (bool, "auto"),
    "cond_cache_size": (int, "auto"),
    # 磁盘 / swap 守卫
    "guard_offload_target": (bool,),
    "offload_guard_ratio": (int, float),
    # 诊断
    "probe": (bool,),
}


# ============================ 判据（纯函数） ============================

def ratio_v(hw):
    """R_v = UNET 权重 ÷ **可用**显存（总量 × VRAM_DRIVER_RESERVE）。

    <1 权重装得下（可常驻）；>1 必须流动（块交换 / 卸载）。
    量不到任一侧返回 None（**不猜**——猜出来的档位比没有档位更糟）。
    """
    unet = hw.get("unet_gb")
    vram = hw.get("vram_total_gb")
    if not unet or not vram:
        return None
    return float(unet) / (float(vram) * VRAM_DRIVER_RESERVE)


def ratio_m(hw):
    """R_m = (UNET + TE) ÷ 内存**总量**。

    >1 ⇒ 卸载下来的权重放不进内存，会被 OS 换出到 swap / pagefile（落盘）。
    用总量而非可用量：计划的实测口径即总量（51.9/32 = 1.62）。
    """
    unet = hw.get("unet_gb") or 0.0
    te = hw.get("te_gb") or 0.0
    ram = hw.get("ram_total_gb")
    total = float(unet) + float(te)
    if not total or not ram:
        return None
    return total / float(ram)


def ratio_d(hw):
    """R_d = 可用磁盘 ÷ (UNET + TE)。<1 ⇒ 连一次完整落盘都承受不了。"""
    unet = hw.get("unet_gb") or 0.0
    te = hw.get("te_gb") or 0.0
    disk = hw.get("disk_free_gb")
    total = float(unet) + float(te)
    if not total or disk is None:
        return None
    return float(disk) / total


def resolve_profile(hw):
    """按 R_v 定档（§2.2）：装得下 → cloud，装不下 → local。

    量不到 R_v 时返回 `custom` —— 探测失败就交给人，不自动套表。
    """
    rv = ratio_v(hw)
    if rv is None:
        return PROFILE_CUSTOM
    return PROFILE_CLOUD if rv <= 1.0 else PROFILE_LOCAL


def offload_guard(hw, ratio=DEFAULT_GUARD_RATIO):
    """落盘守卫（§3.3）：卸载需要 内存可用 + swap 空闲 ≥ 权重 × ratio。

    场景 A 的**静默致命点**：内存看似够，但云端常常没配 swap，而且磁盘本来就快满。
    一旦真落到 swap 上，结果**不是变慢，是进程被 OOM killer 直接杀掉**。
    本函数只读、不改行为，只负责把这件事提前说清楚。

    返回 dict：
        need_gb  需求（权重 × ratio）
        have_gb  内存可用 + swap 空闲
        ok       是否够
        level    "ok" | "warn" | "critical"
        message  一行中文结论（无数据时也给出可行动的提示）
    """
    unet = float(hw.get("unet_gb") or 0.0)
    te = float(hw.get("te_gb") or 0.0)
    weights = unet + te
    need = weights * float(ratio or DEFAULT_GUARD_RATIO)

    ram_avail = hw.get("ram_avail_gb")
    swap_free = hw.get("swap_free_gb")
    have = float(ram_avail or 0.0) + float(swap_free or 0.0)

    if not weights or ram_avail is None:
        return {"need_gb": round(need, 1), "have_gb": round(have, 1),
                "ok": True, "level": "ok",
                "message": "权重/内存未探明，落盘守卫跳过"}

    ok = have >= need
    if ok:
        level = "ok"
    elif have >= need * 0.7:
        level = "warn"
    else:
        level = "critical"

    swap_total = hw.get("swap_total_gb")
    if swap_free is None and swap_total is None:
        swap_txt = "未知"
    elif swap_free is None:
        swap_txt = f"?/{swap_total:.0f}GB"
    elif swap_total is None:
        swap_txt = f"{swap_free:.0f}/?GB"
    else:
        # 空闲/总量 并列写：真机日志里报告行打 total、守卫行打 free，两行相邻
        # 同名不同口径，被读成「前后矛盾」（2026-09-22）。这里强制成一对。
        swap_txt = f"{swap_free:.0f}/{swap_total:.0f}GB"
    disk = hw.get("disk_free_gb")
    disk_txt = "未知" if disk is None else f"{disk:.0f}GB"
    ram_txt = "未知" if ram_avail is None else f"{ram_avail:.0f}GB"

    # 平台分流：Windows 有 pagefile，内存压力的表现是「变慢」不是「被杀」；
    # Linux 无 swap 才是 OOM killer SIGKILL。未知平台按 Linux 给（更保守）。
    is_windows = str(hw.get("os") or "").lower().startswith("win")
    if is_windows:
        crit_tail = ("Windows 有 pagefile，不会 OOM kill，但会把采样拖到分钟级；"
                     "建议先关掉其它进程，或改用不卸载策略")
    else:
        crit_tail = ("本机 swap 不足，Linux 无 swap 时不是变慢而是被 OOM killer 杀掉"
                     "——建议先关掉其它进程，或改用不卸载策略")

    if ok:
        msg = (f"卸载可落内存：可用内存 {ram_txt} + swap {swap_txt} ≥ 需求 {need:.0f}GB")
    else:
        msg = (f"⚠ 本次卸载会把权重挤出到 swap —— 可用内存 {ram_txt} + swap {swap_txt}"
               f" < 需求 {need:.0f}GB（权重 {weights:.1f}GB × {ratio:g}）；"
               f"可用磁盘 {disk_txt}。"
               f"{crit_tail if level == 'critical' else '接近临界，建议先释放内存'}")

    return {"need_gb": round(need, 1), "have_gb": round(have, 1),
            "ok": ok, "level": level, "message": msg}


def suggest_blocks(unet_gb, vram_gb, block_count=H3_DOUBLE_BLOCK_COUNT,
                   reserve_gb=RESERVE_GB, usable=VRAM_USABLE):
    """要让 UNET 权重压进显存，得换出多少个 double block（§5.2）。

    模型：留驻 `unet - n*block`，要求 `留驻 + reserve ≤ vram*usable`，即
        n = ceil((unet + reserve - vram*usable) / block)，block = unet / 50

    标定（计划 §5.2 的参数表即本式的反解）：
        16GB / 26GB UNET → **25**（计划给 19–25，命中）✅
        12GB             → 32（计划 31–38）✅
        32GB             → 0 （装得下，不开）✅
        8GB              → 40（计划 44–50，本式偏保守；取小值更安全，
                              换块太多反而把 PCIe 压成瓶颈——见 §5.4）

    ⚠ 建议值不是强制值：PCIe 带宽才是硬约束，动手前必须先测
    「搬一块 vs 算一块」的耗时（阶段 4c 前置）。
    """
    try:
        unet = float(unet_gb)
        vram = float(vram_gb)
        n_blocks = int(block_count)
    except (TypeError, ValueError):
        return 0
    if unet <= 0 or vram <= 0 or n_blocks <= 0:
        return 0
    block = unet / n_blocks
    need = unet + float(reserve_gb) - vram * float(usable)
    if need <= 0:
        return 0
    return int(min(n_blocks, max(1, math.ceil(need / block))))


# ============================ 策略（纯函数） ============================

def resolve_perf(profile, hw=None, overrides=None):
    """产出完整 `ds.perf`：DEFAULT_PERF ← 场景表 ← 探测微调 ← 用户覆盖。

    优先级从低到高，后者覆盖前者。这样「自动档」和「逐项覆盖」共用一条路径，
    不会出现前端算一遍、后端算一遍的两套真源。

    profile="auto" / "跟随" 时按 hw 判定；判定不出 → custom（不套表）。
    """
    table = dict(DEFAULT_PERF)

    prof = profile
    if prof in (None, "", "auto") and hw:
        prof = resolve_profile(hw)
    if prof not in PROFILES:
        prof = PROFILE_CUSTOM
    table["profile"] = prof
    table.update(PROFILE_TABLE.get(prof, {}))

    # 探测微调：只有「场景 B 且探到了真实尺寸」才用公式替换表里的经验值；
    # 表里的 25 是标定值，公式在 16GB/26GB 上也给 25，两者一致，换成公式
    # 是为了让 12GB / 8GB 这类非标配置也能落到合理区间。
    if hw and prof == PROFILE_LOCAL:
        n = suggest_blocks(hw.get("unet_gb"), hw.get("vram_total_gb"))
        if n:
            table["blocks_to_swap"] = n

    # 用户覆盖：只认已知键，None 视为「没填」（不能把字段刷成 None）
    for k, v in (overrides or {}).items():
        if k in DEFAULT_PERF and v is not None:
            table[k] = v

    return table


def resolve_auto(value, profile, cloud_value, local_value):
    """把 "auto" 解析成场景值（供后端消费 perf 字段时用）。

    `"auto"` 的语义是「跟随场景」，但**只有后端知道场景**——前端拿到的
    perf 里这些字段仍是 "auto"，由本函数在消费点就地解析，
    避免前端自己 if-else 出第二套口径。
    """
    if value != "auto":
        return value
    return cloud_value if profile == PROFILE_CLOUD else local_value


def parse_state(raw):
    """归一化前端/存档里读到的 perf 片段（阶段 1 面板写回同一入口）。

    - 非 dict / 空 → 一份默认副本（**不改 DEFAULT_PERF 本体**，否则一次解析
      就污染了全局默认值）
    - 未知键丢弃（防止旧存档里的废弃字段一路带到渲染路径）
    - 类型不对的键整体丢弃该项（回落到默认），不做猜测式强转
    - 校验依据是 PERF_TYPES，不是 DEFAULT_PERF 的默认值类型
    """
    out = dict(DEFAULT_PERF)
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        types = PERF_TYPES.get(k)
        if types is None or v is None:
            continue
        if "auto" in types and v == "auto":
            out[k] = "auto"
            continue
        # bool 先判：bool 是 int 的子类，反过来会把 True 当成 1 存进 int 字段
        if bool in types and isinstance(v, bool):
            out[k] = v
            continue
        # int / float 是「或」不是「先到先得」：(int, float) 字段收到 1.5 时，
        # int 分支判不合法后必须继续往下走 float 分支，不能直接 continue 掉
        if not isinstance(v, bool):
            if int in types and (isinstance(v, int)
                                 or (isinstance(v, float) and v.is_integer())):
                out[k] = int(v)     # JSON 里 8.0 就是 8
                continue
            if float in types and isinstance(v, (int, float)):
                out[k] = v
                continue
        if str in types and isinstance(v, str):
            out[k] = v
    return out


def report_line(hw, profile=None):
    """一行硬件概览（阶段 0 报告行，§6.3 第 1 行同源）：

    `[H3性能] 显存 32.0GB · 内存 80GB · swap 0GB（空闲 0 · 源 linux-meminfo）`
    ` · R_v 0.83 · R_m 0.65 · 档位 云端高显存 · 平台 Linux`

    缺项显示 "?" 而不是编一个数字——诊断行的价值全在可信。

    swap 同时给「总量（空闲）」：只给总量时，与守卫行（只给空闲）相邻会出现
    21GB / 0GB 这种看似矛盾的读数（2026-09-22 真机）。末尾的「平台」是判定
    落盘守卫该给哪套建议的唯一依据，必须可见。
    """
    def _g(key, nd=1):
        v = hw.get(key)
        return "?" if v is None else f"{float(v):.{nd}f}"

    rv = ratio_v(hw)
    rm = ratio_m(hw)
    prof = profile or resolve_profile(hw)
    sf = hw.get("swap_free_gb")
    sf_txt = "?" if sf is None else f"{sf:.0f}"
    # swap 后面标「源」：windows=GlobalMemoryStatusEx、linux-meminfo=/proc/meminfo、
    # psutil=兜底。真机排障时「这数哪来的」与「这数是多少」同等重要——Windows 的
    # pagefile 与 Linux 的 swap 不是一回事，认错分支整套建议都会跑偏。
    return (f"[H3性能] 显存 {_g('vram_total_gb')}GB · 内存 {_g('ram_total_gb', 0)}GB"
            f" · swap {_g('swap_total_gb', 0)}GB（空闲 {sf_txt}"
            f" · 源 {hw.get('swap_source') or '?'}）"
            f" · R_v {'?' if rv is None else f'{rv:.2f}'}"
            f" · R_m {'?' if rm is None else f'{rm:.2f}'}"
            f" · 档位 {PROFILE_LABELS.get(prof, prof)}"
            f" · 平台 {hw.get('os') or '?'}")


# ============================ 探测（best-effort，绝不抛） ============================

def _read_proc_meminfo():
    """Linux /proc/meminfo → {key_kB: int}；非 Linux / 读不到 → {}。"""
    try:
        out = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                k, rest = line.split(":", 1)
                parts = rest.split()
                if parts and parts[0].isdigit():
                    out[k.strip()] = int(parts[0])
        return out
    except Exception:
        return {}


def _ram_swap_windows():
    """Windows：GlobalMemoryStatusEx。

    ullTotalPageFile = 物理内存 + 页面文件（系统 commit 上限），故
        swap_total = ullTotalPageFile − ullTotalPhys
    ullAvailPageFile = 还能 commit 的总量（物理 + 页面文件合计），故
        swap_free  = ullAvailPageFile − ullAvailPhys
    —— 减的是 **AvailPhys** 不是 TotalPhys：AvailPageFile 是「总的可 commit 余量」，
    其中能被物理内存承接的那部分要扣掉，剩下的才是页面文件的空位。
    （实测：16GB 内存 + 19GB pagefile，AvailPageFile 7.6 / AvailPhys 5.1
    → swap_free 2.5GB；若误减 TotalPhys 会因减出负数被 clamp 成 0，
    把「swap 还有点余量」误报成「swap 已满」。）

    ⚠ 该式在「大量 mmap 文件页」时会**高估**：ComfyUI 用 safetensors mmap 映射几十 GB
    权重，这些页计入 PhysInUse 但不计入 CommitTotal，于是式子变成
    `PF + (PhysInUse − CommitTotal)` 并可能**超过页面文件总容量**。故结果必须
    再 clamp 到 [0, pagefile]——否则日志里会出现「swap 空闲 > swap 总量」的怪值。
    （真机 2026-09-22 日志：swap 21GB / free 0GB，即命中下界。）
    """
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None, None
        phys = st.ullTotalPhys / GB
        pagefile = max(0.0, (st.ullTotalPageFile - st.ullTotalPhys) / GB)
        avail_pagefile = (st.ullAvailPageFile - st.ullAvailPhys) / GB
        # 双向 clamp：下界 0（负数无意义），上界 pagefile（mmap 文件页会把它顶穿）
        avail_pagefile = min(max(0.0, avail_pagefile), pagefile)
        return ({"total_gb": phys, "avail_gb": st.ullAvailPhys / GB},
                {"total_gb": pagefile, "free_gb": avail_pagefile})
    except Exception:
        return None, None


def _ram_swap_linux():
    info = _read_proc_meminfo()
    if not info:
        return None, None
    try:
        ram = {"total_gb": info["MemTotal"] * 1024.0 / GB,
               "avail_gb": info.get("MemAvailable", info.get("MemFree", 0)) * 1024.0 / GB}
        swap = {"total_gb": info.get("SwapTotal", 0) * 1024.0 / GB,
                "free_gb": info.get("SwapFree", 0) * 1024.0 / GB}
        return ram, swap
    except Exception:
        return None, None


def probe_os():
    """平台名（"Windows" / "Linux" / …）；探不到 → None。

    为什么必须有：落盘守卫的结论在 Windows 与 Linux 上**完全相反**——Windows 有
    pagefile，内存压力的表现是「变慢」；Linux 无 swap 时是「进程被 OOM killer
    SIGKILL」。不看平台就给建议，等于把一台机器的话术套到另一台上（2026-09-22
    真机日志：报告行 swap 21GB / 守卫行 swap 0GB，用户据此怀疑系统检测错了，
    而日志里根本没有平台信息可供判断）。
    """
    try:
        import platform

        return platform.system() or None
    except Exception:
        return None


@contextlib.contextmanager
def watch_vram_peak(interval=0.2):
    """区间内**设备级**显存占用的采样峰值（GB）；量不到 → 峰值保持 None。

    为什么不能用 `torch.cuda.max_memory_allocated()`：它只统计 torch 的 caching
    allocator，量不到 DynamicVRAM / aimdo 的权重池（见 `upscale._vram_probe`），
    在 24GB 卡跑 32GB 模型时会报 0.43GB 这种假象。

    为什么必须用采样线程：设备级占用只有**瞬时值**（`mem_get_info` 给 free/total，
    没有 peak 计数器），而 nvidia-ml / NVML 在相当多机器上初始化失败，
    `nvidia-smi` 也不可用。故起一个 daemon 线程定时轮询取 max——
    每次轮询 ~10µs，0.2s 一次，对采样中的前向无可测影响。

    用法（`yield` 的是一个可变 dict，退出后读 `["peak"]`）：

        with perf.watch_vram_peak() as w:
            ...采样...
        print(w["peak"])

    只回答一个问题：**这段区间里显存最高到过多少**。它是「不腾挪 / 少腾挪
    会不会 OOM」这类决策的唯一硬依据——没有它，A/B 只能靠崩不崩来试。
    """
    box = {"peak": None, "samples": 0}
    stop = threading.Event()

    def _loop():
        try:
            import torch
            if not torch.cuda.is_available():
                return
            while not stop.is_set():
                try:
                    free, total = torch.cuda.mem_get_info()
                    used = (total - free) / GB
                    box["samples"] += 1
                    if box["peak"] is None or used > box["peak"]:
                        box["peak"] = used
                except Exception:
                    return
                stop.wait(interval)
        except Exception:
            return

    th = threading.Thread(target=_loop, daemon=True)
    try:
        th.start()
    except Exception:
        pass
    try:
        yield box
    finally:
        stop.set()


_LAST_SWAP_SOURCE = "none"


def probe_ram_swap():
    """(ram, swap) 两个 dict（GB）；探不到给 None。Linux 优先 /proc/meminfo。

    走到哪条分支记进 `perf._LAST_SWAP_SOURCE`（"windows" / "linux-meminfo" /
    "psutil" / "none"），供报告行标注——真机排障时「这个数字是哪条路来的」
    和「这个数字是多少」同样重要。
    """
    global _LAST_SWAP_SOURCE
    ram = swap = None
    if os.name == "nt":
        ram, swap = _ram_swap_windows()
        _LAST_SWAP_SOURCE = "windows" if ram is not None else "none"
    if ram is None:
        ram, swap = _ram_swap_linux()
        _LAST_SWAP_SOURCE = "linux-meminfo" if ram is not None else "none"
    if ram is None:
        # 最后兜底：psutil（ComfyUI 环境常见，但不是必须）
        try:
            import psutil  # type: ignore

            vm = psutil.virtual_memory()
            sm = psutil.swap_memory()
            ram = {"total_gb": vm.total / GB, "avail_gb": vm.available / GB}
            swap = {"total_gb": sm.total / GB, "free_gb": sm.free / GB}
            _LAST_SWAP_SOURCE = "psutil"
        except Exception:
            pass
    return ram, swap


def rss_gb():
    """本进程常驻内存 RSS（GB）；量不到 → None。

    「多段变卡」的唯一可靠线索：显存只看得见 GPU 侧的涨落，而**内存泄漏 /
    缓存累积**在显存账上完全隐形（§5.5）。RSS 曲线与显存峰值同批采样，
    两个曲线一起看才分得清「显存真不够」还是「内存被吃光导致换页」。

    RSS 含共享页，会略微高估独占用量——但我们是看**跨段增量**，不是绝对值，
    共享页基本恒定，不影响结论。
    """
    # psutil 优先：一行到位，ComfyUI 环境常见
    try:
        import psutil  # type: ignore

        return psutil.Process().memory_info().rss / GB
    except Exception:
        pass
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            # ⚠ 64 位 Windows 上 ctypes 的默认 restype 是 c_int、默认整型参数是 32 位，
            # 会把 HANDLE 截断：GetCurrentProcess() 返回的伪句柄 -1（64 位全 1）
            # 被缩成 0x00000000FFFFFFFF，GetProcessMemoryInfo 静默返回 0。
            # 实测：不声明 restype/argtypes 时拿不到 RSS（返回 None）。
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.GetCurrentProcess.argtypes = []
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                                   ctypes.c_void_p, wintypes.DWORD]

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD),
                            ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            c = PROCESS_MEMORY_COUNTERS()
            c.cb = ctypes.sizeof(c)
            if psapi.GetProcessMemoryInfo(
                    kernel32.GetCurrentProcess(),
                    ctypes.cast(ctypes.byref(c), ctypes.c_void_p), c.cb):
                return c.WorkingSetSize / GB
            return None
        # Linux：VmRSS（kB）。statm 也能算但要乘页长，status 更直白
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024.0 / GB
        return None
    except Exception:
        return None


def probe_disk_free(path=None):
    """可用磁盘 GB（输出/模型所在盘）；拿不到 → None。"""
    try:
        p = path or (os.path.dirname(os.path.abspath(__file__)) or ".")
        if hasattr(os, "statvfs"):
            st = os.statvfs(p)
            return st.f_bavail * st.f_frsize / GB
        import ctypes

        free = ctypes.c_ulonglong()
        root = os.path.splitdrive(os.path.abspath(p))[0] + os.sep
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(root), None, None, ctypes.byref(free))
        return free.value / GB if ok else None
    except Exception:
        return None


def probe_vram():
    """(total_gb, free_gb) 显存；无 CUDA → (None, None)。

    ⚠ `nvidia-smi` 在部分机器不可用（NVML 初始化失败），故统一走 torch 计数器——
    与 upscale._vram_probe 同源口径。
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return None, None
        free, total = torch.cuda.mem_get_info()
        return total / GB, free / GB
    except Exception:
        try:
            import torch

            if torch.cuda.is_available():
                props = torch.cuda.get_device_properties(0)
                return props.total_memory / GB, None
        except Exception:
            pass
        return None, None


def probe_aimdo():
    """DynamicVRAM（aimdo）真开关；量不到 → None（不是 False）。

    与 upscale._dynamic_vram_active 同口径：读 `comfy.memory_management.aimdo_enabled`。
    ⚠ 不能查 `sys.modules` 里有没有 aimdo 包——comfy.model_management 顶层就 import 它，
    那种查法恒为 True（P0-3 的坑）。
    """
    try:
        from comfy import memory_management  # type: ignore

        return bool(getattr(memory_management, "aimdo_enabled", False))
    except Exception:
        return None


def probe_vram_policy():
    """DynamicVRAM 的「余量策略」参数（只读镜像）；拿不到 → None。

    为什么必须单独探它：ComfyUI 0.35 起把重心放在**拉高显存利用率**上
    （官方 issue #16150 里维护者的定性是「H3 显存利用率不足是自始的 bug，
    Comfy 编译器就是来修它的」，不是回归）。结果是低显存卡上余量被吃光、
    二采这种「在同一张卡上再插一段高清采样」的流程最容易炸。

    相关旋钮（全部在 comfy/cli_args.py，本函数只读不改）：

    | 键 | 来源 | 作用面 |
    |---|---|---|
    | `vram_headroom_gb` | `--vram-headroom`（0.35 新增，默认 0） | `main.py` 交给 `comfy_aimdo.control.init_devices` 的**每设备**预留 |
    | `reserve_vram_gb` | `--reserve-vram`（老开关） | 同时喂 Python 侧 `EXTRA_RESERVED_VRAM` 与 aimdo 的 `simple_vram_headroom` |
    | `extra_reserved_gb` | 运行时可变 | 只影响 Python 侧 `minimum_inference_memory()` = 0.8GB + 它 |
    | `comfy_compiler` | `--disable-comfy-compiler` | 0.35 新增的编译器，正是拉高占用的主因 |

    ⚠ 「运行时改 `EXTRA_RESERVED_VRAM`」（常见第三方 ReservedVRAM 节点）**盖不到
    aimdo 原生那侧**——原生预留是进程启动时设定的。要真留出余量得给启动参数。
    """
    try:
        from comfy import cli_args  # type: ignore

        a = cli_args.args
        extra = None
        try:
            from comfy import model_management as mm  # type: ignore

            extra = mm.extra_reserved_memory() / GB
        except Exception:
            pass
        return {
            "vram_headroom_gb": float(getattr(a, "vram_headroom", 0) or 0.0),
            "reserve_vram_gb": getattr(a, "reserve_vram", None),
            "extra_reserved_gb": extra,
            "comfy_compiler": not bool(getattr(a, "disable_comfy_compiler", False)),
            "dynamic_vram": bool(cli_args.enables_dynamic_vram()),
        }
    except Exception:
        return None


# `minimum_inference_memory()` 的固定项（comfy/model_management.py:877）：
# 0.8GB + extra_reserved_memory()。load_models_gpu 用它当「最低必须留空」的地板。
MIN_INFERENCE_BASE_GB = 0.8


def vram_budget(hw, policy=None):
    """显存预算账（纯函数，只吃 dict）：留了多少 / 能给权重多少 / 每步要重读多少。

    回答的是**「到底有没有把显存用满」**——这个问题的另一半是「0.35 想吃满，
    我挡住了多少」。

    机制（comfy/model_management.py）：

        minimum_inference_memory() = 0.8GB + extra_reserved_memory()
        load_models_gpu:  minimum_memory_required = max(inference_memory,
                                                        memory_required + extra)

    也就是 `EXTRA_RESERVED_VRAM` 被**数了两次**（保底项与需求项各一次），
    所以运行时把它抬到 2.88GB 的实际后果是「至少留空 3.68GB」，占 24GB 卡的 15%。

    ⚠ 关键语义：DynamicVRAM 下显存是**权重缓存**。留空的每一 GB 都等于每步要多
    重读 1GB 权重（模型 34GB 装不进 24GB，缓存越大、每步重读越少、越快）。
    所以「用满」不是无脑正确的目标——激活（736×1312×57 高清 latent）需要一块
    固定空间，把它挤掉就是 OOM。正确目标是「**缓存尽量大 + 激活峰值刚好够**」。

    返回 {total_gb, reserve_gb, cache_gb, unet_gb, reread_gb}；关键项缺就 None。
    """
    total = hw.get("vram_total_gb")
    if not total:
        return None
    extra = (policy or {}).get("extra_reserved_gb")
    reserve = None if extra is None else MIN_INFERENCE_BASE_GB + float(extra)
    cache = None if reserve is None else max(0.0, float(total) - reserve)
    unet = hw.get("unet_gb")
    reread = None
    if unet and cache is not None:
        reread = max(0.0, float(unet) - cache)
    return {"total_gb": float(total), "reserve_gb": reserve, "cache_gb": cache,
            "unet_gb": unet, "reread_gb": reread}


def vram_budget_line(b):
    """把 vram_budget 渲染成一行（无数据返回 ""）。"""
    if not b:
        return ""
    if b.get("reserve_gb") is None:
        return f"显存预算：总量 {b['total_gb']:.1f}GB · 预留未探明（拿不到 EXTRA_RESERVED_VRAM）"
    txt = (f"显存预算：总量 {b['total_gb']:.1f}GB · 主动留空 {b['reserve_gb']:.2f}GB"
           f"（保底 {MIN_INFERENCE_BASE_GB:g} + EXTRA {b['reserve_gb'] - MIN_INFERENCE_BASE_GB:.2f}）"
           f" · 可给权重缓存 ≈ {b['cache_gb']:.1f}GB")
    if b.get("reread_gb") is not None:
        txt += (f" · 模型 {b['unet_gb']:.1f}GB 装不下 → 每步需重读 ≈ {b['reread_gb']:.1f}GB"
                f"（{b['reread_gb'] / b['unet_gb']:.0%} 的权重）")
    return txt


def vram_slack_hint(peak_gb, total_gb):
    """精化峰值出来后，给一句「预留该不该调」的判据（纯函数；不提则 None）。

    这是「有没有把显存用满」的**实测**答案：`总量 − 精化峰值` 就是真正没被用上的
    那一块。DynamicVRAM 下显存是权重缓存，留空的每 GB 都等于每步多重读 1GB 权重，
    所以余量长期猜不出来、只能量——这也是 `watch_vram_peak` 存在的理由。
    """
    try:
        peak, total = float(peak_gb), float(total_gb)
    except (TypeError, ValueError):
        return None
    if peak <= 0 or total <= 0:
        return None
    slack = total - peak
    if slack >= 4.0:
        return (f"显存没吃满：精化峰值 {peak:.1f}GB / 总量 {total:.1f}GB，"
                f"空着 {slack:.1f}GB —— 留空的每 GB 都在让每步多重读 1GB 权重。"
                f"可下调 EXTRA_RESERVED_VRAM（或改用 --vram-headroom 精确控制），"
                f"把省下的给权重缓存")
    if slack <= 2.0:
        return (f"显存已吃到临界：精化峰值 {peak:.1f}GB / 总量 {total:.1f}GB，"
                f"只剩 {slack:.1f}GB —— 不要再压预留，再压就得靠手动腾挪兜底了")
    return None


def vram_headroom_advice(hw, policy=None):
    """余量策略是否「有风险」——有则回一行可执行的中文建议，否则 None。

    纯函数（只吃 dict）。判定刻意保守：**只有「DynamicVRAM 开着 + headroom 为 0
    + 没给 --reserve-vram」同时成立**才提示。这三条都满足时，0.35 的默认行为会
    把显存吃到接近 100%，靠 `unload_all_models()` 这类手动腾挪才勉强活下来
    ——那正是本项目二采路径一堆腾挪代码的由来。
    """
    if not policy or not policy.get("dynamic_vram"):
        return None
    hr = policy.get("vram_headroom_gb") or 0.0
    rv = policy.get("reserve_vram_gb")
    if hr > 0 or (rv is not None and float(rv) > 0):
        return None
    bits = []
    if policy.get("comfy_compiler"):
        bits.append("Comfy 编译器在跑（0.35 新增，官方定位就是拉高 H3 显存利用率）")
    return ("⚠ 显存余量策略为默认：DynamicVRAM 已启用但 `--vram-headroom` 为 0、"
            "也未给 `--reserve-vram`——0.35 起显存会被吃到接近 100%，二采"
            "（高清采样 + 放大网络）最先炸。"
            + ("；".join(bits) + "。" if bits else "")
            + "建议启动加 `--vram-headroom 1`；仍不稳再加 `--disable-comfy-compiler`"
              "（实测显存 100% → ~77%，代价约 5% 耗时）。"
              "注意：运行时改 EXTRA_RESERVED_VRAM 的第三方节点只影响 Python 侧记账，"
              "盖不到 aimdo 原生预留。")


def probe_attn_backend():
    """当前 attention 后端名（只读镜像用）；量不到 → None。

    §5.1：kitchen attention / sol-attn 是**已采用**的优化，面板只做只读镜像，
    不另造平行开关 —— 所以这里只问名字，不提供 setter。
    """
    try:
        import comfy.ldm.modules.attention as attn  # type: ignore

        fn = getattr(attn, "optimized_attention", None)
        return getattr(fn, "__name__", None) or None
    except Exception:
        return None


def probe_pcie():
    """PCIe 链路 {gen, width}；量不到 → None。

    §5.4：块交换唯一的硬约束是 PCIe 带宽（PCIe 3.0 x8 ≈ 6GB/s 会拖后腿）。
    Linux 从 sysfs 读；Windows 无稳定用户态接口 → None（阶段 4c 用实测基准代替）。
    """
    try:
        if os.name == "nt":
            return None
        base = "/sys/bus/pci/devices"
        for dev in os.listdir(base):
            try:
                with open(os.path.join(base, dev, "class"), encoding="utf-8") as f:
                    cls = f.read().strip()
            except Exception:
                continue
            # 0x030000 = VGA / 0x030200 = 3D controller
            if not cls.startswith("0x0300") and not cls.startswith("0x0302"):
                continue
            out = {}
            for key, fn in (("width", "current_link_width"), ("gen", "current_link_speed")):
                try:
                    with open(os.path.join(base, dev, fn), encoding="utf-8") as f:
                        out[key] = f.read().strip()
                except Exception:
                    pass
            if out:
                return out
    except Exception:
        pass
    return None


def weight_bytes(obj):
    """模型权重总字节（duck-typed，**不 import torch**，故本模块仍零依赖）。

    **优先 `ModelPatcher.model_size()`**——这是 ComfyUI 自己算「模型多大」的口径，
    也是 `load_models_gpu` 决定要不要卸载时的依据。R_v / R_m 回答的正是
    「能不能常驻 / 卸载会不会落盘」，用别的口径等于拿一把没校准过的尺子去量
    另一个系统的判据。

    回落路径（CLIP / 裸模块 / 老版本）：ModelPatcher → `.model`，CLIP →
    `.cond_stage_model`，都不是就用对象本身，按张量 element_size 累加。
    拿不到返回 None（绝不猜——R_v 算错比不算更糟）。

    ⚠ 为什么必须换口径（2026-09-22 真机）：按 element_size 累加在同一份日志里
    推出 UNET 61.6GiB / TE 48.1GiB（合计 109.7GB），而 ComfyUI 自己报的是
    MiniMaxH3 32427MiB + MiniMaxH3TEModel_ 14956MiB（合计约 47GB），差 2.3 倍。
    量化/混合精度模型（`convrot_w4a4` / `int8_tensorwise` / fp8）在 CPU 侧的
    张量布局与 staged 到设备上的布局不是一回事，累加 element_size 会系统性偏高，
    进而让 R_v / R_m 和落盘守卫的 132GB 需求全部虚高。
    """
    fn = getattr(obj, "model_size", None)
    if callable(fn):
        try:
            v = int(fn())
            if v > 0:
                return v
        except Exception:
            pass
    inner = obj
    for name in ("model", "cond_stage_model"):
        cand = getattr(obj, name, None)
        if cand is not None and hasattr(cand, "parameters"):
            inner = cand
            break
    try:
        total = 0
        for p in inner.parameters():
            total += int(p.numel()) * int(p.element_size())
        return total or None
    except Exception:
        return None


@contextlib.contextmanager
def watch_model_loads(mark=_MODEL_LOAD_MARK):
    """统计区间内 ComfyUI 打了多少次「Requested to load …」（只读探测）。

    用途（计划 §5.3）：二采模型与一采**同 base、只差 LoRA** 时，代码判为非独立权重、
    不触发 UNET 级换页 —— 但那是**代码的判断，不是实测**：LoRA 补丁重新应用会不会
    导致权重重传，取决于 ComfyUI 的 patch 机制，必须由这条探测来证实：

        0 次  → 确认零换页，两遍式（先跑完一采再跑二采）没有省头，不必做
        >0 次 → 确实在重传，两遍式重新有价值（2N → 1）

    只装 handler、不改任何行为；退出时移除 handler 并**还原 root logger 级别**。
    """
    hits = []

    class _Catcher(logging.Handler):
        def emit(self, record):
            try:
                msg = record.getMessage()
            except Exception:
                return
            if mark in msg:
                hits.append(msg)

    root = logging.getLogger()
    handler = _Catcher(level=logging.INFO)
    prev_level = root.level
    root.addHandler(handler)
    # 日志级别高于 INFO 时记录根本不会生成，handler 就永远看不到 —— 临时降下来。
    # ComfyUI 默认就是 INFO，所以实际是 no-op；退出时无条件还原。
    if prev_level > logging.INFO:
        root.setLevel(logging.INFO)
    try:
        yield hits
    finally:
        root.removeHandler(handler)
        if root.level != prev_level:
            root.setLevel(prev_level)


def classify_loads(hits, unet_name=None):
    """把 `watch_model_loads` 抓到的原始行按模型名归类。

    ComfyUI 打的是 `Requested to load {model.__class__.__name__}`
    （comfy/model_management.py:964），故名可解析。分类的意义在于把
    「UNET 回载了几次」从「本段总共回载了几次」里**分离**出来：

    - 总次数里混着 TE（建高清 cond）与 VAE（解码）的回载，那两次是代码主动
      触发的既定流程，与「换 LoRA 会不会重传权重」毫无关系；
    - 只有 **UNET ≥2 次**才是重传的证据——第一次是精化前那次主动
      `unload_all_models()` 之后必然的回载。

    返回 {"total": int, "by_model": {name: n}, "unet_loads": int}。
    `unet_name` 给不出（None / 没匹配上）时 unet_loads 记 0，由调用方据此
    判定「本次观测不足以支撑结论」，而不是硬凑一个结论出来。
    （2026-09-22 真机即栽在这里：总次数 3 被直接读成「确实在重传 → 两遍式
    有价值」，实际三次全是既定流程。）
    """
    counts = {}
    for m in hits or ():
        name = str(m).split(_MODEL_LOAD_MARK, 1)[-1].strip() or "?"
        counts[name] = counts.get(name, 0) + 1
    return {"total": len(hits or ()), "by_model": counts,
            "unet_loads": int(counts.get(unet_name, 0)) if unet_name else 0}


# ============================ 探测日志（崩溃安全） ============================
#
# 为什么必须有它：云端显存不足的下场是 **OOM killer 发 SIGKILL** —— 没有 except、
# 没有 finally、没有 atexit，堆在内存里等着段末统一打印的汇总行**一定丢失**。
# 而最需要看数据的恰恰就是崩掉的那一段。
# 故：每采一个点就 append 一行 JSON 并 flush（SIGKILL 不丢已写入 OS 的数据），
# 事后用 tools/perf_report.py 回读。stdout 只管「跟着日志走」，落盘只管「不丢」。
#
# 顺序（从最不丢到最丢）：JSONL 落盘 > 逐点 print > 段末汇总行。
# 三条都做，任一条活下来都够用。

LOG_ENV = "H3_PERF_LOG"          # 环境变量覆盖路径；设 "0" 关闭
LOG_NAME = "h3_perf_probe.jsonl"
LOG_MAX_BYTES = 4 * 1024 * 1024  # 超过先滚一份 .1，避免无限增长
LOG_KEEP_LINES = 20000           # 回读上限（只取尾部，防止老机器读爆内存）

_run_id = None


def run_id():
    """本次进程唯一标识（回读时按它区分「哪一次运行」）。"""
    global _run_id
    if _run_id is None:
        _run_id = "%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid())
    return _run_id


def log_path():
    """JSONL 落盘路径。默认 <插件>/logs/h3_perf_probe.jsonl。"""
    override = os.environ.get(LOG_ENV)
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "logs", LOG_NAME)


def emit(event, path=None):
    """追加一条 JSON 事件并**立即 flush**。

    任何情况都不抛（探测不能成为故障源）：路径不可写 / JSON 序列化失败一律静默。
    返回 True/False 只为便于自测，调用方无需检查。
    """
    try:
        p = path or log_path()
        if p == "0":
            return False
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        try:   # 体积护栏：先滚一份 .1
            if os.path.getsize(p) > LOG_MAX_BYTES:
                os.replace(p, p + ".1")
        except OSError:
            pass
        rec = {"t": round(time.time(), 3),
               "ts": time.strftime("%H:%M:%S"),
               "run": run_id()}
        rec.update(event)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            # flush 已经把数据交给 OS —— **抗 SIGKILL（OOM kill）靠它就够了**。
            # fsync 只多防「机器断电 / 内核崩」，但云端网络盘上可能明显拖慢
            # （每个埋点一次，卡在渲染关键路径上不值得）→ 默认不做，按需开。
            if os.environ.get("H3_PERF_FSYNC") == "1":
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
        return True
    except Exception:
        return False


def read_events(path=None, run=None, limit=LOG_KEEP_LINES):
    """回读事件列表。run 为空时自动取**最后一次**运行。

    坏行（写到一半被 kill 的那种）直接跳过——宁可少一条，也不能让报告读不出来。
    """
    p = path or log_path()
    try:
        with open(p, encoding="utf-8") as f:
            raw = f.readlines()[-limit:]
    except OSError:
        return []
    events = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    if run is None and events:
        run = events[-1].get("run")
    return [e for e in events if e.get("run") == run] if run else events


def list_runs(path=None, limit=LOG_KEEP_LINES):
    """文件里出现过的所有运行标识（按时间顺序）。"""
    p = path or log_path()
    try:
        with open(p, encoding="utf-8") as f:
            raw = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in raw:
        try:
            r = json.loads(line).get("run")
        except Exception:
            continue
        if r and r not in out:
            out.append(r)
    return out


# 六段的固定顺序（与 _vram_report 一致）
STAGES = ("base", "up", "cond", "unload", "refine", "decode")
STAGE_LABELS = ("放大前", "放大后", "cond后", "卸载后", "精化后", "解码后")
# 历史口径：「放大前 / 卸载后」报当时占用，其余报阶段峰值。
# ⚠ 2026-09-22 起**不再用于展示**——「峰值」那一列量不到 DynamicVRAM 权重池
# （upscale._vram_probe），六段已统一成设备级占用。保留该常量只为回读老日志。
ALLOC_STAGES = ("base", "unload")

# 耗时四阶段 + 腾挪（与 upscale.render_segment 的 _timing 键一致）
#
# "unload" 是 2026-09-22 加的：精化前那次 `unload_all_models()` 到底值不值，
# 需要它自己的耗时做分母（腾挪不是免费的，卸载调用本身也要搬权重回 CPU）。
TIMING_KEYS = ("up", "cond", "unload", "refine", "decode")
TIMING_LABELS = ("放大", "条件", "腾挪", "精化", "解码")


def summarize(events):
    """把事件列表渲染成跨段对照报告（文本）。崩溃后回读全靠它。

    看点是**同一列往下有没有往上爬**：段1 解码后 vs 段9 解码后，
    爬就是累积（内存泄漏 / 缓存只增），平就是稳定。
    """
    if not events:
        return ["（无探测数据）"]

    out = []
    over = [e for e in events if e.get("kind") == "overview"]
    if over:
        o = over[-1]
        out.append("硬件概览：" + o.get("line", ""))
        if o.get("guard"):
            out.append("落盘守卫：" + o["guard"])
        if o.get("headroom"):
            out.append("余量策略：" + o["headroom"])
        if o.get("budget"):
            out.append(o["budget"])
        out.append("")

    segs = {}
    for e in events:
        if e.get("kind") != "mark":
            continue
        segs.setdefault(e.get("seg"), {})[e.get("stage")] = e

    for title, key, pick in (("显存占用（设备级 GB）", "vram", _pick_vram),
                             ("内存 RSS（GB）", "rss", _pick_rss)):
        rows = []
        for seg in sorted(s for s in segs if s is not None):
            rows.append([str(seg)] + [pick(segs[seg].get(st)) for st in STAGES])
        if not rows:
            continue
        out.append(title)
        out.append("  段   " + "  ".join(f"{l:>7}" for l in STAGE_LABELS))
        for r in rows:
            out.append("  " + r[0].rjust(3) + "   " + "  ".join(f"{c:>7}" for c in r[1:]))
        out.append("")

    # 耗时表：「多段变卡」归根结底是时间现象，字节曲线看不出来
    tims = {e.get("seg"): e for e in events if e.get("kind") == "timing"}
    if tims:
        out.append("各段耗时（秒）")
        out.append("  段   " + "  ".join(f"{l:>7}" for l in TIMING_LABELS)
                   + f"{'合计':>9}")
        for seg in sorted(s for s in tims if s is not None):
            e = tims[seg]
            vals = [e.get(k) for k in TIMING_KEYS]
            cells = [f"{v:7.1f}" if isinstance(v, (int, float)) else f"{'n/a':>7}"
                     for v in vals]
            tot = sum(v for v in vals if isinstance(v, (int, float)))
            out.append("  " + str(seg).rjust(3) + "   " + "  ".join(cells)
                       + f"{tot:9.1f}")
        out.append("")

    # 精化峰值（设备级）：与「腾挪回收量」配着看，是「该不该在精化前全卸」的判据
    peaks = [e for e in events if e.get("kind") == "refine_peak"]
    if peaks:
        out.append("精化显存峰值（设备级 GB）")
        for e in peaks:
            v = e.get("vram_used")
            t = e.get("vram_total")
            cell = "n/a" if v is None else f"{v:.2f}"
            if v is not None and t:
                cell += f" / {t:.1f}（空 {t - v:.1f}）"
            out.append(f"  段{str(e.get('seg')).rjust(3)}   {cell}")
            if e.get("hint"):
                out.append(f"        {e['hint']}")
        out.append("")

    # 放大前腾挪：为把放大网络搬上卡而做的一次**全卸**，代价与收益（那 659MB）
    # 完全不成比例——打出来才判断得了值不值
    pres = [e for e in events if e.get("kind") == "pre_unload"]
    if pres:
        out.append("放大前腾挪（为搬放大网络上卡而全卸）")
        for e in pres:
            out.append(f"  段{str(e.get('seg')).rjust(3)}   "
                       f"放大网络 {e.get('net_mb') or 0:.0f}MB"
                       f" · 全卸耗时 {e.get('seconds')}s")
        out.append("")

    # 本地 LLM（提示词优化）：加速项是否生效 + 显存账 + 吞吐。
    # 这一段原本完全没有数据——llama.cpp 用自己的 CUDA 分配器（torch 看不见），
    # 掉层又是静默的，不看这几个数就只剩「感觉插件好慢」这一种反馈。
    llms = [e for e in events if e.get("kind") == "local_llm"]
    if llms:
        out.append("本地 LLM（提示词优化）")
        for e in llms:
            mg = e.get("model_gb")
            ds = e.get("vram_delta")
            out.append(f"  {e.get('model')}"
                       f" · 权重 {'?' if mg is None else f'{mg:.1f}GB'}"
                       f" · 装载 {e.get('load_s')}s · 生成 {e.get('gen_s')}s"
                       + ("" if e.get("tok_s") is None else f" · {e['tok_s']} tok/s")
                       + ("" if ds is None else f" · 显存 +{ds:.1f}GB"))
            p = e.get("plan") or {}
            if p:
                out.append(f"      加速项：flash_attn={'√' if p.get('flash_attn') else '×'}"
                           f" · KV={'q8_0' if p.get('kv_quant') else 'fp16'}"
                           f" · n_batch={p.get('n_batch')}"
                           f" · n_ubatch={p.get('n_ubatch') or '默认'}"
                           f" · n_gpu_layers={p.get('n_gpu_layers')}"
                           + ("　⚠ 已降级" if p.get("mode") == "degraded" else ""))
        out.append("")

    for e in events:
        if e.get("kind") == "swap_probe":
            # UNET 回载 ≥2 才算重传证据：总次数里混着 TE/VAE 的既定回载
            # （nodes.py 二采入口探测的口径，2026-09-22 修正）
            _un = e.get("unet_loads")
            _tag = ("（无回载）" if not e.get("loads")
                    else "（UNET≥2 次 → 确实重传，两遍式有价值）" if (_un or 0) >= 2
                    else "（UNET<2 次 → 观测到的都是既定流程，不构成重传证据）")
            out.append(f"二采换页探测：段{e.get('seg')} A≠B={e.get('up_swap')} "
                       f"回载 {e.get('loads')} 次（UNET {_un if _un is not None else '?'} 次）"
                       + _tag)
        elif e.get("kind") == "fail":
            out.append(f"⚠ 段{e.get('seg')} 中断（{e.get('where', '?')}）——"
                       "其后无数据，说明进程在此终止")
    return out


def _pick_vram(e):
    if not e:
        return "n/a"
    # `vram_used` = 设备级占用（2026-09-22 起）。老日志没有该键时才回落历史口径：
    # 那时 vram_peak 装的是 torch max_memory_allocated，在 DynamicVRAM 下是废数
    # （详见 upscale._vram_probe），回落到它只是为了让旧报告还能读出来。
    v = e.get("vram_used")
    if v is None:
        v = (e.get("vram_peak") if e.get("stage") not in ALLOC_STAGES
             else e.get("vram_alloc"))
    return "n/a" if v is None else f"{v:.2f}"


def _pick_rss(e):
    if not e:
        return "n/a"
    v = e.get("rss")
    return "n/a" if v is None else f"{v:.2f}"


def probe_hardware(unet_bytes=None, te_bytes=None, disk_path=None):
    """问一遍环境，返回 hw dict（字段缺则 None，绝不抛）。

    unet_bytes / te_bytes 由调用方从模型参数现算（本模块不 import torch，
    也不该去猜模型结构）；传 None 时该字段为 None，R_v / R_m 随即退化为 None
    —— 报告行会显示 "?"，而不是编一个数字。
    """
    vram_total, vram_free = probe_vram()
    ram, swap = probe_ram_swap()
    return {
        "vram_total_gb": vram_total,
        "vram_free_gb": vram_free,
        "ram_total_gb": (ram or {}).get("total_gb"),
        "ram_avail_gb": (ram or {}).get("avail_gb"),
        "swap_total_gb": (swap or {}).get("total_gb"),
        "swap_free_gb": (swap or {}).get("free_gb"),
        "disk_free_gb": probe_disk_free(disk_path),
        "unet_gb": (float(unet_bytes) / GB) if unet_bytes else None,
        "te_gb": (float(te_bytes) / GB) if te_bytes else None,
        "pcie": probe_pcie(),
        "aimdo": probe_aimdo(),
        "attn_backend": probe_attn_backend(),
        "os": probe_os(),
        "swap_source": _LAST_SWAP_SOURCE,   # 必须在 probe_ram_swap() 之后读
        "vram_policy": probe_vram_policy(),
    }
