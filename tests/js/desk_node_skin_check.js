/* 导演台节点外观守卫（2026-09-27）：彩虹配色 + 5 个端口免打扰。
 *
 * 守四件事：
 *   ① 彩虹只走绘制钩子（onDrawTitleBar / onDrawBackground）—— 不许改 node.color/bgcolor
 *      那种纯色走法（纯色套不了渐变，改了等于把效果降级还不自知）；
 *   ② 免打扰名单就是那 5 个，一个不多一个不少；三个内部方法（_measureSlots / drawSlots /
 *      computeSize）都得包上，少包一个就会出现「端口看不见但还占着行高」；
 *   ③ 必须按**名字**认端口 —— 按位次认的话，控件一增删就错位；
 *   ④ 反向：后端 schema 的端口**一个都不许删**（帧率/起始视频等停机场槽是 schema 真身，
 *      模板上藏而不删）。默认工作流模板自 rev 3.1 起按 1.53.6 序列化口径走（停机场槽
 *      不落盘、3 输出制），「报告」必须是最后一个输出条目且带连线——位次就是连线坐标，
 *      位次错了载入即断线。
 *
 * 纯读文件断言，不需要 jsdom。跑法：NODE_PATH=<root>/node_modules node tests/js/desk_node_skin_check.js
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

const director = read("web/h3_director.js");
const nodesPy = read("nodes.py");
const templateWf = read("web/h3_default_workflow.js");

const fails = [];
function ok(cond, msg) { if (!cond) fails.push(msg); }

/* ---------- ① 彩虹只走绘制钩子 ---------- */
ok(director.includes("node.onDrawTitleBar ="), "标题栏渐变钩子 onDrawTitleBar 缺失");
ok(director.includes("node.onDrawBackground ="), "机身描边钩子 onDrawBackground 缺失");
ok(director.includes("ctx.createLinearGradient"), "渐变应由 createLinearGradient 现铺");
ok(director.includes('nodeType.title_text_color = "#fff"'),
   "彩虹标题栏上的标题字必须转白（title_text_color）");
ok(!/node\.color\s*=/.test(director) && !/node\.bgcolor\s*=/.test(director),
   "不许改 node.color/bgcolor —— 那是纯色，套不了渐变");

const grad = director.match(/const H3_DESK_GRADIENT = \[([^\]]*)\]/);
ok(!!grad, "H3_DESK_GRADIENT 取消色表缺失");
if (grad) {
    const stops = grad[1].split(",").map((s) => s.trim()).filter(Boolean);
    ok(stops.length === 4, `Gemini 四停渐变异动：${stops.length} 停`);
    for (const s of stops) ok(/^"#[0-9A-Fa-f]{6}"$/.test(s), `色停格式不对：${s}`);
}

/* ---------- ② 免打扰名单与三个钩子 ---------- */
const hide = director.match(/const H3_DESK_HIDE_SLOTS = new Set\(\[([^\]]*)\]\)/);
ok(!!hide, "H3_DESK_HIDE_SLOTS 名单缺失");
if (hide) {
    const got = hide[1].split(",").map((s) => s.trim().replace(/^"|"$/g, "")).filter(Boolean).sort();
    const want = ["起始视频", "起始视频音轨", "帧率", "分段图像", "分段音频"].sort();
    ok(JSON.stringify(got) === JSON.stringify(want), `免打扰名单应为 5 个端口，实际：${got.join("/")}`);
}
for (const h of ["node.getInputPos =", "node.getOutputPos =", "node.getInputSlotPos =",
                 "node.drawSlots =", "node.computeSize ="]) {
    ok(director.includes(h), `端口免打扰钩子缺失：${h}`);
}
/* 坐标入口三条都得包上：按下标的两个 + 按槽对象的 getInputSlotPos（拖线找落点走它，
 * 漏掉它就会出现「落点预览与标签不在同一行」）。 */
ok(director.includes("H3_DESK_PARK_X"), "隐藏端口必须丢到停机场，不能和可见端口挤同一行");
ok(director.includes("deskSlotVisible"), "缺少「露头」统一判定");
ok(director.includes("slot.link != null") && director.includes("slot.links.length > 0"),
   "「露头」判定要与前端 isConnected 同口径（输入看 link、输出看 links）");
