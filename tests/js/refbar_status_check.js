/* 引用条收敛真跑检查（node + jsdom，跑 shipped buildRefBar）。
 *
 * 跑法：node tests/js/refbar_status_check.js
 *
 * 覆盖 B01 三件事：
 *  ① 状态监控：**有引用整条转绿（.h3d-refrow.on），无引用为灰**
 *  ② 引用计数彻底删除（不再有"已引用 N 个素材（共 M 次 · 图x/9…）"文字；
 *     只有超官方 9/3/3 上限时才出红字）
 *  ③ 编译映射条：逐行给出「标注 · 素材名 → 官方 token」，且**标注号 ≠ token 号**
 *     时也正确（图片3 单引用 → <Picture 1>，这是两层编号的核心不变量）
 */
const { load, mkNode } = require("./_harness.js");

const { w, errors } = load();
if (errors.length) {
    console.log("FAIL: 装载期抛错 →", errors.join(" | "));
    process.exit(1);
}
if (typeof w.buildRefBar !== "function") {
    console.log("FAIL: 缺少 shipped 函数 buildRefBar");
    process.exit(1);
}

let fails = 0;
const ok = (cond, msg) => { if (!cond) { console.log("FAIL " + msg); fails++; } else console.log("OK   " + msg); };
const eq = (got, want, msg) => ok(JSON.stringify(got) === JSON.stringify(want),
    `${msg}（got ${JSON.stringify(got)} want ${JSON.stringify(want)}）`);

const pool = [
    { label: "阿依", kind: "image", file: "images/a.png", asset_id: "a_111111111111", mark: "图片1", roles: [] },
    { label: "回廊", kind: "image", file: "images/b.png", asset_id: "a_222222222222", mark: "图片3", roles: [] },
    { label: "片头", kind: "video", file: "videos/c.mp4", asset_id: "a_333333333333", mark: "视频1", roles: [] },
];
for (let i = 0; i < 9; i++) {
    pool.push({ label: `群图${i}`, kind: "image", file: `images/g${i}.png`,
                asset_id: `a_g${i}0000000000`.slice(0, 14), mark: `图片${i + 10}`, roles: [] });
}
const node = mkNode({ prompts: [""], segments: [{}], ref_assets: pool });

function mkBar(refs) {
    const RB = {
        node, segIdx: 0, editor: null, gate: true, title: "引用素材", tplKey: "seg0",
        pool,
        livePool: () => pool,
        readRefs: () => refs,
        commit: () => {},
    };
    return w.buildRefBar(RB);
}

/* ① 无引用 = 灰 */
let bar = mkBar([]);
bar.paint();
ok(!bar.el.classList.contains("on"), "无引用时整条不转绿（灰）");
ok(bar.el.textContent.indexOf("已引用") < 0 && bar.el.textContent.indexOf("已标注") < 0,
    "无引用时不出现引用计数文案");

/* ② 有引用 = 绿，且仍不出现计数 */
bar = mkBar(["阿依"]);
bar.paint();
ok(bar.el.classList.contains("on"), "有引用时整条转绿");
ok(bar.el.textContent.indexOf("已引用") < 0 && bar.el.textContent.indexOf("已标注") < 0,
    "有引用时也不出现引用计数文案");
ok(bar.el.querySelectorAll(".h3d-chipbtn.on").length === 1, "只有被引用的 chip 点亮");

/* ③ 编译映射：标注号 图片3 → token 号 <Picture 1>（两层编号必须分离） */
bar = mkBar(["回廊"]);
bar.paint();
const rows = [...bar.el.querySelectorAll(".h3d-refmap-row")];
eq(rows.length, 1, "映射条只有 1 行");
const cells = rows.length ? [...rows[0].children].map((n) => n.textContent) : [];
eq(cells.slice(0, 3), ["图片3", "回廊", "<Picture 1>"],
    "映射行 = 标注 · 素材名 → 官方 token（标注3 编译成 Picture 1）");
ok(!!rows[0] && rows[0].querySelector("button"), "映射行带「改」按钮（改标注入口）");

/* 三类混排：图片/视频各自独立编号 */
bar = mkBar(["阿依", "片头", "回廊"]);
bar.paint();
const t3 = [...bar.el.querySelectorAll(".h3d-refmap-row")].map(
    (r) => [...r.children].slice(0, 3).map((n) => n.textContent));
eq(t3, [["图片1", "阿依", "<Picture 1>"], ["视频1", "片头", "<Video 1>"],
        ["图片3", "回廊", "<Picture 2>"]], "按类型独立编号 + 保序");

/* ④ 超官方上限（图 9/3/3）：唯一还允许出现文字的地方 */
const ten = ["阿依", "回廊", ...Array.from({ length: 8 }, (_x, i) => `群图${i}`)];
bar = mkBar(ten);
bar.paint();
const hint = bar.el.querySelector(".h3d-secs-hint");
ok(!!hint && hint.textContent.indexOf("超官方单段上限") >= 0,
    "超过 9 张图时给出上限警告：" + (hint ? hint.textContent : "(无)"));
ok(!!hint && hint.classList.contains("h3d-refhint-bad"), "上限警告用红色样式类");

process.exit(fails ? 1 : 0);
