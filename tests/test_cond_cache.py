"""cond 文本编码缓存的键边界守卫（纯 stdlib，managed python 可跑）。

这一层是**性能与正确性的交界**：把键放宽能让二采少算一次文本编码器前向（H3 的
TE 是 25.3GB 级），但**放错一样东西**就会让某一段拿到另一段的 cond。所以这里不只
验「能命中」，更重要的是验「什么必须旁路」：

  · 空表参数（`images=[]`）→ 该命中（修复目标）
  · 含张量的参数（`images=[图]`）→ **必须**旁路（一采/二采的图被 resize 到不同画幅，
    视觉 token 数不同，cond 真的不同）
  · 可哈希的自定义对象 → **也必须**旁路（默认对象哈希基于 id，回收后新对象可能
    复用同一 id → 两个内容不同的对象算出同一个键 → 错误命中，拿到上一段的 cond）
  · 命中返回的副本必须**深入嵌套容器**（`minimax_keyframes` 是 extra dict 里的
    list；浅拷贝会让一采贴的锚跑进二采的 cond）

背景实测与理由见 `cond_cache.py` 顶部「键的边界」一节。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cond_cache as CC  # noqa: E402


class Unhashable:
    """模拟 torch.Tensor：不可哈希。"""

    __hash__ = None


class HashableObj:
    """可哈希的自定义对象 —— 走 hash() 也能过，但**必须**旁路（见文件头说明）。"""


class FakeClip:
    """最小 clip 桩：记真实前向次数；cond 结构照官方 = [[张量, extra dict]]。"""

    def __init__(self):
        self.encodes = 0

    def tokenize(self, text, *a, **kw):
        return {"text": text, "kw": kw}

    def encode_from_tokens_scheduled(self, tokens, *a, **kw):
        self.encodes += 1
        return [[Unhashable(), {"prompt": tokens["text"], "minimax_keyframes": []}]]

    def encode_from_tokens(self, tokens, *a, **kw):
        self.encodes += 1
        return [[Unhashable(), {"prompt": tokens["text"]}]]


def _proxy(capacity=32):
    clip = FakeClip()
    return clip, CC.CachedClipProxy(clip, capacity=capacity)


def _enc(p, text, **kw):
    return p.encode_from_tokens_scheduled(p.tokenize(text, **kw))


# ---- 该命中的 ----

def test_empty_list_param_now_hits():
    """`images=[]`（官方 t2v / 续拍段就是这么传的）必须能进缓存。

    这是整次修复的目标：修前因为 `hash([])` 抛 TypeError 而全部旁路。
    """
    clip, p = _proxy()
    a = _enc(p, "同一段提示词", images=[])
    assert clip.encodes == 1 and p.hits == 0
    b = _enc(p, "同一段提示词", images=[])
    assert clip.encodes == 1, "第二次该命中，不该再算一次文本编码器"
    assert p.hits == 1 and p.last_encode_hit is True
    assert a is not b


def test_no_param_call_still_hits():
    """无额外参数（nodes 里负向空串那条）：修前也能命中，别被改坏。"""
    clip, p = _proxy()
    _enc(p, "")
    _enc(p, "")
    assert clip.encodes == 1 and p.hits == 1


def test_dict_key_order_ignored():
    """kwargs 的**键序**不影响键：同内容必须算出同一个键（否则白白少命中）。"""
    clip, p = _proxy()
    _enc(p, "p", a=1, b=2)
    _enc(p, "p", b=2, a=1)
    assert clip.encodes == 1, "键序不同的同一组参数该命中"


def test_scalar_only_ref_items_hit():
    """纯标量结构的 ref_items（如只有音频引用）→ 两次输入真的相同 → 该命中。"""
    clip, p = _proxy()
    items = [{"type": "audio"}]
    _enc(p, "p", minimax_ref_items=list(items))
    _enc(p, "p", minimax_ref_items=list(items))
    assert clip.encodes == 1


# ---- 必须旁路的 ----

def test_tensor_param_still_bypasses():
    """含张量的参数必须旁路 —— 一采/二采的图不同，复用了就是错的。"""
    clip, p = _proxy()
    _enc(p, "同一段", images=[Unhashable()])
    _enc(p, "同一段", images=[Unhashable()])
    assert clip.encodes == 2, "含张量的调用不许进缓存"
    assert p.hits == 0


def test_custom_hashable_object_bypasses():
    """可哈希的自定义对象也必须旁路。

    它是「用 hash() 放行」那条路的致命伤：对象默认哈希基于 id，第一个对象回收后
    第二个对象很可能拿到同一个 id → 两个内容不同的对象算出同一个键 → 错误命中。
    所以白名单只认精确的标量类型。
    """
    clip, p = _proxy()
    _enc(p, "同一段", x=HashableObj())
    _enc(p, "同一段", x=HashableObj())
    assert clip.encodes == 2 and p.hits == 0


def test_bool_and_int_do_not_share_key():
    """`True` 与 `1` 在 Python 里相等，但键里必须分开（键带类型名就是为了这个）。"""
    clip, p = _proxy()
    _enc(p, "p", flag=True)
    _enc(p, "p", flag=1)
    assert clip.encodes == 2, "True 与 1 被当成同一个键了"


def test_mixed_list_with_tensor_bypasses():
    """列表里只要**有一个**不可归一化的项，整个键就算不出来 → 旁路。"""
    clip, p = _proxy()
    _enc(p, "p", items=["标量", Unhashable()])
    _enc(p, "p", items=["标量", Unhashable()])
    assert clip.encodes == 2


def test_self_referencing_structure_does_not_crash():
    """自引用结构（`a=[]; a.append(a)`）必须被深度上限挡住 —— 旁路，且**不许抛异常**。

    缓存层是性能优化，任何输入都不该让它把渲染拖崩。
    """
    clip, p = _proxy()
    a = []
    a.append(a)
    _enc(p, "p", x=a)
    _enc(p, "p", x=a)
    assert clip.encodes == 2 and p.hits == 0


def test_non_str_dict_key_bypasses():
    """非 str 的 dict 键 → 旁路（对象 repr 可能含 id 不稳定；也避免 sorted 抛错）。"""
    clip, p = _proxy()
    _enc(p, "p", x={1: "a"})
    _enc(p, "p", x={1: "a"})
    assert clip.encodes == 2 and p.hits == 0


def test_different_prompts_do_not_share():
    """不同提示词绝不共键（这是缓存最基本的一条）。"""
    clip, p = _proxy()
    _enc(p, "段1的提示词", images=[])
    _enc(p, "段2的提示词", images=[])
    assert clip.encodes == 2 and p.hits == 0


def test_foreign_tokens_bypass():
    """不是本代理 tokenize 出来的 tokens → 旁路直通（任何未预期路径行为不变）。"""
    clip, p = _proxy()
    p.encode_from_tokens_scheduled({"text": "外部造的", "kw": {}})
    p.encode_from_tokens_scheduled({"text": "外部造的", "kw": {}})
    assert clip.encodes == 2 and p.hits == 0


def test_encode_methods_do_not_share():
    """`encode_from_tokens` 与 `..._scheduled` 是两个不同入口，不许共键。"""
    clip, p = _proxy()
    p.encode_from_tokens(p.tokenize("p"))
    p.encode_from_tokens_scheduled(p.tokenize("p"))
    assert clip.encodes == 2


# ---- 副本隔离（命中安全性的核心）----

def test_hit_copy_isolates_nested_list():
    """命中副本必须与其它副本、以及缓存原件**完全隔离**到嵌套层。

    回归 2026-09-24 实测到的问题：旧的浅拷贝让 `minimax_keyframes` 这个 list 在
    缓存原件和所有副本之间共享 —— 一采贴的锚会跑进二采（以及后续所有段）的 cond。
    """
    clip, p = _proxy()
    a = _enc(p, "同一段", images=[])
    b = _enc(p, "同一段", images=[])          # 命中
    assert clip.encodes == 1
    a[0][1]["minimax_keyframes"].append({"seg": "一采的锚"})
    assert b[0][1]["minimax_keyframes"] == [], "两份副本共享了 keyframes 列表"
    c = _enc(p, "同一段", images=[])          # 再取一次
    assert c[0][1]["minimax_keyframes"] == [], "缓存原件被上一次的注入污染了"
    assert a[0][1]["minimax_keyframes"] == [{"seg": "一采的锚"}], "改到的该是自己那份"


def test_copy_shares_tensor_not_deepcopies():
    """副本里的**张量**必须是同一个对象（deepcopy 会复制张量 → 显存爆）。"""
    clip, p = _proxy()
    a = _enc(p, "同一段", images=[])
    b = _enc(p, "同一段", images=[])
    assert a[0][0] is b[0][0], "张量被复制了（应该共享）"
    assert a[0][1] is not b[0][1], "extra dict 该是副本"


def test_nested_dict_in_extra_is_copied_too():
    """extra dict 里嵌套的 dict 也要复制（不只 list）。"""
    clip, p = _proxy()
    clip_res = _enc(p, "同一段")
    clip_res[0][1]["nested"] = {"inner": [1, 2]}
    again = _enc(p, "同一段")
    assert "nested" not in again[0][1], "嵌套结构没隔离"


# ---- 容量 ----

def test_lru_evicts_oldest():
    """容量满后淘汰最旧的（同一容量下反复切换提示词不该无限增长）。"""
    clip, p = _proxy(capacity=2)
    _enc(p, "A")
    _enc(p, "B")
    _enc(p, "C")          # 挤掉 A
    _enc(p, "A")          # A 已被淘汰 → 重新算
    assert clip.encodes == 4
    assert len(p._cond_cache) == 2
