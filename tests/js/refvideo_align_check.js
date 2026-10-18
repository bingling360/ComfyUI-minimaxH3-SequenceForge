/* ⏱ 项目级「对齐动作迁移参考视频时间」—— 端到端回归（node + jsdom，跑 shipped 源码）。
 *
 * 口径（2026-10-18 用户拍板）：**官方怎么喂就按多久对齐**，按钮**只写段时长**。
 * 所以这里不测"按钮长什么样"，只测它真的把值写到了哪：
 *   ① 目标段 = 引用了**任意**视频素材的段（不是只有第一段、也不是只有 video1 引用者）
 *   ② 写下去的秒数 = 后端给的 seconds（前端不自己算帧数/秒数）
 *   ③ **一个设置都不落**（refvidSet 不许被调用）——上一版顺手改 fps_norm/grid，
 *      把 4.20s 的素材变成 4.46s，用户明确否掉
 *   ④ 确认框里写明「模型看到多长 / 按哪一档算的」；取消就一个字节都不写
 *   ⑤ 没有视频引用时明确报错，不静默什么都不做
 *
 * 用法：node tests/js/refvideo_align_check.js
 */
const assert = require("assert");
const { load, mkNode } = require("./_harness.js");

let bad = 0;
function ok(cond, msg) {
    if (cond) { console.log("OK   " + msg); return; }
    bad++;
    console.log("FAIL " + msg);
}

const POOL = [
    { label: "a.mp4", kind: "video", file: "videos/a.mp4", asset_id: "a_111111111111", ref_name: "a.mp4" },
    { label: "背景板", kind: "image", file: "img/b.png", asset_id: "a_222222222222" },
    { label: "BGM", kind: "audio", file: "audio/c.mp3", asset_id: "a_333333333333" },
];

function mkDs(extra) {
    return Object.assign({
        mode: "分段",
        /* 段1/段3 引用视频，段2 只引用图片 —— 段2 必须**不动** */
        /* seg.refs 存的是 refKeyOf（ref_name 优先，含后缀），正文里也是同一个写法 */
        prompts: ["@a.mp4 推开木门走进回廊。", "@背景板 空镜：灯笼晃动。", "@a.mp4 继续向前，抬刀。"],
        segments: [
            { seconds: 5, refs: ["a.mp4"] },
            { seconds: 7, refs: ["背景板"] },
            { seconds: 8, refs: ["a.mp4"] },
        ],
        inserts: [], redo_segs: [], ref_assets: POOL,
        first_frame: "", end_frame: "", last_frame: "",
    }, extra || {});
}

