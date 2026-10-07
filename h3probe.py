"""H3 显存探针 —— 只读、零行为改动。

**它是干什么的**：回答「解码要显存的那一刻，那十几 GB 到底在谁手里，为什么腾不出来」。

在关键动作发生的瞬间把显存账打出来：设备级占用、torch 分配器、aimdo 配额、
每个驻留模型的 `loaded_size`、VBAR 页统计（resident / pinned）、pin 状态，
以及每次腾挪的「请求多少 → 实得多少」。

**开关**（环境变量 `H3PROBE`，在启动 bat 里 `set H3PROBE=...`）：

| 值 | 行为 |
| --- | --- |
| 不设 / `auto` | **默认**。装钩子；解码前后打完整快照；腾挪只在「可疑」时出声（见下） |
| `1` | 全量。连 `load_models_gpu` / `vbar.unpin` 这类高频调用也逐条打 |
| `0` / `off` | 完全关闭，不挂任何钩子 |

「可疑」的判据（默认模式下 `free_memory` 只在命中任一条时出声）：
`for_dynamic=True` · 要求释放 ≥0.5GB · **要求释放 >0 却实释放 ≈0** · 真卸掉了模型。

**为什么默认开着**：它只读、不卸载、不腾挪、不改参数，跑出来的结果与没装它时一致；
而「解码时谁占着显存」这个问题，事后从任何日志都推不出来。要发给普通用户前，
把默认改成关闭即可（见文件末尾 `_DEFAULT_MODE`）。

**卸载**：`H3PROBE=0` 或删掉本文件 + 摘掉 `__init__.py` 里的两行调用。
"""

from __future__ import annotations

import os

_PREFIX = "[H3PROBE]"
# 发给普通用户前把这里改成 "0"，默认就变成「不挂钩子」。
_DEFAULT_MODE = "auto"
# 日志预算：避免长链刷屏。设 0 = 不限。
_BUDGET = int(os.environ.get("H3PROBE_BUDGET", "2000"))

_MODE = str(os.environ.get("H3PROBE", _DEFAULT_MODE) or _DEFAULT_MODE).strip().lower()
_OFF = _MODE in ("0", "off", "false", "no", "none", "disable", "disabled")
_VERBOSE = _MODE in ("1", "on", "true", "yes", "verbose", "all")

_seen = {"n": 0}
_installed = False


# ---------------------------------------------------------------- 工具


def _gb(n):
    try:
        return float(n) / (1024.0 ** 3)
    except Exception:
        return float("nan")


def _size_txt(n):
    """把字节数渲染成人话；1e30 这类哨兵值说成「全部」。"""
    try:
        if float(n) >= 1e29:
            return "全部"
    except Exception:
        pass
    return f"{_gb(n):.2f}GB"


def _say(msg):
    if _BUDGET and _seen["n"] >= _BUDGET:
        return
    _seen["n"] += 1
    try:
        print(f"{_PREFIX} {msg}", flush=True)
    except Exception:
        pass


def _used_gb():
    try:
        import torch
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            return _gb(total - free)
    except Exception:
        pass
    return float("nan")


def _notable_free(req, freed, unloaded, for_dynamic):
    """默认模式下 `free_memory` 值不值得说一句。"""
    if _VERBOSE:
        return True
    if for_dynamic:
        return True
    if unloaded:
        return True
    try:
        if float(req) >= 0.5 * (1024 ** 3):
            return True
        # 要了内存却一点没释放 —— 责任真空的签名
        if float(req) > 0 and abs(float(freed)) < 1e-6:
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- 快照


def _device_line():
    try:
        import torch
        if not torch.cuda.is_available():
            return "cuda 不可用"
        free, total = torch.cuda.mem_get_info()
        return (f"设备 {_gb(total - free):.2f}/{_gb(total):.2f}GB 已用"
                f" · CUDA 报空闲 {_gb(free):.2f}GB"
                f" · torch allocated {_gb(torch.cuda.memory_allocated()):.2f}GB"
                f" reserved {_gb(torch.cuda.memory_reserved()):.2f}GB")
    except Exception as e:
        return f"设备读数失败：{e}"


def _aimdo_line():
    try:
        import comfy_aimdo.control as ac
        return f"aimdo simple headroom {_gb(ac.get_simple_vram_headroom()):.2f}GB"
    except Exception:
        return "aimdo 不可读"


def _vbar_line(patcher):
    """VBAR 页统计 —— 判断「权重页是 resident 还是 pinned」的唯一直接依据。"""
    try:
        inner = getattr(patcher, "model", None)
        vbars = getattr(inner, "dynamic_vbars", None)
        if not isinstance(vbars, dict) or not vbars:
            return "vbar=无"
        parts = []
        for dev, vb in list(vbars.items()):
            try:
                ls = int(vb.loaded_size())
                pages = int(vb.get_nr_pages())
                res = vb.get_residency()
                resident = sum(1 for b in res if b & 1)
                pinned = sum(1 for b in res if b & 2)
                try:
                    wm = f"{_gb(int(vb.get_watermark())):.2f}GB"
                except Exception:
                    wm = "?"
                parts.append(f"vbar({dev}) loaded={_gb(ls):.2f}GB pages={pages} "
                             f"resident={resident} pinned={pinned} watermark={wm}")
            except Exception as e:
                parts.append(f"vbar({dev}) 读取失败：{e}")
        return " · ".join(parts)
    except Exception as e:
        return f"vbar 不可读：{e}"


