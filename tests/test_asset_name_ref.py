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
    s = optimizer.build_system_prompt("Ref2VA", 10.0, ["回廊场景", "少女立绘"], None)
    assert "@回廊场景" in s and "@少女立绘" in s, "应把素材名告诉模型"
    assert "可用素材" in s


def test_system_prompt_keeps_asset_name_out_of_description():
    """@素材名 **只**允许出现在 subject_definitions 的定义行。

    镜头正文里必须用 <Subject N> 等官方标签指代 —— 这正是用户报的
    「多参时镜头正文里也出现图片引用」那个 bug 的正面契约。
    """
    s = optimizer.build_system_prompt("Ref2VA", 10.0, ["回廊场景"], None)
    assert "Never write a bare @素材名 inside the description" in s
    assert "subject_definitions" in s


def test_system_prompt_has_no_contradiction():
    """回归：曾同时出现「具体帧锚用 <Picture N>」与「不要写 <Picture N>」。

    官方语义下两者**不冲突**：<Picture N> 是有确切含义的官方标签（具体帧锚），
    只是「只用来定义角色/场景/服装/风格的图」不该单独占一行 <Picture N>，
    应并进 <Subject N> 的定义行。所以这里钉的是**语义归属**，不是禁令。
    """
    s = optimizer.build_system_prompt("Ref2VA", 10.0, ["回廊场景"], None)
    # <Picture N> 只允许作为「具体帧锚」出现
    assert "<Picture N> = a reference image used as a concrete frame" in s
    # 且必须写明「只作定义用的图不单独占 <Picture N> 行」
    assert "does NOT get a standalone <Picture N> line" in s


def test_system_prompt_tags_asset_kinds():
    """视频 / 音频素材必须带类别标签 —— 否则模型一律写 <Subject N>。

    官方把三类标签语义做得**互斥**（ref-en.txt §2）：图片归 <Subject N> /
    <Picture N>，视频归 <Video N>，音频归 <Audio N>。只给一串名字（图片和视频
    长得一样）时模型没法判种类，只会全写成 <Subject N>，官方语义直接错位。
    """
    s = optimizer.build_system_prompt(
        "Ref2VA", 12.0,
        [{"name": "女主.png", "kind": "image"},
         {"name": "运镜.mp4", "kind": "video"},
         {"name": "雨声.wav", "kind": "audio"}], None)
    assert "@运镜.mp4（视频）" in s, "视频素材要带（视频）标记"
    assert "@雨声.wav（音频）" in s, "音频素材要带（音频）标记"
    assert "@女主.png" in s and "@女主.png（" not in s, "图片不必加括号（默认类）"
    assert "<Video N>" in s and "<Audio N>" in s, "要告诉模型类别与标签的对应"


def test_system_prompt_labels_backward_compatible():
    """labels 的三种形状都要能用：纯字符串 / dict / 二元组。"""
    for labels in (["a.png"], [{"name": "a.png"}], [("image", "a.png")]):
        s = optimizer.build_system_prompt("T2VA", 8.0, labels, None)
        assert "@a.png" in s, f"{labels!r} 应能解析出素材名"


def test_system_prompt_without_assets():
    s = optimizer.build_system_prompt("T2VA", 5.0, [], None)
    assert "本段无参考素材" in s
    assert "可用素材" not in s, "没素材时不该给素材名单"


def test_subject_tag_still_documented():
    """@素材名 只替代素材引用；<Subject N> 等仍按官方写法。"""
    s = optimizer.build_system_prompt("Ref2VA", 10.0, ["回廊场景"], None)
    assert "<Subject N>" in s
    assert "<Video N>" in s


# ------------------------------------------------ 前端取图（源码守卫）
#
# B03 起这一跳的契约升级：media.label / note 用**标注**（图片1）而不是素材名 ——
# 长文件名进 LLM 费 token 又容易被抄错（抄错=静默丢图）。盘上仍存 @素材名，
# 换码规则唯一在 web/h3_prompts.js（marksToText / textToMarks，
# 运行期真跑见 tests/js/mark_codec_check.js）。

def _fn_block(src, header):
    """按**函数边界**取源码片段（不用固定长度 —— 加注释让函数变长就截断、假红）。

    依据是顶层收尾：函数体结束那行的 `}` 顶格（函数内的 `}` 都有缩进）。
    """
    i = src.index(header)
    j = src.index("\n}\n", i)
    return src[i:j + 3]


def test_collect_seg_media_uses_mark_not_raw_name():
    """media.label 必须是**标注** —— 后端 build_system_prompt 直接用它拼名单。

    钉的是"标注优先、回落引用名"这个意图，不是变量名（合并两条分支时
    变量叫过 mk / nm，钉变量名会在改名时假红、却挡不住真的回退）。
    """
    src = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    block = _fn_block(src, "async function collectSegMedia")
    assert "asset.mark || asset.label" in block, \
        "media.label 应**优先**取标注（mark），只在没有标注时才回落引用名"
    assert re.search(r"mark:\s*[A-Za-z_$][\w$]*\(\s*hit\s*\)", block), \
        "标注从素材条目上取（不是现场拼 <Picture N>）"
    assert "`<Picture ${media.length + 1}>`" not in block, \
        "不应再用 <Picture N> 当 media.label"


def test_collect_seg_media_keeps_video_and_audio():
    """视频 / 音频参考**不许**被 collectSegMedia 过滤掉。

    历史 bug：循环里写了 `if (!hit || hit.kind !== "image") continue;`，
    且 media 一律 push `kind: "image"` —— 结果视频与音频参考被静默丢弃，
    模型根本不知道本段挂了它们，永远写不出官方的 <Video N> / <Audio N>。
    后端 REF_CAPS 明明是 {image:9, video:3, audio:3}，官方标签也分了三类。
    """
    src = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    block = _fn_block(src, "async function collectSegMedia")
    assert 'hit.kind !== "image"' not in block, \
        "不许按 kind 过滤参考素材（会把视频/音频整类丢掉）"
    assert 'kind: "image", label: nm' not in block, \
        "media.kind 必须跟随素材真实类别，不能一律写 image"
    assert 'media.push({ kind: "audio", label: nm })' in block, \
        "音频参考要进 media（带 kind: audio，不带 images）"
    assert 'media.push({ kind: _k, label: nm, images: [dataUrl] })' in block, \
        "图/视频参考的 media.kind 要用素材真实 kind"


def test_collect_seg_media_note_mentions_marks():
    src = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    block = _fn_block(src, "async function collectSegMedia")
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
    """跳2 编译器：`@素材名` 只许出现在 subject_definitions 定义行。

    历史口径是"素材引用写 @素材名，不要写 <Picture N>"（把 @ 当**正文**写法）。
    现行口径与之相反：`@` 只进定义行，正文一律用官方标签 —— 所以钉的是
    「位置」与「全篇英文」，不是旧禁令。
    """
    p = os.path.join(TOOLS, "prompts", "system_compile.md")
    src = open(p, encoding="utf-8").read()
    assert "@素材名" in src, "要说明素材名的写法"
    assert "subject_definitions" in src and "定义行" in src, \
        "必须写明 @素材名 只允许出现在 subject_definitions 的定义行"
    assert "detailed_description" in src, "要说明正文里用官方标签指代"
    assert "全篇英文" in src, "语言口径必须是全英文（中英混排会出严重问题）"


def test_dialect_documents_the_divergence():
    p = os.path.join(TOOLS, "references", "h3-dialect.md")
    src = open(p, encoding="utf-8").read()
    assert "@素材名" in src, "dialect 要说明本工具与官方在素材引用上的差异"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
