# -*- coding: utf-8 -*-
"""进度事件时间线探针：思考档位下 progress 帧到底长什么样。

进度条画的就是这些帧。要确认两件事：
1. 「思考阶段」真的会持续一段时间（否则进度条一上来就是撰写，说明 thinking 没生效）；
2. reasoning_chars / content_chars / phase 的推进顺序合理，能画出单调不倒退的条。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import optimizer as opt  # noqa: E402

PROMPT = ("一个少女在哥特式石柱廊里回头看向镜头，肩上立着一个微缩的她自己，"
          "两人同时抬手。需要六字段官方格式：subject_definitions / summary / "
          "retention_analysis / detailed_description / overall_soundscape / non_diegetic_music。")


def run(model, thinking, effort, max_tokens=16384):
    cfg = opt.default_config()
    cfg.update({"model": model, "thinking": thinking, "reasoning_effort": effort,
                "max_tokens": max_tokens, "timeout": 300})
    fields = opt._glm_thinking_fields(cfg)
    rows = []
    t0 = time.monotonic()

    def on_ev(ev):
        rows.append((round(time.monotonic() - t0, 1), ev.get("phase"),
                     ev.get("reasoning_chars"), ev.get("content_chars")))

    try:
        text = opt.optimize_once(cfg, {"prompt": PROMPT, "task": "REF2VA",
                                       "duration": 5, "media": []}, on_progress=on_ev)
    except Exception as e:
        print(f"{model} thinking={thinking} effort={effort!r} 下发={fields}")
        print(f"  失败：{e}\n")
        return
    phases = []
    for r in rows:
        if not phases or phases[-1] != r[1]:
            phases.append(r[1])
    print(f"{model} thinking={thinking} effort={effort!r} 下发={fields}")
    print(f"  总耗时 {round(time.monotonic() - t0, 1)}s  进度帧 {len(rows)}  "
          f"正文 {len(text)} 字")
    print(f"  阶段序列 {phases}")
    print(f"  前 4 帧 {rows[:4]}")
    print(f"  后 2 帧 {rows[-2:]}\n")


if __name__ == "__main__":
    run("glm-4.6v", "disabled", "")
    run("glm-5.3-flash", "disabled", "")
    run("glm-5.3-flash", "enabled", "max")
