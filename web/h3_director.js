/**
 * H3 Seamless Chain —— 「长片导演台」（全屏一体化控制台 + 侧栏迷你入口）
 *
 * 参照「H3 一体化总控导演台」状态驱动架构：所有输入（模式/提示词/首帧/参考素材）
 * 存入节点「导演台状态」JSON widget，节点 execute 读取 JSON 加载素材；
 * 画布节点只作镜像/兜底（配套工作流的常驻隐藏节点），不在画布上动态建线。
 *
 * 状态 JSON schema v2（存入「导演台状态」widget）：
 *   { mode:"文生视频"|"首帧视频"|"多参视频",
 *     prompts:["段1文本","段2文本",...],
 *     first_frame:"input目录下的文件名",
 *     end_frame:"input目录下的文件名（FL2VA 剧情终点，仅首帧模式）",
 *     ref_assets:[ { file:"文件名", kind:"image"|"video"|"audio", label:"角色1" }, ... ],
 *                                // 标签素材池（真源，三类混排；官方单段上限 图9/视3/音3）
 *     ref_images:["文件名1",...], // 兼容保留 = ref_assets 中图片类文件的序列
 *     segments:[
 *       { scene_prompt:"场景描述", character_prompt:"角色描述",
 *         seconds:6.5,        // 本段时长（秒），null=跟随节点「每段时长」默认
 *         refs:["角色1","场景1"] },  // 本段引用的素材标签（跨类别），[]=引用全部
 *       ...
 *     ] }
 *
 * 标签引用：提示词写 [[角色1]]，后端按段按类别压实重编号（图→<Picture k>、视→<Video k>、
 * 音→<Audio j>）；原生 token 写法继续兼容。v1 状态（只有 ref_images）自动迁移为图片类。
 * 段级注入：未勾选的素材完全不进该段 conditioning；参考视频的原声自动配对成 <Audio j>。
 *
 * 模式互斥在 JSON 层完成：切模式即清空不相容字段，后端复验不匹配报错。
 * 分段处理中心：segments 与 prompts 等长，每段的 scene_prompt/character_prompt
 * 由后端组合到该段主提示词前（scene → character → 主提示词），不影响核心采样。
 *
 * 配套工作流（web/h3_default_workflow.js）：画布无 H3 节点时可一键载入官方风格
 * 预置工作流（含提示词×3 镜像 + 分段输出链 CreateVideo→SaveVideo）；
 * 「素材池 · 自动管理」组的 LoadImage/LoadVideo/LoadAudio 常驻预连，导演台上传
 * 素材时点亮对应节点（mode=0），删除时隐藏（mode=2+折叠）——连线始终存在，只做显隐。
 */
import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const NODE_TYPE = "H3SeamlessChainSampler";
const W_REROLL = "重跑起始段";
const W_SEED = "种子";
const W_DIR_NAMES = ["存档目录", "断点目录"];
const W_MODE = "生成模式";
const W_DS = "导演台状态";
const W_AR = "宽高比";
const W_MP = "百万像素";
/* 版本标记：浏览器控制台过滤 [h3-director] 可确认加载的是新 JS 还是缓存旧版 */
const H3D_VER = "20260826+portrait-thumb";
const W_DUR = "每段时长";
const W_WIDTH = "宽度";
const W_HEIGHT = "高度";
const AR_LIST = ["自定义", "21:9", "16:9", "9:16", "4:3", "3:4", "1:1"];
const AR_RATIO = { "21:9": 21 / 9, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "1:1": 1 };
const MP_LIST = [...Array.from({ length: 20 }, (_v, i) => ((i + 1) / 10).toFixed(1)), "0.98"].sort((a, b) => a - b);   // 0.1–2.0 共 20 档 + 0.98（官方 1344×768 原生档，旧档迁移用）
const QUICK_LABELS = ["角色1", "角色2", "场景1", "场景2", "风格", "道具"];
/* 素材类别：与后端 REF_CAPS/_KIND_NAME 对齐（官方单段上限 图9/视3/音3） */
const KIND_LIST = ["image", "video", "audio"];
const KIND_NAME = { image: "图片", video: "视频", audio: "音频" };
const KIND_ICON = { image: "🖼", video: "🎞", audio: "🎵" };
const KIND_TOKEN = { image: "Picture", video: "Video", audio: "Audio" };
const KIND_CAPS = { image: 9, video: 3, audio: 3 };
const KIND_ACCEPT = { image: "image/*", video: "video/*", audio: "audio/*" };
/* 引用语模板库：插入到提示词光标处，[[标签]] 由后端按段压实为 <Picture k> */
const REF_TEMPLATES = [
    ["插入 [[标签]]", (l) => `[[${l}]]`],
    ["主角出场", (l) => `主角 [[${l}]] 全程出镜（主体身份、外观与服饰全程保持一致）`],
    ["配角出场", (l) => `画面中出现的 [[${l}]] 为次要角色，身份与外观保持一致`],
    ["场景还原", (l) => `场景以 [[${l}]] 为准，延续其环境、光照与空间布局`],
    ["风格参考", (l) => `整体画风、色调与质感参考 [[${l}]]`],
    ["镜头参考", (l) => `运镜方式参考 [[${l}]]（可用官方词汇：Push In / Pan Left / Truck Right / Tracking Shot，加 with small amplitude at slow speed 等修饰）`],
    ["说话人", () => `短发女主 (S1) 轻声说：「……」`],
];
const MODES = [
    ["文生视频", "文生", "纯文本，fl2va UNET，不接图片"],
    ["首帧视频", "首帧", "首帧起手（可选尾帧图片=FL2VA 首尾帧），fl2va UNET"],
    ["多参视频", "多参", "参考图/视频/音频，ref2va UNET，[[标签]] 引用"],
];
const MODE_DEFAULT = "文生视频";
const MAX_SEG = 64;

/* ---- 实验性功能（唯一权威 = 后端 experiments.py，经 /h3chain/experiments 动态拉取）----
 * 状态存进「导演台状态」JSON 的 ds.experiments（不新增画布控件、不进 paramsSig）。
 * 契约端到端扁平：ds.experiments = {<id>: true, params: {<id>: {...}}, locked?: bool}
 * （locked 为前端 UI 主开关状态键，后端 ExperimentContext 只认 defs 内 id 与 params，自动忽略） */
const EXP = { defs: null, forceDisabled: false, failed: "", loading: null };

function loadExperimentDefs() {
    if (EXP.defs || EXP.loading) return EXP.loading;    // 幂等：已缓存/在途不重复拉
    EXP.failed = "";
    EXP.loading = apiGet("/h3chain/experiments").then((r) => {
        if (r.ok && Array.isArray(r.data?.experiments)) {
            EXP.defs = r.data.experiments;
            EXP.forceDisabled = !!r.data.force_disabled;
        } else {
            EXP.failed = `HTTP ${r.status || "??"}`;
        }
        EXP.loading = null;
        scheduleRefresh(0);                              // defs 到达后触发面板重渲
    }).catch(() => { EXP.failed = "network"; EXP.loading = null; scheduleRefresh(0); });
    return EXP.loading;
}

function defaultExperiments(defs = EXP.defs) {
    const out = { params: {} };
    for (const e of defs || []) {
        const pd = {};
        for (const p of e.params || []) pd[p.key] = p.def;
        out.params[e.id] = pd;
    }
    return out;
}

/* 扁平归一：defs 未到达时宽进（布尔 true 键与参数字典原样保留，避免 defs 到达前的
 * 早窗口期 setDs 把已存参数清掉；未知 id 交给后端过滤）；
 * defs 到达后严进（只认已知 id），参数按 defs 元数据钳位/白名单；
 * locked 键透传（前端主开关 UI 状态）。 */
function normalizeExperiments(raw, defs = EXP.defs) {
    const out = defaultExperiments(defs);
    if (!raw || typeof raw !== "object") return out;
    if (typeof raw.locked === "boolean") out.locked = raw.locked;
    for (const [k, v] of Object.entries(raw)) {
        if (k === "params" || k === "locked" || v !== true) continue;
        if (defs && !defs.some((e) => e.id === k)) continue;
        out[k] = true;
    }
    const rp = raw.params && typeof raw.params === "object" ? raw.params : {};
    if (!defs) {
        out.params = JSON.parse(JSON.stringify(rp));   // 宽进：参数字典深拷贝透传
        return out;
    }
    for (const e of defs) {
        const src = rp[e.id] && typeof rp[e.id] === "object" ? rp[e.id] : {};
        for (const p of e.params || []) {
            if (p.type === "num") {
                const n = Number(src[p.key]);
                out.params[e.id][p.key] = isFinite(n) ? Math.min(p.max, Math.max(p.min, n)) : p.def;
            } else if (p.opts && p.opts.includes(src[p.key])) {
                out.params[e.id][p.key] = src[p.key];
            }
        }
    }
    return out;
}

let miniBox = null;
let desk = null;
let pendingReset = false;
let refreshTimer = null;
let ledPhase = "idle";
let ledText = "待命";
let apiErrorText = "";
let lastDir = "";            // 最近一次数据刷新解析出的当前项目目录（合并导出用）
/* 合并模式（纯内存勾选态，不落 ds / 不触发重做）：勾选已完成段（含序章/插入段，
 * 按链顺序）+ 可追加上传外部素材 -> 拼接出新 merged_*.mp4，不动链与存档 */
const mergeSel = { on: false, segs: [], files: [] };
/* 拖拽调序进行中（{idx: 提示词数组下标, fromP: 当前 plan 位}）：
 * 重建锁定 + drop 目标计算用；dragend 后置 null */
let dragSeg = null;

/* ---------- 小工具 ---------- */

function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
}

function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
}

function viewUrl(subfolder, filename) {
    return `/api/view?type=output&subfolder=${encodeURIComponent(subfolder)}&filename=${encodeURIComponent(filename)}`;
}

async function fetchJson(subfolder, filename) {
    try {
        const r = await fetch(viewUrl(subfolder, filename));
        if (!r.ok) return null;
        return JSON.parse(await r.text());
    } catch (e) {
        return null;
    }
}

/* 项目存档后端接口（/h3chain/*，与 /api/view 文件读取不同源） */
async function apiGet(path) {
    try {
        const r = await api.fetchApi(path);
        if (!r.ok) return { ok: false, status: r.status };
        return { ok: true, data: await r.json() };
    } catch (e) {
        return { ok: false, status: 0, error: String(e) };
    }
}

function setApiError(text) {
    apiErrorText = text || "";
    document.querySelectorAll(".h3d-banner").forEach((b) => {
        b.textContent = apiErrorText;
        b.style.display = apiErrorText ? "" : "none";
    });
}

