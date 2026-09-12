# 三库重构方案（蓝本：Majoor Assets Manager）

日期：2026-09-12　分支：`experimental-branch-2`

> 用户原话：「根据现有成熟 github 的资产管理，对我的三库进行重构，
> 不需要考虑保留哪部分；对目前现有的三库，唯一的作用就是用来理解我的目的。」
>
> 即：**现有三库只当需求说明书用，实现全部换掉。**

---

## 0. 一句话诊断

现在三库的问题不是"某处写坏了"，而是**根本没按资产管理的成熟模型设计**：

| 缺什么 | 现状 | 后果 |
|---|---|---|
| 索引层 | 每次打开现扫目录、现读 manifest | 素材一多就卡；无法跨项目搜索 |
| 分页 / 虚拟滚动 | 全量渲染 DOM | 瓦片多就卡死 |
| 搜索 / 过滤 / 排序 | 完全没有 | 只能靠肉眼找 |
| 批量操作 | 只能一个一个点 | 无法多选 |
| 通用能力 | 重命名/删除/复制路径/下载/打包 全缺 | 只能用系统文件管理器 |
| 动作承载方式 | **每个动作都做成瓦片上的按钮** | 一个瓦片 7~8 个按钮 → 布局必然挤压（上次修的"展开看不到"就是这个病根） |

最后一行是关键：**按钮堆在瓦片上**这个设计本身就是错的——
成熟 DAM 的做法是「瓦片只放缩略图 + 徽标，**动作全部收进右键菜单 / 预览器**」。

---

## 1. 蓝本：Majoor Assets Manager

为什么选它：131★、活跃维护（v2.5.1，最近仍在更新）、架构完整、有 API 文档。

### 1.1 它有什么

| 能力 | 细节 |
|---|---|
| 浏览范围（scopes） | `Outputs` / `Inputs` / `Custom roots` / `Collections` |
| 索引 | 扫目录 → **SQLite**（`<output>/_mjr_index/assets.sqlite`）+ FTS 全文索引；exiftool / ffprobe 提元数据；增量扫描 + 实时追踪新产物 |
| 搜索 | 全文（文件名 / prompt / 元数据）+ 结构化过滤（类型 / 评分 / 有无工作流 / 日期 / 大小 / 分辨率）+ 排序 + 分页 |
| 组织 | rating(0-5) / tags / collections（独立 JSON）/ 重复检测 |
| 查看 | 虚拟滚动网格；双击打开详情（媒体 + prompt + workflow 元数据）；Floating Viewer（A/B 对比、pins） |
| 复用 | **拖到 loader 节点或画布**；`Load Asset` 直接建 loader 节点；**`stage to input`**（复制进 ComfyUI input 目录） |
| 导出 | 单文件下载 / 批量 ZIP / `Collect Files`（资产 + workflow + prompt + 引用媒体打包） |
| 文件操作 | 重命名 / 删除 / 批量删除 / 移动 / 右键菜单 / 复制路径 / 打开文件夹 |

### 1.2 它的 API（Base `/mjr/am`，可直接抄形状）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/list` | 分页列表（无全文搜索，浏览用） |
| GET | `/search` | 全文 + 多维过滤 + 排序 + 分页 |
| GET | `/asset/{asset_id}` | 单资产详情（含缩略图 URL、评分、标签、完整元数据） |
| GET | `/thumbnail/{asset_id}?size=small\|medium\|large` | 缩略图流 |
| POST | `/scan` | 触发扫描（`incremental` / `force`），后台跑，返回 `scan_id` |
| POST | `/index-files` | 实时追踪：把刚生成的文件列表塞进索引 |
| GET | `/health/counters` | 各 scope 的索引计数 + 最后扫描时间 |
| POST | `/asset/rating` · `/asset/tags` | 写评分 / 标签 |
| GET | `/collections` · POST `/collections/create\|update\|delete` | 集合增删改查 |
| POST | `/asset/rename` · `/asset/delete` · `/assets/delete` | 文件操作（安全模式需环境开关） |
| POST | `/stage-to-input` | 复制/软链到 input 目录，返回 `staged_path` |
| POST | `/batch-zip` + GET `/batch-zip/{token}` | 批量打包下载 |
| GET | `/download` · GET `/metadata` · GET `/viewer/info` | 下载 / 元数据 / 查看器信息 |

