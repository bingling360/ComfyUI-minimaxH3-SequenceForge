"""perf.py —— 硬件判据与场景策略单测（性能优化工程阶段 0）。

perf.py 的设计前提就是**零 ComfyUI 依赖**（与 grid.py 同级，必须能被只有 pytest
的 managed Python 加载），所以这里不造任何 torch / comfy 替身，直接 import 真模块。

钉住的是「判据」而不是「实现细节」：
- 三条判据 R_v / R_m / R_d 的算法与量不到时的行为（None，不猜）
- 两场景自动分档（计划 §2.2 实测值：A=0.83/0.65，B=1.63/1.62）
- 块交换建议值（计划 §5.2：16GB→25、12GB→32、32GB→0）
- 落盘守卫的三种等级（§3.3）
- parse_state 的类型校验（防「自动档字段永远改不动」那类回归）

跑法（仓库根目录）：
    python -m pytest tests/test_perf.py -q
"""

import json
import logging
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import perf  # noqa: E402


# 计划 §2.2 的两台真实机器。改这些数字 = 改计划，不要顺手改。
CLOUD = {   # 场景 A：云端 Linux 32GB 显存 / 80GB 内存 / 无 swap / 磁盘紧张
    "vram_total_gb": 32.0, "ram_total_gb": 80.0, "ram_avail_gb": 62.0,
    "swap_total_gb": 0.0, "swap_free_gb": 0.0, "disk_free_gb": 5.0,
    "unet_gb": 26.0, "te_gb": 25.9,
}

LOCAL = {   # 场景 B：本地 Windows 16GB 显存 / 32GB 内存 / 有 swap / 大盘
    "vram_total_gb": 16.0, "ram_total_gb": 32.0, "ram_avail_gb": 20.0,
    "swap_total_gb": 8.0, "swap_free_gb": 6.0, "disk_free_gb": 900.0,
    "unet_gb": 26.0, "te_gb": 25.9,
}


# ---- 三条判据 ----

def test_ratio_v_matches_plan_scenarios():
    """R_v 复现计划实测：A=0.83、B≈1.63。

    ⚠ 计划里这两个数字**口径本身就不一致**：A 用 32×0.98=31.36（驱动保留后的
    可用显存），B 却直接用裸 16。本实现统一按「可用显存」算，故 B 得
    26/15.68 = 1.66。差异不改变任何判定（两者都 >1 → local），
    但别照抄计划里的 1.63。
    """
    assert perf.ratio_v(CLOUD) == pytest.approx(0.83, abs=0.005)
    assert perf.ratio_v(LOCAL) == pytest.approx(1.66, abs=0.005)


def test_ratio_m_matches_plan_scenarios():
    """R_m = (UNET+TE) ÷ 内存总量：A=0.65、B=1.62。"""
    assert perf.ratio_m(CLOUD) == pytest.approx(0.65, abs=0.005)
    assert perf.ratio_m(LOCAL) == pytest.approx(1.62, abs=0.01)


def test_ratio_v_uses_usable_vram_not_raw_total():
    """R_v 必须用「可用」显存（总量×0.98）：驱动保留的那 2% 真实拿不到，
    算进分母会让 R_v 偏乐观，进而把「装不下」误判成「装得下」。
    """
    hw = dict(CLOUD, unet_gb=31.5, vram_total_gb=32.0)
    assert perf.ratio_v(hw) > 1.0, "31.5GB 权重 / 32GB 卡：算可用显存应是 >1（装不下）"
    # 用裸总量会得出 0.984 → 误判为装得下
    assert 31.5 / 32.0 < 1.0


def test_ratio_d_is_disk_over_weights():
    """R_d = 可用磁盘 ÷ (UNET+TE)：A 的 5GB 盘连一次落盘都扛不住（<1）。"""
    assert perf.ratio_d(CLOUD) == pytest.approx(5.0 / 51.9, abs=1e-6)
    assert perf.ratio_d(CLOUD) < 1.0
    assert perf.ratio_d(LOCAL) > 1.0


@pytest.mark.parametrize("hw,key", [(CLOUD, "unet_gb"), (LOCAL, "vram_total_gb")])
def test_ratios_return_none_when_unmeasurable(hw, key):
    """任何一条判据缺数据都必须返回 None —— **不能猜**。
    猜出来的档位比没有档位更糟：它会让后面的策略表静默套错场景。
    """
    broken = dict(hw)
    broken[key] = None
    assert perf.ratio_v(broken) is None
    assert perf.ratio_m(dict(broken, ram_total_gb=None)) is None
    assert perf.ratio_d(dict(broken, disk_free_gb=None)) is None


def test_ratios_zero_is_treated_as_missing():
    """0 视同缺失（除零保护）：权重为 0 = 没探到，不是「模型不需要显存」。"""
    assert perf.ratio_v({"unet_gb": 0.0, "vram_total_gb": 32.0}) is None
    assert perf.ratio_v({"unet_gb": 26.0, "vram_total_gb": 0.0}) is None


# ---- 分档 ----

def test_resolve_profile_splits_on_rv_one():
    """R_v ≤ 1 装得下 → cloud；> 1 → local。"""
    assert perf.resolve_profile(CLOUD) == perf.PROFILE_CLOUD
    assert perf.resolve_profile(LOCAL) == perf.PROFILE_LOCAL


