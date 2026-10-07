# -*- coding: utf-8 -*-
"""AI 优化设置的**服务端持久化**守卫（2026-10-07）。

背景（用户反馈）：在设置面板填好自己的 API（服务商 / 地址 / Key）→ 保存 →
**重新进入又要再设一遍**。根因是「保存」只写进**当前工作流**的「导演台状态」
控件 —— 换一份工作流（新建 / 导入别人的 / 载入默认模板）配置就不在里面，
而那正是用户"重新进入"时打开的东西。

修法：保存**双写** —— 除了工作流，再写一份**机器级**的 optimizer.local.json
（已 gitignore），并且写成功后**立即刷新模块级 LOCAL_DEFAULTS**（不必重启 ComfyUI）。

这里钉住五件事：
  ① 只写白名单键（脏键不进磁盘）；
  ② 空串 / None **不覆盖**已有值（前端"留空 = 用已有的"）；
  ③ Key 只落那个 gitignore 的文件，`public_config` **绝不回明文**；
  ④ 兜底按**服务商**走（用户存的是百炼的 Key 时，选百炼留空也能跑；
     没配过的服务商**绝不**拿别家的 Key 去顶）；
  ⑤ 写失败要**报出来**（返回 ok=False + message），不能静默。

不碰本机真实的 optimizer.local.json：一律把 LOCAL_CONFIG_PATH 指到 tmp_path。
"""

