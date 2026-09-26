# HANDOFF：合并模式重构（R7）

> **✅ 已落地（2026-09-27）。** 本文件现在的作用是**变更记录 + 设计口径留档**，
> 不再是待办清单。下面第 2 节的代码行号已经过期（改动后行号全移了），
> 要看现状请直接 grep 符号名。
>
> ## ⚠️ 定稿改口（2026-09-27 第二轮，**以本节为准**）
>
> 第一版把合并做成"导演台开合并模式 + 素材库当选择器"，用户看了实际效果后否掉了：
> **合并整套只在素材库里出现，导演台一个入口都不留。**
>
> 理由（用户原话是"含义错了"）：同一个功能两处入口、两份清单状态，必然漂移成
> "我在这儿选好了，那边却显示没选"。所以：
>
> | 位置 | 定稿 |
> |---|---|
> | 素材库 | **标题栏最右（✕ 关闭键左边）**常显 **`⧉ 合并导出`** 入口 → 进**选材模式**（标题换、批量区藏起来）→ 入口自己让位，同一席位换成 **`⧉ 开始合并（按 1→N 顺序）`** + **`✕ 退出合并`**（这两个按钮**只有进了选材模式才出现**）→ 点素材排顺序 → 就地发起拼接 |
> | 素材库 | **只收视频**：进选材模式即把类型筛选**强制成 video**、`latent` 页签消失；非视频瓦片标灰（`.h3l-nomerge`）且点不动 |
> | 素材库 | 拼接请求由**素材库自己发**（`H3Api.libMerge`），失败用 `errText` 显示后端原话；成功清清单 → 退选材模式 → 跳到「成片」 |
> | 导演台 | **不留任何合并 UI**：状态条按钮、`.h3d-mergebar`、段卡勾选框、`mergeSel` 清单全删；只剩 `const mergeJob = {running}` + `window.H3Merge = {begin, end(ok), running}` |
> | 导演台 | 4 处提交守卫读 `mergeJob.running`；状态条给一句「合并导出进行中」徽标 + 生成按钮变「⧉ 合并导出进行中（跑完可生成）」 |
>
> 于是 `open({merge, order, onMergeChanged, onMergeCommit})` 这些入参**全部取消** ——
> 清单是素材库自己的状态（`S.mergeOrder`），不再需要外部注入或回传。
>
> ### 第二轮改口（同日，用户实测反馈）：位置 + 出现时机
>
> 用户报了两条，其实是**同一个 bug 的两面**：
>
> | 反馈 | 真因 | 修法 |
> |---|---|---|
> | 「按钮位置不够显眼，应该放最右边或特殊位置」 | 入口原本埋在工具条里（那一行已经挤了搜索 + 4 个筛选控件 + 批量区 + 上传 + 重新扫描），还会随 `flex-wrap` 换行乱跳 | 合并区整块搬到**标题栏最右、✕ 左边**（`.h3l-mergearea`，席位固定）；CTA 改蓝色（绿色已被「＋ 上传」占了，且蓝色与瓦片上的 1/2/3 角标同色） |
> | 「开始合并 / 退出合并**不该现在就在**，点了合并导出才出现」 | **真 bug**：`.h3l-mergebox` 同时挂了 `.h3l-batch`，两者都是 `(0,1,0)` 权重，而 `.h3l-batch{display:flex}` 在源码里**更靠后** → 压掉了 `.h3l-only-merge{display:none}`，于是这两个按钮从打开素材库起就常显 | `.h3l-mergebox` **只给布局、不给 display**，`display` 由 `.h3l-only-merge`（none）/ `.h3l-merging .h3l-only-merge`（flex，(0,2,0)）独占 —— 不再依赖源码顺序 |
>
> 教训：**显隐工具类的隐藏规则 (0,1,0) 撞上别的 `display` 声明 (0,1,0) 时靠源码顺序决胜，
> 这是会静默失效的**。要么让显示规则权重更高（这里 `(0,2,0)` 做到了），要么隐藏元素上
> 别再挂任何自带 `display` 的类。
>
> 守卫：`tests/js/merge_library_check.js` 用 **jsdom 的层叠计算**（`getComputedStyle().display`）
> 直接断言"没点入口时合并区是 `none`、点了变 `flex`、退出又回 `none`"，另加一条 CSS 不变量
> （`.h3l-mergebox` 不许出现带 `display` 的规则）。两条都做过故障注入，确认能抓。
>
> ### 顺带修掉的一个真 bug（用户截图里的「合并失败：HTTP 400」）
>
> 后端 `_err()` 的字段名是 **`message`**（`{"ok":false,"code":...,"message":...}`），
> 而旧的 `doMergeExport` 读的是 `j.error` → 永远 falsy → 所有失败都只剩一句
> **「HTTP 400」**，真正的原因（素材没了 / 不是视频 / 路径越界）全被吞掉。
>
> 实测确认（对运行中的实例打真请求）：新后端的 `{"asset": id}` 分支**本身没问题**
> —— 拿真素材 id（`project:assets/0.8-30S-1074S.mp4`，文件名带空格）能正常解析，
> 只有故意塞的假 id 才报错。真正的 400 原因是**清单里混了图片**：旧版选材模式
> 让四个库全量可选，用户点中的图片被后端以"只能合并视频：X 是图片"整单拒掉。
> → 所以两处一起修：**前端锁成只选视频** + **失败显示后端原话**。
> 教训：后端错误体的字段名只有一处定义（`routes._err`），前端别再自己猜。

