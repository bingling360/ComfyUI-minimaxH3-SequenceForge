"""H3 提示词优化后端（自研，不依赖外部节点）。

思路参考 goohai/Goohai-MiniMax-H3_Integration 的 prompt_optimizer（本地+云双通道、
规则文件注入、媒体引用），实现全部自写，避免 GPL 原样拷贝：

- 配置归一 + 公开展示（api_key 脱敏，只给 has_api_key）
- prompt/*.txt 规则文件只读下发，由调用方按 rule_file=auto/指定/none 注入
- 云通道：OpenAI 兼容（openai/openrouter/百炼/SiliconFlow/RunningHub 走兼容路径）、
  Gemini GenerateContent、Responses 路径；同步 urllib 实现，调用方放线程池
- 本地通道：Transformers 视觉模型 / GGUF（llama-cpp-python），每次生成完即关句柄、
  归还显存（不做进程内缓存），只扫 ComfyUI/models/llm；未装依赖时报明确缺件错误
- 媒体：图片 dataURL 直传；视频/音频只传 label（不传二进制），与前端约定一致

无第三方导入（torch/transformers/llama_cpp 只在函数内按需 import）。
"""

from __future__ import annotations

import gc
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

# 本地私有默认值：optimizer.local.json（已 gitignore）—— 放 API Key 这类**不该进仓库**
# 的东西。有它就开箱即用（前端 Key 留空也照样能调），没有就回落内置默认值。
# 坏 JSON / 无权限一律静默忽略：这只是一层便利，不是必需品，绝不能因此让插件起不来。
LOCAL_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "optimizer.local.json")


def _load_local_defaults() -> dict:
    try:
        with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


LOCAL_DEFAULTS = _load_local_defaults()

DEFAULT_CONFIG = {
    "mode": "api",
    # 默认走智谱 GLM（国产、直连快、支持多图视觉理解）。
    # 注意 protocol 是 openai（v4 是 OpenAI 兼容接口），不是 runninghub。
    "provider": "glm",
    "api_url": "https://open.bigmodel.cn/api/paas/v4",
    "api_key": "",
    # glm-5.3-flashx：实测 18.8s 出 1330 字六字段（比 4.6v 快一档），
    # 代价是「始终思考」型号 —— 见 thinking 默认值的说明。
    "model": "glm-5.3-flashx",
    "protocol": "openai",
    "read_media": True,
    "output_language": "中文",
    "local_model": "",
    "local_mmproj": "",
    "local_device": "cuda",
    "max_tokens": 8192,
    # 单次 HTTP 读写超时（秒）。旧值 120 是"读操作超时"报错的直接原因：
    # 带图 + 长规则 + 六字段长输出的改写，GLM 开思考时轻易超过 120s。
    "timeout": 300,
    # GLM 系深度思考：auto=不干预（服务商默认）/ enabled / disabled。
    # 默认 disabled —— 意图是"别想太久、快点出结果"。
    # ⚠ 默认型号 glm-5.3-flashx 是「始终思考」型号，传 disabled 会 400，
    #   见 _glm_thinking_fields() 的自动降级：它会翻译成最低强度 low，
    #   而不是什么都不发（什么都不发 = 服务商默认，通常是 max，最慢的）。
    "thinking": "disabled",
    # 内部统一思考档位（off/low/medium/high/max，空=不干预）。三家之外的型号
    # 一律不干预（那是"未知型号"，下发私有字段只会 400）—— 见 _thinking_fields。
    "thinking_level": "off",
    # 思考强度（仅智谱 GLM-5 系支持）：""=不指定（服务商用默认，通常 max）
    # / low / high / max。5.3 系只认这三档，别的值会报错。
    "reasoning_effort": "",
    "rule_file": "auto",
    # 「AI 扩写优化设置」：单框提示词主按钮「AI 扩写 + 优化」的参数。
    # sec_min/sec_max = 0 表示"跟随本段时长 ±2 秒"（前端 optExpandSettings 负责换算）。
    "expand": {"sec_min": 0, "sec_max": 0, "style": "balanced", "run_optimize": True},
}

