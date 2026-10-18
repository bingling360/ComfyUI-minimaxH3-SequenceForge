"""ComfyUI 启动命令速查 —— 结构化数据 + 纯文本导出（**单一真源**）。

为什么要有这个模块
------------------
面板里的「📖 启动命令」弹窗、以及 `docs/ComfyUI启动命令速查.txt`，
都从**这一份** `SECTIONS` 生成 —— 改一处两处都变，不会一边新一边旧。
（本项目对「同一件事两处维护」的态度见 `perf.py` 里那批 ⛔ 注释。）

⚠ 2026-10-18 用户砍到 **3 节 12 条**（原 17 节 148 条）：原话是「别加那么多
没用的命令说明，专门挑一批常用且有用的」。所以这里只留**日常真会用到**的启动参数
与 Linux 后台运行三条；systemd / 防火墙 / nginx / Docker / 云平台那几大节已删除
（需要时去查官方文档，别在这个弹窗里堆）。

⚠ 纪律：`kind == "comfyui"` 的分区里出现的**每个 `--flag`** 都必须是真实存在的
ComfyUI 参数。`tests/test_launch_ref.py` 会解析 `comfy/cli_args.py` 逐个核对 ——
编造参数会直接测试红。非 ComfyUI 的命令（systemd / docker / nginx / ufw 那些）
统一放 `kind == "external"`，不参与那条核对。
"""
from __future__ import annotations

import re

# ============================ 数据 ============================
#
# 条目字段：
#   cmd   —— 命令原文（**逐字可复制**，不带提示符；提示符由渲染层加）
#   title —— 一句话标题
#   why   —— 为什么要这样 / 会发生什么（这是给人看的重点）
#   note  —— 可选，补充注意事项
#
# 分区字段：
#   id / title / note / kind("comfyui" | "external") / entries

SECTIONS = [
    # ---------------------------------------------------------------- 基础
    {
        "id": "basic",
        "title": "① 最小启动与远程访问",
        "kind": "comfyui",
        "note": "先在 ComfyUI 根目录把这节跑通，其余参数都加在它们后面。",
        "entries": [
            {
                "cmd": "python main.py",
                "title": "最小启动",
                "why": "只听 127.0.0.1:8188，只有本机能开。",
                "note": "虚拟环境里把 `python` 换成 `venv/bin/python`（Windows 是 "
                        "`venv\\Scripts\\python.exe`）。",
            },
            {
                "cmd": "python main.py --port 8189",
                "title": "换端口",
                "why": "8188 被占了就换。端口只管听哪个口，能不能从外面连取决于 --listen。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --port 8188",
                "title": "★ 允许外部访问（远程 / 云平台必加）",
                "why": "默认只听本机，局域网和公网都连不上。",
                "note": "⚠ 等于把这个口暴露出去，公网务必用安全组限制来源 IP。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --enable-cors-header",
                "title": "前端被跨域拦住时",
                "why": "放行跨域（默认 \"*\"）。只有入口页面和 API 不同源时才需要。",
            },
            {
                "cmd": "python main.py --auto-launch",
                "title": "启动后自动开浏览器",
                "why": "桌面机省一次点链接；远程/无头机器上没用。",
            },
        ],
    },
    # ---------------------------------------------------------------- 显存
    {
        "id": "vram",
        "title": "② 显存自救",
        "kind": "comfyui",
        "note": "二采 OOM 先加第一条。启动参数改完必须重启才生效。",
        "entries": [
            {
                "cmd": "python main.py --vram-headroom 1",
                "title": "★ 多留 1GB 显存（二采 OOM 首选）",
                "why": "让 DynamicVRAM 额外空出 1GB，权重会被更早换出去。",
                "note": "这是**每设备**预留，进程一起来就定死；不想重启就改面板"
                        "「性能优化 → 显存预留（GB）」，它写的是进程级那一项。",
            },
            {
                "cmd": "python main.py --vram-headroom 2",
                "title": "还炸就留 2GB",
                "why": "余量更大，代价是更多块要来回搬 —— 慢得也更明显。",
            },
            {
                "cmd": "python main.py --disable-comfy-compiler",
                "title": "关掉 Comfy 编译器（1GB 还不够时）",
                "why": "显存占用从接近 100% 降到 ~77%，代价约 5% 耗时。",
            },
            {
                "cmd": "python main.py --cache-none",
                "title": "不缓存节点结果（最省内存）",
                "why": "每次执行都重算，换来更低的主机内存峰值。",
            },
        ],
    },
    # ---------------------------------------------------------------- Linux
    {
        "id": "linux-bg",
        "title": "③ Linux 后台运行",
        "kind": "external",
        "note": "云平台上断开 SSH 就停的话，用这三条。",
        "entries": [
            {
                "cmd": "nohup python main.py --listen 0.0.0.0 --port 8188 > comfyui.log 2>&1 &",
                "title": "脱离终端跑",
                "why": "关掉 SSH 也不退；日志落盘，出问题还能翻。",
            },
            {
                "cmd": "tail -f comfyui.log",
                "title": "看实时日志",
                "why": "模型加载与 OOM 都在这行里。",
            },
            {
                "cmd": "pkill -f \"python main.py\"",
                "title": "停掉它",
                "why": "按命令行匹配杀进程；改完启动参数要重启时用。",
            },
        ],
    },
]