def test_resolve_profile_unknown_falls_to_custom():
    """探不到 R_v 时返回 custom（不套任何自动表）——把决定权交回给人。"""
    assert perf.resolve_profile({}) == perf.PROFILE_CUSTOM
    assert perf.resolve_profile({"vram_total_gb": 16.0}) == perf.PROFILE_CUSTOM


# ---- 块交换建议（§5.2）----

@pytest.mark.parametrize("vram,expected", [
    (32.0, 0),     # 装得下 → 不开
    (16.0, 25),    # 计划给 19–25，本式命中 25
    (12.0, 32),    # 计划 31–38
    (8.0, 40),     # 计划 44–50，本式偏保守（换块太多会把 PCIe 压成瓶颈）
])
def test_suggest_blocks_hits_plan_table(vram, expected):
    assert perf.suggest_blocks(26.0, vram) == expected


def test_suggest_blocks_never_exceeds_block_count():
    """显存极小（乃至 0 / 负）时钳到 [0, 50]，不返回荒谬值。"""
    assert perf.suggest_blocks(26.0, 0.5) == perf.H3_DOUBLE_BLOCK_COUNT
    assert perf.suggest_blocks(26.0, 0.0) == 0
    assert perf.suggest_blocks(0.0, 16.0) == 0


def test_suggest_blocks_scales_with_unet_size():
    """同样 16GB 卡，UNET 越大要换出的块越多（不是写死的 25）。"""
    small = perf.suggest_blocks(13.0, 16.0)   # 半大 UNET：16GB 装得下
    big = perf.suggest_blocks(52.0, 16.0)     # 双倍 UNET
    assert small == 0
    assert big > 25


# ---- 落盘守卫（§3.3）----

def test_offload_guard_ok_when_ram_plus_swap_covers_need():
    """内存 + swap ≥ 权重×1.2 → ok。"""
    g = perf.offload_guard(dict(CLOUD, ram_avail_gb=70.0))
    assert g["ok"] is True and g["level"] == "ok"
    assert "卸载可落内存" in g["message"]


def test_offload_guard_warns_when_short():
    """不够 → 明确报出「会落到 swap」，并列出 swap / 磁盘余量（不是静默发生）。"""
    g = perf.offload_guard(CLOUD)      # 62GB have vs 62.3GB need
    assert g["ok"] is False
    assert g["level"] in ("warn", "critical")
    assert "swap" in g["message"]
    assert str(g["need_gb"]) in g["message"] or f"{g['need_gb']:.0f}" in g["message"]


def test_offload_guard_critical_when_way_short():
    """缺口大 → critical，且必须点明「Linux 无 swap 不是变慢而是被 OOM kill」。"""
    g = perf.offload_guard(dict(CLOUD, ram_avail_gb=8.0, swap_free_gb=0.0))
    assert g["level"] == "critical"
    assert "OOM" in g["message"]


def test_offload_guard_skips_when_unmeasurable():
    """探不到权重/内存时跳过（ok=True 且不报警），不制造假警报。"""
    g = perf.offload_guard({})
    assert g["ok"] is True
    assert "跳过" in g["message"]


def test_offload_guard_ratio_is_configurable():
    """系数可调（面板项 offload_guard_ratio）。"""
    base = perf.offload_guard(CLOUD, ratio=1.0)
    strict = perf.offload_guard(CLOUD, ratio=2.0)
    assert base["need_gb"] < strict["need_gb"]


# ---- resolve_perf：自动策略表 ----

def test_resolve_perf_cloud_matches_plan_table():
    p = perf.resolve_perf("auto", CLOUD)
    assert p["profile"] == perf.PROFILE_CLOUD
    assert p["blocks_to_swap"] == 0
    assert p["prefetch_blocks"] == 0
    assert p["non_blocking"] is False
    assert p["unload_unet_seg"] is False
    assert p["max_upscale_scale"] == 4.0
    assert p["cond_cache_size"] == 32
    assert p["decode_chunk_frames"] == 0
    assert p["guard_offload_target"] is True   # 场景 A 的关键项


def test_resolve_perf_local_matches_plan_table():
    p = perf.resolve_perf("auto", LOCAL)
    assert p["profile"] == perf.PROFILE_LOCAL
    assert p["blocks_to_swap"] == 25
    assert p["prefetch_blocks"] == 2
    assert p["non_blocking"] is True
    assert p["unload_unet_seg"] is True
    assert p["max_upscale_scale"] == 1.5
    assert p["cond_cache_size"] == 8
    assert p["decode_chunk_frames"] == 32
    assert p["frames_to_cpu"] is True


def test_resolve_perf_returns_full_contract():
    """返回的必须是**完整** ds.perf（一项不缺），否则前端读字段会拿到 undefined。"""
    p = perf.resolve_perf("auto", CLOUD)
    assert set(p) == set(perf.DEFAULT_PERF)


