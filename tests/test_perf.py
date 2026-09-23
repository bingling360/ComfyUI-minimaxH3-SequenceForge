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
    # blocks_prefetch 不在档位表里（官方默认即开，场景 A 全常驻时它本来就是空转）
    assert p["blocks_prefetch"] is True
    assert p["unload_unet_seg"] is False
    assert p["max_upscale_scale"] == 4.0
    assert p["cond_cache_size"] == 32
    assert p["guard_offload_target"] is True   # 场景 A 的关键项


def test_resolve_perf_local_matches_plan_table():
    p = perf.resolve_perf("auto", LOCAL)
    assert p["profile"] == perf.PROFILE_LOCAL
    assert p["blocks_to_swap"] == 25
    assert p["blocks_prefetch"] is True
    assert p["unload_unet_seg"] is True
    assert p["max_upscale_scale"] == 1.5
    assert p["cond_cache_size"] == 8
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
                            "blocks_prefetch": 0, "probe": "yes"})
    assert out["blocks_to_swap"] == d["blocks_to_swap"]
    assert out["profile"] == d["profile"]
    assert out["blocks_prefetch"] == d["blocks_prefetch"]   # 0 不是 bool → 丢弃回落
    assert out["probe"] == d["probe"]


def test_parse_state_keeps_false():
    """False 是合法值，不能被当成「没填」跳过。"""
    assert perf.parse_state({"blocks_prefetch": False})["blocks_prefetch"] is False
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


# ---- 分块「开关 + 参数」两件套（2026-09-23）----
#
# 用户要求：每个阶段给一个「开启按钮」，再给「开启多少」的参数。落到契约表上就是
#   * ff_chunk_on / ff_chunk_tokens / ff_chunk_min_tokens
#   * attn_head_on / attn_head_chunks
#   * upscale_temporal_chunk / upscale_chunk_frames / upscale_overlap
#   * refine_temporal_on / refine_temporal_chunk / refine_temporal_overlap
#   * refine_tile_on / refine_tile / refine_tile_overlap / refine_tile_feather
#   * blocks_swap_on / blocks_to_swap（`blocks_prefetch` 不在此列 —— 它独立生效，
#     不受总开关管，见 perf.DEFAULT_PERF 里块交换那段注释）
# 开关默认关（大显存机器不该被默默改行为），参数默认给非零的建议值（用户开开关
# 就有合理值，不必先想「该填多少」）。
#
# ⚠ 2026-09-23 撤掉 decode_chunk_on / decode_chunk_frames：官方 H3 视频 VAE 已按
#   tile 级分块解码（comfy/sd.py 的 handles_tiling=True，256px 空间块 × 17 帧时序块），
#   再套一层自己的分块是 0 收益、纯多一份实现，故整组删除（不是「暂不接线」）。

CHUNK_SWITCHES = (
    "ff_chunk_on", "attn_head_on",
    "refine_temporal_on", "refine_tile_on", "blocks_swap_on",
)

CHUNK_PAIRS = (
    ("ff_chunk_on", ("ff_chunk_tokens", "ff_chunk_min_tokens")),
    ("attn_head_on", ("attn_head_chunks",)),
    ("upscale_temporal_chunk", ("upscale_chunk_frames", "upscale_overlap")),
    ("refine_temporal_on", ("refine_temporal_chunk", "refine_temporal_overlap")),
    ("refine_tile_on", ("refine_tile", "refine_tile_overlap", "refine_tile_feather")),
    ("blocks_swap_on", ("blocks_to_swap",)),
)


def test_all_chunk_switches_off_by_default():
    """除放大网络时序分块（历史上就是硬编码开）外，其余分块开关**默认必须关**。

    默认开 = 静默改了所有人的输出/行为；分块尤其如此（除 FFN/头分块外都有代价）。
    """
    for k in CHUNK_SWITCHES:
        assert k in perf.DEFAULT_PERF, f"{k} 不在 DEFAULT_PERF"
        assert perf.DEFAULT_PERF[k] is False, f"{k} 默认该关，实际 {perf.DEFAULT_PERF[k]}"
    # 放大网络时序分块是例外：沿袭既有行为，默认开
    assert perf.DEFAULT_PERF["upscale_temporal_chunk"] is True


def test_chunk_switches_and_params_are_all_wired_or_explicitly_unwired():
    """每个分块参数键，必须在「已接线」或「未接线（有理由）」名单里，不能两头不沾。

    两头不沾 = 面板上既不生效也不标「接线中」，用户勾了以为有用 —— 这是最坑的一种。
    """
    wired = set(perf.WIRED_KEYS)
    unwired = set(perf.UNWIRED_KEYS)
    for _, params in CHUNK_PAIRS:
        for p in params:
            assert p in perf.DEFAULT_PERF, f"{p} 不在 DEFAULT_PERF"
            assert p in wired or p in unwired, f"{p} 既没接线也没进未接线名单"


