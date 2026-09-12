"""P4 冻结工作流回归（无 ComfyUI 可跑；画布加载需 Comfy 内验）。

跑法（仓库根目录）：
    python -m pytest tests/test_workflows_frozen.py -q

覆盖：bundle 组装纯函数（链接优先/补位/roles/缺文件跳过）、两工作流结构
（Bundle 在/Hub 不在/无镜像加载节点/单线连通/无悬空/控件数不变）、
节点注册与 tooltip。
"""

import json
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "web", "h3_default_workflow.js")
EXAMPLE = os.path.join(ROOT, "example_workflows", "备用初始化导演台工作流.json")
LOADER_TYPES = {"LoadImage", "LoadVideo", "LoadAudio", "GetVideoComponents"}


def _load_top(name, path, patches=None):
    src = open(path, encoding="utf-8").read()
    if patches:
        for a, b in patches:
            src = src.replace(a, b)
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    sys.modules[name] = mod
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


@pytest.fixture(scope="session")
def bundle():
    return _load_top("bundle", os.path.join(ROOT, "bundle.py"))


def _load_wf(path):
    src = open(path, encoding="utf-8").read()
    m = re.search(r"window\.H3_DEFAULT_WORKFLOW\s*=\s*(\{.*\})\s*;?\s*$", src, re.S)
    return json.loads(m.group(1)) if m else json.loads(src)


# ---- bundle 纯函数 ----

def test_bundle_links_first(bundle):
    gl = [{"asset_id": "a_111111111111", "kind": "image", "file": "images/a_x.png"}]
    links = [{"asset_id": "a_111111111111", "alias": "主角", "kind": "image",
              "roles": ["首帧图", "野标注"]}]
    legacy = [{"label": "主角", "kind": "video", "file": "assets/old.mp4"},
              {"label": "配乐", "kind": "audio", "file": "assets/m.mp3"}]
    entries, warns = bundle.build_bundle_pack(legacy, links, gl)
    assert [e["label"] for e in entries] == ["主角", "配乐"]  # 链接胜出，legacy 补位
    assert entries[0] == {"label": "主角", "kind": "image", "file": "images/a_x.png",
                          "asset_id": "a_111111111111", "roles": ["首帧图"]}
    assert warns == []
    assert entries[1] == {"label": "配乐", "kind": "audio", "file": "assets/m.mp3"}


def test_bundle_missing_global_skipped(bundle):
    entries, warns = bundle.build_bundle_pack(
        None, [{"asset_id": "a_222222222222", "alias": "幽灵", "kind": "image"}], [])
    assert entries == [] and len(warns) == 1 and "幽灵" in warns[0]


def test_bundle_empty(bundle):
    assert bundle.build_bundle_pack(None, None, None) == ([], [])


# ---- 工作流结构 ----

def _assert_frozen(wf):
    nodes = {n["id"]: n for n in wf["nodes"]}
    # P4e：loader 类型只允许工具链 58/59（现抽视频源/拆分），且必须旁路休眠
    for n in wf["nodes"]:
        if n["type"] in LOADER_TYPES:
            assert n["id"] in (58, 59) and n.get("mode") == 2, n["id"]
    assert not any(n["type"] == "H3AssetHub" for n in wf["nodes"])
    samplers = [n for n in wf["nodes"] if n["type"] == "H3SeamlessChainSampler"]
    assert len(samplers) == 1
    sampler = samplers[0]
    bundles = [n for n in wf["nodes"] if n["type"] == "H3AssetBundle"]
    assert len(bundles) == 1
    b = bundles[0]
    assert b["outputs"][0]["name"] == "资产包" and b["outputs"][1]["name"] == "报告"
    assert b.get("widgets_values") == [""]
    ins = [i for i in sampler.get("inputs", []) if i.get("name") == "资产包"]
    assert len(ins) == 1 and ins[0].get("type") == "STRING"
    lid = ins[0]["link"]
    assert lid is not None
    assert b["outputs"][0]["links"] == [lid]
    link = next(L for L in wf["links"] if L[0] == lid)
    assert (link[1], link[3]) == (b["id"], sampler["id"])
    assert link[2] == 0 and link[5] == "STRING"
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
    # 主链不断：模型/VAE/CLIP/提示词/报告预览都在
    assert len(sampler.get("widgets_values", [])) == 29
    assert any(L[5] == "MODEL" for L in wf["links"])
    # P4b 全冻结：无任何画布外联（提示词/素材全走导演台状态 + Bundle）
    assert not any(n["type"] == "PrimitiveStringMultiline" for n in wf["nodes"])
    # P4d：画布媒体/提示词条目已删除，只剩模型链 + 序章 + 资产包
    assert sorted(i.get("name") for i in sampler.get("inputs", [])) == sorted([
        "模型", "文本编码器", "视频VAE", "音频VAE",
        "起始视频", "起始视频音轨", "资产包",
    ])
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
    assert "资产包" in names


def test_example_frozen():
    _assert_frozen(_load_wf(EXAMPLE))


