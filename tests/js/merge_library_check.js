/* 合并模式重构（R7）守卫：**点击顺序 = 合并顺序**，且在素材库里选。
 *
 * 背景（为什么要改）：合并模式原先藏在状态条的 `if (done > 0)` 里 —— 一段都没
 * 生成时根本看不到入口；选素材的方式是"回到段卡上勾分段"，而段卡是**链**的视图，
 * 不是素材的视图（成片/全局库里的东西根本没段号可选）。现在：
 *   · 状态条按钮常显；
 *   · 到**素材库**点素材，点击顺序就是拼接顺序，瓦片上画 1/2/3/4 角标；
 *   · 退出合并模式立即清空清单。
 *
 * 本文件钉这几件事（都是容易悄悄坏掉、坏了不报错的地方）：
 *   ① 素材库在 merge 上下文里：标题/提示变、批量区换成合并区、老批量区不出现；
 *   ② 点瓦片 = 排进清单，**数组顺序就是顺序**（不许排序），再点一次移出且角标重排；
 *   ③ 角标登记进 tileKeepers()：缩略图懒加载 replaceChildren 后角标必须还在
 *      （漏登记 = 滚动一次角标整批消失，本仓库踩过这个坑）；
 *   ④ 「⧉ 开始合并」把清单交回导演台（onMergeCommit），空清单不许提交；
 *   ⑤ 导演台：段卡上的勾选框已删、互斥守卫只看"合并导出进行中"（不看合并模式开关）。
 *
 * 用法：node tests/js/merge_library_check.js
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { ROOT } = require("./_harness");

let JSDOM;
try {
    ({ JSDOM } = require("jsdom"));
} catch (e) {
    console.error("SKIP: 未安装 jsdom（npm install）");
    process.exit(2);
}

const LIB_SRC = fs.readFileSync(path.join(ROOT, "web", "h3_library.js"), "utf8");
const DIR_SRC = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8");

/* 三个不同 scope 的素材：合并清单要能混着排（顺序就是点击顺序）。 */
const ITEMS = [
    { id: "finals:finals/a.mp4", scope: "finals", kind: "video", name: "A", file: "finals/a.mp4" },
    { id: "global:video/b.mp4", scope: "global", kind: "video", name: "B", file: "video/b.mp4" },
    { id: "project:assets/c.mp4", scope: "project", kind: "video", name: "C", file: "assets/c.mp4" },
];

function mkWin(opts) {
    const o = opts || {};
    const dom = new JSDOM("<!doctype html><html><body></body></html>", {
        runScripts: "outside-only", url: "http://127.0.0.1:8188/",
        pretendToBeVisual: true,
    });
    const w = dom.window;
    const errors = [];
    w.addEventListener("error", (e) => errors.push(String(e.error || e.message)));
    /* 抓住 IntersectionObserver 的回调：懒加载"离屏卸载"是同步回调，
     * 手工喂一条 entry 就能验角标有没有被 replaceChildren 抹掉。 */
    const observers = [];
    w.IntersectionObserver = class {
        constructor(cb, opts2) { this.cb = cb; this.opts = opts2; observers.push(this); }
        observe() {} unobserve() {} disconnect() {}
    };
    const merged = [];
    const committed = [];
    w.H3Api = {
        async libList() {
            return { body: { ok: true, counters: {},
                data: { items: ITEMS, total: ITEMS.length, total_pages: 1, page: 1 } } };
        },
        async libStatus() { return { body: { ok: true, counters: {} } }; },
        async libCollections() { return { body: { ok: true, collections: [] } }; },
        libRawUrl: () => "raw://x",
        libThumbUrl: () => "thumb://x",
        errText: (r, d) => d || "err",
    };
    w.eval(LIB_SRC);
    return { w, errors, observers, merged, committed };
}

