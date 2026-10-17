"""重摇段种子（`nodes._redo_seed`）回归 —— 「连续重摇只会来回两个种子」。

背景（用户反馈「重摇以后质量不太好」时挖出来的）：旧实现是
`控件种子 + i`，撞上本段存档种子就 **+1**。而一次跑完的链里 `seeds[g]` 恰好
就等于 `控件种子 + i`，于是：

    第 1 次重摇 → 控件种子+i+1（新）
    第 2 次重摇 → 控件种子+i  （= 原版，一模一样）
    第 3 次重摇 → 控件种子+i+1（又回到第 1 次）

即**连续重摇只在两个种子之间来回**，用户第二版拿到的正是原来那一版，观感就是
"重摇没用 / 越摇越差"。现在改成从**本段存档种子**推进一个素数步长（7919，
与接缝自动重摇同一个步长），每次都是新种子且按模运算周期不会回到旧版。

钉四件事：
  1) 有存档种子 → 严格等于 `archived + 7919`（确定性、可复现）；
  2) 连续 5 次重摇（把上一次结果喂回去）→ 5 个种子互不相同、且都不等于原版；
  3) 没有/非法存档种子 → 退回「控件种子 + i」（与「重跑起始段」同规则）；
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
    """从 nodes.py 抽 `_REDO_SEED_STEP` / `_redo_seed`（不 import ComfyUI）。"""
    src = open(NODES, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {}
    want_const = ("_REDO_SEED_STEP",)
    want_fn = ("_redo_seed",)
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in want_const:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<nodes.py>", "exec"), ns)
        elif isinstance(node, ast.FunctionDef) and node.name in want_fn:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<nodes.py>", "exec"), ns)
    missing = [k for k in want_const + want_fn if k not in ns]
    assert not missing, f"抽取失败：{missing}"
    return ns


def test_step_is_the_documented_prime():
    ns = _load_redo_seed()
    assert ns["_REDO_SEED_STEP"] == 7919, "步长改了要同步注释（与接缝重摇同款 7919）"


def test_advances_from_archived_seed():
    fn = _load_redo_seed()["_redo_seed"]
    # 控件种子在这里**不该**起作用：重摇只认本段当前种子
    assert fn(123, 2, 1000) == 1000 + 7919
    assert fn(123, 2, 1000) == fn(999999, 7, 1000), "控件种子/段号不得影响重摇种子"


def test_consecutive_rerolls_never_return_to_a_previous_take():
    """连续重摇：5 次必须 5 个新种子，且一个都不能等于原版。

    旧实现的失败形状就在这里：seed(0)=S → S+1 → S → S+1 …
    """
    fn = _load_redo_seed()["_redo_seed"]
    ctrl, i = 777, 3
    s0 = ctrl + i                       # 一次跑完的链：存档种子 == 控件种子+i
    seen = {s0}
    cur = s0
    for _ in range(5):
        cur = fn(ctrl, i, cur)
        assert cur not in seen, f"重摇又回到了用过的种子 {cur}（来回打转）"
        seen.add(cur)
    assert len(seen) == 6


def test_missing_or_bad_archived_falls_back_to_ctrl_seed():
    fn = _load_redo_seed()["_redo_seed"]
    assert fn(500, 2, None) == 502          # 与「重跑起始段」同规则
    assert fn(500, 2, "garbage") == 502     # 极老存档里的脏值不许把重摇弄崩
    assert fn(500, 2, {}) == 502


def test_wraps_inside_the_mask():
    fn = _load_redo_seed()["_redo_seed"]
    near = MOD - 100
    got = fn(0, 0, near)
    assert 0 <= got < MOD, f"模运算回绕出界：{got}"
    assert got == near + 7919 - MOD
