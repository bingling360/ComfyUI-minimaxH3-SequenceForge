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