def test_chunk_switch_and_param_do_not_overlap_in_wiring():
    """开关与参数不能一个接线一个没有——那会让参数永远改不动或永远改得动。

    （`refine_tile_on` 与其参数曾整组在未接线名单，tile 接线后已整组转入 WIRED，
    故此处不再需要例外分支。）
    """
    wired = set(perf.WIRED_KEYS)
    for sw, params in CHUNK_PAIRS:
        sw_wired = sw in wired
        for p in params:
            p_wired = p in wired
            assert sw_wired == p_wired, (
                f"{sw}({'已接线' if sw_wired else '未接线'}) 与 {p}"
                f"({'已接线' if p_wired else '未接线'}) 不一致")


def test_chunk_param_defaults_are_sane():
    """参数默认值要「开开关即合理」：该非零的非零，该给建议值的给建议值。"""
    d = perf.DEFAULT_PERF
    assert d["ff_chunk_tokens"] == 4096          # 建议起点（计划实测值）
    assert d["ff_chunk_min_tokens"] == 8192      # 对齐 KJ seq_threshold 语义
    assert d["attn_head_chunks"] == 1            # 1 = 不分块
    assert d["upscale_chunk_frames"] == 32       # 上游同款默认
    assert d["upscale_overlap"] == 0             # 0 = 自动取卷积核宽（只增不减）
    assert d["refine_temporal_overlap"] == 8     # 至少 8 latent token
    assert d["refine_tile"] == "off"
    assert d["refine_tile_overlap"] == 32 and d["refine_tile_feather"] == 16
    # blocks_to_swap = -1 是「跟随档位」哨兵，不是 0 —— 0 会被读成「真的一块都不换」
    # 从而覆盖掉档位表按显存算出来的建议值（16GB→25 等）。见 resolve_perf。
    assert d["blocks_to_swap"] == -1
    # blocks_prefetch 默认 True = **官方默认即开、默认不干预**（关掉才包一层；
    # 官方预取深度写死 1 块，所以它只能是 bool）
    assert d["blocks_prefetch"] is True


def test_decode_chunk_is_gone_for_good():
    """decode 分块整组不许复活。

    2026-09-23 撤除理由：官方 H3 视频 VAE 已经 tile 级分块（comfy/sd.py 的
    handles_tiling=True，256px 空间块 × 17 帧时序块），自己再分一层是 0 收益。
    这条守卫是为了防止日后「哦这里缺个显存开关」又被加回来 —— 加之前先看官方
    已经做过没有。
    """
    for k in ("decode_chunk_on", "decode_chunk_frames"):
        assert k not in perf.DEFAULT_PERF, f"{k} 又被加回 DEFAULT_PERF 了"
        for tbl in perf.PROFILE_TABLE.values():
            assert k not in tbl, f"{k} 又出现在档位表里了"


def test_profile_driven_keys_are_derived_from_profile_table():
    """PROFILE_DRIVEN_KEYS 必须是三张档位表的**并集**，不能手写。

    为什么要有这张表：面板判「这项到底生不生效」原先只有二分（在 WIRED_KEYS /
    不在），于是 `unload_upscaler_cache` 这类「由档位表填值、auto 档下确实生效」
    的键被画成灰色的「接线中」—— 把在用的开关说成没用。
    """
    union = set()
    for tbl in perf.PROFILE_TABLE.values():
        union |= set(tbl)
    assert set(perf.PROFILE_DRIVEN_KEYS) == union, "档位名单与档位表并集不一致"
    # 并集里必须真的有内容（三张表都空的话上面那条会「通过」但其实没烤到）
    assert len(union) >= 8, f"档位表并集太小：{sorted(union)}"
    # ★ 回归守卫：**不能**拿 PROFILE_CLOUD 这类名字去迭代 ——
    # 它们是字符串常量（'cloud'），迭代得到单字符 'c','l','o','u','d'。
    # 这个 bug 真的犯过：名单变成 ('a','c','d','l','m','o','s','t','u')。
    assert not set(perf.PROFILE_DRIVEN_KEYS) <= set("cloudlocalcustom"), (
        "档位名单退化成了单字符集合 —— 十有八九是拿 PROFILE_CLOUD 这类字符串常量迭代了")
    for k in perf.PROFILE_DRIVEN_KEYS:
        assert len(k) > 3 and "_" in k or k.isidentifier(), f"档位键形状可疑：{k!r}"


def test_profile_driven_keys_match_frontend_stub():
    """前端守卫用的桩名单必须与后端**值**一致（跨语言文本比对在 JS 侧做不了）。

    JS 侧只能扒字面量，而 `PROFILE_DRIVEN_KEYS` 是推导式、源码无键名字面量 ——
    所以这条归 Python：真 import 后逐一比对 tests/js/perf_modal_check.js 的桩。
    """
    import re
    p = os.path.join(ROOT, "tests", "js", "perf_modal_check.js")
    with open(p, "r", encoding="utf-8") as f:
        js = f.read()
    m = re.search(r"const UNWIRED_STUB = \[(.*?)\];", js, re.S)
    assert m, "perf_modal_check.js 里找不到 UNWIRED_STUB"
    stub = re.findall(r'"([a-z_]+)"', m.group(1))
    assert sorted(stub) == sorted(perf.UNWIRED_KEYS), (
        "JS 桩的 UNWIRED_STUB 与 perf.UNWIRED_KEYS 不一致 —— 改了后端要同步那个桩"
        f"\n桩：{sorted(stub)}\n后端：{sorted(perf.UNWIRED_KEYS)}")
    # 桩里的档位名单（若存在）也必须与后端一致
    m2 = re.search(r"const PROFILE_DRIVEN_STUB = \[(.*?)\];", js, re.S)
    if m2:
        stub2 = re.findall(r'"([a-z_]+)"', m2.group(1))
        assert sorted(stub2) == sorted(perf.PROFILE_DRIVEN_KEYS), (
            "JS 桩的 PROFILE_DRIVEN_STUB 与 perf.PROFILE_DRIVEN_KEYS 不一致")


