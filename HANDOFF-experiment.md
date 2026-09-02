# experiment 分支交接报告

> 生成时间：2026-09-02 ｜ 供在另一台机器继续开发使用
> 本文件是交接文档，不含功能代码改动。拉取后如需丢弃：`git revert <本文件提交>` 即可。

> **✅ 收尾更新（2026-09-03）：本文 §2 列出的阶段 2–7 已全部完成并提交**——
> `00519fe` 阶段2（换模型三项）· `b400b24` 阶段3（上游跟进）· `7f6b44a` 阶段4
> （temporal chunking）· `090f6ad` 阶段5（目标尺寸/百万像素）· `82eba59` 阶段6
> （rocm/force_unload/zero-copy/soft_empty_cache）· `1148a45` 阶段7（color_match
> **证伪后砍掉**，结论与复检判据见 `docs/color_match_falsification.md`）·
> `27ae360` 代码审查修复（指纹兼容/边界崩溃/错误分级等 10 处）。各阶段均有
> 独立回归验证（临时脚本验证完即删）。上游跟进台账与同步 SOP 见
> `docs/UPSTREAM.md`。**下文 §2 起为交接时的历史快照，状态以本段为准。**

---

## 1. 当前状态（已确认）

| 项 | 值 |
| --- | --- |
| 分支 | `experiment` |
| HEAD | `e8c217d`（已推送，`git ls-remote origin experiment` 确认） |
| 工作区 | 干净，无残留临时文件 |
| 上游 | `LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler` @ `d7c01b9`（2026-09-02 抓取） |

### 已完成阶段

- **阶段 0 · 回退二采模块化**：`git revert --no-edit 3b2b1bd` → `8e33b30`。
  `upscale_node.py` 已删，`__init__.py` 回到 2 个节点（H3SeamlessChainSampler / H3SeamDoctor）。
- **阶段 1 · 「神经放大」开关 `enlarge`** → `e8c217d`。
  - `upscale.py`：`parse_state` 加 `enlarge`（默认 True，关时 `scale=1.0; model=""`，不进 `_PARAM_KEYS`）；
    `load_net` 关时早退返回 None；`upscale_video` 首行 `if net is None: return clone` 一处守卫覆盖全部 4 个调用点；
    `preflight`/`render_latent`/`render_segment`/`net.cpu()` 全部改 `if net is not None` 分支。
  - `web/h3_director.js`：规范化加 `enlarge`；面板加「神经放大」勾选框，控制 model/arch/prec/scale 字段显隐。
  - 验证要点：关闭放大跑一段 → 无 `[H3二采] 加载放大模型` 日志、产物分辨率=基础分辨率、二采精化仍生效。

---

## 2. 待办阶段总览（按批准计划，P0→P1→P2，每阶段单独 commit）

| 阶段 | 优先级 | 内容 | 落点 |
| --- | --- | --- | --- |
| 2 | P0 | 换模型三项：子目录扫描 / 权重前缀剥离 / 自动 2D-3D 判定 | `upscale_net.py` + 前端 arch 下拉 |
| 3 | P1 | 上游跟进机制：remote + 锚点注释 + `docs/UPSTREAM.md` + 快照 | 全仓 |
| 4 | P1 | temporal chunking（对齐上游 3D，chunk=32） | `upscale_net.py` `LatentResizer3D.forward` |
| 5 | P2 | 目标尺寸 / 百万像素模式 | `upscale.py` + `upscale_net.py` + 前端 |
| 6 | P2 | rocm / force_unload / zero-copy / soft_empty_cache | `upscale_net.py` + 前端设备下拉 |
| 7 | P2 | color_match（**唯一真缺口，先证伪再动手**） | `nodes.py:2197`（需先跑 Seam Doctor ΔE 分布） |

**明确放弃（重复造轮子，别写）**：frame-0 锚 / identity 锚×3 / motion 锚 / seam_polish 探针门控 / 时间分块 / MMH3 拆分放大路线——本地均已具备（详见计划文件）。

---

## 3. 关键上游情报（重要，别重复调研）

