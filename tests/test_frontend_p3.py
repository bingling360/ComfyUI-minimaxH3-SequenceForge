"""P3 前端三库重做 + 链接路由回归（无 ComfyUI 可跑；面板交互需浏览器验）。

跑法（仓库根目录）：
    python -m pytest tests/test_frontend_p3.py -q

覆盖：unlink_asset、asset_links/link/unlink/mirror/library_file 路由、
check_files 全局库前缀、注册表链接富化；前端源码断言（瓦片网格/@补全/
后台任务区/直传/调入/链接合并）与旧字符串零丢失。
"""

import asyncio
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3p3test_")


def _load_top(name, path, patches=None):
    src = open(path, encoding="utf-8").read()
    if patches:
        for a, b in patches:
            src = src.replace(a, b)
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    sys.modules[name] = mod
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


@pytest.fixture(scope="session", autouse=True)
def _env():
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = lambda: TMP
    fp.get_input_directory = lambda: TMP
    fp.get_annotated_filepath = lambda f: os.path.join(TMP, f)
    fp.get_user_directory = lambda: TMP
    sys.modules["folder_paths"] = fp
    aio = types.ModuleType("aiohttp")

    class _Resp:
        def __init__(self, data, status=200):
            self.data = data
            self.status = status

        async def json(self):
            return self.data

    class _Web:
        @staticmethod
        def json_response(data, status=200):
            return _Resp(data, status)

    aio.web = _Web()
    sys.modules["aiohttp"] = aio
    os.makedirs(os.path.join(TMP, "h3_projects"), exist_ok=True)
    yield


@pytest.fixture(scope="session")
def store():
    return _load_top("asset_store", os.path.join(ROOT, "asset_store.py"))


@pytest.fixture(scope="session")
def asset_hub():
    return _load_top("asset_hub", os.path.join(ROOT, "asset_hub.py"))


@pytest.fixture(scope="session")
def checkpoint():
    return _load_top("checkpoint", os.path.join(ROOT, "checkpoint.py"))


@pytest.fixture(scope="session")
def projects(checkpoint):
    return _load_top("projects", os.path.join(ROOT, "projects.py"),
                     [("from . import checkpoint", "import checkpoint")])


@pytest.fixture(scope="session")
def routes(projects, store, checkpoint):
    _load_top("transcode_queue", os.path.join(ROOT, "transcode_queue.py"))
    _load_top("asset_hub", os.path.join(ROOT, "asset_hub.py"))
    return _load_top("routes", os.path.join(ROOT, "routes.py"),
                     [("from . import projects", "import projects"),
                      ("from . import asset_hub", "import asset_hub"),
                      ("from . import asset_store", "import asset_store"),
                      ("from . import transcode_queue as _tq", "import transcode_queue as _tq"),
                      ("from . import transcode_queue", "import transcode_queue"),
                      ("from . import checkpoint as _ckpt", "import checkpoint as _ckpt"),
                      ("from . import prompts as _prompts", "import prompts as _prompts"),
                      ("from . import library as h3lib", "import library as h3lib")])


class _Router:
    def __init__(self):
        self.paths = []

    def add_get(self, path, handler):
        self.paths.append(("GET", path))

    def add_post(self, path, handler):
        self.paths.append(("POST", path))


class _Req:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


def _handlers(routes):
    h = {}
    r2 = types.SimpleNamespace(
        add_get=lambda p, fn: h.setdefault(("GET", p), fn),
        add_post=lambda p, fn: h.setdefault(("POST", p), fn))
    routes.add_routes(r2)
    return h


# ---- unlink_asset ----

def test_unlink_roundtrip(projects):
    m = projects.create_project("t_unlink")
    m2 = projects.link_asset("t_unlink", "a_1234567890ab", "主角", base_revision=m["revision"])
    assert len(m2["asset_links"]) == 1
    m3 = projects.unlink_asset("t_unlink", asset_id="a_1234567890ab", base_revision=m2["revision"])
    assert m3["asset_links"] == [] and m3["revision"] == m2["revision"] + 1
    # 幂等：再解一次同样成功且 revision 不变
    m4 = projects.unlink_asset("t_unlink", alias="主角", base_revision=m3["revision"])
    assert m4["asset_links"] == [] and m4["revision"] == m3["revision"]
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        projects.unlink_asset("t_unlink", alias="x", base_revision=1)
    assert projects.unlink_asset("t_unlink") is None
    assert projects.unlink_asset("no_proj", alias="x") is None


# ---- 注册表富化 ----

