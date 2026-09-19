/* 总提示词工作台**单框**冒烟（jsdom 真跑整个 h3_director.js）。
 *
 * 覆盖的是「三框 → 单框」这次重写最容易当场炸的地方：
 *  · 打开工作台不抛错，且**只有一个**输入框（不是三个）；
 *  · 「✨ AI扩写 → 剧本」等扩写入口已撤下，只剩「AI分段提示词优化」+「解析并分配」；
 *  · 打开即载入当前链：框里出现【段N】，且**不写** 意图 / 剧本 两个旧标签；
 *  · 顶部只留模式 / 缺省时长（段数·风格·时长范围随多段扩写一起撤下）。
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
    segments: [{ seconds: 9, refs: [] }, { seconds: 8, refs: [] }],
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
 * 只按类名找，**别钉 textarea 标签**：总提示词框已从裸 textarea 换成与段卡
 * 同一套富文本编辑器（div.h3d-rta，正文在 api.value 上，DOM 元素没有 .value）。 */
const boxes = dialog.querySelectorAll(".h3d-mpbox");
ok(boxes.length === 1, `输入框应为 1 个，实际 ${boxes.length} 个`);
const taEl = boxes[0];
/* 编辑器实例挂在元素上（box.__h3Editor），正文要从它读 */
const ta = taEl.__h3Editor || taEl;
ok(!!taEl.__h3Editor, "编辑器实例未挂到 DOM 元素上（拿不到正文）");

/* 2. 打开即载入：出现两段，且不写旧标签 */
ok(ta.value.indexOf("【段1】") >= 0, "载入后缺【段1】：" + JSON.stringify(ta.value));
ok(ta.value.indexOf("【段2】") >= 0, "载入后缺【段2】");
ok(ta.value.indexOf("意图：") < 0, "不应再写「意图：」标签");
ok(ta.value.indexOf("剧本：") < 0, "不应再写「剧本：」标签");
ok(ta.value.indexOf("提示词：") >= 0, "正文应带「提示词：」标签（导出口径）");
ok(ta.value.indexOf("时长：9") >= 0, "段时长应随载入带上：\n" + ta.value);

/* 3. 按钮：只留分段优化 + 解析并分配 */
const labels = [...dialog.querySelectorAll("button")].map((b) => String(b.textContent || "").trim());
ok(labels.includes("✨ AI分段提示词优化"), "缺「✨ AI分段提示词优化」：" + JSON.stringify(labels));
ok(labels.includes("解析并分配"), "缺「解析并分配」");
ok(labels.includes("从当前链载入"), "缺「从当前链载入」");
ok(labels.includes("⚙ AI 优化设置"), "缺「⚙ AI 优化设置」");
ok(!labels.includes("✨ AI扩写 → 剧本"), "「✨ AI扩写 → 剧本」应已撤下");
ok(!labels.some((t) => t.indexOf("AI扩写 → 剧本") >= 0), "多段扩写入口应已撤下");

/* 4. 顶部参数：模式 + 缺省时长；段数/风格/时长范围已撤下 */
const selects = [...dialog.querySelectorAll(".h3d-mpset select")].map((s) => s.value);
ok(selects.includes("T2VA"), "缺模式下拉：" + JSON.stringify(selects));
ok(selects.length === 1, `顶部下拉应只剩「模式」1 个，实际 ${selects.length} 个：${JSON.stringify(selects)}`);
const nums = [...dialog.querySelectorAll('.h3d-mpset input[type="number"]')];
ok(nums.length === 1, `顶部数字输入应只剩「缺省时长」1 个，实际 ${nums.length} 个`);

/* 5. 段数是从文本解析出来的（不是靠输入框指定） */
const info = String((dialog.querySelector(".h3d-mpinfo") || {}).textContent || "");
ok(info.indexOf("2") >= 0, "识别段数应来自文本解析：" + JSON.stringify(info));

const real = (errors || []).filter((e) => !/Not implemented|jsdom/i.test(String(e)));
ok(real.length === 0, "运行时报错：" + JSON.stringify(real));

if (fails.length) {
    console.error("FAIL:\n - " + fails.join("\n - "));
    process.exit(1);
}
console.log("OK: 总提示词工作台单框冒烟通过");
