/* 总提示词三框（① 意图 / ② 剧本 / ③ 结果）合成与回环。
 *
 * 背景：工作台从一个大文本框改成三个独立框，每框只装自己那一层，
 * 靠段头【段N】同序对应。提交前必须能合成一份**与旧版完全一致**的分段文本
 * （parseMasterPrompt 认、导出/粘贴口径不变），否则外贴的文本就废了。
 *
 * jsdom 真跑：从 h3_director.js 抽出 MP_* 与 mpReflow / parseMasterPrompt /
 * mpSplitBox / mpComposeBoxes，不依赖 DOM。
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

function extractConst(name) {
    const re = new RegExp("^\\s*const " + name + " = [^;]*;", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到常量 " + name);
    return m[0];
}

function extractFn(name) {
    const re = new RegExp("^\\s*function " + name + "\\(", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到函数 " + name);
    const i = src.indexOf("{", m.index);
    let depth = 0;
    for (let j = i; j < src.length; j++) {
        if (src[j] === "{") depth++;
        else if (src[j] === "}") { depth--; if (!depth) return src.slice(m.index, j + 1); }
    }
    throw new Error("大括号不配平: " + name);
}

const parts = [
    extractConst("MP_HEAD_RE"), extractConst("MP_END_RE"),
    extractConst("MP_FIELD_RE"), extractConst("MP_FIELDS"),
    extractConst("MP_YES"), extractConst("MP_NO"), extractConst("MP_HEAD_SUB_RE"),
    extractConst("MP_BODY_KEYS"),
    extractFn("mpReflow"), extractFn("newMasterSeg"),
    extractFn("parseMasterPrompt"), extractFn("mpRenderState"),
    extractFn("mpSplitBox"), extractFn("mpComposeBoxes"),
].join("\n");

const dom = new JSDOM("<!doctype html><html><body></body></html>", { runScripts: "outside-only" });
dom.window.eval(parts);

const run = (code) => dom.window.eval(code);
let bad = 0;
const check = (name, cond, extra) => {
    if (cond) { console.log("  ok   " + name); return; }
    bad++;
    console.log("  FAIL " + name);
    if (extra !== undefined) console.log("       " + extra);
};

/* ---------- 1. 单段三框：不带段头也能合成 ---------- */
{
    const I = "雨夜霓虹市场，女孩回头笑说跟上我";
    const S = "镜头一（0–3 秒）：她停下回头\n镜头二（3–9 秒）：她喊了一声";
    const M = "integrated_multimodal_description: [Shot 1] 实拍、电影感……\n\n"
        + "overall_soundscape: 雨声持续。\n\nnon_diegetic_music: N/A";
    const c = run("mpComposeBoxes(" + [I, S, M].map((x) => JSON.stringify(x)).join(",")
        + ",[9])");
    check("单段合成段数=1", c.count === 1, JSON.stringify(c.count));
    check("无段头不告警", c.notes.length === 0, JSON.stringify(c.notes));
    const p = run("parseMasterPrompt(" + JSON.stringify(c.text) + ")");
    check("回环段数=1", p.segs.length === 1, String(p.segs.length));
    const s = p.segs[0] || {};
    check("单段 意图 还原", s.intent === I, JSON.stringify(s.intent));
    check("单段 剧本 还原", s.script === S, JSON.stringify(s.script));
    check("单段 提示词 还原（含内部空行）", s.main === M, JSON.stringify(s.main));
    check("单段 时长 还原", Number(s.seconds) === 9, String(s.seconds));
}

/* ---------- 2. 多段：三框按【段N】同序对应 ---------- */
{
    const I = "【段1】\n雨夜市场，她喊跟上我\n\n【段2】\n天台看烟花";
    const S = "【段1】\n镜头一：她回头\n\n【段2】\n镜头一：跑到天台";
    const M = "【段1】\nintegrated_multimodal_description: [Shot 1] 甲\n\n"
        + "overall_soundscape: 雨声。\n\n【段2】\nintegrated_multimodal_description: [Shot 1] 乙";
    const c = run("mpComposeBoxes(" + [I, S, M].map((x) => JSON.stringify(x)).join(",")
        + ",[9,12])");
    check("多段合成段数=2", c.count === 2, String(c.count));
    const p = run("parseMasterPrompt(" + JSON.stringify(c.text) + ")");
    check("多段回环段数=2", p.segs.length === 2, String(p.segs.length));
    const a = p.segs[0] || {}, b = p.segs[1] || {};
    check("段1 意图", a.intent === "雨夜市场，她喊跟上我", JSON.stringify(a.intent));
    check("段2 意图", b.intent === "天台看烟花", JSON.stringify(b.intent));
    check("段1 时长=9", Number(a.seconds) === 9, String(a.seconds));
    check("段2 时长=12", Number(b.seconds) === 12, String(b.seconds));
    check("段2 提示词含官方字段头",
        String(b.main || "").indexOf("integrated_multimodal_description") === 0,
        JSON.stringify(b.main));
}

/* ---------- 3. 某框段数少：只写它有的那些段，不误清其它段 ---------- */
{
    const I = "【段1】\n只有第一段写了意图";
    const S = "【段1】\n剧本甲\n\n【段2】\n剧本乙\n\n【段3】\n剧本丙";
    const M = "【段1】\n提示甲\n\n【段2】\n提示乙\n\n【段3】\n提示丙";
    const c = run("mpComposeBoxes(" + [I, S, M].map((x) => JSON.stringify(x)).join(",")
        + ",[null,null,null])");
    check("段数取三框最大=3", c.count === 3, String(c.count));
    const p = run("parseMasterPrompt(" + JSON.stringify(c.text) + ")");
    check("段2 意图 未写 → undefined（分配时不覆盖）",
        p.segs[1] && p.segs[1].intent === undefined, JSON.stringify(p.segs[1] && p.segs[1].intent));
    check("段3 剧本 还原", p.segs[2] && p.segs[2].script === "剧本丙",
        JSON.stringify(p.segs[2] && p.segs[2].script));
    check("时长未给 → 不写时长行", c.text.indexOf("时长：") < 0, c.text);
}

