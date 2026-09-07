# 交接：导演台重构 v2（rework-director-v2 → experimental-branch-2）

> 日期：2026-09-07（UTC+8 按仓库时间）
> 基线：`origin/experiment @ 50165b1`
> 旧 `origin/experimental-branch-2 @ 19b1445` 已按决策废弃（94 文件/17k 行大重构全不要，
> 仅 8 份思路文档归档于 `docs/ref_rework/`，旧 tip SHA 见文末，可恢复）。
> 本分支：旧链只增不改，单入口 `h3chain-director` 不变。

## 1. 锁死的决策（勿推翻重来）

1. 后端存储 = **A 纯文件增强**：`output/h3_projects/<项目>/manifest.json` 仍是唯一真相源，
   无 SQLite、无新队列。`manifest_schema=h3seamless/manifest-v2.1` + `revision` 乐观锁。
2. 素材 = 总量不限（999 封顶）+ 单段执行期卡 9 图/3 视/3 音；分发走单总闸节点。
3. Hub 形态 = **A 单总闸**：`H3AssetHub`（JSON 进、规范包出，连主节点 `资产包` 输入）。
4. 首版范围 = **B 暂缓**：二采/实验面板原样保留不迁；先保生成+资产+成片+提示词。
5. 旧项目 = **A 自动迁移**：缺 `revision` 的老 manifest 读时懒补 `revision=1`。
6. latent 三源并存：自动存档 `.pt`（既有）+ 段范围切片 + 输入/成片现抽节点。
7. 前端 = B 拆小文件，但保持 ComfyUI 自动加载 `web/*.js` 全局脚本形态，不注册新入口。

## 2. 本次改动清单（vs origin/experiment）

已改 7 文件（`git diff --stat`：+744/-43）：

| 文件 | 改动 |
|---|---|
| `projects.py` | `MANIFEST_SCHEMA`/`_ensure_revision`；`create_project revision=1`；`save_prompts(..., base_revision)` 冲突抛 `REVISION_CONFLICT`；`list_projects_summary` 分页；`save_assets` 资产库；`slice_latent/delete_latent/trim_asset`；`seg_fields` 透存 `prompt_v2` + `latent_save`；refs 上限 16→64 |
| `routes.py` | 16 路由（旧 12 + `compile/assets/asset_check/latent_slice/latent_delete/trim`）；`ping→v4-manifest-v2.1`；`{code,message}` 结构化错误；`?summary=1`；409 带 `server_revision` |
| `nodes.py` | 池加载失败点名标签+文件名；refs 缺省且总量超限时要求段卡勾选（原来硬套全部导致总量被 9/3/3 卡死）；主节点新增可选输入 `资产包`（Hub 规范包优先，空走旧路） |
| `checkpoint.py` | `_atomic_write` 加 fsync + 24h `.part` 清理 |
| `media.py` | 新增 `probe_fps/trim_av_mp4`（入出点裁剪） |
| `__init__.py` | 注册 4 节点（+`H3AssetHub` +`H3LatentExtract`） |
| `web/h3_director.js` | 仅两处增量：左栏末 `<details>` v2 扩展区（`renderV2Section`，默认收起）+ 末尾 `__H3_V2_MODULES__` 标记 |

新建：`asset_hub.py`（165行，总闸校验）、`latent_tools.py`（148行，现抽节点）、
`prompts.py`（509行，具象化编译+校验+旧迁移）、`web/h3_api.js/h3_prompts.js/h3_assets.js/h3_latent.js`
（全局脚本，无 `registerExtension`）、`tools/h3_prompt_expander/`（28 文件，原样搬入的独立 CLI）、
`tests/test_rework_v2.py`（16 用例）、`docs/ref_rework/`（8 文件只读参考）、本文档。

主链未动：桥裁头、响度对齐（`qc.loudness_align_head`）、记忆锚、合并编码逻辑原样。

## 3. 接口速查（前缀 `/h3chain/`，均含 `/api` 副本）

