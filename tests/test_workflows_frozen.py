"""P4 冻结工作流回归（无 ComfyUI 可跑；画布加载需 Comfy 内验）。

跑法（仓库根目录）：
    python -m pytest tests/test_workflows_frozen.py -q

覆盖：两工作流结构（无镜像加载节点/单线连通/无悬空/控件数不变）、
已下线节点不许回潮（画布/注册表/前端引用三处钉死）、schema 清理与纯函数保留。

P4g（2026-09-20）：H3AssetBundle（资产包）、H3LatentExtract（现抽存档）、
H3LatentUpscale（库内放大）与现抽输入链（LoadVideo/GetVideoComponents）整体下线。
理由：均无执行入口（非 OUTPUT_NODE，报告输出未接下游）且全部前端代码零引用。
资产唯一来源改为导演台状态 ds.ref_assets（前端 poolFromManifest 自拉 manifest）。
"""

import ast
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "web", "h3_default_workflow.js")
EXAMPLE = os.path.join(ROOT, "example_workflows", "备用初始化导演台工作流.json")

# 已下线的画布节点类（资产库时代残留）
REMOVED_NODES = ["H3AssetBundle", "H3AssetHub", "H3MediaToLatent",
                 "H3LatentExtract", "H3LatentUpscale"]
# 已下线的 loader 类型（只服务 H3LatentExtract 的现抽输入链）
REMOVED_LOADERS = ["LoadVideo", "GetVideoComponents"]


def _load_wf(path):
    src = open(path, encoding="utf-8").read()
    m = re.search(r"window\.H3_DEFAULT_WORKFLOW\s*=\s*(\{.*\})\s*;?\s*$", src, re.S)
    return json.loads(m.group(1)) if m else json.loads(src)


# ---- 工作流结构 ----

def _assert_frozen(wf):
    nodes = {n["id"]: n for n in wf["nodes"]}
    # P4g：已下线节点与现抽输入链，一个都不许留在画布上
    for n in wf["nodes"]:
        assert n["type"] not in REMOVED_NODES, (n["id"], n["type"])
        assert n["type"] not in REMOVED_LOADERS, (n["id"], n["type"])
    samplers = [n for n in wf["nodes"] if n["type"] == "H3SeamlessChainSampler"]
    assert len(samplers) == 1
    sampler = samplers[0]
    # 全连线端点/槽位有效，无悬空
    link_ids = set()
    for L in wf["links"]:
        lid_, f, fs, t, ts = L[0], L[1], L[2], L[3], L[4]
        assert lid_ not in link_ids
        link_ids.add(lid_)
        assert f in nodes and t in nodes
        assert 0 <= fs < len(nodes[f].get("outputs", []))
        assert 0 <= ts < len(nodes[t].get("inputs", []))
    for n in wf["nodes"]:
        for o in n.get("outputs", []):
            for x in o.get("links") or []:
                assert x in link_ids
        for i in n.get("inputs", []):
            if i.get("link") is not None:
                assert i["link"] in link_ids
    assert wf["last_node_id"] >= max(nodes) and wf["last_link_id"] >= max(link_ids)
    # 主链不断：模型/VAE/CLIP/报告预览都在
    # 30 = 原 29 + 「参考图像尺寸」「响度对齐强度」- 「一采编码」
    #   （2026-09-23 删「一采编码」：画质档位改由 ⚡ 性能优化弹窗的 encode_profile 管，
    #    节点上再留一份 = 一张表两处入口，两处会打架。新控件仍恒加在末尾。）
    _wv = sampler.get("widgets_values", [])
    assert len(_wv) == 30
    assert _wv[-2:] == ["match", 1.0], _wv[-2:]
    assert any(L[5] == "MODEL" for L in wf["links"])
    # P4b 全冻结：无任何画布外联（提示词/素材全走导演台状态）
    assert not any(n["type"] == "PrimitiveStringMultiline" for n in wf["nodes"])
    # P4g：画布只剩模型链 + 序章（「资产包」输入已删）
    assert sorted(i.get("name") for i in sampler.get("inputs", [])) == sorted([
        "模型", "文本编码器", "视频VAE", "音频VAE",
        "起始视频", "起始视频音轨", "二采模型",
    ])
    # 「二采模型」：可选 MODEL 槽，默认不接线（不接=沿用一采「模型」）
    _up = [i for i in sampler.get("inputs", []) if i.get("name") == "二采模型"]
    assert len(_up) == 1
    assert _up[0].get("type") == "MODEL"
    assert _up[0].get("link") is None
    for i in sampler.get("inputs", []):
        name = i.get("name") or ""
        if name in ("起始视频", "起始视频音轨"):
            assert i.get("link") is None, name
    # P4c：四组参考拉线输入已从 schema 删除，文件里不许残留条目
    for i in sampler.get("inputs", []):
        name = i.get("name") or ""
        assert not name.startswith(("参考图片组", "参考视频组", "参考视频音轨组", "参考音频组")), name
    return sampler


