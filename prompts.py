"""H3 官方格式契约：帧数换算 / 对齐指令 / 字段解析 / 格式校验。

**结构化提示词（prompt_v2 逐镜表单 + compile_segment 编译预览）已整体下线**，
本模块只保留实跑链路与优化链路都要用的确定性部分：

- 契约常量：主字段名 / BASE_FIELDS / REF_FIELDS / VALID_MODES
- 关键帧对齐指令：KEYFRAME_LINE / FL2VA_HEAD / L2VA_HEAD / alignment_lines / keyframe_line
  `S.SS` 由段长**帧数**换算（frames_to_seconds, 24fps），与 nodes.py 实跑注入同口径 ——
  5 秒段是 124 帧 = 5.17s，不是 5.00。
- 模式判定：detect_mode（有参考素材即 Ref2VA，否则按首尾帧实际有无）
- 官方格式文本解析与校验：parse_override / serialize_fields / validate_compiled
- 优化链路专用：
  · `finalize_optimized` —— 确定性收尾（剥模型自写的对齐指令、[Shot 1] 去时间戳、
    单镜补 no cuts、按帧数重算对齐指令），LLM 算不准的活由后端做
  · `validate_text` —— 结构检查 + 语义检查（引号归属 / 否定 / 裸抽象词 / 静止句 / 密度）

对照 docs/ref_rework/official_h3/{SKILL.md,base-en.txt,ref-en.txt}：
- base：integrated_multimodal_description + overall_soundscape(空省略) + non_diegetic_music(空=N/A)
- ref：subject_definitions + summary + retention_analysis + detailed_description + soundscape + music
- Shot：[Shot 1] 无时间戳，[Shot N] At MM:SS.mmm 严格递增；单镜单运镜；
  对白 `<d>[语言] 原文</d>` + (S1)；双引号只给屏显。

无第三方依赖，无 ComfyUI 导入，可单测。
"""

import re


# 主字段名。优化链路的收尾与校验都要知道「正文写在哪个字段」：
# base 三字段是 integrated_multimodal_description，全参考六段式是 detailed_description。
BASE_MAIN_FIELD = "integrated_multimodal_description"
REF_MAIN_FIELD = "detailed_description"
BASE_FIELDS = (BASE_MAIN_FIELD, "overall_soundscape", "non_diegetic_music")
REF_FIELDS = ("subject_definitions", "summary", "retention_analysis",
              REF_MAIN_FIELD, "overall_soundscape", "non_diegetic_music")


VALID_MODES = ("T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA")

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


FRAME_FPS = 24.0


def frames_to_seconds(frames):
    """段长**帧数** -> 对齐指令里用的秒（两位小数，官方 S.SS）。

    段长的真实单位是吸附到 17k+5 网格的帧数（nodes._snap_seconds），而官方
    FL2VA/L2VA 对齐句的 S.SS 是秒 —— 5 秒段实际是 124 帧 = 5.17s。前端锚定栏
    预览与后端实跑注入必须共用这一个换算，否则同一段两边写出不同的 S.SS
    （曾经就是前端 5.00、后端 5.17）。
    """
    try:
        f = int(frames)
    except (TypeError, ValueError):
        return 5.0
    if f <= 0:
        return 5.0
    return round(f / FRAME_FPS, 2)


def detect_mode(prompt, *, has_start=False, has_end=False):
    """模式自动判定：有普通参考/主体/中间引用即 Ref2VA，否则按首尾帧。

    混合模式：首尾帧锚 + 参考素材同时存在时**仍判 Ref2VA** —— 官方六段式能
    同时承载"关键帧锚点"与"参考素材"（帧锚作为 <Picture 1>/<Picture 2> 进
    subject_definitions），比把两种语义塞进 base 三字段更贴近官方格式。
    """
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


def alignment_lines(mode, seconds, has_start, has_end, n_shots=1):
    """本段的官方对齐指令行（纯函数，无状态）—— 后端实跑与前端预览同口径。

    mode=Ref2VA/T2VA 返回 []：**混合模式（帧锚 + 参考素材）不生成对齐句**，
    帧锚改由 subject_definitions / retention_analysis 里的 <Picture k> 承载
    （官方 Ref2VA 六段式本来就不带对齐指令）。
    其余逐字照抄 tools/h3_prompt_expander/references/h3-dialect.md §1.2。
    """
    dur = float(seconds or 5.0)
    n = max(1, int(n_shots or 1))
    if mode in ("T2VA", "Ref2VA"):
        return []
    if mode == "FL2VA" and has_start and has_end:
        return [FL2VA_HEAD.format(first_shot=1, last_shot=n, t=dur)]
    if mode in ("L2VA", "FL2VA") and has_end:
        return [L2VA_HEAD.format(shot=n, t=dur)]
    if mode in ("I2VA", "FL2VA") and has_start:
        return [keyframe_line(0.0, "<Picture 1>", 1)]
    return []


