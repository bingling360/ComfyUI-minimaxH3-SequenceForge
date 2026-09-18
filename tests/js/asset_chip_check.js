/* 素材标识 / 池子合并 真跑检查（node + jsdom，抽 shipped 源码执行）。
 *
 * 跑法：NODE_PATH=<repo>/node_modules node tests/js/asset_chip_check.js
 *
 * 覆盖本轮三个线下 bug：
 *  ① chipLabelText：显示层剥掉夹带的格式后缀（.png/.mp4…），但不误伤 v1.0。
 *  ② poolFromManifest：**有 asset_id 但 file 回填失败**的全局库链接条目
 *     必须留在池子里（旧实现按 file 过滤 → 「视频/音频/部分图片不出现在引用条」）。
 *  ③ assetPreviewUrl + buildAssetThumb：全局库相对路径（images/ videos/ audios/）
 *     不得回落到 input 目录（旧实现 → 缩略图张冠李戴）；图/视给缩略图、音频给音符。
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
    throw new Error("大括号不配平 " + name);
}

/** 抓完整 const 语句（到第一个分号）——正则常量 / 箭头函数都能取。 */
function extractConst(name) {
    const re = new RegExp("^const " + name + " = ", "m");
    const m = re.exec(src);
    if (!m) throw new Error("未找到常量 " + name);
    const end = src.indexOf(";", m.index);
    if (end < 0) throw new Error("常量 " + name + " 没有分号结尾");
    return src.slice(m.index, end + 1);
}

const code = [
    extractConst("KIND_LIST"),
    "const KIND_NAME = { image: \"图片\", video: \"视频\", audio: \"音频\" };",
    "const KIND_ICON = { image: \"🖼\", video: \"🎞\", audio: \"🎵\" };",
    extractConst("_CHIP_EXT_RE"),
    extractFn("chipLabelText"),
    extractConst("_LIB_REL_RE"),
    extractFn("viewUrl"),
    extractFn("inputViewUrl"),
    extractFn("assetPreviewUrl"),
    extractFn("buildAssetThumb"),
    extractConst("_rolesOf"),
    /* poolFromManifest 依赖引用名归一化 + 标注兜底（B1/B2）——
     * 抽函数时漏了它们会直接 ReferenceError，别只补一个。 */
    extractConst("REF_NAME_MAX"),
    extractFn("cleanRefName"),
    extractConst("refKeyOf"),
    extractConst("markOf"),
    extractFn("assignMarks"),
    extractFn("poolFromManifest"),
].join("\n");

const dom = new JSDOM("<!doctype html><html><body></body></html>", { pretendToBeVisual: true });
const { window } = dom;

const make = new Function("window", "document",
    code + "\nreturn { chipLabelText, assetPreviewUrl, buildAssetThumb, poolFromManifest," +
    " KIND_ICON, KIND_NAME };");
const M = make(window, window.document);

