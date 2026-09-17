"""二采显存埋点与预检口径单测（upscale 的 _vram_probe / _vram_report / _vram_gb）。

背景：本机 `nvidia-smi` 不可用（NVML 错误），显存实测只能靠 torch 计数器；
而这几个函数此前**零测试覆盖**（全仓只有 test_upscale_model_tag.py 碰 upscale，
且只测纯函数 model_tag）。改动前先把行为钉住。

无 ComfyUI 依赖：`_vram_report` 是纯函数；`_vram_probe` / `_vram_gb` 在无 CUDA /
无 comfy 时都有降级分支，纯 Python 下也能跑。

跑法（仓库根目录）：
    python -m pytest tests/test_upscale_vram_probe.py -q
"""

import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:  # 优先用真实 torch（ComfyUI 环境）
    import torch as _torch
    _REAL_TORCH = True
except Exception:  # pragma: no cover - 无 torch 时走 stub
    _torch = None
    _REAL_TORCH = False


class _StubCuda(object):
    """torch.cuda 最小替身：模拟「无 CUDA」的保守分支。"""

    @staticmethod
    def is_available():
        return False


def _stub_torch():
    """无 torch 时的最小替身（upscale 顶层只用到 torch.Tensor 的 isinstance）。"""
    m = types.ModuleType("torch")
    m.Tensor = type("_StubTensor", (), {})
    m.cuda = _StubCuda()
    return m


def _load_upscale():
    """以独立包名加载 upscale.py（绕开插件 __init__ 的节点注册）。"""
    pkg_name = "h3sf_upscale_vram"
    if pkg_name in sys.modules:
        return sys.modules[pkg_name + ".upscale"]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [ROOT]
    sys.modules[pkg_name] = pkg
    for name in ("checkpoint", "grid"):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{name}", os.path.join(ROOT, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{name}"] = mod
        spec.loader.exec_module(mod)
    spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.upscale", os.path.join(ROOT, "upscale.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg_name}.upscale"] = mod
    if _REAL_TORCH:
        spec.loader.exec_module(mod)
        return mod
    # 无 torch：只在加载期间挂替身，加载完立刻摘掉——绝不能污染 sys.modules
    _prev = sys.modules.get("torch")
    sys.modules["torch"] = _stub_torch()
    try:
        spec.loader.exec_module(mod)
    finally:
        if _prev is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = _prev
    return mod


upscale = _load_upscale()


# ---- _vram_report：汇总行 ----

def test_vram_report_empty_returns_blank():
    """无数据（或未采到「放大后」）时不打印——调用方据此跳过，不产生噪声行。"""
    assert upscale._vram_report(1, None) == ""
    assert upscale._vram_report(1, {}) == ""
    assert upscale._vram_report(1, {"base": (1.0, 1.0)}) == ""


def test_vram_report_full_contains_six_stages():
    """六个节点全部采到时，一行里六个标签齐备且数值格式化为两位小数。"""
    v = {
        "base": (1.5, 1.6),
        "up": (None, 2.25),
        "cond": (None, 3.5),
        "unload": (0.125, 3.5),
        "refine": (None, 4.0),
        "decode": (None, 5.75),
    }
    line = upscale._vram_report(7, v)
    assert line.startswith("[H3二采] 段7 显存峰值：")
    for label in ("放大前", "放大后", "cond后", "卸载后", "精化后", "解码后"):
        assert label in line
    # 放大前 / 卸载后 报「当时占用」，其余报「阶段峰值」
    assert "放大前1.50GB" in line
    assert "卸载后0.12GB" in line
    assert "放大后2.25GB" in line
    assert "解码后5.75GB" in line


def test_vram_report_missing_stage_is_na():
    """中途异常导致某节点没采到时给 n/a，不抛异常（埋点绝不影响主流程）。"""
    v = {"up": (None, 2.0), "decode": (None, 2.0)}
    line = upscale._vram_report(3, v)
    assert "cond后n/a" in line
    assert "精化后n/a" in line


def test_vram_report_handles_none_snapshot():
    """探针在无 CUDA 时返回 (None, None)，汇总行须退化为 n/a 而非崩溃。"""
    v = {"base": (None, None), "up": (None, None)}
    assert "n/a" in upscale._vram_report(1, v)


# ---- _vram_probe / _vram_probe_reset：探针本身 ----

def test_vram_probe_returns_pair_without_raising():
    """探针永远返回二元组：有 CUDA 时是浮点，无 CUDA / 异常时是 (None, None)。"""
    a, p = upscale._vram_probe()
    assert (a is None) == (p is None)
    if a is not None:
        assert a >= 0.0 and p >= a - 1e-6


def test_vram_probe_reset_never_raises():
    """重置峰值统计是纯副作用调用，任何环境都不该抛（埋点不能成为故障源）。"""
    assert upscale._vram_probe_reset() is None


# ---- _vram_gb：空闲显存口径 ----

def test_vram_gb_returns_float():
    """无 comfy 环境下走 torch 回退分支，返回非负 float（拿不到就 0.0）。"""
    v = upscale._vram_gb()
    assert isinstance(v, float)
    assert v >= 0.0


# ---- P0-1 / P0-2：前向不建图的回归守卫（源码级，与 test_rework_v2 同风格） ----

def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def test_upscale_video_forward_is_wrapped_in_no_grad():
    """P0-1：放大前向必须包在 no_grad 里。

    不包 = 每次前向都建 autograd 图、保存全部中间激活，而该图会被扣押到
    「精化采样结束」才释放（up_v 一路交给采样器），与 UNET + 精化激活正面相撞。
    上游 3d.py:584 本来有 inference_mode，是移植时丢的。
    """
    src = _read("upscale.py")
    body = src.split("def upscale_video(", 1)[1].split("\ndef ", 1)[0]
    assert "with torch.no_grad():" in body, "放大前向必须包 no_grad"
    # 反归一化也必须在块内，否则 y 又被挂回计算图
    assert "return y.to(torch.float32) * std32 + mean32" in body
    assert body.index("with torch.no_grad():") < body.index(
        "return y.to(torch.float32) * std32 + mean32")


def test_upscale_video_uses_no_grad_not_inference_mode():
    """用 no_grad 而非 inference_mode：后者产生的 inference tensor 出了块再做
    原地操作会报错，而 up_v 要交给采样器（风险不可控）。"""
    src = _read("upscale.py")
    body = src.split("def upscale_video(", 1)[1].split("\ndef ", 1)[0]
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
    assert "inference_mode" not in code, "不要用 inference_mode（注释除外）"


def test_upscale_net_load_sets_requires_grad_false():
    """P0-2：加载放大网络时必须 requires_grad_(False)，与上游 3d.py:440 对齐。"""
    src = _read("upscale_net.py")
    assert ".eval().requires_grad_(False)" in src, \
        "放大网络权重必须关梯度（否则前向必建图）"
