"""expander 的服务化入口：把 CLI 能力暴露成可被节点/HTTP 调用的函数。

路由 POST /h3chain/expand 直接调 expand_via_config，前端"AI扩写"按钮才真正可用。
确认环不在这里做交互：本模块只产出"信封 + 确认卡"，确认动作由前端面板承载。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import optimizer as _opt_backend

import screenplay
from confirm_card import render_card
from h3_expand import (DEFAULT_MODEL, STYLES, expand_once, render_h3,
                       revise_envelope)
from validate import validate_envelope

VALID_MODES = ("T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA")


def expand_via_config(config_in, payload):
    """主入口：中文意图 -> {intent, pe, h3_text, card_md, validation, meta}。

    payload 字段：
      prompt        中文原文（或 intent.logline_zh）
      duration      4-15，默认 5
      mode          T2VA/I2VA/FL2VA/L2VA/Ref2VA，默认 T2VA
      style         strict/balanced/creative，默认 balanced
      style_note    风格/案例参考文本
      two_step      是否双跳（先理解后编译）
      max_repairs   repair 次数，默认 2
      intent        已确认的意图 IR（给了就跳过理解，直接编译）
      from_envelope 上次信封 + revision，走修订重入
      media         参考素材 [{label, images:[dataURL]}]
    """
    cfg = _opt_backend.normalize_config(config_in)
    raw = str(payload.get("prompt") or "").strip()
    intent_in = payload.get("intent")
    from_env = payload.get("from_envelope")
    revision = str(payload.get("revision") or "").strip()

    if not raw and isinstance(intent_in, dict):
        raw = str(intent_in.get("logline_zh") or "").strip()
    if not raw and not isinstance(intent_in, dict) and not isinstance(from_env, dict):
        raise ValueError("prompt 为空：请先写一段中文意图")

    duration = float(payload.get("duration") or 5.0)
    duration = max(4.0, min(15.0, duration))
    mode = str(payload.get("mode") or "T2VA").upper()
    if mode not in VALID_MODES:
        raise ValueError(f"mode 非法：{mode}，只许 {VALID_MODES}")
    style = str(payload.get("style") or "balanced").lower()
    if style not in STYLES:
        style = "balanced"
    model = str(payload.get("model") or cfg.get("model") or DEFAULT_MODEL)
    temperature = payload.get("temperature")
    max_repairs = int(payload.get("max_repairs") or 2)
    media = payload.get("media") if isinstance(payload.get("media"), list) else []
    if not cfg.get("read_media"):
        media = []

    if isinstance(from_env, dict):
        if not revision:
            raise ValueError("给了 from_envelope 就必须给 revision 修订意见")
        env = {k: v for k, v in from_env.items() if k != "_meta"}
        env = revise_envelope(env, revision, model, temperature, max_repairs,
                              cfg=cfg, media=media)
    else:
        env = expand_once(raw, duration, mode, model, temperature, max_repairs,
                          two_step=bool(payload.get("two_step")),
                          style=style, style_note=str(payload.get("style_note") or ""),
                          intent_override=intent_in if isinstance(intent_in, dict) else None,
                          cfg=cfg, media=media)

    intent = env.get("intent") or {}
    pe = env.get("pe") or {}
    verdict = validate_envelope(env)
    meta = dict(env.get("_meta") or {})
    meta["validation"] = verdict
    meta.update({"duration": duration, "mode": mode, "media_count": len(media)})

    return {
        "intent": intent,
        "pe": pe,
        "envelope": env,
        "h3_text": render_h3(env),
        "card_md": render_card(env),
        "validation": verdict,
        "ok": bool(verdict.get("ok")),
        "meta": meta,
    }


def expand_optimize_via_config(config_in, payload, on_progress=None):
    """**一步到位**：中文意图 -> 剧本（扩写）-> H3 官方格式（优化）-> 最终文本。

    三框合一之后段卡只有一个框，不再有"① 意图 / ② 剧本"两个中间稿可点，
    所以把原来"AI扩写 → 剧本 → 提示词优化"两步并在后端一次做完：
    前端只有一个按钮、不弹窗，中间剧本不落库（只在返回里带一份，便于排查）。

    payload 与 screenplay_via_config / optimize_once 同源：
      prompt / style / style_note / segment_count / seconds_min、max / media
      task / duration / context（优化阶段用）
    返回 {ok, prompt（最终结果）, script（中间剧本，仅回看用）, seconds, meta}

    on_progress：两个阶段的事件都带上 `stage` 字段（"expand" / "optimize"），
    前端据此把进度条画成两段（扩写 0→45%，优化 45→99%）。两次调用都是
    20~40 秒级，不给进度就是"点下去没反应"。
    """
    cfg = _opt_backend.normalize_config(config_in)
    payload = payload if isinstance(payload, dict) else {}

    def _stage(name):
        def _emit(ev):
            if on_progress:
                on_progress(dict(ev, stage=name))
        return _emit

    # ① 扩写：出剧本（中文自由格式，内容发散）
    sp = screenplay_via_config(cfg, payload, on_progress=_stage("expand"))
    segs = [s for s in (sp.get("segments") or []) if isinstance(s, dict)]
    script = str((segs[0] if segs else {}).get("script") or "").strip()
    if not script:
        raise ValueError("扩写返回为空：换个模型，或把意图写得具体一点再试")

    # ② 优化：把剧本压成官方格式（这一步才决定进模型的文本长什么样）
    seconds = (segs[0] or {}).get("seconds") or payload.get("duration") or 5
    prompt = _opt_backend.optimize_once(cfg, {
        "prompt": script,
        "task": payload.get("task") or "T2VA",
        "duration": seconds,
        "media": payload.get("media") if isinstance(payload.get("media"), list) else [],
        "context": payload.get("context") if isinstance(payload.get("context"), dict) else {},
    }, on_progress=_stage("optimize"))
    return {
        "ok": bool(str(prompt or "").strip()),
        "prompt": str(prompt or "").strip(),
        "script": script,               # 中间稿：只回传，不落库
        "seconds": seconds,
        "meta": dict(sp.get("meta") or {}),
    }


def validate_only(payload):
    """只校验（不调 LLM）：给前端"校验"按钮用。"""
    env = payload.get("envelope") or payload
    verdict = validate_envelope(env)
    return {"ok": bool(verdict.get("ok")), "validation": verdict}


def screenplay_via_config(config_in, payload, on_progress=None):
    """剧本扩写入口：总意图 -> N 段中文剧本（内容发散，不带官方格式）。

    与 expand_via_config 的分工：这里只管**内容**，格式交给提示词优化。
    payload:
      prompt          总意图（单段时即本段意图）
      segment_count   段数 1-12，默认 1
      seconds_min/max 每段时长**范围**（4-15，默认 6-10），模型在范围内自定秒数
      style           strict/balanced/creative
      style_note      风格/案例参考
      media           参考素材 [{label, images:[dataURL]}]
      one_shot        是否一次出全部（缺省：段数 <= 3 时自动启用）
    on_progress     给了就透传给 LLM 通道（流式），上层据此画进度条。
                     只覆盖**单段**路径；多段走 outline + 逐段，调用点多且节奏
                     不同，暂不透传（前端多段入口也没有进度条）。
    """
    cfg = _opt_backend.normalize_config(config_in)
    payload = payload if isinstance(payload, dict) else {}
    raw = str(payload.get("prompt") or "").strip()
    if not raw:
        raise ValueError("prompt 为空：请先写一段中文意图")
    model = str(payload.get("model") or cfg.get("model") or DEFAULT_MODEL)
    style = str(payload.get("style") or "balanced").lower()
    if style not in STYLES:
        style = "balanced"
    media = payload.get("media") if isinstance(payload.get("media"), list) else []
    if not cfg.get("read_media"):
        media = []
    count = screenplay.clamp_count(payload.get("segment_count") or
                                   payload.get("count") or 1)
    sec_range = screenplay.clamp_seconds_range(payload.get("seconds_min", 6),
                                               payload.get("seconds_max", 10))
    temperature = payload.get("temperature")

    if count <= 1:
        res = screenplay.screenplay_once(raw, sec_range, model, cfg, media=media,
                                         style=style,
                                         style_note=str(payload.get("style_note") or ""),
                                         temperature=temperature,
                                         on_progress=on_progress)
        return {
            "ok": bool(str(res.get("script") or "").strip()),
            "segments": [{"index": 0, "seconds": res.get("seconds"),
                          "logline_zh": raw, "transition": "开场",
                          "script": res.get("script") or ""}],
            "meta": dict(res.get("meta") or {}, count=1, media_count=len(media)),
        }
    res = screenplay.screenplay_multi(raw, count, sec_range, model, cfg, media=media,
                                      style=style,
                                      style_note=str(payload.get("style_note") or ""),
                                      temperature=temperature,
                                      one_shot=payload.get("one_shot"))
    res = dict(res)
    res["meta"] = dict(res.get("meta") or {}, media_count=len(media))
    return res
