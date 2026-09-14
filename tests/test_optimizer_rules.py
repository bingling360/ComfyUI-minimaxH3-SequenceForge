"""优化后端：规则文件按模式分流 + temperature 透传 + 优化后校验（阶段 0）。

这三处都是"能跑但结果是错的"型问题，靠肉眼看不出，只能靠断言钉住：

1. 规则错配：auto + 中文 一直注入**全参考四字段**规则（summary/detailed_description
   + <@名字>/<#名字:对话>），给常规段（T2VA/FL2VA）优化时与系统提示词的三字段要求
   正面冲突，产出非官方语法并原样进模型。
2. temperature 写死 0.2：风格三档只是提示词里的一句话，采样温度从未变过。
3. 优化结果从不校验：扩写有 validate+repair 环，优化没有，错误只能人眼看。
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


optimizer = _load("h3optimizer_rules", "optimizer.py")
prompts = _load("h3prompts_rules", "prompts.py")


def test_rule_files_exist():
    files = optimizer.load_rule_files()
    for name in ("minimaxh3_base_prompt_writing_zh.txt",
                 "minimaxh3_base_prompt_writing.txt",
                 "minimaxh3_custom_ref2v_prompt_writing_zh.txt"):
        assert name in files, name


def test_pick_rule_split_by_task():
    files = optimizer.load_rule_files()
    base_zh = optimizer.pick_rule_text(
        {"rule_file": "auto", "output_language": "中文"}, files, "T2VA")
    ref_zh = optimizer.pick_rule_text(
        {"rule_file": "auto", "output_language": "中文"}, files, "Ref2VA")
    assert base_zh is not None and ref_zh is not None
    # 常规模式绝不能再拿到全参考规则（历史 bug）
    assert "integrated_multimodal_description" in base_zh
    assert "<@" not in base_zh and "<#" not in base_zh
    # 全参考模式仍走 ref 规则（含 <@名字>/<#名字> 语法与主字段 detailed_description）
    assert "summary" in ref_zh and "detailed_description" in ref_zh
    assert "<@" in ref_zh and "<#" in ref_zh
    # 英文 + 常规
    base_en = optimizer.pick_rule_text(
        {"rule_file": "auto", "output_language": "English"}, files, "FL2VA")
    assert base_en is not None and "integrated_multimodal_description" in base_en
    # 显式选择优先于自动分流
    forced = optimizer.pick_rule_text(
        {"rule_file": "minimaxh3_custom_ref2v_prompt_writing_zh.txt"},
        files, "T2VA")
    assert "summary" in forced
    # none 不注入
    assert optimizer.pick_rule_text({"rule_file": "none"}, files, "Ref2VA") is None


def test_all_base_modes_never_get_ref_rule():
    files = optimizer.load_rule_files()
    for task in ("T2VA", "I2VA", "FL2VA", "L2VA"):
        text = optimizer.pick_rule_text(
            {"rule_file": "auto", "output_language": "中文"}, files, task)
        assert "<@" not in text, task
        assert "<#" not in text, task


def test_temp_normalize():
    assert optimizer._temp(None) == 0.2
    assert optimizer._temp(0.6) == 0.6
    assert optimizer._temp("bad") == 0.2
    assert optimizer._temp(-1) == 0.0
    assert optimizer._temp(99) == 2.0


def test_temperature_reaches_request(monkeypatch):
    """temperature 必须真的进请求体（历史 bug：写死 0.2）。"""
    seen = {}

    def fake_post(url, payload, headers=None, timeout=120):
        seen.update(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(optimizer, "_http_post_json", fake_post)
    cfg = optimizer.normalize_config(
        {"mode": "api", "provider": "openai", "api_key": "sk-x", "model": "m"})
    optimizer.generate_text(cfg, "sys", "user", temperature=0.75)
    assert seen.get("temperature") == 0.75


BASE_OK = ("integrated_multimodal_description: [Shot 1] 实拍、电影感，中景框住雨夜市场。\n"
           "\n"
           "overall_soundscape: 雨声与远处摊贩叫卖持续。\n"
           "\n"
           "non_diegetic_music: N/A")

REF_STYLE_BAD = ("summary: [reference generation] 目标视频中 <@张三> 在市场。\n"
                 "\n"
                 "detailed_description: [Shot 1] 她回头。<#张三:跟上我。>\n"
                 "\n"
                 "overall_soundscape: 雨声。\n"
                 "\n"
                 "non_diegetic_music: N/A")


def test_validate_optimized_accepts_official_base():
    v = optimizer._validate_optimized(BASE_OK, "T2VA", 9.0)
    assert v["ok"] is True, v


def test_validate_optimized_catches_ref_style_on_base_mode():
    """旧 bug 的产物（四字段 + <@名字>/<#名字>）必须被校验器抓出来。"""
    v = optimizer._validate_optimized(REF_STYLE_BAD, "T2VA", 9.0)
    assert v["ok"] is False
    codes = {e["code"] for e in v["errors"]}
    assert "E_FIELD_SET" in codes, v


