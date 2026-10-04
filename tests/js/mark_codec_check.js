/* 素材标注编解码真跑检查（node + jsdom，跑 shipped web/h3_prompts.js）。
 *
 * 跑法：node tests/js/mark_codec_check.js
 *
 * 不变量（B03 的地基，一旦破了就会**静默丢图**）：
 *  ① 出（marksToText）：@素材名 → @标注；最长名优先，不误伤 a@b.com
 *  ② 回（textToMarks）：@标注 → @素材名；容错漏 @ / 带空格 / 加括号
 *  ③ **幂等**：回填后的文本再出一次，结果不变（往返不漂移）
 *  ④ 不碰官方 token：<Picture 1> 原样保留（它属于另一层编号；回填另走 ⑤）
 *  ⑤ 池里没有的标注形态原样留下（不猜、不吞）
 *  ⑥ officialToMarks（回填兜底）：官方 `<Picture N>` / `<Video N>` / `<Audio N>`
 *     按送进 LLM 的 media 顺序译回 `@素材名` —— LLM 照官方规则写官方标签时，
 *     这一跳保证引用不丢（见 web/h3_prompts.js 的注释）
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "web", "h3_prompts.js"), "utf8");

/* h3_prompts.js 是 IIFE，挂 window.H3Prompts。给个最小 window 就能跑。 */
const win = {};
new Function("window", src)(win);
const HP = win.H3Prompts;
if (!HP || typeof HP.marksToText !== "function" || typeof HP.officialToMarks !== "function") {
    console.log("FAIL: h3_prompts.js 未导出 marksToText / textToMarks / officialToMarks");
    process.exit(1);
}

let fails = 0;
function eq(got, want, msg) {
    if (got === want) { console.log(`OK   ${msg}`); return; }
    console.log(`FAIL ${msg}\n     got  ${JSON.stringify(got)}\n     want ${JSON.stringify(want)}`);
    fails++;
}

const pool = [
    { label: "阿依", mark: "图片1" },
    { label: "阿依的家", mark: "图片2" },      // 前缀重叠：必须最长优先
    { label: "回廊", mark: "图片3" },
    { label: "片头", mark: "视频1" },
    { label: "BGM", mark: "音频1" },
    { label: "没标注的", mark: "" },           // 无标注：不参与换码
];

/* ① 出：素材名 -> 标注 */
eq(HP.marksToText("夜晚，@阿依 撑伞站在巷口", pool),
    "夜晚，@图片1 撑伞站在巷口", "出：单个引用");
eq(HP.marksToText("@阿依的家 的门口站着 @阿依", pool),
    "@图片2 的门口站着 @图片1", "出：最长名优先（阿依的家 ≠ 阿依）");
eq(HP.marksToText("@阿依@回廊@片头@BGM", pool),
    "@图片1@图片3@视频1@音频1", "出：三类各自独立编号");
eq(HP.marksToText("联系 a@b.com 或 @阿依", pool),
    "联系 a@b.com 或 @图片1", "出：不误伤邮箱（负向后顾）");
eq(HP.marksToText("@没标注的", pool), "@没标注的", "出：无标注条目不动");
eq(HP.marksToText("<Picture 1> 与 @阿依", pool),
    "<Picture 1> 与 @图片1", "出：官方 token 原样保留");

/* ② 回：标注 -> 素材名 */
eq(HP.textToMarks("@图片1 撑伞", pool), "@阿依 撑伞", "回：标准形态");
eq(HP.textToMarks("图片1 撑伞", pool), "@阿依 撑伞", "回：容错漏 @");
eq(HP.textToMarks("@图片 1 撑伞", pool), "@阿依 撑伞", "回：容错中间空格");
eq(HP.textToMarks("【图片3】的走廊", pool), "@回廊的走廊", "回：容错全角括号");
eq(HP.textToMarks("@图片10", pool), "@图片10", "回：不吞不存在的编号（图片10）");
eq(HP.textToMarks("@阿依 与 <Picture 1>", pool), "@阿依 与 <Picture 1>",
    "回：素材名与官方 token 都不动");
eq(HP.textToMarks("大图片1", pool), "大图片1", "回：前面是汉字时不当成标注（裸形态）");
eq(HP.textToMarks("@图片1@图片3", pool), "@阿依@回廊", "回：连写的标注都要认（显式 @ 不查前导）");
eq(HP.marksToText("@阿依@回廊", pool), "@图片1@图片3", "出：连写的素材名都要认（与后端 _REF_AT 同口径）");

/* ③ 幂等：出 → 回 → 出 应还原 */
const text0 = "【Shot 1】@阿依 撑伞，@片头 闪回；@BGM 起。";
const out1 = HP.marksToText(text0, pool);
const back = HP.textToMarks(out1, pool);
eq(back, text0, "幂等：出→回 完整还原");
eq(HP.marksToText(back, pool), out1, "幂等：再出一次结果相同");

/* ④ 空池/无标注池：原样返回，不做任何替换 */
eq(HP.marksToText("@阿依 撑伞", []), "@阿依 撑伞", "空池：原样");
eq(HP.textToMarks("@图片1", []), "@图片1", "空池：原样（不回退瞎猜）");

/* ⑤ 回填兜底：官方标签 -> 素材名（编号口径 = 送进 LLM 的 media 顺序）
 *
 * 场景：LLM 照官方 ref-en.txt 把正文写成 `<Picture 1>` 而不是 `@图片1`。
 * 只译标注的话官方标签会原样落盘 → 提示词框里出现 picture1，且 refsFromText
 * 扫不到 `@素材名` → 本段引用为空 → 素材不进模型。 */
const media = [
    { kind: "image", label: "图片1", images: ["d"] },
    { kind: "image", label: "图片2", images: ["d"] },
    { kind: "video", label: "视频1", images: ["d"] },
    { kind: "audio", label: "音频1" },
];
eq(HP.officialToMarks("<Picture 1> 撑伞", media, pool), "@阿依 撑伞", "官方回填：<Picture N> 按图序译回");
eq(HP.officialToMarks("<Picture 2> 站在门口", media, pool), "@阿依的家 站在门口", "官方回填：第二张图");
eq(HP.officialToMarks("<Video 1> 的运镜", media, pool), "@片头 的运镜", "官方回填：<Video N> 独立编号");
eq(HP.officialToMarks("<Audio 1> 起", media, pool), "@BGM 起", "官方回填：<Audio N> 独立编号");
eq(HP.officialToMarks("<Picture 3>", media, pool), "<Picture 3>", "官方回填：编号越界不猜");
eq(HP.officialToMarks("<Subject 1> 站着", media, pool), "<Subject 1> 站着",
    "官方回填：<Subject N> 不动（抽象标签，非素材引用）");
eq(HP.officialToMarks("<Picture 1>", [], pool), "<Picture 1>", "官方回填：无 media 原样（老调用点不受影响）");
eq(HP.officialToMarks("<Picture 1>", media, []), "<Picture 1>", "官方回填：池里查不到标注就不译（不留假引用）");
eq(HP.officialToMarks(HP.textToMarks("<Picture 1> 与 @图片2", pool), media, pool),
    "@阿依 与 @阿依的家", "官方回填：与 textToMarks 串联（模拟 fromLLMText）");

process.exit(fails ? 1 : 0);
