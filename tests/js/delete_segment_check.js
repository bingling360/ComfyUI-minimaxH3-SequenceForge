/* 「✕ 删除此段」端到端：删提示词**同时**叫后端删存档并前移，且不漏落盘。
 *
 * 用户反馈（2026-11-08）：删一段之后「删不干净」——分段成片还在、段卡上的视频
 * 张冠李戴。根因是旧实现只 splice 前端提示词数组：manifest 的 videos/thumbs/seeds
 * 按槽位对齐，段 mp4/latent 也按槽位编号命名。现在必须做三件事：
 *   1) 有 manifest 时先 POST /h3chain/delete_segment（slot 是**全局 0-based**，
 *      有序章时 = off + idx，别漏 off）；
 *   2) 后端成功后才 splice 本地提示词/分段字段（失败 → 本地不动，避免两边不一致）；
 *   3) 立刻 flushPrompts 落盘（防抖被清掉，因为挂起的那次捕获的是删除前的数组）；
 *   4) ds.redo_segs 随槽位前移（不搬就会指到别的段上）。
 */
const path = require("path");
const H = require(path.join(__dirname, "_harness.js"));

let bad = 0;
function ok(cond, msg, got, want) {
    if (cond) return;
    bad++;
    console.log("  FAIL " + msg);
    if (arguments.length > 2) {
        console.log("    期望: " + JSON.stringify(want));
        console.log("    实际: " + JSON.stringify(got));
    }
}

const MF = {
    done: 3, total: 4, has_prologue: false, revision: 2,
    params: { width: 1376, height: 768 },
};

function mkNode(prompts, redoSegs) {
    return H.mkNode({
        prompts, segments: prompts.map(() => ({})), mode: "分段",
        redo_segs: redoSegs || [],
    });
}

function boot(node, mf) {
    return H.load({
        node,
        apiRoutes: {
            "/h3chain/project": { ok: true, manifest: mf },
            "/h3chain/projects": { ok: true, projects: [{ dir: "PROJ" }], state: { dir: "PROJ", done: 3 } },
        },
    });
}

