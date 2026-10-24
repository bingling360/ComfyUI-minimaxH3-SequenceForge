"""桥锚相位对齐回归（2026-10-24）。

背景：锚定区 token 数恒 ≡2 (mod 5)（`video_latent_t(17k+5) = 5k+2`）。
正文若也取 17k+5，其 token 数同样 ≡2，两者相加 ≡4 —— 于是从末端往回数
vt 个 token 的起点落在周期中间（相位 2），抽来的帧数序列
（`4,4,4,1,4,4,4` = 25 帧）与本段 token `0..vt-1`（恒为 `1,4,4,4,4,1,4`
= 22 帧）**逐个错位**：上一段"装 4 帧的 token"被塞进本段"装 1 帧的位置"。

`grid.align_phase_down` 把 end_t 吸附到 ≡2 (mod 5) 修掉它。本文件钉住：
  1) 纯函数行为；
  2) 端到端：所有 ctx 档位 × 所有实际段长，起点相位恒 0、逐 token 完全一致、
     裁头量 == 锚定区帧数；
  3) `引导帧数=1` 的裁头量抬到 5（H3 最小锚定粒度是 2 token = 5 帧）。
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import grid  # noqa: E402


def _snap_window(frames):
    """与 grid.snap_window_down 同式（官方语义：<5 退化为 1）。"""
    n = int(frames)
    if n < grid.MIN_WINDOW_FRAMES:
        return 1
    return grid.latent_t_to_frames(grid.video_latent_t(n))


def _eff_window(ctx):
    """与 nodes._eff_inject 同式：ctx<5 抬到 5，与 video_latent_t 的 2 token 对齐。"""
    w = _snap_window(ctx)
    if 0 < w < grid.MIN_WINDOW_FRAMES:
        w = grid.MIN_WINDOW_FRAMES
    return w


def _simulate(ctx, seg_frames):
    """复现 nodes._decode_crop 的对齐链（含相位吸附）。"""
    skip_f = _eff_window(ctx)
    vis_len = int(seg_frames)
    end_t = grid.frames_to_latent_t(skip_f + vis_len, up=True)
    ph = grid.align_phase_down(end_t)
    if ph != end_t and grid.latent_t_to_frames(ph) - skip_f >= 5:
        end_t = ph
    vis_len2 = grid.latent_t_to_frames(end_t) - skip_f
    vt = grid.video_latent_t(ctx)
    start = end_t - vt
    src = [grid.FRAME_PER_TOKEN[(start + i) % 5] for i in range(vt)]
    dst = [grid.FRAME_PER_TOKEN[i % 5] for i in range(vt)]
    return dict(skip_f=skip_f, vis_len=vis_len2, end_t=end_t, vt=vt,
                start=start, phase=start % 5, src=src, dst=dst)


# 4–15s 区间内实际会被 _snap_seconds 吸附到的段长（17k+5）
REAL_LENS = [n for n in range(96, 363) if n % 17 == 5]


@pytest.mark.parametrize("ctx", [1, 5, 22, 39, 56])
@pytest.mark.parametrize("seg", REAL_LENS)
def test_phase_aligned_after_fix(ctx, seg):
    r = _simulate(ctx, seg)
    assert r["end_t"] % 5 == 2, f"末端未吸附到 ≡2: {r}"
    assert r["phase"] == 0, f"起点相位非 0: {r}"
    assert r["src"] == r["dst"], f"逐 token 错位: {r}"
    assert sum(r["src"]) == r["skip_f"], f"覆盖帧数 != 锚定区帧数: {r}"
    assert r["vis_len"] >= 5, f"导出帧数过小: {r}"


@pytest.mark.parametrize("ctx", [5, 22, 39, 56])
def test_export_len_is_17_multiple(ctx):
    """修好后导出帧数恒为 17 的倍数（22 + 119 = 141 = 17×8+5 的来源）。"""
    for seg in (107, 124, 141, 158):
        r = _simulate(ctx, seg)
        assert r["vis_len"] % 17 == 0, r


def test_align_phase_down_basic():
    assert grid.align_phase_down(44) == 42
    assert grid.align_phase_down(42) == 42
    assert grid.align_phase_down(43) == 42
    assert grid.align_phase_down(39) == 37
    assert grid.align_phase_down(38) == 37
    assert grid.align_phase_down(0) == 0
    assert grid.align_phase_down(-1) == -1


def test_align_phase_down_is_idempotent():
    for t in range(1, 200):
        once = grid.align_phase_down(t)
        assert grid.align_phase_down(once) == once, t


def test_ctx1_window_lifted_to_min():
    """ctx=1 的裁头量必须抬到 5，否则裁 1 帧却钉 5 帧（4 帧上段画面泄漏）。"""
    assert _snap_window(1) == 1          # 官方语义仍是 1
    assert _eff_window(1) == 5           # 生效值抬到 5
    assert _eff_window(5) == 5
    assert _eff_window(22) == 22


def test_ctx1_head_and_anchor_agree():
    """抬到 5 之后，裁头量 == 锚定 token 覆盖帧数。"""
    r = _simulate(1, 124)
    assert r["skip_f"] == 5
    assert sum(r["dst"]) == 5
    assert r["src"] == r["dst"]


@pytest.mark.parametrize("seg", REAL_LENS)
def test_no_anchor_keeps_vis_len(seg):
    """无锚点（skip_f=0）时不需要吸附。

    段长本身已落 17k+5 → `frames_to_latent_t` 直接给出 ≡2 的 token 数，
    `align_phase_down` 应当是恒等变换，导出帧数一分不少。

    这条是「有锚点才需要 17n」的对照面：锚定区把 token 数从 5k+2 推到 5k+4，
    才需要正文补成 17n；没有锚定区时正文自己就是 17k+5。
    """
    end_t = grid.frames_to_latent_t(seg, up=True)
    assert end_t % 5 == 2, f"无锚点时 end_t 应自动 ≡2: {seg}->{end_t}"
    assert grid.align_phase_down(end_t) == end_t, f"无锚点不该触发吸附: {seg}"
    assert grid.latent_t_to_frames(end_t) == seg, f"无锚点时导出帧数应精确等于段长: {seg}"


def test_no_anchor_short_seg():
    """最小段长（5 帧）无锚点也安全。"""
    end_t = grid.frames_to_latent_t(5, up=True)
    assert end_t == 2 and end_t % 5 == 2
    assert grid.align_phase_down(end_t) == end_t


def test_anchor_requires_body_be_17_multiple():
    """有锚点时，导出帧数必须落 17n（不是 17k+5）——这是「有锚点才是 17n」的断言。"""
    for ctx in (5, 22, 39, 56):
        for seg in (124, 141, 158):
            r = _simulate(ctx, seg)
            assert r["vis_len"] % 17 == 0, f"ctx={ctx} seg={seg} -> {r['vis_len']}"
