"""P2 执行解耦回归（无 ComfyUI 可跑；nodes 执行需 Comfy 内验）。

跑法（仓库根目录）：
    python -m pytest tests/test_exec_p2.py -q

覆盖：store 三级寻址顺序、执行期注册表、ref_key 四形态、compile_refs 与旧
check_segment_refs 行为等价（label 链）、nodes.py 接线字符串（校验核/加载期/
锚点/报告/autogrow deprecated/转码排空）。
"""

import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3p2test_")


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
    yield


@pytest.fixture(scope="session")
def store():
    return _load_top("asset_store", os.path.join(ROOT, "asset_store.py"))


@pytest.fixture(scope="session")
def asset_hub():
    return _load_top("asset_hub", os.path.join(ROOT, "asset_hub.py"))


# ---- ref_key / 注册表 ----

def test_ref_key_forms(store):
    assert store.ref_key(" 角色1 ") == ("角色1", "")
    assert store.ref_key({"asset": "a_1234567890ab", "use": "首帧图"}) == ("a_1234567890ab", "首帧图")
    assert store.ref_key({"id": "x", "role": "r"}) == ("x", "r")
    assert store.ref_key({"label": "L"}) == ("L", "")
    assert store._ref_key("A") == store.ref_key("A")


def test_build_execution_registry(store):
    pool = [("image", "角色1", "assets/a.png", []),
            ("video", "片", "assets/b.mp4", ["x"]),
            ("image", "", "assets/noname.png", []),   # 无 label 跳过
            "garbage",                                 # 非法条目跳过
            ("audio", "乐", "", [])]                   # 无 file 跳过
    links = [{"asset_id": "a_cccccccccccc", "alias": "链接", "kind": "image"}]
    reg = store.build_execution_registry(pool, links, None)
    assert "角色1" in reg["by_alias"] and "片" in reg["by_alias"]
    assert "链接" in reg["by_alias"]
    assert reg["by_id"]["a_cccccccccccc"]["alias"] == "链接"
    # pool 明细进 legacy_file，可寻址
    assert reg["by_alias"]["角色1"]["legacy_file"] == "assets/a.png"


# ---- 三级寻址 ----

