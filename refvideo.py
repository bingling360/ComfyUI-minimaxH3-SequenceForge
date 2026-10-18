"""MiniMax H3 参考视频归一：缩放到目标画布 / 官方 17k+5 口径 / 音轨同窗。

与 `grid.py` / `perf.py` 同级：**纯函数、零 ComfyUI 依赖**（只 import 标准库），
这样托管 venv（只有 pytest）也能把决策逻辑真跑一遍。张量运算（lanczos 缩放、
补黑边、切片）留在 `nodes.py`，本模块只负责「算」—— 目标宽高、抽帧表、
音频时间窗、以及给人看的那几行报告。

## 为什么要有它（三条都是现状缺口，2026-10-16 查证）

官方 `MiniMaxH3ReferenceToVideo`（`comfy_extras/nodes_minimax_h3.py:315-350`）
对参考视频**已经**做了两件事，但口径写死、且是隐式的：

1. `adapt_canvas()`：短边 768、面积上限 768×1344、32 对齐 → 缩到该画布；
2. `frames[:frame_count]` 截到本段帧数，再 `while n % 17 != 5: n -= 1`
   **向下砍**到 17k+5 网格（最多砍 16 帧 = 0.67s）。

**没做**的两件事是本模块存在的理由：

- **音轨**：官方只截画面、音轨整轨编码，参考音轨比参考画面长。本模块给出同一个
  源时间窗，并允许「不参考视频音频」（等价于视频只接图像接口）。
- **画布口径显式化**：把官方的 `adapt_canvas` 换成「宽高比 × 百万像素」的同一条
  换算，「只缩不放」也照样保留 —— 面板上能看见自己会被缩成多大。

**帧率不在此列**：官方把帧当 24fps 用（30fps 素材放慢 25%），本模块**照抄**，
不重定时、不抽帧、不补末帧（2026-10-18 用户拍板 + 把四个时间轴开关整个删掉：
参考视频只是参考，官方那种放慢没有副作用；重定时反而会因网格吸附把 4.20s 补成 4.46s）。

顺带解决一个内存问题：旧通路 `_decode_video_file` 用 `InputImpl.VideoFromFile`
**整片**解码成 float32（1080p/30fps/10s ≈ 300×1920×1080×3×4B ≈ **7.4GB** 常驻 CPU），
而 `media.decode_av` 早就支持「帧窗 + 早停」。本模块的 `src_end` 就是给帧窗用的，
解码端因此只需要读到真正会用到的最后一帧。

## 与官方口径的关系

出厂默认 `16:9 @ 0.98MP` = 1344×768，**恰好等于官方参考画布**（短边 768 /
面积上限 768×1344）——也就是「把官方隐式行为显式化，一个字节都不变」。
`只缩不放` 写死 = 官方 `:322-324` 那条回退。总开关默认**关闭**（透传，
即今天的官方行为）。

时间轴相关的一切都**没有开关**：帧率、网格方向、时长上限全部照官方。
契约只剩 6 个键：`on / ar / mp / fit / resample / audio`（见 DEFAULT_REFVID）。

## 对齐参考视频时间（`align_target`）

动作迁移要的是「参考视频归一后的时长」与「每一段生成的时长」**逐帧相等**。
官方 `ref_video` 只是一个整段张量输入（没有「第 N 段取第几秒」），所以这条等式
只能靠两端落在**同一个 17k+5 网格点**上来满足。官方网格是**向下**砍（最多 16 帧），
所以归一后的时长略短于源时长 —— 段时长也照这个数写，两端才真的一样。
`align_target` 就是算这个数，**不动任何设置**。
"""

import hashlib
import json
import os
import threading

# 官方常量（comfy_extras/nodes_minimax_h3.py 同名值）
FPS = 24
CANVAS_MULTIPLE = 32
BASE_SHORT_EDGE = 768
MAX_PIXELS = 768 * 1344

