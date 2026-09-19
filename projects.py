"""游戏式项目存档：output/h3_projects/ 下一个项目一个文件夹。

list_projects 扫描磁盘实时列项目（不依赖索引文件或前端 localStorage，
永不失真）；delete_project 删项目 = 删整个文件夹（视频/提示词/latent 全删）；
merge_project 按序合并若干段/成片/外部视频 -> merged_*.mp4（只追加产物，
不动链、不动段存档）。
本模块不导入 routes（routes 单向导入本模块，避免循环依赖）。
"""

import json
import os
import re
import shutil
import time

from . import checkpoint


MANIFEST_SCHEMA = "h3seamless/manifest-v2.2"


def _ensure_revision(manifest: dict) -> dict:
    """老 manifest 懒迁移：缺 revision/schema_version 则补 1，不重写 ID。

    v2.2（四库规范）：manifest_schema 统一前滚到 v2.2；旧裸文件名
    （seg_000.mp4 / thumb_000.png）不改写，读写走双路径兼容。
    """
    if not isinstance(manifest, dict):
        return manifest
    if not isinstance(manifest.get("revision"), int):
        manifest["revision"] = 1
    if manifest.get("manifest_schema") != MANIFEST_SCHEMA:
        manifest["manifest_schema"] = MANIFEST_SCHEMA
    return manifest


def safe_name(name) -> str:
    """项目目录名安全校验：拒空、路径分隔、盘符、与点开头（防目录穿越）。"""
    s = str(name or "").strip()
    if not s or s.startswith(".") or "/" in s or "\\" in s or ":" in s or ".." in s:
        return ""
    return s


def _cover(root: str, manifest: dict) -> str:
    """第一张实际存在的段缩略图文件名（列表里可能有空串占位）。

    四库兼容：裸名与 finals/ 前缀双路径查找，返回 manifest 原值（前端按原值取图）。
    """
    for name in (manifest.get("thumbs") or []):
        if not name:
            continue
        norm = str(name).strip().replace("\\", "/")
        if os.path.isfile(os.path.join(root, norm)):
            return name
        base = norm.split("/")[-1]
        if os.path.isfile(os.path.join(root, "finals", base)):
            return name
        if os.path.isfile(os.path.join(root, base)):
            return name
    return ""


def list_projects() -> list:
    """扫描项目根目录 -> 摘要列表（按 updated_at 倒序）。

    无 manifest.json 或 manifest 损坏的目录直接跳过（不是本项目系统的产物）。
    """
    root = checkpoint.projects_root()
    if not os.path.isdir(root):
        return []
    out = []
    for name in os.listdir(root):
        pdir = os.path.join(root, name)
        if not os.path.isdir(pdir) or not safe_name(name):
            continue
        try:
            with open(os.path.join(pdir, "manifest.json"), "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            continue
        if not isinstance(manifest, dict):
            continue
        updated = manifest.get("updated_at")
        if not isinstance(updated, (int, float)):
            try:
                updated = os.path.getmtime(os.path.join(pdir, "manifest.json"))
            except OSError:
                updated = 0
        out.append({
            "dir": name,
            "title": manifest.get("title") or name,
            "done": int(manifest.get("done") or 0),
            "total": int(manifest.get("total") or 0),
            "updated_at": updated,
            "cover": _cover(pdir, manifest),
            "finals": list(manifest.get("finals") or []),
            "merges": [m.get("file") for m in (manifest.get("merges") or [])
                       if isinstance(m, dict) and m.get("file")],
            "params": manifest.get("params") or {},
            "revision": int(manifest.get("revision") or 1),
            "manifest_schema": manifest.get("manifest_schema") or MANIFEST_SCHEMA,
            "seg_count": len(manifest.get("prompts") or []),
        })
    out.sort(key=lambda p: p["updated_at"] or 0, reverse=True)
    return out


def list_projects_summary(page: int = 1, size: int = 50) -> dict:
    """轻量摘要分页：M1 新增，前端列表只拿摘要，详情走 read_project。"""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        size = min(200, max(1, int(size)))
    except (TypeError, ValueError):
        size = 50
    all_items = list_projects()
    total = len(all_items)
    start = (page - 1) * size
    items = []
    for p in all_items[start:start + size]:
        items.append({
            "dir": p["dir"], "title": p["title"],
            "revision": p["revision"], "seg_count": p["seg_count"],
            "done": p["done"], "total": p["total"],
            "updated_at": p["updated_at"], "cover": p["cover"],
        })
    return {"items": items, "total": total, "page": page, "size": size}


def read_project(name: str):
    """读单个项目 manifest 全文；不存在/非法返回 None。"""
    if not safe_name(name):
        return None
    root = os.path.join(checkpoint.projects_root(), safe_name(name))
    manifest = checkpoint.load_manifest(root)
    if not isinstance(manifest, dict):
        return None
    return _ensure_revision(manifest)


_ASSET_KINDS = ("image", "video", "audio")
_ASSET_LABEL_MAX = 24


def _clean_asset(raw) -> dict | None:
    """资产条目白名单清洗：{label,kind,file}；总量不限，单段上限执行期按段卡。"""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "image").strip()
    if kind not in _ASSET_KINDS:
        kind = "image"
    label = str(raw.get("label") or "").strip()[:_ASSET_LABEL_MAX]
    file = str(raw.get("file") or "").strip().replace("\\", "/")
    if not file or not label:
        return None
    # 文件名防穿越：允许至多两级子目录，每段过 safe_name
    parts = [p for p in file.split("/") if p and p != "."]
    if not parts or len(parts) > 2 or not all(safe_name(p) for p in parts):
        return None
    if not os.path.splitext(parts[-1])[1]:
        return None
    ent = {"label": label, "kind": kind, "file": "/".join(parts)}
    # 引用名（含格式后缀的全名）：提示词里 `@` 后面写的就是这个，不用截过尾的 label
    _rn = _ref_name_of(raw.get("ref_name"), parts[-1], label)
    if _rn:
        ent["ref_name"] = _rn
    # 标注（图片1 / 视频1 / 音频1）：给 LLM 看的短名，落盘才有稳定性
    _mk = str(raw.get("mark") or "").strip()
    if _mk:
        ent["mark"] = _mk[:16]
    # 资产标注 roles 白名单透存（首帧图/尾帧图；单张冲突校验在执行期做）
    rl = raw.get("roles")
    if isinstance(rl, list):
        kept = [str(r).strip() for r in rl if str(r).strip() in ("首帧图", "尾帧图")]
        if kept:
            ent["roles"] = kept[:2]
    # 标注透存（发给 LLM 的稳定短编号：图片1 / 视频2 / 音频1，见 asset_store.assign_marks）
    mk = _mark_clean(raw)
    if mk:
        ent["mark"] = mk
        if raw.get("mark_auto") is False:
            ent["mark_auto"] = False
    return ent


def _dedupe_assets(items: list) -> list:
    """按 label 去重（首个为准），保持入库顺序。总量不限。"""
    seen, out = set(), []
    for a in items:
        c = _clean_asset(a)
        if c is None or c["label"] in seen:
            continue
        seen.add(c["label"])
        out.append(c)
    return out


def _unique_label(base, taken, cap=24):
    """taken 之外找一个不冲突标签，**保证能停**（详见 library.unique_label）。

    历史坑：写成 `while lbl in taken: lbl = f"{base}{n}"[:cap]`，当 base 已满 cap
    个字符时截断后恒等于 base → 死循环 → aiohttp 事件循环被占死 → 整个 ComfyUI
    卡住（导演台、队列、所有接口全挂）。标签上限 24，中文名很容易凑满。
    """
    b = str(base or "").strip()[:cap] or "素材"
    taken = {str(t) for t in (taken or ()) if t}
    if b not in taken:
        return b
    stem = b[:max(1, int(cap) - 3)]
    n = 2
    while n < 10000:
        cand = f"{stem}{n}"[:cap]
        if cand not in taken:
            return cand
        n += 1
    import time as _t
    return (f"{stem}{int(_t.time() % 100000)}")[:cap]


def _alias_of(*cands) -> str:
    """别名归一（规则唯一在 asset_store.alias_of —— 去扩展名 / 空白标点压成 `_` / 截 24）。

    别名一律来自文件名，而文件名里的扩展名、空格、括号都进不了 `@别名` 语法
    （后端 `_REF_AT` 在空白处断句），所以**落库那一刻**就得归一。
    函数内延迟导入：本模块会被按顶层模块加载（见 tests/test_asset_store_p0.py 的
    `_load_top`，它只替换 `from . import checkpoint` 那一行）。
    """
    try:
        from . import asset_store
    except ImportError:
        import asset_store
    return asset_store.alias_of(*cands)


def _ref_name_of(*cands) -> str:
    """引用名归一（规则唯一在 asset_store.ref_name_of —— 保留扩展名、不砍尾巴）。

    与 `_alias_of` 的区别：引用名**含格式后缀**（`@猫.png`），别名是 stem。
    前端 `cleanRefName` 必须与它同规则，否则 `@全名` 会在一边解析不出来。
    """
    try:
        from . import asset_store
    except ImportError:
        import asset_store
    return asset_store.ref_name_of(*cands)


def _as_mod():
    """asset_store 延迟导入（本模块可能被按顶层模块加载，不能顶层 import）。"""
    try:
        from . import asset_store
    except ImportError:
        import asset_store
    return asset_store


