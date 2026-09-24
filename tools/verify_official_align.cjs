/**
 * 全链路对齐验证（Task #6）：扮演外部 agent → 用 skill 产出 → 进总提示词框 → 与官方格式逐项对比。
 *
 * 跑法：node tools/verify_official_align.cjs
 * 退出码 0 = 全绿。
 *
 * 关键点：用的是**真解析器**（web/h3_director.js 的 parseMasterPrompt / mpRenderState /
 * refsFromText），经 tests/js/_harness.js 剥掉 ESM import 后整份源码跑起来 ——
 * 不在测试里重写一份解析逻辑（重写就等于没验）。
 */
const fs = require("fs");
const path = require("path");
const { load, ROOT } = require(path.join(__dirname, "..", "tests", "js", "_harness.js"));

let pass = 0, fail = 0;
const fails = [];
function check(name, cond, detail) {
  if (cond) pass++;
  else { fail++; fails.push(name + (detail ? "\n      " + String(detail).slice(0, 200) : "")); }
}

/* 解析器从 harness 暴露的全局取：h3_director.js 的顶层 function 声明会挂到 window。 */
const { w } = load();
const parseMasterPrompt = w.eval("parseMasterPrompt");
const mpRenderState = w.eval("mpRenderState");
const refsFromText = w.eval("refsFromText");

check("parseMasterPrompt 可用（真解析器）", typeof parseMasterPrompt === "function");
check("mpRenderState 可用", typeof mpRenderState === "function");

/* ---------- 基准：官方字段集从官方规则文件里抽，不手抄 ---------- */
const OFFICIAL_BASE = fs.readFileSync(path.join(ROOT, "prompt", "minimaxh3_base_prompt_writing.txt"), "utf8");
const OFFICIAL_REF = fs.readFileSync(path.join(ROOT, "prompt", "minimaxh3_official_ref2v_prompt_writing.txt"), "utf8");
const BASE_FIELDS = ["integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"];
const REF_FIELDS = ["subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music"];
for (const f of BASE_FIELDS) check(`官方 base-en 里有 ${f}`, OFFICIAL_BASE.includes(f));
for (const f of REF_FIELDS) check(`官方 ref-en 里有 ${f}`, OFFICIAL_REF.includes(f));

function fieldsInOrder(text, names) {
  let prev = -1;
  for (const n of names) {
    const i = text.indexOf(n + ":");
    if (i < 0 || i < prev) return false;
    prev = i;
  }
  return true;
}

/* ---------- 用例 1：base（I2VA 形态）· 图 + 音频标 混排 ---------- */
{
  const text = `[Segment 1]
Duration: 8
Standalone: no

Prompt:
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description: Live-action, cinematic realism. [Shot 1] A medium shot frames @女主_正面.png at the mouth of a rainy alley, the neon of @雨夜窄巷.png dragging long red streaks across the standing water. The camera pushes in with small amplitude at slow speed.

[Shot 2] At 00:03.200, the camera cuts to a close-up as she turns her head, rain beading on her lashes.

overall_soundscape: Steady rain taps the metal stall roofs; a distant vendor calls out over the crowd.

non_diegetic_music: N/A

[END]`;
  check("用例1：无中文标签", !/【|】|时长：|独立镜头|提示词：/.test(text));
  const p = parseMasterPrompt(text);
  const s = p.segs[0] || {};
  check("用例1：段数 = 1", p.segs.length === 1, `实际 ${p.segs.length}`);
  check("用例1：Duration = 8", Number(s.seconds) === 8, JSON.stringify(s.seconds));
  check("用例1：Standalone:no → unlink=false", s.unlink === false, JSON.stringify(s.unlink));
  check("用例1：对齐指令原样进正文", String(s.main).includes("at 0.00 seconds into the target video"));
  check("用例1：三字段齐全且顺序对", fieldsInOrder(String(s.main), BASE_FIELDS), String(s.main).slice(0, 120));
  check("用例1：字段间空行保留", String(s.main).includes("\n\noverall_soundscape"));
  /* ⚠ refs 派生走的是 **素材对象池**（取 a.ref_name || a.label），不是字符串数组 */
  const pool = [{ ref_name: "女主_正面.png" }, { ref_name: "雨夜窄巷.png" }];
  const derived = refsFromText(String(s.main), pool);
  check("用例1：@素材名 派生 refs", derived.length === 2, JSON.stringify(derived));
}

