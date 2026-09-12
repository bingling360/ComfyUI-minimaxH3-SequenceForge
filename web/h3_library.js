/* H3 素材库浏览器（Library）—— 一个浏览器 + 四个 scope。
 *
 * 蓝本：Majoor Assets Manager。核心照抄的一条是**交互模型**：
 *   瓦片只放「缩略图 + 名称 + 徽标」，所有动作收进【右键菜单 / 预览器】。
 *   （之前把 7~8 个动作做成瓦片按钮，窄栏里必然换行溢出 —— 这才是"展开看不到"的病根。）
 *
 * 四个 scope：项目资产 / 全局库 / 成片 / latent，外加「收藏集」过滤。
 * 后端 library.py 只做索引 + 查询；本文件只做 UI 与动作分发。
 *
 * 用法：window.H3Lib.open({ dir, seg, onChanged })
 *   dir       项目目录名（决定 project/finals/latent 三个 scope 的数据源）
 *   seg       当前选中段（1-based），供「引用到段」用
 *   onChanged 动作改了工程数据后回调（让导演台刷新）
 */
(function () {
  "use strict";

  const SCOPES = [
    ["all", "全部"],
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

  function say(t) {
    if (!S || !S.msg) return;
    S.msg.textContent = String(t || "");
  }

  function api() {
    const A = window.H3Api;
    if (!A || !A.libList) throw new Error("素材库接口未就绪（h3_api.js 未加载）");
    return A;
  }

  /* ---------- 样式（自带一套，不依赖导演台） ---------- */

  function injectStyles() {
    if (styled) return;
    styled = true;
    const css = `
.h3l-overlay{position:fixed;inset:0;z-index:9000;background:#0b0b09e6;display:flex;align-items:center;justify-content:center;padding:18px}
.h3l-box{width:min(1660px,100%);height:100%;display:flex;flex-direction:column;background:#141310;border:1px solid #37332b;border-radius:14px;overflow:hidden;color:#d9d4c9;font:13px/1.5 "Microsoft YaHei UI","Segoe UI",sans-serif}
.h3l-head{display:flex;gap:10px;align-items:center;padding:11px 14px;border-bottom:1px solid #37332b;background:#1b1a16;flex-wrap:wrap}
.h3l-head strong{font-size:15px;color:#f0ece2}
.h3l-scopes{display:flex;gap:6px;flex-wrap:wrap}
.h3l-scope{padding:5px 13px;border:1px solid #3a352c;border-radius:14px;background:#211f1a;color:#a8a294;cursor:pointer;font-size:12.5px;font-family:inherit}
.h3l-scope:hover{border-color:#46604f;color:#d9d4c9}
.h3l-scope.on{border-color:#316dca;background:#1f2f45;color:#9ecbff}
.h3l-scope em{font-style:normal;color:#7f7a70;margin-left:5px;font-size:11px}
.h3l-spacer{flex:1}
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
.h3l-chip{padding:1px 7px;border:1px solid #2f6e57;border-radius:9px;background:#12291f;color:#7fe0b0;font-size:10px}
.h3l-tag{padding:1px 7px;border:1px solid #3a352c;border-radius:9px;color:#a8a294;font-size:10px}
.h3l-foot{display:flex;gap:10px;align-items:center;padding:9px 14px;border-top:1px solid #37332b;background:#1b1a16;font-size:12px;color:#8a857b;flex-wrap:wrap}
.h3l-empty{grid-column:1/-1;padding:44px 12px;text-align:center;color:#7f7a70}
.h3l-msg{flex:1;color:#a8a294;font-size:12px;min-width:120px}
.h3l-menu{position:fixed;z-index:9500;min-width:210px;max-height:70vh;overflow:auto;background:#1e1c18;border:1px solid #46604f;border-radius:9px;padding:5px;box-shadow:0 10px 34px #000c}
.h3l-menu button{display:block;width:100%;text-align:left;padding:7px 10px;border:0;border-radius:6px;background:transparent;color:#d9d4c9;cursor:pointer;font-size:12.5px;font-family:inherit}
.h3l-menu button:hover{background:#24402f;color:#7fe0b0}
.h3l-menu button.danger:hover{background:#3a1a1c;color:#f0a0a4}
.h3l-menu .h3l-sep{height:1px;margin:5px 6px;background:#37332b}
.h3l-menu .h3l-cap{padding:6px 10px 3px;color:#7f7a70;font-size:10.5px}
.h3l-viewer{position:fixed;inset:0;z-index:9400;background:#000000f2;display:flex;gap:0}
.h3l-vstage{flex:1;display:flex;align-items:center;justify-content:center;padding:22px;min-width:0}
.h3l-vstage img,.h3l-vstage video{max-width:100%;max-height:100%;border-radius:8px}
.h3l-vstage audio{width:min(560px,90%)}
.h3l-vside{width:320px;flex:none;background:#16150f;border-left:1px solid #37332b;padding:16px;overflow:auto;display:flex;flex-direction:column;gap:9px}
.h3l-vside h3{margin:0 0 4px;font-size:15px;color:#f0ece2;word-break:break-all}
.h3l-vrow{font-size:12px;color:#a8a294;word-break:break-all}
.h3l-vrow b{color:#d9d4c9;font-weight:600}
.h3l-vact{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.h3l-stars{display:flex;gap:2px}
.h3l-stars span{cursor:pointer;font-size:17px;color:#5c574d}
.h3l-stars span.on{color:#e9c07a}
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
      collection: S.collection || "",
    });
    if (!res.body?.ok) { say(A.errText(res, "读取素材失败")); return; }
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
      const n = k === "all" ? (S.totalAll || "") : (S.counters[k] || 0);
      const b = el("button", "h3l-scope" + (S.scope === k ? " on" : ""),
        esc(zh) + (k === "all" ? "" : `<em>${n}</em>`));
      b.type = "button";
      b.onclick = () => { S.scope = k; S.sel.clear(); fetchPage(false); };
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

  function tileFor(it) {
    const t = el("div", "h3l-tile" + (S.sel.has(it.id) ? " sel" : ""));
    t.dataset.id = it.id;
    const th = el("div", "h3l-thumb");
    th.append(el("span", "h3l-ico", KIND_ICON[it.kind] || "📄"));
    th.dataset.src = api().libRawUrl(S.dir, it.id);
    th.dataset.kind = it.kind;
    th.dataset.thumb = api().libThumbUrl(S.dir, it.id);
    if (it.rating) th.append(el("span", "h3l-star", "★" + it.rating));
    if ((it.roles || []).length) {
      th.append(el("span", "h3l-role", esc(it.roles.join("·"))));
    }
    t.append(th);
    t.append(el("div", "h3l-name", esc(it.name)));
    const bits = [KIND_CN[it.kind] || it.kind];
    if (it.size) bits.push(fmtSize(it.size));
    if (it.mtime) bits.push(fmtTime(it.mtime));
    t.append(el("div", "h3l-meta", bits.map(esc).join(" · ")));
    const bg = el("div", "h3l-badges");
    for (const sn of it.refs || []) bg.append(el("span", "h3l-chip", `段${sn}`));
    for (const tg of it.tags || []) bg.append(el("span", "h3l-tag", esc(tg)));
    if (bg.children.length) t.append(bg);

    t.addEventListener("dblclick", () => openViewer(it));
    t.addEventListener("click", (e) => {
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
        if (kind === "image") {
          const im = document.createElement("img");
          im.loading = "lazy";
          im.src = d.dataset.thumb;
          im.onerror = () => { im.remove(); };
          d.replaceChildren(im);
        } else if (kind === "video") {
          const v = document.createElement("video");
          v.muted = true; v.preload = "metadata"; v.src = d.dataset.src;
          v.onerror = () => v.remove();
          d.replaceChildren(v);
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
    if ((it.kind === "image" || it.kind === "video" || it.kind === "audio") && !many) {
      m.append(menuItem("⤴ 引用到第 " + (S.seg || 1) + " 段", () => actRef(it)));
    }
    if (it.kind === "image" && !many) {
      m.append(menuItem("🎬 标为首帧图", () => actRole(it, "首帧图")));
      m.append(menuItem("🏁 标为尾帧图", () => actRole(it, "尾帧图")));
    }
    if (!many) {
      m.append(el("div", "h3l-sep"));
      m.append(menuItem("⭐ 评分…", () => actRate(it)));
      m.append(menuItem("🏷 标签…", () => actTag(it)));
      m.append(menuItem("✏ 重命名…", () => actAlias(it)));
      m.append(el("div", "h3l-sep"));
    }
    if (it.scope === "global" && !many) {
      m.append(menuItem("⇩ 调入项目", () => actMirror(it)));
    } else if (it.scope === "finals" && !many) {
      m.append(menuItem("→ 调入资产库", () => actToAssets(it)));
    }
    if (it.scope === "latent" && !many && window.H3Director?.upscaleLatent) {
      m.append(menuItem("🔍 二采放大（驱动画布 H3LatentUpscale）",
        () => window.H3Director.upscaleLatent(S.dir, it.file, say)));
    }
    m.append(menuItem("📋 复制路径", () => actCopyPath(it)));
    m.append(menuItem("📥 下载文件" + (many ? "（打包）" : ""), () => actDownload(it, ids)));
    m.append(menuItem("🗜 打包 ZIP" + (many ? `（${ids.length} 个）` : ""), () => actZip(ids)));
    m.append(el("div", "h3l-sep"));
    m.append(menuItem("🧪 暂存到 input（在画布用原生节点）", () => actStage(ids), false));
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
    const stars = el("div", "h3l-stars");
    for (let i = 1; i <= 5; i++) {
      const sp = el("span", i <= (it.rating || 0) ? "on" : "", "★");
      sp.onclick = async () => {
        const r = await A.libRate(S.dir, it.id, i === it.rating ? 0 : i);
        if (r.body?.ok) {
          it.rating = r.body.rating;
          stars.querySelectorAll("span").forEach((n, idx) => n.classList.toggle("on", idx < it.rating));
          renderGrid();
        }
      };
      stars.append(sp);
    }
    side.append(stars);

    const acts = el("div", "h3l-vact");
    const add = (label, fn) => {
      const b = el("button", "h3l-btn", label);
      b.type = "button";
      b.onclick = fn;
      acts.append(b);
      return b;
    };
    if (it.kind !== "latent") add("引用到段", () => actRef(it));
    if (it.kind === "image") {
      add("标首帧图", () => actRole(it, "首帧图"));
      add("标尾帧图", () => actRole(it, "尾帧图"));
    }
    add("重命名", () => actAlias(it));
    add("标签", () => actTag(it));
    if (it.scope === "global") add("调入项目", () => actMirror(it));
    if (it.scope === "finals") add("调入资产库", () => actToAssets(it));
    if (it.scope === "latent" && window.H3Director?.upscaleLatent) {
      add("二采放大", () => window.H3Director.upscaleLatent(S.dir, it.file, say));
    }
    add("暂存到 input", () => actStage([it.id]));
    add("下载", () => actDownload(it, [it.id]));
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
    if (typeof S.onChanged === "function") { try { S.onChanged(); } catch (e) { /* 可选 */ } }
  }

  async function actRef(it) {
    const A = api();
    const segNo = Number(S.seg) || 1;
    const r = await A.libRef(S.dir, it.id, segNo);
    if (!r.body?.ok) { say(A.errText(r, "引用失败")); return; }
    after(`已把「${it.name}」引用到第 ${segNo} 段`);
    fetchPage(false);
  }

  async function actRole(it, role) {
    const A = api();
    const r = await A.libRole(S.dir, it.id, role);
    if (!r.body?.ok) { say(A.errText(r, "标注失败")); return; }
    after(`「${it.name}」→ ${role}`);
    fetchPage(false);
  }

  async function actRate(it) {
    const v = window.prompt(`给「${it.name}」评分（0-5，0 清除）`, String(it.rating || 0));
    if (v === null) return;
    const A = api();
    const r = await A.libRate(S.dir, it.id, Number(v) || 0);
    if (!r.body?.ok) { say(A.errText(r, "评分失败")); return; }
    after("评分已保存");
    fetchPage(false);
  }

  async function actTag(it) {
    const v = window.prompt(`给「${it.name}」打标签（逗号分隔）`, (it.tags || []).join(","));
    if (v === null) return;
    const tags = String(v).split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    const A = api();
    const r = await A.libTag(S.dir, it.id, tags);
    if (!r.body?.ok) { say(A.errText(r, "标签保存失败")); return; }
    after("标签已保存");
    fetchPage(false);
  }

  async function actAlias(it) {
    const v = window.prompt(`「${it.name}」的新显示名（<=24 字）`, it.name);
    if (v === null || !String(v).trim()) return;
    const A = api();
    const r = await A.libAlias(S.dir, it.id, String(v).trim());
    if (!r.body?.ok) { say(A.errText(r, "重命名失败")); return; }
    after("已重命名");
    fetchPage(false);
  }

  async function actMirror(it) {
    const A = api();
    const r = await A.assetMirror({ dir: S.dir, asset_id: it.asset_id, alias: it.name });
    if (!r.body?.ok) { say(A.errText(r, "调入项目失败")); return; }
    after(`「${it.name}」已调入项目`);
    fetchPage(false);
  }

  async function actToAssets(it) {
    const A = api();
    const stem = String(it.file).split("/").pop().replace(/\.[^.]+$/, "") || "clip";
    const r = await A.moveMedia(S.dir, it.file, "assets",
      { register_asset: true, label: stem.slice(0, 24), kind: it.kind });
    if (!r.body?.ok) { say(A.errText(r, "调入资产库失败")); return; }
    after(`「${stem}」已调入资产库`);
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
    window.open(A.libRawUrl(S.dir, it.id), "_blank");
  }

  async function actZip(ids) {
    const A = api();
    const r = await A.libZip(S.dir, ids);
    if (!r.body?.ok) { say(A.errText(r, "打包失败")); return; }
    window.open(A.libZipUrl(S.dir, r.body.zip), "_blank");
    say(`已打包 ${r.body.count} 个文件（${fmtSize(r.body.bytes)}）`);
  }

  async function actStage(ids) {
    const A = api();
    let ok = 0, last = "";
    for (const id of ids) {
      const r = await A.libStage(S.dir, id);
      if (r.body?.ok) { ok++; last = r.body.staged || ""; }
      else say(A.errText(r, "暂存失败"));
    }
    if (ok) say(`已暂存 ${ok} 个到 input/${last ? last.split("/")[0] : ""}（在画布用 LoadImage/LoadVideo 引用）`);
  }

  async function actDelete(ids) {
    if (!window.confirm(`确认删除这 ${ids.length} 个文件的物理文件？（不可撤销）`)) return;
    const A = api();
    const r = await A.libDelete(S.dir, ids);
    if (!r.body?.ok) { say(A.errText(r, "删除失败")); return; }
    S.sel.clear();
    say(`已删除 ${(r.body.deleted || []).length} 个` +
        ((r.body.skipped || []).length ? `，跳过 ${r.body.skipped.length} 个` : ""));
    fetchPage(false);
  }

  /* ---------- 打开 ---------- */

  async function open(opts) {
    const o = opts || {};
    if (document.querySelector(".h3l-overlay")) return;
    injectStyles();
    S = {
      dir: String(o.dir || ""), seg: Number(o.seg) || 1,
      onChanged: o.onChanged,
      scope: "all", kind: "all", sort: "mtime", order: "desc",
      q: "", page: 1, pageSize: 60,
      items: [], total: 0, totalPages: 1, counters: {}, totalAll: 0,
      sel: new Set(), multi: false, collections: [], collection: "",
      cur: null,
    };

    const overlay = el("div", "h3l-overlay");
    const box = el("div", "h3l-box");
    const head = el("div", "h3l-head");
    head.append(el("strong", "", "🗂 素材库"));
    S.scopeBox = el("div", "h3l-scopes");
    head.append(S.scopeBox, el("div", "h3l-spacer"));
    const close = el("button", "h3l-close", "✕");
    close.title = "关闭（Esc）";
    close.onclick = () => overlay.remove();
    head.append(close);

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
    bar.append(mkSel(KINDS, "kind"));
    bar.append(mkSel(SORTS, "sort"));
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
    bar.append(multiBtn);

    const upBtn = el("button", "h3l-btn h3l-btn-cta", "＋ 上传");
    upBtn.type = "button";
    upBtn.title = "上传到全局库并链接本项目（提示词里用 [[别名]] 引用）";
    upBtn.onclick = () => {
      const inp = document.createElement("input");
      inp.type = "file";
      inp.multiple = true;
      inp.onchange = async () => {
        const files = [...(inp.files || [])];
        if (!files.length) return;
        const A = api();
        let ok = 0;
        for (const f of files) {
          try {
            const H3Assets = window.H3Assets;
            if (!H3Assets?.uploadDirect) throw new Error("上传接口不可用");
            const kind = H3Assets.guessKind(f);
            await H3Assets.uploadDirect(f, { kind, link_dir: S.dir, alias: f.name.replace(/\.[^.]+$/, "").slice(0, 24) });
            ok++;
          } catch (e) { say(`「${f.name}」上传失败：${e?.message || e}`); }
        }
        if (ok) { say(`已上传 ${ok} 个到全局库并链接本项目`); fetchPage(false); }
      };
      inp.click();
    };
    bar.append(upBtn);

    const scanBtn = el("button", "h3l-btn", "↻ 重新扫描");
    scanBtn.type = "button";
    scanBtn.onclick = async () => {
      const A = api();
      const r = await A.libScan(S.dir);
      if (!r.body?.ok) { say(A.errText(r, "扫描失败")); return; }
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

    await refreshCollections();
    await fetchPage(false);
    const A = api();
    try {
      const st = await A.libStatus({ dir: S.dir });
      if (st.body?.ok) {
        S.counters = st.body.counters || {};
        S.totalAll = st.body.total || 0;
        renderScopes();
      }
    } catch (e) { /* 状态拿不到不影响浏览 */ }
  }

  window.H3Lib = { open };
})();