def test_unwired_table_is_empty_and_kept():
    """块交换接线（2026-09-23）后**未接线名单为空** —— 空表本身是被钉住的事实。

    两条都要：① 表存在（`perf_get`/`perf_set` 与前端都读它，删了接口会缺字段）；
    ② 它是空的（前端「接线中」那一态因此不再渲染）。空 ≠ 机制没了：下次加未接线
    字段就往这里加，前端不用动。
    """
    assert hasattr(perf, "UNWIRED_KEYS"), "UNWIRED_KEYS 整个删了 —— 它是接口契约"
    assert perf.UNWIRED_KEYS == (), (
        f"未接线名单该是空的（块交换已接线），实际 {sorted(perf.UNWIRED_KEYS)}")


def test_blocks_to_swap_is_wired_and_profile_driven():
    """`blocks_to_swap` 是 wired ∩ profile_driven：机制已接线，但默认值 -1 的语义就是
    「跟随档位」→ 面板照旧显示「跟随档位」而不是「已接线」。"""
    assert "blocks_to_swap" in perf.WIRED_KEYS
    assert "blocks_to_swap" in perf.PROFILE_DRIVEN_KEYS
    assert perf.DEFAULT_PERF["blocks_to_swap"] == -1


def test_wired_and_unwired_never_overlap():
    """同一键不许同时进「已接线」与「未接线」—— 那是自相矛盾的判据。"""
    assert not (set(perf.WIRED_KEYS) & set(perf.UNWIRED_KEYS))


def test_every_field_key_is_classified():
    """DEFAULT_PERF 里的每个键都要能被三态之一判到，不能两头不沾。

    例外是纯内部键（`profile` / `preset` / `probe` 这类由 resolve_perf 消费、
    不进面板的）—— 它们本来就不该出现在面板上，所以不要求进任何名单。
    """
    panel = set(perf.WIRED_KEYS) | set(perf.UNWIRED_KEYS) | set(perf.PROFILE_DRIVEN_KEYS)
    internal = {"profile", "preset", "probe", "cond_cache_size",
                "offload_guard_ratio", "guard_offload_target", "frames_to_cpu",
                "max_upscale_scale", "unload_unet_seg", "unload_before_decode",
                "unload_upscaler_cache"}
    stray = set(perf.DEFAULT_PERF) - panel - internal
    assert not stray, f"这些键既不在面板三态里、也不在内部键白名单：{sorted(stray)}"


def test_plan_ff_chunks_boundaries():
    """FFN 切块边界：这是「数学等价」的落点，边界错了就不等价了。"""
    assert perf.plan_ff_chunks(0, 4096) == []
    assert perf.plan_ff_chunks(-5, 4096) == []
    assert perf.plan_ff_chunks(4096, 0) == [(0, 4096)]      # 0 = 不分块
    # 整除
    assert perf.plan_ff_chunks(8192, 4096) == [(0, 4096), (4096, 8192)]
    # 不整除：末块要收在 n 上，且块间无缝无叠
    assert perf.plan_ff_chunks(10000, 4096) == [(0, 4096), (4096, 8192), (8192, 10000)]
    # 单块
    assert perf.plan_ff_chunks(100, 4096) == [(0, 100)]


def test_plan_ff_chunks_covers_all_tokens_exactly_once():
    """任意切法都必须「不多不少、不重不漏」—— 否则拼接结果与不分块不等价。"""
    for n in (1, 7, 4095, 4096, 4097, 10000, 123457):
        for c in (1, 512, 4096, 8192):
            spans = perf.plan_ff_chunks(n, c)
            assert spans[0][0] == 0, f"n={n} c={c}: 起点不是 0"
            assert spans[-1][1] == n, f"n={n} c={c}: 终点不是 {n}"
            for (s0, e0), (s1, e1) in zip(spans, spans[1:]):
                assert e0 == s1, f"n={n} c={c}: 块间有缝或重叠 ({e0} != {s1})"
                assert e0 > s0, f"n={n} c={c}: 空块 ({s0},{e0})"


def test_plan_ff_chunks_garbage_is_single_block():
    """非法输入不炸，落回单块（等价不分块）—— 绝不能让脏配置变成「只算了前半段」。"""
    assert perf.plan_ff_chunks(None, 4096) == [(0, 0)]
    assert perf.plan_ff_chunks("abc", 4096) == [(0, 0)]


