/* 端口坐标几何守卫（jsdom 真跑 shipped 的 paintDeskNode）。
 *
 * 为什么要有它：desk_node_skin_check 只能证明「那些钩子写在源码里」，证明不了坐标算对。
 * 而节点端口的手感全在**坐标**上——两次踩坑都不是代码缺失，是坐标算错：
 *   ① 过滤 _measureSlots → 标签按紧凑行画、连线按原始行画，「二采模型」的线画到了下面；
 *   ② 隐藏槽与可见槽挤同一行 → 前端命中测试「按数组顺序第一个命中的赢」，
 *      可见端口反而被看不见的暗桩抢走（按住二采模型拖线，拉出来的是起始视频）。
 *
 * 夹具里的坐标公式抄自 comfyui_frontend 1.52.7（settingStore-*.js 的
 * calculateInputSlotPosFromSlot / calculateOutputSlotPos / getDefaultVerticalInputs /
 * getNodeInputOnPos），是用来说明「前端按什么规则算坐标」的，不是复刻实现。
 * 被测的是**真源码**：paintDeskNode 直接从 web/h3_director.js 里取。
 *
 * 跑法：NODE_PATH=<repo>/node_modules node tests/js/desk_slot_geometry_check.js
 */
const path = require("path");

let harness;
try {
    harness = require("./_harness.js");
} catch (e) {
    console.error("SKIP: 未安装 jsdom（npm install）");
    process.exit(2);
}

const SLOT_H = 20;      // 前端 NODE_SLOT_HEIGHT
const SLOT_PAD = 10;    // 命中框以槽位坐标为心，半径 10

const fails = [];
function ok(cond, msg) { if (!cond) fails.push(msg); }

/* ---------- 夹具：H3 主节点真实的端口清单（顺序 = nodes.py schema 里非控件输入的先后） ---------- */
const INPUT_NAMES = ["模型", "文本编码器", "视频VAE", "音频VAE",
                     "起始视频", "起始视频音轨", "二采模型", "自定义Sigmas"];
// rev3.4（2026-10-04）：schema 把停机场槽「帧率」排到「报告」之后 → 「报告」钉在第 2 位
const OUTPUT_NAMES = ["图像", "音频", "报告", "帧率", "分段图像", "分段音频"];
// 用户当前的工作流：一采/二采模型都在用，「报告」有下游；被藏的 5 个都没接线
const CONNECTED_INPUTS = new Set(["模型", "文本编码器", "视频VAE", "音频VAE", "二采模型"]);
const CONNECTED_OUTPUTS = new Set(["图像", "报告"]);

function mkNode() {
    let linkId = 10;
    return {
        pos: [400, 240], size: [828, 150], slotStartY: 0, widgets: [{}],
        drawSlots() {}, _measureSlots() {},
        computeSize() { this.__seen = [this.inputs.length, this.outputs.length]; return [828, 150]; },
        inputs: INPUT_NAMES.map((name) => ({
            name, type: "X", link: CONNECTED_INPUTS.has(name) ? (linkId += 1) : null,
        })),
        outputs: OUTPUT_NAMES.map((name) => ({
            name, type: "X", links: CONNECTED_OUTPUTS.has(name) ? [(linkId += 1)] : [],
        })),
    };
}

/* ---------- 前端坐标规则（1.52.7） ---------- */
const isWidgetInputSlot = (s) => !!s.widget;
const defaultVerticalInputs = (n) => n.inputs.filter((s) => !s.pos && !(n.widgets?.length && isWidgetInputSlot(s)));
const defaultVerticalOutputs = (n) => n.outputs.filter((s) => !s.pos);

function calcInputPos(n, i) {
    const slot = n.inputs[i];
    if (!slot) return [n.pos[0], n.pos[1]];
    if (slot.pos) return [n.pos[0] + slot.pos[0], n.pos[1] + slot.pos[1]];
    const idx = defaultVerticalInputs(n).indexOf(slot);
    return [n.pos[0] + SLOT_H * 0.5, n.pos[1] + (idx + 0.7) * SLOT_H + (n.slotStartY || 0)];
}
function calcOutputPos(n, i) {
    const slot = n.outputs[i];
    if (!slot) return [n.pos[0] + n.size[0], n.pos[1]];
    if (slot.pos) return [n.pos[0] + slot.pos[0], n.pos[1] + slot.pos[1]];
    const idx = defaultVerticalOutputs(n).indexOf(slot);
    return [n.pos[0] + n.size[0] + 1 - SLOT_H * 0.5, n.pos[1] + (idx + 0.7) * SLOT_H + (n.slotStartY || 0)];
}
const isInRectangle = (x, y, rx, ry, w, h) => x >= rx && x < rx + w && y >= ry && y < ry + h;