def keyframe_line(t, label, shot=1):
    return KEYFRAME_LINE.format(t=float(t), label=str(label), shot=int(shot))




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
        # 前置的关键帧对齐指令**合法**：官方格式本来就长成「指令句 + 空行 + 三字段」，
        # 结构化链路与后端实跑注入产出的都是这种形态。只有既非字段头、
        # 也非对齐句的前置内容才判错（那才是模型在正文前写的废话）。
        if preamble and not _ALIGN_HEAD_RE.match(re.sub(r"\s+", " ", preamble).strip()):
            err("E_OVERRIDE_PREAMBLE", "覆盖文本首行既不是官方字段头、也不是关键帧对齐指令")
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
    # 对齐指令在**字段之外**（官方格式：指令句 + 空行 + 三字段），所以必须查整段文本。
    # 查主字段值 desc 会对正确格式永远误报「缺少首帧指令行」。
    if mode in ("I2VA", "FL2VA") and not _KF_RE.search(text):
        err("E_KF_LINE", "缺少官方首帧指令行")
    for w in compiled.get("warnings") or []:
        warnings.append(w)
    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ============================================================================
# 优化链路：确定性收尾 + 语义校验
#
# 结构化提示词下线后，原本「只能靠表单做」的活分两类：
#   A 类（写作）→ 由规则文件 + 本节的语义校验接管
#   B 类（计算）→ 由 finalize_optimized 用已有函数确定性地做，**不许 LLM 写**
#     · 关键帧对齐指令：S.SS 必须由段长**帧数**换算，与 nodes.py 实跑同口径
#     · [Shot 1] 不许带时间戳
# ============================================================================

# 优化链路的 task 可能是 HYBRID（首尾帧锚 + 参考素材的混合模式），官方模式名里没有它 —— 归 Ref2VA：
# 官方六段式本来就能同时承载帧锚与参考素材（见 detect_mode 的说明）。
_MODE_ALIAS = {"HYBRID": "Ref2VA"}


def _norm_mode(mode):
    s = str(mode or "T2VA").upper()
    return _MODE_ALIAS.get(s, s if s in VALID_MODES else "T2VA")


# 对齐指令的宽容识别：模型可能把 em dash 写成 - 或 –，也可能只写半句。
# 只匹配**句首**用于剥离；整句合法性归 validate_compiled 的 E_ALIGN 系。
_ALIGN_HEAD_RE = re.compile(
    r"^(?:For the target video, at \d+(?:\.\d+)? seconds into the target video,"
    r"|How the reference pictures align with the target video\b)", re.I)

_NO_CUTS = "One continuous shot with no cuts."

# ---- 语义检查词表（对齐 h3_prompt_expander/validate.py，只留不依赖意图 IR 的部分）----
_ABSTRACT_WORDS = ("cinematic", "beautiful", "epic", "masterpiece", "8k",
                   "氛围感", "大片感")
_CN_NEG_RE = re.compile(r"(不要|别|没有|无(?!字幕|水印)|禁止|不得)")
_EN_NEG_ALLOW = ("does not look at the camera", "do not look at the camera",
                 "one continuous shot with no cuts", "lips remain completely closed",
                 "lips remain closed", "no further", "no cuts", "without overtaking")
_EN_NEG_RE = re.compile(r"\b(don't|do not|does not|never| no | without |nothing)\b", re.I)
_FREEZE_RE = re.compile(
    r"nothing (about|in) .* changes|stays? (still|where|put)|remains? unchanged"
    r"|什么都不变|保持静止不动", re.I)
_QUOTED_RE = re.compile(r'"([^"]+)"')
# 屏显载体判据：引号前 60 字符内出现这些词，才认为双引号是「画面里的字」而非误用的对白
_SCREEN_CTX_RE = re.compile(r"(reading|read|sign|neon|subtitle|screen|写着|招牌|屏幕|霓虹)")


def _strip_align_block(text):
    """剥掉正文最前面的关键帧对齐指令块（连同其后空行）。

    只识别**句首**，不校验整句 —— 整句合法性由 validate_compiled 判定。
    没识别到就原样返回，绝不吞掉正文。
    """
    t = str(text or "").strip()
    if not t:
        return t
    head, sep, tail = t.partition("\n\n")
    if sep and _ALIGN_HEAD_RE.match(re.sub(r"\s+", " ", head).strip()):
        return tail.strip()
    lines = t.splitlines()
    if lines and _ALIGN_HEAD_RE.match(re.sub(r"\s+", " ", lines[0]).strip()):
        return "\n".join(lines[1:]).strip()
    return t


