"""素材标注（mark）回归：发给 LLM 的稳定短编号，与官方 <Picture N> 分层。

跑法（仓库根目录）：
    python -m pytest tests/test_asset_mark.py -q

覆盖：clean_mark / mark_shaped / next_mark / assign_marks / mark_taken 语义、
clean_alias 对「图片N」形态的错开、projects.link_asset 自动发号（池序）、
normalize_marks 幂等与旧 assets 优先、set_asset_mark 手改/查重/清除、
routes `/h3chain/asset_links` 回带标注、`/h3chain/asset_link` 显式标注、
`/h3chain/asset_mark` handler 与路由挂载。

**分层不变量**：标注只发给 LLM（web 侧 marksToText/textToMarks 换码），
盘上 `prompts` 永远存 `@别名`；标注号 ≠ `<Picture k>`（后者执行期按本段挂载序重算）。
"""

import asyncio
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3marktest_")


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
    return _load_top("routes", os.path.join(ROOT, "routes.py"),
                     [("from . import projects", "import projects"),
                      ("from . import asset_hub", "import asset_hub"),
                      ("from . import asset_store", "import asset_store"),
                      ("from . import prompts as _prompts", "import prompts as _prompts"),
                      ("from . import library as h3lib", "import library as h3lib")])


# ---- 标注原语 ----

def test_clean_mark_whitelist(store):
    assert store.clean_mark("图片1") == "图片1"
    assert store.clean_mark(" 视频12 ") == "视频12"
    assert store.clean_mark("音频999") == "音频999"
    # 非法一律 ""（由调用方发新号，不猜）
    assert store.clean_mark("图片0") == ""
    assert store.clean_mark("图片1000") == ""
    assert store.clean_mark("图1") == ""
    assert store.clean_mark("图片") == ""
    assert store.clean_mark("img1") == ""
    assert store.clean_mark("") == ""
    assert store.clean_mark(None) == ""


def test_mark_shaped(store):
    assert store.mark_shaped("图片7") is True
    assert store.mark_shaped("音频1") is True
    assert store.mark_shaped("图片") is False
    assert store.mark_shaped("阿依") is False


def test_next_mark_per_kind(store):
    items = [{"mark": "图片1", "kind": "image"},
             {"mark": "图片3", "kind": "image"},
             {"mark": "视频2", "kind": "video"}]
    assert store.next_mark(items, "image") == "图片2"     # 最小空闲（补空号，不顶到 4）
    assert store.next_mark(items, "video") == "视频1"
    assert store.next_mark(items, "audio") == "音频1"     # 空表从 1 起
    assert store.next_mark([], "image") == "图片1"
    # 满号保护（不返回 1000）
    full = [{"mark": f"图片{i}", "kind": "image"} for i in range(1, store.MARK_MAX + 1)]
    assert store.next_mark(full, "image") == f"图片{store.MARK_MAX}"


def test_assign_marks_pool_order_and_idempotent(store):
    items = [{"alias": "a", "kind": "image"},
             {"alias": "b", "kind": "video"},
             {"alias": "c", "kind": "image"},
             {"alias": "d", "kind": "image", "mark": "图片9", "mark_auto": False}]
    assert store.assign_marks(items) is True
    assert [x["mark"] for x in items] == ["图片1", "视频1", "图片2", "图片9"]
    # 落盘口径：缺省即自动 → 自动条目**不写** mark_auto（只存手动 False）
    assert "mark_auto" not in items[0]
    assert items[3]["mark_auto"] is False
    # 幂等：第二次无改动
    assert store.assign_marks(items) is False


def test_mark_taken_dedupe(store):
    items = [{"alias": "a", "mark": "图片1", "asset_id": "a_111111111111"},
             {"alias": "b", "mark": "图片2", "asset_id": "a_222222222222"}]
    assert store.mark_taken(items, "图片1") is True
    assert store.mark_taken(items, "图片3") is False
    # 排除自己不算占用（改名场景）
    assert store.mark_taken(items, "图片1", exclude_id="a_111111111111") is False
    assert store.mark_taken(items, "图片1", exclude_label="a") is False


def test_clean_alias_never_mark_shaped(store):
    """别名与标注同形会串号 → clean_alias 必须错开。"""
    assert store.clean_alias("图片1") == "图片1_"
    assert store.clean_alias("图片1.png") == "图片1_"
    assert store.clean_alias("视频12") == "视频12_"
    # 正常名不受影响
    assert store.clean_alias("阿依.png") == "阿依"
    assert store.clean_alias("图片集1") == "图片集1"
    assert store.clean_alias("图1") == "图1"


# ---- link_asset 自动发号（池序） ----

def _link(projects, name, aid, alias, kind, rev=None, mark=None):
    return projects.link_asset(name, aid, alias, kind, base_revision=rev, mark=mark)


