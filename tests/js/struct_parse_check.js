/* 结构化「文本 → 结构」反向解析 真跑检查（node，抽 shipped 源码执行）。
 *
 * 跑法：node tests/js/struct_parse_check.js
 *
 * 钉的是这轮补上的三段反向解析（此前**完全没解析**，贴进来的官方文本只有
 * shots / 环境音 / 配乐落到结构里）：
 *  ① subject_definitions → subjects + references
 *     官方案例常一条写到底用「；」分隔，只按换行切会整段变成一个条目 ——
 *     表现就是"参考素材转不过去"（references 恒空，编译出不图）。
 *  ② retention_analysis → retention（marker 须在官方词表内）
 *  ③ 编译期自动补的兜底行（note 固定 `layout and mood kept`）**不得回写**：
 *     回写了再编译就再补一条，每切一次「结构 ⇄ 文本」多一批条目。
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8");

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
    throw new Error("大括号不配平 " + name);
}

function extractConst(name) {
    const re = new RegExp("^const " + name + " = ", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到常量 " + name);
    const end = src.indexOf(";", m.index);
    if (end < 0) throw new Error("常量语句没结束 " + name);
    return src.slice(m.index, end + 1);
}

/* 解析函数依赖后端词表（window.H3Prompts.RETENTION_MARKERS），抽出来跑得先备好 */
global.window = global.window || {};
global.window.H3Prompts = {
    RETENTION_MARKERS: ["fully_preserved", "partially_preserved", "attribute_transfer",
        "weak_reference", "fully_copy", "partially_copy", "reference"],
};

const code = [
    extractConst("_AUTO_RETENTION_NOTE"),
    extractFn("parseSubjectDefs"),
    extractFn("parseRetention"),
].join("\n");
const api = new Function(code + "\nreturn {parseSubjectDefs, parseRetention};")();
const { parseSubjectDefs, parseRetention } = api;

const fails = [];
function ok(cond, msg) { if (!cond) fails.push(msg); }
function eq(a, b, msg) {
    if (JSON.stringify(a) !== JSON.stringify(b)) {
        fails.push(`${msg}\n    实际=${JSON.stringify(a)}\n    期望=${JSON.stringify(b)}`);
    }
}

/* ① 换行分隔（后端 compose_reference 的写法） */
{
    const txt = "<Subject 1>: 撑伞的女孩：二十岁上下\n<Picture 1>: 雨夜市场窄巷的实景参考图";
    const r = parseSubjectDefs(txt);
    eq(r.subs.length, 1, "换行式 subjects 应 1 条");
    eq(r.refs.length, 1, "换行式 references 应 1 条");
    eq(r.subs[0].definition, "撑伞的女孩：二十岁上下", "subject 定义");
    eq(r.refs[0].label, "<Picture 1>", "reference 标签");
    eq(r.refs[0].note, "雨夜市场窄巷的实景参考图", "reference 说明");
}

/* ② 分号分隔（官方案例的一条写到底）—— 只按换行切会全丢 */
{
    const txt = "<Subject 1> 撑伞的女孩：二十岁上下，黑色湿发贴额；<Picture 1> 雨夜市场窄巷的实景参考图";
    const r = parseSubjectDefs(txt);
    eq(r.subs.length, 1, "分号式 subjects 应 1 条（整段不丢）");
    eq(r.refs.length, 1, "分号式 references 应 1 条");
    ok(/撑伞的女孩/.test(r.subs[0].definition), "分号式 subject 正文");
    ok(/雨夜市场/.test(r.refs[0].note), "分号式 reference 正文");
}

/* ③ 全角冒号也要认（中文文本里常见） */
{
    const r = parseSubjectDefs("<Picture 2>：一只黑猫");
    eq(r.refs.length, 1, "全角冒号应能解析");
    eq(r.refs[0].note, "一只黑猫", "全角冒号 note");
}

/* ④ 非标签行（续行、杂文）不产生条目 */
{
    const r = parseSubjectDefs("这是一句说明\n<Picture 1>: 真的参考");
    eq(r.refs.length, 1, "杂文行不该变成条目");
}

/* ⑤ retention：marker + note */
{
    const txt = "<Subject 1>: fully_preserved - 人物身份与服装在全程保持一致\n"
        + "<Picture 1>: fully_copy, 场景光照沿用参考图";
    const r = parseRetention(txt);
    eq(r.length, 2, "retention 应 2 条");
    eq(r[0].marker, "fully_preserved", "retention[0] marker");
    eq(r[1].marker, "fully_copy", "retention[1] marker");
    eq(r[1].note, "场景光照沿用参考图", "retention[1] note");
}

/* ⑥ 编译兜底行不得回写（否则每轮多一批） */
{
    const txt = "<Picture 3>: partially_preserved, layout and mood kept";
    eq(parseRetention(txt).length, 0, "自动兜底行应被跳过");
}

/* ⑦ 非法 marker 丢弃（编译期会被兜底，解析回去会对不上） */
{
    const r = parseRetention("<Picture 1>: totally_made_up_marker, 随便写的");
    eq(r.length, 0, "非法 marker 应丢弃");
}

/* ⑧ 空输入不炸 */
{
    eq(parseSubjectDefs("").refs.length, 0, "空输入 refs 应为空");
    eq(parseRetention("").length, 0, "空输入 retention 应为空");
    eq(parseSubjectDefs(null).subs.length, 0, "null 输入不该炸");
}

if (fails.length) {
    console.error("FAIL:\n - " + fails.join("\n - "));
    process.exit(1);
}
console.log("OK: 结构化反向解析（主体定义 / 保留度 / 分号分隔 / 兜底行）全部通过");