---

> 下面是**当时**写的原始规格（含过期行号），保留供追溯。
> 写给下一个接手的会话。**先读本文件 + `.workbuddy-ai/memory/MEMORY.md`**，
> 代码地图里的行号是 2026-09-27 当天核过的，改之前用 grep 复验一次（本文件写完
> 之后可能又有提交）。
>
> **落地清单（第一版，已被上面的「定稿改口」覆盖，留档看演进）**：
>
> | 位置 | 结果 |
> |---|---|
> | `web/h3_director.js` | `mergeSel = {on, order[], segs[], files[], running}`；`resetMergeSel()` / `mergeCount()`；状态条 `⧉ 合并模式` 去掉 `if (done>0)` **常显**（开启时显示已选数量）；`mergebar` = `已选 N 个素材` + `🗂 从素材库选` + `＋ 上传视频` + `⧉ 合并导出` + `✕ 退出`；`openMergeLibrary()` 新增 `onMergeCommit`；`doMergeExport(btn)` 的 `btn` 可为 `null`；4 处互斥守卫从 `mergeSel.on` 收紧到 `mergeSel.running`；段卡 `.h3d-mergecb` 与其 CSS 整块删除；`cardsSignature` 的合并指纹改按 `order` 的 id 序列；`canDrag` 不再看 `mergeSel.on` |
> | `web/h3_library.js` | `open({merge, order, onMergeChanged, onMergeCommit})`；`S.merge` / `S.mergeOrder`（数组，顺序即数据）/ `paintMergeBadges()`；瓦片左下角 `.h3l-order` 角标**已登记进 `tileKeepers()`**；合并模式下工具条**只摆合并区**（`⧉ 开始合并（按 1→N 顺序）` + `✕ 清空顺序`），批量区不出现；`renderFoot()` 回显"已排 N 个"；双击预览在合并模式下**保留**（挑片子前要能看一眼） |
> | `projects.py` | 新增 `_merge_asset_source()` + `_allowed_merge_roots()`；`_merge_sources` 支持 `{"asset": "<scope>:<file>"}`（`library.find_by_id` 反查 → `library.resolve_item_path` 解析 → realpath 复核落在项目目录/全局库内；只收视频）。判据是 `it.get("asset")` **真值**，空串回落 `{"file": f}` 分支 |
> | 测试 | 新增 `tests/test_merge_assets.py`（17 条，后端解析）+ `tests/js/merge_library_check.js`（15 条，前端顺序/角标/懒加载/守卫） |
>
> **两处与原文不同的判断**：
> 1. 原文说素材库条目 id 形如 `a_xxxxxxxxxxxx` —— 实测是 **`<scope>:<file>`**（`library._entry`）。
>    后端因此**不自己拼 id**，用 `find_by_id` 反查（生成规则只有 library.py 一份）。
> 2. `mergeSel.segs` 字段**保留但不再写入**（原文推荐的最小改动面），兼容期老存档读得进。

---

> 下面是**当时**写的原始规格（含过期行号），保留供追溯。
> 写给下一个接手的会话。**先读本文件 + `.workbuddy-ai/memory/MEMORY.md`**，
> 代码地图里的行号是 2026-09-27 当天核过的，改之前用 grep 复验一次（本文件写完
> 之后可能又有提交）。

