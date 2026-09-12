"""素材库索引层（library.py）回归。

覆盖：四 scope 扫描 / 查询过滤与排序 / 分页契约（与 Majoor 同形状）/
关键词搜索 / 元数据 sidecar（rating·tags·collections）/ 段引用合入 /
文件操作（删除·打包·重命名·stage）。

无 ComfyUI 可跑：folder_paths / checkpoint 只在函数内延迟导入。
"""
import os
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import library as L  # noqa: E402


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    """临时项目：assets/ + videos/ + latents/ 各放几个文件。"""
    root = tmp_path / "h3_projects" / "demo"
    for sub in ("assets", "videos", "latents"):
        (root / sub).mkdir(parents=True)
    (root / "assets" / "girl.png").write_bytes(b"a" * 10)
    (root / "assets" / "bg.mp4").write_bytes(b"b" * 20)
    (root / "videos" / "seg_000.mp4").write_bytes(b"c" * 30)
    (root / "latents" / "kf.pt").write_bytes(b"d" * 40)
    monkeypatch.setattr(L, "_project_root",
                        lambda name: str(tmp_path / "h3_projects" / str(name)))
    monkeypatch.setattr(L, "_library_root", lambda: None)
    L.invalidate()
    return "demo"


# ---- 工具函数 ----

def test_kind_of_and_safe_rel():
    assert L.kind_of("a/b.PNG") == "image"
    assert L.kind_of("x.mp4") == "video"
    assert L.kind_of("k.pt") == "latent"
    assert L.kind_of("readme.txt") == ""
    assert L.safe_rel("assets/a b.png") == "assets/a b.png"
    assert L.safe_rel("../etc/passwd") == ""
    assert L.safe_rel("a//b") == "a/b"
    assert L.safe_rel("") == ""


# ---- 扫描 ----

def test_scan_scopes(proj):
    assets = L.scan_scope("project", proj)
    assert sorted(e["file"] for e in assets) == ["assets/bg.mp4", "assets/girl.png"]
    assert {e["scope"] for e in assets} == {"project"}

    finals = L.scan_scope("finals", proj)
    assert [e["file"] for e in finals] == ["videos/seg_000.mp4"]
    assert finals[0]["kind"] == "video"
    assert "成片" in finals[0]["origin"]

    lat = L.scan_scope("latent", proj)
    assert [e["file"] for e in lat] == ["latents/kf.pt"]
    assert lat[0]["kind"] == "latent"

    assert L.scan_scope("global", proj) == []     # 无全局库
    assert L.scan_scope("project", "") == []      # 无项目名


def test_build_index_counts(proj):
    items = L.build_index(proj)
    c = L.counters(items)
    assert c["project"] == 2 and c["finals"] == 1 and c["latent"] == 1
    # 缓存命中：连续两次构建结果一致
    assert len(L.build_index(proj)) == len(items)


# ---- 查询 / 分页 ----

def test_query_pagination_contract(proj):
    items = L.build_index(proj)
    r = L.query(items, page=1, page_size=2)
    assert set(r) == {"total", "page", "page_size", "total_pages", "items"}
    assert r["total"] == 4 and r["page_size"] == 2 and r["total_pages"] == 2
    assert len(r["items"]) == 2
    r2 = L.query(items, page=2, page_size=2)
    assert len(r2["items"]) == 2
    # 两页不重叠
    assert not ({e["id"] for e in r["items"]} & {e["id"] for e in r2["items"]})


def test_query_filters(proj):
    items = L.build_index(proj)
    assert L.query(items, scope="project")["total"] == 2
    assert L.query(items, scope="latent")["total"] == 1
    assert L.query(items, kind="video")["total"] == 2
    assert L.query(items, kind="media")["total"] == 3      # 不含 latent
    assert L.query(items, kind="latent")["total"] == 1
    assert L.query(items, scope="all", kind="all")["total"] == 4


def test_query_search(proj):
    items = L.build_index(proj)
    assert L.query(items, q="girl")["total"] == 1
    assert L.query(items, q="assets")["total"] == 2         # 命中文件路径
    assert L.query(items, q="video")["total"] >= 1          # 命中类型名
    assert L.query(items, q="不存在的东西")["total"] == 0


def test_query_sort_and_seg(proj):
    items = L.build_index(proj)
    by_name = L.query(items, sort="name", order="asc")
    names = [e["name"] for e in by_name["items"]]
    assert names == sorted(names, key=str.lower)
    # seg 过滤（无 refs 时应为空）
    assert L.query(items, seg=1)["total"] == 0


# ---- 元数据 sidecar ----

def test_meta_rating_tags(proj):
    A = L.build_index(proj)
    tid = [e for e in A if e["file"] == "assets/girl.png"][0]["id"]
    L.set_rating("project", proj, tid, 4)
    L.set_tags("project", proj, tid, ["女主", "  ", "雪"])
    table = L.all_meta(proj)
    assert table[tid]["rating"] == 4
    assert table[tid]["tags"] == ["女主", "雪"]            # 空串被剔除
    # 评分夹取
    assert L.set_rating("project", proj, tid, 99)["rating"] == 5
    assert L.set_rating("project", proj, tid, -3)["rating"] == 0
    # 合入条目
    A2 = L.apply_meta(L.build_index(proj), proj)
    hit = [e for e in A2 if e["id"] == tid][0]
    assert hit["rating"] == 0 and hit["tags"] == ["女主", "雪"]


def test_meta_target_global(proj):
    assert L.meta_target({"scope": "global"}, proj) == ("global", None)
    assert L.meta_target({"scope": "project"}, proj) == ("project", proj)