PROVIDERS = {
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "glm-5.3-flashx", "openai"),
    "openai": ("https://api.openai.com/v1", "gpt-4.1-mini", "openai"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash", "gemini"),
    "openrouter": ("https://openrouter.ai/api/v1", "google/gemini-2.5-flash", "openai"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-vl-max", "openai"),
    # DeepSeek 官方 OpenAI 兼容端点（base_url 不带 /v1，SDK/本插件会自动补
    # /chat/completions）。默认型号 deepseek-flash = DeepSeek-V4.1-Flash：
    # 它是**唯一支持图片输入**的 DeepSeek 型号（V4 Pro 不支持视觉），而本插件
    # 的提示词优化要读参考图，所以只能拿它当默认。思考默认开、默认 high，
    # 档位 off/low/high/max —— 见 _deepseek_thinking_fields()。
    "deepseek": ("https://api.deepseek.com", "deepseek-flash", "openai"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-VL-72B-Instruct", "openai"),
    "runninghub": ("https://www.runninghub.cn/openapi/v2", "openai/gpt-5.6-sol", "openai"),
    "runninghub_overseas": ("https://www.runninghub.ai/openapi/v2", "openai/gpt-5.6-sol", "openai"),
    "custom": ("", "", "openai"),
}

# 会走 bigmodel（智谱）私有 `thinking` 字段的端点特征。别的端点塞这个字段只会换来 400。
_GLM_HOSTS = ("bigmodel.cn", "bigmodel")

# ---- 智谱思考能力表（2026-09-19 实测 + 官方《深度思考》文档）----
# 「始终思考」型号：传 `thinking.type=disabled` 会被直接拒绝，实测报
#   HTTP 400 / code 1210「该模型始终思考，不支持关闭思考；请使用 low、high 或 max。」
# 复现：glm-5.3 / glm-5.3-flash / glm-5.3-flashx 全拒；glm-5.2 与 glm-4.6v 可关。
GLM_FORCE_THINKING = ("glm-5.3", "glm-4.7", "glm-4.5v")
# 支持 `reasoning_effort` 的型号（官方："仅 GLM-5.2 及以上支持"）。
# 取保守前缀：只有 glm-5 系确定支持，4.7/4.5v 不冒险下发（下发不被识别会报错）。
GLM_EFFORT_PREFIXES = ("glm-5",)
# 可选的思考强度。5.3 系只认这三档，多传别的值直接报错，所以这里就是白名单。
GLM_EFFORT_VALUES = ("low", "high", "max")


def _is_forced_thinking(model: str) -> bool:
    """该模型是否「始终思考」（不能关）。"""
    return str(model or "").lower().startswith(GLM_FORCE_THINKING)


def _supports_effort(model: str) -> bool:
    return str(model or "").lower().startswith(GLM_EFFORT_PREFIXES)


def _glm_caps(model: str) -> dict:
    """给前端的能力提示：这个型号能不能关思考、能不能调强度、关闭会被翻译成什么。"""
    forced = _is_forced_thinking(model)
    supports = _supports_effort(model)
    return {"forced_thinking": forced,
            "supports_effort": supports,
            "effort_values": list(GLM_EFFORT_VALUES),
            # 前端拿它渲染「关闭思考」的说明文案
            "disabled_effect": ("low" if (forced and supports)
                                else ("disabled" if not forced else ""))}


def default_config() -> dict:
    """内置默认值 + 本地私有覆盖（optimizer.local.json）。"""
    cur = dict(DEFAULT_CONFIG)
    cur["expand"] = dict(DEFAULT_CONFIG["expand"])
    for k, v in (LOCAL_DEFAULTS or {}).items():
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if k == "expand" and isinstance(v, dict):
            cur["expand"].update(v)
        elif k in cur:
            cur[k] = v
    return cur

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


def normalize_config(raw: dict | None) -> dict:
    cur = default_config()
    raw = raw if isinstance(raw, dict) else {}
    provider = str(raw.get("provider") or cur["provider"]).lower()
    preset = PROVIDERS.get(provider)
    api_keys = raw.get("api_keys") if isinstance(raw.get("api_keys"), dict) else {}
    pk = api_keys.get(provider)
    # 注意 `""` 与"缺失"必须同等对待：前端设置面板点一次「保存」就会把
    # `api_keys[provider] = ""` 写进来（Key 框留空时），若把空串当"用户显式置空"，
    # 内置 Key 的兜底会被自己顶掉，用户点完保存反而调不通。
    if pk is not None and str(pk).strip():
        api_key = pk
    else:
        api_key = raw.get("api_key")
        if api_key is None or not str(api_key).strip():
            # 前端留空（或压根没下发 Key）时回落到本地私有默认 Key。
            # **只在服务商正好等于本地默认服务商时回落** —— 否则会把 GLM 的 Key
            # 发给 OpenAI，换回来一个 401，比"没填 Key"更难排查。
            api_key = cur.get("api_key") if provider == str(cur.get("provider") or "").lower() else ""
    provider_models = raw.get("provider_models") if isinstance(raw.get("provider_models"), dict) else {}
    pm = provider_models.get(provider)
    try:
        max_tokens = int(raw.get("max_tokens", cur["max_tokens"]))
    except (TypeError, ValueError):
        max_tokens = int(cur["max_tokens"])
    # 上限从 8192 提到 32768：思考模型的 reasoning token **计入** completion_tokens，
    # 上限太小时推理会把预算吃光、正文一个字都留不下（前端看到的就是"LLM 返回空文本"）。
    max_tokens = max(512, min(32768, max_tokens))
    try:
        timeout = int(float(raw.get("timeout", cur.get("timeout", 300))))
    except (TypeError, ValueError):
        timeout = 300
    timeout = max(30, min(1800, timeout))
    thinking = str(raw.get("thinking") or cur.get("thinking") or "auto").lower()
    if thinking not in ("auto", "enabled", "disabled"):
        thinking = "auto"
    effort = str(raw.get("reasoning_effort") or cur.get("reasoning_effort") or "").lower()
    if effort not in GLM_EFFORT_VALUES:
        effort = ""
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
        "timeout": timeout,
        "thinking": thinking,
        "reasoning_effort": effort,
        # 新的**内部统一档位**（off/low/medium/high/max）。空串 = 不干预（auto）。
        # 旧的两个字段（thinking 开关 + reasoning_effort 强度）继续保留：
        # 没给新值时 _cfg_think_level() 会按旧字段推导，老配置不用迁移。
        "thinking_level": str(raw.get("thinking_level")
                              or cur.get("thinking_level") or "").lower(),
        "rule_file": str(raw.get("rule_file") or cur["rule_file"]),
        "expand": _clean_expand(raw.get("expand"), cur.get("expand")),
    }


def _clean_expand(raw, cur=None) -> dict:
    """「AI 扩写优化设置」收敛：秒数 0=跟随本段时长、风格白名单、布尔开关。

    风格取值与 tools/h3_prompt_expander/h3_expand.STYLES 对齐（strict/balanced/creative），
    非法值一律回落 balanced，绝不把脏值原样透给扩写器。
    """
    base = {"sec_min": 0, "sec_max": 0, "style": "balanced", "run_optimize": True}
    base.update(cur if isinstance(cur, dict) else {})
    r = raw if isinstance(raw, dict) else {}

    def _sec(v, d):
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            return d
        return max(0, min(15, n))

    style = str(r.get("style") or base["style"]).lower()
    if style not in ("strict", "balanced", "creative"):
        style = "balanced"
    return {
        "sec_min": _sec(r.get("sec_min"), base["sec_min"]),
        "sec_max": _sec(r.get("sec_max"), base["sec_max"]),
        "style": style,
        "run_optimize": bool(r.get("run_optimize", base["run_optimize"])),
    }


def public_config(cfg: dict) -> dict:
    out = dict(cfg)
    out["api_key"] = ""
    out["has_api_key"] = bool(cfg.get("api_key"))
    # 前端据此判断"Key 留空也能跑"（服务端有本地私有 Key 兜底），
    # 从而不再因为输入框为空就把用户拦到设置面板里。
    out["has_default_key"] = bool(str(LOCAL_DEFAULTS.get("api_key") or "").strip())
    out["providers"] = {k: {"url": v[0], "model": v[1], "protocol": v[2]}
                        for k, v in PROVIDERS.items()}
    # 思考能力表下发（**单一真相源**）：前端据此把「关闭思考」置灰、显示强度下拉。
    # 型号名单只写在后端一处，前端不重复一份 —— 两边各写一份必然漂移。
    out["glm_force_thinking"] = list(GLM_FORCE_THINKING)
    out["glm_effort_prefixes"] = list(GLM_EFFORT_PREFIXES)
    out["glm_effort_values"] = list(GLM_EFFORT_VALUES)
    out["model_caps"] = _glm_caps(cfg.get("model") or "")
    # 思考能力（三家之外的型号 known=false：前端要写明"不干预"，别让人以为默认关了）
    out["thinking_caps"] = _thinking_caps(cfg)
    out["thinking_levels"] = list(THINK_LEVELS)
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
        # extra_model_paths.yaml 可以把模型目录搬到别的盘/别的目录：那时 ComfyUI 装在哪
        # 都跟实际模型位置无关了，models/ 下也就没有 llm 子目录。有人在配置里注册过
        # 「llm」这个目录名就优先用注册的路径 —— 提示的目录得是模型真正在的地方。
        try:
            reg = getattr(folder_paths, "folder_names_and_paths", {}).get("llm")
            paths = reg[0] if isinstance(reg[0], (list, tuple)) else [reg[0]]
            extra = [p for p in paths if p and os.path.isdir(str(p))]
            if extra:
                return [str(p) for p in extra]
        except Exception:
            pass
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


def llm_env_info() -> dict:
    """本地通道环境信息：模型目录 + 依赖装没装（设置面板的存放位置/缺件提示用）。

    只做 find_spec / metadata 探测，**不 import torch 这类重货** —— 设置面板每开
    一次就调一回，开销必须是毫秒级。
    """
    import importlib.metadata as _md
    import importlib.util as _util
    deps = {}
    for mod_name, pkg_name in (("llama_cpp", "llama-cpp-python"),
                               ("transformers", "transformers"),
                               ("torch", "torch")):
        try:
            installed = _util.find_spec(mod_name) is not None
        except (ImportError, ValueError):
            installed = False
        version = ""
        if installed:
            try:
                version = _md.version(pkg_name)
            except Exception:
                version = ""
        deps[mod_name] = {"installed": installed, "version": version}
    roots = _llm_roots()
    primary = roots[0] if roots else ""
    # 目录可能压根还没建（models/ 下没有 llm 子目录）→ 路径照给，但带一个
    # exists 标记，前端据此补一句"先手动建这个文件夹"，别让人对着不存在的路径找。
    return {"llm_dir": primary, "llm_dir_exists": bool(primary) and os.path.isdir(primary),
            "llm_dirs": roots, "deps": deps}


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


# 值得重试的瞬时故障状态码：限流与对端 5xx/网关抖动。
# 4xx 一律不重试 —— Key 错、参数错、余额不足，重试多少次结果都一样，只会白等。
_RETRY_STATUS = (408, 425, 429, 500, 502, 503, 504)


def _is_timeout(exc: BaseException) -> bool:
    """识别超时：urlopen 直抛 TimeoutError/socket.timeout，连接阶段则包在 URLError.reason 里。
    Windows 上读超时的文案是 "The read operation timed out"，不认类型也得认文案。"""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(exc).lower()


def _http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 300,
                    retries: int = 2) -> dict:
    """POST JSON 并解析响应。

    超时语义（旧实现直接 `urlopen(timeout=120)` 且不重试）：
    - timeout 是**读+写**的单次上限，由调用方按服务商配置下发；
    - 超时只重试 1 次（一次已经等了 timeout 秒，再等一轮对用户是双重折磨），
      连接类/5xx/429 才重试 retries 次；
    - 超时的报错必须**可行动**：直接告诉用户去哪儿调大、或关掉深度思考，
      而不是把 socket 的英文原文丢给前端。
    """
    data = json.dumps(payload).encode("utf-8")
    last = "LLM 请求失败"
    timeout_retried = False
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json", **(headers or {})}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                status = getattr(resp, "status", 200)
            if status >= 400:
                raise RuntimeError(f"LLM 返回 HTTP {status}：{body[:1000]}")
            try:
                return json.loads(body)
            except ValueError:
                raise RuntimeError("LLM 返回了无效 JSON")
        except RuntimeError:
            raise
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:800]
            except Exception:
                pass
            last = f"LLM 返回 HTTP {e.code}：{detail}"
            if e.code not in _RETRY_STATUS or attempt >= retries:
                raise RuntimeError(last)
        except Exception as e:
            if _is_timeout(e):
                last = (f"LLM 请求超时（{timeout} 秒内未返回）：服务商没有在超时时间内响应。"
                        f"请到「AI 优化设置 → 请求超时」调大（如 600），"
                        f"或关闭「深度思考」；也可以先关掉「读取视觉参考」再试。")
                if timeout_retried or attempt >= retries:
                    raise RuntimeError(last)
                timeout_retried = True
            else:
                last = f"LLM 请求失败（{type(e).__name__}）：{e}"
                if attempt >= retries:
                    raise RuntimeError(last)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last)


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


