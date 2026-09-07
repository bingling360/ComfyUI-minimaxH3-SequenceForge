# 官方 H3 提示词来源快照

本目录保存官方 skill 文本的**只读快照**，作为 B05 及以后提示词编译器的固定依据。
固定样例测试必须使用本快照，不得在测试中联网，也不得用仓库社区 expander 的
字数、摘要或语言规则覆盖官方格式。

| 文件 | 来源 URL | 字节 | SHA256 |
| --- | --- | --- | --- |
| `SKILL.md` | https://raw.githubusercontent.com/MiniMax-AI/MiniMax-H3/main/skills/h3-prompt-writing/SKILL.md | 2623 | `a7000443588ca3f145e3b3fd8900f14e0325dc460bd811268fac89a9dc8e56d0` |
| `base-en.txt` | https://raw.githubusercontent.com/MiniMax-AI/MiniMax-H3/main/skills/h3-prompt-writing/references/base-en.txt | 15773 | `2cfebc096a6e08370f288d468d90b60f7f9bcb938f94bf090816e910e48e75fc` |
| `ref-en.txt` | https://raw.githubusercontent.com/MiniMax-AI/MiniMax-H3/main/skills/h3-prompt-writing/references/ref-en.txt | 23553 | `1e574f356716ad55612247ffb7bbccbcdb484ad96599d63c7dca1af186b1fab7` |

- 获取日期：2026-09-07
- 上游仓库：https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing
- 快照分支：`main`（未锁定 commit，重新获取须更新本表并复核样例测试）

重新获取时必须同时更新字节数与 SHA256；若两者之一变化，说明上游已改动，
需先复核 `tests/` 中引用官方样例的固定测试是否仍与官方语义一致。
