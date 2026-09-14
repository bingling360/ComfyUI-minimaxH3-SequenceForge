/* 清锚 / 换锚 → 结果框里的官方对齐指令行必须跟着变。
 *
 * 背景：对齐行是编译产物（写进 ds.prompts 的文本）。以前清掉首/尾帧图后，
 * 文本里那句 "Picture 1 aligns with the 0.00-second mark" 还留着，
 * 等于继续告诉模型去参考一张已经不存在的图 —— 而真正注入 latent 的
 * frame_img 早就没了，文本与实跑分家。
 *
 * jsdom 真跑：从 h3_director.js 抽出 segHasFrames / defaultV2Mode / effV2Mode /
 * v2InstrLines / resyncAlignmentLines 与 KF_ALIGN_RE，桩掉 getDs / setPromptText。
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
const promptsSrc = fs.readFileSync(path.join(ROOT, "web", "h3_prompts.js"), "utf8");

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
    const re = new RegExp("^\\s*const " + name + " = [\\s\\S]*?;\\s*$", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到常量 " + name);
    return m[0];
}

const dom = new JSDOM("<!doctype html><html><body></body></html>", { runScripts: "outside-only" });
dom.window.eval(promptsSrc);
dom.window.eval(`
    var _ds = null;
    function getDs() { return _ds; }
    var _writes = [];
    function setPromptText(node, idx, text) {
        _writes.push(text);
        (_ds.prompts = _ds.prompts || [])[idx] = text;
    }
`);

const resync = dom.window.eval([
    "function _getWrites(){ return _writes; }",
    extractConst("V2_MODES"),
    extractConst("KF_ALIGN_RE"),
    extractFn("segHasFrames"),
    extractFn("defaultV2Mode"),
    extractFn("effV2Mode"),
    extractFn("v2InstrLines"),
    extractFn("resyncAlignmentLines"),
    "resyncAlignmentLines",
].join("\n"));

const FL = "How the reference pictures align with the target video — "
    + "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
    + "Picture 2 (from Shot 1) aligns with the 8.00-second mark of the target video.";
const I2 = "For the target video, at 0.00 seconds into the target video, "
    + "<Picture 1> (from [Shot 1]) is fully referenced.";
const BODY = "[Shot 1] A cyclist opens an umbrella.\n\noverall_soundscape: Rain falls.\n\nnon_diegetic_music: N/A";
const HEAD = "integrated_multimodal_description:";

function mkDs(frameImg, first, end, text) {
    return {
        prompts: [text],
        segments: [{ seconds: 8, frame_img: frameImg || null }],
        first_frame: first || "",
        end_frame: end || "",
    };
}

const cases = [
    {
        name: "清除尾帧锚 → FL2VA 双锚句降级为 I2VA 单锚句",
        ds: mkDs({ first: "a.png" }, "a.png", "", `${HEAD} ${FL}\n${BODY}`),
        want: `${HEAD} ${I2}\n${BODY}`,
    },
    {
        name: "清除全部锚 → 摘掉对齐行，正文一字不动",
        ds: mkDs(null, "", "", `${HEAD} ${FL}\n${BODY}`),
        want: `${HEAD} ${BODY}`,
    },
    {
        name: "清锚后再传回首帧 → 自动补出 I2VA 句",
        ds: mkDs({ first: "a.png" }, "a.png", "", `${HEAD} ${BODY}`),
        want: `${HEAD} ${I2}\n${BODY}`,
    },
    {
        name: "原本没有对齐行且当前也无锚 → 不碰手写文本",
        ds: mkDs(null, "", "", `${HEAD} ${BODY}`),
        want: null,
    },
];

let bad = 0;
for (const c of cases) {
    /* getDs() 读的是桩里的 _ds（第一个 eval 用 var 声明 → 挂在 window 上） */
    dom.window._ds = c.ds;
    dom.window.eval("_getWrites().length = 0;");
    resync(null, 0);
    const got = dom.window.eval("(_getWrites().length ? _getWrites()[_getWrites().length - 1] : null)");
    const ok = c.want === null ? got === null : got === c.want;
    if (!ok) bad++;
    console.log((ok ? "  ok   " : "  FAIL ") + c.name);
    if (!ok) {
        console.log("    期望: " + JSON.stringify(c.want));
        console.log("    实际: " + JSON.stringify(got));
    }
}
console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
process.exit(bad ? 1 : 0);
