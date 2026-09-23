"""自定义 sigma 表适配（少步蒸馏 LoRA 通道）回归。

跑法（仓库根目录）：
    python -m pytest tests/test_sigmas_adapter.py -q

钉死三件事：
1. 未接 sigmas 时存档指纹**与旧公式完全一致**（既有项目必须能续上）；
2. 接了 sigmas 后换表 = 换指纹（否则换 sigma 表会静默复用旧段）；
3. ksampler_with_sigmas 的 sigmas=None 分支与官方 common_ksampler 同参，
   非 None 分支把表传下去、且进度步数按 sigma 点数算。
"""

import ast
import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# HyperFlow 官方 8 步表（9 点）
HF_SIGMAS = [1.0, 0.931506, 0.839236, 0.703462, 0.5, 0.296538, 0.160764, 0.068494, 0.0]


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeSigmas:
    """够用的 tensor 桩：detach/flatten/tolist/shape。"""

    def __init__(self, vals):
        self._v = list(vals)
        self.shape = (len(vals),)

    def detach(self):
        return self

    def flatten(self):
        return self

    def tolist(self):
        return list(self._v)


# ---- fingerprint_tag ----

def test_tag_none_means_no_key():
    sa = _load("h3_sigmas_adapter", "sigmas_adapter.py")
    assert sa.fingerprint_tag(None) is None


def test_tag_roundtrip():
    sa = _load("h3_sigmas_adapter", "sigmas_adapter.py")
    tag = sa.fingerprint_tag(FakeSigmas(HF_SIGMAS))
    assert tag.split(",")[0] == "1"
    assert tag.endswith(",0")
    assert len(tag.split(",")) == 9
    # 同表同标签（决定论，跨进程稳定）
    assert tag == sa.fingerprint_tag(FakeSigmas(HF_SIGMAS))
    # 改一点就是新标签
    other = list(HF_SIGMAS)
    other[4] = 0.49
    assert sa.fingerprint_tag(FakeSigmas(other)) != tag


def test_sigmas_steps():
    sa = _load("h3_sigmas_adapter", "sigmas_adapter.py")
    assert sa.sigmas_steps(None) is None
    assert sa.sigmas_steps(FakeSigmas(HF_SIGMAS)) == 8


# ---- 存档指纹兼容性（最关键的一条） ----

def _params(**kw):
    p = {"width": 1280, "height": 720, "length": 120, "ctx": 22,
         "steps": 20, "cfg": 6.0, "sampler": "euler", "scheduler": "simple",
         "chain": "i2v", "fade_ratio": 0.0,
         "gate": {"mode": "关", "threshold": 0.0}}
    p.update(kw)
    return p


def test_fingerprint_unchanged_when_no_sigmas():
    """不接 sigmas 时绝对不能多键：json.dumps(sort_keys) 下多一个空串键也是新指纹，
    会让所有既有项目的指纹漂移、旧存档续不上。"""
    ck = _load("h3_ckpt_sigmas", "checkpoint.py")
    sa = _load("h3_sigmas_adapter2", "sigmas_adapter.py")
    base = ck.fingerprint(_params())
    # 模拟 nodes.py 的行为：tag 为 None 时不加键
    p = _params()
    tag = sa.fingerprint_tag(None)
    if tag is not None:
        p["sigmas"] = tag
    assert ck.fingerprint(p) == base


def test_fingerprint_changes_with_sigmas():
    ck = _load("h3_ckpt_sigmas2", "checkpoint.py")
    sa = _load("h3_sigmas_adapter3", "sigmas_adapter.py")
    base = ck.fingerprint(_params())
    a = _params()
    a["sigmas"] = sa.fingerprint_tag(FakeSigmas(HF_SIGMAS))
    b = _params()
    b["sigmas"] = sa.fingerprint_tag(FakeSigmas([1.0, 0.5, 0.0]))
    assert ck.fingerprint(a) != base      # 接了表 -> 新链
    assert ck.fingerprint(a) != ck.fingerprint(b)   # 换表 -> 重做


# ---- ksampler_with_sigmas 的两条分支 ----