# ---- 精化时序分块（批次 3）----

def test_plan_refine_chunks_single_when_disabled():
    """chunk<=0 / latent 比块短 / 非法输入 -> 单段（等效不分块，零行为变化）。"""
    assert perf.plan_refine_chunks(0, 12, 4) == []
    assert perf.plan_refine_chunks(-3, 12, 4) == []
    assert perf.plan_refine_chunks(27, 0, 4) == [(0, 27, 0, 27)]
    assert perf.plan_refine_chunks(27, 32, 4) == [(0, 27, 0, 27)]
    assert perf.plan_refine_chunks(27, 27, 4) == [(0, 27, 0, 27)]
    assert perf.plan_refine_chunks(None, 12, 4) == []
    assert perf.plan_refine_chunks(27, "x", 4) == [(0, 27, 0, 27)]


def test_plan_refine_chunks_core_covers_the_whole_range():
    """★ 核心不变量：各段 core 的**并集**必须覆盖 [0, n)（不重不漏已成过去式）。

    ⚠ 2026-09-23 语义变更：core 从「硬划分」改为「**咬合重叠**」—— 相邻段在
    咬合带内各写一次、按互补羽化权重混合（这才是「重叠融合」）。所以这里只验
    **并集铺满**（有洞 = 那些 token 权重为 0，是黑洞）。
    """
    for n in (1, 5, 27, 100, 120, 257):
        for c in (2, 7, 12, 16, 64):
            for ov in (0, 1, 4, 8, 64):
                plan = perf.plan_refine_chunks(n, c, ov)
                covered = set()
                for _s, _e, cs, ce in plan:
                    covered.update(range(cs, ce))
                assert covered == set(range(n)), (
                    f"n={n} c={c} ov={ov} 的 core 并集不是 [0,{n})：{plan}")


def test_plan_refine_chunks_interlock_overlap():
    """咬合：内部边界的相邻段 core 必须**真实重叠**（非硬切）。

    回归钉子 —— 早先 core 是硬划分（`e - ov`），重叠从不发生、羽化形同摆设。
    """
    n, c, ov = 120, 16, 8
    plan = perf.plan_refine_chunks(n, c, ov)
    assert len(plan) >= 3
    for (_s0, _e0, _a0, b0), (_s1, _e1, a1, _b1) in zip(plan, plan[1:]):
        assert a1 < b0, f"相邻段 core 不重叠（硬切）：前段止于 {b0}、后段起于 {a1}"
        assert b0 - a1 == max(1, ov - ov % 2), \
            f"咬合带宽度该 = ov（{ov}），实际 {b0 - a1}"


def test_plan_refine_chunks_spans_are_in_bounds_and_overlapping():
    """每段喂入区间必须在 [0,n] 内、非空、且**包住**自己的 core 区。"""
    n, c, ov = 120, 16, 8
    plan = perf.plan_refine_chunks(n, c, ov)
    assert plan[0][0] == 0 and plan[-1][1] == n, "首段须从 0 起、末段须收在 n"
    for s, e, cs, ce in plan:
        assert 0 <= s < e <= n, f"区间越界或为空：({s},{e})"
        assert s <= cs < ce <= e, f"core 必须落在段内：({s},{e}) core({cs},{ce})"
    for (s0, e0, _a, _b), (s1, _e1, _c2, _d2) in zip(plan, plan[1:]):
        assert s1 < e0, f"相邻段没有重叠（s1={s1} >= e0={e0}）—— overlap 没生效"


def test_plan_refine_chunks_overlap_clamped_to_half_chunk():
    """overlap 上限 = 块长的一半（防「净推进趋近 0、段数爆炸」）。

    27 token / 块 8 / 要求 overlap 8 -> 若真按 8 走，每段只前进 0 或 1 个 token，
    会切出几十段、每段各跑一遍完整采样循环。实测过的退化案例就是这个。
    """
    plan = perf.plan_refine_chunks(27, 8, 8)
    assert len(plan) == 6, f"overlap 该被夹到 4（半块），段数不该是 {len(plan)}"
    assert plan[1][0] == 4, f"第二段该从 4 起（overlap 夹到 4），实际 {plan[1][0]}"


def test_feather_weights_shape_and_ramps():
    """羽化权重：首尾各 ramp 个元素**对称**升/降、中间恒 1、且**永不为 0**。

    不为 0 很关键：权重 0 等于把那段信息彻底丢掉（等效黑洞）。
    """
    assert perf.feather_weights(0, 3) == []
    assert perf.feather_weights(-1, 3) == []
    assert perf.feather_weights(10, 0) == [1.0] * 10          # ramp=0 = 不羽化
    assert perf.feather_weights(4, 5) == [1.0] * 4           # ramp 过大 = 全 1
    w = perf.feather_weights(10, 3)
    assert len(w) == 10
    assert w == pytest.approx([0.25, 0.5, 0.75, 1, 1, 1, 1, 0.75, 0.5, 0.25])
    assert all(x > 0 for x in w), "权重不该出现 0"
    # 对称性：w[i] == w[n-1-i]（这是「两块咬合时互补」的几何基础）
    assert all(w[i] == w[len(w) - 1 - i] for i in range(len(w)))


