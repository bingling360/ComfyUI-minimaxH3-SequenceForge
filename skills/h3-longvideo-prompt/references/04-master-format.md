# 04 · 总提示词框格式（最终拼装）

最终交付的文本要被 SequenceForge 的「总提示词框」解析并分配到各段。它只认下面这套语法。

**一律英文。** 段头、段级标签、结束语都用英文写。中文标签（`【段1】` / `时长：`）解析器**仍能读**（只是老存档兼容），但你**不许产出**——正文是英文，段头却写中文，贴进框里读起来是两种语言打架。

## 完整骨架

```
[Segment 1]
Duration: 10
Standalone: no

Prompt:
integrated_multimodal_description: [Shot 1] …

overall_soundscape: …

non_diegetic_music: N/A

[Segment 2]
Duration: 10
Standalone: no

Prompt:
…

[END]
```

- **段头** `[Segment N]`：`N` 可省。别名也认 —— `[Seg N]` / `[Part N]` / `[Shot Group N]`，大小写不敏感。**必须独占一行。**
- **结束语** `[END]`（或 `【完】` / `[End]`）：其后的内容不参与解析。
- **段级标签只有三个**：`Duration` / `Standalone` / `Prompt`。其余一律是正文。

## 续写 / 覆盖已有段落（`N` 是**目标段号**，不是装饰）

一份内容写进链里，默认是**整链替换**：这份有几段，链就变成几段（档位在导入面板的「写入位置」里，见下）。但整套语法也支持**只写后半部分**：

| 你的意图                | 段头怎么写                              | 结果                          |
| ------------------- | ---------------------------------- | --------------------------- |
| 整份新稿（替换整链）          | 从 `[Segment 1]` 起                  | 链变成这份内容的段数（多出来的旧段被删）        |
| 已有 1-3 段，补 4-6 段     | `[Segment 4]` / `[Segment 5]` / `[Segment 6]` | 前 3 段一字不动，4-6 段追加            |
| 只改第 2 段             | 只写一个 `[Segment 2]`                 | 第 2 段被覆盖，段数不变，第 1/3 段不动     |
| 在某段之前插入（留空补位）       | 从 `[Segment 6]` 起（现有 3 段）          | 第 4-5 段补成空段，第 6 段写入         |

两条硬规则：

1. **起点由最小段号决定**。写 `[Segment 4]`…`[Segment 6]` = 从第 4 段起写；写 `[Segment 1]` 起 = 整链替换（历史行为，不会"保留后面几段"）。
2. **段号与出现顺序不一致时，按出现顺序排**，段号只决定**写入起点**（`[Segment 5]` 写在 `[Segment 4]` 前面也照样按 4、5 写）。

导入面板里的「**写入位置**」可以改这个档：`整体替换` / `从第 N 段起写入`（默认按段号自动推断），还有「追加到末尾」一键把起点推到现有段数之后。所以**段号只是你的意图声明**，用户随时能改——但你要写对，否则默认档就落在错的位置。


## 中文对照版怎么放

交付时英文正文之后要附一份**中文对照版**（见 `SKILL.md`）。它**不是**要粘进框的东西，所以必须一眼可分：

- 用**一行醒目分隔**标出，例如
  `———————— 中文对照版（仅供阅读，不要粘进提示词框）————————`；
- 分隔行之后的段落**照抄同一套骨架**（段头 + `Duration` / `Standalone` / `Prompt` + 英文字段名），
  只把正文翻成中文；
- **对白 / 歌词不译**，`<d>[Chinese] …</d>` 原样保留；
- 解析器只认 `[Segment N]` / `Duration:` 这类英文标签，所以分隔行本身不会被当成段头 ——
  但**别**把分隔行写成 `[Segment ...]` 的样子。

## 三个段级标签

| 标签                    | 取值                                           | 语义                                                      |
| --------------------- | -------------------------------------------- | ------------------------------------------------------- |
| `Duration: N`         | 正数秒                                          | 本段时长，必须与前一步分配的一致                                        |
| `Standalone: yes\|no` | `yes/y/true/on/1` = 是；`no/n/false/off/0` = 否 | **yes** = 本段**不**自动引用上段尾帧（跳转 / 闪回 / 蒙太奇）；**no** = 连续接上段 |
| `Prompt:`             | 官方格式正文                                       | 真正进模型的内容。标签独占一行，正文从下一行开始                                |

`Standalone` 是**两态显式值**：`yes` 和 `no` 都要写出来。不写 = 该字段不动（继承旧值），不是「默认 no」。所以**每段都要写这一行**。

## ⚠️ 三个标签必须写在 `Prompt:` 之前

