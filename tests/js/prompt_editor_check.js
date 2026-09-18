/* 富文本提示词编辑器（内联绿框）真跑检查：node + jsdom，直接抽 web/h3_director.js 的
 * shipped 源码执行，断言插/删/计数/落盘行为。失败时进程退出码 1。
 *
 * 跑法：NODE_PATH=<repo>/node_modules node tests/js/prompt_editor_check.js
 *
 * 覆盖：插入/删除/计数/落盘、最长优先分词、晚到素材的裸别名即时补框
 * （normalizeLoose，引用监控"不及时"回归）、绿框 ✕ 同步回调 onRemove、
 * 绿框 ✕ 只取消一处（removeOneTag）与 chip ✕ 清全部（removeTag）语义分层、
 * 锚定方式「裸引用/无」已合并。
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

/** 抓单行 const（正则常量等）——从源码原样取，避免测试与实现脱节。 */
function extractConstLine(name) {
    const re = new RegExp("^const " + name + " = .*$", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到常量 " + name);
    return m[0];
}

const code = [
    extractDecl("KIND_CAPS", "{", "}"),
    extractDecl("KIND_NAME", "{", "}"),
    extractDecl("KIND_ICON", "{", "}"),
    extractDecl("KIND_TOKEN", "{", "}"),
    "const REF_REPEAT_MAX = 9;",
    extractDecl("REF_TEMPLATES", "[", "]"),
    extractConstLine("_CHIP_EXT_RE"),
    extractFn("chipLabelText"),
    extractConstLine("_LIB_REL_RE"),
    extractFn("viewUrl"),
    extractFn("inputViewUrl"),
    extractFn("assetPreviewUrl"),
    extractFn("buildAssetThumb"),
    extractFn("thumbSig"),
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
    " REF_TEMPLATES, KIND_ICON, thumbSig };");
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

/* ⑨ 引用监控"不及时"回归（线下报的 bug）：别名表是**活的**函数而不是建卡快照——
 * 素材晚于正文入池时，正文里的 @别名 会先落成裸文本；素材一入池，normalizeLoose()
 * 必须立刻把它补成绿框（旧实现只在 input 后 700ms 跑，且用建卡快照，补不上）。 */
let liveLabs = ["阿依"];
const ta2 = M.createPromptEditor({
    value: "", labels: () => liveLabs, onRemove: () => {},
});
window.document.body.append(ta2.el);
ta2.insertText("@新素材 出场");                       // 池里还没有「新素材」
eq(ta2.el.querySelectorAll(".h3d-rtag").length, 0, "未入池别名应是裸文本");
ok(ta2.normalizeLoose() === false, "没有裸别名时 normalizeLoose 不动 DOM");
liveLabs = ["阿依", "新素材"];                        // 上传完成 → 入池
ok(ta2.normalizeLoose() === true, "晚到别名应报告修复");
eq(ta2.el.querySelectorAll(".h3d-rtag").length, 1, "晚到别名补成绿框");
eq(ta2.el.querySelector(".h3d-rtag").dataset.label, "新素材", "绿框别名取最长匹配");
ok(ta2.normalizeLoose() === false, "补完再调应幂等（不重复动 DOM）");

/* ⑩ 绿框 ✕ 必须同步回调 onRemove：引用条的 chips/计数靠它即时归零，
 * 不能等下一次整卡重建（焦点守卫会把它挡掉 → "要再按别的才更新"）。 */
let removedLabel = null;
const ta3 = M.createPromptEditor({
    value: "@阿依 与 @阿依的家",
    labels: () => ds.ref_assets.map((a) => a.label),
    onRemove: (l) => { removedLabel = l; },
});
window.document.body.append(ta3.el);
const firstTag = ta3.el.querySelector(".h3d-rtag");
eq(firstTag.dataset.label, "阿依", "首个绿框=阿依");
firstTag.querySelector(".h3d-rtagx").dispatchEvent(
    new window.MouseEvent("mousedown", { bubbles: true, cancelable: true }));
eq(removedLabel, "阿依", "✕ 应触发 onRemove（引用条据此刷新）");
eq(ta3.tagCount("阿依"), 0, "✕ 后该素材绿框清零");
eq(ta3.tagCount("阿依的家"), 1, "✕ 不误伤长别名");

/* ⑪ 锚定方式：原「无」与「裸引用（不加描述）」插入的正文完全一样（都是裸 @标签），
 * 已合并成一个选项——裸引用即默认档，源码里不得再留「锚定方式：无」。 */
ok(M.REF_TEMPLATES[0][0] === "裸引用（不加描述）", `第 0 档应为裸引用：${M.REF_TEMPLATES[0][0]}`);
eq(M.REF_TEMPLATES[0][1]("阿依"), "@阿依", "裸引用句式=裸 @标签");
ok(M.REF_TEMPLATES[0][2] !== "" , "裸引用仍带官方保留标记（语义差异化保留）");
ok(!src.includes("锚定方式：无"), "不应再有「锚定方式：无」选项");

/* ⑫ 绿框 ✕ = 只取消**这一处**（本轮修复）：同一素材引用 N 次时点一下只少一处
 * （正文少一个 @别名 → refs 少一条 → 引用条 N 降到 N-1），而不是一次清光；
 * 降到 0 时才算清空。旧行为是把 ✕ 接到 removeTag（清全部）→ "点小叉就全叉掉"。 */
let removeHits = [];
const ta4 = M.createPromptEditor({
    value: "@阿依 开头，@阿依 结尾",
    labels: () => ds.ref_assets.map((a) => a.label),
    onRemove: (l) => { removeHits.push(l); },
});
window.document.body.append(ta4.el);
eq(ta4.tagCount("阿依"), 2, "初始两处引用");
const tag4 = ta4.el.querySelector(".h3d-rtag");
tag4.querySelector(".h3d-rtagx").dispatchEvent(
    new window.MouseEvent("mousedown", { bubbles: true, cancelable: true }));
eq(ta4.tagCount("阿依"), 1, "点一次 ✕ 只少一处");
eq(removeHits, ["阿依"], "每次 ✕ 回调一次 onRemove");
ok(ta4.value.includes("@阿依"), `正文仍留有另一处引用：${ta4.value}`);
ds.prompts[0] = "@阿依 开头，@阿依 结尾";
ds.segments[0].refs = ["阿依", "阿依"];
M.applyPromptEdit(node, 0, ta4);
eq(ds.segments[0].refs, ["阿依"], "引用条口径=一次 ✕ 只掉一处（旧实现 applyPromptEdit+removeSegmentRef 会连掉两处）");
const tag4b = ta4.el.querySelector(".h3d-rtag");
tag4b.querySelector(".h3d-rtagx").dispatchEvent(
    new window.MouseEvent("mousedown", { bubbles: true, cancelable: true }));
eq(ta4.tagCount("阿依"), 0, "再点一次才归零");
eq(ta4.value, "开头， 结尾", `两处都去掉后正文只剩文字：${ta4.value}`);

/* ⑬ 层层语义不能混：removeTag 仍是"清全部"（引用条 chip 旁的 ✕ 用它），
 * removeOneTag 是"只取一处"（绿框用）。 */
const ta5 = M.createPromptEditor({ value: "@阿依 与 @阿依", labels: () => ["阿依"], onRemove: () => {} });
window.document.body.append(ta5.el);
ta5.removeOneTag(ta5.el.querySelector(".h3d-rtag"));
eq(ta5.tagCount("阿依"), 1, "removeOneTag 只取一处");
ta5.removeTag("阿依");
eq(ta5.tagCount("阿依"), 0, "removeTag 清全部");

/* ⑭ 绿框标识跟活数据走（本轮新增）：素材在库里换类别/换文件后 redrawIcons() 要换掉
 * 标识节点；且必须**幂等** —— 签名没变时不能重建节点（卡片轻量重绘会反复调它，
 * 每次都重建 <video> 会让首帧反复重解码）。 */
let liveInfo = { 素材: { kind: "image", file: "assets/x.png", asset_id: "" } };
const ta6 = M.createPromptEditor({
    value: "@素材 出现",
    labels: () => Object.keys(liveInfo),
    assets: () => liveInfo,
    dir: () => "proj1",
    onRemove: () => {},
});
window.document.body.append(ta6.el);
const tag6 = ta6.el.querySelector(".h3d-rtag");
eq(tag6.querySelector(":scope > .h3d-thumb").tagName, "IMG", "初始图片绿框给 img 缩略图");
const nodeBefore = tag6.querySelector(":scope > .h3d-thumb");
ta6.redrawIcons();
eq(tag6.querySelector(":scope > .h3d-thumb"), nodeBefore, "签名没变：redrawIcons 不重建节点（幂等）");

liveInfo = { 素材: { kind: "audio", file: "assets/x.wav", asset_id: "" } };   // 库里改成音频
ta6.redrawIcons();
eq(tag6.querySelector(":scope > .h3d-thumb"), null, "换类别后旧 img 应被摘掉");
const mk6 = tag6.querySelector(":scope > .h3d-kindmark");
ok(!!mk6, "换类别后应换成类别图标");
eq(mk6 && mk6.textContent, M.KIND_ICON.audio, "音频类别图标=音符");
eq(tag6.dataset.label, "素材", "redrawIcons 不得动 dataset.label（序列化口径不变）");
eq(ta6.value, "@素材 出现", "redrawIcons 不得动正文文本");

/* ⑮ 官方标签 <Picture N> 的可视化（外部 agent 贴进来的官方格式文本）。
 * 要点：渲染成缩略图、序列化**原样还原**（绝不能改写成 @别名）、
 * 不参与 @别名 的计数与删除、挂不到素材时标红警示。 */
const TOKENS = { "<Picture 1>": "阿依", "<Picture 2>": "阿依的家" };
const tokAssets = () => ({
    阿依: { kind: "image", file: "assets/a.png", asset_id: "" },
    阿依的家: { kind: "video", file: "assets/b.mp4", asset_id: "" },
});
const ta7 = M.createPromptEditor({
    value: "subject_definitions:\n<Picture 1>：角色参考\n<Picture 2>：场景参考",
    labels: () => ds.ref_assets.map((a) => a.label),
    assets: tokAssets,
    dir: () => "",
    tokenMap: () => TOKENS,
});
window.document.body.append(ta7.el);
const tokEls = ta7.el.querySelectorAll(".h3d-rtok");
eq(tokEls.length, 2, "两个 <Picture N> 应各渲染成一个 token 框");
eq([...tokEls].map((n) => n.dataset.token), ["<Picture 1>", "<Picture 2>"],
    "dataset.token 记录标签原文");
eq(ta7.value, "subject_definitions:\n<Picture 1>：角色参考\n<Picture 2>：场景参考",
    "序列化必须原样还原 <Picture N>（不得改写成 @别名）");
ok(!ta7.value.includes("@阿依"), "正文里不应出现 @别名");
eq(ta7.tagCount("阿依"), 0, "token 不计入 @别名 引用次数");
ta7.removeTag("阿依");
ok(ta7.value.includes("<Picture 1>"), "removeTag（清引用）不得删掉 token 文本");
eq(ta7.value, "subject_definitions:\n<Picture 1>：角色参考\n<Picture 2>：场景参考",
    "removeTag 对纯 token 文本应是空操作");

/* 混用：@别名 与 <Picture N> 并存，各自形态不变 */
const ta8 = M.createPromptEditor({
    value: "@阿依 与 <Picture 2> 并存",
    labels: () => ds.ref_assets.map((a) => a.label),
    assets: tokAssets,
    dir: () => "",
    tokenMap: () => TOKENS,
});
eq(ta8.value, "@阿依 与 <Picture 2> 并存", "混用时两种形态都要原样保留");
eq(ta8.tagCount("阿依"), 1, "混用时 @别名 仍正常计数");
eq(ta8.el.querySelectorAll(".h3d-rtag:not(.h3d-rtok)").length, 1, "@别名 绿框 1 个");
eq(ta8.el.querySelectorAll(".h3d-rtok").length, 1, "token 框 1 个（两者互不干扰）");

/* 悬空 token：映射里没有 → 标红警示（"挂不到素材"的可视化） */
const ta9 = M.createPromptEditor({
    value: "<Picture 9>：没挂到素材",
    labels: () => [],
    assets: () => ({}),
    dir: () => "",
    tokenMap: () => TOKENS,
});
window.document.body.append(ta9.el);
eq(ta9.el.querySelectorAll(".h3d-rtok-missing").length, 1, "悬空 token 应标 h3d-rtok-missing");
eq(ta9.value, "<Picture 9>：没挂到素材", "悬空 token 也要原样保留");

/* 不传 tokenMap 时行为不变（老调用点不受影响） */
const ta10 = M.createPromptEditor({
    value: "@阿依 与 <Picture 2>",
    labels: () => ds.ref_assets.map((a) => a.label),
    assets: tokAssets,
    dir: () => "",
});
eq(ta10.el.querySelectorAll(".h3d-rtok").length, 0, "未传 tokenMap：不渲染 token 框");
eq(ta10.value, "@阿依 与 <Picture 2>", "未传 tokenMap：正文原样");

if (fails.length) {
    console.error("FAIL:\n - " + fails.join("\n - "));
    process.exit(1);
}
console.log("OK: prompt editor runtime checks passed");