(async () => {
    /* ---------- 1) 有存档：先删后端，再 splice，再落盘 ---------- */
    {
        const node = mkNode(["a", "b", "c", "d"]);
        const { w } = boot(node, MF);
        const data = { node, mf: MF, plan: [], ds: w.getDs(node), state: { dir: "PROJ" } };
        const before = w.__apiCalls.length;
        await w.removePromptSegment(node, 1, data);      // 删段 2（0-based idx=1）
        const calls = w.__apiCalls.slice(before);
        const del = calls.filter((c) => c.path.indexOf("delete_segment") >= 0);
        ok(del.length === 1, "必须调一次 delete_segment", del.length, 1);
        ok(del[0] && del[0].body.dir === "PROJ", "带上存档目录", del[0] && del[0].body, { dir: "PROJ" });
        ok(del[0] && del[0].body.slot === 1, "无序章：段 2 → 全局槽位 1", del[0] && del[0].body);
        ok(del[0] && del[0].body.drop_finals === true, "默认连自动成片一起清");

        const ds = w.getDs(node);
        ok(JSON.stringify(ds.prompts) === JSON.stringify(["a", "c", "d"]),
            "提示词数组要 splice 掉被删段", ds.prompts, ["a", "c", "d"]);

        const save = calls.filter((c) => c.path.indexOf("save_prompts") >= 0);
        ok(save.length === 1, "删完必须立刻落盘（清掉挂起的防抖那一次）", save.length, 1);
        ok(save[0] && JSON.stringify(save[0].body.prompts) === JSON.stringify(["a", "c", "d"]),
            "落盘的是删完的数组", save[0] && save[0].body.prompts, ["a", "c", "d"]);
        ok(save[0] && save[0].body.segments.length === 3, "分段字段同序 splice");
    }

    /* ---------- 2) 有序章：槽位必须带 off ---------- */
    {
        const mfP = Object.assign({}, MF, { has_prologue: true, done: 4, total: 5 });
        const node = mkNode(["a", "b", "c"]);
        const { w } = boot(node, mfP);
        const data = { node, mf: mfP, plan: [], ds: w.getDs(node), state: { dir: "PROJ" } };
        const before = w.__apiCalls.length;
        await w.removePromptSegment(node, 1, data);      // 删提示词第 2 段
        const del = w.__apiCalls.slice(before)
            .filter((c) => c.path.indexOf("delete_segment") >= 0);
        ok(del[0] && del[0].body.slot === 2, "有序章：提示词段 2 → 全局槽位 2（序章占 0）",
            del[0] && del[0].body);
    }

    /* ---------- 3) 重摇标记随槽位前移 ---------- */
    {
        const node = mkNode(["a", "b", "c", "d"], [{ slot: 0, mode: "双锚" },
            { slot: 1, mode: "仅锚上段" }, { slot: 3, mode: "双锚" }]);
        const { w } = boot(node, MF);
        const data = { node, mf: MF, plan: [], ds: w.getDs(node), state: { dir: "PROJ" } };
        await w.removePromptSegment(node, 1, data);
        const marks = w.getDs(node).redo_segs || [];
        ok(JSON.stringify(marks.map((x) => x.slot)) === "[0,2]",
            "已删槽位的标记丢弃、其后减一", marks, [{ slot: 0 }, { slot: 2 }]);
        ok(marks[1] && marks[1].mode === "双锚", "模式跟着段一起搬");
    }

    /* ---------- 3b) 段级 latent 引用跟着改名 ---------- */
    {
        const node = H.mkNode({
            prompts: ["a", "b", "c", "d"], mode: "分段",
            segments: [
                { latent_ref: { on: true, src: { file: "latent/seg001_auto.pt" } } },
                {},                                          // 段 2：被删
                { tail_src: { latent: "latent/seg002_auto.pt" },
                  anchors: [{ src: { kind: "latent", ref: "latent/seg003_auto.pt" } }] },
                { tail_src: { latent: "latent/mine.pt" } },
            ],
        });
        const { w } = boot(node, MF);
        const data = { node, mf: MF, plan: [], ds: w.getDs(node), state: { dir: "PROJ" } };
        await w.removePromptSegment(node, 1, data);      // 删段 2（槽位 1）
        const segs = w.getDs(node).segments || [];
        ok(segs.length === 3, "分段字段同序 splice", segs.length, 3);
        ok(segs[0] && segs[0].latent_ref && segs[0].latent_ref.src.file === "latent/seg001_auto.pt",
            "槽位号 <= g 的引用不动", segs[0] && segs[0].latent_ref);
        ok(segs[1] && segs[1].tail_src && segs[1].tail_src.latent === "latent/seg001_auto.pt",
            "tail_src 引用随槽位前移（原段 3 → 段 2）", segs[1] && segs[1].tail_src);
        ok(segs[1] && segs[1].anchors && segs[1].anchors[0].src.ref === "latent/seg002_auto.pt",
            "锚引用随槽位前移", segs[1] && segs[1].anchors);
        ok(segs[2] && segs[2].tail_src && segs[2].tail_src.latent === "latent/mine.pt",
            "非段号命名（自定义 latent）不动", segs[2] && segs[2].tail_src);
    }

    /* ---------- 4) 没有 manifest（还没跑过）：不动后端，只删本地 ---------- */
    {
        const node = mkNode(["a", "b"]);
        const { w } = boot(node, MF);
        const data = { node, mf: null, plan: [], ds: w.getDs(node), state: { dir: "PROJ" } };
        const before = w.__apiCalls.length;
        await w.removePromptSegment(node, 0, data);
        const del = w.__apiCalls.slice(before)
            .filter((c) => c.path.indexOf("delete_segment") >= 0);
        ok(del.length === 0, "无 manifest 时不该调删段接口", del.length, 0);
        ok(JSON.stringify(w.getDs(node).prompts) === JSON.stringify(["b"]), "本地照删");
    }

    /* ---------- 5) 只剩一段：删完必须立刻补一条**崭新空段** ---------- */
    {
        const node = H.mkNode({
            prompts: ["唯一一段"], mode: "分段",
            segments: [{ intent_zh: "旧意图", scene_prompt: "旧场景", seconds: 9,
                         refs: ["女主"], anchors: [{ src: { kind: "latent", ref: "latent/seg000_auto.pt" } }] }],
        });
        const mf1 = { done: 1, total: 1, has_prologue: false, params: { width: 1376, height: 768 } };
        const { w } = boot(node, mf1);
        const data = { node, mf: mf1, plan: [], ds: w.getDs(node), state: { dir: "PROJ" } };
        const before = w.__apiCalls.length;
        await w.removePromptSegment(node, 0, data);
        const calls = w.__apiCalls.slice(before);
        const del = calls.filter((c) => c.path.indexOf("delete_segment") >= 0);
        ok(del[0] && del[0].body.slot === 0, "唯一一段：槽位 0", del[0] && del[0].body);

        const ds = w.getDs(node);
        ok(ds.prompts.length === 1 && ds.prompts[0] === "",
            "删空后必须补一条空提示词段（面板不留 0 段空窗）", ds.prompts, [""]);
        ok(ds.segments && ds.segments.length === 1, "分段字段也是 1 条", ds.segments && ds.segments.length, 1);
        const s0 = ds.segments && ds.segments[0];
        ok(s0 && !s0.scene_prompt && !s0.intent_zh && s0.seconds == null,
            "补的是**崭新**默认段（旧内容不许残留）", s0);
        ok(s0 && s0.refs.length === 0 && s0.anchors.length === 0, "引用与锚也清空", s0);

        const save = calls.filter((c) => c.path.indexOf("save_prompts") >= 0);
        ok(save[0] && JSON.stringify(save[0].body.prompts) === JSON.stringify([""]),
            "落盘的也是空段（不是 0 段）", save[0] && save[0].body.prompts, [""]);
    }

    /* ---------- 6) 后端失败：本地不动（不许出现"提示词没了、存档还在"） ---------- */
    {
        const node = mkNode(["a", "b", "c"]);
        const { w } = H.load({
            node,
            apiRoutes: {
                "/h3chain/project": { ok: true, manifest: MF },
                "/h3chain/projects": { ok: true, projects: [], state: { dir: "PROJ" } },
            },
            onAlert: () => {},
        });
        /* 让删段接口返回 500 */
        const orig = w.api.fetchApi;
        w.api.fetchApi = async (p, init) => {
            if (String(p).indexOf("delete_segment") >= 0) {
                return { ok: false, status: 500, json: async () => ({ error: "boom" }) };
            }
            return orig(p, init);
        };
        const data = { node, mf: MF, plan: [], ds: w.getDs(node), state: { dir: "PROJ" } };
        await w.removePromptSegment(node, 1, data);
        ok(JSON.stringify(w.getDs(node).prompts) === JSON.stringify(["a", "b", "c"]),
            "后端失败时本地提示词必须原样保留", w.getDs(node).prompts, ["a", "b", "c"]);
    }

    console.log(bad ? `\n${bad} 个用例不符` : "\n全部通过");
    process.exit(bad ? 1 : 0);
})().catch((e) => { console.error("ERR", e); process.exit(1); });
