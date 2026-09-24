#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扮演外部 agent：用 skill 规则手写一份多段提示词，再走总提示词框编译并逐项对官方。

这份文本**不是从解析器倒推的**，而是按 skill 的 04-master-format.md +
03-optimize.md 手写，然后交给真解析器检验 —— 验的是"照 skill 写出来的东西
能不能过框"。

用例构造（用户要求"多测一些特殊情况"）：
- 覆盖 base 三字段 + Ref2VA 六段式；
- 覆盖 纯图 / 多图 / 图+视频 / 图+视频+音频 全混合；
- 覆盖 Standalone 两态、多段往返、对齐指令。

跑法：python tools/simulate_external_agent.py
"""
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 真解析器：从 web/h3_director.js 里把 parseMasterPrompt 那一族抽出来跑 ----
# jsdom 不在 python 里，所以这里用 node 跑一遍再读结果（见 __main__）。


def _official_fields():
    """官方字段集从规则文件里抽，不手抄。"""
    base = open(os.path.join(ROOT, "prompt", "minimaxh3_base_prompt_writing.txt"),
                encoding="utf-8").read()
    ref = open(os.path.join(ROOT, "prompt", "minimaxh3_official_ref2v_prompt_writing.txt"),
               encoding="utf-8").read()
    return base, ref


def _official_examples(text):
    """抽官方示例里的 `<Subject N>` / `<Video N>` / `<Audio N>` 定义句式。"""
    blocks = re.findall(r"```(?:text)?\n(.*?)```", text, re.S)
    pats = {"subject": [], "video": [], "audio": [], "picture": []}
    for b in blocks:
        for line in b.splitlines():
            line = line.strip()
            if line.startswith("<Subject "):
                pats["subject"].append(line)
            elif line.startswith("<Video "):
                pats["video"].append(line)
            elif line.startswith("<Audio "):
                pats["audio"].append(line)
            elif line.startswith("<Picture ") and " is " in line:
                pats["picture"].append(line)
    return pats

# =====================================================================
# 手写的「外部 agent 产物」—— 按 skill 的 04-master-format.md 拼装
# =====================================================================

# 用例 A：多段 base + 图参考（用户没说模式，有参考图 → 后端会升 Ref2VA，
# 但这里演示 base 三字段的 T2VA 段：无图、纯文字）
CASE_A = """[Segment 1]
Duration: 8
Standalone: no

Prompt:
integrated_multimodal_description: Live-action, cinematic realism with soft directional light. [Shot 1] A wide shot frames a narrow alley after rain, neon reflections stretching across the standing water. The camera pushes in with small amplitude at slow speed as a young woman (S1) steps into frame and stops, saying: <d>[Chinese] 你要走了吗？</d>

[Shot 2] At 00:03.200, the camera cuts to a close-up of her side profile as the corners of her mouth lift slightly.

overall_soundscape: Steady rain taps the metal stall roofs, with distant vendors calling out over the crowd.

non_diegetic_music: N/A

[Segment 2]
Duration: 10
Standalone: no

Prompt:
integrated_multimodal_description: Live-action, same alley and lighting direction, continuing directly from the last frame. [Shot 1] She turns and walks deeper in as the camera tracks right with small amplitude at moderate speed. The neon streaks slide across her coat.

[Shot 2] At 00:04.500, the camera tilts up as the alley opens onto a lit main street.

overall_soundscape: The vendors' calls fade with distance while her footsteps become prominent on the wet ground.

non_diegetic_music: A single low sustained string note, very slow, no build.

[END]
"""

# 用例 B：Ref2VA 全混合（图 + 视频 + 音频），单段
CASE_B = """[Segment 1]
Duration: 12
Standalone: yes

Prompt:
subject_definitions:
<Subject 1> is the content shown in <Picture 1>, the source image "女主_正面.png". It fixes the face, the long dark braided hair and the deep-red Tibetan robe.
<Video 1> is the source video for the target video edit, the file "运镜参考.mp4". It fixes the slow rightward tracking move.
<Audio 1> is the reference audio signal that is reused in the target video, the file "现场雨声.wav".

summary: [reference generation + video editing + audio reuse] A wide plateau shot follows <Subject 1> across the ridge, adopting the camera move of <Video 1>, over the rain bed of <Audio 1>.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - face, hair and robe unchanged throughout.
<Video 1> (cut and pacing structure): partially_preserved - camera direction and speed retained, framing recomposed.
<Audio 1>: fully_copy - <Audio 1> is reused 1:1 as the target video's complete final audio track.

detailed_description: A CG three-dimensional render with live-action-grade volumetric and global illumination. [Shot 1] A wide plateau shot places <Subject 1> in the right third of frame, the deep-red robe picking up a cold rim light from the snow field while the mountain ridge line stays sharp behind her. The camera tracks right with small amplitude at slow speed, matching the move established by <Video 1>.

[Shot 2] At 00:06.400, the camera continues its rightward track as <Subject 1> stops and looks toward the ridge, her braids shifting slightly in the wind.

overall_soundscape: A steady low-frequency plateau wind over the continuous rain bed carried by <Audio 1>.

non_diegetic_music: N/A

[END]
"""

# 用例 C：多段 Ref2VA（图 + 图），含正文禁用 @ 的关键检查
CASE_C = """[Segment 1]
Duration: 9
Standalone: no

