/* 段级时长留空时，拿到的是**节点「每段时长」**，不是硬编码的 5 秒。
 *
 * 段级 seconds=null 的语义是"跟随节点默认"，不是"5 秒"。以前 AI 扩写/优化、
 * 对齐指令、编译预览各处都写 `seg.seconds || 5` —— 用户把节点「每段时长」改成
 * 6s/8s 后：界面显示 8s、AI 按 5s 写、对齐句 S.SS 也按 5s 算，三处错位；
 * 而且扩写还给了 4–15 的范围让模型自选，模型于是"自由发挥"写出 12s 的分镜
 * （本段只生成 5s → Shot 时间戳超界 E_SHOT_OVERFLOW）。
 *
 * 统一入口是 segmentSeconds()：段级优先，没填才回落节点控件。
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
    var _dur = 5;                       // 节点「每段时长」控件的值
    function getWidgetValue(node, name) { return name === W_DUR ? _dur : ""; }
`);
dom.window.eval([
    extractConst("W_DUR"),
    extractFn("pyRound"),
    extractFn("snapFrames"),
    extractFn("segmentSeconds"),
    extractFn("segmentFrames"),
    ";window.W_DUR = W_DUR;",
].join("\n"));

const { segmentSeconds, segmentFrames } = dom.window;
const node = {};

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

function withDur(v) { dom.window._dur = v; }

/* ---------- 1) 段级填了 → 永远用段级的，不看节点默认 ---------- */
withDur(6);
ok(segmentSeconds(node, { seconds: 8 }) === 8, "段级 8s 优先（节点默认 6s 不该覆盖）",
    segmentSeconds(node, { seconds: 8 }), 8);
ok(segmentSeconds(node, { seconds: 4.5 }) === 4.5, "段级小数也照用",
    segmentSeconds(node, { seconds: 4.5 }), 4.5);

/* ---------- 2) 段级留空 → 跟随节点默认（**这一条就是本次修的 bug**） ---------- */
withDur(6);
ok(segmentSeconds(node, { seconds: null }) === 6,
    "段级 null → 节点默认 6s（以前一律拿 5）", segmentSeconds(node, { seconds: null }), 6);
ok(segmentSeconds(node, {}) === 6, "段级没有该字段 → 节点默认 6s",
    segmentSeconds(node, {}), 6);
ok(segmentSeconds(node, { seconds: "" }) === 6, "段级空串 → 节点默认 6s",
    segmentSeconds(node, { seconds: "" }), 6);
ok(segmentSeconds(node, { seconds: 0 }) === 6, "段级 0（无效值）→ 节点默认 6s",
    segmentSeconds(node, { seconds: 0 }), 6);

withDur(10);
ok(segmentSeconds(node, { seconds: null }) === 10, "节点默认改成 10s 也要跟着变",
    segmentSeconds(node, { seconds: null }), 10);

/* ---------- 3) 节点默认也没了 → 才回落到 5 ---------- */
withDur(0);
ok(segmentSeconds(node, { seconds: null }) === 5.0, "节点默认无效 → 回落 5s",
    segmentSeconds(node, { seconds: null }), 5.0);
withDur(undefined);
ok(segmentSeconds(node, { seconds: null }) === 5.0, "节点默认缺失 → 回落 5s",
    segmentSeconds(node, { seconds: null }), 5.0);

/* ---------- 4) 帧数显示必须跟着同一个值走 ---------- */
withDur(6);
ok(segmentFrames(node, { seconds: null }) === segmentFrames(node, { seconds: 6 }),
    "段级留空的帧数 == 显式 6s 的帧数",
    segmentFrames(node, { seconds: null }), segmentFrames(node, { seconds: 6 }));
ok(segmentFrames(node, { seconds: null }) === 141, "6s → 141 帧（不是 5s 的 124）",
    segmentFrames(node, { seconds: null }), 141);

console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
process.exit(bad ? 1 : 0);