def _empty_text_error(choice: dict, usage: dict | None) -> RuntimeError:
    """正文为空时的可行动诊断。

    两种成因长得一样（content 都是空串），但处理方式完全不同：
    - **思考吃光预算**：reasoning_content 有内容、finish_reason=length
      → 调大「最大输出 token」或把思考强度降到 low。
    - **服务商就是没给正文**：连 reasoning 都没有
      → 换模型或看 finish_reason。
    统一报"LLM 返回空文本"等于什么都没说，用户只能反复重试。
    """
    reason = str((choice or {}).get("finish_reason") or "?")
    msg = (choice or {}).get("message") or {}
    reasoning = str(msg.get("reasoning_content") or "")
    u = usage or {}
    rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    head = f"LLM 没有产出正文（finish_reason={reason}"
    head += f"，推理 {len(reasoning)} 字" if reasoning else ""
    head += f"，completion_tokens={u.get('completion_tokens')}" if u.get("completion_tokens") else ""
    head += f"，reasoning_tokens={rt}" if rt else ""
    head += "）。"
    if reasoning or (reason == "length"):
        return RuntimeError(
            head + "推理过程很可能吃光了「最大输出 token」—— 思考产生的 token 是计入"
                   "输出上限的。请到「AI 优化设置 → 最大输出 token」调大（如 16384），"
                   "或把「思考强度」降到 low。")
    return RuntimeError(head + "请换一个模型，或检查服务商返回。")


def _api_generate(cfg: dict, system: str, user_prompt: str, media: list, max_tokens: int,
                  temperature: float = 0.2) -> str:
    if not cfg.get("api_key"):
        raise ValueError("未配置 API Key（请在优化设置里填写，或改用本地模型）")
    if not cfg.get("api_url") or not cfg.get("model"):
        raise ValueError("API 地址或模型为空")
    proto = str(cfg.get("protocol") or "openai").lower()
    timeout = int(cfg.get("timeout") or 300)
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
            {"contents": [{"parts": parts}], "generationConfig": {"maxOutputTokens": max_tokens}},
            timeout=timeout)
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
            headers={"Authorization": f"Bearer {cfg['api_key']}"}, timeout=timeout)
        try:
            for item in res.get("output", []):
                for c in item.get("content", []):
                    if c.get("type") in ("output_text", "text") and str(c.get("text") or "").strip():
                        return str(c["text"]).strip()
        except AttributeError:
            pass
        raise RuntimeError("Responses 返回结构异常")
    # 默认 OpenAI 兼容（含 GLM/百炼/SiliconFlow/OpenRouter/RunningHub）
    content2: list = [{"type": "text", "text": user_prompt}]
    for u in images:
        content2.append({"type": "image_url", "image_url": {"url": u}})
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content2}],
        "max_tokens": max_tokens,
    }
    payload.update(_temp_fields(cfg, temperature))
    payload.update(_thinking_fields(cfg))
    res = _http_post_json(_endpoint_for(cfg), payload,
                          headers={"Authorization": f"Bearer {cfg['api_key']}"},
                          timeout=timeout)
    try:
        choice = res["choices"][0]
        text = str(choice["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("LLM 返回结构异常")
    if not text:
        raise _empty_text_error(choice, res.get("usage"))
    return text


def _api_generate_stream(cfg: dict, system: str, user_prompt: str, media: list, max_tokens: int,
                         temperature: float = 0.2, on_progress=None) -> str:
    """OpenAI 兼容 + `stream=true`：边收边报进度，返回完整正文。

    只走 openai 兼容协议（GLM / OpenAI / 百炼 / SiliconFlow / OpenRouter 都支持）；
    gemini 与 responses 两条通道没有流式，调用方要先探测再决定走不走这里。

    为什么单独写一条而不是复用 `_api_generate`：
    1. **真实进度** —— 思考模型的 reasoning 阶段可能几十秒一个字正文都没有，
       没有进度用户只会以为卡死（"缺个进度条"的根因）。
    2. **超时语义更正确** —— urllib 的 timeout 是**单次 socket 操作**上限，
       只要还在持续收数据就不会触发；非流式则是"整包读完"才算一次操作，
       长生成照样会被判超时。
    3. 能拿到 `usage.completion_tokens`（流式最后一帧带），于是进度可以按
       **已用 token / max_tokens** 算真实比例，而不是拍脑袋估时间。
    """
    if not cfg.get("api_key"):
        raise ValueError("未配置 API Key（请在优化设置里填写，或改用本地模型）")
    if not cfg.get("api_url") or not cfg.get("model"):
        raise ValueError("API 地址或模型为空")
    timeout = int(cfg.get("timeout") or 300)
    content2: list = [{"type": "text", "text": user_prompt}]
    for u in _media_images(media):
        content2.append({"type": "image_url", "image_url": {"url": u}})
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content2}],
        "max_tokens": max_tokens,
        "stream": True,
    }
    payload.update(_temp_fields(cfg, temperature))
    payload.update(_thinking_fields(cfg))
    req = urllib.request.Request(
        _endpoint_for(cfg), data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream",
                 "Authorization": f"Bearer {cfg['api_key']}"}, method="POST")

    parts: list = []
    reasons: list = []
    usage: dict = {}
    finish = ""
    t0 = time.monotonic()
    last_emit = 0.0
    emit = on_progress or (lambda ev: None)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:800]
        except Exception:
            pass
        raise RuntimeError(f"LLM 返回 HTTP {e.code}：{detail}")
    except Exception as e:
        if _is_timeout(e):
            raise RuntimeError(
                f"LLM 请求超时（{timeout} 秒内没收到任何数据）：请到「AI 优化设置 → "
                f"请求超时」调大，或降低「思考强度」。")
        raise RuntimeError(f"LLM 请求失败（{type(e).__name__}）：{e}")

    with resp:
        while True:
            raw = resp.readline()          # 逐行读：SSE 每帧一行，读完即到，不缓冲整包
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(obj.get("usage"), dict) and obj["usage"]:
                usage = obj["usage"]
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            rc = delta.get("reasoning_content")
            if rc:
                reasons.append(str(rc))
            c = delta.get("content")
            if c:
                parts.append(str(c))
            if choices[0].get("finish_reason"):
                finish = str(choices[0]["finish_reason"])
            now = time.monotonic()
            if now - last_emit >= 0.2:      # 节流：SSE 一帧报一次会把前端刷爆
                last_emit = now
                emit({"phase": "thinking" if (reasons and not parts) else "writing",
                      "reasoning_chars": sum(len(x) for x in reasons),
                      "content_chars": sum(len(x) for x in parts),
                      "tokens": usage.get("completion_tokens") or 0,
                      "max_tokens": max_tokens,
                      "elapsed": round(now - t0, 1)})

    text = "".join(parts).strip()
    if not text:
        raise _empty_text_error(
            {"finish_reason": finish or "empty",
             "message": {"reasoning_content": "".join(reasons)}}, usage)
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
    """取前 8 张图片 dataURL（与云通道同口径）。"""
    out: list = []
    for item in media or []:
        if not isinstance(item, dict):
            continue
        for u in (item.get("images") or [])[:8]:
            if isinstance(u, str) and u.startswith("data:image"):
                out.append(u)
        if len(out) >= 8:
            break
    return out[:8]


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


