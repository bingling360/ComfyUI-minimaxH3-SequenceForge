# Enhance-A-Video / FETA 子节点

MiniMax H3 的 **Enhance-A-Video / FETA** 时序注意力增强，实现在 `eav_feta.py`。

- 论文（**真正的源头**）：*Enhance-A-Video: Better Generated Video for Free*
  （[arXiv:2502.07508v3](https://arxiv.org/abs/2502.07508)，2025-02-27 定稿；NUS 团队，
  一作 Yang Luo，通讯 Yang You）
- 官方实现（**Apache-2.0**）：[NUS-HPC-AI-Lab/Enhance-A-Video](https://github.com/NUS-HPC-AI-Lab/Enhance-A-Video)
  —— 核心只有 `enhance_a_video/enhance.py` 里约 20 行的 `enhance_score()`。
  T8 的注释把版本钉在 commit `16a7899e6f55f85ea19f1d3a415c6dc0c4096176`（2025-03-08）。
- H3 适配参考（**GPL-3.0-or-later**，注意**不是** Apache-2.0）：
  [T8mars/comfyui-minimax-h3-audio-T8](https://github.com/T8mars/comfyui-minimax-h3-audio-T8)
  → `h3_t8/enhance_a_video_advanced.py`。T8 自称是「clean-room adapter derived from the
  equations」，即按论文公式重写、非代码移植。

> **关于「FETA」这个名字**：论文与官方仓库全文都没有出现过 FETA，官方术语只有
> Enhance-A-Video / CFI / enhance temperature。这个叫法出自 T8 的源码头注释
> （"derived from the equations in Enhance-A-Video / FETA"）。本模块沿用它是为了
> 与 T8 的示例工作流（`H3_Enhance_A_Video_FETA_*.json`）对照，**它不是论文术语**。

`eav_feta.py` **不依赖本项目其它模块**（只用 `torch` + `comfy_api` + 标准库），
也不修改任何既有文件。移除时删掉该文件与 `__init__.py` 里的两行注册即可。

---

## 它做什么

在每层注意力的输出上乘一个**全局标量 g**。g 由「跨帧注意力强度」CFI 决定：

```
CFI = (frames - trace) / (frames * (frames - 1))     trace = 各 T×T 矩阵对角元素之和
g   = max((frames + tau) * CFI, 1)
```

只作用于 packed 序列里的 **target-video 段**；text / cond / ref / audio 行原样不动。

因为 softmax 每行之和恒为 1，矩阵总和 = frames，所以**非对角和 = frames − 对角和** ——
不必 materialize 对角掩码，比参考实现少一次拷贝。对角和用 float64 累加保证精度。

---

## ⚠️ 先读这段：T8 实测下来，这个技术在 H3 上的增益几乎为零

T8 已完成 736×416 与 1152×640 两档、124 帧、20 步、同 seed 的基线/增强对照，
**0.7MP 档 20 次前向 × 50 个主块全部命中**，实测结果是：

| 指标 | T8 实测值 |
|---|---|
| **g 的取值** | **1.0000 ~ 1.0466，平均约 1.00034** |
| 新增工作区 | 约 7.90 MiB |
| **总耗时增加** | **约 7.2%** |

T8 自己的结论原文：

> 自动代理显示运动轨迹确有变化，但**清晰度没有明确提升，声音也不是 bit-exact**，
> 因此当前只证明机械可用，不证明稳定提质。

后续追加的 I2VA / FL2VA / L2VA / Turbo8 四组对照里，FL2VA 变化很小；
其余三组轨迹和音频变化明显，但**自动锐度同样没有提升证据**。

### 为什么 g 几乎恒等于 1

`g = max((frames + tau) * CFI, 1)`。当注意力是**对角占优**（每帧主要看自己）时
CFI 会低于 1/frames，于是 `frames * CFI < 1`，g 被 clamp 拉回 1.0 ——
只剩 `tau * CFI` 这一项贡献放大，量级约 `tau/frames²`。

H3 上 `frames` ≈ 37（latent 帧数，不是像素帧数），`tau=8` 时理论天花板
`g_max = (frames+tau)/(frames−1) ≈ 1.25`，但**实测平均只有 1.00034**。

**这意味着：这个技术值得先测、再决定要不要开。** 所以本节点默认是「仅报告」。

---

## 接线

```
UNETLoader → [LoRA] → [KJ 省显存 Attention / FFN] → H3EAVFetaPatch → 主节点「模型」
```

- **接在链尾**，即主节点「模型」输入之前；在 LoRA 和加速节点**之后**。
- **不需要 `sigmas` 输入** —— 进度窗口直接读采样器写入的 `transformer_options["sigmas"]`。
- 可与任何最终调用 `optimized_attention` 的注意力实现共存（走 ComfyUI 官方的
  `optimized_attention_override`，会保留并委托给已存在的覆盖，不抢 `add_object_patch` 的 key）。
- **报告节点是可选的** —— 不接也完全不影响功能（它只是读取端）。见下节。

## 怎么看到数据

**报告节点不接也能用。** 每次模型前向结束时，控制台会自动打一行：

```
[H3-EAV-FETA] 前向 #4，进度=0.300，测量块=50，CFI=0.022131~0.024812(均0.023104)，g=1.000000~1.001243(均1.000038)
[H3-EAV-FETA] 前向 #1，进度=0.050，窗口外（未测量、未介入）
```

一行对应一个采样步，所以 20 步就是 20 行 —— 不刷屏，也不用接任何线。

| 想做的事 | 要不要接报告节点 |
|---|---|
| 只判断「值不值得开」 | **不用**，看控制台就够了 |
| 拿到结构化 JSON、逐前向明细、便于归档对比 | 接。输出 `报告`（JSON）+ 节点上内联显示 |

报告节点接在采样**之后**的任意图像上（主节点「图像」输出 → 报告 → 视频合成），
它只读数据、原样透传图像，不改变任何生成结果。

报告 JSON 关键字段：`cfi`（`n/min/max/mean`）、`g`、`model_forwards`、
`active_forwards`、`measured_blocks`、`overflow_count`、以及逐前向的 `forwards` 数组。

## 参数

### 模式

| 取值 | 含义 |
|---|---|
| **仅报告**（默认） | 照常测量 CFI 和 g，但**不改输出**。用来判断值不值得开 |
| 应用 | 按 g 缩放 target-video 注意力行 |
| 关闭 | 完全不介入，模型对象原样直通 |

**`关闭` 才是严格旁路。`强度=0` 不是关闭**（那样 g 仍会被 clamp 到 1，但代码路径仍然执行）。

### 强度（tau）

论文的增强权重，范围 −32 ~ 32。放大项是 `tau * CFI`，所以 tau 越大放大越强。

- 参考项目工作流用 **4**（上游候选值），发布说明里的已审值是 **8**。
- T8 明确写过：`tau=4` **只是上游候选，不是 H3 最优值**。
- 考虑到实测 g 平均只有 1.00034，调 tau 的实际影响很小；真要试就小幅逐步加。
- T8 的负证据：`tau=12 / 0%~100%` 的真人样片因**效果过强被否决**。别一上来拉满。

### 起始进度 / 结束进度

生效窗口，单位是**采样进度**（`进度 = 1 − sigma`，与步数无关）。窗口外的前向完全不测量、不介入。

- 默认 **0.15 ~ 0.90**（参考项目发布说明里的已审窗口）。
- 基础 T2VA 模板用的是 0 ~ 1（全程生效）。
- 窗口越窄，耗时越省 —— 因为窗口外直接跳过 CFI 计算。

### 工作区上限MiB

分块计算 CFI 的**临时张量预算**，默认 32。

- 这只是**本节点自己**的缓冲上限，**不代表整套工作流的显存**。
- T8 实测真实占用约 **7.90 MiB**；调低它只会增加分块数（更慢），不会省多少。
- 6GB 显存建议保持 32 或更低。

### 增益上限

g 的安全上限，默认 1.5，范围 1.0 ~ 3.0。

- 理论上限 `g_max = (frames+tau)/(frames−1)`；H3 上约 1.25，所以 1.5 正常不会触发。
- **本节点超限时是「截断 + 计数」，不中断你的生成**（T8 的实现是直接报错拒绝运行）。
  截断次数会出现在日志和报告里，不会静默 —— 这是为了不让你跑了半小时的片子中途崩掉。

## 建议用法

1. **先跑 `仅报告`，固定种子。** 看控制台日志或报告里 CFI 和 g 的分布。
   - 若 `g` 的 min/max 都贴近 1.0（很可能如此，见上文 T8 数据），说明**没有放大空间**，
     那就别开「应用」—— 开了也只是白白多花约 7% 时间。
   - 若 g 明显大于 1，再切「应用」，固定种子 A/B 对比。
2. 切换时**固定素材、提示词、seed、尺寸和步数**，只改这一个开关。
3. **每条成片都要试听。** T8 已实测到联合 AV Transformer 后续层会间接改变声音
   （Prompt Relay 那组音频 RMS 变成 2.16 倍、波形相关 0.21），所以这不是纯画面操作。

## 已知边界

- **单次运行假设**：报告是模块级单例，`H3EAVFetaPatch` 每次执行会重置。
  同一队列里连续跑两次时，**报告节点**只反映最后一次；但**控制台日志**是逐前向实时打的，
  两次都会完整打出来。
- **控制台日志的最后一个前向**：靠主块数（`len(diffusion_model.blocks)`，H3 是 50）判断前向结束，
  所以每次都能打全。万一读不到块数，会退化为「下一个前向开始时才结账」，
  此时最后一次前向的日志可能不出现（报告节点不受影响）。
- **只在主 DiT block 生效**：序列长度不等于 packed 全序列的注意力（如 token_refiner）自动跳过。
- **不做质量承诺**：这是实验性适配。H3 是音视频联合 packed Transformer，
  这里只缩放 target-video 行，后续层仍可能间接影响音频。
- **不省显存**：这是画质方向的尝试，与 KJ 的省显存 Attention / FFN 是两回事，不能互相替代。
- 若上游改了 `transformer_options["minimax_h3_layout"]` 或 `optimized_attention_override`
  的契约，本节点会安全直通（不生效），不会报错中断你的生成。
