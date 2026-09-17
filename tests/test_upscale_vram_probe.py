"""二采显存埋点与预检口径单测（upscale 的 _vram_probe / _vram_report / _vram_gb /
_dynamic_vram_active），以及 P0-1/P0-2「前向不建图」的源码级回归守卫。

背景：本机 `nvidia-smi` 不可用（NVML 错误），显存实测只能靠 torch 计数器；
而这些路径此前**零测试覆盖**（全仓只有 test_upscale_model_tag.py 碰 upscale，
且只测纯函数 model_tag）。改动前先把行为钉住。

无 ComfyUI 依赖：`_vram_report` 是纯函数；`_vram_probe` / `_vram_gb` 在无 CUDA /
无 comfy 时都有降级分支，纯 Python 下也能跑。

跑法（仓库根目录）：
    python -m pytest tests/test_upscale_vram_probe.py -q
"""

import contextlib
import importlib
import importlib.util
import os
import shutil
import sys
import tempfile
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
    # perf 必须一起预载：upscale 顶层 `from . import perf`（RSS 埋点），
    # 而 perf 零依赖可独立加载
    for name in ("checkpoint", "grid", "perf"):
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


@contextlib.contextmanager
def _fake_comfy(**modules):
    """在临时目录里造一个**真实可导入**的 `comfy` 包，临时挂到 sys.path 最前。

    为什么不直接往 `sys.modules` 塞 ModuleType：`import comfy.x as y` 在 CPython
    里走 C 层导入，光有 sys.modules 条目不够——父包没有真实 `__path__` / `__spec__`
    时仍抛 `ModuleNotFoundError`（三种造法都实测失败），必须让导入器真能按路径
    找到文件。

    `modules` 是 {子模块名: 源码}，例如
    `_fake_comfy(model_management="def get_free_memory(): ...")` 会生成
    `comfy/model_management.py`。

    ⚠ 注意 upscale 里两个名字是**不同**的真实模块，别搞混：
    - 空闲显存口径 → `comfy.model_management`（`_vram_gb`）
    - DynamicVRAM 真开关 → `comfy.memory_management`（`_dynamic_vram_active`）

    退出时移除 sys.path 条目、清掉本次产生的 `comfy*` 缓存并还原原有条目、
    删临时目录——绝不污染同会话。
    """
    tmp = tempfile.mkdtemp(prefix="h3sf_fake_comfy_")
    pkg = os.path.join(tmp, "comfy")
    os.makedirs(pkg)
    with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    for mod_name, source in modules.items():
        with open(os.path.join(pkg, f"{mod_name}.py"), "w", encoding="utf-8") as f:
            f.write(source)
    saved = {k: sys.modules[k] for k in list(sys.modules)
             if k == "comfy" or k.startswith("comfy.")}
    for k in saved:
        del sys.modules[k]
    sys.path.insert(0, tmp)
    try:
        yield {n: importlib.import_module(f"comfy.{n}") for n in modules}
    finally:
        try:
            sys.path.remove(tmp)
        except ValueError:
            pass
        for k in [k for k in list(sys.modules)
                  if k == "comfy" or k.startswith("comfy.")]:
            del sys.modules[k]
        sys.modules.update(saved)
        shutil.rmtree(tmp, ignore_errors=True)


# 空闲显存替身：mm 口径给 2GB，用来验证「没走这条」或「回退到这条」
_MODEL_MM_2GB = (
    "def get_torch_device():\n"
    "    return 'cuda:0'\n"
    "\n"
    "\n"
    "def get_free_memory(device=None):\n"
    "    return 2.0 * (1024 ** 3)\n"
)


def _mm_aimdo_flag(value):
    """生成 `comfy/memory_management.py` 源码：只放 DynamicVRAM 真开关。"""
    return f"aimdo_enabled = {bool(value)}\n"


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


def test_rss_report_full_contains_six_stages():
    """RSS 行与显存行同构：六个节点齐备、两位小数、缺项 n/a。"""
    r = {"base": 1.2, "up": 3.4, "cond": 3.6,
         "unload": 3.6, "refine": 4.1, "decode": 4.8}
    line = upscale._rss_report(7, r)
    assert line.startswith("[H3二采] 段7 内存RSS：")
    for label in ("放大前", "放大后", "cond后", "卸载后", "精化后", "解码后"):
        assert label in line
    assert "放大前1.20" in line and "解码后4.80" in line


def test_rss_report_empty_returns_blank():
    """无数据 / 未采到「放大后」时不打印（与显存行同规则：调用方据此跳过）。"""
    assert upscale._rss_report(1, None) == ""
    assert upscale._rss_report(1, {}) == ""
    assert upscale._rss_report(1, {"base": 1.0}) == ""


def test_rss_report_missing_stage_is_na():
    assert "精化后n/a" in upscale._rss_report(1, {"up": 2.0})


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


