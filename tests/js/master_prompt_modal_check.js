/* 总提示词工作台 = **纯分段流水线** 冒烟（jsdom 真跑整个 h3_director.js）。
 *
 * 钉的是「工作台 ≠ 结果框、 ≠ AI 工作台」这三条边界：
 *  · 打开**不自动载入**链上提示词（不是段卡提示词的镜像/同步框）；
 *  · 弹窗里**没有任何 AI 入口**（分段优化 / 扩写 / 优化设置 / 原稿回退）；
 *  · 顶部不再有模式 / 缺省时长，不再有「参考素材（AI 可见）」chips；
 *  · 手动点「从当前链载入」才出现【段N】，且只写 时长 / 独立镜头 / 提示词，
 *    不写 意图 / 剧本 / 参考（资产引用只在正文里以 @素材名 出现）。
 *
 * 跑法：NODE_PATH=<repo>/node_modules node tests/js/master_prompt_modal_check.js
 */
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");

let harness;
try {
    harness = require("./_harness.js");
} catch (e) {
    console.error("SKIP: 未安装 jsdom（npm install）");
    process.exit(2);
}

const ds = {
    prompts: [
        "integrated_multimodal_description: [Shot 1] 实拍、电影感，中景框住雨夜窄巷。\n\n"
        + "overall_soundscape: 雨声持续。\n\nnon_diegetic_music: N/A",
        "integrated_multimodal_description: [Shot 1] 实拍，她转身挤进人流。",
    ],
    /* 第二段的 auto_ref=false → 独立镜头：是（导出侧口径 segAutoRef） */
    segments: [{ seconds: 9, refs: [], auto_ref: true },
        { seconds: 8, refs: [], auto_ref: false, unlink: true }],
    ref_assets: [],
};

/* mkNode 的 type 必须是导演台 findNode() 认的 NODE_TYPE，否则工作台静默走
 * "画布上未找到节点" 分支（见 _harness.js 里的说明）。 */
const { w, errors } = harness.load({ node: harness.mkNode(ds) });
const fails = [];
const ok = (cond, msg) => { if (!cond) fails.push(msg); };

ok(typeof w.openMasterPromptModal === "function", "openMasterPromptModal 未定义");
w.openMasterPromptModal();

const dialog = w.document.querySelector(".h3d-dialog-full");
ok(!!dialog, "工作台没打开（缺 .h3d-dialog-full）");
if (!dialog) {
    console.error("FAIL:\n - " + fails.join("\n - "));
    process.exit(1);
}

/* 1. 只有一个输入框
 * 只按类名找，**别钉 textarea 标签**：总提示词框是与段卡同一套的富文本编辑器
 * （div.h3d-rta，正文在 api.value 上，DOM 元素没有 .value）。 */
const boxes = dialog.querySelectorAll(".h3d-mpbox");
ok(boxes.length === 1, `输入框应为 1 个，实际 ${boxes.length} 个`);
const taEl = boxes[0];
const ta = taEl.__h3Editor || taEl;
ok(!!taEl.__h3Editor, "编辑器实例未挂到 DOM 元素上（拿不到正文）");

/* 2. 打开**不**自动载入：它是分配框，不是段卡提示词的结果框/镜像 */
ok(!String(ta.value || "").trim(), "打开时不应自动载入链上提示词：" + JSON.stringify(ta.value));
ok(String(ta.value || "").indexOf("【段1】") < 0, "打开即载入 = 又变回同步框");

/* 3. 按钮：只剩 载入 / 清空 / 取消 / 解析并分配（+ 框头的解析引用） */
const labels = [...dialog.querySelectorAll("button")].map((b) => String(b.textContent || "").trim());
for (const need of ["从当前链载入", "清空", "取消", "解析并分配", "🔗 解析引用"]) {
    ok(labels.includes(need), `缺「${need}」：` + JSON.stringify(labels));
}
/* AI 全套已撤下：这个框不做 AI，格式清洗在段卡上做 */
for (const dead of ["✨ AI分段提示词优化", "✨ AI扩写+优化", "↩ 原稿", "⚙ AI 优化设置"]) {
    ok(!labels.includes(dead), `「${dead}」应已撤下：` + JSON.stringify(labels));
}
ok(!labels.some((t) => t.indexOf("AI") >= 0), "工作台里不该再有 AI 入口：" + JSON.stringify(labels));

/* 4. 顶部参数行 / 参考素材 chips 行都已撤下（它们只服务于 AI 那一跳） */
ok(!dialog.querySelector(".h3d-mpset"), "顶部「模式 / 缺省时长 / AI 设置」行应已撤下");
ok(!dialog.querySelector(".h3d-mprefs"), "「参考素材（AI 可见）」chips 行应已撤下");
ok(dialog.querySelectorAll("select").length === 0, "不该再有模式下拉");
ok(dialog.querySelectorAll('input[type="number"]').length === 0, "不该再有缺省时长输入");

/* 5. 手动载入：出现两段，只写 时长 / 独立镜头 / 提示词 */
const bLoad = [...dialog.querySelectorAll("button")]
    .find((b) => String(b.textContent || "").trim() === "从当前链载入");
ok(!!bLoad, "找不到「从当前链载入」按钮");
bLoad.onclick();
ok(ta.value.indexOf("【段1】") >= 0, "载入后缺【段1】：" + JSON.stringify(ta.value));
ok(ta.value.indexOf("【段2】") >= 0, "载入后缺【段2】");
ok(ta.value.indexOf("时长：9") >= 0, "段时长应随载入带上：\n" + ta.value);
ok(ta.value.indexOf("独立镜头：是") >= 0, "独立镜头（跳过引用上段）应随载入带上：\n" + ta.value);
ok(ta.value.indexOf("提示词：") >= 0, "正文应带「提示词：」标签（导出口径）");
for (const dead of ["意图：", "剧本：", "参考：", "场景：", "角色：", "环境音：", "配乐："]) {
    ok(ta.value.indexOf(dead) < 0, `载入不该再写「${dead}」标签`);
}

const real = (errors || []).filter((e) => !/Not implemented|jsdom/i.test(String(e)));
ok(real.length === 0, "运行时报错：" + JSON.stringify(real));

if (fails.length) {
    console.error("FAIL:\n - " + fails.join("\n - "));
    process.exit(1);
}
console.log("OK: 总提示词工作台 = 纯分段流水线（无 AI、不自动同步）");