# 画布换算（与 nodes._resolve_canvas / 官方 Resolution Selector 逐位一致）
AR_RATIO = {"21:9": 21 / 9, "16:9": 16 / 9, "9:16": 9 / 16,
            "4:3": 4 / 3, "3:4": 3 / 4, "1:1": 1.0}
MP_OPTIONS = [round(i * 0.1, 1) for i in range(1, 21)]   # 0.1–2.0MP，0.1 步进

# 官方训练区间上限（`length` 控件 tooltip：「trained range is ~124-362」）。
# 对齐目标超过它没有意义：>362 帧的段官方自己也没训过；而段卡秒数上限 15s
# 经 _snap_seconds 吸附正好也是 362 帧 —— 两处必须同口径，不许各写一个数。
TRAINED_MAX_FRAMES = 362

FITS = ("裁切填满", "补黑边", "拉伸")
RESAMPLES = ("lanczos", "bicubic", "bilinear", "area", "nearest-exact")

DEFAULT_AR = "16:9"
DEFAULT_MP = 0.98          # = 1344×768，官方参考画布

# ---- ds / refvid.json 契约（键名与前端一致）----
#
# 只有「缩放怎么缩 / 音轨要不要」这几项留给人选，**时间轴相关的四项一律按官方
# 口径写死**（2026-10-18 用户：「这几个选项都是多余的，按官方默认选项就行了，
# 都删了」）。写死的是：
#   · 只缩不放  → 官方 `if vw*vh < cw*ch` 的回退
#   · 不重定时  → 官方把帧当 24fps 播（30fps 素材慢 25%）
#   · 网格向下  → 官方 `while n % 17 != 5: n -= 1`
#   · 时长上限  → 官方 `frames[:frame_count]`（截到本段帧数）
# 所以它们**不是契约键**：不进 DEFAULT_REFVID、不进 parse_state、不进指纹。
DEFAULT_REFVID = {
    # 总开关。默认**关** = 完全透传（官方行为，一个字节不变）；
    # 开启后才进逐段指纹（见 nodes.py 的 seg_hashes）。
    "on": False,
    # 目标画布：宽高比 × 百万像素（官方 Resolution Selector 形式）
    "ar": DEFAULT_AR,
    "mp": DEFAULT_MP,
    # 宽高比不匹配时的适配方式
    "fit": "裁切填满",
    # 重采样方式（交给 comfy.utils.common_upscale）
    "resample": "lanczos",
    # 参考视频音频：True = 作为 <Audio j> 参考（含静音占号）；False = 只参考画面
    "audio": True,
}

REFVID_TYPES = {
    "on": (bool,), "ar": (str,), "mp": (int, float),
    "fit": (str,), "resample": (str,), "audio": (bool,),
}


def resolve_canvas(ar, mp):
    """宽高比 + 百万像素 -> 画布（官方 Resolution Selector 公式逐位移植）。

    1MP = 1024×1024 = 1048576 px（官方口径，非 1e6）；两侧各自 round 对齐 32 倍数。
    16:9 档位：0.5→960×544 / 0.98→1344×768（H3 原生）/ 1.2→1504×832；
    9:16 + 1.0 → 768×1376。
    """
    r = AR_RATIO[str(ar)]
    total = float(mp) * 1024 * 1024
    w0, h0 = (total * r) ** 0.5, (total / r) ** 0.5
    return (max(CANVAS_MULTIPLE, round(w0 / CANVAS_MULTIPLE) * CANVAS_MULTIPLE),
            max(CANVAS_MULTIPLE, round(h0 / CANVAS_MULTIPLE) * CANVAS_MULTIPLE))


def snap_grid(n):
    """帧数 -> 向下 17k+5 网格（官方 `while n % 17 != 5: n -= 1` 的显式版）。

    只有**向下**这一档：官方就是向下砍（最多 16 帧 = 0.67s）。曾经有「就近/向上」
    两档（会重复末帧把时长补回去），2026-10-18 已删除 —— 参考视频不需要为凑网格
    插帧。结果恒 >= 5（官方硬下限）。
    """
    n = max(0, int(n))
    if n <= 5:
        return 5 if n else 0
    k = (n - 5) // 17
    return 17 * max(0, int(k)) + 5


