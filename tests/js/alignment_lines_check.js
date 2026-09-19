/* 对齐指令的"新契约"：正文不存、锚定栏展示、实跑注入、时间前后端同口径。
 *
 * 旧契约（已废）：对齐行写进 ds.prompts，锚一变就 resync 重写。三个真实代价——
 *   ① AI 扩写/优化把它当正文抄走或改写；
 *   ② 清了锚还留着 "Picture 1 aligns with…"，等于让模型参考一张不存在的图；
 *   ③ resync 只处理 `integrated_...:` **之后**的行，飘在字段前缀之前的裸对齐行
 *      永远摘不掉（贴一次 AI 产出就多一条，越积越多）。
 *
 * 新契约：stripAlignmentLines 只做减法（认出官方对齐整句就删），展示交给
 * mkAlignPreview，注入交给后端 nodes.py。这里锁住减法与句式/时间口径。
 *
 * jsdom 真跑：从 h3_director.js 抽出 pyRound / snapSecondsToFrames / framesToSeconds /
 * segAlignSeconds / v2InstrLines / stripAlignmentLines 与 KF_ALIGN_RE，桩掉 getDs 等。
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

function extractConst(name) {
    const re = new RegExp("^\\s*const " + name + " = [\\s\\S]*?;\\s*$", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到常量 " + name);
    return m[0];
}

const dom = new JSDOM("<!doctype html><html><body></body></html>", { runScripts: "outside-only" });
dom.window.eval(`
    var _ds = null;
    function getDs() { return _ds; }
    var _writes = [];
    function setPromptText(node, idx, text) {
        _writes.push(text);
        (_ds.prompts = _ds.prompts || [])[idx] = text;
    }
    function syncPromptEditor(node, idx, text) { return true; }
    function scheduleRefresh() {}
`);

/* 函数**逐个** eval 成 window 上的声明（不是塞进对象字面量）—— 它们之间有
 * 真实调用链（v2InstrLines → segAlignSeconds → framesToSeconds → snapSecondsToFrames
 * → pyRound），放进对象里就互相看不见了。 */
dom.window.eval([
    extractConst("KF_ALIGN_RE"),      // stripAlignmentLines 依赖它，必须先挂上
    extractFn("pyRound"),
    extractFn("snapFrames"),            // snapSecondsToFrames 转调它
    extractFn("snapSecondsToFrames"),
    extractFn("framesToSeconds"),
    extractFn("segAlignSeconds"),
    extractFn("v2InstrLines"),
    extractFn("stripAlignmentLines"),
    "function _getWrites(){ return _writes; }",
    /* const 在 eval 里是块级作用域、不会自动挂 window，显式导出一下 */
    ";window.KF_ALIGN_RE = KF_ALIGN_RE;",
].join("\n"));
const api = dom.window;

const KF_ALIGN_RE = dom.window.KF_ALIGN_RE;

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

/* ---------- 1) 时间口径：必须与后端 nodes._snap_seconds + frames_to_seconds 一致 ---------- */
/* 后端：f=max(5,round(sec*24)); k=max(0,round((f-5)/17)); frames=17k+5; sec=round(f/24,2)
 * 5s -> 124 帧 -> 5.17s（**不是** 5.00）—— 前端直接用原始秒就会和实跑注入差 0.17s。 */
ok(api.pyRound(6.5) === 6, "pyRound 必须是 banker's rounding（Python round 语义）",
    api.pyRound(6.5), 6);
ok(api.pyRound(7.5) === 8, "pyRound 7.5 -> 8（取偶）", api.pyRound(7.5), 8);
ok(api.snapSecondsToFrames(5) === 124, "5s -> 124 帧", api.snapSecondsToFrames(5), 124);
ok(api.snapSecondsToFrames(8) === 192, "8s -> 192 帧", api.snapSecondsToFrames(8), 192);
ok(api.framesToSeconds(124) === 5.17, "124 帧 -> 5.17s", api.framesToSeconds(124), 5.17);
ok(api.segAlignSeconds({ seconds: 5 }) === 5.17, "段 5s 的对齐 S.SS = 5.17",
    api.segAlignSeconds({ seconds: 5 }), 5.17);
ok(api.segAlignSeconds({}) === 5.17, "没写时长时默认 5s 也要走同一换算",
    api.segAlignSeconds({}), 5.17);

/* ---------- 2) 句式：逐字照抄 h3-dialect.md §1.2 ---------- */
const fl = api.v2InstrLines("FL2VA", 8, true, true, 3);
ok(fl.length === 1 && fl[0].indexOf("Picture 2 (from Shot 3) aligns with the 8.00-second mark") > 0,
    "FL2VA 单句双锚、尾锚 Shot=最后一镜、S.SS 两位小数", fl);
