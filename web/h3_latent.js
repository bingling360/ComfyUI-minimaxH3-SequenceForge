/* H3 latent/剪辑前端 helper：切片（含av/video/audio分存） + 删除 + 入出点裁剪
 * + 库间互调 + 音画分离 + 合并 + 生成忙状态。
 * 只挂 window.H3Latent，不注册入口。两源：主节点自动存档 .pt / 段范围切片。
 * 生成中后端回 423（BUSY），前端据此置灰剪辑按钮（见 isBusy/busyText）。
 */
(function () {
  "use strict";

  function needApi() {
    if (!window.H3Api) throw new Error("H3Api 未加载");
    return window.H3Api;
  }

  async function slice(dir, src, start_f, end_f, save_name, base_revision, kind) {
    const api = needApi();
    const res = await api.latentSlice(dir, src, start_f, end_f, save_name, base_revision, kind || "av");
    if (!res.body?.ok) throw new Error(api.errText(res, "latent 切片失败"));
    return res.body;
  }

  async function remove(dir, file, base_revision) {
    const api = needApi();
    const res = await api.latentDelete(dir, file, base_revision);
    if (!res.body?.ok) throw new Error(api.errText(res, "latent 删除失败"));
    return res.body;
  }

  async function trim(dir, src, start_s, end_s, save_name, base_revision) {
    const api = needApi();
    const res = await api.trim(dir, src, start_s, end_s, save_name, base_revision);
    if (!res.body?.ok) throw new Error(api.errText(res, "裁剪失败"));
    return res.body;
  }

  async function merge(dir, items) {
    const api = needApi();
    const res = await api.merge(dir, items);
    if (!res.body?.ok) throw new Error(api.errText(res, "合并失败"));
    return res.body;
  }

  async function moveMedia(dir, src, dest, opts) {
    const api = needApi();
    const res = await api.moveMedia(dir, src, dest, opts);
    if (!res.body?.ok) throw new Error(api.errText(res, "库间移动失败"));
    return res.body;
  }

  async function splitAv(dir, src, opts) {
    const api = needApi();
    const res = await api.splitAv(dir, src, opts);
    if (!res.body?.ok) throw new Error(api.errText(res, "音画分离失败"));
    return res.body;
  }

  async function isBusy() {
    try {
      const api = needApi();
      const res = await api.busy();
      if (res.body?.ok) return !!res.body.busy;
    } catch (e) { /* 网络异常按不忙处理，靠后端 423 兜底 */ }
    return false;
  }

  function busyText() {
    return "正在生成中，剪辑/转码入口已锁定（完成后自动解锁）";
  }

  if (typeof window !== "undefined") {
    window.H3Latent = { slice, remove, trim, merge, moveMedia, splitAv, isBusy, busyText };
  }
})();
