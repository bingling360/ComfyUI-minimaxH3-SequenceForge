"""MiniMax H3 参考视频 token 压缩（Spark-VPack 内联版）。

来源：`zechengtang/Spark-H3` 的 **Spark-VPack v0.1.0**（release tag
`spark-vpack-v0.1.0`，源码 `comfyui/compact_ref2va/__init__.py` → 根
`comfyui_ref2va.py` 的 `class SparkVPack`）。上游是个独立节点
（`model/conditioning/minimax`，IN `model`+`positive`+`compression_ratio`，
OUT `model`+`positive`）；本模块把同一套计算内联进渲染链。

## 上游节点为什么进不了本仓画布（2026-10-16 逐条核实）

上游靠 `positive` 往返接线：`原生 Ref2VA 的 positive → Spark-VPack → 引导器`。
本仓主节点是「cond 构建 + 采样」的合体，**没有 CONDITIONING 端口，画布上也没有
BasicGuider 节点**（默认工作流只有 `H3SeamlessChainSampler → PreviewAny`），而且
`cond_i` 依赖 `latent_{i-1}`（段间引导桥 / 手动锚 / 重摇都要拿上一段采样结果现算），
所以「先过子节点再进采样」在结构上不成立；就算硬加 cond 输出→输入回环，
`comfy_execution/graph.py` 会直接抛 `DependencyCycleError`。结论：**内联**。

## 它到底做了什么（三件事）

1. **抽帧**：`cond[i][1]["minimax_refs"]` 里 `kind in ("video","video_audio")` 的
   引用项，按 `latent_t` 在时间维 `index_select` 抽掉一部分 latent token。
   分块口径由 H3 视频 VAE 的 `FRAME_PER_TOKEN=(1,4,4,4,4)` 决定：**每 5 个 latent
   token 一块**（token 0 覆盖 1 像素帧、其后各 4 帧 → 一块正好 17 像素帧，即
   `17k+5` 网格的一格），每块保留 head + 若干相位：

       0/5  (0,1,2,3)  不压缩        （等价于关闭）
       1/5  (0,1,2)    压 20%
       2/5  (0,2)      压 40%（上游推荐默认）
       3/5  (2,)       压 60%

2. **写元数据**：往被压的 ref 上写 4 个键（`compact_ref2va_rope_time_scale`
   = 1.0、`_rope_time_mode` = "chunk_phase"、`_dense_latent_t` = 原 dense 帧数、
   `_dense_indices` = 保留下来的原下标）。

3. **打模型补丁**：`model.clone()` + `set_model_unet_function_wrapper`，每次
   forward 读 `args["c"]["minimax_payload"]`，把 `layout.position_ids` 的时间列
   重写成**抽剩帧的原始时间坐标**，并把该段之后所有行的偏移补回来。
   （不修的话模型会把参考视频理解成一段被压缩时长的视频 —— 动作变快、时长对不上。）

## 为什么它可以内联、也可以做成模型链子节点（关键性质）

**这个模型补丁是无状态的**：它只按每段 ref 的元数据键决定做什么，压缩比本身
完全活在 cond 里。所以补丁可以幂等预装、与「用哪个 ratio」解耦；`_MARKER` 打在
layout 对象上，重复安装/多次 forward 只会生效一次。

## 与本仓其它 cond 改动的正交性（不用纠结先后）

- 语义桥（`semantic_bridge`）只改 `cond[i][0]` 的 `[B,T,5120]` 张量；
- VPack 只改 `cond[i][1]["minimax_refs"][k]["latent"]`；
- 锚定（`_apply_guide`）只写 `minimax_keyframes` 与元数据。

三者互不重叠，先后结果逐字节相同。

## 与上游节点的**三处刻意分歧**

- 本段**没有**参考视频时：上游 `pack()` 直接抛 `ValueError`（它的用户必然接了
  视频）。本仓是多段链，同一份设置会作用到**每一段**，没有视频引用的段必须
  静默原样返回 —— 否则开一次 VPack 整条链就废了。
- 只实现 `chunk_phase` 一个 RoPE 模式（上游另有 10 个消融模式，全部是它自己
  的 A/B 实验通道）。读到未知模式直接报错，不静默跳过。
- **锚点行也一起补时间偏移**：上游只把 refs 之后的行平移回去，锚点（keyframes）
  的 cond 段排在 refs 之前、不补，于是锚点与它要锚的目标帧错开整整一个跨度差。
  上游的典型用法里没有 cond 段，本仓段间引导桥/手动锚让「参考视频 + keyframes
  同段共存」成为常态，所以必须补（详见 `apply_rope` 里的注释）。

## 硬报错口径

开关是用户显式设置（与语义桥、锚定同档），所以**校验失败一律硬报错**，不软降级：
`apply()` 里校验 ref 形状；`_apply_rope()` 里校验 layout 契约。软降级的代价是
「压了 token 却没修 RoPE」—— 画面悄悄坏掉，比整链报错糟得多。

## 设置归属

**机器级、全局**，与 `perf.json` / `refvid.json` 同目录的 `vpack.json`，理由同
`refvideo.py` 那段注释：那是「机器多大」、那是「素材怎么处理」、这是「参考视频
压多少」，混在一个文件里重置一个会顺手清掉另一个。UI 入口在导演台右栏
「🗂 参考视频缩放」面板里（用户指定的位置），但存储分开。
"""

