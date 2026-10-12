# -*- coding: utf-8 -*-
"""H3 视频 VAE 的**时间分块解码**（把那张一次性开口支票变成分期付款）。

═══ 为什么必须有这个文件 ═══

H3 的视频 VAE 会在**开始解码之前**把整段视频的完整像素缓冲一次性预分配到
主机内存（ComfyUI `comfy/sd.py:1270-1277` 预先 `torch.empty(decode_output_shape…)`；
VAE 自己的兜底路径 `comfy/ldm/minimax/vae.py:708-711`，注释写着 *"finalized chunks
stream out of VRAM so the full video never sits on the GPU"*）。

于是内存峰值 = **帧数 × 高 × 宽 × 3 × 4 字节**，与帧数线性相关，且与显存是否干净、
卸载开关是否开启**完全无关**。ComfyUI 自带的 VAE 分块（256px 空间 tile + 17 帧
时序块，一直开着）只把「算」分批，**结果**仍然是整段 —— 它不是这个问题的解药。

本模块做的是另一层：**沿时间把 latent 切成 K 段，每段单独调一次 `decode()`**，
每次的预分配缓冲只有 1/K，解完就释放，峰值随之降到约 1/K（累积区用 uint8，占 ¼）。

═══ 接缝问题的真相（2026-10-13 查证，这一节就是安全性的全部）═══

VAE 内部每个时序块都会**丢弃开头的 `frame_pre_padding` 帧**
（`decode_temporal` 里 `clip_dec_chunk[:, :, self.frame_pre_padding:, :, :]`）。
H3 的实际参数是 `vae_ratio_t=4`、`clip_length=17` → `frame_pre_padding = (-17) % 4 = 3`。

后果：把 latent 一刀切开后，每一段**各自**都要丢掉开头 3 帧 →
拼接处的**内容**会跳过 3 帧（约 0.125 秒）。帧数倒是能对上（丢的次数随块数同增），
但内容不连续。

═══ 安全契约：账对不上就**不分块** ═══

帧数对不对，是这类改动唯一真正致命的地方（帧数错了 → 音画不同步、段长漂移、
成片错位）。所以本模块**先算账再动手**：

  1. 用 VAE 自己的公式（`decode_output_shape` / `_decode_temporal_frame_plan`
     的整数版复刻）算出「整段解出多少帧」和「每段各解出多少帧」；
  2. **只有** `sum(每段帧数) == 整段帧数` 才允许分块；
  3. 对不上 → 返回 `ok=False`，调用方照旧走一次完整 `decode()`。

**这意味着：要么它算得完全正确，要么它的行为与没装它时逐字相同。**
宁可这次没省到内存，也不产出一段帧数错的视频。

═══ 已知边界（如实写，别吹）═══

- 拼接处有 `frame_pre_padding`（H3 为 3 帧）的内容跳跃。可用 `blend` 参数做几帧
  交叉溶解来柔化，**但那只是把硬切变成短溶解，不是真正消除** —— 真正无缝需要
  在 latent 上做重叠解码，而那会让帧数对不上（实测：重叠 1 token 就多/少 1 帧），
  被上面的账目校验直接拒绝。这是官方 VAE 的结构决定的，不是实现偷懒。
- 默认**关闭**。要省内存请显式开，并自己看一眼接缝。
"""

from __future__ import annotations

# H3 VideoVAE 的实际构造参数（`comfy/ldm/minimax/vae.py:408-440`），单测用它当真值。
# space_down=(2,2,2,2,1,1) → vae_ratio=16；time_down=(1,2,2,1,1,1) → vae_ratio_t=4
H3_VAE_RATIO_T = 4
H3_CLIP_LENGTH = 17
H3_TOKEN_DROP = 3


def vae_params(vae):
    """从 first_stage_model 上读时序分块参数；拿不到 → None（调用方据此放弃分块）。"""
    m = getattr(vae, "first_stage_model", None)
    if m is None:
        return None
    try:
        return {
            "vae_ratio_t": int(m.vae_ratio_t),
            "clip_length": int(m.clip_length),
            "token_drop": int(m.token_drop),
            "frame_pre_padding": int(m.frame_pre_padding),
            "tokens_chunk_size": int(m.tokens_chunk_size),
            "token_overlap": int(m.token_overlap),
        }
    except Exception:
        return None


