/* 外部 agent 的 JSON 导入：宽容解析 + 与文本通道同构 + 绝不静默写空。
 *
 * 这条通道的意义：agent 的产出是**文件**，而文本通道是给**粘贴**用的（从聊天界面
 * 复制会被 markdown 吃掉换行，那是"整篇塌成一段"的头号来源）。但两条通道必须
 * 给出**同一个结构**——写回只有一条路（applyMasterPromptParsed），所以这里钉的是：
 *
 *  · 段级字段仍然只有三个：提示词 / 时长 / 独立镜头；
 *  · 中文镜像字段（prompt_zh 等）一律不取，取了就是把中文混进英文正文；
 *  · 素材引用不在 JSON 里，仍由正文 @素材名 派生（所以 JSON 里没有 refs 字段）；
 *  · 一份正文都读不到时**报错**，绝不写 N 个空段把现有链静默清空。
 *
 * jsdom 真跑：从 h3_director.js 抽出 MP_* / MPJ_* 常量与整族解析函数，不依赖 DOM。
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
    /* 文本通道那一族（JSON 的整篇文本兜底要用到 parseMasterPrompt） */
    extractConst("MP_HEAD_RE"), extractConst("MP_END_RE"),
    extractConst("MP_FIELD_RE"), extractConst("MP_FIELDS"),
    extractConst("MP_YES"), extractConst("MP_NO"), extractConst("MP_HEAD_SUB_RE"),
    extractConst("MP_BODY_KEYS"),
    extractFn("mpReflow"), extractFn("newMasterSeg"), extractFn("parseMasterPrompt"),
    /* JSON 通道 */
    extractConst("MPJ_LIST_KEYS"), extractConst("MPJ_TEXT_KEYS"),
    extractConst("MPJ_PROMPT_KEYS"), extractConst("MPJ_DUR_KEYS"),
    extractConst("MPJ_SOLO_KEYS"), extractConst("MPJ_IDX_KEYS"),
    extractConst("MPJ_FIELD_ORDER"),
    extractFn("mpjKeys"), extractFn("mpjPick"), extractFn("mpjSeconds"),
    extractFn("mpjStandalone"), extractFn("mpjPromptOf"),
    extractFn("parseMasterPromptJson"),
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
const P = (json) => run("parseMasterPromptJson(" + JSON.stringify(json) + ")");

/* ---------- 1. 标准形态：segments + duration / standalone / prompt ---------- */
{
    const json = JSON.stringify({
        format: "sequenceforge.master-prompt",
        version: 1,
        title: "雨夜市场",
        segments: [
            { duration: 10, standalone: false,
                prompt: "integrated_multimodal_description: [Shot 1] Live-action, a rainy alley.\n"
                    + "\noverall_soundscape: Steady rain.\n\nnon_diegetic_music: N/A" },
            { duration: 8, standalone: true, prompt: "integrated_multimodal_description: [Shot 1] She turns." },
        ],
    });
    const p = P(json);
    check("标准形态解析成功", p.ok === true, JSON.stringify(p.error));
    check("段数 2", p.segs.length === 2, "实际 " + p.segs.length);
    check("标题带出来（只用于预览显示）", p.title === "雨夜市场", p.title);
    check("时长 10 / 8", Number(p.segs[0].seconds) === 10 && Number(p.segs[1].seconds) === 8,
        JSON.stringify([p.segs[0].seconds, p.segs[1].seconds]));
    check("standalone false / true", p.segs[0].unlink === false && p.segs[1].unlink === true,
        JSON.stringify([p.segs[0].unlink, p.segs[1].unlink]));
    check("正文含官方三字段且空行保留",
        p.segs[0].main.indexOf("integrated_multimodal_description") === 0
        && p.segs[0].main.indexOf("\n\noverall_soundscape") > 0, JSON.stringify(p.segs[0].main));
    check("无告警", (p.notes || []).length === 0, JSON.stringify(p.notes));
}

/* ---------- 2. 中文镜像字段一律不取（取了 = 中文混进英文正文） ---------- */
{
    const p = P(JSON.stringify({
        segments: [{
            prompt: "integrated_multimodal_description: [Shot 1] Live-action.",
            prompt_zh: "实拍，电影感，[镜头 1] 画面描述。",
            zh: "另一份中文",
            duration: 9,
        }],
    }));
    check("prompt_zh 不进正文", p.ok && String(p.segs[0].main).indexOf("实拍") < 0,
        JSON.stringify(p.segs[0] && p.segs[0].main));
    check("英文正文取到了", String(p.segs[0].main).indexOf("Live-action") > 0);
}

