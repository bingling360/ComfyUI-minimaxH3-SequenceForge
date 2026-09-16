"""手动锚定重构的**结构守卫**：钉住契约，防回退。

跑法（仓库根目录）：
    python -m pytest tests/test_anchor_wiring.py -q

为什么用源码/结构断言而不是行为断言：这套改动的核心是「七处碎片 → 一个 anchor」，
而真正的行为验证需要 ComfyUI + GPU（本机跑不了，见规划 §11）。行为验证落在规划
§10 的现场验收 6 条；这里守住的是**结构契约**——代码形状一旦退回旧结构立刻红，
比等到现场跑一遍才发现便宜得多。
"""

import ast
import os
import re as _re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


NODES = _src("nodes.py")
EXPS = _src("experiments.py")
ANCHORS_SRC = _src("anchors.py")


# ---- 唯一入口：seg.anchors[] ----

def test_old_fragments_are_gone_from_nodes():
    """latent_ref / tail_src 的解析与消费必须全部消失（碎片收敛）。"""
    for dead in ("seg_latent_ref", "seg_tail_src", "_seg_tail_anchor", "_insert_hash",
                 "proj_inserts", "insert_specs"):
        assert dead not in NODES, f"旧碎片 {dead} 仍在 nodes.py 里"


def test_anchors_are_parsed_through_migration_and_validated():
    """读旧档走 migrate_legacy_seg，读新档走 anchors[]，且**硬校验**。"""
    assert "anchors.migrate_legacy_seg(" in NODES
    assert "anchors.validate_anchors(" in NODES
    assert "seg_anchors" in NODES


def test_anchors_are_serialised_into_segment_hash():
    """anchor 是内容变更：不进哈希就会出现「改了 anchor 不重做」。"""
    assert 'tag = ("anc:' in NODES


def test_change_intervals_drives_rebuild_instead_of_cascade():
    """内容变更走区间模型（标进重摇通道），不再用 reroll_start 截断整链。"""
    assert "anchors.change_intervals(" in NODES
    assert "checkpoint.reroll_start" not in NODES


# ---- 静默降级必须绝迹 ----

def test_load_library_latent_no_longer_falls_back():
    """latent 外源取不到必须抛，不能回落上段尾（设了锚不生效还不说 = 最坏一类 bug）。"""
    fn = ast.parse(NODES)
    node = _find_func_anywhere(fn, "_load_library_latent")
    body = ast.unparse(_without_docstring(node))
    assert "回落" not in body
    assert "return None" not in body
    assert "raise ValueError" in body


def test_manual_anchor_range_check_blocks_instead_of_reporting():
    """组装侧对手动锚做硬拦；旧的 audit_keyframes「只报不拦」不再用于主路径。"""
    assert "guides.audit_keyframes(" not in NODES
    assert 'raise ValueError(f"段{g + 1} 锚点越界' in NODES


# ---- _apply_guide 只收 keyframe 列表 ----

def test_apply_guide_takes_keyframe_list():
    fn = ast.parse(NODES)
    target = _find_func_anywhere(fn, "_apply_guide")
    args = [a.arg for a in target.args.args]
    assert args == ["cond", "keyframes", "sampled_fc"], args


def test_apply_guide_has_no_fragment_parameters():
    """guide/e1_windows/memory_kfs/head_kf/mid_kfs 这些碎片参数不该回来。"""
    fn = ast.parse(NODES)
    target = _find_func_anywhere(fn, "_apply_guide")
    assert target.args.defaults == []
    assert target.args.kwonlyargs == []


def test_apply_guide_call_sites_all_pass_three_args():
    tree = ast.parse(NODES)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_apply_guide"]
    assert calls, "主路径应当还在调 _apply_guide"
    for c in calls:
        assert len(c.args) == 3 and not c.keywords, ast.unparse(c)[:80]


# ---- 实验三件套已并入 anchor ----

def test_e1_e2_mid_experiments_are_retired():
    for dead in ("e1_bridge_shard", "e2_memory_anchor", "mid_anchor"):
        assert f'"{dead}": dict(' not in EXPS, f"{dead} 实验定义仍在"
    for dead in ("e1_window_kf", "e1_windows", "memory_anchor_positions", "memory_tokens"):
        assert f"def {dead}(" not in EXPS, f"{dead} 支撑函数仍在"