我在本机已抓取并核读过上游 `_3d.py`（628 行）关键段。**上游 3D 节点已经实现**：子目录扫描、`safe_open` 零拷贝、temporal chunking、目标尺寸/百万像素、force_unload、device 下拉（cuda/rocm/cpu）、rocm 检测、soft_empty_cache。

三个必须知道的差异：

1. **上游把 2D/3D 拆成两个独立节点，没有「自动 2D/3D 判定」**。我们合并模块的 `load_model(name, device, precision, arch)` 需要自己加 auto（阶段 2.3）。
2. **上游 `_extract_upscaler_sd` 只剥 `upscaler.` 前缀**。计划里的 `model./module./net./state_dict.` 循环剥离是给第三方 ckpt 的防御增强（阶段 2.2），上游没有，需要自写。
3. **上游 3D `_detect_arch` 只读超参**（in_blocks/out_blocks/temporal_every/kernel），不判 2D/3D。

### 3.1 temporal chunking（上游 `_3d.py` L244-339，可直接并入我们 `LatentResizer3D.forward`）

```python
def forward(self, x, scale=None, target_size=None, enable_chunking=True):
    if target_size is not None:
        size = target_size
    elif scale is not None:
        size = tuple(int(round(s * scale)) for s in x.shape[-3:])
    else:
        return x
    if size == x.shape[-3:]:
        return x
    B, C, T, H, W = x.shape
    tk = 0
    for b in self.in_blocks:
        if isinstance(b, TemporalConv):
            tk = b.dwconv.weight.shape[2]
            break
    overlap = tk
    chunk = 32
    if not enable_chunking or T <= chunk:
        return self._forward_seg(x, scale, size)
    x_padded = F.pad(x, (0, 0, 0, 0, overlap, overlap), mode='replicate')
    out_full = torch.zeros(B, C, T, size[-2], size[-1], device=x.device, dtype=x.dtype)
    weight_full = torch.zeros(1, 1, T, 1, 1, device=x.device, dtype=x.dtype)
    start = 0
    while start < T:
        seg_start = start
        seg_end = min(T, start + chunk)
        out_start = max(0, seg_start - overlap)
        out_end = min(T, seg_end + overlap)
        lo = max(0, out_start - overlap)
        hi = min(T + 2 * overlap, out_end + overlap)
        seg = x_padded[:, :, lo:hi].contiguous()
        seg_size = (hi - lo, size[-2], size[-1])
        seg_out = self._forward_seg(seg, scale, seg_size)
        s0 = (out_start + overlap) - lo
        s1 = s0 + (out_end - out_start)
        valid_out = seg_out[:, :, s0:s1]
        n_valid = out_end - out_start
        weight = torch.ones(n_valid, device=x.device, dtype=x.dtype)
        if seg_start > out_start:   # 首块左侧不 ramp
            blend_len = seg_start - out_start
            weight[:blend_len] = torch.arange(1, blend_len + 1, device=x.device, dtype=x.dtype) / (blend_len + 1)
        if out_end > seg_end:       # 末块右侧不 ramp
            blend_len = out_end - seg_end
            weight[-blend_len:] = torch.arange(blend_len, 0, -1, device=x.device, dtype=x.dtype) / (blend_len + 1)
        out_full[:, :, out_start:out_end] += valid_out * weight.view(1, 1, n_valid, 1, 1)
        weight_full[:, :, out_start:out_end] += weight.view(1, 1, n_valid, 1, 1)
        start += chunk
        del seg, seg_out, valid_out
    out_full = out_full / weight_full.clamp(min=1e-8)
    return out_full

def _forward_seg(self, x, scale, size):
    # = 原 forward 去掉 chunking 后的整段前向（conv_in → in_blocks → trilinear → out_blocks → norm_out → conv_out）
    ...
```

注意：我们现有 `forward(x, scale=None, target_size=None)` 需要**抽出 `_forward_seg`** 并加 `enable_chunking=True` 形参。

### 3.2 子目录扫描 + 零拷贝（上游 `_3d.py` L349-372）

