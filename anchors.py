"""手动锚定（Anchor Studio）数据结构：归一化 / 校验 / 旧字段迁移 / 变更区间。

纯函数，零 torch、零 IO、零 ComfyUI 依赖，可在无运行环境时单测。
语义与执行路径见 docs/手动锚定_分段latent参考规范化_实施规划.md §3、§4：

    seg.anchors[] -> resolve_anchor_source()（步骤 4，本模块）-> guides.prepare_anchor()

anchor 结构（§3）：

    {"id": "a1",
     "src": {"kind": "prev_tail|segment|library|video|image",
             "ref": "seg_003|latent/head.pt|素材标签",
             "start_f": 10, "end_f": 32,   # 源内像素帧窗（左闭右开），先裁后编
             "src_fps": 24.0,              # 源真实帧率；None = 未知
             "meta_ok": True},             # 元信息完备度（决定 UI 降级层级）
     "at": {"mode": "head|mid|tail", "frame_idx": 0},
     "window": 22,                         # 已吸附 17k+5（1 = 单帧锚）
     "branches": {"av": "both|video|audio"},
     "on": True}

唯一硬约束是分辨率（C/H/W）；窗宽受 17k+5 约定约束，落点完全自由。
"""

from . import grid
from . import guides

SRC_KINDS = ("prev_tail", "segment", "library", "video", "image")
BRANCHES = ("both", "video", "audio")


# ---- 归一化 ----

def normalize_anchor(a, idx=0):
    """单条 anchor 归一化：补默认值、收敛类型。

    归一化**幂等**——已归一化的 anchor 再过一次结果逐键相同。迁移往返一致性
    依赖这条性质（旧档 -> anchors[] -> 归一化 应稳定）。

    归一化只做形状（补键/转类型），**不做取值合法化**：非法窗宽由
    `validate_anchors` 抛错，不在这里静默吸附。若在归一化时吸附，用户设 18 帧
    就会被悄悄改成 5 帧（video_latent_t 会把 18 折成 2 token = 5 帧），
    等于把手动锚最该避免的静默降级又请回来。

    window 必填：它是唯一「给错默认值就静默改变生成结果」的字段，
    猜一个默认等于替用户决定钉多少帧，故缺省直接报错而不兜底。
    """
    if not isinstance(a, dict):
        raise ValueError(f"anchor 必须是字典，得到 {type(a).__name__}")
    if a.get("window") is None:
        raise ValueError(f"anchor {a.get('id') or idx + 1} 缺少 window（钉住窗宽，必填）")
    win = int(a["window"])
    if win < 1:
        raise ValueError(f"anchor {a.get('id') or idx + 1} 窗宽 {win} 非法（须 ≥ 1 帧）")

    src = a.get("src") if isinstance(a.get("src"), dict) else {}
    at = a.get("at") if isinstance(a.get("at"), dict) else {}
    br = a.get("branches") if isinstance(a.get("branches"), dict) else {}

    def _opt_int(v):
        return None if v is None else max(0, int(v))

    return {
        "id": str(a.get("id") or f"a{int(idx) + 1}"),
        "src": {
            "kind": str(src.get("kind") or "prev_tail"),
            "ref": str(src.get("ref") or ""),
            "start_f": _opt_int(src.get("start_f")),
            "end_f": _opt_int(src.get("end_f")),
            "src_fps": None if src.get("src_fps") is None else float(src["src_fps"]),
            "meta_ok": src.get("meta_ok", True) is not False,
        },
        "at": {
            "mode": str(at.get("mode") or "head"),
            "frame_idx": int(at.get("frame_idx") or 0),
        },
        "window": win,
        "branches": {"av": str(br.get("av") or "both")},
        "on": a.get("on", True) is not False,
    }


def normalize_anchors(raw):
    """anchor 列表归一化（None / 空 = 无手动锚）。"""
    return [normalize_anchor(a, i) for i, a in enumerate(raw or [])]


