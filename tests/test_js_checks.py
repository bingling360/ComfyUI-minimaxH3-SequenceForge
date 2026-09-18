"""tests/js/*_check.js 统一入口 —— 无 ComfyUI 可跑（缺 node / jsdom 自动跳过）。

跑法（仓库根目录）：
    python -m pytest tests/test_js_checks.py -q

为什么要有这个入口：tests/js/ 下的脚本以前只能手工 `node tests/js/xxx.js` 跑，
没进 pytest。结果 B1 重构改了 poolFromManifest 的依赖（多了 cleanRefName），
asset_chip_check.js 当场 ReferenceError，而全量 pytest 却一片绿 —— 脚本坏了
整整一天没人知道。这里把它们全收进 pytest，一个脚本一条用例，坏了就红。
"""

import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, "tests", "js")
NODE_MODULES = os.path.join(ROOT, "node_modules")
NODE = shutil.which("node")


def _scripts():
    if not os.path.isdir(JS_DIR):
        return []
    return sorted(
        os.path.join(JS_DIR, f) for f in os.listdir(JS_DIR)
        if f.endswith("_check.js")
    )


@pytest.mark.skipif(NODE is None, reason="需要 node 执行前端检查")
@pytest.mark.parametrize("script", _scripts(), ids=lambda p: os.path.basename(p))
def test_js_check_script(script):
    if not os.path.isdir(os.path.join(NODE_MODULES, "jsdom")):
        pytest.skip("未安装 jsdom（repo/node_modules 缺失）")
    env = dict(os.environ, NODE_PATH=NODE_MODULES)
    r = subprocess.run([NODE, script], capture_output=True, text=True,
                       timeout=120, env=env)
    assert r.returncode == 0, f"{os.path.basename(script)} 失败：\n{r.stdout}\n{r.stderr}"
