"""静态 JS/CSS 缓存策略中间件的行为测试（真 aiohttp，真 FileResponse）。

跑法（仓库根目录）：
    PYTHONPATH="D:/ComfyUI_windows_portable/python_embeded/Lib/site-packages" \
      ~/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe \
      -m pytest tests/test_static_cache_policy.py -q

背景见 `docs/云端卡顿_静态资源缓存与压缩_2026-09-29.md`：ComfyUI 的
`cache_control` 中间件对一切 .js/.css 响应 setdefault `no-store`，浏览器完全
不缓存，公网上每次刷新全量重传前端 93MB JS 池。修法是把缓存头接管过来：
- /assets/（vite 内容哈希文件）→ immutable 长缓存；
- 其余（/extensions/ 插件文件）→ no-cache 协商重验 + ETag 304；
- 顺带对 200 的 .js/.css 开 gzip 并补 Vary。

测试里复刻了 ComfyUI 的 cache_control（no-store 那条分支），把它挂在外层，
验证内层中间件确实把它压住 —— 这是要防的核心回归：**谁要是把中间件注册顺序
改回外层、或者把强设改回 setdefault，这里必红**。
"""

import asyncio
import os
import sys
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- 装载 routes.py
def _load_routes():
    """把 routes.py 当独立模块装载（真 aiohttp；相对导入与 folder_paths 换桩）。

    模块名带 test 专属后缀，避免与其他测试文件放进 sys.modules 的同名桩互相覆盖。
    """
    for name in ("h3lib_stub", "launch_ref_stub", "perf_stub", "proj_stub"):
        sys.modules.setdefault(name, types.ModuleType(name))
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = lambda: "."
    sys.modules["folder_paths"] = fp

    src = open(os.path.join(ROOT, "routes.py"), encoding="utf-8").read()
    for a, b in [
        ("from . import library as h3lib", "import h3lib_stub as h3lib"),
        ("from . import launch_ref", "import launch_ref_stub"),
        ("from . import perf", "import perf_stub as perf"),
        ("from . import projects", "import proj_stub as projects"),
    ]:
        assert a in src, f"routes.py 结构变了，测试装载桩跟不上：{a}"
        src = src.replace(a, b)
    mod = types.ModuleType("routes_static_cache_test")
    sys.modules[mod.__name__] = mod
    exec(compile(src, os.path.join(ROOT, "routes.py"), "exec"), mod.__dict__)
    return mod


h3routes = _load_routes()