def test_resolve_perf_overrides_win():
    """用户逐项覆盖必须压过场景表（计划 §7：这张表是建议值，不是强制值）。"""
    p = perf.resolve_perf("auto", CLOUD, {"blocks_to_swap": 10,
                                          "cond_cache_size": 4})
    assert p["blocks_to_swap"] == 10
    assert p["cond_cache_size"] == 4
    assert p["max_upscale_scale"] == 4.0    # 未覆盖的仍跟随场景


def test_resolve_perf_ignores_unknown_and_none():
    """未知键与 None 一律忽略（None 不能把字段刷成空）。"""
    p = perf.resolve_perf("auto", CLOUD, {"nonsense": 1, "blocks_to_swap": None})
    assert "nonsense" not in p
    assert p["blocks_to_swap"] == 0


def test_resolve_perf_unknown_profile_is_custom():
    """非 auto 且不在 PROFILES 里 → custom（沿用默认，等用户自己填）。"""
    p = perf.resolve_perf("weird", CLOUD)
    assert p["profile"] == perf.PROFILE_CUSTOM
    assert p["blocks_to_swap"] == perf.DEFAULT_PERF["blocks_to_swap"]


def test_resolve_perf_auto_without_hw_is_custom():
    """auto 但没硬件数据 → custom，不套表。"""
    assert perf.resolve_perf("auto")["profile"] == perf.PROFILE_CUSTOM


def test_resolve_perf_does_not_mutate_defaults():
    """多次解析不得污染 DEFAULT_PERF / PROFILE_TABLE（否则一次调用全局中毒）。"""
    before = dict(perf.DEFAULT_PERF)
    perf.resolve_perf("auto", LOCAL, {"blocks_to_swap": 7})
    assert perf.DEFAULT_PERF == before
    assert perf.PROFILE_TABLE[perf.PROFILE_LOCAL]["blocks_to_swap"] == 25


def test_resolve_auto_follows_profile():
    """"auto" 只有后端能解析（前端不知道场景），解析结果按场景取值。"""
    assert perf.resolve_auto("auto", perf.PROFILE_CLOUD, False, True) is False
    assert perf.resolve_auto("auto", perf.PROFILE_LOCAL, False, True) is True
    assert perf.resolve_auto(True, perf.PROFILE_CLOUD, False, True) is True  # 非 auto 原样


# ---- parse_state：类型校验 ----

def test_parse_state_accepts_int_for_auto_defaulted_field():
    """回归守卫：`cond_cache_size` 默认是字符串 "auto"，但整数 8 必须收下。

    早先按默认值类型反推，得出「只收 str」，于是前端写回的 8 被当成非法值丢掉
    —— 自动档字段永远改不动。
    """
    assert perf.parse_state({"cond_cache_size": 8})["cond_cache_size"] == 8
    assert perf.parse_state({"frames_to_cpu": True})["frames_to_cpu"] is True
    assert perf.parse_state({"unload_upscaler_cache": "auto"})["unload_upscaler_cache"] == "auto"


def test_parse_state_accepts_float_for_numeric_field():
    """(int, float) 字段收到 1.5 不能被 int 分支吞掉。"""
    assert perf.parse_state({"max_upscale_scale": 1.5})["max_upscale_scale"] == 1.5
    assert perf.parse_state({"offload_guard_ratio": 1.3})["offload_guard_ratio"] == 1.3
    assert perf.parse_state({"max_upscale_scale": 4})["max_upscale_scale"] == 4


def test_parse_state_normalizes_integral_float_to_int():
    """JSON 里的 8.0 就是 8（避免 8.0 一路带到需要 int 的地方）。"""
    out = perf.parse_state({"cond_cache_size": 8.0})
    assert out["cond_cache_size"] == 8
    assert isinstance(out["cond_cache_size"], int)


def test_parse_state_rejects_wrong_types():
    """类型不对 → 丢弃该项回落到默认，不做猜测式强转。"""
    d = perf.DEFAULT_PERF
    out = perf.parse_state({"blocks_to_swap": "x", "profile": 5,
                            "non_blocking": 0, "probe": "yes"})
    assert out["blocks_to_swap"] == d["blocks_to_swap"]
    assert out["profile"] == d["profile"]
    assert out["non_blocking"] == d["non_blocking"]
    assert out["probe"] == d["probe"]


def test_parse_state_keeps_false():
    """False 是合法值，不能被当成「没填」跳过。"""
    assert perf.parse_state({"non_blocking": False})["non_blocking"] is False
    assert perf.parse_state({"guard_offload_target": False})["guard_offload_target"] is False


def test_parse_state_drops_unknown_keys_and_none():
    out = perf.parse_state({"nonsense": 1, "blocks_to_swap": None})
    assert "nonsense" not in out
    assert out["blocks_to_swap"] == perf.DEFAULT_PERF["blocks_to_swap"]


def test_parse_state_bad_input_returns_defaults():
    assert perf.parse_state(None) == perf.DEFAULT_PERF
    assert perf.parse_state("x") == perf.DEFAULT_PERF
    assert perf.parse_state({}) == perf.DEFAULT_PERF


def test_parse_state_does_not_mutate_defaults():
    """返回的是副本：解析结果写回后不得改到 DEFAULT_PERF 本体。"""
    before = dict(perf.DEFAULT_PERF)
    out = perf.parse_state({"blocks_to_swap": 12})
    out["blocks_to_swap"] = 99
    assert perf.DEFAULT_PERF == before


