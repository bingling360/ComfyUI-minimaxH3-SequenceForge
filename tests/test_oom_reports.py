"""OOM 报告文案的契约测试。

背景：解码路径**一直**没有 OOM 兜底——采样路径早就有「原参重试 + 可行动中文报错」，
解码却是裸 torch 堆栈。用户既看不懂，也不知道「已落盘的分段不受影响」，而那正是
他此刻最想知道的事。同时解码的归因不能照抄采样那句「画布×帧数」：该 VAE 的全片
像素缓冲在 `intermediate_device()`（默认 cpu）上，根本不在显存里。

这里钉四件事：

1. **所有 `video_vae.decode` 都必须走 `_decode_rescue`** —— 不许再有裸调用。
2. **归因必须是「解码与权重缓存抢显存」**，不许出现「像素缓冲 ∝ 面积×帧数」。
3. **余量对账**：留空与实测需求必须被连起来判，且要说清处方够不够。
4. **余量策略的预测不许漏掉解码**（原先只写「二采最先炸」，实测首段解码先炸）。
"""

import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import perf  # noqa: E402

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


# --------------------------------------------------------------- 1) 解码兜底

def test_every_video_vae_decode_goes_through_rescue(nodes_src, nodes_tree):
    """裸 `video_vae.decode(...)` 一个都不许剩 —— 全部走 `_decode_rescue`。"""
    rescue = _find_func(nodes_tree, "_decode_rescue")
    lo, hi = rescue.lineno, rescue.end_lineno

    tree = ast.parse(nodes_src)
    bare = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute) and f.attr == "decode"
                and isinstance(f.value, ast.Name) and f.value.id == "video_vae"):
            if not (lo <= node.lineno <= hi):
                bare.append(node.lineno)
    assert not bare, f"这些行还有裸 video_vae.decode（未走自救）：{bare}"


def test_decode_rescue_has_original_param_retry(nodes_src, nodes_tree):
    """解码自救必须与采样同口径：`perf.oom_retry` + 原参重试 + `is_oom_error` 分流。"""
    seg = ast.get_source_segment(nodes_src, _find_func(nodes_tree, "_decode_rescue"))
    assert "perf.oom_retry(" in seg, "没有用 perf.oom_retry（应复用既有自救口径）"
    assert "cleanup=_vram_oom_cleanup" in seg, "cleanup 必须是共用的 _vram_oom_cleanup"
    assert "tries=2" in seg, "重试次数应与采样路径一致"
    assert "perf.is_oom_error(" in seg, "非 OOM 异常必须原样上抛，不许吞"


def test_decode_rescue_cleanup_uses_unload_all_models(nodes_src):
    """`_vram_oom_cleanup` 必须真的会 `unload_all_models`。

    为什么：`load_models_gpu([vae.patcher], ...)` 对动态模型是 **no-op**
    （`free_memory` 里 `is_dynamic() and for_dynamic` 把 `memory_to_free` 置 0）。
    唯一走 `for_dynamic=False` → `model_unload` → `detach` 的只有它。
    """
    tree = ast.parse(nodes_src)
    seg = ast.get_source_segment(nodes_src, _find_func(tree, "_vram_oom_cleanup"))
    assert "unload_all_models()" in seg


def test_decode_error_message_does_not_claim_pixel_buffer(nodes_src, nodes_tree):
    """归因不许写「像素缓冲 ∝ 画布面积 × 帧数」——该缓冲在 CPU 上，不在显存里。

    只扫**报错文案本身**（`raise RuntimeError(...)` 那个 Call），不扫整个函数：
    docstring 里正当地把它当反例点名，扫全文会假失败。
    """
    fn = _find_func(nodes_tree, "_decode_rescue")
    msg = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            msg = ast.get_source_segment(nodes_src, node.exc)
            break
    assert msg, "找不到 _decode_rescue 的报错文案"
    assert "像素缓冲" not in msg, "解码报错归因错了（像素缓冲在 intermediate_device/cpu 上）"
    assert "权重缓存" in msg, "必须点明真正抢显存的是权重缓存"
    assert "已落盘的分段不受影响" in msg, "这句是解码失败时用户最需要的"
    assert "--vram-headroom" in msg, "要给可执行的处方"


