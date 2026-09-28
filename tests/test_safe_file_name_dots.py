"""文件名里的 `..` 不能被当成路径穿越（素材登记回归）。

跑法（仓库根目录）：
    python -m pytest tests/test_safe_file_name_dots.py -q

背景（线下线上都踩过）：`safe_name` 曾经有一条 `".." in s` 的子串检查，本意是防目录
穿越，实际把**合法文件名**一起毙了 —— 手机/相机产出的 `IMG1788509671869..jpg` 就是
这种名字。它被 `_clean_asset`、`import_asset` 等**文件名校验点**共用，于是：

    上传时文件照常拷进 <proj>/assets/，登记清单时条目被静默丢弃。

症状：**素材库看得到（它扫目录）、分段提示词框里选不到（它读清单）**，全程不报错。

本文件钉四件事：
  1. 合法文件名（含 `..`、多个点）必须通过；
  2. 真正的穿越（路径分隔、点开头、整段 `..`、盘符）必须继续被拒；
  3. `_clean_asset` 不得丢弃这种条目（这是丢素材的那一刀）；
  4. `repair_assets` 能把这种「文件在、清单没有」的孤儿补回清单，且**只加不删**。
"""

import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3dots_")


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
    fp.get_user_directory = lambda: TMP
    sys.modules["folder_paths"] = fp
    os.makedirs(os.path.join(TMP, "h3_projects"), exist_ok=True)
    yield


@pytest.fixture(scope="session")
def store():
    return _load_top("asset_store", os.path.join(ROOT, "asset_store.py"))


@pytest.fixture(scope="session")
def checkpoint():
    return _load_top("checkpoint", os.path.join(ROOT, "checkpoint.py"))


@pytest.fixture(scope="session")
def library(store):
    return _load_top("library", os.path.join(ROOT, "library.py"))


@pytest.fixture(scope="session")
def projects(checkpoint, library):
    return _load_top("projects", os.path.join(ROOT, "projects.py"),
                     [("from . import checkpoint", "import checkpoint")])


# 合法：手机/相机真的会产出这种名字；多点也合法
LEGAL = ["IMG1788509671869..jpg", "IMG1788509692805..jpg", "a..b.png",
         "a.b.c.png", "normal.jpg", "素材 1.mp4"]

# 非法：穿越/越权靠这四条拦住，与点无关
ILLEGAL = ["", "..", ".", ".hidden", "a/b", "a\\b", "C:x", "D:\\x"]


def test_safe_name_accepts_legal_double_dot(projects, store):
    for n in LEGAL:
        assert projects.safe_name(n) == n, f"projects.safe_name 误拒：{n!r}"
        assert store.safe_name(n) == n, f"asset_store.safe_name 误拒：{n!r}"


def test_safe_name_still_blocks_traversal(projects, store):
    for n in ILLEGAL:
        assert projects.safe_name(n) == "", f"projects.safe_name 漏放：{n!r}"
        assert store.safe_name(n) == "", f"asset_store.safe_name 漏放：{n!r}"


def test_two_safe_name_copies_agree(projects, store):
    """两份 safe_name 是「同语义复刻」，不许各自漂移。"""
    for n in LEGAL + ILLEGAL:
        assert (projects.safe_name(n) or "") == (store.safe_name(n) or ""), n


def test_clean_asset_keeps_double_dot_file(projects):
    for n in LEGAL:
        ent = projects._clean_asset({"label": "L", "kind": "image",
                                     "file": "assets/" + n})
        assert ent is not None, f"_clean_asset 丢掉了合法条目：{n!r}"
        assert ent["file"] == "assets/" + n


def test_clean_asset_still_rejects_traversal(projects):
    for f in ("assets/..", "../x.png", "assets/../../etc/passwd", "",
              "assets/nodot", "a/b/c.png"):
        assert projects._clean_asset({"label": "L", "kind": "image", "file": f}) is None, f


def test_clean_asset_normalizes_leading_slash_to_relative(projects):
    """开头斜杠被丢掉 = 落回项目内相对路径，**不是**穿越，允许但必须去掉根。"""
    ent = projects._clean_asset({"label": "L", "kind": "image", "file": "/abs/x.png"})
    assert ent is not None and ent["file"] == "abs/x.png"


def test_repair_assets_registers_double_dot_orphan(projects, checkpoint):
    """端到端：文件在 assets/、清单里没有 → 补回，且只加不删。"""
    name = "t_dots"
    root = os.path.join(checkpoint.projects_root(), name)
    os.makedirs(os.path.join(root, "assets"), exist_ok=True)
    for fn in LEGAL[:3] + ["notes.txt"]:
        with open(os.path.join(root, "assets", fn), "wb") as f:
            f.write(b"x" * 8)

    checkpoint.save_manifest(root, {
        "schema": checkpoint.SCHEMA, "manifest_schema": projects.MANIFEST_SCHEMA,
        "revision": 3, "done": 0, "total": 0, "title": name,
        "created_at": 0, "updated_at": 0, "prompts": [], "params": {},
        "finals": [], "assets": [{"label": "手工留的", "kind": "image",
                                  "file": "assets/keep.png"}],
    })

    res = projects.repair_assets(name)
    files = [a.get("file") for a in res["assets"]]
    assert "assets/IMG1788509671869..jpg" in files, "双点文件名仍没补回清单"
    assert "assets/IMG1788509692805..jpg" in files
    assert "assets/a..b.png" in files
    assert "assets/notes.txt" not in files, "非媒体文件不该进池"
    assert "assets/keep.png" in files, "只加不删：原有条目必须保留"
    assert res["revision"] == 4
    # 引用名必须等于**真名**（含两个点）——折成一个点会让素材库里选中的真名匹配不上池子
    for a in res["assets"]:
        if a.get("file", "").startswith("assets/IMG"):
            assert a["ref_name"] == a["file"].split("/")[-1], a
    # 手写的 keep.png 没有 ref_name → 第一次跑顺手补正（派生字段，不算增删）
    assert res["fixed"] == ["assets/keep.png"]

    # 幂等：再跑一次没有新东西、没有要改的，也不动 revision
    again = projects.repair_assets(name)
    assert again["added"] == [] and again["fixed"] == []
    assert again["revision"] == 4
