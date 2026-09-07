# h3_prompt_expander · H3 提示词理解+转译（独立工具）

中文口语 → 意图 IR → H3 英文三字段，专治“胡编 / 念提示词 / 烧字幕 / 无端配乐”。

- 独立目录，不碰现有生成链；零第三方依赖（stdlib 即可跑）。
- 默认走智谱 GLM（OpenAI 兼容），可在 opencode/Claude/Codex 里 bash 直调。
- 确定性校验 `validate.py` 无需 key，`--validate-only` 不花钱。
- 意图确认卡强制先行：用户点头前不编译，说不清就给候选方向三选一。

> 泛化设计（相对纯案例库的取舍）：T8 靠案例广度覆盖风格，本工具靠三层兜底覆盖未知输入——
> `normalize.py` 规则层（一切输入先过，零成本）+ `references/scene-packs.md` 场景包层（7 类高频模式自动路由）
> + 确认环（剩下的问用户，不编）。两者互补，T8 案例机制可经 `--style-note` 外挂进来。

## 1. 配置

```powershell
$env:ZHIPU_API_KEY="sk-xxxx"          # 必填
# 强推理：$env:H3_EXPAND_MODEL="glm-4.6"   # 默认 glm-4-flash
```

或复制 `.env.example` 自行加载（本工具不自动读 .env，避免 key 落盘）。

## 2. 用法

```powershell
# 一句话转译，输出直接贴 H3 / SequenceForge 总提示词框
python h3_prompt_expander/h3_expand.py "雨夜霓虹市场，一个女孩回头笑说跟上我" --duration 5

# 推荐：带意图确认的完整流程（先确认再编译，用户说不清时给候选方向）
python h3_prompt_expander/h3_expand.py "……" --output full > /tmp/h3env.json
python h3_prompt_expander/confirm_card.py /tmp/h3env.json   # 把卡片给用户看
python h3_prompt_expander/h3_expand.py --from /tmp/h3env.json --revise "用户意见/选A" --output full > /tmp/h3env2.json

# 风格档 + 场景机制外挂（T8 案例机制可贴进 --style-note）
python h3_prompt_expander/h3_expand.py "……" --style strict
python h3_prompt_expander/h3_expand.py "……" --style-note "距离反转、惊吓后手势降级"

# 管道 + JSON 输出（给 agent 消费）
echo "黄昏教室，短发少女写字后望向窗外说今天也早点回去吧" | python h3_prompt_expander/h3_expand.py --output json --duration 5

# 贵一倍但更稳的双跳（先理解后编译）
python h3_prompt_expander/h3_expand.py "……" --two-step --model glm-4.6

# 只校验 / 只预处理，不花钱（CI 同款）
python h3_prompt_expander/h3_expand.py --validate-only h3_prompt_expander/examples/envelope_ok.json
python h3_prompt_expander/validate.py h3_prompt_expander/examples/envelope_bad.json
python h3_prompt_expander/normalize.py "大片感，不要路人，咖啡漫开淹没钥匙"
```

返回码：`0`=校验通过，`1`=仅校验失败，`3`=已 repair 仍有 errors（信封随 stdout full 可查），`4`=待用户确认。

## 3. 输出是什么

默认 `--output h3` 打印：

```
integrated_multimodal_description: [Shot 1] ...
overall_soundscape: ...
non_diegetic_music: N/A
```

贴进 SequenceForge 时：单段直接贴（后端官方直通不重包装）；多段按八标签段头拼，每段的提示词/环境音/配乐语义位贴对应行。

## 4. 坏案例回归

见 `bad_cases.md`（B1-B10：火星语/烧字幕/无端配乐/企划书跑偏/rushed/随机切镜/碰撞液体/9节拍冻脸/本地中文失声/身份漂移）。
`examples/envelope_ok.json` 应通过，`envelope_bad.json` 应报 E_D_* / E_FREEZE / E_SOUND_DIALOG / E_NO_MUSIC / E_QUOTE_LOST。

## 5. 文件

| 文件 | 用途 |
|---|---|
| `SKILL.md` | agent 入口（opencode 读我，含强制确认环） |
| `h3_expand.py` | 主 CLI（GLM 调用+场景路由+repair/修订重入+渲染） |
| `validate.py` | 确定性校验（无 LLM，含 Ref2VA 六段式） |
| `normalize.py` | 确定性预处理（对白/引号/抽象词/否定/高危模式，不花钱） |
| `confirm_card.py` | 意图确认卡渲染（给用户点头/摇头/三选一） |
| `references/h3-dialect.md` | H3 方言约束 |
| `references/intent-schema.json` | 信封 Schema |
| `prompts/system_intent.md` | 理解跳 |
| `prompts/system_compile.md` | 编译跳 |
| `bad_cases.md` | 失败模式库 |
| `optimized/` | B1-B10 优化对照：`before_after.md` 可直接贴，`B*.json` 为完整意图信封回归集 |
