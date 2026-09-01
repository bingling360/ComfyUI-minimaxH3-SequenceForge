"""潜空间放大二采 · 独立节点（Tap + Commit 单段链路）。

把 nodes.py 主循环里内联的二采渲染抽成独立 ComfyUI 节点 UpscaleNode，
使其可作为「单段二采」入口独立调用（导演台单段重二采 / 图里直连皆可）。

单段二采的两段式链路（与主循环内 render_segment 完全一致）：
- Tap（抽取）：读基础段 latent（video_t）+ 本段桥锚素材（上段尾桥 guide /
  尾帧身份锚 tail_kf_latent / 段级首帧图头锚 head_kf_latent），用放大网络
  同步放大每个锚 latent；
- Commit（落盘）：按高清目标分辨率构造 cond（_apply_guide 注入桥锚，CondSync
  锚住段首与上段高清尾连续性）→ 卸模型腾显存 → 高清 latent 低强度重采样
  → 解码 → seg_NNN.mp4 同名覆盖 + 写 manifest.upscale 记录。

桥锚丢失守卫（plan #3）：本节点在委托引擎前对 guide 做 guides.audit_keyframes
越界体检（只报不拦，给出排查线索）；引擎内仍有 preflight 显存/画布预检 +
cond_video/audio_rows_guard 采样期兜底（防 0.33.x extra_conds 覆盖丢 keyframe
致形状错位）。

实现说明：引擎（render_segment / render_latent / 各 helper）仍驻 upscale.py
（经单测、远程 G 机实测的通道），本节点是其独立入口封装——execute 构造 cfg
后直接委托 upscale.render_segment，保证「同一套二采语义、零行为漂移」。若后续
要把引擎物理搬出 upscale.py，这里已是唯一调用面，搬迁零风险。

注册方式：本节点与 H3SeamlessChainSampler 同属 V3（io.ComfyNode），
define_schema() 返回 io.Schema，由 ComfyUI 新版加载器经 get_node_list() 注册
（自带 GET_SCHEMA()，不再依赖经典 NODE_CLASS_MAPPINGS）。
"""

from comfy_api.latest import io

from . import upscale
from . import grid
from . import guides


# ---- Tap：桥锚越界体检（二采专属守卫，引擎不覆盖） ----

def audit_bridge(guide, video_t, report=None):
    """二采前对段首桥锚做越界体检（只报不拦）。

    guide 是 minimax_keyframes 协议字典 {"resolved_frame_index", "latent",
    "audio_latent"}。越界锚点会被官方 AddGuide 静默丢弃（历史上旧存档续跑
    不再逐帧一致），这里把线索暴露给报告，便于定位「高清接缝为啥跳」。
    """
    if guide is None:
        return
    if video_t is None:
        return
    try:
        fc = grid.latent_t_to_frames(int(video_t.shape[2]))
    except Exception:
        return
    for w in guides.audit_keyframes([guide], fc):
        if report is not None:
            report.append(f"⚠ 二采桥锚体检：{w}")
        else:
            print(f"[H3二采] 桥锚体检：{w}", flush=True)