/* 反向钉两次踩过的坑：① 坐标必须收敛，不许包 _measureSlots；② 不许往槽对象写 pos
 * （会被存档带走，旧版本插件读到就变成永远点不到的端口）。 */
ok(!director.includes("node._measureSlots ="),
   "不许包 _measureSlots —— 标签与连线会各算一套坐标");
ok(!/\.\s*pos\s*=\s*\[\s*node\.pos/.test(director),
   "不许给槽对象写 pos 当停车位 —— 那个字段跟着存档走，会毒到旧版本插件");
ok(director.includes("paintDeskNode(node);"), "mountDeskButton 未调用 paintDeskNode");
ok(director.includes("if (node.__h3DeskPainted) return;"), "paintDeskNode 必须幂等（重复挂会层层套娃）");

/* ---------- ③ 按名字认，不按位次认 ---------- */
ok(director.includes("H3_DESK_HIDE_SLOTS.has(slot.name)"),
   "端口必须按 name 命中，不许按下标/位次认");
ok(!/H3_DESK_HIDE_SLOTS\.has\((?!slot\.name)/.test(director),
   "H3_DESK_HIDE_SLOTS 的命中键只能是槽位名");

/* ---------- ④ 反向：后端端口一个都不许删；模板按 1.53.6 序列化口径 ---------- */
for (const out of ['io.Int.Output("帧率")', 'io.String.Output("报告")']) {
    ok(nodesPy.includes(out), `nodes.py 误删输出：${out}`);
}
for (const inp of ['io.Image.Input("起始视频"', 'io.Audio.Input("起始视频音轨"',
                   'io.Model.Input("二采模型"', 'io.Sigmas.Input("自定义Sigmas"']) {
    ok(nodesPy.includes(inp), `nodes.py 误删输入：${inp}`);
}
/* 模板输出 = schema 4 槽制（1.53.6 实测：运行时输出槽永远按 schema 建，序列化里的
 * outputs 数组对布局无效）；「报告」必须是最后一个输出条目且带着连线，连线表
 * origin_slot 按 schema 位次（3）解析——位次错了报告线会落到「帧率」上（rev3.1 真断过）。
 * 输入侧相反：运行时输入由序列化数组重建，停机场槽（起始视频/起始视频音轨）不进模板。 */
let tplWf = null;
try {
    const m = templateWf.match(/window\.H3_DEFAULT_WORKFLOW\s*=\s*(\{[\s\S]*\})\s*;?\s*$/);
    if (m) tplWf = JSON.parse(m[1]);
} catch (e) { /* 解析失败由下面的 ok 兜底报红 */ }
ok(!!tplWf, "默认工作流 JSON 解析失败");
if (tplWf) {
    const s = (tplWf.nodes || []).find((n) => n.type === "H3SeamlessChainSampler");
    ok(!!s, "默认工作流缺主节点");
    if (s) {
        const outs = (s.outputs || []).map((o) => o.name);
        ok(JSON.stringify(outs) === JSON.stringify(["图像", "音频", "帧率", "报告"]),
           `模板输出应为 schema 4 槽制，实际：${outs.join("/")}`);
        const rep = (s.outputs || []).find((o) => o.name === "报告");
        ok(!!rep && Array.isArray(rep.links) && rep.links.length > 0,
           "模板「报告」输出必须带连线（否则载入即断报告线）");
        ok(outs.indexOf("报告") === outs.length - 1, "「报告」必须是最后一个输出条目（位次=连线坐标）");
        const ins = (s.inputs || []).map((i) => i.name);
        for (const gone of ["起始视频", "起始视频音轨"]) {
            ok(!ins.includes(gone), `模板不该带停机场槽「${gone}」（输入侧由序列化数组重建）`);
        }
    }
}

console.log(fails.length ? `\n${fails.length} 个断言失败：\n  ` + fails.join("\n  ")
                         : "\ndesk_node_skin_check 全部通过");
process.exit(fails.length ? 1 : 0);
