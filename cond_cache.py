"""CLIP 文本编码透明缓存代理（cond text-encode cache）。

TE 的 tokenize+encode 只依赖提示词文本，与画布分辨率、参考素材无关——
一采与二采在各自分辨率下重建条件时，同一段提示词会把大体积文本编码器
（H3 为 25.9GB 级）重复前向一遍。本代理包装 clip，按提示词文本 LRU 缓存
encode 结果：二采与重复提示词段直接命中，参考图/视频的 VAE 编码等其余
属性全部透传原 clip，行为零变化。

只在 H3SeamlessChainSampler.execute 入口包一层（插件内部调用链生效），
ComfyUI 画布其他节点不受影响。tokenize 每次照常执行（纯 CPU、微秒级），
缓存只在 encode 层命中。**命中返回的一定是独立副本**（见 `_copy_cond`）——
不依赖调用方"写时复制"的自觉：cond 的 extra dict 与其中嵌套的 list/dict
都会复制，张量共享。

═══ 键的边界（2026-09-24 修；这一节就是安全性的全部）═══

**旧实现**要求「tokenize 的额外参数**整体**可哈希」，而官方 MiniMax H3 节点固定
传一个 list（i2v 的 `images=`、ref2va 的 `minimax_ref_items=`）—— **空表也传**
（`hash([])` 抛 TypeError）→ 全部旁路、**命中恒为 0**。

**现在**改成类型白名单归一化（`_norm`）：只放行 None / bool / int / float / str /
bytes，以及由它们递归组成的 list / tuple / dict；其余（张量、任意对象）一律返回
None = 该次调用**不进缓存**。于是：

    images=[]                     → 可归一化 → **能命中**（这就是修复目标）
    images=[张量]                 → 含张量 → 旁路（**必须**旁路：一采与二采的图被
                                    `_resize` 到不同画幅、视觉 token 数不同，cond 真的不同）
    minimax_ref_items=[{…张量…}]  → 旁路；纯标量项（如 `{"type": "audio"}`）→ 能命中
    自定义对象                    → 旁路。**不能用 hash() 放行**：默认对象哈希基于
                                   id，对象回收后新对象可能复用同一 id → 两个内容不同
                                   的对象算出同一个键 → **错误命中**，返回上一段的 cond

标量带类型名（`("bool", True)` vs `("int", 1)`）是为了挡住 `True == 1` 这类跨类型
相等 —— 否则 `{"flag": True}` 与 `{"flag": 1}` 会算出同一个键。

**实测**（同一份代码、只把键判定换成旧/新两版；假 clip 复刻官方调用形态，
3 段 × 一采+二采 = 6 次 encode 请求，数字 = 文本编码器真前向次数）：

    调用形态                                     旧键   新键
    无额外参数（负向空串）                         3      3     （本来就命中，未变）
    官方 i2v    images=[]（无首帧图 / 续拍段）      6      3  ★ 每段省 1 次
    官方 ref2va minimax_ref_items=[纯标量项]        6      3  ★ 每段省 1 次
    官方 i2v    images=[张量]（挂了首帧图）         6      6     本该旁路
    官方 ref2va minimax_ref_items=[{…张量…}]        6      6     本该旁路

也就是：**无首帧图、无参考素材**的段（独立镜头、续拍段），二采那次不再重算文本
编码器；挂了首帧图或参考素材的段仍然旁路（正确行为 —— 它们的输入真的不同）。

═══ 与 latent 锚定的关系（2026-09-24 复核）═══

上段尾帧桥 / 尾锚 / 首帧头锚都是在 **encode 之后**通过
`conditioning_set_values(cond, {"minimax_keyframes": …})` 注入的（nodes.py 的
`_inject_keyframes`），**不进缓存键**。所以同一段提示词的多次 encode 可以共享一个
纯文本 cond 原件，各自贴各自的锚。

⚠ 前提是每方都拿到**独立副本** —— 见 `_copy_meta`：extra dict 里嵌套的 list/dict
也必须复制。旧的浅拷贝让 `minimax_keyframes` 这个 list 在缓存原件与所有副本之间
共享，谁原地 append 谁就污染其它段（实测：一采贴的锚会跑进二采的 cond 里）。
张量一律共享（`copy.deepcopy` 会把张量也复制一份，显存直接爆，**不能用**）。
"""


# 标量白名单：类型 -> 键里用的类型名。
# 为什么带类型名：Python 里 `True == 1`、`1 == 1.0` 都成立，若直接把值塞进键，
# `{"flag": True}` 与 `{"flag": 1}` 会算出同一个键 → 错误命中。
# 只认**精确类型**（`type(v) in ...`）：子类（IntEnum 之类）不在白名单 → 旁路。
# 宁可少命中，也不冒「把不认识的东西当标量」的险。
_SCALARS = {bool: "b", int: "i", float: "f", str: "s", bytes: "y"}


