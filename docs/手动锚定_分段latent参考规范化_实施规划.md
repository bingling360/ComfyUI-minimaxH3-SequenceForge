# 手动锚定（Anchor Studio）实施规划 v3 — 终稿

日期：2026-09-16　分支：`experimental-branch-2`（HEAD `c6b6b79`）
取代 v1（分阶段渐进）、v2（一次性重构）。本文为**执行依据**。

> ⚠️ **通读前提**：本仓库存在大量历史残留注释，其中若干"必须/不能"的理由是**错误的**
> （如"参数不一致会导致画风混搭"、"下游不重做接缝会断"）。判断依据一律回到
> **latent 张量本身——只有形状（C/H/W）是硬约束，其余都是策略选择。**

---

## 0. 决策总表（13 项，全部锁定）

| # | 议题 | 决策 |
|---|---|---|
| 1 | 中间锚 vs 首尾锚 | **语义完全相同**，都是官方 AddGuide 的 keyframe，落点自由 |
| 2 | 多锚与漂移 | 锚可多选，不做防漂移处理（规范化能力，非高频路径） |
| 3 | 362 帧编码上限 | 不是问题 —— 统一「**先裁后编**」，不直接编大视频 |
| 4 | 分辨率不匹配 | 有源 → 现编（同样先裁后编）；无源 → **硬报错** |
| 5 | 迁移方式 | 碎片**一次性全迁**，旧结构一次清干净 |
| 6 | 中间锚音频 | **带**，`crop_audio_for_anchor` 按剩余时长裁 |
| 7 | 图像/音频分离 | 源轨三态：图像+音频 / 仅图像 / 仅音频 |
| 8 | 插入视频是否进成片 | **不进成片**，完全合并为 `src.kind="video"` 的 anchor，不再是段类型 |
| 9 | 迁移比对 | 建回滚点 + 样本留底，**不做双跑 diff**，直接看功能是否生效 |
| 10 | 「画风混搭」 | **伪问题**。参数只影响未来要采样的段，与已存 latent 无耦合 |
| 11 | 「下游接缝会断」 | **伪问题**。下段已生成，其开头 latent 在盘上，可反向当锚 |
| 12 | 级联重做 | **不是默认行为**。重做最小单位 = 单段，双锚对齐邻居 |
| 13 | 唯一硬约束 | **只有分辨率**（C/H/W 不匹配 → 桥拼不上） |

---

## 1. 技术基础（已核实，非推测）

### 1.1 官方锚定语义

`guides.py` 已逐行对齐官方 `MiniMaxH3AddGuide`。keyframe 就是
`{resolved_frame_index, latent, audio_latent}`，多锚 = 列表多条。

| 维度 | 约束 | 出处 |
|---|---|---|
| 引导片段**帧数** | ≥5 帧无条件向下对齐 17k+5（5/22/39/56/73…）；<5 帧退化为 1 帧 | `guides.clip_guide_frames` |
| 落点 `resolved_frame_index` | **任意整数**，负值自尾部计数 | `guides.resolve_frame_index` |
| 越界 | `idx + guide_frames <= frame_count` | `guides.validate_anchor` |
| 音频 | `max_rt = floor(audio_t - FRAME_RESCALE × idx)` | `guides.crop_audio_for_anchor` |

**一句话：宽度受 17k+5 约束，位置完全自由。**

### 1.2 latent 有「帧数」，没有「帧率」

| 信息 | 来源 | 精确性 |
|---|---|---|
| 总帧数 | `latent_t_to_frames(shape[2])` | ✅ 精确，纯数学 |
| token 边界 | `grid.token_start_frames(T)` | ✅ 精确，**不等距**（首格 1 帧，其后每格 4 帧） |
| 秒数 | `帧 ÷ fps` | ⚠️ 依赖 fps |
| 缩略图 | latent 无像素 | ❌ 需额外产物 |

**帧率不在 latent 里**（时间维是 token，`FRAME_PER_TOKEN=(1,4,4,4,4)`），
所以帧率归一**只能在解码阶段做**。

- 段 latent fps **恒等于 24**（`save_segment_mp4` 默认 `fps=24`）→ **段 latent 无帧率问题**
- 库 latent 才需要 fps 元信息

### 1.3 分辨率是唯一硬约束

`PackedLayout` 按行预留，C/H/W 不匹配会在模型层直接崩链。

| 情况 | 处理 |
|---|---|
| C/H/W 匹配 | 直接用 |
| 不匹配 + 有源 | 帧窗解码 → `_center_cover` → `video_vae.encode`（先裁后编） |
| 不匹配 + 无源 | **硬报错** + 转档指引 |
| 裸 latent 需放大 | `H3LatentUpscale`（只能放大） |

