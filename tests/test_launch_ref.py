"""启动命令速查（`launch_ref.py` + docs 下的 txt）的守护测试。

三条核心守卫：

1. **不许编造 ComfyUI 参数** —— 文档里每个 `--flag` 都必须真实存在于
   `comfy/cli_args.py`。这是这份文档最容易出的错（凭印象写参数），
   而错了的后果是用户复制去执行直接报错、还以为是环境问题。
2. **txt 不许过期** —— 磁盘上 `docs/<name>.txt` 必须与 `to_text()` 归一化
   换行后逐字一致（Windows autocrlf 检出洗出来的 CRLF 不算过期）。
   改了数据忘了重跑 `tools/make_launch_ref_txt.py` 就红。
3. **LF 换行** —— 这份 txt 是拿到 Linux 上边看边敲的，CRLF 会让复制出来的
   命令带上 `\\r` 直接报错。
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import launch_ref as L  # noqa: E402

# 插件装在 <ComfyUI>/custom_nodes/<本插件> 下 → ComfyUI 根是上两级
COMFY_ROOT = os.path.dirname(os.path.dirname(ROOT))
CLI_ARGS = os.path.join(COMFY_ROOT, "comfy", "cli_args.py")


def _real_flags():
    if not os.path.isfile(CLI_ARGS):
        pytest.skip(f"找不到 {CLI_ARGS}（插件不在 ComfyUI/custom_nodes 下？）")
    with open(CLI_ARGS, encoding="utf-8") as f:
        src = f.read()
    return set(re.findall(r'add_argument\(\s*"(--[A-Za-z0-9_-]+)"', src))


def _git_blob_text(rel):
    """`HEAD:<rel>` 的 blob 原文；拿不到（未提交 / 非 git 仓库 / 无 git）给 None。"""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", ROOT, "cat-file", "blob",
             "HEAD:" + rel.replace(os.sep, "/")],
            capture_output=True, timeout=15)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8")


# ---------------------------------------------------------------- 结构

def test_sections_shape():
    """分区与条目的字段齐全，且 id 不重复（前端按 id 做折叠记忆键）。"""
    assert L.SECTIONS, "分区表是空的？"
    ids = [s["id"] for s in L.SECTIONS]
    assert len(ids) == len(set(ids)), f"分区 id 重复：{ids}"
    for s in L.SECTIONS:
        assert set(s) >= {"id", "title", "kind", "entries", "note"}, s["id"]
        assert s["kind"] in ("comfyui", "external"), s["id"]
        assert s["entries"], f"{s['id']} 没有条目"
        for e in s["entries"]:
            assert set(e) >= {"cmd", "title", "why"}, (s["id"], e)
            assert e["cmd"].strip(), (s["id"], e["title"])
            assert e["why"].strip(), (s["id"], e["title"])


def test_titles_are_numbered_and_unique():
    """标题带序号（①②…）且互不相同 —— 用户是按「第几节」互相沟通的。"""
    titles = [s["title"] for s in L.SECTIONS]
    assert len(titles) == len(set(titles)), "分区标题重复"
    circled = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    for i, t in enumerate(titles):
        assert circled[i] in t, f"第 {i + 1} 个分区标题缺序号：{t}"


def test_linux_coverage():
    """★ Linux 那几节必须真的存在且够厚 —— 这是用户明确要的重点。

    写这条是因为「Linux 部分写详细点」很容易在后续重构里被顺手删掉
    （比如有人觉得「systemd 跟 ComfyUI 无关」）。
    """
    ids = {s["id"] for s in L.SECTIONS}
    for need in ("linux-bg", "linux-systemd", "linux-port", "linux-proxy",
                 "linux-docker", "cloud"):
        assert need in ids, f"缺 Linux/云平台分区：{need}"
    by = {s["id"]: s for s in L.SECTIONS}
    # 开机自启 / 端口 这两节是用户点名要的，条数不能太少
    assert len(by["linux-systemd"]["entries"]) >= 8
    assert len(by["linux-port"]["entries"]) >= 10
    # 关键词抽查：systemd 必须给出 unit 文件本体与 enable 命令
    sysd = "\n".join(e["cmd"] for e in by["linux-systemd"]["entries"])
    for kw in ("[Unit]", "ExecStart=", "WantedBy=multi-user.target",
               "systemctl enable", "journalctl"):
        assert kw in sysd, f"systemd 那节缺关键内容：{kw}"
    # 端口那节必须覆盖三种防火墙
    port = "\n".join(e["cmd"] for e in by["linux-port"]["entries"])
    for kw in ("ufw", "firewall-cmd", "iptables", "ss -lntp"):
        assert kw in port, f"端口那节缺：{kw}"


# ---------------------------------------------------------------- 参数真实性

def test_no_invented_comfy_flags():
    """★★ 文档里出现的每个 ComfyUI 参数都必须真实存在。

    这是本文件最重要的守卫：凭印象写参数是这份文档最容易犯的错，
    而错了的后果是用户复制去执行直接报错、还以为是环境问题。
    """
    real = _real_flags()
    doc = L.all_comfy_flags()
    assert doc, "一个 ComfyUI 参数都没扫到？正则或数据结构坏了"
    bad = [f for f in doc if f not in real]
    assert not bad, (
        "文档里出现了 cli_args.py 里不存在的参数（拼错或编造）："
        + "、".join(bad))


def test_flag_scanner_is_not_vacuous():
    """扫参数的函数本身要有效 —— 别出现「什么都扫不到所以永远通过」。"""
    assert L._comfy_flags_in("python main.py --listen 0.0.0.0 --port 8188") == \
        ["--listen", "--port"]
    assert L._comfy_flags_in("--vram-headroom=1") == ["--vram-headroom"]
    assert L._comfy_flags_in("-- --not-a-flag?") == []
    assert L._comfy_flags_in("") == []


def test_external_sections_are_excluded_from_flag_check():
    """external 分区（systemd/docker/nginx）里的 `--flag` 不该被当成 ComfyUI 参数。

    docker 的 `--gpus`、nginx 的 `--add-port` 这些不是 ComfyUI 的参数，
    混进去会让上一条守卫误报。
    """
    comfy = L.all_comfy_flags()
    ext = []
    for s in L.SECTIONS:
        if s["kind"] != "external":
            continue
        for e in s["entries"]:
            ext += L._comfy_flags_in(e["cmd"])
    assert ext, "external 分区里一个 --flag 都没有？样例数据不对"
    # 两边不该有交集之外的误报：external 里特有的那些不应出现在 comfy 清单里
    assert "--gpus" not in comfy
    assert "--permanent" not in comfy


# ---------------------------------------------------------------- txt 导出

def test_text_has_no_cr():
    """★ 纯 LF —— 这份 txt 要在 Linux 上边看边敲，CRLF 会让命令带上 \\r 报错。"""
    txt = L.to_text()
    assert "\r" not in txt, "txt 里出现了 CR（必须是 LF）"
    assert "\n" in txt


def test_text_covers_every_section_and_entry():
    """txt 不许漏节漏条 —— 弹窗里看得到、txt 里没有，等于两份内容。

    ⚠ 标题要拿 `_plain()` 洗过再比：txt 里是纯文本，`**粗**` 的标记被洗掉了
    （弹窗里才渲染成真加粗），拿原文比会假红。
    ⚠ 命令要比 `_entry_cmd()` 出来的**展示值**：comfyui 分区里 `python main.py`
    前缀被剥掉了，拿原始 `cmd` 比也会假红。
    """
    txt = L.to_text()
    assert L.TITLE in txt
    for s in L.SECTIONS:
        assert L._plain(s["title"]) in txt, f"txt 缺分区：{s['title']}"
        for e in s["entries"]:
            assert L._plain(e["title"]) in txt, f"txt 缺条目：{s['id']} / {e['title']}"
            show, _full = L._entry_cmd(s, e)
            for line in show.split("\n"):
                assert line.strip() in txt, f"txt 缺命令：{line!r}"


def test_entry_cmd_strips_the_redundant_base():
    """★ 剥掉 `python main.py` 前缀 —— 用户反馈「咋每条都带这一行」。

    但**两种例外必须原样保留**（它们本身就是完整命令，剥了就废了）：
      · 那行就是基础命令本身（「最小启动」讲的就是它）；
      · 带环境变量前缀（`CUDA_VISIBLE_DEVICES=0 python main.py`）。
    """
    comfy = next(s for s in L.SECTIONS if s["kind"] == "comfyui")
    ext = next(s for s in L.SECTIONS if s["kind"] == "external")

    assert L._entry_cmd(comfy, {"cmd": "python main.py --port 8189"}) == \
        ("--port 8189", False)
    assert L._entry_cmd(comfy, {"cmd": "python main.py"}) == ("python main.py", True)
    assert L._entry_cmd(comfy, {"cmd": "CUDA_VISIBLE_DEVICES=0 python main.py"}) == \
        ("CUDA_VISIBLE_DEVICES=0 python main.py", True)
    # external 分区里**不是** python main.py 的命令不剥（systemd / docker / nginx）
    assert L._entry_cmd(ext, {"cmd": "sudo systemctl restart comfyui"}) == \
        ("sudo systemctl restart comfyui", True)
    # ★ 但 external 分区里**是** python main.py 的照样剥 —— 云平台/故障速查那两节
    #   标着 external（混着 df -h / nvidia-smi），却也有 ComfyUI 命令；
    #   一开始按「分区 kind」判，那 10 条漏剥了。
    assert L._entry_cmd(ext, {"cmd": "python main.py --listen 0.0.0.0"}) == \
        ("--listen 0.0.0.0", False)

    # 落到真数据上：comfyui 分区里绝大多数都该被剥成纯参数
    stripped = full = 0
    for s in L.SECTIONS:
        for e in s["entries"]:
            _show, is_full = L._entry_cmd(s, e)
            if is_full:
                full += 1
            else:
                stripped += 1
    assert stripped > 60, f"剥出来只有 {stripped} 条？剥的规则坏了"
    assert full > 0, "一条完整命令都没有？external 分区应该全是"


def test_txt_does_not_repeat_base_cmd_every_line():
    """★ txt 里不许每条都重复 `python main.py` —— 那正是用户反馈的噪音。

    判据：`$ python main.py` 这种整行只允许出现在**标成完整命令**的条目上，
    其余行都不该带它。
    """
    txt = L.to_text()
    n_prefix = txt.count("$ " + L.BASE_CMD)
    # 允许的：① 最小启动那条 ② 环境变量那条（它是 `CUDA_VISIBLE_DEVICES=0 python main.py`，
    # 不匹配 `$ python main.py` 前缀，所以不计）→ 实际上只该有 1 条
    assert n_prefix <= 1, (
        f"txt 里有 {n_prefix} 处 `$ python main.py` —— 又把基础命令重复贴回去了")
    # ★ 那句「参数都加在 python main.py 后面」只能出现**一次**（开头说明区）。
    #   第一版挂在每个分区上，17 个分区重复了 11 遍 —— 用户第二次反馈的正是这类
    #   「一堆重复的标签」。
    assert txt.count(f"要加在 {L.BASE_CMD} 后面的东西") == 1, (
        "基础命令说明该只在开头说一次，实际 "
        f"{txt.count(f'要加在 {L.BASE_CMD} 后面的东西')} 处")


def test_txt_has_no_repeated_field_labels():
    """★★ txt 里不许出现「为什么要这样：」「注意：」这类**逐条重复的标签**。

    用户反馈原话：「咋有一堆为什么要这样，你直接说明不就得了」。
    这类标签的毛病与「每条都重复 python main.py」同源：信息量为零、只是噪音。
    判据：这两个词在整个 txt 里出现 0 次。
    """
    txt = L.to_text()
    for bad in ("为什么要这样", "注意："):
        n = txt.count(bad)
        assert n == 0, f"txt 里有 {n} 处「{bad}」标签 —— 直接说事，别加标签"
    # 但**内容**必须还在：随便抽一条，它的 why / note 都得原样出现
    sec = next(s for s in L.SECTIONS if s["id"] == "vram")
    e = sec["entries"][0]
    assert L._plain(e["why"]) in txt, "why 被连着标签一起删了"
    assert L._plain(e["note"]) in txt, "note 被连着标签一起删了"


def test_base_example_is_derived_not_hardcoded():
    """示例必须**从数据里拼**出来，不能写死 —— 写死就是第二份命令副本。"""
    ex = L.base_example()
    assert ex.startswith(L.BASE_CMD + " "), ex
    # 它必须真的是「某条纯参数条目的完整写法」
    shows = {L._entry_cmd(s, e)[0] for s in L.SECTIONS for e in s["entries"]}
    assert ex[len(L.BASE_CMD) + 1:] in shows, f"{ex!r} 不是任何条目的完整写法"
    assert L.has_args() is True


def test_text_strips_markdown_markers():
    """★ txt 是纯文本，`**粗**` / 反引号在终端里只是噪音 —— 要洗掉（内容保留）。

    为什么单列一条：这些标记在**弹窗**里由前端 `mdBold()` 渲染成真加粗，
    很容易被顺手带到 txt 输出里，而 txt 是拿到 Linux 上边看边敲的，
    没人会去脑补 `**` 是加粗。
    """
    assert L._plain("**粗**") == "粗"
    assert L._plain("`code`") == "code"
    assert L._plain("**a** 和 `b` 混着") == "a 和 b 混着"
    # 落单的 `**`（ComfyUI 日志里真有 `** Log path:` 这种原文）不该被吃掉
    assert L._plain("日志里 ** Log path: 那行") == "日志里 ** Log path: 那行"
    txt = L.to_text()
    assert "**数据盘**" not in txt, "标题里的 markdown 标记没洗"
    # 正文里也不该有成对的 `**`
    assert txt.count("**") <= 2, f"txt 里还有成对的 markdown 标记：{txt.count('**')}"


def test_text_write_is_idempotent(tmp_path):
    """写两次内容一致（无时间戳之类的抖动，否则「txt 过期」守卫会天天假红）。"""
    p1, p2 = tmp_path / "a.txt", tmp_path / "b.txt"
    L.write_text(str(p1))
    L.write_text(str(p2))
    assert p1.read_bytes() == p2.read_bytes()


def test_committed_txt_is_up_to_date():
    """★★ 磁盘上那份 txt 必须与实时生成的一致。

    改了 launch_ref.py 却忘了跑 `tools/make_launch_ref_txt.py` → 这里红。
    这正是「单一真源」在 CI 上的落点：允许生成物存在，但不许它过期。

    ⚠ 比对按归一化换行后的内容：Windows 上 core.autocrlf=true 的检出会把
    仓库里的 LF 洗成工作区的 CRLF —— 那是 git 的检出行为，不是「生成物过期」，
    拿原始字节比会在 Windows 上天天假红。
    ⚠ 「必须是 LF」守卫查的是**仓库 blob**（真正会被提交/分发的内容）：
    工作区副本被 autocrlf 洗过不冤枉它；blob 拿不到（未提交/无 git）就跳过。
    """
    rel = os.path.join("docs", L.TXT_NAME)
    out = os.path.join(ROOT, rel)
    assert os.path.isfile(out), (
        f"缺 {out} —— 跑一下 tools/make_launch_ref_txt.py")
    with open(out, encoding="utf-8", newline="") as f:
        on_disk = f.read()
    assert on_disk.replace("\r\n", "\n") == L.to_text(), (
        "docs 下的 txt 与 launch_ref.py 不一致 —— 跑 "
        "`python tools/make_launch_ref_txt.py` 重新生成")
    blob = _git_blob_text(rel)
    if blob is not None:
        assert "\r" not in blob, "仓库里那份带 CRLF"


def test_txt_name_is_plain_and_portable():
    """文件名要能安全落到任何文件系统上（云平台/容器里常是 ext4 + 各种 locale）。"""
    assert L.TXT_NAME.endswith(".txt")
    for ch in '/\\:*?"<>|':
        assert ch not in L.TXT_NAME, f"文件名含非法字符 {ch!r}"


# ---------------------------------------------------------------- payload

def test_payload_shape():
    """前端弹窗吃的结构。键名是接口，不许漂。"""
    p = L.payload()
    assert set(p) == {"title", "base_cmd", "base_example", "has_args", "sections",
                       "section_count", "entry_count", "comfy_flag_count", "txt_name"}
    assert p["section_count"] == len(L.SECTIONS)
    assert p["entry_count"] == sum(len(s["entries"]) for s in L.SECTIONS)
    assert p["comfy_flag_count"] == len(L.all_comfy_flags())
    assert p["txt_name"] == L.TXT_NAME
    assert p["base_cmd"] == L.BASE_CMD
    assert p["base_example"] == L.base_example()
    assert p["has_args"] is True
    # sections 必须是深拷贝：调用方改它不该污染模块级数据
    p["sections"][0]["title"] = "改坏了"
    assert L.SECTIONS[0]["title"] != "改坏了"
    # entries 也必须是新对象（浅拷贝会漏掉这一层）
    p2 = L.payload()
    p2["sections"][0]["entries"][0]["title"] = "也改坏了"
    assert L.SECTIONS[0]["entries"][0]["title"] != "也改坏了"


def test_payload_entries_carry_show_and_full():
    """★ 展示层字段：前端只读 `show` / `full`，别自己剥前缀（剥的规则只有一处）。

    ⚠ `base` **不再挂在分区上**（2026-09-27）：逐节渲染会把同一句说明重复 11 遍，
    改成顶层 `base_cmd` / `base_example` / `has_args`，前端只印一次。
    """
    p = L.payload()
    for s in p["sections"]:
        assert "base" not in s, "分区上不该再挂 base（会逐节重复）"
        for e in s["entries"]:
            assert set(e) >= {"cmd", "show", "full", "title", "why"}, e
            assert isinstance(e["full"], bool), e
            if not e["full"]:
                assert not e["show"].startswith(L.BASE_CMD), \
                    "标成「不是完整命令」却还带着基础命令前缀：" + e["show"]
                assert e["cmd"] == L.BASE_CMD + " " + e["show"], e
    # 剥完不该出现空条目
    for s in p["sections"]:
        for e in s["entries"]:
            assert e["show"].strip(), f"{s['id']} / {e['title']} 剥完是空的"


def test_payload_is_json_serializable():
    """要能直接 json.dumps —— 有一条中文/引号没处理好就会在接口层炸。"""
    import json
    s = json.dumps(L.payload(), ensure_ascii=False)
    assert len(s) > 1000
    assert json.loads(s)["title"] == L.TITLE
