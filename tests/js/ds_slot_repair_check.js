/* 「导演台状态」槽位独立校验（P0-3）：切工作流丢提示词最隐蔽的一条。
 *
 * 背景：widgets_values 按位序列化，ds 恒在第 27 位。旧判据只看「长度 30 + 第 0 位
 * 是字符串」就宣布布局正确，从不检查第 27 位；而迁移层一旦认错，会把 1.0
 * （响度对齐强度）或 "match"（参考图像尺寸）顶进 ds 槽位。getDs 的 JSON.parse
 * 失败后静默回 defaultDs() → 整链提示词"凭空消失"，且不报错。
 *
 * 本用例钉四件事：
 *   1) 30 值布局只修第 27 位，其余 29 位一位不动；
 *   2) 第 27 位坏且没有快照 → 置 ""（不塞数字/别的控件值）+ 留 __h3_ds_missing；
 *   3) 有 properties.__h3_ds 快照 → 优先用它（按名字，不按位）；
 *   4) 旧 35 值布局重排后，ds 仍能找回（尾部 / 快照 / 全数组倒扫）；
 *   5) 正常存档零改动（引用都不换）。
 */
const { load } = require("./_harness.js");

let fails = 0;
function ok(cond, msg) {
    if (cond) return;
    fails += 1;
    console.log("FAIL:", msg);
}

const DS_JSON = JSON.stringify({
    prompts: ["第一段", "第二段"],
    segments: [{ seconds: 5 }, { seconds: 6 }],
    ref_assets: [],
});

/* 30 值布局：第 0 位宽高比字符串，其余用可辨识的数字/字符串 */
function curLayout(fill) {
    const wv = Array.from({ length: 30 }, (_v, i) => i);
    wv[0] = "16:9";
    wv[1] = 1;
    wv[2] = 1376;
    wv[3] = 768;
    wv[27] = fill;
    wv[28] = "match";
    wv[29] = 1.0;
    return wv;
}

const { w } = load({ node: null });
const migrate = w.eval("migrateGraphWidgets");
const parseDs = w.eval("parseDsValue");
const pickDs = w.eval("pickDsJson");

/* ---------- 0) 纯函数边界 ---------- */
ok(parseDs(DS_JSON) && parseDs(DS_JSON).prompts.length === 2, "合法 ds JSON 必须被识别");
ok(parseDs("match") === null, "\"match\"（参考图像尺寸）不是 ds");
ok(parseDs("1") === null, "数字字符串不是 ds");
ok(parseDs(1.0) === null, "number 不是 ds");
ok(parseDs("") === null, "空串不是 ds");
ok(parseDs("{bad") === null, "坏 JSON 不是 ds");

/* ---------- 1) 30 值 + 第 27 位是数字：只修这一位 ---------- */
{
    const wv = curLayout(1.0);
    const g = { nodes: [{ type: "H3SeamlessChainSampler", widgets_values: wv, properties: {} }] };
    migrate(g);
    const out = g.nodes[0].widgets_values;
    ok(out.length === 30, `长度必须仍是 30，实际 ${out.length}`);
    ok(out[27] === "", `无快照时第 27 位应置空，实际 ${JSON.stringify(out[27])}`);
    let same = true;
    for (let i = 0; i < 30; i += 1) if (i !== 27 && out[i] !== wv[i]) same = false;
    ok(same, "其余 29 位必须逐位不变（不许因为 ds 坏了就整条重排）");
    ok(g.nodes[0].properties.__h3_ds_missing === true, "查不到时必须留 __h3_ds_missing 标记");
}

/* ---------- 2) 30 值 + 坏槽位 + 快照：用快照回填 ---------- */
{
    const wv = curLayout(1.0);
    const g = { nodes: [{ type: "H3SeamlessChainSampler", widgets_values: wv,
        properties: { __h3_ds: DS_JSON } }] };
    migrate(g);
    ok(g.nodes[0].widgets_values[27] === DS_JSON, "有 properties 快照时必须用它回填");
    ok(g.nodes[0].widgets_values[0] === "16:9", "画幅等其它位不得被动过");
}

/* ---------- 3) 30 值 + 坏槽位 + 数组里别处藏着真身：倒扫找回 ---------- */
{
    const wv = curLayout("match");
    wv[20] = DS_JSON;                       // 布局漂移：真身留在别的下标
    const g = { nodes: [{ type: "H3SeamlessChainSampler", widgets_values: wv, properties: {} }] };
    migrate(g);
    ok(g.nodes[0].widgets_values[27] === DS_JSON, "数组里能找到真身时应回填到第 27 位");
    ok(g.nodes[0].widgets_values[20] === DS_JSON, "回填不得清掉原位置的值");
}

/* ---------- 4) 正常 30 值存档：零改动（连数组引用都不换） ---------- */
{
    const wv = curLayout(DS_JSON);
    const g = { nodes: [{ type: "H3SeamlessChainSampler", widgets_values: wv, properties: {} }] };
    migrate(g);
    ok(g.nodes[0].widgets_values === wv, "合法存档不得被复制/重写（否则会误触 graph dirty）");
    ok(g.nodes[0].widgets_values[27] === DS_JSON, "合法存档的 ds 必须原样保留");
    ok(!g.nodes[0].properties.__h3_ds_missing, "合法存档不得被打上 missing 标记");
}

/* ---------- 5) 旧 35 值布局：重排后 ds 从尾部取回 ---------- */
{
    const wv = Array.from({ length: 35 }, (_v, i) => i);
    wv[0] = "16:9";
    wv[34] = DS_JSON;
    const g = { nodes: [{ type: "H3SeamlessChainSampler", widgets_values: wv, properties: {} }] };
    migrate(g);
    const out = g.nodes[0].widgets_values;
    ok(out.length === 30, `35 值应重排成 30 值，实际 ${out.length}`);
    ok(out[27] === DS_JSON, `重排后第 27 位应仍是原 ds，实际 ${String(out[27]).slice(0, 20)}`);
}

/* ---------- 6) 极老 25 值布局（本来就没有 ds）：置空 + 留痕，不许塞数字 ---------- */
{
    const wv = Array.from({ length: 25 }, (_v, i) => i + 100);
    wv[0] = 1280;                           // 极老布局首项是宽度（数字）
    const g = { nodes: [{ type: "H3SeamlessChainSampler", widgets_values: wv, properties: {} }] };
    migrate(g);
    const out = g.nodes[0].widgets_values;
    ok(out.length === 30, `25 值应补成 30 值，实际 ${out.length}`);
    ok(out[27] === "", `找不到 ds 时必须置空（绝不能把数字塞进状态位），实际 ${JSON.stringify(out[27])}`);
    ok(g.nodes[0].properties.__h3_ds_missing === true, "极老布局也要留 missing 标记");
}

/* ---------- 7) pickDsJson 优先级：第 27 位 > 快照 > 倒扫 ---------- */
{
    const wv = curLayout(DS_JSON);
    wv[20] = JSON.stringify({ prompts: ["别的"], segments: [] });
    ok(pickDs(wv, { __h3_ds: JSON.stringify({ prompts: ["快照"], segments: [] }) }) === DS_JSON,
        "第 27 位有效时必须优先用它（快照只是兜底，不能反客为主）");
}

console.log(fails ? `\n${fails} 个断言失败` : "\nds_slot_repair_check 全部通过");
process.exit(fails ? 1 : 0);