一旦出现过任一个「正文类」标签（`Prompt` / `Scene` / `Character` / `Ambience` / `Music` / `Intent` / `Script`），后面的 `Duration:` `Standalone:` 就**不再是标签**：

- `Duration` / `Standalone` 会变成**正文的一部分**（正文里写「the duration is 12 seconds」不会被误当段时长）；
- 之后再写别的标签行会**连同那一行被吞进正文、跟着进模型，而且不给任何提示**（已实测复现）。

所以拼装顺序固定为：段头 → `Duration:` → `Standalone:` → `Prompt:` → 正文。

## 废弃标签：一律不要写

中文旧标签 `场景：` `角色：` `环境音：` `配乐：` `意图：` `剧本：` `参考：`  
英文对应 `Scene:` `Character:` `Ambience:` `Music:` `Intent:` `Script:` `Reference:`

这些是旧版的段级字段，现在**只有提示词正文进模型**。场景 / 角色 / 环境音 / 配乐的正确位置是正文里的官方三字段（或六字段）。

写了不会报错，但内容**不会生效**、也不会进正文，只会在解析时收到一条「已忽略」提示。

> 唯一例外：`Reference:` 若出现在 `Prompt:` **之后**，那一行会被**静默吞进正文**（连「已忽略」都没有）。这也是「多参时正文里冒出图片引用」的常见来源之一 —— 别写它。

## 资产引用：正文里的 `@素材名`

**正文是唯一真相。** 本段挂哪些素材，完全由正文里 `@素材名` 的出现序列决定，没有第二份清单。

但**引用出现的位置是有讲究的**——见下一节。

```
Prompt:
integrated_multimodal_description: [Shot 1] Medium shot: <Subject 1> stands at the mouth of a rainy alley, neon from <Subject 2> dragging long streaks across the standing water.

overall_soundscape: Steady rain.

non_diegetic_music: N/A
```

匹配规则：`@` 前一个字符不能是字母/数字/下划线（防 `a@b.com`）；按素材名**从长到短**取最长命中；按首次出现顺序收集，重复引用会重复计数。

### `@素材名` 出现在正文的哪个位置（Ref2VA 重点）

后端会把正文里的 `@素材名` 序列**映射成官方的 `<Subject N>` / `<Picture N>` / `<Video N>` / `<Audio N>`**。所以你写 `@女主`，产物里是 `<Subject 1>`。

| 模式                               | `@素材名` 该写在哪                                                                          |
| -------------------------------- | ------------------------------------------------------------------------------------ |
| base（T2VA / I2VA / FL2VA / L2VA） | 直接写进正文（首帧图 / 尾帧图另由锚定设置挂，**不写 @**）                                                    |
| Ref2VA                           | **只**写在 `subject_definitions` 的定义行；`detailed_description` 及以后一律用 `<Subject N>` 等标签指代 |

**Ref2VA 的错误写法**（正是用户报的 bug）：

```
detailed_description: [Shot 1] @女主 stops and looks back, @雨夜窄巷 neon flickers.   ← 错
```

**Ref2VA 的正确写法**：

```
subject_definitions:
<Subject 1> is the content defined by <Picture 1>, the reference image "女主.png".
<Subject 2> is the content defined by <Picture 2>, the reference image "雨夜窄巷.png".

detailed_description: [Shot 1] <Subject 1> stops and looks back; the neon of <Subject 2> flickers across the wet ground.
```

定义行里用 `@素材名` 的场合：当你说的是「这张图本身」而不是「图里的内容」时（例如整张图当画风参考）。日常跟内容走，就按上面那样写 `<Subject N>` 的定义行。

### 音频 / 视频参考怎么映射

- **视频** → 每个参考视频一条 `<Video N>` 定义行（`<Video N> is the content defined by <Video N>, the reference video "xxx.mp4".`），正文里后续用 `<Video N>` 指代。
- **音频** → 每个参考音频一条 `<Audio N>` 定义行；保留标记用 `fully_copy` / `partially_copy` / `reference` / `weak_reference`（**音频专用**，别跟可见内容的 `fully_preserved` 那套混）。

## 素材定义：`Define:` 行（可选写法，替代正文里的 `@素材名`）

默认写法是正文里写 `@素材名`，编号按出现顺序自动排。**也可以显式定义编号**，正文里直接写官方标签：

```
[Segment 1]
Duration: 10
Standalone: no
Define: Picture 1 = 猫.png
Define: Picture 2 = 雨夜窄巷.png
Define: Video 1 = 参考运镜.mp4

Prompt:
integrated_multimodal_description: [Shot 1] <Picture 1> stands at the mouth of <Picture 2>, neon dragging across the wet ground. <Video 1> drives the handheld sway.
```

规则：

