# 交接：锚定面板 + 段卡「设置」页

日期：2026-09-16 18:55　分支：`anchor-studio`　HEAD：`1f64814`
（本地领先 `origin/anchor-studio` **11 个提交，尚未推送**）

> **新会话怎么用这份文档**：先读本文 §3（待做）与 §5（工程约束），
> 再读 `docs/手动锚定_分段latent参考规范化_实施规划.md` 的 §0 决策总表 / §3 数据结构 /
> §9 附录A 代码地图。**§4 是已锁定的设计决定，不要重新讨论。**

---

## 1. 背景一句话

把「把某段 latent / 素材钉到本段某位置」从散落七处的碎片，收敛成一个 `anchor`
数据结构 + 一套双轨时间线 UI。后端（Python）**已全部落地**；本文剩下的活**全在前端**
（外加一处后端源解析）。

---

## 2. 已完成（基线，别重做）

### 2.1 后端重构（步骤 1–8，全部完成）

| 提交 | 内容 |
|---|---|
| `93fe186` | 步骤 3/5/8 + 插入视频链路整体移除 |
| `cc93c7d` | 步骤 6 主编排切换 + 步骤 4/7 收尾 |
| `df3e14f` | 文档同步 + `projects.py` 死代码清理 |

- `anchors.py`：归一化 / 硬校验 / 旧字段迁移 / 变更区间 / 源归一化（纯函数）
- `checkpoint.assert_match` 收窄为只校验 width/height；`reroll_start` 删除，
  区间计算归 `anchors.change_intervals`，区间标进重摇通道 `redo_map`（不 truncate 级联）
- 插入视频（`ds.inserts`）整体删除；旧存档含非空 inserts **直接报错**
- 实验 `e1_bridge_shard` / `e2_memory_anchor` / `mid_anchor` 并入 anchor 后删除
- **唯一入口**：`seg.anchors[]`；`_apply_guide(cond, keyframes, sampled_fc)`
- `resolve_anchor_source()`：直接切窗 / 有源先裁后编 / 无源硬报错（带转档指引）

### 2.2 前端修复链（每一笔都是真 bug）

| 提交 | 修的什么 |
|---|---|
| `903ce87` | 跨模块按全局名调宿主函数 → 整块「段落卡片渲染失败」 |
| `afcab6b` | 帧区间乱 / 轨道叠加 / 切来源无条目 / 新增没反应 |
| `d2e5dc1` | 目标轨改可点可拖 + **改回一条线** + 去掉「中间」 |
| `d9683bb` | 视频源枚举（**方向错，待拆**）+ 帧数按分段秒数 |
| `77a608b` | 面板自拥有渲染权（不再触发宿主全量重刷） |
| `24972f1` | 设置页：latent 保存按模式显隐 + 锚定面板改上下堆叠 |
| `1f64814` | **刷新问题真凶**：数据写透 + 删掉设置页 105 行冗余控件 |

### 2.3 ★ 刷新问题的真凶（已修，但**待用户强刷确认**）

- `getDs(node)` 第一行就是 `JSON.parse(w.value)` → **每次调用都新建对象**
- `setSegmentField` 写的是它自己那个新对象，而面板手里的 `ctx.data.ds` 仍指向**旧对象**
- → 面板随后所有读取（`renderList` / 体检 / 目标轨 / `findSource`）全是**陈旧数据**
- 症状：**点了新增锚定没反应，退出导演台再进来才出现**
- 修法：`setAnchorsOf` 里写透 `ctx.data.ds.segments[ctx.idx].anchors = arr`

> ⚠️ 如果用户反馈"还是不生效"，先确认强刷（Ctrl+Shift+R）；
> 若仍不行，让用户贴 Console 报错，并**验证 `data.ds` 是否真的与 `getDs` 同源**
> —— 面板收到的 `data` 是宿主在 `buildCards` 时构造的，可能本身就是一次 `getDs` 的快照。

---

## 3. 待做（按依赖顺序，**一次做完，不要再分批交**）

### T1　来源类型收敛（前端 `web/h3d_anchor.js`）

**现状**：来源下拉有 5 类（`prev_tail` / `segment` / `library` / `video` / `image`）。

**要改成**：
- 下拉只留 **「段」/「素材」** 两类
- `prev_tail` **从 UI 去掉**——用户明确说冗余：「有上段的选择就行了」。
  选上一段即可（`src.kind="segment"` + ref 上一段）。后端 `prev_tail` **保留**为
  隐式默认（没有显式 head anchor 时的默认段首桥），只是 UI 不再暴露。
- 「素材」一类内部再按条目的 `kind` 映射到 `library` / `video` / `image`

