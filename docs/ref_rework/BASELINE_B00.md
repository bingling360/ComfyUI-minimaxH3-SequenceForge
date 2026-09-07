# B00 基线冻结记录

日期：2026-09-07。对应计划《DIRECTOR_DESK_REWORK_PLAN.zh-CN.md》第 11 节 B00。
本文只做记录与环境准备，**没有修改任何功能代码**。

## 1. 版本控制状态

- HEAD：`50165b17578e976c112e57eb57454bda20bf52d8`
- 已跟踪文件改动：`__init__.py`（+32/-1，仅新增 H3 Studio 路由注册）
- 未跟踪（**保留，未 reset / 未 clean**）：

```text
_pkg_stubs/
_smoke_assets.py  _smoke_db.py  _smoke_editor.py  _smoke_generation.py
_smoke_pins.py    _smoke_prompts.py  _smoke_routes.py  _smoke_runs.py
docs/DIRECTOR_DESK_REWORK_PLAN.zh-CN.md
h3_prompt_expander/
h3_studio/
output/
studio_node.py
web/h3_studio/
```

## 2. 运行环境

| 项 | 值 |
| --- | --- |
| ComfyUI 目录 | `D:\ComfyUI_windows_portable\ComfyUI` |
| ComfyUI 版本 | `0.34.0`（frontend 1.49.6） |
| 运行方式 | `--windows-standalone-build --cpu`（**CPU，无 CUDA**） |
| 服务 Python | `D:\ComfyUI_windows_portable\python_embeded\python.exe` → 3.12.10 |
| PyAV | 18.1.0 |
| torch | 2.14.0+cpu，`cuda=False` |
| 系统 Python | 3.13.14（`C:\Users\xuan\AppData\Local\Programs\Python\Python313`），**无 av / torch**，仅用于抓取文档 |
| 系统内存 | 16 GB |
| 官方 H3 节点 | `comfy_extras/nodes_minimax_h3.py` 存在 |
| 官方 H3 模型 | `comfy/ldm/minimax/model.py` 存在 |

**约束记录**：本机 ComfyUI 以 `--cpu` 启动且 torch 为 CPU 版。因此 B08/B09/B10/B11
需要的"真实 GPU 采样"在本机**当前实例**上无法以可接受成本完成。这些批次的
真实 Comfy 队列验证仍需 GPU 实例；本机可验证的是路由、张量形状、图的构造与
队列/取消协议，不能冒充"真实采样通过"。

## 3. 验证目录与实际服务目录（关键）

运行中的 ComfyUI 加载的是：

```text
D:\ComfyUI_windows_portable\ComfyUI\custom_nodes\ComfyUI-minimaxH3-SequenceForge
```

工作区是：

```text
D:\jiaojie\ComfyUI-minimaxH3-SequenceForge
```

**两者是两份独立物理目录，不是联接/符号链接**（已用探针文件验证：在工作区
创建 `_b00_probe.txt` 后，服务目录未出现同名文件；`Get-Item -Force` 的
`Attributes=Directory`、`LinkType` 为空）。

因此：

- 在工作区改代码 **不会** 自动生效于浏览器。
- 每次需要在浏览器中验证前，必须把改动同步到服务目录：

```powershell
$src = 'D:\jiaojie\ComfyUI-minimaxH3-SequenceForge'
$dst = 'D:\ComfyUI_windows_portable\ComfyUI\custom_nodes\ComfyUI-minimaxH3-SequenceForge'
robocopy $src $dst /MIR /XD __pycache__ _pkg_stubs /XF *.pyc /NFL /NDL /NJH /NJS /NP
```

  然后重启 ComfyUI（Python 路由改动需要重启；纯前端 JS 改动刷新页面即可）。
- **`output/` 不在同步范围**：它是工作区自己的测试产物，不能覆盖服务目录的
  用户真实数据。

基线比对（robocopy `/L`）：除 B00 新增的文档外，两目录内容一致。

## 4. 路由冲突复现（F03，不运行模型即可复现）

服务已启动于 `http://127.0.0.1:8188`，实测：

| 请求 | 结果 |
| --- | --- |
| `GET /h3chain/projects` | `200 {"ok": true, "projects": []}` ← **旧 handler 胜出**（`routes.py:182`），新前端期望的 `items` 不存在 |
| `GET /h3chain/bootstrap` | `200`，新 schema v1，`current_project_id` 有值，说明新库里确有项目 |
| `GET /api/h3chain/bootstrap` | **404** ← 新 Studio 路由没有 `/api` 前缀副本，与其 `__init__.py` 文档字符串声称的"两份都挂"不符 |
| `GET /h3chain/ping` | `200`，旧路由表 |

结论：同一 `method + path` 被新旧两套注册；旧 `routes.py` 先注册因而覆盖新
`h3_studio/routes/api.py`。B01 必须把新路由迁到 `/h3chain/v2` 并注册 `/api`
前缀副本。

`GET /extensions` 同时列出 `h3_director.js` 与 `h3_studio/entry.js`（F01 证据），
确认存在第二个前端入口。

## 5. 官方 skill 快照

已下载三份官方文本到 `docs/official_h3/`，URL / SHA256 记录在
`docs/official_h3/SOURCES.md`。

## 6. 测试隔离

- 已有环境变量开关：`H3S_STUDIO_ROOT_OVERRIDE`（`h3_studio/db.py:studio_root`）。
- B00 新增 `tests/` 目录与隔离夹具（见 `tests/README.md`），所有测试写临时目录，
  **不读写用户真实项目**。

## 7. 截图

**未取得。** 用户于 B00 执行期间明确指示停止下载浏览器内核，并取消计划中的
截图验证环节。因此：

- 第 1.1 节与 B00 步骤 3 的截图、以及 B03 验收中的"1920/1440/1280/窄窗口截图
  不横向溢出"**本轮不做**。
- 所有布局相关结论只能来自 DOM/CSS/控制流证据，不能声称经过视觉实测。
- 后续若需要恢复该验证，见第 3 节的同步命令 + 任意浏览器自动化工具重新补做。
