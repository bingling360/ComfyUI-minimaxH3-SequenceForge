"""自定义 sigma 表适配（少步蒸馏 LoRA 通道）。

背景
----
少步蒸馏 LoRA（HyperFlow 8 步、H3 Turbo 等）是**带着自己的 sigma 表**训练的：
训练时每步吃的是表里那一段区间，不是「步数 + 调度器」现算出来的表。用
「步数=8 + simple/euler」重算的表和蒸馏轨迹不是一回事，出来的画面会偏。

所以要接这类 LoRA，采样必须能**直接吃外部 sigmas**（等价于官方
`SamplerCustomAdvanced` 的 sigmas 输入），而 `nodes.common_ksampler` 没有这个口子。

本模块提供四件事：

* `ksampler_with_sigmas()` —— `nodes.common_ksampler` 的逐行等价实现 + 可选 sigmas 覆盖。
  sigmas 为 None 时与官方实现**逐字节一致**（旧工作流结果不变），
  非 None 时走 `comfy.sample.sample(sigmas=...)` 覆盖掉「步数」与「调度器」。
* `fingerprint_tag()` —— sigma 表进存档指纹的短标签。
* `sigmas_start()` / `sigmas_steps()` —— 表首值（实际 σ 起点）与表长（实际步数），
  报告回显与 STG 激活窗口共用。
* `sigmas_usable()` —— 「这张表真能用吗」的**唯一判据**（点数 ≥ 2）。「写不写指纹」
  与「按不按表采样」必须同用它，否则空表/坏表会落到「指纹变了、采样没变」的白做。

两个入口共用本模块（2026-10-12）：
* **一采**（基础链，`H3 Seamless Chain` 的「一采自定义Sigmas」槽）——原路径；
* **二采**（潜空间放大高清精化，同节点的「二采自定义Sigmas」槽）——见
  `upscale.render_latent` 的 `sigmas` 形参：二采换了带自己 sigma 表的蒸馏权重时，
  「去噪 + 精化步数 + 调度器」现算的表同样对不上蒸馏轨迹。
  二采是**尾段精化**语义，所以那张表应从目标 σ 起步（首值即精化起点）、末值为 0；
  照搬一采那张 1.0→0 的完整表等于整段重画。

为什么单独成模块：这些函数只依赖 torch 与 comfy 的延迟导入，可以脱离
ComfyUI 在 pytest 里单测（nodes.py 顶层就要 import comfy_api，测不了）。

接法（HyperFlow 官方链路）
-------------------------
    Load Diffusion Model (MiniMax-H3)
      -> ApplyHyperFlow                (Saganaki22/ComfyUI-Hyperflow；MODEL -> MODEL + SIGMAS)
      -> H3 Seamless Chain「一采自定义Sigmas」槽      ← 本模块负责的那一环
      -> 采样器选 euler（其余控件在接了 sigmas 后自动失效）

「步数/调度器」失效是 `comfy.samplers.KSampler.sample` 的行为：`if sigmas is None:
sigmas = self.sigmas` —— 传了就用传的，没传才用调度器算的。
"""

__all__ = ["ksampler_with_sigmas", "fingerprint_tag", "sigmas_steps", "sigmas_start",
           "sigmas_usable"]


def sigmas_usable(sigmas):
    """这张表**真的能用**吗（点数 ≥ 2，即至少 1 步）？

    唯一判据，三个调用点共用，不许各写一份：
    * `nodes.py` —— 决定要不要把表指纹写进 `up_cfg["sigmas"]`（进二采指纹）；
    * `upscale.render_latent` —— 决定精化采样走表还是走原路径；
    * `upscale.render_segment` —— 决定段末报告行按表回显还是按面板回显。

    **为什么必须共用**：如果「写指纹」与「按表采样」用两套判据，空表/坏表就会落到
    「指纹变了、采样没变」的错位状态 —— 既有高清段被判失效全部重做，画面却一个像素
    没动。宁可统一按「未接」处理（走原路径、不加键），代价为零。
    """
    return bool(sigmas_steps(sigmas))