### T2　素材选择器（前端）

**为什么**：用户明确要求「素材肯定是从**全局库或者项目库**里选的，既然都有资产库了，
还枚举干嘛」。我此前在 `anchor_sources` 里自己扫 ComfyUI input 目录枚举视频、
从 `manifest["assets"]` 枚举图片，**方向是错的**。

**做法**：用已有的资产库**服务**做一个薄选择器
- 列表：`GET /h3chain/lib_list?dir=<项目名>&kind=<image|video|latent>&scope=<project|global>`
  （已存在，自带 搜索 `q` / 筛选 / 分页 / 评分 / 别名 / `blocked`）
- 缩略图：`GET /h3chain/lib_thumb?dir=&id=`
- 选中回填：`src.ref` = **标签**（image/video）或 `latent/<文件>`（latent）

**注意**：仓库里**没有**可复用的「素材库选择器组件」——`h3_assets.js` 只是数据模块
（`window.H3Assets`，含 `CAPS`/`cleanAsset`），`h3_library.js` / `h3_director.js` 里
**没有** `openLib*/openPicker` 之类的入口。所以要么新写一个薄弹层（推荐，复用上面两个接口），
要么先给素材库模块加一个 open API 再复用。

### T3　后端：`video` / `image` 源按素材库解析（Python `nodes.py`）

**为什么**：`_resolve_anchor_latent` 现在对 `kind="video"` 调
`_load_input_video(ref, ...)`，而它走 `folder_paths` 的注释路径解析 → **只认 ComfyUI
input 目录**，项目 `assets/` 里的视频**接不进来**。`kind="image"` 已经是按**标签**
寻址（`_anchor_image`）✓，但视频没这条通路。

**要改**：
- 从 `_anchor_image` 里抽出「标签/文件 → 绝对路径」的解析（registry `by_alias`/`by_id`
  → `_AS.resolve_absolute` → 回落 `pool_file_of` / `_load_input_image`），复用到视频
- 视频：拿到绝对路径后走 `media.decode_av(path, start_f, end_f, fps)` →
  后接统一的 `_center_cover` + `video_vae.encode`

### T4　拆掉自建的枚举（`routes.py`）

删掉 `anchor_sources` 里：
- `str(q.get("kind") or "") == "video"` 那一支（扫 input 目录）
- `str(q.get("kind") or "") == "image"` 那一支（扫 `manifest["assets"]`）
- `_probe_media_meta()` 与 `_VIDEO_EXTS`

保留 `segment` / `prev_tail` / `latent` 三类（段列表仍需要它）。
T1–T4 是**一件事**，必须一起改：拆开会出现「来源选了素材但选不了 / 选了也跑不通」。

### T5　设置页收尾 + 零碎

1. **分组小标题**：「设置」页现在 `paneSet` 上平铺堆着几块、权重一样（无层级）。
   按「影响本段生成」/「只影响落盘」分两区，每块给小标题。
2. **空状态自解释**：来源/条目为空时说明**原因**（没项目存档 / latent 库空 /
   还没跑过任何段），不要只显示「（无可用条目）」。
   ⚠️ 后端 `anchor_sources` 在项目不存在时返回 404，而前端 `_getJson` 只抛
   `HTTP 404 · path`，**把真正的原因丢了**——要透传 `message`。
3. **体检「分辨率未知（置?）」**：`data.mf?.params?.width/height` 可能**根本没传进面板**，
   需要核对宿主在 `buildCards` 里构造 `data` 时给了什么。
4. **22 个档位按钮铺两行太吵**：收起成常用几档 + 「更多」。
5. **「段 / latent 库」来源依赖项目存档**：存档目录为空（自动命名、还没跑过段）时必然空，
   要明确提示。

### T6　扩守卫 + 目视验收

- `tests/test_anchor_wiring.py` 现有的 5 条守卫要跟着本次改动更新
  （来源类型、渲染所有权、跨模块裸名）
- **必须实际打开面板目视验收**——用户会亲自看。`node --check` **不够**
  （`getDirValue` 那次崩溃语法完全合法）。

---

## 4. 已锁定的设计决定（**不要重新讨论**）

