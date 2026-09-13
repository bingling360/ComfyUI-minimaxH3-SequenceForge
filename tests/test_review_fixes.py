"""代码审查回归（无 ComfyUI 可跑）：P0-P3 审查中抓到的 bugformer 行为锁定。

跑法（仓库根目录）：
    python -m pytest tests/test_review_fixes.py -q

覆盖：
- B1/B2：加载期 store 优先（旧 `_fn is None` 门控已删除，pool/尾锚双处）
- B3：cleanLabel 上限与后端 24 对齐
- B4：asset_links roles 三态（设/保持/清）+ 路由透传
- B5：syncMirrors 跳过全局条目
- B7/B8：粘贴过滤与优化器媒体查找兼容 id
- B9：multipart 中转名唯一
"""

import asyncio
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3revtest_")


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


def _nodes_src():
    return open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()


def _director_src():
    return open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()


# ---- B1/B2：store 优先门控 ----

def test_store_first_pool_and_tail():
    src = _nodes_src()
    # 旧 bug 门控必须消失（两处：引用集加载 + 尾锚现加载）
    assert "if _fn is None and _AS is not None:" not in src
    # 新顺序：注册表查找无条件先行
    assert "((_reg_ld.get(\"by_id\") or {}).get(_lbl)" in src
    assert "((_reg_t.get(\"by_alias\") or {}).get(lbl)" in src


def test_global_file_pool_resolves(store):
    # B1 场景：pool 文件名是全局路径（images/…），注册表必须能解出绝对路径
    lib = os.path.join(TMP, "librev")
    os.makedirs(os.path.join(lib, "images"), exist_ok=True)
    open(os.path.join(lib, "images", "aa01_hero.png"), "wb").write(b"png")
    rec = {"asset_id": "a_aa01aa01aa01", "alias": "英雄", "kind": "image",
           "origin": "linked", "file": "images/aa01_hero.png"}
    hit = store.resolve_absolute(rec, os.path.join(TMP, "proj_none"), lib)
    assert hit == os.path.join(lib, "images", "aa01_hero.png")


# ---- B3：标签上限 ----

def test_clean_label_24():
    d = _director_src()
    assert '.slice(0, 24)' in d
    assert 'replace(/[[\\]]/g, "").slice(0, 12)' not in d


# ---- B4：roles 三态 ----

def test_link_roles_tristate(projects):
    m = projects.create_project("t_roles")
    m2 = projects.link_asset("t_roles", "a_1234567890ab", "主角", "image",
                             base_revision=m["revision"], roles=["首帧图", "野标注"])
    assert m2["asset_links"] == [{"asset_id": "a_1234567890ab", "alias": "主角",
                                  "kind": "image", "roles": ["首帧图"]}]
    # 不传 roles -> 保持旧标注
    m3 = projects.link_asset("t_roles", "a_1234567890ab", "主角2", "image",
                             base_revision=m2["revision"])
    assert m3["asset_links"][0] == {"asset_id": "a_1234567890ab", "alias": "主角2",
                                    "kind": "image", "roles": ["首帧图"]}
    # 传空列表 -> 清标注
    m4 = projects.link_asset("t_roles", "a_1234567890ab", "主角2", "image",
                             base_revision=m3["revision"], roles=[])
    assert m4["asset_links"][0].get("roles") == []


class _Req:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


def test_asset_links_route_roles(routes, projects):
    h = {}
    r2 = types.SimpleNamespace(
        add_get=lambda p, fn: h.setdefault(("GET", p), fn),
        add_post=lambda p, fn: h.setdefault(("POST", p), fn))
    routes.add_routes(r2)
    projects.create_project("t_roles2")
    link = h[("POST", "/h3chain/asset_link")]
    ok = asyncio.run(link(_Req({"dir": "t_roles2", "asset_id": "a_bbbbbbbbbbbb",
                                "alias": "R", "roles": ["尾帧图"]})))
    assert ok.status == 200
    got = asyncio.run(h[("GET", "/h3chain/asset_links")](_Req(query={"dir": "t_roles2"})))
    assert got.data["links"][0]["roles"] == ["尾帧图"]


# ---- B5：镜像跳过全局条目 ----

def test_mirrors_skip_global():
    d = _director_src()
    assert "const local = (Array.isArray(ds.ref_assets) ? ds.ref_assets : []).filter((a) => a && !a.asset_id);" in d
    assert "const byKind = (k) => local.filter((a) => a.kind === k);" in d


# ---- B7/B8：id 兼容 ----

def test_paste_and_optimizer_id_aware():
    d = _director_src()
    assert "(a.label || a.asset_id)" in d
    assert "a.label === key || a.asset_id === key" in d
    assert "assetPreviewUrl(getDirValue(node), asset.file, asset.asset_id)" in d


# ---- B9：中转名唯一 ----

def test_staged_unique_name():
    src = open(os.path.join(ROOT, "routes.py"), encoding="utf-8").read()
    assert 'tmp_path + "_stage_"' in src
    assert '"h3lib_stage_" + tmp_name' not in src


# ---- B4 前端同步点 ----

def test_roles_sync_points():
    d = _director_src()
    # roles 透传（getDs 读 / setDs 写）——旧三库面板下线后仍在
    assert "roles: Array.isArray(a.roles)" in d
    assert 'r === "首帧图" || r === "尾帧图"' in d
    # 切项目 / 池子 hydration 共用同一个构造函数，roles 归一在其中（_rolesOf）
    assert "function poolFromManifest(mf, links)" in d
    assert "_rolesOf(a.roles)" in d and "_rolesOf(L && L.roles)" in d
