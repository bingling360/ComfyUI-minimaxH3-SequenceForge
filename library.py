"""H3 素材库索引层（Library）：一个浏览器 + 四个 scope。

蓝本：Majoor Assets Manager 的 API 形状与交互模型（见
docs/三库重构方案_基于Majoor资产管理器.md）。本模块只做**索引 + 查询 + 文件操作**，
不碰生成逻辑；前端一个浏览器按 scope 切四种视角。

四个 scope：
- `project`  项目资产   h3_projects/<proj>/assets/
- `global`   全局库     user/minimax_h3/library/（内容寻址，asset_store 管）
- `finals`   成片       h3_projects/<proj>/ 下的 videos/finals/merges/clips
- `latent`   latent     h3_projects/<proj>/latents/

设计取舍：
- **不引 SQLite**：索引在内存里（按项目+目录 mtime 做 TTL 缓存），
  素材量级（长链项目几十~几百）用不上 FTS；查询契约与 Majoor 一致，
  将来量大换实现不改接口。
- **元数据 sidecar**：rating / tags / collections 存在
  `<proj>/library_meta.json`（全局库存 `<library_root>/library_meta.json`），
  不动 manifest（manifest 是生成链的单源真相，不该被 UI 元数据污染）。
- 无 torch 依赖，无 ComfyUI 也可单测纯函数；folder_paths 只在函数内延迟导入。
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile

SCOPES = ("project", "global", "finals", "latent")
KINDS = ("image", "video", "audio", "latent")
SCOPE_CN = {"project": "项目资产", "global": "全局库", "finals": "成片", "latent": "latent"}
KIND_CN = {"image": "图片", "video": "视频", "audio": "音频", "latent": "latent"}

EXT_KIND = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".gif": "image", ".bmp": "image", ".jxl": "image",
    ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video", ".avi": "video",
    ".wav": "audio", ".mp3": "audio", ".flac": "audio", ".ogg": "audio", ".m4a": "audio",
    ".pt": "latent", ".safetensors": "latent",
}
MEDIA_KINDS = ("image", "video", "audio")

DEFAULT_PAGE_SIZE = 60
MAX_PAGE_SIZE = 500
ICON_NAME = "_h3_lib_thumbs"
META_NAME = "library_meta.json"

# 索引缓存：dir/scope -> (built_at, items)。TTL 之内直接复用，避免每次开面板都全盘扫。
_INDEX_CACHE = {}
_INDEX_TTL = 3.0


# ---------- 基础工具 ----------

def kind_of(filename) -> str:
    return EXT_KIND.get(os.path.splitext(str(filename or ""))[1].lower(), "")


def normalize_kind(kind) -> str:
    """类别白名单归一（与 asset_store / asset_hub 同口径）。"""
    k = str(kind or "image").strip()
    return k if k in MEDIA_KINDS else "image"


def safe_rel(rel) -> str:
    """相对路径归一：去反斜杠、拒穿越。非法返回空串。"""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p and p != "."]
    if not parts or ".." in parts:
        return ""
    if any((":" in p) or p.startswith(".") for p in parts):
        return ""
    return "/".join(parts)


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default
    return data if isinstance(data, type(default)) else default


def _project_root(name) -> str:
    try:
        from . import checkpoint
    except ImportError:
        import checkpoint
    return os.path.join(checkpoint.projects_root(), str(name or ""))


def _library_root():
    try:
        from . import asset_store
    except ImportError:
        import asset_store
    return asset_store.try_library_root()


# ---------- 元数据 sidecar（rating / tags / collections） ----------

def meta_path(scope, project=None) -> str:
    if scope == "global":
        root = _library_root()
        return os.path.join(root, META_NAME) if root else ""
    if not project:
        return ""
    return os.path.join(_project_root(project), META_NAME)


def load_meta(scope, project=None) -> dict:
    p = meta_path(scope, project)
    if not p:
        return {"items": {}, "collections": []}
    m = _read_json(p, {"items": {}, "collections": []})
    if not isinstance(m.get("items"), dict):
        m["items"] = {}
    if not isinstance(m.get("collections"), list):
        m["collections"] = []
    return m


def save_meta(scope, project, meta) -> dict:
    p = meta_path(scope, project)
    if not p:
        raise ValueError("无法定位元数据路径（scope/project 非法）")
    meta = {"items": meta.get("items") or {}, "collections": meta.get("collections") or [],
            "updated_at": time.time()}
    _atomic_write_json(p, meta)
    return meta


def set_rating(scope, project, item_id, rating) -> dict:
    r = int(rating or 0)
    r = max(0, min(5, r))
    m = load_meta(scope, project)
    ent = m["items"].setdefault(str(item_id), {})
    ent["rating"] = r
    save_meta(scope, project, m)
    return {"id": item_id, "rating": r}


def set_tags(scope, project, item_id, tags) -> dict:
    clean = [str(t).strip()[:24] for t in (tags or []) if str(t).strip()][:16]
    m = load_meta(scope, project)
    ent = m["items"].setdefault(str(item_id), {})
    ent["tags"] = clean
    save_meta(scope, project, m)
    return {"id": item_id, "tags": clean}


def all_meta(project=None) -> dict:
    """合并「项目 sidecar + 全局 sidecar」-> {id: {rating, tags}}。"""
    out = {}
    if project:
        out.update(load_meta("project", project).get("items") or {})
    out.update(load_meta("global", None).get("items") or {})
    return out


def meta_target(item, project=None):
    """写元数据时的落点：全局库条目写全局 sidecar，其余写项目 sidecar。"""
    if isinstance(item, dict) and item.get("scope") == "global":
        return "global", None
    return "project", project


def apply_meta(items, project=None) -> list:
    """把两份 sidecar 的 rating/tags 合进条目。"""
    if not items:
        return items
    table = all_meta(project)
    for e in items:
        e.setdefault("rating", 0)
        rec = table.get(e["id"])
        if isinstance(rec, dict):
            e["rating"] = int(rec.get("rating") or 0)
            e["tags"] = [str(t) for t in (rec.get("tags") or [])]
    return items


def list_collections(project) -> list:
    return load_meta("project", project)["collections"]


def save_collection(project, name, items=None, cid=None) -> dict:
    name = str(name or "").strip()[:60]
    if not name:
        raise ValueError("集合名不能为空")
    m = load_meta("project", project)
    colls = m["collections"]
    if cid:
        for c in colls:
            if str(c.get("id")) == str(cid):
                c["name"] = name
                if items is not None:
                    c["items"] = [str(x) for x in items]
                c["updated_at"] = time.time()
                save_meta("project", project, m)
                return c
        raise ValueError(f"集合不存在：{cid}")
    cid = "c_" + hashlib.sha1(f"{name}|{time.time()}".encode()).hexdigest()[:10]
    c = {"id": cid, "name": name, "items": [str(x) for x in (items or [])],
         "created_at": time.time(), "updated_at": time.time()}
    colls.append(c)
    save_meta("project", project, m)
    return c


def delete_collection(project, cid) -> bool:
    m = load_meta("project", project)
    keep = [c for c in m["collections"] if str(c.get("id")) != str(cid)]
    if len(keep) == len(m["collections"]):
        return False
    m["collections"] = keep
    save_meta("project", project, m)
    return True


# ---------- 索引构建 ----------

def _entry(scope, kind, name, file, abs_path, **extra) -> dict:
    try:
        st = os.stat(abs_path)
        size, mtime = int(st.st_size), float(st.st_mtime)
    except OSError:
        size, mtime = 0, 0.0
    e = {
        "id": f"{scope}:{file}",
        "scope": scope,
        "kind": kind,
        "name": str(name or os.path.basename(file)),
        "file": file,
        "asset_id": "",
        "size": size,
        "mtime": mtime,
        "rating": 0,
        "tags": [],
        "roles": [],
        "refs": [],
        "origin": SCOPE_CN.get(scope, scope),
    }
    e.update(extra)
    return e


def _walk_files(root, sub="", exts=None):
    """递归列出 root/sub 下的文件 -> [(相对 file, 绝对路径)]，忽略隐藏与缩略图缓存目录。"""
    base = os.path.join(root, *([sub] if sub else []))
    out = []
    if not os.path.isdir(base):
        return out
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d != ICON_NAME]
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            if exts and os.path.splitext(fn)[1].lower() not in exts:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            out.append((rel, full))
    return out


def scan_scope(scope, project=None) -> list:
    """扫一个 scope -> 原始条目列表（不含 rating/tags/refs，由 query 阶段填充）。"""
    items = []
    if scope == "global":
        root = _library_root()
        if not root:
            return items
        try:
            from . import asset_store
        except ImportError:
            import asset_store
        mf = asset_store.load_library(root)
        for a in mf.get("assets") or []:
            if not isinstance(a, dict) or not a.get("file"):
                continue
            f = str(a["file"]).replace("\\", "/")
            abs_p = os.path.join(root, *f.split("/"))
            e = _entry("global", str(a.get("kind") or "image"),
                       a.get("orig_name") or os.path.basename(f), f, abs_p,
                       asset_id=str(a.get("asset_id") or ""))
            e["bytes"] = int(a.get("bytes") or 0)
            e["desc"] = str(a.get("desc") or "")
            e["tags"] = [str(t) for t in (a.get("tags") or [])]
            e["created_at"] = float(a.get("created_at") or 0)
            items.append(e)
        return items

    if not project:
        return items
    root = _project_root(project)
    if scope == "project":
        for rel, full in _walk_files(root, "assets"):
            k = kind_of(rel)
            if k not in MEDIA_KINDS:
                continue
            items.append(_entry("project", k, os.path.basename(rel), rel, full))
        items.extend(_link_entries(project, root))
        return items

    if scope == "finals":
        for sub in ("videos", "finals", "merges", "clips"):
            for rel, full in _walk_files(root, sub):
                k = kind_of(rel)
                if k not in MEDIA_KINDS:
                    continue
                label = {"videos": "分段", "finals": "成片",
                         "merges": "合并", "clips": "剪辑"}.get(sub, sub)
                items.append(_entry("finals", k, os.path.basename(rel), rel, full,
                                    origin=f"成片 · {label}"))
        return items

    if scope == "latent":
        for sub in ("latents", "latent"):
            for rel, full in _walk_files(root, sub):
                if kind_of(rel) != "latent":
                    continue
                items.append(_entry("latent", "latent", os.path.basename(rel), rel, full,
                                    origin="latent"))
        return items
    return items


def _link_entries(project, root) -> list:
    """项目链接（manifest.asset_links）→ 「项目资产」条目。

    双层存储的语义是**链接引用不复制**：文件只在全局库一份，项目里只存
    {asset_id, alias}。但「项目资产」视图如果不列链接条目，用户点完「调入项目」
    看不到任何东西，会以为没生效 —— 这里补上（带 linked 标记，前端显示「链接」徽标）。
    """
    out = []
    try:
        from . import checkpoint as _ck
    except ImportError:
        import checkpoint as _ck
    try:
        manifest = _ck.load_manifest(root)
    except Exception:
        return out
    if not isinstance(manifest, dict):
        return out
    links = [x for x in (manifest.get("asset_links") or []) if isinstance(x, dict)
             and x.get("asset_id")]
    if not links:
        return out
    libroot = _library_root()
    libmap = {}
    if libroot:
        try:
            from . import asset_store
        except ImportError:
            import asset_store
        for a in (asset_store.load_library(libroot).get("assets") or []):
            if isinstance(a, dict) and a.get("asset_id"):
                libmap[str(a["asset_id"])] = a
    for L in links:
        aid = str(L["asset_id"])
        g = libmap.get(aid) or {}
        f = str(g.get("file") or "").replace("\\", "/")
        abs_p = os.path.join(libroot, *f.split("/")) if (libroot and f) else ""
        e = _entry("project", normalize_kind(L.get("kind") or g.get("kind")),
                   str(L.get("alias") or g.get("orig_name") or aid), f, abs_p,
                   asset_id=aid, linked=True, origin="项目资产 · 链接全局库")
        e["bytes"] = int(g.get("bytes") or 0)
        e["roles"] = [str(r) for r in (L.get("roles") or [])
                      if str(r) in ("首帧图", "尾帧图")]
        out.append(e)
    return out


def build_index(project=None, scopes=None, use_cache=True) -> list:
    """四 scope 合并索引（带 TTL 缓存）。"""
    scopes = tuple(scopes or SCOPES)
    now = time.time()
    out = []
    for scope in scopes:
        if scope in ("project", "finals", "latent") and not project:
            continue
        ck = f"{scope}|{project or ''}"
        hit = _INDEX_CACHE.get(ck) if use_cache else None
        if hit and (now - hit[0]) < _INDEX_TTL:
            out.extend(hit[1])
            continue
        got = scan_scope(scope, project)
        if use_cache:
            _INDEX_CACHE[ck] = (now, got)
        out.extend(got)
    return out


def invalidate(project=None):
    """清索引缓存（上传/删除/改名后调用；project=None 清全部）。"""
    if project is None:
        _INDEX_CACHE.clear()
        return
    for k in list(_INDEX_CACHE):
        if k.endswith(f"|{project}"):
            _INDEX_CACHE.pop(k, None)


# ---------- 本插件语义：段引用 / 角色标记 ----------

_REF_BRACKET = re.compile(r"\[\[([^\[\]]{1,24})\]\]")
# 负向后顾：`@` 前不能是字母数字下划线，免得把 a@b.com 里的 b 当成素材标签
_REF_AT = re.compile(
    r"(?<![0-9A-Za-z_])@([^\s@\[\]{}<>()（）,，.。;；:：!！?？\"'`|/\\]{1,24})")


def compute_refs(manifest, items) -> dict:
    """按项目 manifest 算每个素材被哪几段引用 -> {标识: [段号]}。

    三种来源与旧前端 segUsage 同口径：段 refs 勾选、提示词里的 `[[标签]]` / `@标签`、
    asset_links 里的角色标记（首帧图/尾帧图算全段引用）。

    段 refs 有两条落点：运行期写的 manifest["segments"][i]["refs"] 与导演台回写的
    manifest["seg_fields"][i]["refs"]（后者才是常写的一条，按全局槽位对齐，与
    prompts 同索引）—— 两处都读，否则「段卡勾了引用、素材库却不显示被引用」。
    """
    if not isinstance(manifest, dict):
        return {}
    usage = {}
    alias_to_item = {}
    for e in items:
        alias_to_item[e["name"]] = e
        if e.get("asset_id"):
            alias_to_item[e["asset_id"]] = e

    def touch(key, seg_no):
        k = str(key or "").strip()
        if not k:
            return
        it = alias_to_item.get(k)
        if it is None:
            return
        if seg_no not in it["refs"]:
            it["refs"].append(seg_no)

    def refs_at(i):
        """第 i 段（全局槽位）勾选的引用：segments + seg_fields 两处合并。"""
        out = []
        s = segs[i] if i < len(segs) and isinstance(segs[i], dict) else {}
        if isinstance(s.get("refs"), list):
            out.extend(s["refs"])
        f = fields[i] if i < len(fields) and isinstance(fields[i], dict) else {}
        if isinstance(f.get("refs"), list):
            out.extend(f["refs"])
        return out

    def tail_at(i):
        s = segs[i] if i < len(segs) and isinstance(segs[i], dict) else {}
        f = fields[i] if i < len(fields) and isinstance(fields[i], dict) else {}
        for src in (s, f):
            ts = src.get("tail_src")
            if isinstance(ts, dict) and ts.get("asset"):
                return ts["asset"]
        return ""

    prompts = manifest.get("prompts") or []
    segs = manifest.get("segments") or []
    fields = manifest.get("seg_fields") or []
    n = max(len(prompts), len(segs), len(fields))
    for i in range(n):
        for r in refs_at(i):
            key = (r.get("asset") or r.get("id") or r.get("label")) if isinstance(r, dict) else r
            touch(key, i + 1)
        ts = tail_at(i)
        if ts:
            touch(ts, i + 1)
        txt = str(prompts[i] if i < len(prompts) else "")
        for lbl in (_REF_BRACKET.findall(txt) + _REF_AT.findall(txt)):
            touch(lbl, i + 1)

    for e in items:
        for role in e.get("roles") or []:
            if role in ("首帧图", "尾帧图") and not e["refs"]:
                e["refs"] = [i + 1 for i in range(n)]
    return usage


def apply_aliases(items, manifest):
    """用工程清单里的别名覆盖显示名。

    索引初建的 name 是**文件名**，但用户在提示词里写的是 `[[别名]]`
    （manifest.assets[].label / asset_links[].alias）。不覆盖的话
    段引用（`[[别名]]`、refs 勾选）全都匹配不上 —— 这里补上。
    """
    if not isinstance(manifest, dict):
        return items
    by_file, by_id = {}, {}
    for a in manifest.get("assets") or []:
        if isinstance(a, dict) and a.get("label") and a.get("file"):
            by_file[str(a["file"]).replace("\\", "/")] = str(a["label"])
    for L in manifest.get("asset_links") or []:
        if isinstance(L, dict) and L.get("asset_id") and L.get("alias"):
            by_id[str(L["asset_id"])] = str(L["alias"])
    for e in items:
        lab = by_id.get(e.get("asset_id") or "")
        if not lab and e["scope"] == "project":
            lab = by_file.get(e["file"])
        if lab:
            e["name"] = lab
    return items


def apply_roles(items, manifest):
    """首帧图/尾帧图角色：manifest.assets 与 asset_links 两处都可能标。"""
    if not isinstance(manifest, dict):
        return items
    roles_by_key = {}
    for a in manifest.get("assets") or []:
        if isinstance(a, dict) and a.get("roles"):
            roles_by_key[str(a.get("label") or "")] = [str(r) for r in a["roles"]]
    for L in manifest.get("asset_links") or []:
        if isinstance(L, dict) and L.get("roles"):
            roles_by_key[str(L.get("alias") or "")] = [str(r) for r in L["roles"]]
    for e in items:
        hit = roles_by_key.get(e["name"])
        if hit:
            e["roles"] = hit
    return items


# ---------- 查询 ----------

_SORT_KEYS = {
    "name": lambda e: str(e.get("name") or "").lower(),
    "mtime": lambda e: float(e.get("mtime") or 0),
    "size": lambda e: int(e.get("size") or 0),
    "kind": lambda e: str(e.get("kind") or ""),
    "rating": lambda e: int(e.get("rating") or 0),
}


def query(items, q="", scope="all", kind="all", sort="mtime", order="desc",
          page=1, page_size=DEFAULT_PAGE_SIZE, collection=None, seg=None,
          min_rating=0) -> dict:
    """过滤 + 排序 + 分页 -> {total, page, page_size, total_pages, items}。

    与 Majoor 的分页契约一致（page 1-based）；min_rating 用于"只看 N 星以上"。
    """
    rows = list(items or [])
    if scope and scope != "all":
        rows = [e for e in rows if e["scope"] == scope]
    if kind and kind != "all":
        if kind == "media":
            rows = [e for e in rows if e["kind"] in MEDIA_KINDS]
        else:
            rows = [e for e in rows if e["kind"] == kind]
    try:
        mr = int(min_rating or 0)
    except (TypeError, ValueError):
        mr = 0
    if mr > 0:
        rows = [e for e in rows if int(e.get("rating") or 0) >= mr]
    if seg:
        try:
            s = int(seg)
            rows = [e for e in rows if s in (e.get("refs") or [])]
        except (TypeError, ValueError):
            pass
    if collection:
        want = set(collection) if isinstance(collection, (list, tuple, set)) else set()
        rows = [e for e in rows if e["id"] in want]
    kw = str(q or "").strip().lower()
    if kw:
        def hit(e):
            blob = " ".join([
                str(e.get("name") or ""), str(e.get("file") or ""),
                str(e.get("origin") or ""), str(e.get("desc") or ""),
                " ".join(e.get("tags") or []), str(e.get("kind") or ""),
            ]).lower()
            return all(t in blob for t in kw.split())
        rows = [e for e in rows if hit(e)]
    key = _SORT_KEYS.get(str(sort or "mtime"), _SORT_KEYS["mtime"])
    rows.sort(key=key, reverse=(str(order or "desc").lower() != "asc"))
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(page_size or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    return {"total": total, "page": page, "page_size": page_size,
            "total_pages": total_pages, "items": rows[start:start + page_size]}


def counters(items) -> dict:
    c = {s: 0 for s in SCOPES}
    for e in items or []:
        c[e["scope"]] = c.get(e["scope"], 0) + 1
    return c


# ---------- 库间调入：目标库同名检测 ----------

# 每个 scope 允许"调入"的目标 scope（**只列跨库方向**，同库内不算重复）
MOVE_TARGETS = {
    "global": ("project",),                 # 全局库 → 调入项目（复制一份进 assets/）
    "project": ("global",),                 # 项目资产 → 存入全局库
    "finals": ("project", "global"),        # 成片 → 调入项目（搬家）+ 存入全局库
    "latent": (),                           # 全局库不收 latent，没有库间动作
}


def name_keys(item) -> set:
    """判定"同名"用的键集：显示名 + 文件名，统一**去扩展名 + 小写**。

    为什么要两个都取、还要去扩展名：

    - 全局库存的是内容寻址文件名（`ab12cd34_阿依.png`），显示名才是 `阿依.png`；
    - 项目资产的显示名被 manifest 别名覆盖成 `阿依`（**没有扩展名**，见 apply_aliases）；
    - 成片/项目未起别名时显示名就是文件名。

    只比文件名或只比显示名都会漏 —— 两边都取、去扩展名再比，才能对上
    「全局库 阿依.png」vs「项目资产 阿依」这种同一份东西。

    另外补一个 **24 字符截断**的键：`manifest.assets[].label` / `asset_links[].alias`
    入库时被截到 24 字（见 mirror_to_project / store_to_project），超长名字截断后
    才是真正写进项目的那个名字，不补这个键就会漏判、让人连点两次复制出两份。
    """
    out = set()
    for raw in (item.get("name"), item.get("file")):
        s = str(raw or "").replace("\\", "/").split("/")[-1].strip().casefold()
        if not s:
            continue
        s = os.path.splitext(s)[0] or s
        if not s:
            continue
        out.add(s)
        out.add(s[:24])
    return out


def blocked_targets(items) -> dict:
    """{item_id: [目标 scope, ...]}：目标库已有同名文件 -> 该方向的调入按钮要隐藏。

    前端只拿到当前页，看不到目标库全貌，所以这个判定必须在后端用**完整索引**做。
    判定两件事：

    1. 同名：目标库任意条目的 `name_keys` 与它相交（去扩展名、忽略大小写）；
    2. 已链接（仅 全局库 → 项目）：项目 `asset_links` 里已有同一 asset_id，
       那就已经在项目里了，不该再复制一份（这条不靠名字，靠 asset_id）。

    注意"同名"是**跨库**概念：同库内本来就可以有同名（不同文件夹），不参与判定。
    """
    rows = [e for e in (items or []) if isinstance(e, dict)]
    keys, ids = {}, {}
    for e in rows:
        sc = e.get("scope")
        ks = keys.setdefault(sc, set())
        ks |= name_keys(e)
        if e.get("asset_id"):
            ids.setdefault(sc, set()).add(str(e["asset_id"]))
    out = {}
    for e in rows:
        sc = e.get("scope")
        hit = []
        for t in MOVE_TARGETS.get(sc) or ():
            if t not in keys:
                continue
            if name_keys(e) & keys[t]:
                hit.append(t)
            elif (sc == "global" and t == "project" and e.get("asset_id")
                  and str(e["asset_id"]) in (ids.get("project") or set())):
                hit.append(t)          # 项目已链接这份：不用再复制一份进 assets/
        out[e["id"]] = hit
    return out


# ---------- 缩略图 ----------

def thumb_path(project, item_id) -> str:
    h = hashlib.sha1(str(item_id).encode("utf-8")).hexdigest()[:16]
    root = _project_root(project) if project else (_library_root() or tempfile.gettempdir())
    return os.path.join(root, ICON_NAME, h + ".jpg")


def make_thumb(src_abs, project, item_id, kind, size=256) -> str:
    """生成/复用缩略图，返回本地路径；失败返回空串（前端回落原图）。

    图片走 Pillow；视频/音频没有 ffmpeg 就跳过（前端用 <video preload=metadata> 预览）。
    """
    if kind != "image" or not src_abs or not os.path.isfile(src_abs):
        return ""
    dst = thumb_path(project, item_id)
    try:
        if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src_abs):
            return dst
    except OSError:
        pass
    try:
        from PIL import Image
    except ImportError:
        return ""
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with Image.open(src_abs) as im:
            im = im.convert("RGB")
            im.thumbnail((size, size))
            tmp = dst + ".part"
            im.save(tmp, "JPEG", quality=82)
            os.replace(tmp, dst)
        return dst
    except Exception:
        return ""


# ---------- 文件操作 ----------

def stage_to_input(src_abs, subdir="h3_staged") -> dict:
    """把素材复制进 ComfyUI 的 input 目录（LoadImage/LoadVideo 只认 input）。

    这是「在画布打开」的底座：原生节点拿不到 output 里的项目素材，
    先 stage 过去再建节点即可。
    """
    if not src_abs or not os.path.isfile(src_abs):
        raise ValueError(f"源文件不存在：{src_abs!r}")
    import folder_paths
    in_dir = folder_paths.get_input_directory()
    subdir = safe_rel(subdir) or "h3_staged"
    dest_dir = os.path.join(in_dir, *subdir.split("/"))
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.basename(src_abs)
    dest = os.path.join(dest_dir, base)
    stem, ext = os.path.splitext(base)
    i = 1
    while os.path.exists(dest) and os.path.getsize(dest) != os.path.getsize(src_abs):
        dest = os.path.join(dest_dir, f"{stem}_{i}{ext}")
        i += 1
    if not os.path.exists(dest):
        shutil.copy2(src_abs, dest)
    return {"staged": f"{subdir}/{os.path.basename(dest)}",
            "abs": dest, "bytes": os.path.getsize(dest)}


def rename_item(src_abs, new_name) -> dict:
    """重命名文件（只改文件名，不动目录）。"""
    new_name = str(new_name or "").strip()
    if not new_name or os.path.sep in new_name or "/" in new_name or ":" in new_name:
        raise ValueError("非法文件名")
    if not src_abs or not os.path.isfile(src_abs):
        raise ValueError("源文件不存在")
    if not os.path.splitext(new_name)[1]:
        new_name += os.path.splitext(src_abs)[1]
    dst = os.path.join(os.path.dirname(src_abs), new_name)
    if os.path.exists(dst) and os.path.abspath(dst) != os.path.abspath(src_abs):
        raise ValueError(f"目标已存在：{new_name}")
    os.replace(src_abs, dst)
    return {"file": os.path.basename(dst)}


def delete_items(paths) -> dict:
    """删除文件（不删目录，不碰 manifest —— 调用方负责同步引用）。"""
    done, skipped = [], []
    for p in paths or []:
        try:
            if p and os.path.isfile(p):
                os.remove(p)
                done.append(os.path.basename(p))
            else:
                skipped.append(os.path.basename(str(p)))
        except OSError as e:
            skipped.append(f"{os.path.basename(str(p))}({e})")
    return {"deleted": done, "skipped": skipped}


def make_zip(paths, project_root=None) -> dict:
    """把给定文件打包成一个 zip（落在项目根 / 全局库的 _h3_zips 下），返回可下载相对路径。"""
    files = [p for p in (paths or []) if p and os.path.isfile(p)]
    if not files:
        raise ValueError("没有可打包的文件")
    base = _project_root(project_root) if project_root else (_library_root() or tempfile.gettempdir())
    out_dir = os.path.join(base, "_h3_zips")
    os.makedirs(out_dir, exist_ok=True)
    name = f"h3lib_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    dest = os.path.join(out_dir, name)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, os.path.basename(p))
    return {"zip": name, "abs": dest, "count": len(files), "bytes": os.path.getsize(dest)}


def mirror_to_project(project, item, label=None) -> dict:
    """全局库条目 → 项目：拷文件进 assets/ **并写 manifest["assets"]**（一步到位）。

    与 routes 里旧 `asset_mirror` 的区别：那个只拷文件，登记要前端再推一遍池子
    （旧三库面板的 persistPool 干的活）。新浏览器没有那一步，所以这里一次做完，
    调完立刻能在「项目资产」里看到、并用 `[[别名]]` 引用。
    """
    try:
        from . import projects as _pj
    except ImportError:
        import projects as _pj
    name = _pj.safe_name(project)
    if not name:
        raise ValueError("无效的项目目录名")
    src = resolve_item_path(item, name)
    if not src:
        raise ValueError("源文件缺失（全局库文件可能已被删）")
    manifest = _pj.read_project(name)
    if manifest is None:
        raise ValueError("项目不存在（先新建项目或跑一段）")
    adir = os.path.join(_project_root(name), "assets")
    os.makedirs(adir, exist_ok=True)
    kind = normalize_kind(item.get("kind"))
    ext = os.path.splitext(src)[1]
    want = str(label or item.get("name") or os.path.basename(src)).strip()
    want = os.path.basename(want)
    if not os.path.splitext(want)[1]:
        want += ext
    cand, k = want, 2
    while os.path.exists(os.path.join(adir, cand)):
        cand = f"{os.path.splitext(want)[0]}_{k}{ext}"
        k += 1
    shutil.copy2(src, os.path.join(adir, cand))
    rel = f"assets/{cand}"
    assets = [dict(a) if isinstance(a, dict) else a for a in (manifest.get("assets") or [])]
    taken = {str(a.get("label")) for a in assets if isinstance(a, dict) and a.get("label")}
    lbl = (str(label or item.get("name") or os.path.splitext(cand)[0])).strip()[:24]
    lbl = lbl or os.path.splitext(cand)[0][:24]
    b, n = lbl, 2
    while lbl in taken:
        lbl = f"{b}{n}"[:24]
        n += 1
    assets.append({"label": lbl, "kind": kind, "file": rel})
    out = _pj.save_assets(name, assets, None)
    if out is None:
        raise ValueError("写入项目清单失败")
    return {"file": rel, "label": lbl, "kind": kind, "revision": out.get("revision")}


def store_to_project(project, src_abs, name=None, kind="image", label=None) -> dict:
    """上传落点为项目：拷进 <proj>/assets/ 并登记 manifest.assets（**不进全局库**）。

    与 mirror_to_project 的区别：源是本地文件（不是全局库条目），也不写 asset_links
    —— 就是"项目自己的一份"。跨项目复用请用显式的「存入全局库」动作（lib_archive），
    不要在上传时顺手复制一份（用户明确反馈过那样"明显不对"）。
    """
    try:
        from . import projects as _pj
    except ImportError:
        import projects as _pj
    proj = _pj.safe_name(project)
    if not proj:
        raise ValueError("无效的项目目录名")
    if not src_abs or not os.path.isfile(src_abs):
        raise ValueError("源文件不存在")
    manifest = _pj.read_project(proj)
    if manifest is None:
        raise ValueError("项目不存在（先新建项目或跑一段）")
    kk = normalize_kind(kind)
    adir = os.path.join(_project_root(proj), "assets")
    os.makedirs(adir, exist_ok=True)
    ext = os.path.splitext(src_abs)[1]
    want = os.path.basename(str(name or os.path.basename(src_abs))).replace("\\", "/").split("/")[-1]
    if not safe_rel(want) or not os.path.splitext(want)[1]:
        want = os.path.basename(src_abs)
    stem, k = os.path.splitext(want)[0], 2
    cand = want
    while os.path.exists(os.path.join(adir, cand)):
        cand = f"{stem}_{k}{ext}"
        k += 1
    shutil.copy2(src_abs, os.path.join(adir, cand))
    rel = f"assets/{cand}"
    assets = [dict(a) if isinstance(a, dict) else a for a in (manifest.get("assets") or [])]
    taken = {str(a.get("label")) for a in assets if isinstance(a, dict) and a.get("label")}
    lbl = (str(label or "").strip() or os.path.splitext(cand)[0])[:24] or "素材"
    base, n = lbl, 2
    while lbl in taken:
        lbl = f"{base}{n}"[:24]
        n += 1
    assets.append({"label": lbl, "kind": kk, "file": rel})
    out = _pj.save_assets(proj, assets, None)
    if out is None:
        raise ValueError("写入项目清单失败")
    return {"file": rel, "label": lbl, "kind": kk, "revision": out.get("revision")}


def store_to_finals(project, src_abs, name=None) -> dict:
    """把文件拷进项目 finals/（成片库）：同名加 _2 不覆盖，返回 {file, name}。

    上传落点为「成片」时用：成片库是目录扫描型（不走 manifest），拷进去即可见。
    """
    proj = str(project or "").strip()
    if (not proj or proj.startswith(".") or ".." in proj
            or "/" in proj or "\\" in proj or ":" in proj):
        raise ValueError("无效的项目目录名")
    if not src_abs or not os.path.isfile(src_abs):
        raise ValueError("源文件不存在（入库后的全局库文件缺失）")
    d = os.path.join(_project_root(proj), "finals")
    os.makedirs(d, exist_ok=True)
    want = str(name or os.path.basename(src_abs)).replace("\\", "/").split("/")[-1]
    if not safe_rel(want) or "/" in want or not os.path.splitext(want)[1]:
        want = os.path.basename(src_abs)
    stem, ext = os.path.splitext(want)
    cand, k = want, 2
    while os.path.exists(os.path.join(d, cand)):
        cand = f"{stem}_{k}{ext}"
        k += 1
    shutil.copy2(src_abs, os.path.join(d, cand))
    return {"file": f"finals/{cand}", "name": cand}


def resolve_item_path(item, project=None) -> str:
    """条目 -> 存在的绝对路径（不存在返回空串）。

    linked 条目（项目链接 asset_links）文件在全局库，按全局库根解析 —— 否则
    缩略图/预览/删除都会去找 <proj>/images/… 而落空。
    """
    if not isinstance(item, dict):
        return ""
    scope = item.get("scope")
    rel = safe_rel(item.get("file"))
    if not rel:
        return ""
    if scope == "global" or item.get("linked"):
        root = _library_root()
        cand = os.path.join(root, *rel.split("/")) if root else ""
    else:
        cand = os.path.join(_project_root(project), *rel.split("/"))
    return cand if cand and os.path.isfile(cand) else ""