# ---- 报告行 ----

def test_report_line_matches_plan_format():
    """格式与计划 §6.3 概览行同源：`[H3性能] 显存 … R_v … R_m … 档位 …`。"""
    line = perf.report_line(CLOUD)
    assert line.startswith("[H3性能]")
    assert "显存 32.0GB" in line
    assert "内存 80GB" in line
    assert "swap 0GB" in line
    assert "R_v 0.83" in line
    assert "R_m 0.65" in line
    assert perf.PROFILE_LABELS[perf.PROFILE_CLOUD] in line


def test_report_line_shows_question_mark_for_unknown():
    """缺数据显示 "?" 而不是编一个数字——诊断行的价值全在可信。"""
    assert "显存 ?GB" in perf.report_line({})
    assert "R_v ?" in perf.report_line({"vram_total_gb": 32.0})


# ---- 探测函数：best-effort ----

def test_probe_hardware_never_raises_and_holds_keys():
    """probe_hardware 在任何环境都得返回完整 dict（缺项 None，绝不抛）。"""
    hw = perf.probe_hardware()
    for k in ("vram_total_gb", "ram_total_gb", "swap_total_gb", "disk_free_gb",
              "unet_gb", "te_gb", "pcie", "aimdo", "attn_backend"):
        assert k in hw
    assert hw["unet_gb"] is None      # 没传模型 → None，不是 0


def test_probe_hardware_converts_bytes_to_gb():
    hw = perf.probe_hardware(unet_bytes=26.0 * perf.GB, te_bytes=25.9 * perf.GB)
    assert hw["unet_gb"] == pytest.approx(26.0)
    assert hw["te_gb"] == pytest.approx(25.9)


def test_rss_gb_is_positive_or_none():
    """RSS 探不到给 None（本机无 psutil / 无 /proc 时），探到就是正数。"""
    v = perf.rss_gb()
    assert v is None or v > 0


def test_weight_bytes_duck_typed_without_torch():
    """weight_bytes 不能 import torch（否则 perf 就不再能脱离 ComfyUI 单测）。

    用带 .parameters() 的假对象验证：走 ModelPatcher 的 `.model` 与 CLIP 的
    `.cond_stage_model` 两条取内层路径，以及直接用对象本身。
    """
    class _P(object):
        def __init__(self, n, elsize=2):
            self._n, self._e = n, elsize

        def numel(self):
            return self._n

        def element_size(self):
            # 真实 torch 里 element_size 是**方法**不是属性，替身必须同形，
            # 否则 `p.element_size()` 会在替身上炸、反过来冤枉被测代码
            return self._e

    class _Inner(object):
        def parameters(self):
            return [_P(1000, 2), _P(500, 4)]

    assert perf.weight_bytes(_Inner()) == 1000 * 2 + 500 * 4
    assert perf.weight_bytes(type("W", (), {"model": _Inner()})()) == 4000
    assert perf.weight_bytes(type("W", (), {"cond_stage_model": _Inner()})()) == 4000


def test_weight_bytes_returns_none_when_unavailable():
    assert perf.weight_bytes(None) is None
    assert perf.weight_bytes(object()) is None
    assert perf.weight_bytes(type("E", (), {"parameters": lambda self: (_ for _ in ()).throw(
        RuntimeError("boom"))})()) is None


# ---- watch_model_loads：二采换页探测（§5.3）----

def test_watch_model_loads_counts_matching_records():
    with perf.watch_model_loads() as hits:
        logging.info("Requested to load MiniMaxH3")
        logging.info("unrelated noise")
    assert len(hits) == 1
    assert "MiniMaxH3" in hits[0]


def test_watch_model_loads_restores_logger_state():
    """退出必须移除 handler 并还原 level —— 探测不能改变后续日志行为。"""
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    with perf.watch_model_loads():
        assert len(root.handlers) == len(before_handlers) + 1
    assert list(root.handlers) == before_handlers
    assert root.level == before_level


def test_watch_model_loads_captures_even_when_root_above_info():
    """root 高于 INFO 时记录根本不会生成 → 必须临时降级才能探到，退出后还原。"""
    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.WARNING)
    try:
        with perf.watch_model_loads() as hits:
            logging.info("Requested to load MiniMaxH3")
        assert len(hits) == 1, "root=WARNING 时也必须能探到"
    finally:
        root.setLevel(prev)
    assert root.level == prev


def test_watch_model_loads_yields_empty_when_no_load():
    """零换页（§5.3 期待结论）→ 空列表，可据此判定「两遍式没有省头」。"""
    with perf.watch_model_loads() as hits:
        pass
    assert hits == []


# ---- 探测日志（崩溃安全）----

@pytest.fixture
def logfile(tmp_path):
    """每个用例一份独立 JSONL，绝不碰真实的 logs/。"""
    return str(tmp_path / "probe.jsonl")


