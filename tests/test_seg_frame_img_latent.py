"""段级首尾帧参考图：列表里装的是**裸张量**，判「有没有」不许用裸 any()。

线上报错（2026-10-11，客户 ComfyUI 0.38.0 / RTX 4070 Ti SUPER）：

    RuntimeError: Boolean value of Tensor with more than one value is ambiguous
    nodes.py: if any(seg_head_img_latent) or any(seg_end_img_latent):

根因：本仓 `video_vae.encode()` 返回**裸 Tensor**（上游 `comfy/sd.py` 的
`VAE.encode` 末尾是 `return samples`；H3 视频 VAE 没有 `comfy_format_encoded`，
所以 `format_encoded` 保持 None，不走 dict 包装）。而 `any(seq)` 内部会对每一项
执行 `bool(x)`；PyTorch **刻意**让多元素张量的 `bool()` 抛异常（真值不明确），
于是只要用户真在段卡里挂了「首帧图/尾帧图」就必崩（耗时 2–8s，与显存无关）。

列表全为 None 时 `any()` 短路返回 False → 不挂图时静默通过，所以这个 bug 从
引入（719765d，2026-09-13）一直躺到有用户真用该功能。

修法：判「有没有」一律 `is not None`（与紧邻的报告文案用的是同一判据）。
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "nodes.py"

# 这两个列表的元素是 video_vae.encode() 的裸张量，绝不能做真值判断
LATENT_LISTS = {"seg_head_img_latent", "seg_end_img_latent"}
TRUTHINESS_CALLS = {"any", "all"}


def _tree():
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _bare_name_args(tree, list_names, func_names):
    """找出 `any(列表名)` / `all(列表名)` 这种把整个列表丢进去的调用点。"""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id in func_names):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id in list_names:
                hits.append((node.func.id, arg.id, node.lineno))
    return hits


def _iterates_over(iter_node, name):
    """iter_node 是否在遍历 name —— 直接遍历，或 `enumerate(name)` 包装。"""
    if isinstance(iter_node, ast.Name):
        return iter_node.id == name
    if (isinstance(iter_node, ast.Call) and isinstance(iter_node.func, ast.Name)
            and iter_node.func.id == "enumerate" and iter_node.args):
        inner = iter_node.args[0]
        return isinstance(inner, ast.Name) and inner.id == name
    return False


def _is_not_none_judgements(tree, name):
    """找出「遍历 name 且带 `is not None` 判据」的推导式/生成器所在行号。"""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            continue
        if not any(_iterates_over(g.iter, name) for g in node.generators):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Compare) and any(
                    isinstance(op, ast.IsNot) for op in sub.ops):
                hits.append(node.lineno)
                break
    return hits


def test_no_bare_any_over_latent_lists():
    """seg_head/end_img_latent 不得整体交给 any()/all()（元素是张量，bool() 会抛）。"""
    hits = _bare_name_args(_tree(), LATENT_LISTS, TRUTHINESS_CALLS)
    assert not hits, (
        f"nodes.py 行 {sorted(h[2] for h in hits)} 把张量列表直接交给 "
        f"{sorted({h[0] for h in hits})}() —— 多元素张量取真值会抛 "
        "RuntimeError: Boolean value of Tensor with more than one value is ambiguous。"
        "请改写成 any(v is not None for v in 列表)")


def test_guard_uses_is_not_none():
    """两个列表都必须存在 `... if v is not None` 判据（真值判断的唯一合法替身）。"""
    tree = _tree()
    for name in sorted(LATENT_LISTS):
        assert _is_not_none_judgements(tree, name), (
            f"找不到对 {name} 的 `is not None` 判据 —— "
            "「有没有」必须用 is not None，不能靠真值判断")


class _MultiElementTensor:
    """最小模型：多元素张量的 bool() 抛 RuntimeError（PyTorch 的刻意设计）。"""

    def __bool__(self):
        raise RuntimeError(
            "Boolean value of Tensor with more than one value is ambiguous")


def test_truthiness_trap_and_fix():
    """复现报错语义：裸 any() 必炸，`is not None` 版本安全。"""
    seq = [None, _MultiElementTensor()]
    with pytest.raises(RuntimeError, match="ambiguous"):
        any(seq)
    assert any(v is not None for v in seq) is True
    assert any(v is not None for v in [None, None]) is False
