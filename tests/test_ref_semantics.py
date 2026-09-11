"""显性软引用语义回归（AST 抽取 shipped 纯函数真跑，无 ComfyUI 可跑）。

跑法（仓库根目录）：
    python -m pytest tests/test_ref_semantics.py -q

覆盖：最小映射块形态、三类独立编号、缺 tag 精确补齐（含
<Picture 1>/<Picture 10> 子串陷阱）、已写 tag 直通无注入、
未知 [[..]] 点名报错、旧英文长文 header 已删除。
"""

import ast
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WANT_FUNCS = ("_normalize_order", "_kind_tokens", "_apply_label_tokens",
               "_reference_tags_minimal", "_uncovered_tags")


@pytest.fixture(scope="session")
def kern():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FUNCS:
            wanted.append(node)
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_LABEL_TOKEN" for t in node.targets):
            wanted.append(node)
    assert {n.name for n in wanted if isinstance(n, ast.FunctionDef)} == set(_WANT_FUNCS)
    ns = {"re": re}
    exec(compile(ast.Module(body=wanted, type_ignores=[]),
                 "nodes.py:ref-kernel", "exec"), ns)
    return types.SimpleNamespace(**{k: ns[k] for k in list(_WANT_FUNCS) + ["_LABEL_TOKEN"]})


def test_minimal_block_shape(kern):
    out = kern._reference_tags_minimal(
        [("image", "角色1"), ("video", "片"), ("audio", "乐")])
    assert out.splitlines() == [
        "[References]",
        "<Picture 1> = 角色1",
        "<Video 1> = 片",
        "<Audio 1> = 乐",
    ]
    assert "subject_definitions" not in out and "is the" not in out


def test_missing_tags_filled(kern):
    order = [("image", "角色1"), ("video", "片"), ("audio", "乐")]
    full = kern._apply_label_tokens("她走过街道 [[角色1]]", order)
    assert "<Picture 1>" in full
    mapping = kern._kind_tokens(kern._normalize_order(order))
    uncovered = kern._uncovered_tags(full, mapping)
    assert uncovered == ["片", "乐"]
    head = kern._reference_tags_minimal(
        [(k, lbl) for k, lbl in kern._normalize_order(order) if lbl in uncovered])
    assert "<Video 1> = 片" in head and "<Audio 1> = 乐" in head
    assert "角色1" not in head.splitlines()[1]  # 已覆盖的不重复声明


def test_picture_prefix_trap(kern):
    # "<Picture 1>" 是 "<Picture 10>" 的子串：只写了后者时前者必须判为缺失
    mapping = {"图1": "<Picture 1>", "图10": "<Picture 10>"}
    assert kern._uncovered_tags("看 <Picture 10> 里的猫", mapping) == ["图1"]
    assert kern._uncovered_tags("看 <Picture 1> 和 <Picture 10>", mapping) == []


def test_all_typed_passthrough(kern):
    order = [("image", "A")]
    full = kern._apply_label_tokens("见 [[A]]", order)
    mapping = kern._kind_tokens(kern._normalize_order(order))
    assert kern._uncovered_tags(full, mapping) == []


def test_unknown_label_points(kern):
    with pytest.raises(ValueError, match="未知素材标签"):
        kern._apply_label_tokens("见 [[谁]]", [("image", "A")])


def test_old_header_gone():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert "def _reference_header" not in src
    assert "_reference_header(" not in src
    assert "def _reference_tags_minimal" in src
    assert "_uncovered_tags(full, mapping)" in src
