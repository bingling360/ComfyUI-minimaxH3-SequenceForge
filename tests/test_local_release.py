"""本地优化通道：用完即卸 + 思维链剥离（2026-09-20）。

两处「能跑但结果是错的」型问题，肉眼看不出，只能靠断言钉住：

1. **句柄只进不出**：`_GGUF_CACHE` / `_TF_CACHE` 是模块级字典，写进去就再没人删 →
   27B 权重带着 KV 常驻显存，而 ComfyUI 的 `unload_all_models()` 管不到 llama.cpp
   （ggml 自己的显存池）→ 后续 H3 采样的空闲显存凭空少一大块。
2. **思维链进正文**：云通道的思考在 `reasoning_content` 里，本地通道拿到的是
   「思考 + 正文」一整块 `content`，原样返回 → 编辑器里出现整段思考
   （实测 3100 字思考 / 1100 字正文）。

本文件用桩 `llama_cpp` 把 GGUF 路径整条跑通 —— 本机没装 llama-cpp-python，
也不该为了跑测试去装它。
"""
import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


optimizer = _load("h3optimizer_local", "optimizer.py")


class _FakeLlama:
    """桩句柄：记下构造参数与调用，close() 必须被调到（显存归还的唯一通路）。"""

    instances: list = []
    reply = ""
    boom = False

    def __init__(self, **kw):
        self.kw = kw
        self.closed = False
        self.messages = None
        _FakeLlama.instances.append(self)

    def create_chat_completion(self, messages, max_tokens, temperature):
        self.messages = messages
        if _FakeLlama.boom:
            raise RuntimeError("模拟生成失败")
        return {"choices": [{"message": {"content": _FakeLlama.reply}}]}

    def close(self):
        self.closed = True


@pytest.fixture
def fake_llama(monkeypatch):
    pkg = types.ModuleType("llama_cpp")
    pkg.Llama = _FakeLlama
    fmt = types.ModuleType("llama_cpp.llama_chat_format")
    fmt.Llava15ChatHandler = lambda **kw: {"mmproj": kw}
    pkg.llama_chat_format = fmt
    monkeypatch.setitem(sys.modules, "llama_cpp", pkg)
    monkeypatch.setitem(sys.modules, "llama_cpp.llama_chat_format", fmt)
    _FakeLlama.instances = []
    _FakeLlama.reply = ""
    _FakeLlama.boom = False
    return pkg


def _cfg(**over):
    d = {"mode": "local", "local_model": "qwen3.5-27b-Q4_K_M.gguf",
         "local_device": "cuda", "read_media": False, "max_tokens": 16384,
         "thinking": "disabled"}
    d.update(over)
    return optimizer.normalize_config(d)


def _user_text(inst):
    """取 user 消息里的正文（content 是 [{type:text,text:…}, …]）。"""
    return inst.messages[1]["content"][0]["text"]


def test_module_has_no_handle_cache():
    """句柄缓存整体删掉：模式 A（用完即卸）下它只会是"写一条、紧接着删掉"的死代码。"""
    assert not hasattr(optimizer, "_GGUF_CACHE")
    assert not hasattr(optimizer, "_TF_CACHE")


@pytest.mark.parametrize("raw,want", [
    ("<think>想很久</think>时长：5.2秒\n镜头一：特写", "时长：5.2秒\n镜头一：特写"),
    ("思考正文没开标签</think>时长：5秒", "时长：5秒"),
    ("<think>思考被截断、没有闭合标签", ""),
    ("时长：5.2秒（压根没思考）", "时长：5.2秒（压根没思考）"),
    ("<think>a</think>正文A<think>b</think>", "正文A"),
])
def test_strip_think_forms(raw, want):
    assert optimizer._strip_think(raw) == want


def test_local_gguf_releases_handle_and_strips_think(fake_llama):
    _FakeLlama.reply = "<think>想很久</think>时长：5.2秒\n镜头一：特写"
    out = optimizer._local_generate(_cfg(), "SYS", "USER", [])
    assert out == "时长：5.2秒\n镜头一：特写"
    inst = _FakeLlama.instances[0]
    assert inst.closed is True, "生成结束必须关句柄，否则显存不归还"


def test_local_gguf_closes_handle_even_on_failure(fake_llama):
    _FakeLlama.boom = True
    with pytest.raises(RuntimeError, match="模拟生成失败"):
        optimizer._local_generate(_cfg(), "SYS", "USER", [])
    assert _FakeLlama.instances[0].closed is True


def test_local_gguf_rejects_think_only_reply(fake_llama):
    """思考吃光预算、正文一个字没出来 → 必须报错，不能把半段思考当提示词填进编辑器。"""
    _FakeLlama.reply = "<think>想啊想啊想啊"
    with pytest.raises(RuntimeError, match="思维链"):
        optimizer._local_generate(_cfg(), "SYS", "USER", [])
    assert _FakeLlama.instances[0].closed is True


def test_local_thinking_disabled_injects_no_think(fake_llama):
    """本地没有 API 那种 thinking 字段，关思考只能靠 Qwen 系的 /no_think 软开关。"""
    _FakeLlama.reply = "正文"
    optimizer._local_generate(_cfg(thinking="disabled"), "SYS", "USER", [])
    assert _user_text(_FakeLlama.instances[-1]).endswith("/no_think")

    optimizer._local_generate(_cfg(thinking="enabled"), "SYS", "USER", [])
    assert "/no_think" not in _user_text(_FakeLlama.instances[-1])


def test_local_n_ctx_covers_budget_and_device_auto_uses_gpu(fake_llama):
    _FakeLlama.reply = "正文"
    optimizer._local_generate(_cfg(max_tokens=16384), "SYS", "USER", [])
    kw = _FakeLlama.instances[-1].kw
    assert kw["n_ctx"] >= 16384 + 8192, "窗口必须装得下提示词 + 生成"
    assert kw["n_ctx"] % 512 == 0
    assert kw["n_gpu_layers"] == -1

    # auto 也要上 GPU：llama-cpp-python 不设 n_gpu_layers 就是 0 层 = 纯 CPU
    optimizer._local_generate(_cfg(local_device="auto"), "SYS", "USER", [])
    assert _FakeLlama.instances[-1].kw["n_gpu_layers"] == -1

    optimizer._local_generate(_cfg(local_device="cpu"), "SYS", "USER", [])
    assert "n_gpu_layers" not in _FakeLlama.instances[-1].kw
