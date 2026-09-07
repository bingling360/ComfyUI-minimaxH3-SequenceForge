# 批次交接：B01

**目标**：修复新旧 API 路由冲突（F03）与项目身份不稳定（F10），让新版 API 有
自己的命名空间，且段 ID 在调序/插入/删除后始终指向同一段内容。

前置：B00（`docs/BASELINE_B00.md`）。本批不改前端布局，不新建第二个入口。

---

## 修改文件

| 文件 | 用途 |
| --- | --- |
| `h3_studio/routes/api.py` | 新版路由统一迁到 `/h3chain/v2`；错误体结构化（code/message + field_path/segment_id/pin_id/clip_id/server_revision）；分页参数统一解析；`client_mutation_id` 回显；`legacy_conflicts()` 冲突检测 |
| `h3_studio/routes/__init__.py` | 每条路由同时挂根路径与 `/api` 前缀副本；注册前自检与旧 `/h3chain` 入口无冲突（原来只挂根路径，导致 `/api/*` 全部 404） |
| `h3_studio/projects.py` | **删除按数组下标把新 segment_id 改回旧 ID 的逻辑**（F10）；新增重复 ID / 超容量拒绝；`_commit_document()` 事务内条件更新 + 影响行数检查；引用索引与正文同一事务；提交前 staging 快照；clip_id 不再被服务端替换 |
| `h3_studio/schemas.py` | 新增 `DOC_SCHEMA_VERSION`、`MAX_SEGMENTS` 等容量常量；`clean_project` 不再 `[:64]` 静默截断；新增 `duplicate_segment_ids()` / `duplicate_clip_ids()` |
| `h3_studio/db.py` | `SCHEMA_VERSION` 1→2，新增逐级迁移框架 `_MIGRATIONS` 与 `_migrate_v2`（补索引 + 正文补 `schema_version`）；高版本库显式报错 |
| `h3_studio/assets.py` | `track_project_assets` / `track_edit_assets` 支持传入 conn，从而与正文在同一事务提交 |
| `web/h3_studio/api.js` | 前缀改为 `/h3chain/v2`；改用 ComfyUI `api.fetchApi` / `api.apiURL` 发请求（组件不再裸 fetch）；错误对象带定位字段 |
| `tests/`（新增） | `harness.py` 隔离夹具、`README.md`，以及 5 个 B01 测试 |
| `_smoke_db.py`、`_smoke_assets.py` | 修正包内模块加载方式（原来按文件路径加载，无法解析 `from . import`；`_smoke_assets.py` 此前因此整体失败） |

## 关键契约

- 新版前缀：`/h3chain/v2`，同时注册 `/api/h3chain/v2` 副本。旧 `/h3chain/*`
  仍只服务旧导演台入口，两边不再争抢同一个 method + path。
- 列表：`{ok, items, total, page, size}`，items 只有摘要，不含正文。
- 详情：`{ok, project:{project_id,title,revision,created_at,updated_at,project}}`。
- PATCH 请求体：`{base_revision, project}`（剪辑为 `{base_revision, edit}`），
  可选 `client_mutation_id`；响应回显 `client_mutation_id`，可能带
  `snapshot_warning`（数据库已提交但可重建快照写失败）。
- 状态码：400 格式错误 / 404 找不到 / 409 版本冲突 / 422 业务预检失败。
- 错误体：`{error:{code,message[,field_path][,segment_id][,pin_id][,clip_id][,server_revision][,detail]}}`。
- 文档正文新增 `schema_version`（=1），与数据库 `SCHEMA_VERSION`（=2）分开演进。
- segment_id / clip_id 是稳定身份；服务端不再改写，重复即拒绝。

## 实际验证

```powershell
python tests/run_all.py b01
D:\ComfyUI_windows_portable\python_embeded\python.exe tests/test_b01_http_aiohttp.py
```

