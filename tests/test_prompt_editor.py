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
    # 三栏引用互不串味：主框引用条**不再**写具象化（具象化引用归它自己的参考组管）
    assert "applyRefAnchorToV2(node, it.idx, a.label, tplDef || [], roles)" not in d
    assert "const refBars = []" in d          # 意图/剧本/结果 各一条
    assert "gate: true" in d                  # 只有「③ 结果」那条进模型
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
    assert d.count("RB.repaint();") >= 2          # 三栏引用条各自即时重绘（chip/✕ 点击）
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


def test_single_pane_single_refbar():
    """三框合一后：段卡只有**一条**引用条，且它不再是"三栏各自挂一条"（源码点）。

    历史：① 中文意图 / ② 剧本 / ③ 结果 各自持有一条引用栏，只有 ③ 进模型。
    B02 起三栏合并成单框（文本即 ds.prompts[idx]），所以：
      · 只有 refResult 一条引用条，①② 的 refbar 必须消失
      · 它 gate=true（走官方 9/3/3、写 seg.refs）—— 唯一进模型的那条
      · 首尾帧按钮仍在卡片公共区（showFrames 一律 false）
      · 原来的 intent_zh / script 字段（含写它们的 setSegmentField 调用）全部删除
    运行期行为另见 tests/js/refbar_status_check.js（真跑 buildRefBar）。"""
    d = _src()
    assert "function buildRefBar(RB)" in d
    assert "const mkRefBar = (cfg) =>" in d
    assert "const refBars = []" in d
    # 单框一条引用条：①② 的引用条与编辑器都已删除
    assert "refResult" in d
    assert "refIntent" not in d and "refScript" not in d
    assert "intentTa" not in d and "scriptTa" not in d
    # 只有结果栏 gate=true（官方上限 + 写 seg.refs）
    assert d.count("gate: true") == 1
    assert d.count("gate: false") == 0
    # 首尾帧按钮已从引用栏搬走：它在卡片公共区，是段级运行参数。
    assert d.count("showFrames: true") == 0
    assert d.count("showFrames: false") == 1
    assert "h3d-anchorbar" in d and "mkFrameBtns(node, it.idx" in d
    # 三框时代的两个字段：前端不再写入（setSegmentField 调用点全删）。
    # 总提示词文档注释里的残留标签由 B04 一并清掉。
    assert chr(34).join(['setSegmentField(node, it.idx, ', 'intent_zh', ', ']) not in d
    assert chr(34).join(['setSegmentField(node, it.idx, ', 'script', ', ']) not in d
    # 老项目迁移函数在位（主框为空时回填旧剧本/意图，幂等）
    assert "function migrateLegacySegText(text, raw)" in d
    # 具象化模式判定不再吃主框的 seg.refs
    assert 'if (seg && Array.isArray(seg.refs) && seg.refs.length) return ' + chr(34) + 'Ref2VA' + chr(34) + ';' not in d
    # 优化器任务判定与 defaultV2Mode 同口径（不再写死 FL2VA）
    assert "return defaultV2Mode(ds, idx);" in d


def test_expand_optimize_pipeline_wired():
    """「AI 扩写 + 优化」两步流水线（源码点）：不弹小框、不显示进度、失败硬报错。

    设计约束（用户拍板）：点击后**不弹框**，也不显示"1/2 2/2"这类进度，
    只在按钮上禁用 + LED 写一句状态。中间产物（剧本）不回框、不落盘。
    """
    d = _src()
    assert "async function runExpandOptimize(node, idx, ta, ui)" in d
    assert "runExpandOptimize(node, it.idx, ta, { btn: bExpandOpt })" in d
    # 小框（openExpandModal）彻底删除
    assert "function openExpandModal" not in d
    # 不弹框：流水线内不出现 confirm/prompt 之类阻塞对话框
    i = d.index("async function runExpandOptimize(")
    block = d[i:i + 3200]
    assert "window.prompt" not in block and "confirm(" not in block
    # 不显示进度百分比：按钮文案只在 finally 复原，跑的过程中不改
    assert "btnLabel0" in block
    assert "扩写中 1/2" not in block and "优化中 2/2" not in block
    # 两步串行：先 expandMulti 再 optimize
    assert "H3Api.expandMulti" in block and "H3Api.optimize" in block
    assert block.index("expandMulti") < block.index("H3Api.optimize")
    # 与单步优化共用落盘路径（对齐指令必须补回，否则模型丢首尾帧锚）
    assert "resyncAlignmentLines(node, idx)" in block
    # 扩写设置（时长范围/风格/是否续跑）在设置页，不在小框里
    assert "function optExpandSettings(settings, seg)" in d
    assert "AI 扩写优化设置" in d
    assert "const EX_STYLES = " in d
    # 悬空设置 auto_optimize 已删
    assert "auto_optimize" not in d and "autoOptimize" not in d


def _fn_block(d, sig):
    """按大括号配平截出一个顶层函数体（比"取 16000 字符"可靠 —— 后者会越界把
    后面的注释/函数一起框进来，断言就变成假阳性/假阴性）。"""
    i = d.index(sig)
    j = d.index("{", i)
    depth = 0
    k = j
    while k < len(d):
        if d[k] == "{":
            depth += 1
        elif d[k] == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    return d[i:k + 1]


