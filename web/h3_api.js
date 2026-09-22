/* H3 API 封装（M4）：新后端接口 + revision 乐观锁 + 结构化错误。
 * ComfyUI 会自动加载 web/ 下所有 js，本文件只挂 window.H3Api，不注册入口。
 * 依赖：全局 fetch（ComfyUI 前端 api.fetchApi 优先，有则用，无则 fetch 直调）。
 */
(function () {
  "use strict";

  async function _call(path, opts) {
    opts = opts || {};
    // 优先走 ComfyUI 前端 api.fetchApi（自动处理 /api 前缀），无则直 fetch
    try {
      if (typeof window !== "undefined" && window.comfyAPI?.api?.api) {
        const r = await window.comfyAPI.api.api.fetchApi(path, opts);
        const body = await r.json().catch(() => ({}));
        return { status: r.status, body };
      }
    } catch (e) { /* fallthrough */ }
    const r = await fetch(path, opts);
    const body = await r.json().catch(() => ({}));
    return { status: r.status, body };
  }

  function _json(method, path, data) {
    return _call(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: data === undefined ? undefined : JSON.stringify(data),
    });
  }

  /* ---- SSE 流式（提示词优化进度）------------------------------------------
   * 服务端帧格式：`event: <kind>\ndata: <json>\n\n`（kind = progress|done|error）。
   *
   * 为什么用 fetch + ReadableStream 而**不用 EventSource**：EventSource 只支持
   * GET，而优化请求要 POST 一大坨图片 dataURL（几 MB），塞查询串会爆 URL 长度。
   *
   * 为什么要自己拼帧：`reader.read()` 给的是**任意切分的字节块**，一帧可能被劈成
   * 两个 chunk、一个 chunk 也可能含多帧。所以必须缓冲到见到空行为止再解析 ——
   * 直接对每个 chunk 做 JSON.parse 是最常见的写法，也最容易在长输出上偶发丢帧。 */
  function _parseFrame(raw) {
    let kind = "message";
    const data = [];
    for (const line of String(raw).split("\n")) {
      if (line.startsWith("event:")) kind = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trim());
    }
    if (!data.length) return null;
    try { return Object.assign({ type: kind }, JSON.parse(data.join("\n"))); }
    catch (e) { return null; }          // 半截/坏帧直接丢，不让它炸掉整条流
  }

  async function _postStream(path, data, onEvent, opts) {
    opts = opts || {};
    const init = {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(data),
    };
    if (opts.signal) init.signal = opts.signal;
    let res;
    try {
      if (typeof window !== "undefined" && window.comfyAPI?.api?.api) {
        res = await window.comfyAPI.api.api.fetchApi(path, init);
      } else {
        res = await fetch(path, init);
      }
    } catch (e) {
      if (e && e.name === "AbortError") throw e;   // 用户取消：交给调用方识别
      throw new Error(`流式请求发不出去（${e && e.message ? e.message : e}）`);
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      return { status: res.status, body, events: [] };
    }
    if (!res.body || typeof res.body.getReader !== "function") {
      throw new Error("当前环境不支持流式响应（拿不到 ReadableStream）");
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    const events = [];
    let buf = "";
    let last = null;
    /* 统一换行：中间隔一层代理（nginx/cloudflare）时帧分隔符会变成 CRLF，
     * 而下面找的是 "\n\n" —— 不归一化的话整条流一帧都解析不出来，表现是
     * "进度条一动不动，最后报流里没有事件"。JSON 里的 CR 会被 stringify
     * 转义成 \\r，所以整包删 CR 不会伤到正文。 */
    const _norm = (s) => s.replace(/\r/g, "");
    const drain = () => {
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const evt = _parseFrame(buf.slice(0, i));
        buf = buf.slice(i + 2);
        if (!evt) continue;
        events.push(evt);
        last = evt;
        if (onEvent) { try { onEvent(evt); } catch (e) { /* 回调出错不影响收流 */ } }
      }
    };
    for (;;) {
      const step = await reader.read();
      if (step.done) break;
      buf = _norm(buf + decoder.decode(step.value, { stream: true }));
      drain();
    }
    buf = _norm(buf + decoder.decode());
    drain();
    if (buf.trim()) {                   // 服务端没以空行收尾时的尾帧
      const evt = _parseFrame(buf);
      if (evt) {
        events.push(evt); last = evt;
        if (onEvent) { try { onEvent(evt); } catch (e) { /* 同上 */ } }
      }
    }
    if (!last) {
      return { status: 200, events,
               body: { ok: false, code: "EMPTY_STREAM",
                       message: "服务端没有推送任何事件（流被中途掐断？）" } };
    }
    return { status: 200, body: last, events };
  }

  function isConflict(res) {
    return res && (res.status === 409 || res.body?.code === "REVISION_CONFLICT");
  }

  function isBusy(res) {
    return res && (res.status === 423 || res.body?.code === "BUSY");
  }

  function errText(res, fallback) {
    if (!res) return fallback || "网络异常";
    if (isBusy(res)) return "正在生成中，剪辑/转码入口已锁定（完成后自动解锁）";
    if (res.body?.message) return res.body.message;
    if (res.body?.error) return res.body.error;
    return (fallback || "请求失败") + `（HTTP ${res.status}）`;
  }

  /* 查询串：跳过空值（scopes/过滤全是可选参数） */
  function _qs(obj) {
    const p = new URLSearchParams();
    Object.keys(obj || {}).forEach((k) => {
      const v = obj[k];
      if (v === undefined || v === null || v === "") return;
      p.append(k, String(v));
    });
    return p.toString();
  }

  const Api = {
    getProjects: (summary) => _call(summary ? "/h3chain/projects?summary=1" : "/h3chain/projects"),
    getProject: (dir) => _call("/h3chain/project?dir=" + encodeURIComponent(dir || "")),
    createProject: (dir, copyFrom) => _json("POST", "/h3chain/create_project",
    { dir, copy_from: copyFrom || "" }),
    savePrompts: (dir, prompts, segments, base_revision) =>
      _json("POST", "/h3chain/save_prompts", { dir, prompts, segments, base_revision }),
    saveAssets: (dir, assets, base_revision) =>
      _json("POST", "/h3chain/assets", { dir, assets, base_revision }),
    assetCheck: (assets, segments, dir) => _json("POST", "/h3chain/asset_check", { assets, segments, dir }),
    latentSlice: (dir, src, start_f, end_f, save_name, base_revision, kind) =>
      _json("POST", "/h3chain/latent_slice", { dir, src, start_f, end_f, save_name, base_revision, kind: kind || "av" }),
    latentDelete: (dir, file, base_revision) =>
      _json("POST", "/h3chain/latent_delete", { dir, file, base_revision }),
    trim: (dir, src, start_s, end_s, save_name, base_revision) =>
      _json("POST", "/h3chain/trim", { dir, src, start_s, end_s, save_name, base_revision }),
    probe: (dir, src) => _json("POST", "/h3chain/probe", { dir, src }),
    moveMedia: (dir, src, dest, opts) =>
      _json("POST", "/h3chain/move_media", Object.assign({ dir, src, dest }, opts || {})),
    importAsset: (dir, src, opts) =>
      _json("POST", "/h3chain/import_asset", Object.assign({ dir, src }, opts || {})),
    splitAv: (dir, src, opts) =>
      _json("POST", "/h3chain/split_av", Object.assign({ dir, src }, opts || {})),
    busy: () => _call("/h3chain/busy"),
    merge: (dir, items) => _json("POST", "/h3chain/merge", { dir, items }),
    libraryUploadJson: (payload) => _json("POST", "/h3chain/library_upload", payload),
    libraryUpload: (formData) => _call("/h3chain/library_upload", { method: "POST", body: formData }),
    libraryFileUrl: (asset_id) => "/h3chain/library_file?asset_id=" + encodeURIComponent(asset_id || ""),
    compileRefs: (payload) => _json("POST", "/h3chain/compile_refs", payload),
    assetLinks: (dir) => _call("/h3chain/asset_links?dir=" + encodeURIComponent(dir || "")),
    assetLink: (payload) => _json("POST", "/h3chain/asset_link", payload),
    /* 手动改素材标注（发给 LLM 的稳定短编号 图片1/视频1/音频1）；mark 传空串=清除 */
    assetMark: (payload) => _json("POST", "/h3chain/asset_mark", payload),
    assetUnlink: (payload) => _json("POST", "/h3chain/asset_unlink", payload),
    assetMirror: (payload) => _json("POST", "/h3chain/asset_mirror", payload),
    vaeFiles: () => _call("/h3chain/vae_files"),
    getPromptRules: () => _call("/h3chain/prompt-rules"),
    getOptimizerConfig: () => _call("/h3chain/optimizer-config"),
    optimize: (payload) => _json("POST", "/h3chain/optimize", payload),
    /* 同上，但边跑边推进度（SSE）。onEvent 每帧回调一次：
     *   {type:"progress", phase, reasoning_chars, content_chars, tokens, max_tokens, elapsed}
     *   {type:"done",  ok:true,  prompt}
     *   {type:"error", ok:false, code, message}
     * 返回 {status, body, events}，body 即最后一帧（与 optimize 同形，便于复用调用点）。
     * opts.signal = AbortController.signal，用于「取消」。 */
    optimizeStream: (payload, onEvent, opts) =>
      _postStream("/h3chain/optimize_stream", payload, onEvent, opts),
    expand: (payload) => _json("POST", "/h3chain/expand", payload),
    expandValidate: (payload) => _json("POST", "/h3chain/expand_validate", payload),
    /* 剧本扩写（内容发散器）：总意图 + 时长范围 + 段数 -> N 段中文剧本 */
    expandMulti: (payload) => _json("POST", "/h3chain/expand_multi", payload),
    /* 扩写 + 优化一步到位：中文意图 -> 剧本 -> H3 官方格式文本（三框合一后段卡唯一入口） */
    expandOptimize: (payload) => _json("POST", "/h3chain/expand_optimize", payload),
    /* 扩写 + 优化一步到位：**流式版**（进度帧带 stage="expand" / "optimize"） */
    expandOptimizeStream: (payload, onEvent, opts) =>
      _postStream("/h3chain/expand_optimize_stream", payload, onEvent, opts),
    /* 多段提示词优化（格式编译器）：N 段剧本 -> N 段 H3 官方格式 + 逐段校验 */
    optimizeMulti: (payload) => _json("POST", "/h3chain/optimize_multi", payload),
    /* 显存清理：卸载 ComfyUI 驻留的全部模型 + 清空分配器缓存（OOM 后点一下再重跑）。
     * 生成中后端回 423 BUSY（把正在用的模型卸了等于砍掉这次运行）。 */
    vramCleanup: () => _json("POST", "/h3chain/vram_cleanup"),
    /* 同上，**流式版**：N 段串行跑，进度帧多带 seg / seg_no / total
     * （前端据此画「第 i/N 段」的整条进度）。 */
    optimizeMultiStream: (payload, onEvent, opts) =>
      _postStream("/h3chain/optimize_multi_stream", payload, onEvent, opts),
    /* ---- 性能设置（全局，跨项目）----
     * 真源在后端 perf.py：DEFAULT_PERF / parse_state / apply_runtime。
     * 前端不自己算档位，只做开关 + 显示后端给的只读诊断。 */
    perfGet: () => _call("/h3chain/perf"),
    perfSet: (perf) => _json("POST", "/h3chain/perf", { perf: perf || {} }),
    /* ---- 素材库（Library）：一个浏览器 + 四个 scope ---- */
    libList: (p) => _call("/h3chain/lib_list?" + _qs(p)),
    libItem: (p) => _call("/h3chain/lib_item?" + _qs(p)),
    libStatus: (p) => _call("/h3chain/lib_status?" + _qs(p)),
    libCollections: (dir) => _call("/h3chain/lib_collections?" + _qs({ dir })),
    libThumbUrl: (dir, id) =>
      "/h3chain/lib_thumb?" + _qs({ dir, id }),
    libRawUrl: (dir, id, download) =>
      "/h3chain/lib_raw?" + _qs({ dir, id, download: download ? 1 : "" }),
    libZipUrl: (dir, name) => "/h3chain/lib_zip_file?" + _qs({ dir, name }),
    libScan: (dir) => _json("POST", "/h3chain/lib_scan", { dir }),
    libRate: (dir, id, rating) => _json("POST", "/h3chain/lib_rate", { dir, id, rating }),
    libTag: (dir, id, tags) => _json("POST", "/h3chain/lib_tag", { dir, id, tags }),
    libAlias: (dir, id, alias) => _json("POST", "/h3chain/lib_alias", { dir, id, alias }),
    /* mode: "copy"（前端固定传这个：复制一份进项目 assets/ 并登记清单）
     *     | "link"（老形态：只写 asset_links 不复制文件 —— 前端已不再使用，
     *             后端保留只为兼容老项目里已有的链接条目） */
    libMirror: (dir, id, label, mode) =>
      _json("POST", "/h3chain/lib_mirror", { dir, id, label, mode: mode || "link" }),
    libArchive: (dir, ids) => _json("POST", "/h3chain/lib_archive", { dir, ids }),
    libCollectionSave: (payload) => _json("POST", "/h3chain/lib_collection_save", payload),
    libCollectionDelete: (dir, id) => _json("POST", "/h3chain/lib_collection_delete", { dir, id }),
    libDelete: (dir, ids) => _json("POST", "/h3chain/lib_delete", { dir, ids }),
    libStage: (dir, id) => _json("POST", "/h3chain/lib_stage", { dir, id }),
    libZip: (dir, ids) => _json("POST", "/h3chain/lib_zip", { dir, ids }),
    libRef: (dir, id, seg) => _json("POST", "/h3chain/lib_ref", { dir, id, seg }),
    libRole: (dir, id, role) => _json("POST", "/h3chain/lib_role", { dir, id, role }),
    isConflict,
    isBusy,
    errText,
  };

  if (typeof window !== "undefined") window.H3Api = Api;
})();
