/* 素材定义（段级 / 项目级）端到端（jsdom 真跑整个 h3_director.js）。
 *
 * 钉的是「定义**只改 refs 的顺序与集合**，不改编号算法」这条设计：
 *  · refs 顺序 = 槽位号顺序 → 后端按顺序累加出来的 <Picture k> 就是定义的号；
 *  · 定义过的素材**正文里没有 @** 也进 seg.refs（不进就不挂，图根本不会被送进去）；
 *  · 段级 > 项目级；段级还能把一条项目定义「不用」掉；
 *  · 号越界 / 撞号一律回落自动，绝不产生悬空号（正文写 <Picture 5> 而本段只有 3 张图）；
 *  · Define 行（文本）与 defs（JSON）是同一套语义，两个入口都认。
 *
 * 跑法：NODE_PATH=<repo>/node_modules node tests/js/ref_defs_check.js
 */
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");

let harness;
try {
    harness = require("./_harness.js");
} catch (e) {
    console.error("SKIP: 未安装 jsdom（npm install）");
    process.exit(2);
}

const fails = [];
const ok = (cond, msg) => { if (!cond) fails.push(msg); };

const POOL = [
    { label: "猫", ref_name: "猫.png", kind: "image", file: "assets/cat.png" },
    { label: "狗", ref_name: "狗.png", kind: "image", file: "assets/dog.png" },
    { label: "门", ref_name: "门.png", kind: "image", file: "assets/door.png" },
    { label: "景", ref_name: "景.mp4", kind: "video", file: "assets/v.mp4" },
];
const mkDs = () => ({
    prompts: ["", ""],
    segments: [{}, {}],
    ref_assets: POOL.map((a) => ({ ...a })),
});

const { w, errors } = harness.load({ node: harness.mkNode(mkDs()) });
const node = w.findNode();
ok(!!node, "findNode() 拿不到节点");

/* ---------- 1. 没有定义时，行为与旧版逐字一致 ---------- */
{
    const ds = mkDs();
    const r = w.refsForSegment(ds, 0, ["猫.png", "狗.png"]);
    ok(r.join(",") === "猫.png,狗.png", "无定义时应保持正文派生顺序：" + r.join(","));
}

/* ---------- 2. 段级定义改号 = 改 refs 顺序 ---------- */
{
    const ds = mkDs();
    ds.segments[0].ref_defs = { "猫.png": 2 };
    const r = w.refsForSegment(ds, 0, ["猫.png", "狗.png"]);
    ok(r.join(",") === "狗.png,猫.png",
        "猫定义为第 2 位 → refs 应变成 [狗,猫]（后端按顺序编号）：" + r.join(","));
}

/* ---------- 3. 项目定义：正文里没有 @ 也要挂上 ---------- */
{
    const ds = mkDs();
    ds.ref_defs = { "猫.png": 1 };
    const r = w.refsForSegment(ds, 0, []);
    ok(r.join(",") === "猫.png", "项目定义应把素材挂进 refs（正文空也一样）：" + r.join(","));
}

/* ---------- 4. 段级覆盖项目级（同一个素材） ---------- */
{
    const ds = mkDs();
    ds.ref_defs = { "猫.png": 1 };
    ds.segments[0].ref_defs = { "猫.png": 3 };
    const r = w.refsForSegment(ds, 0, ["狗.png", "门.png"]);
    ok(r.join(",") === "狗.png,门.png,猫.png",
        "段级把猫改成第 3 位 → 应排到最后：" + r.join(","));
}

/* ---------- 5. 段级「不用」：只挡项目定义，不挡正文里显式 @ ---------- */
{
    const ds = mkDs();
    ds.ref_defs = { "猫.png": 1 };
    ds.segments[0].ref_skip = ["猫.png"];
    ok(w.refsForSegment(ds, 0, []).length === 0, "「不用」应把项目定义挡掉");
    const r2 = w.refsForSegment(ds, 0, ["猫.png"]);
    ok(r2.join(",") === "猫.png", "正文里显式 @ 过的，不受「不用」影响：" + r2.join(","));
}

/* ---------- 6. 撞号：段级先占，项目级回落（不产生两个第 1 号） ---------- */
{
    const ds = mkDs();
    ds.ref_defs = { "猫.png": 1 };
    ds.segments[0].ref_defs = { "狗.png": 1 };
    const r = w.refsForSegment(ds, 0, ["猫.png", "狗.png"]);
    ok(r.join(",") === "狗.png,猫.png",
        "段级定义（狗=1）应先占号，项目级（猫=1）回落成第 2：" + r.join(","));
    ok(new Set(r).size === r.length, "同一素材不该在 refs 里重复");
}

