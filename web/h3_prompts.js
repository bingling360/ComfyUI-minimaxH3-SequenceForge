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

  // 官方运镜词表（镜像 prompts.py CAMERA_MOVES，前端下拉用）
  const CAMERA_MOVES = ["Zoom In", "Zoom Out", "Push In", "Pull Out", "Pan Left",
    "Pan Right", "Truck Left", "Truck Right", "Tilt Up", "Tilt Down",
    "Pedestal Up", "Pedestal Down", "Arc Shot", "Tracking Shot", "Static Shot",
    "Shake Slightly", "Shake Strongly", "POV", "Roll Clockwise", "Roll Counterclockwise"];
  const CAMERA_AMPS = ["", "small", "large"];
  const CAMERA_SPEEDS = ["", "slow", "fast"];
  const RETENTION_MARKERS = ["fully_preserved", "partially_preserved",
    "attribute_transfer", "weak_reference", "fully_copy", "partially_copy", "reference"];

  // seg.prompt_v2 有则返回（浅拷贝防直接改引用），无则从旧三字段迁移出一个可用副本
  // 注意：调用方改完须经导演台 setPromptV2Field 写回 ds.segments，不直接改 ds。
  function ensurePromptV2(seg) {
    const pv = seg && typeof seg.prompt_v2 === "object" && seg.prompt_v2
      ? JSON.parse(JSON.stringify(seg.prompt_v2)) : migrateLegacySeg(seg || {});
    if (!Array.isArray(pv.shots) || !pv.shots.length) pv.shots = [defaultShot(1)];
    return pv;
  }

  function hasPromptV2(seg) {
    return !!(seg && typeof seg.prompt_v2 === "object" && seg.prompt_v2);
  }

  // 前端模式徽（镜像 prompts.detect_mode）：有参考/主体即 Ref2VA，否则按首尾帧
  function detectMode(pv, opts) {
    opts = opts || {};
    const refs = (pv && pv.references) || [];
    const subs = (pv && pv.subjects) || [];
    if ((Array.isArray(refs) && refs.length) || (Array.isArray(subs) && subs.length)) return "Ref2VA";
    const hs = !!opts.has_start, he = !!opts.has_end;
    if (hs && he) return "FL2VA";
    if (hs) return "I2VA";
    if (he) return "L2VA";
    return "T2VA";
  }

  // 段卡 -> /h3chain/compile 载荷；prompt_v2 有则直用，无则现场迁移。
  // opts.mode 为五模式之一时手动覆写（后端 VALID_MODES），缺省=后端自动判定。
  function compilePayload(seg, opts) {
    opts = opts || {};
    const pv = seg && typeof seg.prompt_v2 === "object" && seg.prompt_v2
      ? seg.prompt_v2 : migrateLegacySeg(seg || {});
    const out = {
      prompt: pv,
      seconds: Number(seg?.seconds) || Number(opts.seconds) || 5.0,
      has_start: !!opts.has_start,
      has_end: !!opts.has_end,
    };
    if (typeof opts.mode === "string" && opts.mode) out.mode = opts.mode;
    return out;
  }

  /* 预留：外部 agent / 本地模型的自动解析分配接口（总提示词框式）。
   *  后续实现：把一段自然语言/官方文本解析成 prompt_v2 各字段并写回 ds.segments。
   *  当前为占位，调用方应捕获其抛出的未实现错误。 */
  function assignV2FromText() {
    throw new Error("assignV2FromText 未实现：后续自动解析分配接口在此落地");
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
      defaultLatentSave, cleanLatentSave, ensurePromptV2, hasPromptV2,
      detectMode, assignV2FromText,
      CAMERA_MOVES, CAMERA_AMPS, CAMERA_SPEEDS, RETENTION_MARKERS,
    };
  }
})();
