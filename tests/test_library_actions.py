"""素材库动作回归（改名 / 上传落点 / 评分）——无 ComfyUI 可跑。

跑法（仓库根目录）：
    python -m pytest tests/test_library_actions.py -q

覆盖：
1) lib_alias 改名：**项目链接 alias 与项目资产 label 两处都改**（只改一处
   显示名不动 = 「点了改名没反应」）；重名直接拦。
2) library_upload 落点 dest=global/project/finals：multipart 分支也要认
   mirror/dest（旧实现只读 JSON 分支，浏览器上传永远只进全局库）。
"""
import asyncio
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3libact_")


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


class _Resp:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status

    async def json(self):
        return self.data


class _Req:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


@pytest.fixture(scope="module", autouse=True)
def _env():
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = lambda: TMP
    fp.get_input_directory = lambda: TMP
    fp.get_annotated_filepath = lambda f: os.path.join(TMP, f)
    fp.get_user_directory = lambda: TMP
    sys.modules["folder_paths"] = fp
    aio = types.ModuleType("aiohttp")

    class _Web:
        @staticmethod
        def json_response(data, status=200):
            return _Resp(data, status)

    aio.web = _Web()
    sys.modules["aiohttp"] = aio
    yield


@pytest.fixture(scope="module")
def h3lib():
    return _load_top("library", os.path.join(ROOT, "library.py"))


@pytest.fixture(scope="module")
def projects(h3lib):
    return _load_top("projects", os.path.join(ROOT, "projects.py"),
                     [("from . import checkpoint", "import checkpoint")])


@pytest.fixture(scope="module")
def routes(projects, h3lib):
    return _load_top("routes", os.path.join(ROOT, "routes.py"),
                     [("from . import projects", "import projects"),
                      ("from . import asset_hub", "import asset_hub"),
                      ("from . import asset_store", "import asset_store"),
("from . import library as h3lib", "import library as h3lib"),
                      ("from . import perf", "import perf")])


@pytest.fixture()
def handlers(routes):
    h = {}

    def add_get(p, fn):
        h.setdefault(("GET", p), fn)

    def add_post(p, fn):
        h.setdefault(("POST", p), fn)

    routes.add_routes(types.SimpleNamespace(add_get=add_get, add_post=add_post))
    return h


@pytest.fixture()
def proj(tmp_path, h3lib, routes, monkeypatch):
    """临时项目：assets/girl.png 一个素材 + 同名项目链接（上传即镜像的真实形态）。"""
    root = tmp_path / "h3_projects" / "demo"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "girl.png").write_bytes(b"png-bytes")
    monkeypatch.setattr(h3lib, "_project_root",
                        lambda name: str(tmp_path / "h3_projects" / str(name)))
    monkeypatch.setattr(h3lib, "_library_root", lambda: str(tmp_path / "lib"))
    h3lib.invalidate()
    state = {
        "mf": {
            "title": "demo", "revision": 3,
            "assets": [{"label": "女主", "kind": "image", "file": "assets/girl.png"}],
            "asset_links": [{"asset_id": "a_0123456789ab", "alias": "女主",
                             "kind": "image"}],
        },
    }
    projects_mod = sys.modules["projects"]
    monkeypatch.setattr(projects_mod, "read_project", lambda n: state["mf"], raising=False)
    monkeypatch.setattr(projects_mod, "safe_name", lambda n: str(n or ""), raising=False)

    def _link(n, aid, alias, kind="image", base_revision=None, roles=None,
              ref_name=None, orig_name=None):
        state["mf"]["asset_links"] = [{"asset_id": aid, "alias": alias, "kind": kind}]
        state["mf"]["revision"] = int(state["mf"]["revision"]) + 1
        return state["mf"]

    def _save(n, assets, base_revision=None):
        state["mf"]["assets"] = assets
        state["mf"]["revision"] = int(state["mf"]["revision"]) + 1
        return state["mf"]

    monkeypatch.setattr(projects_mod, "link_asset", _link, raising=False)
    monkeypatch.setattr(projects_mod, "save_assets", _save, raising=False)
    return {"dir": "demo", "root": root, "state": state}


# ---- 改名 ----

