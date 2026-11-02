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

/* 「已有 1-3 段，再导入 4-6 段」的典型文件：index 是**目标段号**，不是"文件里的第几条"。
 * 这条用例是 2026-11-02 那次改动的核心 —— 以前 index 只用来排序，排完就丢，
 * 导入只能整链替换，想续写只能先把 1-3 段也塞进同一个文件里。 */
const JSON_TAIL = JSON.stringify({
    title: "雨夜市场 · 续三场",
    segments: [
        { index: 4, duration: 6, prompt: "integrated_multimodal_description: [Shot 1] 第四段：她走进门。" },
        { index: 5, duration: 7, prompt: "integrated_multimodal_description: [Shot 1] 第五段：门内的灯。" },
        { index: 6, duration: 7, prompt: "integrated_multimodal_description: [Shot 1] 第六段：她坐下。" },
    ],
});

/* 「只导入第 2 段覆盖原第 2 段」：一份单段文件 + 目标段号。 */
const JSON_ONE = JSON.stringify({
    segments: [{ index: 2, duration: 9, standalone: true,
        prompt: "integrated_multimodal_description: [Shot 1] 覆盖版第二段。" }],
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
    /* 默认档 = 整体替换（这份 JSON 的段号从 1 起）→ 按钮写「整体替换为 2 段」；
     * 段号从 4 起时会变成「写入第 4–6 段」（见第 5 节）。 */
    const btn = [...w.document.querySelectorAll("button")]
        .find((b) => String(b.textContent || "").trim() === "整体替换为 2 段");
    ok(!!btn, "找不到「整体替换为 2 段」按钮："
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

/* ---------- 5. 续写：段号从 4 起 → 默认落在第 4 段，前 3 段一字不动 ---------- */
{
    const ds = mkDs();
    const { w } = harness.load({ node: harness.mkNode(ds) });
    const node = w.findNode();
    const p = w.parseMasterPromptJson(JSON_TAIL);
    ok(p.ok === true, "续写文件解析失败：" + JSON.stringify(p && p.error));
    ok(p.segs.map((s) => s.idx).join(",") === "4,5,6",
        "index 应作为**目标段号**带出来：" + JSON.stringify(p.segs.map((s) => s.idx)));

    w.openMasterPromptJsonPreview(node, p, "tail.json");
    const radios = [...w.document.querySelectorAll(".h3d-mpwrite input[type=radio]")];
    ok(radios.length === 2, `写入位置应有 2 个选项，实际 ${radios.length}`);
    ok(radios[1] && radios[1].checked === true, "段号从 4 起 → 默认档应是「从第 4 段起写入」");
    const num = w.document.querySelector(".h3d-mpwrite-n");
    ok(num && Number(num.value) === 4, "起点没预填成 4：" + (num && num.value));
    const sum = String((w.document.querySelector(".h3d-mpwrite-sum") || {}).textContent || "");
    ok(sum.indexOf("写入后共 6 段") >= 0, "摘要没说清「写入后共 6 段」：" + sum);
    /* 名单里的段号得跟着写入位置走：第 1 条是**目标第 4 段**，不是"第 1 条" */
    const labs = [...w.document.querySelectorAll(".h3d-jrow b")].map((b) => b.textContent);
    ok(labs.join("|") === "段 4|段 5|段 6", "预览段号应跟着写入位置走：" + labs.join("|"));
    /* 预览阶段仍不动链 */
    ok(JSON.parse(node.widgets[0].value).prompts.length === 3, "预览阶段就动了链");

    const btn = [...w.document.querySelectorAll("button")]
        .find((b) => String(b.textContent || "").trim() === "写入第 4–6 段");
    ok(!!btn, "按钮应写成「写入第 4–6 段」："
        + JSON.stringify([...w.document.querySelectorAll("button")].map((b) => b.textContent)));
    if (btn) {
        btn.onclick();
        const after = JSON.parse(node.widgets[0].value);
        ok(after.prompts.length === 6, `续写后应共 6 段，实际 ${after.prompts.length}`);
        ok(after.prompts[0] === "旧的段一" && after.prompts[2] === "旧的段三",
            "前 3 段必须原样保留：" + JSON.stringify(after.prompts));
        ok(String(after.prompts[3]).indexOf("第四段") > 0, "第 4 段正文没写进去");
        ok(String(after.prompts[5]).indexOf("第六段") > 0, "第 6 段正文没写进去");
        ok(Number(after.segments[3].seconds) === 6 && Number(after.segments[5].seconds) === 7,
            "新增段的时长没写进去：" + JSON.stringify(after.segments.map((s) => s.seconds)));
    }
}

/* ---------- 6. 单段覆盖：只写第 2 段，段数不变，该段的帧锚不许被冲掉 ---------- */
{
    const ds = mkDs();
    ds.segments[1].frame_img = { first: "锚.png" };   // 段级运行参数（JSON 里没有这条通道）
    const { w } = harness.load({ node: harness.mkNode(ds) });
    const node = w.findNode();
    const p = w.parseMasterPromptJson(JSON_ONE);
    ok(p.ok === true && p.segs.length === 1, "单段文件应解析出 1 段");
    ok(p.segs[0].idx === 2, "单段 index=2 没带出来：" + p.segs[0].idx);

    w.openMasterPromptJsonPreview(node, p, "one.json");
    const num = w.document.querySelector(".h3d-mpwrite-n");
    ok(num && Number(num.value) === 2, "单段 index=2 → 起点应是 2：" + (num && num.value));
    const btn = [...w.document.querySelectorAll("button")]
        .find((b) => String(b.textContent || "").trim() === "写入第 2 段");
    ok(!!btn, "按钮应写成「写入第 2 段」："
        + JSON.stringify([...w.document.querySelectorAll("button")].map((b) => b.textContent)));
    if (btn) {
        btn.onclick();
        const after = JSON.parse(node.widgets[0].value);
        ok(after.prompts.length === 3, `覆盖单段后段数应不变（3），实际 ${after.prompts.length}`);
        ok(String(after.prompts[1]).indexOf("覆盖版第二段") > 0, "第 2 段没被覆盖");
        ok(after.prompts[0] === "旧的段一" && after.prompts[2] === "旧的段三",
            "第 1 / 3 段不该动：" + JSON.stringify(after.prompts));
        ok(Number(after.segments[1].seconds) === 9, "覆盖段的时长应更新：" + after.segments[1].seconds);
        ok(after.segments[1].unlink === true, "覆盖段的 standalone 应更新");
        ok(after.segments[1].frame_img && after.segments[1].frame_img.first === "锚.png",
            "覆盖段自己的帧锚必须保留（JSON 没有这条通道，不能被顺手清掉）："
            + JSON.stringify(after.segments[1]));
    }
}

/* ---------- 7. 手动改档：同一份续写文件也能整链替换（历史口径仍在） ---------- */
{
    const ds = mkDs();
    const { w } = harness.load({ node: harness.mkNode(ds) });
    const node = w.findNode();
    const p = w.parseMasterPromptJson(JSON_TAIL);
    w.openMasterPromptJsonPreview(node, p, "tail.json");
    const radios = [...w.document.querySelectorAll(".h3d-mpwrite input[type=radio]")];
    radios[0].onchange();                     // 切到「整体替换」
    const btn = [...w.document.querySelectorAll("button")]
        .find((b) => String(b.textContent || "").trim() === "整体替换为 3 段");
    ok(!!btn, "切到整体替换后按钮文案没跟上："
        + JSON.stringify([...w.document.querySelectorAll("button")].map((b) => b.textContent)));
    if (btn) {
        btn.onclick();
        const after = JSON.parse(node.widgets[0].value);
        ok(after.prompts.length === 3, `整体替换后应 3 段，实际 ${after.prompts.length}`);
        ok(String(after.prompts[0]).indexOf("第四段") > 0,
            "整体替换应把这份内容的第 1 条放链首：" + String(after.prompts[0]).slice(0, 40));
    }
}

/* ---------- 8. 追加到末尾 / 中间补空段 ---------- */
{
    const ds = mkDs();
    const { w } = harness.load({ node: harness.mkNode(ds) });
    const node = w.findNode();
    const p = w.parseMasterPromptJson(JSON_ONE);
    w.openMasterPromptJsonPreview(node, p, "one.json");
    const bTail = [...w.document.querySelectorAll("button")]
        .find((b) => String(b.textContent || "").trim() === "追加到末尾");
    ok(!!bTail, "找不到「追加到末尾」："
        + JSON.stringify([...w.document.querySelectorAll("button")].map((b) => b.textContent)));
    if (bTail) {
        bTail.onclick();
        const num = w.document.querySelector(".h3d-mpwrite-n");
        ok(Number(num.value) === 4, "追加到末尾应把起点推到 现有 3 段 + 1 = 4：" + num.value);
        /* 手填 6：第 4-5 段没有内容 → 补空段（不是把第 4 段挤掉） */
        num.value = "6";
        num.dispatchEvent(new w.Event("input"));
        const sum = String(w.document.querySelector(".h3d-mpwrite-sum").textContent || "");
        ok(sum.indexOf("补 2 个空段") >= 0, "从第 6 段起写、现有 3 段 → 应提示补 2 个空段：" + sum);
        const btn = [...w.document.querySelectorAll("button")]
            .find((b) => String(b.textContent || "").trim() === "写入第 6 段");
        ok(!!btn, "找不到「写入第 6 段」按钮");
        if (btn) {
            btn.onclick();
            const after = JSON.parse(node.widgets[0].value);
            ok(after.prompts.length === 6, `应补到 6 段，实际 ${after.prompts.length}`);
            ok(after.prompts[3] === "" && after.prompts[4] === "",
                "第 4/5 段应是空段：" + JSON.stringify(after.prompts));
            ok(after.prompts[0] === "旧的段一", "第 1 段不该动");
            ok(String(after.prompts[5]).indexOf("覆盖版第二段") > 0, "第 6 段没写进去");
        }
    }
}

if (fails.length) {
    console.error("FAIL:\n - " + fails.join("\n - "));
    process.exit(1);
}
console.log("OK: JSON 导入 = 预览 → 同一条写入路径（可整链替换 / 可续写 / 可单段覆盖）");
