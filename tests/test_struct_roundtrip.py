"""B6「结构化 ⇄ 文本」等价性回归 —— 无 ComfyUI 可跑（缺 node 时前端部分跳过）。

跑法（仓库根目录）：
    python -m pytest tests/test_struct_roundtrip.py -q

段卡的「结构化 ⇄ 文本」是**同一份提示词的两种写法**，验收口径就一条：

    结构 → 文本 → 结构 → 文本     第二轮的两者必须与第一轮一致（幂等）

不幂等意味着什么：用户在结构化视图里改一镜，切回文本，再切回结构化，内容变了——
那这个切换就不是"等价视图"，而是"每次切换随机改一次内容"，谁也不敢用。

两条容易被忽略的前提（方案 §9 风险 4）：
1. 前端 defaultPromptV2 的**字段表必须与后端 default_prompt 逐字一致**——后端有
   `visual`（画面整述）、前端曾缺，造出来的对象少一个键，clean_prompt 补默认值后
   就不相等了。本文件直接比对两份默认结构的键集合。
2. 文本 → 结构必须走 splitH3Sections + applyH3TextToSeg（按 [Shot N] 切镜），
   不能再用旧的 applyAiToV2（把整段塞进 shots[0]，多镜段一切就只剩第一镜）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")


def _load_top(name, path):
    src = open(path, encoding="utf-8").read()
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    sys.modules[name] = mod
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


@pytest.fixture(scope="module")
def PR():
    return _load_top("prompts", os.path.join(ROOT, "prompts.py"))


def _js_fn(name, path="web/h3_director.js"):
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    m = re.search(rf"^function {re.escape(name)}\(.*?^\}}", src, re.M | re.S)
    assert m, f"未找到前端函数 {name}"
    return m.group(0)


def _js_const(name, path="web/h3_director.js"):
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    m = re.search(rf"^const {re.escape(name)} = \[?.*?^\]", src, re.M | re.S)
    if not m:
        m = re.search(rf"^const {re.escape(name)} = [^;]*;", src, re.M)
    assert m, f"未找到前端常量 {name}"
    return m.group(0)


def _frontend_apply_h3(text):
    """在 node 里跑前端 applyH3TextToSeg（stub 掉写回），返回产出的 pv。"""
    code = (
        "const H3_SECTION_KEYS = new Set(['integrated_multimodal_description',"
        "'detailed_description','subject_definitions','summary','retention_analysis',"
        "'overall_soundscape','non_diegetic_music']);\n"
        + _js_fn("splitH3Sections") + "\n"
        + _js_fn("_tsToSec") + "\n"
        + _js_fn("applyH3TextToSeg") + "\n"
        + "const store = {};\n"
        "function setPromptV2Field(node, segIdx, mutate) {\n"
        "  const pv = store[segIdx] || (store[segIdx] = {});\n"
        "  mutate(pv); return true;\n"
        "}\n"
        "applyH3TextToSeg(null, 0, " + json.dumps(text, ensure_ascii=False) + ");\n"
        "console.log(JSON.stringify(store[0] || null));"
    )
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _pv_of(shot_descs, seconds_list=None, soundscape="", music=""):
    shots = []
    for i, d in enumerate(shot_descs):
        shots.append({"index": i + 1,
                      "start_seconds": None if i == 0 else (seconds_list or [None, 3.0])[i],
                      "description": d})
    return {"shots": shots, "soundscape": soundscape, "non_diegetic_music": music}


def _compile(PR, pv, seconds=9.0):
    return PR.compile_segment(PR.clean_prompt(pv), seconds=seconds)["prompt_text"]


# ---- 1. 前后端 v2 结构字段表必须一致（否则幂等无从谈起） ----

def test_frontend_default_prompt_v2_keys_match_backend(PR):
    src = open(os.path.join(ROOT, "web", "h3_prompts.js"), encoding="utf-8").read()
    m = re.search(r"function defaultPromptV2\(\)", src)
    assert m, "未找到前端 defaultPromptV2"
    i = src.index("{", m.start())
    depth = 0
    end = i
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    body = src[i:end]
    keys = set(re.findall(r"([A-Za-z_]+)\s*:", body))
    want = set(PR.default_prompt().keys())
    missing = sorted(want - keys)
    assert not missing, f"前端 defaultPromptV2 缺字段（后端有）：{missing}"


# ---- 2. 文本 → 结构：多镜必须切成多镜（旧 applyAiToV2 会只剩第一镜） ----

@pytest.mark.skipif(NODE is None, reason="需要 node 执行前端纯函数")
def test_text_to_struct_splits_shots():
    text = ("integrated_multimodal_description: [Shot 1] 中景框住雨夜窄巷。\n"
            "[Shot 2] At 00:03.200，镜头小幅慢速推近她的侧脸。\n\n"
            "overall_soundscape: 雨声持续。\n\nnon_diegetic_music: N/A")
    pv = _frontend_apply_h3(text)
    assert pv, "文本 → 结构没有产出任何内容"
    assert len(pv.get("shots") or []) == 2, f"两镜被合成一镜：{pv.get('shots')}"
    assert "雨夜窄巷" in pv["shots"][0]["description"]
    assert "侧脸" in pv["shots"][1]["description"]
    assert pv["shots"][1].get("at") == "00:03.200", pv["shots"][1]
    assert pv.get("soundscape") == "雨声持续。"
    assert pv.get("non_diegetic_music") == "N/A"


# ---- 3. 结构 → 文本 → 结构 → 文本：第二轮必须与第一轮一致（幂等） ----

@pytest.mark.skipif(NODE is None, reason="需要 node 执行前端纯函数")
@pytest.mark.parametrize("descs,secs", [
    (["中景框住雨夜窄巷，霓虹在积水里拉出红蓝长影。"], None),
    (["中景框住雨夜窄巷。", "镜头推近她的侧脸，她回头看向镜头。",
      "她转身挤进人流，镜头跟拍。"], [None, 3.0, 7.0]),
])
def test_struct_text_roundtrip_is_idempotent(PR, descs, secs):
    pv0 = _pv_of(descs, secs, soundscape="雨声持续，远处摊贩叫卖。", music="N/A")
    t1 = _compile(PR, pv0)
    pv1 = _frontend_apply_h3(t1)
    assert pv1, "文本 → 结构没产出内容"
    t2 = _compile(PR, pv1)
    pv2 = _frontend_apply_h3(t2)
    t3 = _compile(PR, pv2)

    # 结构层面：两轮结构一致（键集合 + 逐镜正文/时间点 + 声音）
    assert (pv1.get("shots") or []) == (pv2.get("shots") or []), \
        f"结构不幂等：\n1={json.dumps(pv1.get('shots'), ensure_ascii=False)}\n" \
        f"2={json.dumps(pv2.get('shots'), ensure_ascii=False)}"
    assert pv1.get("soundscape") == pv2.get("soundscape")
    assert pv1.get("non_diegetic_music") == pv2.get("non_diegetic_music")
    # 文本层面：第二轮编译出的正文必须稳定
    assert t2 == t3, f"文本不幂等：\n2={t2!r}\n3={t3!r}"


# ---- 4. 旧实现已下线（防止有人把 applyAiToV2 加回来） ----

def test_apply_ai_to_v2_removed():
    d = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    assert "function applyAiToV2(" not in d, "旧的 applyAiToV2 应已删除"
    assert "同步到具象化 →" not in d, "旧的双向同步按钮应已换成「⇄ 结构化提示词」弹窗"
    assert "⇄ 结构化提示词" in d, "缺「⇄ 结构化提示词」弹窗入口"
    assert "function openStructuredModal(node, data, idx, ta)" in d
    # 具象化不再是独立 tab
    assert '["main", "提示词"]' in d and '["set", "锚定设置"]' in d, "段卡 tab 应为 提示词 / 锚定设置"
    # 结构化不再有页内 pane（paneV2 已随弹窗化删掉）
    assert "paneV2" not in d, "页内 paneV2 应已删除"
    m = re.search(r"const panes = \{([^}]*)\};", d)
    assert m and "v2:" not in m.group(1), "panes 里不应再有 v2"