const i2 = api.v2InstrLines("I2VA", 5, true, false, 1);
ok(i2.length === 1
    && i2[0] === "For the target video, at 0.00 seconds into the target video, "
        + "<Picture 1> (from [Shot 1]) is fully referenced.",
    "I2VA 固定句", i2);
const l2 = api.v2InstrLines("L2VA", 5, false, true, 2);
ok(l2.length === 1 && l2[0].indexOf("<Picture 1> (from [Shot 2]) aligns with the 5.17-second mark") > 0,
    "L2VA 只有尾锚且是 1 号、取吸附后的 5.17s", l2);
ok(api.v2InstrLines("Ref2VA", 5, true, true, 1).length === 0,
    "混合模式不生成对齐句（帧锚走 subject_definitions）",
    api.v2InstrLines("Ref2VA", 5, true, true, 1));
ok(api.v2InstrLines("T2VA", 5, false, false, 1).length === 0, "T2VA 无对齐句",
    api.v2InstrLines("T2VA", 5, false, false, 1));

/* ---------- 3) stripAlignmentLines：只做减法 ---------- */
const FL8 = "How the reference pictures align with the target video — "
    + "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
    + "Picture 2 (from Shot 1) aligns with the 8.00-second mark of the target video.";
const I2S = "For the target video, at 0.00 seconds into the target video, "
    + "<Picture 1> (from [Shot 1]) is fully referenced.";
const BODY = "[Shot 1] A cyclist opens an umbrella.\n\noverall_soundscape: Rain falls.\n\nnon_diegetic_music: N/A";
const HEAD = "integrated_multimodal_description:";

function runStrip(text) {
    dom.window._ds = { prompts: [text], segments: [{ seconds: 8 }], first_frame: "", end_frame: "" };
    dom.window.eval("_getWrites().length = 0;");
    api.stripAlignmentLines(null, 0);
    const w = dom.window.eval("_getWrites()");
    return w.length ? w[w.length - 1] : null;
}

/* 3a 字段内的对齐行 */
ok(runStrip(`${HEAD} ${FL8}\n${BODY}`) === `${HEAD} ${BODY}`,
    "剥掉字段内的 FL2VA 对齐行，正文一字不动",
    runStrip(`${HEAD} ${FL8}\n${BODY}`), `${HEAD} ${BODY}`);

/* 3b 字段内 I2VA 句 */
ok(runStrip(`${HEAD} ${I2S}\n${BODY}`) === `${HEAD} ${BODY}`,
    "剥掉字段内的 I2VA 对齐行",
    runStrip(`${HEAD} ${I2S}\n${BODY}`), `${HEAD} ${BODY}`);

/* 3c **字段前缀之前**的裸对齐行 —— 老逻辑的漏洞：只处理前缀之后的行，
 *     于是粘贴/AI 带进来的这种永远摘不掉（截图里正文顶上那条的来源）。 */
ok(runStrip(`${FL8}\n\n${HEAD} ${BODY}`) === `${HEAD} ${BODY}`,
    "剥掉飘在字段前缀之前的裸对齐行",
    runStrip(`${FL8}\n\n${HEAD} ${BODY}`), `${HEAD} ${BODY}`);

/* 3d 六段式主体字段同样处理 */
ok(runStrip(`detailed_description: ${I2S}\n${BODY}`) === `detailed_description: ${BODY}`,
    "六段式 detailed_description 里的对齐行也要剥",
    runStrip(`detailed_description: ${I2S}\n${BODY}`), `detailed_description: ${BODY}`);

/* 3e 没有对齐行 → 不碰手写文本（避免每次回填都无谓改写） */
ok(runStrip(`${HEAD} ${BODY}`) === null, "原本没有对齐行 → 不动手写文本",
    runStrip(`${HEAD} ${BODY}`), null);

/* 3f 非官方字段结构的裸文本里的对齐行也要剥（不然贴一次多一条） */
ok(runStrip(`${I2S}\n${BODY}`) === BODY, "裸文本里的对齐行也要剥",
    runStrip(`${I2S}\n${BODY}`), BODY);

/* 3g KF_ALIGN_RE 必须认得下官方三句式（否则上面全是空转） */
ok(KF_ALIGN_RE.test(FL8), "KF_ALIGN_RE 认 FL2VA 整句");
ok(KF_ALIGN_RE.test(I2S), "KF_ALIGN_RE 认 I2VA 句");
ok(KF_ALIGN_RE.test(api.v2InstrLines("L2VA", 5, false, true, 1)[0]),
    "KF_ALIGN_RE 认 L2VA 句");

console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
process.exit(bad ? 1 : 0);