# ---- 校验（手动锚硬拦，不软降级） ----

def validate_anchors(anchors, frame_count, label=""):
    """手动锚硬校验：任一非法直接抛 ValueError。

    与 `guides.audit_keyframes`（只报不拦）的分工：手动锚是用户显式意图，
    静默丢弃等于「设了锚不生效还不说」，是最坏的一类 bug（规划 §1.3 / §8）；
    历史自动锚继续走 audit_keyframes 的只报不拦，保证旧链逐帧一致。
    """
    name = f"{label}锚" if label else "锚点"
    for i, a in enumerate(anchors):
        tag = f"{name}{i + 1}（{a.get('id')}）"
        src, at = a["src"], a["at"]
        if src["kind"] not in SRC_KINDS:
            raise ValueError(f"{tag} 源类型 {src['kind']!r} 非法（合法值：{'/'.join(SRC_KINDS)}）")
        if at["mode"] not in grid.AT_MODES:
            raise ValueError(f"{tag} 落点 {at['mode']!r} 非法（合法值：{'/'.join(grid.AT_MODES)}）")
        if a["branches"]["av"] not in BRANCHES:
            raise ValueError(f"{tag} 取用分支 {a['branches']['av']!r} 非法"
                             f"（合法值：{'/'.join(BRANCHES)}）")
        snapped = grid.snap_window_down(a["window"])
        if a["window"] != snapped:
            raise ValueError(
                f"{tag} 窗宽 {a['window']} 不在 17k+5 档位——直接钉这个宽度会被模型层"
                f"折成 {snapped} 帧（静默少钉），请显式取 {snapped} "
                f"或 {grid.snap_window_down(a['window'] + 17)}"
                f"（合法档位：{'/'.join(str(w) for w in grid.SNAP_WINDOWS)}）")
        if src["kind"] != "prev_tail" and not src["ref"]:
            raise ValueError(f"{tag} 源类型 {src['kind']!r} 必须给 ref（源标识）")
        if a["on"] is False:
            continue
        if src["start_f"] is not None and src["end_f"] is not None \
                and src["end_f"] <= src["start_f"]:
            raise ValueError(f"{tag} 源帧窗非法：start_f={src['start_f']} "
                             f"end_f={src['end_f']}（左闭右开，须 end_f > start_f）")
        idx = grid.anchor_frame_index(at["mode"], a["window"], frame_count, at["frame_idx"])
        ok, why = guides.validate_anchor(idx, a["window"], frame_count)
        if not ok:
            raise ValueError(f"{tag} 越界：{why}")


# ---- 源归一化 ----

# 无源可重编时的转档指引（kind -> 该去哪转档）。报错要给出路，不能只说"不行"。
_REENCODE_HINT = {
    "video": "把该视频放进项目 assets/ 后，用面板的「转码为 latent」抽成 latent 文件，"
             "再改选 latent 库作源",
    "image": "把该图片放进项目 assets/ 并打上素材标签，或先转成 latent 文件",
    "segment": "该段存档 latent 缺失或被删；重新生成该段，或改选别的源",
    "library": "latent 文件缺失或不是本链分辨率；用 H3LatentUpscale 放大到本链尺寸，"
               "或改用原始视频素材作源（会走先裁后编）",
    "prev_tail": "上段尚未生成；先跑完上段，或改用别的源",
}