def _pins_line(patcher):
    """`current_prompt=True` = 本 prompt 期间被钉住（PromptModelTracker 写的）。"""
    try:
        inner = getattr(patcher, "model", None)
        pins = getattr(inner, "dynamic_pins", None)
        if not isinstance(pins, dict) or not pins:
            return ""
        parts = []
        for dev, st in list(pins.items()):
            if not isinstance(st, dict):
                continue
            parts.append(f"pin({dev}) current_prompt={st.get('current_prompt')} "
                         f"active={st.get('active')} failed={st.get('failed')}")
        return " · ".join(parts)
    except Exception:
        return ""


def _models_lines():
    out = []
    try:
        import comfy.model_management as mm
    except Exception:
        return out
    for i, lm in enumerate(list(getattr(mm, "current_loaded_models", []) or [])):
        try:
            p = getattr(lm, "model", None)
            if p is None:
                continue
            inner = getattr(p, "model", None)
            name = type(inner).__name__ if inner is not None else type(p).__name__
            try:
                dyn = bool(p.is_dynamic())
            except Exception:
                dyn = False
            try:
                ls = _size_txt(int(p.loaded_size()))
            except Exception:
                ls = "?"
            try:
                mem = _size_txt(int(p.model_memory()))
            except Exception:
                mem = "?"
            try:
                off = _size_txt(int(p.model_offloaded_memory()))
            except Exception:
                off = "?"
            bits = [f"#{i} {name} dynamic={dyn} loaded={ls} mem={mem} "
                    f"offloaded={off} currently_used={getattr(lm, 'currently_used', None)}"]
            if dyn:
                bits.append(_vbar_line(p))
                pins = _pins_line(p)
                if pins:
                    bits.append(pins)
            out.append(" | ".join(b for b in bits if b))
        except Exception:
            continue
    return out


def snapshot(tag):
    """打一份完整显存账。可被其它模块主动调用。"""
    _say(f"===== {tag} =====")
    _say(f"  {_device_line()}")
    _say(f"  {_aimdo_line()}")
    try:
        import comfy.model_management as mm
        try:
            free_t, free_torch = mm.get_free_memory(mm.get_torch_device(), torch_free_too=True)
            _say(f"  comfy 口径：设备空闲 {_gb(free_t):.2f}GB · torch 可复用 {_gb(free_torch):.2f}GB")
        except Exception:
            pass
    except Exception:
        pass
    lines = _models_lines()
    if not lines:
        _say("  驻留模型：无")
    for ln in lines:
        _say(f"  {ln}")


# ---------------------------------------------------------------- 挂钩


def _wrap_vae_decode():
    """覆盖整条 `comfy.sd.VAE` 继承链 —— 含 HyperVAE2x 这类自己重写 decode 的子类。"""
    try:
        import comfy.sd

        targets = []

        def walk(cls):
            targets.append(cls)
            try:
                for sub in cls.__subclasses__():
                    walk(sub)
            except Exception:
                pass

        walk(comfy.sd.VAE)
        names = []
        for cls in targets:
            fn = cls.__dict__.get("decode")
            if fn is None or not callable(fn) or getattr(fn, "_h3probe_wrapped", False):
                continue
            cls.decode = _make_decode_wrapper(fn, cls.__name__)
            names.append(cls.__name__)
        _say(f"已挂 VAE.decode 探针：{names or '（无可挂目标）'}")
    except Exception as e:
        _say(f"VAE.decode 探针安装失败：{e}")


def _wrap_model_management():
    try:
        import comfy.model_management as mm

        if getattr(mm, "_h3probe_patched", False):
            return
        mm._h3probe_patched = True

        _orig_free = mm.free_memory

        def free_memory(*a, **kw):
            before = _used_gb()
            out = _orig_free(*a, **kw)
            after = _used_gb()
            req = a[0] if a else kw.get("memory_required", 0)
            dev = a[1] if len(a) > 1 else kw.get("device")
            fdn = kw.get("for_dynamic", a[3] if len(a) > 3 else False)
            try:
                n = len(out)
            except Exception:
                n = "?"
            freed = before - after
            if _notable_free(req, freed, n if isinstance(n, int) else 1, fdn):
                _say(f"free_memory 要求释放 {_size_txt(req)} device={dev} for_dynamic={fdn}"
                     f" → 卸了 {n} 个 · 设备占用 {before:.2f}→{after:.2f}GB"
                     f"（实释放 {freed:.2f}GB）")
            return out

        mm.free_memory = free_memory

        _orig_unload = mm.unload_all_models

        def unload_all_models(*a, **kw):
            before = _used_gb()
            out = _orig_unload(*a, **kw)
            after = _used_gb()
            _say(f"unload_all_models：设备占用 {before:.2f}→{after:.2f}GB"
                 f"（实释放 {before - after:.2f}GB）")
            return out

        mm.unload_all_models = unload_all_models

        if _VERBOSE:
            _orig_load = mm.load_models_gpu

            def load_models_gpu(*a, **kw):
                models = a[0] if a else kw.get("models", [])
                try:
                    dyn = [bool(m.is_dynamic()) for m in models]
                except Exception:
                    dyn = "?"
                mr = kw.get("memory_required", a[1] if len(a) > 1 else 0)
                _say(f"load_models_gpu 模型数={len(models) if hasattr(models, '__len__') else '?'}"
                     f" is_dynamic={dyn} memory_required={_size_txt(mr)}")
                return _orig_load(*a, **kw)

            mm.load_models_gpu = load_models_gpu

        _say("已挂 free_memory / unload_all_models 探针" + ("（+load_models_gpu 全量）" if _VERBOSE else ""))
    except Exception as e:
        _say(f"模型管理探针安装失败：{e}")


