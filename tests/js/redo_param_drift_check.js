/* 重摇 / 二采提交不许把用户刚改的**软参数**静默写回存档值。
 *
 * 用户报告：「先跑了一段画面，不满意，想重新跑 → 重摇此段 → 锚定模式改无锚 →
 * 删掉手动锚定 → 把步数改成 6 步 → 最后没生效，还是跑的 8 步」。
 *
 * 真凶：`submitRedo()` 提交前调 `applyChainParams(node, mf.params)` **全量静默纠偏**
 * ——把 manifest 存档的 steps=8 写回画布控件。注释里给的理由是「后端 assert_match
 * 会硬报错」，但 `checkpoint.assert_match` 自 2026-09-16 起**只硬校验 width/height**，
 * steps/cfg/采样器/调度器/fade_ratio/gate 的变更「只回报、不报错、不触发重做」
 * （docs/手动锚定_分段latent参考规范化_实施规划.md §4.3 决策总表）。也就是说
 * 后端本来就允许，是前端自己把值改回去了。
 *
 * 现在的口径（KEEP_ON_REDO 白名单，2026-10-12 起**恒生效**）：
 *   · 软参数（步数/CFG/采样器/调度器/递减锚定/桥帧门控三件套）→ 保留画布当前值；
 *   · 画幅（宽高比/百万像素）→ 同样保留画布当前值：链参数不再强制整链一致，
 *     改分辨率就按新分辨率重做这一段（相邻段分辨率不同时后端跳过尾帧桥并提示）；
 *   · 时长 / 引导帧数 → 仍按存档还原（帧坐标是硬结构），但**写进 LED**。
 * 旧版还有一条例外「存档目录为空（按参数指纹自动命名）→ 白名单作废，一律还原」，
 * 已随自动目录名固定化（`h3chain`，与参数无关）取消。
 */
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");
const H = require(path.join(__dirname, "_harness.js"));

const MANIFEST = {
    schema: 3,
    params: {
        width: 1376, height: 768, length: 81, ctx: 22, steps: 8, cfg: 1.0,
        sampler: "res_multistep", scheduler: "simple", chain: true,
        fade_ratio: 0, gate: { mode: "关闭", threshold: 30.0, limit: 34 },
    },
};

let bad = 0;
function ok(cond, msg, got, want) {
    if (cond) return;
    bad++;
    console.log("  FAIL " + msg);
    if (arguments.length > 2) {
        console.log("    期望: " + JSON.stringify(want));
        console.log("    实际: " + JSON.stringify(got));
    }
}

function apiRoutes() {
    return {
        "/h3chain/ping": { ok: true },
        "/h3chain/projects": { ok: true, projects: [{ dir: "PROJ" }], state: { dir: "PROJ" } },
        "/h3chain/upscale_models": { ok: true, models: [] },
        "/h3chain/bridge_models": { ok: true, models: [] },
        "/h3chain/project": { ok: true, manifest: MANIFEST },
    };
}

/** 造一个导演台主节点：步数=6（用户刚改的），每段时长=5（与存档 81 帧=3.38s 不符） */
function mkNode(dir, steps) {
    const node = H.mkNode({ redo_segs: [[0, "无锚"]] });
    node.widgets.find((x) => x.name === "存档目录").value = dir;
    node.widgets = node.widgets.concat([
        { name: "宽高比", value: "16:9", type: "text", options: { values: ["16:9", "9:16"] } },
        { name: "百万像素", value: 1.0, type: "number", options: {} },
        { name: "引导帧数", value: "22", type: "text", options: { values: ["关闭", "1", "22"] } },
        { name: "步数", value: steps, type: "number", options: { min: 1, max: 100 } },
        { name: "CFG", value: 1.0, type: "number", options: {} },
        { name: "采样器", value: "res_multistep", type: "text", options: { values: ["res_multistep", "euler"] } },
        { name: "调度器", value: "simple", type: "text", options: { values: ["simple", "normal"] } },
        { name: "递减锚定", value: "关闭", type: "text", options: {} },
        { name: "桥帧门控", value: "关闭", type: "text", options: {} },
        { name: "清晰度阈值", value: 30.0, type: "number", options: {} },
        { name: "回退上限", value: 34, type: "number", options: {} },
        { name: "生成模式", value: "分段", type: "text", options: {} },
    ]);
    return node;
}