def test_feather_weights_interlock_complement_to_one():
    """★ 两块**咬合**时权重必须互补成 1：`A_tail[k] + B_head[k] == 1`。

    这是「过渡没有硬边」的数学根基。A 取尾端 r 个、B 取首端 r 个（同一个咬合带），
    两者对齐相加必须严格 = 1。
    ⚠ 回归钉子：尾端**不能**被「改成」显式降序 —— 那会让 A、B 同向、和恒为 2 倍
    （`2(i+1)/(r+1)`，中点附近等权混合、远离边界权重更小，过渡形状是错的）。
    """
    r = 8
    w = perf.feather_weights(40, r)
    a_tail = w[-r:]            # A 在咬合带的权重
    b_head = w[:r]             # B 在同一咬合带的权重
    for k in range(r):
        assert a_tail[k] + b_head[k] == pytest.approx(1.0), (
            f"咬合带第 {k} 点不互补：A={a_tail[k]} B={b_head[k]}")
    # A 尾端必须是 B 首端的逆序（同一条曲线的镜像）
    assert a_tail == pytest.approx(list(reversed(b_head)))


def test_feather_weights_sum_is_sane():
    """羽化权重之和应 ≥ 长度的一半 —— 归一化时不会因总权重过小放大噪声。"""
    for n, r in ((16, 8), (32, 8), (12, 4), (100, 16)):
        w = perf.feather_weights(n, r)
        assert sum(w) >= n * 0.5, f"n={n} r={r} 权重和太小：{sum(w)}"


# ---- 精化空间 tile（批次 4）----

def test_tile_grid_unknown_mode_is_off():
    """档位名不认识 -> (1,1)（不切），**绝不**静默切成怪形状。"""
    assert perf.tile_grid("off") == (1, 1)
    assert perf.tile_grid("") == (1, 1)
    assert perf.tile_grid(None) == (1, 1)
    assert perf.tile_grid("9x9") == (1, 1)
    assert perf.tile_grid("2x2") == (2, 2)
    assert perf.tile_grid("1x2") == (1, 2)
    assert perf.tile_grid("2x1") == (2, 1)
    # 大小写 / 空白容错
    assert perf.tile_grid(" 3X3 ") == (3, 3)


def test_plan_tiles_off_returns_single_tile():
    """off / 非法输入 -> 单片（等效不分块，零行为变化）。"""
    assert perf.plan_tiles(64, 64, "off", 8) == [(0, 64, 0, 64, 0, 64, 0, 64, 0)]
    assert perf.plan_tiles(0, 64, "2x2", 8) == []
    assert perf.plan_tiles(None, 64, "2x2", 8) == []
    assert perf.plan_tiles(-8, 64, "2x2", 8) == []


def test_plan_tiles_core_union_covers_whole_canvas():
    """★ 核心不变量：各块 core 的**并集**必须铺满整幅 H×W。

    ⚠ 2026-09-23 语义变更：core 从「不重不漏硬划分」改为「**咬合重叠**」——
    相邻块在咬合带内各写一次、按互补羽化权重混合。所以这里只验并集铺满
    （有洞 = 那些像素权重为 0，是黑洞）。
    """
    for H, W in ((64, 64), (96, 128), (100, 60), (256, 256), (33, 37)):
        for mode in ("2x2", "3x3", "4x4", "2x1", "1x2", "off"):
            for ov in (0, 4, 16, 64):
                plan = perf.plan_tiles(H, W, mode, ov)
                seen = set()
                for _hs, _he, _ws, _we, chs, che, cws, cwe, _ov in plan:
                    for y in range(chs, che):
                        for x in range(cws, cwe):
                            seen.add((y, x))
                expect = {(y, x) for y in range(H) for x in range(W)}
                assert seen == expect, (
                    f"H={H} W={W} {mode} ov={ov}：core 并集未铺满（{len(seen)}/{H * W}）")


def test_plan_tiles_interlock_overlap():
    """咬合：内部边界的相邻块 core 必须**真实重叠**（非硬切）。

    回归钉子 —— 早先 core 是硬划分，重叠从不发生、羽化形同摆设。
    """
    plan = perf.plan_tiles(64, 64, "2x2", 8)
    assert len(plan) == 4
    # 行方向：(0,0) 与 (1,0) 的 core 行区必须重叠 8
    r0_0 = plan[0]      # (0,0)
    r1_0 = plan[2]      # (1,0)
    assert r0_0[5] > r1_0[4], f"行方向 core 不重叠：前块止 {r0_0[5]}、后块起 {r1_0[4]}"
    assert r0_0[5] - r1_0[4] == 8, f"行咬合带该 = 8，实际 {r0_0[5] - r1_0[4]}"
    # 列方向：(0,0) 与 (0,1)
    assert plan[0][7] - plan[1][6] == 8, "列方向咬合带该 = 8"