/* ---------- 4. 有段头却缺段头的一框：归入第 1 段并告警 ---------- */
{
    const I = "整篇一句意图，没写段头";
    const S = "【段1】\n甲\n\n【段2】\n乙";
    const M = "【段1】\n甲提示\n\n【段2】\n乙提示";
    const c = run("mpComposeBoxes(" + [I, S, M].map((x) => JSON.stringify(x)).join(",")
        + ",[null,null])");
    check("缺段头的框进 notes", c.notes.some((n) => n.indexOf("① 意图") >= 0), JSON.stringify(c.notes));
    const p = run("parseMasterPrompt(" + JSON.stringify(c.text) + ")");
    check("缺段头内容落在段1", p.segs[0] && p.segs[0].intent === I,
        JSON.stringify(p.segs[0] && p.segs[0].intent));
}

/* ---------- 5. 整框为空 = 不动该字段（不误清） ---------- */
{
    const S = "【段1】\n剧本甲";
    const M = "【段1】\n提示甲";
    const c = run("mpComposeBoxes(" + ["", S, M].map((x) => JSON.stringify(x)).join(",")
        + ",[null],[]" + ")");
    const p = run("parseMasterPrompt(" + JSON.stringify(c.text) + ")");
    check("意图框全空 → intent 为 undefined",
        p.segs[0] && p.segs[0].intent === undefined, JSON.stringify(p.segs[0] && p.segs[0].intent));
}

/* ---------- 6. 段头写了但正文空 = 显式清空 ---------- */
{
    const M = "【段1】\n";
    const c = run("mpComposeBoxes(" + ["", "", M].map((x) => JSON.stringify(x)).join(",")
        + ",[null],[]" + ")");
    const p = run("parseMasterPrompt(" + JSON.stringify(c.text) + ")");
    check("写了段头空正文 → main 为空串（清空）",
        p.segs[0] && p.segs[0].main === "", JSON.stringify(p.segs[0] && p.segs[0].main));
}

/* ---------- 7. 合成结果与旧版导出格式同构（三标签齐全） ---------- */
{
    const state = [
        { intent: "雨夜市场", script: "镜头一：她回头", main: "integrated_multimodal_description: [Shot 1] …", seconds: 9, unlink: false, refs: [] },
    ];
    const legacy = run("mpRenderState(" + JSON.stringify(state) + ")");
    const c = run("mpComposeBoxes(" + [state[0].intent, state[0].script, state[0].main]
        .map((x) => JSON.stringify(x)).join(",") + ",[9])");
    const strip = (t) => t.split("\n").map((l) => l.trim()).filter(Boolean).join("\n");
    check("三框合成 ≈ 旧版导出（去掉空行后逐行一致）",
        strip(c.text).replace("【完】", "") === strip(legacy).replace("【完】", ""),
        JSON.stringify({ now: strip(c.text), legacy: strip(legacy) }));
}

/* ---------- 8. 连续正文行不许被拼成一行（pushBody 换行口径回归） ---------- */
{
    const text = [
        "【段1】",
        "剧本：",
        "镜头一（0–3 秒）：她停下回头",
        "镜头二（3–9 秒）：她喊了一声",
        "",
        "提示词：",
        "integrated_multimodal_description: [Shot 1] 实拍、电影感，中景。",
        "[Shot 2] At 00:03.200，镜头小幅慢速推近。",
        "",
        "overall_soundscape: 雨声持续。",
    ].join("\n");
    const p = run("parseMasterPrompt(" + JSON.stringify(text) + ")");
    const s = p.segs[0] || {};
    check("连续剧本行保留换行",
        s.script === "镜头一（0–3 秒）：她停下回头\n镜头二（3–9 秒）：她喊了一声",
        JSON.stringify(s.script));
    check("官方字段内连续行保留换行",
        String(s.main || "").indexOf("中景。\n[Shot 2] At 00:03.200") > 0,
        JSON.stringify(s.main));
    check("字段之间的空行仍保留", String(s.main || "").indexOf("\n\noverall_soundscape") > 0,
        JSON.stringify(s.main));
}

/* ---------- 9. 三框合成 -> 解析：多行正文不糊（端到端） ---------- */
{
    const M = "integrated_multimodal_description: [Shot 1] 实拍、电影感，中景。\n"
        + "[Shot 2] At 00:03.200，镜头小幅慢速推近。\n\n"
        + "overall_soundscape: 雨声持续。\n\nnon_diegetic_music: N/A";
    const c = run("mpComposeBoxes(" + ["意图甲", "镜头一\n镜头二", M]
        .map((x) => JSON.stringify(x)).join(",") + ",[null])");
    const p = run("parseMasterPrompt(" + JSON.stringify(c.text) + ")");
    const s = p.segs[0] || {};
    check("端到端：意图多行安全", s.intent === "意图甲", JSON.stringify(s.intent));
    check("端到端：剧本多行不拼接", s.script === "镜头一\n镜头二", JSON.stringify(s.script));
    check("端到端：官方三字段原样往返", s.main === M, JSON.stringify(s.main));
}

console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
process.exit(bad ? 1 : 0);
