# 09 · 多段提示词 JSON 文件（导入按钮吃的那一版）

`04-master-format.md` 那套文本是给**人粘贴**用的。这一份是同一份内容的**另一种外包装**：给 SequenceForge 顶栏「📥 导入提示词JSON」按钮吃的**文件**。

**什么时候交这份：**

- 用户说「导出 JSON」「给我一个 json 文件」「我要用导入按钮」「存成文件」；
- 或者由你判断：段数多、正文长（六段式 / 十来段），从聊天界面复制容易丢换行。

用户没提文件就**默认交文本**（`SKILL.md` 的交付形态不变）。JSON 不是新的第三种格式——**段级字段仍然只有三个**，一个不多。

**这份文件不必是整份稿子。** 用户说「我已经有 1-3 段了，再给我 4-6 段」时就**只出 4-6 段**（`index` 写 4 / 5 / 6）；说「只改第 2 段」时就只出那一段（`index: 2`）。导入器按 `index` 决定落到哪几段——详见「续写 / 覆盖已有的链」。

## 与文本版逐项对应

| 文本写法                      | JSON 字段                    | 说明                                        |
| ------------------------- | -------------------------- | ----------------------------------------- |
| `[Segment N]`             | `segments[].index`         | `index` 是**目标段号**：写 4 就是「落到链的第 4 段」。不写 = 按数组顺序 1..N |
| `Duration: N`             | `duration: N`              | **数字**，不要写 `"10s"`（能认，但没必要）                |
| `Standalone: yes\|no` | `standalone: true\|false` | **布尔**，不要写 `"yes"`                         |
| `Prompt:` 后的整段正文         | `prompt: "…"`              | 官方格式正文原样搬进来，**空行写成 `\n\n`**              |
| `Define: Picture 1 = 猫.png` | `defs: {"Picture 1": "猫.png"}` | 可选：显式固定编号（见「固定编号」一节）；不写就按 @ 顺序自动排  |
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

## 续写 / 覆盖已有的链（`index` 是**目标段号**）

导入**不是只能整链替换**。同一份 JSON 既能补后半段，也能只改一段 —— 判据只有一个：`index` 从几起。

| 场景                  | 文件里写什么                     | 导入后                       |
| ------------------- | -------------------------- | ------------------------- |
| 整份新稿（替换整链）          | `index` 从 1 起              | 链变成这份文件的段数（旧的多余段被删）       |
| 已有 1-3 段，补 4-6 段     | 只写 3 段，`index` 依次 4 / 5 / 6 | 第 1-3 段**一字不动**，4-6 追加     |
| 只改第 2 段             | 只写 1 段，`index: 2`          | 第 2 段被覆盖；段数不变，第 1 / 3 段不动 |
| 写到第 6 段（中间留空）       | 只写 1 段，`index: 6`（现有 3 段）  | 第 4-5 段补成空段，第 6 段写入        |

四条规则：

1. **起点 = 最小 `index`**。所以「补后半段」的关键是让第一段 `index` = 现有段数 +1，**而不是 1**。
2. **`index` 只决定起点，不重排数组**：段号与数组顺序不一致时按**数组顺序**写（`[{index:5},{index:4}]` 会写成第 5、6 段）。要按 4、5 写就老实排成 4、5。
3. **每一段都要写 `index`**。缺一个，导入器就不敢猜起点，会退化成整链替换。
4. 用户**始终能在导入面板上改**「写入位置」（整体替换 / 从第 N 段起 / 追加到末尾）。你写对 `index` 只是让默认档落在正确位置 —— 别指望用户每次都去改。

⚠️ 用户没说要续写、你也不知道现有几段时，**一律按整份稿子写（`index` 从 1 起）**。凭空从 4 起编会落到错的位置，而且会静默把前 3 段之外的旧段留着。

导出侧同理：工作台「从当前链载入」拿到的是**整链**（段号从 1 起），照它改完贴回去 = 整链替换。只想改一段就别整链载入。

## 固定编号：`defs` 字段（可选）