def _reclaim_vram() -> None:
    """丢弃句柄后的显存收尾：先 gc（让 llama/torch 的 __del__ 立刻跑），再清分配器缓存。

    TF 路径的显存归 torch 分配器管，empty_cache 直接有效；GGUF 的显存走 ggml 自己的池，
    靠 Llama.close() 归还，torch 这步对它无效（但无害）。
    **不调 llama_backend_free()**：llama-cpp-python 用类级 `__backend_initialized` 把 backend
    锁成「每进程初始化一次」，free 掉之后再次 load 不会重新 init —— 下一次优化必崩。
    """
    gc.collect()
    try:
        import torch  # type: ignore
    except ImportError:
        return
    torch.cuda.empty_cache()


def _strip_think(text: str) -> str:
    """剥掉本地模型内联回来的思维链 —— 云通道不需要这步（思考在 reasoning_content 里）。

    Qwen 系有三种落法，都要认：
      1. `<think>…</think>正文`：标签完整，按最后一个闭合标签切；
      2. `思考正文</think>正文`：起始标签被 chat template 吃掉、只剩闭合标签，同样按它切；
      3. 只有思考、没有闭合标签：思考被 max_tokens 截断、正文还没开始 —— 返回空串，
         由调用方按「没给正文」报错，绝不把半段思考当提示词填进编辑器。
    """
    raw = str(text or "").strip()
    raw = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", raw, flags=re.S).strip()
    ends = [m.end() for m in re.finditer(r"</think\s*>", raw)]
    if ends:
        return raw[ends[-1]:].strip()
    if "<think" in raw:
        return ""
    return raw


# ============================ 本地通道观测 ============================
#
# 为什么必须单独立这一块：提示词优化走本地 GGUF 时，「加速到底生效没有」和
# 「显存用了多少」原本**完全不可见**——
#
# - llama.cpp 用自己的 CUDA 分配器，`torch.cuda.memory_allocated()` 看不见它，
#   只有设备级 `total − free`（`mem_get_info`）才量得到；
# - `n_gpu_layers=-1` 只是**请求**全铺：放不下时 llama.cpp 不报错、只是把层
#   丢给 CPU，decode 从 ~25 tok/s 掉到个位数，**日志里什么都不会说**；
# - `_new_llama` 的降级分支只在失败时吭一声，成功时零输出。
#
# docs/本地LLM保守加速提案_3090.md §2 早就写了验收标准
# （「看到 offloaded X/Y layers to GPU 时 X 必须等于 Y」），只是一直没落到代码里。
# 下面前三个函数补齐「可测的那一半」；想拿 llama.cpp 那句原始凭据时设
# 环境变量 `H3_LLM_VERBOSE=1`（它会把装载日志原样打出来）。

LOCAL_LLM_VERBOSE_ENV = "H3_LLM_VERBOSE"


def _vram_used_gb():
    """设备级显存占用（GB）；无 CUDA / 异常 → None。

    测 llama.cpp 的唯一可用口径：它的分配不属于 torch 的 caching allocator，
    `torch.cuda.memory_allocated()` 会读出接近 0 的假象。
    """
    try:
        import torch  # type: ignore

        if not torch.cuda.is_available():
            return None
        free, total = torch.cuda.mem_get_info()
        return (total - free) / (1024 ** 3)
    except Exception:
        return None


def _file_gb(path):
    """模型文件大小（GB）；拿不到 → None。用于对照「铺了多少」。"""
    try:
        return os.path.getsize(path) / (1024 ** 3)
    except Exception:
        return None


def _plan_line(plan):
    """加速项生效清单（一行）；无 plan → ""。

    `plan` 由 `_new_llama` 回传，记录**这次实际吃到**的项（降级后是剥掉之后的）。
    """
    if not plan:
        return ""
    ok = "√" if plan.get("flash_attn") else "×"
    kv = "q8_0（省一半）" if plan.get("kv_quant") else "fp16（未量化）"
    ub = plan.get("n_ubatch")
    ngl = plan.get("n_gpu_layers")
    if ngl == -1:
        ngl_txt = "n_gpu_layers=-1（请求全铺，能否铺满由显存决定）"
    elif not ngl:
        ngl_txt = "未设 n_gpu_layers（= 纯 CPU）"
    else:
        ngl_txt = f"n_gpu_layers={ngl}"
    txt = (f"加速项：flash_attn {ok} · KV {kv}"
           f" · n_batch {plan.get('n_batch')} · n_ubatch {ub or '默认'} · {ngl_txt}")
    if plan.get("mode") == "degraded":
        txt += (f" ⚠ 已降级（被剥掉 {'/'.join(plan.get('dropped') or [])}）")
    return txt


def _llm_verdict(model_gb, vram_delta_gb, tok_s, plan):
    """按实测数字给结论行（纯函数，可单测；没有可说的返回 []）。

    三条判据都刻意保守——只报**能确定**的：
    1. 降级：功能层面已确知（plan 记着），必报；
    2. 显存增量 << 权重文件：mmap 下铺满 GPU 时增量应 ≥ 文件大小（还要叠 KV），
       明显小于 → 有层在 CPU；阈值取 0.8 倍，给量化打包与 mmap 留余量；
    3. tok/s 过低：同档位正常 20–30，个位数就是掉层/降级的典型表现。
    """
    out = []
    if plan and plan.get("mode") == "degraded":
        out.append("⚠ 加速项被降级（这条 llama-cpp-python 不认新参数）：KV 回 fp16 "
                   "→ 显存翻倍，27B 级模型在 24G 卡上会掉层。建议升级 llama-cpp-python")
    if model_gb and vram_delta_gb is not None and vram_delta_gb < model_gb * 0.8:
        out.append(f"⚠ 显存增量 {vram_delta_gb:.1f}GB 明显小于权重文件 {model_gb:.1f}GB "
                   f"——有层被丢在 CPU（llama.cpp 不报错，只是变慢）。"
                   f"可减小 n_ctx、不挂 mmproj，或换更小的量化")
    if tok_s is not None and tok_s < 10:
        out.append(f"⚠ 生成只有 {tok_s:.1f} tok/s（同档位正常约 20–30）"
                   "——典型是掉层或降级后的表现")
    return out


