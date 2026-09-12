"""P0 资产存储回归（无 ComfyUI 可跑）。

跑法（仓库根目录）：
    python -m pytest tests/test_asset_store_p0.py -q

覆盖：旧 assets 迁移稳定性/去重、alias 与 asset_id 双解析、按类型独立编号、
单段 9/3/3、未知引用点名、多段干跑、全局库入库秒传、routes /h3chain/compile_refs
挂载与 handler 冒烟（aiohttp stub）。旧 manifest["assets"] 零改动可读。
"""

import asyncio
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3p0test_")


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
def projects(store):
    import checkpoint as _ckpt  # noqa: F401  (占位，projects 经 patch 后自带 import)
    return _load_top("projects", os.path.join(ROOT, "projects.py"),
                     [("from . import checkpoint", "import checkpoint")])


@pytest.fixture(scope="session")
def routes(projects):
    return _load_top("routes", os.path.join(ROOT, "routes.py"),
                     [("from . import projects", "import projects"),
                      ("from . import asset_hub", "import asset_hub"),
                      ("from . import asset_store", "import asset_store"),
                      ("from . import prompts as _prompts", "import prompts as _prompts"),
                      ("from . import library as h3lib", "import library as h3lib")])


@pytest.fixture(scope="session")
def checkpoint():
    return _load_top("checkpoint", os.path.join(ROOT, "checkpoint.py"))


# ---- 迁移 ----

def test_legacy_migrate_stable_and_dedupe(store):
    legacy = [
        {"label": "角色1", "kind": "image", "file": "assets/a.png"},
        {"label": "角色1", "kind": "image", "file": "assets/b.png"},
        {"label": "配乐", "kind": "audio", "file": "assets/m.mp3"},
    ]
    links = store.migrate_legacy_assets(legacy)
    assert len(links) == 2 and links[0]["legacy_file"] == "assets/a.png"
    again = store.migrate_legacy_assets(legacy)
    assert [x["asset_id"] for x in again] == [x["asset_id"] for x in links]


def test_registry_alias_and_id(store):
    legacy = [{"label": "角色1", "kind": "image", "file": "assets/a.png"}]
    reg = store.build_registry(None, None, legacy)
    aid = store.asset_id_for_legacy("角色1", "image", "assets/a.png")
    assert aid in reg["by_id"] and "角色1" in reg["by_alias"]
    r1 = store.compile_refs(reg, ["角色1"])
    r2 = store.compile_refs(reg, [aid])
    assert r1["ok"] and r2["ok"]
    assert r1["blocks"][0]["token"] == "<Picture 1>"


def test_kind_numbering_independent(store):
    reg = store.build_registry(None, [
        {"asset_id": "a_111111111111", "alias": "图", "kind": "image"},
        {"asset_id": "a_222222222222", "alias": "片", "kind": "video"},
        {"asset_id": "a_333333333333", "alias": "乐", "kind": "audio"},
    ], None)
    r = store.compile_refs(reg, ["图", "片", "乐"])
    assert r["ok"] and [b["token"] for b in r["blocks"]] == [
        "<Picture 1>", "<Video 1>", "<Audio 1>"]
    assert r["tag_map"]["图"] == "<Picture 1>"


def test_seg_caps(store):
    links = [{"asset_id": f"a_1000000000{i:02d}", "alias": f"图{i}", "kind": "image"}
             for i in range(10)]
    reg = store.build_registry(None, links, None)
    bad = store.compile_refs(reg, [f"图{i}" for i in range(10)])
    assert bad["ok"] is False and bad["errors"][0]["code"] == "E_MEDIA_LIMIT"
    ok9 = store.compile_refs(reg, [f"图{i}" for i in range(9)])
    assert ok9["ok"] is True
    vlinks = [{"asset_id": f"a_2000000000{i:02d}", "alias": f"视{i}", "kind": "video"}
              for i in range(4)]
    rv = store.compile_refs(store.build_registry(None, vlinks, None),
                            [f"视{i}" for i in range(4)])
    assert rv["ok"] is False and rv["errors"][0]["code"] == "E_MEDIA_LIMIT"


