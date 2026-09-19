---
name: h3-prompt-expand
description: 把中文口语理解成意图并编译成 MiniMax H3 官方格式提示词（integrated_multimodal_description / overall_soundscape / non_diegetic_music 三字段，Ref2VA 六段式）。当用户说“转成H3能懂的/优化提示词/别让它念提示词/出烧录字幕了/胡编乱造”时使用。带确定性校验与有界修复。
---

# H3 提示词理解+转译

你不是格式套壳器。你是“理解用户真正想要什么，再把格式对到 H3 官方规范”的人。
格式基准是 MiniMax 官方 `skills/h3-prompt-writing`（`base-en.txt` / `ref-en.txt`），
本工具是它的可执行落地：**骨架英文一字不改，主体内容按需中文**（对白与屏显保持原语言）。

## 何时用我

用户给的是中文口语/企划书式长文/情绪词堆叠，且目标是 MiniMax H3（T2VA/I2VA/FL2VA/L2VA/Ref2VA）时，全程走本 skill，不要直接把中文贴给 H3。

## 调用方式（opencode bash 直调，优先）

```bash
# 1. 准备配置 JSON（结构与 optimizer.DEFAULT_CONFIG 相同：服务商/协议/API Key/模型/本地模型）
#    示例 cfg.json: {"mode":"api","provider":"runninghub","protocol":"openai","api_key":"sk-xxxx","model":"..."}

# 2. 一句话转译（模型取配置里的 model，可用 --model 覆盖）
python h3_prompt_expander/h3_expand.py "雨夜霓虹市场，一个女孩回头笑说跟上我" --duration 5 --config cfg.json

# 输出即 H3 官方格式（三字段间空一行；I2VA/FL2VA/L2VA 前置关键帧对齐指令块），
# 可直接贴进 SequenceForge 总提示词框（官方直通语义）或 H3 API。
# 贵一倍但更稳的双跳：
python h3_prompt_expander/h3_expand.py "……" --two-step --model glm-4.6 --config cfg.json
# 只校验不花钱：
python h3_prompt_expander/h3_expand.py --validate-only h3_prompt_expander/examples/envelope_ok.json
```

导演台内调用：路由 `POST /h3chain/expand`（走 `service.expand_via_config`），
前端"AI扩写"按钮直接用它，产出信封 + 确认卡，不再复制命令到终端。

离线时：照 `references/h3-dialect.md` 手工编译，并用 `validate.py` 自检。

## 四步流水线（第 2 步确认卡是强制的，不许跳过）

### Step 0. 预处理（免费，先跑，不花钱）

```bash
python h3_prompt_expander/normalize.py "用户中文原文"
```

对白/引号误用/抽象词/否定/高危模式先抽出来，后面喂给 LLM，不靠模型自己找茬。
这是泛化底座之一：任何输入先过这一层。

### Step 1. 理解（`prompts/system_intent.md`）

输出意图 IR：一句话 logline + 不变量（说话的才编号 S1/S2）+ 连续 beats + 逐字 dialogue + audio + risks + clarify。
输入含糊（“来点感觉/随便拍个氛围”）时必须给 2-3 个候选方向（A/B/C），**不许只猜一个**。

### Step 2. 确认（强制，用户点头前不许编译）

```bash
python h3_prompt_expander/h3_expand.py "……" --output full > /tmp/h3env.json
python h3_prompt_expander/confirm_card.py /tmp/h3env.json
```

**把确认卡原文展示给用户，等回复**：`确认` → 进 Step 3；`改……`/`选A` → 修订重入：

```bash
python h3_prompt_expander/h3_expand.py --from /tmp/h3env.json --revise "用户意见原文" --output full > /tmp/h3env2.json
python h3_prompt_expander/confirm_card.py /tmp/h3env2.json   # 再确认一轮
```

终端有人值守时可用 `--confirm` 一步走完（管道中会退出码 4，改走上面两条）。
**用户说不清想要什么恰恰是常态**：确认卡就是为此存在的——把机器锁定的假设摆出来让人点头，而不是把猜测藏进英文里。

## 提问工具硬规则（防代理人偷懒，有一条不满足就 STOP）

满足任一即**必须调用提问工具**（结构化选项），不许只贴一段文本让用户自己组织回答：

- `intent.clarify` 非空；或
- `intent.candidates` 非空；或
- 用户第一次用本工具（无历史偏好可继承）。

选项构造：candidates → 选项（id + label_zh + logline_zh）；clarify → 每题一问。
开放性问题（如魔法形式、持物）保留自定义输入，同时预置 2-4 个常见选项。
文本追问**只允许在提问工具不可用时**降级使用。

