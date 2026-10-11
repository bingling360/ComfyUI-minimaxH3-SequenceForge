"""显性软引用语义回归（AST 抽取 shipped 纯函数真跑，无 ComfyUI 可跑）。

跑法（仓库根目录）：
    python -m pytest tests/test_ref_semantics.py -q

覆盖：最小映射块形态、三类独立编号、缺 tag 精确补齐（含
<Picture 1>/<Picture 10> 子串陷阱）、已写 tag 直通无注入、
未知 [[..]] 点名报错、旧英文长文 header 已删除。
"""

import ast
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WANT_FUNCS = ("_normalize_order", "_kind_tokens", "_apply_label_tokens",
               "_reference_tags_minimal", "_uncovered_tags", "_wrap_dialogue",
               "_compose_official",
               "_clean_frame_file", "_parse_frame_imgs")
# `@标签` 语法把原来的 _LABEL_TOKEN 拆成「括号写法 + @写法」两个正则
_WANT_VARS = ("_REF_BRACKET", "_REF_AT", "_OFFICIAL_FIELD_RE")


@pytest.fixture(scope="session")
def kern():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FUNCS:
            wanted.append(node)
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _WANT_VARS for t in node.targets):
            wanted.append(node)
    assert {n.name for n in wanted if isinstance(n, ast.FunctionDef)} == set(_WANT_FUNCS)
    ns = {"re": re}
    exec(compile(ast.Module(body=wanted, type_ignores=[]),
                 "nodes.py:ref-kernel", "exec"), ns)
    return types.SimpleNamespace(
        **{k: ns[k] for k in list(_WANT_FUNCS) + list(_WANT_VARS)})


def test_minimal_block_shape(kern):
    """官方标签语义分流：可复用可见内容（角色/场景/道具/风格图）归 <Subject N>，
    整片关系归 <Video N>，音频信号归 <Audio N>。

    ⛔ 不再把三类一律写成 `<Picture N> is the reference image ...`：官方 ref-en.txt
    §2.2 规定 `<Picture N>` 只给「图片本身当某镜首帧/关键帧/末帧/构图锚」的情形，
    而"用于定义角色/场景/服装/风格的图不单独建 picture 条目"。旧写法与帧锚编号
    池共用 <Picture N> 序号，会让模型把角色图当成"某镜的一帧"。
    """
    out = kern._reference_tags_minimal(
        [("image", "角色1"), ("video", "片"), ("audio", "乐")])
    assert out.splitlines() == [
        # ⚠ 必须带 subject_definitions 字段头：官方六段式里这些条目就属于该字段，
        # 裸条目会落在所有字段之前变成"前言"，被 prompts.validate_compiled 判
        # E_OVERRIDE_PREAMBLE（"覆盖文本首行既不是官方字段头、也不是关键帧对齐指令"）。
        "subject_definitions:",
        '<Subject 1> is the content shown in <Picture 1>, the source image "角色1".',
        '<Video 1> is the source video for the target video edit, the file "片".',
        # 音频号 = 2：参考视频的音轨在官方 tokenizer 里已占掉 <Audio 1>
        '<Audio 2> is the reference audio signal that is reused in the target video, the file "乐".',
    ]
    # 自造赋值表（`token = 别名` / [References] 头）已废止
    assert "[References]" not in out and " = " not in out
    # 无条目 → 空串（不能吐一个光杆字段头）
    assert kern._reference_tags_minimal([]) == ""


def test_missing_tags_filled(kern):
    order = [("image", "角色1"), ("video", "片"), ("audio", "乐")]
    full = kern._apply_label_tokens("她走过街道 [[角色1]]", order)
    assert "<Picture 1>" in full
    mapping = kern._kind_tokens(kern._normalize_order(order))
    uncovered = kern._uncovered_tags(full, mapping)
    assert uncovered == ["片", "乐"]
    head = kern._reference_tags_minimal(
        [(k, lbl) for k, lbl in kern._normalize_order(order) if lbl in uncovered])
    assert head.splitlines() == [
        "subject_definitions:",
        '<Video 1> is the source video for the target video edit, the file "片".',
        '<Audio 2> is the reference audio signal that is reused in the target video, the file "乐".']
    assert "角色1" not in head  # 已覆盖的不重复声明


