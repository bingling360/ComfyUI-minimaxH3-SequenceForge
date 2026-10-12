/* 链参数不再强制整链一致 —— 前端边界提示回归（2026-10-12）。
 *
 * 覆盖三处用户点名的边界：
 *   1) `currentCanvasWH`：宽高比×百万像素 → 画布 [w,h]。它是前端判断「本段与
 *      已存档段分辨率是否一致」的唯一来源，错了后面所有提示都是错的。
 *   2) 重摇弹窗：链首（无上段）/ 链尾（无下段）的锚天然缺省；与旧分辨率相邻时
 *      那一侧的锚会跳过 —— 都要提前说清，否则用户以为「设了双锚没生效」。
 *   3) 「从这段继续」：`重跑起始段` 是全局 1-based 段号（序章占 1 号），必须带
 *      `off`；少了它，有上传序章的项目会连序章一起截断（整链重做）。
 */
const path = require("path");
const H = require(path.join(__dirname, "_harness.js"));

const MANIFEST = {
    schema: 3, done: 3, total: 3, has_prologue: false,
    params: { width: 1376, height: 768, length: 81, ctx: 22, steps: 8, cfg: 1.0 },
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

function mkNode(ar, mp, dir) {
    const node = H.mkNode({ prompts: ["a", "b", "c"], segments: [{}, {}, {}], mode: "分段" });
    node.widgets.find((x) => x.name === "存档目录").value = dir === undefined ? "PROJ" : dir;
    node.widgets = node.widgets.concat([
        { name: "宽高比", value: ar, type: "text", options: { values: ["16:9", "9:16"] } },
        { name: "百万像素", value: mp, type: "number", options: {} },
        { name: "引导帧数", value: "22", type: "text", options: { values: ["关闭", "1", "22"] } },
        { name: "步数", value: 8, type: "number", options: {} },
    ]);
    return node;
}

function planOf(n) {
    return Array.from({ length: n }, (_, i) => ({ kind: "prompt", idx: i }));
}

function boot(node, mf) {
    return H.load({
        node,
        apiRoutes: {
            "/h3chain/project": { ok: true, manifest: mf },
            "/h3chain/projects": { ok: true, projects: [{ dir: "PROJ" }], state: { dir: "PROJ" } },
            "/h3chain/upscale_models": { ok: true, models: [] },
            "/h3chain/bridge_models": { ok: true, models: [] },
        },
    });
}

(async () => {
    /* ---------- 1) currentCanvasWH ---------- */
    {
        const node = mkNode("16:9", 1.0);
        const { w } = boot(node, MANIFEST);
        const c = w.currentCanvasWH(node);
        ok(Array.isArray(c) && c[0] === 1376 && c[1] === 768,
            "16:9 @ 1.0MP → 1376×768", c, [1376, 768]);
        node.widgets.find((x) => x.name === "宽高比").value = "不存在";
        ok(w.currentCanvasWH(node) === null, "非法宽高比 → null", w.currentCanvasWH(node), null);
    }

    /* ---------- 2) 重摇弹窗的边界说明 ---------- */
    {
        const node = mkNode("16:9", 1.0);
        const { w } = boot(node, MANIFEST);
        const ds = w.getDs(node);
        const plan = planOf(3);
        const data = { node, mf: MANIFEST, plan, ds, state: { dir: "PROJ" } };

        /* 2a) 链首（无序章 → 第 1 段是链首）：必须提示上锚缺省 */
        w.openRerollModal(0, data);
        let txt = (w.document.querySelector(".h3d-redo-note") || {}).textContent || "";
        ok(/链首/.test(txt) && /上锚/.test(txt), "链首提示「上锚天然缺省」", txt);
        ok(!/链尾/.test(txt), "链首不该报「链尾」", txt);
        ok(!/当前画布/.test(txt), "画布与存档一致时不该报分辨率提示", txt);
        w.document.querySelector(".h3d-overlay")?.remove();

        /* 2b) 链尾：必须提示下锚缺省 */
        w.openRerollModal(2, data);
        txt = (w.document.querySelector(".h3d-redo-note") || {}).textContent || "";
        ok(/链尾/.test(txt) && /下锚/.test(txt), "链尾提示「下锚天然缺省」", txt);
        ok(!/链首/.test(txt), "链尾不该报「链首」", txt);
        w.document.querySelector(".h3d-overlay")?.remove();

        /* 2c) 中间段 + 画布已改：必须提示「与旧分辨率相邻的那一侧锚会跳过」 */
        const node2 = mkNode("16:9", 0.5);      // 0.5MP → 960×544 ≠ 存档 1376×768
        const b2 = boot(node2, MANIFEST);
        b2.w.openRerollModal(1, { node: node2, mf: MANIFEST, plan: planOf(3),
                                  ds: b2.w.getDs(node2), state: { dir: "PROJ" } });
        const txt2 = (b2.w.document.querySelector(".h3d-redo-note") || {}).textContent || "";
        ok(/当前画布 960×544/.test(txt2) && /1376×768/.test(txt2),
            "画布与存档不同 → 提示两个分辨率", txt2);
        ok(/自动跳过/.test(txt2), "并说明那一侧的锚会自动跳过", txt2);
        ok(!/链首/.test(txt2) && !/链尾/.test(txt2), "中间段不该报链首/链尾", txt2);

        /* 2d) 两侧邻居都已被标重摇 → 那两处接缝会按当前画布重做，不该再报「会跳过」
              （与后端 ResolutionLedger 把 redo 槽排除在"存档值"之外同一口径） */
        const node3 = mkNode("16:9", 0.5);
        const mfMarked = Object.assign({}, MANIFEST, {
            redo_queue: [[0, "双锚"], [2, "双锚"]],
        });
        const b3 = boot(node3, mfMarked);
        b3.w.openRerollModal(1, { node: node3, mf: mfMarked, plan: planOf(3),
                                  ds: b3.w.getDs(node3), state: { dir: "PROJ" } });
        const txt3 = (b3.w.document.querySelector(".h3d-redo-note") || {}).textContent || "";
        ok(!/当前画布/.test(txt3), "邻居已标重摇 → 不再报分辨率边界", txt3);
    }

    /* ---------- 3) 「从这段继续」：重跑起始段必须带 off ---------- */
    /* 提交后 `continueFromSegment` 会把「重跑起始段」还原（防残留旧值让下次普通
     * 运行再次截断），所以不能在调用后读控件——改成拦 `setWidgetValue` 记录落值。 */
    function spyReroll(w) {
        const seen = [];
        const orig = w.setWidgetValue;
        w.setWidgetValue = (n, name, v) => { seen.push([name, v]); return orig(n, name, v); };
        return seen;
    }

    {
        /* 3a) 无序章（off=0）：从段 1 继续 → 重跑起始段 = 2 */
        const node = mkNode("16:9", 1.0);
        const { w } = boot(node, MANIFEST);
        const seen = spyReroll(w);
        await w.continueFromSegment(0, 3, 0, MANIFEST);
        const hit = seen.filter(([name]) => name === "重跑起始段").map(([, v]) => v);
        ok(hit[0] === 2, "无序章：从段 1 继续 → 重跑起始段 2", hit, 2);

        /* 3b) 有序章（off=1）：序章占全局槽位 0，从段 1 继续 → 重跑起始段 3
              （旧写法漏传 off → 2，会把序章也截掉＝整链重做） */
        const mfP = Object.assign({}, MANIFEST, { has_prologue: true, done: 4, total: 4 });
        const node2 = mkNode("16:9", 1.0);
        const b2 = boot(node2, mfP);
        const seen2 = spyReroll(b2.w);
        await b2.w.continueFromSegment(0, 3, 1, mfP);
        const hit2 = seen2.filter(([name]) => name === "重跑起始段").map(([, v]) => v);
        ok(hit2[0] === 3, "有序章：从段 1 继续 → 重跑起始段 3（不得连序章一起截断）", hit2, 3);
    }

    /* ---------- 4) archivedRes：按槽位读 seg_res（与后端 _archived_res 同口径） ---------- */
    {
        const { w } = boot(mkNode("16:9", 1.0), MANIFEST);

        /* 4a) 有逐段记录 → 按槽位取值（混分辨率链里 params 指不了具体段） */
        const mfSeg = Object.assign({}, MANIFEST, {
            seg_res: [[1376, 768], [960, 544], [960, 544]],
        });
        ok(JSON.stringify(w.archivedRes(mfSeg, 0)) === "[1376,768]",
            "seg_res[0] → 1376×768", w.archivedRes(mfSeg, 0), [1376, 768]);
        ok(JSON.stringify(w.archivedRes(mfSeg, 1)) === "[960,544]",
            "seg_res[1] → 960×544", w.archivedRes(mfSeg, 1), [960, 544]);

        /* 4b) 旧档无 seg_res → 整链回落 params */
        ok(JSON.stringify(w.archivedRes(MANIFEST, 1)) === "[1376,768]",
            "旧档（无 seg_res）→ 回落 params", w.archivedRes(MANIFEST, 1), [1376, 768]);

        /* 4c) 不是已存档槽位（g >= done）→ null，不许拿 params 顶替 */
        ok(w.archivedRes(MANIFEST, 3) === null,
            "g >= done（还没生成的段）→ null", w.archivedRes(MANIFEST, 3), null);
        ok(w.archivedRes(MANIFEST, -1) === null, "负槽位 → null", w.archivedRes(MANIFEST, -1), null);

        /* 4d) seg_res 行写坏（长度不对）→ 回落 params，不抛 */
        const mfBad = Object.assign({}, MANIFEST, { seg_res: [[1, 2, 3], null, [0, 0]] });
        ok(JSON.stringify(w.archivedRes(mfBad, 0)) === "[1376,768]",
            "坏行 → 回落 params", w.archivedRes(mfBad, 0), [1376, 768]);
        ok(JSON.stringify(w.archivedRes(mfBad, 2)) === "[1376,768]",
            "全零行 → 回落 params", w.archivedRes(mfBad, 2), [1376, 768]);
    }

    /* ---------- 5) upRecValid：越界口径必须与后端 base_hash 逐字一致 ---------- */
    {
        const { w } = boot(mkNode("16:9", 1.0), MANIFEST);
        const mk = (baseHash) => ({
            done: 3, total: 3, has_prologue: false,
            params: { width: 1376, height: 768 },
            prompt_hashes: ["h0"],            // 故意短于 done：槽 1、2 越界
            seeds: [7],
            upscale: { segs: [null, { done: true, base_hash: baseHash }] },
        });
        /* 5a) 数组短于槽位 → 空串占位（后端口径），不是"判为有效" */
        ok(w.upRecValid(mk("|"), 1) !== null,
            "越界槽：base_hash 恰为 \"|\"（空串占位）→ 有效", null, "非 null");
        ok(w.upRecValid(mk("h0|7"), 1) === null,
            "越界槽：base_hash 用了真实值 → 无效（后端也这么判）", null, null);

        /* 5b) 键整个不存在（极老存档）→ 退回只看 done，行为与从前一致 */
        const mfOld = { done: 1, upscale: { segs: [{ done: true, base_hash: "whatever" }] } };
        ok(w.upRecValid(mfOld, 0) !== null,
            "极老存档（两条数组都缺）→ 退回只看 done", null, "非 null");
    }

    console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
    process.exit(bad ? 1 : 0);
})().catch((e) => { console.error("ERR", e); process.exit(1); });
