/* 「保存一采分段成片」+ 段卡一采/二采双入口 —— 前端端到端回归。
 *
 * 背景：二采成功时后端把高清结果写进 finals/seg_NNN.mp4（**同名覆盖**基础分段），
 * 一采那版分段就没了。二采基础设置新增开关 keep_base_seg：开启后后端把一采解码
 * 结果另存 finals/base_seg_NNN.mp4，段卡上「一采分段成片」/「二采分段成片」两个
 * 入口分别取两版（原「预览」按钮改名拆成两个）。
 *
 * 钉四件事：
 *   1) 两版都有 → 两个按钮都出现，各自打到自己的文件（一采=base_seg，二采=seg）；
 *   2) 二采做过但没开另存 → 一采按钮 disabled（并给出「怎么补」的提示），二采按钮在；
 *   3) 没做二采 → 一采按钮回落到段视频本身，且**不出现**二采按钮；
 *   4) 二采面板基础设置里的勾选真的落到 ds.upscale.keep_base_seg（不是画着好看）。
 */
const { load, mkNode } = require("./_harness.js");

let fails = 0;
function ok(cond, msg, got, want) {
    if (cond) return;
    fails += 1;
    console.log("FAIL:", msg);
    if (arguments.length > 2) {
        console.log("   期望:", JSON.stringify(want), " 实际:", JSON.stringify(got));
    }
}

const BASE = {
    revision: 1, done: 1, total: 1, has_prologue: false,
    prompts: ["一段提示词"], thumbs: ["finals/thumb_000.png"],
    videos: ["finals/seg_000.mp4"], base_videos: [""],
    finals: [], merges: [], assets: [], segments: [{}],
    params: { width: 1376, height: 768, length: 81, ctx: 22, steps: 8, cfg: 1.0 },
};

function env(over) {
    const manifest = JSON.parse(JSON.stringify(BASE));
    Object.assign(manifest, (over && over.mf) || {});
    if (over && over.base_videos !== undefined) manifest.base_videos = over.base_videos;
    if (over && over.upscale !== undefined) manifest.upscale = over.upscale;
    const ds = { prompts: ["一段提示词"], segments: [{}] };
    if (over && over.ds) Object.assign(ds, over.ds);
    const node = mkNode(ds);
    const { w, dom } = load({
        node,
        apiRoutes: {
            "/h3chain/project": () => ({ ok: true, manifest }),
            "/h3chain/projects": { ok: true, projects: [{ dir: "PROJ" }], state: { dir: "PROJ" } },
            "/h3chain/upscale_models": { ok: true, models: [] },
            "/h3chain/bridge_models": { ok: true, models: [] },
        },
    });
    return { w, dom, node };
}

const UP_DONE = { segs: [{ done: true, hash: "h", base_hash: "b", size: [1926, 1076] }] };

/** 段卡标题栏里的两个入口按钮（按文案取）。 */
function segButtons(doc) {
    const btns = [...doc.querySelectorAll(".h3d-ctitle .h3d-previewbtn")];
    return {
        base: btns.find((b) => (b.textContent || "").indexOf("一采分段成片") >= 0) || null,
        up: btns.find((b) => (b.textContent || "").indexOf("二采分段成片") >= 0) || null,
        all: btns,
    };
}

function viewerSrc(doc) {
    const v = doc.querySelector(".h3d-viewer video");
    return v ? String(v.getAttribute("src") || "") : "";
}

function closeViewer(doc) {
    [...doc.querySelectorAll(".h3d-viewer")].forEach((e) => e.remove());
}

/** 打开导演台并等数据到位。 */
async function boot(w) {
    w.eval("openDesk")();
    await new Promise((r) => setTimeout(r, 80));
}

