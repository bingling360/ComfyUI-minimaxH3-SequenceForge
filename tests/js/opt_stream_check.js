/* h3_api.js 的 SSE 流式读取（optimizeStream）回归守卫。
 *
 * 背景（2026-09-19）：用户报「缺了个进度条」。进度条要真数据，就得让优化走
 * /h3chain/optimize_stream（SSE）。而**手写 SSE 解析最容易错的地方是分帧**：
 * `reader.read()` 给的是任意切分的字节块 —— 一帧可能被劈成两个 chunk，
 * 一个 chunk 也可能含多帧，还可能被中间代理改写成 CRLF。这三种情况在真机上
 * 都是偶发的（长输出时概率上升），手点几次测不出来，只有钉在测试里才守得住。
 *
 * 用法：node tests/js/opt_stream_check.js
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");
const ROOT = path.resolve(__dirname, "..", "..");

let ok = true;
const results = [];
async function ta(name, fn) {
    try { await fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}

/** 装一个只跑 h3_api.js 的 jsdom 窗口；fetch 返回给定的分块流。 */
function loadApi(opts = {}) {
    const dom = new JSDOM("<!doctype html><html><body></body></html>", {
        runScripts: "outside-only", url: "http://127.0.0.1:8188/",
    });
    const w = dom.window;
    /* jsdom 不带 Streams / TextDecoder，从 node 全局借一份 */
    w.ReadableStream = globalThis.ReadableStream;
    w.TextDecoder = globalThis.TextDecoder;
    w.TextEncoder = globalThis.TextEncoder;
    const enc = new TextEncoder();
    const calls = [];
    w.fetch = async (p, init) => {
        calls.push({ path: String(p), init });
        if (opts.fetch) return opts.fetch(p, init);
        const chunks = opts.chunks || [];
        let i = 0;
        const body = new ReadableStream({
            pull(c) {
                if (i >= chunks.length) { c.close(); return; }
                c.enqueue(enc.encode(chunks[i++]));
            },
        });
        return { ok: true, status: 200, body };
    };
    w.eval(fs.readFileSync(path.join(ROOT, "web", "h3_api.js"), "utf8"));
    return { w, calls };
}

const frame = (kind, obj) => `event: ${kind}\ndata: ${JSON.stringify(obj)}\n\n`;

