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
/* 合并模式的内存清单（不落盘、不动链与存档）。
 *   order —— 素材库里点选的素材，**数组顺序就是合并顺序**（Q1 口径：点击顺序即
 *            合并顺序，不做拖拽排序）。用数组不用 Set，就是因为顺序本身就是数据。
 *   files —— 追加上传的外部视频（永远排在 order 之后）。
 *   segs  —— 旧"勾选段号"通道。段卡上的勾选框已删，这里**保留字段但不再写入**，
 *            只为兼容可能的旧调用点，别在别处读它。
 *   running —— **合并导出进行中**。互斥守卫只看这一个标记，不看 on：
 *            `on` 现在是个常显的开关，随手开着不该拦住生成。 */
const mergeSel = { on: false, order: [], segs: [], files: [], running: false };

/** 退出合并模式 = 清空全部清单（退出立即 reset，无副作用：清单不落盘）。 */
function resetMergeSel() {
    mergeSel.order = [];
    mergeSel.segs = [];
    mergeSel.files = [];
    mergeSel.running = false;
}

/** 合并清单总项数（素材 + 外部视频）。 */
function mergeCount() {
    return (mergeSel.order || []).length + (mergeSel.files || []).length;
}
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

/** 读项目 manifest：统一走 /h3chain/project（API 直读磁盘）。
 *
 * **不要用 /api/view 读它**：那条路是 FileResponse，只带 Last-Modified/ETag，
 * ComfyUI 只在"危险内容类型"上才补 Cache-Control: no-store —— JSON 是按启发式
 * 可缓存的。而 manifest 是**高频改写**的文件（上传 / 保存 / 入库都动它），又处在
 * 240ms 轮询里：一旦命中缓存就拿到"改动之前"的副本，表现正是「刚上传的素材不进
 * 引用条 / 引用素材一片空白」，而切一下屏（新鲜期过期）又好了。
 * 历史上那些"传了没反应、刷一下就好"的怪象，根子都在这里。 */
async function fetchManifest(dir) {
    if (!dir) return null;
    const r = await apiGet(`/h3chain/project?dir=${encodeURIComponent(dir)}`);
    return (r.ok && r.data && r.data.manifest) || null;
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
    /* 必须用 pyRound（Python round 的 banker's 语义）而不是 Math.round：
     * 后端 nodes._snap_seconds 是 Python，`.5` 的取舍规则和 JS 不一样
     * （round(6.5)=6 而 Math.round(6.5)=7）。段长一旦差 1 帧，对齐指令的
     * S.SS 就跟着差 0.04s，前端显示和实跑注入又对不上。 */
    const s = Number(seconds);
    if (!isFinite(s) || s <= 0) return 124;      // 与后端默认 5s 一致
    const f = Math.max(5, pyRound(s * 24));
    const k = Math.max(0, pyRound((f - 5) / 17));
    return 17 * k + 5;
}

/** 本段总帧数（像素帧）：本段设了秒数就用它，否则跟随节点「每段时长」。
 *
 *  与后端 nodes._snap_seconds 同公式（秒×24 → **就近**吸附 17k+5，不是向上对齐）。
 *  段卡标题那个「≈N帧」和锚定面板的目标轨刻度都取这一个函数——这两处曾经各算一遍，
 *  结果一边向上对齐一边就近，用户看到目标轨的总帧数跟分段时长对不上。
 *  改公式只许改这里（连线另一头是 nodes._snap_seconds，两边必须同时改）。 */
/** 本段时长（**秒**）：段级 seconds 优先，没填 → 跟随节点「每段时长」默认。
 *
 *  AI 扩写/优化、对齐指令的 S.SS、段卡「≈N帧」全走这一个函数。以前这些地方
 *  各自写 `seg.seconds || 5` —— 段级留空（语义就是"跟随节点默认"）时一律拿 5，
 *  用户把节点「每段时长」改成 8s 就会出现"界面显示 8s、AI 按 5s 写、对齐句
 *  S.SS 也对不上"三处错位。段级留空 ≠ 5 秒，是"用节点那个值"。 */
function segmentSeconds(node, seg) {
    const s = Number(seg && seg.seconds);
    if (Number.isFinite(s) && s > 0) return s;
    const defRaw = Number(getWidgetValue(node, W_DUR));
    return Number.isFinite(defRaw) && defRaw > 0 ? defRaw : 5.0;
}

function segmentFrames(node, seg) {
    return snapFrames(segmentSeconds(node, seg));
}

/** 宽高比+百万像素 -> 显示徽章文案；无法判断返回 null。
 *
 * 「自定义」也要出徽章（2026-09-25）：此模式下「百万像素」**完全不参与**换算、
 * 直接吃「宽度/高度」两个控件 —— 以前这里返回 null（自定义就没有徽章），于是
 * "我把百万像素改成 0.5 了怎么还报分辨率不一致"在界面上**完全看不出来**。
 * 反过来非自定义时「宽度/高度」被换算覆盖，也要说清（改它们不生效）。 */