def test_master_prompt_single_box_modal():
    """总提示词框 = 单框分段工具（B04，源码点）。

    需求：去掉 AI 多段扩写、退回单框；只留「AI 分段优化」（整篇一次跑）+「解析并分配」。
    """
    d = _src()
    block = _fn_block(d, "function openMasterPromptModal()")
    # 单框：只有一个 textarea，三框时代的箱体/句柄全删
    assert "h3d-mpboxwrap-main" in block
    assert "MP_PH_MASTER" in block
    for dead in ["mkBox", "boxI", "boxS", "boxR",
                 "h3d-mpboxwrap-1", "h3d-mpboxwrap-2", "h3d-mpboxwrap-3",
                 "const MP_PH_INTENT =", "const MP_PH_SCRIPT =",
                 "const MP_ALIGN_LINE =", "const MP_PH_MAIN_REF2VA =",
                 "function mpComposeBoxes(", "function mpSplitBox("]:
        assert dead not in d, f"三框时代的 {dead} 应已删除"
    # 没有「AI 多段扩写」：弹窗里不出现 expandMulti
    assert "expandMulti" not in block, "总提示词框不该再有 AI 多段扩写"
    assert "AI扩写" not in block and "AI 扩写" not in block
    # 有「AI 分段优化」且走 optimizeMulti（一次请求吃 N 段 = 整篇一次跑）
    assert "✨ AI 分段优化" in block
    assert "optimizeMulti" in block
    # 写回仍是「解析并分配」那一条链路
    assert "applyMasterPrompt(node, raw)" in block
    assert "解析并分配" in block
    # 出/回码要走标注编解码（与段卡同一条规则，不另写一套）
    assert "toLLMText(t.src, pool)" in block and "fromLLMText(" in block
    # 单框渲染器：有值就写回旧四标签，不写「意图/剧本」
    assert "function mpRenderState(state)" in d
    assert "[['场景', 'scene']" not in d and '["场景", "scene"]' in d


def test_structured_modal_replaces_v2_tab():
    """具象化从常驻 tab 变成「⇄ 结构化提示词」弹窗（B05，源码点）。"""
    d = _src()
    assert "function openStructuredModal(node, data, idx, ta)" in d
    assert "⇄ 结构化提示词" in d
    # tab 只剩 提示词 / 锚定设置
    assert 'const tabs = [["main", "提示词"], ["set", "锚定设置"]];' in d
    assert '"v2", "具象化"' not in d
    # 老记忆值回落，避免所有 pane 都被藏掉
    assert "if (!tabKeys.includes(curTab)) curTab = \"main\";" in d
    # 弹窗里复用同一个渲染器 + 文本→结构化那条腿
    i = d.index("function openStructuredModal(")
    block = d[i:i + 2600]
    assert "renderPromptV2Panel(bodyBox, node, data, idx)" in block
    assert "applyAiToV2(node, idx, text)" in block


def test_director_js_module_syntax():
    """前端 JS 按 **ES module** 解析必须无错（浏览器就是这么加载的）。

    回归：曾在段卡作用域里重复声明 const liveAssets ——  按
    **script** 解析放过了这个早期错误，浏览器按 module 解析直接 SyntaxError，
    整个导演台（连左侧入口）都不会出现。所以校验必须走 .mjs / module 模式。
    """
    if not NODE:
        pytest.skip('"未找到 node"')
    tmp = os.path.join(ROOT, '_h3_module_syntax_check.mjs')
    try:
        shutil.copyfile(DIRECTOR, tmp)
        r = subprocess.run([NODE, '--check', tmp], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + chr(10) + r.stderr
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_director_js_module_syntax():
    """前端 JS 按 **ES module** 解析必须无错（浏览器就是这么加载的）。

    回归：曾在段卡作用域里重复声明 const liveAssets ——  按
    **script** 解析放过了这个早期错误，浏览器按 module 解析直接 SyntaxError，
    整个导演台（连左侧入口）都不会出现。所以校验必须走 .mjs / module 模式。
    """
    if not NODE:
        pytest.skip('"未找到 node"')
    tmp = os.path.join(ROOT, '_h3_module_syntax_check.mjs')
    try:
        shutil.copyfile(DIRECTOR, tmp)
        r = subprocess.run([NODE, '--check', tmp], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + chr(10) + r.stderr
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_v2_mode_detection_runtime_jsdom():
    '''jsdom 真跑具象化模式判定：前端 defaultV2Mode 必须与后端 detect_mode 同口径。

    这条是「未启用具象化就漏判成 T2VA」的回归闸：段没有 prompt_v2 时，后端
    compilePayload 会 migrateLegacySeg 把 seg.refs 迁成 references → Ref2VA，
    前端若只读 seg.prompt_v2（null）就会判 T2VA，两边打架（多报
    W_MODE_OVERRIDE + 模板选错）。修法：前端直接复用 H3Prompts.detectMode，
    入参也走 ensurePromptV2。
    '''
    nm = os.path.join(ROOT, "node_modules")
    if not os.path.isdir(os.path.join(nm, "jsdom")):
        pytest.skip("未安装 jsdom（repo/node_modules 缺失）")
    script = os.path.join(ROOT, "tests", "js", "v2_mode_check.js")
    env = dict(os.environ, NODE_PATH=nm)
    r = subprocess.run([NODE, script], capture_output=True, text=True, timeout=90, env=env)
    assert r.returncode == 0, r.stdout + chr(10) + r.stderr