> 现状 `_load_library_latent` 的"静默回落上段尾"**必须删除** —— 设了锚不生效还不说，最坏的一类 bug。

---

## 2. 语义收敛：七处碎片 → 一个 anchor

现在这些东西散在七处，**本质都是"把某段 latent 钉到本段某位置"**：

| 碎片 | 位置 | 等价 anchor |
|---|---|---|
| 头部桥 `latent_ref` | nodes.py:1208 | `{at.mode: "head"}` |
| 段尾锚 `tail_src` | nodes.py:1248 | `{at.mode: "tail"}` |
| 实验 `mid_anchor` | nodes.py:2780 | `{at.mode: "mid"}` |
| 实验 `e1_bridge_shard` | nodes.py:2756 | 同 src 展开多窗 |
| 实验 `e2_memory_anchor` | nodes.py:2768 | `{src.kind: "segment", ref: 首段}` |
| 插入视频 `ds.inserts` | nodes.py:1295 / 2507 | `{src.kind: "video"}`，**不进成片** |
| 重摇四模式 `_REDO_MODES` | nodes.py:319 | 临时 anchor 覆盖层（四种锚定策略组合） |

### 段类型收敛为两种

| 类型 | 进成片 | 说明 |
|---|---|---|
| `prompt` 段 | ✅ | 采样生成 |
| `prologue` 序章 | ✅ | 链首已有视频，走节点接线 |

**「外部素材段」这第三种类型不再存在。**

### 旧存档：不兼容（Q2 已决策「不救」）

旧存档的 `manifest.inserts` 占用真实段号，`seg_NNN.pt` 也按含插入槽的编号落盘。
**决定不迁移**——旧项目直接放弃，省掉"重命名段文件"这块风险最高的活（不写 `migrate_inserts.py`）。

运行时检测：manifest 存在非空 `inserts` → **直接报错，不静默降级**：

> 该存档使用了已移除的「插入视频」功能（段 N 为插入槽）。插入视频已改为「手动锚定」，
> 不再作为独立段。请新建项目重跑。

---

## 3. anchor 数据结构

```python
anchor = {
  "id": "a1",
  "src": {
    "kind": "prev_tail | segment | library | video | image",
    "ref": "seg_003 | latent/head.pt | 素材标签",
    "start_f": 10, "end_f": 32,   # 源内像素帧窗（左闭右开），先裁后编
    "src_fps": 24.0,               # 源真实帧率；null = 未知
    "meta_ok": true,               # 元信息完备度（决定 UI 降级层级）
  },
  "at": {"mode": "head | tail | mid", "frame_idx": 0},  # 目标段内落点，自由
  "window": 22,                    # 17k+5 档位：5 / 22 / 39 / 56…
  "branches": {"av": "both | video | audio"},
  "on": True,
}
```

**落点推导**（新增 `anchors.anchor_frame_index`）：

| mode | `resolved_frame_index` |
|---|---|
| `head` | `0` |
| `tail` | `frame_count - window`（负值自尾部计数亦可） |
| `mid` | 用户指定 `frame_idx`（任意整数） |

**执行路径**：`seg.anchors[]` → `resolve_anchor_source()` → `guides.prepare_anchor()` → `minimax_keyframes`

`_apply_guide` 需从"单 guide + 若干 kf"重构为**接受 anchor 列表**。

### latent 落盘规范（新增元信息）

```python
{"file": "latent/head.pt", "src": "a.mp4", "start_f": 0, "end_f": 22,
 "kind": "av",            # av | video | audio（已有）
 "fps": 30.0,             # 新增：源真实帧率；null = 未知
 "w": 1280, "h": 720,     # 新增：像素分辨率
 "tokens": 7,             # 新增：shape[2]
 "frames": 22,            # 新增：冗余存，UI 免算
 "sheet": "latent/head.sheet.png", "tiles": 12}   # 新增：contact sheet
```

段 latent 的 fps 恒记 24。

---

## 4. 变更检测与重做模型（核心改动）

### 4.1 重做最小单位 = 单段，双锚对齐邻居

重做段 N 时：

- **上锚** = 段 N-1 存档 latent 的**尾部**（`guide` / `_redo_prev_bridge`，已有）
- **下锚** = 段 N+1 存档 latent 的**头部**（`_redo_next_anchor` → `_seam_tail_kf`，已有，
  重摇「仅锚下段」正在用）

双锚一夹，重做完塞回原位，接缝连续。

### 4.2 区间重做模型

