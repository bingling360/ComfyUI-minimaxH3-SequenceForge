"""「保存一采分段成片」（keep_base_seg）回归。

背景：二采成功时 `upscale.render_segment` 把高清结果写进 `finals/seg_NNN.mp4`
——**同名覆盖**基础分段，于是「一采那版分段成片」就没了（用户侧的观感是
"成片没有了"）。二采基础设置新增开关 `keep_base_seg`：开启后主循环在调
`_up_hi` **之前**把一采解码结果另存 `finals/base_seg_NNN.mp4`（+ 缩略图），
段卡上「一采分段成片」/「二采分段成片」两个入口分别取两版。

钉七件事：
  1) `upscale.parse_state` 默认关（旧存档零影响），显式 true 才开；
  2) `keep_base_seg` **不进 params_hash**（拨开关不能让既有高清段重做）；
  3) `checkpoint.base_seg_files` 命名；
  4) `save_thumb` / `save_segment_mp4` 支持 `name=` 显式文件名（一采另存走它）；
  5) projects 白名单纳入 `base_videos`（UI 出口路径归一化覆盖到它）；
  6) nodes.py：一采另存必须在 `_up_hi(...)` **之前**、且只在二采范围内；
     列表与 thumbs/videos 槽位对齐（五个 append 点一个不少）；
  7) 前端：defaultUpscale / getDs / upscaleSig 都带 `keep_base_seg`，基础二采
     设置里有该勾选，段卡拆成一采/二采两个按钮，原「▶ 预览」已下线。

跑法（仓库根目录）：
    python -m pytest tests/test_upscale_keep_base_seg.py -q
"""

import ast
import importlib.util
import os
import pathlib
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODES_SRC = pathlib.Path(ROOT) / "nodes.py"
DIRECTOR = pathlib.Path(ROOT) / "web" / "h3_director.js"

try:  # 优先用真实 torch（ComfyUI 环境）
    import torch as _torch
    _REAL_TORCH = True
except Exception:  # pragma: no cover - 无 torch 时走 stub
    _torch = None
    _REAL_TORCH = False


# ---- 以独立包名加载 upscale.py / checkpoint.py（绕开插件 __init__ 的节点注册） ----

def _load_pkg():
    pkg_name = "h3sf_keepbase_probe"
    if pkg_name in sys.modules:
        return sys.modules[pkg_name + ".upscale"], sys.modules[pkg_name + ".checkpoint"]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [ROOT]
    sys.modules[pkg_name] = pkg
    for name in ("checkpoint", "grid"):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{name}", os.path.join(ROOT, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{name}"] = mod
        spec.loader.exec_module(mod)
    spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.upscale", os.path.join(ROOT, "upscale.py"))
    up = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg_name}.upscale"] = up
    if _REAL_TORCH:
        spec.loader.exec_module(up)
    else:  # pragma: no cover - 只在无 torch 环境走到
        stub = types.ModuleType("torch")
        stub.Tensor = type("_StubTensor", (), {})
        prev = sys.modules.get("torch")
        sys.modules["torch"] = stub
        try:
            spec.loader.exec_module(up)
        finally:
            if prev is None:
                sys.modules.pop("torch", None)
            else:
                sys.modules["torch"] = prev
    return up, sys.modules[f"{pkg_name}.checkpoint"]


upscale, checkpoint = _load_pkg()


# ---- ① parse_state：默认关 / 显式开 ----

def test_keep_base_seg_defaults_off():
    cfg = upscale.parse_state({"upscale": {"mode": "跟随生成"}})
    assert cfg is not None
    assert cfg["keep_base_seg"] is False
    # 非布尔真值不算开（只有 === True 才算，与前端 getDs 同口径）
    cfg2 = upscale.parse_state({"upscale": {"mode": "跟随生成", "keep_base_seg": 1}})
    assert cfg2["keep_base_seg"] is False
    cfg3 = upscale.parse_state({"upscale": {"mode": "跟随生成", "keep_base_seg": "yes"}})
    assert cfg3["keep_base_seg"] is False