(async function main() {
    console.log("\n== 1 基本分帧 ==");
    await ta("多帧一次到达 -> 全部解析，body 是最后一帧", async () => {
        const { w } = loadApi({ chunks: [
            frame("progress", { phase: "thinking", reasoning_chars: 10, content_chars: 0 }),
            frame("progress", { phase: "writing", reasoning_chars: 10, content_chars: 88 }),
            frame("done", { ok: true, prompt: "结果" }),
        ] });
        const seen = [];
        const r = await w.H3Api.optimizeStream({ a: 1 }, (e) => seen.push(e));
        assert.strictEqual(seen.length, 3, "onEvent 应逐帧回调");
        assert.strictEqual(seen[0].type, "progress");
        assert.strictEqual(seen[1].phase, "writing");
        assert.strictEqual(r.body.ok, true);
        assert.strictEqual(r.body.prompt, "结果");
        assert.strictEqual(r.events.length, 3);
    });

    await ta("一帧被劈成两个 chunk -> 仍能拼出完整帧", async () => {
        const whole = frame("progress", { phase: "thinking", reasoning_chars: 42 })
            + frame("done", { ok: true, prompt: "P" });
        const cut = Math.floor(whole.length / 2);      // 故意切在帧中间
        const { w } = loadApi({ chunks: [whole.slice(0, cut), whole.slice(cut)] });
        const r = await w.H3Api.optimizeStream({}, () => {});
        assert.strictEqual(r.events.length, 2, "劈开的帧应该被拼回来");
        assert.strictEqual(r.events[0].reasoning_chars, 42);
        assert.strictEqual(r.body.prompt, "P");
    });

    await ta("逐字节喂（极端切分）也不丢帧", async () => {
        const whole = frame("progress", { content_chars: 7 }) + frame("done", { ok: true, prompt: "X" });
        const chunks = whole.split("").map((c) => c);
        const { w } = loadApi({ chunks });
        const r = await w.H3Api.optimizeStream({}, () => {});
        assert.strictEqual(r.events.length, 2);
        assert.strictEqual(r.body.ok, true);
    });

    await ta("CRLF 帧（中间隔代理）也要认", async () => {
        const crlf = (k, o) => `event: ${k}\r\ndata: ${JSON.stringify(o)}\r\n\r\n`;
        const { w } = loadApi({ chunks: [
            crlf("progress", { phase: "writing", content_chars: 5 }),
            crlf("done", { ok: true, prompt: "CRLF" }),
        ] });
        const r = await w.H3Api.optimizeStream({}, () => {});
        assert.strictEqual(r.events.length, 2, "CRLF 分隔符没被识别");
        assert.strictEqual(r.body.prompt, "CRLF");
    });

    await ta("服务端没以空行收尾 -> 尾帧仍要收下", async () => {
        const { w } = loadApi({ chunks: ['event: done\ndata: {"ok":true,"prompt":"尾"}'] });
        const r = await w.H3Api.optimizeStream({}, () => {});
        assert.strictEqual(r.body.prompt, "尾");
    });

    console.log("\n== 2 错误路径 ==");
    await ta("error 帧 -> body.ok=false 且带 code/message", async () => {
        const { w } = loadApi({ chunks: [
            frame("progress", { phase: "thinking" }),
            frame("error", { ok: false, code: "OPTIMIZE_FAILED", message: "LLM 返回 HTTP 400" }),
        ] });
        const r = await w.H3Api.optimizeStream({}, () => {});
        assert.strictEqual(r.body.ok, false);
        assert.strictEqual(r.body.code, "OPTIMIZE_FAILED");
        assert.ok(/HTTP 400/.test(r.body.message));
    });

    await ta("一帧都没有（流被掐断）-> 不抛异常，给 EMPTY_STREAM", async () => {
        const { w } = loadApi({ chunks: [] });
        const r = await w.H3Api.optimizeStream({}, () => {});
        assert.strictEqual(r.body.ok, false);
        assert.strictEqual(r.body.code, "EMPTY_STREAM");
    });

    await ta("坏帧（半截 JSON）被跳过，不影响后续帧", async () => {
        const { w } = loadApi({ chunks: [
            'event: progress\ndata: {"phase":"thin\n\n',
            frame("done", { ok: true, prompt: "继续" }),
        ] });
        const r = await w.H3Api.optimizeStream({}, () => {});
        assert.strictEqual(r.body.prompt, "继续");
        assert.strictEqual(r.events.length, 1, "坏帧不该进 events");
    });

    await ta("HTTP 非 200 -> 返回 status 与 body，不抛异常", async () => {
        const { w } = loadApi({ fetch: async () => ({
            ok: false, status: 500, json: async () => ({ ok: false, message: "炸了" }),
        }) });
        const r = await w.H3Api.optimizeStream({}, () => {});
        assert.strictEqual(r.status, 500);
        assert.strictEqual(r.body.message, "炸了");
    });

    await ta("没有 body（环境不支持流式）-> 抛错，供调用方降级", async () => {
        const { w } = loadApi({ fetch: async () => ({ ok: true, status: 200, body: null }) });
        let err = null;
        try { await w.H3Api.optimizeStream({}, () => {}); } catch (e) { err = e; }
        assert.ok(err, "应该抛错");
        assert.ok(/不支持流式/.test(err.message), "错误信息要能让调用方识别并降级：" + err.message);
    });

    await ta("取消（AbortError）-> 原样抛出，由调用方识别", async () => {
        const { w } = loadApi({ fetch: async () => {
            const e = new Error("aborted");
            e.name = "AbortError";
            throw e;
        } });
        let err = null;
        try { await w.H3Api.optimizeStream({}, () => {}); } catch (e) { err = e; }
        assert.ok(err && err.name === "AbortError", "AbortError 不该被包装成别的错误");
    });

    await ta("请求体真的发出去了（POST + JSON + Accept 头）", async () => {
        const { w, calls } = loadApi({ chunks: [frame("done", { ok: true, prompt: "x" })] });
        await w.H3Api.optimizeStream({ prompt: "你好", config: { model: "glm-4.6v" } }, () => {});
        assert.strictEqual(calls.length, 1);
        assert.strictEqual(calls[0].path, "/h3chain/optimize_stream");
        assert.strictEqual(calls[0].init.method, "POST");
        assert.ok(/text\/event-stream/.test(calls[0].init.headers.Accept));
        assert.strictEqual(JSON.parse(calls[0].init.body).prompt, "你好");
    });

    results.forEach((r) => console.log(r));
    console.log(ok ? "\nopt_stream_check 全部通过" : "\nopt_stream_check 失败");
    process.exit(ok ? 0 : 1);
})();
