"""断点续跑：共享参数指纹 + 逐段提示词哈希 + manifest 原子读写 + 段 AV latent 落盘。

断点只存采样输出的 latent（约 5 MB/段），不存解码像素（约 300 MB/段）：
续跑时重解码秒级回放，结果与一次跑完逐帧一致（种子序列由 manifest 权威记录）。

v2：指纹只覆盖共享参数（不含提示词、不含种子），改某段提示词仍指向同一条链；
提示词按段存哈希，改了第 N 段 -> 找到变更区间重建（见下）。

v4（2026-09-16 修正旧注释）：**重做不需要级联。** 段 N 可用双锚对齐邻居——
上锚取段 N-1 存档 latent 的尾部，下锚取段 N+1 存档 latent 的头部（`_redo_next_anchor`）。
所以下游段已经生成好的前提下，改某段只重建该段即可，不必连带其后的段。
（旧注释此处写「真·唯一硬约束只有分辨率」——2026-10-12 起已作废，见下方 v6。）

v5（2026-09-16）：变更检测改**区间模型**（重做最小单位 = 单段，离散区间各自双锚）：
- 区间计算迁到 `anchors.change_intervals`，本模块的 `reroll_start`（返回单一 start、
  隐含级联语义）已删除——级联不是默认行为，留着就是第二个真相。
- `assert_match` 收窄为**只校验 width/height**，其余参数变更只回报不报错、不触发重做。

v6（2026-10-12）：**链参数不再强制整链一致**——分辨率也不再是硬约束。
- `assert_match` 从此**从不抛错**，只回报差异（含分辨率），由调用方写进报告。
  中途改分辨率时：旧段保留旧分辨率、此后新段用新分辨率；相邻段分辨率不同则由
  `nodes.py` 跳过「自动引用上段尾帧」与接缝测量，成片阶段跳过自动合成——都只提示。
- 自动项目目录名不再跟参数走（旧版 `h3chain_<w>x<h>_..._<指纹>` 会让「改个分辨率
  就换项目」）：空存档目录时固定用 `h3chain`，并沿用最近更新过的旧版自动目录。

v3：存档根目录迁至 output/h3_projects/<项目名>/（游戏式一项目一文件夹），
manifest 增加 title/created_at/updated_at/finals 键；旧 checkpoints 目录不读不写。

torch / folder_paths 延迟导入：指纹与 manifest 逻辑在无 ComfyUI 环境下可单测。
"""

import hashlib
import json
import os
import re
import tempfile
import threading

SCHEMA = "h3seamless/ckpt-v3"

# 自动项目目录名（「存档目录」留空时用）。**刻意不含任何参数**：旧版把分辨率/时长/
# 指纹写进目录名，导致「改个分辨率就换了项目」——2026-10-12 起改为固定名。
AUTO_PROJECT_NAME = "h3chain"

# ---- 生成互斥锁（剪辑/转码与采样不能同时执行） ----
# nodes.execute 进入时 +1、退出时 -1；trim/slice/move/split 等路由遇忙回 423，
# 前端据此置灰按钮。进程内计数（单 Comfy 进程单队列，够用；多进程部署时
# 前端再以 Comfy /queue 运行态做第二道 pre-check）。
_BUSY = 0
_BUSY_LOCK = threading.Lock()


def mark_busy() -> int:
    """标记一次生成开始，返回当前忙计数。"""
    global _BUSY
    with _BUSY_LOCK:
        _BUSY += 1
        return _BUSY


def unmark_busy() -> int:
    """标记一次生成结束，返回当前忙计数（防负）。"""
    global _BUSY
    with _BUSY_LOCK:
        _BUSY = max(0, _BUSY - 1)
        return _BUSY


def is_busy() -> bool:
    """是否正在生成（剪辑/转码/移动入口用，遇忙回 423）。"""
    with _BUSY_LOCK:
        return _BUSY > 0


def fingerprint(params: dict) -> str:
    """共享参数 -> 8 位十六进制指纹（sha256，跨进程稳定）。

    params 由调用方保证不含提示词与种子：种子控件开着 control_after_generate
    每次运行自动 +1，提示词改动走逐段哈希校验而非换链。

    ⚠ 2026-10-12 起**不再用于自动项目目录命名**（那正是「改个分辨率就换项目」的
    根因）。保留供诊断/复现比对用。
    """
    blob = json.dumps(params, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:8]