class UpscaleNode(io.ComfyNode):
    """潜空间放大二采 · 单段链路节点。

    主循环委托 / 导演台单段重二采 / 图里直连三处共用同一引擎。参数面板与
    导演台二采面板同口径（与 upscale.parse_state 输出 schema 一致）。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="UpscaleNode",
            display_name="二采·归 (UpscaleNode)",
            category="H3/二采",
            description="潜空间放大二采 · 单段链路：放大网络超分 latent → 高清 cond 注入桥锚（CondSync）→ "
                        "低强度重采样 → 解码 → 覆盖 seg_NNN.mp4 + 写 manifest。与导演台「重新二采此段」共用同一引擎。",
            inputs=[
                io.Model.Input("model", tooltip="H3 UNET（ref2va / fl2va）"),
                io.Clip.Input("clip", tooltip="H3 CLIP（文本编码器）"),
                io.Vae.Input("vae", tooltip="视频 VAE"),
                io.Vae.Input("audio_vae", tooltip="音频 VAE"),
                io.Conditioning.Input("negative", tooltip="负向提示词条件"),
                io.Latent.Input("latent", tooltip="基础段视频 latent（video_t），来自二采·取"),
                io.String.Input("prompt", multiline=True, default="",
                                tooltip="本段提示词；空=外部素材段，跳过桥锚注入（与主链同语义）"),
                io.String.Input("root", default="",
                                tooltip="项目目录 output/h3_projects/<项目名>；未填则无法落盘高清分段"),
                io.Int.Input("seg", default=0, min=0, max=999, tooltip="0-based 段位"),
                # —— 二采参数（与 upscale.parse_state schema v2 一致）——
                io.Float.Input("scale", default=2.0, min=1.0, max=4.0, step=0.5, tooltip="放大倍率"),
                io.Combo.Input("arch", options=["2D", "3D"], default="2D"),
                io.Int.Input("steps", default=6, min=1, max=100, tooltip="重采样步数"),
                io.Float.Input("denoise", default=0.35, min=0.05, max=1.0, step=0.05, tooltip="重采样强度"),
                io.Float.Input("cfg", default=1.0, min=0.0, max=100.0, step=0.1),
                io.Combo.Input("precision", options=["fp16", "fp32", "bf16"], default="fp16"),
                io.Combo.Input("encode", options=["标准", "高清", "极致"], default="标准",
                               tooltip="高清分段 mp4 编码质量"),
                # —— 可选锚 / 控制（顺序与经典 INPUT_TYPES 一致，保证示例工作流 widgets_values 对齐）——
                io.Latent.Input("audio_latent", optional=True, tooltip="基础段音频 latent（零音频回归可空）"),
                io.Latent.Input("guide_latent", optional=True, tooltip="上段尾桥视频 latent（桥锚），来自二采·取"),
                io.Latent.Input("guide_audio", optional=True, tooltip="上段尾桥音频 latent"),
                io.Latent.Input("tail_latent", optional=True, tooltip="尾帧身份锚 latent"),
                io.Latent.Input("head_latent", optional=True, tooltip="段级首帧图头锚 latent"),
                io.Image.Input("first_frame", optional=True, tooltip="首帧图"),
                io.Float.Input("time_bias", default=0.0, min=0.0, max=0.2, step=0.01, advanced=True),
                io.Float.Input("mix", default=0.0, min=0.0, max=1.0, step=0.05, advanced=True),
                io.Float.Input("shift", default=0.0, min=0.0, max=100.0, step=0.5, advanced=True),
                io.Float.Input("stg", default=0.0, min=0.0, max=2.0, step=0.1, advanced=True),
                io.Int.Input("stg_block", default=25, min=0, max=49, advanced=True),
                io.Int.Input("passes", default=1, min=1, max=3, advanced=True),
                io.Float.Input("decay", default=0.5, min=0.2, max=0.8, step=0.05, advanced=True),
                io.Float.Input("sharpen", default=0.0, min=0.0, max=1.0, step=0.05, advanced=True),
                io.Float.Input("pixel_sharpen", default=0.0, min=0.0, max=1.0, step=0.05, advanced=True),
                io.String.Input("sampler", default="", advanced=True),
                io.String.Input("scheduler", default="", advanced=True),
                io.Boolean.Input("retry", default=False, advanced=True),
                io.Float.Input("retry_target", default=0.15, min=0.05, max=1.0, step=0.05, advanced=True),
                io.String.Input("model_name", default="", advanced=True,
                                tooltip="放大网络权重名（models/latent_upscale_models/ 下，如 minimax_h3_latent_upscaler_3d_fp16.safetensors）"),
            ],
            outputs=[
                io.String.Output("status", tooltip="运行报告（成功/失败信息）"),
            ],
        )

    # ---- Tap：组装二采 cfg（与 upscale.parse_state 输出同口径） ----

    @staticmethod
    def _build_cfg(kwargs):
        def _num(key, default, lo, hi):
            try:
                v = float(kwargs.get(key, default))
            except (TypeError, ValueError):
                v = default
            if v != v:
                v = default
            return min(max(v, lo), hi)

        arch = str(kwargs.get("arch") or "2D").strip().upper()
        precision = str(kwargs.get("precision") or "fp16")
        if precision not in ("fp32", "fp16", "bf16"):
            precision = "fp16"
        encode = str(kwargs.get("encode") or "标准")
        if encode not in ("标准", "高清", "极致"):
            encode = "标准"
        return {
            "mode": "跟随生成",                 # 单段节点总是「做这一段」
            "model": str(kwargs.get("model_name") or "").strip(),
            "arch": "3D" if arch == "3D" else "2D",
            "scale": _num("scale", 2.0, 1.0, 4.0),
            "denoise": _num("denoise", 0.35, 0.05, 1.0),
            "steps": int(_num("steps", 6, 1, 100)),
            "cfg": _num("cfg", 1.0, 0.0, 100.0),
            "precision": precision,
            "time_bias": _num("time_bias", 0.0, 0.0, 0.2),
            "mix": _num("mix", 0.0, 0.0, 1.0),
            "adaptive": False,
            "shift": _num("shift", 0.0, 0.0, 100.0),
            "stg": _num("stg", 0.0, 0.0, 2.0),
            "stg_block": int(_num("stg_block", 25, 0, 49)),
            "passes": int(_num("passes", 1, 1, 3)),
            "decay": _num("decay", 0.5, 0.2, 0.8),
            "sharpen": _num("sharpen", 0.0, 0.0, 1.0),
            "pixel_sharpen": _num("pixel_sharpen", 0.0, 0.0, 1.0),
            "encode": encode,
            "sampler": str(kwargs.get("sampler") or "").strip(),
            "scheduler": str(kwargs.get("scheduler") or "").strip(),
            "retry": bool(kwargs.get("retry", False)),
            "retry_target": _num("retry_target", 0.15, 0.05, 1.0),
            "include": None,
        }

    @staticmethod
    def _latent_of(inp):
        """LATENT 输入 -> 原始 latent 张量（video_t / audio_t 都是裸 tensor）。"""
        if inp is None:
            return None
        if isinstance(inp, dict):
            return inp.get("samples")
        return inp

    @staticmethod
    def _guide_of(guide_latent, guide_audio):
        """桥锚 latent -> minimax_keyframes 协议字典（resolved_frame_index=0，段首）。

        空哨兵（0 帧 latent，H3LatentLoadSegment 在「无桥锚/首段」时输出）识别为无桥锚。
        """
        gv = UpscaleNode._latent_of(guide_latent)
        ga = UpscaleNode._latent_of(guide_audio)
        # 空桥锚哨兵：时间维为 0 视为"无桥锚"
        if gv is not None and getattr(gv, "shape", None) is not None and gv.shape[2] == 0:
            gv = None
        if ga is not None and getattr(ga, "shape", None) is not None and ga.shape[2] == 0:
            ga = None
        if gv is None and ga is None:
            return None
        kf = {"resolved_frame_index": 0}
        if gv is not None:
            kf["latent"] = gv
        if ga is not None:
            kf["audio_latent"] = ga
        return kf

    # ---- Commit：委托引擎渲染单段高清并落盘 ----

    @classmethod
    def execute(cls, model, clip, vae, audio_vae, negative, latent, prompt,
                root, seg, scale=2.0, arch="2D", steps=6, denoise=0.35, cfg=1.0,
                precision="fp16", encode="标准", audio_latent=None,
                guide_latent=None, guide_audio=None, tail_latent=None,
                head_latent=None, first_frame=None, time_bias=0.0, mix=0.0,
                shift=0.0, stg=0.0, stg_block=25, passes=1, decay=0.5,
                sharpen=0.0, pixel_sharpen=0.0, sampler="", scheduler="",
                retry=False, retry_target=0.15, model_name=""):
        import comfy.model_management

        cfg = cls._build_cfg(dict(
            scale=scale, arch=arch, steps=steps, denoise=denoise, cfg=cfg,
            precision=precision, encode=encode, time_bias=time_bias, mix=mix,
            shift=shift, stg=stg, stg_block=stg_block, passes=passes, decay=decay,
            sharpen=sharpen, pixel_sharpen=pixel_sharpen, sampler=sampler,
            scheduler=scheduler, retry=retry, retry_target=retry_target,
            model_name=model_name,
        ))

        video_t = cls._latent_of(latent)
        audio_t = cls._latent_of(audio_latent)
        guide = cls._guide_of(guide_latent, guide_audio)
        tail_kf_latent = cls._latent_of(tail_latent)
        head_kf_latent = cls._latent_of(head_latent)

        if video_t is None:
            return ("[H3二采] 错误：latent 为空，无法二采",)
        if not root:
            return ("[H3二采] 错误：未提供项目 root，无法落盘高清分段",)

        # plan #3 守卫：桥锚越界体检（只报不拦）
        report = []
        audit_bridge(guide, video_t, report)

        # 放大网络缺失/不匹配 -> 硬停（不降级基础链，由调用方决定）
        try:
            net = upscale.load_net(cfg)
        except ValueError as e:
            return (f"[H3二采] 段{seg + 1} 放大网络加载失败：{e}",)

        kind = "prompt"
        idx = 0
        # 外部素材段（无提示词）跳过桥锚注入——与主循环同语义
        if not prompt.strip():
            kind = "insert"

        try:
            _, up_state = upscale.render_segment(
                模型=model, clip=clip, video_vae=vae, audio_vae=audio_vae,
                negative=negative, cfg=cfg, net=net,
                root=root, g=seg, video_t=video_t, audio_t=audio_t,
                kind=kind, idx=idx,
                seg_prompts=[prompt], seg_label_orders=[], pool_tensors={}, refs={},
                first_frame=first_frame,
                guide=guide, tail_kf_latent=tail_kf_latent, head_kf_latent=head_kf_latent,
                cur_seed=0, skip_f=0, vis_len=10**9,
                wav=None, sample_rate=44100, bh="", report=report,
                采样器="res_multistep", 调度器="simple")
            size = (up_state or {}).get("size") or ["?", "?"]
            status = (f"[H3二采] 段{seg + 1} 完成 → {size[0]}×{size[1]}"
                      + ("".join(" · " + r for r in report) if report else ""))
            return (status,)
        except upscale.UpscaleAbortError as e:
            raise   # 致命（显存/画布越界）：原样上抛终止整链
        except Exception as e:  # 单段偶发：降级返回错误串，不拖垮调用方
            msg = f"[H3二采] 段{seg + 1} 二采失败：{type(e).__name__}: {e}"
            try:
                comfy.model_management.soft_empty_cache()
            except Exception:
                pass
            return (msg,)


class H3LatentLoadSegment(io.ComfyNode):
    """二采·取：从项目存档读基础段 latent（video_t + audio_t），可选带桥锚。

    把 <root>/seg_NNN.pt（torch.save({"video","audio"})）读成 ComfyUI LATENT，
    供 UpscaleNode（二采·归）消费。桥锚 = 上段尾 ctx 帧 latent（复用 nodes._tail_keyframe，
    与主线 _up_hi 同一协议），seg>0 且 带桥锚=开 时输出，否则输出空哨兵（0 帧 latent，
    UpscaleNode._guide_of 识别为"无桥锚"）。

    这是把二采彻底外置后唯一缺的"取"半边：主链内联二采走同一存档，但存档是 H3 私有
    torch pickle 格式，核心 Load Latent 读不了，必须本节点取。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3LatentLoadSegment",
            display_name="二采·取 (H3LatentLoadSegment)",
            category="H3/二采",
            description="从存档 seg_NNN.pt 读基础段视频/音频 latent，可选带「上段尾桥锚」；"
                        "供二采·归（UpscaleNode）消费。seg>0 且 带桥锚=开 时输出 guide_latent。",
            inputs=[
                io.String.Input("root", multiline=True, default="",
                                tooltip="项目目录 output/h3_projects/<项目名>"),
                io.Int.Input("seg", default=0, min=0, max=999, tooltip="0-based 段位"),
                io.Combo.Input("ctx", options=["5", "22", "39", "56"], default="22",
                               tooltip="桥锚帧数，需与主链「引导帧数」一致"),
                io.Combo.Input("带桥锚", options=["开", "关"], default="开"),
            ],
            outputs=[
                io.Latent.Output("latent", tooltip="基础段视频 latent"),
                io.Latent.Output("audio_latent", tooltip="基础段音频 latent"),
                io.Latent.Output("guide_latent", tooltip="上段尾桥视频 latent（无桥锚时为空哨兵）"),
            ],
        )

    @classmethod
    def execute(cls, root, seg, ctx="22", 带桥锚="开"):
        import torch
        from . import checkpoint, nodes

        if not root:
            empty = {"samples": torch.zeros(1, 24, 0, 1, 1)}
            return (empty, empty, empty)
        try:
            video_t, audio_t = checkpoint.load_segment(root, seg)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"[H3二采·取] 找不到段 {seg} 存档：{checkpoint.seg_path(root, seg)}"
                f"——请先跑过主链生成该段，或确认 root 指向 output/h3_projects/<项目名>")
        except Exception as e:
            raise RuntimeError(f"[H3二采·取] 读取段 {seg} 失败：{type(e).__name__}: {e}")

        latent = {"samples": video_t}
        audio_latent = {"samples": audio_t}
        # 桥锚空哨兵：时间维 0 帧，UpscaleNode 识别为无桥锚
        guide_latent = {"samples": video_t.new_zeros(
            video_t.shape[0], video_t.shape[1], 0, *video_t.shape[3:])}
        if 带桥锚 == "开" and seg > 0:
            try:
                pv, pa = checkpoint.load_segment(root, seg - 1)
            except Exception:
                pv, pa = None, None
            if pv is not None:
                full_bridge = nodes.full_bridge_supported()
                kf = nodes._tail_keyframe(
                    pv, pa, int(ctx),
                    nodes.KEYFRAME_AUDIO_SUPPORTED and full_bridge,
                    full_bridge=full_bridge)
                guide_latent = {"samples": kf["latent"]}
        return (latent, audio_latent, guide_latent)
