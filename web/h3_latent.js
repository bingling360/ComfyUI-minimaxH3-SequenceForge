/* H3 latent/剪辑前端 helper（M4）：切片 + 删除 + 入出点裁剪 + 合并。
 * 只挂 window.H3Latent，不注册入口。三源：自动存档 .pt / 段范围切片 / 现抽节点。
 */
(function () {
  "use strict";

  function needApi() {
    if (!window.H3Api) throw new Error("H3Api 未加载");
    return window.H3Api;
  }

  async function slice(dir, src, start_f, end_f, save_name, base_revision) {
    const api = needApi();
    const res = await api.latentSlice(dir, src, start_f, end_f, save_name, base_revision);
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

  if (typeof window !== "undefined") {
    window.H3Latent = { slice, remove, trim, merge };
  }
})();
