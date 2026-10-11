"""对齐指令的三件事：时间口径、官方句式、混合模式（帧锚 + 参考素材）。

为什么要单独锁这些：
- 时间：段长的真实单位是**帧数**（17k+5 网格），对齐句的 S.SS 是秒。5 秒段实际
  124 帧 = 5.17s。前端曾写 5.00、后端写 5.17，同一个锚两个值（锚点位置也跟着偏）。
- 句式：逐字照抄 tools/h3_prompt_expander/references/h3-dialect.md §1.2，改一个词
  就不算官方格式了。
- 混合模式：H3 官方没有原生的"首尾帧 + 参考素材"格式，这里统一走 Ref2VA 六段式，
  帧锚作为 <Picture 1>/<Picture 2> 排进 subject_definitions 最前，任务前缀必须
  带上 keyframe completion —— 否则模型不知道有硬钉的帧。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from prompts import (  # noqa: E402
    FRAME_FPS, frames_to_seconds, alignment_lines, detect_mode, has_alignment_head,
    validate_text, finalize_optimized, _norm_mode)


def test_frames_to_seconds_is_the_single_clock():
    """帧数 -> 秒：前端锚定栏预览与后端实跑注入必须共用这一个换算。"""
    assert FRAME_FPS == 24.0
    assert frames_to_seconds(124) == 5.17      # 5s 吸附后是 124 帧，不是 120
    assert frames_to_seconds(192) == 8.00
    assert frames_to_seconds(5) == 0.21        # 5 帧（最短段）
    # 非法/缺省输入回落默认 5s，不抛
    assert frames_to_seconds(0) == 5.0
    assert frames_to_seconds(None) == 5.0
    assert frames_to_seconds("x") == 5.0


def test_alignment_lines_i2va():
    """I2VA：官方固定句，首帧 = <Picture 1>，0.00s。"""
    out = alignment_lines("I2VA", 5.17, True, False, 1)
    assert out == ["For the target video, at 0.00 seconds into the target video, "
                   "<Picture 1> (from [Shot 1]) is fully referenced."]


def test_alignment_lines_fl2va_one_sentence():
    """FL2VA 是**一条**整句同时锚首尾（不是拆两句），尾锚 Shot = 最后一镜。"""
    out = alignment_lines("FL2VA", 8.00, True, True, 3)
    assert len(out) == 1
    assert out[0] == ("How the reference pictures align with the target video — "
                      "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the "
                      "target video; Picture 2 (from Shot 3) aligns with the 8.00-second "
                      "mark of the target video.")


def test_alignment_lines_l2va():
    """L2VA：只有尾锚，编号是 1（不是 2），Shot = 最后一镜。"""
    out = alignment_lines("L2VA", 5.17, False, True, 2)
    assert out == ["How the reference pictures align with the target video — "
                   "<Picture 1> (from [Shot 2]) aligns with the 5.17-second mark "
                   "of the target video."]


def test_alignment_lines_none_for_t2va_and_ref2va():
    """T2VA 无对齐句；**混合模式（Ref2VA）也不生成** —— 帧锚走 subject_definitions。"""
    assert alignment_lines("T2VA", 5.0, False, False, 1) == []
    assert alignment_lines("Ref2VA", 5.0, True, True, 1) == []


def test_alignment_lines_requires_the_anchor_it_claims():
    """声明了锚但对应 has_* 为假 → 不生成（避免指向不存在的图）。"""
    assert alignment_lines("FL2VA", 5.0, True, False, 1) == [   # 只接了首帧 → 退化 I2VA
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced."]
    assert alignment_lines("FL2VA", 5.0, False, False, 1) == []


def test_detect_mode_hybrid_is_ref2va():
    """首尾帧锚 + 参考素材同时存在 → Ref2VA（走六段式，不生成对齐句）。"""
    with_refs = {"references": [{"label": "<Picture 3>", "note": "角色定妆照"}], "subjects": []}
    assert detect_mode(with_refs, has_start=True, has_end=True) == "Ref2VA"
    # 只有帧锚、没有素材 → 仍是 base 模式，该生成对齐句
    no_refs = {"references": [], "subjects": []}
    assert detect_mode(no_refs, has_start=True, has_end=True) == "FL2VA"


# ---- 注入侧：幂等守卫（「官方那句对齐句从来没进过编码器」的根因回归）----

def test_has_alignment_head_recognizes_official_forms():
    """三种官方对齐句都要认（I2VA / FL2VA / L2VA），且只认**首行整行**。"""
    assert has_alignment_head(
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
        "integrated_multimodal_description: [Shot 1] x")
    assert has_alignment_head(
        "How the reference pictures align with the target video — "
        "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target "
        "video; Picture 2 (from Shot 2) aligns with the 8.00-second mark of the target video.")
    assert has_alignment_head(
        "How the reference pictures align with the target video — "
        "<Picture 1> (from [Shot 2]) aligns with the 5.17-second mark of the target video.")
    # 前置空行不算（第一个非空行才是"首行"）
    assert has_alignment_head("\n\n  For the target video, at 0.00 seconds into the target "
                             "video, <Picture 1> (from [Shot 1]) is fully referenced.")


def test_has_alignment_head_rejects_plain_official_fields():
    """组装后的三字段文本**不是**对齐句 —— 这正是旧守卫恒为假的原因。

    旧注入守卫写的是 `not _OFFICIAL_FIELD_RE.search(...)`，而 `_compose_official`
    对任何非空提示词都必然写出 `integrated_multimodal_description:` 或
    `overall_soundscape:` → 守卫恒为假、注入从来没发生过。判"首行是不是对齐句"
    才是真正的幂等条件。
    """
    assert not has_alignment_head(
        "integrated_multimodal_description: [Shot 1] Live-action, cinematic...\n"
        "overall_soundscape: rain on the awning")
    assert not has_alignment_head("overall_soundscape: rain on the awning")
    # 正文里顺带提到这句（不是首行整行）不该被当成已有指令
    assert not has_alignment_head(
        "integrated_multimodal_description: [Shot 1] note that <Picture 1> is fully referenced.")
    assert not has_alignment_head("")
    assert not has_alignment_head(None)


def test_injected_lines_are_recognized_as_already_present():
    """注入产物必须能被幂等守卫认出来 —— 否则同一段会被重复注入。

    注入写法是 `"\\n".join(lines) + "\\n\\n" + 原文本`；守卫读第一个非空行。
    """
    for mode, hs, he in (("I2VA", True, False), ("L2VA", False, True), ("FL2VA", True, True)):
        lines = alignment_lines(mode, 5.17, hs, he, 2)
        assert lines, mode
        injected = "\n".join(lines) + "\n\n" + "integrated_multimodal_description: [Shot 1] x"
        assert has_alignment_head(injected), f"{mode} 注入产物没被守卫认出来（会重复注入）"
    # Ref2VA / T2VA 无对齐句 → 注入侧直接 continue，不进文本
    assert alignment_lines("Ref2VA", 5.17, True, True, 1) == []
    assert alignment_lines("T2VA", 5.17, False, False, 1) == []


def test_nodes_injects_alignment_with_the_right_guard():
    """注入侧源码点：守卫必须是 has_alignment_head，且统一走 alignment_lines。"""
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert "_P.has_alignment_head(" in src
    assert "_P.alignment_lines(" in src
    # 旧守卫（在组装之后恒为假）不得回流
    assert "_OFFICIAL_FIELD_RE.search(seg_prompts" not in src
    # 直接引用对齐句常量会让句式有两个真相 —— 已改为统一走 alignment_lines
    assert "from .prompts import L2VA_HEAD" not in src
    assert "L2VA_HEAD.format(" not in src
    assert "FL2VA_HEAD.format(" not in src


# ---- mode 归一 + Ref2VA 六段式（「有参考的段主字段名错了」回归）----

def test_norm_mode_is_case_insensitive():
    """mode 归一必须大小写无关 —— VALID_MODES 里的 "Ref2VA" 是**混合大小写**。

    旧实现 `s = str(mode).upper(); s if s in VALID_MODES else "T2VA"`：大写化后去查
    混合大小写的 VALID_MODES 必然失败 → "REF2VA" / "Ref2VA" / "ref2va" 全部退化成
    T2VA。而 optimizer.py 传的正是 `str(task).upper()`，于是 Ref2VA 的
    finalize_optimized / validate_text 长期按 base 规则跑：
      · finalize 取不到主字段（六段式是 detailed_description）→ 剥 [Shot 1] 时间戳、
        补 no cuts 全部静默跳过；
      · validate 拿 BASE_FIELDS 当白名单 → 合法的六段式被误报
        E_FIELD_SET（"T2VA 不应含字段 [...]"）。
    """
    for m in ("Ref2VA", "REF2VA", "ref2va", "ReF2vA"):
        assert _norm_mode(m) == "Ref2VA", m
    for m in ("T2VA", "I2VA", "FL2VA", "L2VA"):
        assert _norm_mode(m) == m
        assert _norm_mode(m.lower()) == m
    assert _norm_mode("HYBRID") == "Ref2VA"          # 历史别名
    assert _norm_mode("") == "T2VA" and _norm_mode(None) == "T2VA"
    assert _norm_mode("乱写") == "T2VA"


SIX_SECTION = (
    "subject_definitions:/n"
    "<Subject 1> is the young woman in <Picture 1>, with long dark hair.\n"
    "retention_analysis:/n"
    "<Subject 1> (appears in [Shot 1]): fully_preserved - identity retained.\n"
    "detailed_description:/n"
    "[Shot 1] Live-action, cinematic, a medium shot frames <Subject 1> by the window.\n"
    "overall_soundscape:/nQuiet room tone throughout.\n"
    "non_diegetic_music:/nN/A")


def test_ref2va_six_section_validates_cleanly():
    """六段式稿件在 Ref2VA 下不得报 E_FIELD_SET / E_EMPTY_DESC / E_NO_SHOT。

    旧 bug 下 mode 被归一成 T2VA → expected 变成 BASE_FIELDS →
    subject_definitions / retention_analysis / detailed_description 全被判"不应含"。
    """
    r = validate_text(SIX_SECTION, "Ref2VA", 8.0, source_text=SIX_SECTION, frames=192)
    codes = [e["code"] for e in r["errors"]]
    assert codes == [], codes
    f = finalize_optimized(SIX_SECTION, mode="REF2VA", frames=192)
    assert f["mode"] == "Ref2VA"
    assert "detailed_description:" in f["text"]
    assert "non_diegetic_music:" in f["text"]
