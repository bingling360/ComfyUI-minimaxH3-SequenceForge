# H3 长视频提示词撰写

一个 skill，把用户的一段话变成**一份可直接粘进 SequenceForge 总提示词框的多段提示词**。

```
用户一句话
   │
   ├─ 提问（时长/段数 · 参考图 · 内容 · 衔接 · 风格 · 对白 · 声音）
   │
   ├─ 参考图分支
   │    ├─ 已有图 → 默认每段都用，往下走
   │    └─ 没有图 → 提问 → 给出「参考图提示词附录 + 初版提示词」→ 等用户生成
   │                → 读实际生成的图 → 校正 → 最终版
   │
   ├─ 逐段扩写剧本（中文，只管内容）
   ├─ 逐段压成 H3 官方格式（Ref2VA 六段式 / 常规三字段）
   └─ 拼成多段文本（段头 + 时长 / 独立镜头 / 提示词）
```

最终产物每段都符合：**官方格式** + **长度匹配该段时长** + **段间剧情连续**（非独立镜头时下段强接上段）。

## 形态

**纯提示词，零依赖、零 API Key。** 读完 `SKILL.md` 和它引用的模块就能产出结果——不需要脚本、网络或任何运行时。

## 文件结构（主文件短，工作模块各管一摊）

| 路径 | 作用 |
|---|---|
| `SKILL.md` | 流程编排 + 三条铁律 + 分步交付（**保持短**） |
| `references/01-interview.md` | 问什么、怎么问、问几轮 |
| `references/02-expand.md` | 剧本扩写：节拍/字数表、段间接续、发散边界 |
| `references/03-optimize.md` | 官方格式压写：字段集、规则分流、禁令、成品字数 |
| `references/04-master-format.md` | 总提示词框语法：段头、三标签、`@素材名`、陷阱 |
| `references/05-reference-images.md` | 参考图全流程（有无图两条分支） |
| `references/06-rules/*.txt` | 六份撰写规则原文（按模式选用） |
| `references/07-checklist.md` | 交付前自检清单 |
| `references/08-example.md` | 完整两段范例（动手前先看） |

主文件只做编排，细节全部下沉到模块——**单文件不长，避免生成时注意力涣散**。

## 来源

规则不是新写的，是从 `ComfyUI-minimaxH3-SequenceForge` 的现行实现里提取的：

| 部分 | 对应实现 |
|---|---|
| 扩写 | `tools/h3_prompt_expander/screenplay.py` + `prompts/system_screenplay.md` / `system_outline.md` |
| 优化 | `optimizer.optimize_once` / `build_system_prompt` + `prompt/*.txt`（六份已逐字复制进 `06-rules/`） |
| 总提示词格式 | `web/h3_director.js` 的 `parseMasterPrompt` / `mpRenderState` / `refsFromText` |
| 参考图流程 | 新增（原项目无此环节） |

## 验证

- 六份规则文件与源文件 **md5 全等**（逐字搬，无手抄走样）。
- 用**真实解析器**（从 `h3_director.js` 抽出 `parseMasterPrompt` / `refsFromText` 在 jsdom 里跑）验证过完整示例：
  段数 / 时长 / `独立镜头` / 六字段齐全与顺序 / 空行保留 / 镜号连续 / `[Shot 1]` 无时间戳 / 时间戳递增不超时长 /
  主描述字数落在 20–40 字/秒区间 / `@素材名` 能提取成 `seg.refs` / 段间承接 —— 41 项断言全过。
- 首次模拟跑出的**主描述只有 180 汉字（低于 10 秒段下限 200）**，被 `07-checklist.md` 拦下回炉补细节后达标——
  这正是自检清单存在的意义。

## 已知不一致（项目现状，未改动）

Ref2VA 有两条并行口径：`prompts.py` 的 `REF_FIELDS` 与 `build_system_prompt` 要**六段式**，
而默认 `rule_file="auto"` + 中文时注入的是**四字段**的 `custom_ref2v` 规则。
本 skill 的 `03-optimize.md` 把两条都写清了，并**推荐六段式**（官方口径、一致性最好）。
