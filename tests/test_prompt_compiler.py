"""统一 Prompt Compiler 回归：编辑文本和 H3 运行文本只在边界转换。"""

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


rm = _load("h3_compiler_resolved_media", "resolved_media.py")
prompts = _load("h3_compiler_prompts", "prompts.py")


ASSETS = [
    {"asset_id": "a-b", "file_name": "b-character.png", "kind": "image"},
    {"asset_id": "a-a", "file_name": "a-character.png", "kind": "image"},
]


def test_compile_preserves_editor_at_names_but_returns_h3_tokens():
    text = (
        "integrated_multimodal_description: [Shot 1] 先出现 @b-character.png，"
        "随后出现 @a-character.png。\n"
        "non_diegetic_music: N/A"
    )
    result = prompts.compile_prompt_document(text, assets=ASSETS, mode="T2VA")
    assert result["ok"]
    assert "@b-character.png" in result["editor_text"]
    assert "<Picture 1>" in result["prompt_text"]
    assert "<Picture 2>" in result["prompt_text"]
    assert result["media_plan"]["blocks"][0]["file_name"] == "b-character.png"


def test_compile_unknown_reference_blocks_execution():
    result = prompts.compile_prompt_document(
        "integrated_multimodal_description: [Shot 1] @not-in-pool.png\n"
        "non_diegetic_music: N/A",
        assets=ASSETS,
        mode="T2VA",
    )
    assert not result["ok"]
    assert any(e["code"] == "H3_UNKNOWN_ASSET_REF" for e in result["errors"])


def test_compile_does_not_accept_direct_picture_token_in_editor_layer():
    result = prompts.compile_prompt_document(
        "integrated_multimodal_description: [Shot 1] <Picture 1>\n"
        "non_diegetic_music: N/A",
        assets=ASSETS,
        mode="T2VA",
    )
    assert not result["ok"]
    assert any(e["code"] == "H3_DIRECT_TOKEN_FORBIDDEN" for e in result["errors"])


def test_compile_wraps_plain_result_without_creating_another_editor_box():
    result = prompts.compile_prompt_document("女孩在雨中回头。", assets=[])
    assert result["ok"]
    assert result["prompt_text"].startswith("integrated_multimodal_description:")
    assert "[Shot 1] 女孩在雨中回头" in result["prompt_text"]
    assert any(w["code"] == "W_RESULT_WRAPPED" for w in result["warnings"])
