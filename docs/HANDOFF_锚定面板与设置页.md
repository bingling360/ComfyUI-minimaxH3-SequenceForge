# 交接：锚定面板 + 段卡「设置」页

日期：2026-09-16 19:40　分支：`anchor-studio`　HEAD：`c2a01c1`（本地领先
`origin/anchor-studio` **11 个提交，尚未推送**）

> **新会话怎么用这份文档**：T1–T6 **已全部落地（本轮未提交）**，
> 先读 §3（本轮改了什么、怎么验的）与 §4（还没验的、要在 G 盘机器上做的事），
> 再读 `docs/手动锚定_分段latent参考规范化_实施规划.md` 的
> §0 决策总表 / §3 数据结构 / §9 附录A 代码地图。**§5 是已锁定的设计决定，不要重新讨论。**

---

## 1. 背景一句话

把「把某段 latent / 素材钉到本段某位置」从散落七处的碎片，收敛成一个 `anchor`
数据结构 + 一套双轨时间线 UI。后端（Python）已全部落地；本轮把前一轮剩下的
**前端收敛（T1/T2/T5）与后端源解析（T3/T4）**全部做完，并补了两件工具。

---

## 2. 上一轮的基线（已完成，别重做）

### 2.1 后端重构（步骤 1–8，全部完成）

| 提交 | 内容 |
|---|---|
| `93fe186` | 步骤 3/5/8 + 插入视频链路整体移除 |
| `cc93c7d` | 步骤 6 主编排切换 + 步骤 4/7 收尾 |
| `df3e14f` | 文档同步 + `projects.py` 死代码清理 |

- `anchors.py`：归一化 / 硬校验 / 旧字段迁移 / 变更区间 / 源归一化（纯函数）
- `checkpoint.assert_match` 收窄为只校验 width/height；`reroll_start` 删除，
  区间计算归 `anchors.change_intervals`，区间标进重摇通道 `redo_map`
- **唯一入口**：`seg.anchors[]`；`_apply_guide(cond, keyframes, sampled_fc)`

### 2.2 前端修复链（每一笔都是真 bug）

| 提交 | 修的什么 |
|---|---|
| `903ce87` | 跨模块按全局名调宿主函数 → 整块「段落卡片渲染失败」 |
| `afcab6b` | 帧区间乱 / 轨道叠加 / 切来源无条目 / 新增没反应 |
| `d2e5dc1` | 目标轨改可点可拖 + **改回一条线** + 去掉「中间」 |
| `77a608b` | 面板自拥有渲染权（不再触发宿主全量重刷） |
| `24972f1` | 设置页：latent 保存按模式显隐 + 锚定面板改上下堆叠 |
| `1f64814` | **刷新问题真凶**：`getDs()` 每次 `JSON.parse` 出新对象，写入没写透副本 |
| `c2a01c1` | 交接文档 |

---

## 3. ★ 本轮做完的（T1–T6，未提交）

### 3.1 来源类型收敛成两类（T1）

`web/h3d_anchor.js`：来源下拉只剩 **「段」/「素材」**。

- 「段」= 本项目已落盘的 `seg_NNN.pt`，条目下拉直接列段（带帧数）
- 「素材」= 从**素材库**挑（图片 / 视频 / latent），底层 kind 由条目决定：
  latent → `library`，其余 → `image` / `video`
- `prev_tail` **退出 UI**（用户明确说冗余：「有上段的选择就行了」）。后端保留它作
  **隐式默认段首桥**（没有显式 head anchor 时才走）。旧存档里已有的 `prev_tail` 锚
  以只读项回显 + 一行替换说明，**不再是一个可选项**
- 新建锚默认「段」+ 预填上一段（拿到的就是同一份 latent）；
  上一段没落盘 latent（该段设了「不存 latent」）时 ref 留空，由用户自己挑
  ——后端 `validate_anchors` 会硬拦空 ref，不会静默生成一个没有源的锚

### 3.2 素材选择器 = 复用现有素材库（T2）

`web/h3_library.js` 加了**挑选模式**：`H3Lib.open({ dir, seg, pickKinds, onPick })`。