### 提问设计规则（选项本身也要 review，翻车实例：首帧×身份混问）

- **一题只问一个维度**：机制（首帧/I2VA 还是参考/Ref2VA）和职责（钉身份/定场景/给动作）是两个正交维度，不许塞进同一组选项。
- **选项说画面，不说黑话**：`首帧`、`Ref2VA`、`retention` 这类词不许裸奔，必须翻译成用户语言（“图2的画面直接动起来”而不是“当首帧”；“脸和衣服按图1长”而不是“fully_preserved”）。
- **同组选项互斥且可执行**：每个选项对应唯一确定的下游（模式/镜数/标签），选完不需要再追问“那到底是哪个”。
- **2-4 个选项 + 自定义**：开放题（魔法形式、具体动作）必须留自定义口；选项按推荐度排序，推荐的注明为什么（“最稳”而不是“推荐”两个字了事）。
拿到答案 → `--from`/`--revise` 重入 → 重新出卡 → validate → 过下面的自检清单 → 才能发英文。

## 发稿前自检（逐项打勾，缺一即停）

- [ ] 确认卡已全文展示给用户？
- [ ] 每个 clarify 都有**用户原话**答案（不是代理人脑补）？
- [ ] candidates 已三选一，或用户给了新方向？
- [ ] 修订已用 `--from`/`--revise` 重入并重新 validate？
- [ ] validate 0 errors，且无 `W_UNCONFIRMED`？
- [ ] 本次只动了用户点的变量（一次一变量），没顺手加戏？

### Step 3. 编译（`prompts/system_compile.md` + `references/h3-dialect.md` + 命中的场景包）

场景包按关键词自动路由（对白/无声情绪/动作追逐/产品/蒙太奇/液体接触/表情特写），兜底走通用方言。
风格档：`--style strict` 只收敛不发挥 / `balanced` 默认 / `creative` 允许补 1 个视觉细节。
想借用 T8 等案例库的机制时：`--style-note "机制描述"` 只吸收机制，不照抄人物。

### Step 4. 校验（`validate.py`，确定性）：errors 必须修（缺Shot/时长溢出/引号丢失/音画混杂/静止句/Ref2VA缺段），warnings 提示即可。repair 最多 2 次，修不好就硬失败并告诉用户缺什么，而不是编一个交差。

## 泛化说明（与 T8 的关系）

- T8 的泛化来自**案例库广度**（234 案例 + 8 官方场景）；本工具的泛化来自**原则兜底**：预处理规则层（一切输入先过）+ 场景包层（7 类高频模式）+ 确认环（剩下的问用户）。未知场景宁可问，不编。
- 两者互补：T8 的案例机制描述可以直接贴进 `--style-note` 当风格参考，本工具负责把它压成 H3 官方格式并校验。

## H3 翻车速查（见 bad_cases.md 全量）

| 症状 | 第一刀 |
|---|---|
| 台词变火星语/念提示词 | 补 `(S1)` 身份 + `<d>[Chinese] 逐字</d>`，无声角色显式写不说话 |
| 对白烧成字幕 | 对白移出双引号，双引号只留屏显文字 |
| 没要配乐却有音乐 | `non_diegetic_music: N/A`，soundscape 别写配乐词 |
| 随机切镜 | 单镜单运镜 + `One continuous shot with no cuts.` |
| 人物/产品漂移 | 首镜钉死外观，ref 模式加 retention_analysis |
| 碰撞/液体翻车 | 不拍瞬间：遮挡转场 + aftermath 静帧 + 音效补因果 |

## 与 SequenceForge「总提示词框」的关系

总提示词框是**纯分段流水线**（多段批量分配通道），不是格式目标、也不做 AI：
它的段级标签只有 `时长 / 独立镜头 / 提示词` 三个，其中 `提示词：` 正文才是本工具的产物。

- **单段**：把本工具输出的官方格式文本整段贴进该段的提示词框（后端官方直通，不重包装）。
- **多段**：按段头 + 三标签拼总提示词，每段的 `提示词：` 正文放对应段的官方格式文本
  （含三字段间空行；I2VA/FL2VA/L2VA 的对齐指令写在正文最前、空一行再接三字段）。
- **资产引用写在正文里**（`@素材名`），与段卡提示词框同一套语义；没有「参考：」标签
  了——旧文本里的 `参考：` 会被认出、丢弃并提示「已忽略」，不会并进正文。
- 旧「场景/角色/环境音/配乐」四标签同上：解析仍认（仅旧文本可粘回），但一律丢弃并提示。
