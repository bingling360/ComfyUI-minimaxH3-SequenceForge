"""refvideo.align_target —— 「项目级对齐参考视频时间」的口径单测（无 ComfyUI 可跑）。

口径（2026-10-18 用户拍板）：**官方怎么喂，就按多久对齐**。
  · 源帧原样按 24fps 播（30fps 素材放慢 25%）→ 向下砍 17k+5；
  · **不改任何设置**，只算「模型看到的参考有多长」，段时长照这个写；
  · 时间轴那四项开关（只缩不放/帧率归一/网格方向/时长上限）已经整个删除。

这里钉住三件事：

  ① 等式：段侧 `_snap_seconds(seconds)` == frames，且 `plan(cfg, frames)` 的
     out_frames == frames（渲染期真的走这一路，这是唯一有意义的验证）
  ② 口径：126 帧 @30fps **不许**变成 107 帧 4.46s ——
     那是旧版"就近吸附 + 补末帧"的结果，用户明确否掉
  ③ 只读：DEFAULT_REFVID 与传入 cfg 一个字节都不许改

跑法（仓库根目录）：
    python -m pytest tests/test_refvideo_align.py -q
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import refvideo as rv  # noqa: E402


def _info(fps=30.0, frames=None, dur=None, w=1280, h=720):
    dur = float(dur if dur is not None else 5.0)
    if frames is None:
        frames = int(round(dur * fps))
    return {"fps": fps, "frames": frames, "duration_s": dur,
            "width": w, "height": h, "has_audio": True}


def _snap_seconds(seconds):
    """后端 nodes._snap_seconds 的同公式（秒 -> 就近 17k+5 帧）。"""
    f = max(5, int(round(float(seconds) * 24)))
    k = max(0, round((f - 5) / 17))
    return 17 * k + 5


# ---------------------------------------------------------------- ① 默认口径

def test_no_timeline_knobs_are_left():
    """时间轴四项已连根删除：既不暴露、也不是契约键。"""
    for k in ("no_upscale", "fps_norm", "grid", "limit", "limit_s"):
        assert k not in rv.DEFAULT_REFVID, k
    assert not hasattr(rv, "GRIDS") and not hasattr(rv, "LIMITS")


def test_user_case_126_frames_must_not_get_padded():
    """用户那条 1280×720 @30fps 4.20s（126 帧）：

    官方口径 = 126 帧按 24fps 播（5.25s）→ 向下吸附 124 帧 = 5.17s。
    **绝对不许**变成 107 帧 / 4.46s（旧版"就近吸附 + 补 7 帧末帧"）。
    """
    t = rv.align_target(_info(fps=30.0, frames=126, dur=4.20), None, "v1")
    assert t["frames"] == 124
    assert t["seconds"] == pytest.approx(124 / 24)
    assert t["frames"] != 107 and t["seconds"] != pytest.approx(4.46, abs=0.01)
    assert (t["frames"] - 5) % 17 == 0
    assert t["report"] and "5.17s" in t["report"][0]


@pytest.mark.parametrize("fps,frames,dur,want", [
    (30.0, 126, 4.20, 124),    # 官方：不去补帧
    (24.0, 240, 10.0, 226),    # 向下砍 14 帧
    (30.0, 300, 10.0, 294),    # 官方：300 帧按 24fps = 12.5s → 向下 294
    (24.0, 192, 8.0, 192),     # 正落在网格上，一刀不砍
    (24.0, 100, 4.17, 90),     # 小素材也照样向下
])
def test_official_presentation_length(fps, frames, dur, want):
    t = rv.align_target(_info(fps=fps, frames=frames, dur=dur))
    assert t["frames"] == want, t["report"][0]


# ---------------------------------------------------------------- ② 等式

@pytest.mark.parametrize("fps,dur", [
    (24.0, 5.0), (24.0, 10.0), (30.0, 10.0), (25.0, 8.0),
    (29.97, 10.01), (60.0, 4.0), (12.0, 6.0), (48.0, 15.0), (24.0, 3.0),
])
def test_both_ends_land_on_the_same_grid_point(fps, dur):
    """段侧（秒->帧）与素材侧（plan）必须落在同一个帧数上。"""
    info = _info(fps=fps, frames=int(round(dur * fps)), dur=dur)
    t = rv.align_target(info)
    assert _snap_seconds(t["seconds"]) == t["frames"], \
        f"{fps}fps {dur}s：段吸附后 {_snap_seconds(t['seconds'])} != 目标 {t['frames']}"
    p = rv.plan(info, t["cfg"], t["frames"], "v1")
    assert p["out_frames"] == t["frames"], \
        f"{fps}fps {dur}s：渲染期实际 {p['out_frames']} 帧 != 目标 {t['frames']}"


# ---------------------------------------------------------------- ③ 只读

def test_never_mutates_settings():
    before = dict(rv.DEFAULT_REFVID)
    cfg = dict(rv.DEFAULT_REFVID, on=True, fit="补黑边", audio=False)
    snapshot = dict(cfg)
    t = rv.align_target(_info(), cfg)
    assert rv.DEFAULT_REFVID == before, "DEFAULT_REFVID 不许被改"
    assert cfg == snapshot, "传入的 cfg 不许被改（对齐只写段时长）"
    assert t["cfg"] == cfg, "回显的口径必须就是传进去那份"


# ---------------------------------------------------------------- ④ 硬边界

def test_rejects_too_short_material():
    with pytest.raises(ValueError) as e:
        rv.align_target(_info(fps=24.0, frames=3, dur=0.125))
    assert "5 帧" in str(e.value)


def test_caps_at_trained_max():
    """20s @24fps：362 帧（15.08s）封顶并明说片尾会被截。"""
    t = rv.align_target(_info(fps=24.0, frames=480, dur=20.0))
    assert t["frames"] == rv.TRAINED_MAX_FRAMES == 362
    assert t["seconds"] == pytest.approx(15.0), "362 帧要夹到段卡上限 15s（吸附回来仍是 362）"
    assert _snap_seconds(t["seconds"]) == 362
    assert any("训练上限" in n for n in t["notes"])


def test_idempotent():
    """同一素材算两遍结果一致（按钮可以反复点）。"""
    a = rv.align_target(_info(fps=30.0, frames=126, dur=4.20))
    b = rv.align_target(_info(fps=30.0, frames=126, dur=4.20))
    assert a["frames"] == b["frames"] and a["seconds"] == b["seconds"]