def finalize_optimized(text, *, mode, seconds=None, frames=None,
                       has_start=False, has_end=False):
    """LLM 产出的官方格式文本 → 确定性收尾，返回 {mode, duration, text, fields, actions, warnings}。

    只做 LLM 算不准或会漏的确定性动作（全部复用已有函数，无新算法）：

    1. 剥掉模型自己写的对齐指令（宽容识别句首）—— 它算不出正确的 S.SS。
    2. `[Shot 1]` 去掉时间戳（官方硬要求；validate_compiled 会以 E_SHOT1_TS 拦下）。
    3. 单镜段补 `One continuous shot with no cuts.`。
    4. 按 mode 重新注入对齐指令，`S.SS` 由段长**帧数**换算（`frames_to_seconds`），
       与 `nodes.py` 实跑注入严格同口径。

    **时间戳递增与超时长不自动修**：那是猜。交给 `validate_text` 报错，让人决定。
    """
    actions, warnings = [], []
    m = _norm_mode(mode)
    dur = frames_to_seconds(frames) if frames else float(seconds or 5.0)
    raw = str(text or "").strip()
    if not raw:
        warnings.append({"code": "W_EMPTY", "message": "优化结果为空，无法收尾"})
        return {"mode": m, "duration": dur, "text": "", "fields": {},
                "actions": actions, "warnings": warnings}

    body = _strip_align_block(raw)
    if body != raw:
        actions.append("strip_alignment")

    fields, order, preamble = parse_override(body)
    main_key = REF_MAIN_FIELD if m == "Ref2VA" else BASE_MAIN_FIELD
    if not fields:
        fields, order = {main_key: body}, [main_key]
        warnings.append({"code": "W_NO_FIELD_HEADER",
                         "message": "正文没有官方字段头（如 integrated_multimodal_description:），"
                                    "已按单字段正文处理"})
    elif preamble:
        # 首个字段头之前的内容既不是字段、也不是对齐句（对齐句上一步已剥掉）→
        # 模型自己写的废话。别让它跟着进模型。
        actions.append("drop_preamble")
    desc = _strip_align_block(str(fields.get(main_key) or "").strip())

    shots = list(_SHOT_RE.finditer(desc))
    if shots and shots[0].group(2) is not None:
        s0 = shots[0]
        tail = desc[s0.end():].lstrip()
        if tail.startswith(","):
            tail = tail[1:].lstrip()
        desc = (desc[:s0.start()] + "[Shot 1] " + tail).strip()
        shots = list(_SHOT_RE.finditer(desc))
        actions.append("drop_shot1_timestamp")

    if len(shots) == 1 and _NO_CUTS.lower() not in desc.lower():
        desc = desc.rstrip() + " " + _NO_CUTS
        actions.append("add_no_cuts")

    fields[main_key] = desc
    out = serialize_fields(fields, order)
    # 对齐指令是**字段之外**的第一块（官方格式：指令句 + 空行 + 三字段）。
    # 拼进主字段值里会被当成正文，校验时还会被误判成 E_OVERRIDE_PREAMBLE。
    kf = alignment_lines(m, dur, has_start, has_end, max(1, len(shots)))
    if kf:
        out = "\n".join(kf) + "\n\n" + out
        actions.append("inject_alignment")
    return {"mode": m, "duration": dur, "text": out,
            "fields": fields, "actions": actions, "warnings": warnings}