/* 前端 getNodeInputOnPos / getNodeOutputOnPos：**按数组顺序返回第一个命中的**。
 * 这就是「暗桩抢走点击」的机制 —— 谁先在下标序里命中，谁赢。 */
function inputOnPos(n, x, y) {
    for (let i = 0; i < n.inputs.length; i += 1) {
        const a = n.inputs[i], r = n.getInputPos(i);
        const w = 20 + ((a.label?.length ?? a.localized_name?.length ?? a.name?.length) || 3) * 7;
        if (isInRectangle(x, y, r[0] - SLOT_PAD, r[1] - SLOT_PAD, w, SLOT_H)) return i;
    }
    return -1;
}
function outputOnPos(n, x, y) {
    for (let i = 0; i < n.outputs.length; i += 1) {
        const r = n.getOutputPos(i);
        if (isInRectangle(x, y, r[0] - SLOT_PAD, r[1] - SLOT_PAD, 40, SLOT_H)) return i;
    }
    return -1;
}

/* ---------- 装上真源码的钩子 ---------- */
const { w } = harness.load({});
const paintDeskNode = w.eval("paintDeskNode");
ok(typeof paintDeskNode === "function", "从 web/h3_director.js 取不到 paintDeskNode");

const node = mkNode();
const origInputPos = (i) => calcInputPos(node, i);
const origOutputPos = (i) => calcOutputPos(node, i);
node.getInputPos = origInputPos;
node.getOutputPos = origOutputPos;
node.getInputSlotPos = (slot) => origInputPos(node.inputs.indexOf(slot));
paintDeskNode(node);

const rowOf = (i) => Math.round(((node.getInputPos(i)[1] - node.pos[1]) / SLOT_H - 0.7) * 10) / 10;
const orowOf = (i) => Math.round(((node.getOutputPos(i)[1] - node.pos[1]) / SLOT_H - 0.7) * 10) / 10;
const byName = (arr, name) => arr.findIndex((s) => s.name === name);

/* ---------- ① 露头的端口各占一行，行号从 0 连续排 ---------- */
const shownInputs = ["模型", "文本编码器", "视频VAE", "音频VAE", "二采模型", "自定义Sigmas"];
shownInputs.forEach((name, row) => {
    const i = byName(node.inputs, name);
    ok(rowOf(i) === row, `「${name}」应在第 ${row} 行，实际第 ${rowOf(i)} 行`);
});
["图像", "音频", "报告"].forEach((name, row) => {
    const i = byName(node.outputs, name);
    ok(orowOf(i) === row, `输出「${name}」应在第 ${row} 行，实际第 ${orowOf(i)} 行`);
});

/* ---------- ② 被藏的端口丢到停机场：点不到、不占行 ---------- */
for (const name of ["起始视频", "起始视频音轨"]) {
    const i = byName(node.inputs, name);
    const p = node.getInputPos(i);
    ok(Math.abs(p[0] - node.pos[0]) > 10000, `「${name}」没进停机场（x=${p[0]}）`);
    ok(Math.abs(p[1] - node.pos[1]) <= SLOT_H, `「${name}」的停车位不该偏离节点高度，否则会撑高机身`);
}
for (const name of ["帧率", "分段图像", "分段音频"]) {
    const i = byName(node.outputs, name);
    ok(Math.abs(node.getOutputPos(i)[0] - node.pos[0]) > 10000, `输出「${name}」没进停机场`);
}