def _chunks(p, z_len):
    """复刻 `vae.py:692 _decode_temporal_chunks`。"""
    tcs = p["tokens_chunk_size"]
    pseudo = z_len + p["token_drop"]
    pad = (-pseudo) % tcs
    pseudo += pad
    n = pseudo // tcs - int(p["token_drop"] > 0)
    if n < 1:
        pad += tcs
        n = 1
    return pad, n


def _pad_frames(p, z_len, pad_tokens):
    """复刻 `vae.py:654 _decode_temporal_pad_frames`。"""
    if pad_tokens <= 0:
        return 0
    rt = p["vae_ratio_t"]
    intra_tail = p["clip_length"] % rt
    if intra_tail == 0:
        return pad_tokens * rt
    tcs = p["tokens_chunk_size"]
    z_before = z_len - pad_tokens
    return sum((intra_tail if (z_before + k) % tcs == 0 else rt)
               for k in range(pad_tokens))


def frames_for(p, z_len):
    """复刻 `vae.py:668 _decode_temporal_frame_plan`：z_len 个 token 解出多少帧。

    **这是整个分块方案的地基** —— 拼接正确性全靠它算得跟 VAE 一模一样。
    单测 `tests/test_vae_chunk.py` 用 H3 的真实参数把它钉死（30 token → 102 帧，
    与手算 `30×4 − 3×6` 一致）。
    """
    rt = p["vae_ratio_t"]
    tcs = p["tokens_chunk_size"]
    fpp = p["frame_pre_padding"]
    chunk_dec = tcs * rt
    split_count = int(p["token_drop"] > 0) + 1
    pad, n = _chunks(p, z_len)
    z = z_len + pad

    total = 0
    final_overlap = 0
    for i in range(n):
        t_start = i * tcs
        t_end = t_start + tcs + p["token_overlap"]
        clip_token_len = max(0, min(t_end, z) - min(t_start, z))
        clip_frame_len = clip_token_len * rt
        for j in range(split_count):
            f_start = j * chunk_dec
            f_end = min(f_start + chunk_dec, clip_frame_len)
            cf = max(0, f_end - f_start - fpp)
            if j == 0:
                total += cf
            else:
                final_overlap = cf
    total += final_overlap
    return total - _pad_frames(p, z, pad)


