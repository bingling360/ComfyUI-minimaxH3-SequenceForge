# HyperFlow / 少步蒸馏 LoRA 适配（自定义 Sigmas 通道）

> 2026-09-21 · 主节点新增「自定义Sigmas」输入槽（`sigmas_adapter.py`）
> 2026-10-12 · 该槽改名「**一采自定义Sigmas**」，并新增「**二采自定义Sigmas**」

## 为什么必须有这个口子

少步蒸馏 LoRA（HyperFlow 8 步、H3 Turbo 等）是**带着自己的 sigma 表**训练的：
训练时每步吃的是表里那一段区间，不是事后用「步数 + 调度器」现算出来的表。
把「步数=8 + euler/simple」喂给它，得到的不是蒸馏轨迹，画面会偏（轻则发糊，重则结构散）。

`nodes.common_ksampler` 没有 sigmas 形参，所以只能自己走一条等价路径：
本插件改成调 `sigmas_adapter.ksampler_with_sigmas()`，它把 sigma 表直接交给
`comfy.sample.sample(sigmas=...)`——与官方 `SamplerCustomAdvanced` 同一条路。
`comfy.samplers.KSampler.sample` 里是 `if sigmas is None: sigmas = self.sigmas`，
**传了就用传的**，所以「步数」与「调度器」两个控件在接了表之后自动失效。

## 接线

```
Load Diffusion Model (MiniMax-H3)
  └─> ApplyHyperFlow                  Saganaki22/ComfyUI-Hyperflow
        ├─ MODEL  ──> H3 Seamless Chain「模型」
        └─ SIGMAS ──> H3 Seamless Chain「一采自定义Sigmas」   ← 2026-09-21 起的槽
```

> ⚠️ **最容易踩的坑：drbaph 有两个仓库，文件名长得像但完全不是一回事。**
>
> | 仓库 | 文件名 | 用法 |
> |---|---|---|
> | `drbaph/Hyperflow-Comfyui` | `custom_node_hyperflow_8step_v1.0_comfyui.safetensors`<br>`custom_node_hyperflow_8step_v1.0_comfyui_pruned.safetensors` | **给 ApplyHyperFlow 节点用**，放 `models/hyperflow/` |
> | `drbaph/MiniMax-H3-Turbo-Lora-ComfyUI` | `minimax_h3_hyperflow_8step_v1.0_comfyui_bf16.safetensors`<br>`minimax_h3_hyperflow_8step_v1.0_comfyui_pruned_bf16.safetensors` | standalone，**只能给 stock Load LoRA 用**，放 `models/loras/` |
>
> 认前缀：节点版一律以 **`custom_node_`** 开头。把 standalone 版喂给 ApplyHyperFlow 会直接报：
> *"is a standalone/generic ComfyUI LoRA (keys like 'diffusion_model.\<module\>.lora_A.weight' and '.alpha'),
> not the HyperFlow node build."*
> 反过来也一样——节点版给 Load LoRA 会缺 `.alpha` 之外的结构，同样挂不上。
>
> 走 standalone 那条路时没有 SIGMAS 输出，需要用 core 的 **`ManualSigmas`** 节点手工填那 9 个点，
> 再接进本节点的「一采自定义Sigmas」槽（见下）。

- 「模型」槽接 ApplyHyperFlow 的 **MODEL** 输出（LoRA 与双时间条件已装在里面）。
- 「一采自定义Sigmas」槽接它的 **SIGMAS** 输出（9 点表 = 8 步）。
- 采样器选 **euler**（HyperFlow 官方配方；其它采样器属 ablation）。
- 「步数」「调度器」不用动——接了表之后它们被忽略，报告里会写明。
- **不需要**再挂 `ModelSamplingMiniMaxH3`：H3 默认就是 video shift 12 / audio shift 3，
  与 HyperFlow 的表一致；想改 shift 才加，且要接在 ApplyHyperFlow **之后**。

不接这个槽时，一切与改造前**逐字节一致**（内部走的是同一个 `comfy.sample.sample`，
只是 `sigmas=None` 原样传下去）。

## 参数建议

| 项 | 值 | 说明 |
|---|---|---|
| 采样器 | `euler` | 官方配方；`res_multistep` 等属 ablation |
| CFG | 1.0 | 本插件默认即 1.0，蒸馏 LoRA 也要求低 CFG |
| 步数 | 任意 | 接了表即失效；报告显示实际步数 = 点数-1 |
| 调度器 | 任意 | 同上 |
| 二采 | 关，或另接基础权重；蒸馏权重就接「二采自定义Sigmas」 | 见下 |

