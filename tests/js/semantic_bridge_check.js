/* 语义桥面板（右栏独立一栏）回归守卫。
 *
 * 守什么：
 *   ① ds.bridge 的默认值与归一（缺键/坏值都要收敛，不能把 undefined 送进后端）；
 *   ② 面板三个控件真的渲染出来、且选项表与后端 semantic_bridge.SCOPES 同口径；
 *   ③ 关闭时下面几行是**置灰**而不是消失（功能可见性）+ 开启时「已开启」角标；
 *   ④ bridgeSig 会随配置变化 —— 不变的话面板就永远停在旧值上。
 *
 * 为什么必须是 jsdom 实跑、不能只跑 `node --check`：那个命令对
 * 「模板字符串里混进反引号」是**假阴性**（exit 0），而这里 summary 全是模板字符串拼的。
 *
 * 用法：node tests/js/semantic_bridge_check.js
 */
const assert = require("assert");
const { load, mkNode } = require("./_harness");

let ok = true;
const results = [];
function t(name, fn) {
    try { fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}

const { w, errors } = load({});

const mkSec = () => w.document.createElement("div");
const mkData = (node, bridge, models) => ({
    node,
    ds: { bridge: Object.assign(w.defaultBridge(), bridge || {}) },
    bridgeModels: models || [],
});
/** 渲染一栏，返回 { det, cbs, nums, sels, params, text } */
function paint(node, bridge, models) {
    const secEl = mkSec();
    w.renderBridgeZone(secEl, mkData(node, bridge, models));
    return {
        secEl,
        det: secEl.querySelector("details"),
        cbs: [...secEl.querySelectorAll("input[type=checkbox]")],
        nums: [...secEl.querySelectorAll("input[type=number]")],
        sels: [...secEl.querySelectorAll("select")],
        params: [...secEl.querySelectorAll(".h3d-param")],
        text: secEl.textContent || "",
    };
}

const NODE = mkNode({ prompts: [""] });
const MODEL = "BUNNY_H3_Semantic_Bridge_V2_seed22345.safetensors";

console.log("\n== 语义桥面板 ==");

t("defaultBridge 契约：默认关闭 / alpha 0.15 / 全量过桥", () => {
    const b = w.defaultBridge();
    assert.strictEqual(b.enabled, false, "默认必须关闭（否则既有项目会被静默改写）");
    assert.strictEqual(b.adapter, "");
    assert.strictEqual(b.alpha, 0.15);
    assert.strictEqual(b.scope, "all");
});

t("getDs 缺键：补出完整 bridge 默认值（不让 undefined 进后端）", () => {
    const n = mkNode({ prompts: ["x"] });
    const b = w.getDs(n).bridge;
    assert.strictEqual(b.enabled, false);
    assert.strictEqual(b.alpha, 0.15);
    assert.strictEqual(b.scope, "all");
    assert.strictEqual(b.adapter, "");
});

t("getDs 归一：坏 scope 回落 all、alpha 钳到 0..1、enabled 只认真 true", () => {
    const n = mkNode({ prompts: ["x"], bridge: { enabled: "yes", alpha: 9, scope: "bogus", adapter: " a.safetensors " } });
    const b = w.getDs(n).bridge;
    assert.strictEqual(b.scope, "all", "未知 scope 必须回落 all");
    assert.strictEqual(b.alpha, 1, "alpha 必须钳到 [0,1]");
    assert.strictEqual(b.enabled, false, "只有真 true 才算开");
    assert.strictEqual(b.adapter, "a.safetensors", "权重名要去空白");
});

t("getDs 归一：alpha 负值钳到 0", () => {
    const n = mkNode({ prompts: ["x"], bridge: { alpha: -3 } });
    assert.strictEqual(w.getDs(n).bridge.alpha, 0);
});

t("渲染：一栏 + 一个开关 + 一个数值 + 两个下拉", () => {
    const p = paint(NODE, {}, [MODEL]);
    assert.ok(p.det, "必须是一栏可折叠面板（details）");
    assert.strictEqual(p.cbs.length, 1, "开关只有 1 个");
    assert.strictEqual(p.nums.length, 1, "强度只有 1 个数值框");
    assert.strictEqual(p.sels.length, 2, "作用范围 + 权重 共 2 个下拉");
    assert.ok(p.text.indexOf("语义桥") >= 0);
});

t("作用范围选项与后端同口径：全量过桥 / 仅文本过桥", () => {
    const p = paint(NODE, {}, [MODEL]);
    const labels = [...p.sels[0].options].map((o) => o.textContent);
    assert.deepStrictEqual(labels, ["全量过桥", "仅文本过桥"]);
    assert.deepStrictEqual([...p.sels[0].options].map((o) => o.value), ["all", "text"]);
});

t("默认态：开关未勾、alpha=0.15、范围选中「全量过桥」", () => {
    const p = paint(NODE, {}, [MODEL]);
    assert.strictEqual(p.cbs[0].checked, false);
    assert.strictEqual(Number(p.nums[0].value), 0.15);
    assert.strictEqual(p.sels[0].value, "all");
});

t("默认态：开关以外的三行是**置灰**而不是隐藏（功能可见）", () => {
    const p = paint(NODE, {}, [MODEL]);
    assert.strictEqual(p.params.length, 4, "开关 + 强度 + 范围 + 权重");
    assert.strictEqual(p.params[0].style.opacity, "", "开关本身不该灰");
    for (const i of [1, 2, 3]) {
        assert.strictEqual(p.params[i].style.opacity, "0.45", `第 ${i} 行该置灰`);
    }
});

t("开启态：角标出现、开关勾上、三行恢复不灰", () => {
    const p = paint(NODE, { enabled: true, adapter: MODEL }, [MODEL]);
    assert.ok(p.text.indexOf("已开启") >= 0, "该有「已开启」角标");
    assert.strictEqual(p.cbs[0].checked, true);
    for (const i of [1, 2, 3]) assert.strictEqual(p.params[i].style.opacity, "");
});

t("权重下拉：列出后端给的列表并选中当前值", () => {
    const p = paint(NODE, { enabled: true, adapter: MODEL }, [MODEL, "other.safetensors"]);
    const vals = [...p.sels[1].options].map((o) => o.value);
    assert.deepStrictEqual(vals, [MODEL, "other.safetensors"]);
    assert.strictEqual(p.sels[1].value, MODEL);
});

t("权重列表为空：下拉禁用且写清原因（不是静默空白）", () => {
    const p = paint(NODE, { enabled: true, adapter: "" }, []);
    assert.strictEqual(p.sels[1].disabled, true);
    assert.strictEqual(p.sels[1].options[0].textContent, "models/ 下无权重");
    assert.ok(p.text.indexOf("models/") >= 0, "要提示权重该放哪");
});

t("文案不许把作用范围说成「增强 latent 锚」", () => {
    const p = paint(NODE, { enabled: true, adapter: MODEL }, [MODEL]);
    assert.ok(p.sels[0].title.indexOf("latent 锚") >= 0,
        "必须讲清 latent 锚不在这条链路上，否则用户会以为它能增强锚定");
    assert.ok(p.sels[0].title.indexOf("逐字节保留") >= 0, "要写明仅文本档的语义");
});

t("画布上没有节点：给明确空态而不是抛错", () => {
    const p = paint(null, {}, [MODEL]);
    assert.ok(p.text.indexOf("未找到节点") >= 0);
});

t("bridgeSig 随开关/强度/范围/权重变化（不变则面板永不重建）", () => {
    const base = w.bridgeSig(mkData(NODE, {}, [MODEL]));
    for (const patch of [{ enabled: true }, { alpha: 0.5 }, { scope: "text" },
                         { adapter: MODEL }]) {
        assert.notStrictEqual(w.bridgeSig(mkData(NODE, patch, [MODEL])), base,
            "改 " + JSON.stringify(patch) + " 必须改变签名");
    }
    assert.strictEqual(w.bridgeSig(mkData(NODE, {}, [MODEL])),
        w.bridgeSig(mkData(NODE, {}, [MODEL])), "配置不变签名必须稳定");
});

t("bridgeSig 随权重列表变化（列表晚到时才会补渲染出选项）", () => {
    const a = w.bridgeSig(mkData(NODE, {}, []));
    const b = w.bridgeSig(mkData(NODE, {}, [MODEL]));
    assert.notStrictEqual(a, b);
});

/* ═══════════ 端到端：真的打开导演台，看这一栏有没有挂上去 ═══════════
 * 上面全是**直接调渲染函数**，验不到「desk 有没有建这个 section、有没有把它
 * 登记进 zones、updateDesk 有没有调它」—— 那正是本项目历史上反复栽的
 * 「控件建了、但没接上」那一类。这里真跑一遍 openDesk。 */
const SOURCE = require("fs").readFileSync(
    require("path").join(__dirname, "..", "..", "web", "h3_director.js"), "utf8");

t("源码：rBridge 区块建好并挂进右栏", () => {
    assert.ok(SOURCE.indexOf('const rBridge = el("section"') >= 0, "缺少 rBridge 区块");
    assert.ok(SOURCE.indexOf("colR.append(rParams, rBridge, rUpscale, rHist)") >= 0,
        "rBridge 没挂进右栏顺序里");
    assert.ok(SOURCE.indexOf("rParams, rBridge, rUpscale, rHist,") >= 0,
        "rBridge 没登记进 desk.zones（不登记就永远不渲染）");
});

t("源码：updateDesk 用 bridgeSig 调度这一栏", () => {
    assert.ok(SOURCE.indexOf("const bsig = bridgeSig(data)") >= 0, "缺签名计算");
    assert.ok(SOURCE.indexOf("renderBridgeZone(z.rBridge, data)") >= 0, "缺渲染调用");
});

t("源码：collectData 取了权重列表并带回", () => {
    assert.ok(SOURCE.indexOf('apiGet("/h3chain/bridge_models")') >= 0, "没请求权重列表");
    assert.ok(/return \{[^}]*bridgeModels \}/.test(SOURCE), "bridgeModels 没随 data 返回");
});