def _sim_run(logfile, crash_at=None):
    """模拟一次运行：概览 + 两段完整 + 可选第三段中途崩。"""
    perf.emit({"kind": "overview", "line": "[H3性能] 显存 32.0GB · R_v 0.83",
               "guard": ""}, path=logfile)
    perf.emit({"kind": "swap_probe", "seg": 1, "up_swap": False, "loads": 0},
              path=logfile)
    stages = [("base", 1.5, 3.2), ("up", 2.2, 4.6), ("cond", 3.5, 4.8),
              ("unload", 0.12, 4.8), ("refine", 4.0, 5.3), ("decode", 5.7, 6.0)]
    for seg in (1, 2, 3):
        done = stages if crash_at != seg else stages[:4]
        for st, vp, rs in done:
            perf.emit({"kind": "mark", "seg": seg, "stage": st, "label": st,
                       "vram_alloc": 0.12 if st == "unload" else vp,
                       "vram_peak": vp, "rss": rs}, path=logfile)
        if crash_at == seg:
            perf.emit({"kind": "fail", "seg": seg, "where": "二采渲染中断"},
                      path=logfile)
            break


def test_emit_appends_flushed_json_lines(logfile):
    """每 emit 一行就是一条完整 JSON —— 写完即在盘上（SIGKILL 不会丢已写数据）。"""
    assert perf.emit({"kind": "test", "a": 1}, path=logfile) is True
    with open(logfile, encoding="utf-8") as f:
        lines = f.read().strip().split("\n")
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["a"] == 1
    assert "t" in rec and "run" in rec      # 自动补时间戳与 run id


def test_emit_never_raises(tmp_path, logfile):
    """探测绝不能成为故障源：路径非法 / 不可序列化都静默返回 False。

    ⚠ 不可达路径要挑**立刻失败**的那种。别用 `Z:/...`（不存在的盘符）：
    实测 Windows 上 `os.path.isdir("Z:/no/such/dir")` 要 **15 秒**才返回，
    会把整个测试套件拖挂。这里用「父路径是个普通文件」→ NotADirectoryError，秒失败。
    """
    assert perf.emit({"bad": object()}, path=logfile) is False
    blocker = tmp_path / "iam_a_file"
    blocker.write_text("x", encoding="utf-8")
    assert perf.emit({"ok": 1}, path=str(blocker / "x.jsonl")) is False


def test_emit_disabled_by_env_zero(logfile):
    """H3_PERF_LOG=0 关闭落盘（生产环境可彻底关掉）。"""
    old = os.environ.get(perf.LOG_ENV)
    os.environ[perf.LOG_ENV] = "0"
    try:
        assert perf.emit({"kind": "x"}) is False
    finally:
        if old is None:
            os.environ.pop(perf.LOG_ENV, None)
        else:
            os.environ[perf.LOG_ENV] = old


def test_read_events_defaults_to_last_run(logfile):
    """回读默认取最后一次运行 —— 崩溃后最想看的就是「刚才那次」。"""
    _sim_run(logfile)
    events = perf.read_events(path=logfile)
    assert events and len({e["run"] for e in events}) == 1
    assert any(e.get("kind") == "overview" for e in events)


def test_summarize_shows_partial_data_after_crash(logfile):
    """★ 崩溃安全的核心：崩之前采到的点在报告里一个不少，缺的显示 n/a。"""
    _sim_run(logfile, crash_at=3)
    text = "\n".join(perf.summarize(perf.read_events(path=logfile)))
    assert "段   3" in text or "  3 " in text
    # 段3 崩在 cond 之后 → 精化后/解码后无数据，但前四列在
    row3 = [l for l in text.split("\n") if l.strip().startswith("3 ") and "n/a" in l]
    assert row3, "崩溃段必须出现在报告里（带 n/a），不能整段消失"
    assert "中断" in text, "必须标出在哪一段中断"


def test_summarize_exposes_rss_climb_across_segments(logfile):
    """跨段同一列的对比——「多段变卡」的判据就看这条曲线爬不爬。"""
    _sim_run(logfile)
    events = perf.read_events(path=logfile)
    for e in events:
        if e.get("kind") == "mark" and e.get("seg") == 3 and e.get("stage") == "base":
            e["rss"] = 9.9        # 人为抬高末段，模拟累积
    text = "\n".join(perf.summarize(events))
    assert "9.90" in text


def test_summarize_empty_is_graceful():
    assert perf.summarize([]) == ["（无探测数据）"]


def test_summarize_includes_timing_table(logfile):
    """耗时表：「多段变卡」归根结底是**时间**现象，字节曲线看不出来。"""
    perf.emit({"kind": "timing", "seg": 1, "up": 12.3, "cond": 44.1,
               "refine": 61.5, "decode": 9.2}, path=logfile)
    text = "\n".join(perf.summarize(perf.read_events(path=logfile)))
    assert "各段耗时" in text
    for label in ("放大", "条件", "精化", "解码"):
        assert label in text
    assert "127.1" in text, "应给出合计（12.3+44.1+61.5+9.2）"


def test_summarize_timing_missing_stage_is_na(logfile):
    """某阶段没跑到（崩了）→ n/a，且合计只算有数的。"""
    perf.emit({"kind": "timing", "seg": 1, "up": 10.0, "cond": None,
               "refine": None, "decode": None}, path=logfile)
    text = "\n".join(perf.summarize(perf.read_events(path=logfile)))
    assert "n/a" in text