def resolve_anchor_source(anchor, chain_chw, source_meta):
    """决定这条 anchor 的源怎么取：**直接切窗 / 先裁后编 / 硬报错**。

    `chain_chw` = 本链的 (C, H, W)：唯一硬约束。`PackedLayout` 按行预留，
    不匹配则桥根本拼不上——所以这里要么把源改成链的尺寸，要么报错，没有第三条路。

    `source_meta` 由调用方探测后传入（形状/帧数/帧率/能否重编）：
        {"shape": (C, T, H, W) | None,   # None = 源不可用
         "frames": int | None,            # 源内可用像素帧数
         "fps": float | None,
         "reencodable": bool}             # 源是视频/图片这类可重新编码的素材

    返回取用计划：
        {"action": "reuse" | "encode", "window": int,
         "start_f": int, "end_f": int, "note": str}
      - `reuse`  ：形状已匹配，直接切窗（零编码开销）
      - `encode` ：形状不匹配但源可重编 → 帧窗解码 → center-cover → VAE 编码（先裁后编）

    无法解决时抛 ValueError 硬报错（手动锚是用户显式意图，静默降级等于
    「设了锚不生效还不说」，是最坏的一类 bug）。
    """
    label = f"锚点 {anchor['id']}"
    kind, ref = anchor["src"]["kind"], anchor["src"]["ref"]
    win = int(anchor["window"])

    shape = source_meta.get("shape")
    if shape is None:
        raise ValueError(f"{label} 源不可用（{kind}"
                         f"{' ' + ref if ref else ''}）：{_REENCODE_HINT.get(kind, '请改选别的源')}")

    src_chw = (int(shape[0]), int(shape[2]), int(shape[3]))
    want = tuple(int(x) for x in chain_chw)
    avail = source_meta.get("frames")
    start_f = 0 if anchor["src"]["start_f"] is None else int(anchor["src"]["start_f"])
    end_f = win if anchor["src"]["end_f"] is None else int(anchor["src"]["end_f"])
    if end_f <= start_f:
        raise ValueError(f"{label} 源帧窗非法：[{start_f},{end_f}) 左闭右开须 end_f > start_f")
    if avail is not None and end_f > int(avail):
        raise ValueError(f"{label} 越界：源只有 {int(avail)} 帧，取用窗 [{start_f},{end_f}) 超出")

    if src_chw == want:
        return {"action": "reuse", "window": win, "start_f": start_f, "end_f": end_f, "note": ""}

    if not source_meta.get("reencodable"):
        raise ValueError(
            f"{label} 分辨率不匹配：源 {src_chw[2]}×{src_chw[1]}，本链 {want[2]}×{want[1]}"
            f"——C/H/W 不同则 latent 桥拼不上。{_REENCODE_HINT.get(kind, '请改选别的源')}")

    return {"action": "encode", "window": win, "start_f": start_f, "end_f": end_f,
            "note": (f"{label}：源 {src_chw[2]}×{src_chw[1]} → 本链 {want[2]}×{want[1]}"
                     f"，先裁后编（帧窗 [{start_f},{end_f})）")}


# ---- 旧字段迁移 ----

