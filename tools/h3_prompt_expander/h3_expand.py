"""H3 提示词理解+转译：中文口语 -> H3 官方英文格式。

  单跳默认：一次 LLM 调用输出 {intent, pe} 信封 -> 本地 validate -> 有界 repair（≤2次）
  双跳可选：--two-step 先理解后编译（更贵但意图锁定更稳）

LLM 通道统一走仓库根目录 optimizer.py：
  云通道 = openai 兼容 / gemini / responses 三协议；本地通道 = Transformers / GGUF。
配置由调用方给出（优化设置框），CLI 用 --config 传 JSON 文件。

示例：
  python h3_expand.py "雨夜霓虹市场，一个女孩回头笑说跟上我" --duration 5 --config cfg.json
  python h3_expand.py --validate-only envelope.json
  echo "一个男孩在教室写字然后望向窗外" | python h3_expand.py --output json --config cfg.json
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from validate import validate_envelope
from normalize import analyze as pre_analyze
from confirm_card import render_card

# LLM 通道统一走优化后端（三协议云通道 + 本地通道）。本工具不再自带 HTTP 客户端。
_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import optimizer as _opt_backend

DEFAULT_MODEL = os.environ.get("H3_EXPAND_MODEL", "glm-4-flash")
# 强推理推荐：glm-4.6 / glm-4.5；便宜快速：glm-4-flash / glm-4-air


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_prompts():
    intent = _read_text(os.path.join(HERE, "prompts", "system_intent.md"))
    compile_ = _read_text(os.path.join(HERE, "prompts", "system_compile.md"))
    dialect = _read_text(os.path.join(HERE, "references", "h3-dialect.md"))
    return intent, compile_, dialect


def chat(model, messages, cfg, media=None, temperature=None):
    """LLM 调用：摊平 OpenAI 风格 messages，交给 optimizer 的三协议通道。

    cfg 必填——通道、服务商、协议、模型全部由它决定。
    temperature 必须透传：历史 bug 是这里没往下传，HTTP 体里恒为 0.2，
    于是 strict/balanced/creative 三档风格只差一句提示词，采样温度从未变过。
    """
    system_parts, user_parts = [], []
    for m in messages or []:
        content = m.get("content")
        if not isinstance(content, str):
            continue
        (system_parts if m.get("role") == "system" else user_parts).append(content)
    return _opt_backend.generate_json(cfg, "\n\n".join(system_parts),
                                      "\n\n".join(user_parts), media=media,
                                      temperature=temperature)


def build_single_system(intent_sys, compile_sys, dialect):
    # 单跳：把理解+编译压进一次调用，省一轮付费；repair 靠 validate 错误回灌
    return (
        intent_sys
        + "\n\n-----\n\n"
        + compile_sys
        + "\n\n## H3 方言约束（全文背诵，违反即失败）\n"
        + dialect
        + "\n\n## 你的输出\n只输出一个 JSON 信封 {\"intent\": {...}, \"pe\": {...}}，无其它文字。"
    )


# 注意：这两份模板用 str.format 填充，正文里字面量的花括号必须写成 {{ }} 转义，
# 否则 format 会把 {"intent": ...} 里的 "intent" 当成字段名并抛 KeyError（历史 bug，已修）。
REPAIR_TMPL = """上一次编译被确定性校验器打回，请只修 errors，不管 warnings 的风格偏好。
errors：
{errors}

要求：
1. 只改必须改的字段（时长/标签/引号/音画混杂/静止句），不许加戏、不许改用户引号原文。
2. 仍然只输出 JSON 信封 {{"intent": {{...}}, "pe": {{...}}}}。
上次输出：
{last}
"""

REVISE_TMPL = """用户看了意图确认卡后的修订意见（最高优先级，必须照办）：
{revision}

