"""链参数不再强制整链一致（2026-10-12）回归。

跑法（仓库根目录）：
    python -m pytest tests/test_chain_resolution_relax.py -q

背景：用户反馈「改个分辨率还要切项目」——旧版把画幅/时长/参数指纹写进**自动项目
目录名**，且 `checkpoint.assert_match` 对分辨率不一致直接抛 ValueError。本次改动：

1. 自动项目目录名固定 `h3chain`（与参数无关），并沿用最近更新过的旧版自动目录；
2. `assert_match` 从不抛错，只回报差异（分辨率也只是回报）；
3. 相邻段分辨率不同 → 跳过「引用上段尾帧」与接缝测量（只提示）；
4. 全链分辨率不一致 → 不自动成片（只提示）。
"""

import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ck(tmp_path, monkeypatch):
    """checkpoint 模块 + 假的 folder_paths（get_output_directory → tmp_path）。"""
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "folder_paths", fp)
    return _load("h3_ckpt_resrelax", "checkpoint.py")


# ---------------------------------------------------------------- 目录命名

def test_auto_dir_name_ignores_params(ck):
    """改分辨率/时长不得换项目目录——这正是「改个分辨率还要切项目」的根因。"""
    a = ck.ckpt_dir({"width": 1376, "height": 768, "length": 197, "ctx": 22})
    b = ck.ckpt_dir({"width": 1920, "height": 1088, "length": 101, "ctx": 39})
    assert a == b, "参数变化不得改变自动项目目录"
    assert os.path.basename(a) == ck.AUTO_PROJECT_NAME == "h3chain"
    assert os.path.isdir(os.path.join(a, "finals")), "四库子目录照建"


def test_custom_dir_still_wins(ck):
    d = ck.ckpt_dir({"width": 1, "height": 1}, "我的项目")
    assert os.path.basename(d) == "我的项目"


def test_adopts_most_recent_legacy_auto_project(ck, tmp_path):
    """固定名还不存在时沿用旧版自动目录，老用户的自动项目不会凭空消失。"""
    proot = os.path.join(str(tmp_path), "h3_projects")
    for name, mt in (("h3chain_1376x768_197f_ctx22_ab12cd34", 100.0),
                     ("h3chain_1920x1088_101f_ctx39_ff00aa11", 200.0)):
        d = os.path.join(proot, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        os.utime(d, (mt, mt))
    got = ck.ckpt_dir({"width": 1376, "height": 768})
    assert os.path.basename(got) == "h3chain_1920x1088_101f_ctx39_ff00aa11"


def test_legacy_without_manifest_is_not_adopted(ck, tmp_path):
    proot = os.path.join(str(tmp_path), "h3_projects")
    os.makedirs(os.path.join(proot, "h3chain_1376x768_197f_ctx22_ab12cd34"), exist_ok=True)
    got = ck.ckpt_dir({"width": 1376, "height": 768})
    assert os.path.basename(got) == ck.AUTO_PROJECT_NAME


def test_fixed_name_wins_over_legacy(ck, tmp_path):
    proot = os.path.join(str(tmp_path), "h3_projects")
    legacy = os.path.join(proot, "h3chain_1376x768_197f_ctx22_ab12cd34")
    os.makedirs(legacy, exist_ok=True)
    with open(os.path.join(legacy, "manifest.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    os.makedirs(os.path.join(proot, ck.AUTO_PROJECT_NAME), exist_ok=True)
    got = ck.ckpt_dir({"width": 1376, "height": 768})
    assert os.path.basename(got) == ck.AUTO_PROJECT_NAME


# ---------------------------------------------------------------- 结构钉死

def _src(filename):
    with open(os.path.join(ROOT, filename), encoding="utf-8") as f:
        return f.read()


def test_no_hard_error_on_resolution_mismatch():
    """全仓不许再出现「分辨率不一致 → 报错阻断」的硬拦。"""
    src = _src("nodes.py")
    assert "请把分辨率改回存档值" not in src
    assert "分辨率是唯一硬约束" not in src
    assert "唯一硬约束——C/H/W 不同则 latent 桥拼不上" not in src


def test_bridge_handoff_guards_cross_resolution():
    src = _src("nodes.py")
    assert "跨分辨率不生成尾帧桥" in src
    assert "跳过自动引用上段尾帧" in src
    assert "_nxt_res = _slot_res(_nxt_g)" in src


def test_seam_and_reroll_skip_on_resolution_mismatch():
    src = _src("nodes.py")
    assert "_seam_ok = bool(not seg_unlink[i] and _visual_bridge(i) and _res_same_prev)" in src
    assert "and _visual_bridge(i) and _res_same_prev:" in src       # 重摇判据
    assert "跳过接缝测量" in src


def test_redo_anchors_guard_cross_resolution():
    src = _src("nodes.py")
    assert "跳过接下段首帧尾锚" in src


def test_final_compose_is_gated_on_uniform_resolution():
    src = _src("nodes.py")
    assert "自动成片已跳过：全链分辨率不一致" in src
    assert "自动成片已跳过" in src


def test_seg_res_is_recorded_in_manifest():
    """没有逐段分辨率记录，下次运行就分不清旧/新分辨率的段。"""
    src = _src("nodes.py")
    assert '"seg_res": _res_list(done)' in src
    assert '"seg_res": [[int(width), int(height)]]' in src
    assert "def _res_list(upto):" in src


def test_hints_are_printed_to_log():
    """提示要同时进导演台（report）与日志（print [H3链]）。"""
    src = _src("nodes.py")
    assert src.count('print(f"[H3链] ') >= 4


# ------------------------------------------------- 边界：重摇锚点 / 首末段

def test_slot_res_prefers_measured_value():
    """同一次运行里前一段可能刚被重摇（或因缺文件重生成）→ 它的真实分辨率是
    当前画布；拿 `_archived_res` 的旧档值判「跨分辨率」会误杀该保留的接缝锚
    （相邻两段一起重摇时最明显）。"""
    src = _src("nodes.py")
    assert "if g in _seg_res_map:" in src
    assert "_slot_res(prev_g)" in src      # 重摇上锚用实测值
    assert "_slot_res(nxt_g)" in src       # 重摇下锚同理


def test_redo_anchor_edges_are_reported():
    """链首无上段 / 链尾无下段：锚天然缺省，要说清（不是故障、也不是 bug）。"""
    src = _src("nodes.py")
    assert "本段是链首（前面没有段）" in src
    assert "本段是链尾（后面没有段）" in src
    # 两侧锚是**独立**判据：一侧不匹配不该连带掐掉另一侧
    assert "_pv_res != (width, height)" in src
    assert "_nx_res != (width, height)" in src


def test_bridge_break_is_scoped_to_one_boundary():
    """只断这一处接缝，其后各段仍照常引用上段（用户明确要求，不是永久断链）。"""
    src = _src("nodes.py")
    assert src.count("其后各段仍照常引用上段") >= 2      # 序章 + 段间两条路径


def test_next_bridge_gating_respects_resolution():
    """桥本就不会生成时，不必再为本段做桥帧门控（不为不存在的桥牺牲帧数）。"""
    src = _src("nodes.py")
    assert "不必再为它牺牲本段帧数做桥帧门控" in src
    assert "next_wants_bridge = False" in src
