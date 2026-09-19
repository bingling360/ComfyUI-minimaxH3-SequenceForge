/* 总提示词分段格式：提示词单主体 + 两个段级标签 + 旧标签只读丢弃 + 导出回环。
 *
 * 背景：工作台退化成**纯分段流水线**——只给每段三样东西：提示词 / 时长 / 独立镜头。
 * 文本交换格式相应收敛为：
 *   【段N】 + 段级标签（时长 / 独立镜头）+ 提示词正文（可带 `提示词：` 标签，
 *   不带也行，段头后的裸正文一律归提示词）。
 *
 * 上一代的分层全部**只读丢弃**（认出来 → 整块丢掉 → 进 notes 点名）：
 *   参考 / 场景 / 角色 / 环境音 / 配乐 / 意图 / 剧本
 * 为什么不是"看不懂就当正文"：这些块的内容**不该进模型**，悄悄并进提示词等于
 * 把"不进模型"的内容送进模型；也不能静默丢弃，用户会以为内容还在。
 * 其中「参考」尤其要点名：它曾是资产引用的第二条通道，现在只有正文里的
 * `@素材名` 算数（与段卡同一套），不提示的话用户会以为自己挂上了素材。
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

/* ---------- 1. 段级标签只有 时长 / 独立镜头；旧标签只读丢弃 ---------- */
{
    const text = [
        "【段1】",
        "时长：9",
        "独立镜头：否",
        "参考：角色1，图片2",
        "场景：黄昏教室",
        "角色：短发少女",
        "环境音：翻书声",
        "配乐：钢琴独奏",
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
    check("时长：9", Number(s.seconds) === 9, String(s.seconds));
    check("独立镜头：否 → false", s.unlink === false, String(s.unlink));
    /* 上一代的七个标签全部不再产出字段 */
    for (const k of ["refs", "scene", "character", "soundscape", "music", "intent", "script"]) {
        check(`「${k}」不再解析成字段`, s[k] === undefined, JSON.stringify(s[k]));
    }
    check("剧本/场景正文**没被当提示词吞掉**",
        String(s.main || "").indexOf("镜头一") < 0
        && String(s.main || "").indexOf("时长：9 秒") < 0
        && String(s.main || "").indexOf("黄昏教室") < 0
        && String(s.main || "").indexOf("参考：角色1") < 0,
        JSON.stringify(s.main));
    check("提示词 → main（含空行）",
        s.main && s.main.indexOf("integrated_multimodal_description") === 0
        && s.main.indexOf("overall_soundscape") > 0 && s.main.indexOf("\n\n") > 0,
        JSON.stringify(s.main));
    check("丢弃要说出来（进 notes）",
        p.notes.some((n) => n.indexOf("已忽略") >= 0), JSON.stringify(p.notes));
    check("「参考」单独点名（否则用户以为挂上素材了）",
        p.notes.some((n) => n.indexOf("参考") >= 0), JSON.stringify(p.notes));
}

/* ---------- 2. 资产引用只认正文里的 @素材名（不再有「参考：」通道） ---------- */
{
    const text = "【段1】\n提示词：integrated_multimodal_description: [Shot 1] "
        + "<Picture 1> 的雨夜市场，@女主.png 站在巷口。";
    const p = run("parseMasterPrompt(" + JSON.stringify(text) + ")");
    check("引用原样留在正文里（不被任何标签吃掉）",
        String(p.segs[0].main).indexOf("@女主.png") > 0, JSON.stringify(p.segs[0].main));
    check("没有「参考：」时不再产生任何 note", p.notes.length === 0, JSON.stringify(p.notes));
}

/* ---------- 3. 软换行丢失容错（从 markdown 界面复制） ---------- */
{
    const text = "【段1】 时长：9 意图：雨夜市场 剧本：镜头一…… 【段2】 时长：8 提示词：[Shot 1] ……";
    const p = run("parseMasterPrompt(" + JSON.stringify(text) + ")");
    check("挤成一行的文本 → 自动重分行识别出 2 段", p.segs.length === 2, "实际 " + p.segs.length);
    check("重分行后意图仍被忽略（不再是字段）",
        p.segs[0] && p.segs[0].intent === undefined, JSON.stringify(p.segs[0]));
    check("重分行会给出提示 notes", p.notes.length > 0);
}

/* ---------- 4. 无段头 = 单段（裸正文直接是提示词） ---------- */
{
    const p = run("parseMasterPrompt(" + JSON.stringify("就一句话，没有段头") + ")");
    check("无段头 → 单段主体", p.segs.length === 1 && p.segs[0].main === "就一句话，没有段头");
}

/* ---------- 5. 导出回环：mpRenderState -> parseMasterPrompt ---------- */
{
    const state = [
        /* 故意带上上一代的字段：导出必须**一个都不写**，否则下次贴回就触发已忽略 */
        { intent: "雨夜市场，她喊跟上我", script: "镜头一：她回头\n\n镜头二：她跑",
            main: "integrated_multimodal_description: [Shot 1] …", seconds: 9,
            unlink: false, refs: ["角色1"], scene: "雨夜", character: "她",
            soundscape: "雨声", music: "N/A" },
        { intent: "天台看烟花", script: "镜头一：跑到天台", main: "", seconds: 12, unlink: true, refs: [] },
    ];
    const text = run("mpRenderState(" + JSON.stringify(state) + ")");
    for (const dead of ["意图：", "剧本：", "参考：", "场景：", "角色：", "环境音：", "配乐："]) {
        check(`导出不再写「${dead.replace("：", "")}」`, text.indexOf(dead) < 0, JSON.stringify(text));
    }
    const p = run("parseMasterPrompt(" + JSON.stringify(text) + ")");
    check("回环段数一致", p.segs.length === 2, "实际 " + p.segs.length);
    const a = p.segs[0] || {};
    check("回环 提示词 一致", a.main === state[0].main, JSON.stringify(a.main));
    check("回环 时长 一致", Number(a.seconds) === 9, String(a.seconds));
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

/* ---------- 7. 只读块到下一个段头为止（跨段重置） ---------- */
{
    const text = "【段1】\n剧本：甲剧本\n提示词：甲\n\n【段2】\n提示词：乙";
    const p = run("parseMasterPrompt(" + JSON.stringify(text) + ")");
    check("只读块被下一个标签终止", p.segs[0] && p.segs[0].main === "甲",
        JSON.stringify(p.segs[0] && p.segs[0].main));
    check("只读块不跨段残留", p.segs[1] && p.segs[1].main === "乙",
        JSON.stringify(p.segs[1] && p.segs[1].main));
}

console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
process.exit(bad ? 1 : 0);