def validate_text(text, mode, seconds, *, source_text="", frames=None):
    """官方格式文本校验 = 结构检查（validate_compiled）+ 语义检查。

    语义检查搬自 `tools/h3_prompt_expander/validate.py` 中**只看最终文本、
    不依赖意图 IR** 的那一段：双引号归属、否定堆叠、裸抽象词、全镜静止句、
    节拍密度、长度密度。

    `source_text` 传用户原始输入，用于「引号原文必须逐字保留」——
    validate.py 原版依赖 intent.must_keep_quotes，这里改为当场从原稿抽取，
    于是优化链路不需要任何意图结构化中间态。
    """
    m = _norm_mode(mode)
    dur = frames_to_seconds(frames) if frames else float(seconds or 5.0)
    txt = str(text or "")
    parsed, order, _preamble = parse_override(txt)
    base = validate_compiled({"mode": m, "duration": dur, "fields": parsed,
                              "prompt_text": txt, "warnings": [], "diagnostics": {},
                              "override": True})
    errors, warnings = list(base["errors"]), list(base["warnings"])

    def err(code, msg):
        errors.append({"code": code, "message": msg})

    def warn(code, msg):
        warnings.append({"code": code, "message": msg})

    is_ref = (m == "Ref2VA")
    desc = _strip_align_block(str(parsed.get(REF_MAIN_FIELD if is_ref else BASE_MAIN_FIELD) or ""))
    if not desc:
        return {"ok": not errors, "errors": errors, "warnings": warnings}

    for mm in _D_TAG_RE.finditer(desc):
        if not mm.group(1).strip():
            err("E_D_LANG", "发现 <d> 缺语言标签，应为 <d>[Chinese] ...</d>")
        if not mm.group(2).strip():
            err("E_D_EMPTY", "发现空 <d></d>，逐字台词丢失")

    for mm in _QUOTED_RE.finditer(desc):
        q = mm.group(1)
        if not re.search(r"[\u4e00-\u9fff]", q):
            continue
        if not _SCREEN_CTX_RE.search(desc[max(0, mm.start() - 60):mm.start()]):
            warn("W_QUOTE_ZH",
                 f"中文被双引号包裹 {q[:20]!r}…：疑似对白误用引号，会被烧成画面字幕；"
                 "对白请改 <d>[Chinese] …</d>")
    if _SPEAKER_RE.search(desc) and not _D_TAG_RE.search(desc) \
            and re.search(r"[\"“”].{2,}[\"“”]", desc):
        warn("W_DIALOG_QUOTED",
             "检测到说话人 ID 但台词疑似写在引号里：对白进双引号会被烧成字幕，请移入 <d>")

    if re.search(r"</d>\s*[^.]*?(swallow|吞咽|closes (his|her) mouth|闭嘴)", desc, re.I):
        warn("W_MOUTH_CONFLICT", "台词后紧跟吞咽/闭嘴类动作：发声跨度内的口部动作请移出")

    low = desc.lower()
    for w in _ABSTRACT_WORDS:
        if w in low:
            warn("W_ABSTRACT", f"抽象词 {w!r} 裸用无效，请翻译成可见的光 / 镜 / 动作")
            break
    if _CN_NEG_RE.search(desc):
        warn("W_NEG_CN", "中文否定（不要 / 别 / 没有 / 无…）：实测会反向生成，请转正向描述")
    tmp = desc
    for allow in _EN_NEG_ALLOW:
        tmp = tmp.replace(allow, " ")
    if _EN_NEG_RE.search(tmp):
        warn("W_NEG_EN", "英文含否定（don't / no / without / nothing）：易反向生成，请转正向描述")
    if _FREEZE_RE.search(desc):
        err("E_FREEZE", "含 nothing changes / 保持静止不动类全镜静止句：会外溢冻住整镜，"
                        "停顿请交给剪辑（Shot A 收动作停止，Shot B 从反应开始）")

    words_en = len(re.findall(r"[A-Za-z']+", desc))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", desc))
    words = words_en + int(cjk * 0.6)
    if words:
        if is_ref:
            scale, (lo, hi), code, label = dur / 10.0, (200, 700), "W_REF_LENGTH", "detailed_description"
        else:
            scale, (lo, hi), code, label = dur / 5.0, (20, 260), "W_LENGTH", "description"
        if not (lo * scale <= words <= hi * scale):
            detail = f"英文 {words_en} 词" + (f" + 中文 {cjk} 字" if cjk else "")
            warn(code, f"{label} 约 {words} 词（{detail}），建议 {dur:g}s 落在 "
                       f"{int(lo * scale)}-{int(hi * scale)} 词；过短太空、过长必 rush")

    parts = _SHOT_RE.split(desc)
    for i in range(1, len(parts), 5):
        body = parts[i + 4] if i + 4 < len(parts) else ""
        n_sent = len([s for s in re.split(r"[.!?。！？]+", body) if s.strip()])
        if n_sent > 4:
            warn("W_BEAT_DENSE",
                 f"Shot {parts[i]} 约 {n_sent} 句，单镜 >4 句易被抹平成平均运动，建议拆镜")

    for q in re.findall(r"[\"“]([^\"”]{1,60})[\"”]", str(source_text or "")):
        q = q.strip()
        if q and q not in txt:
            warn("W_QUOTE_LOST",
                 f"用户原文的引号内容未出现在产出里：{q[:30]!r}…"
                 "（对白应进 <d>，屏显应保留原文）")

    return {"ok": not errors, "errors": errors, "warnings": warnings}