def _wrap_dynamic_unload():
    """请求 vs 实得 —— 直接判「腾挪到底有没有真的还回内存」。"""
    try:
        from comfy.model_patcher import ModelPatcherDynamic

        if not getattr(ModelPatcherDynamic, "_h3probe_patched", False):
            ModelPatcherDynamic._h3probe_patched = True
            _orig_pu = ModelPatcherDynamic.partially_unload

            def partially_unload(self, device_to, memory_to_free=0, force_patch_weights=False):
                got = _orig_pu(self, device_to, memory_to_free, force_patch_weights)
                try:
                    nm = type(self.model).__name__
                except Exception:
                    nm = "?"
                try:
                    want = float(memory_to_free)
                except Exception:
                    want = 0.0
                if _VERBOSE or want >= 0.1 * (1024 ** 3) or (want > 0 and float(got) <= 0):
                    _say(f"partially_unload {nm} 请求 {_size_txt(memory_to_free)}"
                         f" → 实得 {_size_txt(got)}")
                return got

            ModelPatcherDynamic.partially_unload = partially_unload
            _say("已挂 partially_unload 探针")
    except Exception as e:
        _say(f"partially_unload 探针安装失败：{e}")


def _wrap_aimdo_vbar():
    try:
        from comfy_aimdo.model_vbar import ModelVBAR

        if getattr(ModelVBAR, "_h3probe_patched", False):
            return
        ModelVBAR._h3probe_patched = True

        _orig_vf = ModelVBAR.free_memory

        def vbar_free_memory(self, size_bytes):
            got = _orig_vf(self, size_bytes)
            try:
                want = float(size_bytes)
            except Exception:
                want = 0.0
            if _VERBOSE or want >= 0.1 * (1024 ** 3) or (want > 0 and float(got) <= 0):
                _say(f"vbar.free_memory 请求 {_size_txt(size_bytes)} → 实得 {_size_txt(got)}")
            return got

        ModelVBAR.free_memory = vbar_free_memory

        if _VERBOSE:
            _orig_unpin = ModelVBAR.unpin

            def vbar_unpin(self, alloc, size):
                _say(f"vbar.unpin 被调用（只标记可驱逐，不搬数据）size={_size_txt(size)}")
                return _orig_unpin(self, alloc, size)

            ModelVBAR.unpin = vbar_unpin

        _say("已挂 ModelVBAR.free_memory 探针" + ("（+unpin 全量）" if _VERBOSE else ""))
    except Exception as e:
        _say(f"ModelVBAR 探针安装失败：{e}")


def _make_decode_wrapper(fn, clsname):
    def decode(self, *a, **kw):
        snapshot(f"VAE.decode 入口 [{clsname}]")
        try:
            out = fn(self, *a, **kw)
        except BaseException as e:
            _say(f"VAE.decode 抛异常 [{clsname}]：{type(e).__name__}: {e}")
            snapshot(f"VAE.decode 失败现场 [{clsname}]")
            raise
        snapshot(f"VAE.decode 完成 [{clsname}]")
        return out

    decode._h3probe_wrapped = True
    try:
        decode.__name__ = getattr(fn, "__name__", "decode")
        decode.__doc__ = getattr(fn, "__doc__", None)
    except Exception:
        pass
    return decode


# ---------------------------------------------------------------- 入口


def install():
    """由插件 `__init__.py` 调用。**永不抛异常** —— 探针不能把 ComfyUI 带崩。"""
    global _installed
    if _installed:
        return False
    if _OFF:
        print(f"{_PREFIX} H3PROBE={_MODE} → 已禁用（不挂任何钩子）", flush=True)
        return False
    _installed = True
    try:
        _wrap_vae_decode()
        _wrap_model_management()
        _wrap_dynamic_unload()
        _wrap_aimdo_vbar()
        _say(f"探针就绪（模式={'全量' if _VERBOSE else '默认'} · 日志预算 {_BUDGET} 条）。"
             f"关掉它：启动前 set H3PROBE=0")
    except Exception as e:
        try:
            print(f"{_PREFIX} 安装异常（已忽略）：{e}", flush=True)
        except Exception:
            pass
    return True
