"""H3 结构化提示词 -> 官方英文编译 + 校验（M2.5 轻量版）。

对照 docs/ref_rework/official_h3/{SKILL.md,base-en.txt,ref-en.txt}：
- base：integrated_multimodal_description + overall_soundscape(空省略) + non_diegetic_music(空=N/A)
- ref：subject_definitions + summary + retention_analysis + detailed_description + soundscape + music
- Shot：[Shot 1]无时间戳，[Shot N] At MM:SS.mmm 严格递增；单镜单运镜；对白 <d>[语言] 原文</d> + (S1)；双引号只给屏显。
- 模式：默认自动判定（detect_mode），前端可传 mode 手动覆写（T2VA/I2VA/FL2VA/L2VA/Ref2VA），
  覆盖仅切换字段集与校验口径，对齐指令行仍按实际 has_start/has_end 生成（缺帧会如实报错）。

与旧链关系：只增不改。旧 seg {scene_prompt/character_prompt/soundscape/music}
由 migrate_legacy_seg 转新结构；nodes.py 主链继续用旧组装，新前端段卡用本模块
编译预览（POST /h3chain/compile），提交时仍走 save_prompts（prompt_v2 随 seg_fields 透存）。
无第三方依赖，无 ComfyUI 导入，可单测。
"""

import re

try:
    from .resolved_media import resolve_media_plan
except ImportError:  # 允许 pytest/ComfyUI 以顶层模块加载
    from resolved_media import resolve_media_plan

CAMERA_MOVES = ("Zoom In", "Zoom Out", "Push In", "Pull Out", "Pan Left", "Pan Right",
                "Truck Left", "Truck Right", "Tilt Up", "Tilt Down", "Pedestal Up",
                "Pedestal Down", "Arc Shot", "Tracking Shot", "Static Shot",
                "Shake Slightly", "Shake Strongly", "POV", "Roll Clockwise",
                "Roll Counterclockwise")

BASE_FIELDS = ("integrated_multimodal_description", "overall_soundscape", "non_diegetic_music")
REF_FIELDS = ("subject_definitions", "summary", "retention_analysis",
              "detailed_description", "overall_soundscape", "non_diegetic_music")

MAX_PIC, MAX_VID, MAX_AUD = 9, 3, 3  # 官方单段上限（与 nodes.py REF_CAPS 一致）

VALID_MODES = ("T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA")

_SHOT_TAG_RE = re.compile(r"^\s*\[Shot\s+\d+\]\s*")
_LABEL_RE = re.compile(r"<(Subject|Picture|Video|Audio)\s+(\d+)>", re.I)
_FIELD_LINE_RE = re.compile(
    r"^(integrated_multimodal_description|overall_soundscape|non_diegetic_music"
    r"|subject_definitions|summary|retention_analysis|detailed_description)\s*:\s*", re.I)
_KF_RE = re.compile(
    r"For the target video, at ([0-9.]+) seconds into the target video, "
    r"<(Picture|Video|Audio|Subject)\s+(\d+)> \(from \[Shot (\d+)\]\) is fully referenced\."
    # 官方 FL2VA / L2VA 对齐句（base-en.txt 2.1）：整行一条，必须一并识别，
    # 否则 L2VA 句里的 [Shot N] 会被当成真镜头，触发 E_SHOT_ORDER。
    r"|How the reference pictures align with the target video — [^\n]*?"
    r"mark of the target video\.")
_SHOT_RE = re.compile(r"\[Shot\s+(\d+)\](?:\s*At\s+(\d\d):(\d\d)\.(\d\d\d))?")
_D_TAG_RE = re.compile(r"<d>\[(.+?)\](.*?)</d>", re.S)
_SPEAKER_RE = re.compile(r"\(S\d+(?:,S\d+)*\)")

_OFFICIAL_FIELD_RE = re.compile(
    r"integrated_multimodal_description\s*:|overall_soundscape\s*:|detailed_description\s*:")

KEYFRAME_LINE = ("For the target video, at {t:.2f} seconds into the target video, "
                 "{label} (from [Shot {shot}]) is fully referenced.")
