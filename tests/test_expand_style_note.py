"""随图说明的下发口径回归。

前端把 collectSegMedia 的随图说明（`<Picture 1> = 参考素材「xx」`）塞进 style_note，
而 style_note 的既有口径是「风格/案例参考（**只吸收机制**）」。
一句"图1是谁"是**事实信息**，被套进"只吸收机制"里，模型可能把它当成
可吸收可不吸收的参考 —— 于是明明传了图，剧本却写得跟没看见图一样。

现在拆开：随图说明单独成节并强调"务必照图写"，风格/案例参考维持原口径。
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools", "h3_prompt_expander")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


for p in (ROOT, TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

screenplay = _load("screenplay", os.path.join(TOOLS, "screenplay.py"))

VISUAL = "随图说明：<Picture 1> = 参考素材「回廊场景」；<Picture 2> = 参考素材「少女立绘」。"
STYLE = "距离反转、惊吓后手势降级"


def _user(note):
    return screenplay.compose_script_messages("x", (8, 12), None, "balanced", note)["user"]


# ---------------------------------------------------------- 拆分行为

def test_visual_note_gets_its_own_section():
    u = _user(VISUAL)
    assert "随图（" in u
    assert "只吸收机制" not in u, "随图说明不该被当成可取舍的参考"


def test_style_only_keeps_original_wording():
    u = _user(STYLE)
    assert "风格/案例参考（只吸收机制）" in u
    assert "随图（" not in u


def test_mixed_note_is_split():
    u = _user(VISUAL + " 参考T8的距离反转机制")
    assert "随图（" in u
    assert "风格/案例参考（只吸收机制）：参考T8的距离反转机制" in u
    # 风格文本不能被吞进随图说明那一行
    assert "参考T8" not in u.split("随图（")[1].split("\n")[0]


def test_empty_note_adds_nothing():
    u = _user("")
    assert "随图（" not in u
    assert "只吸收机制" not in u


# ---------------------------------------------------------- 边界回归

def test_note_ending_at_string_end_does_not_crash():
    """回归：m.end() 落在串尾时 note[m.end()] 会 IndexError。"""
    v, rest = screenplay._split_style_note("随图说明：<Picture 1> = 参考素材「a」")
    assert "Picture 1" in v
    assert rest == ""


def test_outline_uses_same_split():
    u = screenplay.compose_outline_messages("x", 3, (6, 10), "balanced", VISUAL)["user"]
    assert "随图（" in u
    assert "只吸收机制" not in u


def test_visual_content_is_preserved():
    """拆出来的随图说明内容不许丢字。"""
    v, _ = screenplay._split_style_note(VISUAL)
    assert "回廊场景" in v and "少女立绘" in v


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