(async () => {
    /* ================= ① 写值 + 落设置 ================= */
    {
        const calls = [];
        const confirms = [];
        const node = mkNode(mkDs());
        const { w, errors } = load({
            node,
            H3Api: {
                async refvidGet() {
                    return { body: { ok: true, data: { on: false, ar: "16:9", mp: 0.98,
                        no_upscale: true, fit: "裁切填满", resample: "lanczos",
                        fps_norm: false, grid: "向下", limit: "最长段", limit_s: 0,
                        audio: true } } };
                },
                async refvidAlign(p) {
                    calls.push(["align", p]);
                    return { body: { ok: true, data: {
                        label: "a.mp4",
                        info: { fps: 30, frames: 126, duration_s: 4.2, width: 1280, height: 720 },
                        frames: 124, seconds: 124 / 24, notes: [],
                    } } };
                },
                async refvidSet(p) { calls.push(["set", p]); return { body: { ok: true, data: p.refvid } }; },
            },
        });
        ok(!errors.length, "装载期无异常（" + errors.join(" | ") + "）");
        w.confirm = (msg) => { confirms.push(String(msg)); return true; };

        await w.loadRefvid();
        await w.alignRefVideoTime(node);

        const align = calls.find((c) => c[0] === "align");
        ok(!!align, "确实调了 refvidAlign");
        ok(align && align[1].item && align[1].item.label === "a.mp4",
            "对齐目标是池里第一个视频（= 官方 <Video 1>）");
        ok(align && align[1].dir === "PROJ", "带上了存档目录（素材三级寻址要用）");
        ok(align && align[1].max_frames === 192, "带上了本链最长段帧数（最长段 8s → 192 帧）");

        const ds = JSON.parse(node.widgets.find((x) => x.name === "导演台状态").value);
        const want = 124 / 24;
        ok(Math.abs(ds.segments[0].seconds - want) < 1e-9, "段1（引用视频）写成了 5.167s（官方口径）");
        ok(Math.abs(ds.segments[2].seconds - want) < 1e-9, "段3（引用视频）也写了 5.167s");
        ok(ds.segments[1].seconds === 7, "段2（只引用图片）**没被动** —— 仍是 7s");

        ok(!calls.some((c) => c[0] === "set"), "**一个设置都没落**（refvidSet 不被调用）");
        ok(confirms.length === 1 && confirms[0].indexOf("24fps 直接播") >= 0,
            "确认框说明了口径：24fps 直接播、不重定时");
        ok(confirms[0].indexOf("5.17s") >= 0, "确认框给出了模型看到的时长");
        ok(confirms[0].indexOf("段 1、段 3") >= 0, "确认框点名了会被改的段");
    }

    /* ================= ② 取消 = 一个字节都不写 ================= */
    {
        const calls = [];
        const node = mkNode(mkDs());
        const { w } = load({
            node,
            H3Api: {
                async refvidAlign(p) {
                    calls.push(["align", p]);
                    return { body: { ok: true, data: { frames: 124, seconds: 124 / 24,
                        info: {}, notes: [] } } };
                },
                async refvidSet(p) { calls.push(["set", p]); return { body: { ok: true, data: p.refvid } }; },
            },
        });
        w.confirm = () => false;
        const before = node.widgets.find((x) => x.name === "导演台状态").value;
        await w.alignRefVideoTime(node);
        ok(!calls.some((c) => c[0] === "set"), "点「取消」不落设置（本来也不落）");
        ok(node.widgets.find((x) => x.name === "导演台状态").value === before,
            "点「取消」不改任何段时长");
    }

    /* ================= ③ 没有视频引用：明确报错 ================= */
    {
        const alerts = [];
        const calls = [];
        const node = mkNode(mkDs({ segments: [{ seconds: 5, refs: ["背景板"] }],
                                   prompts: ["@背景板 空镜。"] }));
        const { w } = load({
            node,
            onAlert: (m) => alerts.push(String(m)),
            H3Api: {
                async refvidAlign(p) { calls.push(p); return { body: { ok: true, data: {} } }; },
                async refvidSet(p) { calls.push(["set", p]); return { body: { ok: true, data: p } }; },
            },
        });
        await w.alignRefVideoTime(node);
        ok(!calls.length, "没有段引用视频时不发请求");
        ok(alerts.length && alerts[0].indexOf("没有任何段引用视频") >= 0,
            "而是明确报「没有任何段引用视频」（不静默无反应）");
    }

    /* ================= ④ 素材池里没有视频 ================= */
    {
        const alerts = [];
        const node = mkNode(mkDs({ ref_assets: [POOL[1], POOL[2]] }));
        const { w } = load({ node, onAlert: (m) => alerts.push(String(m)),
                             H3Api: { async refvidAlign() { return { body: { ok: true } }; },
                                      async refvidSet(p) { return { body: { ok: true, data: p } }; } } });
        await w.alignRefVideoTime(node);
        ok(alerts.length && alerts[0].indexOf("还没有视频素材") >= 0,
            "池里没视频时提示先上传，而不是报参数错");
    }

    /* ================= ⑤ 按钮挂在「每段时长」那一格 ================= */
    {
        const node = mkNode(mkDs());
        const { w } = load({ node, H3Api: {} });
        const field = w.renderWidgetField(node, "每段时长");
        /* 对齐按钮**不在**时长那一格里：它是 grid 的独立一项（field._extraEl，跨整行）。
         * 塞进同一格就得整格跨行，输入框会被拉成一整条（用户否过这一版）。 */
        const extraRow = field._extraEl;
        ok(!!extraRow && extraRow.className.indexOf("h3d-alignrow") >= 0,
            "对齐按钮应作为独立整行（h3d-alignrow）挂在时长格后面");
        const btn = (extraRow || field).querySelector("button.h3d-alignbtn");
        ok(!!btn, "「每段时长」下方有对齐按钮（项目级时间入口旁边）");
        /* 时长格本身保持半格，右侧用小标占位（帧数），不再空着一大片 */
        ok(!field.classList.contains("h3d-param-wide"), "时长格不该跨整行（输入框会被拉宽）");
        const fchip = field.querySelector("span.h3d-frames");
        ok(!!fchip, "时长格右侧应有帧数小标");
        const durW = node.widgets.find((x) => x.name === "每段时长");
        ok(fchip && fchip.textContent.indexOf(String(Math.round(Number(durW.value) * 24))) >= 0,
            "帧数小标应按 24fps 折算：" + (fchip && fchip.textContent));
        ok(btn && btn.textContent.indexOf("对齐动作迁移参考视频") >= 0,
            "按钮文案写明是对齐动作迁移参考视频时间：" + (btn ? btn.textContent : "无按钮"));
        ok(btn && btn.title.indexOf("官方口径") >= 0 && btn.title.indexOf("只写段时长") >= 0,
            "悬停说明了口径（官方口径 · 只写段时长）");
        /* 段卡里不该再有第二个入口（用户明确要项目级） */
        ok(w.document.querySelectorAll("button.h3d-alignbtn").length <= 1,
            "只有一个对齐按钮，不在段卡里重复摆");
    }

    console.log(bad ? `\n${bad} FAILED` : "\nALL OK");
    process.exit(bad ? 1 : 0);
})().catch((e) => {
    console.error("FATAL", e);
    process.exit(1);
});
