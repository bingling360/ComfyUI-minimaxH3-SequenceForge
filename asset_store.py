"""H3 资产存储（AssetStore，P0）：全局库 + 项目链接双层 + 编译期子集解析。

目标（改造计划 P0）：资产库 / 成片库 / latent 库三分法与 manifest 单源真相保留；
砍掉“靠节点连线 + JSON 字符串传资产”的实现，换成高星项目验证过的路线——
服务端资产存储 + 稳定 ID + 编译期子集解析 + 冻结单线工作流。

- 全局库（抄 ethanfel/Guide Reference Sheets）：ComfyUI/user 下内容寻址存储，
  manifest 含 asset_id + sha256 + kind + 相对路径 + tags + 描述，与原始
  工作流 / 文件名解耦，跨项目链接引用不复制。
- 项目库（现状 assets/ 原地升级）：项目 manifest 存 asset_links
  [{asset_id, alias}]，alias 是项目内显示别名（<=24 字符）；旧
  manifest["assets"]（{label,kind,file}）零改动可读，转译层按 label->ID 解析。
- 编译器 compile_refs：段 refs（asset_id 或 alias 混排）-> 按类型独立编号的
  <Picture k>/<Video k>/<Audio j> + 9/3/3 卡点 + 未知引用点名。供执行期与
  /h3chain/compile_refs 干跑接口共用（前端切段即时显示真实 tag，不用排队才知道错）。

无 torch 依赖，无 ComfyUI 也可单测纯函数。folder_paths / checkpoint 只在函数
内延迟导入，import 期零副作用（沿用 asset_hub.py 的可测传统）。
"""

import hashlib
import json
import os
import re
import tempfile
import time
import uuid

SCHEMA = "h3/asset-library-v1"
KINDS = ("image", "video", "audio")
KIND_CN = {"image": "图片", "video": "视频", "audio": "音频"}
REF_CAPS = {"image": 9, "video": 3, "audio": 3}
ALIAS_MAX = 24
# 别名里一律不允许的字符（空白与标点）：`@别名` 的解析按它们断句，留着就是死引用。
_ALIAS_BAD = re.compile(r"[^\w\-]+")
# 只剥这些真媒体扩展名；`v1.0` / `2.5` 之类不在名单里的尾巴保持原样（再被 `_ALIAS_BAD` 压成 `_`）。
_MEDIA_EXT = (
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif", "heic", "avif",
    "mp4", "mov", "webm", "mkv", "avi", "wmv", "flv", "m4v",
    "wav", "mp3", "ogg", "flac", "m4a", "aac", "opus",
)
_TOKEN_FMT = {"image": "<Picture {}>", "video": "<Video {}>", "audio": "<Audio {}>"}


def safe_name(name) -> str:
    """与 projects.safe_name 同语义（本地复刻，避免循环导入）。"""
    s = str(name or "").strip()
    if not s or s.startswith(".") or "/" in s or "\\" in s or ":" in s or ".." in s:
        return ""
    return s


def normalize_kind(kind) -> str:
    k = str(kind or "image").strip()
    return k if k in KINDS else "image"


def clean_alias(alias) -> str:
    """别名归一：**唯一权威**（前后端同一规则，前端 web/h3_director.js::cleanLabel）。

    别名是 `@别名` 引用语法的载体，而引用解析按空白/标点断句（后端 `_REF_AT`
    在空格处就停、连 `.` `()` 都不认）。所以别名里不能有空白、括号、点号、
    逗号等任何标点 —— 否则"提示词里看着有引用，编译时说找不到标签"。

    规则：取文件名 -> 剥媒体扩展名（白名单，`v1.0` 这类不动扩展名语义） ->
    非法字符压成 `_` -> 折叠连续 `_-` -> 截 ALIAS_MAX。保留中英文数字、下划线、
    连字符、汉字与假名（`\\w` 的 unicode 语义）。
    """
    s = str(alias or "").strip().replace("\\", "/").split("/")[-1]
    stem, ext = os.path.splitext(s)
    if ext.lstrip(".").lower() in _MEDIA_EXT:
        s = stem
    s = _ALIAS_BAD.sub("_", s)
    s = re.sub(r"[_\-]{2,}", "_", s).strip("_-")
    s = s[:ALIAS_MAX].strip("_-")
    # 别名不能长成「标注」形态（图片1 / 视频2 / 音频3）：标注是发给 LLM 的稳定编号，
    # 与别名同形时，`@图片1` 到底指别名还是标注无法判定 —— 编码/解码会串号。
    # 统一加一个尾下划线错开（`图片1` -> `图片1_`），语义不变、可见、可改名。
    if s and _MARK_RE.match(s):
        s = (s + "_")[:ALIAS_MAX]
    return s


