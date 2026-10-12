"""vae_chunk（H3 视频 VAE 时间分块解码）回归。

跑法（仓库根目录）：
    python -m pytest tests/test_vae_chunk.py -q

**这个测试存在的唯一目的**：证明「分块后的帧数」与「整段的帧数」算法与官方 VAE
**逐字一致**。帧数算错会导致音画不同步 / 段长漂移 / 成片错位，是这类改动唯一
真正致命的失败模式 —— 所以 `plan()` 在账目对不上时会**拒绝分块**，而本文件的
大部分用例就是在钉死「什么时候该拒绝」。

真值来源：`comfy/ldm/minimax/vae.py:408-440` 的构造参数
（space_down=(2,2,2,2,1,1) → vae_ratio=16；time_down=(1,2,2,1,1,1) → vae_ratio_t=4；
clip_length=17、token_drop=3）→ 派生出 frame_pre_padding=3、tokens_chunk_size=5、
token_overlap=2、frame_overlap=5。

手算锚点（30 token，手写推导，不来自任何代码）：
  _chunks(30): pseudo=30+3=33 → pad=(-33)%5=2 → pseudo=35 → n=35//5-1=6
  frame_plan(z=32, n=6, pad=2): 每块 17 帧 ×6 = 102，末尾重叠 5 → 107，
  减去 _pad_frames(32,2)=5（k=0 命中 intra_tail=1、k=1 是 4）→ **102**
  交叉验证：30×4 − 3×6 = 120 − 18 = 102 ✓
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import vae_chunk as VC  # noqa: E402


class FakeFirstStage:
    """照抄 H3 VideoVAE 的真实构造参数与派生值。"""

    vae_ratio = 16
    vae_ratio_t = 4
    clip_length = 17
    token_drop = 3
    frame_pre_padding = (-17) % 4            # 3
    tokens_chunk_size = -(-17 // 4)          # ceil(17/4) = 5
    token_overlap = (-3) % 5                 # 2
    frame_overlap = max(2 * 4 - 3, 0)        # 5

    def decode_output_shape(self, input_shape):
        b, c, t, h, w = input_shape
        return (b, 3, VC.frames_for(VC.vae_params(FakeVAE()), t),
                h * self.vae_ratio, w * self.vae_ratio)


class FakeVAE:
    def __init__(self):
        self.first_stage_model = FakeFirstStage()


def _p():
    return VC.vae_params(FakeVAE())


# ── 1) 参数读取与派生值 ──

def test_params_read_from_real_layout():
    p = _p()
    assert p["vae_ratio_t"] == 4
    assert p["tokens_chunk_size"] == 5
    assert p["frame_pre_padding"] == 3
    assert p["token_overlap"] == 2
    assert p["token_drop"] == 3


def test_params_missing_is_none():
    assert VC.vae_params(None) is None
    assert VC.vae_params(object()) is None


# ── 2) 帧数公式：与手算锚点一致 ──

def test_frames_for_matches_hand_computed_anchor():
    assert VC.frames_for(_p(), 30) == 102


def test_frames_for_more_hand_walked_anchors():
    """另外两个手算锚点（都是完整走一遍官方公式推出来的，不是套简化式）。

    ⚠ 别再拿 `T×4 − 3×块数` 当第二把尺子 —— 它在 T=12 上给出 42 而真值是 39。
    官方分块是**带 2 token 重叠**的（token_overlap=2），块与块并非首尾相接，
    所以「总帧 − 每块丢 3 帧」这个简化式只在部分长度上碰巧成立。
    """
    p = _p()
    # T=20: pseudo=23 → pad=2 → pseudo=25 → n=4；每块 17 帧×4=68，末尾重叠 5 → 73，
    #       _pad_frames(22,2)=5（k=0 命中 intra_tail=1、k=1 是 4）→ 68
    assert VC.frames_for(p, 20) == 68
    # T=12: pseudo=15 → pad=0 → n=2；17×2=34，末尾重叠 5 → 39，pad_frames=0 → 39
    assert VC.frames_for(p, 12) == 39


def test_frames_for_tiny_input_still_positive():
    p = _p()
    for T in (1, 2, 3, 4, 5, 6):
        assert VC.frames_for(p, T) >= 0


# ── 3) 账目校验：对不上就必须拒绝（安全契约的核心）──

def test_plan_refuses_when_sum_mismatches(monkeypatch):
    """伪造一个「怎么切都凑不出整段帧数」的 VAE → plan 必须 ok=False。"""
    p = _p()
    real = VC.frames_for

    def _lying(_p2, z_len):
        return real(p, z_len) + (1 if z_len < 100 else 0)   # 切断处永远多 1 帧
    monkeypatch.setattr(VC, "frames_for", _lying)
    out = VC.plan(FakeVAE(), 100, max_tokens=20)
    assert out["ok"] is False
    assert "帧数" in out["why"]


def test_plan_refuses_when_no_params():
    out = VC.plan(object(), 100, max_tokens=20)
    assert out["ok"] is False


def test_plan_refuses_when_shorter_than_limit():
    out = VC.plan(FakeVAE(), 30, max_tokens=999)
    assert out["ok"] is False
    assert "无需分块" in out["why"]


def test_plan_refuses_bad_max_tokens():
    assert VC.plan(FakeVAE(), 100, max_tokens=0)["ok"] is False
    assert VC.plan(FakeVAE(), 100, max_tokens=None)["ok"] is False


# ── 4) 能分的时候：帧数必须与整段逐字相等 ──

def test_plan_when_accepted_sums_exactly():
    vae = FakeVAE()
    whole = VC.frames_for(_p(), 120)
    for mt in (10, 15, 20, 25, 30, 40, 60):
        out = VC.plan(vae, 120, max_tokens=mt)
        if not out["ok"]:
            continue
        assert sum(out["frames_each"]) == whole
        assert out["frames_total"] == whole
        # 切段必须首尾相接、覆盖整段
        assert out["pieces"][0][0] == 0
        assert out["pieces"][-1][1] == 120
        for i in range(len(out["pieces"]) - 1):
            assert out["pieces"][i][1] == out["pieces"][i + 1][0]


def test_plan_never_produces_single_piece():
    """返回 ok 时必须真的分成了 ≥2 段（否则等于没分，调用方会白付一次规划）。"""
    vae = FakeVAE()
    for mt in (8, 10, 12, 20, 30):
        out = VC.plan(vae, 120, max_tokens=mt)
        if out["ok"]:
            assert len(out["pieces"]) >= 2


def test_plan_respects_max_tokens():
    vae = FakeVAE()
    out = VC.plan(vae, 120, max_tokens=12)
    if out["ok"]:
        for a, b in out["pieces"]:
            assert b - a <= 12


# ── 5) auto 档与 blend ──

def test_auto_chunk_tokens_scales_with_budget(monkeypatch):
    import perf

    class L:
        shape = (1, 24, 100, 68, 120)

    monkeypatch.setattr(perf, "decode_pixels_need_gb", lambda v, l: 8.0)
    monkeypatch.setattr(perf, "probe_ram_swap",
                        lambda: ({"total_gb": 64.0, "avail_gb": 4.0},
                                 {"total_gb": 0.0, "free_gb": 0.0}))
    # 预算 = 4.0 × 0.5 = 2.0GB；need=8.0 → 每块最多 100 × 2/8 = 25 token
    assert perf.auto_chunk_tokens(None, L()) == 25


def test_auto_chunk_tokens_unknown_is_zero(monkeypatch):
    import perf

    monkeypatch.setattr(perf, "decode_pixels_need_gb", lambda v, l: None)
    monkeypatch.setattr(perf, "probe_ram_swap", lambda: (None, None))
    assert perf.auto_chunk_tokens(None, type("L", (), {"shape": (1, 24, 30, 8, 8)})()) == 0


def test_blend_zero_is_identity():
    torch = pytest.importorskip("torch")
    prev = torch.zeros(1, 3, 10, 2, 2)
    nxt = torch.ones(1, 3, 10, 2, 2)
    p2, n2 = VC._blend_join(prev, nxt, 0)
    assert p2 is prev and n2 is nxt            # blend=0 → 什么都不动


def test_blend_join_keeps_frame_count():
    """溶解只能改变像素，**不能**改变帧数 —— 否则整段时长会漂、音画会不同步。"""
    torch = pytest.importorskip("torch")
    prev = torch.zeros(1, 3, 10, 2, 2)
    nxt = torch.ones(1, 3, 10, 2, 2)
    p2, n2 = VC._blend_join(prev, nxt, 3)
    assert int(p2.shape[2]) == 10
    assert int(n2.shape[2]) == 10
    assert int(p2.shape[2]) + int(n2.shape[2]) == 20


def test_blend_join_really_ramps():
    """尾部应被改成「从 prev 值爬到 nxt 值」的斜坡，而不是原样或全量替换。"""
    torch = pytest.importorskip("torch")
    prev = torch.zeros(1, 3, 10, 1, 1)
    nxt = torch.ones(1, 3, 10, 1, 1)
    p2, _ = VC._blend_join(prev, nxt, 4)
    tail = p2[0, 0, -4:, 0, 0].tolist()
    assert tail[0] < tail[1] < tail[2] < tail[3]
    assert tail[3] < 1.0            # 开区间：末帧不是纯 nxt，否则会出现半帧突跳
    assert p2[0, 0, 0, 0, 0].item() == 0.0   # 头部不受影响


# ── 6) perf 键声明（守 DEFAULT_PERF / PERF_TYPES 两处同步）──

def test_perf_keys_declared():
    import perf

    assert perf.DEFAULT_PERF.get("vae_decode_chunk_on") is False   # 默认关
    assert perf.DEFAULT_PERF.get("vae_decode_chunk_tokens") == 0   # 0 = 自动
    assert perf.DEFAULT_PERF.get("vae_decode_blend") == 0
    for k, t in (("vae_decode_chunk_on", bool),
                 ("vae_decode_chunk_tokens", int),
                 ("vae_decode_blend", int)):
        assert t in perf.PERF_TYPES.get(k, ())
