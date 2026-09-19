"""素材库删除：物理文件 + **清单同步**（无 ComfyUI 可跑）。

跑法（仓库根目录）：
    python -m pytest tests/test_lib_delete_cleanup.py -q

回归两条线上症状 —— 同一个根因（删文件不摘清单）：
1) 全局库「根本删除不了」：`scan_scope("global")` 以全局库 manifest 为**唯一源**，
   只删文件不摘登记 -> 条目永久留在库里，瓦片删不掉。
2) 项目资产「删了但引用栏不少一栏」：瓦片扫真目录会消失（所以看着"能删"），
   但导演台「资产引用」栏以 `manifest["assets"]` 为唯一真相
   （前端 poolFromManifest 按它整体重建，不看磁盘）-> 幽灵素材永远留着。
   顺带：`segments[].refs` 与正文 `@别名` 也是死引用，必须一起清。
"""
import asyncio
import json
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3libdel_")


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
def astore(h3lib):
    return _load_top("asset_store", os.path.join(ROOT, "asset_store.py"))


@pytest.fixture(scope="module")
def routes(projects, h3lib, astore):
    return _load_top("routes", os.path.join(ROOT, "routes.py"),
                     [("from . import projects", "import projects"),
                      ("from . import asset_hub", "import asset_hub"),
                      ("from . import asset_store", "import asset_store"),
                      ("from . import library as h3lib", "import library as h3lib")])


@pytest.fixture()
def handlers(routes):
    h = {}

    def add_get(p, fn):
        h.setdefault(("GET", p), fn)

    def add_post(p, fn):
        h.setdefault(("POST", p), fn)

    routes.add_routes(types.SimpleNamespace(add_get=add_get, add_post=add_post))
    return h


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


GLOBAL_REL = "images/abc_demo.png"
# 与下面写入的 b"demo" 内容一致（内容寻址：sha256(b"demo") 前 12 位）
GLOBAL_SHA = "2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea"
GLOBAL_ID = "a_2a97516c354b"


@pytest.fixture()
def sandbox(tmp_path, h3lib, routes, monkeypatch):
    """真实临时工程 + 真实临时全局库（全部落磁盘，走 checkpoint.load/save）。"""
    out = tmp_path / "out"
    proj = out / "h3_projects" / "demo"
    (proj / "assets").mkdir(parents=True)
    (proj / "assets" / "girl.png").write_bytes(b"png")
    (proj / "assets" / "bg.png").write_bytes(b"bg")
    (proj / "assets" / "orphan.png").write_bytes(b"orphan")   # 未登记（清单里没有）
    lib = tmp_path / "library"
    (lib / "images").mkdir(parents=True)
    (lib / "images" / "abc_demo.png").write_bytes(b"demo")
    _write_json(str(proj / "manifest.json"), {
        "title": "demo", "revision": 5, "total": 2, "done": 0,
        "assets": [
            {"label": "女主", "kind": "image", "file": "assets/girl.png"},
            {"label": "背景", "kind": "image", "file": "assets/bg.png"},
        ],
        "asset_links": [{"asset_id": GLOBAL_ID, "alias": "库存素材", "kind": "image"}],
        "prompts": ["镜头一 @女主 走进 @背景", "镜头二 空镜"],
        "segments": [{"refs": ["女主", "背景"]}, {"refs": []}],
    })
    _write_json(str(lib / "manifest.json"), {
        "schema": "h3/asset-library-v1",
        "assets": [{"asset_id": GLOBAL_ID, "sha256": GLOBAL_SHA, "kind": "image",
                    "file": GLOBAL_REL, "orig_name": "demo.png", "bytes": 4,
                    "tags": [], "desc": "", "created_at": 0.0}],
    })
    fp = sys.modules["folder_paths"]
    monkeypatch.setattr(fp, "get_output_directory", lambda: str(out))
    # routes.py 顶部是 `from folder_paths import get_output_directory`（导入期绑定），
    # 只改假模块不够 —— 它手里那份引用也得指到临时目录，否则 delete_file 找不到文件。
    monkeypatch.setattr(routes, "get_output_directory", lambda: str(out), raising=False)
    monkeypatch.setattr(h3lib, "_library_root", lambda: str(lib))
    h3lib.invalidate()
    return {"proj": proj, "lib": lib, "dir": "demo"}


def _delete(handlers, sandbox, ids):
    fn = handlers[("POST", "/h3chain/lib_delete")]
    res = asyncio.run(fn(_Req({"dir": sandbox["dir"], "ids": ids})))
    return res.data


# ---- 全局库：以前"根本删不掉" ----

def test_global_delete_drops_library_manifest_entry(handlers, sandbox, h3lib):
    """文件删了，全局库 manifest 里的登记也必须摘 —— 否则瓦片永远留着。"""
    body = _delete(handlers, sandbox, ["global:" + GLOBAL_REL])
    assert body["ok"] is True
    assert body["deleted"] == ["abc_demo.png"]
    assert body["detached"] == 1
    assert not os.path.isfile(str(sandbox["lib"] / "images" / "abc_demo.png"))
    assert _read_json(str(sandbox["lib"] / "manifest.json"))["assets"] == []
    h3lib.invalidate()
    # 索引里也没了（这就是"删不掉"的正脸：以前这里仍是 1 条）
    assert h3lib.scan_scope("global") == []