def test_template_frozen():
    sampler = _assert_frozen(_load_wf(TEMPLATE))
    names = [i.get("name") for i in sampler.get("inputs", [])]
    assert "资产包" not in names


def test_example_frozen():
    _assert_frozen(_load_wf(EXAMPLE))


def test_no_mirror_text():
    for p in (TEMPLATE, EXAMPLE):
        src = open(p, encoding="utf-8").read()
        assert "连线常驻" not in src


def test_workflows_carry_no_legacy_copy():
    """说明卡片/文件头不得再教用户去连已下线的节点。"""
    for p in (TEMPLATE, EXAMPLE):
        src = open(p, encoding="utf-8").read()
        for gone in ["H3AssetHub", "转码为 latent", "旁路休眠", "一键放大",
                     "取消 H3LatentExtract 旁路"]:
            assert gone not in src, (p, gone)


# ---- 已下线节点不许回潮 ----

def test_removed_nodes_never_come_back():
    """P4g：画布/注册表/前端引用三处同时钉死，防止资产库残留再长回来。"""
    # 1) 两个工作流文件
    for p in (TEMPLATE, EXAMPLE):
        wf = _load_wf(p)
        for n in wf["nodes"]:
            assert n["type"] not in REMOVED_NODES, (p, n["id"], n["type"])
            assert n["type"] not in REMOVED_LOADERS, (p, n["id"], n["type"])
    # 2) 注册表：import 与 node list 都不许再出现
    init = open(os.path.join(ROOT, "__init__.py"), encoding="utf-8").read()
    assert "from .bundle import" not in init
    assert "from .latent_tools import" not in init
    m = re.search(r"return \[(.*?)\]", init, re.S)
    assert m, "get_node_list return not found"
    listed = m.group(1)
    for gone in REMOVED_NODES:
        assert gone not in listed, gone
    # 3) 模块：bundle.py 整体删除；latent_tools.py 只剩纯函数
    assert not os.path.exists(os.path.join(ROOT, "bundle.py"))
    lt = open(os.path.join(ROOT, "latent_tools.py"), encoding="utf-8").read()
    assert "io.ComfyNode" not in lt and "class H3" not in lt
    # 4) 前端驱动与菜单
    d = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    assert "runUpscaleTool" not in d and "window.H3Director" not in d
    lib = open(os.path.join(ROOT, "web", "h3_library.js"), encoding="utf-8").read()
    assert "upscaleLatent" not in lib and "H3LatentUpscale" not in lib


# ---- 控件顺序（防参数静默串位）----

# 主节点控件在 schema 里的**顺序**。
# ComfyUI 的 widgets_values 是**按位置**序列化的：中间插入/删除一个控件，
# 数量检查照样过，但每个参数会静默串到下一位（步数变成采样器、CFG 变成步数…），
# 而画布上看起来完全正常。比数量错危险得多，所以顺序单独钉死。
MAIN_WIDGET_ORDER = [
    "宽高比", "百万像素", "宽度", "高度", "每段时长", "引导帧数", "种子",
    "步数", "CFG", "采样器", "调度器", "自动存档", "存档目录", "桥帧门控",
    "清晰度阈值", "回退上限", "锚定加噪", "审片模式", "自动保存", "重跑起始段",
    "接缝重摇", "重摇阈值", "重摇上限", "递减锚定", "生成模式", "自动成片",
    "导演台状态",
    # —— 以下两个是后加的控件，**顺序只能在末尾**（中间插入会让其后所有参数静默串位）
    "参考图像尺寸", "响度对齐强度",
]
WIDGETISH = {"String", "Int", "Float", "Boolean", "Combo"}


def _main_schema_inputs():
    """按源码顺序抽主节点 define_schema 的 inputs=[...]：[(名字, 类型, 是否带 control_after_generate)]。"""
    tree = ast.parse(open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "H3SeamlessChainSampler")
    fn = next(f for f in cls.body
              if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
              and f.name == "define_schema")
    call = next(n for n in ast.walk(fn)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "Schema")
    lst = next(k.value for k in call.keywords if k.arg == "inputs")
    return [(e.args[0].value,
             getattr(getattr(e.func, "value", None), "attr", None),
             any(k.arg == "control_after_generate" and getattr(k.value, "value", None) is True
                 for k in e.keywords))
            for e in lst.elts]


