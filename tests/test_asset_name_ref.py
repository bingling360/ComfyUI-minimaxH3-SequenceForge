"""素材引用改用「名字索引」的回归。

背景：`<Picture N>` 是**位置索引** —— 外部 agent 写的提示词贴进来后，用户还得
手动挂图、还得保证挂载顺序与编号一致，否则静默错位（只贴文本则完全不生效）。

改成**名字索引**（`@素材名`）后：
- 后端 `_apply_label_tokens` 按 seg.refs 顺序压实成 `<Picture k>`，最终文本仍合规；
- 前端 `applyPromptEdit → syncRefsFromText` 会把正文里的 `@名字` **自动写进 seg.refs**，
  不需要用户手动点 chip；
- 顺序无关（名字唯一），外部 agent 只凭素材名就能独立写完直接贴。

链路三处必须配套（缺一处就断）：
1. 前端 `collectSegMedia` 把**素材名**放进 media.label（不是 `<Picture N>`）；
2. 后端 `build_system_prompt` 告诉模型「可用素材：@名字」；
3. AI 扩写链的 `system_compile.md` 同样要求写 `@素材名`。
"""
import importlib.util
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools", "h3_prompt_expander")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


for p in (ROOT, TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

optimizer = _load("optimizer", os.path.join(ROOT, "optimizer.py"))


# ------------------------------------------------ 后端 system prompt

def test_system_prompt_offers_asset_names():
    s = optimizer.build_system_prompt("Ref2VA", 10.0, ["回廊场景", "少女立绘"], "中文")
    assert "@回廊场景" in s and "@少女立绘" in s, "应把素材名告诉模型"
    assert "可用素材" in s


def test_system_prompt_tells_not_to_write_picture_tags():
    s = optimizer.build_system_prompt("Ref2VA", 10.0, ["回廊场景"], "中文")
    assert "不要写 <Picture N>" in s


def test_system_prompt_has_no_contradiction():
    """回归：曾同时出现「具体帧锚用 <Picture N>」与「不要写 <Picture N>」。"""
    s = optimizer.build_system_prompt("Ref2VA", 10.0, ["回廊场景"], "中文")
    # 去掉那句显式禁令后，不应再出现把 <Picture N> 当正面写法的指令
    body = s.replace("不要写 <Picture N>", "")
    assert "<Picture N>" not in body, f"仍有互相矛盾的 <Picture N> 指令：{body}"


def test_system_prompt_without_assets():
    s = optimizer.build_system_prompt("T2VA", 5.0, [], "中文")
    assert "本段无参考素材" in s
    assert "可用素材" not in s, "没素材时不该给素材名单"


def test_subject_tag_still_documented():
    """@素材名 只替代素材引用；<Subject N> 等仍按官方写法。"""
    s = optimizer.build_system_prompt("Ref2VA", 10.0, ["回廊场景"], "中文")
    assert "<Subject N>" in s
    assert "<Video N>" in s


# ------------------------------------------------ 前端取图（源码守卫）

def test_collect_seg_media_uses_asset_label():
    """media.label 必须是素材名 —— 后端 build_system_prompt 直接用它拼名单。"""
    src = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    i = src.index("async function collectSegMedia")
    block = src[i:i + 2200]
    assert "label: nm" in block or "label: String(asset.label" in block, \
        "media.label 应取素材名"
    assert "`<Picture ${media.length + 1}>`" not in block, \
        "不应再用 <Picture N> 当 media.label"


def test_collect_seg_media_note_mentions_asset_names():
    src = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    i = src.index("async function collectSegMedia")
    block = src[i:i + 2400]
    assert "@${nm}" in block or "`@${" in block, "note 里应给出 @素材名"
    assert "不要写 <Picture N>" in block


# ------------------------------------------------ AI 扩写链 prompt

def test_compile_prompt_requires_asset_names():
    p = os.path.join(TOOLS, "prompts", "system_compile.md")
    src = open(p, encoding="utf-8").read()
    assert "@素材名" in src
    assert "不要写 `<Picture N>`" in src


def test_dialect_documents_the_divergence():
    p = os.path.join(TOOLS, "references", "h3-dialect.md")
    src = open(p, encoding="utf-8").read()
    assert "@素材名" in src, "dialect 要说明本工具与官方在素材引用上的差异"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
