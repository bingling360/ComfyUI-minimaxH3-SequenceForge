"""重摇段种子（`nodes._redo_seed`）回归 —— 种子**跟随链参数**，不再自动换。

历史：2026-09-16 之前是「控件种子 + i」，撞上本段存档种子就 +1 → 连续重摇只在
两个种子之间来回；2026-09-16 改成「存档种子 + 7919」（每次都换新种子）。
2026-11-08 用户要求**把自动换种子整条去掉**：重摇就是"用同一份参数再采一次"，
想换一版自己改「种子」控件 —— 所以现在等于「控件种子 + i」，
且 `archived`（存档种子）只保留在签名里、不参与计算。

钉四件事：
  1) 结果严格等于 `控件种子 + 段序号`（确定性、可复现）；
  2) 连续重摇（把上一次结果喂回 archived）**不换种子** —— 这正是"去掉自动换"；
  3) 存档种子无论取什么值都不影响结果（脏值也不会把重摇弄崩）；
  4) 模运算边界不回绕成 0 以下。

跑法（仓库根目录）：
    python -m pytest tests/test_rework_redo_seed.py -q
"""

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODES = os.path.join(ROOT, "nodes.py")
MOD = 0xFFFFFFFFFFFFFFFF


def _load_redo_seed():
    """从 nodes.py 抽 `_redo_seed`（不 import ComfyUI）。"""
    src = open(NODES, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_redo_seed":
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<nodes.py>", "exec"), ns)
    assert "_redo_seed" in ns, "抽取失败：nodes.py 里找不到 _redo_seed"
    return ns["_redo_seed"]


def test_follows_control_seed():
    fn = _load_redo_seed()
    assert fn(123, 2) == 125, "重摇种子 = 控件种子 + 段序号（与「重跑起始段」同规则）"
    assert fn(123, 2) == fn(123, 2), "同一份参数必须可复现"


def test_archived_seed_no_longer_bumps():
    """存档种子传什么都不影响 —— 自动换种子已被移除。"""
    fn = _load_redo_seed()
    assert fn(123, 2, 1000) == 125
    assert fn(123, 2, None) == 125
    assert fn(123, 2, "garbage") == 125, "极老存档里的脏值不许把重摇弄崩"
    assert fn(123, 2, {}) == 125
    assert fn(123, 2, 999999) == fn(123, 2), "换一个存档种子不得改变结果"


def test_consecutive_rerolls_keep_the_same_seed():
    """连续重摇不再自己变种子（旧实现每次 +7919，用户无法复现任何一版）。"""
    fn = _load_redo_seed()
    ctrl, i = 777, 3
    first = fn(ctrl, i)
    cur = first
    for _ in range(5):
        cur = fn(ctrl, i, cur)          # 把上一次的结果当存档种子喂回去
        assert cur == first, f"重摇又自己换了种子：{cur} != {first}"
    assert fn(ctrl + 1, i) != first, "改控件种子必须换一版（这是用户唯一的换种子入口）"


def test_wraps_inside_the_mask():
    fn = _load_redo_seed()
    got = fn(MOD - 100, 50)
    assert 0 <= got < MOD, f"模运算回绕出界：{got}"
    assert got == (MOD - 100 + 50) % MOD
