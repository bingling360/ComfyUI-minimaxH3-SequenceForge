/* 跨项目作用域守卫（jsdom 真跑 shipped 源码 + 静态登记表）。
 *
 * 背景：导演台的活状态是画布 widget 里那一个 ds，**它本身不分项目**；项目只是
 * manifest 的一份局部快照。于是凡是不进 manifest、又不带项目作用域的状态，都会
 * 在切项目时串味 —— 线下报的「原稿/优化稿切换不跟着项目换」就是这么来的。
 *
 * 本文件钉两件事：
 *   A. 行为：原稿/优化稿的缓存与历史**按项目目录分作用域**（optKey），
 *      切项目后读到的是新项目自己的记录；
 *   B. 登记：h3_director.js 里所有模块级可变状态必须登记在 REGISTRY 里，
 *      并标明作用域；标为 dir 的还要求键表达式里真的带项目目录。
 *      B 是"绊线"不是"证明"：它拦的是"新加了一个模块级 Map 却没想过作用域"
 *      和"有人把键里的 dir 摘掉了"，这两类正是已经踩过的坑。
 *
 * 跑法：NODE_PATH=<repo>/node_modules node tests/js/cross_project_scope_check.js
 */
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");

let harness;
try {
    harness = require("./_harness.js");
} catch (e) {
    console.error("SKIP: 未安装 jsdom（npm install）");
    process.exit(2);
}

let fails = 0;
function ok(cond, msg) {
    if (cond) return;
    fails += 1;
    console.log("FAIL:", msg);
}

/* ---------- A. 行为：原稿/优化稿按项目分作用域 ---------- *
 * 只看**用户看得见的那一面**：paintOptbar 画出来的「原稿 / 优化稿」按钮。
 * （不直接读 _optShown 这些 Map —— 顶层 const 在间接 eval 里落在 eval 自己的
 *  作用域，外部取不到；而按钮文本正是线下报的那个症状。）
 */
{
    const ds = {
        prompts: ["", ""],
        segments: [{}, {}],
        ref_assets: [],
        /* 两个项目各有自己的原稿/优化稿记录，段号相同（都是第 0 段）——
         * 键里不带 dir 时，第二个项目会读到第一个项目的记录。 */
        opt_hist: {
            PROJ_A: { 0: { before: "A 的原稿", after: "A 的优化稿", shown: "after" } },
            PROJ_B: { 0: { before: "B 的原稿", after: "B 的优化稿", shown: "before" } },
        },
    };
    const { w, errors } = harness.load({ node: harness.mkNode(ds) });
    const node = harness.mkNode(ds);
    const paintOptbar = w.eval("paintOptbar");
    const purge = w.eval("optPurgeOtherScopes");
    const setDir = w.eval("setDirValue");
    const getDir = w.eval("getDirValue");
    const fakeTa = { value: "" };
    /* 画一次工具条，取第一个按钮（就是原稿/优化稿切换钮） */
    const btn = (n, d) => {
        const box = w.document.createElement("div");
        paintOptbar(box, n, { ds: d, node: n }, 0, fakeTa);
        const b = box.querySelector("button");
        return b ? { text: String(b.textContent), hidden: b.style.display === "none" } : null;
    };

    ok(getDir(node) === "PROJ", "夹具项目的存档目录应为 PROJ");
    setDir(node, "PROJ_A");
    ok((btn(node, ds) || {}).text === "原稿",
        `A 项目 shown=after → 按钮应显示「原稿」，实际 ${JSON.stringify(btn(node, ds))}`);

    /* 切到 B：旧作用域的缓存必须清掉，且读到的是 B 自己的记录（shown=before → 「优化稿」） */
    setDir(node, "PROJ_B");
    purge("PROJ_B");
    const after = btn(node, ds);
    ok(after && !after.hidden && after.text === "优化稿",
        `B 项目 shown=before → 按钮应显示「优化稿」且可见，实际 ${JSON.stringify(after)}`);

    /* 再切回 A：A 的记录还在（历史按项目分桶，切回去要能还原） */
    setDir(node, "PROJ_A");
    purge("PROJ_A");
    const back = btn(node, ds);
    ok(back && back.text === "原稿", `切回 A 应还原成 after 状态，实际 ${JSON.stringify(back)}`);

    /* 没有原稿历史的项目：按钮必须隐藏（不能拿上一个项目的状态顶） */
    setDir(node, "PROJ_EMPTY");
    purge("PROJ_EMPTY");
    const none = btn(node, ds);
    ok(none && none.hidden, `没有历史的项目按钮应隐藏，实际 ${JSON.stringify(none)}`);

    /* 旧的扁平结构 {idx: 记录} 没有 dir，无从归属（正是串味来源）→ 丢弃 */
    const flat = { prompts: [""], segments: [{}], ref_assets: [],
        opt_hist: { 0: { before: "老结构", after: "老结构优", shown: "after" } } };
    const n2 = harness.mkNode(flat);
    setDir(n2, "PROJ_A");
    const flatBtn = btn(n2, flat);
    ok(flat.opt_hist[0] === undefined, "旧扁平结构的条目应被丢弃");
    ok(flatBtn && flatBtn.hidden, `旧结构不该再点亮按钮，实际 ${JSON.stringify(flatBtn)}`);

    const real = (errors || []).filter((e) => !/Not implemented|jsdom/i.test(String(e)));
    ok(real.length === 0, "运行时报错：" + JSON.stringify(real));
}