def _mark_clean(raw) -> str:
    """标注归一（规则唯一在 asset_store.clean_mark）。非法/缺省返回 ""。

    与 `_mark_of(kind, seq)` 的分工：这个是**读已有值并清洗**（入参是条目 dict），
    那个是**按类型和序号生成新标注文本**。合并两条分支时它们曾同名 `_mark_of`，
    语义却完全不同 —— 传错类型会静默返回 ""（清洗版对非 dict 的处理），
    所以改名拆开，别再合回去。
    """
    if not isinstance(raw, dict):
        return ""
    return _as_mod().clean_mark(raw.get("mark"))


def _mark_of(kind, seq) -> str:
    """标注文本（图片1 / 视频1 / 音频1）；规则唯一在 asset_store.mark_of。"""
    try:
        from . import asset_store
    except ImportError:
        import asset_store
    return asset_store.mark_of(kind, seq)


def _assign_marks(entries) -> list:
    """补标注（图片1 / 视频1 / 音频1）；规则唯一在 asset_store.assign_marks。"""
    try:
        from . import asset_store
    except ImportError:
        import asset_store
    return asset_store.assign_marks(entries)


def _next_mark_seq(kind, used) -> int:
    """下一个标注序号（编号不回收）；规则唯一在 asset_store.next_mark_seq。"""
    try:
        from . import asset_store
    except ImportError:
        import asset_store
    return asset_store.next_mark_seq(kind, used)

def _unique_filename(directory, want, sep="_"):
    """directory 里找一个不冲突文件名（同名加 _2），**保证能停**。"""
    d, w = str(directory or ""), str(want or "")
    stem, ext = os.path.splitext(w)
    cand, k = w, 2
    guard = 0
    while os.path.exists(os.path.join(d, cand)) and guard < 10000:
        cand = f"{stem}{sep}{k}{ext}"
        k += 1
        guard += 1
    return cand


def save_assets(name: str, assets, base_revision=None):
    """资产库持久化：manifest["assets"] 全量覆盖写，revision+1，乐观锁可选。

    总量不限；单段 9/3/3 不在这里卡（执行期按段卡，主节点按段按需加载）。
    目录或 manifest 不存在返回 None；base_revision 不一致抛 ValueError(REVISION_CONFLICT)。
    """
    name = safe_name(name)
    if not name or not isinstance(assets, list):
        return None
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        return None
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    manifest["assets"] = _assign_marks(_dedupe_assets(assets))
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest


def _clean_asset_link(raw) -> dict | None:
    """项目资产链接白名单清洗：{asset_id, alias, kind?, roles?, mark?}；非法丢弃。"""
    import re as _re
    if not isinstance(raw, dict):
        return None
    aid = str(raw.get("asset_id") or "").strip()
    if not _re.fullmatch(r"a_[0-9a-f]{12}", aid):
        return None
    alias = str(raw.get("alias") or "").strip()[:_ASSET_LABEL_MAX]
    if not alias:
        return None
    kind = str(raw.get("kind") or "image").strip()
    if kind not in _ASSET_KINDS:
        kind = "image"
    ent = {"asset_id": aid, "alias": alias, "kind": kind}
    # 引用名（含后缀全名）透存：只认**真名**（显式字段 / 原始文件名 / 落盘文件名）。
    # 不用 alias 兜底 —— alias 是 stem（没后缀），拿它当引用名等于把旧问题固化；
    # 拿不到真名就先不落，等 next hydrate 时由 build_registry 兜底解析。
    _rn = _ref_name_of(raw.get("ref_name"), raw.get("orig_name"),
                       str(raw.get("file") or "").replace("\\", "/").split("/")[-1])
    if _rn:
        ent["ref_name"] = _rn
    # 标注（图片1 / 视频1 / 音频1，按类型独立编号）：给 LLM 看的短名。
    # 可手动改（空串 = 交给自动分配重新编一个）。
    _mk = str(raw.get("mark") or "").strip()
    if _mk:
        ent["mark"] = _mk[:16]
    # P3：标注透存（首帧图/尾帧图，与 _clean_asset 同口径；执行期 roles 仍以 ds 池为准）
    rl = raw.get("roles")
    if isinstance(rl, list):
        kept = [str(r).strip() for r in rl if str(r).strip() in ("首帧图", "尾帧图")]
        if kept:
            ent["roles"] = kept[:2]
    # 标注透存（发给 LLM 的稳定短编号，与旧 assets 同口径）
    mk = _mark_clean(raw)
    if mk:
        ent["mark"] = mk
        if raw.get("mark_auto") is False:
            ent["mark_auto"] = False
    return ent


def _mark_pool(manifest: dict) -> list:
    """按「池序」返回素材条目（同一批 dict 引用，就地改即落盘）。

    池序 = 前端 `poolFromManifest` 的顺序：旧 `assets` 先按数组序，
    同名（label==alias）时 **asset_links 胜出并顶替原位置**（Map.set 语义）。
    只有与前端同口径，标注号才两边一致 —— 否则界面显示"图片3"、后端存的是"图片4"。
    """
    out, idx = [], {}
    for a in (manifest.get("assets") or []):
        if not isinstance(a, dict) or not a.get("label"):
            continue
        idx[str(a["label"])] = len(out)
        out.append(a)
    for L in (manifest.get("asset_links") or []):
        if not isinstance(L, dict) or not L.get("alias"):
            continue
        k = str(L["alias"])
        if k in idx:
            out[idx[k]] = L
        else:
            idx[k] = len(out)
            out.append(L)
    return out


def normalize_marks(name: str, save: bool = True):
    """给项目里还没标注的素材按池序补发标注（幂等）。

    返回 `(manifest, changed)`；目录/manifest 不存在返回 `(None, False)`。
    老项目首次读到时补号并落盘，之后号就固定了（删素材留空号、不重排）。
    """
    name = safe_name(name)
    if not name:
        return None, False
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        return None, False
    _ensure_revision(manifest)
    # assign_marks 返回**新列表**而不是 bool（旧口径返回 bool 的那版已废弃），
    # 所以 changed 要自己比出来 —— 直接拿返回值当布尔用的话，非空列表恒为真，
    # 于是每次读项目都判定"有改动"、白白 +1 revision 并重写 manifest。
    _pool = _mark_pool(manifest)
    changed = False
    for _old, _new in zip(_pool, _as_mod().assign_marks(_pool)):
        if _new.get("mark") and _new.get("mark") != _old.get("mark"):
            _old["mark"] = _new["mark"]
            changed = True
    if changed and save:
        manifest["updated_at"] = time.time()
        manifest["revision"] = int(manifest.get("revision") or 1) + 1
        manifest["manifest_schema"] = MANIFEST_SCHEMA
        checkpoint.save_manifest(root, manifest)
    return manifest, changed


def set_asset_mark(name: str, mark, asset_id=None, alias=None, base_revision=None):
    """手动改标注：查重后写入，并置 `mark_auto=False`（不再被自动发号覆盖）。

    mark 传空串 = 清除标注（下次 normalize_marks 会按池序重新发号）。
    找不到目标素材抛 ValueError；标注被别的素材占用抛 ValueError。
    """
    name = safe_name(name)
    if not name:
        return None
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        return None
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    AS = _as_mod()
    pool = _mark_pool(manifest)
    want = AS.clean_mark(mark)
    raw_mark = str(mark or "").strip()
    if raw_mark and not want:
        raise ValueError(f"标注须是「图片N / 视频N / 音频N」形态，收到「{raw_mark}」")
    target = None
    aid = str(asset_id or "").strip()
    lab = str(alias or "").strip()
    for d in pool:
        if aid and str(d.get("asset_id") or "") == aid:
            target = d
            break
        if lab and str(d.get("alias") or d.get("label") or "") == lab:
            target = d
            break
    if target is None:
        raise ValueError("素材不在项目库中")
    tgt_id = str(target.get("asset_id") or "") or None
    tgt_lab = str(target.get("alias") or target.get("label") or "") or None
    if want and AS.mark_taken(pool, want, exclude_id=tgt_id, exclude_label=tgt_lab):
        raise ValueError(f"标注「{want}」已被其它素材占用，请换一个")
    if want:
        target["mark"], target["mark_auto"] = want, False
    else:
        target.pop("mark", None)
        target.pop("mark_auto", None)
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest



def _purge_alias_refs(manifest: dict, aliases) -> bool:
    """从 segments[i].refs 与 prompts[i] 正文里清掉这些别名（含 `@别名`）-> 是否有改动。

    **解链（unlink_asset）与删素材（remove_assets）共用这一份**：两处各写一份必然
    漂移，历史 bug 就是解链清了正文、删素材没清 —— "删了项目库里的东西，提示词框的
    引用还在"（seg.refs 漏删 → 引用条残留 → 渲染的 @xxx 找不到素材而红框，文本却还在）。
    """
    targets = {str(t).strip() for t in (aliases or ()) if str(t).strip()}
    if not targets:
        return False
    changed = False
    for seg in (manifest.get("segments") or []):
        if not isinstance(seg, dict):
            continue
        cur = seg.get("refs") or []
        new = [r for r in cur if r not in targets]
        if new != cur:
            seg["refs"] = new
            changed = True
    prompts = manifest.get("prompts") or []
    for i in range(len(prompts)):
        if not isinstance(prompts[i], str):
            continue
        txt = prompts[i]
        for t in targets:
            # 单词边界：`@[名]` 后面不能跟 label 字符（_A-Za-z0-9），
            # 防止误删 @回廊 → @回廊场景2 这种更长名字的前缀。
            txt = re.sub(rf"@{re.escape(t)}(?![A-Za-z0-9_])", "", txt)
        txt = re.sub(r"[ \t]+", " ", txt)        # 多余空格压一个
        txt = re.sub(r"\n{3,}", "\n\n", txt)     # 多余空行压一行
        if txt != prompts[i]:
            prompts[i] = txt
            changed = True
    manifest["prompts"] = prompts
    return changed


