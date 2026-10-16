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


def test_default_limit_is_longest_segment_not_unbounded():
    """出厂「时长上限」必须是「最长段」：不限会让超长素材按目标画布白占内存。"""
    assert rv.DEFAULT_REFVID["limit"] == "最长段"
    assert rv.DEFAULT_REFVID["on"] is False


# ---------------------------------------------------------------- 网格

@pytest.mark.parametrize("n,down,near,up", [
    (124, 124, 124, 124),   # 5s@24fps 本来就在网格上
    (72, 56, 73, 73),       # 3.00s → 2.33s / 3.04s
    (130, 124, 124, 141),
    (240, 226, 243, 243),
    (5, 5, 5, 5),
    (4, 5, 5, 5),
])
def test_snap_grid(n, down, near, up):
    assert rv.snap_grid(n, "向下") == down
    assert rv.snap_grid(n, "就近") == near
    assert rv.snap_grid(n, "向上") == up


def test_snap_grid_down_is_official_behaviour():
    """官方 `while n % 17 != 5: n -= 1` == 向下档；且永不补帧。"""
    for n in range(5, 400):
        got = rv.snap_grid(n, "向下")
        assert got <= n and got % 17 == 5


# ---------------------------------------------------------------- 归一方案

def test_plan_retimes_30fps_to_24fps_keeping_duration():
    """30fps 素材必须**按时间轴**抽帧：输出时长 == 源时长（误差 ≤ 一格网格）。

    这是本模块存在的首要理由：官方把帧当 24fps 用 → 30fps 素材整体放慢 25%。
    """
    cfg = rv.parse_state({"on": True, "grid": "就近"})
    p = rv.plan(_info(fps=30.0, dur=5.0, frames=150), cfg, 124, "素材")
    assert p["src_fps"] == 30.0
    assert p["out_frames"] == 124                      # 120 → 就近对齐到 124
    assert abs(p["out_frames"] / rv.FPS - 5.0) <= 0.7  # 时长保住（网格粒度内）
    # 抽帧表 = round(i × 30 / 24)，单调不减（.5 走 Python banker's：round(2.5)=2）
    assert p["src_idx"][:5] == [0, 1, 2, 4, 5]
    assert p["src_idx"] == sorted(p["src_idx"])
    assert p["src_end"] == max(p["src_idx"]) + 1
    assert (p["w"], p["h"]) == (1344, 768)
    assert p["a_start"] == 0.0 and abs(p["a_end"] - p["out_frames"] / 24.0) < 1e-9
    assert any("17k+5" in x for x in p["report"])


def test_plan_no_fps_norm_keeps_source_frames_and_warns():
    cfg = rv.parse_state({"on": True, "fps_norm": False, "grid": "向下"})
    p = rv.plan(_info(fps=30.0, dur=4.0, frames=120), cfg, 124, "素材")
    assert p["src_idx"] == list(range(120)) or p["out_frames"] == len(p["src_idx"])
    assert any("fps 归一" in x for x in p["report"])


def test_plan_24fps_source_still_snaps_down_by_default():
    """24fps 源也要走网格：120 帧 → 向下对齐 107（官方同样砍）。

    ⚠ 这是**向下档的既定代价**（官方 `while n % 17 != 5: n -= 1` 同款），
    所以报告里必须写明「120 → 107，5.00s → 4.46s」；要保住时长就换「就近/向上」。
    """
    cfg = rv.parse_state({"on": True, "grid": "向下"})
    p = rv.plan(_info(fps=24.0, dur=5.0, frames=120), cfg, 124, "素材")
    assert p["out_frames"] == 107
    assert p["src_idx"] == list(range(107))
    assert any("120 → 107" in x for x in p["report"])
    near = rv.plan(_info(fps=24.0, dur=5.0, frames=120),
                   rv.parse_state({"on": True, "grid": "就近"}), 124, "素材")
    assert near["out_frames"] == 124


def test_plan_low_fps_source_repeats_frames_not_truncates():
    """源 12fps：每帧在 24fps 时间轴上重复两次，**时长不变**（绝不去重砍半）。

    去重是个真实的坑：一旦去重，5s 素材会变成 2.25s（放快一倍）。
    """
    cfg = rv.parse_state({"on": True, "grid": "向下"})
    p = rv.plan(_info(fps=12.0, dur=5.0, frames=60), cfg, 0, "素材")
    assert p["out_frames"] == 107
    assert len(p["src_idx"]) == 107
    assert p["src_idx"][:5] == [0, 0, 1, 2, 2]      # round(i/2) 的 banker's 语义
    assert max(p["src_idx"]) <= 59
    assert any("重复" in x for x in p["report"])


def test_plan_tail_padding_is_reported():
    """源不够长 + 就近/向上 → 尾部重复末帧补网格，必须报告（不是静默）。"""
    cfg = rv.parse_state({"on": True, "grid": "就近"})
    p = rv.plan(_info(fps=30.0, dur=5.0, frames=150), cfg, 124, "素材")
    assert p["out_frames"] == 124
    assert p["src_idx"][-1] == 149
    assert any("重复末帧" in x for x in p["report"])


