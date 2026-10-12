"""链参数不再强制整链一致（2026-10-12）回归。

跑法（仓库根目录）：
    python -m pytest tests/test_chain_resolution_relax.py -q

背景：用户反馈「改个分辨率还要切项目」——旧版把画幅/时长/参数指纹写进**自动项目
目录名**，且 `checkpoint.assert_match` 对分辨率不一致直接抛 ValueError。本次改动：

1. 自动项目目录名固定 `h3chain`（与参数无关），并沿用最近更新过的旧版自动目录；
2. `assert_match` 从不抛错，只回报差异（分辨率也只是回报）；
3. 相邻段分辨率不同 → 跳过「引用上段尾帧」与接缝测量（只提示）；
4. 全链分辨率不一致 → 不自动成片（只提示）。

本文件分两层：
  · **行为用例**（跑真逻辑）：`ResolutionLedger` / `anchors.bridge_*` / `ckpt_dir`。
    2026-10-12 审查把分辨率账与桥帧判定从 nodes.py 的巨型闭包里提出来成纯函数，
    就是为了让这两条最容易被改错的口径能被真正测到——旧的字符串断言只能证明
    "注释还在"，抓不到逻辑回归（`_res_list` 用当前画布兜底那个 bug 就是这么漏掉的）。
  · **结构用例**：`nodes.py` 里的接线（无 ComfyUI/torch 环境跑不了整段循环，只能钉住
    "判据接到了哪个纯函数上"）。凡是能写成行为用例的都不写成结构用例。
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


@pytest.fixture(scope="module")
def ANC():
    """anchors 模块（`from . import grid` 相对导入 → 造合成包再按路径加载）。"""
    pkg_name = "h3_resrelax_pkg"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [ROOT]
        sys.modules[pkg_name] = pkg
    for name in ("grid", "guides", "anchors"):
        full = f"{pkg_name}.{name}"
        if full not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                full, os.path.join(ROOT, f"{name}.py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[full] = mod
            spec.loader.exec_module(mod)
    return sys.modules[f"{pkg_name}.anchors"]


def _src(filename):
    with open(os.path.join(ROOT, filename), encoding="utf-8") as f:
        return f.read()


# ================================================================ 行为：目录命名

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


def test_adoption_prefers_manifest_updated_at_over_dir_mtime(ck, tmp_path):
    """「最近更新」优先看 manifest.updated_at：目录 mtime 只在增删文件时变，
    改 manifest / 改段文件都不变，光看它会选错项目。"""
    import json as _json
    proot = os.path.join(str(tmp_path), "h3_projects")
    # 目录 mtime 故意与 updated_at **相反**：a 的目录 mtime 新、updated_at 旧
    for name, mt, upd in (("h3chain_aa_11111111", 500.0, 100.0),
                          ("h3chain_bb_22222222", 100.0, 900.0)):
        d = os.path.join(proot, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            _json.dump({"updated_at": upd}, f)
        os.utime(d, (mt, mt))
    got = ck.ckpt_dir({"width": 1376, "height": 768})
    assert os.path.basename(got) == "h3chain_bb_22222222"


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


# ============================================ 行为：ResolutionLedger（逐段分辨率账）

def _ledger(ck, current=(960, 544), archived=(1376, 768), seg_res=None,
            done=10, redo=(), replayable=True):
    return ck.ResolutionLedger(
        current, archived_wh=archived, archived_seg_res=seg_res, done=done,
        redo_slots=redo,
        replayable=(lambda g: True) if replayable is True
        else (replayable if callable(replayable) else (lambda g: False)))


def test_ledger_snapshot_keeps_archived_value_for_unvisited_slots(ck):
    """**`_res_list` 兜底 bug 的行为回归**（2026-10-12 审查修）。

    审片/重摇中段 break：`done` 是存档里的整链完成数（10），循环只走到第 3 段。
    没走到的槽位必须保留**存档值**——用当前画布兜底会把它们写成"新分辨率"，
    而它们盘上其实是旧分辨率 → 下次运行判「相邻段同分辨率」→ 真去拼 latent 桥
    → C/H/W 不一致崩掉，正是本机制要根除的失败。
    """
    led = _ledger(ck, seg_res=[[1376, 768]] * 10)
    led.record(0, led.slot(0))          # 回放 → 1376×768
    led.record(1, led.slot(1))          # 回放 → 1376×768
    led.record(2, (960, 544))           # 本段按新画布重生成
    snap = led.snapshot(10)
    assert snap[:3] == [[1376, 768], [1376, 768], [960, 544]]
    assert snap[3:] == [[1376, 768]] * 7, (
        "没走到的槽位必须保留存档值；写成当前画布会让下次运行误判相邻段同分辨率")


def test_ledger_snapshot_uses_current_canvas_for_new_slots(ck):
    """本次真会新生成的槽（文件缺失/未存档）→ 当前画布，这是对的那一半。"""
    led = _ledger(ck, seg_res=[[1376, 768]] * 10, replayable=lambda g: g < 1)
    led.record(0, led.slot(0))
    assert led.snapshot(3) == [[1376, 768], [960, 544], [960, 544]]


def test_ledger_slot_prefers_measured_value(ck):
    """同一次运行里前一段可能刚被重摇（或因缺文件重生成）→ 它的真实分辨率是
    **当前画布**；拿存档旧值判「跨分辨率」会误杀该保留的接缝锚（相邻两段一起
    重摇时最明显）。"""
    led = _ledger(ck)
    assert led.slot(3) == (1376, 768), "未处理且可回放 → 存档值"
    led.record(3, (960, 544))            # 本段刚被重摇
    assert led.slot(3) == (960, 544), "实测值必须压过存档值"


def test_ledger_redo_slot_is_not_replay(ck):
    """标了重摇的槽本次会重新生成 → 用当前画布，不算回放。"""
    led = _ledger(ck, redo={4})
    assert led.slot(3) == (1376, 768)
    assert led.slot(4) == (960, 544)


def test_ledger_no_ckpt_means_always_current(ck):
    """use_ckpt 关着（done=0）→ 没有「存档回放」这回事，一律当前画布。"""
    led = _ledger(ck, done=0)
    assert led.slot(0) == (960, 544)
    assert led.snapshot(2) == [[960, 544], [960, 544]]


def test_ledger_archived_of_falls_back_to_chain_value(ck):
    """旧档没有 `seg_res` 逐段记录 → 整链按 manifest.params 的宽高算；越界同理。"""
    led = _ledger(ck, seg_res=None)
    assert led.archived_of(0) == (1376, 768)
    led2 = _ledger(ck, seg_res=[[800, 600]])
    assert led2.archived_of(0) == (800, 600)
    assert led2.archived_of(1) == (1376, 768)


def test_ledger_archived_of_ignores_malformed_rows(ck):
    """逐段记录被写坏（长度不对/非数字/None）→ 回落整链值，不许抛。"""
    led = _ledger(ck, seg_res=[[1, 2, 3], ["a", "b"], None, [640, 360]])
    assert led.archived_of(0) == (1376, 768)
    assert led.archived_of(1) == (1376, 768)
    assert led.archived_of(2) == (1376, 768)
    assert led.archived_of(3) == (640, 360)


def test_ledger_distinct_is_the_final_compose_gate(ck):
    """全链分辨率不一致（>1 种）→ 成片闸门跳过自动合成；禁用段剔除后不算。"""
    led = _ledger(ck, done=3)
    led.record(0, (1376, 768))
    led.record(1, (1376, 768))
    led.record(2, (960, 544))
    assert led.distinct() == {(1376, 768), (960, 544)}
    assert led.distinct({2}) == {(1376, 768)}, "禁用段不进成片，不参与闸门"
    led2 = _ledger(ck, done=2)
    led2.record(0, (1376, 768))
    led2.record(1, (1376, 768))
    assert len(led2.distinct()) == 1, "全链一致 → 闸门放行"


# ================================================ 行为：anchors 的桥来源与帧数

def test_bridge_from_prev_tail_auto_and_explicit(ANC):
    """分辨率只约束「取上一段尾帧」这条通路；外源锚（库/段/视频/图片）先裁后编，
    跨分辨率照样接得上——把它们一起掐掉等于废掉一条能用的显式锚。"""
    assert ANC.bridge_from_prev_tail(None) is True              # 自动桥 = 取上段尾帧
    assert ANC.bridge_from_prev_tail({"src": {"kind": "prev_tail"}}) is True
    assert ANC.bridge_from_prev_tail({"src": {"kind": "segment"}}) is False
    assert ANC.bridge_from_prev_tail({"src": {"kind": "library"}}) is False
    assert ANC.bridge_from_prev_tail({"src": {"kind": "video"}}) is False
    assert ANC.bridge_from_prev_tail({"src": {"kind": "image"}}) is False
    assert ANC.bridge_from_prev_tail({"src": {}}) is True        # kind 缺省 = prev_tail
    assert ANC.bridge_from_prev_tail({}) is True


def test_bridge_extra_frames_zero_when_bridge_skipped(ANC):
    """**跨分辨率裁头 bug 的行为回归**（2026-10-12 审查修）。

    桥不会生成时仍预留 window 帧 → 白生成再裁掉头部 window 帧。没有桥帧可裁，
    裁掉的就是本段真实内容的前 window 帧（默认 22 帧≈0.9s），画面整体前移。
    """
    assert ANC.bridge_extra_frames(22) == 22
    assert ANC.bridge_extra_frames(22, skip_bridge=True) == 0
    assert ANC.bridge_extra_frames(0) == 0
    assert ANC.bridge_extra_frames(None) == 0
    assert ANC.bridge_extra_frames(56, skip_bridge=True) == 0


# ================================================================ 结构：nodes.py 接线

def test_no_hard_error_on_resolution_mismatch():
    """全仓不许再出现「分辨率不一致 → 报错阻断」的硬拦。"""
    src = _src("nodes.py")
    assert "请把分辨率改回存档值" not in src
    assert "分辨率是唯一硬约束" not in src
    assert "唯一硬约束——C/H/W 不同则 latent 桥拼不上" not in src


def test_seg_res_goes_through_ledger_not_current_canvas_fallback():
    """接线钉死：逐段分辨率账只能走 `ResolutionLedger`。

    ⚠ 特别钉住 `_seg_res_map` 这个旧变量**不许复活**——它的
    `.get(x, 当前画布)` 兜底就是 2026-10-12 修掉的那个 bug
    （行为回归见 `test_ledger_snapshot_keeps_archived_value_for_unvisited_slots`）。
    """
    src = _src("nodes.py")
    assert "_ledger = checkpoint.ResolutionLedger(" in src
    assert "_ledger.record(" in src
    assert "_ledger.snapshot(upto)" in src          # `_res_list` 的转发目标
    assert '"seg_res": _res_list(done)' in src
    assert "_ledger.distinct(_off_slots)" in src
    assert "_seg_res_map" not in src, "旧的 `_seg_res_map` 兜底写法不许复活"


def test_skip_f_zeroed_when_bridge_skipped_and_not_on_replay():
    """接线钉死：跨分辨率跳过桥接时本段按「无桥」采样，且**回放段不适用**
    （存档 latent 里已烘进桥帧，跟着置 0 会把桥帧当内容输出）。"""
    src = _src("nodes.py")
    assert "_bridge_off_res = bool(_has_src and not _res_same_prev and _bridge_from_prev(i))" in src
    assert "_no_bridge_now = _bridge_off_res and not replay" in src
    assert "skip_bridge=(_no_bridge_now or not _has_src)" in src


def test_bridge_handoff_guards_cross_resolution():
    """交棒端：跨分辨率**且**桥取自上一段尾帧才跳过；外源锚照常注入。"""
    src = _src("nodes.py")
    assert "跨分辨率不生成尾帧桥" in src
    assert "跳过自动引用上段尾帧" in src
    assert "if _nxt_res != _cur_res and _bridge_from_prev(_nxt_pi):" in src


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


def test_hints_are_printed_to_log():
    """提示要同时进导演台（report）与日志（print [H3链]）。"""
    src = _src("nodes.py")
    assert src.count('print(f"[H3链] ') >= 4


def test_bridge_break_is_scoped_to_one_boundary():
    """只断这一处接缝，其后各段仍照常引用上段（用户明确要求，不是永久断链）。"""
    src = _src("nodes.py")
    assert src.count("其后各段仍照常引用上段") >= 2      # 序章 + 段间两条路径


def test_redo_anchor_edges_are_reported():
    """链首无上段 / 链尾无下段：锚天然缺省，要说清（不是故障、也不是 bug）。"""
    src = _src("nodes.py")
    assert "本段是链首（前面没有段）" in src
    assert "本段是链尾（后面没有段）" in src
    # 两侧锚是**独立**判据：一侧不匹配不该连带掐掉另一侧
    assert "_pv_res != (width, height)" in src
    assert "_nx_res != (width, height)" in src


def test_next_bridge_gating_uses_same_target_as_handoff():
    """桥帧门控（+自动回退裁帧）只为「下段那座用本段尾帧搭的桥」服务：
    对象段与交棒同一个（`_next_consumer`，越过禁用段），且要求桥取自本段尾帧、
    下段分辨率一致——否则会为一座不存在的桥牺牲本段帧数。"""
    src = _src("nodes.py")
    assert "def _next_wants_bridge(item_i):" in src
    assert "nxt = _next_consumer(item_i)" in src
    assert "or not _bridge_from_prev(_nxt0)" in src
    assert "or _slot_res(_nxt0_g) != _cur_res:" in src