def link_asset(name: str, asset_id: str, alias: str, kind="image", base_revision=None,
               roles=None, ref_name=None, orig_name=None, mark=None):
    """全局库链接进项目：manifest["asset_links"] 增量追加/重指向，revision+1。

    P1（双层存储）：全局 asset_id 一次入库，多项目链接引用不复制文件；
    alias 是项目内显示别名（旧 label 命名空间，compile_refs 同口径解析）。
    ref_name：引用名（**含格式后缀**，如 `女主.png`）——提示词里 `@` 后面写的
    就是它；缺省时由 orig_name / 落盘文件名 / alias 兜底推导。
    同 alias 已存在则重指向新 asset_id；同 asset_id 已存在则只改 alias。
    roles：None=不动旧标注；列表（含空）=覆盖（含取消标注）。
    mark：None=按池序自动发号（既有号不动）；非空=手动覆盖并锁定（mark_auto=False）。
    目录或 manifest 不存在返回 None；base_revision 不一致抛
    ValueError(REVISION_CONFLICT)。旧 manifest["assets"] 不动（转译层照读）。
    """
    name = safe_name(name)
    ent = _clean_asset_link({"asset_id": asset_id, "alias": alias, "kind": kind,
                             "roles": roles, "ref_name": ref_name,
                             "orig_name": orig_name, "mark": mark})
    if not name or ent is None:
        return None
    _rn = ent.get("ref_name") or ""
    # roles 三态：None=不动旧标注；列表（含空）=覆盖
    role_override = ("__keep__" if roles is None else
                     [str(r).strip() for r in roles
                      if str(r).strip() in ("首帧图", "尾帧图")][:2])
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        return None
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    AS = _as_mod()
    pool = _mark_pool(manifest)
    want_mark = AS.clean_mark(mark)
    if str(mark or "").strip() and not want_mark:
        raise ValueError(f"标注须是「图片N / 视频N / 音频N」形态，收到「{mark}」")
    if want_mark and AS.mark_taken(pool, want_mark, exclude_id=ent["asset_id"],
                                   exclude_label=ent["alias"]):
        raise ValueError(f"标注「{want_mark}」已被其它素材占用，请换一个")
    links = [x for x in (manifest.get("asset_links") or []) if isinstance(x, dict)]
    links = [x for x in (_clean_asset_link(x) for x in links) if x is not None]
    # 标注：显式给了就用它（改标注），没给就按"已有最大序号 +1"自动分配
    # （编号不回收 —— 删掉的号留空，避免旧提示词悄悄改指向）。
    _mk = str(ent.get("mark") or "").strip()
    _mk_explicit = bool(_mk)
    if not _mk:
        # 同名条目已在池里（旧 assets 或已有 link）：新链接会按 Map.set 语义
        # **顶替它的位置**，所以要**继承它原来的标注号** ——
        # 否则"换一张图但名字不变"会把 图片1 变成 图片3，历史提示词里的
        # `@图片1` 悄悄改指向另一张素材（静默串号）。
        _prev = next((x for x in pool
                      if str(x.get("alias") or x.get("label") or "") == ent["alias"]), None)
        _inherit = str((_prev or {}).get("mark") or "").strip()
        # 例外：**类型换轨不继承**。`图片1` 指代一个视频等于亲手喂给模型一条错信息，
        # 这时必须按新类型重发（`图片1` → `视频1`）。同类型才走继承（防串号）。
        _prev_kind = str((_prev or {}).get("kind") or "image")
        if _inherit and _prev_kind == ent["kind"]:
            _mk = _inherit
        else:
            # 序号必须按**整个池子**（旧 assets + 已有 links）算，不能只看 links ——
            # 只看 links 的话，项目里已有 图片1（在 assets 里）时新链进来的素材
            # 会再拿到 图片1，两个素材撞同一个号。
            _mk = _mark_of(ent["kind"],
                           _next_mark_seq(ent["kind"], [x.get("mark") for x in pool]))
    hit = False
    for x in links:
        if x["alias"] == ent["alias"]:
            # 类型换轨（图片→视频）时标注必须跟着换：标注是"给 LLM 看的短名"，
            # 留着 `图片1` 指代一个视频，等于亲手喂给模型一条错信息。
            _kind_changed = str(x.get("kind") or "image") != ent["kind"]
            x["asset_id"], x["kind"], hit = ent["asset_id"], ent["kind"], True
            if _rn:
                x["ref_name"] = _rn
            # _mk 已按**新类型**算过（见上），换轨时直接换上即可
            if _mk and (_mk_explicit or _kind_changed or not x.get("mark")):
                x["mark"] = _mk
            if role_override != "__keep__":
                x["roles"] = role_override
        elif x["asset_id"] == ent["asset_id"] and not hit:
            _kind_changed = str(x.get("kind") or "image") != ent["kind"]
            x["alias"], x["kind"] = ent["alias"], ent["kind"]
            if _rn:
                x["ref_name"] = _rn
            # _mk 已按**新类型**算过（见上），换轨时直接换上即可
            if _mk and (_mk_explicit or _kind_changed or not x.get("mark")):
                x["mark"] = _mk
            if role_override != "__keep__":
                x["roles"] = role_override
    if not any(x["asset_id"] == ent["asset_id"] for x in links):
        if role_override != "__keep__":
            ent["roles"] = role_override
        ent["mark"] = _mk
        links.append(ent)
    manifest["asset_links"] = links
    # 标注：按**池序**（旧 assets 先、links 覆盖同名）发号，与前端 poolFromManifest 同口径。
    # 注意 assign_marks 返回**新列表**（不就地改入参），必须把结果写回来才落盘 ——
    # 直接调用并丢弃返回值的话，缺号的条目永远补不上号（且不报错）。
    _pool = _mark_pool(manifest)
    for _old, _new in zip(_pool, AS.assign_marks(_pool)):
        if _new.get("mark"):
            _old["mark"] = _new["mark"]
    if want_mark:
        for d in _mark_pool(manifest):
            if str(d.get("asset_id") or "") == ent["asset_id"]:
                d["mark"], d["mark_auto"] = want_mark, False
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest



def unlink_asset(name: str, asset_id=None, alias=None, base_revision=None):
    """解链 + 清理引用：按 asset_id 或 alias 移除 manifest["asset_links"] 条目，
    同步从 segments[i].refs 和 prompts[i] 文本中移除该 alias 的所有 @引用。

    幂等：条目不存在同样成功（返回现 manifest）。目录或 manifest 不存在返回
    None；base_revision 不一致抛 ValueError(REVISION_CONFLICT)。只动链接与引用，
    不删全局库文件与旧 assets。
    """
    name = safe_name(name)
    if not name:
        return None
    aid = str(asset_id or "").strip()
    # 不截断 alias —— 历史 bug：[:_ASSET_LABEL_MAX] 让"ChatGPT_Image_2026年9月18日_14_02_12"
    # 变成"ChatGPT_Image_2026年9月18日"，与素材库不一致，正文清理会漏删。
    als = str(alias or "").strip()
    if not aid and not als:
        return None
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        return None
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    # 收集被解链条目的 alias（用于清理 seg.refs 和正文）。
    # 必须用完整 alias（包括历史 24 字符截断版），否则旧的 alias 清不掉。
    target_aliases = set()
    for x in (manifest.get("asset_links") or []):
        if not isinstance(x, dict):
            continue
        if (aid and str(x.get("asset_id") or "") == aid) or \
           (als and str(x.get("alias") or "") == als):
            if x.get("alias"):
                target_aliases.add(str(x["alias"]))
            if aid:
                target_aliases.add(aid)
    links = [x for x in (manifest.get("asset_links") or []) if isinstance(x, dict)]
    kept = [x for x in links
            if not ((aid and str(x.get("asset_id") or "") == aid)
                    or (als and str(x.get("alias") or "") == als))]
    if len(kept) != len(links):
        manifest["asset_links"] = [_clean_asset_link(x) or x for x in kept]
        manifest["asset_links"] = [x for x in manifest["asset_links"] if x is not None]
    # 同步清理 seg.refs 和正文里的 @xxx —— 此前只解链不解引用，导致"删了项目库
    # 里的东西提示词框的引用还在"（也是 seg.refs 漏删 → 引用条残留 → 渲染的 @xxx
    # 找不到素材而红框，但文本还在那里）。删素材（remove_assets）走同一份实现。
    if target_aliases:
        _purge_alias_refs(manifest, target_aliases)
    # 仅当真的有改动才升 revision 并落盘
    if target_aliases or len(kept) != len(links):
        manifest["updated_at"] = time.time()
        manifest["revision"] = int(manifest.get("revision") or 1) + 1
        manifest["manifest_schema"] = MANIFEST_SCHEMA
        checkpoint.save_manifest(root, manifest)
    return manifest