| 情况 | 重做区间 | 锚定 |
|---|---|---|
| 改了段 5（孤立） | `[5,5]` | 双锚：段 4 尾 + 段 6 首 |
| 改了段 5、6（连续） | `[5,6]` | 区间内串联；两端：段 4 尾 + 段 7 首 |
| 改了段 5 和段 8（离散） | `[5,5]` + `[8,8]` | 各自双锚 |
| 用户「从这段继续」段 5 | `[5, 末尾]` | 首端锚段 4 尾；末端无下段 → 回落本段尾锚 |
| 末段变更 | `[末,末]` | 上锚 + 本段尾锚（尾帧图 / `tail_src`） |

### 4.3 触发与范围

| 触发源 | 重做范围 |
|---|---|
| **内容变更**（提示词 / 段级引用·时长·unlink·首尾帧 / anchor / 素材） | 只重做**变更的段**，合并成区间后双锚重建 |
| **用户主动**「从这段继续」 | 显式区间 `[N, 末尾]` |
| **参数变更** | **不触发**，只影响此后新采样的段 |
| **分辨率变更** | 硬约束：整链重做或新建链 |

| 改什么 | 归属 | 后果 |
|---|---|---|
| **分辨率 `width`/`height`** | `ckpt_params` | ⚠️ **唯一硬约束**：整链重做或新建链 |
| `steps` / `cfg` / 采样器 / 调度器 / 模型 / `fade_ratio` / `gate` / 实验开关 | `ckpt_params` | **不拦截、不重做** |
| 禁用段 `disabled` | 故意不进段哈希 | 零重做成本 |
| 重摇阈值 / 重摇上限 / 锚定增强 | `seam_refine`，不进指纹 | 无触发 |

> 佐证：模型权重根本不在 `ckpt_params` 里——换模型连检测都没有，而它对画风影响最大。

**`assert_match` 收窄为只校验 `width`/`height`**，其余键变化 → 记录 diff →
报告「参数已变更：steps 20→30；仅影响此后新生成的段」→ 继续执行。

### 4.4 连带简化

- `checkpoint.truncate` 的级联用途收缩到只剩三种：分辨率变更、用户主动区间、序章变更。
- 改提示词不再 cascade → 顺带消掉了"误清重摇标记"的大部分触发场景。
- **重摇标记与「改提示词自动重做」合并**：机制同为"重建该段 + 锚定邻居"。

---

## 5. UI：双轨时间线

```
┌─ ① 源轨 — 从素材里选哪一段 ─────────────────────────────────┐
│  [帧缩略图条 · token 刻度（不等距）]                            │
│       ├────[ 22 帧选取框 ]────┤                               │
│  窗宽 [5][22][39][56]   ← 锁 17k+5 档位，不连续拖             │
│  取用 (•)图像+音频  ( )仅图像  ( )仅音频                       │
│  源：latent/head.pt · 30.0fps · 帧[10,32) · 0.73s · 22帧      │
│      （fps 未知时：帧[10,32) · ?s · 22帧 + 手填框）            │
└──────────────────────────────────────────────────────────────┘
                        ↓ 钉到本段
┌─ ② 目标轨 — 钉到本段哪个位置 ───────────────────────────────┐
│  [本段 120 帧 · 已有锚点标记]                                  │
│   [开头] [中间] [结尾]   ← 落点自由，可加多个                  │
└──────────────────────────────────────────────────────────────┘
┌─ 右栏：源选择 + 体检 ─────────┐
│  来源 [上段尾 ▾]               │
│  ✓ 分辨率 1280×720 匹配        │
│  ✓ 帧窗 22 合法 (17k+5)        │
│  ✓ 未越界 (0+22 ≤ 120)         │
│  ⚠ 音频剩余时长将被裁至 N tok  │
└───────────────────────────────┘
```

**缩略图三段降级**：① 落盘时生成 contact sheet（12 格 sprite，零 VAE 开销，主力）
② 段 latent 用 `finals/seg_NNN.mp4` 抽帧（帧序严格一一对应）
③ 无源老条目 → 刻度条 + 数字 + 「生成预览」按钮（按需 VAE 解码一次并缓存）

**token 刻度必须画出来**：宽度锁 17k+5 而 token 边界不等距，画出来约束才自解释。

---

## 6. 实施步骤

### 步骤 1：回滚点与样本留底

| # | 动作 |
|---|---|
| 1.1 | `git tag pre-anchor-refactor` |
| 1.2 | 复制 6–8 个代表性旧存档到 `tests/golden/`：含序章 / 插入视频 / 重摇 / 二采 / 断链 / 段级 `latent_ref` / `tail_src` / E1·E2·mid 全开 |

