# H3 方言约束（编译阶段唯一依据，理解阶段不许用它脑补）

来源：MiniMax 官方 `h3-prompt-writing` base-en/ref-en + aireiter/cdance 社区实测 + phileiny 表演约束。
目标：中文意图 IR -> H3 能稳定执行的英文三字段。

## 1. 三字段顺序与省略语义（错序即失败）

```
integrated_multimodal_description: ...
overall_soundscape: ...        # 无环境音则整行省略（不是 N/A）
non_diegetic_music: ... | N/A  # 无配乐必须写 N/A，否则 20% 概率漏配乐
```

* `overall_soundscape` 只放环境声+动作声+非语言人声（风/雨/脚步/呼吸/笑声），1-4 句一段。
* 对白/角色唱歌/角色能听到的音乐（收音机/街头演唱）**不许进 soundscape**，写进 description 事件行。
* `non_diegetic_music` 只放观众听到、角色听不到的配乐：乐器+节奏+动态，1-3 句。

## 2. 镜头记法

* `[Shot 1]` 开场无时间戳；换镜 `[Shot 2] At 00:03.500, the camera cuts to ...`，时间严格递增且在本段时长内。
* 单镜只许**一个运镜**，只许**≤3 个表演节拍**（phileiny 定律：9 节拍挤单镜=全抹平）。超了就拆镜。
* 运镜词只用：`Push In / Pull Out / Pan Left / Pan Right / Truck Left / Truck Right / Tilt Up / Tilt Down / Pedestal Up / Pedestal Down / Arc Shot / Tracking Shot / Static Shot / POV`，可加 `with small/large amplitude` + `at slow/fast speed`，写成句内动作，不要堆 5 个运镜。
* 单 continuous 镜头加一句 `One continuous shot with no cuts.` 防随机切镜。

## 3. 对白（烧录字幕/火星语重灾区）

* 说话人稳定 ID：`(S1)` `(S2)`，`(S1,S2)` 表齐声；不出声的角色不编号。首现必须钉身份：年龄/性别/在不在画/音色。
* 行格式：`(S1) says ... <d>[Chinese] 台词原文</d>`，identity/action/delivery 全在 `<d>` 外面，`<d>` 内只放语言标签+逐字原文，标点保留。
* **双引号 `"` 只给屏显文字**（招牌/霓虹/字幕），保留原文不翻译：`A red neon sign reading "营业中"`。对白进双引号=烧成字幕，必错。
* 画外音：`says in an off-screen voiceover ... while her lips remain completely closed.` 台词被结尾截断加 `<cutoff>`，跨镜持续加 `<scenetrans>` + `continues seamlessly across the cut`。
* 台词后**不许跟冲突口部动作**（吞咽/闭嘴放到发声跨度之外）；台词按 4.3 字/秒估时，超时就剪词或加时长。

## 4. 禁则（触发即胡编/念提示词）

* 禁抽象词：`cinematic / beautiful / epic / masterpiece / 8K / 氛围感 / 大片感` 单独出现无效，必须翻译成可见的光/镜/动作。
* 禁否定堆叠：`不要/别/没有/无XX` 一律转正向（`画面干净无字幕无水印`是唯一保留的否定）。Runway/H3 实测否定会反向生成。
* 禁比喻作实体：`像塔罗牌一样`会真长出牌面；要么删，要么改写成几何+材质+尺寸+边界。
* 禁 `nothing changes / stays still` 全镜静止句（会外溢冻住整镜）。停顿交给剪辑：Shot A 收在动作停止，Shot B 从反应开始。
* 禁拍碰撞瞬间与液体体积：`手撞倒杯子、咖啡漫开淹没XX` 做不到。改写：手臂扫过遮挡转场 + 切 aftermath 静止状态 + 撞击声挂音轨；液体写死边界（`spread about as far as the width of a hand and no further`）。
* 提到即生成：提示词里出现的每个名词都可能长出来，不入镜的东西一个字都别写。
* 情绪必须写生理顺序：眉->眼->嘴；每段情绪配一次可见呼吸；无台词角色用大肢体（呼气落肩/抹裤/抓颈）别堆脸部 micro-expressions。
* 机位先于表情：先写角色在看什么、在不在画内，再写脸；不在画内先移机位，并加 `she does not look at the camera`。

## 5. 时长密度

* 4-15s，描述密度匹配时长：5s 最多 2-3 beats + 1 台词（≤20 字）； dialogue 太长一定 rushed，先剪词。
* 300 字企划书式输入必须压缩到 50-120 词英文执行版（Threads 20+ 次实测结论）。