def test_registry_link_enrich(store):
    reg = store.build_registry(
        [{"asset_id": "a_ffffffffffff", "kind": "image", "file": "images/ab_x.png"}],
        [{"asset_id": "a_ffffffffffff", "alias": "链", "kind": "image"}], None)
    rec = reg["by_alias"]["链"]
    assert rec["file"] == "images/ab_x.png" and rec["asset_id"] == "a_ffffffffffff"
    assert reg["by_id"]["a_ffffffffffff"]["alias"] == "链"
    # 纯链接（全局缺席）：记录保留，无 file，不断言可寻址
    reg2 = store.build_registry(
        None, [{"asset_id": "a_eeeeeeeeeeee", "alias": "孤链", "kind": "video"}], None)
    assert reg2["by_alias"]["孤链"]["origin"] == "project"
    assert "file" not in reg2["by_alias"]["孤链"]


# ---- check_files 全局库前缀 ----

def test_check_files_global(store, asset_hub):
    src = os.path.join(TMP, "g_hero.png")
    open(src, "wb").write(b"png")
    entry = store.register_content(store.library_root(), src, "image")
    ok = asset_hub.validate_pack([{"label": "英雄", "kind": "image", "file": entry["file"]}])
    assert ok["ok"] is True
    bad = asset_hub.validate_pack([{"label": " ghost", "kind": "image", "file": "images/deadbeef_g.png"}])
    assert bad["ok"] is False and "全局库" in bad["errors"][0]["message"]


# ---- 新路由 ----

def test_new_routes_mount(routes):
    r = _Router()
    routes.add_routes(r)
    for method, p in [("GET", "/h3chain/library_file"),
                      ("GET", "/h3chain/asset_links"),
                      ("POST", "/h3chain/asset_link"),
                      ("POST", "/h3chain/asset_unlink"),
                      ("POST", "/h3chain/asset_mirror")]:
        assert (method, p) in r.paths, (method, p)
        assert (method, "/api" + p) in r.paths, (method, "/api" + p)


def test_asset_link_flow(routes, projects):
    h = _handlers(routes)
    projects.create_project("t_linkflow")
    link = h[("POST", "/h3chain/asset_link")]
    bad = asyncio.run(link(_Req({"dir": "t_linkflow", "asset_id": "nope", "alias": "x"})))
    assert bad.status == 404  # 非法 id -> link_asset 返回 None
    ok = asyncio.run(link(_Req({"dir": "t_linkflow", "asset_id": "a_aaaaaaaaaaaa",
                                "alias": "主角", "kind": "image"})))
    assert ok.status == 200 and len(ok.data["manifest"]["asset_links"]) == 1
    rev = ok.data["manifest"]["revision"]
    conflict = asyncio.run(link(_Req({"dir": "t_linkflow", "asset_id": "a_aaaaaaaaaaaa",
                                      "alias": "主角2", "base_revision": 1})))
    assert conflict.status == 409
    links = h[("GET", "/h3chain/asset_links")]
    got = asyncio.run(links(_Req(query={"dir": "t_linkflow"})))
    assert got.data["ok"] and got.data["links"][0]["alias"] == "主角"
    assert asyncio.run(links(_Req(query={"dir": ".."}))).status == 400
    assert asyncio.run(links(_Req(query={"dir": "t_nonexist"}))).status == 404
    unlink = h[("POST", "/h3chain/asset_unlink")]
    done = asyncio.run(unlink(_Req({"dir": "t_linkflow", "alias": "主角", "base_revision": rev})))
    assert done.status == 200 and done.data["manifest"]["asset_links"] == []
    assert asyncio.run(unlink(_Req({"dir": ".."}))).status == 400


def test_asset_mirror_flow(routes, projects, store):
    h = _handlers(routes)
    mirror = h[("POST", "/h3chain/asset_mirror")]
    assert asyncio.run(mirror(_Req({"dir": "x", "asset_id": "bad"}))).status == 400
    src = os.path.join(TMP, "mir_src.png")
    open(src, "wb").write(b"png-bytes")
    entry = store.register_content(store.library_root(), src, "image")
    projects.create_project("t_mirror")
    ok = asyncio.run(mirror(_Req({"dir": "t_mirror", "asset_id": entry["asset_id"]})))
    assert ok.status == 200 and ok.data["file"].startswith("assets/")
    assert os.path.isfile(os.path.join(
        TMP, "h3_projects", "t_mirror", *ok.data["file"].split("/")))
    # 同名再调 -> _2 后缀
    ok2 = asyncio.run(mirror(_Req({"dir": "t_mirror", "asset_id": entry["asset_id"]})))
    assert ok2.data["file"] != ok.data["file"]
    assert asyncio.run(mirror(_Req({"dir": "t_mirror", "asset_id": "a_000000000000"}))).status == 404
    assert asyncio.run(mirror(_Req({"dir": "t_nonexist", "asset_id": entry["asset_id"]}))).status == 404


