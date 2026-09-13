"""H3 资产总闸（AssetHub）：单节点素材分发与早爆校验。

M2 新增，与 H3SeamlessChainSampler 配套：
- 输入：资产包 JSON（导演台资产库 [{label,kind,file}]，总量不限）
- 输出： validated 规范包 JSON + 人读报告
- 职责：标签归一/去重、防穿越、input 目录存在性预检、单段上限不在此卡
  （执行期按段卡），缺文件/重标签在排队前就报错并点名，不等主节点跑一半才炸。

尽量复用 ComfyUI 原生机制：文件仍走 input 目录 + get_annotated_filepath，
与 LoadImage/LoadVideo/LoadAudio 同源；Hub 只做分发校验，不自建解码器。
无 torch 依赖，无 ComfyUI 也可单测纯函数。
"""

import json
import os

_KINDS = ("image", "video", "audio")
_KIND_CN = {"image": "图片", "video": "视频", "audio": "音频"}
_REF_CAPS = {"image": 9, "video": 3, "audio": 3}
_LABEL_MAX = 24


def normalize_pack(raw) -> tuple:
    """资产包 -> (规范列表, warnings)。未知键剔除，label 去重（首个为准）。

    资产库总量不限：只做形态校验与去重，不截断；单段 9/3/3 上限由
    check_segment_refs（执行期按段）与主节点组装期按段卡。
    """
    items = []
    warns = []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return [], [{"code": "E_PACK_JSON", "message": "资产包不是合法 JSON"}]
    if not isinstance(raw, list):
        return [], [{"code": "E_PACK_TYPE", "message": "资产包须为数组 [{label,kind,file}]"}]
    seen = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "image").strip()
        if kind not in _KINDS:
            warns.append({"code": "W_PACK_KIND", "message": f"未知类别已按图片处理：{kind!r}"})
            kind = "image"
        label = str(entry.get("label") or "").strip()[:_LABEL_MAX]
        file = str(entry.get("file") or "").strip().replace("\\", "/")
        if not label or not file:
            continue
        parts = [p for p in file.split("/") if p and p != "."]
        if not parts or len(parts) > 2 or ".." in parts or any(p.startswith(".") or "/" in p or ":" in p for p in parts):
            warns.append({"code": "W_PACK_PATH", "message": f"非法路径已跳过：{file!r}"})
            continue
        if not os.path.splitext(parts[-1])[1]:
            warns.append({"code": "W_PACK_EXT", "message": f"缺扩展名已跳过：{file!r}"})
            continue
        if label in seen:
            warns.append({"code": "W_PACK_DUP", "message": f"重复标签只保留首个：{label!r}"})
            continue
        seen.add(label)
        ent = {"label": label, "kind": kind, "file": "/".join(parts)}
        if isinstance(entry.get("roles"), list):
            kept = [str(r).strip() for r in entry["roles"] if str(r).strip() in ("首帧图", "尾帧图")]
            if kept:
                ent["roles"] = kept[:2]
        items.append(ent)
    return items, warns


def check_files(items: list, project_root=None) -> list:
    """存在性预检 -> errors（点名标签+文件名）。无 ComfyUI 时跳过文件检查只查形态。

    project_root：项目文件夹绝对路径（给了就认 assets/finals/latent/texts/ 前缀，
    去项目内找；不给或非项目文件走 input 目录）。路径含空格括号均合法。
    """
    errors = []
    try:
        import folder_paths
        in_dir = folder_paths.get_input_directory()
    except Exception:
        return errors
    for a in items:
        rel = str(a["file"] or "").strip().replace("\\", "/")
        parts = [p for p in rel.split("/") if p and p != "."]
        where = "input 目录"
        p = None
        if project_root and len(parts) == 2 and parts[0] in (
                "assets", "finals", "latent", "texts"):
            p = os.path.join(project_root, parts[0], parts[1])
            where = "项目文件夹"
        elif len(parts) == 2 and parts[0] in ("images", "videos", "audios"):
            # P3：全局库文件（library_upload 入库），按内容寻址到 user 库
            try:
                try:
                    from . import asset_store as _as
                except ImportError:
                    import asset_store as _as
                _libroot = _as.try_library_root()
                cand = os.path.join(_libroot, parts[0], parts[1]) if _libroot else None
                if cand and os.path.isfile(cand):
                    continue
                where = "全局库"
            except Exception:
                where = "全局库"
            p = None
        else:
            try:
                p = folder_paths.get_annotated_filepath(rel)
            except Exception:
                p = os.path.join(in_dir, rel)
        if not p or not os.path.isfile(p):
            errors.append({"code": "E_FILE_MISSING", "label": a["label"],
                           "message": f"素材「{a['label']}」文件缺失：{a['file']}（请确认仍在{where}）"})
    return errors


def check_segment_refs(items: list, refs: list, seg_no: int = 1) -> list:
    """单段引用校验 -> errors（总量不限，单段 9/3/3）。refs 为标签列表。"""
    by_label = {a["label"]: a for a in items}
    errors = []
    order = []
    # 同一标签重复引用只算一个素材（编号/上限按素材个数算，重复=正文里多写一次）
    for lbl in dict.fromkeys(str(r).strip() for r in refs or []):
        if lbl not in by_label:
            errors.append({"code": "E_REF_UNKNOWN",
                           "message": f"段{seg_no} 引用未知标签「{lbl}」"})
            continue
        order.append((by_label[lbl]["kind"], lbl))
    counts = {}
    for k, _ in order:
        counts[k] = counts.get(k, 0) + 1
    for k, cap in _REF_CAPS.items():
        if counts.get(k, 0) > cap:
            picked = [lbl for kk, lbl in order if kk == k]
            errors.append({"code": "E_MEDIA_LIMIT",
                           "message": f"段{seg_no} 引用{_KIND_CN[k]}素材 {len(picked)} 个，"
                                      f"超过官方单段上限 {cap} 个（{picked}）"})
    return errors


def validate_pack(raw, segments=None, project_root=None) -> dict:
    """资产包 + 可选分段引用 -> {ok, items, errors, warnings}。"""
    items, warns = normalize_pack(raw)
    errors = check_files(items, project_root)
    if isinstance(segments, list):
        for i, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            refs = seg.get("refs")
            if isinstance(refs, list) and refs:
                errors.extend(check_segment_refs(items, refs, i + 1))
    counts = {k: sum(1 for a in items if a["kind"] == k) for k in _KINDS}
    report = (f"资产总闸：共 {len(items)}（图{counts['image']}/视{counts['video']}/"
              f"音{counts['audio']}），总量不限、单段 9/3/3（按段按需加载）。")
    if errors:
        report += f"错误 {len(errors)}（首错：{errors[0]['message']}）"
    elif warns:
        report += f"警告 {len(warns)}（首条：{warns[0]['message']}）"
    else:
        report += "校验通过。"
    return {"ok": not errors, "items": items, "errors": errors,
            "warnings": warns, "report": report}


# P4d：H3AssetHub 节点类已删除（被 H3AssetBundle 取代；纯函数保留，
# routes / bundle / 单测仍在用 normalize_pack / check_files / validate_pack）。

