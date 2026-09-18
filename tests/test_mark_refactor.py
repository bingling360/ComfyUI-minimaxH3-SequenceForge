"""B2 素材标注（mark）回归 —— 无 ComfyUI 可跑。

跑法（仓库根目录）：
    python -m pytest tests/test_mark_refactor.py -q

标注是**给 AI 看的短名**（图片1 / 视频1 / 音频1）：

- 发给 LLM 之前：正文里的 `@女主.png` → `@图片1`（encode_marks）
- LLM 返回之后：`@图片1` → `@女主.png`（decode_marks）
- 真正送模型：`@女主.png` → `<Picture 1>`（既有 compile_refs 链路，不变）

为什么不让 LLM 直接写真名：长文件名（`微信图片_20260730155838_638.png`）模型
很容易抄错，抄错了就是一个静默的悬空引用（出片才发现没挂上图）。

两条硬约束：
1. **编号不回收** —— 删掉 2 号后新增仍是 4 号（若已有 1/2/3）。回收会让旧提示词
   里的 `@图片2` 悄悄指向另一张素材。
2. **前后端同口径** —— 前端 replaceAtTokens / assignMarks 与后端一致（历史上
   cleanLabel / clean_alias 两份实现漂过一次）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")


def _load_top(name, path):
    src = open(path, encoding="utf-8").read()
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    sys.modules[name] = mod
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


@pytest.fixture(scope="module")
def AS():
    return _load_top("asset_store", os.path.join(ROOT, "asset_store.py"))


# ---- 编号 ----

def test_mark_of_by_kind(AS):
    assert AS.mark_of("image", 1) == "图片1"
    assert AS.mark_of("video", 3) == "视频3"
    assert AS.mark_of("audio", 2) == "音频2"


def test_next_mark_seq_does_not_recycle(AS):
    """删掉中间的号，新号仍从"当前最大 +1"开始（不回收）。"""
    assert AS.next_mark_seq("image", ["图片1", "图片2", "图片3"]) == 4
    # 2 号被删了也不会补回来 —— 补回来就会让旧提示词改指向
    assert AS.next_mark_seq("image", ["图片1", "图片3"]) == 4
    assert AS.next_mark_seq("image", []) == 1
    assert AS.next_mark_seq("image", None) == 1
    # 别的类型的号不干扰
    assert AS.next_mark_seq("image", ["图片1", "视频7", "音频2"]) == 2


# ---- assign_marks ----

def test_assign_marks_per_kind_and_order(AS):
    items = [{"kind": "image", "mark": ""}, {"kind": "video", "mark": ""},
             {"kind": "image", "mark": ""}, {"kind": "audio", "mark": ""}]
    got = AS.assign_marks(items)
    assert [x["mark"] for x in got] == ["图片1", "视频1", "图片2", "音频1"]


def test_assign_marks_keeps_existing(AS):
    """已经落盘的标注**一个都不动** —— 动一次就等于给旧提示词换指向。"""
    items = [{"kind": "image", "mark": "图片5"}, {"kind": "image", "mark": ""},
             {"kind": "image", "mark": "图片2"}]
    got = AS.assign_marks(items)
    assert [x["mark"] for x in got] == ["图片5", "图片6", "图片2"]


# ---- 编解码 ----

def test_encode_marks_for_llm(AS):
    m2n = {"图片1": "女主.png", "图片2": "街道.jpg"}
    n2m = {v: k for k, v in m2n.items()}
    src = "开头 @女主.png 站着，远处是 @街道.jpg 的霓虹"
    got = AS.encode_marks(src, n2m)
    assert got == "开头 @图片1 站着，远处是 @图片2 的霓虹", got


def test_decode_marks_back_to_full_name(AS):
    m2n = {"图片1": "女主.png", "图片2": "街道.jpg"}
    got = AS.decode_marks("画面里 @图片1 走向 @图片2", m2n)
    assert got == "画面里 @女主.png 走向 @街道.jpg", got


def test_marks_roundtrip(AS):
    m2n = {"图片1": "女主.png", "图片10": "远景.png"}
    n2m = {v: k for k, v in m2n.items()}
    src = "@女主.png 与 @远景.png 同框"
    assert AS.decode_marks(AS.encode_marks(src, n2m), m2n) == src


def test_marks_prefix_safe(AS):
    """`@图片1` 不能吃掉 `@图片10` 的前缀（最长优先）。"""
    m2n = {"图片1": "a.png", "图片10": "b.png"}
    got = AS.decode_marks("@图片10 先，@图片1 后", m2n)
    assert got == "@b.png 先，@a.png 后", got


def test_marks_unknown_left_alone(AS):
    """模型造了个不存在的 `@图片99` → 原样保留（前端会渲染成红框，不静默丢）。"""
    got = AS.decode_marks("出现了 @图片99", {"图片1": "a.png"})
    assert got == "出现了 @图片99", got


# ---- 前后端同口径 ----

def _frontend_fn(name):
    src = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    m = re.search(rf"^function {re.escape(name)}\(.*?^\}}", src, re.M | re.S)
    assert m, f"未找到前端函数 {name}"
    return m.group(0)


@pytest.mark.skipif(NODE is None, reason="需要 node 执行前端纯函数")
def test_frontend_replace_at_tokens_matches_backend(AS):
    fn = _frontend_fn("replaceAtTokens")
    cases = [
        ("@女主.png 与 @街道.jpg", {"女主.png": "图片1", "街道.jpg": "图片2"},
         "@图片1 与 @图片2"),
        ("@图片10 先，@图片1 后", {"图片1": "a.png", "图片10": "b.png"},
         "@b.png 先，@a.png 后"),
        ("邮箱 a@女主.com 不误伤", {"女主": "图片1"}, "邮箱 a@女主.com 不误伤"),
        ("没有引用", {"女主.png": "图片1"}, "没有引用"),
    ]
    code = (
        fn + "\n"
        + "const cases = " + json.dumps([[a, b] for a, b, _ in cases], ensure_ascii=False) + ";\n"
        + "console.log(JSON.stringify(cases.map(([t, m]) => replaceAtTokens(t, m))));"
    )
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    for (text, mapping, want), g in zip(cases, got):
        assert AS._replace_at_tokens(text, mapping) == want      # 后端同口径
        assert g == want, f"前端结果不符：{text} -> {g}"


@pytest.mark.skipif(NODE is None, reason="需要 node 执行前端纯函数")
def test_frontend_assign_marks_matches_backend(AS):
    """前端兜底补号与后端规则一致：按类型独立、已有不动、不回收。"""
    fn = _frontend_fn("assignMarks")
    items = [{"kind": "image", "mark": "图片5"}, {"kind": "image", "mark": ""},
             {"kind": "video", "mark": ""}, {"kind": "image", "mark": "图片2"},
             {"kind": "audio", "mark": ""}]
    code = (
        "const KIND_NAME = { image: '图片', video: '视频', audio: '音频' };\n"
        "const markOf = (a) => String((a && a.mark) || '').trim();\n" + fn + "\n"
        + "console.log(JSON.stringify(assignMarks("
        + json.dumps(items, ensure_ascii=False) + ").map((x) => x.mark)));"
    )
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    want = [x["mark"] for x in AS.assign_marks(items)]
    assert got == want, f"前后端补号不一致：\n got={got}\nwant={want}"