def test_plan_tiles_spans_in_bounds_and_contain_core():
    """每块喂给采样器的区间必须在画幅内、非空，且**包住**自己的 core 区。"""
    H, W, ov = 128, 96, 20
    plan = perf.plan_tiles(H, W, "2x2", ov)
    assert len(plan) == 4
    for hs, he, ws, we, chs, che, cws, cwe, _ov in plan:
        assert 0 <= hs < he <= H, f"行区间越界或为空：({hs},{he})"
        assert 0 <= ws < we <= W, f"列区间越界或为空：({ws},{we})"
        assert hs <= chs < che <= he, f"core 行必须落在块内：{chs},{che} vs {hs},{he}"
        assert ws <= cws < cwe <= we, f"core 列必须落在块内：{cws},{cwe} vs {ws},{we}"


def test_plan_tiles_auto_downgrades_when_too_small():
    """画幅太小切不动 -> 自动降档（宁可少省显存，也不切碎画面）。

    降档是**逐级减半**，不是一步到单片：40x40 切 4x4 时 4 等分每块 10px
    小于 min_side 16，降到 2x2（每块 20px ≥ 16）就停下 —— 仍切 4 块。
    """
    # 40x40 切 4x4 -> 每块 10px < 16 -> 降到 2x2（每块 20px >= 16）
    assert len(perf.plan_tiles(40, 40, "4x4", 8, min_side=16)) == 4
    # 24x24 切 2x2 -> 每块 12px < 16 -> 降到单片
    assert len(perf.plan_tiles(24, 24, "2x2", 8, min_side=16)) == 1
    # 128x128 切 4x4 -> 每块 32px >= 16 -> 正常 16 块
    assert len(perf.plan_tiles(128, 128, "4x4", 8, min_side=16)) == 16


def test_plan_tiles_overlap_clamped_to_half_block():
    """overlap 上限 = 半块边长（且取偶数）—— 与 plan_refine_chunks 同防退化逻辑。

    64x64 切 2x2 -> 块边长 32 -> overlap 夹到 16（half=8）。
    写回区：首块向右侧多要 8 -> che = 32+8 = 40；次块向左多要 8 -> chs = 32-8 = 24，
    两者咬合带 [24,40) 宽 16。
    """
    # plan 顺序是**行外列内**：(0,0) (0,1) (1,0) (1,1)
    plan = perf.plan_tiles(64, 64, "2x2", 9999)
    assert plan[0][8] == 16, f"生效 overlap 该夹到 16，实际 {plan[0][8]}"
    # 写回区咬合
    assert plan[0][5] == 40, f"(0,0) che 该 = 32+8 = 40，实际 {plan[0][5]}"
    assert plan[2][4] == 24, f"(1,0) chs 该 = 32-8 = 24，实际 {plan[2][4]}"
    assert plan[0][5] - plan[2][4] == 16, "咬合带宽度该 = 16"
    # 画布外缘不扩
    assert plan[0][4] == 0 and plan[0][6] == 0, "首块外缘不该缩"
    assert plan[3][5] == 64 and plan[3][7] == 64, "末块外缘不该扩"


def test_plan_tiles_overlap_even_forced():
    """生效 overlap 必须取偶数（half = ov//2 两边对称、咬合带恰好铺满 ov）。"""
    for req in (7, 9, 15, 16, 31):
        plan = perf.plan_tiles(128, 128, "2x2", req)
        eff = plan[0][8]
        assert eff % 2 == 0, f"请求 {req} -> 生效 {eff} 不是偶数"


def test_split_divides_evenly_remainder_first():
    """`_split` 均分且余数摊给前面的段（末段不会成为新的峰值点）。"""
    assert perf._split(10, 1) == [(0, 10)]
    assert perf._split(10, 2) == [(0, 5), (5, 10)]
    assert perf._split(10, 3) == [(0, 4), (4, 7), (7, 10)]   # 4,3,3
    assert perf._split(9, 3) == [(0, 3), (3, 6), (6, 9)]
    # 每段长度差不超过 1，且首段 >= 末段
    for total, parts in ((100, 7), (33, 4), (7, 8)):
        segs = perf._split(total, parts)
        lens = [e - s for s, e in segs]
        assert sum(lens) == total
        assert max(lens) - min(lens) <= 1
        assert lens == sorted(lens, reverse=True)

# ============================================================ 块交换（接线到官方机制）
#
# 这一组的定位：块交换**没有自己的搬权重代码**（机制在 ComfyUI 那边：DynamicVRAM 的
# vbar 换入 + 官方 block 循环里的预取队列）。本插件只做两件事：
#   ① 把「放出 N 块」折算成 aimdo 的**显存预留**（blocks_to_swap → headroom）
#   ② 给模型装/拆官方预取队列的开关（blocks_prefetch → transformer_options）
# 所以这里测的是**换算与写入纪律**，不是「搬了几块」——后者由 tools/check_blockswap.py
# 在真 torch/aimdo 环境里验。