def remove_assets(name: str, labels=(), asset_ids=(), files=(), base_revision=None):
    """删素材后清清单：manifest["assets"] 按 label/file 摘、asset_links 按 alias/asset_id 摘，
    并同步清掉 segments[].refs 与正文里的 `@别名`。幂等（条目不存在也算成功）。

    为什么必须清（两条独立的症状，同一个根因）：
    - `manifest["assets"]` 是导演台「资产引用」栏（可引用素材池）的**唯一真相**
      —— 前端 poolFromManifest 按它整体重建，**不看磁盘**。所以项目资产的瓦片删掉了
      （`scan_scope("project")` 扫的是真目录），引用栏却永远少不掉那一栏：
      一条文件已不在、点了也点不亮的幽灵素材。
    - `segments[].refs` / 正文里的 `@别名` 不清就是死引用，编译期点名"找不到资产"。

    labels 与 files 都收：files 是刚被删掉的真路径（最可靠），命中条目后连它的
    label 一起进清理目标 —— 只按 label 匹配会漏掉"清单里 label 与索引显示名不一致"
    的条目（assets/ 下有未登记的文件时就是这样）。
    与 unlink_asset 的分工：那个只解"链接"（文件在全局库那份，绝不能删）；
    这个连项目内文件条目（manifest["assets"]）一起摘。
    目录或 manifest 不存在返回 None；base_revision 不一致抛 ValueError(REVISION_CONFLICT)。
    """
    name = safe_name(name)
    if not name:
        return None
    labels = {str(x).strip() for x in (labels or ()) if str(x).strip()}
    aids = {str(x).strip() for x in (asset_ids or ()) if str(x).strip()}
    files = {str(x).strip().replace("\\", "/") for x in (files or ()) if str(x).strip()}
    if not labels and not aids and not files:
        return None
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        return None
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")

    assets = [x for x in (manifest.get("assets") or []) if isinstance(x, dict)]

    def _rel(x):
        return str(x.get("file") or "").replace("\\", "/")

    # 先按 file 反查 label：删的是文件，清单里的 label 才是引用语法用的那个串
    for x in assets:
        if _rel(x) and _rel(x) in files and x.get("label"):
            labels.add(str(x["label"]))
    # 要清的别名 = 显式 label ∪ 命中链接的 alias ∪ asset_id 本身
    # （label 与 alias 同一命名空间；asset_id 也可能被当成引用 key 写进 seg.refs）
    targets = set(labels)
    for x in (manifest.get("asset_links") or []):
        if not isinstance(x, dict):
            continue
        if (str(x.get("asset_id") or "") in aids) or (str(x.get("alias") or "") in labels):
            if x.get("alias"):
                targets.add(str(x["alias"]))
    targets |= aids

    changed = False
    kept_assets = [x for x in assets
                   if str(x.get("label") or "") not in labels and _rel(x) not in files]
    if len(kept_assets) != len(assets):
        manifest["assets"] = kept_assets
        changed = True
    links = [x for x in (manifest.get("asset_links") or []) if isinstance(x, dict)]
    kept_links = [x for x in links
                  if not ((str(x.get("asset_id") or "") in aids)
                          or (str(x.get("alias") or "") in labels))]
    if len(kept_links) != len(links):
        manifest["asset_links"] = kept_links
        changed = True
    if _purge_alias_refs(manifest, targets):
        changed = True
    if changed:
        manifest["updated_at"] = time.time()
        manifest["revision"] = int(manifest.get("revision") or 1) + 1
        manifest["manifest_schema"] = MANIFEST_SCHEMA
        checkpoint.save_manifest(root, manifest)
    return manifest


def remove_latents(name: str, files, base_revision=None):
    """删 latent 后清 manifest["latents"] 登记（幂等，只动清单不删文件）。

    二采/锚源下拉会 `os.path.isfile` 自过滤，所以幽灵条目不至于"选不出来"，
    但素材库入口的计数（latent N）会虚高 —— 顺手清掉，口径与磁盘一致。
    登记里的 file 恒为 `latent/<名>`（见 slice_latent），而索引扫的是 latents/ 与
    latent/ 两个目录，所以按**相对路径或文件名**任一命中都摘。
    """
    name = safe_name(name)
    if not name:
        return None
    want = {str(f).strip().replace("\\", "/") for f in (files or ())
            if str(f).strip()}
    if not want:
        return None
    want_base = {w.split("/")[-1] for w in want}
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        return None
    _ensure_revision(manifest)
    cur = [x for x in (manifest.get("latents") or []) if isinstance(x, dict)]
    kept = [x for x in cur
            if str(x.get("file") or "").replace("\\", "/") not in want
            and str(x.get("file") or "").replace("\\", "/").split("/")[-1] not in want_base]
    if len(kept) == len(cur):
        return manifest
    manifest["latents"] = kept
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest


def forget_media(name: str, rel: str, base_revision=None):
    """删掉项目内某个媒体文件后，把它从 manifest 的**历史列表**里摘掉（幂等）。

    `manifest["finals"]`（成片文件名）与 `manifest["merges"]`（[{file,...}]）都只是
    历史记录，文件没了就该少一条 —— 否则右栏「成片」永远挂着一张放不出来的卡
    （video src 404），再点删除也没用（删的已经是不存在的文件）。与素材库删除同一根因。

    **故意不动 `manifest["videos"]`**：那是分段链的完成状态（第 i 段做完没有），
    摘掉等于告诉链"这段没做"→ 语义完全不同，要单独决策，不在"清理历史"的范围里。
    """
    name = safe_name(name)
    r = str(rel or "").strip().replace("\\", "/")
    if not name or not r:
        return None
    base = r.split("/")[-1]
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        return None
    _ensure_revision(manifest)

    def _hit(x):
        s = str(x or "").replace("\\", "/")
        return s == r or s.split("/")[-1] == base

    changed = False
    finals = [x for x in (manifest.get("finals") or []) if isinstance(x, str)]
    kept_f = [x for x in finals if not _hit(x)]
    if len(kept_f) != len(finals):
        manifest["finals"] = kept_f
        changed = True
    merges = [x for x in (manifest.get("merges") or []) if isinstance(x, dict)]
    kept_m = [x for x in merges if not _hit(x.get("file"))]
    if len(kept_m) != len(merges):
        manifest["merges"] = kept_m
        changed = True
    if not changed:
        return manifest
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest


def projects_linking(asset_ids, exclude=None) -> dict:
    """哪些项目链接了这些全局资产 -> {asset_id: [项目名...]}（不含 exclude）。

    删全局库素材前用来回答"会波及谁"：全局库是内容寻址的单份文件，多项目靠
    asset_links 引它 —— 删掉文件，那些项目的引用就断了。不是禁止删（用户的地盘
    用户做主），但必须说出来，别让人过两天才发现某个项目打不开。
    """
    want = {str(a).strip() for a in (asset_ids or ()) if str(a).strip()}
    if not want:
        return {}
    skip = str(exclude or "")
    out = {}
    root = checkpoint.projects_root()
    if not os.path.isdir(root):
        return out
    for nm in os.listdir(root):
        if nm == skip or not safe_name(nm):
            continue
        pdir = os.path.join(root, nm)
        if not os.path.isdir(pdir):
            continue
        try:
            with open(os.path.join(pdir, "manifest.json"), "r", encoding="utf-8") as f:
                mf = json.load(f)
        except Exception:
            continue
        if not isinstance(mf, dict):
            continue
        for x in (mf.get("asset_links") or []):
            if not isinstance(x, dict):
                continue
            aid = str(x.get("asset_id") or "")
            if aid in want:
                out.setdefault(aid, []).append(nm)
    return out


# 段级字符串字段白名单。B02 三框合一后只剩传统的场景/角色/环境音/配乐四项：
# 提示词正文（含意图与剧本的最终产物）统一在 prompts[idx]，不再有 intent_zh / script
# 这两个中间字段 —— 前端老项目的文字靠 migrateLegacySegText 在主框为空时回填。
_SEG_STR_FIELDS = ("scene_prompt", "character_prompt", "soundscape", "music")


