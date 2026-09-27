/* 📖 启动命令速查弹窗守卫。
 *
 * 背景：用户要一个「启动命令 + Linux 部署」的速查入口 —— 顶栏按钮 → 弹窗，
 * 每条命令一个独立复制按钮，还要能导出 .txt（拿到云平台机器上边看边敲）。
 *
 * 本文件钉这几件事：
 *   ① 入口在顶栏，且紧挨「⚡ 性能优化」
 *   ② 内容**全部来自后端**：前端不硬编码任何命令（用桩里的 MARKER 命令验证）
 *   ③ 每条命令**都有**自己的复制按钮（不能是「复制全部」那种）
 *   ④ 点复制拿到的必须是**命令原文**（多行命令要连换行一起，不是屏幕上折行的样子）
 *   ⑤ 有 .txt 导出入口，且指向后端路由（不前端拼内容 —— 那是第二份副本）
 *   ⑥ 分区可折叠 + 有跳转条（17 个分区一屏放不下）
 *
 * 用法：node tests/js/launch_ref_check.js
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { load, ROOT } = require("./_harness");

const SRC = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8");

/* 桩数据：命令里塞 MARKER，用来证明「渲染出来的命令确实来自接口」——
 * 前端源码里不可能有这串东西。
 * `show` / `full` 是后端给的**展示层**字段（2026-09-27 加）：comfyui 分区的条目
 * 已剥掉 `python main.py` 前缀，前端只读 show、不自己剥。 */
const MARK_A = "MARKER-CMD-ALPHA --listen 0.0.0.0";
const MARK_B = "MARKER-CMD-BRAVO\n[Unit]\nExecStart=/opt/ComfyUI/venv/bin/python";
const BASE = "python main.py";
const REF = {
    title: "ComfyUI 启动命令速查",
    base_cmd: BASE,
    base_example: BASE + " " + MARK_A,
    has_args: true,
    section_count: 2,
    entry_count: 3,
    comfy_flag_count: 72,
    txt_name: "ComfyUI启动命令速查.txt",
    sections: [
        { id: "basic", title: "① 基础启动", kind: "comfyui", note: "先确认这几条能跑通。",
          entries: [
              { cmd: BASE + " " + MARK_A, show: MARK_A, full: false,
                title: "最小启动", why: "只监听**本机**。", note: "⚠ 用 venv 时换 python 路径。" },
              { cmd: BASE + " --port 8189", show: "--port 8189", full: false,
                title: "换端口", why: "8188 被占了就换。" },
          ] },
        { id: "linux-systemd", title: "⑫ ★ Linux：开机自启", kind: "external",
          note: "开机自启 + 崩溃自动拉起。",
          entries: [
              { cmd: MARK_B, show: MARK_B, full: true,
                title: "service 文件内容", why: "Restart=always 自动拉起。" },
          ] },
    ],
};