/* ---------- 用例 2：Ref2VA · 多图（正文不许出现 @） ---------- */
{
  const text = `[Segment 1]
Duration: 10
Standalone: no

Prompt:
subject_definitions:
<Subject 1> is the content defined by <Picture 1>, the reference image "女主_正面.png".
<Subject 2> is the content defined by <Picture 2>, the reference image "雨夜窄巷.png".

summary: [reference generation] <Subject 1> stops at the mouth of the alley defined by <Subject 2>.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - face, hair and coat unchanged.
<Subject 2> (appears in [Shot 1]): fully_preserved - neon layout and wet ground unchanged.

detailed_description: Live-action, cinematic realism with volumetric light. [Shot 1] <Subject 1> stops at the alley mouth as the neon of <Subject 2> drags long streaks across the standing water.

[Shot 2] At 00:04.000, the camera cuts to a close-up as <Subject 1> turns her head.

overall_soundscape: Steady rain taps the metal stall roofs.

non_diegetic_music: N/A

[END]`;
  const p = parseMasterPrompt(text);
  const s = p.segs[0] || {};
  const body = String(s.main);
  check("用例2：段数 = 1", p.segs.length === 1, `实际 ${p.segs.length}`);
  check("用例2：六段齐全且顺序对", fieldsInOrder(body, REF_FIELDS), body.slice(0, 120));
  const afterDefs = body.split("summary:")[1] || "";
  check("用例2：summary 及以后正文里没有 `@`（用户报的 bug）", !afterDefs.includes("@"), afterDefs.slice(0, 160));
  check("用例2：定义行用 <Picture N> 指来源", /<Picture \d>/.test(body));
  check("用例2：retention 用可见内容标记", body.includes("fully_preserved"));
  const retSec = body.split("retention_analysis:")[1].split("detailed_description:")[0];
  check("用例2：retention 段里不写 (Sx)", !/\(S\d\)/.test(retSec), retSec.slice(0, 120));
}

/* ---------- 用例 3：Ref2VA · 图 + 视频 + 音频 全混合 ---------- */
{
  const text = `[Segment 1]
Duration: 12
Standalone: yes

Prompt:
subject_definitions:
<Subject 1> is the content defined by <Picture 1>, the reference image "女主_正面.png".
<Video 1> is the content defined by <Video 1>, the reference video "运镜参考.mp4".
<Audio 1> is the content defined by <Audio 1>, the reference audio "现场雨声.wav".

summary: [reference generation + video continuation] <Subject 1> continues into the alley, following the camera move of <Video 1>, over <Audio 1>.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - face and coat unchanged.
<Video 1> (appears in [Shot 1]): partially_preserved - camera direction and speed retained.
<Audio 1> (appears in [Shot 1]): fully_copy - the rain bed is copied verbatim.

detailed_description: Live-action. [Shot 1] <Subject 1> walks deeper into the alley, the camera tracking right with moderate amplitude at slow speed.

overall_soundscape: Rain and distant traffic.

non_diegetic_music: N/A

[END]`;
  const p = parseMasterPrompt(text);
  const s = p.segs[0] || {};
  const body = String(s.main);
  check("用例3：段数 = 1", p.segs.length === 1, `实际 ${p.segs.length}`);
  check("用例3：Standalone:yes → unlink=true", s.unlink === true, JSON.stringify(s.unlink));
  check("用例3：六段齐全且顺序对", fieldsInOrder(body, REF_FIELDS));
  check("用例3：<Video N> 定义行存在", body.includes("<Video 1>"));
  check("用例3：<Audio N> 定义行存在", body.includes("<Audio 1>"));
  check("用例3：音频保留标记用 fully_copy", body.includes("fully_copy"));
  const afterDefs = body.split("summary:")[1] || "";
  check("用例3：正文无 `@`", !afterDefs.includes("@"), afterDefs.slice(0, 160));
}

