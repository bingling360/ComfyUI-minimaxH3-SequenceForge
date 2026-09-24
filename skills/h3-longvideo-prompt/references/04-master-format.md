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

## 三个段级标签

| 标签 | 取值 | 语义 |
|---|---|---|
| `Duration: N` | 正数秒 | 本段时长，必须与前一步分配的一致 |
| `Standalone: yes\|no` | `yes/y/true/on/1` = 是；`no/n/false/off/0` = 否 | **yes** = 本段**不**自动引用上段尾帧（跳转 / 闪回 / 蒙太奇）；**no** = 连续接上段 |
| `Prompt:` | 官方格式正文 | 真正进模型的内容。标签独占一行，正文从下一行开始 |

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

| 模式 | `@素材名` 该写在哪 |
|---|---|
| base（T2VA / I2VA / FL2VA / L2VA） | 直接写进正文（首帧图 / 尾帧图另由锚定设置挂，**不写 @**） |
| Ref2VA | **只**写在 `subject_definitions` 的定义行；`detailed_description` 及以后一律用 `<Subject N>` 等标签指代 |

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

## 其他会踩的坑

| 症状 | 原因 |
|---|---|
| 整篇被当成一段 | 段头没独占一行（从聊天界面复制时换行被吃掉）。一般会自动重分行，仍失败就检查段头写法 |
| 三字段糊成一坨 | 粘贴时字段之间的空行被删了——**空行必须保留** |
| 段时长没被认 | `Duration:` 写在了 `Prompt:` 之后 |
| 素材没挂上 | 引用写成了 `Reference:` 标签，而不是正文里的 `@素材名` |
| 正文里冒出 `@女主` | Ref2VA 模式下 `@` 写进了 `detailed_description`（应只出现在 `subject_definitions` 定义行） |

段级标签是**单行元数据**：写完它就归位了，紧跟其后的裸正文归 `Prompt`，不会被并进别的字段。