def _clean_seg_field(raw) -> dict | None:
    """前端分段字段白名单清洗：未知键剔除、类型收敛（防脏 JSON 膨胀 manifest）。

    M2.5：透存 prompt_v2（具象化结构，经 prompts.clean_prompt 收敛，失败则丢弃该键不炸链）。
    """
    if not isinstance(raw, dict):
        return None
    out = {k: (str(raw[k]) if isinstance(raw.get(k), str) else "") for k in _SEG_STR_FIELDS}
    sec = raw.get("seconds")
    out["seconds"] = float(sec) if isinstance(sec, (int, float)) and sec > 0 else None
    # 新双按钮（分段优先，null=跟随全局默认 true）：auto_ref=false 即旧 unlink=true；
    # auto_seq=false 即旧 disabled=true。旧键仍兼容读入并回填新键。
    def _tri(v):
        if v is None:
            return None
        return bool(v)
    auto_ref = _tri(raw.get("auto_ref")) if "auto_ref" in raw else None
    auto_seq = _tri(raw.get("auto_seq")) if "auto_seq" in raw else None
    if auto_ref is None and isinstance(raw.get("unlink"), bool):
        auto_ref = not bool(raw.get("unlink"))
    if auto_seq is None and isinstance(raw.get("disabled"), bool):
        auto_seq = not bool(raw.get("disabled"))
    # 旧调用只传 unlink/disabled（无新键）时保持旧键语义，避免老前端被洗成 null
    out["unlink"] = (not auto_ref) if auto_ref is not None else bool(raw.get("unlink"))
    out["disabled"] = (not auto_seq) if auto_seq is not None else bool(raw.get("disabled"))
    out["auto_ref"] = auto_ref
    out["auto_seq"] = auto_seq
    # 段引用允许重复（同一素材在一段内引用 N 次 = 正文里写 N 次 @别名），
    # 上限按去重后的素材个数算（见 nodes 组装期），这里原样保留重复项。
    refs = raw.get("refs")
    out["refs"] = [str(x) for x in refs if isinstance(x, (str, int))][:64] \
        if isinstance(refs, list) else []
    # 段级首尾帧参考图（提示词框「首帧图/尾帧图」按钮选的项目内图片）：
    # 只留 {first,end} 两个相对路径键，防穿越
    fi = raw.get("frame_img")
    if isinstance(fi, dict):
        keep = {}
        for k in ("first", "end"):
            parts = [p for p in str(fi.get(k) or "").replace("\\", "/").split("/")
                     if p and p != "."]
            if parts and len(parts) <= 2 and ".." not in parts \
                    and not any((":" in p) or p.startswith(".") for p in parts):
                keep[k] = "/".join(parts)
        if keep:
            out["frame_img"] = keep
    fr = raw.get("frame_refs")
    out["frame_refs"] = [str(x) for x in fr if isinstance(x, (str, int))][:4] \
        if isinstance(fr, list) else None
    pv = raw.get("prompt_v2")
    if isinstance(pv, dict):
        try:
            from . import prompts as _prompts
        except ImportError:
            import prompts as _prompts
        try:
            out["prompt_v2"] = _prompts.clean_prompt(pv)
        except Exception:
            pass
    # v2 手动模式（null=跟随导演台）：白名单透存，未知值归 null
    vm = raw.get("v2mode")
    out["v2mode"] = vm if isinstance(vm, str) and vm in (
        "T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA") else None
    # M3：每段 latent 保存策略透存 {mode: all|range|tail|off, start_f, end_f, tail_f,
    # split_av, save_seg, save_all}；latent 引用策略 {on, frames, video, audio}
    # （null=跟随全局，默认尾部 N 帧钉下段首）。
    ls = raw.get("latent_save")
    if isinstance(ls, dict):
        mode = str(ls.get("mode") or "all").strip()
        if mode not in ("all", "range", "tail", "off"):
            mode = "all"

        def _i(v):
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                return 0
        out["latent_save"] = {"mode": mode, "start_f": _i(ls.get("start_f", 0)),
                              "end_f": _i(ls.get("end_f", 0)), "tail_f": _i(ls.get("tail_f", 0)),
                              "split_av": bool(ls.get("split_av", False)),
                              "save_seg": ls.get("save_seg", True) is not False,
                              "save_all": ls.get("save_all", True) is not False}
    lr = raw.get("latent_ref")
    if isinstance(lr, dict):
        def _i2(v):
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                return 0
        on = lr.get("on")
        ent = {"on": None if on is None else bool(on),
               "frames": _i2(lr.get("frames", 0)),
               "video": lr.get("video", True) is not False,
               "audio": lr.get("audio", True) is not False}
        # 外源 keyframe：只认 latent/<名>.pt（防穿越；文件存在性执行期校验）
        src = lr.get("src")
        if isinstance(src, dict) and src.get("file"):
            f = str(src["file"]).strip().replace("\\", "/")
            parts = [p for p in f.split("/") if p and p != "."]
            if len(parts) == 2 and parts[0] == "latent" and safe_name(parts[1]) \
                    and parts[1].endswith(".pt"):
                ent["src"] = {"file": "/".join(parts)}
        out["latent_ref"] = ent
    # 段尾锚来源（替代旧全局每段尾帧锚定）：{asset: 标签} | {latent: latent/x.pt}，
    # 非法值丢弃（执行期按未知标签/缺文件报错点名）
    ts = raw.get("tail_src")
    if isinstance(ts, dict):
        if ts.get("asset"):
            lbl = str(ts["asset"]).strip()[:24]
            if lbl:
                out["tail_src"] = {"asset": lbl}
        elif ts.get("latent"):
            f = str(ts["latent"]).strip().replace("\\", "/")
            parts = [p for p in f.split("/") if p and p != "."]
            if len(parts) == 2 and parts[0] == "latent" and safe_name(parts[1]) \
                    and parts[1].endswith(".pt"):
                out["tail_src"] = {"latent": "/".join(parts)}
    return out


def _latent_dir(root: str) -> str:
    d = os.path.join(root, "latent")
    os.makedirs(d, exist_ok=True)
    return d


def _clean_latent_name(name) -> str:
    s = str(name or "").strip().replace("\\", "/").split("/")[-1]
    if not safe_name(s) or not os.path.splitext(s)[1]:
        return ""
    if not s.endswith(".pt"):
        s += ".pt"
    return s


def slice_latent(name, src, start_f, end_f, save_name, base_revision=None, kind="av"):
    """latent-to-latent 剪辑：段存档 .pt / 已登记 latent 按像素帧窗切片 -> latent/<save_name>.pt。

    M3 三源之一（段范围/尾帧/已存 latent 再剪），纯 torch 无需 VAE：
    video [B,C,T,H,W] 按 frames_to_latent_t 换算 token 窗整 token 切，
    audio [1,32,2,T] 按 audio_tokens_for_frames 比例切。越界/切空抛 ValueError。
    src: {"seg": 0-based段文件号} | {"file": "latent/x.pt"}。登记 manifest["latents"] 并 revision+1。
    kind: "av"（默认，图像+音频）| "video"（只留图像分支）| "audio"（只留音频分支，
    图像/音频分开保存时用；audio 产物无 video 键，注入时只走音频锚）。
    """
    import torch
    try:
        from .grid import frames_to_latent_t, latent_t_to_frames, audio_tokens_for_frames
    except ImportError:
        from grid import frames_to_latent_t, latent_t_to_frames, audio_tokens_for_frames
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    if not isinstance(src, dict):
        raise ValueError("src 须为 {seg: 段号} 或 {file: latent/x.pt}")
    if "seg" in src:
        try:
            idx = int(src["seg"])
        except (TypeError, ValueError):
            raise ValueError(f"段号必须是整数：{src.get('seg')!r}") from None
        if idx < 0:
            raise ValueError("段号从 0 开始（0-based 段文件号）")
        src_path = os.path.join(root, f"seg_{idx:03d}.pt")
        src_desc = f"seg_{idx:03d}.pt"
    else:
        f = str(src.get("file") or "").strip().replace("\\", "/")
        parts = [p for p in f.split("/") if p and p != "."]
        if len(parts) != 2 or parts[0] != "latent" or not safe_name(parts[1]):
            raise ValueError(f"非法 latent 文件：{f!r}（须为 latent/<名>.pt）")
        src_path = os.path.join(root, *parts)
        src_desc = "/".join(parts)
    if not os.path.isfile(src_path):
        raise ValueError(f"源 latent 不存在：{src_desc}（该段还没生成存档？）")
    try:
        sf, ef = int(start_f), int(end_f)
    except (TypeError, ValueError):
        raise ValueError("start_f/end_f 须为整数像素帧") from None
    if ef <= sf or sf < 0:
        raise ValueError(f"帧窗为空（[{sf}, {ef})，须 0 <= start < end）")
    clean = _clean_latent_name(save_name)
    if not clean:
        raise ValueError(f"非法保存名：{save_name!r}")
    kind = str(kind or "av").strip().lower()
    if kind not in ("av", "video", "audio"):
        raise ValueError(f"非法 kind：{kind!r}（须为 av/video/audio）")
    with open(src_path, "rb") as fh:
        payload = torch.load(fh, map_location="cpu", weights_only=True)
    video, audio = payload.get("video"), payload.get("audio")
    if kind == "audio" and (audio is None or getattr(audio, "dim", lambda: 0)() != 4):
        raise ValueError("源 latent 无音频分支，无法只取音频（该 latent 可能是纯图像产物）")
    if kind != "audio" and (video is None or getattr(video, "dim", lambda: 0)() != 5):
        raise ValueError("源 latent 视频张量形态异常（须 5 维 [B,C,T,H,W]）")
    if kind == "audio":
        a_total = int(audio.shape[-1])
        # 纯音频切：按 latent 音频 token 等比映射帧窗
        total_frames = max(1, int(round(a_total / audio_tokens_for_frames(1)))) \
            if audio_tokens_for_frames(1) else a_total
        if sf >= total_frames:
            raise ValueError(f"start_f {sf} 越界（源音频约 {total_frames} 帧）")
        ef = min(ef, total_frames)
        a0 = max(0, int(round(sf / max(1, total_frames) * a_total)))
        a1 = max(a0 + 1, min(a_total, int(round(ef / max(1, total_frames) * a_total))))
        out_payload = {"audio": audio[..., a0:a1].contiguous().clone()}
        t0, t1 = 0, 0
    else:
        total_t = int(video.shape[2])
        total_frames = latent_t_to_frames(total_t)
        if sf >= total_frames:
            raise ValueError(f"start_f {sf} 越界（源共约 {total_frames} 帧）")
        ef = min(ef, total_frames)
        t0 = max(0, frames_to_latent_t(sf, up=False))
        t1 = min(total_t, frames_to_latent_t(ef, up=True))
        if t1 <= t0:
            # 帧窗落在同一 token 内：按整 token 取 1 个（含目标帧），不返回空
            t0 = max(0, min(total_t - 1, frames_to_latent_t(sf, up=True) - 1))
            t1 = min(total_t, t0 + 1)
        cut_v = video[:, :, t0:t1].contiguous().clone()
        cut_a = None
        if audio is not None and getattr(audio, "dim", lambda: 0)() == 4:
            a_total = int(audio.shape[-1])
            a0 = max(0, int(round(sf / max(1, total_frames) * a_total)))
            a1 = max(a0 + 1, min(a_total, int(round(ef / max(1, total_frames) * a_total))))
            cut_a = audio[..., a0:a1].contiguous().clone()
        _ = audio_tokens_for_frames(ef - sf)  # 换算自检（比例异常时不断言，只记录）
        out_payload = {"video": cut_v}
        if kind == "av":
            if cut_a is not None:
                out_payload["audio"] = cut_a
            elif audio is not None:
                out_payload["audio"] = audio
        # kind == "video": 故意不带 audio 键（图像单独保存）
    ldir = _latent_dir(root)
    tmp_path = os.path.join(ldir, clean + ".part")
    final_path = os.path.join(ldir, clean)
    import tempfile
    buf = tempfile.SpooledTemporaryFile(max_size=64 << 20)
    torch.save(out_payload, buf)
    buf.seek(0)
    with open(tmp_path, "wb") as fh:
        fh.write(buf.read())
    buf.close()
    os.replace(tmp_path, final_path)
    manifest.setdefault("latents", [])
    manifest["latents"] = [x for x in manifest["latents"]
                           if isinstance(x, dict) and x.get("file") != f"latent/{clean}"]
    manifest["latents"].append({"file": f"latent/{clean}", "src": src_desc,
                                "start_f": sf, "end_f": ef, "kind": kind,
                                "tokens": [t0, t1], "updated_at": time.time()})
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest


def delete_latent(name, file, base_revision=None):
    """删除 latent 登记文件（幂等）。"""
    name = safe_name(name)
    f = str(file or "").strip().replace("\\", "/")
    parts = [p for p in f.split("/") if p and p != "."]
    if not name or len(parts) != 2 or parts[0] != "latent" or not safe_name(parts[1]):
        raise ValueError(f"非法 latent 文件：{file!r}")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在")
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    try:
        os.remove(os.path.join(root, *parts))
    except OSError:
        pass
    manifest["latents"] = [x for x in (manifest.get("latents") or [])
                           if not (isinstance(x, dict) and x.get("file") == f)]
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    checkpoint.save_manifest(root, manifest)
    return manifest


def _library_of_relpath(root: str, rel: str) -> str:
    """相对路径 -> 所属库目录名（assets/finals/根）。"""
    norm = str(rel or "").replace("\\", "/")
    if norm.startswith("assets/"):
        return "assets"
    if norm.startswith("finals/"):
        return "finals"
    return "finals"


def trim_asset(name, src_file, start_s, end_s, save_name=None, base_revision=None,
               fps=24, crf=20):
    """资产/成片入出点裁剪：项目内 mp4 [start_s, end_s) -> 新文件（重编码）。

    M3 最小剪辑集：资产库行内剪 + 成片库片段截取共用。长耗时放调用方线程池。
    src_file 须为项目内 mp4（seg_/final_/merged_/clip_*.mp4，裸名/finals//assets/
    前缀双兼容）；save_name 缺省 clip_<stamp>.mp4。产物落源文件同库目录
    （资产剪进 assets/，成片剪进 finals/），登记 manifest["clips"] 并 revision+1。
    失败抛 ValueError/RuntimeError。
    """
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    f = str(src_file or "").strip().replace("\\", "/")
    parts = [p for p in f.split("/") if p and p != "."]
    if not parts or len(parts) > 2 or not all(safe_name(p) for p in parts) \
            or not parts[-1].endswith(".mp4"):
        raise ValueError(f"非法源文件：{src_file!r}（须为项目内 xxx.mp4）")
    src_rel = "/".join(parts)
    src_path = checkpoint.resolve_project_file(root, src_rel)
    if not os.path.isfile(src_path):
        raise ValueError(f"源文件不存在：{src_rel}")
    # 实际命中的相对路径（用于 clips 登记与同库落盘判定）
    try:
        src_hit = os.path.relpath(src_path, root).replace("\\", "/")
    except ValueError:
        src_hit = src_rel
    try:
        ss, es = float(start_s), float(end_s)
    except (TypeError, ValueError):
        raise ValueError("start_s/end_s 须为数字秒") from None
    if es <= ss or ss < 0:
        raise ValueError(f"裁剪区间为空（[{ss}s, {es}s)，须 0 <= start < end）")
    lib = _library_of_relpath(root, src_hit)
    out_dir = os.path.join(root, lib)
    os.makedirs(out_dir, exist_ok=True)
    if save_name:
        clean = str(save_name).strip().replace("\\", "/").split("/")[-1]
        if not safe_name(clean):
            raise ValueError(f"非法保存名：{save_name!r}")
        if not clean.endswith(".mp4"):
            clean += ".mp4"
    else:
        clean = f"clip_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        k = 2
        while os.path.exists(os.path.join(out_dir, clean)):
            clean = f"clip_{time.strftime('%Y%m%d_%H%M%S')}_{k}.mp4"
            k += 1
    out_rel = f"{lib}/{clean}" if lib in ("assets", "finals") else clean
    try:
        from . import media
    except ImportError:
        import media
    ok = media.trim_av_mp4(src_path, os.path.join(out_dir, clean), ss, es,
                           fps=fps, crf=crf)
    if not ok:
        raise RuntimeError(f"裁剪编码失败：{media.last_error}")
    fresh = checkpoint.load_manifest(root)
    if isinstance(fresh, dict):
        manifest = fresh
        _ensure_revision(manifest)
    manifest.setdefault("clips", []).append({"file": out_rel, "src": src_hit,
                                             "start_s": ss, "end_s": es,
                                             "updated_at": time.time()})
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest


def _rewrite_media_refs(manifest: dict, old_rel: str, new_rel: str) -> int:
    """manifest 内全部媒体引用 old->new 改写，返回改动条数。

    覆盖 videos/thumbs/finals/merges.file/merges.items[].file/clips
    .file/.src（链路一致性：移动成片库文件不断链）。
    """
    n = 0
    for key in ("videos", "thumbs", "finals"):
        seq = manifest.get(key)
        if isinstance(seq, list):
            for i, v in enumerate(seq):
                if v == old_rel:
                    seq[i] = new_rel
                    n += 1
    for m in (manifest.get("merges") or []):
        if isinstance(m, dict):
            if m.get("file") == old_rel:
                m["file"] = new_rel
                n += 1
            for it in (m.get("items") or []):
                if isinstance(it, dict) and it.get("file") == old_rel:
                    it["file"] = new_rel
                    n += 1
    for c in (manifest.get("clips") or []):
        if isinstance(c, dict):
            if c.get("file") == old_rel:
                c["file"] = new_rel
                n += 1
            if c.get("src") == old_rel:
                c["src"] = new_rel
                n += 1
    return n


def move_media(name, src_file, dest_lib, save_name=None, base_revision=None,
               register_asset=False, label="", kind="video"):
    """成片库 <-> 资产库互调：项目内媒体文件在 assets/finals 间搬家。

    - src_file：项目内相对路径（裸名/finals//assets/ 前缀双兼容；mp4/png/wav）。
    - dest_lib：目标库 "assets" | "finals"。
    - save_name：目标文件名（缺省=同名；同名已存在自动 _2 后缀）。
    - manifest 内 videos/thumbs/finals/merges/clips 引用同步改写，不断链；
      seg_*.pt（链路 latent 存档）禁止移动（抛 ValueError）。
    - register_asset=true 且 dest=assets 时追加 manifest["assets"] 条目
      {label, kind, file}（label 缺省=文件名去扩展名，供提示词 [[标签]] 引用）。
    生成中调用由路由层以 423 拦截（见 checkpoint.is_busy），此处不重复判定以便单测。
    """
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    if dest_lib not in ("assets", "finals"):
        raise ValueError(f"非法目标库：{dest_lib!r}（须为 assets/finals）")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    f = str(src_file or "").strip().replace("\\", "/")
    parts = [p for p in f.split("/") if p and p != "."]
    if not parts or len(parts) > 2 or not all(safe_name(p) for p in parts) \
            or not os.path.splitext(parts[-1])[1]:
        raise ValueError(f"非法源文件：{src_file!r}")
    src_rel = "/".join(parts)
    src_path = checkpoint.resolve_project_file(root, src_rel)
    if not os.path.isfile(src_path):
        raise ValueError(f"源文件不存在：{src_rel}")
    try:
        src_hit = os.path.relpath(src_path, root).replace("\\", "/")
    except ValueError:
        src_hit = src_rel
    if src_hit.endswith(".pt") or os.path.basename(src_hit).startswith("seg_") \
            and src_hit.endswith(".pt"):
        raise ValueError("段 latent 存档（seg_*.pt）禁止移动（断链风险）；请用 latent 切片另存")
    if save_name:
        clean = str(save_name).strip().replace("\\", "/").split("/")[-1]
        if not safe_name(clean) or not os.path.splitext(clean)[1]:
            raise ValueError(f"非法保存名：{save_name!r}")
    else:
        clean = os.path.basename(src_hit)
    dest_dir = os.path.join(root, dest_lib)
    os.makedirs(dest_dir, exist_ok=True)
    base, ext = os.path.splitext(clean)
    cand, k = clean, 2
    while os.path.exists(os.path.join(dest_dir, cand)):
        cand = f"{base}_{k}{ext}"
        k += 1
    dest_path = os.path.join(dest_dir, cand)
    dest_rel = f"{dest_lib}/{cand}"
    if os.path.realpath(src_path) == os.path.realpath(dest_path):
        raise ValueError("源与目标是同一文件，无需移动")
    os.replace(src_path, dest_path)
    # 旧 manifest 可能记的是裸名：新旧两种写法都改写
    _rewrite_media_refs(manifest, src_hit, dest_rel)
    _rewrite_media_refs(manifest, os.path.basename(src_hit), dest_rel)
    _rewrite_media_refs(manifest, src_rel, dest_rel)
    if register_asset and dest_lib == "assets":
        lbl = _alias_of(label, os.path.splitext(cand)[0])
        kk = str(kind or "video").strip()
        if kk not in ("image", "video", "audio"):
            kk = "video"
        manifest.setdefault("assets", [])
        if all(not (isinstance(a, dict) and a.get("label") == lbl) for a in manifest["assets"]):
            manifest["assets"] = _dedupe_assets(
                list(manifest.get("assets") or []) + [{"label": lbl, "kind": kk, "file": dest_rel}])
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest


def split_av(name, src_file, save_video=None, save_audio=None, base_revision=None,
             fps=24, crf=20):
    """音画分离：项目内 mp4 -> 画面 mp4 + 音频 wav（流式单遍 demux）。

    - save_video 缺省 <stem>_v.mp4（落源文件同库），save_audio 缺省 <stem>_a.wav。
    - 视频轨 packet 级 remux（零解码零重编码、无损秒级）；音频轨解码收 PCM。
    - 无音轨源：只产出画面文件并在 clips 登记 note=no-audio。
    - 登记 manifest["clips"]（两条，src 同指源文件）并 revision+1。
    旧版先整片解码（长片 19GB 级 OOM 卡死整机）；现视频零内存、内存 O(音频)。
    fps/crf 参数保留兼容（remux 不重编码，不再使用）。
    供“只编码图像 / 只编码音频进 latent”前一步：分离后再分别走
    主节点自动专跑转码（提交即执行）或 latent 切片 kind 过滤。
    """
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    f = str(src_file or "").strip().replace("\\", "/")
    parts = [p for p in f.split("/") if p and p != "."]
    if not parts or len(parts) > 2 or not all(safe_name(p) for p in parts) \
            or not parts[-1].endswith(".mp4"):
        raise ValueError(f"非法源文件：{src_file!r}（须为项目内 xxx.mp4）")
    src_path = checkpoint.resolve_project_file(root, "/".join(parts))
    if not os.path.isfile(src_path):
        raise ValueError(f"源文件不存在：{'/'.join(parts)}")
    try:
        src_hit = os.path.relpath(src_path, root).replace("\\", "/")
    except ValueError:
        src_hit = "/".join(parts)
    lib = _library_of_relpath(root, src_hit)
    out_dir = os.path.join(root, lib)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src_hit))[0]

    def _clean(v, default):
        if v:
            c = str(v).strip().replace("\\", "/").split("/")[-1]
            if not safe_name(c):
                raise ValueError(f"非法保存名：{v!r}")
            return c
        c, k = default, 2
        while os.path.exists(os.path.join(out_dir, c)):
            b, e = os.path.splitext(default)
            c = f"{b}_{k}{e}"
            k += 1
        return c
    v_name = _clean(save_video, f"{stem}_v.mp4")
    if not v_name.endswith(".mp4"):
        v_name += ".mp4"
    a_name = _clean(save_audio, f"{stem}_a.wav")
    if not a_name.endswith(".wav"):
        a_name += ".wav"
    try:
        from . import media
    except ImportError:
        import media
    info = media.split_av_stream(src_path, os.path.join(out_dir, v_name),
                                 os.path.join(out_dir, a_name))
    if info is None:
        raise RuntimeError(f"音画分离失败：{media.last_error}")
    v_rel = f"{lib}/{v_name}"
    if not info.get("audio"):
        a_rel = ""
        note = "no-audio"
    else:
        a_rel = f"{lib}/{a_name}"
        note = ""
    fresh = checkpoint.load_manifest(root)
    if isinstance(fresh, dict):
        manifest = fresh
        _ensure_revision(manifest)
    now = time.time()
    manifest.setdefault("clips", []).append({"file": v_rel, "src": src_hit,
                                             "op": "split-v", "note": note,
                                             "updated_at": now})
    if a_rel:
        manifest["clips"].append({"file": a_rel, "src": src_hit,
                                  "op": "split-a", "updated_at": now})
    manifest["updated_at"] = now
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest


def import_asset(name, src_file, label="", kind="image", roles=None, base_revision=None):
    """入库：input 目录文件拷贝进项目 assets/，返回 {"file": "assets/<名>", ...}。

    - src_file：input 内相对路径（至多两级子目录，防穿越；realpath 复核不出 input）。
    - 同名已存在则加 _2 后缀，不覆盖。
    - 同时登记 manifest["assets"]（label 去重：重复 label 自动加后缀）。
    - 剪辑/转码统一在入库后的项目文件上做，项目自包含、删项目连带走。
    """
    import shutil
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    f = str(src_file or "").strip().replace("\\", "/")
    parts = [p for p in f.split("/") if p and p != "."]
    if not parts or len(parts) > 2 or not all(safe_name(p) for p in parts) \
            or not os.path.splitext(parts[-1])[1]:
        raise ValueError(f"非法源文件：{src_file!r}")
    try:
        from folder_paths import get_input_directory
        in_root = os.path.realpath(get_input_directory())
    except Exception:
        in_root = None
    if not in_root:
        raise ValueError("无法定位 input 目录")
    src_abs = os.path.realpath(os.path.join(in_root, *parts))
    if not src_abs.startswith(in_root + os.sep) or not os.path.isfile(src_abs):
        raise ValueError(f"源文件不存在（input 目录没有）：{'/'.join(parts)}")
    kk = str(kind or "image").strip()
    if kk not in _ASSET_KINDS:
        kk = "image"
    adir = os.path.join(root, "assets")
    os.makedirs(adir, exist_ok=True)
    cand = _unique_filename(adir, parts[-1])
    shutil.copy2(src_abs, os.path.join(adir, cand))
    dest_rel = f"assets/{cand}"
    taken = {a["label"] for a in (manifest.get("assets") or [])
             if isinstance(a, dict) and a.get("label")}
    lbl = _alias_of(label, os.path.splitext(cand)[0])
    lbl = _unique_label(lbl, taken)
    ent = {"label": lbl, "kind": kk, "file": dest_rel}
    if isinstance(roles, list):
        kept = [str(r).strip() for r in roles if str(r).strip() in ("首帧图", "尾帧图")]
        if kept:
            ent["roles"] = kept[:2]
    manifest["assets"] = _dedupe_assets(list(manifest.get("assets") or []) + [ent])
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return {"file": dest_rel, "label": lbl, "kind": kk,
            "roles": ent.get("roles", []), "manifest": manifest}