def pad_box(src_w, src_h, dst_w, dst_h):
    """补黑边：源等比缩到框内后的内框尺寸与左上偏移 -> (iw, ih, x, y)。

    内框两边取偶（编码器友好）；缩放比 > 1 不拦（放不放大由 plan 的"只缩不放"决定）。
    """
    sw, sh = max(1, int(src_w)), max(1, int(src_h))
    dw, dh = max(2, int(dst_w)), max(2, int(dst_h))
    s = min(dw / sw, dh / sh)
    iw = max(2, int(round(sw * s)) // 2 * 2)
    ih = max(2, int(round(sh * s)) // 2 * 2)
    iw, ih = min(iw, dw), min(ih, dh)
    return iw, ih, (dw - iw) // 2, (dh - ih) // 2


def parse_state(raw):
    """归一化前端/存档里读到的 refvid 片段（面板写回同一入口）。

    非 dict -> 一份默认副本（**不改 DEFAULT_REFVID 本体**）；未知键丢弃；
    类型不对的项整体回落到默认，不做猜测式强转；枚举值不在表里 -> 回落默认。
    """
    out = dict(DEFAULT_REFVID)
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        types = REFVID_TYPES.get(k)
        if types is None or v is None:
            continue
        if bool in types and isinstance(v, bool):
            out[k] = v
            continue
        if not isinstance(v, bool):
            if int in types and (isinstance(v, int)
                                 or (isinstance(v, float) and float(v).is_integer())):
                out[k] = int(v)
                continue
            if float in types and isinstance(v, (int, float)):
                out[k] = float(v)
                continue
        if str in types and isinstance(v, str):
            out[k] = v
    if out["ar"] not in AR_RATIO:
        out["ar"] = DEFAULT_AR
    if not isinstance(out["mp"], float):
        out["mp"] = float(out["mp"])
    out["mp"] = min(max(float(out["mp"]), 0.1), 2.0)
    if out["fit"] not in FITS:
        out["fit"] = DEFAULT_REFVID["fit"]
    if out["resample"] not in RESAMPLES:
        out["resample"] = DEFAULT_REFVID["resample"]
    return out


def plan(info, cfg, max_seg_frames=0, label=""):
    """素材探针 + 配置 -> 归一方案（纯计算，不碰张量）。

    info 来自 `media.probe_media`：{fps, frames, duration_s, width, height, has_audio}。
    max_seg_frames = 全链最长段的帧数（当截取上限，等同官方 `frames[:frame_count]`；也用于
    报告「素材比本段长，模型只会用前 N 帧」这条提示）。

    口径只有官方这一条（2026-10-18 删掉重定时 / 就近 / 向上 / 自定义时长）：
    源帧原样保留并按 24fps 播 → 向下砍到 17k+5 → 只缩不放 → 按 fit 适配。

    返回 dict：
      w/h            目标画布（32 对齐）
      out_frames     归一后的帧数（24fps 时间轴，已向下对齐网格/截到最长段）
      src_idx        要保留的**源帧号**（升序，与输出帧一一对应）
      src_end        解码帧窗上界（exclusive）—— 交给 media.decode_av 的 end_f
      a_start/a_end  音轨时间窗（秒，按**源**时长算，不是 24fps 时间轴）
      crop           "center"（裁切填满）/ "disabled"（拉伸）/ "pad"（补黑边）
      pad            (iw, ih, x, y)，仅 pad 模式有意义
      report         给人看的报告行（第 1 行为主行，其余为提示）
    素材不足官方下限（5 帧）时抛 ValueError —— 早报错好过官方节点跑到一半才炸。
    """
    cfg = parse_state(cfg)
    src_fps = float(info.get("fps") or 0) or float(FPS)
    src_frames = int(info.get("frames") or 0)
    dur = float(info.get("duration_s") or 0.0)
    if src_frames <= 0:
        src_frames = max(0, int(round(dur * src_fps)))
    if dur <= 0:
        dur = src_frames / src_fps if src_fps else 0.0
    if src_frames < 5:
        raise ValueError(
            f"参考视频「{label or info.get('path') or '素材'}」只有 {src_frames} 帧"
            f"（{dur:.2f}s @{src_fps:g}fps），不足官方下限 5 帧（约 0.2s）——请换更长的素材")

    notes = []
    # ---- ⓪ 硬上限（内存闸门，与官方 `frames[:frame_count]` 同一条逻辑）----
    #
    # 官方只取 `frames[:frame_count]`，所以多归一出来的帧既进不了模型、又要在 CPU
    # 侧按目标画布占内存：1344×768 float32 × 124 帧 ≈ 1.5GB。闸门 =「最长段 ×1.5
    # + 一格网格、下限 124 帧」；拿不到段长时按训练上限 362 帧（~15s）。
    #
    # ⚠ 闸门是**帧数**，换算成源秒数必须除以**源 fps**（一源帧 = 一输出帧，官方
    # 口径不重定时）。曾经这里恒除以 24：12fps/6s 的素材，124 帧被算成 5.17s 源，
    # 但源只有 12fps → 实际只解码 62 帧、输出 62 帧 —— 直接砍半。
    _guard = (max(int(int(max_seg_frames) * 1.5) + 17, 124)
              if int(max_seg_frames or 0) > 0 else 362)
    if dur > _guard / src_fps + 1e-9:
        notes.append(f"素材 {dur:.2f}s 超出归一硬上限 {_guard} 帧（{_guard / src_fps:.2f}s）："
                     f"只归一前 {_guard / src_fps:.2f}s（模型最多只会用到本段帧数，"
                     "多余的帧只占内存与 VAE 开销）")

    # ---- ① 时间窗：官方 `frames[:frame_count]` ----
    # 「截到本链最长段」= 官方行为（超出的帧根本进不了模型）；再夹一道硬上限。
    cap_frames = _guard
    if int(max_seg_frames or 0) > 0:
        cap_frames = min(cap_frames, int(max_seg_frames))
    take_s = min(dur, cap_frames / src_fps)
    if take_s < dur:
        # 官方口径的"截"是按**帧**截（take_s 是换算出来的秒），所以按帧报告，
        # 免得被误读成"少截了 0.07 秒"——差的那点其实是 2 帧。
        notes.append(f"按本链最长段截取前 {int(round(take_s * src_fps))} 帧"
                     f"（源 {src_frames} 帧 @{src_fps:g}fps，{take_s:.2f}s）")

    # ---- ② 帧数：源帧原样（官方把帧当 24fps 播，30fps 素材自然慢 25%）----
    raw_out = max(1, min(src_frames, int(round(take_s * src_fps))))

    # ---- ③ 17k+5 网格：官方向下砍 ----
    out = snap_grid(raw_out)
    if out != raw_out:
        notes.append(f"帧数 {raw_out} → {out}（向下对齐 17k+5，官方 `while n%17!=5: n-=1`，"
                     f"时长 {raw_out / FPS:.2f}s → {out / FPS:.2f}s）")

    # ---- ④ 源帧号：一一对应（不重定时、不抽帧、不补帧）----
    idx = list(range(out))
    if not idx:
        raise ValueError(f"参考视频「{label}」解不出可用帧（源 {src_frames} 帧 @{src_fps:g}fps）")

    # ---- ⑤ 目标画布（只缩不放 = 官方 `if vw*vh < cw*ch` 那条回退）----
    sw, sh = int(info.get("width") or 0), int(info.get("height") or 0)
    tw, th = resolve_canvas(cfg["ar"], cfg["mp"])
    no_up = False
    if sw > 0 and sh > 0 and sw * sh < tw * th:
        tw = max(CANVAS_MULTIPLE, round(sw / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
        th = max(CANVAS_MULTIPLE, round(sh / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
        no_up = True

    # ---- ⑥ 适配方式 ----
    fit = cfg["fit"]
    crop = {"裁切填满": "center", "补黑边": "pad", "拉伸": "disabled"}[fit]
    pad = pad_box(sw or tw, sh or th, tw, th) if crop == "pad" else (tw, th, 0, 0)

    # ---- ⑦ 音轨时间窗：与**画面用掉的那段源**同窗（不是 24fps 时间轴的秒数）----
    # 画面被放慢 25% 播，但音轨是真的时间；用 out/src_fps 才是这段素材真实时长。
    a_start, a_end = 0.0, out / src_fps

    main = (f"参考视频「{label or '素材'}」{sw or '?'}×{sh or '?'} @{src_fps:g}fps "
            f"{dur:.2f}s（{src_frames} 帧） → {tw}×{th} @{FPS}fps {out / FPS:.2f}s"
            f"（{out} 帧 · 向下对齐 17k+5） · {fit}/{cfg['resample']}"
            + ("· 只缩不放" if no_up else "")
            + (f" · 音轨同窗 [0.00, {a_end:.2f})s" if cfg["audio"] else " · 音轨不参与参考"))
    if int(max_seg_frames or 0) > 0 and out > int(max_seg_frames):
        notes.append(f"素材 {out} 帧长于本链最长段 {int(max_seg_frames)} 帧"
                     f"（{int(max_seg_frames) / FPS:.2f}s）：模型只会用到前 "
                     f"{int(max_seg_frames)} 帧，多余的帧只增加 VAE 开销")
    return {
        "w": int(tw), "h": int(th), "out_frames": int(out),
        "src_idx": idx, "src_end": (max(idx) + 1) if idx else 0,
        "src_fps": src_fps, "src_frames": src_frames, "src_dur": dur,
        "a_start": a_start, "a_end": a_end, "crop": crop, "pad": pad,
        "no_upscale_hit": no_up, "report": [main] + notes,
    }


# 进逐段指纹的字段：**只放会改变模型输入的项**。
# `on` 不进（开关本身由调用方判断）；其余全部影响参考张量或音轨分支。
_SIG_KEYS = ("ar", "mp", "fit", "resample", "audio")


def align_target(info, cfg=None, label=""):
    """参考视频 -> 项目对齐目标（帧数 / 秒数），**不改任何设置**。

    口径一句话：**官方怎么喂，就按多久对齐**。

      源帧原样按 24fps 用 —— 30fps 素材被放慢 25%（4.20s 的 126 帧 = 5.25s），
      再向下砍到 17k+5（官方 `frames[:frame_count]` + `while n%17!=5: n-=1`）。
      **不重定时、不插帧、不补末帧**。

    为什么按钮要"对齐"：官方 `ref_video` 是一个整段张量，没有「第 N 段取第几秒」；
    模型眼里的参考时长就是 `frames / 24`。段比它短 → 参考被截；段比它长 → 尾巴
    空转。所以对齐 = 把**每个引用视频的段**设成 frames/24，让段长与参考同长。

    ⚠ 这个函数**只读** cfg：以前那版会把设置改成"时钟完美"的那一档，结果近邻吸附
    把 4.20s 的素材变 4.46s（补 7 帧末帧）——用户 2026-10-18 明确否掉：参考视频
    只是参考，宁可官方那种放慢，也不要为对齐改设置或插帧。那几个开关现在连
    契约键都不是了（见 DEFAULT_REFVID 上方注释）。

    返回 dict：
      frames   对齐帧数（17k+5 网格点，24fps 时间轴）
      seconds  frames / 24（段卡秒数；362 帧那档夹到 15s，吸附后仍是 362 帧）
      report   `plan()` 的报告行（「源 30fps 4.20s → 24fps 5.17s」这种）
      notes    额外提示（超训练上限 / 受时长上限截短等）
      cfg      本次计算用的设置（只读副本，供调用方说明口径）

    素材不足官方下限 5 帧抛 ValueError（与 plan 同口径，早报错好过跑到一半才炸）。
    """
    c = dict(parse_state(cfg))
    notes = []

    src_fps = float(info.get("fps") or 0) or float(FPS)
    src_frames = int(info.get("frames") or 0)
    dur = float(info.get("duration_s") or 0.0)
    if dur <= 0 and src_frames > 0:
        dur = src_frames / src_fps
    if src_frames <= 0:
        src_frames = max(0, int(round(dur * src_fps)))
    if src_frames < 5 or dur <= 0:
        raise ValueError(
            f"参考视频「{label or info.get('path') or '素材'}」只有 {src_frames} 帧"
            f"（{dur:.2f}s @{src_fps:g}fps），不足官方下限 5 帧（约 0.2s）——请换更长的素材")

    # 官方口径只有一条路：帧原样按 24fps 播 → 向下砍网格。
    frames = snap_grid(src_frames)
    if frames > TRAINED_MAX_FRAMES:
        notes.append(f"参考时长超出官方训练上限 {TRAINED_MAX_FRAMES} 帧"
                     f"（{TRAINED_MAX_FRAMES / FPS:.2f}s）：段与参考都按 "
                     f"{TRAINED_MAX_FRAMES} 帧对齐，片尾会被截掉（模型也没训过更长的段）")
        frames = TRAINED_MAX_FRAMES

    seconds = min(15.0, frames / FPS)
    # 拿 plan 复核一次：渲染期就是这个入参（每段都是 frames 帧 → 最长段 = frames），
    # 保证「写进段卡的秒数」与「实际喂进模型的帧数」是同一个数。不通过只如实报告
    # （例如素材比闸门还短），不去改设置 —— 这里一个设置都不许动。
    p = plan(info, c, frames, label)
    if int(p["out_frames"]) != frames:
        notes.append(f"按当前设置实际归一为 {p['out_frames']} 帧"
                     f"（{p['out_frames'] / FPS:.2f}s，目标 {frames} 帧）：以实际值为准，"
                     "否则段会比参考长")
        frames = int(p["out_frames"])
        seconds = min(15.0, frames / FPS)

    return {
        "cfg": c, "notes": notes,
        "frames": int(frames), "seconds": float(seconds),
        "report": list(p["report"]),
        "src_fps": src_fps, "src_frames": int(src_frames), "src_dur": float(dur),
    }


def signature(cfg):
    """配置 -> 短指纹（进 seg_hash；改任一影响项都会让引用视频的段重做）。"""
    c = parse_state(cfg)
    key = json.dumps({k: c[k] for k in _SIG_KEYS}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


# ============================ 全局设置（跨项目落盘） ============================
#
# 归属：**机器级、全局**（与 perf.json 同级同目录）—— 用户 2026-10-16 拍板。
# 与 perf.json 分开存：那是「机器多大」的资源档位，这是「素材怎么处理」的
# 内容口径，混在一个文件里语义会打架（重置一个会顺手清掉另一个）。

FILE_NAME = "refvid.json"


def settings_path():
    """<user>/minimax_h3/refvid.json（与 perf.json 同目录）。"""
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
        return dict(DEFAULT_REFVID)
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return dict(DEFAULT_REFVID)
    return parse_state(raw)


# 渲染路径读设置：按 mtime 失效（与 perf.current_settings 同口径）。
# 段循环里每段都会问一次，不缓存就是几十次无谓 IO；缓存了用户改完设置
# 也能立刻被看到（mtime 变了就重读）。
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
        if (not refresh and _CACHE["table"] is not None
                and _CACHE["mtime"] == mt):
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