- 单击瓦片 = 选中并自动关窗；搜索 / 筛选 / 四个 scope / 缩略图 / 上传全部照旧
- 挑选模式下隐藏「多选」，类型下拉按 `pickKinds` 收窄（锚源只收图/视/latent）
- **没有造第二个素材浏览器**：两套 UI 看同一批文件必然漂移

### 3.3 后端两处真 bug（T3 + 顺带发现）

| 问题 | 真相 | 修法 |
|---|---|---|
| **段源一选就报错** | `routes.anchor_sources` 发的是 `seg_003`（`anchors.py` 文档、`tests/test_anchors.py` 都按这个），但 `nodes._resolve_anchor_latent` 要求以 `.pt` 结尾、还按 `_pn[4:-3]` 切片取段号 | nodes 改为接受 `seg_NNN`（规范形式三处一致） |
| **素材库里的视频接不进来** | `kind="video"` 写死走 `_load_input_video` → 只认 ComfyUI input 目录 | 抽出 `_anchor_asset()`（标签/asset_id → 绝对路径，与图片同一条寻址通路），视频新增 `_anchor_video()` 走 `_decode_video_file` |

### 3.4 拆掉自建的素材枚举（T4）

`routes.anchor_sources`：

- 删掉「扫 input 目录枚举视频」与「从 `manifest["assets"]` 枚举图片」两支
- 删 `_probe_media_meta` / `_VIDEO_EXTS`
- 保留 **段 + latent 库**；新增可选参数 `ref=<库条目 id>`：**只探测那一个文件**
  （latent → 形状 + sheet；图片 → PIL 头读尺寸；视频 → av 头读 fps/帧数/尺寸），
  回一条与列表同形的源条目（含 `item_id` / `resolvable`）
  —— 整目录枚举是错的，但「这个文件多少帧」只能由后端按同一套安全解析规则给出，
  面板的刻度与越界体检都靠它
- `prev_slot` 补上**序章偏移**（有序章的项目原先会把「上段尾」指到序章上）
- 「段」列表现在**包含上一段**（原先被排除，于是"让你选上一段"但列表里没有）

前端 `_getJson` 现在把后端 `{message}` 透出来（原先只抛 `HTTP 404 · path`，
真正原因全丢，面板只能显示一句「（无可用条目）」）。

### 3.5 设置页收尾（T5）

`web/h3_director.js` 的 `paneSet`：

- **按作用面分两区 + 小标题**：「影响本段生成」（锚定 / 首尾帧图 / 两个开关）
  ｜「只影响落盘」（latent 保存）
- 两个开关**并成一行**（原先各占两行）
- **latent 保存收敛成人话三选**：存全段（默认）/ 只存尾部 + 帧数输入 / 不存。
  `start_f` / `end_f` / `split_av` 不再铺在界面上；旧档若是 `range` 或 `split_av`，
  给一行提示说明「旧设置仍在生效、怎么替换」
- **本段总帧数只有一个算法**：新增 `segmentFrames(node, seg)`，段卡标题那个「≈N帧」
  与锚定面板目标轨刻度**共用它**，并通过 `frameLen` 注入面板。
  ⚠️ 注意：后端 `nodes._snap_seconds` 是**就近**吸附 17k+5（**不是**向上对齐），
  面板原先自己算过一遍、用的是向上对齐 —— 这就是「目标轨跟分段时间对不上」的根因
- 体检四项改口径：
  1. 分辨率：**段/latent 源**才做硬比对（C/H/W 拼不上就是硬错）；
     **视频/图片源**允许不一致（执行期 center-cover 先裁后编，是正常路径）
  2. 帧窗合法：`1` 也算合法（单帧身份锚；原先只查档位表会把迁移出来的尾锚误报非法）
  3. 源内取用窗越界：比的是**源**的帧数（原先比的是"本段帧数"，两个数毫无关系）
  4. **新增**本段落点越界（`落点 + 窗宽 ≤ 本段帧数`，这才是 `validate_anchors` 拦的那条）
