/* 锚定面板 / 素材库挑选模式的**真 DOM 冒烟**（jsdom 跑 tools/anchor_preview.html）。
 *
 * 为什么需要它：`node --check` 只验语法，跨模块按全局名调用、数据陈旧、
 * 重建把局部状态冲掉——这几类故障语法全合法，只在运行时炸，而且往往只在
 * 「点一下」之后才现形。2026-09-16 的 `getDirValue 未加载` 与 `pickErr 被重建冲掉`
 * 都是这样漏出去的。这个冒烟用真 DOM + 真点击把它们挡在交付之前。
 *
 * 前置：先跑 `python tools/make_anchor_preview.py` 生成预览页；
 *       依赖仓库 node_modules 里的 jsdom（ComfyUI 前端工具链自带的那个）。
 * 跑法：node tools/anchor_ui_smoke.cjs     退出码 0 = 全绿
 */
const path = require("path");
const fs = require("fs");

const ROOT = path.dirname(__dirname);
const PAGE = path.join(ROOT, "tools", "anchor_preview.html");
const JSDOM_DIR = path.join(ROOT, "node_modules", "jsdom");

if (!fs.existsSync(PAGE)) {
  console.log("缺 tools/anchor_preview.html —— 先跑：python tools/make_anchor_preview.py");
  process.exit(2);
}
if (!fs.existsSync(JSDOM_DIR)) {
  console.log("node_modules/jsdom 不在（本机没装前端工具链）——这条冒烟只能在装了的机器上跑");
  process.exit(2);
}
const { JSDOM, VirtualConsole } = require(JSDOM_DIR);

const ERRORS = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => ERRORS.push("jsdomError: " + ((e && e.message) || e)));
vc.on("error", (...a) => ERRORS.push("console.error: " + a.map(String).join(" ")));
vc.on("warn", (...a) => ERRORS.push("console.warn: " + a.map(String).join(" ")));

const tick = (ms = 40) => new Promise((r) => setTimeout(r, ms));
const card = (doc, i) => doc.querySelectorAll(".h3d-anchor-card")[i];
const btn = (root, re) => [...root.querySelectorAll("button")].find((b) => re.test(b.textContent));
let FAIL = 0;
function ok(cond, label, extra) {
  if (!cond) FAIL++;
  console.log((cond ? "  OK " : "  NG ") + String(label).padEnd(48)
    + (extra === undefined ? "" : " " + extra));
}

