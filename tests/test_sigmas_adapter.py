"""自定义 sigma 表适配（少步蒸馏 LoRA 通道）回归。

跑法（仓库根目录）：
    python -m pytest tests/test_sigmas_adapter.py -q

钉死三件事：
1. 未接 sigmas 时存档指纹**与旧公式完全一致**（既有项目必须能续上）；
2. 接了 sigmas 后换表 = 换指纹（否则换 sigma 表会静默复用旧段）；
3. ksampler_with_sigmas 的 sigmas=None 分支与官方 common_ksampler 同参，
   非 None 分支把表传下去、且进度步数按 sigma 点数算。

另钉二采通道（2026-10-12 新增「二采自定义Sigmas」槽）：
4. 两个 sigmas 槽都在末尾、都是纯连线槽（不进 widgets_values）；
5. nodes.py → render_segment → render_latent → 精化采样 四段接线一个不漏；
6. 二采表指纹「接了才进」params_hash，且让位项（passes/retry/scheduler）退出指纹。
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
    """够用的 tensor 桩：detach/flatten/tolist/shape/下标。"""

    def __init__(self, vals):
        self._v = list(vals)
        self.shape = (len(vals),)

    def detach(self):
        return self

    def flatten(self):
        return self

    def tolist(self):
        return list(self._v)

    def __getitem__(self, i):
        return self._v[i]


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


def test_sigmas_start():
    """表首值 = 本表的实际 σ 起点（二采的 STG 激活窗口与报告回显都取它）。"""
    sa = _load("h3_sigmas_adapter_start", "sigmas_adapter.py")
    assert sa.sigmas_start(None) is None
    assert sa.sigmas_start(FakeSigmas([0.35, 0.2, 0.0])) == 0.35
    # 坏表（取不出首值）返回 None —— 调用方据此回落到面板 σ₀，而不是崩掉
    assert sa.sigmas_start(object()) is None


def test_sigmas_usable():
    """「表真能用吗」的唯一判据（点数 ≥ 2）。写指纹与按表采样必须同用它。"""
    sa = _load("h3_sigmas_adapter_usable", "sigmas_adapter.py")
    assert sa.sigmas_usable(None) is False
    assert sa.sigmas_usable(FakeSigmas([])) is False        # 空表
    assert sa.sigmas_usable(FakeSigmas([0.5])) is False     # 1 点 = 0 步
    assert sa.sigmas_usable(FakeSigmas([0.35, 0.0])) is True
    assert sa.sigmas_usable(FakeSigmas(HF_SIGMAS)) is True
    assert sa.sigmas_usable(object()) is False              # 坏表（取不出 shape）


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


# ---- schema：两个 sigmas 槽都必须是末尾的纯连线槽，且不产生 widget ----

def test_sigmas_inputs_are_last_and_link_only():
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
    assert names == ["一采自定义Sigmas", "二采自定义Sigmas"], names

    # 位置：两个槽都必须在最后一个 widget 型控件「响度对齐强度」之后，且二采槽
    # 紧跟一采槽 —— 位次是输入连线的坐标（前端按位次重建），旧工作流里接在
    # 「自定义Sigmas」上的线要靠「一采槽仍占原来那个位次」才不会落空。
    i_last_widget = src.rindex('io.Float.Input("响度对齐强度"')
    i_1 = src.index('io.Sigmas.Input("一采自定义Sigmas"')
    i_2 = src.index('io.Sigmas.Input("二采自定义Sigmas"')
    assert i_1 > i_last_widget
    assert i_2 > i_1


def test_second_pass_channel_is_wired_end_to_end():
    """二采通道的四个接线点，少任何一个这张表都白接（静默失效）。

    历史同类 bug：render_segment 声明了形参却没往下传（埋点 `_vram` 就这么漏过），
    或 nodes.py 没把张量传下去（开关恒为默认值）。
    """
    up = open(os.path.join(ROOT, "upscale.py"), encoding="utf-8").read()
    nd = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()

    # ① 两个入口都声明 sigmas 形参
    for fn in ("render_latent", "render_segment"):
        sig = up.split("def %s(" % fn, 1)[1].split("):", 1)[0]
        assert "sigmas=None" in sig, "%s 必须接收 sigmas（默认 None = 旧调用行为不变）" % fn
    # ② render_segment 透传给 render_latent
    body = up.split("def render_segment(", 1)[1]
    call = body[body.index("render_latent("):][:900]
    assert "sigmas=sigmas" in call, "必须把 sigmas 透传给 render_latent"
    # ③ 精化真的走 sigmas_adapter（而不是仍用 common_ksampler）
    assert "sigmas_adapter.ksampler_with_sigmas(" in up
    assert "sigmas=_ref_sigmas, denoise=ks_denoise" in up
    # ④ nodes.py 把表传下去，并把表指纹写进 cfg（否则 record_stale 判不出「换表=重做」）
    assert "sigmas=二采自定义Sigmas," in nd, "nodes.py 必须把二采表传给 render_segment"
    assert 'up_cfg["sigmas"] = _sigmas_adapter.fingerprint_tag(二采自定义Sigmas)' in nd, \
        "二采表指纹必须写进 up_cfg（否则换表不会触发高清段重做）"


def test_second_pass_fingerprint_only_when_wired():
    """未接表时不加键（既有项目高清记录续不上就是事故）；接了才加。

    顺带钉住「让位项退出指纹」：接了表之后 passes/decay、retry、scheduler 在采样侧
    已失效，拨它们不该触发整链高清重做。
    """
    src = open(os.path.join(ROOT, "upscale.py"), encoding="utf-8").read()
    i = src.index('keys["sigmas"] = cfg["sigmas"]')
    assert "if _has_sig:" in src[max(0, i - 300):i], "sigmas 键必须包在「接了才加」的条件里"
    assert "_has_sig = bool(cfg.get(\"sigmas\"))" in src
    assert src.count("and not _has_sig") >= 3, \
        "passes/decay、retry、scheduler 三项都要在接表时退出指纹"


# ---- 两个边界：只接一采表 / 只接二采表但二采没开（2026-10-12 复核） ----

def test_first_pass_table_never_leaks_into_second_pass():
    """边界①：只接「一采自定义Sigmas」时，二采必须**零影响**。

    一采表的张量只在 nodes.py 的一采采样点用（`sigmas=一采自定义Sigmas`），
    绝不进 upscale 的二采通道；二采指纹也只认 `up_cfg["sigmas"]`（由**二采**槽写入）。
    最强的钉法：upscale.py 里根本不该出现「一采自定义Sigmas」这个名字 ——
    它一旦出现，就说明一采表渗进了二采。
    """
    nd = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    up = open(os.path.join(ROOT, "upscale.py"), encoding="utf-8").read()
    assert "一采自定义Sigmas" not in up, \
        "一采表不得渗进二采通道（upscale.py 只该认 sigmas 形参与 cfg['sigmas']）"
    # 写进 cfg["sigmas"] 的只能来自二采槽
    i = nd.index('up_cfg["sigmas"] =')
    assert "二采自定义Sigmas" in nd[i:i + 120]
    assert "一采自定义Sigmas" not in nd[i:i + 120]
    # 一采采样点仍只吃一采表（二采表不得反向渗进一采）
    i2 = nd.index("sigmas=一采自定义Sigmas")
    assert "二采自定义Sigmas" not in nd[i2:i2 + 80]


def test_second_pass_table_is_inert_when_upscale_off():
    """边界②：只接「二采自定义Sigmas」但二采没开时，必须**零影响**。

    `up_cfg` 由 `upscale.parse_state(ds)` 给，二采关着就是 None → 写键那句在
    `if up_cfg:` 块内、根本执行不到；张量也没人传（render_segment 不会被调用）。
    另外要有一条报告说清「接了但没生效」，否则用户会以为它生效了。
    """
    nd = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    # 写键在 `if up_cfg:` 之内（缩进 12 空格 = 该块内；`if up_cfg:` 本身在 8）
    assert '\n            if _up_sig_usable:\n                up_cfg["sigmas"] =' in nd, \
        "写键必须嵌在 `if up_cfg:` 块里（二采关着时 up_cfg=None，键就不该存在）"
    # 「能不能用」的判据算在块外 —— 报告/提示在 up_cfg 为 None 时也要能用它
    assert '\n        _up_sig_usable = _sigmas_adapter.sigmas_usable(二采自定义Sigmas)' in nd
    # 未生效必须有两句明确提示（坏表 / 二采没开）
    assert "接了表但读不出有效步数" in nd
    assert "已接线但本次二采未启用" in nd
    # 二采通道的唯一入口受 up_cfg 守卫（None 时不会调用 render_segment）
    up = open(os.path.join(ROOT, "upscale.py"), encoding="utf-8").read()
    assert "if not (up_cfg and use_ckpt):" in nd, "二采入口必须受 up_cfg 守卫"
    # 二采槽没接时，二采采样仍是原路径（sigmas=None → common_ksampler 分支）
    assert "_ref_sigmas = sigmas if sigmas_adapter.sigmas_usable(sigmas) else None" in up


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
