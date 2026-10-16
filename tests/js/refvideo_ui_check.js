/* 🎬 参考视频缩放 —— 前端入口 / 弹窗 / 编号口径回归（node + jsdom，跑 shipped 源码）。
 *
 * 覆盖四件事：
 *   ① 入口位置：左栏「🗂 打开素材库」**正下方**（用户指定的位置），且摘要跟着设置走
 *   ② 弹窗：复用 ⚡ 性能优化那套壳（.h3d-opt-dialog / .h3d-perf-dialog / .h3d-perf-row
 *      两行式），控件齐全、保存按钮在拿到设置前是禁用的（读不到就别写）
 *   ③ 音轨开关**跨面板生效**：关掉「参考视频音频」后映射条里独立音频回到 <Audio 1>
 *      （后端 nodes._kind_tokens / asset_store.compile_refs / 这里三处同规则）
 *   ④ 保存契约：改动攒草稿，点「保存并应用」才 POST；恢复默认发空对象
 *
 * 用法：node tests/js/refvideo_ui_check.js
 */
const assert = require("assert");
const { load, mkNode } = require("./_harness.js");

let bad = 0;
function ok(cond, msg) {
    if (cond) { console.log("OK   " + msg); return; }
    bad++;
    console.log("FAIL " + msg);
}

const POOL = [
    { label: "片头", kind: "video", file: "videos/c.mp4", asset_id: "a_333333333333", mark: "视频1", roles: [] },
    { label: "BGM", kind: "audio", file: "audio/d.mp3", asset_id: "a_444444444444", mark: "音频1", roles: [] },
];

function mkDs(extra) {
    return Object.assign({
        mode: "分段", prompts: ["a"], segments: [{}], inserts: [], redo_segs: [],
        ref_assets: POOL, first_frame: "", end_frame: "", last_frame: "",
    }, extra || {});
}