```python
def scan_models():
    names = [name for name in folder_paths.get_filename_list(_LATENT_UPSCALE_FOLDER)
             if os.path.splitext(name)[1].lower() in (".pth", ".safetensors")]
    return names if names else [f"(place models in: {get_models_dir()})"]

def _load_raw_sd(path):
    if path.endswith('.safetensors'):
        try:
            from safetensors import safe_open
            with safe_open(path, framework="pt", device="cpu") as f:
                sd = {k: f.get_tensor(k) for k in f.keys()}
        except ImportError:
            from safetensors.torch import load_file
            sd = load_file(path, device='cpu')
    else:
        sd = torch.load(path, map_location='cpu', weights_only=False)
    ...
```

`load_model` 用 `folder_paths.get_full_path_or_raise(_LATENT_UPSCALE_FOLDER, name)` 定位（路径穿越防护由它保证）；缓存键用 `name::device::precision`。

### 3.3 目标尺寸/百万像素换算（上游 `_3d.py` 节点 execute 区，L455+）

`UpscaleMode` 枚举：`scale by multiplier` / `target dimensions` / `megapixels`。核心换算（L551-554 等）：
`target_pixels = mp * 1024 * 1024`；按原宽高比 `aspect_ratio = w/h`；`h_pixel_target = (target_pixels / aspect_ratio) ** 0.5`，再反推 w。换算后 latent 取偶（=像素 32 对齐）。

### 3.4 rocm / force_unload / soft_empty_cache（上游 `_3d.py` L108-125、L601-605）

- `_is_rocm_build()`：查 `torch.version.hip`；rocm 后端仍映射 `cuda` 设备对象，仅日志/选项区分。
- execute 尾部：`if force_unload: model.to("cpu")`；`else: mm.soft_empty_cache()`（`comfy.model_management`，无 comfy 环境回退 `torch.cuda.empty_cache()`）。

### 3.5 上游权重获取命令（另一台机器执行）

```bash
git remote add upstream https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler
# 若 git fetch 浅克隆对象落盘有问题（本机踩过），直接用 raw 抓：
# 需配代理（本机 127.0.0.1:7890）
curl -x http://127.0.0.1:7890 -o upstream_3d.py \
  "https://raw.githubusercontent.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler/d7c01b9011f2e8439493f6c02c29995a27df276f/nodes/minimax_h3_latent_upscaler_3d.py"
curl -x http://127.0.0.1:7890 -o upstream_2d.py \
  "https://raw.githubusercontent.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler/d7c01b9011f2e8439493f6c02c29995a27df276f/nodes/minimax_h3_latent_upscaler_2d.py"
```

---

## 4. 阶段 2 具体落点（换模型三项，P0）

全在 `upscale_net.py`（行号以当前 e8c217d 为准）：

- **2.1 子目录扫描**：`scan_models`（L430-439）改用 `folder_paths.get_filename_list(LATENT_UPSCALE_FOLDER)` + 后缀过滤（递归子目录、返回相对路径）；`load_model`（L538）改用 `folder_paths.get_full_path_or_raise`；保留 glob 兜底供无 ComfyUI 单测。旧存档存的纯文件名天然兼容。
- **2.2 前缀剥离**：`_extract_upscaler_sd`（L455-459）从只剥 `upscaler.` 扩展为循环剥离 `model.` / `module.` / `net.` / `state_dict.` / `upscaler.`，直到出现 `conv_in.weight` 或 `in_blocks.` 键。
- **2.3 自动架构判定**：新增 `_detect_kind(sd, arch)`（放 L462 前）。顺序：① 面板 arch ∈ {2D,3D} → 直接用；② `auto`/空 → 有 `resizer.` 前缀键 → 2D；③ 看 `conv_in.weight`（剥离后键名）ndim：5→3D、4→2D（能救裸 2D 骨干）；④ 都不行 → None，走「试装择优」。
  `load_model`（L533）：**删除 L543-546 硬报错**；`arch` 默认 `"auto"`；试装择优 = 按 kind 构造 → `load_state_dict(strict=False)` → missing 过多换另一种重建，取 missing 少者；3D strict 语义放宽（attn 白名单）；`in_channels != 24` 明确报错；缓存键用解析后 kind。`_detect_arch`（L462-530）正则全部加可选前缀 `(?:resizer\.)?`。