function wv(node, name) { return node.widgets.find((x) => x.name === name).value; }

/** 装一次导演台并刷新（lastDir 由 collectData 赋值，真机流程同） */
async function boot(dir, steps) {
    const node = mkNode(dir, steps);
    const leds = [];
    const { w } = H.load({ node, apiRoutes: apiRoutes() });
    await w.collectData();
    const orig = w.setLed;
    w.setLed = (...a) => { leds.push(String(a[1] || "")); return orig.apply(w, a); };
    return { w, node, leds, last: () => leds.filter(Boolean).pop() || "" };
}

(async () => {
    /* ---------- 1) 命名项目：步数 8→6 必须生效（本次修的 bug） ---------- */
    {
        const { w, node, last } = await boot("PROJ", 6);
        ok(wv(node, "步数") === 6, "提交前画布步数=6（用户改的）", wv(node, "步数"), 6);
        await w.submitRedo();
        ok(wv(node, "步数") === 6,
            "命名项目：重摇提交后步数仍为 6（以前被静默写回 8）", wv(node, "步数"), 6);
        ok(/本次按画布当前值重做/.test(last()), "LED 说明「本次按画布当前值重做」", last());
        ok(/步数：存档 8 → 当前 6/.test(last()),
            "LED 明确写出步数的 存档→当前 对照", last());
    }

    /* ---------- 2) 时长/引导帧数是硬结构：仍按存档还原，但必须报出来 ---------- */
    {
        const { w, node, last } = await boot("PROJ", 8);
        ok(wv(node, "每段时长") === 5, "画布每段时长=5（与存档 81 帧=3.38s 不符）",
            wv(node, "每段时长"), 5);
        await w.submitRedo();
        ok(wv(node, "每段时长") === 3.38,
            "每段时长按存档还原成 3.38s（帧坐标不能漂）", wv(node, "每段时长"), 3.38);
        ok(/已自动还原存档参数/.test(last()) && /每段时长/.test(last()),
            "LED 报出「已自动还原存档参数：每段时长 …」", last());
        ok(!/本次按画布当前值重做/.test(last()),
            "参数全一致时不该出现「本次按画布当前值重做」", last());
    }

    /* ---------- 3) 自动命名模式（存档目录为空）：白名单同样生效 ---------- */
    {
        const { w, node, last } = await boot("", 6);
        await w.submitRedo();
        ok(wv(node, "步数") === 6,
            "自动命名模式：步数仍为 6（自动目录名固定 h3chain，与参数无关，不再需要全量还原）",
            wv(node, "步数"), 6);
        ok(/本次按画布当前值重做/.test(last()) && /步数：存档 8 → 当前 6/.test(last()),
            "LED 报出「本次按画布当前值重做：步数 …」", last());
    }

    /* ---------- 3b) 画幅（分辨率）也进白名单：改分辨率重摇不再被写回 ---------- */
    {
        const { w, node, last } = await boot("PROJ", 8);
        node.widgets.find((x) => x.name === "宽高比").value = "9:16";
        node.widgets.find((x) => x.name === "百万像素").value = 0.5;
        await w.submitRedo();
        ok(wv(node, "宽高比") === "9:16" && wv(node, "百万像素") === 0.5,
            "改分辨率后重摇：画幅保留画布当前值（存档 16:9·1MP 不得写回）",
            [wv(node, "宽高比"), wv(node, "百万像素")], ["9:16", 0.5]);
        ok(/本次按画布当前值重做/.test(last()) && /画幅/.test(last()),
            "LED 报出画幅按当前值重做", last());
    }

    /* ---------- 4) 手动「恢复项目上次参数」按钮仍是全量口径（白名单只给重摇/二采） ---------- */
    {
        const { w, node } = await boot("PROJ", 6);
        w.applyParamsToCanvas(node, MANIFEST.params);
        ok(wv(node, "步数") === 8,
            "「恢复项目上次参数」按钮照旧把步数写成存档值（用户主动要的）", wv(node, "步数"), 8);
    }

    console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
    process.exit(bad ? 1 : 0);
})().catch((e) => { console.error("ERR", e); process.exit(1); });