def test_lib_alias_renames_link_and_asset(handlers, proj):
    """两处同名都要改：只改链接的话显示名（取 assets.label）纹丝不动。"""
    fn = handlers[("POST", "/h3chain/lib_alias")]
    res = asyncio.run(fn(_Req({"dir": "demo", "id": "project:assets/girl.png",
                               "alias": "女主角"})))
    assert res.status == 200, res.data
    assert res.data["renamed"] == {"link": True, "asset": True}
    mf = proj["state"]["mf"]
    assert mf["asset_links"][0]["alias"] == "女主角"
    assert mf["assets"][0]["label"] == "女主角"


def test_lib_alias_rejects_duplicate(handlers, proj):
    proj["state"]["mf"]["assets"].append(
        {"label": "路人", "kind": "image", "file": "assets/other.png"})
    (proj["root"] / "assets" / "other.png").write_bytes(b"png")
    fn = handlers[("POST", "/h3chain/lib_alias")]
    res = asyncio.run(fn(_Req({"dir": "demo", "id": "project:assets/girl.png",
                               "alias": "路人"})))
    assert res.status == 400 and res.data["code"] == "DUP_ALIAS"


# ---- 上传落点 ----

class _Field:
    def __init__(self, name, value=None, filename=None, chunks=None):
        self.name = name
        self.filename = filename
        self._chunks = chunks or ([value] if value is not None else [])

    async def read(self, decode=False):
        return b"".join(self._chunks)

    async def read_chunk(self):
        return b""


class _Reader:
    def __init__(self, fields):
        self.fields = list(fields)

    async def next(self):
        return self.fields.pop(0) if self.fields else None


class _MultipartReq:
    """最小 multipart 请求替身：headers + multipart() reader。"""

    def __init__(self, fields):
        self.headers = {"Content-Type": "multipart/form-data; boundary=x"}
        self._reader = _Reader(fields)

    async def multipart(self):
        return self._reader


def test_upload_dest_project_stays_in_project(handlers, proj, tmp_path, monkeypatch):
    """dest=project：**只落项目**（不进全局库）—— 上传到哪里就是哪里。"""
    import asset_store as AS
    lib = tmp_path / "lib"
    monkeypatch.setattr(AS, "library_root", lambda *a, **k: str(lib))
    sys.modules["asset_store"] = AS
    fn = handlers[("POST", "/h3chain/library_upload")]
    req = _MultipartReq([
        _Field("kind", b"image"),
        _Field("dest", b"project"),
        _Field("link_dir", b"demo"),
        _Field("alias", "新素材".encode("utf-8")),
        _Field("file", b"png-bytes-abc", filename="girl.png"),
    ])
    res = asyncio.run(fn(req))
    assert res.status == 200, res.data
    assert res.data["dest"] == "project"
    assert res.data["stored"]["label"] == "新素材"
    assert res.data["stored"]["file"].startswith("assets/")
    assert (proj["root"] / str(res.data["stored"]["file"])).is_file()
    # 全局库不该多出任何东西（用户明确反馈"顺手复制一份"不对）
    assert not os.path.isdir(lib) or not os.path.isfile(os.path.join(lib, "manifest.json"))
    assert "entry" not in res.data
    # 项目清单登记了（可用 @别名 引用）
    assert any(a.get("label") == "新素材" for a in proj["state"]["mf"]["assets"])


def test_upload_dest_global_only(handlers, proj, tmp_path, monkeypatch):
    """dest=global：只进全局库，不碰项目（assets/ 数量不变，也不登记清单）。"""
    import asset_store as AS
    lib = tmp_path / "lib"
    monkeypatch.setattr(AS, "library_root", lambda *a, **k: str(lib))
    sys.modules["asset_store"] = AS
    before = sorted(os.listdir(proj["root"] / "assets"))
    fn = handlers[("POST", "/h3chain/library_upload")]
    req = _MultipartReq([
        _Field("kind", b"image"),
        _Field("dest", b"global"),
        _Field("alias", b"gonly"),
        _Field("file", b"png-bytes-def", filename="g.png"),
    ])
    res = asyncio.run(fn(req))
    assert res.status == 200, res.data
    assert res.data["dest"] == "global"
    assert res.data["entry"]["asset_id"].startswith("a_")
    assert not res.data.get("stored")
    assert sorted(os.listdir(proj["root"] / "assets")) == before