def test_blockswap_headroom_math():
    """N 块 → 额外预留：块大小 = UNET/50（H3_DOUBLE_BLOCK_COUNT）。"""
    r = perf.blockswap_headroom(0, 20.0, 40.0)
    assert r["extra_gb"] == 0.0 and r["blocks_eff"] == 0 and not r["clamped"]
    # 20GB UNET → 每块 0.4GB；10 块 = 4GB；32GB 卡的「一半」= 16GB，不触发夹取
    r = perf.blockswap_headroom(10, 20.0, 32.0)
    assert abs(r["extra_gb"] - 4.0) < 1e-9
    assert r["blocks_eff"] == 10 and r["clamped"] is False
    assert "0.40GB" in r["note"]


def test_blockswap_headroom_clamps_to_half_vram():
    """★ 6GB 卡上 `suggest_blocks` 会给 40+ 块 ≈ 16GB > 显存总量 → 必须夹住并如实说明。

    不夹的后果是「预留比卡还大」这种无意义设置；静默夹的后果是用户以为自己填的 41
    生效了。所以既要夹、又要在 note 里说清被夹到几块。
    """
    n = perf.suggest_blocks(20.0, 6.0)
    assert n >= 40, f"6GB 卡上该建议换出 40+ 块，实际 {n}"
    r = perf.blockswap_headroom(n, 20.0, 6.0)
    assert r["clamped"] is True
    assert r["extra_gb"] <= 6.0 * perf.BLOCKSWAP_HEADROOM_CAP + 1e-9
    assert 0 < r["blocks_eff"] < n
    assert "夹" in r["note"] and "⚠" in r["note"]


def test_blockswap_headroom_unknown_sizes_never_guesses():
    """体量没探明（面板打开时还没加载模型）→ 明确「不干预」，**不编数字**。"""
    for n in (5, 41):
        r = perf.blockswap_headroom(n, None, 16.0)
        assert r["extra_gb"] == 0.0 and r["blocks_eff"] == 0
        assert "未探明" in r["note"]
        r2 = perf.blockswap_headroom(n, 20.0, None)
        assert r2["extra_gb"] == 0.0 and "未探明" in r2["note"]


def test_blockswap_headroom_bad_block_count():
    """脏输入不该抛，也不该算出负数预留。"""
    for bad in (None, "x", -3):
        r = perf.blockswap_headroom(bad, 20.0, 32.0)
        assert r["extra_gb"] == 0.0 and r["blocks_eff"] == 0


def test_blockswap_baseline_is_read_once():
    """★ 基线只读一次：第二次读到的可能已被我们自己改过，用它当基线会让
    「关掉开关 = 恢复原样」变成「关掉开关 = 保持我上次设的值」。"""
    saved = dict(perf._BS_BASELINE)
    try:
        perf._BS_BASELINE.clear()
        perf._BS_BASELINE.update({"aimdo_gb": 1.5, "extra_gb": 0.4})
        assert perf._blockswap_baseline() == {"aimdo_gb": 1.5, "extra_gb": 0.4}
        # 就算环境变了，缓存命中就不再重读
        perf._BS_BASELINE["aimdo_gb"] = 9.0
        assert perf._blockswap_baseline()["aimdo_gb"] == 9.0
    finally:
        perf._BS_BASELINE.clear()
        perf._BS_BASELINE.update(saved)


def test_probe_blockswap_shape():
    """探测结果字段齐全；path ∈ {aimdo, legacy, None}。

    None = **没跑在 ComfyUI 进程里**（import 不到 comfy）→ 如实报「探不到」，不硬编一个
    path。本用例在 managed python 下跑（无 comfy / 无 torch），所以这里必然是 None ——
    这条同时钉住 perf.py 全模块的前提「量不到给 None，绝不抛」。
    """
    st = perf.probe_blockswap()
    assert set(st) == {"aimdo", "dynamic", "streams", "non_blocking",
                       "pinned_gb", "headroom_gb", "path"}
    assert st["path"] in (None, "aimdo", "legacy")
    if st["aimdo"] is None:
        assert st["path"] is None, "探不到 aimdo 时不该硬报一个 path"


def test_blockswap_line_states():
    assert "DynamicVRAM 在线" in perf.blockswap_line({"path": "aimdo"}, 1.25)
    assert "1.25GB" in perf.blockswap_line({"path": "aimdo"}, 1.25)
    assert "?" in perf.blockswap_line({"path": "aimdo"}, None)
    assert "无 DynamicVRAM" in perf.blockswap_line({"path": "legacy"}, None)


