/* H3 素材库浏览器（Library）—— 一个浏览器 + 四个 scope。
 *
 * 蓝本：Majoor Assets Manager。核心照抄的一条是**交互模型**：
 *   瓦片只放「缩略图 + 名称 + 徽标」，所有动作收进【右键菜单 / 预览器】。
 *   （之前把 7~8 个动作做成瓦片按钮，窄栏里必然换行溢出 —— 这才是"展开看不到"的病根。）
 *
 * 四个 scope：项目资产 / 全局库 / 成片 / latent，外加「收藏集」过滤。
 * 没有「全部」——四个库各管各的，混一栏只会让人分不清文件到底在哪。
 * 后端 library.py 只做索引 + 查询；本文件只做 UI 与动作分发。
 *
 * 库间搬运动作（**都是显式点击，绝不顺手复制**）：
 *   全局库  →「调入项目」= 复制一份进项目 assets/（项目自包含，不生成链接）
 *   项目资产→「存入全局库」= 全局库多一份（跨项目复用）
 *   成片    →「调入项目」+「存入全局库」（同上两个方向）
 *
 * 用法：window.H3Lib.open({ dir, seg, onChanged })
 *   dir       项目目录名（决定 project/finals/latent 三个 scope 的数据源）
 *   seg       当前选中段（1-based），供「引用到段」用
 *   onChanged 动作改了工程数据后回调（让导演台刷新）
 *
 * **挑选模式**：传 `onPick(item)` 即进入挑选模式——单击瓦片 = 选中该素材并关闭，
 * 回调收到整条库条目（id / scope / kind / file / asset_id / name）。锚定面板的
 * 「选素材」就走这里：复用同一套搜索 / 筛选 / 四个 scope / 缩略图，**不再另造
 * 第二个素材浏览器**（两套 UI 看同一批文件，必然漂移）。
 *   pickKinds 限制类型下拉（如 ["image","video","latent"]——锚源只收这三类）
 */