(async () => {
  const dom = await JSDOM.fromFile(PAGE, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      // jsdom 不实现 IntersectionObserver（h3_library 懒加载缩略图要用）
      window.IntersectionObserver = class {
        observe() {} unobserve() {} disconnect() {}
      };
    },
  });
  const { window } = dom;
  const doc = window.document;
  await tick(120);

  console.log("== 1 面板渲染 ==");
  ok(doc.querySelectorAll(".h3d-anchor-card").length === 3, "三张锚卡（段源/latent/旧上段尾）",
    doc.querySelectorAll(".h3d-anchor-card").length);
  ok(doc.querySelectorAll(".h3d-anchor-three").length === 3, "每张卡一个三段容器");
  ok(card(doc, 0).querySelectorAll(".h3d-track").length === 3, "卡里三个 track（源轨/目标轨/体检）");
  ok(card(doc, 0).querySelectorAll(".h3d-strip").length === 2, "两条时间线（源轨 + 目标轨）");
  const t0 = card(doc, 0).textContent;
  ok(t0.includes("分辨率 1280×720 与本链一致"), "段源分辨率比对生效");
  ok(t0.includes("源共 124 帧"), "源长度从源清单取到");
  ok(t0.includes("本段落点未越界"), "本段落点越界体检在");
  ok(card(doc, 2).textContent.includes("单帧锚"), "window=1 的尾锚不再被误判非法");
  ok(t0.includes("① 源轨") && t0.includes("② 目标轨") && t0.includes("③ 源选择"),
    "上下堆叠三块都在（① → ② → ③）");

  console.log("\n== 2 档位收起 / 展开 ==");
  const rowA = card(doc, 0).querySelectorAll(".h3d-winbtns")[0];
  const rowB = card(doc, 0).querySelectorAll(".h3d-winbtns")[1];
  ok(rowA.querySelectorAll("button").length === 6, "常显 1/5/22/39/56 + 更多 = 6 个",
    rowA.querySelectorAll("button").length);
  ok(rowB.style.display === "none", "更多档位默认收起");
  ok(rowB.querySelectorAll("button").length === 18, "收起 18 个（23 档位 - 5 常见）",
    rowB.querySelectorAll("button").length);
  [...rowA.querySelectorAll("button")].slice(-1)[0].click();
  ok(rowB.style.display !== "none", "点更多展开");
  [...rowB.querySelectorAll("button")].find((b) => b.textContent === "73").click();
  await tick();
  ok(card(doc, 0).querySelector(".h3d-selbox").textContent === "73帧", "点档位后选取框跟着变");

  console.log("\n== 3 素材库挑选模式 ==");
  btn(card(doc, 1), /选择素材|换素材/).click();
  await tick(150);
  const ov = doc.querySelector(".h3l-overlay");
  ok(!!ov, "素材库以挑选模式打开");
  ok(!!ov && ov.classList.contains("h3l-picking"), "带 h3l-picking 标记（单击即选）");
  ok(!!ov && !!ov.querySelector(".h3l-hint"), "头部有「点一下素材就选中」提示");
  ok(!(ov && btn(ov, /多选/)), "挑选模式隐藏多选");
  const tiles = ov ? [...ov.querySelectorAll(".h3l-tile")] : [];
  const names = tiles.map((t) => t.querySelector(".h3l-name").textContent).join(",");
  ok(tiles.length === 2, "默认落在项目资产 scope：2 条", tiles.length);
  ok(names === "角色1,参考片段", "条目名正确", names);
  if (tiles.length) {
    tiles[0].click();
    await tick(180);
    ok(!doc.querySelector(".h3l-overlay"), "选完自动关闭");
    const c1 = card(doc, 1).textContent;
    ok(c1.includes("角色1"), "回填条目名");
    ok(c1.includes("源 1024×1024"), "素材尺寸由后端确认后进体检");
    ok(!c1.includes("寻址不到"), "清单里有别名的素材不报寻址警告");
  }

  console.log("\n== 4 不可寻址素材要提前警告 ==");
  btn(card(doc, 1), /选择素材|换素材/).click();
  await tick(150);
  const ov2 = doc.querySelector(".h3l-overlay");
  const t2 = ov2 && [...ov2.querySelectorAll(".h3l-tile")]
    .find((t) => t.querySelector(".h3l-name").textContent === "参考片段");
  ok(!!t2, "找到参考片段瓦片");
  if (t2) {
    t2.click();
    await tick(180);
    const c = card(doc, 1).textContent;
    ok(c.includes("寻址不到"), "面板提前给出寻址警告（不是等生成才报未知标签）");
    ok(c.includes("1920×1080"), "视频尺寸/帧数由后端探测得到");
  }

  console.log("\n== 5 新增锚定当场重建 ==");
  btn(doc.querySelector(".h3d-anchor-add"), /新增锚定/).click();
  await tick(80);
  ok(doc.querySelectorAll(".h3d-anchor-card").length === 4, "当场多一张卡（不用退出重进）",
    doc.querySelectorAll(".h3d-anchor-card").length);
  ok(card(doc, 3).textContent.includes("段 1"), "新卡预填上一段（段 1）");

  console.log("\n== 6 切来源 段 -> 素材 ==");
  const sel = card(doc, 0).querySelectorAll("select")[0];
  const opts = [...sel.querySelectorAll("option")].map((o) => o.textContent).join("/");
  ok(opts === "段/素材", "来源只有两类", opts);
  sel.value = "asset";
  sel.dispatchEvent(new window.Event("change", { bubbles: true }));
  await tick(80);
  ok(card(doc, 0).textContent.includes("（未选素材）"), "切到素材后 ref 清空并提示未选");

  console.log("\n== 7 旧格式 prev_tail 回显 ==");
  const c2 = card(doc, 2);
  ok(c2.textContent.includes("上段尾（旧格式）"), "旧锚作为只读项显示");
  ok(c2.textContent.includes("等价于选上一段"), "给出替换说明");
  ok(c2.textContent.includes("帧窗 1 合法"), "单帧尾锚校验正确");

  console.log("\n== 8 删除 ==");
  btn(card(doc, 3), /删除/).click();
  await tick(60);
  ok(doc.querySelectorAll(".h3d-anchor-card").length === 3, "删除当场生效",
    doc.querySelectorAll(".h3d-anchor-card").length);

  console.log("\n--- errors: " + ERRORS.length + " ---");
  ERRORS.slice(0, 10).forEach((e) => console.log("  ! " + e.slice(0, 200)));
  console.log("\n" + ((FAIL === 0 && ERRORS.length === 0)
    ? "SMOKE OK" : "SMOKE FAIL: " + FAIL + " assertions / " + ERRORS.length + " errors"));
  window.close();
  process.exit(FAIL || ERRORS.length ? 1 : 0);
})().catch((e) => {
  console.log("HARNESS CRASH:", (e && e.stack) || e);
  process.exit(2);
});
