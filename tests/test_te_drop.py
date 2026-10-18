"""te_drop_after_cond 回归：cond 出口释放 TE 驻留（perf 开关 + 代理挂接）。

跑法（仓库根目录）：
    python -m pytest tests/test_te_drop.py -q

背景（2026-09-30）：64GB 无 swap 云端实测，1MP 单段一采 cgroup 峰值 59.5/64G，
段间残留 +2G/段，第 3-4 段必撞顶被**整个容器**杀掉（断连 + 运行记录消失）。
当初的设计意图 = cond 编码完就把 TE 请出去（`unload_model_and_clones`，只动 TE
不碰主模型暖驻留）。挂接点 = CachedClipProxy（全链 TE encode 唯一入口），
只在 **miss** 时丢 —— hit 没碰 TE 权重，丢了白付回载。

⚠ 2026-10-13 修复：这个开关**直到 10-13 之前一直是空转的** —— 原实现调的是
ComfyUI 里从未存在的 `unload_model_and_clips`，异常又被 `except` 吞掉，
所以它什么都没干、连 `drops` 都不会加。现已改调真名并出声告警。

⛔ 2026-10-13 实测结论：**它不省内存 —— 内存占用无变化。**
`unload_model_and_clones` 走的是 `free_memory(..., for_dynamic=False)` →
`LoadedModel.model_unload()` → `ModelPatcher.detach(unpatch_weights=True)`：
把权重 `model_patches_to(offload_device)` **从显存搬回主机**、从
`current_loaded_models` 摘掉、再 `soft_empty_cache()`。
**ComfyUI 的「卸载」= 从显存搬回内存，不是从内存删掉** → 那 25.9GB 权重副本
该在还在，内存地板一点不动。所以它的真实收益是**腾显存窗口**（小显存卡有意义），
代价是每段首次编码多一次回载 + 重 patch（约 45s）。
当初「默认强制开」是为了救云端内存，该理由已被这条实测推翻。

⚠ 2026-10-18：`te_drop_after_cond` **这个 perf 键已删除** —— 卸载收敛成
`unload_everywhere` 一个开关后，全仓再没有读取方（tests/test_perf_settings.py::
test_no_key_in_default_perf_is_unread 就是这么红的）。所以本文只保留**机制侧**
（cond 出口丢 TE 这件事本身还在，只是由 `unload_everywhere` 驱动），
不再断言那个键与它的三态解析。

钉住：
  3) 代理只在 miss 丢、hit 不丢；
  4) 丢的动作真调 `unload_model_and_clones` 且传的是 clip.patcher；
  5) comfy 环境缺失时静默吞（缓存层任何情况下不该成为崩溃源）；
  6) 源码里不许再出现 ComfyUI 不存在的 API 名（AST 守卫）。
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
        self.patcher = object()   # 真实路径里会传给 unload_model_and_clones

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


# ── 4) 丢的动作 = unload_model_and_clones(clip.patcher) ──
# ⚠ 2026-10-13：老测试 mock 的是 `unload_model_and_clips` —— ComfyUI 里**从未
# 存在**这个名字（真名 `unload_model_and_clones`）。mock 掉被测依赖的后果是：
# 测试只证明了「我们调了这个名字」，没证明「这个名字存在」，于是源码静默失效
# 了很久而测试一直绿。**这里改成真名**，并由下面 test_source_never_names_a_phantom_api
# 守住「不许再写不存在的名字」。

def test_drop_calls_unload_model_and_clones(monkeypatch):
    calls = []
    fake_mm = types.ModuleType("comfy.model_management")
    fake_mm.unload_model_and_clones = lambda patcher, **kw: calls.append(patcher)
    fake_comfy = types.ModuleType("comfy")
    fake_comfy.model_management = fake_mm
    monkeypatch.setitem(sys.modules, "comfy", fake_comfy)
    monkeypatch.setitem(sys.modules, "comfy.model_management", fake_mm)

    clip, p = _proxy(True)
    _enc(p, "你好")
    assert calls == [clip.patcher]
    assert p.drops == 1


# ── 4b) 反回归：源码里不许出现 ComfyUI 不存在的 comfy API 名 ──
# 为什么值得单开一条：这次的事故是「函数名写错 + except 吞异常」，单测形态下
# （mock 一切）永远发现不了。**只能靠静态扫源码**。

def test_source_never_names_a_phantom_api():
    """用 AST 查**真实调用点**（注释/文档里提这个名字不算，反而应该被允许）。"""
    import ast

    path = os.path.join(ROOT, "cond_cache.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    bad = [n for n in ast.walk(tree)
           if isinstance(n, ast.Attribute) and n.attr == "unload_model_and_clips"]
    assert not bad, (
        f"cond_cache.py 第 {[n.lineno for n in bad]} 行调用了 "
        "unload_model_and_clips —— ComfyUI 里不存在这个名字（真名 "
        "unload_model_and_clones）。老实现这么写 → AttributeError 被 "
        "except 吞 → te_drop_after_cond 静默失效")


def test_drop_warns_once_when_api_missing(monkeypatch, capsys):
    """comfy 有、但 API 不在（比如 ComfyUI 改版改名）→ 必须出声，不能静默。"""
    fake_mm = types.ModuleType("comfy.model_management")   # 故意不挂任何 unload_*
    fake_comfy = types.ModuleType("comfy")
    fake_comfy.model_management = fake_mm
    monkeypatch.setitem(sys.modules, "comfy", fake_comfy)
    monkeypatch.setitem(sys.modules, "comfy.model_management", fake_mm)
    CC._DROP_WARNED.clear()

    clip, p = _proxy(True)
    _enc(p, "你好")
    _enc(p, "再一段")
    out = capsys.readouterr().out
    assert p.drops == 0                       # 失败就是失败，不伪装成功
    assert out.count("[H3性能] te_drop_after_cond 未生效") == 1   # 但只报一次，不刷屏


# ── 5) 无 comfy 环境：静默吞，不炸编码链 ──

def test_drop_swallows_missing_comfy(monkeypatch):
    monkeypatch.setitem(sys.modules, "comfy", None)   # import comfy 必然失败
    clip, p = _proxy(True)
    out = _enc(p, "你好")                              # 不该抛
    assert clip.encodes == 1 and p.drops == 0
    assert isinstance(out, list) and out