(function () {
  "use strict";

  /* 没有「全部」：四个库的文件位置语义不同，混一栏看不出东西在哪，
   * 而且上传落点是按当前 scope 决定的，「全部」会让落点含糊。 */
  const SCOPES = [
    ["project", "项目资产"],
    ["global", "全局库"],
    ["finals", "成片"],
    ["latent", "latent"],
  ];
  const KINDS = [
    ["all", "全部类型"], ["media", "图/视/音"], ["image", "图片"],
    ["video", "视频"], ["audio", "音频"], ["latent", "latent"],
  ];
  const SORTS = [
    ["mtime", "按时间"], ["name", "按名称"], ["size", "按大小"],
    ["rating", "按评分"], ["kind", "按类型"],
  ];
  // 与后端 library.MOVE_TARGETS 同源：scope -> 中文名（徽标提示用）
  const SCOPE_CN = {
    project: "项目资产", global: "全局库", finals: "成片", latent: "latent",
  };
  const RATINGS = [
    ["0", "全部星级"], ["1", "≥1★"], ["2", "≥2★"],
    ["3", "≥3★"], ["4", "≥4★"], ["5", "只看 5★"],
  ];
  const KIND_ICON = { video: "🎞", audio: "🎵", latent: "🧬", image: "🖼" };
  const KIND_CN = { image: "图片", video: "视频", audio: "音频", latent: "latent" };

  let S = null;          // 当前会话状态
  let styled = false;
  let io = null;         // IntersectionObserver（懒加载）

  /* ---------- 小工具 ---------- */

  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }

  function fmtTime(t) {
    const n = Number(t) || 0;
    if (!n) return "";
    const d = new Date(n * 1000);
    const p = (x) => String(x).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  let toastEl = null;
  let toastTimer = 0;

  /** 浮层提示：底部状态行会被预览器挡住，只靠它会"点了没反应"。 */
  function toast(msg, isErr) {
    const s = String(msg || "");
    if (!s) return;
    if (toastEl) toastEl.remove();
    clearTimeout(toastTimer);
    toastEl = el("div", "h3l-toast" + (isErr ? " err" : ""), esc(s));
    document.body.append(toastEl);
    toastTimer = setTimeout(() => {
      if (toastEl) { toastEl.remove(); toastEl = null; }
    }, isErr ? 6500 : 3600);
  }

  function say(t) {
    const s = String(t || "");
    if (S && S.msg) S.msg.textContent = s;
    if (s) toast(s);
  }

  /** 失败反馈：红框 + 底部状态行（两处都写，保证看得见）。 */
  function fail(t) {
    const s = String(t || "操作失败");
    if (S && S.msg) S.msg.textContent = s;
    toast(s, true);
  }

  function api() {
    const A = window.H3Api;
    if (!A || !A.libList) throw new Error("素材库接口未就绪（h3_api.js 未加载）");
    return A;
  }

  /* ---------- 自建输入弹窗 ----------
   * 不用 window.prompt：ComfyUI 前端 / 部分嵌入壳会吞掉原生弹窗
   * （点了按钮毫无反应，且没有任何报错 —— 「改名没用」的另一种表现）。 */
  function askText(opt) {
    const o = opt || {};
    return new Promise((resolve) => {
      const ov = el("div", "h3l-modal");
      const box = el("div", "h3l-modalbox");
      box.append(el("h3", "", esc(o.title || "")));
      if (o.hint) box.append(el("p", "h3l-modalhint", esc(o.hint)));
      const inp = el("input", "h3l-input");
      inp.type = "text";
      inp.value = String(o.value == null ? "" : o.value);
      if (o.maxLength) inp.maxLength = Number(o.maxLength);
      inp.placeholder = String(o.placeholder || "");
      box.append(inp);
      const row = el("div", "h3l-modalrow");
      const ok = el("button", "h3l-btn h3l-btn-cta", esc(o.okText || "确定"));
      const no = el("button", "h3l-btn", "取消");
      ok.type = "button"; no.type = "button";
      row.append(no, ok);
      box.append(row);
      ov.append(box);
      const done = (v) => {
        ov.remove();
        document.removeEventListener("keydown", onKey);
        resolve(v);
      };
      const onKey = (e) => {
        if (e.key === "Escape") done(null);
        else if (e.key === "Enter") done(inp.value);
      };
      ok.onclick = () => done(inp.value);
      no.onclick = () => done(null);
      ov.addEventListener("pointerdown", (e) => { if (e.target === ov) done(null); });
      document.body.append(ov);
      document.addEventListener("keydown", onKey);
      inp.focus();
      inp.select();
    });
  }

  /** 星级行：瓦片/预览器共用；点当前星级=清除（0 星）。 */
  function starRow(rating, onPick, small) {
    const r = Number(rating) || 0;
    const box = el("div", "h3l-stars" + (small ? " sm" : ""));
    for (let i = 1; i <= 5; i++) {
      const s = el("span", i <= r ? "on" : "", "★");
      s.title = `${i} 星` + (i === r ? "（再点清除评分）" : "");
      s.onclick = (e) => {
        e.stopPropagation();
        if (typeof onPick === "function") onPick(i === r ? 0 : i);
      };
      box.append(s);
    }
    return box;
  }

  /* ---------- 样式（自带一套，不依赖导演台） ---------- */

  function injectStyles() {
    if (styled) return;
    styled = true;
    const css = `
.h3l-overlay{position:fixed;inset:0;z-index:1000005;background:#0b0b09e6;display:flex;align-items:center;justify-content:center;padding:18px}
.h3l-box{width:min(1660px,100%);height:100%;display:flex;flex-direction:column;background:#141310;border:1px solid #37332b;border-radius:14px;overflow:hidden;color:#d9d4c9;font:13px/1.5 "Microsoft YaHei UI","Segoe UI",sans-serif}
.h3l-head{display:flex;gap:10px;align-items:center;padding:11px 14px;border-bottom:1px solid #37332b;background:#1b1a16;flex-wrap:wrap}
.h3l-head strong{font-size:15px;color:#f0ece2}
.h3l-scopes{display:flex;gap:6px;flex-wrap:wrap}
.h3l-scope{padding:5px 13px;border:1px solid #3a352c;border-radius:14px;background:#211f1a;color:#a8a294;cursor:pointer;font-size:12.5px;font-family:inherit}
.h3l-scope:hover{border-color:#46604f;color:#d9d4c9}
.h3l-scope.on{border-color:#316dca;background:#1f2f45;color:#9ecbff}
.h3l-scope em{font-style:normal;color:#7f7a70;margin-left:5px;font-size:11px}
.h3l-spacer{flex:1}
.h3l-hint{font-size:12px;color:#8f8a7d}
.h3l-picking .h3l-tile:hover{border-color:#316dca}
.h3l-close{width:30px;height:30px;border:1px solid #3a352c;border-radius:8px;background:#211f1a;color:#a8a294;cursor:pointer;font-size:15px;line-height:1}
.h3l-close:hover{border-color:#9a4144;color:#f0a0a4}
.h3l-bar{display:flex;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid #2b2822;background:#171612;flex-wrap:wrap}
.h3l-search{flex:1;min-width:200px;border:1px solid #3a352c;border-radius:8px;background:#211f1a;color:#d9d4c9;padding:7px 11px;font-size:13px;outline:none}
.h3l-search:focus{border-color:#a8d8bd}
.h3l-bar select{border:1px solid #3a352c;border-radius:7px;background:#211f1a;color:#d9d4c9;padding:6px 8px;font-size:12.5px;outline:none}
.h3l-btn{padding:6px 13px;border:1px solid #3a352c;border-radius:8px;background:#211f1a;color:#a8a294;cursor:pointer;font-size:12.5px;font-family:inherit}
.h3l-btn:hover{border-color:#46604f;color:#d9d4c9}
.h3l-btn.on{border-color:#316dca;background:#1f2f45;color:#9ecbff}
.h3l-btn-cta{border-color:#2f6e57;background:#12291f;color:#7fe0b0}
.h3l-grid{flex:1;overflow-y:auto;padding:14px;display:grid;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));gap:11px;align-content:start}
.h3l-tile{display:flex;flex-direction:column;gap:5px;padding:8px;border:1px solid #37332b;border-radius:10px;background:#181712;cursor:pointer;min-width:0}
.h3l-tile:hover{border-color:#46604f;background:#1d1c17}
.h3l-tile.sel{border-color:#316dca;box-shadow:0 0 0 1px #316dca inset}
.h3l-thumb{position:relative;width:100%;height:118px;border-radius:7px;background:#0d0c0a;overflow:hidden;display:flex;align-items:center;justify-content:center}
.h3l-thumb img,.h3l-thumb video{width:100%;height:100%;object-fit:cover}
.h3l-thumb .h3l-ico{font-size:34px;line-height:1;opacity:.75}
.h3l-star{position:absolute;left:5px;top:5px;padding:1px 6px;border-radius:9px;background:#000000b3;color:#e9c07a;font-size:10.5px}
.h3l-role{position:absolute;right:5px;top:5px;padding:1px 6px;border-radius:9px;background:#12291fcc;color:#7fe0b0;font-size:10.5px}
.h3l-name{font-weight:600;font-size:12.5px;word-break:break-all;color:#f0ece2}
.h3l-meta{font-size:10.5px;color:#8a857b;word-break:break-all;display:flex;gap:5px;flex-wrap:wrap}
.h3l-badges{display:flex;gap:3px;flex-wrap:wrap}
.h3l-tbtns{display:flex;gap:4px;flex-wrap:wrap;margin-top:2px}
.h3l-tbtn{padding:2px 7px;border:1px solid #3a352c;border-radius:7px;background:#211f1a;color:#a8a294;cursor:pointer;font-size:10.5px;font-family:inherit}
.h3l-tbtn:hover{border-color:#46604f;color:#d9d4c9}
.h3l-tbtn.on{border-color:#2f6e57;background:#12291f;color:#7fe0b0}
.h3l-tbtn.danger:hover{border-color:#9a4144;color:#f0a0a4}
.h3l-chip{padding:1px 7px;border:1px solid #2f6e57;border-radius:9px;background:#12291f;color:#7fe0b0;font-size:10px}
.h3l-tag{padding:1px 7px;border:1px solid #3a352c;border-radius:9px;color:#a8a294;font-size:10px}
.h3l-tag.dim{border-style:dashed;border-color:#4d4333;color:#9c8a63;cursor:help}
.h3l-foot{display:flex;gap:10px;align-items:center;padding:9px 14px;border-top:1px solid #37332b;background:#1b1a16;font-size:12px;color:#8a857b;flex-wrap:wrap}
.h3l-empty{grid-column:1/-1;padding:44px 12px;text-align:center;color:#7f7a70}
.h3l-msg{flex:1;color:#a8a294;font-size:12px;min-width:120px}
.h3l-menu{position:fixed;z-index:1000007;min-width:210px;max-height:70vh;overflow:auto;background:#1e1c18;border:1px solid #46604f;border-radius:9px;padding:5px;box-shadow:0 10px 34px #000c}
.h3l-menu button{display:block;width:100%;text-align:left;padding:7px 10px;border:0;border-radius:6px;background:transparent;color:#d9d4c9;cursor:pointer;font-size:12.5px;font-family:inherit}
.h3l-menu button:hover{background:#24402f;color:#7fe0b0}
.h3l-menu button.danger:hover{background:#3a1a1c;color:#f0a0a4}
.h3l-menu .h3l-sep{height:1px;margin:5px 6px;background:#37332b}
.h3l-menu .h3l-cap{padding:6px 10px 3px;color:#7f7a70;font-size:10.5px}
.h3l-viewer{position:fixed;inset:0;z-index:1000006;background:#000000f2;display:flex;gap:0}
.h3l-vstage{flex:1;display:flex;align-items:center;justify-content:center;padding:22px;min-width:0}
.h3l-vstage img,.h3l-vstage video{max-width:100%;max-height:100%;border-radius:8px}
.h3l-vstage audio{width:min(560px,90%)}
.h3l-vside{width:320px;flex:none;background:#16150f;border-left:1px solid #37332b;padding:16px;overflow:auto;display:flex;flex-direction:column;gap:9px}
.h3l-vside h3{margin:0 0 4px;font-size:15px;color:#f0ece2;word-break:break-all}
.h3l-vrow{font-size:12px;color:#a8a294;word-break:break-all}
.h3l-vrow b{color:#d9d4c9;font-weight:600}
.h3l-vact{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.h3l-stars{display:flex;gap:2px}
.h3l-stars span{cursor:pointer;font-size:17px;color:#5c574d;line-height:1}
.h3l-stars span.on{color:#e9c07a}
.h3l-stars.sm span{font-size:13px}
.h3l-stars span:hover{color:#e9c07a}
.h3l-modal{position:fixed;inset:0;z-index:1000010;background:#000000b8;display:flex;align-items:center;justify-content:center;padding:18px}
.h3l-modalbox{width:min(460px,100%);background:#1b1a16;border:1px solid #46604f;border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:9px;box-shadow:0 14px 40px #000c}
.h3l-modalbox h3{margin:0;font-size:14.5px;color:#f0ece2;word-break:break-all}
.h3l-modalhint{margin:0;font-size:12px;color:#8a857b;line-height:1.5;white-space:pre-wrap}
.h3l-input{border:1px solid #3a352c;border-radius:8px;background:#211f1a;color:#d9d4c9;padding:8px 10px;font-size:13px;font-family:inherit;outline:none}
.h3l-input:focus{border-color:#a8d8bd}
.h3l-modalrow{display:flex;gap:8px;justify-content:flex-end;margin-top:2px}
.h3l-toast{position:fixed;right:22px;top:22px;z-index:1000009;max-width:min(460px,80vw);padding:11px 15px;border:1px solid #46604f;border-radius:10px;background:#16241c;color:#c9f0d8;font:13px/1.55 "Microsoft YaHei UI","Segoe UI",sans-serif;box-shadow:0 8px 28px #000b;white-space:pre-wrap;word-break:break-word}
.h3l-toast.err{border-color:#9a4144;background:#2c1618;color:#f3b6ba}
`;
    const s = document.createElement("style");
    s.textContent = css;
    document.head.append(s);
  }

  /* ---------- 数据 ---------- */

  async function fetchPage(append) {
    const A = api();
    if (!append) S.page = 1;
    const res = await A.libList({
      dir: S.dir, q: S.q, scope: S.scope, kind: S.kind,
      sort: S.sort, order: S.order, page: S.page, page_size: S.pageSize,
      collection: S.collection || "", min_rating: S.minRating || 0,
    });
    if (!res.body?.ok) { fail(A.errText(res, "读取素材失败")); return; }
    const d = res.body.data || {};
    S.items = append ? S.items.concat(d.items || []) : (d.items || []);
    S.total = d.total || 0;
    S.totalPages = d.total_pages || 1;
    S.page = d.page || 1;
    S.counters = res.body.counters || {};
    renderGrid();
    renderFoot();
    renderScopes();
  }
  /* 注：成片**不再**打开就自动存一份进全局库（原来那段 autoArchive 已删）。
   * 静默复制正是"诡异"的来源；入库改成瓦片上的「存入全局库」显式按钮，
   * 顺带省掉每次打开成片都要给大视频算一遍 sha256。 */

  async function refreshCollections() {
    const A = api();
    try {
      const r = await A.libCollections(S.dir);
      S.collections = (r.body?.ok && r.body.collections) || [];
    } catch (e) { S.collections = []; }
  }

  /* ---------- 渲染 ---------- */

  function renderScopes() {
    S.scopeBox.replaceChildren();
    for (const [k, zh] of SCOPES) {
      const n = S.counters[k] || 0;
      const b = el("button", "h3l-scope" + (S.scope === k ? " on" : ""),
        esc(zh) + `<em>${n}</em>`);
      b.type = "button";
      b.onclick = () => {
        S.scope = k;
        S.sel.clear();
        if (typeof S.paintUpBtn === "function") S.paintUpBtn();   // 上传落点跟着库变
        fetchPage(false);
      };
      S.scopeBox.append(b);
    }
    if (S.collections.length) {
      const sel = el("select");
      sel.append(new Option("收藏集…", ""));
      for (const c of S.collections) sel.append(new Option(`${c.name}（${(c.items || []).length}）`, c.id));
      sel.value = S.collection || "";
      sel.onchange = () => { S.collection = sel.value; S.sel.clear(); fetchPage(false); };
      S.scopeBox.append(sel);
    }
  }

  /** 目标库里已经有同名文件了吗？有 -> 这个方向的「调入」按钮不画出来。
   *
   * `it.blocked` 由后端 `lib_list` 用**完整索引**算好（前端只有当前页，
   * 自己比对会漏掉没加载到的条目）。值是目标 scope 数组，如 ["project"]。
   */
  function isBlocked(it, target) {
    return Array.isArray(it.blocked) && it.blocked.indexOf(target) >= 0;
  }

  /** 瓦片上的常驻动作：只留真正常用的几个（其余进右键菜单，别糊满瓦片）。 */
  function tileButtons(it) {
    const box = el("div", "h3l-tbtns");
    const mk = (label, on, fn, danger) => {
      const b = el("button", "h3l-tbtn" + (on ? " on" : "") + (danger ? " danger" : ""), esc(label));
      b.type = "button";
      b.onclick = (e) => { e.stopPropagation(); fn(); };
      box.append(b);
    };
    const roles = it.roles || [];
    /* 首尾帧标注已挪到导演台「提示词框 · 资产引用」栏（每段两个按钮，按段指定），
     * 素材库不再打标——旧标注仍会显示徽标，且旧项目照常生效（后端未删）。 */
    if (roles.length) {
      mk(`✓ ${roles.join("·")}`, true,
        () => say(`「${it.name}」带有旧的首尾帧标注：现在请在导演台每段的「资产引用」栏指定首/尾帧图`));
    }
    /* 库间搬运：全局库 → 项目（复制文件），项目 / 成片 → 全局库（存入复用）。
     * 每个方向只留一个按钮，语义就是字面意思，没有"链接引用"这种第二种形态。 */
    if (it.scope === "global") {
      if (!isBlocked(it, "project")) mk("调入项目", false, () => actBring(it));
    }
    if (it.scope === "finals") {
      if (!isBlocked(it, "project")) mk("调入项目", false, () => actToAssets(it));
      if (!isBlocked(it, "global")) mk("存入全局库", false, () => actArchive(it));
    }
    if (it.scope === "project" && !it.linked && !isBlocked(it, "global")) {
      mk("存入全局库", false, () => actArchive(it));
    }
    if (it.scope === "project" || it.scope === "global") {
      mk("改名", false, () => actAlias(it));
    }
    mk("删除", false, () => actDelete([it.id]), true);
    return box;
  }

  /** 写评分：成功后就地重画（不整页重拉，滚动位置与勾选都保住）。 */
  async function setRating(it, val, repaint) {
    const A = api();
    try {
      const r = await A.libRate(S.dir, it.id, val);
      if (!r.body?.ok) { fail(A.errText(r, "评分失败")); return; }
      it.rating = Number(r.body.rating) || 0;
      if (typeof repaint === "function") repaint();
    } catch (e) {
      fail(`评分失败：${e?.message || e}`);
    }
  }

  function tileFor(it) {
    const t = el("div", "h3l-tile" + (S.sel.has(it.id) ? " sel" : ""));
    t.dataset.id = it.id;
    const th = el("div", "h3l-thumb");
    th.append(el("span", "h3l-ico", KIND_ICON[it.kind] || "📄"));
    th.dataset.src = api().libRawUrl(S.dir, it.id);
    th.dataset.kind = it.kind;
    th.dataset.thumb = api().libThumbUrl(S.dir, it.id);
    if ((it.roles || []).length) {
      th.append(el("span", "h3l-role", esc(it.roles.join("·"))));
    }
    t.append(th);
    t.append(el("div", "h3l-name", esc(it.name)));
    /* 星级常驻瓦片：不用进预览器才能打分（点当前星级=清除） */
    let stars = null;
    const paintStars = () => {
      const row = starRow(it.rating, (v) => setRating(it, v, paintStars), true);
      if (stars) stars.replaceWith(row);
      stars = row;
    };
    paintStars();
    t.append(stars);
    const bits = [KIND_CN[it.kind] || it.kind];
    if (it.linked) bits.push("🔗 链接（文件在全局库）");
    else if (it.size) bits.push(fmtSize(it.size));
    if (it.mtime) bits.push(fmtTime(it.mtime));
    t.append(el("div", "h3l-meta", bits.map(esc).join(" · ")));
    const bg = el("div", "h3l-badges");
    for (const sn of it.refs || []) bg.append(el("span", "h3l-chip", `段${sn}`));
    for (const tg of it.tags || []) bg.append(el("span", "h3l-tag", esc(tg)));
    /* 调入按钮被"目标库已有同名"挡掉时，留一个说明徽标 —— 否则用户只看到
     * 按钮不见了，不知道是为什么（这不是按钮，纯文字）。 */
    for (const b of it.blocked || []) {
      const cn = SCOPE_CN[b] || b;
      const chip = el("span", "h3l-tag dim", esc(`同名已在${cn}`));
      chip.title = `${cn}里已经有同名文件，所以这个方向的调入按钮不显示`;
      bg.append(chip);
    }
    if (bg.children.length) t.append(bg);
    // 常用动作直接摆在瓦片上（不要藏进"双击才出现"的界面）
    t.append(tileButtons(it));

    t.addEventListener("dblclick", () => { if (!S.pick) openViewer(it); });
    t.addEventListener("click", (e) => {
      // 挑选模式：单击即选中并关闭（瓦片上的按钮/星级都 stopPropagation，
      // 所以「改名 / 删除 / 打分」不会误触发选中）
      if (S.pick) {
        const cb = S.pick, done = S.close;
        t.classList.add("sel");
        if (typeof done === "function") done();
        cb(it);
        return;
      }
      if (S.multi) {
        if (S.sel.has(it.id)) S.sel.delete(it.id);
        else S.sel.add(it.id);
        t.classList.toggle("sel", S.sel.has(it.id));
        renderFoot();
        return;
      }
      S.cur = it;
      // 单选模式下单击 = 选中（右键菜单作用于它）
      document.querySelectorAll(".h3l-tile.sel").forEach((n) => n.classList.remove("sel"));
      t.classList.add("sel");
      S.sel.clear();
      S.sel.add(it.id);
      renderFoot();
    });
    t.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      if (!S.sel.has(it.id)) { S.sel.clear(); S.sel.add(it.id); }
      openMenu(e.clientX, e.clientY, it);
    });
    if (io) io.observe(th);
    return t;
  }

  function renderGrid() {
    if (io) io.disconnect();
    S.grid.replaceChildren();
    if (!S.items.length) {
      S.grid.append(el("div", "h3l-empty",
        S.q ? "没有匹配的素材" : "这里还没有素材：把文件拖进来，或用「上传」按钮"));
      return;
    }
    // 懒加载：缩略图进入视野才发请求（图片走服务端缩略图，视频只解码首帧）
    io = new IntersectionObserver((entries) => {
      for (const en of entries) {
        if (!en.isIntersecting) continue;
        const d = en.target;
        io.unobserve(d);
        const kind = d.dataset.kind;
        // 角标（角色标注）是建瓦片时就挂上的，replaceChildren 会一并抹掉 —— 留住
        const badges = [...d.children].filter(
          (n) => n.classList && (n.classList.contains("h3l-role")
            || n.classList.contains("h3l-star")));
        if (kind === "image") {
          const im = document.createElement("img");
          im.loading = "lazy";
          im.src = d.dataset.thumb;
          im.onerror = () => { im.remove(); };
          d.replaceChildren(im, ...badges);
        } else if (kind === "video") {
          const v = document.createElement("video");
          v.muted = true; v.preload = "metadata"; v.src = d.dataset.src;
          v.onerror = () => v.remove();
          d.replaceChildren(v, ...badges);
        }
      }
    }, { root: S.grid, rootMargin: "240px" });
    const frag = document.createDocumentFragment();
    for (const it of S.items) frag.append(tileFor(it));
    S.grid.append(frag);
  }

  function renderFoot() {
    S.footInfo.textContent =
      `第 ${S.page}/${S.totalPages} 页 · 共 ${S.total} 项` +
      (S.sel.size ? ` · 已选 ${S.sel.size}` : "");
    S.moreBtn.style.display = S.page < S.totalPages ? "" : "none";
  }

  /* ---------- 右键菜单 ---------- */

  function closeMenu() {
    document.querySelectorAll(".h3l-menu").forEach((n) => n.remove());
  }

  function menuItem(label, fn, danger) {
    const b = el("button", danger ? "danger" : "", esc(label));
    b.type = "button";
    b.onclick = () => { closeMenu(); fn(); };
    return b;
  }

  function openMenu(x, y, it) {
    closeMenu();
    const ids = [...S.sel];
    const many = ids.length > 1;
    const m = el("div", "h3l-menu");
    if (many) m.append(el("div", "h3l-cap", `已选 ${ids.length} 项`));

    if (!many) {
      m.append(menuItem("👁 预览", () => openViewer(it)));
      m.append(el("div", "h3l-sep"));
    }
    /* 首尾帧标注已挪到导演台「提示词框 · 资产引用」栏（按段指定），此处不再打标 */
    if (!many) {
      m.append(el("div", "h3l-sep"));
      m.append(menuItem("⭐ 评分…", () => actRate(it)));
      m.append(menuItem("🏷 标签…", () => actTag(it)));
      m.append(menuItem("✏ 重命名…", () => actAlias(it)));
      m.append(el("div", "h3l-sep"));
    }
    if (many) {
      // 目标库已有同名的直接排除在批量动作之外（与瓦片按钮同一套判定）
      const gl = S.items.filter((x) => S.sel.has(x.id) && x.scope === "global"
        && !isBlocked(x, "project"));
      if (gl.length) {
        m.append(menuItem(`⇩ 把选中的 ${gl.length} 个全局素材调入项目（复制）`,
          () => actBringMany(gl)));
        m.append(el("div", "h3l-sep"));
      }
      // latent 不能入库（全局库只收 image/video/audio），过滤掉免得后端逐个报错
      const up = S.items.filter((x) => S.sel.has(x.id)
        && (x.scope === "project" || x.scope === "finals") && !x.linked
        && !isBlocked(x, "global"));
      if (up.length) {
        m.append(menuItem(`⬆ 把选中的 ${up.length} 个存入全局库`, () => actArchiveMany(up)));
        m.append(el("div", "h3l-sep"));
      }
    }
    if (it.scope === "global" && !many) {
      if (!isBlocked(it, "project")) {
        m.append(menuItem("⇩ 调入项目（复制一份到项目 assets/）", () => actBring(it)));
      }
    } else if (it.scope === "finals" && !many) {
      if (!isBlocked(it, "project")) {
        m.append(menuItem("→ 调入项目（从成片挪进 assets/，成片库里这份会移走）",
          () => actToAssets(it)));
      }
      if (!isBlocked(it, "global")) {
        m.append(menuItem("⬆ 存入全局库（跨项目可复用）", () => actArchive(it)));
      }
    } else if (it.scope === "project" && !many && !it.linked && !isBlocked(it, "global")) {
      m.append(menuItem("⬆ 存入全局库（跨项目可复用）", () => actArchive(it)));
    }
    m.append(menuItem("📋 复制路径", () => actCopyPath(it)));
    m.append(menuItem("📥 下载文件" + (many ? "（打包）" : ""), () => actDownload(it, ids)));
    m.append(menuItem("🗜 打包 ZIP" + (many ? `（${ids.length} 个）` : ""), () => actZip(ids)));
    m.append(el("div", "h3l-sep"));
    if (!many) {
      m.append(el("div", "h3l-sep"));
      m.append(menuItem("🗑 删除文件", () => actDelete(ids), true));
    } else {
      m.append(el("div", "h3l-sep"));
      m.append(menuItem(`🗑 删除 ${ids.length} 个文件`, () => actDelete(ids), true));
    }
    document.body.append(m);
    const r = m.getBoundingClientRect();
    m.style.left = Math.max(6, Math.min(x, window.innerWidth - r.width - 8)) + "px";
    m.style.top = Math.max(6, Math.min(y, window.innerHeight - r.height - 8)) + "px";
    setTimeout(() => {
      const off = (ev) => {
        if (!m.contains(ev.target)) { closeMenu(); document.removeEventListener("pointerdown", off); }
      };
      document.addEventListener("pointerdown", off);
    }, 0);
  }

  /* ---------- 预览器 ---------- */

  function openViewer(it) {
    const A = api();
    const v = el("div", "h3l-viewer");
    const stage = el("div", "h3l-vstage");
    const src = A.libRawUrl(S.dir, it.id);
    if (it.kind === "image") {
      const im = document.createElement("img");
      im.src = src;
      stage.append(im);
    } else if (it.kind === "video") {
      const vd = document.createElement("video");
      vd.src = src; vd.controls = true; vd.autoplay = true;
      stage.append(vd);
    } else if (it.kind === "audio") {
      const au = document.createElement("audio");
      au.src = src; au.controls = true; au.autoplay = true;
      stage.append(au);
    } else {
      stage.append(el("div", "h3l-empty", "latent 文件没有可直接预览的画面"));
    }

    const side = el("div", "h3l-vside");
    side.append(el("h3", "", esc(it.name)));
    const rows = [
      ["类型", KIND_CN[it.kind] || it.kind],
      ["来源", it.origin || it.scope],
      ["文件", it.file],
      ["大小", fmtSize(it.size)],
      ["修改", fmtTime(it.mtime)],
    ];
    if ((it.refs || []).length) rows.push(["被引用", (it.refs || []).map((n) => "段" + n).join("、")]);
    if ((it.roles || []).length) rows.push(["角色", (it.roles || []).join("、")]);
    if (it.tags && it.tags.length) rows.push(["标签", it.tags.join("、")]);
    for (const [k, v2] of rows) {
      side.append(el("div", "h3l-vrow", `<b>${esc(k)}</b>：${esc(v2)}`));
    }
    let stars = null;
    const paintStars = () => {
      const row = starRow(it.rating, (v) => { setRating(it, v, paintStars); });
      if (stars) stars.replaceWith(row);
      stars = row;
    };
    paintStars();
    side.append(stars);

    const acts = el("div", "h3l-vact");
    const add = (label, fn) => {
      const b = el("button", "h3l-btn", label);
      b.type = "button";
      b.onclick = fn;
      acts.append(b);
      return b;
    };
    // 预览器只做"看" + 低频项：常用动作都摆在瓦片上了，这儿不重复一遍
    add("下载", () => actDownload(it, [it.id]));
    add("标签", () => actTag(it));
    side.append(acts);

    const bar = el("div", "h3l-vact");
    const close = el("button", "h3l-btn", "关闭（Esc）");
    close.type = "button";
    close.onclick = () => v.remove();
    bar.append(close);
    side.append(bar);

    v.append(stage, side);
    v.addEventListener("pointerdown", (e) => { if (e.target === v) v.remove(); });
    overlayKeydownOnce(v);
    document.body.append(v);
  }

  function overlayKeydownOnce(node) {
    const h = (e) => {
      if (e.key === "Escape") { node.remove(); document.removeEventListener("keydown", h); }
    };
    document.addEventListener("keydown", h);
  }

  /* ---------- 动作 ---------- */

  function after(what) {
    say(what);
    notify();
  }

  /** 通知导演台刷新（素材库改了工程数据：池子/标注/引用都要跟着变）。 */
  function notify() {
    if (typeof S.onChanged === "function") { try { S.onChanged(); } catch (e) { /* 可选 */ } }
  }

  async function actRole(it, role) {
    const A = api();
    const r = await A.libRole(S.dir, it.id, role);
    if (!r.body?.ok) { fail(A.errText(r, "标注失败")); return; }
    after(`「${it.name}」→ ${role}`);
    fetchPage(false);
  }

  /** 评分弹窗（瓦片星条之外的大号入口，右键菜单用）。 */
  function actRate(it) {
    const ov = el("div", "h3l-modal");
    const box = el("div", "h3l-modalbox");
    box.append(el("h3", "", esc(`给「${it.name}」评分`)));
    box.append(el("p", "h3l-modalhint", "点星星即打分，点当前星级 = 清除评分。"));
    let row = null;
    const paint = () => {
      const r = starRow(it.rating, (v) => { setRating(it, v, paint); });
      if (row) row.replaceWith(r);
      row = r;
    };
    paint();
    box.append(row);
    const rw = el("div", "h3l-modalrow");
    const close = el("button", "h3l-btn", "完成");
    close.type = "button";
    close.onclick = () => ov.remove();
    rw.append(close);
    box.append(rw);
    ov.append(box);
    ov.addEventListener("pointerdown", (e) => { if (e.target === ov) ov.remove(); });
    document.body.append(ov);
  }

  async function actTag(it) {
    const v = await askText({
      title: `给「${it.name}」打标签`,
      hint: "逗号分隔。标签是你自己的分类（例：角色 / 场景 / 道具 / 用过），用来筛选和检索素材。",
      value: (it.tags || []).join(","),
      placeholder: "角色,场景,用过",
      okText: "保存",
    });
    if (v === null) return;
    const tags = String(v).split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    const A = api();
    const r = await A.libTag(S.dir, it.id, tags);
    if (!r.body?.ok) { fail(A.errText(r, "标签保存失败")); return; }
    after("标签已保存");
    fetchPage(false);
  }

  async function actAlias(it) {
    const v = await askText({
      title: `重命名「${it.name}」`,
      hint: "这个名字就是提示词里 @别名 用的名字（<=24 字）。\n"
        + "空格与标点会自动换成下划线（后端 @引用 按空白断句，留着会引用不到）。\n"
        + "改名后段落里已勾选的引用会跟着换成新名字。",
      value: it.name,
      maxLength: 24,
      placeholder: "新显示名",
      okText: "改名",
    });
    if (v === null) return;
    const name = String(v).trim();
    if (!name) return;
    const A = api();
    const r = await A.libAlias(S.dir, it.id, name);
    if (!r.body?.ok) { fail(A.errText(r, "重命名失败")); return; }
    /* 以后端归一后的名字为准（用户输入的原文可能带空格/扩展名）；它才是
     * `@引用` 真正要写的那个串。 */
    const fixed = String(r.body.alias || "").trim() || name;
    it.name = fixed;
    after(fixed === name ? `已重命名为「${fixed}」：提示词里写 @${fixed} 即可引用`
      : `已重命名为「${fixed}」（原名里的空格/标点已按 @引用 规则改写）：提示词里写 @${fixed} 即可引用`);
    fetchPage(false);
  }

  /** 全局库 → 项目：**复制一份**进项目 assets/ 并登记清单（唯一形态，不再有"链接"）。
   *
   * 之前有个 mode="link" 只写 asset_links 不复制文件，结果项目里多出一个
   * 「🔗 链接」条目、文件其实还躺在全局库 —— 用户看到的不是"我的项目里有这个素材"。
   * 现在统一 copy：项目自包含（删项目不连累全局库），代价只是多占一份磁盘。
   * 后端 link 分支保留只为兼容老项目里已有的链接条目（能显示、能解链）。
   */
  async function actBring(it) {
    const A = api();
    if (!A.libMirror) { fail("接口未就绪（h3_api.js 未更新）"); return; }
    const r = await A.libMirror(S.dir, it.id, it.name, "copy");
    if (!r.body?.ok) { fail(A.errText(r, "调入项目失败")); return; }
    after(`已调入项目（复制一份）：${r.body.file}\n别名「${r.body.label}」`
      + `——提示词里写 @${r.body.label} 即可引用`);
    fetchPage(false);
  }

  /** 项目资产 → 全局库（显式动作，跨项目复用；不是上传时顺手复制）。 */
  async function actArchive(it) {
    const A = api();
    if (!A.libArchive) { fail("接口未就绪（h3_api.js 未更新）"); return; }
    const r = await A.libArchive(S.dir, [it.id]);
    if (!r.body?.ok) { fail(A.errText(r, "存入全局库失败")); return; }
    const n = (r.body.archived || []).length;
    after(n ? `「${it.name}」已存入全局库（跨项目可复用）`
            : `「${it.name}」已在全局库，无需重复存入`);
    fetchPage(false);
  }

  async function actBringMany(items) {
    const A = api();
    let ok = 0;
    const names = [];
    for (const it of items) {
      const r = await A.libMirror(S.dir, it.id, it.name, "copy");
      if (r.body?.ok) { ok++; names.push(r.body.label); }
      else fail(`${it.name}：${A.errText(r, "调入失败")}`);
    }
    if (ok) {
      after(`已调入项目（各复制一份）${ok} 个：${names.join("、")}\n提示词里写 @别名 即可引用`);
      fetchPage(false);
    }
  }

  /** 批量存入全局库（项目资产 / 成片 → 全局库）。 */
  async function actArchiveMany(items) {
    const A = api();
    if (!A.libArchive) { fail("接口未就绪（h3_api.js 未更新）"); return; }
    const r = await A.libArchive(S.dir, items.map((x) => x.id));
    if (!r.body?.ok) { fail(A.errText(r, "存入全局库失败")); return; }
    const n = (r.body.archived || []).length;
    after(n ? `已存入全局库 ${n} 个（跨项目可复用）` : "选中的都已在全局库，无需重复存入");
    fetchPage(false);
  }

  async function actToAssets(it) {
    const A = api();
    const stem = String(it.file).split("/").pop().replace(/\.[^.]+$/, "") || "clip";
    const r = await A.moveMedia(S.dir, it.file, "assets",
      { register_asset: true, label: stem.slice(0, 24), kind: it.kind });
    if (!r.body?.ok) { fail(A.errText(r, "调入项目失败")); return; }
    after(`「${stem}」已从成片挪进项目资产 assets/`      // 是搬家，不是复制：说清楚
      + `\n别名「${stem.slice(0, 24)}」——提示词里写 @${stem.slice(0, 24)} 即可引用`
      + "\n（成片库里这份已经不在了）");
    fetchPage(false);
  }

  async function actCopyPath(it) {
    const A = api();
    const txt = A.libRawUrl(S.dir, it.id);
    try {
      await navigator.clipboard.writeText(it.file);
      say(`已复制：${it.file}`);
    } catch (e) {
      window.prompt("复制路径：", `${it.file}\n（预览地址 ${txt}）`);
    }
  }

  function actDownload(it, ids) {
    const A = api();
    if (ids.length > 1) { actZip(ids); return; }
    // 带 download=1：后端加 attachment 头触发另存为（否则浏览器会直接打开原文件）
    const a = document.createElement("a");
    a.href = A.libRawUrl(S.dir, it.id, true);
    a.download = it.name || "";
    document.body.append(a);
    a.click();
    a.remove();
    say(`开始下载：${it.name}`);
  }

  async function actZip(ids) {
    const A = api();
    const r = await A.libZip(S.dir, ids);
    if (!r.body?.ok) { fail(A.errText(r, "打包失败")); return; }
    window.open(A.libZipUrl(S.dir, r.body.zip), "_blank");
    say(`已打包 ${r.body.count} 个文件（${fmtSize(r.body.bytes)}）`);
  }

  /** 删除：物理文件 + **清单同步**（后端一次做完），前端负责把"删的到底是什么"说清楚。
   *
   * 三种条目后果不同，混成一句话必然让人误判：
   *   项目内文件 → 删文件 + 从本项目「资产引用」栏与提示词 @引用里一并清掉；
   *   全局库文件 → 从全局库彻底移除（别的项目链接它的引用会失效）；
   *   「链接」条目 → 只解链，全局库那份文件保留。
   */
  async function actDelete(ids) {
    const rows = S.items.filter((x) => ids.includes(x.id));
    const linkedN = rows.filter((x) => x.linked).length;
    const globalN = rows.filter((x) => x.scope === "global").length;
    const projN = rows.length - linkedN - globalN;
    const lines = [];
    if (projN > 0) {
      lines.push(`${projN} 个项目内文件：删物理文件，并从本项目「资产引用」栏`
        + "和提示词里的 @引用一并清掉");
    }
    if (globalN > 0) {
      lines.push(`${globalN} 个全局库文件：从全局库彻底移除（不可撤销）；`
        + "本项目里指向它的链接会一并解开，已被其它项目链接的那些引用会失效");
    }
    if (linkedN > 0) {
      lines.push(`${linkedN} 个「链接」条目：只解除项目链接（全局库那份文件保留）`);
    }
    const tip = (lines.length
      ? `确认删除选中的 ${rows.length} 个条目？\n\n`
        + lines.map((s) => "· " + s).join("\n") + "\n\n不可撤销。"
      : `确认删除这 ${ids.length} 个文件的物理文件？（不可撤销）`);
    if (!window.confirm(tip)) return;
    const A = api();
    const r = await A.libDelete(S.dir, ids);
    if (!r.body?.ok) { fail(A.errText(r, "删除失败")); return; }
    S.sel.clear();
    const un = (r.body.unlinked || []).length;
    const notes = r.body.notes || [];
    say(`已删除 ${(r.body.deleted || []).length} 个`
        + (un ? `，解除项目链接 ${un} 个` : "")
        + ((r.body.skipped || []).length ? `，跳过 ${r.body.skipped.length} 个` : "")
        + (notes.length ? `\n${notes.join("\n")}` : ""));
    notify();          // 项目里没了：导演台的引用栏/提示词补全要同步
    fetchPage(false);
  }

  /* ---------- 打开 ---------- */

  async function open(opts) {
    const o = opts || {};
    if (document.querySelector(".h3l-overlay")) return;
    injectStyles();
    const pickMode = typeof o.onPick === "function";
    S = {
      dir: String(o.dir || ""), seg: Number(o.seg) || 1,
      onChanged: o.onChanged,
      scope: "project", kind: "all", sort: "mtime", order: "desc", minRating: "0",
      q: "", page: 1, pageSize: 60,
      items: [], total: 0, totalPages: 1, counters: {},
      sel: new Set(), multi: false, collections: [], collection: "",
      pick: pickMode ? o.onPick : null,
      pickKinds: Array.isArray(o.pickKinds) ? o.pickKinds : null,
      cur: null,
    };

    const overlay = el("div", "h3l-overlay" + (pickMode ? " h3l-picking" : ""));
    const box = el("div", "h3l-box");
    const head = el("div", "h3l-head");
    head.append(el("strong", "", pickMode ? "🗂 选择素材" : "🗂 素材库"));
    if (pickMode) {
      head.append(el("span", "h3l-hint", "点一下素材就选中，窗口自动关闭"));
    }
    S.scopeBox = el("div", "h3l-scopes");
    head.append(S.scopeBox, el("div", "h3l-spacer"));
    const close = el("button", "h3l-close", "✕");
    close.title = "关闭（Esc）";
    close.onclick = () => overlay.remove();
    head.append(close);
    S.close = () => overlay.remove();

    const bar = el("div", "h3l-bar");
    const search = el("input", "h3l-search");
    search.placeholder = "搜索名称 / 文件名 / 标签 / 来源…";
    let tmr = null;
    search.oninput = () => {
      clearTimeout(tmr);
      tmr = setTimeout(() => { S.q = search.value.trim(); S.sel.clear(); fetchPage(false); }, 260);
    };
    bar.append(search);

    const mkSel = (pairs, key) => {
      const sel = el("select");
      for (const [v, zh] of pairs) sel.append(new Option(zh, v));
      sel.value = S[key];
      sel.onchange = () => { S[key] = sel.value; S.sel.clear(); fetchPage(false); };
      return sel;
    };
    // 挑选模式下按 pickKinds 收窄类型下拉：锚源只收图片/视频/latent，
    // 留着「音频」让用户点了再被后端拒，属于把错误推给下一个环节。
    bar.append(mkSel(S.pickKinds
      ? KINDS.filter(([v]) => v === "all" || v === "media" || S.pickKinds.includes(v))
      : KINDS, "kind"));
    bar.append(mkSel(SORTS, "sort"));
    bar.append(mkSel(RATINGS, "minRating"));
    const ordBtn = el("button", "h3l-btn", "↓ 倒序");
    ordBtn.type = "button";
    ordBtn.onclick = () => {
      S.order = S.order === "desc" ? "asc" : "desc";
      ordBtn.textContent = S.order === "desc" ? "↓ 倒序" : "↑ 正序";
      fetchPage(false);
    };
    bar.append(ordBtn);

    const multiBtn = el("button", "h3l-btn", "☑ 多选");
    multiBtn.type = "button";
    multiBtn.onclick = () => {
      S.multi = !S.multi;
      multiBtn.classList.toggle("on", S.multi);
      if (!S.multi) S.sel.clear();
      renderGrid();
      renderFoot();
    };
    // 挑选模式单击就返回，不存在"选中一批"这回事
    if (!pickMode) bar.append(multiBtn);

    /* 上传落点 = 当前所在库，**上传到哪里就是哪里，不顺手复制**：
     *   全局库 → 只进全局库（跨项目复用）
     *   项目资产 → 只落本项目 assets/（登记清单，可直接 @别名 引用）
     *   成片     → 只落本项目 finals/（目录扫描即见）
     *   latent   → 按项目资产处理
     * 想要"全局库也存一份"用项目瓦片上的「存入全局库」（显式动作）。 */
    const upBtn = el("button", "h3l-btn h3l-btn-cta", "＋ 上传");
    upBtn.type = "button";
    const DEST_CN = { global: "全局库", project: "项目资产", finals: "成片库" };
    const destOf = (scope) => (scope === "global" ? "global"
      : (scope === "finals" ? "finals" : "project"));
    const paintUpBtn = () => {
      const dest = destOf(S.scope);
      upBtn.textContent = `＋ 上传到${DEST_CN[dest] || "项目资产"}`;
      upBtn.title = dest === "global"
        ? "当前在「全局库」：只进全局库（跨项目复用，不碰任何项目）；"
          + "要落进项目请切到「项目资产」再传。"
        : dest === "finals"
          ? "当前在「成片」：只落本项目 finals/（成片库，目录扫描即见）。"
          : "当前在「项目资产」：只落本项目 assets/ 并登记进清单，"
            + "提示词里写 @别名 即可引用（不会往全局库复制一份）。";
    };
    paintUpBtn();
    upBtn.onclick = () => {
      const inp = document.createElement("input");
      inp.type = "file";
      inp.multiple = true;
      inp.onchange = async () => {
        const files = [...(inp.files || [])];
        if (!files.length) return;
        let dest = destOf(S.scope);
        if (dest !== "global" && !S.dir) {
          say("没有打开的项目：这次先只进全局库（切到项目库再传可落到项目里）");
          dest = "global";
        }
        const onlyGlobal = dest === "global";
        let ok = 0;
        for (const f of files) {
          try {
            const H3Assets = window.H3Assets;
            if (!H3Assets?.uploadDirect) throw new Error("上传接口不可用");
            const kind = H3Assets.guessKind(f);
            /* 别名不在这里拼：`@别名` 的规范化规则只有一份（后端 asset_store.clean_alias），
             * 前端再拼一份必然漂移 —— 旧代码只去扩展名、把空格与括号留在了别名里。 */
            const opt = onlyGlobal
              ? { kind, dest: "global" }
              : { kind, dest, link_dir: S.dir, mirror: "1" };
            const res = await H3Assets.uploadDirect(f, opt);
            if (!res?.ok) throw new Error("上传返回异常");
            if (res.store_error) fail(`${f.name}：${res.store_error}`);
            ok++;
          } catch (e) { fail(`「${f.name}」上传失败：${e?.message || e}`); }
        }
        if (!ok) return;
        if (onlyGlobal) {
          say(`已上传 ${ok} 个到全局库（跨项目可复用）\n要落进当前项目，点瓦片上的「调入项目」`);
        } else {
          say(`已上传 ${ok} 个到${DEST_CN[dest]}（只这一份，不往别处复制）`
            + (dest === "project" ? "\n提示词里写 @别名 即可引用" : ""));
          if (typeof S.onChanged === "function") {
            try { S.onChanged(); } catch (e) { /* 通知导演台刷新（可选） */ }
          }
        }
        fetchPage(false);
      };
      inp.click();
    };
    bar.append(upBtn);
    S.paintUpBtn = paintUpBtn;

    const scanBtn = el("button", "h3l-btn", "↻ 重新扫描");
    scanBtn.type = "button";
    scanBtn.onclick = async () => {
      const A = api();
      const r = await A.libScan(S.dir);
      if (!r.body?.ok) { fail(A.errText(r, "扫描失败")); return; }
      say("已重新扫描");
      fetchPage(false);
    };
    bar.append(scanBtn);

    const grid = el("div", "h3l-grid");
    const foot = el("div", "h3l-foot");
    S.grid = grid; S.foot = foot;
    foot.append(el("span", "h3l-msg", ""));
    S.msg = foot.querySelector(".h3l-msg");
    S.footInfo = el("span", "", "");
    foot.append(S.footInfo);
    S.moreBtn = el("button", "h3l-btn", "加载更多");
    S.moreBtn.type = "button";
    S.moreBtn.onclick = () => { S.page += 1; fetchPage(true); };
    foot.append(S.moreBtn);
    const close2 = el("button", "h3l-btn", "关闭");
    close2.type = "button";
    close2.onclick = () => overlay.remove();
    foot.append(close2);

    box.append(head, bar, grid, foot);
    overlay.append(box);
    overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) overlay.remove(); });
    overlayKeydownOnce(overlay);
    document.body.append(overlay);

    // 滚到底自动加载下一页（分页契约不变，只是省一次点击）
    grid.addEventListener("scroll", () => {
      if (S.page >= S.totalPages) return;
      if (grid.scrollTop + grid.clientHeight >= grid.scrollHeight - 120) {
        S.page += 1; fetchPage(true);
      }
    });

    if (pickMode) say("点一下素材即可选中（会自动关闭本窗口）");
    await refreshCollections();
    await fetchPage(false);
    const A = api();
    try {
      const st = await A.libStatus({ dir: S.dir });
      if (st.body?.ok) {
        S.counters = st.body.counters || {};
        renderScopes();
      }
    } catch (e) { /* 状态拿不到不影响浏览 */ }
  }

  window.H3Lib = { open };
})();