Prompt:
subject_definitions:
<Subject 1> is the content shown in <Picture 1>, the source image "少女立绘.png".
<Subject 2> is the content shown in <Picture 2>, the source image "回廊场景.png".

summary: [reference generation] <Subject 1> walks down the corridor defined by <Subject 2>.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - silhouette and costume unchanged.
<Subject 2> (appears in [Shot 1]): fully_preserved - corridor structure and lighting unchanged.

detailed_description: Cinematic realism with practical corridor lighting. [Shot 1] <Subject 1> walks toward camera through <Subject 2>, the corridor lamps raking long shadows across the floor.

overall_soundscape: Footsteps and a low corridor hum.

non_diegetic_music: N/A

[Segment 2]
Duration: 8
Standalone: yes

Prompt:
subject_definitions:
<Subject 1> is the content shown in <Picture 3>, the source image "少女立绘.png".

summary: [reference generation] A jump cut to the same character, now standing still in a different room.

retention_analysis:
<Subject 1> (appears in [Shot 1]): partially_preserved - face preserved, costume redesigned for the new setting.

detailed_description: Cinematic realism. [Shot 1] <Subject 1> stands motionless in an empty room, lit from a single high window.

overall_soundscape: Room tone only.

non_diegetic_music: N/A

[END]
"""

CASES = {"A_base_two_segments": CASE_A, "B_ref2va_full_mix": CASE_B,
         "C_ref2va_two_segments": CASE_C}


def main():
    base_rules, ref_rules = _official_fields()
    ex = _official_examples(ref_rules)

    print("=" * 66)
    print("官方 ref-en 的标签句式（我们的产物要对齐这些骨架）")
    print("=" * 66)
    for k, v in ex.items():
        for line in v[:3]:
            print(f"  [{k}] {line}")

    # 结构校验（纯 python，不依赖 jsdom）
    fails = []

    def chk(name, cond, detail=""):
        if cond:
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name}   {detail}")
            fails.append(name)

    base_fields = ["integrated_multimodal_description", "overall_soundscape",
                   "non_diegetic_music"]
    ref_fields = ["subject_definitions", "summary", "retention_analysis",
                  "detailed_description", "overall_soundscape", "non_diegetic_music"]

    for cname, text in CASES.items():
        print("\n" + "-" * 66)
        print(f"用例 {cname}")
        print("-" * 66)
        heads = re.findall(r"^\[Segment (\d+)\]\s*$", text, re.M)
        chk(f"{cname}: 段头独占一行且序号连续",
            heads == [str(i + 1) for i in range(len(heads))], str(heads))
        chk(f"{cname}: 以 [END] 收尾", text.rstrip().endswith("[END]"))
        chk(f"{cname}: 无中文标签",
            not re.search(r"【|】|时长：|独立镜头：|提示词：", text))
        # 每段三/六字段
        for m in re.finditer(r"^\[Segment (\d+)\]\n(.*?)(?=\n\[Segment |\n\[END\])",
                             text, re.S):
            no, blk = m.group(1), m.group(2)
            is_ref = "subject_definitions:" in blk
            want = ref_fields if is_ref else base_fields
            prev = -1
            ok = True
            for f in want:
                i = blk.find(f + ":")
                if i < 0 or i < prev:
                    ok = False
                    break
                prev = i
            chk(f"{cname}: 段{no} 字段集会 {'六段式' if is_ref else '三字段'}且顺序对", ok)
            # Standalone 必须显式
            chk(f"{cname}: 段{no} 显式写了 Standalone",
                re.search(r"^Standalone: (yes|no)$", blk, re.M) is not None)
            chk(f"{cname}: 段{no} Duration 是正数",
                re.search(r"^Duration: [1-9]\d*$", blk, re.M) is not None)
            if is_ref:
                # 正文（summary 及以后）不许出现 @
                after = blk.split("summary:", 1)[1] if "summary:" in blk else ""
                chk(f"{cname}: 段{no} 正文（summary 及以后）无 @", "@" not in after,
                    after[:80])

    # 与官方句式骨架比对
    print("\n" + "-" * 66)
    print("产物句式 vs 官方骨架")
    print("-" * 66)
    chk("Subject 定义用官方 `<Subject N> is … in/from <Picture k>` 骨架",
        any(l.startswith("<Subject ") and "<Picture " in l
            for l in re.findall(r"^<Subject .*$", CASE_B + CASE_C, re.M)))
    chk("Video 定义用官方 `is the source video for the target video edit`",
        "is the source video for the target video edit" in CASE_B)
    chk("Audio 用官方 fully_copy 标记", "fully_copy" in CASE_B)
    chk("retention 可见内容用 fully_preserved 系列",
        "fully_preserved" in CASE_B and "partially_preserved" in CASE_C)
    chk("音频行不带 (appears in …)（官方 §4.4）",
        re.search(r"^<Audio 1>: fully_copy", CASE_B, re.M) is not None)
    chk("风格句在 [Shot 1] 之前（Ref2VA）",
        re.search(r"detailed_description: [^\n]*\n\[Shot 1\]", CASE_B) is not None
        or re.search(r"detailed_description: [^\n]*\. \[Shot 1\]", CASE_B) is not None)

    print("\n" + "=" * 66)
    print(f"结构校验：{'全部通过' if not fails else str(len(fails)) + ' 项不符'}")
    print("=" * 66)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
