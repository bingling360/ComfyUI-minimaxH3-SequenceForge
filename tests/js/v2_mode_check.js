/* 具象化模式判定：前端 defaultV2Mode 必须与后端 prompts.detect_mode 同口径。
 *
 * jsdom 真跑：加载 web/h3_prompts.js（提供 H3Prompts.detectMode / ensurePromptV2，
 * 就是后端 detect_mode / migrate_legacy_seg 的镜像），再从 h3_director.js 抽出
 * defaultV2Mode 实测。
 *
 * 重点回归：段**未启用**具象化（没有 prompt_v2）但主框引用了素材时，
 * 后端 compilePayload 会 migrateLegacySeg 把 seg.refs 迁成 references → Ref2VA；
 * 前端若只读 seg.prompt_v2（此时 null）就会漏判成 T2VA，
 * 于是前端传 T2VA、后端判 Ref2VA —— 既多报 W_MODE_OVERRIDE，模板也选错
 * （三段式 vs 六段式）。
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

const dom = new JSDOM("<!doctype html><html><body></body></html>", { runScripts: "outside-only" });
dom.window.eval(promptsSrc);
if (!dom.window.H3Prompts || !dom.window.H3Prompts.detectMode) {
    console.error("FAIL: H3Prompts.detectMode 未挂载");
    process.exit(1);
}
/* segHasFrames 是 has_start / has_end 的唯一口径（段级 frame_img 优先），
 * defaultV2Mode 依赖它，得一起 eval 进来。 */
const defaultV2Mode = dom.window.eval(
    extractFn("segHasFrames") + "\n" + extractFn("defaultV2Mode") + "\ndefaultV2Mode");
if (typeof defaultV2Mode !== "function") {
    console.error("FAIL: defaultV2Mode 未取到");
    process.exit(1);
}

/* 后端同口径的参照实现（直接拿 H3Prompts，等价于 detect_mode）。
 * has_start / has_end 必须与节点实跑一致：段级 frame_img 优先，
 * 其次才是全局 first_frame / end_frame（分别只对首段 / 末段生效）。 */
function backendMode(ds, idx) {
    const HP = dom.window.H3Prompts;
    const seg = (ds.segments || [])[idx] || {};
    const nP = (ds.prompts || []).length;
    const fi = (seg.frame_img && typeof seg.frame_img === "object") ? seg.frame_img : {};
    const pv = HP.compilePayload
        ? (seg.prompt_v2 || HP.migrateLegacySeg(Object.assign({}, seg, { prompt: (ds.prompts || [])[idx] || "" })))
        : seg.prompt_v2;
    return HP.detectMode(pv, {
        has_start: !!(String(fi.first || "").trim() || (ds.first_frame && idx === 0)),
        has_end: !!(String(fi.end || "").trim() || (ds.end_frame && idx === nP - 1)),
    });
}

const cases = [
    {
        name: "未启用具象化 + 主框引用素材 → Ref2VA（后端 migrate 会看到 refs）",
        ds: { prompts: ["a", "b", "c"], segments: [{}, { refs: ["角色1"] }, {}] },
        idx: 1,
        want: "Ref2VA",
    },
    {
        name: "已启用具象化 + 主框有引用但 prompt_v2 无参考 → 不串味，按帧判定",
        ds: {
            prompts: ["a", "b"],
            segments: [{ refs: ["角色1"], prompt_v2: { shots: [{ id: 1, description: "x" }] } }],
        },
        idx: 0,
        want: "T2VA",
    },
    {
        name: "已启用具象化 + 自己有 references → Ref2VA",
        ds: {
            prompts: ["a", "b"],
            segments: [{ prompt_v2: { references: [{ label: "角色1" }], shots: [{ id: 1, description: "x" }] } }],
        },
        idx: 0,
        want: "Ref2VA",
    },
    {
        name: "首段有首帧、末段有尾帧 → 首段 FL2VA",
        ds: { prompts: ["a"], first_frame: "s.png", end_frame: "e.png", segments: [{}] },
        idx: 0,
        want: "FL2VA",
    },
    {
        name: "中间段无帧无引用 → T2VA（段链中段不认首尾帧）",
        ds: { prompts: ["a", "b", "c"], first_frame: "s.png", end_frame: "e.png", segments: [{}, {}, {}] },
        idx: 1,
        want: "T2VA",
    },
    {
        name: "单段只有首帧 → I2VA",
        ds: { prompts: ["a"], first_frame: "s.png", segments: [{}] },
        idx: 0,
        want: "I2VA",
    },
    {
        name: "单段只有尾帧 → L2VA",
        ds: { prompts: ["a"], end_frame: "e.png", segments: [{}] },
        idx: 0,
        want: "L2VA",
    },
    {
        name: "中段有段级首帧图 → I2VA（锚定是段级的，不再只认首段的全局首帧）",
        ds: {
            prompts: ["a", "b", "c"], first_frame: "s.png", end_frame: "e.png",
            segments: [{}, { frame_img: { first: "assets/mid.png" } }, {}],
        },
        idx: 1,
        want: "I2VA",
    },
    {
        name: "中段有段级首尾帧图 → FL2VA",
        ds: {
            prompts: ["a", "b", "c"],
            segments: [{}, { frame_img: { first: "assets/m1.png", end: "assets/m2.png" } }, {}],
        },
        idx: 1,
        want: "FL2VA",
    },
];

let bad = 0;
for (const c of cases) {
    const got = defaultV2Mode(c.ds, c.idx);
    const backend = backendMode(c.ds, c.idx);
    const okMode = got === c.want;
    const okSame = got === backend;
    if (!okMode || !okSame) bad++;
    console.log(
        (okMode && okSame ? "  ok   " : "  FAIL ")
        + c.name
        + "  → 前端 " + got + " / 后端 " + backend + "（期望 " + c.want + "）"
    );
}
console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
process.exit(bad ? 1 : 0);
