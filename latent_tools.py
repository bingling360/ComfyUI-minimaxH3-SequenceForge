"""H3 latent 现抽与库内放大（LatentExtract / LatentUpscale）：输入视频/成片现抽 latent 存档。

M3 三源之一（输入视频 + 完成视频现抽），需 ComfyUI 运行环境（videoVAE/audioVAE）：
- H3LatentExtract：待抽视频帧（IMAGE [N,H,W,C]，接 LoadVideo/导演台成片）+ 可选音轨
  + VAE + 帧窗 -> 报告（落盘 latent/*.pt 并登记 manifest）
- 库内 mp4->latent 转码走主节点自动专跑（提交即执行），不再需要转码节点。
- H3LatentUpscale：latent 库任意文件 -> 神经放大（LBH 放大网络，T 不变 H/W 放大）
  -> 可选低强度二次采样（接模型/文本编码器+提示词，denoise<1 补高频）-> 存回
  latent 库并登记。放大的 latent 可在分段设置里选作 keyframe 外源。

编码复用 nodes.py 序章同式：video_vae.encode(center_cover(frames))，
音频 _encode_audio_latent 同式按画面等长 token 裁。纯函数部分（帧窗钳制/命名）
无 Comfy 也可单测。
"""

import os
import time

# 单次 VAE 编码帧数上限：唯一定义在 grid.MAX_WINDOW_FRAMES（H3 训练长度约 124–362 帧）。
# 超限直接报错指引缩小窗口，而不是整窗单次前向把显存/内存顶爆（转码卡死根因）。
# 双导入与本文件其余 grid 引用同口径：本模块在无 ComfyUI 环境下按文件路径单测，
# 那时没有包上下文，相对导入会失败。
try:
    from .grid import MAX_WINDOW_FRAMES as MAX_ENCODE_FRAMES
except ImportError:
    from grid import MAX_WINDOW_FRAMES as MAX_ENCODE_FRAMES


def _transcode_env_note(videoVAE, frames):
    """转码前诊断：只打印，不改行为。

    H3 视频 VAE 约 5GB（fp32 口径）；6-8GB 显存卡只能逐层从内存串流，
    24 帧也可能十几分钟。下次看到编码慢，先看这行判定是不是显存墙：
    是 → 启动参数加 --fp16-vae（VAE 减半，全链同对象，结果一致），
    而不是反复重试或调小窗口（24 帧已是最小可用窗）。
    """
    try:
        import torch
        shape = tuple(getattr(frames, "shape", ()))
        dev = getattr(videoVAE, "device", None)
        vram_gb, dtype_s = None, None
        try:
            if dev is not None and getattr(dev, "type", "") == "cuda":
                vram_gb = torch.cuda.get_device_properties(dev).total_memory / (1024 ** 3)
        except Exception:
            pass
        try:
            inner = getattr(videoVAE, "first_stage_model", videoVAE)
            for p in inner.parameters():
                dtype_s = str(getattr(p, "dtype", "")).replace("torch.", "")
                break
        except Exception:
            pass
        print(f"[H3库转存档] 输入 {shape} -> {dev}（VAE 精度 {dtype_s or '?'}"
              + (f"，本机显存 {vram_gb:.1f}GB" if vram_gb else "") + "）", flush=True)
        if vram_gb is not None and vram_gb < 8.0 and (dtype_s or "").startswith("float32"):
            print("[H3库转存档] 提示：显存 <8GB 且 VAE 为 fp32，编码会逐层串流很慢；"
                  "Comfy 启动参数加 --fp16-vae（VAE 减半、全链同对象，结果一致）可提速数倍。",
                  flush=True)
    except Exception:
        pass


def clamp_window(start_f, end_f, n):
    """像素帧窗钳制 -> (s0, s1)；切空抛 ValueError。"""
    try:
        s0, s1 = int(start_f), int(end_f)
    except (TypeError, ValueError):
        raise ValueError("start_f/end_f 须为整数像素帧") from None
    s0 = max(0, s0)
    s1 = min(max(0, int(n)), s1)
    if s1 <= s0:
        raise ValueError(f"帧窗为空（[{s0}, {s1})，源共 {n} 帧）")
    return s0, s1


