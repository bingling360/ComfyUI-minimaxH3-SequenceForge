/* JSON 导入的端到端：预览 → 写入 / 转总提示词框（jsdom 真跑整个 h3_director.js）。
 *
 * 钉的是这条通道**不会绕过闸门**：
 *  · 解析出来的东西先给人看（每段 时长 / 独立镜头 / 正文首行），点了「写入」才动链；
 *  · 写入走的是 applyMasterPromptParsed —— 与文本粘贴**同一条**写入路径；
 *  · 「在总提示词框里查看」把 JSON 渲染成同一套 [Segment N] 文本，不另造一种格式；
 *  · 解析失败时不弹预览、不动链。
 *
 * 跑法：NODE_PATH=<repo>/node_modules node tests/js/master_prompt_json_import_check.js
 */
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");

let harness;
try {
    harness = require("./_harness.js");
} catch (e) {
    console.error("SKIP: 未安装 jsdom（npm install）");
    process.exit(2);
}

const src = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8");
const fails = [];
const ok = (cond, msg) => { if (!cond) fails.push(msg); };

const JSON_OK = JSON.stringify({
    format: "sequenceforge.master-prompt",
    version: 1,
    title: "雨夜市场",
    segments: [
        { duration: 10, standalone: false,
            prompt: "integrated_multimodal_description: [Shot 1] Live-action, a rainy alley.\n"
                + "\noverall_soundscape: Steady rain.\n\nnon_diegetic_music: N/A" },
        { duration: 8, standalone: true,
            prompt: "integrated_multimodal_description: [Shot 1] She turns into the crowd." },
    ],
});

const mkDs = () => ({
    prompts: ["旧的段一", "旧的段二", "旧的段三"],
    segments: [{ seconds: 5 }, { seconds: 5 }, { seconds: 5 }],
    ref_assets: [],
});

/* ---------- 1. 入口存在（顶栏那两个按钮并排，不是只加了个函数没人调） ---------- */
ok(src.indexOf("mpRow.append(mpBtn, mpJsonBtn)") >= 0, "顶栏没有把「导入 JSON」挂在总提示词旁边");
ok(/mpJsonBtn\.onclick\s*=\s*importMasterPromptJson/.test(src), "「导入 JSON」按钮没接上处理函数");

/* ---------- 2. 预览：先给看，不动链 ---------- */
{
    const ds = mkDs();
    const { w, errors } = harness.load({ node: harness.mkNode(ds) });
    ok(typeof w.importMasterPromptJson === "function", "importMasterPromptJson 未定义");
    ok(typeof w.openMasterPromptJsonPreview === "function", "openMasterPromptJsonPreview 未定义");

    const node = w.findNode();
    ok(!!node, "findNode() 拿不到节点（后续链路会静默走「未找到节点」分支）");
    const p = w.parseMasterPromptJson(JSON_OK);
    ok(p && p.ok === true, "解析失败：" + JSON.stringify(p && p.error));

    w.openMasterPromptJsonPreview(node, p, "agent.json");
    const list = w.document.querySelector(".h3d-jprev");
    ok(!!list, "预览没打开（缺 .h3d-jprev）");
    const rows = list ? [...list.querySelectorAll(".h3d-jrow")] : [];
    ok(rows.length === 2, `预览应列出 2 段，实际 ${rows.length}`);
    if (rows.length === 2) {
        ok(String(rows[0].textContent).indexOf("10s") >= 0, "预览第 1 段缺时长：" + rows[0].textContent);
        ok(String(rows[1].textContent).indexOf("独立镜头") >= 0,
            "预览第 2 段缺独立镜头：" + rows[1].textContent);
        ok(String(rows[0].textContent).indexOf("rainy alley") >= 0,
            "预览第 1 段缺正文首行：" + rows[0].textContent);
    }
    /* 预览阶段绝不动链 */
    const now = JSON.parse(node.widgets[0].value);
    ok(now.prompts.length === 3 && now.prompts[0] === "旧的段一", "预览阶段就把链改了");

    /* ---------- 3. 写入：走的是同一条写入路径 ---------- */
    const btn = [...w.document.querySelectorAll("button")]
        .find((b) => String(b.textContent || "").trim() === "写入 2 段");
    ok(!!btn, "找不到「写入 2 段」按钮："
        + JSON.stringify([...w.document.querySelectorAll("button")].map((b) => b.textContent)));
    if (btn) {
        btn.onclick();
        const after = JSON.parse(node.widgets[0].value);
        ok(after.prompts.length === 2, `写入后应变成 2 段，实际 ${after.prompts.length}`);
        ok(String(after.prompts[0]).indexOf("rainy alley") > 0, "第 1 段正文没写进去");
        ok(String(after.prompts[1]).indexOf("crowd") > 0, "第 2 段正文没写进去");
        ok(Number(after.segments[0].seconds) === 10 && Number(after.segments[1].seconds) === 8,
            "段时长没写进去：" + JSON.stringify(after.segments.map((s) => s.seconds)));
        /* 独立镜头：true 一侧 unlink=true / auto_ref=false；false 一侧相反
         * （与文本通道 applyMasterPrompt 的口径逐字一致） */
        ok(after.segments[0].unlink === false && after.segments[0].auto_ref === true,
            "第 1 段应连续接上段：" + JSON.stringify(after.segments[0]));
        ok(after.segments[1].unlink === true && after.segments[1].auto_ref === false,
            "第 2 段应独立镜头：" + JSON.stringify(after.segments[1]));
    }
    ok(!w.document.querySelector(".h3d-jprev"), "写入后预览没关");

    const real = (errors || []).filter((e) => !/Not implemented|jsdom/i.test(String(e)));
    ok(real.length === 0, "运行时报错：" + JSON.stringify(real));
}