def alias_of(*cands) -> str:
    """用候选串（用户给的 label / 库内显示名 / 文件名 stem）挑一个别名并归一。

    别名一律来自文件名，而文件名里的扩展名、空格、括号都进不了 `@别名` 语法
    （见 clean_alias），所以**落库那一刻**就得归一，而不是留给显示层去圆。
    """
    for c in cands:
        a = clean_alias(c)
        if a:
            return a
    return "素材"


# ---- 素材标注（mark）----
#
# 标注 ≠ 别名。别名（alias）是 `@别名` 引用语法的载体，用户可见可编辑；标注是
# **发给 LLM 用的稳定短编号**（图片1 / 视频2 / 音频1），只在「提示词 ⇄ LLM」这一跳
# 上代替原文名出现：长文件名进 LLM 既费 token 又容易被抄错。
#
# 与官方 <Picture N> **不是一回事**（那是执行期按"本段挂载顺序"重算的 token，
# 见 compile_refs）—— 某段只引用「图片3」时它仍须编译成 <Picture 1>。
# 两层编号必须分离，把标注当 token 用会导致挂载数与编号对不上（模型收不到图）。

MARK_MAX = 999
MARK_KINDS = ("image", "video", "audio")
_MARK_RE = re.compile(r"^(图片|视频|音频)(\d{1,3})$")


def mark_shaped(text) -> bool:
    """字符串是否是标注形态（图片1 / 视频12 / 音频3）。"""
    return bool(_MARK_RE.match(str(text or "").strip()))


def clean_mark(mark) -> str:
    """标注归一：只认「图片N / 视频N / 音频N」，非法一律返回 ""（由调用方发新号）。"""
    m = _MARK_RE.match(str(mark or "").strip())
    if not m:
        return ""
    n = int(m.group(2))
    if n < 1 or n > MARK_MAX:
        return ""
    return f"{m.group(1)}{n}"


def next_mark(items, kind) -> str:
    """同类下一个可用标注 = **最小空闲序号**（图片1 → 图片2 → 图片3 …）。

    取"最小空闲"而不是"最大 +1"：手动把某个素材标成「图片9」时，不该把整条
    自动序列顶到 10（新素材照样拿空缺的 3）。
    复用小号是安全的 —— 标注**只活在"提示词 ⇄ LLM"这一跳**，从不落盘进
    `prompts`（盘上永远是 `@别名`），所以历史文本里不会残留旧号。
    """
    k = normalize_kind(kind)
    prefix = KIND_CN[k]
    used = set()
    for x in (items or []):
        if not isinstance(x, dict):
            continue
        m = _MARK_RE.match(str(x.get("mark") or "").strip())
        if m and m.group(1) == prefix:
            used.add(int(m.group(2)))
    n = 1
    while n in used and n < MARK_MAX:
        n += 1
    return f"{prefix}{n}"


def assign_marks(items) -> bool:
    """就地为缺 mark 的条目按**数组序**补号（幂等）。返回是否有改动。

    顺序即"进项目库的顺序"（`_dedupe_assets` 已保证保持入库序、`asset_links`
    是增量追加），所以标注天然与入库顺序一一对应。手动改过的条目带
    `mark_auto=False`，本函数不碰。

    **不写 `mark_auto=True`**：落盘口径是「缺省即自动、只有手动才存 False」，
    这样字段出现时机确定（不会出现"首次 True、二次消失"这种同义不同形的漂移）。
    """
    changed = False
    for x in (items or []):
        if not isinstance(x, dict):
            continue
        if clean_mark(x.get("mark")):
            continue
        x["mark"] = next_mark(items, x.get("kind") or "image")
        changed = True
    return changed


def mark_taken(items, mark, exclude_id=None, exclude_label=None) -> bool:
    """标注是否已被别的条目占用（手动改标注时查重）。"""
    want = clean_mark(mark)
    if not want:
        return False
    for x in (items or []):
        if not isinstance(x, dict):
            continue
        if exclude_id and str(x.get("asset_id") or "") == str(exclude_id):
            continue
        if exclude_label and str(x.get("alias") or x.get("label") or "") == str(exclude_label):
            continue
        if clean_mark(x.get("mark")) == want:
            return True
    return False



