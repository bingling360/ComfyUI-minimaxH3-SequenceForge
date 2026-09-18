/* H3 资产库前端 helper（M4）：资产增删改查 + 校验早爆 + 存盘（带 revision）。
 * 只挂 window.H3Assets，不注册入口。总量不限；单段 9/3/3 在段引用处卡（执行期同口径）。
 */
(function () {
  "use strict";

  const CAPS = { image: 9, video: 3, audio: 3 };

  function cleanAsset(raw) {
    if (!raw || typeof raw !== "object") return null;
    const kind = ["image", "video", "audio"].includes(raw.kind) ? raw.kind : "image";
    /* label 不截断：历史上截到 24 字符会把
     *   "ChatGPT_Image_2026年9月18日_14_02_12.png"
     * 变成
     *   "ChatGPT_Image_2026年9月18日"
     * 后果：
     *   ① 绿框/引用条 chip 不显示后缀
     *   ② 两个只差尾部的相似素材名字完全相同，无法区分
     *   ③ 前缀匹配把所有 @引用 解析成同一个 label → 引用条只点亮 1 个
     * 显示截断交给 shortLabel 处理（超长才首尾省略）；数据本身要完整。 */
    const label = String(raw.label || "").trim();
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

  /* ---- P3：ID 感知 + 直传 + 轮询（旧导出全部保留） ---- */

  function assetId(a) {
    return (a && typeof a.asset_id === "string") ? a.asset_id : "";
  }

  function poolAliases(pool) {
    return (pool || []).map((a) => String(a?.label || "")).filter(Boolean);
  }

  // 全池引用关系：label -> [段号...]（refs 勾选 + [[标签]] 文本 + tail_src + 首尾帧标注）
  // refs 内 asset_id 归一为别名（与后端 compile_refs 同口径，徽标才能对上瓦片）
  function segUsage(ds) {
    const usage = {};
    const id2label = {};
    for (const a of (ds && ds.ref_assets) || []) {
      if (a?.asset_id && a?.label) id2label[a.asset_id] = a.label;
    }
    const touch = (key, segNo) => {
      let l = String(key || "").trim();
      if (!l) return;
      if (id2label[l]) l = id2label[l];
      if (!usage[l]) usage[l] = [];
      if (!usage[l].includes(segNo)) usage[l].push(segNo);
    };
    const prompts = (ds && ds.prompts) || [];
    const segs = (ds && ds.segments) || [];
    const n = Math.max(prompts.length, segs.length);
    for (let i = 0; i < n; i++) {
      const seg = segs[i] || {};
      for (const r of seg.refs || []) {
        const k = (r && typeof r === "object") ? (r.asset || r.id || r.label) : r;
        touch(k, i + 1);
      }
      const txt = String(prompts[i] || "");
      const re = /\[\[([^\[\]]{1,24})\]\]/g;
      let m;
      while ((m = re.exec(txt))) touch(m[1].trim(), i + 1);
      if (seg.tail_src && seg.tail_src.asset) touch(seg.tail_src.asset, i + 1);
    }
    const pool = (ds && ds.ref_assets) || [];
    for (const a of pool) {
      for (const role of a?.roles || []) {
        if (role === "首帧图" || role === "尾帧图") {
          for (let i = 0; i < n; i++) touch(a.label, i + 1);
        }
      }
    }
    return usage;
  }

  // 干跑编译取某段真实 tag（所见即所得），失败抛错（调用方展示 message）
  async function compileTags(dir, refs, segNo) {
    const H3Api = window.H3Api;
    if (!H3Api) throw new Error("H3Api 未加载");
    if (!H3Api.compileRefs) throw new Error("H3Api.compileRefs 不可用（请更新插件）");
    const res = await H3Api.compileRefs({ dir, refs: refs || [] });
    if (!res.body?.ok) throw new Error(H3Api.errText(res, "引用编译失败"));
    return res.body;
  }

  // 上传即入库（单次往返）：file + kind + dest/link_dir/alias → {entry, manifest, alias}
  // dest: global（只进全局库）| project（+ 项目 assets/ 并登记）| finals（+ 项目 finals/）
  async function uploadDirect(file, opts) {
    const H3Api = window.H3Api;
    if (!H3Api) throw new Error("H3Api 未加载");
    if (!H3Api.libraryUpload) throw new Error("H3Api.libraryUpload 不可用（请更新插件）");
    const o = opts || {};
    const fd = new FormData();
    fd.append("file", file, file.name || "upload.bin");
    if (o.kind) fd.append("kind", o.kind);
    if (o.tags) fd.append("tags", o.tags);
    if (o.desc) fd.append("desc", o.desc);
    if (o.dest) fd.append("dest", o.dest);
    if (o.link_dir) fd.append("link_dir", o.link_dir);
    if (o.alias) fd.append("alias", o.alias);
    if (o.mirror) fd.append("mirror", "1");
    const res = await H3Api.libraryUpload(fd);
    if (!res.body?.ok) throw new Error(H3Api.errText(res, "上传入库失败"));
    return res.body;
  }

  function guessKind(file) {
    const t = String(file?.type || "");
    if (t.startsWith("video/")) return "video";
    if (t.startsWith("audio/")) return "audio";
    return "image";
  }

  if (typeof window !== "undefined") {
    window.H3Assets = { CAPS, cleanAsset, dedupe, checkSegRefs, save, check,
      assetId, poolAliases, segUsage, compileTags, uploadDirect, guessKind };
  }
})();