def test_memory_anchor_storage_is_retired():
    ck = _src("checkpoint.py")
    for dead in ("memory_anchor_path", "save_memory_anchor", "load_memory_anchor"):
        assert f"def {dead}(" not in ck
    assert "memory_anchor" not in NODES


# ---- 实验开关不再触发重做 ----

def test_experiment_switch_no_longer_triggers_rebuild():
    """实验开关与 steps/cfg 同类：只影响此后新采样的段，不该触发整链重做。"""
    ck = _src("checkpoint.py")
    assert "experiments" not in ck, "assert_match 不应再特判 experiments"


# ---- 源归一化的三段路 ----

def test_resolve_anchor_source_covers_three_outcomes():
    for token in ('"reuse"', '"encode"', "_REENCODE_HINT"):
        assert token in ANCHORS_SRC
    assert "分辨率不匹配" in ANCHORS_SRC


# ---- 跨模块依赖：不许按全局名调宿主的模块级函数 ----

def test_anchor_js_has_no_cross_module_global_calls():
    """h3d_anchor.js 不得按全局名调用 h3_director.js 的模块级函数。

    2026-09-16 线上故障：h3d_anchor.js 里 `typeof getDirValue !== "function"` 当场抛错，
    整块「⚠ 段落卡片渲染失败 · getDirValue 未加载」。
    根因：`getDirValue` / `setSegmentField` 都是 h3_director.js 的**模块级**函数，
    并没有挂到 window 上（公开面只有 window.H3Director.upscaleLatent / H3Api /
    H3Prompts / H3Assets），跨模块按全局名调必然失败。
    正确做法：宿主通过 `buildAnchorPanel({ dir, setAnchors, refresh })` 注入访问器。
    """
    code = _js_code(_src("web/h3d_anchor.js"))
    for name in ("getDirValue", "setSegmentField", "scheduleRefresh",
                 "projDir", "setSegAnchors"):
        assert name not in code, f"h3d_anchor.js 又在按全局名调 {name}（应改为宿主注入）"


def test_director_injects_anchor_host_accessors():
    """接线必须真的传 dir / setAnchors，否则面板一渲染就抛。"""
    src = _src("web/h3_director.js")
    i = src.index("window.H3Anchor.buildAnchorPanel(")
    call = src[i:i + 800]
    assert "dir:" in call, "buildAnchorPanel 未注入 dir"
    assert "setAnchors:" in call, "buildAnchorPanel 未注入 setAnchors"


def test_anchor_js_has_no_load_order_requirement():
    """模块不得要求「必须先于/后于某文件加载」——那是设计缺陷，不是约定。"""
    src = _src("web/h3d_anchor.js")
    for bad in ("必须先于本模块", "后于本模块"):
        assert bad not in src


def test_anchor_js_exports_only_namespaced_api():
    """只挂 window.H3Anchor，不往全局摊函数（否则又是一套隐式契约）。"""
    src = _src("web/h3d_anchor.js")
    assert "window.H3Anchor = {" in src
    assert not _re.search(r"^\s*window\.\w+\s*=\s*function", src, _re.M)


def _js_code(src):
    """粗剥 JS 注释（块注释 + 行注释），用于「这个名字只许出现在注释里」这类静态守卫。

    实现刻意简单：本仓库 web/ 下的脚本没把 `//` 写进字符串（URL 都是 /h3chain/…
    单斜杠），所以按行剥即可。若哪天出现 `http://` 这类字面量，这里会多剥一点
    ——只会让守卫变宽松，不会误报。
    """
    out, in_block = [], False
    for ln in src.split("\n"):
        s = ln.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block = True
            continue
        if s.startswith("*"):
            continue
        out.append(ln.split("//")[0] if "//" in ln and "://" not in ln else ln)
    return "\n".join(out)


def _find_func_anywhere(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _without_docstring(node):
    """剥掉 docstring——注释里可以解释"曾经怎样"，代码里不行。"""
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body