## 主模型精度：INT8 ConvRot 的差距其实不大

**先给结论：24GB 卡上用官方 pruned INT8 ConvRot 是正确的选择，不是"将就"。**

INT8 ConvRot 是 ComfyUI 为 H3 专门做的量化（不是通用 int8 硬压）：

- **只量化线性分支的矩阵乘权重**（200 个 block linear：`attn.qkv_proj` / `attn.out_proj` /
  `mlp.fc1` / `mlp.fc2`），**adapter、bias、norm、conv 全部保持原精度**——对挂 LoRA 友好。
- 做法是 int8 + per-output-channel fp32 scales + **Hadamard 旋转**（`convrot_groupsize=256`）。
  已知转换的实测：权重相对 L2 误差 **0.90–1.02%**，cosine **≥ 0.99994**，
  属于"带旋转的 per-channel INT8 的理论下限"。
- 社区同 seed A/B（1280×736 / 61 帧）：**视觉上基本无差别**；生态综述的说法是
  INT8 官方包 "quality is close to BF16 for most generations"。

**但有两个真实代价，别当它完全无损：**

1. **被融合进 int8 kernel 的 `mlp.fc2` 抓不到 bypass hook**，那部分 LoRA 只能走 merge 路径
   （Saganaki 节点的 `_int8_fused_fc2` 检测，控制台会报 `N fused/int8 targets via merge`）。
   README 原话："Merging quantized weights can change numerical results."
2. **HyperFlow 是按 bf16 全精度 H3 蒸馏的**，量化 base 本身就已偏离蒸馏时的底座，属于误差叠加。

**精度档位参考**（生态综述）：

| 档 | 门槛 | 说明 |
|---|---|---|
| BF16 全精度 | 80GB+ VRAM，123.6GB 磁盘 | 上限配置；多数人用不起 |
| **INT8 官方包**（pruned） | 24GB 卡（3090/4090/5090） | **推荐起点**，约 42.5GB 下载 |
| NVFP4 | Blackwell（50 系） | 老卡别用 |
| GGUF Q5 | ≈ INT8 | Q3 明显变软 |
| INT4 / NF4 | 8GB 卡 | 明显掉质量，发软 + 轻微偏色 |

⚠️ 不同人做的 int8 转换**质量不一样**：从 fp32 量化比从 bf16 量化准约 8%（bf16 只有 8 位尾数，
当除数会再损失一次）。优先用 Comfy-Org 官方 repack，别随便抓第三方转换。

## 采样器：必须用 euler（这条没得商量）

Saganaki README 原话：**"Use Euler for the released recipe; other samplers are ablations."**

机制上的原因：HyperFlow 是 flow-map 的 `(t, r)` 表述——**每一步吃的是该步积分区间的两个端点**
（`r = 1 - sigma_next`）。Euler 是一阶方法，正好对应"从 t 积分到 r"这一步。
`res_multistep` 这类多步高阶采样器会用历史步做外推，它实际走的轨迹**与"每步独立吃 (t,r)"
的蒸馏假设错位**——端点条件就对不上它真正在积的区间。

而且只有 8 步，**步数越少采样器差异越被放大**（每步都关键，高阶项的偏差也更大）。

- 本插件默认采样器是 `res_multistep`（对基础模型合适），**挂 HyperFlow 时记得手动改成 euler**，
  报告会提示你。
- 调度器无所谓——接了 sigma 表之后它已经失效了。

## 二采的采样器 / 调度器：别照搬主链的 euler + beta

**结论：二采不需要改 euler，也不需要配 beta。** 那套是给 **一采的 HyperFlow 蒸馏**用的。

- euler 之所以必须，是因为 flow-map 每步吃 `(t, r)` 区间端点。二采若按推荐**接了独立基础权重**，
  它跑的是基础模型的低强度精化，与蒸馏无关——**反而插件默认的 `res_multistep` 更合适**
  （少步短区间里高阶方法通常更精确）。
- drbaph 的 "euler + beta" 是给 **Turbo LoRA 完整生成 6–8 步**的推荐，不是二采精化。
- 二采默认 `σ₀=0.35`、`steps=6`，`sampler/scheduler` 留空 = 沿用主链值。

### 换调度器 = 连起始 σ 一起换掉（实测）

