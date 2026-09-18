/**
 * H3 Seamless Chain —— 「长片导演台」（全屏一体化控制台 + 侧栏迷你入口）
 *
 * 参照「H3 一体化总控导演台」状态驱动架构：所有输入（模式/提示词/首帧/参考素材）
 * 存入节点「导演台状态」JSON widget，节点 execute 读取 JSON 加载素材；
 * 画布节点只作镜像/兜底（配套工作流的常驻隐藏节点），不在画布上动态建线。
 *
 * 状态 JSON schema v2（存入「导演台状态」widget，无手动模式——链路由后端按实际引用自动推导）：
 *   { mode:"文生视频"|"首帧视频"|"多参视频",  // 旧字段保留兼容，后端不再读取
 *     prompts:["段1文本","段2文本",...],
 *     first_frame:"input目录下的文件名",
 *     end_frame:"input目录下的文件名（FL2VA 剧情终点，仅首帧模式）",
 *     ref_assets:[ { file:"文件名", kind:"image"|"video"|"audio", label:"角色1", roles:["首帧图"] }, ... ],
 *                                // 标签素材池 = 资产库（真源，三类混排，总量不限；
 *                                //  官方单段上限 图9/视3/音3 只在段引用时卡，后端按段按需加载）
 *     ref_images:["文件名1",...], // 兼容保留 = ref_assets 中图片类文件的序列
 *     segments:[
 *       { scene_prompt:"场景描述", character_prompt:"角色描述",
 *         seconds:6.5,        // 本段时长（秒），null=跟随节点「每段时长」默认
 *         refs:["角色1","场景1"] },  // 本段引用的素材标签（跨类别），[]/缺省=只用文本@标签出现的
 *       ...
 *     ] }
 *
 * 标签引用：提示词写 @角色1，后端按段按类别压实重编号（图→<Picture k>、视→<Video k>、
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
const H3D_VER = "20260914+lib-dup-gate";
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
/* 引用语模板库：插入到提示词光标处，@标签 由后端按段压实为 <Picture k> */
/* 引用语 = **锚定方式**：先选一种，再点参考素材，句式按该方式写进正文。
 * 每条同时带官方 `retention_analysis` 保留标记与 `subject_definitions` 角色句
 * （具象化段会一并写进 v2 结构，编译时进官方字段；主提示词段只用句式文本）。
 * 官方标记：fully_preserved / partially_preserved / weak_reference（见
 * prompt/minimaxh3_official_ref2v_prompt_writing.txt 第 4 节）。 */
const REF_TEMPLATES = [
    ["裸引用（不加描述）", (l) => `@${l}`, "partially_preserved", ""],
    ["主角出场", (l) => `主角 @${l} 全程出镜，主体身份、外观与服饰全程保持一致`,
        "fully_preserved",
        "the main subject; keep identity, facial features and outfit consistent across every shot"],
    ["配角出场", (l) => `画面中出现的 @${l} 为次要角色，身份与外观保持一致`,
        "fully_preserved",
        "a supporting character; keep identity and appearance consistent"],
    ["场景还原", (l) => `场景以 @${l} 为准，延续其环境、光照与空间布局`,
        "partially_preserved",
        "the environment reference; keep layout, lighting and spatial arrangement"],
    ["风格参考", (l) => `整体画风、色调与质感参考 @${l}`,
        "weak_reference",
        "a style reference; keep overall palette, texture and rendering feel"],
    ["镜头参考", (l) => `运镜方式参考 @${l}（可用官方词汇：Push In / Pan Left / Truck Right / Tracking Shot，加 with small amplitude at slow speed 等修饰）`,
        "weak_reference",
        "a camera-movement reference; keep motion direction and pacing only"],
    ["音频参考", (l) => `声音以 @${l} 为准（音色、节奏与情绪延续）`,
        "weak_reference",
        "an audio reference; keep timbre, rhythm and mood"],
    ["说话人", () => `短发女主 (S1) 轻声说：「……」`, "", ""],
];
/** 每段的锚定方式选择（seg idx → REF_TEMPLATES 下标），重绘不丢。
 *  默认 0 = 裸引用：**没有单独的「无」档**——「无」与「裸引用（不加描述）」插入的
 *  正文完全一样（都是裸 @标签），两个选项只会让人以为有区别。 */
const _refTpl = new Map();
const REF_TPL_DEFAULT = 0;

/* 卡片轻量重绘钩子（每次 buildCards 重置，当前渲染出的段卡各自注册一个）：
 * 焦点守卫会跳过整卡重建，若此时什么都不做，卡片内的即时状态（引用 chips / 计数 /
 * 正文绿框）就停在旧值上——表现就是"要再点一下别的才更新"。刷新周期改为调用这些
 * 钩子，把卡片内的活状态推进到最新，重活（整卡重建）仍等焦点离开。 */
let _cardPainters = [];
function registerCardPainter(fn) { if (typeof fn === "function") _cardPainters.push(fn); }
const MODES = [
    ["文生视频", "文生", "纯文本，fl2va UNET，不接图片"],
    ["首帧视频", "首帧", "首帧起手（可选尾帧图片=FL2VA 首尾帧），fl2va UNET"],
    ["多参视频", "多参", "参考图/视频/音频，ref2va UNET，@标签 引用"],
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
/* 合并模式（纯内存勾选态，不落 ds / 不触发重做）：勾选已完成段
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

/** 可折叠编辑区（主提示词框的三段式外观，总提示词工作台段卡复用同一套）。
 *  返回 { g: <details>, body: 内容容器 }。 */
function h3dPane(title, open) {
    const g = el("details", "h3d-v2group h3d-ppane");
    g.open = !!open;
    g.innerHTML = `<summary>${escapeHtml(title)}</summary>`;
    const b = el("div", "h3d-v2grid");
    g.append(b);
    return { g, body: b };
}

function viewUrl(subfolder, filename) {
    // 四库兼容：filename 含 "finals/seg_000.mp4" 前缀时拆进 subfolder，
    // 使 /api/view 的 subfolder/filename 口径与 Comfy 原生一致（旧裸名直通）。
    let sub = String(subfolder || "");
    let file = String(filename || "");
    if (file.includes("/")) {
        const parts = file.split("/").filter(Boolean);
        file = parts.pop() || "";
        if (parts.length) sub = sub ? `${sub}/${parts.join("/")}` : parts.join("/");
    }
    return `/api/view?type=output&subfolder=${encodeURIComponent(sub)}&filename=${encodeURIComponent(file)}`;
}

/** 带超时的 fetch：后端一旦被卡死（历史上确有其事——标签唯一化死循环把 aiohttp
 *  事件循环占死），普通 fetch 会永远挂着，调用方（hydratePool 等）跟着一起停摆，
 *  界面呈现为"整台没反应"。超时后按失败处理，至少让界面继续走、报错可见。 */
function fetchWithTimeout(url, ms = 12000) {
    if (typeof AbortSignal === "function" && typeof AbortSignal.timeout === "function") {
        return fetch(url, { signal: AbortSignal.timeout(ms) });
    }
    const ac = typeof AbortController === "function" ? new AbortController() : null;
    if (!ac) return fetch(url);
    const t = setTimeout(() => { try { ac.abort(); } catch (e) { /* 忽略 */ } }, ms);
    return fetch(url, { signal: ac.signal }).finally(() => clearTimeout(t));
}

async function fetchJson(subfolder, filename) {
    try {
        const r = await fetchWithTimeout(viewUrl(subfolder, filename), 12000);
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

/** 本段总帧数（像素帧）：本段设了秒数就用它，否则跟随节点「每段时长」。
 *
 *  与后端 nodes._snap_seconds 同公式（秒×24 → **就近**吸附 17k+5，不是向上对齐）。
 *  段卡标题那个「≈N帧」和锚定面板的目标轨刻度都取这一个函数——这两处曾经各算一遍，
 *  结果一边向上对齐一边就近，用户看到目标轨的总帧数跟分段时长对不上。
 *  改公式只许改这里（连线另一头是 nodes._snap_seconds，两边必须同时改）。 */
function segmentFrames(node, seg) {
    const defRaw = Number(getWidgetValue(node, W_DUR));
    const defSec = isFinite(defRaw) && defRaw > 0 ? defRaw : 5.0;
    const secs = Number(seg && seg.seconds);
    return snapFrames((isFinite(secs) && secs > 0) ? secs : defSec);
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

/* 别名归一（前端侧）。**权威规则在后端 asset_store.clean_alias，两边必须同规则**：
 * 后端 `nodes._REF_AT` 在空白处就断句、连 `.` `()` 都不认，而别名一律来自文件名
 * （空格/括号是常态）—— 别名里留着这些字符，就是"提示词里看着有引用、编译时说
 * 找不到标签"。前端 `refTokens` 靠池内最长匹配能穿过去，所以问题只在后端暴露，
 * 更容易被当成偶发。
 *
 * 规则：剥媒体扩展名（白名单，`v1.0` 不动）→ 非法字符压成 `_` → 折叠连续 `_-`
 * → 截 24（与后端 ALIAS_MAX 对齐；旧 12 会在 widget 读写循环里截断长别名，
 * 导致服务端别名与段引用对不上）。 */
const _ALIAS_EXT_RE = /\.(png|jpg|jpeg|gif|webp|bmp|tiff|tif|heic|avif|mp4|mov|webm|mkv|avi|wmv|flv|m4v|wav|mp3|ogg|flac|m4a|aac|opus)$/i;
function cleanLabel(text) {
    const base = String(text ?? "").trim().replace(/\\/g, "/").split("/").pop() || "";
    const stem = base.replace(_ALIAS_EXT_RE, "");
    return (stem || base)
        .replace(/[^\p{L}\p{N}_-]+/gu, "_")
        .replace(/[_-]{2,}/g, "_")
        .replace(/^[_-]+|[_-]+$/g, "")
        .slice(0, 24)
        .replace(/[_-]+$/, "");
}

/** 纯展示用：把标签里夹带的「(.png|.jpg|.mp4|.wav|.webp…)」格式后缀去掉。
 *  别名落库时本来就只截 stem（store_to_project / move_media 等都会去后缀），
 *  但偶尔老 manifest 里有从旧版保留下来的「标签.扩展」式数据；Electron 桌面端
 *  把原文件名拖进上传框时，偶尔会把扩展名带进来。这一层只在**显示**剥掉，原
 *  文不动，避免改坏后端的别名命名空间（lookup 仍按原 alias 走）。
 *
 *  白名单常见音视/图像格式扩展（不区分大小写）：图片 png/jpg/jpeg/gif/webp/bmp/
 *  tiff/heic；视频 mp4/mov/webm/mkv/avi/wmv/flv/m4v；音频 wav/mp3/ogg/flac/
 *  m4a/aac/opus。其它（如「v1.0」「1.0」「2.5」）一律不动。 */
const _CHIP_EXT_RE = /\.(png|jpg|jpeg|gif|webp|bmp|tiff|heic|mp4|mov|webm|mkv|avi|wmv|flv|m4v|wav|mp3|ogg|flac|m4a|aac|opus)$/i;
function chipLabelText(s) {
    const t = String(s == null ? "" : s).trim();
    if (!t) return "";
    return t.replace(_CHIP_EXT_RE, "").trim() || t;
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
    if (typeof ta.insertText === "function") return ta.insertText(text);   // 富文本编辑器
    const s = ta.selectionStart ?? ta.value.length;
    const e = ta.selectionEnd ?? s;
    ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
    const pos = s + text.length;
    ta.focus();
    ta.setSelectionRange(pos, pos);
    return ta.value;
}

/* ---------- 富文本提示词编辑器（@别名 → 内联绿框） ----------
 * 需求：提示词框里被引用的素材要像"标签"一样显示成绿框（点 ✕ 即取消该素材在本段的
 * 全部引用），而不是一段裸文本 —— 裸文本看不见、也删不干净（「取消参考清不完」）。
 *
 * 设计要点：
 * - 正文（序列化后的纯文本）是**唯一真相**：seg.refs 由正文里的 @别名 出现序列同步
 *   （见 syncRefsFromText），所以 ✕/手删/手打都能对上，不会再出现"清不完"。
 * - 对外暴露 textarea 兼容接口（value / focus / addEventListener / selectionStart /
 *   setSelectionRange / dataset / placeholder…），@补全、AI 优化、编译预览等既有代码
 *   零改动即可复用。
 * - 绿框是 contenteditable=false 的原子节点：光标进不去，退格整块删。
 */
function createPromptEditor(opts) {
    const o = opts || {};
    const box = document.createElement("div");
    box.className = "h3d-ta h3d-rta";
    box.contentEditable = "true";
    box.spellcheck = false;
    const labelsOf = () => (typeof o.labels === "function" ? o.labels() : (o.labels || []))
        .map(String).filter(Boolean);
    /* 别名 -> 素材信息 {kind, file, asset_id}：绿框要按它挑缩略图/图标。
     * 没有就给空对象，makeTag 兜底为 image。pool 改变时这块也要保持"活的"
     * ——同样读 getDs(node).ref_assets，不能用建卡那一刻的快照。
     * （早期版本只传 kind，够挑图标但不够拼缩略图地址。） */
    const infoOf = () => (typeof o.assets === "function" ? o.assets() : (o.assets || {})) || {};
    const dirOf = () => (typeof o.dir === "function" ? o.dir() : (o.dir || "")) || "";

    /* —— 序列化：DOM → 纯文本（绿框还原成 @别名；块级换行补 \n） —— */
    function ser(node) {
        let out = "";
        for (const n of node.childNodes) {
            if (n.nodeType === 3) { out += n.nodeValue || ""; continue; }
            if (n.nodeType !== 1) continue;
            if (n.dataset && n.dataset.label) { out += "@" + n.dataset.label; continue; }
            if (n.tagName === "BR") { out += "\n"; continue; }
            const block = /^(DIV|P|LI|TR|SECTION)$/.test(n.tagName);
            const inner = ser(n);
            out += (block && out && !/\n$/.test(out) ? "\n" : "") + inner;
        }
        return out;
    }

    function makeTag(label) {
        const sp = document.createElement("span");
        sp.className = "h3d-rtag";
        sp.contentEditable = "false";
        sp.dataset.label = String(label);
        sp.title = `@${label}：本段引用的素材（编译为官方 <Picture/Video/Audio k>）`
            + "\n右侧 ✕ = 只取消**这一处**引用（同一素材引用 N 次就点 N 下，一次少一处）；"
            + "要一次清掉本段对它的全部引用，用引用条上 chip 旁边的 ✕。";
        /* 视觉上用「缩略图/类别图标 + 别名」替代原本的 `@别名`：@ 是 prompt 文本的
         * 语法标记（序列化时仍然由 ser() 在前面补 `@`，落到 ds.prompts 仍是
         * `@alias`），但在一个"看起来就是标签"的盒子里再写一遍 `@` 既冗余又占地方。
         * 图/视给缩略图（视频=浏览器首帧），音频给音符，一眼能分三类。 */
        const info = infoOf()[label] || {};
        const ainfo = {
            kind: String(info.kind || "image"),
            file: String(info.file || ""),
            asset_id: String(info.asset_id || ""),
        };
        sp.dataset.thumbSig = thumbSig(ainfo);
        sp.append(buildAssetThumb(dirOf(), ainfo));
        /* 显示剥掉夹带的格式后缀（chipLabelText）：dataset.label 保持原值，
         * 序列化/匹配仍按原别名走，只是不把 ".png" 摆到用户眼前。 */
        sp.append(document.createTextNode(chipLabelText(label)));
        /* ✕ 用 span 而不是 <button>：contenteditable 内的交互元素在 Chromium 下
         * 行为不一致（button 的 click 有时被选区逻辑吃掉 → "点了没反应"）。
         * 直接吃 mousedown：不依赖 click 配对，也不会因为重绘换节点而丢事件。 */
        const x = document.createElement("span");
        x.className = "h3d-rtagx";
        x.setAttribute("role", "button");
        x.textContent = "✕";
        x.title = `取消这一处引用（同一素材的其它引用保留；清全部请用引用条 chip 旁的 ✕）`;
        x.addEventListener("mousedown", (e) => {
            e.preventDefault();
            e.stopPropagation();
            removeOneTag(sp);            // 只取这一处 —— 不是 removeTag（那是清全部）
            if (typeof o.onRemove === "function") o.onRemove(label);
        });
        x.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); });
        sp.append(x);
        return sp;
    }

    /** 正文分词：把 `@别名` 切成引用 token（**最长优先**，与后端 compile_refs /
     *  refsFromText 同口径）。渲染、计数、删除全走这一条路 —— 早期用正则
     *  `@短标签(?![0-9A-Za-z_])` 去删，负向后顾只挡 ASCII 字母，于是删「阿依」
     *  会把「@阿依的家」的前缀吃掉（剩下「的家」）。 */
    function refTokens(text) {
        const s = String(text == null ? "" : text);
        const labs = [...labelsOf()].sort((a, b) => b.length - a.length);
        const out = [];
        let buf = "";
        let i = 0;
        while (i < s.length) {
            if (s[i] === "@" && !/[0-9A-Za-z_]/.test(s[i - 1] || "")) {
                const hit = labs.find((l) => s.startsWith(l, i + 1));
                if (hit) {
                    if (buf) { out.push({ text: buf }); buf = ""; }
                    out.push({ label: hit });
                    i += hit.length + 1;
                    continue;
                }
            }
            buf += s[i];
            i += 1;
        }
        if (buf) out.push({ text: buf });
        return out;
    }

    function render(text) {
        box.replaceChildren();
        for (const tk of refTokens(text)) {
            if (tk.label) box.append(makeTag(tk.label));
            else box.append(document.createTextNode(tk.text));
        }
    }

    /* —— 光标偏移（以序列化文本为准） —— */
    function rangeText(range) {
        const tmp = document.createElement("div");
        tmp.append(range.cloneContents());
        return ser(tmp);
    }
    function caret() {
        const sel = window.getSelection();
        if (!sel || !sel.rangeCount) return null;
        const r = sel.getRangeAt(0);
        if (!box.contains(r.startContainer)) return null;
        const pre = r.cloneRange();
        pre.selectNodeContents(box);
        pre.setEnd(r.startContainer, r.startOffset);
        return rangeText(pre).length;
    }
    function placeCaret(offset) {
        const want = Math.max(0, Number(offset) || 0);
        let acc = 0;
        const walk = (node) => {
            for (const n of node.childNodes) {
                if (n.nodeType === 3) {
                    const len = (n.nodeValue || "").length;
                    if (acc + len >= want) {
                        const r = document.createRange();
                        r.setStart(n, Math.max(0, want - acc));
                        r.collapse(true);
                        return r;
                    }
                    acc += len;
                } else if (n.nodeType === 1) {
                    if (n.dataset && n.dataset.label) {
                        const len = n.dataset.label.length + 1;
                        if (acc + len >= want) {
                            const r = document.createRange();
                            r.setStartAfter(n);
                            r.collapse(true);
                            return r;
                        }
                        acc += len;
                    } else if (n.tagName === "BR") {
                        if (acc + 1 >= want) {
                            const r = document.createRange();
                            r.setStartAfter(n);
                            r.collapse(true);
                            return r;
                        }
                        acc += 1;
                    } else {
                        const got = walk(n);
                        if (got) return got;
                    }
                }
            }
            return null;
        };
        const r = walk(box) || (() => {
            const rr = document.createRange();
            rr.selectNodeContents(box);
            rr.collapse(false);
            return rr;
        })();
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
    }

    function fireInput() {
        // h3synthetic：程序化插入（绿框/✕）触发的 input —— @补全据此跳过（别弹窗）
        const ev = new Event("input", { bubbles: true });
        ev.h3synthetic = true;
        box.dispatchEvent(ev);
    }

    /* 注意：box 是 div，**没有 .value**！取正文一律用 ser(box)（textarea 兼容面
     * 上的 api.value 才是 getter）。早期版本这里写成 box.value → undefined，
     * 于是 insertTag 抛 "reading 'length'"（chip 点了报错）、removeTag 静默失效
     * （✕ 取消不掉）。 */
    /** 插入一个绿框（在光标处）；同时可选插入前置/后置文本（引用语句式） */
    function insertTag(label, before, after) {
        const lbl = String(label || "");
        if (!lbl) return "";
        const at = caret();
        const cur = ser(box);                    // 序列化文本（绿框算 @别名 的长度）
        const plain = at == null ? cur.length : at;   // 没焦点=追加到末尾
        const pre = String(before || "");
        const post = String(after || "");
        const needSpace = cur && !/[\s\n]$/.test(cur.slice(0, plain)) ? " " : "";
        const next = cur.slice(0, plain) + needSpace + pre + `@${lbl}` + post + cur.slice(plain);
        render(next);                           // 重渲染（新 @别名 变成绿框）
        box.focus();                            // 先 focus 再摆光标：focus 会把光标重置到开头
        placeCaret(plain + needSpace.length + pre.length + lbl.length + 1);
        fireInput();
        return next;
    }

    /** 删除 token 后的空白收拾（行内连续空格压一个、行首行尾空白去掉） */
    function tidy(t) {
        return String(t).replace(/[ \t]{2,}/g, " ")
            .replace(/ *\n */g, "\n").replace(/^[ \t]+/, "").replace(/[ \t]+$/, "");
    }

    /** 清掉本段对该素材的**全部**引用（引用条 chip 旁的 ✕ / 右键 chip 走这里）。 */
    function removeTag(label) {
        const cur = ser(box);
        const want = String(label || "");
        const at = caret();
        let hit = 0;
        let next = "";
        for (const tk of refTokens(cur)) {
            if (tk.label && tk.label === want) { hit += 1; continue; }
            next += tk.label ? `@${tk.label}` : tk.text;
        }
        if (!hit) return false;
        next = tidy(next);
        render(next);
        box.focus();
        if (at != null) placeCaret(Math.min(at, next.length));
        fireInput();
        return true;
    }

    /** 只取消**一处**引用（绿框自带的 ✕ 走这里）：删掉被点的那个绿框对应的那一次
     *  `@别名`，同名其它引用原样保留 —— 引用次数 N 降到 N-1，降到 0 时引用条里对应的
     *  chip 自动置灰（＋@别名）。
     *
     *  为什么按"第几个"删而不是按 DOM 节点直接删：正文（序列化文本）是唯一真相，
     *  所有渲染都由 render(文本) 重建，所以只能把文本里的第 nth 个同名 token 去掉。
     *  绿框顺序与 refTokens 顺序一致（都由 render 产出），因此 DOM 里第 nth 个同名
     *  绿框 == 文本里第 nth 个同名 token。 */
    function removeOneTag(tagEl) {
        const want = String((tagEl && tagEl.dataset && tagEl.dataset.label) || "");
        if (!want) return false;
        let nth = 0;
        if (tagEl) {
            for (const n of box.querySelectorAll(".h3d-rtag")) {
                if (n === tagEl) break;
                if ((n.dataset.label || "") === want) nth += 1;
            }
        }
        const at = caret();
        let seen = 0;
        let hit = false;
        let next = "";
        for (const tk of refTokens(ser(box))) {
            if (tk.label && tk.label === want) {
                if (seen === nth) { seen += 1; hit = true; continue; }
                seen += 1;
                next += `@${tk.label}`;
                continue;
            }
            next += tk.label ? `@${tk.label}` : tk.text;
        }
        if (!hit) return false;
        next = tidy(next);
        render(next);
        box.focus();
        if (at != null) placeCaret(Math.min(at, next.length));
        fireInput();
        return true;
    }

    function tagCount(label) {
        const want = String(label || "");
        return refTokens(ser(box)).filter((tk) => tk.label === want).length;
    }

    /* —— textarea 兼容面 —— */
    const api = {
        el: box,
        get value() { return ser(box); },
        set value(v) {
            const had = document.activeElement === box;
            const at = had ? caret() : null;
            render(v);
            if (had && at != null) placeCaret(Math.min(at, ser(box).length));
        },
        get dataset() { return box.dataset; },
        get placeholder() { return box.dataset.ph || ""; },
        set placeholder(v) { box.dataset.ph = String(v || ""); },
        get title() { return box.title; },
        set title(v) { box.title = String(v || ""); },
        get disabled() { return box.contentEditable === "false"; },
        set disabled(v) {
            box.contentEditable = v ? "false" : "true";
            box.classList.toggle("h3d-rta-off", !!v);
        },
        get selectionStart() { const c = caret(); return c == null ? this.value.length : c; },
        get selectionEnd() { return this.selectionStart; },
        setSelectionRange(a) { placeCaret(a); },
        focus() { try { box.focus(); } catch (e) { /* 不可聚焦时忽略 */ } },
        blur() { try { box.blur(); } catch (e) { /* 忽略 */ } },
        addEventListener(t, fn, cap) { box.addEventListener(t, fn, cap); },
        removeEventListener(t, fn, cap) { box.removeEventListener(t, fn, cap); },
        dispatchEvent(ev) { return box.dispatchEvent(ev); },
        getBoundingClientRect() { return box.getBoundingClientRect(); },
        contains(n) { return box === n || box.contains(n); },
        insertText(text) {
            // 纯文本插入（含 @别名 时同样渲染成绿框）
            const at = caret();
            const cur = this.value;
            const pos = at == null ? cur.length : at;
            const next = cur.slice(0, pos) + String(text || "") + cur.slice(pos);
            this.value = next;
            box.focus();                        // 先 focus 再摆光标
            placeCaret(pos + String(text || "").length);
            fireInput();
            return next;
        },
        insertTag,
        removeTag,
        removeOneTag,
        tagCount,
        normalizeLoose,
        setLabels(fn) { o.labels = fn; },
        setAssets(fn) { o.assets = fn; },
        setDir(fn) { o.dir = fn; },
        /** 库里改了素材（换类别/换文件）后刷新绿框的缩略图/图标：保留焦点与光标，
         * 只换标识节点，不动正文（dataset.label 与序列化文本都不变）。
         * 幂等：签名（kind|file|asset_id）没变就整段跳过 —— 卡片轻量重绘会反复调它，
         * 不设这个闸门的话每次重绘都要重建 <video>，首帧会反复重解码。 */
        redrawIcons() {
            try {
                const had = box.contains(document.activeElement);
                const at = had ? caret() : null;
                let changed = 0;
                for (const n of box.querySelectorAll(".h3d-rtag")) {
                    const lb = n.dataset && n.dataset.label;
                    if (!lb) continue;
                    const info = infoOf()[lb] || {};
                    const ainfo = {
                        kind: String(info.kind || "image"),
                        file: String(info.file || ""),
                        asset_id: String(info.asset_id || ""),
                    };
                    const sig = thumbSig(ainfo);
                    if (n.dataset.thumbSig === sig
                        && n.querySelector(":scope > .h3d-thumb, :scope > .h3d-kindmark")) {
                        continue;                        // 标识没变：不碰 DOM
                    }
                    const next = buildAssetThumb(dirOf(), ainfo);
                    const cur = n.querySelector(":scope > .h3d-thumb, :scope > .h3d-kindmark");
                    if (cur) cur.replaceWith(next);
                    else n.prepend(next);
                    n.dataset.thumbSig = sig;
                    changed++;
                }
                if (had && at != null && changed) placeCaret(Math.min(at, ser(box).length));
            } catch (e) { /* 失焦等边界态不影响主流程 */ }
        },
    };
    /* 粘贴只收纯文本：防 HTML 注入 / 样式污染 */
    box.addEventListener("paste", (e) => {
        const t = e.clipboardData?.getData("text/plain");
        if (t == null) return;
        e.preventDefault();
        api.insertText(t);
    });
    /* 失焦时规范化：手打的 @别名 也变成绿框 */
    box.addEventListener("blur", () => { render(api.value); });

    /* 打字停顿后自动成框：手打 @别名 也会变成绿框（700ms 防抖 + IME 保护，
     * 只在"确实还有没成框的 @别名"时才动 DOM，避免打断中文输入法组合）。
     * 同时对外暴露 normalizeLoose()：卡片轻量重绘时**立即**补框，不等 700ms、
     * 也不等整卡重建（重建被焦点守卫挡着时，晚到素材的 @别名会一直留成裸文本
     * —— 就是"第二个素材的绿框来不及出现，再点一个才出来"的成因）。 */
    let composing = false;
    let normTimer = 0;
    box.addEventListener("compositionstart", () => { composing = true; });
    box.addEventListener("compositionend", () => { composing = false; });
    /** 把正文里"还没成框"的 @别名 就地渲染成绿框（幂等：无裸别名时返回 false 不动 DOM） */
    function normalizeLoose() {
        if (composing) return false;
        const text = api.value;
        const domCount = {};
        box.querySelectorAll(".h3d-rtag").forEach((n) => {
            const l = n.dataset.label || "";
            domCount[l] = (domCount[l] || 0) + 1;
        });
        let loose = false;
        for (const l of labelsOf()) {
            const inText = text.split(`@${l}`).length - 1;
            if (inText > (domCount[l] || 0)) { loose = true; break; }
        }
        if (!loose) return false;
        const at = caret();
        render(text);
        if (at != null) placeCaret(Math.min(at, text.length));
        return true;
    }
    box.addEventListener("input", (e) => {
        if (e && e.h3synthetic) return;
        clearTimeout(normTimer);
        normTimer = setTimeout(normalizeLoose, 700);
    });
    api.value = String(o.value || "");
    return api;
}

/* 注：引用 token 的匹配/删除一律走 refTokens（最长优先分词），不再用
 * `@标签(?![0-9A-Za-z_])` 这类正则 —— 负向后顾挡不住中文，短标签会吃掉长标签前缀。 */

/* ---------- 引用条：三栏各一条，互不共享 ----------
 * 意图 / 剧本 / 结果 三栏各自持有一条引用栏，谁也不改谁：
 * 每条只读自己正文里的 @别名（正文即真相），点 chip 只写自己的正文。
 * 只有「③ 结果」那条 gate=true：走官方 9/3/3 上限、写 seg.refs / ds.prompts，
 * 也只有它进模型。①② 纯标注（给人/AI 看），不参与 conditioning。
 * 具象化的引用由它自己的参考组管理，不与主框三栏互写。
 */
function buildRefBar(RB) {
    const node = RB.node;
    const segIdx = RB.segIdx;
    const ed = RB.editor || null;
    const pool = RB.pool || [];
    const livePool = RB.livePool;
            /* 统一引用条（tab 外常驻）：
             * chip 点一次 = 在提示词框里插一个绿框（显示 ×N），同一素材可引用多次
             * —— 正文里就有 N 个 @别名，编译后是同一个 <Picture k> 出现 N 次；
             * chip 旁的「✕」= 取消本段对它的**全部**引用（正文里的绿框一起清掉）。
             * 素材个数上限仍按官方 9/3/3（去重算），重复次数另有软上限。
             * 下拉 = 锚定方式（先选方式再点素材，按方式把句式写进正文）。
             * 末尾两个按钮：本段的首帧图 / 尾帧图参考（从项目里的图片选或现传）。
             * **不再要求"建卡时已有素材"**：素材晚到时 chips 由 syncChips 自动补出来，
             * 引用条本身不必等整卡重建才出现（否则零素材时上传第一张会"没反应"）。 */
            const refbar = el("div", "h3d-refrow");
            refbar.append(el("label", "", RB.title || "引用素材"));
            const counter = el("span", "h3d-secs-hint", "");
            refbar.append(counter);        // 计数先入位：chips 一律插在它前面（syncChips 用）
            /* 素材池取**活**的节点状态（上传/改名/删除素材后 chips 与别名表立即跟上），
             * 建卡快照 pool 只作回落 —— 快照会一直停在建卡那一刻。 */
                            /* 锚定方式（引用语）：下标=REF_TEMPLATES，默认「裸引用（不加描述）」。
             * 按段记住（_refTpl），重绘不丢。 */
            let refTpl = _refTpl.has(RB.tplKey) ? _refTpl.get(RB.tplKey) : REF_TPL_DEFAULT;
            if (!REF_TEMPLATES[refTpl]) refTpl = REF_TPL_DEFAULT;
            /* 官方 tag 预览：按「本段勾选顺序」对三类各自独立编号（与后端
             * compile_refs 同口径，重复引用只占一个编号）—— 让用户直接看到
             * @女主 会变成 <Picture 1>，写几次就出现几次。 */
            const TOK_FMT = { image: "<Picture {}>", video: "<Video {}>", audio: "<Audio {}>" };
            /* —— 单 chip 内部结构（缩略图/图标 + 名字 + ✓/＋ + 计数） ——
             * 不再是「✓ @label」裸文本：@ 在这里**没有语义**——这个 chip 不在
             * prompt 正文里，它只是引用条上的勾选钮。标识统一走 buildAssetThumb：
             * 图片=缩略图、视频=首帧、音频=音符图标。 */
            const buildChipBody = (c, a) => {
                const oldIco = c.querySelector(":scope > .h3d-thumb, :scope > .h3d-kindmark");
                if (oldIco) oldIco.remove();
                const oldText = c.querySelector(":scope > .h3d-chipbtn-text");
                if (oldText) oldText.remove();
                c.append(buildAssetThumb(getDirValue(node), a));
                const txt = document.createElement("span");
                txt.className = "h3d-chipbtn-text";
                c.append(txt);
                c.dataset.thumbSig = thumbSig(a);        // 记签名：换类别/换文件时好重建
            };
            const paintChip = (rec, n, token) => {
                const { c, a, roles } = rec;
                c.classList.toggle("on", n > 0);
                /* 标识签名变了（库里换类别/换文件）→ 重建标识；签名没变则一次都不碰
                 * DOM（旧实现只看"有没有标识节点"，换类别后 chip 图标会一直停在旧样式）。 */
                if (c.dataset.thumbSig !== thumbSig(a)
                    || !c.querySelector(":scope > .h3d-thumb, :scope > .h3d-kindmark, :scope > .h3d-chipbtn-text")) {
                    buildChipBody(c, a);
                }
                const lbl = chipLabelText(a.label) || a.label || "素材";
                const txt = c.querySelector(":scope > .h3d-chipbtn-text")
                    || c.appendChild(Object.assign(document.createElement("span"),
                        { className: "h3d-chipbtn-text" }));
                txt.textContent = `${n > 0 ? "✓ " : "＋ "}${lbl}${roles}${n > 1 ? ` ×${n}` : ""}`;
                const armedName = (REF_TEMPLATES[refTpl] || [])[0] || "裸引用（不加描述）";
                c.title = `${KIND_NAME[a.kind] || ""}「${lbl}」${roles}：`
                    + (n > 0
                        ? `本段已引用 ${n} 次${token ? `（正文里每个 @${a.label} 都会编译成 ${token}）` : ""}`
                        : "本段未引用")
                    + "\n点一次 = 在提示词框里插一个绿框（图标+别名，点两次=引用两次）；"
                    + "右侧 ✕（或右键本 chip）= 取消本段对它的全部引用。"
                    + "\n（正文里绿框自带的 ✕ 是「只取消那一处」，点一次少一处。）"
                    + `\n当前锚定方式「${armedName}」：点它会按这个方式写入正文。`;
            };
            const chips = [];
            let chipKeys = null;      // 已建 chips 的别名序列（与 livePool 对齐；池变了才重建 DOM）
            const syncChips = (live) => {
                const arr = live || livePool();
                const joined = arr.map((x) => String(x.label || "")).join("\u0001");
                if (joined === chipKeys) return;       // 池没变：chips DOM 原样留用
                chipKeys = joined;
                for (const rec of chips.splice(0)) { rec.c.remove(); rec.minus.remove(); }
                for (const a of arr) {
                    const rec = mkChip(a);
                    refbar.insertBefore(rec.c, counter);
                    refbar.insertBefore(rec.minus, counter);
                    chips.push(rec);
                }
            };
            const paintAll = () => {
                const live = livePool();
                syncChips(live);
                const refs = RB.readRefs();
                const cnt = { image: 0, video: 0, audio: 0 };
                const tokens = {};
                for (const l of new Set(refs)) {          // 编号按素材，不看重复
                    const hit = live.find((x) => x.label === l) || pool.find((x) => x.label === l);
                    if (!hit) continue;
                    const k = hit.kind || "image";
                    if (cnt[k] === undefined) cnt[k] = 0;
                    cnt[k] += 1;
                    tokens[l] = String(TOK_FMT[k] || TOK_FMT.image).replace("{}", cnt[k]);
                }
                for (const rec of chips) {
                    const n = refs.filter((x) => x === rec.a.label).length;
                    paintChip(rec, n, tokens[rec.a.label]);
                    /* ✕ 常驻显示（未引用时置灰）：以前"有引用才出现"，
                     * 用户想取消时找不到 —— 就是"点不了了"的来源。 */
                    rec.minus.classList.toggle("off", n <= 0);
                }
                const nAsset = new Set(refs).size;
                /* RB.note：调用方补一句说明（如"其中 2 个来自素材调度"）。
                 * 只读展示，不影响 chips 点亮逻辑。 */
                let noteTxt = "";
                try {
                    noteTxt = String(typeof RB.note === "function" ? RB.note() : (RB.note || "")).trim();
                } catch (e) { noteTxt = ""; }
                const tail = noteTxt ? ` · ${noteTxt}` : "";
                counter.textContent = nAsset
                    ? (RB.gate
                        ? `已引用 ${nAsset} 个素材（共 ${refs.length} 次 · 图${cnt.image}/${KIND_CAPS.image}·视${cnt.video}/${KIND_CAPS.video}·音${cnt.audio}/${KIND_CAPS.audio}）${tail}`
                        : `已标注 ${nAsset} 个素材（共 ${refs.length} 次 · 仅标注，不进模型）${tail}`)
                    : ((RB.gate ? "本段未引用素材" : "本段未标注素材") + tail);
            };
            /* 用 mousedown 而不是 click：① 卡片重绘可能夹在 mousedown/click
             * 之间把节点换掉 → click 永远不来（"点了没反应"）；
             * ② preventDefault 不让编辑器失焦，避免失焦回写旧文本。
             * 失败也可见（不再静默）。 */
            const guard = (fn) => (e) => {
                e.preventDefault();
                e.stopPropagation();
                try { fn(); } catch (err) {
                    console.error("[h3-director] 引用操作失败：", err);
                    setLed("error", `引用操作失败：${err?.message || err}`);
                }
            };
            /* 单个 chip（含旁挂 ✕）的构造：池变化时 syncChips 会整批重建，所以
             * 构造函数必须独立于"建卡那一刻的池"。 */
            const mkChip = (a) => {
                const roles = Array.isArray(a.roles) && a.roles.length ? `【${a.roles.join("·")}】` : "";
                const c = el("button", "h3d-chipbtn");
                c.type = "button";
                c.dataset.ref = String(a.label || "");
                /* 一次建好"图标 + 文本"骨架：paintChip 只更新文本，不再重建 DOM，
                 * 这样 chip 不会因为 paint 闪烁，鼠标悬停/焦点也保得住。 */
                buildChipBody(c, a);
                const minus = el("button", "h3d-chipminus", "✕");
                minus.type = "button";
                minus.title = `取消本段对「${a.label}」的全部引用（正文里的绿框一起清掉；`
                    + `只想取消某一处，用正文里那个绿框自带的 ✕）`;
                c.addEventListener("mousedown", guard(() => {
                    if (RB.gate && !canAddRef(node, segIdx, a.label)) return;
                    if (!ed) return;                       // 没有可写正文（只读/无节点）
                    {
                        const tplDef = REF_TEMPLATES[refTpl];
                        if (tplDef) {
                            /* 锚定方式：按句式写入（@别名 落成绿框，前后文一起进正文） */
                            const phrase = tplDef[1](a.label);
                            const m = /^(.*?)@([^\s]+)([\s\S]*)$/.exec(phrase);
                            if (m) ed.insertTag(a.label, m[1], m[3]);
                            else ed.insertText(phrase);
                        } else {
                            ed.insertTag(a.label);
                        }
                        RB.commit(ed);
                                                }
                    RB.repaint();              // chip + 计数 + 绿框一次到位（不等刷新）
                    scheduleRefresh(240);
                }));
                const clearAll = () => {
                    /* 只来自素材调度、正文里没有 @ 的引用：✕ 在正文里清不到任何东西。
                     * 不明确说一句就会变成"点了没反应"（历史上这类抱怨的来源）。 */
                    let inherited = [];
                    try {
                        inherited = (typeof RB.inherited === "function"
                            ? RB.inherited() : (RB.inherited || []));
                    } catch (e) { inherited = []; }
                    if (inherited.includes(a.label)) {
                        setLed("warn", `「${a.label}」来自素材调度，生成时真的会送图；`
                            + "要取消请到素材调度里移除");
                        return;
                    }
                    if (ed) {
                        ed.removeTag(a.label);              // 正文里所有该绿框一次清掉
                        RB.commit(ed);
                    }
                    if (RB.gate) removeSegmentRef(node, segIdx, a.label);
                    RB.repaint();
                    scheduleRefresh(240);
                };
                minus.addEventListener("mousedown", guard(clearAll));
                /* 右键 chip = 一键清零（兜底：万一 ✕ 没看见） */
                c.addEventListener("contextmenu", (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    try { clearAll(); } catch (err) { console.error(err); }
                });
                return { c, minus, a, roles };
            };
            paintAll();
            /* 段级首尾帧参考图：从项目里的图片选（或现传一张），作为本段的
             * 首帧 / 尾帧参考（段头 / 段尾身份锚；首段首帧图 = i2v 起手帧）。 */
            if (RB.showFrames) refbar.append(mkFrameBtns(node, segIdx, () => scheduleRefresh(240)));
                            /* 锚定方式（引用语）：**先选方式，再点参考素材** —— 点哪个素材就按该方式
             * 把句式写进正文（`场景以 @X 为准…`）。
             * 选择是"常驻模式"（切段/重绘后仍保持）；默认「裸引用（不加描述）」= 只插
             * 裸 @标签（原「无」档与它插入的正文完全一样，已合并成一个选项）。 */
            const tpl = document.createElement("select");
            tpl.className = "h3d-reftpl";
            const mkOpt = (v, txt) => {
                const o = document.createElement("option");
                o.value = String(v);
                o.textContent = txt;
                return o;
            };
            REF_TEMPLATES.forEach(([name], ti) => tpl.append(mkOpt(ti, `锚定方式：${name}`)));
            tpl.value = String(refTpl);
            tpl.title = "选一种锚定方式，再点上面的参考素材：该素材会按这个方式写进提示词。"
                + "\n选「裸引用（不加描述）」= 只插 @标签，用法自己在正文里描述。";
            tpl.onchange = () => {
                const v = Number(tpl.value);
                refTpl = Number.isInteger(v) && REF_TEMPLATES[v] ? v : REF_TPL_DEFAULT;
                _refTpl.set(RB.tplKey, refTpl);
                paintAll();
            };
            refbar.append(tpl);
            return { el: refbar, paint: paintAll };
}
/** 正文 → 本段引用集合（正文是唯一真相）：按首次出现顺序，允许重复计数。
 *  与后端 compile_refs / _find_refs 同口径（`@标签`，负向后顾防 a@b.com）。 */
function refsFromText(text, pool) {
    const s = String(text || "");
    const labs = (pool || []).map((a) => String(a.label || "")).filter(Boolean)
        .sort((a, b) => b.length - a.length);
    const out = [];
    let i = 0;
    while (i < s.length) {
        if (s[i] === "@" && !/[0-9A-Za-z_]/.test(s[i - 1] || "")) {
            const hit = labs.find((l) => s.startsWith(l, i + 1));
            if (hit) { out.push(hit); i += hit.length + 1; continue; }
        }
        i += 1;
    }
    return out;
}

/** 段引用同步（ds 内联改）：正文里的 @别名 序列 → seg.refs */
function syncRefsFromText(ds, idx, text) {
    const seg = (ds.segments || [])[idx];
    if (!seg) return false;
    const next = refsFromText(text, ds.ref_assets);
    const cur = Array.isArray(seg.refs) ? seg.refs : [];
    if (cur.length === next.length && cur.every((x, i) => x === next[i])) return false;
    seg.refs = next;
    return true;
}

/* P3：@补全——提示词框内 @ 前缀弹出池别名，点选/回车插入 @别名。
 * 只改文本不调接口不重建面板（焦点守卫安全）；选中后派发 input 走既有防抖落盘。 */
function attachAtComplete(ta, node) {
    if (!ta || ta.dataset.h3at === "1") return;
    ta.dataset.h3at = "1";
    let pop = null, items = [], sel = 0;
    const close = () => { if (pop) { pop.remove(); pop = null; } };
    const current = () => {
        const pos = ta.selectionStart ?? ta.value.length;
        const m = /@([^\s@\[\]]*)$/.exec(ta.value.slice(0, pos));
        return m ? { word: m[1], start: pos - m[0].length } : null;
    };
    const paint = () => {
        close();
        const c = current();
        if (!c) return;
        let pool = [];
        try { pool = getDs(node).ref_assets || []; } catch (e) { pool = []; }
        const w = c.word.toLowerCase();
        items = pool.filter((a) => String(a.label || "").toLowerCase().includes(w)).slice(0, 8);
        if (!items.length) return;
        pop = el("div", "h3d-atpop");
        items.forEach((a, idx) => {
            const d = el("div", idx === sel ? "on" : "",
                `${escapeHtml(a.label)}<small>${KIND_NAME[a.kind] || ""}</small>`);
            d.onmousedown = (e) => { e.preventDefault(); pick(idx); };
            pop.append(d);
        });
        const r = ta.getBoundingClientRect();
        pop.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - 320))}px`;
        pop.style.top = `${Math.min(r.bottom + 4, window.innerHeight - 260)}px`;
        document.body.append(pop);
    };
    const pick = (idx) => {
        const c = current();
        const a = items[idx];
        close();
        if (!c || !a) return;
        const pos = ta.selectionStart ?? ta.value.length;
        ta.value = ta.value.slice(0, c.start) + `@${a.label}` + ta.value.slice(pos);
        const np = c.start + a.label.length + 1;
        ta.focus();
        ta.setSelectionRange(np, np);
        ta.dispatchEvent(new Event("input", { bubbles: true }));
    };
    ta.addEventListener("input", (e) => {
        // 程序化插入（绿框/✕）不触发补全：插入后光标正好贴在 @别名 后面，
        // 否则会立刻弹出一个"已完成"的候选框
        if (e && e.h3synthetic) { close(); return; }
        sel = 0;
        paint();
    });
    ta.addEventListener("keydown", (e) => {
        if (!pop) return;
        if (e.key === "ArrowDown") { e.preventDefault(); sel = (sel + 1) % items.length; paint(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); sel = (sel - 1 + items.length) % items.length; paint(); }
        else if (e.key === "Enter" && items.length) { e.preventDefault(); pick(sel); }
        else if (e.key === "Escape") close();
    });
    ta.addEventListener("blur", () => setTimeout(close, 150));
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
    return { schema: 2, on: true, mode: "关闭", model: "", arch: "auto", scale: 2.0,
             /* 目标尺寸模式：倍率（现状默认）/ 目标尺寸 / 百万像素 */
             size_mode: "倍率", target_w: 1280, target_h: 704, megapixels: 1.0,
             denoise: 0.35, steps: 6, cfg: 1.0, precision: "fp16",
             time_bias: 0.0, mix: 0.0, adaptive: false, shift: 0.0,
             stg: 0.0, stg_block: 25, passes: 1, decay: 0.5,
             sharpen: 0.0, pixel_sharpen: 0.0, encode: "标准",
             /* 3D 时序分块：默认开（省显存 + 治末端闪烁），关掉才进二采指纹 */
             chunk: true,
             /* 设备：自动（默认）/ cuda / rocm / cpu；强制卸载默认关 */
             device: "auto", force_unload: false,
             sampler: "", scheduler: "", retry: false, retry_target: 0.15,
             include: [] };
}

function defaultSegment() {
    // 双按钮（null=跟随全局默认开）：auto_ref=false≈旧unlink，auto_seq=false≈旧disabled；
    // latent_ref（null=跟随全局尾部N帧注入），latent_save（null=默认全存）。
    /* 主框三段式（都**不进模型**，只有 ds.prompts[idx] 进模型）：
     *   intent_zh = 中文意图（支持 @素材，给人和 AI 看）
     *   script    = AI 扩写产物（官方格式剧本，可人工改）
     * 与 prompt_v2.intent_zh 是两回事：后者是具象化内部字段。 */
    return { intent_zh: "", script: "", scene_prompt: "", character_prompt: "", soundscape: "", music: "", seconds: null, refs: [], unlink: false, disabled: false, auto_ref: null, auto_seq: null, frame_refs: null, prompt_v2: null, latent_save: null, latent_ref: null, tail_src: null, v2mode: null, anchors: [] };
}

function segAutoRef(seg) {
    if (seg && seg.auto_ref !== undefined && seg.auto_ref !== null) return !!seg.auto_ref;
    if (seg && typeof seg.unlink === "boolean") return !seg.unlink;
    return true;
}

function segAutoSeq(seg) {
    if (seg && seg.auto_seq !== undefined && seg.auto_seq !== null) return !!seg.auto_seq;
    if (seg && typeof seg.disabled === "boolean") return !seg.disabled;
    return true;
}

/** manifest.seg_fields[全局槽位] -> 分段字段对象（与 defaultSegment 同构）。
 *  旧存档无该键/槽位为 null（序章/插入槽）→ 全默认空段；键缺失容错补齐。
 *  v2：prompt_v2 / latent_save 原样透存（对象才收，无则 null；后端 clean 收敛）。 */
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
        auto_ref: (raw.auto_ref === null || raw.auto_ref === undefined) ? null : !!raw.auto_ref,
        auto_seq: (raw.auto_seq === null || raw.auto_seq === undefined) ? null : !!raw.auto_seq,
        frame_refs: Array.isArray(raw.frame_refs) ? raw.frame_refs.map(String) : null,
        /* 段级首尾帧参考图：**读档必须还原**（flushPrompts 存进了 mf.seg_fields）。
         * 以前这里漏了它 → 一切项目/读档，整链的段级首/尾帧锚全被清成空，
         * 而段卡上还显示着旧文件名（签名里是有的），于是"看着有锚、跑起来没锚"。 */
        frame_img: (raw.frame_img && typeof raw.frame_img === "object")
            ? {
                first: String(raw.frame_img.first || "").trim().replace(/\\/g, "/"),
                end: String(raw.frame_img.end || "").trim().replace(/\\/g, "/"),
            } : null,
        prompt_v2: (raw.prompt_v2 && typeof raw.prompt_v2 === "object") ? raw.prompt_v2 : null,
        latent_save: (raw.latent_save && typeof raw.latent_save === "object") ? raw.latent_save : null,
        latent_ref: (raw.latent_ref && typeof raw.latent_ref === "object") ? raw.latent_ref : null,
        tail_src: (raw.tail_src && typeof raw.tail_src === "object") ? raw.tail_src : null,
        /* 手动锚定（双轨时间线）：读档必须还原，否则"看着有锚、跑起来没锚"——与 frame_img 同款坑 */
        anchors: Array.isArray(raw.anchors) ? raw.anchors : [],
        v2mode: V2_MODES.includes(raw.v2mode) ? raw.v2mode : null,
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
        /* file 为空但带 asset_id 的条目必须留下：那是全局库链接条目，file 由后端
         * 按 asset_id 回填（全局库不可用时回填不到）。以前这里按 file 一刀切，
         * 于是"从全局库调进来的图在引用条里一个都不出现"。 */
        let refAssets = Array.isArray(raw.ref_assets)
            ? raw.ref_assets.filter((a) => a && typeof a === "object" && (a.file || a.asset_id))
                .map((a) => ({
                    file: String(a.file),
                    kind: KIND_LIST.includes(a.kind) ? a.kind : "image",
                    label: cleanLabel(a.label) || "",
                    /* P3：全局库稳定 ID（空=旧本地条目；保存 manifest.assets 时剥离，住 asset_links） */
                    asset_id: typeof a.asset_id === "string" ? a.asset_id.trim() : "",
                    roles: Array.isArray(a.roles)
                        ? a.roles.map(String).filter((r) => r === "首帧图" || r === "尾帧图")
                        : [],
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
        /* P3：段引用同时接受 asset_id（后端 compile_refs 同口径；写回时保持原样） */
        const validIds = new Set(refAssets.map((a) => a.asset_id).filter(Boolean));
        segments = segments.map((s) => {
            const sec = Number(s?.seconds);
            return {
                scene_prompt: typeof s?.scene_prompt === "string" ? s.scene_prompt : "",
                character_prompt: typeof s?.character_prompt === "string" ? s.character_prompt : "",
                soundscape: typeof s?.soundscape === "string" ? s.soundscape : "",
                music: typeof s?.music === "string" ? s.music : "",
                seconds: (isFinite(sec) && sec > 0) ? Math.min(15, Math.max(0.5, sec)) : null,
                /* refs 允许重复：同一素材引用 N 次（正文写 N 个 @别名）——不能用 Set 去重 */
                refs: Array.isArray(s?.refs)
                    ? s.refs.map((r) => String((r && typeof r === "object") ? (r.asset || r.id || r.label || "") : r))
                        .filter((l) => validLabels.has(l) || validIds.has(l)) : [],
                /* 段级首尾帧参考图（提示词框按钮选的项目内图片）：{first,end} 相对路径 */
                frame_img: (s?.frame_img && typeof s.frame_img === "object")
                    ? {
                        first: String(s.frame_img.first || "").trim().replace(/\\/g, "/"),
                        end: String(s.frame_img.end || "").trim().replace(/\\/g, "/"),
                    } : null,
                unlink: !!s?.unlink,
                disabled: !!s?.disabled,
                auto_ref: (s?.auto_ref === null || s?.auto_ref === undefined) ? null : !!s.auto_ref,
                auto_seq: (s?.auto_seq === null || s?.auto_seq === undefined) ? null : !!s.auto_seq,
                /* 段级首尾帧图引用：null=未设置（默认：首段参考首帧图、末段参考尾帧图）；
                 * 数组=显式勾选（可含 "首帧图"/"尾帧图"，空数组=两图都不参考） */
                frame_refs: Array.isArray(s?.frame_refs)
                    ? s.frame_refs.map(String).filter((v) => v === "首帧图" || v === "尾帧图")
                    : null,
                /* v2 具象化结构 + latent 策略：透存（防 getDs 归一化洗掉已存 prompt_v2） */
                prompt_v2: (s?.prompt_v2 && typeof s.prompt_v2 === "object") ? s.prompt_v2 : null,
                latent_save: (s?.latent_save && typeof s.latent_save === "object") ? s.latent_save : null,
                latent_ref: (s?.latent_ref && typeof s.latent_ref === "object") ? s.latent_ref : null,
                /* 段尾锚来源：{asset: 标签} | {latent: latent/x.pt} | null=无尾锚 */
                tail_src: (s?.tail_src && typeof s.tail_src === "object") ? s.tail_src : null,
                /* 手动锚定：透存（防 getDs 归一化洗掉已设 anchors；否则每帧渲染都把锚丢光） */
                anchors: Array.isArray(s?.anchors) ? s.anchors : [],
                /* v2 手动模式（null=跟随导演台） */
                v2mode: V2_MODES.includes(s?.v2mode) ? s.v2mode : null,
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
            /* 神经放大开关：旧 JSON 缺键 = 开（保持既有行为） */
            enlarge: upRaw.enlarge !== false,
            mode: UP_MODES.includes(upRaw.mode) ? upRaw.mode : "关闭",
            model: typeof upRaw.model === "string" ? upRaw.model : "",
            /* 网络架构：auto=按权重自动判定（默认）；显式 2D/3D 才按面板指定装 */
            arch: ["2D", "3D"].includes(upRaw.arch) ? upRaw.arch : "auto",
            scale: upNum(upRaw.scale, 2.0, 1.0, 4.0),
            size_mode: UP_SIZE_MODES.includes(upRaw.size_mode) ? upRaw.size_mode : "倍率",
            target_w: Math.round(upNum(upRaw.target_w, 1280, 64, 8192)),
            target_h: Math.round(upNum(upRaw.target_h, 704, 64, 8192)),
            megapixels: upNum(upRaw.megapixels, 1.0, 0.1, 16.0),
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
            /* 3D 时序分块：旧 JSON 缺键 = 开（对齐后端「默认开」） */
            chunk: upRaw.chunk !== false,
            device: ["", "auto", "cuda", "rocm", "cpu"].includes(upRaw.device) ? upRaw.device : "auto",
            force_unload: upRaw.force_unload === true,
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
            /* AI优化配置与历史（自研后端）：透存，不进后端指纹 */
            optimizer: (raw.optimizer && typeof raw.optimizer === "object") ? raw.optimizer : null,
            opt_hist: (raw.opt_hist && typeof raw.opt_hist === "object") ? raw.opt_hist : null,
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
        /* 与 getDs 同口径：只丢"既无 file 又无 asset_id"的真空条目
         * （旧写法按 file 过滤，会把全局库链接条目整批抹掉）。 */
        ds.ref_assets = ds.ref_assets.filter((a) => a && (a.file || a.asset_id));
        ds.ref_images = ds.ref_assets.filter((a) => (a.kind || "image") === "image" && a.file)
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

/* 模式选择已删除：链路由后端按实际引用自动推导。ds.mode 字段保留兼容
 * （旧工作流/模板里有），后端不再读取；syncModeWidget 照常同步画布控件显示。 */

/* ---- 提示词（状态驱动） ---- */

function getPrompts(node) {
    return getDs(node).prompts;
}

function setPromptText(node, idx, text) {
    const ds = getDs(node);
    if (idx < 0 || idx >= ds.prompts.length) return false;
    ds.prompts[idx] = text;
    /* 正文是唯一真相：本段引用集合跟着正文里的 @别名 走（手删绿框/手打 @别名都同步） */
    syncRefsFromText(ds, idx, text);
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
    _selSeg = ds.prompts.length - 1;   // 新增即选中
    schedulePromptFlush();
    scheduleRefresh(60);
}

function removePromptSegment(node, idx) {
    const ds = getDs(node);
    if (idx < 0 || idx >= ds.prompts.length) return;
    ds.prompts.splice(idx, 1);
    if (ds.segments) ds.segments.splice(idx, 1);
    if (!ds.prompts.length) { ds.prompts = [""]; if (ds.segments) ds.segments = [defaultSegment()]; }
    setDs(node, ds);
    if (_selSeg >= ds.prompts.length) _selSeg = Math.max(0, ds.prompts.length - 1);
    schedulePromptFlush();
    scheduleRefresh(60);
}

function clearPrompts(node) {
    const ds = getDs(node);
    ds.prompts = ds.prompts.map(() => "");
    // 清文本但保留每段时长/断链开关/v2结构（结构设置跨项目沿用）。
    // refs 一并清空：正文是引用的唯一真相，留 refs 会出现「正文没有 @标签、
    // 引用栏却还亮着」的自相矛盾（清不干净的另一半原因）。
    if (ds.segments) ds.segments = ds.segments.map((s) => ({
        ...defaultSegment(), seconds: s?.seconds ?? null,
        refs: [], unlink: !!s?.unlink,
        disabled: !!s?.disabled,
        auto_ref: (s?.auto_ref === null || s?.auto_ref === undefined) ? null : !!s.auto_ref,
        auto_seq: (s?.auto_seq === null || s?.auto_seq === undefined) ? null : !!s.auto_seq,
        prompt_v2: (s?.prompt_v2 && typeof s.prompt_v2 === "object") ? s.prompt_v2 : null,
        latent_save: (s?.latent_save && typeof s.latent_save === "object") ? s.latent_save : null,
        latent_ref: (s?.latent_ref && typeof s.latent_ref === "object") ? s.latent_ref : null,
        tail_src: (s?.tail_src && typeof s.tail_src === "object") ? s.tail_src : null,
        anchors: Array.isArray(s?.anchors) ? s.anchors : [],
        v2mode: V2_MODES.includes(s?.v2mode) ? s.v2mode : null,
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

/** 勾选/取消本段引用的素材标签；全部取消 = 只用提示词文本 @标签 出现的
 * （与后端约定一致：缺省文本驱动，不勾选也能跑）。
 *  勾选时按类别校验官方单段上限（图9/视3/音3），超限拦截并提示。
 *  P5 显性语义：勾选即在段正文末尾补可见 @标签（取消勾选不删正文，防丢字）；
 *  调用方 blur 先于 click 触发，主框未落盘的输入已先行 flush，无竞态。 */
/** 同一素材在一段内可被引用多次（refs 里出现 N 次 = 正文里写 N 次 @别名，
 *  编译后是同一个 <Picture k> 出现 N 次）。上限：素材个数按官方 9/3/3（去重算），
 *  单素材重复次数另有软上限，防手抖点爆。 */
const REF_REPEAT_MAX = 9;

function refCount(seg, label) {
    return (Array.isArray(seg?.refs) ? seg.refs : []).filter((l) => l === label).length;
}

/** 加一次引用（+1）。超限弹提示并返回 false。 */
function addSegmentRef(node, idx, label) {
    if (!canAddRef(node, idx, label)) return false;
    const ds = getDs(node);
    const seg = ds.segments[idx];
    if (!Array.isArray(seg.refs)) seg.refs = [];
    seg.refs.push(label);
    const cur = String((ds.prompts || [])[idx] || "");
    if (!cur.includes(`@${label}`)) {
        if (!Array.isArray(ds.prompts)) ds.prompts = [];
        ds.prompts[idx] = cur + (cur && !/\s$/.test(cur) ? " " : "") + `@${label}`;
    }
    setDs(node, ds);
    return true;
}

/** 减一次引用（-1）；减到 0 即从本段移除。 */
function removeSegmentRef(node, idx, label) {
    const ds = getDs(node);
    if (idx < 0 || idx >= (ds.segments || []).length) return false;
    const seg = ds.segments[idx];
    if (!Array.isArray(seg.refs)) seg.refs = [];
    if (refCount(seg, label) <= 1) seg.refs = seg.refs.filter((l) => l !== label);
    else seg.refs.splice(seg.refs.lastIndexOf(label), 1);
    setDs(node, ds);
    return true;
}

/** 加引用前的上限预检（不写状态）：素材个数按官方 9/3/3（去重算），
 *  单素材重复次数另有软上限。返回 true=可以加。 */
function canAddRef(node, idx, label) {
    const ds = getDs(node);
    const seg = (ds.segments || [])[idx];
    if (!seg) return false;
    const refs = Array.isArray(seg.refs) ? seg.refs : [];
    const asset = (ds.ref_assets || []).find((a) => a.label === label);
    const kind = asset ? asset.kind : "image";
    const uniq = new Set(refs);
    if (!uniq.has(label)) {
        const sameKind = [...uniq].filter((l) => {
            const a = (ds.ref_assets || []).find((x) => x.label === l);
            return a && a.kind === kind;
        }).length;
        if (sameKind >= KIND_CAPS[kind]) {
            alert(`本段引用${KIND_NAME[kind]}素材已达官方上限 ${KIND_CAPS[kind]} 个：请先取消一个再引用「${label}」`);
            return false;
        }
    }
    if (refs.filter((l) => l === label).length >= REF_REPEAT_MAX) {
        alert(`「${label}」在本段已引用 ${REF_REPEAT_MAX} 次（同一段上限）：先取消几次再引用`);
        return false;
    }
    return true;
}

/** 编辑器正文 → ds.prompts[idx] + seg.refs（正文是唯一真相）+ 落盘 */
function applyPromptEdit(node, idx, editor) {
    const text = (editor && typeof editor.value === "string")
        ? editor.value : String(editor == null ? "" : editor);
    cancelPromptWrite(idx);      // 先丢掉防抖中的旧文本，防被盖回去
    const ds = getDs(node);
    if (idx < 0 || idx >= (ds.prompts || []).length) return false;
    ds.prompts[idx] = text;
    syncRefsFromText(ds, idx, text);
    setDs(node, ds);
    schedulePromptFlush();
    return true;
}

/** 把锚定方式写进**已有**具象化结构：references 补官方角色句、retention 补官方
 *  保留标记（编译时进 subject_definitions / retention_analysis 两个字段）。
 *  本段没有 prompt_v2 时**不创建**——免得把三字段段悄悄切成六字段；那种情况下
 *  只靠正文里的句式文本生效（模型同样看得到）。 */
function applyRefAnchorToV2(node, idx, label, tplDef, roles) {
    const marker = (tplDef || [])[2];
    const roleNote = (tplDef || [])[3];
    const ds = getDs(node);
    const seg = (ds.segments || [])[idx];
    const pv = seg && seg.prompt_v2;
    if (!pv || typeof pv !== "object") return false;
    if (!Array.isArray(pv.references)) pv.references = [];
    const ref = pv.references.find((r) => r && (r.label === label || r.label === `@${label}`));
    if (ref) { if (roleNote) ref.note = roleNote; }
    else pv.references.push({ label, note: roleNote || (roles ? roles.slice(1, -1) : "") });
    if (marker) {
        if (!Array.isArray(pv.retention)) pv.retention = [];
        const rt = pv.retention.find((r) => r && r.label === label);
        if (rt) rt.marker = marker;
        else pv.retention.push({ label, marker, note: roleNote || "" });
    }
    setDs(node, ds);
    schedulePromptFlush();
    return true;
}

/** 旧调用的兼容入口：有则清空、无则加一次（语义 = 切换） */
function toggleSegmentRef(node, idx, label) {
    const ds = getDs(node);
    const seg = (ds.segments || [])[idx];
    const r = (seg && refCount(seg, label) > 0)
        ? removeSegmentRef(node, idx, label)
        : addSegmentRef(node, idx, label);
    return r;
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

/* ---- 资产池维护（状态驱动 + 配套工作流 LoadImage 镜像点亮/隐藏） ----
 * 旧三槽位上传（setFirstFrame/setEndFrame/setLastFrame/addAsset）已删除；
 * 素材的上传 / 入库 / 引用统一在「素材库」浏览器（web/h3_library.js）里做。 */

/* 素材池改动直写项目 manifest["assets"]（读最新 revision 落盘，冲突返回 false 调手动保存）。
 * P3：带 asset_id 的全局链接条目住 asset_links，不进 legacy assets（保存时剥离）。 */
async function persistPool(node) {
    try {
        const dir = getDirValue(node);
        if (!dir || !window.H3Api) return false;
        const ds = getDs(node);
        const mf = await fetchJson(`h3_projects/${dir}`, "manifest.json");
        const legacy = (ds.ref_assets || []).filter((a) => a && !a.asset_id);
        const r = await window.H3Api.saveAssets(dir, legacy, mf?.revision);
        if (r.body?.ok) { scheduleRefresh(400); return true; }
        return false;
    } catch (e) { return false; }
}

function removeRefImage(node, idx) {
    const ds = getDs(node);
    if (idx < 0 || idx >= ds.ref_assets.length) return;
    const gone = ds.ref_assets.splice(idx, 1)[0];
    if (gone && gone.label) {
        for (const s of ds.segments || []) {
            if (Array.isArray(s.refs)) {
                s.refs = s.refs.filter((l) => l !== gone.label
                    && (!gone.asset_id || l !== gone.asset_id));
            }
        }
    }
    setDs(node, ds);
    syncMirrors(node, ds);
    scheduleRefresh(120);
    persistPool(node);
    /* P3：链接条目同步解链（幂等；失败只记控制台，widget 已是真相） */
    if (gone && gone.asset_id && window.H3Api?.assetUnlink) {
        const dir = getDirValue(node);
        if (dir) {
            window.H3Api.assetUnlink({ dir, asset_id: gone.asset_id })
                .catch((e) => console.warn("[h3-director] assetUnlink failed:", e));
        }
    }
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
        /* P3：链接条目改名同步服务端 alias（同 alias 重指向语义） */
        if (asset.asset_id && window.H3Api?.assetLink) {
            const dir = getDirValue(node);
            if (dir) {
                window.H3Api.assetLink({ dir, asset_id: asset.asset_id, alias: asset.label, kind: asset.kind })
                    .catch((e) => console.warn("[h3-director] assetLink rename failed:", e));
            }
        }
    }
    setDs(node, ds);
    persistPool(node);
    return asset.label;
}

/* ---- 总提示词（多段一次性导入 / 导出）----
 * 格式（提示词正文按 H3 官方格式，见 tools/h3_prompt_expander/references/h3-dialect.md）：
 *   【段1】          ← 段头：【段1】/【第1段】/【段落1】等价；序号可省略，按出现顺序排段
 *   时长：5         → seg.seconds（官方字段；缺省=沿用全局「每段时长」）
 *   独立镜头：是    → seg.unlink（是/否；是=断链不锚定上段尾帧，跳转/闪回/蒙太奇用）
 *   参考：角色1，图片2 → seg.refs（逗号/顿号分隔的素材标签；不存在的标签分配时剔除并提示）
 *   意图：…         → seg.intent_zh（中文意图；AI 多段扩写的输入，不进模型）
 *   剧本：…         → seg.script（AI 扩写产物：中文自由格式，发散用，不进模型）
 *   提示词：…       → 段主体（ds.prompts[i]，即段卡「③ 结果」），可多行（续行不写标签）。
 *                     正文按 H3 官方格式写：三字段 integrated_multimodal_description /
 *                     overall_soundscape / non_diegetic_music（字段间空行），Ref2VA 六段式；
 *                     I2VA/FL2VA/L2VA 的关键帧对齐指令写在正文最前、空一行再接三字段。
 *   【完】          ← 结束标记（可选）：其后的所有内容（如 AI 的参考素材建议）不参与解析
 * 兼容：旧「场景/角色/环境音/配乐」四标签解析仍认（落到 seg.scene_prompt 等旧字段），
 *       但导出不再写——这两组旧字段已被具象化（prompt_v2）取代，仅保旧项目文本可粘回。
 * 规则：段头后未带标签的正文行视为提示词内容；只认上述行首标签，其余文本原样进主体
 * （官方字段标签 integrated_multimodal_description: 等不受影响）；某标签「写了即生效
 * （含写空=清空），没写不动该字段」。整个文本无任何段头时视为单段主体。
 * 容错：markdown 渲染界面复制常把段内换行合并成空格（软换行丢失），整段糊成一行；
 * 检测到段头不在行首独占即自动「重分行」（见 mpReflow）再按常规行解析。 */
const MP_HEAD_RE = /^【\s*(?:第\s*)?(?:段(?:落)?\s*(\d+)?|(\d+)\s*段(?:落)?)\s*】\s*$/;
const MP_END_RE = /^【\s*(?:完|END|end|结束)\s*】$/;
const MP_FIELD_RE = /^(场景|角色|环境音|配乐|时长|独立镜头|参考|意图|剧本|提示词)\s*[：:]\s*(.*)$/;
const MP_FIELDS = { "场景": "scene", "角色": "character", "环境音": "soundscape", "配乐": "music", "时长": "seconds", "独立镜头": "unlink", "参考": "refs", "意图": "intent", "剧本": "script", "提示词": "main" };
const MP_YES = ["是", "独立", "断链", "开", "true", "yes"];
const MP_NO = ["否", "连续", "关", "false", "no"];
/* 正文块字段：一旦进入，段级标签（时长/独立镜头/参考）就不再在正文里生效。
 * 背景：剧本正文是自由格式，模型常写「时长：9 秒」「配乐：无」这类行——
 * 当成段级标签会把 seg.seconds 冲成 NaN、剧本内容被截断，是个真实的解析冲突。 */
const MP_BODY_KEYS = new Set(["main", "intent", "script", "scene", "character",
    "soundscape", "music"]);
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
    return { main: undefined, intent: undefined, script: undefined, scene: undefined, character: undefined, soundscape: undefined, music: undefined, seconds: undefined, unlink: undefined, refs: undefined };
}

/** 解析总提示词文本。返回 { segs:[{main,scene,character,soundscape,music,seconds}…],
 *  notes:[提示字符串…] }；字段 undefined=该块未写（应用时不覆盖），空串=显式清空。 */
function parseMasterPrompt(text) {
    const out = { segs: [], notes: [] };
    const src = mpReflow(String(text ?? ""), out.notes);   // 软换行丢失容错（未走样原样返回）
    const lines = src.split(/\r\n|\r|\n/);
    let cur = null;          // 当前段对象
    let field = null;        // 当前续行归属字段（"main" 等）
    let inBody = false;      // 是否已在正文块内（见 MP_BODY_KEYS）
    let pendingBlank = 0;    // 段主体内待落实的空行数（官方三字段靠空行分隔，不能被吃掉）
    const stray = [];        // 首个段头之前的游离行（无段头时整体作单段主体）
    const openSeg = () => { cur = newMasterSeg(); out.segs.push(cur); field = "main"; inBody = false; pendingBlank = 0; };
    /* 续行追加：把待落实的空行先补回正文，再追加本行（段头/标签切换时丢弃 pendingBlank）。
     * 修过一个真 bug：旧写法只在「有记账空行」时才补换行，于是**连续两行正文被直接
     * 拼成一行**——多行剧本和官方三字段提示词一进一出就糊掉（官方格式靠空行分节，
     * 段内也有 [Shot N] 逐行写，拼接=毁格式）。正确口径：已有内容就至少补一个 \n，
     * 中间空了几行就补几+1 个；首行（含「标签后无内容即接正文」的空串）不加前缀。 */
    const pushBody = (key, text) => {
        const prev = cur[key];
        const gap = pendingBlank > 0 ? "\n".repeat(pendingBlank + 1) : "\n";
        pendingBlank = 0;
        if (prev === undefined || prev === "") cur[key] = text;      // 首行：别留前导换行
        else cur[key] = `${prev}${gap}${text}`;
    };
    for (const raw of lines) {
        const line = raw.trim();
        if (/^```/.test(line)) continue;               // markdown 代码围栏不参与解析
        if (!line) {                                   // 空行：段主体内的先记账，别处直接忽略
            if (cur && field) pendingBlank += 1;
            continue;
        }
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
            /* 已在正文块内 → 段级标签（时长/独立镜头/参考）不再是标签，
             * 原样归入正文。剧本正文里写「时长：9 秒」「配乐：无」是常态，
             * 这里不挡就会把段时长冲掉、剧本被截断。块级标签仍可切换归属。 */
            if (inBody && !MP_BODY_KEYS.has(key)) {
                pushBody(field || "main", line);
                continue;
            }
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
                pendingBlank = 0;                       // 换标签：之前记的空行不跨字段
                cur[key] = cur[key] === undefined ? fm[2].trim() : `${cur[key]}\n${fm[2].trim()}`;
            }
            if (MP_BODY_KEYS.has(key)) inBody = true;
            field = key;
            continue;
        }
        if (!cur) { stray.push(line); continue; }       // 段头前的普通正文
        pushBody(field || "main", line);
    }
    if (!out.segs.length && stray.length) {             // 无段头=单段（正文原样作主体）
        openSeg();
        cur.main = stray.join("\n");
    } else if (stray.length) {
        out.notes.push(`忽略了 ${stray.length} 行出现在首个段头之前的内容`);
    }
    return out;
}

/** 工作台状态 -> 分段文本（【段N】+ 时长/独立镜头/参考/意图/剧本/提示词）。 */
function mpRenderState(state) {
    const blocks = [];
    for (let i = 0; i < state.length; i++) {
        const seg = state[i] || {};
        const rows = [`【段${i + 1}】`];
        /* 旧四框已下线：导出不再写场景/角色/环境音/配乐（解析仍兼容旧文本，见 parseMasterPrompt） */
        if (Number.isFinite(Number(seg.seconds)) && Number(seg.seconds) > 0) rows.push(`时长：${seg.seconds}`);
        if (seg.unlink) rows.push("独立镜头：是");
        if (Array.isArray(seg.refs) && seg.refs.length) rows.push(`参考：${seg.refs.join("，")}`);
        /* 意图 / 剧本：AI 扩写链路的输入与中间稿，与最终提示词一起导出，
           贴回（或交给外部 AI 续改）时三段都能还原。 */
        const intent = String(seg.intent || "").trim();
        if (intent) rows.push(`意图：\n${intent}`);
        const script = String(seg.script || "").trim();
        if (script) rows.push(`剧本：\n${script}`);
        /* 提示词正文按 H3 官方格式（三字段 / Ref2VA 六段），自身含空行——
           标签独占一行、正文从下一行开始，贴回时按续行原样归入 main。 */
        const main = String(seg.main || "").trim();
        rows.push(main ? `提示词：\n${main}` : "提示词：");
        blocks.push(rows.join("\n"));
    }
    return blocks.join("\n\n") + "\n\n【完】";
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
        /* 旧四框已下线：导出不再写场景/角色/环境音/配乐（解析仍兼容旧文本，见 parseMasterPrompt） */
        if (Number.isFinite(Number(seg.seconds)) && Number(seg.seconds) > 0) rows.push(`时长：${seg.seconds}`);
        if (!segAutoRef(seg)) rows.push("独立镜头：是");
        if (Array.isArray(seg.refs) && seg.refs.length) rows.push(`参考：${seg.refs.join("，")}`);
        /* 意图 / 剧本：AI 扩写链路的输入与中间稿，与最终提示词一起导出，
           贴回（或交给外部 AI 续改）时三段都能还原。 */
        const intent = String(seg.intent_zh || "").trim();
        if (intent) rows.push(`意图：\n${intent}`);
        const script = String(seg.script || "").trim();
        if (script) rows.push(`剧本：\n${script}`);
        /* 提示词正文按 H3 官方格式（三字段 / Ref2VA 六段），自身含空行——
           标签独占一行、正文从下一行开始，贴回时按续行原样归入 main。 */
        const main = String(prompts[i] ?? "").trim();
        rows.push(main ? `提示词：\n${main}` : "提示词：");
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
    /* P3：粘贴的参考标签同时接受 alias 与 asset_id（与 getDs 白名单同口径） */
    const poolList0 = Array.isArray(ds.ref_assets) ? ds.ref_assets : [];
    const labels = new Set(poolList0.map((a) => a && (a.label || a.asset_id)).filter(Boolean));
    ds.prompts = p.segs.map((s) => (s.main === undefined ? "" : s.main));
    ds.segments = p.segs.map((s, i) => {
        const base = (i < old.length && old[i] && typeof old[i] === "object") ? { ...old[i] } : defaultSegment();
        if (s.scene !== undefined) base.scene_prompt = s.scene;
        if (s.character !== undefined) base.character_prompt = s.character;
        if (s.soundscape !== undefined) base.soundscape = s.soundscape;
        if (s.music !== undefined) base.music = s.music;
        if (s.intent !== undefined) base.intent_zh = s.intent;
        if (s.script !== undefined) base.script = s.script;
        if (s.seconds !== undefined) base.seconds = s.seconds;
        if (s.unlink !== undefined) {
            base.unlink = s.unlink;
            base.auto_ref = s.unlink ? false : true;
        }
        if (s.refs !== undefined) {
            const valid = s.refs.filter((r) => labels.has(r));
            const dropped = s.refs.filter((r) => !labels.has(r));
            if (dropped.length) p.notes.push(`段${i + 1} 参考标签不存在已剔除：${dropped.join("、")}（先在「素材与参考」上传素材获得标签）`);
            base.refs = valid;
        }
        return base;
    });
    setDs(node, ds);
    /* 提示词是整段替换进来的（AI 优化产物只有官方三/六字段），对齐指令不会跟着来：
     * 按每段的首尾帧锚补回去，否则贴一次总提示词就把全链的首尾帧锚冲掉。
     * 没有锚的段 resync 内部直接返回，不会打扰手写文本。 */
    for (let i = 0; i < ds.prompts.length; i++) resyncAlignmentLines(node, i);
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
/* 放大目标尺寸模式（与后端 upscale.py SIZE_MODES 同表） */
const UP_SIZE_MODES = ["倍率", "目标尺寸", "百万像素"];

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
function upTargetCanvas(node, up) {
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
    const even = (x) => { const v = Math.max(2, Math.floor(x)); return v + v % 2; };
    const mode = up.size_mode || "倍率";
    if (mode === "目标尺寸") {
        const tw = Math.max(64, Number(up.target_w ?? 1280));
        const th = Math.max(64, Number(up.target_h ?? 704));
        return `${even(tw / 16) * 16}×${even(th / 16) * 16}`;
    }
    if (mode === "百万像素") {
        const mp = Number(up.megapixels ?? 1.0);
        const target = mp * 1024 * 1024;
        const aspect = w / h;
        const hPx = (target / aspect) ** 0.5;
        const wPx = hPx * aspect;
        return `${even(wPx / 16) * 16}×${even(hPx / 16) * 16}`;
    }
    const scale = Number(up.scale ?? 2.0);
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
    /* P3：全局链接条目不进画布镜像（LoadImage 只认 input 目录，写 images/… 会挂红；
     * 导演台面板经 library_file 正常预览，执行期走 store 寻址） */
    const local = (Array.isArray(ds.ref_assets) ? ds.ref_assets : []).filter((a) => a && !a.asset_id);
    const byKind = (k) => local.filter((a) => a.kind === k);
    // 去模式门控：镜像按数据点亮（标注资产优先于旧槽位，与后端汇合口径一致）
    const roleFile = (role) => {
        const a = local.find((x) => Array.isArray(x.roles) && x.roles.includes(role));
        return a ? String(a.file || "") : "";
    };
    setMirrorNode("首帧图", ds.first_frame || roleFile("首帧图"), ["image"]);
    setMirrorNode("目标尾帧图", ds.end_frame || roleFile("尾帧图"), ["image"]);
    setMirrorNode("尾帧图", ds.last_frame || "", ["image"]);   // 旧槽位兼容（已迁移则为空）
    const imgs = byKind("image");
    for (let i = 0; i < 9; i++) {
        setMirrorNode(`参考图·${i + 1}`, imgs[i] ? String(imgs[i].file) : "", ["image"]);
    }
    const vids = byKind("video");
    for (let i = 0; i < 3; i++) {
        const f = vids[i] ? String(vids[i].file) : "";
        setMirrorNode(`参考视频·${i + 1}`, f, ["file"]);
        setMirrorNode(`拆分视频·${i + 1}`, f);   // 与 LoadVideo 同步点亮/隐藏（hidden=断链）
    }
    const auds = byKind("audio");
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

/* pickAsset 已删除：旧三槽位上传入口随资产标注化下线，上传改由「素材库」承担。 */

/* 插入视频段前端已彻底删除（功能合并为 src.kind="video" 的 anchor）。
 * 旧 stub appendInsert/pickInsertVideo/removeInsert 随规划 §7 删除清单一并移除，
 * 无调用点残留（插入视频 UI 此前已下掉），不再保留兼容壳。 */

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
const _segTab = new Map();   // segIdx -> 'main'|'v2'|'set'（切换式段卡记忆，不持久化）
/* 待落盘写回（与 _taTimers 同 key）：切项目时同步 flush 进旧项目，
 * 防止 350ms 窗内的按键写到新项目（跨项目污染）或丢失 */
const _taPending = new Map();
/* UI 代际：switchProject 每次 +1；跨代到期的 debounce 写回直接丢弃 */
let _uiGen = 0;

function debouncePromptWrite(node, idx, text) {
    const key = `p${idx}`;
    const old = _taTimers.get(key);
    if (old) clearTimeout(old);
    const gen = _uiGen;
    _taTimers.set(key, setTimeout(() => {
        _taTimers.delete(key);
        _taPending.delete(key);
        if (gen !== _uiGen) return;   // 切项目后到期的写回直接丢弃（已 flush 过）
        setPromptText(node, idx, text);
    }, 350));
    _taPending.set(key, () => setPromptText(node, idx, text));
}

function debounceSegmentWrite(node, idx, field, text) {
    const key = `s${idx}_${field}`;
    const old = _taTimers.get(key);
    if (old) clearTimeout(old);
    const gen = _uiGen;
    _taTimers.set(key, setTimeout(() => {
        _taTimers.delete(key);
        _taPending.delete(key);
        if (gen !== _uiGen) return;
        setSegmentField(node, idx, field, text);
    }, 350));
    _taPending.set(key, () => setSegmentField(node, idx, field, text));
}

/* 同步刷掉所有挂起的输入框写回（切项目前调用：按键进旧项目，一个不丢） */
/** 丢掉某段待写的提示词（防抖中 + 待 flush 的闭包）：
 *  程序化改正文（插/删绿框）前必须清掉，否则几百毫秒后旧文本会把改动盖回去
 *  —— 表现就是"点了绿框/素材，状态闪一下又变回去"。 */
function cancelPromptWrite(idx) {
    const key = `p${idx}`;
    const t = _taTimers.get(key);
    if (t) { clearTimeout(t); _taTimers.delete(key); }
    _taPending.delete(key);
}

function flushPendingEdits() {
    if (!_taPending.size && !_taTimers.size) return 0;
    const jobs = [..._taPending.values()];
    _taPending.clear();
    for (const t of _taTimers.values()) {
        try { clearTimeout(t); } catch (e) { /* 忽略 */ }
    }
    _taTimers.clear();
    let n = 0;
    for (const fire of jobs) {
        try { fire(); n++; } catch (e) { /* 单条失败不阻断其余 */ }
    }
    return n;
}

/* ---- prompt_v2 分组写回（5.1）：段卡分组表单 -> ds.segments[idx].prompt_v2 ----
 * mutate(pv) 原地改可变副本，写回后 setDs + 防抖落盘（经 save_prompts.segments[].prompt_v2）。
 * pv 缺失时从旧三字段迁移出一份（H3Prompts.ensurePromptV2），保证分组永远可编辑。 */
function getSegPromptV2(node, idx) {
    const ds = getDs(node);
    const seg = (ds.segments || [])[idx];
    if (!seg) return null;
    if (seg.prompt_v2 && typeof seg.prompt_v2 === "object") return seg.prompt_v2;
    try {
        if (window.H3Prompts?.ensurePromptV2) return window.H3Prompts.ensurePromptV2(seg);
    } catch (e) { /* 回落 */ }
    return { shots: [{ description: "" }] };
}

function setPromptV2Field(node, idx, mutate, opts) {
    const ds = getDs(node);
    if (idx < 0 || idx >= (ds.segments || []).length) return false;
    const seg = ds.segments[idx];
    let pv = (seg.prompt_v2 && typeof seg.prompt_v2 === "object") ? seg.prompt_v2 : null;
    if (!pv) {
        try {
            pv = window.H3Prompts?.ensurePromptV2
                ? window.H3Prompts.ensurePromptV2(seg) : { shots: [{ description: "" }] };
        } catch (e) { pv = { shots: [{ description: "" }] }; }
        seg.prompt_v2 = pv;
    }
    try { mutate(pv); } catch (e) { console.warn("[h3-director] prompt_v2 mutate failed:", e); return false; }
    setDs(node, ds);
    if (!opts || opts.flush !== false) schedulePromptFlush();
    return true;
}

function debouncePromptV2Write(node, idx, key, value) {
    const tkey = `pv${idx}_${key}`;
    const old = _taTimers.get(tkey);
    if (old) clearTimeout(old);
    const gen = _uiGen;
    _taTimers.set(tkey, setTimeout(() => {
        _taTimers.delete(tkey);
        _taPending.delete(tkey);
        if (gen !== _uiGen) return;
        setPromptV2Field(node, idx, (pv) => { pv[key] = value; });
    }, 350));
    _taPending.set(tkey, () => setPromptV2Field(node, idx, (pv) => { pv[key] = value; }));
}

/* ---- 具象化两模式（FL2VA 首尾帧 / Ref2VA 全参考） ----
 * 默认跟随导演台：多参→Ref2VA，其余→FL2VA（无帧时 FL2VA 编译即退化为纯文本三字段，与 T2VA 等效）。
 * seg.v2mode = null 即自动（跟随），手动选定后存段级，随 ds 持久化。 */
const V2_MODES = ["FL2VA", "Ref2VA"];

/* details 开合状态记忆：卡片任何操作后都会全量重建 DOM，新建的 details 默认收起，
 * 于是不记住开合就会表现为「点＋对白后莫名切走」「点完按钮面板自己合上」。
 * key 带段号（必要时带镜号）避免不同段/镜互相串。 */
const _v2Open = new Map();
function v2Details(key, cls, summaryHtml, defOpen) {
    const g = el("details", cls);
    g.open = _v2Open.has(key) ? _v2Open.get(key) : !!defOpen;
    g.innerHTML = `<summary>${summaryHtml}</summary>`;
    g.addEventListener("toggle", () => _v2Open.set(key, g.open));
    return g;
}

/** 由「分段素材调度」算出本段应有的官方引用标签（按 kind 分别编号 <Picture/Video/Audio k>）。 */
function v2RefsFromSchedule(ds, segIdx) {
    const seg = ((ds && ds.segments) || [])[segIdx] || {};
    const pool = (ds && ds.ref_assets) || [];
    const cnt = { image: 0, video: 0, audio: 0 };
    const out = [];
    for (const label of (seg.refs || [])) {
        const a = pool.find((x) => x && (x.label === label || x.asset_id === label));
        const kind = (a && KIND_LIST.includes(a.kind)) ? a.kind : "image";
        cnt[kind] = (cnt[kind] || 0) + 1;
        out.push({ label: `<${KIND_TOKEN[kind]} ${cnt[kind]}>`, src: label });
    }
    return out;
}

/* 注：这里**不**做 seg.refs → pv.references 的自动同步。
 * 具象化是独立于主提示词的微调工具：它的引用只由它自己的参考组管理，
 * 主框三栏（意图/剧本/结果）的引用不会写进来。想导入主框的调度结果，
 * 用参考组里的「按素材调度重建」显式按钮——用户主动才发生。 */
const V2_MODE_ZH = { FL2VA: "首尾帧", Ref2VA: "全参考" };
const V2_TASK_TYPES = ["keyframe completion", "reference generation", "video editing",
    "video continuation", "audio reuse", "audio reference"];
const V2_TASK_ZH = { "keyframe completion": "关键帧补全", "reference generation": "参考生成",
    "video editing": "视频编辑", "video continuation": "视频续写",
    "audio reuse": "音频复用", "audio reference": "音频参考" };
const V2_MARKER_ZH = { fully_preserved: "完全保留", partially_preserved: "部分保留",
    attribute_transfer: "特征迁移", weak_reference: "弱参考",
    fully_copy: "完全复制", partially_copy: "部分复制", reference: "参考" };
/* 五模式中文名（自动判定提示用；手动只开放 FL2VA / Ref2VA） */
const V2_MODE_ZH_ALL = { T2VA: "纯文本", I2VA: "首帧", FL2VA: "首尾帧", L2VA: "尾帧", Ref2VA: "全参考" };
/* 运镜三维中英对照（显示中文、存英文，后端拼英文正文） */
const V2_CAM_ZH = { "Zoom In": "推近变焦", "Zoom Out": "拉远变焦", "Push In": "前推",
    "Pull Out": "后拉", "Pan Left": "左摇", "Pan Right": "右摇",
    "Truck Left": "左横移", "Truck Right": "右横移", "Tilt Up": "上摇", "Tilt Down": "下摇",
    "Pedestal Up": "升机", "Pedestal Down": "降机", "Arc Shot": "环绕拍摄",
    "Tracking Shot": "跟踪拍摄", "Static Shot": "固定镜头", "Shake Slightly": "轻微晃动",
    "Shake Strongly": "强烈晃动", "POV": "主观视角", "Roll Clockwise": "顺时针翻滚",
    "Roll Counterclockwise": "逆时针翻滚" };
const V2_AMP_ZH = { "": "（空）", small: "小幅度", large: "大幅度" };
const V2_SPD_ZH = { "": "（空）", slow: "慢速", fast: "快速" };

/* ---- 显示中文 ⇄ 存储英文（面板只做中文浏览/微调，后端永远收英文） ---- */
function zhSpeaker(v) {
    const m = /^S(\d+)$/i.exec(String(v ?? "").trim());
    return m ? `说话人${m[1]}` : String(v ?? "");
}
function enSpeaker(v) {
    let m = /^说话人(\d+)$/.exec(String(v ?? "").trim());
    if (m) return `S${m[1]}`;
    m = /^S(\d+)$/i.exec(String(v ?? "").trim());
    if (m) return `S${m[1]}`;
    return String(v ?? "").trim().slice(0, 16) || "S1";
}
function zhLang(v) {
    const t = String(v ?? "").trim().toLowerCase();
    if (t === "chinese") return "中文";
    if (t === "english") return "英文";
    return String(v ?? "");
}
function enLang(v) {
    const t = String(v ?? "").trim().toLowerCase();
    if (t === "中文" || t === "chinese") return "Chinese";
    if (t === "英文" || t === "english") return "English";
    return String(v ?? "").trim().slice(0, 16) || "Chinese";
}
function zhTasks(arr) {
    return (arr || []).map((t) => V2_TASK_ZH[t] || t).join("、");
}
function enTasks(str) {
    const zh2en = {};
    for (const [en, zh] of Object.entries(V2_TASK_ZH)) zh2en[zh] = en;
    return String(str ?? "").split(/[,，、;；+＋]+/).map((x) => x.trim())
        .filter(Boolean).map((x) => zh2en[x] || x).slice(0, 8);
}

/** 本段有没有首帧锚 / 尾帧锚 —— 全项目唯一口径，必须与节点实跑一致
 *  （nodes.py:1923 起：段级 frame_img 优先，其次全局 first_frame/end_frame 只对首/末段生效）。
 *  凡是要算 has_start / has_end 的地方都走这里，别再各写一遍。 */
function segHasFrames(ds, segIdx) {
    const seg = (ds?.segments || [])[segIdx] || {};
    const fi = (seg.frame_img && typeof seg.frame_img === "object") ? seg.frame_img : {};
    const nP = (ds?.prompts || []).length;
    return {
        has_start: !!(String(fi.first || "").trim() || (ds?.first_frame && segIdx === 0)),
        has_end: !!(String(fi.end || "").trim() || (ds?.end_frame && segIdx === nP - 1)),
    };
}

function defaultV2Mode(ds, segIdx) {
    /* 必须与后端 prompts.detect_mode 同口径，否则前端传过去的 mode 与后端
     * 自动判定不一致，会多报一条 W_MODE_OVERRIDE 警告，模板也会选错。
     * 直接用 H3Prompts.detectMode（它就是 detect_mode 的镜像），**入参也要
     * 跟后端一致**：pv 走 ensurePromptV2 —— 段未启用具象化时它会
     * migrateLegacySeg，把主框的 seg.refs 迁成 references，于是这类段会
     * 正确判成 Ref2VA。
     * 此前这里只读 seg.prompt_v2，没启用时 pv=null → 漏判：前端 T2VA、
     * 后端 Ref2VA，既报警告又用错模板（三段式 vs 六段式）。
     * 段**已启用**具象化时 ensurePromptV2 返回它自己的 prompt_v2，
     * 主框引用不参与判定 —— 三栏引用照样互不串味。 */
    const seg = (ds?.segments || [])[segIdx] || {};
    const { has_start: hasStart, has_end: hasEnd } = segHasFrames(ds, segIdx);
    const HP = window.H3Prompts || {};
    if (HP.detectMode && HP.ensurePromptV2) {
        const pv = HP.ensurePromptV2(Object.assign({}, seg, { prompt: (ds?.prompts || [])[segIdx] || "" }));
        return HP.detectMode(pv, { has_start: hasStart, has_end: hasEnd });
    }
    /* 兜底（H3Prompts 未加载）：与 detect_mode 同规则 */
    const pv = seg.prompt_v2 && typeof seg.prompt_v2 === "object" ? seg.prompt_v2 : null;
    const nRefs = pv ? (pv.references || []).length + (pv.subjects || []).length
        : (Array.isArray(seg.refs) ? seg.refs.length : 0);
    if (nRefs) return "Ref2VA";
    if (hasStart && hasEnd) return "FL2VA";
    if (hasStart) return "I2VA";
    if (hasEnd) return "L2VA";
    return "T2VA";
}

/** 本段生效模式：手动值合法即用，否则跟随导演台默认 */
function effV2Mode(ds, segIdx) {
    const seg = (ds?.segments || [])[segIdx];
    const manual = seg?.v2mode;
    if (V2_MODES.includes(manual)) return manual;
    return defaultV2Mode(ds, segIdx);
}

function setV2Mode(node, idx, mode) {
    const v = V2_MODES.includes(mode) ? mode : null;
    setSegmentField(node, idx, "v2mode", v);
    scheduleRefresh(80);
}

/** 官方对齐指令行（与后端 prompts.py 同口径：FL2VA 单句双锚、L2VA 末帧句、
 *  I2VA keyframe_line；时长一律取本段 seconds）。无锚时返回空数组。 */
function v2InstrLines(mode, seconds, hasStart, hasEnd, nShots) {
    const dur = (Number(seconds) || 5.0).toFixed(2);
    const n = Math.max(1, Number(nShots) || 1);
    if (mode === "T2VA" || mode === "Ref2VA") return [];
    if (mode === "FL2VA" && hasStart && hasEnd) {
        return [`How the reference pictures align with the target video — `
            + `Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; `
            + `Picture 2 (from Shot ${n}) aligns with the ${dur}-second mark of the target video.`];
    }
    if ((mode === "L2VA" || mode === "FL2VA") && hasEnd) {
        return [`How the reference pictures align with the target video — `
            + `<Picture 1> (from [Shot ${n}]) aligns with the ${dur}-second mark of the target video.`];
    }
    if ((mode === "I2VA" || mode === "FL2VA") && hasStart) {
        return [`For the target video, at 0.00 seconds into the target video, `
            + `<Picture 1> (from [Shot 1]) is fully referenced.`];
    }
    return [];
}

/** 面板里的对齐指令预览：v2InstrLines 为空时给人话解释，不返回指令。 */
function v2InstrPreview(mode, seconds, hasStart, hasEnd, nShots) {
    if (mode === "T2VA") return "无对齐指令（纯文本起片，直接写三核心字段）";
    const lines = v2InstrLines(mode, seconds, hasStart, hasEnd, nShots);
    if (!lines.length) {
        /* 不再是"必须上传首尾帧"：没有锚就按纯文本起片，这是合法模式。
         * 上传后这里会自动补出对齐行。 */
        return mode === "FL2VA"
            ? "未检测到首帧/尾帧图，本次按纯文本起片（无对齐指令）；上传后自动补对齐行"
            : "无对齐指令（本段没有可用的首尾帧锚，纯文本起片）";
    }
    return lines.join("\n");
}

/* ---- AI提示词优化（自研后端 /h3chain/optimize，轻量移植测试分支能力） ----
 * 主框=最终文本；优化结果双写主框+回填v2（可解析字段才回填，失败只写主框）。
 * 配置存 ds.optimizer，历史存 ds.opt_hist（原稿/优化稿可切），与测试分支键名一致。 */
const _optBefore = new Map();
const _optAfter = new Map();
const _optShown = new Map();
let _optBusy = null;
const _optRulesCache = { loaded: false, files: {} };

function optDefaultSettings() {
    return {
        mode: "api", provider: "runninghub",
        api_url: "https://www.runninghub.cn/openapi/v2", api_key: "", api_keys: {},
        model: "openai/gpt-5.6-sol", provider_models: {}, protocol: "openai",
        read_media: true, output_language: "中文",
        local_model: "", local_mmproj: "", local_device: "cuda",
        max_tokens: 4096, auto_optimize: false, rule_file: "auto",
    };
}

function optGetSettings(node) {
    const ds = getDs(node);
    const saved = ds && ds.optimizer && typeof ds.optimizer === "object" ? ds.optimizer : null;
    return Object.assign(optDefaultSettings(), saved || {});
}

function optSaveSettings(node, settings) {
    const ds = getDs(node);
    ds.optimizer = Object.assign({}, settings);
    setDs(node, ds);
}

function optTaskForMode(ds, idx) {
    // 去模式跟随：有段引用即 Ref2VA，否则 FL2VA（与 defaultV2Mode 同口径）
    if (typeof ds === "string") return ds === "多参视频" ? "Ref2VA" : ds === "首帧视频" ? "FL2VA" : "T2VA";
    /* 与 defaultV2Mode 同口径（首尾帧实际有无 → FL2VA/I2VA/L2VA/T2VA），
     * 别再写死 FL2VA —— 优化模板会跟着选错。 */
    return defaultV2Mode(ds, idx);
}

async function optImageToDataUrl(url) {
    try {
        const res = await fetch(url);
        if (!res.ok) return null;
        const blob = await res.blob();
        if (!blob.type.startsWith("image/")) return null;
        return await new Promise((resolve, reject) => {
            const fr = new FileReader();
            fr.onload = () => resolve(String(fr.result || ""));
            fr.onerror = () => reject(fr.error);
            fr.readAsDataURL(blob);
        });
    } catch (e) { return null; }
}

/** 收集本段要喂给 AI 的图：先首尾帧锚（结构锚，模型必须先看见起点与终点），
 *  再是本段引用的素材。统一按 <Picture N> 顺序编号。
 *  以前每张图的 label 都写死成 <picture>：多图时系统提示里出现重复标签，
 *  模型分不清谁是谁，也就谈不上"参考首帧写一条运动路径"。
 *  首尾帧取自段级 frame_img，其次全局 first_frame / end_frame（与 segHasFrames 同口径）。
 *  返回 { media, note }，note 是给模型的角色说明。 */
async function collectSegMedia(node, ds, idx) {
    const seg = (ds.segments || [])[idx] || {};
    const fi = (seg.frame_img && typeof seg.frame_img === "object") ? seg.frame_img : {};
    const nP = (ds.prompts || []).length;
    const firstFile = String(fi.first || "").trim()
        || (idx === 0 ? String(ds.first_frame || "").trim() : "");
    const endFile = String(fi.end || "").trim()
        || (idx === nP - 1 ? String(ds.end_frame || "").trim() : "");
    const picks = [];
    if (firstFile) picks.push({ file: firstFile, asset_id: "", role: "本段首帧（0.00s 起点锚）" });
    if (endFile) picks.push({ file: endFile, asset_id: "", role: "本段尾帧（终点锚）" });
    const pool = ds.ref_assets || [];
    for (const key of ((Array.isArray(seg.refs) ? seg.refs : []).slice(0, 8))) {
        const hit = pool.find((a) => a && (a.label === key || a.asset_id === key));
        if (!hit || hit.kind !== "image") continue;
        picks.push({ file: hit.file, asset_id: hit.asset_id || "", role: `参考素材「${hit.label}」` });
    }
    const media = [];
    const notes = [];
    for (const asset of picks) {
        if (media.length >= 8) break;
        const dataUrl = await optImageToDataUrl(
            assetPreviewUrl(getDirValue(node), asset.file, asset.asset_id));
        if (!dataUrl) continue;
        const tag = `<Picture ${media.length + 1}>`;
        media.push({ kind: "image", label: tag, images: [dataUrl] });
        notes.push(`${tag} = ${asset.role}`);
    }
    return { media, note: notes.length ? `随图说明：${notes.join("；")}。` : "" };
}

async function optFetchRuleFiles() {
    if (_optRulesCache.loaded) return _optRulesCache.files;
    try {
        if (!window.H3Api?.getPromptRules) return {};
        const r = await window.H3Api.getPromptRules();
        _optRulesCache.files = (r.body?.files && typeof r.body.files === "object") ? r.body.files : {};
    } catch (e) { _optRulesCache.files = {}; }
    _optRulesCache.loaded = true;
    return _optRulesCache.files;
}

/* AI输出字段回填v2（宽松解析四/六字段，解析失败返回false，调用方只写主框） */
function applyAiToV2(node, idx, text) {
    try {
        const t = String(text || "");
        if (!t.trim()) return false;
        const getField = (name) => {
            const m = t.match(new RegExp("(?:^|\\n)" + name + "\\s*:\\s*\\n([\\s\\S]*?)(?=\\n[a-z_]+\\s*:\\s*\\n|\\n*$)"));
            return m ? m[1].trim() : "";
        };
        const detailed = getField("detailed_description") || getField("integrated_multimodal_description");
        const soundscape = getField("overall_soundscape");
        const music = getField("non_diegetic_music");
        const summary = getField("summary");
        if (!detailed && !soundscape && !music) return false;
        return setPromptV2Field(node, idx, (pv) => {
            if (detailed) {
                pv.shots = pv.shots || [];
                if (!pv.shots[0]) pv.shots[0] = {};
                pv.shots[0].description = detailed.slice(0, 2000);
            }
            if (soundscape) pv.soundscape = soundscape.slice(0, 2000);
            if (music) pv.non_diegetic_music = music.slice(0, 500);
            if (summary) pv.summary_override = summary.slice(0, 500);
        });
    } catch (e) { return false; }
}

function optPersist(node, idx, shown) {
    try {
        const ds = getDs(node);
        if (!ds.opt_hist || typeof ds.opt_hist !== "object") ds.opt_hist = {};
        const key = `${node.id}:${idx}`;
        ds.opt_hist[String(idx)] = {
            before: _optBefore.get(key) ?? null,
            after: _optAfter.get(key) ?? null,
            shown: shown || "after",
        };
        setDs(node, ds);
    } catch (e) { /* 持久失败不阻断 */ }
}

function optRestoreMaps(node, ds) {
    try {
        const hist = (ds || {}).opt_hist || {};
        for (const [k, h] of Object.entries(hist)) {
            if (!h || typeof h !== "object") continue;
            const key = `${node.id}:${k}`;
            if (!_optBefore.has(key) && h.before != null) _optBefore.set(key, h.before);
            if (!_optAfter.has(key) && h.after != null) _optAfter.set(key, h.after);
            if (!_optShown.has(key) && h.shown) _optShown.set(key, h.shown);
        }
    } catch (e) { /* 忽略 */ }
}

async function runOptForSegment(node, idx, ta, ui, srcTa) {
    if (_optBusy && _optBusy.node === node && _optBusy.idx === idx) return;
    /* srcTa = 优化源（② 剧本框）；缺省即用 ta（③ 结果框自身）。
     * 结果只写回 ta，源保持不动，方便换参数反复重优化。 */
    const src = srcTa || ta;
    const before = src ? src.value : "";
    if (!before || !before.trim()) { alert("提示词为空，无需优化"); return; }
    let settings;
    try { settings = optGetSettings(node); }
    catch (e) { alert(`加载优化配置失败：${e.message}`); return; }
    if (settings.mode === "local" && !settings.local_model) { openOptSettings(node); return; }
    if (settings.mode !== "local" && !settings.api_key) { openOptSettings(node); return; }
    _optBusy = { node, idx };
    if (ui.btn) { ui.btn.textContent = "优化中…"; ui.btn.disabled = true; }
    const clearBusy = () => {
        if (ui.btn) { ui.btn.textContent = "✨ 优化"; ui.btn.disabled = false; }
        if (_optBusy && _optBusy.node === node && _optBusy.idx === idx) _optBusy = null;
    };
    try {
        const ds = getDs(node);
        const seg = (ds.segments || [])[idx] || {};
        const secs = seg.seconds || 5;
        /* 图 = 首尾帧锚 + 本段参考素材（collectSegMedia 统一编号并给出角色说明） */
        const mm = settings.read_media !== false
            ? await collectSegMedia(node, ds, idx) : { media: [], note: "" };
        await optFetchRuleFiles();
        const body = {
            /* 角色说明只进请求、不落库：优化器会像规则文件一样把它消费掉，
             * 原稿（before）与写回的 prompt 都不受影响。 */
            prompt: mm.note ? `${mm.note}\n${before}` : before,
            task: optTaskForMode(ds, idx), duration: Number(secs) || 5,
            media: mm.media, context: { main_mode: optTaskForMode(ds, idx) }, config: settings,
        };
        if (!window.H3Api?.optimize) throw new Error("h3_api.js 未更新（缺 optimize）");
        const r = await window.H3Api.optimize(body);
        if (!r.body?.ok) throw new Error(window.H3Api.errText(r, "优化失败"));
        const result = String(r.body.prompt || "").trim() || before;
        const key = `${node.id}:${idx}`;
        _optBefore.set(key, before);
        _optShown.set(key, "after");
        setPromptText(node, idx, result);
        applyAiToV2(node, idx, result);
        /* 优化器只产出官方三/六字段，**不含**关键帧对齐指令（那是编译产物，
         * 由 resyncAlignmentLines 按本段首尾帧锚现算）。直接整段写回就把
         * "Picture 1 aligns with the 0.00-second mark…" 冲掉了——模型于是失去
         * 首尾帧锚。写完立刻按当前锚点补回，再把屏幕上的编辑器同步到最终文本。 */
        resyncAlignmentLines(node, idx);
        const finalText = String((getDs(node).prompts || [])[idx] ?? result);
        if (ta) ta.value = finalText;
        /* 原稿/优化稿切换存的是**最终文本**（含补回的对齐指令），
         * 否则切回"优化稿"又把对齐行弄丢一次。 */
        _optAfter.set(key, finalText);
        optPersist(node, idx, "after");
        if (ui.reset) { ui.reset.style.display = ""; ui.reset.textContent = "原稿"; }
        if (ui.name) ui.name.textContent = settings.mode === "local"
            ? `本地: ${String(settings.local_model || "").split(/[\\/]/).pop() || "未选"}`
            : (settings.model || "").split("/").pop() || "API";
        /* 悬空引用告警：正文写了 <Picture N>/<Subject N>，但本段一张图都没送进模型。
         * 可能是模型照抄了剧本里的标签，也可能是手写的 —— 不说破的话要等出片
         * 才发现人物/场景全变了。 */
        const usedTags = (String(finalText).match(/<(?:Picture|Subject|Video|Audio)\s+\d+>/g) || []).length;
        if (usedTags > 0 && (!mm.media || mm.media.length === 0)) {
            setLed("warn", `已回填，但正文里有 ${usedTags} 处 <Picture/Subject/...> 引用，`
                + "而本段没有挂任何素材 —— 这些标签是悬空的，生成时不会有图。"
                + "请到「引用素材」挂上素材，或删掉这些标签。");
        } else {
            setLed("done", "优化已回填主框＋具象化（对齐指令已按首尾帧锚补回）");
        }
        scheduleRefresh(200);
    } catch (e) {
        alert(`提示词优化失败：${e.message || e}`);
    } finally { clearBusy(); }
}

function optToggle(node, idx, ta, ui) {
    const key = `${node.id}:${idx}`;
    const shown = _optShown.get(key);
    if (shown === "after") {
        const before = _optBefore.get(key);
        if (before == null) return;
        if (ta) ta.value = before;
        setPromptText(node, idx, before);
        _optShown.set(key, "before");
        optPersist(node, idx, "before");
        if (ui.reset) ui.reset.textContent = "优化稿";
    } else if (shown === "before") {
        const after = _optAfter.get(key);
        if (after == null) return;
        if (ta) ta.value = after;
        setPromptText(node, idx, after);
        _optShown.set(key, "after");
        optPersist(node, idx, "after");
        if (ui.reset) ui.reset.textContent = "原稿";
    }
}

/* 服务商预设（与后端 optimizer.py PROVIDERS 对齐：url/model/protocol） */
const OPT_PROVIDERS = {
    runninghub: { label: "RunningHub 国内版（推荐）", url: "https://www.runninghub.cn/openapi/v2", model: "openai/gpt-5.6-sol", protocol: "openai" },
    runninghub_overseas: { label: "RunningHub 海外版", url: "https://www.runninghub.ai/openapi/v2", model: "openai/gpt-5.6-sol", protocol: "openai" },
    openai: { label: "OpenAI", url: "https://api.openai.com/v1", model: "gpt-4.1-mini", protocol: "openai" },
    gemini: { label: "Google Gemini", url: "https://generativelanguage.googleapis.com/v1beta", model: "gemini-2.5-flash", protocol: "gemini" },
    openrouter: { label: "OpenRouter", url: "https://openrouter.ai/api/v1", model: "google/gemini-2.5-flash", protocol: "openai" },
    dashscope: { label: "阿里云百炼", url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-vl-max", protocol: "openai" },
    siliconflow: { label: "SiliconFlow", url: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen2.5-VL-72B-Instruct", protocol: "openai" },
    custom: { label: "自定义", url: "", model: "", protocol: "openai" },
};

/* 提示词优化设置面板（结构对齐参考项目，请求仍走自研 /h3chain 后端）。
 * Key 随导演台状态保存，分享前请清空。 */
async function openOptSettings(node, onSaved) {
    let current;
    try { current = optGetSettings(node); }
    catch (e) { current = optDefaultSettings(); }
    try {
        if (window.H3Api?.getOptimizerConfig) {
            const r = await window.H3Api.getOptimizerConfig();
            if (r.body?.ok) {
                current = Object.assign({}, current);
                if (Array.isArray(r.body.models)) current._models = r.body.models;
                if (Array.isArray(r.body.mmproj_models)) current._mmproj = r.body.mmproj_models;
            }
        }
    } catch (e) { /* 用本地值 */ }
    if (document.querySelector(".h3d-opt-overlay")) return;
    const overlay = el("div", "h3d-opt-overlay");
    const dialog = el("div", "h3d-opt-dialog");
    overlay.append(dialog);
    dialog.append(el("div", "h3d-opt-title", "提示词优化 · 功能设置"));
    dialog.append(el("div", "h3d-opt-sub",
        "自研后端（云 API / 本地模型双通道）+ prompt/*.txt 规则注入。Key 随导演台状态保存，分享前请清空。"));
    /* 未选本地模型时的引导提示：选择模型后自动消失 */
    const guide = el("div", "h3d-opt-guide",
        "尚未选择本地视觉模型：请在下方「本地模型」里选择一个（GGUF 还需选视觉投影 mmproj），"
        + "点「保存」后回到段落即可点「优化」。");
    guide.style.display = current.local_model ? "none" : "";
    dialog.append(guide);

    const row = (label, control) => {
        const wrap = el("label", "h3d-opt-row");
        wrap.append(el("span", "", escapeHtml(label)), control);
        dialog.append(wrap);
        return wrap;
    };

    const mode = el("select", "");
    mode.append(new Option("在线 API", "api"), new Option("本地视觉模型", "local"));
    mode.value = current.mode || "api";

    const provider = el("select", "");
    for (const [value, preset] of Object.entries(OPT_PROVIDERS)) provider.append(new Option(preset.label, value));
    provider.value = OPT_PROVIDERS[current.provider] ? current.provider : "runninghub";

    /* 分服务商记忆 Key 与模型（切换服务商不丢已填值，随 ds.optimizer 持久化） */
    const apiKeys = { ...(current.api_keys || {}) };
    if (current.api_key && !apiKeys[current.provider || "runninghub"]) apiKeys[current.provider || "runninghub"] = current.api_key;
    const providerModels = { ...(current.provider_models || {}) };
    if (current.model && !providerModels[current.provider || "runninghub"]) providerModels[current.provider || "runninghub"] = current.model;

    const key = el("input", ""); key.type = "password"; key.value = apiKeys[provider.value] || "";
    key.placeholder = "sk-…";
    const url = el("input", ""); url.type = "text";
    const model = el("input", ""); model.type = "text";
    model.placeholder = "如 openai/gpt-5.6-sol";
    const protocol = el("select", "");
    protocol.append(new Option("OpenAI 兼容", "openai"), new Option("OpenAI Responses", "responses"), new Option("Gemini", "gemini"));

    const localModel = el("select", "");
    const mmproj = el("select", "");
    const device = el("select", "");
    device.append(new Option("Auto", "auto"), new Option("GPU", "cuda"), new Option("CPU", "cpu"));
    device.value = current.local_device || "cuda";
    const maxTokens = el("input", ""); maxTokens.type = "number";
    maxTokens.min = "512"; maxTokens.max = "8192"; maxTokens.step = "512";
    maxTokens.value = String(Math.max(512, Math.min(8192, Number(current.max_tokens) || 4096)));

    const language = el("div", "h3d-opt-language");
    for (const value of ["中文", "English"]) {
        const lb = el("label", ""); const rd = el("input", ""); rd.type = "radio";
        rd.name = `h3d-opt-lang-${node?.id ?? "x"}`; rd.value = value;
        rd.checked = (current.output_language || "中文") === value;
        lb.append(rd, el("span", "", value)); language.append(lb);
    }
    const readMedia = el("input", ""); readMedia.type = "checkbox"; readMedia.checked = current.read_media !== false;
    const ruleSel = el("select", "");
    for (const [value, label] of [["auto", "自动（中文→自定义中文版 / 其他→自定义英文版）"],
        ["minimaxh3_custom_ref2v_prompt_writing_zh.txt", "自定义中文版"],
        ["minimaxh3_custom_ref2v_prompt_writing.txt", "自定义英文版"],
        ["minimaxh3_official_ref2v_prompt_writing.txt", "官方版（六字段·英文输出）"],
        ["none", "不注入（用后端内置规则）"]]) ruleSel.append(new Option(label, value));
    ruleSel.value = current.rule_file || "auto";
    const autoOptimize = el("input", ""); autoOptimize.type = "checkbox"; autoOptimize.checked = !!current.auto_optimize;

    const rowMode = row("优化模式", mode);
    const rowProv = row("服务商", provider);
    const rowKey = row("API Key", key);
    const rowUrl = row("API URL", url);
    const rowModel = row("模型", model);
    const rowProto = row("协议", protocol);
    const rowLocal = row("本地模型", localModel);
    const rowMmproj = row("视觉投影 mmproj", mmproj);
    const rowDevice = row("本地设备", device);
    row("最大输出 token", maxTokens);
    const langRow = el("label", "h3d-opt-row"); langRow.append(el("span", "", "输出语言"), language); dialog.append(langRow);
    const ruleRow = el("label", "h3d-opt-row"); ruleRow.append(el("span", "", "提示词规则"), ruleSel); dialog.append(ruleRow);
    const checks = el("div", "h3d-opt-checks");
    const chk = (t, c) => { const lb = el("label", ""); lb.append(c, el("span", "", t)); checks.append(lb); };
    chk("读取视觉参考（图片转 dataURL，最多 8 张）", readMedia);
    chk("运行前自动优化提示词", autoOptimize);
    dialog.append(checks);
    const refreshIcon = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7a5 5 0 0 1 4.9 4H20a8 8 0 0 0-2.35-4.65ZM12 17a5 5 0 0 1-4.9-4H4a8 8 0 0 0 8 7v3l5-5-5-5v4Z"/></svg>';
    const refreshModels = el("button", "h3d-btn h3d-opt-refresh", refreshIcon);
    refreshModels.type = "button"; refreshModels.title = "重新扫描本地模型列表（调 /h3chain/optimizer-config）";
    const refreshRow = el("label", "h3d-opt-row"); refreshRow.append(el("span", "", "刷新模型"), refreshModels); dialog.append(refreshRow);

    let models = Array.isArray(current._models) ? current._models : [];
    let mmprojModels = Array.isArray(current._mmproj) ? current._mmproj : [];

    const fillLocal = () => {
        const cur = localModel.value || current.local_model || "";
        localModel.replaceChildren();
        for (const item of models) localModel.append(new Option(item.name || item.relative_path, item.relative_path));
        if ([...localModel.options].some((o) => o.value === cur)) localModel.value = cur;
        if (!localModel.options.length) localModel.append(new Option("未找到可用本地视觉模型", ""));
        const sel = models.find((m) => m.relative_path === localModel.value);
        const gguf = !!sel && sel.format === "gguf";
        /* 只有 GGUF 才需要手动选 mmproj（transformers 目录自带投影） */
        rowMmproj.classList.toggle("h3d-opt-hidden", !gguf);
        if (gguf) {
            const mmCur = mmproj.value || current.local_mmproj || "";
            mmproj.replaceChildren();
            mmproj.append(new Option("（选择视觉投影）", ""));
            for (const m of mmprojModels) mmproj.append(new Option(m.name, m.relative_path));
            if ([...mmproj.options].some((o) => o.value === mmCur)) mmproj.value = mmCur;
        }
    };
    fillLocal();

    const sync = () => {
        const local = mode.value === "local";
        const custom = provider.value === "custom";
        rowKey.classList.toggle("h3d-opt-hidden", local);
        rowUrl.classList.toggle("h3d-opt-hidden", local || !custom);
        rowModel.classList.toggle("h3d-opt-hidden", local || !custom);
        rowProto.classList.toggle("h3d-opt-hidden", local || !custom);
        rowLocal.classList.toggle("h3d-opt-hidden", !local);
        refreshRow.classList.toggle("h3d-opt-hidden", !local);
        rowDevice.classList.toggle("h3d-opt-hidden", !local);
        const sel = models.find((m) => m.relative_path === localModel.value);
        rowMmproj.classList.toggle("h3d-opt-hidden", !local || !(sel && sel.format === "gguf"));
        const preset = OPT_PROVIDERS[provider.value];
        if (!custom && preset) { url.value = preset.url; model.value = preset.model; protocol.value = preset.protocol; }
        void rowMode; void rowProv;
    };
    if (!url.value) url.value = current.api_url || "";
    if (!model.value) model.value = current.model || "";
    protocol.value = ["openai", "responses", "gemini"].includes(current.protocol) ? current.protocol : "openai";
    provider.addEventListener("change", () => {
        const prev = provider.dataset.prev || current.provider || "runninghub";
        apiKeys[prev] = key.value;
        providerModels[prev] = model.value;
        key.value = apiKeys[provider.value] || "";
        sync();
        /* 非自定义服务商切回预设模型（用户改过则保留记忆值） */
        if (provider.value !== "custom") {
            model.value = providerModels[provider.value] || OPT_PROVIDERS[provider.value]?.model || "";
        }
    });
    mode.addEventListener("change", sync);
    localModel.addEventListener("change", () => { fillLocal(); sync(); guide.style.display = localModel.value ? "none" : ""; });
    refreshModels.onclick = async () => {
        refreshModels.disabled = true;
        try {
            if (!window.H3Api?.getOptimizerConfig) throw new Error("h3_api.js 未更新（缺 getOptimizerConfig）");
            const r = await window.H3Api.getOptimizerConfig();
            if (!r.body?.ok) throw new Error(window.H3Api.errText(r, "刷新失败"));
            models = Array.isArray(r.body.models) ? r.body.models : [];
            mmprojModels = Array.isArray(r.body.mmproj_models) ? r.body.mmproj_models : [];
            fillLocal(); sync();
        } catch (e) { alert(e.message); }
        finally { refreshModels.disabled = false; }
    };
    provider.dataset.prev = provider.value;
    sync();

    const actions = el("div", "h3d-opt-actions");
    const cancel = el("button", "h3d-btn", "取消");
    const save = el("button", "h3d-btn h3d-opt-save", "保存");
    actions.append(cancel, save); dialog.append(actions);
    const close = () => overlay.remove();
    cancel.onclick = close;
    overlay.addEventListener("pointerdown", (ev) => { if (ev.target === overlay) close(); });
    save.onclick = () => {
        const outLang = language.querySelector("input:checked")?.value || "中文";
        const preset = OPT_PROVIDERS[provider.value];
        apiKeys[provider.value] = key.value;
        providerModels[provider.value] = model.value;
        const mt = Math.max(512, Math.min(8192, Number(maxTokens.value) || 4096));
        const body = {
            mode: mode.value, provider: provider.value,
            api_url: provider.value === "custom" ? url.value.trim() : (preset?.url || url.value.trim()),
            api_key: key.value.trim(), api_keys: { ...apiKeys },
            model: provider.value === "custom" ? model.value.trim() : (model.value.trim() || preset?.model || ""),
            provider_models: { ...providerModels },
            protocol: provider.value === "custom" ? protocol.value : (preset?.protocol || protocol.value),
            read_media: readMedia.checked, output_language: outLang,
            local_model: localModel.value, local_mmproj: mmproj.value, local_device: device.value,
            rule_file: ruleSel.value,
            max_tokens: mt, auto_optimize: autoOptimize.checked,
        };
        if (body.mode === "local" && !body.local_model) { alert("请先选择一个本地视觉模型"); return; }
        if (body.mode === "api" && (!body.api_key || !body.model)) { alert("请填写 API Key 与模型名"); return; }
        optSaveSettings(node, body);
        close();
        if (onSaved) onSaved(body);
        scheduleRefresh(120);
    };
    document.body.append(overlay);
}

/* 主框工具条：优化/设置/原稿切换/从具象化同步（双写入口） */
function paintOptbar(optbar, node, data, idx, ta) {
    try {
        optbar.replaceChildren();
        optRestoreMaps(node, data.ds);
        const key = node ? `${node.id}:${idx}` : "";
        const shown = key ? _optShown.get(key) : null;
        /* 优化入口已移到 ② 剧本区（剧本 → 结果），这里只留原稿切换与双向同步 */
        const bReset = el("button", "h3d-btn", shown === "before" ? "优化稿" : "原稿");
        bReset.title = "原稿 ⇄ 优化稿切换";
        bReset.style.display = shown ? "" : "none";
        const bFromV2 = el("button", "h3d-btn", "← 从具象化同步");
        bFromV2.title = "把具象化分组编译成官方文本，覆盖结果框";
        const bToV2 = el("button", "h3d-btn", "同步到具象化 →");
        bToV2.title = "把结果框文本解析回具象化分组，方便逐项微调后再同步回来";
        bToV2.onclick = () => {
            try {
                const text = String(ta && ta.value ? ta.value : "").trim();
                if (!text) { alert("结果框为空，没有可同步的内容"); return; }
                applyAiToV2(node, idx, text);
                flushPrompts(node);
                setLed("idle", `第 ${idx + 1} 段结果已同步到具象化`);
                scheduleRefresh(80);
            } catch (e) { alert(`同步到具象化失败：${e.message || e}`); }
        };
        const ui = { btn: null, reset: bReset, name: null };
        bReset.onclick = () => optToggle(node, idx, ta, ui);
        bFromV2.onclick = async () => {
            try {
                const ds = getDs(node);
                const fseg = (ds.segments || [])[idx] || {};
                /* 没有「启用」按钮了：没存过 prompt_v2 就现场从旧字段迁一份来编译
                 * （与具象化页编辑即启用的行为一致，不再拦一道） */
                const pv = (fseg.prompt_v2 && typeof fseg.prompt_v2 === "object")
                    ? fseg.prompt_v2
                    : (window.H3Prompts?.ensurePromptV2 ? window.H3Prompts.ensurePromptV2(fseg) : null);
                if (!pv) { alert("本段没有可编译的内容"); return; }
                const fr = segHasFrames(ds, idx);
                const r = await window.H3Api.compilePreview({
                    prompt: pv, seconds: Number(fseg.seconds) || 5.0,
                    has_start: fr.has_start, has_end: fr.has_end,
                    mode: effV2Mode(ds, idx) });
                if (!r.body?.compiled?.prompt_text) throw new Error(window.H3Api.errText(r, "编译无文本"));
                ta.value = r.body.compiled.prompt_text;
                setPromptText(node, idx, ta.value);
                scheduleRefresh(200);
            } catch (e) { alert(`同步失败：${e.message || e}`); }
        };
        optbar.append(bReset, bFromV2, bToV2);
    } catch (e) { console.warn("[h3-director] paintOptbar failed:", e); }
}

/* ---- 段落计划（状态驱动：仅提示词段，不含序章）。
 *  插入视频段（ds.inserts）前端已废弃：plan 不再混排，后端解析/存档保留，
 *  旧项目数据不丢失；后续 latent 注入框架在分段设置里重做。 ---- */

function planFromDs(node) {
    const ds = getDs(node);
    const prompts = ds.prompts.map((t, i) => ({ text: t.trim(), idx: i }));
    const plan = prompts.map((p) => ({ kind: "prompt", ...p }));
    const drafts = prompts.filter((p) => !p.text).map((p) => `段 ${p.idx + 1}`);
    return { plan, drafts, ds };
}

/** 拖拽调序纯函数：把提示词段 dragIdx 移到「插入线」处，返回新 prompts 序
 *  （null = 非法或原位不动）。ins = 线在当前 strip/plan 的位置（0..plan.length）。
 *  规则：新序位 k = 线上方其余提示词段数（其余段保持相对序，仅拖拽段换位）。 */
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
    _selSeg = k;   // 选中跟随被拖段
    setLed("idle", `段序已调整：从首个变化段起自动级联重做（换位段的接缝需重新生成）`);
    scheduleRefresh(60);
}

/* 选中段（横向 strip 用，纯内存，不落 ds）：_selSeg = prompts 下标。
 * 增删/调序后由各入口钳位或跟随；渲染时越界自动收敛到末段。 */
let _selSeg = 0;

function clampSelSeg(n) {
    const total = Number(n);
    if (!Number.isFinite(total) || total <= 0) { _selSeg = 0; return 0; }
    if (_selSeg < 0) _selSeg = 0;
    if (_selSeg >= total) _selSeg = total - 1;
    return _selSeg;
}

/** 指针 X 在横向 strip 中的插入线位置（0..pill 数）：越过某 pill 中点=线在该 pill 前 */
function dropPillIndexAt(strip, x) {
    const pills = [...strip.querySelectorAll(":scope > .h3d-segpill[data-seg]")];
    for (let i = 0; i < pills.length; i++) {
        const r = pills[i].getBoundingClientRect();
        if (x < r.left + r.width / 2) return i;
    }
    return pills.length;
}

/** strip 插入线吸附显示：只在「可落位」处画线（原位不动不显示），所见即所得 */
function setPillDropLine(strip, data, dragIdx, ins) {
    strip.querySelectorAll(".h3d-drop-before, .h3d-drop-after")
        .forEach((n) => n.classList.remove("h3d-drop-before", "h3d-drop-after"));
    const n = (data.ds?.prompts || []).length;
    const perm = reorderChain([...Array(n).keys()], data.plan, dragIdx, ins);
    if (!perm) return;
    const k = perm.indexOf(dragIdx);
    const pills = [...strip.querySelectorAll(":scope > .h3d-segpill[data-seg]")];
    if (pills[k]) pills[k].classList.add("h3d-drop-before");
    else if (pills.length) pills[pills.length - 1].classList.add("h3d-drop-after");
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
 *  ——与后端 _parse_redo_segs 同口径（未完成/序章/越界/已禁用不重摇） */
function redoSlotValid(slot, mf, plan, ds) {
    const off = planOff(mf);
    const done = Math.max(0, (mf?.done ?? 0) - off);
    const i = slot - off;
    if (!(i >= 0 && i < done && i < (plan || []).length && plan[i]?.kind === "prompt")) return false;
    const seg = (ds?.segments || [])[plan[i].idx];
    return segAutoSeq(seg);
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
        auto_ref: (s.auto_ref === null || s.auto_ref === undefined) ? null : !!s.auto_ref,
        auto_seq: (s.auto_seq === null || s.auto_seq === undefined) ? null : !!s.auto_seq,
        refs: Array.isArray(s.refs) ? s.refs.map(String) : [],
        frame_refs: Array.isArray(s.frame_refs) ? s.frame_refs.map(String) : null,
        /* v2：具象化结构透存（后端 clean_prompt 收敛）；latent 策略透存防洗掉；
           v2mode 手动模式透存（后端白名单收敛） */
        prompt_v2: (s.prompt_v2 && typeof s.prompt_v2 === "object") ? s.prompt_v2 : undefined,
        latent_save: (s.latent_save && typeof s.latent_save === "object") ? s.latent_save : undefined,
        latent_ref: (s.latent_ref && typeof s.latent_ref === "object") ? s.latent_ref : undefined,
        tail_src: (s.tail_src && typeof s.tail_src === "object") ? s.tail_src : undefined,
        /* 手动锚定：随 save_prompts 一起提交（ds.segments[i].anchors）；
         * 数组才收（含空数组），对象才写——沿用现有保存调用，不新造接口 */
        anchors: Array.isArray(s.anchors) ? s.anchors : undefined,
        /* 段级首尾帧参考图（提示词框按钮选的项目内图片） */
        frame_img: (s.frame_img && typeof s.frame_img === "object") ? {
            first: String(s.frame_img.first || "").trim().replace(/\\/g, "/"),
            end: String(s.frame_img.end || "").trim().replace(/\\/g, "/"),
        } : undefined,
        v2mode: V2_MODES.includes(s.v2mode) ? s.v2mode : null,
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
 *  序章项目的 prompts[0] 是「序章（上传视频）」占位（对应起始视频，不是提示词框），
 *  载入时跳过。切项目三件套：先把 350ms 窗内的按键同步刷进旧项目（防跨项目污染），
 *  再回写旧项目，最后换代际（连续快切时过期流程直接退出，不互相覆盖）。 */
async function switchProject(dir) {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点（只读模式）"); return; }
    const myGen = ++_uiGen;
    const stale = () => myGen !== _uiGen;
    flushPendingEdits();                                   // 按键先进旧项目，一个不丢
    clearTimeout(promptFlushTimer);                        // 取消挂起的批量防抖（已同步刷过）
    const oldDir = getDirValue(node);
    if (oldDir && oldDir !== dir) {
        try { await flushPrompts(node, oldDir); } catch (e) {
            console.warn("[h3-director] 切项目前回写旧项目失败：", e);
        }
    }
    if (stale()) return;
    if (!setDirValue(node, dir)) { alert("节点上没有「存档目录/断点目录」控件"); return; }
    setWidgetValue(node, W_REROLL, 0);
    const r = await apiGet(`/h3chain/project?dir=${encodeURIComponent(dir)}`);
    if (stale()) return;
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
            /* 资产库按项目绑定：池子以本项目 manifest["assets"] + asset_links 为准
             * （上传/入库/调入时已同步登记），切项目即换池，不再跨项目共享。
             * 与 hydratePool 共用同一构造函数 —— 两处口径不同就会出现「切了项目
             * 引用栏不换」的偏差。 */
            ds.ref_assets = poolFromManifest(mf, await fetchAssetLinks(dir));
            if (stale()) return;
            // 槽位索引态必须清：重摇标记/二采勾选按槽位存，带到新项目会重做错段
            ds.redo_segs = [];
            if (ds.upscale) ds.upscale.include = [];
            setDs(node, ds);
        } else if (mf) {
            // 空项目（无段落无提示词）：必须清空旧池，否则旧项目内容残留显示
            const ds = getDs(node);
            ds.prompts = [];
            ds.segments = [];
            ds.inserts = [];
            ds.ref_assets = [];
            ds.redo_segs = [];
            if (ds.upscale) ds.upscale.include = [];
            setDs(node, ds);
        }
        if (stale()) return;
        if (mf) {
            setLed("idle", `已读档「${mf.title || dir}」（${mf.total ? `${mf.done ?? 0}/${mf.total} 段` : "草稿，未配置段落"}）`);
        } else {
            setLed("idle", `已指向 ${dir}`);
        }
    } else {
        setLed("idle", `已指向 ${dir}`);
    }
    // 直接全刷一次（不等 200ms 轮询，消灭“来不及更新”）
    try { await refresh(); } catch (e) { /* refresh 内部已兜底 */ }
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
    if (!total) return { text: "暂无段落：点下方「＋」添加", next: false };
    if (done >= total) return { text: `✓ ${total}/${total} 已完成`, next: false };
    if (!done) return { text: `共 ${total} 段 · 待生成段 1`, next: true };
    return { text: `段 ${done}/${total} 已完成 · 将生成段 ${done + 1}`, next: true };
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

/* 段片轻量信息（瘦身卡片用）：只回 URL，不在卡内嵌大播放器，点击走全屏浮层 */
function segMediaInfo(state, mf, idx) {
    const done = mf?.done ?? 0;
    const g = idx + planOff(mf);
    if (g >= done || !state?.dir) return null;
    const sub = `h3_projects/${state.dir}`;
    const videoFile = (mf.videos || [])[g];
    const thumbFile = (mf.thumbs || [])[g];
    if (!videoFile && !thumbFile) return null;
    return {
        videoUrl: videoFile ? viewUrl(sub, videoFile) : "",
        thumbUrl: thumbFile ? viewUrl(sub, thumbFile) : "",
        file: videoFile || thumbFile || "",
    };
}

/* 全屏浮层浏览段片（视频/图片二选一，Esc/点背景关） */
function openSegViewer(info, title) {
    try {
        if (!info || (!info.videoUrl && !info.thumbUrl)) return;
        const overlay = el("div", "h3d-viewer");
        const box = el("div", "h3d-viewer-box");
        const head = el("div", "h3d-viewer-head");
        head.append(el("span", "", escapeHtml(title || "段片预览")));
        const close = el("button", "h3d-close", "✕");
        close.title = "关闭（Esc）";
        close.onclick = () => overlay.remove();
        head.append(close);
        box.append(head);
        if (info.videoUrl) {
            const v = document.createElement("video");
            v.controls = true; v.autoplay = true; v.preload = "metadata";
            v.src = info.videoUrl;
            if (info.thumbUrl) v.poster = info.thumbUrl;
            box.append(v);
            const foot = el("div", "h3d-viewer-foot");
            const dl = el("a", "h3d-dl", "⬇ 下载视频");
            dl.href = info.videoUrl; dl.download = info.file || "segment.mp4";
            foot.append(dl);
            box.append(foot);
        } else {
            const im = document.createElement("img");
            im.src = info.thumbUrl; im.alt = title || "";
            box.append(im);
        }
        overlay.append(box);
        overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) overlay.remove(); });
        overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") overlay.remove(); });
        document.body.append(overlay);
        close.focus();
    } catch (e) { console.warn("[h3-director] openSegViewer failed:", e); }
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
    .h3d-statusbar{display:flex;align-items:center;gap:8px;padding:4px 8px;border:1px solid #2c3a4a;border-radius:6px;background:#1e242c;line-height:1.4;font-size:11.5px;color:var(--h3d-muted)}
    .h3d-statusbar .h3d-st-text{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .h3d-statusbar .h3d-chip{align-self:center;font-size:10px;padding:1px 7px}
    .h3d-statusbar .h3d-btn{padding:2px 8px;font-size:11px}
    .h3d-refresh{padding:2px 8px;flex:none;font-size:11px}
    /* ---- 横向分段选择条（状态条下方）：pill 先压缩、极限后横滑 ---- */
    .h3d-segstrip{display:flex;gap:6px;margin:8px 0 10px;overflow-x:auto;padding:2px 1px 6px;scrollbar-width:thin}
    .h3d-segpill{flex:1 1 0;min-width:44px;max-width:120px;display:inline-flex;align-items:center;justify-content:center;gap:4px;padding:5px 8px;border:1px solid #3a352c;border-radius:14px;background:#1b1a16;color:#a8a294;cursor:pointer;font-size:11.5px;font-family:inherit;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;user-select:none;transition:border-color .12s,background .12s}
    .h3d-segpill:hover{border-color:#46604f;color:#d9d4c9}
    .h3d-segpill.on{border-color:#316dca;background:#1f2f45;color:#9ecbff;font-weight:700}
    .h3d-segpill.done{border-color:#2f6e57}
    .h3d-segpill.done.on{border-color:#316dca}
    .h3d-segpill.next{border-color:#6e7b8c}
    .h3d-segpill.off{opacity:.5;border-style:dashed}
    .h3d-segpill.h3d-dragging{opacity:.35;border-style:dashed;border-color:var(--h3d-cyan)}
    .h3d-segpill.h3d-drop-before{box-shadow:-3px 0 0 0 var(--h3d-cyan)}
    .h3d-segpill.h3d-drop-after{box-shadow:3px 0 0 0 var(--h3d-cyan)}
    .h3d-segpill .dot{flex:none;width:6px;height:6px;border-radius:50%;background:#636e7b}
    .h3d-segpill.done .dot{background:var(--h3d-cyan)}
    .h3d-segpill.next .dot{background:#6e7b8c;animation:h3d-blink 1.2s infinite}
    .h3d-segadd{flex:none;min-width:34px}
    .h3d-rail{display:flex;gap:4px;margin:12px 0 14px}
    .h3d-rail span{flex:1;height:5px;border-radius:4px;background:#444c56}
    .h3d-rail span.done{background:var(--h3d-cyan)}
    .h3d-rail span.next{background:#6e7b8c;animation:h3d-blink 1.2s infinite}
    .h3d-rail span.unlink{background:repeating-linear-gradient(135deg,#e0823d 0 4px,#444c56 4px 8px)}
    .h3d-rail span.offchain{background:repeating-linear-gradient(90deg,#3a414c 0 3px,#22272e 3px 6px)}
    .h3d-cards{display:grid;gap:10px}
    .h3d-card{display:grid;grid-template-columns:minmax(0,1fr);gap:8px;padding:10px;border:1px solid #3f4854;border-radius:9px;background:#262c36}
    .h3d-card.todo{opacity:.72}
    .h3d-card.todo:hover{opacity:1}
    .h3d-card.mergeable{grid-template-columns:auto minmax(0,1fr);align-items:start}
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
    /* 富文本提示词框：@别名 渲染成内联绿框（原子节点，退格整块删） */
    .h3d-rta{display:block;min-height:84px;max-height:40vh;overflow:auto;white-space:pre-wrap;word-break:break-word;cursor:text}
    .h3d-rta:empty:before{content:attr(data-ph);color:#7d8695;pointer-events:none}
    .h3d-rta-off{color:#636e7b;cursor:not-allowed;background:#22272e}
    .h3d-rtag{display:inline-flex;align-items:center;gap:4px;margin:0 2px;padding:0 3px 0 4px;border:1px solid #2f6e57;border-radius:11px;background:#12291f;color:#7fe0b0;font-size:11.5px;line-height:1.75;white-space:nowrap;vertical-align:baseline;user-select:all}
    /* 绿框里的标识：缩略图（图/视首帧）或音符图标。pointer-events:none 防误拖。
     * outline 描一圈内缘（不占布局、不改变尺寸）—— 缩略图压在深色底上时边缘不发虚。 */
    .h3d-rtag>.h3d-thumb{width:15px;height:15px;border-radius:5px;object-fit:cover;background:#0d0c0a;flex:none;pointer-events:none;outline:1px solid #2f6e5799;outline-offset:-1px}
    .h3d-rtag>.h3d-kindmark{font-size:12px;line-height:1;flex:none;pointer-events:none;opacity:.95}
    .h3d-rtagx{display:inline-block;padding:0 5px;border-radius:9px;background:transparent;color:#7fe0b0cc;cursor:pointer;font-size:10.5px;line-height:1.6;user-select:none}
    .h3d-rtagx:hover{background:#3a1a1c;color:#f0a0a4}
    .h3d-fixfocus{font-size:11px;padding:3px 8px;opacity:.75}
    .h3d-fixfocus:hover{opacity:1}
    .h3d-zoneerr{font-size:11px;padding:3px 8px;border-radius:999px;cursor:help;
        color:#ffd7d7;background:rgba(255,80,80,.16);border:1px solid rgba(255,80,80,.45)}
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

    /* ---- 提示词优化设置面板（独立浮层，与通用 h3d-dialog 分开） ---- */
    .h3d-opt-overlay{position:fixed;inset:0;z-index:1000003;display:flex;align-items:center;justify-content:center;padding:20px;background:#0a0906d0;backdrop-filter:blur(10px)}
    .h3d-opt-dialog{width:min(680px,calc(100vw - 40px));max-height:88vh;overflow:auto;border:1px solid #464033;border-radius:14px;background:linear-gradient(160deg,#242019,#191712 62%);box-shadow:0 22px 64px #000b;padding:20px;color:var(--h3d-bone);font:13px/1.5 "Microsoft YaHei UI","Segoe UI",sans-serif}
    .h3d-opt-dialog *{box-sizing:border-box}
    .h3d-opt-title{font-size:16px;font-weight:700;margin-bottom:4px}
    .h3d-opt-sub{color:var(--h3d-muted);margin:0 0 12px;line-height:1.7;font-size:12px}
    .h3d-opt-guide{font-size:11.5px;color:#f0d98c;background:#2a2517;border:1px solid #5a4d1f;border-radius:7px;padding:7px 10px;margin:0 0 10px;line-height:1.6}
    .h3d-opt-row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:7px 0;font-size:12px}
    .h3d-opt-row>span{flex:none;width:96px;color:#9fb0bd}
    .h3d-opt-row input[type=text],.h3d-opt-row input[type=password],.h3d-opt-row select,.h3d-opt-row input[type=number]{flex:1 1 auto;min-width:0;border:1px solid #3a352c;border-radius:6px;background:#211f1a;color:var(--h3d-bone);padding:6px 8px;font-size:12px;outline:none;font-family:inherit}
    .h3d-opt-row input:focus,.h3d-opt-row select:focus{border-color:#a8d8bd}
    .h3d-opt-language{display:flex;gap:16px;flex:1}
    .h3d-opt-language label{display:flex;align-items:center;gap:5px;white-space:nowrap;color:#c8c2b4;cursor:pointer;font-size:12px}
    .h3d-opt-language input{accent-color:#7fc79f}
    .h3d-opt-checks{display:flex;gap:18px;flex-wrap:wrap;margin:9px 0 2px}
    .h3d-opt-checks label{display:flex;align-items:center;gap:6px;font-size:12px;color:#c8c2b4;cursor:pointer}
    .h3d-opt-checks input{accent-color:#7fc79f;width:14px;height:14px}
    .h3d-opt-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}
    .h3d-opt-hidden{display:none!important}
    .h3d-opt-refresh{flex:none;width:auto!important;padding:4px 9px;display:inline-flex;align-items:center}
    .h3d-opt-save{border:0;background:linear-gradient(135deg,#f0c274,#e6b566 55%,#d99e4a);color:#1a1408;font-weight:700}

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
    /* 数字输入一律隐藏上下小箭头（spinner）：卡片里宽度只有 58px，箭头占掉一半
     * 还吸附不到合法档位（17k+5 只能靠换算），纯碍眼。步进用键盘上下键仍可用。 */
    .h3d-secs::-webkit-inner-spin-button,.h3d-secs::-webkit-outer-spin-button,
    .h3d-fpsinput::-webkit-inner-spin-button,.h3d-fpsinput::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
    .h3d-secs,.h3d-fpsinput{-moz-appearance:textfield;appearance:textfield}
    .h3d-secs:focus{border-color:#a8d8bd}
    .h3d-secs-hint{color:var(--h3d-muted);font:10px ui-monospace,Consolas;white-space:nowrap}
    .h3d-ppane .h3d-refrow{margin-top:2px;}
/* 「设置」页分区：按作用面分块 + 小标题，块与块之间才有层级（原先平铺、权重一样） */
.h3d-setsec{display:flex;flex-direction:column;gap:7px;margin-top:10px}
.h3d-setsec-title{font-size:11px;font-weight:600;color:var(--h3d-copper);letter-spacing:.3px;border-bottom:1px solid #37332b;padding-bottom:4px}
.h3d-setrow{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center}
.h3d-refrow{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:4px;padding:7px 8px;border:1px solid #37332b;border-radius:7px;background:#181712}
    .h3d-refrow>label{color:var(--h3d-muted);font-size:10.5px;font-weight:600;flex:none}
    .h3d-refchip{display:inline-flex;gap:5px;align-items:center;padding:3px 8px 3px 3px;border:1px solid #3a352c;border-radius:12px;background:#25221c;color:#a8a294;cursor:pointer;font-size:11px;font-family:inherit}
    .h3d-refchip:hover{border-color:#46604f}
    .h3d-refchip.on{border-color:#2f6e57;background:#12291f;color:#7fe0b0}
    .h3d-refchip .h3d-thumb{width:20px;height:20px;border-radius:9px;object-fit:cover;background:#0d0c0a;flex:none;outline:1px solid #3a352c;outline-offset:-1px}
    .h3d-refchip.on .h3d-thumb{outline-color:#2f6e57}
    .h3d-reftpl{max-width:118px;border:1px solid #3a352c;border-radius:6px;background:#211f1a;color:var(--h3d-bone);padding:3px 4px;font-size:11px;outline:none;margin-left:auto}
    .h3d-reftpl:focus{border-color:#a8d8bd}
    .h3d-chipbtn{display:inline-flex;align-items:center;gap:5px;padding:2px 9px 2px 3px;border:1px solid #3a352c;border-radius:11px;background:#25221c;color:#a39d90;cursor:pointer;font-size:11px;font-family:inherit;max-width:240px}
    .h3d-chipbtn:hover{filter:brightness(1.25)}
    .h3d-chipbtn.on{border-color:#2f6e57;background:#12291f;color:#7fe0b0}
    /* chip 首位的标识：缩略图 18x18 圆角块（图 / 视频首帧），音频是音符图标。 */
    .h3d-chipbtn>.h3d-thumb{width:18px;height:18px;border-radius:7px;object-fit:cover;background:#0d0c0a;flex:none;outline:1px solid #3a352c;outline-offset:-1px}
    .h3d-chipbtn.on>.h3d-thumb{outline-color:#2f6e57}
    .h3d-chipbtn>.h3d-kindmark{font-size:14px;line-height:1;flex:none;padding:0 1px}
    .h3d-chipbtn-text{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
    .h3d-chipminus{padding:2px 6px;margin-left:-3px;border:1px solid #3a352c;border-radius:9px;background:#25221c;color:#a39d90;cursor:pointer;font-size:11px;line-height:1;font-family:inherit}
    .h3d-chipminus:hover{border-color:#9a4144;color:#f0a0a4}
    .h3d-chipminus.off{opacity:.4}
    .h3d-chipminus.off:hover{opacity:1}
    .h3d-frmbtns{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
    .h3d-anchorbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:5px;padding:6px 8px;border:1px solid #33302a;border-radius:7px;background:#191814}
    .h3d-anchorbar>label{color:var(--h3d-muted);font-size:10.5px;font-weight:600;flex:none}
    .h3d-frm{padding:2px 9px;border:1px solid #3a352c;border-radius:11px;background:#25221c;color:#a39d90;cursor:pointer;font-size:11px;font-family:inherit}
    .h3d-frm:hover{filter:brightness(1.25)}
    .h3d-frm.on{border-color:#316dca;background:#1f2f45;color:#9ecbff}
    .h3d-frm{display:inline-flex;align-items:center;gap:5px}
    .h3d-frmthumb{width:18px;height:18px;object-fit:cover;border-radius:3px;flex:0 0 auto;background:#0f1316}
    .h3d-frmpic{font-size:9px;padding:0 4px;border-radius:7px;background:rgba(49,109,202,.28);
        border:1px solid #316dca;color:#bcd9ff;line-height:14px}
    .h3d-frmmap{display:flex;flex-direction:column;gap:2px;margin:2px 0}
    .h3d-frmmap-row{font-size:11px;color:#9aa4ad}
    .h3d-frmmap-row b{color:#9ecbff;font-weight:600}
    .h3d-frmmap-row code{font-size:11px;color:#cfd6dd;background:rgba(255,255,255,.06);
        padding:0 4px;border-radius:3px}
    .h3d-frmgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px;max-height:46vh;overflow:auto;margin:8px 0 4px}
    .h3d-frmtile{display:flex;flex-direction:column;gap:4px;padding:5px;border:1px solid #332f27;border-radius:8px;background:#1b1a16;cursor:pointer;min-width:0}
    .h3d-frmtile:hover{border-color:#46604f}
    .h3d-frmtile.on{border-color:#316dca;box-shadow:0 0 0 1px #316dca inset}
    .h3d-frmtile img{width:100%;height:64px;object-fit:cover;border-radius:5px;background:#262319}
    .h3d-frmico{height:64px;display:grid;place-items:center;font-size:22px;opacity:.7}
    .h3d-frmname{font-size:10.5px;color:#cdd9e1;word-break:break-all;text-align:center}
    .h3d-hubrow{display:flex;gap:8px;flex-wrap:wrap}
    .h3d-hubbtn{flex:1;min-width:120px;padding:10px;border:1px solid #37332b;border-radius:9px;background:#181712;color:var(--h3d-bone);cursor:pointer;font-size:13px;font-family:inherit;text-align:center}
    .h3d-hubbtn:hover{border-color:var(--h3d-cyan)}
    .h3d-hubbtn small{display:block;color:var(--h3d-muted);font-size:10.5px;margin-top:2px}

    /* ---- 素材标签编辑 ---- */
    .h3d-labelinp{width:100%;border:1px solid #3a352c;border-radius:5px;background:#211f1a;color:var(--h3d-bone);padding:4px 6px;font:600 12px "Microsoft YaHei UI","Segoe UI",sans-serif;outline:none}
    .h3d-labelinp:focus{border-color:#a8d8bd;box-shadow:0 0 0 2px #8cc9a833}
    .h3d-quicklbl{display:flex;gap:4px;flex-wrap:wrap;margin-top:5px}
    .h3d-quicklbl button{padding:2px 7px;border:1px solid #3a352c;border-radius:10px;background:#25221c;color:#a39d90;cursor:pointer;font-size:10px;font-family:inherit}
    .h3d-quicklbl button:hover{border-color:#46604f;color:#d9d4c9}
    .h3d-asset-usage{margin-top:5px;display:flex;gap:4px;flex-wrap:wrap}

    /* ---- 伸缩框（foldBox：项目存档等折叠面板） ---- */
    .h3d-libfold{border:1px solid #37332b;border-radius:12px;background:#141310;overflow:hidden}
    .h3d-libfold>summary.h3d-libfoldsum{cursor:pointer;padding:13px 16px;font-size:14.5px;font-weight:700;list-style:none;user-select:none}
    .h3d-libfold>summary.h3d-libfoldsum::-webkit-details-marker{display:none}
    .h3d-libfold>summary.h3d-libfoldsum::before{content:"▸ ";color:var(--h3d-muted)}
    .h3d-libfold[open]>summary.h3d-libfoldsum::before{content:"▾ ";color:var(--h3d-cyan)}
    .h3d-libfoldbody{padding:4px 14px 14px;display:grid;gap:12px}

    /* ---- @ 补全弹层 ---- */
    .h3d-atpop{position:fixed;z-index:99999;min-width:180px;max-width:300px;max-height:240px;overflow:auto;background:#1b1a16;border:1px solid #46604f;border-radius:8px;padding:4px;box-shadow:0 6px 22px #000a}
    .h3d-atpop div{padding:5px 8px;border-radius:5px;cursor:pointer;font-size:12px;color:#d9d4c9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .h3d-atpop div small{color:var(--h3d-muted);margin-left:6px}
    .h3d-atpop div.on,.h3d-atpop div:hover{background:#24402f;color:#7fe0b0}

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
    /* 二采「目标尺寸」模式的条件字段容器：整行跨列 + 内部同款 2 列，
       字段布局与其他网格项一致；display 由 showUp() 在 ""/none 间切换 */
    .h3d-size-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;grid-column:1 / -1}
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

    /* ---- 总提示词工作台：三框（① 意图 → ② 剧本 → ③ 结果） ----
     * 三个框各自可拖右下角伸缩（resize 挂在 wrapper 上，wrapper 内部 textarea
     * flex 撑满，这样拖动的是整块而不是只有编辑区）；内容超出即框内滚轮滚动；
     * 三块加起来超过可视高度时，外层 stack 自己滚。 */
    .h3d-overlay-full{padding:0;place-items:stretch}
    .h3d-dialog-full{width:100vw;height:100vh;max-width:none;border:0;border-radius:0;display:flex;flex-direction:column;padding:14px 18px 12px;gap:8px}
    .h3d-dialog-full h3{margin:0}
    .h3d-dialog-full .h3d-lead{margin:0}
    .h3d-mpwork{flex:1;min-height:0;display:flex}
    .h3d-mpwork .h3d-mpta{height:100%;margin:0;flex:1}
    .h3d-mptop{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    .h3d-mpstack{flex:1;min-height:0;display:flex;flex-direction:column;gap:8px;overflow:auto;padding-right:2px}
    .h3d-mpboxwrap{flex:0 0 auto;display:flex;flex-direction:column;gap:4px;overflow:hidden;resize:vertical;min-width:0;border:1px solid #2a3438;border-radius:7px;background:#10161a;padding:7px 9px 8px}
    .h3d-mpboxwrap:focus-within{border-color:#6cb6ff}
    .h3d-mpboxwrap-1{height:20%;min-height:118px}
    .h3d-mpboxwrap-2{height:30%;min-height:170px}
    .h3d-mpboxwrap-3{height:46%;min-height:220px}
    .h3d-mplabel{display:flex;gap:8px;align-items:baseline;flex:none;font-size:11.5px}
    .h3d-mplabel b{color:var(--h3d-cyan);font-size:11.5px;font-weight:700}
    .h3d-mplabel small{color:var(--h3d-muted);font-size:10.5px;line-height:1.4;flex:1;min-width:0}
    .h3d-mpboxbtn{padding:2px 9px;font-size:11px;flex:none;align-self:center}
    .h3d-mpboxwrap textarea{flex:1;min-height:0;width:100%;resize:none;border:0;background:transparent;color:var(--h3d-bone);padding:0;font:12.5px/1.7 ui-monospace,Consolas,monospace;outline:none}
    .h3d-mpmode{color:var(--h3d-muted);font-size:10.5px;line-height:1.4;flex:1 1 100%;min-width:0}
    .h3d-mprefs{gap:8px}
    .h3d-mprefs-chips{display:flex;gap:6px;flex-wrap:wrap;flex:1;min-width:0}
    .h3d-mprefs-btn{padding:2px 8px;font-size:11px}
    /* 段卡：AI 优化设置条（三页之外的公共区，区别于「锚定设置」页的本段设置） */
    .h3d-optsetbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:2px 0 4px;
        padding:5px 8px;border:1px dashed #2a3438;border-radius:7px;background:#0f1418}
    .h3d-mpset{display:flex;gap:8px;flex-wrap:wrap;align-items:center;border:1px solid #2a3438;border-radius:7px;background:#10161a;padding:8px 10px;margin-bottom:8px}
    .h3d-mpset label{display:inline-flex;gap:4px;align-items:center;font-size:11.5px;color:var(--h3d-muted);white-space:nowrap}
    .h3d-mpset input[type=number]{width:56px;border:1px solid #2a3438;border-radius:5px;background:#1b2126;color:var(--h3d-bone);padding:3px 6px;font-size:12px;outline:none;font-family:inherit}
    .h3d-mpset input[type=number]:focus{border-color:#6cb6ff}
    .h3d-mpset select{border:1px solid #2a3438;border-radius:5px;background:#1b2126;color:var(--h3d-bone);padding:3px 6px;font-size:12px;outline:none;font-family:inherit}

    .h3d-fab{position:fixed;right:16px;top:120px;z-index:80;width:44px;height:44px;border-radius:50%;border:1px solid #46604f;background:#1f2a23;color:#c2e0cd;cursor:pointer;font-size:17px}
    .h3d-fab:hover{filter:brightness(1.2)}

    /* ---- 具象化提示词 v2 分组表单（5.1）：画面/镜头/声音/参考/高级 ---- */
    .h3d-v2panel{border-color:#2c4a52;background:#141d21}
    .h3d-v2panel.has-content{border-color:#316dca80;box-shadow:inset 0 0 0 1px #316dca26}
    .h3d-v2group{margin-top:6px;border:1px solid #2a3438;border-radius:7px;background:#10161a;overflow:hidden}
    .h3d-v2group>summary{padding:7px 10px;cursor:pointer;color:var(--h3d-cyan);font-size:11.5px;font-weight:700;user-select:none}
    .h3d-v2group>summary::-webkit-details-marker{display:none}
    .h3d-v2group>summary::before{content:"▸ ";font-size:10px}
    .h3d-v2group[open]>summary::before{content:"▾ "}
    .h3d-ppane{margin-bottom:8px}
    .h3d-ppane>.h3d-v2grid{padding:2px 8px 8px}
    .h3d-ppane .h3d-rta{min-height:150px;max-height:55vh}
    .h3d-shot-more{margin-top:6px}
    .h3d-v2grid{display:grid;gap:7px;padding:0 10px 10px}
    .h3d-v2f{width:100%;border:1px solid #2a3438;border-radius:6px;background:#1b2126;color:var(--h3d-bone);padding:6px 8px;font-size:12px;outline:none;font-family:inherit}
    .h3d-v2f:focus{border-color:#6cb6ff;box-shadow:0 0 0 2px #6cb6ff33}
    textarea.h3d-v2f{min-height:44px;resize:vertical;line-height:1.55}
    .h3d-v2row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
    .h3d-v2row .h3d-v2f{flex:1;min-width:0}
    .h3d-shot{border:1px solid #2a3438;border-radius:7px;padding:8px;background:#141b20;display:grid;gap:6px}
    .h3d-shot-head{display:flex;gap:8px;align-items:center;font-weight:700;font-size:12px}
    .h3d-v2out{margin:6px 0 0;padding:8px 10px;border-radius:7px;background:#0d1114;font:11px/1.6 ui-monospace,Consolas;white-space:pre-wrap;word-break:break-word;max-height:260px;overflow:auto}
    .h3d-v2out.err{border:1px solid #9a4144;color:#f0a0a4}
    .h3d-v2out.warn{border:1px solid #7a5f36;color:#e9c07a}
    .h3d-v2out.ok{border:1px solid #2f6e57;color:#7ee2a8}
    .h3d-verr{color:#f0a0a4;font-size:11px;line-height:1.6}
    .h3d-vwarn{color:#e9c07a;font-size:11px;line-height:1.6}

    /* ---- 切换式段卡（瘦身）：头一行 + Tab页，主框默认，设置收起 ---- */
    .h3d-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:2px 0 6px}
    .h3d-tab{padding:3px 10px;border:1px solid #3a352c;border-radius:12px;background:#1b1a16;color:#a8a294;cursor:pointer;font-size:11.5px;font-family:inherit}
    .h3d-tab.on{border-color:#316dca;background:#1f2f45;color:#9ecbff}
    .h3d-tabpane{display:grid;gap:7px}
    .h3d-previewbtn{padding:2px 8px;font-size:11px}
    /* ---- 全屏段片浮层 ---- */
    .h3d-viewer{position:fixed;z-index:1000003;inset:0;display:grid;place-items:center;padding:24px;background:#0a0906d0;backdrop-filter:blur(10px)}
    .h3d-viewer-box{width:min(960px,calc(100vw - 40px));max-height:calc(100vh - 40px);overflow:auto;border:1px solid #464033;border-radius:12px;background:#191712;padding:14px}
    .h3d-viewer-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-weight:700}
    .h3d-viewer-box video{width:100%;max-height:70vh;background:#000;border-radius:8px}
    .h3d-viewer-box img{width:100%;border-radius:8px}
    .h3d-viewer-foot{margin-top:8px;display:flex;gap:8px;justify-content:flex-end}

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
    /* 桌面端（Electron）偶发：窗口/画布抢走键盘焦点，输入框点进去打不了字。
     * 兜底按钮：把键盘焦点抢回来（正常情况也用不着，放着不碍事）。 */
    const fixFocus = el("button", "h3d-btn h3d-fixfocus", "⌨ 输入修复");
    fixFocus.title = "桌面端点不进输入框？点这里把键盘焦点抢回来（ComfyUI 桌面端偶发，"
        + "刷新页面也能解决）";
    fixFocus.onclick = () => {
        try { window.focus(); } catch (e) { /* 忽略 */ }
        try { document.body.focus(); } catch (e) { /* 忽略 */ }
        const canvas = document.querySelector("canvas#graph-canvas, canvas.litegraph");
        try { canvas && canvas.blur(); } catch (e) { /* 忽略 */ }
        const ed = desk?.page?.querySelector(".h3d-rta, textarea, input");
        if (ed) { try { ed.focus(); } catch (e) { /* 忽略 */ } }
    };
    const close = el("button", "h3d-close", "✕");
    close.title = "关闭导演台（Esc）";
    /* 分区渲染出错的可见出口：以前异常只进 console，用户看到的是"某个区空了"，
     * 无从判断是没数据还是代码挂了。这里把区名与错误一行摆到顶栏。 */
    const zoneErr = el("span", "h3d-zoneerr", "");
    zoneErr.style.display = "none";
    right.append(fixFocus, ledWrap, sub, zoneErr, close);
    topbar.append(left, right);

    /* 诊断横幅：项目存档接口未注册时显示（/h3chain/ping 探测失败） */
    const banner = el("div", "h3d-banner");
    banner.textContent = apiErrorText;
    banner.style.display = apiErrorText ? "" : "none";

    /* 主舞台三栏（侧栏各含两个固定分区，渲染器只填充分区内容） */
    const stage = el("div", "h3d-stage");
    const colL = el("aside", "h3d-col left");
    const lProj = el("section", "h3d-lsec h3d-projsec");
    colL.append(lProj);
    const colC = el("section", "h3d-col center");
    const cHead = el("div", "h3d-sechead",
        "<strong>段落流水线</strong><small>顶部横向选段（点选看一段，＋ 加段，pill 可拖调序）；✏ 改词 · 🎲 重摇 · 🎬 锚定设置</small>");
    const mpBtn = el("button", "h3d-btn h3d-mpbtn", "📋 总提示词");
    mpBtn.title = "多段工作台：每段 ① 中文意图 → ② 剧本（AI 扩写，中文自由格式）→ "
        + "③ 结果（AI 提示词优化，H3 官方格式）；支持时长范围与一次生成多段";
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
            lProj,
            rParams, rExp, rUpscale, rHist,
            footInfo, run,
            zoneErr,
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
        sel: _selSeg,
        plan: (plan || []).map((it) => ["p", it.text]),
        segs: (ds?.segments || []).map((s) => [s.scene_prompt ?? "", s.character_prompt ?? "",
                                               s.soundscape ?? "", s.music ?? "",
                                               s.seconds ?? 0, (s.refs || []).join(","),
                                               !!s.unlink, !!s.disabled,
                                               s.auto_ref ?? "f", s.auto_seq ?? "f",
                                               Array.isArray(s.frame_refs) ? s.frame_refs.join(",") : null,
                                               s.v2mode ?? "",
                                               /* v2 结构进签名：切换项目/外部写入后卡片重建；编辑中由焦点守卫防丢焦 */
                                               s.prompt_v2 ? JSON.stringify(s.prompt_v2) : "",
                                               s.latent_save ? JSON.stringify(s.latent_save) : "",
                                               s.latent_ref ? JSON.stringify(s.latent_ref) : "",
                                               s.tail_src ? JSON.stringify(s.tail_src) : ""]),
        /* 首尾帧图文件名也要进签名：上传/换图后段卡片的首尾帧 chips 行才会重建。
         * **段级 frame_img 必须一起进**（以前只放全局 first_frame/end_frame 两个旧槽位）：
         * 段级锚是现在唯一的入口，它一变签名却不变 → 段卡不重建 → 具象化面板顶部
         * 的对齐指令预览、锚点区、模式判定行全都停在旧状态，只有"切一下屏"才刷新，
         * 观感就是"清除了锚，具象化那里不跟着清除"。 */
        frames: [ds?.first_frame ?? "", ds?.end_frame ?? "",
            ...(ds?.segments || []).map(
                (s) => `${s?.frame_img?.first || ""}/${s?.frame_img?.end || ""}`)],
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

/* 分区渲染异常登记表（顶栏可见）：updateDesk 里每个区单独兜错，
 * 一个区挂掉不该让整台从此不再刷新 —— 以前的写法里，任何一区抛异常都会让
 * refresh() 整体失败，之后每次刷新都在同一个地方摔倒，用户看到的就是
 * "整个导演台崩掉了"（左栏空 + 中栏不更新 + 侧栏退化成报错卡）。 */
const _deskZoneErrs = new Map();
function runZone(name, fn) {
    try {
        const r = fn();
        if (_deskZoneErrs.delete(name)) paintZoneErrs();
        return r;
    } catch (e) {
        console.error(`[h3-director] 分区「${name}」渲染失败：`, e);
        _deskZoneErrs.set(name, String((e && e.message) || e).slice(0, 120));
        paintZoneErrs();
        return null;
    }
}
function paintZoneErrs() {
    const z = desk && desk.zones;
    if (!z || !z.zoneErr) return;
    if (!_deskZoneErrs.size) { z.zoneErr.style.display = "none"; z.zoneErr.textContent = ""; return; }
    const txt = [..._deskZoneErrs.entries()].map(([k, v]) => `${k}：${v}`).join(" ｜ ");
    z.zoneErr.textContent = `⚠ 渲染异常（${_deskZoneErrs.size}）`;
    z.zoneErr.title = `以下分区本次渲染出错，已跳过（其余分区照常刷新；详细见浏览器控制台）：\n${txt}`;
    z.zoneErr.style.display = "";
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

    /* 左栏：项目与链（含「素材库」入口，见 renderV2Section） */
    runZone("项目与链", () => renderLeftColumn(z.lProj, data));

    /* 中栏：状态条 + 进度轨 + 段落卡片（有编辑器/播放中时跳过重渲） */
    runZone("段落流水线", () => renderCenterColumn(z.colC, data));

    /* 右栏：链参数（编辑中不重建）+ 二采面板（编辑中不重建）+ 成片历史（播放中不重建） */
    const psig = paramsSig(node);
    const pgrid = z.rParams.querySelector(".h3d-params");
    if (!(pgrid && pgrid.contains(document.activeElement)) && z.rParams.dataset.sig !== psig) {
        z.rParams.dataset.sig = psig;
        runZone("链参数", () => renderParamsZone(z.rParams, data));
    }
    /* 实验性功能面板（ds.experiments 驱动；编辑动作经 setExp* -> repaintExperiments() 即时重渲，
     * 不走此守卫；此处守卫仅为保护「全局刷新时区内有输入框正在打字」不丢焦；defs 到达/失败/硬开关变化也触发。
     * !!node 必须进签名：面板曾在节点未就绪时渲染过「画布上未找到节点」，节点随后可用若
     * 签名不变则永不重建（experiments 全空时两头签名相同），须先做点别的操作才能显示 */
    const esig = JSON.stringify([data.ds?.experiments ?? {},
        EXP.defs ? EXP.defs.length : 0, EXP.forceDisabled, EXP.failed, !!data.node]);
    if (!z.rExp.contains(document.activeElement) && z.rExp.dataset.sig !== esig) {
        z.rExp.dataset.sig = esig;
        runZone("实验功能", () => renderExperimentsZone(z.rExp, data));
    }
    const usig = upscaleSig(data);
    if (!z.rUpscale.contains(document.activeElement) && z.rUpscale.dataset.sig !== usig) {
        z.rUpscale.dataset.sig = usig;
        runZone("二采放大", () => renderUpscaleZone(z.rUpscale, data));
    }
        const histSig = (state?.dir ?? "") + "|" + (mf?.finals || []).join(",")
            + "|" + (mf?.merges || []).map((m) => m?.file || "").join(",");
    if (histSig !== desk.histSig || !z.rHist.querySelector(".h3d-hist")) {
        if (!isVideoPlaying(z.rHist)) {
            desk.histSig = histSig;
            runZone("成片历史", () => renderHistoryZone(z.rHist, data));
        }
    }

    /* 页脚 CTA + 提示 */
    runZone("页脚", () => renderFooter(z, data));
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
    {
        const { det, box } = foldBox("proj-list", `项目存档（${projects.length}）`, true);
        box.append(list);
        sec.append(det);
    }

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
    renderV2Section(sec, data);
}

/* ---- 素材库入口（左下角）：一个浏览器 + 四个 scope ----
 * 蓝本 Majoor Assets Manager：瓦片只放缩略图 + 徽标，动作全部收进右键菜单 / 预览器。
 * 真正的 UI 在 web/h3_library.js（window.H3Lib.open）。 */
function renderV2Section(sec, data) {
    try {
        const { state, mf, ds } = data || {};
        const dir = state?.dir || "";
        sec.append(el("div", "h3d-sechead",
            "<strong>素材库</strong><small>项目资产 · 全局库 · 成片 · latent（同一个浏览器）</small>"));
        const hub = el("div", "h3d-hubrow");
        const nAsset = (ds?.ref_assets || []).length;
        const nFin = (mf?.videos || []).filter(Boolean).length + (mf?.finals || []).length
            + (mf?.merges || []).length;
        const nLat = (mf?.latents || []).length;
        const b = el("button", "h3d-hubbtn",
            "🗂 打开素材库" +
            `<small>资产 ${nAsset} · 成片 ${nFin} · latent ${nLat} · 可搜索 / 筛选 / 批量</small>`);
        b.type = "button";
        b.title = dir ? "浏览与管理全部素材（右键 = 动作菜单，双击 = 预览）" : "先新建 / 读档一个项目";
        b.onclick = () => {
            if (!dir) { alert("先新建 / 读档一个项目"); return; }
            if (!window.H3Lib) { alert("素材库前端未加载（web/h3_library.js）"); return; }
            window.H3Lib.open({
                dir,
                seg: (typeof _selSeg === "number" ? _selSeg : 0) + 1,
                onChanged: () => scheduleRefresh(300),
            });
        };
        hub.append(b);
        sec.append(hub);
        sec.append(el("div", "h3d-foot",
            "素材按「缩略图 + 徽标」铺网格；引用到段 / 标注首尾帧 / 调入项目 / 暂存 input 都在右键菜单里。"));
    } catch (e) {
        try { sec.append(el("div", "h3d-empty", "素材库入口加载失败（不影响主功能）")); } catch (_) { /* 空 */ }
    }
}

/* 伸缩框（状态常驻内存，重绘不丢失折叠态） */
const _foldState = {};
function foldBox(id, title, defaultOpen) {
    const det = document.createElement("details");
    det.className = "h3d-libfold";
    det.open = (_foldState[id] !== undefined) ? !!_foldState[id] : !!defaultOpen;
    det.append(el("summary", "h3d-libfoldsum", title));
    const box = el("div", "h3d-libfoldbody");
    det.append(box);
    det.addEventListener("toggle", () => { _foldState[id] = det.open; });
    return { det, box };
}

/* 资产库：链路说明 + 旧槽迁移 + 素材池（标注/引用/移除/剪辑/转码/入库）+ 登记表
 * 原左栏「素材池 · 分段处理中心」与模式条已并入本页；无模式门槛，链路后端自动推导。
 * repaint=重绘本页（标注切换/删卡/入库后调用）。 */
/* 资产预览地址：asset_id（全局库）> assets/…（项目 output）> 其余（input）
 *
 * 注意「全局库相对路径」这一档（images/ videos/ audios/，见 asset_store.register_content
 * 的 kind 目录）：asset_links 回填的 file 就是这种，它们**不在项目里、也不在 input 里**，
 * 只能按 asset_id 走 /h3chain/library_file 取。旧实现把它们回落到 inputViewUrl，
 * 于是缩略图实际指向了 ComfyUI input 目录下的同名文件 —— 找不到就裂图，
 * 找到了就是张冠李戴（线下报的「缩略图跟原素材对不上」）。取不到就返回空串，
 * 由调用方（buildAssetThumb）回落类别图标，绝不显示错图。 */
const _LIB_REL_RE = /^(images|videos|audios)\//;
function assetPreviewUrl(dir, file, asset_id) {
    if (asset_id && window.H3Api?.libraryFileUrl) return window.H3Api.libraryFileUrl(asset_id);
    const f = String(file || "").replace(/\\/g, "/");
    if (f.startsWith("assets/")) return viewUrl(`h3_projects/${dir}`, f);
    if (_LIB_REL_RE.test(f)) return "";          // 全局库路径且无 asset_id：取不到
    return inputViewUrl(f);
}

/** 标识签名：kind|file|asset_id 三者任一变了才需要重建标识节点。
 *  redrawIcons 挂在卡片轻量重绘上（聚焦期间会反复跑），签名相同就直接跳过，
 *  否则每次重绘都重建 <video> 会让首帧反复重新解码、闪一下。 */
function thumbSig(a) {
    return [
        String((a && a.kind) || "image"),
        String((a && a.file) || ""),
        String((a && a.asset_id) || ""),
    ].join("\u0001");
}

/** 素材标识（缩略图 / 类别图标）统一构造 —— 引用条 chip、分段调度 chip、正文绿框共用。
 *  图/视给缩略图（视频走浏览器解码首帧，后端 make_thumb 目前只做图片，没有 ffmpeg 兜底），
 *  音频给音符图标（音频本来就没有画面）。取不到地址一律回落图标而不是错图。
 *  尺寸由外层容器用后代选择器控制（.h3d-chipbtn .h3d-thumb / .h3d-rtag .h3d-thumb …）。 */
function buildAssetThumb(dir, a) {
    const kind = String((a && a.kind) || "image");
    const url = assetPreviewUrl(dir, a && a.file, a && a.asset_id);
    const mkIcon = () => {
        const ic = document.createElement("span");
        ic.className = "h3d-kindmark";
        ic.textContent = KIND_ICON[kind] || KIND_ICON.image;
        ic.title = KIND_NAME[kind] || "";
        return ic;
    };
    if (kind === "audio" || !url) return mkIcon();
    if (kind === "video") {
        /* 首帧当封面：preload=metadata 只解码头信息，#t=0.1 让浏览器把首帧渲染出来
         * （与素材库瓦片同一套做法，不依赖后端的 ffmpeg 抽帧）。 */
        const v = document.createElement("video");
        v.className = "h3d-thumb";
        v.muted = true;
        v.preload = "metadata";
        v.playsInline = true;
        v.setAttribute("aria-hidden", "true");
        v.src = url + (url.includes("#") ? "" : "#t=0.1");
        v.onerror = () => { try { v.replaceWith(mkIcon()); } catch (e) { v.remove(); } };
        return v;
    }
    const im = document.createElement("img");
    im.className = "h3d-thumb";
    im.loading = "lazy";
    im.alt = "";
    im.src = url;
    im.onerror = () => { try { im.replaceWith(mkIcon()); } catch (e) { im.remove(); } };
    return im;
}

/* ---------- 段级首尾帧参考图（提示词框「首帧图/尾帧图」按钮） ----------
 * 标注入口从素材库挪到这里：每段各自指定一张项目内的图片作首/尾帧参考。
 * 首段首帧图 = i2v 起手帧、末段尾帧图 = FL2VA 剧情终点锚；其余段 = 段头/段尾
 * 身份锚（keyframe 注入）。选中的图同时进本段引用（正文补 @别名）。 */
function frameNameOf(file) {
    return String(file || "").split("/").pop() || "";
}

function setSegmentFrameImg(node, idx, key, file) {
    const ds = getDs(node);
    if (idx < 0 || idx >= (ds.segments || []).length) return false;
    const seg = ds.segments[idx];
    const cur = (seg.frame_img && typeof seg.frame_img === "object") ? { ...seg.frame_img } : {};
    if (file) cur[key] = String(file);
    else delete cur[key];
    seg.frame_img = (cur.first || cur.end) ? cur : null;
    /* 注意：这里**不**把图加进本段 refs —— 首/尾帧图是"锚点"不是"素材引用"。
     * 加进 refs 会让首段走 Ref2V（多参）分支，反而丢掉 i2v 起手帧语义。 */
    setDs(node, ds);
    /* 锚点变了，结果框里那句对齐指令必须跟着变：清了锚还留着
     * "Picture 1 aligns with the 0.00-second mark"，等于告诉模型去参考一张
     * 已经不存在的图。对齐行是编译产物不是手写的，可以放心重写。 */
    resyncAlignmentLines(node, idx);
    return true;
}

/* 官方对齐指令行的整行匹配（与后端 prompts._KF_RE 同口径） */
const KF_ALIGN_RE = /^(?:For the target video, at [0-9.]+ seconds into the target video, <(?:Picture|Video|Audio|Subject)\s+\d+> \(from \[Shot \d+\]\) is fully referenced\.|How the reference pictures align with the target video — .*mark of the target video\.)$/;

/* ③ 结果框的活编辑器登记表：程序改了 ds.prompts[idx] 之后必须**同时改 DOM**。
 * 只写 widget 不碰编辑器的话，屏幕上还是旧文本，用户下一次输入/blur 会把旧文本
 * 回写 —— 于是"自动补的对齐指令输不进去 / 一改就没了"，切屏重建卡片才看得见。
 * 刷新周期补不了这个洞：焦点在框里时整卡重建被焦点守卫挡掉。 */
const _promptEditors = new Map();
function registerPromptEditor(node, idx, api) {
    try {
        /* 顺手清掉已下线的编辑器：卡片反复重建，登记表只增不减就会越攒越多
         * （键是 node:idx，换段/换项目后老键永远命中不到）。 */
        if (_promptEditors.size > 64) {
            for (const [k, v] of [..._promptEditors]) {
                if (!v || !v.el || !v.el.isConnected) _promptEditors.delete(k);
            }
        }
        _promptEditors.set(`${node && node.id}:${idx}`, api);
    } catch (e) { /* 忽略 */ }
}
function syncPromptEditor(node, idx, text) {
    const api = _promptEditors.get(`${node && node.id}:${idx}`);
    if (!api || !api.el || !api.el.isConnected) return false;
    try {
        if (api.value === text) return false;
        api.value = text;                       // 保留焦点与光标（setter 内部处理）
        if (typeof api.normalizeLoose === "function") api.normalizeLoose();
        return true;
    } catch (e) { return false; }
}

/** 按当前锚点重写结果框里的对齐指令行：清了锚就摘掉，还有锚就按新锚重写。
 *  只动 integrated_multimodal_description 开头那几句，正文一个字不碰。
 *  原本没有对齐行、现在也不需要 → 直接返回，不打扰手写的文本。 */
function resyncAlignmentLines(node, idx) {
    try {
        const ds = getDs(node);
        const text = String((ds.prompts || [])[idx] || "");
        if (!text.trim()) return;
        const lines = text.split("\n");
        let prefix = null, inline = "", sawAlign = false;
        const before = [], body = [];
        for (const l of lines) {
            if (prefix === null) {
                /* 六段式（Ref2VA）主体字段叫 detailed_description：也要认，
                 * 否则切到 Ref2VA 后残留在正文里的对齐行永远摘不掉。 */
                const m = /^((?:integrated_multimodal_description|detailed_description):)(.*)$/.exec(l);
                if (!m) { before.push(l); continue; }
                prefix = m[1];
                const tail = m[2].trim();
                if (KF_ALIGN_RE.test(tail)) sawAlign = true;
                else if (tail) inline = tail;
                continue;
            }
            if (KF_ALIGN_RE.test(l.trim())) { sawAlign = true; continue; }
            body.push(l);
        }
        if (prefix === null) return;                  // 不是官方字段结构 → 不碰
        const seg = (ds.segments || [])[idx] || {};
        const fr = segHasFrames(ds, idx);
        const mode = effV2Mode(ds, idx);
        const pv = window.H3Prompts?.ensurePromptV2
            ? window.H3Prompts.ensurePromptV2(seg) : null;
        const nShots = pv ? ((pv.shots || []).length || 1) : 1;
        const want = v2InstrLines(mode, Number(seg.seconds) || 5.0,
            fr.has_start, fr.has_end, nShots);
        if (!sawAlign && !want.length) return;        // 原本没有、现在也不要 → 不动
        const parts = want.slice();
        if (inline) parts.push(inline);
        parts.push(...body);
        const head = prefix + (parts.length ? " " + parts.join("\n") : "");
        const newText = before.concat(head).join("\n");
        if (newText !== text) {
            setPromptText(node, idx, newText);
            /* 写完状态还要把屏幕上的编辑器一起改掉：否则编辑器里仍是旧文本，
             * 用户一动键盘就把旧文本回写，刚补的对齐行立刻消失
             * （"对齐指令似乎输不进去"的真正成因，不是输入被拦）。 */
            syncPromptEditor(node, idx, newText);
            scheduleRefresh(150);
        }
    } catch (e) { console.warn("[h3-director] resyncAlignmentLines failed:", e); }
}

/** 段级首帧图 / 尾帧图按钮组：点开选图（项目内图片或现传），已选可换可清。
 *  主提示词框（结果栏）与具象化共用同一套入口 —— 首/尾帧锚定是段级运行参数，
 *  一个段只有一组，不该在两个面板各改各的。 */
function mkFrameBtns(node, segIdx, onChange) {
    const box = el("div", "h3d-frmbtns");
    const paint = () => {
        box.replaceChildren();
        const ds = getDs(node);
        const seg = (ds.segments || [])[segIdx] || defaultSegment();
        const fi = seg.frame_img || {};
        /* 对齐指令里的编号（<Picture k>）按本段生效模式推：用户问"Picture 1 到底
         * 是哪张图"就靠这行对上号。无锚/Ref2VA 时没有编号（Ref2VA 不生成对齐句）。 */
        const picOf = framePictureNumbers(ds, segIdx);
        const dir = node ? getDirValue(node) : "";
        for (const [key, name] of [["first", "首帧图"], ["end", "尾帧图"]]) {
            const has = !!fi[key];
            const b = el("button", "h3d-btn h3d-frm" + (has ? " on" : ""));
            b.type = "button";
            if (has) {
                /* 缩略图：光看文件名分不清是哪张，缩略图才是"精准定位" */
                const im = document.createElement("img");
                im.className = "h3d-frmthumb";
                im.loading = "lazy";
                im.alt = "";
                im.src = assetPreviewUrl(dir, fi[key], "");
                im.onerror = () => { try { im.remove(); } catch (e) { /* 忽略 */ } };
                b.append(im);
            }
            b.append(document.createTextNode(
                `${has ? "" : "＋"}${name}${has ? `（${frameNameOf(fi[key])}）` : ""}`));
            const pic = has ? picOf[key] : 0;
            if (pic) b.append(el("span", "h3d-frmpic", `P${pic}`));
            b.title = has
                ? `本段${name}参考：${fi[key]}`
                    + (pic ? `\n在对齐指令里以 <Picture ${pic}> 出现（${picOf.mode}）` : "")
                    + "\n点开可换一张 / 清除"
                : `选一张项目里的图片当本段${name}参考（也可现场上传）`;
            b.addEventListener("mousedown", (e) => {
                e.preventDefault();
                e.stopPropagation();
                openFramePicker(node, segIdx, key, name, () => { paint(); onChange && onChange(); });
            });
            box.append(b);
        }
    };
    paint();
    return box;
}

/** 本段首尾帧锚在对齐指令里各占几号（首帧→1，FL2VA 尾帧→2；L2VA 只有尾帧且是 1 号）。
 *  与后端 prompts.compile_segment / 前端 v2InstrLines 同口径；无锚或 Ref2VA 返回 0。
 *  官方 I2VA/FL2VA/L2VA 段按定义**不会同时带参考素材**（有参考就是 Ref2VA，
 *  Ref2VA 不生成对齐句），所以这里不会出现编号被别的素材占掉的情况。 */
function framePictureNumbers(ds, segIdx) {
    try {
        const mode = effV2Mode(ds, segIdx);
        const { has_start: hasStart, has_end: hasEnd } = segHasFrames(ds, segIdx);
        const out = { mode, first: 0, end: 0 };
        if (mode === "T2VA" || mode === "Ref2VA") return out;
        if (mode === "FL2VA" && hasStart && hasEnd) { out.first = 1; out.end = 2; return out; }
        if ((mode === "L2VA" || mode === "FL2VA") && hasEnd) { out.end = 1; return out; }
        if ((mode === "I2VA" || mode === "FL2VA") && hasStart) { out.first = 1; return out; }
        return out;
    } catch (e) {
        return { mode: "", first: 0, end: 0 };
    }
}

/** 把一张已落盘的图片补进节点素材池（幂等，按 file 去重）。
 *  必须补：ds.ref_assets 是 persistPool 写回 manifest["assets"] 的**唯一来源**，
 *  上传完不补进去，下一次任何"写资产清单"的动作都会按旧池整表覆盖，
 *  于是"刚传的图没了 / 传一张顶掉上一张"。 */
function pushPoolAsset(node, file, label) {
    const f = String(file || "").trim();
    if (!f) return false;
    const ds = getDs(node);
    ds.ref_assets = Array.isArray(ds.ref_assets) ? ds.ref_assets : [];
    if (ds.ref_assets.some((a) => a && a.file === f)) return false;
    const taken = new Set(ds.ref_assets.map((a) => String((a && a.label) || "")));
    let lbl = String(label || "").trim()
        || String(f).split("/").pop().replace(/\.[^.]+$/, "") || "素材";
    /* 与后端 library.unique_label 同口径：先截到 21（给后缀留位）再递增。
     * 直接 `lbl = base + n` 再整体 slice(0,24)，当 base 已满 24 字时截断后
     * 恒等于自己 —— 死循环（后端曾经就是这么写的，会把 ComfyUI 卡死）。 */
    lbl = lbl.slice(0, 21);
    if (taken.has(lbl)) {
        const stem = lbl;
        let n = 2;
        while (n < 9999 && taken.has(`${stem}${n}`)) n += 1;
        lbl = `${stem}${n}`;
    }
    ds.ref_assets.push({
        file: f, kind: "image", label: lbl.slice(0, 24), asset_id: "", roles: [],
    });
    setDs(node, ds);
    return true;
}

/** 选图弹窗：项目库里的图片 + 现场上传 + 清除。 */
async function openFramePicker(node, idx, key, name, done) {
    const dir = getDirValue(node);
    if (!dir) { alert("先在「存档目录」选一个项目"); return; }
    const ds = getDs(node);
    const seg = (ds.segments || [])[idx] || defaultSegment();
    const curFile = (seg.frame_img || {})[key] || "";
    /* 候选图以**项目库**为准（manifest.assets + asset_links），不是只取节点状态
     * ds.ref_assets —— 后者是 widget 里的一份缓存，从素材库/其它入口上传的图要等
     * 一次刷新才进得来，于是这里"看不见刚传的图"。与 hydratePool 同口径取值。 */
    const imgs = (ds.ref_assets || [])
        .filter((a) => (a.kind || "image") === "image").map((a) => ({ ...a }));
    const seen = new Set(imgs.map((a) => `${a.file || ""}|${a.asset_id || ""}`));
    try {
        const mf = await fetchJson(`h3_projects/${dir}`, "manifest.json");
        const links = await fetchAssetLinks(dir);
        for (const a of poolFromManifest(mf, links)) {
            if ((a.kind || "image") !== "image") continue;
            const k = `${a.file || ""}|${a.asset_id || ""}`;
            if (seen.has(k)) continue;
            seen.add(k);
            imgs.push(a);
        }
    } catch (e) { /* 取不到项目库就退回节点池，不阻断 */ }
    /* 参考图可能不在池子里（直接落 assets/ 的文件）：补一条候选项 */
    if (curFile && !imgs.some((a) => a.file === curFile)) {
        imgs.push({ file: curFile, kind: "image", label: frameNameOf(curFile), asset_id: "" });
    }

    const overlay = el("div", "h3d-overlay");
    const dialog = el("div", "h3d-dialog");
    dialog.innerHTML = `<h3>🖼 本段${name}参考</h3>
        <p class="h3d-lead">选一张项目里的图片，作为第 ${idx + 1} 段的${name}锚点。
        ${key === "first"
            ? "首段：i2v 起手帧（画面从这张图开始）；中段：段头身份锚（keyframe 注入，抑制长链漂移）。"
            : "末段：FL2VA 剧情终点锚；其它段：该段末帧锚（同位置唯一锚，优先于段尾锚）。"}
        它不进素材引用编号（是锚点，不是被引用的素材）。</p>`;
    const grid = el("div", "h3d-frmgrid");
    const mkTile = (a) => {
        const t = el("div", "h3d-frmtile" + (a.file === curFile ? " on" : ""));
        const im = document.createElement("img");
        im.loading = "lazy";
        im.src = assetPreviewUrl(dir, a.file, a.asset_id);
        im.onerror = () => { im.replaceWith(el("span", "h3d-frmico", "🖼")); };
        t.append(im, el("div", "h3d-frmname", escapeHtml(a.label || frameNameOf(a.file))));
        t.onclick = () => {
            setSegmentFrameImg(node, idx, key, a.file);
            overlay.remove();
            if (typeof done === "function") done();
        };
        return t;
    };
    for (const a of imgs) grid.append(mkTile(a));
    if (!imgs.length) {
        grid.append(el("div", "h3d-empty", "项目里还没有图片：用下面的「上传一张」先传进来"));
    }
    dialog.append(grid);

    const row = el("div", "h3d-dialog-row");
    const upBtn = el("button", "h3d-btn", "＋ 上传一张");
    upBtn.type = "button";
    /* 后端 dest=project 就是**只落项目**（早期会顺手往全局库复制一份，已按反馈去掉），
     * 别再照旧文案说"全局库一份"——跨项目复用是素材库里显式的「存入全局库」。 */
    upBtn.title = "上传并登记进本项目 assets/（只落项目一份，可用 @别名 引用），"
        + "然后直接用作本段参考；跨项目复用请用素材库的「存入全局库」";
    /* 上传中闸门：一次只允许一个上传在飞。以前连点/重复 onchange 会并发跑两遍
     * 「写锚点 + 推池 + 重建池」，第二次拿到的是第一次未落盘的中间态，
     * 于是状态互相踩（"传一张顶掉上一张"的一个来源）。 */
    let uploading = false;
    const closeOverlay = () => { try { overlay.remove(); } catch (e) { /* 已移除 */ } };
    upBtn.onclick = () => {
        if (uploading) return;
        const inp = document.createElement("input");
        inp.type = "file";
        inp.accept = "image/*";
        inp.onchange = async () => {
            const f = (inp.files || [])[0];
            if (!f || uploading) return;
            uploading = true;
            upBtn.disabled = true;
            const oldTxt = upBtn.textContent;
            upBtn.textContent = "上传中…";
            try {
                const H3Assets = window.H3Assets;
                if (!H3Assets?.uploadDirect) throw new Error("上传接口不可用");
                const alias = f.name.replace(/\.[^.]+$/, "").slice(0, 24);
                /* 超时兜底：后端一旦被卡住（历史上确有其事——标签唯一化死循环把
                 * aiohttp 事件循环占死），请求永远不返回，界面就停在"上传中"，
                 * 用户只能猜。这里 45s 无响应就明确报错并放行下一次尝试。 */
                const res = await Promise.race([
                    H3Assets.uploadDirect(f, {
                        kind: "image", alias, dest: "project", link_dir: dir, mirror: "1",
                    }),
                    new Promise((_, rej) => setTimeout(
                        () => rej(new Error("后端 45 秒无响应（ComfyUI 可能已卡死，请查看控制台/重启）")),
                        45000)),
                ]);
                if (!res?.ok) throw new Error("上传返回异常");
                const file = res?.mirrored?.file || res?.stored?.file || "";
                if (!file) throw new Error(res?.mirror_error || "上传后没拿到项目内路径");
                const rel = String(file).startsWith("assets/")
                    ? file : `assets/${String(file).split("/").pop()}`;
                setSegmentFrameImg(node, idx, key, rel);
                /* 上传即入池：不补进 ds.ref_assets，稍后任何一次写资产清单都会
                 * 拿旧池整表覆盖，把刚传的这张（以及更早传的）从项目库里抹掉。 */
                pushPoolAsset(node, rel, res?.stored?.label || alias);
                closeOverlay();
                if (typeof done === "function") done();
                /* 立刻按最新 manifest 重建池并刷新（不等 240ms 轮询，也不等"切屏"），
                 * 否则出现"传了没反应、切一下屏又好了"。 */
                try { await hydratePool(true); } catch (e) { /* 忽略 */ }
                scheduleRefresh(120);
            } catch (e) {
                alert(`上传失败：${e?.message || e}`);
            } finally {
                uploading = false;
                upBtn.disabled = false;
                upBtn.textContent = oldTxt;
            }
        };
        inp.click();
    };
    const clearBtn = el("button", "h3d-btn h3d-btn-danger", "清除");
    clearBtn.type = "button";
    clearBtn.disabled = !curFile;
    clearBtn.onclick = () => {
        setSegmentFrameImg(node, idx, key, "");
        overlay.remove();
        if (typeof done === "function") done();
    };
    const cancel = el("button", "h3d-btn", "取消");
    cancel.type = "button";
    cancel.onclick = () => overlay.remove();
    if (curFile) row.append(clearBtn);
    row.append(upBtn, cancel);
    dialog.append(row);
    overlay.append(dialog);
    overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") overlay.remove(); });
    document.body.append(overlay);
    cancel.focus();
}

/* 资产标注切换：同 role 全库唯一，改标自动取消上一张 */
/* 上传并入库：input 上传 → 拷贝进项目 assets/ → 池条目 file 改写 */
/* 旧池条目入库：input 文件 → 项目 assets/ 并改写 file */
/* P3 上传即入库（单次往返）：文件 → 全局库 + 本项目链接 → 推池。
 * 后端无 libraryUpload（旧版）时回落两步走（input 中转 + importAsset）。 */
/* P3 全局库调入项目：拷贝进 assets/ 并推 legacy 池条目（剪辑/转码/旧链用项目内文件） */
/* P3 标签所见即所得：整包干跑一次，把每段真实 token 写回引用徽标（失败静默保留段号）。 */
/* 旧三槽位迁移：first/end/last_frame → 入库打标；last_frame 另写各段 tail_src */
/* 裁剪子面板：探针取帧率/总帧，入帧/出帧数字窗，确认后按帧换算秒执行 */
/* latent 库：已登记 latent（切片/删除）+ 手工切片表单 */
/* P4e：latent 行一键放大——驱动画布 H3LatentUpscale（临时模式切换，finally 还原）。
 * 只走神经 enlarge（×2 存回本库）；二次采样等高级参数去画布节点调。
 * 放大模型下拉为空时拒绝（不排队，避免空跑报错）。 */
async function runUpscaleTool(node, dir, file, say) {
    const g = app.graph;
    const up = ((g && g._nodes) || []).find((n) => n.type === "H3LatentUpscale") || null;
    const main = findNode();
    if (!up) { say("画布上没有 H3LatentUpscale 节点（请载入配套工作流）"); return; }
    if (!main) { say("画布上没有主节点"); return; }
    const W = (name) => (up.widgets || []).find((w) => w.name === name);
    const setW = (name, value) => {
        const w = W(name);
        if (!w) return false;
        w.value = value;
        if (typeof w.callback === "function") { try { w.callback(value); } catch (e) { /* 可选 */ } }
        return true;
    };
    const modelW = W("放大模型");
    if (!modelW || !String(modelW.value || "").trim()) {
        say("请先在画布 H3LatentUpscale 节点选择放大模型（models/latent_upscale_models/）");
        return;
    }
    const stem = String(file).split("/").pop().replace(/\.pt$/i, "") || "latent";
    const outName = `${stem}_up2x.pt`;
    setW("项目名", dir);
    setW("源文件", file);
    setW("保存名", outName);
    const prevUp = up.mode, prevMain = main.mode;
    try {
        up.mode = 0;
        main.mode = 2;
        await app.queuePrompt();
        say(`已提交放大（${file} → ${outName}），完成后在 latent 库查看；主节点已恢复。`);
    } catch (e) {
        say("提交放大失败：" + (e?.message || e));
    } finally {
        try { up.mode = prevUp; } catch (e) { /* 恢复 */ }
        try { main.mode = prevMain; } catch (e) { /* 恢复 */ }
        try { if (up.setDirtyCanvas) up.setDirtyCanvas(true, true); } catch (e) {}
        try { if (main.setDirtyCanvas) main.setDirtyCanvas(true, true); } catch (e) {}
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
    // 编辑中（常驻 textarea 聚焦 / v2 分组输入聚焦 / 临时编辑器打开 / 视频播放中 / 拖拽调序进行中）
    // 跳过重建，防丢焦丢草稿 / 防拖动中卡片列表被轮询刷新换掉导致 drop 目标失效
    // v2 分组含大量 input/select：任一聚焦都视为编辑中（否则 900ms 轮询重建会抢焦点）
    const taFocus = !!colC.querySelector(".h3d-ta:focus, .h3d-v2f:focus, input:focus, select:focus");
    const locked = taFocus || !!colC.querySelector(".h3d-editor") || isVideoPlaying(colC) || !!dragSeg;
    if (locked) {
        /* 不重建，但卡片**自己**的即时状态照旧推进（引用 chips / 计数 / 正文绿框）；
         * 否则焦点在框里时，任何刷新都被丢掉 → "要点别的才更新"。 */
        for (const fn of _cardPainters) {
            try { fn(); } catch (e) { /* 单卡重绘失败不影响别的卡 */ }
        }
        return;
    }
    if (sig !== desk.cardsSig || !colC.querySelector(".h3d-center-pad")) {
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
                && !segAutoRef(data.ds.segments[it.idx]);
            const offchain = it?.kind === "prompt" && it.idx !== undefined
                && !segAutoSeq(data.ds.segments[it.idx]);
            const sp = el("span", (i < done ? "done" : i === done ? "next" : "")
                + (unlinked ? " unlink" : "") + (offchain ? " offchain" : ""));
            sp.style.flexGrow = String(Math.max(1, sec));
            sp.title = `段 ${i + 1} · ${sec}s`
                + (unlinked ? " · 关闭自动引用上段（与上段硬切）" : "")
                + (offchain ? " · 关闭自动按序生成（跳过）" : "");
            rail.append(sp);
        }
        wrap.append(rail);
    }

    /* 横向分段选择条（点选看一段；＋ 横向加段；pill 可拖调序） */
    if (total > 0) wrap.append(renderSegStrip(data));

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
    wrap.append(buildCardsSafe(data));

    /* 添加行 */
    const addrow = el("div", "h3d-addrow");
    if (node) {
        const addSeg = el("button", "h3d-btn", "＋ 添加一段");
        addSeg.title = "新增一段提示词到导演台状态（最多 64 段），新增后自动选中";
        addSeg.onclick = () => addPromptSegment(node);
        addrow.append(addSeg);
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

/* ---- 具象化提示词（FL2VA 首尾帧 / Ref2VA 全参考，对齐官方 h3-prompt-writing） ----
 * 模式默认跟随导演台（多参→Ref2VA，其余→FL2VA），
 * 可手动覆写（存 seg.v2mode，传 /h3chain/compile 的 mode 参数）。
 * 字段按官方顺序组织：base 系=对齐指令＋三核心字段；Ref2VA=六段式。
 * 写回经 setPromptV2Field -> ds.segments[idx].prompt_v2 -> flushPrompts -> save_prompts。
 * 提交前调 POST /h3chain/compile，errors 红、warnings 黄。
 * 焦点守卫：所有输入带 h3d-v2f，renderCenterColumn 见 input:focus 即跳过重建，防丢焦。 */
function renderPromptV2Panel(body, node, data, segIdx) {
    try {
        const seg = (data.ds.segments || [])[segIdx];
        if (!seg) return;
        const HP = window.H3Prompts || {};
        const srcSeg = Object.assign({}, seg, { prompt: ((data.ds.prompts || [])[segIdx] || "") });
        const pv0 = (seg.prompt_v2 && typeof seg.prompt_v2 === "object")
            ? seg.prompt_v2 : (HP.ensurePromptV2 ? HP.ensurePromptV2(srcSeg) : HP.migrateLegacySeg(srcSeg));
        const hasV2 = !!(seg.prompt_v2 && typeof seg.prompt_v2 === "object");
        /* hasStart/hasEnd 走 segHasFrames（段级 frame_img 优先，与节点实跑同口径） */
        const { has_start: hasStart, has_end: hasEnd } = segHasFrames(data.ds, segIdx);
        const autoMode = HP.detectMode ? HP.detectMode(pv0, { has_start: hasStart, has_end: hasEnd }) : "T2VA";
        const effMode = effV2Mode(data.ds, segIdx);
        const isManual = V2_MODES.includes(seg.v2mode);
        const isRef = effMode === "Ref2VA";
        const det = v2Details(`v2${segIdx}`,
            "h3d-seg-panel h3d-v2panel" + (hasV2 ? " has-content" : ""),
            `具象化微调 · ${escapeHtml(V2_MODE_ZH[effMode] || effMode)}`
            /* 没有「启用」概念了：改任意一个输入框就会把 v2 结构写出来，
             * 这个徽只说明"这段此前存没存过 v2"，不阻编辑。 */
            + `${hasV2 ? ' <span class="h3d-chip cyan">已存 v2</span>' : ' <span class="h3d-chip">未存 v2（改动即写入）</span>'}`,
            hasV2);
        const vbody = el("div", "h3d-seg-body");

        /* 判定行：输出结构由本段数据推导（有参考/主体 → 六段式，否则三段式），
         * 不再提供手动下拉——想改判定就去增删参考素材，而不是选模式。
         * 旧存档的 seg.v2mode 仍读，仅用于提示「曾经的手动值已忽略」。 */
        {
            const mrow = el("div", "h3d-v2row");
            const nRefs = (pv0.references || []).length + (pv0.subjects || []).length;
            const why = isRef
                ? `本段有 ${nRefs} 个参考条目`
                : ((hasStart || hasEnd) ? "本段无参考，按首尾帧" : "本段无参考、无首尾帧");
            let hint = (isRef
                ? "六段式（主体定义 → 总结 → 保留分析 → 详细描述 → 环境音 → 配乐）"
                : "三段式（整体描述 → 环境音 → 配乐）") + ` · ${why}`;
            if (isManual) hint += " · 旧存档的手动模式已忽略，现按数据判定";
            mrow.append(el("label", "h3d-seg-label", "输出结构"));
            mrow.append(el("span", "h3d-secs-hint", hint));
            vbody.append(mrow);
            const prev = el("pre", "h3d-v2out", "");
            if (isRef) {
                prev.textContent = "六段式（官方顺序）：主体定义 → 总结 → 保留分析 → "
                    + "详细描述 → 环境音 → 背景配乐";
            } else {
                prev.textContent = v2InstrPreview(effMode,
                    Number(seg.seconds) || 5.0, hasStart, hasEnd,
                    (pv0.shots || []).length);
            }
            vbody.append(prev);
        }

        /* 锚点 ↔ 编号对照：对齐指令是官方固定句，写的是 <Picture k> 编号而不是文件名，
         * 光看那句英文没法确认"Picture 1 到底是不是我传的那张"。这里把编号翻回
         * 具体文件，"能不能精准定位到上传的首尾帧图"就落在这行上。
         * Ref2VA 不生成对齐句 → 不给编号，避免误导。 */
        {
            const pic = framePictureNumbers(data.ds, segIdx);
            const seg2 = (data.ds.segments || [])[segIdx] || {};
            const fi = (seg2.frame_img && typeof seg2.frame_img === "object") ? seg2.frame_img : {};
            const rows = [];
            if (pic.first && fi.first) rows.push([1, "首帧图", fi.first]);
            if (pic.end && fi.end) rows.push([pic.end, "尾帧图", fi.end]);
            const row = el("div", "h3d-v2row");
            if (!rows.length) {
                row.append(el("span", "h3d-secs-hint",
                    isRef ? "本段为全参考（Ref2VA）：官方不生成对齐指令，首尾帧锚走 keyframe 注入，"
                          + "提示词里不出现 Picture 编号"
                          : "当前没有首尾帧锚 → 无对齐指令，也没有 Picture 编号"));
            } else {
                row.append(el("label", "h3d-seg-label", "锚点对照"));
                const box = el("div", "h3d-frmmap");
                for (const [k, name, file] of rows) {
                    box.append(el("span", "h3d-frmmap-row",
                        `<b>&lt;Picture ${k}&gt;</b> = ${escapeHtml(name)} · `
                        + `<code>${escapeHtml(frameNameOf(file))}</code>`));
                }
                row.append(box);
                row.append(el("span", "h3d-secs-hint",
                    "对齐指令是官方固定英文句，只能写编号不能写文件名；"
                    + "真正的图由 keyframe 注入，编号在本段内唯一（有参考素材时本段会判为 Ref2VA，不再生成对齐句）"));
            }
            vbody.append(row);
        }

        /* 首/尾帧锚定不在这里重复挂：它已在卡片公共区（三页之上常驻），
         * 一个段只有一组锚，两处都放只会互相打架。 */

        const mkLabel = (t) => el("label", "h3d-seg-label", escapeHtml(t));
        const mkTa = (val, ph, onInput, onBlur) => {
            const ta = document.createElement("textarea");
            ta.className = "h3d-ta h3d-seg-ta h3d-v2f";
            ta.rows = 2;
            ta.value = val || "";
            ta.placeholder = ph || "";
            if (onInput) ta.addEventListener("input", () => onInput(ta.value));
            if (onBlur) ta.addEventListener("blur", () => onBlur(ta.value));
            return ta;
        };
        const mkInp = (val, ph, onChange) => {
            const inp = document.createElement("input");
            inp.className = "h3d-v2f";
            inp.value = val || "";
            if (ph) inp.placeholder = ph;
            if (onChange) {
                inp.addEventListener("change", () => onChange(inp.value));
                inp.addEventListener("blur", () => onChange(inp.value));
            }
            return inp;
        };
        const mkSel = (val, opts, onChange) => {
            const sel = document.createElement("select");
            sel.className = "h3d-v2f";
            for (const o of opts) {
                const op = document.createElement("option");
                op.value = o;
                op.textContent = o === "" ? "（空）" : o;
                if (String(o) === String(val || "")) op.selected = true;
                sel.append(op);
            }
            if (onChange) sel.addEventListener("change", () => onChange(sel.value));
            return sel;
        };
        /* 中文显示 ⇄ 英文存值：pairs = [[存值, 中文名], …] */
        const mkMapSel = (val, pairs, onChange, title) => {
            const sel = document.createElement("select");
            sel.className = "h3d-v2f";
            if (title) sel.title = title;
            for (const [v, zh] of pairs) {
                const op = document.createElement("option");
                op.value = v;
                op.textContent = zh;
                if (String(v) === String(val ?? "")) op.selected = true;
                sel.append(op);
            }
            if (onChange) sel.addEventListener("change", () => onChange(sel.value));
            return sel;
        };
        const bindTop = (key, ph) => {
            vbody.append(mkLabel(key));
            vbody.append(mkTa(pv0[key] || "", ph,
                (v) => debouncePromptV2Write(node, segIdx, key, v),
                (v) => {
                    const t = _taTimers.get(`pv${segIdx}_${key}`);
                    if (t) { clearTimeout(t); _taTimers.delete(`pv${segIdx}_${key}`); }
                    setPromptV2Field(node, segIdx, (pv) => { pv[key] = v; });
                    scheduleRefresh(200);
                }));
        };

        // —— 画面组（合并为单个 visual 框：风格/构图/环境/光照/角色/道具写在一处）——
        const gPic = v2Details(`pic${segIdx}`, "h3d-v2group",
            "画面 · 风格/构图/环境/光照/角色/道具（拼入首镜开头）", hasV2);
        const gPicBody = el("div", "h3d-v2grid");
        gPicBody.append(mkLabel("画面整述（visual）"));
        gPicBody.append(mkTa(pv0.visual || "",
            "例：Live-action cinematic，medium shot，夜晚便利店门口，霓虹倒映在积水里，撑伞的女孩，纸伞",
            (v) => debouncePromptV2Write(node, segIdx, "visual", v),
            (v) => {
                const t = _taTimers.get(`pv${segIdx}_visual`);
                if (t) { clearTimeout(t); _taTimers.delete(`pv${segIdx}_visual`); }
                setPromptV2Field(node, segIdx, (pv) => { pv.visual = v; });
                scheduleRefresh(200);
            }));
        /* 旧存档的六个分立字段不再有输入口，但有值时仍参与编译（后端 visual 为空才回退），
         * 这里点一句，避免用户以为数据被丢了。 */
        const legacyPic = ["medium_style", "composition", "environment",
            "lighting", "characters", "props"].filter((k) => String(pv0[k] || "").trim());
        if (legacyPic.length && !String(pv0.visual || "").trim()) {
            gPicBody.append(el("div", "h3d-secs-hint",
                `旧存档拆在 ${legacyPic.length} 个分立字段（${legacyPic.join(" / ")}），仍会参与编译；填入上方合并框后以合并框为准`));
        }
        gPic.append(gPicBody);
        vbody.append(gPic);

        // —— 镜头组（Shots） ——
        const gShot = v2Details(`shot${segIdx}`, "h3d-v2group",
            `镜头 ×${(pv0.shots || []).length} · → ${isRef ? "detailed_description（详细描述）" : "integrated_multimodal_description（整体描述）"}：画面＋运镜＋对白＋屏显＋剧中乐＋本镜引用`,
            hasV2);
        const gShotBody = el("div", "h3d-v2grid");
        const MOVES = HP.CAMERA_MOVES || [""];
        const AMPS = HP.CAMERA_AMPS || ["", "small", "large"];
        const SPEEDS = HP.CAMERA_SPEEDS || ["", "slow", "fast"];
        (pv0.shots || []).forEach((sh, si) => {
            const box = el("div", "h3d-shot");
            const head = el("div", "h3d-shot-head", `第 ${si + 1} 镜${si === 0 ? "（无时间戳）" : ""}`);
            const del = el("button", "h3d-btn h3d-btn-danger", "删镜");
            del.style.cssText = "margin-left:auto;padding:2px 8px;font-size:11px";
            del.disabled = (pv0.shots || []).length <= 1;
            del.onclick = () => {
                setPromptV2Field(node, segIdx, (pv) => { pv.shots.splice(si, 1); });
                scheduleRefresh(60);
            };
            head.append(del);
            box.append(head);
            box.append(mkLabel("画面描述（description，英文短句）"));
            box.append(mkTa(sh.description || "", "本镜画面＋动作，官方会自动拼运镜句",
                (v) => {
                    const t = _taTimers.get(`pv${segIdx}_shot${si}_desc`);
                    if (t) clearTimeout(t);
                    _taTimers.set(`pv${segIdx}_shot${si}_desc`, setTimeout(() => {
                        _taTimers.delete(`pv${segIdx}_shot${si}_desc`);
                        setPromptV2Field(node, segIdx, (pv) => { (pv.shots[si] = pv.shots[si] || {}).description = v; });
                    }, 350));
                },
                (v) => {
                    const t = _taTimers.get(`pv${segIdx}_shot${si}_desc`);
                    if (t) { clearTimeout(t); _taTimers.delete(`pv${segIdx}_shot${si}_desc`); }
                    setPromptV2Field(node, segIdx, (pv) => { (pv.shots[si] = pv.shots[si] || {}).description = v; });
                    scheduleRefresh(200);
                }));
            const camRow = el("div", "h3d-v2row");
            camRow.append(mkMapSel(sh.camera_move || "",
                [["", "运镜：空"], ...MOVES.filter((x) => x).map((m) => [m, V2_CAM_ZH[m] || m])], (v) => {
                setPromptV2Field(node, segIdx, (pv) => { pv.shots[si].camera_move = v; });
                scheduleRefresh(80);
            }, "运镜类型（存官方英文）"));
            camRow.append(mkMapSel(sh.camera_amplitude || "",
                AMPS.map((a) => [a, V2_AMP_ZH[a] ?? a]), (v) => {
                setPromptV2Field(node, segIdx, (pv) => { pv.shots[si].camera_amplitude = v; });
                scheduleRefresh(80);
            }, "运镜幅度"));
            camRow.append(mkMapSel(sh.camera_speed || "",
                SPEEDS.map((s) => [s, V2_SPD_ZH[s] ?? s]), (v) => {
                setPromptV2Field(node, segIdx, (pv) => { pv.shots[si].camera_speed = v; });
                scheduleRefresh(80);
            }, "运镜速度"));
            box.append(mkLabel("运镜（类型 / 幅度 / 速度）"));
            box.append(camRow);
            /* 低频项收进「更多」：换镜时间 / 对白 / 屏显 / 剧中乐 / 引用标签。
             * 默认折叠——主界面每镜只剩「画面描述 + 运镜」两项。 */
            const nDg = (sh.dialogues || []).length;
            const nSt = (sh.screen_texts || []).length;
            const gMore = v2Details(`more${segIdx}_${si}`, "h3d-v2group h3d-shot-more",
                "更多 · 换镜时间 / 对白" + (nDg ? ` ×${nDg}` : "")
                + " / 屏显" + (nSt ? ` ×${nSt}` : "") + " / 剧中乐 / 引用", false);
            const gMoreBody = el("div", "h3d-v2grid");
            if (si > 0) {
                gMoreBody.append(mkLabel("换镜时间（秒，空=自动均分）"));
                const num = document.createElement("input");
                num.type = "number"; num.className = "h3d-v2f"; num.min = "0"; num.step = "0.1";
                num.value = (sh.start_seconds === null || sh.start_seconds === undefined) ? "" : String(sh.start_seconds);
                num.placeholder = "空=自动均分";
                num.addEventListener("change", () => {
                    const v = num.value === "" ? null : Number(num.value);
                    setPromptV2Field(node, segIdx, (pv) => {
                        pv.shots[si].start_seconds = (v === null || !isFinite(v) || v < 0) ? null : v;
                    });
                    scheduleRefresh(80);
                });
                gMoreBody.append(num);
            }
            // 对白
            gMoreBody.append(mkLabel(`对白 ×${(sh.dialogues || []).length}（说话人 / 语言 / 内容 / 语气 / 旁白）`));
            (sh.dialogues || []).forEach((d, di) => {
                const dbox = el("div", "h3d-shot");
                const r1 = el("div", "h3d-v2row");
                r1.append(mkInp(zhSpeaker(d.speaker || "S1"), "说话人", (v) => {
                    setPromptV2Field(node, segIdx, (pv) => { pv.shots[si].dialogues[di].speaker = enSpeaker(v); });
                }));
                r1.append(mkInp(zhLang(d.language || "Chinese"), "语言", (v) => {
                    setPromptV2Field(node, segIdx, (pv) => { pv.shots[si].dialogues[di].language = enLang(v); });
                }));
                const vd = el("label", "h3d-secs-hint", "");
                const vcb = document.createElement("input");
                vcb.type = "checkbox"; vcb.checked = !!d.voiceover;
                vcb.onchange = () => {
                    setPromptV2Field(node, segIdx, (pv) => { pv.shots[si].dialogues[di].voiceover = vcb.checked; });
                    scheduleRefresh(80);
                };
                vd.append(vcb, document.createTextNode(" 旁白（闭嘴不出镜）"));
                r1.append(vd);
                dbox.append(r1);
                dbox.append(mkTa(d.text || "", "对白原文（保留原语言）",
                    (v) => {
                        const k = `pv${segIdx}_sh${si}_dg${di}`;
                        const t = _taTimers.get(k);
                        if (t) clearTimeout(t);
                        _taTimers.set(k, setTimeout(() => {
                            _taTimers.delete(k);
                            setPromptV2Field(node, segIdx, (pv) => { pv.shots[si].dialogues[di].text = v; });
                        }, 350));
                    },
                    (v) => {
                        setPromptV2Field(node, segIdx, (pv) => { pv.shots[si].dialogues[di].text = v; });
                        scheduleRefresh(200);
                    }));
                dbox.append(mkInp(d.delivery || "", "语气，如 softly（空省略）", (v) => {
                    setPromptV2Field(node, segIdx, (pv) => { pv.shots[si].dialogues[di].delivery = v; });
                }));
                const drm = el("button", "h3d-btn h3d-btn-danger", "删对白");
                drm.style.cssText = "padding:2px 8px;font-size:11px;justify-self:start";
                drm.onclick = () => {
                    setPromptV2Field(node, segIdx, (pv) => { pv.shots[si].dialogues.splice(di, 1); });
                    scheduleRefresh(60);
                };
                dbox.append(drm);
                gMoreBody.append(dbox);
            });
            const addD = el("button", "h3d-btn", "＋ 对白");
            addD.style.cssText = "padding:3px 8px;font-size:11px;justify-self:start";
            addD.onclick = () => {
                setPromptV2Field(node, segIdx, (pv) => {
                    (pv.shots[si].dialogues = pv.shots[si].dialogues || []).push({ speaker: "S1", language: "Chinese", text: "", delivery: "", voiceover: false });
                });
                /* 重建后保持展开，否则刚加的对白连同「更多」一起被收起来 */
                _v2Open.set(`more${segIdx}_${si}`, true);
                _v2Open.set(`shot${segIdx}`, true);
                scheduleRefresh(60);
            };
            gMoreBody.append(addD);
            gMoreBody.append(mkLabel("屏显文字（一行一条）"));
            gMoreBody.append(mkTa((sh.screen_texts || []).join("\n"), "如 OPEN 24H",
                (v) => {
                    const k = `pv${segIdx}_sh${si}_st`;
                    const t = _taTimers.get(k);
                    if (t) clearTimeout(t);
                    _taTimers.set(k, setTimeout(() => {
                        _taTimers.delete(k);
                        setPromptV2Field(node, segIdx, (pv) => {
                            pv.shots[si].screen_texts = String(v).split("\n").map((x) => x.trim()).filter(Boolean).slice(0, 16);
                        });
                    }, 350));
                },
                (v) => {
                    setPromptV2Field(node, segIdx, (pv) => {
                        pv.shots[si].screen_texts = String(v).split("\n").map((x) => x.trim()).filter(Boolean).slice(0, 16);
                    });
                    scheduleRefresh(200);
                }));
            gMoreBody.append(mkLabel("本镜剧中音乐（空省略）"));
            gMoreBody.append(mkInp(sh.diegetic_music || "", "如 radio plays softly", (v) => {
                setPromptV2Field(node, segIdx, (pv) => { pv.shots[si].diegetic_music = v; });
            }));
            gMoreBody.append(mkLabel("本镜引用标签（逗号分隔）"));
            gMoreBody.append(mkInp((sh.ref_usage || []).join(", "), "如 角色1, 场景1", (v) => {
                setPromptV2Field(node, segIdx, (pv) => {
                    pv.shots[si].ref_usage = String(v).split(/[,，、;；]+/).map((x) => x.trim()).filter(Boolean).slice(0, 32);
                });
            }));
            gMore.append(gMoreBody);
            box.append(gMore);
            gShotBody.append(box);
        });
        const addShot = el("button", "h3d-btn h3d-btn-cyan", "＋ 加一镜");
        addShot.onclick = () => {
            setPromptV2Field(node, segIdx, (pv) => {
                const n = (pv.shots || []).length + 1;
                const ns = HP.defaultShot ? HP.defaultShot(n) : { index: n, description: "" };
                (pv.shots = pv.shots || []).push(ns);
            });
            scheduleRefresh(60);
        };
        gShotBody.append(addShot);
        gShot.append(gShotBody);
        vbody.append(gShot);

        // —— 声音组 ——
        const gSnd = v2Details(`snd${segIdx}`, "h3d-v2group",
            "声音 · overall_soundscape / non_diegetic_music（无配乐写 N/A；剧中音乐写进各镜头）",
            false);
        const gSndBody = el("div", "h3d-v2grid");
        for (const [k, zh, ph] of [["soundscape", "环境音", "环境＋动作音（对白禁入此）"], ["non_diegetic_music", "背景配乐", "角色听不到的配乐，无则 N/A"]]) {
            gSndBody.append(mkLabel(`${zh}（${k}）`));
            gSndBody.append(mkTa(pv0[k] || "", ph,
                (v) => debouncePromptV2Write(node, segIdx, k, v),
                (v) => {
                    const t = _taTimers.get(`pv${segIdx}_${k}`);
                    if (t) { clearTimeout(t); _taTimers.delete(`pv${segIdx}_${k}`); }
                    setPromptV2Field(node, segIdx, (pv) => { pv[k] = v; });
                    scheduleRefresh(200);
                }));
        }
        gSnd.append(gSndBody);
        vbody.append(gSnd);

        // —— 参考组（常驻：官方六段式之 subject_definitions / summary / retention_analysis ＋ 素材调度）——
        /* 以前这里 `if (isRef)` 才挂载，而 isRef 又要求 references/subjects 非空，
         * 条目却只能在本组里加 —— 死锁：没有上传渠道就永远进不了多参。
         * 常驻之后靠数据驱动：本组有条目 → 六段式（多参），清空 → 三段式（文/首尾帧）。 */
        const gRef = v2Details(`ref${segIdx}`, "h3d-v2group",
            "参考 · 主体定义 / 总结 / 保留分析 ＋ 素材调度"
            + "（有条目 → 多参六段式；清空 → 文/首尾帧三段式）", isRef);
        const gRefBody = el("div", "h3d-v2grid");
        gRefBody.append(mkLabel(`主体定义 ×${(pv0.subjects || []).length}（<Subject N>）`));
        (pv0.subjects || []).forEach((st, ti) => {
            const row = el("div", "h3d-v2row");
            row.append(mkTa(st.definition || "", "主体定义，如 <Subject 1> is the young woman in <Picture 1>, ...", (v) => {
                setPromptV2Field(node, segIdx, (pv) => { pv.subjects[ti].definition = String(v).slice(0, 1000); });
            }, null));
            const rm = el("button", "h3d-btn h3d-btn-danger", "✕");
            rm.style.cssText = "padding:3px 8px;flex:none";
            rm.onclick = () => {
                setPromptV2Field(node, segIdx, (pv) => { pv.subjects.splice(ti, 1); });
                scheduleRefresh(60);
            };
            row.append(rm);
            gRefBody.append(row);
        });
        const addSub = el("button", "h3d-btn", "＋ 主体");
        addSub.style.cssText = "padding:3px 8px;font-size:11px;justify-self:start";
        addSub.onclick = () => {
            setPromptV2Field(node, segIdx, (pv) => { (pv.subjects = pv.subjects || []).push({ definition: "" }); });
            scheduleRefresh(60);
        };
        gRefBody.append(addSub);
        /* 素材引用：由「分段素材调度」的勾选自动生成（按 kind 分别编号
         * <Picture/Video/Audio k>，与后端 by_alias 同口径），不再手填标签。
         * note 仍可写，存在 pv.references 里按 label 匹配。 */
        {
            const want = v2RefsFromSchedule(data.ds, segIdx);
            const stored = Array.isArray(pv0.references) ? pv0.references : [];
            gRefBody.append(mkLabel(`素材引用 ×${want.length}（按素材调度自动生成）`));
            if (!want.length) {
                gRefBody.append(el("div", "h3d-secs-hint",
                    "未调度素材：在下方「分段素材调度」勾选后自动生成，无需手填标签"));
            }
            for (const w of want) {
                const row = el("div", "h3d-v2row");
                row.append(el("span", "h3d-chip", escapeHtml(`${w.label} ← ${w.src}`)));
                const hit = stored.find((r) => r && r.label === w.label);
                row.append(mkInp(hit ? (hit.note || "") : "", "说明（可选）", (v) => {
                    setPromptV2Field(node, segIdx, (pv) => {
                        const list = Array.isArray(pv.references) ? pv.references : (pv.references = []);
                        const at = list.findIndex((r) => r && r.label === w.label);
                        const note = String(v).slice(0, 200);
                        if (at >= 0) list[at].note = note;
                        else list.push({ label: w.label, note });
                    });
                }));
                gRefBody.append(row);
            }
            const rebuild = el("button", "h3d-btn", "↻ 按素材调度重建");
            rebuild.style.cssText = "padding:3px 8px;font-size:11px;justify-self:start";
            rebuild.title = "清空现有引用条目，按当前勾选的素材重新生成标签（已写的说明会清空）";
            rebuild.onclick = () => {
                /* 没写说明就不弹窗打断：confirm 关闭后焦点常落空，
                 * 紧接着点输入框会点空一拍，表现成"输入框卡住"。 */
                const hasNote = (pv0.references || [])
                    .some((r) => r && String(r.note || "").trim());
                if (hasNote && !confirm("按当前素材调度重建引用列表？已写的说明会清空。")) return;
                setPromptV2Field(node, segIdx, (pv) => {
                    pv.references = want.map((w) => ({ label: w.label, note: "" }));
                });
                _v2Open.set(`ref${segIdx}`, true);
                scheduleRefresh(80);
            };
            gRefBody.append(rebuild);
        }
        /* 分段素材调度：本段实际喂 conditioning 的资产集合（单段上限 图9/视3/音3）。
         * 缺省（全空）= 只用提示词文本 @标签 出现的；与上方参考条目是两回事：
         * 上方管官方六段式文本，下面管本段 conditioning 调度。 */
        {
            const pool2 = (data.ds.ref_assets || []);
            const seg2 = (data.ds.segments || [])[segIdx] || defaultSegment();
            gRefBody.append(mkLabel("分段素材调度（本段 conditioning，单段上限）"));
            if (!pool2.length) {
                gRefBody.append(el("div", "h3d-empty", "资产库为空：去三库资产页上传入库"));
            } else {
                const sched = el("div", "h3d-refrow");
                const picked = { image: 0, video: 0, audio: 0 };
                pool2.forEach((a) => {
                    const k = a.kind || "image";
                    const on = (seg2.refs || []).includes(a.label);
                    if (on) picked[k] += 1;
                    const roles = Array.isArray(a.roles) && a.roles.length ? `【${a.roles.join("·")}】` : "";
                    const chip = el("button", "h3d-refchip" + (on ? " on" : ""));
                    chip.type = "button";
                    chip.title = `${KIND_NAME[k]}素材${roles}：勾选后本段 conditioning 引用（同类按勾选顺序编号 <${KIND_TOKEN[k]} k>），正文自动补 @标签；单段上限 图${KIND_CAPS.image}/视${KIND_CAPS.video}/音${KIND_CAPS.audio}`;
                    /* 标识统一走 buildAssetThumb：图片=缩略图、视频=首帧、音频=音符图标。 */
                    chip.append(buildAssetThumb(getDirValue(node), a));
                    /* 显示时把夹带的格式后缀剥掉（详见 chipLabelText 注释）。alias 落库
                     * 早就去扩展名了；这里是给老 manifest / 边缘路径兜底。 */
                    chip.append(document.createTextNode(chipLabelText(a.label) + roles));
                    chip.onclick = () => { toggleSegmentRef(node, segIdx, a.label); scheduleRefresh(80); };
                    sched.append(chip);
                });
                if ((seg2.refs || []).length) {
                    sched.insertAdjacentHTML("beforeend",
                        `<span class="h3d-secs-hint">图${picked.image}/${KIND_CAPS.image}`
                        + ` 视${picked.video}/${KIND_CAPS.video} 音${picked.audio}/${KIND_CAPS.audio}</span>`);
                    const reset = el("button", "h3d-btn", "↺ 清空调度");
                    reset.type = "button";
                    reset.style.padding = "3px 8px";
                    reset.style.fontSize = "10.5px";
                    reset.title = "清空段级调度 = 只用提示词文本 @标签 出现的素材";
                    reset.onclick = () => {
                        setSegmentField(node, segIdx, "refs", []);
                        scheduleRefresh(80);
                    };
                    sched.append(reset);
                } else {
                    sched.insertAdjacentHTML("beforeend",
                        '<span class="h3d-secs-hint">未调度=只用文本@标签</span>');
                }
                gRefBody.append(sched);
            }
        }
        gRefBody.append(mkLabel("总结 · 任务类型（六选，可多选，＋ 连接）"));
        gRefBody.append(mkInp(zhTasks(pv0.task_types), "或手动输入、顿号分隔", (v) => {
            setPromptV2Field(node, segIdx, (pv) => { pv.task_types = enTasks(v); });
        }));
        {
            const qrow = el("div", "h3d-v2row");
            for (const t of V2_TASK_TYPES) {
                const on = (pv0.task_types || []).includes(t);
                const qb = el("button", "h3d-btn" + (on ? " h3d-btn-cyan" : ""), (on ? "✓ " : "＋ ") + (V2_TASK_ZH[t] || t));
                qb.style.cssText = "padding:2px 8px;font-size:10.5px";
                qb.title = t;
                qb.onclick = () => {
                    setPromptV2Field(node, segIdx, (pv) => {
                        const cur = Array.isArray(pv.task_types) ? [...pv.task_types] : [];
                        const at = cur.indexOf(t);
                        if (at >= 0) cur.splice(at, 1);
                        else if (cur.length < 8) cur.push(t);
                        pv.task_types = cur;
                    });
                    scheduleRefresh(60);
                };
                qrow.append(qb);
            }
            gRefBody.append(qrow);
        }
        gRefBody.append(mkLabel("总结 · 正文（空=自动生成）"));
        gRefBody.append(mkTa(pv0.summary_override || "", "覆写总结全文", (v) => debouncePromptV2Write(node, segIdx, "summary_override", v), (v) => {
            setPromptV2Field(node, segIdx, (pv) => { pv.summary_override = v; });
            scheduleRefresh(200);
        }));
        gRefBody.append(mkLabel(`保留分析 ×${(pv0.retention || []).length}（标签 / 关系 / 镜头 / 备注）`));
        const MARKERS = HP.RETENTION_MARKERS || ["fully_preserved"];
        (pv0.retention || []).forEach((rt, rti) => {
            const row = el("div", "h3d-v2row");
            row.append(mkInp(rt.label || "", "标签", (v) => {
                setPromptV2Field(node, segIdx, (pv) => { pv.retention[rti].label = String(v).slice(0, 64); });
            }));
            {
                const msel = document.createElement("select");
                msel.className = "h3d-v2f";
                msel.title = "保留关系（官方英文值）";
                for (const m of MARKERS) {
                    const op = document.createElement("option");
                    op.value = m;
                    op.textContent = V2_MARKER_ZH[m] || m;
                    if (String(m) === String(rt.marker || "fully_preserved")) op.selected = true;
                    msel.append(op);
                }
                msel.addEventListener("change", () => {
                    setPromptV2Field(node, segIdx, (pv) => { pv.retention[rti].marker = msel.value; });
                    scheduleRefresh(80);
                });
                row.append(msel);
            }
            const rm = el("button", "h3d-btn h3d-btn-danger", "✕");
            rm.style.cssText = "padding:3px 8px;flex:none";
            rm.onclick = () => {
                setPromptV2Field(node, segIdx, (pv) => { pv.retention.splice(rti, 1); });
                scheduleRefresh(60);
            };
            row.append(rm);
            gRefBody.append(row);
            gRefBody.append(mkInp((rt.shots || []).join(", "), "镜头，如 1,2（空=全镜）", (v) => {
                setPromptV2Field(node, segIdx, (pv) => {
                    pv.retention[rti].shots = String(v).split(/[,，、;；\s]+/).map((x) => x.trim()).filter(Boolean).slice(0, 16);
                });
            }));
            gRefBody.append(mkInp(rt.note || "", "备注", (v) => {
                setPromptV2Field(node, segIdx, (pv) => { pv.retention[rti].note = String(v).slice(0, 300); });
            }));
        });
        const addRet = el("button", "h3d-btn", "＋ 保留分析");
        addRet.style.cssText = "padding:3px 8px;font-size:11px;justify-self:start";
        addRet.onclick = () => {
            setPromptV2Field(node, segIdx, (pv) => {
                (pv.retention = pv.retention || []).push({ label: "", marker: "fully_preserved", shots: [], note: "" });
            });
            scheduleRefresh(60);
        };
        gRefBody.append(addRet);
        gRef.append(gRefBody);
        vbody.append(gRef);

        /* 高级组已移除：源码覆盖（override_text）不是"浏览"也不是"微调"，
         * 它是直接改写最终结果，归入主框的「结果浏览框」里作为直接编辑入口。 */
        void bindTop;

        // —— 动作行：编译预览/启用/同步旧字段 ——
        const out = el("pre", "h3d-v2out", "");
        out.style.display = "none";
        const say = (t, cls) => { out.style.display = ""; out.textContent = String(t); out.className = "h3d-v2out " + (cls || ""); };
        const row = el("div", "h3d-actions");
        const bPrev = el("button", "h3d-btn h3d-btn-cyan", "编译预览+校验");
        bPrev.title = "编译预览：返回官方英文＋校验，红=错误、黄=警告";
        bPrev.onclick = async () => {
            try {
                if (!window.H3Api) { say("h3_api.js 未加载", "err"); return; }
                const fresh = getDs(node);
                const fseg = (fresh.segments || [])[segIdx] || {};
                const payload = HP.compilePayload
                    ? HP.compilePayload(Object.assign({}, fseg, { prompt: (fresh.prompts || [])[segIdx] || "" }),
                        { seconds: Number(fseg.seconds) || 5.0, has_start: hasStart, has_end: hasEnd, mode: effV2Mode(fresh, segIdx) })
                    : { prompt: fseg.prompt_v2 || {}, seconds: 5.0, mode: effV2Mode(fresh, segIdx) };
                say("编译中…", "");
                const r = await window.H3Api.compilePreview(payload);
                const errs = r.body?.errors || [];
                const warns = r.body?.warnings || [];
                const txt = r.body?.compiled?.prompt_text || "";
                const head = `[${r.body?.compiled?.mode || effMode}] ${r.body?.ok ? "通过" : "未通过"} errors=${errs.length} warnings=${warns.length}`;
                const elines = errs.map((e) => `E ${e.code}: ${e.message}`).join("\n");
                const wlines = warns.map((w) => `W ${w.code}: ${w.message}`).join("\n");
                say([head, elines, wlines, "---", txt].filter(Boolean).join("\n"), r.body?.ok ? (warns.length ? "warn" : "ok") : "err");
            } catch (e) { say(`编译请求失败：${e?.message || e}`, "err"); }
        };
        /* 「启用具象化」按钮已删：setPromptV2Field 在 prompt_v2 缺失时会自动迁移创建，
         * 改任何一个输入框就已经启用了，再摆一个启用按钮纯属自我矛盾。
         * 「清除」保留 —— 它真能把结构删掉，让本段回到旧字段 / 以主框文本为准。
         * 面板标题上那句「未启用·用旧字段」只是说明这段还没存过 v2，不影响编辑。 */
        const bTog = hasV2 ? el("button", "h3d-btn", "清除具象化（回旧字段）") : null;
        if (bTog) {
            bTog.title = "删掉本段具象化结构，本段回到旧三字段（场景/角色/声音）路径，分组内容丢失";
            bTog.onclick = () => {
                if (!confirm("清除本段具象化？本段回到旧三字段（场景/角色/声音）路径，分组内容丢失。")) return;
                const ds = getDs(node);
                if (ds.segments[segIdx]) ds.segments[segIdx].prompt_v2 = null;
                setDs(node, ds);
                schedulePromptFlush();
                scheduleRefresh(60);
            };
        }
        const bSync = el("button", "h3d-btn h3d-btn-cyan", "同步到主框");
        bSync.title = "把当前具象化分组编译成官方文本，覆盖写回主提示词框（主框=最终进模型文本，可再手工微调）";
        bSync.onclick = async () => {
            try {
                if (!window.H3Api) { say("h3_api.js 未加载", "err"); return; }
                const fresh = getDs(node);
                const fseg = (fresh.segments || [])[segIdx] || {};
                const pv = (fseg.prompt_v2 && typeof fseg.prompt_v2 === "object")
                    ? fseg.prompt_v2 : (HP.ensurePromptV2 ? HP.ensurePromptV2(fseg) : null);
                if (!pv) { say("本段尚未启用 v2", "err"); return; }
                say("编译同步中…", "");
                const r = await window.H3Api.compilePreview({
                    prompt: pv, seconds: Number(fseg.seconds) || 5.0,
                    has_start: hasStart, has_end: hasEnd,
                    mode: effV2Mode(fresh, segIdx),
                });
                if (!r.body?.compiled?.prompt_text) {
                    say(`同步失败：${window.H3Api.errText(r, "编译无文本")}`, "err");
                    return;
                }
                setPromptText(node, segIdx, r.body.compiled.prompt_text);
                say(`已同步到主框 [${r.body.compiled.mode}] errors=${(r.body.errors || []).length}`, r.body.ok ? "ok" : "err");
                scheduleRefresh(200);
            } catch (e) { say(`同步失败：${e?.message || e}`, "err"); }
        };
        row.append(bPrev, ...(bTog ? [bTog] : []), bSync);
        vbody.append(row);
        vbody.append(out);
        det.append(vbody);
        body.append(det);
    } catch (e) {
        console.warn("[h3-director] renderPromptV2Panel failed:", e);
    }
}

/* ---- 横向分段选择条：pill 先压缩、极限后横滑；pill 可拖动调序 ----
 *  点选 pill 只切内存选中（_selSeg），不落 ds 不触发重做；
 *  拖动 pill 走同一条 applyReorder 路径（含范围清重摇/二采）。 */
function renderSegStrip(data) {
    const { node, state, mf, plan } = data;
    const strip = el("div", "h3d-segstrip");
    const off = planOff(mf);
    const done = Math.max(0, (mf?.done ?? state?.done ?? 0) - off);
    const sel = clampSelSeg(plan.length);
    const canDrag = !!(node && !mergeSel.on && plan.length > 1);
    plan.forEach((it, idx) => {
        if (it.kind !== "prompt") return;
        const seg = (data.ds.segments || [])[it.idx] || defaultSegment();
        const isDone = idx < done;
        const seqOff = !segAutoSeq(seg);
        const refOff = !segAutoRef(seg);
        const pill = el("button", "h3d-segpill"
            + (idx === sel ? " on" : "")
            + (isDone ? " done" : idx === done ? " next" : "")
            + (seqOff ? " off" : ""));
        pill.type = "button";
        pill.dataset.seg = String(idx);
        const label = `段${idx + 1}` + (seqOff ? " ⏸" : refOff ? " 🔗" : "");
        pill.innerHTML = `<span class="dot"></span><span>${escapeHtml(label)}</span>`;
        pill.title = `段 ${idx + 1}${isDone ? "（已完成）" : idx === done ? "（下一段）" : "（待生成）"}`
            + (seqOff ? " · 关闭自动按序生成" : "") + (refOff ? " · 关闭自动引用上段" : "")
            + (canDrag ? " · 按住拖动调序" : "") + " · 点击查看本段";
        pill.onclick = () => {
            if (_selSeg !== idx) { _selSeg = idx; scheduleRefresh(0); }
        };
        if (canDrag) {
            pill.draggable = true;
            pill.addEventListener("dragstart", (e) => {
                dragSeg = { idx: it.idx };
                pill.classList.add("h3d-dragging");
                try {
                    e.dataTransfer.effectAllowed = "move";
                    e.dataTransfer.setData("text/plain", String(it.idx));
                } catch (err) { /* 忽略 */ }
            });
            pill.addEventListener("dragend", () => {
                dragSeg = null;
                strip.querySelectorAll(".h3d-dragging, .h3d-drop-before, .h3d-drop-after")
                    .forEach((n) => n.classList.remove("h3d-dragging", "h3d-drop-before", "h3d-drop-after"));
                scheduleRefresh(60);
            });
        }
        strip.append(pill);
    });
    const add = el("button", "h3d-segpill h3d-segadd", "＋");
    add.type = "button";
    add.title = node ? `新增一段（最多 ${MAX_SEG} 段）` : "画布上未找到节点";
    add.disabled = !node;
    add.style.flex = "none";
    add.onclick = () => { if (node) addPromptSegment(node); };
    strip.append(add);
    /* strip 内拖放：按指针 X 换算插入线并吸附显示，松手提交调序 */
    if (canDrag) {
        strip.addEventListener("dragover", (e) => {
            if (!dragSeg) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            setPillDropLine(strip, data, dragSeg.idx, dropPillIndexAt(strip, e.clientX));
        });
        strip.addEventListener("drop", (e) => {
            if (!dragSeg) return;
            e.preventDefault();
            const dragIdx = dragSeg.idx;
            dragSeg = null;
            strip.querySelectorAll(".h3d-dragging, .h3d-drop-before, .h3d-drop-after")
                .forEach((n) => n.classList.remove("h3d-dragging", "h3d-drop-before", "h3d-drop-after"));
            applyReorder(node, data, dragIdx, dropPillIndexAt(strip, e.clientX));
        });
    }
    return strip;
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
    _cardPainters = [];                        // 旧卡的钩子随旧卡一起丢掉，由下面新卡重新注册

    if (!plan.length) {
        wrap.append(el("div", "h3d-empty",
            node
                ? "还没有段落：点下方「＋」添加第一段提示词。"
                : "画布上未找到 H3 Seamless Chain 节点，且暂无历史链数据。添加节点后这里成为段落流水线。"));
        return wrap;
    }

    /* 横向 strip 选中一段，只渲染该段卡片（其余段经 strip 切换查看） */
    const selIdx = clampSelSeg(plan.length);

    plan.forEach((it, idx) => {
        if (it.kind !== "prompt") return;      // 插入段前端已废弃，不渲染
        if (idx !== selIdx) return;            // 非选中段跳过
        const isDone = idx < done;
        /* 段禁用（不上链） */
        const segData0 = (it.idx !== undefined && data.ds.segments)
            ? (data.ds.segments[it.idx] || defaultSegment()) : null;
        const isDisabled = !segAutoSeq(segData0);
        const card = el("div", "h3d-card" + (isDone ? "" : " todo") + (isDisabled ? " offchain" : ""));
        /* 瘦身：卡内不再嵌大播放器，只留全屏入口（segMediaInfo → openSegViewer） */
        const mediaInfo = segMediaInfo(state, mf, idx);

        const body = el("div", "h3d-cbody");
        const segData = segData0;
        const stateChip = !isDone ? badge("待生成", "") : badge("已完成", "ok");
        const unlinkChip = (segData && !segAutoRef(segData)) ? badge("断引用", "warn") : "";
        const offChip = isDisabled ? badge("⏸ 跳过", "warn") : "";
        /* 待重摇徽章：已标记（未提交）或已在运行队列（审片逐段推进剩余量） */
        const redoMode = isDone ? redoMarks.get(idx + off) : null;
        const redoChip = redoMode ? badge(`🔁 待重摇·${redoMode}`, "warn") : "";
        const upRec = isDone ? (mf?.upscale?.segs || [])[idx + off] : null;
        const upChip = upRec?.done
            ? badge(`二采${upRec.size?.length ? ` ${upRec.size[0]}×${upRec.size[1]}` : "✓"}`, "cyan") : "";
        const title = el("div", "h3d-ctitle",
            `<span>段 ${idx + 1}</span>${stateChip}${offChip}${unlinkChip}${redoChip}${upChip}`);
        /* 段片全屏入口（瘦身后唯一浏览口）：有成片才显示 */
        if (mediaInfo && (mediaInfo.videoUrl || mediaInfo.thumbUrl)) {
            const pv = el("button", "h3d-btn h3d-previewbtn", mediaInfo.videoUrl ? "▶ 预览" : "🖼 查看");
            pv.title = "全屏浏览本段成片（视频/缩略图），不占卡片空间";
            pv.onclick = () => openSegViewer(mediaInfo, `段 ${idx + 1} 成片`);
            title.append(pv);
        }
        /* 合并模式：已完成段可勾选拼接进 merged_*.mp4（勾选值=全局 1-based 段号，
         *  跨段勾选经顶部 strip 逐一切段勾选，勾选态常驻内存）；
         *  已禁用段不进成片（与自动成片/二采拼接同口径），不给勾选 */
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
        /* 锚定面板重建器（面板构建时赋值）：分段时间一变，目标轨的刻度/体检全要
         * 跟着重画——面板手里的 frameLen 闭包读的是构建时的 ds 快照，这里不重建
         * 它就会一直显示旧帧数（实测症状：改了秒数，目标轨总长不动）。 */
        let rebuildAnchorPane = null;
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
            // 帧数与锚定面板目标轨同源（segmentFrames），别再各算一遍
            secsHint = el("span", "h3d-secs-hint", `≈${segmentFrames(node, segData)}帧`);
            const syncHint = (v) => {
                secsHint.textContent = `≈${segmentFrames(node, { seconds: Number(v) })}帧`;
            };
            secsInp.addEventListener("input", () => syncHint(secsInp.value));
            secsInp.addEventListener("change", () => {
                setSegmentSeconds(node, it.idx, secsInp.value);
                // 写透本卡持有的 ds 快照（getDs 每次 JSON.parse 出新对象，
                // 不写透的话锚定面板读到的还是旧 seconds），再重建面板。
                const num = Number(secsInp.value);
                if (data.ds && Array.isArray(data.ds.segments) && data.ds.segments[it.idx]) {
                    data.ds.segments[it.idx].seconds =
                        (secsInp.value === "" || !isFinite(num) || num <= 0)
                            ? null : Math.min(15, Math.max(0.5, num));
                }
                syncHint(secsInp.value);
                if (typeof rebuildAnchorPane === "function") rebuildAnchorPane();
            });
            title.append(secsInp, secsHint, el("span", "h3d-secs-hint", "秒"));
        }
        body.append(title);

        const seedTxt = isDone && seeds[idx + off] != null ? `种子 ${seeds[idx + off]}` : "";
        const seamTxt = isDone && seams[idx + off]
            ? `接缝 ${seams[idx + off][0]}${seams[idx + off][1] == null ? "" : ` / ${seams[idx + off][1]}dB`}` : "";
        const bridgeTxt = isDone && bridges[idx + off] != null ? `桥分 ${bridges[idx + off]}` : "";
        const meta = [seedTxt, seamTxt, bridgeTxt].filter(Boolean).join(" · ");
        if (meta) body.append(el("div", "h3d-cmeta", escapeHtml(meta)));

        /* 段级首尾帧锚定（卡片公共区，在 主提示词/具象化/设置 三页之上常驻）。
         * 以前这两个按钮挂在「③ 结果」的引用条里，可锚点是段级运行参数
         * （节点按它注入 keyframe latent），跟"结果框引用了哪些素材"根本不是一回事，
         * 摆在那儿会让人以为它只作用于结果框。一个段只有一组锚，放在外框才对。 */
        if (node && it.idx !== undefined) {
            const anchorBar = el("div", "h3d-anchorbar");
            anchorBar.append(el("label", "", "首尾帧锚定"));
            anchorBar.append(mkFrameBtns(node, it.idx, () => scheduleRefresh(240)));
            anchorBar.append(el("span", "h3d-secs-hint",
                "本段的起手帧 / 终点锚（段级，三栏共用）；清锚会同步摘掉结果框里的对齐指令"));
            body.append(anchorBar);
        }

        /* 提示词：切换式三页（主提示词/v2/设置；主框=最终进模型文本） */
        {
            const pool = (data.ds.ref_assets || []);
            const poolHint = pool.length
                ? `用 @${pool[0].label} 这样的标签引用素材（引用会显示成绿框，点 ✕ 取消）`
                : "上传参考图后可用 @标签 引用";
            /* 卡片轻量重绘（引用条建好后赋值；在此之前是空实现 = 无害）。
             * 引用 chips / 计数 / 绿框三处状态由它统一推进，任何"改了引用"的入口
             * 都立刻调它，不等 240ms 刷新（刷新在焦点守卫下会被丢掉）。 */
            let repaintCard = () => {};
            /* 富文本提示词框：@别名 显示为内联绿框。
             * 绿框里的 ✕ = 只取消**这一处**引用（引用 N 次点 N 下，N=0 时引用条
             *  对应 chip 自动置灰）；引用条 chip 旁的 ✕ 才是"清全部"。 */
            const ta = createPromptEditor({
                value: it.text || "",
                /* 别名表读**活的**节点状态，不用 collectData 的快照：上传/改名/删除素材后
                 * 快照会一直停在建卡那一刻，新素材的 @别名就永远渲染不成绿框。 */
                labels: () => {
                    let arr = data.ds.ref_assets;
                    if (node) { try { arr = getDs(node).ref_assets; } catch (e) { /* 回落快照 */ } }
                    return (arr || []).map((a) => a.label);
                },
                /* 别名 -> 素材信息 {kind,file,asset_id}（绿框据此挑缩略图/图标）。
                 * 同样读活的节点状态——上传/换类别后旧绿框的标识能立刻对得上。 */
                assets: () => {
                    let arr = data.ds.ref_assets;
                    if (node) { try { arr = getDs(node).ref_assets; } catch (e) { /* 回落快照 */ } }
                    const m = {};
                    for (const a of (arr || [])) {
                        if (a && a.label) {
                            m[a.label] = {
                                kind: a.kind || "image",
                                file: String(a.file || ""),
                                asset_id: String(a.asset_id || ""),
                            };
                        }
                    }
                    return m;
                },
                /* 项目目录：拼项目内 assets/… 的预览地址用（读活的控件值）。 */
                dir: () => getDirValue(node),
                onRemove: (label) => {
                    if (!node || it.idx === undefined) return;
                    /* 正文是唯一真相：applyPromptEdit 按新正文重算 refs，正好少一处。
                     * 这里**不再**跟一个 removeSegmentRef —— 那是"清全部"时代给
                     * removeTag 兜底的；现在 ✕ 只清一处，再减一次会让计数一次掉 2
                     *（引用 2 次点一下直接归零）。写回失败时才退回兜底。 */
                    if (!applyPromptEdit(node, it.idx, ta)) removeSegmentRef(node, it.idx, label);
                    repaintCard();                       // 引用条即时同步（以前要再点别的才刷新）
                    scheduleRefresh(240);
                },
            });
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
                attachAtComplete(ta, node);   // P3：@别名补全（只改文本，防抖落盘不变）
                registerPromptEditor(node, it.idx, ta);   // 程序改提示词时能同步到屏幕
            }
            /* 三页容器：主框 / v2分组 / 锚定设置（时长在标题行，锚定设置页放
             * 手动锚定 + 段级开关 + latent 保存）。页名就叫「锚定设置」——
             * 这一页现在的主角是手动锚定，叫「设置」看不出进去能改什么。 */
            const tabbar = el("div", "h3d-tabs");
            const paneMain = el("div", "h3d-tabpane");
            const paneV2 = el("div", "h3d-tabpane");
            const paneSet = el("div", "h3d-tabpane");
            const tabKey = `seg${it.idx}`;
            let curTab = _segTab.get(tabKey) || "main";
            const tabs = [["main", "主提示词"], ["v2", "具象化"], ["set", "锚定设置"]];
            const panes = { main: paneMain, v2: paneV2, set: paneSet };
            const paintTabs = () => {
                for (const [k, label] of tabs) {
                    const b = tabbar.querySelector(`[data-tab="${k}"]`);
                    if (b) b.classList.toggle("on", curTab === k);
                }
                for (const [k, pane] of Object.entries(panes)) pane.style.display = curTab === k ? "" : "none";
            };
            for (const [k, label] of tabs) {
                const b = el("button", "h3d-tab" + (curTab === k ? " on" : ""), label);
                b.type = "button"; b.dataset.tab = k;
                b.onclick = () => { curTab = k; _segTab.set(tabKey, k); paintTabs(); };
                tabbar.append(b);
            }
            body.append(tabbar);
            /* AI 优化设置放在**三页之外**的公共区：它是全链共用的一套（服务商 /
             * 输出语言 / 规则文件），跟「锚定设置」页里的本段开关不是一回事。
             * 以前挂在 ① 意图框里，既容易误点，也让人以为只对这段生效。 */
            if (node) {
                const optBar = el("div", "h3d-optsetbar");
                const bOptSet = el("button", "h3d-btn", "⚙ AI 优化设置");
                bOptSet.type = "button";
                bOptSet.title = "AI 提示词优化设置（服务商 / 模型 / 输出语言 / 规则文件）"
                    + "—— 全链共用，不是本段设置（本段设置在上面的「锚定设置」页）";
                bOptSet.onclick = () => openOptSettings(node);
                optBar.append(bOptSet, el("span", "h3d-secs-hint",
                    "全链共用：服务商 / 输出语言 / 规则文件（本段参数在「锚定设置」页）"));
                body.append(optBar);
            }
            /* 三栏各自独立的引用条：意图 / 剧本 / 结果 各一条，谁也不改谁。
             * 只有「③ 结果」那条进模型（ds.prompts + seg.refs，走官方 9/3/3）；
             * ①② 只是标注，写完只落在自己的正文里。 */
            const refBars = [];
            /* 活素材池（**数组**）：三栏引用条按它算引用；注意别和下面的别名表 liveAssets 撞名 */
            const poolNow = () => {
                if (!node) return pool;
                try { return (getDs(node).ref_assets || []); } catch (e) { return pool; }
            };
            const mkRefBar = (cfg) => {
                if (!node || it.idx === undefined) return null;
                const b = buildRefBar(Object.assign({
                    node, segIdx: it.idx, pool,
                    repaint: () => repaintCard(),
                    livePool: () => {
                        if (!node) return pool;
                        try { return (getDs(node).ref_assets || []); } catch (e) { return pool; }
                    },
                }, cfg));
                refBars.push(b);
                return b.el;
            };
            /* 卡片轻量重绘：先补结果框正文绿框（晚到的素材别名 → 成框并回写），
             * 再三条引用栏一起重画。聚焦锁定期间由刷新周期反复调用，
             * 也是 ✕ / chip 点击的即时刷新入口。 */
            repaintCard = () => {
                if (!ta.el || !ta.el.isConnected) return;
                let fixed = false;
                try { fixed = ta.normalizeLoose(); } catch (e) { fixed = false; }
                if (fixed && node && it.idx !== undefined) applyPromptEdit(node, it.idx, ta);
                try { ta.redrawIcons(); } catch (e) { /* 边界态忽略 */ }
                for (const b of refBars) { try { b.paint(); } catch (e) { /* 单条失败不影响其它 */ } }
            };
            registerCardPainter(repaintCard);
            /* 主框三段式：① 中文意图 → ② 剧本（扩写产物） → ③ 结果（进模型）。
             * 只有 ③ 进模型（ds.prompts[idx]），①②都只是给人/AI 看的中间稿。 */
            const segNow = (data.ds.segments || [])[it.idx] || {};
            const canEdit = !!node && it.idx !== undefined;
            /* 别名表读**活的**节点状态（与结果框同口径），@补全与绿框才跟得上素材变动 */
            const liveLabels = () => {
                let arr = data.ds.ref_assets;
                if (node) { try { arr = getDs(node).ref_assets; } catch (e) { /* 回落快照 */ } }
                return (arr || []).map((a) => a.label);
            };
            const liveAssets = () => {
                let arr = data.ds.ref_assets;
                if (node) { try { arr = getDs(node).ref_assets; } catch (e) { /* 回落快照 */ } }
                const m = {};
                for (const a of (arr || [])) {
                    if (a && a.label) {
                        m[a.label] = { kind: a.kind || "image", file: String(a.file || ""),
                            asset_id: String(a.asset_id || "") };
                    }
                }
                return m;
            };
            /* 素材调度挂上的引用（seg.refs）—— 与「正文里有没有 @」是两回事：
             * AI 扩写回填只写剧本正文、不会写 @，但 seg.refs 生成时真的会送图。
             * 引用条必须把它算进去，否则显示空白会被误读成"图没挂上"。 */
            const liveRefs = () => {
                if (node) {
                    try {
                        const s = (getDs(node).segments || [])[it.idx];
                        if (s && Array.isArray(s.refs)) return s.refs.slice();
                    } catch (e) { /* 回落快照 */ }
                }
                return Array.isArray(segNow.refs) ? segNow.refs.slice() : [];
            };
            /* 可折叠编辑区：三段统一外观，点标题栏收起/展开。
             * 抽成顶层 h3dPane —— 总提示词工作台的段卡用同一套外观。 */
            const mkPane = h3dPane;

            /* ---- ① 中文意图：最需要 @素材的一段（指定这段参考谁）---- */
            const pIntent = mkPane("① 中文意图（不进模型 · 可用 @素材）", true);
            const intentTa = createPromptEditor({
                value: String(segNow.intent_zh || ""),
                labels: liveLabels,
                assets: liveAssets,
                dir: () => getDirValue(node),
                /* 意图不参与 conditioning，绿框 ✕ 只改文本、不动 seg.refs */
                onRemove: () => {
                    if (canEdit) setSegmentField(node, it.idx, "intent_zh", intentTa.value);
                    scheduleRefresh(200);
                },
            });
            intentTa.placeholder = "一句话说清这段要什么：场景、人物、动作、情绪、镜头感觉。"
                + "例：夜晚便利店门口，@角色1 撑伞等车，霓虹倒映在积水里，缓慢推近。";
            if (canEdit) {
                intentTa.addEventListener("input",
                    () => setSegmentField(node, it.idx, "intent_zh", intentTa.value));
                intentTa.addEventListener("blur", () => {
                    setSegmentField(node, it.idx, "intent_zh", intentTa.value);
                    scheduleRefresh(200);
                });
                attachAtComplete(intentTa, node);
            } else {
                intentTa.disabled = true;
            }
            pIntent.body.append(intentTa.el || intentTa);
            /* ① 的引用条：纯标注（写完只落在 intent_zh 正文里，不进模型、不写 seg.refs） */
            const refIntent = mkRefBar({
                title: "引用素材 · 意图",
                editor: canEdit ? intentTa : null,
                readRefs: () => refsFromText(String((canEdit ? intentTa.value : segNow.intent_zh) || ""),
                    poolNow()),
                commit: (ed) => {
                    setSegmentField(node, it.idx, "intent_zh", ed.value);
                    scheduleRefresh(200);
                },
                gate: false, showFrames: false, tplKey: "i" + it.idx,
            });
            if (refIntent) pIntent.body.append(refIntent);
            const intentBar = el("div", "h3d-actions");
            const bExpand = el("button", "h3d-btn h3d-btn-cyan", "✨ AI扩写 → 剧本");
            bExpand.title = "把一句中文意图扩写成一大段能填满时长的中文剧本（自由格式，"
                + "不套官方字段），生成后可就地改再回填「② 剧本」；"
                + "时长是范围，模型在范围内自定秒数。格式由 ② 的「提示词优化」负责";
            bExpand.disabled = !canEdit;
            bExpand.onclick = () => openExpandModal(node, it.idx,
                (segNow.prompt_v2 && typeof segNow.prompt_v2 === "object") ? segNow.prompt_v2 : null);
            intentBar.append(bExpand);
            pIntent.body.append(intentBar);
            paneMain.append(pIntent.g);

            /* ---- ② 剧本：AI 扩写产物，可手工改后再优化 ---- */
            const pScript = mkPane("② 剧本（扩写产物 · 可手工改）", true);
            const scriptTa = createPromptEditor({
                value: String(segNow.script || ""),
                labels: liveLabels,
                assets: liveAssets,
                dir: () => getDirValue(node),
                onRemove: () => {
                    if (canEdit) setSegmentField(node, it.idx, "script", scriptTa.value);
                    scheduleRefresh(200);
                },
            });
            scriptTa.placeholder = "点上方「✨ AI扩写」生成，或直接粘贴官方格式剧本"
                + "（integrated_multimodal_description: …）";
            if (canEdit) {
                scriptTa.addEventListener("input",
                    () => setSegmentField(node, it.idx, "script", scriptTa.value));
                scriptTa.addEventListener("blur", () => {
                    setSegmentField(node, it.idx, "script", scriptTa.value);
                    scheduleRefresh(200);
                });
                attachAtComplete(scriptTa, node);
            } else {
                scriptTa.disabled = true;
            }
            pScript.body.append(scriptTa.el || scriptTa);
            /* ② 的引用条：正文里的 @ 是纯标注（写完只落在 script 正文里，不进模型、
             * 不写 seg.refs）；但**素材调度挂的 seg.refs 生成时真的会送图**。
             * AI 扩写回填只写剧本正文、不会写 @，所以这里要把 seg.refs 一并点亮
             * 并注明来源 —— 否则引用条一片空白，会被误读成"图没挂上"。 */
            const scriptRefsInText = () => refsFromText(
                String((canEdit ? scriptTa.value : segNow.script) || ""), poolNow());
            const refScript = mkRefBar({
                title: "引用素材 · 剧本",
                editor: canEdit ? scriptTa : null,
                readRefs: () => {
                    const out = scriptRefsInText().slice();
                    for (const l of liveRefs()) {
                        if (l && !out.includes(l)) out.push(l);
                    }
                    return out;
                },
                note: () => {
                    const inText = scriptRefsInText();
                    const n = liveRefs().filter((l) => l && !inText.includes(l)).length;
                    return n ? `其中 ${n} 个来自素材调度（生成时真的送图）` : "";
                },
                inherited: () => {
                    const inText = scriptRefsInText();
                    return liveRefs().filter((l) => l && !inText.includes(l));
                },
                commit: (ed) => {
                    setSegmentField(node, it.idx, "script", ed.value);
                    scheduleRefresh(200);
                },
                gate: false, showFrames: false, tplKey: "s" + it.idx,
            });
            if (refScript) pScript.body.append(refScript);
            const scriptBar = el("div", "h3d-actions");
            const bOptRun = el("button", "h3d-btn h3d-btn-cyan", "✨ 提示词优化 → 结果");
            bOptRun.title = "把剧本优化成最终结果写进「③ 结果」；剧本本身保留，可反复重优化";
            bOptRun.disabled = !canEdit;
            bOptRun.onclick = () => runOptForSegment(node, it.idx, ta, { btn: bOptRun }, scriptTa);
            scriptBar.append(bOptRun);
            pScript.body.append(scriptBar);
            paneMain.append(pScript.g);

            /* ---- ③ 结果：最终进模型文本 ---- */
            const pResult = mkPane("③ 结果（最终进模型）", true);
            pResult.body.append(ta.el || ta);
            /* ③ 的引用条：唯一进模型的那条（gate=true → 走官方 9/3/3、写 seg.refs） */
            const refResult = mkRefBar({
                title: "引用素材",
                editor: canEdit ? ta : null,
                readRefs: () => refsFromText(String(ta.value || ""), poolNow()),
                commit: (ed) => applyPromptEdit(node, it.idx, ed),
                /* showFrames 已关：首/尾帧锚定搬到卡片公共区（三栏之上），
                 * 它是段级运行参数，不属于"结果框的引用"。 */
                gate: true, showFrames: false, tplKey: it.idx,
            });
            if (refResult) pResult.body.append(refResult);
            /* 结果工具条：原稿切换 / 双向同步具象化（优化入口在 ② 剧本区） */
            const optbar = el("div", "h3d-actions");
            optbar.dataset.optbar = String(it.idx);
            pResult.body.append(optbar);
            paneMain.append(pResult.g);
            try { paintOptbar(optbar, node, data, it.idx, ta); } catch (e) {}

            /* 源码覆盖已彻底移除（有结果框就够了）。旧存档若残留 override_text，
             * 它会取代一切编译结果——必须让用户看见并能一键清掉，否则是个暗坑。 */
            {
                const pvNow = (segNow.prompt_v2 && typeof segNow.prompt_v2 === "object")
                    ? segNow.prompt_v2 : null;
                if (pvNow && String(pvNow.override_text || "").trim()) {
                    const row = el("div", "h3d-v2row");
                    row.append(el("span", "h3d-secs-hint",
                        "本段残留「源码覆盖」文本，编译时会取代上面所有内容。"));
                    const clr = el("button", "h3d-btn h3d-btn-danger", "清除覆盖");
                    clr.style.cssText = "padding:2px 8px;font-size:11px";
                    clr.onclick = () => {
                        setPromptV2Field(node, it.idx, (p) => { p.override_text = null; });
                        scheduleRefresh(80);
                    };
                    row.append(clr);
                    pResult.body.append(row);
                }
            }

            /* 引用调度已迁入具象化参考组（分段素材调度，933 上限）；引用语模板已并入上方统一引用条 */

            /* ---- 「设置」页：只放段级开关，按**作用面**分两区 ----
             *   影响本段生成：锚定 / 首尾帧图引用 / 上链与执行两个开关
             *   只影响落盘：本段 latent 保存
             * 原先这几块平铺在一起、权重一样，看的人分不清哪块跟哪块有关，也看不出
             * 改哪个会改变出片、改哪个只是省磁盘。 */
            if (node && it.idx !== undefined) {
                const seg = (data.ds.segments && data.ds.segments[it.idx]) || defaultSegment();
                const secGen = el("div", "h3d-setsec");
                secGen.append(el("div", "h3d-setsec-title", "影响本段生成"));
                const secDisk = el("div", "h3d-setsec");
                secDisk.append(el("div", "h3d-setsec-title", "只影响落盘"));
                paneSet.append(secGen, secDisk);

                /* —— 手动锚定（双轨时间线，见 web/h3d_anchor.js）：anchor 存
                 *    ds.segments[i].anchors，随 save_prompts 一起落盘；模块未加载
                 *    （如旧前端）则跳过，不影响其余设置 —— */
                if (window.H3Anchor && window.H3Anchor.buildAnchorPanel) {
                    // dir / setAnchors / frameLen 是**注入给子模块的宿主访问器**：本文件的
                    // getDirValue / setSegmentField / segmentFrames 都是模块级函数、并不在
                    // window 上，子模块按全局名去找会当场抛错（曾因此整块「段落卡片渲染
                    // 失败」）。顺带把「谁先加载」这条隐式契约也消掉了。
                    // 面板挂在可清空容器里：分段时间变化时整体重建（见 secsInp change）。
                    const anchorWrap = el("div");
                    rebuildAnchorPane = () => {
                        // 重建前记住这块折叠区的展开状态：重建是整块换 DOM，
                        // 不继承就会在改秒数的瞬间「啪」地自动收起来（很莫名）。
                        const oldBox = anchorWrap.querySelector("details");
                        const wasOpen = !!(oldBox && oldBox.open);
                        anchorWrap.innerHTML = "";
                        const anchorBox = window.H3Anchor.buildAnchorPanel({
                            node, data, idx: it.idx,
                            refresh: () => scheduleRefresh(60),
                            dir: () => getDirValue(node),
                            setAnchors: (arr) => setSegmentField(node, it.idx, "anchors", arr),
                            // 本段总帧数**只从这一个函数出**（段卡标题那个「≈N帧」也是它）：
                            // 面板曾经自己再算一遍（还用了向上对齐），于是目标轨刻度与
                            // 分段时长对不上——同一个数只许有一个算法、一个来源。
                            frameLen: () => segmentFrames(node, data.ds.segments[it.idx]),
                        });
                        if (anchorBox) {
                            if (wasOpen) anchorBox.open = true;
                            anchorWrap.append(anchorBox);
                        }
                    };
                    rebuildAnchorPane();
                    secGen.append(anchorWrap);
                }

                /* 首尾帧图段级引用：勾选本段是否参考链首/链尾锚（资产标注或旧槽位）。
                 * 未设置=默认（首段 i2v 首帧、末段尾帧终点锚，与旧档一致）；
                 * 中段勾首帧图=头部身份锚（keyframe 注入抑制长链漂移）；
                 * 任意段勾尾帧图=该段末帧锚（优先于段尾锚） */
                if (data.ds.first_frame || data.ds.end_frame
                        || (data.ds.ref_assets || []).some((x) => Array.isArray(x.roles) && x.roles.length)) {
                    const n = (data.ds.prompts || data.ds.segments || []).length;
                    const def = [];
                    if (data.ds.first_frame && it.idx === 0) def.push("首帧图");
                    if (data.ds.end_frame && it.idx === n - 1) def.push("尾帧图");
                    const cur = Array.isArray(seg.frame_refs) ? seg.frame_refs : def;
                    const roleFile = (role) => {
                        const hit = (data.ds.ref_assets || [])
                            .find((x) => Array.isArray(x.roles) && x.roles.includes(role));
                        return hit ? hit.file : "";
                    };
                    const row = el("div", "h3d-refrow");
                    row.append(el("label", "", "首尾帧图"));
                    [["首帧图", data.ds.first_frame || roleFile("首帧图")],
                     ["尾帧图", data.ds.end_frame || roleFile("尾帧图")]].forEach(([name, file]) => {
                        if (!file) return;
                        const on = cur.includes(name);
                        const chip = el("button", "h3d-refchip" + (on ? " on" : ""));
                        chip.type = "button";
                        chip.title = name === "首帧图"
                            ? "勾选后本段参考链首锚（资产标注/旧槽位）：首段=i2v 起手帧；中段=头部身份锚"
                                + "（keyframe 注入，抑制长链漂移）。不勾=本段不参考首帧图"
                            : "勾选后本段末帧锚定链尾锚（同位置唯一锚，优先于段尾锚）；"
                                + "不勾=回落段 tail_src/旧尾帧锚定";
                        const im = document.createElement("img");
                        im.loading = "lazy";
                        im.src = assetPreviewUrl(getDirValue(node), file);
                        im.onerror = () => im.remove();
                        chip.append(im, document.createTextNode(name));
                        chip.onclick = () => { toggleSegmentFrameRef(node, it.idx, name); scheduleRefresh(80); };
                        row.append(chip);
                    });
                    if (!Array.isArray(seg.frame_refs)) {
                        row.insertAdjacentHTML("beforeend", '<span class="h3d-secs-hint">未设置=默认</span>');
                    }
                    secGen.append(row);
                }

                /* 两个开关并成一行。语义取**反向勾选**（用户拍板）：勾选 = 跳过，
                 * 不勾 = 跟随全局默认开——「跟随全局」从来不是一个可点按钮该表达的
                 * 状态，它只是"没勾"本身；旧版勾选框（自动引用上段）+旁置按钮的
                 * 三件套让人分不清勾的到底是"开"还是"跟全局"。
                 * 数据仍是三态：不勾写 null（跟随全局），勾写显式 false（跳过）。 */
                const swRow = el("div", "h3d-setrow");
                // —— 跳过自动引用上段（勾 = 与上段断链硬切；不勾 = 跟随全局，默认无缝续拍） ——
                const refRow = el("label", "h3d-unlink h3d-offrow");
                const refCb = document.createElement("input");
                refCb.type = "checkbox";
                refCb.checked = seg.auto_ref === false || seg.unlink === true;
                refRow.append(refCb, document.createTextNode("🚫 跳过自动引用上段"));
                refRow.title = "勾选=本段与上段完全断开（不注入桥、不裁头、不做接缝处理，段间硬切）；"
                    + "不勾=跟随全局「视频延续」设置（默认无缝续拍）";
                refCb.onchange = () => {
                    // 不勾写 null（回到跟随全局），勾写显式 false（跳过）；unlink 双写兼容旧后端
                    setSegmentField(node, it.idx, "auto_ref", refCb.checked ? false : null);
                    setSegmentField(node, it.idx, "unlink", refCb.checked);
                    scheduleRefresh(60);
                };
                // —— 跳过自动按序生成（勾 = 本段保留但跳过执行、不进成片；不勾 = 跟随全局） ——
                const seqRow = el("label", "h3d-unlink h3d-offrow");
                const seqCb = document.createElement("input");
                seqCb.type = "checkbox";
                seqCb.checked = seg.auto_seq === false || seg.disabled === true;
                seqRow.append(seqCb, document.createTextNode("⏸ 跳过自动按序生成"));
                seqRow.title = "勾选=本段保留但跳过执行、不进成片（恢复零成本，已完成段沿用存档）；"
                    + "不勾=跟随全局（默认按序生成）";
                seqCb.onchange = () => {
                    setSegmentField(node, it.idx, "auto_seq", seqCb.checked ? false : null);
                    setSegmentField(node, it.idx, "disabled", seqCb.checked);
                    scheduleRefresh(60);
                };
                swRow.append(refRow, seqRow);
                secGen.append(swRow);

                /* —— 本段 latent 保存（null=跟随默认「全存」）——
                 * 落盘策略的底层是 {mode: all|range|tail|off, start_f, end_f, tail_f,
                 * split_av, save_seg, save_all}，但对用户只有三件事：存全段 / 只存尾部
                 * N 帧 / 压根不存。字段不平铺——那是把后端 schema 直接摊给人看。 */
                const ls = (seg.latent_save && typeof seg.latent_save === "object") ? seg.latent_save : {};
                const lsBox = el("details", "h3d-adv");
                lsBox.innerHTML = "<summary>💾 本段 latent 保存</summary>";
                const lsGrid = el("div", "h3d-adv-grid");
                const modeSel = document.createElement("select");
                modeSel.className = "h3d-select";
                // seed 值：旧档的 range 不在三选里，落回「存全段」显示但**不写回**——
                // 只要用户不动它，旧设置原样生效（见下方提示行）。
                const curMode = (ls.mode === "tail" || ls.mode === "off") ? ls.mode : "follow";
                for (const [v, t] of [["follow", "存全段（默认）"], ["tail", "只存尾部"],
                                      ["off", "不存（用完即弃）"]]) {
                    const o = document.createElement("option");
                    o.value = v; o.textContent = t;
                    if (v === curMode) o.selected = true;
                    modeSel.append(o);
                }
                modeSel.title = "本段 latent 落盘策略。存下来的 latent 是「选段 / 选 latent 作锚源」"
                    + "与续跑沿用的原料；不存就只留在存档外的临时结果里，省磁盘但少一条退路";
                const tailWrap = el("div", "h3d-adv-grid");
                tailWrap.style.display = curMode === "tail" ? "grid" : "none";
                const tailInp = document.createElement("input");
                tailInp.type = "number";
                tailInp.className = "h3d-secs";
                tailInp.min = "0"; tailInp.step = "1";
                tailInp.value = (ls.tail_f || 39);
                tailInp.title = "只保留本段最后 N 帧的 latent（默认 39 帧 ≈ 1.6 秒 @24fps）";
                tailInp.onchange = () => {
                    const n = parseInt(tailInp.value, 10);
                    setSegmentField(node, it.idx, "latent_save",
                        { ...ls, mode: "tail", tail_f: Number.isFinite(n) && n > 0 ? n : 39 });
                    scheduleRefresh(60);
                };
                modeSel.onchange = () => {
                    const v = modeSel.value;
                    tailWrap.style.display = v === "tail" ? "grid" : "none";
                    setSegmentField(node, it.idx, "latent_save",
                        v === "follow" ? null : { ...ls, mode: v, tail_f: ls.tail_f || 39 });
                    scheduleRefresh(60);
                };
                lsGrid.append(el("span", "h3d-secs-hint", "保存"), modeSel);
                tailWrap.append(el("span", "h3d-secs-hint", "尾部帧数"), tailInp);
                lsBox.append(lsGrid, tailWrap);
                // 旧设置不再有编辑入口，但也不能装作没看见：说清它还在生效、怎么替换。
                const legacyLs = [];
                if (ls.mode === "range") legacyLs.push("指定帧范围");
                if (ls.split_av) legacyLs.push("图像/音频分开存");
                if (legacyLs.length) {
                    lsBox.append(el("div", "h3d-secs-hint",
                        `本段是旧设置「${legacyLs.join(" / ")}」：界面上不再提供这两项，`
                        + "不动它就一直是旧设置；改选上面任一项即替换。"));
                }
                secDisk.append(lsBox);
            }
            /* v2分组进Tab（主框=最终文本，v2可改+一键同步回主框） */
            if (node && it.idx !== undefined) {
                renderPromptV2Panel(paneV2, node, data, it.idx);
            }
            paintTabs();
            body.append(paneMain, paneV2, paneSet);
        }

        const actions = el("div", "h3d-actions");
        if (!node) {
            actions.append(el("span", "h3d-hint", "只读（画布上未找到节点）"));
        } else {
            if (isDisabled) {
                /* 已禁用段：恢复入口（勾选框在「分段处理」面板内也可取消勾选；
                   重摇/继续/二采均无意义——不执行不进成片） */
                const back = el("button", "h3d-btn h3d-btn-cyan", "▶ 重新上链");
                back.title = "恢复本段自动执行：已完成段直接沿用存档（零成本），"
                    + "未完成段下次运行起正常采样；前后接缝自动重新衔接";
                back.onclick = () => {
                    setSegmentField(node, it.idx, "auto_seq", true);
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
            }
            if (node && it.idx !== undefined) {
                const rm = el("button", "h3d-btn h3d-btn-danger", "✕ 删除此段");
                rm.title = "删除这一段提示词（其后段落自动前移）";
                rm.onclick = () => removePromptSegment(node, it.idx);
                actions.append(rm);
            }
        }
        /* 单段重新二采：已完成段随时可单独补做/重做二采，不动其他段
           ——显示条件放宽到"选了模型"即可见（未做过二采的段也能补做）；
           序章是外部成品素材，不做二采；已禁用段不进成片，不做二采 */
        if (isDone && !isDisabled && node && state?.dir
                && (upRec?.done || data.ds?.upscale?.model)) {
            const rst = el("button", "h3d-btn", "🔄 重新二采此段");
            rst.title = "清本段二采记录并立即重做：latent 存档载入 → 神经放大重采样 → 覆盖段视频"
                + "（不做视频编解码）。其他段不受影响。"
                + "参数未变时重渲=同输出；效果不满意请调右栏二采参数";
            rst.onclick = () => doUpscaleSeg(rst, state.dir, idx + off + 1, idx + 1, done, total);
            actions.append(rst);
        }
        body.append(actions);

        card.append(body);
        wrap.append(card);
    });

    return wrap;
}

/** 段卡构建兜底：一卡挂掉只显示该卡的错误，不让整条流水线（连带左右栏）陪葬。
 *  异常信息直接摆出来——否则用户只看到"卡片空了"，分不清是没数据还是崩了。 */
function buildCardsSafe(data) {
    try {
        return buildCards(data);
    } catch (e) {
        console.error("[h3-director] 段落卡片构建失败：", e);
        const wrap = el("div", "h3d-cards");
        const box = el("div", "h3d-card todo");
        const b = el("div", "h3d-cbody");
        b.append(el("div", "h3d-ctitle", "⚠ 段落卡片渲染失败"));
        b.append(el("div", "h3d-err",
            escapeHtml(String((e && e.message) || e))
            + "<br>其余分区不受影响；改回上一个可用状态（或刷新页面）通常可恢复。"));
        box.append(b);
        wrap.append(box);
        return wrap;
    }
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
        im.src = assetPreviewUrl(getDirValue(node), file);
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
    labelInp.title = "素材标签：提示词用 @标签 引用；回车或失焦提交，重名自动加后缀";
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
    const textDriven = (ds.segments || []).every((s) => !(s.refs || []).length);
    usage.innerHTML = `<span class="h3d-chip">${KIND_ICON[kind]}${KIND_NAME[kind]}</span>`
        + (usedBy ? `<span class="h3d-chip ok">${usedBy} 段指定</span>` : "")
        + (textDriven && (ds.segments || []).length ? '<span class="h3d-chip cyan">文本标签驱动</span>' : "");
    copy.append(labelInp, quick, usage,
        el("small", "", `${escapeHtml(file)} · 提示词写 @${escapeHtml(a.label)} → &lt;${KIND_TOKEN[kind]} k&gt;`));

    const acts = el("div", "h3d-asset-acts");
    const rm = el("button", "h3d-btn h3d-btn-danger", "✕");
    rm.title = "移除该素材（画布镜像节点隐藏，连线保留）";
    rm.onclick = () => { removeRefImage(node, idx); };
    acts.append(rm);
    card.append(thumb, copy, acts);
    return card;
}

/* assetCard / labeledAssetCard / syncMirrors（画布镜像）保留复用。 */

/* ---- 链参数（面板直写画布控件）：基础设置 + 视频延续（关键帧设置/检测重摇） ----
 * 基础设置 = 原生工作流画幅/时长/采样类；视频延续 = 段间引导相关（大折叠套两子折叠，
 * 大小均可伸缩）。ADVANCED_DEFS 保持全量（paramsSig/旧逻辑兼容口径）。 */
const BASIC_DEFS = [W_AR, W_MP, W_DUR, W_SEED, "步数", "CFG", "采样器", "调度器",
    "一采编码", "存档目录", W_WIDTH, W_HEIGHT];
const KEYFRAME_DEFS = ["引导帧数", "锚定加噪", "递减锚定", "审片模式",
    "自动保存", "自动成片", "重跑起始段"];
const SEAM_DEFS = ["桥帧门控", "清晰度阈值", "回退上限",
    "接缝重摇", "重摇阈值", "重摇上限"];
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
        "<strong>链参数</strong><small>基础设置 + 视频延续（关键帧/检测重摇）</small>"));
    if (!node) {
        sec.append(el("div", "h3d-empty", "画布上未找到节点，参数面板不可用"));
        return;
    }
    const ar = String(getWidgetValue(node, W_AR) ?? "");
    /* —— 基础设置（原生画幅/时长/采样类，默认展开） —— */
    const basic = el("details", "h3d-adv");
    basic.open = true;
    basic.innerHTML = "<summary>📐 基础设置（分辨率 / 时长 / 采样器 / 调度器）</summary>";
    const bgrid = el("div", "h3d-adv-grid");
    for (const name of BASIC_DEFS) {
        if (name === W_WIDTH || name === W_HEIGHT) {
            if (ar !== "自定义") continue;
            bgrid.append(renderWidgetField(node, name));
            continue;
        }
        bgrid.append(renderWidgetField(node, name, PARAM_LABELS[name]));
    }
    if (AR_RATIO[ar]) {
        const badgeTxt = canvasBadgeText(node);
        if (badgeTxt) {
            const b = el("div", "h3d-convbadge", `${escapeHtml(badgeTxt)} · 32倍数对齐`);
            b.title = "官方 Resolution Selector 同款换算：1MP=1024×1024，两侧各自 round 对齐 32 倍数";
            bgrid.append(b);
        }
    }
    basic.append(bgrid);
    sec.append(basic);
    /* —— 视频延续（大折叠套两子折叠，大小均可伸缩） —— */
    const cont = el("details", "h3d-adv");
    cont.open = true;
    cont.innerHTML = "<summary>🔗 视频延续（段间引导 / 关键帧 / 接缝）</summary>";
    const cwrap = el("div", "h3d-adv-grid");
    const kf = el("details", "h3d-adv");
    kf.open = false;
    kf.innerHTML = "<summary>📌 关键帧设置（尾部保存 / 注入帧数 / 加噪 / 自动保存成片）</summary>";
    const kgrid = el("div", "h3d-adv-grid");
    for (const name of KEYFRAME_DEFS) kgrid.append(renderWidgetField(node, name));
    kgrid.append(el("div", "h3d-hint",
        "尾部 latent 保存量与分段注入帧数在各段「锚定设置」里按段覆盖（分段优先，空=跟随此处）。"));
    kf.append(kgrid);
    const seam = el("details", "h3d-adv");
    seam.open = false;
    seam.innerHTML = "<summary>🩺 检测重摇（桥帧门控 / 接缝重摇）</summary>";
    const sgrid = el("div", "h3d-adv-grid");
    for (const name of SEAM_DEFS) sgrid.append(renderWidgetField(node, name));
    seam.append(sgrid);
    cwrap.append(kf, seam);
    cont.append(cwrap);
    sec.append(cont);
    sec.append(el("div", "h3d-foot",
        "「锚定设置」里同名子选项按段覆盖此处（分段优先）；「生成模式」由左侧模式条控制。"));
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
        up.chunk !== false,
        up.device ?? "auto", up.force_unload === true,
        up.size_mode ?? "倍率", up.target_w ?? 0, up.target_h ?? 0, up.megapixels ?? 0,
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
        + "单段补做/重做：段卡片「🔄 重新二采此段」（仅生成段；序章不做二采）";
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
        /* 神经放大开关：关掉 = 只做低强度重采样精化，不加载放大网络 */
        const upOnly = [];
        function showUp() {
            upOnly.forEach((f) => { f.style.display = up.enlarge ? "" : "none"; });
        }
        const enField = el("div", "h3d-param");
        enField.append(el("label", "", "神经放大"));
        const enRow = el("div", "h3d-seedrow");
        const enCb = document.createElement("input");
        enCb.type = "checkbox";
        enCb.checked = up.enlarge !== false;
        enCb.title = "勾选（默认）：放大网络先把 latent 超分，再在高清 latent 上低强度重采样精化——产物是放大后的高清视频。"
            + "取消勾选：跳过放大，直接在原分辨率 latent 上做低强度重采样精化——产物仍是原分辨率，"
            + "且完全不加载放大网络（省显存、省加载时间，也没有放大模型带来的细节增益）。";
        enCb.onchange = () => {
            up.enlarge = enCb.checked;
            setUpscaleField(node, "enlarge", enCb.checked);
            showUp();
        };
        enRow.append(enCb);
        enField.append(enRow);
        body.append(enField);

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
        upOnly.push(modelField);

        /* 网络架构：自动（按权重判定，默认）/ 2D 残差骨干 / 纯 3D 卷积 */
        const archField = el("div", "h3d-param");
        archField.append(el("label", "", "网络架构"));
        const archSel = document.createElement("select");
        archSel.className = "h3d-select";
        archSel.title = "自动（默认，推荐）：按权重自动判定——有 resizer. 前缀键=2D、"
            + "conv_in.weight 是 5 维=3D、4 维=2D，都判不出时 2D/3D 各试装一次取缺键少的；\n"
            + "2D=残差骨干+时间卷积（快，上游默认）；3D=纯 3D 卷积（时序一致性更好）。\n"
            + "换模型不用再手动切这一项；显式指定时若与权重不符会直接报错（提示改回自动）。"
            + "层数/通道数仍按权重自动推断。";
        for (const [v, t] of [["auto", "自动"], ["2D", "2D"], ["3D", "3D"]]) {
            const o = document.createElement("option");
            o.value = v;
            o.textContent = t;
            if (v === up.arch) o.selected = true;
            archSel.append(o);
        }
        archSel.onchange = () => setUpscaleField(node, "arch", archSel.value);
        archField.append(archSel);
        body.append(archField);
        upOnly.push(archField);

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
        upOnly.push(precField);

        /* 设备：自动（交给 ComfyUI 调度，默认）/ cuda / rocm / cpu */
        const devField = el("div", "h3d-param");
        devField.append(el("label", "", "设备"));
        const devSel = document.createElement("select");
        devSel.className = "h3d-select";
        devSel.title = "放大网络运行设备：自动（默认，交给 ComfyUI 的 intermediate_device"
            + " + 空闲显存纠偏，与主链协调最稳）；cuda/rocm/cpu 显式指定（排错或专用卡用）。"
            + "rocm 仍映射为 cuda 设备对象，仅日志区分（本地无 ROCm 环境，未验证）。"
            + "显式指定才进二采指纹";
        for (const [v, t] of [["auto", "自动"], ["cuda", "cuda"], ["rocm", "rocm"], ["cpu", "cpu"]]) {
            const o = document.createElement("option");
            o.value = v;
            o.textContent = t;
            if (v === up.device) o.selected = true;
            devSel.append(o);
        }
        devSel.onchange = () => setUpscaleField(node, "device", devSel.value);
        devField.append(devSel);
        body.append(devField);
        upOnly.push(devField);

        /* 3D 时序分块：长段按 32 帧分块前向（省显存 + 治末端闪烁），2D 不受影响 */
        const ckField = el("div", "h3d-param");
        ckField.append(el("label", "", "时序分块"));
        const ckRow = el("div", "h3d-seedrow");
        const ckCb = document.createElement("input");
        ckCb.type = "checkbox";
        ckCb.checked = up.chunk !== false;
        ckCb.title = "仅 3D 架构生效（2D 是逐帧卷积，不分块）：长段按时序切成 32 帧一块、"
            + "块间带 overlap 线性融合后拼回——显存峰值随帧数不再线性增长，末端帧也"
            + "不会因为缺右侧上下文而闪烁。默认开；关掉=整段一次前向（短段更快，"
            + "输出与分块版仅有数值噪声级差异）。关掉才进二采指纹";
        ckCb.onchange = () => setUpscaleField(node, "chunk", ckCb.checked);
        ckRow.append(ckCb);
        ckField.append(ckRow);
        body.append(ckField);
        upOnly.push(ckField);

        /* 强制卸载：每段二采后把放大网络从缓存删掉 + soft_empty_cache（下段重载） */
        const fuField = el("div", "h3d-param");
        fuField.append(el("label", "", "强制卸载"));
        const fuRow = el("div", "h3d-seedrow");
        const fuCb = document.createElement("input");
        fuCb.type = "checkbox";
        fuCb.checked = up.force_unload === true;
        fuCb.title = "每段二采结束后把放大网络从缓存里彻底删掉 + 调 soft_empty_cache"
            + "——下段重新从磁盘加载（换取最大显存/内存头寸，代价是每段重载 ~1s）。"
            + "默认关（段间保留缓存零加载）。多段链后段比首段更易 OOM 时再开。"
            + "开了才进二采指纹";
        fuCb.onchange = () => setUpscaleField(node, "force_unload", fuCb.checked);
        fuRow.append(fuCb);
        fuField.append(fuRow);
        body.append(fuField);
        upOnly.push(fuField);

        /* 目标尺寸模式：倍率 / 目标尺寸 / 百万像素 —— 三选一，按模式显示对应字段 */
        const sizeField = el("div", "h3d-param");
        sizeField.append(el("label", "", "目标尺寸"));
        const sizeSel = document.createElement("select");
        sizeSel.className = "h3d-select";
        sizeSel.title = "倍率：latent H/W 同乘一个系数（现状）；"
            + "目标尺寸：直接给像素宽×高（latent 取偶对齐到像素 32 倍数，可能略小于输入）；"
            + "百万像素：按基础宽高比换算总像素数。三种模式都只放大 H/W，时间维不变";
        for (const m of UP_SIZE_MODES) {
            const o = document.createElement("option");
            o.value = m;
            o.textContent = m;
            if (m === up.size_mode) o.selected = true;
            sizeSel.append(o);
        }
        sizeSel.onchange = () => {
            /* 本地 up 是 getDs 的 JSON 快照（非活引用）：同步改快照 + 直接重渲染
               条件字段（镜像上方 enlarge 勾选的处理模式）——sig 重渲染在下拉
               聚焦时被抑制（contains(activeElement)），不等失焦字段就该切换 */
            up.size_mode = sizeSel.value;
            setUpscaleField(node, "size_mode", sizeSel.value);
            renderSizeFields();
            const tgt = upTargetCanvas(node, up);
            if (tgt) {
                tgtBadge.textContent =
                    `目标画布 ${escapeHtml(tgt)}（latent 偶数对齐 · 时间维不变）`;
                tgtBadge.style.display = "";
            } else {
                tgtBadge.style.display = "none";
            }
        };
        sizeField.append(sizeSel);
        body.append(sizeField);
        upOnly.push(sizeField);

        const sizeFieldsWrap = el("div", "h3d-size-fields");
        function renderSizeFields() {
            sizeFieldsWrap.replaceChildren();
            const m = up.size_mode || "倍率";
            if (m === "目标尺寸") {
                sizeFieldsWrap.append(upNumField("目标宽", up.target_w, 64, 8192, 8,
                    "目标像素宽（latent 取偶对齐后实际可能略小，对齐到 32 倍数）",
                    (v) => { up.target_w = v; setUpscaleField(node, "target_w", v); }));
                sizeFieldsWrap.append(upNumField("目标高", up.target_h, 64, 8192, 8,
                    "目标像素高",
                    (v) => { up.target_h = v; setUpscaleField(node, "target_h", v); }));
            } else if (m === "百万像素") {
                sizeFieldsWrap.append(upNumField("百万像素", up.megapixels, 0.1, 16.0, 0.1,
                    "目标总像素数（按基础宽高比分配宽高）；1MP≈1024×1024",
                    (v) => { up.megapixels = v; setUpscaleField(node, "megapixels", v); }));
            } else {
                sizeFieldsWrap.append(upNumField("放大倍率", up.scale, 1.0, 4.0, 0.1,
                    "latent H/W 同乘（时间维不变）；目标画布见下方徽章。倍率 1.0 = 纯二采不放大；"
                    + "想连放大网络都不加载就取消上方「神经放大」",
                    (v) => { up.scale = v; setUpscaleField(node, "scale", v); }));
            }
        }
        renderSizeFields();
        body.append(sizeFieldsWrap);
        upOnly.push(sizeFieldsWrap);
        showUp();
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

        /* 目标画布徽章（latent 偶数对齐 = 像素 32 倍数，与后端 target_hw 同口径）。
           函数作用域声明（上方 sizeSel.onchange 的闭包要就地刷新它）；画幅未知
           时隐藏而非不创建，避免闭包引用悬空 */
        const tgtBadge = el("div", "h3d-convbadge", "");
        tgtBadge.title = "基础画布 × 倍率后按 latent 偶数（=像素 32 倍数）对齐；"
            + "超过 2.5MP 时后端会警告显存压力";
        const _tgt = upTargetCanvas(node, up);
        if (_tgt) {
            tgtBadge.textContent =
                `目标画布 ${escapeHtml(_tgt)}（latent 偶数对齐 · 时间维不变）`;
        } else {
            tgtBadge.style.display = "none";
        }
        body.append(tgtBadge);
        if (up.scale > 2.0 && up.size_mode === "倍率") {
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
            + "单段重做用段卡片「🔄 重新二采此段」；序章为外部成品素材不二采，成片拼接时缩放对齐");
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

/* ---- 总提示词工作台：三框（① 意图 → ② 剧本 → ③ 结果） ----
 * 历史形态：一个巨大的文本框里用「意图：/剧本：/提示词：」行内标签混写，改一层要
 * 在长正文里翻找，且**没有参考素材入口**（AI 扩写/优化拿不到任何图）。现在三分框：
 *   ① 意图（最小，一段话）→ ② 剧本（中等，中文自由格式）→ ③ 结果（最大，H3 官方格式）
 * 三框各装自己那一层，**段头【段N】在三个框里同序对应**：「解析并分配」时按段号
 * 合成一份标准分段文本再交给 parseMasterPrompt —— 文本格式与旧版、与外部 AI 续改
 * 完全兼容（导出/粘贴口径不变）。框内超出即滚轮滚动，框本身可拖右下角伸缩。 */

/* 范例（严格按 tools/h3_prompt_expander/references/h3-dialect.md：
 * 英文骨架逐字不改，主体中文；[Shot 1] 无时间戳且先声明风格，后续 [Shot N] At MM:SS.mmm；
 * 运镜写成句内自然动作（类型+幅度+速度）；(S1) 说话人 + <d>[Chinese] 原文</d>；
 * 环境音进 overall_soundscape，配乐无则 N/A）。 */
const MP_PH_INTENT = `【段1】
雨夜霓虹市场，女孩忽然回头笑说「跟上我」，我挤进人流跟上她。`;

const MP_PH_SCRIPT = `【段1】
时长：9 秒

镜头一（0–3 秒）：雨幕把霓虹糊成一片色块，她忽然停下，回头
镜头二（3–7 秒）：她笑了一下，抬手拨开湿掉的刘海，喊了一声
镜头三（7–9 秒）：我跟上，镜头跟着挤进人流的缝隙

台词：女孩：「跟上我。」
环境声：雨声、铁棚顶上的雨点、远处摊贩叫卖
背景音乐：无`;

/* ③ 结果的三字段骨架（base：T2VA / I2VA / FL2VA / L2VA） */
const MP_PH_MAIN_BASE = `integrated_multimodal_description: [Shot 1] 实拍、电影感，手持轻微晃动，中景框住雨夜市场的窄巷，霓虹招牌在积水里拉出红蓝长影，雨丝斜穿过画面。

[Shot 2] At 00:03.200，镜头以小幅慢速推近她的侧脸，她停下脚步回头看向镜头，湿掉的刘海贴在额角，嘴角翘起。

[Shot 3] At 00:07.100，(S1) 用清亮的少女嗓音、语速偏快地说道：<d>[Chinese] 跟上我。</d> 说完转身挤进人流，镜头以小幅中速跟拍，霓虹在前景不断被路人肩头切断。

overall_soundscape: 雨声持续，落在铁棚顶上的密集雨点，远处摊贩的叫卖声，电动车驶过水洼的溅水声。

non_diegetic_music: N/A`;

/* ③ 结果的六段式骨架（Ref2VA 全参考） */
const MP_PH_MAIN_REF2VA = `subject_definitions: <Subject 1> 撑伞的女孩：二十岁上下，黑色湿发贴额，白色雨衣；<Picture 1> 雨夜市场窄巷的实景参考图

summary:
[reference generation] 雨夜市场，女孩回头喊话后带路走进人流

retention_analysis:
<Subject 1>: fully_preserved - 人物身份与服装在全程保持一致
<Picture 1>: fully_copy - 场景光照与色彩关系沿用参考图

detailed_description: [Shot 1] 实拍、电影感，<Picture 1> 的雨夜市场作为起点，<Subject 1> 撑伞站在巷口，霓虹在积水里拉出红蓝长影。

[Shot 2] At 00:03.200，镜头以小幅慢速推近 <Subject 1> 的侧脸，她回头看向镜头，嘴角翘起。

overall_soundscape: 雨声持续，落在铁棚顶上的密集雨点，远处摊贩的叫卖声。

non_diegetic_music: N/A`;

/* 关键帧对齐指令（官方整句，只替换 N 与 S.SS）——必须是结果正文的第一行，
 * 其后空一行再接三字段；T2VA 没有这一行。 */
const MP_ALIGN_LINE = {
    I2VA: "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.",
    FL2VA: "How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.",
    L2VA: "How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.",
};

/* 模式下拉的实时说明：模式**确实**改变产出格式——后端 optimizer.build_system_prompt
 * 按 task 给出不同的节数与标签纪律，pick_rule_text 还会据此切换规则文件
 * （base 三字段 vs Ref2VA 六字段）。这里把差异说在界面上，别让人以为是个摆设。 */
const MP_MODE_HINT = {
    T2VA: "三字段，无关键帧对齐指令",
    I2VA: "三字段 + 首帧对齐指令（Picture 1 在 0.00s 被完整参考）",
    FL2VA: "三字段 + 首尾帧对齐指令（Picture 1 @0.00s、Picture 2 @结尾）",
    L2VA: "三字段 + 尾帧对齐指令（Picture 1 落在结尾）",
    Ref2VA: "六段式：subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music",
};

/** 按段头【段N】切一个框：返回 { head: 是否出现过段头, items: [每段正文…] }。
 *  没写段头的框 = 单段（内容整段归入第 1 段）；正文按出现顺序对应段号。 */
function mpSplitBox(text) {
    const src = String(text ?? "");
    if (!src.trim()) return { head: false, items: [] };
    const out = [];
    let cur = null;
    for (const line of src.split(/\r\n|\r|\n/)) {
        if (MP_HEAD_RE.test(line)) { cur = []; out.push(cur); continue; }
        if (!cur) { cur = []; out.push(cur); }
        cur.push(line);
    }
    return {
        head: out.length > 1 || MP_HEAD_RE.test(src.split(/\r\n|\r|\n/)[0] || ""),
        items: out.map((b) => b.join("\n").replace(/^\n+/, "").replace(/\n+$/, "")),
    };
}

/** 三框 -> 一份标准分段文本（① 意图 / ② 剧本 / ③ 提示词，段号同序对齐）。
 *  secs 为每段秒数（未知写 null，就不写「时长：」，避免把链上时长冲掉）。
 *  返回 { text, count, notes }——notes 走返回值而非出参（调用方与解析不在同一
 *   realm 时，push 进出参数组拿不到）。 */
function mpComposeBoxes(intentTxt, scriptTxt, mainTxt, secs) {
    const notes = [];
    const A = mpSplitBox(intentTxt), B = mpSplitBox(scriptTxt), C = mpSplitBox(mainTxt);
    const counts = [A, B, C].filter((x) => x.head).map((x) => x.items.length);
    const n = counts.length ? Math.max(...counts) : Math.max(A.items.length, B.items.length, C.items.length, 1);
    const at = (box, i) => {
        if (!box.items.length) return undefined;          // 整框没写 = 不动该字段
        if (!box.head) return i === 0 ? box.items[0] : undefined;
        return i < box.items.length ? box.items[i] : undefined;
    };
    if (!A.head && A.items.length && n > 1) {
        notes.push("① 意图没写【段N】段头，整段内容已归入第 1 段");
    }
    if (!B.head && B.items.length && n > 1) {
        notes.push("② 剧本没写【段N】段头，整段内容已归入第 1 段");
    }
    if (!C.head && C.items.length && n > 1) {
        notes.push("③ 结果没写【段N】段头，整段内容已归入第 1 段");
    }
    const blocks = [];
    for (let i = 0; i < n; i++) {
        const rows = [`【段${i + 1}】`];
        const sec = Number((secs || [])[i]);
        if (Number.isFinite(sec) && sec > 0) rows.push(`时长：${sec}`);
        const it = at(A, i), sc = at(B, i), mn = at(C, i);
        if (it !== undefined) rows.push(`意图：\n${it}`);
        if (sc !== undefined) rows.push(`剧本：\n${sc}`);
        if (mn !== undefined) rows.push(`提示词：\n${mn}`);
        blocks.push(rows.join("\n"));
    }
    return { text: blocks.join("\n\n") + "\n\n【完】", count: n, notes };
}

/** 总提示词工作台选中的素材 -> AI 可读的 media（后端只吃 data:image，故只收图片，
 *  上限 8 张，与 optimizer._media_images 同口径）。带角色说明，模型才知道谁是谁。 */
async function collectMasterMedia(node, assets) {
    const media = [];
    const notes = [];
    for (const a of (assets || [])) {
        if (media.length >= 8) break;
        if (!a || (a.kind || "image") !== "image") continue;
        const dataUrl = await optImageToDataUrl(
            assetPreviewUrl(getDirValue(node), a.file, a.asset_id));
        if (!dataUrl) continue;
        const tag = `<Picture ${media.length + 1}>`;
        media.push({ kind: "image", label: tag, images: [dataUrl] });
        notes.push(`${tag} = 参考素材「${a.label || a.file || "未命名"}」`);
    }
    return { media, note: notes.length ? `随图说明：${notes.join("；")}。` : "" };
}

function openMasterPromptModal() {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点（只读模式）"); return; }
    if (document.querySelector(".h3d-overlay")) return;

    const overlay = el("div", "h3d-overlay h3d-overlay-full");
    const dialog = el("div", "h3d-dialog h3d-dialog-full");
    dialog.innerHTML = `
        <h3>📋 总提示词 · 多段工作台</h3>
        <p class="h3d-lead">三层递进：<b>① 意图</b>（一段话，说清这段要什么）
        → <b>② 剧本</b>（AI 扩写产物，中文自由格式）
        → <b>③ 结果</b>（压成 H3 官方格式，<b>只有它进模型</b>）。
        多段时三个框<b>都用 <code>【段N】</code> 标段、同序对应</b>；单段直接写内容即可。
        「参考素材（AI 可见）」里<b>勾上要给的图</b>，扩写与优化才会把它们一起送去给模型看。
        <b>改满意了再点「解析并分配」</b>——只有这一步才会写回你的段落。</p>`;

    /* ---- 第一行：多段设置 + 两个 AI 按钮 ---- */
    const top = el("div", "h3d-mpset");
    const numBox = (v, lo, hi) => {
        const i = document.createElement("input");
        i.type = "number"; i.min = String(lo); i.max = String(hi); i.step = "1";
        i.value = String(v);
        return i;
    };
    const lab = (text, ctrl) => {
        const w = el("label", "");
        w.append(el("span", "", text), ctrl);
        return w;
    };
    const secMin = numBox(8, 4, 15);
    const secMax = numBox(12, 4, 15);
    const cntI = numBox(Math.max(1, ((getDs(node).prompts || []).length) || 3), 1, 12);
    const modeSel = document.createElement("select");
    for (const [v, l] of [["T2VA", "T2VA 文生视频"], ["I2VA", "I2VA 首帧"],
        ["FL2VA", "FL2VA 首尾帧"], ["L2VA", "L2VA 尾帧"], ["Ref2VA", "Ref2VA 多参"]]) {
        modeSel.append(new Option(l, v));
    }
    const styleSel = document.createElement("select");
    for (const [v, l] of [["strict", "严格"], ["balanced", "均衡"],
        ["creative", "创意"]]) styleSel.append(new Option(l, v));
    styleSel.value = "balanced";
    const btnSet = el("button", "h3d-btn", "⚙ AI 优化设置");
    btnSet.title = "AI 提示词优化设置（服务商 / 输出语言 / 规则文件）—— 全链共用";
    const modeHint = el("small", "h3d-mpmode", "");
    top.append(lab("每段时长", secMin), el("span", "", "–"), lab("", secMax),
        el("span", "", "秒"), lab("段数", cntI), lab("模式", modeSel),
        lab("风格", styleSel), btnSet, modeHint);

    /* ---- 第二行：参考素材（AI 可见）—— 勾选的图随请求送去给模型 ---- */
    const refRow = el("div", "h3d-refrow h3d-mprefs");
    refRow.append(el("label", "", "参考素材（AI 可见）"));
    const refCount = el("span", "h3d-secs-hint", "");
    refRow.append(refCount);
    const chipBox = el("div", "h3d-mprefs-chips");
    refRow.append(chipBox);
    const bRefAll = el("button", "h3d-btn h3d-mprefs-btn", "全选");
    const bRefNone = el("button", "h3d-btn h3d-mprefs-btn", "清空");
    const bRefReload = el("button", "h3d-btn h3d-mprefs-btn", "⟳");
    bRefReload.title = "重新读取素材池（刚上传/改名/删了素材时点一下）";
    refRow.append(bRefAll, bRefNone, bRefReload);

    /* 两个 AI 动作的实现挂在下面（挂在各自作用的框头上，这里先建句柄） */
    const btnExpand = el("button", "h3d-btn h3d-btn-cyan", "✨ AI扩写 → 剧本");
    const btnOpt = el("button", "h3d-btn h3d-btn-cyan", "✨ AI优化 → 结果");

    /* ---- 三框：① 意图（最小）→ ② 剧本（中）→ ③ 结果（最大）---- */
    const stack = el("div", "h3d-mpstack");
    /* 触发某个动作的按钮挂在**它作用的那一框**的标题栏上（和段卡一致）：
     * AI 扩写读 ①、写 ②；AI 优化读 ②、写 ③。以前两个按钮都堆在顶部设置行，
     * 看不出各自吃哪一框。 */
    const mkBox = (boxCls, title, hint, ph, act) => {
        const wrap = el("div", `h3d-mpboxwrap ${boxCls}`);
        const head = el("div", "h3d-mplabel");
        head.append(el("b", "", title), el("small", "", hint));
        if (act) { act.type = "button"; act.classList.add("h3d-mpboxbtn"); head.append(act); }
        const ta = document.createElement("textarea");
        ta.className = "h3d-mpbox";
        ta.spellcheck = false;
        ta.placeholder = ph;
        wrap.append(head, ta);
        stack.append(wrap);
        return { wrap, ta };
    };
    btnExpand.title = "按全片意图一次生成 N 段：各段意图留在 ①、剧本写进 ②。"
        + "会带上「参考素材（AI 可见）」里勾选的图。";
    btnOpt.title = "把每段剧本压成 H3 官方格式写进 ③（剧本保留，可反复重优化）。"
        + "会带上勾选的参考图；输出格式由顶部「模式」决定。";
    const boxI = mkBox("h3d-mpboxwrap-1", "① 意图",
        "一段话：谁、在哪、干什么、情绪怎么转 · AI 扩写的输入 · 不进模型", MP_PH_INTENT,
        btnExpand);
    const boxS = mkBox("h3d-mpboxwrap-2", "② 剧本",
        "AI 扩写产物 · 中文自由格式（可手工改）· 不进模型", MP_PH_SCRIPT, btnOpt);
    const boxR = mkBox("h3d-mpboxwrap-3", "③ 结果 · 浏览",
        "H3 官方格式 · 提示词优化的产物 · 唯一进模型的一层", "");

    const info = el("div", "h3d-mpinfo", "");
    const err = el("div", "h3d-err", "");
    const row = el("div", "h3d-dialog-row");
    const bLoad = el("button", "h3d-btn", "从当前链载入");
    const bClear = el("button", "h3d-btn", "清空");
    const cancel = el("button", "h3d-btn", "取消");
    const ok = el("button", "h3d-btn h3d-btn-cta", "解析并分配");
    row.append(bLoad, bClear, cancel, ok);
    dialog.append(top, refRow, stack, info, err, row);
    overlay.append(dialog);
    overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") overlay.remove(); });
    document.body.append(overlay);
    boxI.ta.focus();

    /* ---- 模式：实时说明 + 结果框骨架跟着换（让人看见模式真的在起作用）---- */
    const syncMode = () => {
        const m = modeSel.value;
        modeHint.textContent = `→ ${MP_MODE_HINT[m] || ""}`;
        const align = MP_ALIGN_LINE[m] ? MP_ALIGN_LINE[m] + "\n\n" : "";
        boxR.ta.placeholder = align + (m === "Ref2VA" ? MP_PH_MAIN_REF2VA : MP_PH_MAIN_BASE);
    };
    modeSel.addEventListener("change", syncMode);
    syncMode();

    /* ---- 参考素材 chips ---- */
    const selKey = new Set();
    const assetKey = (a) => String(a.asset_id || a.label || a.file || "");
    let imgPool = [];
    const renderRefs = () => {
        let pool = [];
        try { pool = getDs(node).ref_assets || []; } catch (e) { pool = []; }
        imgPool = pool.filter((a) => a && String(a.kind || "image") === "image");
        chipBox.innerHTML = "";
        if (!imgPool.length) {
            chipBox.append(el("span", "h3d-secs-hint",
                "（还没有图片素材：去左侧「素材与参考」上传。AI 只能读图片，视频/音频发不出去）"));
        }
        for (const a of imgPool) {
            const key = assetKey(a);
            const c = el("button", "h3d-refchip" + (selKey.has(key) ? " on" : ""));
            c.type = "button";
            c.append(buildAssetThumb(getDirValue(node), a));
            c.append(el("span", "", String(a.label || a.file || "素材")));
            c.title = `${a.label || a.file}｜点一下 = 让 AI 看这张图（再点取消）`;
            c.onclick = () => {
                if (selKey.has(key)) selKey.delete(key); else selKey.add(key);
                renderRefs();
            };
            chipBox.append(c);
        }
        refCount.textContent = imgPool.length
            ? `已选 ${selKey.size}/${imgPool.length}${selKey.size ? "（随请求发给 AI）" : ""}`
            : "";
    };
    bRefAll.onclick = () => { for (const a of imgPool) selKey.add(assetKey(a)); renderRefs(); };
    bRefNone.onclick = () => { selKey.clear(); renderRefs(); };
    bRefReload.onclick = () => renderRefs();
    renderRefs();

    const pickedAssets = () => imgPool.filter((a) => selKey.has(assetKey(a)));

    /* ---- 段时长：只在「从链载入」或「AI 扩写」给出确切值时才写回，避免冲掉链上时长 ---- */
    let segSecs = [];
    const secRange = () => {
        let lo = Math.max(4, Math.min(15, Number(secMin.value) || 8));
        let hi = Math.max(4, Math.min(15, Number(secMax.value) || 12));
        return hi < lo ? [hi, lo] : [lo, hi];
    };

    const compose = () => mpComposeBoxes(boxI.ta.value, boxS.ta.value, boxR.ta.value, segSecs);

    const paint = () => {
        const c = compose();
        const anyText = [boxI.ta.value, boxS.ta.value, boxR.ta.value].some((v) => String(v || "").trim());
        if (!anyText) { info.textContent = ""; return c; }
        const p = parseMasterPrompt(c.text);
        const cnt = (k) => p.segs.filter((s) => String(s[k] ?? "").trim()).length;
        const total = p.segs.reduce((a, s) => a + (Number(s.seconds) || 0), 0);
        info.innerHTML = `识别 <b>${p.segs.length}</b> 段 · 总时长 <b>${Math.round(total)}s</b>`
            + ` · 意图 <b>${cnt("intent")}</b> · 剧本 <b>${cnt("script")}</b>`
            + ` · 提示词 <b>${cnt("main")}</b>`
            + (p.segs.filter((s) => s.unlink === true).length
                ? ` · 独立镜头 <b>${p.segs.filter((s) => s.unlink === true).length}</b>` : "");
        const allNotes = (c.notes || []).concat(p.notes || []);
        err.textContent = allNotes.length ? "⚠ " + allNotes.join("；") : "";
        return p;
    };
    for (const ta of [boxI.ta, boxS.ta, boxR.ta]) ta.addEventListener("input", paint);

    /* 打开即载入当前链（有内容才载），进来就是可改的现成三层内容 */
    const loadFromChain = () => {
        const ds = getDs(node);
        const prompts = ds.prompts || [];
        const segs = ds.segments || [];
        segSecs = [];
        if (!prompts.length) { boxI.ta.value = ""; boxS.ta.value = ""; boxR.ta.value = ""; paint(); return; }
        const A = [], B = [], C = [];
        for (let i = 0; i < prompts.length; i++) {
            const s = (segs[i] && typeof segs[i] === "object") ? segs[i] : {};
            const sec = Number(s.seconds);
            segSecs.push(Number.isFinite(sec) && sec > 0 ? sec : null);
            const it = String(s.intent_zh || "").trim();
            const sc = String(s.script || "").trim();
            if (it) A.push(`【段${i + 1}】\n${it}`);
            if (sc) B.push(`【段${i + 1}】\n${sc}`);
            C.push(`【段${i + 1}】\n${String(prompts[i] ?? "").trim()}`);
        }
        boxI.ta.value = A.join("\n\n");
        boxS.ta.value = B.join("\n\n");
        boxR.ta.value = C.join("\n\n");
        paint();
    };
    if (((getDs(node).prompts || []).some((x) => String(x ?? "").trim()))) loadFromChain();

    const ensureSettings = () => {
        let st;
        try { st = optGetSettings(node); }
        catch (e) { err.textContent = `加载优化配置失败：${e.message}`; return null; }
        if (st.mode === "local" && !st.local_model) { openOptSettings(node); return null; }
        if (st.mode !== "local" && !st.api_key) { openOptSettings(node); return null; }
        return st;
    };

    /* AI 多段扩写：① 意图 -> N 段（意图 + 剧本），分别填进 ① 和 ② */
    btnExpand.onclick = async () => {
        const st = ensureSettings();
        if (!st) return;
        const prompt = String(boxI.ta.value || "").replace(/^【[^\n]*】\s*/gm, "").trim();
        if (!prompt) { err.textContent = "先在 ① 意图写全片想拍什么（一段话）再扩写"; return; }
        const n = Math.max(1, Math.min(12, Number(cntI.value) || 1));
        const hasOld = [boxS.ta.value, boxR.ta.value].some((v) => String(v || "").trim());
        if (hasOld && !confirm(`会用 ${n} 段新内容覆盖 ② 剧本与 ③ 结果（链上数据不动）。继续？`)) return;
        err.textContent = "";
        btnExpand.disabled = true;
        const oldTxt = btnExpand.textContent;
        btnExpand.textContent = "扩写中…";
        info.textContent = "多段扩写中…（先出大纲再逐段写，段数越多越久）";
        try {
            /* 图要传：后端 service 早支持（media + style_note），前端以前没传，
             * 于是 AI 扩写永远看不见任何参考素材。 */
            const assets = pickedAssets();
            const mm = (st.read_media !== false && assets.length)
                ? await collectMasterMedia(node, assets)
                : { media: [], note: "" };
            const [lo, hi] = secRange();
            const res = await window.H3Api.expandMulti({
                config: st, prompt, segment_count: n,
                seconds_min: lo, seconds_max: hi, style: styleSel.value,
                media: mm.media, style_note: mm.note,
            });
            if (res.status >= 400 || res.body?.error) {
                err.textContent = window.H3Api.errText(res, "扩写失败");
                return;
            }
            const segs = (res.body.segments || []).filter((x) => String(x.script || "").trim());
            if (!segs.length) { err.textContent = "扩写未返回任何剧本"; return; }
            const A = [], B = [], C = [];
            segSecs = [];
            segs.forEach((sg, i) => {
                const sec = Number(sg.seconds);
                segSecs.push(Number.isFinite(sec) && sec > 0 ? sec : null);
                const it = String(sg.logline_zh || "").trim();
                if (it) A.push(`【段${i + 1}】\n${it}`);
                B.push(`【段${i + 1}】\n${String(sg.script || "").trim()}`);
                C.push(`【段${i + 1}】`);
            });
            boxI.ta.value = A.join("\n\n");
            boxS.ta.value = B.join("\n\n");
            boxR.ta.value = C.join("\n\n");
            paint();
            info.textContent = `已扩写 ${segs.length} 段（意图进 ①、剧本进 ②）`
                + (mm.media.length ? `，已带 ${mm.media.length} 张参考图` : "（未勾选参考素材）")
                + "，接着点「✨ AI 多段提示词优化」压成官方格式";
        } catch (e) {
            err.textContent = `多段扩写失败：${e?.message || e}`;
        } finally {
            btnExpand.textContent = oldTxt;
            btnExpand.disabled = false;
        }
    };

    /* AI 多段提示词优化：把 ② 每段的剧本压成官方格式，写进 ③ */
    btnOpt.onclick = async () => {
        const st = ensureSettings();
        if (!st) return;
        const c = compose();
        const p = parseMasterPrompt(c.text);
        if (!p.segs.length) { err.textContent = "三个框里没有任何段落内容"; return; }
        const [lo] = secRange();
        const targets = [];
        p.segs.forEach((s, i) => {
            const src = String(s.script ?? "").trim() || String(s.main ?? "").trim()
                || String(s.intent ?? "").trim();
            if (src) {
                targets.push({
                    index: i, src,
                    seconds: Number((segSecs || [])[i]) || Number(s.seconds) || lo,
                });
            }
        });
        if (!targets.length) { err.textContent = "没有任何段有内容可优化"; return; }
        err.textContent = "";
        btnOpt.disabled = true;
        const oldTxt = btnOpt.textContent;
        btnOpt.textContent = "优化中…";
        info.textContent = `正在优化 ${targets.length} 段…`;
        try {
            const assets = pickedAssets();
            const mm = (st.read_media !== false && assets.length)
                ? await collectMasterMedia(node, assets)
                : { media: [], note: "" };
            const task = modeSel.value;
            const res = await window.H3Api.optimizeMulti({
                config: st,
                media: mm.media,
                task,
                segments: targets.map((t) => ({
                    prompt: (mm.note ? `${mm.note}\n` : "") + t.src,
                    seconds: t.seconds,
                    task,
                })),
            });
            if (res.status >= 400 || res.body?.error) {
                err.textContent = window.H3Api.errText(res, "多段优化失败");
                return;
            }
            const arr = res.body.segments || [];
            const C = [];
            let bad = 0;
            for (let i = 0; i < p.segs.length; i++) C.push(`【段${i + 1}】`);
            arr.forEach((r, k) => {
                const idx = (targets[k] || {}).index;
                if (idx === undefined || idx >= C.length || !r.result) return;
                C[idx] = `【段${idx + 1}】\n${String(r.result).trim()}`;
                bad += (r.errors || []).length;
            });
            boxR.ta.value = C.join("\n\n");
            paint();
            info.textContent = `已优化 ${arr.length} 段（${MP_MODE_HINT[task] || task}）`
                + (mm.media.length ? `，已带 ${mm.media.length} 张参考图` : "")
                + (bad ? `，仍有 ${bad} 项官方格式问题（在 ③ 里手改或换个模型重跑）`
                    : "，全部通过官方格式校验");
        } catch (e) {
            err.textContent = `多段优化失败：${e?.message || e}`;
        } finally {
            btnOpt.textContent = oldTxt;
            btnOpt.disabled = false;
        }
    };

    btnSet.onclick = () => openOptSettings(node);
    bLoad.onclick = () => loadFromChain();
    bClear.onclick = () => {
        if ([boxI.ta.value, boxS.ta.value, boxR.ta.value].some((v) => String(v || "").trim())
            && !confirm("清空三个框？（链上数据不动）")) return;
        boxI.ta.value = ""; boxS.ta.value = ""; boxR.ta.value = "";
        segSecs = [];
        paint();
    };
    cancel.onclick = () => overlay.remove();
    const submit = () => {
        const c = compose();
        if (![boxI.ta.value, boxS.ta.value, boxR.ta.value].some((v) => String(v || "").trim())) {
            err.textContent = "内容为空"; return;
        }
        /* ③ 是唯一进模型的一层：整框空着就分配 = 把所有段落的提示词清空
         * （applyMasterPrompt 对未写的 main 一律写 ""）。这不是"没提就不动"，
         * 是真会抹掉成片文本，必须拦一下让人确认。 */
        if (!String(boxR.ta.value || "").trim()
            && [boxI.ta.value, boxS.ta.value].some((v) => String(v || "").trim())
            && !confirm("③ 结果框是空的：分配会把所有段落的提示词（进模型的文本）清空。\n"
                + "只想改意图/剧本的话请取消，去段卡上改。确定继续？")) {
            return;
        }
        const p = applyMasterPrompt(node, c.text);
        if (!p.segs.length) { err.textContent = "未识别到任何段落"; return; }
        flushPrompts(node);                       // 同步项目 manifest 底稿
        setLed("done", `总提示词已分配到 ${p.segs.length} 段（意图 / 剧本 / 提示词）`);
        overlay.remove();
        scheduleRefresh(60);
        if (p.notes.length) alert("注意：\n- " + p.notes.join("\n- "));
    };
    ok.onclick = submit;
}

/* ---------- AI 扩写确认卡（中文意图 → 官方格式 → 回填分组） ---------- */

/** 把 h3_text 里的三/六字段正文拆出来，准备回填 prompt_v2。
 *  官方格式：字段名独占一行 + 冒号；字段之间空一行；对齐指令在三字段之前（本函数忽略）。
 *  返回 { field: text }，字段名用官方英文名。 */
function splitH3Sections(h3Text) {
    const out = {};
    const lines = String(h3Text || "").split("\n");
    let cur = null;
    const buf = {};
    for (const raw of lines) {
        const m = raw.match(/^([a-z_]+)\s*:\s*(.*)$/);
        if (m && H3_SECTION_KEYS.has(m[1])) {
            cur = m[1];
            buf[cur] = [m[2]];
            continue;
        }
        if (cur) buf[cur].push(raw);
    }
    for (const k of Object.keys(buf)) {
        const text = buf[k].join("\n").trim();
        if (text) out[k] = text;
    }
    return out;
}

const H3_SECTION_KEYS = new Set([
    "integrated_multimodal_description", "detailed_description",
    "subject_definitions", "summary", "retention_analysis",
    "overall_soundscape", "non_diegetic_music",
]);

/** 把官方格式正文回填到段的 prompt_v2：主字段拆成 shots，环境音/配乐直写。 */
function applyH3TextToSeg(node, segIdx, h3Text) {
    const secs = splitH3Sections(h3Text);
    const main = secs.integrated_multimodal_description || secs.detailed_description || "";
    if (!main && !secs.overall_soundscape && !secs.non_diegetic_music) return false;
    setPromptV2Field(node, segIdx, (pv) => {
        if (main) {
            /* [Shot N] 开头的块切成多镜；无标记则整段作为单镜 */
            const blocks = main.split(/(?=^\[Shot\s+\d+\])/m).map((s) => s.trim()).filter(Boolean);
            pv.shots = (blocks.length ? blocks : [main]).map((text) => {
                const head = text.match(/^\[Shot\s+(\d+)\](?:\s*At\s+\d{1,2}:\d{2}\.\d{3})?\s*/);
                const desc = head ? text.slice(head[0].length).trim() : text;
                const at = text.match(/^\[Shot\s+\d+\]\s*At\s+(\d{1,2}:\d{2}\.\d{3})/);
                return { description: desc, at: at ? at[1] : "" };
            });
        }
        if (secs.overall_soundscape) pv.soundscape = secs.overall_soundscape;
        if (secs.non_diegetic_music) pv.non_diegetic_music = secs.non_diegetic_music;
    });
    return true;
}

/** AI 扩写弹窗：填中文意图 → 出确认卡 → 确认回填 / 提意见修订 / 换风格重来。 */
/* ---------- AI 扩写（中文意图 → 剧本 · 内容发散，不套官方格式） ---------- */
function openExpandModal(node, segIdx, pv0) {
    if (document.querySelector(".h3d-overlay")) return;
    /* 扩写与优化共用同一份导演台设置：传 null 会让后端走 optimizer.py 的
     * DEFAULT_CONFIG 常量（语言/规则都不跟随用户选择），必须显式传进来。 */
    let settings;
    try { settings = optGetSettings(node); }
    catch (e) { alert(`加载优化配置失败：${e.message}`); return; }
    if (settings.mode === "local" && !settings.local_model) { openOptSettings(node); return; }
    if (settings.mode !== "local" && !settings.api_key) { openOptSettings(node); return; }
    const ds = getDs(node);
    const seg = (ds.segments || [])[segIdx] || {};
    const pv = seg.prompt_v2 || pv0 || {};
    const baseSec = Number(seg.seconds) || 5;
    let curScript = "";

    const overlay = el("div", "h3d-overlay");
    const dialog = el("div", "h3d-dialog h3d-dialog-wide");
    dialog.innerHTML = `
        <h3>✨ AI 扩写 · 中文意图 → 剧本</h3>
        <p class="h3d-lead">扩写<b>只管内容</b>：把一句话扩写成能填满时长的中文剧本，
        <b>自由格式，不套 H3 官方字段</b>——格式由 ② 的「提示词优化」负责压成官方三/六字段。
        时长给的是<b>范围</b>，模型在范围内自定秒数；长动作给大值、单点情绪给小值。
        生成后可以<b>直接在下面改</b>，再回填到 ② 剧本框。</p>`;
    const ta = document.createElement("textarea");
    ta.className = "h3d-mpta";
    ta.style.height = "70px";
    ta.spellcheck = false;
    ta.placeholder = "一句话说清这段要什么：场景、人物、动作、情绪、镜头感觉。例：夜晚便利店门口，女孩撑伞等车，霓虹倒映在积水里，缓慢推近。";
    ta.value = String(seg.intent_zh || pv.intent_zh || "").trim();

    const numBox = (v, lo, hi) => {
        const i = document.createElement("input");
        i.type = "number"; i.min = String(lo); i.max = String(hi); i.step = "1";
        i.value = String(v);
        return i;
    };
    const lab = (text, ctrl) => {
        const w = el("label", "");
        w.style.cssText = "display:inline-flex;gap:4px;align-items:center;font-size:11.5px;"
            + "color:var(--h3d-muted);white-space:nowrap";
        w.append(el("span", "", text), ctrl);
        return w;
    };
    const secMin = numBox(Math.max(4, Math.round(baseSec) - 2), 4, 15);
    const secMax = numBox(Math.min(15, Math.round(baseSec) + 2), 4, 15);
    const styleSel = document.createElement("select");
    for (const [v, l] of [["strict", "严格（只补机位声源）"], ["balanced", "均衡（补光位材质）"],
        ["creative", "创意（可补1个视觉细节）"]]) styleSel.append(new Option(l, v));
    styleSel.value = "balanced";
    const rangeRow = el("div", "h3d-mpset");
    rangeRow.append(lab("时长范围", secMin), el("span", "", "–"), lab("", secMax),
        el("span", "", "秒"), lab("风格", styleSel));

    /* 剧本预览：直接用 textarea，生成后可就地改，再回填 ② */
    const cardBox = document.createElement("textarea");
    cardBox.className = "h3d-mpta";
    cardBox.style.height = "min(38vh,320px)";
    cardBox.spellcheck = false;
    cardBox.placeholder = "生成的剧本会出现在这里，可直接改。";
    cardBox.style.display = "none";
    const info = el("div", "h3d-mpinfo", "");
    const err = el("div", "h3d-err", "");
    const row = el("div", "h3d-dialog-row");
    const runBtn = el("button", "h3d-btn h3d-btn-cta", "生成剧本");
    const applyBtn = el("button", "h3d-btn", "确认并回填 ②");
    applyBtn.disabled = true;
    const closeBtn = el("button", "h3d-btn", "关闭");
    row.append(runBtn, applyBtn, closeBtn);
    dialog.append(ta, rangeRow, cardBox, info, err, row);
    overlay.append(dialog);
    overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.append(overlay);
    ta.focus();

    const secRange = () => {
        let lo = Math.max(4, Math.min(15, Number(secMin.value) || 6));
        let hi = Math.max(4, Math.min(15, Number(secMax.value) || 10));
        return hi < lo ? [hi, lo] : [lo, hi];
    };

    const callExpand = async () => {
        if (!window.H3Api?.expandMulti) { err.textContent = "接口未就绪（h3_api.js 未加载）"; return; }
        const prompt = String(ta.value || "").trim();
        if (!prompt) { err.textContent = "先写一句中文意图"; return; }
        err.textContent = "";
        runBtn.disabled = true;
        runBtn.textContent = "扩写中…";
        try {
            const [lo, hi] = secRange();
            /* 图要传：后端 service 早支持，角色说明借 style_note 一起走，
             * 否则 AI 扩写看不见任何参考图。 */
            const mm = settings.read_media !== false
                ? await collectSegMedia(node, getDs(node), segIdx) : { media: [], note: "" };
            const res = await window.H3Api.expandMulti({
                config: settings,              // 跟随导演台设置：输出语言 / 规则文件
                prompt,
                segment_count: 1,
                seconds_min: lo,
                seconds_max: hi,
                style: styleSel.value,
                media: mm.media,
                style_note: mm.note,
            });
            if (res.status >= 400 || res.body?.error) {
                err.textContent = window.H3Api.errText(res, "扩写失败");
                return;
            }
            const sg = (res.body.segments || [])[0] || {};
            curScript = String(sg.script || "").trim();
            if (!curScript) { err.textContent = "扩写返回为空，换个模型或改改意图再试"; return; }
            cardBox.value = curScript;
            cardBox.style.display = "";
            const got = Number(sg.seconds) || lo;
            info.innerHTML = `时长 <b>${got}s</b>（范围 ${lo}–${hi}s）`
                + ` · 风格 <b>${styleSel.value}</b>`
                + (mm.media.length ? ` · 已带 ${mm.media.length} 张参考图` : "");
            if (mm.media.length && !String(mm.note || "").trim()) {
                info.innerHTML += " · <span style=\"color:var(--h3d-warn)\">"
                    + "该模型可能不支持读图</span>";
            }
            applyBtn.disabled = false;
        } catch (e) {
            err.textContent = `请求异常：${e?.message || e}`;
        } finally {
            runBtn.disabled = false;
            runBtn.textContent = "生成剧本";
        }
    };

    runBtn.onclick = () => callExpand();
    applyBtn.onclick = () => {
        const script = String(cardBox.value || "").trim();
        if (!script) return;
        /* 意图存两处：段级（主框上半区，下次续改直接带出来）+ v2 内部（兼容旧路径） */
        const intent = String(ta.value || "").trim();
        if (intent) {
            setSegmentField(node, segIdx, "intent_zh", intent);
            setPromptV2Field(node, segIdx, (p) => { p.intent_zh = intent; });
        }
        /* 只写 ② 剧本：**不再**顺手把自由格式塞进 ③ 结果——那是官方格式框，
         * 塞中文散体会污染进模型的文本。定稿走 ② 的「提示词优化 → 结果」。 */
        setSegmentField(node, segIdx, "script", script);
        flushPrompts(node);
        setLed("done", `第 ${segIdx + 1} 段剧本已生成，点 ② 的「✨ 提示词优化 → 结果」定稿`);
        overlay.remove();
        scheduleRefresh(60);
    };
    closeBtn.onclick = () => overlay.remove();
}

/* ---------- 刷新 ---------- */

/* ---------- 池子 hydration（项目清单 → 节点 widget） ----------
 * 素材库里的「调入项目 / 上传 / 改名 / 删除」都只写 manifest，不回填 widget 的话，
 * 段卡的引用勾选（h3d-refchip）与提示词框的 @ 补全就看不到新素材 ——
 * 「素材进了项目库却不能在提示词里用」就是这个。
 *
 * 关键：**manifest 是唯一真相，池子按它整体重建**（旧实现只做增量追加，于是
 * 改名=旧标签残留 + 新标签入池、删除=永远留着、切项目=旧池带过来）。
 * 同名冲突时 asset_links 胜出（与后端 by_alias 首胜口径一致）。
 * 按 dir + revision + 池签名节流，签名不变不写 widget（不打断正在打字的人）。 */
let _poolRev = "";
let _poolDir = "";

const _rolesOf = (r) => (Array.isArray(r) ? r.map(String)
    .filter((x) => x === "首帧图" || x === "尾帧图") : []);

/** manifest.assets + asset_links -> 规范化池子（权威源，不含 widget 里的残留） */
function poolFromManifest(mf, links) {
    const byLabel = new Map();
    const put = (label, ent) => { if (label) byLabel.set(String(label), ent); };
    for (const a of ((mf && mf.assets) || [])) {
        if (!a || !a.file || !a.label) continue;
        put(a.label, {
            file: String(a.file),
            kind: KIND_LIST.includes(a.kind) ? a.kind : "image",
            label: String(a.label),
            asset_id: "",
            roles: _rolesOf(a.roles),
        });
    }
    for (const L of links || []) {
        const alias = String((L && L.alias) || "");
        if (!alias) continue;
        put(alias, {
            file: String((L && L.file) || ""),
            kind: KIND_LIST.includes(L && L.kind) ? L.kind : "image",
            label: alias,
            asset_id: String((L && L.asset_id) || ""),
            roles: _rolesOf(L && L.roles),
        });
    }
    /* 有 asset_id 的链接条目**即使 file 为空也要留下**：file 是后端从全局库按
     * asset_id 回填的，全局库不可用时（try_library_root 定位失败 / 条目还没进库）
     * 就回填不到 —— 旧实现在这里按 file 过滤，于是「从全局库调进来的视频、音频、
     * 部分图片在引用条里一个都不出现」。有 asset_id 时预览走 libraryFileUrl(asset_id)，
     * 寻址照样正确；只有既无 file 又无 asset_id 的才是真·无效条目。 */
    return [...byLabel.values()].filter((a) => a.file || a.asset_id);
}

/** 池子指纹：只有它变了才写回 widget（避免无谓重绘/丢焦） */
function poolSig(pool) {
    return (pool || []).map((a) => [
        String((a && a.label) || ""), String((a && a.file) || ""),
        String((a && a.kind) || "image"), String((a && a.asset_id) || ""),
        _rolesOf(a && a.roles).join("·"),
    ].join(">")).join(";");
}

async function fetchAssetLinks(dir) {
    try {
        if (window.H3Api && typeof window.H3Api.assetLinks === "function") {
            const r = await window.H3Api.assetLinks(dir);
            return (r && r.body && r.body.ok && r.body.links) || [];
        }
    } catch (e) { /* 拿不到链接就只用 manifest.assets，不阻断刷新 */ }
    return [];
}

async function hydratePool(force) {
    const node = findNode();
    if (!node) return;
    const dir = getDirValue(node);
    if (!dir) return;
    let mf = null;
    try {
        mf = await fetchJson(`h3_projects/${dir}`, "manifest.json");
    } catch (e) { return; }
    const links = await fetchAssetLinks(dir);
    const pool = poolFromManifest(mf, links);
    const dirChanged = _poolDir !== dir;
    const sig = poolSig(pool);
    const rev = `${dir}:${(mf && mf.revision) || 0}:${sig}`;
    if (!force && !dirChanged && _poolRev === rev) return;
    _poolDir = dir;
    _poolRev = rev;
    const ds = getDs(node);
    if (poolSig(ds.ref_assets) === sig) return;
    ds.ref_assets = pool;
    setDs(node, ds);
}

async function refresh() {
    try {
        await hydratePool();          // 先同步后端清单，段卡与 @ 补全才看得到新素材
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

/* ---------- 桌面端（Electron）输入焦点兜底 ----------
 * 症状：ComfyUI 桌面端进入后不刷新，所有输入框都打不进字（窗口/画布占着键盘焦点）。
 * 兜底三招（都无害，正常情况下不触发）：
 *   ① 点进输入框时若焦点还停在 body/canvas，主动再 focus 一次（含下一拍重试）；
 *   ② 输入框内按键在捕获阶段 stopPropagation，别被画布的热键处理吃掉；
 *   ③ 顶栏「⌨ 输入修复」按钮手动抢回焦点（见 openDesk）。 */
function installDesktopFocusFix() {
    if (window.__h3FocusFix) return;
    window.__h3FocusFix = true;
    const EDITABLE = "input, textarea, select, [contenteditable='true']";
    document.addEventListener("pointerdown", (e) => {
        const t = e.target;
        if (!t || !t.closest) return;
        const ed = t.closest(EDITABLE);
        if (!ed) return;
        try { if (!document.hasFocus()) window.focus(); } catch (err) { /* 忽略 */ }
        const ae = document.activeElement;
        const stuck = !ae || ae === document.body || ae === document.documentElement
            || ae.tagName === "CANVAS";
        if (stuck && ae !== ed) {
            try { ed.focus({ preventScroll: true }); } catch (err) { try { ed.focus(); } catch (e2) { /* 忽略 */ } }
            setTimeout(() => {
                if (document.activeElement !== ed) {
                    try { ed.focus({ preventScroll: true }); } catch (err) { /* 忽略 */ }
                }
            }, 0);
        }
    }, true);
    document.addEventListener("keydown", (e) => {
        const ae = document.activeElement;
        if (ae && ae.closest && ae.closest(EDITABLE)) e.stopPropagation();
    }, true);
}

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

/* 供「素材库」浏览器调用：latent 二采要驱动画布节点，逻辑留在导演台里。 */
window.H3Director = {
    upscaleLatent(dir, file, say) {
        const node = findNode();
        if (!node) return Promise.resolve();
        return runUpscaleTool(node, dir, file, typeof say === "function" ? say : () => {});
    },
};

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
        installDesktopFocusFix();   // 桌面端输入框打不进字的兜底

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
                tooltip: "H3 Seamless Chain：项目管理 + 段落流水线 + 成片历史（继续下一段 / 重摇 / 改词）",
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

/* M4集成点（默认关闭）：新模块 web/h3_api.js,h3_prompts.js,h3_assets.js,h3_latent.js 由ComfyUI自动加载并挂 window.H3*；本块仅做存在性标记，不改现有UI。开功能时在此后挂按钮/面板。*/
try { if (typeof window !== 'undefined') { window.__H3_V2_MODULES__ = { api: !!window.H3Api, prompts: !!window.H3Prompts, assets: !!window.H3Assets, latent: !!window.H3Latent }; } } catch (e) {}