**分页契约**（统一）：
```json
{ "ok": true, "data": { "total": 150, "page": 1, "page_size": 50, "total_pages": 3, "items": [ ... ] } }
```

### 1.3 它**不适合**直接照搬的地方

- 前端是 **Vue 3 + Pinia + TypeScript + 构建步骤**；本项目 `web/` 是 ComfyUI 直接加载的裸 JS，**没有构建链**。
- 后端是 **SQLite + FTS + schema migration**；本项目现有资产后端是基于 manifest JSON 的（`asset_store.py`）。
- 它是**通用 DAM**，不认识 H3 的"标签 → `<Picture N>`""首帧图/尾帧图""latent 桥"语义。

→ 结论：**抄它的 API 形状、交互模型、功能清单；实现用本项目现有的技术栈。**

---

## 2. 需求说明书：现有三库到底要干什么

（这部分就是"理解你的目的"，重构后这些**诉求**要全部满足，但实现方式换掉）

### 2.1 资产库 —— 参考素材

用户要做的事：
1. 把手上的参考图 / 视频 / 音频放进库（拖放 / 上传）
2. 给它起个**短别名**（如 `女主` / `雪山`），用于在提示词里 `[[女主]]`
3. 在提示词里引用：别名 → 后端压实成 `<Picture 1>` / `<Video 2>` / `<Audio 1>`
4. 打标 **首帧图 / 尾帧图**（全库各只留一张）
5. 看"这个素材被哪几段引用了"（段引用徽标）
6. 区分 **全局库**（跨项目复用）与 **项目资产**（随项目走，可调入项目）

### 2.2 成片库 —— 生成产物

1. 看本项目的产物：分段视频 / 完整成片 / 合并导出 / 剪辑片段
2. 播放预览
3. 挑一段**调进资产库**（作为下一链的参考素材）

### 2.3 latent 库 —— 中间态

1. 看已登记的 latent（`av` 图像+音频 / `video` 仅图像 / `audio` 仅音频）
2. **切片**（帧窗 → 新 latent）
3. **一键二采放大**（驱动画布的 `H3LatentUpscale`）

### 2.4 三库共同点（这才该抽出来）

三个库其实是**同一个东西的三种过滤视角**：

```
一个资产浏览器
  ├── scope: 项目资产   (output/h3_projects/<proj>/assets/)
  ├── scope: 全局库     (user/minimax_h3/library/)
  ├── scope: 成片       (output/h3_projects/<proj>/ 下的 videos/finals/merges/clips)
  └── scope: latent     (output/h3_projects/<proj>/latents/)
```

→ **重构的核心：三个手搓面板 → 一个浏览器 + 四个 scope。**

---

## 3. 目标架构

```
┌─ 导演台侧边栏 tab「素材」────────────────────────────┐
│ [项目资产] [全局库] [成片] [latent] [收藏]   ← scope   │
│ [🔍 搜索……………………] [类型▾] [日期▾] [排序▾] [☑多选]     │
│ ┌────────────────────────────────────────────────┐ │
│ │  虚拟滚动网格：缩略图 + 名称 + 徽标(段1/首帧图)   │ │
│ │  右键 → 动作菜单                                 │ │
│ └────────────────────────────────────────────────┘ │
│ 第 1/3 页 · 共 150 项 · 已选 3 项   [上一页][下一页]   │
└──────────────────────────────────────────────────────┘
        │ 双击
        ▼
┌─ 预览器（全屏浮层）─────────────────────────────────┐
│  媒体播放/大图  │  元数据：类型/大小/尺寸/时长/来源   │
│                │  本插件语义：引用段列表 / 首尾帧标记 │
│  动作：引用到段 / 打标 / 调入项目 / stage to input /  │
│        重命名 / 删除 / 复制路径 / 下载                │
└──────────────────────────────────────────────────────┘
```