import hashlib
import json
import os
import threading

import torch

# ============================ 分块与档位（纯 python，可单测） ============================

FRAME_PER_TOKEN = (1, 4, 4, 4, 4)
"""H3 视频 VAE 的时间压缩：token 0 覆盖 1 像素帧，其后每个 token 覆盖 4 帧。
与上游 `comfyui_ref2va.py` 同名常量一致，也与本仓 `grid.py` 的 17k+5 口径同源。"""

FRAME_RESCALE = 5.0 / 3.0
"""像素帧 → RoPE 时间坐标的缩放（上游同值，`grid.FRAME_RESCALE` 同值）。"""

COMPRESSION_PROFILES = {
    "0/5": (0, 1, 2, 3),
    "1/5": (0, 1, 2),
    "2/5": (0, 2),
    "3/5": (2,),
}
"""档位 → 每块保留的 normal 相位（head 恒保留）。键即 UI 档位名。"""

RATIOS = tuple(COMPRESSION_PROFILES)
DEFAULT_RATIO = "2/5"

ROPE_SCALE_KEY = "compact_ref2va_rope_time_scale"
ROPE_MODE_KEY = "compact_ref2va_rope_time_mode"
ROPE_DENSE_LATENT_T_KEY = "compact_ref2va_dense_latent_t"
ROPE_DENSE_INDICES_KEY = "compact_ref2va_dense_indices"
MODE_CHUNK_PHASE = "chunk_phase"

_MARKER = "_vpack_physical_time_applied"
"""打在 PackedLayout 对象上的「已修正」标记（上游用同名不同串的私有标记）。
同一 layout 跨去噪步复用，靠它保证只修一次。"""

VIDEO_KINDS = ("video", "video_audio")


def chunk_phase_indices(dense_latent_frames, phases):
    """dense 帧数 + 保留相位 -> 要留下的原下标（升序）。

    每 5 个 token 一块：恒留块首（head），再留 `phases` 指定的 normal 相位。
    末块可能不足 5 个 token，`phases` 里越界的相位自动跳过。

    纯 Python（不碰张量）：这是「压多少」唯一的行为定义处，值得独立可测 ——
    上游同一段逻辑由 `_chunk_phase_indices` + `_compression_dense_indices` 两层
    拼出来，本仓合并成一层（另一层只是把档位名映射成 phases）。
    """
    n = int(dense_latent_frames)
    if n <= 0:
        raise ValueError(f"参考视频 latent 帧数必须为正，实为 {dense_latent_frames}")
    want = sorted({int(p) for p in phases})
    if any(p < 0 or p > 3 for p in want):
        raise ValueError(f"保留相位必须落在 0-3，实为 {phases}")
    sel = []
    for start in range(0, n, len(FRAME_PER_TOKEN)):
        sel.append(start)                                   # head 恒留
        normal = list(range(start + 1, min(start + len(FRAME_PER_TOKEN), n)))
        sel.extend(normal[p] for p in want if p < len(normal))
    return sel