def test_read_events_skips_torn_lines(logfile):
    """被 kill 时可能写出半行 —— 回读必须跳过坏行而不是整个读不出来。"""
    with open(logfile, "w", encoding="utf-8") as f:
        f.write('{"kind":"mark","run":"r1","seg":1,"stage":"base"}\n')
        f.write('{"kind":"mark","run":"r1","seg":1,"stage":"up", "trunc')
    events = perf.read_events(path=logfile)
    assert len(events) == 1 and events[0]["stage"] == "base"


# ---- 平台 / swap 口径（2026-09-22 真机日志复核后补） ----

def test_probe_os_returns_known_name_or_none():
    """平台名必须可见——落盘守卫给哪套建议全靠它。"""
    v = perf.probe_os()
    assert v is None or v in ("Windows", "Linux", "Darwin") or isinstance(v, str)


def test_offload_guard_windows_branch_does_not_cry_oom_killer():
    """Windows 有 pagefile，不会 OOM kill——给 Linux 话术是错的。

    真机日志里守卫在 24GB/64GB/21GB pagefile 的机器上输出了
    「Linux 无 swap 时被 OOM killer 杀掉」，而用户怀疑（很可能就是）Windows。
    判据：平台写 Windows 时不得出现 OOM killer 话术。
    """
    hw = dict(CLOUD, os="Windows", ram_avail_gb=8.0, swap_free_gb=0.0)
    g = perf.offload_guard(hw)
    assert g["level"] == "critical"
    assert "pagefile" in g["message"]
    assert "OOM killer" not in g["message"]


def test_offload_guard_linux_branch_keeps_oom_warning():
    """Linux / 未知平台：保守，继续给 OOM killer 话术（未知时宁可说重）。"""
    g = perf.offload_guard(dict(CLOUD, os="Linux", ram_avail_gb=8.0, swap_free_gb=0.0))
    assert "OOM killer" in g["message"]
    # 平台缺失时也必须保守（老存档 / 探测失败）
    assert "OOM killer" in perf.offload_guard(dict(CLOUD, ram_avail_gb=8.0))["message"]


def test_offload_guard_reports_swap_free_over_total():
    """swap 写成「空闲/总量」——只给一个会被读成前后矛盾（报告行给总量、
    守卫行给空闲，真机上是 21GB vs 0GB）。"""
    hw = dict(CLOUD, os="Linux", ram_avail_gb=8.0,
              swap_free_gb=0.0, swap_total_gb=21.0)
    assert "0/21GB" in perf.offload_guard(hw)["message"]


def test_report_line_carries_platform_and_swap_source():
    """真机排障第一问就是「这机器到底是 Windows 还是 Linux」。"""
    hw = dict(CLOUD, os="Windows", swap_free_gb=3.0, swap_total_gb=21.0,
              swap_source="windows")
    line = perf.report_line(hw)
    assert "平台 Windows" in line
    assert "源 windows" in line
    assert "空闲 3" in line        # 空闲与总量必须同框出现


def test_ram_swap_free_never_exceeds_total():
    """Windows 分支的 swap_free 会被 mmap 文件页顶穿，必须被 clamp 住。

    safetensors mmap 的几十 GB 权重计入 PhysInUse 但不计入 CommitTotal，
    原式 `PF + (PhysInUse − CommitTotal)` 于是能算出 > PF 的值。
    """
    ram, swap = perf.probe_ram_swap()
    if ram is None or swap is None:
        pytest.skip("本机两套接口都探不到内存/swap")
    assert swap["free_gb"] >= 0.0
    assert swap["free_gb"] <= swap["total_gb"] + 1e-6


# ---- weight_bytes：口径对齐 ComfyUI ----

def test_weight_bytes_prefers_patcher_model_size():
    """优先 `ModelPatcher.model_size()`——ComfyUI 自己算「模型多大」的口径。

    按 element_size 累加在量化/混合精度模型上会系统性偏高（真机上与
    ComfyUI 自报的 "MB Staged" 差 2.3 倍），进而让 R_v / R_m / 落盘守卫全虚高。
    """
    class _P(object):
        def numel(self):
            return 1000

        def element_size(self):
            return 2

    class _Inner(object):
        def parameters(self):
            return [_P()]

    class _Patcher(object):
        def model_size(self):
            return 123456

        model = _Inner()

    assert perf.weight_bytes(_Patcher()) == 123456
    # 没有 model_size 的仍走参数累加（CLIP / 裸模块 / 老版本）
    assert perf.weight_bytes(_Inner()) == 2000


def test_weight_bytes_falls_back_when_model_size_broken():
    """model_size() 抛异常 / 返回 0 时不能把探测打成 None，要回落到累加。"""
    class _P(object):
        def numel(self):
            return 10

        def element_size(self):
            return 4

    class _Inner(object):
        def parameters(self):
            return [_P()]

    class _Bad(object):
        def model_size(self):
            raise RuntimeError("boom")

        model = _Inner()

    class _Zero(object):
        def model_size(self):
            return 0

        model = _Inner()

    assert perf.weight_bytes(_Bad()) == 40
    assert perf.weight_bytes(_Zero()) == 40


# ---- classify_loads：换页探测不再假阳性 ----