**动作搬家的意义**：瓦片不再堆按钮 → 布局问题从根上消失，再也不会"展开后看不到"。

---

## 4. 后端 API 设计（抄 Majoor 形状，前缀换 `/h3chain/lib`）

| 方法 | 路径 | 说明 | 复用现有 |
|---|---|---|---|
| GET | `/h3chain/lib/list` | 分页列表 | 新 |
| GET | `/h3chain/lib/search` | `q` / `scope` / `kind` / `ref_seg` / `role` / 日期 / 排序 / 分页 | 新 |
| GET | `/h3chain/lib/asset` | 单资产详情（含引用段、角色标记） | 新 |
| GET | `/h3chain/lib/thumb` | 缩略图（服务端生成 + 磁盘缓存） | 新 |
| POST | `/h3chain/lib/scan` | 触发索引重建（项目 / 全局库 / latent） | 新 |
| GET | `/h3chain/lib/status` | 索引计数 + 最后扫描时间 | 新 |
| POST | `/h3chain/lib/rename` | 重命名（改别名 / 改文件） | 部分有 |
| POST | `/h3chain/lib/delete` | 删除（单个 / 批量） | 新 |
| POST | `/h3chain/lib/stage` | 复制/软链到 ComfyUI `input/` | **新（解决上次遗留的「在画布打开」）** |
| POST | `/h3chain/lib/zip` | 批量打包下载 | 新 |
| POST | `/h3chain/lib/collections/*` | 收藏集增删改查 | 新 |
| POST | `/h3chain/lib/rating` · `/tags` | 评分 / 标签 | 新 |
| POST | `/h3chain/lib/ref` | 引用到段（写段 refs） | 复用 `compile_refs` |
| POST | `/h3chain/lib/role` | 首帧图 / 尾帧图打标 | 复用现逻辑 |

**保留不动**（已成熟）：`asset_store.py` 的内容寻址全局库、
项目链接（asset_links）、`compile_refs`（别名 → `<Picture N>`）、三级寻址。

---

## 5. 技术选型（我的推荐 + 理由）

| 项 | 推荐 | 理由 |
|---|---|---|
| 前端框架 | **不引入 Vue**，保持裸 JS | 项目 `web/` 无构建链，引入 Vue 要加 CDN 依赖或构建步骤，违背"集成进节点"；虚拟滚动自己写约 150 行，可控 |
| 索引存储 | **先用内存索引 + JSON sidecar**，不引 SQLite | 长链项目的素材量级（几十~几百）用不上 FTS；API 形状与 Majoor 一致，将来量大可换 SQLite 实现而不改接口 |
| 缩略图 | 服务端生成小图 + 磁盘缓存（Pillow / ffmpeg 按需） | 视频/大图直接铺网格会爆内存 |
| 元数据 | 复用现有 manifest + 全局库 manifest，补 `rating/tags/collections` | 不新造一套 |
| 分页 | 服务端 `page/page_size/total`，前端滚到底加载下一页 | 与 Majoor 同契约 |

---

## 6. 分批实施（每批可独立验收）

| 批次 | 内容 | 验收 |
|---|---|---|
| **1** | 后端索引 + `list` / `search` / `status` API；前端新浏览器骨架（scope 切换 + 搜索栏 + 虚拟网格 + 分页） | 打开面板能列出四类素材、能搜、能翻页；1000 条不卡 |
| **2** | 缩略图服务 + 预览器浮层（媒体 + 元数据） | 双击出预览，视频可播 |
| **3** | 右键菜单 + 批量多选：重命名 / 删除 / 复制路径 / 下载 / ZIP | 右键可控，多选可批量 |
| **4** | **本插件语义接线**：引用到段、首帧/尾帧打标、段引用徽标、调入项目 / 入库 | 老功能全部回来，且入口在右键/预览器 |
| **5** | `stage to input`（补上「在画布打开」）+ latent 桥注入、切片、二采入口 | 素材一键进 input，可用原生节点 |
| **6** | 收藏集 + 评分 + 标签 | 可建集合、打星、打标签 |