### 步骤 2：顺手修两个独立 bug（零风险，先做）

| # | 动作 | 位置 |
|---|---|---|
| 2.1 | `truncate` 误清 `redo_queue`：改为 `slot < start` 才保留 | checkpoint.py:128 |
| 2.2 | `_load_input_video` 加 fps 参数 + 秒窗换算（修 24fps 硬假设） | nodes.py:507 |

### 步骤 3：新基建（纯函数，可单测）

| # | 动作 | 位置 |
|---|---|---|
| 3.1 | 新增 `anchors.py`：归一化 / 校验 / 旧字段迁移 / 源解析 / **区间计算**，纯函数零 torch | 新文件 |
| 3.2 | `grid.py` 补 `SNAP_WINDOWS` / `snap_window_down()` / `anchor_frame_index()` | grid.py |
| 3.3 | `anchors.migrate_legacy_seg()`：旧 `latent_ref`/`tail_src`/`auto_ref`/`last_frame` → `anchors[]` | anchors.py |
| 3.4 | `pytest tests/test_anchors.py` 全绿（含迁移往返一致性、区间合并用例） | tests/ |

### 步骤 4：源归一化与落盘规范

| # | 动作 | 位置 |
|---|---|---|
| 4.1 | 新增 `resolve_anchor_source()`：匹配直用 / 有源先裁后编 / 无源硬报错 | anchors.py |
| 4.2 | latent 落盘补 `{fps, w, h, tokens, frames}` + contact sheet | latent_tools.py / checkpoint.py |
| 4.3 | latent 库条目元信息回填（老条目按 §1.2 三层兜底） | 同上 |

### 步骤 5：变更检测改造（区间重做引擎）

| # | 动作 |
|---|---|
| 5.1 | `assert_match` 收窄为只校验 `width`/`height` |
| 5.2 | `reroll_start` 改为**返回变更区间列表**（不再是单一 start） |
| 5.3 | 区间内串联 + 两端双锚；离散区间各自双锚 |
| 5.4 | 「从这段继续」按钮 → 显式区间 `[N, 末尾]` |

### 步骤 6：主编排切换

| # | 动作 |
|---|---|
| 6.1 | 七处碎片执行点全部改为读 `seg.anchors[]` |
| 6.2 | `_apply_guide` 重构为接受 anchor 列表 |
| 6.3 | 序章编码改走 `resolve_anchor_source` |
| 6.4 | anchor 序列化进段哈希 → 标记为内容变更 → 走区间重做 |
| 6.5 | **验收（看功能生效，不做 diff）**：① 外部视频裁窗编码后下一段确实被带着走；② head/mid/tail 三落点生效；③ A/V 三态生效；④ 改 anchor 触发该段重建、下游不动；⑤ 分辨率不匹配硬报错；⑥ 改 steps 不触发任何重做 |

### 步骤 7：清理

按 §7 删除清单一次删净；旧存档检测改为直接报错（见 §2，不写 `migrate_inserts.py`）。

### 步骤 8：UI

| # | 动作 |
|---|---|
| 8.1 | **先抽 `web/h3d_anchor.js`**（否则又在 432KB 单文件里打架） |
| 8.2 | 双轨组件 + A/V 三态 + token 刻度 + 体检右栏 |
| 8.3 | 报告回显：`锚点：源 seg_003 帧[10,32) → 本段帧 0，22 帧窗，图像+音频` |

---

## 7. 删除清单

| 删除项 | 位置 | 去向 |
|---|---|---|
| `seg.latent_ref` | nodes.py:1208 | → `anchors[]` |
| `seg.tail_src` | nodes.py:1248 | → `anchors[]`（`at.mode="tail"`） |
| `seg.auto_ref` / `seg.unlink` 独立分支 | nodes.py:1190 | → unlink 降级为 UI 快捷开关（= 不生成默认 head anchor） |
| 旧全局 `last_frame` 回退 | nodes.py 锚解析处 | → `anchors[]` |
| 实验 `mid_anchor` | nodes.py:2780 | → `at.mode="mid"` |
| 实验 `e1_bridge_shard` | nodes.py:2756 | → 同 src 多窗 |
| 实验 `e2_memory_anchor` | nodes.py:2768 | → `{src.kind="segment", ref=首段}` |
| `_load_library_latent` 静默回落分支 | nodes.py:1461 | → 硬报错 |
| `seg_latent_save.mode` 的 `range`/`tail` | nodes.py:1236 | → 收敛为 `all \| off`（用时再裁） |
| `ds.inserts` 解析与校验 | nodes.py:1295-1322 | 整个删除 |
| 插入段执行分支 | nodes.py:2507-2605 | 整个删除 |
| `exec_items` 的 `"insert"` 类型 | nodes.py:1316 | → 只剩 `"prompt"` |
| `proj_inserts` / manifest `inserts` | nodes.py:1786 / 2586 / 3146 | 删除 |
| `_insert_hash` | nodes.py:1770 | 删除 |
| `projects.py` `[插入视频]` 占位行 | projects.py:1065 / 1110 | 删除 |
| 前端 `appendInsert` / `pickInsertVideo` stub | web/h3_director.js:2349-2357 | 删除 |
| `assert_match` 的非分辨率校验 | checkpoint.py:243 | 收窄为只校验 width/height |
| `reroll_start` 的单一 start 语义 | checkpoint.py:102 | → 返回区间列表 |
| README「插入视频段」章节 | README.md:27 / 338 / 363 / 371 / 373 | → 改写为「手动锚定」 |

