"""H3 提示词优化后端（自研，不依赖外部节点）。

思路参考 goohai/Goohai-MiniMax-H3_Integration 的 prompt_optimizer（本地+云双通道、
规则文件注入、媒体引用），实现全部自写，避免 GPL 原样拷贝：

- 配置归一 + 公开展示（api_key 脱敏，只给 has_api_key）
- prompt/*.txt 规则文件只读下发，由调用方按 rule_file=auto/指定/none 注入
- 云通道：OpenAI 兼容（openai/openrouter/百炼/SiliconFlow/RunningHub 走兼容路径）、
  Gemini GenerateContent、Responses 路径；同步 urllib 实现，调用方放线程池
- 本地通道：Transformers 视觉模型 / GGUF（llama-cpp-python），句柄进程内缓存，
  只扫 ComfyUI/models/llm；未装依赖时报明确缺件错误
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
    "minimaxh3_base_prompt_writing_zh.txt",
    "minimaxh3_base_prompt_writing.txt",
    "minimaxh3_custom_ref2v_prompt_writing_zh.txt",
    "minimaxh3_custom_ref2v_prompt_writing.txt",
    "minimaxh3_official_ref2v_prompt_writing.txt",
    "none",
)

# 规则文件按模式分流：官方 base（T2VA/I2VA/FL2VA/L2VA）与全参考（Ref2VA）是两套
# 完全不同的字段集（三字段 vs 六字段）与标签语法（<Picture N> vs <Subject N>）。
# 历史 bug：auto + 中文一律注入"自定义中文版"——那是一份**全参考四字段**规则，
# 且注入语写着"此规则优先于其他通用格式要求"，于是给常规段优化时模型同时收到
# 两条互相矛盾的最高优先级指令，产出 summary/detailed_description + <@名字>/
# <#名字:对话> 这类非官方语法。这里按 task 选文件，从根上分流。
REF_TASKS = ("REF2VA", "HYBRID")
RULE_BASE = {"中文": "minimaxh3_base_prompt_writing_zh.txt",
             "English": "minimaxh3_base_prompt_writing.txt"}
RULE_REF = {"中文": "minimaxh3_custom_ref2v_prompt_writing_zh.txt",
            "English": "minimaxh3_custom_ref2v_prompt_writing.txt"}

# 本地模型句柄进程内缓存：同一模型只加载一次（加载一次几十秒，反复加载会拖死节点）
_GGUF_CACHE: dict = {}
_TF_CACHE: dict = {}
MAX_LLM_IMAGES = 9  # 与 H3 单段图片上限一致；超出时显式报错，不静默丢图


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


def pick_rule_text(settings: dict | None, files: dict | None, task: str | None = None) -> str | None:
    """按「显式选择 > 语言+模式」选规则文件。

    task 为 None 时按常规（base）处理——调用方应显式传 task（见 optimize_once）。
    """
    sel = str((settings or {}).get("rule_file") or "auto")
    if sel == "none":
        return None
    files = files if isinstance(files, dict) else load_rule_files()
    if sel != "auto":
        return files.get(sel)
    lang = "中文" if str((settings or {}).get("output_language") or "中文") == "中文" else "English"
    is_ref = str(task or "").upper() in REF_TASKS
    table = RULE_REF if is_ref else RULE_BASE
    return files.get(table[lang]) or files.get(RULE_REF[lang])


def _stored_config() -> dict:
    try:
        try:
            from . import llm_config
        except ImportError:
            import llm_config
        return llm_config.load()
    except Exception:
        return {}


def save_user_config(raw: dict | None) -> dict:
    try:
        try:
            from . import llm_config
        except ImportError:
            import llm_config
        return llm_config.save(raw, keep_secrets=True)
    except Exception as exc:
        raise RuntimeError(f"用户级 LLM 配置保存失败：{exc}") from exc


def normalize_config(raw: dict | None) -> dict:
    cur = DEFAULT_CONFIG
    incoming = raw if isinstance(raw, dict) else {}
    stored = _stored_config()
    # 前端新请求只带非敏感配置或空 api_key 时，自动合并用户级配置；
    # 显式非空值仍可在当前请求覆盖用户默认值。
    raw = dict(stored)
    raw.update(incoming)
    if not str(incoming.get("api_key") or "").strip() and stored.get("api_key"):
        raw["api_key"] = stored["api_key"]
    if not isinstance(incoming.get("api_keys"), dict) and isinstance(stored.get("api_keys"), dict):
        raw["api_keys"] = stored["api_keys"]
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


def _safe_model_ref(value) -> str:
    value = str(value or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or ":" in value or ".." in value.split("/"):
        return ""
    return value


def public_config(cfg: dict) -> dict:
    out = dict(cfg)
    out["api_key"] = ""
    out["api_keys"] = {}
    out["has_api_key"] = bool(cfg.get("api_key") or any(cfg.get("api_keys", {}).values()))
    # 本地模型路径也不应随项目/设置响应向前端暴露绝对路径；前端只使用扫描出的相对名。
    out["local_model_ref"] = _safe_model_ref(cfg.get("local_model"))
    out["local_mmproj_ref"] = _safe_model_ref(cfg.get("local_mmproj"))
    out["local_model"] = ""
    out["local_mmproj"] = ""
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
    # 传**素材名**而不是 <Picture N>：LLM 写 @素材名，由后端 _apply_label_tokens
    # 按 seg.refs 顺序压实成 <Picture k>。这样顺序无关、回填时前端
    # applyPromptEdit→syncRefsFromText 会自动挂图，外部 agent 也只需知道素材名。
    # 与 web/h3_director.js 的 collectSegMedia 配套（它把素材名放进 media.label）。
    lbl = (("可用素材：" + "、".join("@" + str(x) for x in labels)
            + "（正文里直接写 @名字 引用，不要写 <Picture N>）") if labels
           else "本段无参考素材")
    if t in ("REF2VA", "HYBRID"):
        return (
            "你是 MiniMax H3 全参考提示词改写器。输出语言：%s。"
            "恰好输出六节，每节标题独占一行、冒号后换行写内容，节间空一行："
            "subject_definitions, summary, retention_analysis, detailed_description, "
            "overall_soundscape, non_diegetic_music。"
            # 参考素材走 @素材名（见 lbl），这里不再提 <Picture N> —— 否则与
            # 末尾「不要写 <Picture N>」自相矛盾。首尾帧对齐指令由前端自动生成，
            # 本就不需要模型写。
            "标签纪律：复用可见内容用 <Subject N>，参考素材直接用 @素材名（名单见末尾），"
            "整片编辑/续写用 <Video N>，音频用 <Audio N>；summary 首行用 [task type] 前缀；"
            "retention 每行形如 <label>: marker - 解释；对白用 <d>[语言] 原文</d>，说话人用 (S1)/(S2)；"
            "镜头用 [Shot 1] 开头（无时间戳），后续 [Shot N] At MM:SS.mmm；"
            "只改写用户给的内容，不虚构新事件。目标时长 %.1fs，%s。"
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
        "目标时长 %.1fs，%s。只改写用户给的内容。"
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


def _api_generate(cfg: dict, system: str, user_prompt: str, media: list, max_tokens: int,
                  temperature: float = 0.2) -> str:
    if not cfg.get("api_key"):
        raise ValueError("未配置 API Key（请在优化设置里填写，或改用本地模型）")
    if not cfg.get("api_url") or not cfg.get("model"):
        raise ValueError("API 地址或模型为空")
    proto = str(cfg.get("protocol") or "openai").lower()
    images = _media_images(media)
    if len(images) > MAX_LLM_IMAGES:
        raise ValueError(f"本次选择了 {len(images)} 张图片，超过视觉模型上限 {MAX_LLM_IMAGES} 张；请减少本段素材")
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
        "max_tokens": max_tokens, "temperature": _temp(temperature)},
        headers={"Authorization": f"Bearer {cfg['api_key']}"})
    try:
        text = str(res["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("LLM 返回结构异常")
    if not text:
        raise RuntimeError("LLM 返回空文本")
    return text


def _temp(v, default: float = 0.2) -> float:
    """温度归一：未给/非法回落默认值，钳到 [0, 2]。

    历史 bug：三档风格（strict/balanced/creative）的 temperature 只写在提示词里，
    HTTP 请求体里写死 0.2，采样温度从未真正变过。这里统一收口。"""
    try:
        t = float(v)
    except (TypeError, ValueError):
        return default
    if t < 0:
        return 0.0
    return min(2.0, t)


def _media_images(media: list) -> list:
    """收集全部图片 dataURL；上限由调用方显式检查，不静默丢素材。"""
    out: list = []
    for item in media or []:
        if not isinstance(item, dict):
            continue
        for u in item.get("images") or []:
            if isinstance(u, str) and u.startswith("data:image"):
                out.append(u)
    return out


def _b64_to_pil(data_url: str):
    """dataURL -> PIL.Image（PIL 随 transformers 一起装，缺失则报缺件）。"""
    import base64
    import io
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise ValueError("未安装 Pillow，无法读取图片素材")
    _, b64 = data_url.split(",", 1)
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


def _local_generate(cfg: dict, system: str, user_prompt: str, media: list,
                    temperature: float = 0.2) -> str:
    """本地视觉模型推理（GGUF / Transformers 两路），进程内缓存句柄避免重复加载。"""
    sel = str(cfg.get("local_model") or "").strip()
    if not sel:
        raise ValueError("请先在优化设置里选择本地视觉模型")
    images = _media_images(media) if cfg.get("read_media") else []
    if len(images) > MAX_LLM_IMAGES:
        raise ValueError(f"本次选择了 {len(images)} 张图片，超过视觉模型上限 {MAX_LLM_IMAGES} 张；请减少本段素材")
    root = (_llm_roots() or [""])[0]

    # ---- GGUF 路径（llama-cpp-python + mmproj 视觉投影）----
    if sel.lower().endswith(".gguf"):
        try:
            from llama_cpp import Llama  # type: ignore
            from llama_cpp.llama_chat_format import Llava15ChatHandler  # type: ignore
        except ImportError:
            raise ValueError("未安装 llama-cpp-python，无法加载 GGUF 本地模型"
                             "（按 CUDA/Python 版本装轮子，详见 goohai 项目说明）")
        mmproj = str(cfg.get("local_mmproj") or "").strip()
        llm = _GGUF_CACHE.get(sel)
        if llm is None:
            kw = {"model_path": os.path.join(root, sel), "n_ctx": 8192, "verbose": False}
            if mmproj:
                # mproj 走 GPU 加速（视觉塔比语言模型轻，显存占用小）
                kw["chat_handler"] = Llava15ChatHandler(
                    clip_model_path=os.path.join(root, mmproj), verbose=False)
            if str(cfg.get("local_device") or "").lower() == "cuda":
                kw["n_gpu_layers"] = -1
            llm = Llama(**kw)
            _GGUF_CACHE[sel] = llm
        content: list = [{"type": "text", "text": user_prompt}]
        for u in images:
            content.append({"type": "image_url", "image_url": {"url": u}})
        res = llm.create_chat_completion(
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": content}],
            max_tokens=int(cfg.get("max_tokens") or 4096), temperature=_temp(temperature))
        return str(res["choices"][0]["message"]["content"] or "").strip()

    # ---- Transformers 路径（AutoModelForImageTextToText + 进程内缓存）----
    try:
        import torch  # type: ignore
        from transformers import AutoModelForImageTextToText, AutoProcessor  # type: ignore
    except ImportError:
        raise ValueError("未安装 transformers/torch，无法加载本地视觉模型（请改用云 API）")
    cached = _TF_CACHE.get(sel)
    if cached is None:
        path = os.path.join(root, sel)
        device = str(cfg.get("local_device") or "cuda").lower()
        dtype = torch.float16 if device == "cuda" else torch.float32
        processor = AutoProcessor.from_pretrained(path, trust_remote_code=True)
        model = AutoModelForImageTextToText.from_pretrained(
            path, dtype=dtype, device_map=device, trust_remote_code=True)
        model.eval()
        cached = (processor, model)
        _TF_CACHE[sel] = cached
    processor, model = cached
    content = [{"type": "image"} for _ in images] + [{"type": "text", "text": user_prompt}]
    messages = [{"role": "system", "content": [{"type": "text", "text": system}]},
                {"role": "user", "content": content}]
    prompt = processor.apply_chat_template(messages, add_generation_prompt=True)
    inputs = processor(text=prompt, images=[_b64_to_pil(u) for u in images] or None,
                       return_tensors="pt").to(model.device)
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=int(cfg.get("max_tokens") or 4096),
                             do_sample=False)
    gen = out[:, inputs["input_ids"].shape[1]:]
    return processor.batch_decode(gen, skip_special_tokens=True)[0].strip()


def generate_text(config_in: dict | None, system: str, user_prompt: str,
                  media: list | None = None, max_tokens: int | None = None,
                  temperature: float | None = None) -> str:
    """通用文本生成入口：复用本模块的三协议云通道 + 本地通道。

    供 h3_prompt_expander 等上层工具调用，避免各自维护一套 HTTP 客户端。
    调用方负责拼 system/user；本函数只负责"把请求发出去并取回文本"。

    - 云通道：_api_generate（自动按 protocol 走 openai 兼容 / gemini / responses）
    - 本地通道：_local_generate（Transformers / GGUF）
    """
    cfg = normalize_config(config_in)
    system = str(system or "")
    user_prompt = str(user_prompt or "")
    if not user_prompt.strip():
        raise ValueError("待生成内容为空")
    if max_tokens is not None:
        try:
            cfg["max_tokens"] = max(512, min(8192, int(max_tokens)))
        except (TypeError, ValueError):
            pass
    media = media if isinstance(media, list) else []
    temp = _temp(temperature)
    if cfg.get("mode") == "local":
        return _local_generate(cfg, system, user_prompt, media, temp)
    return _api_generate(cfg, system, user_prompt, media, int(cfg.get("max_tokens") or 4096), temp)


def generate_json(config_in: dict | None, system: str, user_prompt: str,
                  media: list | None = None, max_tokens: int | None = None,
                  temperature: float | None = None) -> dict:
    """generate_text 的 JSON 版：容忍 ```json 围栏与前后废话，截取最大 JSON 段。"""
    text = generate_text(config_in, system, user_prompt, media, max_tokens, temperature)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
    raise RuntimeError(f"LLM 未返回合法 JSON：{text[:300]}")


def _generation_system_prompt(segments: list[dict], output_language: str) -> str:
    """统一提示词生成入口的系统约束：一次请求直接产出 H3 JSON。"""
    modes = {str(s.get("task") or "T2VA").upper() for s in segments}
    mode = next(iter(modes)) if len(modes) == 1 else "T2VA"
    duration = max([float(s.get("seconds") or 5.0) for s in segments] or [5.0])
    labels = []
    for seg in segments:
        for asset in seg.get("selected_assets") or []:
            if isinstance(asset, dict):
                name = str(asset.get("file_name") or asset.get("label") or "").strip()
            else:
                name = str(asset or "").strip()
            if name and name not in labels:
                labels.append(name)
    base = build_system_prompt(mode, duration, labels, output_language,
                               {"generation_mode": "one_shot_json"})
    return base + "\\n\\n" + (
        "你现在不是剧本扩写器，也不要输出解释、Markdown 或自由格式剧本。"
        "请一次完成：把每段 intent 直接改写成可执行的 MiniMax H3 官方提示词。"
        "参考素材在编辑层必须使用 @完整文件名（包含扩展名），禁止输出 <Picture N>、"
        "<Video N>、<Audio N>；这些 token 会由后端在发送 H3 前按正文首次出现顺序生成。"
        "返回严格 JSON，顶层只有 schema_version 和 segments。segments 必须按输入顺序返回，"
        "每项必须包含 segment_id、mode、fields、text。text 是 fields 按官方字段顺序拼接的纯文本；"
        "不要在 JSON 之外输出任何文字。schema_version 固定为 h3.prompt.generate.v1。"
    )


def _generation_user_prompt(segments: list[dict], output_language: str) -> str:
    """给 LLM 的文本上下文不携带 data URL；图片由 generate_text 的 media 参数传递。"""
    rows = []
    for index, seg in enumerate(segments):
        assets = []
        for asset in seg.get("selected_assets") or []:
            if isinstance(asset, dict):
                name = str(asset.get("file_name") or asset.get("label") or "").strip()
                kind = str(asset.get("kind") or "image").strip()
                desc = str(asset.get("description") or asset.get("role") or "").strip()
                if name:
                    assets.append({"file_name": name, "kind": kind, "description": desc})
            elif str(asset).strip():
                assets.append({"file_name": str(asset).strip(), "kind": "image"})
        rows.append({
            "index": index,
            "segment_id": str(seg.get("segment_id") or f"seg-{index + 1}"),
            "intent": str(seg.get("intent") or "").strip(),
            "duration_seconds": float(seg.get("seconds") or 5.0),
            "task": str(seg.get("task") or "T2VA").upper(),
            "selected_assets": assets,
            "current_result": str(seg.get("current_result") or "").strip(),
        })
    return (
        f"输出语言：{output_language}。以下是本次要生成的分段 JSON 输入：\\n"
        + json.dumps({"segments": rows}, ensure_ascii=False, indent=2)
        + "\\n请逐段直接生成最终 H3 字段，不要生成 script 字段。"
    )


def _normalize_generation_segments(raw: dict, requested: list[dict]) -> list[dict]:
    """把模型结果归一为请求段顺序，并拒绝模型擅自新增/调换 segment_id。"""
    if not isinstance(raw, dict):
        raise RuntimeError("LLM JSON 顶层不是对象")
    returned = raw.get("segments")
    if not isinstance(returned, list):
        # 单段兼容：允许旧模型直接返回 {text/fields}，但不对多段猜测。
        if len(requested) == 1 and (raw.get("text") or raw.get("fields")):
            returned = [raw]
        else:
            raise RuntimeError("LLM JSON 缺少 segments 数组")
    if len(returned) != len(requested):
        raise RuntimeError(f"LLM 返回 {len(returned)} 段，期望 {len(requested)} 段")
    out = []
    for index, (item, wanted) in enumerate(zip(returned, requested)):
        if not isinstance(item, dict):
            raise RuntimeError(f"LLM 返回的第 {index + 1} 段不是对象")
        wanted_id = str(wanted.get("segment_id") or f"seg-{index + 1}")
        got_id = str(item.get("segment_id") or wanted_id)
        if got_id != wanted_id:
            raise RuntimeError(f"LLM 修改了第 {index + 1} 段的 segment_id：{got_id}")
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        text = str(item.get("text") or "").strip()
        if not text and fields:
            try:
                import prompts as _prompts
                text = _prompts.serialize_fields({str(k): str(v or "") for k, v in fields.items()})
            except Exception as exc:
                raise RuntimeError(f"无法拼接第 {index + 1} 段 fields：{exc}") from exc
        if not text:
            raise RuntimeError(f"LLM 返回的第 {index + 1} 段结果为空")
        out.append({
            "segment_id": wanted_id,
            "mode": str(item.get("mode") or wanted.get("task") or "T2VA").upper(),
            "fields": fields,
            "text": text,
            "seconds": float(wanted.get("seconds") or 5.0),
            "selected_assets": list(wanted.get("selected_assets") or []),
        })
    return out


def generate_prompt_once(config_in: dict | None, payload: dict | None) -> dict:
    """一次 LLM 请求：意图/资产 -> 多段完整 H3 结果 JSON。

    这是新前端唯一应使用的生成业务入口。它只调用 ``generate_json``，不导入
    nodes.py、不调用 ComfyUI API、不创建或修改画布节点。
    """
    cfg = normalize_config(config_in)
    payload = payload if isinstance(payload, dict) else {}
    requested = payload.get("segments")
    if not isinstance(requested, list) or not requested:
        requested = [{
            "segment_id": str(payload.get("segment_id") or "seg-1"),
            "intent": str(payload.get("intent") or payload.get("total_intent") or "").strip(),
            "seconds": payload.get("seconds") or payload.get("duration") or 5.0,
            "task": payload.get("task") or "T2VA",
            "selected_assets": payload.get("selected_assets") or payload.get("assets") or [],
            "current_result": payload.get("current_result") or "",
        }]
    if len(requested) > 24:
        raise ValueError("一次最多生成 24 段提示词")
    normalized = []
    media = []
    shared_media = payload.get("media") if isinstance(payload.get("media"), list) else []
    for index, raw in enumerate(requested):
        raw = raw if isinstance(raw, dict) else {}
        try:
            seconds = float(raw.get("seconds") or raw.get("duration") or 5.0)
        except (TypeError, ValueError):
            seconds = 5.0
        assets = raw.get("selected_assets")
        if not isinstance(assets, list):
            assets = raw.get("assets") if isinstance(raw.get("assets"), list) else []
        seg = {
            "segment_id": str(raw.get("segment_id") or f"seg-{index + 1}"),
            "intent": str(raw.get("intent") or "").strip(),
            "seconds": seconds,
            "task": str(raw.get("task") or payload.get("task") or "T2VA").upper(),
            "selected_assets": assets,
            "current_result": str(raw.get("current_result") or "").strip(),
        }
        if not seg["intent"] and not seg["current_result"]:
            raise ValueError(f"第 {index + 1} 段意图为空")
        normalized.append(seg)
        seg_media = raw.get("media") if isinstance(raw.get("media"), list) else shared_media
        for item in seg_media:
            if isinstance(item, dict) and item not in media:
                media.append(item)
    system = _generation_system_prompt(normalized, cfg.get("output_language") or "中文")
    user_prompt = _generation_user_prompt(normalized, cfg.get("output_language") or "中文")
    raw = generate_json(cfg, system, user_prompt, media,
                        int(cfg.get("max_tokens") or 4096), _temp(payload.get("temperature")))
    results = _normalize_generation_segments(raw, normalized)
    all_ok = True
    for item in results:
        try:
            import prompts as _prompts
            verdict = _prompts.compile_prompt_document(
                item["text"], assets=item["selected_assets"], seconds=item["seconds"],
                mode=item["mode"], segment_id=item["segment_id"],
            )
        except Exception as exc:
            verdict = {"ok": False, "errors": [{"code": "H3_RESULT_VALIDATE_FAILED",
                                                   "message": str(exc)}], "warnings": []}
        item["ok"] = bool(verdict.get("ok"))
        item["errors"] = verdict.get("errors") or []
        item["warnings"] = verdict.get("warnings") or []
        item["editor_text"] = item["text"]
        item["compiled_preview"] = verdict.get("prompt_text") or ""
        all_ok = all_ok and item["ok"]
    return {
        "ok": all_ok,
        "schema_version": "h3.prompt.generate.v1",
        "segments": results,
        "meta": {"count": len(results), "failed": sum(1 for x in results if not x["ok"]),
                 "mode": cfg.get("mode"), "model": cfg.get("model") or ""},
    }


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
    # 规则文件强注入（最高优先级）：按模式分流，别再给常规段喂全参考规则
    rule_text = pick_rule_text(cfg, None, task)
    if rule_text:
        user_prompt = ("请严格按照以下《提示词撰写规则》重写用户提供的视频提示词，"
                       "此规则优先于其他通用格式要求：\n\n" + rule_text +
                       "\n\n===== 待重写的用户提示词 =====\n" + user_prompt)
    if cfg.get("mode") == "local":
        return _local_generate(cfg, system, user_prompt, media,
                               _temp(payload.get("temperature")))
    return _api_generate(cfg, system, user_prompt, media, int(cfg.get("max_tokens") or 4096),
                         _temp(payload.get("temperature")))


def _validate_optimized(text: str, mode: str, seconds: float) -> dict:
    """给优化结果补一道官方格式校验（历史缺口：优化结果从来不校验）。

    优化产出的是**文本**，用 prompts.parse_override 解析官方字段头，再走
    validate_compiled。解析/导入失败不阻断（校验只是提示层）。
    """
    try:
        import prompts as _prompts
    except Exception:
        return {"ok": True, "errors": [], "warnings": []}
    try:
        parsed, order, _preamble = _prompts.parse_override(text)
        compiled = {"mode": mode, "duration": float(seconds or 5.0), "fields": parsed,
                    "prompt_text": text, "warnings": [], "diagnostics": {},
                    "override": True}
        return _prompts.validate_compiled(compiled)
    except Exception:
        return {"ok": True, "errors": [], "warnings": []}


def optimize_multi_once(config_in: dict | None, payload: dict | None) -> dict:
    """多段一次性优化：把 N 段剧本（自由格式）逐段压成 H3 官方格式并校验。

    payload:
      segments: [{prompt, seconds, task, media?}]  —— prompt 是该段剧本正文
      task / media: 缺省值（各段未给时用它）
    返回 {ok, segments:[{index, seconds, task, result, errors, warnings, ok}], meta}
    ok = 所有非空段都通过官方校验（空段跳过不计）。
    """
    cfg = normalize_config(config_in)
    payload = payload if isinstance(payload, dict) else {}
    segs = payload.get("segments")
    if not isinstance(segs, list) or not segs:
        raise ValueError("segments 为空：至少要给一段剧本")
    if len(segs) > 24:
        raise ValueError(f"段数 {len(segs)} 过多，一次最多 24 段")
    shared_media = payload.get("media") if isinstance(payload.get("media"), list) else []
    out, all_ok = [], True
    for i, s in enumerate(segs):
        s = s if isinstance(s, dict) else {}
        script = str(s.get("prompt") or "").strip()
        task = str(s.get("task") or payload.get("task") or "T2VA")
        try:
            seconds = float(s.get("seconds") or 5.0)
        except (TypeError, ValueError):
            seconds = 5.0
        if not script:
            out.append({"index": i, "seconds": seconds, "task": task, "result": "",
                        "skipped": True, "errors": [], "warnings": [], "ok": True})
            continue
        text = optimize_once(cfg, {
            "prompt": script, "task": task, "duration": seconds,
            "media": s.get("media") if isinstance(s.get("media"), list) else shared_media,
            "context": {"main_mode": task},
        })
        verdict = _validate_optimized(text, task, seconds)
        ok = bool(verdict.get("ok"))
        all_ok = all_ok and ok
        out.append({"index": i, "seconds": seconds, "task": task, "result": text,
                    "skipped": False, "errors": verdict.get("errors") or [],
                    "warnings": verdict.get("warnings") or [], "ok": ok})
    return {"ok": all_ok, "segments": out,
            "meta": {"count": len(out),
                     "failed": sum(1 for x in out if not x.get("ok")),
                     "mode": "local" if cfg.get("mode") == "local" else "api",
                     "model": cfg.get("model") or ""}}