/* ---------- B. 静态登记表：模块级可变状态必须声明作用域 ---------- */
{
    /* scope 的含义：
     *   ui      —— 纯界面态（折叠/尺寸/引用节点），跨项目存活无害
     *   derived —— 由当前 node / dir 派生的瞬时值，刷新周期会重算
     *   const   —— new Set/{} 字面量常量查表，声明后只读
     *   dir     —— 按段号或路径缓存的表；键**必须**带项目目录，否则跨项目串味。
     *              key 填"键表达式里必须出现的片段"，用于拦住"有人把 dir 摘了"。
     */
    const REGISTRY = {
        MP_BODY_KEYS: { scope: "const" },
        H3_SECTION_KEYS: { scope: "const" },
        _refTpl: { scope: "dir", key: "tplKey" },
        _cardPainters: { scope: "ui" },
        miniBox: { scope: "ui" },
        desk: { scope: "ui" },
        pendingReset: { scope: "ui" },
        refreshTimer: { scope: "derived" },
        ledPhase: { scope: "ui" },
        ledText: { scope: "ui" },
        apiErrorText: { scope: "ui" },
        lastDir: { scope: "derived" },
        dragSeg: { scope: "ui" },
        _taTimers: { scope: "derived" },      // 防抖定时器：切项目时 flushPendingEdits 清空
        _segTab: { scope: "dir", key: `}|seg` },
        _taPending: { scope: "derived" },     // 同上：切换前被 flush 掉，队列必为空
        _uiGen: { scope: "derived" },
        _v2Open: { scope: "dir", key: "v2Key(" },
        _optBefore: { scope: "dir", key: "optKey(" },
        _optAfter: { scope: "dir", key: "optKey(" },
        _optShown: { scope: "dir", key: "optKey(" },
        _optBusy: { scope: "derived" },
        _selSeg: { scope: "derived" },
        promptFlushTimer: { scope: "derived" },
        _deskZoneErrs: { scope: "ui" },
        _foldState: { scope: "ui" },
        _promptEditors: { scope: "dir", key: "getDirValue(node)}|" },
        _poolRev: { scope: "derived" },
        _poolDir: { scope: "derived" },
        fabEl: { scope: "ui" },
    };

    const src = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8");
    /* 收两类"顶层状态"：
     *   · let / var —— 可变绑定，天生要问一句"它跨项目吗"
     *   · const 且初值是容器（Map/Set/[]/{}）/ null —— 会被填、被清、被缓存
     * 常量（数字/字符串/正则/纯函数引用）不收，它们不会在两个项目之间攒状态。 */
    const declared = new Set();
    const re = /^(const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^\n]*)/gm;
    let m;
    while ((m = re.exec(src))) {
        const kind = m[1];
        const name = m[2];
        const init = m[3];
        const container = /new Map\(|new Set\(|^\{\}|^\[\]|^null\b/.test(init.trim());
        if (kind === "const" && !container) continue;
        declared.add(name);
    }
    /* 正则字面量会被误收（如 /^x/gm 里的 g），按已知清单要求登记，多余的不追究 */
    const unregistered = [...declared].filter((n) => !(n in REGISTRY));
    ok(unregistered.length === 0,
        "模块级可变状态没登记作用域（新增状态请补进 REGISTRY 并想清楚它跨不跨项目）："
        + JSON.stringify(unregistered));

    const missing = Object.keys(REGISTRY).filter((n) => !declared.has(n));
    ok(missing.length === 0,
        "REGISTRY 里的名字在源码里找不到了（改名后请同步，别让守卫变成摆设）："
        + JSON.stringify(missing));

    const noKey = Object.entries(REGISTRY)
        .filter(([_n, cfg]) => cfg.scope === "dir" && cfg.key && !src.includes(cfg.key))
        .map(([n]) => n);
    ok(noKey.length === 0,
        "这些按段号缓存的表，键里没找到项目目录片段（跨项目会串味）：" + JSON.stringify(noKey));

    /* 直白钉一次最要命的形态：键只剩 node.id:idx */
    ok(!src.includes("`${node.id}:${idx}`"),
        "还有键写成 `${node.id}:${idx}` 的地方 —— 不带项目目录的键会跨项目串味");
}

if (fails) {
    console.log(`\n${fails} 个断言失败`);
    process.exit(1);
}
console.log("OK: 跨项目作用域（原稿/优化稿按项目分桶 + 模块级状态已登记）");