/* ---------- 用例 4：多段往返（渲染 → 再解析 幂等） ---------- */
{
  const text = `[Segment 1]
Duration: 8
Standalone: no

Prompt:
integrated_multimodal_description: [Shot 1] Live-action. A wide shot frames the massif.

overall_soundscape: Wind.

non_diegetic_music: N/A

[Segment 2]
Duration: 10
Standalone: yes

Prompt:
integrated_multimodal_description: [Shot 1] Live-action. The ridge gives way.

overall_soundscape: Roar.

non_diegetic_music: N/A

[END]`;
  const p = parseMasterPrompt(text);
  check("用例4：段数 = 2", p.segs.length === 2, `实际 ${p.segs.length}`);
  check("用例4：段1 Duration=8", Number(p.segs[0].seconds) === 8, JSON.stringify(p.segs[0].seconds));
  check("用例4：段2 Duration=10", Number(p.segs[1].seconds) === 10, JSON.stringify(p.segs[1].seconds));
  check("用例4：段1 Standalone=no", p.segs[0].unlink === false, JSON.stringify(p.segs[0].unlink));
  check("用例4：段2 Standalone=yes", p.segs[1].unlink === true, JSON.stringify(p.segs[1].unlink));
  check("用例4：段1 正文含 integrated_…", String(p.segs[0].main).includes("integrated_multimodal_description"));

  const state = p.segs.map(s => ({ main: s.main, seconds: s.seconds, unlink: s.unlink }));
  const re = mpRenderState(state);
  check("用例4：渲染用英文段头 [Segment 1]", /^\[Segment 1\]/.test(re.trim()), re.slice(0, 40));
  check("用例4：渲染用 [END]", re.trim().endsWith("[END]"));
  check("用例4：渲染无中文标签", !/【|时长：|独立镜头|提示词：/.test(re));
  check("用例4：渲染写 Duration", re.includes("Duration: 8") && re.includes("Duration: 10"));
  check("用例4：渲染写 Standalone 两态", re.includes("Standalone: no") && re.includes("Standalone: yes"));
  const p2 = parseMasterPrompt(re);
  check("用例4：渲染→再解析 段数一致", p2.segs.length === p.segs.length, `${p2.segs.length} vs ${p.segs.length}`);
  check("用例4：渲染→再解析 正文一致",
    p2.segs.map(s => String(s.main).trim()).join("|") === p.segs.map(s => String(s.main).trim()).join("|"));
  check("用例4：渲染→再解析 Standalone 一致",
    p2.segs.map(s => s.unlink).join() === p.segs.map(s => s.unlink).join());
  check("用例4：渲染→再解析 Duration 一致",
    p2.segs.map(s => Number(s.seconds)).join() === p.segs.map(s => Number(s.seconds)).join());
}

/* ---------- 用例 5：中文旧标签仍可解析（只读兼容） ---------- */
{
  const legacy = `【段1】\n时长：8\n独立镜头：否\n\n提示词：\nintegrated_multimodal_description: [Shot 1] old.\n\noverall_soundscape: x.\n\nnon_diegetic_music: N/A\n\n【完】`;
  const p = parseMasterPrompt(legacy);
  check("用例5：中文旧标签仍解析出 1 段", p.segs.length === 1, `实际 ${p.segs.length}`);
  check("用例5：中文旧标签 Duration=8", Number(p.segs[0].seconds) === 8, JSON.stringify(p.segs[0].seconds));
  check("用例5：中文旧标签 Standalone=否 → unlink=false", p.segs[0].unlink === false, JSON.stringify(p.segs[0].unlink));
}

/* ---------- 用例 6：@素材名 旁路与最长匹配 ---------- */
{
  const pool = [{ ref_name: "女主_正面.png" }, { ref_name: "女主_正面.png_extra" }];
  const refs = refsFromText("text @女主_正面.png and @女主_正面.png_extra", pool);
  check("用例6：refsFromText 取最长命中（不是短前缀）",
    refs.length === 2 && refs[0] === "女主_正面.png" && refs[1] === "女主_正面.png_extra",
    JSON.stringify(refs));
  /* `@` 前一个字符是字母/数字/下划线时不认（防 a@b.com） */
  const r2 = refsFromText("mail a@女主_正面.png", pool);
  check("用例6：`@` 前是字母时不认（防邮箱）", r2.length === 0, JSON.stringify(r2));
  /* 重复引用重复计数 */
  const r3 = refsFromText("@女主_正面.png ... @女主_正面.png", pool);
  check("用例6：重复引用重复计数", r3.length === 2, JSON.stringify(r3));
}

console.log(`\n=== 全链路对齐验证（真解析器） ===`);
console.log(`PASS=${pass}  FAIL=${fail}`);
if (fail) {
  console.log("\n失败项：");
  fails.forEach(f => console.log("  ✗ " + f));
}
process.exit(fail ? 1 : 0);
