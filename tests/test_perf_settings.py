"""性能设置（perf 全局设置 + 运行时开关 + 素材库索引失效）的守护测试。

针对 2026-09-23 那一轮：素材库卡顿（每张缩略图重建全量索引）与内存上涨
（缩略图全尺寸解码）的修复。这几条挂了，说明优化被改回去了。
"""
import os
import sys
import tempfile
import shutil

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import library as L  # noqa: E402
import perf  # noqa: E402


# ---------------------------------------------------------------- 契约

def test_wired_keys_are_known_fields():
    """WIRED_KEYS 里的每个键都必须是 DEFAULT_PERF / PERF_TYPES 的已知字段。

    WIRED_KEYS 是「改了立刻生效」的清单，前端拿它决定哪些开关可点。
    里面混进一个契约里没有的键 = 前端点亮了一个后端根本不认的开关。
    """
    for k in perf.WIRED_KEYS:
        assert k in perf.DEFAULT_PERF, f"{k} 不在 DEFAULT_PERF"
        assert k in perf.PERF_TYPES, f"{k} 不在 PERF_TYPES"


def test_parse_state_handles_new_fields():
    """新字段（三态 / 枚举 / 数值）必须能正确归一化。

    `upcast_attention` 的默认值是字符串 "auto"，但用户选「强制关」时前端写回
    False —— 类型表若只认 str，这个 False 会被静默丢掉，开关永远改不动。
    """
    st = perf.parse_state({
        "upcast_attention": False,
        "index_mode": "ttl",
        "thumb_on_import": False,
        "thumb_max_mp": 25,
        "vram_shuffle": "soft",
    })
    assert st["upcast_attention"] is False
    assert st["index_mode"] == "ttl"
    assert st["thumb_on_import"] is False
    assert st["thumb_max_mp"] == 25
    assert st["vram_shuffle"] == "soft"
    # "auto" 是三态字段的合法取值，不能被当垃圾丢掉
    assert perf.parse_state({"upcast_attention": "auto"})["upcast_attention"] == "auto"
    # 非法类型应该回落到默认，而不是塞进去
    assert perf.parse_state({"thumb_max_mp": "很大"})["thumb_max_mp"] == perf.DEFAULT_PERF["thumb_max_mp"]


def test_parse_state_rejects_unknown_keys():
    """未知键直接丢弃（防止旧存档里的废弃字段一路带到渲染路径）。"""
    st = perf.parse_state({"not_a_field": 1, "index_mode": "ttl"})
    assert "not_a_field" not in st
    assert st["index_mode"] == "ttl"


def test_apply_runtime_never_raises_without_comfy():
    """没有 ComfyUI 时 apply_runtime 也要能跑完（upcast 那项返回 None 即可）。

    perf.py 的设计前提是「纯函数、零 ComfyUI 依赖」，apply_runtime 里
    upcast 那段依赖 comfy，必须自己兜住，不能把整个设置保存搞崩。
    """
    table = perf.parse_state({"index_mode": "ttl", "thumb_on_import": False})
    out = perf.apply_runtime(table)
    assert isinstance(out, dict)
    assert "index_mode" in out
    assert out["index_mode"] == "ttl"
    assert out["thumb_on_import"] is False
    # 还原，别污染其它用例
    perf.apply_runtime(perf.parse_state({"index_mode": "fingerprint",
                                         "thumb_on_import": True}))


# ---------------------------------------------------------------- 素材库索引

@pytest.fixture()
def lib(tmp_path):
    """造一个合成库：library/（全局）+ h3_projects/demo/（项目）。"""
    root = str(tmp_path)
    glo = os.path.join(root, "library")
    proj = os.path.join(root, "h3_projects", "demo")
    os.makedirs(os.path.join(glo, "images"), exist_ok=True)
    os.makedirs(os.path.join(proj, "assets"), exist_ok=True)
    old_g, old_p = L._library_root, L._project_root
    L._library_root = lambda: glo
    L._project_root = lambda n=None: proj
    L.set_index_mode("fingerprint")
    L.invalidate()
    yield glo, proj
    L._library_root, L._project_root = old_g, old_p
    L.set_index_mode("fingerprint")
    L.invalidate()