def compression_indices(dense_latent_frames, ratio):
    """档位名 -> 保留的原下标。未知档位直接报错（不静默当不压缩）。"""
    key = str(ratio)
    if key not in COMPRESSION_PROFILES:
        raise ValueError(
            f"参考视频压缩档位非法：{ratio!r}（可选：{', '.join(COMPRESSION_PROFILES)}）")
    return chunk_phase_indices(dense_latent_frames, COMPRESSION_PROFILES[key])


def kept_fraction(dense_latent_frames, ratio):
    """保留比例（报告用；dense_latent_frames <= 0 时返回 1.0）。"""
    n = int(dense_latent_frames)
    if n <= 0:
        return 1.0
    return len(compression_indices(n, ratio)) / float(n)


# ============================ ds / vpack.json 契约 ============================

DEFAULT_VPACK = {
    # 总开关。默认**关** = 完全透传（今天的行为，一个字节不变），不进指纹。
    "on": False,
    # 压缩档位（"0/5" 等价于关闭：不抽帧、不装补丁、不进指纹）
    "ratio": DEFAULT_RATIO,
}

VPACK_TYPES = {"on": (bool,), "ratio": (str,)}


def parse_state(raw):
    """归一化设置片段（面板写回同一入口）。未知键丢弃、枚举越界回落默认。

    与 `refvideo.parse_state` 同口径；`on` 只认真 True（前端 `=== true` 同源），
    避免 JSON 里手写 `"false"` 这种非空字符串被 `bool()` 判成开启。
    """
    out = dict(DEFAULT_VPACK)
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        types = VPACK_TYPES.get(k)
        if types is None or v is None:
            continue
        if bool in types and isinstance(v, bool):
            out[k] = v
            continue
        if not isinstance(v, bool) and str in types and isinstance(v, str):
            out[k] = v
    if out["ratio"] not in COMPRESSION_PROFILES:
        out["ratio"] = DEFAULT_RATIO
    return out


def enabled(cfg):
    """是否**真的**要压：开 且 档位不是 0/5。

    「0/5」= 不压缩，语义上与关闭逐字节等价 —— 所以它同样不进指纹、不装补丁、
    不动 cond。把这条判据收在一处，避免三个调用点各写一遍 `!= "0/5"`。
    """
    c = cfg if isinstance(cfg, dict) else {}
    return c.get("on") is True and str(c.get("ratio")) != "0/5"


def signature(cfg):
    """进逐段指纹的短标签（None = 不加键）。

    只有**生效**时才返回非 None：出厂默认（关）与「0/5」都不加键，既有项目存档
    续跑零影响。加了键就是新指纹 —— 改档位 = 引用视频的段全部重做，这是对的：
    它改的是进模型的参考张量。
    """
    if not enabled(cfg):
        return None
    return str(parse_state(cfg)["ratio"])


def report_line(cfg):
    """给人看的报告行（未生效返回 None）。"""
    if not enabled(cfg):
        return None
    c = parse_state(cfg)
    return (f"参考视频压缩：Spark-VPack 内联，档位 {c['ratio']}"
            f"（每 5 个 latent token 一块抽帧 + RoPE 时间坐标还原）")


# ============================ 张量侧：cond 变换 ============================