def _new_llama(kw: dict):
    """构造 Llama，带一次「新参数不被老轮子认识」的降级重试。

    flash_attn / type_k / type_v / n_ubatch 都是较新 llama-cpp-python 才有的构造
    参数：老轮子（或没编 FA 的 CUDA 轮子 + 量化 KV 的组合）会在构造时直接抛异常。
    第一次带满加速参数，失败就剥掉它们重试一次 —— 能吃到多少算多少，
    绝不因为加速项把整次生成弄挂。

    降级要**打到日志里**：KV 回到 fp16 意味着显存翻倍，27B 在 24G 卡上就 spill
    到内存了，速度掉一个数量级 —— 这种"能跑但慢十倍"的状态必须看得见，
    否则用户只会觉得"这个插件好慢"，根本查不到是加速项没生效。

    返回 `(llm, plan)`：`plan` 记着**这次实际吃到了哪些项**（降级后是剥掉之后的），
    供 `_plan_line` / `_llm_verdict` 渲染。成功路径原来零输出，只有失败才吭声，
    于是"加速生效了没"永远无从判断——这份 plan 就是补这个洞。
    """
    from llama_cpp import Llama  # type: ignore
    fast_keys = ("flash_attn", "type_k", "type_v", "n_ubatch")
    plan = {"mode": "fast",
            "dropped": [],
            "flash_attn": bool(kw.get("flash_attn")),
            "kv_quant": ("type_k" in kw and "type_v" in kw),
            "n_batch": kw.get("n_batch"),
            "n_ubatch": kw.get("n_ubatch"),
            "n_gpu_layers": kw.get("n_gpu_layers")}
    try:
        return Llama(**kw), plan
    except Exception:
        if not any(k in kw for k in fast_keys):
            raise
        slow = {k: v for k, v in kw.items() if k not in fast_keys}
        # 老轮子的 n_batch 同时充当物理批，2048 会按最坏情况撑爆计算缓冲区
        slow["n_batch"] = 512
        plan.update({"mode": "degraded",
                     "dropped": [k for k in fast_keys if k in kw],
                     "flash_attn": False, "kv_quant": False,
                     "n_batch": 512, "n_ubatch": None})
        print("[ComfyUI_H3_SeamlessChain] llama-cpp-python 不认本插件的加速参数，已降级运行："
              "关掉 flash attention、KV 缓存退回 fp16（显存占用翻倍，27B 在 24G 卡上会掉层到内存，"
              "速度慢一个数量级）。建议升级 llama-cpp-python 到较新版本")
        return Llama(**slow), plan


def _torch_bf16_ok(torch_mod) -> bool:
    """这张卡能不能跑 bf16（Ampere / sm_80 及以上才有硬件 bf16）。

    20 系及更早没有 bf16 单元：硬上会报错或退化成慢速模拟，那种卡只能回 fp16。
    """
    try:
        if not torch_mod.cuda.is_available():
            return False
        checker = getattr(torch_mod.cuda, "is_bf16_supported", None)
        return True if checker is None else bool(checker())
    except Exception:
        return False


def _pick_hf_dtype(torch_mod, config_dtype, device: str):
    """挑推理精度：**不硬写死**，优先照模型自己 config 标的来。

    为什么不能一律 fp16：这批 VL 模型（Qwen-VL / Gemma 系）是 bf16 训练的，
    激活值超出 fp16 范围会直接溢出 —— 表现是输出乱码/重复/空，不是悄悄变差。
    反过来也不能一律 bf16：bf16 要 Ampere 以上，老卡没有硬件支持，只能回落 fp16。
    """
    if device == "cpu":
        return torch_mod.float32          # CPU 上 bf16 没收益，fp32 更稳
    want = str(config_dtype or "").lower()
    if "bfloat16" in want:
        return torch_mod.bfloat16 if _torch_bf16_ok(torch_mod) else torch_mod.float16
    if "float16" in want:
        return torch_mod.float16
    # config 没标（或标了 float32）：按 bf16 走（当前主流训练精度），老卡回落 fp16
    return torch_mod.bfloat16 if _torch_bf16_ok(torch_mod) else torch_mod.float16


# ---- 思考（thinking）能力档案 -------------------------------------------------
#
# 只维护三家（智谱 / GPT / DeepSeek），其余一律「未知」→ 不下发任何思考字段，
# 交给服务商默认。理由：模型名是手填的，名单穷举不完；对不认识的型号发自家
# 私有字段只会换来 400。三家之外的"默认开还是关"由服务商决定，UI 会写明。
#
# 各家 2026-09 现状（联网核实，别按训练数据写）：
#   GLM    私有 thinking.type + reasoning_effort(low/high/max)，部分型号强制思考
#   GPT    OpenAI 的 reasoning_effort
#            gpt-6-astra：low/medium/high/xhigh/max —— **没有 none，关不掉**（强制）
#            gpt-5.6-sol|terra|luna：none/low/medium/high/xhigh/max，默认 none
#            gpt-5.1 及更早：默认 medium，不支持 none
#            ⚠ GPT-6 及以后不支持 temperature / top_p —— 见 _temp_fields()
#   DeepSeek  deepseek-flash（V4.1 Flash）/ deepseek-v4-pro
#            思考**默认开、默认 high**；档位 low/high/max（OpenAI 风格档名就近映射）
#            开关是 thinking.type，强度用 reasoning_effort
THINK_LEVELS = ("off", "low", "medium", "high", "max")

# 型号前缀 → (默认档, 支持的档位, 能否关闭)
_GPT_EFFORT = {
    "gpt-6": ("medium", ("low", "medium", "high", "xhigh", "max"), False),
    "gpt-5.6": ("none", ("none", "low", "medium", "high", "xhigh", "max"), True),
}
_GPT_MAP = {"off": "none", "low": "low", "medium": "medium", "high": "high", "max": "max"}
# DeepSeek 档位比我们少：medium/high 都落到 high（官方映射表：medium/high/xhigh→high）
_DS_MAP = {"low": "low", "medium": "high", "high": "high", "max": "max"}
# GLM 只有三档：medium 就近取 high（三档里的中间档）
_GLM_MAP = {"low": "low", "medium": "high", "high": "high", "max": "max"}


def _model_base(model: str) -> str:
    """取型号名本体：OpenRouter 之类会写成 `openai/gpt-5.6`。"""
    return str(model or "").split("/")[-1].strip().lower()


def _thinking_family(cfg: dict) -> str:
    """判断当前端点属于哪家：glm / deepseek / gpt / unknown（unknown 一律不干预）。"""
    url = str(cfg.get("api_url") or "").lower()
    model = str(cfg.get("model") or "").lower()
    if any(h in url for h in _GLM_HOSTS):
        return "glm"
    if "deepseek" in url or model.startswith("deepseek"):
        return "deepseek"
    base = _model_base(model)
    # 只认官方域名或明确的 GPT 型号名 —— 不做宽松子串匹配：有些中转/代理地址里
    # 带 "openai" 字样，但后面接的是别家模型，给它发 reasoning_effort 就是 400。
    if "openai.com" in url or base.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "gpt"
    return "unknown"


def _cfg_think_level(cfg: dict):
    """内部统一档位 off/low/medium/high/max；None = 不干预（auto）。

    兼容旧的两个字段（thinking 开关 + reasoning_effort 强度）：没给新字段时按旧
    字段推导，老配置不用迁移。
    """
    lv = str(cfg.get("thinking_level") or "").lower()
    if lv in THINK_LEVELS:
        return lv
    mode = str(cfg.get("thinking") or "auto").lower()
    if mode == "auto":
        return None                  # 什么都不下发，交给服务商默认
    effort = str(cfg.get("reasoning_effort") or "").lower()
    # 显式强度优先于开关：历史配置里有「关闭 + 指定强度」这种组合（强度才是真正
    # 被用户挑过的那一项），按旧口径让强度说话。
    mapped = {"low": "low", "high": "high", "max": "max"}.get(effort)
    if mapped:
        return mapped
    return "off" if mode == "disabled" else "high"


def _gpt_thinking_fields(cfg: dict) -> dict:
    """OpenAI 系：只发标准的 reasoning_effort，不发任何私有字段。"""
    base = _model_base(cfg.get("model"))
    prof = None
    for prefix, value in _GPT_EFFORT.items():
        if base.startswith(prefix):
            prof = value
            break
    if prof is None:
        # 更早的 gpt-5.1/5/5-mini 等：官方口径「默认 medium，不支持 none」。
        # 档位只取各家都认的那三个，别去猜 minimal 之类的边界值。
        if not base.startswith(("gpt-", "o1", "o3", "o4")):
            return {}
        prof = ("medium", ("low", "medium", "high"), False)
    _default, values, can_off = prof
    lv = _cfg_think_level(cfg)
    if lv is None:
        return {}
    want = _GPT_MAP.get(lv)
    if lv == "off" and not can_off:
        want = "low"                 # gpt-6 关不掉：按"少想点"的意图降到最低档
    if want not in values:
        want = values[0] if lv == "off" else values[-1]
    return {"reasoning_effort": want}