- 22 个档位按钮收起成 `1/5/22/39/56 + 更多▾`（选中项若不在常用里会自动外露）
- 面板上的空状态改成说**原因**（没跑过段 / latent 目录空 / 拉取失败的具体 message）
- `grid_spec` 改成缓存 **promise**（原来缓存结果值，三张卡并发会把 1 次请求穿透成 3 次）

### 3.6 交付验证（T6）

| 手段 | 结果 |
|---|---|
| `pytest tests -q --ignore=…eav_feta --ignore=…rework_v2` | **309 passed / 2 failed**（2 个失败都是函数内 `import torch`，环境限制，与基线一致） |
| `tests/test_anchor_wiring.py` | 29 条结构守卫（原 5 条 + 本轮新增/更新；顺手删掉了文件末尾**重复定义**的那条测试） |
| `node --check` × 3 个 js | 通过 |
| `python tools/make_anchor_preview.py` + `node tools/anchor_ui_smoke.cjs` | **SMOKE OK**（35 条断言 / 0 报错）|

**新增两件工具（本轮最有价值的副产品）**：

```bash
python tools/make_anchor_preview.py     # 生成 tools/anchor_preview.html（自包含预览页）
node  tools/anchor_ui_smoke.cjs         # jsdom 真 DOM + 真点击冒烟，退出码 0 = 全绿
```

- 预览页真加载 `web/h3_api.js` / `h3d_anchor.js` / `h3_library.js`，只把 `/h3chain/*`
  换成本地桩 → **能在浏览器里直接看**，不用起 ComfyUI
- 冒烟用 jsdom（仓库 `node_modules` 里就有）跑预览页、真点按钮：
  这一步**当场抓到一个真 bug** —— 选素材的报错原因 `pickErr` 是 `fillSideCol` 的局部变量，
  而报错后要 `rebuildCard` 才看得见、重建正好把它冲掉，于是「这个素材节点侧寻址不到」
  的警告**永远显示不出来**。已改成挂在卡片上下文 `ctx.__pickErr` 上。
  **这类错误 `node --check` 一条都查不出。**

---

## 4. 还没做的（**必须在 G 盘机器上现场验**）

本机没有 ComfyUI 运行环境 + GPU，下面这些只能到现场做：

1. **真写回链路**：面板里改锚 → `setSegmentField` → 保存进 manifest → 重读导演台，
   数值逐项对得上（本机只验到"面板自渲染"这一层）
2. **真的跑一段**：`kind="segment"` / `kind="video"` / `kind="image"` 三种源各跑一次，
   确认 `_resolve_anchor_latent` 不抛、先裁后编路径生效
3. **素材库里的视频**作锚源（本轮新打通的那条路）：项目 `assets/` 与全局库各试一个
4. **有序章的项目**：「上段尾」/「上一段」是否指向正确的段（本机只验了槽位算式）
5. **设置页两区的视觉**（用户会亲自看）：`tools/anchor_preview.html` 只能看锚定面板
   本体，套在「设置」页两区外壳里的样子要在真导演台里看
6. 提醒用户强刷（Ctrl+Shift+R）确认 §2.2 那个刷新修复

---

## 5. 已锁定的设计决定（**不要重新讨论**）

| # | 决定 |
|---|---|
| 1 | **素材一律从资产库选**（全局库/项目库），不自己造枚举 |
| 2 | 来源两类：**段 / 素材**；`prev_tail` 从 UI 去掉（后端留作隐式默认） |
| 3 | 段卡三页里的**「设置」页**才是重构对象——**不是**整个段卡 |
| 4 | **窗宽只能点 17k+5 档位**（或 1=单帧锚），**位置自由**——非档位宽度会被模型折掉、后端硬报错 |
| 5 | 目标轨 = **一条线**（不是跨度框），刻度上**点/拖即定位**（自动切 mid）；不要「中间」按钮 |
| 6 | 锚定面板在段卡里**上下堆叠**（源轨 → 目标轨 → 体检） |
| 7 | **一块 UI 只能有一个渲染主人**：本地改动走本地重建，宿主 refresh 只作兜底 |
| 8 | 后端**保留** `latent_ref` / `tail_src` 的迁移（只读旧档），UI 入口已删——锚定面板是唯一入口 |
| 9 | latent 落盘策略在界面上只有三选（存全段 / 只存尾部 N 帧 / 不存），后端字段不暴露 |

