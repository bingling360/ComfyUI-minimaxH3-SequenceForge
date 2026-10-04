/* H3 运行报告 · 前端皮肤（免连线预览）
 *
 * 后端节点 run_report.py 定义（无输入端口、无参数），这里负责把报告画在节点里。
 *
 * 为什么报告不走进线：
 *   导演台的「报告」输出是**连线**，而 ComfyUI 的连线按**位次**存在 workflow JSON 的
 *   links 里（origin_slot）。只要输出端口的顺序一变，老存档里那根线就会静默落到别的
 *   槽上 —— 画布上看着还连着、下游实际收不到，用户侧就是「每次重启 ComfyUI，
 *   导演台和 PreviewAny 都断连」。（schema 顺序已在 2026-10-04 修好：停机场槽「帧率」
 *   挪到末位、「报告」钉回 2 号位。本节点是兜底：**没有输入端口 = 没有可断的线**。）
 *
 * 报告从哪来：
 *   本来就是后端每跑一段就落盘的（checkpoint.save_state 写
 *   output/h3_projects/h3chain_state.json 的 "report"），前端直接读插件自己的接口
 *   GET /h3chain/projects → {state:{dir,done,total,report,updated_at}}。
 *   不读 /api/view —— 那个端点是可启发式缓存的 FileResponse，而这份文件每跑一段就改写
 *   （会反复拿到旧指针，见 routes.py list_projects 的注释）。
 *
 * 刷新时机：① 节点建立/载入工作流；② 每次 execution_success；③ 手动点「刷新报告」。
 * 三个都不依赖执行图 —— 这个节点默认不进执行图。
 */
import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const NODE_TYPE = "H3RunReport";
const WIDGET_NAME = "h3_run_report_view";
const H3R_VER = "20261004";
const EMPTY_HINT = "（还没有报告）\n先跑一次导演台，报告会自动出现在这里。\n也可以点上面的「🔄 刷新报告」重读磁盘上最新的一份。";

/** 节点实例集合：刷新时遍历它们（避免全图扫描）。 */
const liveNodes = new Set();

function fmtTime(ts) {
    const n = Number(ts);
    if (!Number.isFinite(n) || n <= 0) return "";
    try { return new Date(n * 1000).toLocaleString(); } catch (e) { return ""; }
}

/** 拉最新报告。失败返回 {ok:false, error}，绝不抛（载入期抛会把整个扩展链炸掉）。 */
async function fetchReport() {
    try {
        const r = await api.fetchApi("/h3chain/projects");
        if (!r.ok) return { ok: false, error: `接口 HTTP ${r.status}` };
        const d = await r.json();
        const st = (d && d.state) || {};
        return {
            ok: true,
            report: String(st.report || ""),
            dir: String(st.dir || ""),
            done: Number(st.done) || 0,
            total: Number(st.total) || 0,
            updatedAt: st.updated_at,
        };
    } catch (e) {
        return { ok: false, error: String(e) };
    }
}

/** 把一份报告写进节点（幂等；文本没变就不碰 DOM，避免打断用户选中/滚动）。 */
function paint(node, res) {
    const pre = node.__h3rPre;
    const head = node.__h3rHead;
    if (!pre || !head) return;
    if (!res || !res.ok) {
        node.__h3rText = "";
        head.textContent = `读取失败：${(res && res.error) || "未知错误"}`;
        pre.textContent = "";
        return;
    }
    const text = res.report || "";
    const bits = [];
    if (res.dir) bits.push(`项目 ${res.dir}`);
    if (res.total) bits.push(`进度 ${res.done}/${res.total}`);
    const t = fmtTime(res.updatedAt);
    if (t) bits.push(`更新 ${t}`);
    if (!text) bits.push("尚无报告");
    head.textContent = bits.join(" · ");
    if (node.__h3rText === text) return;
    node.__h3rText = text;
    pre.textContent = text || EMPTY_HINT;
}

