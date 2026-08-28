"""CLIP 文本编码透明缓存代理（cond text-encode cache）。

TE 的 tokenize+encode 只依赖提示词文本，与画布分辨率、参考素材无关——
一采与二采在各自分辨率下重建条件时，同一段提示词会把大体积文本编码器
（H3 为 25.9GB 级）重复前向一遍。本代理包装 clip，按提示词文本 LRU 缓存
encode 结果：二采与重复提示词段直接命中，参考图/视频的 VAE 编码等其余
属性全部透传原 clip，行为零变化。

只在 H3SeamlessChainSampler.execute 入口包一层（插件内部调用链生效），
ComfyUI 画布其他节点不受影响。缓存返回共享 cond 对象：ComfyUI 的
conditioning_set_values / _apply_guide 等均为写时复制，不会原地改动
cond，共享安全。tokenize 每次照常执行（纯 CPU、微秒级），缓存只在
encode 层命中。
"""


def _hashable(v):
    try:
        hash(v)
    except TypeError:
        return False
    return True


def _text_key(text):
    """提示词 -> 可哈希缓存键；不可哈希（罕见复合输入）返回 None 走旁路。"""
    if isinstance(text, str):
        return text
    if _hashable(text):
        return text
    try:
        key = repr(text)
        hash(key)
        return key
    except Exception:
        return None


def _misc_key(args, kwargs):
    """encode 额外位置/关键字参数 -> 可哈希键；含不可哈希项返回 None（不缓存）。"""
    items = list(args) + sorted(kwargs.items(), key=lambda kv: kv[0])
    for v in items:
        if not _hashable(v):
            return None
    return tuple(items) if items else ()


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


def _copy_cond(cond):
    """cond 浅拷贝（list 与 extra dict 复制、张量共享）：命中返回副本，防
    下游在 cond 上原地写字段污染缓存（ComfyUI 惯例是写时复制，此处兜底）。"""
    try:
        return [(t[0], dict(t[1])) if isinstance(t, tuple) and len(t) == 2
                and isinstance(t[1], dict) else t for t in cond]
    except TypeError:
        return cond