def _deepseek_thinking_fields(cfg: dict) -> dict:
    """DeepSeek：开关用 thinking.type，强度用 reasoning_effort（low/high/max）。"""
    lv = _cfg_think_level(cfg)
    if lv is None:
        return {}
    if lv == "off":
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "enabled"},
            "reasoning_effort": _DS_MAP.get(lv, "high")}


def _glm_thinking_fields(cfg: dict) -> dict:
    """智谱系专有的 `thinking` / `reasoning_effort` 字段。非智谱端点返回 {}。

    四条规则，都是被实测打出来的：
    1. `thinking` 是智谱私有字段，塞进 OpenAI/百炼的请求体直接 400 → 非智谱不下发。
    2. **「始终思考」型号不能收 `disabled`**（实测 400 / code 1210：
       「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」）。
       用户选了「关闭」时，把它**翻译成最低强度 `low`** —— 用户的意图是"别想太久、
       快点出结果"，`low` 才是这个意图在强制思考模型上的对应物。
       什么都不发是不行的：那等于让服务商用默认值（通常是 `max`，最慢）。
    3. `reasoning_effort` 只在 thinking 没被真正关掉时有意义。
    4. 模型不支持 effort 时不发 —— 换个不认识这字段的型号整个请求就废了。
    """
    url = str(cfg.get("api_url") or "").lower()
    if not any(h in url for h in _GLM_HOSTS):
        return {}
    model = str(cfg.get("model") or "")
    forced = _is_forced_thinking(model)
    supports = _supports_effort(model)
    out: dict = {}
    lv = _cfg_think_level(cfg)
    if lv is None:
        # auto：沿用旧逻辑（开关字段优先），不下发 reasoning_effort
        mode = str(cfg.get("thinking") or "auto").lower()
        if mode == "disabled" and not forced:
            out["thinking"] = {"type": "disabled"}
        elif mode == "enabled":
            out["thinking"] = {"type": "enabled"}
        return out
    # 「按的是关闭」这条链路（新字段 off，或旧字段 thinking=disabled）一律**不下发
    # thinking.type**：强制思考型号没有"关"这个概念，传进去是多余的一份风险，
    # 只把意图翻译成强度（用户真挑过强度时以强度为准）。
    legacy_off = (not str(cfg.get("thinking_level") or "").strip()
                  and str(cfg.get("thinking") or "auto").lower() == "disabled")
    if lv == "off" or legacy_off:
        if not forced:
            return {"thinking": {"type": "disabled"}}
        return {"reasoning_effort": _GLM_MAP.get(lv, "low")} if supports else {}
    out["thinking"] = {"type": "enabled"}
    if supports:
        out["reasoning_effort"] = _GLM_MAP.get(lv, "high")
    return out


def _thinking_fields(cfg: dict) -> dict:
    """按端点的能力档案下发思考字段。**未知型号一律返回 {}（不干预）。**"""
    fam = _thinking_family(cfg)
    if fam == "glm":
        return _glm_thinking_fields(cfg)
    if fam == "gpt":
        return _gpt_thinking_fields(cfg)
    if fam == "deepseek":
        return _deepseek_thinking_fields(cfg)
    return {}


def _temp_fields(cfg: dict, temperature: float) -> dict:
    """温度字段：GPT-6 及以后**不接受 temperature**（官方：不支持 temperature/
    top_p/logprobs），传了就是 400。其它家照旧。

    DeepSeek 在思考模式下是"忽略不报错"，不用特殊处理；GLM 照旧。
    """
    base = _model_base(cfg.get("model"))
    if _thinking_family(cfg) == "gpt" and base.startswith("gpt-6"):
        return {}
    return {"temperature": _temp(temperature)}


def _thinking_caps(cfg: dict) -> dict:
    """给前端的能力提示：这家能不能关、有几档、默认是什么、未知型号怎么说明。"""
    fam = _thinking_family(cfg)
    if fam == "unknown":
        return {"family": "unknown", "known": False, "levels": [],
                "note": "型号不在已知名单（GLM / GPT / DeepSeek），不干预思考，"
                        "由服务商默认决定（思考型模型多半是开的）"}
    if fam == "glm":
        return {"family": "glm", "known": True, "levels": list(THINK_LEVELS),
                "forced": _is_forced_thinking(cfg.get("model") or ""),
                "note": ""}
    if fam == "gpt":
        base = _model_base(cfg.get("model"))
        for prefix, (_d, values, can_off) in _GPT_EFFORT.items():
            if base.startswith(prefix):
                return {"family": "gpt", "known": True, "levels": list(THINK_LEVELS),
                        "forced": not can_off,
                        "note": ("该型号不能关闭思考，已按最低档处理" if not can_off else "")}
        return {"family": "gpt", "known": True, "levels": ["low", "medium", "high"],
                "forced": True, "note": "较早期的 GPT 思考型号：不能关闭，只调强度"}
    return {"family": "deepseek", "known": True, "levels": list(THINK_LEVELS),
            "forced": False, "note": "DeepSeek 默认开思考（high），可关闭"}


