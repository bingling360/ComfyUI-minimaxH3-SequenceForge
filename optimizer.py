"""H3 提示词优化后端（自研，不依赖外部节点）。

思路参考 goohai/Goohai-MiniMax-H3_Integration 的 prompt_optimizer（本地+云双通道、
规则文件注入、媒体引用），实现全部自写，避免 GPL 原样拷贝：

- 配置归一 + 公开展示（api_key 脱敏，只给 has_api_key）
- prompt/*.txt 规则文件只读下发，由调用方按 rule_file=auto/指定/none 注入
- 云通道：OpenAI 兼容（openai/openrouter/百炼/SiliconFlow/RunningHub 走兼容路径）、
  Gemini GenerateContent、Responses 路径；同步 urllib 实现，调用方放线程池
- 本地通道：Transformers 视觉模型 / GGUF（llama-cpp）可选，未装时报明确缺件错误
- 媒体：图片 dataURL 直传；视频/音频只传 label（不传二进制），与前端约定一致

无第三方导入（torch/transformers/llama_cpp 只在函数内按需 import）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

DEFAULT_CONFIG = {
    "mode": "api",
    "provider": "runninghub",
    "api_url": "https://www.runninghub.cn/openapi/v2",
    "api_key": "",
    "model": "openai/gpt-5.6-sol",
    "protocol": "runninghub",
    "read_media": True,
    "output_language": "中文",
    "local_model": "",
    "local_mmproj": "",
    "local_device": "cuda",
    "max_tokens": 4096,
    "auto_optimize": False,
    "rule_file": "auto",
}

PROVIDERS = {
    "openai": ("https://api.openai.com/v1", "gpt-4.1-mini", "openai"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash", "gemini"),
    "openrouter": ("https://openrouter.ai/api/v1", "google/gemini-2.5-flash", "openai"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-vl-max", "openai"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-VL-72B-Instruct", "openai"),
    "runninghub": ("https://www.runninghub.cn/openapi/v2", "openai/gpt-5.6-sol", "openai"),
    "runninghub_overseas": ("https://www.runninghub.ai/openapi/v2", "openai/gpt-5.6-sol", "openai"),
    "custom": ("", "", "openai"),
}

RULE_OPTIONS = (
    "auto",
    "minimaxh3_custom_ref2v_prompt_writing_zh.txt",
    "minimaxh3_custom_ref2v_prompt_writing.txt",
    "minimaxh3_official_ref2v_prompt_writing.txt",
    "none",
)


def prompt_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt")


def load_rule_files() -> dict:
    out: dict = {}
    try:
        names = sorted(os.listdir(prompt_dir()))
    except OSError:
        return out
    for name in names:
        if not name.lower().endswith(".txt"):
            continue
        try:
            with open(os.path.join(prompt_dir(), name), "r", encoding="utf-8") as fh:
                out[name] = fh.read()
        except OSError:
            continue
    return out


def pick_rule_text(settings: dict | None, files: dict | None) -> str | None:
    sel = str((settings or {}).get("rule_file") or "auto")
    if sel == "none":
        return None
    files = files if isinstance(files, dict) else load_rule_files()
    if sel != "auto":
        return files.get(sel)
    lang = str((settings or {}).get("output_language") or "中文")
    name = ("minimaxh3_custom_ref2v_prompt_writing_zh.txt"
            if lang == "中文" else "minimaxh3_custom_ref2v_prompt_writing.txt")
    return files.get(name)


def normalize_config(raw: dict | None) -> dict:
    cur = DEFAULT_CONFIG
    raw = raw if isinstance(raw, dict) else {}
    provider = str(raw.get("provider") or cur["provider"]).lower()
    preset = PROVIDERS.get(provider)
    api_keys = raw.get("api_keys") if isinstance(raw.get("api_keys"), dict) else {}
    pk = api_keys.get(provider)
    api_key = pk if pk is not None else raw.get("api_key", cur["api_key"])
    provider_models = raw.get("provider_models") if isinstance(raw.get("provider_models"), dict) else {}
    pm = provider_models.get(provider)
    try:
        max_tokens = int(raw.get("max_tokens", cur["max_tokens"]))
    except (TypeError, ValueError):
        max_tokens = 4096
    max_tokens = max(512, min(8192, max_tokens))
    lang_raw = str(raw.get("output_language") or cur["output_language"])
    lang = "中文" if lang_raw.lower() in {"中文", "chinese", "zh"} else "English"
    return {
        "mode": "local" if str(raw.get("mode") or cur["mode"]).lower() == "local" else "api",
        "provider": provider,
        "api_url": str(raw.get("api_url") or (preset[0] if preset else cur["api_url"])).strip(),
        "api_key": str(api_key or ""),
        "api_keys": {str(k): str(v or "") for k, v in api_keys.items()},
        "model": str(pm or raw.get("model") or (preset[1] if preset else cur["model"])).strip(),
        "provider_models": {str(k): str(v or "") for k, v in provider_models.items()},
        "protocol": str(raw.get("protocol") or (preset[2] if preset else cur["protocol"])).lower(),
        "read_media": bool(raw.get("read_media", cur["read_media"])),
        "output_language": lang,
        "local_model": str(raw.get("local_model") or cur["local_model"] or "").strip(),
        "local_mmproj": str(raw.get("local_mmproj") or cur["local_mmproj"] or "").strip(),
        "local_device": str(raw.get("local_device") or cur["local_device"] or "cuda").lower(),
        "max_tokens": max_tokens,
        "auto_optimize": bool(raw.get("auto_optimize", cur["auto_optimize"])),
        "rule_file": str(raw.get("rule_file") or cur["rule_file"]),
    }


def public_config(cfg: dict) -> dict:
    out = dict(cfg)
    out["api_key"] = ""
    out["has_api_key"] = bool(cfg.get("api_key"))
    try:
        out["models"] = scan_visual_models()
    except Exception:
        out["models"] = []
    try:
        out["mmproj_models"] = scan_mmproj_models()
    except Exception:
        out["mmproj_models"] = []
    return out


def _llm_roots() -> list:
    try:
        import folder_paths
        base = os.path.join(os.path.dirname(str(folder_paths.get_input_directory())), "models")
    except Exception:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models")
    pref = os.path.join(base, "llm")
    found = []
    try:
        for name in os.listdir(base):
            if name.lower() == "llm" and os.path.isdir(os.path.join(base, name)):
                found.append(os.path.join(base, name))
    except OSError:
        pass
    return found or [pref]


def _is_visual_dir(path: str) -> bool:
    cfg = os.path.join(path, "config.json")
    if not (os.path.isdir(path) and os.path.isfile(cfg)):
        return False
    try:
        with open(cfg, "r", encoding="utf-8") as fh:
            text = fh.read().lower()
    except OSError:
        return False
    markers = ("qwen2-vl", "qwen2.5-vl", "qwen3-vl", "qwen-vl", "vision", "visual",
               "gemma3", "gemma-3", "minicpm-v", "internvl")
    if not any(m in text for m in markers):
        return False
    return any(os.path.exists(os.path.join(path, n)) for n in
               ("processor_config.json", "preprocessor_config.json", "tokenizer_config.json"))


def scan_visual_models() -> list:
    out = []
    for root in _llm_roots():
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            full = os.path.join(root, name)
            if full.lower().endswith(".gguf") and os.path.isfile(full):
                low = name.lower()
                if "mmproj" in low or "projector" in low:
                    continue
                if any(k in low for k in ("qwen3", "qwen2", "qwq", "vl", "vision")):
                    out.append({"name": name, "relative_path": name, "format": "gguf"})
            elif _is_visual_dir(full):
                out.append({"name": name, "relative_path": name, "format": "transformers"})
    return out


def scan_mmproj_models() -> list:
    out = []
    for root in _llm_roots():
        for dirpath, _dirs, files in os.walk(root):
            for fn in sorted(files):
                low = fn.lower()
                if low.endswith(".gguf") and ("mmproj" in low or "projector" in low):
                    full = os.path.join(dirpath, fn)
                    try:
                        rel = os.path.relpath(full, root).replace("\\", "/")
                    except ValueError:
                        rel = fn
                    out.append({"name": fn, "relative_path": rel})
    return out


def build_system_prompt(task: str, duration: float, labels: list,
                        output_language: str = "中文", context: dict | None = None) -> str:
    """自写的系统提示词：只定结构与标签纪律，文风细则由 prompt/*.txt 规则注入。"""
    t = str(task or "T2VA").upper()
    dur = max(0.5, min(30.0, float(duration or 5.0)))
    lang = "中文" if str(output_language or "中文") in ("中文", "chinese", "zh") else "English"
    lbl = ", ".join(labels) if labels else "无"
    if t in ("REF2VA", "HYBRID"):
        return (
            "你是 MiniMax H3 全参考提示词改写器。输出语言：%s。"
            "恰好输出六节，每节标题独占一行、冒号后换行写内容，节间空一行："
            "subject_definitions, summary, retention_analysis, detailed_description, "
            "overall_soundscape, non_diegetic_music。"
            "标签纪律：复用可见内容用 <Subject N>，首尾帧等具体帧锚用 <Picture N>，"
            "整片编辑/续写用 <Video N>，音频用 <Audio N>；summary 首行用 [task type] 前缀；"
            "retention 每行形如 <label>: marker - 解释；对白用 <d>[语言] 原文</d>，说话人用 (S1)/(S2)；"
            "镜头用 [Shot 1] 开头（无时间戳），后续 [Shot N] At MM:SS.mmm；"
            "只改写用户给的内容，不虚构新事件。目标时长 %.1fs，可用标签：%s。"
            % (lang, dur, lbl)
        )
    if t == "FL2VA":
        return (
            "你是 MiniMax H3 首尾帧提示词改写器。输出语言：%s。恰好三节："
            "integrated_multimodal_description, overall_soundscape, non_diegetic_music。"
            "首尾帧标签必须用小写裸词 picture 1（0.00s 起点锚）与 picture 2（%.2fs 终点锚），"
            "首尾各复述一次；描述一条可观察的连续运动路径；对白用 <d>[语言] 原文</d>。"
            % (lang, dur)
        )
    return (
        "你是 MiniMax H3 提示词改写器（%s）。输出语言：%s。恰好三节："
        "integrated_multimodal_description, overall_soundscape, non_diegetic_music。"
        "用 [Shot 1] 开头，有真实切镜才加后续 [Shot N] At MM:SS.mmm；"
        "运镜写自然语句（含类型/幅度/速度）；对白用 <d>[语言] 原文</d>，说话人 (S1)/(S2)；"
        "环境氛围进 overall_soundscape，角色听不到的配乐进 non_diegetic_music（无则 N/A）。"
        "目标时长 %.1fs，可用标签：%s。只改写用户给的内容。"
        % (t, lang, dur, lbl)
    )


def _http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = getattr(resp, "status", 200)
    except Exception as e:
        raise RuntimeError(f"LLM 请求失败：{e}")
    if status >= 400:
        raise RuntimeError(f"LLM 返回 HTTP {status}：{body[:1000]}")
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError("LLM 返回了无效 JSON")


def _endpoint_for(cfg: dict) -> str:
    base = str(cfg.get("api_url") or "").rstrip("/")
    proto = str(cfg.get("protocol") or "openai").lower()
    if proto == "gemini":
        if re.search(r":generateContent(?:\?|$)", base):
            return base
        return f"{base}/models/{urllib.parse.quote(str(cfg.get('model') or ''), safe='-_.')}:generateContent"
    if proto == "responses":
        return base if base.endswith("/responses") else f"{base}/responses"
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _api_generate(cfg: dict, system: str, user_prompt: str, media: list, max_tokens: int) -> str:
    if not cfg.get("api_key"):
        raise ValueError("未配置 API Key（请在优化设置里填写，或改用本地模型）")
    if not cfg.get("api_url") or not cfg.get("model"):
        raise ValueError("API 地址或模型为空")
    proto = str(cfg.get("protocol") or "openai").lower()
    # 图片只取前 8 张（与测试分支一致，防包过大）
    images: list = []
    for item in media or []:
        if not isinstance(item, dict):
            continue
        for u in (item.get("images") or [])[:8]:
            if isinstance(u, str) and u.startswith("data:image"):
                images.append(u)
        if len(images) >= 8:
            break
    if proto == "gemini":
        parts: list = [{"text": system + "\n\n" + user_prompt}]
        for u in images:
            try:
                head, b64 = u.split(",", 1)
                mime = head.split(";", 1)[0].split(":", 1)[1]
            except (ValueError, IndexError):
                continue
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        res = _http_post_json(
            _endpoint_for(cfg) + f"?key={urllib.parse.quote(str(cfg['api_key']))}",
            {"contents": [{"parts": parts}], "generationConfig": {"maxOutputTokens": max_tokens}})
        try:
            return str(res["candidates"][0]["content"]["parts"][0]["text"]).strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("Gemini 返回结构异常")
    if proto == "responses":
        content: list = [{"type": "input_text", "text": system + "\n\n" + user_prompt}]
        for u in images:
            content.append({"type": "input_image", "image_url": u})
        res = _http_post_json(_endpoint_for(cfg), {
            "model": cfg["model"], "input": [{"role": "user", "content": content}],
            "max_output_tokens": max_tokens},
            headers={"Authorization": f"Bearer {cfg['api_key']}"})
        try:
            for item in res.get("output", []):
                for c in item.get("content", []):
                    if c.get("type") in ("output_text", "text") and str(c.get("text") or "").strip():
                        return str(c["text"]).strip()
        except AttributeError:
            pass
        raise RuntimeError("Responses 返回结构异常")
    # 默认 OpenAI 兼容（含 RunningHub/百炼/SiliconFlow/OpenRouter）
    content2: list = [{"type": "text", "text": user_prompt}]
    for u in images:
        content2.append({"type": "image_url", "image_url": {"url": u}})
    res = _http_post_json(_endpoint_for(cfg), {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content2}],
        "max_tokens": max_tokens, "temperature": 0.2},
        headers={"Authorization": f"Bearer {cfg['api_key']}"})
    try:
        text = str(res["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("LLM 返回结构异常")
    if not text:
        raise RuntimeError("LLM 返回空文本")
    return text


def _local_generate(cfg: dict, system: str, user_prompt: str, media: list) -> str:
    sel = str(cfg.get("local_model") or "").strip()
    if not sel:
        raise ValueError("请先在优化设置里选择本地视觉模型")
    # GGUF 路径
    if sel.lower().endswith(".gguf"):
        try:
            from llama_cpp import Llama  # type: ignore
        except ImportError:
            raise ValueError("未安装 llama-cpp-python，无法加载 GGUF 本地模型（详见 goohai 项目说明按 CUDA/Python 版本装轮子）")
        raise ValueError("GGUF 本地推理走 llama-cpp 常驻通道，本期先报未接通：请改用 Transformers 模型或云 API")
    # Transformers 路径
    try:
        import torch  # type: ignore
        from transformers import AutoModelForImageTextToText, AutoProcessor  # type: ignore
    except ImportError:
        raise ValueError("未安装 transformers/torch，无法加载本地视觉模型（请改用云 API）")
    _ = (torch, AutoModelForImageTextToText, AutoProcessor)
    raise ValueError("本地 Transformers 推理需常驻显存通道，本期先报未接通：请改用云 API")


def optimize_once(config_in: dict | None, payload: dict | None) -> str:
    """单次优化入口（同步，调用方放线程池）。返回优化后文本。"""
    cfg = normalize_config(config_in)
    payload = payload if isinstance(payload, dict) else {}
    user_prompt = str(payload.get("prompt") or "").strip()
    if not user_prompt:
        raise ValueError("提示词为空，无需优化")
    task = str(payload.get("task") or "T2VA")
    try:
        duration = float(payload.get("duration") or 5.0)
    except (TypeError, ValueError):
        duration = 5.0
    media = payload.get("media") if isinstance(payload.get("media"), list) else []
    labels = [str(m.get("label")) for m in media if isinstance(m, dict) and m.get("label")]
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    system = build_system_prompt(task, duration, labels, cfg.get("output_language"), context)
    # 规则文件强注入（最高优先级）
    rule_text = pick_rule_text(cfg, None)
    if rule_text:
        user_prompt = ("请严格按照以下《提示词撰写规则》重写用户提供的视频提示词，"
                       "此规则优先于其他通用格式要求：\n\n" + rule_text +
                       "\n\n===== 待重写的用户提示词 =====\n" + user_prompt)
    if cfg.get("mode") == "local":
        return _local_generate(cfg, system, user_prompt, media)
    return _api_generate(cfg, system, user_prompt, media, int(cfg.get("max_tokens") or 4096))
