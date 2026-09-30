/* 端口错位修复守卫（jsdom 真跑 shipped 的 h3_director.js）。
 *
 * 背景（真事故）：同一份工作流在一台机器上一切正常、在另一台上"连接错位" ——
 * workflow JSON 的连线是**按位次**存的（[id, 源节点, 源槽, 目标节点, **目标槽下标**, 类型]），
 * 两台机器/两个版本组装 node.inputs 的顺序不同（插件 schema 增删过、或前端把 optional
 * 端口排到 required 之后），MODEL 的线就落到「宽高比」(COMBO) / 「宽度」(INT) 上：
 * 前端弹「宽度 输入需要 INT，但连接的输出为 MODEL」，后端 validate_prompt 报
 * received_type(MODEL) mismatch input_type(COMBO)。画布上那根线看着还连着，
 * 肉眼查不出来 —— 所以必须有守卫。
 *
 * 本文件钉四件事（对应源码注释里那三条铁律）：
 *   ① 按**名字**搬线 —— 同名就搬，哪怕类型一模一样（视频VAE ↔ 音频VAE）；
 *   ② 目标端口已下线 → 断开 + 告警，绝不留在错误端口上；
 *   ③ 目标端口被别的线占用 → 一条都不动 + 报冲突；
 *   ④ 只碰本插件节点；出厂默认工作流在同布局机器上零改动。
 *
 * 跑法：NODE_PATH=<root>/node_modules node tests/js/link_repair_check.js
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");

let harness;
try { harness = require("./_harness.js"); }
catch (e) { console.error("SKIP: 未安装 jsdom（npm install）"); process.exit(2); }

const ROOT = harness.ROOT;
const DIR_SRC = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8");
const TPL_SRC = fs.readFileSync(path.join(ROOT, "web", "h3_default_workflow.js"), "utf8");

let ok = true;
const results = [];
async function t(name, fn) {
    try { await fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}

/** 装一个 jsdom 窗口，取回 shipped 源码里的两个函数；顺带拦下 console 便于断言告警。 */
function mkWin() {
    const { w } = harness.load({});
    const logs = [];
    /* 只换方法不换 console 对象：jsdom 的 window.console 未必可整体赋值 */
    w.console.log = (m) => logs.push(["log", String(m)]);
    w.console.warn = (m) => logs.push(["warn", String(m)]);
    w.console.error = (m) => logs.push(["error", String(m)]);
    return {
        w, logs,
        intents: w.eval("savedLinkIntents"),
        repair: w.eval("repairMisplacedLinks"),
        said: (lv) => logs.filter((x) => !lv || x[0] === lv).map((x) => x[1]).join("\n"),
    };
}

/* ---------- 假活图：签名与 litegraph 对齐（connect / disconnectInput），够真跑搬运逻辑 ----------
 * live = [{id, type?, ports: [[名字, 类型, linkId|null], ...]}]
 * meta = { linkId: [源节点 id, 源槽, 线类型] }（源节点自动补一个带 connect 的桩） */
function mkGraph(live, meta) {
    const linkBox = {};
    const nodes = new Map();
    let nextId = 9000;
    const outType = new Map();
    for (const m of Object.values(meta || {})) outType.set(String(m[0]), m[2] || "MODEL");
    for (const oid of outType.keys()) {
        nodes.set(oid, {
            id: Number(oid), type: "UNETLoader",
            connect(slot, target, targetSlot) {
                const id = nextId++;
                linkBox[id] = { id, origin_id: this.id, origin_slot: slot,
                                target_id: target.id, target_slot: targetSlot,
                                type: outType.get(String(this.id)) };
                target.inputs[targetSlot].link = id;
                return linkBox[id];
            },
        });
    }
    for (const n of live) {
        nodes.set(String(n.id), {
            id: n.id, type: n.type || "H3SeamlessChainSampler",
            inputs: n.ports.map(([name, type, link]) => ({ name, type, link: link == null ? null : link })),
            disconnectInput(i) {
                const s = this.inputs[i];
                if (!s || s.link == null) return;
                delete linkBox[s.link];
                s.link = null;
            },
        });
    }
    for (const n of live) {
        n.ports.forEach(([, , link], i) => {
            if (link == null) return;
            const m = (meta || {})[link] || [1, 0, "MODEL"];
            linkBox[link] = { id: link, origin_id: m[0], origin_slot: m[1],
                              target_id: n.id, target_slot: i, type: m[2] };
        });
    }
    return {
        graph: { _nodes: [...nodes.values()], links: linkBox, setDirtyCanvas() {},
                 getNodeById: (id) => nodes.get(String(id)) || null },
        nodes, linkBox,
    };
}

