"""H3 PE 确定性校验器：无 LLM、无 GPU，可跑 CI。

用法：
  python validate.py envelope.json
  python -c "from validate import validate_envelope; print(validate_envelope(obj))"

校验分两级：
  errors   -> 必修，否则 H3 大概率胡编/念提示词/烧字幕，必须触发 repair/硬失败
  warnings -> 风险提示，不阻塞（节拍密度、长度、风格裸词等）
"""
import json
import re
import sys

VALID_MODES = ("T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA")
SHOT_RE = re.compile(r"\[Shot\s+(\d+)\](?:\s*At\s+(\d\d):(\d\d)\.(\d\d\d))?")
D_TAG_RE = re.compile(r"<d>\[(.+?)\](.*?)</d>", re.S)
SPEAKER_RE = re.compile(r"\(S\d+(?:,S\d+)*\)")
QUOTED_RE = re.compile(r'"([^"]+)"')

# ---- 官方结构契约（对齐 MiniMax-AI/MiniMax-H3/skills/h3-prompt-writing） ----
BASE_FIELD = "integrated_multimodal_description"
REF_MAIN_FIELD = "detailed_description"
REF_SECTIONS = ("subject_definitions", "summary", "retention_analysis",
                "detailed_description", "overall_soundscape", "non_diegetic_music")
REF_MARK_VIS = ("fully_preserved", "partially_preserved", "attribute_transfer", "weak_reference")
REF_MARK_AUD = ("fully_copy", "partially_copy", "reference", "weak_reference")
# 官方对齐指令整句；N = 实际末镜号，S.SS = 有效时长（恰好两位小数），编译时必须替换
# 在 _norm_align 归一化后的文本上匹配
ALIGN_I2VA = re.compile(
    r"^For the target video, at 0\.00 seconds into the target video,\s*"
    r"<Picture 1> \(from \[Shot 1\]\) is fully referenced\.$")
ALIGN_FL2VA = re.compile(
    r"^How the reference pictures align with the target video - "
    r"Picture 1 \(from Shot 1\) aligns with the 0\.00-second mark of the target video;\s*"
    r"Picture 2 \(from Shot (?P<n>\d+)\) aligns with the (?P<t>\d+\.\d{2})-second mark of the target video\.$")
ALIGN_L2VA = re.compile(
    r"^How the reference pictures align with the target video - "
    r"<Picture 1> \(from \[Shot (?P<n>\d+)\]\) aligns with the (?P<t>\d+\.\d{2})-second mark of the target video\.$")
ALIGN_BY_MODE = {"I2VA": ("I2VA", ALIGN_I2VA), "FL2VA": ("FL2VA", ALIGN_FL2VA),
                 "L2VA": ("L2VA", ALIGN_L2VA)}
REF_TASK_TYPES = ("keyframe completion", "reference generation", "video editing",
                  "video continuation", "audio reuse", "audio reference")


def _align_blocks(text):
    """取对齐指令块：首个空行之前的内容（FL2VA/L2VA 的模板句本身可换行）。
    返回 (指令块, 三字段正文)。无空行时按行首 'integrated_…' 或 'detailed_…' 切分。"""
    t = str(text or "").strip()
    head, sep, tail = t.partition("\n\n")
    if not sep:
        # 无空行：找三字段中第一个字段名的行首位置作为正文起点
        m = re.search(r"(?m)^(?:integrated_multimodal_description|detailed_description)\s*:", t)
        if m:
            return t[:m.start()].strip(), t[m.start():]
        return t, ""
    return head.strip(), tail.strip()