`denoise` 只定步数比例（`new_steps = int(steps/denoise)`），**尾部那几步的 σ 落点由调度器形状决定**。
H3 的 shift=12 让 sigma 表高度非线性，差异被放大。实测（照 ComfyUI 的 `simple_scheduler` /
`beta_scheduler` 实现算的）：

| 参数 | simple | beta(0.6, 0.6) |
|---|---|---|
| σ₀=0.35 · 3 步 | 0.878 → 0.800 → 0.632 → 0 | 0.855 → 0.719 → **0.425** → 0 |
| σ₀=0.25 · 3 步 | 0.800 → 0.706 → 0.524 → 0 | 0.719 → 0.549 → **0.271** → 0 |
| σ₀=0.15 · 3 步 | 0.679 → 0.571 → 0.387 → 0 | 0.504 → 0.333 → **0.136** → 0 |

**beta 的两个特点：起点更低、收尾更细 → 整体精化更温和。**
所以从 simple 换成 beta 而 σ₀ 不动，二采效果会**变弱**。要同等强度就得把 σ₀ 调高。
（报告里已加提示：显式指定二采调度器时会提醒重调 σ₀。）

⚠️ 顺带一个反直觉的点：**`denoise=0.35` 的实际起始 σ 是 0.87，不是 0.35**——
denoise 是步数比例，不是 σ 比例，别按字面理解它。

### 3 步够吗

3 步偏轻。配合"神经放大 + 低强度补细节"是合理的；想要明显补细节建议 4–6 步。
真要 3 步：`simple` 干预更强，`beta` 更保守（可能几乎看不出变化）。
判断别靠肉眼——开二采的**增益重试**（`retry_target`，默认 0.15）让它自己量。

## 与本插件机制的相互作用（都是要留意的点）

1. **段间锚定仍然成立，但精度会降。** HyperFlow 的双时间条件里，条件行
   （fl2va keyframes / ref2va references）保持 `r = t`，钉法与官方 denoiser 一致——
   本插件的 `minimax_keyframes` 锚定正是条件行，理论上仍被钉住。
   但 8 步下模型对「从锚定帧继续演化」的自由度显著下降，接缝表现需要实测。

2. **接缝阈值要重标。** 重摇阈值 0.06、桥帧清晰度阈值 30 都是按 25 步标的。
   建议先用「桥帧门控=标注」跑一遍看分数分布再定。

3. **递减锚定会阶梯化。** 它按 `sigma_v` 算进度（`progress = 1 - sigma_v`），
   8 步只有 8 个采样点，曲线不再平滑。报告里会提示。

4. **二采（潜空间放大高清）的 sigma 不受影响，但"模型"会——这是最容易漏的一条。**

   - **sigma 表**：一采表只作用于一采，**不会渗到二采**。二采走自己的路径：默认是
     `common_ksampler(steps=n, denoise=σ₀)` 低强度精化；要给二采也换表，必须走**另一个槽**
     「二采自定义Sigmas」（见下一节）。两个槽互不干扰是刻意的——一采那张 1.0→0 的完整表
     和二采「从 σ₀ 起的尾段精化」语义完全不同，自动串过去只会整段重画。
   - **模型**：`_up_model = 模型 if 二采模型 is None else 二采模型`。
     **「二采模型」槽空着 = 二采沿用一采模型 = 带着 HyperFlow LoRA 跑**，
     只不过用的是二采自己的步数/调度器（不是蒸馏表），轨迹对不上。
     **想让二采不用这个 LoRA，唯一办法是在「二采模型」另接一份基础（非蒸馏）权重。**
   - **调度器/采样器**：二采面板留空就沿用主链「采样器/调度器」控件值——注意这两个值
     在接了 sigma 表之后**对一采已失效、对二采仍然生效**（半失效状态）。
     要区分就在二采面板显式填，别留空。报告会提示。
     二采接了独立基础模型时，采样器沿用主链值即可，不必特意换。

