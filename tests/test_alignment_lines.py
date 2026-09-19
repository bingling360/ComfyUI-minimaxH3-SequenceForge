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

from prompts import (FRAME_FPS, frames_to_seconds, alignment_lines,  # noqa: E402
                     compose_reference, detect_mode, default_prompt)


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


def _prompt_with_refs():
    p = default_prompt()
    p["references"] = [{"label": "<Picture 3>", "note": "角色定妆照"}]
    p["shots"][0]["description"] = "她转身。"
    return p


def test_mixed_mode_puts_frame_anchors_first():
    """帧锚排在 subject_definitions **最前**，并标 fully_preserved。"""
    p = _prompt_with_refs()
    fields = compose_reference(p, duration=5.17,
                               frame_anchors=[("<Picture 1>", "首帧锚点，0.00s 起手帧"),
                                              ("<Picture 2>", "尾帧锚点，末帧终点")])
    subj = fields["subject_definitions"].splitlines()
    assert subj[0].startswith("<Picture 1>: 首帧锚点")
    assert subj[1].startswith("<Picture 2>: 尾帧锚点")
    assert any("角色定妆照" in s for s in subj)
    ret = fields["retention_analysis"].splitlines()
    assert ret[0].startswith("<Picture 1>: fully_preserved")
    assert ret[1].startswith("<Picture 2>: fully_preserved")
    # 参考素材是"参考一下"，不能跟着升成 fully_preserved
    assert any(s.startswith("<Picture 3>: partially_preserved") for s in ret)


def test_mixed_mode_declares_both_roles_in_summary():
    """任务前缀必须同时声明 keyframe completion，模型才知道有硬钉的帧。"""
    p = _prompt_with_refs()
    fields = compose_reference(p, duration=5.17,
                               frame_anchors=[("<Picture 1>", "首帧锚点")])
    assert fields["summary"].startswith("[keyframe completion + reference generation]")
    assert "anchored on <Picture 1>" in fields["summary"]


def test_no_frame_anchors_keeps_plain_reference_generation():
    """没有帧锚时不能凭空加 keyframe completion 前缀。"""
    p = _prompt_with_refs()
    fields = compose_reference(p, duration=5.17)
    assert fields["summary"].startswith("[reference generation]")
    assert "keyframe completion" not in fields["summary"]
    assert "anchored on" not in fields["summary"]


def test_detect_mode_hybrid_is_ref2va():
    """首尾帧锚 + 参考素材同时存在 → Ref2VA（走六段式，不生成对齐句）。"""
    p = _prompt_with_refs()
    assert detect_mode(p, has_start=True, has_end=True) == "Ref2VA"
    # 只有帧锚、没有素材 → 仍是 base 模式，该生成对齐句
    assert detect_mode(default_prompt(), has_start=True, has_end=True) == "FL2VA"
