/* 有「参考视频」时，官方标签编号 / 素材识别 / 回填 必须仍然对得上（2026-10-17 回归）。
 *
 * 用户报的现象（截图）：池里挂了 1 条视频 + 2 张 turnaround 图，提示词框里
 *   ① 正文的 `<Video 1>` 被标成"挂不到素材"的红框 —— 明明挂着视频；
 *   ② 两张 turnaround 的描述与名字对调（"顺序错乱/认错素材"）；
 *   ③ 框内 `<Picture k>` 的缩略图不是那张图。
 *
 * 两个真根因（都在前端）：
 *   A. v2RefsFromSchedule 用 `x.label === ref` 匹配池条目，而 seg.refs 存的是
 *      **引用名（ref_name，含后缀）** → 一条都匹配不上 → kind 全回落 image →
 *      视频被算进 <Picture k>（整体错位一格）+ 映射表里没有 <Video 1>（渲染成红框）。
 *   B. collectSegMedia 拿 optImageToDataUrl 取视频封面，而它只收 image/*，
 *      于是参考视频被 `continue` 整条丢掉：media 里没有、名单里没有、
 *      `<Video 1>` 回填时译不回来（原样落盘成红框/死引用）。
 *
 * jsdom 真跑：抽 v2RefsFromSchedule / collectSegMedia / fromLLMText + h3_prompts。
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
const promptsSrc = fs.readFileSync(path.join(ROOT, "web", "h3_prompts.js"), "utf8");

function extractFn(name) {
    const re = new RegExp("^\\s*(?:async\\s+)?function " + name + "\\(", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到函数 " + name);
    const i = src.indexOf("{", m.index);
    let depth = 0;
    for (let j = i; j < src.length; j++) {
        if (src[j] === "{") depth++;
        else if (src[j] === "}") { depth--; if (!depth) return src.slice(m.index, j + 1); }
    }
    throw new Error("大括号不配平 " + name);
}

function extractConst(name) {
    const re = new RegExp("^\\s*const " + name + " = [\\s\\S]*?;\\s*$", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到常量 " + name);
    return m[0];
}

const dom = new JSDOM("<!doctype html><html><body></body></html>", { runScripts: "outside-only" });
dom.window.eval(promptsSrc);          // 装 window.H3Prompts（标注编解码的唯一实现）
dom.window.eval(`
    window.__failFile = "";           // 让某一张图的预览取不到（模拟裂图/取不到）
    async function optImageToDataUrl(url) {
        if (window.__failFile && String(url).includes(window.__failFile)) return null;
        return url && /\\.(png|jpg|jpeg|webp)$/i.test(url) ? "data:image/png;base64," + url : null;
    }
    async function optVideoFrameToDataUrl(url) {
        return url ? "data:image/jpeg;base64,FRAME:" + url : null;
    }
    function assetPreviewUrl(dir, file, aid) { return "PREV:" + String(file || ""); }
    function getDirValue() { return "PROJ"; }
    function refvidAudioOn() { return true; }
`);
dom.window.eval([
    extractConst("refKeyOf"),
    extractConst("KIND_LIST"),
    extractConst("KIND_TOKEN"),
    extractFn("frameNameOf"),
    extractFn("markTextOf"),
    extractFn("v2RefsFromSchedule"),
    extractFn("collectSegMedia"),
    extractFn("fromLLMText"),
    ";window.v2RefsFromSchedule = v2RefsFromSchedule;",
    "window.collectSegMedia = collectSegMedia;",
    "window.fromLLMText = fromLLMText;",
].join("\n"));

const { v2RefsFromSchedule, collectSegMedia, fromLLMText } = dom.window;

let bad = 0;
function ok(cond, msg, got, want) {
    if (cond) return;
    bad++;
    console.log("  FAIL " + msg);
    if (arguments.length > 3) {
        console.log("    期望: " + JSON.stringify(want));
        console.log("    实际: " + JSON.stringify(got));
    }
}

const VIDEO_REF = "01_00-00.000_to_00-05.500.mp4";
const BILLY_REF = "billy-butcher-turnaround.png";
const HOME_REF = "homelander-turnaround-no-cape.png";

/* 池：视频 + 两张 turnaround（label 是别名=无后缀，ref_name 才是引用名） */
const POOL = [
    { file: "assets/v.mp4", kind: "video", label: "01_00-00.000_to_00-05.500",
      ref_name: VIDEO_REF, mark: "视频1", asset_id: "" },
    { file: "assets/b.png", kind: "image", label: "billy-butcher-turnaround",
      ref_name: BILLY_REF, mark: "图片1", asset_id: "" },
    { file: "assets/h.png", kind: "image", label: "homelander-turnaround-no-cape",
      ref_name: HOME_REF, mark: "图片2", asset_id: "" },
];
const pool = () => POOL.map((a) => ({ ...a }));