def test_classify_loads_splits_by_model_name():
    """按模型名分类——总次数里混着 TE/VAE 的既定回载，不能当成重传证据。"""
    hits = ["Requested to load MiniMaxH3TEModel_",
            "Requested to load MiniMaxH3",
            "Requested to load MiniMaxH3VideoVAE"]
    c = perf.classify_loads(hits, "MiniMaxH3")
    assert c["total"] == 3
    assert c["unet_loads"] == 1
    assert c["by_model"]["MiniMaxH3TEModel_"] == 1
    assert c["by_model"]["MiniMaxH3VideoVAE"] == 1


def test_classify_loads_unet_twice_is_the_real_retransfer_signal():
    """UNET ≥2 次才是重传（第 1 次是精化前主动 unload 后必然的回载）。"""
    hits = ["Requested to load MiniMaxH3", "Requested to load MiniMaxH3"]
    assert perf.classify_loads(hits, "MiniMaxH3")["unet_loads"] == 2


def test_classify_loads_no_unet_name_is_zero_not_guess():
    """认不出 UNET 名字时记 0（调用方据此说「证据不足」），不硬凑结论。"""
    c = perf.classify_loads(["Requested to load X"], None)
    assert c["unet_loads"] == 0 and c["total"] == 1
    assert perf.classify_loads([])["total"] == 0


# ---- 显存预算：到底有没有用满 ----

def test_vram_budget_matches_comfy_arithmetic():
    """★ 复现 ComfyUI 的算式：留空 = 0.8GB(保底) + EXTRA_RESERVED_VRAM。

    `load_models_gpu` 的 minimum_memory_required = max(0.8+EXTRA, …)，所以
    EXTRA 被「数了两次」的效果就是至少留空 3.68GB（EXTRA=2.88 时）。
    """
    hw = {"vram_total_gb": 24.0, "unet_gb": 34.0}
    b = perf.vram_budget(hw, {"extra_reserved_gb": 2.88})
    assert b["reserve_gb"] == pytest.approx(3.68)
    assert b["cache_gb"] == pytest.approx(20.32)
    assert b["reread_gb"] == pytest.approx(13.68)


def test_vram_budget_line_is_actionable():
    b = perf.vram_budget({"vram_total_gb": 24.0, "unet_gb": 34.0},
                         {"extra_reserved_gb": 2.88})
    line = perf.vram_budget_line(b)
    assert "主动留空 3.68GB" in line
    assert "可给权重缓存" in line
    assert "每步需重读" in line


def test_vram_budget_degrades_gracefully():
    """探不到 EXTRA 时不能编数字，也不能抛。"""
    assert perf.vram_budget({}) is None
    b = perf.vram_budget({"vram_total_gb": 24.0}, None)
    assert b["reserve_gb"] is None and "预留未探明" in perf.vram_budget_line(b)


def test_vram_slack_hint_flags_underused_vram():
    """★ 峰值远低于总量 → 明确说「没吃满」，并给出方向。"""
    msg = perf.vram_slack_hint(18.0, 24.0)
    assert msg and "没吃满" in msg and "EXTRA_RESERVED_VRAM" in msg


def test_vram_slack_hint_flags_tight_vram():
    msg = perf.vram_slack_hint(22.8, 24.0)
    assert msg and "临界" in msg


def test_vram_slack_hint_silent_in_comfortable_band():
    """4GB > 空余 > 2GB 时保持安静，别每段都唠叨。"""
    assert perf.vram_slack_hint(21.0, 24.0) is None
    assert perf.vram_slack_hint(None, 24.0) is None
    assert perf.vram_slack_hint(0.0, 24.0) is None


# ---- 余量策略（0.35 的 DynamicVRAM 会吃到接近 100%）----

def test_probe_vram_policy_never_raises_and_shape():
    """只读镜像：探不到给 None，探到必须是完整 dict（绝不抛）。"""
    p = perf.probe_vram_policy()
    assert p is None or set(p) == {"vram_headroom_gb", "reserve_vram_gb",
                                   "extra_reserved_gb", "comfy_compiler",
                                   "dynamic_vram"}


def test_vram_headroom_advice_silent_when_headroom_set():
    """给了 --vram-headroom 就不该再唠叨。"""
    pol = {"dynamic_vram": True, "vram_headroom_gb": 1.0, "reserve_vram_gb": None,
           "comfy_compiler": True}
    assert perf.vram_headroom_advice({}, pol) is None


def test_vram_headroom_advice_silent_when_reserve_vram_set():
    """--reserve-vram 是双管（Python 侧 + aimdo 原生），给了也算已处理。"""
    pol = {"dynamic_vram": True, "vram_headroom_gb": 0.0, "reserve_vram_gb": 3.0,
           "comfy_compiler": True}
    assert perf.vram_headroom_advice({}, pol) is None


def test_vram_headroom_advice_silent_without_dynamic_vram():
    """没开 DynamicVRAM 就没有这个问题——不能误报。"""
    pol = {"dynamic_vram": False, "vram_headroom_gb": 0.0, "reserve_vram_gb": None,
           "comfy_compiler": False}
    assert perf.vram_headroom_advice({}, pol) is None
    assert perf.vram_headroom_advice({}, None) is None