**保留做参考**：`git tag pre-anchor-refactor` + 本文档 + 旧字段迁移函数（读旧档用）。

---

## 8. 风险清单

| 风险 | 后果 | 对策 |
|---|---|---|
| 拖动宽度不锁档 | 静默裁到 5/22 | UI 只给档位按钮，不连续拖宽 |
| 手动锚软降级 | 设了不生效还不说 | 手动锚**必须硬报错** |
| anchor 变更不进哈希 | 改了不重做 | 序列化进段哈希（步骤 6.4） |
| 旧存档含 inserts | 老项目打不开 | 已决策**不迁移**；运行时直接报错 + 指引新建项目，不静默降级 |
| 中间锚与首锚冲突 | 段内跳变 | 不做处理（已决策），UI 显示多锚提示 |
| 窗超 362 帧 | 爆显存 | 先裁后编 + `MAX_ENCODE_FRAMES` 护栏 |
| 432KB 单文件改 DOM | 必然回归 | 步骤 8.1 先拆 `h3d_anchor.js` |
| 多锚越界 | 模型层形状错位 | 手动模式下 `guides.audit_keyframes` 改为**拦** |
| latent 无 fps 强显秒数 | 显示错时长 | fps=null → 秒数显示 `?`，主刻度用帧 |
| 区间重做边界算错 | 该重做的没重做 / 重做了不该重做的 | 步骤 3.4 单测覆盖：孤立 / 连续 / 离散 / 末段 / 首段五种 |

---

## 9. Q1–Q4 决策记录（已全部拍板）

| # | 议题 | 决策 |
|---|---|---|
| Q1 | 改 anchor 是否自动重建该段 | **自动**。anchor 直接影响生成结果，等同内容变更 → 标记该段重建；**只重建该段，不级联** |
| Q2 | 旧存档要不要救 | **不救**。运行时检测 `inserts` 非空 → 直接报错 + 指引新建项目；不写 `migrate_inserts.py` |
| Q3 | 分支策略 | **另开 `anchor-studio` 分支**（自 `c6b6b79` 切出）；回滚 tag `pre-anchor-refactor` 同指该提交 |
| Q4 | contact sheet 生成时机 | **同步生成**（见下方说明） |

### contact sheet 是什么

摄影术语「接触印相样片」：把一卷胶卷的缩略图印在同一张纸上，便于快速选片。

在本项目里：把 latent 对应的画面**均匀抽 12 张缩略图，横向拼成一张长条 PNG**
（`latent/<name>.sheet.png`）。时间线只需加载这一张小图，按帧号算出格号 `k`，
裁出第 `k` 格显示即可。

- **不加载 VAE、不解码 latent** → 时间线秒开
- 成本极低：编码 latent 时源帧已在内存，抽 12 张拼图几乎不耗时
- 类比：视频网站进度条 hover 弹出的预览小图，原理相同
- 老条目无 sheet → 退化为刻度条 + 数字，带「生成预览」按钮（按需 VAE 解码一次并缓存）

---

## 10. 执行进度

| 步骤 | 状态 |
|---|---|
| 1 回滚点与样本留底 | ✅ tag `pre-anchor-refactor` + 分支 `anchor-studio`（均 @ `c6b6b79`）；样本留底待做 |
| 2 顺手修两个独立 bug | ✅ 2.1 已完成 + 回归测试 5/5 通过；**2.2 已决定并入步骤 4**（见下） |
| 3 新基建 `anchors.py` | ⏸ 未开始 |
| 4 源归一化与落盘规范 | ⏸ 未开始 |
| 5 变更检测改造 | ⏸ 未开始 |
| 6 主编排切换 | ⏸ 未开始 |
| 7 清理 | ⏸ 未开始 |
| 8 UI | ⏸ 未开始 |

