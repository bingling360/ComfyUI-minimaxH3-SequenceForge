# 上游跟进台账：Minimax H3 latent 放大网络

`upscale_net.py` 逐行移植自上游仓库的**网络结构与权重加载**部分。本文件记录
「移植了什么 / 改了什么 / 怎么跟进上游」——**改 `upscale_net.py` 前先读一遍**，
避免两件蠢事：把本地的刻意增强当 bug 修回去；重复调研上游早实现的东西。

---

## 1. 上游信息

| 项 | 值 |
| --- | --- |
| 仓库 | <https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler> |
| 默认分支 | `main` |
| 基线 commit | `d7c01b9011f2e8439493f6c02c29995a27df276f`（2026-09-02 抓取，当时即 upstream HEAD） |
| 本地 remote | `git remote add upstream https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler` |
| 快照 | `docs/upstream_snapshot/2026-09-02/minimax_h3_latent_upscaler_{2d,3d}.py` |

## 2. 移植范围

**移植**（逐行对齐，网络口径不允许自由发挥）：

- 2D 主干：`LatentResizer` / `VideoLatentResizer` + `AttnBlock` / `ResBlockEmb` / `TemporalConv`
- 3D 主干：`LatentResizer3D` + `AttnBlock3D` / `ResBlockEmb3D` / `TemporalConv3D`
- 权重加载链路：`scan_models` / `_load_raw_sd` / `_extract_upscaler_sd` / `_detect_arch` / `load_model`
- 归一化常量 `LATENTS_MEAN/STD`、`MODEL_CACHE` 缓存语义

**不移植**（我们用自己的通道，别去对齐）：

- 上游的 ComfyUI 节点类（`MinimaxH3LatentUpscaler2D/3D`）与 `UpscaleMode` 枚举 UI
  ——我们在导演台二采面板里自己做；
- 归一化/反归一化的调用位置——我们在 `upscale.py:upscale_video` 统一处理，
  因为二采要在同一个 latent 上接「低强度重采样」；
- `HAS_COMFY_MM` / `USE_NEW_API` 之类的环境探测——我们统一用 try/except 兜底。

## 3. 锚点表（本地符号 ↔ 上游位置）

本地 `upscale_net.py` 里带 `# ↑ upstream/...` 注释的五处即下表。行号以 d7c01b9 为准。

| 本地符号 | 上游位置（d7c01b9） | 本地改动 |
| --- | --- | --- |
| `LatentResizer` | `2d.py:137` | 无（口径一致） |
| `VideoLatentResizer` | `2d.py:191` | 无 |
| `LatentResizer3D` | `3d.py:215` | `forward` 拆出 `_forward_seg` 并加 temporal chunking（见 D7） |
| `_detect_arch(sd, arch)` | `2d.py:298` + `3d.py:379` | 合并 2D/3D 两套键约定，加 `arch` 形参（见 D1） |
| `load_model(name, device, precision, arch)` | `2d.py:358` + `3d.py:417` | 加 `arch` 形参 + 自动判定 + 试装择优（见 D1/D2/D4） |

未打锚点但同源的小函数：`_make_norm_tensors` / `normalization` / `zero_module`
（`2d.py:47`/`55`/`58`，`3d.py:100`/`134`/`137`），`scan_models`（`2d.py:271`、
`3d.py:349`），`_load_raw_sd`（`2d.py:279`、`3d.py:356`），`_extract_upscaler_sd`
（`2d.py:292`、`3d.py:374`）——这几处本地改动较大，差异见下节，不逐行对齐。

## 4. 与上游的差异台账（**别当 bug 修回去**）

