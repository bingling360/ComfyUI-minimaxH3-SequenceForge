"""源码级守卫：**局部变量「可能未绑定就读取」**（UnboundLocalError 类）。

为什么需要它（2026-09-22 线上事故）：
`nodes.py::_execute_inner` 里 `_seg_end_img` 只在 `if replay: ... else:` 的 **else 分支**
赋值，却在 `if use_ckpt:`（两条路径都会走到）里被读取。于是「逐段确认 / 存档续跑」
这类走 **replay 分支** 的运行，跑到第二段必然崩：

    UnboundLocalError: cannot access local variable '_seg_end_img'
                         where it is not associated with a value

**为什么已有守卫没拦住**：`tests/test_no_undefined_names.py` 用的是 pyflakes F821
(undefined name)，那是**词法作用域**判定——名字只要在函数里绑定过一次就算"已定义"，
不看控制流。实测：把带 bug 的版本喂给 pyflakes，它一条 F821 都不报。
（F821 抓的是 `cc93c7d` 那种「整块初始化被删掉、名字从未绑定」；本文件补的是
「绑定过、但某条路径上还没绑就用了」。两者互补，都留着。）

本文件用一个**极简前向数据流**（env = 到当前点必然已绑定的名字集合）判「可能未绑定」，
对 if/else 取「所有能正常结束的分支的交集」，并识别 return/raise/continue/break
的提前退出语义（`try: x = ... / except: return err` 这类只要 handler 提前返回，
body 的绑定就算数）。

判定偏保守（宁漏勿误报）。已知会漏报「跨条件的关联不变量」这类代码，
所以下面留了 BASELINE——把**经人工确认安全**的命中记在这里；出现新命中即测试失败。
"""

import ast
import builtins
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODULES = [
    "anchors.py", "asset_hub.py", "asset_store.py", "checkpoint.py", "cond_cache.py",
    "eav_feta.py", "grid.py", "guides.py", "latent_tools.py", "library.py",
    "media.py", "metrics.py", "nodes.py", "optimizer.py", "perf.py", "projects.py",
    "prompts.py", "qc.py", "routes.py", "seam_doctor.py", "sigmas_adapter.py",
    "transcode_queue.py", "upscale.py", "upscale_net.py",
]

# 人工确认安全的既有命中：(模块, 函数, 名字)
# 每条都写清「为什么安全」，避免后来者照抄着把新 bug 也加进来。
BASELINE = {
    # `eff_guide` / `_tail_kf` 只在 `_execute_inner` 的**生成分支**里赋值，读取点却在
    # `if _redo_mode is not None:` 之下。二者由同一处的 `replay` 判定互斥：
    # `replay = (use_ckpt and g < done and _redo_mode is None and ...)`，
    # 即 `_redo_mode is not None` ⇒ 非 replay ⇒ 走生成分支 ⇒ 必然已赋值。
    # 扫描器不做这种跨条件的关联推理，故保守地上报；人工复核为安全。
    ("nodes.py", "_execute_inner", "eff_guide"),
    ("nodes.py", "_execute_inner", "_tail_kf"),
    # `_tjob` 在 `try:` 体的**第一句**就被赋成 None，之后才可能抛错；且唯一的
    # handler 不提前返回。所以能走到读取点就必然已绑定。扫描器只按「各分支都绑定」
    # 求交集，不追踪 body 内的先后位置，故保守上报；人工复核为安全。
    ("nodes.py", "_execute_transcode_only", "_tjob"),
}


BUILTINS = set(dir(builtins))
SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
ABRUPT = (ast.Return, ast.Raise, ast.Continue, ast.Break)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _target_names(target):
    out, stack = [], [target]
    while stack:
        t = stack.pop()
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            stack.extend(t.elts)
        elif isinstance(t, ast.Starred):
            stack.append(t.value)
    return out


def _always_abrupt(stmts):
    """该块是否必然以 return/raise/continue/break 收尾（到不了后继代码）。"""
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, ABRUPT):
        return True
    if isinstance(last, ast.If) and last.orelse:
        return _always_abrupt(last.body) and _always_abrupt(last.orelse)
    return False


def _params_of(func):
    a = func.args
    names = {x.arg for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def _iter_own_nodes(func):
    """遍历函数自身节点，不进入嵌套作用域。

    过滤必须放在 **pop 时**：若只在 push 时过滤，`func.body` 里那个嵌套 FunctionDef
    节点本身仍会入栈并被处理，随后它的 body 语句（不是 SCOPE_NODES）也会被推进去，
    于是嵌套函数的绑定被错算成本函数的局部名（实测导致 `re` / `comfy` 等模块名误报）。
    """
    start = func.body if isinstance(func.body, list) else [func.body]
    stack = list(start)
    while stack:
        n = stack.pop()
        if isinstance(n, SCOPE_NODES):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _local_names(func):
    locals_, decls = set(), set()
    for n in _iter_own_nodes(func):
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in tgts:
                locals_ |= set(_target_names(t))
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            locals_ |= set(_target_names(n.target))
        elif isinstance(n, ast.NamedExpr):
            locals_ |= set(_target_names(n.target))
        elif isinstance(n, ast.ExceptHandler) and n.name:
            locals_.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                locals_.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.Global):
            decls |= set(n.names)
        elif isinstance(n, ast.Nonlocal):
            decls |= set(n.names)
    return locals_, _params_of(func), decls