function report() {
    results.forEach((r) => console.log(r));
    if (errors.length) {
        console.log("\n页面错误: " + errors.join(" | "));
        ok = false;
    }
    console.log(ok ? "\nsemantic_bridge_check 全部通过" : "\nsemantic_bridge_check 失败");
    process.exit(ok ? 0 : 1);
}

(async () => {
    const manifest = {
        revision: 1, done: 0, finals: [], merges: [], assets: [], seg_fields: [],
        prompts: ["integrated_multimodal_description: 测试"], segments: [{ seconds: 5 }],
    };
    const env = load({
        node: mkNode({ prompts: ["integrated_multimodal_description: 测试"] }),
        apiRoutes: {
            /* 注意键序：harness 按**插入顺序**做子串匹配，"/h3chain/project"
             * 是 "/h3chain/projects" 的前缀 —— 反了就会把 manifest 当项目列表发出去。 */
            "/h3chain/projects": () => ({ ok: true, projects: [], state: null }),
            "/h3chain/project": () => ({ ok: true, manifest }),
            "/h3chain/bridge_models": () => ({ ok: true, models: [MODEL] }),
            "/h3chain/upscale_models": () => ({ ok: true, models: [] }),
        },
    });
    const w2 = env.w;
    const doc = w2.document;
    const tick = (ms) => new Promise((r) => setTimeout(r, ms));
    await tick(20);
    w2.eval("openDesk")();
    await tick(120);

    const bridgeSec = [...doc.querySelectorAll(".h3d-rsec")]
        .find((s) => (s.textContent || "").indexOf("语义桥") >= 0);

    t("端到端：导演台右栏真的渲染出「语义桥」一栏", () => {
        assert.ok(bridgeSec, "右侧没有语义桥那一栏（section 建了但没渲染）");
    });

    t("端到端：真 DOM 里三个控件都在，权重来自后端列表", () => {
        assert.ok(bridgeSec, "没有这一栏");
        assert.strictEqual(bridgeSec.querySelectorAll('input[type="checkbox"]').length, 1);
        assert.strictEqual(bridgeSec.querySelectorAll('input[type="number"]').length, 1);
        const sels = [...bridgeSec.querySelectorAll("select")];
        assert.strictEqual(sels.length, 2);
        assert.deepStrictEqual([...sels[0].options].map((o) => o.value), ["all", "text"]);
        assert.deepStrictEqual([...sels[1].options].map((o) => o.value), [MODEL],
            "权重下拉必须来自 /h3chain/bridge_models");
    });

    t("端到端：点开关 → 写进 ds.bridge 并自动选上权重", () => {
        const cb = bridgeSec.querySelector('input[type="checkbox"]');
        cb.checked = true;
        cb.dispatchEvent(new w2.Event("change"));
        const b = w2.getDs(w2.app.graph._nodes[0]).bridge;
        assert.strictEqual(b.enabled, true, "开关没写进 ds.bridge");
        assert.strictEqual(b.adapter, MODEL, "开启时应顺手把权重选上（否则一开就报错）");
    });

    t("端到端：改作用范围 / 强度 → 都落到 ds.bridge", () => {
        const sels = [...bridgeSec.querySelectorAll("select")];
        sels[0].value = "text";
        sels[0].dispatchEvent(new w2.Event("change"));
        const num = bridgeSec.querySelector('input[type="number"]');
        num.value = "0.3";
        num.dispatchEvent(new w2.Event("change"));
        const b = w2.getDs(w2.app.graph._nodes[0]).bridge;
        assert.strictEqual(b.scope, "text", "作用范围没写回");
        assert.strictEqual(b.alpha, 0.3, "强度没写回");
    });

    if (env.errors.length) {
        errors.push(...env.errors.map((e) => "desk: " + e));
    }
    report();
})();
