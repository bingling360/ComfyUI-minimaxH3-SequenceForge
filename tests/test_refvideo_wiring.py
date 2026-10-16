"""参考视频缩放的**接线守卫**（源码级，无 ComfyUI 可跑）。

本项目反复栽在「开关建好了、但没接到渲染链上」（force_unload 的正面条件恒假、
render_latent 漏 _vram 形参、base_videos 三态写成空串……）。所以这里不看"有没有
写这个函数"，只看**渲染路径真的走到它**：

  ① refvideo.py 必须零 ComfyUI 依赖（只有标准库）—— 托管 venv 才跑得动单测
  ② nodes 的画布换算委托给 refvideo（单一真源，链画布与参考面板同一个公式）
  ③ 视频入池分两路：归一开 = 只产 `_ref_videos`（不落原分辨率整片）；
     关 = 旧通路逐字不变，并登记进 `_ref_videos`（参考侧读法两条路统一）
  ④ **一采与二采吃同一份**：参考组装与 `upscale.render_segment` 都拿 `_ref_pool_view()`
     —— 否则基础链缩放、高清链不缩放（一条数据两个真相）
  ⑤ 进逐段指纹：只在「引用了视频」且「偏离默认」时加键（旧存档续跑零影响）
  ⑥ 音轨开关关 → `aud = None`（官方那边 `if soundtrack is not None` 不成立）
  ⑦ 路由两个都挂两处（ROUTES + handler 映射），否则探测不到或 404

跑法（仓库根目录）：
    python -m pytest tests/test_refvideo_wiring.py -q
"""

import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import refvideo as rv  # noqa: E402