def _local_generate(cfg: dict, system: str, user_prompt: str, media: list,
                    temperature: float = 0.2) -> str:
    """本地视觉模型推理（GGUF / Transformers 两路）。

    **用完即卸**：本次调用一结束就关掉模型句柄、归还显存（不再进程内缓存句柄）。
    27B 级权重常驻会把后续 H3 采样的显存吃掉，而 ComfyUI 的 unload_all_models()
    管不到 llama.cpp（ggml 自己的池）。代价是每次优化都要重新加载模型 —— 取显存。
    """
    sel = str(cfg.get("local_model") or "").strip()
    if not sel:
        raise ValueError("请先在优化设置里选择本地视觉模型")
    images = _media_images(media) if cfg.get("read_media") else []
    root = (_llm_roots() or [""])[0]
    # 本地通道没有 API 那种 thinking 字段，Qwen 系的软开关就是往输入里塞 /no_think。
    # 不塞的话它默认长思考，把正文预算吃光（实测 3100 字思考 / 1100 字正文）。
    if str(cfg.get("thinking") or "disabled").lower() == "disabled":
        user_prompt = user_prompt.rstrip() + "\n/no_think"

    try:
        if sel.lower().endswith(".gguf"):
            # ---- GGUF 路径（llama-cpp-python + mmproj 视觉投影）----
            try:
                import llama_cpp  # noqa: F401  （GGML_TYPE_Q8_0 常量探测用）
                from llama_cpp import Llama  # noqa: F401  （探测「装没装」的哨兵）
                from llama_cpp.llama_chat_format import Llava15ChatHandler  # type: ignore
            except ImportError:
                raise ValueError(
                    "未安装 llama-cpp-python，无法加载 GGUF 本地模型。安装方法"
                    "（必须装进 ComfyUI 用的那个 Python 环境）：\n"
                    "· NVIDIA 显卡（默认 cu130 = CUDA 13.0）：pip install llama-cpp-python "
                    "--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu130\n"
                    "  驱动/工具链较老就往下换档：cu125（CUDA 12.5，12.x 线最高档）/ "
                    "cu124 / cu123 / cu122 / cu121 / cu118\n"
                    "· 纯 CPU：pip install llama-cpp-python\n"
                    "两个坑：预编译轮子只给 Python 3.10/3.11/3.12（3.13 及以上要从源码编）；"
                    "CUDA 13 轮子要求显卡算力 7.5 以上（3090 是 8.6，可用）\n"
                    "项目主页：https://github.com/abetlen/llama-cpp-python")
            mmproj = str(cfg.get("local_mmproj") or "").strip()
            if mmproj and not os.path.isfile(os.path.join(root, mmproj)):
                raise ValueError(f"视觉投影文件不存在：{mmproj}"
                                 "（请确认已放进本地模型目录，再回设置里点「刷新模型」）")
            budget = int(cfg.get("max_tokens") or 8192)
            # n_ctx 必须容得下「提示词 + 生成」：旧值写死 8192，而 max_tokens 默认 16384
            # → 生成本身就大于窗口，思考刚写完就被截断。多留 8192 给系统提示+规则文件+图。
            n_ctx = (max(8192, budget + 8192) + 511) // 512 * 512
            # 通用加速项（全部是 llama-cpp-python 自带参数，不引新依赖）：
            # - flash_attn：显存与长上下文速度双赢；
            # - type_k/type_v=q8_0：KV 缓存显存减半 —— 24G 卡上 27B 能不能整个
            #   塞进显存（塞不下会静默掉层到 CPU，速度掉一个数量级）的胜负手。
            #   llama.cpp 要求 V 量化必须开 FA，所以两个都只在 FA 请求发出时带上；
            # - n_batch / n_ubatch：n_batch 是「一次喂多少 token」，加到 2048 给长系统
            #   提示 + 规则文件的 prefill 提速；n_ubatch 是「物理微批」，**必须单独压到
            #   512** —— llama.cpp 按微批的最坏情况撑开计算缓冲区，2048 的物理批会凭
            #   空吃掉 1~2G 显存，正好是 27B 挤不上 24G 卡的那部分。
            kw = {"model_path": os.path.join(root, sel), "n_ctx": n_ctx,
                  "verbose": False, "n_batch": 2048, "n_ubatch": 512,
                  "flash_attn": True}
            q8 = getattr(llama_cpp, "GGML_TYPE_Q8_0", None)
            if q8 is not None:
                kw["type_k"] = q8
                kw["type_v"] = q8
            if mmproj:
                # mproj 走 GPU 加速（视觉塔比语言模型轻，显存占用小）
                kw["chat_handler"] = Llava15ChatHandler(
                    clip_model_path=os.path.join(root, mmproj), verbose=False)
            # auto 也算 GPU：llama-cpp-python 没有「自动铺层」这回事，不设 n_gpu_layers
            # 就是 0 层 = 纯 CPU，27B 在 CPU 上出 1000 字要按分钟算。
            if str(cfg.get("local_device") or "cuda").lower() != "cpu":
                kw["n_gpu_layers"] = -1
            if os.environ.get(LOCAL_LLM_VERBOSE_ENV) == "1":
                # 想看 llama.cpp 自己那句「offloaded X/Y layers to GPU」的原始凭据
                # （提案 §2 的验收标准）就开它；平时关着——否则每次优化都刷几百行
                # 张量装载日志。开了之后 X≠Y 就是掉层的铁证。
                kw["verbose"] = True
            _model_path = os.path.join(root, sel)
            _model_gb = _file_gb(_model_path)
            _vram0 = _vram_used_gb()
            _t_load = time.perf_counter()
            llm, _plan = _new_llama(kw)
            _load_s = time.perf_counter() - _t_load
            _vram1 = _vram_used_gb()
            content: list = [{"type": "text", "text": user_prompt}]
            for u in images:
                content.append({"type": "image_url", "image_url": {"url": u}})
            _t_gen = time.perf_counter()
            try:
                res = llm.create_chat_completion(
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": content}],
                    max_tokens=budget, temperature=_temp(temperature))
            finally:
                # 显存靠它归还（同 __del__，但此刻立刻生效）：生成报错时也不能漏关，
                # 否则句柄会挂在线程池 future 的 traceback 上，一直到那次异常被回收。
                llm.close()
            _gen_s = time.perf_counter() - _t_gen
            _vram2 = _vram_used_gb()
            # 本地通道的性能与显存账：**加不加这行差别很大**——llama.cpp 用自己的
            # CUDA 分配器（torch 计数器看不见）、掉层又是静默的，不打出来就只剩
            # 「感觉这个插件好慢」这一种反馈。
            try:
                _u = (res or {}).get("usage") or {}
                _ct = _u.get("completion_tokens")
                _pt = _u.get("prompt_tokens")
                _tok_s = (float(_ct) / _gen_s) if (_ct and _gen_s > 0) else None
                _delta = (None if (_vram1 is None or _vram0 is None)
                          else _vram1 - _vram0)
                _line1 = (f"[本地LLM] {sel} · 权重文件 "
                          f"{'?' if _model_gb is None else f'{_model_gb:.1f}GB'}"
                          f" · 装载 {_load_s:.1f}s"
                          + ("" if _delta is None else
                             f" · 装载前后显存 {_vram0:.1f} → {_vram1:.1f}GB"
                             f"（+{_delta:.1f}）"))
                _line2 = (f"[本地LLM] 生成 {_gen_s:.1f}s · prompt "
                          f"{_pt if _pt is not None else '?'} tok · 输出 "
                          f"{_ct if _ct is not None else '?'} tok"
                          + ("" if _tok_s is None else f" · {_tok_s:.1f} tok/s"))
                _line3 = _plan_line(_plan)
                _line4 = ("" if (_vram2 is None or _vram1 is None) else
                          f"[本地LLM] 关闭后显存 {_vram1:.1f} → {_vram2:.1f}GB"
                          f"（已归还 {_vram1 - _vram2:.1f}GB）")
                for _ln in (_line1, _line2, _line3, _line4):
                    if _ln:
                        print(_ln, flush=True)
                for _w in _llm_verdict(_model_gb, _delta, _tok_s, _plan):
                    print(f"[本地LLM] {_w}", flush=True)
                try:   # 落盘：抓崩时的唯一留存（与 perf 的其它埋点同一份 JSONL）
                    from . import perf as _perf

                    _perf.emit({"kind": "local_llm", "model": sel,
                                "model_gb": _model_gb,
                                "vram_before": _vram0, "vram_after_load": _vram1,
                                "vram_after_close": _vram2, "vram_delta": _delta,
                                "load_s": round(_load_s, 2),
                                "gen_s": round(_gen_s, 2),
                                "prompt_tokens": _pt, "completion_tokens": _ct,
                                "tok_s": None if _tok_s is None else round(_tok_s, 2),
                                "plan": _plan})
                except Exception:
                    pass
            except Exception:
                pass
            raw = str(res["choices"][0]["message"]["content"] or "")
        else:
            # ---- Transformers 路径 ----
            try:
                import torch  # type: ignore
                from transformers import AutoModelForImageTextToText, AutoProcessor  # type: ignore
            except ImportError:
                raise ValueError("未安装 transformers/torch，无法加载本地视觉模型。"
                                 "请在 ComfyUI 的 Python 环境里执行："
                                 "pip install torch transformers accelerate"
                                 "（或改用云 API / 换 GGUF 模型）")
            path = os.path.join(root, sel)
            # 设备归一：设置里的 "auto" 要在这里落地成具体设备（device_map 认 "auto"，
            # 但 .to(torch.device("auto")) 会直接抛错）。
            device = str(cfg.get("local_device") or "cuda").lower()
            dev = device if device in ("cuda", "cpu", "mps") else (
                "cuda" if torch.cuda.is_available() else "cpu")
            # 精度：读模型 config 自己标的 torch_dtype，别硬写死 —— 见 _pick_hf_dtype
            try:
                from transformers import AutoConfig  # type: ignore
                cfg_dtype = getattr(
                    AutoConfig.from_pretrained(path, trust_remote_code=True),
                    "torch_dtype", None)
            except Exception:
                cfg_dtype = None
            dtype = _pick_hf_dtype(torch, cfg_dtype, dev)
            processor = AutoProcessor.from_pretrained(path, trust_remote_code=True)
            kw = {"dtype": dtype, "trust_remote_code": True}
            # device_map 走 accelerate 的「边读边搬」：大模型不必先在内存里整份摊开。
            # 但它传字符串必须装了 accelerate —— 没装就退到 .to()（能跑，代价是内存
            # 峰值等于整份权重，32B bf16 ≈ 64G），所以 .to() 只是兜底，不是首选。
            try:
                import accelerate  # noqa: F401  （只探测在不在，不用它的 API）
                kw["device_map"] = dev
            except ImportError:
                pass
            # sdpa 是 torch 自带的高性能注意力（不用装 flash-attn），算的是**精确**
            # 注意力，但不像 eager 那样摊开 N×N 的大矩阵（8000 token 就是几 GB）。
            # 极少数模型/老 transformers 不认这个参数 → 剥掉重试一次。
            try:
                model = AutoModelForImageTextToText.from_pretrained(
                    path, attn_implementation="sdpa", **kw)
            except Exception:
                model = AutoModelForImageTextToText.from_pretrained(path, **kw)
            if "device_map" not in kw:
                model = model.to(torch.device(dev))
            model.eval()
            content = [{"type": "image"} for _ in images] + [{"type": "text", "text": user_prompt}]
            messages = [{"role": "system", "content": [{"type": "text", "text": system}]},
                        {"role": "user", "content": content}]
            # tokenize=False 才是「可读的 prompt 文本」；默认 True 返回 input_ids，
            # 再喂 processor(text=...) 等于把 id 列表当字符串用。
            prompt = processor.apply_chat_template(messages, tokenize=False,
                                                   add_generation_prompt=True)
            inputs = processor(text=prompt, images=[_b64_to_pil(u) for u in images] or None,
                               return_tensors="pt").to(model.device)
            # 与 GGUF 路径同口径：温度 > 0 才采样，否则贪心（温度 0 传进 HF 会直接报错）
            gen_kw = ({"do_sample": True, "temperature": temperature} if temperature > 0
                      else {"do_sample": False})
            with torch.inference_mode():
                out = model.generate(**inputs,
                                     max_new_tokens=int(cfg.get("max_tokens") or 4096),
                                     **gen_kw)
            gen = out[:, inputs["input_ids"].shape[1]:]
            raw = processor.batch_decode(gen, skip_special_tokens=True)[0]
            del model, processor, inputs, out, gen
    finally:
        _reclaim_vram()

    text = _strip_think(raw)
    if not text:
        raise RuntimeError("本地模型只输出了思维链、正文被截断（max_tokens 被思考吃光）："
                           "请把「最大生成」调大，或把「思考强度」设为关闭后重试")
    return text


