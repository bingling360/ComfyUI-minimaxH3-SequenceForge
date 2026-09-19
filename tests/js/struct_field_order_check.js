/* 结构化面板的**字段顺序**必须等于官方 skill 的字段顺序。
 *
 * 跑法：node tests/js/struct_field_order_check.js
 *
 * 为什么单独钉这一条（源码串断言挡不住）：
 *   pytest 里那条只钉 `vbody.append(gSub, gSum, gRet, gShot, gSnd, gSched)` 这一行 ——
 *   组**内容**搬错家（比如把"总结正文"留在主体定义组里）它照样通过。
 *   而用户明说"一定要符合 h3 官方提示词 skill 的格式"，顺序就是格式的一部分：
 *   官方六段式 ①subject_definitions ②summary ③retention_analysis
 *   ④detailed_description ⑤overall_soundscape ⑥non_diegetic_music；
 *   三段式 ①integrated_multimodal_description ②overall_soundscape ③non_diegetic_music。
 *   面板顺序与它不一致时，用户照面板从上填到下就会编译出官方不认的顺序。
 *
 * 所以这里**真渲染一次**，按 DOM 出现的先后断言组标题。
 */
const { load, mkNode } = require("./_harness.js");

let fails = 0;
const ok = (cond, msg) => {
    if (cond) return;
    fails += 1;
    console.log("FAIL " + msg);
};

function groupsOf(withRefs) {
    const prompts = ["integrated_multimodal_description: [Shot 1] 雨夜市场"];
    const segments = [{ seconds: 5, frame_img: null, refs: withRefs ? ["阿依"] : [] }];
    const ds = {
        prompts,
        segments: JSON.parse(JSON.stringify(segments)),
        ref_assets: [{ file: "a.png", kind: "image", label: "阿依", asset_id: "", roles: [] }],
    };
    const node = mkNode(ds);
    const { w, dom } = load({
        node,
        routes: {
            "manifest.json": () => ({
                revision: 1,
                assets: [{ file: "a.png", label: "阿依", kind: "image" }],
                prompts, segments, done: 0, finals: [], merges: [],
            }),
        },
    });
    const box = dom.window.document.createElement("div");
    w.eval("renderPromptV2Panel")(box, node, { ds: w.eval("getDs")(node), node }, 0);
    /* 只取顶层组（.h3d-v2group 的 summary）：镜内「更多」是子组，混进来会污染顺序 */
    return [...box.querySelectorAll(".h3d-v2group > summary")]
        .map((s) => String(s.textContent || "").trim());
}

/* ---- 六段式：官方顺序 ①②③④⑤⑥，且序号要钉在标题上 ---- */
{
    const g = groupsOf(true);
    const order = ["subject_definitions", "summary", "retention_analysis",
        "detailed_description", "overall_soundscape", "non_diegetic_music"];
    const idx = order.map((f) => g.findIndex((t) => t.indexOf(f) >= 0));
    ok(idx.every((i) => i >= 0), `六段式缺官方字段：${JSON.stringify(g)}`);
    /* ①②③④ 各自独立成组 → 严格递增；⑤⑥ 合在「声音」一组 → 同一下标 */
    ok(idx[0] < idx[1] && idx[1] < idx[2] && idx[2] < idx[3],
        `六段式 ①→④ 必须是四个独立组且按官方顺序，实际下标 ${JSON.stringify(idx)}`);
    ok(idx[4] > idx[3] && idx[4] === idx[5],
        `⑤环境音 与 ⑥背景配乐 同属声音组、且排在 ④ 之后，实际下标 ${JSON.stringify(idx)}`);
    /* 序号可见：①..⑥ 应该分别出现在对应组标题里 */
    const marks = ["①", "②", "③", "④", "⑤", "⑥"];
    marks.forEach((m, i) => {
        const line = g.find((t) => t.indexOf(order[i]) >= 0) || "";
        ok(line.indexOf(m) >= 0, `六段式第 ${i + 1} 段标题应带序号 ${m}，实际：${line}`);
    });
}

/* ---- 三段式：只有 ④⑤⑥ 三段有序号（前三段不参与编译） ---- */
{
    const g = groupsOf(false);
    const order = ["integrated_multimodal_description", "overall_soundscape",
        "non_diegetic_music"];
    const idx = order.map((f) => g.findIndex((t) => t.indexOf(f) >= 0));
    ok(idx.every((i) => i >= 0), `三段式缺官方字段：${JSON.stringify(g)}`);
    ok(idx[0] < idx[1] && idx[1] === idx[2],
        `三段式：①整体描述在前，②环境音 与 ③背景配乐 同组在后，实际 ${JSON.stringify(idx)}`);
    ["①", "②", "③"].forEach((m, i) => {
        const line = g.find((t) => t.indexOf(order[i]) >= 0) || "";
        ok(line.indexOf(m) >= 0, `三段式第 ${i + 1} 段标题应带序号 ${m}，实际：${line}`);
    });
    /* 前三段（主体定义/总结/保留分析）在两段式里要**明说没参与**，不能闷声挂着 */
    ok(g.some((t) => t.indexOf("subject_definitions") >= 0 && t.indexOf("三段式无此段") >= 0),
        "三段式应注明「主体定义」不参与编译");
}

/* ---- 素材调度不是官方字段：必须单独成组并在标题里点明 ---- */
{
    const g = groupsOf(true);
    const i = g.findIndex((t) => t.indexOf("素材调度") >= 0);
    ok(i >= 0, "应有「素材调度」组");
    ok(i === g.length - 1, "素材调度应放最后（它不是官方字段）");
    ok(g[i].indexOf("非官方字段") >= 0, `素材调度标题应点明非官方字段，实际：${g[i]}`);
}

console.log(fails ? `\n${fails} 个断言失败` : "\nstruct_field_order_check 全部通过");
process.exit(fails ? 1 : 0);