### 步骤 2 调整说明

- **2.1（`truncate` 误清重摇标记）已修**：`checkpoint.truncate` 改为按 slot 过滤
  （`slot < start` 保留），并新增 `tests/test_checkpoint_truncate.py`（5 用例全过）。
- **2.2（`_load_input_video` 无 fps 归一）并入步骤 4**：查调用方后发现只有两处——
  资产池（nodes.py:1916/1924）与插入视频（nodes.py:2518）。后者会在本次重构中整体删除，
  前者的 fps 归一属于 `resolve_anchor_source()` 的职责范围。
  **现在改会立刻被重构覆盖，故并入步骤 4 一并做。**
- 顺带修正了 `checkpoint.py` 头部 docstring 里"其后段必然级联重做"的错误注释。

---

## 11. 环境备注（本机实测）

| 用途 | 解释器 | 说明 |
|---|---|---|
| 跑**不依赖 torch** 的测试 | `C:/Users/xuan/.workbuddy/binaries/python/versions/3.13.12/python.exe` | 有 pytest，无 torch |
| 跑**依赖 torch** 的测试 | `D:/ComfyUI_windows_portable/python_embeded/python.exe` | 有 torch，**无 pytest** |

→ 两者都不完整：`test_rework_v2.py` 这类 `import torch` 的测试**当前环境跑不了**。
需要时先给嵌入式 Python 装 pytest，或给 managed Python 装 torch。

---

# 附录 A：执行上下文速查（换机续做用）

> 本附录目标是**自包含**：换一台机器、开一个新会话，读 `§0` + `§9` + `本附录`
> 即可开工，不必重新通读代码。所有行号基于 **commit `c6b6b79`**，会漂移，
> 定位请用**函数名**。

## A.1 仓库状态

| 项 | 值 |
|---|---|
| 远端 | `git@github.com:bingling360/ComfyUI-minimaxH3-SequenceForge.git`（SSH 通，HTTPS 不通） |
| 当前分支 | `anchor-studio`（自 `c6b6b79` 切出） |
| 回滚点 | tag `pre-anchor-refactor` @ `c6b6b79` |
| 已完成 | 步骤 1（tag+分支）、步骤 2.1（`truncate` 修复 + 5 测试） |
| 下一步 | 步骤 3：新建 `anchors.py` 纯函数 + `grid.py` 补函数 + `tests/test_anchors.py` |

## A.2 现状代码地图

### guides.py —— 官方锚定语义（**已对齐，不要改语义**）

| 函数 | 行 | 要点 |
|---|---|---|
| `resolve_frame_index` | 38 | 负值自尾部计数 |
| `clip_guide_frames` | 44 | ≥5 帧向下对齐 17k+5；<5 → 1 |
| `latent_frames_of` | 56 | `[B,C,T,H,W]` → 像素帧数 |
| `validate_anchor` | 63 | `idx + guide_frames <= frame_count` |
| `crop_audio_for_anchor` | 74 | `max_rt = floor(audio_t - FRAME_RESCALE × idx)` |
| `build_keyframe` | 91 | 只放非 None 分支 |
| `mid_anchor_index` | 101 | 单帧锚位置（比例定位） |
| `audit_keyframes` | 113 | **只报不拦**（手动模式要改成拦） |
| `prepare_anchor` | 131 | 一站式：解析 → 校验 → 裁音频 → 组装 |

### grid.py —— 纯数学（无 torch，可单测）

`FRAME_PER_TOKEN=(1,4,4,4,4)` / `FRAME_RESCALE=5/3`（7-11）
`align_frame_count` 14 / `align_frame_count_down` 21 / `video_latent_t` 30 /
`latent_t_to_frames` 35 / `frames_to_latent_t` 40 / `audio_tokens_for_frames` 57 /
**`token_start_frames` 62**（UI 画 token 刻度用）

### nodes.py —— 主编排

