"""`projects.delete_segment` 回归：「删除此段」必须连存档一起删并把其后各段前移。

用户反馈（2026-11-08）：删掉一段提示词后「删不干净」—— 分段成片还在、段卡上的
视频张冠李戴。根因是只删了前端提示词数组：manifest 的 seeds/videos/thumbs/
prompt_hashes/seg_res 全按**槽位对齐**，段 mp4/缩略图/latent 切片也按槽位编号
命名，不搬就等于把每一段的内容整体错位一格。

钉这几件事：
  1) 被删槽位的产物（段 latent / 段 mp4 / 缩略图 / 尾帧锚 / 一采另存 /
     latent 切片）全部消失；
  2) 其后各段的产物**文件名整体减一**（seg_003.mp4 → seg_002.mp4）；
  3) manifest 的槽位列表同步前移，且 videos/thumbs/base_videos 里的号跟着改小
     （不改号 = 段卡指向不存在的文件）；
  4) 新接缝的 seams/bridge_scores/seam_metrics 置空；二采记录前移并重算 files；
     重摇队列丢弃已删槽位、其后减一；
  5) 自动合成的成片被删除并清空记录（内容已缺一段）；
  6) done/total 各减一；序章槽与越界段拒绝删除。

无 ComfyUI 可跑：folder_paths 用 tmp 桩，projects.py 的相对导入改绝对后按文件装载。
"""
import json
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3delseg_")

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


