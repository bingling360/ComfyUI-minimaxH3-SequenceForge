# h3_prompt_expander · H3 提示词理解+转译（独立工具）

中文口语 → 意图 IR → **H3 官方格式**（`integrated_multimodal_description` / `overall_soundscape` /
`non_diegetic_music` 三字段，Ref2VA 六段式），专治"胡编 / 念提示词 / 烧字幕 / 无端配乐"。

格式基准 = MiniMax 官方 `MiniMax-AI/MiniMax-H3` → `skills/h3-prompt-writing`
（`SKILL.md` + `references/base-en.txt` + `references/ref-en.txt`），本工具是它的可执行落地：
**骨架英文一字不改，主体内容按需中文**。

- LLM 通道统一走根目录 `optimizer.py`：云通道支持 OpenAI 兼容 / Gemini / Responses 三协议，
  本地通道支持 Transformers / GGUF。设置框里选什么，这里就用什么。
- 确定性校验 `validate.py` 无需 key，`--validate-only` 不花钱。
- 意图确认卡强制先行：用户点头前不编译，说不清就给候选方向三选一。
- 既做 CLI，也做服务：`service.py` 供节点/路由进程内直调（`POST /h3chain/expand`）。

> 泛化设计（相对纯案例库的取舍）：T8 靠案例广度覆盖风格，本工具靠三层兜底覆盖未知输入——
> `normalize.py` 规则层（一切输入先过，零成本）+ `references/scene-packs.md` 场景包层（7 类高频模式自动路由）
> + 确认环（剩下的问用户，不编）。两者互补，T8 案例机制可经 `--style-note` 外挂进来。

## 1. 配置

配置与导演台的"提示词优化设置"同结构（即 `optimizer.DEFAULT_CONFIG`），存成 JSON 后用 `--config` 传入：

```json
{
  "mode": "api",
  "provider": "runninghub",
  "protocol": "openai",
  "api_key": "sk-xxxx",
  "model": "openai/gpt-5.6-sol",
  "read_media": true,
  "max_tokens": 4096
}
```

```powershell
python h3_prompt_expander/h3_expand.py "……" --config cfg.json
```

`--config` 不给则用默认值（`optimizer.DEFAULT_CONFIG`，默认 RunningHub）。
本地模型把 `mode` 设为 `local`，并填 `local_model` / `local_mmproj` / `local_device`。

## 2. 用法

```powershell
# 一句话转译，输出直接贴 H3 / SequenceForge 总提示词框
python h3_prompt_expander/h3_expand.py "雨夜霓虹市场，一个女孩回头笑说跟上我" --duration 5 --config cfg.json

# 推荐：带意图确认的完整流程（先确认再编译，用户说不清时给候选方向）
python h3_prompt_expander/h3_expand.py "……" --output full --config cfg.json > /tmp/h3env.json
python h3_prompt_expander/confirm_card.py /tmp/h3env.json   # 把卡片给用户看
python h3_prompt_expander/h3_expand.py --from /tmp/h3env.json --revise "用户意见/选A" --output full --config cfg.json > /tmp/h3env2.json

# 风格档 + 场景机制外挂（T8 案例机制可贴进 --style-note）
python h3_prompt_expander/h3_expand.py "……" --style strict --config cfg.json
python h3_prompt_expander/h3_expand.py "……" --style-note "距离反转、惊吓后手势降级" --config cfg.json

# 管道 + JSON 输出（给 agent 消费）
echo "黄昏教室，短发少女写字后望向窗外说今天也早点回去吧" | python h3_prompt_expander/h3_expand.py --output json --duration 5 --config cfg.json

# 贵一倍但更稳的双跳（先理解后编译）
python h3_prompt_expander/h3_expand.py "……" --two-step --model glm-4.6 --config cfg.json

# 只校验 / 只预处理，不花钱（CI 同款）
python h3_prompt_expander/h3_expand.py --validate-only h3_prompt_expander/examples/envelope_ok.json
python h3_prompt_expander/validate.py h3_prompt_expander/examples/envelope_bad.json
python h3_prompt_expander/normalize.py "大片感，不要路人，咖啡漫开淹没钥匙"
```