const fails = [];
function eq(got, want, msg) {
    if (JSON.stringify(got) !== JSON.stringify(want)) {
        fails.push(`${msg}：got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
    }
}
function ok(cond, msg) { if (!cond) fails.push(msg); }

/* ① chipLabelText：剥格式后缀，不误伤版本号 */
eq(M.chipLabelText("AI时代普通人该提升的3件事 (1).png"), "AI时代普通人该提升的3件事 (1)", "剥 .png");
eq(M.chipLabelText("ChatGPT Image 2026年9月4日"), "ChatGPT Image 2026年9月4日", "无后缀不动");
eq(M.chipLabelText("v1.0"), "v1.0", "版本号不误伤");
eq(M.chipLabelText("clip.MP4"), "clip", "大写扩展也剥");
eq(M.chipLabelText("voice.wav"), "voice", "音频扩展");
eq(M.chipLabelText(""), "", "空串");
eq(M.chipLabelText(null), "", "null");

/* ② poolFromManifest：file 缺失但有 asset_id 的链接必须留下 */
{
    const mf = { assets: [
        { label: "项目图", kind: "image", file: "assets/a.png" },
        { label: "坏条目", kind: "image" },                       // 无 file 无 asset_id -> 丢弃
    ] };
    const links = [
        { asset_id: "a_0123456789ab", alias: "全局视频", kind: "video" },          // file 回填失败
        { asset_id: "a_abcdefabcdef", alias: "全局音频", kind: "audio", file: "" }, // 显式空
        { asset_id: "", alias: "空链接", kind: "image" },                          // 无 id 无 file -> 丢弃
        { asset_id: "a_111111111111", alias: "全局图", kind: "image", file: "images/x.png" },
    ];
    const pool = M.poolFromManifest(mf, links);
    const labels = pool.map((a) => a.label);
    eq(labels.includes("全局视频"), true, "有 asset_id 的链接即使 file 为空也要进池（同步修复）");
    eq(labels.includes("全局音频"), true, "显式空 file 的链接也要进池");
    eq(labels.includes("全局图"), true, "有 file 的链接正常进池");
    eq(labels.includes("项目图"), true, "项目资产正常进池");
    eq(labels.includes("坏条目"), false, "既无 file 又无 asset_id 的条目才丢弃");
    eq(labels.includes("空链接"), false, "无 asset_id 的链接丢弃");
    /* B1：池子条目必须带**含后缀的引用名**（正文里 `@` 后面写的就是它）；
     * 落盘文件名带 sha 前缀时要用 ref_name / orig_name，不能用 file。 */
    const g = pool.find((a) => a.label === "全局图");
    /* 链接条目**不用落盘 file 当引用名**：全局库 file 形如 images/<sha12>_猫.png，
     * 带 sha 前缀，直接拿来当引用名就是一串没人认得的乱码。真名来自
     * ref_name > orig_name（入库时带上），都没有才回落别名（至少能被解析到）。 */
    eq(g && g.ref_name, "全局图", "无真名时回落到别名（不用带 sha 前缀的落盘名）");
    const g2 = M.poolFromManifest({ assets: [] },
        [{ asset_id: "a_222222222222", alias: "带真名", kind: "image",
           file: "images/a_222222222222_猫.png", ref_name: "猫.png" }]);
    eq(g2[0] && g2[0].ref_name, "猫.png", "链接带了 ref_name 就用它（含后缀）");
    const g3 = M.poolFromManifest({ assets: [] },
        [{ asset_id: "a_333333333333", alias: "带原名", kind: "image",
           file: "images/a_333333333333_狗.png", orig_name: "小狗 (1).png" }]);
    eq(g3[0] && g3[0].ref_name, "小狗_1_.png", "orig_name 归一（空格/括号压成 _）");
    /* B2：标注按类型独立编号、池子统一兜底（后端落盘的为准） */
    const marks = pool.map((a) => a.mark);
    ok(marks.includes("图片1") && marks.includes("视频1") && marks.includes("音频1"),
        "标注应按类型独立编号（图片1/视频1/音频1），实际 " + JSON.stringify(marks));
    eq(new Set(marks.filter(Boolean)).size, marks.filter(Boolean).length, "标注不得重复");
}

/* ③ assetPreviewUrl：全局库路径不得回落 input */
{
    const dir = "proj1";
    // 有 asset_id -> 走 library_file（无 H3Api 时回落，故这里只验前缀分支）
    // viewUrl 会把 "assets/a.png" 的目录部分并进 subfolder（与 Comfy 原生口径一致）
    eq(M.assetPreviewUrl(dir, "assets/a.png", ""),
        "/api/view?type=output&subfolder=h3_projects%2Fproj1%2Fassets&filename=a.png",
        "项目 assets 走 output（目录并入 subfolder）");
    eq(M.assetPreviewUrl(dir, "images/x.png", ""), "", "全局库路径无 asset_id -> 空串（不回落 input）");
    eq(M.assetPreviewUrl(dir, "videos/v.mp4", ""), "", "全局库 videos 同上");
    eq(M.assetPreviewUrl(dir, "audios/a.wav", ""), "", "全局库 audios 同上");
    eq(M.assetPreviewUrl(dir, "plain.png", ""), "/api/view?type=input&subfolder=&filename=plain.png", "裸名仍走 input（旧兼容）");
}

/* ④ buildAssetThumb：图/视给缩略图，音频给音符图标 */
{
    const img = M.buildAssetThumb("p", { kind: "image", file: "assets/a.png" });
    eq(img.tagName, "IMG", "图片 -> img 缩略图");
    eq(img.className, "h3d-thumb", "图片 class");

    const vid = M.buildAssetThumb("p", { kind: "video", file: "assets/v.mp4" });
    eq(vid.tagName, "VIDEO", "视频 -> video 首帧");
    eq(vid.className, "h3d-thumb", "视频 class");
    eq(vid.muted, true, "视频静音");
    ok(String(vid.src).includes("#t="), "视频 src 带 #t 锚点取首帧");

    const aud = M.buildAssetThumb("p", { kind: "audio", file: "assets/a.wav" });
    eq(aud.tagName, "SPAN", "音频 -> 图标（无画面）");
    eq(aud.className, "h3d-kindmark", "音频 class");
    eq(aud.textContent, M.KIND_ICON.audio, "音频用音符");

    // 取不到地址（全局库无 asset_id）-> 图标，绝不生成会 404/错图的 img
    const noUrl = M.buildAssetThumb("p", { kind: "video", file: "videos/v.mp4", asset_id: "" });
    eq(noUrl.tagName, "SPAN", "视频取不到地址 -> 图标兜底");
    eq(noUrl.textContent, M.KIND_ICON.video, "视频图标兜底用 🎞");
}

if (fails.length) {
    console.error("FAIL:\n - " + fails.join("\n - "));
    process.exit(1);
}
console.log("OK: asset chip / pool merge runtime checks passed");