def asset_id_for_content(sha_hex: str) -> str:
    """内容寻址 ID：sha256 hex -> a_<12>。"""
    h = str(sha_hex or "").strip().lower()
    if len(h) < 12 or any(c not in "0123456789abcdef" for c in h[:12]):
        raise ValueError(f"非法 sha：{sha_hex!r}")
    return "a_" + h[:12]


def asset_id_for_legacy(label, kind, file) -> str:
    """旧资产 {label,kind,file} -> 稳定 ID（sha1(label|kind|file)，迁移幂等）。"""
    blob = "|".join([clean_alias(label), normalize_kind(kind),
                     str(file or "").strip().replace("\\", "/")])
    return "a_" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def new_asset_id() -> str:
    return "a_" + uuid.uuid4().hex[:12]


# ---- 全局库路径 / manifest ----

def library_root(user_root=None) -> str:
    """全局库根目录。user_root 可注入（单测用）；否则延迟找 folder_paths。"""
    if user_root:
        root = os.path.join(str(user_root), "minimax_h3", "library")
        os.makedirs(root, exist_ok=True)
        return root
    try:
        from folder_paths import get_user_directory  # type: ignore
        base = get_user_directory()
    except Exception:
        base = None
    if not base:
        try:
            from folder_paths import get_output_directory  # type: ignore
            base = os.path.join(os.path.dirname(get_output_directory()), "user", "default")
        except Exception:
            raise ValueError("无法定位 user 目录（folder_paths 不可用时请显式传 user_root）")
    root = os.path.join(base, "minimax_h3", "library")
    os.makedirs(root, exist_ok=True)
    return root


