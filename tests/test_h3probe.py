"""显存探针（h3probe.py）的契约测试。

探针是**诊断工具**，但它的失败模式很危险：它挂在 `comfy.model_management` 与
`comfy.sd.VAE` 上，一旦自己抛异常，就会把整个插件（乃至 ComfyUI 的采样链）带崩。
所以这里钉三条：

1. **模块级不许 import comfy** —— 否则 ComfyUI 版本不匹配时，探针会变成插件的硬依赖。
2. **`install()` 永不抛异常**，且在无 comfy 的环境里也要能安全返回。
3. **`install()` 幂等** —— 重复调用只生效一次（ComfyUI 可能多次加载入口）。
"""

import ast
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "h3probe.py")
INIT = os.path.join(ROOT, "__init__.py")


def _load(monkeypatch=None, mode=None):
    """按需设 `H3PROBE` 后重新加载一份**全新**的探针模块（模块级状态随之重置）。"""
    if monkeypatch is not None:
        if mode is None:
            monkeypatch.delenv("H3PROBE", raising=False)
        else:
            monkeypatch.setenv("H3PROBE", mode)
    spec = importlib.util.spec_from_file_location("h3probe_under_test", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_has_no_module_level_comfy_import():
    """模块级不许 import comfy / comfy_aimdo / torch。

    探针要在 ComfyUI 启动早期就能加载，且必须能在「没有 comfy」的环境里被单测。
    所有 comfy 依赖都必须在函数内部 import。
    """
    tree = ast.parse(open(PROBE, encoding="utf-8").read())
    banned = ("comfy", "comfy_aimdo", "torch")
    for node in tree.body:  # 只看模块顶层
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] not in banned, f"模块级 import {a.name}"
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned, (
                f"模块级 from {node.module} import ...")


def test_install_never_raises_without_comfy(monkeypatch):
    """无 comfy 环境下 install() 必须安全返回 —— 探针不许把插件带崩。"""
    mod = _load(monkeypatch)
    assert mod.install() is True
    mod.snapshot("无 comfy 快照")  # 也不许抛


def test_install_is_idempotent(monkeypatch):
    """重复 install() 只生效一次（第二次返回 False）。"""
    mod = _load(monkeypatch)
    assert mod.install() is True
    assert mod.install() is False


def test_off_mode_installs_nothing(monkeypatch):
    """H3PROBE=0 时完全不挂钩子。"""
    mod = _load(monkeypatch, mode="0")
    assert mod.install() is False


def test_default_mode_is_auto_not_off(monkeypatch):
    """默认口径是 auto（挂钩子），不是 off —— 否则用户「更新即用」会看到一片空白。"""
    mod = _load(monkeypatch)
    assert mod._DEFAULT_MODE == "auto"
    assert mod._OFF is False


def test_verbose_switch(monkeypatch):
    for val, expect in [("1", True), ("verbose", True), ("auto", False),
                        ("0", False), ("off", False)]:
        mod = _load(monkeypatch, mode=val)
        assert mod._VERBOSE is expect, val


def test_size_txt_renders_sentinel_as_all():
    """1e30 是 ComfyUI 的「全部卸载」哨兵值，渲染成「全部」而不是天文数字。"""
    mod = _load()
    assert mod._size_txt(1e30) == "全部"
    assert mod._size_txt(0) == "0.00GB"
    assert mod._size_txt(2 * 1024 ** 3) == "2.00GB"


def test_need_txt_says_need_not_release():
    """`需要 X 可用` —— X 是上游 memory_required，不能读成「要腾出 X」。"""
    mod = _load()
    assert mod._need_txt(1e30) == "全部显存"
    assert mod._need_txt(2 * 1024 ** 3) == "2.00GB 可用"


def test_num_never_pretends_nan_is_zero():
    """读数失败必须显示 `?` —— 冒充 0 会被读成「什么都没释放」。"""
    mod = _load()
    assert mod._num(float("nan")) == "?"
    assert mod._num(None) == "?"
    assert mod._num(1.5) == "1.50"


def test_notable_free_keeps_the_real_signals(monkeypatch):
    """真卸了模型 / 非动态路径的老判据 / 动态路径真还回了 —— 一定要出声。"""
    mod = _load(monkeypatch)
    gb = 1024 ** 3
    assert mod._notable_free(0.1 * gb, 0.0, 0, False) is True        # 责任真空（非动态）
    assert mod._notable_free(0.1 * gb, 0.1 * gb, 0, False) is False  # 非动态且真释放、额度也小
    assert mod._notable_free(0.1 * gb, 0.0, 2, True) is True         # 真卸了 2 个
    assert mod._notable_free(0.1 * gb, 0.5 * gb, 0, True) is True    # 动态路径真还回了
    # unload_all_models 那条关键行：哨兵额度 + for_dynamic=False，绝不能被压掉
    assert mod._notable_free(1e30, 15.77 * gb, 4, False) is True


def test_notable_free_pressure_gate(monkeypatch):
    """动态路径 + 没还回：只在设备吃紧时出声，例行问价必须压掉。"""
    mod = _load(monkeypatch)
    gb = 1024 ** 3
    total = 24 * gb
    # 设备空着、要 29GB、卸 0 个、释放 0 —— 例行问价，压掉
    assert mod._notable_free(29 * gb, 0.0, 0, True,
                             used_gb=0.3 * gb, total_gb=total) is False
    # 设备 90% —— 这才是「腾不出来」的现场
    assert mod._notable_free(11 * gb, 0.0, 0, True,
                             used_gb=21.6 * gb, total_gb=total) is True
    # 读数缺失 → 不猜、不出声（否则退化成旧版的刷屏）
    assert mod._notable_free(11 * gb, 0.0, 0, True) is False
    # 全量模式下不再过滤
    verbose = _load(monkeypatch, mode="1")
    assert verbose._notable_free(11 * gb, 0.0, 0, True) is True


def test_plugin_entry_wires_the_probe():
    """插件入口必须调用探针 —— 否则「更新版本后直接使用」不成立。"""
    init = open(INIT, encoding="utf-8").read()
    assert "from .h3probe import" in init, "h3probe 没被 __init__ 导入"
    assert "_install_h3probe()" in init, "h3probe.install() 没被调用"


def test_probe_import_does_not_disturb_frozen_node_list():
    """探针的引入不许改变 get_node_list 的解析结果（test_workflows_frozen 的口径）。

    `test_workflows_frozen` 用**第一个** `return [...]` 取节点列表；探针代码
    一旦在里面引入 `return [`，那条测试会静默取到错的列表。
    """
    import re
    init = open(INIT, encoding="utf-8").read()
    m = re.search(r"return \[(.*?)\]", init, re.S)
    assert m, "get_node_list return not found"
    listed = m.group(1)
    assert "H3SeamlessChainSampler" in listed and "H3SeamDocto" in listed
    assert "_REPORT_NODES" in init[m.end():m.end() + 200]
