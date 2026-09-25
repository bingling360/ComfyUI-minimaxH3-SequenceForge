"""精化空间 tile 融合正确性验证（真 torch 环境独立跑，不依赖 pytest）。

为什么是独立脚本：本仓库没有「pytest + torch 双全」的环境（见 MEMORY）：
  · managed python 3.13.12 有 pytest 但**没 torch**
  · <根>/.venv 有 torch 但**没 pytest**
所以真 torch 的断言走独立 runner，退出码就是结论。

跑法：
    D:/.../ComfyUI/ComfyUI/.venv/Scripts/python.exe tools/check_refine_tile_fusion.py
退出码 0 = 全部通过。

钉的是「tile 融合」这条**正确性根基**（不是画质 —— 块边抖动是 tile 的固有代价，
本脚本不假装能消除它）：
  ① 各块 core 不重不漏铺满整幅 —— 有洞=黑洞、有重叠=白算
  ② 权重归一化后，若「各块采样结果 == 原 latent」（理想收敛），融合输出**逐位等于**原 latent
  ③ 二维羽化权重 = 行权重 × 列权重，且核区恒 1
  ④ 只有 tile 开、时序关时也真的会切（不能被时序层的直通吞掉）
  ⑤ **采样输出在 CPU 时融合仍成立** —— `comfy.sample.sample` 收尾恒把结果丢到
     `intermediate_device()`（无 `--gpu-only` 时 = CPU），累加器却在 cuda；
     旧代码只写 `.to(acc.dtype)`，线上第一次融合就崩（2026-09-25）。

这三条是数学性质，与具体 H3 权重无关 —— 用随机张量即可判定。
"""

import os
import sys
import types

import torch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _stub_comfy():
    """桩掉 comfy 家族（upscale.py 导入期需要）。"""
    def mk(name):
        m = types.ModuleType(name)
        m.__path__ = []
        sys.modules[name] = m
        return m

    for sub in ("", ".model_management", ".quant_ops", ".ops",
                ".ldm", ".ldm.modules", ".ldm.modules.attention",
                ".ldm.minimax", ".ldm.minimax.model"):
        mk("comfy" + sub)


def _load_upscale():
    """把插件目录造成合成包再 import upscale（目录名含连字符，不能当正规包名）。"""
    import importlib

    pkgname = "h3seqforge"
    if pkgname not in sys.modules:
        pkg = types.ModuleType(pkgname)
        pkg.__path__ = [ROOT]
        pkg.__package__ = pkgname
        sys.modules[pkgname] = pkg
    return importlib.import_module(pkgname + ".upscale")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


class _Skip(Exception):
    """环境不满足（如本机无 CUDA）——不算通过也不算失败，报告里显式标出。"""


class FakeNested:
    """NestedTensor 的最小同构体：`.tensors` 元组 + 下标访问（upscale 里两种都用）。"""

    def __init__(self, tensors):
        self.tensors = tuple(tensors)

    def __getitem__(self, i):
        return self.tensors[i]


def _fuse(vid, aud, mode, ov, feather, sampled=None):
    """复刻 `_refine_tiled` 的融合数学（不跑采样：用 `sampled` 直接替换「采样结果」）。

    `sampled` = None 表示「各块采样结果 == 切下来的输入」（理想收敛，融合应恒等）。
    否则 `sampled(vid_piece, ti)` 返回该块采样后的张量，用来验证「不同的块结果
    在 overlap 区按权重过渡、在 core 区各自生效」。

    ⚠ 融合前的「采样输出 → 累加器」那一步**必须调真代码的 `_sampler_out_to`**，
    不能自己写 `.to(acc.dtype)`：本副本早先就是那么写的，于是永远测不出
    「采样输出在 CPU、累加器在 cuda」这条真实故障（2026-09-25 线上踩到）。
    """
    from h3seqforge import perf
    up = sys.modules["h3seqforge.upscale"]

    H, W = int(vid.shape[3]), int(vid.shape[4])
    plan = perf.plan_tiles(H, W, mode, ov)
    acc = torch.zeros_like(vid)
    wsum = torch.zeros((1, 1, 1, H, W), device=vid.device, dtype=torch.float32)
    for ti, (hs, he, ws, we, chs, che, cws, cwe, eff_ov) in enumerate(plan):
        piece = vid[:, :, :, hs:he, ws:we]
        out = sampled(piece, ti) if sampled is not None else piece
        sh = up._sampler_out_to(out, acc)
        # 权重算在**写回区（core）**坐标系上；ramp 上限 = 实际生效 overlap
        rh = min(feather, eff_ov)
        rw = min(feather, eff_ov)
        wh = perf.edge_weights(che - chs, rh, chs > 0, che < H)
        ww = perf.edge_weights(cwe - cws, rw, cws > 0, cwe < W)
        wv = (torch.tensor(wh, device=acc.device, dtype=torch.float32).view(1, 1, 1, -1, 1)
              * torch.tensor(ww, device=acc.device, dtype=torch.float32).view(1, 1, 1, 1, -1))
        ah, bh = chs - hs, che - hs
        aw, bw = cws - ws, cwe - ws
        acc[:, :, :, chs:che, cws:cwe] = (
            acc[:, :, :, chs:che, cws:cwe] + sh[:, :, :, ah:bh, aw:bw] * wv)
        wsum[:, :, :, chs:che, cws:cwe] = (
            wsum[:, :, :, chs:che, cws:cwe] + wv)
    bad = (wsum <= 0).any().item()
    assert not bad, "core 并集有空洞（wsum<=0）—— 融合不变量破了"
    return (acc / wsum), plan