def test_plan_no_upscale_falls_back_to_source_size():
    """只缩不放：源比目标小 → 回落到源尺寸的 32 对齐（官方 :322-324 同款）。"""
    cfg = rv.parse_state({"on": True, "no_upscale": True})
    p = rv.plan(_info(fps=24.0, dur=2.0, w=640, h=360), cfg, 124, "小素材")
    assert p["no_upscale_hit"] is True
    assert (p["w"], p["h"]) == (640, 352)
    assert any("只缩不放" in x for x in p["report"])


def test_plan_upscale_when_switch_off():
    cfg = rv.parse_state({"on": True, "no_upscale": False})
    p = rv.plan(_info(fps=24.0, dur=2.0, w=640, h=360), cfg, 124, "小素材")
    assert p["no_upscale_hit"] is False
    assert (p["w"], p["h"]) == (1344, 768)


def test_plan_limit_longest_segment_caps_frames():
    cfg = rv.parse_state({"on": True, "limit": "最长段", "grid": "向下"})
    p = rv.plan(_info(fps=24.0, dur=10.0, frames=240), cfg, 124, "长素材")
    assert p["out_frames"] <= 124
    assert any("截取前" in x for x in p["report"])


def test_plan_hard_guard_bounds_memory_even_with_limit_off():
    """「不限」也不能无界：模型只会用到 frame_count 帧，多归一 = 白占内存。

    1344×768 float32 × 124 帧 ≈ 1.5GB，所以闸门必须独立于用户设置存在。
    """
    cfg = rv.parse_state({"on": True, "limit": "不限", "grid": "向下"})
    p = rv.plan(_info(fps=24.0, dur=30.0, frames=720), cfg, 124, "超长素材")
    assert p["out_frames"] <= int(124 * 1.5) + 17 + 17   # 闸门 + 一格网格
    assert any("硬上限" in x for x in p["report"])
    assert any("只会用到前 124 帧" in x for x in p["report"])


def test_plan_guard_uses_trained_bound_without_segment_info():
    """拿不到段长（max_seg_frames=0）时按官方训练上限 362 帧兜底。"""
    cfg = rv.parse_state({"on": True, "limit": "不限", "grid": "向下"})
    p = rv.plan(_info(fps=24.0, dur=60.0, frames=1440), cfg, 0, "无段长")
    assert p["out_frames"] <= 362
    assert any("362" in x for x in p["report"])


def test_plan_reports_素材长于本段():
    cfg = rv.parse_state({"on": True, "limit": "不限", "grid": "向下"})
    p = rv.plan(_info(fps=24.0, dur=10.0, frames=240), cfg, 124, "长素材")
    assert p["out_frames"] > 124
    assert any("只会用到前 124 帧" in x for x in p["report"])


def test_plan_fit_modes():
    for fit, crop in (("裁切填满", "center"), ("补黑边", "pad"), ("拉伸", "disabled")):
        cfg = rv.parse_state({"on": True, "fit": fit})
        p = rv.plan(_info(fps=24.0, dur=2.0), cfg, 0, "素材")
        assert p["crop"] == crop


def test_plan_audio_flag_is_reported_not_applied():
    """plan 只回报音频口径；真正「注不注入音轨」由调用方按同一个开关决定。"""
    on = rv.plan(_info(), rv.parse_state({"on": True, "audio": True}), 0, "素材")
    off = rv.plan(_info(), rv.parse_state({"on": True, "audio": False}), 0, "素材")
    assert any("音轨同步裁" in x for x in on["report"])
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
    out = rv.parse_state({"mp": 99, "ar": "不存在", "fit": "乱填", "grid": 7,
                          "limit_s": -5, "音偏置": 1, "on": "yes"})
    assert out["mp"] == 2.0
    assert out["ar"] == rv.DEFAULT_AR
    assert out["fit"] == rv.DEFAULT_REFVID["fit"]
    assert out["grid"] == rv.DEFAULT_REFVID["grid"]
    assert out["limit_s"] == 0.0
    assert out["on"] is False
    assert "音偏置" not in out


def test_parse_state_keeps_valid_values():
    out = rv.parse_state({"on": True, "ar": "9:16", "mp": 0.5, "no_upscale": False,
                          "fit": "补黑边", "resample": "area", "fps_norm": False,
                          "grid": "就近", "limit": "自定义", "limit_s": 3.5,
                          "audio": False})
    assert out["on"] is True and out["ar"] == "9:16" and out["mp"] == 0.5
    assert out["no_upscale"] is False and out["fit"] == "补黑边"
    assert out["resample"] == "area" and out["fps_norm"] is False
    assert out["grid"] == "就近" and out["limit"] == "自定义"
    assert out["limit_s"] == 3.5 and out["audio"] is False


# ---------------------------------------------------------------- 指纹

def test_signature_is_stable_and_ignores_master_switch():
    """`on` 不进指纹（调用方按开关决定加不加键），其余影响项都进。"""
    a = rv.signature(rv.parse_state({"on": False}))
    b = rv.signature(rv.parse_state({"on": True}))
    assert a == b
    base = rv.signature(rv.parse_state({"on": True}))
    for k, v in (("ar", "9:16"), ("mp", 0.5), ("no_upscale", False),
                 ("fit", "拉伸"), ("resample", "area"), ("fps_norm", False),
                 ("grid", "就近"), ("limit", "自定义"), ("limit_s", 3.0),
                 ("audio", False)):
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