def test_keep_base_seg_explicit_true():
    cfg = upscale.parse_state({"upscale": {"mode": "跟随生成", "keep_base_seg": True}})
    assert cfg["keep_base_seg"] is True
    # 关闭二采（mode=关闭）时 parse_state 返回 None —— 开关无意义
    assert upscale.parse_state(
        {"upscale": {"mode": "关闭", "keep_base_seg": True}}) is None


# ---- ② 不进指纹 ----

def test_keep_base_seg_not_in_params_hash():
    """拨开关不能让既有高清段判失效重做（它只多存一份产物）。"""
    base = {"mode": "跟随生成", "scale": 2.0}
    off = upscale.parse_state({"upscale": dict(base, keep_base_seg=False)})
    on = upscale.parse_state({"upscale": dict(base, keep_base_seg=True)})
    assert upscale.params_hash(off) == upscale.params_hash(on)
    # 显式确认它没被塞进 _hash_params 的键表
    assert "keep_base_seg" not in upscale._hash_params(on)


# ---- ③ 命名 ----

def test_base_seg_files_naming():
    f = checkpoint.base_seg_files(7)
    assert f["mp4"] == "base_seg_007.mp4"
    assert f["thumb"] == "base_thumb_007.png"


def test_remove_base_seg_only_removes_base_files(tmp_path):
    """失效清理只删一采另存件，**绝不碰 seg_NNN.mp4**（二采关着时它就是当前成片）。"""
    finals = tmp_path / "finals"
    finals.mkdir()
    for n in ("base_seg_002.mp4", "base_thumb_002.png",
              "seg_002.mp4", "thumb_002.png", "uplast_002.png"):
        (finals / n).write_bytes(b"x")
    (tmp_path / "base_seg_002.mp4").write_bytes(b"x")     # 旧格式裸名也清
    checkpoint.remove_base_seg(str(tmp_path), 2)
    assert not (finals / "base_seg_002.mp4").exists()
    assert not (finals / "base_thumb_002.png").exists()
    assert not (tmp_path / "base_seg_002.mp4").exists()
    for keep in ("seg_002.mp4", "thumb_002.png", "uplast_002.png"):
        assert (finals / keep).exists(), f"{keep} 不该被一采另存清理误删"
    # 幂等：再删一次不报错
    checkpoint.remove_base_seg(str(tmp_path), 2)


def test_save_helpers_accept_explicit_name():
    """一采另存靠 name= 走另一套文件名（默认仍是 seg_NNN / thumb_NNN）。"""
    import inspect
    for fn in (checkpoint.save_thumb, checkpoint.save_segment_mp4):
        params = inspect.signature(fn).parameters
        assert "name" in params, f"{fn.__name__} 必须支持显式文件名"
        assert params["name"].default is None, "默认 None = 沿用旧文件名（向后兼容）"


# ---- ④ projects 白名单 ----

def test_projects_media_whitelist_has_base_videos():
    import re
    src = (pathlib.Path(ROOT) / "projects.py").read_text(encoding="utf-8")
    m = re.search(r"_MEDIA_LIST_KEYS\s*=\s*\(([^)]*)\)", src)
    assert m, "找不到 _MEDIA_LIST_KEYS"
    assert "base_videos" in m.group(1), \
        "base_videos 必须进 UI 出口白名单，否则前端拿不到一采分段路径"


# ---- ⑤ nodes.py 接线 ----

def _tree():
    return ast.parse(NODES_SRC.read_text(encoding="utf-8"))


