"""预处理层的守卫回归：资产引用不能被当成对白。

翻车场景（2026-09-18 带图模拟时踩出）：用户在导演台里用
`@image#1:"ChatGPT Image 2026年9月18日 14_02_12.png"` 引用素材，
`QUOTE_RE` 把文件名当成引号内容，又因为文件名里含中文（"年"），
被判成"对白误用引号"并生成指令"须移入 <d>，引号只留屏显文字"。

这些 note 会进 user_msg 的"确定性预处理发现（优先采信）"，
模型照做就会把 **图片文件名写进 `<d>` 当台词念出来** —— 正是
bad_cases 里 B1「念提示词」的翻法，而且是被自家预处理误导的。

守卫要做的不是关掉引号检查，而是把它不该管的东西排除掉。
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

normalize = _load("normalize", os.path.join(TOOLS, "normalize.py"))


def _notes(raw):
    return normalize.analyze(raw)["notes_for_llm"]


# -------------------------------------------------- 资产引用不得误判为对白

def test_image_ref_not_treated_as_dialogue():
    """@image#N:"xxx.png" 里的文件名不是对白。"""
    n = normalize.analyze(
        '断裂的石造回廊 @image#1:"ChatGPT Image 2026年9月18日 14_02_12.png"，少女后退')
    assert n["quotes"] == [], f"文件名被当成引号内容：{n['quotes']}"


def test_image_ref_produces_no_misuse_note():
    """关键：不能生成'须移入 <d>'的误导指令，否则模型会念出文件名。"""
    notes = _notes('回廊 @image#1:"ChatGPT Image 2026年9月18日 14_02_12.png"，少女后退')
    assert not any("<d>" in x for x in notes), f"生成了误导指令：{notes}"


def test_multiple_refs_all_ignored():
    raw = ('石造回廊 @image#1:"a b 2026年.png"，少女 @image#2:"c d 2026年.jpg"，'
           '操控人偶与圣骑士战斗')
    assert normalize.analyze(raw)["quotes"] == []


def test_video_and_audio_refs_ignored():
    """@video/@audio 引用同理。"""
    raw = '街头追逐 @video#1:"clip 片段.mp4"，配乐 @audio#1:"bgm 音轨.wav"'
    assert normalize.analyze(raw)["quotes"] == []


# -------------------------------------------------- 守卫本身不许被关掉

def test_real_dialogue_still_detected():
    """真·中文对白仍要被判成引号误用，并提示移入 <d>。"""
    n = normalize.analyze('女孩回头说"跟上我"')
    assert "跟上我" in [q["text"] for q in n["quotes"]]
    assert any("<d>" in x for x in n["notes_for_llm"])


def test_visible_text_still_classified_visible():
    """屏显文字（霓虹/招牌）要判成 visible，不能被误伤成误用。"""
    n = normalize.analyze('霓虹招牌写着"营业中"')
    q = {x["text"]: x["likely"] for x in n["quotes"]}
    assert q.get("营业中") == "visible"


def test_combat_risk_still_detected_with_refs():
    """排除资产引用不该影响别的高危模式识别。"""
    raw = ('回廊 @image#1:"a.png"，少女操控人偶与圣骑士激烈战斗')
    ids = [r["id"] for r in normalize.analyze(raw)["risk_topics"]]
    assert "combat-melee" in ids


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