def _install_fake_comfy():
    """注入 comfy.sample / comfy.utils / latent_preview 桩，记录调用参数。"""
    calls = {}
    comfy = types.ModuleType("comfy")
    sample = types.ModuleType("comfy.sample")
    utils = types.ModuleType("comfy.utils")
    utils.PROGRESS_BAR_ENABLED = True

    def fix_empty_latent_channels(model, li, a, b):
        return li

    def prepare_noise(li, seed, batch_inds):
        calls["noise"] = (seed, batch_inds)
        return "NOISE"

    def sample_fn(model, noise, steps, cfg, sampler_name, scheduler,
                  positive, negative, latent_image, denoise=1.0,
                  disable_noise=False, start_step=None, last_step=None,
                  force_full_denoise=False, noise_mask=None, sigmas=None,
                  callback=None, disable_pbar=None, seed=None):
        calls["sample"] = {"steps": steps, "denoise": denoise, "sigmas": sigmas,
                           "seed": seed, "sampler_name": sampler_name,
                           "scheduler": scheduler, "callback": callback}
        return "SAMPLED"

    sample.fix_empty_latent_channels = fix_empty_latent_channels
    sample.prepare_noise = prepare_noise
    sample.sample = sample_fn

    lp = types.ModuleType("latent_preview")

    def prepare_callback(model, steps):
        calls["cb_steps"] = steps
        return "CB"

    lp.prepare_callback = prepare_callback

    saved = {k: sys.modules.get(k) for k in
             ("comfy", "comfy.sample", "comfy.utils", "latent_preview")}
    sys.modules["comfy"] = comfy
    sys.modules["comfy.sample"] = sample
    sys.modules["comfy.utils"] = utils
    sys.modules["latent_preview"] = lp
    comfy.sample = sample
    comfy.utils = utils

    def restore():
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return calls, restore


def test_ksampler_none_sigmas_matches_official():
    sa = _load("h3_sigmas_adapter4", "sigmas_adapter.py")
    calls, restore = _install_fake_comfy()
    try:
        latent = {"samples": "LATENT"}
        out = sa.ksampler_with_sigmas("MODEL", 42, 25, 1.0, "euler", "simple",
                                      "POS", "NEG", latent, sigmas=None, denoise=1.0)[0]
        assert out["samples"] == "SAMPLED"
        assert calls["sample"]["sigmas"] is None      # 与官方 common_ksampler 一致
        assert calls["sample"]["steps"] == 25
        assert calls["cb_steps"] == 25                # 进度步数 = 控件步数
        assert calls["noise"] == (42, None)
    finally:
        restore()


def test_ksampler_custom_sigmas_overrides():
    sa = _load("h3_sigmas_adapter5", "sigmas_adapter.py")
    calls, restore = _install_fake_comfy()
    try:
        sig = FakeSigmas(HF_SIGMAS)
        latent = {"samples": "LATENT"}
        sa.ksampler_with_sigmas("MODEL", 42, 25, 1.0, "euler", "simple",
                                "POS", "NEG", latent, sigmas=sig, denoise=1.0)[0]
        assert calls["sample"]["sigmas"] is sig       # 表原样传下去（步数/调度器失效）
        assert calls["cb_steps"] == 8                 # 进度步数按 sigma 间隔数
    finally:
        restore()


# ---- schema：新槽必须是最后一个 input，且不产生 widget ----

def test_sigmas_input_is_last_and_link_only():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute)):
            continue
        if f.value.attr != "Sigmas" or f.attr != "Input":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            names.append(node.args[0].value)
        # optional=True 必须是字面量 True（写成 True 以外的值就不是可选槽）
        kw = {k.arg: k.value for k in node.keywords}
        assert isinstance(kw.get("optional"), ast.Constant) and kw["optional"].value is True
    assert names == ["自定义Sigmas"], names

    # 位置：必须在最后一个 widget 型控件「响度对齐强度」之后
    i_last_widget = src.rindex('io.Float.Input("响度对齐强度"')
    i_sigmas = src.index('io.Sigmas.Input("自定义Sigmas"')
    assert i_sigmas > i_last_widget


def test_no_widget_count_change_for_sigmas_slot():
    """纯连线槽不进 widgets_values：主节点控件值仍应是 30（迁移层判据依赖它）。

    30 = 删「一采编码」后的当前布局（2026-09-23）；sigmas 是纯输入槽，
    不该在 widgets_values 里占位，所以这个数只随真正的控件增删而变。
    """
    wf_path = os.path.join(ROOT, "web", "h3_default_workflow.js")
    import json
    import re
    src = open(wf_path, encoding="utf-8").read()
    m = re.search(r"window\.H3_DEFAULT_WORKFLOW\s*=\s*(\{.*\})\s*;?\s*$", src, re.S)
    wf = json.loads(m.group(1)) if m else json.loads(src)
    sampler = [n for n in wf["nodes"] if n["type"] == "H3SeamlessChainSampler"][0]
    assert len(sampler["widgets_values"]) == 30
