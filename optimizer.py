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
    mode = str(cfg.get("thinking") or "auto").lower()
    effort = str(cfg.get("reasoning_effort") or "").lower()
    forced = _is_forced_thinking(model)
    supports = _supports_effort(model)
    out: dict = {}
    if mode == "disabled":
        if not forced:
            out["thinking"] = {"type": "disabled"}
        elif supports:
            out["reasoning_effort"] = "low"
    elif mode == "enabled":
        out["thinking"] = {"type": "enabled"}
    # auto（或强制思考型号被降级）→ 不下发 thinking，交给服务商默认
    # 显式指定的强度永远优先于上面的翻译
    if effort in GLM_EFFORT_VALUES and supports \
            and out.get("thinking", {}).get("type") != "disabled":
        out["reasoning_effort"] = effort
    return out


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

# 本地模型句柄进程内缓存：同一模型只加载一次（加载一次几十秒，反复加载会拖死节点）
_GGUF_CACHE: dict = {}
_TF_CACHE: dict = {}


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
        "max_tokens": max_tokens, "temperature": _temp(temperature),
    }
    payload.update(_glm_thinking_fields(cfg))
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
        "max_tokens": max_tokens, "temperature": _temp(temperature),
        "stream": True,
    }
    payload.update(_glm_thinking_fields(cfg))
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


def _local_generate(cfg: dict, system: str, user_prompt: str, media: list,
                    temperature: float = 0.2) -> str:
    """本地视觉模型推理（GGUF / Transformers 两路），进程内缓存句柄避免重复加载。"""
    sel = str(cfg.get("local_model") or "").strip()
    if not sel:
        raise ValueError("请先在优化设置里选择本地视觉模型")
    images = _media_images(media) if cfg.get("read_media") else []
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
