"""提示词富文本编辑器（内联绿框引用）回归。

跑法（仓库根目录）：
    python -m pytest tests/test_prompt_editor.py -q

覆盖：
1) `refsFromText` 真跑（抽出 shipped 源码交给 node 执行）：正文 → 引用集合，
   与后端 compile_refs / _find_refs 同口径（负向后顾、最长优先、允许重复计数）。
2) 源码断言：富文本编辑器、✕ 清全部引用、正文=唯一真相（setPromptText 同步 refs）、
   锚定方式（引用语）为常驻模式且带官方 retention 标记。
3) 引用监控即时性（本轮修复）：卡片轻量重绘钩子 + 活池/活别名表 + 晚到别名即时补框。
4) 两层 ✕ 语义分层：绿框 ✕ 只取消一处（removeOneTag），引用条 chip ✕ 清全部（removeTag）。
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


@pytest.mark.skipif(NODE is None, reason="需要 node 执行前端")
def test_editor_runtime_jsdom():
    """jsdom 真跑编辑器：插/删/计数/落盘/锚定方式全链路（缺 jsdom 则跳过）。

    这条是「引用点不了」的回归闸：历史 bug 是 insertTag/removeTag 里写了
    `box.value`（div 没有 .value → undefined），insertTag 抛 reading 'length'、
    removeTag 静默失效；以及正则删短标签会吃掉长标签前缀。
    """
    nm = os.path.join(ROOT, "node_modules")
    if not os.path.isdir(os.path.join(nm, "jsdom")):
        pytest.skip("未安装 jsdom（repo/node_modules 缺失）")
    script = os.path.join(ROOT, "tests", "js", "prompt_editor_check.js")
    env = dict(os.environ, NODE_PATH=nm)
    r = subprocess.run([NODE, script], capture_output=True, text=True, timeout=90, env=env)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"


def test_editor_source_points():
    d = _src()
    # 富文本编辑器 + 内联绿框 + 两层 ✕（绿框=取消这一处；引用条 chip=清全部）
    assert "function createPromptEditor(opts)" in d
    assert 'classList.add("h3d-rtag")' not in d            # 绿框类由 className 赋值
    assert '"h3d-rtag"' in d and '"h3d-rtagx"' in d
    assert "取消这一处引用" in d                              # 绿框 ✕ 的 tooltip
    assert "取消本段对「${a.label}」的全部引用" in d            # 引用条 chip ✕ 的 tooltip
    # 编辑器对外暴露 textarea 兼容面（@补全/AI 优化/编译预览零改动）
    for api in ("insertText(text)", "insertTag", "removeTag", "removeOneTag", "tagCount",
                "normalizeLoose", "get value()", "set value(v)", "setSelectionRange"):
        assert api in d, f"编辑器缺少兼容接口：{api}"
    # 正文是唯一真相：写回提示词时同步 refs
    assert "syncRefsFromText(ds, idx, text);" in d
    assert "function syncRefsFromText(ds, idx, text)" in d


def test_two_level_remove_semantics():
    """两层 ✕ 语义必须分层（线下 bug：绿框 ✕ 被接到 removeTag → "点小叉全叉掉了"）。

    - 绿框自带 ✕ → removeOneTag：只取消**这一处**（引用 N 次点 N 下）。
    - 引用条 chip 旁 ✕ / 右键 chip → removeTag：清本段**全部**引用。
    - 段卡 onRemove 不得再跟一个 removeSegmentRef（否则一次点击计数掉 2）。
    """
    d = _src()
    # 绿框 ✕ 走 removeOneTag，不走 removeTag
    i = d.index("x.addEventListener(\"mousedown\", (e) => {")
    assert "removeOneTag(sp);" in d[i:i + 400], "绿框 ✕ 必须调 removeOneTag"
    assert "function removeOneTag(tagEl)" in d
    # 引用条那一层仍是清全部
    assert "removeTag(a.label);" in d                     # chip ✕ / 右键 chip 的 clearAll
    assert "function removeTag(label)" in d
    # 段卡 onRemove：写回失败才退回 removeSegmentRef（不能无条件再减一次）
    assert "if (!applyPromptEdit(node, it.idx, ta)) removeSegmentRef(node, it.idx, label);" in d
    assert d.count("removeSegmentRef(node, it.idx, label)") == 1, \
        "绿框 ✕ 路径只应有一处 removeSegmentRef（且是带条件的兜底）"


def test_anchor_mode_source_points():
    d = _src()
    # 引用语 = 常驻「锚定方式」模式（选方式 → 点素材 → 按方式写入正文）
    assert "const _refTpl = new Map()" in d
    assert "const REF_TPL_DEFAULT = 0" in d
    assert "applyRefAnchorToV2(node, it.idx, a.label, tplDef || [], roles)" in d
    assert "function applyRefAnchorToV2(node, idx, label, tplDef, roles)" in d
    # 模板带官方 retention 标记与角色句
    assert '"fully_preserved"' in d and '"partially_preserved"' in d and '"weak_reference"' in d
    # 只在已有具象化结构时写 v2（不把三字段段切成六字段）
    assert "if (!pv || typeof pv !== \"object\") return false;" in d
    # 裸引用模板仍在，且**只此一档**：原「无」与它插入的正文完全一样，已合并掉
    assert "裸引用（不加描述）" in d
    assert "锚定方式：无" not in d
    assert 'mkOpt("", "锚定方式' not in d


def test_ref_state_timely_source_points():
    """引用监控"不及时"回归（源码点）：三条链路缺一不可。

    旧行为：焦点在提示词框里时 renderCenterColumn 整体跳过重建，而引用条/绿框
    只靠整卡重建刷新 —— 于是"切了锚定方式/按了 ✕ 要再点一下别的才更新"。
    """
    d = _src()
    # ① 卡片轻量重绘钩子：锁定期间仍推进卡片内的即时状态
    assert "let _cardPainters = []" in d
    assert "function registerCardPainter(fn)" in d
    assert "_cardPainters = [];" in d                     # buildCards 每次重置
    assert "for (const fn of _cardPainters)" in d
    assert "registerCardPainter(repaintCard);" in d
    # ② ✕ / chip 点击立即重绘（并 guard 掉已被换掉的旧卡 DOM）
    assert d.count("repaintCard();") >= 3
    assert "if (!ta.el || !ta.el.isConnected) return;" in d
    # ③ 别名表 + 素材池读**活**状态，不读建卡快照；晚到别名即时补框
    assert "livePool" in d
    assert "normalizeLoose" in d
    assert "try { arr = getDs(node).ref_assets; }" in d
    # chips 跟随活池重建（新上传素材不必等整卡重建就能出现）
    assert "const syncChips = (live)" in d


def test_clear_prompts_drops_refs():
    """清空提示词时引用一起清（正文=唯一真相，否则出现「正文没标签、引用栏还亮着」）。"""
    d = _src()
    i = d.index("function clearPrompts(node)")
    block = d[i:i + 1400]
    assert "refs: []," in block
