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
    """规则文件 = base / ref **各一中一英**，共 4 份；语言是可选维度，默认英文。

    历史：曾按 output_language 分成中英两套（`*_zh.txt` / 无后缀 = en），
    还额外自研过一套四字段 ref 规则（`<@名字>` / `<#名字:对话>` —— 自造语法，
    官方 tokenizer 根本不认），两套都被 279cad0 删掉。现在中文版**按官方原文
    重写后回归**，但：① 默认仍是英文；② 语言只由 `rule_file` 一个字段决定
    （独立的 `output_language` 配置字段已整体删除）。
    """
    files = optimizer.load_rule_files()
    for name in ("minimaxh3_base_prompt_writing.txt",
                 "minimaxh3_base_prompt_writing_zh.txt",
                 "minimaxh3_official_ref2v_prompt_writing.txt",
                 "minimaxh3_official_ref2v_prompt_writing_zh.txt"):
        assert name in files, name
    for gone in ("minimaxh3_custom_ref2v_prompt_writing.txt",
                 "minimaxh3_custom_ref2v_prompt_writing_zh.txt",
                 "prompt_translate_to_en.txt"):
        assert gone not in files, f"{gone} 应已删除（自研四字段口径已下线）"


def test_pick_rule_split_by_task():
    """规则文件按 **task**（不是语言）分流：base 模式 / Ref2VA 各取一份。"""
    files = optimizer.load_rule_files()
    base = optimizer.pick_rule_text({"rule_file": "auto"}, files, "T2VA")
    ref = optimizer.pick_rule_text({"rule_file": "auto"}, files, "Ref2VA")
    assert base is not None and ref is not None
    # 常规模式绝不能再拿到全参考规则（历史 bug）
    assert "integrated_multimodal_description" in base
    assert "<@" not in base and "<#" not in base
    # 全参考模式走**六段式** ref 规则 —— 与 build_system_prompt 的 REF2VA 分支、
    # prompts.REF_FIELDS 三者同口径
    assert "subject_definitions" in ref and "retention_analysis" in ref
    assert "detailed_description" in ref and "summary" in ref
    assert "fully_preserved" in ref and "<Subject " in ref
    assert "<#" not in ref, "旧的 <#名字:对话> 语法应已下线"
    # **auto（默认）拿到的必须是英文规则**：正文语言默认英文，不许悄悄变中文。
    # 注意：官方示例里**允许**出现中文 —— 那是"屏幕上可见文字要照抄"的正面例子
    # （`A red neon sign reading "营业中"`），不是中文指令。所以只禁"指令性中文"：
    # 中文出现在正文/字段说明里必然带这些高信号词，官方示例一个都不带。
    zh_cmd = ("提示词", "镜头", "必须", "不要", "应该", "字段", "段落", "时长",
              "参考", "素材", "保留", "音频", "视频", "风格")
    for task in ("T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA"):
        t = optimizer.pick_rule_text({"rule_file": "auto"}, files, task) or ""
        assert t, task
        bad = [w for w in zh_cmd if w in t]
        assert not bad, f"auto 下 {task} 的规则文件里混进了中文指令词：{bad}"
    # **zh 档**：同一套 task 分流，只把文件换成中文版 —— 且必须是中文指令
    for task in ("T2VA", "FL2VA", "Ref2VA"):
        zt = optimizer.pick_rule_text({"rule_file": "zh"}, files, task) or ""
        assert zt, task
        assert any(w in zt for w in zh_cmd), f"zh 档 {task} 应拿到中文规则"
        assert zt != optimizer.pick_rule_text({"rule_file": "auto"}, files, task), task
    # zh 档也要按 task 分流：常规段不许拿到六段式（历史上正是这么串味的）
    zh_base = optimizer.pick_rule_text({"rule_file": "zh"}, files, "T2VA") or ""
    zh_ref = optimizer.pick_rule_text({"rule_file": "zh"}, files, "Ref2VA") or ""
    assert "integrated_multimodal_description" in zh_base
    assert "subject_definitions" not in zh_base
    # 六段式：六个段名一个不少，且顺序固定（`subject_definitions` 在最前）
    ref_order = ["subject_definitions", "summary", "retention_analysis",
                 "detailed_description", "overall_soundscape", "non_diegetic_music"]
    assert all(sec in zh_ref for sec in ref_order)
    # 顺序固定看「1. 整体结构」那张表（全文首个 `detailed_description` 出现在引言里，
    # 拿全文下标比会被它带偏）
    tbl = zh_ref.split("## 1. 整体结构", 1)[1].split("## 2.", 1)[0]
    idx = [tbl.index(sec) for sec in ref_order]
    assert idx == sorted(idx), "六段顺序必须固定"
    # 注意：ref 规则里**会出现** `integrated_multimodal_description` —— 那是 5.2 节
    # 「全参考 vs T2VA」对照表里对 T2VA 主体字段的正常引用，不是"串味"。
    assert "detailed_description" in zh_ref
    # 显式选择优先于自动分流
    forced = optimizer.pick_rule_text(
        {"rule_file": "minimaxh3_official_ref2v_prompt_writing.txt"}, files, "T2VA")
    assert "subject_definitions" in forced
    # 老存档里已删除的文件名静默回落（不炸）：带 _zh 后缀的落到中文档，其余落到英文档
    legacy_zh = optimizer.pick_rule_text(
        {"rule_file": "minimaxh3_custom_ref2v_prompt_writing_zh.txt"}, files, "Ref2VA")
    assert legacy_zh is not None and "subject_definitions" in legacy_zh
    assert optimizer.rule_language(
        {"rule_file": "minimaxh3_custom_ref2v_prompt_writing_zh.txt"}) == "zh"
    legacy_en = optimizer.pick_rule_text(
        {"rule_file": "minimaxh3_custom_ref2v_prompt_writing.txt"}, files, "Ref2VA")
    assert legacy_en is not None and "subject_definitions" in legacy_en
    # none 不注入
    assert optimizer.pick_rule_text({"rule_file": "none"}, files, "Ref2VA") is None