| 测试 | 环境 | 结果 |
| --- | --- | --- |
| `test_b01_migration.py` | 系统 python | 12/12 通过 |
| `test_b01_project_identity.py` | 系统 python | 21/21 通过 |
| `test_b01_revision.py` | 系统 python | 17/17 通过 |
| `test_b01_routes.py` | 系统 python | 22/22 通过 |
| `test_b01_http_aiohttp.py` | ComfyUI 自带 python（**真实 aiohttp + 真实 HTTP**） | 14/14 通过 |
| `_smoke_db/assets/routes/pins/prompts/generation.py` | 系统 python | 全部通过 |

覆盖的验收点：

- 空库新建项目能出现在列表（`total==1`，items 只有摘要）。
- ABC 调序为 CAB：显示顺序变为 C,A,B，而 `{id_a:A, id_b:B, id_c:C}` 映射不变。
- 删除 B 后剩余段 ID 仍是 `[id_c, id_a]`，B 的 ID 没有被复用。
- 头部插入新段不改动既有段 ID。
- 两客户端同 revision 保存：A 成功 revision+1，B 得 409 且
  `server_revision==2`，服务器内容仍是 A 的版本。
- 真实 aiohttp 下 `/h3chain/v2/*` 与 `/api/h3chain/v2/*` 均 200；
  旧 `/h3chain/projects` 由新版看是 404（不再被抢答）。
- 迁移：v1 库升 v2 后 `schema_version` 补齐、segment_id 与段内容保留、
  revision 不变；重复执行两次数据字节不变；库版本超前时显式 RuntimeError。

实测发现并修复的一个真实缺口：`list_projects` / `list_edits` 此前忽略
`size` 查询参数（永远返回 50），已统一走 `_page_size()`。

**未做**：浏览器/真机验证——`docs/BASELINE_B00.md` 第 3 节已记录
工作区与服务目录是两份物理副本，同步命令在该文档；本批没有重启用户的
ComfyUI 实例，因此运行中的服务仍是旧代码。

## 未完成

1. **未在运行的 ComfyUI 上实测**：改的是工作区副本。要生效需执行
   `docs/BASELINE_B00.md` 第 3 节的 robocopy 同步并重启 ComfyUI。
2. **前端尚未切到 v2**：`web/h3_director.js`（旧导演台）目前仍用旧
   `/h3chain/*` 接口，`web/h3_studio/store.js` 与各视图会自动走 v2，
   但两边并存的状态要到 B03 收口。
3. **截图验证缺失**：用户已明确取消，U01/U03 与 B03 的响应式验收无法做视觉确认。
4. **静默截断只修了段和轨道**：`clean_prompt` 对 references(16)、
   subjects(16)、retention、screen_texts(16)、ref_usage(32) 等仍是静默截断；
   图钉每段上限 24 也是硬截。这些留到各自职责批次（B05/B07）处理。
5. **迁移只有 v2 一级**：后续批次加字段时应在此框架上继续加，不要另起炉灶。
6. 旧 `routes.py` 的 `/h3chain/*` 未被改动，仍服务旧入口（计划要求保留）。

## 下一批：B02

需要读取的函数与文件：

- `web/h3_studio/store.js`（`:107`、`:173` 的摘要覆盖正文与保存响应直接替换草稿）
- `web/h3_studio/api.js`（本批已换成 `fetchApi`，B02 要加请求 epoch 与取消）
- `web/h3_studio/generation/view.js`（`renderRefBox` 的 `prompt` 遮蔽、
  `renderTop()` 的 `pid()` 误用、`renderPins` 缺编辑器、草稿变更重建整栏）
- `web/h3_studio/editor/view.js`、`web/h3_studio/entry.js`（订阅与定时器回收）

验收目标：中文连续输入不丢字不失焦、2 秒慢响应下三次编辑全部保留、
保存期间切换项目不串写、关闭/打开 20 次监听数不累计。
