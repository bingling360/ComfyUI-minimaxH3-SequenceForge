"""decode_ram_guard 回归：解码前的**内存**预算体检（perf 键 + 纯函数判据）。

跑法（仓库根目录）：
    python -m pytest tests/test_decode_ram_guard.py -q

背景（2026-10-13 用户追问「为什么第二段解码时内存爆」）：

  ★ 根因：H3 视频 VAE 会在**开始解码之前**把整段视频的完整像素缓冲一次性
    预分配到**主机内存**（ComfyUI `comfy/sd.py:1281-1286` 预先
    `torch.empty(first_stage_model.decode_output_shape(...))`；VAE 自己的兜底路径
    `comfy/ldm/minimax/vae.py:713` 注释写着 "stream out of VRAM so the full
    video never sits on the GPU"）。这是一张**与帧数线性相关的固定开口支票**：
    显存再干净也救不了，任何卸载开关都影响不到它。

  ★ 为什么现有的兜底全失效：Linux 无 swap / cgroup 下内存不够是 **SIGKILL**，
    连异常都不抛 → `oom_retry`「抛了再救」的路径完全用不上（表现为无声断连）。

  ★ 用户要求：**不要在没必要的时候白付代价**。所以判据是「预测需求」而不是
    「历史残留」——够用就一个 if 都不多走，只有预测真会撞时才做最便宜的两件事
    （gc.collect + malloc_trim），且必须复测如实报告。

钉住：
  1) perf 键存在、默认 "auto"、类型表收 str；
  2) 像素支票的算式（帧数 × 高 × 宽 × 3 × 4B × 余量）；
  3) 够用时**零动作**（不 gc、不 trim）；
  4) 预测不够 + auto → gc + trim + 复测；mode=warn → 永不改行为；
  5) mode=off → 连 meminfo 都不读；
  6) 任何一环量不到 → 降级跳过，**绝不拦解码的路**。
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import perf  # noqa: E402


class FakeLatent:
    def __init__(self, shape):
        self.shape = shape


class FakeFirstStage:
    """最小 first_stage_model 桩：只兑现 `decode_output_shape`。"""

    def __init__(self, shape):
        self._shape = shape

    def decode_output_shape(self, input_shape):
        return self._shape


class FakeVAE:
    def __init__(self, shape):
        self.first_stage_model = FakeFirstStage(shape)


def _install(monkeypatch, avail_gb, rss_values):
    """把内存探测换成固定值。`rss_values` 会被依次消费（模拟回收后下降）。"""
    monkeypatch.setattr(perf, "probe_ram_swap",
                        lambda: ({"total_gb": 64.0, "avail_gb": avail_gb},
                                 {"total_gb": 0.0, "free_gb": 0.0}))
    seq = list(rss_values)

    def _rss():
        return seq[0] if len(seq) == 1 else seq.pop(0)
    monkeypatch.setattr(perf, "rss_gb", _rss)
    return seq


# ── 1) perf 键声明 ──

def test_perf_key_declared():
    assert perf.DEFAULT_PERF.get("decode_ram_guard") == "auto"
    t = perf.PERF_TYPES.get("decode_ram_guard")
    assert t is not None and str in t


# ── 2) 像素支票的算式 ──
# (1,3,240,1080,1920) = 240 帧 1920×1080 → 240*1080*1920*3*4 = 5.97GB

def test_decode_pixels_need_gb_math():
    vae = FakeVAE((1, 3, 240, 1080, 1920))
    need = perf.decode_pixels_need_gb(vae, FakeLatent((1, 24, 30, 68, 120)), factor=1.0)
    assert need is not None
    assert abs(need - (240 * 1080 * 1920 * 3 * 4) / (1024 ** 3)) < 1e-6


def test_decode_pixels_need_gb_unknown_shape_is_none():
    class NoShape:
        pass
    assert perf.decode_pixels_need_gb(NoShape(), FakeLatent((1,))) is None


# ── 3) 够用时零动作（用户的核心要求）──

def test_enough_means_no_action(monkeypatch):
    _install(monkeypatch, avail_gb=40.0, rss_values=[30.0])
    called = {"gc": 0, "trim": 0}
    monkeypatch.setattr(perf, "malloc_trim", lambda level=0: called.__setitem__("trim", 1))
    import gc
    monkeypatch.setattr(gc, "collect", lambda: called.__setitem__("gc", 1))

    vae = FakeVAE((1, 3, 240, 1080, 1920))
    out = perf.decode_ram_check(vae, FakeLatent((1, 24, 30, 68, 120)), mode="auto")
    assert out["level"] == "ok"
    assert out["action"] == "none"
    assert called == {"gc": 0, "trim": 0}


def test_off_mode_does_not_even_probe(monkeypatch):
    probes = []
    monkeypatch.setattr(perf, "probe_ram_swap", lambda: probes.append(1) or (None, None))
    vae = FakeVAE((1, 3, 240, 1080, 1920))
    perf.decode_ram_check(vae, FakeLatent((1,)), mode="off")
    assert probes == []


# ── 4) 预测不够时才动手，且 warn 永不改行为 ──

def test_short_and_auto_returns(monkeypatch, capsys):
    _install(monkeypatch, avail_gb=3.0, rss_values=[58.0, 55.5])
    called = {"gc": 0, "trim": 0}
    import gc
    monkeypatch.setattr(gc, "collect", lambda: called.__setitem__("gc", 1))
    monkeypatch.setattr(perf, "malloc_trim", lambda level=0: called.__setitem__("trim", 1))

    vae = FakeVAE((1, 3, 240, 1080, 1920))
    out = perf.decode_ram_check(vae, FakeLatent((1,)), mode="auto", tag="段2 ")
    assert out["level"] == "short"
    assert out["action"] == "returned"
    assert called == {"gc": 1, "trim": 1}
    assert out["freed_gb"] == pytest.approx(2.5, abs=0.01)
    assert "解码预算不足" in out["message"]
    assert "解码预算不足" in capsys.readouterr().out


def test_warn_mode_never_changes_anything(monkeypatch):
    _install(monkeypatch, avail_gb=3.0, rss_values=[58.0])
    called = {"gc": 0, "trim": 0}
    import gc
    monkeypatch.setattr(gc, "collect", lambda: called.__setitem__("gc", 1))
    monkeypatch.setattr(perf, "malloc_trim", lambda level=0: called.__setitem__("trim", 1))

    vae = FakeVAE((1, 3, 240, 1080, 1920))
    out = perf.decode_ram_check(vae, FakeLatent((1,)), mode="warn")
    assert out["level"] == "short"
    assert out["action"] == "none"          # 只报不改
    assert called == {"gc": 0, "trim": 0}
    assert "建议" in out["message"]


def test_tight_but_ok_stays_put(monkeypatch):
    """需求 6.4GB、可用 7.0GB：理论够但余量 <1GB → level=tight，仍然不动手。"""
    _install(monkeypatch, avail_gb=7.0, rss_values=[30.0])
    called = {"trim": 0}
    monkeypatch.setattr(perf, "malloc_trim", lambda level=0: called.__setitem__("trim", 1))
    vae = FakeVAE((1, 3, 240, 1080, 1920))
    out = perf.decode_ram_check(vae, FakeLatent((1,)), mode="auto")
    assert out["level"] == "tight"
    assert out["action"] == "none"
    assert called == {"trim": 0}


# ── 6) 量不到就降级跳过，绝不拦路 ──

def test_missing_avail_is_skipped(monkeypatch):
    monkeypatch.setattr(perf, "probe_ram_swap", lambda: (None, None))
    monkeypatch.setattr(perf, "rss_gb", lambda: None)
    vae = FakeVAE((1, 3, 240, 1080, 1920))
    out = perf.decode_ram_check(vae, FakeLatent((1,)), mode="auto")
    assert out["action"] == "skipped"
    assert out["level"] == "ok"


def test_malloc_trim_on_windows_is_noop(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    assert perf.malloc_trim() is None


def test_malloc_trim_never_raises(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")

    def _boom(*a, **k):
        raise RuntimeError("no libc")
    import builtins
    monkeypatch.setattr(builtins, "__import__", _boom)
    assert perf.malloc_trim() is None
