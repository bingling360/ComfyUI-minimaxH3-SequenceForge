# H3 官方格式约束（编译阶段唯一依据，理解阶段不许用它脑补）

来源：MiniMax 官方 `MiniMax-AI/MiniMax-H3` → `skills/h3-prompt-writing/`
（`SKILL.md` + `references/base-en.txt` + `references/ref-en.txt`）。
本文件是那两份指南的可执行摘要：**骨架一字不改，主体内容用中文**。

---

## 0. 语言分工（最高优先级，违反即失败）

| 类别 | 语言 | 说明 |
| --- | --- | --- |
| 字段名 / section 名 | 英文，逐字 | `integrated_multimodal_description` 等，拼写不许变 |
| 结构标记 | 英文，逐字 | `[Shot N]`、`At MM:SS.mmm`、`(S1)`、`<d>`、`</d>`、`<Subject N>`、`<Picture N>`、`<Video N>`、`<Audio N>`、`<scenetrans>`、`<cutoff>` |
| 保留标记（retention） | 英文，逐字 | `fully_preserved` / `partially_preserved` / `attribute_transfer` / `weak_reference` / `fully_copy` / `partially_copy` / `reference` |
| 任务类型前缀 | 英文，逐字 | `[reference generation]` 等，方括号内英文 |
| 关键帧对齐指令 | 英文，逐字 | 官方整句模板，只替换 `N` 与 `S.SS` |
| 中文对白原文 | **中文** | 只放 `<d>[Chinese] …</d>` 内，逐字保留 |
| 画面内可见文字 | **原语言** | 只放英文双引号内，原文不译 |
| **其余全部描述内容** | **中文** | 画面、动作、环境、光照、材质、运镜、音色描述等 |

**白话**：格式壳子是英文，壳子里的肉是中文。对白和招牌保持原语言。

---

## 0.5 素材引用：写 `@素材名`，不要写 `<Picture N>`（本工具约定）

本节是**本工具与官方指南的唯一差异**，其余照抄官方。

- 正文里引用参考素材时写 `@素材名`（名字由 user 消息的「随图说明」给出，如 `@回廊场景`）。
- 后端会按**本段勾选顺序**把它压实成官方 `<Picture k>` —— 所以最终送进模型的文本
  仍是合规官方格式，只是**中间稿**用名字。
- 好处：素材顺序变动不会错位；回填时前端会自动把素材挂到该段；外部 agent 只凭
  素材名就能独立写完直接粘贴。
- `<Subject N>` / `<Video N>` / `<Audio N>` 与关键帧对齐指令**仍按官方写法**（它们不是
  素材名索引的范畴）。

---

## 1. base 模式：三字段结构（T2VA / I2VA / FL2VA / L2VA）

### 1.1 字段与顺序（错序即失败）

```text
integrated_multimodal_description: [Shot 1] ...

overall_soundscape: ...

non_diegetic_music: ...
```

* 三字段名称与顺序固定，官方示例中**字段之间空一行**。
* `overall_soundscape` **无内容写 `N/A`**（官方 §4.6：仅当用户明确要求全片无声才写 N/A；即默认要有环境音）。
* `non_diegetic_music` **无配乐必须写 `N/A`**（官方 §4.7：没有非剧情音乐时用 N/A）。

### 1.2 关键帧对齐指令（仅 I2VA / FL2VA / L2VA）

指令必须是**最终提示词的第一行**，其后**空一行**再接三字段。

I2VA（首帧，固定句）：
```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
```

FL2VA（首尾帧，固定句）：
```text
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.
```

L2VA（尾帧，固定句）：
```text
How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.
```

* `N` = 实际最后一镜的编号；`S.SS` = 有效时长，**恰好两位小数**。
* T2VA **没有**对齐指令，直接从三字段开始。

---

## 2. 镜头与剪辑（`integrated_multimodal_description` 主体）

用中文写画面，但镜头标记用英文骨架：

* `[Shot 1]` 开场**不加时间戳**；在 `[Shot 1]` 之后、描述之前先声明整体风格（中文可写「实拍、电影感」，风格词表见 §6）。
* 后续镜头：`[Shot N] At MM:SS.mmm, ...`，时间**严格递增**且落在时长内。
* 普通切镜用 `the camera cuts to` 类词汇（可中英混排写「镜头切换到」但 `[Shot N] At …` 骨架保留英文）；仅用户明确要求才用叠化/淡入淡出/擦除。
* 每次切镜必须带来**新信息**（新主体／新空间／新状态／新视角／新时间）；只是距离或角度微调，用运镜不要切镜。
* 单镜节拍 ≤3；超了拆镜。

