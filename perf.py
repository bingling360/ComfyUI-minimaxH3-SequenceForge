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

    swap_txt = "未知" if swap_free is None else f"{swap_free:.0f}GB"
    disk = hw.get("disk_free_gb")
    disk_txt = "未知" if disk is None else f"{disk:.0f}GB"
    ram_txt = "未知" if ram_avail is None else f"{ram_avail:.0f}GB"

    if ok:
        msg = (f"卸载可落内存：可用内存 {ram_txt} + swap {swap_txt} ≥ 需求 {need:.0f}GB")
    else:
        msg = (f"⚠ 本次卸载会把权重挤出到 swap —— 可用内存 {ram_txt} + swap {swap_txt}"
               f" < 需求 {need:.0f}GB（权重 {weights:.1f}GB × {ratio:g}）；"
               f"可用磁盘 {disk_txt}。"
               f"{'本机 swap 不足，Linux 无 swap 时不是变慢而是被 OOM killer 杀掉' if level == 'critical' else '接近临界，建议先释放内存'}"
               "——建议先关掉其它进程，或改用不卸载策略")

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

    `[H3性能] 显存 32.0GB · 内存 80GB · swap 0GB · R_v 0.83 · R_m 0.65 · 档位 云端高显存`

    缺项显示 "?" 而不是编一个数字——诊断行的价值全在可信。
    """
    def _g(key, nd=1):
        v = hw.get(key)
        return "?" if v is None else f"{float(v):.{nd}f}"

    rv = ratio_v(hw)
    rm = ratio_m(hw)
    prof = profile or resolve_profile(hw)
    return (f"[H3性能] 显存 {_g('vram_total_gb')}GB · 内存 {_g('ram_total_gb', 0)}GB"
            f" · swap {_g('swap_total_gb', 0)}GB"
            f" · R_v {'?' if rv is None else f'{rv:.2f}'}"
            f" · R_m {'?' if rm is None else f'{rm:.2f}'}"
            f" · 档位 {PROFILE_LABELS.get(prof, prof)}")


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
        avail_pagefile = max(0.0, (st.ullAvailPageFile - st.ullAvailPhys) / GB)
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


def probe_ram_swap():
    """(ram, swap) 两个 dict（GB）；探不到给 None。Linux 优先 /proc/meminfo。"""
    ram = swap = None
    if os.name == "nt":
        ram, swap = _ram_swap_windows()
    if ram is None:
        ram, swap = _ram_swap_linux()
    if ram is None:
        # 最后兜底：psutil（ComfyUI 环境常见，但不是必须）
        try:
            import psutil  # type: ignore

            vm = psutil.virtual_memory()
            sm = psutil.swap_memory()
            ram = {"total_gb": vm.total / GB, "avail_gb": vm.available / GB}
            swap = {"total_gb": sm.total / GB, "free_gb": sm.free / GB}
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

    ModelPatcher → 取 `.model`；CLIP → 取 `.cond_stage_model`；都不是就用对象本身。
    拿不到返回 None（绝不猜——R_v 算错比不算更糟）。

    ⚠ 口径说明：按张量实际 element_size 累加，int8 打包存储（ComfyUI 常见的
    fp16 权重 + 独立 scale）会**低估**。诊断看的是量级与跨段趋势，不追求字节级精确。
    """
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
# 这两段报「当时占用」，其余报「阶段峰值」
ALLOC_STAGES = ("base", "unload")

# 耗时四阶段（与 upscale.render_segment 的 _timing 键一致）
TIMING_KEYS = ("up", "cond", "refine", "decode")
TIMING_LABELS = ("放大", "条件", "精化", "解码")


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
        out.append("")

    segs = {}
    for e in events:
        if e.get("kind") != "mark":
            continue
        segs.setdefault(e.get("seg"), {})[e.get("stage")] = e

    for title, key, pick in (("显存峰值（GB）", "vram", _pick_vram),
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

    for e in events:
        if e.get("kind") == "swap_probe":
            out.append(f"二采换页探测：段{e.get('seg')} A≠B={e.get('up_swap')} "
                       f"回载 {e.get('loads')} 次"
                       + ("（零换页 → 两遍式无省头）" if not e.get("loads")
                          else "（确实重传 → 两遍式有价值）"))
        elif e.get("kind") == "fail":
            out.append(f"⚠ 段{e.get('seg')} 中断（{e.get('where', '?')}）——"
                       "其后无数据，说明进程在此终止")
    return out


def _pick_vram(e):
    if not e:
        return "n/a"
    v = e.get("vram_peak") if e.get("stage") not in ALLOC_STAGES else e.get("vram_alloc")
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
    }
