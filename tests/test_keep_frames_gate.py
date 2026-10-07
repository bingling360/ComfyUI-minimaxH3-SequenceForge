"""默认口径不留全链帧（只有 final_mode=memory 才留）—— 源码级契约锁定。

背景：全链帧 tensor 是 **CPU 内存**峰值的大头。实测 544×960 × 5895 帧
（约 49 段、4 分钟）单张 float32 IMAGE 就要 36.9GB，把 64GB 机器的
`DefaultCPUAllocator` 直接打爆（`not enough memory: you tried to allocate
36943257600 bytes`，见 nodes.py:_build_chain_images）。

默认口径（final_mode=auto/stream）成片走**流式拼接分段 mp4**，从盘上拼、
帧用完即弃 —— 全链帧内存占用为 0。只有用户显式选 `final_mode=memory`
（强制内存帧编码）时才必须把帧留在内存里。

本测试用 AST 钉死这条契约，防止有人「为了下游可能要用 IMAGE 输出」把它
改回无条件保留（默认工作流里主节点的「图像」输出 links 是空数组，
web/h3_default_workflow.js —— 根本没人接）。
"""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "nodes.py"


def _tree():
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _parents(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _find_assign(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return node
    return None


def _is_call_to(node, func_name):
    """node 是否形如 func_name(...)（Name 调用）。"""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == func_name)


def _is_method_call(node, obj_name, method):
    """node 是否形如 obj_name.method(...)。"""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == obj_name)


def _enclosed_by_if(node, parents, names):
    """从 node 往上走，是否被 `if <name>:`（name ∈ names）包住。"""
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.If) and isinstance(cur.test, ast.Name) \
                and cur.test.id in names:
            return True
    return False


def test_keep_frames_is_memory_only():
    """_keep_frames 必须严格等于 (_final_mode == "memory")。"""
    node = _find_assign(_tree(), "_keep_frames")
    assert node is not None, "nodes.py 里找不到 _keep_frames 定义"
    cmp = node.value
    assert isinstance(cmp, ast.Compare), (
        f"_keep_frames 应是等值比较 (_final_mode == 'memory')，实际 {ast.dump(cmp)}")
    assert isinstance(cmp.left, ast.Name) and cmp.left.id == "_final_mode", \
        "_keep_frames 左边必须是 _final_mode"
    assert len(cmp.ops) == 1 and isinstance(cmp.ops[0], ast.Eq), \
        "_keep_frames 必须是等值比较（不是 in / 其它）"
    right = cmp.comparators[0]
    assert isinstance(right, ast.Constant) and right.value == "memory", \
        '_keep_frames 右边必须是字面量 "memory"'


def test_all_frames_append_is_gated():
    """两处 all_frames.append 都必须在 `if _keep_frames:` 里。"""
    tree = _tree()
    parents = _parents(tree)
    hits = []
    for node in ast.walk(tree):
        if _is_method_call(node, "all_frames", "append"):
            hits.append(node)
            assert _enclosed_by_if(node, parents, {"_keep_frames"}), (
                f"nodes.py:{node.lineno} 的 all_frames.append 没被 `if _keep_frames:` "
                "保护 —— 默认口径会把全链帧留在内存（长链 36.9GB 起步）")
    assert len(hits) == 2, f"预期 2 处 all_frames.append，实际 {len(hits)}"


def test_frame_counts_recorded_per_segment():
    """段计数必须由 _frame_counts 承担（每段一条，与 append 同处）。"""
    tree = _tree()
    hits = [n for n in ast.walk(tree) if _is_method_call(n, "_frame_counts", "append")]
    assert len(hits) == 2, (
        f"预期 2 处 _frame_counts.append（序章 + 普通段），实际 {len(hits)} —— "
        "不留帧时它是唯一的段计数来源")


def test_all_frames_not_used_as_counter():
    """all_frames 不得再被当段计数/总帧数用（那是 _frame_counts 的活）。"""
    tree = _tree()
    bad = []
    for node in ast.walk(tree):
        # len(all_frames)
        if _is_call_to(node, "len") and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Name) and arg.id == "all_frames":
                bad.append(node.lineno)
        # for x in all_frames / sum(... for x in all_frames)
        iters = []
        if isinstance(node, ast.For):
            iters.append(node.iter)
        if isinstance(node, ast.comprehension):
            iters.append(node.iter)
        for it in iters:
            if isinstance(it, ast.Name) and it.id == "all_frames":
                bad.append(getattr(it, "lineno", -1))
    assert not bad, (
        f"nodes.py 行 {bad} 仍在拿 all_frames 当计数/遍历 —— 默认口径下它是空的，"
        "这些地方会静默算错（段数恒为 0）。请改用 _frame_counts")


def test_autosave_final_requires_frames():
    """内存帧编码只在有帧时才能调 —— 否则会用单帧占位图编出黑片。"""
    tree = _tree()
    parents = _parents(tree)
    hits = [n for n in ast.walk(tree) if _is_call_to(n, "_autosave_final")]
    assert hits, "找不到 _autosave_final 调用点"
    for node in hits:
        assert _enclosed_by_if(node, parents, {"all_frames"}), (
            f"nodes.py:{node.lineno} 调 _autosave_final 时没检查 all_frames —— "
            "默认口径下 images 是单帧占位，会编出 1 帧黑片")