要求：
1. 先按意见改 intent（人物/节拍/对白/时长/候选选择），再重编 pe；用户选了候选 A/B/C 则以该候选为准并清空 candidates。
2. 不许借机加戏；用户引号原文一字不动。
3. 只输出 JSON 信封 {{"intent": {{...}}, "pe": {{...}}}}。
上次输出：
{last}
上次校验 errors（如有）：
{errors}
"""

STYLES = {
    "strict": {"temperature": 0.2,
               "note": "严格模式：只收敛不发挥，不许新增任何视觉细节，节拍预算从紧（单镜至多2节拍）。"},
    "balanced": {"temperature": 0.3,
                 "note": "均衡模式：允许补全机位/光位/材质/声源等执行细节，不许加新人物新道具。"},
    "creative": {"temperature": 0.6,
                 "note": "创意模式：放开手脚——可补新视觉细节、次角、群演、天气与构图层次，"
                         "只守住红线（用户点名的人物/关键道具/引号里的原话/结局走向一字不动）。"},
}

# normalize 高危模式 -> 场景包的兜底映射（关键词零命中时用）
RISK_TO_PACK = {
    "collision": "action_chase",
    "liquid-spread": "liquid_contact",
    "beat-dense": "face_performance",
    "vo-risk": "dialogue",
    "continuity-risk": "montage",
    "combat-melee": "action_chase",
    "crowd-scale": "action_chase",
}


def load_scene_packs():
    """解析 references/scene-packs.md（人类可读即唯一来源）。"""
    packs = []
    try:
        text = _read_text(os.path.join(HERE, "references", "scene-packs.md"))
    except OSError:
        return packs
    for chunk in re.split(r"(?m)^## ", text):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#") or "触发：" not in chunk:
            continue
        pid, _, _body = chunk.partition("\n")
        m = re.search(r"触发：(.+)", chunk)
        triggers = [t.strip() for t in m.group(1).split("、")] if m else []
        packs.append({"id": pid.strip(), "triggers": triggers, "body": chunk})
    return packs


def route_scene(raw, norm, packs):
    best, best_hit = None, 0
    for p in packs:
        hit = sum(1 for t in p["triggers"] if t and t in raw)
        if hit > best_hit:
            best, best_hit = p, hit
    if best is None:
        by_id = {p["id"]: p for p in packs}
        for r in norm.get("risk_topics", []):
            pid = RISK_TO_PACK.get(r.get("id", ""))
            if pid and pid in by_id:
                return by_id[pid]
    return best


def compose_messages(raw_zh, duration, mode, style="balanced", style_note=""):
    """组装发给 LLM 的全部输入（不含 key 可运行，供 --dry-run 检查黑盒）。"""
    intent_sys, compile_sys, dialect = load_prompts()
    norm = pre_analyze(raw_zh)
    packs = load_scene_packs()
    pack = route_scene(raw_zh, norm, packs)
    style_cfg = STYLES.get(style, STYLES["balanced"])

    notes = list(norm.get("notes_for_llm", []))
    if norm.get("suggested_duration") and abs(norm["suggested_duration"] - duration) > 0.01:
        notes.append(f"用户暗示时长约 {norm['suggested_duration']}s，但本次指定 {duration}s：以指定为准，冲突进 risks 说明")
    extra = [style_cfg["note"]]
    if style_note.strip():
        extra.append(f"用户指定的风格/案例参考（吸收其机制，不许照抄人物）：{style_note.strip()}")
    if pack:
        extra.append(f"场景包路由命中 [{pack['id']}]，编译须遵守其预算/机位/音频/必查：\n{pack['body']}")
    user_msg = ("用户中文原文：" + raw_zh
                + f"\n期望 duration：{duration}s\n期望 mode：{mode}"
                + "\n" + "\n".join(extra))
    if notes:
        user_msg += "\n确定性预处理发现（优先采信）：\n- " + "\n- ".join(notes)
    user_msg += "\n请输出 JSON 信封。"

    single_system = build_single_system(intent_sys, compile_sys, dialect)
    if pack:
        single_system += "\n\n## 场景包（路由命中，须遵守）\n" + pack["body"]
    single_system += "\n\n## 风格\n" + style_cfg["note"]
    compile_system = (compile_sys + "\n\n## H3 方言约束\n" + dialect
                      + ("\n\n## 场景包\n" + pack["body"] if pack else "")
                      + "\n\n## 风格\n" + style_cfg["note"])
    return {"intent_sys": intent_sys, "compile_sys": compile_sys, "dialect": dialect,
            "single_system": single_system, "compile_system": compile_system,
            "user_msg": user_msg, "scene_pack": pack["id"] if pack else None,
            "style_note": style_cfg["note"], "norm": norm}


def expand_once(raw_zh, duration, mode, model, temperature, max_repairs,
                two_step=False, style="balanced", style_note="", intent_override=None,
                cfg=None, media=None):
    comp = compose_messages(raw_zh, duration, mode, style, style_note)
    intent_sys, compile_sys = comp["intent_sys"], comp["compile_sys"]
    norm, pack_id = comp["norm"], comp["scene_pack"]
    style_cfg = STYLES.get(style, STYLES["balanced"])
    if temperature is None:
        temperature = style_cfg["temperature"]
    user_msg = comp["user_msg"]

    def _validate_and_repair(obj, temps):
        verdict = validate_envelope(obj)
        repairs = 0
        while not verdict["ok"] and repairs < max_repairs:
            errs = json.dumps(verdict["errors"], ensure_ascii=False)
            last = json.dumps(obj, ensure_ascii=False)
            obj = chat(model,
                       [{"role": "system", "content": intent_sys + "\n\n" + compile_sys},
                        {"role": "user", "content": REPAIR_TMPL.format(errors=errs, last=last[:6000])}],
                       cfg, media=media, temperature=temperature)
            verdict = validate_envelope(obj)
            repairs += 1
        return obj, verdict, repairs

    if intent_override is not None:
        intent = intent_override
        obj = chat(model,
                   [{"role": "system", "content": comp["compile_system"]},
                    {"role": "user", "content": "已确认的意图 IR（不许质疑，直接编译）：\n"
                     + json.dumps(intent, ensure_ascii=False) + "\n" + user_msg}],
                   cfg, media=media, temperature=temperature)
        if "intent" not in obj:
            obj = {"intent": intent, "pe": obj.get("pe", obj)}
        obj, verdict, repairs = _validate_and_repair(obj, temperature)
    elif not two_step:
        obj = chat(model,
                   [{"role": "system", "content": comp["single_system"]},
                    {"role": "user", "content": user_msg}],
                   cfg, media=media, temperature=temperature)
        obj, verdict, repairs = _validate_and_repair(obj, temperature)
    else:
        # 双跳：先理解，后编译（贵一倍，意图锁定更稳）
        o1 = chat(model,
                  [{"role": "system", "content": intent_sys}, {"role": "user", "content": user_msg}],
                  cfg, media=media, temperature=temperature)
        intent = o1.get("intent", o1)
        # 第二跳必须把 user_msg 一起发回：它带着 duration、style_note（随图说明）、
        # 预处理 notes 与场景包。历史 bug 只发了 intent IR —— 图传上去了、
        # 「哪张图是谁」的说明却在第二跳丢了，模型对着一堆无序图猜。
        o2 = chat(model,
                  [{"role": "system", "content": comp["compile_system"]},
                   {"role": "user", "content": "意图 IR（不许质疑，直接编译）：\n"
                    + json.dumps(intent, ensure_ascii=False) + "\n\n" + user_msg}],
                  cfg, media=media, temperature=temperature)
        pe = o2.get("pe", o2)
        obj = {"intent": intent, "pe": pe}
        obj, verdict, repairs = _validate_and_repair(obj, temperature)

    obj["_meta"] = {"repairs": repairs, "model": model, "style": style,
                    "scene_pack": pack_id, "pre_analysis": norm,
                    "validation": verdict, "two_step": two_step}
    return obj


def revise_envelope(obj, revision, model, temperature, max_repairs, cfg=None, media=None):
    """用户确认环的修订重入：--from 上次信封 + --revise 用户意见。"""
    intent_sys, compile_sys, _ = load_prompts()
    if temperature is None:
        temperature = 0.3
    verdict = validate_envelope(obj)
    repairs = 0
    current, current_verdict = obj, verdict
    while repairs < max(1, max_repairs):
        last = json.dumps(current, ensure_ascii=False)
        errs = json.dumps(current_verdict.get("errors", []), ensure_ascii=False)
        current = chat(model,
                       [{"role": "system", "content": intent_sys + "\n\n" + compile_sys},
                        {"role": "user", "content": REVISE_TMPL.format(
                            revision=revision, last=last[:6000], errors=errs)}],
                       cfg, media=media, temperature=temperature)
        current_verdict = validate_envelope(current)
        repairs += 1
        if current_verdict["ok"]:
            break
    current["_meta"] = {"repairs": repairs, "model": model, "revised": True,
                        "validation": current_verdict, "two_step": False,
                        "scene_pack": (obj.get("_meta") or {}).get("scene_pack")}
    return current


def render_h3(obj):
    """按官方格式渲染：字段名逐字、Ref2VA 六段齐全、base 三字段间空一行。

    关键帧模式（I2VA/FL2VA/L2VA）的对齐指令官方要求是**最终提示词的第一行**、
    独立于字段之外；pe 里它存在 description 头部，这里剥离成独立首块。
    对齐块切分规则与 validate._align_blocks 一致（首个空行前为指令块）。
    """
    pe = obj["pe"]
    blocks = []
    if pe.get("mode") == "Ref2VA":
        # 六段固定顺序（官方 ref-en §1）；缺段留空块而非跳过，便于人看出哪段没写
        for key in ("subject_definitions", "summary", "retention_analysis",
                    "detailed_description", "overall_soundscape", "non_diegetic_music"):
            val = pe.get(key)
            if key in ("overall_soundscape", "non_diegetic_music"):
                val = val or "N/A"
            blocks.append(f"{key}: {val or ''}")
    else:
        desc = str(pe.get("integrated_multimodal_description", ""))
        head, body = _align_blocks(desc)
        if _is_align_instruction(head):
            blocks.append(head)                      # 对齐指令独立成首块
            desc = body
        blocks.append(f"integrated_multimodal_description: {desc}")
        blocks.append(f"overall_soundscape: {pe.get('overall_soundscape') or 'N/A'}")
        blocks.append(f"non_diegetic_music: {pe.get('non_diegetic_music') or 'N/A'}")
    return "\n\n".join(blocks)


def _align_blocks(text):
    """取对齐指令块与三字段正文：首个空行前为指令块（与 validate 同口径）。"""
    t = str(text or "").strip()
    head, sep, tail = t.partition("\n\n")
    if not sep:
        m = re.search(r"(?m)^(?:integrated_multimodal_description|detailed_description)\s*:", t)
        if m:
            return t[:m.start()].strip(), t[m.start():]
        return t, ""
    return head.strip(), tail.strip()


def _is_align_instruction(s):
    return bool(re.match(r"^(?:For the target video, at 0\.00|How the reference pictures align)", str(s or "")))


def load_config(path):
    """读取优化设置 JSON（与 optimizer.normalize_config 同结构）；不给则用默认值。"""
    if not path:
        return _opt_backend.normalize_config(None)
    with open(path, encoding="utf-8") as f:
        return _opt_backend.normalize_config(json.load(f))


def main(argv=None):
    ap = argparse.ArgumentParser(description="H3 提示词理解+转译（中文进，官方英文出）")
    ap.add_argument("text", nargs="?", help="中文口语（不给则读 stdin）")
    ap.add_argument("--config", metavar="FILE",
                    help="优化设置 JSON（服务商/协议/API Key/模型/本地模型，见 optimizer.DEFAULT_CONFIG）")
    ap.add_argument("--duration", type=float, default=5, help="目标秒数 4-15（默认 5）")
    ap.add_argument("--mode", default="T2VA", choices=["T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA"])
    ap.add_argument("--model", default=None,
                    help="覆盖配置里的模型名（不填则用 --config 里的 model，最后才回落到内置默认）")
    ap.add_argument("--temperature", type=float, default=None, help="不填则按 --style 取默认")
    ap.add_argument("--style", default="balanced", choices=["strict", "balanced", "creative"],
                    help="strict只收敛 / balanced默认 / creative允许补1个视觉细节")
    ap.add_argument("--style-note", default="",
                    help="风格/案例参考文本（如 T8 案例机制描述），只吸收机制不照抄")
    ap.add_argument("--intent-file", metavar="FILE",
                    help="已确认的意图 IR（JSON，可含 intent/pe），跳过理解直编")
    ap.add_argument("--from", dest="from_file", metavar="FILE",
                    help="确认环修订重入：上次信封 JSON，须配合 --revise")
    ap.add_argument("--revise", default="", help="用户看了确认卡后的修订意见/候选选择")
    ap.add_argument("--confirm", action="store_true",
                    help="先打印意图确认卡 supervise：终端交互确认/修订；管道中则退出码4")
    ap.add_argument("--max-repairs", type=int, default=2)
    ap.add_argument("--two-step", action="store_true", help="先理解后编译（贵一倍）")
    ap.add_argument("--output", default="h3", choices=["h3", "json", "full"])
    ap.add_argument("--validate-only", metavar="FILE", help="只校验已有信封，不调 LLM")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将发给 LLM 的 system/user 消息，不调接口（查黑盒用）")
    args = ap.parse_args(argv)

    if args.validate_only:
        with open(args.validate_only, encoding="utf-8") as f:
            obj = json.load(f)
        res = validate_envelope(obj)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    cfg = load_config(args.config)
    model = args.model or cfg.get("model") or DEFAULT_MODEL

    if args.from_file:
        if not args.revise.strip():
            ap.error("--from 须配合 --revise 修订意见一起用")
        with open(args.from_file, encoding="utf-8") as f:
            obj = json.load(f)
        obj.pop("_meta", None)
        obj = revise_envelope(obj, args.revise.strip(), model,
                              args.temperature, args.max_repairs, cfg=cfg)
        return emit(obj, args)

    text = args.text
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text and not args.intent_file:
        ap.error("请给中文输入（参数或 stdin 管道），或用 --intent-file 跳过理解")
    if not (4 <= args.duration <= 15):
        ap.error("duration 必须在 4-15s")

    if args.dry_run:
        comp = compose_messages(text or "(intent-file 模式)", args.duration,
                                args.mode, args.style, args.style_note)
        comp.pop("norm", None)
        print(json.dumps(comp, ensure_ascii=False, indent=2))
        return 0

    intent_override = None
    if args.intent_file:
        with open(args.intent_file, encoding="utf-8") as f:
            loaded = json.load(f)
        intent_override = loaded.get("intent", loaded)
        if not text:
            text = intent_override.get("logline_zh", "")

    obj = expand_once(text, args.duration, args.mode, model,
                      args.temperature, args.max_repairs, args.two_step,
                      args.style, args.style_note, intent_override, cfg=cfg)

    if args.confirm:
        return confirm_flow(obj, args, cfg, model)
    return emit(obj, args)


def emit(obj, args):
    if args.output == "h3":
        print(render_h3(obj))
        v = obj["_meta"]["validation"]
        if v["warnings"] or not v["ok"]:
            print(f"\n[校验] repairs={obj['_meta']['repairs']} "
                  f"errors={len(v['errors'])} warnings={len(v['warnings'])}", file=sys.stderr)
            for w in v["warnings"][:5]:
                print(f"  WARN {w['code']}: {w['message']}", file=sys.stderr)
            for e in v["errors"][:5]:
                print(f"  ERR {e['code']}: {e['message']}", file=sys.stderr)
    elif args.output == "json":
        print(json.dumps(obj["pe"], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0 if obj["_meta"]["validation"]["ok"] else 3


def confirm_flow(obj, args, cfg, model):
    """意图确认环：打印确认卡；终端可交互修订，非终端退出码4由 agent 接管。"""
    print(render_card(obj))
    if not sys.stdin.isatty():
        print("[待确认] 管道模式不交互：请把卡片示给用户，用 --from/--revise 重入", file=sys.stderr)
        return 4
    for _ in range(2):
        try:
            ans = input("确认请回车/输y；改请直接写意见；选候选请输A/B/C：").strip()
        except EOFError:
            return 4
        if ans.lower() in ("", "y", "yes", "确认", "好", "ok", "confirm"):
            break
        obj = revise_envelope(obj, ans, model, args.temperature, 1, cfg=cfg)
        print(render_card(obj))
    else:
        print("[待确认] 修订轮次用完仍未确认，请检查意图后重跑", file=sys.stderr)
        return 4
    return emit(obj, args)


if __name__ == "__main__":
    raise SystemExit(main())
