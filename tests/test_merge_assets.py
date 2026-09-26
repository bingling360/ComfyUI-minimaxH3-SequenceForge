"""合并清单的 `{"asset": id}` 来源解析（R7 合并模式重构的后端半边）。

背景：合并清单从"导演台里勾分段"改成"素材库里点素材"之后，清单项不再只有
`{"seg": n}` / `{"file": f}` —— 素材库给的是**素材 id**（`<scope>:<file>`）。
全局库素材的文件在 `user/minimax_h3/library/` 下，既不在项目目录也不在 input，
走 file 分支必然报"文件不存在"，所以必须有一条按 id 解析的分支。

钉这几件事：
  1) global / project / finals 三种 scope 的素材 id 都能解析出真实路径；
  2) **顺序即数据**：sources 的顺序与清单一致（不许 sort）；
  3) 非视频（图片/音频/latent）提前拒绝，消息能直接给人看；
  4) id 不存在 / 目录穿越（`..`）一律拒绝，不会解析到项目外；
  5) 老形态 `{"seg": n}` / `{"file": f}` 行为不变（`asset` 为空串要回落 file 分支）。

无 ComfyUI / 无 PyAV 可跑：只测 `_merge_sources` 纯函数，不触发编码。
"""
import json
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3mergeasset_")
PROJ = "projA"

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_top(name, path, patches=None):
    src = open(path, encoding="utf-8").read()
    for a, b in (patches or []):
        src = src.replace(a, b)
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    sys.modules[name] = mod
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


@pytest.fixture(scope="module", autouse=True)
def _env():
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = lambda: TMP
    fp.get_input_directory = lambda: TMP
    fp.get_user_directory = lambda: TMP
    fp.get_annotated_filepath = lambda f: os.path.join(TMP, f)
    sys.modules["folder_paths"] = fp
    yield


@pytest.fixture(scope="module")
def h3lib():
    return _load_top("library", os.path.join(ROOT, "library.py"))


@pytest.fixture(scope="module")
def P(h3lib):
    return _load_top("projects", os.path.join(ROOT, "projects.py"),
                     [("from . import checkpoint", "import checkpoint")])