(async () => {
    /* ---------- 1) 两版都在：两个按钮，各自打自己的文件 ---------- */
    {
        const { w, dom } = env({
            base_videos: ["finals/base_seg_000.mp4"],
            upscale: UP_DONE,
        });
        await boot(w);
        const doc = w.document;
        const b = segButtons(doc);
        ok(!!b.base, "有 base_videos 时「一采分段成片」按钮必须出现");
        ok(!!b.up, "有二采记录时「二采分段成片」按钮必须出现");
        ok(!b.base.disabled, "一采按钮必须可点（有另存件）");
        if (b.base) {
            b.base.click();
            ok(viewerSrc(doc).indexOf("base_seg_000.mp4") >= 0,
                "一采入口要打开 base_seg_000.mp4", viewerSrc(doc));
            closeViewer(doc);
        }
        if (b.up) {
            b.up.click();
            ok(viewerSrc(doc).indexOf("seg_000.mp4") >= 0,
                "二采入口要打开 seg_000.mp4（高清结果同名覆盖）", viewerSrc(doc));
            closeViewer(doc);
        }
    }

    /* ---------- 2) 二采做过但当时没开另存：一采按钮 disabled + 指路 ---------- */
    {
        const { w, dom } = env({ base_videos: [""], upscale: UP_DONE });
        await boot(w);
        const doc = w.document;
        const b = segButtons(doc);
        ok(!!b.base, "没另存时一采按钮仍在（告诉用户为什么没有）");
        ok(b.base && b.base.disabled, "一采版没留存 → 按钮必须 disabled，不能点出空播放器");
        ok(b.base && (b.base.title || "").indexOf("保存一采分段成片") >= 0,
            "disabled 的一采按钮要指路到「保存一采分段成片」", b.base && b.base.title);
        ok(!!b.up, "二采按钮照常在");
    }

    /* ---------- 3) 没做二采：一采回落段视频本身，且没有二采按钮 ---------- */
    {
        const { w, dom } = env({ base_videos: [""], upscale: { segs: [null] } });
        await boot(w);
        const doc = w.document;
        const b = segButtons(doc);
        ok(!!b.base, "未做二采时段视频就是基础版，一采按钮必须可用");
        ok(b.base && !b.base.disabled, "未做二采时一采按钮不该 disabled");
        if (b.base) {
            b.base.click();
            ok(viewerSrc(doc).indexOf("seg_000.mp4") >= 0,
                "未做二采时一采入口回落段视频", viewerSrc(doc));
            closeViewer(doc);
        }
        ok(!b.up, "没做二采就不该出现「二采分段成片」按钮");
    }

    /* ---------- 3b) 从没跑过二采（manifest 连 upscale 键都没有）----------
     * 「只跑一采」的边界：此时 seg_NNN.mp4 就是当前的一采成片，一采入口必须可用。 */
    {
        const { w, dom } = env({ base_videos: [""], upscale: undefined });
        await boot(w);
        const doc = w.document;
        const b = segButtons(doc);
        ok(!!b.base, "从没跑过二采时「一采分段成片」按钮必须出现");
        ok(b.base && !b.base.disabled, "从没跑过二采时一采按钮不该 disabled");
        if (b.base) {
            b.base.click();
            ok(viewerSrc(doc).indexOf("seg_000.mp4") >= 0,
                "从没跑过二采时一采入口回落段视频", viewerSrc(doc));
            closeViewer(doc);
        }
        ok(!b.up, "从没跑过二采就不该出现「二采分段成片」按钮");
    }

    /* ---------- 3c) 记录在但已失效（二采关着时改了提示词/种子）----------
     * 该段重新采样、seg_NNN.mp4 写回基础版，而旧高清记录不会被自动清掉：
     * 只看 done 会把基础版当高清 → 一采入口被错误置灰、二采入口指向基础分辨率视频。 */
    {
        const stale = {
            segs: [{ done: true, hash: "h", base_hash: "OLDHASH|123", size: [1926, 1076] }],
        };
        const { w, dom } = env({
            base_videos: [""], upscale: stale,
            mf: { prompt_hashes: ["NEWHASH"], seeds: [456] },
        });
        await boot(w);
        const doc = w.document;
        const b = segButtons(doc);
        ok(!!b.base && !b.base.disabled,
            "记录已失效时一采按钮必须可用（此时 seg_NNN.mp4 就是基础版）");
        ok(!b.up, "记录已失效就不该再给「二采分段成片」入口（那个文件是基础分辨率）");
        const chips = [...doc.querySelectorAll(".h3d-ctitle .h3d-chip")].map((c) => c.textContent).join("|");
        ok(chips.indexOf("二采") < 0, "失效记录不该再挂「二采」徽章", chips);
        if (b.base) {
            b.base.click();
            ok(viewerSrc(doc).indexOf("seg_000.mp4") >= 0,
                "失效记录下一采入口回落段视频", viewerSrc(doc));
            closeViewer(doc);
        }
    }

    /* ---------- 3d) 记录有效（base_hash 与当前 prompt_hashes|seeds 一致）----------
     * 与前一条成对：判据必须是 base_hash，不能是"有记录就算"。 */
    {
        const live = {
            segs: [{ done: true, hash: "h", base_hash: "NEWHASH|456", size: [1926, 1076] }],
        };
        const { w, dom } = env({
            base_videos: [""], upscale: live,
            mf: { prompt_hashes: ["NEWHASH"], seeds: [456] },
        });
        await boot(w);
        const doc = w.document;
        const b = segButtons(doc);
        ok(!!b.up, "记录有效时「二采分段成片」按钮必须出现");
        ok(b.base && b.base.disabled,
            "记录有效 + 没另存 → 一采版确实没留存，按钮置灰并指路");
        const chips = [...doc.querySelectorAll(".h3d-ctitle .h3d-chip")].map((c) => c.textContent).join("|");
        ok(chips.indexOf("二采") >= 0, "有效记录要挂「二采」徽章", chips);
    }

    /* ---------- 4) 面板勾选真的落到 ds ----------
     * 二采开着才显示该勾选（关掉二采时它没有意义，面板只留「模式」一行）。 */
    {
        const { w, node } = env({
            base_videos: [""], upscale: { segs: [null] },
            ds: { upscale: { mode: "跟随生成" } },
        });
        await boot(w);
        const doc = w.document;
        const fields = [...doc.querySelectorAll(".h3d-upsec .h3d-param")];
        const field = fields.find((f) => {
            const lb = f.querySelector("label");
            return lb && (lb.textContent || "").indexOf("保存一采分段成片") >= 0;
        });
        ok(!!field, "二采面板基础设置里必须有「保存一采分段成片」勾选");
        const cb = field && field.querySelector('input[type="checkbox"]');
        ok(!!cb, "该项必须是勾选框");
        ok(cb && cb.checked === false, "出厂默认必须是不勾（用户拍板默认关）");
        if (cb) {
            cb.checked = true;
            cb.onchange();
            const ds = w.eval("getDs")(node);
            ok(ds.upscale.keep_base_seg === true,
                "勾上要写进 ds.upscale.keep_base_seg", ds.upscale.keep_base_seg);
            // 老存档缺键 → 归一为 false（与后端 parse_state 同口径）
            const ds2 = w.eval("getDs")(mkNode({ prompts: ["x"], segments: [{}] }));
            ok(ds2.upscale.keep_base_seg === false,
                "老存档缺键要归一为 false", ds2.upscale.keep_base_seg);
        }
    }

    /* ---------- 5) 缓存钥匙：同名覆盖的段产物，URL 必须带 v=<updated_at> ----------
     * 重摇 / 二采接管都写回**同一个** finals/seg_NNN.mp4（一采另存同理
     * base_seg_NNN.mp4），而 /api/view 是 FileResponse、没有 Cache-Control
     * （启发式可缓存）→ 同一个 URL 再打开会命中旧字节。用户看到的就是
     * 「重摇完了，段卡「一采分段成片」还是上一版，只有去文件夹里才看得到新的」。
     * 钉两件事：①URL 带上 manifest.updated_at；②它变了 URL 跟着变。 */
    {
        const { w } = env({
            base_videos: ["finals/base_seg_000.mp4"], upscale: UP_DONE,
            mf: { updated_at: 1700000000 },
        });
        await boot(w);
        const doc = w.document;
        const b = segButtons(doc);
        ok(!!b.base, "带 updated_at 时一采按钮仍要出现");
        if (b.base) {
            b.base.click();
            const src = viewerSrc(doc);
            ok(src.indexOf("v=1700000000000") >= 0,
                "一采入口 URL 必须带 v=<updated_at 毫秒> 当缓存钥匙", src);
            closeViewer(doc);
        }
        if (b.up) {
            b.up.click();
            ok(viewerSrc(doc).indexOf("v=1700000000000") >= 0,
                "二采入口 URL 也必须带同一个缓存钥匙", viewerSrc(doc));
            closeViewer(doc);
        }
        // 版本号变了 → URL 必须跟着变（否则浏览器照旧给旧字节）
        const { w: w2 } = env({
            base_videos: ["finals/base_seg_000.mp4"], upscale: UP_DONE,
            mf: { updated_at: 1700000001 },
        });
        await boot(w2);
        const b2 = segButtons(w2.document);
        if (b2.base) {
            b2.base.click();
            ok(viewerSrc(w2.document).indexOf("v=1700000001000") >= 0,
                "manifest 更新后 URL 必须换一个新钥匙", viewerSrc(w2.document));
            closeViewer(w2.document);
        }
    }

    console.log(fails ? `\n${fails} 个断言失败` : "\nkeep_base_seg_ui_check 全部通过");
    process.exit(fails ? 1 : 0);
})();
