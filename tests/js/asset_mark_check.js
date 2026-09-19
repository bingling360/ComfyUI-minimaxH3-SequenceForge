/* 素材标注（mark）前端真跑检查：直接跑 web/h3_director.js 里的 shipped 代码，
 * 断言与 asset_store.py 的 clean_mark / mark_shaped / next_mark_seq / assign_marks
 * **逐字同规则**（规则唯一在 web/h3_prompts.js，director 只做同名转发 + 兜底）。
 *
 * 跑法：node tests/js/asset_mark_check.js
 * 最重要的一条：**编号不回收** —— 删掉的号留空，新号从"当前最大号 +1"接着编。
 * 回收（最小空闲号）会让旧提示词里的 `@图片2` 悄悄改指向另一张素材 —— 静默串号。
 *
 * 另一条：**两层编号不可混用** —— mark 是发给 LLM 的稳定号，<Picture N> 是
 * 执行期按本段挂载顺序重算的 token。本文件只锁第一层。 */
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

for (const fn of ["cleanMark", "markShaped", "nextMarkSeq", "assignMarks", "markOf",
                  "markTextOf", "poolFromManifest", "poolSig"]) {
    if (typeof w[fn] !== "function") {
        console.log(`FAIL: 缺少 shipped 函数 ${fn}`);
        fails++;
    }
}

/* ---- cleanMark / markShaped：与后端 clean_mark 同一条尺子 ---- */
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

/* ---- nextMarkSeq：不回收（最大号 +1） ---- */
eq(w.nextMarkSeq("image", ["图片1", "图片3"]), 4, "nextMarkSeq 不补空缺的 2");
eq(w.nextMarkSeq("image", ["图片1", "图片9"]), 10, "nextMarkSeq 不被手动大号顶飞");
eq(w.nextMarkSeq("video", ["视频2"]), 3, "nextMarkSeq 按类独立");
eq(w.nextMarkSeq("audio", []), 1, "nextMarkSeq 空表从 1 起");
eq(w.nextMarkSeq("image", ["图片x", "nonsense"]), 1, "nextMarkSeq 脏值不参与取最大");

/* ---- assignMarks：返回**新列表**、不就地改、幂等 ---- */
const pool = [
    { label: "a", kind: "image" },
    { label: "b", kind: "video" },
    { label: "c", kind: "image" },
    { label: "d", kind: "image", mark: "图片9" },
];
const out1 = w.assignMarks(pool);
eq(out1.map((x) => x.mark), ["图片10", "视频1", "图片11", "图片9"],
   "assignMarks 手动大号把自动序列顶到 10 起");
eq(pool[0].mark, undefined, "assignMarks 不就地改入参（返回新列表）");
eq(w.assignMarks(out1).map((x) => x.mark),
   ["图片10", "视频1", "图片11", "图片9"], "assignMarks 幂等");
/* 非法形态必须重发，不能留着占位：留着的话界面显示的号与实际挂的素材不是同一张 */
eq(w.assignMarks([{ label: "x", kind: "image", mark: "图1" }])[0].mark, "图片1",
   "assignMarks 非法标注重发（不是留着占位）");
eq(w.assignMarks([{ label: "x", kind: "image", mark: "图片0" }])[0].mark, "图片1",
   "assignMarks 图片0 非法 → 重发");

/* ---- poolFromManifest：链接带 mark / 缺号按池序补 / 旧 assets 在前 ---- */
const mf = { assets: [{ label: "旧图", kind: "image", file: "assets/o.png" }] };
const links = [
    { asset_id: "a_111111111111", alias: "链图", kind: "image", file: "images/x.png", mark: "图片2" },
    { asset_id: "a_222222222222", alias: "链视", kind: "video", file: "videos/y.mp4", mark: "视频1" },
];
const merged = w.poolFromManifest(mf, links);
eq(merged.map((x) => x.label), ["旧图", "链图", "链视"], "池序：旧 assets 在前");
eq(merged.map((x) => x.mark), ["图片3", "图片2", "视频1"],
   "缺号的按池序补（不回收 → 旧图拿 3 而不是空着的 1）、已有的不动");
eq(w.markOf(merged, "链图"), "图片2", "markOf 命中（按别名）");
eq(w.markOf(merged, "不存在"), "", "markOf 未命中");
eq(merged.every((x) => String(x.ref_name || "").length > 0), true,
   "池里每条都有 ref_name（全名，解析外部提示词靠它）");

/* 无 file 无 asset_id 的条目被过滤；有 asset_id 的即使没 file 也要留下
 * （file 由后端按 asset_id 回填，过滤掉就永远不出现） */
const gap = w.poolFromManifest({ assets: [{ label: "坏条目", kind: "image" }] }, links);
eq(gap.map((x) => x.label), ["链图", "链视"], "无 file 无 asset_id 的条目被过滤");
eq(gap[0].mark, "图片2", "被过滤的条目不占号");
const onlyId = w.poolFromManifest({ assets: [] },
    [{ asset_id: "a_333333333333", alias: "库视频", kind: "video", file: "" }]);
eq(onlyId.map((x) => x.label), ["库视频"], "只有 asset_id 的条目保留（file 后端回填）");

/* ---- poolSig：标注变化必须进指纹（否则池不写回 widget） ---- */
const s1 = w.poolSig([{ label: "a", kind: "image", mark: "图片1" }]);
const s2 = w.poolSig([{ label: "a", kind: "image", mark: "图片2" }]);
eq(s1 !== s2, true, "poolSig 包含 mark");

process.exit(fails ? 1 : 0);
