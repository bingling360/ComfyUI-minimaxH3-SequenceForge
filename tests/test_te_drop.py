"""te_drop_after_cond 回归：cond 出口释放 TE 驻留（perf 开关 + 代理挂接）。

跑法（仓库根目录）：
    python -m pytest tests/test_te_drop.py -q

背景（2026-09-30）：64GB 无 swap 云端实测，1MP 单段一采 cgroup 峰值 59.5/64G，
段间残留 +2G/段，第 3-4 段必撞顶被**整个容器**杀掉（断连 + 运行记录消失）。
修复 = cond 编码完就把 TE 权重副本丢掉（unload_model_and_clips，只动 TE 不碰
主模型暖驻留），采样/解码全程不背 25.9GB。挂接点 = CachedClipProxy（全链 TE
encode 唯一入口），只在 **miss** 时丢 —— hit 没碰 TE 权重，丢了白付回载。

钉住：
  1) perf 键存在、默认 "auto"、类型表收 bool 与字面量 "auto"；
  2) te_drop_after_cond_enabled 的解析矩阵（显式值 / auto 按 OS / 乱串回落 auto）；
  3) 代理只在 miss 丢、hit 不丢；
  4) 丢的动作真调 unload_model_and_clips 且传的是 clip.patcher；
  5) comfy 环境缺失时静默吞（缓存层任何情况下不该成为崩溃源）。
"""

import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import perf  # noqa: E402
import cond_cache as CC  # noqa: E402


class Unhashable:
    __hash__ = None


class FakeClip:
    """最小 clip 桩：记真实前向次数（结构照 test_cond_cache.py 的 FakeClip）。"""

    def __init__(self):
        self.encodes = 0
        self.patcher = object()   # 真实路径里会传给 unload_model_and_clips

    def tokenize(self, text, *a, **kw):
        return {"text": text}

    def encode_from_tokens_scheduled(self, tokens, *a, **kw):
        self.encodes += 1
        return [[Unhashable(), {"prompt": tokens.get("text", "")}]]


def _proxy(drop_after_encode, cap=32):
    clip = FakeClip()
    p = CC.CachedClipProxy(clip, capacity=cap, drop_after_encode=drop_after_encode)
    return clip, p


def _enc(p, text="你好"):
    return p.encode_from_tokens_scheduled(p.tokenize(text))


# ── 1) perf 键声明 ──

def test_perf_key_declared():
    assert perf.DEFAULT_PERF.get("te_drop_after_cond") == "auto"
    t = perf.PERF_TYPES.get("te_drop_after_cond")
    assert t is not None and bool in t and "auto" in t


# ── 2) 解析矩阵 ──

def test_enabled_explicit(monkeypatch):
    assert perf.te_drop_after_cond_enabled(True) is True
    assert perf.te_drop_after_cond_enabled(False) is False
    assert perf.te_drop_after_cond_enabled("true") is True
    assert perf.te_drop_after_cond_enabled("OFF") is False
    # 显式值优先于 OS：Windows 上写 true 也开
    monkeypatch.setattr(platform(), "system", lambda: "Windows")
    assert perf.te_drop_after_cond_enabled(True) is True


def test_enabled_auto_by_os(monkeypatch):
    monkeypatch.setattr(platform(), "system", lambda: "Linux")
    assert perf.te_drop_after_cond_enabled("auto") is True
    assert perf.te_drop_after_cond_enabled(None) is True
    monkeypatch.setattr(platform(), "system", lambda: "Windows")
    assert perf.te_drop_after_cond_enabled("auto") is False
    assert perf.te_drop_after_cond_enabled(None) is False


def test_enabled_garbage_falls_back_to_auto(monkeypatch):
    monkeypatch.setattr(platform(), "system", lambda: "Linux")
    # 乱串不是合法布尔 → 走 auto 分支（Linux → 开）
    assert perf.te_drop_after_cond_enabled("随手写的") is True


def platform():
    import platform
    return platform


# ── 3) 代理只在 miss 丢 ──
# ⚠ 命中判定按 tokens 对象身份走（id 保活表）：hit 测试必须**持有同一个 tokens**
# 重复编码 —— ComfyUI 真实流程也是「tokenize 一次、正负向共用」这个形态。


def test_drop_on_miss_only():
    clip, p = _proxy(True)
    calls = []
    p._drop_te = lambda: calls.append(1)      # 影子掉真实现，专注数拍子
    tok = p.tokenize("同一段提示词")
    _enc(p, tok)
    assert clip.encodes == 1 and len(calls) == 1      # miss → 丢
    _enc(p, tok)
    assert clip.encodes == 1 and len(calls) == 1      # hit → 不丢不重编码
    _enc(p, p.tokenize("另一段提示词"))
    assert clip.encodes == 2 and len(calls) == 2      # 新文本 miss → 再丢


def test_no_drop_when_disabled():
    clip, p = _proxy(False)
    calls = []
    p._drop_te = lambda: calls.append(1)
    tok = p.tokenize("你好")
    _enc(p, tok)
    _enc(p, tok)
    assert clip.encodes == 1 and len(calls) == 0


# ── 4) 丢的动作 = unload_model_and_clips(clip.patcher) ──

def test_drop_calls_unload_model_and_clips(monkeypatch):
    calls = []
    fake_mm = types.ModuleType("comfy.model_management")
    fake_mm.unload_model_and_clips = lambda patcher, **kw: calls.append(patcher)
    fake_comfy = types.ModuleType("comfy")
    fake_comfy.model_management = fake_mm
    monkeypatch.setitem(sys.modules, "comfy", fake_comfy)
    monkeypatch.setitem(sys.modules, "comfy.model_management", fake_mm)

    clip, p = _proxy(True)
    _enc(p, "你好")
    assert calls == [clip.patcher]
    assert p.drops == 1


# ── 5) 无 comfy 环境：静默吞，不炸编码链 ──

def test_drop_swallows_missing_comfy(monkeypatch):
    monkeypatch.setitem(sys.modules, "comfy", None)   # import comfy 必然失败
    clip, p = _proxy(True)
    out = _enc(p, "你好")                              # 不该抛
    assert clip.encodes == 1 and p.drops == 0
    assert isinstance(out, list) and out
