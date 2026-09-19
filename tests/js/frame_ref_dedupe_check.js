/* 同一张图既是「首尾帧锚」又是「被 @引用的参考素材」时，只许占一个位。
 *
 * 这是个真实用法：用户就是想让画面从这张图开始、并且长得像它。于是同一张图会
 * 从两条路进来 —— 帧锚（frame_img.first/end）和参考素材（seg.refs）。
 * 不去重的后果：
 *   ① 编号侧：同一张图占 <Picture 1> 和 <Picture 2> 两个号，正文里手写的
 *      <Picture N> 和它指的东西对不上；
 *   ② 给 LLM 的 media 里出现两张一模一样的图、两个同名的 @图片1 —— 模型收到
 *      编号 1、2 却长得一样，会以为自己看错，描述就写歪了；
 *   ③ 白占一个官方 9 图名额。
 *
 * jsdom 真跑：抽 v2RefsFromSchedule / collectSegMedia，桩掉图片抓取。
 */
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");

let JSDOM;
try {
    ({ JSDOM } = require("jsdom"));
} catch (e) {
    console.error("SKIP: 未安装 jsdom");
    process.exit(2);
}

const src = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8");

function extractFn(name) {
    const re = new RegExp("^\\s*(?:async\\s+)?function " + name + "\\(", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到函数 " + name);
    const i = src.indexOf("{", m.index);
    let depth = 0;
    for (let j = i; j < src.length; j++) {
        if (src[j] === "{") depth++;
        else if (src[j] === "}") { depth--; if (!depth) return src.slice(m.index, j + 1); }
    }
    throw new Error("大括号不配平 " + name);
}

function extractConst(name) {
    const re = new RegExp("^\\s*const " + name + " = [\\s\\S]*?;\\s*$", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到常量 " + name);
    return m[0];
}

const dom = new JSDOM("<!doctype html><html><body></body></html>", { runScripts: "outside-only" });
dom.window.eval(`
    /* 图片抓取桩：按 url 后缀回一个假的 data URL（不去真的下载） */
    async function optImageToDataUrl(url) { return url ? "data:image/png;base64," + url : null; }
    function assetPreviewUrl(dir, file, aid) { return "PREV:" + String(file || ""); }
    function getDirValue() { return "PROJ"; }
`);
dom.window.eval([
    extractConst("refKeyOf"),
    extractConst("KIND_LIST"),
    extractConst("KIND_TOKEN"),
    extractFn("frameNameOf"),
    extractFn("markTextOf"),
    extractFn("v2RefsFromSchedule"),
    extractFn("collectSegMedia"),
    ";window.KIND_LIST = KIND_LIST; window.KIND_TOKEN = KIND_TOKEN;",
].join("\n"));

const { v2RefsFromSchedule, collectSegMedia } = dom.window;

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

/* 池：a.png（女主，标注 图片1）、b.png（街道，标注 图片2） */
const POOL = [
    { file: "assets/a.png", kind: "image", label: "女主", ref_name: "女主.png", mark: "图片1", asset_id: "" },
    { file: "assets/b.png", kind: "image", label: "街道", ref_name: "街道.jpg", mark: "图片2", asset_id: "" },
];

async function main() {
    /* ---------- 1) 编号：帧锚和参考是同一张 → 只占一个号 ---------- */
    {
        const ds = {
            prompts: ["x"],
            ref_assets: POOL,
            segments: [{ frame_img: { first: "assets/a.png" }, refs: ["女主.png"] }],
        };
        const out = v2RefsFromSchedule(ds, 0);
        ok(out.length === 1, "同一张图（帧锚+参考）只占一个 <Picture> 号", out);
        ok(out[0] && out[0].label === "<Picture 1>",
            "它占的是 1 号（帧锚排最前）", out[0] && out[0].label, "<Picture 1>");
        ok(out[0] && out[0].src === "女主.png", "指向同一张素材", out[0] && out[0].src);
    }

    /* ---------- 2) 编号：首帧和尾帧是同一张 → 也只占一个号 ---------- */
    {
        const ds = {
            prompts: ["x", "y"],
            ref_assets: POOL,
            segments: [{ frame_img: { first: "assets/a.png", end: "assets/a.png" }, refs: [] }],
        };
        const out = v2RefsFromSchedule(ds, 0);
        ok(out.length === 1, "首帧=尾帧=同一张 → 只占一个号", out);
    }

    /* ---------- 3) 编号：帧锚 + 另一张素材 → 两个号，素材顺延为 2 ---------- */
    {
        const ds = {
            prompts: ["x"],
            ref_assets: POOL,
            segments: [{ frame_img: { first: "assets/a.png" }, refs: ["街道.jpg"] }],
        };
        const out = v2RefsFromSchedule(ds, 0);
        ok(out.length === 2, "帧锚 + 另一张素材 → 两个号", out);
        ok(out[1] && out[1].label === "<Picture 2>", "素材顺延为 2 号",
            out[1] && out[1].label, "<Picture 2>");
    }

    /* ---------- 4) 给 LLM 的 media：同一张图不许挂两次 ---------- */
    {
        const ds = {
            prompts: ["x"],
            ref_assets: POOL,
            segments: [{ frame_img: { first: "assets/a.png" }, refs: ["女主.png"] }],
        };
        const r = await collectSegMedia(null, ds, 0);
        ok(r.media.length === 1, "media 里同一张图只挂一次（帧锚+参考是同一张）", r.media);
        ok(r.media[0] && r.media[0].label === "图片1", "它的标注是 图片1", r.media[0] && r.media[0].label);
        /* @图片1 出现 **2** 次是对的：一次列进「可用素材」名单（LLM 要知道能引用
         * 它）、一次在锚点说明里点性质（"其中 @图片1 是首尾帧锚点"）。
         * 没去重时 media 两张 → 名单里会列两次 + 锚点一次 = **3** 次，
         * 所以这个计数正好能挡住重复挂载回归。 */
        const n1 = (r.note.match(/@图片1/g) || []).length;
        ok(n1 === 2, "随图说明里 @图片1 出现 2 次（名单一次 + 锚点一次）", n1, 2);
        /* 它是锚，说明里必须点明"硬钉"，不能只当普通参考 */
        ok(/锚点/.test(r.note), "说明里要点明它是锚点", r.note);
    }

    /* ---------- 5) media：首帧与尾帧同一张 → 也只挂一次 ---------- */
    {
        const ds = {
            prompts: ["x", "y"],
            ref_assets: POOL,
            segments: [{ frame_img: { first: "assets/a.png", end: "assets/a.png" }, refs: [] }],
        };
        const r = await collectSegMedia(null, ds, 0);
        ok(r.media.length === 1, "首帧=尾帧=同一张 → media 只挂一次", r.media);
    }

    /* ---------- 6) media：帧锚 + 另一张素材 → 两张都要挂 ---------- */
    {
        const ds = {
            prompts: ["x"],
            ref_assets: POOL,
            segments: [{ frame_img: { first: "assets/a.png" }, refs: ["街道.jpg"] }],
        };
        const r = await collectSegMedia(null, ds, 0);
        ok(r.media.length === 2, "帧锚 + 另一张素材 → 挂两张", r.media);
        ok(r.media[0].label === "图片1" && r.media[1].label === "图片2",
            "顺序：帧锚在前、素材顺延",
            r.media.map((m) => m.label));
    }

    /* ---------- 7) 只有参考、没有帧锚 → 正常编号从 1 开始 ---------- */
    {
        const ds = {
            prompts: ["x"],
            ref_assets: POOL,
            segments: [{ refs: ["女主.png", "街道.jpg"] }],
        };
        const out = v2RefsFromSchedule(ds, 0);
        ok(out.length === 2 && out[0].label === "<Picture 1>" && out[1].label === "<Picture 2>",
            "无帧锚时素材从 1 号开始", out.map((o) => o.label));
    }

    /* ---------- 8) note 措辞按首/尾角色区分（用户报的真实 bug）----------
     *
     * 用户截图：只设了尾帧图，但 AI 写成"以 X 为首帧与结尾定格"。
     * 根因有二：① 后端把同一张图同时挂到链级首+尾（库 roles 残留）—— 已修；
     * ② collectSegMedia 的 note 含糊写"画面必须从它开始 / 必须在它结束"，
     * 用户只设尾帧时模型就以为这张图也是首帧。改：按实际角色区分措辞。 */
    {
        const ds = {
            prompts: ["x"], ref_assets: POOL,
            segments: [{ frame_img: { end: "assets/a.png" }, refs: [] }],
        };
        const r = await collectSegMedia(null, ds, 0);
        ok(/尾帧锚点/.test(r.note), "只有尾帧图 → note 说「尾帧锚点」", r.note);
        ok(/末帧硬钉/.test(r.note), "note 含「末帧硬钉」", r.note);
        ok(/不是[^\n]*首帧/.test(r.note), "note 标明「不是首帧」—— 避免 LLM 误读", r.note);
        ok(!/0\.00s 硬钉/.test(r.note), "note 不含「0.00s 硬钉」（只有尾帧不应说从它开始）", r.note);
    }
    {
        const ds = {
            prompts: ["x"], ref_assets: POOL,
            segments: [{ frame_img: { first: "assets/a.png" }, refs: [] }],
        };
        const r = await collectSegMedia(null, ds, 0);
        ok(/首帧锚点/.test(r.note), "只有首帧图 → note 说「首帧锚点」", r.note);
        ok(/0\.00s 硬钉/.test(r.note), "note 含「0.00s 硬钉」", r.note);
        ok(/不是[^\n]*尾帧/.test(r.note), "note 标明「不是尾帧」", r.note);
    }
    {
        const ds = {
            prompts: ["x"], ref_assets: POOL,
            segments: [{ frame_img: { first: "assets/a.png", end: "assets/a.png" }, refs: [] }],
        };
        const r = await collectSegMedia(null, ds, 0);
        ok(/首尾帧锚点/.test(r.note), "首帧=尾帧=同一张 → note 说「首尾帧锚点」", r.note);
    }

    console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
    process.exit(bad ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
