/* 状态解析失败不许静默（P0-4）：留证 + 可导出，且不许用默认值盖掉旧快照。
 *
 * 为什么单独钉：`getDs` 的 catch 原本只 `return defaultDs()`。UI 下一次交互就
 * `setDs` 把控件值写成默认 JSON —— 原文从此彻底消失，连"为什么空的"都查不出来。
 * 现在要求三件事：① 原文进 properties.__h3_ds_raw；② 顶栏能问到 dsBrokenInfo()；
 * ③ 快照函数在状态损坏时**返回空**，绝不能用默认值把上一份好快照顶掉。
 */
const { load, mkNode } = require("./_harness.js");

let fails = 0;
function ok(cond, msg) {
    if (cond) return;
    fails += 1;
    console.log("FAIL:", msg);
}

function env(value) {
    const node = mkNode({ prompts: ["正文"], segments: [{ seconds: 5 }], ref_assets: [] });
    node.widgets[0].value = value;
    const { w } = load({ node });
    return { w, node };
}

/* ---------- 1) 坏 JSON：回默认态，但原文必须留证 ---------- */
{
    const { w, node } = env("{oops");
    const ds = w.eval("getDs")(node);
    ok((ds.prompts || []).length === 1 && !ds.prompts[0],
        `坏状态应回默认单空段，实际 ${JSON.stringify(ds.prompts)}`);
    ok(node.properties && node.properties.__h3_ds_raw === "{oops",
        `原文必须存进 properties.__h3_ds_raw，实际 ${node.properties && node.properties.__h3_ds_raw}`);
    const brk = w.eval("dsBrokenInfo")();
    ok(brk && brk.len === 5, `dsBrokenInfo 应记下原文长度，实际 ${brk && brk.len}`);
    ok(brk && String(brk.nodeId) === String(node.id), "dsBrokenInfo 必须带节点 id（横幅按节点判定）");
}

/* ---------- 2) 原文更长才覆盖，不许用短原文盖掉长留证 ---------- */
{
    const { w, node } = env("{oops");
    w.eval("getDs")(node);
    node.properties.__h3_ds_raw = "这是一段更长的旧原文".padEnd(80, "x");
    node.widgets[0].value = "{a";
    w.eval("getDs")(node);
    ok(String(node.properties.__h3_ds_raw).length === 80,
        "短原文不得覆盖更长的留证，实际长度 " + String(node.properties.__h3_ds_raw).length);
}

/* ---------- 3) 修好之后损坏记录要自动撤销 ---------- */
{
    const { w, node } = env("{oops");
    w.eval("getDs")(node);
    node.widgets[0].value = JSON.stringify({ prompts: ["好了"], segments: [{ seconds: 5 }] });
    const ds = w.eval("getDs")(node);
    ok((ds.prompts || [])[0] === "好了", `修好之后应能读出正文，实际 ${JSON.stringify(ds.prompts)}`);
    ok(w.eval("dsBrokenInfo")() === null, "解析成功后 dsBrokenInfo 必须清空（横幅不该继续亮）");
}

/* ---------- 4) 状态损坏时快照必须让位（不许盖掉上一份好快照） ---------- */
{
    const { w, node } = env("{oops");
    w.eval("getDs")(node);                       // 触发损坏登记
    node.properties.__h3_ds = "OLD-SNAPSHOT";
    const snap = w.eval("safeDsSnapshot")(node);
    ok(snap === "", `损坏时快照必须返回空，实际 ${String(snap).slice(0, 20)}`);
    ok(node.properties.__h3_ds === "OLD-SNAPSHOT", "损坏时的空快照不得覆盖已存的旧快照");
}

/* ---------- 5) 正常状态要出快照，且快照本身可解析 ---------- */
{
    const { w, node } = env(JSON.stringify({ prompts: ["正文"], segments: [{ seconds: 5 }] }));
    const snap = w.eval("safeDsSnapshot")(node);
    ok(!!snap, "正常状态必须有快照");
    const parsed = JSON.parse(String(snap));
    ok((parsed.prompts || [])[0] === "正文", `快照要能还原出正文，实际 ${JSON.stringify(parsed.prompts)}`);
}

console.log(fails ? `\n${fails} 个断言失败` : "\nds_broken_evidence_check 全部通过");
process.exit(fails ? 1 : 0);
