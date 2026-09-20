/* jsdom 全量装载 harness：把 web/h3_director.js 当成真脚本跑起来，
 * 便于在 node 里驱动 openFramePicker / 上传 / 刷新等端到端链路。
 * 只给最小桩（app / api / fetch / H3* 模块），其余全部走 shipped 源码。 */
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");

let JSDOM;
try {
    ({ JSDOM } = require("jsdom"));
} catch (e) {
    console.error("SKIP: 未安装 jsdom（npm install）");
    process.exit(2);
}

function load(opts = {}) {
    const dom = new JSDOM("<!doctype html><html><body></body></html>", {
        runScripts: "outside-only",
        url: "http://127.0.0.1:8188/",
        pretendToBeVisual: true,
    });
    const w = dom.window;

    /* ---- 最小环境桩 ---- */
    const errors = [];
    w.addEventListener("error", (e) => errors.push(String(e.error || e.message)));
    w.alert = (m) => { (opts.onAlert || (() => {}))(String(m)); };
    w.confirm = () => true;

    const node = opts.node;
    w.app = {
        graph: { _nodes: node ? [node] : [], change() {} },
        registerExtension(ext) { this._ext = ext; },
        extensionManager: { registerSidebarTab() {} },
        queuePrompt() {},
        loadGraphData() {},
        canvas: {},
    };
    /* 记录 /h3chain/* 请求，供端到端断言（如 flushPrompts 有没有把 frame_img 带上）。
     * apiRoutes：按路径片段给 API 体（值可以是对象或 path => 体 的函数）。
     * 为什么需要它：manifest 一律走 /h3chain/project 读（API 直读磁盘）；
     * /api/view 那条路是可启发式缓存的 FileResponse，读高频改写的 JSON 会拿到旧副本。 */
    const apiCalls = [];
    const apiRoutes = Object.assign({}, opts.apiRoutes);
    w.api = {
        addEventListener() {},
        async fetchApi(path, init) {
            const body = init && init.body ? JSON.parse(String(init.body)) : null;
            apiCalls.push({ path: String(path || ""), body });
            for (const k of Object.keys(apiRoutes)) {
                if (String(path || "").indexOf(k) >= 0) {
                    const v = apiRoutes[k];
                    return { ok: true, status: 200,
                        json: async () => (typeof v === "function" ? v(String(path)) : v) };
                }
            }
            return { ok: true, status: 200, json: async () => (opts.apiReply || { ok: true }) };
        },
    };
    w.__apiCalls = apiCalls;

    const routes = Object.assign({}, opts.routes);
    w.fetch = async (url) => {
        const u = String(url || "");
        for (const k of Object.keys(routes)) {
            if (u.indexOf(k) >= 0) {
                const v = routes[k];
                const body = typeof v === "function" ? v(u) : v;
                if (body === null || body === undefined) {
                    return { ok: false, status: 404, text: async () => "" };
                }
                return { ok: true, status: 200, text: async () => JSON.stringify(body) };
            }
        }
        return { ok: false, status: 404, text: async () => "" };
    };

    /* H3* 依赖模块：真源码 */
    for (const f of ["h3_prompts.js", "h3_assets.js"]) {
        w.eval(fs.readFileSync(path.join(ROOT, "web", f), "utf8"));
    }
    /* h3_director.js 是 ESM（import { app } / { api }）：jsdom 里只能当脚本跑，
     * 剥掉 import 后由 harness 注入同名全局。其余源码一字不改。 */
    const dirSrc = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8")
        .replace(/^\s*import\s+[^\n]*\n/gm, "");
    if (opts.h3api !== false) {
        w.H3Api = Object.assign({
            async assetLinks() { return { body: { ok: true, links: [] } }; },
            async saveAssets() { return { body: { ok: true } }; },
        }, opts.H3Api || {});
    }
    if (opts.H3Assets) w.H3Assets = opts.H3Assets;

    w.eval(dirSrc);
    return { dom, w, errors };
}

/** 造一个假的 H3 主节点（只带导演台需要的 widget） */
function mkNode(ds) {
    const widgets = [
        { name: "导演台状态", value: JSON.stringify(ds), type: "text", options: {} },
        { name: "存档目录", value: "PROJ", type: "text", options: {} },
        { name: "每段时长", value: 5, type: "number", options: {} },
        { name: "画幅", value: "16:9", type: "text", options: {} },
        { name: "重跑起始段", value: 0, type: "number", options: {} },
        { name: "审片模式", value: "关闭", type: "text", options: {} },
    ];
    return {
        id: 7,
        /* 必须是导演台 findNode() 认的那个 NODE_TYPE（"H3SeamlessChainSampler"）：
         * 写成 "H3SeamlessChain" 的话 findNode() 永远返回 null，所有依赖真节点的
         * 链路（打开工作台 / 落地写回 / 上传）都会静默走"画布上未找到节点"分支，
         * 测试看起来在跑、其实一条都没验到。 */
        type: "H3SeamlessChainSampler",
        widgets,
        setDirtyCanvas() {},
        graph: { change() {} },
        getSize() { return [300, 400]; },
    };
}

module.exports = { load, mkNode, ROOT };