def test_optimize_multi_validates_each_segment(monkeypatch):
    calls = []

    def fake_optimize_once(cfg, payload):
        calls.append(payload)
        # 甲段返回旧 bug 风格的产物（应被判不合格），乙段返回合规的官方三字段
        return REF_STYLE_BAD if payload.get("prompt") == "剧本甲" else BASE_OK

    monkeypatch.setattr(optimizer, "optimize_once", fake_optimize_once)
    res = optimizer.optimize_multi_once(
        {"mode": "api", "provider": "openai", "api_key": "sk-x"},
        {"segments": [{"prompt": "剧本甲", "seconds": 9, "task": "T2VA"},
                      {"prompt": "剧本乙", "seconds": 8, "task": "T2VA"}]})
    assert len(calls) == 2
    assert res["ok"] is False                       # 甲段被判不合格
    assert res["segments"][0]["ok"] is False
    assert res["segments"][1]["ok"] is True
    assert res["meta"]["failed"] == 1
    assert res["segments"][0]["result"] == REF_STYLE_BAD   # 结果仍返回，供人改


def test_optimize_multi_skips_empty_and_rejects_oversize(monkeypatch):
    monkeypatch.setattr(optimizer, "optimize_once", lambda cfg, p: BASE_OK)
    res = optimizer.optimize_multi_once(
        {"mode": "api", "provider": "openai", "api_key": "sk-x"},
        {"segments": [{"prompt": "  ", "seconds": 5}, {"prompt": "有内容", "seconds": 5}]})
    assert res["segments"][0]["skipped"] is True
    assert res["segments"][0]["result"] == ""
    assert res["ok"] is True                        # 空段跳过不计失败
    with pytest.raises(ValueError, match="段数"):
        optimizer.optimize_multi_once(
            {"mode": "api", "provider": "openai", "api_key": "sk-x"},
            {"segments": [{"prompt": "x"}] * 25})
    with pytest.raises(ValueError, match="segments 为空"):
        optimizer.optimize_multi_once({}, {"segments": []})


def test_optimize_multi_passes_task_to_rule_pick(monkeypatch):
    """task 必须传到 pick_rule_text，否则又回到"一律注入 ref 规则"的老路。"""
    picked = []

    def fake_optimize_once(cfg, payload):
        picked.append(optimizer.pick_rule_text(cfg, optimizer.load_rule_files(),
                                               payload.get("task")))
        return BASE_OK

    monkeypatch.setattr(optimizer, "optimize_once", fake_optimize_once)
    optimizer.optimize_multi_once(
        {"mode": "api", "provider": "openai", "api_key": "sk-x",
         "output_language": "中文"},
        {"segments": [{"prompt": "x", "seconds": 5, "task": "T2VA"}]})
    assert picked and "<@" not in (picked[0] or "")