function canvasBadgeText(node) {
    const ar = String(getWidgetValue(node, W_AR) ?? "");
    if (!AR_RATIO[ar]) {
        const w = Number(getWidgetValue(node, W_WIDTH));
        const h = Number(getWidgetValue(node, W_HEIGHT));
        if (!Number.isFinite(w) || !Number.isFinite(h) || !w || !h) return null;
        return `自定义画幅 ${w}×${h}（百万像素不参与，改它无效）`;
    }
    const mp = String(getWidgetValue(node, W_MP) ?? "0.5");
    const c = resolveCanvas(ar, mp);
    return c ? `${ar} · ${mp}MP → ${c[0]}×${c[1]}（宽/高 控件被覆盖，改它们无效）` : null;
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

/** 引用名归一（前端侧）：提示词里 `@` 后面写的那个名字，**含格式后缀**。
 *  权威规则在后端 asset_store.clean_ref_name，两边必须同规则 —— 不同就会出现
 *  "前端认得、后端编译不出来"的半边引用。
 *
 *  与 cleanLabel 的唯一区别：**不剥扩展名**。引用名就是素材的真名（女主.png），
 *  用 stem 当引用名的话，用户写 `@女主.png` 时分词器只能匹配到 `女主`，
 *  剩下的 `.png` 当普通文本留在正文里 —— 绿框后面挂个裸后缀。
 *
 *  规则：非法字符（空白/括号等）压成 `_`（保留 `.` `_` `-`）→ 折叠连续 `_-`
 *  与 `..` → 去首尾 `._-` → 超长**保尾**（尾巴是两条长名素材的唯一区分点）。 */
const REF_NAME_MAX = 96;

/** 素材的**引用键**：提示词里 `@` 后面写的那个名字。
 *  ref_name（含格式后缀的真名）优先；旧档没有 ref_name 时退回别名（stem）。
 *  所有"引用"相关的比较/存取都必须走它 —— 直接比 `a.label` 会在有 ref_name
 *  的素材上永远匹配不上（正文里写的是全名，label 是 stem）。 */
const refKeyOf = (a) => String((a && (a.ref_name || a.label)) || "");

function cleanRefName(text) {
    const base = String(text ?? "").trim().replace(/\\/g, "/").split("/").pop() || "";
    if (!base) return "";
    let s = base.replace(/[^\p{L}\p{N}._-]+/gu, "_")
        .replace(/[_-]{2,}/g, "_")
        .replace(/\.{2,}/g, ".");
    s = s.replace(/^[._-]+|[._-]+$/g, "");
    if (s.length > REF_NAME_MAX) s = s.slice(0, REF_NAME_MAX - 12) + s.slice(-12);
    return s.replace(/^[._-]+|[._-]+$/g, "");
}

/** `@key` 替换（池内最长前缀）：encode / decode 共用一份实现。
 *  最长优先是必须的 —— 否则 `图片1` 会吃掉 `图片10` 的前缀。 */
function replaceAtTokens(text, mapping) {
    const s = String(text || "");
    if (!s || s.indexOf("@") < 0 || !mapping) return s;
    const keys = Object.keys(mapping).filter(Boolean).sort((a, b) => b.length - a.length);
    let out = "";
    let i = 0;
    while (i < s.length) {
        if (s[i] === "@" && !/[0-9A-Za-z_]/.test(s[i - 1] || "")) {
            const hit = keys.find((k) => s.startsWith(k, i + 1));
            if (hit) { out += `@${mapping[hit]}`; i += hit.length + 1; continue; }
        }
        out += s[i];
        i += 1;
    }
    return out;
}

/** 正文里的 `@素材全名` → `@标注`（**发给 LLM 之前**用）。
 *  LLM 看见的是 `图片1` 这种短而稳的名字，不会把长文件名抄错，也不会凭空造素材。 */
const encodeMarks = (text, nameToMark) => replaceAtTokens(text, nameToMark);

/** LLM 返回的 `@标注` → `@素材全名`（写回提示词框之前用）。
 *  译不出来的标注（模型造了个 `@图片99`）**原样保留** —— 框里会渲染成红框
 *  （悬空引用），而不是静默丢掉。 */
const decodeMarks = (text, markToName) => replaceAtTokens(text, markToName);

/* LLM 一跳的换码**入口**：规则唯一在 h3_prompts.js（marksToText / textToMarks）。
 *
 * 以前调用方现场用 collectSegMedia 的 markMap 拼映射表再喂给 encode/decodeMarks ——
 * 那张表只含"本段勾选的参考素材"，正文里写了但没勾的素材换不了码，
 * 于是模型看见一半 `@猫.png`、一半 `@图片1`，两边对不上。
 * 现在直接传**整个池子**，池内所有素材都能换，规则也只有一份。 */
function toLLMText(text, pool) {
    const HP = window.H3Prompts || {};
    if (typeof HP.marksToText === "function") return HP.marksToText(text, pool);
    return String(text == null ? "" : text);
}

function fromLLMText(text, pool) {
    const HP = window.H3Prompts || {};
    if (typeof HP.textToMarks === "function") return HP.textToMarks(text, pool);
    return String(text == null ? "" : text);
}

/* 标注三原语全部走 h3_prompts（规则唯一，与后端 asset_store 同口径）。
 * 这里只做**兜底**实现，保证 h3_prompts 没加载时也不炸（老页面/单测直跑）。 */
const _HP = () => (typeof window !== "undefined" ? (window.H3Prompts || {}) : {});
function cleanMark(s) {
    const HP = _HP();
    if (typeof HP.cleanMark === "function") return HP.cleanMark(s);
    const t = String(s == null ? "" : s).trim();
    const m = /^(图片|视频|音频)(\d{1,3})$/.exec(t);
    if (!m) return "";
    const n = Number(m[2]);
    return n >= 1 && n <= 999 ? `${m[1]}${n}` : "";   // 图片0 / 图片1000 非法
}
function markShaped(s) { return cleanMark(s) !== ""; }
function markOf(pool, nm) {
    const HP = _HP();
    if (typeof HP.markOf === "function") return HP.markOf(pool, nm);
    const want = String(nm || "").trim();
    for (const a of pool || []) {
        const names = [String((a && a.ref_name) || "").trim(),
            String((a && (a.label || a.alias)) || "").trim()];
        if (names.includes(want)) return cleanMark(a && a.mark);
    }
    return "";
}
/** 下一个可用号（不回收）：走 h3_prompts.nextMarkSeq；缺模块时本地兜底。 */
function nextMarkSeq(kind, marks) {
    const HP = _HP();
    if (typeof HP.nextMarkSeq === "function") return HP.nextMarkSeq(kind, marks);
    const k = KIND_NAME[String(kind || "image")] || "图片";
    let mx = 0;
    for (const m of marks || []) {
        const g = /^(图片|视频|音频)(\d{1,3})$/.exec(String(m || "").trim());
        if (g && g[1] === k) mx = Math.max(mx, Number(g[2]));
    }
    return mx + 1;
}

/** 素材条目上挂的标注文本（图片1 / 视频1 / 音频1）。 */
function markTextOf(a) { return String((a && a.mark) || "").trim(); }

/** 补标注：合法的已有不动（含非法的一律重发），缺的按类型独立递增（**编号不回收**）。
 *
 *  **非法形态必须重发**而不是"留着占位"：后端 clean_mark 判非法就当没号重新发，
 *  前端若照原样显示，用户看到的「图片2」和实际挂上的素材不是同一张 —— 静默串号。
 *  与后端 asset_store.assign_marks 同口径；后端已落盘的为准，这里只兜底旧档。 */
function assignMarks(items) {
    const list = items || [];
    const marks = list.map(markTextOf);
    const seq = { 图片: 0, 视频: 0, 音频: 0 };
    for (const m of marks) {
        const g = /^(图片|视频|音频)(\d{1,3})$/.exec(cleanMark(m));
        if (g) seq[g[1]] = Math.max(seq[g[1]] || 0, Number(g[2]));
    }
    return list.map((e) => {
        if (cleanMark(markTextOf(e))) return e;
        const k = KIND_NAME[String((e && e.kind) || "image")] || "图片";
        seq[k] = (seq[k] || 0) + 1;
        return { ...e, mark: `${k}${seq[k]}` };
    });
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

/** 超长素材名的显示形态：保留首尾、中间省略。
 *
 * 素材名常常是长文件名，而两个素材可能只差尾部几位
 * （`…_14_02_12.png` / `…_14_02_03.png`）。纯前缀截断会让它们在界面上长得
 * 一模一样 —— 尾巴才是区分点，必须留着，否则用户根本看不出引用了哪个。
 * **含格式后缀**（如 `.png`）：素材引用要"完整"，@回廊场景.png 才是自描述完整；
 * 短名字行为不变（`foo.png` 直接显示 `foo.png`，不省略）。 */
function shortLabel(s, head = 13, tail = 10) {
    const t = String(s == null ? "" : s).trim();
    if (t.length <= head + tail + 1) return t;
    return t.slice(0, head) + "…" + t.slice(-tail);
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
/* —— 引用名表：一个素材在正文里可被写成**两种**名字 ——
 *
 *   · 全名（ref_name，含格式后缀，如 `猫.png`）—— 现在的落盘口径
 *   · 别名（alias/label，无后缀，如 `猫`）—— 旧档与用户手打的写法
 *
 * 两张表必须**同时**喂给分词器，否则就是那条著名的"后缀溢出"：
 * 只认别名时，用户从外面贴进来的 `@猫.png` 只能匹配到 `猫`，剩下的 `.png`
 * 当普通文本留在正文里（绿框后面挂个裸后缀）。
 *
 * 分词器按**长度降序**匹配，所以更长的全名会先命中 —— 正文里存什么取决于
 * 表里给它什么名字：给了全名，插入/序列化就自然写成 `@猫.png`（不用另写规则）。
 *
 * 只补全名还不够：素材**进项目库时必须保留完整文件名（含后缀）**，
 * ref_name 才推导得出来（见后端 asset_store.clean_ref_name / projects._ref_name_of）。
 */
function refLabelsOf(pool) {
    const out = [];
    for (const a of (pool || [])) {
        if (!a) continue;
        const rn = String(a.ref_name || "").trim();
        const lb = String(a.label || a.alias || "").trim();
        if (rn) out.push(rn);
        if (lb && lb !== rn) out.push(lb);
    }
    return out;
}

/* 名字 -> 素材信息 {kind, file, asset_id, mark}：绿框按它挑缩略图/图标。
 * 全名与别名**指向同一份信息**（同一个 key 存两次），
 * 否则按全名插进来的绿框会因为查不到信息而退化成默认图标。 */
function refInfoMapOf(pool) {
    const m = {};
    for (const a of (pool || [])) {
        if (!a) continue;
        const lb = String(a.label || a.alias || "").trim();
        if (!lb && !a.ref_name) continue;
        const info = {
            kind: KIND_LIST.includes(a.kind) ? a.kind : "image",
            file: String(a.file || ""),
            asset_id: String(a.asset_id || ""),
            mark: String(a.mark || ""),
            /* 规范化名字：正文里该写成什么。解析按钮靠它把别名/标注统一成全名，
             * 否则"从外面复制来的提示词"里三种写法混着，每次解析结果都不一样。 */
            ref_name: String(a.ref_name || lb || "").trim(),
        };
        if (a.ref_name) m[String(a.ref_name).trim()] = info;
        if (lb) m[lb] = info;
    }
    return m;
}

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
    /* token 映射：{"<Picture 1>": "素材别名"}。外部 agent / 官方格式的文本里写的是
     * `<Picture N>`，这里把它也渲染成缩略图（序列化时**原样还原** `<Picture N>`），
     * 让用户直接看见"Picture 几"到底对应哪张图 —— 素材顺序挂错时一眼就能发现。
     * 与 @别名 并存互不干扰：@别名 是导演台引用语法（可增删、带 ✕），
     * `<Picture N>` 是官方格式的一部分（只读展示，要改就编辑正文）。 */
    const tokenMapOf = () => (typeof o.tokenMap === "function" ? o.tokenMap() : (o.tokenMap || {})) || {};
    /* 是否启用官方标签可视化：**只在显式传入 tokenMap 的编辑器里开启**。
     * ①②框不传 → 不识别 `<Picture N>`，避免误伤正文里恰好出现的尖括号文本；
     * ③结果框传了 → 即使一张素材都没挂（映射为空）也照渲染，标红警示悬空引用。 */
    const tokenOn = () => typeof o.tokenMap === "function"
        || (!!o.tokenMap && typeof o.tokenMap === "object");

    /* —— 序列化：DOM → 纯文本（绿框还原成 @别名；块级换行补 \n） —— */
    function ser(node) {
        let out = "";
        for (const n of node.childNodes) {
            if (n.nodeType === 3) { out += n.nodeValue || ""; continue; }
            if (n.nodeType !== 1) continue;
            /* token 优先：<Picture N> 框要还原成官方标签原文，别被当成 @别名 输出 */
            if (n.dataset && n.dataset.token) { out += n.dataset.token; continue; }
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
        sp.append(document.createTextNode(shortLabel(label)));
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

    /** 官方格式标签 `<Picture N>` / `<Video N>` / `<Audio N>` 的可视化。
     *
     * 与 makeTag（@别名）的区别：**不带 ✕**。它是外部/官方文本的一部分，不是导演台的
     * 引用语法 —— 要删就编辑正文，不走引用计数逻辑。
     * dataset.token 用于序列化还原；dataset.label 让缩略图/换标识逻辑复用；
     * class 带 h3d-rtok，使 normalizeLoose / removeOneTag 的 `.h3d-rtag` 计数能精确
     * 排除它（否则 token 会被误当成 @别名 的框，漏判"还有裸别名没成框"）。
     * 挂不到素材时标 h3d-rtok-missing —— 这正是"悬空引用"的可视化。 */
    function makeTokenTag(token, label) {
        const sp = document.createElement("span");
        sp.className = "h3d-rtag h3d-rtok";
        sp.contentEditable = "false";
        sp.dataset.token = String(token);
        sp.dataset.label = String(label || "");
        const info = infoOf()[label] || {};
        const ainfo = {
            kind: String(info.kind || "image"),
            file: String(info.file || ""),
            asset_id: String(info.asset_id || ""),
        };
        sp.dataset.thumbSig = thumbSig(ainfo);
        if (label) {
            sp.append(buildAssetThumb(dirOf(), ainfo));
            sp.title = `${token}：官方格式引用，指向素材「${label}」（按素材调度顺序编号）`;
        } else {
            sp.classList.add("h3d-rtok-missing");
            sp.title = token.startsWith("@")
                ? `${token}：素材池里没有这个名字（多半是抄错了）—— 生成时不会有图。`
                  + "请核对「引用素材」里的名字，或改成正确的 @素材名。"
                : `${token}：没有挂到任何素材 —— 生成时不会有图。`
                  + "请到「引用素材」按顺序挂上对应素材，或删掉这个标签。";
        }
        sp.append(document.createTextNode(token));
        return sp;
    }

    /** 正文分词：把 `@别名` 切成引用 token（**最长优先**，与后端 compile_refs /
     *  refsFromText 同口径）。渲染、计数、删除全走这一条路 —— 早期用正则
     *  `@短标签(?![0-9A-Za-z_])` 去删，负向后顾只挡 ASCII 字母，于是删「阿依」
     *  会把「@阿依的家」的前缀吃掉（剩下「的家」）。 */
    function refTokens(text) {
        const s = String(text == null ? "" : text);
        const labs = [...labelsOf()].sort((a, b) => b.length - a.length);
        const map = tokenMapOf();
        const out = [];
        let buf = "";
        let i = 0;
        while (i < s.length) {
            /* 官方标签 <Picture N>/<Video N>/<Audio N>：切成 token 形态，序列化时
             * 原样还原（不会像 @别名 那样被改写）。
             * 按**语法形态**识别而非查映射表 —— 否则挂不到素材的标签根本不被渲染，
             * "手贴了文本却没挂图"就完全没有提示。映射表只用来查它指向哪张素材，
             * 查不到即 label 为空，渲染层标红警示。 */
            if (tokenOn() && s[i] === "<") {
                const mt = /^<(?:Picture|Video|Audio)\s+\d+>/.exec(s.slice(i));
                if (mt) {
                    if (buf) { out.push({ text: buf }); buf = ""; }
                    out.push({ token: mt[0], label: String(map[mt[0]] || "") });
                    i += mt[0].length;
                    continue;
                }
            }
            if (s[i] === "@" && !/[0-9A-Za-z_]/.test(s[i - 1] || "")) {
                const hit = labs.find((l) => s.startsWith(l, i + 1));
                if (hit) {
                    if (buf) { out.push({ text: buf }); buf = ""; }
                    out.push({ label: hit });
                    i += hit.length + 1;
                    continue;
                }
                /* 未知 @xxx：素材池里没有这个名字。LLM 抄错素材名时不会有任何反馈
                 * —— 它只会当普通文本静默留在正文里，等出片才发现没挂上图。
                 * 借 token 形态（label 为空）渲染成红框，让抄错立刻可见。
                 * 仅在启用 token 可视化的框（③结果框）里做，且要求 @ 后至少 2 个
                 * 非空白非标点字符，避免误伤 @2x 之类。 */
                const mu = tokenOn()
                    ? /^@[^\s@，。；：、,.;:!?（）()\[\]【】<>"'`|]{2,80}/.exec(s.slice(i))
                    : null;
                /* 还要排除 @2x 这类技术写法：要求含中文，或整体长度≥4
                 * （@2x 只有 3 字符且纯 ASCII → 不标红）。 */
                const looksNamed = mu && (/[\u4e00-\u9fff]/.test(mu[0]) || mu[0].length >= 4);
                if (looksNamed) {
                    if (buf) { out.push({ text: buf }); buf = ""; }
                    out.push({ token: mu[0], label: "" });
                    i += mu[0].length;
                    continue;
                }
            }
            buf += s[i];
            i += 1;
        }
        if (buf) out.push({ text: buf });
        return out;
    }

    /* token 重建：三类 token 各自的文本形态。序列化统一走这里 ——
     * 凡是从 token 数组拼回文本的地方都必须用它，否则 `<Picture N>`
     * 会被写成 `@别名`（形态被悄悄改写）。 */
    const emitTok = (tk) => (tk.token ? tk.token : (tk.label ? `@${tk.label}` : tk.text));

    function render(text) {
        box.replaceChildren();
        for (const tk of refTokens(text)) {
            if (tk.token) box.append(makeTokenTag(tk.token, tk.label));
            else if (tk.label) box.append(makeTag(tk.label));
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
                    if (n.dataset && n.dataset.token) {
                        /* token 按标签原文计长（`<Picture 1>` = 11 字符） */
                        const len = n.dataset.token.length;
                        if (acc + len >= want) {
                            const r = document.createRange();
                            r.setStartAfter(n);
                            r.collapse(true);
                            return r;
                        }
                        acc += len;
                    } else if (n.dataset && n.dataset.label) {
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
            /* 只删 @别名；token（<Picture N>）是官方文本的一部分，不归引用计数管 */
            if (tk.label && !tk.token && tk.label === want) { hit += 1; continue; }
            next += emitTok(tk);
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
            /* :not(.h3d-rtok)：token 框不算 @别名 的第 n 个，否则序号会错位 */
            for (const n of box.querySelectorAll(".h3d-rtag:not(.h3d-rtok)")) {
                if (n === tagEl) break;
                if ((n.dataset.label || "") === want) nth += 1;
            }
        }
        const at = caret();
        let seen = 0;
        let hit = false;
        let next = "";
        for (const tk of refTokens(ser(box))) {
            if (tk.label && !tk.token && tk.label === want) {
                if (seen === nth) { seen += 1; hit = true; continue; }
                seen += 1;
                next += emitTok(tk);
                continue;
            }
            next += emitTok(tk);
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
        /* 只数 @别名：token 不算"引用次数"（它是官方文本，不是导演台引用语法） */
        return refTokens(ser(box)).filter((tk) => tk.label && !tk.token && tk.label === want).length;
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
        resolveRefs,
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
    /* 解析（手动按钮用）：把正文里的素材名**统一成池内全名**再成框。
     *
     * 与 normalizeLoose 的区别：那个只"补框"（文本不变），这个还会把别名/标注
     * 规范化成全名 —— 从外面复制来的提示词里 `@猫` `@猫.png` `@图片1` 三种写法
     * 常常混着，不统一的话每次解析出的引用都不一样，正文也永远稳定不下来。
     *
     * 同时收集**认不出的 @名字**返回给调用方提示 —— 复制来的文本里最常见的就是
     * 素材名对不上（改名过 / 没入库），不提示的话用户只会觉得"解析了没反应"。
     * 返回 {changed, hits, unknown, missing}。 */
    function resolveRefs() {
        const text = api.value;
        const labs = [...labelsOf()].sort((a, b) => b.length - a.length);
        const toks = refTokens(text);
        const info = infoOf();
        let out = "", hits = 0;
        for (const t of toks) {
            if (t.text != null) { out += t.text; continue; }
            /* 官方标签 <Picture N> 原样保留：它是官方格式的一部分，不是导演台语法 */
            if (t.token) { out += t.token; continue; }
            const canon = String((info[t.label] || {}).ref_name || t.label || "").trim();
            out += "@" + (canon || t.label);
            hits += 1;
        }
        /* 认不出的 `@xxx`：逐个挑出来（refTokens 只在开了 token 可视化的框里标红，
         * 这里必须独立扫一遍，否则普通提示词框里的悬空引用毫无提示）。 */
        const unknown = [];
        const re = /@([^\s@，。；：、,.;:!?（）()\[\]【】<>"'`|]{1,80})/g;
        for (let m = re.exec(text); m; m = re.exec(text)) {
            const nm = String(m[1] || "");
            if (!nm || /^[0-9A-Za-z_]{1,3}$/.test(nm)) continue;   // @2x 之类不是素材名
            if (labs.some((l) => nm === l || nm.startsWith(l))) continue;
            if (!unknown.includes(nm)) unknown.push(nm);
        }
        const changed = out !== text;
        if (changed) api.value = out; else render(text);
        return { changed, hits, unknown };
    }

    /* 失焦/打字停顿后**自动**成框：默认**关闭**。
     * 自动改 DOM 会在打字时打断中文输入法组合，也会让人觉得"我打的字被改了"。
     * 现在的分工是：初始渲染照常把已有引用显示成绿框（看得见），
     * 手打的名字不再自动变，要点提示词框上的「解析」按钮（resolveRefs）。 */
    if (o.autoNormalize) {
        box.addEventListener("blur", () => { render(api.value); });
    }

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
        /* 只数 @别名 的框：token（<Picture N>）也带 dataset.label，混进来会让
         * domCount 虚高 → 漏判"还有裸别名没成框"（手打的 @别名 迟迟不变绿框）。 */
        box.querySelectorAll(".h3d-rtag:not(.h3d-rtok)").forEach((n) => {
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
        if (!o.autoNormalize) return;      // 手动模式：打字不自动补框（见 resolveRefs）
        clearTimeout(normTimer);
        normTimer = setTimeout(normalizeLoose, 700);
    });
    api.value = String(o.value || "");
    /* 把 api 挂回 DOM 元素：拿着元素（从 querySelector / 事件 target 来的）也能读到
     * 正文 —— contenteditable 的 div 本身没有 `.value`，不挂的话外部只能去解析
     * innerHTML（会被绿框的 span 污染）。测试与工具条按钮都靠这个反查。 */
    box.__h3Editor = api;
    return api;
}

/* 注：引用 token 的匹配/删除一律走 refTokens（最长优先分词），不再用
 * `@标签(?![0-9A-Za-z_])` 这类正则 —— 负向后顾挡不住中文，短标签会吃掉长标签前缀。 */

/* ---------- 引用条：三栏各一条，互不共享 ----------
 * 意图 / 剧本 / 结果 三栏各自持有一条引用栏，谁也不改谁：
 * 每条只读自己正文里的 @别名（正文即真相），点 chip 只写自己的正文。
 * 唯一的引用条 gate=true：走官方 9/3/3 上限、写 seg.refs / ds.prompts，
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
                const key = refKeyOf(a);
                const lbl = shortLabel(key) || key || "素材";
                const txt = c.querySelector(":scope > .h3d-chipbtn-text")
                    || c.appendChild(Object.assign(document.createElement("span"),
                        { className: "h3d-chipbtn-text" }));
                txt.textContent = `${lbl}${roles}`;
                const armedName = (REF_TEMPLATES[refTpl] || [])[0] || "裸引用（不加描述）";
                c.title = `${KIND_NAME[a.kind] || ""}「${lbl}」${roles}：`
                    + (n > 0
                        ? `本段已引用${token ? `（正文里每个 @${key} 都会编译成 ${token}）` : ""}`
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
                /* 签名含引用键：ref_name 变了（改名/重新入库）也要重建 chips，
                 * 只按 label 比会让 chip 一直停在旧名字上。 */
                const joined = arr.map((x) => refKeyOf(x)).join("\u0001");
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
            /* 编译映射条：逐行「标注 · 素材名 → 官方 token」。
             *
             * 为什么必须有：标注号（图片3）与官方 token 号（<Picture 1>）是**两层
             * 独立编号** —— token 按"本段挂载顺序"每段重算，标注是全局稳定号。
             * 只显示一层的界面会让人以为「图片3」编译出去还是 3，实际本段只挂它
             * 一张图时是 1。差异写在这里，一眼可核对。
             * 「改」= 改这个素材的标注（写库，走 /h3chain/asset_mark）。 */
            const mapBox = el("div", "h3d-refmap");
            const editMark = async (a) => {
                const cur = markTextOf(a);
                const raw = window.prompt(
                    `给「${refKeyOf(a)}」改标注（图片N / 视频N / 音频N，N 为 1-999）\n`
                    + "留空 = 清除，下次自动补号", cur);
                if (raw === null) return;
                const mk = cleanMark(raw);
                if (String(raw || "").trim() && !mk) {
                    window.alert("标注只能是「图片N / 视频N / 音频N」（N 为 1-999），例如：图片2");
                    return;
                }
                try {
                    await window.H3Api.assetMark({
                        dir: getDirValue(node), mark: mk,
                        asset_id: String((a && a.asset_id) || ""),
                        alias: String((a && a.label) || ""),
                    });
                    setLed("done", mk ? `标注已改成 ${mk}` : "标注已清除（下次自动补号）");
                    scheduleRefresh(120);
                } catch (e) {
                    window.alert(`改标注失败：${e?.message || e}`);
                }
            };
            const paintRefMap = (refs, tokens, live) => {
                mapBox.replaceChildren();
                const seen = new Set();
                for (const l of refs) {
                    if (seen.has(l)) continue;      // 重复引用只占一个编号 → 只列一行
                    seen.add(l);
                    const hit = (live || []).find((x) => refKeyOf(x) === l)
                        || pool.find((x) => refKeyOf(x) === l);
                    if (!hit) continue;
                    const row = el("div", "h3d-refmap-row");
                    /* 必须用 textContent：**官方 token 是 `<Picture 1>`**，走 el() 的
                     * innerHTML 会被当成标签吃掉（只剩空壳，符号一个字都不剩）。 */
                    for (const [cls, txt] of [["h3d-refmap-mark", markTextOf(hit) || "—"],
                        ["h3d-refmap-name", refKeyOf(hit)], ["h3d-refmap-tok", tokens[l] || ""]]) {
                        const sp = el("span", cls);
                        sp.textContent = txt;
                        row.append(sp);
                    }
                    const bEdit = el("button", "h3d-refmap-edit", "改");
                    bEdit.type = "button";
                    bEdit.title = "改这个素材的标注（图片N / 视频N / 音频N）";
                    bEdit.addEventListener("mousedown", guard(() => { editMark(hit); }));
                    row.append(bEdit);
                    mapBox.append(row);
                }
                mapBox.style.display = mapBox.children.length ? "" : "none";
            };
            const paintAll = () => {
                const live = livePool();
                syncChips(live);
                const refs = RB.readRefs();
                const cnt = { image: 0, video: 0, audio: 0 };
                const tokens = {};
                for (const l of new Set(refs)) {          // 编号按素材，不看重复
                    const hit = live.find((x) => refKeyOf(x) === l) || pool.find((x) => refKeyOf(x) === l);
                    if (!hit) continue;
                    const k = hit.kind || "image";
                    if (cnt[k] === undefined) cnt[k] = 0;
                    cnt[k] += 1;
                    tokens[l] = String(TOK_FMT[k] || TOK_FMT.image).replace("{}", cnt[k]);
                }
                for (const rec of chips) {
                    const n = refs.filter((x) => x === refKeyOf(rec.a)).length;
                    paintChip(rec, n, tokens[refKeyOf(rec.a)]);
                    /* ✕ 常驻显示（未引用时置灰）：以前"有引用才出现"，
                     * 用户想取消时找不到 —— 就是"点不了了"的来源。 */
                    rec.minus.classList.toggle("off", n <= 0);
                }
                /* 计数不常驻：引用条只回答一个问题 —— 这个素材本段有没有引用
                 * （chip 绿 / 灰 + 整条转绿）。再挂一行"已引用 N 个素材"是重复占版面。
                 * 但**官方 9/3/3 是硬约束**，超了生成时必失败，所以超限要立刻红字点名，
                 * 不能等到出片才发现 —— 这是唯一还允许出文字的地方。 */
                const nAsset = new Set(refs).size;
                const overKind = Object.keys(KIND_CAPS)
                    .filter((k) => (cnt[k] || 0) > KIND_CAPS[k]);
                let noteTxt = "";
                try {
                    noteTxt = String(typeof RB.note === "function" ? RB.note() : (RB.note || "")).trim();
                } catch (e) { noteTxt = ""; }
                const tail = noteTxt ? ` · ${noteTxt}` : "";
                if (overKind.length) {
                    counter.textContent = `已引用 ${nAsset} 个素材 · `
                        + overKind.map((k) => `${KIND_NAME[k]}${cnt[k]}/${KIND_CAPS[k]}`).join("、")
                        + ` 超官方单段上限，生成会失败${tail}`;
                    counter.classList.add("on", "h3d-refhint-bad");
                    counter.style.color = "";
                } else {
                    counter.textContent = nAsset ? tail.replace(/^ · /, "") : "本段未引用素材";
                    counter.classList.remove("on", "h3d-refhint-bad");
                    counter.style.color = "";
                }
                /* 整条转绿 = "本段挂了素材"的一眼判据（chip 一多，逐个找太慢） */
                refbar.classList.toggle("on", nAsset > 0);
                paintRefMap(refs, tokens, live);
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
                const key = refKeyOf(a);
                const roles = Array.isArray(a.roles) && a.roles.length ? `【${a.roles.join("·")}】` : "";
                const c = el("button", "h3d-chipbtn");
                c.type = "button";
                c.dataset.ref = key;
                /* 一次建好"图标 + 文本"骨架：paintChip 只更新文本，不再重建 DOM，
                 * 这样 chip 不会因为 paint 闪烁，鼠标悬停/焦点也保得住。 */
                buildChipBody(c, a);
                const minus = el("button", "h3d-chipminus", "✕");
                minus.type = "button";
                minus.title = `取消本段对「${key}」的全部引用（正文里的绿框一起清掉；`
                    + `只想取消某一处，用正文里那个绿框自带的 ✕）`;
                c.addEventListener("mousedown", guard(() => {
                    if (RB.gate && !canAddRef(node, segIdx, key)) return;
                    if (!ed) return;                       // 没有可写正文（只读/无节点）
                    {
                        const tplDef = REF_TEMPLATES[refTpl];
                        if (tplDef) {
                            /* 锚定方式：按句式写入（@别名 落成绿框，前后文一起进正文） */
                            const phrase = tplDef[1](key);
                            const m = /^(.*?)@([^\s]+)([\s\S]*)$/.exec(phrase);
                            if (m) ed.insertTag(key, m[1], m[3]);
                            else ed.insertText(phrase);
                        } else {
                            ed.insertTag(key);
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
                    if (inherited.includes(key)) {
                        setLed("warn", `「${key}」来自素材调度，生成时真的会送图；`
                            + "要取消请到素材调度里移除");
                        return;
                    }
                    if (ed) {
                        ed.removeTag(key);              // 正文里所有该绿框一次清掉
                        RB.commit(ed);
                    }
                    if (RB.gate) removeSegmentRef(node, segIdx, key);
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
            refbar.append(mapBox);
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
    /* 匹配表是**引用键**（ref_name 含后缀）。旧档没有 ref_name，refKeyOf 退回别名，
     * 于是旧正文里的 `@别名` 照样解析得出来 —— 新旧两写法共用一条匹配路径。 */
    const labs = (pool || []).map(refKeyOf).filter(Boolean)
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
        items = pool.filter((a) => refKeyOf(a).toLowerCase().includes(w)).slice(0, 8);
        if (!items.length) return;
        pop = el("div", "h3d-atpop");
        items.forEach((a, idx) => {
            const d = el("div", idx === sel ? "on" : "",
                `${escapeHtml(refKeyOf(a))}<small>${KIND_NAME[a.kind] || ""}</small>`);
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
        const key = refKeyOf(a);
        ta.value = ta.value.slice(0, c.start) + `@${key}` + ta.value.slice(pos);
        const np = c.start + key.length + 1;
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

/* 旧版工作流 widget 布局迁移到当前 30 值 schema。
 * 当前后端 define_schema 共 30 个控件值（29 控件 + 种子 control 占 1 位）。
 * 第二阶段提交 7435084 剔除了 8 个接缝/精修控件（接缝处理/混合帧数/精修强度/
 * 精修窗口/智能切镜/切镜最多丢帧/全链丢弃预算/自适应精修）并新增「自动成片」，
 * 旧 35 值布局因此中段错位、缺自动成片；更早的「导演台时代」极老工作流为 25 值。
 * 下面统一把这两种旧布局直接映射到当前 30 值：
 * 头部 0..16 位与当前一致，中段已删控件取当前默认值，自动成片插在生成模式前，
 * 生成模式/导演台状态从尾部取，缺失尾部按默认值补齐
 * （「参考图像尺寸」「响度对齐强度」恒补 match / 1.0）。
 *
 * ⚠ 2026-09-20 修：旧判据是 `wv.length !== CUR_WIDGET_COUNT`，而 CUR_WIDGET_COUNT
 * 是手写常数 —— 1148ae3 把控件从 29 值加到 31 值时忘了同步它，于是**完全正确的
 * 31 值工作流（含插件自带默认工作流）被判成旧布局并强行重排**，参数整体错位：
 * 锚定加噪收到"分段"、审片模式收到 0、重摇上限收到导演台状态 JSON…
 * 现改为按「值形态」识别，长度漂移不再致病。 */
const V35_WIDGET_COUNT = 35;   // 第二阶段剔除控件后、未加自动成片的陈旧布局
const V31_WIDGET_COUNT = 31;   // 「一采编码」还在时的上一版布局（2026-09-23 前）
const CUR_WIDGET_COUNT = 30;   // 当前 schema 控件值总数（仅供日志/断言，判据不依赖它）
// ⚠ 2026-09-23：「一采编码」控件已删（编码档位统一收进 ⚡ 性能优化设置），
// 布局 31 → 30 值。删除点在 idx28，**其后只有 参考图像尺寸(29) / 响度对齐强度(30)**，
// 前移到 28 / 29 —— 故 `wv[27]`（导演台状态）位置不变，形态判据仍然成立。

/* 当前布局判据：长度精确等于当前值数，或第 0 位（宽高比）与第 27 位
 * （导演台状态）都是字符串。这两条能覆盖全部历史布局：
 *   当前 30 值 / 上一版 29 值 → true（29 值只缺末尾两个控件，ComfyUI 会用控件默认值
 *                              补齐 match / 1.0，恰是所需，无需重排）
 *   35 值陈旧布局 → 第 27 位是数字「重摇上限」→ false
 *   极老 25 值布局 → 第 0 位是数字「宽度」→ false
 * 刻意不比较长度：控件增删会让长度漂移，忘了同步常数就会误迁移正确工作流。 */
function isCurrentWidgetLayout(wv) {
    if (!Array.isArray(wv)) return false;
    // 长度恰好等于当前值数 → 必然逐位对齐（历史布局是 25 / 29 / 31 / 35，撞不上）。
    // 这一条还兜住「已被旧版迁移层改坏、又被用户存进 ComfyUI 的工作流」：
    // 那种存档长度仍是当前值数，但第 27 位是数字 1.0，靠形态判据会被再迁一次。
    if (wv.length === CUR_WIDGET_COUNT && typeof wv[0] === "string") return true;
    // ⚠ 上一版 31 值布局（含已删的「一采编码」）**长度与形态都像当前布局**，
    // 但多一位 → 必须交给迁移层砍掉 idx28，否则整体右移一位、参数全错。
    if (wv.length === V31_WIDGET_COUNT) return false;
    // 否则看跨版本稳定的两个位置：宽高比（0）与导演台状态（27）都应是字符串
    return typeof wv[0] === "string" && typeof wv[27] === "string";
}

// 当前 schema「中段」默认值（位置 17..26）：锚定加噪, 审片模式, 自动保存, 重跑起始段,
// 接缝重摇, 重摇阈值, 重摇上限, 递减锚定, 自动成片
const CUR_MID_DEFAULTS = [0.0, "关闭", "分段", 0, "自动", 0.06, 1, "关闭", "开启"];

// 旧 35 值布局中、与当前控件一一对应的源下标
const V35_PICK = {
    aod: 19, review: 20, autosave: 21, rerun: 22,   // 锚定加噪, 审片模式, 自动保存, 重跑起始段
    reseam: 25, resth: 26, resmax: 27, decr: 29,    // 接缝重摇, 重摇阈值, 重摇上限, 递减锚定
    genmode: 33, ds: 34,                            // 生成模式, 导演台状态
};

/* 旧布局 wv → 当前 30 值布局。两条路径：
 *   · 31 值（上一版，含已删的「一采编码」，2026-09-23 前）→ **只砍 idx28**，
 *     其余位原样。它整体只差这一个控件，不该走下面的 V35 重建路径（那会
 *     从 V35 下标取中段，31 值布局的下标与 V35 不同 → 取错）。
 *   · 35/25 值等更老布局 → 头部 0..16 原样；中段 8 个从 V35 对应位取
 *     （更老布局这些位不存在则取 CUR_MID_DEFAULTS）；自动成片固定 "开启"；
 *     生成模式/导演台状态取尾部；末尾两个后加控件恒补 match / 1.0。 */
function remapOldWidgetValuesToCurrent(wv) {
    // ---- 路径 A：31 值（只少「一采编码」）----
    if (wv.length === V31_WIDGET_COUNT) {
        const out = wv.slice(0, V31_WIDGET_COUNT);
        out.splice(28, 1);          // 删「一采编码」；29/30 自动前移到 28/29
        return out;
    }
    // ---- 路径 B：更老布局（35 / 25 值等）----
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
        "match",                                        // 参考图像尺寸（idx28；原 29，一采编码已删）
        1.0,                                            // 响度对齐强度（idx29；原 30）
    ];
}

function migrateGraphWidgets(graphData) {
    if (!graphData || !Array.isArray(graphData.nodes)) return graphData;
    let migrated = 0;
    for (const n of graphData.nodes) {
        if (n.type !== NODE_TYPE || !Array.isArray(n.widgets_values) || !n.widgets_values.length) continue;
        const wv = n.widgets_values;
        // 已是当前布局则跳过；否则（极老数字首项 / 35 值陈旧）统一迁移。
        // 判据看形态不看长度 —— 长度会随控件增删漂移，比错判更危险（详见上方说明）。
        if (isCurrentWidgetLayout(wv)) continue;
        try {
            n.widgets_values = remapOldWidgetValuesToCurrent(wv);
            migrated += 1;
        } catch (e) {
            console.warn(`[h3-director] 节点 ${n.type} 参数迁移失败，保持原样交由兜底修正`, e);
        }
    }
    if (migrated) console.log(`[h3-director] 已迁移 ${migrated} 个旧版 H3 节点的参数（旧布局 → 当前 ${CUR_WIDGET_COUNT} 值）`);
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
    return { mode: MODE_DEFAULT, prompts: [""], first_frame: "", end_frame: "", last_frame: "", ref_images: [], ref_assets: [], segments: [], inserts: [], redo_segs: [], upscale: defaultUpscale(), bridge: defaultBridge() };
}

/* 语义桥（BUNNY H3 Conditioning Bridge 内联版）：对 H3 文本 conditioning 过一次
 * 5120→512→512→5120 的小网络，把「谁在做什么 / 道具归属 / 前后状态」的关系表达推清楚。
 *
 * 逐项目（跟二采设置同款，不像性能设置那样跟机器）：它改的是画出来的东西，该跟着作品走。
 * 关闭时后端一个张量都不碰 —— 不加载权重、不建张量，也不进任何指纹。
 * 开启时进 ckpt_params 指纹（`scope`/`alpha`/权重任一变化 → 整链重做）：cond 变了，
 * 只重做一半会做出「前几段带桥、后几段不带」的半条链，比整链重做糟得多。 */
function defaultBridge() {
    return { enabled: false, adapter: "", alpha: 0.15, scope: "all" };
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
             sharpen: 0.0, pixel_sharpen: 0.0,
             /* 3D 时序分块（`chunk`）与放大网络强制卸载（`force_unload`）**已迁到
              * 性能优化设置**（机器级、全局），项目存档里不再有这两个键 —— 见该字段表
              * 「放大网络 · 分块 / 常驻」两组。别再往这里加回来。 */
             device: "auto",
             sampler: "", scheduler: "", retry: false, retry_target: 0.15,
             include: [] };
}

function defaultSegment() {
    // 双按钮（null=跟随全局默认开）：auto_ref=false≈旧unlink，auto_seq=false≈旧disabled；
    // latent_ref（null=跟随全局尾部N帧注入），latent_save（null=默认全存）。
    /* 主框三段式（都**不进模型**，只有 ds.prompts[idx] 进模型）：
     *   intent_zh = 中文意图（支持 @素材，给人和 AI 看）
     *   script    = AI 扩写产物（官方格式剧本，可人工改）
     */
    return { intent_zh: "", script: "", scene_prompt: "", character_prompt: "", soundscape: "", music: "", seconds: null, refs: [], unlink: false, disabled: false, auto_ref: null, auto_seq: null, frame_refs: null, latent_save: null, latent_ref: null, tail_src: null, anchors: [] };
}

function segAutoRef(seg) {
    /* ⚠ 顺序不能反：`unlink` 是**显式赋值**（总提示词框 `Standalone:` / 段卡开关写的就是
     * 它），`auto_ref` 只是它的镜像。以前先读 auto_ref，于是刚从总提示词框贴进来的段
     * （base.unlink = true/false 已赋值，base.auto_ref 还是旧值）会被判成旧值 ——
     * 导出时 `Standalone:` 整行消失，用户刚贴进去的「独立镜头」回环一圈就丢了。
     * 口径：显式 unlink 优先，其次 auto_ref，最后默认 true。 */
    if (seg && typeof seg.unlink === "boolean") return !seg.unlink;
    if (seg && seg.auto_ref !== undefined && seg.auto_ref !== null) return !!seg.auto_ref;
    return true;
}

function segAutoSeq(seg) {
    if (seg && seg.auto_seq !== undefined && seg.auto_seq !== null) return !!seg.auto_seq;
    if (seg && typeof seg.disabled === "boolean") return !seg.disabled;
    return true;
}

/** manifest.seg_fields[全局槽位] -> 分段字段对象（与 defaultSegment 同构）。
 *  旧存档无该键/槽位为 null（序章/插入槽）→ 全默认空段；键缺失容错补齐。
 *  v2：latent_save 原样透存（对象才收，无则 null；后端 clean 收敛）。 */
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
        latent_save: (raw.latent_save && typeof raw.latent_save === "object") ? raw.latent_save : null,
        latent_ref: (raw.latent_ref && typeof raw.latent_ref === "object") ? raw.latent_ref : null,
        tail_src: (raw.tail_src && typeof raw.tail_src === "object") ? raw.tail_src : null,
        /* 手动锚定（双轨时间线）：读档必须还原，否则"看着有锚、跑起来没锚"——与 frame_img 同款坑 */
        anchors: Array.isArray(raw.anchors) ? raw.anchors : [],
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
                    /* 引用名：提示词里 @ 后面写的名字（**含格式后缀**）。缺省由落盘
                     * 文件名推导，再不行退回别名 —— 与后端 asset_store.ref_name_of 同口径。 */
                    ref_name: cleanRefName(a.ref_name || a.file || "") || "",
                    /* P3：全局库稳定 ID（空=旧本地条目；保存 manifest.assets 时剥离，住 asset_links） */
                    asset_id: typeof a.asset_id === "string" ? a.asset_id.trim() : "",
                    /* 标注（图片1/视频1/音频1）：给 AI 看的短名，后端落盘为准 */
                    mark: typeof a.mark === "string" ? a.mark.trim() : "",
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
            /* 引用名兜底：没推出来就用别名（旧档没后缀，但至少能被解析到，
             * 下一次 hydratePool 从 manifest 重建时会补成真名）。 */
            if (!a.ref_name) a.ref_name = a.label;
        });
        const taken = new Set();
        refAssets.forEach((a) => {
            a.label = uniqueLabelFrom(taken, a.label);
            taken.add(a.label);
        });
        /* 标注兜底：后端已落盘（图片1…）的为准，这里只给还没标过的旧档补号 */
        refAssets = assignMarks(refAssets);

        let segments = Array.isArray(raw.segments) ? raw.segments : [];
        while (segments.length < prompts.length) segments.push(defaultSegment());
        if (segments.length > prompts.length) segments = segments.slice(0, prompts.length);
        /* 三框合一迁移：旧档的「剧本」顶上正文（只在正文为空时），「意图」丢弃。
         * 幂等（正文一旦非空就不再动），所以不用额外标记，重复读不会改写用户输入。
         * intent_zh 之所以丢：它是"想拍什么"的一句话，已经没有 UI 承载，
         * 留着只会让人以为它还会进模型 —— 内容在 script 里（或已在正文里）。 */
        for (let i = 0; i < prompts.length; i += 1) {
            if (String(prompts[i] || "").trim()) continue;
            const sc = String((segments[i] || {}).script || "").trim();
            if (sc) prompts[i] = sc;
        }
        /* 引用键（ref_name）+ 旧别名都算合法：旧档 seg.refs 里存的是 label */
        const validLabels = new Set(refAssets.flatMap((a) => [a.label, refKeyOf(a)]).filter(Boolean));
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
                /* latent 策略：透存（防 getDs 归一化洗掉已存设置） */
                latent_save: (s?.latent_save && typeof s.latent_save === "object") ? s.latent_save : null,
                latent_ref: (s?.latent_ref && typeof s.latent_ref === "object") ? s.latent_ref : null,
                /* 段尾锚来源：{asset: 标签} | {latent: latent/x.pt} | null=无尾锚 */
                tail_src: (s?.tail_src && typeof s.tail_src === "object") ? s.tail_src : null,
                /* 手动锚定：透存（防 getDs 归一化洗掉已设 anchors；否则每帧渲染都把锚丢光） */
                anchors: Array.isArray(s?.anchors) ? s.anchors : [],
                /* v2 手动模式（null=跟随导演台） */
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
            /* 时序分块 / 强制卸载 / 编码档位都不在这里读了：
             *   前两者（`chunk` / `force_unload`）迁到性能优化设置（机器级、全局）；
             *   编码档位（`encode`）同在性能设置里，与 encoder / nvenc_cq 并排。
             * 项目存档里不再保留这三个键 —— 分块是显存手段、编码档是编码手段，
             * 都该跟着机器走，而不是跟着作品走。 */
            device: ["", "auto", "cuda", "rocm", "cpu"].includes(upRaw.device) ? upRaw.device : "auto",
            sampler: typeof upRaw.sampler === "string" ? upRaw.sampler.trim() : "",
            scheduler: typeof upRaw.scheduler === "string" ? upRaw.scheduler.trim() : "",
            retry: upRaw.retry === true,
            retry_target: upNum(upRaw.retry_target, 0.15, 0.05, 1.0),
            include: (Array.isArray(upRaw.include) ? upRaw.include : [])
                .map((x) => Number(x)).filter((x) => Number.isInteger(x) && x >= 0),
        };
        /* 语义桥配置（旧 JSON 无此键 = 关闭；与后端 semantic_bridge.config 同口径）。
           开启后它会进后端 ckpt_params 指纹 —— 改开关/强度/范围/权重都整链重做。 */
        const brRaw = raw.bridge && typeof raw.bridge === "object" ? raw.bridge : {};
        const bridge = {
            enabled: brRaw.enabled === true,
            adapter: typeof brRaw.adapter === "string" ? brRaw.adapter.trim() : "",
            alpha: upNum(brRaw.alpha, 0.15, 0.0, 1.0),
            scope: BRIDGE_SCOPES.includes(brRaw.scope) ? brRaw.scope : "all",
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
            bridge,
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
        latent_save: (s?.latent_save && typeof s.latent_save === "object") ? s.latent_save : null,
        latent_ref: (s?.latent_ref && typeof s.latent_ref === "object") ? s.latent_ref : null,
        tail_src: (s?.tail_src && typeof s.tail_src === "object") ? s.tail_src : null,
        anchors: Array.isArray(s?.anchors) ? s.anchors : [],
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
    const asset = (ds.ref_assets || []).find((a) => refKeyOf(a) === label);
    const kind = asset ? asset.kind : "image";
    const uniq = new Set(refs);
    if (!uniq.has(label)) {
        const sameKind = [...uniq].filter((l) => {
            const a = (ds.ref_assets || []).find((x) => refKeyOf(x) === l);
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
        const mf = await fetchManifest(dir);
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
    const goneKey = refKeyOf(gone);
    if (goneKey) {
        for (const s of ds.segments || []) {
            if (Array.isArray(s.refs)) {
                /* 引用键与旧的别名两种写法都清：旧档 refs 里存的是 label */
                s.refs = s.refs.filter((l) => l !== goneKey
                    && l !== String(gone.label || "")
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

/* ---- 总提示词（多段一次性分配）----
 * 定位：**纯粹的分段流水线**，不是结果框、不做 AI、不与段卡提示词双�向同步。
 * 把外部（AI / 另一个项目 / 手写）的一大段多段文本粘进来 → 「解析并分配」写回段落。
 * 只给每段三样东西：**提示词、时长、独立镜头**（是否跳过引用上段尾帧）。
 *
 * ★ 段头与段级标签**一律英文**（与提示词正文同为英文，避免中英混排被模型
 *   当成正文的一部分）。骨架：
 *   [Segment 1]      ← 段头：`[Segment 1]` / `[Seg 1]` / `[Part 1]` / `[Shot Group 1]` 等价；
 *                      序号可省（按出现顺序排段）
 *   Duration: 5      → seg.seconds（不写=不动该段时长）
 *   Standalone: yes  → seg.unlink（yes=断链不锚定上段尾帧，跳转/闪回/蒙太奇用；
 *                      no=连续接上段。旧写法 `独立镜头：是/否` 仍认）
 *   Prompt: …        → 段主体（ds.prompts[i]，即段卡那个框），可多行（续行不写标签）
 *                     正文按 H3 官方格式：三字段 integrated_multimodal_description /
 *                     overall_soundscape / non_diegetic_music（字段间空行），Ref2VA 六段式；
 *                     I2VA/FL2VA/L2VA 的关键帧对齐指令写在正文最前、空一行再接三字段。
 *                     **资产引用就写在正文里：@素材名**，与段卡提示词框同一套语义。
 *   [END]            ← 结束标记（可选）：其后的所有内容（如 AI 的参考素材建议）不参与解析
 *
 * 只读兼容（认出来 → 整块丢弃 → 提示「已忽略」，绝不悄悄并进正文）：
 *   参考 / 场景 / 角色 / 环境音 / 配乐 / 意图 / 剧本（中文旧标签）
 *   Reference / Scene / Character / Ambience / Music / Intent / Script（英文旧标签）
 * ——这些都是**上一代**的分层：场景/角色/环境音/配乐被官方三字段吸收（写进正文），
 *   意图/剧本不再进模型，参考则已统一成正文里的 @素材名。留着它们只是为了旧文本
 *   与外部 skill 的产出**不把不该进模型的内容静默塞进提示词**，所以认出即丢并点名。
 * 规则：段头后未带标签的正文行视为提示词内容；只认上述行首标签，其余文本原样进主体
 * （官方字段标签 integrated_multimodal_description: 等不受影响；官方字段里出现
 *  `Duration:` / `Prompt:` 这类词时由「正文块保护」挡住，不会冲掉段级元数据）；
 * 某标签「写了即生效（含写空=清空），没写不动该字段」。整个文本无任何段头时视为单段主体。
 * 容错：markdown 渲染界面复制常把段内换行合并成空格（软换行丢失），整段糊成一行；
 * 检测到段头不在行首独占即自动「重分行」（见 mpReflow）再按常规行解析。 */
/* 段头：英文为唯一写入口径（`[Segment N]` / `[Seg N]` / `[Part N]` / `[Shot Group N]`，
 * 序号可省）；中文旧写法 `【段1】` / `【第1段】` / `【段落1】` / `【1段】` 只读兼容 ——
 * 去掉它会让老文本的段头不匹配 → 整篇塌成一整段、段内标签全糊进正文且无警告。 */
const MP_HEAD_RE = /^(?:\[\s*(?:segment|seg|part|shot\s*group)\s*(\d+)?\s*\]|【\s*(?:第\s*)?(?:段(?:落)?\s*(\d+)?|(\d+)\s*段(?:落)?)\s*】)\s*$/i;
/* 【完】的中文写法保留（只读兼容老文本），新写一律 [END]。 */
const MP_END_RE = /^(?:\[\s*end\s*\]|【\s*(?:完|END|end|结束)\s*】)$/i;
/* 段级标签：英文为唯一写入口径；中文旧名只读兼容（认出来照样当段级标签解析，
 * 不然老文本里的「时长：8」会被当成正文吞进提示词）。 */
const MP_FIELD_RE = /^(Duration|Standalone|Prompt|Reference|Scene|Character|Ambience|Music|Intent|Script|时长|独立镜头|提示词|参考|场景|角色|环境音|配乐|意图|剧本)\s*[:：]\s*(.*)$/i;
const MP_FIELDS = {
    "duration": "seconds", "standalone": "unlink", "prompt": "main",
    "reference": "refs", "scene": "scene", "character": "character",
    "ambience": "soundscape", "music": "music", "intent": "intent", "script": "script",
    "时长": "seconds", "独立镜头": "unlink", "提示词": "main", "参考": "refs",
    "场景": "scene", "角色": "character", "环境音": "soundscape", "配乐": "music",
    "意图": "intent", "剧本": "script",
};
const MP_YES = ["是", "独立", "断链", "开", "true", "yes", "y", "on"];
const MP_NO = ["否", "连续", "关", "false", "no", "n", "off"];
/* 正文块字段：一旦进入，段级标签（时长/独立镜头/参考）就不再在正文里生效。
 * 背景：剧本正文是自由格式，模型常写「时长：9 秒」「配乐：无」这类行——
 * 当成段级标签会把 seg.seconds 冲成 NaN、剧本内容被截断，是个真实的解析冲突。 */
const MP_BODY_KEYS = new Set(["main", "intent", "script", "scene", "character",
    "soundscape", "music"]);
/* 段头子串版（无行锚，序号可省）：英文新写法 + 中文旧写法都认。
 * ⚠ 必须带 `g`：mpReflow 里是 `new RegExp(MP_HEAD_SUB_RE.source, "g")` 复用它做全局
 * 替换 —— 少一个 `g` 就成了「只替换第一处」甚至一处都不换（lastIndex 语义），
 * 表现为「挤成一行的文本重分行后还是 1 段」，全篇静默塌成单段。 */
const MP_HEAD_SUB_RE = /\[\s*(?:segment|seg|part|shot\s*group)\s*\d*\s*\]|【\s*(?:第\s*)?(?:段(?:落)?\s*\d*|\d+\s*段(?:落)?)\s*】/gi;

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
    /* 标签前缀必须带**词边界**：`Duration` / `Prompt` / `Scene` 这些英文词是正文与
     * 官方字段里极常见的普通词，没有边界的话 `Duration: 9 seconds of screen time.`
     * 会被拦腰断成「\nDuration: 9 …」，把一句正文拆成两行。中文标签天然无此问题，
     * 但统一加边界更省心（`(?<![\w])` 不认中文，中文仍靠后面的 [：:] 判据）。 */
    const fieldSub = new RegExp(
        "(?<![\\w])(" + Object.keys(MP_FIELDS).join("|") + ")\\s*[:：]", "gi");
    /* 去行锚的 [END] / 【完】子串版：两端标记必须整体处理，只认 `[` `]` 内部完整形式，
     * 否则正文里的 `[END]` 字面量会被误当结束标记切一刀。 */
    const endSub = MP_END_RE.source.replace(/^\^|\$$/g, "");
    /* ⚠ 重建正则必须**连同原 flags 一起带**（MP_HEAD_SUB_RE.flags = "gi"）：
     * 只写 "g" 会丢掉 `i`，于是 `[Segment 1]`（大写 S）匹配不上，重分行形同虚设 ——
     * 挤成一行的文本重分行后仍是 1 段，而且因为 notes 已经报了"已自动重分行"，
     * 用户只会看到"提示说重分了、但还是只有一段"。 */
    const headSub = new RegExp(MP_HEAD_SUB_RE.source, MP_HEAD_SUB_RE.flags);
    return lines.map((l) =>
        l.replace(headSub, "\n$&\n")                                       // 段头独占一行
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
            /* 还没遇到段头（= 无段头的单段文本）：空行必须留下来——官方三字段
             * 全靠空行分节，丢了就把三字段糊成一坨。以前这里一律丢弃，粘一段
             * 不带【段1】的官方格式文本进来就被压成一行。 */
            else if (stray.length) stray.push("");
            continue;
        }
        if (MP_END_RE.test(line)) break;               // 【完】：其后的建议/解说不参与解析
        const hm = line.match(MP_HEAD_RE);
        if (hm) {
            openSeg();
            const n = hm[1] !== undefined ? Number(hm[1])
                : (hm[2] !== undefined ? Number(hm[2])
                    : (hm[3] !== undefined ? Number(hm[3]) : 0));
            if (n && n !== out.segs.length) {
                out.notes.push(`段头序号 ${n} 与出现顺序（第 ${out.segs.length} 段）不一致，已按出现顺序排列`);
            }
            continue;
        }
        const fm = line.match(MP_FIELD_RE);
        if (fm) {
            if (!cur) { stray.push(line); continue; }   // 字段行出现在任何段头之前
            /* 标签查表必须**归一化**：MP_FIELD_RE 带 `i`（认 Duration / duration /
             * DURATION），而 MP_FIELDS 的键是小写 + 中文原名。直接 MP_FIELDS[fm[1]]
             * 会让 `Duration:` 查不到（undefined）→ 掉进 else 分支被当成正文写入
             * cur[undefined]，段时长静默丢失。 */
            const rawKey = fm[1].trim();
            const key = MP_FIELDS[rawKey] || MP_FIELDS[rawKey.toLowerCase()];
            if (!key) { pushBody(field || "main", line); continue; }
            /* 意图 / 剧本的**丢弃**放在解析之后统一做（见函数末尾）——这里照旧
             * 解析进 cur：剧本正文里写「时长：9 秒」是常态，靠 inBody 挡住才不会
             * 把段时长冲成 NaN；不解析就丢了这道保护。 */
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
                if (!isFinite(v) || v <= 0) out.notes.push(`「${rawKey}: ${fm[2]}」不是有效秒数，已忽略`);
            } else if (key === "unlink") {
                const v = fm[2].trim().toLowerCase();
                if (MP_YES.includes(v)) cur.unlink = true;
                else if (MP_NO.includes(v)) cur.unlink = false;
                else out.notes.push(`「${rawKey}: ${fm[2].trim()}」应为 yes/是 或 no/否，已忽略`);
            } else if (key === "refs") {
                cur.refs = fm[2].split(/[，,、;；]+/).map((s) => s.trim()).filter(Boolean);
            } else {
                pendingBlank = 0;                       // 换标签：之前记的空行不跨字段
                cur[key] = cur[key] === undefined ? fm[2].trim() : `${cur[key]}\n${fm[2].trim()}`;
            }
            if (MP_BODY_KEYS.has(key)) inBody = true;
            /* 段级标签（时长/独立镜头/参考）是**单行元数据**，后续裸正文一律归提示词。
             * 以前 field 直接跟着标签走，于是「参考：角色1」后面接正文时，正文被当成
             * 参考标签的续行并进了 refs（refs 从数组变成一坨字符串）——三框时代每段
             * 都以「提示词：」开头所以没暴露，单框后正文常常是裸写的，当场踩到。 */
            field = MP_BODY_KEYS.has(key) ? key : "main";
            continue;
        }
        if (!cur) { stray.push(line); continue; }       // 段头前的普通正文
        pushBody(field || "main", line);
    }
    /* 场景 / 角色 / 环境音 / 配乐 / 意图 / 剧本 / 参考：**只读兼容**（见文件头注释）。
     * 解析时照旧收进 cur（为了保住上面的 inBody 保护），收完立刻丢弃并提示：
     * ① 不当作正文吞掉 = 不能把「不进模型」的内容悄悄送进模型；
     * ② 也不静默保留 = 用户会以为内容还在。汇总成一条 note，不逐段刷屏。
     * 「参考」单独点名：它曾经是资产引用的**第二条通道**，现在只有正文里的
     * @素材名 算数，不提示的话用户会以为自己挂上了素材（实际没有）。 */
    let _nIgn = 0, _nRef = 0;
    for (const s of out.segs) {
        for (const k of ["scene", "character", "soundscape", "music", "intent", "script"]) {
            if (String(s[k] ?? "").trim()) _nIgn += 1;
            s[k] = undefined;
        }
        if (Array.isArray(s.refs) ? s.refs.length : String(s.refs ?? "").trim()) _nRef += 1;
        s.refs = undefined;
    }
    if (_nIgn) {
        out.notes.push(`已忽略 ${_nIgn} 处「场景 / 角色 / 环境音 / 配乐 / 意图 / 剧本」`
            + "（只有提示词进模型：场景/角色/环境音/配乐写进正文的官方三字段）");
    }
    if (_nRef) {
        out.notes.push(`已忽略 ${_nRef} 处「参考」标签：资产引用只认正文里的 @素材名`
            + "（与段卡提示词框同一套，由正文 @序列 派生 seg.refs）");
    }
    if (!out.segs.length && stray.length) {             // 无段头=单段（正文原样作主体）
        while (stray.length && !String(stray[0]).trim()) stray.shift();       // 去首尾空行
        while (stray.length && !String(stray[stray.length - 1]).trim()) stray.pop();
        openSeg();
        cur.main = stray.join("\n");
    } else if (stray.length) {
        out.notes.push(`忽略了 ${stray.length} 行出现在首个段头之前的内容`);
    }
    return out;
}

/** 工作台状态 -> 分段文本（[Segment N] + Duration / Standalone / Prompt）。
 *  段级标签只有这三个：**资产引用不在这里**——它就写在提示词正文里的 @素材名，
 *  与段卡提示词框同一套语义（导出正文 = 导出引用，不需要第二份清单）。
 *  ★ 一律英文（与正文同为英文）：`[Segment N]` / `Duration:` / `Standalone: yes|no`
 *  / `Prompt:` / `[END]`。中文旧标签仍可解析（只读兼容，见 parseMasterPrompt）。
 *  注意：**不再写 场景 / 角色 / 环境音 / 配乐 / 意图 / 剧本 / 参考**——这些层
 *  已经没有 UI 承载，写进文本只会在下次贴回时触发"已忽略"提示。 */
function mpRenderState(state) {
    const blocks = [];
    for (let i = 0; i < state.length; i++) {
        const seg = state[i] || {};
        const rows = [`[Segment ${i + 1}]`];
        if (Number.isFinite(Number(seg.seconds)) && Number(seg.seconds) > 0) rows.push(`Duration: ${seg.seconds}`);
        /* Standalone 是**两态显式值**，yes / no 都要写出来：
         * 以前只在 `seg.unlink` 为真时写 `Standalone: yes`，false 一侧干脆不写 ——
         * 于是「导出 → 贴回」的往返里 no 是**不可表达**的（贴回来时该标签缺席 =
         * 不动该字段，继承旧值）。用户看到的就是「我把 Standalone 改成 no，
         * 贴回去又变回 yes」。 */
        if (seg.unlink !== undefined && seg.unlink !== null) {
            rows.push(`Standalone: ${seg.unlink ? "yes" : "no"}`);
        }
        /* 提示词正文按 H3 官方格式（三字段 / Ref2VA 六段），自身含空行——
           标签独占一行、正文从下一行开始，贴回时按续行原样归入 main。 */
        const main = String(seg.main || "").trim();
        rows.push(main ? `Prompt:\n${main}` : "Prompt:");
        blocks.push(rows.join("\n"));
    }
    return blocks.join("\n\n") + "\n\n[END]";
}

/** 把当前链的提示词导出为总提示词文本（只写非空字段，可回贴/喂给外部改）。
 *  只用于「从当前链载入」这一个手动动作——工作台**打开时不再自动载入**：
 *  它不是一个跟着段卡提示词走的结果框，只是个分配框。 */
function exportMasterPrompt(node) {
    const ds = getDs(node);
    const prompts = ds.prompts || [];
    const segs = ds.segments || [];
    return mpRenderState(prompts.map((main, i) => {
        const seg = (i < segs.length && segs[i] && typeof segs[i] === "object") ? segs[i] : {};
        const sec = Number(seg.seconds);
        return {
            main: String(main ?? ""),
            seconds: Number.isFinite(sec) && sec > 0 ? sec : undefined,
            unlink: segAutoRef(seg) ? false : true,
        };
    }));
}

/** 解析并分配到当前链：prompts 重排为 N 段，segments 同步伸缩（既有段保留未提及字段），
 *  插入视频段（inserts）不动。**只赋三样**：提示词 / 时长 / 独立镜头。
 *  资产引用不在这里赋值——它跟段卡是同一套口径：**正文是唯一真相**，
 *  seg.refs 由正文里的 @素材名 序列派生（syncRefsFromText），没有第二份清单。
 *  返回解析结果供界面提示。 */
function applyMasterPrompt(node, text) {
    const p = parseMasterPrompt(text);
    if (!p.segs.length) return p;
    const ds = getDs(node);
    const old = Array.isArray(ds.segments) ? ds.segments : [];
    ds.prompts = p.segs.map((s) => (s.main === undefined ? "" : s.main));
    ds.segments = p.segs.map((s, i) => {
        const base = (i < old.length && old[i] && typeof old[i] === "object") ? { ...old[i] } : defaultSegment();
        /* 场景 / 角色 / 环境音 / 配乐 / 意图 / 剧本 / 参考 一律不写回：
         * parseMasterPrompt 已把它们标记为只读并丢弃，这里再赋值只会让
         * "已忽略"变成"忽略了一半"（提示有了、字段也写了）。 */
        if (s.seconds !== undefined) base.seconds = s.seconds;
        if (s.unlink !== undefined) {
            base.unlink = s.unlink;
            base.auto_ref = s.unlink ? false : true;
        }
        return base;
    });
    /* 引用派生必须在 ds.prompts / ds.segments 都就位之后：refsFromText 按"本段正文
     * 里的 @素材名 出现序列"出结果，与段卡提示词框**逐字同一条函数** —— 这里若另写
     * 一套（旧版的「参考：」标签），就会出现"总提示词框挂上的素材和段卡不是一回事"。 */
    for (let i = 0; i < ds.prompts.length; i++) {
        syncRefsFromText(ds, i, String(ds.prompts[i] ?? ""));
    }
    setDs(node, ds);
    /* 提示词是整段替换进来的（AI 优化产物只有官方三/六字段），对齐指令不会跟着来：
     * 按每段的首尾帧锚补回去，否则贴一次总提示词就把全链的首尾帧锚冲掉。
     * 没有锚的段 resync 内部直接返回，不会打扰手写文本。 */
    for (let i = 0; i < ds.prompts.length; i++) stripAlignmentLines(node, i);
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
/* 编码画质档已从三档「标准/高清/极致」压成开关 `encode_hq`（2026-09-23）——
   后端真源 `upscale._ENCODE_SETTINGS`（preset + aq + 抖动），crf 另由 `x264_crf`
   独立给。选项表已不存在，面板也不该再造一个。 */
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
    if (mergeSel.running) { alert("合并导出进行中：等它跑完再操作"); return; }
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
        const mf = await fetchManifest(dir);
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

/** 打开素材库的「合并模式」上下文：点一个素材就按选择顺序排进去。
 *  顺序与角标都由素材库那边维护，这里只收结果；两边共用同一个 mergeSel.order，
 *  所以关掉窗口再打开，之前选好的顺序与角标还在。 */
function openMergeLibrary() {
    if (!window.H3Lib?.open) { alert("素材库模块未加载（请刷新页面）"); return; }
    window.H3Lib.open({
        dir: getDirValue(findNode()) || lastDir || "",
        merge: true,
        order: (mergeSel.order || []).slice(),
        onMergeChanged: (order) => {
            mergeSel.order = Array.isArray(order) ? order.slice() : [];
            scheduleRefresh(0);
        },
        /* 素材库里的「⧉ 开始合并」= 导演台的「⧉ 合并导出」：收掉素材库窗口，
         * 回导演台跑（进度/LED/历史都长在这边，素材库不自己发合并请求）。 */
        onMergeCommit: (order) => {
            mergeSel.order = Array.isArray(order) ? order.slice() : [];
            const lb = document.querySelector(".h3l-overlay");
            if (lb) lb.remove();
            scheduleRefresh(0);
            doMergeExport(null);
        },
    });
}

/** `btn` 可以为 null：素材库里点「开始合并」时导演台的清单条可能根本没渲染
 *  （合并模式开着但中栏没画到那一条），没有按钮可改文案就直接跑。 */
async function doMergeExport(btn) {
    const dir = lastDir;
    if (!dir) { alert("没有当前项目目录（先读档或运行一次）"); return; }
    /* 顺序 = 用户在素材库里点选的顺序（mergeSel.order）。**这里绝对不能 sort**：
     * 合并顺序就是数据本身，排一下就把用户点出来的顺序改掉了。
     * asset 给后端按 id 精确解析（全局库素材也认），file 是回落路径。 */
    const items = [
        ...(mergeSel.order || []).map((x) => ({
            asset: String(x.id || ""), file: String(x.file || ""), name: String(x.name || ""),
        })),
        ...(mergeSel.files || []).map((f) => ({ file: f })),
    ];
    if (!items.length) { alert("先到素材库里点选要合并的素材（或上传外部视频）"); return; }
    const oldTxt = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "合并中…"; }
    mergeSel.running = true;                 // 互斥守卫只看这个（不看 on）
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
            /* 合并成功即收摊：退出模式并清空清单（与手动「✕ 退出」同一条路径）。 */
            mergeSel.on = false;
            resetMergeSel();
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
        mergeSel.running = false;      // 成功路径已 reset，这里兜底失败/异常
        if (btn) { btn.disabled = false; btn.textContent = oldTxt; }
    }
}

/* ---- 提示词写回防抖 ---- */

const _taTimers = new Map();
const _segTab = new Map();   // segIdx -> 'main'|'set'（切换式段卡记忆，不持久化；'v2' 已并入 main）
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


/** 由「分段素材调度」算出本段应有的官方引用标签（按 kind 分别编号 <Picture/Video/Audio k>）。 */
function v2RefsFromSchedule(ds, segIdx) {
    const seg = ((ds && ds.segments) || [])[segIdx] || {};
    const pool = (ds && ds.ref_assets) || [];
    const cnt = { image: 0, video: 0, audio: 0 };
    const out = [];
    /* 首尾帧锚**先**占位：它复用素材池编号（选为锚时已发 图片N），固定排在本段
     * <Picture k> 最前（首帧=1，再尾帧=2），参考素材顺延 —— 与后端
     * nodes._anchors_of + _kind_tokens 完全同口径。
     * 以前帧锚根本不进编号池，于是：① 对齐指令里的 <Picture 1> 在映射表查不到
     * 素材，被富文本框标成"悬空"红框（其实它就指向这张帧图）② 帧锚和第一个
     * 参考素材撞号，正文里手写的 <Picture 1> 和帧锚指向两张不同的图。 */
    const fi = (seg.frame_img && typeof seg.frame_img === "object") ? seg.frame_img : {};
    for (const fkey of ["first", "end"]) {
        const f = String(fi[fkey] || "").trim();
        if (!f) continue;
        const hit = pool.find((x) => x && String(x.file || "") === f);
        if (!hit) continue;
        const lbl = refKeyOf(hit);
        if (!lbl || out.some((r) => r.src === lbl)) continue;   // 同一张图只占一个号
        cnt.image = (cnt.image || 0) + 1;
        out.push({ label: `<Picture ${cnt.image}>`, src: lbl, anchor: fkey });
    }
    for (const label of (seg.refs || [])) {
        if (out.some((r) => r.src === label)) continue;         // 帧锚已占号
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
     * 结构化提示词下线后不再读逐镜结构 —— 参考素材的唯一真相是 seg.refs
     * （正文里 @别名 的同步产物，与后端段 refs 同源）。 */
    const seg = (ds?.segments || [])[segIdx] || {};
    const { has_start: hasStart, has_end: hasEnd } = segHasFrames(ds, segIdx);
    const nRefs = Array.isArray(seg.refs)
        ? seg.refs.filter((x) => String(x || "").trim()).length : 0;
    if (nRefs) return "Ref2VA";
    if (hasStart && hasEnd) return "FL2VA";
    if (hasStart) return "I2VA";
    if (hasEnd) return "L2VA";
    return "T2VA";
}


/** Python round() = 四舍六入五取偶（banker's）；JS Math.round 是四舍五入。
 *  段长吸附要和后端 nodes._snap_seconds **逐帧一致**，否则同一段前端算 124 帧、
 *  后端算 122 帧，对齐指令的 S.SS 又对不上（这就是时间同步要修的根）。 */
function pyRound(x) {
    const f = Math.floor(x);
    const d = x - f;
    if (d > 0.5) return f + 1;
    if (d < 0.5) return f;
    return (f % 2 === 0) ? f : f + 1;
}

/** 段长秒 -> 帧数：**唯一入口就是 snapFrames**，这里只是别名 —— 同一个数只许有
 *  一个算法（段卡「≈N帧」、锚定面板刻度、对齐指令的 S.SS 全走它）。 */
function snapSecondsToFrames(sec) {
    return snapFrames(sec);
}

/** 帧数 -> 对齐指令用的秒（两位小数），与后端 prompts.frames_to_seconds 同式。
 *  段长的真实单位是帧数，5 秒段实际是 124 帧 = 5.17s；直接用原始秒会写出 5.00，
 *  和后端实跑注入的 5.17 差 0.17 秒（锚点位置也跟着偏）。 */
function framesToSeconds(frames) {
    const f = Number(frames);
    if (!Number.isFinite(f) || f <= 0) return 5.0;
    return Math.round((f / 24) * 100) / 100;
}

/** 本段对齐指令里那个 S.SS：先吸附成帧数再反算秒 —— 显示、预览、后端注入
 *  三处必须都走这一步，看到的 S.SS 就是模型收到的 S.SS。 */
function segAlignSeconds(seg) {
    /* 兼容两种入参：段对象 `{seconds: 8}` 或直接的秒数 `8`。
     * **调用方必须先把段级留空解析成节点默认**（segmentSeconds）—— 本函数没有
     * node，拿不到「每段时长」，直接吃 `seg.seconds` 会把"跟随默认"当成 5 秒。 */
    const s = (seg && typeof seg === "object") ? seg.seconds : seg;
    return framesToSeconds(snapSecondsToFrames(s));
}

/** 官方对齐指令行（与后端 prompts.py 同口径：FL2VA 单句双锚、L2VA 末帧句、
 *  I2VA keyframe_line；时长一律取本段 seconds）。无锚时返回空数组。 */
function v2InstrLines(mode, seconds, hasStart, hasEnd, nShots) {
    const dur = segAlignSeconds({ seconds }).toFixed(2);
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


/* ---- AI提示词优化（自研后端 /h3chain/optimize，轻量移植测试分支能力） ----
 * 主框=最终文本；优化结果双写主框+回填v2（可解析字段才回填，失败只写主框）。
 * 配置存 ds.optimizer；原稿/优化稿历史存 ds.opt_hist，**按项目分桶**（见 optKey）。 */
/* ---- 原稿/优化稿：键里为什么必须带"项目目录" ------------------------------
 * 这三个 Map 是模块级（浏览器进程级），键以前只有 `node.id:idx`：切到另一个项目后
 * 同一个段号**仍然命中上一个项目的记录** —— 「原稿」按钮显示的是别的项目的状态，
 * 点下去还会把别的项目的老稿写进当前段；而恢复函数 optRestoreMaps 只在"键不存在"
 * 时才填，新项目的历史永远进不来。两头一夹就是"先到者胜"，症状正是
 * 「切了项目，原稿/优化稿切换不跟着换」。
 * 现在键里带 dir：跨项目天然命中不到，不依赖谁记得清。ds.opt_hist 同步改成
 * { dir: { idx: 记录 } } 分桶 —— 历史跟着项目走，也仍然留在工作流里。 */
const _optBefore = new Map();
const _optAfter = new Map();
const _optShown = new Map();

function optKey(node, idx) {
    let dir = "";
    try { dir = getDirValue(node) || ""; } catch (e) { dir = ""; }
    return `${dir}|${node ? node.id : 0}:${idx}`;
}

/** 丢掉不属于 dir 的原稿/优化稿缓存。键带 dir 本来就命中不到，这里只为别让缓存
 *  无限攒 —— 数据在 widget 的 ds.opt_hist[dir] 里，切回去会由 optRestoreMaps
 *  重新装回来，丢了不心疼。 */
function optPurgeOtherScopes(dir) {
    const keep = String(dir || "") + "|";
    for (const m of [_optBefore, _optAfter, _optShown]) {
        for (const k of [...m.keys()]) {
            if (!k.startsWith(keep)) m.delete(k);
        }
    }
}
let _optBusy = null;
const _optRulesCache = { loaded: false, files: {} };

/** AI 扩写优化设置的默认值（段卡「AI扩写+优化」按钮用，配置在 AI 优化设置里）。
 *  以前这些参数在扩写弹窗里（填完才跑），现在弹窗没了，统一住进 ds.optimizer。
 *  「每段时长范围」已删：前端只剩单段扩写（segment_count:1），请求里的时长锁死为
 *  本段时长（见 runExpandOptimizeForSegment），这个范围没有任何消费方。 */
function optDefaultExpandSettings() {
    return {
        style: "balanced",            // strict / balanced / creative
        then_optimize: true,          // 扩写后接着做提示词优化（关掉 = 只扩写不优化）
    };
}

function optGetExpandSettings(node) {
    const s = optGetSettings(node);
    const saved = (s && s.expand && typeof s.expand === "object") ? s.expand : null;
    return Object.assign(optDefaultExpandSettings(), saved || {});
}

/* 配置版本。v2 = 默认服务商由 RunningHub 换成智谱 GLM（模型 GLM-4.6V）。
 * v3 = 默认模型 GLM-4.6V -> GLM-5.3-FlashX。
 * 老工作流里存的仍是旧默认值，而那套默认值开箱就是坏的（没有 Key、模型是
 * 预设占位名，请求只能超时）—— 所以老配置必须迁移，否则"改了默认值"对
 * 已经存过档的用户毫无作用。迁移只在**确认用户没动过**时执行。 */
const OPT_CFG_VER = 3;
const OPT_LEGACY_DEFAULT = { providers: ["runninghub", "runninghub_overseas"], model: "openai/gpt-5.6-sol" };
/* v2 的默认模型：cfg_ver==2 且服务商还是 glm、模型还是它 → 说明用户没挑过型号，
 * 可以安全地升到 v3 的默认。挑过别的型号（或换了服务商）就不动。 */
const OPT_V2_DEFAULT_MODEL = "glm-4.6v";

function optDefaultSettings() {
    return {
        mode: "api", provider: "glm",
        api_url: "https://open.bigmodel.cn/api/paas/v4", api_key: "", api_keys: {},
        model: "glm-5.3-flashx", provider_models: {}, protocol: "openai",
        read_media: true, output_language: "中文",
        local_model: "", local_mmproj: "", local_device: "cuda",
        max_tokens: 8192, timeout: 300, thinking: "disabled", reasoning_effort: "",
        rule_file: "auto",          // 开关删留见 openOptSettings 注释
        cfg_ver: OPT_CFG_VER,
        expand: optDefaultExpandSettings(),
    };
}

function optMigrateSettings(saved) {
    if (!saved || typeof saved !== "object") return null;
    const ver = Number(saved.cfg_ver) || 0;
    if (ver >= OPT_CFG_VER) return saved;
    const out = Object.assign({}, saved);
    if (ver < 2) {
        const keys = (out.api_keys && typeof out.api_keys === "object") ? out.api_keys : {};
        /* "没动过" = 还是那两个旧预设之一 + 模型还是旧预设占位名 + 该服务商下没填过 Key。
         * 三条同时成立才迁移 —— 用户自己填过任何一项，都说明他在用别的服务商，
         * 这时候改他的 provider 等于把他配置弄丢。 */
        const untouched = OPT_LEGACY_DEFAULT.providers.includes(String(out.provider || ""))
            && String(out.model || "") === OPT_LEGACY_DEFAULT.model
            && !String(keys[out.provider] || out.api_key || "").trim();
        if (untouched) {
            const p = OPT_PROVIDERS.glm;
            out.provider = "glm";
            out.api_url = p.url;
            out.model = p.model;
            out.protocol = p.protocol;
        }
    }
    /* v2 -> v3：只认"服务商还是 glm 且型号还是 v2 的默认"这一种组合。
     * 这里**不看 Key**（与 v2 那段不同）：填过 GLM 的 Key 只说明他认了这个服务商，
     * 型号仍是插件给的默认，跟着升到新默认是符合预期的；而 v1 那段是在
     * 跨服务商搬（RunningHub -> GLM），搬错代价高，才要额外确认没填过 Key。 */
    if (String(out.provider || "") === "glm"
        && String(out.model || "") === OPT_V2_DEFAULT_MODEL) {
        const p = OPT_PROVIDERS.glm;
        out.model = p.model;
        out.api_url = p.url;
        out.protocol = p.protocol;
    }
    out.cfg_ver = OPT_CFG_VER;
    return out;
}

function optGetSettings(node) {
    const ds = getDs(node);
    const saved = ds && ds.optimizer && typeof ds.optimizer === "object" ? ds.optimizer : null;
    return Object.assign(optDefaultSettings(), optMigrateSettings(saved) || {});
}

/* 服务端是否内置了 Key（optimizer.local.json）。前端 Key 框为空**不等于**不能用：
 * 服务端会在服务商与默认服务商一致时用自己的 Key 兜底。所以判"能不能跑"
 * 必须问一次服务端，只看输入框会把开箱可用的用户拦回设置面板。 */
const _optBackend = { loaded: false, hasKey: false, providers: null };

async function optBackendProbe() {
    if (_optBackend.loaded) return _optBackend;
    try {
        if (window.H3Api?.getOptimizerConfig) {
            const r = await window.H3Api.getOptimizerConfig();
            if (r.body?.ok) {
                _optBackend.hasKey = r.body.has_default_key === true;
                _optBackend.providers = r.body.providers || null;
                optCapsLoad(r.body);
            }
        }
    } catch (e) { /* 探不到就当作没有，退回原来的"拦人填 Key"行为 */ }
    _optBackend.loaded = true;
    return _optBackend;
}

/** 该不该把用户拦到设置面板：本地缺模型 / 云通道既没填 Key 服务端也没有兜底 Key。 */
async function optNeedsSetup(node, st) {
    if (st.mode === "local") {
        if (st.local_model) return false;
    } else if (String(st.api_key || "").trim()) {
        return false;
    } else if ((await optBackendProbe()).hasKey) {
        return false;
    }
    openOptSettings(node);
    return true;
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
    const pool = ds.ref_assets || [];
    const infoOfFile = (f) => pool.find((a) => a && String(a.file || "") === f) || {};
    const picks = [];
    const anchorNotes = [];
    /* 同一张图只挂一次：它可能**同时**是帧锚和被 @引用的素材（用户就是想让画面
     * 从这张图开始、并且长得像它）。按 file 去重 —— 不拦的话 media 里会出现两张
     * 一模一样的图、两个同名的 @图片1，LLM 收到编号 1 和 2 却长得一样，
     * 会以为自己看错而把描述写歪；还白占一个官方 9 图的名额。 */
    const pickedFiles = new Set();
    /* 帧锚**复用素材池编号**（选为锚时已领 图片N），所以它有 mark / label，
     * 和参考素材一起进 media —— 模型按 <Picture k> 认图，帧锚排在最前。
     * 以前帧图没 mark → media 第一项 label 是空字符串，LLM 收到一张"无名图"，
     * 既不知道它是锚，也没法在正文里引用它。 */
    const addAnchor = (file, role) => {
        if (!file || pickedFiles.has(file)) return;
        pickedFiles.add(file);
        const info = infoOfFile(file);
        const lbl = String(refKeyOf(info) || frameNameOf(file) || "").trim();
        const mk = String(markTextOf(info) || "").trim();
        picks.push({ file, asset_id: String(info.asset_id || ""), mark: mk,
            label: lbl, role });
        if (mk) anchorNotes.push(`@${mk}`);
    };
    if (firstFile) addAnchor(firstFile, "首帧锚点：画面必须从这张图开始（0.00s 硬钉）");
    if (endFile) addAnchor(endFile, "尾帧锚点：画面必须在这一帧结束（末帧硬钉）");
    for (const key of ((Array.isArray(seg.refs) ? seg.refs : []).slice(0, 8))) {
        const hit = pool.find((a) => a && (refKeyOf(a) === key || a.asset_id === key));
        if (!hit) continue;
        /* ⚠ **不许按 kind 过滤**（以前只收图片，把视频/音频整类丢掉了）：
         * 后端 REF_CAPS 是 {image:9, video:3, audio:3}，官方标签也
         * 分了 <Video N> / <Audio N> —— 视频与音频是一等公民。丢掉的话模型
         * 根本不知道本段挂了参考视频/音频，永远写不出 <Video N> / <Audio N>。 */
        if (pickedFiles.has(hit.file)) continue;      // 已是帧锚 → 不重复挂
        pickedFiles.add(hit.file);
        const _kind = String(hit.kind || "image").toLowerCase();
        picks.push({ file: hit.file, asset_id: hit.asset_id || "", kind: _kind,
            label: refKeyOf(hit), mark: markTextOf(hit),
            role: `参考${_kind === "video" ? "视频" : _kind === "audio" ? "音频" : "素材"}「${refKeyOf(hit)}」` });
    }
    const media = [];
    const notes = [];
    /* 标注 -> 素材全名：LLM 返回的是 `@图片1`，回写前要用它译回 `@女主.png` */
    const markMap = {};
    for (const asset of picks) {
        /* label 用**标注**（图片1 / 视频1 / 音频1），不再用素材全名：
         * LLM 看见长文件名（`微信图片_20260730….png`）很容易抄错，标注短而稳，
         * 且不会凭空造出一个不存在的素材名。返回后由 decodeMarks 译回真名。
         * 首尾帧没有标注 → 回落到引用名（后端按 label 过滤空值）。 */
        const nm = String(asset.mark || asset.label || "");
        const _k = String(asset.kind || "image").toLowerCase();
        if (_k === "audio") {
            /* 音频没有画面可预览，也**不该**塞进 images：它只是"有这条素材"的
             * 声明。带上 kind 让模型知道该写 <Audio N> 而不是 <Subject N>。 */
            if (asset.mark && asset.label) markMap[asset.mark] = asset.label;
            media.push({ kind: "audio", label: nm });
            if (nm) notes.push(`@${nm}`);
            continue;
        }
        if (media.length >= 8) break;
        const dataUrl = await optImageToDataUrl(
            assetPreviewUrl(getDirValue(node), asset.file, asset.asset_id));
        if (!dataUrl) continue;
        if (asset.mark && asset.label) markMap[asset.mark] = asset.label;
        /* 视频：取首帧当缩略图，但 kind 必须是 "video" —— 否则模型会拿
         * <Subject N> 去指代一条视频，官方语义直接错位。 */
        media.push({ kind: _k, label: nm, images: [dataUrl] });
        if (nm) notes.push(`@${nm}`);
    }
    const base = notes.length
        ? `随图说明：可用素材 ${notes.join("、")}`
          + "（正文里直接写 @名字 引用，不要写 <Picture N>）。"
        : "";
    /* 混合模式的关键一句：模型必须分清"哪几张是硬钉的锚、哪些只是参考"。
     * 不点名的话 LLM 会把首帧图当成普通素材去"描述外观"，甚至把它写进
     * subject_definitions 当参考项 —— 锚点语义就丢了，出片也不从它开始。
     *
     * 措辞按**实际角色**区分：只有首帧 / 只有尾帧 / 两者都有 —— 不能含糊地
     * 写"画面必须从它开始 / 必须在它结束"，否则用户只设了尾帧图时模型会以为
     * 这张图也是首帧（"以 X 为首帧与结尾定格"就是由此而来）。 */
    const anchorNote = anchorNotes.length
        ? (() => {
            const isFirst = !!firstFile;
            const isEnd = !!endFile;
            const role = isFirst && isEnd
                ? "**首尾帧锚点**（画面必须从它开始、也必须在它结束，是硬钉的关键帧）"
                : isFirst
                    ? "**首帧锚点**（画面必须从它开始，0.00s 硬钉，**不是**尾帧）"
                    : "**尾帧锚点**（画面必须在这一帧结束，末帧硬钉，**不是**首帧）";
            return `\n其中 ${anchorNotes.join("、")} 是${role}（不是普通参考素材）。`
                + "不要把它当素材去描述外观，也不要改写它的编号；"
                + "其余素材才是“参考一下”的角色。"
                + "写 subject_definitions 时锚点标 fully_preserved，参考素材标 partially_preserved。";
        })()
        : "";
    return { media, markMap, note: base + anchorNote };
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


function optPersist(node, idx, shown) {
    try {
        const ds = getDs(node);
        const dir = String(getDirValue(node) || "");
        if (!ds.opt_hist || typeof ds.opt_hist !== "object") ds.opt_hist = {};
        const bucket = (ds.opt_hist[dir] && typeof ds.opt_hist[dir] === "object")
            ? ds.opt_hist[dir] : {};
        const key = optKey(node, idx);
        bucket[String(idx)] = {
            before: _optBefore.get(key) ?? null,
            after: _optAfter.get(key) ?? null,
            shown: shown || "after",
        };
        ds.opt_hist[dir] = bucket;
        setDs(node, ds);
    } catch (e) { /* 持久失败不阻断 */ }
}

function optRestoreMaps(node, ds) {
    try {
        const hist = (ds || {}).opt_hist;
        if (!hist || typeof hist !== "object") return;
        const dir = String(getDirValue(node) || "");
        /* 旧的扁平结构 {idx: 记录} 没有 dir，无法判断属于哪个项目 —— 那正是
         * "切了项目还显示上一个项目原稿"的来源，直接丢弃（纯数字键不可能是目录名）。 */
        for (const k of Object.keys(hist)) {
            if (/^\d+$/.test(k)) delete hist[k];
        }
        const bucket = (hist[dir] && typeof hist[dir] === "object") ? hist[dir] : {};
        for (const [k, h] of Object.entries(bucket)) {
            if (!h || typeof h !== "object") continue;
            const key = optKey(node, k);
            /* 不再有 !has 守卫：键已经带 dir，命中就是本项目的记录，
             * 工作流里存的才是权威，Map 里的旧值必须让位。 */
            if (h.before != null) _optBefore.set(key, h.before);
            if (h.after != null) _optAfter.set(key, h.after);
            if (h.shown) _optShown.set(key, h.shown);
        }
    } catch (e) { /* 忽略 */ }
}

/* ---- 优化进度条（SSE 实时进度）-------------------------------------------
 * 思考模型一次改写 30~90 秒，前几十秒正文一个字都没有 —— 没有进度条，用户看到
 * 的就是"点下去就卡死"。所以优化改走 /h3chain/optimize_stream（SSE），这里把
 * 事件画成一条**阶段进度**。
 *
 * 为什么不按 token 画真实比例：实测（tools/_probe_stream_usage.py：glm-4.6v /
 * glm-5.3-flash，带不带 stream_options 都一样）**usage 只在最后一帧出现**，
 * 中途 completion_tokens 恒为 0。按 token 画 = 全程停在 0%，比没有还糟。
 * 所以改成三段式：
 *   连接 0→8% / 思考 8→60% / 撰写 60→99%，段内按已生成字数渐近推进。
 *
 * 「扩写+优化」是**两次** LLM 调用（扩写 → 优化），进度帧多带一个 `stage`，
 * 于是画成两格：① 扩写 4→45%，② 优化 45→99%。两次都是 20~40 秒级，
 * 只报一格的话"扩写"那半程看起来就是原地不动。 */
const _optProg = { box: null, timer: null };

function optProgressClose() {
    if (_optProg.timer) { clearInterval(_optProg.timer); _optProg.timer = null; }
    if (_optProg.box) { _optProg.box.remove(); _optProg.box = null; }
}

/** 开一条进度条。返回的句柄：update(事件) / done() / fail() / close() /
 *  cancelled() / onCancel(fn) / onPhase(fn)。 */
function optProgressStart(title) {
    optProgressClose();
    const box = el("div", "h3d-opt-prog");
    const head = el("div", "h3d-opt-prog-head");
    head.append(el("span", "h3d-opt-prog-name", escapeHtml(title || "提示词优化")));
    const cancelBtn = el("button", "h3d-opt-prog-cancel", "取消");
    cancelBtn.type = "button";
    head.append(cancelBtn);
    const track = el("div", "h3d-opt-prog-track");
    const fill = el("div", "h3d-opt-prog-fill");
    track.append(fill);
    const meta = el("div", "h3d-opt-prog-meta", "正在连接模型…");
    box.append(head, track, meta);
    document.body.append(box);
    _optProg.box = box;

    const st = { pct: 4, phase: "connect", rchars: 0, cchars: 0, tokens: 0, maxTokens: 0,
                 stage: "", two: false, segNo: 0, total: 0, t0: Date.now() };
    let cancelled = false;
    let onPhase = null;
    /* 渐近推进：字数越接近"典型量"越慢，永不越过阶段上界。
     * 900 / 2200 是**实测手感值**（tools/_probe_progress_timeline.py）：
     *   glm-4.6v 关思考     → 只有撰写，正文 399 字 / 5.8s
     *   glm-5.3-flash 关闭  → 思考 70 字 → 撰写 642 字 / 4.7s
     *   glm-5.3-flash max   → 思考 13255 字 → 撰写 908 字 / 43.6s
     * 思考段跨度太大（70 ~ 13000 字），半衰值取 2500 折中：短思考几乎立刻
     * 进撰写段，长思考也能一路爬到 60% 而不是早早顶住不动。不是精确模型 ——
     * 只求"一直在动、不倒退、不虚报完成"。 */
    const creep = (chars, half) => 1 - Math.exp(-Math.max(0, chars) / half);

    const render = () => {
        let pct = st.pct;
        let phaseText = "正在连接模型…";
        if (st.total > 1) {
            /* 多段（optimizeMulti）：N 段**串行**，每段内部仍是 thinking/writing。
             * 整条 = 已完成段数 + 本段内部进度：段内 连接 0→5% / 思考 5→50% /
             * 撰写 50→100%，跨段靠 st.pct 的单调守卫接续（看不到回退）。
             * 用 `total > 1` 判定而不是"调用方自称多段"：只有一段真要跑时，
             * 单段那条曲线更准确（空段已被后端跳过，不进分母）。 */
            const per = 100 / st.total;
            const base = (st.segNo - 1) * per;
            if (st.phase === "thinking") {
                pct = base + per * (0.05 + 0.45 * creep(st.rchars, 2500));
                phaseText = `第 ${st.segNo}/${st.total} 段 · 思考中 · 推理 ${st.rchars} 字`;
            } else if (st.phase === "writing") {
                pct = base + per * (0.5 + 0.5 * creep(st.cchars, 1600));
                phaseText = `第 ${st.segNo}/${st.total} 段 · 撰写中 · 正文 ${st.cchars} 字`;
            } else {
                pct = base + per * 0.05;
                phaseText = `第 ${st.segNo}/${st.total} 段 · 连接中…`;
            }
        } else if (st.two && st.stage === "expand") {
            /* 第①格：扩写。它内部也有 thinking/writing 之分，但对用户是**一个**
             * 步骤（"先把意图写开"），所以两段合起来占 4→45%。
             * 字数取 cchars || rchars：思考阶段只有推理字数，撰写阶段才有正文。 */
            const chars = st.cchars || st.rchars;
            pct = 4 + 41 * creep(chars, 1200);
            phaseText = (st.phase === "thinking" ? "① 扩写剧本 · 思考中" : "① 扩写剧本 · 撰写中")
                + ` · 已生成 ${chars} 字`;
        } else if (st.phase === "thinking") {
            pct = (st.two ? 45 : 8) + (st.two ? 15 : 52) * creep(st.rchars, 2500);
            phaseText = (st.two ? "② 优化格式 · 思考中" : "思考中") + ` · 推理 ${st.rchars} 字`;
        } else if (st.phase === "writing") {
            /* 没经过思考阶段（关掉思考的型号）就别从 60% 起跳 —— 那一段
             * 对用户根本没发生过，凭空跳一格只会让人以为漏了什么。
             * 两段式里第①格已经垫到 45%，所以第②格恒定从 60% 起。 */
            const base = st.two ? 60 : (st.rchars > 0 ? 60 : 10);
            const span = st.two ? 39 : (st.rchars > 0 ? 39 : 89);
            pct = base + span * creep(st.cchars, 1600);
            phaseText = (st.two ? "② 优化格式 · 撰写中" : "撰写中") + ` · 正文 ${st.cchars} 字`;
        }
        if (st.tokens && st.maxTokens) {
            pct = Math.max(pct, Math.min(99, 100 * st.tokens / st.maxTokens));
        }
        st.pct = Math.max(st.pct, pct);
        fill.style.width = `${Math.min(99, st.pct).toFixed(1)}%`;
        const secs = ((Date.now() - st.t0) / 1000).toFixed(1);
        meta.textContent = `${phaseText} · 已用 ${secs} 秒`
            + (st.tokens && st.maxTokens ? ` · ${st.tokens}/${st.maxTokens} token` : "");
        /* 往外报的"阶段"用**用户视角**的粒度：两段式里第①格的 thinking/writing
         * 都属于「扩写」，报给按钮文案时应该还是"扩写中…"，而不是细化成
         * "思考中…"（用户关心的是"现在在扩写还是在优化"）。 */
        if (onPhase) {
            const report = (st.two && st.stage === "expand") ? "expand" : st.phase;
            try { onPhase(report); } catch (e) { /* 忽略 */ }
        }
    };
    render();
    /* 本地 250ms 心跳：事件是被节流过的（服务端 0.2s 一帧，且连上之前一帧都没有），
     * 没有它，慢的首帧会让秒数冻在 0.0 上，看着还是像卡死。 */
    _optProg.timer = setInterval(render, 250);

    return {
        cancelled: () => cancelled,
        onCancel(fn) {
            cancelBtn.onclick = () => {
                cancelled = true;
                cancelBtn.disabled = true;
                cancelBtn.textContent = "取消中…";
                try { fn && fn(); } catch (e) { /* 忽略 */ }
            };
        },
        onPhase(fn) { onPhase = fn; },
        update(evt) {
            if (!evt || evt.type !== "progress") return;
            /* `stage` 是「扩写+优化」才有的字段（expand / optimize）；单段优化
             * 的帧里没有 → 保持一格进度。 */
            if (evt.stage) { st.stage = String(evt.stage); st.two = true; }
            /* `seg_no` / `total` 是「多段优化」才有的字段。换段时**显式清零**
             * 段内计数器：每段是一次独立的 LLM 调用，字数各自从零开始。
             * 流式帧通常会把三个字段全带上（等于自动覆盖），所以这里是防御性的 ——
             * 但换段是个真实的状态边界，靠"事件一定带全字段"这种隐式约定，
             * 容易在别的协议（gemini/responses）或本地模型分支上加流式时翻车。 */
            if (evt.total) st.total = Number(evt.total) || 0;
            const no = Number(evt.seg_no) || 0;
            if (no && no !== st.segNo) {
                st.segNo = no;
                st.rchars = 0; st.cchars = 0; st.tokens = 0;
            }
            if (evt.phase) st.phase = String(evt.phase);
            st.rchars = Number(evt.reasoning_chars) || 0;
            st.cchars = Number(evt.content_chars) || 0;
            st.tokens = Number(evt.tokens) || 0;
            st.maxTokens = Number(evt.max_tokens) || 0;
            render();
        },
        done() {
            if (_optProg.timer) { clearInterval(_optProg.timer); _optProg.timer = null; }
            fill.style.width = "100%";
            box.classList.add("h3d-done");
            meta.textContent = `完成 · 用时 ${((Date.now() - st.t0) / 1000).toFixed(1)} 秒`;
            cancelBtn.remove();
            setTimeout(optProgressClose, 1400);
        },
        /* 失败/取消一律**立刻撤掉**：错误信息由调用方的 alert 负责说清楚，
         * 面板再挂一条只会和弹窗重复，还挡住下面的卡片。 */
        close: optProgressClose,
    };
}

/** 走 SSE 流式调用并驱动进度条。返回与整包版同形的 {status, body}。
 *
 *  opts.stream   = 流式方法名（默认 optimizeStream）
 *  opts.fallback = 降级用的整包方法名（默认 optimize）
 *  opts.busy     = 阶段 → 按钮文案，例如 {thinking: "思考中…", writing: "撰写中…"}
 *
 *  降级路径：老后端没有这条流式接口 / 环境不支持 ReadableStream 时，
 *  **静默退回整包**（没有进度，但功能不丢）—— 不能让"多了一条进度条"变成
 *  "老版本直接不能用了"。 */
async function optCallStream(body, title, ui, opts) {
    opts = opts || {};
    const streamFn = opts.stream || "optimizeStream";
    const fallbackFn = opts.fallback || "optimize";
    const busyText = opts.busy || { thinking: "思考中…", writing: "撰写中…", expand: "扩写中…" };
    const prog = optProgressStart(title);
    const ac = new AbortController();
    prog.onCancel(() => ac.abort());
    if (ui && ui.btn) {
        prog.onPhase((p) => {
            if (ui.btn && ui.btn.disabled && busyText[p]) ui.btn.textContent = busyText[p];
        });
    }
    try {
        if (typeof window.H3Api?.[streamFn] !== "function") throw new Error("__no_stream__");
        const r = await window.H3Api[streamFn](body, (evt) => prog.update(evt), { signal: ac.signal });
        if (r.body?.ok) { prog.done(); return r; }
        prog.close();
        return r;
    } catch (e) {
        prog.close();
        if (prog.cancelled() || (e && e.name === "AbortError")) {
            return { status: 200, body: { ok: false, cancelled: true, message: "已取消" } };
        }
        const msg = String((e && e.message) || e);
        if (msg === "__no_stream__" || /不支持流式|ReadableStream/.test(msg)) {
            return await window.H3Api[fallbackFn](body);
        }
        throw e;
    }
}

async function runOptForSegment(node, idx, ta, ui, srcTa) {
    if (_optBusy && _optBusy.node === node && _optBusy.idx === idx) return;
    /* srcTa = 优化源（外部给的待优化文本）；缺省即用 ta（段卡提示词框自身）。
     * 结果只写回 ta，源保持不动，方便换参数反复重优化。 */
    const src = srcTa || ta;
    const before = src ? src.value : "";
    if (!before || !before.trim()) { alert("提示词为空，无需优化"); return; }
    let settings;
    try { settings = optGetSettings(node); }
    catch (e) { alert(`加载优化配置失败：${e.message}`); return; }
    if (await optNeedsSetup(node, settings)) return;
    _optBusy = { node, idx };
    if (ui.btn) { ui.btn.textContent = "优化中…"; ui.btn.disabled = true; }
    const clearBusy = () => {
        if (ui.btn) { ui.btn.textContent = "✨ 优化"; ui.btn.disabled = false; }
        if (_optBusy && _optBusy.node === node && _optBusy.idx === idx) _optBusy = null;
    };
    try {
        const ds = getDs(node);
        const seg = (ds.segments || [])[idx] || {};
        /* 段级留空 = 跟随节点「每段时长」，不是 5 秒（见 segmentSeconds） */
        const secs = segmentSeconds(node, seg);
        /* 图 = 首尾帧锚 + 本段参考素材（collectSegMedia 统一编号并给出角色说明） */
        const mm = settings.read_media !== false
            ? await collectSegMedia(node, ds, idx) : { media: [], note: "", markMap: {} };
        await optFetchRuleFiles();
        /* 发给 LLM 之前：正文里的 `@素材全名` 换成 `@标注`（图片1…）。
         * 图是按标注报给模型的，正文里却写真名会让模型两边对不上。 */
        const outText = toLLMText(before, ds.ref_assets || []);
        const body = {
            /* 角色说明只进请求、不落库：优化器会像规则文件一样把它消费掉，
             * 原稿（before）与写回的 prompt 都不受影响。 */
            prompt: mm.note ? `${mm.note}\n${outText}` : outText,
            task: optTaskForMode(ds, idx), duration: secs,
            media: mm.media, context: { main_mode: optTaskForMode(ds, idx) }, config: settings,
        };
        if (!window.H3Api?.optimize) throw new Error("h3_api.js 未更新（缺 optimize）");
        /* 走 SSE 流式（有进度条 + 可取消）。老后端/老浏览器自动退回整包。 */
        const r = await optCallStream(body, `第 ${idx + 1} 段 · 提示词优化`, ui);
        if (r.body?.cancelled) return;      // 用户主动取消：不弹错，静默收场
        if (!r.body?.ok) throw new Error(window.H3Api.errText(r, "优化失败"));
        /* LLM 返回的是 `@图片1` —— 译回 `@女主.png` 再落库（译不出的原样保留，
         * 正文里会显示成红框，不静默丢）。 */
        const result = fromLLMText(String(r.body.prompt || "").trim() || before,
            ds.ref_assets || []);
        const key = optKey(node, idx);
        _optBefore.set(key, before);
        _optShown.set(key, "after");
        setPromptText(node, idx, result);
        /* 对齐指令**不写进正文**：它是编译产物（锚定栏展示 + 实跑时由后端注入）。
         * LLM 有时会自己抄一句 "Picture 1 aligns with…" 进产出，这里必须剥掉 ——
         * 留着会跟实跑注入的那句重复，锚一变还变成指向不存在图片的陈旧指令。
         * 剥完再同步屏幕上的编辑器，否则用户一动键盘旧文本又回写。 */
        stripAlignmentLines(node, idx);
        const finalText = String((getDs(node).prompts || [])[idx] ?? result);
        if (ta) ta.value = finalText;
        /* 原稿/优化稿切换存的是**最终文本**（已剥掉对齐指令的干净正文）。 */
        _optAfter.set(key, finalText);
        optPersist(node, idx, "after");
        if (ui.reset) { ui.reset.style.display = ""; ui.reset.textContent = "原稿"; }
        if (ui.name) ui.name.textContent = settings.mode === "local"
            ? `本地: ${String(settings.local_model || "").split(/[\\/]/).pop() || "未选"}`
            : (settings.model || "").split("/").pop() || "API";
        /* 悬空引用告警：<Picture N> 的编号超过实际送进模型的图片数。
         * 编号是**按挂载顺序**分配的 —— collectSegMedia 先把首/尾帧排在最前，
         * 再接 seg.refs，所以"挂了几张"和"产出里写了几号"可能对不上：
         * 挂了 2 张却写 <Picture 3>、或事后调过挂载顺序，引用都会指向不存在的图。
         * 只查 Picture：collectSegMedia 只传 kind==="image"，Video/Audio 不在其中。 */
        const picIdx = [...String(finalText).matchAll(/<Picture\s+(\d+)>/g)].map((m) => Number(m[1]));
        const maxPic = picIdx.length ? Math.max(...picIdx) : 0;
        const nMedia = (mm.media || []).length;
        if (maxPic > nMedia) {
            setLed("warn", `已回填，但正文引用了 <Picture ${maxPic}>，而本段只有 ${nMedia} 张图`
                + "送进模型 —— 超出的编号是悬空的，生成时不会有图。"
                + "请到「引用素材」补挂对应图片，或删掉这些引用。");
        } else {
            setLed("done", "优化已回填提示词框＋结构化结构（对齐指令已按首尾帧锚补回）");
        }
        scheduleRefresh(200);
    } catch (e) {
        alert(`提示词优化失败：${e.message || e}`);
    } finally { clearBusy(); }
}

/** 「AI扩写+优化」：把框里的内容当意图，一步出成品（**不弹窗**）。
 *
 *  三框合一后没有"剧本框"这个中间层了，所以扩写与优化在后端串成一次请求
 *  （/h3chain/expand_optimize），中间剧本不落库，只在响应里带回一份便于排查。
 *  参数全部来自「⚙ AI 优化设置 → AI 扩写优化设置」，点按钮前不需要再填什么。
 *
 *  误点保护：写入前把当前正文存为原稿，工具条上的「原稿」按钮可一键还原
 *  （不做内容形态自动判定 —— 判定会误判，宁可让用户自己点还原）。 */
async function runExpandOptimizeForSegment(node, idx, ta, ui) {
    if (_optBusy && _optBusy.node === node && _optBusy.idx === idx) return;
    const before = ta ? ta.value : "";
    if (!before || !before.trim()) { alert("提示词为空：写一句这段要拍什么，再点扩写"); return; }
    let settings;
    try { settings = optGetSettings(node); }
    catch (e) { alert(`加载优化配置失败：${e.message}`); return; }
    if (await optNeedsSetup(node, settings)) return;
    const ex = optGetExpandSettings(node);
    _optBusy = { node, idx };
    if (ui.btn) { ui.btn.textContent = "扩写中…"; ui.btn.disabled = true; }
    const clearBusy = () => {
        if (ui.btn) { ui.btn.textContent = "✨ AI扩写+优化"; ui.btn.disabled = false; }
        if (_optBusy && _optBusy.node === node && _optBusy.idx === idx) _optBusy = null;
    };
    try {
        const ds = getDs(node);
        const seg = (ds.segments || [])[idx] || {};
        /* 段级留空 = 跟随节点「每段时长」（见 segmentSeconds） */
        const secs = segmentSeconds(node, seg);
        const mm = settings.read_media !== false
            ? await collectSegMedia(node, ds, idx) : { media: [], note: "", markMap: {} };
        await optFetchRuleFiles();
        const outText = toLLMText(before, ds.ref_assets || []);
        if (!window.H3Api?.expandOptimize) throw new Error("h3_api.js 未更新（缺 expandOptimize）");
        /* 走 SSE 流式（两次 LLM 调用各 20~40 秒，没有进度就是"点完没反应"）。
         * 老后端自动退回整包。 */
        const r = await optCallStream({
            config: settings,
            prompt: mm.note ? `${mm.note}\n${outText}` : outText,
            style: ex.style,
            /* 单段扩写（segment_count:1）：时长**锁死**为段时长，不留给模型自选。
             * 给 4–15 的范围它会写出 12s 的分镜，而本段实际只生成 5s —— Shot 时间戳
             * 直接超界（validate E_SHOT_OVERFLOW），出片被截断，还会让模型以为
             * 时长没定、于是"自由发挥"。duration 与范围必须是同一个数。 */
            seconds_min: secs,
            seconds_max: secs,
            segment_count: 1,
            media: mm.media,
            style_note: mm.note || "",
            task: optTaskForMode(ds, idx),
            duration: secs,
            context: { main_mode: optTaskForMode(ds, idx) },
        }, `第 ${idx + 1} 段 · 扩写 + 优化`, ui, {
            stream: "expandOptimizeStream", fallback: "expandOptimize",
            busy: { expand: "扩写中…", thinking: "优化中…", writing: "优化中…" },
        });
        if (r.body?.cancelled) return;      // 用户主动取消：不弹错
        if (!r.body?.ok) throw new Error(window.H3Api.errText(r, "扩写失败"));
        const result = fromLLMText(String(r.body.prompt || "").trim() || before,
            ds.ref_assets || []);
        const key = optKey(node, idx);
        _optBefore.set(key, before);          // 原稿：误点了可以原样还原
        _optShown.set(key, "after");
        setPromptText(node, idx, result);
        stripAlignmentLines(node, idx);       // 同上：对齐指令不进正文
        const finalText = String((getDs(node).prompts || [])[idx] ?? result);
        if (ta) ta.value = finalText;
        _optAfter.set(key, finalText);
        optPersist(node, idx, "after");
        if (ui.reset) { ui.reset.style.display = ""; ui.reset.textContent = "原稿"; }
        setLed("done", "扩写+优化已回填（点「原稿」可还原改写前的内容）");
        scheduleRefresh(200);
    } catch (e) {
        alert(`扩写+优化失败：${e.message || e}`);
    } finally { clearBusy(); }
}

function optToggle(node, idx, ta, ui) {
    const key = optKey(node, idx);
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

/* ---- 思考强度：能力表 ----------------------------------------------------
 * **单一真相源在后端**（optimizer.py 的 GLM_FORCE_THINKING / GLM_EFFORT_PREFIXES），
 * 前端只做前缀匹配，不另写一份名单 —— 两边各写一份，改一处忘一处必然漂移，
 * 症状是"界面说能关、请求却被服务商 400 拒绝"。
 * 拿不到就退化成空表：界面只给「关闭 / 开启」，不猜、不假装支持强度分级。 */
const _optCapsTbl = { force: [], effortPrefixes: [], effortValues: ["low", "high", "max"] };

function optCapsLoad(body) {
    if (!body || typeof body !== "object") return;
    if (Array.isArray(body.glm_force_thinking)) {
        _optCapsTbl.force = body.glm_force_thinking.map((s) => String(s).toLowerCase());
    }
    if (Array.isArray(body.glm_effort_prefixes)) {
        _optCapsTbl.effortPrefixes = body.glm_effort_prefixes.map((s) => String(s).toLowerCase());
    }
    if (Array.isArray(body.glm_effort_values) && body.glm_effort_values.length) {
        _optCapsTbl.effortValues = body.glm_effort_values.map((s) => String(s).toLowerCase());
    }
}

/** `thinking` / `reasoning_effort` 是智谱私有字段，别家端点收到只会 400。 */
function optIsGlm(url) {
    return String(url || "").toLowerCase().indexOf("bigmodel") >= 0;
}

/* 端点属于哪家（与后端 optimizer._thinking_family 同口径，改一处要改两处）。
 * 只认三家：glm / deepseek / gpt；其余一律 unknown —— **不干预思考**，
 * 由服务商默认决定（思考型模型多半是开的，这点必须在界面上说清楚，
 * 否则用户会以为"没选项 = 关了"）。 */
function optThinkingFamily(url, model) {
    const u = String(url || "").toLowerCase();
    const m = String(model || "").toLowerCase();
    if (u.indexOf("bigmodel") >= 0) return "glm";
    if (u.indexOf("deepseek") >= 0 || m.startsWith("deepseek")) return "deepseek";
    const base = m.split("/").pop();
    /* 与后端同口径：只认官方域名或明确的 GPT 型号名，不做宽松子串匹配
     * （中转地址里带 openai 字样但跑别家模型的情况很常见）。 */
    if (u.indexOf("openai.com") >= 0 || base.startsWith("gpt-")
        || base.startsWith("o1") || base.startsWith("o3") || base.startsWith("o4")
        || base.startsWith("chatgpt")) return "gpt";
    return "unknown";
}

/** 该型号的思考能力：能不能关、能不能调强度、选「关闭」实际会被翻译成什么。 */
function optModelCaps(model) {
    const m = String(model || "").toLowerCase();
    const forced = _optCapsTbl.force.some((p) => m.startsWith(p));
    const supports = _optCapsTbl.effortPrefixes.some((p) => m.startsWith(p));
    return {
        forced,
        supports,
        values: supports ? _optCapsTbl.effortValues.slice() : [],
        /* 与后端 _glm_caps().disabled_effect 同口径：强制思考且支持强度 → low；
         * 能真正关掉 → disabled；强制思考又不支持强度 → 空（后端什么都不发）。 */
        disabledEffect: forced ? (supports ? "low" : "") : "disabled",
    };
}

const OPT_EFFORT_LABEL = { low: "低强度（快）", high: "高强度", max: "最高强度（最慢）" };

/* 服务商预设（与后端 optimizer.py PROVIDERS 对齐：url/model/protocol）
 * 顺序 = 下拉顺序，glm 排第一因为它是默认值。 */
const OPT_PROVIDERS = {
    glm: { label: "智谱 GLM（BigModel，默认）", url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-5.3-flashx", protocol: "openai" },
    runninghub: { label: "RunningHub 国内版", url: "https://www.runninghub.cn/openapi/v2", model: "openai/gpt-5.6-sol", protocol: "openai" },
    runninghub_overseas: { label: "RunningHub 海外版", url: "https://www.runninghub.ai/openapi/v2", model: "openai/gpt-5.6-sol", protocol: "openai" },
    openai: { label: "OpenAI", url: "https://api.openai.com/v1", model: "gpt-4.1-mini", protocol: "openai" },
    gemini: { label: "Google Gemini", url: "https://generativelanguage.googleapis.com/v1beta", model: "gemini-2.5-flash", protocol: "gemini" },
    openrouter: { label: "OpenRouter", url: "https://openrouter.ai/api/v1", model: "google/gemini-2.5-flash", protocol: "openai" },
    dashscope: { label: "阿里云百炼", url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-vl-max", protocol: "openai" },
    /* deepseek-flash（V4.1 Flash）是 DeepSeek 唯一支持图片输入的型号
     * （V4 Pro 不支持视觉），本插件要读参考图，所以默认只能是它。 */
    deepseek: { label: "DeepSeek（官方）", url: "https://api.deepseek.com", model: "deepseek-flash", protocol: "openai" },
    siliconflow: { label: "SiliconFlow", url: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen2.5-VL-72B-Instruct", protocol: "openai" },
    custom: { label: "自定义", url: "", model: "", protocol: "openai" },
};

/* ---- ⚡ 性能优化设置（**全局**、跨项目）----
 *
 * 真源在后端 perf.py：DEFAULT_PERF / PERF_TYPES / parse_state / apply_runtime。
 * 前端**不自己算档位**（既有的铁律：参数单一真源），只做两件事：
 *   ① 显示后端给的只读诊断（显存 / 内存 / swap / R_v / R_m / 档位 / 平台）
 *   ② 渲染开关，改完点「保存并应用」一次性 POST 回后端并在进程内应用
 *
 * ⛔ 这里原本还有第三件事：按后端名单把字段标成「接线中」（灰掉）或「跟随档位」。
 * 2026-09-23 连根删除：名单是**人工维护的**、必然说谎，而「未接线」那一态早已恒空。
 * 现在「这个键有没有用」由代码事实决定（有没有人读它，后端有 AST 测试守着），
 * 面板一律直接渲染；需要提示的地方用**字段属性 + 当前值**表达（见字段表的 `autoWhen`）。
 *
 * ═══ 分类轴 = 处理链阶段（2026-09-23 重构）═══
 *
 * 旧分类是「显存 / 内存 / 编码 / 运行时开关 / 素材库」——**按资源类型**分。
 * 这正是「同一件事散在两个组里、找不着」的根源：分块明明全在压显存，却因为
 * 「一采的、放大的、精化的」分散在三个地方，用户想找「我现在卡在哪一段」时
 * 无从下手。改成按**处理阶段**分，与用户脑子里的模型一致。
 *
 * 一级阶段：一采采样 → VAE 解码 → 放大网络 → 精化二采 → 成片与编码 → 通用与机器
 * 二级用途：分块 / 注意力 / 权重流动 / 自救 …
 *
 * 每条一级标题带 stage 元信息：
 *   stageWhen —— 生效时机徽章（立即 / 下次渲染）
 *   stageNote —— 一句话说清这一段在干什么（用户不必猜）
 *
 * 控件形态统一为「启用开关 + 参数」两件套（见 enable 字段）：
 *   分块类字段一律配一个独立开关，关掉时参数控件 disabled 但**不清零** ——
 *   以前三种形态混用（0=关的数值 / bool 勾选 / 下拉即开关），用户看不出
 *   「这个分块到底开没开」。
 */

/* 阶段元信息表：一级标题 -> { 说明, 生效时机 }。
 * 为什么不写在字段的 group 里：那时 6 个阶段会重复出现 30 次，改一处要改 30 行。 */
const H3_PERF_STAGES = {
    "一采采样": { when: "下次渲染", note: "主干扩散采样：UNET 权重流动 + FFN / 注意力的显存峰值" },
    "放大网络": { when: "下次渲染", note: "潜空间神经放大（T 不变，只放大 H/W）+ 网络的加载常驻" },
    "精化二采": { when: "下次渲染", note: "在高清 latent 上低步数重采样：细节增益与接缝的主要来源" },
    "成片与编码": { when: "混合", note: "全链帧合成与 mp4 编码；编码器改动**立即生效**" },
    "通用与机器": { when: "混合", note: "跨阶段：OOM 自救、运行时开关、素材库" },
};

const H3_PERF_FIELDS = [
    /* ═══ ① 一采采样 ═══ */
    { key: "ff_chunk_on", label: "FFN 分块（Chunk FeedForward）", kind: "bool", group: "一采采样 · 分块",
      hint: "按 token 切块算 MLP —— **主干 OOM 正崩在这里**（HyperFlow bypass 单次请求 6.13GB，占 24GB 卡的 26%）。"
          + "`nn.Linear` 沿 token 行可分离，所以**数学等价、零画质损失**，是本项目唯一无损的分块。装之前会先自测，不一致就不装" },
    { key: "ff_chunk_tokens", label: "　　每块 token 数", kind: "num", enable: "ff_chunk_on", group: "一采采样 · 分块",
      hint: "切成多大一块。**块数 = 序列 token ÷ 此值**，而开销 ∝ 块数 —— 每个 Linear 每块都要取一次权重、"
          + "同步一次流，块数一多就线性拖慢采样。所以**宁大勿小**：H3 常见序列 5–10 万 token，"
          + "填 4096 会切出 20 多块。显存代价 = 每块 token × 28672 × 2 字节（16384 ≈ 0.9GB）。"
          + "建议 16384 起，还 OOM 再减半；实际块数会在日志里打出来（`[H3分块] FFN 实际切块`）。"
          + "⚠ 单位是「每块的 token 数」，与 KJNodes 的「切成几份」不是同一口径" },
    { key: "ff_chunk_min_tokens", label: "　　低于多少 token 不切", kind: "num", enable: "ff_chunk_on", group: "一采采样 · 分块",
      hint: "短序列切块只增加开销、省不了多少。序列 token 数低于此值就整段直通（对齐 KJNodes 的 seq_threshold，其默认 4096）" },
    { key: "attn_head_on", label: "注意力头分块（Low VRAM Attention）", kind: "bool", group: "一采采样 · 注意力",
      hint: "把注意力按 head 分组逐组算。**head 之间独立 → 分组本身精确无损**；装之前会自测（拿小输入跑"
          + "「分组 vs 整段」对比），不一致就不装。收益**完全取决于后端内核**："
          + "**int8 kitchen 内核下实测有效** —— S=8192 时省 25.7% 的 kernel 临时量、耗时与整段持平"
          + "（5619ms vs 5465ms），8 组即平台期。默认 sdpa / flash 不物化注意力矩阵，理论收益小得多"
          + "（**未实测**），此时主要是多 n 次 kernel 调用的开销。"
          + "⚠ 要拿 kitchen 收益必须**启动加 `--use-ck-attention` 并重启** —— 面板的「Attention 后端」只在"
          + "已加载的后端之间切换，切不到它" },
    { key: "attn_head_chunks", label: "　　切成几组头", kind: "num", enable: "attn_head_on", group: "一采采样 · 注意力",
      hint: "把注意力按 head 分组逐组算：kernel 内部的临时量（int8 q/k 副本、fp32 累加器）按组数缩小，"
          + "融合 qkv buffer 也被拆成小组随用随放 —— **省的就是这两处**。1=关，上限 = 头数。"
          + "⚠ 下面这组实测的前提是 **int8 kitchen 内核**：8 组省 25.7%、耗时与整段持平，14 组只多省 1.5 个点"
          + "（S=8192）→ 8 是平台期起点。⚠ 默认 sdpa 后端下这两处收益**未实测**，见上面总开关的说明。"
          + "数学等价（head 之间独立），装前自测不一致就不装。移植自 KJNodes MiniMaxLowVRAMAttention" },
    { key: "attn_backend", label: "Attention 后端", kind: "sel", group: "一采采样 · 注意力",
      opts: [["auto", "跟随 ComfyUI（推荐）"], ["sdpa", "sdpa"], ["sage", "SageAttention"], ["flash", "FlashAttention"]],
      hint: "auto = 不动。Sage 在 30 系上有失败报告；环境里拿不到该后端时会保持现状而不是置空" },
    { key: "blocks_swap_on", label: "块交换（Blockswap）", kind: "bool", group: "一采采样 · 权重流动",
      hint: "**显存装不下时的活路**：让 block 权重在 GPU / CPU 之间流动起来。"
          + "H3 = 50 个 double block，每块 ≈ 0.4GB —— 6GB 卡只放得下约 13 块，靠它才能跑起来。"
          + "**它换的是显存、代价是时间**（每块每次前向都要来回搬一趟），装得下（≥24GB 卡）就别开。"
          + "⚠ 本项接的是 **ComfyUI 官方**的块级流动（DynamicVRAM 的 vbar 换入 + 官方 block 循环里的预取队列），"
          + "插件**不自己搬权重**：手工搬会与官方按需换入抢同一批参数、并打坏 LoRA 的权重账。"
          + "开关的作用是把下面「交换块数」折算成**显存预留**交给 aimdo —— 抬高预留 = 逼官方把更多块换出去" },
    { key: "blocks_to_swap", label: "　　交换块数（0–50）", kind: "num", enable: "blocks_swap_on",
      autoWhen: -1, group: "一采采样 · 权重流动",
      hint: "放出多少块到 CPU，等价于「多留出 N × 每块大小 的显存」。参考：16GB→19–25 块；12GB→31–38；8GB→44–50。"
          + "-1 = 跟随档位（按本机**真实 UNET 体量**反解，只有渲染开始拿到模型时才算得准）。"
          + "⚠ 会被**夹取**：预留不可能超过显存总量，超过「显存一半」的部分会被夹掉并在报告行里说明" },
    { key: "blocks_prefetch", label: "　　块级预取", kind: "bool", group: "一采采样 · 权重流动",
      hint: "提前把下一块搬上来，用搬运的空闲时间盖住一部分开销（**本项独立生效，不受总开关影响**"
          + " —— 官方那套块级流动不管总开关开不开都在跑）。"
          + "⚠ 只能是开关：官方预取队列的深度写死为「提前 1 块」，**没有「预取 N 块」这个旋钮**。"
          + "关掉 = 少占一块显存、更慢；关的是预取（lookahead），不是块级流动本身" },

    /* ═══ ② 放大网络 ═══ */
    { key: "upscale_temporal_chunk", label: "放大网络 3D 时序分块", kind: "bool", group: "放大网络 · 分块",
      hint: "长段按时序切块前向：3D 卷积的激活随 T 线性增长，长段一次性前向容易爆；且末端帧缺右侧上下文会闪。"
          + "切块后每块左右各带 overlap 帧上下文、只取中间有效区、块间线性融合。**关闭会改变输出口径 → 进指纹、触发既有高清段重做**" },
    { key: "upscale_chunk_frames", label: "　　每块帧数", kind: "num", enable: "upscale_temporal_chunk", group: "放大网络 · 分块",
      hint: "每块多少帧（默认 32）。越小越省显存，但重叠部分的重复计算占比越高（实跑 T=107 分 4 块时约 +37% 计算量）。"
          + "⚠ 调小会**真的丢上下文**吗？不会 —— overlap 是独立参数、不受此值影响" },
    { key: "upscale_overlap", label: "　　块间重叠帧（只增不减）", kind: "num", enable: "upscale_temporal_chunk", group: "放大网络 · 分块",
      hint: "**它等于时序卷积核宽度，是「跨块上下文完整性」的保证** —— 小于核宽就会真的丢信息。"
          + "所以本项目只允许往大调，调小无效（会被夹回核宽）。默认自动取核宽（实跑 5）。调大更保险、代价是重复计算" },
    { key: "keep_upscaler_resident", label: "放大网络段间保留（不强制卸载）", kind: "bool", group: "放大网络 · 常驻",
      hint: "**默认开，延续现状口径**：段间把放大网络留在缓存里，下段零加载。"
          + "关掉 = 每段二采收尾**强制卸载**（把网络从缓存里删掉 + soft_empty_cache，下段重新从磁盘加载 ~1s），"
          + "换来的是 CPU 侧那 ~659MB 权重副本 —— 多段链「后段比首段更易 OOM」时才需要关" },
    /* ═══ ④ 精化二采 ═══ */
    { key: "refine_temporal_on", label: "精化时序分块", kind: "bool", group: "精化二采 · 分块",
      hint: "把高清 latent 沿时间切段，**每段独立跑 N 步去噪**。⚠ 与放大网络那个时序分块有本质区别："
          + "那个是纯前馈（一次 forward，切开算再融合即可）；这个是**扩散采样循环**，段与段之间没有注意力交互 → "
          + "接缝两侧各自收敛到不同局部解 → **接缝逐帧闪烁**（不是一条静止的缝）。必须配合接缝医生" },
    { key: "refine_temporal_chunk", label: "　　每段帧数", kind: "num", enable: "refine_temporal_on", group: "精化二采 · 分块",
      hint: "0=关。每段多少帧。越小越省显存，接缝也越多。建议先给 16–24（一段 5 秒 ≈ 120 帧 → 5–8 段）" },
    { key: "refine_temporal_overlap", label: "　　段间重叠（latent token）", kind: "num", enable: "refine_temporal_on", group: "精化二采 · 分块",
      hint: "**单位是 latent token，不是像素**（与下面空间那个 overlap 不是一个量纲）。至少 8，不够会明显闪烁。"
          + "视频模型的 latent 时间压缩比通常为 4 或 8，所以 8 个 latent token 约等于 32–64 帧" },
    { key: "refine_tile_on", label: "精化空间分块（tile）", kind: "bool", group: "精化二采 · 分块",
      hint: "本阶段总开关。**关掉时下面的切块档位与全部 tile 参数灰掉但保留数值**，下次开还按原值跑。"
          + "把高清画布按 H/W 切成网格、逐块独立去噪 —— **这是全清单里画质风险最高的一项**："
          + "H3 靠全局注意力维持整幅画面的色调 / 光照 / 构图一致，切开后每块只看得见自己那块 → 块边界出现竖缝 + 色调不一致。"
          + "视频比图像严重得多（块边会在**帧间抖动**，因为每块独立去噪、噪声轨迹不同）。"
          + "5 秒 1080p 精化画布的激活是这段链条里最大的一笔，只有逼到墙角才建议开" },
    { key: "refine_tile", label: "　　切块档位", kind: "sel", enable: "refine_tile_on", group: "精化二采 · 分块",
      opts: [["off", "关（0 块，等同不开）"], ["2x2", "2×2（4 块）"], ["3x3", "3×3（9 块）"],
             ["4x4", "4×4（16 块，风险最高）"], ["2x1", "横向 2 条（2 块）"], ["1x2", "竖向 2 条（2 块）"]],
      hint: "切成几块（总开关决定「做不做」，这里决定「切多细」）。"
          + "⚠ 档位越大越省显存但越容易崩：**2×2 是每块仍有 1/4 画幅的极限**，3×3 起全局构图基本断裂。" },
    { key: "refine_tile_overlap", label: "　　块间重叠（像素）", kind: "num", enable: "refine_tile_on", group: "精化二采 · 分块",
      hint: "缓解竖缝的第一手段。建议 32–64；太小缝明显，太大会吃掉省下的显存收益。注意单位是**像素**" },
    { key: "refine_tile_feather", label: "　　接缝羽化宽度（像素）", kind: "num", enable: "refine_tile_on", group: "精化二采 · 分块",
      hint: "块间融合的渐变带宽度（线性 ramp）。**只靠 overlap 会有硬边**，羽化把过渡抹开。建议取 overlap 的一半到等宽" },
    { key: "vram_shuffle", label: "精化前腾挪强度", kind: "sel", group: "精化二采 · 腾挪",
      opts: [["auto", "跟随档位（小显存=全卸）"], ["off", "不腾挪"], ["soft", "只卸放大网络"], ["full", "全卸驻留模型"]],
      hint: "精化前把显存腾出来。24GB 卡上全卸疑似净亏（RSS 4.98→36.96GB），但 32GB 卡上有过 OOM 实测才加的它 —— 换卡前只做 A/B" },

    /* ═══ ⑤ 成片与编码 ═══ */
    { key: "final_mode", label: "成片合成方式", kind: "sel", group: "成片与编码 · 合成",
      opts: [["auto", "能拼就拼（推荐）"], ["stream", "强制流式拼接"], ["memory", "强制内存帧编码"]],
      hint: "stream = 用分段 mp4 流式拼接，**全程不碰全链内存帧**（NLE 的一贯做法）；auto 在分段齐全时自动走 stream" },
    { key: "frames_dtype", label: "全链帧存储精度", kind: "sel", group: "成片与编码 · 合成",
      opts: [["float32", "float32（现状）"], ["uint8", "uint8（内存 ×¼）"]],
      hint: "uint8：帧存内存降到 ¼，且成片改为预分配逐段填充 —— 峰值从 2× 全链帧降到约 1.25×。输出的 IMAGE 仍是 float32。**内存紧就开**" },
    { key: "guard_action", label: "落盘守卫行为", kind: "sel", group: "成片与编码 · 合成",
      opts: [["warn", "只报告"], ["block", "critical 时禁止全卸"]],
      hint: "block：判定会挤到 swap 时跳过二采精化前的全卸 —— Linux 无 swap 机器上全卸不是变慢，是进程被 OOM killer 直接杀掉" },
    { key: "encoder", label: "编码器实现", kind: "sel", group: "成片与编码 · 编码",
      opts: [["libx264", "libx264（CPU）"], ["h264_nvenc", "H.264 NVENC（GPU）"], ["hevc_nvenc", "H.265 NVENC（GPU）"]],
      hint: "**用哪个编码器实现**，与下面「画质档位」正交、可任意组合。"
          + "**libx264（CPU）**：压缩效率更高（同码率画质更好），代价是**长片时把 CPU 吃满**"
          + "（起 核数×1.5 个线程，跑链时整机卡）。"
          + "**NVENC（GPU）**：显卡专用硬件，几乎不占 CPU、长片编码更快；"
          + "代价是**同画质需要更高码率**（压缩效率略低于 x264，用下面 cq 调低补偿）。"
          + "两者**都不省显存**（帧最终仍要落 CPU 侧进编码器）。" },
    { key: "x264_crf", label: "libx264 质量档 crf", kind: "num", group: "成片与编码 · 编码",
      showWhen: { key: "encoder", values: ["libx264"] },
      hint: "x264 恒定质量（**越小越清晰**，常用 13–23；默认 20）。"
          + "与下面「高清编码档」**正交**：那个开关管 preset + 抗条纹抖动，crf 在这里单独调 —— "
          + "想要更高画质直接把这里压到 16 / 13（不必再找已取消的「极致」档）。"
          + "本值就是 x264 唯一的质量真源，改完立即对后续编码生效" },
    { key: "nvenc_cq", label: "NVENC 质量档 cq", kind: "num", group: "成片与编码 · 编码",
      showWhen: { key: "encoder", values: ["h264_nvenc", "hevc_nvenc"] },
      hint: "NVENC 恒定质量（**越小越清晰**，语义对应 x264 的 crf）。"
          + "**NVENC 不认 crf**，两者必须分开传，否则 NVENC 会静默丢掉质量档。"
          + "因 NVENC 压缩效率略低，同画质可比 crf 再调低 2–4。" },
    { key: "encode_hq", label: "高清编码档（preset + 抗条纹）", kind: "bool", group: "成片与编码 · 编码",
      hint: "**开**：medium preset（veryfast 的率失真差约 5–15%，是编码层的二次模糊）"
          + "+ aq-mode 3（暗部自适应量化，保暗场细节）+ Bayer 抖动（打散 8bit 量化台阶，"
          + "消解渐变色带 / 天空横向条纹）。**关**（默认）= 现状 veryfast 无抖动。"
          + "⚠ 与 crf / cq **正交** —— 本开关只管 preset + 抖动，质量数值在上面单独调，"
          + "两边互不覆盖。开了会进二采指纹（换了档 → 既有高清段判失效重做）" },

    /* ═══ ⑥ 通用与机器 ═══ */
    { key: "oom_autoretry", label: "主干采样 OOM 自救", kind: "bool", group: "通用与机器 · 自救",
      hint: "跨阶段。主干采样撞 OOM 时自动卸载驻留模型 + 回收残留后**原参**重试一次（参数与产物都不降级）。"
          + "救不回来会给可行动的中文报错。ComfyUI 自己那次自救救的是权重，救不到 LoRA 的激活" },
    { key: "act_peak_probe", label: "量 LoRA 激活峰值", kind: "bool", group: "通用与机器 · 自救",
      hint: "首段采样时采设备级显存峰值 → 打出「LoRA 账：权重 +X GB（N patches）· 实测单步激活峰值 Y GB · 建议留空 ≥Z GB」。"
          + "权重那半 ComfyUI 自己算得到，激活那半只有实测才看得见" },
    { key: "upcast_attention", label: "Upcast Attention", kind: "tri", group: "通用与机器 · 运行时",
      hint: "attention 强制走 fp32：更稳但更吃显存、更慢。**小显存卡通常关**；auto = 跟随启动参数。改动立即生效" },
    { key: "index_mode", label: "素材库索引失效口径", kind: "sel", group: "通用与机器 · 素材库",
      opts: [["fingerprint", "目录指纹（推荐）"], ["ttl", "固定 3 秒过期"]],
      hint: "指纹：没增删文件就一直复用，翻页不再重建索引（1200 条目实测省掉 1895ms）；ttl：老行为，遇到「新素材不显示」可退回" },
    { key: "thumb_on_import", label: "入库即生成缩略图", kind: "bool", group: "通用与机器 · 素材库",
      hint: "关掉则退回「首次浏览时才生成」—— 会为每张大图付一次全解码峰值（6000×4000 JPEG 实测 193 MB，且 RSS 涨上去不易回落）" },
    { key: "thumb_max_mp", label: "缩略图源图上限（百万像素）", kind: "num", group: "通用与机器 · 素材库",
      hint: "超过就跳过解码，前端回落类型图标 —— 挡住超大图的解码尖峰" },
];

/* 性能优化弹窗（顶栏「⚡ 性能优化」入口）。
 *
 * 为什么是弹窗：性能项有 20 条、每条都带一段解释，右栏的宽度装不下
 * 「控件 + 说明」，两者挤一行等于把说明吞掉 —— 这正是用户报的「信息展现不全」。
 * 弹窗里一律两行式：第一行 label + 控件，第二行说明全文换行。
 *
 * 保存语义：改动先进 draft，点「保存并应用」一次性 POST（原实现是勾一下发一次
 * 请求，误触无法收回）。读不到配置时保存按钮直接禁用 —— 列一排勾选却存不下去，
 * 比直说读不到更糟。 */
async function openPerfSettings() {
    if (document.querySelector(".h3d-perf-overlay")) return;
    const A = window.H3Api;
    const overlay = el("div", "h3d-opt-overlay h3d-perf-overlay");
    const dialog = el("div", "h3d-opt-dialog h3d-perf-dialog");
    overlay.append(dialog);
    dialog.append(el("div", "h3d-opt-title", "⚡ 性能优化 · 功能设置"));
    dialog.append(el("div", "h3d-opt-sub",
        "机器级设置（这台卡多大、内存多少）——<b>全局生效、跨项目共用</b>，"
        + "存 &lt;user&gt;/minimax_h3/perf.json。改完点「保存并应用」："
        + "Upcast / 编码器 / 素材库那几项立即生效，显存·内存那几项在下一次渲染生效。"));
    /* 正文（可滚）与底栏（固定）分开：底栏在滚动区**外**，改完随手就能点保存，
     * 不用先滚到底把按钮找出来。回执行也跟着底栏走，保存完立刻可见。 */
    const body = el("div", "h3d-perf-body");
    const foot = el("div", "h3d-perf-foot");
    dialog.append(body, foot);
    const status = el("div", "h3d-perf-status", "");
    const actions = el("div", "h3d-opt-actions");
    const cancel = el("button", "h3d-btn", "关闭");
    const save = el("button", "h3d-btn h3d-opt-save", "保存并应用");
    save.disabled = true;
    actions.append(cancel, save);
    foot.append(status, actions);
    const close = () => overlay.remove();
    cancel.onclick = close;
    overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) close(); });
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    document.body.append(overlay);

    const fail = (msg) => {
        body.replaceChildren(el("div", "h3d-perf-hint", msg));
        save.disabled = true;
    };
    if (!A || !A.perfGet) { fail("性能设置接口不可用（请更新插件）"); return; }
    body.append(el("div", "h3d-perf-hint", "读取中…"));
    let info;
    try { info = (await A.perfGet()).body; }
    catch (e) { fail("读取性能设置失败：" + ((e && e.message) || e)); return; }
    if (!info || !info.ok) { fail("读取性能设置失败"); return; }

    const draft = Object.assign({}, info.data || {});
    /* 没有「判态」这一步了（三态名单已于 2026-09-23 连根删除，见文件头注释）。 */
    body.replaceChildren();

    /* 只读诊断：机器现状 + 当前真正生效的值（后端量，前端不猜） */
    const diag = el("div", "h3d-perf-diag");
    diag.append(el("div", "h3d-setsec-title", "当前机器（只读）"));
    diag.append(el("pre", null,
        escapeHtml(String(info.report || "").replace(/^\[H3性能\]\s*/, "") || "未探明")));
    if (info.upcast) {
        diag.append(el("pre", null,
            "Upcast Attention 当前生效：" + (info.upcast.effective ? "开" : "关")
            + "（启动参数 force=" + (info.upcast.cli_force_upcast ? "1" : "0")
            + " / dont=" + (info.upcast.cli_dont_upcast ? "1" : "0") + "）"));
    }
    /* 块级权重流动的**现状**（后端探，前端不算）。机制在 ComfyUI 那边，所以「这台
     * 机器到底有没有在块级流动」必须如实摆出来 —— 有 DynamicVRAM 的机器上它本来
     * 就在跑，用户不需要勾任何开关；没 DynamicVRAM 的机器则要靠 ComfyUI 自己的按
     * 模块流动。两者都不是本插件提供的，不能让人以为是。 */
    const bsAp = (info.applied || {}).blockswap;
    if (bsAp) {
        const _bsGb = (v) => (v == null ? "?" : Number(v).toFixed(2) + "GB");
        diag.append(el("pre", null,
            (bsAp.path === "aimdo"
                ? "块级权重流动：DynamicVRAM 在线（官方块级预取 + vbar 换入），当前显存预留 "
                  + _bsGb(bsAp.headroom_gb)
                : "块级权重流动：本机无 DynamicVRAM → 装不下时由 ComfyUI 自己按模块流动（粒度≈block）")
            + (bsAp.note ? "\n" + bsAp.note : "")));
    }
    body.append(diag);

    /* 分类树：group 形如「显存 · 自救」→ 一级「显存」+ 二级「自救」；不含「 · 」的
     * （编码 / 运行时开关 / 素材库）本身就是一级。
     * 为什么两级都做成可折叠：19 条带说明的字段一屏放不下，能按大分类把无关的几组
     * 收起来（只想调素材库时把显存那 8 项合上），找东西就不必一路滚。
     * 折叠态交给 foldSection 统一记忆（_foldState）—— 弹窗每次打开都整块重建，
     * 不记忆的话收起的分组会当场弹回来，跟右栏那几栏一个道理。 */
    const tree = [];
    for (const f of H3_PERF_FIELDS) {
        const parts = String(f.group || "其他").split(" · ");
        let head = tree.find((g) => g.name === parts[0]);
        if (!head) { head = { name: parts[0], subs: [] }; tree.push(head); }
        const subName = parts[1] || "";
        let sub = head.subs.find((s) => s.name === subName);
        if (!sub) { sub = { name: subName, fields: [] }; head.subs.push(sub); }
        sub.fields.push(f);
    }
    for (const head of tree) {
        const total = head.subs.reduce((a, s) => a + s.fields.length, 0);
        const group = foldSection("perf-g-" + head.name, true,
            "<summary>" + escapeHtml(head.name) + "<small>" + total + " 项</small></summary>");
        group.classList.add("h3d-perf-group");
        body.append(group);
        for (const sub of head.subs) {
            let host = group;
            if (sub.name) {
                host = foldSection("perf-s-" + head.name + "-" + sub.name, true,
                    "<summary>" + escapeHtml(sub.name) + "</summary>");
                host.classList.add("h3d-perf-sub");
                group.append(host);
            }
            for (const f of sub.fields) {
                const row = el("div", "h3d-perf-row");
                const top = el("div", "h3d-perf-top");
                /* 「跟随档位」按**当前值**提示，不再依赖后端名单：字段表里标了
                 * `autoWhen` 的项，值等于那个哨兵（如 blocks_to_swap 的 -1）时在
                 * label 后加一句 —— 这样提示永远与用户眼前的值一致，不会过期。 */
                const lab = el("label", null, "");
                const syncFollow = () => {
                    const fl = f.autoWhen != null
                        && Number(draft[f.key]) === Number(f.autoWhen);
                    lab.textContent = f.label + (fl ? " · 跟随档位" : "");
                };
                let ctl;
                if (f.kind === "tri") {
                    ctl = document.createElement("select");
                    for (const [v, txt] of [["auto", "跟随启动参数"], ["true", "强制开"], ["false", "强制关"]]) {
                        ctl.append(new Option(txt, v));
                    }
                    ctl.value = draft[f.key] === true ? "true" : draft[f.key] === false ? "false" : "auto";
                } else if (f.kind === "sel") {
                    ctl = document.createElement("select");
                    for (const [v, txt] of (f.opts || [])) ctl.append(new Option(txt, v));
                    ctl.value = String(draft[f.key] ?? (f.opts && f.opts[0][0]) ?? "");
                } else if (f.kind === "bool") {
                    ctl = document.createElement("input");
                    ctl.type = "checkbox";
                    ctl.checked = !!draft[f.key];
                } else {
                    ctl = document.createElement("input");
                    ctl.type = "number";
                    ctl.value = String(draft[f.key] ?? 0);
                }
                /* 开关两件套（f.enable 绑定一个 bool 开关 key）：开关关掉时参数控件
                 * disabled 但**不清零** —— 用户只是临时关掉，数值还留着。
                 * 这正是「给每个阶段配一个开启按钮 + 开启多少」的落地方式：
                 * 一个阶段一个总开关，下面挂「开多大」的参数，层级一眼可见。
                 * 初始灰/亮在整棵树建完后统一收尾（见本函数末尾），因为字段表里
                 * 参数可能排在它的开关之前。 */
                if (f.kind === "tri") {
                    ctl.onchange = () => {
                        const v = ctl.value;
                        draft[f.key] = v === "true" ? true : v === "false" ? false : "auto";
                    };
                } else if (f.kind === "bool") {
                    ctl.onchange = () => {
                        draft[f.key] = ctl.checked;
                        /* 总开关一动，把它名下的参数控件一起亮 / 灰（两件套的正向联动）。
                         * 只认本字段表里 enable 指向它的那些 key —— 不猜、不误伤。 */
                        const owned = H3_PERF_FIELDS
                            .filter((x) => x.enable === f.key).map((x) => x.key);
                        for (const ownedKey of owned) {
                            const oc = document.querySelector(
                                '[data-h3perf-key="' + ownedKey + '"]');
                            if (oc) oc.disabled = !ctl.checked;
                        }
                    };
                } else if (f.kind === "num") {
                    ctl.onchange = () => {
                        draft[f.key] = Number(ctl.value) || 0;
                        syncFollow();     // 数值一改，「跟随档位」提示跟着变
                    };
                } else {
                    ctl.onchange = () => { draft[f.key] = ctl.value; };
                }
                /* 反向联动：开关一动，把它名下的参数一起亮 / 灰。用 data 属性登记，
                 * 让参数行在 render 时能反查宿主开关（见 gate 那段）。 */
                ctl.dataset.h3perfKey = f.key;
                /* 按值显隐：登记「本行归属哪个宿主字段、什么值下才显示」。
                 * 为什么用 data 属性而不是现在就判：字段表里 crf 排在 encoder 之前，
                 * 建行时宿主的 select 还没建出来 → querySelector 落空、初始态会判错。
                 * 统一在整棵树建完后由 applyShowWhen 收尾（与 enable 两件套同一个坑）。 */
                if (f.showWhen) {
                    row.dataset.h3perfShowKey = f.showWhen.key;
                    row.dataset.h3perfShowVals = (f.showWhen.values || []).join("\u0001");
                }
                syncFollow();
                top.append(lab, ctl);
                row.append(top);
                if (f.hint) {
                    /* 说明里的 **强调** 转真加粗：原实现当纯文本渲染，星号直接露在界面上 */
                    row.append(el("div", "h3d-perf-hint",
                        String(f.hint).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")));
                }
                host.append(row);
            }
        }
    }

    /* 两件套收尾（必须整棵树都插进 DOM 之后再做）：逐条 enable 记录，把「开关当前
     * 状态」应用到它名下的参数控件上。不能在做行时顺手查 —— 字段表里
     * attn_head_chunks 排在 attn_head_on **之前**，那时宿主开关还没建出来，
     * querySelector 会落空、参数行就永远是亮的。 */
    for (const f of H3_PERF_FIELDS) {
        if (!f.enable) continue;
        const gate = body.querySelector('[data-h3perf-key="' + f.enable + '"]');
        const child = body.querySelector('[data-h3perf-key="' + f.key + '"]');
        if (!gate || !child) continue;
        child.disabled = !gate.checked;
    }

    /* 按值显隐收尾（同 enable 两件套，必须整棵树建完后做）。
     * 场景：crf / cq 两个质量控件随 `encoder` 的当前值切换显示 —— 选 libx264 只露
     * crf、选 NVENC 只露 cq。**隐藏是 display 而非 disabled**：两个旋钮语义不同
     * （NVENC 不认 crf），留着灰控件只会让人以为「这两个都要填」。 */
    const applyShowWhen = () => {
        for (const row of body.querySelectorAll("[data-h3perf-show-key]")) {
            const gate = body.querySelector(
                '[data-h3perf-key="' + row.dataset.h3perfShowKey + '"]');
            const vals = String(row.dataset.h3perfShowVals || "").split("\u0001");
            const cur = gate ? String(gate.value) : "";
            row.style.display = vals.includes(cur) ? "" : "none";
        }
    };
    applyShowWhen();
    /* 宿主值一变就重算：只挂在本表内被 showWhen 引用的键上，不猜、不误伤 */
    for (const hostKey of new Set(H3_PERF_FIELDS.filter((x) => x.showWhen)
        .map((x) => x.showWhen.key))) {
        const gate = body.querySelector('[data-h3perf-key="' + hostKey + '"]');
        if (!gate) continue;
        const prev = gate.onchange;
        gate.onchange = (ev) => { if (prev) prev.call(gate, ev); applyShowWhen(); };
    }

    save.disabled = false;
    save.onclick = async () => {
        save.disabled = true;
        save.textContent = "保存中…";
        try {
            const b = (await A.perfSet(draft)).body;
            if (!b || !b.ok) throw new Error("后端未接受（详见 ComfyUI 控制台）");
            const ap = b.applied || {};
            const fmt = (v) => v === true ? "开" : v === false ? "关" : (v === "auto" ? "跟随" : String(v ?? "?"));
            status.textContent = "已应用 · upcast=" + fmt(ap.upcast_attention)
                + " · 编码器=" + fmt(ap.encoder)
                + " · 成片=" + fmt(ap.final_mode)
                + " · 帧精度=" + fmt(ap.frames_dtype)
                + " · OOM自救=" + fmt(ap.oom_autoretry)
                /* ⚠ 这里是**块交换自己的三种情形**（开 / 关 / 待渲染生效），与已删的
                 * 面板三态名单无关：面板打开或保存时拿不到 UNET 体量 → 算不出目标、
                 * 本次不写（见 perf.apply_blockswap 的守卫），此时说「关」是假话 ——
                 * 它其实是「等渲染时按真实体量生效」。 */
                + " · 块交换=" + (ap.blockswap
                    ? (ap.blockswap.applied
                        ? (ap.blockswap.blocks + "块/" + ap.blockswap.path)
                        : (ap.blockswap.on ? "待渲染生效" : "关"))
                    : "关")
                + "\n（其余项为下一次渲染生效）";
            save.textContent = "✓ 已应用";
            setTimeout(() => { save.textContent = "保存并应用"; save.disabled = false; }, 1200);
        } catch (e) {
            save.textContent = "保存并应用";
            save.disabled = false;
            alert("保存失败：" + ((e && e.message) || e));
        }
    };
}

/* 提示词优化设置面板（结构对齐参考项目，请求仍走自研 /h3chain 后端）。
 * Key 随导演台状态保存，分享前请清空。 */
async function openOptSettings(node, onSaved) {
    let current;
    try { current = optGetSettings(node); }
    catch (e) { current = optDefaultSettings(); }
    let hasDefaultKey = false;
    try {
        if (window.H3Api?.getOptimizerConfig) {
            const r = await window.H3Api.getOptimizerConfig();
            if (r.body?.ok) {
                current = Object.assign({}, current);
                if (Array.isArray(r.body.models)) current._models = r.body.models;
                if (Array.isArray(r.body.mmproj_models)) current._mmproj = r.body.mmproj_models;
                if (r.body.llm_env && typeof r.body.llm_env === "object") current._env = r.body.llm_env;
                hasDefaultKey = r.body.has_default_key === true;
                _optBackend.loaded = true;
                _optBackend.hasKey = hasDefaultKey;
                _optBackend.providers = r.body.providers || null;
                /* 思考能力表（型号名单）由后端下发，前端只做前缀匹配 */
                optCapsLoad(r.body);
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
    provider.value = OPT_PROVIDERS[current.provider] ? current.provider : "glm";

    /* 分服务商记忆 Key 与模型（切换服务商不丢已填值，随 ds.optimizer 持久化） */
    const apiKeys = { ...(current.api_keys || {}) };
    if (current.api_key && !apiKeys[current.provider || "glm"]) apiKeys[current.provider || "glm"] = current.api_key;
    const providerModels = { ...(current.provider_models || {}) };
    if (current.model && !providerModels[current.provider || "glm"]) providerModels[current.provider || "glm"] = current.model;

    const key = el("input", ""); key.type = "password"; key.value = apiKeys[provider.value] || "";
    /* 服务端内置了 Key 且当前就是默认服务商时，留空 = 用内置 Key。说清楚，
     * 否则用户会以为"必须填"而反复去找自己的 Key。 */
    const keyHint = () => {
        key.placeholder = (hasDefaultKey && provider.value === "glm")
            ? "已内置 Key，留空即用（也可在此覆盖）" : "sk-…";
    };
    keyHint();
    const url = el("input", ""); url.type = "text";
    const model = el("input", ""); model.type = "text";
    model.placeholder = "如 glm-5.3-flashx";
    const protocol = el("select", "");
    protocol.append(new Option("OpenAI 兼容", "openai"), new Option("OpenAI Responses", "responses"), new Option("Gemini", "gemini"));

    const localModel = el("select", "");
    const mmproj = el("select", "");
    const device = el("select", "");
    device.append(new Option("Auto", "auto"), new Option("GPU", "cuda"), new Option("CPU", "cpu"));
    device.value = current.local_device || "cuda";
    /* 上限 32768（旧值 8192 太紧）：**推理 token 也算在 max_tokens 里**，
     * 思考模型动辄先烧掉几千个推理 token，正文还没开始就被 finish_reason=length
     * 截断 → 用户看到的是"LLM 返回空文本"。上限卡在 8192 等于把这条路堵死。
     * 默认 8192（后端同值），够六字段长输出 + 一轮中等推理。 */
    const maxTokens = el("input", ""); maxTokens.type = "number";
    maxTokens.min = "512"; maxTokens.max = "32768"; maxTokens.step = "512";
    maxTokens.value = String(Math.max(512, Math.min(32768, Number(current.max_tokens) || 8192)));
    /* 单次 HTTP 读写超时。默认 300：带图 + 长规则 + 六字段长输出的改写，
     * 旧的 120 秒必然"读操作超时"。 */
    const timeout = el("input", ""); timeout.type = "number";
    timeout.min = "30"; timeout.max = "1800"; timeout.step = "30";
    timeout.value = String(Math.max(30, Math.min(1800, Number(current.timeout) || 300)));
    /* 思考强度：**一个下拉管两个后端字段**（thinking + reasoning_effort）。
     * 以前只有一个「深度思考」勾选框 —— 用户选了 glm-5.3-flash 这类分档模型
     * 却只能开/关，低/高/最高三档根本选不到（用户报的就是这个）。
     * 默认关：同一份改写从分钟级降到二三十秒，结构化改写并不需要长思考。
     * 选项**按型号能力重建**（见 rebuildThinking）：智谱按型号给强度档，本地模型给
     * 开/关两档（它没有 thinking 字段，关思考靠 Qwen 系 /no_think 软开关），
     * 其它服务商整行隐藏 —— 后端对它们根本不下发这两个字段，摆着只会让人以为调了有用。 */
    const thinking = el("select", "");
    /* 当前档位：强度档 > 开启 > 关闭。历史值 `auto`（= 服务商默认）按「开启」显示 ——
     * GLM 系默认就是开思考，显示成「关闭」才是骗人。 */
    const _lv0 = String(current.reasoning_effort || "").toLowerCase();
    let level = ["low", "high", "max"].includes(_lv0) ? _lv0
        : (String(current.thinking || "disabled") === "disabled" ? "disabled" : "enabled");
    /* 内部统一档位（off/low/medium/high/max）：GPT / DeepSeek 用这一套，
     * GLM 与本地继续用老的「开关 + 强度」两字段（后端会自己推导）。 */
    let tkLevel = ["off", "low", "medium", "high", "max"].includes(
        String(current.thinking_level || "").toLowerCase())
        ? String(current.thinking_level).toLowerCase()
        : (level === "disabled" ? "off" : "high");

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
    /* 「运行前自动优化提示词」开关已删：它从来没有消费方（存了就没人读），
     * 是个**不能兑现的承诺** —— 勾了以为运行前会优化一遍，实际什么都没发生。
     * 要么接上要么删掉；接上要动采样链路，这里先删。 */

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
    const rowTimeout = row("请求超时（秒）", timeout);
    const rowThinking = row("思考强度", thinking);
    const thinkingHint = el("div", "h3d-opt-hint");
    dialog.append(thinkingHint);
    /* 选项按**当前模式与型号**重建：
     *   支持强度（glm-5.x）  → 关闭 / 低 / 高 / 最高
     *   不支持强度（glm-4.6v）→ 关闭 / 开启
     *   本地模型             → 关闭 / 开启（本地没有 thinking 字段，关思考靠 /no_think）
     *   其它端点             → 整行隐藏（后端不下发这两个字段）
     * 强制思考型号（glm-5.3 / 4.7 / 4.5v）选「关闭」会被后端翻译成 low，
     * 界面上**直接说明**，否则用户以为自己关成功了、实际还在思考。 */
    const rebuildThinking = () => {
        const local = mode.value === "local";
        const glm = !local && optIsGlm(url.value);
        const fam = local ? "local" : optThinkingFamily(url.value, model.value);
        const show = local || glm || fam === "gpt" || fam === "deepseek" || fam === "unknown";
        rowThinking.classList.toggle("h3d-opt-hidden", !show);
        thinkingHint.classList.toggle("h3d-opt-hidden", !show);
        if (fam === "unknown") {
            /* 未知型号：后端一个思考字段都不下发。这句话必须写出来 —— 以前整行
             * 隐藏，用户看到的是"没得选"，很容易误以为等于关掉了。 */
            thinking.replaceChildren();
            thinking.append(new Option("不干预（用服务商默认）", ""));
            thinking.value = "";
            thinkingHint.textContent = "型号不在已知名单（GLM / GPT / DeepSeek）：本插件**不干预**思考，"
                + "一个思考参数都不下发，由服务商默认决定 —— 思考型模型多半是**开**的，"
                + "若遇正文被截断请调大「最大输出 token」。";
            thinkingHint.classList.remove("h3d-opt-hidden");
            return;
        }
        if (fam === "gpt" || fam === "deepseek") {
            const opts = [["off", "关闭思考（最快）"], ["low", "低"], ["medium", "中"],
                          ["high", "高"], ["max", "最高"]];
            thinking.replaceChildren();
            for (const [v, l] of opts) thinking.append(new Option(l, v));
            thinking.value = opts.some(([v]) => v === tkLevel) ? tkLevel : "off";
            const notes = [];
            if (fam === "gpt") {
                notes.push("GPT-5.6 可关闭思考；GPT-6 档位里没有「关闭」，选关闭会降到最低档。"
                    + "GPT-6 不接受温度参数，后端已自动不下发。");
            } else {
                notes.push("DeepSeek 默认开思考（high）；档位只有 低/高/最高（选「中」按官方映射落到「高」）。");
            }
            /* 推理 token 与正文共用 max_tokens 配额（GLM 那条同理） */
            if (["high", "max"].includes(thinking.value) && Number(maxTokens.value) < 16384) {
                notes.push("高强度思考的推理过程也占「最大输出 token」配额，建议调到 16384 以上。");
            }
            thinkingHint.textContent = notes.join(" ");
            thinkingHint.classList.remove("h3d-opt-hidden");
            return;
        }
        if (local) {
            /* 本地通道没有 thinking / reasoning_effort 字段 —— 后端只能往输入里塞
             * Qwen 系的 /no_think 软开关，所以只有开/关两档，没有低/高/最高。 */
            thinking.replaceChildren();
            thinking.append(new Option("关闭思考（最快）", "disabled"),
                            new Option("开启思考", "enabled"));
            thinking.value = level === "disabled" ? "disabled" : "enabled";
            const w = thinking.value === "enabled"
                ? "本地模型的长思考同样占「最大输出 token」配额，正文可能被截断"
                  + "（思考过程由后端剥掉，不会写进提示词）。" : "";
            thinkingHint.textContent = w;
            thinkingHint.classList.toggle("h3d-opt-hidden", !w);
            return;
        }
        if (!glm) return;
        const caps = optModelCaps(model.value);
        const opts = [["disabled", "关闭思考（最快）"]];
        if (caps.supports) for (const v of caps.values) opts.push([v, OPT_EFFORT_LABEL[v] || v]);
        else opts.push(["enabled", "开启思考"]);
        thinking.replaceChildren();
        for (const [v, l] of opts) thinking.append(new Option(l, v));
        const has = (v) => opts.some(([x]) => x === v);
        /* 档位在当前型号上不存在（如从 glm-5.3 的最高档切到 glm-4.6v）时，
         * 退到「开启」但**不覆盖 level** —— 切回去还能还原用户选的档。 */
        thinking.value = has(level) ? level : (has("enabled") ? "enabled" : "disabled");
        const name = model.value.trim() || "该型号";
        const warns = [];
        if (caps.forced && caps.supports) {
            warns.push(`「${name}」始终思考，服务商不允许关闭；选「关闭思考」将按最低强度 low 执行。`);
        } else if (caps.forced) {
            warns.push(`「${name}」始终思考，且不支持强度分级；选「关闭思考」将按服务商默认强度执行。`);
        } else if (!caps.supports) {
            warns.push("该型号无强度分级，只能开或关（要分档请换 glm-5 系型号）。");
        }
        /* 推理 token 与正文**共用** max_tokens 配额：实测 effort=max 单是推理就
         * 产出 13255 字（≈上万 token），8192 的额度会被推理吃光，正文一个字都
         * 出不来 —— 用户看到的「LLM 返回空文本」就是这么来的。额度不够就直接
         * 在这里点出来，别等他跑完再报错。 */
        if (caps.supports && ["high", "max"].includes(thinking.value)
            && Number(maxTokens.value) < 16384) {
            warns.push("高强度思考的推理过程也占「最大输出 token」配额，建议调到 16384 以上。");
        }
        thinkingHint.textContent = warns.join(" ");
        thinkingHint.classList.toggle("h3d-opt-hidden", !warns.length);
    };
    const langRow = el("label", "h3d-opt-row"); langRow.append(el("span", "", "输出语言"), language); dialog.append(langRow);
    const ruleRow = el("label", "h3d-opt-row"); ruleRow.append(el("span", "", "提示词规则"), ruleSel); dialog.append(ruleRow);
    const checks = el("div", "h3d-opt-checks");
    const chk = (t, c) => { const lb = el("label", ""); lb.append(c, el("span", "", t)); checks.append(lb); };
    chk("读取视觉参考（图片转 dataURL，最多 8 张）", readMedia);
    /* 「深度思考」勾选框已升级成上面的「思考强度」下拉：勾选框只能表达开/关，
     * 而 glm-5.3 这类型号真正的控制维度是 low/high/max 三档。 */
    dialog.append(checks);
    /* ---- AI 扩写优化设置（段卡「✨ AI扩写+优化」按钮的参数）----
     * 扩写弹窗取消后，这些参数没有地方填了 —— 住进设置里，点按钮就直接用，
     * 不再每次弹窗问一遍。 */
    const ex = optGetExpandSettings(node);
    dialog.append(el("div", "h3d-opt-sub", "AI 扩写优化设置"));
    dialog.append(el("div", "h3d-opt-hint",
        "段卡「✨ AI扩写+优化」按钮用的参数：把框里的内容当意图，先扩写成剧本，"
        + "再按官方格式优化后写回同一个框（原稿自动留存，点「原稿」可还原）。"));
    const exStyle = el("select", "");
    for (const [v, l] of [["strict", "严格（只补机位与声源）"],
        ["balanced", "均衡（可补光位与材质）"],
        ["creative", "创意（可补 1 个视觉细节）"]]) exStyle.append(new Option(l, v));
    exStyle.value = ["strict", "creative"].includes(ex.style) ? ex.style : "balanced";
    const exThen = el("input", ""); exThen.type = "checkbox";
    exThen.checked = ex.then_optimize !== false;
    row("扩写风格", exStyle);
    const exChecks = el("div", "h3d-opt-checks");
    {
        const lb = el("label", "");
        lb.append(exThen, el("span", "", "扩写后自动接提示词优化（关掉 = 只扩写，不改格式）"));
        exChecks.append(lb);
    }
    dialog.append(exChecks);

    const refreshIcon = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7a5 5 0 0 1 4.9 4H20a8 8 0 0 0-2.35-4.65ZM12 17a5 5 0 0 1-4.9-4H4a8 8 0 0 0 8 7v3l5-5-5-5v4Z"/></svg>';
    const refreshModels = el("button", "h3d-btn h3d-opt-refresh", refreshIcon);
    refreshModels.type = "button"; refreshModels.title = "重新扫描本地模型列表（调 /h3chain/optimizer-config）";
    const refreshRow = el("label", "h3d-opt-row"); refreshRow.append(el("span", "", "刷新模型"), refreshModels); dialog.append(refreshRow);

    let models = Array.isArray(current._models) ? current._models : [];
    let mmprojModels = Array.isArray(current._mmproj) ? current._mmproj : [];

    /* 模型存放位置 + 缺件提示（后端 llm_env 下发，随「刷新模型」一起更新）。
     * 只在本地模式下显示；后端没给（旧后端/接口异常）就整块省略，别挡设置。 */
    const envHint = el("div", "h3d-opt-hint h3d-opt-env");
    envHint.style.whiteSpace = "pre-line";
    dialog.append(envHint);
    const renderEnv = () => {
        const env = current._env;
        if (mode.value !== "local" || !env) { envHint.classList.add("h3d-opt-hidden"); return; }
        const lines = [];
        if (env.llm_dir) {
            lines.push("模型目录：" + env.llm_dir
                + "（.gguf 主模型与 *mmproj*.gguf 投影都放这里，放好后点上面「刷新模型」）");
            if (env.llm_dir_exists === false) {
                lines.push("⚠ 这个目录还不存在：请在 ComfyUI 的 models 文件夹下新建一个 llm 目录再放模型");
            }
        }
        const dep = env.deps || {};
        const miss = [];
        if (dep.llama_cpp?.installed === false) miss.push("llama-cpp-python（GGUF 必需）");
        if (dep.transformers?.installed === false) miss.push("transformers（Transformers 格式模型必需）");
        if (miss.length) {
            lines.push("缺少依赖：" + miss.join("、") + "\n装进 ComfyUI 同一个 Python 环境，"
                + "GGUF 显卡加速参考：pip install llama-cpp-python --extra-index-url "
                + "https://abetlen.github.io/llama-cpp-python/whl/cu130"
                + "（cu130 = CUDA 13.0；驱动较老往下换 cu125 / cu124 / cu123）\n"
                + "注意：预编译轮子只支持 Python 3.10/3.11/3.12；CUDA 13 轮子要求显卡算力 7.5 以上");
        }
        envHint.textContent = lines.join("\n");
        envHint.classList.toggle("h3d-opt-hidden", !lines.length);
    };
    renderEnv();

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
        /* URL / 模型 / 协议三行对**所有**服务商可见（以前只给"自定义"看）。
         * 隐藏它们时用户看不到自己实际在调什么模型 —— 默认模型换成 GLM-4.6V
         * 之后这一点尤其要紧：看不见就没法确认改没改到。 */
        rowKey.classList.toggle("h3d-opt-hidden", local);
        rowUrl.classList.toggle("h3d-opt-hidden", local);
        rowModel.classList.toggle("h3d-opt-hidden", local);
        rowProto.classList.toggle("h3d-opt-hidden", local);
        rowTimeout.classList.toggle("h3d-opt-hidden", local);
        rowLocal.classList.toggle("h3d-opt-hidden", !local);
        refreshRow.classList.toggle("h3d-opt-hidden", !local);
        rowDevice.classList.toggle("h3d-opt-hidden", !local);
        /* 思考强度跟 URL/型号/模式走，所以每次重绘都重建一遍。本地模式**也要给**：
         * 本地靠 /no_think 软开关关思考，藏起来的话用户既看不见也改不了，
         * 只能被默认值支配（后端报错时还让他去调一个看不见的下拉）。 */
        rebuildThinking();
        const sel = models.find((m) => m.relative_path === localModel.value);
        rowMmproj.classList.toggle("h3d-opt-hidden", !local || !(sel && sel.format === "gguf"));
        renderEnv();
        void rowMode; void rowProv;
    };
    /* 预设只在**切换服务商时**落地一次。放进 sync() 会在任何一次重绘
     * （切模式、选本地模型…）把用户手改过的 URL / 模型冲回预设值。 */
    const applyPreset = (value) => {
        const preset = OPT_PROVIDERS[value];
        if (!preset || value === "custom") return;
        url.value = preset.url;
        protocol.value = preset.protocol;
        model.value = providerModels[value] || preset.model;
    };
    if (!url.value) url.value = current.api_url || "";
    if (!model.value) model.value = current.model || "";
    protocol.value = ["openai", "responses", "gemini"].includes(current.protocol) ? current.protocol : "openai";
    provider.addEventListener("change", () => {
        /* prev 是「上一次选中的服务商」，**必须即用即写回**。
         * 少这一行时 dataset.prev 永远是初始值（默认 glm）：每次切换都把当前框里的
         * 模型名存进**初始服务商**的记忆槽 —— 智谱的槽被别家的模型名逐次覆盖，
         * 于是切回智谱看到的是上一个服务商的模型（其余服务商因为槽位从没被写过，
         * 每次都回落预设默认名，反倒"看起来正常"）。 */
        const prev = provider.dataset.prev || current.provider || "glm";
        apiKeys[prev] = key.value;
        providerModels[prev] = model.value;
        provider.dataset.prev = provider.value;
        key.value = apiKeys[provider.value] || "";
        applyPreset(provider.value);
        keyHint();
        sync();
    });
    mode.addEventListener("change", () => { sync(); keyHint(); });
    /* 改型号 / 改地址都可能改变思考能力（glm-4.6v ↔ glm-5.3-flash 就是两种形态），
     * 所以两行都要重算。用 input 事件（不是 change）—— 边打字边更新，选完就看到
     * 「该型号不支持关闭」这类说明，不用等失焦。 */
    model.addEventListener("input", rebuildThinking);
    url.addEventListener("input", rebuildThinking);
    /* max_tokens 也参与提示：高强度思考 + 额度不够要当场提醒（见 rebuildThinking） */
    maxTokens.addEventListener("input", rebuildThinking);
    thinking.addEventListener("change", () => {
        /* 两个口径的取值**有重叠**（low/high/max 既是 GLM 的强度档也是新档位），
         * 所以不能靠值判断走哪套：两套变量都要更新，各自的下拉自己挑着用。
         * 只更新一个的话，GLM 上选「max」会被当成新档位，老口径的 level 停在
         * 原值 → 重建下拉时又被拉回「关闭」，用户看着像没选上。 */
        level = thinking.value;
        tkLevel = ["off", "low", "medium", "high", "max"].includes(thinking.value)
            ? thinking.value
            : (thinking.value === "disabled" ? "off" : (thinking.value === "enabled" ? "high" : ""));
        rebuildThinking();
    });
    localModel.addEventListener("change", () => { fillLocal(); sync(); guide.style.display = localModel.value ? "none" : ""; });
    refreshModels.onclick = async () => {
        refreshModels.disabled = true;
        try {
            if (!window.H3Api?.getOptimizerConfig) throw new Error("h3_api.js 未更新（缺 getOptimizerConfig）");
            const r = await window.H3Api.getOptimizerConfig();
            if (!r.body?.ok) throw new Error(window.H3Api.errText(r, "刷新失败"));
            models = Array.isArray(r.body.models) ? r.body.models : [];
            mmprojModels = Array.isArray(r.body.mmproj_models) ? r.body.mmproj_models : [];
            if (r.body.llm_env && typeof r.body.llm_env === "object") current._env = r.body.llm_env;
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
        const mt = Math.max(512, Math.min(32768, Number(maxTokens.value) || 8192));
        const to = Math.max(30, Math.min(1800, Number(timeout.value) || 300));
        /* 思考强度档 → 两个后端字段：强度档本身就是"开思考 + 指定强度"，
         * 「关闭」「开启」两个档不带强度。 */
        const lv = thinking.value;
        const isEffort = ["low", "high", "max"].includes(lv);
        const body = {
            mode: mode.value, provider: provider.value,
            /* 三行现在对所有服务商可见，就以**框里的值**为准（预设只在切服务商时
             * 落一次）。以前预设值直接覆盖用户输入，等于这三行对预设服务商不可改。 */
            api_url: url.value.trim() || preset?.url || "",
            api_key: key.value.trim(), api_keys: { ...apiKeys },
            model: model.value.trim() || preset?.model || "",
            provider_models: { ...providerModels },
            protocol: protocol.value || preset?.protocol || "openai",
            read_media: readMedia.checked, output_language: outLang,
            local_model: localModel.value, local_mmproj: mmproj.value, local_device: device.value,
            rule_file: ruleSel.value,
            max_tokens: mt, timeout: to,
            thinking: isEffort ? "enabled" : lv,
            reasoning_effort: isEffort ? lv : "",
            /* 内部统一档位：只给 GPT / DeepSeek 存（这两家后端按它翻译字段）；
             * 未知型号留空 = 不干预，别把档位写死到不认识的型号上。 */
            thinking_level: (optThinkingFamily(url.value.trim(), model.value.trim()) === "gpt"
                || optThinkingFamily(url.value.trim(), model.value.trim()) === "deepseek")
                ? (["off", "low", "medium", "high", "max"].includes(thinking.value)
                    ? thinking.value : tkLevel) : "",
            cfg_ver: OPT_CFG_VER,
            /* AI 扩写优化设置（段卡「AI扩写+优化」按钮的参数） */
            expand: {
                style: exStyle.value,
                then_optimize: exThen.checked,
            },
        };
        if (body.mode === "local" && !body.local_model) { alert("请先选择一个本地视觉模型"); return; }
        /* Key 留空**不算错**：服务端可能内置了 Key（hasDefaultKey），此时留空
         * 就是"用内置的"。只有既没填、服务端也没有，才拦。 */
        if (body.mode === "api" && !body.model) { alert("请填写模型名"); return; }
        if (body.mode === "api" && !body.api_key && !hasDefaultKey) {
            alert("请填写 API Key（或改用本地模型）"); return;
        }
        optSaveSettings(node, body);
        close();
        if (onSaved) onSaved(body);
        scheduleRefresh(120);
    };
    document.body.append(overlay);
}


function paintOptbar(optbar, node, data, idx, ta) {
    try {
        optbar.replaceChildren();
        optRestoreMaps(node, data.ds);
        const key = node ? optKey(node, idx) : "";
        const shown = key ? _optShown.get(key) : null;
        const bReset = el("button", "h3d-btn", shown === "before" ? "优化稿" : "原稿");
        bReset.title = "原稿 ⇄ 优化稿切换";
        bReset.style.display = shown ? "" : "none";
        const ui = { btn: null, reset: bReset, name: null };
        bReset.onclick = () => optToggle(node, idx, ta, ui);

        optbar.append(bReset);
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
    /* 互斥只看**合并导出进行中**，不看 `mergeSel.on`：合并模式现在是个常显开关，
     * 随手开着但清单空着时不该拦住生成。 */
    if (mergeSel.running) { alert("合并导出进行中：等它跑完再提交生成"); return; }
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

/** 终止当前生成 —— 语义与 ComfyUI 顶上那个「终止运行」**完全一致**（都是
 *  POST /interrupt：服务端在当前节点收尾后停下，已生成完的段保留在存档里，
 *  未生成的段不跑）。唯一区别是它摆在导演台内部，不用为了取消一次生成先退出
 *  面板去外面找按钮。
 *
 *  二次确认里必须写清「会中断 ComfyUI 当前所有作业」：/interrupt 是**全局**的，
 *  一台机器多人/多标签共用时不说清楚，点一下会把别人的作业一起停掉。 */
async function stopGeneration() {
    if (!window.confirm("终止当前生成？\n\n"
        + "· 已生成完的段会保留在存档里，未生成的段不跑；\n"
        + "· 这一下等同 ComfyUI 顶上的「终止运行」，会中断 ComfyUI 当前正在跑的\n"
        + "  **所有**作业（不只是这一条链）；\n"
        + "· 正在写的提示词不会被清掉。")) {
        return;
    }
    let sent = false;
    /* 三条路都试：ComfyUI 新前端挂在 comfyAPI，老版本挂在 app.api，
     * 两者都没有就直接打 /interrupt。全失败也要如实报出来 —— 静默失败
     * 会让用户以为"点了没反应、还在跑"。 */
    const tries = [
        async () => {
            const a = window.comfyAPI?.api?.api;
            if (a && typeof a.interrupt === "function") { await a.interrupt(); return true; }
            return false;
        },
        async () => {
            if (typeof app !== "undefined" && typeof app.api?.interrupt === "function") {
                await app.api.interrupt();
                return true;
            }
            return false;
        },
        async () => {
            const r = await fetch("/interrupt", { method: "POST" });
            return !!r.ok;
        },
    ];
    for (const fn of tries) {
        try { if (await fn()) { sent = true; break; } } catch (e) { /* 换下一条路 */ }
    }
    if (!sent) {
        setLed("err", "终止请求没发出去（请用 ComfyUI 自带的「终止运行」）");
        alert("没能连上 ComfyUI 的中断接口。\n请改用 ComfyUI 界面上自带的「终止运行」。");
        return;
    }
    setLed("idle", "已请求终止（等当前节点收尾）");
    scheduleRefresh(600);
    /* 中断不是瞬时的（服务端要等当前节点收尾），隔一会儿再按后端真实状态
     * 校正一次灯：终止生效后不该还挂着「running」。 */
    setTimeout(() => { syncRunLed(); }, 1500);
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
    if (mergeSel.running) { alert("合并导出进行中：等它跑完再提交重摇"); return; }
    let restored = [];
    if (lastDir) {
        const mf = await fetchManifest(lastDir);
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
    if (mergeSel.running) { alert("合并导出进行中：等它跑完再标记重摇"); return; }
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
        /* latent 策略透存，防 getDs 归一化洗掉 */
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
    /* 原稿/优化稿缓存按目录分作用域：换项目即清掉旧作用域的条目（数据在 widget 的
     * ds.opt_hist[dir] 里，切回去会重新装回来）—— 留着也不会被命中，只是白占内存。 */
    optPurgeOtherScopes(dir);
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
    let bridgeModels = [];
    /* 链状态指针（h3_projects/h3chain_state.json）随项目列表一起回。它以前由前端
     * 经 /api/view 直读 —— 同一个陈旧缓存问题：这个文件每跑一段就改写。 */
    let stateRaw = null;
    if (ping.ok) {
        const r = await apiGet("/h3chain/projects");
        if (r.ok) {
            projects = r.data?.projects || [];
            stateRaw = r.data?.state || null;
        }
        const um = await apiGet("/h3chain/upscale_models");
        if (um.ok) upscaleModels = um.data?.models || [];
        const bm = await apiGet("/h3chain/bridge_models");
        if (bm.ok) bridgeModels = bm.data?.models || [];
    }
    setApiError(ping.ok ? "" :
        `项目存档接口未注册（HTTP ${ping.status || "??"}）：请重启 ComfyUI 并检查控制台是否出现`
        + `「[ComfyUI_H3_SeamlessChain] 路由已注册」日志；若仍失败请把控制台报错反馈给开发。`);

    /* 当前项目：节点「存档目录」指向优先（用户刚切换还没跑），回落 state 指针 */
    const nodeDir = node ? String(getDirValue(node) || "").trim() : "";
    const dir = nodeDir || stateRaw?.dir || "";
    lastDir = dir;                                           // 合并导出等即时动作取当前项目
    const mf = dir ? await fetchManifest(dir) : null;
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
    return { node, state, mf, plan, drafts, ds, projects, apiOk: ping.ok, upscaleModels, bridgeModels };
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
    /* 禁用态必须看得出来：否则「按钮灰了」和「点了没反应」从外观上分不出来 */
    .h3d-btn:disabled{opacity:.45;cursor:not-allowed}
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
    /* 顶栏「↻ 刷新」：原来挂在状态条里，被压到 11px 挤在一行；现在上顶栏，
     * 按 .h3d-btn 的常规尺寸走，跟旁边「⌨ 输入修复 / ⚡ 性能优化」一个规格。 */
    .h3d-refresh{flex:none}
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
    /* .h3d-card.mergeable / .mergeable-off 已删：段卡上的合并勾选框随合并模式
     * 重构一起下线，合并清单改在素材库里点选（见 h3_library.js 的合并模式）。 */
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
    /* .h3d-mergecb 已删（段卡合并勾选框下线）。 */
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
    .h3d-rtok{border-color:#2f5a8e;background:#111f2e;color:#8fc4f0;padding:0 4px;cursor:default}
    .h3d-rtok.h3d-rtok-missing{border-color:#8e2f2f;background:#2e1414;color:#f09595}
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
    /* 设置面板里的行内说明（思考强度的能力提示等）：小一号、弱化色，
       它解释的是"为什么这个选项长这样"，不是必读项。 */
    .h3d-opt-hint{color:var(--h3d-muted);font-size:11px;line-height:1.6;margin:-2px 0 8px}
    /* ---- ⚡ 性能优化弹窗（共用 AI 优化设置的 overlay / dialog 底）----
       右栏那种窄折叠装不下「控件 + 整段说明」，两者挤一行等于把说明吞掉
       （用户报的「信息展现不全」）。这里一律两行式：第一行 label + 控件，
       第二行说明全文换行。 */
    /* 弹窗用**列布局**：标题与底栏固定，只有正文滚。
       AI 优化设置那份是「整个 dialog 带滚动条」，按钮跟在内容末尾 —— 项一多就
       必须先滚到底才能点保存（用户报的痛点）。这里把按钮放到滚动区之外。 */
    .h3d-perf-dialog{width:min(1040px,calc(100vw - 40px));height:min(900px,92vh);max-height:92vh;overflow:hidden;display:flex;flex-direction:column}
    .h3d-perf-dialog>.h3d-opt-title,.h3d-perf-dialog>.h3d-opt-sub{flex:none}
    .h3d-perf-body{flex:1 1 auto;min-height:0;overflow:auto;overflow-x:hidden;padding-right:6px}
    .h3d-perf-foot{flex:none;margin-top:10px;border-top:1px solid #37332b;padding-top:9px}
    .h3d-perf-dialog .h3d-opt-actions{margin-top:8px}
    .h3d-perf-diag{border:1px solid #37332b;border-radius:8px;background:#181712;padding:9px 11px;margin:0 0 10px}
    .h3d-perf-diag pre{margin:6px 0 0;white-space:pre-wrap;word-break:break-all;color:#9fb0bd;font:11px/1.7 ui-monospace,Consolas}
    .h3d-perf-row{padding:8px 0;border-bottom:1px dashed #2e2a23}
    .h3d-perf-row:last-child{border-bottom:0}
    .h3d-perf-top{display:flex;align-items:center;justify-content:space-between;gap:12px}
    .h3d-perf-top>label{color:#c8c2b4;font-size:12px;font-weight:600;min-width:0}
    .h3d-perf-top select,.h3d-perf-top input[type=number]{flex:none;border:1px solid #3a352c;border-radius:6px;background:#211f1a;color:var(--h3d-bone);padding:5px 7px;font-size:12px;outline:none;font-family:inherit;max-width:280px}
    .h3d-perf-top input[type=number]{width:96px}
    .h3d-perf-top input[type=checkbox]{width:15px;height:15px;accent-color:#7fc79f;flex:none}
    .h3d-perf-hint{margin-top:4px;color:var(--h3d-muted);font-size:11.5px;line-height:1.65}
    .h3d-perf-status{margin:8px 0 0;color:#8fc9a5;font-size:11.5px;line-height:1.6;white-space:pre-wrap}
    /* 弹窗内的两级分类树：复用 foldSection（折叠态记忆），视觉覆盖右栏那套 ——
       右栏的齿轮图标（.h3d-adv summary::before）与紧凑内边距在弹窗里不合适。
       特异性必须带上 .h3d-perf-body：.h3d-adv 那几条规则定义在本块之后，
       同级特异性会被它们压掉。 */
    .h3d-perf-body .h3d-perf-group{border:1px solid #37332b;border-radius:9px;background:#1d1a15;margin:9px 0 0}
    .h3d-perf-body .h3d-perf-group>summary{display:flex;align-items:center;gap:8px;padding:9px 11px;font-size:12.5px;font-weight:600;color:var(--h3d-bone)}
    .h3d-perf-body .h3d-perf-group>summary::before{content:"▸";font-size:10px;color:var(--h3d-muted);transition:transform .15s}
    .h3d-perf-body .h3d-perf-group[open]>summary{border-bottom:1px solid #302c25;color:var(--h3d-copper)}
    .h3d-perf-body .h3d-perf-group[open]>summary::before{transform:rotate(90deg)}
    .h3d-perf-body .h3d-perf-group>summary small{margin-left:auto;font-size:11px;font-weight:400;color:var(--h3d-muted)}
    .h3d-perf-body .h3d-perf-sub{border:0;border-left:2px solid #302c25;border-radius:0;background:transparent;margin:0 0 0 10px}
    .h3d-perf-body .h3d-perf-sub>summary{display:flex;align-items:center;gap:7px;padding:7px 10px;font-size:11.5px;font-weight:600;color:var(--h3d-muted)}
    .h3d-perf-body .h3d-perf-sub>summary::before{content:"▸";font-size:9px;color:#5f5a4f;transition:transform .15s}
    .h3d-perf-body .h3d-perf-sub[open]>summary{border-bottom:0;color:#a9a396}
    .h3d-perf-body .h3d-perf-sub[open]>summary::before{transform:rotate(90deg)}
    .h3d-perf-body .h3d-perf-group>.h3d-perf-row,.h3d-perf-body .h3d-perf-sub>.h3d-perf-row{padding:8px 11px}

    /* ---- 优化进度条（SSE）----
       思考阶段几十秒没有正文，没有它用户只会以为点下去卡死了。
       固定贴在视口底部中间：段卡可能在滚动区任意位置，跟着卡片走会跑出视野。 */
    .h3d-opt-prog{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:1000004;width:min(420px,calc(100vw - 32px));border:1px solid #464033;border-radius:11px;background:linear-gradient(160deg,#242019,#191712 62%);box-shadow:0 16px 44px #000c;padding:11px 13px;color:var(--h3d-bone);font:12px/1.5 "Microsoft YaHei UI","Segoe UI",sans-serif;box-sizing:border-box}
    .h3d-opt-prog *{box-sizing:border-box}
    .h3d-opt-prog-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:7px}
    .h3d-opt-prog-name{font-weight:600;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .h3d-opt-prog-cancel{flex:none;border:1px solid #4a4436;border-radius:6px;background:#211f1a;color:#c8c2b4;font:11px/1.4 inherit;padding:2px 9px;cursor:pointer}
    .h3d-opt-prog-cancel:hover{border-color:#8a5a42;color:#e0a892}
    .h3d-opt-prog-cancel:disabled{opacity:.5;cursor:default}
    .h3d-opt-prog-track{height:6px;border-radius:4px;background:#211f1a;border:1px solid #37332b;overflow:hidden}
    .h3d-opt-prog-fill{height:100%;width:4%;border-radius:3px;background:linear-gradient(90deg,#7fc79f,#f0c274);transition:width .35s ease-out}
    .h3d-opt-prog-meta{margin-top:6px;color:var(--h3d-muted);font:10.5px ui-monospace,Consolas;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .h3d-opt-prog.h3d-done .h3d-opt-prog-fill{background:linear-gradient(90deg,#7fc79f,#a8d8bd)}
    .h3d-opt-prog.h3d-done .h3d-opt-prog-meta{color:#8fc9a5}

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
/* 段级两个开关的外框行：并进**页签行**里（提示词 / 锚定设置 同一栏），
 * margin-left:auto 把它顶到行尾，跟页签共用一行不再单独占一行。 */
.h3d-pubswitch{margin:0 0 0 auto;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.h3d-refrow{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:4px;padding:7px 8px;border:1px solid #37332b;border-radius:7px;background:#181712}
    .h3d-refrow>label{color:var(--h3d-muted);font-size:10.5px;font-weight:600;flex:none}
    /* 有引用 → 整条转绿：chip 一多，"本段到底挂没挂素材"靠逐个找太慢 */
    .h3d-refrow.on{border-color:#2f6e57;background:#12200f}
    /* 编译映射条：标注 · 素材名 → 官方 token。两层编号的差异必须一眼可见
       —— 只显示一层，用户会以为「图片3」编译出去还是 3。 */
    .h3d-refmap{display:flex;flex-direction:column;gap:2px;width:100%;margin-top:3px}
    .h3d-refmap-row{display:flex;align-items:center;gap:6px;font:10px ui-monospace,Consolas;color:var(--h3d-muted)}
    .h3d-refmap-mark{color:var(--h3d-copper);font-weight:600;flex:none}
    .h3d-refmap-name{color:var(--h3d-bone);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .h3d-refmap-tok{color:#7fe0b0;flex:none}
    .h3d-refmap-edit{border:1px solid #3a352c;border-radius:5px;background:#25221c;color:var(--h3d-muted);font:10px ui-monospace,Consolas;padding:0 5px;cursor:pointer;margin-left:auto;flex:none}
    .h3d-refmap-edit:hover{filter:brightness(1.3)}
    .h3d-refhint-bad{color:var(--h3d-danger,#c0392b);font-weight:600}
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
    /* 对齐指令预览（.h3d-alignprev / -alignline / -alignnone / -alignhint）随
     * mkAlignPreview 一起删：对齐指令由后端注入，前端不再展示。 */
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
    /* 二采的两个子面板（基础/高级）：与外框拉开层次，各自默认收起 */
    .h3d-upsub{margin:8px 10px 0;border-color:#2c4a52}
    .h3d-upsub summary{padding:7px 10px}
    .h3d-upwarn{grid-column:1/-1;padding:7px 10px;border:1px solid #9a4144;border-radius:7px;background:#402227;color:#f0a0a4;font-size:11.5px;line-height:1.6}
    .h3d-param{margin:0}
    .h3d-param .h3d-hint{display:block;margin-top:4px}

    .h3d-loadwf{display:flex;flex-direction:column;gap:10px;align-items:center;padding:22px 14px;border:1px dashed #46604f;border-radius:10px;background:#1f2a23;text-align:center;line-height:1.9}
    .h3d-loadwf p{margin:0;color:var(--h3d-muted);font-size:11.5px}

    .h3d-footer{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:0 20px;border-top:1px solid var(--h3d-line);background:#1b1a16}
    .h3d-footinfo{display:flex;gap:16px;color:var(--h3d-muted);flex-wrap:wrap;min-width:0;font-size:11.5px;align-items:center}
    .h3d-footinfo b{color:var(--h3d-bone)}
    .h3d-run{min-width:150px;padding:11px 18px}
    /* ✕ 终止：比普通 danger 更实心的红 —— 它是页脚里唯一"会打断正在跑的东西"
     * 的按钮，必须一眼就跟旁边黄色的「开始生成」区分开，不能误点。 */
    .h3d-stop{border:1px solid #c2565f;background:#7d2b33;color:#ffe0e3;font-weight:700}
    .h3d-stop:hover{background:#932f39;border-color:#e06b74;filter:brightness(1.08)}

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

    /* ---- 总提示词工作台入口（纯分段流水线，见 openMasterPromptModal） ----
     * 单框可拖右下角伸缩（resize 挂在 wrapper 上，wrapper 内部编辑器
     * flex 撑满，这样拖动的是整块而不是只有编辑区）；内容超出即框内滚轮滚动。 */
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
    /* 三框时代的 -1/-2/-3（按框序给高度）随三框一起删了：现在是**单框**，
     * 高度就该写成"主框"这一个语义，不是"第三个框"。 */
    .h3d-mpboxwrap-main{height:100%;min-height:260px}
    .h3d-mplabel{display:flex;gap:8px;align-items:baseline;flex:none;font-size:11.5px}
    .h3d-mplabel b{color:var(--h3d-cyan);font-size:11.5px;font-weight:700}
    .h3d-mplabel small{color:var(--h3d-muted);font-size:10.5px;line-height:1.4;flex:1;min-width:0}
    .h3d-mpboxbtn{padding:2px 9px;font-size:11px;flex:none;align-self:center}
    /* 编辑器是 contenteditable 的 DIV（createPromptEditor 产出 div.h3d-ta.h3d-rta，
     * 工作台这里再补一个 .h3d-mpbox），**不是 textarea**。原来这条选择器写的是元素名
     * 「textarea」，对 DIV 永远匹配不上，于是内层高度落到 .h3d-rta 的 max-height:40vh，
     * 外框却被 -main 的 height:100% 撑满 —— 正文只占上半截，下半截全是死空白。
     * 现在按真实类名选（.h3d-mpbox 由 JS 加在编辑器根上，也是单框用例的句柄），
     * 并顺手解开 40vh 封顶：高度交给 flex 分配，正文填满整块。 */
    .h3d-mpboxwrap>.h3d-mpbox{flex:1;min-height:0;max-height:none;width:100%;overflow:auto;resize:none;border:0;box-shadow:none;background:transparent;color:var(--h3d-bone);padding:0;font:12.5px/1.7 ui-monospace,Consolas,monospace;outline:none}
    /* 「.h3d-optsetbar」已删：AI 优化设置条从段卡挪到顶栏（与 ⚡ 性能优化 同级），
     * 段卡里不再有这个容器。总提示词框曾经的「模式 / 缺省时长」行与「参考素材
     * （AI 可见）」chips 更早前已随那一跳一起删掉。 */

    .h3d-fab{position:fixed;right:16px;top:120px;z-index:80;width:44px;height:44px;border-radius:50%;border:1px solid #46604f;background:#1f2a23;color:#c2e0cd;cursor:pointer;font-size:17px}
    .h3d-fab:hover{filter:brightness(1.2)}

    /* ---- 折叠分组（主提示词框三段式与各面板共用） ---- */
    .h3d-v2panel{border-color:#2c4a52;background:#141d21}
    .h3d-v2panel.has-content{border-color:#316dca80;box-shadow:inset 0 0 0 1px #316dca26}
    .h3d-v2group{margin-top:6px;border:1px solid #2a3438;border-radius:7px;background:#10161a;overflow:hidden}
    .h3d-v2group>summary{padding:7px 10px;cursor:pointer;color:var(--h3d-cyan);font-size:11.5px;font-weight:700;user-select:none}
    .h3d-v2group>summary::-webkit-details-marker{display:none}
    .h3d-v2group>summary::before{content:"▸ ";font-size:10px}
    .h3d-v2group[open]>summary::before{content:"▾ "}
    .h3d-ppane{margin-bottom:8px}
    .h3d-ppane>.h3d-v2grid{padding:2px 8px 8px}
    /* 段卡提示词框：自己一条垂直滚动条，不靠中栏外层滚。
     * 上限压到 38vh —— 中栏可视高约 78vh，上面还压着状态条 / 进度轨 / 选段条 /
     * 锚定栏 / 页签，框再高就得先滚中栏才看得全，"框内独立滚"等于白给。 */
    .h3d-ppane .h3d-rta{min-height:170px;max-height:38vh;overflow-y:auto}
    .h3d-v2grid{display:grid;gap:7px;padding:0 10px 10px}
    .h3d-v2f{width:100%;border:1px solid #2a3438;border-radius:6px;background:#1b2126;color:var(--h3d-bone);padding:6px 8px;font-size:12px;outline:none;font-family:inherit}
    .h3d-v2f:focus{border-color:#6cb6ff;box-shadow:0 0 0 2px #6cb6ff33}
    textarea.h3d-v2f{min-height:44px;resize:vertical;line-height:1.55}
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
    /* 全面刷新：紧挨「输入修复」——两个都是"界面卡住了先点它"的兜底动作。
     * 它比状态条里那个旧的 ↻ 多做两件事：校正状态灯 + 无条件重挂三栏
     * （详见 refreshAll / syncRunLed）。 */
    const refreshBtn = el("button", "h3d-btn h3d-refresh", "↻ 刷新");
    refreshBtn.title = "全面刷新：重读节点 / 存档 / 素材库，并按后端真实状态校正右上角的状态灯"
        + "（报错或被中断后不再一直挂着「已提交队列」）。\n"
        + "不会取消正在跑的生成 —— 要取消请用页脚「✕ 终止」。";
    refreshBtn.onclick = () => { refreshAll(); };
    /* AI 优化设置也是**全链共用**的一套（服务商 / 模型 / 输出语言 / 规则文件），
     * 跟性能优化同级。以前它挂在每段卡里 —— 每段都长一个按钮，看着像"本段设置"，
     * 而且必须翻到某一段才点得到。放顶栏后位置固定，不用先找段。
     * 节点在打开时现取：顶栏是常驻 DOM，建台那一刻画布上未必已经有节点。 */
    const optBtn = el("button", "h3d-btn h3d-optbtn", "⚙ AI 优化设置");
    optBtn.title = "AI 提示词优化设置（服务商 / 模型 / 输出语言 / 规则文件）——"
        + "全链共用，不是某一段的设置";
    optBtn.onclick = () => {
        const n = findNode();
        if (!n) {
            alert("画布上没找到 H3 链节点：请先添加节点，或点「⚡ 一键载入配套工作流」。");
            return;
        }
        openOptSettings(n);
    };
    /* 性能优化是**机器级**设置（这台卡多大、内存多少），不属于某个项目、也不属于
     * 某一段，所以和上面两个兜底按钮一样放在顶栏；点开是独立弹窗（openPerfSettings）
     * —— 右栏那种窄折叠装不下「控件 + 整段说明」，说明会被挤没。 */
    const perfBtn = el("button", "h3d-btn", "⚡ 性能优化");
    perfBtn.title = "OOM 自救 / FFN 分块 / 成片内存 / 编码器 / 素材库索引等机器级设置"
        + "（全局，跨项目共用）";
    perfBtn.onclick = openPerfSettings;
    /* 27B 本地模型 + H3 采样轮流抢显存，谁后加载谁 OOM。兜底动作：把 ComfyUI
     * 驻留的模型全卸了再清缓存 —— OOM 之后点一下就能重跑，不用重启 ComfyUI。
     * 本地 LLM 句柄本来就是用完即卸，这里只管 torch 这边的驻留模型。
     * 生成中会被后端 423 拒掉（把正在用的模型卸了等于砍掉这次运行）。 */
    const vramBtn = el("button", "h3d-btn h3d-vrambtn", "🧹 显存清理");
    vramBtn.title = "OOM / 显存吃紧时点这个：卸载 ComfyUI 当前驻留的全部模型并清空缓存。"
        + "本地大模型优化完，先点一下再跑 H3，就不会被挤 OOM";
    vramBtn.onclick = async () => {
        if (vramBtn.disabled) return;
        if (!window.H3Api?.vramCleanup) { alert("h3_api.js 未更新（缺 vramCleanup），刷新页面试试"); return; }
        vramBtn.disabled = true;
        const old = vramBtn.textContent;
        vramBtn.textContent = "清理中…";
        try {
            const r = await window.H3Api.vramCleanup();
            if (!r.body?.ok) throw new Error(window.H3Api.errText(r, "清理失败"));
            vramBtn.textContent = "✓ 已清理";
            setTimeout(() => { vramBtn.textContent = old; }, 2000);
        } catch (e) {
            vramBtn.textContent = old;
            alert(e.message);
        } finally { vramBtn.disabled = false; }
    };
    const close = el("button", "h3d-close", "✕");
    close.title = "关闭导演台（Esc）";
    /* 分区渲染出错的可见出口：以前异常只进 console，用户看到的是"某个区空了"，
     * 无从判断是没数据还是代码挂了。这里把区名与错误一行摆到顶栏。 */
    const zoneErr = el("span", "h3d-zoneerr", "");
    zoneErr.style.display = "none";
    right.append(fixFocus, refreshBtn, optBtn, perfBtn, vramBtn, ledWrap, sub, zoneErr, close);
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
    mpBtn.title = "多段工作台：一个框管全片，用 [Segment N] 分段，框里就是直接进模型的提示词；"
        + "可一键洗成 H3 官方格式再分配回段落（扩写在段卡上做）";
    mpBtn.onclick = openMasterPromptModal;
    cHead.append(mpBtn);
    colC.append(cHead);
    const colR = el("aside", "h3d-col right");
    const rParams = el("section", "h3d-rsec h3d-psec");
    const rBridge = el("section", "h3d-rsec h3d-psec");
    const rUpscale = el("section", "h3d-rsec h3d-upsec");
    const rHist = el("section", "h3d-rsec h3d-hsec");
    colR.append(rParams, rBridge, rUpscale, rHist);
    stage.append(colL, colC, colR);

    /* 页脚 */
    const footer = el("footer", "h3d-footer");
    const footInfo = el("div", "h3d-footinfo", "");
    /* ✕ 终止：就是 ComfyUI 顶上那个「终止运行」搬进导演台（同一次 POST
     * /interrupt），省得为了取消一次生成先退出面板去外面找按钮。
     * 默认隐藏，renderFooter 按运行状态显隐。 */
    const stop = el("button", "h3d-btn h3d-stop", "✕ 终止");
    stop.title = "终止当前生成（等同 ComfyUI 的「终止运行」）：已生成完的段保留在存档，"
        + "未生成的段不跑。会中断 ComfyUI 当前正在跑的所有作业";
    stop.onclick = () => stopGeneration();
    const run = el("button", "h3d-btn h3d-btn-cta h3d-run", "▶ 开始生成");
    footer.append(footInfo, stop, run);

    page.append(topbar, banner, stage, footer);
    document.body.append(page);
    page.focus();

    desk = {
        page,
        zones: {
            project: sub,
            colC,
            lProj,
            rParams, rBridge, rUpscale, rHist,
            footInfo, run, stop,
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
                                               /* latent 设置进签名：切换项目/外部写入后卡片重建 */
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
        /* 全名 + 别名都进签名：只放别名的话，素材换了个全名（改名/重传）
         * 签名却不变 → 段卡不重绘 → 绿框继续指着旧名字。 */
        labels: refLabelsOf(ds?.ref_assets || []),
        mode: ds?.mode ?? "",
        /* 重摇标记/运行队列进签名：标记/取消/队列消费后待重摇徽章即时刷新 */
        redo: [(ds?.redo_segs || []).map((x) => `${x.slot}:${x.mode}`).join(","),
               (mf?.redo_queue || []).map((x) => (Array.isArray(x) ? x.join(":") : "")).join(",")],
        dur: String(getWidgetValue(data.node, W_DUR) ?? ""),
        merge: [mergeSel.on, (mergeSel.order || []).map((x) => x.id).join(","),
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

    /* 右栏：链参数（编辑中不重建）+ 语义桥 + 二采面板（编辑中不重建）+ 成片历史（播放中不重建） */
    const psig = paramsSig(node);
    const pgrid = z.rParams.querySelector(".h3d-params");
    if (!(pgrid && pgrid.contains(document.activeElement)) && z.rParams.dataset.sig !== psig) {
        z.rParams.dataset.sig = psig;
        runZone("链参数", () => renderParamsZone(z.rParams, data));
    }
    const bsig = bridgeSig(data);
    if (!z.rBridge.contains(document.activeElement) && z.rBridge.dataset.sig !== bsig) {
        z.rBridge.dataset.sig = bsig;
        runZone("语义桥", () => renderBridgeZone(z.rBridge, data));
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
            + "步数/CFG/采样器/门控/递减锚定等）一变就换新项目，不变则续用同一个。"
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
                /* 素材库改的是**项目 manifest**，而导演台的池子是画布 widget 里的
                 * 一份快照。这里必须**强制**重拉（hydratePool(true)）：非 force 会被
                 * "revision+签名没变"的节流挡掉，池子就停在改动之前 —— 期间任何一次
                 * persistPool（删素材 / 改标签）都会拿旧池整表覆盖 manifest["assets"]，
                 * 把刚上传的条目抹掉。这是「上传了但引用素材里不显示」的直接成因。 */
                onChanged: () => {
                    hydratePool(true).catch(() => {}).then(() => scheduleRefresh(60));
                },
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

/* 右栏可折叠面板：与 foldBox **共用同一份** _foldState（都是同一次会话内的界面偏好，
 * 不按项目分桶）。面板每次数据刷新都会被整块重建 —— 不记展开态的话，用户改一个设置
 * 面板就当场折叠回去（"默认折叠"只该管**首次打开**的样子）；关掉导演台再进来，
 * 展开态也要跟上一次一致。 */
function foldSection(id, defaultOpen, summaryHtml) {
    const det = el("details", "h3d-adv");
    det.open = (_foldState[id] !== undefined) ? !!_foldState[id] : !!defaultOpen;
    det.innerHTML = summaryHtml;
    det.addEventListener("toggle", () => { _foldState[id] = det.open; });
    return det;
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

/** 帧图进素材池并领一个「图片N」标注 —— 只在**被选为锚的这一刻**发，不是上传时。
 *
 * 为什么不在上传时发：帧图是从项目素材库里挑的、随时可换，"这张图叫什么"和
 * "它现在是不是锚"是两回事。跟着上传发会让池里堆满从没当过锚的号，而真正被
 * 选中的那张（老档 / 从项目 manifest 直选的）反而可能没号。
 *
 * 拿到标注后帧图就能：① 进本段 <Picture k> 编号（帧锚排在最前，素材顺延）
 * ② 在正文里被 @名字 引用 ③ 对齐指令里的 <Picture 1> 不再是"查无此图"的红框
 * （以前帧锚完全不进编号池，正文手写的 <Picture 1> 会和帧锚撞号）。 */
function ensureFrameAssetMark(node, file) {
    const f = String(file || "").trim();
    if (!f) return "";
    const ds = getDs(node);
    ds.ref_assets = Array.isArray(ds.ref_assets) ? ds.ref_assets : [];
    const inPool = ds.ref_assets.some((a) => a && String(a.file || "") === f);
    if (!inPool) {
        pushPoolAsset(node, f, frameNameOf(f));      // 内部已 assignMarks 发号
    } else if (!ds.ref_assets.every((a) => cleanMark(markTextOf(a)))) {
        ds.ref_assets = assignMarks(ds.ref_assets);  // 在池但没号：补发（合法的不动）
        setDs(node, ds);
    }
    const hit = getDs(node).ref_assets.find((a) => a && String(a.file || "") === f);
    return hit ? markTextOf(hit) : "";
}

function setSegmentFrameImg(node, idx, key, file) {
    const ds = getDs(node);
    if (idx < 0 || idx >= (ds.segments || []).length) return false;
    const seg = ds.segments[idx];
    const cur = (seg.frame_img && typeof seg.frame_img === "object") ? { ...seg.frame_img } : {};
    if (file) {
        cur[key] = String(file);
        /* 选为锚 = 进池领号（见 ensureFrameAssetMark）。
         * 这里**不**把图加进本段 refs —— 锚点不是"被正文引用的素材"，
         * 但它**占**一个 <Picture k> 编号（由 v2RefsFromSchedule 前置）。 */
        ensureFrameAssetMark(node, String(file));
    } else delete cur[key];
    seg.frame_img = (cur.first || cur.end) ? cur : null;
    setDs(node, ds);
    /* 对齐指令**不写进正文**：它是编译产物，实跑时由后端注入（见
     * stripAlignmentLines）。正文保持"用户视角的纯文本"。 */
    return true;
}

/* 官方对齐指令行的整行匹配（与后端 prompts._KF_RE 同口径） */
const KF_ALIGN_RE = /^(?:For the target video, at [0-9.]+ seconds into the target video, <(?:Picture|Video|Audio|Subject)\s+\d+> \(from \[Shot \d+\]\) is fully referenced\.|How the reference pictures align with the target video — .*mark of the target video\.)$/;

/* 提示词框的活编辑器登记表：程序改了 ds.prompts[idx] 之后必须**同时改 DOM**。
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
        _promptEditors.set(`${getDirValue(node)}|${node && node.id}:${idx}`, api);
    } catch (e) { /* 忽略 */ }
}
function syncPromptEditor(node, idx, text) {
    const api = _promptEditors.get(`${getDirValue(node)}|${node && node.id}:${idx}`);
    if (!api || !api.el || !api.el.isConnected) return false;
    try {
        if (api.value === text) return false;
        api.value = text;                       // 保留焦点与光标（setter 内部处理）
        if (typeof api.normalizeLoose === "function") api.normalizeLoose();
        return true;
    } catch (e) { return false; }
}

/** 把正文里的对齐指令行**剥掉** —— 正文只存"用户视角的纯文本"。
 *
 * 对齐指令是**编译产物**：实跑时由后端注入正文前（nodes.py L2VA_HEAD /
 * keyframe_line），前端不展示、不落盘。它一旦落进 ds.prompts 就会：
 * ① 跟实跑注入的那句重复；② 锚一变就成了指向不存在图片的陈旧指令；
 * ③ AI 扩写/优化把它当正文改写或抄走；④ 飘在字段前缀之前的那些根本清不掉
 * （老逻辑只处理 `integrated_...:` 之后的行，前缀之前的永远留着）。
 * 所以这里只做减法：认出官方对齐句整行（KF_ALIGN_RE）就删，其余一字不动。 */
function stripAlignmentLines(node, idx) {
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
                if (!m) {
                    /* 字段前缀**之前**的裸对齐行（粘贴/AI 带进来的）同样要摘 */
                    if (KF_ALIGN_RE.test(l.trim())) { sawAlign = true; continue; }
                    before.push(l);
                    continue;
                }
                prefix = m[1];
                const tail = m[2].trim();
                if (KF_ALIGN_RE.test(tail)) sawAlign = true;
                else if (tail) inline = tail;
                continue;
            }
            if (KF_ALIGN_RE.test(l.trim())) { sawAlign = true; continue; }
            body.push(l);
        }
        if (!sawAlign) return;                        // 本来就没有 → 不动
        const parts = [];
        if (inline) parts.push(inline);
        parts.push(...body);
        const head = prefix === null ? null
            : prefix + (parts.length ? " " + parts.join("\n") : "");
        const newText = (head === null ? before : before.concat(head)).join("\n")
            .replace(/^\n+/, "");
        if (newText !== text) {
            setPromptText(node, idx, newText);
            /* 写完状态还要把屏幕上的编辑器一起改掉：否则编辑器里仍是旧文本，
             * 用户一动键盘就把旧文本回写，刚摘掉的对齐行又冒出来。 */
            syncPromptEditor(node, idx, newText);
            scheduleRefresh(150);
        }
    } catch (e) { console.warn("[h3-director] stripAlignmentLines failed:", e); }
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

/* 「对齐指令预览」（原 mkAlignPreview / .h3d-alignprev）已整块删除：它只是
 * "提交时会自动往正文前拼什么"的一句只读预览，用户既不能改也不用看，却占着
 * 锚定栏一整行。对齐指令改由后端 nodes.py 注入，前端只管锚点本身。
 * v2InstrLines / segAlignSeconds / defaultV2Mode 仍被后端口径的其它入口使用，
 * 一并保留（tests/js/alignment_lines_check.js 锁着它们）。 */

/** 本段首尾帧锚在对齐指令里各占几号（首帧→1，FL2VA 尾帧→2；L2VA 只有尾帧且是 1 号）。
 *  与后端 prompts.compile_segment / 前端 v2InstrLines 同口径；无锚或 Ref2VA 返回 0。
 *  官方 I2VA/FL2VA/L2VA 段按定义**不会同时带参考素材**（有参考就是 Ref2VA，
 *  Ref2VA 不生成对齐句），所以这里不会出现编号被别的素材占掉的情况。 */
function framePictureNumbers(ds, segIdx) {
    try {
        const mode = defaultV2Mode(ds, segIdx);
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

/** 把一份已落盘的素材补进节点素材池（幂等，按 file 去重）。
 *  必须补：ds.ref_assets 是 persistPool 写回 manifest["assets"] 的**唯一来源**，
 *  上传完不补进去，下一次任何"写资产清单"的动作都会按旧池整表覆盖，
 *  于是"刚传的图没了 / 传一张顶掉上一张"。
 *  kind 由调用方给（缺省 image）：素材池按 kind 分号（图片N/视频N/音频N）与分
 *  9/3/3 限额，标错类会让引用条与编译一起错位。 */
function pushPoolAsset(node, file, label, kind = "image") {
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
    /* 标注（图片1 / 视频1 / 音频1）：按现有池子顺延编号，不回收旧号 */
    const withMark = assignMarks([...ds.ref_assets, {
        file: f, kind, label: lbl.slice(0, 24), asset_id: "", roles: [],
        /* 引用名取落盘文件名的 basename（含后缀）——它就是用户写进提示词的名字 */
        ref_name: cleanRefName(f) || lbl,
    }]);
    ds.ref_assets = withMark;
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
        const mf = await fetchManifest(dir);
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
                        kind: "image", alias, dest: "project", link_dir: dir,
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
    /* 合并模式：到**素材库**点选素材，点击顺序即合并顺序（纯内存清单，不动链与存档）。
     *  **常显** —— 以前被 `done > 0` 包着，一段都没生成时根本看不到这个入口，而
     *  "把已有素材拼一条片子"本来就该是随时可用的独立动作。 */
    {
        const nSel = mergeCount();
        const mergeBtn = el("button", "h3d-btn" + (mergeSel.on ? " h3d-btn-cyan" : ""),
            mergeSel.on ? `⧉ 合并模式·开${nSel ? `（${nSel}）` : ""}` : "⧉ 合并模式");
        mergeBtn.title = "开启后到素材库里点选要合并的素材（**点击顺序 = 合并顺序**），"
            + "可再追加外部视频，按 1→N 流式拼接成 merged_*.mp4 导出到项目文件夹；"
            + "不动链、不动存档，随时可退出（退出即清空清单）";
        mergeBtn.onclick = () => {
            mergeSel.on = !mergeSel.on;
            if (!mergeSel.on) resetMergeSel();
            scheduleRefresh(0);
        };
        bar.append(mergeBtn);
    }
    wrap.append(bar);
    /* 状态条上那个 ↻ 已挪到顶栏「⌨ 输入修复」旁边并升级成 refreshAll ——
     * 摆在这儿容易被当成"只刷这一条状态"，而用户点刷新想要的是全局重刷。 */

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

    /* 合并清单条：素材（按 1→N 顺序）+ 外部上传 + 导出/退出。
     * 清单内容全在**素材库**里点选（本条的「从素材库选」是唯一入口），
     * 这里只回显顺序与总数 —— 段卡上那套"逐段切屏勾选"已删。 */
    if (mergeSel.on) {
        const mbar = el("div", "h3d-mergebar");
        const order = mergeSel.order || [];
        const fileSum = (mergeSel.files || []).length
            ? ` ＋ 外部视频×${mergeSel.files.length}` : "";
        const sum = el("div", "h3d-merge-sum",
            order.length
                ? `合并清单（按选择顺序）：<b>${order.map((x, i) => `${i + 1}. ${escapeHtml(x.name || x.file || x.id)}`).join("　→　")}</b>${escapeHtml(fileSum)}`
                : `<b>还没选素材</b>：点右边「🗂 从素材库选」挑要拼接的素材（点击顺序就是合并顺序）`);
        mbar.append(sum);
        const pickBtn = el("button", "h3d-btn h3d-btn-cyan", "🗂 从素材库选");
        pickBtn.title = "打开素材库（合并模式）：点一个素材就按顺序排进去，"
            + "瓦片角标 1/2/3/4 就是合并顺序；再点一次取消";
        pickBtn.onclick = () => openMergeLibrary();
        const upBtn = el("button", "h3d-btn", "＋ 上传视频");
        upBtn.title = "上传外部视频追加到合并清单末尾（input 目录；建议 24fps、画幅与项目一致，"
            + "不同画幅会自动缩放裁剪到项目画幅）";
        upBtn.onclick = pickMergeVideo;
        const goBtn = el("button", "h3d-btn h3d-btn-cta", "⧉ 合并导出");
        goBtn.title = "按清单顺序（1→N）流式拼接为 merged_*.mp4"
            + "（PyAV 编码，分钟级耗时，期间界面可用）";
        goBtn.disabled = !mergeCount();
        goBtn.onclick = () => doMergeExport(goBtn);
        const exitBtn = el("button", "h3d-btn", "✕ 退出");
        exitBtn.title = "退出合并模式并清空清单（不影响链与存档）";
        exitBtn.onclick = () => {
            mergeSel.on = false;
            resetMergeSel();
            scheduleRefresh(0);
        };
        mbar.append(pickBtn, upBtn, goBtn, exitBtn);
        wrap.append(mbar);
    }

    /* 未填写提示 */
    if (drafts && drafts.length) {
        wrap.append(el("div", "h3d-drafts",
            `未填写（运行时跳过）：${drafts.map(escapeHtml).join("、")}`));
    }

    /* 段落卡片 */
    wrap.append(buildCardsSafe(data));

    /* 添加行：只留「一键载入配套工作流」这一个兜底（画布上没节点时）。
     * 「＋ 添加一段」已删 —— 加段的入口是顶部横向选段条右侧的 ＋，两个按钮做
     * 同一件事只会让人分不清该点哪个（而且这个还藏在卡片下方要滚到底才看见）。 */
    const addrow = el("div", "h3d-addrow");
    if (node) {
        /* 有节点时这一行就没有内容了：加段走选段条，不再重复摆一个按钮。 */
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


/* ---- 横向分段选择条：pill 先压缩、极限后横滑；pill 可拖动调序 ----
 *  点选 pill 只切内存选中（_selSeg），不落 ds 不触发重做；
 *  拖动 pill 走同一条 applyReorder 路径（含范围清重摇/二采）。 */
function renderSegStrip(data) {
    const { node, state, mf, plan } = data;
    const strip = el("div", "h3d-segstrip");
    const off = planOff(mf);
    const done = Math.max(0, (mf?.done ?? state?.done ?? 0) - off);
    const sel = clampSelSeg(plan.length);
    /* 拖拽调序不再被合并模式挡住：合并顺序现在由素材库的选择顺序决定，跟链顺序
     * 已经解耦，拖 pill 只改链、不影响合并清单。 */
    const canDrag = !!(node && plan.length > 1);
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
        /* 段卡上**不再有**合并勾选框：合并清单改在素材库里点选（点击顺序即合并
         * 顺序），不再需要"逐段切屏勾选"。段卡只负责链本身。 */

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
         * 以前这两个按钮挂在结果框的引用条里，可锚点是段级运行参数
         * （节点按它注入 keyframe latent），跟"结果框引用了哪些素材"根本不是一回事，
         * 摆在那儿会让人以为它只作用于结果框。一个段只有一组锚，放在外框才对。 */
        if (node && it.idx !== undefined) {
            const anchorBar = el("div", "h3d-anchorbar");
            anchorBar.append(el("label", "", "首尾帧锚定"));
            anchorBar.append(mkFrameBtns(node, it.idx, () => scheduleRefresh(240)));
            anchorBar.append(el("span", "h3d-secs-hint",
                "本段的起手帧 / 终点锚（段级，三栏共用）；选为锚即领一个「图片N」编号，"
                + "占本段 <Picture k> 最前，参考素材顺延"));
            /* 对齐指令**不再在前端露出**（原 mkAlignPreview 已删）：它从来只是
             * "提交时会自动往正文前拼什么"的一句预览，用户既不能改也不需要看，
             * 占着锚定栏一整行。现在统一由后端 nodes.py 注入，前端只管锚点本身。 */
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
                /* 引用名表读**活的**节点状态，不用 collectData 的快照：上传/改名/删除
                 * 素材后快照会停在建卡那一刻，新素材的 @名字 就永远渲染不成绿框。
                 * 用 refLabelsOf 而不是 `.map(a => a.label)`：表里必须**同时有全名和
                 * 别名**，只给别名就会重现「@猫.png 只认出 猫、.png 溢出成正文」。 */
                labels: () => {
                    let arr = data.ds.ref_assets;
                    if (node) { try { arr = getDs(node).ref_assets; } catch (e) { /* 回落快照 */ } }
                    return refLabelsOf(arr);
                },
                /* 名字 -> 素材信息 {kind,file,asset_id,mark}（绿框据此挑缩略图/图标）。
                 * 同样读活的节点状态——上传/换类别后旧绿框的标识能立刻对得上。
                 * 全名与别名指向同一份信息，否则按全名插进来的绿框会没有缩略图。 */
                assets: () => {
                    let arr = data.ds.ref_assets;
                    if (node) { try { arr = getDs(node).ref_assets; } catch (e) { /* 回落快照 */ } }
                    return refInfoMapOf(arr);
                },
                /* 官方标签 → 素材别名：{"<Picture 1>": "回廊场景", ...}。
                 * 外部 agent 写的官方格式文本里是 `<Picture N>`，靠它渲染成缩略图，
                 * 让用户直接看见"Picture 几"是哪张图（顺序挂错一眼可见）；
                 * 挂不到素材的标红警示。序列化时原样还原，不改写正文。
                 * 与 v2RefsFromSchedule 同口径 —— 那正是后端 _kind_tokens 的编号规则。 */
                tokenMap: () => {
                    const out = {};
                    let dsNow = data.ds;
                    if (node) { try { dsNow = getDs(node); } catch (e) { /* 回落快照 */ } }
                    for (const r of v2RefsFromSchedule(dsNow, it.idx)) {
                        out[r.label] = String(r.src || "");
                    }
                    return out;
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
            /* 两页容器：主框 / 锚定设置（时长在标题行，锚定设置页放
             * 手动锚定 + 段级开关 + latent 保存）。页名就叫「锚定设置」——
             * 这一页现在的主角是手动锚定，叫「设置」看不出进去能改什么。 */
            const tabbar = el("div", "h3d-tabs");
            const paneMain = el("div", "h3d-tabpane");
            const paneSet = el("div", "h3d-tabpane");
            /* 页签记忆同样带项目目录：不带的话，切项目后这一段的页签会停在
             * 上一个项目选的那一页（纯界面状态，但同样是"跨项目串味"）。 */
            const tabKey = `${getDirValue(node) || ""}|seg${it.idx}`;
            let curTab = _segTab.get(tabKey) || "main";
            const tabs = [["main", "提示词"], ["set", "锚定设置"]];
            /* 「具象化」不再是独立 tab：三框合一后它只是**同一份提示词的另一种
             * 写法**（结构化分组），改走工具条「⇄ 结构化提示词」弹窗。
             * 旧存档的 tab 值（"v2" 等）一律回落 main —— 只挡 "v2" 一个的话，
             * 换过页名的旧值会把**所有** pane 都藏掉（段卡整块开天窗）。 */
            const tabKeys = tabs.map(([k]) => k);
            if (!tabKeys.includes(curTab)) curTab = "main";
            const panes = { main: paneMain, set: paneSet };
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
            /* 段级两个开关（跳过自动引用上段 / 跳过自动按序生成）也放**三页之外**的
             * 公共区：它们既不是「提示词的引用」，也不是「锚定设置」页里的锚点参数，
             * 而是"本段是否参与链路"的开关 —— 藏进页签里就要翻页才看得见。
             * 容器先挂上，内容在下方 seg 快照就绪处填（那里才有本段状态）。 */
            /* 两个跳过开关挂在**页签行里**（提示词 / 锚定设置 同一栏，靠右）：
             * 它们以前单独占一行，把段卡撑高一大截，而这两个勾选项跟页签同属
             * "这一段怎么跑"的开关组，并成一栏更顺手。
             * 容器先挂上，内容在下方 seg 快照就绪处填（那里才有本段状态）。 */
            const pubSwitchRow = el("div", "h3d-setrow h3d-pubswitch");
            tabbar.append(pubSwitchRow);
            /* 「⚙ AI 优化设置」已挪到顶栏（与 ⚡ 性能优化 同级）：它是全链共用的一套，
             * 挂在段卡里每段长一个按钮，看着像本段设置。 */
            /* 三栏各自独立的引用条：意图 / 剧本 / 结果 各一条，谁也不改谁。
             * 唯一的引用条直接进模型（ds.prompts + seg.refs，走官方 9/3/3）；
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
            /* 段卡只有一个提示词框（三框合一后的形态），框里的内容就是最终进模型的
             * ds.prompts[idx]；想从一句大白话起步就点「✨ AI扩写+优化」。 */
            const segNow = (data.ds.segments || [])[it.idx] || {};
            const canEdit = !!node && it.idx !== undefined;
            /* 别名表读**活的**节点状态（与结果框同口径），@补全与绿框才跟得上素材变动。
             * 表里放的是**引用键**（ref_name 含后缀）——正文里写的就是它。 */
            const liveLabels = () => {
                let arr = data.ds.ref_assets;
                if (node) { try { arr = getDs(node).ref_assets; } catch (e) { /* 回落快照 */ } }
                return (arr || []).map(refKeyOf).filter(Boolean);
            };
            const liveAssets = () => {
                let arr = data.ds.ref_assets;
                if (node) { try { arr = getDs(node).ref_assets; } catch (e) { /* 回落快照 */ } }
                const m = {};
                for (const a of (arr || [])) {
                    const k = refKeyOf(a);
                    if (k) {
                        m[k] = { kind: a.kind || "image", file: String(a.file || ""),
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

            /* ---- 提示词：唯一一框（三框合一），直接进模型 ----
             * 旧版是「意图 / 剧本 / 结果」三层：中间两层只活在 widget（切项目
             * 就丢，见 flushPrompts 白名单），而且两个框都写着"提示词"，没人分得清
             * 哪个进模型。现在只有一个框：
             *   · 直接写结果 → 直接生成；
             *   · 写一句大白话意图 → 点「AI扩写+优化」一步到位（扩写+格式优化，不弹窗）。
             * 旧档迁移见 getDs（正文为空时用 script 顶上，intent_zh 丢弃）。 */
            const pResult = mkPane("提示词（最终进模型）", true);
            pResult.body.append(ta.el || ta);
            /* 引用条（唯一一条）：gate=true → 走官方 9/3/3、写 seg.refs */
            const refResult = mkRefBar({
                title: "引用素材",
                editor: canEdit ? ta : null,
                readRefs: () => refsFromText(String(ta.value || ""), poolNow()),
                commit: (ed) => applyPromptEdit(node, it.idx, ed),
                /* showFrames 已关：首/尾帧锚定在卡片公共区（框之上），
                 * 它是段级运行参数，不属于"提示词的引用"。 */
                gate: true, showFrames: false, tplKey: it.idx,
            });
            if (refResult) pResult.body.append(refResult);
            /* AI 工具条：扩写+优化（一步到位，**不弹窗**）/ 单提示词优化。
             * 两个按钮都只写本框；写入前自动存原稿（「原稿」按钮可还原）。 */
            const aiBar = el("div", "h3d-actions");
            const bExpandOpt = el("button", "h3d-btn h3d-btn-cyan", "✨ AI扩写+优化");
            bExpandOpt.type = "button";
            bExpandOpt.title = "把框里的内容当意图：AI 先扩写成剧本，再按 H3 官方格式优化，"
                + "结果直接写回本框（原稿自动存一份，点「原稿」可还原）。"
                + "参数在「⚙ AI 优化设置 → AI 扩写优化设置」里调";
            bExpandOpt.disabled = !canEdit;
            bExpandOpt.onclick = () => runExpandOptimizeForSegment(node, it.idx, ta, { btn: bExpandOpt });
            const bOptRun = el("button", "h3d-btn h3d-btn-cyan", "✨ 提示词优化");
            bOptRun.type = "button";
            bOptRun.title = "只做格式规范化：把框里的内容按 H3 官方格式重写，不改你的意思";
            bOptRun.disabled = !canEdit;
            bOptRun.onclick = () => runOptForSegment(node, it.idx, ta, { btn: bOptRun });
            /* 「解析引用」：把正文里的素材名对应到素材库 —— 别名/标注统一写成
             * 完整文件名（含后缀），并渲染成绿框。
             * **只在这里手动触发**：打字和失焦都不再自动改文本（自动改会打断
             * 中文输入法组合，也会让人觉得"我打的字被改了"）。
             * 典型用法是从别处复制一大段提示词粘进来，点它一次把引用认全。 */
            const bResolve = el("button", "h3d-btn", "🔗 解析引用");
            bResolve.type = "button";
            bResolve.title = "把正文里的素材名对应到素材库：别名 / 标注统一写成完整文件名"
                + "（含后缀），认不出来的名字会点名提示。\n"
                + "从别处复制来的提示词粘进来后点它一次即可。";
            bResolve.disabled = !canEdit;
            bResolve.onclick = () => {
                if (!ta.resolveRefs) { setLed("idle", "本框不支持解析"); return; }
                let r = null;
                try {
                    r = ta.resolveRefs();
                } catch (e) {
                    setLed("err", `解析失败：${e && e.message ? e.message : e}`);
                    return;
                }
                if (!r) { setLed("idle", "解析无结果"); return; }
                if (r.unknown && r.unknown.length) {
                    /* 认不出必须点名：静默跳过的话，用户只会看到"解析了没反应"，
                     * 而出片时那张图根本不会挂上。 */
                    setLed("warn", `${r.unknown.length} 个名字在素材库里找不到：`
                        + r.unknown.slice(0, 6).join("、")
                        + (r.unknown.length > 6 ? " …" : ""));
                    alert("这些名字在素材库里对不上（不会挂上素材）：\n- "
                        + r.unknown.join("\n- ")
                        + "\n\n请核对素材名，或先把素材加进项目库。");
                } else if (r.hits) {
                    setLed("done", `已解析 ${r.hits} 处引用`
                        + (r.changed ? "（名字已统一成完整文件名）" : ""));
                } else {
                    setLed("idle", "正文里没有 @引用");
                }
                scheduleRefresh(60);
            };
            /* ---- ⬆ 上传素材：与素材库「上传到项目资产」**同一条通道**
             * （H3Assets.uploadDirect + dest=project + link_dir=项目目录）。
             * 上传即入池：ds.ref_assets 是写回 manifest["assets"] 的唯一来源，
             * 不补进去，后面任何一次写清单都会按旧池整表覆盖，把刚传的抹掉。 */
            const bUpload = el("button", "h3d-btn", "⬆ 上传素材");
            bUpload.type = "button";
            bUpload.disabled = !canEdit;
            bUpload.title = "上传图片 / 视频 / 音频到本项目 assets/ 并登记进清单——"
                + "与素材库「上传到项目资产」同一条通道（只落项目一份，不往全局库复制）。\n"
                + "传完即可在正文里写 @别名 引用；要跨项目复用请去素材库点「存入全局库」。";
            bUpload.onclick = () => {
                if (!node || it.idx === undefined) {
                    alert("画布上没找到导演台节点，无法上传素材");
                    return;
                }
                const dir = getDirValue(node);
                if (!dir) {
                    alert("当前还没有项目：请先在左栏读档或新建一条项目，再上传素材。");
                    return;
                }
                const H3Assets = window.H3Assets;
                if (!H3Assets?.uploadDirect) {
                    alert("上传接口不可用（请更新插件并重启 ComfyUI）");
                    return;
                }
                /* input **必须真的挂进文档**再 click()：未挂载的 file input 调 click()
                 * 在部分 WebView / 桌面壳里不会弹选择框，表现就是「点了完全没反应」。
                 * 选中或取消后立刻移除，不留 DOM 垃圾。 */
                const inp = document.createElement("input");
                inp.type = "file";
                inp.multiple = true;
                inp.style.cssText = "position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0";
                document.body.append(inp);
                const drop = () => { try { inp.remove(); } catch (e) { /* 已移除 */ } };
                inp.oncancel = () => { drop(); setLed("idle", "已取消选择素材"); };
                inp.onchange = async () => {
                    const files = [...(inp.files || [])];
                    if (!files.length) { drop(); return; }
                    bUpload.disabled = true;
                    const oldTxt = bUpload.textContent;
                    bUpload.textContent = `上传中…（${files.length}）`;
                    setLed("running", `正在上传 ${files.length} 个素材…`);
                    let ok = 0;
                    const failed = [];
                    for (const f of files) {
                        try {
                            const kind = H3Assets.guessKind(f);
                            const res = await H3Assets.uploadDirect(f, {
                                kind, dest: "project", link_dir: dir,
                            });
                            if (!res?.ok) throw new Error("上传返回异常");
                            const file = res?.mirrored?.file || res?.stored?.file || "";
                            if (!file) throw new Error(res?.mirror_error || "上传后没拿到项目内路径");
                            const rel = String(file).startsWith("assets/")
                                ? file : `assets/${String(file).split("/").pop()}`;
                            pushPoolAsset(node, rel, res?.stored?.label || "", kind);
                            ok++;
                        } catch (e) { failed.push(`${f.name}（${e?.message || e}）`); }
                    }
                    drop();
                    bUpload.disabled = false;
                    bUpload.textContent = oldTxt;
                    /* 失败必须显性报出来：只改顶上那盏小灯的话，用户会以为"点了没反应"。 */
                    if (!ok) {
                        setLed("err", `上传失败：${failed.slice(0, 3).join("、")}`);
                        alert(`上传失败（${failed.length} 个）：\n- ` + failed.join("\n- "));
                        return;
                    }
                    /* 立刻按最新 manifest 重建池并刷新（不等 240ms 轮询）：
                     * 否则出现「传了没反应、切一下屏又好了」。 */
                    try { await hydratePool(true); } catch (e) { /* 忽略 */ }
                    setLed(failed.length ? "warn" : "done",
                        `已上传 ${ok} 个到项目 assets/`
                        + (failed.length ? `；${failed.length} 个失败：${failed.slice(0, 3).join("、")}` : "")
                        + "——正文里写 @别名 即可引用");
                    if (failed.length) alert("部分文件上传失败：\n- " + failed.join("\n- "));
                    scheduleRefresh(120);
                };
                inp.click();
                setLed("idle", "已打开文件选择框：选中素材即上传到本项目 assets/");
            };
            aiBar.append(bExpandOpt, bOptRun, bResolve, bUpload);
            pResult.body.append(aiBar);
            /* 工具条：原稿切换 / 结构化弹窗（paintOptbar 填）。
             * 挂在 paneMain 上、pResult.body 之外 —— 放进结果框里的话
             * 一折叠就跟着一起藏了。 */
            const optbar = el("div", "h3d-actions");
            optbar.dataset.optbar = String(it.idx);
            paneMain.append(pResult.g, optbar);
            try {
                paintOptbar(optbar, node, data, it.idx, ta);
            } catch (e) {}

            /* 引用调度已迁入素材调度组（分段素材调度，933 上限）；引用语模板已并入上方统一引用条 */

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

                /* 两个开关并成一行，落在**卡片外框的公共区**（pubSwitchRow，见上方
                 * body.append(tabbar) 处）—— 它们不属于任何一页。语义取**反向勾选**
                 * （用户拍板）：勾选 = 跳过，不勾 = 跟随全局默认开——「跟随全局」
                 * 从来不是一个可点按钮该表达的状态，它只是"没勾"本身；旧版勾选框
                 * （自动引用上段）+旁置按钮的三件套让人分不清勾的到底是"开"还是"跟全局"。
                 * 数据仍是三态：不勾写 null（跟随全局），勾写显式 false（跳过）。 */
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
                pubSwitchRow.append(refRow, seqRow);

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
            paintTabs();
            body.append(paneMain, paneSet);
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
 * 基础设置 = 原生工作流画幅/时长/采样类 + 链路存档策略（审片/自动保存/自动成片）
 * + 参考图像尺寸；视频延续 = 段间引导相关（大折叠套两子折叠）。
 * 「存档目录」「重跑起始段」不再在导演台露出（项目与续跑由导演台自己管）——
 * 节点控件与后端逻辑照旧保留，只是不占界面。
 * ADVANCED_DEFS 保持全量（paramsSig/旧逻辑兼容口径），新增控件都要登记进去，
 * 否则改它不会触发面板重建。 */
const BASIC_DEFS = [W_AR, W_MP, W_DUR, W_SEED, "步数", "CFG", "采样器", "调度器",
    "审片模式", "自动保存", "自动成片", "参考图像尺寸", W_WIDTH, W_HEIGHT];
const KEYFRAME_DEFS = ["引导帧数", "锚定加噪", "递减锚定", "响度对齐强度"];
const SEAM_DEFS = ["桥帧门控", "清晰度阈值", "回退上限",
    "接缝重摇", "重摇阈值", "重摇上限"];
const PRIMARY_DEFS = [W_AR, W_MP, W_DUR, W_SEED, "步数"];
const ADVANCED_DEFS = [
    "引导帧数", "CFG", "采样器", "调度器",
    "审片模式", "自动保存", "自动成片", "参考图像尺寸", "响度对齐强度",
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
    /* —— 基础设置：**首次打开默认展开**的一栏（其余栏与子面板首次一律收起）；
     * 之后一律跟随用户的折叠操作（见 foldSection）。 —— */
    const basic = foldSection("param-basic", true,
        "<summary>📐 基础设置（分辨率 / 时长 / 采样器 / 存档与成片）</summary>");
    const bgrid = el("div", "h3d-adv-grid");
    for (const name of BASIC_DEFS) {
        if (name === W_WIDTH || name === W_HEIGHT) {
            if (ar !== "自定义") continue;
            bgrid.append(renderWidgetField(node, name));
            continue;
        }
        bgrid.append(renderWidgetField(node, name, PARAM_LABELS[name]));
    }
    /* 徽章：自定义画幅也要显示（说清「百万像素不参与」），见 canvasBadgeText 注释 */
    const badgeTxt = canvasBadgeText(node);
    if (badgeTxt) {
        const isCustom = !AR_RATIO[ar];
        const b = el("div", "h3d-convbadge",
            isCustom ? escapeHtml(badgeTxt) : `${escapeHtml(badgeTxt)} · 32倍数对齐`);
        b.title = isCustom
            ? "宽高比=自定义：本次画幅直接用「宽度/高度」两个控件，「百万像素」不参与换算"
            : "官方 Resolution Selector 同款换算：1MP=1024×1024，两侧各自 round 对齐 32 倍数；"
              + "此模式下「宽度/高度」控件被换算覆盖，改它们不生效";
        bgrid.append(b);
    }
    basic.append(bgrid);
    sec.append(basic);
    /* —— 视频延续（大折叠套两子折叠） —— */
    const cont = foldSection("param-cont", false,
        "<summary>🔗 视频延续（段间引导 / 关键帧 / 接缝）</summary>");
    const cwrap = el("div", "h3d-adv-grid");
    const kf = foldSection("param-kf", false,
        "<summary>📌 关键帧设置（尾部保存 / 注入帧数 / 加噪 / 响度对齐）</summary>");
    const kgrid = el("div", "h3d-adv-grid");
    for (const name of KEYFRAME_DEFS) kgrid.append(renderWidgetField(node, name));
    kgrid.append(el("div", "h3d-hint",
        "尾部 latent 保存量与分段注入帧数在各段「锚定设置」里按段覆盖（分段优先，空=跟随此处）。"));
    kf.append(kgrid);
    const seam = foldSection("param-seam", false,
        "<summary>🩺 检测重摇（桥帧门控 / 接缝重摇）</summary>");
    const sgrid = el("div", "h3d-adv-grid");
    for (const name of SEAM_DEFS) sgrid.append(renderWidgetField(node, name));
    seam.append(sgrid);
    cwrap.append(kf, seam);
    cont.append(cwrap);
    sec.append(cont);
    sec.append(el("div", "h3d-foot",
        "「锚定设置」里同名子选项按段覆盖此处（分段优先）；「生成模式」由左侧模式条控制。"));
}

/* 二采/链参数面板局部重渲：提交型编辑当场重建，修「编辑后界面不动」。
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

/* ---- 语义桥（右栏独立一栏：自己的签名、自己的渲染主人） ----
 *
 * 为什么独立成栏、而不是塞进「链参数」：那一栏的签名来自**画布控件值**
 * （paramsSig(node)），而语义桥整份配置住在 ds 里 —— 塞进去就只有改画布控件时
 * 才会重渲，面板会一直停在旧值上。独立一栏 + 自己的 bridgeSig 才符合
 * 「一块 UI 只能有一个渲染主人」。
 *
 * 配置真源是 ds.bridge（逐项目）；权重列表走 /h3chain/bridge_models（后端扫插件 models/）。 */

/* 作用范围（与后端 semantic_bridge.SCOPES 同表，别各写一套）：
 *   all  = 全量过桥（参考图/首帧图的视觉 token 一起改，作者原版行为）
 *   text = 仅文本过桥（视觉 token 逐字节保留，素材保真优先） */
const BRIDGE_SCOPES = ["all", "text"];
const BRIDGE_SCOPE_LABELS = { all: "全量过桥", text: "仅文本过桥" };

function bridgeSig(data) {
    const b = data.ds?.bridge || {};
    return JSON.stringify([
        b.enabled === true, b.adapter ?? "", b.alpha ?? 0, b.scope ?? "all",
        (data.bridgeModels || []).join(","),
    ]);
}

/** 语义桥配置写回（提交型编辑当场重建，理由同 repaintUpscale）。 */
function setBridge(node, patch) {
    const ds = getDs(node);
    Object.assign(ds.bridge, patch);
    setDs(node, ds);
    scheduleRefresh(60);
    repaintBridge();
}

function repaintBridge() {
    if (!desk || !desk.zones || !desk.zones.rBridge) return;
    try {
        const data = Object.assign({}, desk.lastData, { node: findNode() });
        data.ds = data.node ? getDs(data.node) : (data.ds || {});
        desk.zones.rBridge.dataset.sig = bridgeSig(data);
        renderBridgeZone(desk.zones.rBridge, data);
    } catch (e) {
        console.warn("[h3-director] repaintBridge failed:", e);
    }
}

function renderBridgeZone(sec, data) {
    const { node } = data;
    const b = data.ds?.bridge || defaultBridge();
    const models = data.bridgeModels || [];
    sec.replaceChildren();
    const det = foldSection("bridge-top", false,
        "<summary>🌉 语义桥（条件增强）"
        + (b.enabled ? ' <span class="h3d-chip cyan">已开启</span>' : "") + "</summary>");
    const body = el("div", "h3d-adv-grid");
    det.append(body);
    sec.append(det);
    if (!node) {
        body.append(el("div", "h3d-empty", "画布上未找到节点，语义桥面板不可用"));
        return;
    }
    /* 关闭时把下面几行整体置灰（**不隐藏**）：让「功能在、只是没开」一眼可见，
     * 也免得开关一勾整栏跳版。 */
    const dim = [];
    const gate = () => dim.forEach((f) => { f.style.opacity = b.enabled ? "" : "0.45"; });

    const enField = el("div", "h3d-param");
    enField.append(el("label", "", "语义桥"));
    const enRow = el("div", "h3d-seedrow");
    const enCb = document.createElement("input");
    enCb.type = "checkbox";
    enCb.checked = b.enabled;
    enCb.title = "开启后每段 cond 都过一次小网络（5120→512→512→5120），把"
        + "「谁在做什么 / 道具归属 / 谁攻击谁 / 遮挡后的身份与状态」这类关系表达推清楚；"
        + "它不是动作修复工具，也不改写提示词。"
        + "开启会进整链指纹：改开关 / 强度 / 范围 / 权重都从第 1 段整链重做"
        + "（cond 变了，只重做一半会做出「前几段带桥、后几段不带」的半条链）。"
        + "关闭时后端一个张量都不碰，连权重都不加载。";
    enCb.onchange = () => {
        /* 勾上时若还没选权重、而 models/ 里又有文件，顺手选第一个 ——
         * 否则一开启就是「权重名为空」的报错，白让用户撞一次墙。 */
        const patch = { enabled: enCb.checked };
        if (enCb.checked && !b.adapter && models.length) patch.adapter = models[0];
        setBridge(node, patch);
    };
    enRow.append(enCb);
    enField.append(enRow);
    body.append(enField);

    const alField = el("div", "h3d-param");
    alField.append(el("label", "", "强度 alpha"));
    const alRow = el("div", "h3d-seedrow");
    const alInp = document.createElement("input");
    alInp.type = "number";
    alInp.value = b.alpha;
    alInp.min = "0";
    alInp.max = "1";
    alInp.step = "0.05";
    alInp.title = "桥的混合比例：out = 原值 + alpha ×（过桥值 − 原值）。"
        + "作者推荐 0.10~0.15 起步，0 = 等于不启用。"
        + "强度不是越高越好 —— 他自述约 10% 的案例开启后反而出现新错误，0.2 以上画面细节会变软。"
        + "请固定同一个 seed 做对照，别凭一次结果下结论。";
    alInp.addEventListener("wheel", (e) => e.preventDefault(), { passive: false });
    alInp.onchange = () => {
        const n = Number(alInp.value);
        if (isFinite(n)) setBridge(node, { alpha: Math.min(1, Math.max(0, n)) });
    };
    alRow.append(alInp);
    alField.append(alRow);
    body.append(alField);

    const scField = el("div", "h3d-param");
    scField.append(el("label", "", "作用范围"));
    const scSel = document.createElement("select");
    scSel.className = "h3d-select";
    scSel.title = "cond 张量里同时混着文本 token 与视觉 token —— H3 不走 chat template，"
        + "参考图/首帧图是以 vision block 形式拼进同一个序列的。"
        + "「全量过桥」= 两类都改（作者原版行为）；"
        + "「仅文本过桥」= 视觉 token 逐字节保留，参考图/首帧图的原始表达不受影响。"
        + "注意：尾帧图、每段尾帧锚定、头锚、手动锚只做 VAE latent，不进这个张量，"
        + "语义桥碰不到它们 —— 别把它理解成「连 latent 锚一起增强」。";
    for (const sc of BRIDGE_SCOPES) {
        const o = document.createElement("option");
        o.value = sc;
        o.textContent = BRIDGE_SCOPE_LABELS[sc];
        if (sc === b.scope) o.selected = true;
        scSel.append(o);
    }
    scSel.onchange = () => setBridge(node, { scope: scSel.value });
    scField.append(scSel);
    body.append(scField);

    const mdField = el("div", "h3d-param");
    mdField.append(el("label", "", "权重文件"));
    const mdSel = document.createElement("select");
    mdSel.className = "h3d-select";
    mdSel.title = "本插件 models/ 目录下的语义桥权重（.safetensors）。"
        + "列表为空 = 该目录下没有权重文件，请先放一个进去再开启。";
    if (models.length) {
        for (const m of models) {
            const o = document.createElement("option");
            o.value = m;
            o.textContent = m;
            if (m === b.adapter) o.selected = true;
            mdSel.append(o);
        }
    } else {
        const o = document.createElement("option");
        o.value = "";
        o.textContent = "models/ 下无权重";
        mdSel.append(o);
        mdSel.disabled = true;
    }
    mdSel.onchange = () => setBridge(node, { adapter: mdSel.value });
    mdField.append(mdSel);
    body.append(mdField);

    dim.push(alField, scField, mdField);
    gate();

    const foot = el("div", "h3d-hint",
        b.enabled
            ? "已开启：每段一采与二采都会过一次语义桥（同一份配置、同一个位置）。"
                + "改这里的任何一项都会让整链重做。"
            : (models.length
                ? "未开启：后端不加载权重、不改 cond，与加这个功能之前逐字节一致。"
                : "未开启；且 models/ 目录下没有权重文件 —— 先放一个 .safetensors 进去。"));
    sec.append(foot);
    sec.append(el("div", "h3d-foot",
        "权重放在本插件目录的 models/ 里；作用范围只区分「文本 / 视觉」两类 token，"
        + "无法只护参考图而放开首帧图（两者在同一条视觉 token 段里）。"));
}

/* ---- 潜空间放大二采面板（右栏，独立后处理通道：主链完成后的清扫执行） ---- */

function upscaleSig(data) {
    const up = data.ds?.upscale || {};
    return JSON.stringify([
        up.mode ?? "", up.model ?? "", up.arch ?? "", up.scale ?? 0, up.denoise ?? 0,
        up.steps ?? 0, up.cfg ?? 0, up.precision ?? "", up.time_bias ?? 0, up.mix ?? 0,
        up.adaptive === true, up.shift ?? 0, (up.include || []).join(","),
        up.stg ?? 0, up.stg_block ?? 25, up.passes ?? 1, up.decay ?? 0.5,
        up.sharpen ?? 0, up.pixel_sharpen ?? 0,
        /* 时序分块 / 强制卸载 / 编码档位不在这里：已迁性能优化设置（机器级、全局） */
        up.device ?? "auto",
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

    const det = foldSection("up-top", false,
        `<summary>✦ 潜空间放大二采${on ? ' <span class="h3d-chip cyan">已开启</span>' : ""}</summary>`);
    det.classList.add("h3d-updet");
    if (on) det.classList.add("on");

    /* 两个子面板：基础（普通二采参数）/ 高级（抗糊增强那一套）。
     * body 就是**基础面板的网格** —— 原有 body.append 全部原样落进基础；
     * 只有明确属于「抗糊 / 自适应 / 细节」的字段改写进 agrid。
     * 三层的展开态都由 foldSection 记忆：改一个参数触发重渲时不会再弹回去。 */
    const basicBox = foldSection("up-basic", false,
        "<summary>⚙ 基础二采设置（模型 / 尺寸 / 采样 / 编码）</summary>");
    basicBox.classList.add("h3d-upsub");
    const body = el("div", "h3d-adv-grid");
    basicBox.append(body);
    const advBox = foldSection("up-adv", false,
        "<summary>🧪 高级二采设置（抗糊增强 / 细节混合 / 段自适应）</summary>");
    advBox.classList.add("h3d-upsub");
    const agrid = el("div", "h3d-adv-grid");
    advBox.append(agrid);

    if (!node) {
        body.append(el("div", "h3d-empty", "画布上未找到节点，二采面板不可用"));
        det.append(basicBox);
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

        /* ⛔ 「时序分块」与「强制卸载」两个控件**已从这里迁走**（2026-09-23）。
         * 理由：这两项是**机器级**设置（同一台卡该怎么省显存），与作品无关；
         * 放在项目存档里会随项目切换而串味，且 `chunk=false` / `force_unload=true`
         * 会进二采指纹、把既有高清段判失效重做。现由「⚡ 性能优化」弹窗统一管：
         *   · 时序分块 → 阶段「放大网络 · 分块」（upscale_temporal_chunk + 帧数/overlap）
         *   · 强制卸载 → 阶段「放大网络 · 常驻」（keep_upscaler_resident，取反面）
         * 别再往本面板加回来。 */

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
        agrid.append(upNumField("细节混合", up.mix, 0.0, 1.0, 0.05,
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
        agrid.append(adField);
        body.append(upNumField("调度偏移", up.shift, 0.0, 16.0, 0.5,
            "二采档 flow shift（T8 实证 12→6：高分辨率下调度更线性、细节合成更充分；"
            + "镜像官方 MiniMaxH3SigmaShift 的克隆补丁，主链模型零改动）：0=关（默认，沿用主链 "
            + "H3 默认 12）；6=推荐档。仅在 >0 时进二采指纹",
            (v) => setUpscaleField(node, "shift", v)));

        /* ---- 抗糊武器库（全部默认关；详见《更新说明_二采抗糊抗条纹》）----
         * 与「细节混合 / 段自适应σ」同属打画质实验手感的一套，统一放高级面板。 */
        agrid.append(el("div", "h3d-convbadge", "⟡ 抗糊增强（全部默认关，按需开启）"));
        agrid.append(upNumField("STG 引导", up.stg, 0.0, 2.0, 0.05,
            "跳块差分细节引导（T8 机制移植，GPL）：激活窗内每步多跑一次「跳掉一个 double block」"
            + "的弱前向，把完整前向多出来的细节显式放大——CFG=1.0 下也有效。0=关（默认）；"
            + "0.5-1.0 常用（每激活步约 +1 次前向 ≈ +50% 精化耗时）。仅在 >0 时进二采指纹",
            (v) => setUpscaleField(node, "stg", v)));
        agrid.append(upNumField("STG 跳块", up.stg_block, 0, 49, 1,
            "STG 弱前向跳过的 double block 编号（H3 共 50 个）：T8 默认 25（中段块，"
            + "细节/纹理引导最稳）；换块号可改变引导性质（前段块=构图、后段块=质感）",
            (v) => setUpscaleField(node, "stg_block", v)));
        agrid.append(upNumField("精化轮数", up.passes, 1, 3, 1,
            "多轮递降精化：每轮以更小 σ 在上一轮输出上再精化（先修结构、再抠细节）。"
            + "1=单轮（默认，现行为）；2-3=递进（耗时×轮数）。仅在 >1 时进二采指纹",
            (v) => setUpscaleField(node, "passes", v)));
        agrid.append(upNumField("σ 衰减", up.decay, 0.2, 0.8, 0.05,
            "多轮递降的每轮 σ 缩放系数：第 k 轮 σ=σ₀·衰减^k（默认 0.5 → 0.35/0.18/0.09）。"
            + "仅精化轮数 >1 时生效",
            (v) => setUpscaleField(node, "decay", v)));
        agrid.append(upNumField("latent 锐化", up.sharpen, 0.0, 1.0, 0.05,
            "latent 域 unsharp 锐化（精化输出高频再放大一档，CPU 零显存零前向）："
            + "0=关（默认）；0.2-0.4 常用；过大可能放大噪声/伪影。仅在 >0 时进二采指纹",
            (v) => setUpscaleField(node, "sharpen", v)));
        agrid.append(upNumField("像素锐化", up.pixel_sharpen, 0.0, 1.0, 0.05,
            "解码后逐帧 unsharp 锐化（连 VAE 解码的软化一起补偿，编码前生效）："
            + "0=关（默认）；0.2-0.4 常用。与 latent 锐化正交可叠加。仅在 >0 时进二采指纹",
            (v) => setUpscaleField(node, "pixel_sharpen", v)));

        /* ⛔ 「编码档位」控件**已从这里迁走**（2026-09-23）。
         * 理由：它管的是「用哪套质量档」（crf + preset + 暗部抖动），与 perf 的
         * `encoder`（用哪个编码器实现）+ `nvenc_cq` 是**正交**的两件事，但同属编码
         * 话题 —— 分在两处正是「同一件事散在两个组里」的典型。现统一收进
         * 「⚡ 性能优化 → 成片与编码 · 编码」区，与编码器并排，说明互补关系。
         * 别再往本面板加回来。 */

        /* 独立采样器/调度器（空 = 沿用主链）：**下拉选择**，选项直接取主节点
         * 「采样器 / 调度器」两个控件的注册名单——与一采基础栏同一份来源，
         * 不再手打名字（打错只能等运行时静默回退，用户看不出自己填错了）。 */
        const upChoiceField = (label, value, widgetName, tip, commit) => {
            const field = el("div", "h3d-param");
            field.append(el("label", "", label));
            const sel = document.createElement("select");
            sel.className = "h3d-select";
            sel.title = tip;
            const w = node ? (node.widgets || []).find((x) => x.name === widgetName) : null;
            const opts = (w && w.options && Array.isArray(w.options.values)) ? w.options.values : [];
            const o0 = document.createElement("option");
            o0.value = "";
            o0.textContent = "沿用主链";
            sel.append(o0);
            for (const v of opts) {
                const o = document.createElement("option");
                o.value = v;
                o.textContent = v;
                sel.append(o);
            }
            /* 存档里存了主链当前列表没有的名字（换过插件/自定义节点）→ 照样列出来，
             * 静默丢掉等于把用户的设置无声抹掉 */
            if (value && !opts.includes(value)) {
                const o = document.createElement("option");
                o.value = value;
                o.textContent = `${value}（不在当前列表）`;
                sel.append(o);
            }
            sel.value = value || "";
            sel.onchange = () => commit(sel.value);
            field.append(sel);
            return field;
        };
        body.append(upChoiceField("采样器", up.sampler, "采样器",
            "二采独立采样器（沿用主链=不另设）。非空进二采指纹",
            (v) => setUpscaleField(node, "sampler", v)));
        body.append(upChoiceField("调度器", up.scheduler, "调度器",
            "二采独立调度器（沿用主链=不另设）。非空进二采指纹",
            (v) => setUpscaleField(node, "scheduler", v)));

        /* 增益自适应重试（判据依赖精化后的细节增益，属高级项） */
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
        agrid.append(rtField);
        agrid.append(upNumField("重试目标", up.retry_target, 0.05, 1.0, 0.05,
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
    det.append(basicBox);
    if (on) det.append(advBox);   // 关闭时高级面板没有可调项，不占位

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
    if (mergeSel.running) infos.push("合并导出进行中：生成按钮暂时不可用（跑完自动恢复）");
    z.footInfo.innerHTML = infos.map((s) => `<span>${s}</span>`).join("");

    const run = z.run;
    run.onclick = queuePrompt;
    /* 终止按钮只在"认为正在跑"时露出来：没在跑却摆一个醒目的红叉，只会让人
     * 以为哪里坏了。判定跟顶栏那盏灯同源（提交那一刻置 running）。 */
    if (z.stop) z.stop.style.display = ledPhase === "running" ? "" : "none";
    const pend = redoPending(data), queued = redoQueued(data);
    if (mergeSel.running) {
        run.disabled = true;
        run.textContent = "⧉ 合并导出进行中（跑完可生成）";
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
        点「创建项目」会在 output/h3_projects/ 下立即建好文件夹（游戏存档槽：创建即可见）。
        默认建<b>空白项目</b>（0 段起跑，零继承）；勾选下方<b>复制当前项目</b>则把提示词、
        分段设置与参考图整份带过来。<br><b>分辨率 / 步数 / CFG 等链参数不继承</b>：
        那是"这条链用什么设置跑出来的"指纹，跟过来会把新项目钉在旧画幅上
        （改了就与存档不符、跑不动）。新项目按画布当前值起跑。</p>`;
    const input = document.createElement("input");
    input.type = "text";
    input.value = def;
    input.spellcheck = false;
    const err = el("div", "h3d-err", "");
    const check = el("label", "h3d-check");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    check.append(cb, document.createTextNode(
        "复制当前项目（提示词 / 分段设置 / 参考图整份带过来）"));
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
        /* 要复制的源项目：节点「存档目录」优先，回落 lastDir（自动命名模式下没有
           显式目录名，但画布上正在编辑的就是上次运行的那个项目）。 */
        const srcDir = String(getDirValue(node) || lastDir || "");
        const copyFrom = cb.checked ? srcDir : "";
        if (cb.checked && !copyFrom) {
            err.textContent = "当前没有可复制的项目：存档目录为空，且还没跑过任何项目";
            return;
        }
        clearTimeout(promptFlushTimer);     // 取消挂起的批量防抖：旧项目下面显式回写
        await flushPrompts(node);           // 旧项目先落盘 —— 复制要拷的就是这一份
        /* 立即落盘建项目文件夹（游戏存档槽语义：创建即可见）。copy_from 让后端把
           源项目的 manifest + assets/ 整份复制过来（素材文件跟着走，路径才成立）。
           接口 404/405（未注册）时降级为惰性建目录，不阻断。 */
        let diskOk = false;
        try {
            const r = await api.fetchApi("/h3chain/create_project", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ dir: name, copy_from: copyFrom }),
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
        close();
        if (!diskOk) {
            /* 接口未注册（旧版 / 未重启）：退回惰性路径 —— 首次运行会自动建目录。
               此时没法复制文件，只能沿用底稿或清空，并在 LED 里说清降级了。 */
            if (!setDirValue(node, name)) { alert("节点上没有「存档目录/断点目录」控件"); return; }
            setWidgetValue(node, W_REROLL, 0);
            if (copyFrom) flushPrompts(node, name); else clearPrompts(node);
            setLed("idle", `新项目「${name}」已就绪（接口未注册，首次运行时自动建目录）`);
            scheduleRefresh(200);
            return;
        }
        /* 装载走**读档那一条路径**：新建与读档共用同一套加载逻辑，不再有
           "新建一套、读档另一套"的双轨 —— 双轨正是状态串味的温床。
           空项目装出空画布；复制项目装出与源一致的画布。 */
        await switchProject(name);
    };
    ok.onclick = submit;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
}

/* ---- 总提示词工作台：纯分段流水线 ----
 *
 * **它不是一个结果框，也不做 AI。** 打开就是空框，只干一件事：把外部（AI /
 * 另一个工程 / 手写）的一大段多段文本粘进来，「解析并分配」写回段落。
 * 给每段的东西只有三样：
 *   · 提示词 —— ds.prompts[i]，就是段卡那个框，直接进模型；
 *   · 时长   —— `Duration: 9`；
 *   · 独立镜头 —— `Standalone: yes` = 本段不自动引用上段尾帧（跳转 / 闪回 / 蒙太奇）。
 * 段级标签只有这三个，其余一律是正文。
 * ★ 标签一律**英文**（与正文同为英文）：`[Segment N]` / `Duration:` /
 *   `Standalone:` / `Prompt:` / `[END]`。中文旧标签仍可解析（只读兼容）。
 *
 * **资产引用与段卡同一套语义**：就写在提示词正文里的 `@素材名`（绿框），
 * 分配时由正文的 @序列 派生 seg.refs（syncRefsFromText）。不再有「参考：」那条
 * 第二通道——两条通道必然对不上，那正是"总提示词框引用不对"的来源。
 *
 * **不做 AI**：格式清洗交给段卡上的「✨ AI扩写+优化 / ✨ 提示词优化」（一次一段、
 * 就地写回，不用在两个框之间倒文本）。所以这里没有模式下拉、没有参考图勾选、
 * 没有原稿回退——那三个控件只是 AI 那一跳的参数。
 *
 * **也不与段卡同步**：打开时不自动载入。「从当前链载入」是个手动按钮，只服务于
 * 「导出 → 外部改 → 贴回」的往返，不是常驻镜像。 */

/* 空框占位（严格按官方 h3-prompt-writing base-en.txt：三字段英文骨架逐字不改；
 * [Shot 1] 无时间戳且先声明风格，后续 [Shot N] At MM:SS.mmm；运镜写成句内自然动作
 * （类型+幅度+速度）；(S1) 说话人 + <d>[Language] 原文</d>；环境音进
 * overall_soundscape，配乐无则 N/A。正文英文，只有对白保原语言）。
 * 只示范「段头 + Duration / Standalone + Prompt」这一种写法——模式、对齐指令那些是
 * 段卡上「AI 优化」按锚点产出的，这里不做 AI，就不在占位里假装有。 */
const MP_PH_MAIN = `[Segment 1]
Duration: 8

Prompt:
integrated_multimodal_description: [Shot 1] Live-action, cinematic, a medium shot with slight handheld shake frames the narrow alley of a rainy night market, neon signs dragging long red and blue streaks across the standing water. The camera pushes in with small amplitude at slow speed as a young woman with a quiet, breathy voice (S1) stops and looks back, saying: <d>[Chinese] 你要走了吗？</d>

[Shot 2] At 00:03.200, the camera cuts to a close-up of her side profile as the corners of her mouth lift slightly.

overall_soundscape: Steady rain continues, tapping densely on the metal stall roofs, with distant vendors calling out over the crowd.

non_diegetic_music: N/A

[Segment 2]
Duration: 8
Standalone: yes

Prompt:
integrated_multimodal_description: [Shot 1] Live-action, she turns and squeezes into the crowd as the camera tracks right with small amplitude at moderate speed.

overall_soundscape: The vendors' calls fade with distance while her footsteps become prominent.

non_diegetic_music: N/A`;

function openMasterPromptModal() {
    const node = findNode();
    if (!node) { alert("画布上未找到 H3 Seamless Chain 节点（只读模式）"); return; }
    if (document.querySelector(".h3d-overlay")) return;

    const overlay = el("div", "h3d-overlay h3d-overlay-full");
    const dialog = el("div", "h3d-dialog h3d-dialog-full");
    dialog.innerHTML = `
        <h3>📋 总提示词 · 多段分配</h3>
        <p class="h3d-lead">把外部（AI / 另一个工程 / 手写）的一大段多段文本粘进来，
        <b>「解析并分配」</b>一次写回所有段落。用 <code>[Segment N]</code> 分段
        （不写段头 = 单段）；段内只有三个段级标签
        <b>Duration / Standalone / Prompt</b>，其余一律是正文。
        <b>资产引用就写在提示词正文里的 <code>@素材名</code></b>——与段卡提示词框同一套，
        粘完点「🔗 解析引用」可把别名 / 标注统一成素材全名。
        这里<b>不做 AI</b>：要洗格式请去段卡上的「✨ AI扩写+优化 / ✨ 提示词优化」。
        <b>只有「解析并分配」会写回你的段落。</b></p>`;

    /* ---- 单框：全片分段提示词 ---- */
    const stack = el("div", "h3d-mpstack");
    const boxWrap = el("div", "h3d-mpboxwrap h3d-mpboxwrap-main");
    const head = el("div", "h3d-mplabel");
    /* 解析引用（与段卡同一个动作、同一套规则）：从别处复制一大段提示词粘进来后，
     * 点它一次把里面的素材名对应到素材库（别名/标注统一成完整文件名）。
     * onclick 在 ta 建好后绑定（要用到编辑器实例）。 */
    const btnResolve = el("button", "h3d-btn", "🔗 解析引用");
    btnResolve.type = "button";
    btnResolve.classList.add("h3d-mpboxbtn");
    btnResolve.title = "把正文里的素材名对应到素材库：别名 / 标注统一写成完整文件名"
        + "（含后缀），认不出来的名字会点名提示。\n"
        + "从别处复制来的提示词粘进来后点它一次即可。";
    head.append(el("b", "", "提示词（最终进模型）"),
        el("small", "", "[Segment N] 分段 · 段级标签只有 Duration / Standalone / Prompt"
            + " · 资产引用写正文 @素材名"),
        btnResolve);
    /* 素材池（总提示词框用）：与段卡读同一处活状态，别用快照。 */
    const mpPool = () => {
        try { return getDs(node).ref_assets || []; } catch (e) { return []; }
    };
    /* 提示词框与段卡**同一套**编辑器（createPromptEditor）：引用名表、绿框渲染、
     * 解析逻辑、以及分配时的 refs 派生（syncRefsFromText）全部共用 —— 以前这里是
     * 裸 textarea 加一个「参考：」标签，那正是"总提示词框的素材引用和分段两回事"
     * 的根因。现在两者没有任何语义差异，只有"一段 vs 全篇"这个粒度差别。 */
    const ta = createPromptEditor({
        value: "",
        labels: () => refLabelsOf(mpPool()),
        assets: () => refInfoMapOf(mpPool()),
        /* 官方标签 <Picture N> 可视化打开：外部 agent / 官方格式的文本里是这种写法，
         * 渲染出来才能一眼看出"Picture 几"指向哪张图（顺序挂错立刻可见）。 */
        tokenMap: () => ({}),
        dir: () => { try { return getDirValue(node); } catch (e) { return ""; } },
        autoNormalize: false,
    });
    ta.el.classList.add("h3d-mpbox");
    ta.el.spellcheck = false;
    ta.placeholder = MP_PH_MAIN;
    boxWrap.append(head, ta.el);
    btnResolve.onclick = () => {
        let r = null;
        try {
            r = ta.resolveRefs();
        } catch (e) {
            setLed("err", `解析失败：${e && e.message ? e.message : e}`);
            return;
        }
        if (!r) { setLed("idle", "解析无结果"); return; }
        if (r.unknown && r.unknown.length) {
            setLed("warn", `${r.unknown.length} 个名字在素材库里找不到：`
                + r.unknown.slice(0, 6).join("、")
                + (r.unknown.length > 6 ? " …" : ""));
            alert("这些名字在素材库里对不上（不会挂上素材）：\n- "
                + r.unknown.join("\n- ")
                + "\n\n请核对素材名，或先把素材加进项目库。");
        } else if (r.hits) {
            setLed("done", `已解析 ${r.hits} 处引用`
                + (r.changed ? "（名字已统一成完整文件名）" : ""));
        } else {
            setLed("idle", "正文里没有 @引用");
        }
    };
    stack.append(boxWrap);

    const info = el("div", "h3d-mpinfo", "");
    const err = el("div", "h3d-err", "");
    const row = el("div", "h3d-dialog-row");
    const bLoad = el("button", "h3d-btn", "从当前链载入");
    const bClear = el("button", "h3d-btn", "清空");
    const cancel = el("button", "h3d-btn", "取消");
    const ok = el("button", "h3d-btn h3d-btn-cta", "解析并分配");
    row.append(bLoad, bClear, cancel, ok);
    dialog.append(stack, info, err, row);
    overlay.append(dialog);
    overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") overlay.remove(); });
    document.body.append(overlay);
    ta.focus();

    const paint = () => {
        const text = String(ta.value ?? "");
        if (!text.trim()) { info.textContent = ""; err.textContent = ""; return null; }
        const p = parseMasterPrompt(text);
        const cntMain = p.segs.filter((s) => String(s.main ?? "").trim()).length;
        const total = p.segs.reduce((a, s) => a + (Number(s.seconds) || 0), 0);
        const nUnlink = p.segs.filter((s) => s.unlink === true).length;
        /* 引用计数直接调 refsFromText（**段卡用的同一个函数**）：这里显示几处，
         * 分配后 seg.refs 就是几个，不会出现"框里看着引用了、段落却没挂上"。 */
        const pool = mpPool();
        const nRef = p.segs.reduce((a, s) => a + refsFromText(String(s.main ?? ""), pool).length, 0);
        info.innerHTML = `识别 <b>${p.segs.length}</b> 段 · 总时长 <b>${Math.round(total)}s</b>`
            + ` · 有正文 <b>${cntMain}</b> 段`
            + (nUnlink ? ` · 独立镜头 <b>${nUnlink}</b>` : "")
            + (nRef ? ` · 素材引用 <b>${nRef}</b> 处` : "");
        const allNotes = (p.notes || []).filter(Boolean);
        err.textContent = allNotes.length ? "⚠ " + allNotes.join("；") : "";
        return p;
    };
    ta.addEventListener("input", paint);

    /* 「从当前链载入」：**只在手动点的时候**发生。
     * 打开时不再自动载入 —— 这个框不是段卡提示词的镜像/结果框。自动载入会让人
     * 以为两边是双向同步的（改段卡这边跟着变、改这边段卡也跟着变），而实际上
     * 只有「解析并分配」是单向写入。想拿现成内容去外面改，就手动点一下。 */
    const loadFromChain = () => {
        ta.value = exportMasterPrompt(node);
        paint();
        info.textContent = "已载入当前链（改完点「解析并分配」写回段落）";
    };

    bLoad.onclick = () => loadFromChain();
    bClear.onclick = () => {
        if (String(ta.value || "").trim() && !confirm("清空整个框？（链上数据不动）")) return;
        ta.value = "";
        paint();
    };
    cancel.onclick = () => overlay.remove();
    const submit = () => {
        const text = String(ta.value ?? "");
        if (!text.trim()) { err.textContent = "内容为空"; return; }
        const p = applyMasterPrompt(node, text);
        if (!p.segs.length) { err.textContent = "未识别到任何段落"; return; }
        flushPrompts(node);                       // 同步项目 manifest 底稿
        setLed("done", `总提示词已分配到 ${p.segs.length} 段`);
        overlay.remove();
        scheduleRefresh(60);
        if (p.notes.length) alert("注意：\n- " + p.notes.join("\n- "));
    };
    ok.onclick = submit;
}


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
            ref_name: cleanRefName(a.ref_name || a.file || "") || String(a.label),
            mark: String(a.mark || ""),
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
            /* 引用名优先取链接上的真名（含后缀）；全局库条目 file 形如
             * `images/<sha12>_猫.png`，带 sha 前缀不能直接用，所以顺序是
             * ref_name > orig_name > 别名兜底。 */
            ref_name: cleanRefName((L && L.ref_name) || (L && L.orig_name) || "")
                || alias,
            mark: String((L && L.mark) || ""),
            asset_id: String((L && L.asset_id) || ""),
            roles: _rolesOf(L && L.roles),
        });
    }
    /* 有 asset_id 的链接条目**即使 file 为空也要留下**：file 是后端从全局库按
     * asset_id 回填的，全局库不可用时（try_library_root 定位失败 / 条目还没进库）
     * 就回填不到 —— 旧实现在这里按 file 过滤，于是「从全局库调进来的视频、音频、
     * 部分图片在引用条里一个都不出现」。有 asset_id 时预览走 libraryFileUrl(asset_id)，
     * 寻址照样正确；只有既无 file 又无 asset_id 的才是真·无效条目。
     * 最后统一补标注（图片1/…）：后端落盘的为准，这里只兜底旧档。 */
    return assignMarks([...byLabel.values()].filter((a) => a.file || a.asset_id));
}

/** 池子指纹：只有它变了才写回 widget（避免无谓重绘/丢焦） */
function poolSig(pool) {
    return (pool || []).map((a) => [
        String((a && a.label) || ""), String((a && a.file) || ""),
        String((a && a.kind) || "image"), String((a && a.asset_id) || ""),
        _rolesOf(a && a.roles).join("·"),
        String(refKeyOf(a) || ""),
        String(markTextOf(a) || ""),
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

/** 旧档引用名迁移：正文 / seg.refs 里的 `@别名(stem)` → `@引用名(含后缀)`。
 *
 *  引用名换轨后（ref_name 带后缀），旧正文里的 `@女主` 就解析不出来了 —— 不迁移
 *  的话一打开旧项目满屏红框（悬空引用）。这里按池子的 label→ref_name 映射改写，
 *  **先试更长的全名**（已经写了 `@女主.png` 的原样放过，否则会变成 `.png.png`）。
 *  只改 widget 里的 ds，落盘等下一次 flush 自然带上。 */
function migrateRefsToFullNames(ds, pool) {
    const pairs = (pool || [])
        .map((a) => ({ label: String(a.label || ""), full: refKeyOf(a) }))
        .filter((p) => p.label && p.full && p.full !== p.label)
        .sort((a, b) => b.full.length - a.full.length || b.label.length - a.label.length);
    if (!pairs.length || !ds) return false;
    const fixText = (t) => {
        const s = String(t || "");
        if (!s.includes("@")) return s;
        let out = "";
        let i = 0;
        while (i < s.length) {
            if (s[i] === "@" && !/[0-9A-Za-z_]/.test(s[i - 1] || "")) {
                const hitFull = pairs.find((p) => s.startsWith(p.full, i + 1));
                if (hitFull) { out += `@${hitFull.full}`; i += hitFull.full.length + 1; continue; }
                const hit = pairs.find((p) => s.startsWith(p.label, i + 1));
                if (hit) { out += `@${hit.full}`; i += hit.label.length + 1; continue; }
            }
            out += s[i];
            i += 1;
        }
        return out;
    };
    let changed = false;
    for (let i = 0; i < (ds.prompts || []).length; i += 1) {
        const old = String(ds.prompts[i] || "");
        const next = fixText(old);
        if (next !== old) { ds.prompts[i] = next; changed = true; }
    }
    for (const s of ds.segments || []) {
        if (!Array.isArray(s.refs)) continue;
        const next = s.refs.map((l) => {
            const hit = pairs.find((p) => p.label === String(l));
            return hit ? hit.full : l;
        });
        if (next.some((x, i) => x !== s.refs[i])) { s.refs = next; changed = true; }
    }
    return changed;
}

async function hydratePool(force) {
    const node = findNode();
    if (!node) return;
    const dir = getDirValue(node);
    if (!dir) return;
    const mf = await fetchManifest(dir);
    /* 读不到 **不等于** 项目是空的。把读取失败当成空池会把 widget 里的池子清掉，
     * 紧接着任何一次 persistPool（删素材 / 改标签）就拿这个空池整表覆盖
     * manifest["assets"] —— 那是真丢数据。所以只在确确实实读到 manifest 时才用它的
     * 池子覆盖（读到但无资产 = 真空项目，那时清空才是对的）。 */
    if (!mf) return;
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
    /* 池子刚换过：顺带把正文里旧写法 `@别名` 迁到 `@全名`（只动 widget） */
    migrateRefsToFullNames(ds, pool);
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

/** 按后端**真实**生成锁校正顶栏那盏灯。
 *
 *  缺口在这里：LED 只在提交那一刻 setLed("running","已提交队列")，之后**没有
 *  任何人**负责把它放下来。链在服务端跑完 / 报错 / 被中断，灯都一直亮着
 *  running —— 用户看到的是"还在跑"，实际早就结束了（报错尤其如此）。
 *  这里用 GET /h3chain/busy 补上：后端说"不忙"而灯还停在 running，说明这一轮
 *  已经收场，把灯放下来。
 *
 *  只做校正，**不做任何取消动作** —— 取消生成是页脚「✕ 终止」的事，刷新
 *  绝不能顺手把别人正在跑的链给停了。 */
async function syncRunLed() {
    if (ledPhase !== "running") return false;      // 不是 running 就别乱盖（err/done 是有信息的）
    let busy = null;
    try {
        const r = await window.H3Api?.busy?.();
        const b = r?.body;
        if (b && typeof b.busy === "boolean") busy = b.busy;
    } catch (e) {
        return false;      // 接口不可用就保持原状，不猜、不误报
    }
    if (busy !== false) return false;
    setLed("idle", "已结束（刷新校正）");
    return true;
}

/** 顶栏 ↻：一次真正的"全面"刷新。
 *  refresh() 是给 900ms 轮询用的那条路：签名没变就不重挂、编辑中还会主动跳过
 *  （防丢焦丢草稿）。所以"自动刷新看着没反应"是设计使然。用户**手动**点时
 *  想要的是无条件全量重挂 —— 这里把三栏的签名缓存清掉强制重画，并先按后端
 *  真实状态把灯校正。 */
async function refreshAll() {
    await syncRunLed();
    if (desk) { desk.cardsSig = ""; desk.histSig = ""; }
    await refresh();
    /* 手动刷新时若焦点正停在某个输入框里，updateDesk 仍会刻意跳过卡片重建
     * （防打断输入）。这种情况必须说出来，否则用户以为"点了刷新没反应"。 */
    if (desk && desk.page.querySelector(".h3d-ta:focus, .h3d-v2f:focus, input:focus")) {
        setLed("idle", "已刷新数据（正编辑的框保持不动，未打断输入）");
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

app.registerExtension({
    name: "H3SeamlessChain.DirectorDesk",
    /* 画布节点就绪即刷新：面板首刷可能早于工作流载入（节点未就绪 → 各区渲染
     * 「画布上未找到节点」），之后没有任何事件再触发刷新——这里在节点载入/
     * 新建时主动补一刷，mini 卡与参数/二采区随之恢复可用 */
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