def test_link_asset_assigns_mark_by_pool_order(projects):
    m = projects.create_project("t_mark1")
    assert (m.get("asset_links") or []) == []
    m = _link(projects, "t_mark1", "a_111111111111", "图甲", "image")
    assert m["asset_links"][0]["mark"] == "图片1"
    assert "mark_auto" not in m["asset_links"][0]      # 缺省即自动
    m = _link(projects, "t_mark1", "a_222222222222", "片乙", "video")
    assert [x["mark"] for x in m["asset_links"]] == ["图片1", "视频1"]
    m = _link(projects, "t_mark1", "a_333333333333", "图丙", "image")
    assert [x["mark"] for x in m["asset_links"]] == ["图片1", "视频1", "图片2"]


def test_manual_mark_does_not_inflate_auto_numbering(projects):
    """手动标成「图片9」不该把自动序列顶到 10 —— 新素材照样拿空缺的 2。"""
    projects.create_project("t_mark1b")
    _link(projects, "t_mark1b", "a_111111111111", "甲", "image")
    _link(projects, "t_mark1b", "a_222222222222", "乙", "image", mark="图片9")
    m = _link(projects, "t_mark1b", "a_333333333333", "丙", "image")
    assert {x["alias"]: x["mark"] for x in m["asset_links"]} == {
        "甲": "图片1", "乙": "图片9", "丙": "图片2"}


def test_link_asset_keeps_mark_on_repoint_and_rename(projects):
    """同 alias 重指向 = 池槽位不变，标注跟着槽位走（不重发）。"""
    projects.create_project("t_mark2")
    _link(projects, "t_mark2", "a_111111111111", "主角", "image")
    m = _link(projects, "t_mark2", "a_222222222222", "主角", "image")
    assert len(m["asset_links"]) == 1
    assert m["asset_links"][0]["asset_id"] == "a_222222222222"
    assert m["asset_links"][0]["mark"] == "图片1"


def test_link_asset_explicit_mark_and_dedupe(projects):
    projects.create_project("t_mark3")
    _link(projects, "t_mark3", "a_111111111111", "甲", "image")
    m = _link(projects, "t_mark3", "a_222222222222", "乙", "image", mark="图片7")
    marks = {x["alias"]: x["mark"] for x in m["asset_links"]}
    assert marks == {"甲": "图片1", "乙": "图片7"}
    assert [x for x in m["asset_links"] if x["alias"] == "乙"][0]["mark_auto"] is False
    # 与已有标注重复 → 报错
    with pytest.raises(ValueError, match="已被其它素材占用"):
        _link(projects, "t_mark3", "a_333333333333", "丙", "image", mark="图片1")
    # 形态非法 → 报错
    with pytest.raises(ValueError, match="形态"):
        _link(projects, "t_mark3", "a_333333333333", "丙", "image", mark="图7")


def test_normalize_marks_legacy_first_and_idempotent(projects):
    """池序 = 旧 assets 先、asset_links 覆盖同名（与前端 poolFromManifest 同口径）。"""
    projects.create_project("t_mark4")
    projects.save_assets("t_mark4", [
        {"label": "旧图A", "kind": "image", "file": "assets/a.png"},
        {"label": "旧图B", "kind": "image", "file": "assets/b.png"},
    ])
    mf, changed = projects.normalize_marks("t_mark4")
    assert changed is True
    assert [x["mark"] for x in mf["assets"]] == ["图片1", "图片2"]
    # 第二次：无改动（幂等，不涨 revision）
    mf2, changed2 = projects.normalize_marks("t_mark4")
    assert changed2 is False
    assert mf2["revision"] == mf["revision"]
    # 缺项目
    assert projects.normalize_marks("no_such") == (None, False)


def test_mark_pool_legacy_first_then_links(projects):
    """池序：旧 assets 在前，asset_links 顺延（不是各自从 1 起）。"""
    projects.create_project("t_mark5")
    projects.save_assets("t_mark5", [
        {"label": "旧图A", "kind": "image", "file": "assets/a.png"},
    ])
    m = projects.link_asset("t_mark5", "a_111111111111", "链图B", "image")
    pool = [(x.get("label") or x.get("alias"), x.get("mark"))
            for x in projects._mark_pool(m)]
    assert pool == [("旧图A", "图片1"), ("链图B", "图片2")]