def generate_text(config_in: dict | None, system: str, user_prompt: str,
                  media: list | None = None, max_tokens: int | None = None,
                  temperature: float | None = None, on_progress=None) -> str:
    """通用文本生成入口：复用本模块的三协议云通道 + 本地通道。

    供 h3_prompt_expander 等上层工具调用，避免各自维护一套 HTTP 客户端。
    调用方负责拼 system/user；本函数只负责"把请求发出去并取回文本"。

    - 云通道：_api_generate（自动按 protocol 走 openai 兼容 / gemini / responses）
    - 本地通道：_local_generate（Transformers / GGUF）

    on_progress：给了就走**流式**（只有 openai 兼容协议有），边收边回调，
    上层据此画进度条（扩写阶段也要进度，否则那 20~40 秒是黑箱）。
    """
    cfg = normalize_config(config_in)
    system = str(system or "")
    user_prompt = str(user_prompt or "")
    if not user_prompt.strip():
        raise ValueError("待生成内容为空")
    if max_tokens is not None:
        try:
            # 上限必须与 normalize_config 一致（32768）。这里写 8192 是个陷阱：
            # 调用方传 16384 会被静默砍半，思考模型的推理就够吃光。
            cfg["max_tokens"] = max(512, min(32768, int(max_tokens)))
        except (TypeError, ValueError):
            pass
    media = media if isinstance(media, list) else []
    temp = _temp(temperature)
    if cfg.get("mode") == "local":
        return _local_generate(cfg, system, user_prompt, media, temp)
    budget = int(cfg.get("max_tokens") or 8192)
    if on_progress and str(cfg.get("protocol") or "openai") == "openai":
        return _api_generate_stream(cfg, system, user_prompt, media, budget, temp,
                                    on_progress=on_progress)
    return _api_generate(cfg, system, user_prompt, media, budget, temp)


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


def optimize_once(config_in: dict | None, payload: dict | None, on_progress=None) -> str:
    """单次优化入口（同步，调用方放线程池）。返回优化后文本。

    on_progress(ev)：可选进度回调，**只有在 openai 兼容协议 + 云通道**下才生效
    （走流式）。ev 形如
        {"phase": "thinking"|"writing", "reasoning_chars": n, "content_chars": m,
         "tokens": k, "max_tokens": N, "elapsed": 秒}
    其它协议（gemini/responses）与本地模型没有流式，静默退化成"无进度"，
    调用方要自己显示不确定态进度条。"""
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
    max_tokens = int(cfg.get("max_tokens") or 4096)
    temp = _temp(payload.get("temperature"))
    # 要进度就走流式。只有 openai 兼容协议有 SSE；gemini/responses 退回整包模式，
    # 前端拿不到进度事件就会显示不确定态进度条（不会卡住，只是没有百分比）。
    if on_progress and str(cfg.get("protocol") or "openai").lower() == "openai":
        return _api_generate_stream(cfg, system, user_prompt, media, max_tokens, temp, on_progress)
    return _api_generate(cfg, system, user_prompt, media, max_tokens, temp)


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


def optimize_multi_once(config_in: dict | None, payload: dict | None,
                        on_progress=None) -> dict:
    """多段一次性优化：把 N 段剧本（自由格式）逐段压成 H3 官方格式并校验。

    payload:
      segments: [{prompt, seconds, task, media?}]  —— prompt 是该段剧本正文
      task / media: 缺省值（各段未给时用它）
    返回 {ok, segments:[{index, seconds, task, result, errors, warnings, ok}], meta}
    ok = 所有非空段都通过官方校验（空段跳过不计）。

    on_progress(ev)：可选进度回调。**逐段转发 `optimize_once` 的流式事件，
    并补上段序号**，于是前端能画「第 i/N 段」的整条进度：
        {"seg": 原始下标, "seg_no": 第几段(1 起，只数非空段), "total": 非空段总数,
         "phase": "thinking"|"writing", "reasoning_chars": n, "content_chars": m,
         "tokens": k, "max_tokens": N, "elapsed": 秒}
    没有流式的通道（gemini/responses/本地模型）不会产生任何事件 ——
    调用方要自己显示不确定态，别把"没有帧"当成失败。
    """
    cfg = normalize_config(config_in)
    payload = payload if isinstance(payload, dict) else {}
    segs = payload.get("segments")
    if not isinstance(segs, list) or not segs:
        raise ValueError("segments 为空：至少要给一段剧本")
    if len(segs) > 24:
        raise ValueError(f"段数 {len(segs)} 过多，一次最多 24 段")
    shared_media = payload.get("media") if isinstance(payload.get("media"), list) else []
    # 先数一遍非空段：前端要显示「第 i/N 段」，分母必须是**真正要跑的段数**，
    # 不能拿 len(segs) —— 空段会被跳过，分母虚高则进度永远到不了 100%。
    total = sum(1 for s in segs
                if str((s if isinstance(s, dict) else {}).get("prompt") or "").strip())
    seg_no = 0
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
        seg_no += 1

        # 用默认参数把 i / seg_no 钉住：闭包直接引用循环变量的话，
        # 所有回调拿到的都会是最后一段的值（经典坑）。
        def _relay(ev, _i=i, _no=seg_no):
            if not on_progress:
                return
            e = dict(ev) if isinstance(ev, dict) else {}
            e["seg"] = _i
            e["seg_no"] = _no
            e["total"] = total
            on_progress(e)

        text = optimize_once(cfg, {
            "prompt": script, "task": task, "duration": seconds,
            "media": s.get("media") if isinstance(s.get("media"), list) else shared_media,
            "context": {"main_mode": task},
        }, on_progress=_relay if on_progress else None)
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
