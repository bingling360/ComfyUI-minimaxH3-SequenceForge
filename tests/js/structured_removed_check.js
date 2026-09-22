/* 结构化提示词下线守卫（2026-09-22）。
 *
 * 结构化提示词（prompt_v2 逐镜表单 / 运镜下拉 / 屏显列表 / 预编译 + 编译预览）
 * 已**整体下线**：正文是唯一真相，编辑与「只能靠表单做」的那些活改由 AI 提示词优化承担
 * （规则注入 + 后端 prompts.finalize_optimized 确定性收尾 + prompts.validate_text 语义校验）。
 *
 * 两件事一起守：
 *   ① 反向 —— 结构化不许回流（源码里再出现任何一个入口即失败）；
 *   ② 正向 —— 不许「删过头」（与结构化无关的保留项一个都不能少）。
 *
 * 纯读文件断言，不需要 jsdom。跑法：NODE_PATH=<root>/node_modules node tests/js/structured_removed_check.js
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

const director = read("web/h3_director.js");
const promptsJs = read("web/h3_prompts.js");
const promptsPy = read("prompts.py");
const routesPy = read("routes.py");
const apiJs = read("web/h3_api.js");
const projectsPy = read("projects.py");

const fails = [];
function ok(cond, msg) { if (!cond) fails.push(msg); }

/* ---------- ① 反向：结构化不许回流 ---------- */
const GONE_DIRECTOR = [
    "openStructuredModal", "renderPromptV2Panel", "setPromptV2Field",
    "debouncePromptV2Write", "getSegPromptV2", "v2Details", "v2InstrPreview",
    "splitH3Sections", "applyH3TextToSeg", "applyRefAnchorToV2",
    "assignV2FromText", "V2_MODES", "effV2Mode", "V2_TASK_ZH", "V2_CAM_ZH",
    "V2_AMP_ZH", "V2_SPD_ZH", "compilePreview", "_v2Open", "v2Key",
    "prompt_v2", "v2mode",
];
for (const g of GONE_DIRECTOR) {
    ok(!director.includes(g), `h3_director.js 结构化遗留：${g}`);
}
const GONE_HELPER = [
    "defaultPromptV2", "defaultShot", "ensurePromptV2", "hasPromptV2",
    "migrateLegacySeg", "detectMode", "compilePayload",
    "CAMERA_MOVES", "CAMERA_AMPS", "CAMERA_SPEEDS", "RETENTION_MARKERS",
];
for (const g of GONE_HELPER) {
    ok(!promptsJs.includes(g), `h3_prompts.js 结构化遗留：${g}`);
}
const GONE_BACKEND = [
    "def compile_segment", "def clean_prompt", "def default_prompt",
    "def default_shot", "def migrate_legacy_seg", "def compose_description",
    "def compose_reference", "def compose_base", "CAMERA_MOVES",
];
for (const g of GONE_BACKEND) {
    ok(!promptsPy.includes(g), `prompts.py 结构化遗留：${g}`);
}
ok(!routesPy.includes("compile_prompt") && !routesPy.includes('"/h3chain/compile"'),
   "routes.py 里 /h3chain/compile 应已下线");
ok(!apiJs.includes("compilePreview"), "h3_api.js 的 compilePreview 应已下线");
ok(!projectsPy.includes("prompt_v2"), "projects.py 的 prompt_v2 透存应已下线");

/* ---------- ② 正向：不许删过头 ---------- */
const KEEP_DIRECTOR = [
    "defaultV2Mode", "v2InstrLines", "segAlignSeconds", "framesToSeconds",
    "snapSecondsToFrames", "mkAlignPreview", "framePictureNumbers",
    "v2RefsFromSchedule", "segHasFrames", "segHasFrames",
    "paintOptbar", "optToggle", "optRestoreMaps", "optKey", "_optShown",
    "stripAlignmentLines", "runOptForSegment", "collectSegMedia",
    "function addSegmentRef(", "function refsFromText(",
];
for (const k of KEEP_DIRECTOR) {
    ok(director.includes(k), `h3_director.js 误删保留项：${k}`);
}
const KEEP_HELPER = [
    "marksToText", "textToMarks", "markPairs", "markOf", "cleanMark",
    "markShaped", "nextMarkSeq", "defaultLatentSave", "cleanLatentSave",
];
for (const k of KEEP_HELPER) {
    ok(promptsJs.includes(k), `h3_prompts.js 误删保留项：${k}`);
}
const KEEP_BACKEND = [
    "def frames_to_seconds", "def alignment_lines", "def keyframe_line",
    "L2VA_HEAD", "FL2VA_HEAD", "def parse_override", "def serialize_fields",
    "def validate_compiled", "def detect_mode", "def finalize_optimized",
    "def validate_text",
];
for (const k of KEEP_BACKEND) {
    ok(promptsPy.includes(k), `prompts.py 误删保留项：${k}`);
}
// 段卡 tab 只剩 提示词 / 锚定设置
ok(director.includes('const tabs = [["main", "提示词"], ["set", "锚定设置"]];'),
   "段卡 tab 应只剩 提示词 / 锚定设置");

console.log(fails.length ? `\n${fails.length} 个断言失败：\n  ` + fails.join("\n  ")
                         : "\nstructured_removed_check 全部通过");
process.exit(fails.length ? 1 : 0);
