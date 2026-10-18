/* 切工作流不丢分段提示词（P0）：换图前落盘 + 落盘绑定发起时的 node/dir + 去重。
 *
 * 三条断言对应三个真实事故：
 *   1) `app.loadGraphData` 直接换图 → 350ms 窗口的输入、以及没进工作流文件的编辑同时消失；
 *   2) `schedulePromptFlush` 1.5s 后才 findNode() → 切到同名项目的新图时，
 *      空 prompts 把磁盘上唯一那份兜底冲成空（最伤的一条）；
 *   3) 每次落盘都 revision += 1 → 换图钩子会让版本号空涨，必须做内容去重。
 */
const { load, mkNode } = require("./_harness.js");

let fails = 0;
function ok(cond, msg) {
    if (cond) return;
    fails += 1;
    console.log("FAIL:", msg);
}

function mk(id, dir, text) {
    const n = mkNode({ prompts: [text], segments: [{ seconds: 5 }], ref_assets: [] });
    n.id = id;
    const wd = n.widgets.find((x) => x.name === "存档目录");
    if (wd) wd.value = dir;
    return n;
}

(async () => {
    /* ---------- 1) 换图前落盘：旧节点 + 旧目录 + 最新正文 ---------- */
    {
        const node = mk(7, "PROJ", "A text");
        const { w } = load({ node });
        w.app.graph._nodes = [node];
        w.app._ext.setup();                       // 装上 loadGraphData 包装器（真实链路）
        w.app.loadGraphData({ nodes: [] });        // 模拟切到另一份工作流
        const calls = w.__apiCalls.filter((c) => c.path.indexOf("save_prompts") >= 0);
        ok(calls.length === 1, `换图应触发一次 save_prompts，实际 ${calls.length}`);
        const b = calls[0] && calls[0].body;
        ok(b && b.dir === "PROJ", `落盘必须写**旧**目录 PROJ，实际 ${b && b.dir}`);
        ok(b && (b.prompts || [])[0] === "A text",
            `落盘必须带旧正文，实际 ${JSON.stringify(b && b.prompts)}`);
    }

    /* ---------- 2) 1.5s 落盘绑定发起时的 node/dir：切到别的节点也不串写 ---------- */
    {
        const a = mk(7, "PROJ", "A text");
        const b = mk(8, "OTHER", "");
        const { w } = load({ node: a });
        w.app.graph._nodes = [a];
        w.eval("schedulePromptFlush")(a);
        w.app.graph._nodes = [b];                 // 模拟"切走了"：新图第一个节点是 b
        await new Promise((r) => setTimeout(r, 1700));
        const calls = w.__apiCalls.filter((c) => c.path.indexOf("save_prompts") >= 0);
        ok(calls.length === 1, `挂起落盘应只发一次，实际 ${calls.length}`);
        const body = calls[0] && calls[0].body;
        ok(body && body.dir === "PROJ",
            `迟到落盘必须写发起时的 PROJ（不能写新节点的 OTHER），实际 ${body && body.dir}`);
        ok(body && (body.prompts || [])[0] === "A text",
            `迟到落盘必须带发起时的正文，实际 ${JSON.stringify(body && body.prompts)}`);
    }

    /* ---------- 3) 内容不变不重复落盘 ---------- */
    {
        const node = mk(7, "PROJ", "A text");
        const { w } = load({ node });
        w.app.graph._nodes = [node];
        await w.eval("flushPrompts")(node, "PROJ");
        await w.eval("flushPrompts")(node, "PROJ");
        const calls = w.__apiCalls.filter((c) => c.path.indexOf("save_prompts") >= 0);
        ok(calls.length === 1, `同内容第二次应跳过，实际 ${calls.length}`);
    }

    /* ---------- 4) 内容变了必须照发（去重不能把真改动吞掉） ---------- */
    {
        const node = mk(7, "PROJ", "A text");
        const { w } = load({ node });
        w.app.graph._nodes = [node];
        await w.eval("flushPrompts")(node, "PROJ");
        w.eval("setPromptText")(node, 0, "B text");
        await w.eval("flushPrompts")(node, "PROJ");
        const calls = w.__apiCalls.filter((c) => c.path.indexOf("save_prompts") >= 0);
        ok(calls.length === 2, `内容变化必须再发一次，实际 ${calls.length}`);
        ok(calls[1] && (calls[1].body.prompts || [])[0] === "B text",
            `第二次落盘应带新正文，实际 ${JSON.stringify(calls[1] && calls[1].body.prompts)}`);
    }

    console.log(fails ? `\n${fails} 个断言失败` : "\ngraph_switch_flush_check 全部通过");
    process.exit(fails ? 1 : 0);
})();
