"""B1 引用名换轨回归（@全名含后缀）—— 无 ComfyUI 可跑。

跑法（仓库根目录）：
    python -m pytest tests/test_ref_name_refactor.py -q

这次重构修的是两个用户可见的症状：

1. **后缀溢出**：别名是 stem（clean_alias 剥扩展名），写 `@猫.png` 时池内最长前缀
   只匹配到 `猫`，剩下的 `.png` 当普通文本留在正文里 —— 绿框后面挂个裸后缀。
   引用名改为**含后缀的全名**后，`@猫.png` 整体命中，后缀不再溢出。
2. **显示不全 / 引用错人**：别名被截到 24 字符，两个长文件名前 24 字符相同时会
   互相顶掉。引用名不砍尾（只在极端长名时保尾截断）。

另外锁死一条硬约束：**前端 cleanRefName 与后端 clean_ref_name 必须同规则**
（历史上 cleanLabel / clean_alias 两份实现漂过，导致"前端认得、后端编译不出来"）。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
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


# ---- clean_ref_name ----

def test_ref_name_keeps_extension(AS):
    """引用名**保留格式后缀** —— 与 clean_alias（剥后缀）唯一的区别。"""
    assert AS.clean_ref_name("女主.png") == "女主.png"
    assert AS.clean_ref_name("猫.jpg") == "猫.jpg"
    assert AS.clean_ref_name("雨声.wav") == "雨声.wav"
    assert AS.clean_alias("女主.png") == "女主"          # 别名仍是 stem（不冲突）
    assert AS.ref_name_of("女主.png") == "女主.png"


def test_ref_name_normalizes_spaces_and_brackets(AS):
    """空格/括号压成 `_`：@引用 的解析按空白断句，留着就是死引用。"""
    assert AS.clean_ref_name("my photo (1).png") == "my_photo_1_.png"
    assert AS.clean_ref_name("  猫 咪 .png  ") == "猫_咪_.png"
    # 路径只取 basename
    assert AS.clean_ref_name("images/sub/女主.png") == "女主.png"


def test_ref_name_truncation_keeps_tail(AS):
    """超长名**保尾不保头**：尾巴是两条同类素材的唯一区分点。"""
    long = "超长素材名" * 30 + "_14_02_12.png"      # 远超 96
    got = AS.clean_ref_name(long)
    assert len(got) <= AS.REF_NAME_MAX, len(got)
    # 尾部 12 字符（区分点，含扩展名）必须完整留下
    assert got.endswith(long[-12:]), f"尾巴被砍掉了：{got}"
    assert got.endswith(".png")


def test_ref_name_fallback_chain(AS):
    """ref_name_of 按候选顺序取第一个非空：显式 > 原名 > 落盘名 > 别名。"""
    assert AS.ref_name_of("", None, "images/abc123456789_猫.png") == "abc123456789_猫.png"
    assert AS.ref_name_of("手动标注.png", "猫.png") == "手动标注.png"
    assert AS.ref_name_of() == ""


# ---- 注册表：by_ref_name ----

def _reg(AS, links=None, legacy=None, glob=None):
    return AS.build_registry(glob or [], links or [], legacy or [])


def test_registry_indexes_ref_name(AS):
    links = [{"asset_id": "a_0123456789ab", "alias": "女主", "kind": "image",
              "ref_name": "女主.png"}]
    r = _reg(AS, links=links)
    assert r["by_ref_name"]["女主.png"]["asset_id"] == "a_0123456789ab"
    # 别名（stem）也照旧可查 —— 旧档正文里的 @女主 不能突然失效
    assert r["by_alias"]["女主"]["asset_id"] == "a_0123456789ab"


def test_registry_ref_name_falls_back_to_orig_name(AS):
    """链接没显式 ref_name 时，由原始文件名（orig_name）推导。"""
    links = [{"asset_id": "a_0123456789ab", "alias": "女主", "kind": "image",
              "orig_name": "女主全身照.png"}]
    r = _reg(AS, links=links)
    assert r["by_ref_name"]["女主全身照.png"]["asset_id"] == "a_0123456789ab"


def test_registry_legacy_assets_get_ref_name(AS):
    """旧 manifest["assets"]（无 ref_name）迁移后也有引用名（= 落盘文件名）。"""
    legacy = [{"label": "阿依", "kind": "image", "file": "assets/阿依.png"}]
    r = _reg(AS, legacy=legacy)
    assert "阿依.png" in r["by_ref_name"]


# ---- compile_refs：全名解析 + 官方编号 ----

def test_compile_refs_accepts_full_name(AS):
    """正文里写 `@女主.png`（全名）能编译出 <Picture 1> —— 换轨的核心。"""
    links = [{"asset_id": "a_0123456789ab", "alias": "女主", "kind": "image",
              "ref_name": "女主.png"},
             {"asset_id": "a_0123456789cd", "alias": "街道", "kind": "image",
              "ref_name": "街道.jpg"}]
    reg = _reg(AS, links=links)
    res = AS.compile_refs(reg, ["女主.png", "街道.jpg"], 1)
    assert res["ok"], res["errors"]
    assert [b["token"] for b in res["blocks"]] == ["<Picture 1>", "<Picture 2>"]
    # 正文替换表必须同时覆盖全名与别名（旧写法也能替换掉）
    assert res["tag_map"]["女主.png"] == "<Picture 1>"
    assert res["tag_map"]["女主"] == "<Picture 1>"


def test_compile_refs_still_accepts_old_alias(AS):
    """旧档正文里的 `@别名`（无后缀）照样解析 —— 迁移期的兼容闸门。"""
    links = [{"asset_id": "a_0123456789ab", "alias": "女主", "kind": "image",
              "ref_name": "女主.png"}]
    res = AS.compile_refs(_reg(AS, links=links), ["女主"], 1)
    assert res["ok"], res["errors"]
    assert res["blocks"][0]["token"] == "<Picture 1>"


def test_compile_refs_unknown_full_name_points_at_it(AS):
    """写了池里没有的全名 -> 点名报错（不是静默放过）。"""
    links = [{"asset_id": "a_0123456789ab", "alias": "女主", "kind": "image",
              "ref_name": "女主.png"}]
    res = AS.compile_refs(_reg(AS, links=links), ["路人.png"], 1)
    assert not res["ok"]
    assert res["errors"][0]["code"] == "E_REF_UNKNOWN"
    assert "路人.png" in res["errors"][0]["message"]


# ---- 前后端同口径（防止两份实现漂移）----

@pytest.mark.skipif(NODE is None, reason="需要 node 执行前端纯函数")
def test_frontend_clean_ref_name_matches_backend(AS):
    """前端 cleanRefName 与后端 clean_ref_name 必须对同一批样本给出同一结果。

    历史上 cleanLabel / clean_alias 就是两份实现，漂过一次（"前端认得、后端
    编译不出来"）。这条用真跑前端源码来锁死。
    """
    src = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    import re
    m = re.search(r"^function cleanRefName\(text\) \{.*?^\}", src, re.M | re.S)
    assert m, "未找到前端 cleanRefName"
    fn = m.group(0)
    samples = ["女主.png", "my photo (1).png", "  猫 咪 .png  ", "images/sub/阿依.png",
               "超长素材名" * 30 + "_14_02_12.png", "雨声.wav", "无后缀名"]
    code = (
        "const REF_NAME_MAX = 96;\n" + fn + "\n"
        + "console.log(JSON.stringify(" + json.dumps(samples, ensure_ascii=False)
        + ".map(cleanRefName)));"
    )
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    want = [AS.clean_ref_name(s) for s in samples]
    assert got == want, f"前后端引用名归一不一致：\n got={got}\nwant={want}"


@pytest.mark.skipif(NODE is None, reason="需要 node 执行前端纯函数")
def test_frontend_migrate_refs_to_full_names():
    """旧档迁移：`@别名` → `@全名`，且已经写了全名的不会被加成 `.png.png`。"""
    src = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    import re
    m = re.search(r"^function migrateRefsToFullNames\(ds, pool\) \{.*?^\}", src, re.M | re.S)
    assert m, "未找到前端 migrateRefsToFullNames"
    fn = m.group(0)
    code = """
    const refKeyOf = (a) => String((a && (a.ref_name || a.label)) || "");
    __FN__
    const pool = [
      { label: "女主", ref_name: "女主.png" },
      { label: "街道", ref_name: "街道.jpg" },
    ];
    const ds = {
      prompts: ["开头 @女主 站着，@女主.png 又出现一次，@街道 在远处"],
      segments: [{ refs: ["女主", "街道"] }, { refs: [] }],
    };
    const changed = migrateRefsToFullNames(ds, pool);
    console.log(JSON.stringify({ changed, p: ds.prompts[0], r: ds.segments[0].refs }));
    """.replace("__FN__", fn)
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["changed"] is True
    # 旧的裸别名被改写；已经是全名的那一次没被加成 .png.png
    assert out["p"] == "开头 @女主.png 站着，@女主.png 又出现一次，@街道.jpg 在远处", out["p"]
    assert out["r"] == ["女主.png", "街道.jpg"]
