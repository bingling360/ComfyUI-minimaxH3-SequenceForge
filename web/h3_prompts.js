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

  /* ---- 素材标注编解码（B03）----
   * 发给 LLM 的一跳用**标注**（`@图片1`）；磁盘上永远存**素材名**（`@阿依.png`）。
   *
   * 为什么：长文件名进 LLM 既费 token 又容易被抄错，抄错 = 静默丢图（模型收不到那张参考图，
   * 而正文看着完全正常）。短编号还能让"引用的是哪一张"在人机对话里可核对。
   *
   * 为什么标注不能落盘：执行期 nodes._find_refs 只认池里的**别名**，标注不是别名 ——
   * 一旦写进 prompts，编译时找不到标签，那张图就再也不进 conditioning。
   * 所以这两个函数**只允许**出现在请求/回填这一跳上。
   *
   * 与官方 `<Picture N>` 分层：那个是执行期按"本段挂载顺序"每段重算的 token
   * （asset_store.compile_refs）。某段只引用「图片3」时仍须编译成 `<Picture 1>`。
   */
  const MARK_RE = /^(图片|视频|音频)(\d{1,3})$/;
  const MARK_OPEN = "【[（(「";
  const MARK_CLOSE = { "【": "】", "[": "]", "（": "）", "(": ")", "「": "」" };
  /* `@别名` 语法的前导判据：**必须与后端 nodes._REF_AT 的负向后顾逐字一致**
   * （只挡 ASCII 字母数字下划线）—— 写成含 CJK 的"更严"版本会让
   * `@阿依@回廊` 这种连写漏掉后一个，与 refsFromText / 后端编译结果不一致。 */
  const _AT_PREV_BAD = (c) => /[0-9A-Za-z_]/.test(c || "");
  /* 裸形态（LLM 漏了 @）的前导判据：连汉字也挡，避免把散文里的「大图片1」当引用 */
  const _BARE_PREV_BAD = (c) => /[0-9A-Za-z_\u4e00-\u9fff]/.test(c || "");

  /** 池 → 可用的 (素材名, 标注) 对（标注形态非法/缺失的条目直接跳过） */
  function markPairs(pool) {
    const out = [];
    for (const a of (pool || [])) {
      const label = String((a && a.label) || "").trim();
      const mark = String((a && a.mark) || "").trim();
      if (label && MARK_RE.test(mark)) out.push({ label, mark });
    }
    return out;
  }

  /** 出（给 LLM）：`@素材名` → `@标注`。最长素材名优先，与 refsFromText / 后端 _find_refs 同口径。 */
  function marksToText(text, pool) {
    const s = String(text == null ? "" : text);
    const pairs = markPairs(pool).sort((a, b) => b.label.length - a.label.length);
    if (!pairs.length) return s;
    let out = "";
    let i = 0;
    while (i < s.length) {
      if (s[i] === "@" && !_AT_PREV_BAD(s[i - 1])) {
        const hit = pairs.find((p) => s.startsWith(p.label, i + 1));
        if (hit) { out += "@" + hit.mark; i += hit.label.length + 1; continue; }
      }
      out += s[i];
      i += 1;
    }
    return out;
  }

  /** 回（LLM 产出）：`@标注` → `@素材名`。
   *
   * 容错按"宁可多认不可漏认"：LLM 可能写成 `图片1`（漏 @）、`@图片 1`（中间多个空格）、
   * `【图片1】`/`(图片1)`（加括号强调）。**故意接受裸形态** —— 漏认的后果是引用静默丢失
   * （图片不进模型，正文却看着正常），而多认的后果只是个明显的绿色引用框，用户一眼能删。
   */
  function textToMarks(text, pool) {
    const s = String(text == null ? "" : text);
    const pairs = markPairs(pool);
    if (!pairs.length) return s;
    const table = new Map();                       // 前缀 -> [{digits, label}]
    for (const p of pairs) {
      const m = MARK_RE.exec(p.mark);
      if (!m) continue;
      const arr = table.get(m[1]) || [];
      arr.push({ digits: m[2], label: p.label });
      table.set(m[1], arr);
    }
    const prefixes = [...table.keys()].sort((a, b) => b.length - a.length);
    let out = "";
    let i = 0;
    while (i < s.length) {
      /* 显式 @ 形态不查前导字符（@ 本身就是标记，且 @ 后面紧跟 CJK 不可能是邮箱）；
       * 裸形态才查，且连汉字一起挡（散文里的「大图片1」不能被认成引用）。 */
      const prevOk = s[i] === "@" ? true : !_BARE_PREV_BAD(s[i - 1]);
      if (prevOk) {
        let j = s[i] === "@" ? i + 1 : i;
        let open = "";
        if (MARK_OPEN.includes(s[j])) { open = s[j]; j += 1; }
        const pf = prefixes.find((p) => s.startsWith(p, j));
        if (pf) {
          let k = j + pf.length;
          while (k < s.length && /\s/.test(s[k])) k += 1;      // 允许「图片 1」中间的空格
          const hit = (table.get(pf) || [])
            .filter((x) => s.startsWith(x.digits, k) && !/[0-9]/.test(s[k + x.digits.length] || ""))
            .sort((a, b) => b.digits.length - a.digits.length)[0];
          const end = k + (hit ? hit.digits.length : 0);
          if (!hit || (open && s[end] !== MARK_CLOSE[open])) {
            /* 有前缀没编号 / 括号不闭合：当普通文字，不动 */
          } else {
            out += "@" + hit.label;
            i = end + (open ? 1 : 0);
            continue;
          }
        }
      }
      out += s[i];
      i += 1;
    }
    return out;
  }

  /** 预留：外部 agent / 本地模型的自动解析分配接口（总提示词框式）。
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
      /* 素材标注编解码（B03）：只用于「提示词 ⇄ LLM」这一跳，绝不落盘 */
      markPairs, marksToText, textToMarks,
      CAMERA_MOVES, CAMERA_AMPS, CAMERA_SPEEDS, RETENTION_MARKERS,
    };
  }
})();