def plan(vae, z_len, max_tokens):
    """把 z_len 个 latent token 切成若干段；**账对不上就拒绝分块**。

    返回 {"ok", "pieces"=[(a,b)…], "frames_each"=[…], "frames_total", "why"}。
    `ok=False` 时调用方必须照旧走一次完整解码 —— 这是本模块唯一的安全出口。
    """
    out = {"ok": False, "pieces": [], "frames_each": [], "frames_total": 0,
           "why": ""}
    p = vae_params(vae)
    if p is None:
        out["why"] = "读不到 VAE 的时序分块参数"
        return out
    try:
        mt = int(max_tokens)
    except (TypeError, ValueError):
        mt = 0
    if z_len <= 0 or mt <= 0 or z_len <= mt:
        out["why"] = f"段长 {z_len} token 不超过每块上限 {mt} token —— 无需分块"
        return out

    whole = frames_for(p, z_len)
    tcs = p["tokens_chunk_size"]
    # 候选段长：**从大到小**试。段越长接缝越少（每多一处接缝就多一次
    # `frame_pre_padding` 帧的内容跳跃），能两三档解决就别切十几段。
    #
    # ⚠ 段长必须向**下**对齐到 VAE 时序块大小的整数倍：向上对齐会超过
    # `max_tokens`（缓冲反而更大，分块就白做了）；而按 `step*i` 各自对齐则会在
    # step 不是块大小整数倍时切出 15 > 12 这种越界段。两处都由单测钉着。
    biggest = max(tcs, (min(mt, z_len) // tcs) * tcs)
    for piece in range(biggest, tcs - 1, -tcs):
        cuts = list(range(0, z_len, piece))
        # 尾段太碎就并进上一段：一个 1 token 的尾段只解出 1 帧，而那 1 帧基本是
        # 尾部 padding 的垃圾 —— 白付一次 decode 调用，还把末尾污染了。
        if len(cuts) >= 2 and z_len - cuts[-1] < tcs:
            cuts.pop()
        cuts.append(z_len)
        if len(cuts) < 3:
            continue
        pieces = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]
        pieces = [(a, b) for a, b in pieces if b > a]
        if len(pieces) < 2:
            continue
        if any(b - a < tcs for a, b in pieces):
            continue                                # 有太碎的段 → 换一档
        each = [frames_for(p, b - a) for a, b in pieces]
        if sum(each) != whole:
            continue                                # 账不对 → 换一档再试
        out.update({"ok": True, "pieces": pieces, "frames_each": each,
                    "frames_total": whole,
                    "why": f"{len(pieces)} 段 × ≤{max(each)} 帧"})
        return out
    out["why"] = (f"所有切法都凑不出与整段一致的帧数（整段 {whole} 帧）"
                  f"—— 保守起见本次不分块")
    return out


def _blend_join(prev, nxt, blend):
    """把 prev 的**尾 n 帧原地替换**成「prev 尾 → nxt 头」的线性斜坡（帧数不变）。

    为什么要它：见文件头「接缝问题的真相」—— 每段会各自丢掉开头
    `frame_pre_padding`（H3 是 3）帧，拼接处的**内容**因此有 3 帧跳跃。
    斜坡让 prev 的收尾平滑地爬到 nxt 的开头，把「硬跳」变成「短溶解」。

    ⚠ **为什么是「原地替换 prev 的尾帧」而不是「丢弃 nxt 的头帧」**：
    帧数是这类改动唯一真正致命的东西（错了就音画不同步、段长漂移），而
    `plan()` 的账目校验保证 `sum(每段帧数) == 整段帧数`。任何丢弃帧的溶解都会
    破坏这个等式。所以这里只改 prev 尾部的**像素**，两个张量的帧数都不动。

    **它没有消除那 3 帧的内容跳跃** —— 真正无缝需要在 latent 上重叠解码，而那会
    让帧数对不上（实测重叠 1 token 就差 1 帧），会被账目校验直接拒绝。这是官方
    VAE 的结构决定的。
    """
    if blend <= 0 or prev is None or nxt is None:
        return prev, nxt
    try:
        n = int(min(blend, prev.shape[2], nxt.shape[2]))
    except Exception:
        return prev, nxt
    if n <= 0:
        return prev, nxt
    try:
        import torch

        # ramp 取 (0,1) 开区间：首尾帧保持原样，中间渐变，避免出现半帧黑边
        ramp = torch.linspace(0.0, 1.0, n + 2, dtype=prev.dtype,
                              device=prev.device)[1:-1]
        ramp = ramp.reshape((1, 1, n) + (1,) * (prev.dim() - 3))
        tail = prev[:, :, -n:, :, :]
        head = nxt[:, :, :n, :, :].to(dtype=prev.dtype)
        mixed = tail * (1.0 - ramp) + head * ramp
        prev = torch.cat([prev[:, :, :-n, :, :], mixed], dim=2)
    except Exception:
        pass                      # 溶解失败不影响拼接本身：退回硬切，帧数仍然正确
    return prev, nxt


def decode_chunked(vae, z, pl, blend=0, log=None):
    """按 `plan` 的切法分块解码并拼回去。**任一步异常都向上抛，由调用方兜底**。

    返回拼接后的帧张量。时间维取 `dim=2`（ComfyUI 视频 VAE 的输出是
    [B, C, T, H, W]；`comfy/sd.py` 的 `process_output` 之后才是 [B, T, H, W, C]，
    两种形态的时间维都在 dim=2，所以这里统一按 dim=2 拼）。
    """
    parts = []
    prev = None
    for i, (a, b) in enumerate(pl["pieces"]):
        piece = vae.decode(z[:, :, a:b, :, :])
        if prev is not None:
            prev, piece = _blend_join(prev, piece, blend)
            parts.append(prev)
            prev = None
        prev = piece
        if log is not None:
            try:
                log(f"  分块解码 {i + 1}/{len(pl['pieces'])}："
                    f"token {a}..{b} → {int(piece.shape[2])} 帧")
            except Exception:
                pass
    if prev is not None:
        parts.append(prev)
    try:
        import torch

        out = torch.cat(parts, dim=2)
    except Exception:
        raise
    finally:
        del parts
    # 最后一道闸：帧数必须等于整段应得的帧数，否则整段结果作废（调用方会退回整解）
    if int(out.shape[2]) != int(pl["frames_total"]):
        raise RuntimeError(
            f"分块解码帧数不符：拼出 {int(out.shape[2])} 帧，"
            f"整段应为 {int(pl['frames_total'])} 帧")
    return out
