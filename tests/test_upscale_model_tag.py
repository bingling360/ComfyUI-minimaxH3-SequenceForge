"""二采模型结构签名（upscale.model_tag）与换模型提示（model_mismatch_note）单测。

无 ComfyUI 依赖：只有 upscale.py 顶层 `import torch`，无 torch 时注入最小 stub
（model_tag 只用到 torch.Tensor 的 isinstance 判定），测试在纯 Python 下也能跑。

跑法（仓库根目录）：
    python -m pytest tests/test_upscale_model_tag.py -q
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


# ---- 最小 torch stub（仅 model_tag 用到的 isinstance 判定） ----

class _StubTensor(object):
    pass


class _Sum(object):
    def __init__(self, v):
        self._v = v

    def item(self):
        return self._v


def _stub_torch():
    """无 torch 时的最小替身（只有 upscale 用到的 isinstance 判定）。"""
    m = types.ModuleType("torch")
    m.Tensor = _StubTensor
    return m


def _load_upscale():
    """以独立包名加载 upscale.py（绕开插件 __init__ 的节点注册）。"""
    pkg_name = "h3sf_upscale_probe"
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
    # 无 torch：只在加载期间挂替身，加载完立刻摘掉——绝不能污染 sys.modules，
    # 否则同会话里其它测试 import torch 会拿到这个空壳（模块全局已绑定引用，
    # 摘掉后 model_tag 依然可用）
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


# ---- 假 ModelPatcher ----

class _Param(object):
    def __init__(self, numel, dtype, shape):
        self._numel = numel
        self.dtype = dtype
        self.shape = shape

    def numel(self):
        return self._numel


class _DiffusionModel(object):
    def __init__(self, params):
        self._params = params

    def parameters(self):
        return iter(self._params)


class _Inner(object):
    def __init__(self, dm):
        self.diffusion_model = dm


class _FakeModel(object):
    """够 model_tag 用的 ModelPatcher 替身：model.model.diffusion_model + patches。"""

    def __init__(self, params=None, patches=None, object_patches=None, cls=_DiffusionModel):
        params = params if params is not None else [_Param(1000, "torch.float16", (10, 100))]
        self.model = _Inner(cls(params))
        self.patches = patches or {}
        self.object_patches = object_patches or {}


if _REAL_TORCH:
    def _mk_tensor(vals):
        return _torch.tensor(vals, dtype=_torch.float32)
else:
    class _StubT(_StubTensor):
        """够 _patch_digest 用的张量替身：detach/reshape/[:n]/float/sum/item。"""

        def __init__(self, vals):
            self._vals = list(vals)

        def detach(self):
            return self

        def reshape(self, *a, **k):
            return self

        def __getitem__(self, k):
            return self

        def float(self):
            return self

        def sum(self):
            return _Sum(sum(self._vals))

    def _mk_tensor(vals):
        return _StubT(vals)


# ---- model_tag ----

def test_tag_stable_and_hex8():
    m = _FakeModel()
    a, b = upscale.model_tag(m), upscale.model_tag(m)
    assert a == b
    assert a and len(a) == 8 and all(c in "0123456789abcdef" for c in a)


def test_tag_differs_by_quantization():
    """dtype 集合不同（fp16 vs fp8 vs bf16）→ 签名不同（不同量化能被区分）。"""
    base = upscale.model_tag(_FakeModel([_Param(1000, "torch.float16", (10, 100))]))
    fp8 = upscale.model_tag(
        _FakeModel([_Param(1000, "torch.float8_e4m3fn", (10, 100))]))
    bf16 = upscale.model_tag(_FakeModel([_Param(1000, "torch.bfloat16", (10, 100))]))
    assert base != fp8 and base != bf16 and fp8 != bf16


def test_tag_differs_by_param_count_and_class():
    a = upscale.model_tag(_FakeModel([_Param(1000, "torch.float16", (10, 100))]))
    b = upscale.model_tag(_FakeModel([_Param(2000, "torch.float16", (10, 100))]))
    assert a != b

    class _OtherDM(_DiffusionModel):
        pass
    c = upscale.model_tag(_FakeModel([_Param(1000, "torch.float16", (10, 100))],
                                     cls=_OtherDM))
    assert a != c


def test_tag_differs_by_lora_name_and_strength():
    """LoRA 种类（patches 键）与强度都要能区分。"""
    plain = upscale.model_tag(_FakeModel())
    lora_a = upscale.model_tag(_FakeModel(
        patches={"diffusion_model.blocks.0.lora_A.weight": [(1.0, _mk_tensor([1.0, 2.0]))]}))
    lora_b = upscale.model_tag(_FakeModel(
        patches={"diffusion_model.blocks.9.lora_A.weight": [(1.0, _mk_tensor([1.0, 2.0]))]}))
    assert plain != lora_a
    assert lora_a != lora_b
    # 同键同权重、强度不同 → 也要能区分
    lora_a_weak = upscale.model_tag(_FakeModel(
        patches={"diffusion_model.blocks.0.lora_A.weight": [(0.3, _mk_tensor([1.0, 2.0]))]}))
    assert lora_a != lora_a_weak


def test_tag_differs_by_lora_weight_same_strength():
    a = upscale.model_tag(_FakeModel(
        patches={"k.lora_A.weight": [(1.0, _mk_tensor([1.0, 2.0]))]}))
    b = upscale.model_tag(_FakeModel(
        patches={"k.lora_A.weight": [(1.0, _mk_tensor([9.0, 7.0]))]}))
    assert a != b


def test_tag_empty_on_no_params_or_broken_model():
    assert upscale.model_tag(_FakeModel(params=[])) == ""

    class _Boom(object):
        @property
        def model(self):
            raise RuntimeError("boom")

    assert upscale.model_tag(_Boom()) == ""


def test_patch_digest_non_tensor_is_empty():
    assert upscale._patch_digest(None) == ""
    assert upscale._patch_digest("not-a-tensor") == ""
    if _REAL_TORCH:
        assert upscale._patch_digest(_torch.tensor([1.0, 2.0])) != ""


# ---- model_mismatch_note ----

def _manifest_with(rec, idx=0):
    """把 rec 放在 segs[idx]（前面补 None），便于测段号换算。"""
    segs = [None] * (idx + 1)
    segs[idx] = rec
    return {"upscale": {"segs": segs}}


def test_mismatch_note_none_cases():
    # 无签名（未接二采模型 / 签名失败）→ 不提示
    assert upscale.model_mismatch_note(_manifest_with({"model": "aaaaaaaa"}), 0, "") is None
    # 存档无 model 字段（旧记录）→ 不提示
    assert upscale.model_mismatch_note(_manifest_with({"done": True}), 0, "bbbbbbbb") is None
    # 一致 → 不提示
    assert upscale.model_mismatch_note(
        _manifest_with({"model": "aaaaaaaa"}), 0, "aaaaaaaa") is None
    # 该段无记录 / manifest 为 None → 不提示
    assert upscale.model_mismatch_note({"upscale": {"segs": []}}, 0, "aaaaaaaa") is None
    assert upscale.model_mismatch_note(None, 0, "aaaaaaaa") is None


def test_mismatch_note_warns_on_model_switch():
    note = upscale.model_mismatch_note(
        _manifest_with({"model": "aaaaaaaa"}, idx=2), 2, "bbbbbbbb")
    assert note is not None
    assert "aaaaaaaa" in note and "bbbbbbbb" in note
    assert "段3" in note                      # 提示里是 1-based 段号
    assert "不自动重做" in note and "重跑起始段" in note


def test_mismatch_does_not_touch_stale_judgement():
    """回归护栏：模型签名只留痕，不进 params_hash（换模型不触发自动重做）。"""
    cfg = upscale.parse_state({"upscale": {"mode": "跟随生成", "scale": 2.0}})
    assert cfg is not None
    # 换任意模型都不改变二采参数指纹
    h1 = upscale.params_hash(cfg)
    _ = upscale.model_tag(_FakeModel([_Param(1, "torch.float16", (1,))]))
    _ = upscale.model_tag(_FakeModel([_Param(2, "torch.float8_e4m3fn", (1,))]))
    assert upscale.params_hash(cfg) == h1
    # write_record 之外的记录字段不参与 _record_valid 判定
    segs = [{"done": True, "hash": h1, "base_hash": "x", "model": "zzzzzzzz"}]
    # 文件不存在 → False；这只验证判据仍是 hash/base_hash + 产物齐全，
    # 不会因多了 model 字段而抛错
    assert upscale._record_valid(segs, ROOT, 0, h1, "x") is False
    assert upscale._record_valid(segs, ROOT, 0, "deadbeef", "x") is False


# ---- models_distinct（二采独立模型时是否值得先卸一采） ----

class _UUID(object):
    """永远不会相等的 uuid 替身（模拟两份独立权重）。"""

    def __eq__(self, other):
        return self is other

    def __ne__(self, other):
        return self is not other

    def __hash__(self):
        return id(self)


class _BaseModel(object):
    """够 models_distinct 用的 ModelPatcher 替身：只有 clone_base_uuid。"""

    def __init__(self, uuid=None):
        if uuid is not None:
            self.clone_base_uuid = uuid


def test_distinct_false_when_same_object_or_none():
    m = _BaseModel(_UUID())
    assert upscale.models_distinct(m, m) is False          # 未接二采槽：沿用一采
    assert upscale.models_distinct(None, m) is False
    assert upscale.models_distinct(m, None) is False
    assert upscale.models_distinct(None, None) is False


def test_distinct_false_for_same_base_clone():
    """同 base 的克隆（只差 LoRA）不该卸——卸了等于把二采自己搬走。"""
    u = _UUID()
    a = _BaseModel(u)
    b = _BaseModel(u)          # 不同对象，但同源
    assert a is not b
    assert upscale.models_distinct(a, b) is False


def test_distinct_true_for_independent_weights():
    a = _BaseModel(_UUID())
    b = _BaseModel(_UUID())
    assert upscale.models_distinct(a, b) is True


def test_distinct_conservative_without_uuid():
    """拿不到 clone_base_uuid（老版本/非 ModelPatcher）→ 保守判「不同」。

    宁可多一次换页，也不让碎片问题留着；判成 False 才会漏。
    """
    class _Bare(object):
        pass

    assert upscale.models_distinct(_Bare(), _Bare()) is True
    assert upscale.models_distinct(_BaseModel(_UUID()), _Bare()) is True
    assert upscale.models_distinct(_Bare(), _BaseModel(_UUID())) is True
