/* H3 资产库前端 helper（M4）：资产增删改查 + 校验早爆 + 存盘（带 revision）。
 * 只挂 window.H3Assets，不注册入口。总量不限；单段 9/3/3 在段引用处卡（执行期同口径）。
 */
(function () {
  "use strict";

  const CAPS = { image: 9, video: 3, audio: 3 };

  function cleanAsset(raw) {
    if (!raw || typeof raw !== "object") return null;
    const kind = ["image", "video", "audio"].includes(raw.kind) ? raw.kind : "image";
    const label = String(raw.label || "").trim().slice(0, 24);
    const file = String(raw.file || "").trim().replace(/\\/g, "/");
    if (!label || !file) return null;
    const parts = file.split("/").filter((p) => p && p !== ".");
    if (!parts.length || parts.length > 2) return null;
    if (!/\.[A-Za-z0-9]+$/.test(parts[parts.length - 1])) return null;
    return { label, kind, file: parts.join("/") };
  }

  function dedupe(assets) {
    const seen = new Set();
    const out = [];
    for (const a of assets || []) {
      const c = cleanAsset(a);
      if (!c || seen.has(c.label)) continue;
      seen.add(c.label);
      out.push(c);
    }
    return out;
  }

  // 本段引用是否超单段上限 -> 错误串（空串=通过）
  function checkSegRefs(assets, refs, segNo) {
    const byLabel = {};
    for (const a of assets || []) byLabel[a.label] = a;
    const order = [];
    for (const r of refs || []) {
      const lbl = String(r).trim();
      if (!byLabel[lbl]) return `段${segNo} 引用未知标签「${lbl}」`;
      order.push(byLabel[lbl].kind);
    }
    const count = {};
    for (const k of order) count[k] = (count[k] || 0) + 1;
    for (const k of Object.keys(CAPS)) {
      if ((count[k] || 0) > CAPS[k]) {
        const cn = { image: "图片", video: "视频", audio: "音频" }[k];
        return `段${segNo} 引用${cn}素材 ${count[k]} 个，超过单段上限 ${CAPS[k]} 个`;
      }
    }
    return "";
  }

  async function save(dir, assets, base_revision) {
    const H3Api = window.H3Api;
    if (!H3Api) throw new Error("H3Api 未加载");
    return H3Api.saveAssets(dir, dedupe(assets), base_revision);
  }

  async function check(assets, segments) {
    const H3Api = window.H3Api;
    if (!H3Api) throw new Error("H3Api 未加载");
    return H3Api.assetCheck(dedupe(assets), segments);
  }

  if (typeof window !== "undefined") {
    window.H3Assets = { CAPS, cleanAsset, dedupe, checkSegRefs, save, check };
  }
})();
