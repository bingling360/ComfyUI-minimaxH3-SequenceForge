# 坏案例优化对照（本模型按 skill 流水线手工执行，可直接贴 H3）

> 完整意图 IR（含 logline / beats / risks / clarify）见同目录 `B*.json`，均已过 `validate.py`（0 errors / 0 warnings）。
> 用法：复制每节 ```h3 代码块贴进 H3；B9 是部署坑，无需 prompt。

## B1. 台词变火星语 → 补 S1 身份加 `<d>`

- 原文：`一个女孩笑着说今天天气真好，快跟上我呀`
- 修法：默认唯一的女孩为 S1 并钉身份（年龄/在画/音色），台词逐字进 `<d>[Chinese]`。

```h3
integrated_multimodal_description: [Shot 1] Live-action sunny street in daylight. A young woman in her 20s (S1), on-screen, shoulder-length hair and a light jacket, walks toward the camera with a bright smile. The camera pushes in with small amplitude at slow speed as she says cheerfully in Mandarin <d>[Chinese] 今天天气真好，快跟上我呀。</d> One continuous shot with no cuts.
overall_soundscape: Light wind, distant traffic and her footsteps on the pavement.
non_diegetic_music: N/A
```

## B2. 对白烧成字幕 → 对白出引号，引号只留屏显

- 原文：`女孩说“跟上我”，背后霓虹灯写着“营业中”`
- 修法：`跟上我` 移入 `<d>`；只有 `reading "营业中"` 保留双引号且不翻译。

```h3
integrated_multimodal_description: [Shot 1] Live-action rainy night street, wet asphalt with neon reflections. A young woman in her 20s (S1), on-screen, dark ponytail and denim jacket, walks past shopfronts as the camera tracks beside her at slow speed. She stops beneath a red neon sign reading "营业中", turns toward the camera and says firmly in Mandarin <d>[Chinese] 跟上我。</d> One continuous shot with no cuts.
overall_soundscape: Steady rain on awnings and her footsteps on wet stone.
non_diegetic_music: N/A
```

## B3. 无端配乐 → N/A 加具体声源，不写“安静一点”

- 原文：`深夜便利店独白，安静一点`
- 修法：`non_diegetic_music: N/A`；soundscape 只点名电流声加远车流；“独白”无词暂按无对白处理，clarify 追问台词。

```h3
integrated_multimodal_description: [Shot 1] Live-action convenience store interior past midnight, cool fluorescent light over empty aisles. A male clerk in his 30s in a dark uniform slowly wipes the counter, the only person in the store. The camera holds a static wide shot as he pauses and gazes out the glass door at the empty street. One continuous shot with no cuts.
overall_soundscape: Low fluorescent hum and faint traffic beyond the glass, nothing else.
non_diegetic_music: N/A
```

## B4. 300 字企划书 → 压成一句话加 2 节拍

- 原文：`赛博朋克少女+身世+三幕情绪+运镜清单……约300字`
- 修法：只保留迷茫到坚定，其余砍进 risks 说明；8 秒 2 镜，每镜单运镜。

```h3
integrated_multimodal_description: [Shot 1] Stylized cyberpunk night city, rain-slicked streets under dense neon signage. A teenage girl with silver bob hair and a patched techwear jacket drifts through the crowd with lowered gaze as the camera tracks beside her at slow speed. [Shot 2] At 00:04.500, the camera cuts to a slow push in on her face as she stops, lifts her chin and stares ahead with hardened resolve.
overall_soundscape: Steady rain, muffled crowd murmur and footsteps on wet pavement.
non_diegetic_music: N/A
```

## B5. 台词 rushed → 39 字配 12 秒（4.3 字/秒硬约束）

- 原文：5 秒镜塞 39 字台词加转身加推镜
- 修法：台词逐字保留，时长延至 12 秒；若坚持 5 秒必须剪到 20 字内。

```h3
integrated_multimodal_description: [Shot 1] Live-action station plaza in daylight. A young woman in her 20s (S1), on-screen, light trench coat and canvas bag, turns and strides toward the station entrance as the camera follows behind her at steady speed. [Shot 2] At 00:03.000, the camera cuts to a medium close-up and pushes in with small amplitude at slow speed as she stops and speaks earnestly in Mandarin <d>[Chinese] 我昨天想了一整晚，我觉得我们还是应该把这件事说清楚，不然以后见面多尴尬啊你说对不对。</d>
overall_soundscape: Plaza crowd murmur, her hurrying footsteps and rolling luggage nearby.
non_diegetic_music: N/A
```

## B6. 运镜打架 → 只剩固定加单次右摇

- 原文：`环绕+推近+拉远+跟拍，一镜到底`
- 修法：单镜单运镜；换镜必须带时间戳。

```h3
integrated_multimodal_description: [Shot 1] Live-action concrete tunnel interior at dawn, bright daylight glowing at the exit. A female runner in her 20s in a grey singlet sprints toward the light as the camera holds a static wide shot. [Shot 2] At 00:04.000, the camera cuts to the open track outside and pans right once at slow speed to reveal the empty lanes ahead as she runs on.
overall_soundscape: Echoing footsteps swelling in the tunnel, then wind and birdsong outside.
non_diegetic_music: N/A
```

## B7. 碰撞与液体 → 不拍瞬间，遮挡转场加 aftermath

- 原文：`手撞倒咖啡杯，咖啡漫开淹没桌上的钥匙`
- 修法：Shot1 手臂扫过盖镜；Shot2 切静帧（杯已倒、渍已停、钥匙差几厘米没碰到），液体写死边界。

```h3
integrated_multimodal_description: [Shot 1] Live-action wooden kitchen table in morning light, an upright ceramic mug near the edge and a brass key beside it. A forearm sweeps in from the frame edge and passes close across the lens, fully covering the view for a beat. [Shot 2] At 00:02.500, the camera cuts to the settled aftermath from a static close angle: the mug lies on its side, a dark coffee stain spread about as far as the width of a hand and no further, its leading edge a few centimetres short of the brass key.
overall_soundscape: A ceramic clink and a soft liquid splash at the cut, then room quiet.
non_diegetic_music: N/A
```

## B8. 9 连拍冻脸 → 拆刺激加屏息加单主反应

- 原文：`震惊九连拍：睁眼+扬眉+张嘴+后退+吸气+眨眼+转头+出汗+颤抖`
- 修法：Shot1 刺激加屏息（肩膀锁高），Shot2 特写只做睁眼加退半步；不说话故不编号；无静止句。

```h3
integrated_multimodal_description: [Shot 1] Live-action dim apartment hallway in the evening. A young woman in her 20s in a loose sweater with tied hair walks down the hallway as a friend steps out from behind a door. She freezes mid-step, shoulders locked high, breath held. [Shot 2] At 00:02.500, the camera cuts to a close-up as her upper eyelids open fully and she falls back a half-step with a sharp inhale.
overall_soundscape: Hallway quiet, a door creak, then her sharp inhale.
non_diegetic_music: N/A
```

## B9. 本地版不说中文 → 部署排查（非 prompt 问题）

1. 换官方 repo tokenizer（不许复用旧 Qwen tokenizer）；
2. 提交链路 Unicode 安全（powershell 设 UTF-8，key 与文本不经过 GBK）；
3. 对白坚持 `<d>[Chinese] 台词</d>`。三步做完云端本地行为一致才算排除。

## B10. 参考图漂移 → Ref2VA 六段式，先定职责再用

- 原文：给了 3 张参考图但没说谁是谁
- 修法：图1身份 `fully_preserved`、图2背景 `partially_preserved`、图3夹克 `attribute_transfer`，每镜重复 `<Subject 1>`。

```h3
subject_definitions:
<Subject 1>: the teenage girl from <Picture 1>, silver bob hair, slim build
<Picture 1>: identity reference for <Subject 1>, face and hair
<Picture 2>: background and style reference, rainy neon street at night
<Picture 3>: outfit reference, patched techwear jacket for <Subject 1>

summary: [reference generation] A 5-second clip of <Subject 1> walking through the rainy neon street from <Picture 2>, wearing the jacket from <Picture 3>.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved, same face, hair and proportions
<Picture 1> (identity): fully_preserved
<Picture 2> (background): partially_preserved, neon layout and wet mood kept, crowd may change
<Picture 3> (outfit): attribute_transfer, jacket pattern transferred onto <Subject 1>

detailed_description: [Shot 1] Stylized rainy neon street at night consistent with <Picture 2>. <Subject 1> walks through the crowd with lowered gaze, the patched jacket from <Picture 3> clearly visible. The camera tracks beside <Subject 1> at slow speed. One continuous shot with no cuts.
overall_soundscape: Steady rain and footsteps on wet pavement.
non_diegetic_music: N/A
```