def test_sampling_error_message_also_points_at_headroom(nodes_src):
    """采样 OOM 文案同样要给 `--vram-headroom` —— 与解码口径一致。"""
    tree = ast.parse(nodes_src)
    hits = [ast.get_source_segment(nodes_src, n) for n in ast.walk(tree)
            if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
            and getattr(n.exc.func, "id", "") == "RuntimeError"]
    joined = "\n".join(h for h in hits if h and "主干采样显存不足" in h)
    assert joined, "找不到主干采样的 OOM 报错"
    assert "--vram-headroom" in joined


# --------------------------------------------------------------- 2) 余量对账

def test_headroom_gap_line_reports_shortfall_and_says_fix_is_not_enough():
    """缺口要说清「差多少」，并点明只加 +1GB 补不上。"""
    line = perf.headroom_gap_line(1.48, 3.9)
    assert line
    assert "1.48GB" in line and "3.90GB" in line
    assert "差 2.42GB" in line
    assert "--vram-headroom 1" in line and "补不上" in line


def test_headroom_gap_line_says_ok_when_enough():
    line = perf.headroom_gap_line(5.0, 3.9)
    assert line and "余量够用" in line
    assert "差" not in line


def test_headroom_gap_line_returns_none_without_measurement():
    """量不到需求（None / ≤0）→ 不编数字。"""
    assert perf.headroom_gap_line(1.5, None) is None
    assert perf.headroom_gap_line(None, 3.9) is None
    assert perf.headroom_gap_line(1.5, 0) is None
    assert perf.headroom_gap_line(1.5, -1) is None


def test_headroom_gap_line_extra_grows_the_need():
    """额外需求（如解码工作集）应当把「需要」抬高。"""
    assert perf.headroom_gap_line(5.0, 3.9) and "余量够用" in perf.headroom_gap_line(5.0, 3.9)
    line = perf.headroom_gap_line(5.0, 3.9, extra_gb=2.0)
    assert line and "5.90GB" in line


def test_nodes_wires_the_gap_line_into_the_report(nodes_src):
    """对账行必须进**报告**，不能只打控制台 —— 用户读的是报告。"""
    assert "perf.headroom_gap_line(" in nodes_src
    assert "report.append(_gap)" in nodes_src


# --------------------------------------------------------------- 3) 余量策略预测

def test_headroom_advice_predicts_decode_not_only_second_pass():
    """预测不许只写「二采最先炸」——实测首段 VAE 解码先炸。"""
    msg = perf.vram_headroom_advice({}, {"dynamic_vram": True, "vram_headroom_gb": 0,
                                         "reserve_vram_gb": None}, 0.0)
    assert msg, "该策略下应当给出建议"
    assert "解码" in msg, "预测漏了解码（本机实测就是首段解码先炸）"
    assert "二采" in msg


def test_compiler_advice_carries_the_counter_evidence():
    """`--disable-comfy-compiler` 的建议必须带上反证：关掉后失败点会前移。"""
    msg = perf.vram_headroom_advice({}, {"dynamic_vram": True, "vram_headroom_gb": 0,
                                         "reserve_vram_gb": None}, 0.0)
    assert "disable-comfy-compiler" in msg
    assert "前移" in msg, "只报好处不报反证 = 会把用户引到反方向"


def test_headroom_advice_silent_when_policy_is_fine():
    assert perf.vram_headroom_advice({}, {"dynamic_vram": True, "vram_headroom_gb": 1}, 0.0) is None
    assert perf.vram_headroom_advice({}, {"dynamic_vram": False, "vram_headroom_gb": 0}, 0.0) is None
    assert perf.vram_headroom_advice({}, None, 0.0) is None