def test_vram_gb_prefers_patcher_caliber():
    """给了 ModelPatcher 时优先用 `patcher.get_free_memory(device)` 口径。

    DynamicVRAM 下 `mm.get_free_memory()` 不含 aimdo 可换出的权重页（严重低估、
    常趋近 0），只有 patcher 口径才是「真实可动用」——官方解码路径
    （comfy/sd.py:1242）用的就是它。这里把 mm 口径故意设成 0，用来证明没走它。
    """
    calls = {}

    class _FakePatcher(object):
        def get_free_memory(self, dev):
            calls["dev"] = dev
            return 3.5 * (1024 ** 3)

    src = ("def get_torch_device():\n    return 'cuda:0'\n"
           "\n\n"
           "def get_free_memory(device=None):\n    return 0.0\n")
    with _fake_comfy(model_management=src):
        got = upscale._vram_gb(_FakePatcher())
    assert got == pytest.approx(3.5)
    assert calls.get("dev") == "cuda:0"


def test_vram_gb_falls_back_when_patcher_lacks_method():
    """patcher 没有 get_free_memory（老版本 / 非 ModelPatcher）时静默回退到 mm。"""
    class _Bare(object):
        pass

    with _fake_comfy(model_management=_MODEL_MM_2GB):
        got = upscale._vram_gb(_Bare())
    assert got == pytest.approx(2.0)


def test_vram_gb_patcher_raises_is_swallowed():
    """patcher 口径内部抛异常时退回 mm 口径，绝不把异常抛给预检。"""
    class _FakePatcher(object):
        def get_free_memory(self, dev):
            raise RuntimeError("vbar 不可用")

    with _fake_comfy(model_management=_MODEL_MM_2GB):
        got = upscale._vram_gb(_FakePatcher())
    assert got == pytest.approx(2.0)


def test_vram_gb_without_arg_uses_mm_caliber():
    """不传参时保持原口径（mm.get_free_memory）——老调用点行为不变。"""
    with _fake_comfy(model_management=_MODEL_MM_2GB):
        got = upscale._vram_gb()
    assert got == pytest.approx(2.0)


# ---- P0-3：DynamicVRAM 真开关 ----

def test_dynamic_vram_active_reads_real_switch():
    """必须读 `comfy.memory_management.aimdo_enabled`（真开关），开关两边都要跟。"""
    with _fake_comfy(memory_management=_mm_aimdo_flag(True)):
        assert upscale._dynamic_vram_active() is True
    with _fake_comfy(memory_management=_mm_aimdo_flag(False)):
        assert upscale._dynamic_vram_active() is False


def test_dynamic_vram_active_ignores_module_presence():
    """P0-3 回归守卫：`comfy_aimdo` 出现在 sys.modules 里 **不等于** 开关开着。

    旧实现查 `"aimdo" in sys.modules` / `find_spec("aimdo")`——而
    `comfy/model_management.py` 顶层就 import comfy_aimdo，于是它恒为 True、
    预检硬停分支永久失效。这里模拟「包已导入但开关是关的」，必须返回 False。
    """
    fake_aimdo = types.ModuleType("comfy_aimdo")
    prev = sys.modules.get("comfy_aimdo")
    sys.modules["comfy_aimdo"] = fake_aimdo
    try:
        with _fake_comfy(memory_management=_mm_aimdo_flag(False)):
            assert "comfy_aimdo" in sys.modules, "前提：包在场"
            assert upscale._dynamic_vram_active() is False, \
                "包在场但开关关闭时必须返回 False"
    finally:
        if prev is None:
            sys.modules.pop("comfy_aimdo", None)
        else:
            sys.modules["comfy_aimdo"] = prev


def test_dynamic_vram_active_missing_attribute_is_false():
    """老版本 comfy 没有该字段时保守返回 False（走非 DynamicVRAM 分支）。"""
    with _fake_comfy(memory_management="# 模拟老版本：没有 aimdo_enabled 字段\n"):
        assert upscale._dynamic_vram_active() is False


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
    assert body.index("with torch.no_grad():") < body.index(
        "return y.to(torch.float32) * std32 + mean32"), \
        "反归一化也必须在 no_grad 块内，否则 y 又被挂回计算图"


def test_upscale_video_uses_no_grad_not_inference_mode():
    """用 no_grad 而非 inference_mode：后者产生的 inference tensor 出了块再做
    原地操作会报错，而 up_v 要交给采样器（风险不可控）。"""
    src = _read("upscale.py")
    body = src.split("def upscale_video(", 1)[1].split("\ndef ", 1)[0]
    code = "\n".join(ln for ln in body.splitlines()
                     if not ln.strip().startswith("#"))
    assert "inference_mode" not in code, "不要用 inference_mode（注释除外）"


def test_upscale_net_load_sets_requires_grad_false():
    """P0-2：加载放大网络时必须 requires_grad_(False)，与上游 3d.py:440 对齐。"""
    src = _read("upscale_net.py")
    assert ".eval().requires_grad_(False)" in src, \
        "放大网络权重必须关梯度（否则前向必建图）"


