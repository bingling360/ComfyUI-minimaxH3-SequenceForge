"""h3.director.v3 文档和旧状态迁移回归。"""

import importlib.util
import os


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, filename):
    path = os.path.join(ROOT, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rm = _load("h3_resolved_media_for_document", "resolved_media.py")
import sys
sys.modules["resolved_media"] = rm
docmod = _load("h3_prompt_document", "prompt_document.py")


ASSETS = [
    {"asset_id": "a-girl", "file_name": "girl_final.v3.png", "kind": "image", "alias": "女主"},
    {"asset_id": "a-room", "file_name": "room wide.jpg", "kind": "image", "alias": "场景"},
]


def test_legacy_migration_preserves_hidden_script_and_converts_bracket_ref():
    legacy = {
        "manifest_schema": "h3seamless/manifest-v2.2",
        "prompts": ["integrated_multimodal_description: [Shot 1] [[女主]] 回头"],
        "segments": [{
            "intent_zh": "雨夜里的女孩回头",
            "script": "旧剧本：女孩先停下，再回头。",
            "refs": ["女主"],
            "prompt_v2": {"visual": "legacy"},
        }],
        "optimizer": {"api_key": "secret"},
    }
    migrated = docmod.migrate_legacy_document(legacy, assets=ASSETS)
    assert migrated["schema_version"] == "h3.director.v3"
    seg = migrated["segments"][0]
    assert seg["intent"] == "雨夜里的女孩回头"
    assert seg["result"]["text"] == "integrated_multimodal_description: [Shot 1] @girl_final.v3.png 回头"
    assert seg["selected_assets"][0]["file_name"] == "girl_final.v3.png"
    assert migrated["compat"]["legacy_segments"][0]["script"].startswith("旧剧本")
    assert migrated["compat"]["legacy_optimizer"]["api_key"] == "secret"


def test_migration_is_idempotent_for_v3_document():
    first = docmod.migrate_legacy_document(
        {"prompts": ["x"], "segments": [{"intent_zh": "意图"}]}, assets=ASSETS
    )
    second = docmod.migrate_legacy_document(first, assets=ASSETS)
    assert second["schema_version"] == "h3.director.v3"
    assert second["segments"][0]["segment_id"] == first["segments"][0]["segment_id"]
    assert second["segments"][0]["result"]["text"] == "x"


def test_duplicate_legacy_segments_get_stable_distinct_ids():
    first = docmod.migrate_legacy_document(
        {"prompts": ["same", "same"], "segments": [{"intent_zh": "同"}, {"intent_zh": "同"}]},
        assets=ASSETS,
    )
    second = docmod.migrate_legacy_document(
        {"prompts": ["same", "same"], "segments": [{"intent_zh": "同"}, {"intent_zh": "同"}]},
        assets=ASSETS,
    )
    ids1 = [x["segment_id"] for x in first["segments"]]
    ids2 = [x["segment_id"] for x in second["segments"]]
    assert ids1 == ids2
    assert ids1[0] != ids1[1]


def test_total_result_is_derived_and_legacy_projection_keeps_prompts():
    document = docmod.migrate_legacy_document(
        {"prompts": ["one", "two"], "segments": [{"intent_zh": "a"}, {"intent_zh": "b"}]},
        assets=ASSETS,
    )
    assert docmod.compose_total_result(document) == "【段1】\none\n\n【段2】\ntwo"
    projected = docmod.document_to_legacy_state(document)
    assert projected["prompts"] == ["one", "two"]
    assert projected["segments"][0]["intent_zh"] == "a"
