/* 项目存档 → 画布的一键恢复（P1-2）。
 *
 * 钉三件事：
 *   1) promptsFromManifest 的**槽位对齐**（序章占 0、插入槽占位行跳过、seg_fields
 *      按全局槽位取）—— 这是"读档后第 3 段跑到第 2 段"的唯一真源；
 *   2) adoptManifestPrompts 只写 prompts/segments/inserts，不碰别的；
 *   3) restoreCandidate 的判据：画布有内容不打扰、存档也空没得恢复。
 *
 * 为什么不做自动写回：节点为空也可能是用户刚清空，自动复活就是反向 bug。
 */
const { load, mkNode } = require("./_harness.js");

let fails = 0;
function ok(cond, msg) {
    if (cond) return;
    fails += 1;
    console.log("FAIL:", msg);
}

const { w } = load({ node: null });
const fromManifest = w.eval("promptsFromManifest");
const restoreCand = w.eval("restoreCandidate");

/* 序章占全局槽 0；槽 2 是「插入视频」占位行（另有 inserts 记录） */
const MF = {
    has_prologue: true,
    prompts: ["「序章（上传视频）」", "段1正文", "[插入视频] a.mp4", "段3正文"],
    inserts: [{ slot: 2, file: "a.mp4" }],
    seg_fields: [null, { seconds: 5 }, null, { seconds: 7 }],
    total: 4,
};

/* ---------- 1) 槽位对齐 ---------- */
{
    const p = fromManifest(MF);
    ok(p.texts.length === 2 && p.texts[0] === "段1正文" && p.texts[1] === "段3正文",
        `应跳过序章与插入槽，实际 ${JSON.stringify(p.texts)}`);
    ok(p.rows === 3, `rows 应等于去掉序章后的行数，实际 ${p.rows}`);
    ok(p.inserts.length === 1 && p.inserts[0].pos === 2 && p.inserts[0].file === "a.mp4",
        `插入段应按 slot→链位还原，实际 ${JSON.stringify(p.inserts)}`);
    ok(p.segs.length === 2 && p.segs[0].seconds === 5 && p.segs[1].seconds === 7,
        `seg_fields 必须按全局槽位取（0→序章、2→插入槽），实际 ${JSON.stringify(p.segs)}`);
}

/* ---------- 2) 没有序章的普通项目 ---------- */
{
    const p = fromManifest({ prompts: ["A", "B"], seg_fields: [{ seconds: 3 }, { seconds: 4 }] });
    ok(p.texts.length === 2 && p.texts[0] === "A", `无序列时逐条对应，实际 ${JSON.stringify(p.texts)}`);
    ok(p.segs[1].seconds === 4, `第二段秒数应为 4，实际 ${p.segs[1].seconds}`);
}

/* ---------- 3) 装回画布：只写这三样 ---------- */
{
    const node = mkNode({ prompts: [""], segments: [{ seconds: 9 }], ref_assets: [], inserts: [] });
    const { w: w2 } = load({ node });
    const n = w2.eval("adoptManifestPrompts")(node, MF);
    ok(n === 2, `应装回 2 段，实际 ${n}`);
    const ds = w2.eval("getDs")(node);
    ok(ds.prompts[0] === "段1正文" && ds.prompts[1] === "段3正文",
        `装回的正文必须逐段对位，实际 ${JSON.stringify(ds.prompts)}`);
    ok(ds.segments[0].seconds === 5 && ds.segments[1].seconds === 7,
        `分段字段也要一起回来，实际 ${JSON.stringify(ds.segments.map((s) => s.seconds))}`);
    ok((ds.inserts || []).length === 1, "插入段也要一起回来");
}

/* ---------- 4) 判据：画布有内容 → 不打扰 ---------- */
{
    const node = mkNode({ prompts: ["我有内容"], segments: [{ seconds: 5 }], ref_assets: [] });
    const { w: w2 } = load({ node });
    ok(restoreCand(node, MF, "PROJ") === null, "画布有提示词时不得弹恢复");
}

/* ---------- 5) 判据：存档也空 → 没得恢复 ---------- */
{
    const node = mkNode({ prompts: [""], segments: [{ seconds: 5 }], ref_assets: [] });
    const { w: w2 } = load({ node });
    ok(restoreCand(node, { prompts: ["", ""], seg_fields: [] }, "PROJ") === null,
        "存档同样为空时不应弹恢复");
}

/* ---------- 6) 判据：画布空 + 存档有 → 应给候选 ---------- */
{
    const node = mkNode({ prompts: [""], segments: [{ seconds: 5 }], ref_assets: [] });
    const { w: w2 } = load({ node });
    const cand = restoreCand(node, MF, "PROJ");
    ok(cand === MF, "画布空 + 存档有 → 必须给出候选（否则切工作流后永远找不回来）");
}

/* ---------- 7) 缺节点 / 缺存档：null ---------- */
{
    const { w: w2 } = load({ node: null });
    ok(restoreCand(null, MF, "PROJ") === null, "没有节点时不得给候选");
    ok(restoreCand({ id: 1, widgets: [] }, null, "PROJ") === null, "没有 manifest 时不得给候选");
}

console.log(fails ? `\n${fails} 个断言失败` : "\nmanifest_restore_check 全部通过");
process.exit(fails ? 1 : 0);