- **位置固定在 `Standalone:` 之后、`Prompt:` 之前**。写在 `Prompt:` 之后就进了正文块，那一行只会变成给模型看的字面量（不会被当定义，也不会报错——最坑的一种失败）。
- **只认 `Picture` / `Video` / `Audio` + 序号**。`Subject` 不行：它是「可复用内容」的概念（角色/场景），不对应某个素材文件。
- **定义 = 引用**：定义过的素材，正文里一次 `@` 都不写也会被挂上，并占住那个号。**别为了"挂上素材"硬在正文里写 `@素材名`** —— 只写 `<Picture N>` 才是官方那种写法。
- **号是「同类型素材里的第几位」**，按类型各自从 1 数。**首尾帧锚不占这个号** —— 有首帧锚的段，`Define: Picture 1` 实跑是 `<Picture 2>`（后端把帧锚强制排最前，这是官方的对齐语义）。
- **官方编号必须连续（1..N），没有跳号**。上游 tokenizer 是按顺序发号的（`<Picture 1>: <Picture 2>: …`，见 `comfy/text_encoders/minimax.py`）。正文里写 `<Picture 9>` 而只挂了 3 张图 → 那个标签找不到对应图像，**静默失效**（不报错，但等于没挂）。
- **想"跨顺序"就写大号，它表示"排后面"**。3 张图里 `Define: Picture 9 = 某图` 的效果是"这张排最后"（实跑被压成 `<Picture 3>`）：顺序意图保住，不会被丢掉。
- 可以只定义一部分，其余素材仍按出现顺序自动补位。

什么时候用它：

- 多图段里要把某张图**固定**成官方示例里的 `Picture 1`（验收方按号对图）；
- 素材名很长，正文里要重复写很多次 —— 定义一次，正文只写 `<Picture 1>`；
- 素材**不进正文**但要挂上（整张图当风格/构图锚，正文不提它）。

不需要固定编号时**别用**：`@素材名` 更短，而且素材改名时池子会统一迁移引用；`Define:` 行里的名字是死文本，改名后要手动跟着改。

## 其他会踩的坑

| 症状          | 原因                                                                         |
| ----------- | -------------------------------------------------------------------------- |
| 整篇被当成一段     | 段头没独占一行（从聊天界面复制时换行被吃掉）。一般会自动重分行，仍失败就检查段头写法                                 |
| 三字段糊成一坨     | 粘贴时字段之间的空行被删了——**空行必须保留**                                                  |
| 段时长没被认      | `Duration:` 写在了 `Prompt:` 之后                                               |
| 素材没挂上       | 引用写成了 `Reference:` 标签，而不是正文里的 `@素材名`                                       |
| 正文里冒出 `@女主` | Ref2VA 模式下 `@` 写进了 `detailed_description`（应只出现在 `subject_definitions` 定义行） |
| 想续写却把整链替换了  | 段头从 `[Segment 1]` 写起 = 整链替换。续写要把段号写成「现有段数 +1」起（已有 3 段 → `[Segment 4]`）    |
| 续写后段数不对     | 档位还停在「整体替换」：这份只有 2 段，链就剩 2 段。切「从第 N 段起写入」或「追加到末尾」                          |
| Define 行没生效   | 写在 `Prompt:` 之后了 —— 它必须和 `Duration` / `Standalone` 一起写在正文之前                        |
| Define 的号对不上  | 该段有首帧/尾帧锚：帧锚固定占 `<Picture 1>`（和 2），定义号是同类型里的第几位，不是最终官方号                    |
| 正文里的 `<Picture 9>` 像没生效 | 编号跳号。官方按顺序发号（1..N 连续），超出实际素材数的号找不到图像，静默失效。想"排最后"用 `Define:`，别在正文手写大号 |

段级标签是**单行元数据**：写完它就归位了，紧跟其后的裸正文归 `Prompt`，不会被并进别的字段。

## 同一份内容，另一种外包装：JSON 文件

上面这套文本是给人**粘贴**用的。用户要的是**文件**（说「导出 JSON」「给我 json」「我要用导入按钮」）时，同一份内容按 `09-json-export.md` 写成 JSON —— 段级字段仍然是这三个，一个不多。

两条路进的是**同一条写入逻辑**，只是入口不同：文本走「总提示词框」，文件走「📥 导入提示词JSON」。所以本文件里关于段头 / 三标签 / `@素材名` / 废弃标签 / **续写起点**的所有规则，在 JSON 里同样成立（`duration` ← `Duration`、`standalone` ← `Standalone`、`prompt` ← `Prompt`、`index` ← 段头里的 `N`）。

另外：用户在总提示词框里点「从当前链载入」拿到的是**整链**（段号从 1 起），照它改完贴回去 = 整链替换；只想改一段就别整链载入，按上面的「只改第 2 段」写单段。