/* ---------- 7. 号越界 → 回落自动（绝不出现悬空 <Picture 5>） ---------- */
{
    const ds = mkDs();
    ds.segments[0].ref_defs = { "猫.png": 5 };     // 本段只有 2 张图
    const r = w.refsForSegment(ds, 0, ["猫.png", "狗.png"]);
    ok(r.join(",") === "猫.png,狗.png", "越界定义应被忽略、顺序回到原样：" + r.join(","));
}

/* ---------- 8. 跨类型互不干扰（视频的号不影响图片） ---------- */
{
    const ds = mkDs();
    ds.segments[0].ref_defs = { "景.mp4": 1 };
    const r = w.refsForSegment(ds, 0, ["猫.png", "景.mp4", "狗.png"]);
    ok(r.join(",") === "猫.png,景.mp4,狗.png",
        "视频定义不该改变图片之间的相对顺序：" + r.join(","));
}

/* ---------- 9. Define 行：文本通道解析（越界 / 格式错都要有点名） ---------- */
{
    const p = w.parseMasterPrompt("[Segment 1]\nDuration: 8\n"
        + "Define: Picture 2 = 猫.png\nDefine: Video 1 = 景.mp4\n"
        + "Prompt:\nintegrated_multimodal_description: [Shot 1] x\n\n[END]");
    const d = p.segs[0] && p.segs[0].defs;
    ok(!!d && d["猫.png"] === 2 && d["景.mp4"] === 1,
        "Define 行没解析成定义：" + JSON.stringify(d));
    ok(String(p.segs[0].main || "").indexOf("Define") < 0, "Define 行不该混进正文");
    const bad = w.parseMasterPrompt("[Segment 1]\nDefine: 猫 = 1\nPrompt:\nx\n\n[END]");
    ok((bad.notes || []).some((n) => n.indexOf("Define") >= 0),
        "格式不对的 Define 要提示：" + JSON.stringify(bad.notes));
}

/* ---------- 10. Define 行渲染：导出带上定义（导出→贴回不丢号） ---------- */
{
    const txt = w.mpRenderState([{ main: "x", seconds: 8, unlink: false,
        defs: { "猫.png": 2 } }], 1, POOL);
    ok(txt.indexOf("Define: Picture 2 = 猫.png") >= 0, "导出缺 Define 行：\n" + txt);
    ok(txt.indexOf("Define:") < txt.indexOf("Prompt:"), "Define 必须写在 Prompt 之前：\n" + txt);
    /* 没有定义的段一行都不多写（旧档导出逐字不变） */
    const plain = w.mpRenderState([{ main: "x", seconds: 8, unlink: false }], 1, POOL);
    ok(plain.indexOf("Define:") < 0, "无定义时不该出现 Define 行：\n" + plain);
}

/* ---------- 11. JSON defs：与 Define 行同义 ---------- */
{
    const p = w.parseMasterPromptJson(JSON.stringify({
        segments: [{ index: 1, duration: 5, prompt: "正文",
            defs: { "Picture 1": "猫.png", "Video 1": "景.mp4" } }],
    }));
    ok(p.ok === true, "JSON 解析失败：" + JSON.stringify(p.errors || p.error));
    const d = p.segs[0] && p.segs[0].defs;
    ok(!!d && d["猫.png"] === 1 && d["景.mp4"] === 1,
        "JSON defs 没解析成定义：" + JSON.stringify(d));
    const bad = w.parseMasterPromptJson(JSON.stringify({
        segments: [{ prompt: "x", defs: { "Subject 1": "猫.png" } }],
    }));
    ok(!(bad.segs[0] && bad.segs[0].defs), "Subject 不属于素材编号空间，应被忽略");
}

/* ---------- 12. 写入口：项目定义自动挂到每一段（含之后新增的） ---------- */
{
    const ds = mkDs();
    const n2 = harness.mkNode(ds);
    w.app.graph._nodes = [n2];
    w.setProjRefDef(n2, "猫.png", 1);
    const st = w.getDs(n2);
    ok(st.ref_defs && st.ref_defs["猫.png"] === 1, "项目定义没落进 ds：" + JSON.stringify(st.ref_defs));
    for (const i of [0, 1]) {
        ok((st.segments[i].refs || []).includes("猫.png"),
            `项目定义应自动挂到第 ${i + 1} 段：` + JSON.stringify(st.segments[i].refs));
    }
    /* 新加的段（走 defaultSegment + 一次 sync）也应带上 */
    st.prompts.push("");
    st.segments.push(w.defaultSegment());
    w.syncRefsFromText(st, 2, "");
    ok((st.segments[2].refs || []).includes("猫.png"), "新增段也要自动挂上项目定义");
}

