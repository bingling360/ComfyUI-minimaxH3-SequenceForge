/* 富文本提示词编辑器（内联绿框）真跑检查：node + jsdom，直接抽 web/h3_director.js 的
 * shipped 源码执行，断言插/删/计数/落盘行为。失败时进程退出码 1。
 *
 * 跑法：NODE_PATH=<repo>/node_modules node tests/js/prompt_editor_check.js
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

function extractDecl(name, open, close) {
    const re = new RegExp("^const " + name + " = ", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到常量 " + name);
    const i = src.indexOf(open, m.index);
    let depth = 0;
    for (let j = i; j < src.length; j++) {
        if (src[j] === open) depth++;
        else if (src[j] === close) { depth--; if (!depth) return src.slice(m.index, j + 1) + ";"; }
    }
    throw new Error("括号不配平 " + name);
}

const code = [
    extractDecl("KIND_CAPS", "{", "}"),
    extractDecl("KIND_NAME", "{", "}"),
    "const REF_REPEAT_MAX = 9;",
    extractDecl("REF_TEMPLATES", "[", "]"),
    extractFn("refsFromText"),
    extractFn("createPromptEditor"),
    extractFn("syncRefsFromText"),
    extractFn("refCount"),
    extractFn("canAddRef"),
    extractFn("addSegmentRef"),
    extractFn("removeSegmentRef"),
    extractFn("applyPromptEdit"),
    extractFn("applyRefAnchorToV2"),
].join("\n");

const dom = new JSDOM("<!doctype html><html><body></body></html>", { pretendToBeVisual: true });
const { window } = dom;

const ds = {
    prompts: ["开头 @阿依 站着"],
    ref_assets: [
        { label: "阿依", kind: "image", file: "assets/a.png", asset_id: "", roles: [] },
        { label: "阿依的家", kind: "video", file: "assets/b.mp4", asset_id: "", roles: [] },
    ],
    segments: [{ refs: ["阿依"], frame_img: null, prompt_v2: null }],
};
const node = { widgets: [] };
const getDs = () => ds;
const setDs = () => true;
const cancelPromptWrite = () => {};
const schedulePromptFlush = () => {};
const scheduleRefresh = () => {};
const setLed = () => {};
const alert = (m) => { throw new Error("alert: " + m); };

const make = new Function("window", "document", "Event", "alert", "getDs", "setDs",
    "cancelPromptWrite", "schedulePromptFlush", "scheduleRefresh", "setLed",
    code + "\nreturn { createPromptEditor, canAddRef, addSegmentRef, removeSegmentRef," +
    " applyPromptEdit, applyRefAnchorToV2, syncRefsFromText, refsFromText, refCount," +
    " REF_TEMPLATES };");
const M = make(window, window.document, window.Event, alert, getDs, setDs,
    cancelPromptWrite, schedulePromptFlush, scheduleRefresh, setLed);

const fails = [];
function ok(cond, msg) {
    if (!cond) fails.push(msg);
}
function eq(got, want, msg) {
    if (JSON.stringify(got) !== JSON.stringify(want)) {
        fails.push(`${msg}：got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
    }
}

const ta = M.createPromptEditor({
    value: ds.prompts[0],
    labels: () => ds.ref_assets.map((a) => a.label),
    onRemove: (label) => { M.removeSegmentRef(node, 0, label); },
});
window.document.body.append(ta.el);

// ① 插入绿框：不得抛错（历史 bug：box.value undefined → reading 'length'）
let threw = null;
try { ta.insertTag("阿依"); } catch (e) { threw = e; }
ok(!threw, `insertTag 抛错：${threw && threw.message}`);
eq(ta.value, "开头 @阿依 站着 @阿依", "insertTag 结果");
eq(ds.segments[0].refs, ["阿依"], "插入后 refs 由正文同步");

// ② 引用语句式（锚定方式）：前后文一起进正文
ta.insertTag("阿依的家", "场景以 ", " 为准");
ok(ta.value.endsWith("场景以 @阿依的家 为准"), `句式插入：${ta.value}`);

// ③ 计数：重复引用
eq(ta.tagCount("阿依"), 2, "tagCount 阿依");

// ④ 删除短标签不得误伤长标签前缀（历史 bug：@阿依 吃掉 @阿依的家 → 剩「的家」）
ta.removeTag("阿依");
ok(ta.value.includes("@阿依的家"), `removeTag 误伤长标签：${ta.value}`);
eq(ta.tagCount("阿依"), 0, "removeTag 后计数");
eq(ta.tagCount("阿依的家"), 1, "长标签仍在");

// ⑤ 落盘：applyPromptEdit 写 prompts + 同步 refs
ta.insertTag("阿依");
M.applyPromptEdit(node, 0, ta);
eq(ds.prompts[0], ta.value, "applyPromptEdit 写正文");
eq(ds.segments[0].refs, ["阿依的家", "阿依"], "refs 顺序=正文出现顺序");

// ⑥ chip 点击链路（含锚定方式）：不抛错、正文含句式
threw = null;
try {
    const a = ds.ref_assets[0];
    const tplDef = M.REF_TEMPLATES[3];                 // 场景还原
    const phrase = tplDef[1](a.label);
    const m = /^(.*?)@([^\s]+)([\s\S]*)$/.exec(phrase);
    ok(!!m, "模板句式应含 @别名");
    if (m) ta.insertTag(a.label, m[1], m[3]); else ta.insertText(phrase);
    M.applyPromptEdit(node, 0, ta);
    M.applyRefAnchorToV2(node, 0, a.label, tplDef, "");
} catch (e) { threw = e; }
ok(!threw, `chip 点击链路抛错：${threw && threw.message}`);
ok(ta.value.includes("场景以 @阿依 为准"), `锚定方式句式未写入：${ta.value}`);

// ⑦ ✕ 清零：正文里该素材的绿框全部清掉，其它素材不动
ta.removeTag("阿依");
M.applyPromptEdit(node, 0, ta);
M.removeSegmentRef(node, 0, "阿依");
eq(ta.tagCount("阿依"), 0, "✕ 后该素材计数归零");
eq(ta.tagCount("阿依的家"), 1, "✕ 不影响其它素材");
eq(ds.segments[0].refs.filter((l) => l === "阿依").length, 0, "refs 里该素材归零");

// ⑧ 解析函数（与后端同口径）
eq(M.refsFromText("联系 a@阿依.com 不算", ds.ref_assets), [], "邮箱不误伤");
eq(M.refsFromText("@阿依的家 与 @阿依", ds.ref_assets), ["阿依的家", "阿依"], "最长优先");

if (fails.length) {
    console.error("FAIL:\n - " + fails.join("\n - "));
    process.exit(1);
}
console.log("OK: prompt editor runtime checks passed");
