/* H3 手动锚定：双轨时间线组件（步骤 8.1 / 8.2 / 8.3）。
 * 背景：把"把某段 latent 钉到本段某位置"从七处碎片收敛成一个 anchor 数据结构
 * （见 docs/手动锚定_分段latent参考规范化_实施规划.md §3），本文件只负责前端双轨 UI。
 * 加载约定照 h3_api.js：ComfyUI 自动加载 web/ 下所有 js，本文件只挂 window.H3Anchor，
 * 不注册入口；h3_director.js 在段设置面板里调用 buildAnchorPanel 做最小接线。
 * 后端契约冻结：grid_spec / anchor_sources / anchor_sheet / anchor_sheet_build 接口
 * 与 anchor 数据结构字段名严格按规划写，不在前端硬编码档位数字（一律取自 grid_spec）。
 *
 * 来源只有两类（用户 2026-09-16 拍板）：
 *   「段」  = 本项目已落盘的 seg_NNN.pt（选上一段 = 旧 prev_tail，同一份 latent）
 *   「素材」= 从**现有素材库**里挑的图片/视频/latent（挑出条目后由后端确认元信息）
 * 素材一律从资产库选，不自己枚举输入目录——那等于另造一个素材库，两套数据必然漂移。
 */
(function () {
  "use strict";

  /* ---- 宿主访问器（由 h3_director.js 在 buildAnchorPanel 调用点注入）----
   * node / data / idx    卡片上下文
   * dir()                当前项目目录名
   * setAnchors(arr)      写回 anchor 列表
   * frameLen()           本段总帧数（像素帧）——与段卡标题那个「≈N帧」同一个换算函数
   * getSources()         段源清单（整块只拉一次，各卡共享同一份）
   * loadSources(ref?)    拉清单；给 ref 时把该素材的元信息条目并进来
   *
   * 为什么不直接按全局名调宿主的写入器：那些是 h3_director.js 的**模块级**函数，
   * 并没有挂到 window 上，按全局名去找会当场抛错，还把"谁先加载"变成隐式契约
   * ——本模块曾因此整块渲染失败。改成注入后，本模块对加载顺序零依赖。 */
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
    ctx.setAnchors(arr);   // 宿主内部走 setSegmentField（写进节点 widget）
    // ★ 写透本面板手里的副本：getDs(node) 每次都是 `JSON.parse(w.value)` 出来的**新对象**，
    //   而 setSegmentField 写的是它自己那一个，本面板的 ctx.data.ds 仍旧指向旧对象。
    //   不写透的话，紧接着的 renderList()/体检/目标轨读到的全是**陈旧数据**——症状就是
    //   「点了新增没反应，退出导演台再进来才出现」（重进时整卡重建才拿到新 JSON）。
    //   这是刷新问题的真正根因，不是 DOM 被推倒（那只是表象）。
    if (ctx.data && ctx.data.ds && Array.isArray(ctx.data.ds.segments)
        && ctx.data.ds.segments[ctx.idx]) {
      ctx.data.ds.segments[ctx.idx].anchors = arr;
    }
  }

  /* ---- 后端接口（与 h3_api.js 同款前缀处理：优先 ComfyUI api.fetchApi，否则直 fetch） ---- */
  async function _raw(path, opts) {
    if (typeof window !== "undefined" && window.comfyAPI?.api?.api) {
      return window.comfyAPI.api.api.fetchApi(path, opts || {});
    }
    return fetch(path, opts || {});
  }
  /* 后端 _err 的响应体是 {ok:false, code, message}：把 message 透出来。
   * 只抛「HTTP 404 · path」会把真正的原因（项目不存在 / 素材没登记 / 文件不在原位）
   * 全丢掉，面板就只能显示一句「（无可用条目）」，用户不知道该去做什么。 */
  async function _errText(r, path) {
    let msg = "";
    try {
      const j = await r.json();
      msg = (j && (j.message || j.error)) || "";
    } catch (e) { msg = ""; }
    return msg || ("HTTP " + r.status + " · " + path);
  }
  async function _getJson(path) {
    const r = await _raw(path);
    if (!r.ok) throw new Error(await _errText(r, path));
    return r.json();
  }
  async function _postJson(path, body) {
    const r = await _raw(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await _errText(r, path));
    return r.json();
  }
  /* 素材库两个取图接口（自己拼 URL，不依赖 H3Api 先加载——少一条隐式契约） */
  const libThumbUrl = (dir) => (id) =>
    "/h3chain/lib_thumb?dir=" + encodeURIComponent(dir) + "&id=" + encodeURIComponent(id);
  const libRawUrl = (dir) => (id) =>
    "/h3chain/lib_raw?dir=" + encodeURIComponent(dir) + "&id=" + encodeURIComponent(id);

  /* grid_spec 是常量唯一来源：token 刻度与档位窗宽都不在前端写死，统一从这里取。
   * 缓存的是**那个 promise**（不是解析结果）：面板挂载时每张卡都会来取一次，
   * 缓存结果值会被并发穿透成 N 次重复请求，缓存 promise 才只有一次。 */
  let _specReq = null;
  function getSpec() {
    if (!_specReq) {
      _specReq = _getJson("/h3chain/grid_spec").then((j) => {
        if (!j || !j.ok) throw new Error("grid_spec 返回异常");
        return j;
      });
    }
    return _specReq;
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
  /* 窗宽合法性：档位表 + 特例 1（单帧身份锚）。1 不在 SNAP_WINDOWS 里，但
   * grid.snap_window_down(1) == 1、后端也接受（迁移出来的尾锚就是 window=1），
   * 只查档位表会把合法的单帧锚误报成"非法"。 */
  function isSnapWindow(spec, w) {
    return w === 1 || (spec.snap_windows || []).indexOf(w) >= 0;
  }
  function genId() {
    // 稳定唯一 id：本会话内不重复即可（后端以此做锚标识）
    return "ax_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  }
  /* 取不超过源帧数的最大 17k+5 档位：源比最小档还短时返回 1（单帧锚）。
   * 原实现一律退到最小档 5，等于悄悄把"源只有 8 帧"的锚钉成 5 帧之外还要更多，
   * 遇到图片源（1 帧）更是直接矛盾。 */
  function snapDown(spec, frames) {
    const ws = spec.snap_windows || [];
    const minW = spec.min_window_frames || 5;
    if (!Number.isFinite(frames) || frames < minW) return 1;
    let best = 1;
    for (const w of ws) if (w <= frames) best = w;
    return best;
  }
  /* 新建锚：默认「段」+ 上一段（同一份 latent，就是旧的 prev_tail）。
   * 上一段没落盘 latent（该段设了「不存 latent」）时 ref 留空，由用户从「段」列表里挑
   * ——后端 validate_anchors 会硬拦空 ref，不会静默生成一个没有源的锚。 */
  function newAnchor(prev) {
    return {
      id: genId(),
      src: {
        kind: "segment", ref: (prev && prev.ref) || "",
        start_f: 0, end_f: 22,
        src_fps: (prev && prev.fps != null) ? prev.fps : null,
        meta_ok: false,
      },
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
      // 段卡里的可用宽度很窄（还要跟段卡其它块并排），横向三栏 + 230px 侧栏会被压扁，
      // 所以统一上下堆叠：源轨 → 目标轨 → 体检。宽屏下也一样，保持一种读法。
      ".h3d-anchor-three{display:grid;grid-template-columns:1fr;gap:8px}",
      ".h3d-track{border:1px solid #2c3a4a;border-radius:8px;background:#161b21;padding:8px}",
      ".h3d-track h5{margin:0 0 6px;font-size:11px;color:var(--h3d-muted);font-weight:600}",
      ".h3d-strip{position:relative;height:56px;border-radius:6px;overflow:hidden;background:linear-gradient(90deg,#222a33,#2b3540,#222a33);border:1px solid #313b46;user-select:none;touch-action:none}",
      ".h3d-strip img,.h3d-strip video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.85;pointer-events:none}",
      ".h3d-tick{position:absolute;top:0;bottom:0;width:1px;background:#5b6675;opacity:.55}",
      ".h3d-tick.major{background:#8b97a6;opacity:.9}",
      ".h3d-ticklabel{position:absolute;top:1px;font:9px ui-monospace,Consolas;color:var(--h3d-muted);transform:translateX(2px);pointer-events:none}",
      ".h3d-selbox{position:absolute;top:0;bottom:0;background:rgba(108,182,255,.22);border:1px solid var(--h3d-cyan);border-radius:4px;cursor:grab;display:flex;align-items:center;justify-content:center;color:var(--h3d-cyan);font:700 10px ui-monospace,Consolas}",
      ".h3d-selbox:active{cursor:grabbing}",
      ".h3d-winbtns{display:flex;flex-wrap:wrap;gap:4px;margin-top:7px;align-items:center}",
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
      ".h3d-selrow{display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap}",
      ".h3d-fpsinput{width:64px;border:1px solid #3f4854;border-radius:4px;background:#161b21;color:var(--h3d-bone);padding:2px 5px;font:11px ui-monospace,Consolas}",
      ".h3d-genprev{margin-top:6px;font-size:11px}",
      ".h3d-anchor-add{margin-top:8px}",
      ".h3d-anchor-echo{font:10.5px ui-monospace,Consolas;color:var(--h3d-muted);word-break:break-all}",
      ".h3d-anchor-empty{font-size:11.5px;color:var(--h3d-warn);line-height:1.6}",
    ].join("\n");
    document.head.append(s);
  }

  /* ---- 报告回显（8.3）：把一段 anchor 拼成一行可读说明，贴在卡片底部 ---- */
  function anchorEcho(anchor, srcLabel, targetFrames) {
    const rf = resolveFrame(anchor, targetFrames);
    const span = anchor.window;
    const kindTxt = { both: "图像+音频", video: "仅图像", audio: "仅音频" }[anchor.branches.av] || "图像+音频";
    const atTxt = { head: "段首", mid: "该帧", tail: "段尾" }[anchor.at.mode] || anchor.at.mode;
    const sf = Math.max(0, Number(anchor.src.start_f) || 0);
    const ef = sf + span;          // 派生（end_f 不是独立真相，见 setAnchorsOf）
    return `锚点：源 ${srcLabel} 帧[${sf},${ef}) → 本段${atTxt} 帧 ${rf}，${span} 帧窗，${kindTxt}`;
  }

  /* ---- 主入口：在 h3_director 段设置面板里挂一段"手动锚定"折叠区 ---- */
  function buildAnchorPanel(ctx) {
    ensureStyles();
    const { data, idx } = ctx;
    if (typeof ctx.frameLen !== "function") {
      throw new Error("锚定面板缺少 frameLen（本段总帧数）注入，接线不完整");
    }
    const seg = (data.ds.segments && data.ds.segments[idx]) || {};

    const box = el("details", "h3d-adv h3d-anchorpanel");
    box.innerHTML = '<summary>📌 手动锚定（双轨时间线）</summary>';

    const grid = el("div", "h3d-anchor-grid");
    const empty = el("div", "h3d-anchor-empty");   // 源清单拉不到 / 为空时的自解释

    /* 段源清单**整块只拉一次**，各卡共享同一份：新增锚要拿它预填「上一段」，
     * 选素材时要往里并一条（loadSources(ref)）。 */
    let sources = null;
    const getSources = () => sources;
    const loadSources = async (extraRef) => {
      let u = "/h3chain/anchor_sources?dir=" + encodeURIComponent(dirOf(ctx))
        + "&seg=" + (idx + 1);
      if (extraRef) u += "&ref=" + encodeURIComponent(extraRef);
      const j = await _getJson(u);
      if (!j || !j.ok) throw new Error("anchor_sources 返回异常");
      sources = j.sources || [];
      return sources;
    };
    /* 「上一段」= 后端标成 prev_tail 的那个槽位（它仍带着 slot，可回查段条目）。
     * 该段没落盘 latent 时清单里就没有它 → 返回 null，ref 留空等用户挑。 */
    const prevSegment = () => {
      const all = getSources() || [];
      const nt = all.find((s) => s.kind === "prev_tail");
      if (!nt) return null;
      return all.find((s) => s.kind === "segment" && s.slot === nt.slot) || null;
    };

    // 本地重建列表：新增/删除必须**当场**看见。只靠宿主 refresh() 不够——它刷的是
    // 整个导演台，段卡里这一块不一定会重挂（实测症状：点了没反应，切屏回来才有）。
    // seg 是同一对象引用，重读 seg.anchors 即最新。
    const renderList = () => {
      const list = Array.isArray(seg.anchors) ? seg.anchors : [];
      grid.innerHTML = "";
      list.forEach((a, i) => grid.append(renderAnchorCard(
        { ...ctx, anchor: a, anchors: list, index: i,
          getSources, loadSources, rebuildList: renderList })));
    };
    renderList();
    box.append(empty, grid);

    const addWrap = el("div", "h3d-anchor-add");
    const add = el("button", "h3d-btn", "＋ 新增锚定");
    add.title = "新增一条 anchor（同源可多条；head/mid/tail 落点各自自由）。"
      + "默认取上一段作源——与「没有显式锚时自动注入的段首桥」是同一份 latent";
    add.onclick = () => {
      const cur = (Array.isArray(seg.anchors) ? seg.anchors : []).slice();
      cur.push(newAnchor(prevSegment()));
      setAnchorsOf(ctx, cur);
      renderList();                 // 当场重建
      // 不再调宿主 refresh()：它会**全量重刷导演台**，把本面板刚渲染的 DOM 连同
      // 正在进行的异步填充一起推倒，结果是"点了没反应，退出重进才出现轨道"。
    };
    addWrap.append(add);
    box.append(addWrap);

    (async () => {
      try {
        await loadSources();
      } catch (e) {
        sources = [];
        empty.textContent = "无法读取源清单：" + ((e && e.message) || e);
        return;
      }
      // 空状态要说明**原因**（"（无可用条目）"等于没说）：段源看项目存档，
      // 素材走素材库，两者空的原因完全不同。
      const nSeg = sources.filter((s) => s.kind === "segment").length;
      const nLib = sources.filter((s) => s.kind === "library").length;
      const sol = [];
      if (!nSeg) sol.push("本项目还没有已落盘的段（跑完至少一段才有段源；"
        + "若某段设了「不存 latent」，它也不会出现在段列表里）");
      if (!nLib) sol.push("项目 latent/ 目录还是空的（可用「转码为 latent」生成，"
        + "或直接把素材库里的图片/视频选作源）");
      empty.textContent = sol.length ? "可选项提示：" + sol.join("；") : "";
      empty.style.display = sol.length ? "" : "none";
      renderList();
    })();
    return box;
  }

  /* 单条 anchor 卡片：先放骨架，再异步拉 spec/sources 填充双轨（避免阻塞卡片渲染） */
  function renderAnchorCard(ctx) {
    const { data, idx, anchor, anchors, index } = ctx;
    const sources = ctx.getSources() || [];
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
      // 同「新增」：本地重建，不惊动宿主
    };
    head.append(del);
    card.append(head);

    const three = el("div", "h3d-anchor-three");
    const srcTrack = el("div", "h3d-track"); srcTrack.append(el("h5", "", "① 源轨 · 从源里取哪一段"));
    const tgtTrack = el("div", "h3d-track"); tgtTrack.append(el("h5", "", "② 目标轨 · 钉到本段哪个位置"));
    const sideCol = el("div", "h3d-track"); sideCol.append(el("h5", "", "③ 源选择 + 体检"));
    three.append(srcTrack, tgtTrack, sideCol);
    card.append(three);

    const echo = el("div", "h3d-anchor-echo");
    card.append(echo);

    // 异步填双轨：spec（常量）+ sources（该段可选源清单，panel 已缓存）
    buildTracks();

    return card;

    function buildTracks() {
      Promise.all([getSpec()])
        .then(([spec]) => {
          fillSrcTrack(ctx, spec, sources, srcTrack);
          fillTgtTrack(ctx, spec, tgtTrack);
          fillSideCol(ctx, spec, sources, sideCol);
          updateEcho(ctx, sources, echo);
        })
        .catch((e) => {
          console.error("[h3d-anchor] grid_spec 失败", e);
          srcTrack.append(el("div", "h3d-anchor-echo", "⚠ 无法获取 grid_spec（后端接口未就绪）"));
        });
    }

    /* 写回：改完即时落盘 + 本地重建；用现有 setSegmentField，不另造保存 */
    function commit() {
      setAnchorsOf(ctx, anchors.slice());
      if (typeof ctx.__rebuildCard === "function") ctx.__rebuildCard();
    }
  }

  /* 在 sources 清单里按 kind+ref 找源条目（拿 frames/fps/sheet/w/h 给刻度与体检用） */
  function findSource(sources, anchor) {
    if (!Array.isArray(sources)) return null;
    const wantRef = anchor.src.ref || "";
    return sources.find((s) => s.kind === anchor.src.kind && (s.ref || "") === wantRef) || null;
  }
  function srcLabel(anchor, src) {
    if (anchor.src.kind === "prev_tail") return "上段尾";
    if (src && src.label) return src.label;
    if (anchor.src.ref) return anchor.src.ref;
    return anchor.src.kind === "segment" ? "（未选段）" : "（未选素材）";
  }
  /* 本链分辨率：manifest 里记的 params（上一次跑生成时的 C/H/W 基准）。
   * 项目还没跑过任何一段时没有这个键 —— 那就说"项目还没跑过"，不要只说"未知"。 */
  function chainWH(ctx) {
    const p = (ctx.data.mf && ctx.data.mf.params) || {};
    const w = Number(p.width), h = Number(p.height);
    return (w > 0 && h > 0) ? { w, h } : null;
  }
  const ASSET_KINDS = ["image", "video", "library"];

  function fillSrcTrack(ctx, spec, sources, host) {
    const { anchor, data, idx } = ctx;
    const src = findSource(sources, anchor);
    // 每次填充先清空：否则 rebuildCard / 切来源会在同一条轨上**再叠一层**
    //（实测症状：切一次来源就多一条轨道，分不清到底在看哪一段）
    host.innerHTML = "";
    host.append(el("h5", "", "① 源轨 · 从源里取哪一段"));
    const strip = el("div", "h3d-strip");
    // 轨长只能是**源的真实帧数**。拿 anchor.src.end_f 当轨长是错的——那是"取用窗的
    // 终点"、不是源的长度；混用会让 帧区间/帧数/刻度 三者互相矛盾（实测 帧[68,63)）。
    // 源长度未知时退到一个至少容得下当前选取的宽度，绝不产生反区间。
    const totalFrames = (src && Number.isFinite(src.frames))
      ? src.frames
      : Math.max(anchor.window, (Math.max(0, Number(anchor.src.start_f) || 0)) + anchor.window);
    // 预览：latent 有 contact sheet 就贴 sheet（零 VAE）；素材库来的条目用库缩略图
    // （图片）/ 库原文件首帧（视频）。都没有才落到下面的「生成预览」按钮。
    if (src && src.sheet) {
      const im = document.createElement("img");
      im.loading = "lazy";
      im.src = "/h3chain/anchor_sheet?dir=" + encodeURIComponent(dirOf(ctx))
        + "&file=" + encodeURIComponent(src.sheet);
      im.onerror = () => im.remove();
      strip.append(im);
    } else if (src && src.item_id) {
      if (src.kind === "video") {
        const v = document.createElement("video");
        v.muted = true; v.preload = "metadata";
        v.src = libRawUrl(dirOf(ctx))(src.item_id);
        v.onerror = () => v.remove();
        strip.append(v);
      } else {
        const im = document.createElement("img");
        im.loading = "lazy";
        im.src = libThumbUrl(dirOf(ctx))(src.item_id);
        im.onerror = () => im.remove();
        strip.append(im);
      }
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

    // 窗宽档位（锁 17k+5，不连续拖宽）：常用几档外露，其余收进「更多」——22 个按钮
    // 铺两行把面板撑得很吵，而实际会用到的就前面那几个。
    const COMMON = [1, 5, 22, 39, 56];
    const winBtns = el("div", "h3d-winbtns");
    const moreBtns = el("div", "h3d-winbtns");
    moreBtns.style.display = "none";
    const all = [1].concat(spec.snap_windows || []);
    const paintWins = () => {
      winBtns.querySelectorAll("button").forEach((x) => {
        x.classList.toggle("on", Number(x.dataset.w) === anchor.window);
      });
      moreBtns.querySelectorAll("button").forEach((x) => {
        x.classList.toggle("on", Number(x.dataset.w) === anchor.window);
      });
    };
    const mkWinBtn = (w, into) => {
      const b = el("button", "", String(w));
      b.dataset.w = String(w);
      b.title = w === 1
        ? "单帧身份锚（tail 锚的默认形态）：只强调某一张画面，不带时长"
        : "窗宽档位 " + w + " 帧（17k+5 对齐，非档位宽度会被模型折掉、后端硬校验）";
      b.onclick = () => {
        anchor.window = w;
        // 窗宽变后夹紧起点，避免越界
        const s = Math.min(anchor.src.start_f || 0, Math.max(0, totalFrames - w));
        anchor.src.start_f = s;
        anchor.src.end_f = s + w;
        place();
        paintWins();
        updateInfo();
        // 同时刷新目标轨越界体检
        const card = host.closest(".h3d-anchor-card");
        const tgt = card ? card.querySelectorAll(".h3d-track")[1] : null;
        if (tgt) fillTgtTrack(ctx, spec, tgt);
        commit();
      };
      into.append(b);
    };
    all.forEach((w) => {
      if (COMMON.indexOf(w) >= 0 || w === anchor.window) mkWinBtn(w, winBtns);
      else mkWinBtn(w, moreBtns);
    });
    paintWins();
    const moreToggle = el("button", "h3d-btn", "更多▾");
    moreToggle.title = "展开全部档位（5 起每 +17 一档，到 " +
      ((spec.snap_windows || []).slice(-1)[0] || "?") + " 帧）";
    moreToggle.onclick = () => {
      const open = moreBtns.style.display !== "none";
      moreBtns.style.display = open ? "none" : "flex";
      moreToggle.textContent = open ? "更多▾" : "收起▴";
    };
    winBtns.append(moreToggle);
    host.append(winBtns, moreBtns);

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
      info.textContent = `源：${srcLabel(anchor, src)} · ${fps == null ? "?" : fps + "fps"}`
        + ` · 帧[${s},${e}) · ${secTxt} · ${span}帧`
        + (src && Number.isFinite(src.frames) ? ` / 源共 ${src.frames} 帧` : " / 源长度未知");
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

    // 无 sheet 的 latent 老条目给「生成预览」按钮（按需 VAE 解码一次并缓存）。
    // 素材库来的条目已经有缩略图/首帧，不需要这一步。
    if (anchor.src.kind === "library" && (!src || !src.sheet) && anchor.src.ref) {
      const gp = el("div", "h3d-genprev");
      const btn = el("button", "h3d-btn", "🖼 生成预览");
      btn.title = "给这个 latent 补一张 contact sheet（用同段已落盘的 finals/seg_NNN.mp4 抽帧，不加载 VAE）";
      btn.onclick = async () => {
        btn.disabled = true; btn.textContent = "生成中…";
        try {
          const r = await _postJson("/h3chain/anchor_sheet_build",
            { dir: dirOf(ctx), file: anchor.src.ref });
          if (r && r.ok && r.sheet) {
            anchor.src.meta_ok = true;
            await ctx.loadSources();
            commit();
            fillSrcTrack(ctx, spec, ctx.getSources(), host);
          } else {
            btn.textContent = "生成失败";
          }
        } catch (err) {
          btn.textContent = "生成失败";
          btn.title = (err && err.message) || String(err);
          console.error("[h3d-anchor] anchor_sheet_build 失败", err);
        }
      };
      gp.append(btn);
      host.append(gp);
    }

    function commit() {
      setAnchorsOf(ctx, (Array.isArray(data.ds.segments[idx].anchors)
        ? data.ds.segments[idx].anchors : []).slice());
      if (typeof ctx.__rebuildCard === "function") ctx.__rebuildCard();
    }
  }

  function fillTgtTrack(ctx, spec, host) {
    const { anchor, data, idx } = ctx;
    host.innerHTML = "";
    host.append(el("h5", "", "② 目标轨 · 钉到本段哪个位置"));
    // 本段总帧数**只从宿主取**（frameLen）：它就是段卡标题那个「≈N帧」，与后端
    // nodes._snap_seconds 同一套换算（秒×24 → 就近吸附 17k+5）。曾经这里自己又算
    // 一遍（而且用的是向上对齐），于是目标轨刻度和分段时间对不上——同一个数只许
    // 有一个算法、一个来源。
    const totalFrames = Math.max(1, Number(ctx.frameLen()) || 1);

    const strip = el("div", "h3d-strip");
    strip.style.background = "linear-gradient(90deg,#2a2230,#342a38,#2a2230)";
    strip.title = "在刻度上点或拖 = 把落点钉到该帧。位置随便放；宽度由 ① 的窗宽决定"
      + "（非 17k+5 档位的宽度会被模型折掉，所以宽度只能点档位按钮）";
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
    // 别的锚的落点（提醒多锚共存）
    const all = Array.isArray(data.ds.segments[idx].anchors) ? data.ds.segments[idx].anchors : [];
    all.forEach((o) => {
      if (o === anchor) return;
      const m = el("div", "h3d-marker");
      m.style.left = (resolveFrame(o, totalFrames) / totalFrames) * 100 + "%";
      m.title = "已有锚 @" + resolveFrame(o, totalFrames);
      strip.append(m);
    });
    // 自己的落点：**一条线**（用户明确要求：目标轨是"钉在哪一帧"，不是跨度）。
    // 窗宽不在这里表达——它在 ① 已经用选取框画过了，这里再画一遍反而混淆。
    const mark = el("div", "h3d-marker");
    mark.style.background = "var(--h3d-cyan)";
    mark.style.width = "3px";
    const label = el("div", "h3d-ticklabel");
    label.style.color = "var(--h3d-cyan)";
    label.style.fontWeight = "700";
    const place = () => {
      const rf = resolveFrame(anchor, totalFrames);
      const x = (rf / totalFrames) * 100;
      mark.style.left = x + "%";
      label.style.left = x + "%";
      label.textContent = "帧 " + rf;
    };
    place();
    strip.append(mark, label);
    host.append(strip);

    // 落点模式：开头/结尾是快捷预设；任意位置直接在刻度上点/拖（= mid）。
    // 「中间」按钮已去掉——能拖之后它只是冗余。
    const atBtns = el("div", "h3d-atbtns");
    [["head", "开头"], ["tail", "结尾"]].forEach(([v, t]) => {
      const b = el("button", anchor.at.mode === v ? "on" : "", t);
      b.onclick = () => {
        anchor.at.mode = v;
        atBtns.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
        b.classList.add("on");
        midWrap.style.display = "none";
        place();
        commit();
      };
      atBtns.append(b);
    });
    host.append(atBtns);

    // mid 的精确输入（负值自尾部计数）——拖不准时用键盘补
    const midWrap = el("div", "h3d-selrow");
    midWrap.style.display = anchor.at.mode === "mid" ? "flex" : "none";
    midWrap.append(el("span", "h3d-secs-hint", "落点帧（负=自尾部；也可直接拖刻度）"));
    const fi = document.createElement("input");
    fi.type = "number"; fi.className = "h3d-fpsinput";
    fi.value = anchor.at.frame_idx || 0;
    fi.onchange = () => {
      anchor.at.frame_idx = parseInt(fi.value, 10) || 0;
      anchor.at.mode = "mid";
      atBtns.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
      midWrap.style.display = "flex";
      place();
      commit();
    };
    midWrap.append(fi);
    host.append(midWrap);

    // 点/拖定位：指针落在刻度哪个位置就把落点钉到哪一帧。
    // 夹到 [0, 总帧数 - 窗宽]——再往右会越界（后端会硬报错，不如这里就夹住）。
    const setFromX = (clientX) => {
      const rect = strip.getBoundingClientRect();
      if (!rect.width) return;
      const raw = ((clientX - rect.left) / rect.width) * totalFrames;
      const f = Math.max(0, Math.min(Math.round(raw), Math.max(0, totalFrames - anchor.window)));
      anchor.at.mode = "mid";
      anchor.at.frame_idx = f;
      fi.value = f;
      midWrap.style.display = "flex";
      atBtns.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
      place();
    };
    strip.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      strip.setPointerCapture(e.pointerId);
      setFromX(e.clientX);
      const move = (ev) => setFromX(ev.clientX);
      const up = () => {
        strip.removeEventListener("pointermove", move);
        strip.removeEventListener("pointerup", up);
        commit();
      };
      strip.addEventListener("pointermove", move);
      strip.addEventListener("pointerup", up);
    });

    function commit() {
      setAnchorsOf(ctx, (Array.isArray(data.ds.segments[idx].anchors)
        ? data.ds.segments[idx].anchors : []).slice());
      if (typeof ctx.__rebuildCard === "function") ctx.__rebuildCard();
    }
  }

  function fillSideCol(ctx, spec, sources, host) {
    const { anchor, data } = ctx;
    host.innerHTML = "";
    host.append(el("h5", "", "③ 源选择 + 体检"));

    // 选素材的失败原因挂在**卡片上下文**上，不放在本函数的局部变量里：
    // 报错后要 rebuildCard 才看得见，而重建会把局部变量冲掉（实测症状：警告永远不出现）。
    let pickErr = ctx.__pickErr || "";
    const isAsset = ASSET_KINDS.indexOf(anchor.src.kind) >= 0;
    const legacyPrev = anchor.src.kind === "prev_tail";

    /* 来源只有两类：「段」= 本项目已落盘的 seg_NNN.pt；「素材」= 从素材库挑。
     * prev_tail 已从 UI 撤掉（用户明确说冗余——「有上段的选择就行了」，选上一段就是
     * 同一份 latent），只在旧存档回显时以只读项出现，改选任一项即被替换。 */
    const selRow = el("div", "h3d-selrow");
    const sel = document.createElement("select");
    sel.className = "h3d-select";
    sel.style.maxWidth = "200px";
    if (legacyPrev) {
      const o = document.createElement("option");
      o.value = "prev_tail"; o.textContent = "上段尾（旧格式）"; o.selected = true;
      sel.append(o);
    }
    for (const [v, t] of [["segment", "段"], ["asset", "素材"]]) {
      const o = document.createElement("option");
      o.value = v; o.textContent = t;
      if (!legacyPrev && (v === "segment") === !isAsset) o.selected = true;
      sel.append(o);
    }
    sel.onchange = () => {
      // 切来源 = 换一类源：原来的 ref 不再适用，清空待用户重选（后端会硬拦空 ref，
      // 不会拿着一个旧条目静默跑出别的东西）。
      const seg2 = sel.value === "segment";
      anchor.src.kind = seg2 ? "segment" : "image";
      anchor.src.ref = "";
      anchor.src.start_f = 0;
      anchor.src.end_f = anchor.window;
      anchor.src.src_fps = null;
      anchor.src.meta_ok = false;
      commit();
      rebuildCard();
    };
    selRow.append(el("span", "h3d-secs-hint", "来源"), sel);
    host.append(selRow);
    if (legacyPrev) {
      host.append(el("div", "h3d-anchor-echo",
        "当前是旧格式的「上段尾」源：它等价于选上一段，改成任一条目即可替换。"));
    }

    /* ---- 条目：段 = 下拉选段；素材 = 打开素材库挑（复用现有库面板） ---- */
    const itemRow = el("div", "h3d-selrow");
    if (anchor.src.kind === "segment") {
      const items = (sources || []).filter((s) => s.kind === "segment");
      const itemSel = document.createElement("select");
      itemSel.className = "h3d-select";
      itemSel.style.maxWidth = "200px";
      if (!items.length) {
        const o = document.createElement("option");
        o.textContent = (sources && sources.length)
          ? "还没有已落盘的段（跑完至少一段才会出现）"
          : "载入中…";
        itemSel.append(o);
        itemSel.disabled = true;
      } else {
        if (!anchor.src.ref) {
          itemSel.append(new Option("（请选择）", ""));
        }
        for (const s of items) {
          const o = document.createElement("option");
          o.value = s.ref || "";
          o.textContent = (s.label || s.ref) +
            (Number.isFinite(s.frames) ? ` · ${s.frames}帧` : "");
          if ((s.ref || "") === (anchor.src.ref || "")) o.selected = true;
          itemSel.append(o);
        }
      }
      itemSel.onchange = () => {
        const s = items.find((x) => (x.ref || "") === itemSel.value) || null;
        anchor.src.ref = itemSel.value;
        if (s) {
          anchor.src.src_fps = s.fps == null ? null : s.fps;
          anchor.src.meta_ok = !!s.meta_ok;
          // 窗宽跟着源走：源多长就用不超过它的最大档位；比最小档还短（图片=1 帧）
          // 就取 1（单帧身份锚）。后端允许 window==1，非法档位它会硬报错。
          anchor.window = snapDown(spec, s.frames);
          anchor.src.start_f = 0;
          anchor.src.end_f = anchor.window;
        }
        commit();
        rebuildCard();
      };
      itemRow.append(el("span", "h3d-secs-hint", "条目"), itemSel);
    } else {
      const cur = findSource(sources, anchor);
      const btn = el("button", "h3d-btn", anchor.src.ref ? "🔁 换素材" : "🗂 选择素材");
      btn.title = "打开素材库挑选（项目资产 / 全局库 / 成片 / latent 四个库，"
        + "带搜索、筛选、缩略图）；选中后自动回填并确认帧数、尺寸、帧率";
      btn.onclick = () => pickAsset();
      itemRow.append(el("span", "h3d-secs-hint", "素材"), btn,
        el("span", "h3d-secs-hint", srcLabel(anchor, cur)));
    }
    host.append(itemRow);
    if (pickErr) {
      const we = el("div");
      we.append(el("span", "bad", "⚠"), el("span", "", pickErr));
      const wbox = el("div", "h3d-check");
      wbox.append(we);
      host.append(wbox);
    }

    /* ---- 打开素材库挑一个源 ----
     * 素材一律从资产库选（用户拍板），不在这里枚举输入目录——那等于另造一个素材库。
     * 挑完先让后端确认这一个文件的元信息（帧数/尺寸/帧率）并把它并进源清单，
     * 再落进 anchor：面板后面的刻度、体检、越界都靠这份元信息。 */
    async function pickAsset() {
      const H3Lib = window.H3Lib;
      if (!H3Lib || typeof H3Lib.open !== "function") {
        ctx.__pickErr = "素材库模块（h3_library.js）没加载，无法挑素材";
        rebuildCard();
        return;
      }
      H3Lib.open({
        dir: dirOf(ctx),
        seg: ctx.idx + 1,
        pickKinds: ["image", "video", "latent"],
        onPick: async (item) => {
          const kind = item.kind === "latent" ? "library" : item.kind;
          try {
            const list = await ctx.loadSources(item.id);
            const hit = (list || []).find((x) => x.item_id === item.id);
            if (!hit) throw new Error("后端没有返回这个素材的元信息");
            anchor.src.kind = kind;
            anchor.src.ref = hit.ref;
            anchor.src.src_fps = hit.fps == null ? null : hit.fps;
            anchor.src.meta_ok = !!hit.meta_ok;
            anchor.window = snapDown(spec, hit.frames);
            anchor.src.start_f = 0;
            anchor.src.end_f = anchor.window;
            ctx.__pickErr = hit.resolvable === false
              ? `「${hit.label}」在素材库里没有别名、也没有 asset_id，生成时节点侧寻址不到：`
                + "先在素材库给它「改名」（登记别名）或重新上传一次"
              : "";
            setAnchorsOf(ctx, (Array.isArray(data.ds.segments[ctx.idx].anchors)
              ? data.ds.segments[ctx.idx].anchors : []).slice());
          } catch (e) {
            ctx.__pickErr = ((e && e.message) || String(e));
          }
          rebuildCard();
        },
      });
    }

    /* ---- 体检：逐条打勾/警告 ----
     * 口径（唯一硬约束是 C/H/W）：
     *   段 / latent 源 —— 分辨率必须与本链一致，否则 latent 桥按行预留拼不上（硬报错）
     *   视频 / 图片源 —— 允许不一致：执行期走 center-cover 先裁后编，这是正常路径
     * 本链尺寸来自 manifest.params（上一次跑生成的记录）；项目还没跑过就说"还没跑过"，
     * 不要笼统地说"未知"——那会让人以为是面板坏了。 */
    const checks = el("div", "h3d-check");
    const src = findSource(sources, anchor);
    const chain = chainWH(ctx);
    const c1 = el("div");
    if (anchor.src.kind === "prev_tail") {
      c1.append(el("span", "ok", "✓"),
        el("span", "", "上段尾：取的就是上一段输出的 latent 本身，形状天然与本链一致"));
    } else if (!src) {
      c1.append(el("span", "warn", "⚠"), el("span", "", anchor.src.ref
        ? `源「${anchor.src.ref}」不在源清单里（旧档，或换过素材库）：重选一次条目即可补齐帧数与分辨率`
        : "还没有选源：本锚不会生效——后端以「非 prev_tail 必须给 ref」硬拦，不会静默跳过"));
    } else if (src.kind === "video" || src.kind === "image") {
      c1.append(el("span", "ok", "✓"),
        el("span", "", (src.w && src.h)
          ? `源 ${src.w}×${src.h} → 按本链尺寸 center-cover 先裁后编${chain ? `（${chain.w}×${chain.h}）` : ""}`
          : "视频/图片源按本链尺寸 center-cover 先裁后编"));
    } else if (src && src.w && src.h && chain) {
      if (src.w === chain.w && src.h === chain.h) {
        c1.append(el("span", "ok", "✓"), el("span", "", `分辨率 ${src.w}×${src.h} 与本链一致`));
      } else {
        c1.append(el("span", "bad", "✗"),
          el("span", "", `分辨率 ${src.w}×${src.h} ≠ 本链 ${chain.w}×${chain.h}`
            + "（latent 源无法先裁后编，桥拼不上；请改用素材或放大到本链尺寸）"));
      }
    } else if (!chain) {
      c1.append(el("span", "warn", "⚠"),
        el("span", "", "本链分辨率未知：项目还没跑过任何一段（存档里没有宽高记录）"));
    } else {
      c1.append(el("span", "warn", "⚠"),
        el("span", "", `源分辨率读不出（源 ${src.w || "?"}×${src.h || "?"}）：`
          + "该 latent / 段存档可能损坏，或后端缺 Pillow/av 依赖"));
    }
    checks.append(c1);
    // 2) 帧窗合法（17k+5 档位；1 = 单帧身份锚）
    const c2 = el("div");
    if (isSnapWindow(spec, anchor.window)) {
      c2.append(el("span", "ok", "✓"), el("span", "", `帧窗 ${anchor.window} 合法`
        + (anchor.window === 1 ? "（单帧锚）" : " (17k+5)")));
    } else {
      c2.append(el("span", "bad", "✗"), el("span", "", `帧窗 ${anchor.window} 非法`
        + "（不在 17k+5 档位，后端会硬报错）"));
    }
    checks.append(c2);
    // 3) 源内取用窗不越界（比的是**源**的帧数，不是本段帧数——这两个数没有任何关系）
    const c3 = el("div");
    const s0 = Math.max(0, Number(anchor.src.start_f) || 0);
    if (src && Number.isFinite(src.frames)) {
      if (s0 + anchor.window <= src.frames) {
        c3.append(el("span", "ok", "✓"),
          el("span", "", `源内取用窗未越界 (${s0}+${anchor.window} ≤ ${src.frames})`));
      } else {
        c3.append(el("span", "bad", "✗"),
          el("span", "", `源内取用窗越界 (${s0}+${anchor.window} > ${src.frames})`));
      }
    } else {
      c3.append(el("span", "warn", "⚠"), el("span", "", "源帧数未知，越界检查跳过"));
    }
    checks.append(c3);
    // 4) 目标落点不越界（validate_anchors 会拦：落点 + 窗宽 ≤ 本段帧数）
    const c4 = el("div");
    const tgtFrames = Math.max(1, Number(ctx.frameLen()) || 1);
    const rf = resolveFrame(anchor, tgtFrames);
    if (rf + anchor.window <= tgtFrames) {
      c4.append(el("span", "ok", "✓"),
        el("span", "", `本段落点未越界 (${rf}+${anchor.window} ≤ ${tgtFrames})`));
    } else {
      c4.append(el("span", "bad", "✗"),
        el("span", "", `本段落点越界 (${rf}+${anchor.window} > ${tgtFrames} 帧)：`
          + "把落点往左挪，或改用「结尾」"));
    }
    checks.append(c4);
    // 5) 音频剩余时长警告（仅当取音频分支时）
    if ((anchor.branches.av === "both" || anchor.branches.av === "audio")) {
      const c5 = el("div");
      const nTok = tokensForFrames(spec, anchor.window);
      c5.append(el("span", "warn", "⚠"), el("span", "", `音频剩余时长将被裁至 ${nTok} token`));
      checks.append(c5);
    }
    host.append(checks);

    function commit() {
      setAnchorsOf(ctx, (Array.isArray(data.ds.segments[ctx.idx].anchors)
        ? data.ds.segments[ctx.idx].anchors : []).slice());
      if (typeof ctx.__rebuildCard === "function") ctx.__rebuildCard();
    }
    // 把本地重建能力挂到 ctx：其余两个轨（以及本轨）的 commit() 靠它做一致性刷新，
    // 取代原先"每次改动都让宿主全量重刷"的做法。每次 rebuildCard 都会重设，幂等。
    ctx.__rebuildCard = () => rebuildCard();
    function rebuildCard() {
      // 来源/条目切换后重建整卡（spec 已缓存，直接同步填充）
      const card = host.closest(".h3d-anchor-card");
      if (!card) { (ctx.refresh || (() => {}))(); return; }
      const tracks = card.querySelectorAll(".h3d-track");
      if (tracks.length >= 3) {
        const list = ctx.getSources() || [];
        fillSrcTrack(ctx, spec, list, tracks[0]);
        fillTgtTrack(ctx, spec, tracks[1]);
        fillSideCol(ctx, spec, list, tracks[2]);
      }
      const e = card.querySelector(".h3d-anchor-echo");
      if (e) updateEcho(ctx, ctx.getSources() || [], e);
    }
  }

  function updateEcho(ctx, sources, echoEl) {
    const { anchor } = ctx;
    const targetFrames = Math.max(1, Number(ctx.frameLen()) || 1);
    echoEl.textContent = anchorEcho(anchor, srcLabel(anchor, findSource(sources, anchor)), targetFrames);
  }

  if (typeof window !== "undefined") {
    window.H3Anchor = { buildAnchorPanel, getSpec, genTokens, tokensForFrames };
  }
})();
