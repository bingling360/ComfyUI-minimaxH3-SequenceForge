"""用户级 LLM 配置存储。

API Key、本地视觉模型引用等不再写入导演台状态或项目 manifest。配置文件位于
ComfyUI user/default 目录（由 folder_paths 提供），只保存用户配置；响应给前端
时由 optimizer.public_config 脱敏。没有 ComfyUI 运行时的离线测试不会触发写入。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


FILENAME = "h3_sequenceforge_llm.json"
SECRET_KEYS = {"api_key", "api_keys"}


def _user_dir() -> str | None:
    try:
        import folder_paths  # type: ignore
        fn = getattr(folder_paths, "get_user_directory", None)
        if callable(fn):
            value = str(fn() or "").strip()
            if value:
                return value
    except Exception:
        pass
    return None


def config_path() -> str | None:
    root = _user_dir()
    return os.path.join(root, FILENAME) if root else None


def load() -> dict:
    path = config_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _safe_model_ref(value: Any) -> str:
    value = str(value or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or ":" in value or ".." in value.split("/"):
        return ""
    return value


def _clean(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    allowed = {
        "mode", "provider", "api_url", "api_key", "api_keys", "model", "protocol",
        "provider_models", "read_media", "output_language", "local_model", "local_mmproj",
        "local_device", "max_tokens", "auto_optimize", "rule_file",
    }
    for key in allowed:
        if key not in raw:
            continue
        value = raw[key]
        if key == "api_keys":
            if isinstance(value, dict):
                out[key] = {str(k): str(v or "") for k, v in value.items() if str(v or "")}
        elif key == "provider_models":
            if isinstance(value, dict):
                out[key] = {str(k): str(v or "") for k, v in value.items() if str(v or "")}
        elif key in ("local_model", "local_mmproj"):
            out[key] = _safe_model_ref(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out


def save(raw: Any, *, keep_secrets: bool = True) -> dict:
    """保存用户配置并返回完整内部值；不把结果返回给 HTTP 客户端。"""
    current = load()
    incoming = _clean(raw)
    if keep_secrets:
        if not incoming.get("api_key") and current.get("api_key"):
            incoming["api_key"] = current["api_key"]
        old_keys = current.get("api_keys") if isinstance(current.get("api_keys"), dict) else {}
        new_keys = incoming.get("api_keys") if isinstance(incoming.get("api_keys"), dict) else {}
        merged_keys = dict(old_keys)
        merged_keys.update(new_keys)
        if merged_keys:
            incoming["api_keys"] = merged_keys
    merged = dict(current)
    merged.update(incoming)
    path = config_path()
    if not path:
        # 没有 ComfyUI user 目录时不把秘密落到插件目录；返回内存值给当前请求使用。
        return merged
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".h3cfg_", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, ensure_ascii=False, indent=1)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return merged


def public(raw: Any) -> dict:
    data = _clean(raw)
    return {
        key: value for key, value in data.items()
        if key not in SECRET_KEYS
    } | {
        "has_api_key": bool(data.get("api_key") or any((data.get("api_keys") or {}).values())),
    }
