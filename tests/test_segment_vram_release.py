"""段间显存回收的契约测试（2026-10-08「多段链卡住不动」）。

背景（实测更正了「显存池被占满」的旧判断）：
导演台的多段链是**一次**节点执行里跑完 N 段的，而 ComfyUI 的执行级清理
（`cleanup_prefetch_queues` / `reset_cast_buffers` / `vbars_reset_watermark_limits`）
挂在「节点执行结束」这个点上 → **段间从不发生**。手动逐段跑则每段各是一次执行、
每段都清一次 —— 这就是「手动逐段 + 点一下清理就正常」的真正机制。

实测数据（`output/h3_mem_probe/run3seg_big_trace.csv`，0.5MP × 2s × 3 段）：
卡住那一轮第 4 步耗时 **597s**，而磁盘只读了 20.2GB（≈ UNET 全量），速率从
**225MB/s 塌到 34MB/s**（≈ 32MiB 一页/秒的抖动）；显存**始终留着 ~1GB 空闲**
（4.7–5.7GB 震荡），采样期 `torch_reserved` 只有 96–128MB（权重全在 aimdo VBAR 里）。
所以病根不是「显存被占满」，而是段间没重置的那批**不在任何模型账本上**的显存
（CUDA 图 / cast buffer / VBAR watermark）—— 其中 CUDA 图的显存池
**`torch.cuda.empty_cache()` 收不走**。

这里钉五件事：

1. `_comfy_exec_cleanup` 必须真的调那三个 ComfyUI 函数（少一步 = 手动跑少清一样）。
2. 解码前必须调它，且**在** `video_vae.decode` 之前（解码就是段内的显存压力峰值）。
3. 主段循环的**段末**必须调 `_segment_end_cleanup`（「跨段累积」那一半）。
4. `_reclaim_orphan_vram` 判据是「设备占用 − 账本占用 > 阈值」，回收走官方
   `soft_empty_cache()`（与 `/h3chain/vram_cleanup`「点一下」同一条路）。
5. `_log_reclaim` 在量不到 after / 释放不足时不出声，且**任何输入都不许抛**
   —— 它挂在解码路径上，抛了就是把一次能救的运行直接带崩。

跑法（仓库根目录）：
    python -m pytest tests/test_segment_vram_release.py -q
"""

import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NODES_PY = os.path.join(ROOT, "nodes.py")


@pytest.fixture(scope="module")
def nodes_src():
    return open(NODES_PY, encoding="utf-8").read()


@pytest.fixture(scope="module")
def nodes_tree(nodes_src):
    return ast.parse(nodes_src)


def _find_func(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _call_name(node):
    """Call 的被调名：`f()` → 'f'；`a.b()` → 'b'；否则 None。"""
    if not isinstance(node, ast.Call):
        return None
    return getattr(node.func, "id", None) or getattr(node.func, "attr", None)


def _lines_of_calls(node, name):
    return sorted(n.lineno for n in ast.walk(node)
                  if isinstance(n, ast.Call) and _call_name(n) == name)


# --------------------------------------------------- 1) 执行级清理的三个动作

def test_exec_cleanup_calls_the_three_comfy_hooks(nodes_src, nodes_tree):
    """三步一个都不能少 —— 它们分别对应 CUDA 图 / cast buffer / VBAR watermark。"""
    seg = ast.get_source_segment(nodes_src, _find_func(nodes_tree, "_comfy_exec_cleanup"))
    for call in ("cleanup_prefetch_queues()", "reset_cast_buffers()",
                 "vbars_reset_watermark_limits()"):
        assert call in seg, f"缺 `{call}` —— 少一步就等于手动逐段跑少清一样东西"


def test_exec_cleanup_is_fault_tolerant(nodes_src, nodes_tree):
    """三步必须各自 try/except：非 aimdo 环境缺第三步不该让整段崩。"""
    fn = _find_func(nodes_tree, "_comfy_exec_cleanup")
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert len(handlers) >= 3, "三个动作应各自独立 try/except（缺一个不影响其余）"
    for t in handlers:
        assert t.handlers, "空 try 没有 except —— 那就不叫容错了"


# --------------------------------------------------- 2) 解码前必须清

def test_decode_rescue_runs_exec_cleanup_before_decode(nodes_src, nodes_tree):
    """清理必须在 `video_vae.decode` **之前** —— 等它卡住就已经晚了。"""
    fn = _find_func(nodes_tree, "_decode_rescue")
    clean = _lines_of_calls(fn, "_comfy_exec_cleanup")
    decode = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
              and _call_name(n) == "decode"
              and getattr(n.func.value, "id", "") == "video_vae"]
    assert clean, "_decode_rescue 里没有调 _comfy_exec_cleanup"
    assert decode, "找不到 video_vae.decode"
    assert min(clean) < min(decode), "执行级清理必须在 decode 之前"


def test_decode_rescue_reclaims_orphan_vram(nodes_src, nodes_tree):
    """解码前除了执行级清理，还要按「无主显存」判据再收一次。"""
    seg = ast.get_source_segment(nodes_src, _find_func(nodes_tree, "_decode_rescue"))
    assert "_reclaim_orphan_vram(" in seg


# --------------------------------------------------- 3) 段末必须清

