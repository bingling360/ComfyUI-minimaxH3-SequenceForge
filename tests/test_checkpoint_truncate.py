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


# ---- 2026-10-12：新增的两个槽位对齐列表（base_videos / seg_res）与另存件 ----

def test_truncates_base_videos_and_seg_res():
    """`base_videos`（一采另存）与 `seg_res`（逐段分辨率）也是槽位对齐列表，必须一起截。

    漏掉 `base_videos`：被弃段的一采入口留在段卡上指向已删文件。
    漏掉 `seg_res`：「跨分辨率跳过桥接」的判定会读到被弃段的旧值（槽位错位）。
    """
    ck = _load_checkpoint()
    root = _mk("h3ckpt_")
    manifest = {
        "schema": ck.SCHEMA, "done": 5, "seeds": list(range(5)),
        "base_videos": [f"finals/base_seg_{i:03d}.mp4" for i in range(5)],
        "seg_res": [[1376, 768]] * 5,
    }
    out = ck.truncate(root, manifest, 3)
    assert out["base_videos"] == [f"finals/base_seg_{i:03d}.mp4" for i in range(3)]
    assert out["seg_res"] == [[1376, 768]] * 3


def test_deletes_base_seg_artifacts_of_dropped_slots():
    """截断要清掉被弃段的**一采另存件**。

    旧清理正则 `seg_(\\d{3,})` 用 `re.match` 从头锚定，`base_seg_NNN.mp4` 前缀不匹配
    → 文件与记录都留下来，重跑后残留旧内容（且 base_videos 已截断，成了孤儿文件）。
    """
    ck = _load_checkpoint()
    root = _mk("h3ckpt_")
    for sub in ("", "finals"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    keep = [os.path.join(root, "finals", "base_seg_000.mp4"),
            os.path.join(root, "finals", "base_thumb_000.png"),
            os.path.join(root, "finals", "seg_000.mp4"),
            os.path.join(root, "finals", "thumb_000.png")]
    drop = [os.path.join(root, "finals", "base_seg_003.mp4"),
            os.path.join(root, "finals", "base_thumb_003.png"),
            os.path.join(root, "base_seg_004.mp4")]      # 旧格式裸名双路径也清
    for p in keep + drop:
        open(p, "wb").close()
    ck.truncate(root, {"schema": ck.SCHEMA, "done": 5}, 3)
    for p in keep:
        assert os.path.isfile(p), f"未越界的产物不该删：{p}"
    for p in drop:
        assert not os.path.isfile(p), f"越界的一采另存件必须删：{p}"
