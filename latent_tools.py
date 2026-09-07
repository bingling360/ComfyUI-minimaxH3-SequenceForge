"""H3 latent 现抽（LatentExtract）：输入视频/成片现抽 latent 存档。

M3 三源之一（输入视频 + 完成视频现抽），需 ComfyUI 运行环境（videoVAE/audioVAE）：
- 输入：待抽视频帧（IMAGE [N,H,W,C]，接 LoadVideo/导演台成片）+ 可选音轨 + VAE + 帧窗
- 输出：LATENT dict {video, audio}（与 checkpoint.save_segment 同格式）+ 报告
- 落盘：填项目名+保存名即写 output/h3_projects/<项目>/latent/<名>.pt 并登记 manifest

编码复用 nodes.py 序章同式：video_vae.encode(center_cover(frames))，
音频 _encode_audio_latent 同式按画面等长 token 裁。纯函数部分（帧窗钳制/命名）
无 Comfy 也可单测。
"""

import os
import time


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
            cut = 视频帧[s0:s1]
            # 与序章同式：按 VAE 要求中心覆盖到目标画幅由 VAE 内部处理，这里直编
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

except ImportError:
    pass
