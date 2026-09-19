# -*- coding: utf-8 -*-
"""GLM（智谱）默认通道 + 超时 / 思考开关的回归守卫。

背景（2026-09-19）：默认服务商是 RunningHub、模型是预设占位名，开箱就调不通；
`_http_post_json` 写死 timeout=120 且不重试，于是"带图 + 长规则 + 六字段长输出"
的改写必然抛 `LLM 请求失败：The read operation timed out`。这里钉住修复后的口径。

不联网、不读本机的 optimizer.local.json（那个文件是 gitignore 的私有层，
CI 上没有）—— 需要它的用例一律 monkeypatch `LOCAL_DEFAULTS`。
"""

import importlib.util
import json
import os
import socket

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "h3optimizer_glm", os.path.join(ROOT, "optimizer.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def opt():
    return _load()


class _Resp:
    """最小响应替身：urlopen 的上下文管理器 + read()。"""

    def __init__(self, payload=b'{"ok":1}', status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---- 默认值 ----

def test_default_provider_is_glm(opt):
    cfg = opt.normalize_config(None)
    assert cfg["provider"] == "glm"
    assert cfg["model"] == "glm-5.3-flashx"
    assert cfg["api_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert cfg["protocol"] == "openai"
    assert opt.PROVIDERS["glm"] == ("https://open.bigmodel.cn/api/paas/v4",
                                    "glm-5.3-flashx", "openai")
    # 默认思考关。注意默认型号是**「始终思考」型号**，所以这里的 disabled 会被
    # 翻译成最低强度 low（见 test_default_request_uses_low_effort）。
    assert cfg["thinking"] == "disabled"


def test_default_request_uses_low_effort(opt, monkeypatch):
    """默认配置真正发出去的请求体：默认型号 glm-5.3-flashx 不能收
    `thinking.type=disabled`（400 / code 1210），所以必须是 `reasoning_effort: low`。

    这条守卫的价值在于：改默认型号时很容易只改 DEFAULT_CONFIG，忘了它会连带
    改变**请求体形状**。默认值一变，开箱那条请求就跟着变。"""
    seen = {}

    def cap(req, timeout=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp(b'{"choices":[{"message":{"content":"ok"}}]}')

    monkeypatch.setattr(opt.urllib.request, "urlopen", cap)
    cfg = opt.normalize_config({"api_key": "K"})
    assert opt._api_generate(cfg, "sys", "user", [], 8192) == "ok"
    assert seen["body"]["model"] == "glm-5.3-flashx"
    assert "thinking" not in seen["body"], "强制思考型号不能收 thinking.type=disabled"
    assert seen["body"]["reasoning_effort"] == "low"


def test_timeout_default_and_clamp(opt):
    assert opt.normalize_config(None)["timeout"] == 300
    assert opt.normalize_config({"timeout": 1})["timeout"] == 30
    assert opt.normalize_config({"timeout": 99999})["timeout"] == 1800
    assert opt.normalize_config({"timeout": "abc"})["timeout"] == 300
    assert opt.normalize_config({"timeout": "600"})["timeout"] == 600


def test_thinking_normalized(opt):
    assert opt.normalize_config({"thinking": "ENABLED"})["thinking"] == "enabled"
    assert opt.normalize_config({"thinking": "垃圾值"})["thinking"] == "auto"


# ---- 请求体：thinking / reasoning_effort 只发给智谱 ----

def test_thinking_fields_only_for_bigmodel(opt):
    """`thinking` / `reasoning_effort` 都是智谱私有字段。塞进 OpenAI / 百炼的
    请求体只会换来 400，所以非智谱端点必须是空 dict（不下发任何字段）。"""
    glm = {"api_url": "https://open.bigmodel.cn/api/paas/v4", "thinking": "disabled",
           "model": "glm-4.6v"}
    assert opt._glm_thinking_fields(glm) == {"thinking": {"type": "disabled"}}
    assert opt._glm_thinking_fields(dict(glm, thinking="enabled")) == {"thinking": {"type": "enabled"}}
    assert opt._glm_thinking_fields(dict(glm, thinking="auto")) == {}
    assert opt._glm_thinking_fields({"api_url": "https://api.openai.com/v1",
                                     "thinking": "enabled"}) == {}
    assert opt._glm_thinking_fields({"api_url": "", "thinking": "enabled"}) == {}


def test_forced_thinking_model_translates_disabled_to_low(opt):
    """「始终思考」型号不能收 `thinking.type=disabled` —— 实测 400 / code 1210：
    「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」。

    用户选「关闭」的意图是"别想太久、快点出结果"，所以**翻译成最低强度 low**，
    而不是什么都不发（什么都不发 = 服务商默认，通常是 max，最慢的那个）。
    """
    cfg = {"api_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-5.3-flash",
           "thinking": "disabled", "reasoning_effort": ""}
    assert opt._glm_thinking_fields(cfg) == {"reasoning_effort": "low"}
    # 换型号：glm-4.6v 能真正关掉，就该老老实实下发 disabled
    assert opt._glm_thinking_fields(dict(cfg, model="glm-4.6v")) == {"thinking": {"type": "disabled"}}


def test_effort_only_for_supporting_models(opt):
    """`reasoning_effort` 只对 glm-5 系有效（官方："仅 GLM-5.2 及以上支持"）。
    给不认识的型号发这个字段，整个请求都会被拒。"""
    base = {"api_url": "https://open.bigmodel.cn/api/paas/v4", "thinking": "enabled"}
    got = opt._glm_thinking_fields(dict(base, model="glm-5.3-flash", reasoning_effort="max"))
    assert got == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    # glm-4.6v 不支持强度：只发 thinking，不发 effort
    got = opt._glm_thinking_fields(dict(base, model="glm-4.6v", reasoning_effort="max"))
    assert got == {"thinking": {"type": "enabled"}}


def test_explicit_effort_wins_over_disabled_translation(opt):
    """强制思考型号 + 「关闭」+ 显式强度：显式指定的强度优先于降级翻译。"""
    cfg = {"api_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-5.3-flash",
           "thinking": "disabled", "reasoning_effort": "high"}
    assert opt._glm_thinking_fields(cfg) == {"reasoning_effort": "high"}


def test_glm_caps_matches_thinking_fields(opt):
    """能力表（下发给前端渲染下拉）必须与真实下发行为一致 —— 两边不一致的
    症状就是"界面说能关、请求却被 400 拒绝"。"""
    caps = opt._glm_caps("glm-5.3-flash")
    assert caps["forced_thinking"] is True and caps["supports_effort"] is True
    assert caps["disabled_effect"] == "low"
    caps = opt._glm_caps("glm-4.6v")
    assert caps["forced_thinking"] is False and caps["supports_effort"] is False
    assert caps["disabled_effect"] == "disabled"
    # 强制思考但不支持强度（glm-4.7）：关闭时后端什么都不发，所以是空串
    caps = opt._glm_caps("glm-4.7")
    assert caps["forced_thinking"] is True and caps["supports_effort"] is False
    assert caps["disabled_effect"] == ""
    assert opt._glm_thinking_fields({"api_url": "https://open.bigmodel.cn/api/paas/v4",
                                     "model": "glm-4.7", "thinking": "disabled"}) == {}


def test_max_tokens_clamp_allows_long_reasoning(opt):
    """上限 32768：推理 token **也算在 max_tokens 里**，上限卡在 8192 等于把
    "思考模型 + 长提示词"这条路堵死（用户看到的「LLM 返回空文本」）。"""
    assert opt.normalize_config({"max_tokens": 200000})["max_tokens"] == 32768
    assert opt.normalize_config({"max_tokens": 16384})["max_tokens"] == 16384
    assert opt.normalize_config({"max_tokens": 10})["max_tokens"] == 512
    assert opt.normalize_config({})["max_tokens"] >= 8192


def test_empty_text_error_names_the_real_cause(opt):
    """空正文不是"服务商抽风"：最常见是**推理把配额吃光**（finish_reason=length）。
    报错必须把这件事说清楚并给出可操作的两条路，否则用户只能反复重试。"""
    msg = str(opt._empty_text_error(
        {"finish_reason": "length", "message": {"reasoning_content": "想" * 1975}},
        {"completion_tokens": 512, "completion_tokens_details": {"reasoning_tokens": 509}}))
    assert "最大输出 token" in msg and "16384" in msg
    assert "思考强度" in msg
    # 真·空返回（没有推理痕迹）不该硬套"推理吃光配额"的解释
    msg2 = str(opt._empty_text_error({"finish_reason": "stop", "message": {}}, {}))
    assert "没有产出正文" in msg2 and "推理" not in msg2.split("。")[0]


def test_glm_request_body_and_timeout(opt, monkeypatch):
    seen = {}

    def cap(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["auth"] = req.headers.get("Authorization")
        seen["timeout"] = timeout
        return _Resp(b'{"choices":[{"message":{"content":"ok"}}]}')

    monkeypatch.setattr(opt.urllib.request, "urlopen", cap)
    cfg = opt.normalize_config({"provider": "glm", "api_key": "K", "timeout": 420,
                                "model": "glm-4.6v"})
    assert opt._api_generate(cfg, "sys", "user", [], 4096) == "ok"
    assert seen["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert seen["body"]["model"] == "glm-4.6v"
    assert seen["body"]["thinking"] == {"type": "disabled"}
    assert seen["auth"] == "Bearer K"
    # 超时必须真的下发到 socket 层，不能只存在配置里
    assert seen["timeout"] == 420


def test_non_glm_request_body_has_no_thinking(opt, monkeypatch):
    seen = {}

    def cap(req, timeout=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp(b'{"choices":[{"message":{"content":"ok"}}]}')

    monkeypatch.setattr(opt.urllib.request, "urlopen", cap)
    cfg = opt.normalize_config({"provider": "openai", "api_key": "K", "thinking": "enabled"})
    opt._api_generate(cfg, "s", "u", [], 4096)
    assert "thinking" not in seen["body"]


# ---- 本地私有 Key 的兜底边界 ----

def test_local_key_fallback_only_for_default_provider(opt):
    """只在服务商 == 本地默认服务商时兜底。否则会把 GLM 的 Key 发给 OpenAI，
    换回一个 401 —— 比"没填 Key"更难排查。"""
    opt.LOCAL_DEFAULTS = {"provider": "glm", "api_key": "LOCALKEY"}
    assert opt.normalize_config({"provider": "glm"})["api_key"] == "LOCALKEY"
    assert opt.normalize_config({})["api_key"] == "LOCALKEY"          # 缺省即默认服务商
    assert opt.normalize_config({"provider": "openai"})["api_key"] == ""
    # 显式填的永远优先
    assert opt.normalize_config({"provider": "glm", "api_key": "USER"})["api_key"] == "USER"
    assert opt.normalize_config({"provider": "glm", "api_keys": {"glm": "PER"}})["api_key"] == "PER"
    # 空串 == 没填：前端点一次「保存」就会写进 {"glm": ""}，不能把兜底顶掉
    assert opt.normalize_config({"provider": "glm", "api_keys": {"glm": ""}})["api_key"] == "LOCALKEY"
    assert opt.normalize_config({"provider": "glm", "api_keys": {"glm": "  "}})["api_key"] == "LOCALKEY"
    assert opt.normalize_config({"provider": "glm", "api_key": ""})["api_key"] == "LOCALKEY"


def test_public_config_masks_key_but_reports_it(opt):
    opt.LOCAL_DEFAULTS = {"provider": "glm", "api_key": "LOCALKEY"}
    pub = opt.public_config(opt.normalize_config(None))
    assert pub["api_key"] == ""                 # 脱敏：Key 永不出网
    assert pub["has_api_key"] is True
    assert pub["has_default_key"] is True       # 前端据此放行"Key 留空"
    assert pub["providers"]["glm"]["model"] == "glm-5.3-flashx"


def test_public_config_without_local_file(opt):
    opt.LOCAL_DEFAULTS = {}
    pub = opt.public_config(opt.normalize_config(None))
    assert pub["has_default_key"] is False
    assert pub["has_api_key"] is False


# ---- HTTP 层：超时文案可行动 + 重试边界 ----

def test_timeout_error_is_actionable(opt, monkeypatch):
    """不能把 socket 的英文原文丢给前端。"""
    def boom(*a, **k):
        raise socket.timeout("The read operation timed out")

    monkeypatch.setattr(opt.urllib.request, "urlopen", boom)
    monkeypatch.setattr(opt.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError) as ei:
        opt._http_post_json("http://x", {}, timeout=120)
    msg = str(ei.value)
    assert "超时" in msg and "120" in msg and "请求超时" in msg
    assert "read operation" not in msg


def test_urlerror_wrapping_timeout_is_detected(opt, monkeypatch):
    """连接阶段超时会被包进 URLError.reason，类型判不出来就得靠文案。"""
    def boom(*a, **k):
        raise opt.urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr(opt.urllib.request, "urlopen", boom)
    monkeypatch.setattr(opt.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="超时"):
        opt._http_post_json("http://x", {}, timeout=60)


def test_http_retries_transient_then_succeeds(opt, monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionResetError("connection reset by peer")
        return _Resp(b'{"ok":1}')

    monkeypatch.setattr(opt.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(opt.time, "sleep", lambda s: None)
    assert opt._http_post_json("http://x", {}) == {"ok": 1}
    assert calls["n"] == 2


def test_http_4xx_not_retried(opt, monkeypatch):
    """401/400 重试没有意义，只会白等。"""
    calls = {"n": 0}

    def bad(*a, **k):
        calls["n"] += 1
        raise opt.urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(opt.urllib.request, "urlopen", bad)
    monkeypatch.setattr(opt.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="401"):
        opt._http_post_json("http://x", {})
    assert calls["n"] == 1


def test_http_5xx_retried_then_raises(opt, monkeypatch):
    calls = {"n": 0}

    def bad(*a, **k):
        calls["n"] += 1
        raise opt.urllib.error.HTTPError("http://x", 503, "Unavailable", {}, None)

    monkeypatch.setattr(opt.urllib.request, "urlopen", bad)
    monkeypatch.setattr(opt.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="503"):
        opt._http_post_json("http://x", {}, retries=2)
    assert calls["n"] == 3


def test_no_api_key_committed_in_source():
    """**公开仓库**：任何入库的 .py/.js/.json 都不许出现形如 GLM Key 的字面量。

    2026-09-19 真实踩到：探针脚本 tools/_probe_stream_usage.py 里直接写了
    `KEY = "<36位>.<16位>"`。本仓库是 public 的，一旦 commit 就等于把 Key 送出去。
    现在探针改为从 `GLM_API_KEY` 环境变量 / `optimizer.local.json`（已 gitignore）读。

    这里不联网、不查 git，只扫工作区源码 —— 因为 CI 与本地都可能没装 git 元数据。
    """
    import re
    # 智谱 Key 形如 <32位小写十六进制>.<16位字母数字>
    pat = re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b")
    skip_dirs = {"node_modules", "__pycache__", ".git", ".workbuddy-ai", "logs"}
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if not fn.endswith((".py", ".js", ".json", ".cjs", ".md", ".txt")):
                continue
            if fn.startswith("optimizer.local.json"):
                continue          # 私有层，本来就不入库
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
            except OSError:
                continue
            for m in pat.finditer(src):
                rel = os.path.relpath(p, ROOT).replace("\\", "/")
                hits.append("%s: %s" % (rel, m.group(0)))
    assert not hits, "发现疑似硬编码 API Key（公开仓库禁止入库）：\n" + "\n".join(hits)
