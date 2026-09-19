"""库级 roles 标的首尾帧图，与段级 frame_img 锚指向同一文件时——库级那张必须失效。

用户截图：只设了段级尾帧图（start_bg.jpg），但库内这张图的 roles 还残留着
「首帧图」（之前在别的场景/项目手动打的标）。后端旧逻辑会把同一张图**同时**
挂到链级首帧 + 链级尾帧，AI 看到 subject_definitions 里 <Picture 1> 和 <Picture 2>
都指向它，于是产出"以 X 为首帧与结尾定格"——但用户只想要尾帧。

修法：在 `_head_seg` / `_tail_seg` 算出之后、has_first_eff / has_end_eff 之前，
按**文件路径**比较，库级与段级冲突则库级失效。roles 是用户手动打的标，
跨场景复用很常见（库内标过"首帧图"的图以后用作尾帧图也合理），库级不能
强压段级意图。

这个测试是**结构性**的：读源码断言关键互斥逻辑存在；不执行节点（避免
依赖 comfy_extras + ComfyUI 启动）。
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_lib_roles_and_segment_marks_are_mutually_exclusive():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    # 库级首帧 vs 段级尾帧：同文件 → 库级失效
    assert "_tail_seg and _head_lib" in src, \
        "库级首帧图（_head_lib）与段级尾帧图（_tail_seg）必须做互斥判定"
    assert "_head_seg and _tail_lib" in src, \
        "库级尾帧图（_tail_lib）与段级首帧图（_head_seg）必须做互斥判定"
    # 必须按**文件路径**比较（不是按 label 字符串）：
    # 池里 label=「女主」、段里 file=「assets/女主.png」时字符串不等，按 label 比会漏。
    assert "pool_file_of.get(_head_lib)" in src, \
        "互斥判定必须通过 pool_file_of（label→file）转成路径比较"
    assert "pool_file_of.get(_tail_lib)" in src, \
        "对称地，库级尾帧的互斥也要走 pool_file_of"
    # 必须有归一化（路径分隔符、前导 ./）
    assert "replace(\"\\\\\", \"/\")" in src or 'replace("\\\\", "/")' in src, \
        "路径归一化必须有（Windows/Unix 分隔符、前导 ./）"


def test_chain_head_label_only_used_when_seg_unset():
    """库级 roles 仍是"默认锚"语义——段级未设时才兜底；段级一旦设了，库级不得强压。

    优先级仍是 全局 first_frame → 段级 first → 库级 roles（兜底）。
    但**同文件互斥**得放在 has_first_eff 判定之前，否则"链级首帧" + "段级尾帧"
    指向同一文件时仍会双挂。
    """
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    # 必须早于 has_first_eff / has_end_eff
    idx_mutual = src.find("_tail_seg and _head_lib")
    idx_has = src.find("has_first_eff = 首帧图片 is not None")
    assert idx_mutual > 0 and idx_has > 0 and idx_mutual < idx_has, \
        "互斥判定必须早于 has_first_eff，否则链级首+段级尾仍会双挂"