def test_mirror_link_not_copy(handlers, proj, tmp_path, monkeypatch):
    """调入项目默认=链接（asset_links，不复制文件）；copy 模式才拷一份。"""
    lib = tmp_path / "lib"
    (lib / "images").mkdir(parents=True)
    (lib / "images" / "x.png").write_bytes(b"png")
    import asset_store as AS
    monkeypatch.setattr(AS, "library_root", lambda *a, **k: str(lib))
    sys.modules["asset_store"] = AS
    proj["state"]["mf"]["asset_links"] = []
    aid = "a_0123456789ab"
    links = []

    def _link(n, asset_id, alias, kind="image", base_revision=None, roles=None,
              ref_name=None, orig_name=None):
        links.append({"asset_id": asset_id, "alias": alias, "kind": kind})
        proj["state"]["mf"]["asset_links"] = links
        return proj["state"]["mf"]

    sys.modules["projects"].link_asset = _link
    # 全局库 manifest 里放一个条目，好让 _find_item 找到它
    AS.save_library(str(lib), {"assets": [{
        "asset_id": aid, "sha256": "0" * 64, "kind": "image",
        "file": "images/x.png", "orig_name": "girl.png", "bytes": 3,
        "tags": [], "desc": "", "created_at": 1, "updated_at": 1}]})
    before_files = sorted(os.listdir(proj["root"] / "assets"))
    fn = handlers[("POST", "/h3chain/lib_mirror")]
    res = asyncio.run(fn(_Req({"dir": "demo", "id": "global:images/x.png",
                               "label": "女主"})))
    assert res.status == 200, res.data
    assert res.data["mode"] == "link" and res.data["alias"] == "女主"
    assert links and links[0]["asset_id"] == aid
    # 项目 assets/ 没有多出文件（链接不复制）
    assert sorted(os.listdir(proj["root"] / "assets")) == before_files
    # 全局库条目数不变
    assert len(AS.load_library(str(lib))["assets"]) == 1


def _seed_global_lib(tmp_path, monkeypatch, orig_name="girl.png", aid="a_aaaaaaaaaaaa"):
    """在临时全局库里放一个条目（项目里有同名 assets/girl.png）。"""
    import asset_store as AS
    lib = tmp_path / "lib"
    (lib / "images").mkdir(parents=True, exist_ok=True)
    (lib / "images" / "g.png").write_bytes(b"png")
    monkeypatch.setattr(AS, "library_root", lambda *a, **k: str(lib))
    sys.modules["asset_store"] = AS
    AS.save_library(str(lib), {"assets": [{
        "asset_id": aid, "sha256": "1" * 64, "kind": "image",
        "file": "images/g.png", "orig_name": orig_name, "bytes": 3,
        "tags": [], "desc": "", "created_at": 1, "updated_at": 1}]})
    sys.modules["library"].invalidate()
    return lib


def test_lib_list_marks_blocked_targets(handlers, proj, tmp_path, monkeypatch):
    """目标库已有同名 -> lib_list 给该条目标 blocked（前端据此不画按钮）。"""
    _seed_global_lib(tmp_path, monkeypatch)
    fn = handlers[("GET", "/h3chain/lib_list")]
    res = asyncio.run(fn(_Req(query={"dir": "demo", "scope": "global"})))
    assert res.status == 200, res.data
    rows = res.data["data"]["items"]
    assert [e["file"] for e in rows] == ["images/g.png"]
    assert rows[0]["blocked"] == ["project"], "项目资产里有同名 girl.png"

    # 反向：项目资产那条也该被标成 global（全局库已有同名）
    res2 = asyncio.run(fn(_Req(query={"dir": "demo", "scope": "project"})))
    rows2 = [e for e in res2.data["data"]["items"] if e["file"] == "assets/girl.png"]
    assert rows2 and rows2[0]["blocked"] == ["global"]


def test_lib_mirror_copy_refused_when_dup(handlers, proj, tmp_path, monkeypatch):
    """接口层兜底：项目已有同名时 copy 模式直接 409，不复制出第二份。"""
    _seed_global_lib(tmp_path, monkeypatch)
    before = sorted(os.listdir(proj["root"] / "assets"))
    fn = handlers[("POST", "/h3chain/lib_mirror")]
    res = asyncio.run(fn(_Req({"dir": "demo", "id": "global:images/g.png",
                               "mode": "copy", "label": "女主2"})))
    assert res.status == 409 and res.data.get("code") == "DUP_NAME"
    assert sorted(os.listdir(proj["root"] / "assets")) == before


