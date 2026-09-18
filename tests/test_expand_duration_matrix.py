"""多时长 × 多模式 校验矩阵回归。

为什么需要这个：前三轮"扮演 LLM 跑完整流程"挖出的 5 个 bug，**没有一个能靠
读代码发现**，全部要真跑才暴露。而且每修一个就暴露出下一个只在**别的时长或
别的模式**下才现形的问题：

- W_LENGTH 只数英文词 → 只在中文主体下现形
- 多镜空行被吞 → 只在多镜（≥8s）下现形
- W_LENGTH 阈值写死 5s → 只在 10s 下现形
- W_REF_LENGTH 区间写死 → 只在 Ref2VA 短段（4-5s）下现形

所以回归必须铺开矩阵，不能只测默认的「5s T2VA」。

两个极易混淆的换行规则（构造用例时踩过）：
- 对齐指令块 ↔ 正文：**必须空一行**（`_align_blocks` 靠它切分）
- 多镜之间：**禁止空行**（否则后一镜被当成独立块剥掉 → E_SHOT_ORDER）
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

FILL = "少女沿回廊侧身后退，细线绷紧，人偶冲出，碎石滚动，铠甲摩擦，风穿过柱廊"
DURS = [4, 5, 8, 10, 15]
MODES = ["T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA"]


def _fill(n):
    s = ""
    while len(s) < n:
        s += FILL
    return s[:n]


def _body(dur, per_sec=20):
    chars = int(dur * per_sec)
    shots = max(1, int(round(dur / 3.5)))
    per = max(20, chars // shots)
    out = []
    for i in range(shots):
        t = i * (dur / shots)
        head = "[Shot 1] 实拍冷色调奇幻写实。" if i == 0 else \
            "[Shot %d] At %02d:%06.3f, 镜头切换。" % (i + 1, int(t) // 60, t % 60)
        out.append(head + _fill(per))
    return "\n".join(out)


def _align(mode, dur, shots):
    if mode == "I2VA":
        return ("For the target video, at 0.00 seconds into the target video, "
                "<Picture 1> (from [Shot 1]) is fully referenced.")
    if mode == "FL2VA":
        return ("How the reference pictures align with the target video — "
                "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
                "Picture 2 (from Shot %d) aligns with the %.2f-second mark of the target video."
                % (shots, dur))
    if mode == "L2VA":
        return ("How the reference pictures align with the target video — "
                "<Picture 1> (from [Shot %d]) aligns with the %.2f-second mark of the target video."
                % (shots, dur))
    return ""


def _env(dur, mode):
    shots = max(1, int(round(dur / 3.5)))
    if mode == "Ref2VA":
        return {"intent": {"logline_zh": "t", "constraints": {"duration": dur, "shots": shots,
                                                             "mode": mode}},
                "pe": {"mode": mode, "duration": dur,
                       "subject_definitions": "<Subject 1>：少女，黑短发蓝眼，黑斗篷白领，"
                                              "来自 Picture 1。\n<Picture 1>：角色参考帧。",
                       "summary": "[reference generation] 少女在回廊与圣骑士交手一个回合。",
                       "retention_analysis": "<Subject 1> (appears in [Shot 1]): "
                                             "fully_preserved - 装束一致。\n"
                                             "<Picture 1>: fully_preserved - 外观照此图。",
                       "detailed_description": "实拍冷色调奇幻写实。\n" + _body(dur, per_sec=60),
                       "overall_soundscape": "风穿过柱廊的呼啸，碎石滚动。",
                       "non_diegetic_music": "N/A"}}
    b = _body(dur)
    a = _align(mode, dur, shots)
    if a:
        b = a + "\n\n" + b
    return {"intent": {"logline_zh": "t", "constraints": {"duration": dur, "shots": shots,
                                                         "mode": mode}},
            "pe": {"mode": mode, "duration": dur,
                   "integrated_multimodal_description": b,
                   "overall_soundscape": "风穿过柱廊的呼啸，碎石滚动，铠甲金属摩擦声。",
                   "non_diegetic_music": "N/A"}}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("dur", DURS)
def test_compliant_envelope_has_no_false_alarm(mode, dur):
    """按时长缩放的合规信封，在任何「模式 × 时长」组合下都不该报警。"""
    v = validate.validate_envelope(_env(dur, mode))
    assert not v["errors"], f"{mode}@{dur}s 误报错误：{v['errors']}"
    assert not v["warnings"], f"{mode}@{dur}s 误报警告：{v['warnings']}"


# ------------------------------------------------ 单点回归（钉住具体 bug）

def test_ref2va_short_segment_not_flagged():
    """回归：W_REF_LENGTH 区间写死 350-500 词，4s 段被要求写 580-830 中文字
    （145-208 字/秒，是 base 模式 20-40 字/秒的 4-7 倍，写不出来）。"""
    v = validate.validate_envelope(_env(4, "Ref2VA"))
    assert not [w for w in v["warnings"] if w["code"] == "W_REF_LENGTH"]


def test_ref2va_too_short_still_flagged():
    """缩放不能把守卫放掉。"""
    env = _env(10, "Ref2VA")
    env["pe"]["detailed_description"] = "实拍冷色调。\n[Shot 1] 少女后退。"
    v = validate.validate_envelope(env)
    assert [w for w in v["warnings"] if w["code"] == "W_REF_LENGTH"]


def test_alignment_block_separated_by_blank_line():
    """对齐指令块必须能靠空行切分出去，其 [Shot N] 不算真实镜头。"""
    for mode in ("I2VA", "FL2VA", "L2VA"):
        v = validate.validate_envelope(_env(15, mode))
        codes = [e["code"] for e in v["errors"]]
        assert "E_SHOT_ORDER" not in codes, f"{mode} 的对齐指令被当成真实镜头：{v['errors']}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