def test_no_mirror_text():
    for p in (TEMPLATE, EXAMPLE):
        src = open(p, encoding="utf-8").read()
        assert "连线常驻" not in src


def test_tool_nodes_wired_bypassed():
    # P4e：工具节点预连线 + 旁路休眠（零成本），槽位按文件下标
    for p in (TEMPLATE, EXAMPLE):
        wf = _load_wf(p)
        nodes = {n["id"]: n for n in wf["nodes"]}
        for nid, ntype in [(56, "H3LatentExtract"), (57, "H3LatentUpscale"),
                           (58, "LoadVideo"), (59, "GetVideoComponents")]:
            assert nid in nodes and nodes[nid]["type"] == ntype, (p, nid)
            assert nodes[nid].get("mode") == 2, (p, nid)
        ext = nodes[56]
        assert [(i.get("name"), i.get("type")) for i in ext["inputs"]] == [
            ("视频帧", "IMAGE"), ("视频VAE", "VAE"), ("音轨", "AUDIO"), ("音频VAE", "VAE")]
        assert ext.get("widgets_values") == [0, 0, "", ""]
        up = nodes[57]
        assert [(i.get("name"), i.get("type")) for i in up["inputs"]] == [
            ("模型", "MODEL"), ("文本编码器", "CLIP")]
        assert up.get("widgets_values") == ["", "", "", "auto", 2.0, "fp16", "关闭", "",
                                            0, 6, 0.35, 1.0, ""]
        by_id = {L[0]: L for L in wf["links"]}
        assert by_id[39][1:] == [59, 0, 56, 0, "IMAGE"]
        assert by_id[41][1:] == [3, 0, 56, 1, "VAE"]
        assert by_id[43][1:] == [1, 0, 57, 0, "MODEL"]
        assert by_id[44][1:] == [2, 0, 57, 1, "CLIP"]
        # 存量输出扇出不断原链
        v3 = next(o for o in nodes[3]["outputs"] if o.get("type") == "VAE")
        assert 3 in v3["links"] and 41 in v3["links"]
        assert wf["last_node_id"] >= 59 and wf["last_link_id"] >= 44


def test_director_upscale_driver():
    d = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    assert "function runUpscaleTool(" in d
    assert "window.H3Director" in d          # 供素材库调用的二采入口
    for sym in ['W("放大模型")', 'setW("项目名", dir)', 'setW("源文件", file)',
                "up.mode = 0", "main.mode = 2", "await app.queuePrompt()",
                "up.mode = prevUp", "main.mode = prevMain"]:
        assert sym in d, sym


# ---- 注册与说明 ----

def test_bundle_registered():
    src = open(os.path.join(ROOT, "__init__.py"), encoding="utf-8").read()
    assert "H3AssetBundle" in src


def test_asset_pack_tooltip():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert "H3AssetBundle" in src and "H3AssetHub" in src


def test_ref_groups_removed_from_schema():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    for name in ["参考图片组", "参考视频组", "参考视频音轨组", "参考音频组",
                 "首帧图片", "尾帧图片", "每段尾帧锚定"]:
        assert ('Input("%s"' % name) not in src, name
    # P4d：画布提示词组 autogrow 已删除（提示词只走导演台状态）
    assert 'Autogrow.Input("提示词组"' not in src
    # 冻结单线、序章与兜底保留
    assert 'io.String.Input("资产包"' in src
    assert 'io.Image.Input("起始视频"' in src
    # 主节点输出只剩 4 个（分段列表输出已删除）
    assert 'Output("分段图像"' not in src and 'Output("分段音频"' not in src
    for name in ['Output("图像"', 'Output("音频"', 'Output("帧率"', 'Output("报告"']:
        assert name in src, name


def test_tool_nodes_removed():
    # P4d：备用节点类已删除（Hub 被 Bundle 取代，库转被自动专跑取代）
    for path, cls in [("asset_hub.py", "class H3AssetHub"),
                      ("latent_tools.py", "class H3MediaToLatent")]:
        assert cls not in open(os.path.join(ROOT, path), encoding="utf-8").read(), cls
    init = open(os.path.join(ROOT, "__init__.py"), encoding="utf-8").read()
    assert "from .asset_hub import" not in init
    m = re.search(r"return \[(.*?)\]", init, re.S)
    assert m, "get_node_list return not found"
    listed = m.group(1)
    for gone in ["H3AssetHub", "H3MediaToLatent"]:
        assert gone not in listed, gone
    for keep in ["H3SeamlessChainSampler", "H3SeamDoctor", "H3AssetBundle",
                 "H3LatentExtract", "H3LatentUpscale"]:
        assert keep in listed, keep
    # 纯函数保留（路由/Bundle/单测仍在用）
    assert "def run_transcode_job(" in open(
        os.path.join(ROOT, "latent_tools.py"), encoding="utf-8").read()
    assert "def normalize_pack(" in open(
        os.path.join(ROOT, "asset_hub.py"), encoding="utf-8").read()
