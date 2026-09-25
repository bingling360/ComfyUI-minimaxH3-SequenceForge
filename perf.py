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
    # 场景（档位名由 resolve_perf 解析，本键是「用户选的那一档」）
    "profile": "auto",

    # 显存：权重流动（块交换）
    #
    # ★ 机制归属（2026-09-23 重开，查证后定的口径）：**块级权重流动是 ComfyUI 官方
    # 做的**，本插件不自己搬权重。H3 的 block 循环里官方已有预取/换入队列
    # （`comfy/ldm/minimax/model.py:751-765` 的 make_prefetch_queue / prefetch_queue_pop
    # + `comfy/model_prefetch.py` + aimdo 的 vbar 故障页），总闸是
    # `comfy/model_base.py:250` 的 `transformer_options["prefetch_dynamic_vbars"]
    # = patcher.is_dynamic()`。
    # 手工搬权重那条路的结局是：与官方 partial load / vbar **抢同一批参数**、打坏
    # LoRA 的 patch 账（`model_loaded_weight_memory`），而且在 vbar 上根本搬不动
    # （官方明确禁止外部 pin：ModelPatcherDynamic.pin_weight_to_device → RuntimeError）。
    # 所以我们只驱动两个**真实存在**的旋钮，见 apply_blockswap：
    #   · blocks_to_swap → aimdo 的「显存预留」(VRAM headroom)：抬高 = 逼 vbar 驱逐
    #     页面 = 更多块必须从主机内存换入 → **换时间换显存**（与面板语义 1:1）
    #   · blocks_prefetch → 官方预取队列的开关（深度由 prefetch_queue_pop 的队列
    #     结构写死为「提前 1 块」→ 只能是 bool，不能是「预取 N 块」）
    "blocks_swap_on": False,       # 块交换总开关（关 = 恢复 baseline，不干预官方）
    "blocks_to_swap": -1,          # -1 = 跟随 profile（suggest_blocks 反解）；0..50
    "blocks_prefetch": True,       # 官方块级预取（官方深度固定 1 块，只能开关）

    # 显存：常驻
    #
    # ⛔ 2026-09-23 本轮**连根删除** 4 个从未实现的显存策略键（`unload_unet_seg` /
    # `unload_before_decode` / `frames_to_cpu` / `max_upscale_scale`）：它们只出现在
    # DEFAULT_PERF / PROFILE_TABLE / PERF_TYPES 三张表里，全仓零消费端 —— 属于
    # 「声明了没实现」的假能力。同类诉求现在都有真实现：段间腾挪 `vram_shuffle`、
    # 成片内存 `final_mode` / `frames_dtype`、卸载时机由 ComfyUI 按需换入决定。
    # **别再往这里加回来**：要在某个阶段省显存，走真实的消费端（有 `*_on` 开关的
    # 分块 / 块交换），不要新造一个只有名字的键。

    # 显存：分块（按画质风险递增排列：FFN=无 → 头分块=无 → 时序=低 → 空间 tile=中高）
    #
    # 每类分块都是「启用开关 + 参数」两件套：开关是 `*_on`（或语义化的 bool），
    # 参数在开关关掉时仍保留数值、只是不生效 —— 免得用户来回开关时得重填。
    # 开关本身不进指纹（它们是显存手段，不是画质手段，见 upscale._hash_params）。
    "ff_chunk_on": False,             # FFN token 分块总开关
    # 每块 token 数（数学等价，零画质损失）。⚠ **宁大勿小**：块数 = 序列 token ÷ 此值，
    # 而开销 ∝ 块数 —— 每个 Linear 每块都要取一次权重、同步一次流，块数一多就线性
    # 拖慢采样。H3 常见序列 5–10 万 token，填 4096 会切出 20+ 块；16384 只需约 7 块，
    # 单块激活代价 ≈ 16384×28672×2B ≈ 0.9GB，压峰效果依然充分。
    # 2026-09-25 由 4096 上调：旧值是按 S=8192 设计的，与 H3 真实序列长度差一个量级。
    "ff_chunk_tokens": 16384,
    "ff_chunk_min_tokens": 8192,      # 序列 token 低于此值不切（对齐 KJ seq_threshold 语义）
    "attn_head_on": False,            # 注意力头分块总开关
    "attn_head_chunks": 8,            # 注意力头分几组（1=关）；精确无损（2026-09-24 实测平台期起点）
    "attn_backend": "auto",           # auto / sdpa / sage / flash（auto = 沿用 ComfyUI 选定）
    "upscale_temporal_chunk": True,   # 放大网络 3D 时序分块（内部已实现，此前硬编码为开）
    "upscale_chunk_frames": 32,       # 放大网络每块帧数
    "upscale_overlap": 0,             # 块间重叠帧；0 = 自动取时序卷积核宽（只增不减）
    # ⛔ VAE 解码分块（decode_chunk_on / decode_chunk_frames）**已整体移除**（2026-09-23）：
    # 不是「暂不接线」，是**本就不该存在** —— H3 视频 VAE 在官方层就已 tile 级分块解码
    # （`comfy/sd.py:1038` handles_tiling=True：256px 空间 × 17 帧时序块，且显存估算
    # `frames = min(frames, chunk_frames + 2)` 只按单块算）。外面再套一层是与官方 tile
    # 叠加 → 更慢更糊、显存一点不多省。留着只会让人以为勾了有用。
    "refine_temporal_on": False,      # 精化时序分块总开关
    "refine_temporal_chunk": 0,       # 精化每段帧数（0 = 不分块）
    "refine_temporal_overlap": 8,     # 段间重叠 latent token（单位：latent token，非像素）
    "refine_tile_on": False,          # 精化空间分块总开关
    "refine_tile": "off",             # off / 2x2 / 3x3 / 4x4 / 2x1 / 1x2
    "refine_tile_overlap": 32,        # 块间重叠像素
    "refine_tile_feather": 16,        # 接缝羽化宽度（像素）

    # 显存：峰值与自救
    "oom_autoretry": True,         # 主干采样 OOM 自救（卸载 + 回收后原参重试一次）
    "act_peak_probe": True,        # 量 LoRA / bypass 前向的**激活**峰值（权重 ComfyUI 已算到）
    # 放大网络常驻（**不**强制卸载）：True = 段间保留缓存、零加载 —— 这是**现状口径**
    # （与旧项目存档里的 `force_unload: false` 同义，迁移不得改默认行为）；
    # False = 每段二采收尾把网络从 MODEL_CACHE 删掉 + soft_empty_cache，下段重新从
    # 磁盘加载（换 CPU 侧那 659MB 权重副本，代价是每段 ~1s 重载）。
    "keep_upscaler_resident": True,

    # 内存：成片合成
    "final_mode": "auto",          # auto=能拼就拼 / stream=强制流式 / memory=强制内存帧
    "frames_dtype": "float32",     # float32 / uint8（帧存内存 ×¼）

    # 内存
    #
    # ⛔ `unload_upscaler_cache` 已**连根删除**（2026-09-23 本轮）：它与
    # `keep_upscaler_resident` 是同一件事的两面（取反），而**只有后者有消费端**
    # （upscale.render_segment 收尾）。留着就是面板上一个勾了没用的三方开关
    # （tri：auto/on/off），比不给更糟。**别再往这里加回来**。
    #
    # ⛔ `cond_cache_size`（cond 文本编码缓存的 LRU 条数）**删除了**。
    #   注意它跟上面那批「没人读的键」不是同一种问题 —— nodes 确实读它、值确实传到了
    #   `CachedClipProxy(capacity=…)`，但**那个旋钮对真实调用路径毫无作用**：
    #   · cond 缓存的键要求「tokenize 的额外参数可哈希」，而官方 MiniMax H3 节点
    #     固定传一个 list（i2v 传 `images=`、ref2va 传 `minimax_ref_items=`，**空表也算**）
    #     → `_misc_key` 返回 None → 全部旁路，命中恒为 0；
    #   · 实测（3 段 × 一采+二采，2026-09-23）：带任何 list 参数时 cap=1/8/32 的
    #     TE 真前向次数**逐字相同**（6 / 6 / 6），只有无参调用才进缓存。
    #   · 且带图/带参考时，一采与二采的视觉输入本就不同（画幅不同）→ 本就不该复用。
    #   **别再把它当「内存旋钮」加回来**。
    #   ⚠ 补充（2026-09-24）：cond_cache 的键已改成**类型白名单归一化**，空表参数
    #   现在能命中（无首帧图、无参考素材的段每段省 1 次文本编码器前向），见
    #   cond_cache.py 顶部实测表。但**容量仍然不需要做成旋钮**：同一段的一采/二采
    #   在段循环里紧邻，容量 ≥2 就够；跨段复用要的也只是几十条，构造默认 32 覆盖得住。
    #   等真出现「提示词多到互相挤掉」的实测再说。

    # 磁盘 / swap 守卫（场景 A 必备）
    #
    # ⛔ `guard_offload_target` 已连根删除（2026-09-23 本轮）：语义与 `guard_action`
    # 重叠（都在说「守卫判不过时要不要真拦」）且从未有消费端，属假开关。
    # `offload_guard_ratio` 则**接线了**：offload_guard / guard_allows_unload 本来就
    # 收 ratio 入参，以前 nodes 没传 → 键改了不生效；现在按它传。
    "offload_guard_ratio": DEFAULT_GUARD_RATIO,
    "guard_action": "warn",        # warn=只报 / block=critical 时禁止全卸

    # 编码。**一个「质量数值」跟着编码器换含义**（面板上按 encoder 值显隐）：
    #   encoder=libx264     -> 读 `x264_crf`（恒定质量，越小越清晰）
    #   encoder=*_nvenc     -> 读 `nvenc_cq`（NVENC 的恒定质量，语义对应 crf）
    # 两者**不是同一个旋钮**：NVENC 不认 crf、x264 不认 cq，传错会被静默忽略
    # （质量档整个失效），所以后端按 encoder 显式分流（见 media._video_stream_options）。
    #
    # ★ `encode_hq`（2026-09-23 本轮，用户拍板）把原三档「标准 / 高清 / 极致」压成
    #   **一个开关**：
    #     关（默认）= 旧「标准」：veryfast · 无 aq · 无抖动
    #     开         = 旧「高清」：medium · aq-mode 3（暗部自适应）· Bayer 抖动
    #   「极致」（slow）删除 —— crf 被 `x264_crf` 独立接管后，它与「高清」**只差
    #   preset 速度档**，不值得单列。crf / cq 与 preset / 抖动是**两个正交旋钮**：
    #   档位只定后者，前者在上面的质量数值里单独调（面板也不再声称「切档位会带着
    #   改 crf」—— 它本来就不该带着改）。
    #   真源：`upscale._ENCODE_SETTINGS` / `resolve_encode_quad`。
    "encoder": "libx264",          # libx264 / h264_nvenc / hevc_nvenc（不再有假 auto）
    "x264_crf": 20,                # x264 恒定质量（越小越清晰，常用 13–23）
    "nvenc_cq": 20,                # NVENC 恒定质量（对应 x264 的 crf）
    "encode_hq": False,            # 高清编码档：medium preset + 暗部 aq + 抖动

    # ComfyUI 运行时开关（**节点端适配，不动启动参数**）
    "upcast_attention": "auto",   # auto=跟随启动参数；True/False=强制开/关
    "vram_shuffle": "auto",       # 精化前腾挪强度：auto / off / soft / full

    # 素材库
    "index_mode": "fingerprint",  # fingerprint（目录指纹）/ ttl（3 秒硬过期）
    "thumb_on_import": True,      # 入库即生成缩略图
    "thumb_max_mp": 40,           # 缩略图源图解码上限（百万像素）

    # ⛔ `probe` 已连根删除（2026-09-23 本轮）：全仓零读取方，前端也无字段，
    # 属「名字在表里、功能不存在」。诊断走专门的工具（tools/ 下独立 runner）
    # 与 verbose 环境变量（如 H3_LLM_VERBOSE），不要塞回 perf 表。
}

