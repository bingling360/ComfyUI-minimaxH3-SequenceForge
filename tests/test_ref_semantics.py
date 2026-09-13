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
               "_reference_tags_minimal", "_uncovered_tags",
               "_clean_frame_file", "_parse_frame_imgs")
# `@标签` 语法把原来的 _LABEL_TOKEN 拆成「括号写法 + @写法」两个正则
_WANT_VARS = ("_REF_BRACKET", "_REF_AT")


@pytest.fixture(scope="session")
def kern():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FUNCS:
            wanted.append(node)
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _WANT_VARS for t in node.targets):
            wanted.append(node)
    assert {n.name for n in wanted if isinstance(n, ast.FunctionDef)} == set(_WANT_FUNCS)
    ns = {"re": re}
    exec(compile(ast.Module(body=wanted, type_ignores=[]),
                 "nodes.py:ref-kernel", "exec"), ns)
    return types.SimpleNamespace(
        **{k: ns[k] for k in list(_WANT_FUNCS) + list(_WANT_VARS)})


def test_minimal_block_shape(kern):
    """官方 subject_definitions 句式：<Picture N> 自己当主语，后接「is the ...」。"""
    out = kern._reference_tags_minimal(
        [("image", "角色1"), ("video", "片"), ("audio", "乐")])
    assert out.splitlines() == [
        '<Picture 1> is the reference image "角色1".',
        '<Video 1> is the reference video "片".',
        '<Audio 1> is the reference audio "乐".',
    ]
    # 自造赋值表（`token = 别名` / [References] 头）已废止
    assert "[References]" not in out and " = " not in out


def test_missing_tags_filled(kern):
    order = [("image", "角色1"), ("video", "片"), ("audio", "乐")]
    full = kern._apply_label_tokens("她走过街道 [[角色1]]", order)
    assert "<Picture 1>" in full
    mapping = kern._kind_tokens(kern._normalize_order(order))
    uncovered = kern._uncovered_tags(full, mapping)
    assert uncovered == ["片", "乐"]
    head = kern._reference_tags_minimal(
        [(k, lbl) for k, lbl in kern._normalize_order(order) if lbl in uncovered])
    assert head.splitlines() == ['<Video 1> is the reference video "片".',
                                 '<Audio 1> is the reference audio "乐".']
    assert "角色1" not in head  # 已覆盖的不重复声明


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


def test_repeat_refs_keep_single_token(kern):
    """同一素材在一段里引用多次：编号仍只占一个 <Picture k>（重复=正文写多次）。"""
    order = [("image", "女主"), ("image", "女主"), ("image", "背景")]
    mapping = kern._kind_tokens(kern._normalize_order(order))
    assert mapping == {"女主": "<Picture 1>", "背景": "<Picture 2>"}
    full = kern._apply_label_tokens("开头 @女主 站着，结尾又回到 @女主", order)
    assert full.count("<Picture 1>") == 2      # 正文里出现两次 → 参考两次
    assert "<Picture 2>" not in full


def test_parse_frame_imgs(kern):
    """段级首尾帧参考图：{first,end} 相对路径，拒穿越。"""
    segs = [{"frame_img": {"first": "assets/a.png"}},
            {},
            {"frame_img": {"end": "assets\\b.png", "first": "../evil.png"}}]
    got = kern._parse_frame_imgs(segs, 3)
    assert got == [("assets/a.png", ""), ("", ""), ("", "assets/b.png")]
    # 段数不足时按 n 补齐；非法路径一律清空
    assert kern._parse_frame_imgs(segs, 1) == [("assets/a.png", "")]
    assert kern._clean_frame_file("a/b/c.png") == ""
    assert kern._clean_frame_file("assets/ok.png") == "assets/ok.png"


def test_old_header_gone():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert "def _reference_header" not in src
    assert "_reference_header(" not in src
    assert "def _reference_tags_minimal" in src
    assert "_uncovered_tags(full, mapping)" in src
