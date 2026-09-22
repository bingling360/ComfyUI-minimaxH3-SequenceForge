"""优化链路的两道后端保险：确定性收尾 + 格式/语义校验（无 torch，可直接跑）。

为什么这两件事必须有（结构化提示词下线后的接管点）：

- **收尾（finalize_optimized）**：对齐指令的 `S.SS` 必须由段长**帧数**换算
  （5 秒段是 124 帧 = 5.17s，不是 5.00）；`[Shot 1]` 不许带时间戳；单镜补
  `One continuous shot with no cuts.`。这些是 LLM 算不准或会漏的，交给后端确定性做。
  对齐指令还必须落在**字段之外**（官方格式 = 指令句 + 空行 + 三字段）。

- **校验（validate_text）**：结构化下线后，屏显 / 对白的「引号归属」不再有表单兜底，
  只能靠语义检查发现「对白被双引号包住 → 会被烧成画面字幕」这类翻车；前置废话、
  裸抽象词、否定堆叠、全镜静止句同样只剩这一道闸。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from prompts import (FRAME_FPS, alignment_lines, detect_mode,  # noqa: E402
                     finalize_optimized, frames_to_seconds, validate_text)

I2VA_RAW = (
    "For the target video, at 0.00 seconds into the target video, <Picture 1> "
    "(from [Shot 1]) is fully referenced.\n\n"
    "integrated_multimodal_description: [Shot 1] At 00:00.000, 雨夜霓虹街道，"
    "女孩在湿滑人行道上侧向行走，路面积水映出招牌红光。\n\n"
    "overall_soundscape: 雨声与脚步。\n\nnon_diegetic_music: N/A")

PLAIN_RAW = ("integrated_multimodal_description: [Shot 1] 木屋外观，风吹动窗帘，"
             "檐下雨水成线落下，远处山谷被雾遮住。\n\nnon_diegetic_music: N/A")


def test_frame_clock_is_the_single_source():
    assert FRAME_FPS == 24.0
    assert frames_to_seconds(124) == 5.17      # 5s 吸附后是 124 帧，不是 120
    assert frames_to_seconds(192) == 8.00
    # 非法/缺省回落 5.0，不抛
    assert frames_to_seconds(0) == 5.0
    assert frames_to_seconds(None) == 5.0


def test_finalize_strips_then_reinjects_alignment():
    """模型自写的对齐句先剥掉，再按真实帧数重算 —— 5.17 而不是它写错的 5.00。"""
    r = finalize_optimized(I2VA_RAW, mode="I2VA", frames=124, has_start=True)
    assert r["actions"] == ["strip_alignment", "drop_shot1_timestamp",
                            "add_no_cuts", "inject_alignment"]
    assert r["duration"] == 5.17
    # 对齐句在字段**之外**（官方格式：指令句 + 空行 + 三字段）
    assert r["text"].startswith("For the target video, at 0.00 seconds")
    assert "\n\nintegrated_multimodal_description:" in r["text"]
    # [Shot 1] 不许带时间戳；单镜补 no cuts
    assert "[Shot 1] 雨夜霓虹街道" in r["text"]
    assert "One continuous shot with no cuts." in r["text"]


def test_finalize_l2va_uses_frame_derived_seconds():
    r = finalize_optimized(PLAIN_RAW, mode="L2VA", frames=124, has_end=True)
    assert r["actions"] == ["add_no_cuts", "inject_alignment"]
    assert "5.17-second mark" in r["text"], "对齐句的 S.SS 必须由帧数换算"


def test_finalize_does_not_inject_for_t2va_or_ref2va():
    t2 = finalize_optimized(PLAIN_RAW, mode="T2VA", frames=124)
    assert "inject_alignment" not in t2["actions"]
    assert "For the target video" not in t2["text"]
    hy = finalize_optimized(PLAIN_RAW, mode="HYBRID", frames=124)
    assert "inject_alignment" not in hy["actions"], "混合模式走六段式，不生成对齐句"


def test_finalize_drops_model_preamble():
    """首个字段头之前的废话（既非字段、也非对齐句）不许跟着进模型。"""
    r = finalize_optimized(
        "这是模型写的废话。\n\n" + PLAIN_RAW, mode="T2VA", frames=124)
    assert "drop_preamble" in r["actions"]
    assert "模型废话" not in r["text"]


def test_finalize_output_passes_validation():
    """收尾完的文本必须能过自己的校验（闭环）。"""
    for mode, kw in (("I2VA", {"has_start": True}), ("L2VA", {"has_end": True}),
                     ("T2VA", {})):
        raw = I2VA_RAW if mode == "I2VA" else PLAIN_RAW
        r = finalize_optimized(raw, mode=mode, frames=124, **kw)
        v = validate_text(r["text"], mode, None, frames=124)
        assert v["ok"] is True, f"{mode}: {v['errors']}"


def test_validate_catches_dialogue_in_double_quotes():
    """中文被双引号包住且前面没有屏显载体 → 疑似对白烧成字幕。"""
    bad = ('integrated_multimodal_description: [Shot 1] cinematic 画面，'
           '女孩说"跟上我"，不要拍她的脸。\n\nnon_diegetic_music: N/A')
    v = validate_text(bad, "T2VA", 5.0, source_text='女孩说"跟上我"')
    codes = {w["code"] for w in v["warnings"]}
    assert {"W_QUOTE_ZH", "W_ABSTRACT", "W_NEG_CN"} <= codes


def test_validate_accepts_screen_text_with_carrier():
    """写了载体的屏显不该被误判成对白误用引号。"""
    good = ('integrated_multimodal_description: [Shot 1] 雨夜店面，招牌写着 "营业中"，'
            "霓虹倒映在湿路面上。\n\nnon_diegetic_music: N/A")
    v = validate_text(good, "T2VA", 5.0)
    assert "W_QUOTE_ZH" not in {w["code"] for w in v["warnings"]}


def test_validate_rejects_preamble_but_allows_alignment_block():
    """前置对齐指令**合法**（官方格式本来就这样）；前置别的废话必须拦下。"""
    ok_txt = ("For the target video, at 0.00 seconds into the target video, "
              "<Picture 1> (from [Shot 1]) is fully referenced.\n\n" + PLAIN_RAW)
    v = validate_text(ok_txt, "I2VA", None, frames=124)
    assert "E_OVERRIDE_PREAMBLE" not in {e["code"] for e in v["errors"]}
    bad = "模型废话。\n\n" + PLAIN_RAW
    v2 = validate_text(bad, "T2VA", 5.0)
    assert "E_OVERRIDE_PREAMBLE" in {e["code"] for e in v2["errors"]}


def test_validate_freezes_and_missing_music():
    v = validate_text("integrated_multimodal_description: [Shot 1] nothing about the scene "
                      "changes for the whole shot.\n", "T2VA", 5.0)
    codes = {e["code"] for e in v["errors"]}
    assert "E_FREEZE" in codes          # 全镜静止句会冻住整镜
    assert "E_NO_MUSIC" in codes        # 无配乐必须写 N/A，不能省略


def test_detect_mode_matches_backend_rule():
    assert detect_mode({"references": [{"label": "<Picture 3>"}], "subjects": []},
                       has_start=True, has_end=True) == "Ref2VA"
    assert detect_mode({"references": [], "subjects": []},
                       has_start=True, has_end=True) == "FL2VA"
    assert detection_helper_kept() is True


def detection_helper_kept():
    """对齐指令族必须还在（实跑 nodes.py 依赖它们）。"""
    return alignment_lines("L2VA", 5.17, False, True, 1) == [
        "How the reference pictures align with the target video — "
        "<Picture 1> (from [Shot 1]) aligns with the 5.17-second mark "
        "of the target video."]
