/* 主节点 widgets_values 布局迁移的回归守卫。
 *
 * 背景（用户报的原始现象）：插件自带默认工作流一加载就「参数错位」——
 *   H3SeamlessChainSampler 校验失败：锚定加噪收到"分段"、审片模式收到 0、
 *   接缝重摇收到"文生视频"、重摇上限收到导演台状态 JSON、递减锚定收到"match"。
 *
 * 根因：h3_director.js 的 loadGraphData 拦截层用 `wv.length !== CUR_WIDGET_COUNT`
 *   判断「是不是旧布局」，而 CUR_WIDGET_COUNT 是手写常数。1148ae3 把控件从 29 值
 *   加到 31 值（新增「参考图像尺寸」「响度对齐强度」）时忘了同步这个常数，
 *   于是**完全正确的 31 值工作流被判成旧布局并强行重排**。改布局本身没错，
 *   错在判据依赖会漂移的长度。
 *
 * 本守卫钉死两条：
 *   A. 当前布局（含插件自带默认工作流）**一位都不许动**；
 *   B. 真旧布局（35 值 / 31 值 / 极老数字首项）仍要能迁到 30 值，尾部新控件补齐。
 *
 * 2026-09-23：主节点又少了一位 —— 「一采编码」控件删除（画质档位改由 ⚡ 性能优化
 *   弹窗的 encode_profile 管，节点上留一份是「一张表两处入口」，两处会打架）。
 *   于是当前布局 31 → 30 值，老 31 值存档需要 `splice(28, 1)` 摘掉那一格。
 *
 * 2026-09-24：默认工作流的**导演台参数**按用户导出更新（百万像素 0.4→1、
 *   审片模式 关闭→逐段确认、导演台状态换新 schema + 语义桥开启）。下面
 *   「关键位语义仍对齐」逐位写的就是**自带默认工作流自己的值**——它是串位探测器，
 *   所以以后**每次改默认工作流参数，这里对应的那几行必须同步改**，不许删。
 *
 * 用法：node tests/js/widget_layout_migrate_check.js
 *      （pytest 侧由 tests/test_js_checks.py 统一收集）
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { load, ROOT } = require("./_harness");

let ok = true;
const results = [];
function t(name, fn) {
    try { fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}

const { w, errors } = load({});
w.eval(fs.readFileSync(path.join(ROOT, "web", "h3_default_workflow.js"), "utf8"));

const clone = (o) => JSON.parse(JSON.stringify(o));
const samplerOf = (wf) => wf.nodes.find((n) => n.type === "H3SeamlessChainSampler");
const CUR = 30;   // 29 控件 + 种子 control 1 位

console.log("\n== 主节点 widgets_values 布局迁移 ==");

t("默认工作流已装入 harness", () => {
    assert.ok(w.H3_DEFAULT_WORKFLOW, "window.H3_DEFAULT_WORKFLOW 未定义");
});

t("迁移入口与判据都在（外层是函数声明，jsdom 间接 eval 里可见）", () => {
    assert.strictEqual(typeof w.migrateGraphWidgets, "function");
    assert.strictEqual(typeof w.isCurrentWidgetLayout, "function");
});

t("默认工作流本身是当前布局（30 值 / 首项宽高比 / 第 27 位导演台状态）", () => {
    const wv = samplerOf(clone(w.H3_DEFAULT_WORKFLOW)).widgets_values;
    assert.strictEqual(wv.length, CUR, "默认工作流控件值数应为 " + CUR);
    assert.strictEqual(typeof wv[0], "string", "首项应是宽高比字符串");
    assert.strictEqual(typeof wv[27], "string", "第 27 位应是导演台状态");
    assert.ok(wv[27].startsWith("{"), "第 27 位应是导演台状态 JSON");
});

t("★ 迁移层不得改动当前布局：默认工作流逐位原样通过", () => {
    const before = samplerOf(clone(w.H3_DEFAULT_WORKFLOW)).widgets_values;
    const graph = clone(w.H3_DEFAULT_WORKFLOW);
    w.migrateGraphWidgets(graph);
    const after = samplerOf(graph).widgets_values;
    assert.deepStrictEqual(after, before, "当前布局被迁移层改动了（参数会整体错位）");
});

t("迁移后关键位语义仍对齐（任何一位串位都会红）", () => {
    const graph = clone(w.H3_DEFAULT_WORKFLOW);
    w.migrateGraphWidgets(graph);
    const wv = samplerOf(graph).widgets_values;
    assert.strictEqual(wv[1], 1, "百万像素");
    assert.strictEqual(wv[2], 864, "宽度");
    assert.strictEqual(wv[8], 20, "步数");
    assert.strictEqual(wv[9], 1, "CFG");
    assert.strictEqual(wv[10], "res_multistep", "采样器");
    assert.strictEqual(wv[16], 34, "回退上限");
    assert.strictEqual(wv[17], 0, "锚定加噪");
    assert.strictEqual(wv[18], "逐段确认", "审片模式");
    assert.strictEqual(wv[21], "关闭", "接缝重摇");
    assert.strictEqual(wv[22], 0.06, "重摇阈值");
    assert.strictEqual(wv[23], 1, "重摇上限");
    assert.strictEqual(wv[24], "关闭", "递减锚定");
    assert.strictEqual(wv[25], "文生视频", "生成模式");
    assert.strictEqual(wv[26], "开启", "自动成片");
    assert.strictEqual(wv[28], "match", "参考图像尺寸");
    assert.strictEqual(wv[29], 1.0, "响度对齐强度");
});

t("判据：当前 30 值 / 上一版 29 值 → 视为当前，不重排", () => {
    const cur = samplerOf(clone(w.H3_DEFAULT_WORKFLOW)).widgets_values;
    assert.strictEqual(w.isCurrentWidgetLayout(cur), true);
    // 29 值只缺末尾两个后加控件，ComfyUI 会用控件默认值补齐 match / 1.0，无需重排
    assert.strictEqual(w.isCurrentWidgetLayout(cur.slice(0, 29)), true);
});

t("判据：老 31 值存档（含已删的「一采编码」）→ 视为旧，需摘掉那一格", () => {
    const cur = samplerOf(clone(w.H3_DEFAULT_WORKFLOW)).widgets_values;
    const old31 = cur.slice();
    old31.splice(28, 0, "高清");      // 老布局第 28 位是「一采编码」
    assert.strictEqual(old31.length, 31);
    assert.strictEqual(w.isCurrentWidgetLayout(old31), false, "老 31 值必须判为旧布局");
});

t("判据：被旧版改坏又被存下的 31 值存档（第 27 位是数字）也按当前处理", () => {
    const broken = samplerOf(clone(w.H3_DEFAULT_WORKFLOW)).widgets_values.slice();
    broken[27] = 1.0;   // 旧版迁移层把「重摇上限」的 1 留在了导演台状态位
    assert.strictEqual(w.isCurrentWidgetLayout(broken), true);
    const graph = { nodes: [{ type: "H3SeamlessChainSampler", widgets_values: broken }] };
    w.migrateGraphWidgets(graph);
    assert.deepStrictEqual(graph.nodes[0].widgets_values, broken, "不该被再迁一次");
});

t("判据：35 值陈旧布局 / 极老数字首项 → 视为旧，需迁移", () => {
    const wv35 = new Array(35).fill(0);
    wv35[0] = "16:9";
    wv35[27] = 1;                       // 该位是数字「重摇上限」，不是导演台状态
    assert.strictEqual(w.isCurrentWidgetLayout(wv35), false);
    const wv25 = new Array(25).fill(0); // 极老：首项是数字「宽度」
    assert.strictEqual(w.isCurrentWidgetLayout(wv25), false);
});

t("★ 旧 35 值布局仍能迁到 30 值，且尾部补上两个新控件", () => {
    const wv35 = new Array(35).fill(0);
    wv35[0] = "16:9";
    wv35[1] = 0.5;
    wv35[2] = 864;
    wv35[3] = 480;
    wv35[16] = 34;                                    // 回退上限
    wv35[33] = "文生视频";                             // 生成模式
    wv35[34] = '{"mode":"文生视频"}';                  // 导演台状态
    const graph = { nodes: [{ type: "H3SeamlessChainSampler", widgets_values: wv35 }] };
    w.migrateGraphWidgets(graph);
    const out = graph.nodes[0].widgets_values;
    assert.strictEqual(out.length, CUR, "迁移结果应为 " + CUR + " 值");
    assert.strictEqual(out[0], "16:9", "宽高比");
    assert.strictEqual(out[25], "文生视频", "生成模式来自旧 33 位");
    assert.strictEqual(out[26], "开启", "自动成片");
    assert.strictEqual(out[27], '{"mode":"文生视频"}', "导演台状态来自旧 34 位");
    assert.strictEqual(out[28], "match", "参考图像尺寸（已删的一采编码不再占位）");
    assert.strictEqual(out[29], 1.0, "响度对齐强度");
});

t("★ 老 31 值布局迁到 30 值：只摘掉第 28 位「一采编码」，其余逐位等价", () => {
    const cur = samplerOf(clone(w.H3_DEFAULT_WORKFLOW)).widgets_values;
    const old31 = cur.slice();
    old31.splice(28, 0, "高清");
    const graph = { nodes: [{ type: "H3SeamlessChainSampler", widgets_values: old31 }] };
    w.migrateGraphWidgets(graph);
    const out = graph.nodes[0].widgets_values;
    assert.deepStrictEqual(out, cur, "老 31 值迁移后应与当前 30 值逐位相同");
});

t("★ 当前布局但宽高比=「自定义」（已删档位）→ 只换画布两槽，其余位原样", () => {
    const before = samplerOf(clone(w.H3_DEFAULT_WORKFLOW)).widgets_values;
    const legacy = before.slice();
    legacy[0] = "自定义";
    // 864×480 面积 ≈0.3955MP → 就近 0.4MP；比例 1.8 → 16:9
    const graph = { nodes: [{ type: "H3SeamlessChainSampler", widgets_values: legacy }] };
    w.migrateGraphWidgets(graph);
    const out = graph.nodes[0].widgets_values;
    assert.strictEqual(out.length, CUR, "迁移不应改变值数");
    assert.strictEqual(out[0], "16:9", "宽高比应反推到最近比例");
    assert.strictEqual(out[1], 0.4, "百万像素应按面积就近（0.1 步进）");
    for (let i = 2; i < CUR; i++) {
        assert.strictEqual(out[i], before[i], `第 ${i} 位不应被迁移改动`);
    }
});

t("非主节点/空值不被误伤", () => {
    const other = { nodes: [{ type: "VAELoader", widgets_values: ["a.safetensors"] }] };
    w.migrateGraphWidgets(other);
    assert.deepStrictEqual(other.nodes[0].widgets_values, ["a.safetensors"]);
});

results.forEach((r) => console.log(r));
if (errors.length) { console.log("\n页面错误: " + errors.join(" | ")); ok = false; }
console.log(ok ? "\nwidget_layout_migrate_check 全部通过" : "\nwidget_layout_migrate_check 失败");
process.exit(ok ? 0 : 1);
