/* AI 优化设置「保存」的持久化守卫（2026-10-07）。
 *
 * 用户反馈：填好自己的 API（服务商 / 地址 / Key）→ 保存 → **重新进入又要再设一遍**。
 * 根因：「保存」只写进**当前工作流**的「导演台状态」控件 —— 换一份工作流（新建 /
 * 导入别人的 / 载入默认模板）配置就不在里面，而那正是"重新进入"时打开的东西。
 *
 * 修法：保存**双写**（工作流 + 服务端 optimizer.local.json，已 gitignore）；
 * 面板打开时若工作流里没存过配置，就用服务端那份全局配置回填。
 *
 * 这里钉住五件事：
 *   ① 保存确实会 POST /h3chain/optimizer-config（不是只写本地就完事）；
 *   ② 工作流没存过 → 用服务端全局值回填（服务商 / 地址 / 模型 / 规则 / 超时）；
 *   ③ 工作流存过 → **不被**全局值覆盖（存档优先，绝不静默改用户的值）；
 *   ④ 服务端存着**当前服务商**的 Key 时不再拦人；没存过仍要拦；
 *   ⑤ 写不进工作流时要**报出来**（alert），不再静默 —— 那正是"点了保存却像没存"。
 *
 * 用法：node tests/js/opt_global_save_check.js
 */
const assert = require("assert");
const { load, mkNode } = require("./_harness");

