"""refvideo.py —— 参考视频归一的决策单测（无 ComfyUI 可跑）。

refvideo.py 的设计前提就是**零 ComfyUI 依赖**（与 grid.py / perf.py 同级），
所以这里直接 import 真模块，不造任何 torch / comfy 替身。

钉住的是「口径」而不是实现细节：
  · 画布换算与官方 Resolution Selector / nodes._resolve_canvas 逐位一致
  · 17k+5 网格三档方向（向下=官方行为）
  · fps 重定时：源帧号表 = round(i × 源fps ÷ 24)，**输出时长 == 源时长**
  · 音轨时间窗与画面同窗；关闭音频开关时语义交给调用方（plan 只回报）
  · 只缩不放 / 三种适配方式 / 时长上限
  · 指纹只随「改变模型输入」的项变化（开关本身不进）
  · 全局设置读写往返 + 缓存失效

跑法（仓库根目录）：
    python -m pytest tests/test_refvideo.py -q
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import refvideo as rv  # noqa: E402


def _info(fps=30.0, frames=None, dur=None, w=1920, h=1080, audio=True):
    dur = float(dur if dur is not None else 5.0)
    if frames is None:
        frames = int(round(dur * fps))
    return {"fps": fps, "frames": frames, "duration_s": dur,
            "width": w, "height": h, "has_audio": audio}


# ---------------------------------------------------------------- 画布换算

@pytest.mark.parametrize("mp,want", [
    (0.2, (608, 352)), (0.5, (960, 544)), (0.98, (1344, 768)),
    (1.0, (1376, 768)), (1.2, (1504, 832)), (1.5, (1664, 928)),
    (2.0, (1920, 1088)),
])
def test_resolve_canvas_matches_official_table(mp, want):
    """官方 Resolution Selector 档位表（nodes._resolve_canvas docstring 同表）。"""
    assert rv.resolve_canvas("16:9", mp) == want


def test_resolve_canvas_portrait():
    assert rv.resolve_canvas("9:16", 1.0) == (768, 1376)


def test_default_is_official_reference_canvas():
    """出厂默认必须恰好等于官方参考画布（短边 768 / 面积上限 768×1344）。

    这是「把官方隐式行为显式化、一个字节不变」这句话的机器可验证形式：
    16:9 @ 0.98MP → 1344×768，短边 768、面积 = 768×1344 正好顶格。
    """
    w, h = rv.resolve_canvas(rv.DEFAULT_AR, rv.DEFAULT_MP)
    assert (w, h) == (1344, 768)
    assert min(w, h) == rv.BASE_SHORT_EDGE
    assert w * h == rv.MAX_PIXELS


def test_defaults_are_official_and_timeline_knobs_are_gone():
    """出厂只有 5 个键。时间轴那四项（只缩不放 / 帧率归一 / 网格方向 / 时长上限）
    2026-10-18 连根删除，不是隐藏控件 —— 所以它们**不是契约键**。"""
    assert rv.DEFAULT_REFVID["on"] is False
    for k in ("no_upscale", "fps_norm", "grid", "limit", "limit_s"):
        assert k not in rv.DEFAULT_REFVID, k
    assert not hasattr(rv, "GRIDS") and not hasattr(rv, "LIMITS")


# ---------------------------------------------------------------- 网格（只剩向下）

@pytest.mark.parametrize("n,want", [
    (124, 124),     # 5s@24fps 本来就在网格上
    (72, 56),       # 3.00s → 2.33s
    (130, 124),
    (240, 226),
    (126, 124),     # 用户那条 1280×720@30fps 4.20s 的 126 帧
    (5, 5), (4, 5),
])
def test_snap_grid_snaps_down(n, want):
    assert rv.snap_grid(n) == want


def test_snap_grid_matches_official_loop():
    """逐字对齐官方 `while n % 17 != 5: n -= 1`，且永不补帧。"""
    for n in range(5, 400):
        want = n
        while want % 17 != 5:
            want -= 1
        assert rv.snap_grid(n) == want
        assert rv.snap_grid(n) <= n


# ---------------------------------------------------------------- 归一方案（官方单一路径）

def test_plan_keeps_source_frames_then_snaps_down():
    """官方口径：帧原样保留（30fps 素材按 24fps 播 = 放慢 25%）→ 向下砍 17k+5。

    150 帧 @30fps（5.00s）→ 141 帧（5.88s）。
    """
    p = rv.plan(_info(fps=30.0, dur=5.0, frames=150), rv.parse_state({"on": True}), 0, "素材")
    assert p["src_fps"] == 30.0
    assert p["out_frames"] == 141
    assert p["src_idx"] == list(range(141)), "不抽帧、不重定时：源帧号与输出一一对应"
    assert p["src_end"] == 141
    assert abs(p["out_frames"] - 141) == 0 and p["out_frames"] <= 150, "只向下砍，9 帧以内"
    assert p["out_frames"] / rv.FPS > 150 / 30.0, "按 24fps 播 → 比源时长长（官方放慢 25%）"
    assert any("向下对齐 17k+5" in x for x in p["report"])


def test_plan_audio_window_uses_source_seconds():
    """音轨窗按**源**时长（out / src_fps）—— 音轨是真实时间，不跟着 24fps 放慢。"""
    p = rv.plan(_info(fps=30.0, dur=5.0, frames=150), rv.parse_state({"on": True}), 0, "素材")
    assert p["a_start"] == 0.0
    assert p["a_end"] == pytest.approx(p["out_frames"] / 30.0)
    assert abs(p["a_end"] - p["out_frames"] / rv.FPS) > 0.5


def test_plan_low_fps_source_is_not_halved():
    """12fps/6s（72 帧）：闸门 362 帧换算成源秒要除以 **12**，不能把帧砍半。

    这是旧版恒除以 24 留下的真 bug（72 帧被算成只取 28 帧）。
    """
    p = rv.plan(_info(fps=12.0, dur=6.0, frames=72), rv.parse_state({"on": True}), 0, "素材")
    assert p["out_frames"] == 56
    assert p["src_idx"] == list(range(56))


def test_plan_caps_at_longest_segment():
    """截到本链最长段 = 官方 `frames[:frame_count]`（多余的帧根本进不了模型）。"""
    p = rv.plan(_info(fps=24.0, dur=10.0, frames=240), rv.parse_state({"on": True}), 124, "长素材")
    assert p["out_frames"] <= 124
    assert any("最长段" in x for x in p["report"])


def test_plan_guard_bounds_memory_without_segment_info():
    """拿不到段长时按官方训练上限 362 帧兜底（~15s）。"""
    p = rv.plan(_info(fps=24.0, dur=60.0, frames=1440), rv.parse_state({"on": True}), 0, "无段长")
    assert p["out_frames"] <= 362
    assert any("362" in x for x in p["report"])


def test_plan_no_upscale_falls_back_to_source_size():
    """只缩不放（写死）= 官方 `if vw*vh < cw*ch` 那条回退。"""
    p = rv.plan(_info(fps=24.0, dur=2.0, w=640, h=360), rv.parse_state({"on": True}), 124, "小素材")
    assert p["no_upscale_hit"] is True
    assert (p["w"], p["h"]) == (640, 352)
    assert any("只缩不放" in x for x in p["report"])


def test_plan_fit_modes():
    for fit, crop in (("裁切填满", "center"), ("补黑边", "pad"), ("拉伸", "disabled")):
        cfg = rv.parse_state({"on": True, "fit": fit})
        p = rv.plan(_info(fps=24.0, dur=2.0), cfg, 0, "素材")
        assert p["crop"] == crop


def test_plan_audio_flag_is_reported_not_applied():
    """plan 只回报音频口径；真正「注不注入音轨」由调用方按同一个开关决定。"""
    on = rv.plan(_info(), rv.parse_state({"on": True, "audio": True}), 0, "素材")
    off = rv.plan(_info(), rv.parse_state({"on": True, "audio": False}), 0, "素材")
    assert any("音轨同窗" in x for x in on["report"])
    assert any("音轨不参与参考" in x for x in off["report"])
    assert on["out_frames"] == off["out_frames"]


def test_plan_rejects_too_short_material():
    with pytest.raises(ValueError) as e:
        rv.plan(_info(fps=24.0, dur=0.1, frames=3), rv.parse_state({"on": True}), 0, "太短")
    assert "5 帧" in str(e.value) and "太短" in str(e.value)


def test_pad_box_keeps_aspect_and_centers():
    iw, ih, x, y = rv.pad_box(1920, 1080, 768, 768)
    assert (iw, ih) == (768, 432)
    assert (x, y) == (0, 168)


# ---------------------------------------------------------------- parse_state

def test_parse_state_defaults_and_no_mutation():
    before = dict(rv.DEFAULT_REFVID)
    out = rv.parse_state(None)
    assert out == before and out is not rv.DEFAULT_REFVID
    out["on"] = True
    assert rv.DEFAULT_REFVID == before, "parse_state 不能污染全局默认值"


def test_parse_state_drops_unknown_and_clamps():
    out = rv.parse_state({"mp": 99, "ar": "不存在", "fit": "乱填",
                          "音偏置": 1, "on": "yes"})
    assert out["mp"] == 2.0
    assert out["ar"] == rv.DEFAULT_AR
    assert out["fit"] == rv.DEFAULT_REFVID["fit"]
    assert out["on"] is False
    assert "音偏置" not in out


def test_parse_state_keeps_valid_values():
    out = rv.parse_state({"on": True, "ar": "9:16", "mp": 0.5,
                          "fit": "补黑边", "resample": "area", "audio": False})
    assert out["on"] is True and out["ar"] == "9:16" and out["mp"] == 0.5
    assert out["fit"] == "补黑边" and out["resample"] == "area" and out["audio"] is False


def test_parse_state_ignores_deleted_timeline_keys():
    """旧 refvid.json 里还带着那四个键 → 直接丢弃，不许影响行为。"""
    out = rv.parse_state({"on": True, "no_upscale": False, "fps_norm": True,
                          "grid": "就近", "limit": "不限", "limit_s": 9.0})
    for k in ("no_upscale", "fps_norm", "grid", "limit", "limit_s"):
        assert k not in out, k


# ---------------------------------------------------------------- 指纹

def test_signature_is_stable_and_ignores_master_switch():
    """`on` 不进指纹（调用方按开关决定加不加键），其余影响项都进。"""
    a = rv.signature(rv.parse_state({"on": False}))
    b = rv.signature(rv.parse_state({"on": True}))
    assert a == b
    base = rv.signature(rv.parse_state({"on": True}))
    for k, v in (("ar", "9:16"), ("mp", 0.5), ("fit", "拉伸"),
                 ("resample", "area"), ("audio", False)):
        assert rv.signature(rv.parse_state({"on": True, k: v})) != base, k


# ---------------------------------------------------------------- 全局设置读写

def test_settings_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "refvid.json"
    monkeypatch.setattr(rv, "settings_path", lambda: str(p))
    assert rv.load_settings() == rv.DEFAULT_REFVID          # 文件不存在 → 默认副本
    rv.save_settings({"on": True, "ar": "9:16", "mp": 0.5, "audio": False})
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    assert raw["on"] is True and raw["mp"] == 0.5 and raw["audio"] is False
    assert raw["resample"] == rv.DEFAULT_REFVID["resample"]  # 缺失项补齐
    assert rv.load_settings()["ar"] == "9:16"


def test_settings_cache_invalidates_on_write(tmp_path, monkeypatch):
    p = tmp_path / "refvid.json"
    monkeypatch.setattr(rv, "settings_path", lambda: str(p))
    rv.save_settings({"on": True})
    assert rv.current()["on"] is True
    rv.save_settings({"on": False})
    assert rv.current()["on"] is False


def test_save_settings_without_path_returns_none(monkeypatch):
    monkeypatch.setattr(rv, "settings_path", lambda: "")
    assert rv.save_settings({"on": True}) is None