def _norm_align(s):
    """对齐指令比较用归一化：压空白、统破折号。"""
    s = re.sub(r"[—–-]", "-", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()

# 裸抽象词：单独出现≈无效，必须配具体光/镜/动作；这里只告警不报错
ABSTRACT_WORDS = ["cinematic", "beautiful", "epic", "masterpiece", "8k", "ultra cinematic", "氛围感", "大片感"]
# 中文否定：唯一保留“画面干净无字幕无水印”
CN_NEG_RE = re.compile(r"(不要|别|没有|无(?!字幕|水印)|禁止|不得)")
# 英文否定白名单之外的否定都告警
EN_NEG_ALLOW = [
    "does not look at the camera",
    "do not look at the camera",
    "one continuous shot with no cuts",
    "lips remain completely closed",
    "lips remain closed",
    "no further",
    "no cuts",
    "without overtaking",
]
EN_NEG_RE = re.compile(r"\b(don't|do not|does not|never| no | without |nothing)\b", re.I)
FREEZE_RE = re.compile(r"nothing (about|in) .* changes|stays? (still|where|put)|remains? unchanged|什么都不变|保持静止不动", re.I)


def _ts_to_sec(mm, ss, ms):
    return int(mm) * 60 + int(ss) + int(ms) / 1000.0


def validate_envelope(obj):
    errors, warnings = [], []

    def err(code, msg):
        errors.append({"code": code, "message": msg})

    def warn(code, msg):
        warnings.append({"code": code, "message": msg})

    if not isinstance(obj, dict):
        return {"ok": False, "errors": [{"code": "E_ENVELOPE", "message": "顶层必须是 JSON 对象"}], "warnings": []}
    intent = obj.get("intent")
    pe = obj.get("pe")
    if not isinstance(intent, dict):
        err("E_NO_INTENT", "缺少 intent 对象（理解跳被跳过了？）")
    if not isinstance(pe, dict):
        err("E_NO_PE", "缺少 pe 对象（编译跳被跳过了？）")
        return {"ok": False, "errors": errors, "warnings": warnings}

    # ---- pe 基础 ----
    mode = pe.get("mode")
    duration = pe.get("duration")
    is_ref = (mode == "Ref2VA")
    if is_ref:
        subj = pe.get("subject_definitions") or ""
        summ = pe.get("summary") or ""
        ret = pe.get("retention_analysis") or ""
        desc = pe.get("detailed_description") or ""
        if not subj.strip():
            err("E_REF_SUBJ", "Ref2VA 缺少 subject_definitions（每个 <Picture/Video/Audio N> 必须先定义职责）")
        if not summ.strip():
            err("E_REF_SUMM", "Ref2VA 缺少 summary（须以 [reference generation] 等任务前缀开头）")
        if not ret.strip():
            err("E_REF_RET", "Ref2VA 缺少 retention_analysis（每标签一行保留标记，否则身份必漂移）")
        if not desc.strip():
            err("E_REF_DESC", "Ref2VA 缺少 detailed_description")
        # 标签一致性：detailed/summary/retention 里用的标签必须在 subject_definitions 里定义过
        def _norm(t):
            return re.sub(r"\s+", " ", t.strip().upper())
        defined = set(_norm(m.group(0)) for m in re.finditer(
            r"<(Subject|Picture|Video|Audio)\s+\d+>", subj, re.I))
        used = set(_norm(m.group(0)) for m in re.finditer(
            r"<(Subject|Picture|Video|Audio)\s+\d+>", desc + " " + summ + " " + ret, re.I))
        missing = used - defined
        if missing:
            err("E_REF_LABEL", f"引用了未定义的标签：{sorted(missing)}（标签必须全文一致）")
        if ret.strip() and not re.search(
                r"fully_preserved|partially_preserved|attribute_transfer|weak_reference|fully_copy|partially_copy",
                ret, re.I):
            warn("W_REF_MARKER", "retention_analysis 未见保留标记（fully_preserved 等）：H3 不知道脸和衣服谁该留谁可变")
        # 六段齐全 + 顺序（官方 §1：顺序固定）
        missing_sec = [s for s in REF_SECTIONS if not str(pe.get(s) or "").strip()]
        if missing_sec:
            err("E_REF_SECTION", f"Ref2VA 六段不全，缺：{missing_sec}（顺序固定：{list(REF_SECTIONS)}）")
        # summary 必须以方括号任务类型前缀开头
        if summ.strip() and not re.match(r"^\[\s*[a-z][a-z ]*(?:\s*\+\s*[a-z][a-z ]*)*\s*\]", summ.strip()):
            err("E_REF_SUMMARY_PREFIX",
                "summary 须以方括号任务类型前缀开头，如 [reference generation] / [video editing + audio reuse]")
        elif summ.strip():
            inner = re.match(r"^\[([^\]]+)\]", summ.strip()).group(1)
            bad = [t.strip() for t in inner.split("+") if t.strip() not in REF_TASK_TYPES]
            if bad:
                warn("W_REF_TASK_TYPE", f"summary 任务类型非常规：{bad}（官方表：{list(REF_TASK_TYPES)}）")
        # retention_analysis 不许出现 (Sx)；音频行须用音频标记
        if ret.strip() and SPEAKER_RE.search(ret):
            err("E_REF_SPEAKER_IN_RET", "retention_analysis 里不许写 (Sx) 说话人 ID（官方 §5.4）")
        aud_lines = [l for l in ret.splitlines() if l.strip().startswith("<Audio")]
        if aud_lines and not any(any(m in l for m in REF_MARK_AUD) for l in aud_lines):
            warn("W_REF_AUDIO_MARK", "retention_analysis 的 <Audio N> 行未见音频标记（fully_copy / partially_copy / reference / weak_reference）")
        # detailed_description：风格须在 [Shot 1] 之前先立
        if desc.strip() and desc.lstrip().startswith("[Shot 1]"):
            warn("W_REF_STYLE_AFTER_SHOT", "Ref2VA 风格应在 [Shot 1] 之前用一到两句先立，当前直接从 [Shot 1] 开始")
        # 同 W_LENGTH：只数英文词会对中文主体永久误报，按 1 中文字 ≈ 0.6 英文词折算。
        words_ref_en = len(re.findall(r"[A-Za-z']+", desc))
        cjk_ref = len(re.findall(r"[\u4e00-\u9fff]", desc))
        words_ref = words_ref_en + int(cjk_ref * 0.6)
        if words_ref and not (200 <= words_ref <= 700):
            detail = f"英文 {words_ref_en} 词" + (f" + 中文 {cjk_ref} 字" if cjk_ref else "")
            warn("W_REF_LENGTH", f"detailed_description 约 {words_ref} 词（{detail}），"
                                 f"官方生成任务参考区间 350-500 词（中文约 580-830 字）")
        # 标签一致性补充：subject_definitions 里 <Audio N> 绑定时须复用 (Sx)
        for m in re.finditer(r"<Audio\s+\d+>[^\n]*<Subject\s+\d+>", subj, re.I):
            line = m.group(0)
            if not SPEAKER_RE.search(line):
                warn("W_REF_AUDIO_SX", "subject_definitions 里 <Audio N> 绑定到 <Subject N> 时应复用其 (Sx)，如 <Audio 1> is the voice-timbre reference for <Subject 1> (S1).")
                break
    else:
        desc = pe.get("integrated_multimodal_description") or ""
        # 对齐指令：I2VA/FL2VA/L2VA 必须为首块且整句正确；T2VA 不得有
        if mode in ALIGN_BY_MODE:
            head, body = _align_blocks(desc)
            am = ALIGN_BY_MODE[mode][1].match(_norm_align(head))
            if not am:
                err("E_ALIGN", f"{mode} 缺少正确的关键帧对齐指令：必须为最终提示词第一块（空行前），"
                               f"整句照抄官方模板、并把 N/S.SS 替换成真实值（当前首块：{head[:70]!r}）")
            elif not body:
                warn("W_ALIGN_BLANK", "对齐指令与三字段之间应有空行（官方 §2.1）")
            else:
                # S.SS 必须等于有效时长（恰好两位小数）；N 必须等于实际末镜号
                gd = am.groupdict()
                if gd.get("t") is not None and isinstance(duration, (int, float)):
                    if abs(float(gd["t"]) - float(duration)) > 1e-9:
                        err("E_ALIGN_TIME", f"对齐指令里的时长 {gd['t']}s 与 duration {duration}s 不一致"
                                            "（官方：S.SS 为有效时长，恰好两位小数）")
                if gd.get("n") is not None:
                    shots_in_body = re.findall(r"\[Shot\s+(\d+)\]", body)
                    if shots_in_body and int(gd["n"]) != max(int(x) for x in shots_in_body):
                        err("E_ALIGN_SHOT", f"对齐指令里的末镜号 Shot {gd['n']} 与实际最后一镜 "
                                            f"Shot {max(int(x) for x in shots_in_body)} 不一致")
        elif mode == "T2VA" and re.match(
                r"^(For the target video, at 0\.00|How the reference pictures align)",
                _norm_align(desc)):
            err("E_ALIGN_T2VA", "T2VA 不应有关键帧对齐指令（官方 §2.1：无图对齐指令，直接从三字段开始）")
        # 对齐指令块不参与镜头/长度统计（其 [Shot N] 是模板占位，不是真实镜头）
        desc = _align_blocks(desc)[1] or desc
    soundscape = pe.get("overall_soundscape")
    music = pe.get("non_diegetic_music")
    if mode not in VALID_MODES:
        err("E_MODE", f"mode 非法：{mode!r}，只许 {VALID_MODES}")
    if not isinstance(duration, (int, float)) or not (4 <= duration <= 15):
        err("E_DURATION", f"duration 必须在 4-15s，当前 {duration!r}")
    if not is_ref and not desc.strip():
        err("E_EMPTY_DESC", "integrated_multimodal_description 为空")
    if music is None or (isinstance(music, str) and not music.strip()):
        err("E_NO_MUSIC", "non_diegetic_music 缺失：无配乐必须写 N/A，不能省略")
    elif isinstance(music, str) and music.strip() not in ("N/A", "n/a"):
        n_sent = len([s for s in re.split(r"[.!?。！？]+", music) if s.strip()])
        if n_sent > 3:
            warn("W_MUSIC_LONG", f"配乐 {n_sent} 句，官方建议 1-3 句")

    # 长度：密度按**时长缩放**（官方 5s 配 60-120 词 / 中文约 100-200 字，
    # 约每 3 秒一个切点）。只数英文词会对全中文描述永久误报，故按
    # 1 中文字 ≈ 0.6 英文词折算后合并计数。
    # 阈值与文案都必须跟着 duration 走：写死 5s 口径会把 10s 段误报成"过长"。
    words_en = len(re.findall(r"[A-Za-z']+", desc))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", desc))
    words = words_en + int(cjk * 0.6)
    sec = duration if isinstance(duration, (int, float)) and duration > 0 else 5.0
    scale = sec / 5.0
    if words and not (20 * scale <= words <= 260 * scale):
        detail = f"英文 {words_en} 词" + (f" + 中文 {cjk} 字" if cjk else "")
        warn("W_LENGTH", f"description 约 {words} 词（{detail}），建议 {sec:g}s 配 "
                         f"{int(60 * scale)}-{int(120 * scale)} 词"
                         f"（中文约 {int(100 * scale)}-{int(200 * scale)} 字，"
                         "过短太空、过长必 rush）")

    # ---- 镜头时间戳 ----
    shots = list(SHOT_RE.finditer(desc))
    if not shots:
        err("E_NO_SHOT", "缺少 [Shot 1] 镜头记法")
    else:
        nums = [int(m.group(1)) for m in shots]
        if nums[0] != 1 or nums != sorted(nums):
            msg = f"Shot 编号必须从 1 递增，当前 {nums}"
            # desc 已被 _align_blocks 剥掉对齐指令块，空行随之消失；
            # 判断"多镜误用空行"必须回原始字段看，否则永远检测不到。
            raw = pe.get(REF_MAIN_FIELD if is_ref else BASE_FIELD) or ""
            if "\n\n" in str(raw):
                msg += ("；description 内出现了空行，多镜之间只能用单个换行——"
                        "空行会被当成关键帧对齐指令块的分隔符，导致后续镜头被整段截掉")
            err("E_SHOT_ORDER", msg)
        times = []
        for m in shots:
            if m.group(2) is not None:
                times.append(_ts_to_sec(m.group(2), m.group(3), m.group(4)))
        if times != sorted(times):
            err("E_SHOT_TIME", f"Shot 时间戳必须递增，当前 {times}")
        if isinstance(duration, (int, float)) and times and max(times) > duration + 1e-6:
            err("E_SHOT_OVERFLOW", f"Shot 时间戳 {max(times)}s 超出 duration {duration}s")
        # 单镜节拍密度：每镜按句号切分，>4 句告警
        parts = SHOT_RE.split(desc)
        for i in range(1, len(parts), 5):
            body = parts[i + 4] if i + 4 < len(parts) else ""
            n_sent = len([s for s in re.split(r"[.!?。！？]+", body) if s.strip()])
            if n_sent > 4:
                warn("W_BEAT_DENSE", f"Shot {parts[i]} 约 {n_sent} 句，单镜>4 句易被抹平成平均运动，建议拆镜")

    # ---- 对白 ----
    d_tags = list(D_TAG_RE.finditer(desc))
    speakers = set(SPEAKER_RE.findall(desc))
    for m in d_tags:
        lang, line = m.group(1).strip(), m.group(2)
        if not lang:
            err("E_D_LANG", "发现 <d> 缺语言标签，应为 <d>[Chinese] ...</d>")
        if not line.strip():
            err("E_D_EMPTY", "发现空 <d></d>，逐字台词丢失")
    # 有说话人 ID 却无 <d>：台词可能写散了 -> 火星语风险
    if speakers and not d_tags and re.search(r"[\"“”].{2,}[\"“”]", desc):
        warn("W_DIALOG_QUOTED", "检测到说话人 ID 但台词疑似写在引号里：对白进双引号会被烧成字幕，请移入 <d> 标签")
    # 台词后紧跟冲突口部动作
    if re.search(r"</d>\s*[^.]*?(swallow|吞咽|closes (his|her) mouth|闭嘴)", desc, re.I):
        warn("W_MOUTH_CONFLICT", "台词后紧跟吞咽/闭嘴类动作：H3 会把整镜当讲话铺满嘴型，发声跨度内的口部动作请移出")

    # ---- 双引号只给屏显 ----
    for m in QUOTED_RE.finditer(desc):
        q = m.group(1)
        if re.search(r"[\u4e00-\u9fff]", q):
            before = desc[max(0, m.start() - 60):m.start()].lower()
            if not re.search(r"(reading|read|sign|neon|subtitle|screen|写着|招牌)", before):
                warn("W_QUOTE_ZH", f"中文被双引号包裹 {q[:20]!r}…：疑似对白误用引号，会烧成屏显字幕，请改 <d>[Chinese]")

    # ---- 禁则 ----
    low = desc.lower()
    for w in ABSTRACT_WORDS:
        if w.lower() in low:
            warn("W_ABSTRACT", f"抽象词 {w!r} 裸用无效，请翻译成可见的光/镜/动作")
            break
    cn_text = intent.get("constraints", {}) if isinstance(intent, dict) else {}
    _ = cn_text  # intent 中文不校验否定（口语允许），只校验英文编译结果
    tmp = desc
    for allow in EN_NEG_ALLOW:
        tmp = tmp.replace(allow, " ")
    if EN_NEG_RE.search(tmp):
        warn("W_NEG_EN", "英文编译含否定（don't/no/without/nothing）：H3/Runway 实测否定易反向生成，请转正向描述")
    if FREEZE_RE.search(desc):
        err("E_FREEZE", "含 nothing changes / 保持静止不动类全镜静止句：会外溢冻住整镜，停顿请交给剪辑（Shot A 收动作停止，Shot B 从反应开始）")

    # ---- 音画分离 ----
    if soundscape:
        if "<d>" in soundscape or SPEAKER_RE.search(soundscape):
            err("E_SOUND_DIALOG", "overall_soundscape 里混入对白：对白只许在 description 事件行，soundscape 只放环境+动作+非语言人声")
        elif re.search(r"[\u4e00-\u9fff]", soundscape) and re.search(
                r"(says|say|speaks?|talks?|shouts?|whispers?|sings?|说|台词|对白)", soundscape, re.I):
            err("E_SOUND_DIALOG", "overall_soundscape 里混入疑似对白（含中文+说话动词）：对白只许在 description 事件行")
    if soundscape and isinstance(music, str) and music.strip() not in ("N/A", ""):
        # 两字段重复同一配乐描述
        s_low = soundscape.lower()
        if any(k in s_low for k in ("piano score", "background music", "配乐", "bgm")):
            warn("W_AUDIO_DUP", "soundscape 疑似混入配乐描述：配乐只许在 non_diegetic_music，两字段不要重复")

    # ---- 引号原文保留 ----
    if isinstance(intent, dict):
        inv = intent.get("invariants", {}) or {}
        for q in inv.get("must_keep_quotes", []) or []:
            if q and q not in desc and q not in (soundscape or "") and q not in (music or ""):
                err("E_QUOTE_LOST", f"用户引号原文丢失：{q[:30]!r}… 必须逐字保留（含标点）")
        if intent.get("candidates"):
            warn("W_UNCONFIRMED", "含未确认候选方向：禁止直接投产，须用户三选一后用 --from/--revise 重入")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def main():
    if len(sys.argv) != 2:
        print("用法: python validate.py envelope.json", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        obj = json.load(f)
    res = validate_envelope(obj)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
