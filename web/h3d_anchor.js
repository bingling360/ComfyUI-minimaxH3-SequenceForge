/* H3 手动锚定：双轨时间线组件（步骤 8.1 / 8.2 / 8.3）。
 * 背景：把"把某段 latent 钉到本段某位置"从七处碎片收敛成一个 anchor 数据结构
 * （见 docs/手动锚定_分段latent参考规范化_实施规划.md §3），本文件只负责前端双轨 UI。
 * 加载约定照 h3_api.js：ComfyUI 自动加载 web/ 下所有 js，本文件只挂 window.H3Anchor，
 * 不注册入口；h3_director.js 在段设置面板里调用 buildAnchorPanel 做最小接线。
 * 后端契约冻结：grid_spec / anchor_sources / anchor_sheet / anchor_sheet_build 接口
 * 与 anchor 数据结构字段名严格按规划写，不在前端硬编码档位数字（一律取自 grid_spec）。
 */
(function () {
  "use strict";

  /* ---- 宿主访问器（由 h3_director.js 在 buildAnchorPanel 调用点注入）----
   * 为什么不直接调 getDirValue / setSegmentField：那两个是 h3_director.js 的
   * 模块级函数，并没有挂到 window 上，按全局名去找会当场抛错，还把"谁先加载"
   * 变成隐式契约——本模块曾因此整块渲染失败。改成注入后，本模块对加载顺序
   * 零依赖，也不再往全局摊函数。 */
  function dirOf(ctx) {
    const d = ctx && ctx.dir;
    return typeof d === "function" ? String(d() || "") : String(d || "");
  }
  function setAnchorsOf(ctx, arr) {
    if (!ctx || typeof ctx.setAnchors !== "function") {
      throw new Error("锚定面板缺少宿主写入器（h3_director.js 的 buildAnchorPanel 接线不完整）");
    }
    // 单一真相：end_f 恒等于 start_f + window。存储里这两个字段曾各自被 6 处代码写，
    // 于是出现「帧[68,63)」这种反区间、以及"5帧"和"39帧窗"同时出现在一行里。
    // 在唯一出口处夹一次，比去追每一处写入便宜也可靠。
    (arr || []).forEach((a) => {
      if (!a || !a.src) return;
      a.src.start_f = Math.max(0, Number(a.src.start_f) || 0);
      a.src.end_f = a.src.start_f + (Number(a.window) || 0);
    });
    ctx.setAnchors(arr);   // 宿主内部走 setSegmentField（已进段哈希 / 防抖落盘）
  }

  /* ---- 后端接口（与 h3_api.js 同款前缀处理：优先 ComfyUI api.fetchApi，否则直 fetch） ---- */
  async function _raw(path, opts) {
    if (typeof window !== "undefined" && window.comfyAPI?.api?.api) {
      return window.comfyAPI.api.api.fetchApi(path, opts || {});
    }
    return fetch(path, opts || {});
  }
  async function _getJson(path) {
    const r = await _raw(path);
    if (!r.ok) throw new Error("HTTP " + r.status + " · " + path);
    return r.json();
  }
  async function _postJson(path, body) {
    const r = await _raw(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error("HTTP " + r.status + " · " + path);
    return r.json();
  }

  /* grid_spec 是常量唯一来源：token 刻度与档位窗宽都不在前端写死，统一从这里取 */
  let _spec = null;
  async function getSpec() {
    if (_spec) return _spec;
    const j = await _getJson("/h3chain/grid_spec");
    if (!j || !j.ok) throw new Error("grid_spec 返回异常");
    _spec = j;
    return _spec;
  }

  /* ---- 小工具 ---- */
  function el(tag, cls, txt) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt !== undefined && txt !== null) e.textContent = String(txt);
    return e;
  }
  /* token 边界帧：首 token 1 帧、其后每 4 帧（FRAME_PER_TOKEN 循环），
   * 所以刻度不等距——这是 §5 强制画出来的原因（宽度锁 17k+5 才自解释）。 */
  function genTokens(spec, total) {
    const fpt = spec.frame_per_token;
    const marks = [];
    let f = 0, k = 0;
    while (f < total) {
      marks.push(f);
      f += fpt[k % fpt.length];
      k += 1;
    }
    marks.push(f); // 末边界（略超出 total，作收尾刻度）
    return marks;
  }
  /* 覆盖 window 帧所需的 token 数：从 frame_per_token 累加直到包住 window，
   * 与 latent 落盘规范里 tokens 字段同算法（如 22 帧→7 token）。 */
  function tokensForFrames(spec, frames) {
    const fpt = spec.frame_per_token;
    let f = 0, k = 0;
    while (f < frames) { f += fpt[k % fpt.length]; k += 1; }
    return k;
  }
  function clampInt(v, lo, hi) {
    v = Math.round(Number(v));
    if (!Number.isFinite(v)) v = lo;
    return Math.max(lo, Math.min(v, hi));
  }
  function isSnapWindow(spec, w) {
    return spec.snap_windows.indexOf(w) >= 0;
  }
  function genId() {
    // 稳定唯一 id：本会话内不重复即可（后端以此做锚标识）
    return "ax_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  }
  function newAnchor() {
    return {
      id: genId(),
      src: { kind: "prev_tail", ref: "", start_f: 0, end_f: 22, src_fps: null, meta_ok: false },
      at: { mode: "head", frame_idx: 0 },
      window: 22,
      branches: { av: "both" },
      on: true,
    };
  }
  /* 目标段内落点：head=0；tail=总长-window（负值自尾部计数亦在此减）；mid=用户 frame_idx（可负） */
  function resolveFrame(anchor, targetFrames) {
    if (anchor.at.mode === "tail") return Math.max(0, targetFrames - anchor.window);
    if (anchor.at.mode === "mid") {
      const fi = Number(anchor.at.frame_idx) || 0;
      return fi < 0 ? targetFrames + fi : fi;
    }
    return 0;
  }

  /* ---- 注入本组件专用样式（只在首次挂载时写一次，不在 h3_director.js 里堆 DOM 样式） ---- */
  let _styleDone = false;
  function ensureStyles() {
    if (_styleDone) return;
    _styleDone = true;
    const s = document.createElement("style");
    s.id = "h3d-anchor-style";
    s.textContent = [
      ".h3d-anchorpanel .h3d-anchor-grid{display:flex;flex-direction:column;gap:10px;margin-top:8px}",
      ".h3d-anchor-card{border:1px solid #3f4854;border-radius:9px;background:#1b2027;padding:10px;display:flex;flex-direction:column;gap:10px}",
      ".h3d-anchor-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}",
      ".h3d-anchor-head .h3d-anchor-id{font:700 11px ui-monospace,Consolas;color:var(--h3d-copper)}",
      ".h3d-anchor-three{display:grid;grid-template-columns:1fr 1fr 230px;gap:10px}",
      "@media (max-width:760px){.h3d-anchor-three{grid-template-columns:1fr}}",
      ".h3d-track{border:1px solid #2c3a4a;border-radius:8px;background:#161b21;padding:8px}",
      ".h3d-track h5{margin:0 0 6px;font-size:11px;color:var(--h3d-muted);font-weight:600}",
      ".h3d-strip{position:relative;height:56px;border-radius:6px;overflow:hidden;background:linear-gradient(90deg,#222a33,#2b3540,#222a33);border:1px solid #313b46;user-select:none;touch-action:none}",
      ".h3d-strip img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.85;pointer-events:none}",
      ".h3d-tick{position:absolute;top:0;bottom:0;width:1px;background:#5b6675;opacity:.55}",
      ".h3d-tick.major{background:#8b97a6;opacity:.9}",
      ".h3d-ticklabel{position:absolute;top:1px;font:9px ui-monospace,Consolas;color:var(--h3d-muted);transform:translateX(2px);pointer-events:none}",
      ".h3d-selbox{position:absolute;top:0;bottom:0;background:rgba(108,182,255,.22);border:1px solid var(--h3d-cyan);border-radius:4px;cursor:grab;display:flex;align-items:center;justify-content:center;color:var(--h3d-cyan);font:700 10px ui-monospace,Consolas}",
      ".h3d-selbox:active{cursor:grabbing}",
      ".h3d-winbtns{display:flex;flex-wrap:wrap;gap:4px;margin-top:7px}",
      ".h3d-winbtns button{border:1px solid #3f4854;border-radius:5px;background:#2d333b;color:var(--h3d-bone);padding:3px 8px;font:11px ui-monospace,Consolas;cursor:pointer}",
      ".h3d-winbtns button.on{border-color:var(--h3d-cyan);color:var(--h3d-cyan);background:#1c2a3a}",
      ".h3d-av{display:flex;gap:4px;margin-top:7px}",
      ".h3d-av button{border:1px solid #3f4854;border-radius:5px;background:#2d333b;color:var(--h3d-bone);padding:3px 7px;font-size:11px;cursor:pointer}",
      ".h3d-av button.on{border-color:var(--h3d-copper);color:var(--h3d-copper);background:#2a2418}",
      ".h3d-infoline{margin-top:7px;font:11px ui-monospace,Consolas;color:var(--h3d-bone);word-break:break-all}",
      ".h3d-atbtns{display:flex;gap:4px;margin-top:7px}",
      ".h3d-atbtns button{border:1px solid #3f4854;border-radius:5px;background:#2d333b;color:var(--h3d-bone);padding:3px 9px;font-size:11px;cursor:pointer}",
      ".h3d-atbtns button.on{border-color:var(--h3d-cyan);color:var(--h3d-cyan);background:#1c2a3a}",
      ".h3d-marker{position:absolute;top:-2px;width:2px;height:calc(100% + 4px);background:var(--h3d-warn)}",
      ".h3d-check{display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--h3d-bone)}",
      ".h3d-check div{display:flex;gap:6px;align-items:center}",
      ".h3d-check .ok{color:var(--h3d-ok)}",
      ".h3d-check .warn{color:var(--h3d-warn)}",
      ".h3d-check .bad{color:var(--h3d-danger)}",
      ".h3d-selrow{display:flex;gap:6px;align-items:center;margin-bottom:6px}",
      ".h3d-fpsinput{width:64px;border:1px solid #3f4854;border-radius:4px;background:#161b21;color:var(--h3d-bone);padding:2px 5px;font:11px ui-monospace,Consolas}",
      ".h3d-genprev{margin-top:6px;font-size:11px}",
      ".h3d-anchor-add{margin-top:8px}",
      ".h3d-anchor-echo{font:10.5px ui-monospace,Consolas;color:var(--h3d-muted);word-break:break-all}",
    ].join("\n");
    document.head.append(s);
  }

  /* ---- 报告回显（8.3）：把一段 anchor 拼成一行可读说明，贴在卡片底部 ---- */
  function anchorEcho(anchor, srcLabel, targetFrames) {
    const rf = resolveFrame(anchor, targetFrames);
    const span = anchor.window;
    const kindTxt = { both: "图像+音频", video: "仅图像", audio: "仅音频" }[anchor.branches.av] || "图像+音频";
    const sf = Math.max(0, Number(anchor.src.start_f) || 0);
    const ef = sf + span;          // 派生（end_f 不是独立真相，见 setAnchorsOf）
    const win = `帧[${sf},${ef})`;
    return `锚点：源 ${srcLabel} ${win} → 本段帧 ${rf}，${span} 帧窗，${kindTxt}`;
  }

  /* ---- 主入口：在 h3_director 段设置面板里挂一段"手动锚定"折叠区 ---- */
  function buildAnchorPanel(ctx) {
    ensureStyles();
    const { node, data, idx, refresh } = ctx;
    const seg = (data.ds.segments && data.ds.segments[idx]) || {};
    const anchors = Array.isArray(seg.anchors) ? seg.anchors : [];
    const dir = dirOf(ctx);

    const box = el("details", "h3d-adv h3d-anchorpanel");
    box.innerHTML = '<summary>📌 手动锚定（双轨时间线）</summary>';

    const grid = el("div", "h3d-anchor-grid");
    // 本地重建列表：新增/删除必须**当场**看见。只靠宿主 refresh() 不够——它刷的是
    // 整个导演台，段卡里这一块不一定会重挂（实测症状：点了没反应，切屏回来才有，
    // 有时切屏也没用）。seg 是同一对象引用，重读 seg.anchors 即最新。
    const renderList = () => {
      const list = Array.isArray(seg.anchors) ? seg.anchors : [];
      grid.innerHTML = "";
      list.forEach((a, i) => grid.append(renderAnchorCard(
        { ...ctx, anchor: a, anchors: list, index: i, rebuildList: renderList })));
    };
    renderList();
    box.append(grid);

    const addWrap = el("div", "h3d-anchor-add");
    const add = el("button", "h3d-btn", "＋ 新增锚定");
    add.title = "新增一条 anchor（同源可多条；head/mid/tail 落点各自自由）";
    add.onclick = () => {
      const cur = (Array.isArray(seg.anchors) ? seg.anchors : []).slice();
      cur.push(newAnchor());
      setAnchorsOf(ctx, cur);
      renderList();                 // 当场重建（宿主 refresh 不一定重挂这一块）
      (refresh || (() => {}))();
    };
    addWrap.append(add);
    box.append(addWrap);
    return box;
  }

  /* 单条 anchor 卡片：先放骨架，再异步拉 spec/sources 填充双轨（避免阻塞卡片渲染） */
  function renderAnchorCard(ctx) {
    const { node, data, idx, dir, refresh, anchor, anchors, index } = ctx;
    const card = el("div", "h3d-anchor-card");
    const head = el("div", "h3d-anchor-head");
    head.append(el("span", "h3d-anchor-id", "#" + (anchor.id || ("a" + index))));
    const onCb = document.createElement("input");
    onCb.type = "checkbox";
    onCb.checked = anchor.on !== false;
    onCb.title = "关闭=本锚不生效（仍保留配置）";
    onCb.onchange = () => { anchor.on = onCb.checked; commit(); };
    head.append(onCb, el("span", "", anchor.on === false ? "（已关）" : "生效中"));
    const del = el("button", "h3d-btn h3d-btn-danger", "✕ 删除");
    del.style.marginLeft = "auto";
    del.onclick = () => {
      const cur = (Array.isArray(data.ds.segments[idx].anchors)
        ? data.ds.segments[idx].anchors : []).filter((x) => x !== anchor);
      setAnchorsOf(ctx, cur);
      if (typeof ctx.rebuildList === "function") ctx.rebuildList();
      (refresh || (() => {}))();
    };
    head.append(del);
    card.append(head);

    const three = el("div", "h3d-anchor-three");
    const srcTrack = el("div", "h3d-track"); srcTrack.append(el("h5", "", "① 源轨 · 从素材选哪一段"));
    const tgtTrack = el("div", "h3d-track"); tgtTrack.append(el("h5", "", "② 目标轨 · 钉到本段哪个位置"));
    const sideCol = el("div", "h3d-track"); sideCol.append(el("h5", "", "③ 源选择 + 体检"));
    three.append(srcTrack, tgtTrack, sideCol);
    card.append(three);

    const echo = el("div", "h3d-anchor-echo");
    card.append(echo);

    // 异步填双轨：spec（常量）+ sources（该段可选源清单）
    Promise.all([
      getSpec().catch((e) => { console.error("[h3d-anchor] grid_spec 失败", e); return null; }),
      fetchSources(dir, idx + 1).catch((e) => { console.error("[h3d-anchor] anchor_sources 失败", e); return null; }),
    ]).then(([spec, sources]) => {
      if (!spec) { srcTrack.append(el("div", "h3d-anchor-echo", "⚠ 无法获取 grid_spec（后端接口未就绪）")); return; }
      fillSrcTrack(ctx, spec, sources, srcTrack);
      fillTgtTrack(ctx, spec, tgtTrack);
      fillSideCol(ctx, spec, sources, sideCol);
      updateEcho(ctx, sources, echo);
    });

    return card;

    /* 写回：改完即时落盘 + 刷新；用现有 setSegmentField，不另造保存 */
    function commit() { setAnchorsOf(ctx, anchors.slice()); (refresh || (() => {}))(); }
  }

  async function fetchSources(dir, seg1based) {
    const j = await _getJson("/h3chain/anchor_sources?dir=" + encodeURIComponent(dir || "") + "&seg=" + seg1based);
    if (!j || !j.ok) throw new Error("anchor_sources 返回异常");
    return j.sources || [];
  }
  /* 在 sources 清单里按 kind+ref 找源条目（拿 frames/fps/sheet/w/h 给刻度与体检用） */
  function findSource(sources, anchor) {
    if (!Array.isArray(sources)) return null;
    const wantRef = anchor.src.ref || "";
    return sources.find((s) => s.kind === anchor.src.kind && (s.ref || "") === wantRef) || null;
  }
  function srcLabel(anchor, src) {
    if (anchor.src.kind === "prev_tail") return "上段尾";
    return src ? (src.label || anchor.src.ref || anchor.src.kind) : (anchor.src.ref || anchor.src.kind);
  }

  function fillSrcTrack(ctx, spec, sources, host) {
    const { anchor, node, idx, data, refresh } = ctx;
    const src = findSource(sources, anchor);
    // 每次填充先清空：否则 rebuildCard / 切来源会在同一条轨上**再叠一层**
    //（实测症状：切一次来源就多一条轨道，分不清到底在看哪一段）
    host.innerHTML = "";
    host.append(el("h5", "", "① 源轨 · 从素材选哪一段"));
    const strip = el("div", "h3d-strip");
    // 轨长只能是**源的真实帧数**。拿 anchor.src.end_f 当轨长是错的——那是"取用窗的
    // 终点"、不是源的长度；混用会让 帧区间/帧数/刻度 三者互相矛盾（实测 帧[68,63)）。
    // 源长度未知时退到一个至少容得下当前选取的宽度，绝不产生反区间。
    const totalFrames = (src && Number.isFinite(src.frames))
      ? src.frames
      : Math.max(anchor.window, (Math.max(0, Number(anchor.src.start_f) || 0)) + anchor.window);
    // 三级降级 ①：有 contact sheet 直接裁格显示（零 VAE）
    if (src && src.sheet) {
      const im = document.createElement("img");
      im.loading = "lazy";
      im.src = "/h3chain/anchor_sheet?dir=" + encodeURIComponent(dirOf(ctx)) + "&file=" + encodeURIComponent(src.sheet);
      im.onerror = () => im.remove();
      strip.append(im);
    }
    // token 刻度（不等距）：按 frame_per_token 累加落点
    const marks = genTokens(spec, totalFrames);
    for (let t = 0; t < marks.length; t += 1) {
      const x = (marks[t] / totalFrames) * 100;
      const tk = el("div", "h3d-tick" + (t % 5 === 0 ? " major" : ""));
      tk.style.left = x + "%";
      strip.append(tk);
      if (t % 5 === 0) {
        const lb = el("div", "h3d-ticklabel", "T" + t);
        lb.style.left = x + "%";
        strip.append(lb);
      }
    }
    // 选取框：位置 = start_f，宽度 = window（窗宽锁档位，不允许拖宽）
    const sel = el("div", "h3d-selbox");
    const place = () => {
      const s = anchor.src.start_f || 0;
      sel.style.left = (s / totalFrames) * 100 + "%";
      sel.style.width = (anchor.window / totalFrames) * 100 + "%";
      sel.textContent = anchor.window + "帧";
    };
    place();
    // 拖框改起点（窗宽不变）——§8 风险：只允许拖位置，窗宽只能点档位按钮
    sel.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const rect = strip.getBoundingClientRect();
      const startX = e.clientX;
      const orig = anchor.src.start_f || 0;
      sel.setPointerCapture(e.pointerId);
      const move = (ev) => {
        const dx = ev.clientX - startX;
        let nf = Math.round(orig + (dx / rect.width) * totalFrames);
        nf = Math.max(0, Math.min(nf, totalFrames - anchor.window));
        anchor.src.start_f = nf;
        anchor.src.end_f = nf + anchor.window;
        place();
        updateInfo();
      };
      const up = () => {
        sel.removeEventListener("pointermove", move);
        sel.removeEventListener("pointerup", up);
        commit();
      };
      sel.addEventListener("pointermove", move);
      sel.addEventListener("pointerup", up);
    });
    strip.append(sel);
    host.append(strip);

    // 窗宽档位按钮（锁 17k+5，不连续拖宽）
    const winBtns = el("div", "h3d-winbtns");
    spec.snap_windows.forEach((w) => {
      const b = el("button", anchor.window === w ? "on" : "", String(w));
      b.title = "窗宽档位 " + w + " 帧（17k+5 对齐，后端硬校验）";
      b.onclick = () => {
        anchor.window = w;
        // 窗宽变后夹紧起点，避免越界
        const s = Math.min(anchor.src.start_f || 0, totalFrames - w);
        anchor.src.start_f = s;
        anchor.src.end_f = s + w;
        place();
        winBtns.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
        b.classList.add("on");
        updateInfo();
        // 同时刷新目标轨越界体检
        fillTgtTrack(ctx, spec, host.parentElement.querySelector(".h3d-track:nth-child(2)"));
        commit();
      };
      winBtns.append(b);
    });
    host.append(winBtns);

    // A/V 三态单选
    const av = el("div", "h3d-av");
    [["both", "图像+音频"], ["video", "仅图像"], ["audio", "仅音频"]].forEach(([v, t]) => {
      const b = el("button", (anchor.branches.av || "both") === v ? "on" : "", t);
      b.onclick = () => {
        anchor.branches.av = v;
        av.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
        b.classList.add("on");
        commit();
      };
      av.append(b);
    });
    host.append(av);

    // 源信息行（fps 未知时显示 ?s 并给手填框）
    const info = el("div", "h3d-infoline");
    const updateInfo = () => {
      const fps = anchor.src.src_fps;
      const span = anchor.window;
      const s = Math.max(0, Number(anchor.src.start_f) || 0);
      const e = s + span;          // 派生，不读存储的 end_f（曾与 start_f/window 互相矛盾）
      let secTxt;
      if (fps == null) {
        secTxt = "?s";
      } else {
        secTxt = (span / fps).toFixed(2) + "s";
      }
      info.textContent = `源：${anchor.src.ref || anchor.src.kind} · ${fps == null ? "?" : fps + "fps"} · 帧[${s},${e}) · ${secTxt} · ${span}帧`;
      // fps 未知：追加手填框（归一只能解码阶段做，这里让用户补元信息）
      if (fps == null) {
        const inp = el("input", "h3d-fpsinput");
        inp.type = "number"; inp.min = "1"; inp.step = "0.1"; inp.placeholder = "填 fps";
        inp.title = "源真实帧率未知：手填后秒数才准（仅前端显示用，后端以 latent 元信息为准）";
        inp.onchange = () => {
          const v = parseFloat(inp.value);
          if (Number.isFinite(v) && v > 0) { anchor.src.src_fps = v; updateInfo(); commit(); }
        };
        info.append(document.createTextNode(" "), inp);
      }
    };
    updateInfo();
    host.append(info);

    // 三级降级 ③：无 sheet 老条目给「生成预览」按钮（按需 VAE 解码一次并缓存）
    if (!src || !src.sheet) {
      const gp = el("div", "h3d-genprev");
      const btn = el("button", "h3d-btn", "🖼 生成预览");
      btn.onclick = async () => {
        btn.disabled = true; btn.textContent = "生成中…";
        try {
          const r = await _postJson("/h3chain/anchor_sheet_build", { dir: dirOf(ctx), file: anchor.src.ref || src?.ref || "" });
          if (r && r.ok && r.sheet) {
            anchor.src.meta_ok = true;
            // 重新拉源清单刷新 sheet
            const ns = await fetchSources(dirOf(ctx), idx + 1).catch(() => null);
            commit();
            // 重建源轨（含新 sheet）
            host.innerHTML = ""; host.append(el("h5", "", "① 源轨 · 从素材选哪一段"));
            fillSrcTrack({ ...ctx, anchor }, spec, ns, host);
          } else {
            btn.textContent = "生成失败";
          }
        } catch (err) {
          btn.textContent = "生成失败";
          console.error("[h3d-anchor] anchor_sheet_build 失败", err);
        }
      };
      gp.append(btn);
      host.append(gp);
    }

    function commit() { setAnchorsOf(ctx, (Array.isArray(data.ds.segments[idx].anchors) ? data.ds.segments[idx].anchors : []).slice()); (refresh || (() => {}))(); }
  }

  function fillTgtTrack(ctx, spec, host) {
    const { anchor, node, idx, data, refresh } = ctx;
    host.innerHTML = ""; host.append(el("h5", "", "② 目标轨 · 钉到本段哪个位置"));
    // 目标段总帧数：来自项目 manifest 的 params.length（每段帧数），取本段
    const mf = data.mf || {};
    const segLen = Array.isArray(mf.params && mf.params.length) ? (mf.params.length[idx] || mf.params.length[0] || 120) : 120;
    const totalFrames = Math.max(1, Number(segLen) || 120);

    const strip = el("div", "h3d-strip");
    strip.style.background = "linear-gradient(90deg,#2a2230,#342a38,#2a2230)";
    const marks = genTokens(spec, totalFrames);
    for (let t = 0; t < marks.length; t += 1) {
      const x = (marks[t] / totalFrames) * 100;
      const tk = el("div", "h3d-tick" + (t % 5 === 0 ? " major" : ""));
      tk.style.left = x + "%";
      strip.append(tk);
      if (t % 5 === 0) {
        const lb = el("div", "h3d-ticklabel", "T" + t);
        lb.style.left = x + "%";
        strip.append(lb);
      }
    }
    // 已有锚点标记（除自己外，标出其它锚的落点，提醒多锚）
    const all = Array.isArray(data.ds.segments[idx].anchors) ? data.ds.segments[idx].anchors : [];
    all.forEach((o) => {
      if (o === anchor) return;
      const rf = resolveFrame(o, totalFrames);
      const m = el("div", "h3d-marker");
      m.style.left = (rf / totalFrames) * 100 + "%";
      m.title = "已有锚 @" + rf;
      strip.append(m);
    });
    // 自己的落点高亮
    const meRf = resolveFrame(anchor, totalFrames);
    const me = el("div", "h3d-marker");
    me.style.background = "var(--h3d-cyan)";
    me.style.left = (meRf / totalFrames) * 100 + "%";
    strip.append(me);
    host.append(strip);

    // 落点按钮 head/mid/tail
    const atBtns = el("div", "h3d-atbtns");
    [["head", "开头"], ["mid", "中间"], ["tail", "结尾"]].forEach(([v, t]) => {
      const b = el("button", anchor.at.mode === v ? "on" : "", t);
      b.onclick = () => {
        anchor.at.mode = v;
        if (v === "mid" && (anchor.at.frame_idx === undefined || anchor.at.frame_idx === null)) anchor.at.frame_idx = Math.floor(totalFrames / 2);
        atBtns.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
        b.classList.add("on");
        midWrap.style.display = v === "mid" ? "flex" : "none";
        commit();
      };
      atBtns.append(b);
    });
    host.append(atBtns);

    // mid 模式：frame_idx 输入框（负值自尾部计数）
    const midWrap = el("div", "h3d-selrow");
    midWrap.style.display = anchor.at.mode === "mid" ? "flex" : "none";
    midWrap.append(el("span", "h3d-secs-hint", "落点帧 idx（负=自尾部）"));
    const fi = document.createElement("input");
    fi.type = "number"; fi.className = "h3d-fpsinput";
    fi.value = anchor.at.frame_idx || 0;
    fi.onchange = () => {
      anchor.at.frame_idx = parseInt(fi.value, 10) || 0;
      commit();
    };
    midWrap.append(fi);
    host.append(midWrap);

    function commit() { setAnchorsOf(ctx, (Array.isArray(data.ds.segments[idx].anchors) ? data.ds.segments[idx].anchors : []).slice()); (refresh || (() => {}))(); }
  }

  function fillSideCol(ctx, spec, sources, host) {
    const { anchor, node, idx, data, refresh } = ctx;
    host.innerHTML = "";
    host.append(el("h5", "", "③ 源选择 + 体检"));

    // 来源下拉：src_kind 五类
    const selRow = el("div", "h3d-selrow");
    const sel = document.createElement("select");
    sel.className = "h3d-select";
    sel.style.maxWidth = "200px";
    const kindLabel = { prev_tail: "上段尾", segment: "段", library: "latent 库", video: "视频", image: "图片" };
    const opts = (spec.src_kinds || []).map((k) => [k, kindLabel[k] || k]);
    for (const [v, t] of opts) {
      const o = document.createElement("option");
      o.value = v; o.textContent = t;
      if (v === anchor.src.kind) o.selected = true;
      sel.append(o);
    }
    sel.onchange = async () => {
      // 切来源：重置 src 元信息（ref/规格由后端 anchor_sources 决定，先置空待选具体条目）
      anchor.src.kind = sel.value;
      anchor.src.ref = "";
      anchor.src.start_f = 0;
      anchor.src.end_f = anchor.window;
      anchor.src.src_fps = null;
      anchor.src.meta_ok = false;
      commit();
      // 必须**重拉**源清单：sources 只含上一批 kind，不重拉的话切到任何新来源
      // 条目都是「（无可用条目）」——这正是"看不懂怎么选素材"的直接原因。
      // sources 是 fillSideCol 的形参，同一闭包内重写，rebuildCard 即可读到新值。
      const ns = await fetchSources(dirOf(ctx), idx + 1).catch(() => null);
      if (ns) sources = ns;
      rebuildCard();
    };
    selRow.append(el("span", "h3d-secs-hint", "来源"), sel);
    host.append(selRow);

    // 具体条目（按来源过滤 sources 清单，标到 ref）
    const itemRow = el("div", "h3d-selrow");
    const itemSel = document.createElement("select");
    itemSel.className = "h3d-select";
    itemSel.style.maxWidth = "200px";
    const items = (Array.isArray(sources) ? sources : []).filter((s) => s.kind === anchor.src.kind);
    const curRef = anchor.src.ref || "";
    if (!items.length) {
      itemSel.append(el("option", "", "（无可用条目）"));
    } else {
      if (anchor.src.kind === "prev_tail") {
        itemSel.append(el("option", "", "上段尾（默认）"));
      }
      for (const s of items) {
        const o = document.createElement("option");
        o.value = s.ref || "";
        o.textContent = s.label || s.ref || s.kind;
        if ((s.ref || "") === curRef) o.selected = true;
        itemSel.append(o);
      }
    }
    itemSel.onchange = () => {
      const s = items.find((x) => (x.ref || "") === itemSel.value) || null;
      anchor.src.ref = itemSel.value;
      if (s) {
        anchor.src.src_fps = s.fps == null ? null : s.fps;
        anchor.src.meta_ok = !!s.meta_ok;
        // 默认框占满源（不超窗宽档位则夹到档位）
        const frames = Number.isFinite(s.frames) ? s.frames : anchor.window;
        const w = isSnapWindow(spec, frames) ? frames : (spec.snap_windows[0] || anchor.window);
        anchor.window = w;
        anchor.src.start_f = 0;
        anchor.src.end_f = Math.min(w, frames);
      }
      commit();
      rebuildCard();
    };
    itemRow.append(el("span", "h3d-secs-hint", "条目"), itemSel);
    host.append(itemRow);

    // 体检项（逐条打勾/警告）
    const checks = el("div", "h3d-check");
    const src = findSource(sources, anchor);
    const projW = data.mf?.params?.width, projH = data.mf?.params?.height;
    // 1) 分辨率匹配（唯一硬约束：C/H/W）
    const c1 = el("div");
    if (src && src.w && src.h && projW && projH) {
      if (src.w === projW && src.h === projH) {
        c1.append(el("span", "ok", "✓"), el("span", "", `分辨率 ${src.w}×${src.h} 匹配`));
      } else {
        c1.append(el("span", "bad", "✗"), el("span", "", `分辨率 ${src.w}×${src.h} 与项目 ${projW}×${projH} 不匹配（硬约束，须先裁后编或报错）`));
      }
    } else {
      c1.append(el("span", "warn", "⚠"), el("span", "", `分辨率未知（源 ${src ? src.w + "×" + src.h : "?"}）`));
    }
    checks.append(c1);
    // 2) 帧窗合法（17k+5 档位）
    const c2 = el("div");
    if (isSnapWindow(spec, anchor.window)) {
      c2.append(el("span", "ok", "✓"), el("span", "", `帧窗 ${anchor.window} 合法 (17k+5)`));
    } else {
      c2.append(el("span", "bad", "✗"), el("span", "", `帧窗 ${anchor.window} 非法（不在 17k+5 档位，后端会硬报错）`));
    }
    checks.append(c2);
    // 3) 未越界（start+window ≤ 目标段总帧数）
    const c3 = el("div");
    const targetFrames = Math.max(1, Number((Array.isArray(data.mf?.params?.length) ? (data.mf.params.length[idx] || data.mf.params.length[0]) : 120)) || 120);
    const s0 = anchor.src.start_f || 0;
    if (s0 + anchor.window <= targetFrames) {
      c3.append(el("span", "ok", "✓"), el("span", "", `未越界 (${s0}+${anchor.window} ≤ ${targetFrames})`));
    } else {
      c3.append(el("span", "bad", "✗"), el("span", "", `越界 (${s0}+${anchor.window} > ${targetFrames})`));
    }
    checks.append(c3);
    // 4) 音频剩余时长警告（仅当取音频分支时）
    if ((anchor.branches.av === "both" || anchor.branches.av === "audio")) {
      const c4 = el("div");
      const nTok = tokensForFrames(spec, anchor.window);
      c4.append(el("span", "warn", "⚠"), el("span", "", `音频剩余时长将被裁至 ${nTok} token`));
      checks.append(c4);
    }
    host.append(checks);

    function commit() { setAnchorsOf(ctx, (Array.isArray(data.ds.segments[idx].anchors) ? data.ds.segments[idx].anchors : []).slice()); (refresh || (() => {}))(); }
    function rebuildCard() {
      // 来源/条目切换后重建整卡（spec 已缓存，直接同步填充）
      const card = host.closest(".h3d-anchor-card");
      if (!card) { (refresh || (() => {}))(); return; }
      const three = card.querySelector(".h3d-anchor-three");
      const tracks = three ? three.querySelectorAll(".h3d-track") : [];
      if (tracks.length >= 3) {
        fillSrcTrack(ctx, spec, sources, tracks[0]);
        fillTgtTrack(ctx, spec, tracks[1]);
        fillSideCol(ctx, spec, sources, tracks[2]);
      }
      updateEchoCtx();
    }
    function updateEchoCtx() {
      const ech = host.closest(".h3d-anchor-card");
      const e = ech ? ech.querySelector(".h3d-anchor-echo") : null;
      if (e) updateEcho(ctx, sources, e);
    }
  }

  function updateEcho(ctx, sources, echoEl) {
    const { anchor, idx } = ctx;
    const targetFrames = Math.max(1, Number((Array.isArray(ctx.data.mf?.params?.length) ? (ctx.data.mf.params.length[idx] || ctx.data.mf.params.length[0]) : 120)) || 120);
    echoEl.textContent = anchorEcho(anchor, srcLabel(anchor, findSource(sources, anchor)), targetFrames);
  }

  if (typeof window !== "undefined") {
    window.H3Anchor = { buildAnchorPanel, getSpec, genTokens, tokensForFrames };
  }
})();
