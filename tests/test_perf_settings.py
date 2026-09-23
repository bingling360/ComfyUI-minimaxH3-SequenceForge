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

def test_runtime_later_keys_are_known_fields():
    """RUNTIME_LATER_KEYS（渲染期消费、面板只回声）里的键必须是已知字段。

    混进一个契约里没有的键 = 面板显示一个后端根本不认的状态。
    （原先这条查的是 `WIRED_KEYS` —— 那份「已接线名单」已随三态机制连根删除。）
    """
    assert perf.RUNTIME_LATER_KEYS, "渲染期键名单是空的？至少分块与成片那几个该在"
    for k in perf.RUNTIME_LATER_KEYS:
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


def test_new_keys_are_declared():
    """新增的键必须同时在 DEFAULT_PERF / PERF_TYPES 里声明。

    （曾经还要求同步一份「已接线名单」`WIRED_KEYS` —— 那份名单已随三态机制删除。
    「这个键有没有人读」改由 `test_no_key_in_default_perf_is_unread` 的 AST 判据守，
    它比人工名单可靠：名单会过期，读取方不会。）
    """
    for k in ("oom_autoretry", "act_peak_probe", "ff_chunk_tokens", "attn_backend",
              "upscale_temporal_chunk", "keep_upscaler_resident", "final_mode",
              "frames_dtype", "guard_action", "encoder", "nvenc_cq",
              "encode_hq", "offload_guard_ratio"):
        assert k in perf.DEFAULT_PERF, k
        assert k in perf.PERF_TYPES, k


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
    代码，而新开关在面板字段表里标着「已接线」。这条把「不许再读
    旧键」钉死，免得下一次重构又写回来。
    """
    with open(os.path.join(ROOT, "upscale.py"), encoding="utf-8") as f:
        keys = _get_call_keys(f.read())
    assert "force_unload" not in keys, \
        "force_unload 已无写入端（键已退场），读它只会得到恒假条件"
    assert "keep_upscaler_resident" in keys, \
        "强制卸载的正面条件必须是 keep_upscaler_resident（当前无人消费它）"


# ------------------------------------- 遗留修补（2026-09-23 第二轮审查）

def test_no_key_in_default_perf_is_unread():
    """★ 根治类守卫：DEFAULT_PERF 里的每个键都必须有**真实读取方**。

    本轮审查抓到的本类问题：表里有 10 个键全仓零读取 ——
    `unload_unet_seg` / `unload_before_decode` / `frames_to_cpu` / `max_upscale_scale` /
    `guard_offload_target` / `preset` / `probe` / `unload_upscaler_cache`
    （前端还摆着控件，勾了没用）+ `cond_cache_size` / `offload_guard_ratio`
    （消费端早就收这个入参，调用方却一直没传）。
    它们让「没有未接线字段」这句话（当时由 `UNWIRED_KEYS = ()` 声称）变成假话，也让读代码的人
    以为这些功能存在。静态挡在这里：**新加键必须同时给出读取方**，否则测试红。

    ⚠ 需要保持这份模块清单覆盖所有真实消费点；漏掉一个文件会误报「没人读」。
    """
    import ast
    import glob

    read = set()
    for p in glob.glob(os.path.join(ROOT, "*.py")):
        with open(p, encoding="utf-8") as f:
            src = f.read()
        read |= _get_call_keys(src)
        # 下标式读取 `cfg["k"]` 也算消费端（apply_blockswap 那类写法）
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) \
                    and isinstance(n.slice.value, str):
                read.add(n.slice.value)
    missing = sorted(k for k in perf.DEFAULT_PERF if k not in read)
    assert not missing, (
        f"这些键在 DEFAULT_PERF 里、但全仓没有任何读取方（改了不会有任何效果）：{missing}\n"
        "要么给它加消费端（有人读它），要么删掉 —— 别留只有名字的键。")


@pytest.mark.parametrize("dead", [
    "preset", "probe",
    "unload_unet_seg", "unload_before_decode", "frames_to_cpu",
    "max_upscale_scale", "guard_offload_target", "unload_upscaler_cache",
    "encode_profile",
    # `cond_cache_size` 是**另一种**死法：有人读、值也传下去了，但那个旋钮对真实
    # 调用路径毫无作用（官方节点固定给 tokenize 传 list → 缓存全部旁路、命中恒 0，
    # 容量 1/8/32 结果逐字相同）—— 实测见 cond_cache.py 顶部说明。
    # 判据升级：**有人读 ≠ 旋钮有效**。
    "cond_cache_size",
])
def test_removed_dead_keys_stay_removed(dead):
    """被本轮删掉的死键不许回来。

    各自理由（详见 perf.py 对应位置的注释）：
      · `preset` / `probe` —— 从未落地，全仓零消费
      · `unload_unet_seg` / `unload_before_decode` / `frames_to_cpu` /
        `max_upscale_scale` —— 从未实现；同类诉求已有真实现
        （`vram_shuffle` / `final_mode` / `frames_dtype`）
      · `guard_offload_target` —— 与 `guard_action` 语义重叠且零消费
      · `unload_upscaler_cache` —— 与 `keep_upscaler_resident` 同一件事的两面，
        而只有后者有消费端（前端曾摆着这个勾了没用的三方开关）
      · `encode_profile` —— 三档已压成开关 `encode_hq`（crf 改由 `x264_crf` 独立给）
    """
    assert dead not in perf.DEFAULT_PERF
    assert dead not in perf.PERF_TYPES
    for tbl in perf.PROFILE_TABLE.values():
        assert dead not in tbl, f"{dead} 又回到档位表里了"


def test_encode_hq_is_a_two_state_switch():
    """编码画质档 = 开关（原「标准/高清/极致」三档压成一档）。

    用户拍板（2026-09-23）：crf 与 preset/抖动**分开** —— 档位只留「开不开抗糊/
    抗条纹」，crf 由 `x264_crf` 单独调。所以真值表必须**恰好两态**。
    """
    import ast
    assert perf.DEFAULT_PERF["encode_hq"] is False
    assert perf.PERF_TYPES["encode_hq"] == (bool,)
    with open(os.path.join(ROOT, "upscale.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    settings = None
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_ENCODE_SETTINGS" for t in n.targets):
            settings = n.value
    assert isinstance(settings, ast.Dict), "找不到 upscale._ENCODE_SETTINGS 字面量表"
    keys = [k.value for k in settings.keys]
    assert sorted(keys, key=str) == [False, True], (
        f"编码档必须恰好两态（关 / 开），实际 {keys} —— 三档已经被砍掉了，别加回来")


def test_x264_encoder_uses_caller_crf():
    """media 的 x264 分支必须**用调用方给的 crf**。

    以前这里只认进程级 `ENCODER_CRF`、把入参整个丢掉（`resolve_encode_quad`
    按 `perf.x264_crf` 算好的值再对也不生效）—— 典型的「有真值却不用」静默失效。
    """
    import media
    old_enc, old_crf = media.ENCODER, media.ENCODER_CRF
    try:
        media.ENCODER, media.ENCODER_CRF = "libx264", 20
        _codec, opts = media._video_stream_options(16, "medium", 4, 3)
        assert opts["crf"] == "16", "入参 crf 被忽略（又回到只认 ENCODER_CRF）"
        assert opts["preset"] == "medium" and opts["aq-mode"] == "3"
        # 入参缺失 / 非法 → 退回进程级兜底，不许抛异常
        for bad in (None, "auto", "", "x"):
            _c, o2 = media._video_stream_options(bad, "veryfast", 4, None)
            assert o2["crf"] == "20", f"入参 {bad!r} 时没退回进程级兜底"
        # NVENC 仍走 cq、不看 crf（两个旋钮不串味）
        media.ENCODER = "h264_nvenc"
        _c, o3 = media._video_stream_options(16, "medium", 4, 3)
        assert "crf" not in o3 and o3["cq"] == "20"
    finally:
        media.ENCODER, media.ENCODER_CRF = old_enc, old_crf


def test_guard_ratio_is_passed_and_cond_knob_stays_out():
    """`offload_guard_ratio` 必须真的传下去；`cond_cache_size` 不许接回来。

    前者：`perf.offload_guard(hw, ratio)` / `perf.guard_allows_unload(hw, action, ratio)`
    的签名本来就收 ratio，nodes.py 以前没传 → 系数恒等于常量 1.2，键改了不生效。

    后者：`cond_cache_size` 走的是另一种死法 —— 有人读、值也传到了
    `CachedClipProxy(capacity=…)`，但**那个旋钮对真实路径毫无作用**（官方 H3 节点
    固定给 tokenize 传 list → cond 缓存全部旁路、命中恒 0，容量 1/8/32 结果逐字相同，
    实测见 cond_cache.py 顶部）。所以它被删了，且**不许再传回来**。
    教训：判据不能停在「有人读」。
    """
    with open(os.path.join(ROOT, "nodes.py"), encoding="utf-8") as f:
        src = f.read()
    assert "offload_guard(_hw, _gratio)" in src, "落盘守卫没把 offload_guard_ratio 传进去"
    assert 'guard_allows_unload(_hw, "block", _gratio)' in src, \
        "guard_allows_unload 没把 offload_guard_ratio 传进去"
    # ⚠ 用 AST 判「有没有读这个键」，不能用裸字符串 —— 源码注释里写着
    # 「cond_cache_size 已删除」，字符串匹配会把注释算进来对着正确代码报红
    # （这正是本仓库 `_get_call_keys` 存在的原因）。
    keys = _get_call_keys(src)
    assert "cond_cache_size" not in keys, \
        "cond_cache_size 已删（实测该旋钮对真实路径无效），别再把它接回来"