## 0. 一句话目标

把「合并模式」从**导演台里选段**改成**素材库里选素材**，让它始终可见，并用
数字角标把合并顺序显式画出来。

## 1. 用户已拍板的口径（别自行改动，改之前先问）

| # | 口径 |
|---|---|
| Q1 | **点击顺序 = 合并顺序**（不做拖拽排序）。**退出合并模式立即 reset 全部状态**。 |
| Q2 | 素材库批量操作按钮**常显**（不选也要看得见；选中态只决定作用于谁，不是决定按钮存不存在）。 |
| Q3 | 复制项目**不继承 `params`**；切项目**永不静默**把存档参数写回画布。 |
| Q4 | 终止按钮 = `POST /interrupt`，二次确认必须写明"会中断当前所有作业"。 |

Q2–Q4 已落地。本文件只管 R7。

## 2. 当前状态（代码地图）

### 导演台 `web/h3_director.js`

| 行 | 是什么 |
|---|---|
| `123` | `const mergeSel = { on: false, segs: [], files: [] }` — 全局内存态，不落 ds |
| `2942` | `pickMergeVideo()` — 上传外部视频附加到 `mergeSel.files` |
| `2960` | `doMergeExport(btn)` — 组装 `items` 后 `POST /h3chain/merge` |
| `7513` | 状态条合并按钮，**被 `if (done > 0)` 包着 → 这就是"要生成一段才看得到"的根因** |
| `7558` | `.h3d-mergebar`（`＋ 上传视频` / `⧉ 合并导出` / `✕ 退出`），仅在 `mergeSel.on` 时渲染 |
| `7754–7772` | 段卡前置 checkbox `.h3d-mergecb` + `card.classList.add("mergeable")` — **这就是要取消的"在外面选分段"** |
| `6002/6003` | CSS `.h3d-card.mergeable` / `.mergeable-off` |
| `6019` | CSS `.h3d-mergecb` |
| `6020` | CSS `.h3d-mergebar` |

`mergeSel.on` 还有三处互斥守卫（合并中会挡住提交）：
`queuePrompt`（`5115` 附近）、`submitRedo`、`openRerollModal`，文案都是
"合并模式进行中：请先完成或退出合并导出"。

### 素材库 `web/h3_library.js`

| 行 | 是什么 |
|---|---|
| `16` | 用法注释：`window.H3Lib.open({ dir, seg, onChanged })` |
| `386` | `tileFor(it)` — 造瓦片，`.h3l-tile` + `.sel` 类表示选中 |
| `428–455` | `dblclick` 预览 / `click` 选中（`S.pick` 挑选模式 → 单击即返回并关闭；否则多选切换） |
| `931` | `const pickMode = typeof o.onPick === "function"` |
| `942` | `S` 初始化，`sel: new Set(), multi: true` |
| `1001` | `batchBox` —— **R4 已加的批量操作区**（全选 / 调入项目 / 存入全局库 / 删除），常显；`!pickMode` 时才 append |

素材库条目 `it` 的关键字段：`it.id`（`a_xxxxxxxxxxxx`）、`it.scope`
（`global` / `project` / `finals` / `latent`）、`it.kind`、`it.file`（相对路径）、
`it.name`、`it.blocked`。

### 后端

| 位置 | 是什么 |
|---|---|
| `routes.py:256` | `async def merge(request)` — 取 `data["items"]` 丢给 `projects.merge_project` |
| `projects.py:1922` | `def merge_project(name, items, fps=24, crf=20)` |
| `projects.py:1866` | **`def _merge_sources(root, manifest, items)`** — 清单 → 绝对路径，**只认两种形态** |
| `library.py:1283` | `def resolve_item_path(item, project=None)` — 按素材条目解析绝对路径（**现成的，直接用**） |

### ⚠️ 最关键的约束

`_merge_sources`（`projects.py:1880–1918`）只支持：

- `{"seg": n}` — n 是 1-based **全局槽位**（含序章），映射到 `manifest.videos[n-1]`
- `{"file": f}` — 先查项目目录（`resolve_project_file`），再查 `input` 目录；
  `f` 至多两级子目录、每段过 `safe_name` 防穿越

**不支持 `{"asset": id}`**。而素材库里 `scope: "global"`（全局库）的素材既不在
项目目录也不在 `input` → 传 `{"file": it.file}` 一定会在后端报
`文件不存在（项目目录与 input 目录都没有）`。

