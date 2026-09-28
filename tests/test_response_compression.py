"""响应体压缩中间件的两条绊线（防止重犯两次已踩过的坑）。

跑法（仓库根目录）：
    python -m pytest tests/test_response_compression.py -q

这里**故意用源码级断言**（不是行为测试）：`routes.py` 顶层 `import aiohttp`，要真跑就得
把 aiohttp 大半 stub 出来，而这两条要防的恰恰是「写法」而不是「逻辑」——
拦的是"有人把中间件表整体换掉""有人把 Vary 删了"，源码级最直接、最不容易漏。
（与 `tests/js/structured_removed_check.js` 同类：绊线，不是证明。）

背景两件事，都是实测摔过的：
  1. 写成 `app._middlewares = (fn,) + tuple(...)` → aiohttp 起服务时
     `pre_freeze() -> self._middlewares.freeze()` 抛 `AttributeError: 'tuple' object
     has no attribute 'freeze'` → **ComfyUI 直接起不来**（进程消失、端口不再监听）。
     正确写法是**原地 append**（FrozenList 支持），和官方 `--enable-compress-response-body`
     一样。
  2. 官方 `server.compress_body` **不设 `Vary: Accept-Encoding`**。压缩与否取决于请求头，
     响应就必须声明同一 URL 会因该头而不同 —— 否则中间一层会缓存的代理（租卡平台前面
     必有）可能把 gzip 那份发给没声明支持的客户端，浏览器拿到二进制，前端解析
     `/object_info` 直接炸。本机直连永远遇不到。
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "routes.py"), encoding="utf-8").read()


def test_middlewares_is_mutated_in_place_never_replaced():
    """中间件表只许原地追加 —— 整体赋值会让 ComfyUI 起不来。"""
    assert "app._middlewares = " not in SRC, (
        "不要给 app._middlewares 整体赋值：它是 frozenlist.FrozenList，"
        "换成普通 tuple 后 aiohttp 的 pre_freeze() 会抛 "
        "'tuple' object has no attribute 'freeze'，ComfyUI 直接起不来。"
        "请用 app._middlewares.append(...) 原地追加。")
    assert "app._middlewares.append(" in SRC, "压缩中间件应当原地 append 到 app._middlewares"


def test_compression_wrapper_declares_vary():
    """压缩响应必须带 Vary: Accept-Encoding（官方函数漏了，我们补）。"""
    assert 'setdefault("Vary", "Accept-Encoding")' in SRC, (
        "压缩响应缺少 Vary: Accept-Encoding —— 中间有会缓存的代理时，"
        "可能把 gzip 响应发给不支持 gzip 的客户端。")
    # 包装层必须调用官方那个实现，不许自己写压缩逻辑
    assert "await fn(request, handler)" in SRC
    # 静态 JS/CSS 是 FileResponse（流式），不许被这里动到
    assert "isinstance(resp, web.Response)" in SRC


def test_middleware_calls_the_bound_official_function():
    """不许出现裸名 `compress_body(...)`。

    官方实现只能 `getattr(server, "compress_body")` 拿到；写成模块级函数直接调裸名是
    **NameError** —— 实测后果是**每一个请求都 500**（`web_app.py` 里中间件在请求路径上，
    抛异常就是全局 500）。必须经由闭包绑定的 `fn` 调用。
    """
    assert "await compress_body(" not in SRC, (
        "不要直接调用裸名 compress_body —— routes.py 里没有这个全局名，"
        "会 NameError 让每个请求 500。请用闭包绑定的 fn（见 _compression_middleware）。")
    assert "fn = getattr(_server, \"compress_body\", None)" in SRC


def test_middleware_is_new_style_with_explicit_signature():
    """必须带 `@web.middleware`（新式标记）+ 老实写 `(request, handler)`。

    aiohttp 对带 `__middleware_version__ == 1` 的中间件按 `partial(m, handler=handler)`
    调用（`web_app.py:555`）—— `handler` 走**关键字**传参，所以签名必须能接住它；
    少了 `@web.middleware` 则会被当老式工厂用 `m(app, handler)` 调，语义完全不同。
    """
    assert "@web.middleware" in SRC, "缺少 @web.middleware 标记（会被当老式中间件调用）"
    assert "async def _mw(request, handler):" in SRC, (
        "中间件签名必须是 (request, handler)：handler 是关键字传参，写成 (*args) 会漏掉它")
