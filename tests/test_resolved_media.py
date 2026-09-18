"""ResolvedMediaPlan 纯函数回归。

这里专门冻结编辑层与 H3 执行层之间的边界：完整 @文件名只在编译时变成
官方 token，编号按正文首次出现顺序，而不是素材选择器顺序。
"""

import importlib.util
import os


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    path = os.path.join(ROOT, "resolved_media.py")
    spec = importlib.util.spec_from_file_location("h3_resolved_media_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rm = _load()


ASSETS = [
    {"asset_id": "a-girl", "file_name": "白袍少女_final.v3.png", "kind": "image"},
    {"asset_id": "a-room", "file_name": "scene with spaces (final).jpg", "kind": "image"},
    {"asset_id": "a-video", "file_name": "market_take_02.mp4", "kind": "video"},
    {"asset_id": "a-audio", "file_name": "rain-night.wav", "kind": "audio"},
]


def test_numbering_uses_first_text_occurrence_not_selection_order():
    result = rm.resolve_media_plan(
        "先看 @market_take_02.mp4，再看 @白袍少女_final.v3.png，最后再次 @market_take_02.mp4。",
        ASSETS,
    )
    assert result["ok"]
    assert result["prompt_text"] == (
        "先看 <Video 1>，再看 <Picture 1>，最后再次 <Video 1>。"
    )
    assert [x["file_name"] for x in result["blocks"]] == [
        "market_take_02.mp4", "白袍少女_final.v3.png"
    ]


def test_quoted_filename_supports_spaces_and_parentheses():
    result = rm.resolve_media_plan(
        '人物是 @"scene with spaces (final).jpg"，声音来自 @rain-night.wav。', ASSETS
    )
    assert result["ok"]
    assert "<Picture 1>" in result["prompt_text"]
    assert "<Audio 1>" in result["prompt_text"]
    assert not result["errors"]


def test_long_filename_is_not_truncated():
    name = "角色_最终版本_2026_09_18_带完整后缀的超长文件名.png"
    result = rm.resolve_media_plan(
        f"保持 @\"{name}\" 的人物外观。",
        [{"asset_id": "a-long", "file_name": name, "kind": "image"}],
    )
    assert result["ok"]
    assert result["blocks"][0]["file_name"] == name
    assert result["prompt_text"] == "保持 <Picture 1> 的人物外观。"


def test_repeated_reference_uses_one_token():
    result = rm.resolve_media_plan(
        "@白袍少女_final.v3.png 回头，@白袍少女_final.v3.png 继续向前。", ASSETS
    )
    assert result["ok"]
    assert result["prompt_text"].count("<Picture 1>") == 2
    assert len(result["blocks"]) == 1
    assert len(result["mentions"]) == 2
    assert {m["asset_id"] for m in result["mentions"]} == {"a-girl"}


def test_unknown_at_reference_is_blocking_error():
    result = rm.resolve_media_plan("画面出现 @missing_character.png。", ASSETS)
    assert not result["ok"]
    assert result["errors"][0]["code"] == "H3_UNKNOWN_ASSET_REF"
    assert result["errors"][0]["asset_name"] == "missing_character.png"


def test_legacy_bracket_reference_is_accepted_as_compatibility_input():
    result = rm.resolve_media_plan(
        "旧项目写的是 [[白袍少女_final.v3.png]]。", ASSETS
    )
    assert result["ok"]
    assert "<Picture 1>" in result["prompt_text"]


def test_unused_selected_asset_is_warning_only():
    result = rm.resolve_media_plan(
        "只引用 @白袍少女_final.v3.png。", ASSETS,
        selected_assets=[ASSETS[0], ASSETS[1]],
    )
    assert result["ok"]
    assert any(w["code"] == "W_SELECTED_ASSET_UNUSED" for w in result["warnings"])


def test_casefold_collision_is_not_silently_bound():
    result = rm.resolve_media_plan(
        "引用 @hero.png。",
        [
            {"asset_id": "a1", "file_name": "Hero.png", "kind": "image"},
            {"asset_id": "a2", "file_name": "hero.png", "kind": "image"},
        ],
    )
    assert not result["ok"]
    assert result["errors"][0]["code"] == "H3_AMBIGUOUS_ASSET_REF"