# 官方 FL2VA 对齐句（base-en.txt 2.1）：一条句子同时锚住首帧（0.00s）与尾帧（本段时长）。
# 官方用裸词 Picture 1 / Picture 2（不带尖括号）、Shot 不带方括号，逐字照抄。
FL2VA_HEAD = ("How the reference pictures align with the target video — "
              "Picture 1 (from Shot {first_shot}) aligns with the 0.00-second mark "
              "of the target video; Picture 2 (from Shot {last_shot}) aligns with "
              "the {t:.2f}-second mark of the target video.")
# 官方 L2VA 对齐句（base-en.txt 2.1）：只有末帧锚，标签带尖括号、Shot 带方括号。
L2VA_HEAD = ("How the reference pictures align with the target video — "
             "<Picture 1> (from [Shot {shot}]) aligns with the {t:.2f}-second mark "
             "of the target video.")


def _s(v, d=""):
    return v.strip() if isinstance(v, str) else d


def _f(v, d, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return d
    if f < lo or f > hi:
        return d
    return f


def default_shot(index=1):
    return {"index": index, "start_seconds": None, "description": "",
            "camera_move": "", "camera_amplitude": "", "camera_speed": "",
            "dialogues": [], "screen_texts": [], "diegetic_music": "", "ref_usage": []}


def default_prompt():
    """visual = 画面整述（导演台合并框：风格/构图/环境/光照/角色/道具写在一处）。

    有值时优先于下面六个分立字段；六个旧字段保留只为兼容旧存档，界面已不再暴露。"""
    return {"intent_zh": "", "visual": "", "medium_style": "", "composition": "",
            "environment": "", "lighting": "", "characters": "", "props": "",
            "shots": [default_shot(1)], "diegetic_music": "", "soundscape": "",
            "non_diegetic_music": "", "references": [], "subjects": [],
            "task_types": [], "summary_override": "", "retention": [],
            "override_text": None}


def clean_prompt(raw):
    out = default_prompt()
    if not isinstance(raw, dict):
        return out
    for k in ("intent_zh", "visual", "medium_style", "composition", "environment", "lighting",
              "characters", "props", "diegetic_music", "soundscape",
              "non_diegetic_music", "summary_override"):
        out[k] = _s(raw.get(k))[:2000] if isinstance(raw.get(k), str) else ""
    out["override_text"] = raw["override_text"] if isinstance(raw.get("override_text"), str) else None
    shots = raw.get("shots")
    if isinstance(shots, list) and shots:
        cleaned = []
        for sh in shots:
            if not isinstance(sh, dict):
                continue
            c = default_shot(len(cleaned) + 1)
            c["index"] = len(cleaned) + 1
            c["start_seconds"] = _f(sh.get("start_seconds"), None, 0.0, 3600.0)
            for k in ("description", "camera_move", "camera_amplitude", "camera_speed",
                      "diegetic_music"):
                c[k] = _s(sh.get(k))[:2000]
            if c["camera_move"] not in CAMERA_MOVES:
                c["camera_move"] = ""
            if c["camera_amplitude"] not in ("", "small", "large"):
                c["camera_amplitude"] = ""
            if c["camera_speed"] not in ("", "slow", "fast"):
                c["camera_speed"] = ""
            dl = sh.get("dialogues")
            if isinstance(dl, list):
                for d in dl:
                    if isinstance(d, dict) and _s(d.get("text")):
                        c["dialogues"].append({
                            "speaker": _s(d.get("speaker"), "S1")[:16] or "S1",
                            "language": _s(d.get("language"), "Chinese")[:16] or "Chinese",
                            "text": _s(d.get("text"))[:2000],
                            "delivery": _s(d.get("delivery"))[:200],
                            "voiceover": bool(d.get("voiceover"))})
            st = sh.get("screen_texts")
            if isinstance(st, list):
                c["screen_texts"] = [_s(x)[:200] for x in st if _s(x)][:16]
            ru = sh.get("ref_usage")
            if isinstance(ru, list):
                c["ref_usage"] = [_s(x)[:64] for x in ru if _s(x)][:32]
            cleaned.append(c)
        out["shots"] = cleaned or [default_shot(1)]
    if out["shots"]:
        out["shots"][0]["start_seconds"] = None  # 官方：Shot 1 无时间戳，入口强制归零
    refs = raw.get("references")
    if isinstance(refs, list):
        for r in refs:
            if not isinstance(r, dict):
                continue
            label = _s(r.get("label") or r.get("asset_id"))[:64]
            if not label:
                continue
            out["references"].append({"label": label,
                                      "note": _s(r.get("note"))[:200]})
        out["references"] = out["references"][:16]
    subs = raw.get("subjects")
    if isinstance(subs, list):
        for s in subs:
            if not isinstance(s, dict) or not _s(s.get("definition")):
                continue
            out["subjects"].append({"definition": _s(s.get("definition"))[:1000]})
        out["subjects"] = out["subjects"][:16]
    tt = raw.get("task_types")
    if isinstance(tt, list):
        out["task_types"] = [_s(x)[:64] for x in tt if _s(x)][:8]
    rt = raw.get("retention")
    if isinstance(rt, list):
        for r in rt:
            if not isinstance(r, dict) or not _s(r.get("label")):
                continue
            marker = str(r.get("marker") or "fully_preserved")
            if marker not in ("fully_preserved", "partially_preserved",
                              "attribute_transfer", "weak_reference",
                              "fully_copy", "partially_copy", "reference"):
                marker = "fully_preserved"
            out["retention"].append({"label": _s(r.get("label"))[:64], "marker": marker,
                                     "shots": [_s(x)[:16] for x in (r.get("shots") or [])][:16],
                                     "note": _s(r.get("note"))[:300]})
    return out


def migrate_legacy_seg(seg):
    """旧 seg {scene_prompt/character_prompt/soundscape/music/prompt} -> 新 prompt。

    场景 -> environment，角色 -> characters，主提示词 -> shots[0].description，
    环境音/配乐直搬。返回新 prompt dict（未清洗，调用方再 clean_prompt）。
    """
    seg = seg if isinstance(seg, dict) else {}
    p = default_prompt()
    p["environment"] = _s(seg.get("scene_prompt"))
    p["characters"] = _s(seg.get("character_prompt"))
    p["soundscape"] = _s(seg.get("soundscape"))
    p["non_diegetic_music"] = _s(seg.get("music"))
    main = _s(seg.get("prompt") or seg.get("main") or seg.get("text"))
    if main:
        p["shots"] = [dict(default_shot(1), description=main)]
    refs = seg.get("refs")
    if isinstance(refs, list):
        p["references"] = [{"label": _s(r)[:64], "note": ""} for r in refs if _s(r)][:16]
    return p


def detect_mode(prompt, *, has_start=False, has_end=False):
    """模式自动判定：有普通参考/主体/中间引用即 Ref2VA，否则按首尾帧。"""
    refs = prompt.get("references") or []
    subs = prompt.get("subjects") or []
    if refs or subs:
        return "Ref2VA"
    if has_start and has_end:
        return "FL2VA"
    if has_start:
        return "I2VA"
    if has_end:
        return "L2VA"
    return "T2VA"


def keyframe_line(t, label, shot=1):
    return KEYFRAME_LINE.format(t=float(t), label=str(label), shot=int(shot))


def format_ts(seconds):
    t = max(0.0, float(seconds))
    mm = int(t // 60)
    ss = t - mm * 60
    si = int(ss)
    ms = int(round((ss - si) * 1000))
    if ms >= 1000:
        ms -= 1000
        si += 1
    return f"{mm:02d}:{si:02d}.{ms:03d}"


def _sentence(t):
    s = str(t).strip().rstrip("。.；;，, ")
    return s + "." if s else ""


_CAMVERBS = {"Zoom In": "zooms in", "Zoom Out": "zooms out", "Push In": "pushes in",
             "Pull Out": "pulls out", "Pan Left": "pans left", "Pan Right": "pans right",
             "Truck Left": "trucks left", "Truck Right": "trucks right",
             "Tilt Up": "tilts up", "Tilt Down": "tilts down",
             "Pedestal Up": "pedestals up", "Pedestal Down": "pedestals down",
             "Arc Shot": "arcs around the subject", "Tracking Shot": "tracks beside the subject",
             "Static Shot": "holds a static shot", "Shake Slightly": "shakes slightly",
             "Shake Strongly": "shakes strongly", "POV": "adopts a POV perspective",
             "Roll Clockwise": "rolls clockwise", "Roll Counterclockwise": "rolls counterclockwise"}


def _camera_clause(shot):
    verb = _CAMVERBS.get(shot.get("camera_move") or "")
    if not verb:
        return ""
    parts = [verb]
    if shot.get("camera_amplitude"):
        parts.append(f"with {shot['camera_amplitude']} amplitude")
    if shot.get("camera_speed"):
        parts.append(f"at {shot['camera_speed']} speed")
    return " as the camera " + " ".join(parts)


def _dialogue_sentence(d):
    sp = _s(d.get("speaker"), "S1") or "S1"
    lang = _s(d.get("language"), "Chinese") or "Chinese"
    text = _s(d.get("text"))
    if d.get("voiceover"):
        return (f"({sp}) says in an off-screen voiceover <d>[{lang}] {text}</d> "
                f"while their lips remain completely closed.")
    delivery = _s(d.get("delivery"))
    prose = {"chinese": "Mandarin", "mandarin": "Mandarin"}.get(lang.lower(), lang)
    if delivery:
        return f"({sp}) speaks {delivery} in {prose} <d>[{lang}] {text}</d>"
    return f"({sp}) says in {prose} <d>[{lang}] {text}</d>"


def compose_description(prompt, *, instruction_lines=(), duration=None):
    shots = prompt.get("shots") or []
    if not shots:
        return "\n".join(instruction_lines) if instruction_lines else ""
    scene = _s(prompt.get("visual")).strip()
    if not scene:
        scene = " ".join(x for x in (_sentence(v) for v in (
            prompt.get("medium_style"), prompt.get("composition"),
            prompt.get("environment"), prompt.get("lighting"),
            prompt.get("characters"), prompt.get("props"))) if x)
    times = [0.0] + [s.get("start_seconds") for s in shots[1:]]
    if any(t is None for t in times[1:]):
        gap = (duration or float(len(shots))) / max(1, len(shots))
        times = [i * gap for i in range(len(shots))]
    out = list(instruction_lines)
    for i, shot in enumerate(shots):
        desc = _SHOT_TAG_RE.sub("", _s(shot.get("description"))).strip()
        cam = _camera_clause(shot)
        body = _sentence(desc.rstrip("。.；;，, ") + (cam or "")) if desc else ""
        sents = []
        if i == 0:
            first = " ".join(x for x in (scene, body) if x)
            if first:
                sents.append(first)
        elif body:
            sents.append(f"[Shot {i + 1}] At {format_ts(times[i])}, the camera cuts to {body}")
        else:
            sents.append(f"[Shot {i + 1}] At {format_ts(times[i])}.")
        for d in shot.get("dialogues") or []:
            sents.append(_dialogue_sentence(d))
        for t in shot.get("screen_texts") or []:
            t = str(t).strip()
            sents.append(_sentence(t if '"' in t else f'The on-screen text "{t}" is clearly visible.'))
        if shot.get("diegetic_music"):
            sents.append(_sentence(shot["diegetic_music"]))
        if sents:
            if i == 0:
                sents[0] = "[Shot 1] " + sents[0]
            out.append(" ".join(sents))
    text = " ".join(out)
    if len(shots) == 1 and "no cuts" not in text.lower():
        if out and "[Shot 1]" in text:
            out.append("One continuous shot with no cuts.")
        else:
            base = " ".join(x for x in (scene,) if x) or "An establishing shot."
            out = [f"[Shot 1] {_sentence(base)}", "One continuous shot with no cuts."]
    if not out:
        out = ["[Shot 1] An establishing shot.", "One continuous shot with no cuts."]
    elif "[Shot 1]" not in " ".join(out):
        out[0] = "[Shot 1] " + out[0]
    if prompt.get("diegetic_music"):
        out.append(_sentence(prompt["diegetic_music"]))
    return "\n".join(x for x in out if x)


def compose_base(prompt, *, keyframe_lines=()):
    desc = compose_description(prompt, instruction_lines=keyframe_lines)
    fields = {"integrated_multimodal_description": desc}
    if _s(prompt.get("soundscape")):
        fields["overall_soundscape"] = _s(prompt.get("soundscape"))
    fields["non_diegetic_music"] = _s(prompt.get("non_diegetic_music")) or "N/A"
    return fields


def _norm_label(label):
    s = _s(label)
    m = s.strip("<>").split()
    if len(m) == 2 and m[0].lower() in ("subject", "picture", "video", "audio"):
        return f"<{m[0][0].upper()}{m[0][1:].lower()} {m[1]}>"
    return s


def compose_reference(prompt, *, duration=5.0):
    refs = prompt.get("references") or []
    subs = prompt.get("subjects") or []
    subj_lines = [f"<Subject {i + 1}>: {_s(s.get('definition'))}" for i, s in enumerate(subs)]
    for r in refs:
        subj_lines.append(f"{_norm_label(r.get('label'))}: {_s(r.get('note')) or 'reference asset'}")
    tasks = [_s(t) for t in (prompt.get("task_types") or []) if _s(t)]
    prefix = "[" + " + ".join(tasks or ["reference generation"]) + "]"
    summary = _s(prompt.get("summary_override"))
    if not summary:
        bits = [f"{prefix} A {max(1, int(round(float(duration or 5.0))))}-second clip"]
        if subs:
            bits.append("of " + " and ".join(f"<Subject {i + 1}>" for i in range(len(subs))))
        if refs:
            bits.append("based on " + " and ".join(_norm_label(r.get("label")) for r in refs))
        summary = " ".join(bits) + "."
    ret_lines = []
    for r in prompt.get("retention") or []:
        lbl = _norm_label(r.get("label"))
        if lbl:
            ret_lines.append(f"{lbl}: {r.get('marker') or 'fully_preserved'}"
                             + (f", {_s(r.get('note'))}" if _s(r.get("note")) else ""))
    seen = {(_norm_label(r.get("label")) or "").lower() for r in prompt.get("retention") or []}
    for r in refs:
        lbl = _norm_label(r.get("label"))
        if lbl and lbl.lower() not in seen:
            ret_lines.append(f"{lbl}: partially_preserved, layout and mood kept")
    fields = {"subject_definitions": "\n".join(subj_lines),
              "summary": summary,
              "retention_analysis": "\n".join(ret_lines),
              "detailed_description": compose_description(prompt)}
    if _s(prompt.get("soundscape")):
        fields["overall_soundscape"] = _s(prompt.get("soundscape"))
    fields["non_diegetic_music"] = _s(prompt.get("non_diegetic_music")) or "N/A"
    return fields


def compile_segment(prompt_raw, *, seconds=5.0, has_start=False, has_end=False, mode=None):
    """结构化 prompt -> {mode, fields, prompt_text, warnings, diagnostics}。
    mode：None/非法=自动判定；合法五模式之一=手动覆写（字段集与校验按该模式，
    对齐指令行仍按 has_start/has_end 实际生成）。"""
    prompt = clean_prompt(prompt_raw)
    auto = detect_mode(prompt, has_start=has_start, has_end=has_end)
    mode = mode if mode in VALID_MODES else auto
    warnings, diagnostics = [], {}
    if mode != auto:
        warnings.append({"code": "W_MODE_OVERRIDE",
                         "message": f"手动模式 {mode}（自动判定为 {auto}）"})
    kf_lines = []
    n_shots = len(prompt.get("shots") or [])
    if mode in ("I2VA", "L2VA", "FL2VA"):
        dur = float(seconds or 5.0)
        n = max(1, n_shots)
        if mode == "FL2VA" and has_start and has_end:
            # 官方 FL2VA：一条对齐句说完首尾两锚，尾帧锚是本段的 S.SS（不是全片总时长）
            kf_lines.append(FL2VA_HEAD.format(first_shot=1, last_shot=n, t=dur))
        elif mode in ("L2VA", "FL2VA") and has_end:
            kf_lines.append(L2VA_HEAD.format(shot=n, t=dur))
        elif mode in ("I2VA", "FL2VA") and has_start:
            kf_lines.append(keyframe_line(0.0, "<Picture 1>", 1))
    if mode == "Ref2VA":
        fields = compose_reference(prompt, duration=seconds)
    else:
        fields = compose_base(prompt, keyframe_lines=kf_lines)
    override = prompt.get("override_text")
    if isinstance(override, str) and override.strip():
        parsed, order, preamble = parse_override(override)
        if parsed:
            fields = parsed
            text = serialize_fields(parsed, order)
        else:
            fields = {"integrated_multimodal_description": override.strip()}
            text = override.strip()
        if preamble:
            warnings.append({"code": "W_OVERRIDE_PREAMBLE", "message": "覆盖文本首行不是官方字段"})
    else:
        text = serialize_fields(fields)
    miss = [i + 2 for i, s in enumerate((prompt.get("shots") or [])[1:])
            if s.get("start_seconds") is None]
    if miss:
        diagnostics["shot_times_missing"] = miss
    return {"mode": mode, "duration": float(seconds or 5.0), "fields": fields,
            "prompt_text": text, "warnings": warnings, "diagnostics": diagnostics,
            "override": bool(isinstance(override, str) and override.strip())}


def parse_override(text):
    fields, order, preamble, cur = {}, [], [], None
    for line in str(text or "").splitlines():
        m = _FIELD_LINE_RE.match(line)
        if m:
            name = m.group(1).lower()
            if name in fields:
                fields[name] += "\n" + line[m.end():].strip()
            else:
                order.append(name)
                fields[name] = line[m.end():].strip()
                cur = name
            continue
        if cur is not None:
            fields[cur] += "\n" + line.rstrip()
        else:
            preamble.append(line)
    return fields, order, "\n".join(preamble).strip()


def serialize_fields(fields, order=None):
    keys = order or [k for k in
                     ("subject_definitions", "summary", "retention_analysis",
                      "detailed_description", "integrated_multimodal_description",
                      "overall_soundscape", "non_diegetic_music") if k in fields]
    lines = []
    for k in keys:
        s = str(fields.get(k) or "").strip()
        if not s:
            if k == "overall_soundscape":
                continue
            if k == "non_diegetic_music":
                s = "N/A"
            else:
                continue
        lines.append(f"{k}: {s}")
    return "\n".join(lines)


def _used_labels(*texts):
    out = set()
    for t in texts:
        for m in _LABEL_RE.finditer(str(t or "")):
            out.add(f"<{m.group(1).capitalize()} {m.group(2)}>")
    return out


def validate_compiled(compiled):
    """校验 compile_segment 结果。errors 阻提交，warnings 仅提示。"""
    errors, warnings = [], []

    def err(code, message):
        errors.append({"code": code, "message": message})

    def warn(code, message):
        warnings.append({"code": code, "message": message})

    mode = compiled.get("mode")
    duration = float(compiled.get("duration") or 0)
    fields = compiled.get("fields") or {}
    text = compiled.get("prompt_text") or ""
    is_ref = (mode == "Ref2VA")
    expected = REF_FIELDS if is_ref else BASE_FIELDS
    if compiled.get("override"):
        parsed, order, preamble = parse_override(text)
        if preamble:
            err("E_OVERRIDE_PREAMBLE", "覆盖文本首行不是官方字段")
        if not order:
            err("E_OVERRIDE_FIELDS", "覆盖文本缺少官方字段头")
        bad = [k for k in order if k not in expected]
        if bad:
            err("E_FIELD_SET", f"{mode} 不应含字段 {bad}")
    else:
        required = (("subject_definitions", "summary", "retention_analysis",
                     "detailed_description", "non_diegetic_music") if is_ref
                    else ("integrated_multimodal_description", "non_diegetic_music"))
        missing = [k for k in required if k not in fields]
        if missing:
            err("E_FIELD_MISSING", f"{mode} 缺少字段 {missing}")
    if not str(fields.get("non_diegetic_music") or "").strip():
        err("E_NO_MUSIC", "non_diegetic_music 缺失：无配乐写 N/A")
    desc = fields.get("detailed_description" if is_ref else "integrated_multimodal_description") or ""
    if not str(desc).strip():
        err("E_EMPTY_DESC", "描述正文为空")
    sc = fields.get("overall_soundscape") or ""
    if "<d>" in str(sc) or _SPEAKER_RE.search(str(sc)):
        err("E_SOUND_DIALOG", "overall_soundscape 混入对白")
    for i in (compiled.get("diagnostics") or {}).get("shot_times_missing") or []:
        err("E_SHOT_TIME_MISSING", f"Shot {i} 缺换镜时间")
    shots = list(_SHOT_RE.finditer(_KF_RE.sub("", desc)))
    if not shots:
        err("E_NO_SHOT", "缺少 [Shot 1]")
    else:
        nums = [int(m.group(1)) for m in shots]
        if nums != list(range(1, len(nums) + 1)):
            err("E_SHOT_ORDER", f"Shot 须从1连续：{nums}")
        if shots[0].group(2) is not None:
            err("E_SHOT1_TS", "[Shot 1] 不应有时间戳")
        times = [int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0
                 for m in shots if m.group(2) is not None]
        if len(times) >= 2 and times != sorted(times):
            err("E_SHOT_TIME", "Shot 时间戳须递增")
        if times and duration and max(times) > duration + 1e-6:
            err("E_SHOT_OVERFLOW", "Shot 时间戳超出时长")
    if mode in ("I2VA", "FL2VA") and not _KF_RE.search(desc):
        err("E_KF_LINE", "缺少官方首帧指令行")
    for w in compiled.get("warnings") or []:
        warnings.append(w)
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def compile_prompt_document(result_text, *, assets=None, seconds=5.0, mode=None,
                            has_start=False, has_end=False, segment_id=None,
                            allow_official_tokens=False):
    """统一编译编辑层结果文本，并在最后一步解析素材引用。

    编辑层保存的是完整 ``@文件名``；本函数返回的 ``prompt_text`` 才是给 H3
    conditioning 的官方文本。它把字段解析、官方格式校验和素材编号放在一个
    返回值里，供编译预览、新提示词 API 和 nodes.py 执行期共同调用。

    这不是另一个 LLM：输入已经是用户/模型生成的结果文本，函数不发请求、不
    创建节点、不提交队列。错误会阻断调用方，但原始 ``editor_text`` 始终保留，
    便于前端在结果框继续修改后重试。
    """
    editor_text = str(result_text or "").strip()
    warnings, errors = [], []
    if not editor_text:
        return {"ok": False, "editor_text": "", "prompt_text": "", "fields": {},
                "mode": mode if mode in VALID_MODES else "T2VA",
                "media_plan": resolve_media_plan("", assets, segment_id=segment_id),
                "errors": [{"code": "H3_EMPTY_RESULT", "message": "结果提示词为空"}],
                "warnings": []}

    if not allow_official_tokens and _LABEL_RE.search(editor_text):
        errors.append({"code": "H3_DIRECT_TOKEN_FORBIDDEN",
                       "message": "编辑层结果应使用 @完整文件名，不能直接写 <Picture N>/<Video N>/<Audio N>",
                       "segment_id": segment_id})

    parsed, order, preamble = parse_override(editor_text)
    if preamble:
        # 新架构不再显示额外剧本/修复框：自然语言仍可作为一个最小 base 描述，
        # 但保留 warning，让用户知道结果不是完整字段文本。
        if not parsed:
            parsed = {"integrated_multimodal_description": "[Shot 1] " + editor_text,
                      "non_diegetic_music": "N/A"}
            order = ["integrated_multimodal_description", "non_diegetic_music"]
            warnings.append({"code": "W_RESULT_WRAPPED", "message": "自然语言已包装为 H3 基础字段"})
        else:
            errors.append({"code": "H3_RESULT_PREAMBLE", "message": "结果字段前存在无法归属的文本"})

    auto_mode = "Ref2VA" if any(k in parsed for k in REF_FIELDS[:3]) else "T2VA"
    selected_mode = mode if mode in VALID_MODES else auto_mode
    media_plan = resolve_media_plan(editor_text, assets, segment_id=segment_id)
    warnings.extend(media_plan.get("warnings") or [])
    errors.extend(media_plan.get("errors") or [])
    official_text = media_plan.get("prompt_text") or editor_text
    parsed_tokens, token_order, token_preamble = parse_override(official_text)
    if token_preamble and parsed_tokens:
        errors.append({"code": "H3_COMPILED_PREAMBLE", "message": "编译后的结果字段前存在额外文本"})
    fields = parsed_tokens or parsed
    if not fields:
        fields = {"integrated_multimodal_description": "[Shot 1] " + official_text,
                  "non_diegetic_music": "N/A"}
        token_order = list(fields)
    if "non_diegetic_music" not in fields or not str(fields.get("non_diegetic_music") or "").strip():
        fields["non_diegetic_music"] = "N/A"
        if "non_diegetic_music" not in token_order:
            token_order.append("non_diegetic_music")
    canonical_text = serialize_fields(fields, token_order)

    compiled = {"mode": selected_mode, "duration": float(seconds or 5.0),
                "fields": fields, "prompt_text": canonical_text,
                "warnings": warnings, "diagnostics": {}, "override": True}
    verdict = validate_compiled(compiled)
    errors.extend(verdict.get("errors") or [])
    warnings.extend(verdict.get("warnings") or [])
    return {
        "ok": not errors,
        "editor_text": editor_text,
        "prompt_text": canonical_text,
        "fields": fields,
        "mode": selected_mode,
        "media_plan": media_plan,
        "errors": errors,
        "warnings": warnings,
        "diagnostics": compiled.get("diagnostics") or {},
    }
