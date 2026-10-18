# 09 · 多段提示词 JSON 文件（导入按钮吃的那一版）

`04-master-format.md` 那套文本是给**人粘贴**用的。这一份是同一份内容的**另一种外包装**：给 SequenceForge 顶栏「📥 导入提示词JSON」按钮吃的**文件**。

**什么时候交这份：**

- 用户说「导出 JSON」「给我一个 json 文件」「我要用导入按钮」「存成文件」；
- 或者由你判断：段数多、正文长（六段式 / 十来段），从聊天界面复制容易丢换行。

用户没提文件就**默认交文本**（`SKILL.md` 的交付形态不变）。JSON 不是新的第三种格式——**段级字段仍然只有三个**，一个不多。

## 与文本版逐项对应

| 文本写法                      | JSON 字段                    | 说明                                        |
| ------------------------- | -------------------------- | ----------------------------------------- |
| `[Segment N]`             | `segments` 数组里的第 N 项       | 顺序即段序；写了 `index` 就按 `index` 排             |
| `Duration: N`             | `duration: N`              | **数字**，不要写 `"10s"`（能认，但没必要）                |
| `Standalone: yes\|no` | `standalone: true\|false` | **布尔**，不要写 `"yes"`                         |
| `Prompt:` 后的整段正文         | `prompt: "…"`              | 官方格式正文原样搬进来，**空行写成 `\n\n`**              |
| `[END]`                   | 不需要                        | 数组结束就是结束                                   |
| 中文对照版                    | `prompt_zh: "…"`           | **只给人看**：导入器不读它，写了对导入没影响，但**不许**写进 `prompt` |

## 完整骨架

```json
{
  "format": "sequenceforge.master-prompt",
  "version": 1,
  "title": "雨夜市场 · 三分钟短片",
  "segments": [
    {
      "index": 1,
      "duration": 10,
      "standalone": false,
      "prompt": "integrated_multimodal_description: [Shot 1] Live-action, cinematic, a medium shot with slight handheld shake frames the narrow alley of a rainy night market, neon signs dragging long red and blue streaks across the standing water. The camera pushes in with small amplitude at slow speed as a young woman with a quiet, breathy voice (S1) stops and looks back, saying: <d>[Chinese] 你要走了吗？</d>\n\n[Shot 2] At 00:03.200, the camera cuts to a close-up of her side profile as the corners of her mouth lift slightly.\n\noverall_soundscape: Steady rain continues, tapping densely on the metal stall roofs, with distant vendors calling out over the crowd.\n\nnon_diegetic_music: N/A",
      "prompt_zh": "实拍、电影感，中景带轻微手持晃动……（同一段正文的中译，仅供阅读）"
    },
    {
      "index": 2,
      "duration": 8,
      "standalone": true,
      "prompt": "integrated_multimodal_description: [Shot 1] Live-action, she turns and squeezes into the crowd as the camera tracks right with small amplitude at moderate speed.\n\noverall_soundscape: The vendors' calls fade with distance while her footsteps become prominent.\n\nnon_diegetic_music: N/A",
      "prompt_zh": "实拍，她转身挤进人流……"
    }
  ]
}
```

## 字段表

| 字段                       | 必填    | 取值                                     | 不写会怎样                  |
| ------------------------ | ----- | -------------------------------------- | ---------------------- |
| `segments`               | **是** | 数组，每项一段                                | 整份导入失败（不会静默写空段）         |
| `segments[].prompt`      | **是** | 官方格式正文（英文），字段间 `\n\n`                | 该段留空并点名提示；**一段正文都没有则整份失败** |
| `segments[].duration`    | 推荐    | 正数（秒），与前一步分配的一致                       | 该段时长不动（继承导入前的值）         |
| `segments[].standalone`  | 推荐    | `true` = 独立镜头（跳转 / 闪回 / 蒙太奇）；`false` = 连续接上段 | 该段接续状态不动（继承旧值）         |
| `segments[].index`       | 可选    | 数字段号                                   | 不写就按数组顺序                |
| `title`                  | 可选    | 片名 / 工程名                               | 只显示在导入预览的标题上            |
| `segments[].prompt_zh`   | 可选    | 该段正文的中译                                | 导入器忽略（给人对照看）            |
| `format` / `version`     | 可选    | 照抄 `sequenceforge.master-prompt` / `1` | 只作标记，不参与解析              |

## 导入器还能吃下的几种外形（都是 agent 常见吐法，但**你只许按上面的骨架产出**）

- 顶层直接是数组（`[{…}, {…}]`）；
- 段数组换名：`segs` / `shots` / `prompts` / `list` / `items`；
- 单段对象（顶层就写着 `prompt` / `duration` / `standalone`，没有数组）；
- 把整篇总提示词文本塞进 `master_prompt` / `text` 字段 → 直接按 `04` 的语法解析；
- 段里不给 `prompt` 整串，而是按官方字段拆着给（`integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music`，Ref2VA 再加 `summary` / `subject_definitions` / `retention_analysis` / `detailed_description`）→ 按官方顺序拼回正文。

字段名与布尔值**大小写不敏感**；`"10s"` / `"8 秒"` / `"yes"` / `"是"` / `1` 都认。别拿这些宽容度当借口——产出按骨架写死。

## 会踩的坑

| 症状                 | 原因                                                         |
| ------------------ | ---------------------------------------------------------- |
| 导入说「不是合法 JSON」     | 文件里混了 markdown 代码围栏、解释文字、或者最后的中文对照版没放进 `prompt_zh` 而是裸写在 JSON 外面 |
| 导入说「没有读到任何提示词正文」  | 每段都没有 `prompt`（比如只写了 `duration`），或者字段名拼错                     |
| 段时长没变              | `duration` 写了字符串 / 0 / 负数 → 该字段被忽略（该段时长不动）                   |
| 正文里冒出中文           | 中文对照写进了 `prompt`，而不是 `prompt_zh`                             |
| 三字段糊成一坨            | `prompt` 里把 `\n\n` 写成了 `\n`（或写成了真换行以外的内容）——官方三字段靠空行分节        |
| 素材没挂上              | 想在 JSON 里另给一份 `refs` 清单。**没有这条通道**：引用照旧只认正文里的 `@素材名`，位置规则见 `04` |
| 段顺序不对              | 既没按数组顺序写、又写了乱序的 `index`（写了 `index` 就会按它重排）                   |

## 交付时的动作

1. 文件名：`<片名>_segments.json`（UTF-8）。
2. 文件内容**只有 JSON**，不要代码围栏、不要前后加说明。
3. 用户要中文对照 → 走 `prompt_zh` 字段（逐段对应），**不要**在文件里再附一份中文段落结构。
4. 用户两种都要 → 先给文本（`[END]` 收尾），再给 JSON 文件，两份内容必须逐段一致。
