/* 右栏折叠面板的状态记忆（foldSection）回归守卫。
 *
 * 背景（用户报的原始现象）：二采面板改一个设置，面板就当场折叠回去 ——
 * 因为面板每次数据刷新都会被**整块重建**，重建时按硬编码的默认展开态走。
 * 「默认折叠」只该管**首次打开**的样子；用户手动展开过之后，重建（改参数、
 * 切换段、退出导演台再进来）都必须沿用他最后那次的状态。
 *
 * 用法：node tests/js/fold_section_check.js
 */
const assert = require("assert");
const { load } = require("./_harness");

let ok = true;
const results = [];
function t(name, fn) {
    try { fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}

const { w, errors } = load({});

const fire = (det) => det.dispatchEvent(new w.Event("toggle"));

console.log("\n== foldSection 折叠态记忆 ==");

t("是函数（顶层函数声明在 jsdom 的间接 eval 里可见）", () => {
    assert.strictEqual(typeof w.foldSection, "function");
});

t("首次：按 defaultOpen 给展开态", () => {
    assert.strictEqual(w.foldSection("t-open", true, "<summary>A</summary>").open, true);
    assert.strictEqual(w.foldSection("t-closed", false, "<summary>B</summary>").open, false);
});

t("用户展开后重建：保持展开（不被默认折叠弹回去）", () => {
    const d1 = w.foldSection("t-remember", false, "<summary>X</summary>");
    assert.strictEqual(d1.open, false, "首次该是折叠的");
    d1.open = true;
    fire(d1);
    const d2 = w.foldSection("t-remember", false, "<summary>X</summary>");
    assert.strictEqual(d2.open, true, "重建后应保持展开");
});

t("用户收起后重建：保持收起（即使 defaultOpen=true）", () => {
    const d1 = w.foldSection("t-remember2", true, "<summary>Y</summary>");
    assert.strictEqual(d1.open, true, "首次该是展开的");
    d1.open = false;
    fire(d1);
    const d2 = w.foldSection("t-remember2", true, "<summary>Y</summary>");
    assert.strictEqual(d2.open, false, "重建后应保持收起");
});

t("不同 id 互不干扰", () => {
    const a = w.foldSection("t-a", false, "<summary>a</summary>");
    a.open = true;
    fire(a);
    assert.strictEqual(w.foldSection("t-b", false, "<summary>b</summary>").open, false);
});

t("带 class 的用法（二采面板靠 classList 加 h3d-updet / h3d-upsub）", () => {
    const d = w.foldSection("t-cls", false, "<summary>z</summary>");
    d.classList.add("h3d-upsub");
    assert.ok(d.classList.contains("h3d-adv"), "基础类应是 h3d-adv");
    assert.ok(d.classList.contains("h3d-upsub"));
    assert.strictEqual(d.querySelector("summary").textContent, "z");
});

results.forEach((r) => console.log(r));
if (errors.length) { console.log("\n页面错误: " + errors.join(" | ")); ok = false; }
console.log(ok ? "\nfold_section_check 全部通过" : "\nfold_section_check 失败");
process.exit(ok ? 0 : 1);
