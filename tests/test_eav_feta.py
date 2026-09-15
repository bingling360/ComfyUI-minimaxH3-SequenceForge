"""Enhance-A-Video / FETA 子节点回归 —— 无 ComfyUI 可跑（仅需 torch）。

跑法（仓库根目录）：
    python -m pytest tests/test_eav_feta.py -q

覆盖：
1) CFI 数学：与「逐 (空间位置, head) 物化 T×T 矩阵、抹对角线求均值」的暴力参考实现
   逐值对齐；分块大小不改变结果；均匀注意力时精确等于理论上限 1/frames。
2) 形状守卫：行数 / 维度 / frames 非法一律返回 None，不抛异常。
3) 路由解析：只认 packed 序列里的 video 段，几何从 layout.signature 推导。
4) 端到端：应用模式只缩放 target-video 行、非 video 行逐位不变；仅报告模式不改输出；
   进度窗口外不测量、不介入。
5) 覆盖链：已有 optimized_attention_override 时委托给它，不自我递归。
6) 节点契约：新版 io.ComfyNode schema 合法，execute 形参与输入名一致。
"""
import importlib.util
import io as _io
import logging
import os
import sys
import types

import pytest
import torch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# comfy_api.latest 桩（只覆盖 eav_feta 在定义期用到的符号）
# --------------------------------------------------------------------------
def _install_comfy_api_stub():
    class _ComfyNode:
        pass

    class _IO:
        def __init__(self, *a, **k):
            self.id = a[0] if a else k.get("id")
            self.name = self.id
            for key, val in k.items():
                setattr(self, key, val)

    class _Schema:
        def __init__(self, **k):
            self.__dict__.update(k)

    fake_io = types.ModuleType("comfy_api.latest.io")
    fake_io.ComfyNode = _ComfyNode
    fake_io.Schema = _Schema
    fake_io.NodeOutput = lambda *a, **k: tuple(a)
    for name in ("Model", "Image", "String", "Float", "Int", "Combo", "Latent", "Audio"):
        holder = type(name, (), {"Input": _IO, "Output": _IO})
        setattr(fake_io, name, holder)

    fake_ui = types.ModuleType("comfy_api.latest.ui")
    fake_ui.PreviewText = lambda value, **k: value

    pkg = types.ModuleType("comfy_api")
    pkg.__path__ = []
    latest = types.ModuleType("comfy_api.latest")
    latest.io = fake_io
    latest.ui = fake_ui
    pkg.latest = latest
    sys.modules["comfy_api"] = pkg
    sys.modules["comfy_api.latest"] = latest
    sys.modules["comfy_api.latest.io"] = fake_io
    sys.modules["comfy_api.latest.ui"] = fake_ui
    return fake_io