const portOf = (env, nodeId, name) => {
    const n = env.nodes.get(String(nodeId));
    return n ? n.inputs.find((s) => s.name === name) : null;
};
/** 端口上那条线的源节点 id；没接线 / 名字不存在 → null。 */
const originAt = (env, nodeId, name) => {
    const p = portOf(env, nodeId, name);
    if (!p || p.link == null) return null;
    const l = env.linkBox[p.link];
    return l ? l.origin_id : null;
};

/* ---------- 夹具 ---------- */

/* 用户的真实情形：老存档**没有**「起始视频/起始视频音轨」两个槽，二采模型因此在
 * idx4；本机 schema 把它们排回去了 → 前端按位次接，MODEL 就落在 idx4 的「宽高比」上。 */
const OLD_SAVED = {
    nodes: [{ id: 10, type: "H3SeamlessChainSampler", inputs: [
        { name: "模型", type: "MODEL", link: 11 },
        { name: "文本编码器", type: "CLIP", link: 12 },
        { name: "视频VAE", type: "VAE", link: 13 },
        { name: "音频VAE", type: "VAE", link: 14 },
        { name: "二采模型", type: "MODEL", link: 15 },
    ] }],
    links: [
        [11, 60, 0, 10, 0, "MODEL"],
        [12, 3, 0, 10, 1, "CLIP"],
        [13, 4, 0, 10, 2, "VAE"],
        [14, 5, 0, 10, 3, "VAE"],
        [15, 1, 0, 10, 4, "MODEL"],
    ],
};
const OLD_META = { 11: [60, 0, "MODEL"], 12: [3, 0, "CLIP"], 13: [4, 0, "VAE"],
                   14: [5, 0, "VAE"], 15: [1, 0, "MODEL"] };
/* 本机端口顺序（= 前端现场组装的那套）：按位次接进来的线全在错位置上 */
const MODERN_LIVE = [{ id: 10, ports: [
    ["视频VAE", "VAE", 11], ["音频VAE", "VAE", 12], ["模型", "MODEL", 13], ["文本编码器", "CLIP", 14],
    ["宽高比", "COMBO", 15], ["百万像素", "FLOAT", null], ["宽度", "INT", null], ["高度", "INT", null],
    ["起始视频", "IMAGE", null], ["起始视频音轨", "AUDIO", null], ["二采模型", "MODEL", null],
    ["自定义Sigmas", "SIGMAS", null],
] }];

