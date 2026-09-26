/* 合并导出（R7 定稿：**整套住在素材库里**）守卫。
 *
 * 定稿口径（2026-09-27 用户拍板，别改）：
 *   · 合并**只在素材库出现**，导演台一个入口都不留 —— 同一个功能两处入口、
 *     两份清单状态，必然漂移成"我在这儿选好了，那边却显示没选"；
 *   · 素材库工具条有专门按钮 `⧉ 合并导出` 进选材模式；点素材排顺序，
 *     瓦片角标 1→N 就是拼接顺序；
 *   · **只收视频**：进选材模式即把类型筛选强制成 video，非视频瓦片标灰且点不动
 *     （混进清单的话后端会把整单拒掉，用户看到的是莫名其妙的整体失败）；
 *   · 退出合并模式（或合并成功）立即清空清单；
 *   · 导演台只剩一个互斥标记 `window.H3Merge`：拼接期间挡住生成按钮。
 *
 * 本文件用**真 h3_api.js + 真 h3_library.js** 跑（jsdom 里 stub 掉
 * `comfyAPI.api.api.fetchApi`），所以顺带钉住了错误回显那条：后端 `_err` 的字段名是
 * `message`，不是 `error` —— 读错字段会让所有失败都只剩一句"HTTP 400"。
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

const API_SRC = fs.readFileSync(path.join(ROOT, "web", "h3_api.js"), "utf8");
const LIB_SRC = fs.readFileSync(path.join(ROOT, "web", "h3_library.js"), "utf8");
const DIR_SRC = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8");

/* 三个不同 scope 的视频 + 一张图片（图片用来验"合并只收视频"这道闸）。 */
const V_A = { id: "finals:finals/a.mp4", scope: "finals", kind: "video", name: "A", file: "finals/a.mp4" };
const V_B = { id: "global:video/b.mp4", scope: "global", kind: "video", name: "B", file: "video/b.mp4" };
const V_C = { id: "project:assets/c.mp4", scope: "project", kind: "video", name: "C", file: "assets/c.mp4" };
const IMG = { id: "project:assets/pic.png", scope: "project", kind: "image", name: "PIC", file: "assets/pic.png" };

/** 装一个 jsdom 环境：真 h3_api.js（走 stub 的 fetchApi）+ 真 h3_library.js。 */
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

    const calls = [];
    const mergeReplies = [];
    /* lib_list 的返回：**真的按 kind 过滤**（后端就是这么做的）。
     * `leakKinds` 用来模拟"筛选器被改回 all"这类漏网，验前端的第二道闸。 */
    const allItems = () => [V_A, V_B, V_C, IMG];
    const route = (p, body) => {
        calls.push({ path: String(p || ""), body });
        const q = new URLSearchParams(String(p).split("?")[1] || "");
        if (p.indexOf("/h3chain/lib_list") >= 0) {
            const kind = q.get("kind") || "all";
            let items = allItems();
            if (!o.leakKinds && kind !== "all") items = items.filter((x) => x.kind === kind);
            return { status: 200, body: { ok: true, counters: {},
                data: { items, total: items.length, total_pages: 1, page: 1 } } };
        }
        if (p.indexOf("/h3chain/lib_status") >= 0) {
            return { status: 200, body: { ok: true, counters: {} } };
        }
        if (p.indexOf("/h3chain/lib_collections") >= 0) {
            return { status: 200, body: { ok: true, collections: [] } };
        }
        if (p.indexOf("/h3chain/merge") >= 0) {
            return mergeReplies.shift()
                || { status: 200, body: { ok: true, file: "finals/merged_x.mp4" } };
        }
        return { status: 200, body: { ok: true } };
    };
    w.comfyAPI = { api: { api: { fetchApi: async (p, init) => {
        const body = init && init.body ? JSON.parse(String(init.body)) : null;
        const r = route(String(p || ""), body);
        return { status: r.status, ok: r.status < 400, json: async () => r.body };
    } } } };

    w.eval(API_SRC);            // 真 H3Api（含真 errText）
    w.eval(LIB_SRC);            // 真 H3Lib
    return { w, errors, observers, calls, mergeReplies };
}

