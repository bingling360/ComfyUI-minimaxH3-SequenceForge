/* 导演台节点外观守卫（2026-09-27）：彩虹配色 + 5 个端口免打扰。
 *
 * 守四件事：
 *   ① 彩虹只走绘制钩子（onDrawTitleBar / onDrawBackground）—— 不许改 node.color/bgcolor
 *      那种纯色走法（纯色套不了渐变，改了等于把效果降级还不自知）；
 *   ② 免打扰名单就是那 5 个，一个不多一个不少；三个内部方法（_measureSlots / drawSlots /
 *      computeSize）都得包上，少包一个就会出现「端口看不见但还占着行高」；
 *   ③ 必须按**名字**认端口 —— 按位次认的话，控件一增删就错位；
 *   ④ 反向：后端 schema 与两个工作流 JSON 里的端口**一个都不许删**。删「帧率」会把
 *      「报告」从第 3 位挤到第 2 位（输出位次就是连线的坐标），老工作流的报告连线会
 *      静默接到帧率上。这条只有靠守卫钉着才不会被后人「顺手清理」掉。
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
for (const h of ["node.getInputPos =", "node.getOutputPos =", "node.drawSlots =", "node.computeSize ="]) {
    ok(director.includes(h), `端口免打扰钩子缺失：${h}`);
}
/* 反向钉一次踩过的坑：坐标必须只在 getInputPos/getOutputPos 一处换下标。
 * 包 _measureSlots 会让「量标签」与「画连线」各用一套下标 → 端口的接线点画到别的行去。 */
ok(!director.includes("node._measureSlots ="),
   "不许包 _measureSlots —— 标签与连线会各算一套坐标（二采模型错位就是这么来的）");
ok(director.includes("paintDeskNode(node);"), "mountDeskButton 未调用 paintDeskNode");
ok(director.includes("if (node.__h3DeskPainted) return;"), "paintDeskNode 必须幂等（重复挂会层层套娃）");

/* ---------- ③ 按名字认，不按位次认 ---------- */
ok(director.includes("H3_DESK_HIDE_SLOTS.has(s.name)"),
   "端口必须按 name 命中，不许按下标/位次认");
ok(director.includes("H3_DESK_HIDE_SLOTS.has(arr[k].name)"),
   "换下标那处也必须按 name 认");
ok(!/H3_DESK_HIDE_SLOTS\.has\((?!s\.name|arr\[k\]\.name)/.test(director),
   "H3_DESK_HIDE_SLOTS 的命中键只能是槽位名");

/* ---------- ④ 反向：后端与工作流的端口一个都不许删 ---------- */
for (const out of ['io.Int.Output("帧率")', 'io.String.Output("报告")']) {
    ok(nodesPy.includes(out), `nodes.py 误删输出：${out}`);
}
for (const inp of ['io.Image.Input("起始视频"', 'io.Audio.Input("起始视频音轨"',
                   'io.Model.Input("二采模型"', 'io.Sigmas.Input("自定义Sigmas"']) {
    ok(nodesPy.includes(inp), `nodes.py 误删输入：${inp}`);
}
for (const nm of ["起始视频", "起始视频音轨", "帧率"]) {
    ok(templateWf.includes('"name": "' + nm + '"'),
       `默认工作流误删端口条目：${nm}（藏而不删，删了会串位）`);
}

console.log(fails.length ? `\n${fails.length} 个断言失败：\n  ` + fails.join("\n  ")
                         : "\ndesk_node_skin_check 全部通过");
process.exit(fails.length ? 1 : 0);