def test_unknown_ref(store):
    reg = store.build_registry(None, None, [{"label": "A", "kind": "image", "file": "a.png"}])
    r = store.compile_refs(reg, ["不存在"])
    assert r["ok"] is False and r["errors"][0]["code"] == "E_REF_UNKNOWN"


def test_dict_ref_use_passthrough(store):
    reg = store.build_registry(
        None, [{"asset_id": "a_aaaaaaaaaaaaaaaa", "alias": "首帧", "kind": "image"}], None)
    r = store.compile_refs(reg, [{"asset": "a_aaaaaaaaaaaaaaaa", "use": "首帧图"}])
    assert r["ok"] and r["blocks"][0]["use"] == "首帧图"


def test_compile_segments(store):
    reg = store.build_registry(
        None, [{"asset_id": "a_bbbbbbbbbbbb", "alias": "A", "kind": "image"}], None)
    res = store.compile_segments(reg, [["A"], ["不存在"]])
    assert res["ok"] is False and len(res["segs"]) == 2
    assert res["segs"][0]["ok"] is True and res["segs"][1]["ok"] is False


# ---- 全局库 ----

def test_register_content_roundtrip_and_dedup(store):
    lib = os.path.join(TMP, "lib1")
    src = os.path.join(TMP, "hero.png")
    open(src, "wb").write(b"fake-image-bytes")
    e1 = store.register_content(lib, src, "image", tags=["角色"], desc="主角")
    assert e1["asset_id"].startswith("a_") and e1["file"].startswith("images/")
    assert os.path.isfile(os.path.join(lib, e1["file"]))
    e2 = store.register_content(lib, src, "image")
    assert e2["asset_id"] == e1["asset_id"]  # 秒传
    assert len(store.load_library(lib)["assets"]) == 1


# ---- routes ----

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


def test_compile_refs_route_mount(routes):
    r = _Router()
    routes.add_routes(r)
    assert ("POST", "/h3chain/compile_refs") in r.paths
    assert ("POST", "/api/h3chain/compile_refs") in r.paths


def test_compile_refs_handler(routes):
    h = {}
    r2 = types.SimpleNamespace(
        add_get=lambda p, fn: h.setdefault(("GET", p), fn),
        add_post=lambda p, fn: h.setdefault(("POST", p), fn))
    routes.add_routes(r2)
    fn = h[("POST", "/h3chain/compile_refs")]
    body = {"assets": [{"label": "A", "kind": "image", "file": "a.png"}],
            "refs": ["A"]}
    res = asyncio.run(fn(_Req(body)))
    assert res.status == 200 and res.data["blocks"][0]["token"] == "<Picture 1>"
    bad = asyncio.run(fn(_Req({"assets": [], "refs": ["谁都不是"]})))
    assert bad.status == 422 and bad.data["errors"][0]["code"] == "E_REF_UNKNOWN"
    multi = asyncio.run(fn(_Req({"assets": [{"label": "A", "kind": "image", "file": "a.png"}],
                                 "segments": [["A"], ["B"]]})))
    assert multi.status == 422 and len(multi.data["segs"]) == 2


def test_register_orig_name(store):
    # multipart 中转名（h3lib_xxx_stage_）不得污染库内命名
    lib = os.path.join(TMP, "lib_orig")
    staged = os.path.join(TMP, "h3lib_AB12CD34_stage_hero.mp4")
    open(staged, "wb").write(b"mp4-bytes")
    e = store.register_content(lib, staged, "video", orig_name="hero.mp4")
    assert e["file"].endswith("_hero.mp4") and "h3lib_" not in e["file"]
    assert e["orig_name"] == "hero.mp4"
    # 缺省行为不变：取源文件名
    plain = os.path.join(TMP, "plain_clip.mp4")
    open(plain, "wb").write(b"other-bytes")
    e2 = store.register_content(lib, plain, "video")
    assert e2["file"].endswith("_plain_clip.mp4") and e2["orig_name"] == "plain_clip.mp4"