def test_language_follows_rule_selection():
    """正文语言**只由 rule_file 推导**，且系统提示词与规则文件必须同语言。

    历史 bug：规则文件要中文、系统提示词却写死 "Write ... in English"，模型同时
    收到两条互斥的最高优先级指令 → 产出中英混排正文。所以语言只有一个来源
    （`rule_language`），再由它同时驱动规则文件与系统提示词。
    """
    assert optimizer.rule_language({"rule_file": "auto"}) == "en"
    assert optimizer.rule_language({}) == "en"                      # 缺省 = 英文
    assert optimizer.rule_language({"rule_file": "zh"}) == "zh"
    assert optimizer.rule_language({"rule_file": "minimaxh3_base_prompt_writing_zh.txt"}) == "zh"
    # 默认参数就是英文（老调用方不传 lang 时行为不变）
    assert optimizer.build_system_prompt("T2VA", 5.0, []) == \
        optimizer.build_system_prompt("T2VA", 5.0, [], None, "en")
    en = optimizer.build_system_prompt("T2VA", 5.0, [], None, "en")
    zh = optimizer.build_system_prompt("T2VA", 5.0, [], None, "zh")
    assert "Write the body in English" in en and "in Chinese" not in en
    assert "Write the body in Chinese" in zh and "in English" not in zh
    # 全参考分支说的是 "all six sections"，同样跟着语言走
    assert "Write all six sections in Chinese" in optimizer.build_system_prompt(
        "Ref2VA", 5.0, [], None, "zh")
    assert "Write all six sections in English" in optimizer.build_system_prompt(
        "Ref2VA", 5.0, [], None, "en")
    # 骨架两种语言下都保持英文：字段名 / 标签 / 单镜句不许被翻译
    for t in (en, zh):
        for token in ("integrated_multimodal_description", "overall_soundscape",
                      "non_diegetic_music", "<d>[language]", "At MM:SS.mmm",
                      "One continuous shot with no cuts."):
            assert token in t, token
    # 也不许残留 "N English sentences / English words" 这类会把中文档带偏的硬口径
    assert "English sentences" not in zh and "English words" not in zh


