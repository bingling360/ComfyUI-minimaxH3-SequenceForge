"""素材引用改用「名字索引」的回归。

背景：`<Picture N>` 是**位置索引** —— 外部 agent 写的提示词贴进来后，用户还得
手动挂图、还得保证挂载顺序与编号一致，否则静默错位（只贴文本则完全不生效）。

改成**名字索引**（`@素材名`）后：
- 后端 `_apply_label_tokens` 按 seg.refs 顺序压实成 `<Picture k>`，最终文本仍合规；
- 前端 `applyPromptEdit → syncRefsFromText` 会把正文里的 `@名字` **自动写进 seg.refs**，
  不需要用户手动点 chip；
- 顺序无关（名字唯一），外部 agent 只凭素材名就能独立写完直接贴。

链路三处必须配套（缺一处就断）：
1. 前端 `collectSegMedia` 把**标注**（图片1，B03 起）放进 media.label；
2. 后端 `build_system_prompt` 把 media.label 拼成「可用素材：@图片1」；
3. AI 扩写链的 `system_compile.md` 同样要求写 `@素材名`（标注只在 LLM 往返那一跳存在，
   盘上仍存 @素材名 —— 换码见 web/h3_prompts.js 的 marksToText / textToMarks）。
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
#
# B03 起这一跳的契约升级：media.label / note 用**标注**（图片1）而不是素材名 ——
# 长文件名进 LLM 费 token 又容易被抄错（抄错=静默丢图）。盘上仍存 @素材名，
# 换码规则唯一在 web/h3_prompts.js（marksToText / textToMarks，
# 运行期真跑见 tests/js/mark_codec_check.js）。

def test_collect_seg_media_uses_mark_not_raw_name():
    """media.label 必须是**标注** —— 后端 build_system_prompt 直接用它拼名单。

    钉的是"标注优先、回落引用名"这个意图，不是变量名（合并两条分支时
    变量叫过 mk / nm，钉变量名会在改名时假红、却挡不住真的回退）。
    """
    src = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    i = src.index("async function collectSegMedia")
    block = src[i:i + 2600]
    assert "asset.mark || asset.label" in block, \
        "media.label 应**优先**取标注（mark），只在没有标注时才回落引用名"
    assert re.search(r"mark:\s*[A-Za-z_$][\w$]*\(\s*hit\s*\)", block), \
        "标注从素材条目上取（不是现场拼 <Picture N>）"
    assert "`<Picture ${media.length + 1}>`" not in block, \
        "不应再用 <Picture N> 当 media.label"


def test_collect_seg_media_note_mentions_marks():
    src = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    i = src.index("async function collectSegMedia")
    block = src[i:i + 2800]
    assert "notes.push(`@${nm}`)" in block, "note 里应给出 @标注"
    assert "不要写 <Picture N>" in block
    assert "@图片N" in block or "图片1" in block, "note 要说明标注形态（@图片N）"


def test_llm_roundtrip_uses_mark_codec():
    """两条 LLM 链路（单步优化 / 扩写+优化）都必须换码，否则引用会丢。"""
    src = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    assert "function toLLMText(text, pool)" in src
    assert "function fromLLMText(text, pool)" in src
    for fn in ("async function runOptForSegment(",
               "async function runExpandOptimizeForSegment("):
        assert fn in src, f"找不到 {fn}（改名了就同步这里）"
        i = src.index(fn)
        block = src[i:i + 4200]
        assert "toLLMText(" in block, f"{fn} 出参未换码"
        assert "fromLLMText(" in block, f"{fn} 回参未换码"
    # 编解码规则只在 h3_prompts.js 一份（前端不许再写一套）
    hp = open(os.path.join(ROOT, "web", "h3_prompts.js"), encoding="utf-8").read()
    assert "function marksToText(text, pool)" in hp
    assert "function textToMarks(text, pool)" in hp


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