想让某张图**固定**成 `<Picture 1>`（而不是按出现顺序排），用 `defs`：

```json
{
  "index": 1,
  "duration": 10,
  "defs": { "Picture 1": "猫.png", "Picture 2": "雨夜窄巷.png" },
  "prompt": "integrated_multimodal_description: [Shot 1] <Picture 1> stands at the mouth of <Picture 2> …"
}
```

- 键 = 官方编号：`Picture` / `Video` / `Audio` + 序号。**`Subject` 不收**（它是「可复用内容」的概念，不对应某个素材文件）。
- 值 = 素材名（池里的引用名，带后缀最稳）。
- **定义 = 引用**：正文里一次 `@` 都不写，这个素材也会被挂上并占住那个号。所以正文只写 `<Picture 1>` 就够了，不必写素材名。
- 号是**同类型里的第几位**（按类型各自从 1 数），**不含首尾帧锚** —— 有首帧锚的段，`Picture 1` 实跑是 `<Picture 2>`。
- **官方编号必须连续（1..N），没有跳号**：上游 tokenizer 按顺序发号（`<Picture 1>: <Picture 2>: …`）。正文里写 `<Picture 9>` 而只挂了 3 张图 → 那个标签找不到图像，**静默失效**。
- **想"排最后"就写大号**：3 张图里定义 `"Picture 9": "某图"` 的效果是"这张排最后"（实跑压成 `<Picture 3>`），顺序意图保住、不会被丢弃。

与文本版的 `Define:` 行逐字同义（见 `04-master-format.md`）。不需要指定顺序就**别写** `defs`：默认的「按 @ 出现顺序编号」更省事，素材改名也更好迁移。

## 字段表

| 字段                       | 必填    | 取值                                     | 不写会怎样                  |
| ------------------------ | ----- | -------------------------------------- | ---------------------- |
| `segments`               | **是** | 数组，每项一段                                | 整份导入失败（不会静默写空段）         |
| `segments[].prompt`      | **是** | 官方格式正文（英文），字段间 `\n\n`                | 该段留空并点名提示；**一段正文都没有则整份失败** |
| `segments[].duration`    | 推荐    | 正数（秒），与前一步分配的一致                       | 该段时长不动（继承导入前的值）         |
| `segments[].standalone`  | 推荐    | `true` = 独立镜头（跳转 / 闪回 / 蒙太奇）；`false` = 连续接上段 | 该段接续状态不动（继承旧值）         |
| `segments[].index`       | 续写 / 局部改时**必填** | 目标段号（1-based）                          | 不写 = 按数组顺序补 1..N（= 整链替换）  |
| `segments[].defs`        | 可选    | `{"Picture 1": "猫.png"}`（键是官方编号，值是素材名） | 不写 = 按正文 @ 出现顺序自动编号    |
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
| 段顺序不对              | 数组顺序就是写入顺序，`index` 只决定**起点**。要按 4、5、6 写，就把数组也排成 4、5、6      |
| 想续写结果整链被替换         | `index` 从 1 起了（或每段都没写 `index`）。续写要从「现有段数 +1」起编                |
| 续写后段数不对            | 导入面板档位还停在「整体替换」：这份 3 段，链就 3 段（切到「从第 N 段起 / 追加到末尾」）         |

## 交付时的动作

1. 文件名：`<片名>_segments.json`（UTF-8）。
2. 文件内容**只有 JSON**，不要代码围栏、不要前后加说明。
3. 用户要中文对照 → 走 `prompt_zh` 字段（逐段对应），**不要**在文件里再附一份中文段落结构。
4. 用户两种都要 → 先给文本（`[END]` 收尾），再给 JSON 文件，两份内容必须逐段一致。
5. 用户要的是**续写 / 只改某段**时：文件里**只放那几段**，`index` 写目标段号（现有 3 段要补 4-6 就写 4 / 5 / 6），文件外的说明里提一句「这份是第 4-6 段」——**别**把这句说明写进 JSON。