def test_vram_headroom_advice_fires_on_default_policy():
    """★ 默认策略（headroom 0 + 无 reserve + DynamicVRAM 开）必须报，且给出动作。"""
    pol = {"dynamic_vram": True, "vram_headroom_gb": 0.0, "reserve_vram_gb": None,
           "comfy_compiler": True}
    msg = perf.vram_headroom_advice({}, pol)
    assert msg and "vram-headroom" in msg
    assert "disable-comfy-compiler" in msg
    # 必须点明「运行时改 EXTRA_RESERVED_VRAM 盖不到 aimdo 原生预留」
    assert "EXTRA_RESERVED_VRAM" in msg


def test_summarize_shows_headroom_advice(logfile):
    perf.emit({"kind": "overview", "line": "[H3性能] 显存 24.0GB",
               "guard": "", "headroom": "⚠ 显存余量策略为默认"}, path=logfile)
    text = "\n".join(perf.summarize(perf.read_events(path=logfile)))
    assert "余量策略" in text


def test_watch_vram_peak_never_raises_and_yields_box():
    """采样线程是诊断设施——任何环境（含无 CUDA）都不能抛、不能挂。

    返回的是可变 dict，退出后读 ["peak"]；量不到保持 None（不编数字）。
    """
    with perf.watch_vram_peak(interval=0.01) as box:
        assert isinstance(box, dict)
        assert "peak" in box
    assert box["peak"] is None or box["peak"] >= 0.0


def test_watch_vram_peak_stops_thread_on_exit():
    """退出后线程必须停：留着会一直轮询显存，污染后续所有计时。"""
    with perf.watch_vram_peak(interval=0.01) as box:
        pass
    n = box["samples"]
    time.sleep(0.05)
    assert box["samples"] == n, "退出后采样数不应再增长"


def test_summarize_shows_local_llm_observability(logfile):
    """本地 GGUF 通道的加速项 / 显存账 / 吞吐必须进报告。"""
    perf.emit({"kind": "local_llm", "model": "qwen.gguf", "model_gb": 12.6,
               "vram_delta": 13.4, "load_s": 8.4, "gen_s": 41.5, "tok_s": 28.4,
               "plan": {"mode": "fast", "flash_attn": True, "kv_quant": True,
                        "n_batch": 2048, "n_ubatch": 512, "n_gpu_layers": -1}},
              path=logfile)
    text = "\n".join(perf.summarize(perf.read_events(path=logfile)))
    assert "本地 LLM" in text
    assert "12.6GB" in text and "28.4 tok/s" in text
    assert "flash_attn=√" in text and "KV=q8_0" in text


def test_summarize_flags_degraded_local_llm(logfile):
    perf.emit({"kind": "local_llm", "model": "q.gguf", "model_gb": 19.0,
               "vram_delta": 8.0, "load_s": 12.0, "gen_s": 90.0, "tok_s": 6.1,
               "plan": {"mode": "degraded", "flash_attn": False,
                        "kv_quant": False, "n_batch": 512}},
              path=logfile)
    text = "\n".join(perf.summarize(perf.read_events(path=logfile)))
    assert "flash_attn=×" in text and "KV=fp16" in text
    assert "已降级" in text


def test_summarize_shows_pre_unload_cost(logfile):
    """放大前那次全卸要能看出「为多少 MB 付了多少秒」。"""
    perf.emit({"kind": "pre_unload", "seg": 1, "net_mb": 659.0,
               "seconds": 4.2}, path=logfile)
    text = "\n".join(perf.summarize(perf.read_events(path=logfile)))
    assert "放大前腾挪" in text
    assert "659MB" in text and "4.2s" in text


def test_summarize_shows_refine_peak(logfile):
    """精化峰值必须进报告——它是「精化前那次全卸该不该做」的判据之一。"""
    perf.emit({"kind": "refine_peak", "seg": 1, "vram_used": 21.5}, path=logfile)
    text = "\n".join(perf.summarize(perf.read_events(path=logfile)))
    assert "精化显存峰值" in text
    assert "21.50" in text


def test_timing_keys_cover_unload():
    """腾挪（unload）必须单独计时——它是「腾挪值不值」的分母。"""
    assert "unload" in perf.TIMING_KEYS
    assert len(perf.TIMING_KEYS) == len(perf.TIMING_LABELS)


def test_summarize_swap_probe_needs_unet_count(logfile):
    """报告里不能只写「回载 N 次」——那会读成「确实在重传」。"""
    perf.emit({"kind": "swap_probe", "seg": 1, "up_swap": False, "loads": 3,
               "unet_loads": 1}, path=logfile)
    text = "\n".join(perf.summarize(perf.read_events(path=logfile)))
    assert "UNET 1 次" in text
    assert "不构成重传证据" in text


def test_read_events_missing_file_is_empty(tmp_path):
    missing = str(tmp_path / "nope.jsonl")     # 同目录存在，只是文件没有 → 秒失败
    assert perf.read_events(path=missing) == []
    assert perf.list_runs(path=missing) == []


def test_list_runs_returns_distinct_runs_in_order(logfile):
    _sim_run(logfile)
    runs = perf.list_runs(path=logfile)
    assert runs and all(isinstance(r, str) for r in runs)


def test_run_id_stable_within_process():
    assert perf.run_id() == perf.run_id()