---

## 3. 运镜：类型 + 幅度 + 速度

* 运镜词表（英文，逐字）：`Zoom In / Zoom Out`、`Push In / Pull Out`、`Pan Left / Pan Right`、`Truck Left / Truck Right`、`Tilt Up / Tilt Down`、`Pedestal Up / Pedestal Down`、`Arc Shot`、`Tracking Shot`、`Static Shot`、`Shake Slightly / Shake Strongly`、`POV`、`Roll Clockwise / Roll Counterclockwise`。
* 幅度：`with small amplitude` / `with large amplitude`；速度：`at slow speed` / `at fast speed`。**中等幅度与正常速度通常省略**。
* 写法：运镜作为**句内自然动作**，不要堆在句尾当标签。
  * 对：`镜头以小幅慢速推近她手中折起的信纸`（中文主体，运镜语义完整）
  * 错：`…，推近，小幅度，慢速`
* `Zoom In`（机身不动变焦距）与 `Push In`（机位前移）语义不同，不许混用。
* 单镜只写**一个**运镜。

---

## 4. 说话人、对白、歌声

* 会出声的角色给稳定 ID：`(S1)` `(S2)`；多人齐声 `(S1,S2)`。同一角色跨镜保持同一 ID；**不出声的角色不给 ID**。
* 角色首现时钉身份：年龄/性别/是否在画内/音高/音色/语速/口音——**放在 `<d>` 外面**。
* `<d>` 内**只放语言标签 + 用户原文**，逐字保留含标点，**不翻译不改写**：
  ```text
  (S1) 用温柔的嗓音说道：<d>[Chinese] 我在下一站下车。</d>
  ```
* 画面内可见文字（招牌/横幅/标签/字幕/霓虹）放英文双引号，**原文不译**：
  ```text
  A red neon sign reading "营业中" glows above the doorway.
  ```
* 画外音用固定短语 `says in an off-screen voiceover`（此句为英文骨架），紧随其后声明画面中角色嘴唇闭合。
* 台词跨切镜：在两段连接处都用 `<scenetrans>`，并明确说明音频跨切镜连续（`continues seamlessly across the cut` 等）。
* 台词被视频结尾截断：用 `<cutoff>`。
* 台词后**不许跟冲突口部动作**；台词按 4.3 字/秒估时，超时先剪词或加时长。

---

## 5. Ref2VA：六段式结构

顺序固定（官方 §1）：

```text
subject_definitions:
...

summary:
...

retention_analysis:
...

detailed_description:
...

overall_soundscape:
...

non_diegetic_music:
...
```

### 5.1 `subject_definitions`（标签定义）

四种标签，逐字使用：

| 标签 | 含义 |
| --- | --- |
| `<Subject N>` | 从素材抽象出的**可复用/可修改的可见内容**（人、动物、物、场景、服装、道具、风格、动作、表情、姿势） |
| `<Picture N>` | 作为具体目标帧或分镜锚点的参考图 |
| `<Video N>` | 提供剪辑源、续写起点或全片时间结构的参考视频 |
| `<Audio N>` | 被复制或被参考的音频信号 |

* 每个需单独追踪的参考项**独立一行**，说明标签指代什么、参考职责、要跟随的主要特征。
* `<Picture N>`/`<Video N>` 若只是标识另一参考项的来源、后续不单独分析，**写在那一项的句子里**，不另起一行。
* `<Audio N>` 明确对应目标说话人时复用其全局 ID：`<Subject N> (Sx)`。
* `<Video N>` 与 `<Audio N>` **各自独立编号**，索引只表示本类内顺序，不编码两者配对关系。
* 参考视频不因其文件含声音就自动产生 `<Audio N>`。

### 5.2 `summary`（一段英文，方括号任务前缀开头）

任务类型（可 ` + ` 组合，不重复）：
`keyframe completion` / `reference generation` / `video editing` / `video continuation` / `audio reuse` / `audio reference`

* 仅在真正扮演该角色时才计入；参考视频只提供运镜/剪辑/节奏 → 归 `reference generation`。
* 本段**不许引入新标签**。
* 剪辑任务在任务前缀后以 `The target video is an edited version of <Video 1>.` 起头。

### 5.3 `retention_analysis`（每标签一行）

可见内容标记（`<Subject N>`/`<Picture N>`/`<Video N>`）：
`fully_preserved` / `partially_preserved` / `attribute_transfer` / `weak_reference`

