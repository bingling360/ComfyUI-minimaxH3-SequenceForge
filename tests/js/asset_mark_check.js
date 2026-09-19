/* 素材标注（mark）前端真跑检查：直接跑 web/h3_director.js 里的 shipped 代码，
 * 断言与 asset_store.py 的 clean_mark / next_mark / assign_marks 逐字同规则。
 *
 * 跑法：node tests/js/asset_mark_check.js
 * 最重要的一条：**两层编号不可混用** —— mark 是发给 LLM 的稳定号，
 * <Picture N> 是执行期按本段挂载顺序重算的 token。本文件只锁第一层。 */
const { load } = require("./_harness.js");

const { w, errors } = load();
if (errors.length) {
    console.log("FAIL: 装载期抛错 →", errors.join(" | "));
    process.exit(1);
}

let fails = 0;
function eq(got, want, label) {
    const a = JSON.stringify(got);
    const b = JSON.stringify(want);
    if (a !== b) {
        console.log(`FAIL ${label}: got ${a} want ${b}`);
        fails++;
    } else {
        console.log(`OK   ${label}: ${a}`);
    }
}

for (const fn of ["cleanMark", "markShaped", "nextMark", "assignMarks", "markOf",
                  "poolFromManifest", "poolSig"]) {
    if (typeof w[fn] !== "function") {
        console.log(`FAIL: 缺少 shipped 函数 ${fn}`);
        fails++;
    }
}

/* ---- cleanMark / markShaped ---- */
eq(w.cleanMark("图片1"), "图片1", "cleanMark 图片1");
eq(w.cleanMark(" 视频12 "), "视频12", "cleanMark 去空白");
eq(w.cleanMark("音频999"), "音频999", "cleanMark 音频999");
eq(w.cleanMark("图片0"), "", "cleanMark 拒绝 0");
eq(w.cleanMark("图片1000"), "", "cleanMark 拒绝 4 位");
eq(w.cleanMark("图1"), "", "cleanMark 拒绝 图1");
eq(w.cleanMark(""), "", "cleanMark 空");
eq(w.cleanMark(null), "", "cleanMark null");
eq([w.markShaped("图片7"), w.markShaped("图片"), w.markShaped("阿依")],
   [true, false, false], "markShaped");

/* ---- nextMark：最小空闲（手动标 图片9 不顶飞自动序列） ---- */
eq(w.nextMark([{ mark: "图片1" }, { mark: "图片3" }], "image"), "图片2", "nextMark 补空号");
eq(w.nextMark([{ mark: "图片1" }, { mark: "图片9", mark_auto: false }], "image"),
   "图片2", "nextMark 不被手动号顶飞");
eq(w.nextMark([{ mark: "视频2" }], "video"), "视频1", "nextMark 按类独立（最小空闲）");
eq(w.nextMark([], "audio"), "音频1", "nextMark 空表");

/* ---- assignMarks：池序 + 幂等 + 不写 mark_auto ---- */
const pool = [
    { label: "a", kind: "image" },
    { label: "b", kind: "video" },
    { label: "c", kind: "image" },
    { label: "d", kind: "image", mark: "图片9", mark_auto: false },
];
eq(w.assignMarks(pool), true, "assignMarks 有改动");
eq(pool.map((x) => x.mark), ["图片1", "视频1", "图片2", "图片9"], "assignMarks 池序发号");
eq("mark_auto" in pool[0], false, "assignMarks 不写 mark_auto（缺省即自动）");
eq(pool[3].mark_auto, false, "assignMarks 不动手动标记");
eq(w.assignMarks(pool), false, "assignMarks 幂等");

/* ---- poolFromManifest：链接带 mark / 缺号自动补 / 旧 assets 在前 ---- */
const mf = { assets: [{ label: "旧图", kind: "image", file: "assets/o.png" }] };
const links = [
    { asset_id: "a_111111111111", alias: "链图", kind: "image", file: "images/x.png", mark: "图片2" },
    { asset_id: "a_222222222222", alias: "链视", kind: "video", file: "videos/y.mp4", mark: "视频1" },
];
const merged = w.poolFromManifest(mf, links);
eq(merged.map((x) => x.label), ["旧图", "链图", "链视"], "池序：旧 assets 在前");
eq(merged.map((x) => x.mark), ["图片1", "图片2", "视频1"], "缺号的按池序补、已有的不动");
eq(w.markOf(merged, "链图"), "图片2", "markOf 命中");
eq(w.markOf(merged, "不存在"), "", "markOf 未命中");

/* 无效条目也要占号（与后端 _mark_pool 逐位对齐，避免前后端错位） */
const gap = w.poolFromManifest({ assets: [{ label: "坏条目", kind: "image" }] }, links);
eq(gap.map((x) => x.label), ["链图", "链视"], "无 file 无 asset_id 的条目被过滤");
eq(gap[0].mark, "图片2", "被过滤条目仍占号 → 与后端一致");

/* ---- poolSig：标注变化必须进指纹（否则池不写回 widget） ---- */
const s1 = w.poolSig([{ label: "a", kind: "image", mark: "图片1" }]);
const s2 = w.poolSig([{ label: "a", kind: "image", mark: "图片2" }]);
eq(s1 !== s2, true, "poolSig 包含 mark");

process.exit(fails ? 1 : 0);