function dsOf(refs, extraAssets) {
    return {
        prompts: ["x"],
        ref_assets: pool().concat(extraAssets || []),
        segments: [{ refs: refs.slice() }],
    };
}

(async () => {
    /* ---------- 1) 有视频时，<Video k> 与 <Picture k> 各自独立编号 ---------- */
    {
        const out = v2RefsFromSchedule(dsOf([VIDEO_REF, BILLY_REF, HOME_REF]), 0);
        const got = out.map((r) => [r.label, r.src]);
        const want = [["<Video 1>", VIDEO_REF], ["<Picture 1>", BILLY_REF],
            ["<Picture 2>", HOME_REF]];
        ok(JSON.stringify(got) === JSON.stringify(want),
            "视频占 <Video 1>，图片仍从 <Picture 1> 起（不整体错位）", got, want);
    }

    /* ---------- 2) 旧档 refs 存的是别名（无后缀）也要认 ---------- */
    {
        const out = v2RefsFromSchedule(dsOf(["01_00-00.000_to_00-05.500", BILLY_REF]), 0);
        ok(out[0] && out[0].label === "<Video 1>",
            "refs 用别名写时，视频仍判为 <Video 1>", out.map((r) => r.label), ["<Video 1>", "<Picture 1>"]);
    }

    /* ---------- 3) 重复引用只占一个号；视频音轨先占一个 <Audio j> ---------- */
    {
        const ds = dsOf([VIDEO_REF, "voice", VIDEO_REF], [
            { file: "assets/a.wav", kind: "audio", label: "voice",
              ref_name: "voice.wav", mark: "音频1", asset_id: "" },
        ]);
        const out = v2RefsFromSchedule(ds, 0);
        const got = out.map((r) => r.label);
        ok(JSON.stringify(got) === JSON.stringify(["<Video 1>", "<Audio 2>"]),
            "重复引用不重复占号；视频音轨预扣 → 独立音频是 <Audio 2>", got, ["<Video 1>", "<Audio 2>"]);
    }

    /* ---------- 4) collectSegMedia：视频必须真的挂进 media（带首帧） ---------- */
    {
        const r = await collectSegMedia(null, dsOf([VIDEO_REF, BILLY_REF, HOME_REF]), 0);
        const got = r.media.map((m) => [m.kind, m.label, (m.images || []).length]);
        const want = [["video", "视频1", 1], ["image", "图片1", 1], ["image", "图片2", 1]];
        ok(JSON.stringify(got) === JSON.stringify(want),
            "参考视频进 media（kind=video + 首帧图），不再整条丢失", got, want);
        ok(/@视频1（视频 = <Video 1>）/.test(r.note), "名单里点名视频，并给出它的 <Video k>",
            r.note);
        ok(/@图片1（图片 = <Picture 1>）/.test(r.note) && /@图片2（图片 = <Picture 2>）/.test(r.note),
            "图片的 <Picture k> 编号与 media 顺序同口径", r.note);
    }

    /* ---------- 5) 某张图取不到预览时，编号不许塌陷 ---------- */
    {
        dom.window.__failFile = "h.png";
        const r = await collectSegMedia(null, dsOf([VIDEO_REF, BILLY_REF, HOME_REF]), 0);
        dom.window.__failFile = "";
        const got = r.media.map((m) => [m.label, (m.images || []).length]);
        const want = [["视频1", 1], ["图片1", 1], ["图片2", 0]];
        ok(JSON.stringify(got) === JSON.stringify(want),
            "取不到画面的那张仍占位（否则 <Picture k> 会整体前移）", got, want);
    }

    /* ---------- 6) 端到端：LLM 写官方标签 → 回填成正确的 @素材名 ---------- */
    {
        const r = await collectSegMedia(null, dsOf([VIDEO_REF, BILLY_REF, HOME_REF]), 0);
        const llm = "subject_definitions:\n"
            + "<Subject 1> is the replacement for the man in <Video 1>. "
            + "His appearance comes only from <Picture 1>.\n"
            + "<Subject 2> is the replacement for the glasses man in <Video 1>. "
            + "His appearance comes only from <Picture 2>.";
        const got = fromLLMText(llm, pool(), r.media);
        ok(got.includes("in @" + VIDEO_REF + "."), "<Video 1> → @视频引用名（不再留红框）", got);
        ok(got.includes("only from @" + BILLY_REF + "."),
            "<Picture 1> → 第一张图（billy）", got);
        ok(got.includes("only from @" + HOME_REF + "."),
            "<Picture 2> → 第二张图（homelander），两张不许对调", got);
        ok(!/<(?:Picture|Video)\s+\d+>/.test(got), "回填后不该残留官方标签", got);
    }

    console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
    process.exit(bad ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
