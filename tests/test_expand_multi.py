"""剧本扩写（内容发散器）契约测试 + 双跳上下文回归。

定位：扩写管**内容**（短意图 → 一大段能填满时长的中文剧本，自由格式），
优化管**格式**（剧本 → H3 官方三/六字段）。这个分工以前是反的：
扩写器被焊死成"格式转换器"（system_compile 明令"不许加戏"），
于是"AI 扩写"只能产出一段 4–15s 的官方提示词，出不了剧本。

本文件钉住：
1. 时长是**范围**（模型在 [min,max] 内自定秒数），不是定值；
2. 多段扩写能出 N 段，段数/秒数被钳进合法区间；
3. 剧本提示词明确禁止输出 H3 官方字段名（否则又变回格式转换器）；
4. 双跳第二跳必须带上 user_msg（历史 bug：只发 intent IR，
   把 duration / style_note 随图说明 / 场景包全丢了）。
"""
import importlib.util
import json
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

optimizer = _load("optimizer", os.path.join(ROOT, "optimizer.py"))
screenplay = _load("screenplay", os.path.join(TOOLS, "screenplay.py"))
h3_expand = _load("h3_expand", os.path.join(TOOLS, "h3_expand.py"))
service = _load("service", os.path.join(TOOLS, "service.py"))


# ---------------------------------------------------------------- 参数归一

def test_clamp_seconds_range():
    assert screenplay.clamp_seconds_range(6, 10) == (6.0, 10.0)
    assert screenplay.clamp_seconds_range(1, 3) == (4.0, 4.0)     # 下限 4s
    assert screenplay.clamp_seconds_range(20, 30) == (15.0, 15.0)  # 上限 15s
    assert screenplay.clamp_seconds_range(12, 8) == (8.0, 12.0)   # 反了要换回来
    # 非法输入 → 回落默认 5s（不是 0，也不是 H3 下限）
    assert screenplay.clamp_seconds_range("bad", None) == (5.0, 5.0)


def test_clamp_count():
    assert screenplay.clamp_count(3) == 3
    assert screenplay.clamp_count(0) == 1
    assert screenplay.clamp_count(99) == screenplay.MAX_SEGMENTS
    assert screenplay.clamp_count("x") == 3


def test_parse_script_seconds():
    assert screenplay.parse_script_seconds("时长：11 秒\n\n镜头一…", 6) == 11
    assert screenplay.parse_script_seconds("没有写秒数", 6) == 6
    assert screenplay.parse_script_seconds("时长：99 秒", 6) == 15   # 钳上限


# ---------------------------------------------------------------- 单段剧本

def test_screenplay_once_returns_plain_script(monkeypatch):
    captured = {}

    def fake_text(cfg, system, user, media=None, max_tokens=None, temperature=None,
                  on_progress=None):
        captured.update({"system": system, "user": user, "temperature": temperature})
        return "时长：10 秒\n\n镜头一（0–3 秒）：她停下回头。\n镜头二（3–10 秒）：她喊了一声。"

    monkeypatch.setattr(optimizer, "generate_text", fake_text)
    cfg = optimizer.normalize_config({"mode": "api", "provider": "openai",
                                      "api_key": "sk-x", "model": "m"})
    res = screenplay.screenplay_once("雨夜市场，女孩回头喊跟上我", (8, 12), "m", cfg,
                                     style="creative")
    assert res["seconds"] == 10
    assert "镜头一" in res["script"]
    # 风格档位要真的进采样温度（历史 bug：恒为 0.2）
    assert captured["temperature"] == screenplay.style_cfg("creative")["temperature"]
    assert "8" in captured["user"] and "12" in captured["user"]   # 时长范围进提示词


# ---------------------------------------------------------------- 多段