5. **一采换表不会让已存档的段重做 —— 必须手动「重跑起始段」。**
   （⚠ 2026-10-12 复核更正：本节旧文写「换表 = 重做」，那是 `fingerprint()` 还拿
   参数命名项目目录时的行为。同日起自动项目目录固定为 `h3chain`、`fingerprint()`
   退化为诊断用（见 `checkpoint.ckpt_dir` / `checkpoint.fingerprint` 的注释），
   于是**整条基础链的参数**（步数/采样器/调度器/一采表…）都只进 `manifest.params`，
   由 `checkpoint.assert_match` **只报告不重做**：报告里会出现
   「参数已变更（sigmas: …）——仅影响此后新生成的段；已有段是盘上张量、与采样参数
   无耦合，不会因此重做」。
   一采表仍写进 `ckpt_params["sigmas"]`（未接时**不加键**，否则 `json.dumps(sort_keys)`
   下多一个键就是新指纹、既有项目续不上）——留着做**诊断/复现比对**，也是「哪些段
   是这份参数下生成的」的存档证据。
   👉 想让换了表之后的基础段重跑：设「重跑起始段」。想让**二采**高清段跟着重做：
   二采表走的是另一条路（`upscale.params_hash`，见下节），二采面板的参数一改就会
   自动重做高清段。

## 二采自定义Sigmas（2026-10-12 新增）

**它解决什么问题**：一采挂蒸馏 LoRA、二采又另接了一份**带自己 sigma 表的蒸馏权重**时，
二采的「去噪 + 精化步数 + 调度器」现算出来的表同样对不上蒸馏轨迹。
这个槽就是把「外部 sigma 表」这条通道接到二采上。

```
ApplyHyperFlow（二采那份权重）
  ├─ MODEL  ──> H3 Seamless Chain「二采模型」
  └─ SIGMAS ──> H3 Seamless Chain「二采自定义Sigmas」   ← 本槽
```

### 接了之后，哪些控件失效 / 哪些仍然生效

| 二采面板项 | 接表后 | 说明 |
|---|---|---|
| 去噪（σ₀） | ❌ 失效 | 精化起点改由**表首值**决定 |
| 精化步数 | ❌ 失效 | 实际步数 = **表点数 − 1** |
| 调度器 | ❌ 失效 | 表即调度（`comfy.samplers.KSampler.sample` 里 `if sigmas is None` 才用现算的） |
| 多轮递降（passes/decay） | ❌ 让位 | 表本身就是整条精化轨迹，再叠轮次没有语义 |
| 增益重试（retry） | ❌ 让位 | 它要从 σ₀+0.1 现推一张表，而表是外部给的，推不出来 |
| 段自适应σ（adaptive） | ❌ 失效 | σ 起点由表首值定，不再按段运动量偏移 |
| **采样器** | ✅ 生效 | 与一采同口径：`sampler_name` 仍传给采样器 |
| shift / STG | ✅ 生效 | 激活窗口按**表首值**（本表实际 σ 起点）算 |
| 时序分块 / 空间 tile | ✅ 生效 | 每块各自跑整张表（表首值即每块的加噪起点） |

报告里会把失效项逐条写出来；接表时控制台也会打一行 `[H3二采] 段N：使用二采自定义 sigma 表`。

### ⚠ 表是「尾段精化」语义，别照搬一采那张

一采那张 HyperFlow 表是 `1.0 → 0`（从纯噪声起步）；二采精化是从**放大后的 latent** 起步，
首值就是**精化起点 σ**、末值应为 0。典型二采表长这样：

```
0.35, 0.28, 0.19, 0.09, 0.0        # σ₀=0.35 的 4 步精化（对应面板「去噪 0.35 / 精化 4 步」）
```

首值不在 (0,1) 开区间时报告会提示一次（多半是拿错了表）。

### 指纹口径

表指纹（`sigmas_adapter.fingerprint_tag`）由 `nodes.py` 写进 `up_cfg["sigmas"]` →
进 `upscale.params_hash` → **换表 = 既有高清分段全部重做**（`record_stale` 判得出）。
未接时**不加键**（与一采同一条铁律）。接了之后 `passes/decay`、`retry`、`scheduler`
在采样侧已失效，故**一并退出指纹** —— 拨这些不生效的旋钮不该触发整链高清重做。

⚠ 写键的判据是 `sigmas_adapter.sigmas_usable`（点数 ≥ 2），**与采样分支同源**。
空表/坏表按「未接」处理：既不加键、也不改采样。若两处用两套判据，空表就会落到
「指纹变了、采样没变」的白做——既有高清段全被判失效重做，画面一个像素没动。

### 两个边界（2026-10-12 复核，各有守卫测试）