def clean_latent_name(name):
    s = str(name or "").strip().replace("\\", "/").split("/")[-1]
    if not s or s.startswith(".") or "/" in s or "\\" in s or ":" in s or ".." in s:
        return ""
    if not os.path.splitext(s)[1]:
        return ""
    if not s.endswith(".pt"):
        return ""
    return s


try:
    from comfy_api.latest import io

    class H3LatentExtract(io.ComfyNode):
        """现抽节点：视频帧窗 -> latent 存档（输入视频/成片现抽）。"""

        @classmethod
        def define_schema(cls):
            return io.Schema(
                node_id="H3LatentExtract",
                display_name="H3 Latent Extract (现抽存档)",
                category="MiniMaxH3",
                description="从输入视频或成片按帧窗编码抽 latent：视频VAE 编码画面、"
                            "音频VAE 编码音轨，存 output/h3_projects/<项目>/latent/*.pt "
                            "并登记 manifest（成片库可见）。编码与序章同式。",
                inputs=[
                    io.Image.Input("视频帧", tooltip="待抽视频帧（LoadVideo/成片解码，[N,H,W,C]）"),
                    io.Vae.Input("视频VAE", tooltip="与主链同一视频 VAE"),
                    io.Audio.Input("音轨", optional=True,
                                   tooltip="同源音轨（不接按静音处理，仍登记空音频）"),
                    io.Vae.Input("音频VAE", optional=True, tooltip="接了音轨时必填"),
                    io.Int.Input("起始帧", default=0, min=0, max=100000,
                                 tooltip="帧窗起点（含，像素帧号）"),
                    io.Int.Input("结束帧", default=0, min=0, max=100000,
                                 tooltip="帧窗终点（不含）；0 或超出按源尾钳制；须大于起点"),
                    io.String.Input("项目名", default="",
                                    tooltip="存档项目（output/h3_projects/<项目>）；空=只输出不落盘"),
                    io.String.Input("保存名", default="",
                                    tooltip="latent 文件名（须以 .pt 结尾，如 seg0_head.pt）；空=只输出不落盘"),
                ],
                outputs=[
                    io.String.Output("报告", tooltip="抽取结果（帧窗/token/落盘路径）"),
                ],
            )

        @classmethod
        def execute(cls, 视频帧, 视频VAE, 音轨=None, 音频VAE=None,
                    起始帧=0, 结束帧=0, 项目名="", 保存名=""):
            import torch
            n = int(视频帧.shape[0]) if getattr(视频帧, "shape", None) is not None else 0
            if n <= 0:
                raise ValueError("视频帧为空（N=0），无法现抽")
            try:
                ef = int(结束帧) or n
            except (TypeError, ValueError):
                ef = n
            s0, s1 = clamp_window(起始帧, ef, n)
            if s1 - s0 > MAX_ENCODE_FRAMES:
                raise ValueError(
                    f"帧窗 {s1 - s0} 帧超出单次编码上限 {MAX_ENCODE_FRAMES} 帧（约 15 秒）："
                    "请缩小窗口分多次抽存（转 keyframe 一般只取尾部几秒）")
            cut = 视频帧[s0:s1]
            # 与序章同式：按 VAE 要求中心覆盖到目标画幅由 VAE 内部处理，这里直编
            print(f"[H3现抽存档] 视频VAE 编码 [{s0}, {s1}) {s1 - s0}帧…", flush=True)
            v_lat = 视频VAE.encode(cut)
            if 音轨 is not None and 音频VAE is not None:
                try:
                    from .nodes import _encode_audio_latent as _enc_a
                except ImportError:
                    from nodes import _encode_audio_latent as _enc_a
                try:
                    from .grid import audio_tokens_for_frames as _atok
                except ImportError:
                    from grid import audio_tokens_for_frames as _atok
                a_lat = _enc_a(音频VAE, 音轨, _atok(s1 - s0))
            else:
                a_lat = None
            saved = ""
            proj = str(项目名 or "").strip()
            sname = clean_latent_name(保存名)
            if proj and sname:
                try:
                    from . import checkpoint as _ckpt
                except ImportError:
                    import checkpoint as _ckpt
                from folder_paths import get_output_directory
                root = os.path.join(get_output_directory(), "h3_projects", proj)
                if not os.path.isdir(root):
                    raise ValueError(f"项目不存在：{proj}（先新建或跑一段）")
                manifest = _ckpt.load_manifest(root)
                if manifest is None:
                    raise ValueError(f"项目无 manifest：{proj}")
                ldir = os.path.join(root, "latent")
                os.makedirs(ldir, exist_ok=True)
                payload = {"video": v_lat.detach().cpu().clone()}
                if a_lat is not None:
                    payload["audio"] = a_lat.detach().cpu().clone()
                buf_tmp = os.path.join(ldir, sname + ".part")
                import tempfile
                buf = tempfile.SpooledTemporaryFile(max_size=64 << 20)
                torch.save(payload, buf)
                buf.seek(0)
                with open(buf_tmp, "wb") as fh:
                    fh.write(buf.read())
                buf.close()
                os.replace(buf_tmp, os.path.join(ldir, sname))
                manifest.setdefault("latents", [])
                manifest["latents"] = [x for x in manifest["latents"]
                                       if isinstance(x, dict) and x.get("file") != f"latent/{sname}"]
                manifest["latents"].append({"file": f"latent/{sname}", "src": "extract",
                                            "start_f": s0, "end_f": s1,
                                            "updated_at": time.time()})
                manifest["updated_at"] = time.time()
                manifest["revision"] = int(manifest.get("revision") or 1) + 1
                _ckpt.save_manifest(root, manifest)
                saved = f"h3_projects/{proj}/latent/{sname}"
            vt = tuple(v_lat.shape) if getattr(v_lat, "shape", None) is not None else ()
            rep = (f"现抽：像素帧[{s0}, {s1}) {s1 - s0}帧 -> video latent {vt}"
                   + (f"，已存 {saved}" if saved else "（未落盘：项目名/保存名为空）"))
            return (rep,)

    # P4d：H3MediaToLatent 节点类已删除（转码走主节点自动专跑，提交即执行；
    # run_transcode_job 纯函数保留，主节点转码任务共用）。


    def run_transcode_job(项目名, 源文件, 视频VAE, 音频VAE=None,
                          起始秒=0.0, 结束秒=0.0, 分支="图像+音频", 分开保存="否", 保存名="",
                          _pbar=None, _interrupted=None):
        """库内 mp4 -> latent 纯函数（供主节点转码任务调用）。

        有界：秒窗超 362 帧直接报错；解码最多读上限+1 帧。_pbar 有 ProgressBar 则
        分步推进；_interrupted() 为真则抛 Interrupted（主节点转码任务支持队列取消）。
        成功返回报告串；失败抛 ValueError/RuntimeError。
        """
        class _Interrupted(Exception):
            pass

        try:
            from comfy.model_management import InterruptProcessingException as _IPE
        except Exception:
            _IPE = None

        def _chk():
            if not callable(_interrupted):
                return
            try:
                stop = bool(_interrupted())
            except Exception:
                return
            if stop:
                raise (_IPE() if _IPE is not None else _Interrupted())

        import torch
        proj = str(项目名 or "").strip()
        if not proj:
            raise ValueError("项目名不能为空")
        try:
            from . import checkpoint as _ckpt
        except ImportError:
            import checkpoint as _ckpt
        try:
            from . import projects as _proj
        except ImportError:
            import projects as _proj
        from folder_paths import get_output_directory
        if _proj.safe_name(proj) != proj:
            raise ValueError(f"非法项目名：{proj!r}")
        root = os.path.join(get_output_directory(), "h3_projects", proj)
        manifest = _ckpt.load_manifest(root)
        if manifest is None:
            raise ValueError(f"项目不存在：{proj}（先新建或跑一段）")
        src_path = _ckpt.resolve_project_file(root, str(源文件 or ""))
        if not os.path.isfile(src_path):
            raise ValueError(f"源文件不存在：{源文件}（须为项目内 mp4）")
        try:
            from . import media as _media
        except ImportError:
            import media as _media
        if _pbar is None:
            try:
                import comfy.utils as _cu
                _pbar = _cu.ProgressBar(4)
            except Exception:
                _pbar = None
        real_fps = _media.probe_fps(src_path, 24.0)
        try:
            ss = max(0.0, float(起始秒 or 0.0))
        except (TypeError, ValueError):
            ss = 0.0
        try:
            es = float(结束秒 or 0.0)
        except (TypeError, ValueError):
            es = 0.0
        s0 = max(0, int(round(ss * real_fps)))
        s1 = int(round(es * real_fps)) if es > 0 else None
        if s1 is not None and s1 <= s0:
            raise ValueError(f"秒窗为空（[{ss}s, {es}s) -> 帧[{s0}, {s1})）")
        if s1 is not None and s1 - s0 > MAX_ENCODE_FRAMES:
            raise ValueError(
                f"秒窗 {s1 - s0} 帧超出单次编码上限 {MAX_ENCODE_FRAMES} 帧（约 15 秒，模型训练长度）："
                "请缩小窗口（转 keyframe 一般只取尾部几秒），分多次转存")
        print(f"[H3库转存档] 解码帧窗 [{s0}, {s1 if s1 is not None else '尾'}）@{real_fps:.2f}fps…",
              flush=True)
        # 解码本身也有界：最多读到上限+1 帧，多 1 帧即判定超限（到片尾模式
        # 不会把整片解进内存——之前卡死的另一半原因）
        decode_end = s1 if s1 is not None else s0 + MAX_ENCODE_FRAMES + 1
        frames, wav, sr = _media.decode_av(src_path, s0, decode_end, real_fps)
        n = int(frames.shape[0])
        if n > MAX_ENCODE_FRAMES:
            raise ValueError(
                f"窗口共 {n} 帧，超出单次编码上限 {MAX_ENCODE_FRAMES} 帧（约 15 秒）："
                "请填写结束秒缩小窗口（转 keyframe 一般只取尾部几秒），分多次转存")
        s1 = s0 + n
        _chk()
        if _pbar is not None:
            _pbar.update(1)
        want_v = str(分支 or "") != "仅音频"
        want_a = str(分支 or "") != "仅图像"
        cut = frames
        _transcode_env_note(视频VAE, cut)
        print(f"[H3库转存档] 视频VAE 编码 {n} 帧…", flush=True)
        v_lat = 视频VAE.encode(cut) if want_v else None
        _chk()
        if _pbar is not None:
            _pbar.update(1)
        a_lat = None
        if want_a:
            if 音频VAE is None:
                raise ValueError("含音频分支时必须接「音频VAE」")
            try:
                from .nodes import _encode_audio_latent as _enc_a
            except ImportError:
                from nodes import _encode_audio_latent as _enc_a
            try:
                from .grid import audio_tokens_for_frames as _atok
            except ImportError:
                from grid import audio_tokens_for_frames as _atok
            rate = int(sr or 44100)
            if wav is not None and getattr(wav, "numel", lambda: 0)() > 0:
                a0 = max(0, int(round(s0 / real_fps * rate)))
                a1 = max(a0, int(round(s1 / real_fps * rate)))
                sub = wav[..., a0:a1] if a1 > a0 else wav
                audio = {"waveform": sub, "sample_rate": rate}
            else:
                import torch as _t
                audio = {"waveform": _t.zeros(1, 1, 0), "sample_rate": rate}
            print(f"[H3库转存档] 音频VAE 编码 {_atok(s1 - s0)} tokens…", flush=True)
            a_lat = _enc_a(音频VAE, audio, _atok(s1 - s0))
            if _pbar is not None:
                _pbar.update(1)
        sname = clean_latent_name(保存名)
        if not sname:
            raise ValueError(f"非法保存名：{保存名!r}（须以 .pt 结尾）")
        ldir = os.path.join(root, "latent")
        os.makedirs(ldir, exist_ok=True)
        stem = sname[:-3]
        outs = []
        if want_v and want_a and str(分开保存) == "是":
            payloads = [("_v", {"video": v_lat.detach().cpu().clone()}, "video"),
                        ("_a", {"audio": a_lat.detach().cpu().clone()}, "audio")]
        else:
            payload = {}
            if v_lat is not None:
                payload["video"] = v_lat.detach().cpu().clone()
            if a_lat is not None:
                payload["audio"] = a_lat.detach().cpu().clone()
            kd = "av" if (v_lat is not None and a_lat is not None) \
                else ("video" if v_lat is not None else "audio")
            payloads = [("", payload, kd)]
        import tempfile
        for suffix, payload, kd in payloads:
            fn = f"{stem}{suffix}.pt"
            tmp = os.path.join(ldir, fn + ".part")
            buf = tempfile.SpooledTemporaryFile(max_size=64 << 20)
            torch.save(payload, buf)
            buf.seek(0)
            with open(tmp, "wb") as fh:
                fh.write(buf.read())
            buf.close()
            os.replace(tmp, os.path.join(ldir, fn))
            manifest.setdefault("latents", [])
            manifest["latents"] = [x for x in manifest["latents"]
                                   if isinstance(x, dict) and x.get("file") != f"latent/{fn}"]
            manifest["latents"].append({"file": f"latent/{fn}", "src": str(源文件),
                                        "start_f": s0, "end_f": s1, "kind": kd,
                                        "updated_at": time.time()})
            outs.append(f"latent/{fn}({kd})")
        manifest["updated_at"] = time.time()
        manifest["revision"] = int(manifest.get("revision") or 1) + 1
        _ckpt.save_manifest(root, manifest)
        if _pbar is not None:
            _pbar.update(1)
        rep = (f"库转存档：{os.path.basename(src_path)} 帧[{s0}, {s1}) "
               f"{s1 - s0}帧@{real_fps:.2f}fps -> {', '.join(outs)}")
        print(f"[H3库转存档] {rep}", flush=True)
        return rep

    class H3LatentUpscale(io.ComfyNode):
        """库内放大：latent 库任意文件 -> 神经放大 -> 可选二次采样 -> 存回 latent 库。

        - 放大：LBH 潜空间放大网络（与主链二采同一权重目录
          models/latent_upscale_models/），T 不变、H/W 放大，归一化口径与训练一致。
        - 二次采样（可选）：接模型 + 文本编码器 + 提示词后，对放大 latent 做
          denoise<1 的低强度重采样补回高频细节（JZL 范式）；不接模型=只放大不重采。
        - 音频分支原样透传（放大只动图像 latent 时间维不变，音画仍对齐）。
        - 产物可直接在分段设置「桥来源」选作 keyframe 外源（形状须与目标链一致）。
        """

        @classmethod
        def define_schema(cls):
            return io.Schema(
                node_id="H3LatentUpscale",
                display_name="H3 Latent Upscale (库内放大)",
                category="MiniMaxH3",
                description="latent 库任意文件神经放大 + 可选二次采样，存回 latent 库并登记。"
                            "放大产物可在分段设置里选作 keyframe 外源。",
                inputs=[
                    io.String.Input("项目名", default="",
                                    tooltip="存档项目（output/h3_projects/<项目>）"),
                    io.String.Input("源文件", default="",
                                    tooltip="latent 库文件（如 latent/head.pt）或段存档（seg_000.pt）"),
                    io.String.Input("放大模型", default="",
                                    tooltip="models/latent_upscale_models/ 下的权重文件名（/h3chain/upscale_models 可查）"),
                    io.Combo.Input("架构", options=["auto", "2D", "3D"], default="auto",
                                   tooltip="须与权重匹配（auto=按权重自动判定）；三份官方权重均为 3D"),
                    io.Float.Input("倍率", default=2.0, min=1.0, max=4.0, step=0.5,
                                   tooltip="latent H/W 放大倍率（偶数对齐=像素 32 倍数）"),
                    io.Combo.Input("精度", options=["fp16", "bf16", "fp32"], default="fp16",
                                   tooltip="放大网络计算精度"),
                    io.Combo.Input("二次采样", options=["关闭", "开启"], default="关闭",
                                   tooltip="开启后对放大 latent 低强度重采样（需接模型/文本编码器/提示词）"),
                    io.Model.Input("模型", optional=True, tooltip="二次采样用（H3 UNET，关闭时不需接）"),
                    io.Clip.Input("文本编码器", optional=True, tooltip="二次采样用（关闭时不需接）"),
                    io.String.Input("提示词", multiline=True, default="",
                                    tooltip="二次采样的文本条件（为空=空提示精化）"),
                    io.Int.Input("种子", default=0, min=0, max=0xffffffffffffffff),
                    io.Int.Input("步数", default=6, min=1, max=100,
                                 tooltip="二次采样步数（低强度精化，6 步常用）"),
                    io.Float.Input("强度", default=0.35, min=0.05, max=1.0, step=0.05,
                                   tooltip="二次采样 denoise（0.35-0.55 常用）"),
                    io.Float.Input("CFG", default=1.0, min=0.0, max=100.0, step=0.1),
                    io.String.Input("保存名", default="",
                                    tooltip="latent 文件名（须以 .pt 结尾，如 head_up2x.pt）"),
                ],
                outputs=[
                    io.String.Output("报告", tooltip="放大结果（尺寸/分支/落盘路径）"),
                ],
            )

        @classmethod
        def execute(cls, 项目名, 源文件, 放大模型="", 架构="auto", 倍率=2.0, 精度="fp16",
                    二次采样="关闭", 模型=None, 文本编码器=None, 提示词="",
                    种子=0, 步数=6, 强度=0.35, CFG=1.0, 保存名=""):
            import torch
            proj = str(项目名 or "").strip()
            if not proj:
                raise ValueError("项目名不能为空")
            try:
                from . import checkpoint as _ckpt
            except ImportError:
                import checkpoint as _ckpt
            try:
                from . import projects as _proj
            except ImportError:
                import projects as _proj
            from folder_paths import get_output_directory
            if _proj.safe_name(proj) != proj:
                raise ValueError(f"非法项目名：{proj!r}")
            root = os.path.join(get_output_directory(), "h3_projects", proj)
            manifest = _ckpt.load_manifest(root)
            if manifest is None:
                raise ValueError(f"项目不存在：{proj}（先新建或跑一段）")
            f = str(源文件 or "").strip().replace("\\", "/")
            parts = [p for p in f.split("/") if p and p != "."]
            if len(parts) == 1 and parts[0].startswith("seg_") and parts[0].endswith(".pt"):
                src_path = os.path.join(root, parts[0])
                src_desc = parts[0]
            elif len(parts) == 2 and parts[0] == "latent" and parts[1].endswith(".pt"):
                src_path = os.path.join(root, "latent", parts[1])
                src_desc = "/".join(parts)
            else:
                raise ValueError(f"非法源文件：{源文件!r}（须为 latent/<名>.pt 或 seg_NNN.pt）")
            if not os.path.isfile(src_path):
                raise ValueError(f"源 latent 不存在：{src_desc}")
            with open(src_path, "rb") as fh:
                payload = torch.load(fh, map_location="cpu", weights_only=True)
            video = payload.get("video")
            audio = payload.get("audio")
            if video is None or getattr(video, "dim", lambda: 0)() != 5:
                raise ValueError("源 latent 无视频分支（纯音频 latent 不可放大）")
            try:
                from .grid import latent_t_to_frames as _l2f
            except ImportError:
                from grid import latent_t_to_frames as _l2f
            if _l2f(int(video.shape[2])) > 720:
                raise ValueError(
                    f"源 latent 约 {_l2f(int(video.shape[2]))} 帧，超出单次放大上限 720 帧（约30秒）："
                    "请先用 latent 切片截出需要的段落再放大")
            kind = "av" if audio is not None else "video"
            try:
                from . import upscale as _up
            except ImportError:
                import upscale as _up
            cfg = {"model": str(放大模型 or "").strip(), "arch": str(架构 or "auto"),
                   "scale": float(倍率 or 2.0), "precision": str(精度 or "fp16"),
                   "enlarge": True, "device": "auto", "force_unload": False}
            net = _up.load_net(cfg)
            print(f"[H3库内放大] 神经放大 {tuple(video.shape)} ×{cfg['scale']:g}…", flush=True)
            try:
                up_v = _up.upscale_video(video, net, cfg["scale"], cfg.get("arch", "auto"))
            finally:
                try:
                    from . import upscale_net as _un
                except ImportError:
                    import upscale_net as _un
                _un.soft_empty_cache()
            oh, ow = int(video.shape[-2]), int(video.shape[-1])
            nh, nw = int(up_v.shape[-2]), int(up_v.shape[-1])
            if str(二次采样) == "开启":
                if 模型 is None or 文本编码器 is None:
                    raise ValueError("二次采样=开启时必须接「模型」与「文本编码器」")
                import nodes as _comfy_nodes
                print(f"[H3库内放大] 二次采样精化 {int(步数)} 步 @强度{float(强度):g}…", flush=True)
                cond = 文本编码器.encode_from_tokens_scheduled(
                    文本编码器.tokenize(str(提示词 or "")))
                negative = 文本编码器.encode_from_tokens_scheduled(文本编码器.tokenize(""))
                # 设备：common_ksampler 内部处理对齐，latent 传 CPU 张量即可
                sampled = _comfy_nodes.common_ksampler(
                    模型, int(种子), int(步数), float(CFG),
                    "res_multistep", "simple", cond, negative,
                    {"samples": up_v.clone()}, denoise=float(强度))[0]
                up_v = sampled["samples"]
            sname = clean_latent_name(保存名)
            if not sname:
                raise ValueError(f"非法保存名：{保存名!r}（须以 .pt 结尾）")
            ldir = os.path.join(root, "latent")
            os.makedirs(ldir, exist_ok=True)
            out_payload = {"video": up_v.detach().cpu().contiguous().clone()}
            if audio is not None:
                out_payload["audio"] = audio.detach().cpu().contiguous().clone()
            import tempfile
            tmp = os.path.join(ldir, sname + ".part")
            buf = tempfile.SpooledTemporaryFile(max_size=64 << 20)
            torch.save(out_payload, buf)
            buf.seek(0)
            with open(tmp, "wb") as fh:
                fh.write(buf.read())
            buf.close()
            os.replace(tmp, os.path.join(ldir, sname))
            manifest.setdefault("latents", [])
            manifest["latents"] = [x for x in manifest["latents"]
                                   if isinstance(x, dict) and x.get("file") != f"latent/{sname}"]
            manifest["latents"].append({"file": f"latent/{sname}", "src": src_desc,
                                        "kind": kind, "upscale": round(float(倍率 or 0), 2),
                                        "resample": str(二次采样) == "开启",
                                        "updated_at": time.time()})
            manifest["updated_at"] = time.time()
            manifest["revision"] = int(manifest.get("revision") or 1) + 1
            _ckpt.save_manifest(root, manifest)
            rep = (f"库内放大：{src_desc} latent {oh}×{ow} -> {nh}×{nw}"
                   f"（×{float(倍率 or 0):g}，{cfg.get('arch')}）"
                   + (" + 二次采样精化" if str(二次采样) == "开启" else "")
                   + f" -> latent/{sname}")
            print(f"[H3库内放大] {rep}", flush=True)
            return (rep,)

except ImportError:
    pass