/* ---------- 3. 顶层数组 / 段数组换名 / 段里是字符串 ---------- */
{
    const a = P(JSON.stringify(["integrated_multimodal_description: [Shot 1] A.",
        "integrated_multimodal_description: [Shot 1] B."]));
    check("顶层数组 → 2 段", a.ok && a.segs.length === 2, JSON.stringify(a.error));
    check("纯字符串段 = 正文", a.segs[1].main.indexOf("B.") > 0, JSON.stringify(a.segs[1].main));

    const b = P(JSON.stringify({ segs: [{ main: "甲" }], shots: [{ main: "乙" }] }));
    check("段数组别名 segs 优先", b.ok && b.segs.length === 1 && b.segs[0].main === "甲",
        JSON.stringify(b.segs));
    check("正文别名 main 也认", b.segs[0].main === "甲");
}

/* ---------- 4. 单段对象（没有数组、也没有整篇文本） ---------- */
{
    const p = P(JSON.stringify({ prompt: "integrated_multimodal_description: [Shot 1] C.", duration: 6 }));
    check("单段对象 → 1 段", p.ok && p.segs.length === 1, JSON.stringify(p.error));
    check("单段对象的时长**没被吃掉**（顶层 prompt 不能当整篇文本）",
        Number(p.segs[0].seconds) === 6, String(p.segs[0] && p.segs[0].seconds));
}

/* ---------- 5. 整篇文本包在 JSON 里 → 交文本解析器（同一条链路） ---------- */
{
    const text = "[Segment 1]\nDuration: 9\nStandalone: yes\n\nPrompt:\n甲\n\n"
        + "[Segment 2]\nDuration: 8\n\nPrompt:\n乙\n\n[END]";
    const p = P(JSON.stringify({ master_prompt: text }));
    check("文本字段 → 2 段", p.ok && p.segs.length === 2, JSON.stringify(p.error));
    check("文本字段的 Duration / Standalone 照旧解析",
        Number(p.segs[0].seconds) === 9 && p.segs[0].unlink === true && p.segs[0].main === "甲",
        JSON.stringify(p.segs[0]));
}

/* ---------- 6. 官方字段拆着给 → 按官方顺序拼回正文 ---------- */
{
    const p = P(JSON.stringify({
        segments: [{
            overall_soundscape: "Rain.",
            integrated_multimodal_description: "[Shot 1] Live-action.",
            non_diegetic_music: "N/A",
            duration: 7,
        }],
    }));
    const main = String(p.segs[0].main);
    check("官方字段拼回正文（三段齐全）", p.ok && main.indexOf("integrated_multimodal_description") === 0
        && main.indexOf("overall_soundscape") > 0 && main.indexOf("non_diegetic_music") > 0,
        JSON.stringify(main));
    check("拼装顺序是官方顺序（画面 → 环境音 → 配乐）",
        main.indexOf("overall_soundscape") < main.indexOf("non_diegetic_music")
        && main.indexOf("integrated_multimodal_description") < main.indexOf("overall_soundscape"),
        JSON.stringify(main));
    check("字段间是空行（与文本通道同形）", main.indexOf("\n\n") > 0, JSON.stringify(main));
}

/* ---------- 7. 时长 / 独立镜头的各种写法 ---------- */
{
    const p = P(JSON.stringify({
        segments: [
            { prompt: "A", duration: "10s" },
            { prompt: "B", seconds: "8 秒" },
            { prompt: "C", duration: 6.5 },
            { prompt: "D", duration: "abc" },
            { prompt: "E", standalone: "yes" },
            { prompt: "F", standalone: "否" },
            { prompt: "G", standalone: 1 },
            { prompt: "H", standalone: "maybe" },
        ],
    }));
    check("\"10s\" → 10", Number(p.segs[0].seconds) === 10, String(p.segs[0].seconds));
    check("\"8 秒\" → 8", Number(p.segs[1].seconds) === 8, String(p.segs[1].seconds));
    check("小数 6.5", Number(p.segs[2].seconds) === 6.5, String(p.segs[2].seconds));
    check("无效时长 → undefined（该段时长不动）", p.segs[3].seconds === undefined,
        String(p.segs[3].seconds));
    check("无效时长要点名", p.notes.some((n) => n.indexOf("时长") >= 0 && n.indexOf("4") >= 0),
        JSON.stringify(p.notes));
    check("standalone: yes → true", p.segs[4].unlink === true, String(p.segs[4].unlink));
    check("standalone: 否 → false", p.segs[5].unlink === false, String(p.segs[5].unlink));
    check("standalone: 1 → true", p.segs[6].unlink === true, String(p.segs[6].unlink));
    check("认不出的 standalone → undefined 且点名",
        p.segs[7].unlink === undefined && p.notes.some((n) => n.indexOf("standalone") >= 0),
        JSON.stringify([p.segs[7].unlink, p.notes]));
}

