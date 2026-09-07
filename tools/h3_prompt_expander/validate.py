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
    else:
        desc = pe.get("integrated_multimodal_description") or ""
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

    # 长度：5s 配 60-120 词，过长多为企划书直翻
    words = len(re.findall(r"[A-Za-z']+", desc))
    if words and (words < 20 or words > 260):
        warn("W_LENGTH", f"description 约 {words} 词，建议 5s 配 60-120 词（过短太空、过长必 rush）")

    # ---- 镜头时间戳 ----
    shots = list(SHOT_RE.finditer(desc))
    if not shots:
        err("E_NO_SHOT", "缺少 [Shot 1] 镜头记法")
    else:
        nums = [int(m.group(1)) for m in shots]
        if nums[0] != 1 or nums != sorted(nums):
            err("E_SHOT_ORDER", f"Shot 编号必须从 1 递增，当前 {nums}")
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