class _ScopeScan:
    def __init__(self, risky):
        self.risky = risky
        self.hits = []

    def expr(self, node, env):
        if node is None:
            return
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                self._note(node, env)
            return
        if isinstance(node, ast.NamedExpr):
            self.expr(node.value, env)
            return
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, COMPREHENSIONS):
            gens = node.generators
            if gens:
                self.expr(gens[0].iter, env)
            inner = set(env)
            for g in gens:
                inner |= set(_target_names(g.target))
            for g in gens:
                for cond in g.ifs:
                    self.expr(cond, inner)
                self.expr(g.iter, inner)
            for c in (getattr(node, "elt", None), getattr(node, "key", None),
                      getattr(node, "value", None)):
                self.expr(c, inner)
            return
        for c in ast.iter_child_nodes(node):
            self.expr(c, env)

    def _note(self, node, env):
        if node.id in env or node.id not in self.risky:
            return
        self.hits.append((node.lineno, node.id))

    def block(self, stmts, env):
        env = set(env)
        for st in stmts:
            self.stmt(st, env)
            env |= self.after(st, env)      # after() 返回「新增」，须并集
        return env

    def stmt(self, stmt, env):
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            self.expr(stmt.iter, env)
        elif isinstance(stmt, ast.While):
            self.expr(stmt.test, env)
        elif isinstance(stmt, ast.If):
            self.expr(stmt.test, env)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for it in stmt.items:
                self.expr(it.context_expr, env)
        elif isinstance(stmt, ast.Try):
            for h in stmt.handlers:
                self.expr(h.type, env)
        elif isinstance(stmt, ast.Match):
            self.expr(stmt.subject, env)

        sub_lists = [getattr(stmt, a, []) or [] for a in ("body", "orelse", "finalbody")]
        sub_lists += [h.body for h in getattr(stmt, "handlers", []) or []]
        cases = getattr(stmt, "cases", None) or []
        sub_lists += [c.body for c in cases]
        skip = {id(x) for L in sub_lists for x in L}
        for c in ast.iter_child_nodes(stmt):
            if id(c) in skip or isinstance(c, (ast.stmt, ast.ExceptHandler)):
                continue
            self.expr(c, env)

        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            self.block(stmt.body, set(env) | set(_target_names(stmt.target)))
            self.block(stmt.orelse, env)
        elif isinstance(stmt, ast.While):
            self.block(stmt.body, env)
            self.block(stmt.orelse, env)
        elif isinstance(stmt, ast.If):
            self.block(stmt.body, env)
            self.block(stmt.orelse, env)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            benv = set(env)
            for it in stmt.items:
                if it.optional_vars is not None:
                    benv |= set(_target_names(it.optional_vars))
            self.block(stmt.body, benv)
        elif isinstance(stmt, ast.Try):
            self.block(stmt.body, env)
            for h in stmt.handlers:
                self.block(h.body, set(env) | ({h.name} if h.name else set()))
            self.block(stmt.orelse, env)
            self.block(stmt.finalbody, env)
        elif isinstance(stmt, ast.Match):
            for c in cases:
                cenv = set(env)
                for pat in ast.walk(c.pattern):
                    if isinstance(pat, (ast.MatchAs, ast.MatchStar)) and pat.name:
                        cenv.add(pat.name)
                self.block(c.body, cenv)

    def after(self, stmt, env):
        add = set()
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            tgts = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for t in tgts:
                add |= set(_target_names(t))
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for a in stmt.names:
                add.add((a.asname or a.name).split(".")[0])
        elif isinstance(stmt, ast.If):
            branches = []
            if not _always_abrupt(stmt.body):
                branches.append(self.block(stmt.body, env))
            if stmt.orelse and not _always_abrupt(stmt.orelse):
                branches.append(self.block(stmt.orelse, env))
            if branches:
                inter = set(branches[0])
                for b in branches[1:]:
                    inter &= b
                add |= (inter - set(env))
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            benv = set(env)
            for it in stmt.items:
                if it.optional_vars is not None:
                    benv |= set(_target_names(it.optional_vars))
            add |= (self.block(stmt.body, benv) - set(env))
        elif isinstance(stmt, ast.Try):
            # 只对「能正常结束、从而走到后继代码」的分支求交集：必然 return/raise 的
            # 分支到不了这里，不要求它绑定。这正是 `try: x = 解析() / except: return err`
            # 与 `try: br = int(s) / except: raise ValueError` 能判对的关键。
            branches = []
            if not _always_abrupt(stmt.body):
                branches.append(self.block(stmt.body, env))
            for h in stmt.handlers:
                if _always_abrupt(h.body):
                    continue
                branches.append(self.block(h.body, set(env) | ({h.name} if h.name else set())))
            if branches:
                inter = set(branches[0])
                for b in branches[1:]:
                    inter &= b
                add |= (inter - set(env))
            add |= (self.block(stmt.finalbody, env) - set(env))
        return add


