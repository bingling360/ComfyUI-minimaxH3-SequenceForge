/* 优化进度条 + 流式调用兜底 回归守卫。
 *
 * 背景（2026-09-19）：用户报「缺了个进度条」。进度条做出来后有两个地方最容易
 * 悄悄坏掉，而手点很难覆盖：
 *   1. **比例倒退 / 顶死不动** —— 思考阶段跨度能从 70 字到 13000 字，曲线写错
 *      就是"一直 60% 卡住"或"跳回去"，看起来比没有进度条更糟；
 *   2. **降级路径** —— 老后端没有 optimize_stream 时必须静默退回整包，
 *      不能因为"多了一条进度条"把老版本用户挡在门外。
 * 另外取消/失败必须把浮层撤掉，不能留一块盖在卡片上的残影。
 *
 * 用法：node tests/js/opt_progress_check.js
 */
const assert = require("assert");
const { load, mkNode } = require("./_harness");

let ok = true;
const results = [];
async function ta(name, fn) {
    try { await fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}

const pct = (w) => {
    const f = w.document.querySelector(".h3d-opt-prog-fill");
    return f ? parseFloat(f.style.width) || 0 : -1;
};
const meta = (w) => (w.document.querySelector(".h3d-opt-prog-meta")?.textContent || "");

(async function main() {
    console.log("\n== 1 进度条渲染 ==");
    const { w, errors } = load({});
    await ta("开一条进度条：有标题 / 轨道 / 计数行 / 取消按钮", async () => {
        const p = w.optProgressStart("第 1 段 · 提示词优化");
        assert.ok(w.document.querySelector(".h3d-opt-prog"), "浮层没建出来");
        assert.ok(/第 1 段/.test(w.document.querySelector(".h3d-opt-prog-name").textContent));
        assert.ok(w.document.querySelector(".h3d-opt-prog-track"), "缺轨道");
        assert.ok(w.document.querySelector(".h3d-opt-prog-cancel"), "缺取消按钮");
        assert.strictEqual(pct(w) > 0, true, "初始就该有点宽度（否则看着像没启动）");
        p.close();
    });

    await ta("思考帧 -> 显示「思考中 · 推理 N 字」且比例推进", async () => {
        const p = w.optProgressStart("t");
        p.update({ type: "progress", phase: "thinking", reasoning_chars: 0, content_chars: 0 });
        const p0 = pct(w);
        p.update({ type: "progress", phase: "thinking", reasoning_chars: 1200, content_chars: 0 });
        const p1 = pct(w);
        assert.ok(/思考中/.test(meta(w)), "阶段文案不对：" + meta(w));
        assert.ok(/1200/.test(meta(w)), "推理字数没显示：" + meta(w));
        assert.ok(p1 > p0, `思考阶段比例该推进：${p0} -> ${p1}`);
        assert.ok(p1 < 60, "思考阶段不该越过 60%：" + p1);
        p.close();
    });

    await ta("撰写帧 -> 从思考段上界继续推（不回落）", async () => {
        const p = w.optProgressStart("t");
        p.update({ type: "progress", phase: "thinking", reasoning_chars: 5000, content_chars: 0 });
        const pThink = pct(w);
        p.update({ type: "progress", phase: "writing", reasoning_chars: 5000, content_chars: 200 });
        const pWrite = pct(w);
        assert.ok(/撰写中/.test(meta(w)), "阶段文案不对：" + meta(w));
        assert.ok(pWrite > pThink, `从思考切撰写该继续推进：${pThink} -> ${pWrite}`);
        assert.ok(pWrite < 99, "未完成不该显示满：" + pWrite);
        p.close();
    });

    await ta("没有思考阶段（关思考的型号）也不该从 60% 起跳", async () => {
        const p = w.optProgressStart("t");
        p.update({ type: "progress", phase: "writing", reasoning_chars: 0, content_chars: 100 });
        const p1 = pct(w);
        assert.ok(p1 < 30, `没思考过就不该跳过高位：${p1}`);
        p.update({ type: "progress", phase: "writing", reasoning_chars: 0, content_chars: 2000 });
        assert.ok(pct(w) > p1, "撰写阶段该继续推进");
        p.close();
    });

    await ta("字数回退/乱序事件 -> 比例单调不倒退", async () => {
        const p = w.optProgressStart("t");
        p.update({ type: "progress", phase: "thinking", reasoning_chars: 4000 });
        const hi = pct(w);
        p.update({ type: "progress", phase: "thinking", reasoning_chars: 10 });
        p.update({ type: "progress", phase: "connect" });
        assert.ok(pct(w) >= hi, `比例倒退了：${hi} -> ${pct(w)}`);
        p.close();
    });

    await ta("有 token 读数时按 token/max_tokens 抬高（但不越过 99）", async () => {
        const p = w.optProgressStart("t");
        p.update({ type: "progress", phase: "writing", content_chars: 10,
                   tokens: 8000, max_tokens: 8192 });
        assert.ok(pct(w) > 90 && pct(w) <= 99, "token 比例没生效：" + pct(w));
        p.close();
    });

    await ta("done() -> 满格 + done 类 + 撤掉取消按钮", async () => {
        const p = w.optProgressStart("t");
        p.done();
        const box = w.document.querySelector(".h3d-opt-prog");
        assert.strictEqual(pct(w), 100);
        assert.ok(box.classList.contains("h3d-done"), "缺少完成态样式类");
        assert.ok(!w.document.querySelector(".h3d-opt-prog-cancel"), "完成后取消按钮该撤掉");
        assert.ok(/完成/.test(meta(w)), "完成文案不对：" + meta(w));
        p.close();
    });

    await ta("close() -> 浮层从 DOM 移除（不留残影）", async () => {
        const p = w.optProgressStart("t");
        assert.ok(w.document.querySelector(".h3d-opt-prog"));
        p.close();
        assert.ok(!w.document.querySelector(".h3d-opt-prog"), "浮层没撤掉");
    });

    await ta("取消按钮 -> 回调触发且 cancelled() 为真", async () => {
        const p = w.optProgressStart("t");
        let hit = 0;
        p.onCancel(() => { hit += 1; });
        w.document.querySelector(".h3d-opt-prog-cancel").onclick();
        assert.strictEqual(hit, 1, "取消回调没触发");
        assert.strictEqual(p.cancelled(), true);
        p.close();
    });

    await ta("onPhase 回调把阶段推给调用方（按钮文案跟着变）", async () => {
        const p = w.optProgressStart("t");
        const seen = [];
        p.onPhase((ph) => seen.push(ph));
        p.update({ type: "progress", phase: "thinking", reasoning_chars: 5 });
        assert.ok(seen.includes("thinking"), "没收到 thinking：" + JSON.stringify(seen));
        p.close();
    });

    console.log("\n== 2 「扩写+优化」两段式进度 ==");
    await ta("带 stage=expand 的帧 -> 走第①格（文案带 ①扩写，比例 < 45%）", async () => {
        const p = w.optProgressStart("第 1 段 · 扩写 + 优化");
        p.update({ type: "progress", stage: "expand", phase: "writing", content_chars: 800 });
        const p1 = pct(w);
        assert.ok(/①/.test(meta(w)) && /扩写/.test(meta(w)), "阶段文案不对：" + meta(w));
        assert.ok(/800/.test(meta(w)), "扩写字数没显示：" + meta(w));
        assert.ok(p1 > 4 && p1 < 45, `扩写格应落在 4~45%：${p1}`);
        p.close();
    });

    await ta("扩写阶段内部也有 thinking/writing，但都算在第①格（不跳格）", async () => {
        const p = w.optProgressStart("t");
        p.update({ type: "progress", stage: "expand", phase: "thinking", reasoning_chars: 300 });
        const pThink = pct(w);
        p.update({ type: "progress", stage: "expand", phase: "writing", content_chars: 300 });
        const pWrite = pct(w);
        assert.ok(pThink > 4 && pThink < 45, `扩写思考也该在第①格：${pThink}`);
        assert.ok(pWrite >= pThink, `同字数换阶段不该倒退：${pThink} -> ${pWrite}`);
        assert.ok(pWrite < 45, "扩写格不该越过 45%：" + pWrite);
        p.close();
    });

    await ta("切到 stage=optimize -> 从 45% 继续（不回落到个位数）", async () => {
        const p = w.optProgressStart("t");
        p.update({ type: "progress", stage: "expand", phase: "writing", content_chars: 1200 });
        const pExpand = pct(w);
        p.update({ type: "progress", stage: "optimize", phase: "thinking", reasoning_chars: 100 });
        const pOpt = pct(w);
        assert.ok(/②/.test(meta(w)) && /优化/.test(meta(w)), "阶段文案不对：" + meta(w));
        assert.ok(pOpt >= 45 && pOpt > pExpand, `第②格该从 45% 起：${pExpand} -> ${pOpt}`);
        p.update({ type: "progress", stage: "optimize", phase: "writing", content_chars: 900 });
        assert.ok(pct(w) > 60, "第②格撰写段该过 60%：" + pct(w));
        assert.ok(pct(w) < 99, "未完成不该满格");
        p.close();
    });

    await ta("不带 stage 的帧（单段优化）仍是一格进度", async () => {
        const p = w.optProgressStart("t");
        p.update({ type: "progress", phase: "thinking", reasoning_chars: 100 });
        assert.ok(!/①|②/.test(meta(w)), "单段优化不该出现阶段编号：" + meta(w));
        p.close();
    });

    console.log("\n== 3 optCallStream：成功 / 取消 / 降级 ==");
    await ta("成功 -> 返回结果，浮层先停在完成态（1.4 秒后自动撤）", async () => {
        const { w: w2 } = load({ H3Api: {
            async optimizeStream(body, onEvent) {
                onEvent({ type: "progress", phase: "writing", content_chars: 300 });
                return { status: 200, body: { ok: true, prompt: "优化稿" }, events: [] };
            },
        } });
        const r = await w2.optCallStream({ prompt: "x" }, "标题", null);
        assert.strictEqual(r.body.prompt, "优化稿");
        assert.ok(w2.document.querySelector(".h3d-opt-prog"), "完成态应短暂保留（让人看到 100%）");
    });

    await ta("取消 -> 返回 cancelled 标记，不抛异常", async () => {
        const { w: w2 } = load({ H3Api: {
            async optimizeStream(body, onEvent, opts) {
                /* 模拟用户点了取消：AbortController 触发 AbortError */
                const btn = w2.document.querySelector(".h3d-opt-prog-cancel");
                assert.ok(btn, "还没开进度条就取消？");
                btn.onclick();
                const e = new Error("aborted");
                e.name = "AbortError";
                throw e;
            },
        } });
        const r = await w2.optCallStream({ prompt: "x" }, "标题", null);
        assert.strictEqual(r.body.cancelled, true, "应返回 cancelled 标记");
        assert.strictEqual(r.body.ok, false);
        assert.ok(!w2.document.querySelector(".h3d-opt-prog"), "取消后浮层该撤掉");
    });

    await ta("后端缺 optimizeStream -> 静默退回整包 optimize（老版本不能因此不能用）", async () => {
        let called = 0;
        const { w: w2 } = load({ H3Api: {
            async optimize() { called += 1; return { status: 200, body: { ok: true, prompt: "整包" } }; },
        } });
        const r = await w2.optCallStream({ prompt: "x" }, "标题", null);
        assert.strictEqual(called, 1, "没退回整包调用");
        assert.strictEqual(r.body.prompt, "整包");
        assert.ok(!w2.document.querySelector(".h3d-opt-prog"), "降级后不该留进度浮层");
    });

    await ta("流式不可用（抛「不支持流式」）-> 同样退回整包", async () => {
        let called = 0;
        const { w: w2 } = load({ H3Api: {
            async optimizeStream() { throw new Error("当前环境不支持流式响应（拿不到 ReadableStream）"); },
            async optimize() { called += 1; return { status: 200, body: { ok: true, prompt: "整包2" } }; },
        } });
        const r = await w2.optCallStream({ prompt: "x" }, "标题", null);
        assert.strictEqual(called, 1, "没退回整包调用");
        assert.strictEqual(r.body.prompt, "整包2");
    });

    await ta("后端返回 ok:false -> 原样返回（错误交给调用方的 alert）", async () => {
        const { w: w2 } = load({ H3Api: {
            async optimizeStream() {
                return { status: 200, body: { ok: false, code: "OPTIMIZE_FAILED", message: "炸了" } };
            },
        } });
        const r = await w2.optCallStream({ prompt: "x" }, "标题", null);
        assert.strictEqual(r.body.ok, false);
        assert.strictEqual(r.body.message, "炸了");
        assert.ok(!w2.document.querySelector(".h3d-opt-prog"), "失败后浮层该撤掉（错误由弹窗说）");
    });

    await ta("真异常（非降级场景）继续往上抛", async () => {
        const { w: w2 } = load({ H3Api: {
            async optimizeStream() { throw new Error("流式请求发不出去（网络断了）"); },
        } });
        let err = null;
        try { await w2.optCallStream({ prompt: "x" }, "标题", null); } catch (e) { err = e; }
        assert.ok(err && /网络断了/.test(err.message), "该把异常抛给调用方");
        assert.ok(!w2.document.querySelector(".h3d-opt-prog"), "异常后浮层该撤掉");
    });

    console.log("\n== 4 optCallStream：指定别的方法（扩写+优化） ==");
    await ta("opts.stream/fallback 生效 -> 调的是 expandOptimizeStream", async () => {
        let used = "";
        const { w: w2 } = load({ H3Api: {
            async expandOptimizeStream(body, onEvent) {
                used = "stream";
                onEvent({ type: "progress", stage: "expand", phase: "writing", content_chars: 400 });
                return { status: 200, body: { ok: true, prompt: "成品" }, events: [] };
            },
            async expandOptimize() { used = "fallback"; return { status: 200, body: { ok: true } }; },
        } });
        const r = await w2.optCallStream({ prompt: "x" }, "标题", null,
            { stream: "expandOptimizeStream", fallback: "expandOptimize" });
        assert.strictEqual(used, "stream");
        assert.strictEqual(r.body.prompt, "成品");
    });

    await ta("缺 expandOptimizeStream -> 退回 expandOptimize（老后端不能因此不能用）", async () => {
        let called = 0;
        const { w: w2 } = load({ H3Api: {
            async expandOptimize() { called += 1; return { status: 200, body: { ok: true, prompt: "整包" } }; },
        } });
        const r = await w2.optCallStream({ prompt: "x" }, "标题", null,
            { stream: "expandOptimizeStream", fallback: "expandOptimize" });
        assert.strictEqual(called, 1, "没退回 expandOptimize");
        assert.strictEqual(r.body.prompt, "整包");
    });

    await ta("busy 文案映射把按钮改成「扩写中…」（用户不用猜现在在干嘛）", async () => {
        const { w: w2 } = load({ H3Api: {
            async expandOptimizeStream(body, onEvent) {
                onEvent({ type: "progress", stage: "expand", phase: "writing", content_chars: 100 });
                return { status: 200, body: { ok: true, prompt: "P" }, events: [] };
            },
        } });
        const btn = w2.document.createElement("button");
        btn.disabled = true;
        btn.textContent = "✨ AI扩写+优化";
        await w2.optCallStream({ prompt: "x" }, "标题", { btn },
            { stream: "expandOptimizeStream", fallback: "expandOptimize",
              busy: { expand: "扩写中…", thinking: "优化中…", writing: "优化中…" } });
        assert.strictEqual(btn.textContent, "扩写中…", "按钮文案没跟着阶段走");
    });

    console.log("\n== 5 多段优化：「第 i/N 段」 ==");

    await ta("N 段串行：整条进度按段切分，第 2 段从 1/N 附近起且不回退", async () => {
        const p = w.optProgressStart("总提示词框 · 优化 3 段");
        const ev = (o) => p.update(Object.assign({ type: "progress" }, o));
        ev({ seg_no: 1, total: 3, phase: "thinking", reasoning_chars: 0, content_chars: 0 });
        const a = pct(w);
        ev({ seg_no: 1, total: 3, phase: "writing", reasoning_chars: 900, content_chars: 2000 });
        const b = pct(w);
        assert.ok(b > a, `第 1 段内该推进：${a} -> ${b}`);
        assert.ok(b <= 100 / 3 + 0.5, `第 1 段不该越过 1/3 太多：${b}`);
        ev({ seg_no: 2, total: 3, phase: "thinking", reasoning_chars: 0, content_chars: 0 });
        const c = pct(w);
        assert.ok(c >= b - 0.5, `换段不许回退：${b} -> ${c}`);
        assert.ok(c >= 100 / 3 - 1 && c < 100 / 3 + 3, `第 2 段起点该在 1/3 附近：${c}`);
        assert.ok(/第 2\/3 段/.test(meta(w)), "文案没带段序号：" + meta(w));
        p.close();
    });

    await ta("换段后段内进度从该段起点重算（不继承上一段的字数）", async () => {
        const p = w.optProgressStart("t");
        const ev = (o) => p.update(Object.assign({ type: "progress" }, o));
        ev({ seg_no: 1, total: 2, phase: "writing", reasoning_chars: 500, content_chars: 3000 });
        const before = pct(w);
        assert.ok(before > 40, "第 1 段该跑过 40%：" + before);
        ev({ seg_no: 2, total: 2, phase: "thinking", reasoning_chars: 0, content_chars: 0 });
        const s = pct(w);
        assert.ok(s < before + 8, `第 2 段刚开头不该跳一大格：${before} -> ${s}`);
        assert.ok(/推理 0 字/.test(meta(w)), "段内字数没跟着新段走：" + meta(w));
        p.close();
    });

    await ta("段文案：撰写/思考两种阶段都带「第 i/N 段」", async () => {
        const p = w.optProgressStart("t");
        p.update({ type: "progress", seg_no: 1, total: 4, phase: "writing", content_chars: 800 });
        assert.ok(/第 1\/4 段 · 撰写中/.test(meta(w)), "撰写文案不对：" + meta(w));
        p.update({ type: "progress", seg_no: 3, total: 4, phase: "thinking", reasoning_chars: 700 });
        assert.ok(/第 3\/4 段 · 思考中/.test(meta(w)), "思考文案不对：" + meta(w));
        p.close();
    });

    await ta("只有 1 段真跑时不切多段模式（单段曲线更准）", async () => {
        const p = w.optProgressStart("t");
        p.update({ type: "progress", seg_no: 1, total: 1, phase: "thinking", reasoning_chars: 100 });
        assert.ok(!/第 1\/1 段/.test(meta(w)), "total=1 不该显示「第 1/1 段」：" + meta(w));
        assert.ok(/思考中/.test(meta(w)), "该走单段曲线：" + meta(w));
        p.close();
    });

    await ta("多段走 optCallStream：路由到 optimizeMultiStream", async () => {
        let called = 0;
        const { w: w2 } = load({ H3Api: {
            async optimizeMultiStream(body, onEvent) {
                called += 1;
                onEvent({ type: "progress", seg_no: 1, total: 2, phase: "thinking", reasoning_chars: 5 });
                return { status: 200, body: { ok: true, segments: [] }, events: [] };
            },
        } });
        const r = await w2.optCallStream({ segments: [] }, "标题", null,
            { stream: "optimizeMultiStream", fallback: "optimizeMulti" });
        assert.strictEqual(called, 1, "没走 optimizeMultiStream");
        assert.strictEqual(r.body.ok, true);
    });

    await ta("多段流式不存在时静默退回整包 optimizeMulti", async () => {
        let called = 0;
        const { w: w2 } = load({ H3Api: {
            async optimizeMulti() { called += 1; return { status: 200, body: { ok: true, segments: [1] } }; },
        } });
        const r = await w2.optCallStream({ segments: [] }, "标题", null,
            { stream: "optimizeMultiStream", fallback: "optimizeMulti" });
        assert.strictEqual(called, 1, "没退回 optimizeMulti");
        assert.deepStrictEqual(r.body.segments, [1]);
    });

    results.forEach((r) => console.log(r));
    if (errors.length) { console.log("\n页面错误: " + errors.join(" | ")); ok = false; }
    console.log(ok ? "\nopt_progress_check 全部通过" : "\nopt_progress_check 失败");
    process.exit(ok ? 0 : 1);
})();