def test_picture_prefix_trap(kern):
    # "<Picture 1>" 是 "<Picture 10>" 的子串：只写了后者时前者必须判为缺失
    mapping = {"图1": "<Picture 1>", "图10": "<Picture 10>"}
    assert kern._uncovered_tags("看 <Picture 10> 里的猫", mapping) == ["图1"]
    assert kern._uncovered_tags("看 <Picture 1> 和 <Picture 10>", mapping) == []


def test_all_typed_passthrough(kern):
    order = [("image", "A")]
    full = kern._apply_label_tokens("见 [[A]]", order)
    mapping = kern._kind_tokens(kern._normalize_order(order))
    assert kern._uncovered_tags(full, mapping) == []


def test_unknown_label_points(kern):
    with pytest.raises(ValueError, match="未知素材标签"):
        kern._apply_label_tokens("见 [[谁]]", [("image", "A")])


def test_repeat_refs_keep_single_token(kern):
    """同一素材在一段里引用多次：编号仍只占一个 <Picture k>（重复=正文写多次）。"""
    order = [("image", "女主"), ("image", "女主"), ("image", "背景")]
    mapping = kern._kind_tokens(kern._normalize_order(order))
    assert mapping == {"女主": "<Picture 1>", "背景": "<Picture 2>"}
    full = kern._apply_label_tokens("开头 @女主 站着，结尾又回到 @女主", order)
    assert full.count("<Picture 1>") == 2      # 正文里出现两次 → 参考两次
    assert "<Picture 2>" not in full


def test_parse_frame_imgs(kern):
    """段级首尾帧参考图：{first,end} 相对路径，拒穿越。"""
    segs = [{"frame_img": {"first": "assets/a.png"}},
            {},
            {"frame_img": {"end": "assets\\b.png", "first": "../evil.png"}}]
    got = kern._parse_frame_imgs(segs, 3)
    assert got == [("assets/a.png", ""), ("", ""), ("", "assets/b.png")]
    # 段数不足时按 n 补齐；非法路径一律清空
    assert kern._parse_frame_imgs(segs, 1) == [("assets/a.png", "")]
    assert kern._clean_frame_file("a/b/c.png") == ""
    assert kern._clean_frame_file("assets/ok.png") == "assets/ok.png"


def test_old_header_gone():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert "def _reference_header" not in src
    assert "_reference_header(" not in src
    assert "def _reference_tags_minimal" in src
    assert "_uncovered_tags(full, mapping)" in src


def test_compose_official_always_emits_music_field(kern):
    """配乐字段必须**无条件存在**（无配乐写 N/A）—— 官方 base-en.txt §4.7。

    旧实现留空就整字段省略，理由是"官方 N/A = 明确请求无声，省略 = 不约束" ——
    那是把 §4.6 overall_soundscape 的规则（N/A = 明确要求全片静音）错套到配乐上：
    §4.7 明写 "Use `N/A` when there is no non-diegetic music"。而前端根本没有配乐
    输入框 → **每一段**都缺这个官方必填字段，prompts.validate_compiled 判 E_NO_MUSIC。
    """
    out, _ = kern._compose_official("", "", "[Shot 1] a woman walks in", "", "")
    assert "non_diegetic_music: N/A" in out
    # 环境音留空仍**省略**（官方只在"明确要求全片静音"时才写 N/A）
    assert "overall_soundscape" not in out
    out2, _ = kern._compose_official("", "", "[Shot 1] x", "rain on the awning", "soft cello")
    assert "overall_soundscape: rain on the awning" in out2
    assert "non_diegetic_music: soft cello" in out2
    # 已是官方格式（含任一字段头）→ 原样直通，不补字段
    raw = "detailed_description: [Shot 1] x\nnon_diegetic_music: N/A"
    assert kern._compose_official("", "", raw, "", "")[0] == raw


def test_ref2va_main_field_is_detailed_description_source_point():
    """有参考素材的段，主字段名必须是 detailed_description（官方 ref2v 字段对照表）。

    旧实现无论有没有参考都写 base 的 integrated_multimodal_description ——
    prompts.validate_compiled 判 "Ref2VA 不应含字段 ['integrated_multimodal_description']"，
    随后按 REF_MAIN_FIELD 取正文取不到 → E_EMPTY_DESC / E_NO_SHOT，
    即"整段描述对模型等于不存在，只声明了引用没给正文"。
    """
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert r'r"(?m)^integrated_multimodal_description:"' in src
    assert '"detailed_description:", full, count=1' in src


