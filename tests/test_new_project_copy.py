"""projects.create_project(copy_from=...) 回归：新建项目 = 空白，或整项目复制。

钉三件事：
  1) 不传 copy_from → 空白项目（0 段、无素材、assets/ 里没有文件）；
  2) 传 copy_from → 只继承内容键（提示词 / 分段字段 / 素材清单 / 资产链接 / 共享参数 /
     二采设置），且**被引用到的文件真被复制**（assets/ 的图 + 段级锚用到的 latent）——
     段级 frame_img 存的是项目相对路径 assets/xxx.png，只带清单不带文件的话，
     新项目挂的就是指向空气的引用，看着有锚、跑起来找不到；
  3) 产物与进度不跟过来（成片/合并/latent 清单/切片清单、done/种子/指纹/缩略图/
     分段视频/二采段记录/重跑队列），否则新 manifest 会指向新项目里不存在的文件。

无 ComfyUI 可跑：folder_paths 用 tmp 桩，projects.py 的相对导入改绝对后按文件装载。
"""
import json
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3newproj_")

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
    fp.get_annotated_filepath = lambda f: os.path.join(TMP, f)
    sys.modules["folder_paths"] = fp
    yield


@pytest.fixture(scope="module")
def projroot():
    root = os.path.join(TMP, "h3_projects")
    os.makedirs(root, exist_ok=True)
    return root


@pytest.fixture(scope="module")
def P():
    return _load_top("projects", os.path.join(ROOT, "projects.py"),
                     [("from . import checkpoint", "import checkpoint")])


def _write_src(root, name="src"):
    """造一个"跑过两段"的源项目：有提示词、有素材文件、有产物清单与进度。"""
    src = os.path.join(root, name)
    for sub in ("assets", "finals", "latent"):
        os.makedirs(os.path.join(src, sub), exist_ok=True)
    with open(os.path.join(src, "assets", "girl.png"), "wb") as f:
        f.write(b"img-bytes")
    with open(os.path.join(src, "finals", "seg_000.mp4"), "wb") as f:
        f.write(b"mp4")
    # 段级锚引用的项目内 latent：复制时必须连文件一起带走
    with open(os.path.join(src, "latent", "kf.pt"), "wb") as f:
        f.write(b"latent-bytes")
    with open(os.path.join(src, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({
            "revision": 7, "manifest_schema": "h3/manifest-v2",
            "done": 2, "total": 3, "title": "src",
            "created_at": 1.0, "updated_at": 2.0,
            "prompts": ["integrated_multimodal_description: 主体 A",
                        "integrated_multimodal_description: 主体 B"],
            "seg_fields": [
                {"seconds": 9, "frame_img": {"first": "assets/girl.png"}},
                {"seconds": 8, "tail_src": {"latent": "latent/kf.pt"},
                 "latent_ref": {"on": True, "frames": 3,
                                "src": {"file": "latent/kf.pt"}}},
            ],
            "assets": [{"label": "女主", "kind": "image", "file": "assets/girl.png",
                        "ref_name": "girl.png"}],
            "asset_links": [{"asset_id": "a_0123456789ab", "alias": "参考图",
                             "kind": "image"}],
            "params": {"width": 1280, "height": 704},
            "upscale": {"include": [0], "scale": 2.0, "segs": [{"slot": 0}]},
            "finals": [{"file": "seg_000.mp4"}], "merges": [{"file": "merged.mp4"}],
            "latents": [{"file": "latent/kf.pt"}], "clips": [{"file": "assets/clip.mp4"}],
            "seeds": [11, 22], "prompt_hashes": ["h1", "h2"], "redo_queue": [[1, "双锚"]],
            "videos": ["seg_000.mp4"], "thumbs": ["thumb_000.png"],
        }, f, ensure_ascii=False)
    return src


def test_blank_project_has_nothing(P, projroot):
    mf = P.create_project("blank")
    assert mf is not None
    assert mf["prompts"] == []
    assert (mf["done"], mf["total"], mf["revision"]) == (0, 0, 1)
    assert mf["title"] == "blank"
    assert not os.path.exists(os.path.join(projroot, "blank", "assets", "girl.png"))


def test_copy_project_inherits_content_and_files(P, projroot):
    _write_src(projroot)
    mf = P.create_project("copy1", "src")
    assert mf["prompts"] == ["integrated_multimodal_description: 主体 A",
                             "integrated_multimodal_description: 主体 B"]
    assert mf["seg_fields"][0]["frame_img"]["first"] == "assets/girl.png"
    assert mf["assets"][0]["file"] == "assets/girl.png"
    assert mf["asset_links"][0]["asset_id"] == "a_0123456789ab"
    assert mf["params"] == {"width": 1280, "height": 704}
    assert (mf["done"], mf["total"]) == (0, 3), "内容 3 段照带，进度归零"
    assert (mf["title"], mf["revision"]) == ("copy1", 1)
    # 被引用到的文件必须真的在（否则 frame_img / latent 锚全是空指向）
    copied = os.path.join(projroot, "copy1", "assets", "girl.png")
    assert os.path.isfile(copied), "素材文件要跟着走，否则 assets/ 路径没有落点"
    with open(copied, "rb") as f:
        assert f.read() == b"img-bytes"
    kf = os.path.join(projroot, "copy1", "latent", "kf.pt")
    assert os.path.isfile(kf), "段级 latent 锚引用的文件也要跟着走"
    with open(kf, "rb") as f:
        assert f.read() == b"latent-bytes"


def test_copy_project_drops_products_and_progress(P, projroot):
    _write_src(projroot)
    mf = P.create_project("copy2", "src")
    for k in ("finals", "merges", "latents", "clips"):
        assert not mf.get(k), "%s 是产物清单，不该带过来：%r" % (k, mf.get(k))
    assert mf["done"] == 0
    for k in ("seeds", "prompt_hashes", "redo_queue", "videos", "thumbs"):
        assert not mf.get(k), "%s 属于上一条链的进度，应清空：%r" % (k, mf.get(k))
    assert not (mf.get("upscale") or {}).get("segs"), "二采段记录不该带过来"
    assert (mf.get("upscale") or {}).get("scale") == 2.0, "二采设置属于内容，应保留"


def test_copy_from_missing_source_makes_blank(P, projroot):
    mf = P.create_project("copy3", "nope")
    assert mf["prompts"] == []
    assert mf["done"] == 0