def _find_calls(tree, func_attr):
    """所有形如 <any>.<func_attr>(...) 的调用节点。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == func_attr:
            out.append(node)
    return out


def _lineno(node):
    return node.lineno


def test_nodes_saves_base_before_upscale_call():
    """一采另存必须在 _up_hi 之前 —— 二采致命错误会终止整链，放后面一段都留不下。"""
    src = NODES_SRC.read_text(encoding="utf-8")
    tree = _tree()
    saves = _find_calls(tree, "save_segment_mp4")
    # 一采另存那一处：带了 name= 关键字，且值是 _bf["mp4"]（base_seg_files 的产物）
    base_save = None
    for call in saves:
        for kw in call.keywords:
            if kw.arg == "name":
                base_save = call
                assert isinstance(kw.value, ast.Subscript), \
                    "一采另存的文件名应取自 base_seg_files() 的返回值"
    assert base_save is not None, "没找到带 name= 的 save_segment_mp4 调用（一采另存）"
    # 命名单一真源：checkpoint.base_seg_files
    assert "checkpoint.base_seg_files(g)" in src, \
        "一采另存的文件名必须来自 checkpoint.base_seg_files（命名单一真源）"

    # 只比生成段那一次调用（第 4 个位置参数是 "prompt"）——序章那次在别的分支里
    up_hi = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_up_hi" and len(n.args) > 3
             and isinstance(n.args[3], ast.Constant) and n.args[3].value == "prompt"]
    assert up_hi, "找不到生成段的 _up_hi 调用"
    prompt_up = min(_lineno(n) for n in up_hi)
    assert _lineno(base_save) < prompt_up, "一采另存必须排在 _up_hi 之前"


def test_nodes_base_save_is_guarded_by_scope():
    """只在二采范围内另存：范围外的段 seg_NNN.mp4 本就是基础版，另存=白占盘。"""
    src = NODES_SRC.read_text(encoding="utf-8")
    assert "_keep_base_seg" in src
    assert 'up_cfg.get("keep_base_seg")' in src, \
        "开关必须从 upscale.parse_state 出来的 up_cfg 取（前后端同源）"
    assert "upscale.in_scope(up_cfg, g)" in src, "另存必须限定在二采范围内"


def _base_video_appends(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "append" \
                and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "base_videos":
            out.append(node)
    return out


def test_nodes_base_videos_slot_aligned():
    """base_videos 必须与 thumbs/videos 同槽位对齐：每段恰好 append 一次。

    槽位出口共 7 处（互斥，一段只走一条）：序章 1 / 禁用段 1 / 生成段三态 4
    （另存成功、另存失败、回放沿用、重采样清空）/ 非存档模式 1。
    """
    src = NODES_SRC.read_text(encoding="utf-8")
    appends = _base_video_appends(_tree())
    assert len(appends) == 7, \
        f"base_videos.append 应为 7 处（实际 {len(appends)}），否则槽位会与 thumbs/videos 错位"
    # 逐个点名，防止有人删掉某一路
    assert src.count('base_videos.append("")') == 3, \
        "序章槽 / 重采样未另存 / 非存档模式三处空占位缺一不可"
    assert 'base_videos.append(_old_rec("base_videos", ""))' in src, \
        "禁用段槽位要沿用旧记录（否则重新上链后槽位错位）"
    assert "base_videos.append(_bmp4)" in src, "一采另存结果要进槽位"
    # 声明处必须在主循环之前（与 thumbs/videos 同批初始化）
    assert "base_videos = []" in src
    # 三个 manifest 写盘点都要带上 base_videos（序章全量写 + 两处增量键）
    assert src.count('"base_videos": []') == 1, "序章 manifest 全量写必须带 base_videos"
    assert src.count('"base_videos": base_videos') == 2, \
        "两处 manifest 增量键（逐段 + 兜底回写）都要带 base_videos"


def _load_merged_list():
    """把 nodes.py 的 `_merged_list` 单独抽出来 exec（纯函数，不拖 ComfyUI 依赖）。"""
    tree = _tree()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_merged_list":
            ns = {}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "nodes.py", "exec"), ns)
            return ns["_merged_list"]
    raise AssertionError("nodes.py 里找不到 _merged_list")


def test_merged_list_truncates_when_full():
    """`_ml` 在「新列表已填满 upto」时是**纯截断**，旧记录完全不参与。

    这就是 base_videos「本次没重算的槽位必须沿用旧值」的根本原因：写空串不是
    "没变化"，而是**把旧记录删掉**（槽位越多、影响越大——手动选择模式一次只有
    一段在范围内，其余段的记录会被整片冲空）。
    """
    ml = _load_merged_list()
    assert ml(["", "b", ""], ["", "old_b", "old_c"], 3) == ["", "b", ""]
    # 没填满（审片中段 break 等）才由旧记录补尾
    assert ml(["a"], ["old_a", "old_b"], 2) == ["a", "old_b"]


def _base_seg_branch(tree):
    """生成段那处 if/elif/else（test 里出现 _keep_base_seg）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "_keep_base_seg" in ast.dump(node.test):
            return node
    return None