def _mk(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(content)


def test_resolve_order(store):
    proj = os.path.join(TMP, "proj1")
    lib = os.path.join(TMP, "lib1")
    _mk(os.path.join(proj, "assets", "a.png"), b"proj")
    _mk(os.path.join(lib, "images", "ab12_a.png"), b"lib")
    rec = {"asset_id": "a_ab12ab12ab12", "alias": "A", "kind": "image",
           "file": "images/ab12_a.png", "legacy_file": "assets/a.png"}
    # 项目优先
    assert store.resolve_absolute(rec, proj, lib) == os.path.join(proj, "assets", "a.png")
    # 项目缺失 -> 全局
    os.remove(os.path.join(proj, "assets", "a.png"))
    assert store.resolve_absolute(rec, proj, lib) == os.path.join(lib, "images", "ab12_a.png")
    # 都缺 -> None（调用方走旧兼容路径，报错口径不变）
    os.remove(os.path.join(lib, "images", "ab12_a.png"))
    assert store.resolve_absolute(rec, proj, lib) is None
    # 纯字符串：只查项目内
    _mk(os.path.join(proj, "assets", "b.png"))
    assert store.resolve_absolute("assets/b.png", proj, lib) == os.path.join(proj, "assets", "b.png")
    assert store.resolve_absolute("assets/nope.png", proj, lib) is None
    assert store.resolve_absolute("assets/b.png") is None
    assert store.resolve_absolute(None, proj, lib) is None


def test_try_library_root_no_raise(store):
    assert isinstance(store.try_library_root(), str)


# ---- 新核 vs 旧核等价（label 链） ----

def _reg_from_pool(store, pool):
    legacy = [{"label": l, "kind": k, "file": f} for k, l, f in pool]
    return store.build_registry(None, None, legacy)


def test_kernel_parity_ok(store, asset_hub):
    pool = [("image", "角色1", "a.png"), ("video", "片", "b.mp4"), ("audio", "乐", "c.mp3")]
    items = [{"label": l, "kind": k, "file": f} for k, l, f in pool]
    refs = ["角色1", "片", "乐"]
    old = asset_hub.check_segment_refs(items, refs, 1)
    new = store.compile_refs(_reg_from_pool(store, pool), refs, 1)
    assert old == [] and new["ok"] is True
    assert [b["token"] for b in new["blocks"]] == ["<Picture 1>", "<Video 1>", "<Audio 1>"]


def test_kernel_parity_unknown(store, asset_hub):
    pool = [("image", "A", "a.png")]
    items = [{"label": l, "kind": k, "file": f} for k, l, f in pool]
    old = asset_hub.check_segment_refs(items, ["谁"], 2)
    new = store.compile_refs(_reg_from_pool(store, pool), ["谁"], 2)
    assert old and old[0]["code"] == "E_REF_UNKNOWN"
    assert not new["ok"] and new["errors"][0]["code"] == "E_REF_UNKNOWN"


def test_kernel_parity_caps(store, asset_hub):
    pool = [( "image", f"图{i}", f"u{i}.png") for i in range(10)]
    items = [{"label": l, "kind": k, "file": f} for k, l, f in pool]
    refs = [f"图{i}" for i in range(10)]
    old = asset_hub.check_segment_refs(items, refs, 3)
    new = store.compile_refs(_reg_from_pool(store, pool), refs, 3)
    assert old and old[0]["code"] == "E_MEDIA_LIMIT"
    assert not new["ok"] and new["errors"][0]["code"] == "E_MEDIA_LIMIT"


def test_new_kernel_accepts_ids(store):
    reg = store.build_registry(
        [{"asset_id": "a_dddddddddddd", "kind": "image", "file": "images/x.png"}],
        [{"asset_id": "a_eeeeeeeeeeee", "alias": "链", "kind": "video"}], None)
    r = store.compile_refs(reg, ["a_dddddddddddd", {"asset": "a_eeeeeeeeeeee", "use": "运动"}])
    assert r["ok"] and [b["token"] for b in r["blocks"]] == ["<Picture 1>", "<Video 1>"]
    assert r["blocks"][1]["use"] == "运动"


# ---- nodes 接线（源码级，执行需 Comfy） ----

def _nodes_src():
    return open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()


def test_nodes_validation_kernel():
    src = _nodes_src()
    assert "compile_refs(_reg, _keys, i + 1)" in src
    assert "_asset_registry()" in src and "build_execution_registry" in src
    # 旧报错文案逐字保留（旧链行为兼容）
    assert "引用了未知素材标签" in src and "可用标签 {pool_labels}" in src
    assert "超过官方单段上限" in src and "请在段卡片少勾几个" in src
    assert "提示词引用了未知素材标签" in src  # _apply_label_tokens 兜底仍在


def test_nodes_store_loading():
    src = _nodes_src()
    for sym in ["_decode_image_file", "_decode_video_file", "_decode_audio_file"]:
        assert f"def {sym}(" in src, sym
    assert "resolve_absolute" in src and "_store_hits" in src
    assert "全局库×" in src and "def _store_short(" in src
    assert "_load_anchor_image_file(pool_file_of[_head_lib], _head_lib)" in src
    # P4d：画布回退分支已删除（引用集只走 store + 旧文件名路径）


def test_nodes_deprecated_autogrow():
    src = _nodes_src()
    # P4c/P4d：参考拉线与画布媒体/提示词入口已从 schema 删除（注释保留说明）
    assert "P4d" in src
    for name in ["参考图片组", "参考视频组", "参考视频音轨组", "参考音频组"]:
        assert ('Autogrow.Input("%s"' % name) not in src, name
    for name in ["首帧图片", "尾帧图片", "每段尾帧锚定"]:
        assert ('Input("%s"' % name) not in src, name
    assert 'Autogrow.Input("提示词组"' not in src


def test_nodes_transcode_drain():
    src = _nodes_src()
    assert "_tq.claim(proj_default or None)" in src
    assert "后台队列排空" in src
    assert "队列任务完成" in src and "队列任务失败" in src