| # | 决定 |
|---|---|
| 1 | **素材一律从资产库选**（全局库/项目库），不自己造枚举 |
| 2 | 来源两类：**段 / 素材**；`prev_tail` 从 UI 去掉 |
| 3 | 段卡三页里的**「设置」页**才是重构对象——**不是**整个段卡（用户明确纠正过我） |
| 4 | **窗宽只能点 17k+5 档位**（或 1=单帧锚），**位置自由**——非档位宽度会被模型折掉、后端硬报错 |
| 5 | 目标轨 = **一条线**（不是跨度框），刻度上**点/拖即定位**（自动切 mid）；不要「中间」按钮 |
| 6 | 锚定面板在段卡里**上下堆叠**（源轨 → 目标轨 → 体检） |
| 7 | **一块 UI 只能有一个渲染主人**：本地改动走本地重建，宿主 refresh 只作兜底 |
| 8 | 后端**保留** `latent_ref` / `tail_src` 的迁移（只读旧档），但 UI 入口已删——锚定面板是唯一入口 |

---

## 5. 工程约束（**必须遵守，全是踩出来的**）

### 5.1 前端架构

- `web/` 下每个模块 = IIFE + `window.H3Xxx`。公开面只有
  `H3Api` / `H3Prompts` / `H3Assets` / `H3Anchor` / `H3Director.upscaleLatent`
- ⚠️ **`h3_director.js` 的顶层函数（`getDirValue` / `setSegmentField` / `scheduleRefresh`
  / `escapeHtml` / `getDs` / `findNode` …）是模块级的、没挂 window**。
  别的模块按全局名调必然失败 → 必须**宿主注入访问器**
  （锚定面板收 `{ node, data, idx, refresh, dir, setAnchors }`）
- ⚠️ **`getDs()` 每次 `JSON.parse` 出新对象** → 宿主写入后，子模块的 `data.ds` 是旧的
  → 子模块**必须写透**自己的副本（见 §2.3）
- ⚠️ **`h3_director.js` 的模块级 import 会影响用 `exec()` 按路径加载它的测试**
  （`test_transcode_p1` / `test_library_actions` / `test_review_fixes`）→
  给 `routes.py` 加依赖要**用函数内延迟导入**

### 5.2 测试与环境（本机实测）

| 命令 | 结果 |
|---|---|
| `pytest tests -q --ignore=tests/test_eav_feta.py --ignore=tests/test_rework_v2.py` | **298 passed / 2 failed**（2 个失败均为函数内 `import torch`，环境限制） |
| `pytest tests -q`（不排除） | 收集期 2 error（torch） |
| torch-free 解释器 | `C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe`（pytest ✓，无 torch） |
| torch 解释器 | `.../ComfyUI/ComfyUI/.venv/Scripts/python.exe`（torch ✓，**无 pytest**） |
| node | `C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2-3/node.exe` |

- **Bash 工具 PATH 残缺**：`ls`/`cat`/`head`/`find` 全没有（但 `git` 可用）
  → 文件系统操作用 `python.exe -c`；**heredoc `cat > file` 一定失败**，
  写临时脚本用 **Write 工具**写到 `%TEMP%`
- **PowerShell 工具不返回 stdout**，且写中文脚本会 GBK 解析失败 → 尽量不用
- **文件操作建议用绝对路径**（本轮出现过相对路径找不到文件）
- **提交信息里不要用反引号**（bash 当命令替换，会把 `-m "..."` 搞乱）

### 5.3 交付纪律

1. **`node --check` 只验语法**，跨模块引用错、数据陈旧这类问题它查不出来
2. 可用的**运行时探针**：`%TEMP%/h3_anchor_probe2.cjs`
   （Node + Proxy 自动桩模拟 DOM，真调 `buildAnchorPanel`，能验"渲染期是否抛错"）
3. 大段删除用 **python 脚本按行范围删 + assert 边界**（`Edit` 工具容易连带删掉续行）
4. 改完必须**目视打开面板**；但**不要**在 `h3_director.js`（500KB+）里大段写 DOM——
   新功能一律抽独立模块

---

## 6. 需要现场确认的疑点

1. **刷新修复是否真生效**（§2.3）——待用户强刷后反馈
2. `data.mf` 是否真的传进了锚定面板（决定「分辨率未知」「帧数与分段时间对齐」两个问题）
3. 目标轨帧数与「本段时长」控件是否**同源**
   —— 已查明 `mf.seg_lengths` 在 `h3_director.js` 里 **0 次出现**（后端也不写这个键），
   所以现在只能退回全局 `params.length`
4. **二采路径**只重注段首桥 / 头锚 / 尾锚，`mid` / `tail` 手动锚不参与高清重渲
   （与重构前入参范围一致，**不是回归**，属既有范围）

---

## 7. 相关文档

- `docs/手动锚定_分段latent参考规范化_实施规划.md` —— 主规划（§10 进度表已同步）
- `.workbuddy/memory/2026-09-16.md` —— 当日工作日志（含每轮根因与踩坑）
- `.workbuddy/memory/MEMORY.md` —— 长期约定（前端模块约定 / 渲染所有权 / 素材来源）