def apply(cond, cfg, info=None):
    """对 conditioning 逐项压缩参考视频 latent。未生效 / 本段无视频 → 原样返回。

    返回**新列表 + 新元数据 dict**（`minimax_refs` 是重写的新表，被压的 ref 是
    `dict(ref)` 副本）：不原地改上游 cond，重摇/二采各自重建 cond 时互不干扰。

    info：可选 dict，真的压了时被填上
    `{"videos": 压了几个, "before": 原 token 总数, "after": 压后 token 总数}`，
    供调用方写报告行 —— 静默变换是这个项目最忌的，用户要能一眼看到「压掉了多少」。
    """
    if not enabled(cfg):
        return cond
    ratio = str(parse_state(cfg)["ratio"])
    out = []
    packed = 0
    before_t = after_t = 0
    for item in cond:
        tensor, meta = item[0], item[1]
        refs = meta.get("minimax_refs") if isinstance(meta, dict) else None
        if not isinstance(refs, list) or not refs:
            out.append(item)
            continue
        new_refs = []
        item_packed = 0
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("kind") not in VIDEO_KINDS:
                new_refs.append(ref)
                continue
            latent = ref.get("latent")
            dense_t = int(ref.get("latent_t") or 0)
            if (not torch.is_tensor(latent) or latent.ndim != 5
                    or dense_t != int(latent.shape[2])):
                raise ValueError(
                    "参考视频 latent 与 latent_t 不一致（VPack 需要原生 Ref2VA 的 "
                    f"[B,C,T,H,W] latent）：kind={ref.get('kind')!r}，"
                    f"latent_t={dense_t}，latent={tuple(latent.shape) if torch.is_tensor(latent) else type(latent).__name__}")
            idx = compression_indices(dense_t, ratio)
            sel = torch.as_tensor(idx, dtype=torch.long, device=latent.device)
            new_ref = dict(ref)
            new_ref["latent"] = latent.index_select(2, sel)
            new_ref["latent_t"] = len(idx)
            new_ref.update({
                ROPE_SCALE_KEY: 1.0,
                ROPE_MODE_KEY: MODE_CHUNK_PHASE,
                ROPE_DENSE_LATENT_T_KEY: dense_t,
                ROPE_DENSE_INDICES_KEY: list(idx),
            })
            new_refs.append(new_ref)
            item_packed += 1
            before_t += dense_t
            after_t += len(idx)
        if not item_packed:
            out.append(item)
            continue
        new_meta = dict(meta)
        new_meta["minimax_refs"] = new_refs
        out.append([tensor, new_meta])
        packed += item_packed
    # 本段没有参考视频：静默原样返回（上游在这里抛错，本仓是多段链，见模块头注释）
    if not packed:
        return cond
    if isinstance(info, dict):
        info.update({"videos": packed, "before": before_t, "after": after_t})
    return out


# ============================ 张量侧：RoPE 时间坐标修正 ============================


def video_rope_span(latent_frames):
    """latent 帧数 -> 该段在 RoPE 时间轴上的跨度（上游 `_video_rope_span`）。"""
    return sum(FRAME_RESCALE * FRAME_PER_TOKEN[i % len(FRAME_PER_TOKEN)]
               for i in range(int(latent_frames)))


def video_rope_offsets(latent_frames, device=None):
    """dense 时间轴各 token 的起点（exclusive cumsum，上游 `_video_rope_offsets`）。"""
    n = int(latent_frames)
    if n <= 0:
        raise ValueError(f"latent 帧数必须为正，实为 {latent_frames}")
    spans = torch.tensor(
        [FRAME_RESCALE * FRAME_PER_TOKEN[i % len(FRAME_PER_TOKEN)] for i in range(n)],
        dtype=torch.float64, device=device)
    return torch.cat((torch.zeros(1, dtype=torch.float64, device=device),
                      spans[:-1].cumsum(0)))


