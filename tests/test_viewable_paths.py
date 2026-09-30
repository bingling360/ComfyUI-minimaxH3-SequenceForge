"""projects.with_viewable_paths 回归：UI 出口的旧格式裸名媒体路径归一化。

跑法（仓库根目录）：
    python -m pytest tests/test_viewable_paths.py -q

背景（2026-09-30 修）：四库迁移后新文件一律写 finals/，但旧格式 manifest 里
的媒体仍是裸名（seg_000.mp4）——「不改写、读写走双路径兼容」口径下，后端
resolve_project_file 都能兜底，唯独前端拼的 /api/view URL 按字面路径找：
裸名 + 文件在 finals/ 必 404，表现为 9:16 旧项目预览黑屏、下载「文件不存在」。
修复 = /h3chain/project 与 /h3chain/projects 出口经 with_viewable_paths 把
「根目录没有、finals//assets/ 有」的裸名换成真实位置；manifest 本体零改动。

钉六件事：
  1) 裸名 + 文件在 finals/ → 出口换成 finals/ 前缀（videos/thumbs/finals/merges）；
  2) 裸名 + 文件在根目录 → 保持裸名（本来就打得开，别画蛇添足）；
  3) 裸名 + 哪都没有 → 原样保留（不发明路径）；
  4) 已带前缀（finals/xxx）→ 原样透传；
  5) 非媒体/无扩展名/未知扩展名的字符串一律不动（.pt latent、ref 引用等）；
  6) manifest 本体零改动（出口归一化，不是写回）。
"""

import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3viewable_")

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
def P():
    return _load_top("projects", os.path.join(ROOT, "projects.py"),
                     [("from . import checkpoint", "import checkpoint")])


@pytest.fixture(scope="module")
def projdir():
    """一个混装项目：finals/ 里有真文件，根目录有一个旧文件，manifest 记裸名。"""
    d = os.path.join(TMP, "h3_projects", "vp1")
    os.makedirs(os.path.join(d, "finals"), exist_ok=True)
    for fn in ("finals/seg_000.mp4", "finals/thumb_000.png",
               "finals/final_916.mp4", "finals/merged_0.mp4",
               "seg_002.mp4", "assets/pic.jpg"):
        path = os.path.join(d, fn.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x")
    return d


MANIFEST = {
    "videos": ["seg_000.mp4", "finals/seg_001.mp4", "seg_002.mp4", "seg_003.mp4"],
    "thumbs": ["finals/thumb_000.png"],
    "finals": ["final_916.mp4"],
    "merges": [{"file": "merged_0.mp4", "items": [{"seg": 0}]}, {"file": "gone.mp4"}],
    "seams": ["seam_0.mp4", "not_a_media_ref", "lat.pt"],
    "prompts": ["seg_003 这段还没生成"],
}


def test_finals_hit_gets_prefixed(P, projdir):
    out = P.with_viewable_paths("vp1", MANIFEST)
    assert out["videos"][0] == "finals/seg_000.mp4"
    assert out["finals"] == ["finals/final_916.mp4"]
    assert out["merges"][0]["file"] == "finals/merged_0.mp4"


def test_root_hit_stays_bare(P, projdir):
    out = P.with_viewable_paths("vp1", MANIFEST)
    # 根目录真有 seg_002.mp4：裸名本来就打得开，不画蛇添足
    assert out["videos"][2] == "seg_002.mp4"


def test_missing_stays_bare(P, projdir):
    out = P.with_viewable_paths("vp1", MANIFEST)
    # seg_003 没生成、merged gone.mp4 不存在：不发明路径
    assert out["videos"][3] == "seg_003.mp4"
    assert out["merges"][1]["file"] == "gone.mp4"
    # seam_0.mp4 / .pt / 纯文本：非命中项原样
    assert out["seams"][0] == "seam_0.mp4"
    assert out["seams"][1] == "not_a_media_ref"
    assert out["seams"][2] == "lat.pt"


def test_prefixed_passthrough(P, projdir):
    out = P.with_viewable_paths("vp1", MANIFEST)
    assert out["videos"][1] == "finals/seg_001.mp4"
    assert out["thumbs"] == ["finals/thumb_000.png"]


def test_assets_fallback(P, projdir):
    m = {"videos": ["pic.jpg"]}
    out = P.with_viewable_paths("vp1", m)
    assert out["videos"] == ["assets/pic.jpg"]


def test_manifest_untouched(P, projdir):
    P.with_viewable_paths("vp1", MANIFEST)
    assert MANIFEST["videos"][0] == "seg_000.mp4"
    assert MANIFEST["finals"] == ["final_916.mp4"]
    assert MANIFEST["merges"][0]["file"] == "merged_0.mp4"


def test_bad_name_passthrough(P, projdir):
    m = {"videos": ["seg_000.mp4"]}
    assert P.with_viewable_paths("../evil", m) is m
    assert P.with_viewable_paths("", m) is m


def test_list_projects_normalizes(P, projdir):
    import json
    with open(os.path.join(projdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"title": "vp1", "done": 3, "total": 4,
                   "finals": ["final_916.mp4"],
                   "merges": [{"file": "merged_0.mp4"}]}, f, ensure_ascii=False)
    rows = {r["dir"]: r for r in P.list_projects()}
    assert rows["vp1"]["finals"] == ["finals/final_916.mp4"]
    assert rows["vp1"]["merges"] == ["finals/merged_0.mp4"]
