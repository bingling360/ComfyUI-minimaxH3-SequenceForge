# H3 坏案例库（网上搜集 + 按失败机理归纳）

> 已逐条优化：对照与可直接粘贴的 H3 英文见 `optimized/before_after.md`，完整意图信封见 `optimized/B*.json`（validate 0 errors / 0 warnings）。

> 用法：每个案例含【用户原文】→【H3 症状】→【根因】→【本工具的修法】。
> 前 6 条来自官方指南/社区实测的高频翻车，可直接当回归集。

## B1. 台词变火星语/念提示词（最高频）

- 用户原文：`一个女孩笑着说今天天气真好，快跟上我呀`
- H3 症状：生成 gibberish / Sims-speak，或把“今天天气真好”念成含糊音（Reddit r/StableDiffusion H3 gibberish 帖 + 官方指南点名）。
- 根因：无说话人 ID、无 `<d>` 结构、无声角色没声明，H3 不知道嘴型挂给谁。
- 修法：首现钉身份 `A young woman in her 20s (S1), on-screen ...` + `(S1) says in Mandarin <d>[Chinese] 今天天气真好，快跟上我。</d>`；同镜无声者加 `the vendor nearby keeps silent`。

## B2. 对白烧成屏显字幕

- 用户原文：`女孩说“跟上我”，背后霓虹灯写着“营业中”`
- H3 症状：画面底部烧出“跟上我”字幕（官方明确：引号是屏显保留位）。
- 根因：对白误用双引号。
- 修法：对白进 `<d>[Chinese] 跟上我</d>`；只有 `reading "营业中"` 这种屏显才用双引号且保留原文不翻译。

## B3. 没要配乐却自动加音乐 / 要了安静却有声

- 用户原文：`深夜便利店独白，安静一点`
- H3 症状：自动铺钢琴 BGM（社区 20% 漏配乐报告）。
- 根因：`non_diegetic_music` 留空/含糊 + soundscape 写了“氛围感”。
- 修法：无配乐写 `non_diegetic_music: N/A`；soundscape 只点名具体声（`fluorescent hum, distant traffic`），抽象词删掉。

## B4. 300 字企划书 → 完全跑偏（Threads 高赞吐槽）

- 用户原文：`赛博朋克少女+身世+城市隐喻+三幕情绪+运镜清单……300字`
- H3 症状：方向全错；改到 50 字精简版反而对了（20+ 次实测）。
- 根因：H3 按序处理信息，实体过多即漂移；密度与 5s 时长不匹配。
- 修法：Jump1 先压成一句话 logline + ≤3 beats，Jump2 只编译 60-120 词执行版，砍掉身世隐喻（那是分镜/剪辑的活）。

## B5. 对白 rushed / 说不完（pacing 翻车）

- 用户原文：5s 镜塞 40 字台词 + 转身 + 推镜。
- H3 症状：语速飞起或后半截被切（u/Relevant_One_2261 等多人报 pacing 是最大问题）。
- 根因：中文 4.3字/秒硬约束，40字要 9s。
- 修法：剪到 ≤20 字或加时长；台词跨镜加 `<scenetrans>`；真人配音场景先量语速再定镜长。

## B6. 随机切镜 / 运镜打架

- 用户原文：`环绕+推近+拉远+跟拍，一镜到底`
- H3 症状：无时间戳却多个场景动词 → H3 自行加切镜。
- 根因：一切换一时间戳原则被违反。
- 修法：单镜单运镜；换镜写 `[Shot 2] At 00:03.500, the camera cuts to ...`；单 continuous 加 `One continuous shot with no cuts.`。

## B7. 碰撞与液体（模型能力边界，改提示词也救不了）

- 用户原文：`手撞倒咖啡杯，咖啡漫开淹没桌上的钥匙`
- H3 症状：手没碰到杯、杯悬空自转、咖啡无限增殖、钥匙从未被碰到（phileiny 实拍）。
- 根因：接触驱动因果 + 液体体积守恒是 H3 已知短板。
- 修法：Shot1 手臂扫过遮挡转场，Shot2 切 aftermath（杯已倒、渍已停）+ 撞击声挂音轨；液体写死边界 `spread about as far as the width of a hand and no further`。

## B8. 单镜塞 9 个表情 → 全冻住（phileiny 对照实验）

- 用户原文：`震惊：睁眼+扬眉+张嘴+后退+吸气+眨眼+转头+出汗+颤抖`
- H3 症状：PSNR 37-42dB，脸完全没动且无报错。
- 根因：多节拍挤单镜收敛成平均运动。
- 修法：拆成 戳子（刺激）/ 接收（屏息）/ 主反应（1 个）/ 收束 四镜，或单镜只留 1 主反应 + 1 呼吸；`nothing changes` 类静止句删掉，停顿交给剪辑。

## B9. 本地版 H3 不说中文（部署坑，非提示词错）

- 症状：同 prompt 云端会说、本地 ComfyUI 不说（@eternityspring 实测）。
- 根因：复用了 Qwen 旧 tokenizer + shell 编码吞中文。
- 修法：换官方 repo tokenizer + Unicode 安全提交 + `<d>[Chinese] 台词</d>`；本工具只保证文本正确，部署问题看终端编码。

## B10. 参考图身份漂移

- 用户原文：给了 3 张参考图但没说谁是谁。
- H3 症状：脸/衣服每镜换一套。
- 根因：reference role 未声明。
- 修法：Ref2VA 六段式 + `retention_analysis`（`fully_preserved` 钉脸，`attribute_transfer` 放衣服），每镜重复标签 `<Subject 1> (S1)`。
