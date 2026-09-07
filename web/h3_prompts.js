/* H3 具象化提示词前端 helper（M4）：旧三字段 <-> 新 prompt_v2 互转 + 编译载荷组装。
 * 只挂 window.H3Prompts，不注册入口。镜像 prompts.py 的 migrate_legacy_seg，
 * 段卡分组（画面/镜头/声音/参考/高级）渲染由导演台按需调用，不改现有三框布局。
 */
(function () {
  "use strict";

  function s(v) { return typeof v === "string" ? v : ""; }

  function defaultShot(index) {
    return {
      index: index || 1, start_seconds: null, description: "",
      camera_move: "", camera_amplitude: "", camera_speed: "",
      dialogues: [], screen_texts: [], diegetic_music: "", ref_usage: [],
    };
  }

  function defaultPromptV2() {
    return {
      intent_zh: "", medium_style: "", composition: "", environment: "",
      lighting: "", characters: "", props: "", shots: [defaultShot(1)],
      diegetic_music: "", soundscape: "", non_diegetic_music: "",
      references: [], subjects: [], task_types: [], summary_override: "",
      retention: [], override_text: null,
    };
  }

  // 旧 seg -> 新 prompt_v2（镜像后端 prompts.migrate_legacy_seg，前端离线可用）
  function migrateLegacySeg(seg) {
    seg = seg && typeof seg === "object" ? seg : {};
    const p = defaultPromptV2();
    p.environment = s(seg.scene_prompt);
    p.characters = s(seg.character_prompt);
    p.soundscape = s(seg.soundscape);
    p.non_diegetic_music = s(seg.music);
    const main = s(seg.prompt || seg.main || seg.text);
    if (main) p.shots = [Object.assign(defaultShot(1), { description: main })];
    if (Array.isArray(seg.refs)) {
      p.references = seg.refs.filter((r) => String(r).trim()).slice(0, 16)
        .map((r) => ({ label: String(r).trim(), note: "" }));
    }
    return p;
  }

  // 段卡 -> /h3chain/compile 载荷；prompt_v2 有则直用，无则现场迁移
  function compilePayload(seg, opts) {
    opts = opts || {};
    const pv = seg && typeof seg.prompt_v2 === "object" && seg.prompt_v2
      ? seg.prompt_v2 : migrateLegacySeg(seg || {});
    return {
      prompt: pv,
      seconds: Number(seg?.seconds) || Number(opts.seconds) || 5.0,
      has_start: !!opts.has_start,
      has_end: !!opts.has_end,
    };
  }

  // latent 策略默认值（M3 seg_fields.latent_save 透存，前端按钮写回）
  function defaultLatentSave() {
    return { mode: "all", start_f: 0, end_f: 0, tail_f: 0 };
  }

  function cleanLatentSave(raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    const mode = ["all", "range", "tail", "off"].includes(raw.mode) ? raw.mode : "all";
    const num = (v) => { const n = parseInt(v, 10); return Number.isFinite(n) && n > 0 ? n : 0; };
    return { mode, start_f: num(raw.start_f), end_f: num(raw.end_f), tail_f: num(raw.tail_f) };
  }

  if (typeof window !== "undefined") {
    window.H3Prompts = {
      defaultShot, defaultPromptV2, migrateLegacySeg, compilePayload,
      defaultLatentSave, cleanLatentSave,
    };
  }
})();