def find_possibly_unbound(tree):
    """返回 [(行号, 名字, 函数名), ...]，按行号排序去重。"""
    out = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        locals_, params, decls = _local_names(func)
        risky = (locals_ | params) - decls - BUILTINS
        sc = _ScopeScan(risky)
        if isinstance(func, ast.Lambda):
            sc.expr(func.body, params)
        else:
            sc.block(func.body, params)
        name = getattr(func, "name", "<lambda>")
        for lineno, var in sc.hits:
            out.append((lineno, var, name))
    seen, uniq = set(), []
    for lineno, var, fn in sorted(out, key=lambda x: x[0]):
        if (lineno, var) in seen:
            continue
        seen.add((lineno, var))
        uniq.append((lineno, var, fn))
    return uniq


# ------------------------------------------------------------------ 守守卫自身

_SHOULD_FLAG = """
def f(replay, use_ckpt, lst, i):
    for item in range(3):
        if replay:
            v = 1
        else:
            _seg_end_img = lst[i]
        if use_ckpt:
            x = _seg_end_img or g()
"""

_SHOULD_NOT_FLAG = [
    # 与本事故同形，但赋值上提到两条路径之前（即本次修复）
    """
def f(replay, use_ckpt, lst, i):
    for item in range(3):
        _seg_end_img = lst[i]
        if replay:
            v = 1
        else:
            v = 2
        if use_ckpt:
            x = _seg_end_img or g()
""",
    # 解析失败即提前返回（routes.py 的惯用写法）
    """
async def h(req):
    try:
        data = await req.json()
    except Exception:
        return err()
    return data.get("a")
""",
    # 校验失败即抛错（projects.py 的惯用写法）
    """
def h(spec):
    try:
        br = int(spec)
    except (TypeError, ValueError):
        raise ValueError("bad")
    return br + 1
""",
    # import 回退链
    """
def f():
    try:
        from . import a as tq
    except ImportError:
        tq = None
    return tq
""",
    # 推导式目标名是独立作用域，不是本函数的局部名
    """
def f(lst, items):
    return [i + 1 for i, v in enumerate(lst) if v], {k: v for k, v in items}
""",
    # 嵌套函数里的绑定不算**外层**函数的局部名（作用域隔离）
    # 若扫描器钻进嵌套函数体，`comfy` 会被误当 outer 的局部名 → 末行误报。
    """
def outer(c):
    def inner():
        comfy = 1
        return comfy
    return inner, comfy
""",
]


def test_scanner_flags_the_known_shape():
    """守守卫：扫描器必须能抓到本次事故的最小形态，否则它就是个摆设。"""
    hits = find_possibly_unbound(ast.parse(_SHOULD_FLAG))
    assert any(v == "_seg_end_img" for _, v, _ in hits), \
        f"扫描器漏掉了 _seg_end_img 形态，命中={hits}"


@pytest.mark.parametrize("src", _SHOULD_NOT_FLAG)
def test_scanner_does_not_flag_safe_shapes(src):
    """守守卫：常见安全写法不得误报（误报会让守卫被绕过/关掉）。"""
    hits = find_possibly_unbound(ast.parse(src))
    assert not hits, f"误报：{hits}"


# ------------------------------------------------------------------ 仓库扫描

def test_no_new_possibly_unbound_locals():
    """扫描插件模块，出现 BASELINE 之外的新命中即失败。

    命中不一定都是 bug（扫描器不做跨条件关联推理），但**每一条都必须人工看过**
    并写进 BASELINE、附上「为什么安全」的理由。
    """
    new = []
    for fname in MODULES:
        path = os.path.join(ROOT, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for lineno, var, func in find_possibly_unbound(tree):
            if (fname, func, var) in BASELINE:
                continue
            new.append(f"{fname}:{lineno} {func}() 里 {var!r} 可能未绑定就读取")
    assert not new, (
        "出现「可能未绑定就读取」的新命中（局部变量只在部分分支赋值、却在两条路径"
        "都会走到的位置读取——这正是 _seg_end_img 崩掉逐段确认的形态）。\n"
        "若人工复核确认安全，请连同理由加进本文件的 BASELINE：\n"
        + "\n".join(new))