def _main_segment_loop(tree):
    """主段循环 `for item_i, item in enumerate(exec_items):`。

    只认真正的 `ast.For`（推导式里的 `for item_i, it in ...` 是 `ast.comprehension`，
    不会命中）—— 避免把 `_off_slots = {...}` 那种推导式误当主循环。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        tgt = node.target
        if not isinstance(tgt, ast.Tuple) or len(tgt.elts) < 2:
            continue
        if (getattr(tgt.elts[0], "id", None) == "item_i"
                and getattr(tgt.elts[1], "id", None) == "item"):
            return node
    return None


def test_segment_loop_calls_end_cleanup_after_progress(nodes_src, nodes_tree):
    """段末清理 = 「跨段累积」那一半，也是自动跑与手动逐段跑的唯一差别。"""
    loop = _main_segment_loop(nodes_tree)
    assert loop is not None, "找不到主段循环 for item_i, item in enumerate(exec_items)"
    clean = _lines_of_calls(loop, "_segment_end_cleanup")
    prog = _lines_of_calls(loop, "update")     # pbar.update(1)
    assert clean, "主段循环里没有 _segment_end_cleanup —— 段间又会累积"
    assert prog, "找不到段末的 pbar.update(1)"
    assert min(clean) > max(prog), "段末清理必须在该段全部落盘之后（pbar.update 之后）"


def test_segment_end_cleanup_does_both_halves(nodes_src, nodes_tree):
    """段末 = 执行级清理 + 无主显存回收，两半都要。"""
    seg = ast.get_source_segment(nodes_src, _find_func(nodes_tree, "_segment_end_cleanup"))
    assert "_comfy_exec_cleanup()" in seg
    assert "_reclaim_orphan_vram(" in seg


# --------------------------------------------------- 4) 判据与回收口径

def test_reclaim_uses_official_soft_empty_cache(nodes_src, nodes_tree):
    """回收走官方 `soft_empty_cache()` —— 与手动「点一下清理」同一条路。

    裸 `torch.cuda.empty_cache()` 既不是手动清理的口径，也收不走 CUDA 图的显存池。
    """
    seg = ast.get_source_segment(nodes_src, _find_func(nodes_tree, "_reclaim_orphan_vram"))
    assert "soft_empty_cache()" in seg, "应当调官方 soft_empty_cache()"
    assert "_vram_used_gb()" in seg and "_loaded_models_gb()" in seg


def test_reclaim_judges_device_minus_ledger(nodes_src, nodes_tree):
    """判据必须是「设备占用 − 账本占用 > 阈值」—— 两者都不含池，差值正好暴露池。"""
    fn = _find_func(nodes_tree, "_reclaim_orphan_vram")
    seg = ast.get_source_segment(nodes_src, fn)
    assert "orphan = used - loaded" in seg, "差值口径被改动了"
    assert "orphan <= threshold_gb" in seg, "阈值门控被拿掉了（会无条件同步+清缓存）"


def test_reclaim_does_not_unload_models(nodes_src, nodes_tree):
    """这条路径**不卸载模型**（模型真在用时卸载代价很大）；卸载是 OOM 自救那条路的活。"""
    seg = ast.get_source_segment(nodes_src, _find_func(nodes_tree, "_reclaim_orphan_vram"))
    assert "unload_all_models" not in seg


def test_loaded_models_gb_sums_the_ledger(nodes_src, nodes_tree):
    """账本口径 = 遍历 `current_loaded_models` 累加 `loaded_size()`（与探针同口径）。"""
    seg = ast.get_source_segment(nodes_src, _find_func(nodes_tree, "_loaded_models_gb"))
    assert "current_loaded_models" in seg
    assert "loaded_size()" in seg


# --------------------------------------------------- 5) 日志函数不许抛

def _load_log_reclaim(nodes_src, nodes_tree):
    """把 `_log_reclaim` 单独取出来执行（它只依赖内置 print / float，可独立跑）。"""
    src = ast.get_source_segment(nodes_src, _find_func(nodes_tree, "_log_reclaim"))
    ns = {}
    exec(compile(src, "<_log_reclaim>", "exec"), ns)
    return ns["_log_reclaim"]


def test_log_reclaim_silent_when_nothing_freed(nodes_src, nodes_tree, capsys):
    fn = _load_log_reclaim(nodes_src, nodes_tree)
    fn(None, "段1 段末")
    fn({"freed_gb": 0.1, "used_before_gb": 5.0, "used_after_gb": 4.9, "orphan_gb": 5.0}, "x")
    fn({"freed_gb": None, "used_before_gb": 5.0, "used_after_gb": None, "orphan_gb": 5.0}, "x")
    fn({"freed_gb": "?"}, "x")
    assert capsys.readouterr().out == "", "没有实质释放就不该出声"


def test_log_reclaim_survives_unknown_after(nodes_src, nodes_tree, capsys):
    """`used_after_gb` 量不到时不许把 `None:.2f` 抛出来 —— 它在解码路径上。"""
    fn = _load_log_reclaim(nodes_src, nodes_tree)
    fn({"freed_gb": 1.5, "used_before_gb": 5.0, "used_after_gb": None, "orphan_gb": 5.0},
       "段1 段末")
    out = capsys.readouterr().out
    assert "5.00→?GB" in out, f"量不到 after 时应当降级成 `?`，实际：{out!r}"


def test_log_reclaim_reports_numbers_when_freed(nodes_src, nodes_tree, capsys):
    fn = _load_log_reclaim(nodes_src, nodes_tree)
    fn({"freed_gb": 1.234, "used_before_gb": 5.0, "used_after_gb": 3.766, "orphan_gb": 5.0},
       "段1 段末")
    out = capsys.readouterr().out
    assert "[H3性能]" in out and "段1 段末" in out
    assert "5.00→3.77GB" in out and "释放 1.23GB" in out
