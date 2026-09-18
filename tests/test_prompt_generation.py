"""统一意图 -> H3 JSON 生成入口的离线回归。"""

import importlib.util
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


resolved_media = _load("resolved_media", "resolved_media.py")
prompts = _load("prompts", "prompts.py")
optimizer = _load("optimizer", "optimizer.py")


def _cfg():
    return optimizer.normalize_config({
        "mode": "api", "provider": "openai", "api_key": "test-key",
        "model": "test-model", "rule_file": "none",
    })


def test_generate_prompt_once_is_one_llm_call_and_returns_structured_segments(monkeypatch):
    calls = []

    def fake_generate_json(cfg, system, user, media, max_tokens, temperature):
        calls.append({"system": system, "user": user, "media": media})
        return {
            "schema_version": "h3.prompt.generate.v1",
            "segments": [{
                "segment_id": "seg-1",
                "mode": "T2VA",
                "fields": {
                    "integrated_multimodal_description":
                        "[Shot 1] 女孩在雨中回头 @girl_final.v3.png。",
                    "non_diegetic_music": "N/A",
                },
                "text": (
                    "integrated_multimodal_description: [Shot 1] 女孩在雨中回头 @girl_final.v3.png。\n"
                    "non_diegetic_music: N/A"
                ),
            }],
        }

    monkeypatch.setattr(optimizer, "generate_json", fake_generate_json)
    result = optimizer.generate_prompt_once(_cfg(), {
        "segments": [{
            "segment_id": "seg-1",
            "intent": "白袍少女在雨中回头",
            "seconds": 5,
            "task": "T2VA",
            "selected_assets": [{
                "asset_id": "a-girl", "file_name": "girl_final.v3.png", "kind": "image",
            }],
        }],
        "media": [{"kind": "image", "label": "girl_final.v3.png",
                   "images": ["data:image/png;base64,AA=="]}],
    })
    assert len(calls) == 1
    assert result["schema_version"] == "h3.prompt.generate.v1"
    assert result["ok"]
    assert result["segments"][0]["editor_text"].count("@girl_final.v3.png") == 1
    assert "<Picture 1>" in result["segments"][0]["compiled_preview"]
    assert "girl_final.v3.png" in calls[0]["user"]
    assert calls[0]["media"][0]["images"]


def test_generate_prompt_once_rejects_model_unknown_asset(monkeypatch):
    monkeypatch.setattr(optimizer, "generate_json", lambda *args: {
        "segments": [{
            "segment_id": "seg-1", "mode": "T2VA",
            "text": "integrated_multimodal_description: [Shot 1] @missing.png\n"
                     "non_diegetic_music: N/A",
        }]
    })
    result = optimizer.generate_prompt_once(_cfg(), {
        "intent": "意图", "segment_id": "seg-1", "selected_assets": [],
    })
    assert not result["ok"]
    assert result["segments"][0]["errors"][0]["code"] == "H3_UNKNOWN_ASSET_REF"


def test_generate_prompt_once_rejects_wrong_segment_count(monkeypatch):
    monkeypatch.setattr(optimizer, "generate_json", lambda *args: {"segments": []})
    try:
        optimizer.generate_prompt_once(_cfg(), {
            "segments": [{"segment_id": "a", "intent": "一"},
                         {"segment_id": "b", "intent": "二"}],
        })
    except RuntimeError as exc:
        assert "期望 2 段" in str(exc)
    else:
        raise AssertionError("错误段数应阻断，而不是静默补空段")


def test_public_config_does_not_expose_key_or_local_paths():
    public = optimizer.public_config({
        **_cfg(), "api_keys": {"openai": "secret"},
        "local_model": "C:/models/private.gguf", "local_mmproj": "C:/models/mmproj.gguf",
    })
    assert public["api_key"] == ""
    assert public["api_keys"] == {}
    assert public["local_model"] == ""
    assert public["local_mmproj"] == ""
    assert public["has_api_key"]
