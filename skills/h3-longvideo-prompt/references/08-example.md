# 08 · 完整范例（照这个写）

题材：30 秒藏族高原洪水 PV，CG 三维渲染，宏观大远景，不做人物近景。
配置：10 秒 / 段 × 3 段（此处示范前 2 段）；用户已按附录生成好参考图；走 Ref2VA 六段式（全段引用）。

## 交付文本（直接可粘贴）

```
[Segment 1]
Duration: 10
Standalone: no

Prompt:
subject_definitions:
<Subject 1> is the content defined by <Picture 1>, the reference image "雪山主峰_广角.png". It fixes the mountain ridge silhouette, the snow-line height and the sky tone.
<Subject 2> is the content defined by <Picture 2>, the reference image "藏袍牧民_远景.png". It fixes the cut and colour of the Tibetan robes worn by the distant group.

summary: [reference generation] Early morning on the plateau; the massif defined by <Subject 1> shows its first crack while still otherwise motionless, snow grains sliding down the steep slope, three herders abstracted from <Subject 2> stopping in the far distance to look up.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - ridge silhouette, snow-line height and sky tone stay unchanged throughout.
<Subject 2> (appears in [Shot 3]): partially_preserved - robe cut and colour retained; the figures remain distant silhouettes only.

detailed_description: A CG three-dimensional render with live-action-grade volumetric and global illumination. [Shot 1] A locked-off wide shot presses the whole massif into the right two thirds of the frame, the ridge line sharp, a single cold-white highlight refracting off the summit, grey-brown exposed rock and scree at the foot, a thin ice-crystal haze floating at mid-height, the snow field reading cold-white with a blue cast under a low side-backlight from the upper left, the sky an unlit deep slate grey. The camera pushes in with small amplitude at slow speed.

[Shot 2] At 00:04.000, the camera trucks right with large amplitude at slow speed while pressing down slightly. A narrow crack opens along the ridge line; ice shards and snow grains slide down the steep slope in a white streak, the lifted snow dust showing dense grains in the side light, the shadow inside the crack a deep blue, a few blocks breaking loose from the edge and tumbling down.

[Shot 3] At 00:07.500, the camera pushes in toward the crack with small amplitude at slow speed. The ice cross-section at the crack edge reveals layered blue strata and bubble marks. In the lower left, three herders of <Subject 2> stand still and look up, each about one tenth of the frame height, the deep-red robes picking up a rim light from the snow, one of them raising an arm to point at the ridge.

overall_soundscape: A steady low-frequency plateau wind, the fine rasp of snow grains sliding, one dull crack from deep inside the ice in the distance, light breathing from the herders.

non_diegetic_music: A sustained low string drone at a very slow tempo, a single soft kick from a low drum underneath, no build.

[Segment 2]
Duration: 10
Standalone: no

Prompt:
subject_definitions:
<Subject 1> is the content defined by <Picture 3>, the reference image "雪山主峰_广角.png". It is the collapsing massif.
<Subject 2> is the content defined by <Picture 4>, the reference image "洪水峡谷_航拍.png". It fixes the floodwater and the canyon terrain.

summary: [reference generation] Continuing from the previous segment, the ridge crack widens into a full collapse of that stretch; glacier ice and rock pour down the steep face, snow mist and rock dust rolling up to hide half the peak, while in the same frame floodwater surges out of the canyon mouth and climbs the gravel flat below.

retention_analysis:
<Subject 1> (appears in [Shot 1]): partially_preserved - ridge silhouette retained, the upper part decomposing into collapse debris.
<Subject 2> (appears in [Shot 2], [Shot 3]): fully_preserved - canyon direction, water colour and flat morphology unchanged throughout.

detailed_description: A CG three-dimensional render, continuing the previous segment's camera position and side-backlight direction. [Shot 1] The crack widens within seconds into a full collapse of that stretch of ridge; glacier blocks and grey-brown rock pour down the steep face in sheets, snow mist and rock powder rolling into a diagonal grey-white curtain in the side light, the outer edge of the curtain made of irregular geometric blocks that drag short dust trails as they fall. The camera pulls back with large amplitude at slow speed while rising into an aerial overhead.

[Shot 2] At 00:03.500, the collapse mist hides half the peak. The canyon mouth is revealed below; the turbid-yellow floodwater of <Subject 2> surges out of it, large in volume and fast in advance, lifting a continuous white foam band across the surface, climbing the gravel flat at the canyon floor and swallowing the small ice blocks that slid down earlier, the shore gravel shifting continuously in the current.

[Shot 3] At 00:08.000, the camera pans left with small amplitude at moderate speed. The floodwater advances downstream, the channel widening within seconds; broken white surge and drifting splintered wood turn up in the turbid water, the gravel flat is covered whole, and the waterline pushes all the way to the base of the rock wall on the left of frame.

overall_soundscape: A continuous low-frequency roar from the collapse, the crunch of rock striking rock, broad-spectrum water noise from the canyon mouth moving from far to near, the wind drowned out.

non_diegetic_music: The low strings turn to dense short-bow tremolo at a faster tempo, the low drum striking in a run of increasing force, no melody.

[END]
```

