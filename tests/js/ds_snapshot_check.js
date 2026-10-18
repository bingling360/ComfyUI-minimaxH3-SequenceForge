/* 存档时把状态按**名字**再存一份（P1-1，随工作流走）。
 *
 * widgets_values 是按位的：迁移层一旦认错布局，整份状态就没了。properties 不按位、
 * 不参与后端执行，是这里唯一稳的通道（与已有的 __h3_out_intents 同一个理由）。
 * 代价是工作流 JSON 里多一份提示词 → 超过 DS_SNAPSHOT_MAX 直接放弃。
 */
const { load, mkNode } = require("./_harness.js");

let fails = 0;
function ok(cond, msg) {
    if (cond) return;
    fails += 1;
    console.log("FAIL:", msg);
}

const NODE_TYPE = "H3SeamlessChainSampler";

/* ---------- 1) onSerialize 必须写出 __h3_ds 与版本 ---------- */
{
    const node = mkNode({ prompts: ["第一段", "第二段"], segments: [{ seconds: 5 }, { seconds: 6 }], ref_assets: [] });
    const { w } = load({ node });
    const ext = w.app._ext;
    const fakeType = function H3SeamlessChainSampler() {};
    fakeType.prototype = {};
    ext.beforeRegisterNodeDef(fakeType, { name: NODE_TYPE });

    const inst = Object.assign(Object.create(fakeType.prototype), {
        id: 7, widgets: node.widgets, outputs: [], graph: { change() {} },
        setDirtyCanvas() {},
    });
    const o = {};
    inst.onSerialize(o);
    ok(!!(o.properties && o.properties.__h3_ds), "onSerialize 必须写出 properties.__h3_ds");
    ok(!!(o.properties && o.properties.__h3_ds_ver), "快照必须带版本标记 __h3_ds_ver");
    let parsed = null;
    try { parsed = JSON.parse(String(o.properties.__h3_ds)); } catch (e) { parsed = null; }
    ok(parsed && (parsed.prompts || []).length === 2,
        `快照要能还原出两段提示词，实际 ${o.properties && String(o.properties.__h3_ds).slice(0, 40)}`);
    ok(parsed && (parsed.prompts || [])[0] === "第一段", "快照第一段正文必须正确");
    ok(!!(o.properties && Array.isArray(o.properties.__h3_out_intents)),
        "原有输出意图快照不能被顺手删掉");
}

/* ---------- 2) 超大状态放弃快照（工作流体积兜底） ---------- */
{
    const big = "x".repeat(600 * 1024);
    const node = mkNode({ prompts: [big], segments: [{ seconds: 5 }], ref_assets: [] });
    const { w } = load({ node });
    const snap = w.eval("safeDsSnapshot")(node);
    ok(snap === "", `超过 512KB 必须放弃快照，实际长度 ${String(snap).length}`);
}

/* ---------- 3) 没有节点时不炸 ---------- */
{
    const { w } = load({ node: null });
    ok(w.eval("safeDsSnapshot")(null) === "", "safeDsSnapshot(null) 应返回空而不是抛异常");
}

console.log(fails ? `\n${fails} 个断言失败` : "\nds_snapshot_check 全部通过");
process.exit(fails ? 1 : 0);