# ============================ 导出 ============================

TITLE = "ComfyUI 启动命令速查"

# 基础命令。`kind == "comfyui"` 的分区里，条目都是**加在它后面的参数** ——
# 展示时把这段前缀剥掉，只留「要加的东西」。
#
# ⚠ 2026-09-27 用户反馈：「咋所有命令前面都有 `python main.py` 这一行，
# 把要加的东西列出来不就得了」—— 对，几十条命令每条都重复一遍基础命令是纯噪音。
# 数据里仍存**完整命令**（无歧义、可被 all_comfy_flags 直接扫），
# 剥前缀只发生在展示层（`_entry_cmd`），改一处两处都变。
BASE_CMD = "python main.py"


def _entry_cmd(sec, e):
    """返回 `(展示/复制的内容, 是否是完整命令)`。

    **按命令本身判断，不按分区类型**（2026-09-27 修正）：云平台/故障速查那两节
    标着 `external`（里面混着 `df -h`、`nvidia-smi` 这类 shell 命令），
    但它们的条目里也有 `python main.py --listen ...` —— 那些同样该只留参数。
    一开始按「分区 kind」判，结果那两节的 10 条漏剥了。

    两种例外原样返回并标成完整命令：
      · 那行**本身就是基础命令**（「最小启动」讲的就是它）；
      · 带环境变量前缀（`CUDA_VISIBLE_DEVICES=0 python main.py`）。
    """
    cmd = str(e["cmd"])
    if cmd == BASE_CMD:
        return cmd, True
    if cmd.startswith(BASE_CMD + " "):
        return cmd[len(BASE_CMD) + 1:], False
    return cmd, True


def _section_has_args(sec):
    """这一节里有没有「只列参数」的条目 —— 决定要不要印那句「都加在 main.py 后面」。"""
    return any(not _entry_cmd(sec, e)[1] for e in sec["entries"])


def has_args():
    """整份文档里有没有「只列参数」的条目。"""
    return any(_section_has_args(s) for s in SECTIONS)


def base_example():
    """基础命令的一个**完整写法示例**。

    从数据里拼出来，**不写死** —— 写死就是第二份命令副本，改了数据它不会跟着变
    （与前端「不硬编码命令」同一条纪律）。

    ⚠ 优先挑**带 ≥2 个参数**的条目：只挑第一条会落到「换端口」那种单参数条目上
    （`python main.py --port 8189`），当示例读起来像在教人改端口，不具代表性。
    挑不到就退回第一条纯参数的。
    """
    first = None
    for s in SECTIONS:
        for e in s["entries"]:
            show, full = _entry_cmd(s, e)
            if full:
                continue
            if first is None:
                first = show
            if show.count("--") >= 2:
                return f"{BASE_CMD} {show}"
    return f"{BASE_CMD} {first}" if first else BASE_CMD


def _comfy_flags_in(cmd):
    """从一段命令里抠出所有 `--flag`（长参数），去掉 `=value` 部分。

    只认以 `--` 开头、后面跟字母/数字/连字符/下划线的 token。
    """
    out = []
    for tok in str(cmd or "").replace("\n", " ").split():
        if not tok.startswith("--"):
            continue
        name = tok.split("=", 1)[0].strip("\"'")
        if len(name) > 2 and all(c.isalnum() or c in "-_" for c in name[2:]):
            out.append(name)
    return out


def all_comfy_flags():
    """文档里出现的全部 ComfyUI 参数（去重、保序）—— 给「不许编造参数」的测试用。"""
    seen = []
    for sec in SECTIONS:
        if sec.get("kind") != "comfyui":
            continue
        for e in sec["entries"]:
            for f in _comfy_flags_in(e["cmd"]):
                if f not in seen:
                    seen.append(f)
    return seen


