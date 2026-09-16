"""anchors.py 纯函数回归：档位吸附 / 落点推导 / 归一化 / 硬校验 / 旧字段迁移 / 变更区间。

跑法（仓库根目录）：
    python -m pytest tests/test_anchors.py -q

本模块不 import torch，managed Python 即可跑（ComfyUI 的 .venv 有 torch 无 pytest，
故 torch-free 的纯函数测试一律走 managed Python）。

anchors.py 与 guides.py 都按仓库惯例用 `from . import grid` 相对导入，而包目录名
含连字符（ComfyUI-minimaxH3-SequenceForge）无法作为 Python 包名，所以这里先造一个
合成包再按文件路径加载——而不是为了让测试能跑，在生产代码里塞 try/except 双重导入。
"""

import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = "h3_anchor_test_pkg"


def _load():
    """把 grid / guides / anchors 装进合成包后返回 (grid, guides, anchors)。"""
    if _PKG not in sys.modules:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [ROOT]
        sys.modules[_PKG] = pkg
    for name in ("grid", "guides", "anchors"):
        full = f"{_PKG}.{name}"
        if full not in sys.modules:
            spec = importlib.util.spec_from_file_location(full, os.path.join(ROOT, f"{name}.py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[full] = mod
            spec.loader.exec_module(mod)
    return (sys.modules[f"{_PKG}.grid"], sys.modules[f"{_PKG}.guides"],
            sys.modules[f"{_PKG}.anchors"])


GRID, GUIDES, ANC = _load()


# ---- 档位表 ----

def test_snap_windows_are_exactly_17k_plus_5():
    assert GRID.SNAP_WINDOWS == tuple(5 + 17 * k for k in range(22))
    assert GRID.SNAP_WINDOWS[0] == 5 and GRID.SNAP_WINDOWS[-1] == 362
    assert list(GRID.SNAP_WINDOWS) == sorted(GRID.SNAP_WINDOWS)


def test_every_snap_window_is_token_reachable():
    """档位必须都能被整 token 表达，否则 UI 给得出的宽度实际切不出来。"""
    for w in GRID.SNAP_WINDOWS:
        assert GRID.latent_t_to_frames(GRID.video_latent_t(w)) == w, w


@pytest.mark.parametrize("n,expect", [
    (1, 1), (4, 1),                 # <5 退化单帧锚
    (5, 5), (6, 5), (18, 5), (21, 5),
    (22, 22), (24, 22),
    (39, 39), (40, 39),
    (56, 56), (73, 73),
    (361, 345), (362, 362),         # 上限档位
    (498, 498), (500, 498),         # 超出上限仍按 17k+5 推导（档位表只截到编码护栏）
])
def test_snap_window_down(n, expect):
    assert GRID.snap_window_down(n) == expect


def test_snap_window_down_agrees_with_guides_clip_guide_frames():
    """guids.clip_guide_frames 是官方语义实现，grid.snap_window_down 是纯数学推导。

    两者必须逐值一致——否则导航栏给的档位与执行期实际裁剪会打架。
    这条断言就是「暂留两份实现」的兜底证据：一旦有人改动其中之一，这里立刻红。
    """
    for n in range(1, 2001):
        assert GRID.snap_window_down(n) == GUIDES.clip_guide_frames(n), n


# ---- 落点推导 ----

def test_anchor_frame_index_head():
    assert GRID.anchor_frame_index("head", 22, 120) == 0


def test_anchor_frame_index_tail_sits_at_end():
    assert GRID.anchor_frame_index("tail", 22, 120) == 98
    assert GRID.anchor_frame_index("tail", 1, 120) == 119


def test_anchor_frame_index_mid_passes_through():
    assert GRID.anchor_frame_index("mid", 22, 120, 50) == 50
    # 负值原样返回，解析权在 guides.resolve_frame_index（只定义一处）
    assert GRID.anchor_frame_index("mid", 22, 120, -30) == -30
    assert GUIDES.resolve_frame_index(-30, 120) == 90


def test_anchor_frame_index_rejects_unknown_mode():
    with pytest.raises(ValueError, match="未知锚点落点"):
        GRID.anchor_frame_index("middle", 22, 120)


# ---- 归一化 ----

def _raw(**kw):
    """形状完整的 anchor 原文：校验测的是取值合法性，形状由归一化保证。"""
    a = {"window": 22, "src": {"kind": "prev_tail"}, "at": {"mode": "head"},
         "branches": {"av": "both"}, "on": True}
    a.update(kw)
    return a


def test_normalize_fills_defaults():
    a = ANC.normalize_anchor(_raw(), 0)
    assert a == {
        "id": "a1",
        "src": {"kind": "prev_tail", "ref": "", "start_f": None, "end_f": None,
                "src_fps": None, "meta_ok": True},
        "at": {"mode": "head", "frame_idx": 0},
        "window": 22,
        "branches": {"av": "both"},
        "on": True,
    }


def test_normalize_is_idempotent():
    once = ANC.normalize_anchor(_raw(window=18, id="a7", src={"kind": "library",
                                                             "ref": "latent/h.pt",
                                                             "src_fps": 30.0}), 0)
    twice = ANC.normalize_anchor(once, 5)          # 换 idx 也不该改掉已有 id
    assert once == twice
    assert once["id"] == "a7"


def test_normalize_does_not_silently_snap_window():
    """归一化只补形状，不合法化取值——否则「设 18 帧实际钉 5 帧」会被藏起来。"""
    assert ANC.normalize_anchor(_raw(window=18), 0)["window"] == 18
    with pytest.raises(ValueError, match="请显式取 5 或 22"):
        ANC.validate_anchors([ANC.normalize_anchor(_raw(window=18), 0)], 120)


def test_normalize_rejects_non_positive_window():
    with pytest.raises(ValueError, match="须 ≥ 1 帧"):
        ANC.normalize_anchor(_raw(window=0), 0)


def test_normalize_requires_window():
    with pytest.raises(ValueError, match="缺少 window"):
        ANC.normalize_anchor({"at": {"mode": "head"}})


def test_normalize_rejects_non_dict():
    with pytest.raises(ValueError, match="必须是字典"):
        ANC.normalize_anchor("a1")


def test_normalize_anchors_handles_none():
    assert ANC.normalize_anchors(None) == []


# ---- 硬校验（手动锚不软降级） ----

def test_validate_accepts_head_mid_tail():
    fc = 120
    ANC.validate_anchors([
        ANC.normalize_anchor(_raw(window=22), 0),
        ANC.normalize_anchor(_raw(window=22, at={"mode": "tail"}), 1),
        ANC.normalize_anchor(_raw(window=22, at={"mode": "mid", "frame_idx": 50}), 2),
        ANC.normalize_anchor(_raw(window=1, at={"mode": "tail"}), 3),
    ], fc)


def test_validate_hard_fails_on_out_of_range():
    """设了锚不生效还不说 = 最坏的一类 bug，必须抛而不是静默裁。"""
    with pytest.raises(ValueError, match="越界"):
        ANC.validate_anchors(
            [ANC.normalize_anchor(_raw(window=22, at={"mode": "mid", "frame_idx": 110}), 0)],
            120)


def test_validate_checks_negative_mid_index():
    fc = 120
    ANC.validate_anchors(
        [ANC.normalize_anchor(_raw(window=22, at={"mode": "mid", "frame_idx": -22}), 0)], fc)
    with pytest.raises(ValueError, match="越界"):
        ANC.validate_anchors(
            [ANC.normalize_anchor(_raw(window=22, at={"mode": "mid", "frame_idx": -10}), 0)], fc)


@pytest.mark.parametrize("bad,patt", [
    ({"src": {"kind": "bogus"}}, "源类型"),
    ({"at": {"mode": "bogus"}}, "落点"),
    ({"branches": {"av": "sound"}}, "取用分支"),
    ({"window": 18}, "不在 17k\\+5 档位"),
    ({"src": {"kind": "library", "ref": ""}}, "必须给 ref"),
    ({"src": {"kind": "library", "ref": "latent/a.pt",
              "start_f": 10, "end_f": 10}}, "源帧窗非法"),
])
def test_validate_hard_fails_on_bad_field(bad, patt):
    """直接构造非法 anchor 送校验（绕过归一化的吸附/收敛，专测校验本身）。"""
    a = _raw(**bad)
    with pytest.raises(ValueError, match=patt):
        ANC.validate_anchors([a], 120)


def test_validate_on_false_skips_range_but_keeps_shape_checks():
    off = _raw(window=22, at={"mode": "mid", "frame_idx": 999}, on=False)
    ANC.validate_anchors([off], 120)                     # 关闭的锚不校验越界
    with pytest.raises(ValueError, match="源类型"):
        ANC.validate_anchors([_raw(src={"kind": "bogus"}, on=False)], 120)


# ---- 旧字段迁移 ----

CTX = 22


def test_migrate_default_latent_ref_makes_no_anchor():
    """缺省 = 跟随全局 ctx 的隐式段首桥，不是显式锚（与 seg_hashes 同口径）。"""
    out = ANC.migrate_legacy_seg({"latent_ref": {"on": None, "frames": 0,
                                                 "video": True, "audio": True}}, CTX)
    assert "anchors" not in out
    assert out["unlink"] is False


def test_migrate_latent_ref_frames_override_global_ctx():
    out = ANC.migrate_legacy_seg({"latent_ref": {"frames": 39}}, CTX)
    a = out["anchors"][0]
    assert a["window"] == 39 and a["at"] == {"mode": "head", "frame_idx": 0}
    assert a["src"]["kind"] == "prev_tail" and a["on"] is True


def test_migrate_latent_ref_snaps_frames_to_window():
    out = ANC.migrate_legacy_seg({"latent_ref": {"frames": 18}}, CTX)
    assert out["anchors"][0]["window"] == 5


def test_migrate_latent_ref_library_source():
    out = ANC.migrate_legacy_seg(
        {"latent_ref": {"src": {"file": "latent\\head.pt"}}}, CTX)
    assert out["anchors"][0]["src"] == {
        "kind": "library", "ref": "latent/head.pt", "start_f": None, "end_f": None,
        "src_fps": None, "meta_ok": True}


def test_migrate_latent_ref_audio_off_narrows_branches():
    out = ANC.migrate_legacy_seg({"latent_ref": {"frames": 22, "audio": False}}, CTX)
    assert out["anchors"][0]["branches"]["av"] == "video"


def test_migrate_latent_ref_video_off_means_no_bridge():
    """_inject_guide 的门控是 not want_v -> None，故不带视频 = 整条桥不挂。"""
    out = ANC.migrate_legacy_seg({"latent_ref": {"frames": 22, "video": False}}, CTX)
    assert out["anchors"][0]["on"] is False


def test_migrate_latent_ref_on_false_keeps_light_cut():
    out = ANC.migrate_legacy_seg({"latent_ref": {"on": False}}, CTX)
    a = out["anchors"][0]
    assert a["on"] is False and a["window"] == 22


def test_migrate_tail_src_asset_becomes_single_frame_image_anchor():
    out = ANC.migrate_legacy_seg({"tail_src": {"asset": "雪山"}}, CTX)
    a = out["anchors"][0]
    assert a["src"]["kind"] == "image" and a["src"]["ref"] == "雪山"
    assert a["at"] == {"mode": "tail", "frame_idx": 0}
    assert a["window"] == 1 and a["branches"]["av"] == "video"


def test_migrate_tail_src_latent_becomes_library_anchor():
    out = ANC.migrate_legacy_seg({"tail_src": {"latent": "latent/tail.pt"}}, CTX)
    assert out["anchors"][0]["src"]["kind"] == "library"
    assert out["anchors"][0]["src"]["ref"] == "latent/tail.pt"


def test_migrate_global_last_frame_fills_segments_without_tail_src():
    out = ANC.migrate_legacy_seg({}, CTX, last_frame="尾帧.png")
    assert out["anchors"][0]["src"] == {"kind": "image", "ref": "尾帧.png",
                                        "start_f": None, "end_f": None,
                                        "src_fps": None, "meta_ok": True}


def test_migrate_global_last_frame_does_not_shadow_tail_src():
    out = ANC.migrate_legacy_seg({"tail_src": {"asset": "雪山"}}, CTX,
                                 last_frame="尾帧.png")
    assert len(out["anchors"]) == 1
    assert out["anchors"][0]["src"]["ref"] == "雪山"


def test_migrate_auto_ref_false_becomes_unlink():
    out = ANC.migrate_legacy_seg({"auto_ref": False, "latent_ref": {"frames": 0}}, CTX)
    assert out["unlink"] is True
    assert "anchors" not in out


def test_migrate_legacy_unlink_key_is_read():
    out = ANC.migrate_legacy_seg({"unlink": True}, CTX)
    assert out["unlink"] is True


def test_migrate_drops_all_legacy_keys():
    out = ANC.migrate_legacy_seg(
        {"prompt_v2": "x", "latent_ref": {"frames": 39}, "tail_src": {"asset": "雪山"},
         "auto_ref": True, "unlink": False, "latent_save": {"mode": "all"}}, CTX)
    for k in ("latent_ref", "tail_src", "auto_ref"):
        assert k not in out
    assert out["prompt_v2"] == "x"                       # 无关字段原样保留
    assert out["latent_save"] == {"mode": "all"}         # 保存策略不属本次迁移（步骤 7）


def test_migrate_round_trip_is_idempotent():
    seg = {"prompt_v2": "x", "latent_ref": {"frames": 39, "src": {"file": "latent/a.pt"}},
           "tail_src": {"asset": "雪山"}, "auto_ref": False}
    once = ANC.migrate_legacy_seg(seg, CTX)
    twice = ANC.migrate_legacy_seg(once, CTX)
    assert once == twice
    assert len(once["anchors"]) == 2
    assert once["unlink"] is True


def test_migrate_result_passes_validation():
    out = ANC.migrate_legacy_seg(
        {"latent_ref": {"frames": 39, "src": {"file": "latent/a.pt"}},
         "tail_src": {"asset": "雪山"}}, CTX)
    ANC.validate_anchors(out["anchors"], 120)


# ---- 源归一化（直接切窗 / 先裁后编 / 硬报错） ----

CHW = (16, 720, 1280)          # 本链 (C, H, W)


def _meta(shape=CHW + (), frames=120, reencodable=False, fps=30.0):
    """形状按 (C,T,H,W) 组：_meta(CHW + (31,)) 表示 (16,31,720,1280)。"""
    return {"shape": shape, "frames": frames, "fps": fps, "reencodable": reencodable}


def test_resolve_reuses_when_shape_matches():
    a = ANC.normalize_anchor(_raw(window=22, src={"kind": "segment", "ref": "seg_003"}), 0)
    plan = ANC.resolve_anchor_source(a, CHW, _meta((16, 31, 720, 1280)))
    assert plan["action"] == "reuse"
    assert (plan["start_f"], plan["end_f"], plan["window"]) == (0, 22, 22)
    assert plan["note"] == ""


def test_resolve_encodes_when_shape_differs_but_reencodable():
    """外部视频/图片分辨率不符 → 先裁后编，而不是报错。"""
    a = ANC.normalize_anchor(
        _raw(window=39, src={"kind": "video", "ref": "a.mp4", "start_f": 10, "end_f": 49}), 0)
    plan = ANC.resolve_anchor_source(a, CHW, _meta((16, 61, 1080, 1920), reencodable=True))
    assert plan["action"] == "encode"
    assert (plan["start_f"], plan["end_f"]) == (10, 49)
    assert "先裁后编" in plan["note"] and "1920×1080" in plan["note"]


def test_resolve_hard_fails_when_shape_differs_and_not_reencodable():
    """裸 latent 尺寸不对且不能重编 → 硬报错，并给出转档指引。"""
    a = ANC.normalize_anchor(_raw(window=22, src={"kind": "library", "ref": "latent/x.pt"}), 0)
    with pytest.raises(ValueError, match="分辨率不匹配"):
        ANC.resolve_anchor_source(a, CHW, _meta((16, 31, 1080, 1920)))


def test_resolve_hard_fails_when_source_missing():
    for kind, ref in (("prev_tail", ""), ("segment", "seg_003"),
                      ("library", "latent/x.pt"), ("video", "a.mp4"), ("image", "雪山")):
        a = ANC.normalize_anchor(_raw(window=22, src={"kind": kind, "ref": ref}), 0)
        with pytest.raises(ValueError, match="源不可用"):
            ANC.resolve_anchor_source(a, CHW, _meta(shape=None, frames=None))
        # 每种源都要给出可执行的出路，不能只说「不行」
        try:
            ANC.resolve_anchor_source(a, CHW, _meta(shape=None, frames=None))
        except ValueError as e:
            assert "：" in str(e) and len(str(e)) > 20


def test_resolve_hard_fails_on_window_past_source_end():
    a = ANC.normalize_anchor(
        _raw(window=39, src={"kind": "video", "ref": "a.mp4", "start_f": 100, "end_f": 139}), 0)
    with pytest.raises(ValueError, match="源只有 120 帧"):
        ANC.resolve_anchor_source(a, CHW, _meta((16, 31, 720, 1280), frames=120,
                                               reencodable=True))


def test_resolve_hard_fails_on_inverted_frame_window():
    a = ANC.normalize_anchor(
        _raw(window=22, src={"kind": "video", "ref": "a.mp4", "start_f": 30, "end_f": 30}), 0)
    with pytest.raises(ValueError, match="源帧窗非法"):
        ANC.resolve_anchor_source(a, CHW, _meta((16, 31, 720, 1280), reencodable=True))


def test_resolve_defaults_window_to_zero_onward():
    a = ANC.normalize_anchor(_raw(window=56, src={"kind": "prev_tail"}), 0)
    plan = ANC.resolve_anchor_source(a, CHW, _meta((16, 61, 720, 1280), frames=200))
    assert (plan["start_f"], plan["end_f"]) == (0, 56)


def test_resolve_ignores_source_fps_for_shape_decision():
    """fps 不参与形状判断（latent 里根本没有帧率，帧率只影响源内秒窗换算）。"""
    a = ANC.normalize_anchor(_raw(window=22, src={"kind": "library", "ref": "latent/x.pt"}), 0)
    plan = ANC.resolve_anchor_source(a, CHW, _meta((16, 31, 720, 1280), fps=None))
    assert plan["action"] == "reuse"


# ---- 变更区间 ----

def _h(*vals):
    return list(vals)


def test_change_intervals_none():
    assert ANC.change_intervals(_h("a", "b", "c"), _h("a", "b", "c"), 3) == []


def test_change_intervals_isolated():
    assert ANC.change_intervals(_h("a", "b", "c"), _h("a", "X", "c"), 3) == [[1, 1]]


def test_change_intervals_consecutive_merge():
    assert ANC.change_intervals(_h("a", "b", "c", "d", "e"),
                                _h("a", "X", "Y", "d", "e"), 5) == [[1, 2]]


def test_change_intervals_discrete_stay_separate():
    assert ANC.change_intervals(_h("a", "b", "c", "d", "e"),
                                _h("a", "X", "c", "Y", "e"), 5) == [[1, 1], [3, 3]]


def test_change_intervals_first_segment():
    assert ANC.change_intervals(_h("a", "b"), _h("X", "b"), 2) == [[0, 0]]


def test_change_intervals_last_segment():
    assert ANC.change_intervals(_h("a", "b", "c"), _h("a", "b", "X"), 3) == [[2, 2]]


def test_change_intervals_runs_to_end():
    assert ANC.change_intervals(_h("a", "b", "c"), _h("X", "Y", "Z"), 3) == [[0, 2]]


def test_change_intervals_ignores_appended_segments():
    """末尾追加 = 未完成的段，交给续跑，不是「变更」。"""
    assert ANC.change_intervals(_h("a", "b"), _h("a", "b", "c", "d"), 2) == []


def test_change_intervals_ignores_removed_tail():
    """删除末尾段交给调用方 truncate，不是区间重做。"""
    assert ANC.change_intervals(_h("a", "b", "c"), _h("a", "b"), 3) == []


def test_change_intervals_done_zero():
    assert ANC.change_intervals(_h(), _h("a", "b"), 0) == []


def test_change_intervals_only_compares_completed_prefix():
    """done 之后的段尚未生成，其哈希差异不构成变更。"""
    assert ANC.change_intervals(_h("a", "b", "OLD"), _h("a", "b", "NEW"), 2) == []