/* ---------- 4. 「在总提示词框里查看」：渲染成同一套 [Segment N] 文本 ---------- */
{
    const ds = mkDs();
    const { w } = harness.load({ node: harness.mkNode(ds) });
    const node = w.findNode();
    const p = w.parseMasterPromptJson(JSON_OK);
    w.openMasterPromptJsonPreview(node, p, "agent.json");
    const bView = [...w.document.querySelectorAll("button")]
        .find((b) => String(b.textContent || "").trim() === "在总提示词框里查看");
    ok(!!bView, "找不到「在总提示词框里查看」按钮");
    if (bView) {
        bView.onclick();
        const dialog = w.document.querySelector(".h3d-dialog-full");
        ok(!!dialog, "总提示词框没打开");
        const box = dialog && dialog.querySelector(".h3d-mpbox");
        const ta = box && (box.__h3Editor || box);
        ok(!!ta && String(ta.value).indexOf("[Segment 1]") >= 0,
            "预填内容不是 [Segment N] 格式：" + JSON.stringify(ta && ta.value));
        ok(String(ta.value).indexOf("Duration: 10") >= 0, "预填缺时长：\n" + ta.value);
        ok(String(ta.value).indexOf("Standalone: yes") >= 0, "预填缺独立镜头：\n" + ta.value);
        /* 预填**不等于**写入：链上仍是旧的 */
        const now = JSON.parse(node.widgets[0].value);
        ok(now.prompts.length === 3, "「查看」不该写回链上");
    }
}

/* ---------- 5. 坏的 JSON：不弹预览、不动链 ---------- */
{
    const ds = mkDs();
    const alerts = [];
    const { w } = harness.load({ node: harness.mkNode(ds), onAlert: (m) => alerts.push(m) });
    const node = w.findNode();
    const p = w.parseMasterPromptJson('{"segments":[{"duration":10}]}');
    ok(p.ok === false, "没有正文的 JSON 应判失败");
    w.openMasterPromptJsonPreview(node, p, "bad.json");
    ok(!w.document.querySelector(".h3d-jprev"), "解析失败还弹了预览");
    ok(alerts.length === 1 && String(alerts[0]).indexOf("导入失败") >= 0,
        "失败时应 alert 一次：" + JSON.stringify(alerts));
    const now = JSON.parse(node.widgets[0].value);
    ok(now.prompts.length === 3 && now.prompts[0] === "旧的段一", "失败时不该动链");
}

if (fails.length) {
    console.error("FAIL:\n - " + fails.join("\n - "));
    process.exit(1);
}
console.log("OK: JSON 导入 = 预览 → 同一条写入路径（与文本通道同构）");