def sigmas_steps(sigmas):
    """sigma 表 -> 实际去噪步数（表点数 - 1）；sigmas 为 None/坏表返回 None。"""
    if sigmas is None:
        return None
    try:
        return max(int(sigmas.shape[-1]) - 1, 0)
    except Exception:
        return None


def sigmas_start(sigmas):
    """sigma 表 -> 首值（= 本表的实际 σ 起点）；sigmas 为 None/坏表返回 None。

    用途有二：
    * 报告行回显「这张表从哪个 σ 起步」（表是外部给的，控件上的「去噪/起始 σ」
      对采样已失效，只有表首值能说明实际强度）；
    * 二采的 STG 激活窗口按「本段精化的 σ 起点」算（见 upscale._stg_model），
      接表时那个起点就是表首值，不是面板上的「去噪」。
    """
    if sigmas is None:
        return None
    try:
        return float(sigmas.detach().flatten()[0])
    except Exception:
        return None


def ksampler_with_sigmas(model, seed, steps, cfg, sampler_name, scheduler,
                         positive, negative, latent, sigmas=None, denoise=1.0):
    """官方 nodes.common_ksampler 的等价实现，额外支持用自定义 sigmas 覆盖调度。

    sigmas 为 None 时与官方 common_ksampler 行为完全一致（不传 sigmas、回调步数=
    steps，旧工作流结果逐字节不变）；非 None 时走 SamplerCustomAdvanced 同款路径——
    把 sigma 表直接交给 CFGGuider，「步数」「调度器」两个控件自动失效。

    与官方实现的唯一两处差异，都在 sigmas 分支里：
    * 进度/预览回调的步数按 ``len(sigmas)-1``（否则进度条步数与 sigma 表长度不符）；
    * 多传一个 ``sigmas=`` 给 comfy.sample.sample。

    device 无需操心：CFGGuider.sample 内部有 ``sigmas = sigmas.to(device)``。
    """
    import comfy.sample
    import comfy.utils
    try:
        import latent_preview as _lp
    except Exception:      # 兜底：comfy 包内也有同名模块
        from comfy import latent_preview as _lp

    latent_image = latent["samples"]
    latent_image = comfy.sample.fix_empty_latent_channels(
        model, latent_image,
        latent.get("downscale_ratio_spacial", None),
        latent.get("downscale_ratio_temporal", None))

    batch_inds = latent["batch_index"] if "batch_index" in latent else None
    noise = comfy.sample.prepare_noise(latent_image, seed, batch_inds)

    noise_mask = None
    if "noise_mask" in latent:
        noise_mask = latent["noise_mask"]

    cb_steps = sigmas_steps(sigmas)
    callback = _lp.prepare_callback(model, max(cb_steps if cb_steps is not None else steps, 0))
    disable_pbar = not comfy.utils.PROGRESS_BAR_ENABLED

    samples = comfy.sample.sample(
        model, noise, steps, cfg, sampler_name, scheduler,
        positive, negative, latent_image, denoise=denoise,
        noise_mask=noise_mask, sigmas=sigmas,
        callback=callback, disable_pbar=disable_pbar, seed=seed)

    out = latent.copy()
    out.pop("downscale_ratio_spacial", None)
    out.pop("downscale_ratio_temporal", None)
    out["samples"] = samples
    return (out,)


def fingerprint_tag(sigmas):
    """sigma 表 -> 进存档指纹的短标签；sigmas 为 None 返回 None。

    返回 None 的语义很重要：调用方据此**决定要不要把 sigmas 键放进指纹 dict**。
    checkpoint.fingerprint 走的是 ``json.dumps(params, sort_keys=True)``，
    「多一个空串键」也算新指纹——未接 sigmas 时必须不加键，否则既有项目的
    指纹全部漂移、旧存档续不上。
    """
    if sigmas is None:
        return None
    try:
        vals = sigmas.detach().flatten().tolist()
        return ",".join(f"{float(v):.6g}" for v in vals)
    except Exception:
        return "custom"