let ok = true;
const results = [];
async function t(name, fn) {
    try { await fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}

const ov = (w) => w.document.querySelector(".h3l-overlay");
const tilesOf = (w) => [...ov(w).querySelectorAll(".h3l-tile")];
const badgeOf = (w, i) => {
    const b = tilesOf(w)[i].querySelector(".h3l-order");
    return b ? b.textContent : null;
};
const clickTile = (w, n) => tilesOf(w)[n]
    .dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
const clickBtn = (w, sel) => ov(w).querySelector(sel)
    .dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
const tick = () => new Promise((r) => setTimeout(r, 30));
/** 素材库里"合并导出"入口（普通模式工具条上的那个 CTA）。 */
const entryBtn = (w) => [...ov(w).querySelectorAll(".h3l-bar .h3l-only-normal")]
    .find((n) => n.tagName === "BUTTON" && n.textContent.indexOf("合并导出") >= 0);
const mergeBtn = (w) => [...ov(w).querySelectorAll(".h3l-mergebox button")]
    .find((b) => b.textContent.indexOf("开始合并") >= 0);
const vis = (n) => (n ? n.offsetParent !== null || n.getClientRects().length > 0 : false);

(async () => {
    console.log("\n== 合并导出：入口在素材库（不在导演台） ==");

    await t("导演台一个合并入口都不留（按钮 / 清单条 / 勾选框全删）", () => {
        /* ⚠ 判据一律带「调用括号」或「CSS 规则」：删除处的墓碑注释里写着这些名字
         * （"openMergeLibrary / doMergeExport 已删…"），拿 indexOf 裸字符串断言
         * 只会命中注释，变成假失败。 */
        for (const gone of ["h3d-mergecb", "mergeable-off"]) {
            assert.ok(DIR_SRC.indexOf('"' + gone + '"') < 0, "导演台又出现已删的合并勾选：" + gone);
        }
        for (const re of [/\.h3d-mergebar\s*\{/, /\.h3d-merge-sum\s*\{/, /\.h3d-mergecb\s*\{/]) {
            assert.ok(!re.test(DIR_SRC), "导演台又出现已删的合并样式：" + re);
        }
        for (const fn of ["openMergeLibrary", "doMergeExport", "pickMergeVideo", "resetMergeSel",
            "mergeCount"]) {
            assert.ok(!new RegExp(fn + "\\s*\\(").test(DIR_SRC),
                "导演台又出现了已删的合并函数：" + fn);
        }
        assert.ok(DIR_SRC.indexOf("⧉ 合并模式") < 0, "状态条的「⧉ 合并模式」按钮又回来了");
        assert.ok(DIR_SRC.indexOf("const mergeSel") < 0, "导演台又在存合并清单了（mergeSel）");
    });

    await t("导演台只留互斥标记：window.H3Merge 暴露 begin/end/running", () => {
        assert.ok(/window\.H3Merge\s*=/.test(DIR_SRC), "没暴露 window.H3Merge");
        for (const m of ["begin()", "end(ok)"]) {
            assert.ok(DIR_SRC.indexOf(m) >= 0, "H3Merge 缺 " + m);
        }
        /* 只数**守卫**（带 alert 的那几处）：footer 里还有一处 `if (mergeJob.running)`
         * 是改按钮文案的，不是守卫，别一起数进来。 */
        const hits = [...DIR_SRC.matchAll(/if \(mergeJob\.running\) \{ alert\(/g)];
        assert.strictEqual(hits.length, 4,
            "该有 4 处互斥守卫（二采提交 / 提交生成 / 提交重摇 / 标记重摇），实际 " + hits.length);
    });

    await t("普通模式：工具条上有专门的「⧉ 合并导出」按钮，批量区也在", async () => {
        const { w } = mkWin();
        await w.H3Lib.open({ dir: "PROJ" });
        const b = entryBtn(w);
        assert.ok(b, "找不到「⧉ 合并导出」入口按钮");
        assert.ok(ov(w).querySelector(".h3l-batch:not(.h3l-mergebox)"), "普通模式该有批量区");
        assert.ok(ov(w).querySelector(".h3l-kindsel, select"), "普通模式该有类型下拉");
        assert.strictEqual(w.document.querySelectorAll(".h3l-mergebox").length, 1, "合并区该在 DOM 里");
    });

    await t("点入口进选材模式：标题换、批量区藏、合并区出、latent 页签消失", async () => {
        const { w } = mkWin();
        await w.H3Lib.open({ dir: "PROJ" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        assert.ok(ov(w).classList.contains("h3l-merging"), "overlay 没带 h3l-merging");
        assert.ok(ov(w).querySelector(".h3l-head strong").textContent
            .indexOf("选择要合并的素材") >= 0, "标题没换");
        assert.ok(ov(w).querySelector(".h3l-hint").textContent.indexOf("角标") >= 0,
            "没告诉用户角标就是顺序");
        assert.ok(mergeBtn(w), "缺「开始合并」按钮");
        assert.ok(ov(w).querySelector(".h3l-bar").textContent.indexOf("调入项目") >= 0,
            "批量区被删了（只是该被 CSS 藏起来）");
        const scopeNames = [...ov(w).querySelectorAll(".h3l-scope")].map((n) => n.textContent);
        assert.ok(!scopeNames.some((s) => s.indexOf("latent") >= 0),
            "合并模式下 latent 页签该消失（类型被强制成 video，点进去必然为空）：" + scopeNames);
    });

    await t("只收视频：类型筛选被强制成 video，图片根本不出现在列表里", async () => {
        const { w, calls } = mkWin();
        await w.H3Lib.open({ dir: "PROJ" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        const last = calls.filter((c) => c.path.indexOf("lib_list") >= 0).pop();
        assert.ok(last && last.path.indexOf("kind=video") >= 0,
            "进合并模式后没把类型筛选切成 video：" + (last && last.path));
        const names = tilesOf(w).map((t) => t.querySelector(".h3l-name").textContent);
        assert.ok(names.indexOf("PIC") < 0, "图片混进合并列表了：" + names);
    });

    await t("第二道闸：漏网的图片点不动，且给出人话（不许悄悄进清单）", async () => {
        const { w } = mkWin({ leakKinds: true });   // 模拟筛选器失效，图片混进列表
        await w.H3Lib.open({ dir: "PROJ" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        const pic = tilesOf(w).find((t) => t.querySelector(".h3l-name").textContent === "PIC");
        assert.ok(pic, "（桩没造出图片瓦片，用例失效）");
        assert.ok(pic.classList.contains("h3l-nomerge"), "非视频瓦片该标成不可选");
        pic.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        assert.strictEqual(pic.querySelector(".h3l-order"), null, "图片被排进清单了");
        assert.ok(ov(w).querySelector(".h3l-msg").textContent.indexOf("只能合并视频") >= 0,
            "点非视频该给一句说明：" + ov(w).querySelector(".h3l-msg").textContent);
    });

    await t("点击顺序 = 合并顺序：角标 1/2/3 与清单顺序一致", async () => {
        const { w } = mkWin();
        await w.H3Lib.open({ dir: "PROJ" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        assert.strictEqual(badgeOf(w, 0), null, "还没点就有角标");
        clickTile(w, 1);            // 先点 B
        clickTile(w, 2);            // 再点 C
        clickTile(w, 0);            // 最后点 A
        assert.strictEqual(badgeOf(w, 1), "1", "B 该是第 1 个");
        assert.strictEqual(badgeOf(w, 2), "2", "C 该是第 2 个");
        assert.strictEqual(badgeOf(w, 0), "3", "A 该是第 3 个");
        assert.ok(ov(w).querySelector(".h3l-foot").textContent.indexOf("已排 3 个") >= 0,
            "页脚没回显数量");
        assert.ok(mergeBtn(w).textContent.indexOf("1→3") >= 0,
            "按钮文案该带上顺序长度：" + mergeBtn(w).textContent);
    });

    await t("再点一次 = 移出，后面的角标整体前移（1/2 重排）", async () => {
        const { w } = mkWin();
        await w.H3Lib.open({ dir: "PROJ" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        clickTile(w, 0); clickTile(w, 1); clickTile(w, 2);
        clickTile(w, 1);            // 移出 B
        assert.strictEqual(badgeOf(w, 0), "1", "A 该前移成 1");
        assert.strictEqual(badgeOf(w, 2), "2", "C 该前移成 2");
        assert.strictEqual(badgeOf(w, 1), null, "B 的角标该消失");
        assert.ok(!tilesOf(w)[1].classList.contains("sel"), "移出后不该还是选中态");
    });

    await t("角标登记进 tileKeepers：懒加载离屏卸载后角标还在", async () => {
        const { w, observers } = mkWin();
        await w.H3Lib.open({ dir: "PROJ" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        clickTile(w, 2);
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

    await t("退出合并模式：清单与角标立即清空、类型筛选还原、latent 页签回来", async () => {
        const { w } = mkWin();
        await w.H3Lib.open({ dir: "PROJ" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        clickTile(w, 0); clickTile(w, 1);
        clickBtn(w, ".h3l-mergebox button:not(.h3l-btn-cta)");
        await tick();
        assert.ok(!ov(w).classList.contains("h3l-merging"), "没退出合并模式");
        assert.strictEqual(badgeOf(w, 0), null, "角标没清");
        assert.strictEqual(badgeOf(w, 1), null, "角标没清");
        assert.ok(ov(w).querySelector(".h3l-head strong").textContent.indexOf("素材库") >= 0,
            "标题没还原");
        const scopeNames = [...ov(w).querySelectorAll(".h3l-scope")].map((n) => n.textContent);
        assert.ok(scopeNames.some((s) => s.indexOf("latent") >= 0), "latent 页签没还原");
    });

    await t("空清单点「开始合并」只提示、不发请求", async () => {
        const { w, calls } = mkWin();
        await w.H3Lib.open({ dir: "PROJ" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        mergeBtn(w).dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        await tick();
        assert.strictEqual(calls.filter((c) => c.path.indexOf("merge") >= 0).length, 0,
            "空清单不该发合并请求");
        assert.ok(ov(w).querySelector(".h3l-msg").textContent.indexOf("先点") >= 0,
            "该提示先选素材");
    });

    await t("发起合并：请求体按角标顺序、带 asset id，且不排序", async () => {
        const { w, calls } = mkWin();
        await w.H3Lib.open({ dir: "PROJ" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        clickTile(w, 2); clickTile(w, 0);      // C 在前、A 在后
        mergeBtn(w).dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        await tick();
        const m = calls.filter((c) => c.path.indexOf("/h3chain/merge") >= 0).pop();
        assert.ok(m, "没发合并请求");
        assert.strictEqual(m.body.dir, "PROJ", "没带项目目录");
        assert.deepStrictEqual(Array.from(m.body.items, (x) => x.asset),
            ["project:assets/c.mp4", "finals:finals/a.mp4"],
            "顺序或 asset id 不对（顺序就是数据，不许排序）");
    });

    await t("合并成功：清清单 / 退模式 / 跳到「成片」/ 放开互斥（end(true)）", async () => {
        const { w, calls } = mkWin();
        const life = [];
        w.H3Merge = { begin: () => life.push("begin"), end: (o) => life.push("end:" + o) };
        await w.H3Lib.open({ dir: "PROJ" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        clickTile(w, 0);
        mergeBtn(w).dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        await tick(); await tick();
        assert.deepStrictEqual(life, ["begin", "end:true"], "互斥标记没配对："
            + JSON.stringify(life));
        assert.ok(!ov(w).classList.contains("h3l-merging"), "成功后该退出选材模式");
        assert.strictEqual(badgeOf(w, 0), null, "成功后清单该清空");
        const lastList = calls.filter((c) => c.path.indexOf("lib_list") >= 0).pop();
        assert.ok(lastList.path.indexOf("scope=finals") >= 0,
            "成功后该跳到「成片」看产物：" + lastList.path);
        assert.ok(ov(w).querySelector(".h3l-msg").textContent.indexOf("已合并") >= 0,
            "没有成功回执：" + ov(w).querySelector(".h3l-msg").textContent);
    });

    /* ★ 这条钉的是真 bug：后端 `_err` 的字段名是 `message`，前端曾读 `error`，
     * 于是所有失败都只剩一句"HTTP 400"，真正的原因（素材没了 / 不是视频 /
     * 路径越界）全被吞掉 —— 用户看到的正是截图里那个无语的弹窗。 */
    await t("合并失败：显示后端原话（message），不是「HTTP 400」", async () => {
        const { w, mergeReplies } = mkWin();
        mergeReplies.push({ status: 400, body: { ok: false, code: "BAD_REQUEST",
            message: "只能合并视频：pic.png 是图片（音频/图片/latent 拼接没有意义）" } });
        const life = [];
        w.H3Merge = { begin: () => life.push("begin"), end: (o) => life.push("end:" + o) };
        await w.H3Lib.open({ dir: "PROJ" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        clickTile(w, 0);
        mergeBtn(w).dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        await tick(); await tick();
        const txt = ov(w).querySelector(".h3l-msg").textContent;
        assert.ok(txt.indexOf("只能合并视频") >= 0, "后端原话没显示出来：" + txt);
        assert.ok(txt.indexOf("HTTP 400") < 0, "又只剩一句 HTTP 400 了：" + txt);
        assert.deepStrictEqual(life, ["begin", "end:false"], "失败也要放开互斥，否则生成按钮永远点不动");
        assert.ok(ov(w).classList.contains("h3l-merging"), "失败不该退出选材模式（清单还在，方便重试）");
    });

    await t("h3_api 里 libMerge 指向 /h3chain/merge，且 errText 优先读 message", () => {
        assert.ok(/libMerge:\s*\(dir, items\)\s*=>\s*_json\("POST",\s*"\/h3chain\/merge"/.test(API_SRC),
            "libMerge 没接 /h3chain/merge");
        const i = API_SRC.indexOf("function errText(");
        const body = API_SRC.slice(i, i + 400);
        assert.ok(body.indexOf("body?.message") >= 0 && body.indexOf("body?.error") >= 0,
            "errText 该先读 message、再回落 error");
        assert.ok(body.indexOf("body?.message") < body.indexOf("body?.error"),
            "message 必须排在 error 前面（后端 _err 用的是 message）");
    });

    await t("独立打开（没有导演台）时用 onChanged 通知宿主", async () => {
        const { w } = mkWin();
        let changed = 0;
        await w.H3Lib.open({ dir: "PROJ", onChanged: () => { changed++; } });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        clickTile(w, 0);
        mergeBtn(w).dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        await tick(); await tick();
        assert.strictEqual(changed, 1, "没有 H3Merge 时该走 onChanged 通知宿主");
    });

    await t("没有项目目录时进不去合并模式（产物要有地方落）", async () => {
        const { w } = mkWin();
        await w.H3Lib.open({ dir: "" });
        clickBtn(w, ".h3l-only-normal.h3l-btn-cta");
        await tick();
        assert.ok(!ov(w).classList.contains("h3l-merging"), "没项目目录不该进合并模式");
        assert.ok(ov(w).querySelector(".h3l-msg").textContent.indexOf("先打开一个项目") >= 0,
            "该说清为什么进不去");
    });

    results.forEach((r) => console.log(r));
    console.log(ok ? "\nmerge_library_check 全部通过" : "\nmerge_library_check 失败");
    process.exit(ok ? 0 : 1);
})();