def prompt_hash(prompt: str) -> str:
    """单段提示词 -> 8 位十六进制哈希（与共享参数指纹同法）。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]


def image_hash(frame) -> str:
    """单帧图 [H,W,3] 0-1 tensor -> 8 位十六进制像素哈希（分镜关键帧失效判定用）。

    uint8 量化后哈希：VAE 解码的微小浮点抖动（<1/255）不改变哈希，
    断点续跑重解码同一 latent 不会误触发重跑。
    """
    import torch

    arr = (frame.detach().float().clamp(0.0, 1.0).cpu().numpy() * 255.0).astype("uint8")
    return hashlib.sha1(arr.tobytes()).hexdigest()[:8]


def save_keyframe(root: str, idx: int, frame) -> str:
    """分镜关键帧原图副本 -> keyframes/kf_NNN.png（长边 512，断点自包含、面板可显示）。"""
    name = f"kf_{idx:03d}.png"
    try:
        from PIL import Image

        arr = (frame.detach().float().clamp(0.0, 1.0).cpu().numpy() * 255.0).astype("uint8")
        img = Image.fromarray(arr)
        w, h = img.size
        scale = 512.0 / max(w, h)
        if scale < 1.0:
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        kdir = os.path.join(root, "keyframes")
        os.makedirs(kdir, exist_ok=True)
        img.save(os.path.join(kdir, name))
        return name
    except Exception:
        return ""


def truncate(root: str, manifest: dict, start: int) -> dict:
    """丢弃第 start 段起的进度：截断 manifest 各列表并立即原子落盘，删除被弃段文件。

    截断即时落盘：截断后立刻崩溃也不会复活旧进度。段文件含 latent(.pt)、
    分段视频(.mp4) 与缩略图(.png)，三者同下标一起清，防止重跑后残留旧画面
    （二采渲染与基础段同名——seg_NNN.mp4 被清即高清记录自然失效）。
    二采记录 manifest.upscale.segs 同段号联动截断，旧版独立产物
    （upseg_*/upthumb_*/uplast_*）一并清理；基础段重做后其二采必然失效，
    重跑时按新 base_hash 自动补渲染。
    返回更新后的 manifest。
    """
    out = dict(manifest)
    out["done"] = start
    # 重摇标记只清「被重做区间覆盖」的槽位：slot >= start 的段本来就要重做，
    # 标记无意义 → 清；slot < start 的段保留存档、标记仍然有效 → 必须留
    #（与调用方对 inserts 的过滤同口径，见 nodes.py 存档续跑处）。
    # 旧实现无条件清空：改靠后的段会误杀靠前的重摇标记（10 段全完成 → 标段 3
    # 重摇 → 改段 8 提示词使 start=7 → 段 3 标记被连带清掉）。
    if out.get("redo_queue"):
        _kept = []
        for _x in out["redo_queue"]:
            try:
                _s = int(_x[0])
            except (TypeError, ValueError, IndexError):
                continue          # 顺带滤掉畸形条目
            if _s < start:
                _kept.append(_x)
        out["redo_queue"] = _kept
    # 注意：manifest 里没有 "anchors" 键——anchor 是**请求侧**字段（ds.segments[i].anchors），
    # 只按段并进 prompt_hashes，不落盘成 manifest 列表（旧实现这里留了个永不命中的键，已删）。
    # ⚠ `base_videos`（一采另存，checkpoint.base_seg_files）与 `seg_res`（逐段分辨率）
    # 必须一起截断：漏掉前者会让被弃段的一采入口留在段卡上指向已删文件；漏掉后者会让
    # 「跨分辨率跳过桥接」的判定读到被弃段的旧值。两者都是**槽位对齐**列表，短了不报错、
    # 但错位会静默走错分支。
    for key in ("seeds", "trims", "prompt_hashes", "thumbs", "videos", "prompts",
                "seams", "bridge_scores", "seam_metrics", "base_videos", "seg_res"):
        if key in out:
            out[key] = list(out[key])[:start]
    up = out.get("upscale")
    if isinstance(up, dict) and len(up.get("segs") or []) > start:
        up2 = dict(up)
        up2["segs"] = list(up.get("segs"))[:start]
        out["upscale"] = up2
    save_manifest(root, out)
    # 段文件清理：`(?:base_)?` 前缀让一采另存件（base_seg_NNN.mp4 / base_thumb_NNN.png）
    # 与本体同口径一起清。`re.match` 从头锚定，所以旧版只会漏掉带前缀的那些，不会误伤。
    pat = re.compile(r"(?:base_)?seg_(\d{3,})\.(?:pt|mp4)$")
    thumb_pat = re.compile(r"(?:base_)?thumb_(\d{3,})\.png$")
    up_pat = re.compile(r"upseg_(\d{3,})\.(?:pt|mp4)$")
    up_thumb_pat = re.compile(r"up(?:thumb|last)_(\d{3,})\.png$")
    for sub in ("", "finals"):
        d = root if sub == "" else os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            m = pat.match(name) or thumb_pat.match(name) or up_pat.match(name) or up_thumb_pat.match(name)
            if m and int(m.group(1)) >= start:
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass
    return out


def _adopt_legacy_auto_project(proot: str) -> str:
    """旧版「按参数指纹自动命名」的自动项目目录：`h3chain_<w>x<h>_..._<指纹>`。

    自动名改固定 `h3chain` 后，若固定名目录还不存在，就沿用**最近更新过**的那个
    旧版自动目录（必须是已有 manifest.json 的目录）——否则老用户升级后自动项目
    会凭空多出一个空壳，看着像进度丢了。目录名保持原样，不搬家。
    没有可用旧目录时返回空串（调用方回落到固定名）。

    「最近更新」的判据按优先级：`manifest.updated_at`（每次运行都会刷，语义最准）
    → 目录 mtime（**只是近似**：目录 mtime 只在增删文件时变，改 manifest 或改段
    文件都不变）→ 0。拿不到 manifest 内容的目录不因此被排除，只是排序靠后。
    """
    best, best_mt = "", -1.0
    try:
        names = os.listdir(proot)
    except OSError:
        return ""
    for n in names:
        if n == AUTO_PROJECT_NAME or not n.startswith(AUTO_PROJECT_NAME + "_"):
            continue
        d = os.path.join(proot, n)
        if not os.path.isdir(d) or not os.path.isfile(os.path.join(d, "manifest.json")):
            continue
        mt = 0.0
        try:
            with open(os.path.join(d, "manifest.json"), "r", encoding="utf-8") as f:
                _u = json.load(f).get("updated_at")
            if isinstance(_u, (int, float)) and not isinstance(_u, bool):
                mt = float(_u)
        except Exception:
            pass          # 读不动/没这个键 → 退回目录 mtime
        if mt <= 0.0:
            try:
                mt = os.path.getmtime(d)
            except OSError:
                continue
        if mt > best_mt:
            best, best_mt = d, mt
    return best


def ckpt_dir(params: dict, custom: str = "") -> str:
    """项目根目录：output/h3_projects/<自定义名> 或自动项目目录。

    自动项目名**不跟参数走**（2026-10-12）：旧版按
    `h3chain_<w>x<h>_<len>f_ctx<ctx>_<指纹>` 命名，于是「改个分辨率/时长就换了
    项目」——用户必须切项目才能改参数，这正是要根除的体验。现在空「存档目录」
    固定落 `h3chain`；同名目录不存在时**沿用最近更新过的旧版自动目录**
    （`h3chain_*`，老用户的自动项目不会凭空消失），都没有才新建 `h3chain`。

    参数仍写进 manifest.params（诊断/`_describe_canvas` 用），只是不再参与命名。

    四库规范：顺手建好 assets/finals/latent/texts（幂等，旧项目懒建）。
    """
    from folder_paths import get_output_directory

    proot = os.path.join(get_output_directory(), "h3_projects")
    if custom:
        root = os.path.join(proot, custom)
    else:
        root = os.path.join(proot, AUTO_PROJECT_NAME)
        if not os.path.isdir(root):
            legacy = _adopt_legacy_auto_project(proot)
            if legacy:
                root = legacy
    os.makedirs(root, exist_ok=True)
    ensure_project_dirs(root)
    return root


def _atomic_write(path: str, data: bytes):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        # 清理同目录残留 .part（24h 以上，避免崩溃堆积）
        try:
            d = os.path.dirname(path)
            now = __import__("time").time()
            for n in os.listdir(d):
                if n.startswith(".tmp_") and n.endswith(".part"):
                    p = os.path.join(d, n)
                    try:
                        if now - os.path.getmtime(p) > 86400:
                            os.remove(p)
                    except OSError:
                        pass
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _json_fallback(obj):
    """manifest 兜底：意外混入的 torch/numpy 0 维标量转 Python 数值再序列化，
    防一次类型疏漏炸掉整条链的存档写入；多元素张量仍报错——存档里不该有整块张量。"""
    item = getattr(obj, "item", None)
    if item is not None:
        try:
            return item()
        except (ValueError, RuntimeError, TypeError):
            pass
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def save_manifest(root: str, manifest: dict):
    _atomic_write(os.path.join(root, "manifest.json"),
                  json.dumps(manifest, ensure_ascii=False, indent=1,
                             default=_json_fallback).encode("utf-8"))


def load_manifest(root: str):
    path = os.path.join(root, "manifest.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolution_changed(old: dict, new: dict) -> bool:
    """存档参数与当前参数的**分辨率**是否不同（旧档缺宽高视为未变）。"""
    old, new = old or {}, new or {}
    if old.get("width") is None or old.get("height") is None:
        return False
    try:
        return (int(old.get("width")), int(old.get("height"))) != (
            int(new.get("width")), int(new.get("height")))
    except (TypeError, ValueError):
        return False


def assert_match(old: dict, new: dict, hint: str = ""):
    """逐键比对存档参数与当前参数；**从不抛错**，只回报差异（调用方写进报告）。

    2026-10-12 起**分辨率也不再是硬约束**。链参数不再强制整链一致：中途改分辨率时
    旧段保留旧分辨率、此后新段用新分辨率。相邻段分辨率不同时，`nodes.py` 会跳过
    「自动引用上段尾帧」（latent 桥按行预留，C/H/W 不同拼不上）与接缝测量，成片阶段
    跳过自动合成——三处都只提示、不报错、不阻断生成。

    旧实现（2026-09-16 ~ 2026-10-11）对 width/height 不一致直接抛 ValueError，逼用户
    「改回存档分辨率或换新存档目录开新链」——用户反馈的最痛点就是这条。

    除分辨率外其余参数（steps/cfg/采样器/调度器/模型/fade_ratio/gate/实验开关）本就
    只影响**此后要采样的段**：已完成段是盘上的张量，与参数没有任何耦合。

    `hint`：可选补充说明（调用方拼好的"本次画布四个控件谁在生效"），只附在分辨率
    那一条上——用户改错控件（改宽高而实际由宽高比×百万像素换算）时只有两个数字
    是看不出来的。

    返回：变了但只影响此后新生成段的参数说明列表（分辨率那条恒排在最前）；无变化返回 []。
    """
    old, new = old or {}, new or {}
    notes = []
    if resolution_changed(old, new):
        _msg = (f"分辨率: 存档={old.get('width')}×{old.get('height')} "
                f"当前={new.get('width')}×{new.get('height')}")
        if hint:
            _msg += f"（{hint}）"
        notes.append(_msg)
    for k in new:
        if k in ("width", "height"):
            continue          # 已并入上面那一条（一起说才看得出是"画幅变了"）
        if old.get(k, new[k]) != new[k]:
            notes.append(f"{k}: 存档={old.get(k)!r} 当前={new[k]!r}")
    return notes


# ---- 逐段分辨率账（链参数不再强制整链一致，2026-10-12） ----

class ResolutionLedger:
    """谁在什么分辨率下生成的：逐槽位的分辨率台账（跨次运行续跑用）。

    **纯逻辑，不 import torch / folder_paths**，所以能在无 ComfyUI 环境下直接单测
    （nodes.py 的 `_archived_res` / `_slot_res` / `_res_list` 就是它的薄封装，
    逻辑只此一份，别在那边抄第二份判据）。

    为什么需要它：链参数不再强制整链一致后，一次运行里同时存在「存档时旧分辨率的段」
    与「本次当前画布新生成的段」，而 `manifest.params.width/height` 每次运行都被当前
    画布覆盖、只代表「上次运行用的画布」，指不了任何具体段。要判某处的分辨率边界
    （跳过尾帧桥 / 跳过接缝测量 / 成片闸门）必须**按槽位**问这里。

    真源优先级（高→低）：
      1. `record(g, wh)` 记下的本次实测值（本段已处理过，可能是重摇出来的新分辨率）；
      2. `archived_seg_res[g]`（`manifest.seg_res` 逐段记录）；
      3. `archived_wh`（旧档没有逐段记录时，整链按 manifest.params 的宽高算）；
      4. `current_wh`（当前画布）——只有「本次会新生成」的槽才落在这里。

    ⚠ 判定「本次是否回放」的三个条件与主循环的 `replay` 完全同源：槽位已存档
    （`g < done`）、没被标重摇、段文件在盘（`replayable(g)`）。三者任一不满足 →
    该槽本次会重新生成 → 用当前画布。
    """

    def __init__(self, current_wh, archived_wh=None, archived_seg_res=None,
                 done=0, redo_slots=(), replayable=None):
        self.current = (int(current_wh[0]), int(current_wh[1]))
        self.archived = (int(archived_wh[0]), int(archived_wh[1])) \
            if archived_wh else self.current
        self._seg_res = list(archived_seg_res or [])
        self.done = int(done or 0)
        self._redo = {int(x) for x in (redo_slots or [])}
        self._replayable = replayable or (lambda g: False)
        self._map = {}

    def archived_of(self, g):
        """槽 g **存档时**的分辨率（旧档无逐段记录 → 整链一个值）。"""
        if 0 <= g < len(self._seg_res):
            r = self._seg_res[g]
            if isinstance(r, (list, tuple)) and len(r) == 2:
                try:
                    return (int(r[0]), int(r[1]))
                except (TypeError, ValueError):
                    pass
        return self.archived

    def is_replay(self, g):
        """槽 g 本次是否走「存档回放」（已存档、未标重摇、段文件在盘）。"""
        return (g < self.done and g not in self._redo and bool(self._replayable(g)))

    def slot(self, g):
        """槽 g **本次实际**用的分辨率。

        已处理过的槽用实测值（`record`）——这一条很关键：同一次运行里前一段可能刚被
        重摇（或因缺文件重生成），此刻它的真实分辨率是**当前画布**，而存档记录还停在
        旧值；用旧值判「跨分辨率」会把本该保留的接缝锚误判掉（相邻两段一起重摇时最明显）。
        """
        if g in self._map:
            return self._map[g]
        if self.is_replay(g):
            return self.archived_of(g)
        return self.current

    def record(self, g, wh):
        """记下槽 g 本次实际用的分辨率（`slot` 从此以它为准）。"""
        self._map[g] = (int(wh[0]), int(wh[1]))

    def snapshot(self, upto):
        """`manifest.seg_res`：逐段 `[w, h]`（槽位对齐，跨次运行续跑的真源）。

        ⚠ 必须**逐槽问 `slot()`**，不许写成 `self._map.get(x, self.current)`：
        `_map` 只装本次循环**真正走过**的槽位，而 `upto`（= done）是存档里的整链完成数，
        两者不等（审片/重摇中段 break 时 done 仍是 10、循环只走到第 3 段）。用当前画布
        兜底会把没走到的槽位写成"新分辨率"——它们盘上其实是旧分辨率，下次运行据此判
        「相邻段同分辨率」→ 真去拼 latent 桥 → C/H/W 不一致崩掉，正是本机制要根除的失败。
        """
        return [list(self.slot(x)) for x in range(upto)]

    def distinct(self, skip=()):
        """本次实际用到的分辨率集合（成片闸门：>1 = 全链不一致，不自动合成）。

        只看 `_map`（本次真正生成/回放过的槽）——没走到的不参与判断，
        与 `nodes.py` 里「成片只覆盖已确认部分」的既有语义一致。
        """
        skip = {int(x) for x in (skip or [])}
        return {tuple(self._map[g]) for g in self._map if g not in skip}


def seg_path(root: str, idx: int) -> str:
    return os.path.join(root, f"seg_{idx:03d}.pt")


def contiguous_done(root: str, done: int) -> int:
    """manifest 记录的完成段数，遇到缺失的段文件向前截断（该段起重新采样）。"""
    while done > 0 and not os.path.exists(seg_path(root, done - 1)):
        done -= 1
    return done


def save_segment(root: str, idx: int, video_t, audio_t):
    """段 AV latent 原子落盘（CPU 副本，不动显存里的原张量）。"""
    import torch

    payload = {
        "video": video_t.detach().cpu().clone(),
        "audio": audio_t.detach().cpu().clone(),
    }
    buf = tempfile.SpooledTemporaryFile(max_size=64 << 20)
    torch.save(payload, buf)
    buf.seek(0)
    _atomic_write(seg_path(root, idx), buf.read())
    buf.close()


def load_segment(root: str, idx: int):
    """读回段 AV latent（CPU 张量，调用方按需 .to(device)）。"""
    import torch

    with open(seg_path(root, idx), "rb") as f:
        payload = torch.load(f, map_location="cpu", weights_only=True)
    return payload["video"], payload["audio"]


def upscale_files(idx: int) -> dict:
    """二采渲染产物文件名（与基础段同名合并存储，不再有 upseg_* 副本族）。

    四库规范：mp4=finals/seg_NNN.mp4（高清直接覆盖基础分段名——段视频就是
    二采结果）；thumb=finals/thumb_NNN.png；last=finals/uplast_NNN.png
    （尾帧锚，下一段高清接缝平滑用）。旧根目录裸名读走 resolve 双兼容。
    """
    return {
        "mp4": f"finals/seg_{idx:03d}.mp4",
        "thumb": f"finals/thumb_{idx:03d}.png",
        "last": f"finals/uplast_{idx:03d}.png",
    }


def base_seg_files(idx: int) -> dict:
    """一采（基础）分段**另存**产物文件名（二采基础设置「保存一采分段成片」开启时）。

    为什么需要单独一份：二采成功时 `finals/seg_NNN.mp4` 会被高清结果**同名覆盖**，
    一采那版分段就永久没了。开启该选项后，主循环把一采解码结果另存
    `finals/base_seg_NNN.mp4`（缩略图 `finals/base_thumb_NNN.png`），与高清版并存
    —— 段卡上「一采分段成片」/「二采分段成片」两个入口分别取两版。
    """
    return {
        "mp4": f"base_seg_{idx:03d}.mp4",
        "thumb": f"base_thumb_{idx:03d}.png",
    }


def remove_base_seg(root: str, idx: int) -> None:
    """删掉一采另存产物（base_seg_NNN.mp4 / base_thumb_NNN.png）。

    什么时候用：该段**重新采样**过（内容变了）又没重新另存 —— 旧件已是上一版
    内容，必须清掉。只清 manifest 记录不删文件是不够的：下次运行该段是回放
    （replay），另存走 `fresh=False`，会把盘上那份旧件当"现件"直接沿用，用户
    点开「一采分段成片」看到的是一段对不上的旧视频。

    ⚠ **不碰 `seg_NNN.mp4`**：那是段视频本体（二采关着时它就是当前的一采成片），
    删了等于把成片删掉。旧格式裸名与 finals/ 双路径都清。
    """
    for name in base_seg_files(idx).values():
        for cand in (os.path.join(root, "finals", name), os.path.join(root, name)):
            try:
                os.remove(cand)
            except OSError:
                pass


def upscale_legacy_files(idx: int) -> tuple:
    """旧版二采独立产物文件名（重渲染该段时清理，防新旧文件混淆）。"""
    return (f"upseg_{idx:03d}.pt", f"upseg_{idx:03d}.mp4", f"upthumb_{idx:03d}.png")


def projects_root() -> str:
    from folder_paths import get_output_directory

    return os.path.join(get_output_directory(), "h3_projects")


# ---- 四库文件夹规范（资产库/成片库/latent库/文本库，物理子文件夹） ----
PROJECT_SUBDIRS = ("assets", "finals", "latent", "texts")


def ensure_project_dirs(root: str) -> dict:
    """建好 assets/finals/latent/texts 四个子文件夹，返回 {名: 绝对路径}。

    幂等；旧项目缺目录时懒建，不搬旧文件（读走双路径兼容，写走新路径）。
    """
    out = {}
    for name in PROJECT_SUBDIRS:
        p = os.path.join(root, name)
        os.makedirs(p, exist_ok=True)
        out[name] = p
    return out


def assets_dir(root: str) -> str:
    p = os.path.join(root, "assets")
    os.makedirs(p, exist_ok=True)
    return p


def finals_dir(root: str) -> str:
    p = os.path.join(root, "finals")
    os.makedirs(p, exist_ok=True)
    return p


def texts_dir(root: str) -> str:
    p = os.path.join(root, "texts")
    os.makedirs(p, exist_ok=True)
    return p


def resolve_project_file(root: str, filename: str) -> str:
    """项目内媒体文件名 -> 实际存在的绝对路径（双路径兼容）。

    查找序：原样（含子目录前缀）> finals/<basename> > 根目录 > assets/<basename>。
    都不存在时返回新规范路径（finals/<basename>，调用方按需新建）。
    filename 允许 "seg_000.mp4"（旧）或 "finals/seg_000.mp4"（新）。
    """
    norm = str(filename or "").strip().replace("\\", "/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts:
        return os.path.join(finals_dir(root), "")
    if len(parts) >= 2:
        cand = os.path.join(root, *parts)
        if os.path.isfile(cand):
            return cand
        # 前缀目录不对时回退按 basename 找
        base = parts[-1]
    else:
        base = parts[0]
        cand = os.path.join(root, base)
        if os.path.isfile(cand):
            return cand
    for d in ("finals", "assets"):
        cand2 = os.path.join(root, d, base)
        if os.path.isfile(cand2):
            return cand2
    return os.path.join(finals_dir(root), base)


def save_state(state: dict):
    """链状态指针写到 h3_projects/h3chain_state.json（审片面板据此定位当前链）。

    固定路径 + /api/view 端点：面板无需自建 HTTP 路由，也不必复刻指纹算法。
    """
    root = projects_root()
    os.makedirs(root, exist_ok=True)
    _atomic_write(os.path.join(root, "h3chain_state.json"),
                  json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8"))


def load_state():
    path = os.path.join(projects_root(), "h3chain_state.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_thumb(seg_dir: str, idx: int, frame, name: str = None) -> str:
    """段可见首帧 -> finals/thumb_NNN.png（长边 256）。Pillow 缺失时返回空串（面板降级占位）。

    name=显式文件名（不带目录），默认 thumb_NNN.png；一采另存的分段缩略图走它
    （见 base_seg_files）。

    四库规范：新文件一律写 finals/，返回 "finals/thumb_NNN.png"；
    旧根目录文件读走 resolve_project_file 双路径兼容，不主动搬。
    """
    name = name or f"thumb_{idx:03d}.png"
    rel = f"finals/{name}"
    try:
        from PIL import Image

        arr = (frame.detach().float().clamp(0.0, 1.0).cpu().numpy() * 255.0).astype("uint8")
        img = Image.fromarray(arr)
        w, h = img.size
        scale = 256.0 / max(w, h)
        if scale < 1.0:
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        img.save(os.path.join(finals_dir(seg_dir), name))
        return rel
    except Exception:
        return ""


def save_segment_mp4(seg_dir: str, idx: int, frames, wav, sample_rate: int,
                     fps: int = 24, fresh: bool = True, crf: int = 20,
                     preset: str = "veryfast", aq_mode=None, dither=False,
                     name: str = None) -> str:
    """段可见帧 + 音轨 -> seg_NNN.mp4（编码实现在 media.save_av_mp4，与成片保存共用）。

    crf/preset/aq_mode/dither 为编码质量旋钮（默认值=现状兼容；二采 HQ 档
    由 upscale.render_segment 按编码档位解析后传入，基础链仍走默认）。

    name=显式文件名（不带目录），默认 seg_NNN.mp4；一采另存分段走 base_seg_NNN.mp4
    （见 base_seg_files）。

    缺失或编码失败返回空串（上游回退为缩略图，不影响主流程）。
    fresh=False（存档回放）且文件已存在时直接沿用，避免每次续跑全链重编码。

    四库规范：新文件写 finals/seg_NNN.mp4，返回 "finals/seg_NNN.mp4"；
    fresh=False 时 finals/ 与旧根目录双路径命中即沿用（返回实际命中的相对路径）。
    """
    name = name or f"seg_{idx:03d}.mp4"
    rel = f"finals/{name}"
    path = os.path.join(finals_dir(seg_dir), name)
    if not fresh:
        hit = resolve_project_file(seg_dir, name)
        if os.path.isfile(hit):
            return os.path.relpath(hit, seg_dir).replace("\\", "/")
    try:
        from .media import save_av_mp4    # 包内（ComfyUI 运行时）
    except ImportError:
        from media import save_av_mp4     # 顶层导入（无 ComfyUI 的单测环境）
    return name if save_av_mp4(path, frames, wav, sample_rate, fps,
                               crf=crf, preset=preset, aq_mode=aq_mode,
                               dither=dither) else ""