def _touch(path: str, data: bytes = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return os.path.realpath(path)


@pytest.fixture(scope="module")
def world(h3lib):
    """造一个项目 + 一个全局库，各自放好视频/图片。"""
    root = os.path.join(TMP, "h3_projects", PROJ)
    v_finals = _touch(os.path.join(root, "finals", "merged_a.mp4"))
    v_assets = _touch(os.path.join(root, "assets", "p1.mp4"))
    v_seg = _touch(os.path.join(root, "videos", "seg_001.mp4"))
    # 分段视频的**规范落点**是 finals/（checkpoint.resolve_project_file 只找
    # finals/ 与 assets/，不找 videos/）——老 seg 形态要用这个位置。
    v_seg_legacy = _touch(os.path.join(root, "finals", "seg_002.mp4"))
    img_assets = _touch(os.path.join(root, "assets", "img.png"))
    audio_assets = _touch(os.path.join(root, "assets", "voice.wav"))

    lroot = os.path.join(TMP, "minimax_h3", "library")
    g_video = _touch(os.path.join(lroot, "video", "g1.mp4"))
    g_img = _touch(os.path.join(lroot, "image", "g2.png"))
    with open(os.path.join(lroot, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "assets": [
            {"file": "video/g1.mp4", "kind": "video", "orig_name": "g1.mp4",
             "asset_id": "as_1", "bytes": 1, "tags": [], "created_at": 0},
            {"file": "image/g2.png", "kind": "image", "orig_name": "g2.png",
             "asset_id": "as_2", "bytes": 1, "tags": [], "created_at": 0},
        ]}, f)
    h3lib.invalidate()
    return {
        "root": root, "lroot": lroot,
        "v_finals": v_finals, "v_assets": v_assets, "v_seg": v_seg,
        "v_seg_legacy": v_seg_legacy,
        "img_assets": img_assets, "audio_assets": audio_assets,
        "g_video": g_video, "g_img": g_img,
    }


def _src(P, world, items, videos=()):
    return P._merge_sources(world["root"], {"videos": list(videos)}, items)


# ---------- 1) 三种 scope 的素材 id ----------

def test_global_asset_resolves(P, world):
    """全局库素材：file 分支一定找不到（不在项目目录也不在 input），
    asset 分支必须能解析到库内真实路径。"""
    got = _src(P, world, [{"asset": "global:video/g1.mp4", "name": "g1.mp4"}])
    assert got == [world["g_video"]]


def test_project_and_finals_assets_resolve(P, world):
    got = _src(P, world, [
        {"asset": "project:assets/p1.mp4"},
        {"asset": "finals:finals/merged_a.mp4"},
        {"asset": "finals:videos/seg_001.mp4"},
    ])
    assert got == [world["v_assets"], world["v_finals"], world["v_seg"]]


# ---------- 2) 顺序即数据 ----------

def test_order_is_preserved_not_sorted(P, world):
    """合并顺序就是用户点出来的顺序：清单里 finals 在前、global 在后，
    结果必须一模一样（按路径排序就会把这个顺序改掉）。"""
    a = {"asset": "finals:finals/merged_a.mp4"}
    b = {"asset": "global:video/g1.mp4"}
    c = {"asset": "project:assets/p1.mp4"}
    assert _src(P, world, [a, b, c]) == [world["v_finals"], world["g_video"], world["v_assets"]]
    assert _src(P, world, [c, b, a]) == [world["v_assets"], world["g_video"], world["v_finals"]]


def test_asset_and_file_mix_in_order(P, world):
    """素材 + 外部上传视频混排：各自解析完仍按清单顺序落位。"""
    got = _src(P, world, [
        {"asset": "global:video/g1.mp4"},
        {"file": "finals/merged_a.mp4"},
        {"asset": "project:assets/p1.mp4"},
    ])
    assert got == [world["g_video"], world["v_finals"], world["v_assets"]]


# ---------- 3) 非视频提前拒绝 ----------

@pytest.mark.parametrize("aid,word", [
    ("project:assets/img.png", "图片"),
    ("project:assets/voice.wav", "音频"),
    ("global:image/g2.png", "图片"),
])
def test_non_video_rejected(P, world, aid, word):
    with pytest.raises(ValueError) as ei:
        _src(P, world, [{"asset": aid}])
    msg = str(ei.value)
    assert "只能合并视频" in msg and word in msg


# ---------- 4) 不存在 / 穿越 ----------

@pytest.mark.parametrize("aid", [
    "global:video/nope.mp4",
    "project:assets/nope.mp4",
    "project:assets/../../secret.mp4",
    "global:../../etc/passwd.mp4",
    "",
])
def test_bad_asset_rejected(P, world, aid):
    items = [{"asset": aid}] if aid else [{"asset": ""}]
    if not aid:
        # 空 asset 要**回落** file 分支（而 file 也缺 → 非法文件名）
        with pytest.raises(ValueError):
            _src(P, world, items)
        return
    with pytest.raises(ValueError) as ei:
        _src(P, world, items)
    assert "素材" in str(ei.value)


def test_asset_path_stays_inside_allowed_roots(P, world):
    """能解析出来的路径必须落在项目目录或全局库内（realpath 复核）。"""
    allowed = [os.path.realpath(world["root"]), os.path.realpath(world["lroot"])]
    got = _src(P, world, [
        {"asset": "global:video/g1.mp4"},
        {"asset": "project:assets/p1.mp4"},
        {"asset": "finals:finals/merged_a.mp4"},
    ])
    for p in got:
        assert any(p == a or p.startswith(a + os.sep) for a in allowed), p


# ---------- 5) 老形态不回归 ----------

def test_legacy_seg_still_works(P, world):
    got = _src(P, world, [{"seg": 1}], videos=["seg_002.mp4"])
    assert got == [world["v_seg_legacy"]]


def test_legacy_file_still_works(P, world):
    got = _src(P, world, [{"file": "finals/merged_a.mp4"}])
    assert got == [world["v_finals"]]


def test_empty_asset_falls_back_to_file(P, world):
    """`asset` 为空串（老前端 / 缺 id）不许直接报错，要回落到 file 分支。"""
    got = _src(P, world, [{"asset": "", "file": "finals/merged_a.mp4"}])
    assert got == [world["v_finals"]]


def test_empty_items_rejected(P, world):
    with pytest.raises(ValueError):
        _src(P, world, [])