# ---- P1-2：解码前卸 UNET（仅 A≠B） ----

def test_render_segment_signature_accepts_up_swap():
    """render_segment 必须有 _up_swap 开关，且默认 False（保持旧行为可回滚）。"""
    src = _read("upscale.py")
    sig = src.split("def render_segment(", 1)[1].split("):", 1)[0]
    assert "_up_swap=False" in sig


def test_render_segment_unloads_unet_before_decode():
    """P1-2：卸载必须发生在 decode 之前，受 _up_swap 保护，且用精准卸载。

    用 unload_model_and_clones 而非 unload_all_models：前者按 clone_base_uuid
    只卸同源模型，解码要用的 VAE 会保留（不用立刻重载）。
    """
    src = _read("upscale.py")
    body = src.split("def render_segment(", 1)[1]
    i_unload = body.index("unload_model_and_clones(模型)")
    i_decode = body.index("video_vae.decode(up_v)")
    assert i_unload < i_decode, "卸载必须在解码之前"
    assert "if _up_swap:" in body[:i_unload], "必须受 _up_swap 条件保护"
    seg = body[i_unload:i_decode]
    assert seg.index("gc.collect()") < seg.index("torch.cuda.empty_cache()"), \
        "顺序必须是 unload → gc → empty_cache（反序收不回）"


def test_nodes_passes_up_swap_to_render_segment():
    """nodes.py 主循环必须把 _up_swap 传下去，否则开关恒为 False（等于没生效）。"""
    src = _read("nodes.py")
    assert "_up_swap=_up_swap)" in src, \
        "nodes.py 必须把 _up_swap 传给 render_segment"


# ---- 阶段 0：埋点容器必须真的接得上 ----

def test_render_latent_accepts_vram_and_rss_containers():
    """回归守卫：render_latent 必须有 `_vram` / `_rss` 形参。

    2026-09-17 实测到的真 bug：render_segment 一直在传 `_vram=_vram`，而
    render_latent 的签名里**漏了这两个形参** —— 每次二采都会在调用处
    TypeError。埋点此前从没在真机跑通过，所以一直没暴露。
    """
    src = _read("upscale.py")
    sig = src.split("def render_latent(", 1)[1].split("):", 1)[0]
    assert "_vram=None" in sig, "render_latent 必须接收 _vram 容器"
    assert "_rss=None" in sig, "render_latent 必须接收 _rss 容器"


def test_render_segment_hands_both_containers_to_render_latent():
    """两个容器都建了、也都得传进去（建了不传 = 埋点永远是空 dict）。"""
    src = _read("upscale.py")
    body = src.split("def render_segment(", 1)[1]
    assert "_vram = {}" in body and "_rss = {}" in body
    assert "_vram=_vram" in body and "_rss=_rss" in body


def test_six_marks_are_all_collected():
    """六段埋点必须齐全（此前只有 4/6，cond / unload 缺失）。

    显存与 RSS 由同一个 `_mark()` 一次采齐：分开采会漏（两个函数得各调一次），
    而且**逐点落盘**也只能发生在一个地方。
    """
    src = _read("upscale.py")
    body = src.split("def render_latent(", 1)[1]
    for key in ("base", "up", "cond", "unload", "refine"):
        assert f'_mark("{key}"' in body, f"缺埋点 {key}"
    # decode 在 render_segment 里采（render_latent 返回后才解码）
    assert '_vram["decode"] = _vram_probe()' in src
    assert '_rss["decode"] = _rss_probe()' in src


def test_marks_emit_to_durable_log():
    """崩溃安全守卫：每个埋点必须 **逐点落盘**，不能只堆到段末统一打印。

    云端 OOM 的下场是 SIGKILL —— 没有 except / finally，段末汇总一定丢；
    而最需要看数据的恰恰是崩掉的那一段。
    """
    src = _read("upscale.py")
    body = src.split("def _mark(key, label):", 1)[1].split("\n    def ", 1)[0]
    assert "perf.emit(" in body, "_mark 必须逐点 emit（落盘）"
    assert 'print(' in body and "flush=True" in body, "并立即 flush 打印"


def test_render_segment_dumps_partial_marks_on_crash():
    """中断时要把**已采到的**埋点吐出来，不能等段末（那时已经没机会了）。"""
    src = _read("upscale.py")
    body = src.split("def render_segment(", 1)[1]
    i_try = body.index("def _dump_partial(")
    # _dump_partial 定义在 render_segment 内，且必须挂在 render_latent 的异常路径上
    assert "except BaseException:" in body[i_try:], "render_latent 必须有异常兜底"
    i_except = body.index("except BaseException:", i_try)
    assert "_dump_partial(" in body[i_except:i_except + 200]
    assert "raise" in body[i_except:i_except + 200], "兜底后必须原样上抛（不改变语义）"
