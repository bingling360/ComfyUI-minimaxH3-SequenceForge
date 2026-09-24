"""语义桥（semantic_bridge.py）单测 + 接线守卫。

分两类：
  · **行为单测**：config 归一、权重路径校验、目录扫描、作用范围掩码口径。
    在无 torch 环境下注入最小 stub 跑（本模块顶层只有 `import torch` / `torch.nn`，
    纯逻辑测试不碰任何张量运算）。
  · **接线守卫**（源码断言）：本项目历史上反复栽在「开关建好了、但没接到渲染链上」
    （force_unload 的正面条件恒假、render_latent 漏 _vram 形参……）。这里把
    「一采接了、二采也接了、进指纹了、路由挂上了」四件事钉死。

跑法（仓库根目录）：
    python -m pytest tests/test_semantic_bridge.py -q
"""

import importlib.util
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:  # 优先用真实 torch（ComfyUI / .venv 环境）
    import torch as _torch
    _REAL_TORCH = True
except Exception:  # pragma: no cover - 无 torch 时走 stub
    _torch = None
    _REAL_TORCH = False


class _StubBase(object):
    pass


def _stub_torch():
    """最小替身：只够把模块**加载**起来（类定义要 nn.Module，其余只在调用时用）。"""
    m = types.ModuleType("torch")
    nn = types.ModuleType("torch.nn")
    nn.Module = _StubBase
    nn.Linear = lambda *a, **k: None
    nn.SiLU = lambda *a, **k: None
    m.nn = nn
    m.Tensor = type("Tensor", (object,), {})
    return m, nn


def _load_bridge():
    name = "h3sf_semantic_bridge_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "semantic_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    if _REAL_TORCH:
        spec.loader.exec_module(mod)
        return mod
    stub, nn = _stub_torch()
    prev_t, prev_n = sys.modules.get("torch"), sys.modules.get("torch.nn")
    sys.modules["torch"], sys.modules["torch.nn"] = stub, nn
    try:
        spec.loader.exec_module(mod)
    finally:
        # 加载完立刻摘掉替身：留着会污染同会话里其它 `import torch` 的测试
        # （模块全局已绑定引用，摘掉不影响本模块的后续使用）。
        if prev_t is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = prev_t
        if prev_n is None:
            sys.modules.pop("torch.nn", None)
        else:
            sys.modules["torch.nn"] = prev_n
    return mod


BR = _load_bridge()


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _func_body(src, name):
    """取**顶层**函数体：从 `def name(` 到下一个列 0 的 `def`。

    为什么不用 `split("def name(")`：本项目里 `def X(` 可能先出现在注释/文档串里，
    或者被同名函数的调用点抢先命中 —— upscale.py 就这么坑过一次
    （`render_segment` 的源码位置在 `render_latent` **之后**，而 cond 是在
    `render_latent` 里建的，按名字 split 会取错函数体）。
    """
    i = src.index("\ndef %s(" % name)
    j = src.find("\ndef ", i + 1)
    return src[i:j if j > 0 else len(src)]


# ══════════════════ 常量契约 ══════════════════

def test_dim_is_h3_text_encoder_width():
    """5120 是 H3 文本编码器输出维（comfy/text_encoders/minimax.py embedding_size=5120）。
    写错就是整个功能静默失效（权重塞不进去）或尺寸断言失效。"""
    assert BR.DIM == 5120


def test_defaults_match_the_agreed_starting_point():
    """alpha 0.15、全量过桥、per_token 幅度对齐 —— 用户拍板的起始配置。"""
    assert BR.DEFAULT_ALPHA == 0.15
    assert BR.DEFAULT_SCOPE == "all"
    assert BR.MAGNITUDE_MATCH == "per_token"
    assert tuple(BR.SCOPES) == ("all", "text")


def test_frontend_scope_table_matches_backend():
    """前端 BRIDGE_SCOPES 必须与后端 SCOPES 同表 —— 两套口径会让面板选得出、
    后端认不得（回落成 all），用户以为切了「仅文本」其实没切。"""
    src = _read("web/h3_director.js")
    m = re.search(r'BRIDGE_SCOPES\s*=\s*\[([^\]]*)\]', src)
    assert m, "前端必须显式声明 BRIDGE_SCOPES"
    js = [x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()]
    assert js == list(BR.SCOPES), f"前端 {js} != 后端 {list(BR.SCOPES)}"