def test_nodes_base_seg_three_state_branch():
    """三态分支必须齐：另存 / 回放沿用旧记录 / 重采样清空记录并删文件。

    ⚠ 中间那档最容易写错成空串 —— `_ml` 在列表填满时是**纯截断**，写空等于把
    其余段（手动选择模式下不在范围内、或二采关着时的全部段）的记录抹掉，
    段卡上一采入口集体变灰而文件还在盘上。
    """
    src = NODES_SRC.read_text(encoding="utf-8")
    br = _base_seg_branch(_tree())
    assert br is not None, "找不到以 _keep_base_seg 为条件的 if 分支"
    # 收集 if / elif 链与最后的 else 体
    ifs, else_body, cur = [br], [], br
    while len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
        cur = cur.orelse[0]
        ifs.append(cur)
    else_body = cur.orelse
    assert len(ifs) == 2 and else_body, \
        f"应为 if(另存) / elif(回放) / else(重采样) 三态，实际 {len(ifs)} 个条件分支"
    for i, body in enumerate([ifs[0].body, ifs[1].body, else_body]):
        assert any(_base_video_appends(stmt) for stmt in body), \
            f"第 {i + 1} 态缺少 base_videos.append（槽位会错位）"
    # ② 回放沿用旧记录（不是空串）
    assert 'base_videos.append(_bv_old if _bv_ok else "")' in src, \
        "回放态必须沿用旧记录（写空串会抹掉其余段的一采记录）"
    assert '_old_val("base_videos", g)' in src, "旧值必须走 _old_val（与 _ml 同一份快照）"
    # ③ 重采样清空：记录 + 文件一起清（只清记录会让下次回放把旧件当现件沿用）
    assert "checkpoint.remove_base_seg(root, g)" in src, \
        "重采样后必须删掉失效的一采另存件"
    assert any("remove_base_seg" in ast.dump(stmt) for stmt in else_body), \
        "重采样态（else）里要清失效的一采另存件"


def test_nodes_base_videos_in_old_lists_merge():
    """_old_lists 必须含 base_videos，否则审片中段 break 后旧记录会被写没。"""
    src = NODES_SRC.read_text(encoding="utf-8")
    m = src.find("_old_lists = (")
    assert m > 0
    seg = src[m:m + 400]
    assert "base_videos" in seg, "_old_lists 的键表要加 base_videos"


# ---- ⑥ 前端 ----

def test_frontend_defaults_and_normalization():
    d = DIRECTOR.read_text(encoding="utf-8")
    # 出厂默认 + 老存档归一化 + 面板重渲签名，三处都要有
    assert "keep_base_seg: false" in d
    assert "keep_base_seg: upRaw.keep_base_seg === true" in d
    assert "up.keep_base_seg === true" in d
    # 基础二采设置里的勾选
    assert "保存一采分段成片" in d
    assert 'setUpscaleField(node, "keep_base_seg"' in d


def test_frontend_segment_card_two_entries():
    d = DIRECTOR.read_text(encoding="utf-8")
    assert "一采分段成片" in d and "二采分段成片" in d
    assert "▶ 预览" not in d, "原「▶ 预览」按钮应已改名拆成两个入口"
    # segMediaInfo 必须分别给两版 URL（一采回落到段视频本身 / 二采=被覆盖后的高清）
    assert "baseUrl" in d and "upUrl" in d
    assert "mf.base_videos" in d or "(mf.base_videos || [])" in d


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