def _replica_cache_control():
    """ComfyUI `middleware/cache_middleware.py` 的 .js/.css 分支复刻（no-store）。

    只复刻与本测试相关的行为：外层、后执行、setdefault 语义。
    """

    @web.middleware
    async def mw(request, handler):
        resp = await handler(request)
        if request.path.endswith(".js") or request.path.endswith(".css"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    return mw


def _write(tmpdir, name, body):
    p = os.path.join(tmpdir, name)
    with open(p, "wb") as f:
        f.write(body)
    return p


async def _flow(tmpdir):
    app = web.Application(middlewares=[_replica_cache_control()])

    ext_js = _write(tmpdir, "h3_director.js", b"window.H3 = 1;" + b"x" * 2048)
    asset_js = _write(tmpdir, "index-abc123.js", b"console.log(1);" + b"y" * 2048)
    thumb = _write(tmpdir, "thumb01.jpg", b"\xff\xd8fakejpeg")

    async def f_ext(request):
        return web.FileResponse(ext_js)

    async def f_asset(request):
        return web.FileResponse(asset_js)

    async def f_thumb(request):
        # 模拟 lib_thumb：处理器自带 Cache-Control，中间件不许动非 .js/.css
        return web.FileResponse(thumb, headers={
            "Cache-Control": "public, max-age=86400"})

    async def f_gone(request):
        return web.Response(status=404, text="gone")

    app.router.add_get(
        "/extensions/ComfyUI-minimaxH3-SequenceForge/h3_director.js", f_ext)
    app.router.add_get("/assets/index-abc123.js", f_asset)
    app.router.add_get("/h3chain/lib_thumb", f_thumb)
    app.router.add_get("/gone.js", f_gone)

    assert h3routes.enable_static_cache_policy(app) is True
    n_mw = len(app._middlewares)
    # 幂等：重复启用不重复挂
    assert h3routes.enable_static_cache_policy(app) is True
    assert len(app._middlewares) == n_mw

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # ── 1. /extensions/*.js：外层的 no-store 必须被压成 no-cache，且带 ETag ──
        r1 = await client.get(
            "/extensions/ComfyUI-minimaxH3-SequenceForge/h3_director.js")
        assert r1.status == 200
        cc = r1.headers["Cache-Control"]
        assert cc == "no-cache", f"扩展 JS 缓存策略应是 no-cache，实际 {cc!r}"
        etag = r1.headers.get("ETag")
        assert etag and etag.startswith('"') and etag.endswith('"'), etag

        # ── 2. 协商重验：If-None-Match 命中 → 304 空响应，策略头仍在 ──
        r2 = await client.get(
            "/extensions/ComfyUI-minimaxH3-SequenceForge/h3_director.js",
            headers={"If-None-Match": etag})
        assert r2.status == 304, f"ETag 命中应 304，实际 {r2.status}"
        assert r2.headers["Cache-Control"] == "no-cache"
        assert (await r2.read()) == b""

        # ── 3. ETag 对不上（文件被换过）→ 必须回 200 新内容 ──
        r3 = await client.get(
            "/extensions/ComfyUI-minimaxH3-SequenceForge/h3_director.js",
            headers={"If-None-Match": '"deadbeef-1"'})
        assert r3.status == 200

        # ── 4. /assets/*.js：vite 内容哈希 → immutable 长缓存 ──
        r4 = await client.get("/assets/index-abc123.js")
        assert r4.status == 200
        cc4 = r4.headers["Cache-Control"]
        assert cc4 == "public, max-age=31536000, immutable", cc4

        # ── 5. gzip：Accept-Encoding 带 gzip 时 200 响应走压缩 ──
        r5 = await client.get(
            "/extensions/ComfyUI-minimaxH3-SequenceForge/h3_director.js",
            headers={"Accept-Encoding": "gzip"})
        assert r5.status == 200
        assert r5.headers.get("Content-Encoding") == "gzip", (
            "FileResponse 的 enable_compression 应生效（aiohttp 会退到分块流式压缩）")
        assert r5.headers.get("Vary") == "Accept-Encoding"

        # ── 6. 非 .js/.css 的 FileResponse：中间件不碰，处理器自己的头原样在 ──
        r6 = await client.get("/h3chain/lib_thumb")
        assert r6.status == 200
        assert r6.headers["Cache-Control"] == "public, max-age=86400"
        assert "ETag" in r6.headers  # aiohttp 原生自带，过期后能协商 304

        # ── 7. 处理器自己 404 的 .js：stat 失败原样放行，不许变 5xx ──
        r7 = await client.get("/gone.js")
        assert r7.status == 404
    finally:
        await client.close()


def test_full_flow(tmp_path):
    asyncio.run(_flow(str(tmp_path)))


def test_304_invalidated_by_content_change(tmp_path):
    """mtime/size 没变 → 304；文件内容变了 → 新 ETag → 200 新内容。"""
    target = tmp_path / "h3_director.js"
    target.write_bytes(b"a" * 2048)

    async def flow():
        app = web.Application()

        async def f(request):
            return web.FileResponse(str(target))

        app.router.add_get("/extensions/x/h3_director.js", f)
        h3routes.enable_static_cache_policy(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r1 = await client.get("/extensions/x/h3_director.js")
            e1 = r1.headers["ETag"]
            assert (await client.get(
                "/extensions/x/h3_director.js",
                headers={"If-None-Match": e1})).status == 304
            # 内容变化 → mtime_ns/size 至少一个变 → 新 ETag → 200
            target.write_bytes(b"b" * 2049)
            st = target.stat()
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            r3 = await client.get("/extensions/x/h3_director.js")
            assert r3.status == 200
            assert r3.headers["ETag"] != e1
        finally:
            await client.close()

    asyncio.run(flow())


def test_middleware_tripwires():
    """绊线（同 test_response_compression 的口径）：写法不能回退。"""
    src = open(os.path.join(ROOT, "routes.py"), encoding="utf-8").read()
    # 缓存头必须强设占位（改成 setdefault 就压不住外层的 no-store）
    assert 'resp.headers["Cache-Control"] = policy' in src
    # 同两条老纪律：原地 append、新式签名
    assert "app._middlewares.append(mw)" in src
    assert "async def _mw(request, handler):" in src
    # ETag 格式与 aiohttp 原生 FileResponse 一致（mtime_ns:x-size:x）
    assert "{st.st_mtime_ns:x}-{st.st_size:x}" in src
    # gzip 响应必须声明 Vary（中间有会缓存的代理时防发错版本）
    assert '"Vary", "Accept-Encoding"' in src
