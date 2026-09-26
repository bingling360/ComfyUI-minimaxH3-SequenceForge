/* 清除/更换段级首尾帧锚 → 整张段卡（含具象化面板的对齐预览）必须跟着变。
 *
 * 根因（用户报的"清除了，具象化那里不会跟着一起清除"）：
 *   cardsSignature 的 frames 只放了**全局** ds.first_frame / ds.end_frame 两个旧槽位，
 *   没放段级 seg.frame_img。段级锚是现在唯一的入口，改锚后签名不变 →
 *   renderCenterColumn 认为"没变化"→ 段卡不重建 → 具象化面板顶部的对齐指令预览、
 *   模式判定行、锚点区全部停在旧状态，只有切屏（签名因别的原因变化）才刷新。
 *
 * 同时锁锚点编号映射：对齐指令是官方固定句，只能写 <Picture k> 编号，
 * 这里验证编号 ↔ 首尾帧的映射与后端 prompts.compile_segment 同口径。
 */
const { load, mkNode } = require("./_harness.js");

let fails = 0;
function ok(cond, msg) {
    if (cond) return;
    fails += 1;
    console.log("FAIL:", msg);
}

const I2 = "For the target video, at 0.00 seconds into the target video, "
    + "<Picture 1> (from [Shot 1]) is fully referenced.";

function envWith(frameImg) {
    const prompts = [`integrated_multimodal_description: ${I2}\n主体`];
    const segments = [{ seconds: 5, frame_img: frameImg }];
    const manifest = {
        revision: 3, assets: [{ file: "assets/A.png", label: "A", kind: "image" }],
        prompts, segments, done: 0, finals: [], merges: [],
    };
    const ds = {
        prompts,
        segments: JSON.parse(JSON.stringify(segments)),
        ref_assets: [{ file: "assets/A.png", kind: "image", label: "A", asset_id: "", roles: [] }],
    };
    const node = mkNode(ds);
    node.type = "H3SeamlessChainSampler";
    const { w, dom } = load({ node, apiRoutes: { "/h3chain/project": () => ({ ok: true, manifest }) } });
    return { w, dom, node };
}

/* ---------- 1) 签名必须含段级 frame_img ---------- */
{
    const a = envWith({ first: "assets/A.png" });
    const b = envWith(null);
    const sigOf = (e) => e.w.eval("cardsSignature")({
        state: {}, plan: [{ kind: "prompt", idx: 0 }], ds: e.w.eval("getDs")(e.node), mf: {}, node: e.node,
    });
    ok(sigOf(a) !== sigOf(b),
        "有锚 vs 无锚的段卡签名必须不同（否则清锚后段卡不重建）");

    const c = envWith({ first: "assets/B.png" });
    ok(sigOf(a) !== sigOf(c), "换了另一张图签名也要变");
    const d = envWith({ end: "assets/A.png" });
    ok(sigOf(a) !== sigOf(d), "首帧锚 vs 尾帧锚签名也要变");
}

/* ---------- 2) 编号映射与后端同口径 ---------- */
{
    const e = envWith({ first: "assets/A.png", end: "assets/B.png" });
    const nums = e.w.eval("framePictureNumbers");
    const ds = e.w.eval("getDs")(e.node);
    let m = nums(ds, 0);
    ok(m.mode === "FL2VA", `双锚应判 FL2VA，实际 ${m.mode}`);
    ok(m.first === 1 && m.end === 2, `FL2VA：首帧=P1 尾帧=P2，实际 ${m.first}/${m.end}`);

    const e2 = envWith({ first: "assets/A.png" });
    m = nums(e2.w.eval("getDs")(e2.node), 0);
    ok(m.mode === "I2VA" && m.first === 1 && m.end === 0,
        `仅首帧应 I2VA 且只有 P1，实际 ${m.mode} ${m.first}/${m.end}`);

    const e3 = envWith({ end: "assets/B.png" });
    m = nums(e3.w.eval("getDs")(e3.node), 0);
    ok(m.mode === "L2VA" && m.end === 1 && m.first === 0,
        `仅尾帧应 L2VA 且尾帧=P1，实际 ${m.mode} ${m.first}/${m.end}`);

    const e4 = envWith(null);
    m = nums(e4.w.eval("getDs")(e4.node), 0);
    ok(m.mode === "T2VA" && !m.first && !m.end, `无锚应 T2VA 无编号，实际 ${m.mode}`);
}


/* ---------- 3) 端到端：清锚后段卡要跟着变；对齐指令**不再**在前端露出 ----------
 * 结构化提示词已下线（弹窗与 .h3d-v2out 都没了）；再往后「对齐指令预览」
 * （mkAlignPreview / .h3d-alignprev）也整块删掉了 —— 它只是"提交时会自动拼什么"
 * 的只读预览，用户改不了也用不上，占着锚定栏一整行，现在统一由后端注入。
 * 所以这里改成**反向断言**：DOM 里不许再出现对齐指令预览，正文里的对齐句
 * 也不许被前端渲染出来。清锚后的联动改看首尾帧按钮（.h3d-frm）。 */
const alignNodes = (doc) => [...doc.querySelectorAll(".h3d-alignprev")];

(async () => {
    const { w, dom, node } = envWith({ first: "assets/A.png" });
    const doc = w.document;
    w.eval("openDesk")();
    await new Promise((r) => setTimeout(r, 60));

    /* 前端不再渲染对齐指令预览（后端注入，不占 UI） */
    ok(alignNodes(doc).length === 0,
        `锚定栏不得再渲染 .h3d-alignprev（对齐指令已由后端注入），实际找到 `
        + `${alignNodes(doc).length} 个`);
    const bodyText = doc.body.textContent || "";
    ok(bodyText.indexOf("0.00-second mark") < 0,
        "对齐指令不得出现在前端任何可见文本里（后端注入即可）");

    /* 清锚 */
    w.eval("setSegmentFrameImg")(node, 0, "first", "");
    await w.eval("refresh")();
    await new Promise((r) => setTimeout(r, 30));

    ok(alignNodes(doc).length === 0, "清锚后也不得出现对齐指令预览");
    const frBtn = [...doc.querySelectorAll(".h3d-frm")].map((b) => b.textContent).join(" | ");
    ok(frBtn.indexOf("A.png") < 0, `清锚后按钮不得再显示文件名，实际：${frBtn}`);

    console.log(fails ? `\n${fails} 个断言失败` : "\nframe_anchor_sync_check 全部通过");
    process.exit(fails ? 1 : 0);
})();
