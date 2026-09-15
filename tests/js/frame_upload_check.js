/* 首尾帧「已有锚 + 再上传一张」端到端回归（jsdom 真跑 shipped 源码）。
 *
 * 背景（用户报的"整个导演台都崩掉了"）：
 *   上传别名 = 文件名主干.slice(0, 24)。后端给新素材挑标签时写的是
 *       while lbl in taken: lbl = f"{base}{n}"[:24]
 *   当 base 本身已满 24 字时，截断后恒等于 base → **死循环** → aiohttp 事件循环
 *   被占死 → ComfyUI 全站无响应（导演台、队列、所有 /h3chain 接口一起挂）。
 *   触发条件就是"连着传两张同名前缀 ≥24 字的图"。
 *
 * 本文件锁两件事：
 *   1) 前端 pushPoolAsset 的标签唯一化必须能停（同样不能写 truncate-in-loop）；
 *   2) 已有锚时再上传一张：锚点换掉、对齐指令跟着重写、池子不丢条目、
 *      整台 refresh 不抛异常（分区兜底后更不该把台打死）。
 */
const { load, mkNode } = require("./_harness.js");

let fails = 0;
function ok(cond, msg) {
    if (cond) return;
    fails += 1;
    console.log("FAIL:", msg);
}

/* ---------- 1) 标签唯一化（纯函数，直接跑源码） ---------- */
{
    const ds = { prompts: [""], segments: [], ref_assets: [] };
    const node = mkNode(ds);
    node.type = "H3SeamlessChainSampler";
    const { w } = load({ node });
    const push = w.eval("pushPoolAsset");
    const getDs = w.eval("getDs");

    push(node, "assets/x.png", "女主");
    push(node, "assets/y.png", "女主");
    push(node, "assets/z.png", "女主");
    const labels = getDs(node).ref_assets.map((a) => a.label);
    ok(labels.length === 3, `应入池 3 条，实际 ${labels.length}`);
    ok(new Set(labels).size === 3, `标签必须互不相同：${JSON.stringify(labels)}`);

    /* 24 字打满 + 撞名：必须在有限步内给出不同标签（旧写法这里转不出来） */
    const longBase = "角".repeat(24);
    push(node, "assets/l1.png", longBase);
    push(node, "assets/l2.png", longBase);
    push(node, "assets/l3.png", longBase);
    const l2 = getDs(node).ref_assets.slice(3).map((a) => a.label);
    ok(new Set(l2).size === 3, `24 字打满时也要各自唯一：${JSON.stringify(l2)}`);
    ok(l2.every((s) => s && s.length <= 24), "标签不得超过 24 字");

    /* 幂等：同 file 重复推不产生第二条 */
    const before = getDs(node).ref_assets.length;
    push(node, "assets/l1.png", longBase);
    ok(getDs(node).ref_assets.length === before, "同 file 重复推池必须幂等");
}

/* ---------- 2) 已有锚 + 再上传一张：整条链路 ---------- */
(async () => {
    const I2 = "For the target video, at 0.00 seconds into the target video, "
        + "<Picture 1> (from [Shot 1]) is fully referenced.";
    const manifest = {
        revision: 3,
        assets: [{ file: "assets/A.png", label: "A", kind: "image" }],
        prompts: [`integrated_multimodal_description: ${I2}\n主体`],
        segments: [{ seconds: 5, frame_img: { first: "assets/A.png" } }],
        done: 0, finals: [], merges: [],
    };
    let links = [];
    const ds = {
        prompts: [`integrated_multimodal_description: ${I2}\n主体`],
        segments: [{ seconds: 5, frame_img: { first: "assets/A.png" } }],
        ref_assets: [{ file: "assets/A.png", kind: "image", label: "A", asset_id: "", roles: [] }],
    };
    const node = mkNode(ds);
    node.type = "H3SeamlessChainSampler";

    const alerts = [];
    const { w, dom } = load({
        node,
        onAlert: (m) => alerts.push(String(m)),
        routes: { "manifest.json": () => manifest },
        H3Api: {
            async libraryUpload() {
                manifest.assets.push({ file: "assets/B.png", label: "B", kind: "image" });
                manifest.revision += 1;
                links.push({ asset_id: "a_0123456789ab", alias: "B", file: "assets/B.png", kind: "image" });
                return { body: { ok: true, dest: "project", stored: { file: "assets/B.png", label: "B", name: "B.png" } } };
            },
            async assetLinks() { return { body: { ok: true, links } }; },
        },
    });
    const doc = w.document;
    /* 抓住 upBtn.onclick 里临时创建的 file input */
    const realCreate = doc.createElement.bind(doc);
    let captured = null;
    doc.createElement = function (t, ...r) {
        const e = realCreate(t, ...r);
        if (String(t).toLowerCase() === "input") {
            const oc = e.click.bind(e);
            e.click = () => { if (e.type === "file") captured = e; else oc(); };
        }
        return e;
    };

    w.eval("openDesk")();
    await new Promise((r) => setTimeout(r, 30));

    const p = w.eval("openFramePicker")(node, 0, "first", "首帧图", () => {});
    await new Promise((r) => setTimeout(r, 30));
    await p;
    const up = [...doc.querySelectorAll(".h3d-dialog-row button")]
        .find((b) => b.textContent.indexOf("上传") >= 0);
    ok(!!up, "选图弹窗应有「上传一张」");

    /* 重复点两下：并发闸门必须挡住第二次（否则状态互相踩） */
    up.click();
    up.click();
    const blob = new w.Blob([new Uint8Array([1, 2, 3])], { type: "image/png" });
    blob.name = "B.png";
    Object.defineProperty(captured, "files", { value: [blob], configurable: true });
    await captured.onchange();
    await new Promise((r) => setTimeout(r, 60));

    ok(alerts.length === 0, `不该弹上传失败：${alerts.join(" / ")}`);
    const d2 = w.eval("getDs")(node);
    ok(d2.segments[0].frame_img.first === "assets/B.png",
        `锚点应换成新图，实际 ${JSON.stringify(d2.segments[0].frame_img)}`);
    const files = d2.ref_assets.map((a) => a.file);
    ok(files.indexOf("assets/A.png") >= 0 && files.indexOf("assets/B.png") >= 0,
        `两张图都要在池里：${JSON.stringify(files)}`);
    ok(files.length === new Set(files).size, `池内 file 不能重复：${JSON.stringify(files)}`);
    ok(String(d2.prompts[0]).indexOf("0.00 seconds into the target video") > 0,
        "换锚后对齐指令行必须还在");

    /* 台子还能刷新：分区兜底后任何一区异常都不该冒到这里 */
    await w.eval("refresh")();
    await w.eval("refresh")();
    ok(!!doc.querySelector(".h3d-center-pad"), "刷新后面板仍在");
    const errChip = doc.querySelector(".h3d-zoneerr");
    ok(!errChip || errChip.style.display === "none", "不该出现分区渲染异常");

    console.log(fails ? `\n${fails} 个断言失败` : "\nframe_upload_check 全部通过");
    process.exit(fails ? 1 : 0);
})();
