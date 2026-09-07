"""确定性预处理：LLM 之前先把中文口语的结构化线索抽出来，并标出必翻车模式。

无依赖、无 key、可单测。h3_expand.py 会自动调 analyze() 并把 notes_for_llm 喂给模型。
泛化逻辑在这里：任何输入先过这一层，LLM 只做判断不做找茬，漏检率大降。

用法：
  python normalize.py "雨夜市场，女孩说跟上我，背后霓虹写着营业中"
"""
import json
import re
import sys

ABSTRACT_MAP = {
    "大片感": "删：换成具体运镜+光线（如缓慢推近+暖侧光）",
    "氛围感": "翻译成具体声/光（如雨声+霓虹倒影），不许裸用",
    "电影感": "删：换成景别+运镜+灯光三件套",
    "高级感": "删：换成材质+留白+慢运镜",
    "史诗": "删：5s 小屏撑不起史诗，换成一个具体奇观动作",
    "唯美": "翻译成光线+色彩（如逆光+暖调），不许裸用",
    "震撼": "换成一个可见的尺度对比或动作",
    "燃": "换成具体加速动作+鼓点式剪辑，不许裸用",
    "精彩": "删：换成具体招式/转折（如冲击波掀翻前排），不许裸用",
    "刺激": "删：换成具体节奏（追击/反杀/溃退三段），不许裸用",
    "激烈": "换成可见的强度（烟尘/冲击环/人仰马翻），不许裸用",
}
# 唯一保留的否定：画面干净无字幕无水印
CN_NEG_ALLOW = ("无字幕", "无水印")
CN_NEG_RE = re.compile(r"(不要|别|不能|不可以|没有|禁止|不得|避免|无[\u4e00-\u9fffA-Za-z]{1,4}|没[有]?[\u4e00-\u9fff]{1,4})")
RISK_TOPICS = [
    (re.compile(r"(撞倒|撞上|碰撞|打翻|踢倒|摔碎|追尾)"), "collision", "接触因果：H3 拍不出碰撞瞬间，须改遮挡转场+aftermath"),
    (re.compile(r"(漫开|淹没|四溅|飞溅|泼|洒出|倒出)"), "liquid-spread", "液体体积：写死边界，不许 widening/spreading 裸词"),
    (re.compile(r"(微表情大全|九连拍|表情包|又哭又笑|情绪过山车|眼神杀)"), "beat-dense", "节拍过载：单镜≤3节拍，超了拆镜"),
    (re.compile(r"(独白|旁白|内心戏|心声|OS)"), "vo-risk", "独白：先确认台词原文，再定对口还是画外（画外加 lips closed）"),
    (re.compile(r"(分身|变身|换脸|换装|穿越|闪回)"), "continuity-risk", "连续性高危：必须拆镜+时间戳，身份锚每镜重复"),
    (re.compile(r"(战斗|交战|击退|进攻|围攻|大战|对决|混战)"), "combat-melee", "群战因果：不拍一对多全程，只拍三段（压境/反杀/溃退）；人数不写死数字"),
    (re.compile(r"(数百|数千|上百|上千|千军万马|成千上万|铺天盖地)"), "crowd-scale", "群像规模：改写为潮水般/黑压压等质感词，H3 画不出精确大数字"),
]
READING_HINTS = ("写着", "刻着", "印着", "显示", "招牌", "屏幕", "霓虹", "reading", "sign", "neon")
DIALOGUE_RE = re.compile(r"「([^」]{1,80})」")
QUOTE_RE = re.compile(r'"([^"]{1,60})"|“([^”]{1,60})”')
DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:s|秒)")
ACTION_VERBS = ("走", "跑", "转身", "抬头", "坐下", "站起", "推开", "拿起", "放下", "看向",
                "望向", "笑", "哭", "说", "喊", "跳", "追", "逃", "飞", "倒", "撞", "开", "关",
                "击", "攻", "战", "斗", "杀", "砍", "射", "炸")


def _likely_visible(text, before):
    return any(h in before for h in READING_HINTS)


def analyze(raw):
    raw = (raw or "").strip()
    out = {"dialogue_lines": [], "quotes": [], "abstract_hits": [],
           "negations": [], "risk_topics": [], "suggested_duration": None,
           "long_input": len(raw) > 150, "action_count": 0, "notes_for_llm": []}
    notes = out["notes_for_llm"]

    for m in DIALOGUE_RE.finditer(raw):
        out["dialogue_lines"].append(m.group(1))
    if out["dialogue_lines"]:
        notes.append(f"抽到「」对白 {len(out['dialogue_lines'])} 句，须逐字进 <d>[Chinese]，不许改字：{out['dialogue_lines'][:2]}")

    for m in QUOTE_RE.finditer(raw):
        text = m.group(1) if m.group(1) is not None else m.group(2)
        before = raw[max(0, m.start() - 10):m.start()]
        likely = "visible" if _likely_visible(text, before) else "dialogue-misuse?"
        out["quotes"].append({"text": text, "likely": likely})
        if likely != "visible" and re.search(r"[\u4e00-\u9fff]", text):
            notes.append(f"双引号中文“{text[:15]}”疑似对白误用引号：须移入 <d>，引号只留屏显文字")

    for word, advice in ABSTRACT_MAP.items():
        if word in raw:
            out["abstract_hits"].append({"word": word, "advice": advice})
    if out["abstract_hits"]:
        notes.append("抽象词必须翻译成可见项：" + "；".join(
            f"{h['word']}→{h['advice']}" for h in out["abstract_hits"]))

    for m in CN_NEG_RE.finditer(raw):
        frag = raw[max(0, m.start() - 4):m.end() + 6]
        if any(a in frag for a in CN_NEG_ALLOW):
            continue
        out["negations"].append(frag)
    if out["negations"]:
        notes.append("否定表达须转正向（保留“画面干净无字幕无水印”除外）：" + "；".join(out["negations"][:3]))

    for rx, rid, advice in RISK_TOPICS:
        if rx.search(raw):
            out["risk_topics"].append({"id": rid, "advice": advice})
            notes.append(f"高危模式 [{rid}]：{advice}")

    m = DUR_RE.search(raw)
    if m:
        try:
            out["suggested_duration"] = float(m.group(1))
        except ValueError:
            pass
    if out["long_input"]:
        notes.append(f"输入 {len(raw)} 字疑似企划书：先压一句话 logline 再定节拍，不许全翻")
    out["action_count"] = sum(raw.count(v) for v in ACTION_VERBS)
    if out["action_count"] > 6:
        notes.append(f"动作动词约 {out['action_count']} 个：疑似节拍过载，单镜≤3节拍，超了拆镜")
    return out


def main():
    if len(sys.argv) < 2 and sys.stdin.isatty():
        print("用法: python normalize.py \"中文口语\"", file=sys.stderr)
        return 2
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read().strip()
    print(json.dumps(analyze(raw), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
