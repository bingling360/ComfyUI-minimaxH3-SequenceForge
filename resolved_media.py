"""统一的 H3 素材引用解析与运行期编号计划。

用户编辑层使用 ``@完整文件名``（含扩展名）；旧项目还可以使用
``[[旧标签]]``。只有在编译边界才转换为 H3 官方的
``<Picture N>`` / ``<Video N>`` / ``<Audio N>``。

本模块是纯 Python、无 ComfyUI 依赖的核心逻辑。它不调用 LLM、不加载节点、
不修改画布，也不写磁盘。调用方只需要传入**当前分段允许使用的资产列表**。

编号规则：
- 按结果正文中引用第一次出现的位置排序；
- 图片、视频、音频分别从 1 开始编号；
- 同一资产重复出现只占一个编号；
- 选择器的顺序只影响发给多模态 LLM 的图片顺序，不影响 H3 token。
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable


KINDS = ("image", "video", "audio")
REF_CAPS = {"image": 9, "video": 3, "audio": 3}
TOKEN_FORMATS = {
    "image": "<Picture {}>",
    "video": "<Video {}>",
    "audio": "<Audio {}>",
}
KIND_NAMES = {"image": "图片", "video": "视频", "audio": "音频"}

# 旧标签不允许跨越方括号；这里故意不限制长度，长度由资产注册表决定。
_BRACKET_REF = re.compile(r"\[\[([^\[\]]+)\]\]")
# 没有注册表候选时的降级扫描。完整文件名的点号、连字符和下划线必须保留。
_AT_FALLBACK = re.compile(
    r"(?<![0-9A-Za-z_])@([^\s@\[\]{}<>（）()，,。；;：:!?！？\"'`|/\\]+)"
)
_QUOTED_AT = re.compile(r"(?<![0-9A-Za-z_])@[\"']([^\"']+)[\"']")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _kind(value: Any) -> str:
    value = _text(value).lower()
    return value if value in KINDS else "image"


def _basename(value: Any) -> str:
    value = _text(value).replace("\\", "/")
    return value.rsplit("/", 1)[-1] if value else ""


def _asset_file_name(raw: dict) -> str:
    """提取用户引用用的完整文件名；绝不调用短别名截断。"""
    for key in ("file_name", "filename", "orig_name", "original_name", "name"):
        value = _text(raw.get(key))
        if value:
            return value
    # 旧项目通常只有 file。只取 basename，保留扩展名。
    base = _basename(raw.get("file"))
    if base and os.path.splitext(base)[1]:
        return base
    # 没有原始文件名时，旧 label/alias 仍可作为迁移期引用 key。
    return _text(raw.get("label") or raw.get("alias"))


def normalize_asset(raw: Any) -> dict | None:
    """把旧资产、全局资产和前端选择项归一为不截断的记录。"""
    if not isinstance(raw, dict):
        return None
    file_name = _asset_file_name(raw)
    alias = _text(raw.get("alias") or raw.get("label"))
    asset_id = _text(raw.get("asset_id") or raw.get("id"))
    if not file_name and not alias and not asset_id:
        return None
    out = {
        "asset_id": asset_id or file_name or alias,
        "file_name": file_name or alias or asset_id,
        "alias": alias,
        "kind": _kind(raw.get("kind")),
    }
    for key in ("file", "legacy_file", "storage_ref", "description", "role"):
        value = raw.get(key)
        if value not in (None, ""):
            out[key] = value
    return out


def normalize_assets(assets: Iterable[Any] | None) -> list[dict]:
    """归一化资产并保留输入顺序；同 asset_id 只保留首条记录。"""
    out, seen = [], set()
    for raw in assets or ():
        item = normalize_asset(raw)
        if item is None:
            continue
        key = item["asset_id"] or item["file_name"]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _candidate_keys(asset: dict) -> list[str]:
    """返回可被正文匹配的 key，第一项是现行完整文件名。"""
    values = [
        asset.get("file_name"),
        asset.get("orig_name"),
        asset.get("original_name"),
        asset.get("alias"),
        asset.get("label"),
        asset.get("asset_id"),
    ]
    # 旧记录的 file 允许作为兼容 key，但不作为用户首选显示名。
    f = _basename(asset.get("file"))
    if f:
        values.append(f)
    out, seen = [], set()
    for value in values:
        value = _text(value)
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            out.append(value)
    return out


def build_asset_index(assets: Iterable[Any] | None) -> dict:
    """建立精确/大小写折叠索引。

    索引值是列表而不是单值：同名但不同 asset_id 时必须报歧义，不能首胜静默错绑。
    """
    records = normalize_assets(assets)
    exact: dict[str, list[dict]] = {}
    folded: dict[str, list[dict]] = {}
    candidates: list[tuple[str, dict, str]] = []
    for asset in records:
        # 只对完整文件名候选标记 primary；别名是兼容候选。
        primary = _text(asset.get("file_name"))
        for key in _candidate_keys(asset):
            source = "primary" if key == primary else "compat"
            exact.setdefault(key, []).append(asset)
            folded.setdefault(key.casefold(), []).append(asset)
            candidates.append((key, asset, source))
    return {"records": records, "exact": exact, "folded": folded, "candidates": candidates}


def _unique_records(items: Iterable[dict]) -> list[dict]:
    out, seen = [], set()
    for item in items:
        key = item.get("asset_id") or item.get("file_name")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _lookup(index: dict, key: str) -> tuple[dict | None, str | None]:
    key = _text(key)
    if not key:
        return None, "H3_UNKNOWN_ASSET_REF"
    # Windows 文件系统通常大小写不敏感；即使正文大小写刚好命中其中一条，
    # 只要折叠后存在多个不同 asset_id，就不能静默猜测用户想要哪一条。
    folded = _unique_records((index.get("folded") or {}).get(key.casefold(), ()))
    if len(folded) > 1:
        return None, "H3_AMBIGUOUS_ASSET_REF"
    exact = _unique_records((index.get("exact") or {}).get(key, ()))
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, "H3_AMBIGUOUS_ASSET_REF"
    if len(folded) == 1:
        return folded[0], None
    return None, "H3_UNKNOWN_ASSET_REF"


def _primary_candidates(index: dict) -> list[tuple[str, dict]]:
    """按长度降序返回完整文件名候选，避免短名吃掉长名。"""
    out = []
    for asset in index.get("records") or ():
        key = _text(asset.get("file_name"))
        if key:
            out.append((key, asset))
    return sorted(out, key=lambda x: len(x[0]), reverse=True)


def extract_mentions(text: Any, assets: Iterable[Any] | None = None) -> list[dict]:
    """解析正文中的引用，返回按出现位置排序的 mention 列表。

    每项包含 ``key``、``raw``、``start``、``end``、``syntax``。对含空格/括号的文件名，
    优先使用完整文件名候选做最长匹配；插入器推荐使用 ``@\"文件名.ext\"``，两种语法
    都能被解析。
    """
    source = str(text or "")
    index = build_asset_index(assets)
    found: list[dict] = []
    occupied: list[tuple[int, int]] = []

    def add(start: int, end: int, key: str, raw: str, syntax: str):
        if any(start < b and end > a for a, b in occupied):
            return
        occupied.append((start, end))
        found.append({"key": key, "raw": raw, "start": start, "end": end,
                      "syntax": syntax})

    # 方括号是旧语法，优先记录。
    for match in _BRACKET_REF.finditer(source):
        add(match.start(), match.end(), match.group(1).strip(), match.group(0), "bracket")

    # 引号形式允许文件名含空格和大多数标点。
    for match in _QUOTED_AT.finditer(source):
        add(match.start(), match.end(), match.group(1), match.group(0), "at_quoted")

    primary = _primary_candidates(index)
    i = 0
    while i < len(source):
        if source[i] != "@" or (i and re.match(r"[0-9A-Za-z_]", source[i - 1])):
            i += 1
            continue
        if any(i < b and i + 1 > a for a, b in occupied):
            i += 1
            continue
        tail = source[i + 1:]
        hit = None
        for candidate, _asset in primary:
            if tail.casefold().startswith(candidate.casefold()):
                hit = candidate
                break
        if hit:
            end = i + 1 + len(hit)
            add(i, end, hit, source[i:end], "at")
            i = end
            continue
        match = _AT_FALLBACK.match(source, i)
        if match:
            add(match.start(), match.end(), match.group(1), match.group(0), "at")
            i = match.end()
        else:
            i += 1

    return sorted(found, key=lambda item: (item["start"], item["end"]))


def _token(kind: str, number: int) -> str:
    return TOKEN_FORMATS[kind].format(number)


def resolve_media_plan(text: Any, assets: Iterable[Any] | None = None,
                       *, selected_assets: Iterable[Any] | None = None,
                       segment_id: str | None = None) -> dict:
    """编译一个分段的素材引用，返回可供预览和执行共同消费的计划。"""
    source = str(text or "")
    records = normalize_assets(assets)
    index = build_asset_index(records)
    mentions = extract_mentions(source, records)
    errors: list[dict] = []
    warnings: list[dict] = []
    resolved_mentions: list[dict] = []
    unique: list[dict] = []
    by_asset: dict[str, dict] = {}

    for mention in mentions:
        rec, error_code = _lookup(index, mention["key"])
        if rec is None:
            errors.append({
                "code": error_code or "H3_UNKNOWN_ASSET_REF",
                "message": f"引用了未知或歧义素材「{mention['key']}」",
                "asset_name": mention["key"],
                "offset": mention["start"],
                "syntax": mention["syntax"],
                "segment_id": segment_id,
            })
            continue
        aid = rec.get("asset_id") or rec.get("file_name")
        resolved = dict(mention)
        resolved["asset_id"] = aid
        resolved["file_name"] = rec.get("file_name") or mention["key"]
        resolved["kind"] = rec.get("kind") or "image"
        resolved["record"] = rec
        resolved_mentions.append(resolved)
        if aid not in by_asset:
            item = {
                "asset_id": aid,
                "file_name": rec.get("file_name") or mention["key"],
                "alias": rec.get("alias") or "",
                "kind": rec.get("kind") or "image",
                "file": rec.get("file") or rec.get("legacy_file") or "",
                "first_occurrence": mention["start"],
                "occurrences": [mention["start"]],
            }
            by_asset[aid] = item
            unique.append(item)
        else:
            by_asset[aid]["occurrences"].append(mention["start"])

    counts = {kind: 0 for kind in KINDS}
    for item in unique:
        kind = item["kind"]
        if kind not in counts:
            errors.append({"code": "H3_UNKNOWN_ASSET_KIND", "message": f"未知素材类别「{kind}」",
                           "asset_name": item["file_name"], "segment_id": segment_id})
            continue
        counts[kind] += 1
        item["token"] = _token(kind, counts[kind])

    for kind, cap in REF_CAPS.items():
        if counts[kind] > cap:
            errors.append({
                "code": "H3_MEDIA_LIMIT",
                "message": f"{KIND_NAMES[kind]}引用 {counts[kind]} 个，超过单段上限 {cap}",
                "kind": kind,
                "count": counts[kind],
                "limit": cap,
                "segment_id": segment_id,
            })

    replacement_by_id = {item["asset_id"]: item["token"] for item in unique if item.get("token")}
    output = source
    for mention in sorted(resolved_mentions, key=lambda item: item["start"], reverse=True):
        token = replacement_by_id.get(mention.get("asset_id"))
        if token:
            output = output[:mention["start"]] + token + output[mention["end"]:]

    selected = normalize_assets(selected_assets if selected_assets is not None else records)
    used = {item["asset_id"] for item in unique}
    for item in selected:
        aid = item.get("asset_id") or item.get("file_name")
        if aid not in used:
            warnings.append({
                "code": "W_SELECTED_ASSET_UNUSED",
                "message": f"已选素材未在结果中引用：{item.get('file_name') or aid}",
                "asset_id": aid,
                "file_name": item.get("file_name") or "",
                "segment_id": segment_id,
            })

    blocks = []
    for item in unique:
        block = dict(item)
        block.pop("occurrences", None)
        blocks.append(block)

    tag_map: dict[str, str] = {}
    for item in unique:
        token = item.get("token")
        if not token:
            continue
        for key in (item.get("file_name"), item.get("alias"), item.get("asset_id")):
            key = _text(key)
            if key:
                tag_map[key] = token

    return {
        "ok": not errors,
        "segment_id": segment_id,
        "source_text": source,
        "prompt_text": output,
        "mentions": resolved_mentions,
        "blocks": blocks,
        "tag_map": tag_map,
        "counts": counts,
        "errors": errors,
        "warnings": warnings,
    }


# 语义化别名，便于旧 routes/新 compiler 逐步切换而不复制实现。
compile_media_plan = resolve_media_plan
compile_text_refs = resolve_media_plan


__all__ = [
    "KINDS", "REF_CAPS", "TOKEN_FORMATS", "normalize_asset", "normalize_assets",
    "build_asset_index", "extract_mentions", "resolve_media_plan", "compile_media_plan",
    "compile_text_refs",
]
