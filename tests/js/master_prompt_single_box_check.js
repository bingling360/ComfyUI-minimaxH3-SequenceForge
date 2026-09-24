/* 总提示词工作台**单框**回环（三框已下线）。
 *
 * 背景：工作台从三框（① 意图 / ② 剧本 / ③ 结果）退化为单框——框里就是直接进
 * 模型的提示词，用 [Segment N] 分段。三框那套「三框按段号同序合成一份文本」的逻辑
 * （mpSplitBox / mpComposeBoxes）随之删除；这个用例改钉单框的**往返口径**：
 *
 *   框里写的文本 → parseMasterPrompt → mpRenderState → 再解析
 *
 * 必须稳定（幂等），否则「AI分段提示词优化」写回一次就把用户的文本洗变形了。
 * 另外锁死两条老 bug 的回归：连续正文行不许被拼成一行、官方三字段的空行不能吃掉。
 *
 * jsdom 真跑：从 h3_director.js 抽出 MP_* 与 mpReflow / parseMasterPrompt /
 * mpRenderState，不依赖 DOM。
 */
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");

let JSDOM;
try {
    ({ JSDOM } = require("jsdom"));
} catch (e) {
    console.error("SKIP: 未安装 jsdom");
    process.exit(2);
}

const src = fs.readFileSync(path.join(ROOT, "web", "h3_director.js"), "utf8");