def test_apply_blockswap_unknown_size_does_not_touch(monkeypatch):
    """★★ **读接口不许改进程状态**。

    面板打开会走 `perf_get → apply_runtime → apply_blockswap`，而那时拿不到模型体量。
    若此时按「extra=0」照写，就会把上一次渲染设好的预留**悄悄抹掉** —— 一个 GET
    请求不该有副作用。所以这里钉死：算不出目标 → 一次 `_set_*` 都不许调。
    """
    calls = []
    monkeypatch.setattr(perf, "_set_aimdo_headroom", lambda gb: calls.append(gb) or 7.0)
    monkeypatch.setattr(perf, "_set_extra_reserved", lambda gb: calls.append(gb) or 0.6)
    monkeypatch.setattr(perf, "probe_blockswap",
                        lambda: {"path": "aimdo", "headroom_gb": 7.0})
    saved = dict(perf._BS_BASELINE)
    try:
        perf._BS_BASELINE.clear()
        perf._BS_BASELINE.update({"aimdo_gb": 7.0, "extra_gb": 0.6})
        out = perf.apply_blockswap({"blocks_swap_on": True, "blocks_to_swap": 25},
                                   {"vram_total_gb": 16.0})
        assert calls == [], f"体量未知却写了预留：{calls}"
        assert out["applied"] is False and "未探明" in out["note"]
    finally:
        perf._BS_BASELINE.clear()
        perf._BS_BASELINE.update(saved)


def test_apply_blockswap_aimdo_applies_and_restores(monkeypatch):
    """开 → 预留抬到 baseline + N×块大小；关 → **恢复 baseline**（不是保持在 N 块）。"""
    seen = []
    monkeypatch.setattr(perf, "_set_aimdo_headroom",
                        lambda gb: (seen.append(gb), round(gb, 3))[1])
    monkeypatch.setattr(perf, "probe_blockswap",
                        lambda: {"path": "aimdo", "headroom_gb": 0.0})
    saved = dict(perf._BS_BASELINE)
    try:
        perf._BS_BASELINE.clear()
        perf._BS_BASELINE.update({"aimdo_gb": 0.5, "extra_gb": 0.6})
        hw = {"unet_gb": 20.0, "vram_total_gb": 32.0}
        out = perf.apply_blockswap({"blocks_swap_on": True, "blocks_to_swap": 10}, hw)
        assert out["applied"] is True and out["blocks"] == 10
        assert abs(seen[-1] - 4.5) < 1e-9, seen          # 0.5 基线 + 4.0
        assert abs(out["headroom_gb"] - 4.5) < 1e-9      # 读回核验后的值
        out2 = perf.apply_blockswap({"blocks_swap_on": False, "blocks_to_swap": 10}, hw)
        assert abs(seen[-1] - 0.5) < 1e-9, seen
        assert "恢复" in out2["note"]
    finally:
        perf._BS_BASELINE.clear()
        perf._BS_BASELINE.update(saved)


def test_apply_blockswap_follows_profile_sentinel(monkeypatch):
    """`blocks_to_swap = -1` = 跟随档位 → 用 suggest_blocks 按**真实体量**反解。"""
    monkeypatch.setattr(perf, "_set_aimdo_headroom", lambda gb: round(gb, 3))
    monkeypatch.setattr(perf, "probe_blockswap",
                        lambda: {"path": "aimdo", "headroom_gb": 0.0})
    saved = dict(perf._BS_BASELINE)
    try:
        perf._BS_BASELINE.clear()
        perf._BS_BASELINE.update({"aimdo_gb": 0.0, "extra_gb": 0.0})
        hw = {"unet_gb": 20.0, "vram_total_gb": 16.0}
        out = perf.apply_blockswap({"blocks_swap_on": True, "blocks_to_swap": -1}, hw)
        assert out["blocks"] == perf.suggest_blocks(20.0, 16.0)
        assert out["blocks"] > 0
    finally:
        perf._BS_BASELINE.clear()
        perf._BS_BASELINE.update(saved)


def test_apply_blockswap_legacy_path(monkeypatch):
    """没有 DynamicVRAM 时退化为 ComfyUI 的 EXTRA_RESERVED_VRAM（同一条语义）。"""
    seen = []
    monkeypatch.setattr(perf, "_set_extra_reserved",
                        lambda gb: (seen.append(gb), round(gb, 3))[1])
    monkeypatch.setattr(perf, "probe_blockswap",
                        lambda: {"path": "legacy", "headroom_gb": None})
    saved = dict(perf._BS_BASELINE)
    try:
        perf._BS_BASELINE.clear()
        perf._BS_BASELINE.update({"aimdo_gb": None, "extra_gb": 0.6})
        out = perf.apply_blockswap({"blocks_swap_on": True, "blocks_to_swap": 5},
                                   {"unet_gb": 20.0, "vram_total_gb": 32.0})
        assert out["path"] == "legacy" and out["applied"] is True
        assert abs(seen[-1] - (0.6 + 2.0)) < 1e-9, seen
        assert "legacy" in out["note"] and "DynamicVRAM" in out["note"]
    finally:
        perf._BS_BASELINE.clear()
        perf._BS_BASELINE.update(saved)


def test_apply_blockswap_keys_are_stable():
    """返回结构是接口（前端诊断区与 nodes 报告都按它读），键名不许漂。"""
    saved = dict(perf._BS_BASELINE)
    try:
        perf._BS_BASELINE.clear()
        out = perf.apply_blockswap({}, None)
        assert set(out) == {"on", "path", "blocks", "extra_gb", "clamped",
                            "headroom_gb", "applied", "note"}
    finally:
        perf._BS_BASELINE.clear()
        perf._BS_BASELINE.update(saved)