| 函数 | 行 | 要点 / 去向 |
|---|---|---|
| `_tail_keyframe` | 222 | 上段尾 ctx 帧 latent 直切；`resolved_frame_index=0` |
| `_center_cover` | 245 | `[F,H,W,3]` → cover-crop 到目标画幅 |
| `_encode_audio_latent` | 428 | 重采样到 VAE sr 后整段编码再 `[:tokens]` |
| `_decode_video_file` | 495 | `InputImpl.VideoFromFile().get_components()` |
| `_load_input_video` | 507 | **无 fps 归一**（bug，并入步骤 4） |
| `_REDO_MODES` | 319 | `("双锚","仅锚上段","仅锚下段","无锚")` |
| `_parse_redo_segs` | 322 | `ds.redo_segs` → `(slot, mode)`；要求 `slot < done` |
| `_redo_seed` | 360 | 与存档种子相同则 +1，保证必变 |
| `_seam_tail_kf` | 389 | 下段首 token 直切（**下锚，已有**） |
| `seg_auto_ref` / `seg_unlink` | 1190 | → UI 快捷开关 |
| `seg_latent_ref` | 1208 | → `anchors[]` |
| `seg_latent_save` | 1239 | → 收敛为 `all\|off` |
| `seg_tail_src` | 1248 | → `anchors[]`（`at.mode="tail"`） |
| `_eff_inject` | 1423 | 段级注入帧数（分段优先） |
| `_inject_guide` | 1437 | → 统一入口 |
| `_load_library_latent` | 1461 | **静默回落分支要删** → 硬报错 |
| `ckpt_params` | 1702 | `{width,height,length,ctx,steps,cfg,sampler,scheduler,chain,fade_ratio,gate}` |
| `seam_refine` | 1714 | 不进指纹 |
| `seg_hashes` | 1719-1760 | 逐段哈希（时长/引用/unlink/latent_ref/latent_save/tail_src 进哈希） |
| `use_ckpt` | 1688 | `resume or review or autosave` |
| 存档续跑块 | 1790-1869 | manifest 校验 / truncate / redo 合并 |
| `_seg_tail_anchor` | 2035 | |
| `_auto_latent_save` | 2141 | |
| `_redo_prev_bridge` | 2219 | **上锚（重摇时现算）** |
| `_redo_next_anchor` | 2242 | **下锚** |
| `_up_hi` | 2264 | 二采；`kind != "prompt"` 直接 return |
| 插入段执行分支 | 2507-2605 | **整体删除** |
| prompt 段执行 | 2637 起 | main loop |
| 锚定组装 | 2727-2751 | 重摇四模式 vs 普通段两套分支 |
| `_apply_guide` | 3527 | 签名见下，**需重构为接受 anchor 列表** |

```python
# 现状（要改）
def _apply_guide(cond, guide, sampled_fc, tail_kf_latent=None, e1_windows=None,
                 memory_kfs=None, head_kf_latent=None, mid_kfs=None):
```
内部行为：合并 `cond[0][1]["minimax_keyframes"]`，依次 append
`guide`（或 e1_windows）→ `head_kf` @0 → `memory_kfs` → `mid_kfs` → `tail_kf` @`sampled_fc-1`。

### checkpoint.py

`SCHEMA="h3seamless/ckpt-v3"` 23 / `fingerprint` 55 / `prompt_hash` 65 /
`reroll_start` 102（**返回单一 start，要改成区间列表**）/ `truncate` 115（**已修**）/
`ckpt_dir` 166 / `save_manifest` 229 / `assert_match` 243（**要收窄为只校验分辨率**）/
`contiguous_done` 279 / `save_segment` 286（**存全量 latent**）/ `load_segment` 301 /
`PROJECT_SUBDIRS` 371 = `("assets","finals","latent","texts")` / `save_thumb` 456 /
`save_segment_mp4` 479（`fps=24` 默认）

### latent_tools.py / media.py / library.py

- `latent_tools.MAX_ENCODE_FRAMES = 362`（22）—— 窗宽护栏
- `latent_tools.run_transcode_job` 199 —— **先裁后编的正确范式**（`probe_fps` + 秒窗）
- `latent_tools.H3LatentExtract` 87 / `H3LatentUpscale` 371（只能放大）
- `media.decode_av(path, start_f, end_f, fps)` 158 —— **fps 只用于 seek，不重采样**
- `media.probe_fps` 397
- `library.make_thumb` 752 —— contact sheet 可复用

## A.3 现状数据结构

**manifest 键**（nodes.py 3138-3160 写入）：
`schema, done, has_prologue, seeds, prompt_hashes, total, prompts, params, seam_refine,
experiments, memory_anchor, inserts, title, created_at, updated_at, finals, upscale,
redo_queue, assets, latents, clips, merges, seg_fields`
+ `_ml(thumbs, videos, seams, bridge_scores, seam_metrics, trims)`

**`ds.segments[i]` 字段**（web/h3_director.js:1299）：
`intent_zh, script, scene_prompt, character_prompt, soundscape, music, seconds, refs,
unlink, disabled, auto_ref, auto_seq, frame_refs, prompt_v2, latent_save, latent_ref,
tail_src, v2mode` → **新增 `anchors`**