def _touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _mk_project(root, name="proj", segs=4, prologue=False):
    """造一个"跑了 4 段"的项目：每段都有 latent/段 mp4/缩略图/尾帧锚/一采另存/
    latent 切片，另有一份自动成片。"""
    d = os.path.join(root, name)
    for sub in ("finals", "latent", "assets"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    off = 1 if prologue else 0
    mf = {
        "revision": 5, "manifest_schema": "h3seamless/manifest-v2.2",
        "has_prologue": prologue, "done": segs + off, "total": segs + off,
        "prompts": (["「序章（上传视频）」"] if prologue else []) + [f"p{i}" for i in range(segs)],
        "params": {"width": 1376, "height": 768},
        "seeds": [100 + i for i in range(segs + off)],
        "prompt_hashes": [f"h{i}" for i in range(segs + off)],
        "videos": [f"finals/seg_{i:03d}.mp4" for i in range(segs + off)],
        "thumbs": [f"finals/thumb_{i:03d}.png" for i in range(segs + off)],
        "base_videos": [f"finals/base_seg_{i:03d}.mp4" for i in range(segs + off)],
        "seg_res": [[1376, 768]] * (segs + off),
        "seams": [[0.02, -1.0]] * (segs + off),
        "bridge_scores": [0.9] * (segs + off),
        "seam_metrics": [{"flow_z": 0.1}] * (segs + off),
        "trims": [0] * (segs + off),
        "upscale": {"segs": [{"done": True, "hash": f"u{i}",
                              "files": {"mp4": f"finals/seg_{i:03d}.mp4"}}
                             for i in range(segs + off)]},
        "redo_queue": [[2, "双锚"], [3, "双锚"]] if segs + off > 3 else [],
        "latents": [
            {"file": f"latent/seg{i:03d}_auto.pt", "src": f"seg_{i:03d}.pt", "kind": "av",
             "sheet": f"latent/seg{i:03d}_auto.sheet.png"}
            for i in range(1, segs + off)
        ] + [{"file": "latent/mine.pt", "src": "assets/clip.mp4", "kind": "av"}],
        "finals": ["finals/final_20260101_000000.mp4"],
        "merges": [{"file": "finals/merged_x.mp4"}],
    }
    for i in range(segs + off):
        _touch(os.path.join(d, "seg_%03d.pt" % i))
        _touch(os.path.join(d, "finals", "seg_%03d.mp4" % i))
        _touch(os.path.join(d, "finals", "thumb_%03d.png" % i))
        _touch(os.path.join(d, "finals", "uplast_%03d.png" % i))
        _touch(os.path.join(d, "finals", "base_seg_%03d.mp4" % i))
    for i in range(1, segs + off):
        _touch(os.path.join(d, "latent", "seg%03d_auto.pt" % i))
        _touch(os.path.join(d, "latent", "seg%03d_auto.sheet.png" % i))
    _touch(os.path.join(d, "latent", "mine.pt"))
    _touch(os.path.join(d, "finals", "final_20260101_000000.mp4"))
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(mf, f, ensure_ascii=False)
    return d


def _exists(d, rel):
    return os.path.isfile(os.path.join(d, *rel.split("/")))


def test_delete_middle_segment_shifts_everything(P, tmp_path=None):
    root = os.path.join(TMP, "h3_projects")
    os.makedirs(root, exist_ok=True)
    d = _mk_project(root, "mid")

    mf, info = P.delete_segment("mid", 1)
    assert info["slot"] == 1
    assert info["dropped_finals"] == 1
    assert (mf["done"], mf["total"]) == (3, 3), "done/total 各减一"

    # ①② 被删段产物消失，其后各段文件名减一（4 段删中间 → 尾巴少一格）
    assert not _exists(d, "seg_003.pt"), "原第 4 段应前移成 seg_002，不该留在原位"
    assert _exists(d, "seg_000.pt") and _exists(d, "seg_001.pt") and _exists(d, "seg_002.pt")
    assert not _exists(d, "finals/seg_003.mp4") and _exists(d, "finals/seg_002.mp4")
    assert not _exists(d, "finals/uplast_003.png") and _exists(d, "finals/uplast_002.png")
    assert not _exists(d, "finals/base_seg_003.mp4") and _exists(d, "finals/base_seg_002.mp4")
    assert not _exists(d, "latent/seg003_auto.pt"), "原 seg003_auto 应前移成 seg002_auto"
    assert _exists(d, "latent/seg001_auto.pt"), "原 seg002_auto 应前移成 seg001_auto"
    assert _exists(d, "latent/mine.pt"), "非段来源的 latent 文件不许误删"

    # ③ 槽位列表前移 + 名字里的号改小
    assert mf["seeds"] == [100, 102, 103], mf["seeds"]
    assert mf["prompt_hashes"] == ["h0", "h2", "h3"], mf["prompt_hashes"]
    assert mf["videos"] == ["finals/seg_000.mp4", "finals/seg_001.mp4",
                            "finals/seg_002.mp4"], mf["videos"]
    assert mf["thumbs"][1] == "finals/thumb_001.png"
    assert mf["base_videos"][1] == "finals/base_seg_001.mp4"
    assert len(mf["seg_res"]) == 3 and len(mf["trims"]) == 3

    # ④ 新接缝清空
    assert mf["seams"][1] is None and mf["bridge_scores"][1] is None
    assert mf["seam_metrics"][1] is None
    # ⑤ 二采记录前移 + files 重算
    assert [x["hash"] for x in mf["upscale"]["segs"]] == ["u0", "u2", "u3"]
    assert mf["upscale"]["segs"][1]["files"]["mp4"] == "finals/seg_001.mp4"
    # 重摇队列：丢弃已删槽位、其后减一
    assert mf["redo_queue"] == [[1, "双锚"], [2, "双锚"]]
    # latent 清单：被删段的切片消失，其后改名，非段来源的条目原样保留
    assert [x["file"] for x in mf["latents"]] == [
        "latent/seg001_auto.pt", "latent/seg002_auto.pt", "latent/mine.pt"]
    assert mf["latents"][0]["src"] == "seg_001.pt"
    assert mf["latents"][0]["sheet"] == "latent/seg001_auto.sheet.png"

    # ⑥ 自动成片删除 + 记录清空；merges（用户显式导出）不动
    assert not _exists(d, "finals/final_20260101_000000.mp4")
    assert mf["finals"] == []
    assert mf["merges"] == [{"file": "finals/merged_x.mp4"}]

    # ⑦ 下一次运行能直接续上（存档连续、各列表同长）——"自动顺延"的判据
    ck = sys.modules["checkpoint"]
    assert ck.contiguous_done(d, mf["done"]) == mf["done"] == 3
    for key in ("seeds", "prompt_hashes", "videos", "thumbs", "seg_res"):
        assert len(mf[key]) == mf["done"], f"{key} 与 done 不同长（下一跑会错位）"


def test_delete_last_segment(P):
    root = os.path.join(TMP, "h3_projects")
    d = _mk_project(root, "last")
    mf, _info = P.delete_segment("last", 3)
    assert (mf["done"], mf["total"]) == (3, 3)
    assert len(mf["videos"]) == 3
    assert not _exists(d, "seg_003.pt")
    assert _exists(d, "seg_002.pt"), "删最后一段不该动前面的文件"


def test_delete_undeleted_segment_only_shrinks_lists(P):
    """槽位 >= done（还没生成）：没有产物可搬，只前移列表与计数。"""
    root = os.path.join(TMP, "h3_projects")
    d = _mk_project(root, "todo")
    mf = json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8"))
    mf["done"] = 2                       # 只跑了 2 段，第 3/4 段还没生成
    mf["finals"] = []
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(mf, f, ensure_ascii=False)
    out, info = P.delete_segment("todo", 3)
    assert info["done"] == 2, "删未生成的段不该倒退进度"
    assert out["total"] == 3


def test_delete_the_only_segment_wipes_the_chain(P):
    """只剩一段：删完链要归零（done/total=0、列表全空、文件全清）——
    前端随后补一条崭新空段，面板上不会出现"0 段"的空窗口。"""
    root = os.path.join(TMP, "h3_projects")
    d = _mk_project(root, "solo", segs=1)
    mf, info = P.delete_segment("solo", 0)
    assert (mf["done"], mf["total"]) == (0, 0), (mf["done"], mf["total"])
    for key in ("seeds", "prompt_hashes", "videos", "thumbs", "base_videos",
                "seg_res", "trims", "seams", "bridge_scores", "seam_metrics"):
        assert mf.get(key) == [], f"{key} 没清干净：{mf.get(key)}"
    assert mf["upscale"]["segs"] == []
    assert mf["latents"] == [{"file": "latent/mine.pt", "src": "assets/clip.mp4",
                              "kind": "av"}], "只该剩下与段无关的 latent"
    assert not _exists(d, "seg_000.pt") and not _exists(d, "finals/seg_000.mp4")
    assert not _exists(d, "finals/thumb_000.png")
    assert not _exists(d, "latent/seg000_auto.pt")


def test_delete_only_prompt_segment_keeps_prologue(P):
    """有序章：删掉唯一一条提示词段后，序章槽（0）与它的记录必须原样留着。"""
    root = os.path.join(TMP, "h3_projects")
    d = _mk_project(root, "solopro", segs=1, prologue=True)
    mf, _info = P.delete_segment("solopro", 1)
    assert (mf["done"], mf["total"]) == (1, 1), "序章本身仍是一段已完成"
    for key in ("seeds", "prompt_hashes", "videos", "thumbs", "seg_res"):
        assert len(mf[key]) == 1, f"{key} 应只剩序章槽"
    assert not _exists(d, "seg_001.pt") and _exists(d, "seg_000.pt"), \
        "序章的 seg_000.pt 不许动"
    assert [x["file"] for x in mf["latents"]] == ["latent/mine.pt"], \
        "被删段的 latent 切片要一起没"


def test_delete_slot_beyond_total_is_a_noop_cleanup(P):
    """槽位 > total（前端刚加段、1.5s 防抖还没落盘）：不许报"越界"把删除卡死。"""
    root = os.path.join(TMP, "h3_projects")
    d = _mk_project(root, "stale", segs=1)
    mf = json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8"))
    mf["total"] = 1                      # 前端其实已经有 3 段，manifest 落后
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(mf, f, ensure_ascii=False)
    out, info = P.delete_segment("stale", 2)   # 删"第 3 段"
    assert info["slot"] == 2
    assert out["total"] == 1, "total 之外的槽不压 total（save_prompts 会重算）"
    assert out["done"] == 1


def test_prologue_slot_is_protected(P):
    root = os.path.join(TMP, "h3_projects")
    _mk_project(root, "pro", prologue=True)
    with pytest.raises(ValueError):
        P.delete_segment("pro", 0)            # 序章槽：只能清「起始视频」重跑
    with pytest.raises(ValueError):
        P.delete_segment("pro", -1)
    with pytest.raises(ValueError):
        P.delete_segment("no_such_project", 1)
    # 越界槽位不报错（防把"前端刚加、还没落盘"的段卡死），只空转
    mf, info = P.delete_segment("pro", 99)
    assert info["slot"] == 99
    assert (mf["done"], mf["total"]) == (5, 5), "空转不许动 done/total"
    assert mf["finals"], "越界空转不该顺手把自动成片删掉"
    assert info["dropped_finals"] == 0


def test_drop_finals_can_be_skipped(P):
    root = os.path.join(TMP, "h3_projects")
    d = _mk_project(root, "keepfinal")
    mf, info = P.delete_segment("keepfinal", 1, drop_finals=False)
    assert info["dropped_finals"] == 0
    assert _exists(d, "finals/final_20260101_000000.mp4")
    assert mf["finals"] == ["finals/final_20260101_000000.mp4"]


def test_route_registered_in_both_places():
    """新接口必须同时挂进 ROUTES 声明与 handler 映射（否则探测不到或 404）。"""
    src = open(os.path.join(ROOT, "routes.py"), encoding="utf-8").read()
    assert src.count('"/h3chain/delete_segment"') == 2, \
        "ROUTES 与 handler 映射各要登记一次 /h3chain/delete_segment"