/* ---------- ③ 命中测试：可见端口自己那一行必须命中自己（这才是「手感」） ---------- */
shownInputs.forEach((name) => {
    const i = byName(node.inputs, name);
    const [x, y] = node.getInputPos(i);
    ok(inputOnPos(node, x, y) === i, `点「${name}」的圆点，命中的却是 ${node.inputs[inputOnPos(node, x, y)]?.name}`);
    ok(inputOnPos(node, x + 10, y) === i, `点「${name}」标签正中，命中的却是 ${node.inputs[inputOnPos(node, x + 10, y)]?.name}`);
});
["图像", "音频", "报告"].forEach((name) => {
    const i = byName(node.outputs, name);
    const [x, y] = node.getOutputPos(i);
    ok(outputOnPos(node, x, y) === i, `点输出「${name}」的圆点，命中的却是 ${node.outputs[outputOnPos(node, x, y)]?.name}`);
});

/* ---------- ④ 落点预览（getInputSlotPos）也要落在同一行 ---------- */
shownInputs.forEach((name) => {
    const i = byName(node.inputs, name);
    ok(node.getInputSlotPos(node.inputs[i])[1] === node.getInputPos(i)[1],
       `「${name}」的拖线落点与标签不在同一行`);
});

/* ---------- ⑤ 机身高度：按露头的端口算，停机场不许把机身撑开 ---------- */
const bottoms = [];
for (let i = 0; i < node.inputs.length; i += 1) bottoms.push(node.getInputPos(i)[1] + SLOT_H);
for (let i = 0; i < node.outputs.length; i += 1) bottoms.push(node.getOutputPos(i)[1] + SLOT_H);
const bodyBottom = Math.max(...bottoms) - node.pos[1];       // arrange() 用它定位按钮
const shownBottom = node.getInputPos(byName(node.inputs, "自定义Sigmas"))[1] + SLOT_H - node.pos[1];
// 第 5 行（0-based）的底边 = (5 + 0.7) * 20 + 20 = 134
ok(bodyBottom === (5 + 0.7) * SLOT_H + SLOT_H,
   `按钮位置应贴着最后一个露头端口（${(5 + 0.7) * SLOT_H + SLOT_H}），实际 ${bodyBottom}`);
ok(shownBottom === bodyBottom, "机身底边应只由露头端口决定（停机场不能把它撑开）");
node.computeSize();
ok(String(node.__seen) === "6,3", `computeSize 应只看到露头的端口，实际 ${node.__seen}`);
ok(node.inputs.length === INPUT_NAMES.length && node.outputs.length === OUTPUT_NAMES.length,
   "computeSize 之后必须把完整端口表还原（否则存档会缺端口）");

/* ---------- ⑥ 接了线的端口必须露头（否则线会悬空） ---------- */
const up = byName(node.inputs, "二采模型");
ok(CONNECTED_INPUTS.has("二采模型") && rowOf(up) === 4, "接了线的「二采模型」必须照常露头");
const rep = byName(node.outputs, "报告");
ok(CONNECTED_OUTPUTS.has("报告") && orowOf(rep) === 2, "接了线的「报告」必须照常露头");

/* 反向：把「起始视频」接上线，它就该回到第 4 行并让后面整体下移 */
const node2 = mkNode();
const o2in = (i) => calcInputPos(node2, i), o2out = (i) => calcOutputPos(node2, i);
node2.getInputPos = o2in;
node2.getOutputPos = o2out;
node2.getInputSlotPos = (slot) => o2in(node2.inputs.indexOf(slot));
node2.inputs[byName(node2.inputs, "起始视频")].link = 77;
paintDeskNode(node2);
const r2 = Math.round(((node2.getInputPos(byName(node2.inputs, "起始视频"))[1] - node2.pos[1]) / SLOT_H - 0.7) * 10) / 10;
const r2up = Math.round(((node2.getInputPos(byName(node2.inputs, "二采模型"))[1] - node2.pos[1]) / SLOT_H - 0.7) * 10) / 10;
ok(r2 === 4 && r2up === 5, `接上线的端口应占回第 4 行并把后面顶下去，实际 起始视频=${r2} 二采模型=${r2up}`);

console.log(fails.length ? `\n${fails.length} 个断言失败：\n  ` + fails.join("\n  ")
                         : "\ndesk_slot_geometry_check 全部通过");
process.exit(fails.length ? 1 : 0);