def test_frontend_default_alpha_matches_backend():
    """前端 defaultBridge 的 alpha 默认值必须等于后端 DEFAULT_ALPHA，
    否则「没碰过这个控件」的项目在前端显示 0.15、后端实际跑另一个数。"""
    src = _read("web/h3_director.js")
    body = src.split("function defaultBridge()", 1)[1].split("\n}", 1)[0]
    m = re.search(r"alpha:\s*([0-9.]+)", body)
    assert m, "defaultBridge 必须显式给出 alpha"
    assert float(m.group(1)) == BR.DEFAULT_ALPHA


# ══════════════════ config 归一 ══════════════════

def test_config_defaults_when_missing():
    cfg = BR.config({})
    assert cfg == {"enabled": False, "adapter": "", "alpha": 0.15, "scope": "all"}


@pytest.mark.parametrize("ds", [None, {}, {"bridge": None}, {"bridge": "x"},
                                {"bridge": 7}, {"bridge": []}])
def test_config_survives_junk_and_stays_off(ds):
    """旧档 / 手改坏的 JSON 一律收敛到「关闭」，绝不半开。"""
    assert BR.config(ds)["enabled"] is False


def test_config_requires_real_true_for_enabled():
    """只认真 True（与前端 `=== true` 同口径）。

    不能用 `bool()` 的原因就在这里：`"enabled": "false"` 是非空字符串 → bool 为真
    → 一个「手改成字符串的假值」会把整个桥静默打开，代价是整链重做。"""
    assert BR.config({"bridge": {"enabled": "true"}})["enabled"] is False
    assert BR.config({"bridge": {"enabled": "false"}})["enabled"] is False
    assert BR.config({"bridge": {"enabled": 1}})["enabled"] is False
    assert BR.config({"bridge": {"enabled": True}})["enabled"] is True


def test_config_clamps_alpha():
    assert BR.config({"bridge": {"alpha": 9}})["alpha"] == 1.0
    assert BR.config({"bridge": {"alpha": -4}})["alpha"] == 0.0
    assert BR.config({"bridge": {"alpha": 0.4}})["alpha"] == 0.4


def test_config_falls_back_on_unknown_scope():
    assert BR.config({"bridge": {"scope": "bogus"}})["scope"] == "all"
    assert BR.config({"bridge": {"scope": "text"}})["scope"] == "text"


def test_config_strips_adapter_whitespace():
    assert BR.config({"bridge": {"adapter": "  a.safetensors "}})["adapter"] == "a.safetensors"


# ══════════════════ 权重路径与扫描 ══════════════════

def test_scan_adapters_lists_only_safetensors():
    names = BR.scan_adapters()
    assert all(n.lower().endswith(".safetensors") for n in names)
    assert names == sorted(names, key=str.lower), "列表要稳定排序（下拉顺序不能每次刷新都变）"


def test_scan_adapters_finds_the_bundled_weight():
    """插件自带权重必须被扫到 —— 扫不到的话用户开了开关也只会得到「权重不存在」。"""
    assert any("BUNNY" in n for n in BR.scan_adapters()), \
        f"models/ 下没扫到权重：{BR.scan_adapters()}"


def test_path_of_accepts_bundled_name():
    name = [n for n in BR.scan_adapters() if "BUNNY" in n][0]
    assert os.path.isfile(BR.path_of(name))


@pytest.mark.parametrize("bad", ["", "   ", None, "../secrets.safetensors",
                                 "sub/dir.safetensors", "..\\win.safetensors",
                                 "/abs/path.safetensors", "C:\\x.safetensors"])
def test_path_of_rejects_empty_and_traversal(bad):
    """只认 models/ 下的裸文件名：空名给「未选择」的明确文案，带路径一律拒。"""
    with pytest.raises(ValueError):
        BR.path_of(bad)


def test_path_of_rejects_missing_file():
    with pytest.raises(ValueError):
        BR.path_of("no_such_weight_here.safetensors")


def test_empty_adapter_message_tells_user_what_to_do():
    with pytest.raises(ValueError) as e:
        BR.path_of("")
    assert "未选择权重文件" in str(e.value)


# ══════════════════ 作用范围掩码 ══════════════════

