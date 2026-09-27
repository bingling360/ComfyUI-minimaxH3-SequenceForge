/* ⚡ 性能优化弹窗守卫。
 *
 * 背景（为什么要搬到弹窗）：性能项有 30+ 条、每条都带一段解释，原先塞在右栏
 * 「链参数」的窄折叠里 —— label + 控件 + 整段说明挤在同一行，右栏宽度放不下，
 * 说明被挤没，用户报的就是「信息展现不全」。现在入口在顶栏「⚡ 性能优化」，
 * 点开是与 AI 优化设置同款的弹窗，一律两行式（label+控件 / 说明另起一行）。
 *
 * 2026-09-23 改版：分类树从「按资源维度」（显存/内存/编码/…）改成**按处理链阶段**
 * （一采采样 → 放大网络 → 精化二采 → 成片与编码 → 通用与机器），
 * 因为用户调参时的真实提问是「我这个阶段该开什么分块」，而不是「这东西吃显存还是内存」。
 * 同日晚些又删了「VAE 解码」一级（官方 VAE 已内部分块，该组 0 收益，控件已撤）。
 *
 * 本文件钉这几件事：
 *   ① 一个渲染主人：右栏不得再有 param-perf 折叠 / paintPerfPane 残留
 *   ② 阶段树完整：5 个一级阶段全部渲染、顺序与字段表一致、默认全展开
 *   ③ 字段一个不漏：DOM 里的行数 = H3_PERF_FIELDS 条目数
 *   ④ 开关两件套：带 enable 的字段由它绑定的开关控灰
 *   ⑤ 按值显隐：带 showWhen 的字段（crf / cq）跟随宿主下拉切换，且可逆
 *   ⑥ 说明真的展开：hint 里的 **强调** 渲染成 <b>，不能露出字面星号
 *   ⑦ 保存契约：改动攒草稿，点「保存并应用」才 POST；未接线名单已空（块交换接线后）
 *   ⑧ 右栏不得再有已迁走的性能控件（时序分块 / 强制卸载 / 编码档位）
 *
 * 用法：node tests/js/perf_modal_check.js
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { load, ROOT } = require("./_harness");

const SRC = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8");

/* 从源码取字段表（jsdom 间接 eval 取不到顶层 const，只能正则数） */
const STAGES_START = SRC.indexOf("const H3_PERF_STAGES = {");
const STAGES_BLOCK = SRC.slice(STAGES_START, SRC.indexOf("\nconst H3_PERF_FIELDS", STAGES_START));
const STAGE_NAMES = [...STAGES_BLOCK.matchAll(/^\s{4}"([^"]+)":\s*\{/gm)].map((m) => m[1]);
/* 一级名 -> {when, note}：2026-09-27 起 H3_PERF_STAGES 真的被渲染了（之前
 * 定义了没人读），所以要能把两个字段取出来逐字比对，不能只看名字。 */
const STAGES = {};
for (const m of STAGES_BLOCK.matchAll(/^\s{4}"([^"]+)":\s*\{([^}]*)\}/gm)) {
    const w = m[2].match(/when:\s*"([^"]*)"/);
    const n = m[2].match(/note:\s*"([^"]*)"/);
    STAGES[m[1]] = { when: w ? w[1] : "", note: n ? n[1] : "" };
}

const BLOCK_START = SRC.indexOf("const H3_PERF_FIELDS = [");
const BLOCK = SRC.slice(BLOCK_START, SRC.indexOf("\n];", BLOCK_START));
/* 只认字段表条目：每个条目一律以行首的 `{ key: "..."` 起头。
 * ⚠ 不能写成 /{ key: "([^"]+)"/g —— 那只按字面找，会连 showWhen 内层的
 *   `{ key: "encoder", values: [...] }` 一起吞进来（本轮加了 crf/cq 的按值显隐后，
 *   该正则多出 2 个假条目，36 行 DOM 对 38 条字段表，误报「字段漏渲染」）。 */
