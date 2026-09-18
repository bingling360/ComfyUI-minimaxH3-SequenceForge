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
# 引用名（@全名）上限：比别名宽松得多 —— 引用名要**含格式后缀**且不能砍掉尾巴
# （两个素材常常只差尾部几位），截断只在极端长名时兜底，且保尾不保头。
REF_NAME_MAX = 96
# 别名里一律不允许的字符（空白与标点）：`@别名` 的解析按它们断句，留着就是死引用。
_ALIAS_BAD = re.compile(r"[^\w\-]+")
# 引用名允许的字符集：**比别名多点号**（`猫.png` 的后缀是引用名的一部分）。
# 解析靠池内最长前缀精确匹配，点号不会像别名那样造成断句歧义。
_REF_NAME_BAD = re.compile(r"[^\w.\-]+")
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
    return s[:ALIAS_MAX].strip("_-")


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


def clean_ref_name(name) -> str:
    """引用名归一：**提示词里 `@` 后面写的那个名字**（含格式后缀）。

    与别名的区别只有一条：**保留扩展名**。历史上引用名就是别名（stem），于是
    用户/LLM 写 `@猫.png` 时分词器只能匹配到 `猫`，剩下的 `.png` 当普通文本留在
    正文里 —— 绿框后面挂个裸后缀（"奇怪的后缀溢出"）。引用名带后缀后，
    `@猫.png` 整体命中池内条目，后缀不再溢出。

    规则：取文件名 -> 空白/括号等非法字符压成 `_`（**保留 `.` `_` `-`**）-> 折叠
    连续 `_-` 与 `..` -> 去首尾 `._-` -> 超长时**保尾**（尾巴是两条同名素材的
    唯一区分点，砍头不砍尾）。
    """
    s = str(name or "").strip().replace("\\", "/").split("/")[-1]
    if not s:
        return ""
    s = _REF_NAME_BAD.sub("_", s)
    s = re.sub(r"[_\-]{2,}", "_", s)
    s = re.sub(r"\.{2,}", ".", s)
    s = s.strip("._-")
    if len(s) > REF_NAME_MAX:
        s = s[:REF_NAME_MAX - 12] + s[-12:]
        s = s.strip("._-")
    return s


def ref_name_of(*cands) -> str:
    """挑一个引用名并归一（候选串通常是原始文件名 orig_name / 落盘文件名）。

    候选顺序由调用方给：显式 ref_name > 原始文件名 > 落盘文件名的 basename
    > 别名（别名没有后缀，是最后兜底 —— 那种情况下引用名不带后缀，但至少能用）。
    """
    for c in cands:
        r = clean_ref_name(c)
        if r:
            return r
    return ""


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


def assign_marks(entries) -> list:
    """给条目列表补标注（`mark`）：**已有的不动，缺的按类型独立、列表顺序递增**。

    标注是给 LLM 看的短名（图片1 / 视频1 / 音频1）—— LLM 看见 `微信图片_2026….png`
    这种长名很容易抄错，看见 `图片1` 不会。

    **编号不回收**：删掉的号留空，新条目从"当前最大序号 +1"接着编。回收会让旧
    提示词里的 `@图片2` 悄悄指向另一张素材 —— 比留个空号危险得多。
    """
    out = []
    seq = {"图片": 0, "视频": 0, "音频": 0}
    for e in entries or []:
        m = re.fullmatch(r"(图片|视频|音频)(\d+)", str((e or {}).get("mark") or "").strip())
        if m:
            seq[m.group(1)] = max(seq.get(m.group(1), 0), int(m.group(2)))
    for e in entries or []:
        ent = dict(e or {})
        if not str(ent.get("mark") or "").strip():
            k = KIND_CN.get(normalize_kind(ent.get("kind")), "图片")
            seq[k] = seq.get(k, 0) + 1
            ent["mark"] = f"{k}{seq[k]}"
        out.append(ent)
    return out


def _replace_at_tokens(text, mapping) -> str:
    """按池内最长前缀把 `@key` 换成 `@映射值`（key 不含 `@`）。

    encode / decode 共用一份实现：两边都是"把 @后面那段名字换掉"，只是映射方向
    相反。最长优先是必须的 —— 否则 `图片1` 会吃掉 `图片10` 的前缀。
    """
    s = str(text or "")
    if not s or "@" not in s or not mapping:
        return s
    keys = sorted((str(k) for k in mapping if k), key=len, reverse=True)
    out, i = [], 0
    while i < len(s):
        # 注意：i == 0 时 s[i-1] 在 Python 里是**最后一个字符**（负索引！），
        # 会把"开头就是 @引用"误判成前面有字母数字 → 该替换的没替换。
        prev = s[i - 1] if i > 0 else ""
        if s[i] == "@" and not re.match(r"[0-9A-Za-z_]", prev):
            hit = next((k for k in keys if s.startswith(k, i + 1)), None)
            if hit:
                out.append("@" + str(mapping[hit]))
                i += len(hit) + 1
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def encode_marks(text, name_to_mark) -> str:
    """正文里的 `@素材全名` → `@标注`（**发给 LLM 之前**用它）。

    LLM 看见的是 `图片1` 这种短而稳的名字，不会把 `微信图片_20260730…png`
    这种长名抄错，也不会凭空造出一个不存在的素材名。返回时用 decode_marks
    把它译回真名。
    """
    return _replace_at_tokens(text, name_to_mark or {})