音频标记（`<Audio N>`）：
`fully_copy` / `partially_copy` / `reference` / `weak_reference`

* 行格式：`<Subject 1> (appears in [Shot 1], [Shot 3]): fully_preserved - …`
* 音品行格式：`<Audio 1>: reference - …`
* **不许在本段写 `(Sx)`**。
* 只能在该标签于 `subject_definitions` 已定义的职责范围内选择标记。

### 5.4 `detailed_description`（主体）

* 主字段名与 T2VA 不同：这里是 `detailed_description`。
* **风格在 `[Shot 1]` 之前用一到两句英文先立**（T2VA 是写在 `[Shot 1]` 之后）。
* 镜头/运镜/说话人/对白/声音格式与 §2–§4 同。
* 首次出现重要 `<Subject N>` 时描述其参考特征、画面位置、当前动作；后续沿用同一标签不重定义。
* 具体帧锚点用自然措辞：`the shot begins from <Picture 1>` / `the shot's keyframe corresponds to <Picture 2>` / `the shot ends on <Picture 3>`。
* 参考主体发声时**同时保留标签与 ID**：`<Subject 2> (S1) turns toward ... and says, <d>[Chinese] …</d>`。
* 台词只被 BGM/完整音轨里的口头内容触发、没有具体人发声时，用 `<Audio N>` 作为声源，**不另造 `(Sx)`**。
* 直接复用的参考音频台词/歌词/旁白，在 `<d>` 内保留原文原语言；听不清的跨度写 `[unclear]`，**不许猜或改写**；标点规范化为 `, . ? !`，去掉重复波浪号/emoji/项目符号/装饰性标点；完整陈述/疑问/感叹分别在 `</d>` 前以 `.` `?` `!` 收尾。
* 仅参考音色/节奏/情绪/演绎时，**不许**把参考音频里的原台词带进目标视频。
* `(Sx)` 按目标视频中实际发声事件顺序分配一次，之后每次发声复用同一 ID；不在 `retention_analysis` 里写 `(Sx)`。
* 长度：生成任务通常 **350–500 英文词**；对白密集时优先保证完整口播时间线，不机械凑词数；剪辑任务随源片复杂度伸缩。

### 5.5 `overall_soundscape` / `non_diegetic_music`

* 定义与 base 模式同（§1.1）。
* 用参考音频时，只在匹配的听觉层说明复制/参考关系：环境声与音效 → `overall_soundscape`；仅观众可听的配乐 → `non_diegetic_music`。
* 完整对白/歌词**只写在 `detailed_description` 的 `<d>` 内**，这两段不重复。

---

## 6. 禁则（触发即胡编/念提示词）

* **禁抽象词**：`cinematic / beautiful / epic / masterpiece / 8K / 氛围感 / 大片感` 单独出现无效，必须翻译成可见的光/镜/动作。（注意：风格声明词 `Cinematic` 作为**风格标签**在 `[Shot 1]` 开头是官方允许的；此处禁的是**当作质量形容词滥用**。）
* **禁否定堆叠**：`不要/别/没有/无XX` 一律转正向（`画面干净无字幕无水印` 是唯一保留的否定）。实测否定会反向生成。
* **禁比喻作实体**：`像塔罗牌一样` 会真长出牌面；要么删，要么改写成几何+材质+尺寸+边界。
* **禁 `nothing changes / stays still` 全镜静止句**（会外溢冻住整镜）。停顿交给剪辑。
* **禁拍碰撞瞬间与液体体积**：改写为「遮挡转场 + 切 aftermath 静止状态 + 撞击声挂音轨」；液体写死边界。
* **提到即生成**：不入镜的名词一个字都别写。
* **情绪写生理顺序**：眉→眼→嘴；每段情绪配一次可见呼吸；无台词角色用大肢体。
* **机位先于表情**：先写角色在看什么、在不在画内，再写脸；不在画内先移机位。
* **配乐禁情绪词**：只写乐器、速度、节奏、动态变化，不写「悲伤的/史诗感的」。

---

## 7. 时长密度

* 4–15s；描述密度匹配时长：**约每 3 秒一个切点**，每镜至少 3 秒才能立住东西；10 秒内 4 镜已属激进。
* 对白预算：自然语速约 2.5 英文词/秒；10 秒片能承载的对白总量约 20–25 词（若还想让别的事发生）。
* 单镜只写一个说话人（最可靠的干净口型技巧）；两人要说话就切镜。
* 300 字企划书式输入必须压缩到可执行密度，不许全翻。