## 这个范例做对了什么

| 要点 | 体现 |
|---|---|
| 段间强关联 | 段2 首句明写「Continuing from the previous segment」「continuing the previous segment's camera position and side-backlight direction」；段1 的裂痕→段2 扩展成崩塌；段1 埋的「峡谷」在段2 兑现 |
| 长度配得上时长 | 两段主描述各约 280 汉字（10 秒 × 20–40 字/秒 = 200–400） |
| 六个字段齐全 + 顺序固定 | subject_definitions → summary → retention_analysis → detailed_description → overall_soundscape → non_diegetic_music |
| 段级标签写在 `Prompt:` 之前 | 段头 / `Duration` / `Standalone` / `Prompt`，顺序固定，**全英文** |
| `Standalone:` 显式写出 | 每段都写 `Standalone: no`（全片连续），不是省略 |
| 参考图默认全段引用 | 两段都引用了场景图；段2 追加洪水图 |
| **引用位置正确** | `@` 只出现在 `subject_definitions` 定义行；`summary` / `retention_analysis` / `detailed_description` 一律用 `<Subject N>` 指代 |
| **标签语义正确** | 可复用的内容（山、洪水）用 `<Subject N>`；`<Picture N>` 只作「图本身」出现 |
| 保留标记分流 | 可见内容走 `fully_preserved` / `partially_preserved`；没有音频所以不出现 `fully_copy` 那套 |
| 配乐走非叙事 | 只写乐器、速度、力度，不写「悲壮 / 史诗」 |
| 无对白不硬凑 | 全片无对白，`overall_soundscape` 只写环境音与物理声 |
| `[Shot 1]` 无时间戳 | 后续 `[Shot 2]` / `[Shot 3]` 带 `At MM:SS.mmm`，递增且 ≤ 10 秒 |
| 风格句在 `[Shot 1]` 之前 | 「A CG three-dimensional render with live-action-grade volumetric and global illumination.」独立成句，先立风格再开镜 |

## 反例提醒

- ❌ 段2 直接写「洪水冲进村子」——段1 没有任何铺垫，观众断线；且属于「不许改结局」之外的**加戏**。
- ❌ 每段都把「三名牧民穿深红藏袍」重写一遍——重复静态信息，浪费字数，还会让模型以为换了造型。
- ❌ 在 `detailed_description` 里写 `@雪山主峰_广角.png`——这正是「多参时正文冒出图片引用」的 bug；正文只能用 `<Subject N>`。
- ❌ 把 `Reference: 雪山主峰_广角.png` 写成独立标签——已废弃，且出现在 `Prompt:` 之后会**静默进正文**（见 `04-master-format.md`）。
- ❌ 段头写成 `【段1】` / `时长：10`——中文标签。解析器读得懂，但正文是英文，不产出。