def test_index_mode_switch_and_fingerprint_invalidation(lib):
    """指纹模式下**新增文件**必须让索引自动失效（不等 3 秒、不用手工 invalidate）。

    这是「翻一页不再重建索引」的前提：指纹算的是文件数 + 最新 mtime，
    新增一个文件文件数就变了。若这里退回 TTL 语义，用户传完素材要等缓存过期才看得到。
    """
    glo, proj = lib
    assert L.set_index_mode("fingerprint") == "fingerprint"
    before = L.build_index("demo")
    # 新增一个项目素材
    with open(os.path.join(proj, "assets", "new.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    after = L.build_index("demo")
    assert len(after) == len(before) + 1, "新增文件后索引没更新（指纹失效没生效）"
    # ttl 模式也要能切过去
    assert L.set_index_mode("ttl") == "ttl"
    assert L.set_index_mode("fingerprint") == "fingerprint"


def test_find_by_id_o1_and_equivalent_to_full_pipeline(lib):
    """find_by_id 取到的条目，其定位字段必须与全管线一致。

    快路径之所以安全，是因为 apply_* 只改 name/roles/refs/rating/tags，
    而 resolve_item_path 只看 scope/file/linked。这条守的就是这个前提。
    """
    glo, proj = lib
    for i in range(3):
        with open(os.path.join(proj, "assets", f"a{i}.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    items = L.build_index("demo")
    assert items
    for e in items:
        got = L.find_by_id("demo", e["id"])
        assert got is not None
        for k in ("id", "scope", "file", "kind", "linked"):
            assert got.get(k) == e.get(k), f"{k} 不一致：快路径与全管线不等价"
    assert L.find_by_id("demo", "不存在") is None
    assert L.find_by_id("demo", "") is None


def test_thumb_on_import_flag(lib):
    """入库缩略图开关能切，且默认值是开（否则浏览时又要付全解码峰值）。"""
    assert L.set_thumb_on_import(False) is False
    assert L.THUMB_ON_IMPORT is False
    assert L.set_thumb_on_import(True) is True
    assert L.THUMB_ON_IMPORT is True


def test_thumb_max_pixels_constant_reasonable():
    """缩略图源图上限要存在且量级合理（几十 MP 量级，不是 0 也不是无限大）。"""
    assert hasattr(L, "THUMB_MAX_PIXELS")
    assert 5_000_000 <= L.THUMB_MAX_PIXELS <= 200_000_000


# ------------------------------------------------- 2026-09-23 第二批：显存/内存/编码

def test_oom_retry_rescues_once_then_gives_up():
    """OOM 自救：救得回来就救，救不回来必须**原样上抛**且不超过设定次数。

    这三条是自救的全部契约：① 非 OOM 一次都不重试（别把参数错误也吞了）；
    ② OOM 时先 cleanup 再原参重试；③ 仍失败就抛（不无限重试、不降规格）。
    """
    calls = {"n": 0}
    cleaned = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("CUDA out of memory. Tried to allocate 6.13 GiB")
        return "ok"

    out, rescued = perf.oom_retry(flaky, cleanup=lambda: cleaned.__setitem__("n", cleaned["n"] + 1),
                                  tries=2)
    assert out == "ok"
    assert rescued == 1, "自救次数必须是 1（报告要靠它说明「这段是靠自救活下来的」）"
    assert cleaned["n"] == 1, "重试前必须先腾挪"

    # 非 OOM：一次都不重试
    calls["n"] = 0

    def bad():
        calls["n"] += 1
        raise ValueError("参数不合法")

    with pytest.raises(ValueError):
        perf.oom_retry(bad, cleanup=lambda: cleaned.__setitem__("n", cleaned["n"] + 1), tries=3)
    assert calls["n"] == 1

    # 一直 OOM：只试 tries 次，最后抛出原始 OOM
    def always_oom():
        raise RuntimeError("CUDA out of memory")

    with pytest.raises(RuntimeError):
        perf.oom_retry(always_oom, cleanup=None, tries=2)


def test_is_oom_error_covers_common_shapes():
    """判断 OOM 只认类型名与消息（**不 import torch**，保住本模块零依赖）。"""
    assert perf.is_oom_error(RuntimeError("CUDA out of memory. Tried to allocate"))
    assert perf.is_oom_error(RuntimeError("CUBLAS_STATUS_ALLOC_FAILED"))
    assert not perf.is_oom_error(RuntimeError("shape mismatch"))
    assert not perf.is_oom_error(None)
    # torch.OutOfMemoryError 走类型名分支（这里造一个同名类，避免依赖 torch）
    OOM = type("OutOfMemoryError", (RuntimeError,), {})
    assert perf.is_oom_error(OOM("x"))


def test_plan_ff_chunks_boundaries():
    """FFN 分块的边界口径：0/负数=关（单块）、切得尽、切不尽都要对。"""
    assert perf.plan_ff_chunks(0, 4) == []
    assert perf.plan_ff_chunks(-3, 4) == []
    assert perf.plan_ff_chunks(10, 0) == [(0, 10)], "0 = 关，必须等价于不分块"
    assert perf.plan_ff_chunks(10, 4) == [(0, 4), (4, 8), (8, 10)]
    assert perf.plan_ff_chunks(8, 4) == [(0, 4), (4, 8)]
    assert perf.plan_ff_chunks(3, 100) == [(0, 3)]
    assert perf.plan_ff_chunks("x", 4) == [(0, 0)]


def test_guard_allows_unload_blocks_on_critical():
    """落盘守卫接行为：只有 critical + block 才**禁止**全卸。

    这条守的是 D4：Linux 无 swap 的机器上，一次 unload 就是 OOM killer
    SIGKILL——连 except 都跑不到，所以必须在动手前拦住。
    """
    hw = {"ram_total_gb": 60.0, "ram_avail_gb": 31.0, "swap_total_gb": 0.0,
          "swap_free_gb": 0.0, "disk_free_gb": 60.0,
          "unet_gb": 67.5, "te_gb": 0.0}
    allowed, g = perf.guard_allows_unload(hw, "warn")
    assert g["level"] == "critical" and allowed is True, "warn 只报不改行为"
    allowed, g = perf.guard_allows_unload(hw, "block")
    assert g["level"] == "critical" and allowed is False, "critical + block 必须禁止全卸"
    # 富余的机器：block 也不拦
    rich = dict(hw, ram_avail_gb=500.0, swap_total_gb=64.0, swap_free_gb=64.0)
    assert perf.guard_allows_unload(rich, "block")[0] is True


def test_resolve_encoder_does_not_mix_knobs():
    """NVENC 认 cq、x264 认 crf —— 传错就是静默丢质量档，所以必须显式分流。"""
    assert perf.resolve_encoder("auto", cq=18, crf=20) == ("libx264", {"crf": "20"})
    assert perf.resolve_encoder("", cq=18) == ("libx264", {"crf": "20"})
    codec, opts = perf.resolve_encoder("h264_nvenc", cq=18, crf=20)
    assert codec == "h264_nvenc" and opts == {"cq": "18"}
    assert "crf" not in opts
    codec, opts = perf.resolve_encoder("hevc_nvenc", cq=21)
    assert codec == "hevc_nvenc" and opts == {"cq": "21"}
    # 不认识的回落 libx264，不能把未知 codec 塞给 PyAV
    assert perf.resolve_encoder("libvpx", crf=20) == ("libx264", {"crf": "20"})


def test_probe_lora_footprint_counts_patch_tensors():
    """LoRA 权重账：从 patches 里累加张量字节（结构随来源变，必须递归展开）。"""
    class FakeT:
        def __init__(self, n, es):
            self._n, self._es = n, es

        def numel(self):
            return self._n

        def element_size(self):
            return self._es

    class FakePatcher:
        patches = {
            "a": [(1.0, {"w": FakeT(1024, 2)}, None)],
            "b": [(0.5, {"w": FakeT(2048, 2)})],
        }

    got = perf.probe_lora_footprint(FakePatcher())
    assert got["patch_groups"] == 2
    assert got["patch_count"] == 2
    assert got["lora_weight_gb"] == round(6144 / (1024 ** 3), 2)

    # 没挂 LoRA：给全 0（调用方要的是「没挂」这个结论，不是「量不到」）
    assert perf.probe_lora_footprint(object()) == {
        "patch_groups": 0, "patch_count": 0, "lora_weight_gb": 0.0}
    assert perf.probe_lora_footprint(None)["patch_count"] == 0


def test_lora_account_line_names_both_halves():
    """那一行必须同时有「权重」和「实测激活」——只报权重就是 D2 的错误复现。"""
    line = perf.lora_account_line(12.4, 50, 6.13)
    assert "权重 +12.4GB" in line and "50 patches" in line
    assert "激活峰值 6.1GB" in line and "建议留空" in line
    no_act = perf.lora_account_line(12.4, 50, None)
    assert "未实测" in no_act
    assert perf.suggest_act_reserve(6.13) == 7.4
    assert perf.suggest_act_reserve(None) is None
    assert perf.suggest_act_reserve(0) is None


def test_media_encoder_switch_keeps_x264_default():
    """编码器开关：默认与不认识的值都必须落回 libx264（现状兼容）。"""
    import media
    assert media.set_encoder("auto") == "libx264"
    codec, opts = media._video_stream_options(20, "veryfast", 4, 3)
    assert codec == "libx264" and opts["crf"] == "20" and opts["aq-mode"] == "3"
    assert media.set_encoder("h264_nvenc", 18) == "h264_nvenc"
    codec, opts = media._video_stream_options(20, "veryfast", 4, 3)
    assert codec == "h264_nvenc"
    assert opts == {"cq": "18", "preset": "p4"}, "NVENC 不能收 crf / x264 的 preset / aq-mode"
    assert media.set_encoder("libvpx") == "libx264", "未知编码器必须回落，别塞给 PyAV"
    media.set_encoder("auto")


def test_new_wired_keys_are_all_declared():
    """本轮新接线的键必须同时在 DEFAULT_PERF / PERF_TYPES / WIRED_KEYS 里。"""
    for k in ("oom_autoretry", "act_peak_probe", "ff_chunk_tokens", "attn_backend",
              "upscale_temporal_chunk", "keep_upscaler_resident", "final_mode",
              "frames_dtype", "guard_action", "encoder", "nvenc_cq"):
        assert k in perf.DEFAULT_PERF, k
        assert k in perf.PERF_TYPES, k
        assert k in perf.WIRED_KEYS, k


def test_apply_runtime_echoes_late_keys():
    """渲染期消费的键：apply_runtime 要把存下来的值回声出去（面板靠它显示状态）。"""
    t = perf.parse_state({"frames_dtype": "uint8", "final_mode": "stream",
                          "oom_autoretry": False, "encoder": "auto"})
    out = perf.apply_runtime(t)
    assert out["frames_dtype"] == "uint8"
    assert out["final_mode"] == "stream"
    assert out["oom_autoretry"] is False
    assert "encoder" in out
    # 还原
    perf.apply_runtime(perf.parse_state({"frames_dtype": "float32",
                                         "final_mode": "auto", "oom_autoretry": True}))


# ------------------------------------- 提交前审查修正（2026-09-23 同批）

def test_keep_upscaler_resident_default_matches_old_behavior():
    """默认必须是 True。

    口径是 `resident = !force_unload`，旧默认 `force_unload=false` 就等价于
    `resident=True`（段间保留缓存、零加载）。改成 False 等于把默认行为从
    「段间零加载」悄悄换成「每段重载 ~1s」—— 那是改默认行为，不是迁移。
    """
    assert perf.DEFAULT_PERF["keep_upscaler_resident"] is True


def _get_call_keys(src):
    """源码里**真实代码**调用的 `x.get("键")` 键集合（AST 解析，注释不算）。

    为什么不用字符串匹配：修 bug 的注释里往往要把旧条件原样写一遍（「此前是
    `cfg.get("force_unload") and ...`」），字符串匹配会把注释一起算进去 →
    对着正确代码报红。
    """
    import ast
    out = set()
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and n.args):
            a0 = n.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                out.add(a0.value)
    return out


def test_unload_gate_reads_only_resident_key():
    """回归守卫：强制卸载的**正面条件**只能是 keep_upscaler_resident。

    2026-09-23 提交前审查抓到的真 bug：`force_unload` 已从**所有写入端**退场
    （前端控件删了、`upscale.parse_state` 不再输出、nodes.py 也不再写），但
    `upscale.render_segment` 收尾处仍写着
        `if net is not None and cfg.get("force_unload") and not cfg.get("keep_...")`
    → 正面条件恒 None → 分支**永假** → `upscale_net.force_unload()` 成了不可达
    代码，而新开关在 WIRED_KEYS / 面板字段表里标着「已接线」。这条把「不许再读
    旧键」钉死，免得下一次重构又写回来。
    """
    with open(os.path.join(ROOT, "upscale.py"), encoding="utf-8") as f:
        keys = _get_call_keys(f.read())
    assert "force_unload" not in keys, \
        "force_unload 已无写入端（键已退场），读它只会得到恒假条件"
    assert "keep_upscaler_resident" in keys, \
        "强制卸载的正面条件必须是 keep_upscaler_resident（当前无人消费它）"