def test_global_delete_unlinks_current_project(handlers, sandbox, h3lib):
    """本项目链着它 -> 顺手解开；否则项目里留一条指向已删文件的死链接。"""
    body = _delete(handlers, sandbox, ["global:" + GLOBAL_REL])
    assert body["unlinked"] == ["demo.png"]
    mf = _read_json(str(sandbox["proj"] / "manifest.json"))
    assert mf["asset_links"] == []
    assert "库存素材" not in mf["segments"][0]["refs"]
    assert "@库存素材" not in mf["prompts"][0]


def test_global_delete_without_project_link_keeps_manifest(handlers, sandbox, h3lib):
    """项目没链它时不动项目清单（别顺手改不该改的东西）。"""
    _write_json(str(sandbox["proj"] / "manifest.json"), {
        "title": "demo", "revision": 5,
        "assets": [{"label": "女主", "kind": "image", "file": "assets/girl.png"}],
        "asset_links": [],
        "prompts": ["镜头一 @女主"], "segments": [{"refs": ["女主"]}],
    })
    body = _delete(handlers, sandbox, ["global:" + GLOBAL_REL])
    assert body["unlinked"] == []
    mf = _read_json(str(sandbox["proj"] / "manifest.json"))
    assert mf["revision"] == 5                  # 清单没被碰过
    assert mf["assets"][0]["label"] == "女主"


def test_global_delete_is_idempotent(handlers, sandbox):
    """重复删同一条：不报错，也不再摘（第二次已经没有了）。"""
    _delete(handlers, sandbox, ["global:" + GLOBAL_REL])
    body = _delete(handlers, sandbox, ["global:" + GLOBAL_REL])
    assert body["ok"] is True
    assert body["detached"] == 0


def test_global_delete_keeps_file_when_manifest_absent(handlers, sandbox, h3lib):
    """登记已经不在（孤儿文件）时也只报跳过，不炸。"""
    _write_json(str(sandbox["lib"] / "manifest.json"),
                {"schema": "h3/asset-library-v1", "assets": []})
    h3lib.invalidate()
    body = _delete(handlers, sandbox, ["global:" + GLOBAL_REL])
    assert body["ok"] is True
    assert body["detached"] == 0


# ---- 项目资产：以前"删了引用栏不少一栏" ----

def test_project_delete_cleans_pool_and_refs(handlers, sandbox, h3lib):
    """manifest["assets"]（引用栏唯一真相）+ seg.refs + 正文 @别名 三处都要清。"""
    body = _delete(handlers, sandbox, ["project:assets/girl.png"])
    assert body["ok"] is True
    assert body["deleted"] == ["girl.png"]
    assert not os.path.isfile(str(sandbox["proj"] / "assets" / "girl.png"))
    mf = _read_json(str(sandbox["proj"] / "manifest.json"))
    # 引用栏的源：少了一栏（以前这里仍是 ["女主","背景"]）
    assert [a["label"] for a in mf["assets"]] == ["背景"]
    assert "女主" not in mf["segments"][0]["refs"]
    assert mf["segments"][0]["refs"] == ["背景"]
    assert "@女主" not in mf["prompts"][0]
    assert "@背景" in mf["prompts"][0]          # 别的引用不能误伤
    assert mf["revision"] > 5                   # 清单真落盘了


def test_project_delete_only_touches_target(handlers, sandbox, h3lib):
    """删「背景」只清「背景」：另一个素材（女主）的清单条目/引用纹丝不动。"""
    body = _delete(handlers, sandbox, ["project:assets/bg.png"])
    assert body["ok"] is True
    mf = _read_json(str(sandbox["proj"] / "manifest.json"))
    assert [a["label"] for a in mf["assets"]] == ["女主"]
    assert mf["segments"][0]["refs"] == ["女主"]
    assert "@背景" not in mf["prompts"][0]
    assert "@女主" in mf["prompts"][0]


def test_project_delete_unregistered_file_keeps_manifest(handlers, sandbox, h3lib):
    """assets/ 里没登记的文件：文件删掉，清单原样（不能按文件名误摘同名 label）。"""
    before = _read_json(str(sandbox["proj"] / "manifest.json"))
    body = _delete(handlers, sandbox, ["project:assets/orphan.png"])
    assert body["ok"] is True
    assert body["deleted"] == ["orphan.png"]
    assert not os.path.isfile(str(sandbox["proj"] / "assets" / "orphan.png"))
    assert _read_json(str(sandbox["proj"] / "manifest.json")) == before


# ---- 链接条目：只解链，全局库那份文件必须活着 ----