def test_text_slots_picks_only_text_tag():
    # 1 = 文本，0 = 视觉块
    assert BR.text_slots([0, 0, 1, 1, 1, 0]) == [2, 3, 4]


def test_text_slots_all_vision_or_all_text():
    assert BR.text_slots([0, 0, 0]) == []
    assert BR.text_slots([1, 1]) == [0, 1]
    assert BR.text_slots([]) == []


def test_text_tag_constant_is_the_official_one():
    """官方 token_tags_from_embeds_info：1=文本、0=视觉块。写反了就是「仅文本」
    实际只改图片、文本一个不动 —— 静默错到没法察觉。"""
    assert BR.TEXT_TAG == 1


# ══════════════════ 接线守卫（源码断言） ══════════════════

def test_nodes_applies_bridge_to_base_cond():
    src = _read("nodes.py")
    assert "semantic_bridge.apply(cond, _bridge_cfg)" in src, \
        "一采必须过语义桥，否则开了开关等于没开"


def test_nodes_reads_bridge_config_from_ds():
    src = _read("nodes.py")
    assert "semantic_bridge.config(ds)" in src, \
        "配置必须从 ds 读（逐项目），不能另起一个全局源"


def test_nodes_passes_bridge_to_render_segment():
    src = _read("nodes.py")
    assert "bridge=_bridge_cfg" in src, "二采必须拿到同一份配置"


def test_render_segment_call_still_ends_with_up_swap():
    """回归：既有守卫断言调用以 `_up_swap=_up_swap)` 结尾。
    插入 bridge= 时若放在它后面，那条守卫会红 —— 这里把它钉住，免得下次又被顶掉。"""
    src = _read("nodes.py")
    assert "_up_swap=_up_swap)" in src


def test_upscale_applies_bridge_after_cond_is_built():
    """二采的 cond 是在 **render_latent** 里重新编码出来的（高清画幅），
    所以过桥必须发生在那里、且在那句 cond 诞生之后。"""
    body = _func_body(_read("upscale.py"), "render_latent")
    i_cond = body.index("cond, latent = out[0], out[1]")
    i_apply = body.index("semantic_bridge.apply(cond, bridge)")
    assert i_apply > i_cond, "语义桥必须作用在二采重建出来的 cond 上"


def test_bridge_param_declared_on_both_upscale_entry_points():
    src = _read("upscale.py")
    for fn in ("render_latent", "render_segment"):
        sig = src.split("def %s(" % fn, 1)[1].split("):", 1)[0]
        assert "bridge=None" in sig, "%s 必须接收 bridge（默认 None = 旧调用行为不变）" % fn


def test_render_segment_forwards_bridge_to_render_latent():
    """历史同类 bug：render_segment 一直在传 `_vram=_vram`，而 render_latent 的签名里
    漏了这两个形参 —— 每次二采都在调用处 TypeError（埋点从没在真机跑通过）。
    桥同理：只在 render_segment 上声明形参而不往下传，等于二采从没接过桥。"""
    body = _func_body(_read("upscale.py"), "render_segment")
    call = body[body.index("render_latent("):][:800]
    assert "bridge=bridge" in call, "必须把 bridge 透传给 render_latent"


def test_bridge_only_enters_fingerprint_when_enabled():
    """只在开启时写 ckpt_params：未启用不加键 —— 否则既有项目的存档全部续不上
    （json.dumps(sort_keys) 下「多一个键」就是新指纹）。"""
    src = _read("nodes.py")
    i = src.index('ckpt_params["bridge"]')
    assert 'if _bridge_cfg["enabled"]:' in src[max(0, i - 400):i], \
        "bridge 必须包在 enabled 条件里才进指纹"


def test_bridge_route_registered_both_ways():
    """自定义路由要同时挂进 ROUTES 与 handler 映射，否则要么探测不到、要么 404。"""
    src = _read("routes.py")
    assert src.count('"/h3chain/bridge_models"') == 2, \
        "ROUTES 与 handler 映射各要登记一次 /h3chain/bridge_models"


def test_route_lists_plugin_models_dir():
    src = _read("routes.py")
    i = src.index("async def bridge_models")
    assert "scan_adapters" in src[i:i + 400], "路由必须走 semantic_bridge.scan_adapters"