/** 建控件（只做一次）。DOM 控件能滚动 —— 报告动辄几十行，画布文字没法滚。 */
function mountView(node) {
    if (node.__h3rMounted) return;
    if (typeof node.addDOMWidget !== "function" || typeof node.addWidget !== "function") return;
    node.__h3rMounted = true;

    const box = document.createElement("div");
    box.style.cssText = "display:flex;flex-direction:column;gap:4px;width:100%;height:100%;"
        + "box-sizing:border-box;padding:2px;";
    // DOM 控件的高度：minHeight 决定节点最小高度，maxHeight 让它随节点拉高而长高
    box.style.setProperty("--comfy-widget-min-height", "180");
    box.style.setProperty("--comfy-widget-max-height", "4000");
    box.style.setProperty("--comfy-widget-height", "400");

    const head = document.createElement("div");
    head.textContent = "读取中…";
    head.style.cssText = "flex:0 0 auto;font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;"
        + "opacity:.65;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";

    const pre = document.createElement("pre");
    pre.textContent = "读取中…";
    pre.style.cssText = "flex:1 1 auto;margin:0;overflow:auto;white-space:pre-wrap;"
        + "word-break:break-word;font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;"
        + "background:rgba(127,127,127,.10);border-radius:6px;padding:6px;box-sizing:border-box;";

    box.append(head, pre);
    node.__h3rHead = head;
    node.__h3rPre = pre;

    try {
        node.addDOMWidget(WIDGET_NAME, "div", box, {
            serialize: false,     // 纯展示：不进 workflow JSON
            hideOnZoom: false,    // 缩小时也留着，方便一眼扫
        });
    } catch (e) {
        console.warn("[h3-report] DOM 控件挂载失败，降级为按钮 + 控制台输出", e);
    }

    const btn = node.addWidget("button", "🔄 刷新报告", null, () => refreshNode(node));
    if (btn) {
        btn.serialize = false;
        if (btn.options) btn.options.serialize = false;
    }

    liveNodes.add(node);
    // 首次挂载后立刻拉一次（不阻塞节点建立）
    setTimeout(() => refreshNode(node), 0);
}

async function refreshNode(node) {
    if (!node || !node.__h3rMounted) return;
    if (node.__h3rBusy) return;                 // 防抖：连点按钮不打爆接口
    node.__h3rBusy = true;
    try {
        const res = await fetchReport();
        paint(node, res);
        if (!res.ok) {
            console.warn(`[h3-report] 报告读取失败：${res.error}`);
        } else if (!res.report) {
            console.log("[h3-report] 磁盘上还没有报告（output/h3_projects/h3chain_state.json 无 report 字段）");
        }
    } finally {
        node.__h3rBusy = false;
    }
}

/** 刷新画布上所有报告节点（一次请求，N 个节点共用结果）。 */
async function refreshAll() {
    const nodes = Array.from(liveNodes).filter((n) => n && n.__h3rMounted);
    if (!nodes.length) return;
    const res = await fetchReport();
    for (const n of nodes) paint(n, res);
    if (!res.ok) console.warn(`[h3-report] 报告读取失败：${res.error}`);
}

app.registerExtension({
    name: "H3SeamlessChain.RunReport",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (!nodeData || nodeData.name !== NODE_TYPE) return;
        // 报告节点不需要「连不连得上」的任何语义：它没有输入端口
        nodeType.prototype.__h3rIsReport = true;
    },
    nodeCreated(node) {
        if (!node || node.type !== NODE_TYPE) return;
        mountView(node);
        try {
            if (!node.size || node.size[1] < 260) node.setSize([430, 340]);
        } catch (e) { /* 尺寸设置失败不影响功能 */ }
    },
    loadedGraphNode(node) {
        if (!node || node.type !== NODE_TYPE) return;
        mountView(node);            // 载入路径兜底：某些前端建控件晚于 nodeCreated
    },
    setup() {
        console.log("[h3-report] loaded", H3R_VER);
        // 跑完就刷新 —— 报告是后端落盘的，不需要这个节点参与执行
        api.addEventListener("execution_success", () => { refreshAll(); });
        // 审片/逐段模式下每次执行都会改写报告，也顺手刷一把
        api.addEventListener("executed", () => { refreshAll(); });
    },
});