def test_linked_entry_only_unlinks(handlers, sandbox, h3lib):
    body = _delete(handlers, sandbox, ["project:" + GLOBAL_REL])
    assert body["ok"] is True
    assert body["unlinked"] == ["库存素材"]
    assert body["deleted"] == []                 # 链接条目不删文件
    assert os.path.isfile(str(sandbox["lib"] / "images" / "abc_demo.png"))
    mf = _read_json(str(sandbox["proj"] / "manifest.json"))
    assert mf["asset_links"] == []
    assert _read_json(str(sandbox["lib"] / "manifest.json"))["assets"] != []


# ---- 跨项目提醒：删全局素材要说出"会波及谁" ----

def test_global_delete_warns_other_projects(handlers, sandbox, tmp_path, monkeypatch):
    other = sandbox["proj"].parent / "other"
    other.mkdir(parents=True, exist_ok=True)
    _write_json(str(other / "manifest.json"), {
        "title": "other", "revision": 1,
        "asset_links": [{"asset_id": GLOBAL_ID, "alias": "库存素材", "kind": "image"}],
    })
    body = _delete(handlers, sandbox, ["global:" + GLOBAL_REL])
    assert any("other" in n for n in body["notes"]), body["notes"]
    assert any("引用" in n for n in body["notes"])


# ---- 历史幽灵条目自愈（老版本留下的：文件已删、登记还在） ----

def test_scan_global_hides_ghost_entries(sandbox, h3lib):
    """文件不在就不列（只读自愈）：老用户库里攒的幽灵不用手工清。"""
    os.remove(str(sandbox["lib"] / "images" / "abc_demo.png"))
    h3lib.invalidate()
    assert h3lib.scan_scope("global") == []
    # 只读：manifest 不动（读路径绝不落盘）
    assert len(_read_json(str(sandbox["lib"] / "manifest.json"))["assets"]) == 1


def test_reupload_repairs_ghost_entry(sandbox, astore):
    """重新上传同一份内容：不能"秒传"回一条看不见的幽灵。"""
    lib = str(sandbox["lib"])
    src = str(sandbox["lib"] / "images" / "abc_demo.png")
    os.remove(src)
    again = os.path.join(str(sandbox["lib"]), "again.png")
    with open(again, "wb") as f:
        f.write(b"demo")                      # 与登记内容一致 -> 同 sha/asset_id
    e = astore.register_content(lib, again, "image", orig_name="again.png")
    assert e["asset_id"] == GLOBAL_ID
    assert os.path.isfile(str(sandbox["lib"] / "images" / "abc_demo.png")), "文件没被补齐"
    assert len(_read_json(str(sandbox["lib"] / "manifest.json"))["assets"]) == 1


# ---- 成片历史删除：同一个根因，右栏卡片也不能"删了还在" ----

def test_delete_file_forgets_final_and_merge(handlers, sandbox):
    proj = sandbox["proj"]
    (proj / "final_a.mp4").write_bytes(b"v")
    mf = _read_json(str(proj / "manifest.json"))
    mf["finals"] = ["final_a.mp4", "final_b.mp4"]
    mf["merges"] = [{"file": "final_a.mp4", "n": 2}, {"file": "merged_x.mp4"}]
    mf["videos"] = ["finals/seg_000.mp4"]
    _write_json(str(proj / "manifest.json"), mf)
    fn = handlers[("POST", "/h3chain/delete_file")]
    res = asyncio.run(fn(_Req({"path": "h3_projects/demo/final_a.mp4"})))
    assert res.data["ok"] is True
    assert not os.path.isfile(str(proj / "final_a.mp4"))
    mf2 = _read_json(str(proj / "manifest.json"))
    assert mf2["finals"] == ["final_b.mp4"]
    assert [m["file"] for m in mf2["merges"]] == ["merged_x.mp4"]
    assert mf2["videos"] == ["finals/seg_000.mp4"], "videos 是链状态，不该被当历史清掉"


# ---- 前端接线：删完必须让引用栏跟着少一栏（源码级守卫） ----

def _src(name):
    with open(os.path.join(ROOT, "web", name), encoding="utf-8") as f:
        return f.read()


def test_library_frontend_surfaces_notes():
    """后端的"会波及谁"提醒得显示出来，否则用户看不到断链风险。"""
    d = _src("h3_library.js")
    assert "r.body.notes" in d, "删除结果里的提醒没展示"
    assert 'x.scope === "global"' in d, "确认框没区分全局库条目"


def test_director_has_no_overlay_chips_to_repaint():
    """总提示词框不再有「参考素材（AI 可见）」chips，活渲染通道随之删除。

    那条通道是专为 chips 建的（删完素材那一栏要跟着减）。总提示词框退化成纯
    分段流水线后不再有 chips，留着 registerLivePainter 就是没人注册的死代码 ——
    段卡那边的同类需求走 _cardPainters。
    """
    d = _src("h3_director.js")
    for dead in ["function registerLivePainter(", "function runLivePainters(",
                 "registerLivePainter(paintLive);", "h3d-mprefs"]:
        assert dead not in d, f"{dead} 应随 chips 一起删除"
    assert "function registerCardPainter(" in d, "段卡的卡片级活渲染仍要在"
