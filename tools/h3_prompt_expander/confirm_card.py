"""意图确认卡：把 intent IR 渲染成用户能点头/摇头的中文卡片。

这是确认环的核心：LLM 不许跳过这一步直接编译。
agent 流程：h3_expand 跑出信封 -> 本卡展示给用户 -> 用户确认/修订/选候选 -> --from/--revise 重入。

用法：
  python confirm_card.py optimized/B4_brief.json
"""
import json
import sys


def render_card(obj):
    intent = obj.get("intent", {})
    inv = intent.get("invariants", {}) or {}
    cons = intent.get("constraints", {}) or {}
    audio = intent.get("audio", {}) or {}
    lines = []
    lines.append("## H3 意图确认卡（先确认，再编译，不猜）")
    lines.append("")
    lines.append(f"一句话：{intent.get('logline_zh', '（缺）')}")
    lines.append(f"模式：{cons.get('mode', 'T2VA')} / 时长：{cons.get('duration', '?')}s / 镜数：{cons.get('shots', '?')}")
    lines.append("")
    lines.append("### 我锁定的关键假设（如错请指正）")
    chars = inv.get("characters", []) or []
    if chars:
        for c in chars:
            tag = c.get("id", "?") + ("（说话）" if c.get("speaks") else "（不说话）")
            lines.append(f"- 人物 {tag}：{c.get('desc_zh', '')}")
    else:
        lines.append("- 人物：无（空镜/纯物）")
    lines.append(f"- 场景：{inv.get('setting_zh', '（缺）')}")
    if inv.get("key_props_zh"):
        lines.append(f"- 关键物（会长出来，宁缺毋滥）：{', '.join(inv['key_props_zh'])}")
    for b in intent.get("beats", []) or []:
        lines.append(f"- 节拍 {b.get('t', '?')}：{b.get('what_zh', '')}")
    dlg = intent.get("dialogue", []) or []
    if dlg:
        for d in dlg:
            vo = "（画外）" if d.get("voiceover") else ""
            lines.append(f"- 对白 {d.get('speaker', '?')}{vo}：{d.get('line', '')}")
    else:
        lines.append("- 对白：无")
    amb = audio.get("ambience_zh", []) or []
    lines.append(f"- 环境音：{'、'.join(amb) if amb else '无（整行省略）'}")
    lines.append(f"- 配乐：{audio.get('music_zh') or '无（N/A）'}")
    if intent.get("risks"):
        lines.append("")
        lines.append("### 风险预判（H3 已知短板）")
        for r in intent["risks"]:
            lines.append(f"- {r}")
    cands = intent.get("candidates", []) or []
    if cands:
        lines.append("")
        lines.append("### 输入含糊，请选一个方向（不猜）")
        for c in cands:
            diff = f" —— 分歧：{c['diff_zh']}" if c.get("diff_zh") else ""
            lines.append(f"- {c.get('id', '?')} {c.get('label_zh', '')}：{c.get('logline_zh', '')}{diff}")
    if intent.get("clarify"):
        lines.append("")
        lines.append("### 需要你确认")
        for i, q in enumerate(intent["clarify"], 1):
            lines.append(f"{i}. {q}")
    lines.append("")
    lines.append("请回复：**确认** / **改**（说明怎么改）" + (" / **选A/B/C**" if cands else ""))
    return "\n".join(lines)


def main():
    if len(sys.argv) != 2:
        print("用法: python confirm_card.py envelope.json", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        obj = json.load(f)
    print(render_card(obj))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