def _src(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


def _fn(src, name, size=6000):
    """函数名 -> 该函数源码片段（到下一个同级 def 为止；含嵌套函数）。

    ⚠ 不能写死 `\\ndef name(` —— 本次这批函数（_pool_video / _prepare_ref_video /
    _resize_ref_frames）全是 `execute` 里的**嵌套**函数，缩进 8 空格。
    """
    i = src.index("def " + name + "(")
    i = src.rindex("\n", 0, i) + 1          # 回到行首（保住缩进）
    j = src.find("\n    def ", i + 1)       # 下一个同级（4 空格缩进）或顶层 def
    j2 = src.find("\ndef ", i + 1)
    if j < 0 or (0 <= j2 < j):
        j = j2
    return src[i:(j if 0 < j < i + size else i + size)]


# ---------------------------------------------------------------- ① 零依赖

def test_refvideo_has_no_comfy_or_torch_import():
    tree = ast.parse(_src("refvideo.py"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods.add(n.module.split(".")[0])
    # perf 是例外且唯一：它同样是纯模块（零 ComfyUI 依赖），只用来取
    # 「全局设置目录」（refvid.json 与 perf.json 同目录）。
    assert mods <= {"hashlib", "json", "os", "threading", "perf"}, \
        f"refvideo.py 必须零 ComfyUI/torch 依赖，实际 import：{sorted(mods)}"
    assert not (mods & {"torch", "comfy", "comfy_api", "folder_paths", "nodes"})


def test_refvideo_only_relative_import_is_perf():
    """唯一的相对 import 是 `from . import perf`（纯模块，取设置目录用）。"""
    tree = ast.parse(_src("refvideo.py"))
    rel = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level == 1:
            rel.append(n.module or n.names[0].name)
    assert rel == ["perf"], rel


def test_contract_keys_match_types():
    assert set(rv.DEFAULT_REFVID) == set(rv.REFVID_TYPES)
    assert rv.DEFAULT_REFVID["on"] is False, "出厂必须关闭（透传 = 官方行为）"


# ---------------------------------------------------------------- ② 单一真源

def test_nodes_canvas_math_delegates_to_refvideo():
    src = _src("nodes.py")
    assert "_AR_RATIO = refvideo.AR_RATIO" in src
    assert "_MP_OPTIONS = refvideo.MP_OPTIONS" in src
    body = _fn(src, "_resolve_canvas", 1200)
    assert "refvideo.resolve_canvas(ar, mp)" in body


# ---------------------------------------------------------------- ③④ 入池与共用

def test_ensure_pool_tensors_routes_video_through_pool_video():
    src = _src("nodes.py")
    body = _fn(src, "_ensure_pool_tensors", 9000)
    assert "_pool_video(_lbl, _abs, None, _proot)" in body
    assert "_pool_video(_lbl, None, _fn, _proot)" in body
    # 旧的两处直写必须消失（否则归一开时也会落一份原分辨率整片）
    assert 'pool_tensors["video"][_lbl] = _decode_video_file' not in body
    assert 'pool_tensors["video"][_lbl] = _load_input_video' not in body


def test_pool_video_keeps_raw_path_when_off():
    body = _fn(_src("nodes.py"), "_pool_video", 2000)
    assert "if _rv_on:" in body
    assert "_ref_videos[label] = _hit" in body and "return" in body
    # 关着（或归一失败）必须回落到旧通路，且两条路都登记进 _ref_videos
    assert "pool_tensors[\"video\"][label] = _raw" in body
    assert "_ref_videos[label] = _raw" in body


def test_ref_assembly_uses_prepared_videos():
    src = _src("nodes.py")
    assert "def _ref_media(lbl, kind):" in src
    i = src.index("seg_refs = {\"ref_images\": {}")
    seg = src[i:i + 1400]
    assert "_media_t = _ref_media(_lbl, _k)" in seg
    assert 'pool_tensors["video"][_lbl]' not in seg, "参考组装不得再直读原始池"


def test_upscale_gets_the_same_view():
    """一采二采必须同一份参考素材视图。"""
    src = _src("nodes.py")
    assert "def _ref_pool_view():" in src
    assert "seg_prompts, seg_label_orders, _ref_pool_view(), refs," in src, \
        "二采 render_segment 必须吃归一后的视图"


def test_prepare_ref_video_pipeline():
    body = _fn(_src("nodes.py"), "_prepare_ref_video", 4000)
    assert "_media.probe_media(path)" in body
    assert "refvideo.plan(info, _rv_cfg, _maxf, label)" in body
    assert "_media.decode_av(path, 0, _p[\"src_end\"], info.get(\"fps\"))" in body, \
        "必须走帧窗解码（旧通路整片解码是 GB 级常驻）"
    assert "_resize_ref_frames(imgs, _p, _rv_cfg)" in body
    # 音轨开关：关 → None（官方 if soundtrack is not None 不成立）
    assert "if not _rv_audio:" in body and "aud = None" in body


def test_resize_supports_pad_and_resample_choice():
    body = _fn(_src("nodes.py"), "_resize_ref_frames", 1200)
    assert 'comfy.utils.common_upscale' in body
    assert '_cfg.get("resample")' in body
    assert '_p["crop"] == "pad"' in body, "补黑边必须走两步（先等比缩、再补零）"


# ---------------------------------------------------------------- ⑤ 指纹

def test_seg_hash_markers_gated_on_defaults():
    src = _src("nodes.py")
    i = src.index("# 参考视频归一（全局机器级设置）：**只对引用了视频的段**加键")
    seg = src[i:i + 900]
    assert 'any(_k == "video" for _k, _ in seg_label_orders[i])' in seg
    assert "if _rv_on:" in seg and 'f"rv:{refvideo.signature(_rv_cfg)}|{tag}"' in seg
    assert "if not _rv_audio:" in seg and 'f"rva:0|{tag}"' in seg


def test_config_read_once_with_mtime_cache():
    src = _src("nodes.py")
    assert src.count("_rv_cfg = refvideo.current()") == 1
    assert "_rv_audio = bool(_rv_cfg.get(\"audio\", True))" in src
    assert "_rv_on = bool(_rv_cfg.get(\"on\", False))" in src


# ---------------------------------------------------------------- ⑥ 报告

def test_report_lines_are_appended_once():
    src = _src("nodes.py")
    i = src.index("for _line in _rv_report:")
    assert "report.append(_line)" in src[i:i + 200]


# ---------------------------------------------------------------- ⑦ 路由

def test_routes_registered_both_ways():
    src = _src("routes.py")
    # 每个路径两处登记（ROUTES 声明 + handler 映射），refvid 有 GET/POST 两条
    assert src.count('"/h3chain/refvid"') == 4, "ROUTES 与 handler 映射各两条（GET/POST）"
    assert src.count('"/h3chain/refvid_plan"') == 2
    assert "async def refvid_get(" in src
    assert "async def refvid_set(" in src
    assert "async def refvid_plan(" in src
    assert '("POST", "/h3chain/refvid_plan", refvid_plan),' in src


def test_refvid_plan_resolver_is_read_only_and_never_raises():
    body = _fn(_src("routes.py"), "_refvid_src_path", 2200)
    assert "resolve_project_file" in body
    assert "try_library_root" in body or "library" in body
    assert "return None" in body


def test_frontend_api_surface():
    src = _src(os.path.join("web", "h3_api.js"))
    for k in ("refvidGet:", "refvidSet:", "refvidPlan:"):
        assert k in src, k