| 情形 | 二采受影响吗 | 一采受影响吗 | 依据 |
|---|---|---|---|
| **只接一采表**，二采不接 | **零影响** | 按表走 | 一采表的张量只在一采采样点用；`upscale.py` 里根本不出现「一采自定义Sigmas」这个名字（守卫：`test_first_pass_table_never_leaks_into_second_pass`）。二采指纹只认 `up_cfg["sigmas"]`，那是由**二采**槽写入的 |
| **只接二采表**，二采没开 | 零影响（本来就没跑） | 零影响 | `up_cfg = upscale.parse_state(ds)` 二采关着就是 `None` → 写键那句在 `if up_cfg:` 块内、执行不到；`render_segment` 也受 `if not (up_cfg and use_ckpt)` 守卫不会被调用（守卫：`test_second_pass_table_is_inert_when_upscale_off`） |
| 只接二采表，二采开着但该段不在「手动选择」范围 | 该段不重做 | 零影响 | `in_scope()` 逐段判定 |

两个边界都有报告行兜底：坏表（点数 < 2）→「接了表但读不出有效步数…已按未接处理」；
二采没开 →「已接线但本次二采未启用…该表未生效」。

**唯一的非零成本**：接线本身让上游那张表节点进了执行图（比如 `ManualSigmas` 会照跑）。
不需要时把它 bypass（Ctrl+B）即可——纯连线槽不参与执行，bypass 不影响本节点。

### 与一采表的「重做口径」不对称（刻意如此）

| | 改参数后已存档段会重做吗 | 机制 |
|---|---|---|
| **一采表 / 一采步数 / 一采采样器** | ❌ 不会（要手动「重跑起始段」） | 基础链只进 `manifest.params`，`assert_match` 只报告 |
| **二采表 / 二采去噪 / 二采步数 / 二采采样器** | ✅ 会（高清段自动重渲染，基础 latent 不动） | `upscale.params_hash` → `record_stale` |

不对称是两边既有设计各自成立：基础段是**贵**产物、且参数与盘上张量无耦合，
所以「改参数不重做」；二采是**便宜**的再渲染一遍，所以「参数变即重做」。
二采表跟着二采那一侧走。

## 已知限制（来自上游，非本插件）

- **pruned / full 必须配对，配错是硬报错（不是静默降级）。**
  ComfyUI-HyperFlow 的 `apply_lora()` 逐个校验 LoRA 目标在 base 上是否解析得到、形状是否对得上，
  docstring 写明 "errors are never warnings"：

  | 组合 | 结果 |
  |---|---|
  | full base + full 权重 | ✅ 完整双时间配方（官方发布模型） |
  | pruned base + pruned 权重 | ✅ 能跑，但 **backbone-only / single-time（off-recipe）**，输出偏离官方发布模型 |
  | pruned base + full 权重 | ❌ `RuntimeError`——`time_embedder.*` / `endpoint_time_embedder.*` 在 pruned base 上既无模块也无 state_dict 条目，节点直接报"该用哪个文件" |
  | full base + pruned 权重 | ❌ 同样报错（白白放弃完整配方） |

  两个权重文件大小只差约 30 MB（3.67 vs 3.64 GiB），差的就是那两块 time embedder LoRA。

- **为什么 pruned 形态拿不到双时间条件：结构上没有可 patch 的对象。**
  不是节点偷懒。ComfyUI 的 H3 DiT（`comfy/ldm/minimax/model.py`）里：

  ```python
  self.use_adaln_curves = adaln_curve_grid is not None
  if self.use_adaln_curves:
      self.register_buffer("adaln_t_table", ...)     # curve 形态：查表
  else:
      self.time_embedder = TimeEmbedder(...)          # full 形态：真模块
  ```

  forward 里同理：`if self.use_adaln_curves: t_emb = 表插值 else: t_emb = self.time_embedder(t_vals)`。
  pruned/curve 形态**根本没有 `time_embedder` 这个模块**，节点的 endpoint embedder 无处可挂。

- **降级为 single-time 后仍然要喂这张 sigma 表。** 8 步蒸馏的轨迹就是那张表，
  「步数=8 + euler/simple」现算的不是它——降级掉的是双时间条件，不是少步这件事。
- **量化 base 未验证。** 蒸馏基于 bf16 全精度 H3；int8-convrot / NVFP4 / GGUF repack 上
  挂蒸馏 LoRA 属于误差叠加，官方没验证过。量化 base 上被折叠进融合 kernel 的 LoRA 目标
  会自动走 merge 路径（控制台报 `N fused/int8 targets via merge`）。
