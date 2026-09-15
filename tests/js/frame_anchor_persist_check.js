/* 首尾帧锚的"存活链路"：优化不冲掉、落盘带得上、读档回得来。
 *
 * 用户问的其实是一句话：最后生成时这个锚到底生不生效。答案是生效，但要满足三件事：
 *   1) AI 提示词优化只改 prompts[idx]，不能顺手清掉 segments[idx].frame_img；
 *   2) flushPrompts（save_prompts）的 payload 必须带 frame_img，否则后端 manifest 里没有；
 *   3) 读档 restoreSegField 必须还原 frame_img —— 之前这里漏了，
 *      于是"切一次项目 / 读一次档，整链的段级首尾帧锚全没了"，
 *      而段卡上还显示着旧文件名，观感是"看着有锚、跑起来没锚"。
 * 真正把图钉进画面的是 nodes.py 的 seg_head/end_img_latent（VAE 编码 → keyframe
 * 注入，每步重注入），它只读 ds.segments[i].frame_img，与提示词文本无关。
 */
const { load, mkNode } = require("./_harness.js");

let fails = 0;
function ok(cond, msg) {
    if (cond) return;
    fails += 1;
    console.log("FAIL:", msg);
}

function env() {
    const prompts = ["integrated_multimodal_description: 主体"];
    const segments = [{ seconds: 5, frame_img: { first: "assets/A.png", end: "assets/B.png" } }];
    const manifest = {
        revision: 3,
        assets: [{ file: "assets/A.png", label: "A", kind: "image" },
                 { file: "assets/B.png", label: "B", kind: "image" }],
        prompts, segments, done: 0, finals: [], merges: [],
    };
    const ds = {
        prompts: [...prompts],
        segments: JSON.parse(JSON.stringify(segments)),
        ref_assets: [
            { file: "assets/A.png", kind: "image", label: "A", asset_id: "", roles: [] },
            { file: "assets/B.png", kind: "image", label: "B", asset_id: "", roles: [] },
        ],
    };
    const node = mkNode(ds);
    node.type = "H3SeamlessChainSampler";
    const { w, dom } = load({ node, routes: { "manifest.json": () => manifest } });
    return { w, dom, node };
}

/* ---------- 1) 优化写回不得冲掉锚 ---------- */
{
    const { w, node } = env();
    w.eval("setPromptText")(node, 0, "integrated_multimodal_description: 优化后的文本");
    const seg = w.eval("getDs")(node).segments[0];
    ok(seg.frame_img && seg.frame_img.first === "assets/A.png" && seg.frame_img.end === "assets/B.png",
        `setPromptText 不得动 frame_img，实际 ${JSON.stringify(seg.frame_img)}`);
}

/* ---------- 2) 落盘 payload 必须带 frame_img ---------- */
(async () => {
    const { w, node } = env();
    await w.eval("flushPrompts")(node, "PROJ");
    const call = w.__apiCalls.find((c) => c.path.indexOf("save_prompts") >= 0);
    ok(!!call, "应发出 save_prompts");
    const seg = call && (call.body.segments || [])[0];
    ok(seg && seg.frame_img && seg.frame_img.first === "assets/A.png"
        && seg.frame_img.end === "assets/B.png",
        `save_prompts 必须带 frame_img，实际 ${JSON.stringify(seg && seg.frame_img)}`);
})();

/* ---------- 3) 读档 restoreSegField 必须还原 frame_img ---------- */
{
    const { w } = env();
    const restored = w.eval("restoreSegField")(
        { seconds: 5, frame_img: { first: "assets/A.png", end: "assets/B.png" }, refs: [] });
    ok(restored.frame_img && restored.frame_img.first === "assets/A.png"
        && restored.frame_img.end === "assets/B.png",
        `读档必须还原 frame_img，实际 ${JSON.stringify(restored.frame_img)}`);

    const none = w.eval("restoreSegField")({ seconds: 5 });
    ok(!none.frame_img, "没有 frame_img 的旧存档应保持 null，不造空对象");
}

/* ---------- 4) 总提示词导入不得冲掉锚 ---------- */
{
    const { w, node } = env();
    w.eval("applyMasterPrompt")(node, "【段1】\n提示词：integrated_multimodal_description: 新文本");
    const seg = w.eval("getDs")(node).segments[0];
    ok(seg.frame_img && seg.frame_img.first === "assets/A.png",
        `贴总提示词不得冲掉 frame_img，实际 ${JSON.stringify(seg.frame_img)}`);
    const txt = w.eval("getDs")(node).prompts[0];
    ok(txt.indexOf("0.00-second mark") > 0,
        `双锚段贴完总提示词应自动补回 FL2VA 对齐行，实际：${txt}`);
}

console.log(fails ? `\n${fails} 个断言失败` : "\nframe_anchor_persist_check 全部通过");
process.exit(fails ? 1 : 0);
