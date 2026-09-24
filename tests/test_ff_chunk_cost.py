"""FFN 分块自测的代价 + 画布诊断（2026-09-25 修「开了分块就加载不动」）。

背景（实测数字，torch 2.14.0+cpu / 8 线程 / H3 尺寸 hidden=5376 ffn=14336）：
旧自测按 `rows = chunk_tokens + 1` 造输入（默认 4096 → **4097 行**）且
`device = w.device` —— DynamicVRAM / 卸载态下 `sub.weight` 就在 CPU 上
（`comfy/ops.py:337 cast_bias_weight` 按 `device = input.device` 决定算在哪，
ops.py:358 还会先 materialize 再把整块权重拷到 CPU），于是自测变成"在 CPU 上跑
fp16 前向"：单块 MLP 一次 57.4s、自测跑两遍、52 块 ≈ **100 分钟**（bf16 ≈132 分钟）。
表现就是"模型加载特别久 / 根本加载不动"，而采样一行都还没开始。

本文件钉住修好之后的三条不变量：
  1. 自测输入**行数封顶**（不再随 chunk_tokens 涨），且真的走了切块路径；
  2. 同一**结构签名只测一次**（进程内缓存）—— H3 的 104 个 Linear 归并成 2 次；
  3. 装上的包装**结果等价**（切块前后一致），且观测统计真的报得出来。

外加画布诊断（本次事故的另一半）：`nodes._describe_canvas` 必须与
`nodes._resolve_canvas` 严格互逆，否则报错提示里的"反解档位"会指错方向。

跑法（仓库根目录）：
    python -m pytest tests/test_ff_chunk_cost.py -q
"""

import ast
import importlib.util
import os
import sys
import types

import pytest

torch = pytest.importorskip("torch")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_upscale():
    """以独立包名加载 upscale.py（绕开插件 __init__ 的节点注册）。"""
    pkg_name = "h3sf_ffchunk_probe"
    if pkg_name in sys.modules:
        return sys.modules[pkg_name + ".upscale"]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [ROOT]
    sys.modules[pkg_name] = pkg
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
    spec.loader.exec_module(mod)
    return mod


UP = _load_upscale()


@pytest.fixture(autouse=True)
def _clean_cache():
    """签名缓存是进程级的 —— 每条用例前后都清，别让顺序影响结论。"""
    UP._FF_SELFCHECK_CACHE.clear()
    yield
    UP._FF_SELFCHECK_CACHE.clear()


class _MLP(torch.nn.Module):
    def __init__(self, hid=64, ffn=128):
        super().__init__()
        self.fc1 = torch.nn.Linear(hid, ffn, bias=False)
        self.fc2 = torch.nn.Linear(ffn, hid, bias=False)

    def forward(self, x):
        return self.fc2(self.fc1(x))


class _Diff(torch.nn.Module):
    def __init__(self, n=3):
        super().__init__()
        self.blocks = torch.nn.ModuleList([_MLP() for _ in range(n)])


class _Model(torch.nn.Module):
    """形状够用即可：install_ff_chunking 只认 model.diffusion_model。"""

    def __init__(self, n=3):
        super().__init__()
        self.diffusion_model = _Diff(n)


# ---- ① 自测输入封顶，且真的走切块路径 ----


def test_selfcheck_rows_are_capped(monkeypatch):
    """chunk_tokens=4096 时输入**不再**是 4097 行（那是 100 分钟的来源）。"""
    seen = []
    real_randn = torch.randn

    def fake_randn(*args, **kw):
        first = args[0] if args else kw.get("size")
        seen.append(tuple(first) if isinstance(first, (tuple, list)) else (int(first),))
        return real_randn(*args, **kw)

    monkeypatch.setattr(UP.torch, "randn", fake_randn)
    sub = torch.nn.Linear(32, 64, bias=False)
    stats = {}
    assert UP._ff_chunk_selfcheck(sub, sub.forward, 4096, stats) is True
    assert seen and seen[0][0] <= UP._FF_SELFCHECK_ROWS_CAP, seen
    assert stats["rows"] <= UP._FF_SELFCHECK_ROWS_CAP
    # 必须真的切了（本地 chunk 小于行数）——否则等于没测到切块路径
    assert stats["test_ct"] < stats["rows"]


def test_selfcheck_detects_cross_token_op(monkeypatch):
    """掺了跨 token 操作（这里按列归一化）→ 自测必须判不通过，不许装。"""
    class _CrossToken(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.randn(8, 4))

        def forward(self, x):
            x = x / (x.mean(dim=0, keepdim=True) + 1.0)     # 跨 token 依赖
            return torch.nn.functional.linear(x, self.weight)

    sub = _CrossToken()
    assert UP._ff_chunk_selfcheck(sub, sub.forward, 8) is False


# ---- ② 结构签名去重 ----


def test_selfcheck_caches_by_signature():
    """同结构只测一次：104 个 Linear 归并成 2 次（否则就是 104 次全量前向）。"""
    a = torch.nn.Linear(32, 64, bias=False)
    b = torch.nn.Linear(32, 64, bias=False)          # 同形状同类型 = 同签名
    s1, s2 = {}, {}
    assert UP._ff_chunk_selfcheck(a, a.forward, 4096, s1) is True
    assert UP._ff_chunk_selfcheck(b, b.forward, 4096, s2) is True
    assert s1["tests"] == 1 and s1.get("cache_hits", 0) == 0
    assert s2.get("tests", 0) == 0 and s2["cache_hits"] == 1
    # 形状不同 → 不同签名，各测各的
    c = torch.nn.Linear(32, 128, bias=False)
    s3 = {}
    assert UP._ff_chunk_selfcheck(c, c.forward, 4096, s3) is True
    assert s3["tests"] == 1