def test_all_base_modes_never_get_ref_rule():
    files = optimizer.load_rule_files()
    for task in ("T2VA", "I2VA", "FL2VA", "L2VA"):
        text = optimizer.pick_rule_text({"rule_file": "auto"}, files, task) or ""
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

    def fake_optimize_once(cfg, payload, on_progress=None):
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
    monkeypatch.setattr(optimizer, "optimize_once", lambda cfg, p, on_progress=None: BASE_OK)
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

    def fake_optimize_once(cfg, payload, on_progress=None):
        picked.append(optimizer.pick_rule_text(cfg, optimizer.load_rule_files(),
                                               payload.get("task")))
        return BASE_OK

    monkeypatch.setattr(optimizer, "optimize_once", fake_optimize_once)
    optimizer.optimize_multi_once(
        {"mode": "api", "provider": "openai", "api_key": "sk-x"},
        {"segments": [{"prompt": "x", "seconds": 5, "task": "T2VA"}]})
    assert picked and "<@" not in (picked[0] or "")


def test_optimize_multi_relays_progress_with_segment_numbering(monkeypatch):
    """多段优化必须把每段的流式事件转发出去，并带上**段序号**。

    前端靠 seg_no / total 画「第 i/N 段」的整条进度。三个易错点各钉一条：
    1. **分母是非空段数**：空段会被跳过，用 len(segments) 当分母 → 进度永远到不了 100%；
    2. **闭包必须钉住 i / seg_no**：直接引用循环变量的话，所有回调拿到的都是
       最后一段的值（第 2 段的帧会说自己还是第 1 段）；
    3. 事件要**原样透传** reasoning_chars / content_chars，别在转发层丢掉。
    """
    def fake_optimize_once(cfg, payload, on_progress=None):
        if on_progress:
            on_progress({"phase": "thinking", "reasoning_chars": 10, "content_chars": 0,
                         "tokens": 0, "max_tokens": 8192, "elapsed": 0.5})
            on_progress({"phase": "writing", "reasoning_chars": 10, "content_chars": 99,
                         "tokens": 0, "max_tokens": 8192, "elapsed": 1.5})
        return BASE_OK

    monkeypatch.setattr(optimizer, "optimize_once", fake_optimize_once)
    events = []
    optimizer.optimize_multi_once(
        {"mode": "api", "provider": "openai", "api_key": "sk-x"},
        {"segments": [{"prompt": "   ", "seconds": 5},   # 空段：跳过，不进分母
                      {"prompt": "甲", "seconds": 9},
                      {"prompt": "乙", "seconds": 8}]},
        on_progress=events.append)

    assert len(events) == 4, events                    # 2 个非空段 × 2 帧
    assert [e["total"] for e in events] == [2] * 4, "分母必须是非空段数，不是 len(segments)"
    assert [e["seg_no"] for e in events] == [1, 1, 2, 2], "段序号必须跟着走（闭包钉住）"
    assert [e["seg"] for e in events] == [1, 1, 2, 2], "原始下标要保留（空段占了 0）"
    assert [e["phase"] for e in events] == ["thinking", "writing"] * 2
    assert events[0]["content_chars"] == 0 and events[1]["content_chars"] == 99
    assert events[0]["max_tokens"] == 8192


def test_optimize_multi_without_progress_passes_none(monkeypatch):
    """没有进度回调时也要**显式**传 on_progress=None（调用形状统一）。

    这样 mock 只要照抄真签名就永远不会因"多了一个 kwarg"炸掉 ——
    比"按需才传"更难写错。
    """
    got = []

    def fake_optimize_once(cfg, payload, on_progress=None):
        got.append(on_progress)
        return BASE_OK

    monkeypatch.setattr(optimizer, "optimize_once", fake_optimize_once)
    optimizer.optimize_multi_once(
        {"mode": "api", "provider": "openai", "api_key": "sk-x"},
        {"segments": [{"prompt": "x", "seconds": 5}]})
    assert got == [None]