(async () => {
    /* ================= ① 入口位置 + 摘要 ================= */
    {
        const calls = [];
        const node = mkNode(mkDs());
        const { w, errors } = load({
            node,
            H3Api: {
                async refvidGet() {
                    calls.push("get");
                    return { body: { ok: true, data: { on: false, ar: "16:9", mp: 0.98, audio: true },
                                     ar_ratio: ["21:9", "16:9", "9:16"], mp_options: [0.5, 0.98],
                                     fits: ["裁切填满", "补黑边", "拉伸"],
                                     resamples: ["lanczos", "area"], grids: ["向下", "就近"],
                                     limits: ["不限", "最长段", "自定义"] } };
                },
                async refvidSet(p) { calls.push(["set", p]); return { body: { ok: true, data: p.refvid } }; },
                async refvidPlan(p) { calls.push(["plan", p]); return { body: { ok: true, rows: [] } }; },
                async vpackGet() {
                    return { body: { ok: true, data: { on: false, ratio: "2/5" },
                                     ratios: ["0/5", "1/5", "2/5", "3/5"],
                                     default_ratio: "2/5" } };
                },
                async vpackSet(p) { calls.push(["vpset", p]); return { body: { ok: true, data: p.vpack } }; },
            },
        });
        ok(!errors.length, "装载期无异常（" + errors.join(" | ") + "）");

        await w.loadRefvid();
        const sec = w.document.createElement("div");
        w.renderV2Section(sec, { node, state: { dir: "PROJ" }, mf: { videos: [] }, ds: mkDs() });
        const btns = [...sec.querySelectorAll("button.h3d-hubbtn")];
        const texts = btns.map((b) => b.textContent);
        const iLib = texts.findIndex((t) => t.indexOf("打开素材库") >= 0);
        const iRv = texts.findIndex((t) => t.indexOf("参考视频缩放") >= 0);
        ok(iLib >= 0, "左栏有「打开素材库」按钮");
        ok(iRv === iLib + 1, "「参考视频缩放」紧跟在「打开素材库」正下方（顺序 " + iLib + "→" + iRv + "）");
        ok(texts[iRv].indexOf("关闭") >= 0 && texts[iRv].indexOf("透传") >= 0,
            "总开关关闭时摘要写「关闭 · 透传」，实际：" + texts[iRv]);
        ok(typeof w.openRefVideoSettings === "function", "openRefVideoSettings 是顶层函数");
    }

    /* ================= ② 弹窗结构 + 保存契约 ================= */
    {
        const calls = [];
        const vpCalls = [];
        const node = mkNode(mkDs());
        const { w } = load({
            node,
            H3Api: {
                async refvidGet() {
                    return { body: { ok: true, data: { on: true, ar: "16:9", mp: 0.98,
                                                       audio: true, fit: "裁切填满",
                                                       resample: "lanczos", grid: "向下",
                                                       limit: "不限", no_upscale: true,
                                                       fps_norm: true, limit_s: 0 },
                                     ar_ratio: ["21:9", "16:9", "9:16"], mp_options: [0.5, 0.98],
                                     fits: ["裁切填满", "补黑边", "拉伸"],
                                     resamples: ["lanczos", "area"], grids: ["向下", "就近"],
                                     limits: ["不限", "最长段", "自定义"] } };
                },
                async refvidSet(draft) { calls.push(draft); return { body: { ok: true, data: draft } }; },
                async refvidPlan(p) { return { body: { ok: true, rows: [
                    { label: "片头", ok: true,
                      report: ["参考视频「片头」1920×1080 @30fps 5.00s（150 帧） → 1344×768"] }] } }; },
                async vpackGet() {
                    return { body: { ok: true, data: { on: false, ratio: "2/5" },
                                     ratios: ["0/5", "1/5", "2/5", "3/5"],
                                     default_ratio: "2/5" } };
                },
                async vpackSet(draft) { vpCalls.push(draft); return { body: { ok: true, data: draft } }; },
            },
        });
        await w.openRefVideoSettings(node);
        const ov = w.document.querySelector(".h3d-refvid-overlay");
        ok(!!ov, "弹窗打开（.h3d-refvid-overlay）");
        ok(!!ov.querySelector(".h3d-opt-dialog") && !!ov.querySelector(".h3d-perf-dialog"),
            "复用 ⚡ 性能优化同一套外壳（opt-dialog + perf-dialog）");
        const rows = [...ov.querySelectorAll(".h3d-perf-row")];
        const rowOf = (t) => rows.find((r) => r.textContent.indexOf(t) >= 0);
        for (const t of ["启用参考视频归一", "目标画布", "只缩不放", "宽高比对不上时的适配",
                         "重采样方式", "帧率归一", "17k+5 网格对齐方向", "时长上限",
                         "参考视频音频", "素材实测"]) {
            ok(!!rowOf(t), "弹窗有「" + t + "」行");
        }
        ok(rows.every((r) => r.querySelector(".h3d-perf-hint")), "每行都带说明（两行式，不被吞掉）");

        /* 🗜 参考视频压缩（Spark-VPack）：入口在缩放面板里，但**存储分开**（vpack.json）——
         * 档位表由后端下发（每 5 个 latent token 一块的分块口径只许有一份）。 */
        const vpRow = rowOf("参考视频压缩");
        ok(!!vpRow, "弹窗有「参考视频压缩（Spark-VPack）」行");
        const vpSel = vpRow.querySelector("select");
        ok(!!vpSel && vpSel.options.length === 4, "档位下拉是后端下发的 4 档，实际 "
            + (vpSel ? vpSel.options.length : "无下拉"));
        ok(!!vpSel && vpSel.value === "2/5", "档位默认 2/5（后端 default_ratio），实际 "
            + (vpSel ? vpSel.value : "-"));
        ok(!!vpRow.querySelector("input[type=checkbox]"), "有总开关（checkbox）");
        ok(vpRow.textContent.indexOf("vpack.json") >= 0, "说明里点名存储位置 vpack.json（与 refvid 分开）");
        ok(vpRow.textContent.indexOf("RoPE") >= 0,
            "说明里讲清「抽剩帧的时间坐标会被还原」——不然用户会以为参考视频被缩短了");
        ok(vpRow.textContent.indexOf("指纹") >= 0, "说明里讲清会进逐段指纹（改档位→引用视频的段重做）");

        /* 目标画布行：宽高比 + 百万像素 + 跟随链画布 + 即时换算显示 */
        const cv = rowOf("目标画布");
        ok(cv.querySelectorAll("select").length === 2, "目标画布行有两个下拉（宽高比 / 百万像素）");
        ok(cv.textContent.indexOf("跟随链画布") >= 0, "有「跟随链画布」快捷按钮");
        ok(/1344×768/.test(cv.textContent), "即时换算显示 1344×768（与链画布同一公式）");

        /* 试算：点了要发 /refvid_plan，且把报告首行摆出来 */
        const tryBtn = [...ov.querySelectorAll("button")].find((b) => b.textContent.indexOf("试算") >= 0);
        ok(!!tryBtn, "有「试算」按钮");
        tryBtn.click();
        await new Promise((r) => setTimeout(r, 30));
        ok(!!ov.querySelector(".h3d-refvid-ok"), "试算结果渲染到面板（.h3d-refvid-ok）");

        /* 保存契约：攒草稿，点保存才 POST */
        const audioRow = rowOf("参考视频音频");
        const cb = audioRow.querySelector("input[type=checkbox]");
        cb.checked = false;
        cb.dispatchEvent(new w.Event("change"));
        ok(calls.length === 0, "只改控件不发请求（改动先进草稿）");
        const saveBtn = [...ov.querySelectorAll("button")].find((b) => b.textContent.indexOf("保存并应用") >= 0);
        saveBtn.click();
        await new Promise((r) => setTimeout(r, 30));
        ok(calls.length === 1 && calls[0].audio === false,
            "点「保存并应用」才 POST，且带上 audio:false，实际：" + JSON.stringify(calls[0]));
        ok(vpCalls.length === 1 && vpCalls[0].on === false,
            "压缩设置同一次保存一并 POST（两个文件各写一份），实际：" + JSON.stringify(vpCalls[0]));
    }

    /* ================= ③ 音轨开关改变映射条编号 ================= */
    {
        const node = mkNode(mkDs());
        const { w } = load({
            node,
            H3Api: {
                async refvidGet() {
                    return { body: { ok: true, data: { on: true, audio: false } } };
                },
            },
        });
        const mkBar = (refs) => w.buildRefBar({
            node, segIdx: 0, editor: null, gate: true, title: "引用素材", tplKey: "seg0",
            pool: POOL, livePool: () => POOL, readRefs: () => refs, commit: () => {},
        });
        const toks = (bar) => [...bar.el.querySelectorAll(".h3d-refmap-row")]
            .map((r) => r.querySelector(".h3d-refmap-tok").textContent);

        /* 默认（还没取到设置）→ 视频音轨占号：独立音频是 <Audio 2> */
        let bar = mkBar(["片头", "BGM"]);
        bar.paint();
        ok(JSON.stringify(toks(bar)) === JSON.stringify(["<Video 1>", "<Audio 2>"]),
            "音频开关未知时按旧口径（<Video 1> / <Audio 2>），实际 " + JSON.stringify(toks(bar)));

        /* 取到 audio:false → 不预扣，独立音频回到 <Audio 1> */
        await w.loadRefvid();
        ok(w.refvidAudioOn() === false, "refvidAudioOn() 跟随设置");
        bar = mkBar(["片头", "BGM"]);
        bar.paint();
        ok(JSON.stringify(toks(bar)) === JSON.stringify(["<Video 1>", "<Audio 1>"]),
            "音频开关关 → 独立音频回到 <Audio 1>，实际 " + JSON.stringify(toks(bar)));
        ok(typeof w.refvidSummary === "function" && w.refvidSummary().indexOf("不参考音轨") >= 0,
            "摘要如实写「不参考音轨」");
    }

    /* ================= ④ 恢复默认 ================= */
    {
        const calls = [];
        const vpCalls = [];
        const node = mkNode(mkDs());
        const { w } = load({
            node,
            H3Api: {
                async refvidGet() { return { body: { ok: true, data: { on: true } ,
                                                     ar_ratio: ["16:9"], mp_options: [0.98],
                                                     fits: ["裁切填满"], resamples: ["lanczos"],
                                                     grids: ["向下"], limits: ["不限"] } }; },
                async refvidSet(draft) { calls.push(draft); return { body: { ok: true, data: draft } }; },
                async refvidPlan() { return { body: { ok: true, rows: [] } }; },
                async vpackGet() { return { body: { ok: true, data: { on: true, ratio: "3/5" },
                                                    ratios: ["0/5", "1/5", "2/5", "3/5"],
                                                    default_ratio: "2/5" } }; },
                async vpackSet(draft) { vpCalls.push(draft); return { body: { ok: true, data: draft } }; },
            },
        });
        await w.openRefVideoSettings(node);
        const ov = w.document.querySelector(".h3d-refvid-overlay");
        const resetBtn = [...ov.querySelectorAll("button")].find((b) => b.textContent === "恢复默认");
        ok(!!resetBtn, "弹窗有「恢复默认」");
        resetBtn.click();
        await new Promise((r) => setTimeout(r, 30));
        ok(calls.length >= 1 && JSON.stringify(calls[0]) === "{}",
            "恢复默认 = POST 空对象（后端以 DEFAULT_REFVID 起底），实际：" + JSON.stringify(calls[0]));
        ok(vpCalls.length >= 1 && JSON.stringify(vpCalls[0]) === "{}",
            "压缩设置也一并恢复默认（POST 空对象 → DEFAULT_VPACK），实际：" + JSON.stringify(vpCalls[0]));
    }

    console.log(bad ? `\n${bad} 项失败` : "\nALL OK");
    process.exit(bad ? 1 : 0);
})();