def test_collections(proj):
    c = L.save_collection(proj, "开场素材", ["project:assets/girl.png"])
    assert c["id"].startswith("c_") and c["name"] == "开场素材"
    assert [x["id"] for x in L.list_collections(proj)] == [c["id"]]
    # 改名 + 增项
    c2 = L.save_collection(proj, "开场素材2", ["a", "b"], cid=c["id"])
    assert c2["name"] == "开场素材2" and c2["items"] == ["a", "b"]
    assert L.delete_collection(proj, c["id"]) is True
    assert L.list_collections(proj) == []
    assert L.delete_collection(proj, "c_nope") is False
    with pytest.raises(ValueError):
        L.save_collection(proj, "   ")


# ---- 本插件语义 ----

def test_apply_aliases(proj):
    """索引初建的 name 是文件名；工程清单里的别名要覆盖它（否则 [[别名]] 匹配不上）。"""
    items = L.build_index(proj)
    mf = {"assets": [{"label": "女主", "file": "assets/girl.png", "kind": "image"}]}
    L.apply_aliases(items, mf)
    girl = [e for e in items if e["file"] == "assets/girl.png"][0]
    assert girl["name"] == "女主"
    # 未登记的条目保持文件名
    assert [e for e in items if e["file"] == "assets/bg.mp4"][0]["name"] == "bg.mp4"


def test_compute_refs(proj):
    items = L.build_index(proj)
    mf = {
        "assets": [{"label": "girl", "file": "assets/girl.png", "kind": "image"},
                   {"label": "bg", "file": "assets/bg.mp4", "kind": "video"}],
        "prompts": ["开场 [[girl]] 出现", "无引用"],
        "segments": [{"refs": []}, {"refs": ["bg"]}],
    }
    L.apply_aliases(items, mf)
    L.compute_refs(mf, items)
    girl = [e for e in items if e["name"] == "girl"][0]
    bg = [e for e in items if e["name"] == "bg"][0]
    assert girl["refs"] == [1]        # 来自 [[girl]] 文本
    assert bg["refs"] == [2]          # 来自段 refs


def test_apply_roles(proj):
    items = L.build_index(proj)
    mf = {"assets": [{"label": "女主", "file": "assets/girl.png",
                      "kind": "image", "roles": ["首帧图"]}]}
    L.apply_aliases(items, mf)
    L.apply_roles(items, mf)
    girl = [e for e in items if e["file"] == "assets/girl.png"][0]
    assert girl["name"] == "女主" and girl["roles"] == ["首帧图"]


# ---- 文件操作 ----

def test_delete_items(proj, tmp_path):
    p1 = tmp_path / "h3_projects" / "demo" / "assets" / "girl.png"
    p2 = tmp_path / "h3_projects" / "demo" / "assets" / "nope.png"
    r = L.delete_items([str(p1), str(p2)])
    assert r["deleted"] == ["girl.png"]
    assert r["skipped"] == ["nope.png"]
    assert not p1.exists()


def test_rename_item(proj, tmp_path):
    p = tmp_path / "h3_projects" / "demo" / "assets" / "bg.mp4"
    L.rename_item(str(p), "背景")
    assert (p.parent / "背景.mp4").is_file()
    with pytest.raises(ValueError):
        L.rename_item(str(p), "../evil")


def test_make_zip_and_resolve(proj, tmp_path):
    root = tmp_path / "h3_projects" / "demo"
    files = [str(root / "assets" / "girl.png"), str(root / "assets" / "bg.mp4")]
    r = L.make_zip(files, proj)
    assert r["count"] == 2 and os.path.isfile(r["abs"])
    with zipfile.ZipFile(r["abs"]) as z:
        assert sorted(z.namelist()) == ["bg.mp4", "girl.png"]
    with pytest.raises(ValueError):
        L.make_zip([], proj)

    items = L.build_index(proj)
    girl = [e for e in items if e["file"] == "assets/girl.png"][0]
    assert L.resolve_item_path(girl, proj).endswith("girl.png")
    assert L.resolve_item_path({"scope": "project", "file": "../x"}, proj) == ""


def test_mirror_to_project(proj, tmp_path, monkeypatch):
    """全局库条目 → 项目：拷文件进 assets/ 并登记 manifest（一步到位，不是只拷文件）。"""
    import sys
    import types

    saved = {}
    fake = types.ModuleType("projects")
    fake.safe_name = lambda n: str(n or "")
    fake.read_project = lambda n: {"assets": [{"label": "已有的", "kind": "image",
                                               "file": "assets/old.png"}]}

    def _save(n, assets, rev=None):
        saved["assets"] = assets
        return {"revision": 7}
    fake.save_assets = _save
    monkeypatch.setitem(sys.modules, "projects", fake)

    lib = tmp_path / "lib"
    (lib / "images").mkdir(parents=True)
    (lib / "images" / "x.png").write_bytes(b"png")
    monkeypatch.setattr(L, "_library_root", lambda: str(lib))

    item = {"scope": "global", "kind": "image", "name": "女主",
            "file": "images/x.png", "asset_id": "a_000000000000"}
    res = L.mirror_to_project("demo", item)
    assert res["label"] == "女主"
    assert res["file"].startswith("assets/")
    # 文件真的落到项目 assets/ 了
    assert (tmp_path / "h3_projects" / "demo" / res["file"]).is_file()
    # manifest 里也登记了（旧条目保留）
    labels = [a["label"] for a in saved["assets"]]
    assert "女主" in labels and "已有的" in labels
    # 源文件缺失要报错
    item2 = dict(item, file="images/nope.png")
    with pytest.raises(ValueError):
        L.mirror_to_project("demo", item2)


def test_invalidate(proj):
    L.build_index(proj)
    assert any(k.endswith("|demo") for k in L._INDEX_CACHE)
    L.invalidate(proj)
    assert not any(k.endswith("|demo") for k in L._INDEX_CACHE)
