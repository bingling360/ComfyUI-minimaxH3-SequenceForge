"""参考视频音频开关的**编号口径**回归（三处同规则，无 ComfyUI 可跑）。

背景：官方 tokenizer 会为每条 `ref_video_audio_N` 插一条 `<Audio j>`（排在
`<Video k>` 之前），而节点对每个参考视频都塞了音轨槽 → 正文里的音频号必须
「先给每个视频的音轨预扣一个」。三处实现同一规则：

    后端 nodes._kind_tokens  /  asset_store.compile_refs  /  前端 h3_director.js paintAll

2026-10-16 加了「参考视频音频 = 不参考」开关（全局机器级）：音轨不注入、
自然也不占号。**这条规则一旦三处不一致，界面显示的编号就会与实跑差 1**
（历史上正是这个 bug 让「独立音频等于没被引用」）。

跑法（仓库根目录）：
    python -m pytest tests/test_refvideo_tokens.py -q
"""

import ast
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_WANT_FUNCS = ("_normalize_order", "_kind_tokens", "_apply_label_tokens",
               "_reference_tags_minimal", "_uncovered_tags")
_WANT_VARS = ("_REF_BRACKET", "_REF_AT", "_OFFICIAL_FIELD_RE")


@pytest.fixture(scope="session")
def kern():
    """从 nodes.py 抽纯函数真跑（nodes 顶层 import comfy，托管 venv 里跑不了）。"""
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FUNCS:
            wanted.append(node)
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _WANT_VARS for t in node.targets):
            wanted.append(node)
    assert {n.name for n in wanted if isinstance(n, ast.FunctionDef)} == set(_WANT_FUNCS)
    ns = {"re": re}
    mod = ast.Module(body=wanted, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), "<nodes-pure>", "exec"), ns)
    # refvideo 真模块：nodes._kind_tokens 的 docstring 引用它，但不 import（纯函数无依赖）
    return types.SimpleNamespace(**ns)


def test_kind_tokens_video_audio_on_by_default(kern):
    """默认（音频开关开）：视频音轨占一个音频号 → 独立音频是 <Audio 2>。"""
    order = [("video", "片头"), ("audio", "BGM"), ("image", "女主")]
    m = kern._kind_tokens(kern._normalize_order(order))
    assert m["片头"] == "<Video 1>"
    assert m["BGM"] == "<Audio 2>"
    assert m["女主"] == "<Picture 1>"


def test_kind_tokens_video_audio_off_shifts_independent_audio(kern):
    """音频开关关：视频音轨不占号 → 独立音频回到 <Audio 1>，视频号不受影响。"""
    order = [("video", "片头"), ("audio", "BGM"), ("image", "女主")]
    m = kern._kind_tokens(kern._normalize_order(order), video_audio=False)
    assert m["片头"] == "<Video 1>"
    assert m["BGM"] == "<Audio 1>"
    assert m["女主"] == "<Picture 1>"


def test_kind_tokens_two_videos_offset(kern):
    """两个视频：开=偏 2、关=不偏（这就是"不预扣会让独立音频整体错位"的原形）。"""
    order = [("video", "甲"), ("video", "乙"), ("audio", "BGM")]
    on = kern._kind_tokens(kern._normalize_order(order))
    off = kern._kind_tokens(kern._normalize_order(order), video_audio=False)
    assert on["BGM"] == "<Audio 3>" and off["BGM"] == "<Audio 1>"
    assert on["乙"] == off["乙"] == "<Video 2>"


def test_apply_label_tokens_passes_flag(kern):
    """正文替换必须与编号同源：同一个开关，@引用 换成同一个 token。"""
    order = [("video", "片头"), ("audio", "BGM")]
    assert kern._apply_label_tokens("看 @片头 配 @BGM", order) == "看 <Video 1> 配 <Audio 2>"
    assert (kern._apply_label_tokens("看 @片头 配 @BGM", order, False)
            == "看 <Video 1> 配 <Audio 1>")


def test_reference_tags_minimal_passes_flag(kern):
    """最小声明块（subject_definitions）里的 <Audio j> 也必须跟着开关走。"""
    order = [("video", "片头"), ("audio", "BGM")]
    on = kern._reference_tags_minimal(order)
    off = kern._reference_tags_minimal(order, False)
    assert "<Audio 2>" in on and "<Audio 2>" not in off
    assert "<Audio 1> is the reference audio" in off


# ---- 另两处同规则实现（源码级：一个在无 Comfy 环境 import 不了、一个在浏览器里） ----

def _src(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


def test_asset_store_pre_deduct_is_gated():
    src = _src("asset_store.py")
    i = src.index("def compile_refs(")
    body = src[i:i + 3000]
    assert "video_audio: bool = True" in body
    assert re.search(r"if video_audio:\s*\n\s*_seen_v = set\(\)", body), \
        "音轨预扣必须整块包在 if video_audio 里"
    assert "compile_segments(registry: dict, segments, video_audio: bool = True)" in src


def test_frontend_paintall_pre_deduct_is_gated():
    src = _src(os.path.join("web", "h3_director.js"))
    assert "if (refvidAudioOn()) {" in src, "paintAll 的预扣必须跟随音频开关"
    i = src.index("if (refvidAudioOn()) {")
    assert "ord.audio += 1" in src[i:i + 600]


def test_routes_compile_refs_passes_audio_flag():
    src = _src("routes.py")
    assert "compile_segments(reg, data[\"segments\"], _va)" in src
    assert "compile_refs(reg, data.get(\"refs\") or [], 1, _va)" in src
    assert "refvideo" in src[:2000] or "import refvideo" in src