def test_library_file_handler(routes, store):
    h = _handlers(routes)
    fn = h[("GET", "/h3chain/library_file")]
    assert asyncio.run(fn(_Req(query={"asset_id": "bad"}))).status == 400
    assert asyncio.run(fn(_Req(query={"asset_id": "a_000000000000"}))).status == 404
    src = os.path.join(TMP, "lf_src.png")
    open(src, "wb").write(b"png")
    entry = store.register_content(store.library_root(), src, "image")
    # stub 无 FileResponse -> 走 json 回退分支（不断言文件流，浏览器端由 aiohttp 真实现服务）
    r = asyncio.run(fn(_Req(query={"asset_id": entry["asset_id"]})))
    assert r.status == 200 and r.data["asset_id"] == entry["asset_id"]


class _MPField:
    """最小 multipart field 桩：read_chunk 流式 + read 全量。"""

    def __init__(self, name, filename=None, data=b""):
        self.name = name
        self.filename = filename
        self._data = data
        self._pos = 0

    async def read_chunk(self, size=8192):
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:self._pos + size]
        self._pos += size
        return chunk

    async def read(self, decode=False):
        return self._data


class _MPReader:
    def __init__(self, fields):
        self._fields = list(fields)

    async def next(self):
        if not self._fields:
            return None
        return self._fields.pop(0)


class _ReqMP:
    """multipart 请求桩：带 Content-Type；json() 一旦被调就记数（回归：multipart
    路径不许碰 json()，否则真机 aiohttp body 被消费导致找不到 boundary）。"""

    def __init__(self, fields):
        self.headers = {"Content-Type": "multipart/form-data; boundary=----FakeBoundary"}
        self._reader = _MPReader(fields)
        self.json_calls = 0

    async def json(self):
        self.json_calls += 1
        raise ValueError("multipart has no json")

    async def multipart(self):
        return self._reader


def test_library_upload_multipart(routes, projects):
    h = _handlers(routes)
    fn = h[("POST", "/h3chain/library_upload")]
    projects.create_project("t_mpup")
    req = _ReqMP([
        _MPField("kind", data=b"image"),
        _MPField("link_dir", data="t_mpup".encode()),
        _MPField("alias", data="拖放图".encode()),
        _MPField("file", filename="drop.png", data=b"\x89PNG-fake-bytes"),
    ])
    r = asyncio.run(fn(req))
    assert req.json_calls == 0
    assert r.status == 200, r.data
    assert r.data["entry"]["asset_id"].startswith("a_")
    assert r.data["alias"] == "拖放图"
    assert any(x["alias"] == "拖放图" for x in r.data["manifest"]["asset_links"])
    # 缺 file 字段 -> 400
    r2 = asyncio.run(fn(_ReqMP([_MPField("kind", data=b"image")])))
    assert r2.status == 400


# ---- 前端源码断言 ----

def _read(web_file):
    return open(os.path.join(ROOT, "web", web_file), encoding="utf-8").read()


def test_library_browser_ui():
    """新素材库：常用按钮摆在瓦片上，低频动作在右键菜单，预览器只做"看"。"""
    lib = _read("h3_library.js")
    for sym in ["window.H3Lib", "h3l-scope", "h3l-grid", "h3l-tile", "h3l-menu",
                "h3l-viewer", "h3l-thumb", "h3l-tbtns", "h3l-tbtn",
                "function tileButtons(", "function openMenu(", "function openViewer(",
                "libList", "libThumbUrl", "libRawUrl", "libMirror", "libZip",
                "libRole", "libAlias"]:
        assert sym in lib, sym
    # 多余动作已下线：不再有"引用到段""复制到 input"
    for gone in ["actRef", "actStage", "作为参考素材", "复制到 input"]:
        assert gone not in lib, gone
    d = _read("h3_director.js")
    assert "window.H3Lib.open" in d
    # 后端清单必须回填 widget，否则段引用与 @ 补全看不到新素材
    assert "await hydratePool()" in d
    for gone in ["h3d-tilegrid", "h3d-kindcols", "h3d-dropzone", "renderLibAssets"]:
        assert gone not in d, gone


def test_director_atcomplete():
    d = _read("h3_director.js")
    for sym in ["attachAtComplete", "h3d-atpop", "ta.dataset.h3at",
                "dispatchEvent(new Event(\"input\"", "attachAtComplete(ta, node)"]:
        assert sym in d, sym