def main():
    _stub_comfy()
    up = _load_upscale()

    results = []

    def check(name, fn):
        try:
            fn()
            results.append(("OK  ", name))
        except _Skip as e:
            results.append(("SKIP", f"{name} :: {e}"))
        except AssertionError as e:
            results.append(("FAIL", f"{name} :: {e}"))
        except Exception as e:                        # noqa: BLE001
            results.append(("ERR ", f"{name} :: {type(e).__name__}: {e}"))

    torch.manual_seed(0)

    # ① 理想收敛下融合 = 恒等（逐元素）—— 这是「权重归一化对不对」的硬判据
    def t_identity():
        for (B, C, T, H, W) in ((1, 4, 3, 64, 64), (1, 2, 2, 96, 128), (1, 1, 1, 33, 37)):
            vid = torch.randn(B, C, T, H, W, dtype=torch.float32)
            for mode in ("2x2", "3x3", "4x4", "2x1", "1x2"):
                for feather in (0, 4, 16):
                    got, plan = _fuse(vid, None, mode, 16, feather)
                    if len(plan) <= 1:
                        continue
                    assert got.shape == vid.shape, f"{mode} 形状变了"
                    assert torch.allclose(got, vid, atol=1e-4, rtol=1e-4), (
                        f"{mode} feather={feather} 理想收敛下融合 != 原 latent"
                        f"（最大偏差 {float((got - vid).abs().max())}）")
    check("理想收敛下融合 == 恒等（5 档位 × 3 羽化 × 3 形状）", t_identity)

    # ② 权重归一化：feather == overlap 时 wsum 处处为 1
    #    （羽化带与咬合带等宽 = 严格互补，是设计配置）
    def t_wsum_one():
        vid = torch.randn(1, 2, 2, 64, 64)
        from h3seqforge import perf
        h = w_ = 64
        for mode in ("2x2", "3x3"):
            for ov in (8, 16):
                _got, plan = _fuse(vid, None, mode, ov, ov)
                wsum = torch.zeros((h, w_))
                for (_hs, _he, _ws, _we, chs, che, cws, cwe, fo) in plan:
                    rh = min(ov, fo)
                    rw = min(ov, fo)
                    wh = perf.edge_weights(che - chs, rh, chs > 0, che < h)
                    ww = perf.edge_weights(cwe - cws, rw, cws > 0, cwe < w_)
                    ww2 = torch.tensor(wh).view(-1, 1) * torch.tensor(ww).view(1, -1)
                    wsum[chs:che, cws:cwe] += ww2
                assert torch.allclose(wsum, torch.ones_like(wsum), atol=1e-5), (
                    f"{mode} ov={ov} 的权重和不为 1（min={wsum.min()} max={wsum.max()}）")
    check("feather==overlap 时融合权重处处 == 1（2 档位 × 2 重叠）", t_wsum_one)

    # ③ 各块给不同常量偏移：核区=本块值、咬合带=互补混合、画布外缘不放大
    def t_2d_blend():
        vid = torch.randn(1, 1, 1, 64, 64)
        # plan 顺序：行外列内。(0,0)→+10 (0,1)→+20 (1,0)→+30 (1,1)→+40
        def sampled(piece, ti):
            return piece + float(ti + 1) * 10.0
        got, plan = _fuse(vid, None, "2x2", 16, 16, sampled=sampled)
        d = (got - vid)
        # 块 (0,0) 核心（远离咬合带）应恰为 +10
        assert abs(d[0, 0, 0, 8, 8].item() - 10.0) < 1e-3, \
            f"块(0,0)核心该=10，实际 {d[0, 0, 0, 8, 8].item()}"
        # 画布外缘（行 0 / 列 0）也必须恰为本块值 10（**不能被归一化放大**）
        assert abs(d[0, 0, 0, 0, 0].item() - 10.0) < 1e-3, \
            f"画布角(0,0)该=10（外缘不羽化），实际 {d[0, 0, 0, 0, 0].item()}"
        assert abs(d[0, 0, 0, 0, 64 - 1].item() - 20.0) < 1e-3, \
            f"画布右上角该=(0,1)的20，实际 {d[0, 0, 0, 0, 63].item()}"
        # 咬合带：块(0,0)与(1,0) 的 core 在行 [24,40) 重叠（ov=16, 32±8）
        #   行 24..31 → 更偏向 (0,0)=10；行 32..39 → 更偏向 (1,0)=30
        top = d[0, 0, 0, 25, 8].item()     # 咬合带靠上
        bot = d[0, 0, 0, 38, 8].item()     # 咬合带靠下
        assert 10.0 - 1e-3 <= top < 30.0, f"咬合带靠上该介于 10..30，实际 {top}"
        assert 10.0 < bot <= 30.0 + 1e-3, f"咬合带靠下该介于 10..30，实际 {bot}"
        assert top < bot, f"咬合带该单调过渡，实际 top={top} bot={bot}"
        # 咬合带中点应接近两值均值 20（互补羽化）
        mid = d[0, 0, 0, 32, 8].item()
        assert abs(mid - 20.0) < 4.0, f"咬合带中点该接近 20，实际 {mid}"
    check("核区=本块值 / 外缘不放大 / 咬合带互补混合", t_2d_blend)

    # ④ core 的**并集**铺满整幅（不再要求「不重」—— 咬合带本就该重叠）
    def t_plan_covers():
        from h3seqforge import perf
        for (H, W, mode, ov) in ((64, 64, "2x2", 8), (96, 128, "3x3", 16),
                                 (100, 60, "4x4", 32), (33, 37, "2x2", 4)):
            plan = perf.plan_tiles(H, W, mode, ov)
            seen = set()
            for (_hs, _he, _ws, _we, chs, che, cws, cwe, _ov) in plan:
                for y in range(chs, che):
                    for x in range(cws, cwe):
                        seen.add((y, x))
            assert seen == {(y, x) for y in range(H) for x in range(W)}, \
                f"{H}x{W} {mode} core 并集未铺满"
    check("各档位 core 并集铺满整幅（4 组形状）", t_plan_covers)

    # ⑤ 咬合确实发生：相邻 core 在内部边界处重叠 `ov` 个像素（不是硬切）
    def t_interlock():
        from h3seqforge import perf
        H = W = 64
        for mode, ov in (("2x2", 8), ("2x2", 16), ("3x3", 12), ("2x1", 8)):
            plan = perf.plan_tiles(H, W, mode, ov)
            # 逐列统计「被几个 core 覆盖」；有内部列边界的列必须被 2 个覆盖
            cover = [[0] * W for _ in range(H)]
            for (_hs, _he, _ws, _we, chs, che, cws, cwe, _ov) in plan:
                for y in range(chs, che):
                    for x in range(cws, cwe):
                        cover[y][x] += 1
            # 至少存在某列被 >=2 个 core 覆盖（= 咬合带）
            maxc = max(max(r) for r in cover)
            assert maxc >= 2, f"{mode} ov={ov} 没有任何重叠 —— 仍是硬切"
            # 被 >1 覆盖的总列/行数应约等于 ov * 内部边界数
            mult = sum(1 for r in cover for v in r if v >= 2)
            assert mult > 0
    check("相邻 core 在内部边界真实咬合（非硬切）", t_interlock)

    # ⑥ 只有一个尺度：只切 H 或只切 W 时另一维 core 必须整段保留
    def t_single_axis():
        from h3seqforge import perf
        for mode in ("2x1", "1x2"):
            plan = perf.plan_tiles(64, 64, mode, 8)
            for (_hs, _he, _ws, _we, chs, che, cws, cwe, _ov) in plan:
                if mode == "2x1":      # 只切行 -> 列必须整幅
                    assert (cws, cwe) == (0, 64), f"{mode} 列被误切：{cws},{cwe}"
                else:                  # 只切列 -> 行必须整幅
                    assert (chs, che) == (0, 64), f"{mode} 行被误切：{chs},{che}"
    check("单轴档位（2x1 / 1x2）另一维整段保留", t_single_axis)

    # ⑦ 时序分块（批次 3）的咬合与互补 —— 与 tile 同一套融合数学
    def t_temporal_interlock():
        from h3seqforge import perf
        for (n, c, ov) in ((120, 16, 8), (100, 20, 10), (64, 12, 4)):
            plan = perf.plan_refine_chunks(n, c, ov)
            assert plan[0][2] == 0 and plan[-1][3] == n, "首/末段 core 未贴链端"
            eff = max(0, min(ov, c // 2))
            # core == span（段只写自己采样过的）
            for (s, e, cs, ce) in plan:
                assert (cs, ce) == (s, e), f"core != span：({cs},{ce}) vs ({s},{e})"
            # 相邻段咬合带宽度 = eff
            for (_s0, e0, _a0, _b0), (s1, _e1, _a1, _b1) in zip(plan, plan[1:]):
                if s1 < e0:
                    assert e0 - s1 == eff or eff == 0, \
                        f"咬合带该 {eff}，实际 {e0 - s1}"
            # 权重互补：段 i 尾 eff 个 + 段 i+1 首 eff 个 == 1
            if eff > 0 and len(plan) >= 2:
                w0 = perf.edge_weights(plan[0][1] - plan[0][0], eff, False, True)
                w1 = perf.edge_weights(plan[1][1] - plan[1][0], eff, True, False)
                for k in range(eff):
                    tot = w0[len(w0) - eff + k] + w1[k]
                    assert abs(tot - 1.0) < 1e-9, \
                        f"时序咬合带第 {k} 点不互补：{w0[len(w0) - eff + k]} + {w1[k]} = {tot}"
    check("时序分块咬合 + 权重互补（3 组参数）", t_temporal_interlock)

    # ⑧ 采样输出在 **CPU**（`comfy.sample.sample` 的真实返回设备）时融合仍成立
    #
    # 这是线上踩到的原样：`comfy/sample.py:83` 收尾把采样结果
    # `.to(intermediate_device())`，而 `intermediate_device()` 在没加 `--gpu-only`
    # 时恒为 CPU；融合累加器却是 `torch.zeros_like(_vid)`（cuda）。
    # 旧代码只写 `.to(acc.dtype)`（不搬设备）→ 第一次融合就
    # `RuntimeError: Expected all tensors to be on the same device … cuda:0 and cpu!`
    #
    # ⚠ 只有在**有 CUDA** 的机器上才有判别力：纯 CPU 环境里 acc 与 sh 同设备，
    # 本用例会退化成恒真（所以它是「有卡则真测，无卡则明说跳过」）。
    def t_sampler_out_on_cpu():
        dev = "cuda" if torch.cuda.is_available() else None
        if dev is None:
            raise _Skip("本机无 CUDA：acc 与采样输出同设备，该用例无判别力")
        vid = torch.randn(1, 2, 2, 64, 64, device=dev, dtype=torch.float32)

        def sampled(piece, ti):
            # 采样输出的真实形态：算完在 cuda、交回时已在 CPU
            return (piece + 0.0).to("cpu")

        got, plan = _fuse(vid, None, "2x2", 16, 16, sampled=sampled)
        assert len(plan) > 1, "该用例需要真的切块"
        assert got.device.type == "cuda", f"融合结果该在 cuda，实际 {got.device}"
        assert torch.allclose(got.cpu(), vid.cpu(), atol=1e-4, rtol=1e-4), \
            "采样输出在 CPU 时融合结果不等于原 latent"
    check("采样输出在 CPU 时融合仍成立（真机 cuda 判别）", t_sampler_out_on_cpu)

    print()
    bad = 0
    skipped = 0
    for tag, msg in results:
        print(f"  {tag} {msg}")
        if tag == "SKIP":
            skipped += 1
        elif tag != "OK  ":
            bad += 1
    print()
    _tail = f"（跳过 {skipped} 项：环境不满足）" if skipped else ""
    print("check_refine_tile_fusion：全部通过" + _tail if not bad
          else f"check_refine_tile_fusion：{bad} 项失败{_tail}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