def _widget_slots():
    """控件在 widgets_values 里的实际位序；带 control_after_generate 的控件后多占一位。"""
    slots = []
    for name, kind, cag in _main_schema_inputs():
        if kind not in WIDGETISH:
            continue
        slots.append((name, kind))
        if cag:
            slots.append((None, "ControlAfterGenerate"))
    return slots


def test_main_widget_order_pinned():
    """控件顺序钉死（数量对而顺序错 = 全部参数静默串位）。"""
    assert [n for n, _ in _widget_slots() if n] == MAIN_WIDGET_ORDER


def test_widgets_values_align_semantically():
    """widgets_values 逐位对齐：长度=控件数+附加位，且关键位类型/取值落在对的控件上。"""
    slots = _widget_slots()
    pos = {n: i for i, (n, _) in enumerate(slots) if n}
    for path in (TEMPLATE, EXAMPLE):
        wf = _load_wf(path)
        wv = next(n for n in wf["nodes"]
                  if n["type"] == "H3SeamlessChainSampler").get("widgets_values", [])
        assert len(wv) == len(slots), (path, len(wv), len(slots))
        # 若整体错位一位，这几条会立刻红
        assert isinstance(wv[pos["宽度"]], int), (path, wv[pos["宽度"]])
        assert isinstance(wv[pos["高度"]], int), (path, wv[pos["高度"]])
        assert isinstance(wv[pos["步数"]], int), (path, wv[pos["步数"]])
        assert isinstance(wv[pos["采样器"]], str), (path, wv[pos["采样器"]])
        assert isinstance(wv[pos["调度器"]], str), (path, wv[pos["调度器"]])
        # 导演台状态是 JSON 字符串（错位时会变成别的控件的短值）
        assert isinstance(wv[pos["导演台状态"]], str) and wv[pos["导演台状态"]].startswith("{"), path
        # 种子的附加位紧跟其后，且取值合法
        assert wv[pos["种子"] + 1] in ("fixed", "increment", "decrement", "randomize"), path


# ---- schema 与注册 ----

def test_asset_pack_input_removed():
    """P4g：画布「资产包」输入已删除，资产唯一来源是导演台状态 ds.ref_assets。"""
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert 'io.String.Input("资产包"' not in src
    assert "normalize_pack" not in src      # 覆盖 ds 的那条支路已摘除
    assert "ds.ref_assets" in src           # 导演台来源仍在
    # 签名里也不许再留形参（位置传参会整体错位）
    assert "资产包=" not in src


def test_ref_groups_removed_from_schema():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    for name in ["参考图片组", "参考视频组", "参考视频音轨组", "参考音频组",
                 "首帧图片", "尾帧图片", "每段尾帧锚定", "资产包"]:
        assert ('Input("%s"' % name) not in src, name
    # P4d：画布提示词组 autogrow 已删除（提示词只走导演台状态）
    assert 'Autogrow.Input("提示词组"' not in src
    # 冻结单线、序章与兜底保留
    assert 'io.Image.Input("起始视频"' in src
    assert 'io.String.Input("导演台状态"' in src
    # 主节点输出只剩 4 个（分段列表输出已删除）
    assert 'Output("分段图像"' not in src and 'Output("分段音频"' not in src
    for name in ['Output("图像"', 'Output("音频"', 'Output("帧率"', 'Output("报告"']:
        assert name in src, name


def test_tool_nodes_removed():
    # P4d/P4g：备用节点类已删除（Hub -> Bundle -> 整体下线；库转被自动专跑取代）
    for path, cls in [("asset_hub.py", "class H3AssetHub"),
                      ("latent_tools.py", "class H3MediaToLatent"),
                      ("latent_tools.py", "class H3LatentExtract"),
                      ("latent_tools.py", "class H3LatentUpscale")]:
        assert cls not in open(os.path.join(ROOT, path), encoding="utf-8").read(), cls
    init = open(os.path.join(ROOT, "__init__.py"), encoding="utf-8").read()
    assert "from .asset_hub import" not in init
    m = re.search(r"return \[(.*?)\]", init, re.S)
    assert m, "get_node_list return not found"
    listed = m.group(1)
    for gone in REMOVED_NODES:
        assert gone not in listed, gone
    for keep in ["H3SeamlessChainSampler", "H3SeamDoctor"]:
        assert keep in listed, keep
    # 纯函数保留（路由/主节点/单测仍在用）
    assert "def run_transcode_job(" in open(
        os.path.join(ROOT, "latent_tools.py"), encoding="utf-8").read()
    assert "def normalize_pack(" in open(
        os.path.join(ROOT, "asset_hub.py"), encoding="utf-8").read()