所以：

- **方案 B（推荐）**：后端 `_merge_sources` 新增 `{"asset": id}` 分支，用
  `h3lib.resolve_item_path(item, project)` 解析。全局库素材也能合并，语义最干净。
- 方案 A（降级，后端零改动）：前端按 `it.scope` 分流 —— `project` / `finals`
  传 `{"file": it.file}`，`global` 直接拒绝并提示"全局库素材请先调入项目"。
  **先做 A 跑通交互，再补 B**，是最省事的路径。

## 3. 分步改动

### S1 — 状态条合并按钮改常显

`h3_director.js:7513` 去掉 `if (done > 0)` 包裹。

按钮语义保持：`mergeSel.on` 切换。文案建议 `⧉ 合并模式` / `⧉ 合并模式·开`，
**开的时候把已选数量画出来**（见 S4）。

> 设计点：合并模式**常显**之后，"开着但清单为空"会成为常态。原先三处互斥守卫
> 是"合并模式进行中"就挡住提交生成，现在会变成"我随手开了个合并模式，结果生成
> 按钮点了没反应"。**把互斥条件从 `mergeSel.on` 收紧到"合并导出进行中"**
> （用一个新标记，例如 `mergeSel.running`），清单为空不该拦人。

### S2 — 删掉段卡上的"外面选分段"

删 `h3_director.js:7754–7772` 整块（`cb.className = "h3d-mergecb"` 那段），
连带删 CSS `.h3d-mergecb`（`6019`）与 `.h3d-card.mergeable` / `.mergeable-off`
（`6002`/`6003`）—— 没有 checkbox 之后它们是死样式。

`mergeSel.segs` 这个字段**保留但不填**（老存档 / 兼容期），还是彻底删掉，由你
判断；推荐**保留字段、不再写入**，改动面最小。

### S3 — 素材库加"合并模式"上下文

`h3_library.js`：

1. `open(opts)` 新增入参：`o.merge`（布尔，进入合并模式）。
2. 合并模式下：
   - 标题改为 `🗂 选择要合并的素材（按点击顺序）`。
   - **隐藏** `batchBox`（`1001`，R4 加的批量区）—— 合并模式下"调入项目/删除"
     跟合并没关系，摆着只会让人点错。
   - 工具条加一个显眼的 `⧉ 合并（按 1→N 顺序）` 按钮。
3. 顺序状态：`S.mergeOrder = []`（**数组，不是 Set** —— 顺序就是它本身）。
   点瓦片 = push / 已存在则移除（移除后后面元素的角标要重排）。
4. 数字角标：瓦片左上角画一个 `1` / `2` / `3` / `4` 的圆标。
   建议放在 `.h3l-thumb` 里（跟 `.h3l-ico` / `.h3l-role` 同级），
   **记得同步进 `tileKeepers()`**（`467`）—— 缩略图懒加载会 `replaceChildren`，
   不登记进去角标会在滚动时被抹掉。这是本仓库踩过的坑，务必注意。
5. 退出合并模式（`S.close` 或关闭窗口）→ **`S.mergeOrder = []` 立即 reset**
   （Q1 口径）。

### S4 — 导演台与素材库的状态同步

导演台开合并模式后需要一个入口打开素材库（合并上下文）。现有的素材库入口在
左栏「项目与链」区（`renderLeftColumn`，`window.H3Lib.open` 调用处）。合并模式
下把那个入口的语义换成"打开素材库选合并素材"，或者直接在 `mergebar` 里加一个
`🗂 从素材库选` 按钮 —— 后者改动更小，推荐。

状态条上的 `mergebar`（`7558`）改画：
`已选 N 个素材（按 1→2→3 顺序）` + `🗂 从素材库选` + `⧉ 合并导出` + `✕ 退出`。
`＋ 上传视频`（`pickMergeVideo`）保留 —— 外部视频仍是合法合并来源。

### S5 — 合并调用改传参