def _visual_segments(layout, refs):
    """把 refs 逐个对到 PackedLayout 里的视觉段 (start, stop)；非视觉项给 None。

    端口自上游 `_reference_layout_segments`：段表顺序是 text →（cond/cond_audio，
    即 keyframes）→ refs 各自的视觉段/音频段 → audio → video。keyframes 的
    cond 段在开头连续排布，先整体跳过再逐 ref 走。
    """
    raw = list(getattr(layout, "segments", ()) or ())
    if not raw or any(not isinstance(s, (tuple, list)) or len(s) != 3 for s in raw):
        raise ValueError("当前 ComfyUI 的 MiniMax-H3 PackedLayout.segments 契约不受支持"
                         "（VPack 需要 (start, stop, kind) 三元组表）")
    segs = [(int(a), int(b), str(k)) for a, b, k in raw]
    if segs[0][2] != "text" or any(
            a < 0 or b <= a or (i and a != segs[i - 1][1])
            for i, (a, b, _k) in enumerate(segs)):
        raise ValueError("当前 ComfyUI 产出的 MiniMax-H3 段表不是连续表")
    i = 1
    while i < len(segs) and segs[i][2] in ("cond", "cond_audio"):
        i += 1
    out = []
    for ref in refs:
        kind = ref.get("kind")
        if kind == "image":
            if i >= len(segs) or segs[i][2] != "ref_img":
                raise ValueError("MiniMax-H3 参考图与 PackedLayout 段表不匹配")
            out.append((segs[i][0], segs[i][1]))
            i += 1
        elif kind == "audio":
            if int(ref.get("ref_audio_t", 0)) > 0:
                if i >= len(segs) or segs[i][2] != "ref_audio":
                    raise ValueError("MiniMax-H3 参考音频与 PackedLayout 段表不匹配")
                i += 1
            out.append(None)
        elif kind in VIDEO_KINDS:
            if int(ref.get("ref_audio_t", 0)) > 0:
                if i >= len(segs) or segs[i][2] != "ref_audio":
                    raise ValueError("MiniMax-H3 参考视频音轨与 PackedLayout 段表不匹配")
                i += 1
            if i >= len(segs) or segs[i][2] != "ref_img":
                raise ValueError("MiniMax-H3 参考视频与 PackedLayout 段表不匹配")
            out.append((segs[i][0], segs[i][1]))
            i += 1
        else:
            raise ValueError(f"不支持的 MiniMax-H3 引用类型 {kind!r}")
    return out


