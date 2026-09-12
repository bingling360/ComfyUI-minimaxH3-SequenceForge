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
    createProject: (dir) => _json("POST", "/h3chain/create_project", { dir }),
    savePrompts: (dir, prompts, segments, base_revision) =>
      _json("POST", "/h3chain/save_prompts", { dir, prompts, segments, base_revision }),
    compilePreview: (payload) => _json("POST", "/h3chain/compile", payload),
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
    assetUnlink: (payload) => _json("POST", "/h3chain/asset_unlink", payload),
    assetMirror: (payload) => _json("POST", "/h3chain/asset_mirror", payload),
    vaeFiles: () => _call("/h3chain/vae_files"),
    getPromptRules: () => _call("/h3chain/prompt-rules"),
    getOptimizerConfig: () => _call("/h3chain/optimizer-config"),
    optimize: (payload) => _json("POST", "/h3chain/optimize", payload),
    expand: (payload) => _json("POST", "/h3chain/expand", payload),
    expandValidate: (payload) => _json("POST", "/h3chain/expand_validate", payload),
    /* ---- 素材库（Library）：一个浏览器 + 四个 scope ---- */
    libList: (p) => _call("/h3chain/lib_list?" + _qs(p)),
    libItem: (p) => _call("/h3chain/lib_item?" + _qs(p)),
    libStatus: (p) => _call("/h3chain/lib_status?" + _qs(p)),
    libCollections: (dir) => _call("/h3chain/lib_collections?" + _qs({ dir })),
    libThumbUrl: (dir, id) =>
      "/h3chain/lib_thumb?" + _qs({ dir, id }),
    libRawUrl: (dir, id) => "/h3chain/lib_raw?" + _qs({ dir, id }),
    libZipUrl: (dir, name) => "/h3chain/lib_zip_file?" + _qs({ dir, name }),
    libScan: (dir) => _json("POST", "/h3chain/lib_scan", { dir }),
    libRate: (dir, id, rating) => _json("POST", "/h3chain/lib_rate", { dir, id, rating }),
    libTag: (dir, id, tags) => _json("POST", "/h3chain/lib_tag", { dir, id, tags }),
    libAlias: (dir, id, alias) => _json("POST", "/h3chain/lib_alias", { dir, id, alias }),
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