const FIELD_KEYS = [...BLOCK.matchAll(/(?:^|\n)\s*\{ key: "([^"]+)"/g)].map((m) => m[1]);
const GROUPS = [...new Set([...BLOCK.matchAll(/group: "([^"]+)"/g)].map((m) => m[1]))];
/* 未接线项：字段上带 wired: false。当前是**零** —— 前端已经把 37 项全画出来，
 * 后端的 wired 名单再决定哪几项真生效。所以这里不数 false，改数「谁没进 wired 名单」，
 * 由测试用桩数据的 wired 名单驱动（见「未接线项灰掉」用例）。 */
/* ⛔「未接线名单」的三态机制已于 2026-09-23 **连根删除**（后端三个名单 + 接口字段
 * + 前端判态与徽章全没了）。这里不再备任何副本，改为**反向守卫**：
 * 见下面「三态名单不许回潮」用例 —— 谁把 WIRED_KEYS / UNWIRED_KEYS /
 * PROFILE_DRIVEN_KEYS 或「接线中」徽章写回来，直接挂。 */
/* key -> label（从字段表正则取，供「按标签找行」用） */
const LABELS = {};
for (const m of BLOCK.matchAll(/(?:^|\n)\s*\{ key: "([^"]+)",\s*label: "([^"]+)"/g)) LABELS[m[1]] = m[2];
const labelOf = (k) => {
    const v = LABELS[k];
    if (!v) throw new Error("字段表里没有 key：" + k);
    return v;
};
/* key -> **本条目自己**的 group。
 * ⚠ 不能写成 `GROUPS[FIELD_KEYS.indexOf(k)]` —— GROUPS 是**去重后的 Set**，
 *   与 FIELD_KEYS 不同标，按下标取会串行（2026-09-27 加断言时踩过）。
 * 这里从 key 的位置往后取**第一个** group：字段表里 group 一定在同一个条目内、
 * 且在 key 之后；showWhen 的内层对象只带 key/values、不带 group，不会串到下一个条目。 */
const GROUP_OF = (k) => {
    const i = BLOCK.indexOf('key: "' + k + '"');
    if (i < 0) throw new Error("字段表里没有 key：" + k);
    const m = BLOCK.slice(i).match(/group: "([^"]+)"/);
    if (!m) throw new Error(k + " 的条目里没有 group");
    return m[1];
};
const ENABLE_PAIRS = [...BLOCK.matchAll(/\{ key: "([^"]+)"[^\n]*?enable: "([^"]+)"/g)]
    .map((m) => [m[1], m[2]]);

/* 分类树：group 形如「一采采样 · 分块」→ 一级阶段 + 二级用途；不含「 · 」的本身就是一级。
 * HEAD_COUNT 数的是**字段条目**（每条字段都带一次 group），不是去重后的组名。 */
const HEADS = [...new Set(GROUPS.map((g) => g.split(" · ")[0]))];
const SUBS_OF = (h) => [...new Set(GROUPS.filter((g) => g.split(" · ")[0] === h)
    .map((g) => g.split(" · ")[1]).filter(Boolean))];
const HEAD_COUNT = (h) => [...BLOCK.matchAll(/group: "([^"]+)"/g)]
    .filter((m) => m[1].split(" · ")[0] === h).length;
const groupEls = (root) => [...root.querySelectorAll(".h3d-perf-body > .h3d-perf-group")];
const subEls = (grp) => [...grp.children].filter((n) => n.classList.contains("h3d-perf-sub"));
const titleOf = (det) => det.querySelector("summary").childNodes[0].textContent;

let ok = true;
const results = [];
async function t(name, fn) {
    try { await fn(); results.push("  OK " + name); }
    catch (e) { ok = false; results.push("  FAIL " + name + " :: " + e.message); }
}

(async () => {
    console.log("\n== ⚡ 性能优化弹窗 ==");

    await t("字段表读到了（正则是活的，不是空数组蒙过）", () => {
        assert.ok(FIELD_KEYS.length >= 26, "字段数异常：" + FIELD_KEYS.length);
        assert.ok(GROUPS.length >= 5, "分组数异常：" + GROUPS.length);
        assert.ok(STAGE_NAMES.length === 5, "阶段元信息应有 5 个：" + STAGE_NAMES.join(","));
        /* 阶段树的一级名必须与字段表 group 的一级名一一对应，不能一边多一边少 */
        assert.deepStrictEqual(HEADS.slice().sort(), STAGE_NAMES.slice().sort(),
            "字段表一级名与阶段表对不上：" + HEADS.join(",") + " vs " + STAGE_NAMES.join(","));
        /* 开关两件套是硬结构：每个 enable 引用的开关 key 必须真的存在，否则那个
         * 参数控件永远灰着（或永远亮着），用户看不出来。 */
        for (const [key, master] of ENABLE_PAIRS) {
            assert.ok(FIELD_KEYS.includes(master),
                key + " 绑的开关 " + master + " 不在字段表里");
        }
    });

    await t("★ 分块归「全局分块」：一采二采共用，不许再挂「一采采样」名下", () => {
        /* 2026-09-27 用户拍板：FFN / 注意力头分块在 nodes.py 里会**同时装到一采模型
         * 和二采模型**上（nodes.py:2156 / 2183 / 2201 / 2208），块交换与显存预留写的
         * 又是**进程级**的 aimdo 值 —— 挂在「一采采样」名下会让人以为二采不生效。
         * 所以这一组改按**作用范围**命名。本条同时钉住旧名不许回来。 */
        assert.ok(HEADS.indexOf("全局分块") >= 0,
            "一级阶段里没有「全局分块」：" + HEADS.join(","));
        assert.strictEqual(HEADS.indexOf("一采采样"), -1,
            "「一采采样」不该再作为一级阶段存在（分块是全局的）");
        const subs = SUBS_OF("全局分块");
        for (const want of ["FFN", "注意力", "权重流动"]) {
            assert.ok(subs.indexOf(want) >= 0,
                "「全局分块」下缺二级用途 " + want + "（现有 " + subs.join(",") + "）");
        }
        /* 真的改了 group 字段，不是只在注释里改了名 */
        assert.ok(GROUP_OF("ff_chunk_on").indexOf("全局分块") === 0,
            "ff_chunk_on 的 group 还是 " + GROUP_OF("ff_chunk_on"));
        assert.ok(GROUP_OF("attn_head_on").indexOf("全局分块") === 0,
            "attn_head_on 的 group 还是 " + GROUP_OF("attn_head_on"));
    });

    await t("★ 「显存预留（GB）」与块交换同组，且是**独立项**（不绑总开关）", () => {
        /* 它与 blocks_to_swap 写的是**同一个 aimdo 值**（两种单位）→ 必须同组，
         * 否则用户看不出两者的关系与优先级。
         * 但它必须**独立于 blocks_swap_on** —— 绑上去会让「只想留余量」的用户
         * 被迫顺带改掉权重流动行为，那不是他要的（后端 apply_blockswap 同此口径）。 */
        assert.ok(FIELD_KEYS.indexOf("vram_reserve_gb") >= 0,
            "字段表里没有 vram_reserve_gb");
        assert.strictEqual(GROUP_OF("vram_reserve_gb"), GROUP_OF("blocks_to_swap"),
            "显存预留该与「交换块数」同组：" + GROUP_OF("vram_reserve_gb")
            + " vs " + GROUP_OF("blocks_to_swap"));
        assert.ok(!ENABLE_PAIRS.some((p) => p[0] === "vram_reserve_gb"),
            "显存预留不该绑任何总开关（它独立生效）");
    });

    await t("三态名单不许回潮（后端无名单、前端无判态、无「接线中」徽章）", () => {
        /* 2026-09-23 用户拍板：三态（已接线 / 接线中 / 跟随档位）连根删除。
         * 理由是那套名单人工维护、必然说谎 —— 「未接线」态早已恒空，而同一张名单
         * 曾把 10 个没人读的键当「内部键」放行。判据改为代码事实（有读取方），
         * 由 `tests/test_perf_settings.py::test_no_key_in_default_perf_is_unread` 守。 */
        const py = fs.readFileSync(path.join(ROOT, "perf.py"), "utf8");
        for (const name of ["WIRED_KEYS", "UNWIRED_KEYS", "PROFILE_DRIVEN_KEYS"]) {
            assert.ok(py.indexOf(name + " =") < 0,
                "perf.py 里又出现了 " + name + " —— 三态名单已删除，别再引入人工接线名单");
        }
        const routes = fs.readFileSync(path.join(ROOT, "routes.py"), "utf8");
        for (const field of ['"wired"', '"unwired"', '"profile_driven"']) {
            assert.ok(routes.indexOf(field + ":") < 0,
                "routes.py 的 perf 接口又回 " + field + " 了");
        }
        /* 前端：判态函数与徽章文案都不该再出现（「接线中」只在文件头的历史说明里） */
        assert.ok(SRC.indexOf("fieldState") < 0, "前端判态函数 fieldState 又回来了");
        const badge = SRC.split("\n").filter((l) => l.indexOf("接线中") >= 0
            && l.trimStart().indexOf("*") !== 0 && l.trimStart().indexOf("//") !== 0);
        assert.deepStrictEqual(badge, [], "前端又出现「接线中」徽章：" + badge.join(" | "));
    });

    await t("一个渲染主人：右栏不再有 param-perf / paintPerfPane", () => {
        assert.ok(SRC.indexOf("param-perf") < 0, "右栏旧折叠 id 还在");
        assert.ok(SRC.indexOf("paintPerfPane") < 0, "旧渲染函数还有残留引用");
        /* 不写死"紧跟 fixFocus"：顶栏后来又加了「⚙ AI 优化设置」在 perfBtn 左边，
         * 硬钉相对位置会让任何一次顶栏增补都把这条断言撞红。只要 perfBtn 仍在
         * 顶栏挂载点里（没被挪回右栏折叠）就算过。 */
        assert.ok(/right\.append\(fixFocus,[^)]*perfBtn/.test(SRC), "顶栏挂载点没带上 perfBtn");
    });

    await t("已迁走的性能控件不在右栏二采区复活", () => {
        /* 时序分块 / 强制卸载 / 编码档位三项 2026-09-23 统一收进性能弹窗。
         * 右栏若再出现同名**控件**，就是有人把它们加回来了。
         * 判据用控件构造特征而非中文词 —— 删除处的说明注释里必然提到这些词，
         * 拿词做断言只会命中注释（假失败）。 */
        assert.ok(SRC.indexOf("const UP_ENCODES") < 0, "右栏编码档位常量 UP_ENCODES 又冒出来了");
        const zone = SRC.slice(SRC.indexOf("function renderUpscaleZone"));
        const zoneBody = zone.slice(0, zone.indexOf("\nfunction ", 10));
        for (const k of ["up.chunk", "up.force_unload", "up.encode"]) {
            assert.ok(zoneBody.indexOf(k + " = ") < 0 || zoneBody.indexOf("/*") > zoneBody.indexOf(k + " = "),
                "右栏二采区又在写入 " + k);
        }
        /* 更硬的判据：这两个键已被明文禁止出现在项目存档里 */
        for (const k of ["force_unload", "chunk:"]) {
            const hit = zoneBody.indexOf(k + ":");
            if (hit < 0) continue;
            const line = zoneBody.slice(zoneBody.lastIndexOf("\n", hit) + 1, zoneBody.indexOf("\n", hit));
            assert.ok(line.trimStart().startsWith("*") || line.trimStart().startsWith("/*"),
                "右栏二采区又出现了 " + k + " 赋值：" + line.trim());
        }
    });

    let saved = null;
    let perfGetCalls = 0;
    const { w, errors } = load({
        H3Api: {
            async perfGet() {
                perfGetCalls++;
                return { body: {
                    ok: true,
                    data: { ff_chunk_tokens: 4096, oom_autoretry: true, encoder: "h264_nvenc",
                            x264_crf: 20, nvenc_cq: 18, encode_hq: false,
                            blocks_to_swap: 25, blocks_prefetch: true },
                    /* 三态名单（wired / unwired / profile_driven）已随机制一起删除，
                     * 接口不再回它们 —— 桩数据里也不该再出现（见上面的回潮守卫）。 */
                    /* 块交换的现状（后端 probe，前端只显示）：面板诊断区读它 */
                    applied: { blockswap: { on: false, path: "aimdo", blocks: 0,
                        applied: true, headroom_gb: 0.0, clamped: false,
                        note: "关闭 → 已恢复进程启动时的预留（0.00GB）" } },
                    report: "[H3性能] 显存 24.0GB / 内存 64GB / swap 0.0GB\n档位：balanced",
                    upcast: { effective: false, cli_force_upcast: 0, cli_dont_upcast: 1 },
                    /* 启动参数体检（2026-09-27 加）：桩里必须给，否则那块 UI
                     * 整块不渲染、下面的断言就成了空跑。字段照后端真实形状给
                     * （args 仅 flags、cmdline 是完整命令）。 */
                    launch_args: {
                        items: [
                            { flag: "--vram-headroom", value: "1", current: "未给（默认 0）",
                              tier: "primary", missing: true, runtime_ok: false,
                              why: "让 aimdo 多留 1GB。本机 VRAM 6GB —— 余量紧张，这项**强烈建议加上**。" },
                            { flag: "--use-ck-attention", value: "", current: "未给",
                              tier: "primary", missing: true, runtime_ok: false,
                              why: "注意力头分块的收益完全取决于后端内核。" },
                            { flag: "--disable-comfy-compiler", value: "",
                              current: "未给（编译器在跑）", tier: "fallback", missing: true,
                              runtime_ok: false, why: "关掉约 5% 耗时。" },
                            { flag: "--reserve-vram", value: "1", current: "未给",
                              tier: "alt", missing: false, runtime_ok: true,
                              why: "面板「块交换 → 交换块数」接的就是它。" },
                        ],
                        missing: ["--vram-headroom", "--use-ck-attention", "--disable-comfy-compiler"],
                        cmdline_flags: ["--vram-headroom", "--use-ck-attention"],
                        args: "--vram-headroom 1 --use-ck-attention",
                        cmdline: "python main.py --vram-headroom 1 --use-ck-attention",
                        vram_gb: 6.0, kitchen_ok: true,
                        note: "以上几项都是**启动时**定死的，面板改不了。",
                    },
                } };
            },
            async perfSet(perf) {
                saved = perf;
                return { body: { ok: true, applied: {
                    upcast_attention: "auto", encoder: "h264_nvenc", final_mode: "auto",
                    frames_dtype: "float32", oom_autoretry: true,
                } } };
            },
        },
    });

    await t("openPerfSettings 是顶层函数（弹窗入口在）", () => {
        assert.strictEqual(typeof w.openPerfSettings, "function");
    });

    await w.openPerfSettings();
    const overlay = w.document.querySelector(".h3d-perf-overlay");

    await t("弹窗打开了，且与 AI 优化设置共用同一套外壳", () => {
        assert.ok(overlay, "没有 .h3d-perf-overlay");
        assert.ok(overlay.querySelector(".h3d-opt-dialog"), "没复用 .h3d-opt-dialog 底");
        assert.ok(overlay.querySelector(".h3d-perf-dialog"), "缺 1040px 宽度的专用类");
        assert.ok(perfGetCalls === 1, "打开时该拉一次后端，实际 " + perfGetCalls);
    });

    await t("字段一个不漏：DOM 行数 = 字段表条目数", () => {
        const rows = overlay.querySelectorAll(".h3d-perf-row");
        assert.strictEqual(rows.length, FIELD_KEYS.length,
            "渲染 " + rows.length + " 行，字段表 " + FIELD_KEYS.length + " 条");
    });

    await t("阶段树：一级 5 个，顺序与字段表一致，默认全展开", () => {
        assert.strictEqual(HEADS.length, 5, "一级阶段数不对：" + HEADS.join(","));
        const groups = groupEls(overlay);
        assert.deepStrictEqual(groups.map(titleOf), HEADS, "一级阶段标题或顺序不对");
        for (const g of groups) {
            assert.strictEqual(g.tagName, "DETAILS", "一级阶段该是可折叠的 details");
            assert.strictEqual(g.open, true, "一级阶段默认该展开");
        }
    });

    await t("★ H3_PERF_STAGES 接线：徽章 + 说明真的渲染出来了", () => {
        /* 2026-09-27 修：这张表**定义了但全文件无人消费** —— `when` / `note`
         * 只活在源码注释里，注释承诺的「生效时机徽章」实际不存在。
         * 这条钉住「它被真的渲染出来」，别再退回去。 */
        const groups = groupEls(overlay);
        const badges = overlay.querySelectorAll(".h3d-perf-stage-when");
        const notes = overlay.querySelectorAll(".h3d-perf-stage-note");
        assert.strictEqual(badges.length, groups.length,
            "每个一级阶段都该有一个生效时机徽章，实际 " + badges.length);
        assert.strictEqual(notes.length, groups.length,
            "每个一级阶段都该有一条说明，实际 " + notes.length);
        /* 文案必须**来自阶段表**（不是硬编码在渲染里）—— 逐字比对 */
        for (const g of groups) {
            const name = titleOf(g);
            const st = STAGES[name];
            assert.ok(st, name + " 在阶段表里没有条目");
            assert.strictEqual(g.querySelector(".h3d-perf-stage-when").textContent,
                st.when, name + " 的徽章文案不对");
            assert.strictEqual(g.querySelector(".h3d-perf-stage-note").textContent,
                st.note, name + " 的说明文案不对");
        }
        /* 徽章得在 summary 里（点标题才看得见）、说明得在 summary 外
         * （否则会被算进可点击区、折叠态也会怪） */
        for (const g of groups) {
            assert.ok(g.querySelector("summary .h3d-perf-stage-when"),
                "徽章该在 summary 里");
            assert.ok(g.querySelector("summary .h3d-perf-stage-note") === null,
                "说明不该塞进 summary（会被算进点击区）");
        }
    });

    await t("★ 启动参数体检：复制的是「仅 flags」（桌面 GUI 用），完整命令另收一层", () => {
        /* 2026-09-27 修的真问题：原来的复制按钮给的是 `python main.py --flags` 整行，
         * 而 ComfyUI Desktop 顶部那个「启动参数」框只吃 flags（没有 python、没有
         * main.py）→ 用户根本粘不进去。所以现在主按钮复制 `args`，
         * 完整命令收进折叠层给终端用户。 */
        const hint = overlay.querySelector(".h3d-perf-args-hint");
        assert.ok(hint, "缺「复制到哪」的提示");
        assert.ok(hint.textContent.indexOf("启动参数") >= 0
            && hint.textContent.indexOf("Desktop") >= 0,
            "提示必须说清粘到哪里：" + hint.textContent);
        const pres = [...overlay.querySelectorAll(".h3d-perf-diag pre")]
            .map((p) => p.textContent);
        assert.ok(pres.indexOf("--vram-headroom 1 --use-ck-attention") >= 0,
            "主区该摆仅 flags 的那行，实际 " + JSON.stringify(pres));
        /* ⚠ 不能断言「全局不许出现 main.py」—— 完整命令**本来就含** main.py，
         * 它只是被收进折叠层。要验的是「哪一行在折叠层里」。
         * ⚠ 也不能只按「含 --vram-headroom 且不在 details 里」筛 —— 上面那行
         * 「缺 X / 现在 Y / 建议 Z」的状态清单同样是 `<pre>`。所以按**整行相等**判。 */
        const fullPres = [...overlay.querySelectorAll("details pre")]
            .filter((p) => p.textContent.indexOf("main.py") >= 0);
        assert.strictEqual(fullPres.length, 1,
            "完整命令该有且只有一条、且在折叠层里，实际 " + fullPres.length);
        const bare = [...overlay.querySelectorAll("pre")]
            .filter((p) => !p.closest("details"))
            .map((p) => p.textContent);
        assert.strictEqual(bare.filter(
            (t) => t === "--vram-headroom 1 --use-ck-attention").length, 1,
            "主区该有且只有一条「仅 flags」的可复制行，实际 " + JSON.stringify(bare));
        /* 主按钮文案要指向「启动参数」，不是含糊的「启动命令」 */
        const btns = [...overlay.querySelectorAll(".h3d-perf-diag .h3d-btn")]
            .map((b) => b.textContent);
        assert.ok(btns.indexOf("复制启动参数") >= 0,
            "缺「复制启动参数」按钮，实际 " + JSON.stringify(btns));
        assert.ok(btns.indexOf("复制完整命令") >= 0,
            "缺「复制完整命令」按钮，实际 " + JSON.stringify(btns));
        /* 判定口径要写出来（本机 VRAM 多少）—— 让用户知道建议不是模板套的 */
        assert.ok(overlay.textContent.indexOf("本机 VRAM 6GB") >= 0,
            "该说明「判定口径：本机 VRAM 6GB」");
    });

    await t("一级阶段带「N 项」计数，二级用途齐全且默认展开", () => {
        for (const g of groupEls(overlay)) {
            const name = titleOf(g);
            assert.strictEqual(g.querySelector("summary small").textContent,
                HEAD_COUNT(name) + " 项", name + " 的计数标错了");
            assert.deepStrictEqual(subEls(g).map(titleOf), SUBS_OF(name),
                name + " 的二级用途不对");
            for (const s of subEls(g)) assert.strictEqual(s.open, true, name + " 的子分类默认该展开");
        }
    });

    await t("说明另起一行且 **强调** 转成真加粗（不再露星号）", () => {
        const hints = [...overlay.querySelectorAll(".h3d-perf-hint")];
        assert.ok(hints.length >= 26, "说明行太少：" + hints.length);
        assert.ok(hints.some((h) => h.querySelector("b")), "没有一条说明带加粗强调");
        for (const h of hints) {
            assert.ok(h.innerHTML.indexOf("**") < 0, "说明里还露着字面星号：" + h.textContent.slice(0, 40));
        }
    });

    /* showWhen（按值显隐）：crf / cq 两个质量控件跟着 encoder 切换。
     * 为什么必须测：这机制**只在 DOM 上做**（display:none），后端一无所知 ——
     * 坏了不会有任何报错，只会两个控件的显隐关系悄悄错掉。 */
    await t("按值显隐：crf / cq 跟随 encoder 切换（可逆）", () => {
        const encSel = overlay.querySelector('[data-h3perf-key="encoder"]');
        assert.ok(encSel, "找不到 encoder 下拉");
        const rowOf = (k) => overlay.querySelector('[data-h3perf-key="' + k + '"]')
            .closest(".h3d-perf-row");
        const crfRow = rowOf("x264_crf");
        const cqRow = rowOf("nvenc_cq");
        assert.ok(crfRow && cqRow, "找不到 crf / cq 行");
        /* 初始态跟随桩数据（本用例桩里 encoder=h264_nvenc）：cq 露、crf 隐 */
        assert.strictEqual(cqRow.style.display, "", "NVENC 初始下 cq 该显示");
        assert.strictEqual(crfRow.style.display, "none", "NVENC 初始下 crf 该隐藏");
        /* 切到 libx264 并手动触发 onchange（jsdom 不会自动派发） */
        encSel.value = "libx264";
        encSel.onchange({ target: encSel });
        assert.strictEqual(crfRow.style.display, "", "libx264 下 crf 该显示");
        assert.strictEqual(cqRow.style.display, "none", "libx264 下 cq 该隐藏");
        /* 切回必须还原 —— 单向生效是这类联动最常见的 bug */
        encSel.value = "hevc_nvenc";
        encSel.onchange({ target: encSel });
        assert.strictEqual(crfRow.style.display, "none", "hevc_nvenc 下 crf 该隐藏");
        assert.strictEqual(cqRow.style.display, "", "hevc_nvenc 下 cq 该显示");
    });

    await t("只读诊断：机器现状与 upcast 生效值都摆出来了", () => {
        const diag = overlay.querySelector(".h3d-perf-diag");
        assert.ok(diag, "缺诊断区");
        const txt = diag.textContent;
        assert.ok(txt.indexOf("显存 24.0GB") >= 0, "诊断正文没渲染出来");
        assert.ok(txt.indexOf("Upcast Attention 当前生效：关") >= 0, "缺 upcast 生效状态");
        /* 块级权重流动的现状必须如实显示 —— 机制在 ComfyUI 那边，
         * 不能让用户以为「勾了开关才会有块级流动」。 */
        assert.ok(txt.indexOf("块级权重流动：") >= 0, "缺块级流动现状");
        assert.ok(txt.indexOf("DynamicVRAM 在线") >= 0, "该说明 DynamicVRAM 在线");
        assert.ok(txt.indexOf("0.00GB") >= 0, "该带上当前显存预留读数");
    });

    await t("全表不出现「接线中」徽章（三态机制已删，不该有任何一行灰着）", () => {
        const rows = [...overlay.querySelectorAll(".h3d-perf-row")];
        for (const r of rows) {
            assert.ok(r.textContent.indexOf("接线中") < 0,
                "不该再出现「接线中」：" + r.textContent.slice(0, 48));
        }
        /* 每个字段表条目都渲染成可操作的行；两件套参数除外（由宿主开关决定灰亮，
         * 上面已有专门用例）。这里只钉「有过一整套灰化机制」这件事已经消失。 */
        const alive = rows.find((r) => r.textContent.indexOf("主干采样 OOM 自救") >= 0);
        assert.ok(alive && !alive.querySelector("input").disabled, "普通项被误灰");
        const master = rows.find((r) => r.textContent.indexOf(labelOf("blocks_swap_on")) >= 0);
        assert.ok(master, "找不到块交换总开关行");
        assert.strictEqual(master.querySelector("input").disabled, false, "总开关不该是灰的");
    });

    /* ★ 「跟随档位」现在按**当前值**提示（字段表的 `autoWhen`），不再依赖后端名单 ——
     * 名单已被删除（人工维护的名单必然说谎）。这条钉两个方向：具体值不提示、
     * 哨兵值提示、且改回具体值提示要消失（可逆、且不用重开弹窗）。 */
    await t("「跟随档位」提示跟随数值本身（哨兵 -1 才显示，改回去就消失）", () => {
        const rows = [...overlay.querySelectorAll(".h3d-perf-row")];
        const row = rows.find((r) => r.textContent.indexOf(labelOf("blocks_to_swap")) >= 0);
        assert.ok(row, "找不到「交换块数」行");
        const lab = row.querySelector(".h3d-perf-top label");
        const input = row.querySelector("input");
        assert.ok(lab && input, "行结构变了");
        // 桩数据给的是 25（具体块数）→ 不提示
        assert.ok(lab.textContent.indexOf("跟随档位") < 0,
            "25 是具体块数，不该标「跟随档位」：" + lab.textContent);
        // 改成 -1（哨兵）→ 当场提示
        input.value = "-1";
        input.dispatchEvent(new w.Event("change"));
        assert.ok(lab.textContent.indexOf("跟随档位") >= 0,
            "-1 是「跟随档位」哨兵，label 该跟着值提示：" + lab.textContent);
        // 再改回桩里的原值（25）→ 提示消失，且后面的用例仍能读到「不清零」的原值
        input.value = "25";
        input.dispatchEvent(new w.Event("change"));
        assert.ok(lab.textContent.indexOf("跟随档位") < 0,
            "改回具体块数后提示该消失：" + lab.textContent);
    });

    await t("块交换两件套：总开关默认关 → 交换块数灰；拨开后当场亮（不清零）", () => {
        /* 块交换接线后，它是**普通的两件套**了（不再是「未接线、整组灰」）。
         * 验的仍是「开关一动、名下参数跟着亮/灰」这条契约。 */
        const rows = [...overlay.querySelectorAll(".h3d-perf-row")];
        const master = rows.find((r) => r.textContent.indexOf(labelOf("blocks_swap_on")) >= 0);
        assert.ok(master, "找不到块交换总开关行");
        const mc = master.querySelector("input");
        assert.strictEqual(mc.disabled, false, "已接线的总开关该可点");
        const sub = rows.find((r) => r.textContent.indexOf(labelOf("blocks_to_swap")) >= 0);
        assert.ok(sub, "找不到交换块数行");
        const sc = sub.querySelector("input");
        assert.strictEqual(mc.checked, false, "总开关默认该是关的");
        assert.strictEqual(sc.disabled, true, "总开关关着时子参数该灰");
        assert.strictEqual(sc.value, "25", "子参数该显示桩里的值（不清零）");
        mc.checked = true;
        mc.dispatchEvent(new w.Event("change"));
        assert.strictEqual(sc.disabled, false, "总开关开后子参数该亮");
        assert.strictEqual(sc.value, "25", "拨开开关不该改数值");
    });

    await t("块级预取是独立项：不受块交换总开关影响，默认开", () => {
        /* 为什么必须独立：官方那套块级流动不管我们的总开关开不开都在跑，
         * 预取是它的 lookahead —— 把它挂到总开关下会让用户以为「不开块交换就没预取」。 */
        const rows = [...overlay.querySelectorAll(".h3d-perf-row")];
        const pf = rows.find((r) => r.textContent.indexOf(labelOf("blocks_prefetch")) >= 0);
        assert.ok(pf, "找不到块级预取行");
        const pc = pf.querySelector("input");
        assert.strictEqual(pc.disabled, false, "块级预取该独立可点");
        assert.strictEqual(pc.checked, true, "默认该是开（官方默认即开，不干预）");
    });

    await t("两件套正向联动：拨动总开关，参数控件当场亮 / 灰（不清零）", () => {
        const rows = [...overlay.querySelectorAll(".h3d-perf-row")];
        /* 拿放大网络时序分块验正向：它**已接线**（wired 名单里有），所以总开关可点。
         * 验的是「开关一动，子参数跟着亮 / 灰」这条两件套契约，以及关掉不清零。 */
        const master = rows.find((r) => r.textContent.indexOf("放大网络 3D 时序分块") >= 0);
        assert.ok(master, "找不到放大网络时序分块总开关行");
        const mc = master.querySelector("input");
        assert.strictEqual(mc.disabled, false, "已接线的总开关该可点");
        const sub = rows.find((r) => r.textContent.indexOf("每块帧数") >= 0);
        assert.ok(sub, "找不到「每块帧数」子参数行");
        const sc = sub.querySelector("input");
        sc.value = "24";
        sc.dispatchEvent(new w.Event("change"));
        mc.checked = false;
        mc.dispatchEvent(new w.Event("change"));
        assert.strictEqual(sc.disabled, true, "总开关关掉后子参数该灰");
        assert.strictEqual(sc.value, "24", "关掉开关不该把参数归零");
        mc.checked = true;
        mc.dispatchEvent(new w.Event("change"));
        assert.strictEqual(sc.disabled, false, "总开关开回来子参数该亮");
    });

    await t("保存契约：改动攒草稿，点保存才 POST", async () => {
        const box = overlay.querySelectorAll(".h3d-perf-row input[type=number]")[0];
        assert.ok(box, "找不到数字输入（ff_chunk_tokens）");
        box.value = "8192";
        box.dispatchEvent(new w.Event("change"));
        assert.strictEqual(saved, null, "还没点保存就发出去了");

        const save = overlay.querySelector(".h3d-opt-save");
        assert.strictEqual(save.disabled, false, "读取成功后保存按钮该可用");
        save.dispatchEvent(new w.Event("click"));
        await new Promise((r) => setTimeout(r, 20));
        assert.ok(saved, "点了保存却没提交");
        assert.strictEqual(saved.ff_chunk_tokens, 8192, "草稿里的改动没带上");
        assert.ok(overlay.querySelector(".h3d-perf-status").textContent.indexOf("已应用") >= 0,
            "缺保存结果回执");
    });

    await t("保存 / 关闭按钮固定在滚动区外（不用滚到底才点得到）", () => {
        const body = overlay.querySelector(".h3d-perf-body");
        const foot = overlay.querySelector(".h3d-perf-foot");
        assert.ok(body && foot, "缺正文区或固定底栏");
        assert.ok(body.querySelector(".h3d-opt-actions") === null, "按钮还埋在滚动区里");
        /* 只认类名与摆放：保存按钮的文字会短暂变成「✓ 已应用」（上一条刚点过），
         * 拿瞬时文案做断言会假失败。 */
        const btns = [...overlay.querySelectorAll(".h3d-perf-foot .h3d-btn")].map((b) => b.className);
        assert.strictEqual(btns.length, 2, "底栏该有两个按钮：" + btns.join("/"));
        assert.ok(/h3d-opt-save/.test(btns[1]), "底栏第二个该是保存：" + btns[1]);
        assert.strictEqual(overlay.querySelector(".h3d-perf-foot .h3d-btn").textContent, "关闭");
        const css = SRC.slice(SRC.indexOf(".h3d-perf-dialog{"));
        const oneLine = css.slice(0, css.indexOf("}"));
        assert.ok(oneLine.indexOf("overflow:hidden") >= 0, "弹窗自身还在滚 → 按钮位置又会跟着内容跑");
        assert.ok(oneLine.indexOf("flex-direction:column") >= 0, "缺列布局（标题/正文/底栏三段）");
        assert.ok(oneLine.indexOf("1040px") >= 0, "宽度回退成窄框了");
    });

    await t("Esc 能关掉弹窗", () => {
        overlay.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape" }));
        assert.ok(!w.document.querySelector(".h3d-perf-overlay"), "Esc 没关掉");
    });

    await t("接口不可用时不摆空勾选（保存直接禁用）", async () => {
        const r2 = load({ H3Api: {} });
        await r2.w.openPerfSettings();
        const ov = r2.w.document.querySelector(".h3d-perf-overlay");
        assert.ok(ov, "即使接口缺失，弹窗也该开出来说明原因");
        assert.ok(ov.textContent.indexOf("不可用") >= 0, "没说清为什么不可用");
        assert.strictEqual(ov.querySelector(".h3d-opt-save").disabled, true, "这时不该给可点的保存");
        assert.strictEqual(ov.querySelectorAll(".h3d-perf-row").length, 0, "不该摆出空开关");
    });

    await t("两级都能收起，且收起态被记住（重开弹窗不弹回）", async () => {
        await w.openPerfSettings();
        const box1 = w.document.querySelector(".h3d-perf-overlay");
        const g1 = groupEls(box1)[0];
        const head1 = titleOf(g1);
        const subs1 = subEls(g1);
        assert.ok(subs1.length >= 1, head1 + " 该至少有一个子分类");
        subs1[0].open = false;                     // 收起第一个子分类
        subs1[0].dispatchEvent(new w.Event("toggle"));
        g1.open = false;                           // 连整个一级阶段一起收起
        g1.dispatchEvent(new w.Event("toggle"));
        box1.querySelector(".h3d-perf-foot .h3d-btn").dispatchEvent(new w.Event("click"));
        assert.ok(!w.document.querySelector(".h3d-perf-overlay"), "关闭没生效");

        /* 弹窗每次打开都整块重建 —— 收起态得靠 foldSection 的 _foldState 撑住 */
        await w.openPerfSettings();
        const box2 = w.document.querySelector(".h3d-perf-overlay");
        const g2 = groupEls(box2).find((g) => titleOf(g) === head1);
        assert.strictEqual(g2.open, false, "一级阶段的收起态没记住，重开就弹回来了");
        assert.strictEqual(subEls(g2)[0].open, false, "子分类的收起态没记住");
        assert.strictEqual(subEls(g2)[1].open, true, "没收起的兄弟分类被连累了");
        box2.querySelector(".h3d-perf-foot .h3d-btn").dispatchEvent(new w.Event("click"));
    });

    results.forEach((r) => console.log(r));
    if (errors.length) { console.log("\n页面错误: " + errors.join(" | ")); ok = false; }
    console.log(ok ? "\nperf_modal_check 全部通过" : "\nperf_modal_check 失败");
    process.exit(ok ? 0 : 1);
})();