import importlib.util
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "h3optimizer_local", os.path.join(ROOT, "optimizer.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def opt(tmp_path):
    m = _load()
    m.LOCAL_CONFIG_PATH = str(tmp_path / "optimizer.local.json")
    m.LOCAL_DEFAULTS = {}
    return m


def _read(m):
    with open(m.LOCAL_CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def test_whitelist_ignores_unknown_keys(opt):
    """白名单之外的一律不进磁盘。

    mode / expand 是**弹窗交互态**，写进机器级配置只会让下一份工作流莫名其妙地
    继承上一次弹窗里的临时选择 —— 所以它们在 LOCAL_SAVE_KEYS 之外。
    """
    r = opt.save_local_config({"provider": "dashscope", "mode": "local",
                               "expand": {"style": "creative"}, "evil": "x"})
    assert r["ok"] is True
    d = _read(opt)
    assert d["provider"] == "dashscope"
    assert "mode" not in d and "expand" not in d and "evil" not in d


def test_blank_values_do_not_overwrite(opt):
    """留空 = 不改。前端 Key 框留空是「用已有的」，不是「清掉」。

    这条同时保住了「恢复默认」按钮：它会把 api_key 置空，但**不该**把用户存在
    服务端的 Key 一起抹掉（否则点一次恢复默认，用户就得重新申请一遍 Key）。
    """
    opt.save_local_config({"provider": "dashscope", "api_key": "sk-first"})
    r = opt.save_local_config({"api_key": "", "provider": "", "model": None})
    assert r["ok"] is True
    assert r["saved"] == [], "全是空值就不该声称写入了"
    d = _read(opt)
    assert d["api_key"] == "sk-first"
    assert d["provider"] == "dashscope"


def test_api_keys_merge_by_provider(opt):
    """分服务商记忆：后一次保存不该把前一家挤掉。"""
    opt.save_local_config({"api_keys": {"dashscope": "sk-a"}})
    opt.save_local_config({"api_keys": {"openai": "sk-b"}})
    assert _read(opt)["api_keys"] == {"dashscope": "sk-a", "openai": "sk-b"}


def test_api_keys_blank_slot_ignored(opt):
    """前端保存过一次「Key 框留空的服务商」会写进空串 —— 不能把已有 Key 顶掉。"""
    opt.save_local_config({"api_keys": {"glm": "sk-real"}})
    opt.save_local_config({"api_keys": {"glm": ""}})
    assert _read(opt)["api_keys"]["glm"] == "sk-real"


def test_numbers_clamped_like_normalize(opt):
    """数值键按 normalize_config 同口径夹取，绝不把脏值写进磁盘。"""
    opt.save_local_config({"timeout": 99999, "max_tokens": 1})
    d = _read(opt)
    assert d["timeout"] == 1800
    assert d["max_tokens"] == 512


def test_takes_effect_without_restart(opt):
    """写成功后模块级 LOCAL_DEFAULTS 立刻刷新 —— 以前只在 import 时读一次。"""
    opt.save_local_config({"provider": "dashscope", "model": "qwen-vl-max"})
    cur = opt.default_config()
    assert cur["provider"] == "dashscope"
    assert cur["model"] == "qwen-vl-max"
    # 后端跑推理时读的是同一份 default_config()，所以无需重启即生效
    assert opt.normalize_config({})["model"] == "qwen-vl-max"


def test_key_fallback_is_per_provider(opt):
    """兜底按服务商走 —— 这是「设一次、以后新工作流免填」的核心。"""
    opt.save_local_config({"provider": "glm", "api_key": "sk-glm",
                           "api_keys": {"dashscope": "sk-ds"}})
    # 用户选百炼、前端留空 → 用本地文件里百炼那把
    assert opt.normalize_config({"provider": "dashscope"})["api_key"] == "sk-ds"
    # 用户选 GLM、前端留空 → 用本地文件里的默认 Key
    assert opt.normalize_config({"provider": "glm"})["api_key"] == "sk-glm"
    # 本地文件里没配过的服务商 → 一把都不给（绝不把别家的 Key 发出去换 401）
    assert opt.normalize_config({"provider": "openai"})["api_key"] == ""


def test_frontend_key_still_wins(opt):
    """前端这次真的填了 Key，就以它为准（本地文件只是兜底，不是覆盖）。"""
    opt.save_local_config({"api_keys": {"glm": "sk-local"}})
    got = opt.normalize_config({"provider": "glm", "api_key": "sk-live"})
    assert got["api_key"] == "sk-live"


def test_public_config_never_returns_key(opt):
    """脱敏：接口响应里不许出现任何 Key 明文（工作流会被导出分享）。"""
    opt.save_local_config({"provider": "dashscope", "api_key": "sk-secret",
                           "api_keys": {"openai": "sk-other"}})
    pub = opt.public_config(opt.normalize_config(None))
    blob = json.dumps(pub, ensure_ascii=False)
    assert "sk-secret" not in blob and "sk-other" not in blob
    assert pub["api_key"] == ""
    assert pub["api_keys"] == {}
    # 只给"哪些服务商有 Key"的名字
    assert "dashscope" in pub["global_keys"]
    # 可回显的那份里也不含 Key
    assert pub["global"]["provider"] == "dashscope"
    assert "api_key" not in pub["global"] and "api_keys" not in pub["global"]


def test_backup_written_and_pruned(opt, monkeypatch):
    """原子写 + 带时间戳的备份，且只留最近几份（别每次保存堆一个文件）。"""
    seq = {"i": 0}

    def fake_strftime(fmt):
        seq["i"] += 1
        return "202601010000%02d" % seq["i"]

    monkeypatch.setattr(opt.time, "strftime", fake_strftime)
    opt.save_local_config({"provider": "glm"})
    for i in range(5):
        opt.save_local_config({"model": "m%d" % i})
    d = os.path.dirname(opt.LOCAL_CONFIG_PATH)
    base = os.path.basename(opt.LOCAL_CONFIG_PATH) + ".bak-"
    baks = sorted(n for n in os.listdir(d) if n.startswith(base))
    assert len(baks) == opt._LOCAL_BAK_KEEP, "备份份数没被裁剪：%r" % baks
    # 备份名必须带 `-`，否则落在 .gitignore 的 `optimizer.local.json.bak-*` 之外
    assert all(n.startswith("optimizer.local.json.bak-") for n in baks)


def test_write_failure_is_reported(opt, tmp_path):
    """写不进去必须回 ok=False + message，不能装作成功。"""
    opt.LOCAL_CONFIG_PATH = str(tmp_path)          # 指向目录，写入必失败
    r = opt.save_local_config({"provider": "glm"})
    assert r["ok"] is False
    assert r["message"]


def test_non_dict_patch_rejected(opt):
    assert opt.save_local_config(None)["ok"] is False
    assert opt.save_local_config("x")["ok"] is False