**keyframe 字典**：
```python
{"resolved_frame_index": int, "latent": [B,C,T,H,W], "audio_latent": [...]}
```

**`_REDO_MODES`**：`("双锚", "仅锚上段", "仅锚下段", "无锚")`

## A.4 已证伪的错误注释（**不要信**）

| 位置 | 错误说法 | 真相 |
|---|---|---|
| `checkpoint.py` 头 docstring | "其后段必然级联重做" | ❌ 双锚可对齐邻居，**已修正** |
| `checkpoint.assert_match` docstring | "参数不同 = 新段与旧段画风不一致的链混搭" | ❌ 伪问题；已完成段是盘上张量，与参数无耦合。**要收窄为只校验分辨率** |
| `nodes.py:500` | 报错文案写死"可解码的 24fps 视频" | ❌ 无 fps 归一，非 24fps 素材时长/音画会漂 |
| 各处"必须级联""必须参数一致" | — | ❌ 一律回到「只有 C/H/W 是硬约束」判断 |

**可信的注释**：`guides.py` 头部关于 17k+5 的复核（2026-09 逐字核对过官方源码），是准确的。

## A.5 已知现状 bug

| # | 问题 | 处理 |
|---|---|---|
| 1 | `truncate` 误清 redo_queue | ✅ **已修**（步骤 2.1） |
| 2 | `_load_input_video` 无 fps 归一 | 并入步骤 4（`resolve_anchor_source`） |
| 3 | 插入段 `fc` 上限用全局 `length`，无视 `seg_lengths` | 插入段整体删除后自然消失 |
| 4 | `_load_library_latent` 静默回落上段尾 | 步骤 6 改硬报错 |
| 5 | 多锚越界只报不拦 | 步骤 6 手动模式改为拦 |

## A.6 验收清单（步骤 6.5，逐条实测）

1. 外部视频 → 裁窗 → 编码 → 下一段确实被它带着走
2. head / mid / tail 三落点都生效
3. A/V 三态（图像+音频 / 仅图像 / 仅音频）分别生效
4. 改 anchor 触发**该段**重建、**下游不动**
5. 分辨率不匹配 → 硬报错（不是静默回落）
6. 改 `steps` → **不触发任何重做**

## A.7 执行顺序速查

```
[ ] 步骤 3  anchors.py 纯函数 + grid.py 补函数 + tests/test_anchors.py
[ ] 步骤 4  resolve_anchor_source()（先裁后编 / fps 归一 / 硬报错）+ latent 元信息 + contact sheet
[ ] 步骤 5  区间重做引擎：assert_match 收窄 / reroll_start 返回区间 / 双锚 / 「从这段继续」
[ ] 步骤 6  主编排切换 + _apply_guide 重构为 anchor 列表 + 验收 6 条
[ ] 步骤 7  按 §7 删除清单清理（19 项）
[ ] 步骤 8  UI：先抽 h3d_anchor.js，再双轨 + A/V + token 刻度
```

**每个步骤做完跑一次**：`python -m pytest tests/test_anchors.py tests/test_checkpoint_truncate.py -q`

---

## 10. 关键文件索引

| 文件 | 作用 | 定位（用函数名，行号会漂移） |
|---|---|---|
| `guides.py` | 官方 AddGuide 语义 | `clip_guide_frames` / `validate_anchor` / `prepare_anchor` / `crop_audio_for_anchor` / `build_keyframe` |
| `grid.py` | 17k+5 / token↔frame 数学 | `align_frame_count_down` / `latent_t_to_frames` / `frames_to_latent_t` / `token_start_frames` |
| `latent_tools.py` | 现抽 / 转档 / 库内放大 | `H3LatentExtract` / `run_transcode_job` / `H3LatentUpscale` / `MAX_ENCODE_FRAMES` |
| `checkpoint.py` | 段 latent 落盘 / 变更检测 | `save_segment` / `load_segment` / `truncate` / `reroll_start` / `assert_match` / `contiguous_done` |
| `media.py` | fps 探测 / 帧窗解码 | `probe_fps` / `decode_av` |
| `library.py` | 缩略图（contact sheet 复用） | `make_thumb` |
| `nodes.py` | 主编排 | `_inject_guide` / `_load_library_latent` / `_apply_guide` / `_load_input_video` / `_redo_next_anchor` / `_seam_tail_kf` |
| `routes.py` | latent 路由 | `POST /h3chain/latent_slice` / `latent_delete` |
| `docs/导演台前端重构计划_基于成熟方案.md` | 母计划批次 E（一直 ⏸） | 本规划即其落地方案 |