let ok = true;
const results = [];
async function t(name, fn) {
    try { await fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}

const tilesOf = (w) => [...w.document.querySelectorAll(".h3l-tile")];
const badgeOf = (w, i) => {
    const b = tilesOf(w)[i].querySelector(".h3l-order");
    return b ? b.textContent : null;
};
const click = (w, n) => tilesOf(w)[n].dispatchEvent(new w.MouseEvent("click", { bubbles: true }));

(async () => {
    console.log("\n== 合并模式（素材库侧） ==");

    await t("merge 上下文：overlay 带 h3l-merging、标题与提示都换了", async () => {
        const { w } = mkWin();
        await w.H3Lib.open({ dir: "PROJ", merge: true, order: [],
            onMergeChanged: (x) => { w.__merged = x; } });
        const ov = w.document.querySelector(".h3l-overlay");
        assert.ok(ov, "素材库没打开");
        assert.ok(ov.classList.contains("h3l-merging"), "缺 h3l-merging 标记");
        assert.ok(ov.querySelector(".h3l-head strong").textContent.indexOf("选择要合并的素材") >= 0,
            "标题没换：" + ov.querySelector(".h3l-head strong").textContent);
        assert.ok(ov.querySelector(".h3l-hint").textContent.indexOf("角标") >= 0,
            "没告诉用户角标就是顺序");
    });

    await t("合并模式下不摆批量区，摆的是合并区（调入项目/删除跟拼接无关）", async () => {
        const { w } = mkWin();
        await w.H3Lib.open({ dir: "PROJ", merge: true });
        const ov = w.document.querySelector(".h3l-overlay");
        const boxes = [...ov.querySelectorAll(".h3l-batch")];
        assert.strictEqual(boxes.length, 1, "工具条上该只有一个区，实际 " + boxes.length);
        assert.ok(boxes[0].classList.contains("h3l-mergebox"), "那个区该是合并区");
        assert.ok(ov.querySelector(".h3l-mergebox button").textContent.indexOf("开始合并") >= 0,
            "缺「开始合并」按钮");
        for (const w2 of ["调入项目", "存入全局库", "删除"]) {
            assert.ok(ov.querySelector(".h3l-bar").textContent.indexOf(w2) < 0,
                "合并模式不该出现批量动作：" + w2);
        }
    });

    await t("非合并模式仍是批量区（R4 的行为不许被改掉）", async () => {
        const { w } = mkWin();
        await w.H3Lib.open({ dir: "PROJ" });
        const ov = w.document.querySelector(".h3l-overlay");
        assert.ok(!ov.classList.contains("h3l-merging"), "普通模式不该带 merging 标记");
        assert.ok(!ov.querySelector(".h3l-mergebox"), "普通模式不该有合并区");
        assert.ok(ov.querySelector(".h3l-batch"), "普通模式该有批量区");
        assert.ok(ov.querySelector(".h3l-bar").textContent.indexOf("调入项目") >= 0, "批量区内容丢了");
    });

    await t("点击顺序 = 合并顺序：角标 1/2/3 与清单顺序一致", async () => {
        const { w } = mkWin();
        const got = [];
        await w.H3Lib.open({ dir: "PROJ", merge: true,
            onMergeChanged: (x) => got.push(x.map((i) => i.id).join(">")) });
        assert.strictEqual(badgeOf(w, 0), null, "还没点就有角标");
        click(w, 1);            // 先点 B
        click(w, 2);            // 再点 C
        click(w, 0);            // 最后点 A
        assert.strictEqual(badgeOf(w, 1), "1", "B 该是第 1 个");
        assert.strictEqual(badgeOf(w, 2), "2", "C 该是第 2 个");
        assert.strictEqual(badgeOf(w, 0), "3", "A 该是第 3 个");
        assert.strictEqual(got[got.length - 1],
            "global:video/b.mp4>project:assets/c.mp4>finals:finals/a.mp4",
            "回调拿到的顺序不对：" + got[got.length - 1]);
        assert.ok(w.document.querySelector(".h3l-foot").textContent.indexOf("已排 3 个") >= 0,
            "页脚没回显数量");
    });

    await t("再点一次 = 移出，后面的角标整体前移（1/2 重排）", async () => {
        const { w } = mkWin();
        await w.H3Lib.open({ dir: "PROJ", merge: true });
        click(w, 0); click(w, 1); click(w, 2);
        click(w, 1);            // 移出 B
        assert.strictEqual(badgeOf(w, 0), "1", "A 该前移成 1");
        assert.strictEqual(badgeOf(w, 2), "2", "C 该前移成 2");
        assert.strictEqual(badgeOf(w, 1), null, "B 的角标该消失");
        assert.ok(!tilesOf(w)[1].classList.contains("sel"), "移出后不该还是选中态");
    });

    await t("角标登记进 tileKeepers：懒加载离屏卸载后角标还在", async () => {
        const { w, observers } = mkWin();
        await w.H3Lib.open({ dir: "PROJ", merge: true });
        click(w, 2);
        const th = tilesOf(w)[2].querySelector(".h3l-thumb");
        assert.ok(th.querySelector(".h3l-order"), "点击后该有角标");
        /* 造一个"已加载"的缩略图，再喂一条"离开视野"——真实链路里这里会
         * replaceChildren(...keep)，漏登记角标就被抹掉。 */
        th.append(w.document.createElement("img"));
        const io = observers[observers.length - 1];
        io.cb([{ target: th, isIntersecting: false }], io);
        assert.strictEqual(th.querySelector("img"), null, "离屏该把 img 摘掉");
        const b = th.querySelector(".h3l-order");
        assert.ok(b, "角标被 replaceChildren 抹掉了 —— 忘了登记进 tileKeepers()");
        assert.strictEqual(b.textContent, "1", "角标数字也不该变");
    });

    await t("「⧉ 开始合并」把清单交回导演台；空清单只提示、不提交", async () => {
        const { w } = mkWin();
        let committed = null;
        await w.H3Lib.open({ dir: "PROJ", merge: true,
            onMergeCommit: (x) => { committed = x; } });
        const btn = w.document.querySelector(".h3l-mergebox button");
        btn.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        assert.strictEqual(committed, null, "空清单不该提交");
        click(w, 2); click(w, 0);
        btn.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        assert.ok(Array.isArray(committed), "点了开始合并却没交回清单");
        /* ⚠ 必须 Array.from：committed 是 jsdom 窗口里的 Array，原型与 node 的不是
         * 同一个，deepStrictEqual 会因为「原型不同」判不等（内容一模一样也挂）。 */
        assert.deepStrictEqual(Array.from(committed, (x) => x.id),
            ["project:assets/c.mp4", "finals:finals/a.mp4"], "交回的顺序不对");
        assert.ok(btn.textContent.indexOf("1→2") >= 0, "按钮文案该带上顺序长度：" + btn.textContent);
    });

    await t("「✕ 清空顺序」清干净且通知导演台（不整片重画，滚动位置不跳）", async () => {
        const { w } = mkWin();
        const got = [];
        await w.H3Lib.open({ dir: "PROJ", merge: true,
            onMergeChanged: (x) => got.push(x.length) });
        click(w, 0); click(w, 1);
        const clear = [...w.document.querySelectorAll(".h3l-mergebox button")][1];
        clear.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        assert.strictEqual(badgeOf(w, 0), null, "角标没清");
        assert.strictEqual(badgeOf(w, 1), null, "角标没清");
        assert.strictEqual(got[got.length - 1], 0, "没通知导演台清单已空");
    });

    await t("重开素材库能带回已排的清单与角标（清单活在导演台那边）", async () => {
        const { w } = mkWin();
        await w.H3Lib.open({ dir: "PROJ", merge: true });
        click(w, 2);
        const order = [{ id: ITEMS[2].id, file: ITEMS[2].file, name: ITEMS[2].name,
            scope: ITEMS[2].scope, kind: ITEMS[2].kind }];
        w.document.querySelector(".h3l-overlay").remove();
        await w.H3Lib.open({ dir: "PROJ", merge: true, order });
        assert.strictEqual(badgeOf(w, 2), "1", "重开后角标没回来");
    });

    await t("合并模式下双击仍能预览（挑片子前要能看一眼内容）", () => {
        /* 挑选模式不能开双击（单击就选中并关窗）；合并模式**必须**开 ——
         * 否则用户只能靠文件名猜哪个是哪个。 */
        assert.ok(LIB_SRC.indexOf('if (!S.pick && !S.merge) openViewer') < 0,
            "合并模式又把双击预览禁掉了");
        assert.ok(/addEventListener\("dblclick", \(\) => \{ if \(!S\.pick\) openViewer\(it\); \}\)/
            .test(LIB_SRC), "双击预览的守卫变了，请确认合并模式仍可预览");
    });

    console.log("\n== 合并模式（导演台侧，源码契约） ==");

    await t("段卡上的「外面选分段」已删干净（checkbox 与死样式都不在）", () => {
        /* ⚠ 判据用「CSS 规则 / 类名赋值」而不是纯字符串：删除处的墓碑注释里
         * 必然写着这些类名（.h3d-mergecb / .mergeable-off 都留了说明），
         * 拿 indexOf 断言只会命中注释，变成假失败。 */
        for (const re of [/\.h3d-mergecb\s*\{/, /\.mergeable-off\s*\{/,
            /\.h3d-card\.mergeable\s*[,{]/]) {
            assert.ok(!re.test(DIR_SRC), "又出现了已删的合并勾选样式：" + re);
        }
        for (const cls of ['"h3d-mergecb"', '"mergeable-off"', '"mergeable"']) {
            assert.ok(DIR_SRC.indexOf(cls) < 0, "又出现了已删的合并勾选类名：" + cls);
        }
    });

    await t("合并模式按钮常显（不再被 done > 0 包着）", () => {
        const i = DIR_SRC.indexOf("const mergeBtn = el(\"button\", \"h3d-btn\"");
        assert.ok(i > 0, "找不到状态条合并按钮");
        /* 往上找 600 字符：这段不该再被 `if (done > 0)` 之类的条件裹住 */
        const before = DIR_SRC.slice(Math.max(0, i - 600), i);
        assert.ok(before.indexOf("if (done > 0)") < 0,
            "合并按钮又被 done>0 包起来了 —— 一段没生成时用户就看不到入口");
    });

    await t("互斥守卫只看「合并导出进行中」，不看合并模式开关", () => {
        /* 常显之后"开着但清单为空"是常态：拿 mergeSel.on 当守卫会变成
         * "随手开了个合并模式，结果生成按钮点了没反应"。 */
        const hits = [...DIR_SRC.matchAll(/if \(mergeSel\.running\) \{\s*alert\("合并导出进行中/g)];
        assert.strictEqual(hits.length, 4,
            "该有 4 处守卫（提交生成 / 提交重摇 / 标记重摇 / 二采提交），实际 " + hits.length);
        assert.ok(DIR_SRC.indexOf("合并模式进行中") < 0, "旧的「合并模式进行中」守卫又回来了");
    });

    await t("退出合并模式立即清空清单（resetMergeSel 挂在退出与成功两条路上）", () => {
        const resets = [...DIR_SRC.matchAll(/resetMergeSel\(\)/g)];
        assert.ok(resets.length >= 3, "resetMergeSel 调用点太少：" + resets.length);
        assert.ok(/function resetMergeSel\(\) \{[\s\S]{0,400}?mergeSel\.running = false;/
            .test(DIR_SRC), "resetMergeSel 该把 running 也放下来");
    });

    await t("合并导出按清单顺序组装，且不再 sort（顺序就是数据）", () => {
        const i = DIR_SRC.indexOf("async function doMergeExport(btn)");
        assert.ok(i > 0, "找不到 doMergeExport");
        const body = DIR_SRC.slice(i, i + 1800);
        assert.ok(body.indexOf("mergeSel.order") >= 0, "没按 mergeSel.order 组装");
        assert.ok(body.indexOf("asset: String(x.id") >= 0, "清单项没带 asset id（全局库素材就解析不了）");
        assert.ok(body.indexOf(".sort(") < 0, "又给合并顺序排序了");
        assert.ok(body.indexOf("mergeSel.running = true") >= 0, "导出期间没立互斥标记");
    });

    results.forEach((r) => console.log(r));
    console.log(ok ? "\nmerge_library_check 全部通过" : "\nmerge_library_check 失败");
    process.exit(ok ? 0 : 1);
})();