GET `ping/projects?summary=1/project/upscale_models/experiments`；
POST `create_project/save_prompts(+base_revision)/compile/assets/asset_check/latent_slice/latent_delete/trim/delete_project/delete_file/merge/upscale_reset/redo_cancel`。
冲突一律 `409 {code:REVISION_CONFLICT, server_revision}`，前端刷新重试。

## 4. 验证状态（已绿 / 待新电脑补）

- [x] `python -m pytest tests/test_rework_v2.py -q` → **16 passed**（本机：M1 revision/CAS/迁移/分页；M2 总量20过/单段10拦/缺文件点名；M2.5 T2VA/Ref2VA/迁移/校验；M3 切片往返/空窗/冲突/删除/trim参数；routes 16双挂+handler冒烟；单入口；expander离线validate）
- [x] 真机 Comfy：`/extensions` 仅导演台系 6 文件；`ping=v4` 16 路由；`create→rev1`；`save rev1→rev2`；旧 rev 报 409+server_revision；`assets` 入库 rev3；`asset_check` 缺文件 `E_FILE_MISSING`；`compile` 出 `T2VA+[Shot 1]+N/A`
- [ ] trim 真编码（本机缺 PyAV，Comfy 自带——跑一段出 mp4 后调 `/trim` 验证）
- [ ] `H3AssetHub` / `H3LatentExtract` 在 Comfy 画布内实际执行
- [ ] 浏览器 v2 扩展区四组按钮（编译预览/资产保存校验/切片列表/裁剪列表）
- [ ] `prompt_v2` 分组表单（见 §5.1，做之前先玩透上面三项）

## 5. 后续工作（按顺序）

### 5.1 prompt_v2 分组表单（约 300 行 DOM，大活）
段卡从三框改分组折叠：画面（媒介/构图/环境/光照/角色/道具）/ 镜头（Shots：描述+运镜三维+对白S1/S2+屏显+剧中音乐+本镜引用）/
声音（环境音/配乐N/A）/ 参考（素材引用+主体+任务类型+retention）/ 高级（源码覆盖）。
头显模式徽（调 `detect_mode`），提交前调 `validate_compiled`，errors 红定位、warnings 黄条。
写回经 `save_prompts.segments[].prompt_v2`（后端已透存）。注意中栏重建签名 `cardsSignature` 防丢焦。

### 5.2 真机补验（§4 未勾项）
出一段后：`latent_slice {seg:0,0~48帧→head.pt}`、`trim {seg_000.mp4,0~2s}`、Hub→主节点单线连通。

### 5.3 expander 接线
段卡 `[AI扩写]`：`intent_zh → tools/h3_prompt_expander/normalize → 确认卡 → h3_expand → 贴回 shots/soundscape/music`。
需配 key（`$env:ZHIPU_API_KEY`），无 key 走 `h3-dialect.md` 手工 + `validate.py`。确认环强制，不许跳过。

### 5.4 二期（暂缓）
二采/实验面板迁移；Hub 张量直传（当前 Hub 只传 JSON 包文件名，解码仍走 input 目录）。

## 6. 新电脑起手式

```powershell
git clone https://github.com/bingling360/ComfyUI-minimaxH3-SequenceForge.git
cd ComfyUI-minimaxH3-SequenceForge
git checkout experimental-branch-2
python -m pytest tests/test_rework_v2.py -q     # 应 16 passed
robocopy . <ComfyUI\custom_nodes\ComfyUI-minimaxH3-SequenceForge> /MIR /XD .git output
# 重启 ComfyUI，硬刷新浏览器
curl localhost:8188/h3chain/ping               # version=v4-manifest-v2.1
curl localhost:8188/extensions                  # 仅导演台系 6 文件
```

> 双目录陷阱（B00 踩过）：工作区改完必须 `robocopy /MIR` 到运行目录 + 重启 Comfy，
> 否则调的是旧代码。`output/` 不同步，不在 robocopy 内。

## 7. 可恢复性
废弃的旧重构 tip：`19b1445bce699489bfddb3cf2748bc4d3517d151`（`origin/experimental-branch-2` 推送前）。
本文档提交后该分支指针指向新工作（含 50165b1 全部历史，`git log` 可查），旧对象仍可按 SHA 找回。