def payload():
    """给前端弹窗用的 JSON（纯 dict，可直接 json.dumps）。

    ⚠ 每条 entry 除原始 `cmd` 外，另给两个**展示层**字段（2026-09-27 加）：
      · `show` —— 要展示/复制的内容。comfyui 分区里已剥掉 `python main.py`
        前缀，只剩要加的参数（见 `_entry_cmd`）。
      · `full` —— True 表示 `show` 本身就是完整命令（外部工具、或带环境变量前缀）。
    前端**只读 `show` / `full`**，别自己去剥 —— 剥的规则只有一处。

    `base_cmd` / `base_example` / `has_args` 放**顶层**（2026-09-27 改）：
    原先挂在每个分区上、由前端逐节渲染，结果 17 个分区把那句「参数都加在
    python main.py 后面」重复了 11 遍 —— 与「每条都重复基础命令」是同一类噪音。
    现在前端只在正文开头印一次。
    """
    secs = []
    for s in SECTIONS:
        out = dict(s)
        out["entries"] = []
        for e in s["entries"]:
            show, full = _entry_cmd(s, e)
            out["entries"].append(dict(e, show=show, full=full))
        secs.append(out)
    n = sum(len(s["entries"]) for s in SECTIONS)
    return {
        "title": TITLE,
        "base_cmd": BASE_CMD,
        "base_example": base_example(),
        "has_args": has_args(),
        "sections": secs,
        "section_count": len(SECTIONS),
        "entry_count": n,
        "comfy_flag_count": len(all_comfy_flags()),
        "txt_name": TXT_NAME,
    }


TXT_NAME = "ComfyUI启动命令速查.txt"

_RULE = "=" * 78
_THIN = "-" * 78


def _plain(s):
    """把面板用的 markdown 轻标记洗成纯文本：`**粗**` → 粗，`` `代码` `` → 代码。

    这份 txt 的用途是拿到终端 / 编辑器里读，`**` 和反引号在那儿只是噪音
    （弹窗里由前端 `mdBold()` 渲染成真加粗，两边各用各的表示法）。
    **只洗标记、保留内容** —— 不改成 `【】` 之类，那只是换一种噪音。
    """
    s = str(s if s is not None else "")
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    return s


def to_text():
    """生成纯文本版（LF 换行 + 轻标记已洗，见 `_plain`）。

    ⚠ **显式用 LF**：这份 txt 的主要用途是拿到 Linux 机器上边看边敲，
    CRLF 在 vim/less 里会多出 ^M，复制到 shell 还会带上 \\r 导致命令报错。
    """
    lines = [
        _RULE,
        f"  {TITLE}",
        "  由 ComfyUI-minimaxH3-SequenceForge 生成（单一真源：launch_ref.py）",
        "  —— 面板里的「📖 启动命令」与本文件内容同源，改一处两处都变。",
        _RULE,
        "",
        "说明：",
        f"  · 标「参数：」的行是「要加在 {BASE_CMD} 后面的东西」，只列参数本身；",
        "    标「$」的行是完整命令，可直接跑。",
        "  · 不重复写基础命令（每条都带一遍 python main.py 是纯噪音）。完整写法示例：",
        f"      {base_example()}",
        "  · 「⚠」是踩过的坑，建议先看。",
        "  · 「★」是本插件场景下最常用的几条。",
        "  · ③ 那节与 ComfyUI 本身无关，是 Linux 部署环境的事。",
        "",
    ]
    for i, sec in enumerate(SECTIONS, 1):
        lines.append("")
        lines.append(_RULE)
        lines.append(f"  {_plain(sec['title'])}")
        lines.append(_RULE)
        if sec.get("note"):
            lines.append("")
            lines.append(f"  {_plain(sec['note'])}")
        for e in sec["entries"]:
            show, full = _entry_cmd(sec, e)
            lines.append("")
            lines.append(f"  ▸ {_plain(e['title'])}")
            for j, cl in enumerate(show.split("\n")):
                if full:
                    lines.append(("    $ " if j == 0 else "      ") + cl)
                else:
                    lines.append(("    参数：" if j == 0 else "          ") + cl)
            # ⚠ 不再写「为什么要这样：」/「注意：」这类标签（2026-09-27 用户反馈：
            # 「咋有一堆为什么要这样，你直接说明不就得了」）—— 直接说事。
            # why 与 note 在展示上就是一前一后两段，区别只在数据模型里。
            lines.append(f"    {_plain(e['why'])}")
            if e.get("note"):
                lines.append(f"    {_plain(e['note'])}")
    lines += [
        "",
        _RULE,
        "  完",
        _RULE,
        "",
    ]
    return "\n".join(lines)


def write_text(path):
    """把 txt 写到 `path`（显式 LF，见 `to_text` 注释）。返回写入的字节数。"""
    data = to_text()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)
    return len(data.encode("utf-8"))