- **许可。** MiniMax H3 Community License 排除美/欧/英/韩的本地使用（中国大陆与香港不受限）。
- **硬件。** 官方数据基于 H200 80G（单卡 + CPU offload 也要 24G margin）。

## 用剪枝版，画质差多少？（结构推算 + 自测方案）

**上游没有公开的 pruned vs full 量化对比**，下面是从结构能算出来的部分，不是实测。

### 缺的到底是什么

剪枝版 = backbone only（attention / FF），缺两块 time embedder LoRA：

- `time_embedder.*`（对 base 时间嵌入的适配）
- `endpoint_time_embedder.*`（第二个时间嵌入 + gate 混合）

**按参数量算只占约 0.8%**：两个权重文件 3.64 vs 3.67 GiB，差的 ~30 MB 就是这两块
（rank 256 的 proj_in/proj_out，endpoint 那一份还是 fp32）。99%+ 的适配器参数（50 个 block
的 qkv/mlp）剪枝版全都保留了。

**但参数量不等于影响力。** 时间嵌入是 DiT 的全局 AdaLN 调制信号，影响每一个 block。
不过文件头里 `hyperflow_gate = 0.25`，双时间条件是
`emb = emb_t(t) + 0.25 * (emb_r(r) - emb_t(t))` —— **一个 25% 权重的修正项，不是主体**。

所以合理预期是：**有差别，但不是"差一档"那种差别。**

### 三档损失的量级排序（别搞混了）

| 档 | 对比 | 量级 |
|---|---|---|
| 第一档（最大） | 8 步蒸馏 vs 49 步 base | 步数压了 6 倍，少步蒸馏**先掉材质与细节**——这是主要损失来源 |
| 第二档（小） | pruned(single-time) vs full(two-time) | 缺 0.8% 参数、25% gate 的端点修正，属二阶差异 |
| 独立变量 | pruned base vs full base | 曲线表替代完整 AdaLN 权重，与 HyperFlow 无关，是 base 选择的代价 |

**也就是说：如果你已经接受了 8 步蒸馏，pruned vs full 是它里面更小的一层差距。**

### 对你（长片续拍）的额外风险 —— 推断，未实测

双时间条件里有一条规定：**条件行（fl2va keyframes / ref2va references）保持 `r = t`**，
官方明确说这样"钉法与官方 denoiser 完全一致"。你的段间锚定 `minimax_keyframes` 正是条件行。
剪枝版没有这套机制，锚定帧的处理退化为普通单时间——**你的续拍恰好最依赖这个**。
所以剪枝版在你的场景里，差距可能比单镜头场景更明显，尤其体现为接缝一致性与角色漂移。

### 自测方案（同 seed，HyperFlow 官方推荐的比法）

固定 prompt + 固定 seed，只改一个变量，跑三组各一段：

| 组 | 配置 | 看什么 |
|---|---|---|
| A | pruned base + pruned HyperFlow + 8 步表 | 你的目标方案 |
| B | pruned base + **不挂** LoRA + 25 步 | 你现在的质量基线 |
| C | 显存够就跑：full base + full HyperFlow + 8 步表 | 完整配方上限 |

判读：A vs B 看"8 步值不值"；A vs C 看"剪枝到底差多少"。
接缝一致性别靠肉眼——接 `H3SeamDoctor` 看缝差数值，比主观评分靠谱。

### 一个绕不开的约束

**HyperFlow 锁死 8 步**（步数由权重文件里的表决定，改步数就是偏离蒸馏轨迹）。
所以"质量不够就加步数"这条路**走不通**——要么接受它的 8 步，要么不挂 LoRA 回到 25 步。

真觉得细节不够，用本插件的**潜空间放大二采**（并记得「二采模型」另接一份基础权重）
补回高频，这比在步数上做文章有效得多。

## 验证

```
python -m pytest tests/test_sigmas_adapter.py -q     # 12 条：指纹兼容 / 换表重做 / 两分支参数
                                                     #   + 二采通道四段接线 / 两个槽的位次
python -m pytest tests/ -q                            # 全量（含 widgets_values 仍为 30）
node tests/js/desk_node_skin_check.js                 # 端口免打扰名单（两个 sigmas 槽必须露头）
node tests/js/desk_slot_geometry_check.js             # 端口行号（新增一位后按钮位置随之下移）
node tests/js/link_repair_check.js                    # 按名搬线（旧存档接在「自定义Sigmas」上的线）
```
