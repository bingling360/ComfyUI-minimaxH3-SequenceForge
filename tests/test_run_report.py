"""H3 运行报告（免连线节点）守卫 —— 2026-10-04。

真事故：导演台的「报告」是**连线输出**，而 ComfyUI 的连线按**位次**存
（workflow JSON 的 links 里是 origin_slot）。输出端口顺序一变，老存档那根线就
静默落到别的槽上：画布上看着还连着、下游实际收不到 ——「每次重启 ComfyUI，
导演台和 PreviewAny 都断连」就是这个。

两条防线，各自钉死：
  ① 结构性：schema 输出顺序让「报告」位次与历史存档自洽（帧率排到末位）——
     见 tests/test_workflows_frozen.py 与 tests/js/desk_node_skin_check.js；
  ② 兜底：本文件的 H3RunReport —— **没有输入端口 = 没有可断的线**，报告由前端
     直接读插件接口，不经过任何连线。

本文件钉 ②，并防止它退化回「接一个输入再显示」那种又会断连的形态。

跑法：python -m pytest tests/test_run_report.py -q
"""

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_PY = os.path.join(ROOT, "run_report.py")
REPORT_JS = os.path.join(ROOT, "web", "h3_report.js")
INIT = os.path.join(ROOT, "__init__.py")


def _schema_keywords(path, cls_name):
    tree = ast.parse(open(path, encoding="utf-8").read())
    # 节点类包在 try: 里（老 ComfyUI 没有 comfy_api 时要能整体降级）→ 必须 walk
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == cls_name)
    fn = next(f for f in cls.body
              if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
              and f.name == "define_schema")
    call = next(n for n in ast.walk(fn)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "Schema")
    return {k.arg: k.value for k in call.keywords}


def test_report_node_has_no_inputs():
    """★ 核心不变量：**一个输入端口都没有**。

    有输入 = 有可断的线 = 重新引入「重启即断连」。这条一旦被改红，
    先想清楚你是不是在把已经根治的故障模式装回来。
    """
    kw = _schema_keywords(REPORT_PY, "H3RunReport")
    ins = kw.get("inputs")
    assert isinstance(ins, ast.List), "define_schema 必须显式给 inputs（不许省略）"
    assert len(ins.elts) == 0, (
        "H3RunReport 不许有输入端口（免连线是它的全部意义）："
        + str([getattr(e, "value", e) for e in ins.elts]))


def test_report_node_registered():
    init = open(INIT, encoding="utf-8").read()
    assert "from .run_report import" in init, "run_report 没被 __init__ 导入"
    m = re.search(r"return \[(.*?)\]", init, re.S)
    assert m, "get_node_list 的 return 找不到了"
    assert "_REPORT_NODES" in init[m.end():m.end() + 200], (
        "get_node_list 必须把 _REPORT_NODES 并进节点列表")


def test_report_node_keeps_output_and_ischanged():
    """留一个「报告」输出（接进图里当普通节点也能用）+ 恒 nan 的 IS_CHANGED。"""
    src = open(REPORT_PY, encoding="utf-8").read()
    assert 'io.String.Output("报告"' in src
    assert "IS_CHANGED" in src and "float(\"nan\")" in src, (
        "接进图里时必须强制重读（报告不在任何输入里，缓存会给出旧文本）")


def test_frontend_reads_plugin_api_not_view_endpoint():
    """报告走 /h3chain/projects（每次都回真实 state）。

    不许走 /api/view —— 那是可启发式缓存的 FileResponse，而 h3chain_state.json
    每跑一段就改写，缓存会反复给出旧指针（routes.py list_projects 的注释里
    已经为这个坑留过记录）。
    """
    js = open(REPORT_JS, encoding="utf-8").read()
    assert "/h3chain/projects" in js
    # 只认「真的拿去请求」的形态（注释里解释这个坑不算违规）
    assert '"/api/view' not in js and "'/api/view" not in js and "`/api/view" not in js, (
        "报告不许经 /api/view 读（缓存会给出旧指针）")
    assert "state" in js and "report" in js
    # 必须是「纯展示」：不碰执行图、不碰连线
    assert "registerExtension" in js
    assert "connect(" not in js and "disconnect" not in js, (
        "报告节点不许动连线（免连线的全部意义就在这里）")


def test_frontend_never_throws_during_load():
    """载入期抛异常会把整个扩展链炸掉 —— 拉取必须自己吞掉失败。"""
    js = open(REPORT_JS, encoding="utf-8").read()
    assert "try {" in js and "catch" in js
    assert "addDOMWidget" in js and "addWidget" in js
    # serialize:false —— 纯展示控件不许写进 workflow JSON
    assert "serialize: false" in js