def apply_rope(payload):
    """就地修正一个 payload 的 layout 时间坐标；返回是否改动过。

    端口自上游 `_apply_physical_time_rope`，**只保留 `chunk_phase` 分支**
    （上游另有 10 个消融模式，是它自己的 A/B 通道）。已修正过的 layout 直接跳过。
    """
    if not isinstance(payload, dict):
        return False
    layout = payload.get("layout")
    refs = payload.get("refs")
    if layout is None or not isinstance(refs, list):
        return False
    if getattr(layout, _MARKER, False):
        return False
    marked = [r.get(ROPE_MODE_KEY) for r in refs
              if isinstance(r, dict) and r.get(ROPE_MODE_KEY)]
    unknown = sorted({str(m) for m in marked if m != MODE_CHUNK_PHASE})
    if unknown:
        raise ValueError(
            f"不支持的参考视频 RoPE 修正模式 {unknown}"
            "（本仓只实现 chunk_phase；上游 VPack 的消融模式未移植）")
    if not marked:
        return False

    visual = _visual_segments(layout, refs)
    starts = [s[0] for s in visual if s is not None]
    first_ref_start = min(starts) if starts else None
    pos = getattr(layout, "position_ids", None)
    if (not torch.is_tensor(pos) or pos.ndim != 2 or int(pos.shape[1]) != 3
            or int(pos.shape[0]) != int(layout.segments[-1][1])):
        raise ValueError("当前 ComfyUI 产出的 MiniMax-H3 position_ids 布局不受支持")

    corrected = pos.clone()
    tail_delta = 0.0
    for ref, seg in zip(refs, visual):
        if not isinstance(ref, dict) or ref.get(ROPE_MODE_KEY) != MODE_CHUNK_PHASE:
            continue
        if seg is None:
            continue
        dense_t = int(ref.get(ROPE_DENSE_LATENT_T_KEY, 0))
        indices = ref.get(ROPE_DENSE_INDICES_KEY)
        latent_frames = int(ref.get("latent_t") or 0)
        if (not isinstance(indices, (list, tuple)) or dense_t <= 0 or latent_frames <= 0
                or len(indices) != latent_frames
                or int(indices[0]) < 0 or int(indices[-1]) >= dense_t
                or any(b <= a for a, b in zip(indices, indices[1:]))):
            raise ValueError("chunk_phase 的 dense 下标表非法"
                             f"（dense_latent_t={dense_t}，latent_t={latent_frames}，"
                             f"indices={indices!r}）")
        start, stop = seg
        temporal = corrected[start:stop, 0]
        if temporal.numel() == 0 or int(torch.unique(temporal).numel()) != latent_frames:
            raise ValueError("参考视频在 PackedLayout 里的时间坐标行数与 latent 不符"
                             f"（段 {start}:{stop}，latent_t={latent_frames}）")
        origin = temporal[0].clone()
        idx = torch.tensor([int(v) for v in indices], dtype=torch.long,
                           device=temporal.device)
        dense_times = video_rope_offsets(dense_t, device=temporal.device).index_select(0, idx)
        rows_per_frame, remainder = divmod(int(temporal.numel()), latent_frames)
        if remainder:
            raise ValueError("参考视频的 layout 行数不是帧对齐的")
        corrected[start:stop, 0] = origin + dense_times.repeat_interleave(rows_per_frame)
        # 本段跨度变了 → 它之后所有行（目标视频/音频）整体平移，保持相对时间关系
        audio_span = float(ref.get("ref_audio_t", 0))
        delta = (max(audio_span, video_rope_span(dense_t))
                 - max(audio_span, video_rope_span(latent_frames)))
        tail_delta += delta
        if delta:
            corrected[stop:, 0] += delta

    # 锚点行（keyframes）同样坐在**目标时间轴**上 —— 官方 PackedLayout 的注释写得很
    # 明白：「anchors count from the target timeline origin」。压缩把目标原点整体提前
    # 了，上面那批 `corrected[stop:, 0]` 只补了 refs **之后**的行；锚点的 cond 段排在
    # refs **之前**，不补就会与它要锚的那一帧错开整整 tail_delta（在 12→8 token 的
    # 例子里约 22 个时间单位，目标视频自身跨度才 ~207 —— 约 10%，不是舍入误差）。
    #
    # ⚠ 这是**与上游 VPack 的第三处刻意分歧**：上游 `_apply_physical_time_rope` 不做
    # 这一步，因为它的典型用法里 cond 段不存在。本仓段间引导桥 / 手动锚 / 尾帧锚让
    # 「参考视频 + keyframes 同段共存」成为常态，所以必须补。
    if tail_delta and first_ref_start is not None:
        for a, b, kind in layout.segments:
            if kind in ("cond", "cond_audio") and b <= first_ref_start:
                corrected[a:b, 0] += tail_delta

    layout.position_ids = corrected
    setattr(layout, _MARKER, True)
    return True


def install(model, cfg):
    """给 MODEL 装 RoPE 时间坐标修正；返回**可用于采样**的模型。

    未生效 / 模型不支持原生 MiniMax H3 时原样返回（不报错 —— 是否装了补丁由
    `patchable()` 判定，调用方在报告里点名）。装的方式与上游一致：`clone()` + 写
    `model_options["model_function_wrapper"]`，并**串联**已有 wrapper（别人先装的
    补丁不能被我们顶掉）。

    为什么返回 clone 而不是原地装：`模型` 是 loader 的**缓存对象**，原地改
    `model_options` 会跨执行残留。clone 是本仓既有做法（`upscale._shifted_model`
    / `_stg_model` 都是 clone 后喂给采样器），权重与 patches 共享、零拷贝成本。
    """
    if not enabled(cfg) or model is None:
        return model
    if not (hasattr(model, "clone") and hasattr(model, "get_model_object")
            and hasattr(model, "set_model_unet_function_wrapper")
            and isinstance(getattr(model, "model_options", None), dict)):
        return model
    try:
        dit = model.get_model_object("diffusion_model")
    except Exception:
        return model
    if not any(t.__name__ == "MiniMaxH3Model" for t in type(dit).__mro__):
        return model

    previous = model.model_options.get("model_function_wrapper")

    def model_function_wrapper(apply_model, args):
        conditioning = args["c"].copy()
        apply_rope(conditioning.get("minimax_payload"))
        forwarded = args | {"c": conditioning}
        if previous is not None:
            return previous(apply_model, forwarded)
        return apply_model(forwarded["input"], forwarded["timestep"], **conditioning)

    patched = model.clone()
    patched.set_model_unet_function_wrapper(model_function_wrapper)
    return patched


