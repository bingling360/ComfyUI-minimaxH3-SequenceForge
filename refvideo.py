"""MiniMax H3 参考视频归一：缩放 / 24fps 重定时 / 17k+5 网格对齐 / 音轨同步。

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

- **帧率**：官方把帧序列当 24fps 用（Qwen 侧按 `FPS // 2` 抽帧、时间戳写
  `i / 2.0`）。30fps 素材因此**整体放慢 25%**，再被 `[:frame_count]` 砍掉尾巴
  —— 音画不同步是必然的。正确做法是按时间轴**重定时**（取 `round(i * src_fps / 24)`
  号源帧），输出时长与源时长一致。
- **音轨**：官方只截画面、音轨整轨编码，参考音轨比参考画面长。本模块给出同一个
  时间窗，并允许「不参考视频音频」（等价于视频只接图像接口）。

顺带解决一个内存问题：旧通路 `_decode_video_file` 用 `InputImpl.VideoFromFile`
**整片**解码成 float32（1080p/30fps/10s ≈ 300×1920×1080×3×4B ≈ **7.4GB** 常驻 CPU），
而 `media.decode_av` 早就支持「帧窗 + 早停」。本模块的 `src_end` 就是给帧窗用的，
解码端因此只需要读到真正会用到的最后一帧。

## 与官方口径的关系

出厂默认 `16:9 @ 0.98MP` = 1344×768，**恰好等于官方参考画布**（短边 768 /
面积上限 768×1344）——也就是「把官方隐式行为显式化，一个字节都不变」。
默认 `只缩不放` 同样对齐官方 `:322-324` 的那条回退。总开关默认**关闭**（透传，
即今天的官方行为）。
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

FITS = ("裁切填满", "补黑边", "拉伸")
RESAMPLES = ("lanczos", "bicubic", "bilinear", "area", "nearest-exact")
GRIDS = ("向下", "就近", "向上")
LIMITS = ("不限", "最长段", "自定义")

DEFAULT_AR = "16:9"
DEFAULT_MP = 0.98          # = 1344×768，官方参考画布

# ---- ds / refvid.json 契约（键名与前端一致）----
DEFAULT_REFVID = {
    # 总开关。默认**关** = 完全透传（今天的行为，一个字节不变）；
    # 开启后才进逐段指纹（见 nodes.py 的 seg_hashes）。
    "on": False,
    # 目标画布：宽高比 × 百万像素（官方 Resolution Selector 形式）
    "ar": DEFAULT_AR,
    "mp": DEFAULT_MP,
    # 只缩不放：源像素面积小于目标时不放大（官方 :322-324 同款）
    "no_upscale": True,
    # 宽高比不匹配时的适配方式
    "fit": "裁切填满",
    # 重采样方式（交给 comfy.utils.common_upscale）
    "resample": "lanczos",
    # fps 归一：按时间轴重定时到 24fps（关 = 源帧原样按 24fps 解释，会变速）
    "fps_norm": True,
    # 17k+5 网格对齐方向
    "grid": "向下",
    # 时长上限：不限 / 截到全链最长段时长 / 自定义秒
    # ⚠ 出厂默认「最长段」而不是「不限」：官方 `frames[:frame_count]` 决定了
    # 超出的帧根本进不了模型，留着只会按目标画布占内存（1344×768×124 帧 ≈ 1.5GB）。
    # 不论选哪一档，plan 里还有一条独立的硬上限（最长段 ×1.5 + 一格，下限 124）。
    "limit": "最长段",
    "limit_s": 0.0,
    # 参考视频音频：True = 作为 <Audio j> 参考（含静音占号）；False = 只参考画面
    "audio": True,
}

REFVID_TYPES = {
    "on": (bool,), "ar": (str,), "mp": (int, float),
    "no_upscale": (bool,), "fit": (str,), "resample": (str,),
    "fps_norm": (bool,), "grid": (str,), "limit": (str,),
    "limit_s": (int, float), "audio": (bool,),
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


def snap_grid(n, mode="向下"):
    """帧数 -> 就近的 17k+5 网格（官方 `while n % 17 != 5: n -= 1` 的显式版）。

    向下 = 官方行为（绝不补帧）；就近 = 上下都行；向上 = 会重复末帧补足。
    结果恒 >= 5（官方硬下限）。
    """
    n = max(0, int(n))
    if n <= 5:
        return 5 if n else 0
    k = (n - 5) / 17.0
    if mode == "就近":
        k = round(k)
    elif mode == "向上":
        k = int(k) if float(k).is_integer() else int(k) + 1
    else:
        k = int(k)
    return 17 * max(0, int(k)) + 5


def pad_box(src_w, src_h, dst_w, dst_h):
    """补黑边：源等比缩到框内后的内框尺寸与左上偏移 -> (iw, ih, x, y)。

    内框两边取偶（编码器友好）；缩放比 > 1 不拦（是否放大由 no_upscale 决定）。
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
    if out["grid"] not in GRIDS:
        out["grid"] = DEFAULT_REFVID["grid"]
    if out["limit"] not in LIMITS:
        out["limit"] = DEFAULT_REFVID["limit"]
    out["limit_s"] = min(max(float(out["limit_s"]), 0.0), 60.0)
    return out