def _norm(v, _depth=0):
    """值 -> 「可安全比较的键」；不可归一化返回 None（该次调用不进缓存）。

    放行：None / bool / int / float / str / bytes，及由它们**递归组成**的
    list / tuple / dict。其余（张量、任意对象、子类）一律 None → 旁路，
    理由见文件头「键的边界」一节（尤其是「不能用 hash() 放行」那条）。

    深度上限：递归到 12 层就放弃（返回 None = 不缓存）。它挡的是**自引用结构**
    （`a = []; a.append(a)`）—— 没有这条会直接 RecursionError 冒到渲染链上。
    缓存层是性能优化，**任何情况下都不该成为崩溃源**。
    """
    if _depth > 12:
        return None
    if v is None:
        return ("n",)
    t = type(v)
    if t in _SCALARS:
        return (_SCALARS[t], v)
    if isinstance(v, (list, tuple)):
        parts = []
        for x in v:
            n = _norm(x, _depth + 1)
            if n is None:
                return None
            parts.append(n)
        return ("q", tuple(parts))
    if isinstance(v, dict):
        if not all(isinstance(k, str) for k in v):
            return None      # 非 str 键不拿 repr 兜底：对象 repr 可能含 id → 不稳定；
                             # 也顺带挡住 `sorted()` 在混合类型键上抛 TypeError
        parts = []
        for k in sorted(v):  # 键序无关：{a,b} 与 {b,a} 必须算出同一个键
            n = _norm(v[k], _depth + 1)
            if n is None:
                return None
            parts.append((k, n))
        return ("m", tuple(parts))
    return None


def _text_key(text):
    """提示词 -> 缓存键；不可归一化返回 None（旁路）。"""
    return _norm(text)


def _misc_key(args, kwargs):
    """额外位置/关键字参数 -> 缓存键；含不可归一化项返回 None（不缓存）。"""
    return _norm((tuple(args), dict(kwargs)))


class CachedClipProxy:
    """clip 透明代理：tokenize 记账 + encode_from_tokens(_scheduled) 结果缓存。

    命中判定：encode 收到的 tokens 必须来自本代理的 tokenize（通过保活表
    id -> (tokens, 键) 关联，对象存活期间 id 不复用）；外部直接传入的
    tokens 一律旁路直通，保证任何未预期调用路径行为不变。
    hits/misses/last_encode_hit 供计时报告标注「TE命中/未命中」。
    """

    def __init__(self, clip, capacity=32):
        self._clip = clip
        self._cap = max(1, int(capacity))
        self._cond_cache = {}   # (方法, 提示词键, 调用参数键) -> cond，dict 保序做 LRU
        self._tok_alive = {}    # id(tokens) -> (tokens, 键)   强引用保活防 id 复用
        self.hits = 0
        self.misses = 0
        self.last_encode_hit = None

    def __getattr__(self, name):
        return getattr(self._clip, name)

    def tokenize(self, text, *args, **kwargs):
        tokens = self._clip.tokenize(text, *args, **kwargs)
        key = _text_key(text)
        misc = _misc_key(args, kwargs)
        if key is not None and misc is not None:
            self._tok_alive[id(tokens)] = (tokens, (key, misc))
            if len(self._tok_alive) > self._cap * 2:
                for k in list(self._tok_alive)[:self._cap]:
                    del self._tok_alive[k]
        return tokens

    def encode_from_tokens_scheduled(self, tokens, *args, **kwargs):
        return self._encode("sched", self._clip.encode_from_tokens_scheduled,
                            tokens, args, kwargs)

    def encode_from_tokens(self, tokens, *args, **kwargs):
        return self._encode("plain", self._clip.encode_from_tokens,
                            tokens, args, kwargs)

    def _encode(self, method, fn, tokens, args, kwargs):
        entry = self._tok_alive.get(id(tokens))
        cache_key = None
        if entry is not None:
            misc = _misc_key(args, kwargs)
            if misc is not None:
                cache_key = (method, entry[1], misc)
        if cache_key is not None and cache_key in self._cond_cache:
            self.hits += 1
            self.last_encode_hit = True
            cond = self._cond_cache.pop(cache_key)
            self._cond_cache[cache_key] = cond   # LRU 触碰：移到最新
            return _copy_cond(cond)
        cond = fn(tokens, *args, **kwargs)
        self.misses += 1
        self.last_encode_hit = False
        if cache_key is not None:
            self._cond_cache[cache_key] = cond
            while len(self._cond_cache) > self._cap:
                self._cond_cache.pop(next(iter(self._cond_cache)))
            return _copy_cond(cond)   # 存原件返回副本：调用方永不持有缓存内部引用
        return cond


def _copy_meta(v):
    """递归复制**容器**、共享**张量**。

    为什么不用 `copy.deepcopy`：cond 里最大的对象是张量，deepcopy 会把它们各复制
    一份（显存直接爆）。这里只重建 dict / list / tuple 这三层结构，张量与其它对象
    原样返回 —— 结构是小的（keyframes 列表等），张量是大的，正好各取所需。
    """
    if isinstance(v, dict):
        return {k: _copy_meta(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_copy_meta(x) for x in v]
    if isinstance(v, tuple):
        return tuple(_copy_meta(x) for x in v)
    return v


def _copy_cond(cond):
    """cond 副本：外层结构 + extra dict **及其嵌套容器**都复制，张量共享。

    为什么必须复制到嵌套层：`minimax_keyframes` 是 extra dict 里的一个 **list**。
    浅拷贝（只 `dict(t[1])`）会让缓存原件与每一份副本共享同一个 list —— 谁往里面
    append，其它段拿到的 cond 就跟着变（实测：一采贴的锚会跑进二采的 cond）。
    ComfyUI 惯例虽是写时复制（`conditioning_set_values` 返回新对象），但那是调用方
    的自觉；缓存层不该把正确性寄托在别人的写法上。
    """
    try:
        return [(t[0], _copy_meta(t[1])) if isinstance(t, tuple) and len(t) == 2
                and isinstance(t[1], dict) else _copy_meta(t) for t in cond]
    except TypeError:
        return cond
