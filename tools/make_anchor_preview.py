"""生成锚定面板 / 素材库挑选模式的**离线预览页**（自包含单文件 HTML）。

为什么要它：本机没有 ComfyUI 运行环境，锚定面板这类 UI 改动只能靠
`node --check`（只验语法）+ 运行时探针（验"渲染期是否抛错"）来兜，
**看不到长什么样**。而 `node --check` 查不出的那类故障（跨模块裸名、数据陈旧）
恰恰最容易在视觉上暴露，所以交付前得有一份能直接打开的页面。

它真加载 web/ 下那几个模块（不是复制一份改改），只把后端接口换成本地桩：
    python tools/make_anchor_preview.py  ->  tools/anchor_preview.html
生成物里的 JS 与样式都是从仓库文件里读出来的，**改完源码要重新生成**。

预览页里做的事：
  1. 段卡「设置」页里那块「📌 手动锚定」的真实渲染（源轨 / 目标轨 / 体检）
  2. 点「选择素材」弹出的素材库挑选模式（真 H3Lib，桩 lib_list）
  3. 「＋ 新增锚定」当场出一张卡
不能替代的：真 ComfyUI 里的写回链路（setSegmentField / 保存 / 生成）。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")


def _read(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


def _director_style():
    """从 h3_director.js 里抠出它那段样式表（`:root` 变量 + .h3d-* 全部规则）。

    抠文本比在预览页里重写一份样式可靠：重写的那份一定会跟真界面漂移，
    而预览页存在的意义就是"看到的就是真界面"。
    """
    src = _read("h3_director.js")
    head = "style.textContent = `"
    i = src.index(head) + len(head)
    # CSS 里不含反引号，所以开头反引号之后的**第一个**反引号就是它自己的结束
    return src[i:src.index("`", i)]


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>H3 锚定面板 &amp; 素材库挑选模式 · 离线预览</title>
<style>
__DIRECTOR_CSS__
</style>
<style>
  html,body{margin:0;padding:0;background:#14171c;color:var(--h3d-bone);
    font:13px/1.55 "Microsoft YaHei UI","Segoe UI",sans-serif}
  .wrap{max-width:900px;margin:0 auto;padding:18px 16px 60px}
  .note{border:1px solid #4a4335;background:#241f16;color:#e6d5a6;border-radius:9px;
    padding:10px 12px;font-size:12px;line-height:1.7;margin-bottom:16px}
  .note b{color:#f0d98c}
  .fakecard{border:1px solid var(--h3d-line);border-radius:11px;background:#20252b;padding:12px}
  .bar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:14px 0 8px}
  .h3d-tabpane{padding:6px 0}
</style>
</head>
<body>
<div class="wrap">
  <div class="note">
    <b>离线预览（假后端）</b>——真加载 <code>web/h3d_anchor.js</code> 与
    <code>web/h3_library.js</code>，只把 <code>/h3chain/*</code> 换成本地桩，所以
    「看到的就是真界面」。<br>
    能看：① 源轨 ② 目标轨 ③ 源选择+体检、档位「更多」、新增锚定的当场重建、
    素材库挑选模式（点「选择素材」）。<br>
    看不到：真 ComfyUI 里的写回 / 保存 / 生成链路——那要在 G 盘机器上跑。
  </div>
  <div class="fakecard">
    <div class="h3d-tabs" style="display:flex;gap:6px;margin-bottom:10px">
      <button class="h3d-tab on" type="button">主提示词</button>
      <button class="h3d-tab" type="button">具象化</button>
      <button class="h3d-tab on" type="button">设置</button>
    </div>
    <div class="bar">
      <span class="h3d-secs-hint">裸锚定面板（不套「设置」页两区外壳，外壳在 h3_director.js 里）</span>
    </div>
    <div id="pane" class="h3d-tabpane"></div>
  </div>
</div>
<script>__API_JS__</script>
<script>__ANCHOR_JS__</script>
<script>__LIBRARY_JS__</script>
<script>
/* ---------------- 后端桩 ---------------- */
const SPEC = {ok:true, frame_per_token:[1,4,4,4,4], frame_rescale:5/3,
  snap_windows:[5,22,39,56,73,90,107,124,141,158,175,192,209,226,243,260,277,294,311,328,345,362],
  min_window_frames:5, max_window_frames:362, at_modes:["head","mid","tail"],
  src_kinds:["prev_tail","segment","library","video","image","audio"], branches:["both","video","audio"]};

const SOURCES = {ok:true, seg_length:124, parse:{}, sources:[
  {slot:0,kind:"prev_tail",ref:"",label:"上段尾（段 1）",frames:124,w:1280,h:720,fps:24,sheet:null,meta_ok:true},
  {slot:0,kind:"segment",ref:"seg_000",label:"段 1",frames:124,w:1280,h:720,fps:24,sheet:null,meta_ok:true},
  {slot:1,kind:"segment",ref:"seg_001",label:"段 2",frames:158,w:1280,h:720,fps:24,sheet:null,meta_ok:true},
  {slot:null,kind:"library",ref:"latent/head.pt",label:"head.pt",frames:39,tokens:9,w:1280,h:720,fps:null,
   sheet:"latent/head.sheet.png",meta_ok:true},
  // 音频源：时长 10s 按 24fps 折成 240 等效帧（与窗宽同一刻度），无宽高/帧率
  {slot:null,kind:"audio",ref:"as_bgm0001",label:"bgm.wav",frames:240,w:null,h:null,fps:null,
   sheet:null,meta_ok:true,duration:10.0,item_id:"global:audio/bgm.wav"},
]};

const LIB_ITEMS = [
  {id:"project:assets/char_a.png", scope:"project", kind:"image", name:"角色1", file:"assets/char_a.png",
   asset_id:"", size:820000, mtime:Date.now()/1000, rating:4, tags:["主角"], roles:["首帧图"], refs:[]},
  {id:"global:images/9f3_photo.png", scope:"global", kind:"image", name:"photo.png", file:"images/9f3_photo.png",
   asset_id:"as_9f3c1d2e", size:1500000, mtime:Date.now()/1000-3600, rating:3, tags:[], roles:[], refs:[]},
  {id:"project:assets/ref_clip.mp4", scope:"project", kind:"video", name:"参考片段", file:"assets/ref_clip.mp4",
   asset_id:"", size:9200000, mtime:Date.now()/1000-7200, rating:0, tags:[], roles:[], refs:[]},
  {id:"latent:latent/head.pt", scope:"latent", kind:"latent", name:"head.pt", file:"latent/head.pt",
   asset_id:"", size:2200000, mtime:Date.now()/1000-600, rating:0, tags:[], roles:[], refs:[]},
  {id:"finals:videos/seg_000.mp4", scope:"finals", kind:"video", name:"seg_000.mp4", file:"videos/seg_000.mp4",
   asset_id:"", size:12000000, mtime:Date.now()/1000-300, rating:0, tags:[], roles:[], refs:[]},
  // 音频条目：放 global，避免干扰冒烟里"项目资产 scope 2 条"的断言
  {id:"global:audio/bgm.wav", scope:"global", kind:"audio", name:"bgm.wav", file:"audio/bgm.wav",
   asset_id:"as_bgm0001", size:3200000, mtime:Date.now()/1000-120, rating:0, tags:[], roles:[], refs:[]},
];
const KIND_OF_NAME = {"角色1":"image","photo.png":"image","参考片段":"video","head.pt":"latent","seg_000.mp4":"video","bgm.wav":"audio"};
const ITEM_BY_ID = {};
LIB_ITEMS.forEach(i => ITEM_BY_ID[i.id] = i);

function libList(qs){
  const p = new URLSearchParams(qs);
  const scope = p.get("scope") || "all", kind = p.get("kind") || "all";
  let rows = LIB_ITEMS.filter(i => (scope==="all" || i.scope===scope)
    && (kind==="all" || i.kind===kind || (kind==="media" && i.kind!=="latent")));
  const kw = (p.get("q")||"").toLowerCase();
  if (kw) rows = rows.filter(i => (i.name+i.file+(i.tags||[]).join("")).toLowerCase().includes(kw));
  return {ok:true, data:{total:rows.length, page:1, page_size:60, total_pages:1, items:rows},
    counters:{project:3, global:1, finals:1, latent:1}};
}

function route(path){
  const p = String(path);
  if (p.includes("grid_spec")) return SPEC;
  if (p.includes("anchor_sources")) {
    // 与真后端同口径：ref= 可多个（面板把已有锚的 ref 一起带来合并元信息），
    // 找不到的不整体报错、进 missing。
    const qs = p.split("?")[1] || "";
    const ids = new URLSearchParams(qs).getAll("ref").map(decodeURIComponent).filter(Boolean);
    if (ids.length) {
      const extra = [];
      for (const id of ids) {
        const it = ITEM_BY_ID[id];
        if (!it) continue;
        const kind = it.kind === "latent" ? "library" : it.kind;
        const meta = it.kind === "latent"
          ? {frames:39, tokens:9, w:1280, h:720, fps:null, sheet:"latent/head.sheet.png", meta_ok:true}
          : it.kind === "video"
            ? {frames:198, tokens:null, w:1920, h:1080, fps:30, sheet:null, meta_ok:true}
            : it.kind === "audio"
              ? {frames:null, tokens:null, w:null, h:null, fps:null, sheet:null, meta_ok:true}
              : {frames:1, tokens:null, w:1024, h:1024, fps:null, sheet:null, meta_ok:true};
        const ref = it.asset_id || it.name;
        extra.push(Object.assign({
          slot:null, kind:kind, ref:ref, label:it.name, item_id:id,
          resolvable: !!it.asset_id || (it.scope==="project" && it.name==="角色1"),
        }, meta));
      }
      return {ok:true, sources: SOURCES.sources.concat(extra),
        missing: ids.filter(i => !ITEM_BY_ID[i])};
    }
    return SOURCES;
  }
  if (p.includes("lib_list")) return libList(p.split("?")[1]||"");
  if (p.includes("lib_collections")) return {ok:true, collections:[]};
  if (p.includes("lib_status")) return {ok:true, counters:{project:3,global:1,finals:1,latent:1}, total:5, collections:[]};
  if (p.includes("anchor_sheet_build")) return {ok:true, sheet:"latent/head.sheet.png", cached:false};
  if (p.includes("ping")) return {ok:true};
  return {ok:true};
}

/* h3d_anchor.js 的 _raw 与 h3_api.js 的 _call 都优先走 comfyAPI.api.api.fetchApi */
window.comfyAPI = {api:{api:{fetchApi: async (path) => {
  const body = route(path);
  return {ok: body.ok !== false, status: body.ok === false ? 404 : 200, json: async () => body};
}}}};
/* 图片类 URL（<img src>）由浏览器直接请求 → 必然 404 → 代码里 onerror 会摘掉，
   预览页不显示缩略图属预期，不是 bug。 */

/* ---------------- 造一个段卡上下文 ---------------- */
const ds = {segments:[{seconds:6.5, anchors:[
  {id:"ax_seg", src:{kind:"segment", ref:"seg_000", start_f:0, end_f:22, src_fps:24, meta_ok:true},
   at:{mode:"head", frame_idx:0}, window:22, branches:{av:"both"}, on:true},
  {id:"ax_lib", src:{kind:"library", ref:"latent/head.pt", start_f:0, end_f:39, src_fps:null, meta_ok:true},
   at:{mode:"mid", frame_idx:40}, window:39, branches:{av:"both"}, on:true},
  {id:"ax_tail", src:{kind:"prev_tail", ref:"", start_f:null, end_f:null, src_fps:null, meta_ok:true},
   at:{mode:"tail", frame_idx:0}, window:1, branches:{av:"video"}, on:true},
  {id:"ax_aud", src:{kind:"audio", ref:"as_bgm0001", start_f:0, end_f:22, src_fps:null, meta_ok:true},
   at:{mode:"head", frame_idx:0}, window:22, branches:{av:"audio"}, on:true},
]}]};

function segmentFrames(sec){           // 与 h3_director.segmentFrames 同公式（就近 17k+5）
  const f = Math.max(5, Math.round(Number(sec) * 24));
  return 17 * Math.max(0, Math.round((f - 5) / 17)) + 5;
}
const ctx = {
  node:{}, data:{ds, mf:{params:{width:1280, height:720, length:124}}}, idx:0,
  refresh: () => console.log("[preview] 宿主 refresh（只该在兜底路径）"),
  dir: () => "preview_project",
  setAnchors: (arr) => { ds.segments[0].anchors = arr; },
  frameLen: () => segmentFrames(ds.segments[0].seconds),
};
const box = window.H3Anchor.buildAnchorPanel(ctx);
box.open = true;                       // 预览页默认展开（真界面里默认折叠）
document.getElementById("pane").append(box);
</script>
</body>
</html>
"""


def main():
    out = HTML
    out = out.replace("__DIRECTOR_CSS__", _director_style())
    # h3_api.js 是素材库的硬依赖（h3_library.js 的 api() 直接取 window.H3Api）；
    # 锚定面板刻意不走它（自己拼 URL，少一条隐式契约）。
    out = out.replace("__API_JS__", _read("h3_api.js"))
    out = out.replace("__ANCHOR_JS__", _read("h3d_anchor.js"))
    out = out.replace("__LIBRARY_JS__", _read("h3_library.js"))
    dst = os.path.join(ROOT, "tools", "anchor_preview.html")
    with open(dst, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"写出 {dst}（{len(out)} 字节）")


if __name__ == "__main__":
    main()
