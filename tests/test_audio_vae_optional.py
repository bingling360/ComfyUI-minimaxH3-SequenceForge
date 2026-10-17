"""「音频VAE」改为可选（无音频模式）——回归守卫（无 ComfyUI 可跑）。

跑法（仓库根目录）：
    python -m pytest tests/test_audio_vae_optional.py -q

背景：主节点原来把「音频VAE」做成必填槽，于是**没有音频 VAE 权重的人根本跑不了整条链**
（纯画面迁移 / 转码「仅图像」分支都被一个用不上的输入卡住）。2026-10-17 起改成可选：
不接 = 全链静音（不编码也不解码音轨），参考音频仍进文本编码器参与画面描述。

钉三件事：
  ① schema 与 execute 外壳：可选声明 + 缺省补 None（否则缺位置参数直接 TypeError）；
  ② 静音占位 _silent_wav 的形态与采样率（必须与 _decode_audio 一致，下游才不用分叉）；
  ③ 所有「拿 audio_vae 干活」的调用点都被 无音频 守卫——漏一处就是 NoneType 崩。
"""

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODES = os.path.join(ROOT, "nodes.py")


def _src():
    with open(NODES, encoding="utf-8") as fh:
        return fh.read()


def test_audio_vae_is_optional_in_schema():
    """① 可选声明 + execute 外壳缺省补 None。"""
    src = _src()
    assert 'io.Vae.Input("音频VAE", optional=True' in src, \
        "「音频VAE」必须是 optional——否则没这份权重的人跑不了纯画面链"
    assert 'kwargs.setdefault("音频VAE", None)' in src, \
        "未接线的可选输入不进 kwargs，缺这句 _execute_inner 会 TypeError"
    # 视频 VAE 仍是必填（任何链路都要解码画面）——别顺手把它也放开
    assert 'io.Vae.Input("视频VAE")' in src


def test_silent_wav_shape_and_rate():
    """② 静音占位：形态 [1,C,T] 与时长换算（AST 抽取 shipped 代码真跑）。"""
    import torch

    tree = ast.parse(_src())
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef)}
    assert "_silent_wav" in fns
    const = next(n for n in tree.body
                 if isinstance(n, ast.Assign)
                 and getattr(n.targets[0], "id", "") == "AUDIO_SR_NO_VAE")
    ns = {"torch": torch}
    exec(compile(ast.Module(body=[const, fns["_silent_wav"]], type_ignores=[]),
                 "nodes.py:_silent_wav", "exec"), ns)
    silent, rate = ns["_silent_wav"](48)          # 48 帧 @24fps = 2.0s
    assert rate == 32000, rate                    # 与 H3 音频 VAE 输出同率
    assert tuple(silent.shape) == (1, 2, 64000), tuple(silent.shape)
    assert float(silent.abs().max()) == 0.0
    # 0 帧不炸、形状仍是 [1,C,T]（下游 save_av_mp4 按 T 切片）
    w0, _ = ns["_silent_wav"](0)
    assert tuple(w0.shape) == (1, 2, 0), tuple(w0.shape)


@pytest.mark.parametrize("needle", ["_decode_audio(audio_vae", "_encode_audio_latent(audio_vae"])
def test_every_audio_vae_use_is_guarded(needle):
    """③ 每个真正用 audio_vae 干活的调用点，附近必须有 无音频 守卫。

    守卫可能在上一行（三元表达式跨行），也可能在几行之前的 raise（报错文案占 3 行），
    故回看 5 行窗口；这是**粗粒度但有回声**的守卫：漏改一处，无音频模式第一次跑到
    那儿就是 AttributeError: 'NoneType'。
    """
    lines = _src().splitlines()
    # 函数定义本身也含同样的形参名，不算「调用点」（它们是声明，不是使用）
    hits = [i for i, ln in enumerate(lines)
            if needle in ln and not ln.lstrip().startswith("def ")]
    assert hits, f"没找到 {needle}（守卫本身失效了？）"
    for i in hits:
        window = "\n".join(lines[max(0, i - 5):i + 1])
        assert "无音频" in window, \
            f"{needle} @行{i + 1} 没有「无音频」守卫：无音频模式会在这里撞 None"


def test_no_audio_mode_wired_once():
    """无音频模式的开关与设备回落只有一处定义（别在各处各判一次，口径会分叉）。"""
    src = _src()
    assert "无音频 = audio_vae is None" in src
    assert src.count("def _audio_dev(") == 1
    # 音频张量落设备只剩 _audio_dev() 那一处判据（无音频时随视频 VAE）；
    # 谁再直接摸 audio_vae.device，无音频模式下就是 AttributeError
    assert src.count("audio_vae.device") == 1
