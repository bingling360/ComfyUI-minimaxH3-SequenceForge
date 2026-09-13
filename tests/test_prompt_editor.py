"""提示词富文本编辑器（内联绿框引用）回归。

跑法（仓库根目录）：
    python -m pytest tests/test_prompt_editor.py -q

覆盖：
1) `refsFromText` 真跑（抽出 shipped 源码交给 node 执行）：正文 → 引用集合，
   与后端 compile_refs / _find_refs 同口径（负向后顾、最长优先、允许重复计数）。
2) 源码断言：富文本编辑器、✕ 清全部引用、正文=唯一真相（setPromptText 同步 refs）、
   锚定方式（引用语）为常驻模式且带官方 retention 标记。
"""

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRECTOR = os.path.join(ROOT, "web", "h3_director.js")
NODE = shutil.which("node")


def _src():
    return open(DIRECTOR, encoding="utf-8").read()


def _extract_fn(src, name):
    """按大括号配平抽出函数源码（顶层或嵌套均可）。"""
    m = re.search(rf"^\s*function {re.escape(name)}\(", src, re.M)
    assert m, f"未找到函数 {name}"
    start = m.start()
    i = src.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"函数 {name} 大括号不配平")


@pytest.mark.skipif(NODE is None, reason="需要 node 执行前端纯函数")
def test_refs_from_text_runtime():
    """正文 → 引用集合：真跑（node），覆盖重复计数/最长优先/邮箱不误伤。"""
    code = "\n".join([
        _extract_fn(_src(), "escapeRegExp"),
        _extract_fn(_src(), "refsFromText"),
        """
        const pool = [{label: "女主"}, {label: "女主的家"}, {label: "背景"}];
        const cases = [
          ["开头 @女主 出现，结尾又回到 @女主", ["女主", "女主"]],
          ["场景 @女主的家 与 @女主 同段", ["女主的家", "女主"]],
          ["联系 a@女主.com 不算引用", []],
          ["@背景 一次", ["背景"]],
          ["没有引用", []],
          ["@不存在的标签", []],
        ];
        const out = cases.map(([t, want]) => {
          const got = refsFromText(t, pool);
          return { t, got, want, ok: JSON.stringify(got) === JSON.stringify(want) };
        });
        console.log(JSON.stringify(out));
        """,
    ])
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout.strip().splitlines()[-1])
    bad = [x for x in rows if not x["ok"]]
    assert not bad, f"refsFromText 行为不符：{bad}"


@pytest.mark.skipif(NODE is None, reason="需要 node 执行前端纯函数")
def test_serialize_roundtrip_runtime():
    """DOM → 纯文本：绿框还原成 @别名、<br>/块级元素还原成换行（真跑 node + 假 DOM）。"""
    code = "\n".join([
        _extract_fn(_src(), "ser"),
        """
        const T = (v) => ({ nodeType: 3, nodeValue: v, childNodes: [] });
        const E = (o) => Object.assign(
          { nodeType: 1, tagName: "SPAN", childNodes: [], dataset: {} }, o);
        const chip = (l) => E({ dataset: { label: l } });
        const br = E({ tagName: "BR" });
        const div = (kids) => E({ tagName: "DIV", childNodes: kids });
        const box = { childNodes: [
          T("开头 "), chip("女主"), T(" 与 "), chip("女主的家"), T(" 一起"),
          br, div([T("第二行")]),
        ] };
        console.log(JSON.stringify(ser(box)));
        """,
    ])
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got == "开头 @女主 与 @女主的家 一起\n第二行", got


def test_editor_source_points():
    d = _src()
    # 富文本编辑器 + 内联绿框 + ✕ 清全部
    assert "function createPromptEditor(opts)" in d
    assert 'classList.add("h3d-rtag")' not in d            # 绿框类由 className 赋值
    assert '"h3d-rtag"' in d and '"h3d-rtagx"' in d
    assert "取消本段对「${label}」的全部引用" in d
    # 编辑器对外暴露 textarea 兼容面（@补全/AI 优化/编译预览零改动）
    for api in ("insertText(text)", "insertTag", "removeTag", "tagCount",
                "get value()", "set value(v)", "setSelectionRange"):
        assert api in d, f"编辑器缺少兼容接口：{api}"
    # 正文是唯一真相：写回提示词时同步 refs
    assert "syncRefsFromText(ds, idx, text);" in d
    assert "function syncRefsFromText(ds, idx, text)" in d


def test_anchor_mode_source_points():
    d = _src()
    # 引用语 = 常驻「锚定方式」模式（选方式 → 点素材 → 按方式写入正文）
    assert "锚定方式：无（只插 @标签）" in d
    assert "const _refTpl = new Map()" in d
    assert "applyRefAnchorToV2(node, it.idx, a.label, tplDef || [], roles)" in d
    assert "function applyRefAnchorToV2(node, idx, label, tplDef, roles)" in d
    # 模板带官方 retention 标记与角色句
    assert '"fully_preserved"' in d and '"partially_preserved"' in d and '"weak_reference"' in d
    # 只在已有具象化结构时写 v2（不把三字段段切成六字段）
    assert "if (!pv || typeof pv !== \"object\") return false;" in d
    # 裸引用模板仍在
    assert "裸引用（不加描述）" in d


def test_clear_prompts_drops_refs():
    """清空提示词时引用一起清（正文=唯一真相，否则出现「正文没标签、引用栏还亮着」）。"""
    d = _src()
    i = d.index("function clearPrompts(node)")
    block = d[i:i + 1400]
    assert "refs: []," in block
