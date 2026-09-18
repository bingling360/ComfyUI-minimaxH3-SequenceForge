"""导演台提示词文档与旧状态迁移。

新 UI 的权威状态是 ``h3.director.v3`` 文档：每段只有意图、资产选择和结果。
旧的 ``prompts``、``intent_zh``、``script``、``prompt_v2``、``refs`` 等字段仍然
可以读取，但只进入 ``compat``，用于旧项目恢复和主节点兼容投影。

本模块不写磁盘，也不导入 ComfyUI。项目存档层可以把它放在 manifest 或导演台
状态 JSON 里；这样迁移逻辑可以在离线测试中完整验证，并且重复打开旧项目不会
不断生成新段或丢失历史字段。
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable

try:
    from .resolved_media import build_asset_index, normalize_assets
except ImportError:  # 允许离线测试按顶层模块加载
    from resolved_media import build_asset_index, normalize_assets


SCHEMA_VERSION = "h3.director.v3"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _dict(value: Any) -> dict:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def stable_segment_id(raw: Any, index: int, *, fallback_text: str = "") -> str:
    """为没有 ID 的旧分段生成幂等 ID。

    旧列表中两个内容完全相同的段也必须不同，因此 index 是最后的稳定盐；
    同一旧列表重复迁移得到相同 ID，调 UI 不会每次打开都换段身份。
    """
    if isinstance(raw, dict):
        for key in ("segment_id", "id", "uid", "key"):
            value = _text(raw.get(key))
            if value:
                return value
    payload = {
        "index": int(index),
        "intent": _text(raw.get("intent") if isinstance(raw, dict) else ""),
        "intent_zh": _text(raw.get("intent_zh") if isinstance(raw, dict) else ""),
        "prompt": _text(raw.get("prompt") if isinstance(raw, dict) else fallback_text),
        "fallback": _text(fallback_text),
    }
    digest = hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return "seg-" + digest.hexdigest()[:12]


def _legacy_segment(raw: Any) -> dict:
    return _dict(raw) if isinstance(raw, dict) else {}


def _asset_for_key(key: str, index: dict) -> dict | None:
    key = _text(key)
    if not key:
        return None
    exact = (index.get("exact") or {}).get(key, [])
    if len(exact) == 1:
        return copy.deepcopy(exact[0])
    folded = (index.get("folded") or {}).get(key.casefold(), [])
    if len(folded) == 1:
        return copy.deepcopy(folded[0])
    return None


def _selected_assets(raw_segment: dict, assets_index: dict) -> list[dict]:
    """把旧 refs 中能确认的条目转成新 selected_assets；不猜歧义。"""
    values = raw_segment.get("selected_assets")
    if not isinstance(values, list):
        values = raw_segment.get("assets")
    if not isinstance(values, list):
        values = raw_segment.get("refs")
    out, seen = [], set()
    for value in values or []:
        key = value
        if isinstance(value, dict):
            key = value.get("asset_id", value.get("id", value.get("alias", value.get("label", ""))))
        key = _text(key)
        rec = _asset_for_key(key, assets_index)
        if rec is None and isinstance(value, dict):
            rec = {
                "asset_id": _text(value.get("asset_id") or value.get("id")),
                "file_name": _text(value.get("file_name") or value.get("orig_name")
                                     or value.get("name") or value.get("label")),
                "alias": _text(value.get("alias") or value.get("label")),
                "kind": _text(value.get("kind")) or "image",
            }
            if not rec["asset_id"] and not rec["file_name"]:
                rec = None
        if rec is None:
            continue
        aid = _text(rec.get("asset_id") or rec.get("file_name"))
        if not aid or aid in seen:
            continue
        seen.add(aid)
        out.append({
            "asset_id": aid,
            "file_name": _text(rec.get("file_name") or rec.get("alias") or aid),
            "kind": _text(rec.get("kind")) or "image",
        })
    return out


def _migrate_result_text(text: str, assets: list[dict]) -> tuple[str, list[dict]]:
    """将旧 bracket/alias 引用尽可能转换成现行 @完整文件名。"""
    source = str(text or "")
    index = build_asset_index(assets)
    warnings = []
    # 只转换旧 bracket，避免把用户已经写好的新 @文件名重新改写。
    import re

    def repl(match):
        key = match.group(1).strip()
        rec = _asset_for_key(key, index)
        if rec is None:
            warnings.append({"code": "W_LEGACY_REF_UNRESOLVED", "asset_name": key})
            return match.group(0)
        return "@" + _text(rec.get("file_name") or key)

    source = re.sub(r"\[\[([^\[\]]+)\]\]", repl, source)
    return source, warnings


def migrate_legacy_document(raw: Any = None, *, prompts: Iterable[Any] | None = None,
                            segments: Iterable[Any] | None = None,
                            assets: Iterable[Any] | None = None) -> dict:
    """把旧导演台状态转换成 ``h3.director.v3``，操作幂等且不改输入。"""
    source = _dict(raw)
    if isinstance(source.get("document"), dict):
        source = _dict(source["document"])
    asset_records = normalize_assets(assets if assets is not None else source.get("ref_assets"))
    asset_index = build_asset_index(asset_records)
    old_prompts = _list(prompts if prompts is not None else source.get("prompts"))
    old_segments = _list(segments if segments is not None else source.get("segments"))
    old_fields = _list(source.get("seg_fields"))
    count = max(len(old_prompts), len(old_segments), len(old_fields))

    # 已经是 v3 时仍做一次轻量归一，但不把兼容数据丢掉。
    if source.get("schema_version") == SCHEMA_VERSION and isinstance(source.get("segments"), list):
        doc = _dict(source)
        doc.setdefault("total_intent", _text(doc.get("intent") or doc.get("total_intent")))
        doc.setdefault("execution_settings", {})
        doc.setdefault("compat", {})
        normalized = []
        for i, raw_segment in enumerate(doc.get("segments") or []):
            seg = _dict(raw_segment)
            seg["segment_id"] = stable_segment_id(seg, i, fallback_text=seg.get("intent", ""))
            seg["order"] = i
            seg["intent"] = _text(seg.get("intent") or seg.get("intent_zh"))
            result = _dict(seg.get("result"))
            result["text"] = str(result.get("text") or "")
            result.setdefault("source", "manual")
            seg["result"] = result
            seg["selected_assets"] = _selected_assets(seg, asset_index)
            seg.setdefault("execution", {"state": "draft", "awaiting_review": False})
            normalized.append(seg)
        doc["segments"] = normalized
        doc["schema_version"] = SCHEMA_VERSION
        return doc

    compat = {
        "legacy_prompts": copy.deepcopy(old_prompts),
        "legacy_segments": copy.deepcopy(old_segments),
        "legacy_seg_fields": copy.deepcopy(old_fields),
    }
    for key in ("prompt_v2", "optimizer", "opt_hist", "refs", "v2mode"):
        if key in source:
            compat["legacy_" + key] = copy.deepcopy(source[key])
    warnings = []
    result_segments = []
    for i in range(count):
        legacy = _legacy_segment(old_segments[i] if i < len(old_segments) else {})
        field = _legacy_segment(old_fields[i] if i < len(old_fields) else {})
        prompt = old_prompts[i] if i < len(old_prompts) else ""
        if not prompt:
            prompt = legacy.get("prompt") or legacy.get("result") or field.get("prompt") or ""
        text, ref_warnings = _migrate_result_text(str(prompt or ""), asset_records)
        warnings.extend({**w, "segment_index": i} for w in ref_warnings)
        intent = _text(legacy.get("intent") or legacy.get("intent_zh") or field.get("intent_zh"))
        selected = _selected_assets(legacy, asset_index)
        result = {
            "text": text,
            "mode": _text(legacy.get("mode") or field.get("v2mode")) or None,
            "source": "migrated" if text else "draft",
            "validation": {"ok": False if text else True, "errors": [], "warnings": []},
            "revision": 0,
        }
        result_segments.append({
            "segment_id": stable_segment_id(legacy, i, fallback_text=text or intent),
            "order": i,
            "intent": intent,
            "selected_assets": selected,
            "result": result,
            "execution": {"state": "draft" if not text else "ready", "awaiting_review": False},
        })

    compat["migration"] = {
        "source_schema": source.get("schema_version") or source.get("manifest_schema") or "legacy",
        "warnings": warnings,
    }
    doc = {
        "schema_version": SCHEMA_VERSION,
        "total_intent": _text(source.get("total_intent") or source.get("intent") or ""),
        "segments": result_segments,
        "execution_settings": {
            "review_mode": _text(source.get("review_mode") or "off") or "off",
        },
        "compat": compat,
    }
    return doc


def normalize_document(raw: Any = None, **kwargs) -> dict:
    """统一入口：v3 文档轻量归一，旧输入走迁移。"""
    return migrate_legacy_document(raw, **kwargs)


def compose_total_result(document: Any) -> str:
    """把各段结果投影成总结果框文本；总结果不是第二份权威状态。"""
    doc = _dict(document)
    chunks = []
    for i, seg in enumerate(doc.get("segments") or []):
        if not isinstance(seg, dict):
            continue
        text = _text((_dict(seg.get("result"))).get("text"))
        if text:
            chunks.append(f"【段{i + 1}】\n{text}")
    return "\n\n".join(chunks)


def document_to_legacy_state(document: Any, base: Any = None) -> dict:
    """生成旧主节点仍能消费的导演台状态投影，不覆盖原 compat 历史字段。"""
    doc = normalize_document(document)
    out = _dict(base)
    segments, prompts = [], []
    legacy_segments = ((_dict(doc.get("compat"))).get("legacy_segments") or [])
    for i, seg in enumerate(doc.get("segments") or []):
        seg = _dict(seg)
        result = _dict(seg.get("result"))
        legacy = _legacy_segment(legacy_segments[i] if i < len(legacy_segments) else {})
        refs = [
            _text(item.get("file_name") or item.get("asset_id"))
            for item in (seg.get("selected_assets") or []) if isinstance(item, dict)
        ]
        refs = [value for value in refs if value]
        old = dict(legacy)
        old.update({
            "intent_zh": _text(seg.get("intent")),
            "refs": refs,
            "segment_id": _text(seg.get("segment_id")),
            "prompt_v2": copy.deepcopy(old.get("prompt_v2") or {}),
        })
        segments.append(old)
        prompts.append(str(result.get("text") or ""))
    out["prompts"] = prompts
    out["segments"] = segments
    out["schema_version"] = SCHEMA_VERSION
    out["document"] = copy.deepcopy(doc)
    return out


__all__ = [
    "SCHEMA_VERSION", "stable_segment_id", "migrate_legacy_document", "normalize_document",
    "compose_total_result", "document_to_legacy_state",
]
