/* 优化设置默认值 / 老配置迁移 回归守卫。
 *
 * 背景（2026-09-19）：默认服务商是 RunningHub + 预设占位模型，开箱调不通
 * （用户看到的是 "LLM 请求失败：The read operation timed out"）。改成默认
 * 智谱 GLM-4.6V 之后，**老工作流里已经存过档的 optimizer 配置**必须一起迁移，
 * 否则"改了默认值"对存过档的用户毫无作用；但迁移又不能把用户自己配过的服务商
 * 冲掉。这里两个方向都钉住，最后把设置面板真开一次、验面板上的值。
 *
 * 注意：h3_director.js 里的 `const OPT_PROVIDERS` 是**词法声明**，jsdom 的
 * w.eval 拿不到（只有 `function` 声明会挂到 window 上）。所以服务商列表
 * 一律通过 openOptSettings 的 DOM 结果来断言，不去读那个常量。
 *
 * 用法：node tests/js/opt_provider_default_check.js
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
    if (!r) throw new Error(`面板上没有「${label}」这一行`);
    return r.querySelector("input, select, textarea") || r.querySelector(".h3d-opt-inline")?.firstChild;
};
const checkOf = (dlg, label) => {
    const lb = [...dlg.querySelectorAll(".h3d-opt-checks label")]
        .find((l) => (l.textContent || "").includes(label));
    if (!lb) throw new Error(`面板上没有「${label}」勾选项`);
    return lb.querySelector("input");
};

(async function main() {
    const { w, errors } = load({});

    console.log("\n== 1 默认设置 ==");
    t("默认服务商是 glm", () => {
        const d = w.optDefaultSettings();
        assert.strictEqual(d.provider, "glm");
        assert.strictEqual(d.model, "glm-5.3-flashx");
        assert.strictEqual(d.api_url, "https://open.bigmodel.cn/api/paas/v4");
        assert.strictEqual(d.protocol, "openai");
    });
    t("默认超时 300（旧值 120 是读操作超时的根因）", () => {
        assert.strictEqual(w.optDefaultSettings().timeout, 300);
    });
    t("默认关思考（对 glm-5.3-flashx 会被翻译成最低强度 low）", () => {
        assert.strictEqual(w.optDefaultSettings().thinking, "disabled");
        assert.strictEqual(w.optDefaultSettings().reasoning_effort, "");
    });
    t("默认配置带 cfg_ver", () => {
        assert.strictEqual(w.optDefaultSettings().cfg_ver, 3);
    });

    console.log("\n== 2 老配置迁移 ==");
    t("没动过的旧默认配置 -> 迁到 glm", () => {
        const got = w.optMigrateSettings({
            provider: "runninghub", model: "openai/gpt-5.6-sol",
            api_url: "https://www.runninghub.cn/openapi/v2", api_keys: {},
        });
        assert.strictEqual(got.provider, "glm");
        assert.strictEqual(got.model, "glm-5.3-flashx");
        assert.strictEqual(got.api_url, "https://open.bigmodel.cn/api/paas/v4");
        assert.strictEqual(got.cfg_ver, 3);
    });
    t("v2 默认（glm + glm-4.6v，没挑过型号）-> 升到 glm-5.3-flashx", () => {
        const got = w.optMigrateSettings({
            provider: "glm", model: "glm-4.6v", cfg_ver: 2,
            api_url: "https://open.bigmodel.cn/api/paas/v4",
            api_keys: { glm: "" },
        });
        assert.strictEqual(got.model, "glm-5.3-flashx");
        assert.strictEqual(got.cfg_ver, 3);
    });
    t("v2 里用户自己挑过型号 -> 不动（只升版本号）", () => {
        const got = w.optMigrateSettings({
            provider: "glm", model: "glm-4.5v", cfg_ver: 2, api_keys: { glm: "sk-real" },
        });
        assert.strictEqual(got.model, "glm-4.5v", "用户挑的型号不能被默认值冲掉");
        assert.strictEqual(got.cfg_ver, 3);
    });
    t("v2 里用户换过服务商 -> 不动", () => {
        const got = w.optMigrateSettings({
            provider: "dashscope", model: "qwen-vl-max", cfg_ver: 2,
            api_keys: { dashscope: "sk-u" },
        });
        assert.strictEqual(got.provider, "dashscope");
        assert.strictEqual(got.model, "qwen-vl-max");
    });
    t("用户填过 Key 的旧配置 -> 不迁（那是他在用的服务商）", () => {
        const got = w.optMigrateSettings({
            provider: "runninghub", model: "openai/gpt-5.6-sol",
            api_keys: { runninghub: "sk-real" },
        });
        assert.strictEqual(got.provider, "runninghub");
        assert.strictEqual(got.model, "openai/gpt-5.6-sol");
        assert.strictEqual(got.cfg_ver, 3);
    });
    t("用户改过模型的旧配置 -> 不迁", () => {
        const got = w.optMigrateSettings({
            provider: "runninghub", model: "my/custom-model", api_keys: {},
        });
        assert.strictEqual(got.provider, "runninghub");
        assert.strictEqual(got.model, "my/custom-model");
    });
    t("已经是 v3 的配置 -> 原样返回（不重复迁移）", () => {
        const src = { provider: "openai", model: "gpt-4.1-mini", cfg_ver: 3 };
        assert.strictEqual(w.optMigrateSettings(src), src);
    });
    t("没有配置 -> null（交给 optDefaultSettings 兜底）", () => {
        assert.strictEqual(w.optMigrateSettings(null), null);
        assert.strictEqual(w.optMigrateSettings(undefined), null);
    });

    console.log("\n== 3 optGetSettings 组装 ==");
    t("空 ds -> 全默认（glm）", () => {
        const s = w.optGetSettings(mkNode({}));
        assert.strictEqual(s.provider, "glm");
        assert.strictEqual(s.model, "glm-5.3-flashx");
    });
    t("旧 ds -> 迁移后的 glm", () => {
        const s = w.optGetSettings(mkNode({
            optimizer: { provider: "runninghub", model: "openai/gpt-5.6-sol", api_keys: {} },
        }));
        assert.strictEqual(s.provider, "glm");
        assert.strictEqual(s.model, "glm-5.3-flashx");
    });
    t("用户自己的配置原样保留，缺的新字段补默认值", () => {
        const s = w.optGetSettings(mkNode({
            optimizer: { provider: "dashscope", model: "qwen-vl-max", api_key: "sk-u",
                         api_keys: { dashscope: "sk-u" } },
        }));
        assert.strictEqual(s.provider, "dashscope");
        assert.strictEqual(s.model, "qwen-vl-max");
        assert.strictEqual(s.api_key, "sk-u");
        assert.strictEqual(s.timeout, 300);
        assert.strictEqual(s.thinking, "disabled");
    });

    console.log("\n== 4 设置面板实际显示（无内置 Key） ==");
    await ta("面板打开且默认选中 glm", async () => {
        await w.openOptSettings(mkNode({}));
        const dlg = w.document.querySelector(".h3d-opt-dialog");
        assert.ok(dlg, "面板没打开");
        const prov = rowOf(dlg, "服务商").querySelector("select");
        assert.strictEqual(prov.value, "glm");
        assert.strictEqual(prov.options[0].value, "glm", "glm 应该排在下拉第一位");
        assert.ok([...prov.options].some((o) => o.value === "runninghub"),
            "旧服务商预设不能删");
    });
    await ta("模型行显示 glm-5.3-flashx 且对所有服务商可见", async () => {
        const dlg = w.document.querySelector(".h3d-opt-dialog");
        const mrow = rowOf(dlg, "模型");
        assert.ok(!mrow.classList.contains("h3d-opt-hidden"), "模型行被隐藏了");
        assert.strictEqual(mrow.querySelector("input").value, "glm-5.3-flashx");
    });
    await ta("超时行显示 300", async () => {
        const dlg = w.document.querySelector(".h3d-opt-dialog");
        assert.strictEqual(ctrlOf(dlg, "请求超时").value, "300");
    });
    await ta("思考强度默认「关闭思考」（旧版是一个「深度思考」勾选框）", async () => {
        const dlg = w.document.querySelector(".h3d-opt-dialog");
        assert.strictEqual(ctrlOf(dlg, "思考强度").value, "disabled");
    });
    await ta("最大输出 token 上限 32768（旧上限 8192 会把思考模型的正文截空）", async () => {
        const dlg = w.document.querySelector(".h3d-opt-dialog");
        const mt = ctrlOf(dlg, "最大输出 token");
        assert.strictEqual(mt.max, "32768");
        assert.strictEqual(mt.value, "8192", "默认值应与后端一致（8192）");
    });
    await ta("拿不到能力表时只给 关闭/开启（不假装支持强度分级）", async () => {
        const dlg = w.document.querySelector(".h3d-opt-dialog");
        const sel = ctrlOf(dlg, "思考强度");
        assert.deepStrictEqual([...sel.options].map((o) => o.value), ["disabled", "enabled"]);
    });
    await ta("没有内置 Key 时占位符是 sk-…", async () => {
        const dlg = w.document.querySelector(".h3d-opt-dialog");
        assert.strictEqual(ctrlOf(dlg, "API Key").placeholder, "sk-…");
    });
    await ta("切到本地模式时隐藏 API 相关行", async () => {
        const dlg = w.document.querySelector(".h3d-opt-dialog");
        const modeSel = rowOf(dlg, "优化模式").querySelector("select");
        modeSel.value = "local";
        modeSel.dispatchEvent(new w.Event("change"));
        assert.ok(rowOf(dlg, "API Key").classList.contains("h3d-opt-hidden"));
        assert.ok(rowOf(dlg, "请求超时").classList.contains("h3d-opt-hidden"));
    });
    await ta("本地模式保留「思考强度」：开/关两档（本地靠 /no_think 软开关关思考）", async () => {
        const dlg = w.document.querySelector(".h3d-opt-dialog");
        assert.ok(!rowOf(dlg, "思考强度").classList.contains("h3d-opt-hidden"),
            "本地模式该保留思考强度：藏起来用户既看不见也改不了，只能被默认值支配");
        assert.deepStrictEqual([...ctrlOf(dlg, "思考强度").options].map((o) => o.value),
            ["disabled", "enabled"], "本地只有开/关两档（没有 low/high/max）");
    });

    console.log("\n== 5 思考强度：按型号能力重建选项 ==");
    /* 用户报的原始问题：选 glm-5.3-flash 这种型号时"有不同的思考强度，
     * 现在这样不能选择"。能力表（哪些型号强制思考 / 支持强度）由后端
     * /h3chain/optimizer-config 下发，前端只做前缀匹配 —— 这里就把三档形态
     * 都验一遍：四档 / 两档 / 整行隐藏。 */
    const GLM_CAPS = {
        ok: true, has_default_key: true, has_api_key: true, api_key: "",
        models: [], mmproj_models: [],
        providers: { glm: { url: "https://open.bigmodel.cn/api/paas/v4",
                            model: "glm-4.6v", protocol: "openai" } },
        glm_force_thinking: ["glm-5.3", "glm-4.7", "glm-4.5v"],
        glm_effort_prefixes: ["glm-5"],
        glm_effort_values: ["low", "high", "max"],
    };
    const { w: wc, errors: errorsC } = load({
        H3Api: { async getOptimizerConfig() { return { body: GLM_CAPS }; } },
    });
    const cNode = mkNode({});
    const cDlg = () => wc.document.querySelector(".h3d-opt-dialog");
    const cSet = (label, val) => {
        const inp = rowOf(cDlg(), label).querySelector("input");
        inp.value = val;
        inp.dispatchEvent(new wc.Event("input"));
        return inp;
    };
    await ta("面板能打开", async () => {
        await wc.openOptSettings(cNode);
        assert.ok(cDlg(), "面板没打开");
    });
    await ta("glm-5.3-flash -> 关闭/低/高/最高 四档可选", async () => {
        cSet("模型", "glm-5.3-flash");
        const sel = ctrlOf(cDlg(), "思考强度");
        assert.deepStrictEqual([...sel.options].map((o) => o.value),
            ["disabled", "low", "high", "max"], "glm-5.3-flash 应该能选强度档");
    });
    await ta("强制思考型号选「关闭」-> 界面说明会按 low 执行（别让人以为真关了）", async () => {
        const hint = cDlg().querySelector(".h3d-opt-hint");
        assert.ok(/始终思考/.test(hint.textContent), "缺强制思考说明：" + hint.textContent);
        assert.ok(/low/.test(hint.textContent), "没说明会翻译成 low：" + hint.textContent);
    });
    await ta("高强度 + 额度不足 -> 当场提示调大 max_tokens（空文本故障的预防）", async () => {
        const sel = ctrlOf(cDlg(), "思考强度");
        sel.value = "max";
        sel.dispatchEvent(new wc.Event("change"));
        const mt = ctrlOf(cDlg(), "最大输出 token");
        assert.strictEqual(mt.value, "8192");
        assert.ok(/16384/.test(cDlg().querySelector(".h3d-opt-hint").textContent),
            "没提示额度不足：" + cDlg().querySelector(".h3d-opt-hint").textContent);
        mt.value = "16384";
        mt.dispatchEvent(new wc.Event("input"));
        assert.ok(!/16384/.test(cDlg().querySelector(".h3d-opt-hint").textContent),
            "调够额度后不该还提示：" + cDlg().querySelector(".h3d-opt-hint").textContent);
    });
    await ta("glm-4.6v -> 只给 关闭/开启（该型号无强度分级）", async () => {
        cSet("模型", "glm-4.6v");
        const sel = ctrlOf(cDlg(), "思考强度");
        assert.deepStrictEqual([...sel.options].map((o) => o.value), ["disabled", "enabled"]);
        const hint = cDlg().querySelector(".h3d-opt-hint");
        assert.ok(/无强度分级/.test(hint.textContent), "应提示无强度分级：" + hint.textContent);
    });
    await ta("切到非智谱端点 -> 整行隐藏（后端对别家不下发这两个字段）", async () => {
        cSet("API URL", "https://api.openai.com/v1");
        assert.ok(rowOf(cDlg(), "思考强度").classList.contains("h3d-opt-hidden"), "非智谱端点该隐藏");
        cSet("API URL", "https://open.bigmodel.cn/api/paas/v4");
        assert.ok(!rowOf(cDlg(), "思考强度").classList.contains("h3d-opt-hidden"), "切回智谱该恢复");
    });
    await ta("选「最高强度」保存 -> thinking=enabled + reasoning_effort=max", async () => {
        cSet("模型", "glm-5.3-flash");
        const sel = ctrlOf(cDlg(), "思考强度");
        sel.value = "max";
        sel.dispatchEvent(new wc.Event("change"));
        cDlg().querySelector(".h3d-opt-save").onclick();
        const saved = wc.optGetSettings(cNode);
        assert.strictEqual(saved.model, "glm-5.3-flash");
        assert.strictEqual(saved.thinking, "enabled", "强度档本身就是开思考");
        assert.strictEqual(saved.reasoning_effort, "max");
    });
    await ta("重新打开后档位还原，改选「关闭」保存 -> effort 清空", async () => {
        await wc.openOptSettings(cNode);
        assert.strictEqual(ctrlOf(cDlg(), "思考强度").value, "max", "已保存的档位没还原");
        const sel = ctrlOf(cDlg(), "思考强度");
        sel.value = "disabled";
        sel.dispatchEvent(new wc.Event("change"));
        cDlg().querySelector(".h3d-opt-save").onclick();
        const saved = wc.optGetSettings(cNode);
        assert.strictEqual(saved.thinking, "disabled");
        assert.strictEqual(saved.reasoning_effort, "", "关掉思考要顺手清掉强度，否则残留档位会复活");
    });

    console.log("\n== 6 切换服务商：各家默认模型名互不串（回归） ==");
    /* 用户报的原始问题：来回切服务商后，切回智谱时模型框里留下的是**上一家**的
     * 默认模型名。根因是 provider.dataset.prev 在 change 里没有写回 —— 它永远是初始
     * 服务商（默认 glm），于是每次切换都把当前模型名记进 glm 的槽，智谱的记忆被
     * 后一家家的默认名逐次覆盖。其它服务商因为槽位从没被写过、每次回落预设默认名，
     * 反倒"看起来正常"，所以这个 bug 只在智谱身上显形。 */
    const { w: wp, errors: errorsP } = load({});
    const pNode = mkNode({});
    const pDlg = () => wp.document.querySelector(".h3d-opt-dialog");
    const provSel = () => rowOf(pDlg(), "服务商").querySelector("select");
    const modelInp = () => rowOf(pDlg(), "模型").querySelector("input");
    const switchTo = (v) => {
        const s = provSel();
        s.value = v;
        s.dispatchEvent(new wp.Event("change"));
    };
    await ta("面板打开：默认 glm / glm-5.3-flashx", async () => {
        await wp.openOptSettings(pNode);
        assert.ok(pDlg(), "面板没打开");
        assert.strictEqual(provSel().value, "glm");
        assert.strictEqual(modelInp().value, "glm-5.3-flashx");
    });
    await ta("切到 openai -> 落到该家默认模型", async () => {
        switchTo("openai");
        assert.strictEqual(modelInp().value, "gpt-4.1-mini");
    });
    await ta("再切 gemini -> 落到该家默认模型", async () => {
        switchTo("gemini");
        assert.strictEqual(modelInp().value, "gemini-2.5-flash");
    });
    await ta("切回 glm -> 仍是智谱自己的默认模型（不是上一家的）", async () => {
        switchTo("glm");
        assert.strictEqual(modelInp().value, "glm-5.3-flashx",
            "智谱的记忆槽被别家的模型名覆盖了");
    });
    await ta("再切 openai -> 回到它自己的模型", async () => {
        switchTo("openai");
        assert.strictEqual(modelInp().value, "gpt-4.1-mini");
    });
    await ta("手改模型后再切走切回 -> 记住的是手改值，不是预设名", async () => {
        const inp = modelInp();
        inp.value = "openai/gpt-5.6-sol";
        inp.dispatchEvent(new wp.Event("input"));
        switchTo("glm");
        switchTo("openai");
        assert.strictEqual(modelInp().value, "openai/gpt-5.6-sol");
    });

    console.log("\n== 7 设置面板实际显示（有内置 Key） ==");
    const { w: w2, errors: errors2 } = load({
        H3Api: {
            async getOptimizerConfig() {
                return { body: { ok: true, has_default_key: true, has_api_key: true,
                                 api_key: "", models: [], mmproj_models: [],
                                 providers: { glm: { url: "https://open.bigmodel.cn/api/paas/v4",
                                                    model: "glm-4.6v", protocol: "openai" } } } };
            },
        },
    });
    await ta("有内置 Key 时占位符说明「留空即用」", async () => {
        await w2.openOptSettings(mkNode({}));
        const dlg = w2.document.querySelector(".h3d-opt-dialog");
        assert.ok(/留空即用/.test(ctrlOf(dlg, "API Key").placeholder),
            "占位符没说明内置 Key：" + ctrlOf(dlg, "API Key").placeholder);
    });
    await ta("有内置 Key 时不再因为 Key 为空把用户拦到面板（optNeedsSetup 放行）", async () => {
        const st = w2.optGetSettings(mkNode({}));
        assert.strictEqual(await w2.optNeedsSetup(mkNode({}), st), false);
    });

    console.log("\n== 8 无内置 Key 时仍要拦 ==");
    await ta("没填 Key 且服务端没有 -> 拦截", async () => {
        const st = w.optGetSettings(mkNode({}));
        assert.strictEqual(await w.optNeedsSetup(mkNode({}), st), true);
    });
    await ta("填了 Key -> 放行", async () => {
        const st = w.optGetSettings(mkNode({ optimizer: { api_key: "sk-x" } }));
        assert.strictEqual(await w.optNeedsSetup(mkNode({}), st), false);
    });

    results.forEach((r) => console.log(r));
    const allErr = [...errors, ...errors2, ...errorsC, ...errorsP];
    if (allErr.length) { console.log("\n页面错误: " + allErr.join(" | ")); ok = false; }
    console.log(ok ? "\nopt_provider_default_check 全部通过" : "\nopt_provider_default_check 失败");
    process.exit(ok ? 0 : 1);
})();