/* ---------- 8. 段序号乱序 → 按 index 重排并提示 ---------- */
{
    const p = P(JSON.stringify({
        segments: [
            { index: 2, prompt: "B", duration: 8 },
            { index: 1, prompt: "A", duration: 10 },
        ],
    }));
    check("按 index 重排", p.ok && p.segs[0].main === "A" && p.segs[1].main === "B",
        JSON.stringify(p.segs.map((s) => s.main)));
    check("重排要提示", p.notes.some((n) => n.indexOf("重排") >= 0), JSON.stringify(p.notes));
    const q = P(JSON.stringify({ segments: [{ index: 1, prompt: "A" }, { index: 2, prompt: "B" }] }));
    check("顺序本来就是对的 → 不吭声", (q.notes || []).length === 0, JSON.stringify(q.notes));
}

/* ---------- 9. 坏输入一律报错，绝不静默写空段 ---------- */
{
    const bads = [
        ["不是 JSON", "{not json"],
        ["空对象", "{}"],
        ["有数组但每段都没正文", '{"segments":[{"duration":10},{"duration":8}]}'],
        ["顶层是字符串", '"just a string"'],
        ["顶层是数字", "42"],
    ];
    for (const [name, raw] of bads) {
        const p = P(raw);
        check(`${name} → 报错（不写入）`, p.ok === false && !!p.error, JSON.stringify(p));
    }
    const p = P('{"segments":[{"duration":10},{"duration":8}]}');
    check("报错时 segs 不参与写入（ok=false 拦在预览层）", p.ok === false);
}

/* ---------- 10. 部分段缺正文：留空但要点名，且**不**整份失败 ---------- */
{
    const p = P(JSON.stringify({
        segments: [{ prompt: "A", duration: 5 }, { duration: 6 }, { prompt: "C", duration: 7 }],
    }));
    check("有一份正文就整体成立", p.ok === true, JSON.stringify(p.error));
    check("缺正文的段留空串", p.segs[1].main === "", JSON.stringify(p.segs[1].main));
    check("缺正文要点名第几段", p.notes.some((n) => n.indexOf("第 2 段") >= 0),
        JSON.stringify(p.notes));
}

/* ---------- 11. BOM / 大小写不敏感 ---------- */
{
    const withBom = "﻿" + JSON.stringify({ segments: [{ prompt: "A", duration: 5 }] });
    check("带 BOM 的 JSON 照样解析（Windows 写出来的常见）", P(withBom).ok === true,
        JSON.stringify(P(withBom).error));
    const p = P(JSON.stringify({ Segments: [{ Duration: 9, Standalone: "YES", Prompt: "A" }] }));
    check("键名 / 值大小写不敏感", p.ok && Number(p.segs[0].seconds) === 9
        && p.segs[0].unlink === true, JSON.stringify(p.segs[0]));
}

/* ---------- 12. 素材引用不在 JSON 里（仍由正文 @素材名 派生） ---------- */
{
    const p = P(JSON.stringify({
        segments: [{ prompt: "integrated_multimodal_description: [Shot 1] @女主.png stands there.",
            refs: ["女主.png"], duration: 9 }],
    }));
    check("正文里的 @素材名 原样保留", String(p.segs[0].main).indexOf("@女主.png") > 0,
        JSON.stringify(p.segs[0].main));
    check("JSON 里的 refs 字段不被解析成字段（正文是唯一真相）",
        p.segs[0].refs === undefined, JSON.stringify(p.segs[0].refs));
}

console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
process.exit(bad ? 1 : 0);