| # | 差异 | 为什么 |
| --- | --- | --- |
| D1 | 上游 2D/3D 是**两个独立节点、两个文件**；我们合并进一个模块，并新增 `_detect_kind` 自动判定 | 一个权重下拉不该让用户先猜架构。判定顺序：面板显式 > `resizer.` 前缀 > `conv_in.weight` 维度（5=3D / 4=2D）> 试装择优 |
| D2 | `_extract_upscaler_sd` 上游只剥 `upscaler.`；我们循环剥 `model./module./net./state_dict./upscaler.` | 第三方/自训练 ckpt 的包装前缀防御（上游只服务它自己的官方权重） |
| D3 | 模型目录注册延迟到首次扫描；新增 `set_model_dirs()` 供离线单测注入 | 无 ComfyUI 环境也能 `import upscale_net` 做前向单测 |
| D4 | 缓存键含架构（`name::arch::device::precision`） | 同一权重按 2D/3D 分别加载时不串味；auto 与显式同架构共享一份缓存 |
| D5 | 归一化/反归一化外移到 `upscale.py` | 二采要在放大后的 latent 上继续采样，网络只管前向 |
| D6 | 设备选择：上游是面板下拉 `cuda/rocm/cpu`；我们原走 `comfy.model_management.intermediate_device()`，阶段 6 补上面板下拉（默认仍「自动」= 走 ComfyUI 调度 + `_cuda_if_room` 纠偏） | 二采在 UNET 常驻的显存环境里跑，默认交回 ComfyUI 调度最稳；显式 cuda/rocm/cpu 仅排错/专用卡用 |
| D7 | 3D `forward` 加 temporal chunking（上游 `3d.py:244-339` 同样实现） | 长段省显存 + 治末端闪烁；本地默认开，且只做 3D 前向 |
| D8 | 目标尺寸 / 百万像素模式（上游 `3d.py:455-578`） | 面板直接给像素尺寸更符合视频工作流；本地沿用「latent 偶数 = 像素 32 对齐」口径 |
| D9 | `force_unload` / `soft_empty_cache` / rocm 检测 / `safe_open` 零拷贝（上游 `3d.py:108-129`、L356-372、L599-608），阶段 6 并入 | 与上游的口径差：我们的 `force_unload` 是缓存逐出 + `soft_empty_cache`（上游只 `to('cpu')` 留缓存）——多段链后段更易 OOM 的主因是 CPU 侧权重副本，逐出才真释放；rocm 仍映射 cuda 设备对象，本地无法验证，代码注明 |
| D10 | 3D 装权从 `strict=True` 放宽为 `strict=False` + attn 白名单 | 与 2D 同口径（attn 推理强制关闭会缺键）；非 attn 缺键仍报错 |

## 5. 同步 SOP

1. **先看有没有新东西**（最便宜）：
   `git ls-remote upstream` —— 只有 `HEAD` 与本文件「基线 commit」不同才需要往下走。
2. **取文件**（二选一，别用裸 `git fetch upstream --depth 1`，见§6）：
   - `git fetch upstream refs/heads/main:refs/remotes/upstream/main`（显式 refspec）
   - 或 curl raw（需代理加 `-x http://127.0.0.1:7890`）：
     `https://raw.githubusercontent.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler/<sha>/nodes/minimax_h3_latent_upscaler_3d.py`
3. **存快照 + diff**：新文件放进 `docs/upstream_snapshot/<日期>/`，与上一版快照 diff：
   `git diff --no-index docs/upstream_snapshot/<上次>/<f> docs/upstream_snapshot/<今天>/<f>`
4. **判定影响面**：
   - 只动节点层 / UI / 文档 → **忽略**，不用改本地代码；
   - 动网络结构（组件实现 / 前向顺序 / 归一化常量）→ **必须同步**，并跑前向等价性检查（同输入同权重，改动前后输出应在数值噪声量级）；
   - 动加载 / 架构判定 / 目录扫描 → 同步后跑一遍三类权重加载验证（上游 2D 带 `resizer.` 前缀 / 上游 3D 裸键 / 人为加 `model.` 前缀的 ckpt）。
5. **更新锚点行号**：在新快照里 `grep -n '^class \|^def '` 拿行号，改本地 `# ↑ upstream/...` 注释，同时更新本文件的「基线 commit」与锚点表。
6. **提交**：message 注明 `同步上游 <sha>`，并在差异台账里追加新条目（如果是新增强而不是同步）。

## 6. 踩过的坑

- **浅克隆 fetch 的 ref 不落盘**：`git fetch upstream --depth 1` 报成功，但
  `rev-parse upstream/main` 失败、`git branch -r` 为空。绕法：显式 refspec（见§5.2）
  或干脆 curl raw 抓单文件（我们只需要两个文件，不值得为它拉完整历史）。
- **代理**：git 走 `http://127.0.0.1:7890`（`http.proxy`）；curl 走代理加
  `-x http://127.0.0.1:7890`。shell 里的 `HTTPS_PROXY=...6387` 是死的，别用。
- **中文文件名误读**：`git ls-tree`/`status` 会把中文名转义成八进制串，判断 git
  状态一律加 `git -c core.quotepath=false`。

## 7. 不做的事

- **不写自动同步脚本**：上游改动要人工判断「跟不跟、怎么跟」（它服务独立节点，
  我们服务二采通道），自动同步只会制造噪声。
- **不 import 上游包**：本插件零第三方依赖，只做源码级移植 + 台账跟踪。
