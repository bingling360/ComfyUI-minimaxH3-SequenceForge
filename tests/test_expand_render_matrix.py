"""render_h3 渲染矩阵：结构正确性 + 渲染→校验口径闭环。

为什么单独测渲染层：`render_h3` 会把关键帧对齐指令**剥离成独立首块**（用空行
分隔），而 `validate._align_blocks` 又靠**首个空行**把它切回来。两者是同一套
约定的两端 —— 一旦改了一边没改另一边，就会出现"渲染出来看着对、贴回去校验
就报错"的怪象，而且单看任何一边的代码都发现不了。

闭环断言因此是核心：render 出来的文本，还原成 envelope 后必须**仍然校验通过**。
"""
import copy
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
h3_expand = _load("h3_expand", os.path.join(TOOLS, "h3_expand.py"))

BASE_FIELD = "integrated_multimodal_description"
REF_FIELDS = ["subject_definitions", "summary", "retention_analysis",
              "detailed_description", "overall_soundscape", "non_diegetic_music"]
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
                       "subject_definitions": "<Subject 1>：少女，来自 Picture 1。\n"
                                              "<Picture 1>：角色参考帧。",
                       "summary": "[reference generation] 少女在回廊与圣骑士交手。",
                       "retention_analysis": "<Subject 1> (appears in [Shot 1]): "
                                             "fully_preserved - 装束一致。\n"
                                             "<Picture 1>: fully_preserved - 外观照此图。",
                       "detailed_description": "实拍冷色调奇幻写实。\n" + _body(dur, per_sec=60),
                       "overall_soundscape": "风穿过柱廊的呼啸。",
                       "non_diegetic_music": "N/A"}}
    b = _body(dur)
    a = _align(mode, dur, shots)
    if a:
        b = a + "\n\n" + b
    return {"intent": {"logline_zh": "t", "constraints": {"duration": dur, "shots": shots,
                                                         "mode": mode}},
            "pe": {"mode": mode, "duration": dur,
                   "integrated_multimodal_description": b,
                   "overall_soundscape": "风穿过柱廊的呼啸，碎石滚动。",
                   "non_diegetic_music": "N/A"}}


def _unrender(out, mode):
    """渲染输出还原成 envelope 字段（模拟用户把结果贴回提示词框）。"""
    parts = out.split("\n\n")
    if mode == "Ref2VA":
        d = {}
        for p in parts:
            for f in REF_FIELDS:
                if p.startswith(f + ": "):
                    d[f] = p[len(f) + 2:]
                elif p.strip() == f + ":":
                    d[f] = ""
        return d
    key = BASE_FIELD + ": "
    if parts[0].startswith(key):
        return {BASE_FIELD: parts[0][len(key):]}
    return {BASE_FIELD: parts[0] + "\n\n" + parts[1][len(key):]}


# ------------------------------------------------------------- 结构

@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("dur", DURS)
def test_render_structure(mode, dur):
    parts = h3_expand.render_h3(_env(dur, mode)).split("\n\n")
    if mode == "Ref2VA":
        assert [p.split(":")[0] for p in parts] == REF_FIELDS, "Ref2VA 六段顺序固定"
    else:
        key = BASE_FIELD + ": "
        if mode in ("I2VA", "FL2VA", "L2VA"):
            assert h3_expand._is_align_instruction(parts[0]), "对齐指令必须是独立首块"
            assert parts[1].startswith(key), "首块之后空一行接主字段"
        else:
            assert parts[0].startswith(key)
        assert parts[-1].startswith("non_diegetic_music: "), "末块是配乐字段"


# ------------------------------------------------------------- 闭环

@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("dur", DURS)
def test_render_round_trip_keeps_valid(mode, dur):
    """渲染 → 还原 → 校验，必须仍然通过。

    这是 render 与 validate 共享「空行切分对齐块」约定的护栏：
    改了一边没改另一边，这里会红。
    """
    env = _env(dur, mode)
    out = h3_expand.render_h3(env)
    env2 = copy.deepcopy(env)
    env2["pe"].update(_unrender(out, mode))
    v = validate.validate_envelope(env2)
    assert not v["errors"], f"{mode}@{dur}s 还原后报错：{v['errors']}"
    assert not v["warnings"], f"{mode}@{dur}s 还原后报警：{v['warnings']}"


def test_alignment_block_is_first_and_separated():
    """对齐指令与正文之间必须是空行（不是单换行），否则 validate 切不出来。"""
    out = h3_expand.render_h3(_env(10, "L2VA"))
    assert out.startswith("How the reference pictures align with the target video")
    assert out.split("\n")[0].rstrip().endswith("of the target video.")
    assert "\n\nintegrated_multimodal_description: " in out


def test_ref2va_missing_field_keeps_empty_block():
    """Ref2VA 缺段要留空块（便于人看出哪段没写），不是跳过。"""
    env = _env(10, "Ref2VA")
    env["pe"]["overall_soundscape"] = ""
    parts = h3_expand.render_h3(env).split("\n\n")
    assert [p.split(":")[0] for p in parts] == REF_FIELDS
    assert "overall_soundscape: N/A" in h3_expand.render_h3(env)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