def _atomic_write_json(path: str, data: dict):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_library(root: str) -> dict:
    path = os.path.join(root, "manifest.json")
    if not os.path.isfile(path):
        return {"schema": SCHEMA, "assets": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
    except Exception:
        return {"schema": SCHEMA, "assets": []}
    if not isinstance(m, dict):
        return {"schema": SCHEMA, "assets": []}
    if not isinstance(m.get("assets"), list):
        m["assets"] = []
    m["schema"] = SCHEMA
    return m


def save_library(root: str, manifest: dict) -> dict:
    manifest = dict(manifest or {})
    manifest["schema"] = SCHEMA
    manifest["updated_at"] = time.time()
    os.makedirs(root, exist_ok=True)
    _atomic_write_json(os.path.join(root, "manifest.json"), manifest)
    return manifest


def register_content(root: str, src_path: str, kind, tags=None, desc="",
                     orig_name=None) -> dict:
    """文件入库全局库：sha256 内容寻址拷贝 + manifest 登记（秒传：同 sha 直接返回）。

    返回 entry {asset_id, sha256, kind, file, orig_name, bytes, ...}。
    kind 目录：images/videos/audios。只做拷贝与登记，不做探针（探针是 P1 后台任务的事）。
    orig_name：入库显示名（缺省取源文件名；multipart 中转的是随机临时名，
    必须显式传真实文件名，否则库内/调入/转码全链路都被 h3lib_ 前缀污染）。
    """
    kind = normalize_kind(kind)
    if not src_path or not os.path.isfile(src_path):
        raise ValueError(f"源文件不存在：{src_path!r}")
    sub = {"image": "images", "video": "videos", "audio": "audios"}[kind]
    h = hashlib.sha256()
    with open(src_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    aid = asset_id_for_content(sha)
    raw_base = str(orig_name or os.path.basename(src_path))
    raw_base = raw_base.replace("\\", "/").split("/")[-1]
    base = raw_base if safe_name(raw_base) and os.path.splitext(raw_base)[1] \
        else f"file{os.path.splitext(raw_base)[1] or '.bin'}"
    stored = f"{sha[:12]}_{base}"
    dest = os.path.join(root, sub, stored)
    manifest = load_library(root)
    for e in manifest["assets"]:
        if not (isinstance(e, dict) and e.get("asset_id") == aid):
            continue
        old_abs = os.path.join(root, *str(e.get("file") or "").replace("\\", "/").split("/")) \
            if e.get("file") else ""
        if old_abs and os.path.isfile(old_abs):
            return e          # 秒传：同内容 + 文件确实在
        if not old_abs:
            manifest["assets"].remove(e)      # 无 file 的坏条目：摘掉，往下重建一条
            break
        # 幽灵修复：登记在、文件没了（老版本删除只 os.remove 不摘登记；新版 scan_scope
        # 按 isfile 过滤，条目看不见）。若在这里"秒传"返回，用户重新上传同一份文件会
        # 拿回一条永远看不见的幽灵 —— 就是"传了没反应"。复用原路径补齐文件并刷新登记，
        # 而不是新开一条同 asset_id 的条目（两条同 id 会让注册表首胜口径变得不可预测）。
        os.makedirs(os.path.dirname(old_abs), exist_ok=True)
        tmp = old_abs + ".part"
        with open(src_path, "rb") as fi, open(tmp, "wb") as fo:
            fo.write(fi.read())
        os.replace(tmp, old_abs)
        now = time.time()
        e["bytes"] = os.path.getsize(old_abs)
        e["orig_name"] = raw_base
        e["updated_at"] = now
        save_library(root, manifest)
        return e
    os.makedirs(os.path.join(root, sub), exist_ok=True)
    if not os.path.isfile(dest):
        tmp = dest + ".part"
        with open(src_path, "rb") as fi, open(tmp, "wb") as fo:
            fo.write(fi.read())
        os.replace(tmp, dest)
    now = time.time()
    entry = {"asset_id": aid, "sha256": sha, "kind": kind,
             "file": f"{sub}/{stored}", "orig_name": raw_base,
             "bytes": os.path.getsize(dest), "tags": [str(t) for t in (tags or [])][:16],
             "desc": str(desc or "")[:500], "created_at": now, "updated_at": now}
    manifest["assets"].append(entry)
    save_library(root, manifest)
    return entry


def remove_asset(root: str, asset_id=None, file=None) -> dict:
    """从全局库 manifest 摘掉一条登记（幂等）-> {removed, remaining}。

    **只改 manifest，不碰磁盘**：物理文件由调用方按需删。顺序反过来（先删文件、
    再摘登记）中间失败就会留下"文件没了但条目还在"的幽灵，而 `scan_scope("global")`
    恰恰以 manifest 为唯一源 —— 幽灵条目会永远显示在全局库里，点删除也删不掉
    （删的是已经不在的文件，条目照旧）—— 这就是"全局库根本删除不了"的真身。
    所以宁可能留一个孤儿文件，也不留幽灵条目。

    匹配口径：asset_id 优先，其次 file（相对路径，与 manifest 里存的一致）。
    """
    aid = str(asset_id or "").strip()
    rel = str(file or "").strip().replace("\\", "/")
    if not root or (not aid and not rel):
        return {"removed": None, "remaining": 0}
    manifest = load_library(root)
    kept, removed = [], None
    for e in manifest.get("assets") or []:
        if not isinstance(e, dict):
            continue
        if removed is None and (
                (aid and str(e.get("asset_id") or "") == aid)
                or (rel and str(e.get("file") or "").replace("\\", "/") == rel)):
            removed = e
            continue
        kept.append(e)
    if removed is None:
        return {"removed": None, "remaining": len(kept)}
    manifest["assets"] = kept
    save_library(root, manifest)
    return {"removed": removed, "remaining": len(kept)}


# ---- 注册表（全局 + 项目链接 + 旧资产三源汇合） ----

def migrate_legacy_assets(legacy_assets) -> list:
    """旧 manifest["assets"] -> 项目链接 [{asset_id, alias, kind, legacy_file}]。

    纯函数：按 label 去重（首个为准，与 _dedupe_assets 同口径），ID 稳定可重复跑。
    """
    out, seen = [], set()
    for a in legacy_assets or []:
        if not isinstance(a, dict):
            continue
        alias = clean_alias(a.get("label"))
        kind = normalize_kind(a.get("kind"))
        f = str(a.get("file") or "").strip().replace("\\", "/")
        if not alias or not f or alias in seen:
            continue
        seen.add(alias)
        out.append({"asset_id": asset_id_for_legacy(alias, kind, f),
                    "alias": alias, "kind": kind, "legacy_file": f})
    return out


def build_registry(global_assets=None, project_links=None, legacy_assets=None) -> dict:
    """三源汇合 -> {by_id, by_alias}，冲突时首个为准并记 warnings。

    by_id: asset_id -> {asset_id, alias, kind, origin, file?}
    by_alias: alias/label -> 同上（旧 label 与新 alias 同一命名空间）。
    """
    by_id, by_alias, warnings = {}, {}, {}

    def _put(ent, origin):
        aid = str(ent.get("asset_id") or "")
        if not aid:
            return
        alias = clean_alias(ent.get("alias") or ent.get("label") or "")
        kind = normalize_kind(ent.get("kind"))
        if aid not in by_id:
            rec = {"asset_id": aid, "alias": alias, "kind": kind, "origin": origin}
            for k in ("file", "legacy_file"):
                if ent.get(k):
                    rec[k] = ent[k]
            by_id[aid] = rec
        else:
            # 同 id 多条目（全局 + 项目链接）：记录首胜，但缺失的别名/文件仍回填，
            # 别名命名空间同样注册（label/alias/id 三者任一可查到同一资产）。
            rec = by_id[aid]
            if alias and not rec.get("alias"):
                rec["alias"] = alias
            for k in ("file", "legacy_file"):
                if ent.get(k) and not rec.get(k):
                    rec[k] = ent[k]
        if alias:
            if alias not in by_alias:
                by_alias[alias] = rec
            elif by_alias[alias] is not rec:
                warnings.setdefault("dup_alias", []).append(alias)

    for e in global_assets or []:
        if isinstance(e, dict):
            _put(e, "global")
    for e in project_links or []:
        if isinstance(e, dict):
            _put(e, "project")
    for e in migrate_legacy_assets(legacy_assets):
        _put(e, "legacy")
    # P3 富化：有 asset_id 但无 file 的记录（项目链接）从全局库回填 file，
    # 使 resolve_absolute 能命中全局库绝对路径；origin 记 linked 以便排查。
    for aid, rec in by_id.items():
        if rec.get("file") or rec.get("legacy_file"):
            continue
        for e in global_assets or []:
            if isinstance(e, dict) and str(e.get("asset_id") or "") == aid and e.get("file"):
                rec["file"] = e["file"]
                rec["origin"] = "linked"
                break
    return {"by_id": by_id, "by_alias": by_alias, "warnings": warnings}


def _ref_key(ref) -> tuple:
    """段引用元素 -> (key, use)。dict 支持 {asset|id|label, use|role}，str 为 alias/id。"""
    return ref_key(ref)


def ref_key(ref) -> tuple:
    """_ref_key 的公开别名（nodes 执行期解析 dict 引用用）。"""
    if isinstance(ref, dict):
        key = ref.get("asset", ref.get("id", ref.get("label", "")))
        use = ref.get("use", ref.get("role", ""))
        return str(key or "").strip(), str(use or "").strip()
    return str(ref or "").strip(), ""


def compile_refs(registry: dict, refs, seg_no: int = 1) -> dict:
    """段引用编译 -> {ok, blocks, tag_map, errors}。

    blocks 保序：[{asset_id, alias, kind, token, use?}]，token 按类型独立 1-based
    （与 nodes._kind_tokens 同规则）；单段 9/3/3 超限与未知引用返回 errors
    （code 复用 E_REF_UNKNOWN / E_MEDIA_LIMIT，前端可沿用旧文案）。
    """
    by_id = (registry or {}).get("by_id") or {}
    by_alias = (registry or {}).get("by_alias") or {}
    errors, order = [], []
    seen = {}          # asset_id -> order 下标：同一素材重复引用只编号一次
    for r in refs or []:
        key, use = _ref_key(r)
        if not key:
            continue
        rec = by_id.get(key) or by_alias.get(key)
        if rec is None:
            errors.append({"code": "E_REF_UNKNOWN",
                           "message": f"段{seg_no} 引用未知资产「{key}」"})
            continue
        # 同一素材在一段内可被引用多次（refs 里出现 N 次 = 正文里写 N 次 @别名）：
        # 编号仍只占一个 <Picture k>，重复项只累加次数，不新开编号。
        hit = seen.get(rec["asset_id"])
        if hit is None:
            seen[rec["asset_id"]] = len(order)
            order.append((rec, use))
        else:
            rec0, use0 = order[hit]
            if not use0 and use:
                order[hit] = (rec0, use)
    counts = {}
    for rec, _use in order:
        counts[rec["kind"]] = counts.get(rec["kind"], 0) + 1
    for k, cap in REF_CAPS.items():
        if counts.get(k, 0) > cap:
            picked = [rec["alias"] or rec["asset_id"] for rec, _ in order if rec["kind"] == k]
            errors.append({"code": "E_MEDIA_LIMIT",
                           "message": f"段{seg_no} 引用{KIND_CN[k]}素材 {len(picked)} 个，"
                                      f"超过官方单段上限 {cap} 个（{picked}）"})
            break
    blocks, tag_map, counters = [], {}, {k: 0 for k in KINDS}
    for rec, use in order:
        counters[rec["kind"]] = counters.get(rec["kind"], 0) + 1
        tok = _TOKEN_FMT[rec["kind"]].format(counters[rec["kind"]])
        b = {"asset_id": rec["asset_id"], "alias": rec["alias"],
             "kind": rec["kind"], "token": tok}
        if use:
            b["use"] = use
        blocks.append(b)
        tag_map[rec["alias"] or rec["asset_id"]] = tok
        tag_map[rec["asset_id"]] = tok
    return {"ok": not errors, "blocks": blocks, "tag_map": tag_map, "errors": errors}


def compile_segments(registry: dict, segments) -> dict:
    """多段干跑 -> {ok, segs: [compile_refs...], errors}（segments[i] 可为 refs 数组或 {refs}）。"""
    segs, all_errs = [], []
    for i, seg in enumerate(segments or []):
        refs = seg.get("refs") if isinstance(seg, dict) else seg
        if not isinstance(refs, list):
            refs = []
        r = compile_refs(registry, refs, i + 1)
        segs.append(r)
        all_errs.extend(r["errors"])
    return {"ok": not all_errs, "segs": segs, "errors": all_errs}


def describe_resolution(rec: dict) -> dict:
    """执行期寻址描述：全局 file vs 旧 legacy_file（绝对路径映射见 resolve_absolute）。"""
    if not isinstance(rec, dict):
        return {}
    out = {"asset_id": rec.get("asset_id"), "alias": rec.get("alias"),
           "kind": normalize_kind(rec.get("kind"))}
    if rec.get("file"):
        out["global_file"] = rec["file"]
    if rec.get("legacy_file"):
        out["legacy_file"] = rec["legacy_file"]
    return out


def try_library_root():
    """library_root 的永不抛版本（执行期用）：定位失败返回 None，调用方走兼容路径。"""
    try:
        return library_root()
    except Exception:
        return None


def build_execution_registry(pool_files, asset_links=None, global_assets=None) -> dict:
    """执行期注册表：pool [(kind, label, file[, ...])] 转 legacy + 项目链接 + 全局库。

    nodes 主节点在组装期（校验）与加载期（寻址）各调一次，输入相同则结果一致。
    """
    legacy = []
    for ent in pool_files or []:
        try:
            k, lbl, fn = ent[0], ent[1], ent[2]
        except (TypeError, ValueError, IndexError):
            continue
        if lbl and fn:
            legacy.append({"label": lbl, "kind": k, "file": fn})
    return build_registry(global_assets, asset_links, legacy)


def resolve_absolute(rec_or_file, project_root=None, library_root_dir=None) -> str | None:
    """三级寻址 -> 存在的绝对路径，找不到返回 None（调用方走旧兼容路径，报错口径不变）。

    查找序：项目内旧路径（legacy_file 在 project_root 下存在）-> 全局库文件
    （library_root_dir/<file> 存在）-> None。只做存在性门控，不抛错。
    纯字符串输入视为旧相对路径，只查项目内一级。
    """
    def _join_isfile(base, rel):
        try:
            parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p and p != "."]
            if not parts:
                return None
            cand = os.path.join(base, *parts)
            return cand if os.path.isfile(cand) else None
        except Exception:
            return None

    if isinstance(rec_or_file, dict):
        rec = rec_or_file
        if project_root and rec.get("legacy_file"):
            hit = _join_isfile(project_root, rec["legacy_file"])
            if hit:
                return hit
        if library_root_dir and rec.get("file"):
            hit = _join_isfile(library_root_dir, rec["file"])
            if hit:
                return hit
        return None
    if project_root and rec_or_file:
        return _join_isfile(project_root, rec_or_file)
    return None