**批次 1 做完就能看到质变**（三库合一 + 能搜能翻页），建议先做 1、2 再评估。

---

## 7. 需要你拍板的点

1. **前端**：确认**不引入 Vue**、保持裸 JS？（我推荐这样；若你想上 Vue，我需要加构建步骤或 CDN）
2. **索引**：确认**先不上 SQLite**、用内存索引 + JSON？（我推荐这样，接口留好将来可换）
3. **三库合并**：确认把三个面板合并成**一个浏览器 + scope 切换**？（这是重构的核心动作）
4. **现有三库页面**：确认**整个删掉重写**（不是改造）？
5. **成片库的两个按钮（裁剪 / 音画分离）**：一起退掉，还是留到批次 5 用 `stage to input` + 原生节点替代？

---

## 8. 实施结果（2026-09-12 已完成）

三个手搓面板已整体替换为**一个资产浏览器 + 四个 scope**，六批一次做完。

### 后端
| 文件 | 内容 |
|---|---|
| `library.py`（新增） | 索引层：四 scope 扫描、查询/过滤/排序/分页（Majoor 同契约）、关键词搜索、元数据 sidecar（rating·tags·collections）、别名与角色合入、段引用计算、缩略图、stage-to-input、重命名/删除/打包 ZIP |
| `routes.py` | `GET /h3chain/lib_list｜lib_item｜lib_thumb｜lib_raw｜lib_status｜lib_collections｜lib_zip_file`；`POST /h3chain/lib_scan｜lib_rate｜lib_tag｜lib_alias｜lib_collection_save｜lib_collection_delete｜lib_delete｜lib_stage｜lib_zip｜lib_ref｜lib_role`（18 个） |

复用既有 `asset_store.py`（内容寻址全局库 / 项目链接 / `compile_refs`），没有重造。

### 前端
| 文件 | 变化 |
|---|---|
| `web/h3_library.js` | **新增**。浏览器本体：scope 切换 + 搜索 + 类型/排序/正倒序 + 多选 + 分页（滚到底自动加载）+ 懒加载网格 + 右键动作菜单 + 预览器（媒体 / 元数据 / 评分 / 动作） |
| `web/h3_director.js` | **删掉 3.7 万字符旧实现**：`openLibraryHub`/`renderLibAssets`/`renderLibFinals`/`renderLibLatent`/`openTrimSub`/`mergeServerPool`/`kindCol`/`setAssetRoles`/`uploadPoolFile`/`importPoolFile`/`uploadLibFile`/`mirrorGlobalEntry`/`upgradeTileTags`/`migrateLegacyAnchors` 及对应 CSS；入口改为一键 `window.H3Lib.open({dir, seg, onChanged})`；暴露 `window.H3Director.upscaleLatent` 供 latent 二采 |
| `web/h3_api.js` | 新增 17 个 `lib*` 接口封装 |

### 关键设计（照抄 Majoor 的那一条）
> **瓦片只放缩略图 + 徽标，所有动作收进右键菜单 / 预览器。**

布局挤压（"展开后功能选项看不到"）从根上不再可能发生 —— 瓦片上不再堆按钮。

### 动作清单（右键菜单 / 预览器）
预览 · 引用到段 · 标首帧图/尾帧图 · 评分 · 标签 · 重命名 · 调入项目 · 调入资产库 ·
二采放大（latent） · 复制路径 · 下载 · 打包 ZIP · 暂存到 input · 删除（多选批量）

