/* H3 前端 helper：素材标注编解码 + latent 策略。
 *
 * 与后端的两个契约：
 *   · 标注（`图片1` / `视频1` / `音频1`）只活在「提示词 ⇄ LLM」这一跳，
 *     磁盘上永远存**素材名**（`@阿依.png`）——理由见下方 MARK 节的长注释。
 *   · latent 策略随 seg_fields 透存，后端按模式收敛。
 *
 * 结构化提示词（prompt_v2 / 逐镜表单 / 运镜下拉 / 屏显列表）已整体下线：
 * 正文是唯一真相，编辑与「只能靠表单做」的那些活改由 AI 提示词优化承担
 * （规则注入 + 后端确定性收尾 prompts.finalize_optimized + 语义校验）。
 */
(function () {
  "use strict";

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
  const MARK_MAX = 999;   // 与后端 asset_store.MARK_MAX 一致
  const MARK_OPEN = "【[（(「";
  const MARK_CLOSE = { "【": "】", "[": "]", "（": "）", "(": ")", "「": "」" };
  /* `@别名` 语法的前导判据：**必须与后端 nodes._REF_AT 的负向后顾逐字一致**
   * （只挡 ASCII 字母数字下划线）—— 写成含 CJK 的"更严"版本会让
   * `@阿依@回廊` 这种连写漏掉后一个，与 refsFromText / 后端编译结果不一致。 */
  const _AT_PREV_BAD = (c) => /[0-9A-Za-z_]/.test(c || "");
  /* 裸形态（LLM 漏了 @）的前导判据：连汉字也挡，避免把散文里的「大图片1」当引用 */
  const _BARE_PREV_BAD = (c) => /[0-9A-Za-z_\u4e00-\u9fff]/.test(c || "");

  /** 池 → 可用的 (素材名, 标注) 对（标注形态非法/缺失的条目直接跳过）。
   *
   * **全名（ref_name）与别名都要建映射**：正文里 `@猫.png` 和 `@猫` 两种写法都在，
   * 只映射别名的话，`@猫.png` 会被只吃掉 `猫`、把 `.png` 留在正文 ——
   * 和提示词框那个"后缀溢出"是同一个坑（匹配表不全）。
   * 全名先入，于是"标注 → 名字"回写时优先还原成全名。 */
  function markPairs(pool) {
    const out = [];
    for (const a of (pool || [])) {
      const mark = String((a && a.mark) || "").trim();
      if (!MARK_RE.test(mark)) continue;
      const names = [String((a && a.ref_name) || "").trim(),
        String((a && (a.label || a.alias)) || "").trim()];
      for (const n of names) {
        if (n && !out.some((p) => p.label === n)) out.push({ label: n, mark });
      }
    }
    return out;
  }

  /** 按素材名（全名或别名皆可）查标注；查不到返回 ""。
   *  给 LLM 的素材名单用它取显示名 —— 名单写短编号，模型才不容易抄错。 */
  function markOf(pool, nm) {
    const want = String(nm || "").trim();
    if (!want) return "";
    const p = markPairs(pool).find((x) => x.label === want);
    return p ? p.mark : "";
  }

  /** 标注归一：形态合法就返回去空白后的文本，否则 ""（与后端 clean_mark 逐字同规则）。
   *  校验必须和后端用同一条尺子 —— 前端松一点，老存档里的垃圾标注（"图1"/"图片0"/
   *  "图片1000"）就会被当成有效号占位，后端却判非法重新发号，两边显示的号对不上。 */
  function cleanMark(s) {
    const t = String(s == null ? "" : s).trim();
    const m = MARK_RE.exec(t);
    /* 光形状对不够，还得看号在 1..999：**图片0 是非法号**（后端 clean_mark 同判据） */
    if (!m) return "";
    const n = Number(m[2]);
    return n >= 1 && n <= MARK_MAX ? `${m[1]}${n}` : "";
  }

  /** 形态是否合法（只看形状，不查重） */
  function markShaped(s) { return MARK_RE.test(String(s == null ? "" : s).trim()); }

  /** 下一个可用号 = 当前最大号 +1（**编号不回收**）。
   *  为什么不用"最小空闲号"：删掉 2 号后补成 2 的话，旧的提示词/LLM 对话里
   *  写的「图片2」会突然指向另一张素材（串号），而且完全静默。留空更安全。 */
  function nextMarkSeq(kind, marks) {
    const k = String(kind || "image");
    const want = k === "video" ? "视频" : k === "audio" ? "音频" : "图片";
    let mx = 0;
    for (const m of marks || []) {
      const g = /^(图片|视频|音频)(\d{1,3})$/.exec(String(m || "").trim());
      if (g && g[1] === want) mx = Math.max(mx, Number(g[2]));
    }
    return mx + 1;
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

  /** 回（LLM 产出）：**官方标签** → `@素材名`（回填兜底，2026-10-04）。
   *
   * 为什么需要它：发给 LLM 的素材名是**标注**（`@图片1`），但官方 ref-en.txt
   * 要求镜头正文用 `<Subject N>` / `<Picture N>` / `<Video N>` / `<Audio N>` 指代
   * 参考内容 —— 模型两边都听，**有时就把官方标签直接写进产出**。而 `textToMarks`
   * 只认标注（官方 token 属于"另一层编号"，故意不动）。于是官方标签原样落盘：
   * ① 提示词框里出现 `<Picture 1>`（用户看到的"picture1"）；
   * ② `refsFromText` 只扫 `@素材名`，扫不到 → 本段引用为空 → 素材不进模型
   *   （用户报的"有些时候无法正确识别素材"）。
   * 所以回填必须再补这一跳：官方标签 → `@素材名`，把引用关系还原回来。
   *
   * 编号口径 = **送进 LLM 的 media 顺序**（`collectSegMedia` 与执行期
   * `nodes._kind_tokens` 同口径：帧锚前置 + 参考素材顺延，各类别独立从 1 编号），
   * 所以 `<Picture 1>` 就是 media 里第 1 张图，与执行期实跑的 `<Picture k>` 一致。
   * media 项没有标注时 label 本身就是素材名，直接用它。
   *
   * **译不出的（编号越界 / media 里没有）原样保留** —— 不猜、不吞，宁可留个
   * 明显的红框/告警让用户看见，也不静默指向错图。
   *
   * `<Subject N>` **不在此列**：它是"可复用可见内容"的抽象标签（角色/场景/风格），
   * 不是素材引用，编号也由模型自行定义（与 media 顺序无对应关系）—— 保留给 H3
   * 模型读，执行期直通。
   */
  function officialToMarks(text, media, pool) {
    const s = String(text == null ? "" : text);
    if (!s || !Array.isArray(media) || !media.length) return s;
    if (!/<(?:Picture|Video|Audio)\s+\d+>/.test(s)) return s;
    /* 标注 -> 素材名（全名优先；同一标注只认第一个，与 markPairs 同口径） */
    const nameOf = {};
    for (const a of (pool || [])) {
      const mk = cleanMark(a && a.mark);
      const nm = String((a && (a.ref_name || a.label || a.alias)) || "").trim();
      if (mk && nm && !nameOf[mk]) nameOf[mk] = nm;
    }
    const seq = { Picture: [], Video: [], Audio: [] };
    for (const m of media) {
      const k = String((m && m.kind) || "image").toLowerCase();
      const tag = k === "video" ? "Video" : (k === "audio" ? "Audio" : "Picture");
      const lbl = String((m && m.label) || "").trim();
      /* label 是**标注**（图片1）时必须有映射才敢用 —— 池里查不到就说明这张图
       * 已经不在池里，硬译成 `@图片1` 只会留个假引用；label 本身是素材名
       * （帧锚无标注的情形）才直接采用。 */
      seq[tag].push(nameOf[lbl] || (cleanMark(lbl) ? "" : lbl));
    }
    return s.replace(/<(Picture|Video|Audio)\s+(\d+)>/g, (full, tag, num) => {
      const nm = seq[tag][Number(num) - 1];
      return nm ? "@" + nm : full;
    });
  }

  /* ---- latent 策略（二采/续接用，随 seg_fields 透存） ---- */

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
      /* 素材标注编解码（B03）：只用于「提示词 ⇄ LLM」这一跳，绝不落盘 */
      markPairs, markOf, marksToText, textToMarks, officialToMarks,
      cleanMark, markShaped, nextMarkSeq,
      /* latent 策略 */
      defaultLatentSave, cleanLatentSave,
    };
  }
})();