---

## 6. 工程约束（**必须遵守，全是踩出来的**）

### 6.1 前端架构

- `web/` 下每个模块 = IIFE + `window.H3Xxx`。公开面只有
  `H3Api` / `H3Prompts` / `H3Assets` / `H3Lib` / `H3Anchor` / `H3Director.upscaleLatent`
- ⚠️ **`h3_director.js` 的顶层函数（`getDirValue` / `setSegmentField` / `scheduleRefresh`
  / `escapeHtml` / `getDs` / `segmentFrames` …）是模块级的、没挂 window**。
  别的模块按全局名调必然失败 → 必须**宿主注入访问器**
  （锚定面板现在收 `{ node, data, idx, refresh, dir, setAnchors, frameLen }`）
- ⚠️ **`getDs()` 每次 `JSON.parse` 出新对象** → 宿主写入后，子模块的 `data.ds` 是旧的
  → 子模块**必须写透**自己的副本
- ⚠️ **状态别放在会被重建冲掉的局部变量里**：报错信息这类「重建后还要看得见」的东西
  要挂在卡片上下文（`ctx.__pickErr`）上，否则 `rebuildCard()` 一跑就没了
- ⚠️ **`h3_library.js` 依赖 `window.H3Api`**（`api()` 里硬校验）；`h3d_anchor.js`
  **刻意不依赖**它，自己拼 URL —— 少一条隐式契约。预览页因此要带上 `h3_api.js`
- ⚠️ 给 `routes.py` 加依赖要**用函数内延迟导入**（`exec()` 按路径加载它的三个测试
  文件在收集期就会 ImportError）

### 6.2 测试与环境（本机实测）

| 命令 | 结果 |
|---|---|
| `pytest tests -q --ignore=tests/test_eav_feta.py --ignore=tests/test_rework_v2.py` | **309 passed / 2 failed**（2 个失败均为函数内 `import torch`） |
| `pytest tests -q`（不排除） | 收集期 2 error（torch） |
| `node tools/anchor_ui_smoke.cjs` | SMOKE OK（需先跑 `make_anchor_preview.py`；需 jsdom） |
| torch-free 解释器 | `C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe` |
| torch 解释器 | `.../ComfyUI/ComfyUI/.venv/Scripts/python.exe`（**无 pytest**） |
| node | `C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2-3/node.exe` |

- **Bash 工具 PATH 残缺**：`ls`/`cat`/`head`/`find`/`rm`/`dirname` 全没有（`git` 可用）
  → 文件系统操作用 `python.exe -c`；**heredoc `cat > file` 一定失败**，
  写临时脚本用 **Write 工具**写到 `%TEMP%`
- **PowerShell 工具不返回 stdout** → 尽量不用
- **文件操作建议用绝对路径**
- **提交信息里不要用反引号**（bash 会当命令替换，把 `-m "..."` 搞乱）

### 6.3 交付纪律

1. **`node --check` 只验语法**，跨模块引用错、数据陈旧、状态被重建冲掉 —— 一条都查不出
2. 交付 UI 改动**必须跑** `make_anchor_preview.py` + `anchor_ui_smoke.cjs`；
   能起浏览器就直接开 `tools/anchor_preview.html` 看
3. 大段删除用 **python 脚本按行范围删 + assert 边界**（`Edit` 工具容易连带删掉续行）
4. 改完必须**目视打开面板**；但**不要**在 `h3_director.js`（500KB+）里大段写 DOM——
   新功能一律抽独立模块

---

## 7. 相关文档

- `docs/手动锚定_分段latent参考规范化_实施规划.md` —— 主规划（§10 进度表已同步）
- `tools/make_anchor_preview.py` / `tools/anchor_preview.html` —— 离线预览页
- `tools/anchor_ui_smoke.cjs` —— jsdom 真 DOM 冒烟
- `.workbuddy/memory/2026-09-16.md` —— 当日工作日志（含每轮根因与踩坑）
- `.workbuddy/memory/MEMORY.md` —— 长期约定