### 验证
- `node --check`：`h3_director.js` / `h3_library.js` / `h3_api.js` 全通过
- `py_compile`：`library.py` / `routes.py` 通过
- 新增 `tests/test_library.py`：扫描 / 分页契约 / 过滤 / 搜索 / 排序 / sidecar / 集合 / 别名 / 段引用 / 角色 / 删除 / 重命名 / 打包 / 寻址
- 回归：**107 passed / 2 failed**（2 个是 `test_transcode_p1.py` 缺 torch 的环境问题，与本次改动无关）

### 顺带修掉的真 bug
索引初建的显示名是**文件名**，而用户在提示词里写的是 `[[别名]]` ——
不覆盖会导致段引用徽标全部匹配不上。新增 `apply_aliases()`，用
`manifest.assets[].label` / `asset_links[].alias` 覆盖 `name`。

### 已知边界
- 缩略图只对图片生效（视频/音频没有 ffmpeg 抽帧；网格里视频用 `<video preload=metadata>` 解首帧，音频显图标）
- 未引入 SQLite：索引在内存 + TTL 缓存；查询契约与 Majoor 一致，将来量大可换实现而不改接口
- 「在画布打开」改为**暂存到 input**（原生 LoadImage/LoadVideo 只读 `input/`），由用户自己连线
- 后端 `latent_tools.py` / `transcode_queue.py` 的独立能力保留未动

---

## 9. 首轮试用反馈修复（2026-09-12）

用户反馈：前端风格对了，但**界面被导演台盖住、「调入项目」没反应、预览器按钮像失效、下载直接打开浏览器、动作语义看不懂**。

| 现象 | 根因 | 修法 |
|---|---|---|
| 素材库被导演台盖住，要叉掉导演台才看得见 | 导演台 `.h3d-page` 的 z-index 是 `1000000`，素材库只有 `9000` | 素材库 `1000005`、预览器 `1000006`、右键菜单 `1000007`、提示 `1000009` |
| 「调入项目」点了没反应 | 旧 `asset_mirror` **只拷文件不写 manifest** —— 它假设前端会再推一次池子登记（旧三库面板的 `persistPool` 有这步，新浏览器没有） | 新增 `POST /h3chain/lib_mirror` → `library.mirror_to_project()`：拷进 `assets/` **并**写 `manifest["assets"]`，一步到位 |
| 预览器里的按钮像失效 | 反馈只写浏览器底部状态行，被预览器挡住 → "点了没反应" | 新增 toast 浮层（成功绿 / 失败红），所有失败路径改走 `fail()` |
| 下载跳到浏览器打开 | `window.open(libRawUrl)` 走的是 inline | `lib_raw?download=1` 带 `Content-Disposition: attachment`；前端改用 `<a download>` |
| 「暂存到 input」看不懂 | 文案没解释 | 改「📤 复制到 input 目录（供画布 LoadImage / LoadVideo 选择）」，成功后提示去哪选 |
| 「引用到段」不知道干嘛 | 文案没说效果 | 改「🎯 作为第 N 段的参考素材」，成功后提示"会编译成 `<Picture 1>` 这类编号写进该段提示词" |
| 全局库素材引用/打标失败 | 没登记进项目清单，`compile_refs` 解析不到 | `lib_ref` / `lib_role` 对全局库条目**自动补链接**（`link_asset`）后再写 |
| 索引找不到刚上传的文件 | 索引有 3 秒 TTL 缓存 | `lib_mirror` 入口先 `invalidate` 再查 |

另外补上：
- 多选状态下可**批量调入项目**（`actMirrorMany`）
- `normalize_kind` 从 `asset_store` 复刻进 `library.py`（`mirror_to_project` 依赖它，此前漏定义）
- 全局库 vs 项目资产的关系写进上传按钮提示：上传 = ① 进全局库（跨项目复用）② 链接本项目；要让文件落进项目 `assets/` 点「调入项目」

验证：`node --check` × 3 + `py_compile` 通过；回归 **108 passed / 2 failed**（2 个为缺 torch 的环境问题）。

---

*本文档为设计方案 + 实施记录。*