def patchable(model, cfg):
    """这个模型**能不能**装 VPack 的 RoPE 修正补丁？（护栏用；未生效时恒 True）

    只回答「装得上吗」，不回答「装了吗」—— 装没装由 `install()` 的返回值决定。
    用途是防**静默失效**：模型不是原生 MiniMax H3 时 `install()` 会原样返回（不抛，
    因为它只是个「能不能」的问题，不该把整链带崩），于是「压了 token 却没修 RoPE」
    会悄悄发生。调用方据此在报告里点名（本仓最忌的就是静默变换）。
    """
    if not enabled(cfg) or model is None:
        return True
    if not hasattr(model, "get_model_object"):
        return False
    try:
        dit = model.get_model_object("diffusion_model")
    except Exception:
        return False
    return any(t.__name__ == "MiniMaxH3Model" for t in type(dit).__mro__)


# ============================ 全局设置（跨项目落盘） ============================
#
# 归属：**机器级、全局**（与 perf.json / refvid.json 同级同目录）。
# 与 refvid.json 分开存：那是「素材怎么处理」，这是「参考视频压多少」，
# 混在一个文件里重置一个会顺手清掉另一个（同 refvideo.py 的判断）。

FILE_NAME = "vpack.json"


def settings_path():
    """<user>/minimax_h3/vpack.json（与 perf.json / refvid.json 同目录）。"""
    try:
        try:
            from . import perf  # type: ignore
        except ImportError:
            import perf  # type: ignore
        p = perf.settings_path()
        return os.path.join(os.path.dirname(p), FILE_NAME) if p else ""
    except Exception:
        return ""


def load_settings():
    """读全局设置；读不到给一份默认副本。"""
    p = settings_path()
    if not p or not os.path.isfile(p):
        return dict(DEFAULT_VPACK)
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return dict(DEFAULT_VPACK)
    return parse_state(raw)


# 渲染路径读设置：按 mtime 失效（与 perf.current_settings / refvideo.current 同口径）。
_CACHE = {"mtime": None, "table": None}
_LOCK = threading.Lock()


def current(refresh=False):
    """渲染路径读设置（带 mtime 缓存）；返回**副本**。"""
    p = settings_path()
    try:
        mt = os.path.getmtime(p) if (p and os.path.isfile(p)) else None
    except OSError:
        mt = None
    with _LOCK:
        if not refresh and _CACHE["table"] is not None and _CACHE["mtime"] == mt:
            return dict(_CACHE["table"])
        t = load_settings()
        _CACHE["mtime"] = mt
        _CACHE["table"] = dict(t)
        return dict(t)


def save_settings(table):
    """写全局设置（原子写）；路径不可用时返回 None。"""
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
    with _LOCK:
        _CACHE["mtime"] = None
        _CACHE["table"] = None
    return clean


__all__ = [
    "COMPRESSION_PROFILES", "RATIOS", "DEFAULT_RATIO", "DEFAULT_VPACK", "VPACK_TYPES",
    "FRAME_PER_TOKEN", "FRAME_RESCALE", "VIDEO_KINDS",
    "ROPE_SCALE_KEY", "ROPE_MODE_KEY", "ROPE_DENSE_LATENT_T_KEY", "ROPE_DENSE_INDICES_KEY",
    "MODE_CHUNK_PHASE",
    "chunk_phase_indices", "compression_indices", "kept_fraction",
    "parse_state", "enabled", "signature", "report_line",
    "apply", "apply_rope", "install", "patchable",
    "video_rope_span", "video_rope_offsets",
    "settings_path", "load_settings", "current", "save_settings",
]
