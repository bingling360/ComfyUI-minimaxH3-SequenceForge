"""源码级守卫：**插件内不得存在「用了但从未定义」的名字**。

为什么需要它（2026-09-18 实测事故）：
`cc93c7d`「手动锚定步骤 6 主编排切换」重构时把主循环的一整块初始化
（`pbar` / `total` / `prompt_list` / `thumbs` / `videos` / `seams` /
`bridge_scores` / `all_frames` / `seg_frames` / `seg_wavs` / `trims` /
`seam_metrics_rows`）**整体删掉了**。结果是任何带存档的运行必然崩在第一行
`save_state` 的 `NameError: name 'total' is not defined` —— 而且**修完一个立刻
撞上下一个**（十余个名字排队）。这类 bug 单测完全覆盖不到（没人会跑整条链），
只有静态扫描能兜住。

同时它也能抓到 `routes.py` 里那种「某个路由 handler 少定义一行变量 → 该接口
必然 500」的同类问题。

用 pyflakes 的 F821（undefined name）；没装 pyflakes 时整文件 skip，
不能因为缺个开发依赖就把测试变红。
"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只扫插件自己的模块（不扫 node_modules / docs / web 前端）
MODULES = [
    "anchors.py", "asset_store.py", "checkpoint.py", "cond_cache.py",
    "eav_feta.py", "grid.py", "guides.py", "latent_tools.py",
    "library.py", "media.py", "metrics.py", "nodes.py", "optimizer.py",
    "perf.py", "projects.py", "prompts.py", "qc.py", "routes.py",
    "seam_doctor.py", "upscale.py", "upscale_net.py",
]

try:
    import pyflakes.api  # noqa: F401
    import pyflakes.reporter  # noqa: F401
    _HAS_PYFLAKES = True
except Exception:
    _HAS_PYFLAKES = False

pytestmark = pytest.mark.skipif(
    not _HAS_PYFLAKES,
    reason="需要 pyflakes（pip install pyflakes）；未安装时跳过静态扫描")


def _pyflakes_undefined(path):
    """跑 pyflakes，返回 (行号, 名字) 列表；只看 F821 undefined name。

    走子进程而不是 `pyflakes.api.check`：后者要求调用方实现 Reporter 的
    write/stdout 全套接口（版本间还不一样），不值得为一条扫描去适配。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = []
    for line in (proc.stdout or "").splitlines():
        if "undefined name" not in line:
            continue
        # 形如 `path:1712:52: undefined name 'latent'`
        try:
            lineno = int(line.split(":")[1])
            name = line.rsplit("'", 2)[-2]
        except (IndexError, ValueError):
            continue
        out.append((lineno, name))
    return out


def _enclosing_top_bindings(path, name, lineno):
    """兜底复核：这个名字在**最外层函数体/模块层**是否被赋值过。

    pyflakes 在极少数结构下会对「闭包读外层局部变量」误报（本项目 upscale.py
    的 `cond` / `latent` / `up_v_base` 就是这种：它们确实在 render_latent 顶层
    由 `cond, latent = out[0], out[1]` 绑定）。这里用 AST 复核，避免被误报逼着
    去改本来正确的代码。
    """
    import ast

    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    # 收集所有「函数体顶层 / 模块顶层」的绑定（含元组解包）
    bound_at = set()   # (enclosing scope node, name)
    for scope in [tree] + [n for n in ast.walk(tree)
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for st in scope.body:
            targets = []
            if isinstance(st, ast.Assign):
                targets = st.targets
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                targets = [st.target]
            for t in targets:
                if isinstance(t, ast.Tuple):
                    for e in t.elts:
                        if isinstance(e, ast.Name):
                            bound_at.add((id(scope), e.id))
                elif isinstance(t, ast.Name):
                    bound_at.add((id(scope), t.id))
    # 找出 lineno 所属的最内层函数，再看它或任一外层是否绑定过
    owners = []
    for scope in [n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if scope.lineno <= lineno <= (getattr(scope, "end_lineno", scope.lineno) or scope.lineno):
            owners.append(scope)
    # 外层在前（按定义行升序），加上模块层
    owners.sort(key=lambda n: n.lineno)
    chain = [tree] + owners
    return any((id(s), name) in bound_at for s in chain)


@pytest.mark.parametrize("fname", MODULES)
def test_no_undefined_names(fname):
    path = os.path.join(ROOT, fname)
    if not os.path.exists(path):
        pytest.skip(f"{fname} 不存在")
    findings = _pyflakes_undefined(path)
    real = []
    for lineno, name in findings:
        if name and _enclosing_top_bindings(path, name, lineno):
            continue    # pyflakes 对闭包读外层局部变量的误报
        real.append(f"{fname}:{lineno} undefined name {name!r}")
    assert not real, (
        f"{fname} 存在「用了但从未定义」的名字（重构丢变量是本项目已发生过的事故）：\n"
        + "\n".join(real))
