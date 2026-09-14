/* 总提示词分段格式：意图 / 剧本 / 提示词 三标签 + 旧八标签兼容 + 导出回环。
 *
 * 背景：总提示词框改成多段工作台后，每段要带三块内容——
 *   ① 中文意图（AI 扩写的输入）→ ② 剧本（扩写产物，中文自由格式）
 *   → ③ 结果（提示词优化产物，H3 官方格式，进模型）。
 * 文本交换格式相应从「四标签」扩成「意图 / 剧本 / 提示词」，
 * 旧文本（场景/角色/环境音/配乐）必须还能粘回来。
 *
 * jsdom 真跑：从 h3_director.js 抽出 MP_* 常量与 mpReflow / newMasterSeg /
 * parseMasterPrompt / mpRenderState，不依赖 DOM。
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

/* 顶层 `const NAME = ...;`（到行内第一个分号为止，允许后面跟行尾注释） */
function extractConst(name) {
    const re = new RegExp("^\\s*const " + name + " = [^;]*;", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到常量 " + name);
    return m[0];
}

/* 顶层 `function NAME(...) { ... }`（按大括号配平） */
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

/* ---------- 1. 三标签：意图 / 剧本 / 提示词 ---------- */
{
    const text = [
        "【段1】",
        "时长：9",
        "独立镜头：否",
        "参考：角色1，图片2",
        "意图：雨夜霓虹市场，女孩回头笑说跟上我",
        "剧本：",
        "时长：9 秒",
        "",
        "镜头一（0–3 秒）：雨幕里霓虹糊成色块，她忽然停下回头",
        "镜头二（3–9 秒）：她笑了一下，喊了一声",
        "",
        "提示词：",
        "integrated_multimodal_description: [Shot 1] 实拍、电影感……",
        "",
        "overall_soundscape: 雨声持续。",
        "",
        "non_diegetic_music: N/A",
        "【完】",
    ].join("\n");
    const p = run("parseMasterPrompt(" + JSON.stringify(text) + ")");
    check("识别 1 段", p.segs.length === 1, "实际 " + p.segs.length);
    const s = p.segs[0];
    check("意图 → intent", s.intent === "雨夜霓虹市场，女孩回头笑说跟上我", JSON.stringify(s.intent));
    check("剧本 → script（多行原样）",
        s.script && s.script.indexOf("时长：9 秒") === 0 && s.script.indexOf("镜头二") > 0,
        JSON.stringify(s.script));
    check("剧本内的空行保留（官方三字段靠空行分隔）",
        s.script && s.script.indexOf("\n\n") > 0);
    check("提示词 → main（含空行）",
        s.main && s.main.indexOf("integrated_multimodal_description") === 0
        && s.main.indexOf("overall_soundscape") > 0 && s.main.indexOf("\n\n") > 0,
        JSON.stringify(s.main));
    check("时长：9", Number(s.seconds) === 9, String(s.seconds));
    check("参考：切成两个标签",
        JSON.stringify(s.refs) === JSON.stringify(["角色1", "图片2"]), JSON.stringify(s.refs));
    check("独立镜头：否 → false", s.unlink === false, String(s.unlink));
    check("【完】之后的解说不参与解析", p.notes.length === 0, JSON.stringify(p.notes));
}

/* ---------- 2. 旧八标签仍认（旧项目文本可粘回） ---------- */
{
    const text = [
        "【段1】",
        "场景：黄昏教室",
        "角色：短发少女",
        "环境音：翻书声",
        "配乐：钢琴独奏",
        "提示词：她抬起头。",
    ].join("\n");
    const p = run("parseMasterPrompt(" + JSON.stringify(text) + ")");
    const s = p.segs[0];
    check("旧标签 场景/角色/环境音/配乐 仍解析",
        s.scene === "黄昏教室" && s.character === "短发少女"
        && s.soundscape === "翻书声" && s.music === "钢琴独奏",
        JSON.stringify(s));
    check("旧文本没写意图/剧本 → undefined（不覆盖）",
        s.intent === undefined && s.script === undefined);
}

/* ---------- 3. 软换行丢失容错（从 markdown 界面复制） ---------- */
{
    const text = "【段1】 时长：9 意图：雨夜市场 剧本：镜头一…… 【段2】 时长：8 提示词：[Shot 1] ……";
    const p = run("parseMasterPrompt(" + JSON.stringify(text) + ")");
    check("挤成一行的文本 → 自动重分行识别出 2 段", p.segs.length === 2, "实际 " + p.segs.length);
    check("重分行后意图仍解析得到",
        p.segs[0] && p.segs[0].intent === "雨夜市场", JSON.stringify(p.segs[0]));
    check("重分行会给出提示 notes", p.notes.length > 0);
}

/* ---------- 4. 无段头 = 单段 ---------- */
{
    const p = run("parseMasterPrompt(" + JSON.stringify("就一句话，没有段头") + ")");
    check("无段头 → 单段主体", p.segs.length === 1 && p.segs[0].main === "就一句话，没有段头");
}

/* ---------- 5. 导出回环：mpRenderState -> parseMasterPrompt ---------- */
{
    const state = [
        { intent: "雨夜市场，她喊跟上我", script: "镜头一：她回头\n\n镜头二：她跑", main: "integrated_multimodal_description: [Shot 1] …", seconds: 9, unlink: false, refs: ["角色1"] },
        { intent: "天台看烟花", script: "镜头一：跑到天台", main: "", seconds: 12, unlink: true, refs: [] },
    ];
    const text = run("mpRenderState(" + JSON.stringify(state) + ")");
    const p = run("parseMasterPrompt(" + JSON.stringify(text) + ")");
    check("回环段数一致", p.segs.length === 2, "实际 " + p.segs.length);
    const a = p.segs[0] || {};
    check("回环 意图 一致", a.intent === state[0].intent, JSON.stringify(a.intent));
    check("回环 剧本 一致（含空行）", a.script === state[0].script, JSON.stringify(a.script));
    check("回环 提示词 一致", a.main === state[0].main, JSON.stringify(a.main));
    check("回环 时长 一致", Number(a.seconds) === 9, String(a.seconds));
    check("回环 参考 一致", JSON.stringify(a.refs) === JSON.stringify(["角色1"]), JSON.stringify(a.refs));
    const b = p.segs[1] || {};
    check("回环 独立镜头 是 → true", b.unlink === true, String(b.unlink));
    check("回环 空提示词 → 空串不是 undefined", b.main === "", JSON.stringify(b.main));
    check("回环无告警", p.notes.length === 0, JSON.stringify(p.notes));
}

/* ---------- 6. 段头序号与出现顺序不一致只提示不报错 ---------- */
{
    const text = "【段3】\n提示词：甲\n\n【段2】\n提示词：乙";
    const p = run("parseMasterPrompt(" + JSON.stringify(text) + ")");
    check("序号乱序仍按出现顺序排", p.segs.length === 2 && p.segs[0].main === "甲");
    check("序号不一致进 notes", p.notes.some((n) => n.indexOf("不一致") >= 0), JSON.stringify(p.notes));
}

console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
process.exit(bad ? 1 : 0);
