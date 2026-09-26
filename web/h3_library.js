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
    /* 合并顺序角标：数字就是拼接位次。放左下角（左上/右上已被评分与角色角标占住），
     * 必须登记进 tileKeepers() —— 缩略图懒加载会 replaceChildren，漏了它滚动一次就没了。 */
    .h3l-order{position:absolute;left:5px;bottom:5px;min-width:21px;height:21px;padding:0 5px;border-radius:11px;background:#316dca;color:#fff;font-size:12px;font-weight:700;line-height:21px;text-align:center;box-shadow:0 2px 8px #000a;pointer-events:none}
    /* 合并模式的显隐切换：**靠类切**，不重建 DOM（重建会丢滚动位置与已选清单）。
     * 两条 .h3l-merging 规则的选择器权重更高，压得住 .h3l-batch / .h3l-btn 自带的
     * display；.h3l-only-merge 的 display:none 写在 .h3l-batch 之后，同权重时后者生效。 */
    .h3l-only-merge{display:none}
    .h3l-merging .h3l-only-normal{display:none}
    .h3l-merging .h3l-only-merge{display:flex}
    .h3l-merging .h3l-hint{color:#9ecbff}
    /* 合并模式下的非视频瓦片：看得见但点不动（点了给一句说明） */
    .h3l-nomerge{opacity:.42;cursor:not-allowed}
    .h3l-nomerge:hover{border-color:#37332b;background:#181712}
.h3l-name{font-weight:600;font-size:12.5px;word-break:break-all;color:#f0ece2}
.h3l-meta{font-size:10.5px;color:#8a857b;word-break:break-all;display:flex;gap:5px;flex-wrap:wrap}
.h3l-badges{display:flex;gap:3px;flex-wrap:wrap}
.h3l-tbtns{display:flex;gap:4px;flex-wrap:wrap;margin-top:2px}
.h3l-tbtn{padding:2px 7px;border:1px solid #3a352c;border-radius:7px;background:#211f1a;color:#a8a294;cursor:pointer;font-size:10.5px;font-family:inherit}
.h3l-tbtn:hover{border-color:#46604f;color:#d9d4c9}
.h3l-tbtn.on{border-color:#2f6e57;background:#12291f;color:#7fe0b0}
.h3l-tbtn.danger:hover{border-color:#9a4144;color:#f0a0a4}
    /* 工具条批量操作区：与筛选控件同排但**常显**（不选也看得见），
     * 分隔符把它和前面的搜索/筛选隔开，避免误读成"筛选条件"。 */
    .h3l-batch{display:flex;gap:6px;align-items:center;flex-wrap:wrap;
        margin-left:2px;padding-left:10px;border-left:1px solid #3a352c}
    .h3l-btn-danger:hover{border-color:#9a4144;color:#f0a0a4}
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
      /* 合并只收视频：latent 库在合并模式下**必然是空的**（类型被强制成 video），
       * 摆一个点进去只有"没有匹配的素材"的页签，就是让人白点一次。 */
      if (S.merge && k === "latent") continue;
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

  /** 瓦片上的常驻动作：**只留「改名」**。
   *
   *  「调入项目 / 存入全局库 / 删除」原本都在这里，结果每张瓦片被按钮糊满、
   *  缩略图只剩一条缝，而它们真正的使用场景是"对一批素材做同一件事"——
   *  一件一件点反而更慢。这三个已迁到工具条的批量操作区（常显，见 open 里的
   *  batchBox），选中谁就作用于谁；右键菜单里也还留着同样的入口。
   *  改名是唯一"只跟这一张有关"的动作，留在瓦片上。 */
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
    if (it.scope === "project" || it.scope === "global") {
      mk("改名", false, () => actAlias(it));
    }
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
    /* 合并模式只收视频：非视频瓦片**标出来并挡住**。列表已被强制过滤成 video，
     * 这里再挡一层是防"筛选器被改回 all"之类的漏网 —— 一旦有图片进了清单，
     * 后端会以"只能合并视频：X 是图片"把**整单**拒掉，用户看到的是一次
     * 莫名其妙的整体失败，而不是"这张图不该选"。 */
    const noMerge = S.merge && it.kind !== "video";
    const t = el("div", "h3l-tile" + (S.sel.has(it.id) ? " sel" : "")
      + (noMerge ? " h3l-nomerge" : ""));
    t.dataset.id = it.id;
    const th = el("div", "h3l-thumb");
    th.append(el("span", "h3l-ico", KIND_ICON[it.kind] || "📄"));
    th.dataset.src = api().libRawUrl(S.dir, it.id);
    th.dataset.kind = it.kind;
    th.dataset.thumb = api().libThumbUrl(S.dir, it.id);
    if ((it.roles || []).length) {
      th.append(el("span", "h3l-role", esc(it.roles.join("·"))));
    }
    /* 合并模式：已在清单里的瓦片，角标立即出现（关掉窗口再打开也认得）。
     * 这个角标必须登记进 tileKeepers()，否则缩略图懒加载 replaceChildren 会把它抹掉。 */
    if (S.merge) {
      const n = S.mergeOrder.findIndex((x) => x.id === it.id);
      if (n >= 0) th.append(el("span", "h3l-order", String(n + 1)));
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

    /* 双击预览：挑选模式不能开（单击就选中并关窗了），**合并模式要开** ——
     * 挑要拼接的片子时更需要先看一眼内容。双击会连带触发两次单击，
     * 而合并清单里"push 一次再 splice 一次"正好抵消，顺序不受影响。 */
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
      /* 合并模式：点击 = 排进/移出合并清单。**顺序就是 S.mergeOrder 的数组顺序**，
       * 所以移除一个之后后面的角标要整体重排（paintMergeBadges 全片重画）。 */
      if (S.merge) {
        if (noMerge) {
          say(`只能合并视频：「${it.name}」是${KIND_CN[it.kind] || it.kind}。`
            + "退出合并模式后可以正常浏览、预览、调入项目");
          return;
        }
        const i = S.mergeOrder.findIndex((x) => x.id === it.id);
        if (i >= 0) S.mergeOrder.splice(i, 1);
        else S.mergeOrder.push({
          id: it.id, file: it.file, name: it.name, scope: it.scope, kind: it.kind,
        });
        t.classList.toggle("sel", i < 0);
        paintMergeBadges();
        renderFoot();
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

  /* 瓦片上除了缩略图/视频之外要留住的东西：类型图标 + 角色角标 + 星级 + 合并顺序角标。
   * replaceChildren 会一并抹掉，每次「加载 / 卸载」都要把它们带回来。
   * （合并角标漏登记的话，滚动触发懒加载后角标会整批消失 —— 本仓库踩过这个坑。） */
  function tileKeepers(d) {
    return [...d.children].filter((n) => n.classList && (
      n.classList.contains("h3l-ico") || n.classList.contains("h3l-role")
      || n.classList.contains("h3l-star") || n.classList.contains("h3l-order")));
  }

  /** 合并顺序角标：数字 = 在清单里的位次。顺序一变就**整片重画** ——
   *  删掉第 2 个，后面的 3/4 都要往前挪成 2/3，逐个改反而更容易漏。 */
  function paintMergeBadges() {
    if (!S.grid || !S.merge) return;
    const pos = new Map((S.mergeOrder || []).map((x, i) => [x.id, i + 1]));
    for (const t of S.grid.querySelectorAll(".h3l-tile")) {
      const n = pos.get(t.dataset.id);
      let b = t.querySelector(".h3l-order");
      if (!n) { if (b) b.remove(); continue; }
      if (!b) {
        b = el("span", "h3l-order", "");
        const th = t.querySelector(".h3l-thumb");
        (th || t).append(b);
      }
      b.textContent = String(n);
    }
  }

  /** 切「选材合并」模式：**不重建 DOM**，只切类 + 改标题 + 重拉列表。
   *
   * 进入时把类型筛选**强制成 video**（合并只收视频）。为什么不是"让用户自己挑
   * 类型"：图片/音频/latent 进了清单后端会整单拒掉（"只能合并视频：X 是图片"），
   * 与其让人选完再失败，不如根本不给选的机会。退出时还原成 all。
   *
   * 清单在退出时**立即清空**（Q1 口径）：它是纯内存的临时选择，留着只会在下次
   * 进合并模式时"莫名其妙多出几个角标"。 */
  function setMergeMode(on) {
    if (!S || S.pick) return;                 // 挑选模式（锚源）没有合并这回事
    const next = !!on;
    if (next === S.merge) return;
    S.merge = next;
    S.mergeOrder = [];
    S.kind = next ? "video" : "all";
    if (S.overlay) S.overlay.classList.toggle("h3l-merging", next);
    if (S.headTitle) S.headTitle.textContent = next ? "🗂 选择要合并的素材" : "🗂 素材库";
    if (S.headHint) {
      S.headHint.textContent = next
        ? "点素材排进合并清单：瓦片角标 1 / 2 / 3 / 4 就是拼接顺序"
          + "（再点一次取消；双击可预览）"
        : "";
    }
    if (S.kindSel) S.kindSel.value = S.kind;
    /* 退出时把瓦片上的勾选/角标一起抹掉：`sel` 是多选批量用的，跟合并清单不是
     * 一回事，混在一起会让人以为"这些也进了合并"。 */
    if (S.grid) {
      S.grid.querySelectorAll(".h3l-tile.sel").forEach((n) => n.classList.remove("sel"));
    }
    renderScopes();
    paintMergeBadges();
    renderFoot();
    fetchPage(false);                          // 类型筛选变了，列表必须重拉
    if (next) say("选材模式：点素材排进合并清单（角标 1→N 就是拼接顺序）");
  }

  /** 就地发起合并（POST /h3chain/merge）。
   *
   * 顺序 = `S.mergeOrder` 的数组顺序，**绝不 sort** —— 顺序就是数据本身。
   * 请求里同时给 `asset`（素材 id，后端据此精确解析，全局库素材只有这条能认）
   * 与 `file`（回落路径）。失败一定要把**后端原话**显示出来：`_err` 的字段名是
   * `message`，以前这里读的是 `error`，于是所有失败都只剩一句"HTTP 400"，
   * 真正的原因（素材没了 / 不是视频 / 路径越界）全被吞掉。 */
  async function doMerge() {
    if (S.mergeBusy) return;
    if (!S.dir) { say("合并需要先打开一个项目：产物落在该项目的 finals/ 里"); return; }
    if (!S.mergeOrder.length) {
      say("先点素材排进合并清单：瓦片角标 1 / 2 / 3 就是拼接顺序（再点一次取消）");
      return;
    }
    const items = S.mergeOrder.map((x) => ({
      asset: String(x.id || ""), file: String(x.file || ""), name: String(x.name || ""),
    }));
    const A = api();
    S.mergeBusy = true;
    if (typeof S.paintMergeBtn === "function") S.paintMergeBtn();
    /* 告诉导演台"合并跑起来了"：PyAV 流式编码是分钟级 CPU 密集任务，这期间
     * 再提交生成就是两个重活抢 CPU。导演台没加载（老页面缓存）时静默跳过。 */
    try { window.H3Merge?.begin?.(); } catch (e) { /* 没有导演台也照常合并 */ }
    say(`合并 ${items.length} 项拼接中…（分钟级，期间可继续浏览，但先别提交生成）`);
    let ok = false;
    try {
      const r = await A.libMerge(S.dir, items);
      if (!r.body?.ok) {
        fail(A.errText(r, "合并失败"));
      } else {
        ok = true;
        const file = String(r.body.file || "");
        S.mergeOrder = [];
        /* 先切 scope 再退模式：产物落在 `<proj>/finals/`，正是「成片」这个 scope，
         * 而 setMergeMode(false) 里那次 fetchPage 就会落在成片上（省一次全量索引查询）。 */
        S.scope = "finals";
        setMergeMode(false);
        say(`已合并 → ${file || "merged_*.mp4"}（已在「成片」里，可直接播放/预览）`);
      }
    } catch (e) {
      fail(`合并请求失败：${e?.message || e}`);
    } finally {
      S.mergeBusy = false;
      if (typeof S.paintMergeBtn === "function") S.paintMergeBtn();
      /* end(ok)：ok 为真时导演台会 refresh —— 产物出现在成片区，它那边要跟着更新；
       * 失败也要 end，否则互斥标记会永远挂着，生成按钮再也点不动。 */
      try { window.H3Merge?.end?.(ok); } catch (e) { /* 同上 */ }
      /* 没有导演台时（独立打开素材库 / 单测）才走通用回调，免得两边都刷一次。 */
      if (!window.H3Merge) notify();
    }
  }

  function renderGrid() {
    if (io) io.disconnect();
    S.grid.replaceChildren();
    if (!S.items.length) {
      S.grid.append(el("div", "h3l-empty",
        S.q ? "没有匹配的素材" : "这里还没有素材：把文件拖进来，或用「上传」按钮"));
      return;
    }
    /* 懒加载 **+ 离屏卸载**：进视野才发请求，出视野就把 img/video 摘掉。
     *
     * 为什么必须卸载：浏览器会把解码后的位图常驻（256px 缩略图约 260KB，
     * 视频 <video> 还要拖着一段已缓冲的原文件）。而「加载更多」是 append 模式
     * —— S.items 只增不减，翻十页就是 600 张常驻，浏览器内存一路涨不下��，
     * 这正是「素材一多就卡」的前端那一半。
     *
     * 卸载只摘 img/video，瓦片 DOM、data-* 、勾选状态全保留，滚回去会重新加载。 */
    io = new IntersectionObserver((entries) => {
      for (const en of entries) {
        const d = en.target;
        const keep = tileKeepers(d);
        if (!en.isIntersecting) {
          if (d.querySelector("img,video")) d.replaceChildren(...keep);
          continue;
        }
        // 已加载就别重复发请求（滚动抖动会连续触发）；失败过的也不再重试
        if (d.querySelector("img,video") || d.dataset.thumbFail === "1") continue;
        const kind = d.dataset.kind;
        if (kind === "image") {
          const im = document.createElement("img");
          im.loading = "lazy";
          im.src = d.dataset.thumb;
          im.onerror = () => { im.remove(); d.dataset.thumbFail = "1"; };
          d.replaceChildren(im, ...keep);
        } else if (kind === "video") {
          const v = document.createElement("video");
          v.muted = true; v.preload = "metadata"; v.src = d.dataset.src;
          v.onerror = () => { v.remove(); d.dataset.thumbFail = "1"; };
          d.replaceChildren(v, ...keep);
        }
      }
    }, { root: S.grid, rootMargin: "240px" });
    const frag = document.createDocumentFragment();
    for (const it of S.items) frag.append(tileFor(it));
    S.grid.append(frag);
  }

  function renderFoot() {
    const mn = (S.mergeOrder || []).length;
    S.footInfo.textContent =
      `第 ${S.page}/${S.totalPages} 页 · 共 ${S.total} 项` +
      (S.merge
        ? (mn ? ` · 已排 ${mn} 个（合并顺序 1→${mn}）` : " · 合并清单为空：点视频开始排")
        : (S.sel.size ? ` · 已选 ${S.sel.size}` : ""));
    S.moreBtn.style.display = S.page < S.totalPages ? "" : "none";
    /* 「☑ 全选 / ☐ 全不选」跟着当前页的实际勾选状态走：翻页/换筛选后列表换了，
     * 按钮上写的还是上一页的状态就会点反。 */
    if (typeof S.paintSelAll === "function") S.paintSelAll();
    /* 合并按钮同理：清单数量变了，文案里的「按 1→N 顺序」也得跟着变。 */
    if (typeof S.paintMergeBtn === "function") S.paintMergeBtn();
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
      /* multi 恒为 true：素材库**默认就是多选**，点瓦片即勾选/取消勾选。
       * 原来要先点「☑ 多选」才能多选，那个按钮已经删了——它唯一的作用是
       * 让人以为"只能选一个"，而批量操作（调入项目/存入全局库/删除）本来就
       * 靠多选才成立。挑选模式（onPick）单击即返回，不受这项影响。 */
      sel: new Set(), multi: true, collections: [], collection: "",
      pick: pickMode ? o.onPick : null,
      pickKinds: Array.isArray(o.pickKinds) ? o.pickKinds : null,
      cur: null,
      /* 合并导出**整个住在素材库里**（入口按钮、选材、顺序、发起拼接都在这儿）。
       *
       * 为什么不做成"导演台开合并模式、素材库当选择器"：那样同一个功能有
       * 两处入口、两份清单状态，必然漂移成"我在这儿选好了，那边却显示没选"。
       * 导演台只剩一个互斥标记（`window.H3Merge`），在拼接期间挡住生成按钮。
       *
       * mergeOrder 是**数组**（顺序即数据，别用 Set）：点一个 push 一个，
       * 再点一次 splice 掉，瓦片角标 = 下标 + 1。 */
      merge: false,
      mergeOrder: [],
      mergeBusy: false,
    };

    const overlay = el("div", "h3l-overlay" + (pickMode ? " h3l-picking" : ""));
    const box = el("div", "h3l-box");
    const head = el("div", "h3l-head");
    /* 标题与提示在切合并模式时改写（setMergeMode），所以留住引用。 */
    S.headTitle = el("strong", "", pickMode ? "🗂 选择素材" : "🗂 素材库");
    head.append(S.headTitle);
    S.headHint = el("span", "h3l-hint", pickMode ? "点一下素材就选中，窗口自动关闭" : "");
    head.append(S.headHint);
    S.overlay = overlay;
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
    const kindSel = mkSel(S.pickKinds
      ? KINDS.filter(([v]) => v === "all" || v === "media" || S.pickKinds.includes(v))
      : KINDS, "kind");
    /* 合并模式只收视频，类型筛选会被**强制成 video**，所以这个下拉在合并模式下
     * 藏起来（`h3l-only-normal`）—— 留着会让人以为"还能挑图片"，而图片一旦进清单
     * 后端会整单拒掉，用户看到的是一次莫名其妙的失败。 */
    kindSel.classList.add("h3l-only-normal");
    S.kindSel = kindSel;
    bar.append(kindSel);
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

    /* ---- 批量操作区（常显） ----
     * 「全选 / 调入项目 / 存入全局库 / 删除」原本分散在每张瓦片上：瓦片被按钮
     * 糊满（缩略图只剩一条），而"对一批素材做同一件事"反而要一张张点。
     * 现在统一收到工具条，**不选也一直看得见**——选中态只是决定它作用于谁，
     * 不是决定这个按钮存不存在（藏起来只会让人以为功能没了）。 */
    const batchBox = el("div", "h3l-batch h3l-only-normal");
    const selAllBtn = el("button", "h3l-btn", "☑ 全选");
    selAllBtn.type = "button";
    selAllBtn.title = "选中当前页全部素材；再点一次取消全选（只作用于当前页，不跨页）";
    const paintSelAll = () => {
      const allOn = S.items.length > 0 && S.items.every((x) => S.sel.has(x.id));
      selAllBtn.textContent = allOn ? "☐ 全不选" : "☑ 全选";
      selAllBtn.classList.toggle("on", allOn);
    };
    selAllBtn.onclick = () => {
      const allOn = S.items.length > 0 && S.items.every((x) => S.sel.has(x.id));
      if (allOn) S.sel.clear();
      else for (const x of S.items) S.sel.add(x.id);
      paintSelAll();
      renderGrid();
      renderFoot();
    };
    batchBox.append(selAllBtn);

    /** 批量动作的公共前置：没选任何东西时**说清楚**，而不是把按钮灰掉。
     *  按钮常显（用户要求），所以"为什么点了没反应"必须靠这句话回答。 */
    const needSel = () => {
      if (S.sel.size) return true;
      say("先点素材选中（可多选，或用「☑ 全选」选当前页），再执行这个操作");
      return false;
    };
    const mkBatch = (label, title, fn, danger) => {
      const b = el("button", "h3l-btn" + (danger ? " h3l-btn-danger" : ""), label);
      b.type = "button";
      b.title = title;
      b.onclick = () => { if (needSel()) fn(); };
      batchBox.append(b);
      return b;
    };
    mkBatch("⇩ 调入项目",
      "把选中的全局库/成片素材复制进本项目 assets/（源库那份还在）。"
      + "目标库已有同名的会被自动跳过",
      () => {
        const gl = S.items.filter((x) => S.sel.has(x.id) && x.scope === "global"
          && !isBlocked(x, "project"));
        const fi = S.items.filter((x) => S.sel.has(x.id) && x.scope === "finals"
          && !isBlocked(x, "project"));
        const all = [...gl, ...fi];
        if (!all.length) { say("选中的素材都不在全局库/成片里（项目资产本来就已在项目里）"); return; }
        for (const x of gl) actBring(x);
        for (const x of fi) actToAssets(x);
      });
    mkBatch("⬆ 存入全局库",
      "把选中的项目/成片素材存进全局库（跨项目可复用）。"
      + "latent 不能入库，会自动跳过",
      () => {
        const up = S.items.filter((x) => S.sel.has(x.id)
          && (x.scope === "project" || x.scope === "finals") && !x.linked
          && !isBlocked(x, "global"));
        if (!up.length) { say("选中的素材没有可存入全局库的（已是全局素材 / latent / 目标库已有同名）"); return; }
        actArchiveMany(up);
      });
    mkBatch("🗑 删除", "删除选中的素材（不可撤销）",
      () => actDelete([...S.sel]), true);
    S.paintSelAll = paintSelAll;

    /* ---- 合并导出：入口 + 清单区（都住在素材库里） ----
     *
     * 合并**整套**在这里：入口按钮 → 选材模式 → 点素材排顺序 → 就地发起拼接。
     * 导演台不再有合并入口、也不再存清单（只留一个互斥标记 `window.H3Merge`）。
     *
     * 两个区靠 CSS 类切换（`.h3l-only-normal` / `.h3l-only-merge` 配
     * `.h3l-merging`），**不重建 DOM** —— 重建会把滚动位置和已选清单一起丢掉。 */
    const mergeEntry = el("button", "h3l-btn h3l-btn-cta h3l-only-normal", "⧉ 合并导出");
    mergeEntry.type = "button";
    mergeEntry.title = "把多个视频按顺序拼成一条（不改链、不动存档）："
      + "进入选材模式后点素材排顺序，瓦片角标 1→N 就是拼接顺序；"
      + "只收视频；产物落在本项目 finals/，完成后自动跳到「成片」";
    mergeEntry.onclick = () => {
      if (!S.dir) { say("合并需要先打开一个项目：产物落在该项目的 finals/ 里"); return; }
      setMergeMode(true);
    };

    const mergeBox = el("div", "h3l-batch h3l-mergebox h3l-only-merge");
    const mergeBtn = el("button", "h3l-btn h3l-btn-cta", "⧉ 开始合并");
    mergeBtn.type = "button";
    const paintMergeBtn = () => {
      const n = (S.mergeOrder || []).length;
      if (S.mergeBusy) {
        mergeBtn.textContent = "⧉ 合并中…";
        mergeBtn.disabled = true;
        mergeBtn.classList.add("on");
        return;
      }
      mergeBtn.disabled = false;
      mergeBtn.textContent = n ? `⧉ 开始合并（按 1→${n} 顺序）` : "⧉ 开始合并";
      mergeBtn.title = n
        ? `按瓦片角标顺序拼接这 ${n} 个视频为 merged_*.mp4（PyAV，分钟级，期间可继续浏览）`
        : "先点素材排进清单：瓦片角标 1 / 2 / 3 就是拼接顺序（再点一次取消）";
      mergeBtn.classList.toggle("on", n > 0);
    };
    mergeBtn.onclick = () => doMerge();
    const exitMergeBtn = el("button", "h3l-btn", "✕ 退出合并");
    exitMergeBtn.type = "button";
    exitMergeBtn.title = "退出选材模式并清空清单（素材本身不受影响）";
    exitMergeBtn.onclick = () => setMergeMode(false);
    mergeBox.append(mergeBtn, exitMergeBtn);
    S.paintMergeBtn = paintMergeBtn;
    if (typeof S.paintMergeBtn === "function") S.paintMergeBtn();

    // 挑选模式单击就返回，不存在"选中一批"这回事，也没有合并这回事
    if (!pickMode) bar.append(batchBox, mergeEntry, mergeBox);

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
              : { kind, dest, link_dir: S.dir };
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
          after(`已上传 ${ok} 个到${DEST_CN[dest]}（只这一份，不往别处复制）`
            + (dest === "project" ? "\n提示词里写 @别名 即可引用" : ""));
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