let ok = true;
const results = [];
function t(name, fn) {
    try { fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}
async function ta(name, fn) {
    try { await fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}

/* ---- 面板 DOM 取值小工具 ---- */
const rowsOf = (dlg) => [...dlg.querySelectorAll(".h3d-opt-row")];
const rowOf = (dlg, label) => rowsOf(dlg).find(
    (r) => (r.querySelector("span")?.textContent || "").includes(label));
const ctrlOf = (dlg, label) => {
    const r = rowOf(dlg, label);
    if (!r) throw new Error("面板上没有「" + label + "」这一行");
    return r.querySelector("input, select, textarea");
};
const dlgOf = (w) => w.document.querySelector(".h3d-opt-dialog");

/* 服务端**全局配置**（optimizer.local.json 的可回显部分，不含任何 Key） */
const GLOBAL_CFG = {
    provider: "dashscope",
    api_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: "qwen-vl-max",
    protocol: "openai",
    rule_file: "zh",
    timeout: 600,
};

/* 后端桩：getOptimizerConfig 给配置，setOptimizerConfig 记录调用 */
function backend(opts) {
    opts = opts || {};
    const calls = opts.calls || [];
    return {
        calls,
        H3Api: {
            async getOptimizerConfig() {
                return { body: Object.assign({
                    ok: true, has_default_key: false, has_api_key: false, api_key: "",
                    global_keys: [], global: null, models: [], mmproj_models: [],
                }, opts.cfg || {}) };
            },
            async setOptimizerConfig(payload) {
                calls.push(payload);
                return { body: Object.assign(
                    { ok: true, saved: Object.keys(payload) }, opts.reply || {}) };
            },
        },
    };
}

(async function main() {
    console.log("\n== 1 保存 = 双写（工作流 + 服务端） ==");
    const a = backend({});
    const { w: w1, errors: e1 } = load({ H3Api: a.H3Api });
    const n1 = mkNode({});
    await ta("点保存会 POST 到服务端，并带上服务商/模型/Key", async () => {
        await w1.openOptSettings(n1);
        const dlg = dlgOf(w1);
        assert.ok(dlg, "面板没打开");
        const model = rowOf(dlg, "模型").querySelector("input");
        model.value = "glm-5.3-flash";
        model.dispatchEvent(new w1.Event("input"));
        const key = ctrlOf(dlg, "API Key");
        key.value = "sk-live";
        key.dispatchEvent(new w1.Event("input"));
        await dlg.querySelector(".h3d-opt-save").onclick();
        assert.strictEqual(a.calls.length, 1, "保存没调服务端（那就还是只写工作流）");
        assert.strictEqual(a.calls[0].model, "glm-5.3-flash");
        assert.strictEqual(a.calls[0].api_key, "sk-live");
        /* 本地那份也要写（同一份工作流内立刻生效） */
        assert.strictEqual(w1.optGetSettings(n1).model, "glm-5.3-flash");
    });

    console.log("\n== 2 工作流没存过配置 → 用服务端全局值回填 ==");
    const b = backend({ cfg: { global: GLOBAL_CFG, global_keys: ["dashscope"] } });
    const { w: w2, errors: e2 } = load({ H3Api: b.H3Api });
    await ta("面板显示服务端保存过的服务商 / 模型 / 超时 / 规则", async () => {
        await w2.openOptSettings(mkNode({}));
        const dlg = dlgOf(w2);
        assert.strictEqual(rowOf(dlg, "服务商").querySelector("select").value, "dashscope",
            "服务商没回填：新工作流仍会落到默认的 glm");
        assert.strictEqual(rowOf(dlg, "模型").querySelector("input").value, "qwen-vl-max");
        assert.strictEqual(ctrlOf(dlg, "请求超时").value, "600");
        assert.strictEqual(ctrlOf(dlg, "提示词规则").value, "zh");
    });

    console.log("\n== 3 工作流存过配置 → 不被全局值覆盖（存档优先） ==");
    const c = backend({ cfg: { global: GLOBAL_CFG, global_keys: ["dashscope"] } });
    const { w: w3, errors: e3 } = load({ H3Api: c.H3Api });
    await ta("面板显示工作流自己的 openai 配置，不是服务端的 dashscope", async () => {
        await w3.openOptSettings(mkNode({
            optimizer: { provider: "openai", model: "gpt-4.1-mini",
                         api_url: "https://api.openai.com/v1", cfg_ver: 3,
                         api_keys: { openai: "sk-x" } },
        }));
        const dlg = dlgOf(w3);
        assert.strictEqual(rowOf(dlg, "服务商").querySelector("select").value, "openai");
        assert.strictEqual(rowOf(dlg, "模型").querySelector("input").value, "gpt-4.1-mini");
    });

    console.log("\n== 4 服务端存着当前服务商的 Key → 不再拦人 ==");
    const d = backend({ cfg: { global: GLOBAL_CFG, global_keys: ["dashscope"] } });
    const { w: w4, errors: e4 } = load({ H3Api: d.H3Api });
    await ta("选百炼且 Key 留空 → 放行（服务端有兜底）", async () => {
        const st = w4.optGetSettings(mkNode({
            optimizer: { provider: "dashscope", model: "qwen-vl-max", cfg_ver: 3 },
        }));
        assert.strictEqual(await w4.optNeedsSetup(mkNode({}), st), false);
    });
    const e = backend({ cfg: { global_keys: [] } });
    const { w: w5, errors: e5 } = load({ H3Api: e.H3Api });
    await ta("服务端没有这个服务商的 Key → 仍然拦", async () => {
        const st = w5.optGetSettings(mkNode({
            optimizer: { provider: "openai", model: "gpt-4.1-mini", cfg_ver: 3 },
        }));
        assert.strictEqual(await w5.optNeedsSetup(mkNode({}), st), true);
    });

    console.log("\n== 5 Key 绝不回显明文 ==");
    const f = backend({ cfg: { global: GLOBAL_CFG, global_keys: ["dashscope"],
                               has_default_key: true, api_key: "sk-should-not-show" } });
    const { w: w6, errors: e6 } = load({ H3Api: f.H3Api });
    await ta("Key 框为空，只给「已保存过」的占位提示", async () => {
        await w6.openOptSettings(mkNode({}));
        const dlg = dlgOf(w6);
        const key = ctrlOf(dlg, "API Key");
        assert.strictEqual(key.value, "", "Key 明文被回显了（会随工作流被导出分享）");
        assert.ok(/留空即用/.test(key.placeholder), "没提示已保存过：" + key.placeholder);
        assert.ok(!/sk-should-not-show/.test(dlg.textContent || ""), "面板里出现了 Key 明文");
    });

    console.log("\n== 6 写不进工作流要报出来（以前是静默的） ==");
    const alerts = [];
    const g = backend({});
    const { w: w7, errors: e7 } = load({ H3Api: g.H3Api, onAlert: (m) => alerts.push(m) });
    await ta("节点没有「导演台状态」控件 → alert 提示，且服务端仍已保存", async () => {
        const bare = {
            id: 9, type: "H3SeamlessChainSampler", widgets: [],
            setDirtyCanvas() {}, graph: { change() {} }, getSize() { return [300, 400]; },
        };
        await w7.openOptSettings(bare);
        const dlg = dlgOf(w7);
        const key = ctrlOf(dlg, "API Key");
        key.value = "sk-live";
        key.dispatchEvent(new w7.Event("input"));
        await dlg.querySelector(".h3d-opt-save").onclick();
        assert.strictEqual(g.calls.length, 1, "服务端没收到保存请求");
        assert.ok(alerts.some((m) => /没有完全保存/.test(m)),
            "静默失败了，用户以为存上了：" + JSON.stringify(alerts));
        assert.ok(alerts.some((m) => /导演台状态/.test(m)),
            "提示没说清是哪一半没存：" + JSON.stringify(alerts));
    });

    console.log("\n== 7 服务端写盘失败也要说出来 ==");
    const alerts2 = [];
    const h = backend({ reply: { ok: false, message: "磁盘只读" } });
    const { w: w8, errors: e8 } = load({ H3Api: h.H3Api, onAlert: (m) => alerts2.push(m) });
    await ta("服务端回 ok=false → alert 里带上原因", async () => {
        const n8 = mkNode({});
        await w8.openOptSettings(n8);
        const dlg = dlgOf(w8);
        const key = ctrlOf(dlg, "API Key");
        key.value = "sk-live";
        key.dispatchEvent(new w8.Event("input"));
        await dlg.querySelector(".h3d-opt-save").onclick();
        assert.ok(alerts2.some((m) => /磁盘只读/.test(m)),
            "没把服务端失败原因报出来：" + JSON.stringify(alerts2));
    });

    results.forEach((r) => console.log(r));
    const allErr = [...e1, ...e2, ...e3, ...e4, ...e5, ...e6, ...e7, ...e8];
    if (allErr.length) { console.log("\n页面错误: " + allErr.join(" | ")); ok = false; }
    console.log(ok ? "\nopt_global_save_check 全部通过" : "\nopt_global_save_check 失败");
    process.exit(ok ? 0 : 1);
})();