(async () => {
    await t("布局一致 → 一条都不动（修复必须是幂等的空转）", () => {
        const { intents, repair } = mkWin();
        const saved = {
            nodes: [{ id: 10, type: "H3SeamlessChainSampler", inputs: [
                { name: "模型", type: "MODEL", link: 11 },
                { name: "文本编码器", type: "CLIP", link: 12 },
                { name: "视频VAE", type: "VAE", link: 13 },
                { name: "音频VAE", type: "VAE", link: 14 },
            ] }],
            links: [[11, 60, 0, 10, 0, "MODEL"], [12, 3, 0, 10, 1, "CLIP"],
                    [13, 4, 0, 10, 2, "VAE"], [14, 5, 0, 10, 3, "VAE"]],
        };
        const env = mkGraph([{ id: 10, ports: [
            ["模型", "MODEL", 11], ["文本编码器", "CLIP", 12], ["视频VAE", "VAE", 13],
            ["音频VAE", "VAE", 14], ["宽高比", "COMBO", null], ["宽度", "INT", null],
        ] }], OLD_META);
        const res = repair(intents(saved), env.graph);
        assert.strictEqual(res.moved, 0, "同布局不该搬任何线");
        assert.strictEqual(res.rest.length, 0, "不该留待修项");
        assert.strictEqual(portOf(env, 10, "模型").link, 11, "原连线 id 该原样保留");
        assert.strictEqual(originAt(env, 10, "视频VAE"), 4, "视频VAE 该还在原位");
    });

    await t("★ 老存档少两个槽：MODEL 落在「宽高比」上 → 按名字全部搬回", () => {
        const { intents, repair, said } = mkWin();
        const env = mkGraph(MODERN_LIVE, OLD_META);
        const res = repair(intents(OLD_SAVED), env.graph);
        assert.strictEqual(res.moved, 5, "5 条线都该搬回去");
        assert.strictEqual(res.conflicts + res.dropped + res.rest.length, 0, "不该有冲突/断开/待修");
        assert.strictEqual(originAt(env, 10, "模型"), 60, "模型该接回 60");
        assert.strictEqual(originAt(env, 10, "文本编码器"), 3, "文本编码器该接回 3");
        assert.strictEqual(originAt(env, 10, "视频VAE"), 4, "视频VAE 该接回 4");
        assert.strictEqual(originAt(env, 10, "音频VAE"), 5, "音频VAE 该接回 5");
        assert.strictEqual(originAt(env, 10, "二采模型"), 1, "二采模型该接回 1");
        for (const nm of ["宽高比", "百万像素", "宽度", "高度"]) {
            assert.strictEqual(portOf(env, 10, nm).link, null, `「${nm}」必须被腾空（不许留在控件槽上）`);
        }
        assert.ok(said("warn").indexOf("端口错位修复") >= 0, "搬线必须留告警，不许静默改线");
        assert.ok(said("warn").indexOf("宽高比") >= 0, "告警里该说清原先落在哪个端口");

        /* 幂等：同一份意图再跑一遍，不该再动 */
        const again = repair(intents(OLD_SAVED), env.graph);
        assert.strictEqual(again.moved, 0, "修完再跑必须空转");
    });

    await t("类型一模一样也要按名字搬（视频VAE ↔ 音频VAE 互换，类型判据发现不了）", () => {
        const { intents, repair } = mkWin();
        const saved = {
            nodes: [{ id: 10, type: "H3SeamlessChainSampler", inputs: [
                { name: "视频VAE", type: "VAE", link: 13 },
                { name: "音频VAE", type: "VAE", link: 14 },
            ] }],
            links: [[13, 4, 0, 10, 0, "VAE"], [14, 5, 0, 10, 1, "VAE"]],
        };
        const env = mkGraph([{ id: 10, ports: [
            ["音频VAE", "VAE", 13], ["视频VAE", "VAE", 14],
        ] }], { 13: [4, 0, "VAE"], 14: [5, 0, "VAE"] });
        const res = repair(intents(saved), env.graph);
        assert.strictEqual(res.moved, 2, "两条都得按名字换回来");
        assert.strictEqual(originAt(env, 10, "视频VAE"), 4, "视频VAE 该接 4");
        assert.strictEqual(originAt(env, 10, "音频VAE"), 5, "音频VAE 该接 5");
    });

    await t("目标端口已下线 → 断开 + 告警（绝不留在错误端口上）", () => {
        const { intents, repair, said } = mkWin();
        const saved = {
            nodes: [{ id: 10, type: "H3SeamlessChainSampler", inputs: [
                { name: "起始视频", type: "IMAGE", link: null },
                { name: "起始视频音轨", type: "AUDIO", link: 16 },
            ] }],
            links: [[16, 8, 0, 10, 1, "AUDIO"]],
        };
        /* 本机没有「起始视频音轨」这个端口了：前端按位次把这条线挂到了 idx1 = 宽高比 */
        const env = mkGraph([{ id: 10, ports: [
            ["起始视频", "IMAGE", null], ["宽高比", "COMBO", 16], ["宽度", "INT", null],
        ] }], { 16: [8, 0, "AUDIO"] });
        const res = repair(intents(saved), env.graph);
        assert.strictEqual(res.dropped, 1, "下线端口的线该被断开");
        assert.strictEqual(portOf(env, 10, "宽高比").link, null, "不许留在「宽高比」上");
        assert.strictEqual(portOf(env, 10, "起始视频").link, null, "也不许挪到别的端口上");
        assert.ok(said("warn").indexOf("已不存在") >= 0, "该说清端口已下线");
        assert.ok(said("warn").indexOf("已断开") >= 0, "该说清已断开");
    });

    await t("目标端口被别的线占用 → 一条都不动 + 报冲突（不许拆别人的线）", () => {
        const { intents, repair, said } = mkWin();
        const saved = {
            nodes: [{ id: 10, type: "H3SeamlessChainSampler", inputs: [
                { name: "模型", type: "MODEL", link: 11 },
            ] }],
            links: [[11, 60, 0, 10, 0, "MODEL"]],
        };
        const env = mkGraph([{ id: 10, ports: [
            ["宽高比", "COMBO", 11], ["模型", "MODEL", 99],
        ] }], { 11: [60, 0, "MODEL"], 99: [77, 0, "MODEL"] });
        const res = repair(intents(saved), env.graph);
        assert.strictEqual(res.conflicts, 1, "该报冲突");
        assert.strictEqual(res.moved, 0, "冲突时一条都不许动");
        assert.strictEqual(portOf(env, 10, "宽高比").link, 11, "原线该留在原地等人工确认");
        assert.strictEqual(originAt(env, 10, "模型"), 77, "别人接在「模型」上的线不许被拆");
        assert.ok(said("error").indexOf("已被另一条线占用") >= 0, "冲突必须报 error，不许静默");
    });

    await t("只碰本插件节点：别人的节点（KSampler）连意图都不收", () => {
        const { intents, repair } = mkWin();
        const saved = {
            nodes: [{ id: 5, type: "KSampler", inputs: [{ name: "model", type: "MODEL", link: 21 }] }],
            links: [[21, 1, 0, 5, 3, "MODEL"]],
        };
        const list = intents(saved);
        assert.strictEqual(list.length, 0, "非本插件节点不该产生意图：" + JSON.stringify(list));
        const env = mkGraph([{ id: 5, type: "KSampler", ports: [["seed", "INT", 21]] }], { 21: [1, 0, "MODEL"] });
        const res = repair(list, env.graph);
        assert.strictEqual(res.moved + res.dropped + res.conflicts, 0, "别人的节点一条线都不许动");
        assert.strictEqual(portOf(env, 5, "seed").link, 21, "别人的线该原样保留");
    });

    await t("极老存档没有端口名：类型唯一候选才兜底搬；类型对得上就不动", () => {
        const { intents, repair, said } = mkWin();
        const saved = {
            nodes: [{ id: 10, type: "H3SeamlessChainSampler", inputs: [{ type: "VAE", link: 31 }] }],
            links: [[31, 4, 0, 10, 0, "VAE"]],
        };
        const bad = mkGraph([{ id: 10, ports: [["模型", "MODEL", 31], ["视频VAE", "VAE", null]] }],
                            { 31: [4, 0, "VAE"] });
        const r1 = repair(intents(saved), bad.graph);
        assert.strictEqual(r1.moved, 1, "类型对不上且只有一个 VAE 候选 → 该兜底搬过去");
        assert.strictEqual(originAt(bad, 10, "视频VAE"), 4, "VAE 该落到 VAE 端口");

        const fine = mkGraph([{ id: 10, ports: [["视频VAE", "VAE", 31], ["模型", "MODEL", null]] }],
                             { 31: [4, 0, "VAE"] });
        const r2 = repair(intents(saved), fine.graph);
        assert.strictEqual(r2.moved, 0, "落点类型本来就对 → 没有错位证据，不许乱搬");
        assert.strictEqual(portOf(fine, 10, "视频VAE").link, 31, "原线该原样保留");
        assert.strictEqual(said("error"), "", "没有错位证据时不该报 error");
    });

    await t("源码守卫：载入钩子收意图 + 三处触发 + 按名字判定", () => {
        const i = DIR_SRC.indexOf("app.loadGraphData = function (data, ...args) {");
        assert.ok(i > 0, "loadGraphData 包装不见了（widget 迁移与连线修复都挂在它上面）");
        const body = DIR_SRC.slice(i, i + 700);
        assert.ok(body.indexOf("migrateGraphWidgets(data)") >= 0, "widget 迁移层不该被挤掉");
        assert.ok(body.indexOf("pendingLinkIntents = savedLinkIntents(migrated)") >= 0,
            "载入钩子必须先把连线意图收下来");
        assert.ok(body.indexOf("res.then(run, run)") >= 0, "该在 Promise 结算后跑一次");
        assert.ok(body.indexOf("setTimeout(run, 600)") >= 0, "该留定时器兜底（建节点晚于返回的前端）");
        assert.ok(DIR_SRC.indexOf("if (pendingLinkIntents.length) setTimeout(runPendingLinkRepair, 0);") >= 0,
            "loadedGraphNode 那条触发路径不见了（还得是下一拍，不能同步改线）");
        assert.ok(DIR_SRC.indexOf("s.name === it.name") >= 0, "必须按端口**名字**判定");
        assert.ok(DIR_SRC.indexOf("已被另一条线占用") >= 0 && DIR_SRC.indexOf("本机已不存在") >= 0,
            "三条铁律的代码锚点（冲突 / 下线）不见了");
        assert.ok(DIR_SRC.indexOf('const LINK_REPAIR_TYPES = new Set([NODE_TYPE, "H3SeamDoctor", '
            + '"H3EAVFetaPatch", "H3EAVFetaReport"]);') >= 0, "修复范围必须只限本插件节点");
        /* 反向：不许去复刻前端的端口排序规则（那正是随版本漂移的东西），也不许重排活节点端口表 */
        assert.ok(DIR_SRC.indexOf("defaultVerticalInputs") < 0, "不许复刻前端排序规则");
        assert.ok(!/node\.inputs\.(sort|reverse|splice)\(/.test(DIR_SRC), "不许重排活节点的端口表");
    });

    await t("出厂默认工作流：意图自洽，同布局机器上零改动", () => {
        const m = TPL_SRC.match(/window\.H3_DEFAULT_WORKFLOW\s*=\s*(\{[\s\S]*\})\s*;?\s*$/);
        assert.ok(m, "读不出 web/h3_default_workflow.js 的 JSON");
        const tpl = JSON.parse(m[1]);
        const { intents, repair } = mkWin();
        const list = intents(tpl);
        const names = Array.from(list, (x) => x.name).sort();   // Array.from 收进主 realm
        assert.deepStrictEqual(names, ["文本编码器", "模型", "视频VAE", "音频VAE"],
            "默认工作流的接线端口漂了：" + JSON.stringify(names));
        const sampler = tpl.nodes.find((n) => n.type === "H3SeamlessChainSampler");
        for (const it of list) {
            const port = sampler.inputs.find((s) => s.name === it.name);
            assert.ok(port, "断言用的端口在文件里不存在：" + it.name);
            assert.strictEqual(port.link != null, true, `「${it.name}」在文件里没有连线，意图不该收录`);
        }
        /* 同布局机器（端口顺序 = 文件顺序）→ 一条都不该动 */
        const meta = {};
        for (const L of tpl.links) meta[L[0]] = [L[1], L[2], L[5]];
        const env = mkGraph([{ id: sampler.id, ports: sampler.inputs.map((s) => [s.name, s.type, s.link]) }], meta);
        const res = repair(list, env.graph);
        assert.strictEqual(res.moved, 0, "同布局机器上出厂工作流不该被改动");
        assert.strictEqual(res.conflicts + res.dropped + res.rest.length, 0, "也不该有冲突/断开/待修");
    });

    results.forEach((r) => console.log(r));
    console.log(ok ? "\nlink_repair_check 全部通过" : "\nlink_repair_check 失败");
    process.exit(ok ? 0 : 1);
})();