def save_prompts(name: str, prompts, segments=None, base_revision=None):
    """把导演台当前提示词组回写进项目 manifest（提示词的持久源=项目文件夹）。

    只更新 prompts / seg_fields / total / updated_at / revision（及 done 超界钳制），不动
    params / seeds / prompt_hashes / finals / merges——运行时的重做判定依旧按
    节点控件提示词 vs prompt_hashes 逐段比对，改词段落照常自动重做。
    manifest.prompts 与磁盘段文件按全局槽位对齐（前端卡片/合并按此索引）：
    序章项目自动补回「序章」占位头——纯提示词回写不能让槽位错位。
    （「插入视频」段类型已随手动锚定重构移除，故不再有插入槽占位。）
    segments 为分段处理字段（场景/角色/环境音/配乐/时长/独立镜头/不上链/
    参考标签/首尾帧引用），与 prompts 同序（仅提示词段），存为
    manifest["seg_fields"] 并按全局槽位对齐（序章槽为 null）——
    切换项目时前端据此还原各段卡片，不再丢分段字段。
    目录或 manifest 不存在（未跑过的指纹目录）返回 None，调用方按无项目跳过。
    base_revision 非空时做乐观锁：与当前 revision 不一致抛 ValueError(REVISION_CONFLICT)，
    调用方映射 409。M1 新增，向后兼容（不传即不检查）。
    """
    name = safe_name(name)
    if not name or not isinstance(prompts, list):
        return None
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        return None
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    seg_prompts = [str(p) for p in prompts][:64]
    seg_meta = [_clean_seg_field(x) for x in segments] \
        if isinstance(segments, list) else []
    while len(seg_meta) < len(seg_prompts):
        seg_meta.append(None)
    off = 1 if manifest.get("has_prologue") else 0
    rows, fields, si = [], [], 0
    for g in range(off + len(seg_prompts)):
        if g == 0 and off:
            rows.append("「序章（上传视频）」")
            fields.append(None)
        else:
            rows.append(seg_prompts[si])
            fields.append(seg_meta[si])
            si += 1
    manifest["prompts"] = rows
    # 全空（无任何分段字段）不写键：旧 manifest 结构零变化，体积零增长
    if any(x is not None for x in fields):
        manifest["seg_fields"] = fields
    else:
        manifest.pop("seg_fields", None)
    manifest["total"] = len(rows)
    manifest["done"] = min(int(manifest.get("done") or 0), manifest["total"])
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    # 文本库镜像：提示词快照同步一份到 texts/（项目文本文件夹，报告/日志同目录）
    try:
        tdir = checkpoint.texts_dir(root)
        snap = {"dir": safe_name(name), "revision": manifest.get("revision"),
                "updated_at": manifest.get("updated_at"),
                "prompts": list(manifest.get("prompts") or []),
                "seg_fields": list(manifest.get("seg_fields") or [])}
        with open(os.path.join(tdir, "prompts_latest.json"), "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return manifest


def create_project(name: str):
    """新建项目：当场建文件夹 + 写初始 manifest（0 段草稿态），列表立即可见。

    游戏存档槽语义——此前文件夹要等首次运行 ckpt_dir() 才建、manifest 要等
    首段采样完才写，点完「新建项目」磁盘上什么都没有，项目也不在列表里。
    params 留空：首跑 assert_match 对空旧档按「沿用当前值」放行（旧档缺键
    视为一致），跑完由真实参数覆写；title/created_at 首跑会被继承保留。
    已存在（含跑过一段以上的正式项目）直接幂等返回现 manifest，不重写。
    """
    name = safe_name(name)
    if not name:
        return None
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is not None:
        checkpoint.ensure_project_dirs(root)
        return _ensure_revision(manifest)
    os.makedirs(root, exist_ok=True)
    checkpoint.ensure_project_dirs(root)
    now = time.time()
    manifest = {
        "schema": checkpoint.SCHEMA, "manifest_schema": MANIFEST_SCHEMA,
        "revision": 1, "done": 0, "total": 0,
        "title": name, "created_at": now, "updated_at": now,
        "prompts": [], "params": {}, "finals": [],
    }
    checkpoint.save_manifest(root, manifest)
    return manifest


def delete_project(name: str):
    """删除项目 = 删除整个项目文件夹；被删的是当前链时清掉状态指针。

    返回被删目录的相对路径（output 内）；目录不存在返回 None。
    抛出的 OSError（如文件被占用）由调用方（路由层）捕获反馈。
    """
    name = safe_name(name)
    if not name or name == "h3chain_state.json":
        return None
    root = checkpoint.projects_root()
    target = os.path.join(root, name)
    if not os.path.isdir(target):
        return None
    shutil.rmtree(target)
    state = checkpoint.load_state()
    if isinstance(state, dict) and state.get("dir") == name:
        try:
            os.remove(os.path.join(root, "h3chain_state.json"))
        except OSError:
            pass
    return f"h3_projects/{name}"


def upscale_reset(name, seg):
    """清掉某段的二采记录与产物文件（下次开启二采运行时该段重做）。

    新方案高清分段与基础段同名合并存储（seg_NNN.mp4 即二采结果），故重置
    连同 seg mp4/缩略图/尾帧锚一起删（下次运行按记录缺失自愈重建：二采开=
    重渲染高清，二采关=回放段重编码基础分辨率）。不碰基础链的段 latent 存档
    （seg_NNN.pt）、finals 与 merges；段号是 1-based 全局槽位（含序章
    视频段，与前端段落卡片链位一致）。项目/段号非法抛 ValueError。
    """
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    try:
        g = int(seg) - 1
    except (TypeError, ValueError):
        raise ValueError(f"段号必须是整数：{seg!r}") from None
    if g < 0:
        raise ValueError("段号从 1 开始（1-based 全局槽位）")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    total = int(manifest.get("total") or 0)
    if total and g >= total:
        raise ValueError(f"段号 {g + 1} 越界（有效范围 1-{total}）")
    up = dict(manifest.get("upscale") or {})
    segs = [x if isinstance(x, dict) else None for x in (up.get("segs") or [])]
    while len(segs) <= g:
        segs.append(None)
    segs[g] = None
    up["segs"] = segs
    manifest["upscale"] = up
    manifest["updated_at"] = time.time()
    checkpoint.save_manifest(root, manifest)
    for f in (*checkpoint.upscale_files(g).values(), *checkpoint.upscale_legacy_files(g)):
        # 四库兼容：finals/ 前缀与旧根目录裸名双路径都清
        for cand in (os.path.join(root, f),
                     os.path.join(root, "finals", os.path.basename(f)),
                     os.path.join(root, os.path.basename(f))):
            try:
                os.remove(cand)
            except OSError:
                pass
    return manifest


def redo_cancel(name, slot):
    """从 manifest 重摇队列移除某段（撤销已提交未执行的重摇标记）。

    slot 是 0-based 全局槽位（与 redo_queue 条目、前端 ds.redo_segs 同口径）。
    幂等：槽位不在队列时同样返回成功（前端「取消重摇标记」统一走此口，
    覆盖 ds 标记与已入队条目两种来源）。只改 manifest，不动段 latent 存档
    与已成产物。项目/槽位非法抛 ValueError（路由层映射 400）。
    """
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    try:
        s = int(slot)
    except (TypeError, ValueError):
        raise ValueError(f"槽位必须是整数：{slot!r}") from None
    if s < 0:
        raise ValueError("槽位从 0 开始（0-based 全局槽位）")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    kept = []
    for x in (manifest.get("redo_queue") or []):
        try:
            if int(x[0]) == s:
                continue
        except (TypeError, ValueError, IndexError):
            pass
        kept.append(x)
    manifest["redo_queue"] = kept
    manifest["updated_at"] = time.time()
    checkpoint.save_manifest(root, manifest)
    return manifest


def _merge_sources(root: str, manifest: dict, items):
    """合并清单 -> 按序绝对路径列表。非法/缺失抛 ValueError。

    - {"seg": n}：n 为 1-based 全局槽位（含序章），映射
      manifest.videos[n-1]（seg_NNN.mp4 文件名，裸名/finals/ 前缀双兼容）——
      前端段落卡片链位即此编号。
    - {"file": f}：先查项目目录（final_* / merged_* / 任意 mp4，finals/优先），
      再查 input 目录（上传外部素材）。f 允许至多两级子目录，
      每段过 safe_name 防穿越；input 命中再以 realpath+startswith 复核。
    """
    if not isinstance(items, list) or not items:
        raise ValueError("合并清单为空（至少勾选一个来源）")
    videos = list(manifest.get("videos") or [])
    sources = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError(f"清单项必须是对象：{it!r}")
        if "seg" in it:
            try:
                n = int(it["seg"])
            except (TypeError, ValueError):
                raise ValueError(f"段号必须是整数：{it.get('seg')!r}") from None
            if not 1 <= n <= len(videos):
                raise ValueError(f"段号 {n} 越界（有效范围 1-{len(videos)}）")
            fname = videos[n - 1]
            if not fname:
                raise ValueError(f"段 {n} 还没有生成视频文件（先跑完该段）")
            path = checkpoint.resolve_project_file(root, fname)
            if not os.path.isfile(path):
                raise ValueError(f"段 {n} 的视频文件缺失：{fname}")
            sources.append(path)
            continue
        f = str(it.get("file") or "").strip().replace("\\", "/")
        parts = [p for p in f.split("/") if p and p != "."]
        if not parts or len(parts) > 2 or not all(safe_name(p) for p in parts):
            raise ValueError(f"非法文件名：{f!r}")
        if not os.path.splitext(parts[-1])[1]:
            raise ValueError(f"文件名缺少扩展名：{f!r}")
        cand = checkpoint.resolve_project_file(root, "/".join(parts))
        if os.path.isfile(cand):
            sources.append(cand)
            continue
        try:
            from folder_paths import get_input_directory
            in_root = os.path.realpath(get_input_directory())
        except Exception:
            in_root = None
        if in_root:
            cand2 = os.path.realpath(os.path.join(in_root, *parts))
            if cand2.startswith(in_root + os.sep) and os.path.isfile(cand2):
                sources.append(cand2)
                continue
        raise ValueError(f"文件不存在（项目目录与 input 目录都没有）：{f}")
    return sources


def merge_project(name, items, fps=24, crf=20):
    """按序合并 -> 项目目录 merged_%Y%m%d_%H%M%S.mp4，返回更新后的 manifest。

    只追加产物与 manifest.merges 记录，不动链、不动 latent、不动段存档；
    画幅按 manifest.params.width/height（缺失回退首个源实际尺寸，不缩放）。
    长耗时编码（PyAV 流式）交由调用方放线程池；这里同步执行便于单测。
    失败抛 ValueError（清单/缺文件）/RuntimeError（编码失败，详情见
    media.last_error），.part 临时文件由 concat_av_mp4 自清理。
    """
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    sources = _merge_sources(root, manifest, items)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_name = f"merged_{stamp}.mp4"
    k = 2
    fdir = checkpoint.finals_dir(root)
    while os.path.exists(os.path.join(fdir, out_name)):
        out_name = f"merged_{stamp}_{k}.mp4"
        k += 1
    params = manifest.get("params") or {}
    try:
        from . import media                  # 包内（ComfyUI 运行时）
    except ImportError:
        import media                         # 顶层导入（无 ComfyUI 的单测环境）
    ok = media.concat_av_mp4(sources, os.path.join(fdir, out_name),
                             width=params.get("width"), height=params.get("height"),
                             fps=fps, crf=crf)
    if not ok:
        raise RuntimeError(f"合并编码失败：{media.last_error}")

    # 编码耗时较长：写回前重读 manifest，只追加 merges，避免覆盖期间
    # 生成主循环落盘的新段进度/成片（残余竞态窗口缩到毫秒级）
    fresh = checkpoint.load_manifest(root)
    if isinstance(fresh, dict):
        manifest = fresh
    manifest.setdefault("merges", []).append({
        "file": f"finals/{out_name}", "items": items, "updated_at": time.time()})
    manifest["updated_at"] = time.time()
    checkpoint.save_manifest(root, manifest)
    return manifest