/* ---------- 13. 写入口：段级定义 / 不用 会改这一段的 refs ---------- */
{
    const ds = mkDs();
    const n3 = harness.mkNode(ds);
    w.app.graph._nodes = [n3];
    const st0 = w.getDs(n3);
    st0.prompts = ["@狗.png", ""];
    w.setDs(n3, st0);
    w.setSegRefDef(n3, 0, "狗.png", 2);
    const st1 = w.getDs(n3);
    ok((st1.segments[0].ref_defs || {})["狗.png"] === 2, "段级定义没落库");
    w.setSegRefSkip(n3, 0, "狗.png", true);
    const st2 = w.getDs(n3);
    ok((st2.segments[0].ref_skip || []).includes("狗.png"), "段级「不用」没落库");
}

/* ---------- 14. 定义入口：段卡下拉 + 独立的「项目定义」弹窗 ---------- */
{
    const fs = require("fs");
    const src = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8");
    ok(/h3d-chipsel/.test(src), "引用条 chip 旁应有编号下拉（.h3d-chipsel）");
    ok(!/openRefDefsDialog/.test(src), "旧的「本段+项目」二合一面板应已删除");
    ok(/🌐 项目定义/.test(src), "缺少「项目定义」按钮");
    ok(/function openProjRefDefsDialog\(/.test(src), "缺少项目定义弹窗");
    /* ★ 引用即定义：点 chip 的 mousedown 里必须顺手写一条编号定义，
     * 不然"引用"和"定义"还是两个动作（下拉一直显示「自动」） */
    ok(src.indexOf("if (autoN) setSegRefDef(node, segIdx, key, autoN);") >= 0,
        "点 chip 引用时应自动写编号定义（引用即定义）");
    /* ★ 引用条的数据源必须走 refsForSegment：只读正文的话，项目定义挂上的
     * 素材在条上根本不出现（用户报的"项目定义了却没被引用"） */
    ok(/refsForSegment\(getDs\(node\), it\.idx, fromText\)/.test(src),
        "引用条数据源必须走 refsForSegment（正文 ∪ 段定义 ∪ 项目定义）");

    /* 点 chip = 引用 + 自动定义（号 = 该类型已有引用数 + 1），这里是数据层等价调用 */
    const ds = mkDs();
    const n5 = harness.mkNode(ds);
    w.app.graph._nodes = [n5];
    w.setSegRefDef(n5, 0, "猫.png", 1);
    const st0 = w.getDs(n5);
    ok((st0.segments[0].refs || []).includes("猫.png"), "定义后应立刻进 refs（引用即定义）");

    /* 项目定义弹窗：列全部池素材，选号后全项目生效 */
    w.openProjRefDefsDialog(n5, () => {});
    const box = w.document.querySelector(".h3d-defbox");
    ok(!!box, "项目定义弹窗没打开（缺 .h3d-defbox）");
    const rows = box ? [...box.querySelectorAll(".h3d-defrow:not(.h3d-defhead)")] : [];
    ok(rows.length === POOL.length, `项目定义应列全部 ${POOL.length} 个素材，实际 ${rows.length}`);
    ok(rows.every((r) => r.querySelector(".h3d-defthumb")), "每行都该有缩略图位");
    const sels = box ? [...box.querySelectorAll(".h3d-defsel")] : [];
    /* 编号空间按**官方单段上限**给：图 9 / 视 3 / 音 3（不是"池里有几张"） */
    ok(sels[0] && sels[0].options.length === 1 + 9,
        `图片编号应给到 9 档（官方上限），实际 ${sels[0] ? sels[0].options.length - 1 : 0}`);
    if (sels.length) {
        sels[0].value = "1";
        sels[0].onchange();
        const st1 = w.getDs(n5);
        ok((st1.ref_defs || {})["猫.png"] === 1, "弹窗里选号没写进项目定义");
        ok((st1.segments[1].refs || []).includes("猫.png"), "第 2 段应自动获得该项目定义");
    }
    /* 清空：项目定义消失，段级定义不受影响 */
    w.clearProjRefDefs(n5);
    const st2 = w.getDs(n5);
    ok(Object.keys(st2.ref_defs || {}).length === 0, "清空项目定义失败");
    ok(!(st2.segments[1].refs || []).includes("猫.png"), "清空后第 2 段不该再有它");
    ok((st2.segments[0].ref_defs || {})["猫.png"] === 1, "清空项目定义不该动段级定义");
}

const real = (errors || []).filter((e) => !/Not implemented|jsdom/i.test(String(e)));
ok(real.length === 0, "运行时报错：" + JSON.stringify(real));

if (fails.length) {
    console.error("FAIL:\n - " + fails.join("\n - "));
    process.exit(1);
}
console.log("OK: 素材定义（段级/项目级）= 改 refs 顺序与集合，不碰编号算法");