OUTLINE = {
    "logline_zh": "雨夜市场，她喊上我一起跑到天台",
    "characters": [{"name": "女孩", "traits": "短发，灰卫衣"}],
    "continuity": ["全程夜雨", "灰卫衣不变"],
    "segments": [
        {"index": 0, "seconds": 9, "logline_zh": "市场里她回头", "transition": "开场",
         "beats_hint": ["她停下", "她回头"]},
        {"index": 1, "seconds": 12, "logline_zh": "穿过摊贩", "transition": "连续动作",
         "beats_hint": ["挤进人流"]},
    ],
}


def _json_once(payload):
    state = {"n": 0}

    def fake_json(cfg, system, user, media=None, max_tokens=None, temperature=None):
        state["n"] += 1
        return payload
    return fake_json, state


def test_screenplay_multi_outline_then_scripts(monkeypatch):
    monkeypatch.setattr(optimizer, "generate_json",
                        _json_once(OUTLINE)[0])
    scripts = []

    def fake_text(cfg, system, user, media=None, max_tokens=None, temperature=None,
                  on_progress=None):
        scripts.append(user)
        return "时长：9 秒\n\n镜头一：她回头。"

    monkeypatch.setattr(optimizer, "generate_text", fake_text)
    cfg = optimizer.normalize_config({"mode": "api", "provider": "openai",
                                      "api_key": "sk-x", "model": "m"})
    res = screenplay.screenplay_multi("雨夜市场", 2, (8, 12), "m", cfg, one_shot=False)
    assert res["ok"] is True
    assert len(res["segments"]) == 2
    assert res["meta"]["one_shot"] is False
    assert res["logline_zh"] == OUTLINE["logline_zh"]
    # 跨段一致性要带到每一段的编写上下文里，人物才不会段间变脸
    assert len(scripts) == 2
    assert all("灰卫衣" in u for u in scripts)
    # 第二段要拿到前情提要
    assert "市场里她回头" in scripts[1]
    assert res["segments"][0]["seconds"] == 9


def test_screenplay_multi_one_shot(monkeypatch):
    payload = {
        "logline_zh": "总", "characters": [], "continuity": [],
        "segments": [{"index": 0, "seconds": 9, "logline_zh": "甲",
                      "script": "时长：9 秒\n\n镜头一：甲"},
                     {"index": 1, "seconds": 11, "logline_zh": "乙",
                      "script": "时长：11 秒\n\n镜头一：乙"}],
    }
    monkeypatch.setattr(optimizer, "generate_json", _json_once(payload)[0])
    cfg = optimizer.normalize_config({"mode": "api", "provider": "openai",
                                      "api_key": "sk-x", "model": "m"})
    res = screenplay.screenplay_multi("雨夜市场", 2, (8, 12), "m", cfg, one_shot=True)
    assert len(res["segments"]) == 2
    assert res["meta"]["one_shot"] is True
    assert res["segments"][1]["seconds"] == 11
    assert res["meta"]["total_seconds"] == 20


def test_screenplay_multi_clamps_model_seconds(monkeypatch):
    """模型给出的秒数超出范围/超出 H3 上限时，必须被钳回来。"""
    payload = dict(OUTLINE)
    payload["segments"] = [
        {"index": 0, "seconds": 60, "logline_zh": "超长", "transition": "开场"},
        {"index": 1, "seconds": 2, "logline_zh": "过短", "transition": "切镜"},
    ]
    monkeypatch.setattr(optimizer, "generate_json", _json_once(payload)[0])
    monkeypatch.setattr(optimizer, "generate_text",
                        lambda *a, **k: "时长：9 秒\n\n镜头一：x")
    cfg = optimizer.normalize_config({"mode": "api", "provider": "openai",
                                      "api_key": "sk-x", "model": "m"})
    res = screenplay.screenplay_multi("x", 2, (8, 12), "m", cfg, one_shot=False)
    got = [s["seconds"] for s in res["segments"]]
    assert all(8 <= g <= 12 for g in got), got


# ---------------------------------------------------------------- 服务入口