function extractConst(name) {
    const re = new RegExp("^\\s*const " + name + " = [^;]*;", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到常量 " + name);
    return m[0];
}

function extractFn(name) {
    const re = new RegExp("^\\s*function " + name + "\\(", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到函数 " + name);
    const i = src.indexOf("{", m.index);
    let depth = 0;
    for (let j = i; j < src.length; j++) {
        if (src[j] === "{") depth++;
        else if (src[j] === "}") { depth--; if (!depth) return src.slice(m.index, j + 1); }
    }
    throw new Error("大括号不配平: " + name);
}

const parts = [
    extractConst("MP_HEAD_RE"), extractConst("MP_END_RE"),
    extractConst("MP_FIELD_RE"), extractConst("MP_FIELDS"),
    extractConst("MP_YES"), extractConst("MP_NO"), extractConst("MP_HEAD_SUB_RE"),
    extractConst("MP_BODY_KEYS"),
    extractFn("mpReflow"), extractFn("newMasterSeg"),
    extractFn("parseMasterPrompt"), extractFn("mpRenderState"),
].join("\n");

const dom = new JSDOM("<!doctype html><html><body></body></html>", { runScripts: "outside-only" });
dom.window.eval(parts);

const run = (code) => dom.window.eval(code);
const parse = (t) => run("parseMasterPrompt(" + JSON.stringify(t) + ")");
const render = (segs) => run("mpRenderState(" + JSON.stringify(segs) + ")");
let bad = 0;
const check = (name, cond, extra) => {
    if (cond) { console.log("  ok   " + name); return; }
    bad++;
    console.log("  FAIL " + name);
    if (extra !== undefined) console.log("       " + extra);
};

const OFFICIAL = "integrated_multimodal_description: [Shot 1] 实拍、电影感，中景。\n"
    + "[Shot 2] At 00:03.200，镜头小幅慢速推近。\n\n"
    + "overall_soundscape: 雨声持续。\n\nnon_diegetic_music: N/A";

/* ---------- 1. 单段：不带段头也能解析 ---------- */
{
    const p = parse(OFFICIAL);
    check("单段段数=1", p.segs.length === 1, String(p.segs.length));
    check("无段头不告警", p.notes.length === 0, JSON.stringify(p.notes));
    check("单段正文原样还原（含内部空行）", p.segs[0].main === OFFICIAL,
        JSON.stringify(p.segs[0].main));
}

/* ---------- 2. 多段：段级标签 + 正文按 [Segment N] 同序 ---------- */
{
    /* 「参考：」是上一代的资产引用通道，已停用：认出来 → 丢弃 → 进 notes。
     * 关键是它**不能并进正文**（否则把不该进模型的清单送进模型），也不能静默消失。 */
    const text = "[Segment 1]\nDuration: 9\nReference: 角色1\n\n" + OFFICIAL
        + "\n\n[Segment 2]\nDuration: 12\nStandalone: yes\nReference: 角色1\n\n"
        + "integrated_multimodal_description: [Shot 1] 乙";
    const p = parse(text);
    check("多段段数=2", p.segs.length === 2, String(p.segs.length));
    const a = p.segs[0] || {}, b = p.segs[1] || {};
    check("段1 时长=9", Number(a.seconds) === 9, String(a.seconds));
    check("段2 时长=12", Number(b.seconds) === 12, String(b.seconds));
    check("参考不再产出 refs 字段", a.refs === undefined && b.refs === undefined,
        JSON.stringify([a.refs, b.refs]));
    check("参考没被并进正文", String(a.main || "").indexOf("角色1") < 0,
        JSON.stringify(a.main));
    check("参考被点名提示", p.notes.some((n) => n.indexOf("参考") >= 0), JSON.stringify(p.notes));
    check("段2 独立镜头=是", b.unlink === true, String(b.unlink));
    check("段1 官方三字段原样", a.main === OFFICIAL, JSON.stringify(a.main));
    check("段2 正文含官方字段头", String(b.main || "").indexOf("integrated_multimodal_description") === 0,
        JSON.stringify(b.main));
}

/* ---------- 3. 空段 → 渲染出 Prompt: → 回读为显式空串（= 清空该段） ---------- */
{
    const p = parse("[Segment 1]\n");
    check("空段头 → main 未定义（分配时按空串 = 清空）",
        p.segs[0] && p.segs[0].main === undefined,
        JSON.stringify(p.segs[0] && p.segs[0].main));
    const t = render([{ seconds: null, unlink: false }]);
    check("渲染空段会写出 Prompt: 标签", t.indexOf("Prompt:") >= 0, JSON.stringify(t));
    check("渲染→回读 = 显式空串（不是 undefined）", parse(t).segs[0].main === "",
        JSON.stringify(parse(t).segs[0].main));
}

/* ---------- 4. 连续正文行不许被拼成一行（pushBody 换行口径回归） ---------- */
{
    const text = "[Segment 1]\nPrompt:\n甲行一\n甲行二\n\n乙段\n\noverall_soundscape: 雨声。";
    const p = parse(text);
    const s = p.segs[0] || {};
    check("连续正文行保留换行", String(s.main || "").indexOf("甲行一\n甲行二") === 0,
        JSON.stringify(s.main));
    check("字段之间的空行仍保留", String(s.main || "").indexOf("\n\noverall_soundscape") > 0,
        JSON.stringify(s.main));
}

/* ---------- 5. 单框往返幂等：文本 → 解析 → 渲染 → 再解析 ---------- */
{
    const text = "[Segment 1]\nDuration: 9\n\n" + OFFICIAL
        + "\n\n[Segment 2]\nDuration: 8\nStandalone: yes\n\n"
        + "integrated_multimodal_description: [Shot 1] 乙\n\nnon_diegetic_music: N/A";
    const p1 = parse(text);
    const t2 = render(p1.segs);
    const p2 = parse(t2);
    const t3 = render(p2.segs);
    check("渲染→再解析 段数一致", p2.segs.length === p1.segs.length,
        `${p1.segs.length} -> ${p2.segs.length}`);
    check("渲染→再解析 正文一致",
        JSON.stringify(p2.segs.map((s) => s.main)) === JSON.stringify(p1.segs.map((s) => s.main)),
        JSON.stringify({ a: p1.segs.map((s) => s.main), b: p2.segs.map((s) => s.main) }));
    check("渲染→再解析 时长/独立镜头一致",
        JSON.stringify(p2.segs.map((s) => [s.seconds, s.unlink]))
        === JSON.stringify(p1.segs.map((s) => [s.seconds, s.unlink])),
        JSON.stringify(p2.segs.map((s) => [s.seconds, s.unlink])));
    check("第二轮渲染文本稳定（幂等）", t3 === t2, JSON.stringify({ t2, t3 }));
    check("往返无告警", p2.notes.length === 0, JSON.stringify(p2.notes));
}

/* ---------- 6. 意图 / 剧本 / 参考 不再是单框格式的一部分 ---------- */
{
    const segs = [{ main: OFFICIAL, seconds: 9, unlink: false,
        intent: "雨夜市场", script: "镜头一：她回头", refs: ["角色1"] }];
    const t = render(segs);
    check("渲染不再写 意图 / 剧本 / 参考",
        t.indexOf("意图：") < 0 && t.indexOf("剧本：") < 0 && t.indexOf("参考：") < 0,
        JSON.stringify(t));
    const p = parse("[Segment 1]\nIntent: 雨夜市场\nScript: 镜头一\nPrompt:" + OFFICIAL);
    check("解析丢弃 意图", p.segs[0] && p.segs[0].intent === undefined,
        JSON.stringify(p.segs[0] && p.segs[0].intent));
    check("解析丢弃 剧本", p.segs[0] && p.segs[0].script === undefined,
        JSON.stringify(p.segs[0] && p.segs[0].script));
    check("剧本内容没被并进提示词", p.segs[0] && p.segs[0].main === OFFICIAL,
        JSON.stringify(p.segs[0] && p.segs[0].main));
    check("丢弃要说出来（进 notes）", p.notes.some((n) => n.indexOf("已忽略") >= 0),
        JSON.stringify(p.notes));
}

/* ---------- 7. 三框合成 / AI 工作台已下线（防止有人把旧实现加回来） ---------- */
{
    check("mpSplitBox 已删除", !/function mpSplitBox\s*\(/.test(src));
    check("mpComposeBoxes 已删除", !/function mpComposeBoxes\s*\(/.test(src));
    check("三框工厂 mkBox 已删除", !/const mkBox = /.test(src));
    check("多段扩写入口已下线", src.indexOf("✨ AI扩写 → 剧本") < 0);
    /* 总提示词框不做 AI：分段优化 / 模式下拉 / 参考图勾选 全部撤下 */
    check("AI 分段提示词优化已下线", src.indexOf("✨ AI分段提示词优化") < 0);
    check("模式下拉（MP_MODE_HINT）已下线", !/const MP_MODE_HINT/.test(src));
    check("参考素材 chips（collectMasterMedia）已下线",
        !/function collectMasterMedia\s*\(/.test(src));
    check("打开即自动载入已下线", !/if \(\(\(getDs\(node\)\.prompts/.test(src));
}

console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
process.exit(bad ? 1 : 0);