function fmtTime(v) {
    if (!v) return "";
    if (typeof v === "number") {
        const d = new Date(v * 1000);
        const pad = (x) => String(x).padStart(2, "0");
        return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
    return String(v).replace("T", " ").slice(5, 16);
}

function badge(text, cls) {
    return `<span class="h3d-chip ${cls || ""}">${escapeHtml(text)}</span>`;
}

/* ---------- 官方参数换算（与 nodes.py _resolve_canvas/_snap_seconds 同公式） ---------- */

function resolveCanvas(ar, mp) {
    const r = AR_RATIO[String(ar)];
    if (!r) return null;
    const total = parseFloat(mp) * 1024 * 1024;
    if (!isFinite(total) || total <= 0) return null;
    const fit = (x) => {
        const u = x / 32, f = Math.floor(u), d = u - f;
        const k = d === 0.5 ? (f % 2 === 0 ? f : f + 1) : Math.round(u);   // Python round：.5 取偶
        return Math.max(32, k * 32);
    };
    return [fit(Math.sqrt(total * r)), fit(Math.sqrt(total / r))];
}

function snapFrames(seconds) {
    const f = Math.max(5, Math.round(Number(seconds) * 24));
    const k = Math.max(0, Math.round((f - 5) / 17));
    return 17 * k + 5;
}

/** 宽高比+百万像素 -> 显示徽章文案；自定义/非法返回 null */
function canvasBadgeText(node) {
    const ar = String(getWidgetValue(node, W_AR) ?? "");
    if (!AR_RATIO[ar]) return null;
    const mp = String(getWidgetValue(node, W_MP) ?? "0.5");
    const c = resolveCanvas(ar, mp);
    return c ? `${ar} · ${mp}MP → ${c[0]}×${c[1]}` : null;
}

/** 反推：宽高完全命中某 AR×MP 组合则返回 [ar, mp]，否则 null（旧工作流迁移用）。
 *  mp 返回数值（「百万像素」已改浮点控件，写字符串会留下类型混杂） */
function matchCanvasCombo(w, h) {
    for (const ar of Object.keys(AR_RATIO)) {
        for (const mp of MP_LIST) {
            const c = resolveCanvas(ar, mp);
            if (c && c[0] === w && c[1] === h) return [ar, Number(mp)];
        }
    }
    return null;
}

/* ---------- 素材标签工具 ---------- */

function cleanLabel(text) {
    return String(text ?? "").trim().replace(/[[\]]/g, "").slice(0, 12);
}

function uniqueLabelFrom(taken, base) {
    if (!taken.has(base)) return base;
    let n = 2;
    while (taken.has(`${base}${n}`)) n += 1;
    return `${base}${n}`;
}

/** 在 textarea 光标处插入文本（未聚焦则追加到末尾），返回新值 */
function insertAtCursor(ta, text) {
    if (!ta) return "";
    const s = ta.selectionStart ?? ta.value.length;
    const e = ta.selectionEnd ?? s;
    ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
    const pos = s + text.length;
    ta.focus();
    ta.setSelectionRange(pos, pos);
    return ta.value;
}

function inputViewUrl(name) {
    const norm = String(name ?? "").replaceAll("\\", "/");
    const parts = norm.split("/");
    const file = parts.pop() ?? "";
    const sub = parts.join("/");
    return `/api/view?type=input&subfolder=${encodeURIComponent(sub)}&filename=${encodeURIComponent(file)}`;
}

async function uploadToInput(file) {
    const fd = new FormData();
    fd.append("image", file);
    fd.append("type", "input");
    fd.append("overwrite", "true");
    const r = await api.fetchApi("/upload/image", { method: "POST", body: fd });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    return j.subfolder ? `${j.subfolder}/${j.name}` : j.name;
}

/* ---------- 画布节点操作 ---------- */

function findNode() {
    const nodes = app.graph?._nodes || [];
    return nodes.find((n) => n.type === NODE_TYPE) || null;
}

function dirWidget(node) {
    if (!node) return null;
    return (node.widgets || []).find((w) => W_DIR_NAMES.includes(w.name)) || null;
}

function getWidgetValue(node, name) {
    if (!node) return null;
    const w = (node.widgets || []).find((w) => w.name === name);
    return w ? w.value : null;
}

function setWidgetValue(node, name, value) {
    if (!node) return false;
    const w = (node.widgets || []).find((w) => w.name === name);
    if (!w) return false;
    w.value = value;
    if (typeof w.callback === "function") {
        try { w.callback(value); } catch (e) { /* callback 可选 */ }
    }
    node.setDirtyCanvas(true, true);
    return true;
}

function getDirValue(node) {
    const w = node && dirWidget(node);
    return w ? String(w.value ?? "").trim() : "";
}

function setDirValue(node, value) {
    const w = node && dirWidget(node);
    if (!w) return false;
    w.value = value;
    if (typeof w.callback === "function") {
        try { w.callback(value); } catch (e) { /* callback 可选 */ }
    }
    node.setDirtyCanvas(true, true);
    return true;
}

/* ---- 旧工作流迁移：widget 改名/新增后 widgets_values 按位错位，载入前重排 ----
 * 老格式：[宽, 高, 每段帧数, 引导帧数, 种子, (ctrl)?, 步数, CFG, …]
 * 新格式：[宽高比, 百万像素, 宽, 高, 每段时长, 引导帧数, 种子, ctrl, 步数, CFG, …]
 * 识别：老格式 wv[0] 是数字（宽度）；新格式 wv[0] 是宽高比字符串 */
const SEED_CTRL_VALUES = ["fixed", "increment", "decrement", "randomize"];

function remapOldWidgetValues(wv) {
    const [w, h, frames, guide, seed, maybeCtrl, ...rest] = wv;
    const hasCtrl = typeof maybeCtrl === "string" && SEED_CTRL_VALUES.includes(maybeCtrl);
    const ctrl = hasCtrl ? maybeCtrl : "fixed";
    const tail = hasCtrl ? rest : [maybeCtrl, ...rest];
    const combo = matchCanvasCombo(Number(w), Number(h));
    const framesNum = Number(frames);
    const secs = isFinite(framesNum) && framesNum > 0
        ? Math.max(0.5, Math.min(15, Math.round((framesNum / 24) * 10) / 10)) : 5.0;
    return [
        combo ? combo[0] : "自定义",          // 宽高比：能命中 AR×MP 组合则迁移，否则保持自定义画幅
        combo ? combo[1] : 0.5,              // 百万像素（浮点控件）
        Number(w), Number(h),                // 宽/高（自定义模式继续生效，存档指纹不变）
        secs, guide, Number(seed), ctrl, ...tail,
    ];
}

/* 旧版工作流 widget 布局迁移到当前 29 值 schema。
 * 当前后端 define_schema 共 29 个控件值（28 控件 + 种子 control 占 1 位）。
 * 第二阶段提交 7435084 剔除了 8 个接缝/精修控件（接缝处理/混合帧数/精修强度/
 * 精修窗口/智能切镜/切镜最多丢帧/全链丢弃预算/自适应精修）并新增「自动成片」，
 * 旧 35 值布局因此中段错位、缺自动成片；更早的「导演台时代」极老工作流为 25 值。
 * 下面统一把这两种（及任意短于 29 的）旧布局直接映射到当前 29 值：
 * 头部 0..16 位与当前一致，中段已删控件取当前默认值，自动成片插在生成模式前，
 * 生成模式/导演台状态从尾部取，缺失尾部按默认值补齐（新尾部「一采编码」恒补标准）。 */
const V35_WIDGET_COUNT = 35;   // 第二阶段剔除控件后、未加自动成片的陈旧布局
const CUR_WIDGET_COUNT = 29;   // 当前 schema 控件值总数

// 当前 schema「中段」默认值（位置 17..26）：锚定加噪, 审片模式, 自动保存, 重跑起始段,
// 接缝重摇, 重摇阈值, 重摇上限, 递减锚定, 自动成片
const CUR_MID_DEFAULTS = [0.0, "关闭", "分段", 0, "自动", 0.06, 1, "关闭", "开启"];

// 旧 35 值布局中、与当前控件一一对应的源下标
const V35_PICK = {
    aod: 19, review: 20, autosave: 21, rerun: 22,   // 锚定加噪, 审片模式, 自动保存, 重跑起始段
    reseam: 25, resth: 26, resmax: 27, decr: 29,    // 接缝重摇, 重摇阈值, 重摇上限, 递减锚定
    genmode: 33, ds: 34,                            // 生成模式, 导演台状态
};

/* 旧布局 wv → 当前 28 值布局。头部 0..16 原样；中段 8 个控件从 V35 对应位取
 * （更老布局这些位不存在则取 CUR_MID_DEFAULTS）；自动成片固定 "开启"；
 * 生成模式/导演台状态取尾部（35 值取末两位，更老布局取最后两元素）。 */
function remapOldWidgetValuesToCurrent(wv) {
    const head = wv.slice(0, 17);                    // 宽高比…回退上限（0..16）
    const pick = (i) => (i < wv.length ? wv[i] : undefined);
    const body = [
        pick(V35_PICK.aod), pick(V35_PICK.review), pick(V35_PICK.autosave), pick(V35_PICK.rerun),
        pick(V35_PICK.reseam), pick(V35_PICK.resth), pick(V35_PICK.resmax), pick(V35_PICK.decr),
    ];
    for (let i = 0; i < body.length; i++) {
        if (body[i] === undefined) body[i] = CUR_MID_DEFAULTS[i];
    }
    const tailGen = (wv.length >= V35_WIDGET_COUNT) ? pick(V35_PICK.genmode) : wv[wv.length - 2];
    const tailDs = (wv.length >= V35_WIDGET_COUNT) ? pick(V35_PICK.ds) : wv[wv.length - 1];
    return [
        ...head,
        ...body,
        (tailGen !== undefined ? tailGen : "文生视频"),  // 生成模式（idx25）
        "开启",                                         // 自动成片（新增控件，恒开启，idx26）
        (tailDs !== undefined ? tailDs : ""),           // 导演台状态（应为 JSON 字符串，idx27）
        "标准",                                          // 一采编码（新增控件，恒标准，idx28）
    ];
}

function migrateGraphWidgets(graphData) {
    if (!graphData || !Array.isArray(graphData.nodes)) return graphData;
    let migrated = 0;
    for (const n of graphData.nodes) {
        if (n.type !== NODE_TYPE || !Array.isArray(n.widgets_values) || !n.widgets_values.length) continue;
        const wv = n.widgets_values;
        // 已是当前合法布局则跳过；否则（极老数字首项 / 35 值陈旧 / 其它长短不一）统一迁移
        const legacy = (typeof wv[0] !== "string") || (wv.length !== CUR_WIDGET_COUNT);
        if (!legacy) continue;
        try {
            n.widgets_values = remapOldWidgetValuesToCurrent(wv);
            migrated += 1;
        } catch (e) {
            console.warn(`[h3-director] 节点 ${n.type} 参数迁移失败，保持原样交由兜底修正`, e);
        }
    }
    if (migrated) console.log(`[h3-director] 已迁移 ${migrated} 个旧版 H3 节点的参数（旧布局 → 当前 28 控件）`);
    return graphData;
}

/** 兜底：宽高比控件值非法（错位载入/手工改坏）时修正，避免后端换算报错 */
function fixInvalidArWidget(node) {
    if (!node) return;
    const w = (node.widgets || []).find((x) => x.name === W_AR);
    if (!w) return;
    const v = String(w.value ?? "");
    if (AR_LIST.includes(v)) return;
    const width = Number(getWidgetValue(node, W_WIDTH));
    const height = Number(getWidgetValue(node, W_HEIGHT));
    const combo = (Number.isFinite(width) && Number.isFinite(height)) ? matchCanvasCombo(width, height) : null;
    const fixed = combo ? combo[0] : "自定义";
    console.warn(`[h3-director] 「宽高比」控件值「${v}」无效，已修正为「${fixed}」（旧工作流请用 Load 按钮载入以完整迁移）`);
    setWidgetValue(node, W_AR, fixed);
    if (combo) setWidgetValue(node, W_MP, combo[1]);
}

/* ---------- 导演台状态（JSON widget 驱动，不操作画布连线） ---------- */

function defaultDs() {
    return { mode: MODE_DEFAULT, prompts: [""], first_frame: "", end_frame: "", last_frame: "", ref_images: [], ref_assets: [], segments: [], inserts: [], redo_segs: [], upscale: defaultUpscale(), experiments: defaultExperiments() };
}

function defaultUpscale() {
    /* 潜空间放大二采：主循环内渲染通道（每段采样定稿后、段落盘前），参数不进基础链指纹。
       schema 2：steps=尾段精化步数（与 denoise=尾段起始σ 解耦），旧 JSON 由 getDs 迁移。
       time_bias / mix / shift / stg / sharpen / pixel_sharpen 默认 0=关、adaptive / retry
       默认 false=关、passes=1=单轮、encode=标准、采样器空=沿用主链——仅启用时进后端
       指纹（不使既有记录失效）。抗糊武器库详见《更新说明_二采抗糊抗条纹》 */
    return { schema: 2, on: true, mode: "关闭", model: "", arch: "2D", scale: 2.0,
             denoise: 0.35, steps: 6, cfg: 1.0, precision: "fp16",
             time_bias: 0.0, mix: 0.0, adaptive: false, shift: 0.0,
             stg: 0.0, stg_block: 25, passes: 1, decay: 0.5,
             sharpen: 0.0, pixel_sharpen: 0.0, encode: "标准",
             sampler: "", scheduler: "", retry: false, retry_target: 0.15,
             include: [] };
}

function defaultSegment() {
    return { scene_prompt: "", character_prompt: "", soundscape: "", music: "", seconds: null, refs: [], unlink: false, disabled: false, frame_refs: null };
}

/** manifest.seg_fields[全局槽位] -> 分段字段对象（与 defaultSegment 同构）。
 *  旧存档无该键/槽位为 null（序章/插入槽）→ 全默认空段；键缺失容错补齐。 */
function restoreSegField(raw) {
    const base = defaultSegment();
    if (!raw || typeof raw !== "object") return base;
    const sec = Number(raw.seconds);
    return {
        scene_prompt: typeof raw.scene_prompt === "string" ? raw.scene_prompt : "",
        character_prompt: typeof raw.character_prompt === "string" ? raw.character_prompt : "",
        soundscape: typeof raw.soundscape === "string" ? raw.soundscape : "",
        music: typeof raw.music === "string" ? raw.music : "",
        seconds: (raw.seconds === null || raw.seconds === undefined
            || !Number.isFinite(sec) || sec <= 0) ? null : sec,
        refs: Array.isArray(raw.refs) ? raw.refs.map(String) : [],
        unlink: !!raw.unlink,
        disabled: !!raw.disabled,
        frame_refs: Array.isArray(raw.frame_refs) ? raw.frame_refs.map(String) : null,
    };
}

function getDs(node) {
    if (!node) return defaultDs();
    const w = (node.widgets || []).find((x) => x.name === W_DS);
    if (!w) return defaultDs();
    try {
        const raw = w.value ? JSON.parse(w.value) : {};
        const prompts = Array.isArray(raw.prompts) && raw.prompts.length ? raw.prompts.map(String) : [""];

        /* v2 标签素材池（含 kind 三类）；v1（只有 ref_images）自动迁移为图片 */
        let refAssets = Array.isArray(raw.ref_assets)
            ? raw.ref_assets.filter((a) => a && typeof a === "object" && a.file)
                .map((a) => ({
                    file: String(a.file),
                    kind: KIND_LIST.includes(a.kind) ? a.kind : "image",
                    label: cleanLabel(a.label) || "",
                }))
            : null;
        if (!refAssets) {
            const legacy = Array.isArray(raw.ref_images) ? raw.ref_images.filter(String).map(String) : [];
            refAssets = legacy.map((file) => ({ file, kind: "image", label: "" }));
        }
        const kindCount = { image: 0, video: 0, audio: 0 };
        refAssets.forEach((a) => {
            if (!a.label) { kindCount[a.kind] += 1; a.label = `${KIND_NAME[a.kind]}${kindCount[a.kind]}`; }
        });
        const taken = new Set();
        refAssets.forEach((a) => {
            a.label = uniqueLabelFrom(taken, a.label);
            taken.add(a.label);
        });

        let segments = Array.isArray(raw.segments) ? raw.segments : [];
        while (segments.length < prompts.length) segments.push(defaultSegment());
        if (segments.length > prompts.length) segments = segments.slice(0, prompts.length);
        const validLabels = new Set(refAssets.map((a) => a.label));
        segments = segments.map((s) => {
            const sec = Number(s?.seconds);
            return {
                scene_prompt: typeof s?.scene_prompt === "string" ? s.scene_prompt : "",
                character_prompt: typeof s?.character_prompt === "string" ? s.character_prompt : "",
                soundscape: typeof s?.soundscape === "string" ? s.soundscape : "",
                music: typeof s?.music === "string" ? s.music : "",
                seconds: (isFinite(sec) && sec > 0) ? Math.min(15, Math.max(0.5, sec)) : null,
                refs: Array.isArray(s?.refs) ? s.refs.map(String).filter((l) => validLabels.has(l)) : [],
                unlink: !!s?.unlink,
                disabled: !!s?.disabled,
                /* 段级首尾帧图引用：null=未设置（默认：首段参考首帧图、末段参考尾帧图）；
                 * 数组=显式勾选（可含 "首帧图"/"尾帧图"，空数组=两图都不参考） */
                frame_refs: Array.isArray(s?.frame_refs)
                    ? s.frame_refs.map(String).filter((v) => v === "首帧图" || v === "尾帧图")
                    : null,
            };
        });
        /* 插入视频段：{pos: 1-based 链位（不含序章）, file: input 目录文件名}；
         * 同位去重保首个、按链位升序（运行时后端会再校验，这里只做展示级归一） */
        const insSeen = new Set();
        const inserts = (Array.isArray(raw.inserts) ? raw.inserts : [])
            .filter((x) => x && typeof x === "object" && typeof x.file === "string"
                && x.file.trim() && Number.isInteger(Number(x.pos)) && Number(x.pos) >= 1)
            .filter((x) => (insSeen.has(x.pos) ? false : insSeen.add(x.pos)))
            .map((x) => ({ pos: Number(x.pos), file: x.file.trim() }))
            .sort((a, b) => a.pos - b.pos);
        /* 潜空间放大二采配置（v3 新增；旧 JSON 无此键=默认关闭，后端 parse_state 同口径）
           schema 2：steps=尾段精化步数；v1「调度总步数」在此迁移为 int(N×强度) 等价口径 */
        const upRaw = raw.upscale && typeof raw.upscale === "object" ? raw.upscale : {};
        const upNum = (v, def, lo, hi) => {
            const n = Number(v);
            const x = isFinite(n) ? n : def;
            return Math.min(hi, Math.max(lo, x));
        };
        const upSchema = Number.isInteger(Number(upRaw.schema)) ? Number(upRaw.schema) : 1;
        const upDenoise = upNum(upRaw.denoise, 0.35, 0.05, 1.0);
        /* v1「调度总步数」迁移为 int(N×强度) 等价口径；没存过 steps 的直接落新默认 6 */
        const hasUpSteps = upRaw.steps !== undefined && upRaw.steps !== null && upRaw.steps !== "";
        const upSteps = upSchema >= 2
            ? Math.round(upNum(upRaw.steps, 6, 1, 100))
            : (hasUpSteps
                ? Math.min(100, Math.max(1, Math.floor(upNum(upRaw.steps, 15, 1, 100) * upDenoise)))
                : 6);
        const upscale = {
            schema: 2,
            on: upRaw.on !== false,
            mode: UP_MODES.includes(upRaw.mode) ? upRaw.mode : "关闭",
            model: typeof upRaw.model === "string" ? upRaw.model : "",
            arch: upRaw.arch === "3D" ? "3D" : "2D",
            scale: upNum(upRaw.scale, 2.0, 1.0, 4.0),
            denoise: upDenoise,
            steps: upSteps,
            cfg: upNum(upRaw.cfg, 1.0, 0.0, 100.0),
            precision: UP_PRECISIONS.includes(upRaw.precision) ? upRaw.precision : "fp16",
            time_bias: upNum(upRaw.time_bias, 0.0, 0.0, 0.2),
            mix: upNum(upRaw.mix, 0.0, 0.0, 1.0),
            adaptive: upRaw.adaptive === true,
            shift: upNum(upRaw.shift, 0.0, 0.0, 100.0),
            /* 抗糊武器库（旧 JSON 缺键 = 默认关，后端 parse_state 同口径） */
            stg: upNum(upRaw.stg, 0.0, 0.0, 2.0),
            stg_block: Math.round(upNum(upRaw.stg_block, 25, 0, 49)),
            passes: Math.round(upNum(upRaw.passes, 1, 1, 3)),
            decay: upNum(upRaw.decay, 0.5, 0.2, 0.8),
            sharpen: upNum(upRaw.sharpen, 0.0, 0.0, 1.0),
            pixel_sharpen: upNum(upRaw.pixel_sharpen, 0.0, 0.0, 1.0),
            encode: UP_ENCODES.includes(upRaw.encode) ? upRaw.encode : "标准",
            sampler: typeof upRaw.sampler === "string" ? upRaw.sampler.trim() : "",
            scheduler: typeof upRaw.scheduler === "string" ? upRaw.scheduler.trim() : "",
            retry: upRaw.retry === true,
            retry_target: upNum(upRaw.retry_target, 0.15, 0.05, 1.0),
            include: (Array.isArray(upRaw.include) ? upRaw.include : [])
                .map((x) => Number(x)).filter((x) => Number.isInteger(x) && x >= 0),
        };
        return {
            mode: MODES.some(([m]) => m === raw.mode) ? raw.mode : MODE_DEFAULT,
            prompts,
            first_frame: typeof raw.first_frame === "string" ? raw.first_frame : "",
            end_frame: typeof raw.end_frame === "string" ? raw.end_frame : "",
            last_frame: typeof raw.last_frame === "string" ? raw.last_frame : "",
            ref_images: refAssets.filter((a) => a.kind === "image").map((a) => a.file),
            ref_assets: refAssets,
            segments,
            inserts,
            /* 重摇标记（选择性重做）：{slot: 0-based 全局槽位（含序章），mode: 锚定模式}。
               后端 _parse_redo_segs 同口径校验；旧 JSON 无此键 = 空（普通续跑） */
            redo_segs: (Array.isArray(raw.redo_segs) ? raw.redo_segs : [])
                .filter((x) => x && typeof x === "object" && Number.isInteger(Number(x.slot)))
                .map((x) => ({ slot: Number(x.slot), mode: REDO_MODES.some((r) => r[0] === x.mode) ? x.mode : "双锚" })),
            upscale,
            experiments: normalizeExperiments(raw.experiments),
        };
    } catch (e) {
        return defaultDs();
    }
}

function setDs(node, ds) {
    if (!node) return false;
    const w = (node.widgets || []).find((x) => x.name === W_DS);
    if (!w) return false;
    /* ref_images 兼容字段 = ref_assets 图片类文件序列（旧版前端/后端仍可读） */
    if (Array.isArray(ds.ref_assets)) {
        ds.ref_assets = ds.ref_assets.filter((a) => a && a.file);
        ds.ref_images = ds.ref_assets.filter((a) => (a.kind || "image") === "image")
            .map((a) => String(a.file));
    }
    /* 实验开关已是端到端扁平契约 {<id>:true, params:{...}}，直接序列化即存档格式 */
    w.value = JSON.stringify(ds);
    if (typeof w.callback === "function") {
        try { w.callback(w.value); } catch (e) { /* callback 可选 */ }
    }
    node.setDirtyCanvas(true, true);
    node.graph?.change?.();
    syncMirrors(node, ds);   // 状态写入统一同步画布镜像（提示词/首帧/素材，幂等）
    return true;
}

/** 写回模式控件（combo widget 同步，便于画布侧也能看到） */
function syncModeWidget(node, mode) {
    setWidgetValue(node, W_MODE, mode);
}

/** 模式互斥：在 JSON 状态层清空不相容字段 */
function applyModeDs(ds, mode) {
    if (mode === "文生视频") {
        ds.first_frame = "";
        ds.end_frame = "";
        ds.ref_assets = [];
    } else if (mode === "首帧视频") {
        ds.ref_assets = [];
    } else if (mode === "多参视频") {
        ds.first_frame = "";
        ds.end_frame = "";
    }
    ds.mode = mode;
}

function getMode(node) {
    return getDs(node).mode;
}

function setMode(node, mode) {
    const ds = getDs(node);
    applyModeDs(ds, mode);
    setDs(node, ds);
    syncModeWidget(node, mode);
    syncMirrors(node, ds);
    return true;
}

/* ---- 提示词（状态驱动） ---- */

function getPrompts(node) {
    return getDs(node).prompts;
}

function setPromptText(node, idx, text) {
    const ds = getDs(node);
    if (idx < 0 || idx >= ds.prompts.length) return false;
    ds.prompts[idx] = text;
    setDs(node, ds);
    schedulePromptFlush();   // 编辑即落盘（防抖）：提示词的持久源=项目文件夹
    return true;
}

function addPromptSegment(node) {
    const ds = getDs(node);
    if (ds.prompts.length >= MAX_SEG) { alert(`最多 ${MAX_SEG} 段提示词`); return; }
    ds.prompts.push("");
    if (!ds.segments) ds.segments = [];
    ds.segments.push(defaultSegment());
    setDs(node, ds);
    schedulePromptFlush();
    scheduleRefresh(60);
}

function removePromptSegment(node, idx) {
    const ds = getDs(node);
    if (idx < 0 || idx >= ds.prompts.length) return;
    ds.prompts.splice(idx, 1);
    if (ds.segments) ds.segments.splice(idx, 1);
    setDs(node, ds);
    schedulePromptFlush();
    scheduleRefresh(60);
}

function clearPrompts(node) {
    const ds = getDs(node);
    ds.prompts = ds.prompts.map(() => "");
    // 清文本但保留每段时长/素材引用/断链开关（结构设置跨项目沿用）
    if (ds.segments) ds.segments = ds.segments.map((s) => ({
        ...defaultSegment(), seconds: s?.seconds ?? null,
        refs: Array.isArray(s?.refs) ? s.refs : [], unlink: !!s?.unlink,
        disabled: !!s?.disabled,
    }));
    setDs(node, ds);
}

/* ---- 分段处理中心：场景/角色/声音提示词 + 每段时长 + 段级素材引用（状态驱动） ---- */

function getSegment(node, idx) {
    const ds = getDs(node);
    if (idx < 0 || idx >= (ds.segments || []).length) return defaultSegment();
    return ds.segments[idx];
}

function setSegmentField(node, idx, field, text) {
    const ds = getDs(node);
    if (idx < 0 || idx >= (ds.segments || []).length) return false;
    ds.segments[idx][field] = text;
    setDs(node, ds);
    return true;
}

/** 本段时长（秒）；v=null 表示跟随节点「每段时长」默认 */
function setSegmentSeconds(node, idx, v) {
    const ds = getDs(node);
    if (idx < 0 || idx >= (ds.segments || []).length) return false;
    const num = Number(v);
    ds.segments[idx].seconds = (v === "" || v == null || !isFinite(num) || num <= 0)
        ? null : Math.min(15, Math.max(0.5, num));
    setDs(node, ds);
    return true;
}

/** 勾选/取消本段引用的素材标签；全部取消 = 引用全部（与后端约定一致）。
 *  勾选时按类别校验官方单段上限（图9/视3/音3），超限拦截并提示。 */
function toggleSegmentRef(node, idx, label) {
    const ds = getDs(node);
    if (idx < 0 || idx >= (ds.segments || []).length) return;
    const seg = ds.segments[idx];
    if (!Array.isArray(seg.refs)) seg.refs = [];
    const pos = seg.refs.indexOf(label);
    if (pos >= 0) {
        seg.refs.splice(pos, 1);
    } else {
        const asset = (ds.ref_assets || []).find((a) => a.label === label);
        const kind = asset ? asset.kind : "image";
        const sameKind = seg.refs.filter((l) => {
            const a = (ds.ref_assets || []).find((x) => x.label === l);
            return a && a.kind === kind;
        }).length;
        if (sameKind >= KIND_CAPS[kind]) {
            alert(`本段引用${KIND_NAME[kind]}素材已达官方上限 ${KIND_CAPS[kind]} 个：请先取消一个再勾选「${label}」`);
            return;
        }
        seg.refs.push(label);
    }
    setDs(node, ds);
}

/** 勾选/取消本段的首尾帧图引用（仅首帧模式有首帧/尾帧图）。
 *  首次点选把缺省值物化为显式数组（null → 数组），之后再切换；改动进该段
 *  哈希（与默认不同时）——保存后重跑自动从该段起重做。 */
function toggleSegmentFrameRef(node, idx, name) {
    const ds = getDs(node);
    if (idx < 0 || idx >= (ds.segments || []).length) return;
    if (name !== "首帧图" && name !== "尾帧图") return;
    const seg = ds.segments[idx];
    if (!Array.isArray(seg.frame_refs)) {
        const n = (ds.prompts || ds.segments || []).length;
        seg.frame_refs = [];
        if (ds.first_frame && idx === 0) seg.frame_refs.push("首帧图");
        if (ds.end_frame && idx === n - 1) seg.frame_refs.push("尾帧图");
    }
    const pos = seg.frame_refs.indexOf(name);
    if (pos >= 0) seg.frame_refs.splice(pos, 1);
    else seg.frame_refs.push(name);
    setDs(node, ds);
}

/* ---- 首帧/参考图片（状态驱动 + 配套工作流 LoadImage 镜像点亮/隐藏） ---- */

function setFirstFrame(node, filename) {
    const ds = getDs(node);
    ds.first_frame = filename || "";
    setDs(node, ds);
    syncMirrors(node, ds);
    scheduleRefresh(120);
}

function addAsset(node, kind, filename) {
    const k = KIND_LIST.includes(kind) ? kind : "image";
    const ds = getDs(node);
    const sameKind = ds.ref_assets.filter((a) => a.kind === k).length;
    if (sameKind >= KIND_CAPS[k]) { alert(`${KIND_NAME[k]}素材最多 ${KIND_CAPS[k]} 个（官方单段上限）`); return; }
    const taken = new Set(ds.ref_assets.map((a) => a.label));
    const label = uniqueLabelFrom(taken, `${KIND_NAME[k]}${sameKind + 1}`);
    ds.ref_assets.push({ file: filename, kind: k, label });
    setDs(node, ds);
    syncMirrors(node, ds);
    scheduleRefresh(120);
}

function removeRefImage(node, idx) {
    const ds = getDs(node);
    if (idx < 0 || idx >= ds.ref_assets.length) return;
    const gone = ds.ref_assets.splice(idx, 1)[0];
    if (gone && gone.label) {
        for (const s of ds.segments || []) {
            if (Array.isArray(s.refs)) s.refs = s.refs.filter((l) => l !== gone.label);
        }
    }
    setDs(node, ds);
    syncMirrors(node, ds);
    scheduleRefresh(120);
}

/** 重命名素材标签：唯一性自动加后缀；同步重命名所有段级引用 */
function renameAssetLabel(node, idx, text) {
    const ds = getDs(node);
    const asset = ds.ref_assets[idx];
    if (!asset) return "";
    const clean = cleanLabel(text);
    if (!clean) return asset.label;   // 空输入保持原标签
    const old = asset.label;
    const taken = new Set(ds.ref_assets.filter((_a, i) => i !== idx).map((a) => a.label));
    asset.label = uniqueLabelFrom(taken, clean);
    if (old !== asset.label) {
        for (const s of ds.segments || []) {
            if (Array.isArray(s.refs)) s.refs = s.refs.map((l) => (l === old ? asset.label : l));
        }
    }
    setDs(node, ds);
    return asset.label;
}

function removeFirstFrame(node) {
    const ds = getDs(node);
    ds.first_frame = "";
    setDs(node, ds);
    syncMirrors(node, ds);
    scheduleRefresh(120);
}

/* 尾帧图片（FL2VA 剧情终点，仅首帧模式）：整链最后一段的末帧 keyframe，与首帧图片组成官方首尾帧 */
function setEndFrame(node, filename) {
    const ds = getDs(node);
    ds.end_frame = filename || "";
    setDs(node, ds);
    syncMirrors(node, ds);
    scheduleRefresh(120);
}

function removeEndFrame(node) {
    setEndFrame(node, "");
}

/* 每段尾帧锚定（身份锚点，任意模式可用）：与首帧同机制，但不随模式切换清空 */
function setLastFrame(node, filename) {
    const ds = getDs(node);
    ds.last_frame = filename || "";
    setDs(node, ds);
    syncMirrors(node, ds);
    scheduleRefresh(120);
}

function removeLastFrame(node) {
    setLastFrame(node, "");
}

/* ---- 总提示词（多段一次性导入 / 导出）----
 * 格式（skill「h3-video-prompts」同一规范，AI 按此生成的全文可直接粘贴分配）：
 *   【段1】          ← 段头：【段1】/【第1段】/【段落1】等价；序号可省略，按出现顺序排段
 *   场景：…         → seg.scene_prompt（风格契约：媒介+质感+调色板+光照）
 *   角色：…         → seg.character_prompt（外观+服饰+位置；弱编码器下逐段原样复用）
 *   环境音：…       → seg.soundscape（overall_soundscape；写每個声音及其进入时间）
 *   配乐：…         → seg.music（non_diegetic_music；乐器+节奏+进入时间）
 *   时长：5         → seg.seconds（可选，秒；缺省=沿用全局「每段时长」）
 *   独立镜头：是    → seg.unlink（是/否；是=断链不锚定上段尾帧，跳转/闪回/蒙太奇用）
 *   参考：角色1，图片2 → seg.refs（逗号/顿号分隔的素材标签；不存在的标签分配时剔除并提示）
 *   提示词：…       → 段主体（ds.prompts[i]），可多行（续行不写标签），官方 [2s-4s] 时间线写在这里
 *   【完】          ← 结束标记（可选）：其后的所有内容（如 AI 的参考素材建议）不参与解析
 * 规则：段头后未带标签的正文行视为提示词内容；只认上述 8 个行首标签，其余文本原样进
 * 主体（官方字段标签 integrated_multimodal_description: 等不受影响）；某标签「写了即生效
 * （含写空=清空），没写不动该字段」。整个文本无任何段头时视为单段主体。
 * 容错：markdown 渲染界面复制常把段内换行合并成空格（软换行丢失），整段糊成一行；
 * 检测到段头不在行首独占即自动「重分行」（见 mpReflow）再按常规行解析。 */
const MP_HEAD_RE = /^【\s*(?:第\s*)?(?:段(?:落)?\s*(\d+)?|(\d+)\s*段(?:落)?)\s*】\s*$/;
const MP_END_RE = /^【\s*(?:完|END|end|结束)\s*】$/;
const MP_FIELD_RE = /^(场景|角色|环境音|配乐|时长|独立镜头|参考|提示词)\s*[：:]\s*(.*)$/;
const MP_FIELDS = { "场景": "scene", "角色": "character", "环境音": "soundscape", "配乐": "music", "时长": "seconds", "独立镜头": "unlink", "参考": "refs", "提示词": "main" };
const MP_YES = ["是", "独立", "断链", "开", "true", "yes"];
const MP_NO = ["否", "连续", "关", "false", "no"];
const MP_HEAD_SUB_RE = /【\s*(?:第\s*)?(?:段(?:落)?\s*\d*|\d+\s*段(?:落)?)\s*】/;   // 段头子串版（无行锚，序号可省）

/* 软换行丢失容错（mpReflow）：聊天界面按 markdown 渲染 AI 输出时，段内单个换行
 * 复制后常变空格——段头正则要求独占一行，全文于是塌缩成「1 段」、八标签全部糊进
 * 主体且无任何警告。行内出现段头（非独占行）是这种走样的铁证，此时整篇重分行：
 * 段头与【完】前后插换行、行中标签前提行，再交回逐行解析。未走样的文本不改动
 * （正文里写「参考：」等字样不受影响——重分行仅在检测到走样后才发生）。 */
function mpReflow(text, notes) {
    const lines = String(text).split(/\r\n|\r|\n/);
    const degraded = lines.some((l) => { const t = l.trim(); return t && !MP_HEAD_RE.test(t) && MP_HEAD_SUB_RE.test(t); });
    if (!degraded) return text;
    notes.push("检测到段落结构被合并成单行（常见于从 markdown 渲染界面复制丢失换行），已自动重分行解析");
    const fieldSub = new RegExp("(" + Object.keys(MP_FIELDS).join("|") + ")\\s*[：:]", "g");
    const endSub = MP_END_RE.source.replace(/^\^|\$$/g, "");                 // 去行锚的【完】子串版
    return lines.map((l) =>
        l.replace(new RegExp(MP_HEAD_SUB_RE.source, "g"), "\n$&\n")          // 段头独占一行
            .replace(new RegExp(endSub, "g"), "\n$&\n")                      // 行内【完】独占一行截断
            .replace(fieldSub, (m, _p1, off, s) =>                            // 行中标签提到行首
                off === 0 || s[off - 1] === "\n" ? m : "\n" + m)
    ).join("\n");
}

function newMasterSeg() {
    return { main: undefined, scene: undefined, character: undefined, soundscape: undefined, music: undefined, seconds: undefined, unlink: undefined, refs: undefined };
}

/** 解析总提示词文本。返回 { segs:[{main,scene,character,soundscape,music,seconds}…],
 *  notes:[提示字符串…] }；字段 undefined=该块未写（应用时不覆盖），空串=显式清空。 */
function parseMasterPrompt(text) {
    const out = { segs: [], notes: [] };
    const src = mpReflow(String(text ?? ""), out.notes);   // 软换行丢失容错（未走样原样返回）
    const lines = src.split(/\r\n|\r|\n/);
    let cur = null;          // 当前段对象
    let field = null;        // 当前续行归属字段（"main" 等）
    const stray = [];        // 首个段头之前的游离行（无段头时整体作单段主体）
    const openSeg = () => { cur = newMasterSeg(); out.segs.push(cur); field = "main"; };
    for (const raw of lines) {
        const line = raw.trim();
        if (!line || /^```/.test(line)) continue;      // 空行与 markdown 代码围栏不参与解析
        if (MP_END_RE.test(line)) break;               // 【完】：其后的建议/解说不参与解析
        const hm = line.match(MP_HEAD_RE);
        if (hm) {
            openSeg();
            const n = hm[1] !== undefined || hm[2] !== undefined ? Number(hm[1] ?? hm[2]) : 0;
            if (n && n !== out.segs.length) {
                out.notes.push(`段头序号 ${n} 与出现顺序（第 ${out.segs.length} 段）不一致，已按出现顺序排列`);
            }
            continue;
        }
        const fm = line.match(MP_FIELD_RE);
        if (fm) {
            if (!cur) { stray.push(line); continue; }   // 字段行出现在任何段头之前
            const key = MP_FIELDS[fm[1]];
            if (key === "seconds") {
                const v = Number(fm[2]);
                cur.seconds = isFinite(v) && v > 0 ? v : undefined;
                if (!isFinite(v) || v <= 0) out.notes.push(`「时长：${fm[2]}」不是有效秒数，已忽略`);
            } else if (key === "unlink") {
                const v = fm[2].trim().toLowerCase();
                if (MP_YES.includes(v)) cur.unlink = true;
                else if (MP_NO.includes(v)) cur.unlink = false;
                else out.notes.push(`「独立镜头：${fm[2].trim()}」应为 是/否，已忽略`);
            } else if (key === "refs") {
                cur.refs = fm[2].split(/[，,、;；]+/).map((s) => s.trim()).filter(Boolean);
            } else {
                cur[key] = cur[key] === undefined ? fm[2].trim() : `${cur[key]}\n${fm[2].trim()}`;
            }
            field = key;
            continue;
        }
        if (!cur) { stray.push(line); continue; }       // 段头前的普通正文
        cur[field || "main"] = cur[field || "main"] === undefined
            ? line : `${cur[field || "main"]}\n${line}`;
    }
    if (!out.segs.length && stray.length) {             // 无段头=单段（正文原样作主体）
        openSeg();
        cur.main = stray.join("\n");
    } else if (stray.length) {
        out.notes.push(`忽略了 ${stray.length} 行出现在首个段头之前的内容`);
    }
    return out;
}

/** 把当前链的提示词导出为总提示词文本（只写非空字段，可回贴/喂给 AI 续改）。 */
function exportMasterPrompt(node) {
    const ds = getDs(node);
    const prompts = ds.prompts || [];
    const segs = ds.segments || [];
    const blocks = [];
    for (let i = 0; i < prompts.length; i++) {
        const seg = (i < segs.length && segs[i] && typeof segs[i] === "object") ? segs[i] : {};
        const rows = [`【段${i + 1}】`];
        if (seg.scene_prompt) rows.push(`场景：${seg.scene_prompt}`);
        if (seg.character_prompt) rows.push(`角色：${seg.character_prompt}`);
        if (seg.soundscape) rows.push(`环境音：${seg.soundscape}`);
        if (seg.music) rows.push(`配乐：${seg.music}`);
        if (Number.isFinite(Number(seg.seconds)) && Number(seg.seconds) > 0) rows.push(`时长：${seg.seconds}`);
        if (seg.unlink) rows.push("独立镜头：是");
        if (Array.isArray(seg.refs) && seg.refs.length) rows.push(`参考：${seg.refs.join("，")}`);
        const main = String(prompts[i] ?? "").trim();
        rows.push(main ? `提示词：${main}` : "提示词：");
        blocks.push(rows.join("\n"));
    }
    return blocks.join("\n\n") + "\n\n【完】";
}

/** 解析并分配到当前链：prompts 重排为 N 段，segments 同步伸缩（既有段保留 refs/unlink
 *  等未提及字段），插入视频段（inserts）不动；参考标签按「素材与参考」已有标签过滤
 *  （未知标签剔除并进 notes，防止运行期「引用未知素材标签」报错）。返回解析结果供界面提示。 */
function applyMasterPrompt(node, text) {
    const p = parseMasterPrompt(text);
    if (!p.segs.length) return p;
    const ds = getDs(node);
    const old = Array.isArray(ds.segments) ? ds.segments : [];
    const labels = new Set((Array.isArray(ds.ref_assets) ? ds.ref_assets : []).map((a) => a && a.label).filter(Boolean));
    ds.prompts = p.segs.map((s) => (s.main === undefined ? "" : s.main));
    ds.segments = p.segs.map((s, i) => {
        const base = (i < old.length && old[i] && typeof old[i] === "object") ? { ...old[i] } : defaultSegment();
        if (s.scene !== undefined) base.scene_prompt = s.scene;
        if (s.character !== undefined) base.character_prompt = s.character;
        if (s.soundscape !== undefined) base.soundscape = s.soundscape;
        if (s.music !== undefined) base.music = s.music;
        if (s.seconds !== undefined) base.seconds = s.seconds;
        if (s.unlink !== undefined) base.unlink = s.unlink;
        if (s.refs !== undefined) {
            const valid = s.refs.filter((r) => labels.has(r));
            const dropped = s.refs.filter((r) => !labels.has(r));
            if (dropped.length) p.notes.push(`段${i + 1} 参考标签不存在已剔除：${dropped.join("、")}（先在「素材与参考」上传素材获得标签）`);
            base.refs = valid;
        }
        return base;
    });
    setDs(node, ds);
    return p;
}

/* ---- 潜空间放大二采：状态读写（ds.upscale，主循环内逐段渲染：采样定稿后、段落盘前） ---- */

/* 二采模式（前端）：关闭 / 跟随生成。「手动选择」已从界面移除（勾选交互
   怪异且与单段二采按钮重复）——后端 parse_state 仍认它，仅作单段重新二采
   的内部提交通道（doUpscaleSeg 临时切换，用户不可见）；旧状态经 getDs
   归一自动迁移为「关闭」 */
const UP_MODES = ["关闭", "跟随生成"];

/* 重摇锚定模式（与后端 nodes.py _REDO_MODES 同表）：决定重摇本段时模型看向哪些锚点 */
const REDO_MODES = [
    ["双锚", "首锚=上段尾帧桥 · 尾锚=下段首帧：两端接缝都平滑，无缝替换（推荐）"],
    ["仅锚上段", "开头接续上段结尾，结尾自由发挥：接下来还打算重摇下一段时用"],
    ["仅锚下段", "开头重新起手，结尾接续下段首帧"],
    ["无锚", "完全自由发挥：两端硬切（独立镜头式重摇）"],
];
const UP_PRECISIONS = ["fp32", "fp16", "bf16"];
const UP_ENCODES = ["标准", "高清", "极致"];

function setUpscaleField(node, field, value) {
    const ds = getDs(node);
    if (field === "mode") {
        ds.upscale.mode = value;
        ds.upscale.include = [];   // 切模式清勾选（单段二采的临时勾选不跨模式残留）
    } else {
        ds.upscale[field] = value;
    }
    setDs(node, ds);
    scheduleRefresh(60);
    repaintUpscale();          // 编辑即刷新：徽章/参数区当场更新（见 repaintUpscale 注释）
}

/** 目标画布估算（与后端 target_hw 同口径：latent 偶数对齐=像素 32 倍数）。
 *  画幅来源与后端 _resolve_canvas / 链参数换算徽章同源：非「自定义」按 宽高比×百万像素
 *  换算——宽/高控件此时只是旧残留，直接读会算出与主徽章打架的错数；「自定义」才读宽高。 */
function upTargetCanvas(node, scale) {
    const ar = String(getWidgetValue(node, W_AR) ?? "");
    let w = 0, h = 0;
    if (AR_RATIO[ar]) {
        const c = resolveCanvas(ar, String(getWidgetValue(node, W_MP) ?? "0.5"));
        if (c) [w, h] = c;
    } else {
        w = Number(getWidgetValue(node, W_WIDTH));
        h = Number(getWidgetValue(node, W_HEIGHT));
    }
    if (!isFinite(w) || !isFinite(h) || !w || !h) return "";
    const even = (x) => { const v = Math.max(2, Math.round(x)); return v + v % 2; };
    return `${even(w / 16 * scale) * 16}×${even(h / 16 * scale) * 16}`;
}

/** 单段重新二采：清该段记录 → 临时切「手动选择+只勾本段」提交队列 → 立即还原设置。
 *  「手动选择」是后端保留的内部模式（界面上已无此选项）：parse_state 认它，
 *  doUpscaleSeg 借它实现"只重渲这一段"；其他段回放 fresh=False 直接沿用已有
 *  mp4，不会被基础分辨率覆盖。本段 latent 存档载入 → 神经放大重采样
 *  → 覆盖 seg_NNN.mp4，全程不碰视频编解码。
 *  二采渲染确定性：参数未变时重渲=同输出；价值在参数已变/记录缺失/补做时立即执行。
 *  健壮性（与 submitRedo 同口径）：①提交前自动套用存档共享参数——二采=同链重渲，
 *  后端 assert_match 硬校验参数一致，画布改过参数会整次运行失败（用户只看到
 *  "二采没生效"）；②临时清重摇标记/关审片逐段确认——残留的重摇会劫持本次提交
 *  （redo 优先走重采样），逐段审片会把全链回放打断成多次运行；③提交前校验放大
 *  权重文件在位——换卡/重装后权重丢失时 load_net 降级只有报告行，提前 alert；
 *  ④队列提交失败不再只 console.warn，LED 直接报出来。 */
async function doUpscaleSeg(btn, dir, segNo, dispNo, done, total) {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点"); return; }
    if (mergeSel.on) { alert("合并模式进行中：请先完成或退出合并导出"); return; }
    const ds = getDs(node);
    if (!ds.upscale?.model) {
        alert("请先在右栏「二采面板」选择放大模型，再对本段执行二采。");
        return;
    }
    if (done < total
        && !confirm(`链未完成（${done}/${total} 段）：本次运行会先继续生成剩余段落，`
            + `再对段 ${dispNo} 执行二采（本次新生成的段按当前模式处理）。\n继续？`)) return;
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "提交中…";
    try {
        /* 0. 放大权重在位校验：文件丢失时后端 load_net 降级只有报告行，
              这里提前拦下（换卡/重装 ComfyUI 后 models 目录易缺文件） */
        const um = await apiGet("/h3chain/upscale_models");
        if (Array.isArray(um?.models) && um.models.length
            && !um.models.includes(ds.upscale.model)) {
            alert(`放大模型「${ds.upscale.model}」不在 models/latent_upscale_models/ 目录里`
                + "——请重新选择权重文件后再试。");
            return;
        }
        /* 1. 清该段二采记录（幂等，无记录无害）——保证强制重做而非沿用 */
        const r = await api.fetchApi("/h3chain/upscale_reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ dir, seg: segNo }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || !j.ok) {
            if (r.status === 404 || r.status === 405) {
                setApiError(`二采接口未注册（HTTP ${r.status}）：请重启 ComfyUI 并检查控制台「路由已注册」日志。`);
            } else {
                alert(`重新二采失败：${j.error || `HTTP ${r.status}`}`);
            }
            return;
        }
        /* 2. 存档参数静默套用（后端 assert_match 同口径）——二采段与链上其余段
              共享锚定与采样配置，参数漂移会让整次运行在校验处失败 */
        let restored = [];
        const mf = await fetchJson(`h3_projects/${dir}`, "manifest.json");
        if (mf?.params) restored = applyChainParams(node, mf.params);
        /* 3. 临时切手动选择+只勾本段提交（原模式"关闭"时 parse_state 返回 None 不执行）；
              同时清残留重摇标记（redo 优先会劫持成本段重采样）、关审片逐段确认
              （会把全链回放打断成多次运行）——提交后立即全部还原，
              队列快照已在服务端，用户界面不残留临时值 */
        const prevMode = ds.upscale.mode;
        const prevInc = [...(ds.upscale.include || [])];
        const prevRedo = [...(ds.redo_segs || [])];
        const prevReview = getWidgetValue(node, "审片模式") ?? "关闭";
        const prevReroll = getWidgetValue(node, W_REROLL) ?? 0;
        setWidgetValue(node, W_REROLL, 0);
        setWidgetValue(node, "审片模式", "关闭");
        ds.upscale.mode = "手动选择";
        ds.upscale.include = [segNo - 1];
        ds.redo_segs = [];
        setDs(node, ds);
        syncModeWidget(node, ds.mode);
        setLed("running", `段 ${dispNo} 重新二采已提交（其余段照常回放）`
            + (restored.length ? `；已自动还原存档参数：${restored.join("、")}` : ""));
        try {
            await app.queuePrompt();
        } catch (e) {
            console.warn("[h3-director] queue failed:", e);
            setApiError(`段 ${dispNo} 重新二采提交队列失败：${e}——`
                + "该段二采记录已清（下次开启二采的运行会自动补渲染）");
        }
        /* 4. 全量还原（含重摇标记与审片模式） */
        const ds2 = getDs(node);
        ds2.upscale.mode = prevMode;
        ds2.upscale.include = prevInc;
        ds2.redo_segs = prevRedo;
        setDs(node, ds2);
        setWidgetValue(node, W_REROLL, prevReroll);
        setWidgetValue(node, "审片模式", prevReview);
        scheduleRefresh();
    } catch (e) {
        alert("重新二采请求失败：" + e);
    } finally {
        btn.disabled = false;
        btn.textContent = old;
    }
}

/* ---- 配套工作流：素材/提示词节点镜像（连线常驻，仅切换 点亮/隐藏） ---- */

function mirrorNodeByTitle(title) {
    return (app.graph?._nodes || []).find((n) => n.getTitle?.() === title || n.title === title) || null;
}

/** 点亮/隐藏配套工作流节点并同步其值：有值=mode0+展开，无值=mode2+折叠（连线与配置保留）。
 *  widgetNames 为候选控件名列表（LoadImage=image / LoadVideo=file / LoadAudio=audio / Primitive=value），
 *  都找不到时退回第一个控件。 */
function setMirrorNode(title, value, widgetNames) {
    const m = mirrorNodeByTitle(title);
    if (!m) return false;
    let w = null;
    for (const n of (widgetNames || [])) {
        w = (m.widgets || []).find((x) => x.name === n);
        if (w) break;
    }
    w = w || (m.widgets || [])[0];
    if (w && value && String(w.value ?? "") !== String(value)) {
        w.value = value;
        if (typeof w.callback === "function") { try { w.callback(value); } catch (e) { /* 可选 */ } }
    }
    const want = value ? 0 : 2;
    if (m.mode !== want) m.mode = want;
    m.flags = m.flags || {};
    m.flags.collapsed = !value;
    m.setDirtyCanvas?.(true, true);
    return true;
}

/** 全量同步镜像：首帧图 + 参考图·1..9 + 参考视频·1..3（联动拆分视频）+ 参考音频·1..3 + 提示词·1..3。
 *  仅导演台素材/提示词操作时调用，不动手摆工作流（找不到同名节点=手摆，静默跳过）。 */
function syncMirrors(node, ds) {
    if (!node || !ds) return;
    const byKind = (k) => (Array.isArray(ds.ref_assets) ? ds.ref_assets.filter((a) => a.kind === k) : []);
    setMirrorNode("首帧图", ds.mode === "首帧视频" ? (ds.first_frame || "") : "", ["image"]);
    setMirrorNode("目标尾帧图", ds.mode === "首帧视频" ? (ds.end_frame || "") : "", ["image"]);   // 尾帧图片（FL2VA）：仅首帧模式
    setMirrorNode("尾帧图", ds.last_frame || "", ["image"]);   // 每段尾帧锚定：任意模式可用
    const multi = ds.mode === "多参视频";
    const imgs = multi ? byKind("image") : [];
    for (let i = 0; i < 9; i++) {
        setMirrorNode(`参考图·${i + 1}`, imgs[i] ? String(imgs[i].file) : "", ["image"]);
    }
    const vids = multi ? byKind("video") : [];
    for (let i = 0; i < 3; i++) {
        const f = vids[i] ? String(vids[i].file) : "";
        setMirrorNode(`参考视频·${i + 1}`, f, ["file"]);
        setMirrorNode(`拆分视频·${i + 1}`, f);   // 与 LoadVideo 同步点亮/隐藏（hidden=断链）
    }
    const auds = multi ? byKind("audio") : [];
    for (let i = 0; i < 3; i++) {
        setMirrorNode(`参考音频·${i + 1}`, auds[i] ? String(auds[i].file) : "", ["audio"]);
    }
    const prompts = Array.isArray(ds.prompts) ? ds.prompts.map((p) => String(p || "")) : [];
    for (let i = 0; i < 3; i++) {
        const t = prompts[i] && prompts[i].trim() ? prompts[i] : "";
        setMirrorNode(`提示词·${i + 1}`, t, ["value"]);
    }
}

/** 一键载入配套默认工作流（画布无 H3 节点时的引导入口） */
async function loadDefaultWorkflow() {
    if (!window.H3_DEFAULT_WORKFLOW) {
        alert("配套工作流模板未加载：请确认插件 web/ 目录含 h3_default_workflow.js 并刷新页面");
        return;
    }
    if (!confirm("将载入配套工作流并替换当前画布（未保存的画布修改会丢失），继续？")) return;
    try {
        const p = app.loadGraphData(JSON.parse(JSON.stringify(window.H3_DEFAULT_WORKFLOW)));
        // 新版 ComfyUI 前端 loadGraphData 返回 Promise；极老旧前端返回 undefined，按成功降级处理
        if (p && typeof p.then === "function") await p;
    } catch (e) {
        console.error("[h3-director] 载入配套工作流失败", e);
        alert("载入配套工作流失败：" + (e && e.message ? e.message : e) + "\n请检查 H3_DEFAULT_WORKFLOW 模板与当前后端节点版本是否一致。");
        return;
    }
    await new Promise((r) => setTimeout(r, 150));
    const node = findNode();
    if (node && app.canvas?.centerOnNode) { try { app.canvas.centerOnNode(node); } catch (e) { /* 可选 */ } }
    const ds = getDs(node);
    syncMirrors(node, ds);
    scheduleRefresh(300);
}

/** 文件选择 + 上传：target="first"=首帧图；target="end"=尾帧图片（FL2VA）；target="last"=每段尾帧锚定；kind=入素材池。
 *  上传统一走 /upload/image（服务端按原样字节写入 input 目录，不限图片）。 */
async function pickAsset(target, kind) {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点"); return; }
    const input = document.createElement("input");
    input.type = "file";
    input.accept = (target === "first" || target === "end" || target === "last") ? "image/*" : KIND_ACCEPT[kind] || "image/*";
    input.onchange = async () => {
        const f = input.files && input.files[0];
        if (!f) return;
        try {
            const name = await uploadToInput(f);
            if (target === "first") {
                setFirstFrame(node, name);
            } else if (target === "end") {
                setEndFrame(node, name);
            } else if (target === "last") {
                setLastFrame(node, name);
            } else {
                addAsset(node, kind, name);
            }
        } catch (e) {
            alert(`上传失败：${e}`);
        }
    };
    input.click();
}

/* ---- 插入视频段（导演台状态 ds.inserts：画面+原声进成片，尾帧桥指导下一段） ---- */

/** 追加插入段到指定链位（1-based，不含序章）。链位 = 提示词段+插入段混排后的位置：
 *  pos=2 → 第 1 个提示词段占链位 1，插入视频占链位 2，原第 2 个提示词段顺延链位 3。 */
function appendInsert(pos, file) {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点"); return; }
    const ds = getDs(node);
    const maxPos = ds.prompts.length + ds.inserts.length + 1;
    pos = Math.max(1, Math.min(Math.round(Number(pos) || 1), maxPos));
    ds.inserts = ds.inserts.filter((x) => !(x.pos === pos && x.file === file));
    ds.inserts.push({ pos, file });
    ds.inserts.sort((a, b) => a.pos - b.pos);
    setDs(node, ds);
    scheduleRefresh();
}

function removeInsert(pos, file) {
    const node = findNode();
    if (!node) return;
    const ds = getDs(node);
    ds.inserts = ds.inserts.filter((x) => !(x.pos === pos && x.file === file));
    setDs(node, ds);
    scheduleRefresh();
}

function pickInsertVideo(pos) {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点"); return; }
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "video/*";
    input.onchange = async () => {
        const f = input.files && input.files[0];
        if (!f) return;
        try {
            const name = await uploadToInput(f);
            appendInsert(pos, name);
        } catch (e) {
            alert(`上传失败：${e}`);
        }
    };
    input.click();
}

/* ---- 合并导出（勾选态纯内存，POST /h3chain/merge 流式拼接成 merged_*.mp4） ---- */

/** 上传外部视频追加到合并清单末尾（input 目录，不进链，仅作拼接素材）。 */
function pickMergeVideo() {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "video/*";
    input.onchange = async () => {
        const f = input.files && input.files[0];
        if (!f) return;
        try {
            const name = await uploadToInput(f);
            mergeSel.files = [...(mergeSel.files || []), name];
            scheduleRefresh(0);
        } catch (e) {
            alert(`上传失败：${e}`);
        }
    };
    input.click();
}

async function doMergeExport(btn) {
    const dir = lastDir;
    if (!dir) { alert("没有当前项目目录（先读档或运行一次）"); return; }
    const items = [
        ...[...mergeSel.segs].sort((a, b) => a - b).map((s) => ({ seg: s })),
        ...(mergeSel.files || []).map((f) => ({ file: f })),
    ];
    if (!items.length) { alert("先勾选要合并的段（或上传外部视频）"); return; }
    const oldTxt = btn.textContent;
    btn.disabled = true;
    btn.textContent = "合并中…";
    setLed("running", `合并 ${items.length} 项拼接中`);
    try {
        const r = await api.fetchApi("/h3chain/merge", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ dir, items }),
        });
        const j = await r.json().catch(() => ({}));
        if (r.ok && j.ok) {
            setLed("done", `已合并 → ${j.file}`);
            mergeSel.on = false; mergeSel.segs = []; mergeSel.files = [];
            refresh();
            return;
        }
        if (r.status === 404 || r.status === 405) {
            setApiError(`合并接口未注册（HTTP ${r.status}）：请重启 ComfyUI 并检查控制台「路由已注册」日志。`);
        } else {
            alert(`合并失败：${j.error || `HTTP ${r.status}`}`);
        }
    } catch (e) {
        alert("合并请求失败：" + e);
    } finally {
        btn.disabled = false;
        btn.textContent = oldTxt;
    }
}

/* ---- 提示词写回防抖 ---- */

const _taTimers = new Map();

function debouncePromptWrite(node, idx, text) {
    const key = `p${idx}`;
    const old = _taTimers.get(key);
    if (old) clearTimeout(old);
    _taTimers.set(key, setTimeout(() => {
        _taTimers.delete(key);
        setPromptText(node, idx, text);
    }, 350));
}

function debounceSegmentWrite(node, idx, field, text) {
    const key = `s${idx}_${field}`;
    const old = _taTimers.get(key);
    if (old) clearTimeout(old);
    _taTimers.set(key, setTimeout(() => {
        _taTimers.delete(key);
        setSegmentField(node, idx, field, text);
    }, 350));
}

/* ---- 段落计划（状态驱动：提示词段 + 插入视频段按链位混排，不含序章） ---- */

function planFromDs(node) {
    const ds = getDs(node);
    const prompts = ds.prompts.map((t, i) => ({ text: t.trim(), idx: i }));
    const items = (ds.inserts || []).map((x) => [x.pos, x.file]);   // getDs 已去重排序
    const plan = [];
    let pi = 0;
    for (const [pos, file] of items) {
        while (plan.length + 1 < pos) {
            if (pi < prompts.length) plan.push({ kind: "prompt", ...prompts[pi++] });
            else break;
        }
        plan.push({ kind: "insert", pos, file });
    }
    while (pi < prompts.length) plan.push({ kind: "prompt", ...prompts[pi++] });
    const drafts = prompts.filter((p) => !p.text).map((p) => `段 ${p.idx + 1}`);
    return { plan, drafts, ds };
}

/** 拖拽调序纯函数：把提示词段 dragIdx 移到「插入线」处，返回新 prompts 序
 *  （null = 非法或原位不动）。ins = 线在当前 plan 的位置（0..plan.length）。
 *  规则：新序位 k = 线上方其余提示词段数（其余段保持相对序，仅拖拽段换位）。
 *  插入段 pos 固定不动——线落在插入段与提示词段之间且线上的提示词不足时
 *  该位置不可达（返回 null/无操作），此时插到链尾等价于跨插入段下移。 */
function reorderChain(prompts, plan, dragIdx, ins) {
    if (!Array.isArray(prompts) || !Array.isArray(plan)) return null;
    if (!Number.isInteger(dragIdx) || dragIdx < 0 || dragIdx >= prompts.length) return null;
    if (!Number.isInteger(ins) || ins < 0 || ins > plan.length) return null;
    if (!plan.some((it) => it?.kind === "prompt" && it.idx === dragIdx)) return null;
    let k = 0;
    for (let i = 0; i < ins; i++) {
        const it = plan[i];
        if (it?.kind === "prompt" && it.idx !== dragIdx) k += 1;
    }
    if (k === dragIdx) return null;                    // 原位（线在自身上下沿）
    const without = prompts.filter((_x, i) => i !== dragIdx);
    const out = without.slice(0, k);
    out.push(prompts[dragIdx]);
    out.push(...without.slice(k));
    return out;
}

/** 提交拖拽调序：prompts/segments 按同一置换搬移（段级属性随段走）。
 *  链序变化由哈希机制自动级联重做（首个变化段起截断重生成，接缝重新衔接）。
 *  拖动范围内的段槽位整体错位一位——清掉该范围内的重摇标记与二采勾选
 *  （范围外的段槽位不变，标记仍有效；manifest 运行队列随截断自动清空）。 */
function applyReorder(node, data, dragIdx, ins) {
    const ds = getDs(node);
    const perm = reorderChain((ds.prompts || []).map((_x, i) => i), data.plan, dragIdx, ins);
    if (!perm) return;
    const oldPrompts = [...(ds.prompts || [])];
    const oldSegs = [...(ds.segments || [])];
    ds.prompts = perm.map((i) => oldPrompts[i]);
    ds.segments = perm.map((i) => oldSegs[i] || defaultSegment());
    /* 拖动范围 = 拖拽段新旧 plan 位之间（新位 = 现 rank-k 提示词段的位置） */
    const k = perm.indexOf(dragIdx);
    const posOf = (rank) => {
        let c = -1;
        for (let i = 0; i < data.plan.length; i++) {
            if (data.plan[i]?.kind === "prompt") {
                c += 1;
                if (c === rank) return i;
            }
        }
        return data.plan.length;
    };
    const lo = Math.min(posOf(dragIdx), posOf(k));
    const hi = Math.max(posOf(dragIdx), posOf(k));
    const off = planOff(data.mf);
    const inRange = (slot) => { const i = Number(slot) - off; return i >= lo && i <= hi; };
    ds.redo_segs = (ds.redo_segs || []).filter((x) => x && !inRange(x.slot));
    setDs(node, ds);
    setLed("idle", `段序已调整：从首个变化段起自动级联重做（换位段的接缝需重新生成）`);
    scheduleRefresh(60);
}

/** ⬆/⬇ 兜底调序的落线位置（拖不动时可用）：按提示词段名次换位，
 *  中间隔着插入视频时一并跨过（与拖拽同语义，走同一条 applyReorder 路径）。
 *  up=与上一名次段换位；down=与下一名次段换位。返回 null=已在边界不可移。 */
function nudgeIns(data, dragIdx, up) {
    const plan = data.plan || [];
    const pos = [];   // pos[rank] = 该名次提示词段的 plan 下标
    let r = -1;
    plan.forEach((it, i) => {
        if (it?.kind !== "prompt") return;
        pos.push(i);
        if (it.idx === dragIdx) r = pos.length - 1;
    });
    if (r < 0) return null;
    if (up) {
        if (r === 0) return null;
        return pos[r - 1];           // 线放上一名次段之前 → 目标名次 r-1
    }
    if (r >= pos.length - 1) return null;   // 已是最后一段
    /* 线放「下下名次段」之前（前方恰有 r+1 个非拖拽段）；无下下名次则线放链尾 */
    return r + 2 < pos.length ? pos[r + 2] : plan.length;
}

/** 指针 Y 在卡片列表中的插入线位置（0..卡数）：越过某卡中点=线在该卡前 */
function dropIndexAt(wrap, y) {
    const cards = [...wrap.querySelectorAll(":scope > .h3d-card")];
    for (let i = 0; i < cards.length; i++) {
        const r = cards[i].getBoundingClientRect();
        if (y < r.top + r.height / 2) return i;
    }
    return cards.length;
}

/** 插入线吸附显示：只在「可落位」处画线（原位/不可达不显示），
 *  线画在拖拽段将占据的位置（现 rank-k 提示词卡之前 / 链尾），所见即所得 */
function setDropLine(wrap, data, dragIdx, ins) {
    wrap.querySelectorAll(".h3d-drop-before, .h3d-drop-end")
        .forEach((n) => n.classList.remove("h3d-drop-before", "h3d-drop-end"));
    const n = (data.ds?.prompts || []).length;
    const perm = reorderChain([...Array(n).keys()], data.plan, dragIdx, ins);
    if (!perm) return;
    const k = perm.indexOf(dragIdx);
    const cards = [...wrap.querySelectorAll(":scope > .h3d-card")];
    let rank = 0, shown = false;
    for (let i = 0; i < data.plan.length && i < cards.length; i++) {
        if (data.plan[i]?.kind !== "prompt") continue;
        if (rank === k) { cards[i].classList.add("h3d-drop-before"); shown = true; break; }
        rank += 1;
    }
    if (!shown && cards.length) cards[cards.length - 1].classList.add("h3d-drop-end");
}

/* ---- 槽位口径：plan 索引（不含序章） <-> 后端全局槽位（含序章） ---- */

/** manifest 槽位偏移：序章（起始视频接线）占全局槽 0，plan 索引从序章之后起算。
 *  后端 seeds/thumbs/videos/二采记录/重摇队列全部按全局槽位索引——
 *  序章项目必须 +off 换算，否则卡片媒体/勾选/重摇错位一段。 */
function planOff(mf) {
    return mf?.has_prologue ? 1 : 0;
}

/** 重摇标记合并视图 slot -> mode：manifest 运行队列（已提交）打底，ds 标记（未提交）
 *  覆盖同槽位——与后端"ds 优先合并队列"同规则（改模式=重新标记即可覆盖队列）。 */
function redoMap(ds, mf) {
    const m = new Map();
    for (const x of (mf?.redo_queue || [])) {
        if (Array.isArray(x) && Number.isInteger(Number(x[0]))) {
            m.set(Number(x[0]), REDO_MODES.some((r) => r[0] === x[1]) ? String(x[1]) : "双锚");
        }
    }
    for (const x of (ds?.redo_segs || [])) {
        if (x && Number.isInteger(Number(x.slot))) {
            m.set(Number(x.slot), REDO_MODES.some((r) => r[0] === x.mode) ? x.mode : "双锚");
        }
    }
    return m;
}

/** 槽位可重摇校验：落在"已完成的启用提示词段"上才有效
 *  ——与后端 _parse_redo_segs 同口径（未完成/插入段/序章/越界/已禁用不重摇） */
function redoSlotValid(slot, mf, plan, ds) {
    const off = planOff(mf);
    const done = Math.max(0, (mf?.done ?? 0) - off);
    const i = slot - off;
    if (!(i >= 0 && i < done && i < (plan || []).length && plan[i]?.kind === "prompt")) return false;
    const seg = (ds?.segments || [])[plan[i].idx];
    return !seg?.disabled;
}

/** 未提交标记数（ds.redo_segs，页脚「重摇已标记 N 段」CTA 计数）。
 *  manifest 队列另算（redoQueued）：提交后 ds 清空、队列接管徽章显示。 */
function redoPending(data) {
    let n = 0;
    for (const x of (data?.ds?.redo_segs || [])) {
        if (x && Number.isInteger(x.slot) && redoSlotValid(x.slot, data.mf, data.plan, data.ds)) n += 1;
    }
    return n;
}

/** manifest 重摇队列剩余条目数（已提交未执行完：审片逐段推进时每运行一段停一次，
 *  剩余段驻留队列等下次运行——页脚显示「继续重摇剩余 N 段」） */
function redoQueued(data) {
    let n = 0;
    for (const x of (data?.mf?.redo_queue || [])) {
        if (Array.isArray(x) && Number.isInteger(Number(x[0]))
            && redoSlotValid(Number(x[0]), data.mf, data.plan, data.ds)) n += 1;
    }
    return n;
}

function queuePrompt() {
    if (mergeSel.on) { alert("合并模式进行中：请先完成或退出合并导出，再提交生成"); return; }
    const node = findNode();
    if (node) {
        const ds = getDs(node);
        setDs(node, ds);
        syncModeWidget(node, ds.mode);
    }
    setLed("running", "已提交队列");
    app.queuePrompt();
    scheduleRefresh();
}

function scheduleRefresh(delay = 900) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, delay);
}

/* ---------- 项目动作（游戏式存读档） ---------- */

/** 提交重摇（页脚 CTA）：随机种子 + 临时开审片（逐段推进：每重摇一段暂停审看），
 *  队列快照在服务端——提交后立即恢复控件并清 ds 标记（manifest 队列接管徽章显示）。
 *  redo 与「重跑起始段」互斥（后端 redo 优先）：提交时显式清零避免旧值干扰。
 *  共享参数自动套用存档值（applyChainParams）：重摇=同链换种子重做，参数必须与
 *  存档一致，画布控件若在成链后被改过会在后端 assert_match 硬报错——静默纠偏，
 *  实际改动项写进 LED 让用户知情。 */
async function submitRedo() {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点"); return; }
    if (mergeSel.on) { alert("合并模式进行中：请先完成或退出合并导出，再提交重摇"); return; }
    let restored = [];
    if (lastDir) {
        const mf = await fetchJson(`h3_projects/${lastDir}`, "manifest.json");
        if (mf?.params) restored = applyChainParams(node, mf.params);
    }
    const prevReview = getWidgetValue(node, "审片模式") ?? "关闭";
    const prevReroll = getWidgetValue(node, W_REROLL) ?? 0;
    if (!setWidgetValue(node, "审片模式", "逐段确认")) {
        alert("节点上没有「审片模式」控件：请重新载入配套工作流");
        return;
    }
    setWidgetValue(node, W_REROLL, 0);
    setWidgetValue(node, W_SEED, Math.floor(Math.random() * 2 ** 48));
    const ds = getDs(node);          // redo_segs 已在弹窗标记时写入
    setDs(node, ds);
    syncModeWidget(node, ds.mode);
    setLed("running", "重摇已提交（逐段审片推进，满意后继续）"
        + (restored.length ? `；已自动还原存档参数：${restored.join("、")}` : ""));
    try {
        await app.queuePrompt();
    } catch (e) {
        console.warn("[h3-director] queue failed:", e);
    }
    setWidgetValue(node, "审片模式", prevReview);
    setWidgetValue(node, W_REROLL, prevReroll);
    const ds2 = getDs(node);
    ds2.redo_segs = [];
    setDs(node, ds2);
    scheduleRefresh();
}

/* ---- 重摇锚定模式选择模态（段卡片「🎲 重摇」入口） ---- */

/** 标记/修改/取消重摇：弹窗选锚定模式 → 写入 ds.redo_segs（不立即执行），
 *  多段分别标记后由页脚「重摇已标记 N 段」统一提交。
 *  已标记段再点 = 改模式或「取消重摇标记」（撤销 ds 标记，并调
 *  /h3chain/redo_cancel 幂等清掉已入队的同槽位条目——覆盖提交后反悔场景）。 */
function openRerollModal(idx, data) {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点"); return; }
    if (mergeSel.on) { alert("合并模式进行中：请先完成或退出合并导出，再标记重摇"); return; }
    if (document.querySelector(".h3d-overlay")) return;

    const { mf, plan, ds, state } = data;
    const slot = idx + planOff(mf);
    if (!redoSlotValid(slot, mf, plan, ds)) return;
    const cur = redoMap(ds, mf).get(slot);
    let picked = cur ?? "双锚";

    const overlay = el("div", "h3d-overlay");
    const dialog = el("div", "h3d-dialog");
    dialog.innerHTML = `
        <h3>🎲 重摇段 ${idx + 1}</h3>
        <p class="h3d-lead">只重做这一段（换种子重新采样），其余段保留不动——
        可先给多段分别标记，再点页脚「重摇已标记 N 段」一次提交（互不级联）。
        锚定模式决定重采样时模型看向哪些接缝锚点（与本段「独立镜头」属性无关）。</p>
        <div class="h3d-sub">锚定模式${cur ? `（当前：${cur}）` : ""}</div>`;
    const optrow = el("div", "h3d-optrow");
    REDO_MODES.forEach(([m, tip]) => {
        const lab = el("label", "h3d-opt" + (m === picked ? " on" : ""));
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "h3d-redo-mode";
        radio.checked = m === picked;
        radio.onchange = () => {
            picked = m;
            optrow.querySelectorAll(".h3d-opt").forEach((x) => x.classList.remove("on"));
            lab.classList.add("on");
        };
        const txt = el("div", "");
        txt.innerHTML = `<b>${m}</b><small>${tip}</small>`;
        lab.append(radio, txt);
        optrow.append(lab);
    });
    const err = el("div", "h3d-err", "");
    const row = el("div", "h3d-dialog-row");
    const cancel = el("button", "h3d-btn", "取消");
    const unmark = el("button", "h3d-btn h3d-btn-danger", "取消重摇标记");
    const ok = el("button", "h3d-btn h3d-btn-cta", cur ? "更新标记" : "标记重摇");
    if (cur) row.append(unmark);
    row.append(cancel, ok);
    dialog.append(optrow, err, row);
    overlay.append(dialog);
    overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.append(overlay);
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") overlay.remove(); });
    optrow.querySelector("input:checked")?.focus();   // 聚焦使 Escape 可关

    const close = () => overlay.remove();
    cancel.onclick = close;

    /* 标记/改模式：upsert 进 ds.redo_segs（其余段标记不动），卡片立即出徽章 */
    ok.onclick = () => {
        const d = getDs(node);
        const rest = (d.redo_segs || []).filter((x) => x && Number(x.slot) !== slot);
        d.redo_segs = [...rest, { slot, mode: picked }].sort((a, b) => a.slot - b.slot);
        setDs(node, d);
        setLed("idle", `段 ${idx + 1} 已标记重摇（${picked}）：可继续标记其他段，或点页脚提交`);
        close();
        scheduleRefresh(60);
    };

    /* 撤销标记：清 ds 条目 + 幂等清 manifest 队列同槽位条目（接口未注册时
       仅清 ds——队列条目留待运行消费，不阻断） */
    unmark.onclick = async () => {
        unmark.disabled = true;
        unmark.textContent = "撤销中…";
        try {
            const d = getDs(node);
            d.redo_segs = (d.redo_segs || []).filter((x) => x && Number(x.slot) !== slot);
            setDs(node, d);
            if (state?.dir) {
                const r = await api.fetchApi("/h3chain/redo_cancel", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ dir: state.dir, slot }),
                });
                if (r.status === 404 || r.status === 405) {
                    setApiError(`撤销接口未注册（HTTP ${r.status}）：请重启 ComfyUI 并检查控制台「路由已注册」日志。`
                        + `本次仅撤销了未提交标记，已入队条目需运行消费。`);
                } else if (!r.ok) {
                    const j = await r.json().catch(() => ({}));
                    err.textContent = `撤销失败：${j.error || `HTTP ${r.status}`}`;
                    unmark.disabled = false;
                    unmark.textContent = "取消重摇标记";
                    return;
                }
            }
            setLed("idle", `段 ${idx + 1} 重摇标记已撤销`);
            close();
            scheduleRefresh(60);
        } catch (e) {
            err.textContent = "撤销请求失败：" + e;
            unmark.disabled = false;
            unmark.textContent = "取消重摇标记";
        }
    };
}

/** 只跑某一段（预览下一段）：审片模式组合，顺序生成段 done+1 后暂停（无损）。
 *  segNo = done+1：不设重跑 → 顺序生成下一段后暂停；
 *  segNo > done+1：链式续拍必须按顺序，拦截并说明；
 *  segNo <= done：已完成段不走此入口（「重摇」只重做本段 /「从这段继续」级联重做）。
 *  队列提交后立即还原控件（提示词快照已在服务端，用户界面不残留临时值）。 */
async function doRunOnly(segNo, done) {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点"); return; }
    if (!Number.isInteger(segNo) || segNo < 1) return;
    if (segNo !== done + 1) {
        alert(`链式续拍需按顺序生成：段 ${done + 1} 尚未完成。\n`
            + `请先连续生成到段 ${done + 1}（普通「▶ 继续」每次一段）；`
            + `已完成段想重做请用「🎲 重摇」（只重做该段）或「▶ 从这段继续」（其后全部重做）。`);
        return;
    }
    const prevReview = getWidgetValue(node, "审片模式") ?? "关闭";
    const prevReroll = getWidgetValue(node, W_REROLL) ?? 0;
    if (!setWidgetValue(node, "审片模式", "逐段确认")) {
        alert("节点上没有「审片模式」控件：请重新载入配套工作流");
        return;
    }
    setWidgetValue(node, W_REROLL, 0);
    const ds = getDs(node);
    setDs(node, ds);
    syncModeWidget(node, ds.mode);
    setLed("running", `只跑段 ${segNo} 已提交`);
    try {
        await app.queuePrompt();
    } catch (e) {
        console.warn("[h3-director] queue failed:", e);
    }
    setWidgetValue(node, "审片模式", prevReview);
    setWidgetValue(node, W_REROLL, prevReroll);
    scheduleRefresh();
}

/** 从段 idx+1 之后继续生成到底（游戏读档语义）：
 *  保留第 1..n 段（n=idx+1，plan 口径），n 之后有旧存档时先截断（弹确认），
 *  然后连续生成到链尾。「重跑起始段」= 全局 1-based（序章占 1），提交后立即还原——
 *  残留旧值会让下一次普通运行再次截断（本次修复的隐患）。 */
async function continueFromSegment(idx, done, off) {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点"); return; }
    const n = idx + 1;
    if (n < done
        && !confirm(`从段 ${n + 1} 继续生成？\n段 ${n + 1} 之后的旧存档会被丢弃，从段 ${n + 1} 起逐段重新生成到链尾。`)) return;
    const prevReroll = getWidgetValue(node, W_REROLL) ?? 0;
    setWidgetValue(node, W_REROLL, n < done ? n + (off || 0) + 1 : 0);
    setLed("running", `从段 ${Math.min(n + 1, done + 1)} 续拍已提交`);
    await queuePrompt();
    setWidgetValue(node, W_REROLL, prevReroll);
}

/** 删除项目 = 删除整个项目文件夹（分段视频/成片/提示词/latent 全删）。 */
async function deleteProject(dir) {
    if (!dir) return;
    if (!confirm(`删除项目「${dir}」？\n\n`
        + `将删除整个文件夹：output/h3_projects/${dir}\n`
        + `（全部分段视频、成片、提示词清单、续拍 latent 一并删除）\n\n不可恢复，继续？`)) return;
    try {
        const r = await api.fetchApi("/h3chain/delete_project", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ dir }),
        });
        if (r.status === 404 || r.status === 405) {
            setApiError(`项目接口未注册（HTTP ${r.status}）：请重启 ComfyUI 并检查控制台是否出现`
                + `「[ComfyUI_H3_SeamlessChain] 路由已注册」日志；若仍失败请把控制台报错反馈给开发。`);
            setLed("error", `删除路由未注册 (HTTP ${r.status})`);
            return;
        }
        if (!r.ok) {
            const j = await r.json().catch(() => ({}));
            alert(`删除失败：${j.error || `HTTP ${r.status}`}`);
            return;
        }
        setLed("idle", "项目已删除");
        scheduleRefresh(300);
    } catch (e) {
        alert(`删除失败：${e}`);
    }
}

/** 提示词回写：把画布当前提示词组 + 分段处理字段存进「存档目录」指向的项目
 *  manifest——项目提示词的持久源=项目文件夹。三个触发点：
 *  ① 切换项目前（旧项目先落盘，否则画布状态被覆盖即丢失）；
 *  ② 新建项目前（同上）；
 *  ③ 编辑后 1.5s 防抖自动回写（断电/崩溃也不丢提示词草稿）。
 *  seg_fields 与 prompts 同序（仅提示词段，全局槽位由后端对齐）——切换项目
 *  丢分段字段（场景/角色/声音框清空）的修复正依赖这里先存后切。
 *  项目目录/manifest 不存在（未新建未跑过的指纹目录）→ 404 静默跳过；
 *  失败仅 console 警告，绝不阻断切换流程。 */
async function flushPrompts(node, dir) {
    node = node || findNode();
    if (!node) return false;
    const target = dir || getDirValue(node);
    if (!target) return false;
    const ds = getDs(node);
    const segments = (ds.segments || []).map((s) => s ? {
        scene_prompt: String(s.scene_prompt || ""),
        character_prompt: String(s.character_prompt || ""),
        soundscape: String(s.soundscape || ""),
        music: String(s.music || ""),
        seconds: (s.seconds === null || s.seconds === undefined || s.seconds === ""
            || !Number.isFinite(Number(s.seconds))) ? null : Number(s.seconds),
        unlink: !!s.unlink,
        disabled: !!s.disabled,
        refs: Array.isArray(s.refs) ? s.refs.map(String) : [],
        frame_refs: Array.isArray(s.frame_refs) ? s.frame_refs.map(String) : null,
    } : null);
    try {
        const r = await api.fetchApi("/h3chain/save_prompts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ dir: target, prompts: (ds.prompts || []).map(String), segments }),
        });
        if (r.status === 404) return false;   // 无 manifest 的目录：跳过不报错
        if (!r.ok) { console.warn("[h3-director] save_prompts HTTP", r.status); return false; }
        return true;
    } catch (e) {
        console.warn("[h3-director] save_prompts failed:", e);
        return false;
    }
}

let promptFlushTimer = 0;
function schedulePromptFlush() {
    clearTimeout(promptFlushTimer);
    promptFlushTimer = setTimeout(() => { flushPrompts(); }, 1500);
}

/** 读档：切换「存档目录」+ 载入该项目的提示词进导演台状态（后端续跑校验共享参数）。
 *  切走前先把当前提示词回写旧项目（否则覆盖即丢）；序章项目的 prompts[0]
 *  是「序章（上传视频）」占位（对应起始视频，不是提示词框），载入时跳过。 */
async function switchProject(dir) {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点（只读模式）"); return; }
    clearTimeout(promptFlushTimer);                             // 取消挂起的防抖：防止迟到的回写打到新项目
    const oldDir = getDirValue(node);
    if (oldDir && oldDir !== dir) await flushPrompts(node, oldDir);
    if (!setDirValue(node, dir)) { alert("节点上没有「存档目录/断点目录」控件"); return; }
    setWidgetValue(node, W_REROLL, 0);
    const r = await apiGet(`/h3chain/project?dir=${encodeURIComponent(dir)}`);
    const mf = r.ok ? (r.data?.manifest || null) : null;
    if (mf) {
        const off = mf.has_prologue ? 1 : 0;
        const plist = (mf.prompts || []).slice(off);
        if (plist.length || mf.total) {
            const ds = getDs(node);
            /* 重建提示词段与插入段：prompts 行按全局槽位对齐，插入槽是
             * 「[插入视频] 文件名」占位行——跳过并按 manifest.inserts
             * （slot → 链位 pos = slot + 1 - off）还原 ds.inserts */
            const insByPos = {};
            for (const x of (mf.inserts || [])) {
                if (x && x.file) {
                    const pos = Number(x.slot) - off + 1;
                    if (Number.isInteger(pos) && pos >= 1) insByPos[pos] = String(x.file);
                }
            }
            const texts = [];
            const segs = [];
            const inserts = [];
            /* seg_fields 按全局槽位对齐（序章/插入槽 null）：plist[j] 的全局槽位
             * = j + off——还原本项目的分段字段（场景/角色/环境音/配乐等），
             * 不再沿用切走前项目的旧值（切回即丢正是本修复目标） */
            const fields = Array.isArray(mf.seg_fields) ? mf.seg_fields : [];
            let pos = 0;
            for (let j = 0; j < plist.length; j++) {
                pos += 1;
                if (insByPos[pos]) { inserts.push({ pos, file: insByPos[pos] }); continue; }
                if (String(plist[j]).startsWith("[插入视频]")) continue;   // 无 inserts 记录的残留占位
                texts.push(String(plist[j] ?? ""));
                segs.push(restoreSegField(fields[j + off]));
            }
            ds.prompts = texts;
            ds.segments = segs.length ? segs : Array.from({ length: texts.length }, () => defaultSegment());
            ds.inserts = inserts;
            setDs(node, ds);
        }
        setLed("idle", `已读档「${mf.title || dir}」（${mf.total ? `${mf.done ?? 0}/${mf.total} 段` : "草稿，未配置段落"}）`);
    } else {
        setLed("idle", `已指向 ${dir}`);
    }
    scheduleRefresh(200);
}

/** 把 manifest 共享参数静默映射回画布控件，返回实际改动（值未变不记）的描述列表。
 *  覆盖进指纹的全部共享参数：画幅/时长/引导帧数/步数/CFG/采样器/调度器/
 *  递减锚定/桥帧门控三件套——重摇提交前自动调用纠偏参数漂移（后端
 *  assert_match 同口径），也供手动「套用参数」按钮复用。
 *  自定义画幅（无 AR×MP 命中）同时把宽高比切到「自定义」：非自定义时后端
 *  会按宽高比×MP 重新换算宽高，只写宽/高控件不生效。 */
function applyChainParams(node, params) {
    if (!node || !params) return [];
    const applied = [];
    /* setWidgetValue 的带回报版：值变化才写并记录描述（幂等重放不产生噪音） */
    const put = (name, value, label) => {
        const wd = (node.widgets || []).find((x) => x.name === name);
        if (!wd || String(wd.value ?? "") === String(value)) return false;
        return setWidgetValue(node, name, value) ? (applied.push(label), true) : false;
    };
    const w = Number(params.width), h = Number(params.height);
    if (w && h) {
        const combo = matchCanvasCombo(w, h);
        if (combo) {
            const oldAr = String(getWidgetValue(node, W_AR) ?? "");
            const oldMp = String(getWidgetValue(node, W_MP) ?? "");
            setWidgetValue(node, W_AR, combo[0]);
            setWidgetValue(node, W_MP, combo[1]);
            if (oldAr !== combo[0] || oldMp !== String(combo[1])) {
                applied.push(`画幅 ${combo[0]}·${combo[1]}MP（${w}×${h}）`);
            }
        } else {
            const old = [String(getWidgetValue(node, W_AR) ?? ""),
                String(getWidgetValue(node, W_WIDTH) ?? ""),
                String(getWidgetValue(node, W_HEIGHT) ?? "")];
            setWidgetValue(node, W_AR, "自定义");
            setWidgetValue(node, W_WIDTH, w);
            setWidgetValue(node, W_HEIGHT, h);
            if (old[0] !== "自定义" || old[1] !== String(w) || old[2] !== String(h)) {
                applied.push(`宽×高 ${w}×${h}（自定义）`);
            }
        }
    }
    if (params.length) {
        const sec = +(params.length / 24).toFixed(2);
        put(W_DUR, sec, `每段时长 ${sec}s`);
    }
    if (params.ctx != null) {
        const v = String(params.ctx);
        const wd = (node.widgets || []).find((x) => x.name === "引导帧数");
        if (!wd || (wd.options?.options || []).includes(v)) {
            put("引导帧数", v, `引导帧数 ${v}`);
        }
    }
    if (params.steps) put("步数", params.steps, `步数 ${params.steps}`);
    if (params.cfg != null) put("CFG", params.cfg, `CFG ${params.cfg}`);
    for (const [name, key] of [["采样器", "sampler"], ["调度器", "scheduler"]]) {
        const v = params[key];
        if (!v) continue;
        const wd = (node.widgets || []).find((x) => x.name === name);
        if (wd && !(wd.options?.options || []).includes(v)) continue;
        put(name, v, `${name} ${v}`);
    }
    if (params.fade_ratio != null) {
        const v = Number(params.fade_ratio) === 0 ? "关闭" : String(params.fade_ratio);
        put("递减锚定", v, `递减锚定 ${v}`);
    }
    const gate = params.gate || {};
    if (gate.mode) put("桥帧门控", gate.mode, `桥帧门控 ${gate.mode}`);
    if (gate.threshold != null) put("清晰度阈值", gate.threshold, `清晰度阈值 ${gate.threshold}`);
    /* 回退上限：指纹存的是 //17*17 对齐值，直接写回同值（limit 已对齐再对齐不变） */
    if (gate.limit != null) put("回退上限", gate.limit, `回退上限 ${gate.limit}`);
    return applied;
}

/** 把项目 manifest 的共享参数映射回画布控件（画幅优先尝试 宽高比×MP combo 匹配）。 */
function applyParamsToCanvas(node, params) {
    const applied = applyChainParams(node, params);
    scheduleRefresh(300);
    alert(applied.length
        ? `已套用参数到画布：\n\n${applied.join("\n")}`
        : "当前画布参数已与存档一致（或无可套用的参数）");
}

/* ---------- LED 状态灯 ---------- */

function setLed(phase, text) {
    ledPhase = phase;
    if (text) ledText = text;
    paintLeds();
}

function paintLeds() {
    document.querySelectorAll(".h3d-led").forEach((node) => {
        node.className = `h3d-led ${ledPhase}`;
        const em = node.querySelector("em");
        if (em) em.textContent = ledText;
    });
}

/* ---------- 数据汇总 ---------- */

function paramsSummary(node, mf) {
    const p = (mf && mf.params) || {};
    const gw = (name) => {
        const w = node && (node.widgets || []).find((x) => x.name === name);
        return w ? String(w.value ?? "").trim() : "";
    };
    /* 画幅：优先 宽高比+百万像素 换算，自定义/非法回落 宽×高（或存档指纹） */
    const ar = AR_RATIO[gw(W_AR)] ? gw(W_AR) : "";
    let geo = "";
    if (ar) {
        const c = resolveCanvas(ar, gw(W_MP) || "0.5");
        geo = c ? `${c[0]}×${c[1]}` : "—";
    } else {
        const w = p.width || gw(W_WIDTH);
        const h = p.height || gw(W_HEIGHT);
        geo = w && h ? `${w}×${h}` : "—";
    }
    const durRaw = Number(p.length ? p.length / 24 : gw(W_DUR));
    const len = isFinite(durRaw) && durRaw > 0
        ? `${(p.length ? p.length / 24 : durRaw).toFixed(1)}s/段${p.length ? `(${p.length}f)` : ""}` : "";
    const ctx = p.ctx || gw("引导帧数");
    return {
        geo,
        len,
        ctx: ctx ? `引导${ctx}帧` : "",
    };
}

/** 项目总时长（秒）：生成段按 ds.segments[i].seconds（缺省=节点默认），插入段按默认估 */
function chainSeconds(node, ds, plan) {
    const defRaw = Number(getWidgetValue(node, W_DUR));
    const def = isFinite(defRaw) && defRaw > 0 ? defRaw : 5.0;
    let total = 0;
    for (let i = 0; i < (plan || []).length; i++) {
        const it = plan[i];
        if (it.kind === "prompt" && it.idx !== undefined) {
            const s = ds?.segments?.[it.idx];
            total += s?.seconds ?? def;
        } else {
            total += def;   // 插入视频时长未知，按默认估
        }
    }
    return total;
}

async function collectData() {
    const node = findNode();
    if (node) fixInvalidArWidget(node);

    /* 诊断 + 项目列表（后端实时扫描磁盘，一个项目一个文件夹） */
    const ping = await apiGet("/h3chain/ping");
    let projects = [];
    let upscaleModels = [];
    if (ping.ok) {
        const r = await apiGet("/h3chain/projects");
        if (r.ok) projects = r.data?.projects || [];
        const um = await apiGet("/h3chain/upscale_models");
        if (um.ok) upscaleModels = um.data?.models || [];
        loadExperimentDefs();    // 实验定义随刷新周期尽早到达（函数自身幂等）
    }
    setApiError(ping.ok ? "" :
        `项目存档接口未注册（HTTP ${ping.status || "??"}）：请重启 ComfyUI 并检查控制台是否出现`
        + `「[ComfyUI_H3_SeamlessChain] 路由已注册」日志；若仍失败请把控制台报错反馈给开发。`);

    /* 当前项目：节点「存档目录」指向优先（用户刚切换还没跑），回落 state 指针 */
    const stateRaw = await fetchJson("h3_projects", "h3chain_state.json");
    const nodeDir = node ? String(getDirValue(node) || "").trim() : "";
    const dir = nodeDir || stateRaw?.dir || "";
    lastDir = dir;                                           // 合并导出等即时动作取当前项目
    const mf = dir ? await fetchJson(`h3_projects/${dir}`, "manifest.json") : null;
    const state = stateRaw || {};
    const sameChain = !!stateRaw && dir === stateRaw.dir;
    state.dir = dir;
    state.done = mf?.done ?? (sameChain ? stateRaw?.done : 0) ?? 0;
    state.total = mf?.total ?? (sameChain ? stateRaw?.total : 0) ?? 0;


    let plan = null;
    let drafts = [];
    let ds = defaultDs();
    if (node) {
        ({ plan, drafts, ds } = planFromDs(node));
    }
    if (!plan || !plan.length) {
        const total = state.total ?? (mf && mf.total) ?? 0;
        if (total && node) {
            // 采纳存档提示词进导演台状态：卡片可编辑（此前回退成只读历史段，导致无法输入）
            // 插入槽占位行（[插入视频] 文件名）跳过，ds.inserts 按 manifest.inserts 重建
            const dsAdopt = getDs(node);
            const off = mf && mf.has_prologue ? 1 : 0;
            const insByPos = {};
            for (const x of ((mf && mf.inserts) || [])) {
                if (x && x.file) {
                    const p = Number(x.slot) - off + 1;
                    if (Number.isInteger(p) && p >= 1) insByPos[p] = String(x.file);
                }
            }
            const texts = [];
            const segs = [];
            const inserts = [];
            const fields = Array.isArray(mf?.seg_fields) ? mf.seg_fields : [];
            let pos = 0;
            let j = 0;
            for (const row of (mf && mf.prompts || []).slice(off)) {
                pos += 1;
                if (insByPos[pos]) { inserts.push({ pos, file: insByPos[pos] }); j += 1; continue; }
                if (String(row).startsWith("[插入视频]")) { j += 1; continue; }
                texts.push(String(row ?? ""));
                segs.push(restoreSegField(fields[j + off]));
                j += 1;
            }
            dsAdopt.prompts = texts;
            dsAdopt.segments = segs.length ? segs
                : Array.from({ length: texts.length }, (_v, i) => dsAdopt.segments?.[i] ?? defaultSegment());
            dsAdopt.inserts = inserts;
            setDs(node, dsAdopt);
            ({ plan, drafts, ds } = planFromDs(node));
        } else if (total) {
            plan = Array.from({ length: total }, (_v, i) => {
                const ins = ((mf && mf.inserts) || []).find((x) => x.slot === i);
                const row = String(((mf && mf.prompts) || [])[i] || "");
                return ins ? { kind: "insert", pos: i + 1, file: ins.file || "" }
                    : row.startsWith("[插入视频]") ? { kind: "insert", pos: i + 1, file: row.slice(6).trim() }
                    : { kind: "prompt", text: row };
            });
        } else {
            plan = [];
        }
    }
    return { node, state, mf, plan, drafts, ds, projects, apiOk: ping.ok, upscaleModels };
}

function statusLine(state, mf, plan) {
    const off = planOff(mf);
    const total = plan ? plan.length : Math.max(0, (mf?.total ?? state?.total ?? 0) - off);
    const done = Math.max(0, (mf?.done ?? state?.done ?? 0) - off);
    if (!total) return { text: "尚未配置段落：在流水线卡片填写提示词，或点「＋ 添加一段」", next: false };
    if (done >= total) return { text: `本链已全部完成（${total}/${total} 段）✓ 可「＋ 新建项目」开下一条`, next: false };
    const anchored = done > 0 ? `，锚定段 ${done} 尾部` : "";
    return { text: `已载入段 1–${done}（存档回放）→ 本次将生成段 ${done + 1}${anchored}`, next: true };
}

function segMediaHtml(state, mf, idx) {
    const done = mf?.done ?? 0;
    const g = idx + planOff(mf);   // 全局槽位：序章占 0，manifest 数组按全局槽位索引
    if (g >= done || !state?.dir) return null;
    const sub = `h3_projects/${state.dir}`;
    const thumbFile = (mf.thumbs || [])[g];
    const videoFile = (mf.videos || [])[g];
    const thumbSrc = thumbFile ? viewUrl(sub, thumbFile) : "";
    if (videoFile) {
        return `<video class="h3d-segvideo" controls preload="metadata"${thumbSrc ? ` poster="${thumbSrc}"` : ""} src="${viewUrl(sub, videoFile)}"></video>`;
    }
    if (thumbSrc) return `<img loading="lazy" src="${thumbSrc}" alt="段${idx + 1}">`;
    return null;
}

/* ---------- 样式（参照一体化总控导演台，前缀 h3d-） ---------- */

const STYLE_ID = "h3-director-style";

function injectStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const style = el("style");
    style.id = STYLE_ID;
    style.textContent = `
    :root{--h3d-ink:#1c2128;--h3d-panel:#22272e;--h3d-panel2:#2d333b;--h3d-line:#444c56;--h3d-cyan:#6cb6ff;--h3d-copper:#daaa3f;--h3d-bone:#cdd9e1;--h3d-muted:#909dab;--h3d-ok:#57ab5a;--h3d-warn:#e0823d;--h3d-danger:#e5534b}
    .h3d-page *,.h3d-mini *,.h3d-dialog *{box-sizing:border-box}
    @keyframes h3d-blink{50%{opacity:.35}}
    .h3d-led{display:inline-flex;gap:7px;align-items:center;color:var(--h3d-muted);font-size:12px;white-space:nowrap}
    .h3d-led i{width:8px;height:8px;border-radius:50%;background:#636e7b;flex:none}
    .h3d-led.running i{background:var(--h3d-cyan);box-shadow:0 0 10px #6cb6ffcc;animation:h3d-blink 1s infinite}
    .h3d-led.done i{background:var(--h3d-ok);box-shadow:0 0 12px #57ab5a88}
    .h3d-led.error i{background:var(--h3d-warn);box-shadow:0 0 12px #e0823d88}
    .h3d-btn{cursor:pointer;border:1px solid #444c56;border-radius:6px;background:#2d333b;color:var(--h3d-bone);padding:6px 11px;font-size:12px;font-family:inherit;transition:border-color .12s,filter .12s}
    .h3d-btn:hover{filter:brightness(1.18)}
    .h3d-btn:disabled{opacity:.5;cursor:not-allowed;filter:none}
    .h3d-btn-cyan{border-color:#316dca;background:#1f2f45;color:#9ecbff}
    .h3d-btn-danger{border-color:#9a4144;background:#3a2225;color:#f0a0a4}
    .h3d-btn-cta{border:0;background:linear-gradient(135deg,#f0c274,#e6b566 55%,#d99e4a);color:#1a1408;font-weight:700;box-shadow:0 2px 14px #e6b56633}
    .h3d-btn-cta:hover{filter:brightness(1.08);box-shadow:0 3px 18px #e6b56644}
    .h3d-chip{font-size:10.5px;padding:2px 8px;border-radius:10px;border:1px solid #444c56;background:#2d333b;color:#adbac7;white-space:nowrap}
    .h3d-chip.ok{border-color:#2ea04366;background:#12261e;color:#7ee2a8}
    .h3d-chip.media{border-color:#7a5f36;background:#352a19;color:#e9c07a}
    .h3d-chip.cyan{border-color:#316dca;background:#1f2f45;color:#9ecbff}
    .h3d-chip.warn{border-color:#9a4144;background:#402227;color:#f0a0a4}

    /* ---- 侧栏迷你入口卡 ---- */
    .h3d-mini{display:flex;flex-direction:column;gap:9px;padding:10px;font-size:12px;color:var(--h3d-bone);background:linear-gradient(150deg,#22272e,#2d333b);border:1px solid #444c56;border-radius:10px}
    .h3d-mini-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
    .h3d-mini-brand{font-weight:700;letter-spacing:.04em;min-width:0}
    .h3d-mini-brand small{display:block;margin-top:3px;color:var(--h3d-cyan);font-weight:500;font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .h3d-mini-rail{display:flex;gap:3px}
    .h3d-mini-rail span{flex:1;height:4px;border-radius:4px;background:#444c56}
    .h3d-mini-rail span.done{background:var(--h3d-cyan)}
    .h3d-mini-rail span.next{background:#6e7b8c;animation:h3d-blink 1.2s infinite}
    .h3d-mini-cards{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px}
    .h3d-mini-card{min-height:62px;padding:9px;border:1px solid #3f4854;border-radius:8px;background:#1b2027;font-size:11px;color:var(--h3d-muted);line-height:1.65;overflow:hidden}
    .h3d-mini-count{display:grid;place-items:center;padding:9px 10px;min-width:66px;border:1px solid #3f4854;border-radius:8px;background:#1b2027;font:700 20px/1.15 ui-monospace,Consolas;color:var(--h3d-cyan);text-align:center}
    .h3d-mini-count small{font-size:10px;color:var(--h3d-muted);font-weight:400}
    .h3d-mini-foot{display:flex;flex-direction:column;gap:8px}
    .h3d-mini-params{color:var(--h3d-muted);font:10.5px ui-monospace,Consolas;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .h3d-mini-open{width:100%;padding:9px}

    /* ---- 全屏导演台 ---- */
    .h3d-page{position:fixed;inset:0;z-index:1000000;background:var(--h3d-ink);color:var(--h3d-bone);font:13px/1.5 "Microsoft YaHei UI","Segoe UI",sans-serif;display:grid;grid-template-rows:58px 1fr 58px}
    .h3d-topbar{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:16px;padding:0 20px;border-bottom:1px solid var(--h3d-line);background:linear-gradient(90deg,#22272e,#262c36)}
    .h3d-top-left{min-width:0;display:flex;gap:14px;align-items:center}
    .h3d-kicker{color:var(--h3d-copper);font:700 11px/1 ui-monospace,Consolas;letter-spacing:.18em;white-space:nowrap}
    .h3d-title{font-size:17px;font-weight:700;white-space:nowrap}
    .h3d-sub{min-width:0;color:var(--h3d-muted);font-family:ui-monospace,Consolas;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .h3d-top-right{display:flex;gap:14px;align-items:center;justify-self:end}
    .h3d-close{width:36px;height:36px;border:1px solid var(--h3d-line);border-radius:7px;background:#2d333b;color:var(--h3d-bone);cursor:pointer;font-size:15px}
    .h3d-close:hover{filter:brightness(1.2)}
    .h3d-banner{display:none;padding:9px 18px;border-bottom:1px solid #8a4a3f;background:#3a2620;color:#ffc9b8;font-size:12px;line-height:1.6}
    .h3d-stage{min-height:0;display:grid;grid-template-columns:minmax(255px,300px) minmax(400px,1fr) minmax(255px,300px);gap:1px;background:var(--h3d-line)}
    .h3d-col{min-width:0;min-height:0;background:var(--h3d-panel);overflow:auto}
    .h3d-sechead{position:sticky;top:0;z-index:3;padding:14px 16px 10px;background:#22272eee;backdrop-filter:blur(8px);border-bottom:1px solid #3f4854}
    .h3d-sechead strong{display:block}
    .h3d-sechead small{color:var(--h3d-muted)}

    .h3d-projlist{display:grid;gap:7px;padding:12px}
    .h3d-projrow{display:flex;gap:6px;align-items:stretch}
    .h3d-projrow .h3d-proj{flex:1;min-width:0}
    .h3d-proj-del{flex:none;width:30px;border:1px solid #4c3a3d;border-radius:8px;background:#2a2225;color:#b08a8e;cursor:pointer;font-size:13px;line-height:1;transition:.15s}
    .h3d-proj-del:hover{border-color:#c2565f;background:#3a2429;color:#ffb3b8}
    .h3d-proj{display:grid;grid-template-columns:44px 1fr;grid-template-rows:auto auto;gap:2px 10px;padding:8px 10px 9px 13px;border:1px solid #3f4854;border-radius:8px;background:#262c36;cursor:pointer;box-shadow:inset 3px 0 0 #444c56;text-align:left;font-family:inherit;color:inherit}
    .h3d-proj:hover{background:#2a313b}
    .h3d-proj.active{box-shadow:inset 3px 0 0 var(--h3d-cyan);border-color:#316dca}
    .h3d-proj-cover{grid-row:1/3;width:44px;height:33px;object-fit:cover;border-radius:5px;border:1px solid #3f4854;background:#1c2128}
    .h3d-proj.nocover{grid-template-columns:1fr;padding-left:13px}
    .h3d-proj-name{font-weight:700;word-break:break-all;font-size:12.5px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;color:var(--h3d-bone)}
    .h3d-proj-meta{color:var(--h3d-muted);font-size:11px;font-family:ui-monospace,Consolas}
    .h3d-newrow{display:flex;gap:8px;padding:0 12px 12px;flex-wrap:wrap}
    .h3d-autohint{margin:12px;padding:11px 12px;border:1px solid #4a4230;border-radius:8px;background:#2a2820;color:#d8cfa8;font-size:11.5px;line-height:1.75}
    .h3d-autohint b{color:#f0d98c}
    .h3d-autohint .h3d-btn{margin-top:8px}
    .h3d-meter{margin:0 12px 12px;padding:11px;border:1px solid #444c56;border-radius:8px;background:#262c36}
    .h3d-meter strong{display:block;color:var(--h3d-cyan);font:700 16px/1.3 ui-monospace,Consolas}
    .h3d-meter p{margin:4px 0 0;color:var(--h3d-muted);font-size:11px}
    .h3d-drafts{margin:0 12px 12px;color:var(--h3d-muted);font-size:11px;line-height:1.7}

    .h3d-center-pad{padding:14px 18px 20px}
    .h3d-statusbar{display:flex;align-items:center;gap:10px;padding:10px 13px;border:1px solid #316dca;border-radius:8px;background:linear-gradient(90deg,#1f2f45,#22272e);line-height:1.6}
    .h3d-statusbar .h3d-st-text{flex:1;min-width:0}
    .h3d-statusbar .h3d-chip{align-self:center}
    .h3d-refresh{padding:4px 10px;flex:none}
    .h3d-rail{display:flex;gap:4px;margin:12px 0 14px}
    .h3d-rail span{flex:1;height:5px;border-radius:4px;background:#444c56}
    .h3d-rail span.done{background:var(--h3d-cyan)}
    .h3d-rail span.next{background:#6e7b8c;animation:h3d-blink 1.2s infinite}
    .h3d-rail span.unlink{background:repeating-linear-gradient(135deg,#e0823d 0 4px,#444c56 4px 8px)}
    .h3d-rail span.offchain{background:repeating-linear-gradient(90deg,#3a414c 0 3px,#22272e 3px 6px)}
    .h3d-cards{display:grid;gap:10px}
    .h3d-card{display:grid;grid-template-columns:150px minmax(0,1fr);gap:11px;padding:10px;border:1px solid #3f4854;border-radius:9px;background:#262c36}
    .h3d-card.todo{opacity:.72}
    .h3d-card.todo:hover{opacity:1}
    .h3d-card.mergeable{grid-template-columns:auto 150px minmax(0,1fr);align-items:start}
    .h3d-card.mergeable-off{opacity:.4}
    /* 拖拽调序：把手 / 拖动中的源卡 / 插入线（线画在目标位卡片的上沿或链尾卡的下沿） */
    .h3d-grip{flex:none;width:18px;height:20px;display:inline-grid;place-items:center;border-radius:5px;color:#8794a3;font-size:13px;letter-spacing:-1px;cursor:grab;user-select:none}
    .h3d-grip:hover{color:var(--h3d-cyan);background:#1c2128}
    .h3d-grip:active{cursor:grabbing}
    /* ⬆/⬇ 兜底调序小按钮：默认隐藏，卡片 hover 时露出（与把手并排） */
    .h3d-mv{flex:none;width:16px;height:18px;display:inline-grid;place-items:center;border:0;border-radius:4px;padding:0;background:transparent;color:#8794a3;font-size:10px;line-height:1;cursor:pointer;opacity:0;transition:opacity .12s}
    .h3d-card:hover .h3d-mv{opacity:1}
    .h3d-mv:hover:not(:disabled){color:var(--h3d-cyan);background:#1c2128}
    .h3d-mv:disabled{opacity:.25!important;cursor:not-allowed}
    .h3d-card.h3d-dragging{opacity:.35;border-style:dashed}
    .h3d-card.h3d-drop-before{box-shadow:0 -3px 0 0 var(--h3d-cyan)}
    .h3d-card.h3d-drop-end{box-shadow:0 3px 0 0 var(--h3d-cyan)}
    /* 段禁用（不上链）：半透明虚线框，与待生成 todo 区分 */
    .h3d-card.offchain{opacity:.5;border-style:dashed;border-color:#59626f}
    .h3d-card.offchain:hover{opacity:.85}
    .h3d-mergecb{width:15px;height:15px;margin:4px 0 0 2px;accent-color:#6cb6ff;cursor:pointer;flex:none}
    .h3d-mergebar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:-2px 0 14px;padding:9px 12px;border:1px solid #7a5f36;border-radius:8px;background:linear-gradient(90deg,#352a19,#2d333b)}
    .h3d-merge-sum{flex:1;min-width:200px;color:#e9c07a;font-size:11.5px;line-height:1.7;word-break:break-all}
    .h3d-merge-sum b{color:#f5d9a0}
    .h3d-merge-sum small{display:block;color:var(--h3d-muted);font:10px ui-monospace,Consolas;word-break:break-all}
    .h3d-thumb{position:relative;width:150px;aspect-ratio:16/9;border-radius:6px;overflow:hidden;background:#10151c;display:grid;place-items:center;color:#636e7b;font:700 11px ui-monospace,Consolas}
    /* 视频/图片绝对定位填满盒子：若走普通流，grid 自动行高会把竖屏视频的 intrinsic 高度
       （如 9:16 → 150×266.7）当元素高度，底部控制栏被 overflow:hidden 整条裁掉——
       16:9 恰好 intrinsic 高度=盒高才侥幸正常。绝对定位让元素恒等于盒子尺寸，控制栏任何画幅都在。 */
    .h3d-thumb video,.h3d-thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
    .h3d-thumb video.h3d-segvideo{object-fit:contain;background:#000}
    /* 竖屏段：缩略盒换竖版（高 150、宽自适应），预览面积约 2.6 倍且 contain 不再只剩细条 */
    .h3d-thumb.portrait{aspect-ratio:9/16;width:auto;height:150px;justify-self:center}
    /* 缩略图悬浮操作：全屏 / 下载 —— 不依赖原生控制栏（元素窄到 84px 时浏览器会隐藏 ⋮ 菜单） */
    .h3d-thumb-acts{position:absolute;top:4px;right:4px;display:flex;gap:4px;z-index:2}
    .h3d-tact{width:21px;height:21px;display:grid;place-items:center;border-radius:5px;border:1px solid #444c56cc;background:#1c2128dd;color:#cdd9e1;font-size:11.5px;line-height:1;cursor:pointer;text-decoration:none}
    .h3d-tact:hover{filter:brightness(1.35);border-color:var(--h3d-cyan)}
    .h3d-cbody{min-width:0;display:flex;flex-direction:column;gap:5px}
    .h3d-ctitle{display:flex;gap:7px;align-items:center;flex-wrap:wrap;font-weight:700}
    .h3d-cmeta{color:var(--h3d-muted);font:11px ui-monospace,Consolas;word-break:break-all}
    .h3d-cprompt{color:#b6c2cf;font-size:12px;line-height:1.65;word-break:break-word}
    .h3d-ta{width:100%;min-height:84px;resize:vertical;border:1px solid #444c56;border-radius:6px;background:#2d333b;color:var(--h3d-bone);padding:8px 9px;font:12.5px/1.65 "Microsoft YaHei UI","Segoe UI",sans-serif;outline:none;box-sizing:border-box}
    .h3d-ta:focus{border-color:#6cb6ff;box-shadow:0 0 0 2px #6cb6ff33}
    .h3d-ta:disabled{color:#636e7b;cursor:not-allowed;background:#22272e}
    .h3d-ta-row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:4px}
    .h3d-ta-hint{color:var(--h3d-muted);font-size:10.5px}
    .h3d-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:2px}
    .h3d-hint{color:var(--h3d-muted);font-size:11px;align-self:center}
    .h3d-editor{display:grid;gap:7px;margin-top:4px;padding:10px;border:1px solid #444c56;border-radius:8px;background:#20262e}
    .h3d-editor textarea{width:100%;min-height:96px;resize:vertical;border:1px solid #444c56;border-radius:6px;background:#2d333b;color:var(--h3d-bone);padding:9px;font:13px/1.7 "Microsoft YaHei UI","Segoe UI",sans-serif;outline:none}
    .h3d-editor textarea:focus{border-color:#6cb6ff;box-shadow:0 0 0 2px #6cb6ff33}
    .h3d-editor-row{display:flex;gap:7px;flex-wrap:wrap}
    .h3d-addrow{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
    .h3d-drawer{margin-top:14px;border:1px solid #3f4854;border-radius:8px;background:#262c36}
    .h3d-drawer summary{padding:10px 12px;cursor:pointer;color:var(--h3d-cyan)}
    .h3d-drawer pre{margin:0;padding:0 12px 12px;white-space:pre-wrap;font-size:11.5px;max-height:300px;overflow:auto;color:#b6c2cf}

    .h3d-hist{padding:12px;display:grid;gap:10px;align-content:start}
    .h3d-hist-head{color:var(--h3d-copper);font:700 10.5px/1 ui-monospace,Consolas;letter-spacing:.08em;margin:2px 0 -2px;word-break:break-all}
    .h3d-empty{padding:16px 10px;border:1px dashed #444c56;border-radius:8px;color:var(--h3d-muted);text-align:center;background:#262c36;line-height:1.8}
    .h3d-result{padding:8px;border:1px solid #3f4854;border-radius:9px;background:#20262e}
    .h3d-result.current{border-color:#316dca}
    .h3d-result.gone{opacity:.45}
    .h3d-result video{display:block;width:100%;max-height:230px;border-radius:6px;background:#0a0d12}
    .h3d-result-meta{display:flex;gap:8px;align-items:center;justify-content:space-between;margin-top:7px}
    .h3d-result-name{min-width:0;color:#b6c2cf;font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .h3d-result-info{color:var(--h3d-muted);font:10.5px ui-monospace,Consolas;margin-top:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .h3d-result-acts{display:flex;gap:6px;flex:none}
    .h3d-dl{padding:4px 8px;border:1px solid #316dca;border-radius:6px;color:#9ecbff;text-decoration:none;background:#1f2f45;font-size:11px}
    .h3d-dl:hover{filter:brightness(1.15)}
    .h3d-foot{padding:2px 16px 14px;color:var(--h3d-muted);font-size:10.5px;word-break:break-all;line-height:1.8}

    /* ---- 分段处理中心面板 ---- */
    .h3d-seg-panel{margin-top:6px;border:1px solid #37332b;border-radius:7px;background:#181712;overflow:hidden}
    .h3d-seg-panel summary{padding:7px 10px;cursor:pointer;color:var(--h3d-cyan);font-size:11.5px;font-weight:600;user-select:none;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
    .h3d-seg-panel summary::-webkit-details-marker{display:none}
    .h3d-seg-panel summary::before{content:"▸";font-size:10px;transition:transform .15s}
    .h3d-seg-panel[open] summary::before{transform:rotate(90deg)}
    .h3d-seg-panel.has-content{border-color:#46604f}
    .h3d-seg-body{display:grid;gap:7px;padding:0 10px 10px}
    .h3d-seg-label{display:block;margin-bottom:2px;color:var(--h3d-muted);font-size:10.5px;font-weight:600}
    .h3d-seg-ta{min-height:48px !important;font-size:12px !important;line-height:1.55 !important}
    .h3d-unlink{display:flex;gap:7px;align-items:center;cursor:pointer;padding:6px 8px;border:1px solid #5a3b2e;border-radius:7px;background:#221912;color:#e0a892;font-size:11.5px;font-weight:600;user-select:none}
    .h3d-unlink:hover{border-color:#8a5a42}
    .h3d-unlink input{accent-color:#e0823d;cursor:pointer}
    .h3d-unlink.h3d-offrow{border-color:#54383e;background:#241419;color:#e79aa6}
    .h3d-unlink.h3d-offrow:hover{border-color:#8a5a68}
    .h3d-unlink.h3d-offrow input{accent-color:#c9566a}

    /* ---- 每段时长 + 段级引用素材 ---- */
    .h3d-secs{width:58px;border:1px solid #3a352c;border-radius:5px;background:#211f1a;color:var(--h3d-bone);padding:2px 4px;font:11px ui-monospace,Consolas;text-align:right;outline:none}
    .h3d-secs:focus{border-color:#a8d8bd}
    .h3d-secs-hint{color:var(--h3d-muted);font:10px ui-monospace,Consolas;white-space:nowrap}
    .h3d-refrow{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:4px;padding:7px 8px;border:1px solid #37332b;border-radius:7px;background:#181712}
    .h3d-refrow>label{color:var(--h3d-muted);font-size:10.5px;font-weight:600;flex:none}
    .h3d-refchip{display:inline-flex;gap:5px;align-items:center;padding:3px 8px 3px 3px;border:1px solid #3a352c;border-radius:12px;background:#25221c;color:#a8a294;cursor:pointer;font-size:11px;font-family:inherit}
    .h3d-refchip:hover{border-color:#46604f}
    .h3d-refchip.on{border-color:#2f6e57;background:#12291f;color:#7fe0b0}
    .h3d-refchip img{width:20px;height:20px;border-radius:9px;object-fit:cover;background:#0d0c0a}
    .h3d-reftpl{max-width:118px;border:1px solid #3a352c;border-radius:6px;background:#211f1a;color:var(--h3d-bone);padding:3px 4px;font-size:11px;outline:none;margin-left:auto}
    .h3d-reftpl:focus{border-color:#a8d8bd}

    /* ---- 素材标签编辑 ---- */
    .h3d-labelinp{width:100%;border:1px solid #3a352c;border-radius:5px;background:#211f1a;color:var(--h3d-bone);padding:4px 6px;font:600 12px "Microsoft YaHei UI","Segoe UI",sans-serif;outline:none}
    .h3d-labelinp:focus{border-color:#a8d8bd;box-shadow:0 0 0 2px #8cc9a833}
    .h3d-quicklbl{display:flex;gap:4px;flex-wrap:wrap;margin-top:5px}
    .h3d-quicklbl button{padding:2px 7px;border:1px solid #3a352c;border-radius:10px;background:#25221c;color:#a39d90;cursor:pointer;font-size:10px;font-family:inherit}
    .h3d-quicklbl button:hover{border-color:#46604f;color:#d9d4c9}
    .h3d-asset-usage{margin-top:5px;display:flex;gap:4px;flex-wrap:wrap}

    /* ---- 链参数：换算徽章 + 高级设置折叠 ---- */
    .h3d-convbadge{grid-column:1/-1;margin:-4px 0 0;padding:8px 10px;border:1px dashed #46604f;border-radius:7px;background:#1f2a23;color:#c2e0cd;font:11.5px ui-monospace,Consolas;word-break:break-all}
    .h3d-adv{grid-column:1/-1;border:1px solid #37332b;border-radius:8px;background:#181712;overflow:hidden}
    .h3d-adv summary{padding:8px 10px;cursor:pointer;color:var(--h3d-muted);font-size:11.5px;font-weight:600;user-select:none}
    .h3d-adv summary:hover{color:#d9d4c9}
    .h3d-adv summary::-webkit-details-marker{display:none}
    .h3d-adv summary::before{content:"⚙ ";}
    .h3d-adv[open] summary{border-bottom:1px solid #302c25;color:var(--h3d-cyan)}
    .h3d-adv-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:10px}
    .h3d-adv .h3d-param{margin:0}
    .h3d-updet{border-color:#2c4a52;background:#141d21}
    .h3d-updet summary::before{content:"✦ "}
    .h3d-updet.on{border-color:#316dca80;box-shadow:inset 0 0 0 1px #316dca26}
    .h3d-updet[open] summary{color:#9ecbff}
    .h3d-upwarn{grid-column:1/-1;padding:7px 10px;border:1px solid #9a4144;border-radius:7px;background:#402227;color:#f0a0a4;font-size:11.5px;line-height:1.6}
    .h3d-param{margin:0}
    .h3d-param .h3d-hint{display:block;margin-top:4px}

    .h3d-loadwf{display:flex;flex-direction:column;gap:10px;align-items:center;padding:22px 14px;border:1px dashed #46604f;border-radius:10px;background:#1f2a23;text-align:center;line-height:1.9}
    .h3d-loadwf p{margin:0;color:var(--h3d-muted);font-size:11.5px}

    .h3d-footer{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:0 20px;border-top:1px solid var(--h3d-line);background:#1b1a16}
    .h3d-footinfo{display:flex;gap:16px;color:var(--h3d-muted);flex-wrap:wrap;min-width:0;font-size:11.5px;align-items:center}
    .h3d-footinfo b{color:var(--h3d-bone)}
    .h3d-run{min-width:150px;padding:11px 18px}

    /* ---- 素材与参考 / 链参数 ---- */
    .h3d-lsec,.h3d-rsec{display:flex;flex-direction:column;min-height:0}
    .h3d-col.left .h3d-sechead,.h3d-col.right .h3d-sechead{position:static;backdrop-filter:none}
    .h3d-modebar{display:flex;gap:6px;padding:10px 12px 6px;border-bottom:1px solid var(--h3d-line);background:#171612}
    .h3d-mode{flex:1;min-width:0;padding:8px 4px;border:1px solid #332f27;border-radius:7px;background:#1b1a16;color:#a8a294;cursor:pointer;font:700 12px "Microsoft YaHei UI","Segoe UI",sans-serif;transition:all .12s}
    .h3d-mode:hover{border-color:#46604f;color:#d9d4c9}
    .h3d-mode.active{color:#12241c;background:var(--h3d-cyan);border-color:var(--h3d-cyan);box-shadow:0 0 0 1px var(--h3d-cyan) inset,0 0 14px #8cc9a844}
    .h3d-mode small{display:block;font-weight:400;font-size:9.5px;opacity:.72;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .h3d-mode.active small{opacity:.85}
    .h3d-assets{display:grid;gap:8px;padding:12px}
    .h3d-asset{display:grid;grid-template-columns:64px minmax(0,1fr) auto;gap:9px;align-items:center;padding:8px;border:1px solid #332f27;border-radius:8px;background:#1b1a16;box-shadow:inset 3px 0 0 #3a352c}
    .h3d-asset.on{box-shadow:inset 3px 0 0 var(--h3d-cyan)}
    .h3d-asset-thumb{width:64px;height:52px;border-radius:5px;background:#262319;display:grid;place-items:center;overflow:hidden;color:#77705f;font:700 10px ui-monospace,Consolas}
    .h3d-asset-thumb img{width:100%;height:100%;object-fit:cover}
    .h3d-asset-copy{min-width:0}
    .h3d-asset-copy strong{display:block;font-size:12px}
    .h3d-asset-copy small{display:block;margin-top:3px;color:var(--h3d-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .h3d-asset-acts{display:flex;gap:5px}
    .h3d-asset-acts .h3d-btn{padding:4px 8px;font-size:11px}
    .h3d-addasset{justify-self:start;padding:6px 12px}
    .h3d-upload-row{display:flex;gap:7px;flex-wrap:wrap}
    .h3d-upload-row .h3d-btn{flex:1;min-width:84px}
    .h3d-upload-row .h3d-btn:disabled{opacity:.38;cursor:not-allowed}
    .h3d-kindmark{font-size:12px;line-height:1}
    .h3d-kindmark.big{font-size:22px}
    .h3d-params{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:12px}
    .h3d-param label{display:block;margin-bottom:4px;color:var(--h3d-muted);font-size:11px}
    .h3d-select,.h3d-seedrow input{width:100%;border:1px solid #3a352c;border-radius:6px;background:#211f1a;color:var(--h3d-bone);padding:6px 7px;font-size:12px;outline:none;font-family:inherit}
    .h3d-select:focus,.h3d-seedrow input:focus{border-color:#a8d8bd}
    /* 实验性功能面板 */
    .h3d-expsec{padding-bottom:14px}
    .h3d-exp-sec{margin:0 0 4px}
    .h3d-exp-sec>summary{display:flex;gap:8px;align-items:center;padding:10px 12px;cursor:pointer;list-style:none;user-select:none;flex-wrap:wrap}
    .h3d-exp-sec>summary::-webkit-details-marker{display:none}
    .h3d-exp-sec>summary::before{content:"▸";color:var(--h3d-muted);font-size:11px}
    .h3d-exp-sec[open]>summary::before{content:"▾";color:var(--h3d-cyan)}
    .h3d-exp-sec>summary small{color:var(--h3d-muted);font-weight:400}
    .h3d-exp-sec>summary .h3d-btn{margin-left:auto}
    .h3d-exp-ban{margin:8px 12px 0;padding:7px 10px;border:1px solid #9a4144;border-radius:7px;background:#402227;color:#f0a0a4;font-size:11.5px;line-height:1.6}
    .h3d-exp-card{margin:8px 12px 0;border:1px solid #332f27;border-radius:9px;background:#1b1a16;overflow:hidden}
    .h3d-exp-card.on{border-color:#3f6b52;box-shadow:inset 3px 0 0 var(--h3d-cyan)}
    .h3d-exp-head{display:flex;gap:8px;align-items:center;padding:9px 10px}
    .h3d-exp-head input[type=checkbox]{accent-color:#7fc79f;width:15px;height:15px;cursor:pointer}
    .h3d-exp-head input[type=checkbox]:disabled{opacity:.35;cursor:not-allowed}
    .h3d-exp-head strong{font-size:12px;flex:1;min-width:0}
    .h3d-exp-desc{display:block;padding:0 10px 9px;color:var(--h3d-muted);font-size:11px;line-height:1.55}
    .h3d-exp-params{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:10px;border-top:1px solid #2a261f}
    .h3d-exp-params .h3d-param{margin:0}
    .h3d-exp-params .h3d-param label{font-size:10px}
    .h3d-seedrow{display:flex;gap:5px}
    .h3d-seedrow input{flex:1;min-width:0}
    .h3d-seedrow input::-webkit-outer-spin-button,.h3d-seedrow input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
    .h3d-seedrow input[type=number]{-moz-appearance:textfield;appearance:textfield}
    .h3d-seedrow .h3d-btn{padding:4px 8px;flex:none}
    .h3d-psec .h3d-foot,.h3d-asec .h3d-foot,.h3d-projsec .h3d-foot{padding:0 16px 14px}

    /* ---- 新建项目模态 ---- */
    .h3d-overlay{position:fixed;z-index:1000003;inset:0;display:grid;place-items:center;padding:24px;background:#0a0906d0;backdrop-filter:blur(10px)}
    .h3d-dialog{width:min(470px,calc(100vw - 40px));border:1px solid #464033;border-radius:14px;background:linear-gradient(160deg,#242019,#191712 62%);box-shadow:0 22px 64px #000b;padding:20px;color:var(--h3d-bone);font:13px/1.5 "Microsoft YaHei UI","Segoe UI",sans-serif}
    .h3d-dialog h3{margin:0 0 6px;font-size:16px}
    .h3d-dialog .h3d-lead{color:var(--h3d-muted);margin:0 0 14px;line-height:1.75;font-size:12px}
    .h3d-dialog input[type=text]{width:100%;border:1px solid #3a352c;border-radius:6px;background:#211f1a;color:var(--h3d-bone);padding:9px 10px;font:13px ui-monospace,Consolas;outline:none;margin-bottom:6px}
    .h3d-dialog input[type=text]:focus{border-color:#a8d8bd}
    .h3d-err{color:#e89090;font-size:11px;min-height:16px;margin-bottom:6px}
    .h3d-check{display:flex;gap:8px;align-items:center;color:var(--h3d-muted);font-size:12px;margin-bottom:14px;cursor:pointer}
    .h3d-dialog-row{display:flex;gap:8px;justify-content:flex-end}
    .h3d-optrow{display:flex;flex-direction:column;gap:7px;margin:0 0 14px}
    .h3d-opt{display:flex;gap:10px;align-items:flex-start;padding:9px 11px;border:1px solid #3a352c;border-radius:9px;background:#211f1a;cursor:pointer;transition:border-color .12s,background .12s}
    .h3d-opt:hover{border-color:#46604f}
    .h3d-opt.on{border-color:#2f6e57;background:#12291f}
    .h3d-opt input{margin-top:3px;accent-color:#2f6e57;flex:none}
    .h3d-opt b{display:block;font-size:12.5px}
    .h3d-opt small{color:var(--h3d-muted);font-size:11px;line-height:1.5}

    /* ---- 总提示词模态（宽版 + 等宽多行编辑 + 实时识别预览） ---- */
    .h3d-dialog-wide{width:min(760px,calc(100vw - 40px))}
    .h3d-mpta{width:100%;height:min(46vh,420px);resize:vertical;border:1px solid #3a352c;border-radius:6px;background:#211f1a;color:var(--h3d-bone);padding:10px 12px;font:12.5px/1.7 ui-monospace,Consolas,monospace;outline:none;margin-bottom:8px}
    .h3d-mpta:focus{border-color:#a8d8bd}
    .h3d-mpinfo{color:var(--h3d-muted);font-size:12px;min-height:18px;margin-bottom:4px}
    .h3d-mpbtn{display:block;margin:8px 0 0}

    .h3d-fab{position:fixed;right:16px;top:120px;z-index:80;width:44px;height:44px;border-radius:50%;border:1px solid #46604f;background:#1f2a23;color:#c2e0cd;cursor:pointer;font-size:17px}
    .h3d-fab:hover{filter:brightness(1.2)}

    @media(max-width:1200px){.h3d-sub{display:none}}
    @media(max-width:1050px){.h3d-stage{grid-template-columns:240px minmax(0,1fr)}.h3d-col.right{grid-column:1/-1;max-height:260px}}
    @media(max-width:760px){.h3d-page{grid-template-rows:auto 1fr 64px}.h3d-topbar{grid-template-columns:1fr auto;padding:8px 12px;flex-wrap:wrap;gap:8px}.h3d-title{font-size:15px}.h3d-footer{flex-direction:column;padding:8px 12px;gap:8px}}
    `;
    document.head.appendChild(style);
}

/* ---------- 侧栏迷你卡 ---------- */

function renderMini(data) {
    if (!miniBox) return;
    const { state, mf, plan, node } = data;
    miniBox.innerHTML = "";
    const card = el("div", "h3d-mini");

    const total = plan ? plan.length : 0;
    const off = planOff(mf);
    const done = Math.max(0, (mf?.done ?? state?.done ?? 0) - off);

    const head = el("div", "h3d-mini-head");
    const brand = el("div", "h3d-mini-brand", "长片导演台");
    /* 空存档目录 = 按参数指纹自动开项目：如实标注，不拿上次项目名冒充当前链
     * （否则参数一变悄悄换项目，用户只看到「名字没变却新开了项目」） */
    brand.append(el("small", "", escapeHtml(node
        ? (String(getDirValue(node) || "").trim() || "未命名链 · 自动存档")
        : "无节点")));
    head.append(brand, el("span", `h3d-led ${ledPhase}`, '<i></i><em></em>'));

    const rail = el("div", "h3d-mini-rail");
    const cells = Math.max(1, Math.min(total, MAX_SEG));
    for (let i = 0; i < cells; i++) {
        rail.append(el("span", i < done ? "done" : i === done && done < total ? "next" : ""));
    }

    const cards = el("div", "h3d-mini-cards");
    const info = el("div", "h3d-mini-card", escapeHtml(statusLine(state, mf, plan).text));
    const count = el("div", "h3d-mini-count", total
        ? `${done}<small>/${total} 段</small>`
        : `<small>0 段<br>待添加</small>`);
    cards.append(info, count);

    const foot = el("div", "h3d-mini-foot");
    const ps = paramsSummary(node, mf);
    const totalSec = plan?.length ? `共${chainSeconds(node, data.ds, plan).toFixed(1)}s/${plan.length}段` : "";
    foot.append(el("div", "h3d-mini-params",
        escapeHtml([ps.geo, ps.len, ps.ctx, totalSec].filter(Boolean).join(" · ") || "参数待首次运行后显示")));
    const open = el("button", "h3d-btn h3d-btn-cta h3d-mini-open", "打开长片导演台");
    open.onclick = openDesk;
    foot.append(open);

    card.append(head, rail, cards, foot);
    miniBox.append(card);
    paintLeds();
    removeFab();   // 侧栏标签已能渲染内容，撤下保底悬浮球
}

/* 刷新失败的保底迷你卡：报错信息 + 入口按钮，侧栏标签永不空白 */
function renderMiniFallback(e) {
    if (!miniBox) return;
    miniBox.innerHTML = "";
    const card = el("div", "h3d-mini");
    card.append(el("div", "h3d-mini-brand", "长片导演台"));
    card.append(el("div", "h3d-mini-card",
        "刷新失败：" + escapeHtml(String((e && e.message) || e))
        + "<br>接口异常不影响入口，可先打开导演台查看顶部诊断横幅"));
    const open = el("button", "h3d-btn h3d-btn-cta h3d-mini-open", "打开长片导演台");
    open.onclick = openDesk;
    card.append(open);
    miniBox.append(card);
}

/* ---------- 全屏导演台 ---------- */

function openDesk() {
    if (desk) return;
    const page = el("section", "h3d-page");
    page.tabIndex = -1;
    /* 免疫浏览器整页自动翻译：本面板中英混排（2D/latent/simple…），被机翻改写会全面错乱
     * （2D→二维、simple→简单的、整句被重写）。translate 属性随 DOM 继承，标在根上即可。 */
    page.setAttribute("translate", "no");
    page.classList.add("notranslate");

    /* 顶栏 */
    const topbar = el("header", "h3d-topbar");
    const left = el("div", "h3d-top-left");
    left.append(
        el("span", "h3d-kicker", "H3 · SEAMLESS CHAIN"),
        el("span", "h3d-title", "长片一体化导演台"),
        el("span", "h3d-sub", "项目控制台 · 存档续拍 · 逐段审片"),
    );
    const right = el("div", "h3d-top-right");
    const ledWrap = el("span", `h3d-led ${ledPhase}`, '<i></i><em></em>');
    const sub = el("span", "h3d-sub h3d-top-project", "");
    const close = el("button", "h3d-close", "✕");
    close.title = "关闭导演台（Esc）";
    right.append(ledWrap, sub, close);
    topbar.append(left, right);

    /* 诊断横幅：项目存档接口未注册时显示（/h3chain/ping 探测失败） */
    const banner = el("div", "h3d-banner");
    banner.textContent = apiErrorText;
    banner.style.display = apiErrorText ? "" : "none";

    /* 主舞台三栏（侧栏各含两个固定分区，渲染器只填充分区内容） */
    const stage = el("div", "h3d-stage");
    const colL = el("aside", "h3d-col left");
    const lProj = el("section", "h3d-lsec h3d-projsec");
    const lAssets = el("section", "h3d-lsec h3d-asec");
    colL.append(lProj, lAssets);
    const colC = el("section", "h3d-col center");
    const cHead = el("div", "h3d-sechead",
        "<strong>段落流水线</strong><small>卡片 = 生成顺序（1–64 段不限，「＋ 添加一段」即可加段；「提示词·1..3」只是画布镜像）；✏ 改词 · 🎲 重摇 · 📎 插视频 · 🎬 分段处理（场景/角色）</small>");
    const mpBtn = el("button", "h3d-btn h3d-mpbtn", "📋 总提示词");
    mpBtn.title = "多段提示词一次性粘贴分配（段头+场景/角色/环境音/配乐/时长/提示词 六标签格式，可让 AI 按 skill 生成后直接贴入）";
    mpBtn.onclick = openMasterPromptModal;
    cHead.append(mpBtn);
    colC.append(cHead);
    const colR = el("aside", "h3d-col right");
    const rParams = el("section", "h3d-rsec h3d-psec");
    const rExp = el("section", "h3d-rsec h3d-expsec");
    const rUpscale = el("section", "h3d-rsec h3d-upsec");
    const rHist = el("section", "h3d-rsec h3d-hsec");
    colR.append(rParams, rExp, rUpscale, rHist);
    stage.append(colL, colC, colR);

    /* 页脚 */
    const footer = el("footer", "h3d-footer");
    const footInfo = el("div", "h3d-footinfo", "");
    const run = el("button", "h3d-btn h3d-btn-cta h3d-run", "▶ 开始生成");
    footer.append(footInfo, run);

    page.append(topbar, banner, stage, footer);
    document.body.append(page);
    page.focus();

    desk = {
        page,
        zones: {
            project: sub,
            colC,
            lProj, lAssets,
            rParams, rExp, rUpscale, rHist,
            footInfo, run,
        },
        cardsSig: "",
        histSig: "",
        close() {
            page.remove();
            desk = null;
        },
    };
    close.onclick = () => desk && desk.close();
    page.addEventListener("keydown", (e) => {
        if (e.key === "Escape") desk && desk.close();
    });
    refresh();
}

function isVideoPlaying(root) {
    return [...root.querySelectorAll("video")].some((v) => !v.paused && !v.ended);
}

function cardsSignature(data) {
    const { state, plan, ds, mf } = data;
    return JSON.stringify({
        dir: state?.dir ?? "",
        done: state?.done ?? 0,
        total: plan?.length ?? 0,
        review: !!state?.review,
        reroll: state?.reroll ?? 0,
        prologue: !!mf?.has_prologue,
        plan: (plan || []).map((it) => it.kind === "insert" ? ["i", it.pos, it.file] : ["p", it.text]),
        segs: (ds?.segments || []).map((s) => [s.scene_prompt ?? "", s.character_prompt ?? "",
                                               s.seconds ?? 0, (s.refs || []).join(","),
                                               !!s.unlink, !!s.disabled,
                                               Array.isArray(s.frame_refs) ? s.frame_refs.join(",") : null]),
        /* 首尾帧图文件名也要进签名：上传/换图后段卡片的首尾帧 chips 行才会重建 */
        frames: [ds?.first_frame ?? "", ds?.end_frame ?? ""],
        labels: (ds?.ref_assets || []).map((a) => a.label),
        mode: ds?.mode ?? "",
        /* 重摇标记/运行队列进签名：标记/取消/队列消费后待重摇徽章即时刷新 */
        redo: [(ds?.redo_segs || []).map((x) => `${x.slot}:${x.mode}`).join(","),
               (mf?.redo_queue || []).map((x) => (Array.isArray(x) ? x.join(":") : "")).join(",")],
        dur: String(getWidgetValue(data.node, W_DUR) ?? ""),
        merge: [mergeSel.on, [...mergeSel.segs].sort((a, b) => a - b).join(","),
                (mergeSel.files || []).join(",")],
    });
}

function updateDesk(data) {
    if (!desk) return;
    const { node, state, mf, plan, drafts } = data;
    const z = desk.zones;
    desk.lastData = data;      // 供 repaintUpscale 局部重渲沿用 mf/state/模型列表缓存

    /* 顶栏项目名 + LED */
    const dirName = state?.dir || (node ? getDirValue(node) : "") || "无当前链";
    z.project.textContent = `项目 · ${dirName}`;
    paintLeds();

    /* 左栏：项目与链 + 素材与参考（标签输入聚焦时跳过重渲，防丢焦） */
    renderLeftColumn(z.lProj, data);
    if (!z.lAssets.querySelector(".h3d-labelinp:focus")) {
        renderAssetsZone(z.lAssets, data);
    }

    /* 中栏：状态条 + 进度轨 + 段落卡片（有编辑器/播放中时跳过重渲） */
    renderCenterColumn(z.colC, data);

    /* 右栏：链参数（编辑中不重建）+ 二采面板（编辑中不重建）+ 成片历史（播放中不重建） */
    const psig = paramsSig(node);
    const pgrid = z.rParams.querySelector(".h3d-params");
    if (!(pgrid && pgrid.contains(document.activeElement)) && z.rParams.dataset.sig !== psig) {
        z.rParams.dataset.sig = psig;
        renderParamsZone(z.rParams, data);
    }
    /* 实验性功能面板（ds.experiments 驱动；编辑动作经 setExp* -> repaintExperiments() 即时重渲，
     * 不走此守卫；此处守卫仅为保护「全局刷新时区内有输入框正在打字」不丢焦；defs 到达/失败/硬开关变化也触发。
     * !!node 必须进签名：面板曾在节点未就绪时渲染过「画布上未找到节点」，节点随后可用若
     * 签名不变则永不重建（experiments 全空时两头签名相同），须先做点别的操作才能显示 */
    const esig = JSON.stringify([data.ds?.experiments ?? {},
        EXP.defs ? EXP.defs.length : 0, EXP.forceDisabled, EXP.failed, !!data.node]);
    if (!z.rExp.contains(document.activeElement) && z.rExp.dataset.sig !== esig) {
        z.rExp.dataset.sig = esig;
        renderExperimentsZone(z.rExp, data);
    }
    const usig = upscaleSig(data);
    if (!z.rUpscale.contains(document.activeElement) && z.rUpscale.dataset.sig !== usig) {
        z.rUpscale.dataset.sig = usig;
        renderUpscaleZone(z.rUpscale, data);
    }
        const histSig = (state?.dir ?? "") + "|" + (mf?.finals || []).join(",")
            + "|" + (mf?.merges || []).map((m) => m?.file || "").join(",");
    if (histSig !== desk.histSig || !z.rHist.querySelector(".h3d-hist")) {
        if (!isVideoPlaying(z.rHist)) {
            desk.histSig = histSig;
            renderHistoryZone(z.rHist, data);
        }
    }

    /* 页脚 CTA + 提示 */
    renderFooter(z, data);
}

/* ---- 左栏 ---- */

function renderLeftColumn(sec, data) {
    const { node, state, mf } = data;
    sec.replaceChildren();
    sec.append(el("div", "h3d-sechead",
        "<strong>项目存档</strong><small>一个项目一个文件夹 · 点击读档继续拍</small>"));

    /* 「存档目录」为空 = 按画布参数指纹自动命名：参数一变即静默开新项目
     * （用户视角就是「保存有时新建项目、有时又不建」）。把隐式行为亮出来：
     * 提示成因 + 一键固定到上次运行的项目，之后参数漂移会被后端明确拦下 */
    const autoNamed = !!node && !String(getDirValue(node) || "").trim();
    if (autoNamed) {
        const hint = el("div", "h3d-autohint");
        hint.append(el("div", "",
            "「存档目录」为空：保存按画布参数指纹自动开项目——参数（画幅/每段时长/"
            + "步数/CFG/采样器/门控/递减锚定/实验开关等）一变就换新项目，不变则续用同一个。"
            + (state?.dir ? `上次运行存入 ${escapeHtml(state.dir)}。` : "")
            + "固定项目：读档任一项目，或点下方按钮。"));
        if (state?.dir) {
            const pin = el("button", "h3d-btn h3d-btn-cyan", "📌 固定当前项目");
            pin.title = `把「${state.dir}」写进存档目录：此后保存固定进这个项目，`
                + "参数变化时后端会明确报「存档参数与当前不一致」而不是静默开新项目";
            pin.onclick = () => {
                if (!setDirValue(node, state.dir)) { alert("节点上没有「存档目录/断点目录」控件"); return; }
                setLed("idle", `已固定项目「${state.dir}」，此后保存都进这个文件夹`);
                scheduleRefresh(200);
            };
            hint.append(pin);
        }
        sec.append(hint);
    }

    const list = el("div", "h3d-projlist");
    const projects = mergeProjects(data.projects, state);
    if (!data.apiOk) {
        list.append(el("div", "h3d-empty",
            "项目接口未注册：列表暂不可用（生成不受影响）。请重启 ComfyUI 后刷新浏览器。"));
    } else if (!projects.length) {
        list.append(el("div", "h3d-empty",
            "暂无项目存档：点下方「＋ 新建项目」立即在 output/h3_projects/ 建好文件夹；"
            + "开自动保存/审片跑一次也会自动生成项目。"));
    }
    for (const p of projects) {
        const active = state?.dir && p.dir === state.dir;
        const row = el("div", "h3d-projrow");
        const coverUrl = p.cover ? viewUrl(`h3_projects/${p.dir}`, p.cover) : "";
        const btn = el("button", "h3d-proj" + (active ? " active" : "") + (coverUrl ? "" : " nocover"));
        btn.innerHTML = `
            <span class="h3d-proj-name">${escapeHtml(p.title || p.dir)}
                ${p.finals && p.finals.length ? badge(`成片×${p.finals.length}`, "media") : ""}
                ${!p.total ? badge("草稿", "") : ""}
            </span>
            <span class="h3d-proj-meta">${p.total
                ? `${p.done ?? 0}/${p.total} 段`
                : "未配置段落"}${p.updated_at ? " · " + escapeHtml(fmtTime(p.updated_at)) : ""}</span>`;
        if (coverUrl) {
            const img = document.createElement("img");
            img.className = "h3d-proj-cover";
            img.loading = "lazy";
            img.src = coverUrl;
            img.alt = "";
            img.onerror = () => img.remove();
            btn.prepend(img);
        }
        btn.title = "点击读档：切换「存档目录」并载入该项目提示词（共享参数需与其一致，否则后端会提示换链）";
        btn.onclick = () => switchProject(p.dir);
        row.append(btn);
        const del = el("button", "h3d-proj-del", "🗑");
        del.type = "button";
        del.title = `删除项目（整个文件夹 output/h3_projects/${p.dir}，视频与提示词全删，不可恢复）`;
        del.onclick = (e) => { e.stopPropagation(); deleteProject(p.dir); };
        row.append(del);
        list.append(row);
    }
    sec.append(list);

    const newrow = el("div", "h3d-newrow");
    const newBtn = el("button", "h3d-btn h3d-btn-cyan", "＋ 新建项目");
    newBtn.title = "新开一条视频链：换存档目录名，提示词沿用当前内容作底稿";
    newBtn.onclick = openNewProjectModal;
    newrow.append(newBtn);
    if (node && mf?.params && Object.keys(mf.params).length) {
        const ap = el("button", "h3d-btn", "⚙ 套用参数到画布");
        ap.title = "把当前项目的共享参数（画幅/每段时长/引导帧数/步数/采样器等）写回节点控件";
        ap.onclick = () => applyParamsToCanvas(node, mf.params);
        newrow.append(ap);
    }
    sec.append(newrow);

    if (state?.dir) {
        const off = planOff(mf);
        const done = Math.max(0, (mf?.done ?? state.done ?? 0) - off);
        const total = data.plan?.length ?? Math.max(0, (state.total ?? 0) - off);
        const ps = paramsSummary(node, data.mf);
        const totalSec = data.plan?.length
            ? `共${chainSeconds(node, data.ds, data.plan).toFixed(1)}s` : "";
        const meter = el("div", "h3d-meter");
        meter.innerHTML = `<strong>${total ? `${done}/${total} 段` : "未配置段落（提示词每行一段）"}${state.review ? " · 审片中" : ""}</strong>
            <p>${escapeHtml([ps.geo, ps.len, ps.ctx, totalSec].filter(Boolean).join(" · ") || "参数待首次运行后显示")}</p>`;
        sec.append(meter);
        sec.append(el("div", "h3d-foot", `项目文件夹：output/h3_projects/${escapeHtml(state.dir)}`));
    }
}

function mergeProjects(projects, state) {
    const map = new Map();
    for (const p of projects || []) {
        if (p?.dir) map.set(p.dir, p);
    }
    if (state?.dir && !map.has(state.dir)) {
        map.set(state.dir, { dir: state.dir, done: state.done, total: state.total, updated_at: state.updated_at });
    }
    const arr = [...map.values()];
    arr.sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));
    return arr.slice(0, 40);
}

/* ---- 中栏 ---- */

function renderCenterColumn(colC, data) {
    const sig = cardsSignature(data);
    // 编辑中（常驻 textarea 聚焦 / 临时编辑器打开 / 视频播放中 / 拖拽调序进行中）
    // 跳过重建，防丢焦丢草稿 / 防拖动中卡片列表被轮询刷新换掉导致 drop 目标失效
    const taFocus = !!colC.querySelector(".h3d-ta:focus");
    const locked = taFocus || !!colC.querySelector(".h3d-editor") || isVideoPlaying(colC) || !!dragSeg;
    if (!locked && (sig !== desk.cardsSig || !colC.querySelector(".h3d-center-pad"))) {
        desk.cardsSig = sig;
        [...colC.children].slice(1).forEach((n) => n.remove());
        colC.append(buildCenterBody(data));
    }
}

function buildCenterBody(data) {
    const { node, state, mf, plan, drafts } = data;
    const wrap = el("div", "h3d-center-pad");
    const total = plan ? plan.length : 0;
    const off = planOff(mf);
    const done = Math.max(0, (mf?.done ?? state?.done ?? 0) - off);

    /* 状态条 */
    const bar = el("div", "h3d-statusbar");
    const st = statusLine(state, mf, plan);
    bar.append(el("div", "h3d-st-text", escapeHtml(st.text)));
    if (state?.review) bar.insertAdjacentHTML("beforeend", badge("逐段审片", "cyan"));
    if (state?.reroll > 0) bar.insertAdjacentHTML("beforeend", badge(`重跑起始段=${state.reroll}`, "warn"));
    if (mergeSel.on) bar.insertAdjacentHTML("beforeend", badge("合并模式", "media"));
    /* 合并模式：勾选已完成段按链顺序拼接导出（纯内存勾选态，不动链与存档） */
    if (done > 0) {
        const mergeBtn = el("button", "h3d-btn" + (mergeSel.on ? " h3d-btn-cyan" : ""),
            mergeSel.on ? "⧉ 合并模式·开" : "⧉ 合并模式");
        mergeBtn.title = "开启后勾选若干已完成段（可追加上传外部视频），按链顺序流式拼接成"
            + " merged_*.mp4 导出到项目文件夹；不动链、不动存档，随时可退出";
        mergeBtn.onclick = () => {
            mergeSel.on = !mergeSel.on;
            if (!mergeSel.on) { mergeSel.segs = []; mergeSel.files = []; }
            scheduleRefresh(0);
        };
        bar.append(mergeBtn);
    }
    const refreshBtn = el("button", "h3d-btn h3d-refresh", "↻");
    refreshBtn.title = "重新读取节点与存档数据";
    refreshBtn.onclick = refresh;
    bar.append(refreshBtn);
    wrap.append(bar);

    /* 段落进度轨（按各段时长加权，宽度≈时长比例） */
    if (total > 0) {
        const defRaw = Number(getWidgetValue(node, W_DUR));
        const def = isFinite(defRaw) && defRaw > 0 ? defRaw : 5.0;
        const rail = el("div", "h3d-rail");
        for (let i = 0; i < Math.min(total, MAX_SEG); i++) {
            const it = plan[i];
            const sec = it?.kind === "prompt" && it.idx !== undefined
                ? (data.ds.segments[it.idx]?.seconds ?? def) : def;
            const unlinked = it?.kind === "prompt" && it.idx !== undefined
                && !!(data.ds.segments[it.idx]?.unlink);
            const offchain = it?.kind === "prompt" && it.idx !== undefined
                && !!(data.ds.segments[it.idx]?.disabled);
            const sp = el("span", (i < done ? "done" : i === done ? "next" : "")
                + (unlinked ? " unlink" : "") + (offchain ? " offchain" : ""));
            sp.style.flexGrow = String(Math.max(1, sec));
            sp.title = `段 ${i + 1} · ${sec}s`
                + (it?.kind === "insert" ? "（插入视频，按默认估）" : "")
                + (unlinked ? " · 独立镜头（与上段硬切）" : "")
                + (offchain ? " · 已禁用（不上链）" : "");
            rail.append(sp);
        }
        wrap.append(rail);
    }

    /* 合并清单条：已勾段号（链序）+ 外部上传 + 导出/退出 */
    if (mergeSel.on) {
        const mbar = el("div", "h3d-mergebar");
        const segsSorted = [...mergeSel.segs].sort((a, b) => a - b);
        const segSum = segsSorted.length
            ? segsSorted.map((s) => `段${Math.max(1, s - off)}`).join("＋") : "未勾选段";
        const fileSum = (mergeSel.files || []).length
            ? ` ＋ 外部视频×${mergeSel.files.length}` : "";
        const sum = el("div", "h3d-merge-sum",
            `合并清单（按链顺序）：<b>${escapeHtml(segSum + fileSum)}</b>`
            + (mergeSel.files || []).map((f) => `<small>${escapeHtml(f)}</small>`).join(""));
        mbar.append(sum);
        const upBtn = el("button", "h3d-btn", "＋ 上传视频");
        upBtn.title = "上传外部视频追加到合并清单末尾（input 目录；建议 24fps、画幅与项目一致，"
            + "不同画幅会自动缩放裁剪到项目画幅）";
        upBtn.onclick = pickMergeVideo;
        const goBtn = el("button", "h3d-btn h3d-btn-cta", "⧉ 合并导出");
        goBtn.title = "按链顺序把勾选项流式拼接为 merged_*.mp4（PyAV 编码，分钟级耗时，期间界面可用）";
        goBtn.disabled = !(mergeSel.segs.length || (mergeSel.files || []).length);
        goBtn.onclick = () => doMergeExport(goBtn);
        const exitBtn = el("button", "h3d-btn", "✕ 退出");
        exitBtn.title = "退出合并模式并清空勾选（不影响链与存档）";
        exitBtn.onclick = () => {
            mergeSel.on = false; mergeSel.segs = []; mergeSel.files = [];
            scheduleRefresh(0);
        };
        mbar.append(upBtn, goBtn, exitBtn);
        wrap.append(mbar);
    }

    /* 未填写提示 */
    if (drafts && drafts.length) {
        wrap.append(el("div", "h3d-drafts",
            `未填写（运行时跳过）：${drafts.map(escapeHtml).join("、")}`));
    }

    /* 段落卡片 */
    wrap.append(buildCards(data));

    /* 添加行 */
    const addrow = el("div", "h3d-addrow");
    if (node) {
        const addSeg = el("button", "h3d-btn", "＋ 添加一段");
        addSeg.title = "新增一段提示词到导演台状态（最多 64 段；「提示词·1..3」画布镜像只显前 3 段，超过 3 段全在导演台管理）";
        addSeg.onclick = () => addPromptSegment(node);
        addrow.append(addSeg);
        if (total) {
            const tail = insertButton("尾部插入视频", total + 1);
            tail.title = `上传视频追加到成片末尾（${total + 1}|文件名）`;
            addrow.append(tail);
        }
    } else if (window.H3_DEFAULT_WORKFLOW) {
        const wf = el("button", "h3d-btn h3d-btn-cyan", "⚡ 一键载入配套工作流");
        wf.title = "画布上还没有 H3 链节点：载入官方风格预置工作流（模型加载 + 主节点 + 常驻连线的素材池）";
        wf.onclick = loadDefaultWorkflow;
        addrow.append(wf);
    }
    wrap.append(addrow);

    /* 上次运行报告 */
    if (state?.report) {
        const det = el("details", "h3d-drawer");
        det.innerHTML = `<summary>上次运行报告</summary><pre>${escapeHtml(state.report)}</pre>`;
        wrap.append(det);
    }
    return wrap;
}

function buildCards(data) {
    const { node, state, mf, plan } = data;
    const off = planOff(mf);
    const done = Math.max(0, (mf?.done ?? state?.done ?? 0) - off);
    const total = plan.length;
    const seeds = mf?.seeds || [];
    const seams = mf?.seams || [];
    const bridges = mf?.bridge_scores || [];
    const redoMarks = redoMap(data.ds, mf);   // slot -> mode（ds 标记 ∪ manifest 队列）
    const wrap = el("div", "h3d-cards");

    if (!plan.length) {
        wrap.append(el("div", "h3d-empty",
            node
                ? "还没有段落：点上方「＋ 添加一段」开始填写提示词。"
                : "画布上未找到 H3 Seamless Chain 节点，且暂无历史链数据。添加节点后这里成为段落流水线。"));
        return wrap;
    }

    plan.forEach((it, idx) => {
        const isDone = idx < done;
        const isInsert = it.kind === "insert";
        const isHead = isInsert && it.pos === 1;
        /* 段禁用（不上链）：仅提示词段；插入段位置固定不参与（有专用移除操作） */
        const segData0 = (it.idx !== undefined && data.ds.segments)
            ? (data.ds.segments[it.idx] || defaultSegment()) : null;
        const isDisabled = !isInsert && !!segData0?.disabled;
        const card = el("div", "h3d-card" + (isDone ? "" : " todo") + (isDisabled ? " offchain" : ""));
        const media = segMediaHtml(state, mf, idx);
        const thumb = el("div", "h3d-thumb");
        thumb.innerHTML = media
            ?? `<div>${isDone ? "…" : isInsert ? "插入段" : "待生成"}</div>`;
        const img = thumb.querySelector("img");
        if (img) img.onerror = () => { img.replaceWith(el("div", "", "⚠ 加载失败")); };

        /* 缩略图视频：竖屏段换竖版盒；任何画幅都提供悬浮「全屏 / 下载」，
           不再依赖原生控制栏右下角 ⋮ 菜单（小尺寸元素下浏览器会隐藏它） */
        const segVid = thumb.querySelector("video");
        if (segVid) {
            const markPortrait = () => {
                if (segVid.videoHeight > segVid.videoWidth) thumb.classList.add("portrait");
            };
            if (segVid.readyState >= 1) markPortrait();
            else segVid.addEventListener("loadedmetadata", markPortrait, { once: true });
            const vidUrl = segVid.getAttribute("src") || "";
            const acts = el("div", "h3d-thumb-acts");
            const fsBtn = el("button", "h3d-tact", "⛶");
            fsBtn.title = "全屏播放本段";
            fsBtn.onclick = () => {
                const p = segVid.requestFullscreen ? segVid.requestFullscreen() : null;
                if (p && p.catch) p.catch(() => { window.open(vidUrl, "_blank"); });
            };
            const dl = el("a", "h3d-tact", "⬇");
            dl.href = vidUrl;
            dl.download = (mf?.videos || [])[idx] || "segment.mp4";
            dl.title = "下载本段视频";
            acts.append(fsBtn, dl);
            thumb.append(acts);
        }

        const body = el("div", "h3d-cbody");
        const segData = segData0;
        const stateChip = !isDone
            ? badge(isInsert ? "插入段（未跑）" : "待生成", "")
            : isHead ? badge("片头（上传）", "media")
            : isInsert ? badge("插入段", "media") : badge("已完成", "ok");
        const unlinkChip = !isInsert && segData?.unlink ? badge("独立镜头", "warn") : "";
        const offChip = isDisabled ? badge("⏸ 已禁用", "warn") : "";
        /* 待重摇徽章：已标记（未提交）或已在运行队列（审片逐段推进剩余量） */
        const redoMode = isDone && !isInsert ? redoMarks.get(idx + off) : null;
        const redoChip = redoMode ? badge(`🔁 待重摇·${redoMode}`, "warn") : "";
        const upRec = isDone ? (mf?.upscale?.segs || [])[idx + off] : null;
        const upChip = upRec?.done
            ? badge(`二采${upRec.size?.length ? ` ${upRec.size[0]}×${upRec.size[1]}` : "✓"}`, "cyan") : "";
        const title = el("div", "h3d-ctitle",
            `<span>段 ${idx + 1}${isInsert ? `（位置 ${it.pos}）` : ""}</span>${stateChip}${offChip}${unlinkChip}${redoChip}${upChip}`);
        /* 拖拽调序把手（仅提示词段；插入段位置固定由 pos 决定，不参与拖拽） */
        if (!isInsert && node && !mergeSel.on && it.idx !== undefined) {
            const grip = el("span", "h3d-grip", "⠿");
            grip.title = "按住拖动调整此段在链上的位置：其余段保持相对顺序，"
                + "从首个变化段起自动级联重做（接缝重新衔接）";
            grip.draggable = true;
            grip.addEventListener("dragstart", (e) => {
                dragSeg = { idx: it.idx };
                card.classList.add("h3d-dragging");
                try {
                    e.dataTransfer.effectAllowed = "move";
                    e.dataTransfer.setData("text/plain", String(it.idx));
                } catch (err) { /* IE 模式忽略 */ }
            });
            grip.addEventListener("dragend", () => {
                dragSeg = null;
                wrap.querySelectorAll(".h3d-dragging, .h3d-drop-before, .h3d-drop-end")
                    .forEach((n) => n.classList.remove("h3d-dragging", "h3d-drop-before", "h3d-drop-end"));
                scheduleRefresh(60);   // 清残留态并恢复常规重建
            });
            title.append(grip);
            /* ⬆/⬇ 兜底小按钮（hover 显示）：拖拽不可用/不顺手时按名次换位，
               走同一条 applyReorder 路径（含拖动范围清重摇标记/二采勾选） */
            const mvUp = el("button", "h3d-mv", "⬆");
            mvUp.title = "与上一个提示词段换位（中间隔着插入视频则一并跨过）；"
                + "从首个变化段起自动级联重做";
            mvUp.onclick = () => {
                const ins = nudgeIns(data, it.idx, true);
                if (ins !== null) applyReorder(node, data, it.idx, ins);
            };
            const mvDn = el("button", "h3d-mv", "⬇");
            mvDn.title = "与下一个提示词段换位（中间隔着插入视频则一并跨过）；"
                + "从首个变化段起自动级联重做";
            mvDn.onclick = () => {
                const ins = nudgeIns(data, it.idx, false);
                if (ins !== null) applyReorder(node, data, it.idx, ins);
            };
            mvUp.disabled = nudgeIns(data, it.idx, true) === null;
            mvDn.disabled = nudgeIns(data, it.idx, false) === null;
            title.append(mvUp, mvDn);
        }

        /* 合并模式：已完成段（含插入段）可勾选拼接进 merged_*.mp4（勾选值=全局 1-based 段号）；
           已禁用段不进成片（与自动成片/二采拼接同口径），不给勾选 */
        if (mergeSel.on) {
            if (isDone && !isDisabled) {
                const cb = document.createElement("input");
                cb.type = "checkbox";
                cb.className = "h3d-mergecb";
                const g1 = idx + off + 1;
                cb.checked = mergeSel.segs.includes(g1);
                cb.title = `勾选后并入合并导出（链位 ${idx + 1}，按链顺序拼接）`;
                cb.onchange = () => {
                    mergeSel.segs = cb.checked
                        ? [...mergeSel.segs, g1] : mergeSel.segs.filter((x) => x !== g1);
                    scheduleRefresh(0);
                };
                card.classList.add("mergeable");
                card.prepend(cb);
            } else {
                card.classList.add("mergeable-off");
            }
        }

        /* 每段时长（秒）：留空=跟随节点默认；显示吸附后的帧数 */
        let secsHint = null;
        if (node && it.idx !== undefined) {
            const defRaw = Number(getWidgetValue(node, W_DUR));
            const defSec = isFinite(defRaw) && defRaw > 0 ? defRaw : 5.0;
            const secsInp = document.createElement("input");
            secsInp.type = "number";
            secsInp.className = "h3d-secs";
            secsInp.min = "0.5"; secsInp.max = "15"; secsInp.step = "0.1";
            secsInp.placeholder = String(defSec);
            secsInp.value = (segData || defaultSegment()).seconds ?? "";
            secsInp.title = "本段时长（秒）：留空=跟随右栏「每段时长」默认；内部自动吸附 17k+5 帧网格(@24fps)";
            secsHint = el("span", "h3d-secs-hint",
                `≈${snapFrames((segData || defaultSegment()).seconds ?? defSec)}帧`);
            const syncHint = (v) => {
                const num = Number(v);
                const sec = (v === "" || !isFinite(num) || num <= 0) ? defSec : num;
                secsHint.textContent = `≈${snapFrames(sec)}帧`;
            };
            secsInp.addEventListener("input", () => syncHint(secsInp.value));
            secsInp.addEventListener("change", () => {
                setSegmentSeconds(node, it.idx, secsInp.value);
                syncHint(secsInp.value);
            });
            title.append(secsInp, secsHint, el("span", "h3d-secs-hint", "秒"));
        }
        body.append(title);

        const seedTxt = isDone && !isInsert && seeds[idx + off] != null ? `种子 ${seeds[idx + off]}` : "";
        const seamTxt = isDone && seams[idx + off]
            ? `接缝 ${seams[idx + off][0]}${seams[idx + off][1] == null ? "" : ` / ${seams[idx + off][1]}dB`}` : "";
        const bridgeTxt = isDone && !isInsert && bridges[idx + off] != null ? `桥分 ${bridges[idx + off]}` : "";
        const meta = [seedTxt, seamTxt, bridgeTxt].filter(Boolean).join(" · ");
        if (meta) body.append(el("div", "h3d-cmeta", escapeHtml(meta)));

        /* 提示词：常驻 textarea（插入段显示文件名只读）—— 直接在台内编辑，实时写回 JSON 状态 */
        if (isInsert) {
            body.append(el("div", "h3d-cprompt", escapeHtml(it.file)));
        } else {
            const ta = document.createElement("textarea");
            ta.className = "h3d-ta";
            ta.rows = 4;
            ta.value = it.text || "";
            const pool = (data.ds.ref_assets || []);
            const poolHint = pool.length
                ? `用 [[${pool[0].label}]] 这样的标签引用素材，或手写 <Picture N>`
                : "上传参考图后可用 [[标签]] 引用";
            ta.placeholder = `第 ${idx + 1} 段画面与动作时间线：顺着上一段结尾继续；`
                + `对白写「…」自动转官方 <d>[中文] 格式，说话人标 (S1)；`
                + `运镜可写 The camera pushes in with small amplitude at slow speed；${poolHint}`;
            if (!node || it.idx === undefined) {
                ta.disabled = true;
                ta.title = it.idx === undefined ? "历史段只读（来自存档，非导演台状态）" : "画布上未找到节点";
            } else {
                ta.addEventListener("input", () => debouncePromptWrite(node, it.idx, ta.value));
                ta.addEventListener("blur", () => {
                    const key = `p${it.idx}`;
                    const t = _taTimers.get(key);
                    if (t) { clearTimeout(t); _taTimers.delete(key); }
                    setPromptText(node, it.idx, ta.value);
                    scheduleRefresh(200);
                });
            }
            body.append(ta);

            /* 引用素材：勾选本段用哪些（图/视/音混排，后端按类别独立编号压实 token） */
            if (node && it.idx !== undefined && data.ds.mode === "多参视频" && pool.length) {
                const seg = data.ds.segments[it.idx] || defaultSegment();
                const row = el("div", "h3d-refrow");
                row.append(el("label", "", "引用素材"));
                const pickedByKind = { image: 0, video: 0, audio: 0 };
                pool.forEach((a) => {
                    const k = a.kind || "image";
                    const on = (seg.refs || []).includes(a.label);
                    if (on) pickedByKind[k] += 1;
                    const chip = el("button", "h3d-refchip" + (on ? " on" : ""));
                    chip.type = "button";
                    chip.title = `${KIND_NAME[k]}素材：勾选后本段引用它（同类按勾选顺序编号 <${KIND_TOKEN[k]} k>）；`
                        + `提示词写 [[${a.label}]] 引用；单段上限 图${KIND_CAPS.image}/视${KIND_CAPS.video}/音${KIND_CAPS.audio}`;
                    if (k === "image") {
                        const im = document.createElement("img");
                        im.loading = "lazy";
                        im.src = inputViewUrl(a.file);
                        im.onerror = () => im.remove();
                        chip.append(im);
                    } else {
                        chip.insertAdjacentHTML("afterbegin", `<span class="h3d-kindmark">${KIND_ICON[k]}</span>`);
                    }
                    chip.append(document.createTextNode(a.label));
                    chip.onclick = () => { toggleSegmentRef(node, it.idx, a.label); scheduleRefresh(80); };
                    row.append(chip);
                });
                if ((seg.refs || []).length) {
                    row.insertAdjacentHTML("beforeend",
                        `<span class="h3d-secs-hint">图${pickedByKind.image}/${KIND_CAPS.image}`
                        + ` 视${pickedByKind.video}/${KIND_CAPS.video} 音${pickedByKind.audio}/${KIND_CAPS.audio}</span>`);
                    const reset = el("button", "h3d-btn", "↺ 全部");
                    reset.type = "button";
                    reset.style.padding = "3px 8px";
                    reset.style.fontSize = "10.5px";
                    reset.title = "清空段级选择 = 引用全部素材（与旧版行为一致）";
                    reset.onclick = () => {
                        const ds = getDs(node);
                        if (ds.segments[it.idx]) ds.segments[it.idx].refs = [];
                        setDs(node, ds);
                        scheduleRefresh(80);
                    };
                    row.append(reset);
                } else {
                    row.insertAdjacentHTML("beforeend",
                        '<span class="h3d-secs-hint">未勾选=引用全部</span>');
                }
                /* 引用语模板：把现成句式插入主提示词光标处 */
                const tpl = document.createElement("select");
                tpl.className = "h3d-reftpl";
                const optEmpty = document.createElement("option");
                optEmpty.value = "";
                optEmpty.textContent = "引用语…";
                tpl.append(optEmpty);
                REF_TEMPLATES.forEach(([name, fn], ti) => {
                    const o = document.createElement("option");
                    o.value = String(ti);
                    o.textContent = name;
                    tpl.append(o);
                });
                tpl.onchange = () => {
                    const ti = Number(tpl.value);
                    tpl.value = "";
                    if (!Number.isInteger(ti) || !REF_TEMPLATES[ti]) return;
                    const target = (seg.refs || [])[0] || pool[0].label;
                    const phrase = REF_TEMPLATES[ti][1](target);
                    insertAtCursor(ta, (ta.value && !/[\s,，。;；]$/.test(ta.value.slice(-1)) ? "\n" : "") + phrase);
                    setPromptText(node, it.idx, ta.value);
                    scheduleRefresh(150);
                };
                row.append(tpl);
                body.append(row);
            }

            /* 首尾帧图段级引用（仅首帧模式）：勾选本段是否参考首帧/尾帧图。
             * 未设置=默认（首段 i2v 首帧、末段尾帧终点锚，与旧档一致）；
             * 中段勾首帧图=头部身份锚（keyframe 注入抑制长链漂移）；
             * 任意段勾尾帧图=该段末帧锚（优先于每段尾帧锚定） */
            if (node && it.idx !== undefined && data.ds.mode === "首帧视频"
                    && (data.ds.first_frame || data.ds.end_frame)) {
                const seg = data.ds.segments[it.idx] || defaultSegment();
                const n = (data.ds.prompts || data.ds.segments || []).length;
                const def = [];
                if (data.ds.first_frame && it.idx === 0) def.push("首帧图");
                if (data.ds.end_frame && it.idx === n - 1) def.push("尾帧图");
                const cur = Array.isArray(seg.frame_refs) ? seg.frame_refs : def;
                const row = el("div", "h3d-refrow");
                row.append(el("label", "", "首尾帧图"));
                [["首帧图", data.ds.first_frame], ["尾帧图", data.ds.end_frame]].forEach(([name, file]) => {
                    if (!file) return;
                    const on = cur.includes(name);
                    const chip = el("button", "h3d-refchip" + (on ? " on" : ""));
                    chip.type = "button";
                    chip.title = name === "首帧图"
                        ? "勾选后本段参考首帧图片：首段=i2v 起手帧；中段=头部身份锚"
                            + "（keyframe 注入，抑制长链漂移）。不勾=本段不参考首帧图"
                        : "勾选后本段末帧锚定尾帧图片（同位置唯一锚，优先于每段尾帧锚定）；"
                            + "不勾=回落到每段尾帧锚定（若设）";
                    const im = document.createElement("img");
                    im.loading = "lazy";
                    im.src = inputViewUrl(file);
                    im.onerror = () => im.remove();
                    chip.append(im, document.createTextNode(name));
                    chip.onclick = () => { toggleSegmentFrameRef(node, it.idx, name); scheduleRefresh(80); };
                    row.append(chip);
                });
                if (!Array.isArray(seg.frame_refs)) {
                    row.insertAdjacentHTML("beforeend", '<span class="h3d-secs-hint">未设置=默认</span>');
                }
                body.append(row);
            }

            /* 分段处理中心：场景/角色提示词（可展开折叠） */
            if (node && it.idx !== undefined) {
                const seg = (data.ds.segments && data.ds.segments[it.idx]) || defaultSegment();
                const hasSeg = !!(seg.scene_prompt || seg.character_prompt || seg.soundscape
                    || seg.music || seg.unlink || seg.disabled);
                const det = el("details", "h3d-seg-panel" + (hasSeg ? " has-content" : ""));
                det.open = hasSeg;
                det.innerHTML = `<summary>分段处理 · 场景 / 角色 / 声音${hasSeg ? ' <span class="h3d-chip cyan">已填写</span>' : ""}</summary>`;
                const segBody = el("div", "h3d-seg-body");

                const segFields = [
                    ["scene_prompt", "场景提示词",
                        "本段场景（风格+环境+光照），如：实景电影感，雨夜的东京小巷，霓虹灯反射在湿柏油路上。留空省略"],
                    ["character_prompt", "角色提示词",
                        "本段角色（外观+服饰+位置），如：米色风衣短发女主，坐在便利店门口台阶上。留空省略"],
                    ["soundscape", "环境音",
                        "环境音+动作音（1-4句，官方 overall_soundscape），如：雨点敲打柏油路，远处车流，伞面撑开的轻响。留空=不约束"],
                    ["music", "配乐",
                        "背景配乐（乐器+节奏+动态，角色听不到的；官方 non_diegetic_music），如：慢速钢琴单音，弦乐渐强后淡出。留空=不约束"],
                ];
                for (const [field, label, ph] of segFields) {
                    const lab = el("label", "h3d-seg-label", label);
                    const ta = document.createElement("textarea");
                    ta.className = "h3d-ta h3d-seg-ta";
                    ta.rows = 2;
                    ta.value = seg[field] || "";
                    ta.placeholder = ph;
                    ta.addEventListener("input", () => debounceSegmentWrite(node, it.idx, field, ta.value));
                    ta.addEventListener("blur", () => {
                        const key = `s${it.idx}_${field}`;
                        const t = _taTimers.get(key);
                        if (t) { clearTimeout(t); _taTimers.delete(key); }
                        setSegmentField(node, it.idx, field, ta.value);
                        scheduleRefresh(200);
                    });
                    segBody.append(lab, ta);
                }

                /* 独立镜头：一键断开与上段的全部长视频衔接（硬切转场） */
                const unlinkRow = el("label", "h3d-unlink");
                const unlinkCb = document.createElement("input");
                unlinkCb.type = "checkbox";
                unlinkCb.checked = !!seg.unlink;
                unlinkRow.append(unlinkCb, document.createTextNode("🔗 独立镜头（与上段断链）"));
                unlinkRow.title = "本段与上一段完全断开衔接：不注入上段尾帧引导桥、不裁头、"
                    + "不做接缝精修/像素混合/接缝测量/响度对齐，上段也不做桥帧门控回退——段间硬切转场。"
                    + "适用于与上一镜头毫无关联的独立画面。本段每段尾帧锚定与素材引用不受影响；"
                    + "切换此开关会使该段起重跑（指纹语义正确）";
                unlinkCb.onchange = () => {
                    setSegmentField(node, it.idx, "unlink", unlinkCb.checked);
                    scheduleRefresh(60);
                };
                segBody.append(unlinkRow);

                /* 不上链（段禁用）：勾选框即状态——点选立即生效并当场反映，
                   取代旧版「⏸ 不上链」按钮（点击后界面无反馈的问题根源） */
                const offRow = el("label", "h3d-unlink h3d-offrow");
                const offCb = document.createElement("input");
                offCb.type = "checkbox";
                offCb.checked = !!seg.disabled;
                offRow.append(offCb, document.createTextNode("⏸ 不上链（本段跳过执行）"));
                offRow.title = "本段保留在链上但跳过执行：不采样不解码不进成片，"
                    + "槽位与存档记录原样保留（取消勾选即零成本恢复，已完成段直接沿用存档），"
                    + "下段锚定自动跨接到本段之前最近一个已执行段的尾帧；"
                    + "若本段尚未生成，恢复后会正常采样";
                offCb.onchange = () => {
                    setSegmentField(node, it.idx, "disabled", offCb.checked);
                    scheduleRefresh(60);
                };
                segBody.append(offRow);

                det.append(segBody);
                body.append(det);
            }
        }

        const actions = el("div", "h3d-actions");
        if (!node) {
            actions.append(el("span", "h3d-hint", "只读（画布上未找到节点）"));
        } else if (isInsert) {
            if (!isDone) {
                const rm = el("button", "h3d-btn h3d-btn-danger", "✕ 移除插入");
                rm.title = "从「插入视频」清单删除这一条（其后段落自动重做）";
                rm.onclick = () => removeInsert(it.pos, it.file);
                actions.append(rm);
            } else {
                actions.append(el("span", "h3d-hint", "更换/移除插入 = 其后重做"));
            }
            actions.append(insertButton("在此段后插入", idx + 2));
        } else {
            if (isDisabled) {
                /* 已禁用段：恢复入口（勾选框在「分段处理」面板内也可取消勾选；
                   重摇/继续/二采均无意义——不执行不进成片） */
                const back = el("button", "h3d-btn h3d-btn-cyan", "▶ 重新上链");
                back.title = "恢复本段自动执行：已完成段直接沿用存档（零成本），"
                    + "未完成段下次运行起正常采样；前后接缝自动重新衔接";
                back.onclick = () => {
                    setSegmentField(node, it.idx, "disabled", false);
                    scheduleRefresh(60);
                };
                actions.append(back);
            } else if (isDone) {
                /* 已完成段两个入口：「从这段继续」= 其后全部级联重做（主动想全重来时用）；
                   「重摇」= 只重做本段（换种子，按锚定模式接缝），其余段保留不动——
                   可标记多段后由页脚统一提交（间隔选择重做，互不级联） */
                const cont = el("button", "h3d-btn h3d-btn-cta", "▶ 从这段继续");
                cont.title = `保留段 1–${idx + 1}，从段 ${idx + 2} 起连续生成到链尾`
                    + (idx + 1 < done ? `（段 ${idx + 2} 之后的旧存档会被丢弃）` : "");
                cont.onclick = () => continueFromSegment(idx, done);
                actions.append(cont);
                const rerollBtn = el("button", "h3d-btn", redoMode ? "🎲 重摇·改标记" : "🎲 重摇此段");
                rerollBtn.title = `只重做段 ${idx + 1}（换种子重新采样，按锚定模式接缝），其余段保留不动；`
                    + `可连续标记多段，点页脚「重摇已标记 N 段」一次提交；`
                    + `「从这段继续」才是该段之后全部重做`;
                rerollBtn.onclick = () => openRerollModal(idx, data);
                actions.append(rerollBtn);
            } else {
                const runOne = el("button", "h3d-btn h3d-btn-cyan", "▶ 只跑这段");
                runOne.title = `只生成段 ${idx + 1} 后暂停（审片模式）：预览单段效果，满意再继续整链`;
                runOne.onclick = () => doRunOnly(idx + 1, done);
                actions.append(runOne);
                actions.append(insertButton(`插视频到段 ${idx + 1} 前`, idx + 1));
            }
            if (node && it.idx !== undefined) {
                const rm = el("button", "h3d-btn h3d-btn-danger", "✕ 删除此段");
                rm.title = "删除这一段提示词（其后段落自动前移）";
                rm.onclick = () => removePromptSegment(node, it.idx);
                actions.append(rm);
            }
        }
        /* 单段重新二采（仅生成段）：已完成段随时可单独补做/重做二采，不动其他段
           ——显示条件放宽到"选了模型"即可见（未做过二采的段也能补做）；
           插入视频段/序章是外部成品素材，不做二采（成片拼接时按需缩放对齐）；
           已禁用段不进成片，不做二采 */
        if (isDone && !isDisabled && !isInsert && node && state?.dir
                && (upRec?.done || data.ds?.upscale?.model)) {
            const rst = el("button", "h3d-btn", "🔄 重新二采此段");
            rst.title = "清本段二采记录并立即重做：latent 存档载入 → 神经放大重采样 → 覆盖段视频"
                + "（不做视频编解码）。其他段不受影响。"
                + "参数未变时重渲=同输出；效果不满意请调右栏二采参数";
            rst.onclick = () => doUpscaleSeg(rst, state.dir, idx + off + 1, idx + 1, done, total);
            actions.append(rst);
        }
        body.append(actions);

        card.append(thumb, body);
        wrap.append(card);
    });

    /* 容器拖放：把手拖动中按指针 Y 换算插入线并吸附显示（setDropLine 只在
       可落位处画线），松手时提交调序。dragover 高频触发——dropIndexAt 是
       纯 DOM 测量、reorderChain 在 setDropLine 内纯计算，无状态副作用。 */
    if (node && !mergeSel.on) {
        wrap.addEventListener("dragover", (e) => {
            if (!dragSeg) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            setDropLine(wrap, data, dragSeg.idx, dropIndexAt(wrap, e.clientY));
        });
        wrap.addEventListener("drop", (e) => {
            if (!dragSeg) return;
            e.preventDefault();
            const dragIdx = dragSeg.idx;
            dragSeg = null;   // 先清再提交：applyReorder 内的 scheduleRefresh 不受拖动态干扰
            wrap.querySelectorAll(".h3d-dragging, .h3d-drop-before, .h3d-drop-end")
                .forEach((n) => n.classList.remove("h3d-dragging", "h3d-drop-before", "h3d-drop-end"));
            applyReorder(node, data, dragIdx, dropIndexAt(wrap, e.clientY));
        });
    }
    return wrap;
}

function insertButton(label, pos) {
    const b = el("button", "h3d-btn", `📎 ${label}`);
    b.title = `选择本地视频上传到 input 目录，并写入「插入视频」= ${pos}|文件名`;
    b.onclick = () => pickInsertVideo(pos);
    return b;
}

/* ---- 素材与参考（状态驱动：标签素材池存 JSON，缩略图直接回显；配套工作流节点做画布镜像） ---- */

function assetCard(title, file, onRemove) {
    const card = el("div", "h3d-asset" + (file ? " on" : ""));
    const thumb = el("div", "h3d-asset-thumb", file ? "" : "<span>IMG</span>");
    if (file) {
        const im = document.createElement("img");
        im.loading = "lazy";
        im.src = inputViewUrl(file);
        im.alt = title;
        im.onerror = () => { thumb.replaceChildren(el("span", "", "⚠")); };
        thumb.append(im);
    }
    const copy = el("div", "h3d-asset-copy");
    copy.innerHTML = `<strong>${escapeHtml(title)}</strong><small>${escapeHtml(file || "未设置")}</small>`;
    card.append(thumb, copy);
    if (file && onRemove) {
        const acts = el("div", "h3d-asset-acts");
        const rm = el("button", "h3d-btn h3d-btn-danger", "✕");
        rm.title = "移除";
        rm.onclick = onRemove;
        acts.append(rm);
        card.append(acts);
    }
    return card;
}

/** 多参模式：带标签编辑的素材卡（重命名同步所有段级引用；图/视/音三类混排） */
function labeledAssetCard(node, ds, idx) {
    const a = ds.ref_assets[idx];
    const file = a.file;
    const kind = a.kind || "image";
    const card = el("div", "h3d-asset on");
    const thumb = el("div", "h3d-asset-thumb");
    if (kind === "image") {
        const im = document.createElement("img");
        im.loading = "lazy";
        im.src = inputViewUrl(file);
        im.alt = a.label;
        im.onerror = () => { thumb.replaceChildren(el("span", "", "⚠")); };
        thumb.append(im);
    } else {
        thumb.innerHTML = `<span class="h3d-kindmark big">${KIND_ICON[kind]}</span>`;
        thumb.title = `${KIND_NAME[kind]}素材：${file}`;
    }

    const copy = el("div", "h3d-asset-copy");
    const labelInp = document.createElement("input");
    labelInp.className = "h3d-labelinp";
    labelInp.value = a.label;
    labelInp.spellcheck = false;
    labelInp.maxLength = 12;
    labelInp.title = "素材标签：提示词用 [[标签]] 引用；回车或失焦提交，重名自动加后缀";
    const commit = () => {
        const next = renameAssetLabel(node, idx, labelInp.value);
        if (next !== labelInp.value) labelInp.value = next;
        scheduleRefresh(120);
    };
    labelInp.addEventListener("change", commit);
    labelInp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); labelInp.blur(); } });
    const quick = el("div", "h3d-quicklbl");
    QUICK_LABELS.forEach((q) => {
        const b = el("button", "", q);
        b.type = "button";
        b.title = `把标签设为「${q}」（被占用时自动加后缀）`;
        b.onclick = () => {
            const taken = new Set(ds.ref_assets.filter((_x, i) => i !== idx).map((x) => x.label));
            const want = uniqueLabelFrom(taken, q);
            labelInp.value = want;
            const got = renameAssetLabel(node, idx, want);
            labelInp.value = got;
            scheduleRefresh(120);
        };
        quick.append(b);
    });
    const usage = el("div", "h3d-asset-usage");
    const usedBy = (ds.segments || []).filter((s) => (s.refs || []).includes(a.label)).length;
    const defaultAll = (ds.segments || []).every((s) => !(s.refs || []).length);
    const sameKind = ds.ref_assets.filter((x) => (x.kind || "image") === kind).length;
    usage.innerHTML = `<span class="h3d-chip">${KIND_ICON[kind]}${KIND_NAME[kind]}·${sameKind}/${KIND_CAPS[kind]}</span>`
        + (usedBy ? `<span class="h3d-chip ok">${usedBy} 段指定</span>` : "")
        + (defaultAll && (ds.segments || []).length ? '<span class="h3d-chip cyan">默认全段引用</span>' : "");
    copy.append(labelInp, quick, usage,
        el("small", "", `${escapeHtml(file)} · 提示词写 [[${escapeHtml(a.label)}]] → &lt;${KIND_TOKEN[kind]} k&gt;`));

    const acts = el("div", "h3d-asset-acts");
    const rm = el("button", "h3d-btn h3d-btn-danger", "✕");
    rm.title = "移除该素材（画布镜像节点隐藏，连线保留）";
    rm.onclick = () => { removeRefImage(node, idx); };
    acts.append(rm);
    card.append(thumb, copy, acts);
    return card;
}

function renderAssetsZone(sec, data) {
    const { node, ds } = data;
    sec.replaceChildren();
    sec.append(el("div", "h3d-sechead",
        "<strong>素材池 · 分段处理中心</strong><small>上传素材 → 打标签 → 各段卡片勾选引用</small>"));

    /* 模式条 */
    const mode = ds.mode;
    const mbar = el("div", "h3d-modebar");
    for (const [code, label, desc] of MODES) {
        const b = el("button", "h3d-mode" + (code === mode ? " active" : ""));
        const sub = code === "多参视频" ? "ref2va · [[标签]]"
            : code === "首帧视频" ? "fl2va · 首帧" : "fl2va · 纯文本";
        b.innerHTML = `${escapeHtml(label)}<small>${sub}</small>`;
        b.title = desc;
        b.onclick = () => { if (!node) return; setMode(node, code); scheduleRefresh(120); };
        mbar.append(b);
    }
    sec.append(mbar);

    const box = el("div", "h3d-assets");
    if (!node) {
        if (window.H3_DEFAULT_WORKFLOW) {
            const guide = el("div", "h3d-loadwf");
            guide.innerHTML = "<p>画布上还没有 H3 Seamless Chain 节点</p>"
                + "<p>一键载入配套工作流：模型加载 + 主节点 + 常驻连线的素材池<br>（素材节点默认隐藏，上传素材时自动点亮，不再动态建线）</p>";
            const wf = el("button", "h3d-btn h3d-btn-cyan", "⚡ 一键载入配套工作流");
            wf.onclick = loadDefaultWorkflow;
            guide.append(wf);
            box.append(guide);
        } else {
            box.append(el("div", "h3d-empty", "画布上未找到 H3 Seamless Chain 节点"));
        }
        sec.append(box);
        return;
    }

    if (mode === "文生视频") {
        box.append(el("div", "h3d-empty",
            "文生模式：无需图片素材，提示词直接描述画面即可。<br>需要起手图请切「首帧」，需要多参考请切「多参」。"));
    } else if (mode === "首帧视频") {
        const file = ds.first_frame;
        const card = assetCard("首帧图片", file,
            file ? () => { removeFirstFrame(node); } : null);
        card.querySelector("small").textContent = file || "未设置（第 1 段的起始帧）";
        const acts = card.querySelector(".h3d-asset-acts") || el("div", "h3d-asset-acts");
        const up = el("button", "h3d-btn h3d-btn-cyan", file ? "替换" : "上传");
        up.title = "上传图片到 input 目录，文件名存入导演台状态，画布「首帧图」节点同步点亮";
        up.onclick = () => pickAsset("first");
        acts.insertBefore(up, acts.firstChild);
        if (!card.querySelector(".h3d-asset-acts")) card.append(acts);
        box.append(card);
        /* 尾帧图片（FL2VA 官方首尾帧）：整链最后一段的末帧 keyframe = 剧情终点 */
        const ef = ds.end_frame;
        const ecard = assetCard("尾帧图片", ef,
            ef ? () => { removeEndFrame(node); } : null);
        ecard.querySelector("small").textContent = ef || "未设置（可选，整链终点的到达画面）";
        const eacts = ecard.querySelector(".h3d-asset-acts") || el("div", "h3d-asset-acts");
        const eup = el("button", "h3d-btn h3d-btn-cyan", ef ? "替换" : "上传");
        eup.title = "FL2VA 官方首尾帧：整链最后一段的末帧锚（剧情终点画面）。"
            + "末段提示词写「如何走到这个画面」，不复述图内静态内容；设了它末段不再叠加每段尾帧锚定。"
            + "画布「目标尾帧图」节点同步点亮";
        eup.onclick = () => pickAsset("end");
        eacts.insertBefore(eup, eacts.firstChild);
        if (!ecard.querySelector(".h3d-asset-acts")) ecard.append(eacts);
        box.append(ecard);
    } else {
        /* 多参：标签素材池（图/视/音三类，状态驱动 + 画布镜像） */
        ds.ref_assets.forEach((_a, i) => {
            box.append(labeledAssetCard(node, ds, i));
        });
        const upRow = el("div", "h3d-upload-row");
        for (const k of KIND_LIST) {
            const sameKind = ds.ref_assets.filter((a) => (a.kind || "image") === k).length;
            const full = sameKind >= KIND_CAPS[k];
            const b = el("button", "h3d-btn h3d-addasset", `＋ ${KIND_NAME[k]}`);
            b.disabled = full;
            b.title = full
                ? `${KIND_NAME[k]}素材已达上限 ${KIND_CAPS[k]} 个（官方单段上限）`
                : `上传${KIND_NAME[k]} → 存入标签素材池（提示词写 [[标签]] → &lt;${KIND_TOKEN[k]} k&gt;；`
                    + `配套工作流对应节点自动点亮，手摆工作流不受影响）`;
            b.onclick = () => pickAsset("pool", k);
            upRow.append(b);
        }
        box.append(upRow);
    }

    /* 每段尾帧锚定（身份锚点，任意模式可用）：每段末尾注入参考帧，与段首引导桥形成双锚约束防漂移
     * （与「尾帧图片」不同：那是 FL2VA 剧情终点，只落最后一段；这是全链恒定的身份锚） */
    {
        const lf = ds.last_frame;
        const card = assetCard("每段尾帧锚定", lf, lf ? () => { removeLastFrame(node); } : null);
        card.querySelector("small").textContent = lf || "未设置（可选，建议角色正面清晰帧）";
        const acts = card.querySelector(".h3d-asset-acts") || el("div", "h3d-asset-acts");
        const up = el("button", "h3d-btn h3d-btn-cyan", lf ? "替换" : "上传");
        up.title = "每段尾帧锚定（任意模式可用）：同一张身份锚点图注入每段末尾（人物/场景参考），"
            + "与段首引导桥形成「隧道」双锚约束，防止过了桥窗口后主体漂移。"
            + "注意与「尾帧图片」不同：那是首帧模式下的 FL2VA 剧情终点（只落最后一段）。"
            + "画布「尾帧图」节点同步点亮";
        up.onclick = () => pickAsset("last");
        acts.insertBefore(up, acts.firstChild);
        if (!card.querySelector(".h3d-asset-acts")) card.append(acts);
        box.append(card);
    }
    sec.append(box);
    sec.append(el("div", "h3d-foot",
        "标签素材池（图 ≤9 / 视 ≤3 / 音 ≤3，官方单段上限）：每类素材打标签（角色1 / 场景1…），"
        + "段落卡片里勾选本段引用哪些——未勾选的素材不进该段（后端按段压实重编号），提示词写 [[标签]] 引用"
        + "（图→&lt;Picture k&gt;、视→&lt;Video k&gt;、音→&lt;Audio j&gt;）。参考视频的原声自动配对，无需单独传音轨。<br>"
        + "尾帧锚定不限模式：不想锚定就不上传。<br>"
        + "配套工作流下素材节点由导演台点亮/隐藏——连线常驻，只是隐藏，请勿删除。"));
}

/* ---- 链参数（面板直写画布控件）：常规五项 + 高级设置收纳其余全部 ---- */

const PRIMARY_DEFS = [W_AR, W_MP, W_DUR, W_SEED, "步数"];
const ADVANCED_DEFS = [
    "引导帧数", "CFG", "采样器", "调度器",
    "存档目录", "审片模式", "自动保存", "自动成片", "重跑起始段", "一采编码",
    "桥帧门控", "清晰度阈值", "回退上限", "锚定加噪",
    "接缝重摇", "重摇阈值", "重摇上限", "递减锚定",
    W_WIDTH, W_HEIGHT,
];
const PARAM_LABELS = { [W_DUR]: "每段时长(秒) · 新段默认" };

function paramsSig(node) {
    if (!node) return "n";
    return [...PRIMARY_DEFS, ...ADVANCED_DEFS].map((n) => {
        const w = (node.widgets || []).find((x) => x.name === n);
        return w ? String(w.value) : "-";
    }).join("|");
}

/** 单个节点控件的表单域（combo→select，数值→number 输入；种子带🎲） */
function renderWidgetField(node, name, labelOverride) {
    const w = node ? (node.widgets || []).find((x) => x.name === name) : null;
    const field = el("div", "h3d-param");
    field.append(el("label", "", escapeHtml(labelOverride || name)));
    if (!w) {
        field.append(el("span", "h3d-hint", "—"));
        return field;
    }
    const opts = w.options && Array.isArray(w.options.values) ? w.options.values : null;
    if (opts) {
        const sel = document.createElement("select");
        sel.className = "h3d-select";
        for (const v of opts) {
            const o = document.createElement("option");
            o.value = v;
            o.textContent = v;
            if (String(v) === String(w.value)) o.selected = true;
            sel.append(o);
        }
        sel.onchange = () => { setWidgetValue(node, name, sel.value); repaintAfterWidget(name); };
        field.append(sel);
    } else {
        const row = el("div", "h3d-seedrow");
        const inp = document.createElement("input");
        inp.type = "number";
        inp.value = w.value;
        const step = w.options && w.options.step;
        inp.step = step || (name === W_MP || name === "CFG" || name === W_DUR ? 0.1 : 1);
        if (w.options && Number.isFinite(w.options.min)) inp.min = w.options.min;
        if (w.options && Number.isFinite(w.options.max)) inp.max = w.options.max;
        inp.onchange = () => { setWidgetValue(node, name, Number(inp.value)); repaintAfterWidget(name); };
        inp.addEventListener("wheel", (e) => e.preventDefault(), { passive: false });   // 滚轮滚动面板时不改数值
        row.append(inp);
        if (name === W_MP) {
            row.append(el("span", "h3d-secs-hint", "MP"));
            inp.title = "目标总像素 0.1–2.0MP，直接输入数字（官方口径 1MP=1024×1024）："
                + "0.2 草稿(608×352) / 0.5 预览(960×544) / 0.98 H3原生(1344×768) / 1.0(1376×768) / 2.0(1920×1088)";
            inp.min = inp.min || 0.1;   // 控件 options 未下发时兜底
            inp.max = inp.max || 2.0;
        }
        if (name === W_SEED) {
            const dice = el("button", "h3d-btn", "🎲");
            dice.title = "随机种子";
            dice.onclick = () => {
                const v = Math.floor(Math.random() * 2 ** 48);
                setWidgetValue(node, name, v);
                inp.value = v;
                repaintAfterWidget(name);
            };
            row.append(dice);
        }
        field.append(row);
    }
    return field;
}

function renderParamsZone(sec, data) {
    const { node } = data;
    sec.replaceChildren();
    sec.append(el("div", "h3d-sechead",
        "<strong>链参数</strong><small>常规五项直改；其余收进「高级设置」</small>"));
    if (!node) {
        sec.append(el("div", "h3d-empty", "画布上未找到节点，参数面板不可用"));
        return;
    }
    const grid = el("div", "h3d-params");
    for (const name of PRIMARY_DEFS) {
        grid.append(renderWidgetField(node, name, PARAM_LABELS[name]));
    }
    /* 换算徽章：宽高比+百万像素 -> 实际画布；自定义模式改为显示宽/高输入 */
    const ar = String(getWidgetValue(node, W_AR) ?? "");
    if (AR_RATIO[ar]) {
        const badgeTxt = canvasBadgeText(node);
        if (badgeTxt) {
            const b = el("div", "h3d-convbadge", `${escapeHtml(badgeTxt)} · 32倍数对齐`);
            b.title = "官方 Resolution Selector 同款换算：1MP=1024×1024，两侧各自 round 对齐 32 倍数";
            grid.append(b);
        }
    } else {
        grid.append(renderWidgetField(node, W_WIDTH));
        grid.append(renderWidgetField(node, W_HEIGHT));
    }
    /* 高级设置：其余全部参数（默认收起） */
    const adv = el("details", "h3d-adv");
    adv.innerHTML = "<summary>⚙ 高级设置（引导 / 采样 / 审片 / 存档 / 接缝 / 画布微调）</summary>";
    const agrid = el("div", "h3d-adv-grid");
    for (const name of ADVANCED_DEFS) {
        if (ar !== "自定义" && (name === W_WIDTH || name === W_HEIGHT)) continue;
        agrid.append(renderWidgetField(node, name));
    }
    adv.append(agrid);
    grid.append(adv);
    sec.append(grid);
    sec.append(el("div", "h3d-foot",
        "全部节点参数已收纳于此：改「高级设置」同样随工作流保存；「生成模式」由左侧模式条控制。"));
}

/* ---- 实验性功能面板（右栏，ds.experiments 扁平契约 {<id>:true, params:{...}} 驱动） ---- */

function expActiveList(ex) { return Object.keys(ex || {}).filter((k) => ex[k] === true); }

/* 主开关锁定态：ds JSON 里的 locked 键优先；缺省时 有开启=解锁 / 全关=锁定。
 * locked 持久化进 ds JSON（后端 ExperimentContext 只认 defs 内 id 与 params，自动忽略该键），
 * 保证刷新页面后「启用实验功能」的解锁选择不丢失。 */
function expLocked(ex) {
    if (ex && typeof ex.locked === "boolean") return ex.locked;
    return expActiveList(ex).length === 0;
}

/* 落盘实验开关/参数并联动重渲面板（切换实验组合由后端指纹判整链重做） */
function setExpOn(node, id, on) {
    const ds = getDs(node);
    if (on) ds.experiments[id] = true;
    else delete ds.experiments[id];
    setDs(node, ds);
    repaintExperiments();        // 勾选后参数区当场出现（任意编辑动作即时重渲）
}

function setExpParam(node, id, key, value) {
    const ds = getDs(node);
    (ds.experiments.params[id] = ds.experiments.params[id] || {})[key] = value;
    setDs(node, ds);
    repaintExperiments();
}

/* 主开关：true=锁定（全部关闭并禁用子项），false=解锁（子项可勾选，仍保持全关） */
function setExpLocked(node, locked) {
    const ds = getDs(node);
    if (locked) for (const id of expActiveList(ds.experiments)) delete ds.experiments[id];
    ds.experiments.locked = !!locked;
    setDs(node, ds);
    repaintExperiments();   // 任意编辑动作即时重渲（绕过 updateDesk 焦点守卫）
}

/* 实验面板局部重渲：编辑动作（勾选/改参/主开关）后即时反映，不依赖全局 refresh，
 * 因而绕过 updateDesk 的「焦点在区内则不重建」守卫——那些守卫是为保护全局刷新时
 * 正在输入的文本框而设；而此处都是「提交型」编辑：勾选框提交后即重建无妨，数值输入
 * 走 onchange（提交时焦点本就离开）。直接读画布控件，零网络往返，点一下当场更新
 * （参数区出现 / 父级徽章计数 / 子项禁用态）。父级 <details> 折叠态由 renderExperimentsZone 自身保留。
 * 同步更新 rExp.dataset.sig，避免紧随的全局 refresh 因签名已一致而重复重建。 */
function repaintExperiments() {
    if (!desk) return;
    const z = desk.zones;
    if (!z.rExp) return;
    try {
        const node = findNode();
        const ds = node ? getDs(node) : {};
        const esig = JSON.stringify([ds?.experiments ?? {},
            EXP.defs ? EXP.defs.length : 0, EXP.forceDisabled, EXP.failed, !!node]);
        z.rExp.dataset.sig = esig;   // 与 updateDesk 同公式，防止后续全局刷新重复重建
        renderExperimentsZone(z.rExp, { node });
    } catch (e) {
        console.warn("[h3-director] repaintExperiments failed:", e);
    }
}

/* 二采/链参数面板局部重渲：与 repaintExperiments 同款机制，修「编辑后界面不动」。
 * 病根：数值/下拉的 onchange 以回车提交时焦点仍在区内控件上，updateDesk 的焦点守卫
 * 会跳过重渲，且此后没有事件再触发刷新——目标画布徽章、换算徽章、「已开启」角标
 * 就一直停留旧值。此处改为提交型编辑当场重建：直读画布控件零网络往返；
 * mf/state/模型列表沿用最近一次全局刷新的缓存（desk.lastData），只关乎本地参数显示。
 * 同步刷新 dataset.sig（与 updateDesk 同公式），防止紧随的全局 refresh 重复重建。 */
function repaintUpscale() {
    if (!desk || !desk.zones || !desk.zones.rUpscale) return;
    try {
        const data = Object.assign({}, desk.lastData, { node: findNode() });
        data.ds = data.node ? getDs(data.node) : (data.ds || {});
        if (!data.ds.upscale) return;
        desk.zones.rUpscale.dataset.sig = upscaleSig(data);
        renderUpscaleZone(desk.zones.rUpscale, data);
    } catch (e) {
        console.warn("[h3-director] repaintUpscale failed:", e);
    }
}

function repaintParams() {
    if (!desk || !desk.zones || !desk.zones.rParams) return;
    try {
        const node = findNode();
        desk.zones.rParams.dataset.sig = paramsSig(node);
        renderParamsZone(desk.zones.rParams, { node });
    } catch (e) {
        console.warn("[h3-director] repaintParams failed:", e);
    }
}

/** 链参数编辑后的跨区联动：画幅四件（宽高比/百万像素/宽/高）变更重建本区
 *  （换算徽章、自定义模式宽高输入切换）与二采区（目标画布）；其余参数无跨区显示不动。 */
function repaintAfterWidget(name) {
    if (name !== W_AR && name !== W_MP && name !== W_WIDTH && name !== W_HEIGHT) return;
    repaintParams();
    repaintUpscale();
}

function renderExperimentsZone(sec, data) {
    const { node } = data;
    // 重渲前记住父级折叠态，避免每次 setDs 触发的重渲把面板弹回默认
    const wasOpen = sec.querySelector("details.h3d-exp-sec")?.open;
    sec.replaceChildren();
    if (!node) {
        sec.append(el("div", "h3d-empty", "画布上未找到节点，实验性功能面板不可用"));
        return;
    }
    loadExperimentDefs();   // 兜底触发（幂等）
    const ds = getDs(node);
    const ex = ds.experiments || {};
    const active = expActiveList(ex).length;
    const locked = expLocked(ex);

    // 父级单 <details> 折叠（与「高级设置」一致）；子卡不折叠
    const det = el("details", "h3d-exp-sec");
    det.open = wasOpen ?? active > 0;          // 首次：有开启项默认展开，全关默认收起
    const sum = document.createElement("summary");
    sum.insertAdjacentHTML("beforeend",
        "<strong>🧪 实验性功能</strong><small>生成期干预 · 默认全关 · 逐项试效果再试组合</small>");
    if (active) sum.insertAdjacentHTML("beforeend", badge(`开启 ${active} 项`, "cyan"));
    // 主开关：永远真实可按（不用 disabled 属性）；点在 summary 里须阻止折叠联动
    const msbtn = el("button", "h3d-btn " + (locked ? "h3d-btn-cyan" : "h3d-btn-warn"),
        locked ? "▶ 启用实验功能" : `✕ 全部关闭（当前${active}项）`);
    msbtn.title = locked
        ? "解锁下方实验复选框（仍保持全关，逐项手动开启）"
        : "一键关闭所有实验并锁定复选框；不同实验组合会触发对应段重新生成（后端指纹），结论请用同一项目文件夹对比。";
    msbtn.onclick = (ev) => { ev.preventDefault(); ev.stopPropagation(); setExpLocked(node, !locked); };
    sum.append(msbtn);
    det.append(sum);

    if (EXP.forceDisabled) {
        det.append(el("div", "h3d-exp-ban",
            "后端已强制关闭实验性功能（H3_EXPERIMENTS=0），以下开关不生效。"));
    } else if (!EXP.defs) {
        det.append(el("div", "h3d-empty", EXP.failed
            ? `实验定义拉取失败（${EXP.failed}）：请确认后端已重启、路由已注册。`
            : "实验定义加载中…"));
        if (EXP.failed) {
            const retry = el("button", "h3d-btn", "重试");
            retry.onclick = () => { EXP.failed = ""; loadExperimentDefs(); };
            det.append(retry);
        }
    } else {
        for (const e of EXP.defs) {
            const isOn = ex[e.id] === true;
            const card = el("div", "h3d-exp-card" + (isOn ? " on" : ""));
            const head = el("div", "h3d-exp-head");
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.checked = isOn;
            cb.disabled = locked || EXP.forceDisabled;   // 锁定/后端强制关闭时子项灰显不可选
            cb.onchange = () => setExpOn(node, e.id, cb.checked);
            head.append(cb);
            head.append(el("strong", "", escapeHtml(e.name)));
            head.insertAdjacentHTML("beforeend", badge(e.group, "media"));  // badge 返回 HTML 字符串，须以 HTML 方式插入
            card.append(head);
            card.append(el("small", "h3d-exp-desc", escapeHtml(e.desc)));
            // 参数区：仅该实验开启时渲染；setExpOn -> repaintExperiments() 保证勾选即现
            if (isOn) {
                const paramBox = el("div", "h3d-exp-params");
                for (const p of e.params || []) {
                    const row = el("div", "h3d-param");
                    row.append(el("label", "", escapeHtml(p.key)));
                    const cur = ds.experiments.params?.[e.id]?.[p.key] ?? p.def;
                    if (p.type === "enum" && p.opts) {
                        const sel = document.createElement("select");
                        sel.className = "h3d-select";
                        for (const v of p.opts) {
                            const o = document.createElement("option");
                            o.value = v; o.textContent = v;
                            if (String(v) === String(cur)) o.selected = true;
                            sel.append(o);
                        }
                        sel.onchange = () => setExpParam(node, e.id, p.key, sel.value);
                        row.append(sel);
                    } else {
                        // 包 .h3d-seedrow 命中既有深色 width:100% 样式，避免浏览器默认亮色/宽度溢出
                        const sr = el("div", "h3d-seedrow");
                        const inp = document.createElement("input");
                        inp.type = "number";
                        inp.value = cur;
                        inp.min = String(p.min); inp.max = String(p.max); inp.step = String(p.step);
                        inp.addEventListener("wheel", (ev) => ev.preventDefault(), { passive: false });
                        inp.onchange = () => {
                            const n = Number(inp.value);
                            if (isFinite(n)) setExpParam(node, e.id, p.key, Math.min(p.max, Math.max(p.min, n)));
                        };
                        sr.append(inp);
                        row.append(sr);
                    }
                    paramBox.append(row);
                }
                card.append(paramBox);
            }
            det.append(card);
        }
        det.append(el("div", "h3d-foot",
            "全部默认关闭；切换实验组合会触发对应段重新生成（改存档指纹判定整链重做）。"
            + "逐项开启试效果，再试组合；结论请用同一项目文件夹对比，避免缓存污染。"));
    }
    sec.append(det);
}

/* ---- 潜空间放大二采面板（右栏，独立后处理通道：主链完成后的清扫执行） ---- */

function upscaleSig(data) {
    const up = data.ds?.upscale || {};
    return JSON.stringify([
        up.mode ?? "", up.model ?? "", up.arch ?? "", up.scale ?? 0, up.denoise ?? 0,
        up.steps ?? 0, up.cfg ?? 0, up.precision ?? "", up.time_bias ?? 0, up.mix ?? 0,
        up.adaptive === true, up.shift ?? 0, (up.include || []).join(","),
        up.stg ?? 0, up.stg_block ?? 25, up.passes ?? 1, up.decay ?? 0.5,
        up.sharpen ?? 0, up.pixel_sharpen ?? 0, up.encode ?? "标准",
        up.sampler ?? "", up.scheduler ?? "", up.retry === true, up.retry_target ?? 0,
        (data.upscaleModels || []).join(","),
        data.mf?.upscale?.hash ?? "",
        (data.mf?.upscale?.segs || []).filter((r) => r && r.done).length,
        (data.mf?.upscale?.finals || []).length,
        data.state?.done ?? 0,
        `${getWidgetValue(data.node, W_WIDTH) ?? ""}x${getWidgetValue(data.node, W_HEIGHT) ?? ""}`,
    ]);
}

/** 单个数值输入（带单位后缀与滚轮防误触） */
function upNumField(label, value, min, max, step, tip, commit) {
    const field = el("div", "h3d-param");
    field.append(el("label", "", label));
    const row = el("div", "h3d-seedrow");
    const inp = document.createElement("input");
    inp.type = "number";
    inp.value = value;
    inp.min = String(min);
    inp.max = String(max);
    inp.step = String(step);
    inp.title = tip || "";
    inp.addEventListener("wheel", (e) => e.preventDefault(), { passive: false });
    inp.onchange = () => {
        const n = Number(inp.value);
        if (isFinite(n)) commit(Math.min(max, Math.max(min, n)));
    };
    row.append(inp);
    field.append(row);
    return field;
}

function renderUpscaleZone(sec, data) {
    const { node, state, mf } = data;
    sec.replaceChildren();
    const up = data.ds.upscale;
    const on = up.mode !== "关闭";
    const models = data.upscaleModels || [];
    const done = mf?.done ?? state?.done ?? 0;
    const upRecs = mf?.upscale?.segs || [];
    const upDone = upRecs.filter((r) => r && r.done).length;
    const upFinals = mf?.upscale?.finals || [];

    const det = el("details", "h3d-adv h3d-updet" + (on ? " on" : ""));
    det.open = on;
    det.innerHTML = `<summary>✦ 潜空间放大二采${on ? ' <span class="h3d-chip cyan">已开启</span>' : ""}</summary>`;
    const body = el("div", "h3d-adv-grid h3d-upgrid");

    if (!node) {
        body.append(el("div", "h3d-empty", "画布上未找到节点，二采面板不可用"));
        det.append(body);
        sec.append(det);
        return;
    }

    /* 模式：关闭 / 跟随生成 */
    const modeField = el("div", "h3d-param");
    modeField.append(el("label", "", "模式"));
    const modeSel = document.createElement("select");
    modeSel.className = "h3d-select";
    modeSel.title = "跟随生成：每段采样定稿后立即二采，段视频直接存高清结果（逐段审片时即「生成一段二采一段」）；"
        + "关闭：不执行二采（已产出的高清分段/成片不受影响）。"
        + "二采参数不进基础链指纹——改参数只重做二采，不动已生成段。"
        + "单段补做/重做：段卡片「🔄 重新二采此段」（仅生成段；插入视频/序章不做二采）";
    for (const m of UP_MODES) {
        const o = document.createElement("option");
        o.value = m;
        o.textContent = m;
        if (m === up.mode) o.selected = true;
        modeSel.append(o);
    }
    modeSel.onchange = () => setUpscaleField(node, "mode", modeSel.value);
    modeField.append(modeSel);
    body.append(modeField);

    if (on) {
        /* 放大模型（models/latent_upscale_models/ 目录扫描） */
        const modelField = el("div", "h3d-param");
        modelField.append(el("label", "", "放大模型"));
        const modelSel = document.createElement("select");
        modelSel.className = "h3d-select";
        modelSel.title = "神经放大权重（HuggingFace LBH-123-AI/Minimax_h3_latent_Upscaler 下载 "
            + ".pth/.safetensors 放入 models/latent_upscale_models/，刷新后在此选择）";
        const mo = document.createElement("option");
        mo.value = "";
        mo.textContent = models.length ? "（选择权重）" : "（目录为空）";
        mo.selected = !up.model;
        modelSel.append(mo);
        for (const m of models) {
            const o = document.createElement("option");
            o.value = m;
            o.textContent = m;
            if (m === up.model) o.selected = true;
            modelSel.append(o);
        }
        modelSel.onchange = () => setUpscaleField(node, "model", modelSel.value);
        modelField.append(modelSel);
        body.append(modelField);

        /* 网络架构：2D 残差骨干 / 纯 3D 卷积 */
        const archField = el("div", "h3d-param");
        archField.append(el("label", "", "网络架构"));
        const archSel = document.createElement("select");
        archSel.className = "h3d-select";
        archSel.title = "2D=残差骨干+时间卷积（快，上游默认）；3D=纯 3D 卷积（时序一致性更好）——"
            + "须与权重训练结构匹配，加载时按权重自动推断层数";
        for (const a of ["2D", "3D"]) {
            const o = document.createElement("option");
            o.value = a;
            o.textContent = a;
            if (a === up.arch) o.selected = true;
            archSel.append(o);
        }
        archSel.onchange = () => setUpscaleField(node, "arch", archSel.value);
        archField.append(archSel);
        body.append(archField);

        /* 精度 */
        const precField = el("div", "h3d-param");
        precField.append(el("label", "", "精度"));
        const precSel = document.createElement("select");
        precSel.className = "h3d-select";
        precSel.title = "放大网络推理精度：fp16 默认（参考工作流口径，省显存；放大后重采样仍在原精度）。"
            + "目标画布 >2.5MP 出现高频花屏时可回 fp32";
        for (const p of UP_PRECISIONS) {
            const o = document.createElement("option");
            o.value = p;
            o.textContent = p;
            if (p === up.precision) o.selected = true;
            precSel.append(o);
        }
        precSel.onchange = () => setUpscaleField(node, "precision", precSel.value);
        precField.append(precSel);
        body.append(precField);

        body.append(upNumField("放大倍率", up.scale, 1.0, 4.0, 0.1,
            "latent H/W 同乘（时间维不变）；目标画布见下方徽章。倍率 1.0 = 纯二采不放大",
            (v) => setUpscaleField(node, "scale", v)));
        body.append(upNumField("二采强度", up.denoise, 0.05, 1.0, 0.05,
            "尾段起始噪声 σ（sigma 尾段精化区间的起点）：0.3-0.45 常用；越大越接近重生成（会改写画面内容），越小仅轻修细节",
            (v) => setUpscaleField(node, "denoise", v)));
        body.append(upNumField("二采步数", up.steps, 1, 100, 1,
            "尾段精化步数：在 [强度σ → 0] 区间内的实际采样步数，与强度解耦（建议 3-8，参考工作流常用 3-5）",
            (v) => setUpscaleField(node, "steps", v)));
        body.append(upNumField("二采 CFG", up.cfg, 0.0, 100.0, 0.1,
            "重采样 CFG：H3 常用 1.0（官方推荐低 CFG），与主链可不同",
            (v) => setUpscaleField(node, "cfg", v)));
        body.append(upNumField("时间偏置", up.time_bias, 0.0, 0.2, 0.005,
            "尾段精化窗口内把模型看到的时间向更干净方向偏置（Detail-Daemon/T8 机制，零额外前向）："
            + "0=关（默认）；0.025-0.05 常用，过大可能过锐/伪细节。仅在 >0 时进二采指纹",
            (v) => setUpscaleField(node, "time_bias", v)));
        body.append(upNumField("细节混合", up.mix, 0.0, 1.0, 0.05,
            "频域分层混合：把精化结果的低频（结构）换回纯放大 latent、只保留精化补出的高频细节"
            + "——对冲精化带花/内容漂移，段间接缝更稳：0=关（默认，完全用精化结果）；"
            + "0.3-0.6 常用；1=结构全锁放大 latent。仅在 >0 时进二采指纹",
            (v) => setUpscaleField(node, "mix", v)));

        /* 段自适应σ（路线④）：勾选即启用——按段运动档位自动偏移精化 σ 起点 */
        const adField = el("div", "h3d-param");
        adField.append(el("label", "", "段自适应σ"));
        const adRow = el("div", "h3d-seedrow");
        const adCb = document.createElement("input");
        adCb.type = "checkbox";
        adCb.checked = up.adaptive === true;
        adCb.title = "按段运动量（latent 域逐帧相对变化）自动调精化 σ 起点：静态对话/特写 +0.05 抠脸、"
            + "高运动打斗 −0.075 防拖影鬼影、中档不动；报告行打印「自适应σ档位(运动量)」供阈值校准。"
            + "开启即进二采指纹（该段重做）；生效 σ 由该段基础 latent 决定论派生，重放不串档";
        adCb.onchange = () => setUpscaleField(node, "adaptive", adCb.checked);
        adRow.append(adCb);
        adField.append(adRow);
        body.append(adField);
        body.append(upNumField("调度偏移", up.shift, 0.0, 16.0, 0.5,
            "二采档 flow shift（T8 实证 12→6：高分辨率下调度更线性、细节合成更充分；"
            + "镜像官方 MiniMaxH3SigmaShift 的克隆补丁，主链模型零改动）：0=关（默认，沿用主链 "
            + "H3 默认 12）；6=推荐档。仅在 >0 时进二采指纹",
            (v) => setUpscaleField(node, "shift", v)));

        /* ---- 抗糊武器库（全部默认关；详见《更新说明_二采抗糊抗条纹》） ---- */
        body.append(el("div", "h3d-convbadge", "⟡ 抗糊增强（全部默认关，按需开启）"));
        body.append(upNumField("STG 引导", up.stg, 0.0, 2.0, 0.05,
            "跳块差分细节引导（T8 机制移植，GPL）：激活窗内每步多跑一次「跳掉一个 double block」"
            + "的弱前向，把完整前向多出来的细节显式放大——CFG=1.0 下也有效。0=关（默认）；"
            + "0.5-1.0 常用（每激活步约 +1 次前向 ≈ +50% 精化耗时）。仅在 >0 时进二采指纹",
            (v) => setUpscaleField(node, "stg", v)));
        body.append(upNumField("STG 跳块", up.stg_block, 0, 49, 1,
            "STG 弱前向跳过的 double block 编号（H3 共 50 个）：T8 默认 25（中段块，"
            + "细节/纹理引导最稳）；换块号可改变引导性质（前段块=构图、后段块=质感）",
            (v) => setUpscaleField(node, "stg_block", v)));
        body.append(upNumField("精化轮数", up.passes, 1, 3, 1,
            "多轮递降精化：每轮以更小 σ 在上一轮输出上再精化（先修结构、再抠细节）。"
            + "1=单轮（默认，现行为）；2-3=递进（耗时×轮数）。仅在 >1 时进二采指纹",
            (v) => setUpscaleField(node, "passes", v)));
        body.append(upNumField("σ 衰减", up.decay, 0.2, 0.8, 0.05,
            "多轮递降的每轮 σ 缩放系数：第 k 轮 σ=σ₀·衰减^k（默认 0.5 → 0.35/0.18/0.09）。"
            + "仅精化轮数 >1 时生效",
            (v) => setUpscaleField(node, "decay", v)));
        body.append(upNumField("latent 锐化", up.sharpen, 0.0, 1.0, 0.05,
            "latent 域 unsharp 锐化（精化输出高频再放大一档，CPU 零显存零前向）："
            + "0=关（默认）；0.2-0.4 常用；过大可能放大噪声/伪影。仅在 >0 时进二采指纹",
            (v) => setUpscaleField(node, "sharpen", v)));
        body.append(upNumField("像素锐化", up.pixel_sharpen, 0.0, 1.0, 0.05,
            "解码后逐帧 unsharp 锐化（连 VAE 解码的软化一起补偿，编码前生效）："
            + "0=关（默认）；0.2-0.4 常用。与 latent 锐化正交可叠加。仅在 >0 时进二采指纹",
            (v) => setUpscaleField(node, "pixel_sharpen", v)));

        /* 编码档位（抗条纹主手段） */
        const encField = el("div", "h3d-param");
        encField.append(el("label", "", "编码档位"));
        const encSel = document.createElement("select");
        encSel.className = "h3d-select";
        encSel.title = "二采分段及最终高清成片的 mp4 编码质量（基础链不变）："
            + "标准=crf20 veryfast（现状兼容）；高清=crf16 medium + 暗部自适应量化 + Bayer 抖动；"
            + "极致=crf13 slow + 同上。8bit 渐变色带（暗部/天空横向条纹）靠抖动消解，"
            + "编码层二次模糊靠 crf/preset 消解——极致档编码明显变慢。非标准档进二采指纹";
        for (const e of UP_ENCODES) {
            const o = document.createElement("option");
            o.value = e;
            o.textContent = e;
            if (e === up.encode) o.selected = true;
            encSel.append(o);
        }
        encSel.onchange = () => setUpscaleField(node, "encode", encSel.value);
        encField.append(encSel);
        body.append(encField);

        /* 独立采样器/调度器（空 = 沿用主链） */
        const upTextField = (label, value, tip, commit) => {
            const field = el("div", "h3d-param");
            field.append(el("label", "", label));
            const row = el("div", "h3d-seedrow");
            const inp = document.createElement("input");
            inp.type = "text";
            inp.value = value;
            inp.placeholder = "沿用主链";
            inp.title = tip;
            inp.onchange = () => commit(inp.value.trim());
            row.append(inp);
            field.append(row);
            return field;
        };
        body.append(upTextField("采样器", up.sampler,
            "二采独立采样器（空=沿用主链；名字须为 ComfyUI 注册名，如 euler / res_multistep，"
            + "写错自动回退主链并报告）。非空进二采指纹",
            (v) => setUpscaleField(node, "sampler", v)));
        body.append(upTextField("调度器", up.scheduler,
            "二采独立调度器（空=沿用主链；如 simple / beta / ddim_uniform，"
            + "写错自动回退主链并报告）。非空进二采指纹",
            (v) => setUpscaleField(node, "scheduler", v)));

        /* 增益自适应重试 */
        const rtField = el("div", "h3d-param");
        rtField.append(el("label", "", "增益重试"));
        const rtRow = el("div", "h3d-seedrow");
        const rtCb = document.createElement("input");
        rtCb.type = "checkbox";
        rtCb.checked = up.retry === true;
        rtCb.title = "精化后细节增益未达目标时，自动以 σ+0.1 重跑整条精化链一次、"
            + "按增益取优（耗时最多翻倍，只在确实不达标时发生）。开启即进二采指纹";
        rtCb.onchange = () => setUpscaleField(node, "retry", rtCb.checked);
        rtRow.append(rtCb);
        rtField.append(rtRow);
        body.append(rtField);
        body.append(upNumField("重试目标", up.retry_target, 0.05, 1.0, 0.05,
            "细节增益目标（+15%=0.15 健康线下限）：增益低于此值才触发重试。"
            + "仅增益重试开启时生效",
            (v) => setUpscaleField(node, "retry_target", v)));

        /* 目标画布徽章（latent 偶数对齐 = 像素 32 倍数，与后端 target_hw 同口径） */
        const target = upTargetCanvas(node, up.scale);
        if (target) {
            const b = el("div", "h3d-convbadge",
                `目标画布 ${escapeHtml(target)}（latent 偶数对齐 · 时间维不变）`);
            b.title = "基础画布 × 倍率后按 latent 偶数（=像素 32 倍数）对齐；"
                + "超过 2.5MP 时后端会警告显存压力";
            body.append(b);
        }
        if (up.scale > 2.0) {
            body.append(el("div", "h3d-upwarn",
                `注意：倍率 ${up.scale}× 目标画布大，二采显存/耗时显著增加，建议先小倍率试一段`));
        }
    }
    det.append(body);

    /* 分区脚注：模式说明 + 当前进度 */
    const footBits = [];
    if (!on) {
        footBits.push("关闭中：主链照常，不做放大重采样（已产出的高清分段/成片不受影响）");
    } else {
        footBits.push("跟随生成：每段采样定稿后立即二采（新增/失效段自动重做）；逐段审片=生成一段二采一段。"
            + "单段重做用段卡片「🔄 重新二采此段」；插入视频/序章为外部成品素材不二采，成片拼接时缩放对齐");
    }
    if (done > 0) {
        footBits.push(`已二采 ${upDone}/${done} 段`
            + (upFinals.length ? ` · 高清成片×${upFinals.length}` : ""));
    }
    footBits.push("产物在项目文件夹：seg_*.mp4 即高清分段（同名覆盖基础段）/ final_*.mp4 高清成片；音轨沿用原声");
    if (on && !up.model && models.length) footBits.push("⚠ 未选择放大模型：运行时会报错提示");
    det.insertAdjacentHTML("beforeend",
        `<div class="h3d-foot">${footBits.map(escapeHtml).join("<br>")}</div>`);

    sec.append(det);
}

/* ---- 右栏 ---- */

function renderHistoryZone(sec, data) {
    const { state, mf } = data;
    sec.replaceChildren();
    sec.append(el("div", "h3d-sechead",
        "<strong>成片</strong><small>项目文件夹内的成片（manifest.finals）</small>"));
    const box = el("div", "h3d-hist");

    /* 第一区块：项目文件夹内的成片（manifest.finals，最新在前） */
    const dir = state?.dir;
    const finals = (mf?.finals || []).filter(Boolean).slice(-10).reverse();
    if (dir && finals.length) {
        const head = el("div", "h3d-hist-head", `项目成片（output/h3_projects/${escapeHtml(dir)}/）`);
        box.append(head);
        finals.forEach((file, i) => {
            const sub = `h3_projects/${dir}`;
            const url = viewUrl(sub, file);
            const card = el("div", "h3d-result" + (i === 0 ? " current" : ""));
            const v = document.createElement("video");
            v.controls = true;
            v.preload = "metadata";
            v.src = url;
            card.append(v, el("div", "h3d-result-name", (i === 0 ? "最新成片 · " : "") + escapeHtml(file)));
            const meta = el("div", "h3d-result-meta");
            const acts = el("div", "h3d-result-acts");
            const open = el("a", "h3d-dl", "↗ 打开");
            open.href = url; open.target = "_blank";
            const dl = el("a", "h3d-dl", "⬇ 下载");
            dl.href = url; dl.download = file;
            acts.append(open, dl);
            const del = el("button", "h3d-btn h3d-btn-danger", "🗑 删除");
            del.title = "删除项目内该成片文件（二次确认，项目其余内容不动）";
            del.onclick = async () => {
                if (del.dataset.armed !== "1") {
                    del.dataset.armed = "1";
                    del.textContent = "确认删除？";
                    setTimeout(() => { del.dataset.armed = ""; del.textContent = "🗑 删除"; }, 2500);
                    return;
                }
                try {
                    const r = await api.fetchApi("/h3chain/delete_file", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ path: `h3_projects/${dir}/${file}` }),
                    });
                    if (r.ok) { refresh(); return; }
                    if (r.status === 404 || r.status === 405) {
                        setApiError(`删除接口未注册（HTTP ${r.status}）：请重启 ComfyUI 并检查控制台「路由已注册」日志。`);
                        return;
                    }
                    const j = await r.json().catch(() => ({}));
                    alert(`删除失败：${j.error || `HTTP ${r.status}`}`);
                } catch (e) {
                    alert("删除请求失败：" + e);
                }
            };
            acts.append(del);
            meta.append(acts);
            card.append(meta);
            box.append(card);
        });
    }

    /* 第二区块：合并片段（merged_*.mp4，manifest.merges，最新在前） */
    const dirM = state?.dir;
    const merges = (mf?.merges || []).filter((m) => m && m.file).slice(-10).reverse();
    if (dirM && merges.length) {
        box.append(el("div", "h3d-hist-head", "合并片段（按序拼接导出）"));
        merges.forEach((m) => {
            const file = m.file;
            const sub = `h3_projects/${dirM}`;
            const url = viewUrl(sub, file);
            const card = el("div", "h3d-result");
            const v = document.createElement("video");
            v.controls = true;
            v.preload = "metadata";
            v.src = url;
            v.onerror = () => { card.classList.add("gone"); };
            const segList = (m.items || []).map((it) =>
                it?.seg != null ? `段${it.seg}` : it?.file || "?").join("＋");
            card.append(v,
                el("div", "h3d-result-name", "合并 · " + escapeHtml(file)),
                el("div", "h3d-result-info",
                    escapeHtml(`${fmtTime(m.updated_at)} · 来源：${segList || "—"}`)));
            const meta = el("div", "h3d-result-meta");
            const acts = el("div", "h3d-result-acts");
            const open = el("a", "h3d-dl", "↗ 打开");
            open.href = url; open.target = "_blank";
            const dl = el("a", "h3d-dl", "⬇ 下载");
            dl.href = url; dl.download = file;
            const del = el("button", "h3d-btn h3d-btn-danger", "🗑 删除");
            del.title = "删除该合并片段文件（二次确认；manifest 里的记录会随之失效）";
            del.onclick = async () => {
                if (del.dataset.armed !== "1") {
                    del.dataset.armed = "1";
                    del.textContent = "确认删除？";
                    setTimeout(() => { del.dataset.armed = ""; del.textContent = "🗑 删除"; }, 2500);
                    return;
                }
                try {
                    const r = await api.fetchApi("/h3chain/delete_file", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ path: `h3_projects/${dirM}/${file}` }),
                    });
                    if (r.ok) { refresh(); return; }
                    if (r.status === 404 || r.status === 405) {
                        setApiError(`删除接口未注册（HTTP ${r.status}）：请重启 ComfyUI 并检查控制台「路由已注册」日志。`);
                        return;
                    }
                    const j = await r.json().catch(() => ({}));
                    alert(`删除失败：${j.error || `HTTP ${r.status}`}`);
                } catch (e) {
                    alert("删除请求失败：" + e);
                }
            };
            acts.append(open, dl, del);
            meta.append(acts);
            card.append(meta);
            box.append(card);
        });
    }

    sec.append(box);
    if (state?.dir) {
        sec.append(el("div", "h3d-foot", `项目文件夹：output/h3_projects/${escapeHtml(state.dir)}（分段视频也在其中）`));
    }
}

/* ---- 页脚 ---- */

function renderFooter(z, data) {
    const { node, state, mf, plan } = data;
    const total = plan ? plan.length : 0;
    const done = mf?.done ?? state?.done ?? 0;

    const infos = [];
    infos.push(`当前 <b>${escapeHtml(state?.dir || (node ? getDirValue(node) || "未命名" : "无"))}</b>`);
    if (node) {
        const ps = paramsSummary(node, mf);
        const totalSec = total ? `共${chainSeconds(node, data.ds, plan).toFixed(1)}s` : "";
        const geoInfo = [ps.geo, totalSec].filter(Boolean).join(" · ");
        if (geoInfo) infos.push(`<b>${escapeHtml(geoInfo)}</b>`);
    }
    if (state?.review) infos.push("逐段审片：每次排队只生成下一段");
    if (!node) infos.push("只读模式：画布上未找到 H3 Seamless Chain 节点");
    if (mergeSel.on) infos.push("合并模式进行中：生成按钮已暂停（退出合并模式后恢复）");
    z.footInfo.innerHTML = infos.map((s) => `<span>${s}</span>`).join("");

    const run = z.run;
    run.onclick = queuePrompt;
    const pend = redoPending(data), queued = redoQueued(data);
    if (mergeSel.on) {
        run.disabled = true;
        run.textContent = "⧉ 合并模式进行中（退出后可生成）";
    } else if (!total) {
        run.disabled = true;
        run.textContent = "▶ 开始生成（先配置段落）";
    } else if (pend && node) {
        /* 重摇标记优先（最新意图）：提交=随机种子+逐段审片推进；
           队列残留段后端自动合并进本次执行 */
        run.disabled = false;
        run.textContent = `🎲 重摇已标记 ${pend} 段`;
        run.onclick = submitRedo;
    } else if (queued && node) {
        /* 已提交队列未消费完（审片逐段推进每运行一段停一次）：普通提交续跑，
           后端先消费队列再继续剩余段（种子逐段自动 bump，控件种子不动） */
        run.disabled = false;
        run.textContent = `🎲 继续重摇剩余 ${queued} 段`;
        run.onclick = queuePrompt;
    } else if (done >= total) {
        /* 链已完成：单段二采走段卡片「🔄 重新二采此段」，生成按钮不再提供批量入口 */
        run.disabled = true;
        run.textContent = `✓ 本链已完成（${total} 段）`;
    } else {
        run.disabled = false;
        run.textContent = done > 0
            ? `▶ 继续下一段（段 ${done + 1}）`
            : `▶ 开始生成（共 ${total} 段）`;
    }
}

/* ---- 新建项目模态 ---- */

function openNewProjectModal() {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点（只读模式）"); return; }
    if (document.querySelector(".h3d-overlay")) return;

    const t = new Date();
    const pad = (x) => String(x).padStart(2, "0");
    const def = `h3chain_${t.getFullYear()}${pad(t.getMonth() + 1)}${pad(t.getDate())}_${pad(t.getHours())}${pad(t.getMinutes())}`;

    const overlay = el("div", "h3d-overlay");
    const dialog = el("div", "h3d-dialog");
    dialog.innerHTML = `
        <h3>＋ 新建项目</h3>
        <p class="h3d-lead">新开一条视频链：换存档目录名即换链，旧链原样保留可随时切回。
        点「创建项目」会在 output/h3_projects/ 下立即建好文件夹（游戏存档槽：创建即可见，0 段起跑）。
        提示词沿用当前内容作底稿（可勾选下方清空）。</p>`;
    const input = document.createElement("input");
    input.type = "text";
    input.value = def;
    input.spellcheck = false;
    const err = el("div", "h3d-err", "");
    const check = el("label", "h3d-check");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    check.append(cb, document.createTextNode("创建后清空全部提示词（否则沿用当前底稿）"));
    const row = el("div", "h3d-dialog-row");
    const cancel = el("button", "h3d-btn", "取消");
    const ok = el("button", "h3d-btn h3d-btn-cta", "创建项目");
    row.append(cancel, ok);
    dialog.append(input, err, check, row);
    overlay.append(dialog);
    overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.append(overlay);
    input.focus();
    input.select();

    const close = () => overlay.remove();
    cancel.onclick = close;
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    const submit = async () => {
        const name = input.value.trim();
        if (!name) { err.textContent = "项目名不能为空"; return; }
        if (!/^[0-9A-Za-z_\-一-龥]+$/.test(name)) {
            err.textContent = "仅允许中文、字母、数字、下划线、连字符";
            return;
        }
        /* 立即落盘建项目文件夹（游戏存档槽语义：创建即可见）。
           接口 404/405（未注册）时降级为旧的惰性行为——首次运行仍会建目录，不阻断。 */
        let diskOk = false;
        try {
            const r = await api.fetchApi("/h3chain/create_project", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ dir: name }),
            });
            if (r.ok) {
                diskOk = true;
            } else if (r.status === 400) {
                const j = await r.json().catch(() => ({}));
                err.textContent = `创建失败：${j.error || "项目名非法"}`;
                return;
            } else if (r.status === 404 || r.status === 405) {
                setApiError(`项目接口未注册（HTTP ${r.status}）：请重启 ComfyUI 并检查控制台「路由已注册」日志。`
                    + `本次新建未落盘，首次运行时会自动建目录。`);
            } else {
                const j = await r.json().catch(() => ({}));
                err.textContent = `创建失败：${j.error || `HTTP ${r.status}`}`;
                return;
            }
        } catch (e) {
            console.warn("[h3-director] create_project failed:", e);
        }
        clearTimeout(promptFlushTimer);       // 同上：取消挂起防抖，旧项目由下方显式回写
        await flushPrompts(node);   // 旧项目底稿先落盘，再切走
        if (!setDirValue(node, name)) { err.textContent = "节点上没有「存档目录/断点目录」控件"; return; }
        setWidgetValue(node, W_REROLL, 0);
        if (cb.checked) clearPrompts(node);
        if (diskOk) flushPrompts(node, name);   // 沿用底稿时把携带的提示词存进新项目
        setLed("idle", `新项目「${name}」已就绪${diskOk ? "（文件夹已建）" : ""}`);
        close();
        scheduleRefresh(200);
    };
    ok.onclick = submit;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
}

/* ---- 总提示词模态：多段一次性粘贴分配（格式与 skill「h3 总提示词」同规范） ---- */

const MP_PLACEHOLDER = `【段1】
场景：风格+环境+光照，如：日系动画风，黄昏教室，暖橘侧光
角色：外观+服饰+位置，如：蓝白校服的短发少女，坐在窗边倒数第二排
环境音：翻书声，远处操场喧闹
配乐：钢琴独奏，慢板
提示词：镜头缓慢推近。少女抬头望向窗外，轻声说「放学后见。」

【段2】
场景：同上（保持世界观一致）
提示词：校门口逆光剪影，两人并肩走出，镜头拉远。`;

function openMasterPromptModal() {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点（只读模式）"); return; }
    if (document.querySelector(".h3d-overlay")) return;

    const ds = getDs(node);
    const existing = (ds.prompts || []).some((x) => String(x ?? "").trim());
    const overlay = el("div", "h3d-overlay");
    const dialog = el("div", "h3d-dialog h3d-dialog-wide");
    dialog.innerHTML = `
        <h3>📋 总提示词 · 多段一次性分配</h3>
        <p class="h3d-lead">按「段头 + 八标签」格式粘贴全文（可用 AI 按 skill「h3-video-prompts」生成），
        一次分给所有段：<b>场景 / 角色 / 环境音 / 配乐 / 时长 / 独立镜头 / 参考 / 提示词</b>。
        标签<b>写了即生效（写空=清空），没写的字段不动</b>；段数按段头数量重排，
        插入视频段与各段的素材勾选/断链标记保留；「参考」只认「素材与参考」已有标签，
        未上传的会剔除并提示；<b>【完】</b>之后的内容（如 AI 的素材建议）不参与解析。
        下方可先把现有段落导出改写再贴回。</p>`;
    const ta = document.createElement("textarea");
    ta.className = "h3d-mpta";
    ta.spellcheck = false;
    ta.placeholder = MP_PLACEHOLDER;
    if (existing) ta.value = exportMasterPrompt(node);
    const info = el("div", "h3d-mpinfo", "");
    const err = el("div", "h3d-err", "");
    const row = el("div", "h3d-dialog-row");
    const exp = el("button", "h3d-btn", "从当前段落导出");
    const cancel = el("button", "h3d-btn", "取消");
    const ok = el("button", "h3d-btn h3d-btn-cta", "解析并分配");
    row.append(exp, cancel, ok);
    dialog.append(ta, info, err, row);
    overlay.append(dialog);
    overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") overlay.remove(); });
    document.body.append(overlay);
    ta.focus();

    const preview = () => {
        err.textContent = "";
        const p = parseMasterPrompt(ta.value);
        if (!ta.value.trim()) { info.textContent = ""; return; }
        const cnt = (k) => p.segs.filter((s) => s[k] !== undefined && s[k] !== "").length;
        const bits = [`识别 <b>${p.segs.length}</b> 段`,
            `场景 ${cnt("scene")} · 角色 ${cnt("character")} · 环境音 ${cnt("soundscape")}`,
            `配乐 ${cnt("music")} · 时长 ${p.segs.filter((s) => s.seconds !== undefined).length} · 主体 ${cnt("main")}`,
            `独立镜头 ${p.segs.filter((s) => s.unlink === true).length} · 参考 ${p.segs.filter((s) => (s.refs || []).length > 0).length}`];
        info.innerHTML = bits.join("　");
        if (p.notes.length) err.textContent = "⚠ " + p.notes.join("；");
    };
    ta.addEventListener("input", preview);
    preview();

    exp.onclick = () => { ta.value = exportMasterPrompt(node); preview(); };
    cancel.onclick = () => overlay.remove();
    const submit = () => {
        if (!ta.value.trim()) { err.textContent = "内容为空"; return; }
        const p = applyMasterPrompt(node, ta.value);
        if (!p.segs.length) { err.textContent = "未识别到任何段落"; return; }
        flushPrompts(node);                       // 同步项目 manifest 底稿
        setLed("idle", `总提示词已分配到 ${p.segs.length} 段`);
        overlay.remove();
        scheduleRefresh(60);
    };
    ok.onclick = submit;
}

/* ---------- 刷新 ---------- */

async function refresh() {
    try {
        const data = await collectData();
        renderMini(data);
        if (desk) updateDesk(data);
    } catch (e) {
        console.warn("[h3-director] refresh failed:", e);
        renderMiniFallback(e);   // 出错也保住入口按钮，不留空白标签
    }
}

/* ---------- 挂载 ---------- */

const FAB_ICON = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5v14M17 5v14M3 9h4M3 15h4M17 9h4M17 15h4"/></svg>`;

function mountSidebar(target) {
    miniBox = target;
    target.setAttribute("translate", "no");   // 迷你卡同样免疫浏览器机翻（容器为本扩展专属）
    /* 先渲染占位卡再刷新：刷新未返回或失败时标签点开也不空白 */
    renderMini({ state: {}, mf: null, plan: null, node: null });
    refresh();
}

let fabEl = null;

function mountFallbackFab() {
    if (fabEl) return;
    const btn = document.createElement("button");
    btn.className = "h3d-fab";
    btn.setAttribute("translate", "no");
    btn.title = "长片导演台（H3 Seamless Chain）";
    btn.innerHTML = FAB_ICON;
    btn.onclick = openDesk;
    document.body.append(btn);
    fabEl = btn;
}

/* 侧栏标签内容首次成功渲染后撤下悬浮球（标签不可用时悬浮球常驻保底入口） */
function removeFab() {
    if (fabEl) { fabEl.remove(); fabEl = null; }
}

app.registerExtension({
    name: "H3SeamlessChain.DirectorDesk",
    /* 画布节点就绪即刷新：面板首刷可能早于工作流载入（节点未就绪 → 各区渲染
     * 「画布上未找到节点」），之后没有任何事件再触发刷新——这里在节点载入/
     * 新建时主动补一刷，mini 卡与实验/参数/二采区随之恢复可用 */
    loadedGraphNode(node) {
        if (node && node.type === NODE_TYPE) scheduleRefresh(250);
    },
    nodeCreated(node) {
        if (node && node.type === NODE_TYPE) scheduleRefresh(250);
    },
    setup() {
        console.log("[h3-director] loaded", H3D_VER);
        injectStyles();

        /* 旧工作流迁移：widget 参数官方化后按位错位，载入画布前重排 widgets_values */
        if (typeof app.loadGraphData === "function") {
            const origLoadGraphData = app.loadGraphData.bind(app);
            app.loadGraphData = function (data, ...args) {
                return origLoadGraphData(migrateGraphWidgets(data), ...args);
            };
        }

        if (app.extensionManager && typeof app.extensionManager.registerSidebarTab === "function") {
            app.extensionManager.registerSidebarTab({
                id: "h3chain-director",
                // 图标必须是已加载图标库的类名（前端内置 PrimeVue），传 SVG 源码不会渲染
                icon: "pi pi-video",
                title: "长片导演台",
                tooltip: "H3 Seamless Chain：项目管理 + 段落流水线 + 成片历史（继续下一段 / 重摇 / 改词 / 插入视频）",
                type: "custom",
                render: (elTarget) => mountSidebar(elTarget),
            });
        }
        /* 悬浮球常驻保底：新版前端存在「标签注册成功但从不调用 render」的情况
         * （AutoDL ComfyUI 0.33.0 / 前端 1.49.6 实测，标签点开空白）——悬浮球
         * 不依赖侧栏机制，任何前端都有入口；标签内容首次渲染成功后自动撤下。 */
        mountFallbackFab();

        api.addEventListener("executing", ({ detail }) => {
            if (detail === null) { scheduleRefresh(); return; } // 队列清空
            const n = (app.graph?._nodes || []).find((x) => String(x.id) === String(detail));
            if (n && n.type === NODE_TYPE) {
                setLed("running", "生成中");
            }
        });
        api.addEventListener("execution_success", () => {
            if (pendingReset) {
                const node = findNode();
                if (node) setWidgetValue(node, W_REROLL, 0);
                pendingReset = false;
            }
            setLed("done", "生成完成");
            scheduleRefresh();
        });
        api.addEventListener("execution_error", () => {
            setLed("error", "运行出错");
            scheduleRefresh();
        });
    },
});