def test_lib_archive_skips_dup_name(handlers, proj, tmp_path, monkeypatch):
    """接口层兜底：全局库已有同名 -> lib_archive 跳过而不是再存一份。"""
    _seed_global_lib(tmp_path, monkeypatch)
    fn = handlers[("POST", "/h3chain/lib_archive")]
    res = asyncio.run(fn(_Req({"dir": "demo", "ids": ["project:assets/girl.png"]})))
    assert res.status == 200, res.data
    assert res.data["archived"] == []
    assert any("全局库已有同名" in s for s in res.data["skipped"])


def test_upload_dest_finals(handlers, proj, tmp_path, monkeypatch):
    """dest=finals：拷进项目 finals/（目录扫描即见），不登记进资产清单。"""
    import asset_store as AS
    lib = tmp_path / "lib"
    monkeypatch.setattr(AS, "library_root", lambda *a, **k: str(lib))
    sys.modules["asset_store"] = AS
    fn = handlers[("POST", "/h3chain/library_upload")]
    req = _MultipartReq([
        _Field("kind", b"video"),
        _Field("dest", b"finals"),
        _Field("link_dir", b"demo"),
        _Field("alias", b"clip"),
        _Field("file", b"mp4-bytes-xyz", filename="clip.mp4"),
    ])
    res = asyncio.run(fn(req))
    assert res.status == 200, res.data
    assert res.data["stored"]["file"] == "finals/clip.mp4"
    assert (proj["root"] / "finals" / "clip.mp4").is_file()
    assert not res.data.get("manifest")


# ---- 标签唯一化：不能死循环 ----

def test_unique_label_stops_when_base_fills_cap(h3lib):
    """base 已占满 24 字也必须能停（旧实现截断后恒等于 base → 死循环 → 整台卡死）。

    真实触发：前端 alias = 文件名主干.slice(0, 24)。中文名很容易凑满 24 字，
    于是「在没有清理旧图的时候再传一张同名前缀的图」就会把 aiohttp 事件循环
    占死 —— 导演台、队列、所有 /h3chain 接口一起无响应（只能杀进程）。
    """
    base = "角" * 24
    got = h3lib.unique_label(base, {base})
    assert got and got != base and len(got) <= 24
    # 连撞 3 次也得各自不同
    taken = {base}
    seen = set()
    for _ in range(3):
        g = h3lib.unique_label(base, taken)
        assert g not in seen
        seen.add(g)
        taken.add(g)


def test_unique_label_plain_case_unchanged(h3lib):
    """不冲突时原样返回；短名冲突按 base2/base3 递增（旧行为保持不变）。"""
    assert h3lib.unique_label("女主", set()) == "女主"
    assert h3lib.unique_label("女主", {"女主"}) == "女主2"
    assert h3lib.unique_label("女主", {"女主", "女主2"}) == "女主3"


def test_upload_twice_with_same_long_alias_no_hang(handlers, proj, tmp_path, monkeypatch):
    """同 24 字前缀连续上传两张：第二张必须拿到不同标签，接口必须返回（不能挂）。"""
    import asset_store as AS
    monkeypatch.setattr(AS, "library_root", lambda *a, **k: str(tmp_path / "lib"))
    sys.modules["asset_store"] = AS
    fn = handlers[("POST", "/h3chain/library_upload")]
    long_alias = "角" * 30          # 前端 slice(0,24) 之后与下一张完全相同

    def _up(name, payload):
        return asyncio.run(fn(_MultipartReq([
            _Field("kind", b"image"),
            _Field("dest", b"project"),
            _Field("link_dir", b"demo"),
            _Field("alias", long_alias.encode("utf-8")),
            _Field("file", payload, filename=name),
        ])))

    r1 = _up("long_one.png", b"png-bytes-111")
    assert r1.status == 200, r1.data
    r2 = _up("long_two.png", b"png-bytes-222")
    assert r2.status == 200, r2.data
    assert r2.data["stored"]["label"] != r1.data["stored"]["label"]
    labels = [a["label"] for a in proj["state"]["mf"]["assets"]]
    assert len(labels) == len(set(labels)), labels
