"""H3 显存探针 —— 只读、零行为改动。

**它是干什么的**：回答「解码要显存的那一刻，那十几 GB 到底在谁手里，为什么腾不出来」。

在关键动作发生的瞬间把显存账打出来：设备级占用、torch 分配器、aimdo 配额、
每个驻留模型的 `loaded_size`、VBAR 页统计（resident / pinned）、pin 状态，
以及每次腾挪的「要多少 → 真还回多少」。

**怎么读**（最容易读错的一行）：

```
free_memory 需要 11.53GB 可用 device=cuda:0 for_dynamic=True → 卸了 0 个 · 设备占用 21.25→21.25GB（实释放 0.00GB · 占用 90%）
```

- `需要 X 可用` —— X 是上游 `memory_required`（**本次调用要多少可用显存**），
  **不是「要腾出 X」**。要腾多少是上游自己算的：`memory_to_free = memory_required - 当前空闲`。
  旧文案写「要求释放 X」正是误读的来源，已改。
- `卸了 N 个` —— **真正被卸载**的模型数。`for_dynamic=True` 时上游按设计
  「不为别的动态模型去卸动态模型」，动态模型一律不卸，所以 N=0 是常态，不是失败。
- `实释放 X` —— 设备占用的实际下降量。它可以是 0（什么也没发生），
  也可以非 0 而 N=0 —— 那是 `soft_empty_cache()` 把 torch 分配器缓存还给了 CUDA，
  **不是**权重被换出。
- `占用 P%` —— 打印时的设备占用率，也是默认模式决定「这条要不要说」的依据。

**开关**（环境变量 `H3PROBE`，在启动 bat 里 `set H3PROBE=...`）：

| 值 | 行为 |
| --- | --- |
| 不设 / `auto` | **默认**。装钩子；解码前后打完整快照；腾挪只在「有事发生」时出声（见下） |
| `1` | 全量。连 `load_models_gpu` / `vbar.unpin` 这类高频调用也逐条打 |
| `0` / `off` | 完全关闭，不挂任何钩子 |

「有事发生」的判据（默认模式下 `free_memory` 只在命中任一条时出声）：

1. 真卸掉了模型（`N > 0`）；
2. 非动态路径（`for_dynamic=False`，即 `unload_all_models` / 显式卸载），且沿用原判据：
   要了大额度（≥0.5GB），或要了却一点没释放；
3. 动态路径，但**真还回了显存**（`实释放 > 0`）；
4. 动态路径、没还回显存，但**设备确实吃紧**（占用 ≥ `_PRESSURE_RATIO`，可用 `H3PROBE_PRESSURE` 调）。

为什么要有第 4 条、而不是旧的「`for_dynamic=True` 一律出声」：每次 `load_models_gpu`
都会调两次 `free_memory(for_dynamic=True)`，而「设备空着 + 要一个大额度 + 卸 0 个
+ 释放 0」是**例行问价** —— 长链下几百条、且不含任何信息。旧的「一律出声」会让
这种例行行淹掉真正可疑的行（2000 条预算会被吃光）。

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
# 默认模式判「设备吃紧」的占用率阈值 —— 只有到这个比例，例行腾挪才值得出声。
# 0.85 取自项目自己的「余量策略」告警口径（0.35 起显存会被吃到接近 100%）。
_PRESSURE_RATIO = float(os.environ.get("H3PROBE_PRESSURE", "0.85"))

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


def _num(n):
    """GB 读数渲染；NaN → `?`。读数失败不许冒充「0」。"""
    try:
        v = float(n)
    except Exception:
        return "?"
    return "?" if v != v else f"{v:.2f}"


def _size_txt(n):
    """把字节数渲染成人话；1e30 这类哨兵值说成「全部」。"""
    try:
        if float(n) >= 1e29:
            return "全部"
    except Exception:
        pass
    return f"{_num(_gb(n))}GB"


def _need_txt(n):
    """`free_memory` 第一个参数的读法 —— 它是「需要多少可用」，不是「要腾出多少」。"""
    try:
        if float(n) >= 1e29:
            return "全部显存"
    except Exception:
        pass
    return f"{_num(_gb(n))}GB 可用"


def _pct_txt(used_gb, total_gb):
    """占用率后缀；读数不全 → 空串（宁可不写，也不写一个错的百分比）。"""
    try:
        t = float(total_gb)
        if t > 0:
            return f" · 占用 {float(used_gb) / t * 100:.0f}%"
    except Exception:
        pass
    return ""


def _say(msg):
    if _BUDGET and _seen["n"] >= _BUDGET:
        return
    _seen["n"] += 1
    try:
        print(f"{_PREFIX} {msg}", flush=True)
    except Exception:
        pass


def _mem_pair():
    """(已用 GB, 总量 GB)；拿不到 → (nan, nan)。占用率与释放量共用一次读数。"""
    try:
        import torch
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            return _gb(total - free), _gb(total)
    except Exception:
        pass
    return float("nan"), float("nan")


def _used_gb():
    return _mem_pair()[0]


def _notable_free(req, freed, unloaded, for_dynamic, used_gb=None, total_gb=None):
    """默认模式下 `free_memory` 值不值得说一句。判据见模块 docstring。

    设计目标：默认模式只报「有事发生」的腾挪，把例行问价压掉。
    例行问价长这样：设备基本空着、要一个很大的额度、结果卸 0 个、实释放 0 ——
    每次 `load_models_gpu` 都会产生两条，长链下几百条且不含任何信息。

    只收紧 **动态路径**：非动态路径（`unload_all_models` 等显式卸载）本来就少，
    且 `unload_all_models` 是「卸了 4 个 / 释放 15.77GB」那条关键行，绝不能被压掉。
    """
    if _VERBOSE:
        return True
    # ① 真卸掉了模型 —— 一定有信息量
    if unloaded:
        return True

    if not for_dynamic:
        # ② 非动态路径：沿用原判据（要了大额度，或要了却一点没释放 = 责任真空的签名）
        try:
            if float(req) >= 0.5 * (1024 ** 3):
                return True
            if float(req) > 0 and abs(float(freed)) < 1e-6:
                return True
        except Exception:
            return True  # 读数坏了 → 宁可说
        return False

    # ③ 动态路径但真还回了显存 —— 说明有隐性占用（torch 分配器缓存）被清掉
    try:
        if abs(float(freed)) >= 1e-6:
            return True
    except Exception:
        pass
    # ④ 动态路径、没还回，但设备确实吃紧 —— 这才是「腾不出来」的现场。
    #    读数缺失时不猜、不出声（否则退化成旧版的刷屏）。
    try:
        if used_gb is not None and total_gb and float(total_gb) > 0:
            return float(used_gb) >= float(total_gb) * _PRESSURE_RATIO
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
        return (f"设备 {_num(_gb(total - free))}/{_num(_gb(total))}GB 已用"
                f" · CUDA 报空闲 {_num(_gb(free))}GB"
                f" · torch allocated {_num(_gb(torch.cuda.memory_allocated()))}GB"
                f" reserved {_num(_gb(torch.cuda.memory_reserved()))}GB")
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
            _say(f"  comfy 口径：设备空闲 {_num(_gb(free_t))}GB · torch 可复用 {_num(_gb(free_torch))}GB")
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
        _say(f"已挂 VAE.decode 探针：{names or '（无可挂目标）'}"
             f"（tag 用运行时类名 —— 子类实例会显示自己的名字，不再一律是 [VAE]）")
    except Exception as e:
        _say(f"VAE.decode 探针安装失败（多半是 import comfy.sd 没成功）：{e}")


def _wrap_model_management():
    try:
        import comfy.model_management as mm

        if getattr(mm, "_h3probe_patched", False):
            return
        mm._h3probe_patched = True

        _orig_free = mm.free_memory

        def free_memory(*a, **kw):
            before, total = _mem_pair()
            out = _orig_free(*a, **kw)
            after, _ = _mem_pair()
            req = a[0] if a else kw.get("memory_required", 0)
            dev = a[1] if len(a) > 1 else kw.get("device")
            fdn = kw.get("for_dynamic", a[3] if len(a) > 3 else False)
            try:
                n = len(out)
            except Exception:
                n = "?"
            freed = before - after
            if freed != freed:  # NaN：读数失败时不冒充「释放了 0」
                freed = float("nan")
            if _notable_free(req, freed, n if isinstance(n, int) else 1, fdn,
                             used_gb=before, total_gb=total):
                _say(f"free_memory 需要 {_need_txt(req)} device={dev}"
                     f" for_dynamic={fdn} → 卸了 {n} 个 · 设备占用"
                     f" {_num(before)}→{_num(after)}GB"
                     f"（实释放 {_num(freed)}GB{_pct_txt(before, total)}）")
            return out

        mm.free_memory = free_memory

        _orig_unload = mm.unload_all_models

        def unload_all_models(*a, **kw):
            before, total = _mem_pair()
            out = _orig_unload(*a, **kw)
            after, _ = _mem_pair()
            _say(f"unload_all_models：设备占用 {_num(before)}→{_num(after)}GB"
                 f"（实释放 {_num(before - after)}GB{_pct_txt(before, total)}）")
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
    """请求 vs 实释放 —— 直接判「腾挪到底有没有真的还回内存」。

    这里的「请求」是**真的要腾出的字节数**（上游传进来的 `memory_to_free`），
    与 `free_memory` 第一参数（需要多少可用）不是一回事，别混读。
    """
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
                         f" → 实释放 {_size_txt(got)}")
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
                _say(f"vbar.free_memory 请求 {_size_txt(size_bytes)} → 实释放 {_size_txt(got)}")
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


def _runtime_name(obj, fallback):
    """运行时类名 —— tag 要的是「这次真的在跑哪个 VAE」，不是包装时猜的那个。

    包装时只能拿到「谁定义了 `decode`」（通常是基类 `VAE`），所以旧日志里
    video / audio 两个 VAE 的 tag 都是 `[VAE]`，只能靠中间的
    `Requested to load MiniMaxH3VideoVAE/AudioVAE` 反推。改成运行时取类名后，
    一行就能看出是哪一个。
    """
    try:
        return type(obj).__name__ or fallback
    except Exception:
        return fallback


def _make_decode_wrapper(fn, clsname):
    def decode(self, *a, **kw):
        tag = _runtime_name(self, clsname)
        snapshot(f"VAE.decode 入口 [{tag}]")
        try:
            out = fn(self, *a, **kw)
        except BaseException as e:
            _say(f"VAE.decode 抛异常 [{tag}]：{type(e).__name__}: {e}")
            snapshot(f"VAE.decode 失败现场 [{tag}]")
            raise
        snapshot(f"VAE.decode 完成 [{tag}]")
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
        _say("读法：`需要 X 可用` 的 X 是本次要多少可用显存（上游 memory_required），"
             "不是「要腾出 X」；`卸了 N 个` 才是真卸的模型数——"
             "for_dynamic=True 时上游按设计不卸动态模型，N=0 是常态。")
    except Exception as e:
        try:
            print(f"{_PREFIX} 安装异常（已忽略）：{e}", flush=True)
        except Exception:
            pass
    return True
