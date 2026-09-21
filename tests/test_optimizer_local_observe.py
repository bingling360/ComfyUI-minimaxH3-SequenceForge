# -*- coding: utf-8 -*-
"""本地 GGUF 通道的观测口径（加速项生效 / 显存账 / 吞吐）。

背景（2026-09-22）：提示词优化走本地模型时，「加速到底生效没有」和「显存用了多少」
**完全不可见**——llama.cpp 用自己的 CUDA 分配器（`torch.cuda.memory_allocated()`
看不见它），`n_gpu_layers=-1` 只是请求、放不下就静默掉层（decode 从 ~25 tok/s
掉到个位数），而 `_new_llama` 只在降级时吭一声、成功时零输出。
docs/本地LLM保守加速提案_3090.md §2 早已写明验收标准却一直没落到代码里。

这里钉住新增的三件事：plan（实际吃到的加速项）、显存账、判据。

不联网、不装 llama_cpp —— `_new_llama` 用 sys.modules 假替身验证降级/成功两条路。
"""

import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "h3optimizer_local_observe", os.path.join(ROOT, "optimizer.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def opt():
    return _load()


# ---- 探针原语：绝不抛 ----

def test_vram_used_gb_never_raises(opt):
    """无 CUDA / 无 torch 一律 None（不编数字），有就是非负 float。"""
    v = opt._vram_used_gb()
    assert v is None or (isinstance(v, float) and v >= 0.0)


def test_file_gb_handles_missing(opt, tmp_path):
    assert opt._file_gb(str(tmp_path / "nope.gguf")) is None
    p = tmp_path / "m.gguf"
    p.write_bytes(b"x" * 2048)
    assert opt._file_gb(str(p)) == pytest.approx(2048 / (1024 ** 3))


# ---- plan：成功路径也要有输出（原来只有失败才吭声）----

def test_plan_line_fast_path_lists_effective_items(opt):
    plan = {"mode": "fast", "flash_attn": True, "kv_quant": True,
            "n_batch": 2048, "n_ubatch": 512, "n_gpu_layers": -1}
    line = opt._plan_line(plan)
    assert "flash_attn √" in line
    assert "q8_0" in line
    assert "n_ubatch 512" in line
    assert "请求全铺" in line
    assert "降级" not in line


def test_plan_line_degraded_says_what_was_dropped(opt):
    plan = {"mode": "degraded", "flash_attn": False, "kv_quant": False,
            "n_batch": 512, "n_ubatch": None, "n_gpu_layers": -1,
            "dropped": ["flash_attn", "type_k", "type_v", "n_ubatch"]}
    line = opt._plan_line(plan)
    assert "flash_attn ×" in line
    assert "fp16" in line
    assert "已降级" in line
    assert "flash_attn/type_k/type_v/n_ubatch" in line


def test_plan_line_empty_is_blank(opt):
    assert opt._plan_line(None) == ""
    assert opt._plan_line({}) == ""


def test_plan_line_flags_cpu_only(opt):
    """没设 n_gpu_layers 就是纯 CPU —— 必须说出口，别让人以为在跑 GPU。"""
    assert "纯 CPU" in opt._plan_line({"n_gpu_layers": None, "n_batch": 2048})


# ---- 判据：只报能确定的 ----

def test_verdict_silent_when_healthy(opt):
    plan = {"mode": "fast", "flash_attn": True, "kv_quant": True}
    assert opt._llm_verdict(4.0, 4.6, 27.0, plan) == []


def test_verdict_reports_degradation(opt):
    plan = {"mode": "degraded", "flash_attn": False, "kv_quant": False}
    msgs = opt._llm_verdict(19.0, 20.0, 25.0, plan)
    assert any("降级" in m for m in msgs)


def test_verdict_reports_layer_spill(opt):
    """★ 显存增量远小于权重文件 = 有层丢在 CPU。这是掉层唯一的一眼判据。"""
    msgs = opt._llm_verdict(19.0, 8.0, 25.0, {"mode": "fast"})
    assert len(msgs) == 1
    assert "丢在 CPU" in msgs[0]


def test_verdict_reports_low_throughput(opt):
    msgs = opt._llm_verdict(None, None, 6.5, {"mode": "fast"})
    assert len(msgs) == 1
    assert "tok/s" in msgs[0]


def test_verdict_handles_missing_numbers(opt):
    """量不到就不报（宁可沉默，也不制造假警报）。"""
    assert opt._llm_verdict(None, None, None, None) == []
    assert opt._llm_verdict(None, None, None, {"mode": "fast"}) == []


# ---- _new_llama：两条路都要回传 plan ----

class _FakeLlama:
    """老轮子替身：见到加速参数就抛（模拟"不被认识"）。"""

    def __init__(self, **kw):
        self.kw = kw
        if "flash_attn" in kw:
            raise TypeError("unexpected keyword argument 'flash_attn'")
        self.closed = False

    def close(self):
        self.closed = True


class _NewFakeLlama:
    """新轮子替身：全部照收。"""

    def __init__(self, **kw):
        self.kw = kw

    def close(self):
        pass


@pytest.fixture()
def old_llama(monkeypatch):
    mod = types.ModuleType("llama_cpp")
    mod.Llama = _FakeLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", mod)
    return mod


@pytest.fixture()
def new_llama(monkeypatch):
    mod = types.ModuleType("llama_cpp")
    mod.Llama = _NewFakeLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", mod)
    return mod


def _kw():
    return {"model_path": "x.gguf", "flash_attn": True, "type_k": 1, "type_v": 1,
            "n_batch": 2048, "n_ubatch": 512, "n_gpu_layers": -1}


def test_new_llama_fast_path_returns_plan(opt, new_llama):
    """成功路径原来零输出 —— 现在必须回传「实际吃到了哪些项」。"""
    llm, plan = opt._new_llama(_kw())
    assert plan["mode"] == "fast"
    assert plan["flash_attn"] is True and plan["kv_quant"] is True
    assert plan["n_batch"] == 2048 and plan["n_ubatch"] == 512
    assert plan["n_gpu_layers"] == -1 and plan["dropped"] == []
    assert isinstance(llm, _NewFakeLlama)


def test_new_llama_degraded_path_strips_and_reports(opt, old_llama):
    llm, plan = opt._new_llama(_kw())
    assert plan["mode"] == "degraded"
    assert set(plan["dropped"]) == {"flash_attn", "type_k", "type_v", "n_ubatch"}
    assert plan["flash_attn"] is False and plan["kv_quant"] is False
    # 老轮子的 n_batch 兼作物理批，必须压回 512
    assert plan["n_batch"] == 512 and llm.kw["n_batch"] == 512
    assert "flash_attn" not in llm.kw and "type_k" not in llm.kw
    # n_gpu_layers 不是加速项，降级时不该被剥掉
    assert llm.kw["n_gpu_layers"] == -1


def test_new_llama_reraises_when_not_an_accel_problem(opt, old_llama):
    """权重文件不存在这类错误必须原样上抛，不能被降级分支吞掉。"""

    class _Boom:
        def __init__(self, **kw):
            raise FileNotFoundError("x.gguf")

    old_llama.Llama = _Boom
    with pytest.raises(FileNotFoundError):
        opt._new_llama({"model_path": "x.gguf", "n_batch": 2048})