def decode_marks(text, mark_to_name) -> str:
    """LLM 返回的 `@标注` → `@素材全名`（写回提示词框之前用它）。

    译不出来的标注（模型造了个 `@图片99`）**原样保留** —— 前端会把它渲染成
    红框（悬空引用），而不是静默丢掉。
    """
    return _replace_at_tokens(text, mark_to_name or {})


def mark_of(kind, seq) -> str:
    """标注文本：图片1 / 视频1 / 音频1（按类型独立编号）。"""
    return f"{KIND_CN.get(normalize_kind(kind), '图片')}{max(1, int(seq))}"


def next_mark_seq(kind, used_marks) -> int:
    """下一个可用序号（**编号不回收**：删掉的号留空，新增继续递增）。

    为什么不回收：提示词里写的是 `@图片2`，如果删掉 2 号素材后把后面的整体前移，
    旧提示词就会悄悄指向另一张图 —— 比留一个空号危险得多。
    """
    k = normalize_kind(kind)
    prefix = KIND_CN.get(k, "图片")
    mx = 0
    for m in used_marks or []:
        mm = re.fullmatch(rf"{re.escape(prefix)}(\d+)", str(m or "").strip())
        if mm:
            mx = max(mx, int(mm.group(1)))
    return mx + 1


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
                    "alias": alias, "kind": kind, "legacy_file": f,
                    # 旧档没有 ref_name：落盘文件名的 basename 就是引用名（含后缀）
                    "ref_name": ref_name_of(a.get("ref_name"), f.split("/")[-1], alias)})
    return out


def build_registry(global_assets=None, project_links=None, legacy_assets=None) -> dict:
    """三源汇合 -> {by_id, by_alias, by_ref_name}，冲突时首个为准并记 warnings。

    by_id: asset_id -> {asset_id, alias, kind, origin, file?, ref_name?}
    by_alias: alias/label -> 同上（旧 label 与新 alias 同一命名空间）。
    by_ref_name: 引用名（含后缀的全名）-> 同上。**引用名的解析只走这个索引**
    （别名是 stem，用它解析会把 `.png` 留在正文里 —— 见 clean_ref_name）。
    """
    by_id, by_alias, by_ref_name, warnings = {}, {}, {}, {}

    def _put(ent, origin):
        aid = str(ent.get("asset_id") or "")
        if not aid:
            return
        alias = clean_alias(ent.get("alias") or ent.get("label") or "")
        kind = normalize_kind(ent.get("kind"))
        # 引用名：显式字段 > 原始文件名 > 落盘文件名 basename > 别名（无后缀兜底）
        f_now = str(ent.get("file") or ent.get("legacy_file") or "").replace("\\", "/")
        rname = ref_name_of(ent.get("ref_name"), ent.get("orig_name"),
                            f_now.split("/")[-1], alias)
        if aid not in by_id:
            rec = {"asset_id": aid, "alias": alias, "kind": kind, "origin": origin}
            if rname:
                rec["ref_name"] = rname
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
            if rname and not rec.get("ref_name"):
                rec["ref_name"] = rname
            for k in ("file", "legacy_file"):
                if ent.get(k) and not rec.get(k):
                    rec[k] = ent[k]
        if alias:
            if alias not in by_alias:
                by_alias[alias] = rec
            elif by_alias[alias] is not rec:
                warnings.setdefault("dup_alias", []).append(alias)
        if rname:
            if rname not in by_ref_name:
                by_ref_name[rname] = rec
            elif by_ref_name[rname] is not rec:
                warnings.setdefault("dup_ref_name", []).append(rname)

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
    return {"by_id": by_id, "by_alias": by_alias, "by_ref_name": by_ref_name,
            "warnings": warnings}


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
    by_ref_name = (registry or {}).get("by_ref_name") or {}
    errors, order = [], []
    seen = {}          # asset_id -> order 下标：同一素材重复引用只编号一次
    for r in refs or []:
        key, use = _ref_key(r)
        if not key:
            continue
        # 引用名（含后缀全名）优先：正文里写的就是它，别名（stem）只作旧档兜底
        rec = by_id.get(key) or by_ref_name.get(key) or by_alias.get(key)
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
        if rec.get("ref_name"):
            b["ref_name"] = rec["ref_name"]
        if use:
            b["use"] = use
        blocks.append(b)
        tag_map[rec["alias"] or rec["asset_id"]] = tok
        tag_map[rec["asset_id"]] = tok
        # 正文里的 @引用名 也映射到同一 token（@猫.png -> <Picture 1>）
        if rec.get("ref_name"):
            tag_map[rec["ref_name"]] = tok
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