def test_audio_ordinal_reserves_video_soundtrack(kern):
    """音频号必须给每个参考视频的音轨预扣一个。

    官方 tokenizer 会为每条 `ref_video_audio_N` 插一条 `<Audio j>: `（源码原话：
    "the soundtrack gets its own <Audio j> label, emitted before <Video k>"），
    而本节点对每个参考视频都塞了音轨槽（`_decode_video_file` 对无音轨视频也回
    一个静音 dict）→ 官方那边 `soundtrack is not None` 恒成立。
    所以「视频 + 独立音频」同段时独立音频是 `<Audio 2>`；写错的话正文里的
    `<Audio j>` 会指向视频音轨、独立音频等于没被引用。
    与 asset_store.compile_refs / 前端映射条同规则，三处必须一致。
    """
    # 无视频：音频号不受影响（确认修复没有过度偏移）
    assert kern._kind_tokens([("image", "图A"), ("image", "图B"), ("audio", "乐M")]) == {
        "图A": "<Picture 1>", "图B": "<Picture 2>", "乐M": "<Audio 1>"}
    # 1 个视频 → 独立音频偏 1
    assert kern._kind_tokens([("image", "图A"), ("video", "视V"), ("audio", "乐M")]) == {
        "图A": "<Picture 1>", "视V": "<Video 1>", "乐M": "<Audio 2>"}
    # 视频在前也一样（音频号的分配顺序是「视频音轨 → 独立音频」，与勾选顺序无关）
    assert kern._kind_tokens([("video", "视V"), ("audio", "乐M")]) == {
        "视V": "<Video 1>", "乐M": "<Audio 2>"}
    # 2 个视频 → 独立音频偏 2；图片/视频编号不受影响
    assert kern._kind_tokens([("video", "视1"), ("video", "视2"),
                             ("audio", "乐A"), ("audio", "乐B")]) == {
        "视1": "<Video 1>", "视2": "<Video 2>",
        "乐A": "<Audio 3>", "乐B": "<Audio 4>"}
    # 三类混排：图/视各自独立计数，音频含音轨占位
    assert kern._kind_tokens([("image", "图A"), ("video", "视V"),
                             ("image", "图B"), ("audio", "乐M")]) == {
        "图A": "<Picture 1>", "视V": "<Video 1>",
        "图B": "<Picture 2>", "乐M": "<Audio 2>"}


def test_kind_tokens_matches_frontend_and_asset_store():
    """三处编号实现必须同规则（nodes._kind_tokens / 前端映射条 / asset_store）。

    前端映射条的真跑断言在 tests/js/refbar_status_check.js（⑤/⑥），
    asset_store 的在 tests/test_asset_store_p0.py::test_audio_ordinal_reserves_video_soundtrack；
    这里锁「规则」本身在源码里都写着预扣，防止有人只改一处。
    """
    js = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    store = open(os.path.join(ROOT, "asset_store.py"), encoding="utf-8").read()
    assert "ord.audio += 1" in js, "前端映射条缺少音轨预扣"
    assert 'counters["audio"] += 1' in store, "asset_store 缺少音轨预扣"
    assert "_seen_video" in open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()


def test_frame_anchors_not_declared_in_subject_definitions_source_point():
    """帧锚不该由 subject_definitions 声明（只有"有真参考素材"才走 Ref2VA 六段式）。

    帧锚（首帧图/尾帧图）也会进 order —— 它要拿 <Picture k> 的 vision block。
    但：
      · 纯帧锚段（I2VA/FL2VA/L2VA，base 三字段）的声明由对齐指令句承担，
        subject_definitions 是 Ref2VA 字段，写进去校验器判 E_FIELD_SET；
      · 混合段（帧锚 + 参考素材）帧锚才该进 subject_definitions（官方 ref2v §2）。
    所以判定必须用「非帧锚引用」而不是「order 非空」。
    """
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert "_anch_lbls_i = set(_anchors_of(i))" in src
    assert "_real_refs = [(k, l) for k, l in order if l not in _anch_lbls_i]" in src
    assert "if _real_refs:" in src