def test_service_single_segment(monkeypatch):
    monkeypatch.setattr(optimizer, "generate_text",
                        lambda *a, **k: "时长：7 秒\n\n镜头一：她回头。")
    res = service.screenplay_via_config(
        {"mode": "api", "provider": "openai", "api_key": "sk-x", "model": "m"},
        {"prompt": "雨夜市场", "seconds_min": 6, "seconds_max": 10})
    assert len(res["segments"]) == 1
    assert res["segments"][0]["seconds"] == 7
    assert res["meta"]["count"] == 1


def test_service_multi_segment(monkeypatch):
    monkeypatch.setattr(optimizer, "generate_json", _json_once(OUTLINE)[0])
    monkeypatch.setattr(optimizer, "generate_text",
                        lambda *a, **k: "时长：9 秒\n\n镜头一：x")
    res = service.screenplay_via_config(
        {"mode": "api", "provider": "openai", "api_key": "sk-x", "model": "m"},
        {"prompt": "雨夜市场", "segment_count": 2,
         "seconds_min": 8, "seconds_max": 12})
    assert len(res["segments"]) == 2
    assert res["meta"]["count"] == 2
    assert res["meta"]["seconds_range"] == [8.0, 12.0]


def test_service_rejects_empty():
    with pytest.raises(ValueError, match="prompt 为空"):
        service.screenplay_via_config(
            {"mode": "api", "provider": "openai", "api_key": "sk-x"}, {"prompt": "  "})


# ---------------------------------------------------------------- 定位契约

def test_screenplay_prompt_forbids_official_format():
    """扩写提示词必须明令"不准输出官方字段"，否则模型会把它当格式编译器用。"""
    sys_txt = screenplay.load_screenplay_prompts()[0]
    for token in ("integrated_multimodal_description", "overall_soundscape",
                  "non_diegetic_music"):
        assert token in sys_txt
    assert "不要输出" in sys_txt or "不许输出" in sys_txt
    assert "发散" in sys_txt


def test_outline_prompt_requires_exact_segment_count():
    outline_txt = screenplay.load_screenplay_prompts()[1]
    assert "segment_count" in outline_txt
    assert "严格等于" in outline_txt


# ---------------------------------------------------------------- 双跳回归

def test_two_step_second_jump_keeps_user_context():
    """双跳第二跳必须带上 user_msg（duration / style_note / notes / 场景包）。

    历史 bug：只发 "意图 IR：\\n" + json，随图说明与风格在第二跳全丢，
    于是"图传上去了、哪张图是谁却没了"。前端 two_step 是写死的，等于默认
    一直走信息更少那条路。
    """
    src = open(os.path.join(TOOLS, "h3_expand.py"), encoding="utf-8").read()
    i = src.index("else:\n        # 双跳")
    block = src[i:i + 1400]
    assert "o2 = chat(" in block
    assert "user_msg" in block.split("o2 = chat(")[1][:400], \
        "第二跳的 user content 必须包含 user_msg"


def test_chat_passes_temperature():
    src = open(os.path.join(TOOLS, "h3_expand.py"), encoding="utf-8").read()
    assert "def chat(model, messages, cfg, media=None, temperature=None)" in src
    assert "temperature=temperature" in src


def test_cli_model_flag_does_not_shadow_config():
    """`--model` 的 argparse 默认值曾经是 DEFAULT_MODEL（恒真的非空串），于是
    `args.model or cfg.get("model") or DEFAULT_MODEL` 里的 **cfg 永远轮不到** ——
    用户传了 --config 也没用，实际一直在调内置的 glm-4-flash。

    2026-09-19 换默认服务商到 GLM-4.6V 时发现：配置里写 glm-4.6v，跑出来却是
    glm-4-flash。默认必须是 None，优先级才是「显式 --model > --config 里的 model
    > 内置默认」。
    """
    src = open(os.path.join(TOOLS, "h3_expand.py"), encoding="utf-8").read()
    assert 'ap.add_argument("--model", default=None' in src, \
        "--model 的默认值不能是恒真值，否则会压住 --config 里的 model"
    assert 'model = args.model or cfg.get("model") or DEFAULT_MODEL' in src