@pytest.fixture(scope="module")
def eav():
    _install_comfy_api_stub()
    spec = importlib.util.spec_from_file_location(
        "eav_feta_under_test", os.path.join(ROOT, "eav_feta.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eav_feta_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# 公共夹具
# --------------------------------------------------------------------------
HEADS, DIM, FRAMES, SPATIAL, TEXT, AUDIO = 4, 8, 5, 12, 3, 4
SEQ = TEXT + AUDIO + FRAMES * SPATIAL
VS, VE = TEXT + AUDIO, SEQ
BLOCKS = 5


class _Layout:
    """模拟 comfy.ldm.minimax.model.PackedLayout。"""

    def __init__(self):
        self.segments = [(0, TEXT, "text"), (TEXT, TEXT + AUDIO, "audio"), (VS, VE, "video")]
        self.signature = (TEXT, FRAMES, 6, 8, 2)      # text_len, latent_t, lat_h, lat_w, audio_t
        self.seq_len = SEQ


def _qkv(seed=0):
    torch.manual_seed(seed)
    return (torch.randn(1, HEADS, SEQ, DIM), torch.randn(1, HEADS, SEQ, DIM))


def _video_qkv(seed=0):
    """只取 target-video 段（_chunked_cfi 要求行数 == frames*spatial）。"""
    q, k = _qkv(seed)
    return q[:, :, VS:VE], k[:, :, VS:VE]


def _config(eav, mode=None, **kw):
    cfg = {"mode": mode or eav.MODE_APPLY, "tau": 8.0, "start": 0.15, "end": 0.90,
           "workspace": 32, "g_limit": 1.5}
    cfg.update(kw)
    return cfg


# --------------------------------------------------------------------------
# 1) CFI 数学
# --------------------------------------------------------------------------
def _brute_force_cfi(q, k, frames, spatial, heads, dim):
    """完全按论文：逐 (空间位置, head) 物化 T×T，抹对角线取均值。"""
    scale = dim ** -0.5
    total = 0.0
    count = 0
    for s in range(spatial):
        for h in range(heads):
            qv = torch.stack([q[0, h, t * spatial + s] for t in range(frames)])
            kv = torch.stack([k[0, h, t * spatial + s] for t in range(frames)])
            probs = torch.softmax(((qv * scale) @ kv.T).to(torch.float32), dim=-1)
            off = probs.sum() - torch.diagonal(probs).sum()
            total += float(off) / (frames * frames - frames)
            count += 1
    return total / count


def test_cfi_matches_brute_force(eav):
    q, k = _video_qkv(0)
    ref = _brute_force_cfi(q, k, FRAMES, SPATIAL, HEADS, DIM)
    got, chunk_rows, workspace = eav._chunked_cfi(q, k, FRAMES, SPATIAL, 32)
    assert abs(ref - float(got)) < 1e-5
    assert chunk_rows >= 1
    assert workspace > 0


def test_cfi_chunk_size_invariant(eav):
    q, k = _video_qkv(1)
    big, _, _ = eav._chunked_cfi(q, k, FRAMES, SPATIAL, 64)
    small, _, _ = eav._chunked_cfi(q, k, FRAMES, SPATIAL, 4)
    assert abs(float(big) - float(small)) < 1e-6


def test_cfi_uniform_equals_one_over_frames(eav):
    """注意力均匀时非对角均值恰好是 1/frames。"""
    q = torch.zeros(1, 2, FRAMES * 3, DIM)
    k = torch.zeros(1, 2, FRAMES * 3, DIM)
    got, _, _ = eav._chunked_cfi(q, k, FRAMES, 3, 32)
    assert abs(float(got) - 1.0 / FRAMES) < 1e-6


def test_cfi_never_exceeds_paper_upper_bound(eav):
    """CFI 上限是 1/(frames-1)（trace→0 时），不是 1/frames。"""
    q, k = _video_qkv(7)
    got, _, _ = eav._chunked_cfi(q, k, FRAMES, SPATIAL, 32)
    assert float(got) <= 1.0 / (FRAMES - 1) + 1e-6


@pytest.mark.parametrize("frames,spatial,ndim", [
    (FRAMES, SPATIAL + 1, 4),      # 行数与 frames*spatial 不符
    (1, FRAMES * SPATIAL, 4),      # frames < 2
    (FRAMES, SPATIAL, 3),          # 维度不对
])
def test_cfi_shape_guards_return_none(eav, frames, spatial, ndim):
    q, k = _qkv(0)
    if ndim == 3:
        q, k = q[0], k[0]
    assert eav._chunked_cfi(q, k, frames, spatial, 32) is None


# --------------------------------------------------------------------------
# 2) 路由解析
# --------------------------------------------------------------------------
def test_route_resolves_video_segment(eav):
    route = eav._resolve_route({"minimax_h3_layout": _Layout()})
    assert route is not None
    assert route["video_start"] == VS and route["video_end"] == VE
    assert route["frames"] == FRAMES
    # 每帧空间 token = ceil(lat_h/2) * ceil(lat_w/2) = ceil(6/2)*ceil(8/2) = 12
    assert route["spatial_tokens"] == 12
    assert route["seq_len"] == SEQ


@pytest.mark.parametrize("bad", [
    {},                                                    # 没有 layout
    {"minimax_h3_layout": None},
    {"minimax_h3_layout": types.SimpleNamespace(segments=[(0, 3, "text")], signature=(3, 5, 6, 8, 2), seq_len=3)},
    # video 行数与 frames*spatial 不符
    {"minimax_h3_layout": types.SimpleNamespace(segments=[(0, 10, "video")], signature=(3, 5, 6, 8, 2), seq_len=10)},
])
def test_route_rejects_bad_layout(eav, bad):
    assert eav._resolve_route(bad) is None


def test_route_ignores_non_main_attention(eav):
    """序列长度对不上（如 token_refiner）时原样返回。"""
    q, k = _qkv(0)
    out = torch.ones(1, 5, HEADS * DIM)
    opts = {"minimax_h3_layout": _Layout(), "sigmas": torch.tensor(500.0)}
    eav._reset_run(_config(eav), BLOCKS)
    res = eav._apply_gain(q[:, :, :5], k[:, :, :5], out, opts, _config(eav))
    assert torch.equal(res, out)


# --------------------------------------------------------------------------
# 3) 端到端
# --------------------------------------------------------------------------
def test_apply_scales_only_video_rows(eav):
    q, k = _qkv(2)
    out = torch.ones(1, SEQ, HEADS * DIM)
    opts = {"minimax_h3_layout": _Layout(), "sigmas": torch.tensor(500.0)}
    cfg = _config(eav)
    eav._reset_run(cfg, BLOCKS)
    res = eav._apply_gain(q, k, out.clone(), opts, cfg)

    video = res[0, VS:VE]
    other = torch.cat([res[0, :VS], res[0, VE:]])
    assert not torch.allclose(video, torch.ones_like(video)), "video 行应被缩放"
    assert torch.allclose(other, torch.ones_like(other)), "非 video 行必须逐位不变"
    ratio = video / torch.ones_like(video)
    assert torch.allclose(ratio, ratio.flatten()[0].expand_as(ratio), atol=1e-5), "应是统一比例"
    g = float(ratio.flatten()[0])
    assert g >= 1.0 and g <= cfg["g_limit"] + 1e-6


def test_report_only_does_not_change_output(eav):
    q, k = _qkv(3)
    out = torch.ones(1, SEQ, HEADS * DIM)
    cfg = _config(eav, mode=eav.MODE_REPORT)
    eav._reset_run(cfg, BLOCKS)
    res = eav._apply_gain(q, k, out.clone(),
                          {"minimax_h3_layout": _Layout(), "sigmas": torch.tensor(500.0)}, cfg)
    assert torch.equal(res, out), "仅报告模式不得改动输出"
    assert eav._RUN.report()["measured_forwards"] == 1, "但仍应测量"


def test_disabled_mode_is_passthrough(eav):
    q, k = _qkv(4)
    out = torch.ones(1, SEQ, HEADS * DIM)
    cfg = _config(eav, mode=eav.MODE_DISABLED)
    eav._reset_run(cfg, BLOCKS)
    res = eav._apply_gain(q, k, out.clone(),
                          {"minimax_h3_layout": _Layout(), "sigmas": torch.tensor(500.0)}, cfg)
    assert torch.equal(res, out)
    assert eav._RUN.report()["model_forwards"] == 0


def test_outside_progress_window_skips_measurement(eav):
    q, k = _qkv(5)
    out = torch.ones(1, SEQ, HEADS * DIM)
    cfg = _config(eav)
    eav._reset_run(cfg, BLOCKS)
    # sigma=50 -> 进度 0.95，在窗口 0.15~0.90 之外
    res = eav._apply_gain(q, k, out.clone(),
                          {"minimax_h3_layout": _Layout(), "sigmas": torch.tensor(50.0)}, cfg)
    assert torch.equal(res, out)
    rep = eav._RUN.report()
    assert rep["model_forwards"] == 1, "窗口外仍记录前向"
    assert rep["measured_forwards"] == 0, "窗口外不测量"


def test_overflow_is_clamped_and_counted(eav):
    """g 超上限时截断并计数，不抛异常。"""
    q, k = _qkv(6)
    out = torch.ones(1, SEQ, HEADS * DIM)
    cfg = _config(eav, tau=32.0, g_limit=1.0)      # 上限压到 1.0，必然触发
    eav._reset_run(cfg, BLOCKS)
    res = eav._apply_gain(q, k, out.clone(),
                          {"minimax_h3_layout": _Layout(), "sigmas": torch.tensor(500.0)}, cfg)
    rep = eav._RUN.report()
    assert rep["overflow_count"] >= 1
    video = res[0, VS:VE]
    assert float(video.flatten()[0]) <= 1.0 + 1e-6


# --------------------------------------------------------------------------
# 4) 覆盖链
# --------------------------------------------------------------------------
def test_override_delegates_to_previous_without_recursion(eav):
    calls = {"n": 0}

    def backend(q, k, v, heads, **kwargs):
        calls["n"] += 1
        return torch.ones(1, q.shape[2], heads * q.shape[3])

    def previous(func, q, k, v, heads, **kwargs):
        calls["n"] += 1
        return func(q, k, v, heads, **kwargs)

    q, k = _qkv(0)
    cfg = _config(eav, mode=eav.MODE_REPORT)
    eav._reset_run(cfg, BLOCKS)
    override = eav._make_override(cfg, previous)
    out = override(backend, q, k, q, HEADS, mask=None, attn_precision=None,
                   skip_reshape=True, skip_output_reshape=False,
                   transformer_options={"minimax_h3_layout": _Layout(),
                                        "sigmas": torch.tensor(500.0)})
    assert calls["n"] == 2, "上游覆盖与真实后端各调用一次"
    assert tuple(out.shape) == (1, SEQ, HEADS * DIM)


def test_override_without_previous_calls_backend_directly(eav):
    q, k = _qkv(0)
    cfg = _config(eav, mode=eav.MODE_REPORT)
    eav._reset_run(cfg, BLOCKS)
    override = eav._make_override(cfg, None)
    out = override(lambda q, k, v, heads, **kw: torch.ones(1, q.shape[2], heads * q.shape[3]),
                   q, k, q, HEADS, mask=None, attn_precision=None, skip_reshape=True,
                   skip_output_reshape=False,
                   transformer_options={"minimax_h3_layout": _Layout(),
                                        "sigmas": torch.tensor(500.0)})
    assert tuple(out.shape) == (1, SEQ, HEADS * DIM)


def test_apply_gain_survives_bad_batch(eav):
    """batch != 1 不抛异常。"""
    q = torch.randn(2, HEADS, SEQ, DIM)
    out = torch.ones(2, SEQ, HEADS * DIM)
    cfg = _config(eav)
    eav._reset_run(cfg, BLOCKS)
    res = eav._apply_gain(q, q, out, {"minimax_h3_layout": _Layout(),
                                      "sigmas": torch.tensor(500.0)}, cfg)
    assert res is out


# --------------------------------------------------------------------------
# 5) 控制台日志与前向边界
# --------------------------------------------------------------------------
def test_console_log_works_without_report_node(eav):
    """不接报告节点时，控制台每个前向打一行。"""
    cfg = _config(eav)
    # 先清掉其它用例的残留：reset 会把上一个未结账的前向打出来，必须发生在挂 handler 之前
    eav._reset_run(cfg, BLOCKS)
    stream = _io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    eav.LOG.addHandler(handler)
    old_level = eav.LOG.level
    eav.LOG.setLevel(logging.INFO)
    try:
        eav._reset_run(cfg, BLOCKS)
        q, k = _qkv(0)
        for sigma in (900.0, 700.0, 500.0):
            opts = {"minimax_h3_layout": _Layout(), "sigmas": torch.tensor(sigma)}
            for _ in range(BLOCKS):
                eav._apply_gain(q, k, torch.ones(1, SEQ, HEADS * DIM), opts, cfg)
    finally:
        eav.LOG.removeHandler(handler)
        eav.LOG.setLevel(old_level)

    lines = [ln for ln in stream.getvalue().splitlines() if "H3-EAV-FETA" in ln]
    assert len(lines) == 3, f"3 个前向应有 3 行日志，实际 {len(lines)}：{lines}"
    assert any("窗口外" in ln for ln in lines), "sigma=900 -> 进度 0.1 在窗口外"
    assert any("CFI=" in ln for ln in lines)


def test_reused_options_dict_with_new_sigma_is_new_forward(eav):
    """上游若复用同一 options 字典，sigma 变化也要判为新前向。"""
    cfg = _config(eav)
    eav._reset_run(cfg, BLOCKS)
    q, k = _qkv(0)
    shared = {"minimax_h3_layout": _Layout(), "sigmas": torch.tensor(900.0)}
    for _ in range(BLOCKS):
        eav._apply_gain(q, k, torch.ones(1, SEQ, HEADS * DIM), shared, cfg)
    shared["sigmas"] = torch.tensor(500.0)
    for _ in range(BLOCKS):
        eav._apply_gain(q, k, torch.ones(1, SEQ, HEADS * DIM), shared, cfg)
    rep = eav._RUN.report()
    assert rep["model_forwards"] == 2
    assert rep["forwards"][1]["progress"] == 0.5, "第二个前向必须用新 sigma 判窗口"


# --------------------------------------------------------------------------
# 6) 节点契约
# --------------------------------------------------------------------------
def test_node_schema_is_valid(eav):
    for cls in (eav.H3EAVFetaPatch, eav.H3EAVFetaReport):
        schema = cls.define_schema()
        assert schema.node_id
        assert schema.display_name
        assert schema.category == "MiniMaxH3"
        assert schema.inputs and schema.outputs

        declared = [getattr(i, "id", None) or getattr(i, "name", None) for i in schema.inputs]
        params = list(cls.execute.__func__.__code__.co_varnames[
            1:1 + cls.execute.__func__.__code__.co_argcount - 1])
        assert declared == params, f"{schema.node_id}: 输入名 {declared} 与 execute 形参 {params} 不一致"


def test_patch_node_defaults(eav):
    schema = eav.H3EAVFetaPatch.define_schema()
    by_id = {}
    for item in schema.inputs:
        by_id[getattr(item, "id", None) or getattr(item, "name", None)] = item
    assert by_id["模式"].default == eav.MODE_REPORT, "默认必须是仅报告（先测再开）"
    assert by_id["强度"].default == 8.0
    assert by_id["起始进度"].default == 0.15
    assert by_id["结束进度"].default == 0.90
    assert by_id["工作区上限MiB"].default == 32
    assert by_id["增益上限"].default == 1.5


def test_bad_progress_window_raises(eav):
    with pytest.raises(ValueError):
        eav.H3EAVFetaPatch.execute(_FakeModel(), eav.MODE_APPLY, 8.0, 0.9, 0.1, 32, 1.5)


def test_disabled_returns_same_model_object(eav):
    model = _FakeModel()
    out = eav.H3EAVFetaPatch.execute(model, eav.MODE_DISABLED, 8.0, 0.15, 0.90, 32, 1.5)
    assert out[0] is model


def test_clone_does_not_pollute_source_model(eav):
    model = _FakeModel()
    model.model_options["transformer_options"]["keep_me"] = "x"
    out = eav.H3EAVFetaPatch.execute(model, eav.MODE_REPORT, 8.0, 0.15, 0.90, 32, 1.5)
    cloned = out[0]
    assert cloned is not model
    assert "optimized_attention_override" in cloned.model_options["transformer_options"]
    assert "optimized_attention_override" not in model.model_options["transformer_options"]
    assert cloned.model_options["transformer_options"]["keep_me"] == "x"


class _FakeModel:
    """最小 ModelPatcher 替身：只需要 clone / model_options / get_model_object。"""

    def __init__(self, blocks=3):
        self._blocks = [object() for _ in range(blocks)]
        self.model_options = {"transformer_options": {}}

    def clone(self):
        n = _FakeModel.__new__(_FakeModel)
        n._blocks = self._blocks
        n.model_options = {"transformer_options": dict(self.model_options["transformer_options"])}
        return n

    def get_model_object(self, key):
        return types.SimpleNamespace(blocks=self._blocks)
