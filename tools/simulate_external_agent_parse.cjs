/**
 * 把 tools/simulate_external_agent.py 里手写的三段文本喂给**真解析器**，
 * 确认「照 skill 写出来的东西」真的能过总提示词框。
 *
 * 跑法：node tools/simulate_external_agent_parse.cjs
 */
const path = require("path");
const { load, ROOT } = require(path.join(__dirname, "..", "tests", "js", "_harness.js"));
const fs = require("fs");

const { w } = load();
const parseMasterPrompt = w.eval("parseMasterPrompt");
const mpRenderState = w.eval("mpRenderState");

/* 从 python 脚本里把三个 CASE 常量抠出来（保持单一真相，不复制粘贴） */
const py = fs.readFileSync(path.join(ROOT, "tools", "simulate_external_agent.py"), "utf8");
function extractCase(name) {
  const m = py.match(new RegExp(`^CASE_${name} = """([\\s\\S]*?)"""`, "m"));
  if (!m) throw new Error("找不到 CASE_" + name);
  return m[1];
}
const CASES = {
  A_base_two_segments: extractCase("A"),
  B_ref2va_full_mix: extractCase("B"),
  C_ref2va_two_segments: extractCase("C"),
};

let pass = 0, fail = 0;
const fails = [];
function check(name, cond, detail) {
  if (cond) pass++;
  else { fail++; fails.push(name + (detail ? "\n      " + String(detail).slice(0, 180) : "")); }
}

for (const [name, text] of Object.entries(CASES)) {
  const p = parseMasterPrompt(text);
  const want = name === "A_base_two_segments" ? 2 : (name === "B_ref2va_full_mix" ? 1 : 2);
  check(`${name}: 真解析器认出 ${want} 段`, p.segs.length === want, `实际 ${p.segs.length}`);
  check(`${name}: 无解析告警`, !(p.warnings && p.warnings.length), JSON.stringify(p.warnings));

  for (let i = 0; i < p.segs.length; i++) {
    const s = p.segs[i];
    check(`${name}: 段${i + 1} Duration 解析到`, Number(s.seconds) > 0, JSON.stringify(s.seconds));
    check(`${name}: 段${i + 1} Standalone 解析到（不是 undefined）`,
      s.unlink === true || s.unlink === false, JSON.stringify(s.unlink));
    const main = String(s.main || "");
    check(`${name}: 段${i + 1} 正文非空且带官方字段`, main.length > 60 &&
      (main.includes("integrated_multimodal_description:") || main.includes("subject_definitions:")),
      main.slice(0, 100));
    check(`${name}: 段${i + 1} 正文首尾未被段级标签污染`,
      !/^Duration:|^Standalone:/.test(main), main.slice(0, 60));
  }

  /* 往返幂等：解析 → 渲染 → 再解析，段数与正文逐段一致 */
  const state = p.segs.map(s => ({ main: s.main, seconds: s.seconds, unlink: s.unlink }));
  const re = mpRenderState(state);
  const p2 = parseMasterPrompt(re);
  check(`${name}: 往返后段数一致`, p2.segs.length === p.segs.length,
    `${p2.segs.length} vs ${p.segs.length}`);
  check(`${name}: 往返后正文逐段一致`,
    p2.segs.map(s => String(s.main).trim()).join("\u0001") ===
    p.segs.map(s => String(s.main).trim()).join("\u0001"));
  check(`${name}: 往返后 Standalone 一致`,
    p2.segs.map(s => s.unlink).join() === p.segs.map(s => s.unlink).join());
}

/* 逐项打印解析结果，便于人眼核对 */
console.log("=" * 0 + "\n逐段解析结果：");
for (const [name, text] of Object.entries(CASES)) {
  const p = parseMasterPrompt(text);
  console.log(`\n[${name}]  ${p.segs.length} 段`);
  p.segs.forEach((s, i) => {
    const main = String(s.main || "");
    const kind = main.includes("subject_definitions:") ? "Ref2VA六段式" : "三字段";
    console.log(`  段${i + 1}: ${s.seconds}s  Standalone=${s.unlink}  ${kind}  ${main.length}字`);
  });
}

console.log(`\n=== 外部 agent 产物过框验证（真解析器） ===`);
console.log(`PASS=${pass}  FAIL=${fail}`);
if (fail) { console.log("\n失败项："); fails.forEach(f => console.log("  x " + f)); }
process.exit(fail ? 1 : 0);