`doMergeExport`（`2960`）现在传：
```js
[...mergeSel.segs].sort((a,b)=>a-b).map(s=>({seg:s}))     // 段号（要废弃）
...(mergeSel.files||[]).map(f=>({file:f}))                 // 外部视频（保留）
```
改成按**素材顺序**组装（顺序就是 `S.mergeOrder`，**不要 sort**）：
```js
...mergeOrder.map(it => ({ asset: it.id, file: it.file }))   // 方案 B
// 或方案 A：...mergeOrder.map(it => ({ file: it.file }))
...(mergeSel.files||[]).map(f=>({file:f}))
```
⚠️ `assets` 这种顺序错了就全错的地方，**别用 sort**，顺序是用户点的顺序。

### S6 — 后端（方案 B，可选）

`projects.py:_merge_sources` 在 `if "seg" in it` 之前加一段：
```python
if "asset" in it:
    # 用现成的 library.resolve_item_path 解析；路径必须仍落在项目目录/全局库内
    ...
```
要求：
- 解析不出来 → `ValueError`，消息要能直接给人看（"素材 X 的文件找不到"）。
- **保留 `safe_name` / realpath 复核的防穿越口径**，别因为换了入口就放松。
- 只接受**视频** kind（音频/图片/latent 合并没有意义）—— 提前拒绝比让 PyAV 报
  半截错好。

## 4. 验收（手测，逐条打勾）

- [ ] 导演台打开就有 `⧉ 合并模式` 按钮（**链一段都没生成时也在**）。
- [ ] 开启后，段卡上**没有** checkbox（外面选分段已取消）。
- [ ] 开合并模式但一个素材都没选 → **生成按钮仍可用**（不被互斥拦住）。
- [ ] 从素材库里点 3 个素材 → 瓦片角标显示 `1` `2` `3`，顺序与点击一致。
- [ ] 再点第 2 个 → 角标消失，后面的重排成 `1` `2`。
- [ ] 素材库滚动（触发懒加载）后角标**还在**（`tileKeepers` 已登记）。
- [ ] 退出合并模式 → 角标与顺序**立即清空**。
- [ ] 合并导出 → 产物 `merged_*.mp4` 的片段顺序与角标 `1→2→3` 一致。
- [ ] 全局库素材：方案 A 会给明确提示；方案 B 能直接合并。

## 5. 跑测与交付流程（别漏）

```bash
# 全量基线（本机托管 venv，basetemp 必须是**空**目录，否则 safe-delete 会吃掉
# 几条删文件的用例、误判成回归）
C:/Users/xuan/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe \
  -m pytest tests/ -q -p no:randomly --basetemp=<新的空目录>

# 改完 web/*.js 必跑（anchor_preview.html 是**生成物**，不重跑页面就停在旧代码）
python tools/make_anchor_preview.py
NODE_PATH=$PWD/node_modules node tools/anchor_ui_smoke.cjs     # 期望 SMOKE OK / errors: 0
```

- **当前基线：968 passed / 0 failed**。判绿以「有没有真实 FAILED/ERROR」为准，
  别只看数字。
- 日志里出现 `safe-delete` 就说明删除被策略拦了（跨调用累积计数器），那不是回归，
  换空 basetemp 重跑。

## 6. 可能受影响的测试（改之前先 grep，删功能就得同步改断言）

- `tests/js/*_check.js` 里凡是断言 `mergeSel` / `h3d-mergecb` / `mergebar` 的。
- `tests/test_frontend_p3.py`、`tests/test_rework_v2.py` 里有前端符号的 KEEP/GONE
  名单（这两份名单先前已经因为删 `mkAlignPreview` 改过一次）。
- `tests/test_lib_delete_cleanup.py:355` 会读 `h3_library.js` 源码做断言。

## 7. 红线（别碰）

- **别动主节点 schema / `widgets_values` 顺序**：那是按位置序列化的，增删控件会
  让参数静默串位。改之前先读 MEMORY.md 里「改主节点 schema 的铁律」那条。
- 已删节点（`H3AssetBundle` / `H3LatentExtract` / `H3LatentUpscale`）相关死代码别碰。
- `docs/*.md` 是历史归档，**不要**为了同步而改旧文档；只更新 `README.md` 与本文件。
- `routes.py` 的 `transcode_submit` / `transcode_jobs` / `transcode_job` /
  `transcode_cancel` 是**设计上保留、前端零调用**的死接口，别当遗漏去"修"。
- 素材库的 `pickMode`（锚源挑选）跟新的 `merge` 模式是**两套上下文**，别混用同一
  个状态位 —— `S.pick` 是"单击即返回"，`S.mergeOrder` 是"累加排序"。
</content>