- **前端**：`web/h3_director.js` 架构下拉加「自动」并默认（值 `"auto"`）；规范化（~L589）改 `arch: ["2D","3D"].includes(upRaw.arch) ? upRaw.arch : "auto"`。

**调用点兼容性清单（改签名前必须对齐）**：
- `upscale.py:571` `upscale_video(video_t, net, scale, arch="2D")`
- `upscale.py:593` 3D 路径：`net(xn, scale=float(scale), target_size=(x.shape[2], h2, w2))`
- `upscale.py:595` 2D 路径：`net(xn, scale=float(scale), target_hw=(h2, w2))`
- `upscale.py:698` `upscale_net.load_model(cfg["model"], dev, cfg["precision"], cfg["arch"])`
- 2D `LatentResizer.forward` 收 `target_hw`；3D `LatentResizer3D.forward` 收 `target_size`（3 元组含 T）——别混。

**验证**：三类权重逐个加载 —— ① 上游 2D（`resizer.` 前缀）② 上游 3D（裸键）③ 人为加 `model.` 前缀的 ckpt。① ② 自动判定正确、③ 剥前缀成功。

---

## 5. 后续阶段速记

- **阶段 3（P1）**：`git remote add upstream <url>`；`upscale_net.py` 五处加锚点注释（LatentResizer/VideoLatentResizer/LatentResizer3D/_detect_arch/load_model 上方，格式 `# ↑ upstream/nodes/minimax_h3_latent_upscaler_<kind>.py:<行号>（同步于 d7c01b9）`）；新建 `docs/UPSTREAM.md`（台账+同步 SOP）；`docs/upstream_snapshot/2026-09-02/` 存两个上游文件快照；README 加指向节。不写自动同步脚本。
- **阶段 4（P1）**：见 3.1，并入 `LatentResizer3D.forward`，开关默认开，只做 3D 放大前向不做 2D。验证：同段开关 chunking 各跑一次，输出差异应在数值噪声量级。
- **阶段 5（P2）**：见 3.3。`upscale.py parse_state` 加 `size_mode`/`target_w`/`target_h`/`megapixels`；`effective_scale` 传 net；`<1.0` 报错。**务必走一遍非方画幅拼接**（历史上 `upscale.py:553` 注释记过宽高对调 bug）。
- **阶段 6（P2）**：见 3.4。rocm 本地无法验证，代码注明「未验证」。
- **阶段 7（P2）**：先用 `seam_doctor` 跑一批看 ΔE 分布，**证伪段间色彩漂移再动手**；漂移小就直接砍掉。默认关、回放段不参与。

---

## 6. 本机踩过的坑（备忘）

1. **中文文件名 + `core.quotepath` 转义造成误读**：`git ls-tree HEAD` 输出 `\345\244\207...` 转义串，summary 曾误判成"幽灵条目 `二采独立节点示例.json`"。用 `git -c core.quotepath=false` 才看清真实文件名是 `example_workflows/备用初始化导演台工作流.json`（合法资产，已从 HEAD 恢复，勿删）。**判断 git 状态一律加 `-c core.quotepath=false`**。
2. **credential helper 卡死**：PortableGit system 级 `credential.helper=helper-selector` 在非交互环境会挂 27s。修复：local 级 `credential.helper=`（空，清空上层）+ `--add credential.helper='!...git-credential-manager.exe'`。push 前检查 `git config --get-all credential.helper`。
3. **代理**：git 走 `http://127.0.0.1:7890`（http.proxy）；shell 里 `HTTPS_PROXY=...6387` 是死的，别用。curl 走代理加 `-x http://127.0.0.1:7890`。
4. **浅克隆 remote-tracking ref 不落盘**：`git fetch upstream --depth 1` 报成功但 `rev-parse upstream/main` 失败、`git branch -r` 为空。绕法：显式 refspec fetch，或干脆用 curl raw 抓文件（见 3.5）。"upstream is gone" 警告是本地 cosmetic，不影响远端。