def test_mark_pool_link_overrides_same_label(projects):
    """同名时链接胜出并**顶替原位置**（Map.set 语义），标注也随之落到链接条目上。"""
    projects.create_project("t_mark5b")
    projects.save_assets("t_mark5b", [
        {"label": "同名", "kind": "image", "file": "assets/a.png"},
        {"label": "旧图B", "kind": "image", "file": "assets/b.png"},
    ])
    m = projects.link_asset("t_mark5b", "a_111111111111", "同名", "image")
    pool = projects._mark_pool(m)
    assert pool[0].get("asset_id") == "a_111111111111"        # 位置 0 被链接顶替
    assert [x.get("mark") for x in pool] == ["图片1", "图片2"]
    assert projects.normalize_marks("t_mark5b")[1] is False    # 已齐，不再改动


def test_set_asset_mark_manual_dedupe_and_clear(projects):
    projects.create_project("t_mark6")
    _link(projects, "t_mark6", "a_111111111111", "甲", "image")
    _link(projects, "t_mark6", "a_222222222222", "乙", "image")
    m = projects.set_asset_mark("t_mark6", "图片9", alias="乙")
    got = {x["alias"]: (x.get("mark"), x.get("mark_auto")) for x in m["asset_links"]}
    assert got["乙"] == ("图片9", False)
    # 查重
    with pytest.raises(ValueError, match="已被其它素材占用"):
        projects.set_asset_mark("t_mark6", "图片1", alias="乙")
    # 非法形态
    with pytest.raises(ValueError, match="形态"):
        projects.set_asset_mark("t_mark6", "素材9", alias="乙")
    # 找不到目标
    with pytest.raises(ValueError, match="不在项目库"):
        projects.set_asset_mark("t_mark6", "图片8", alias="不存在")
    # 清除 → 自动补号重新接手
    m = projects.set_asset_mark("t_mark6", "", alias="乙")
    assert "mark" not in [x for x in m["asset_links"] if x["alias"] == "乙"][0]
    m = projects.set_asset_mark("t_mark6", "", alias="甲")
    mf, changed = projects.normalize_marks("t_mark6")
    assert changed is True
    assert [x["mark"] for x in mf["asset_links"]] == ["图片1", "图片2"]


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


def _handlers(routes):
    h = {}
    r2 = types.SimpleNamespace(
        add_get=lambda p, fn: h.setdefault(("GET", p), fn),
        add_post=lambda p, fn: h.setdefault(("POST", p), fn))
    routes.add_routes(r2)
    return h


def test_asset_mark_routes_mounted(routes):
    r = _Router()
    routes.add_routes(r)
    for method, p in [("GET", "/h3chain/asset_links"),
                      ("POST", "/h3chain/asset_link"),
                      ("POST", "/h3chain/asset_mark"),
                      ("POST", "/h3chain/asset_unlink")]:
        assert (method, p) in r.paths, (method, p)
        assert (method, "/api" + p) in r.paths, (method, "/api" + p)


def test_asset_links_handler_returns_mark(routes, projects):
    projects.create_project("t_route_mark")
    projects.link_asset("t_route_mark", "a_111111111111", "甲", "image")
    h = _handlers(routes)
    res = asyncio.run(h[("GET", "/h3chain/asset_links")](_Req(query={"dir": "t_route_mark"})))
    assert res.status == 200
    links = res.data["links"]
    assert links[0]["mark"] == "图片1"
    assert links[0]["mark_auto"] is True


def test_asset_link_handler_explicit_mark(routes, projects):
    projects.create_project("t_route_mark2")
    h = _handlers(routes)
    fn = h[("POST", "/h3chain/asset_link")]
    ok = asyncio.run(fn(_Req({"dir": "t_route_mark2", "asset_id": "a_111111111111",
                              "alias": "甲", "kind": "image", "mark": "图片5"})))
    assert ok.status == 200
    assert ok.data["manifest"]["asset_links"][0]["mark"] == "图片5"
    bad = asyncio.run(fn(_Req({"dir": "t_route_mark2", "asset_id": "a_222222222222",
                               "alias": "乙", "kind": "image", "mark": "素材5"})))
    assert bad.status == 400


def test_asset_mark_handler(routes, projects):
    projects.create_project("t_route_mark3")
    projects.link_asset("t_route_mark3", "a_111111111111", "甲", "image")
    h = _handlers(routes)
    fn = h[("POST", "/h3chain/asset_mark")]
    bad_name = asyncio.run(fn(_Req({"dir": "../x", "mark": "图片2"})))
    assert bad_name.status == 400
    missing = asyncio.run(fn(_Req({"dir": "t_noproj", "mark": "图片2", "alias": "甲"})))
    assert missing.status == 404
    ok = asyncio.run(fn(_Req({"dir": "t_route_mark3", "mark": "图片4", "alias": "甲"})))
    assert ok.status == 200
    assert ok.data["manifest"]["asset_links"][0]["mark"] == "图片4"
    no_target = asyncio.run(fn(_Req({"dir": "t_route_mark3", "mark": "图片6",
                                     "alias": "查无此人"})))
    assert no_target.status == 400