def migrate_legacy_seg(seg, ctx_frames, last_frame=None):
    """旧段字段 -> 新结构：返回带 `anchors[]` 的段，旧键一次清干净。

    迁入项（规划 §7 删除清单）：
      `latent_ref` -> `anchors[]`（`at.mode="head"`）：`src.file` 置 `kind="library"`，
        否则 `kind="prev_tail"`；`frames>0` 覆盖全局 ctx；`audio=false` 收窄为
        `branches.av="video"`；`on=false` 或 `video=false` 都表示「本段不挂桥」（同效，
        见下）→ 保留为 `on=false`
      `tail_src`   -> `anchors[]`（`at.mode="tail"`，窗宽 1 = 单帧身份锚）：
        `{asset}` -> `kind="image"`，`{latent}` -> `kind="library"`
      `auto_ref` / 旧 `unlink` -> `unlink` 布尔（UI 快捷开关：不生成默认段首桥）
      `last_frame` 全局回退 -> 无 `tail_src` 的段各补一条 `kind="image"` 尾锚
        （现状即「tail_src 缺省时所有段都吃全局尾帧」，展开后行为不变）

    **默认配置不生成 anchor**：`latent_ref` 缺省 / `frames=0` / 双分支开 / 无 `src`
    表示「跟随全局 ctx 的默认段首桥」——那是隐式行为而非显式锚，与 `seg_hashes`
    的「显式非默认才进哈希」同口径。

    注意：段级/链级「尾帧图」优先级仍高于尾锚，迁移不改这条执行期优先级
    （见 nodes.py 尾锚解析链）——否则原本被尾帧图遮挡的 `tail_src` 会突然生效。

    窗宽在这里（且只在这里）吸附：旧档的 `frames` 是自由值，`video_latent_t`
    早就把它折到 17k+5 了，迁移时显式写回吸附后的值，空出的信息反而被记录清楚。
    """
    out = dict(seg)
    # 已是新格式的锚原样保留：迁移是一次性的，但重复调用必须幂等（段被读第二次时
    # 旧字段已清空，此时唯一正确的行为是把现有 anchors 原样带回去，而不是丢掉）。
    anchors = list(seg.get("anchors") or [])

    lr = seg.get("latent_ref") if isinstance(seg.get("latent_ref"), dict) else None
    if lr is not None:
        src = lr.get("src") if isinstance(lr.get("src"), dict) else {}
        file = str(src.get("file") or "").strip().replace("\\", "/")
        frames = int(lr.get("frames") or 0)
        video = lr.get("video", True) is not False
        audio = lr.get("audio", True) is not False
        window = grid.snap_window_down(frames if frames > 0 else ctx_frames)
        # video=false 与 on=false 同效：_inject_guide 的门控是 `not want_v -> None`，
        # 视频分支是主干、音频只是附加，故「不带视频」= 整条桥都不挂。
        if lr.get("on") is False or not video:
            anchors.append({"src": {"kind": "prev_tail"}, "at": {"mode": "head"},
                            "window": window, "branches": {"av": "both"}, "on": False})
        elif file or frames > 0 or not audio:
            anchors.append({
                "src": {"kind": "library", "ref": file} if file else {"kind": "prev_tail"},
                "at": {"mode": "head"},
                "window": window,
                "branches": {"av": "both" if audio else "video"},
                "on": True,
            })

    ts = seg.get("tail_src") if isinstance(seg.get("tail_src"), dict) else None
    tail_ref = None
    if ts is not None:
        if ts.get("asset"):
            tail_ref = ("image", str(ts["asset"]).strip())
        elif ts.get("latent"):
            tail_ref = ("library", str(ts["latent"]).strip().replace("\\", "/"))
    if tail_ref is None and last_frame:
        tail_ref = ("image", str(last_frame).strip())
    if tail_ref is not None:
        anchors.append({"src": {"kind": tail_ref[0], "ref": tail_ref[1]},
                        "at": {"mode": "tail"}, "window": 1,
                        "branches": {"av": "video"},   # 尾锚只钉图像身份，无音频分支
                        "on": True})

    auto_ref = seg.get("auto_ref")
    if auto_ref is None and seg.get("unlink") is not None:
        auto_ref = not bool(seg.get("unlink"))
    for legacy in ("latent_ref", "tail_src", "auto_ref", "unlink"):
        out.pop(legacy, None)
    out["unlink"] = auto_ref is not None and not bool(auto_ref)
    if anchors:
        out["anchors"] = normalize_anchors(anchors)
    else:
        out.pop("anchors", None)
    return out


# ---- 变更区间 ----

def change_intervals(old_hashes, new_hashes, done):
    """已完成段哈希 vs 当前哈希 -> 需重建的变更区间列表（左闭右闭）。

    重做最小单位 = 单段、双锚对齐邻居（规划 §4.1），所以这里只回答
    「哪些段的内容变了」，不回答「从哪段开始级联」——后者已不存在。

    连续变更合并为一个区间（区间内串联重建），离散变更各自成区间
    （各自双锚，互不影响）。末尾追加新段不算变更（那是未完成的段，交给续跑），
    删除末尾段也不算区间（交给调用方 truncate）。无变更返回 []。
    """
    n = min(int(done), len(new_hashes))
    out = []
    for i in range(n):
        if old_hashes[i] == new_hashes[i]:
            continue
        if out and out[-1][1] == i - 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out
