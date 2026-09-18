"""expander 校验器的两条守卫回归。

钉住两个真实翻车点（2026-09-18 扮演 LLM 跑完整流程时踩出）：

1. `W_LENGTH` 曾只数英文单词（`re.findall(r"[A-Za-z']+")`），而主体内容按官方
   规范是中文写的 —— 于是一段 242 中文字的合规描述被报成"约 3 词，过短"，
   **每次中文扩写都挂一条误导性警告**。现在按 1 中文字 ≈ 0.6 英文词折算。
2. `description` 内部出现空行会被 `_align_blocks` 当成"关键帧对齐指令块"的分隔符，
   第二镜之后整段被截掉 → 报 `E_SHOT_ORDER: 编号必须从 1 递增，当前 [2]`。
   这是 LLM 极易犯的错（人总觉得分镜该空行），但旧报错完全没提空行，
   只能靠 repair 循环瞎猜。现在报错直接点名，并从编译提示词源头禁掉。
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

validate = _load("validate", os.path.join(TOOLS, "validate.py"))


def _env(desc, duration=5):
    """最小可用信封：只带校验必需的 pe，intent 给骨架即可。"""
    return {
        "intent": {"logline_zh": "测试", "constraints": {"duration": duration,
                                                     "shots": 2, "mode": "T2VA"}},
        "pe": {"mode": "T2VA", "duration": duration,
               "integrated_multimodal_description": desc,
               "overall_soundscape": "风声与碎石滚动声。",
               "non_diegetic_music": "N/A"},
    }


def _codes(verdict):
    return [x["code"] for x in verdict["errors"] + verdict["warnings"]]


# 约 90 中文字的多镜描述（>20 英文词当量，长度合规）
_CN_MULTI = (
    "[Shot 1] 实拍冷色调，断裂的石造回廊，湿冷石板地面泛着积水反光，"
    "一名少女沿回廊侧身后退，右手垂下的细线缓缓绷紧。\n"
    "[Shot 2] At 00:02.400, 镜头切换到回廊侧后方，少女绕到圣骑士背后双手下拉，"
    "泛光的细线骤然收紧，木质人偶同时抱住他的小腿。"
)


# ------------------------------------------------------ W_LENGTH 中文折算

def test_w_length_no_false_alarm_for_chinese():
    """全中文主体不应再被误报'过短'。回归：242 字曾被算成 3 词。"""
    v = validate.validate_envelope(_env(_CN_MULTI))
    assert "W_LENGTH" not in _codes(v), f"中文描述被误报长度：{v['warnings']}"


def test_w_length_still_fires_when_too_short():
    """折算后确实过短的仍要报（不能把守卫整个关掉）。"""
    v = validate.validate_envelope(_env("[Shot 1] 短。"))
    assert "W_LENGTH" in _codes(v)


def test_w_length_still_fires_when_too_long():
    """中文企划书直翻（超长）仍要报。"""
    long_desc = "[Shot 1] " + "画面持续推进" * 120
    v = validate.validate_envelope(_env(long_desc))
    assert "W_LENGTH" in _codes(v)


def test_w_length_message_reports_chinese_count():
    """警告文案要说清中文折算，别只丢一个英文词数让人困惑。"""
    v = validate.validate_envelope(_env("[Shot 1] " + "画面持续推进" * 120))
    msg = next(w["message"] for w in v["warnings"] if w["code"] == "W_LENGTH")
    assert "中文" in msg


# ------------------------------------------------------ 多镜空行陷阱

def test_multi_shot_with_single_newline_passes():
    """多镜之间用单个换行是合法写法，不该报 E_SHOT_ORDER。"""
    v = validate.validate_envelope(_env(_CN_MULTI))
    assert "E_SHOT_ORDER" not in _codes(v), f"单换行被误判：{v['errors']}"


def test_blank_line_between_shots_is_reported():
    """多镜之间用空行必须报错（内容会被 _align_blocks 截掉）。"""
    v = validate.validate_envelope(_env(_CN_MULTI.replace("\n", "\n\n")))
    assert "E_SHOT_ORDER" in _codes(v)


def test_blank_line_error_names_the_cause():
    """报错要点名'空行'，否则 repair 只能瞎猜。"""
    v = validate.validate_envelope(_env(_CN_MULTI.replace("\n", "\n\n")))
    msg = next(e["message"] for e in v["errors"] if e["code"] == "E_SHOT_ORDER")
    assert "空行" in msg, f"报错没点名空行，repair 无从下手：{msg}"
    assert "单个换行" in msg


def test_compile_prompt_forbids_blank_line():
    """源头防线：编译提示词必须写明禁止空行，否则 LLM 会一直踩。"""
    src = open(os.path.join(TOOLS, "prompts", "system_compile.md"), encoding="utf-8").read()
    assert "禁止空行" in src
    assert "单个换行" in src


# ------------------------------------------------------ Ref2VA 长度折算

def _ref_env(desc):
    return {
        "intent": {"logline_zh": "测试", "constraints": {"duration": 10, "shots": 2,
                                                         "mode": "Ref2VA"}},
        "pe": {"mode": "Ref2VA", "duration": 10,
               "subject_definitions": "<Subject 1>：少女，深色短斗篷，来自 Picture 1。",
               "summary": "[reference generation] 少女在回廊中与圣骑士交手一个回合。",
               "retention_analysis": "<Subject 1> (appears in [Shot 1], [Shot 2]): "
                                     "fully_preserved - 装束与发型全片一致。",
               "detailed_description": desc,
               "overall_soundscape": "风声与铠甲摩擦声。",
               "non_diegetic_music": "N/A"},
    }


def test_w_ref_length_no_false_alarm_for_chinese():
    """Ref2VA 的 detailed_description 也曾只数英文词，中文主体会误报。"""
    desc = ("实拍冷色调奇幻写实。\n[Shot 1] " + "少女沿回廊侧身后退" * 40
            + "\n[Shot 2] At 00:05.000, " + "圣骑士持盾冲来" * 40)
    v = validate.validate_envelope(_ref_env(desc))
    assert "W_REF_LENGTH" not in _codes(v), f"中文主体被误报：{v['warnings']}"


def test_w_ref_length_still_fires_when_too_short():
    v = validate.validate_envelope(_ref_env("实拍冷色调。\n[Shot 1] 少女后退。"))
    assert "W_REF_LENGTH" in _codes(v)


# ------------------------------------------------------ W_LENGTH 时长缩放

def test_w_length_scales_with_duration():
    """10s 段的内容量约为 5s 的两倍，阈值必须跟着 duration 走。

    回归：阈值写死 20-260（5s 口径），一个 442 中文字的 10s 段被误报"过长"。
    这是只有真跑 10s 场景才会暴露的问题 —— 上一轮修中文折算时只测了 5s。
    """
    desc = "[Shot 1] " + "画面持续推进" * 74          # 约 444 中文字
    v5 = validate.validate_envelope(_env(desc, duration=5))
    v10 = validate.validate_envelope(_env(desc, duration=10))
    assert "W_LENGTH" in _codes(v5), "5s 段这个量确实该报过长"
    assert "W_LENGTH" not in _codes(v10), "10s 段同量不该报（阈值没缩放）"


def test_w_length_message_shows_actual_duration():
    """文案要给出当前时长的建议区间，不能写死 5s。"""
    v = validate.validate_envelope(_env("[Shot 1] " + "画面持续推进" * 200, duration=10))
    msg = next(w["message"] for w in v["warnings"] if w["code"] == "W_LENGTH")
    assert "10s" in msg
    assert "120-240 词" in msg, f"未按 2 倍缩放：{msg}"


def test_w_length_short_still_fires_at_any_duration():
    """缩放不能把下限也放掉：任何时长下过短都该报。"""
    for d in (5, 10, 15):
        v = validate.validate_envelope(_env("[Shot 1] 短。", duration=d))
        assert "W_LENGTH" in _codes(v), f"{d}s 过短未报"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