def test_director_no_inline_transcode():
    """资产库只做引用/打标：内联的裁剪/分离/转latent 与后台任务区必须已下线。"""
    d = _read("h3_director.js")
    for gone in ["renderServerJobs", "renderTranscodeJobs", "pruneDoneJobs",
                 "openTranscodeSub", "autoQueueTranscode", "transcodeVaeFiles",
                 "transcode_jobs", "h3d-jstat", "提交后台任务"]:
        assert gone not in d, gone


def test_director_link_sync():
    d = _read("h3_director.js")
    assert "asset_id: typeof a.asset_id" in d  # getDs 透传
    assert "validIds" in d  # refs 白名单含 id
    assert "filter((a) => a && !a.asset_id)" in d  # 保存剥离链接
    assert "assetUnlink({ dir, asset_id: gone.asset_id })" in d
    assert "assetLink({ dir, asset_id: asset.asset_id, alias: asset.label" in d
    assert "assetLinks(dir)" in d or "assetLinks" in d


def test_director_legacy_intact():
    """三库下线后仍要留住的通用件（折叠框 / 预览地址 / 入口 / 画布镜像）。"""
    d = _read("h3_director.js")
    for sym in ["function foldBox(", "function assetPreviewUrl(",
                "function renderV2Section(", "window.H3Lib.open",
                "window.H3Director", "syncMirrors"]:
        assert sym in d, sym
    for gone in ["function openTrimSub", "function renderLibFinals",
                 "function renderLibLatent", "function renderLibAssets"]:
        assert gone not in d, gone


def test_helpers_api():
    a = _read("h3_assets.js")
    for sym in ["assetId", "poolAliases", "segUsage", "compileTags", "uploadDirect",
                "guessKind", "cleanAsset", "dedupe", "checkSegRefs"]:
        assert sym in a, sym
    assert "registerExtension" not in a
    api = _read("h3_api.js")
    for sym in ["compileRefs", "assetLinks", "assetLink", "assetUnlink",
                "assetMirror", "libraryFileUrl", "libraryUpload", "vaeFiles"]:
        assert sym in api, sym


def test_library_entry_and_old_gone():
    """导演台只留入口；旧三库面板（全屏 libbox / 三栏 kindcols / 瓦片网格）全下线。"""
    d = _read("h3_director.js")
    for sym in ["function renderV2Section(", "window.H3Lib.open",
                "function foldBox(", "h3d-libfold", "proj-list", "项目存档（"]:
        assert sym in d, sym
    for gone in ["h3d-libbox", "h3d-libhead", "h3d-libtab", "h3d-libbody",
                 "h3d-kindcols", "h3d-kindcol", "h3d-tilegrid", "h3d-dropzone",
                 "h3d-prog", "h3d-jstat", "width:min(1060px,96vw)"]:
        assert gone not in d, gone


def test_explicit_ref_binding():
    d = _read("h3_director.js")
    # 勾选即在正文补可见 @标签（取消不删）
    assert "ds.prompts[idx] = cur" in d
    assert "勾选自动在正文补 @标签" in d or "正文自动补 @标签" in d


def test_switch_race_guards():
    d = _read("h3_director.js")
    # 代际守卫：连续快切时过期流程退出
    assert "let _uiGen = 0" in d
    assert "const myGen = ++_uiGen" in d
    assert "if (stale()) return" in d
    # 待落盘按键同步刷进旧项目（防跨项目污染/丢失）
    assert "const _taPending = new Map()" in d
    assert "function flushPendingEdits(" in d
    assert "flushPendingEdits();" in d
    # 回写失败不中断切换
    assert "切项目前回写旧项目失败" in d
    # 空项目清空旧池（防旧内容残留）；槽位索引态清零（防重做错段）
    assert "必须清空旧池" in d
    assert "ds.redo_segs = [];" in d
    assert "ds.upscale.include = [];" in d
    # 直接全刷（不等轮询）
    assert "await refresh();" in d


def test_library_backend_index():
    """索引层：四 scope + 分页契约 + 语义合入（角色 / 段引用 / 元数据 sidecar）。"""
    src = open(os.path.join(ROOT, "library.py"), encoding="utf-8").read()
    for sym in ['SCOPES = ("project", "global", "finals", "latent")',
                "def build_index(", "def scan_scope(", "def query(",
                "def apply_aliases(", "def compute_refs(", "def apply_roles(",
                "def apply_meta(", "def stage_to_input(", "def make_zip(",
                "def make_thumb(", "def load_meta(", "def save_meta(",
                "total_pages", "page_size"]:
        assert sym in src, sym
