"""checkpoint.assert_match 回归：只有分辨率是硬约束，其余参数变更不拦不重做。

跑法（仓库根目录）：
    python -m pytest tests/test_checkpoint_assert.py -q

背景（2026-09-16 步骤 5.1）：旧实现严格比对**全部**参数键，并在 docstring 里写
「参数不同 = 新旧段画风不一致的链混搭」。该理由已被证伪——已完成段是盘上的张量，
与采样参数没有任何耦合，改 steps/cfg/采样器只影响此后新采样的段。
唯一真·硬约束是分辨率（C/H/W 不同则 PackedLayout 拼不上，链根本走不通）。
"""

import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_checkpoint():
    spec = importlib.util.spec_from_file_location(
        "h3_ckpt_assert", os.path.join(ROOT, "checkpoint.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _params(**kw):
    p = {"width": 1280, "height": 720, "length": 120, "ctx": 22,
         "steps": 20, "cfg": 6.0, "sampler": "euler", "scheduler": "simple",
         "chain": "i2v", "fade_ratio": 0.0, "gate": {"mode": "关", "threshold": 0.0}}
    p.update(kw)
    return p


def test_identical_params_return_no_notes():
    ck = _load_checkpoint()
    assert ck.assert_match(_params(), _params()) == []


@pytest.mark.parametrize("key,value", [
    ("steps", 30), ("cfg", 4.5), ("sampler", "dpmpp_2m"), ("scheduler", "karras"),
    ("chain", "r2v"), ("fade_ratio", 0.3), ("length", 240), ("ctx", 39),
    ("gate", {"mode": "开", "threshold": 0.5}),
])
def test_non_resolution_change_reports_instead_of_raising(key, value):
    """改采样参数/链结构/门控 → 只回报一句，不报错、不触发重做。"""
    ck = _load_checkpoint()
    notes = ck.assert_match(_params(), _params(**{key: value}))
    assert len(notes) == 1
    assert notes[0].startswith(f"{key}:")


def test_experiments_change_is_not_fatal():
    """实验开关组合变了也不该炸——旧实现专门把它升级为「整链重做」。"""
    ck = _load_checkpoint()
    notes = ck.assert_match(_params(experiments=""), _params(experiments="e1:1"))
    assert notes == ["experiments: 存档='' 当前='e1:1'"]


def test_key_absent_from_old_is_not_a_diff():
    """旧存档没有的新键按「沿用当前值」处理——否则每次加参数都会误报变更。"""
    ck = _load_checkpoint()
    assert ck.assert_match(_params(), _params(experiments="e1:1")) == []


@pytest.mark.parametrize("key,bad", [("width", 1920), ("height", 1080)])
def test_resolution_mismatch_is_fatal(key, bad):
    ck = _load_checkpoint()
    with pytest.raises(ValueError, match="唯一硬约束"):
        ck.assert_match(_params(), _params(**{key: bad}))


def test_resolution_mismatch_reports_both_values():
    ck = _load_checkpoint()
    with pytest.raises(ValueError, match=r"width: 存档=1280 当前=1920"):
        ck.assert_match(_params(), _params(width=1920))


def test_missing_keys_in_new_are_ignored():
    """当前 params 少一个键：沿用存档值，不算不一致。"""
    ck = _load_checkpoint()
    new = _params()
    del new["fade_ratio"]
    assert ck.assert_match(_params(), new) == []


def test_legacy_manifest_without_new_keys_is_fine():
    """旧存档多出的历史键不在当前 params 里 → 不看（只看 new 的键）。"""
    ck = _load_checkpoint()
    old = _params(smart_cut_max=4, drop_budget=2)
    assert ck.assert_match(old, _params()) == []


def test_reroll_start_is_gone():
    """单一 start 语义（隐含级联）已删除，区间计算归 anchors.change_intervals。"""
    ck = _load_checkpoint()
    assert not hasattr(ck, "reroll_start")


def test_truncate_does_not_touch_keys_that_are_not_manifest_lists():
    """manifest 里没有 anchors 列表（anchor 是请求侧字段）——截断不该动它。"""
    import tempfile
    ck = _load_checkpoint()
    root = tempfile.mkdtemp(prefix="h3ckpt_")
    manifest = {"schema": ck.SCHEMA, "done": 5, "seeds": [0, 1, 2, 3, 4],
                "anchors": ["a1", "a2", "a3", "a4", "a5"]}
    out = ck.truncate(root, manifest, 2)
    assert out["seeds"] == [0, 1]
    assert out["anchors"] == ["a1", "a2", "a3", "a4", "a5"]