def plan(info, cfg, max_seg_frames=0, label=""):
    """素材探针 + 配置 -> 归一方案（纯计算，不碰张量）。

    info 来自 `media.probe_media`：{fps, frames, duration_s, width, height, has_audio}。
    max_seg_frames = 全链最长段的帧数（`limit == "最长段"` 时用它当上限；也用于
    报告「素材比本段长，模型只会用前 N 帧」这条提示）。

    返回 dict：
      w/h            目标画布（32 对齐）
      out_frames     归一后的帧数（24fps 时间轴，已网格对齐/限长）
      src_idx        要保留的**源帧号**（升序，去重）
      src_end        解码帧窗上界（exclusive）—— 交给 media.decode_av 的 end_f
      a_start/a_end  音轨时间窗（秒）
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
    # ---- ⓪ 硬上限（与「时长上限」无关的内存闸门）----
    #
    # 官方 `frames[:frame_count]` 决定了**模型最多只会用到本段帧数**，所以多归一
    # 出来的帧既进不了模型、又要在 CPU 侧按目标画布占内存：
    # 1344×768 float32 × 124 帧 ≈ 1.5GB，10s 素材不设闸就是 3GB+（还没算源侧解码）。
    # 所以这里给一条**独立于用户设置**的闸门：最长段的 1.5 倍 + 一格网格，
    # 下限 124（一个标准段）；拿不到段长信息时按官方训练上限 362 帧（~15s）。
    _guard = (max(int(int(max_seg_frames) * 1.5) + 17, 124)
              if int(max_seg_frames or 0) > 0 else 362)
    if dur > _guard / FPS + 1e-9:
        notes.append(f"素材 {dur:.2f}s 超出归一硬上限 {_guard} 帧（{_guard / FPS:.2f}s）："
                     f"只归一前 {_guard / FPS:.2f}s（模型最多只会用到本段帧数，"
                     "多余的帧只占内存与 VAE 开销）")

    # ---- ① 时间窗（时长上限）----
    cap_s = float(_guard) / FPS
    if cfg["limit"] == "最长段" and int(max_seg_frames or 0) > 0:
        cap_s = min(cap_s, float(max_seg_frames) / FPS)
    elif cfg["limit"] == "自定义" and cfg["limit_s"] > 0:
        cap_s = min(cap_s, float(cfg["limit_s"]))
    take_s = min(dur, cap_s)
    if take_s < dur:
        notes.append(f"按「{cfg['limit']}」截取前 {take_s:.2f}s（源 {dur:.2f}s）")

    # ---- ② 24fps 时间轴上的帧数 ----
    # avail = 源时长在 24fps 下**不用补帧**就能给出的帧数；raw_out 先按它封顶，
    # 网格对齐（③）允许越过它（就近/向上会重复末帧，那是用户选的档位，如实报告）。
    avail = max(1, int(round(dur * FPS)))
    if cfg["fps_norm"]:
        raw_out = min(int(round(take_s * FPS)), avail)
    else:
        # 不做 fps 归一：源帧原样保留，被当作 24fps 用（时长 = 帧数/24，会变速）
        raw_out = min(src_frames, max(1, int(round(take_s * src_fps))))
        avail = src_frames
        notes.append(f"已关闭 fps 归一：源帧按 24fps 解释，时长将变为 "
                     f"{raw_out / FPS:.2f}s（源 {dur:.2f}s）")
    raw_out = max(1, raw_out)

    # ---- ③ 17k+5 网格对齐 ----
    out = snap_grid(raw_out, cfg["grid"])
    if out != raw_out:
        notes.append(f"帧数 {raw_out} → {out}（{cfg['grid']}对齐 17k+5，"
                     f"时长 {raw_out / FPS:.2f}s → {out / FPS:.2f}s）")

    # ---- ④ 源抽帧表（重定时）----
    #
    # ⚠ **不去重**。源 fps 低于 24 时（12fps 素材）`round(i × 12 ÷ 24)` 必然撞号，
    # 但那是「每帧在 24fps 时间轴上显示两次」，是重定时的正确结果；一去重就等于
    # 把时长砍半（5s 素材变成 2.25s、放快一倍）。同理源不够长时尾部会重复末帧
    # ——那是网格对齐的既定代价，由「网格对齐」档位决定，如实报告即可。
    idx = []
    for i in range(out):
        j = int(round(i * src_fps / FPS)) if cfg["fps_norm"] else i
        idx.append(min(max(0, j), src_frames - 1))
    if not idx:
        raise ValueError(f"参考视频「{label}」解不出可用帧（源 {src_frames} 帧 @{src_fps:g}fps）")
    if cfg["fps_norm"] and src_fps < FPS - 1e-6:
        notes.append(f"源 {src_fps:g}fps 低于 24fps：每帧在 24fps 时间轴上重复 "
                     f"{len(idx) / max(1, len(set(idx))):.2f} 次（时长不变；"
                     "低于 24fps 的素材无法凭空补出中间帧）")
    _need_last = int(round((out - 1) * src_fps / FPS)) if cfg["fps_norm"] else out - 1
    if _need_last > src_frames - 1:
        _pad = sum(1 for j in idx if j >= src_frames - 1)
        notes.append(f"素材只有 {src_frames} 帧（{dur:.2f}s）：末 {_pad} 帧重复末帧补足 "
                     f"{out} 帧（「网格对齐」选「向下」可避免）")

    # ---- ⑤ 目标画布（只缩不放）----
    sw, sh = int(info.get("width") or 0), int(info.get("height") or 0)
    tw, th = resolve_canvas(cfg["ar"], cfg["mp"])
    no_up = False
    if cfg["no_upscale"] and sw > 0 and sh > 0 and sw * sh < tw * th:
        tw = max(CANVAS_MULTIPLE, round(sw / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
        th = max(CANVAS_MULTIPLE, round(sh / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
        no_up = True

    # ---- ⑥ 适配方式 ----
    fit = cfg["fit"]
    crop = {"裁切填满": "center", "补黑边": "pad", "拉伸": "disabled"}[fit]
    pad = pad_box(sw or tw, sh or th, tw, th) if crop == "pad" else (tw, th, 0, 0)

    # ---- ⑦ 音轨时间窗（与画面同一段）----
    a_start, a_end = 0.0, out / FPS

    main = (f"参考视频「{label or '素材'}」{sw or '?'}×{sh or '?'} @{src_fps:g}fps "
            f"{dur:.2f}s（{src_frames} 帧） → {tw}×{th} @{FPS}fps {out / FPS:.2f}s"
            f"（{out} 帧 · {cfg['grid']}对齐 17k+5） · {fit}/{cfg['resample']}"
            + ("· 只缩不放" if no_up else "")
            + (f" · 音轨同步裁 [0.00, {a_end:.2f})s" if cfg["audio"] else " · 音轨不参与参考"))
    if int(max_seg_frames or 0) > 0 and out > int(max_seg_frames):
        notes.append(f"素材 {out} 帧长于本链最长段 {int(max_seg_frames)} 帧"
                     f"（{int(max_seg_frames) / FPS:.2f}s）：模型只会用到前 "
                     f"{int(max_seg_frames)} 帧，多余的帧只增加 VAE 开销"
                     "（「时长上限」选「最长段」可省掉）")
    return {
        "w": int(tw), "h": int(th), "out_frames": int(out),
        "src_idx": idx, "src_end": (max(idx) + 1) if idx else 0,
        "src_fps": src_fps, "src_frames": src_frames, "src_dur": dur,
        "a_start": a_start, "a_end": a_end, "crop": crop, "pad": pad,
        "no_upscale_hit": no_up, "report": [main] + notes,
    }


# 进逐段指纹的字段：**只放会改变模型输入的项**。
# `on` 不进（开关本身由调用方判断）；其余全部影响参考张量或音轨分支。
_SIG_KEYS = ("ar", "mp", "no_upscale", "fit", "resample",
             "fps_norm", "grid", "limit", "limit_s", "audio")


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