# ---- ③ 装上的包装结果等价 + 观测可用 ----


def test_install_ff_chunking_stats_and_equivalence():
    torch.manual_seed(0)
    model = _Model(n=3)
    x = torch.randn(32, 64)
    ref = model.diffusion_model.blocks[0](x)
    stats = {}
    n, why = UP.install_ff_chunking(model, 8, min_tokens=0, stats=stats)
    assert (n, why) == (6, "")                       # 3 块 × (fc1, fc2)
    assert stats["candidates"] == 6
    assert stats["installed"] == 6 and stats["skipped"] == 0
    assert stats["tests"] == 2                       # 只两个签名（64→128 / 128→64）
    assert stats["cache_hits"] == 4
    assert stats["rows"] <= UP._FF_SELFCHECK_ROWS_CAP
    assert stats["install_secs"] < 5.0
    # 包装真的生效，且切块前后逐位一致（32 行 / 每块 8 → 4 块）
    assert model.diffusion_model.blocks[0].fc1._h3_ff_chunk == 8
    assert torch.allclose(model.diffusion_model.blocks[0](x), ref, atol=1e-5, rtol=1e-4)


def test_install_ff_chunking_short_sequence_passthrough():
    """min_tokens 高于实际 token 数 → 直通原路径（结果仍等价）。"""
    torch.manual_seed(1)
    model = _Model(n=1)
    x = torch.randn(16, 64)
    ref = model.diffusion_model.blocks[0](x)
    n, _ = UP.install_ff_chunking(model, 4, min_tokens=64)
    assert n == 2
    assert torch.allclose(model.diffusion_model.blocks[0](x), ref, atol=1e-5, rtol=1e-4)


def test_install_ff_chunking_reports_reason_when_off():
    stats = {}
    assert UP.install_ff_chunking(_Model(1), 0, stats=stats) == (0, "")
    assert "关" in stats["reason"]


# ---- 画布诊断：反解必须与换算严格互逆 ----


def _load_canvas_helpers():
    """从 nodes.py 抽 _AR_RATIO/_MP_OPTIONS/_resolve_canvas/_describe_canvas（不 import ComfyUI）。"""
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {}
    want_const = ("_AR_RATIO", "_MP_OPTIONS")
    want_fn = ("_resolve_canvas", "_describe_canvas")
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in want_const:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<nodes.py>", "exec"), ns)
        elif isinstance(node, ast.FunctionDef) and node.name in want_fn:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<nodes.py>", "exec"), ns)
    missing = [k for k in want_const + want_fn if k not in ns]
    assert not missing, missing
    return ns


def test_describe_canvas_round_trips():
    """每个档位换算出的宽高，反解回来必须还是同一个档位（否则诊断会指错方向）。"""
    ns = _load_canvas_helpers()
    resolve, describe = ns["_resolve_canvas"], ns["_describe_canvas"]
    for ar in ns["_AR_RATIO"]:
        for mp in ns["_MP_OPTIONS"] + [0.98]:
            w, h = resolve(ar, mp)
            txt = describe(w, h)
            back_ar, back_mp = txt.split(" @ ")
            assert resolve(back_ar, float(back_mp.rstrip("MP"))) == (w, h), (ar, mp, txt)


def test_describe_canvas_names_the_incident_values():
    """本次事故的四个数字必须解得出档位（9:16 竖屏，0.5MP vs 1.0MP）。"""
    ns = _load_canvas_helpers()
    describe = ns["_describe_canvas"]
    assert describe(544, 960) == "9:16 @ 0.5MP"
    assert describe(768, 1376) == "9:16 @ 1MP"
    assert "非标准" in describe(768, 999)
    assert "无法反解" in describe(None, None)


def test_assert_match_hint_is_appended():
    """报错必须带上"画布侧四个控件谁在生效"——只有两个数字时用户会一直改错的控件。"""
    spec = importlib.util.spec_from_file_location(
        "h3_ckpt_hint", os.path.join(ROOT, "checkpoint.py"))
    ck = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ck)
    old = {"width": 544, "height": 960}
    new = {"width": 768, "height": 1376}
    with pytest.raises(ValueError) as ei:
        ck.assert_match(old, new, hint="宽高比=自定义 → 直接用「宽度 768 × 高度 1376」")
    msg = str(ei.value)
    assert "width: 存档=544 当前=768" in msg          # 原有口径不许改
    assert "唯一硬约束" in msg
    assert "宽高比=自定义" in msg                     # 新增的诊断必须真的附上
    # 不传 hint 时行为与旧版逐字一致（无尾随换行）
    with pytest.raises(ValueError) as ei2:
        ck.assert_match(old, new)
    assert ei2.value.args[0].endswith("或换个新存档目录开新链")


def test_canvas_diagnostic_is_printed_before_heavy_work():
    """诊断行必须在 FFN 分块（自测/安装）之前 —— 卡在自测里也要看得到画布。"""
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert "[H3画布]" in src and "[H3分块]" in src
    assert src.index("[H3画布]") < src.index("[H3分块]")
    # 报错提示走同一个反解函数，别各写一套
    assert "hint=_hint" in src
