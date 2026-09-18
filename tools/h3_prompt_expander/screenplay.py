"""剧本扩写：短意图 -> 一大段中文剧本（内容发散器）。

与 h3_expand 的分工（这轮定位修正的核心）：

- **本模块 = 内容**：把一句话扩写成能填满目标时长的中文剧本，自由格式，
  鼓励合理发散（机位/光位/材质/微动作/环境声源）。输出**不是** H3 官方格式。
- **h3_expand / optimizer = 格式**：把剧本压成官方三/六字段（base / Ref2VA）。

时长给的是**范围**而不是定值：模型在 [min, max] 内自行定秒数，
长动作取大值、单点情绪取小值，别用固定秒数把内容量锁死。

成本：outline + 逐段 = N+1 次调用；one_shot（段数少时自动启用）= 1 次。
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import optimizer as _opt_backend  # noqa: E402

SECONDS_MIN, SECONDS_MAX = 4.0, 15.0      # H3 单段硬上限
MAX_SEGMENTS = 12
_FENCE_RE = re.compile(r"^\s*```(?:json|text|markdown)?\s*\n([\s\S]*?)\n```\s*$", re.I)
_SECONDS_RE = re.compile(r"时长\s*[:：]\s*(\d+(?:\.\d+)?)\s*秒")


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_screenplay_prompts():
    return (_read_text(os.path.join(HERE, "prompts", "system_screenplay.md")),
            _read_text(os.path.join(HERE, "prompts", "system_outline.md")))


def _styles():
    from h3_expand import STYLES
    return STYLES


def style_cfg(style: str) -> dict:
    st = _styles()
    return st.get(str(style or "balanced").lower(), st["balanced"])


def clamp_seconds_range(lo, hi):
    """时长范围归一：钳进 [4, 15]，且保证 lo <= hi。"""
    try:
        lo = float(lo)
    except (TypeError, ValueError):
        lo = 5.0
    try:
        hi = float(hi)
    except (TypeError, ValueError):
        hi = lo
    lo = max(SECONDS_MIN, min(SECONDS_MAX, lo))
    hi = max(SECONDS_MIN, min(SECONDS_MAX, hi))
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def clamp_count(n, default=3):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_SEGMENTS, n))


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(str(text or "").strip())
    return m.group(1).strip() if m else str(text or "").strip()


def _gen(cfg, system, user, media=None, max_tokens=None, temperature=None):
    return _opt_backend.generate_text(cfg, system, user, media=media,
                                      max_tokens=max_tokens, temperature=temperature)


def _gen_json(cfg, system, user, media=None, max_tokens=None, temperature=None):
    return _opt_backend.generate_json(cfg, system, user, media=media,
                                      max_tokens=max_tokens, temperature=temperature)


def _as_list(v, limit=8):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()][:limit]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


# ---------------------------------------------------------------- outline

def _split_style_note(note):
    """把「随图说明」从风格/案例参考里拆出来。

    前端 collectSegMedia 把随图说明（`<Picture 1> = 参考素材「xx」`）塞进 style_note
    一起下发，但那句是**事实信息**——哪张图是谁；而 style_note 的既有口径是
    「风格/案例参考（只吸收机制）」。混在一起会让模型把"图1是谁"也当成
    可吸收可不吸收的参考。这里拆开：随图说明单独成节，并强调照图写。
    """
    note = str(note or "").strip()
    if not note:
        return "", ""
    # 到句号为止：贪婪到行尾会把同行的风格/案例参考一起吞进来
    m = re.search(r"随图说明：([^。\n]*)。?", note)
    if not m:
        return "", note
    visual = m.group(1).strip()
    # 切片而非索引：「随图说明：」落在串尾时 m.end() == len(note)，索引会越界
    rest = re.sub(r"[；;。\s]+", " ", (note[:m.start()] + " " + note[m.end():])).strip(" ；;。")
    return visual, rest


def compose_outline_messages(raw_zh, count, sec_range, style="balanced", style_note=""):
    _screenplay_sys, outline_sys = load_screenplay_prompts()
    lo, hi = sec_range
    cfg_s = style_cfg(style)
    extra = [cfg_s["note"]]
    visual, rest = _split_style_note(style_note)
    if visual:
        extra.append(f"随图（你实际看到的图片，规划时照图来）：{visual}")
    if rest:
        extra.append(f"用户指定的风格/案例参考（吸收其机制，不许照抄人物）：{rest}")
    user = ("用户全片中文意图：" + str(raw_zh).strip()
            + f"\n需要切成 {count} 段"
            + f"\n每段时长范围：{lo:g}–{hi:g} 秒（你在范围内自行定秒数）"
            + "\n" + "\n".join(extra)
            + "\n请输出 JSON 大纲。")
    return {"system": outline_sys, "user": user}


def outline_once(raw_zh, count, sec_range, model, cfg, media=None,
                 style="balanced", style_note="", temperature=None, max_repairs=1):
    """总意图 -> 分段大纲 JSON。"""
    count = clamp_count(count)
    sec_range = clamp_seconds_range(*sec_range)
    msg = compose_outline_messages(raw_zh, count, sec_range, style, style_note)
    if temperature is None:
        temperature = style_cfg(style)["temperature"]
    obj = _gen_json(cfg, msg["system"], msg["user"], media=media, temperature=temperature)
    segs = obj.get("segments")
    if not isinstance(segs, list) or not segs:
        raise RuntimeError("大纲返回缺少 segments")
    out = []
    for i, s in enumerate(segs[:MAX_SEGMENTS]):
        s = s if isinstance(s, dict) else {}
        try:
            sec = float(s.get("seconds") or sec_range[0])
        except (TypeError, ValueError):
            sec = sec_range[0]
        out.append({
            "index": i,
            "seconds": max(sec_range[0], min(sec_range[1], sec)),
            "logline_zh": str(s.get("logline_zh") or "").strip(),
            "transition": str(s.get("transition") or "").strip(),
            "beats_hint": _as_list(s.get("beats_hint"), 6),
        })
    return {
        "logline_zh": str(obj.get("logline_zh") or "").strip(),
        "characters": [c for c in (obj.get("characters") or []) if isinstance(c, dict)][:8],
        "continuity": _as_list(obj.get("continuity"), 10),
        "segments": out,
        "_meta": {"model": model, "style": style, "seconds_range": list(sec_range),
                  "count": len(out)},
    }


# ---------------------------------------------------------------- 单段剧本

def compose_script_messages(logline_zh, sec_range, ctx=None, style="balanced", style_note=""):
    screenplay_sys, _outline_sys = load_screenplay_prompts()
    lo, hi = sec_range
    cfg_s = style_cfg(style)
    ctx = ctx if isinstance(ctx, dict) else {}
    parts = []
    if ctx.get("logline_zh"):
        parts.append(f"整片一句话：{ctx['logline_zh']}")
    cont = _as_list(ctx.get("continuity"), 10)
    if cont:
        parts.append("跨段一致性（不许走样）：\n- " + "\n- ".join(cont))
    chars = [c for c in (ctx.get("characters") or []) if isinstance(c, dict)]
    if chars:
        parts.append("人物固定特征：\n"
                     + "\n".join(f"- {c.get('name', '角色')}：{c.get('traits', '')}"
                                 for c in chars))
    if ctx.get("prev"):
        parts.append(f"前情提要（上一段结尾）：{ctx['prev']}")
    if ctx.get("transition") and ctx["transition"] != "开场":
        parts.append(f"与上一段的衔接方式：{ctx['transition']}")
    hints = _as_list(ctx.get("beats_hint"), 6)
    if hints:
        parts.append("节拍提示（可参考，不必照抄）：\n- " + "\n- ".join(hints))
    parts.append("本段要写的内容：" + str(logline_zh).strip())
    parts.append(f"本段时长范围：{lo:g}–{hi:g} 秒（你在这个范围内自行定秒数，"
                 "并在正文开头写「时长：N 秒」）")
    extra = [cfg_s["note"]]
    visual, rest = _split_style_note(style_note)
    if visual:
        extra.append(f"随图（这是你实际看到的图片，务必照图写，不要当成可取舍的参考）：{visual}")
    if rest:
        extra.append(f"风格/案例参考（只吸收机制）：{rest}")
    parts.append("\n".join(extra))
    return {"system": screenplay_sys, "user": "\n\n".join(parts)}


def script_once(logline_zh, sec_range, model, cfg, media=None, ctx=None,
                style="balanced", style_note="", temperature=None):
    """一段 logline -> 一大段中文剧本（纯文本，非官方格式）。"""
    sec_range = clamp_seconds_range(*sec_range)
    msg = compose_script_messages(logline_zh, sec_range, ctx, style, style_note)
    if temperature is None:
        temperature = style_cfg(style)["temperature"]
    text = _strip_fence(_gen(cfg, msg["system"], msg["user"], media=media,
                             temperature=temperature))
    return text, msg


def parse_script_seconds(text, fallback):
    """从剧本正文开头解析「时长：N 秒」，解析不到用大纲给的秒数。"""
    m = _SECONDS_RE.search(str(text or "")[:200])
    if not m:
        return fallback
    try:
        v = float(m.group(1))
    except ValueError:
        return fallback
    return max(SECONDS_MIN, min(SECONDS_MAX, v))


# ---------------------------------------------------------------- 多段

def screenplay_multi(raw_zh, count, sec_range, model, cfg, media=None,
                     style="balanced", style_note="", temperature=None,
                     one_shot=None):
    """总意图 -> N 段剧本。

    one_shot=None 时自动：段数 <= 3 一次出全部（省调用），否则 outline + 逐段
    （内容量更可控，避免一次输出过长被 max_tokens 截断）。
    """
    count = clamp_count(count)
    sec_range = clamp_seconds_range(*sec_range)
    if one_shot is None:
        one_shot = count <= 3
    if one_shot:
        return _multi_one_shot(raw_zh, count, sec_range, model, cfg, media,
                               style, style_note, temperature)
    outline = outline_once(raw_zh, count, sec_range, model, cfg, media,
                           style, style_note, temperature)
    ctx = {"logline_zh": outline.get("logline_zh") or "",
           "continuity": outline.get("continuity") or [],
           "characters": outline.get("characters") or []}
    segs = []
    prev = ""
    for s in outline["segments"]:
        sub = dict(ctx)
        sub["transition"] = s.get("transition") or ""
        sub["beats_hint"] = s.get("beats_hint") or []
        if prev:
            sub["prev"] = prev
        text, _msg = script_once(s.get("logline_zh") or "", sec_range, model, cfg,
                                 media=media, ctx=sub, style=style,
                                 style_note=style_note, temperature=temperature)
        seg = dict(s)
        seg["script"] = text
        seg["seconds"] = parse_script_seconds(text, s.get("seconds"))
        segs.append(seg)
        prev = str(s.get("logline_zh") or "")
    return _pack(outline, segs, model, cfg, style, sec_range, one_shot=False)


def _multi_one_shot(raw_zh, count, sec_range, model, cfg, media,
                    style, style_note, temperature):
    """一次调用同时出大纲与全部剧本（段数少时用，省 N 次调用）。"""
    screenplay_sys, outline_sys = load_screenplay_prompts()
    lo, hi = sec_range
    cfg_s = style_cfg(style)
    extra = [cfg_s["note"]]
    if str(style_note or "").strip():
        extra.append(f"风格/案例参考（只吸收机制）：{style_note.strip()}")
    system = (outline_sys + "\n\n-----\n\n" + screenplay_sys
              + "\n\n## 本次任务\n你需要在**一次回复里**同时给出分段大纲和每一段的完整剧本。")
    user = ("用户全片中文意图：" + str(raw_zh).strip()
            + f"\n切成 {count} 段，每段时长范围 {lo:g}–{hi:g} 秒（每段自行定秒数）"
            + "\n" + "\n".join(extra)
            + "\n只输出 JSON：{\"logline_zh\": \"…\", \"characters\": [{\"name\":\"\",\"traits\":\"\"}],"
              " \"continuity\": [\"…\"], \"segments\": [{\"index\":1, \"seconds\":9,"
              " \"logline_zh\":\"…\", \"transition\":\"开场\", \"script\":\"时长：9 秒\\n\\n镜头一…\"}]}")
    if temperature is None:
        temperature = cfg_s["temperature"]
    obj = _gen_json(cfg, system, user, media=media, temperature=temperature)
    raw_segs = obj.get("segments")
    if not isinstance(raw_segs, list) or not raw_segs:
        raise RuntimeError("一次性扩写返回缺少 segments")
    segs = []
    for i, s in enumerate(raw_segs[:MAX_SEGMENTS]):
        s = s if isinstance(s, dict) else {}
        try:
            sec = float(s.get("seconds") or sec_range[0])
        except (TypeError, ValueError):
            sec = sec_range[0]
        script = _strip_fence(str(s.get("script") or ""))
        segs.append({"index": i,
                     "logline_zh": str(s.get("logline_zh") or "").strip(),
                     "transition": str(s.get("transition") or "").strip(),
                     "beats_hint": _as_list(s.get("beats_hint"), 6),
                     "script": script,
                     "seconds": parse_script_seconds(
                         script, max(sec_range[0], min(sec_range[1], sec)))})
    outline = {"logline_zh": str(obj.get("logline_zh") or "").strip(),
               "characters": [c for c in (obj.get("characters") or []) if isinstance(c, dict)][:8],
               "continuity": _as_list(obj.get("continuity"), 10),
               "segments": segs}
    return _pack(outline, segs, model, cfg, style, sec_range, one_shot=True)


def _pack(outline, segs, model, cfg, style, sec_range, one_shot):
    return {
        "ok": all(str(s.get("script") or "").strip() for s in segs),
        "logline_zh": outline.get("logline_zh") or "",
        "characters": outline.get("characters") or [],
        "continuity": outline.get("continuity") or [],
        "segments": segs,
        "meta": {"count": len(segs), "seconds_range": list(sec_range), "style": style,
                 "model": model, "one_shot": bool(one_shot),
                 "total_seconds": round(sum(float(s.get("seconds") or 0) for s in segs), 2)},
    }


def screenplay_once(raw_zh, sec_range, model, cfg, media=None, ctx=None,
                    style="balanced", style_note="", temperature=None):
    """单段扩写（段卡里的「AI扩写 → 剧本」用）：一句话 -> 一大段剧本。"""
    sec_range = clamp_seconds_range(*sec_range)
    text, _msg = script_once(raw_zh, sec_range, model, cfg, media=media, ctx=ctx,
                             style=style, style_note=style_note, temperature=temperature)
    return {"script": text,
            "seconds": parse_script_seconds(text, sec_range[0]),
            "meta": {"seconds_range": list(sec_range), "style": style, "model": model}}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="剧本扩写：短意图 -> 一大段中文剧本")
    ap.add_argument("text", nargs="?", help="中文意图（不给则读 stdin）")
    ap.add_argument("--config", metavar="FILE", help="优化设置 JSON")
    ap.add_argument("--segments", type=int, default=1, help="段数，默认 1")
    ap.add_argument("--sec-min", type=float, default=6)
    ap.add_argument("--sec-max", type=float, default=10)
    ap.add_argument("--style", default="balanced", choices=["strict", "balanced", "creative"])
    ap.add_argument("--model", default="")
    ap.add_argument("--one-shot", dest="one_shot", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只打印将发送的消息")
    args = ap.parse_args(argv)

    text = args.text
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        ap.error("请给中文输入")
    cfg = (json.load(open(args.config, encoding="utf-8")) if args.config
           else _opt_backend.normalize_config(None))
    model = args.model or cfg.get("model") or ""
    rng = clamp_seconds_range(args.sec_min, args.sec_max)

    if args.dry_run:
        if args.segments <= 1:
            print(json.dumps(compose_script_messages(text, rng, style=args.style),
                             ensure_ascii=False, indent=2))
        else:
            print(json.dumps(compose_outline_messages(text, args.segments, rng, args.style),
                             ensure_ascii=False, indent=2))
        return 0

    if args.segments <= 1:
        res = screenplay_once(text, rng, model, cfg, style=args.style)
        print(res["script"])
        print(f"\n[时长] {res['seconds']}s（范围 {rng[0]:g}–{rng[1]:g}）", file=sys.stderr)
        return 0
    res = screenplay_multi(text, args.segments, rng, model, cfg, style=args.style,
                           one_shot=args.one_shot or None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