返回码：`0`=校验通过，`1`=仅校验失败，`3`=已 repair 仍有 errors（信封随 stdout full 可查），`4`=待用户确认。

### 在导演台内直接用（推荐路径）

导演台「具象化提示词 → AI扩写」按钮已直连本工具，不必再开终端：

- 点 `［AI扩写］中文意图 → 官方格式` → 弹窗填一句中文意图 → `生成确认卡`
- 弹窗走 `POST /h3chain/expand`（服务端复用已保存的优化设置，无需在前端填 key）
- 确认卡核对无误后点 `确认并回填`：`integrated_multimodal_description`
  （Ref2VA 为 `detailed_description`）按 `[Shot N]` 切成多镜写入 `shots`，
  `overall_soundscape` / `non_diegetic_music` 写入同名字段
- 理解有偏 → 点 `提意见重改`，带着上一封信封 + 修订意见重入（`from_envelope` + `revision`）

对应的服务化入口是 `service.py: expand_via_config()`，返回值含
`intent / pe / envelope / h3_text / card_md / validation / ok / meta`。

## 3. 输出是什么

默认 `--output h3` 打印官方格式，字段之间空一行：

```
integrated_multimodal_description: [Shot 1] ...

overall_soundscape: ...

non_diegetic_music: N/A
```

I2VA / FL2VA / L2VA 会在最前面多一块**关键帧对齐指令**（独立首块，其后空一行再接三字段）：

```
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 2) aligns with the 8.00-second mark of the target video.

integrated_multimodal_description: [Shot 1] ...
```

Ref2VA 输出六段，顺序固定：

```
subject_definitions: ...

summary: ...

retention_analysis: ...

detailed_description: ...

overall_soundscape: ...

non_diegetic_music: N/A
```

**贴进 SequenceForge 时**：每条提示词框只放**一段**（后端官方直通不重包装）。
多段按「总提示词」格式拼（段头 + `时长/独立镜头/参考/提示词` 四标签），
每段的 `提示词：` 正文就是上面这份官方格式文本（含空行）；
I2VA/FL2VA/L2VA 的对齐指令写在正文最前、空一行再接三字段。

## 4. 坏案例回归

见 `bad_cases.md`（B1-B10：火星语/烧字幕/无端配乐/企划书跑偏/rushed/随机切镜/碰撞液体/9节拍冻脸/本地中文失声/身份漂移）。
`examples/envelope_ok.json` 应通过，`envelope_bad.json` 应报 E_D_* / E_FREEZE / E_SOUND_DIALOG / E_NO_MUSIC / E_QUOTE_LOST。

## 5. 文件

| 文件 | 用途 |
|---|---|
| `SKILL.md` | agent 入口（opencode 读我，含强制确认环） |
| `h3_expand.py` | 主逻辑（场景路由 + repair/修订重入 + 渲染 + CLI） |
| `service.py` | 服务化入口（`expand_via_config` / `validate_only`，供路由进程内直调） |
| `validate.py` | 确定性校验（无 LLM；含官方结构检查：对齐指令整句与 N/S.SS 一致性、Ref2VA 六段齐全与顺序、summary 任务前缀、retention 标记位置） |
| `normalize.py` | 确定性预处理（对白/引号/抽象词/否定/高危模式，不花钱） |
| `confirm_card.py` | 意图确认卡渲染（给用户点头/摇头/三选一） |
| `references/h3-dialect.md` | H3 官方格式约束（base-en / ref-en 的可执行摘要） |
| `references/intent-schema.json` | 信封 Schema |
| `prompts/system_intent.md` | 理解跳 |
| `prompts/system_compile.md` | 编译跳 |
| `bad_cases.md` | 失败模式库 |
| `optimized/` | B1-B10 优化对照：`before_after.md` 可直接贴，`B*.json` 为完整意图信封回归集 |