# ⛔ **三态名单连根删除**（2026-09-23，用户拍板）：
#   `WIRED_KEYS`（已接线）/ `UNWIRED_KEYS`（未接线，面板标「接线中」+ 灰）/
#   `PROFILE_DRIVEN_KEYS`（由档位表填值，面板标「跟随档位」）三个名单全部删掉，
#   连带 `perf_get` / `perf_set` 的返回字段与前端那套判态、徽章、灰化分支。
#
# 为什么删：名单是**人工维护的**，因此必然说谎 ——
#   · 第七批接线完成后 `UNWIRED_KEYS` 恒为空，「接线中」那一态再没有对象，
#     却留下整套判态机制与接口字段（用户原话：「这三态拿来干嘛的，没有给我删掉」）；
#   · 更糟的是同一套名单曾把 10 个「表里有值、全仓没人读」的键当成「纯内部键」
#     放行，让「没有未接线字段」这句话变成假的。
#
# 判据从此**只剩代码事实**：一个键有没有用，看有没有人读它 ——
# `tests/test_perf_settings.py::test_no_key_in_default_perf_is_unread` 用 AST
# 扫全仓来钉这件事（比人工名单强，且不会过期）。
# 面板侧：字段表里每一项都直接渲染成可点，没有灰化/徽章分支；「跟随档位」这类
# 提示改由**字段自己的属性 + 当前值**表达（前端字段表的 `autoWhen`）。
# **别再引入任何「接线名单」**：要么有人读这个键，要么别加它。