let ok = true;
const results = [];
async function t(name, fn) {
    try { await fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}

(async () => {
    console.log("\n== 📖 启动命令速查弹窗 ==");

    /* 源码层：入口存在且挂在顶栏按钮上（不靠 jsdom 打开也能先验一半） */
    await t("openLaunchRef 是顶层函数（弹窗入口在）", () => {
        assert.ok(/^async function openLaunchRef\(/m.test(SRC),
            "找不到顶层 openLaunchRef");
    });

    await t("★ 前端不硬编码任何命令（命令只来自后端）", () => {
        /* 渲染逻辑里不该出现具体命令字面量 —— 出现就说明有人把数据抄了一份进前端，
         * 于是改后端不会同步、两份迟早不一致（本项目对「单一真源」的态度见 perf.py）。 */
        const body = SRC.slice(SRC.indexOf("async function openLaunchRef("),
                               SRC.indexOf("\nasync function openOptSettings("));
        assert.ok(body.length > 500, "切不出 openLaunchRef 函数体");
        for (const bad of ["--vram-headroom", "--listen", "systemctl", "ExecStart",
                           "ufw ", "firewall-cmd"]) {
            assert.ok(body.indexOf(bad) < 0,
                "openLaunchRef 里出现了硬编码命令片段 " + bad
                + " —— 命令必须全部来自 /h3chain/launch_ref");
        }
        assert.ok(body.indexOf("A.launchRef") >= 0, "没调后端接口");
    });

    await t("顶栏有「📖 启动命令」按钮，且紧挨「⚡ 性能优化」", () => {
        assert.ok(SRC.indexOf('"📖 启动命令"') >= 0, "顶栏缺按钮");
        assert.ok(SRC.indexOf("cmdRefBtn.onclick = openLaunchRef") >= 0, "按钮没绑入口");
        const append = SRC.match(/right\.append\(([^)]*)\)/);
        assert.ok(append, "找不到顶栏按钮区 right.append(...)");
        const order = append[1];
        const iPerf = order.indexOf("perfBtn");
        const iCmd = order.indexOf("cmdRefBtn");
        assert.ok(iPerf >= 0 && iCmd >= 0, "两个按钮都该在顶栏：" + order);
        assert.ok(iCmd > iPerf, "「启动命令」该排在「性能优化」后面（同级相邻）");
    });

    /* 打开真弹窗 */
    let copied = [];
    let openedUrl = null;
    const { w, errors } = load({
        H3Api: { async launchRef() { return { body: { ok: true, data: REF } }; } },
    });
    Object.defineProperty(w.navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (x) => { copied.push(String(x)); } },
    });
    /* 下载按钮是 <a download href=...>，jsdom 里 click 会走导航 —— 拦下来记地址 */
    const origClick = w.HTMLAnchorElement.prototype.click;
    w.HTMLAnchorElement.prototype.click = function () { openedUrl = this.getAttribute("href"); };

    await w.openLaunchRef();
    const overlay = w.document.querySelector(".h3d-lref-overlay");

    await t("弹窗打开了，且复用性能优化那套外壳", () => {
        assert.ok(overlay, "没有 .h3d-lref-overlay");
        assert.ok(overlay.querySelector(".h3d-perf-body"), "没复用 .h3d-perf-body");
        assert.ok(overlay.querySelector(".h3d-perf-foot"), "没复用固定底栏");
    });

    await t("分区数与条目数来自接口，且分区标题/说明都渲染了", () => {
        const secs = [...overlay.querySelectorAll(".h3d-lref-sec")];
        assert.strictEqual(secs.length, REF.sections.length,
            "分区数不对：" + secs.length);
        for (const s of REF.sections) {
            const el = overlay.querySelector('[data-lref-sec="' + s.id + '"]');
            assert.ok(el, "缺分区 " + s.id);
            assert.ok(el.querySelector("summary").textContent.indexOf(s.title) >= 0,
                s.id + " 的标题没渲染");
            const note = el.querySelector(".h3d-perf-stage-note");
            assert.ok(note && note.textContent === s.note, s.id + " 的说明没渲染");
        }
    });

    await t("★ 命令来自后端（MARKER 渲染出来了）", () => {
        const text = overlay.textContent;
        assert.ok(text.indexOf("MARKER-CMD-ALPHA") >= 0, "基础启动那条没渲染");
        assert.ok(text.indexOf("MARKER-CMD-BRAVO") >= 0, "systemd 那条没渲染");
    });

    await t("★★ 不再每条都重复 `python main.py`（用户反馈的噪音）", () => {
        /* 用户原话：「咋所有命令前面都有这一行，把要加的东西列出来不就得了」。
         * 判据：命令块里**只有**参数；基础命令只在正文开头说一次。 */
        const cmds = [...overlay.querySelectorAll(".h3d-lref-cmd")]
            .map((p) => p.textContent);
        for (const c of cmds) {
            assert.ok(c.indexOf("python main.py") < 0,
                "命令块里还带着基础命令前缀：" + JSON.stringify(c));
        }
        assert.ok(cmds.indexOf(MARK_A) >= 0, "参数那行该只显示参数：" + cmds);
        assert.ok(cmds.indexOf("--port 8189") >= 0, "换端口那条该只显示 --port 8189");
        /* 基础命令必须在**正文开头**说一次（不能既剥掉又不说，那就没法用了）。
         * ⚠ 2026-09-27 改：原先挂在每个分区上，17 个分区重复 11 遍 ——
         * 与「每条都重复基础命令」是同一类噪音，所以只允许出现一次。 */
        const baseNotes = overlay.querySelectorAll(".h3d-lref-base");
        assert.strictEqual(baseNotes.length, 1,
            "基础命令说明该只在开头出现一次，实际 " + baseNotes.length + " 处");
        assert.ok(baseNotes[0].textContent.indexOf(BASE) >= 0, "该写出基础命令");
        assert.ok(baseNotes[0].textContent.indexOf(REF.base_example) >= 0,
            "该给出一个完整写法示例");
        /* 分区里不该再有第二处 */
        for (const g of overlay.querySelectorAll(".h3d-lref-sec")) {
            assert.ok(g.querySelector(".h3d-lref-base") === null,
                "分区里又印了一遍基础命令说明");
        }
    });

    await t("★★ 不再有「为什么要这样」这类逐条重复的标签", () => {
        /* 用户原话：「咋有一堆为什么要这样，你直接说明不就得了」。
         * 判据：整块正文里不许出现这个词；但 why / note 的**内容**必须在。 */
        const txt = overlay.querySelector(".h3d-perf-body").textContent;
        assert.ok(txt.indexOf("为什么要这样") < 0,
            "还印着「为什么要这样」标签 —— 直接说事");
        assert.ok(txt.indexOf("只监听本机。") >= 0, "why 被连着标签一起删了");
        assert.ok(txt.indexOf("用 venv 时换 python 路径") >= 0, "note 被删了");
    });

    await t("★ 每行标出「参数」还是「完整命令」", () => {
        const rows = [...overlay.querySelectorAll(".h3d-lref-row")];
        const kindOf = (tt) => rows.find((r) =>
            r.querySelector(".h3d-lref-title").textContent === tt)
            .querySelector(".h3d-lref-kind").textContent;
        assert.strictEqual(kindOf("最小启动"), "参数");
        assert.strictEqual(kindOf("service 文件内容"), "完整命令");
    });

    await t("★ 每条命令都有自己的复制按钮（不是「复制全部」）", () => {
        const rows = [...overlay.querySelectorAll(".h3d-lref-row")];
        assert.strictEqual(rows.length, REF.entry_count,
            "命令行数不对：" + rows.length);
        for (const r of rows) {
            const btns = [...r.querySelectorAll(".h3d-lref-copy")];
            assert.strictEqual(btns.length, 1,
                "这一行该有且只有一个复制按钮：" + r.querySelector(".h3d-lref-title").textContent);
        }
        /* 反面：不许出现一个「复制全部」把 140 条一起塞进剪贴板 */
        assert.ok(overlay.textContent.indexOf("复制全部") < 0,
            "出现了「复制全部」—— 用户要的是挑一条粘");
    });

    await t("★ 点复制拿到的是命令**原文**（多行含换行）", async () => {
        const rows = [...overlay.querySelectorAll(".h3d-lref-row")];
        const byTitle = (tt) => rows.find((r) =>
            r.querySelector(".h3d-lref-title").textContent === tt);

        copied = [];
        byTitle("最小启动").querySelector(".h3d-lref-copy")
            .dispatchEvent(new w.Event("click"));
        await new Promise((r) => setTimeout(r, 0));
        /* 复制的是 `show`（只含参数），不是带 `python main.py` 的 `cmd` */
        assert.deepStrictEqual(copied, [MARK_A], "单行命令复制内容不对：" + copied);

        copied = [];
        byTitle("service 文件内容").querySelector(".h3d-lref-copy")
            .dispatchEvent(new w.Event("click"));
        await new Promise((r) => setTimeout(r, 0));
        assert.deepStrictEqual(copied, [MARK_B],
            "多行命令复制时丢了换行（会直接粘坏）：" + JSON.stringify(copied));
    });

    await t("命令块是 <pre>（保留换行），说明在它下面另起一段", () => {
        const pre = overlay.querySelector(".h3d-lref-cmd");
        assert.ok(pre && pre.tagName === "PRE", "命令该放在 <pre> 里");
        const row = pre.closest(".h3d-lref-row");
        const hints = [...row.querySelectorAll(".h3d-perf-hint")];
        assert.ok(hints.length >= 2, "why + note 该是两段");
        /* ⚠ 不再有「为什么要这样：」标签，说明就是普通一段（2026-09-27 改） */
        assert.ok(hints[0].textContent.indexOf("只监听本机") >= 0,
            "第一段该是 why 的内容，实际：" + hints[0].textContent);
        assert.ok(hints[0].querySelector("b"), "why 里的 **强调** 该转成真加粗");
        assert.ok(hints[0].innerHTML.indexOf("**") < 0, "不该露着字面星号");
        assert.ok(hints[1].classList.contains("h3d-lref-warn"), "note 该用警示色那一段");
    });

    await t("★ 有 .txt 导出，且指向后端路由（不在前端拼第二份副本）", () => {
        const dl = [...overlay.querySelectorAll(".h3d-perf-foot .h3d-btn")]
            .find((b) => b.textContent.indexOf(".txt") >= 0);
        assert.ok(dl, "底栏缺 .txt 下载按钮");
        dl.dispatchEvent(new w.Event("click"));
        assert.strictEqual(openedUrl, "/h3chain/launch_ref.txt",
            "下载地址不对（应为后端 txt 路由）：" + openedUrl);
    });

    await t("跳转条：每个分区一个入口，点击不报错", () => {
        const items = [...overlay.querySelectorAll(".h3d-lref-toc-i")];
        assert.strictEqual(items.length, REF.sections.length, "跳转条条数不对");
        items[1].dispatchEvent(new w.Event("click"));   // jsdom 无 scrollIntoView 也要不抛
    });

    await t("底栏状态把「参数已逐个核对」写出来（这是这份文档的信任来源）", () => {
        const st = overlay.querySelector(".h3d-perf-status").textContent;
        assert.ok(st.indexOf("72") >= 0, "状态行该报参数个数：" + st);
        assert.ok(st.indexOf("核对") >= 0, "状态行该说明参数已核对：" + st);
    });

    await t("接口不可用时不白屏，给出可读提示", async () => {
        const r2 = load({ H3Api: {} });
        await r2.w.openLaunchRef();
        const box = r2.w.document.querySelector(".h3d-lref-overlay");
        assert.ok(box, "连弹窗都没开");
        assert.ok(box.textContent.indexOf("接口不可用") >= 0,
            "该提示接口不可用，实际：" + box.textContent.slice(0, 60));
    });

    await t("Esc 能关掉弹窗", () => {
        overlay.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        assert.ok(!w.document.querySelector(".h3d-lref-overlay"), "Esc 没关掉");
    });

    w.HTMLAnchorElement.prototype.click = origClick;

    results.forEach((r) => console.log(r));
    if (errors.length) { console.log("\n页面错误: " + errors.join(" | ")); ok = false; }
    console.log(ok ? "\nlaunch_ref_check 全部通过" : "\nlaunch_ref_check 失败");
    process.exit(ok ? 0 : 1);
})();
