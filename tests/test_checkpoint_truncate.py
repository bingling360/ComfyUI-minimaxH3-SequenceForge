"""checkpoint.truncate 回归：重摇标记只清「被重做区间覆盖」的槽位。

跑法（仓库根目录）：
    python -m pytest tests/test_checkpoint_truncate.py -q

背景（2026-09-16 修）：旧实现无条件 `redo_queue = []`，改靠后的段会误杀靠前的
重摇标记。复现：10 段全完成 → 标段 3 重摇（slot=2）→ 改段 8 提示词使 start=7
→ 段 3 的标记本应保留（该段保留存档、标记仍有效），却被连带清空。

正确语义与 inserts 同口径：
    slot >= start  该段本来就要重做，标记无意义 → 清
    slot <  start  该段保留存档，标记仍然有效 → 留
"""

import importlib.util
import os
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_checkpoint():
    path = os.path.join(ROOT, "checkpoint.py")
    spec = importlib.util.spec_from_file_location("h3_ckpt_truncate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk(root_name):
    return tempfile.mkdtemp(prefix=root_name)


def test_keeps_marks_before_start():
    ck = _load_checkpoint()
    root = _mk("h3ckpt_")
    manifest = {
        "schema": ck.SCHEMA, "done": 10, "seeds": list(range(10)),
        "redo_queue": [[2, "双锚"], [8, "双锚"]],
    }
    out = ck.truncate(root, manifest, 7)
    kept = [int(x[0]) for x in out["redo_queue"]]
    assert 2 in kept, "slot<start 的段保留存档，重摇标记必须留"
    assert 8 not in kept, "slot>=start 的段本来就要重做，标记无意义应清"
    assert out["done"] == 7


def test_drops_all_when_start_zero():
    ck = _load_checkpoint()
    root = _mk("h3ckpt_")
    manifest = {
        "schema": ck.SCHEMA, "done": 5, "seeds": list(range(5)),
        "redo_queue": [[1, "双锚"], [3, "无锚"]],
    }
    out = ck.truncate(root, manifest, 0)
    assert out["redo_queue"] == []
    assert out["done"] == 0


def test_keeps_all_when_start_beyond_marks():
    ck = _load_checkpoint()
    root = _mk("h3ckpt_")
    manifest = {
        "schema": ck.SCHEMA, "done": 10, "seeds": list(range(10)),
        "redo_queue": [[1, "双锚"], [2, "仅锚上段"]],
    }
    out = ck.truncate(root, manifest, 5)
    kept = sorted(int(x[0]) for x in out["redo_queue"])
    assert kept == [1, 2]


def test_filters_malformed_entries():
    """畸形条目（非 int 槽位 / None）应被滤掉而不是抛异常。"""
    ck = _load_checkpoint()
    root = _mk("h3ckpt_")
    manifest = {
        "schema": ck.SCHEMA, "done": 5, "seeds": list(range(5)),
        "redo_queue": [[1, "双锚"], ["x", "双锚"], None, [4, "无锚"]],
    }
    out = ck.truncate(root, manifest, 3)
    kept = [int(x[0]) for x in out["redo_queue"]]
    assert kept == [1]


def test_no_redo_queue_key_is_fine():
    ck = _load_checkpoint()
    root = _mk("h3ckpt_")
    manifest = {"schema": ck.SCHEMA, "done": 4, "seeds": list(range(4))}
    out = ck.truncate(root, manifest, 2)
    assert "redo_queue" not in out