# 自动策略表（§7）—— 只列两场景有差异的项，其余沿用 DEFAULT_PERF。
# ⚠ 这是**建议值不是强制值**：面板必须允许逐项覆盖（resolve_perf 的 overrides）。
#
# ★ 2026-09-23 本轮**瘦身到只剩一项**：原先 8 项里的 6 项（`unload_unet_seg` /
#   `unload_before_decode` / `frames_to_cpu` / `max_upscale_scale` /
#   `unload_upscaler_cache` / `guard_offload_target`）**从没有消费端**；
#   第 7 项 `cond_cache_size` 有消费端但**旋钮空转**（见 DEFAULT_PERF 里的实测说明）。
#   档位表里写着值、跑起来什么都不改，是「这条策略已生效」的假承诺 —— 已全部删除。
#   现在只剩 `blocks_to_swap` 一项，它**按真实 UNET 体量反解**、由 apply_blockswap 消费。
PROFILE_TABLE = {
    PROFILE_CLOUD: {
        # 云端显存给得足：不逼官方换块（0 = 不干预）
        "blocks_to_swap": 0,
    },
    PROFILE_LOCAL: {
        # 本机小显存：按真实 UNET 体量反解块数（-1 语义由 apply_blockswap 消费）
        "blocks_to_swap": 25,
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
    # 显存：权重流动（块交换）
    "blocks_swap_on": (bool,),
    "blocks_to_swap": (int,),
    "blocks_prefetch": (bool,),
    # 显存：分块（每类 = 开关 + 参数两件套）
    "ff_chunk_on": (bool,),
    "ff_chunk_tokens": (int,),
    "ff_chunk_min_tokens": (int,),
    "attn_head_on": (bool,),
    "attn_head_chunks": (int,),
    "attn_backend": (str,),
    "upscale_temporal_chunk": (bool,),
    "upscale_chunk_frames": (int,),
    "upscale_overlap": (int,),
    "refine_temporal_on": (bool,),
    "refine_temporal_chunk": (int,),
    "refine_temporal_overlap": (int,),
    "refine_tile_on": (bool,),
    "refine_tile": (str,),
    "refine_tile_overlap": (int,),
    "refine_tile_feather": (int,),
    # 显存：峰值与自救
    "oom_autoretry": (bool,),
    "act_peak_probe": (bool,),
    "keep_upscaler_resident": (bool,),
    # 内存：成片合成
    "final_mode": (str,),
    "frames_dtype": (str,),
    # 磁盘 / swap 守卫
    "offload_guard_ratio": (int, float),
    "guard_action": (str,),
    # 编码（两个维度：实现 / 质量档；质量档随 encoder 换含义，crf 与 cq 分开存）
    "encoder": (str,),
    "x264_crf": (int,),
    "nvenc_cq": (int,),
    "encode_hq": (bool,),
    # ComfyUI 运行时开关
    "upcast_attention": (bool, "auto"),
    "vram_shuffle": (str,),
    # 素材库
    "index_mode": (str,),
    "thumb_on_import": (bool,),
    "thumb_max_mp": (int, float),
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



# ---- 块交换：接线到**官方**的块级权重流动（2026-09-23，批次 5 重开）----
#
# 为什么这里没有「搬权重的代码」：见 DEFAULT_PERF 那段注释。一句话 —— H3 的 block
# 循环里官方已经有预取/换入队列（`comfy/ldm/minimax/model.py:751-765`），权重住在
# aimdo 的 vbar 里（`m._v`），官方明确禁止外部 pin，手工搬只会抢同一批参数。
#
# 所以我们只驱动两个**真实存在**的旋钮：
#   1) `blocks_to_swap` → aimdo 的「显存预留」(headroom)。抬高它 = 逼 vbar 驱逐页面 =
#      更多块只能从主机内存换入 = **换时间换显存**。与面板语义 1:1：放出 N 块 =
#      多预留 N × (UNET/50) 字节。
#      官方 `comfy_aimdo.control.set_simple_vram_headroom` 文档原文：「Raising it takes
#      effect at the next VBAR fault or hooked device allocation and is honoured by
#      evicting VBAR pages only」→ 运行期真实生效（本机 aimdo 0.5.5 实测：设完读回一致，
#      见 tools/check_blockswap.py）。
#   2) `blocks_prefetch` → 官方预取队列的开关。深度由 `prefetch_queue_pop` 的队列结构
#      写死（`[None] + blocks + [None]`，每轮只 pin `queue[0]` = 下一块）→ 只能是 bool。
#
# 无 aimdo 的环境（老 ComfyUI / `--disable-dynamic-vram` / `--highvram`）不是没救：那时走
# legacy ModelPatcher，模型装不下时由它自己的 `partially_load()` / `_load_list()` 按
# **模块**流（粒度 ≈ block，每 double block ≈ 0.4GB）；我们把 `blocks_to_swap` 折算成
# Python 侧的 `EXTRA_RESERVED_VRAM`（`load_models_gpu` 的「最低必须留空」地板）同样能
# 改变「留多少在卡上」。
BLOCKSWAP_HEADROOM_CAP = 0.5   # 预留最多占到显存总量的这一比例（再高没有意义）

# 进程启动时的预留基线：**只读一次并缓存**。第二次读到的可能已被我们自己改过，用它
# 当基线会让「关掉开关 = 恢复原样」变成「关掉开关 = 保持我上次设的值」。
_BS_BASELINE = {}


def probe_blockswap():
    """块级权重流动的**在线状态**（best-effort，量不到给 None，绝不抛）。

    `path`：
      · ``"aimdo"``  —— DynamicVRAM 在线 → 官方的 vbar 换入 + 块级预取都在跑
      · ``"legacy"`` —— 没有 aimdo（老版本 / 关了动态显存）→ 靠 ComfyUI 自己的按模块流
    """
    out = {"aimdo": None, "dynamic": None, "streams": None, "non_blocking": None,
           "pinned_gb": None, "headroom_gb": None, "path": None}
    try:
        from comfy import memory_management as mem  # type: ignore
        out["aimdo"] = bool(getattr(mem, "aimdo_enabled", False))
    except Exception:
        return out
    try:
        from comfy import model_patcher as mp  # type: ignore
        out["dynamic"] = mp.CoreModelPatcher is mp.ModelPatcherDynamic
    except Exception:
        pass
    try:
        from comfy import model_management as mm  # type: ignore
        out["streams"] = int(getattr(mm, "NUM_STREAMS", 0) or 0)
        out["non_blocking"] = bool(mm.device_supports_non_blocking(mm.get_torch_device()))
        pinned = float(getattr(mm, "MAX_PINNED_MEMORY", 0) or 0)
        out["pinned_gb"] = round(pinned / GB, 2) if pinned > 0 else None
    except Exception:
        pass
    out["headroom_gb"] = _aimdo_headroom_gb()
    out["path"] = "aimdo" if (out["aimdo"] and out["dynamic"]) else "legacy"
    return out


def _aimdo_headroom_gb():
    """当前 aimdo 显存预留（GB）；拿不到 → None（**不是 0** —— 0 是「不留」这个合法值）。"""
    try:
        import comfy_aimdo.control as ctl  # type: ignore
        return round(float(ctl.get_simple_vram_headroom()) / GB, 3)
    except Exception:
        return None


def blockswap_headroom(n_blocks, unet_gb, vram_gb, baseline_gb=0.0,
                       cap=BLOCKSWAP_HEADROOM_CAP):
    """「放出 N 块」→ 需要额外预留多少显存（**纯函数**）。

    extra = N × (UNET / 50)，块大小取 H3_DOUBLE_BLOCK_COUNT（H3 = 50 个 double block）。

    ⚠ **必须夹取**：6GB 卡上 `suggest_blocks` 会算出 41 块 = 16.4GB > 显存总量，真去设
    这么大的预留是无意义的（预留不可能超过卡本身）。夹到 ``vram × cap``（默认一半，
    另一半留给激活 / VAE / 碎片 —— 与 RESERVE_GB 同一口径），并把「被夹了」如实报出去，
    别让用户以为自己填的 41 生效了。

    返回 ``{"extra_gb", "blocks_eff", "clamped", "note"}``。
    """
    try:
        n = int(n_blocks)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return {"extra_gb": 0.0, "blocks_eff": 0, "clamped": False,
                "note": "交换块数 0 —— 不额外预留（权重全部允许常驻）"}
    if not unet_gb or not vram_gb:
        return {"extra_gb": 0.0, "blocks_eff": 0, "clamped": False,
                "note": "UNET / 显存体量未探明 → 本次不干预"}
    block_gb = float(unet_gb) / float(H3_DOUBLE_BLOCK_COUNT)
    want = n * block_gb
    room = max(0.0, float(vram_gb) * float(cap) - float(baseline_gb or 0.0))
    extra = min(want, room)
    eff = int(extra / block_gb) if block_gb > 0 else 0
    clamped = eff < n
    note = f"放出 {eff} 块 ≈ 多预留 {extra:.2f}GB（块大小 {block_gb:.2f}GB）"
    if clamped:
        note += (f"；⚠ 你要的 {n} 块 ≈ {want:.1f}GB 超过显存可让出的一半"
                 f"（{room:.1f}GB），已夹到 {eff} 块")
    return {"extra_gb": float(extra), "blocks_eff": eff, "clamped": clamped, "note": note}


def _blockswap_baseline():
    """进程启动时的两份预留基线（**只读一次**，见 `_BS_BASELINE` 注释）。"""
    if _BS_BASELINE:
        return _BS_BASELINE
    _BS_BASELINE["aimdo_gb"] = _aimdo_headroom_gb()
    extra = None
    try:
        from comfy import model_management as mm  # type: ignore
        extra = round(float(mm.extra_reserved_memory()) / GB, 3)
    except Exception:
        pass
    _BS_BASELINE["extra_gb"] = extra
    return _BS_BASELINE


def _set_aimdo_headroom(headroom_gb):
    """真设 aimdo 显存预留并**读回核验**；返回读回值（GB）；不支持 → None。"""
    try:
        import comfy_aimdo.control as ctl  # type: ignore
    except Exception:
        return None
    try:
        ctl.set_simple_vram_headroom(max(0, int(float(headroom_gb) * GB)))
        return round(float(ctl.get_simple_vram_headroom()) / GB, 3)
    except Exception:
        return None


def _set_extra_reserved(headroom_gb):
    """legacy 路径：Python 侧的「最低必须留空」地板（`load_models_gpu` 读它）。"""
    try:
        from comfy import model_management as mm  # type: ignore
        mm.EXTRA_RESERVED_VRAM = max(0, int(float(headroom_gb) * GB))
        return round(float(mm.extra_reserved_memory()) / GB, 3)
    except Exception:
        return None


def apply_blockswap(table, hw=None):
    """把块交换应用到当前进程 → 状态 dict（note 供报告 / 面板显示）。

    - 总开关关 → **恢复 baseline**（别把上次设的值留在进程里）
    - ``blocks_to_swap = -1`` → 用 ``suggest_blocks(unet_gb, vram_gb)`` 反解真实块数
    - ``blocks_prefetch`` 是**模型侧**的（要给具体模型打包装），由
      ``upscale.install_block_prefetch`` 在拿到模型时装 —— 这里只回声，不假装做了。

    返回 ``{"on","path","blocks","extra_gb","clamped","headroom_gb","applied","note"}``。
    """
    t = dict(table or {})
    hw = hw or {}
    st = probe_blockswap()
    base = _blockswap_baseline()
    out = {"on": bool(t.get("blocks_swap_on", False)), "path": st["path"],
           "blocks": 0, "extra_gb": 0.0, "clamped": False,
           "headroom_gb": st.get("headroom_gb"), "applied": False, "note": ""}

    n = t.get("blocks_to_swap", -1)
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = -1
    if n < 0:
        n = suggest_blocks(hw.get("unet_gb"), hw.get("vram_total_gb"))
    out["blocks"] = n

    if st["path"] == "aimdo":
        base_gb = base.get("aimdo_gb")
        if base_gb is None:
            out["note"] = "读不到 aimdo 当前显存预留（版本不支持读回）→ 未干预"
            return out
        target = float(base_gb)
        if out["on"] and n > 0:
            pl = blockswap_headroom(n, hw.get("unet_gb"), hw.get("vram_total_gb"), base_gb)
            # 算不出目标就别写：面板打开时拿不到 UNET 体量（没加载模型），此时若照
            # 「extra=0」去写，会把上一次渲染设好的预留**悄悄抹掉** —— 读接口不该
            # 改进程状态。
            if pl["blocks_eff"] <= 0:
                out["note"] = pl["note"]
                return out
            target = base_gb + pl["extra_gb"]
            out.update(extra_gb=pl["extra_gb"], clamped=pl["clamped"],
                       blocks=pl["blocks_eff"], note=pl["note"])
        got = _set_aimdo_headroom(target)
        if got is None:
            out["note"] = (out["note"] + "；" if out["note"] else "") + \
                "aimdo 拒绝设置显存预留 → 未生效"
            return out
        out["applied"] = True
        out["headroom_gb"] = got
        if not out["on"]:
            out["note"] = f"关闭 → 已恢复进程启动时的预留（{got:.2f}GB）"
        elif n <= 0:
            out["note"] = f"交换块数 0 → 预留保持基线 {got:.2f}GB，权重全允许常驻"
        else:
            out["note"] += f"；aimdo 预留现为 {got:.2f}GB（读回核验）"
        return out

    # ---- legacy：没有 DynamicVRAM，走 ComfyUI 自己的 EXTRA_RESERVED_VRAM ----
    base_gb = base.get("extra_gb")
    if base_gb is None:
        out["note"] = "拿不到 ComfyUI 的预留基线 → 未干预"
        return out
    target = float(base_gb)
    if out["on"] and n > 0:
        pl = blockswap_headroom(n, hw.get("unet_gb"), hw.get("vram_total_gb"), base_gb)
        if pl["blocks_eff"] <= 0:       # 同上：算不出目标 → 不写（读接口不许改状态）
            out["note"] = pl["note"]
            return out
        target = base_gb + pl["extra_gb"]
        out.update(extra_gb=pl["extra_gb"], clamped=pl["clamped"],
                   blocks=pl["blocks_eff"], note=pl["note"])
    got = _set_extra_reserved(target)
    if got is None:
        out["note"] = "无法写 ComfyUI 预留 → 未生效"
        return out
    out["applied"] = True
    out["headroom_gb"] = got
    tail = (f"本机无 DynamicVRAM（legacy ModelPatcher）：已把 ComfyUI 的「最低留空」"
            f"设成 {got:.2f}GB —— 装不下时由它按模块流动" if (out["on"] and n > 0)
            else f"本机无 DynamicVRAM：已恢复 ComfyUI 预留基线（{got:.2f}GB）")
    out["note"] = (out["note"] + "；" if out["note"] else "") + tail
    return out


def blockswap_line(state, headroom_gb=None):
    """块级权重流动的现状一行（报告 / 面板诊断共用）。

    ⚠ 只说**环境与机制归属**，不说「省了多少」—— 那要实测才有数（本机 6GB 卡跑不动
    20GB UNET，量不到）。
    """
    path = (state or {}).get("path")
    if path == "aimdo":
        h = "?" if headroom_gb is None else f"{headroom_gb:.2f}GB"
        return (f"块级流动：DynamicVRAM 在线（官方块级预取 + vbar 换入），"
                f"当前显存预留 {h}")
    return ("块级流动：本机无 DynamicVRAM（legacy ModelPatcher）→ 装不下时由 ComfyUI "
            "自己按**模块**流动（粒度 ≈ block，每 double block ≈ 0.4GB）")


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


def guard_allows_unload(hw, action="warn", ratio=DEFAULT_GUARD_RATIO):
    """落盘守卫**接行为**：critical 且 action="block" 时禁止全卸。

    `offload_guard()` 只报数不改行为，这里才是「守卫说不能卸 → 调用方真的别卸」
    的那一环。为什么需要它：D4 里 Linux 无 swap 的机器上，一次
    `unload_all_models()` 会把几十 GB 权重瞬间推给内存，OS 收不下就是
    **OOM killer SIGKILL**——连 except 都跑不到。与其崩，不如不卸。

    返回 (allowed, guard_dict)；guard_dict 同 `offload_guard()`。
    """
    g = offload_guard(hw, ratio)
    if str(action or "warn").lower() == "block" and g.get("level") == "critical":
        return False, g
    return True, g


# ---- 编码档位 ----
#
# 诚实结论（见 docs/性能优化设置_规划_2026-09-22.md §C 组）：**编码不是瓶颈**
# ——557 帧实测只要 13 秒。NVENC 的真实收益是 CPU 占用（x264 会打满核数×1.5
# 个线程），不是耗时、更不是内存（帧最终仍要落 CPU 进编码器）。故默认 auto
# = 沿用 libx264，NVENC 交给用户按自己的机器选。
ENCODERS = ("libx264", "h264_nvenc", "hevc_nvenc")
_NVENC = ("h264_nvenc", "hevc_nvenc")


def resolve_encoder(name, cq=20, crf=20):
    """编码器名 + 质量档 -> (codec, options_片段)；不认识的都回落 libx264。

    纯函数：NVENC 用 `cq`（恒定质量，语义对应 x264 的 crf），x264 用 `crf`。
    两者**不是**同一个旋钮（NVENC 不认 crf、x264 不认 cq），传错就是静默忽略
    质量档——所以这里显式分流，而不是把 crf 原样塞给 NVENC。

    ⚠ 历史上还有过一个 `auto` 值，但它的分支和 `libx264` 一模一样 —— 是个
      「想做自动判断但没做」的假预留位，同一件事在面板上有两个入口
      （`libx264（推荐）` / `libx264（CPU）`），用户会问「既然一样干嘛弄两个」。
      2026-09-23 已删除；老存档里的 "auto" 仍由下面的 in 判断兜住。
    """
    n = str(name or "libx264").strip().lower()
    if n in _NVENC:
        return n, {"cq": str(int(cq))}
    return "libx264", {"crf": str(int(crf))}


# ---- FFN 分块 ----

def plan_ff_chunks(tokens, chunk_tokens):
    """把 `tokens` 个 token 按 `chunk_tokens` 切块 -> 边界列表 [[s,e), ...]。

    chunk_tokens<=0 / 非法 -> **单块**（等价于不分块）；tokens<=0 -> []。
    纯函数，零 torch 依赖：接线端（upscale/nodes）只拿边界，自己切片。

    为什么分块能救 OOM：主干 OOM 实测崩在 FFN 的 `F.linear(x, down)`（rank256
    中间张量）+ `F.linear(…, up)`（满 hidden 输出），**单次请求 6.13GB**。
    这两步沿 token 维完全可分离 —— 切块后每块的中间张量降到 1/n，
    数学结果与不分块逐位相同（**唯一**的有代价项只是 GPU 利用率略降）。
    """
    try:
        n = int(tokens)
        c = int(chunk_tokens)
    except (TypeError, ValueError):
        return [(0, 0)]
    if n <= 0:
        return []
    if c <= 0:
        return [(0, n)]
    out = []
    for s in range(0, n, c):
        out.append((s, min(n, s + c)))
    return out


# ---- 精化时序分块（扩散采样循环的切段）----

def plan_refine_chunks(latent_t, chunk_tokens, overlap_tokens):
    """把 `latent_t` 个视频 latent token 沿时间切成若干段 -> [(s, e, core_s, core_e), ...]。

    - `s` / `e`：本段喂给采样器的区间 **[s, e)**
    - `core_s` / `core_e`：本段**写回**的区间 —— 语义上等于 `[s, e)`：段只能写
      自己采样过的东西。相邻段的喂入区间本身就重叠 `ov`（净推进 = c - ov），
      那段共享区就是**咬合带**，两块在其中各自羽化、权重互补成 1。

    ⚠ **core 是「咬合」不是「划分」**（2026-09-23 修正）：
    早先 core 是「去掉右侧 overlap 的硬划分」（`core_e = e - ov`），于是相邻段的
    写回区**完全不相交**、「overlap + 羽化」根本没发生 —— 接缝**硬切**、羽化权重
    形同摆设。现在 core = span，相邻段的写回区在咬合带上真实重叠、按互补权重混合。

    ⚠ **单位是 latent token，不是像素帧**（与 `refine_tile_overlap` 的像素不同量纲）。
      视频模型 latent 的时间压缩不是 1:1（官方 `grid.video_latent_t`：首 token 1 帧、
      其后 4 帧、每 5 token 循环），所以「帧」与「token」必须分清。

    ⚠ 与 `plan_ff_chunks` 的**本质区别**：FFN 是纯前馈、可分离，切块结果逐位相同；
      这里是**扩散采样循环** —— 每段独立跑 N 步去噪、段间没有注意力交互，
      两侧各自收敛到不同的局部解，接缝会有轻微差异。所以必须**给 overlap 且做
      加权融合**，且 overlap 太小会明显闪烁（方案建议 ≥8 token）。

    单段（chunk<=0 / latent_t<=chunk / 非法输入）-> [(0, latent_t, 0, latent_t)]，
    即「等效不分块」，调用方据此走原路径（零行为变化）。
    """
    # 逐个转换：**只有 latent_t 非法才算「没有 latent」**（返回 []）；
    # chunk/overlap 非法 = 「没要求分块」→ 落回单段（等价不分块）。
    # 两者语义不同，不能一起往 except 里塞 —— 否则一个打错的 chunk 值
    # 会让整段精化被静默跳过（[] 在调用方眼里就是「没东西可做」）。
    try:
        n = int(latent_t)
    except (TypeError, ValueError):
        return []
    if n <= 0:
        return []
    try:
        c = int(chunk_tokens)
    except (TypeError, ValueError):
        c = 0
    try:
        ov = int(overlap_tokens)
    except (TypeError, ValueError):
        ov = 0
    if c <= 0 or n <= c:
        return [(0, n, 0, n)]
    # overlap 上限取块长的**一半**：overlap 越接近块长，「净推进」越小 —— 到
    # c-1 时每段只前进 1 个 token，段数爆炸（27 token 会切出 20 段），
    # 每段还都要跑完整采样循环，纯属灾难。工程师想要的「多留上下文」到这个
    # 程度早已饱和，所以这里硬性砍到半块。
    ov = max(0, min(ov, c // 2))
    # 净推进 = c - ov -> 相邻段的**喂入区间**本身就重叠 `ov`：
    #   段i = [s, s+c)，段i+1 = [s+c-ov, s+2c-ov)，交集 = [s+c-ov, s+c) 宽 ov。
    # **写回区 = 喂入区**（core == span）：段只能写自己采样过的东西，而它的
    # 尾/首 ov 个 token 恰好就是与邻居共享的咬合带 —— 两块各自在其上羽化、
    # 权重互补成 1。这样既满足「core ⊆ span」（不越界读），又真正发生重叠融合。
    step = c - ov
    out = []
    s = 0
    while s < n:
        e = min(n, s + c)
        out.append((s, e, s, e))
        if e >= n:
            break
        s += step
    return out


def feather_weights(length, ramp):
    """长度 `length` 的 1-D 权重：**首尾各 ramp 个元素对称升/降**，中间恒 1。

    用于重叠区加权融合：相邻两段在重叠区各自的权重曲线**互补成 1**
    （`a[i]*w[i] + b[i]*(1-w[i])`），过渡才平滑无硬边。

    ⚠ 两端用的是**同一条曲线** `(i+1)/(r+1)` —— 首端从 `1/(r+1)` 升到 1，
    尾端从 `1/(r+1)` 升到 1（**以自身坐标为参照**；对全局而言就是向尾部降）。
    这样两块在咬合带对齐时（A 的尾端 ↔ B 的首端）恰好互补：
    `A_tail[k] + B_head[k] = (r-k)/(r+1) + (k+1)/(r+1) = 1`。
    ⚠ 别把尾端「改成」显式的降序 — 那会变成 A、B 同向，和恒为 2 倍（踩过）。

    两端都取不到 0（避免「权重 0 = 丢信息」）。

    `ramp<=0` 或 `ramp*2 >= length` -> 全 1（无羽化；该情形下重叠区取平均由
    调用方自行归一 —— 见 `_refine_chunked` / `_refine_tiled` 的 `wsum` 除法）。
    纯函数，零 torch 依赖，返回 list[float]。
    """
    try:
        n = int(length)
        r = int(ramp)
    except (TypeError, ValueError):
        return []
    if n <= 0:
        return []
    if r <= 0 or r * 2 >= n:
        return [1.0] * n
    w = [1.0] * n
    for i in range(r):
        v = (i + 1) / (r + 1)
        w[i] = v
        w[n - 1 - i] = v
    return w


def edge_weights(length, ramp, lo_has_neighbor, hi_has_neighbor):
    """`feather_weights` 的**边界感知**版：只对「真有邻居」的那侧羽化。

    为什么必须有它（2026-09-23 修正的设计缺口）：整幅最外缘那一条（画面第 0
    行、时间轴首 token）**没有邻居**来补权重。若也在那里羽化，本块内容会被压到
    `1/(r+1)`，归一化 (`/wsum`) 再把它放大回来 —— 数值上仍是原值，但**权重退化
    到 0.11**：一旦该块结果有偏差，外缘就被放大 `(r+1)` 倍（噪声显式放大）。

    铁律：**羽化是「与邻居交接」的事，不是「淡出」**。只有真接壤才羽化。
    `ramp` 必须恰等于咬合带宽度，两侧才严格互补为 1。返回 list[float]。

    ⚠ `ramp` 的合法性是**按侧**判的，不是 `feather_weights` 那种「两端合计不能
    超过全长」：这里可能只有一侧接壤（另一侧是链/画布端点，不羽化），此时 ramp
    占满大半长度也完全合法。故只有 `r >= n`（连一侧都放不下）才退化。
    """
    n = int(length)
    r = int(ramp)
    if n <= 0:
        return []
    sides = int(bool(lo_has_neighbor)) + int(bool(hi_has_neighbor))
    if r <= 0 or sides == 0 or r >= n or (sides == 2 and r * 2 >= n):
        return [1.0] * n
    w = [1.0] * n
    for i in range(r):
        if lo_has_neighbor:
            w[i] = (i + 1) / (r + 1)
        if hi_has_neighbor:
            w[n - 1 - i] = (i + 1) / (r + 1)
    return w


# ---- 精化空间 tile（把 H/W 切网格）----

# 档位表：下拉值 -> (沿高切几份, 沿宽切几份)。off = 不切。
# 「2x1」= 横向 2 条（沿 H 切 2 份，每份整宽）；「1x2」= 竖向 2 条。
TILE_MODES = {
    "off": (1, 1),
    "2x2": (2, 2),
    "3x3": (3, 3),
    "4x4": (4, 4),
    "2x1": (2, 1),
    "1x2": (1, 2),
}


def tile_grid(mode):
    """档位名 -> (rows, cols)；不认识的一律 (1,1)（= 不切，绝不静默切成怪形状）。"""
    try:
        r, c = TILE_MODES.get(str(mode or "off").strip().lower(), (1, 1))
    except Exception:
        return (1, 1)
    return (max(1, int(r)), max(1, int(c)))


def plan_tiles(h, w, mode, overlap, min_side=16):
    """把 H×W 切成网格 -> [(hs, he, ws, we, chs, che, cws, cwe, eff_ov), ...]。

    每项 = 一段在**像素/特征图坐标**上的切片：
    - `hs/he`、`ws/we`：喂给采样器的区间（含四周 overlap 上下文）
    - `chs/che`、`cws/cwe`：本块**写回**的区间（相邻块的这些区间在 overlap 带上**咬合重叠**）
    - `eff_ov`：**实际生效**的 overlap（可能被 `min_side` 上限压小）。
      融合时羽化 ramp 必须取它 —— 取请求值会与真实咬合带宽度不符 → 权重不互补。

    融合时两块在咬合带内按互补羽化权重混合（见 `edge_weights`）。

    ⚠ **core 是「咬合」不是「划分」**（2026-09-23 修正）：
    早先 core 是 `_split` 出来的**不重不漏硬划分**，于是「overlap + 羽化」根本没
    发生 —— 相邻块各自只写自己那一半、接缝处**硬切**，羽化权重形同摆设。
    现在每块 core 在自己的边界处**向外多要 `ov` 的一半**，与邻居在 `[b-ov/2, b+ov/2)`
    这段带宽内互相覆盖、按互补权重混合 —— 这才是「重叠融合」的本义。
    因此 `sum(core)` **大于** H×W（重叠部分被两块各写一次），不是划分。

    `min_side`：块的最小边长（像素/特征图单位）。网格切得过细会把画面切成
    碎片（既费算力又让全局构图彻底断裂），所以边小于它时**自动降档**到能切
    的档位。切不动就返回单片（不切）。

    ⚠ 与 `plan_refine_chunks` 的 overlap **不同量纲**：这里是像素/特征图像素，
    那边是 latent token。别把两个值互相套用。

    ⚠ 视频比图像严重：每块独立去噪 → 块边会**帧间抖动**（噪声轨迹不同）。
      故默认只推荐 2×2（每块仍有 1/4 画幅）。
    """
    try:
        H, W = int(h), int(w)
    except (TypeError, ValueError):
        return []
    if H <= 0 or W <= 0:
        return []
    try:
        ov = max(0, int(overlap))
    except (TypeError, ValueError):
        ov = 0
    rows, cols = tile_grid(mode)
    # 自动降档：任何一维切完边长不足 min_side 就减半网格，直到装得下
    while (rows > 1 or cols > 1) and (H // rows < min_side or W // cols < min_side):
        if rows > 1 and H // rows < min_side:
            rows = max(1, rows // 2) if rows > 2 else 1
        if cols > 1 and W // cols < min_side:
            cols = max(1, cols // 2) if cols > 2 else 1
        if rows == 1 and cols == 1:
            break
    if rows <= 1 and cols <= 1:
        return [(0, H, 0, W, 0, H, 0, W, 0)]
    # overlap 上限 = 半块边长（同分块，防「净推进趋近 0」）；**取偶数**，
    # 让 half = ov//2 两边对称、两块合起来正好铺满 ov 的咬合带。
    ov = min(ov, max(1, min(H // rows, W // cols) // 2))
    ov = max(0, ov - (ov % 2))
    out = []
    hs_list = _split(H, rows)
    ws_list = _split(W, cols)
    for hi, (rs, re_) in enumerate(hs_list):
        for wi, (cs, ce) in enumerate(ws_list):
            # 喂给采样器的区间：在核心区外再向四周扩 `ov` 的上下文
            hs = max(0, rs - ov)
            he = min(H, re_ + ov)
            ws = max(0, cs - ov)
            we = min(W, ce + ov)
            # 写回区间（core，咬合）：**外缘不扩**（画布外没有邻居），
            # 内边界向外要 `ov//2`（与邻居共享咬合带；两块合起来正好铺满 `ov`）。
            half = ov // 2
            chs = 0 if hi == 0 else max(0, rs - half)
            che = H if hi == rows - 1 else min(H, re_ + half)
            cws = 0 if wi == 0 else max(0, cs - half)
            cwe = W if wi == cols - 1 else min(W, ce + half)
            out.append((hs, he, ws, we, chs, che, cws, cwe, ov))
    return out


def _split(total, parts):
    """把 `total` 尽量均分成 `parts` 段 -> [(s,e), ...]（余数摊给前面的段）。

    均分而不是「整除+末段兜底」：末段过大会让它成为新的显存峰值点，
    原本想平摊峰值，结果峰值还落在一个超大的末块上。
    """
    parts = max(1, int(parts))
    if parts == 1:
        return [(0, total)]
    base = total // parts
    rem = total % parts
    out = []
    s = 0
    for i in range(parts):
        n = base + (1 if i < rem else 0)
        out.append((s, s + n))
        s += n
    return out


# ---- OOM 自救 ----

def is_oom_error(exc):
    """是不是显存不足（duck-typed，**不 import torch**，故本模块仍零依赖）。

    判类型名（torch.OutOfMemoryError）而不是 isinstance(torch.OutOfMemoryError)——
    后者会逼 perf.py 依赖 torch，破坏「纯函数、可在无 GPU 环境单测」的前提。
    兜底看消息：CUDA / ROCm / MPS 的 OOM 文案都含 "out of memory"，
    cuBLAS 那一路是 "CUBLAS_STATUS_ALLOC_FAILED"（它不走 torch 的 OOM 类型）。
    """
    if exc is None:
        return False
    try:
        if type(exc).__name__ in ("OutOfMemoryError", "OutOfMemoryException"):
            return True
    except Exception:
        pass
    msg = str(getattr(exc, "args", exc) or exc) or ""
    low = msg.lower()
    return ("out of memory" in low
            or "cublas_status_alloc_failed" in low
            or "cudnn_status_alloc_failed" in low
            or "mps backend out of memory" in low)


def oom_retry(fn, cleanup=None, tries=2, on_retry=None):
    """OOM 自救：捕获 OOM → `cleanup()` → **原参**重试；仍 OOM 才上抛最后一次。

    返回 `(结果, 自救次数)`——自救次数 0 = 一次过，>0 = 真的救回来了一次
    （这个数字是要打进报告的：它能证明「这段是靠自救活下来的」）。

    为什么必须**原参**重试（不改步数/画布/帧数）：自救的目的是确认「这次 OOM
    是显存碎片/残留引用导致的偶发，还是规格真的超了」。降规格重试能救活的
    那次会掩盖后者，用户下次换个段照样炸，且不知道是自己规格开大了。
    救不回来时由调用方给可行动的中文报错（降画布/降帧数），那才是正解。

    `cleanup` 由调用方给（各腾挪点的卸载口径不同：有的全卸、有的只卸放大网络），
    本模块因此不必知道 comfy 的存在。cleanup 自己抛不算故障（吞掉继续）。
    """
    n = max(1, int(tries or 1))
    last = None
    for i in range(n):
        try:
            return fn(), i
        except Exception as e:              # 只拦 OOM，其余原样上抛
            if not is_oom_error(e):
                raise
            last = e
            if i + 1 >= n:
                break
            if on_retry is not None:
                try:
                    on_retry(i + 1, e)
                except Exception:
                    pass
            if cleanup is not None:
                try:
                    cleanup()
                except Exception:
                    pass
    raise last


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


# ============================ 运行时控制（会改进程状态） ============================

# 启动时的 upcast attention 原值，"auto" 靠它还原（运行时改过之后就读不到原值了）
_UPCAST_BOOT = {}


def upcast_attention_state():
    """当前 upcast attention 的实际状态（只读镜像）；量不到 → None。

    ComfyUI 把它当**模块级常量**用（comfy/ldm/modules/attention.py:78）：
        FORCE_UPCAST_ATTENTION_DTYPE = model_management.force_upcast_attention_dtype()
        get_attn_precision(): if args.dont_upcast_attention: return None
    所以运行时改这两处即可生效 —— 不需要重启、不需要动启动参数。
    """
    try:
        from comfy import cli_args  # type: ignore
        import comfy.ldm.modules.attention as attn  # type: ignore
        a = cli_args.args
        forced = bool(getattr(a, "force_upcast_attention", False))
        dont = bool(getattr(a, "dont_upcast_attention", False))
        cur = getattr(attn, "FORCE_UPCAST_ATTENTION_DTYPE", None)
        return {"cli_force_upcast": forced, "cli_dont_upcast": dont,
                "module_dtype": None if cur is None else str(cur),
                "effective": bool(cur) and not dont}
    except Exception:
        return None


def apply_upcast_attention(value):
    """运行时开关 upcast attention（不动启动参数）——见 upcast_attention_state 的机制说明。

    value：`True` / `False` / `"auto"`（还原到进程启动时的设定）。
    返回实际生效值；环境不支持返回 None。

    取舍：开着（fp32 attention）在某些卡上更稳（老 macOS 有黑图 bug），
    代价是显存与耗时都上去；12 GB 这类小卡通常关掉更划算。
    **这是收益/代价都真实存在的选项，所以交给用户选，不替他决定。**
    """
    global _UPCAST_BOOT
    try:
        from comfy import cli_args  # type: ignore
        import comfy.ldm.modules.attention as attn  # type: ignore
        import comfy.model_management as mm  # type: ignore
    except Exception:
        return None
    a = cli_args.args
    if not _UPCAST_BOOT:
        _UPCAST_BOOT = {"force": bool(getattr(a, "force_upcast_attention", False)),
                        "dont": bool(getattr(a, "dont_upcast_attention", False))}
    if value in (None, "auto"):
        b = _UPCAST_BOOT
        setattr(a, "force_upcast_attention", b["force"])
        setattr(a, "dont_upcast_attention", b["dont"])
        attn.FORCE_UPCAST_ATTENTION_DTYPE = mm.force_upcast_attention_dtype()
        return None if attn.FORCE_UPCAST_ATTENTION_DTYPE is None else True
    if bool(value):
        setattr(a, "dont_upcast_attention", False)
        setattr(a, "force_upcast_attention", True)
        attn.FORCE_UPCAST_ATTENTION_DTYPE = mm.force_upcast_attention_dtype()
        return True
    setattr(a, "dont_upcast_attention", True)
    attn.FORCE_UPCAST_ATTENTION_DTYPE = None
    return False


def attn_backend_state():
    """当前 attention 后端名（只读镜像）；量不到 → None。"""
    try:
        import comfy.ldm.modules.attention as attn  # type: ignore
        fn = getattr(attn, "optimized_attention", None)
        name = getattr(fn, "__name__", None)
        return {"current": name or None}
    except Exception:
        return None


_ATTN_BOOT = {}


def apply_attn_backend(name):
    """运行时切 attention 后端（"auto" = 还原到进程启动时的那个）。

    返回实际生效的函数名；环境里没有该后端 → **None 且保持现状**。

    ⚠ 为什么「换不了就别换」而不是报错：SageAttention 在 30 系上有失败报告
    （部分 head dim / dtype 组合没有内核），换过去若它在前向里抛，整段就没了。
    拿不到候选函数就原地不动，比把 `optimized_attention` 置成 None 安全得多
    ——置空等于让所有 attention 走最慢的 fallback 或直接崩。
    """
    n = str(name or "auto").strip().lower()
    try:
        import comfy.ldm.modules.attention as attn  # type: ignore
    except Exception:
        return None
    global _ATTN_BOOT
    if not _ATTN_BOOT:
        _ATTN_BOOT["fn"] = getattr(attn, "optimized_attention", None)
    if n in ("", "auto"):
        fn = _ATTN_BOOT.get("fn")
        if fn is None:
            return None
        attn.optimized_attention = fn
        return getattr(fn, "__name__", None)
    cand = None
    for attr in ("attention_" + n, n + "_attention", n):
        c = getattr(attn, attr, None)
        if callable(c):
            cand = c
            break
    if cand is None:
        return None
    attn.optimized_attention = cand
    return getattr(cand, "__name__", None)


# 渲染期消费的键：改了不立刻动进程状态，而是在下一次渲染时由 nodes/upscale
# 读 `load_settings()` 生效。面板把它们算作「已接线」，但 apply_runtime 只能
# 回声存下来的值——这点必须在 UI 上说清，否则用户会以为点了没反应。
RUNTIME_LATER_KEYS = (
    "oom_autoretry", "act_peak_probe",
    "ff_chunk_on", "ff_chunk_tokens", "ff_chunk_min_tokens",
    "attn_head_on", "attn_head_chunks",
    "upscale_temporal_chunk", "upscale_chunk_frames", "upscale_overlap",
    "refine_temporal_on", "refine_temporal_chunk", "refine_temporal_overlap",
    "refine_tile_on", "refine_tile", "refine_tile_overlap", "refine_tile_feather",
    "blocks_swap_on", "blocks_to_swap", "blocks_prefetch",
    "keep_upscaler_resident", "vram_shuffle",
    "final_mode", "frames_dtype", "guard_action", "nvenc_cq", "encode_hq",
    "x264_crf",
    # 遗留修补（本轮）：守卫系数在 perf.probe_hardware 之后算守卫时才被读到。
    "offload_guard_ratio",
)


def apply_runtime(table):
    """把 perf 表里能**立刻作用到进程**的项应用上去 -> {键: 实际生效值}。

    分两半：能立刻改的（upcast / attention 后端 / 编码器与 crf / 素材库索引 /
    块交换的显存预留）在这里直接调；**渲染期才消费**的（各种分块、成片格式、
    守卫系数、cond 容量…）只把值**回声**出去，下一次渲染时由 nodes / upscale
    读 `load_settings()` 生效（名单见 `RUNTIME_LATER_KEYS`）。
    单项失败不影响其它项（返回里该键为 None）。
    """
    t = dict(table or {})
    out = {}
    out["upcast_attention"] = apply_upcast_attention(t.get("upcast_attention", "auto"))
    out["attn_backend"] = apply_attn_backend(t.get("attn_backend", "auto"))
    # 编码器：写进 media 的进程级默认（下一次编码生效；正在跑的编码不受影响）
    try:
        try:
            from . import media  # type: ignore
        except ImportError:
            import media  # type: ignore
        out["encoder"] = media.set_encoder(t.get("encoder", "libx264"),
                                           t.get("nvenc_cq", 20))
        # x264 的质量档另走一条通道：media 的进程级 ENCODER_CRF（NVENC 不看它）
        try:
            media.set_crf(t.get("x264_crf", 20))
        except AttributeError:
            pass
    except Exception:
        out["encoder"] = None
    for k in RUNTIME_LATER_KEYS:
        out[k] = t.get(k)
    try:
        try:
            from . import library  # type: ignore
        except ImportError:
            import library  # type: ignore
        out["index_mode"] = library.set_index_mode(t.get("index_mode", "fingerprint"))
        out["thumb_on_import"] = library.set_thumb_on_import(
            t.get("thumb_on_import", True))
        try:
            mp = float(t.get("thumb_max_mp") or 0)
            if mp > 0:
                library.THUMB_MAX_PIXELS = int(mp * 1000000)
        except (TypeError, ValueError):
            pass
        out["thumb_max_mp"] = library.THUMB_MAX_PIXELS / 1000000.0
    except Exception:
        pass
    # 块交换的进程级那半边（显存预留）**保存即生效** —— 用户点了保存就想看到反馈，
    # 不该等到下次渲染。模型侧的预取开关在 nodes.py 拿到模型后装
    # （upscale.install_block_prefetch）；这里 hw=None 给不出 UNET 真实体量，
    # blocks_to_swap=-1 时先按基线走，渲染开始会用真实体量重算一次。
    try:
        out["blockswap"] = apply_blockswap(t)
    except Exception:
        pass
    return out


# ============================ 全局性能设置（跨项目落盘） ============================

def settings_path():
    """全局性能设置文件：<user>/minimax_h3/perf.json（与素材库同级，跨项目共用）。

    性能设置是**机器相关**的（这台卡多大、内存多少），不是项目数据 ——
    所以放全局而不是项目 manifest，换项目不该丢。
    """
    try:
        try:
            from . import asset_store  # type: ignore
        except ImportError:
            import asset_store  # type: ignore
        root = ""
        try:
            root = asset_store.try_library_root() or ""
        except Exception:
            root = ""
        if not root:
            return ""
        return os.path.join(os.path.dirname(root), "perf.json")
    except Exception:
        return ""


def load_settings():
    """读全局性能设置；读不到给一份默认副本。"""
    p = settings_path()
    if not p or not os.path.isfile(p):
        return dict(DEFAULT_PERF)
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return dict(DEFAULT_PERF)
    return parse_state(raw)


# 渲染路径读设置的缓存：按 mtime 失效。
#
# 为什么需要它：一批设置（oom_autoretry / frames_dtype / final_mode / vram_shuffle…）
# 是**渲染期**消费的，段循环里每段都会问一次。每段读一次 JSON 不算贵，但段数
# 一多就是几十次无谓的磁盘 IO，而且中途用户在面板改了设置也应当立刻被看到
# ——mtime 变了就重读，两件事都满足。
_SETTINGS_CACHE = {"mtime": None, "table": None}


def current_settings(refresh=False):
    """渲染路径读设置（带 mtime 缓存）；返回**副本**，调用方改它不污染缓存。"""
    p = settings_path()
    try:
        mt = os.path.getmtime(p) if (p and os.path.isfile(p)) else None
    except OSError:
        mt = None
    if (not refresh and _SETTINGS_CACHE["table"] is not None
            and _SETTINGS_CACHE["mtime"] == mt):
        return dict(_SETTINGS_CACHE["table"])
    t = load_settings()
    _SETTINGS_CACHE["mtime"] = mt
    _SETTINGS_CACHE["table"] = dict(t)
    return dict(t)


def save_settings(table):
    """写全局性能设置（原子写）；路径不可用时返回 None。"""
    p = settings_path()
    if not p:
        return None
    clean = parse_state(dict(table or {}))
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:
        return None
    return clean


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


def _tensor_bytes(t):
    """单个张量的字节数（duck-typed）；不是张量 → 0。"""
    n = getattr(t, "numel", None)
    es = getattr(t, "element_size", None)
    if callable(n) and callable(es):
        try:
            return int(n()) * int(es())
        except Exception:
            return 0
    return 0


def _patch_bytes(patch, _seen=None):
    """一条 LoRA patch 里的张量字节合计。

    ComfyUI 的 `ModelPatcher.patches[key]` 是「补丁列表」，单层结构随来源变
    （LoRA 是 `(strength, weights_dict, ...)`，DoRA/Wildcard 又不同），
    所以这里**递归展开**容器直到撞见张量，而不是硬编码某一层下标——
    硬编码下标在换一个补丁来源时就会静默算成 0，比不算更糟。
    """
    seen = set() if _seen is None else _seen
    stack = [patch]
    total = 0
    while stack:
        cur = stack.pop()
        if isinstance(cur, (str, bytes, int, float, bool)) or cur is None:
            continue
        if id(cur) in seen:      # 补丁结构里可能有共享引用，防环也防重复计数
            continue
        seen.add(id(cur))
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
        else:
            total += _tensor_bytes(cur)
    return total


def probe_lora_footprint(model):
    """LoRA 权重账：**ComfyUI 算得到**的那部分（duck-typed，不 import torch）。

    与 `weight_bytes(model)`（它优先 `model_size()`，把 patches 也算进去了）
    交叉验证用：`model_size − 裸模型` 应当 ≈ 这里的 `lora_weight_gb`。
    两边对不上说明有补丁没走 `patches`（比如自定义 adapter），值得报出来。

    ⚠ 它**算不到** bypass 型 adapter 的**前向激活**——那才是真凶（D2 里单次
    6.13GB）。激活只能靠 `watch_vram_peak()` 在第一步采样时实测，见
    `lora_account_line()` 把两半账拼成一行。

    返回 {"patch_groups", "patch_count", "lora_weight_gb"}；没有 patches 时
    给全 0（不是 None——调用方要的是「没挂 LoRA」这个结论，不是「量不到」）。
    """
    patches = getattr(model, "patches", None)
    if not isinstance(patches, dict) or not patches:
        return {"patch_groups": 0, "patch_count": 0, "lora_weight_gb": 0.0}
    total = 0
    count = 0
    for v in patches.values():
        items = v if isinstance(v, (list, tuple)) else [v]
        count += len(items)
        for one in items:
            total += _patch_bytes(one)
    return {"patch_groups": len(patches), "patch_count": count,
            "lora_weight_gb": round(total / GB, 2)}


def suggest_act_reserve(act_peak_gb, safety=1.2):
    """按实测激活峰值给「该给激活留多少空」的建议（GB）；量不到 → None。"""
    try:
        p = float(act_peak_gb)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    return round(p * float(safety or 1.2), 1)


def lora_account_line(lora_weight_gb, patch_count, act_peak_gb=None, safety=1.2):
    """把「LoRA 账」拼成一行（权重 + 实测激活 + 建议留空）。

    `权重 +12.4GB（50 patches）· 实测单步激活峰值 6.1GB · 建议留空 ≥7.3GB`

    为什么这一行必须存在：官方的 `model_size()` 只算权重，而 bypass adapter
    的激活**不在任何账上**。用户看到「模型 31.7GB / 卡 23.5GB → 每步重读
    9.3GB」会以为只是慢，实际是随时可能 OOM。把两半账写在一行，
    「留空该留多少」才第一次有了数字依据。
    """
    try:
        w = float(lora_weight_gb)
    except (TypeError, ValueError):
        w = 0.0
    bits = [f"权重 +{w:.1f}GB（{int(patch_count or 0)} patches）"]
    if act_peak_gb:
        bits.append(f"实测单步激活峰值 {float(act_peak_gb):.1f}GB")
        res = suggest_act_reserve(act_peak_gb, safety)
        if res:
            bits.append(f"建议留空 ≥{res:.1f}GB")
    else:
        bits.append("激活峰值未实测（开 act_peak_probe 后首次采样可得）")
    return "LoRA 账：" + " · ".join(bits)


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
