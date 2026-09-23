"""PyAV 编解码共享：mp4 落盘与上传视频解码。

分镜段视频（checkpoint.save_segment_mp4）、采样器自动保存（nodes 自动成片）
两处共用，编码逻辑只维护一份。
PyAV 是 ComfyUI 新视频栈（CreateVideo/SaveVideo）的既有依赖，无新依赖。
"""

import os
from fractions import Fraction

last_error = None

# ---- 编码器（性能优化设置 `encoder` / `x264_crf` / `nvenc_cq`）----
#
# 两个正交维度，别混成一个旋钮：
#   · `encoder` 选实现：libx264（CPU 软编，压缩效率高，但默认起 核数×1.5 个线程，
#     长链编码时整机 CPU 吃满）/ h264_nvenc / hevc_nvenc（GPU 硬件编，几乎不占 CPU，
#     代价是同画质需要更高码率）→ 见 resolve_encoder。
#   · `encode_profile` 只管 preset（编码速度档）+ 8bit 暗部抖动，两边通用。
#
# 质量数值**跟编码器换含义**：x264 认 `crf`、NVENC 认 `cq`，互不认识。
# 传错的一侧会被 ffmpeg 静默忽略（不报错，质量档直接失效），所以这两个值分开存、
# 按当前 ENCODER 分流给 —— 面板上也按 encoder 的值显隐（见 h3_director.js 的 showWhen）。
#
# 关于「编码是不是瓶颈」：短片段（百帧级）不是，但长链是 —— 8 段 × 10s ≈ 1600+ 帧，
# libx264 的 CPU 占用会拖住整机（采样是 GPU 在跑，编码是 CPU 在跑，两者抢的是
# 同一台机器的整机吞吐）。所以别拿几百帧的实测去代表长链。
ENCODER = "libx264"      # 进程级生效值（由 perf.apply_runtime 写入）
ENCODER_CRF = 20         # libx264 专用质量档（NVENC 不看这个）
ENCODER_CQ = 20          # NVENC 专用质量档（x264 不看这个）


def set_crf(crf=20):
    """设置进程级 x264 crf（越小越清晰、文件越大）；返回生效值。"""
    global ENCODER_CRF
    try:
        ENCODER_CRF = max(0, min(51, int(crf)))
    except (TypeError, ValueError):
        ENCODER_CRF = 20
    return ENCODER_CRF


def set_encoder(name, cq=20):
    """设置进程级编码器；返回实际生效的 codec 名（不认识的回落 libx264）。"""
    global ENCODER, ENCODER_CQ
    try:
        from . import perf  # type: ignore
    except ImportError:
        try:
            import perf  # type: ignore
        except Exception:
            perf = None
    codec = "libx264"
    if perf is not None:
        codec = perf.resolve_encoder(name, cq=cq)[0]
    else:
        n = str(name or "auto").strip().lower()
        if n in ("h264_nvenc", "hevc_nvenc"):
            codec = n
    ENCODER = codec
    try:
        ENCODER_CQ = int(cq)
    except (TypeError, ValueError):
        ENCODER_CQ = 20
    return ENCODER


def _video_stream_options(crf, preset, threads, aq_mode):
    """视频流 codec + options（编码器分流在此，两个入口共用）。

    NVENC 不认 `crf`（静默忽略 → 质量档整个失效）、也不认 `preset` 里的
    x264 档名（veryfast/medium 会被当成 NVENC 的 p1..p7 之外的非法值），
    所以走 NVENC 时只给 `cq` + `preset=p4`（NVENC 的中档，对应 x264 的
    medium 量级），x264 那套 crf/preset/aq-mode 原样保留。

    ⚠ `crf` 入参是「调用方按 encode_profile 查表拿到的默认值」；只要用户
      在性能设置里动过 x264 质量档，就以进程级 `ENCODER_CRF` 为准（面板那个
      数值才是用户输入的真相，档位表的 crf 只是它的出厂默认）。
    """
    if ENCODER in ("h264_nvenc", "hevc_nvenc"):
        return ENCODER, {"cq": str(int(ENCODER_CQ)), "preset": "p4"}
    options = {"crf": str(int(ENCODER_CRF)), "preset": str(preset),
               "threads": str(max(1, int(threads)))}
    if aq_mode:
        options["aq-mode"] = str(int(aq_mode))
    return "libx264", options


# 4×4 Bayer 有序抖动矩阵（0-15）：标准 magic-square 排列，16 档阈值均布
_BAYER4 = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)


def dither_quantize(arr_f):
    """[H,W,3] float 0-1 / 整数图像 -> uint8（4×4 Bayer 有序抖动）。

    抗条纹（banding）核心：量化加阈值 (k+0.5)/16 后取整，常量场的量化误差
    从「整块同值」打散为 16 档细粒度交替——平滑渐变区（天空/皮肤/暗部）的
    8bit 横向色带被转为不可察觉的微噪，期望值无偏（E[量化] = 原值 ×255）。
    确定性（无随机源，重放一致）；整数输入先按 dtype 满量程归一化，避免误把
    uint8 的 0..255 当 0..1 再量化导致整帧截白。
    """
    import numpy as np
    src = np.asarray(arr_f)
    if np.issubdtype(src.dtype, np.integer):
        x = src.astype("float32") / float(np.iinfo(src.dtype).max)
    else:
        x = src.astype("float32", copy=False)
    h, w = x.shape[:2]
    off = (np.asarray(_BAYER4, dtype="float32") + 0.5) / 16.0
    tile = np.tile(off, ((h + 3) // 4, (w + 3) // 4))[:h, :w]
    q = np.floor(x * 255.0 + tile[..., None]).clip(0.0, 255.0)
    return q.astype("uint8")


def _av_trace_tail(tb_text):
    """traceback 文本 -> av 内部调用点摘要（去重保序，报错自定位用）。

    av 17 容器级错误（ArgumentError/EINVAL 22 等）的 last_error 只有异常一行，
    无法区分 write_header / mux / trailer 三个抛出点；把 av/container、av/codec
    的栈帧并入 last_error，报告行即可直接定位，无需翻 ComfyUI 控制台。
    """
    parts = []
    for ln in tb_text.splitlines():
        s = ln.strip()
        if s.startswith(('File "av/', "File 'av/")) and s not in parts:
            parts.append(s)
    return " @".join(parts)


def save_av_mp4(path, frames, wav, sample_rate, fps=24, crf=20, threads=4,
                preset="veryfast", aq_mode=None, dither=False):
    """可见帧 + 音轨 -> mp4（H.264 + AAC），成功返回 True。

    先写 .part 再原子改名：失败不留半成品、不破坏已有文件。编码失败
    （或 PyAV 缺失）返回 False，调用方降级（缩略图 / 空串），不影响主流程。
    失败原因存入 media.last_error 供调用方读取（报告/日志）。

    内存与 CPU 约束：分块（32 帧）搬运到 CPU，峰值内存 ~160MB 而非整链
    float32（长链数 GB，曾致内存耗尽假死）；threads 限 4（x264 默认
    线程=核数×1.5，会打满 CPU 导致整机卡顿）。

    编码质量旋钮（抗糊 N5，默认值 = 现状兼容）：
    - crf：恒定质量（越小越清晰，20 标准 / 16 高清 / 13 极致）；
    - preset：x264 率失真档（veryfast 快但糊 5-15%，medium/slow 保细节）；
    - aq_mode：自适应量化（3=暗部保细节，None=关）；
    - dither：Bayer 有序抖动（8bit 渐变色带 → 微噪，条纹正解）。
    """
    global last_error
    try:
        import av
        import numpy
    except Exception as e:
        last_error = f"PyAV/numpy 导入失败：{type(e).__name__}: {e}"
        return False
    tmp = path + ".part"
    try:
        n = int(frames.shape[0])
        h, w = int(frames.shape[1]), int(frames.shape[2])
        h, w = h - h % 2, w - w % 2  # yuv420p 要求偶数尺寸
        pcm = numpy.ascontiguousarray(wav.detach().float().cpu().clamp(-1.0, 1.0).numpy())
        if pcm.ndim == 3:                    # [batch, ch, samples] → [ch, samples]
            pcm = pcm[0]
        ch = int(pcm.shape[0])
        layout = "stereo" if ch >= 2 else "mono"

        # tmp 扩展名是 .part，PyAV 按扩展名猜不出封装格式会直接抛
        # ValueError: Could not determine output format —— 必须显式指定
        container = av.open(tmp, mode="w", format="mp4")
        try:
            _codec, options = _video_stream_options(crf, preset, threads, aq_mode)
            vstream = container.add_stream(_codec, rate=fps)
            vstream.width, vstream.height = w, h
            vstream.pix_fmt = "yuv420p"
            vstream.options = options
            astream = container.add_stream("aac", rate=int(sample_rate), layout=layout)
            v_pts = a_pts = 0   # 显式时间戳计数：视频帧号 / 音频样点数（编码器时基）

            for start in range(0, n, 32):
                raw = (frames[start:start + 32].detach().float().clamp(0.0, 1.0)
                       .cpu().numpy()[:, :h, :w, :3])
                for i in range(raw.shape[0]):
                    arr = dither_quantize(raw[i]) if dither \
                        else (raw[i] * 255.0).astype("uint8")
                    frame = av.VideoFrame.from_ndarray(
                        numpy.ascontiguousarray(arr), format="rgb24")
                    frame.pts = v_pts   # pts=None 依赖编码器/封装器兜底已被 FFmpeg 标记废弃
                    v_pts += 1
                    for packet in vstream.encode(frame):
                        container.mux(packet)
            if ch > 2:
                pcm = pcm[:2]
            for start in range(0, pcm.shape[1], 1024):
                aframe = av.AudioFrame.from_ndarray(
                    pcm[:, start:start + 1024], format="fltp", layout=layout)
                aframe.sample_rate = int(sample_rate)
                aframe.pts = a_pts
                a_pts += aframe.samples
                for packet in astream.encode(aframe):
                    container.mux(packet)
            for packet in vstream.encode():
                container.mux(packet)
            for packet in astream.encode():
                container.mux(packet)
        finally:
            container.close()
        os.replace(tmp, path)
        last_error = None
        return True
    except Exception as e:
        last_error = f"{type(e).__name__}: {e}"
        print(f"[save_av_mp4] 编码异常：{last_error}")
        import traceback
        tb = traceback.format_exc()
        print(tb)
        tail = _av_trace_tail(tb)
        if tail:
            last_error += " @" + tail
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def decode_av(path, start_f=None, end_f=None, fps=None):
    """上传视频 -> (帧 tensor[N,H,W,3] float 0-1, 波形[C,T] float32, 采样率 int|None)。

    不做音频重采样：采样器 _encode_audio_latent 会按音频 VAE 采样率重采样。
    无音频轨时返回 (帧, zeros(1,0), None)。文件无视频轨或 PyAV 缺失抛
    RuntimeError——序章上传路径失败应明确报错而非静默降级。

    帧窗（可选）：start_f/end_f 为像素帧号（end_f=None/<=0 表到片尾），fps 为
    该视频帧率（缺省按 24 计）。给出后 seek 到窗起点附近再解码，收到 end_f
    即早停——长文件只转一小段时不再全片解码（转 latent 卡死的根因之一）。
    音频仍整轨解码（AAC 解码快一个量级），由调用方按帧窗切片。
    """
    try:
        import av
        import numpy
        import torch
    except Exception as e:
        raise RuntimeError("解码上传视频需要 PyAV（ComfyUI 新视频栈自带）；缺失请安装 av 后重试") from e
    try:
        s0 = max(0, int(start_f)) if start_f else 0
    except (TypeError, ValueError):
        s0 = 0
    try:
        s1 = int(end_f) if end_f and int(end_f) > 0 else None
    except (TypeError, ValueError):
        s1 = None
    if s1 is not None and s1 <= s0:
        raise RuntimeError(f"解码帧窗为空（[{s0}, {s1})）")
    try:
        rate = float(fps) if fps and float(fps) > 0 else 24.0
    except (TypeError, ValueError):
        rate = 24.0
    with av.open(path) as container:
        vstream = next((s for s in container.streams if s.type == "video"), None)
        if vstream is None:
            raise RuntimeError("上传的文件里没有视频轨")
        # 窗起点较深时先 seek（微秒级时间戳，关键帧对齐），再用帧 pts 精确定位——
        # seek 落点只保证"附近"，逐帧计数在 seek 后会整体错位，必须按 pts 换算帧号
        sought = False
        if s0 > 240:
            try:
                container.seek(int(s0 / rate * 1000000), stream=vstream)
                sought = True
            except Exception:
                sought = False
        try:
            tb = float(vstream.time_base)
        except Exception:
            tb = 0.0
        frames = []
        idx = -1
        for f in container.decode(vstream):
            pts = getattr(f, "pts", None)
            if pts is not None and tb > 0:
                fno = int(round(pts * tb * rate))
            elif sought:
                fno = s0  # 无 pts 又 seek 过：无法定位，按窗内收（罕见，直接计入）
            else:
                idx += 1
                fno = idx
            if fno < s0:
                continue
            frames.append(f.to_ndarray(format="rgb24"))
            if s1 is not None and fno + 1 >= s1:
                break
        if not frames:
            raise RuntimeError("上传的视频里没有可解码的帧"
                               + (f"（帧窗 [{s0}, {s1 if s1 is not None else '尾'}）超出片长？）" if s0 else ""))
        astream = next((s for s in container.streams if s.type == "audio"), None)
        chunks, sample_rate = [], None
        if astream is not None:
            for f in container.decode(astream):
                arr = f.to_ndarray()  # fltp -> [C, T] float32
                if arr.ndim == 2 and arr.shape[0]:
                    chunks.append(arr)
                    sample_rate = f.sample_rate
    video = torch.from_numpy(numpy.stack(frames)).float() / 255.0
    if chunks:
        ch = chunks[0].shape[0]
        wav = torch.from_numpy(numpy.concatenate(
            [c[:ch] for c in chunks if c.shape[0] >= ch], axis=1))
    else:
        wav = torch.zeros(1, 0)
    return video, wav, sample_rate


def probe_video_size(path):
    """mp4 实测 (宽, 高)；无视频轨/打开失败返回 None（调用方自行兜底）。"""
    try:
        import av
        with av.open(path) as c:
            vs = next((s for s in c.streams if s.type == "video"), None)
            if vs is not None and vs.codec_context.width and vs.codec_context.height:
                return int(vs.codec_context.width), int(vs.codec_context.height)
    except Exception:
        pass
    return None


def concat_av_mp4(sources, out_path, width=None, height=None, fps=24, crf=20, threads=4,
                  preset="veryfast", aq_mode=None, dither=False):
    """多个 mp4 按序流式拼接为一个（H.264 + AAC），成功返回 True。

    合并导出专用：逐源 demux→decode→encode，视频帧不进 Python 侧累积
    （1080p 全帧进内存会到 GB 级），内存占用 ≈ 单帧。
    - 画幅统一 width×height（缺省取第一个源）：源尺寸不同时 reformat 缩放
      （项目段 mp4 同画幅时为直通，零损）
    - 帧率统一 fps：帧按解码顺序写入（源非 24fps 时线性重定时，建议素材
      上传前转 24fps；H3 全链输出本身即 24fps）
    - 音频以第一个含音轨源的参数为基准，源间 AudioResampler 统一采样率/声道；
      无音轨源按其视频时长补静音
    先写 .part 再原子改名；失败原因存 media.last_error。preset/aq_mode 与
    save_av_mp4 同口径，供二采高清成片沿用分段编码档位。dither 保留为同口径
    调用参数，但不在拼接阶段重复量化：源分段已在 float->uint8 时完成抖动，
    解码后的帧只有 8bit，再抖一次既无法恢复精度，还会破坏已有像素。
    """
    global last_error
    try:
        import av
        import numpy
    except Exception as e:
        last_error = f"PyAV/numpy 导入失败：{type(e).__name__}: {e}"
        return False
    if not sources:
        last_error = "合并清单为空"
        return False
    tmp = out_path + ".part"
    try:
        base_w = base_h = None
        base_rate, base_layout = None, None
        for p in sources:
            if not os.path.exists(p):
                raise RuntimeError(f"合并源不存在：{p}")
            with av.open(p) as probe:
                vs = next((s for s in probe.streams if s.type == "video"), None)
                if vs is None:
                    raise RuntimeError(f"{os.path.basename(p)} 没有视频轨，无法合并")
                if base_w is None:
                    base_w = int(vs.codec_context.width or 0)
                    base_h = int(vs.codec_context.height or 0)
                if base_rate is None:
                    aprobe = next((s for s in probe.streams if s.type == "audio"), None)
                    if aprobe is not None and aprobe.codec_context.sample_rate:
                        base_rate = int(aprobe.codec_context.sample_rate)
                        base_layout = "stereo" if (aprobe.codec_context.channels or 1) >= 2 else "mono"
        if base_rate is None:
            base_rate, base_layout = 44100, "stereo"
        if not base_w or not base_h:
            raise RuntimeError("无法探测源视频画幅")
        w, h = int(width or base_w), int(height or base_h)
        w, h = w - w % 2, h - h % 2   # yuv420p 要求偶数尺寸
        out_ch = 2 if base_layout == "stereo" else 1

        container = av.open(tmp, mode="w", format="mp4")
        try:
            _codec, options = _video_stream_options(crf, preset, threads, aq_mode)
            vstream = container.add_stream(_codec, rate=fps)
            vstream.width, vstream.height = w, h
            vstream.pix_fmt = "yuv420p"
            vstream.options = options
            astream = container.add_stream("aac", rate=base_rate, layout=base_layout)
            v_pts = a_pts = 0   # 全局单调时间戳：视频帧号 / 音频样点数（跨段连续）
            frame_tb = Fraction(1) / Fraction(fps)   # 兼容 int/float 帧率
            audio_parts = []    # 全段音频 PCM 统一在视频轨写完后编码（时长以视频为准）
            for p in sources:
                with av.open(p) as src:
                    vs = next((s for s in src.streams if s.type == "video"), None)
                    as_in = next((s for s in src.streams if s.type == "audio"), None)
                    n_frames = 0
                    got_audio = False
                    rs = (av.AudioResampler(format="fltp", layout=base_layout,
                                            rate=base_rate) if as_in is not None else None)
                    # 单趟多流解码：先解完视频再解音频会把容器内全部包耗尽，
                    # 音频轨解出 0 帧（PyAV demux 包按序消费，不回退）
                    for frame in src.decode(*([vs, as_in] if as_in is not None else [vs])):
                        if isinstance(frame, av.AudioFrame):
                            got_audio = True
                            rf = rs.resample(frame)
                            for f in (rf if isinstance(rf, list) else [rf]):
                                arr = f.to_ndarray()   # fltp -> [C, T] float32
                                if arr.ndim == 2 and arr.shape[0]:
                                    audio_parts.append(arr[:out_ch])
                            continue
                        vf = frame
                        if vf.width != w or vf.height != h or str(vf.format.name) != "yuv420p":
                            vf = vf.reformat(width=w, height=h, format="yuv420p")
                        # 显式线性重定时(帧号 + 1/fps 时基)。解码帧自带源时基
                        # (x264 段常见 1/12288)：只写 pts 不写 time_base 会被
                        # 重缩放到编码器时基，帧号除以倍率后大量塌缩成同一
                        # 时间戳，~260 帧起 dts 冲突被 mp4 mux 拒收(EINVAL 22)
                        vf.pts = v_pts
                        vf.time_base = frame_tb
                        v_pts += 1
                        n_frames += 1
                        for packet in vstream.encode(vf):
                            container.mux(packet)
                    if not got_audio:   # 无音轨源按其视频时长补静音
                        silent_n = int(round(n_frames / fps * base_rate))
                        audio_parts.append(
                            numpy.zeros((out_ch, max(0, silent_n)), dtype="float32"))
            pcm = (numpy.concatenate(audio_parts, axis=1) if audio_parts
                   else numpy.zeros((out_ch, 0), dtype="float32"))
            for start in range(0, pcm.shape[1], 1024):
                chunk = numpy.ascontiguousarray(pcm[:, start:start + 1024])
                af_out = av.AudioFrame.from_ndarray(chunk, format="fltp", layout=base_layout)
                af_out.sample_rate = base_rate
                af_out.pts = a_pts
                a_pts += chunk.shape[1]
                for packet in astream.encode(af_out):
                    container.mux(packet)
            for packet in vstream.encode():
                container.mux(packet)
            for packet in astream.encode():
                container.mux(packet)
        finally:
            container.close()
        os.replace(tmp, out_path)
        last_error = None
        return True
    except Exception as e:
        last_error = f"{type(e).__name__}: {e}"
        print(f"[concat_av_mp4] 合并异常：{last_error}")
        import traceback
        tb = traceback.format_exc()
        print(tb)   # EINVAL 之类环境错误需要完整栈才能定位抛出点
        tail = _av_trace_tail(tb)
        if tail:
            last_error += " @" + tail
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def probe_fps(path, default=24.0):
    """视频平均帧率；探测失败回 default（调用方显式记录兜底）。"""
    try:
        import av
        with av.open(path) as c:
            vs = next((s for s in c.streams if s.type == "video"), None)
            if vs is not None and getattr(vs, "average_rate", None):
                return float(vs.average_rate)
    except Exception:
        pass
    return float(default)


def probe_media(path):
    """媒体探针 -> {fps, frames, duration_s, width, height, has_audio}。

    frames 为估算（时长×帧率取整；变帧率/元数据缺失时可能偏差，供裁剪窗参考，
    精确值以解码为准）。失败抛 RuntimeError。
    """
    try:
        import av
    except Exception as e:
        raise RuntimeError("探测需要 PyAV（ComfyUI 新视频栈自带）") from e
    with av.open(path) as c:
        vs = next((s for s in c.streams if s.type == "video"), None)
        if vs is None:
            raise RuntimeError("文件里没有视频轨")
        fps = 24.0
        try:
            if getattr(vs, "average_rate", None):
                fps = float(vs.average_rate)
        except Exception:
            pass
        n = None
        try:
            if getattr(vs, "frames", 0):
                n = int(vs.frames)
        except Exception:
            n = None
        dur = None
        try:
            if getattr(vs, "duration", None) and getattr(vs, "time_base", None):
                dur = float(vs.duration * vs.time_base)
        except Exception:
            dur = None
        if n is None and dur:
            n = max(0, int(round(dur * fps)))
        if n is None:
            n = 0
        w = h = 0
        try:
            w, h = int(vs.codec_context.width or 0), int(vs.codec_context.height or 0)
        except Exception:
            pass
        has_audio = any(s.type == "audio" for s in c.streams)
    return {"fps": round(fps, 3), "frames": n,
            "duration_s": round(n / fps, 3) if fps > 0 else 0.0,
            "width": w, "height": h, "has_audio": bool(has_audio)}


def trim_av_mp4(src_path, out_path, start_s, end_s, fps=24, crf=20,
                preset="veryfast", aq_mode=None, dither=False):
    """入出点裁剪：[start_s, end_s) 秒窗口转码（流式）。

    旧版先整片解码再切片（长片直接 19GB 级 OOM 卡死整机）；现 seek 到窗起点、
    只解码窗口帧并逐帧编码落盘，内存 O(在途帧) 与片长无关。
    行为对齐旧版：输出从 0 计时、帧率取源实际帧率、crf/preset/aq/dither 同档、
    无音轨照样带空 aac 轨、先写 .part 再原子改名、失败 False + last_error。
    通道数/采样率以容器头为准（与解码值一致）；中途变径的坏包跳过
    （旧版整片解码会直接炸维度），中途变采样率的音频包丢弃。
    """
    global last_error
    try:
        import av
        import numpy
    except Exception as e:
        last_error = f"PyAV/numpy 导入失败：{type(e).__name__}: {e}"
        return False
    try:
        ss, es = float(start_s), float(end_s)
    except (TypeError, ValueError):
        last_error = "start_s/end_s 须为数字秒"
        return False
    if es <= ss or ss < 0:
        last_error = f"裁剪区间为空（[{ss}s, {es}s)，须 0 <= start < end）"
        return False
    real_fps = probe_fps(src_path, fps)
    s0 = max(0, int(round(ss * real_fps)))
    s1 = int(round(es * real_fps))
    if s1 <= s0:
        try:
            n_est = (probe_media(src_path) or {}).get("frames") or 0
        except Exception:
            n_est = 0
        last_error = (f"裁剪区间为空（[{ss}s, {es}s) -> 帧[{s0}, {s1})，"
                      f"源共{n_est}帧@{real_fps:.2f}fps）")
        return False
    out_fps = int(round(real_fps)) or 24
    tmp = out_path + ".part"
    try:
        with av.open(src_path) as inp:
            vs = next((s for s in inp.streams if s.type == "video"), None)
            if vs is None:
                raise RuntimeError("文件里没有视频轨")
            au = next((s for s in inp.streams if s.type == "audio"), None)
            try:
                vw, vh = int(vs.codec_context.width or 0), int(vs.codec_context.height or 0)
            except Exception:
                vw = vh = 0
            if not vw or not vh:
                vw, vh = probe_video_size(src_path) or (0, 0)
            if not vw or not vh:
                raise RuntimeError("无法识别视频尺寸")
            vw, vh = vw - vw % 2, vh - vh % 2  # yuv420p 要求偶数尺寸
            try:
                au_ch0 = int(au.codec_context.channels or 0) if au is not None else 0
            except Exception:
                au_ch0 = 0
            try:
                au_sr0 = int(au.codec_context.sample_rate or 0) if au is not None else 0
            except Exception:
                au_sr0 = 0
            if not au_ch0:
                au_ch0 = 2  # 通道数缺失时按立体声开轨（与旧版多数源一致），不匹配的包逐个丢弃
            sr = au_sr0 or 44100
            a0, a1 = max(0, int(round(s0 / real_fps * sr))), int(round(s1 / real_fps * sr))
            if s0 > 240:
                try:
                    inp.seek(int(s0 / real_fps * 1000000), stream=vs)
                    sought = True
                except Exception:
                    sought = False
            else:
                sought = False
            try:
                tb = float(vs.time_base)
            except Exception:
                tb = 0.0
            out = av.open(tmp, mode="w", format="mp4")
            try:
                _codec, _opts = _video_stream_options(crf, preset, 4, aq_mode)
                vo = out.add_stream(_codec, rate=out_fps)
                vo.width, vo.height = vw, vh
                vo.pix_fmt = "yuv420p"
                vo.options = _opts
                ao = out.add_stream(
                    "aac", rate=int(sr),
                    layout="stereo" if au_ch0 >= 2 else "mono")
                ao_ch = 2 if au_ch0 >= 2 else 1
                v_pts = a_pts = a_off = v_idx = kept_v = 0
                audio_chunks = []
                audio_bytes = 0
                v_done = False
                streams = [vs] + ([au] if au is not None else [])
                for packet in inp.demux(*streams):
                    try:
                        is_v = packet.stream.index == vs.index
                    except Exception:
                        is_v = getattr(packet.stream, "type", "") == "video"
                    if is_v:
                        if v_done:
                            continue
                        for frame in packet.decode():
                            pts = getattr(frame, "pts", None)
                            if pts is not None and tb > 0:
                                fno = int(round(pts * tb * real_fps))
                            elif sought:
                                fno = s0
                            else:
                                v_idx += 1
                                fno = v_idx
                            if fno < s0:
                                continue
                            if fno >= s1:
                                v_done = True
                                break
                            arr = frame.to_ndarray(format="rgb24")
                            if arr.shape[0] != vh or arr.shape[1] != vw:
                                continue
                            enc = dither_quantize(arr) if dither \
                                else numpy.ascontiguousarray(arr)
                            vframe = av.VideoFrame.from_ndarray(enc, format="rgb24")
                            vframe.pts = v_pts
                            v_pts += 1
                            kept_v += 1
                            for pkt in vo.encode(vframe):
                                out.mux(pkt)
                    elif au is not None:
                        try:
                            is_a = packet.stream.index == au.index
                        except Exception:
                            is_a = getattr(packet.stream, "type", "") == "audio"
                        if not is_a:
                            continue
                        for frame in packet.decode():
                            try:
                                arr = frame.to_ndarray()
                            except Exception:
                                continue
                            if getattr(arr, "ndim", 0) != 2 or not arr.shape[0]:
                                continue
                            if int(getattr(frame, "sample_rate", 0) or 0) not in (0, sr):
                                continue  # 中途变采样率：编码器改不了率，丢包
                            if arr.shape[0] < ao_ch:
                                continue
                            arr = arr[:ao_ch]
                            n_samp = int(arr.shape[1])
                            f0, f1 = a_off, a_off + n_samp
                            a_off = f1
                            lo, hi = max(f0, a0), min(f1, a1)
                            if hi > lo:
                                cut = numpy.ascontiguousarray(arr[:, lo - f0:hi - f0])
                                audio_chunks.append(cut)
                                audio_bytes += cut.nbytes
                                if audio_bytes > (1 << 30):
                                    raise RuntimeError(
                                        "音频窗口超过 1GB（超长裁剪区间），请缩小区间分多次裁剪")
                    if v_done and (au is None or a_off >= a1):
                        break
                if kept_v <= 0:
                    raise RuntimeError(
                        f"窗口内无可解码帧（[{ss}s, {es}s) -> 帧[{s0}, {s1})，超出片长？）")
                if audio_chunks:
                    import numpy as _np
                    pcm = _np.concatenate(audio_chunks, axis=1)
                else:
                    import numpy as _np
                    pcm = _np.zeros((ao_ch, 0), dtype=_np.float32)
                for start in range(0, pcm.shape[1], 1024):
                    aframe = av.AudioFrame.from_ndarray(
                        pcm[:, start:start + 1024], format="fltp",
                        layout="stereo" if ao_ch >= 2 else "mono")
                    aframe.sample_rate = int(sr)
                    aframe.pts = a_pts
                    a_pts += aframe.samples
                    for pkt in ao.encode(aframe):
                        out.mux(pkt)
                for pkt in vo.encode():
                    out.mux(pkt)
                for pkt in ao.encode():
                    out.mux(pkt)
            finally:
                out.close()
        os.replace(tmp, out_path)
        last_error = None
        return True
    except Exception as e:
        last_error = f"{type(e).__name__}: {e}"
        print(f"[trim_av_mp4] 裁剪异常：{last_error}")
        import traceback
        traceback.print_exc()
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def save_wav_pcm16(path, pcm, sample_rate):
    """[C,T]/[1,C,T] float32 -1..1（torch 或 numpy）-> 标准 PCM16 wav。

    成功 True；失败 False（last_error）。供 split_av_stream 与调用方共用。
    """
    global last_error
    try:
        import wave
        import numpy as _np
        a = pcm.detach().float().cpu().numpy() if hasattr(pcm, "detach") else _np.asarray(pcm)
        a = _np.ascontiguousarray(a, dtype=_np.float32).clip(-1.0, 1.0)
        if a.ndim == 3:
            a = a[0]
        ch = int(a.shape[0]) if a.ndim == 2 else 1
        if a.ndim != 2:
            a = a.reshape(1, -1)
            ch = 1
        arr = (a.transpose(1, 0) * 32767.0).clip(-32768, 32767).astype("<i2")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(min(2, ch))
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate or 44100))
            wf.writeframes(arr.tobytes())
        last_error = None
        return True
    except Exception as e:
        last_error = f"{type(e).__name__}: {e}"
        return False


def split_av_stream(src_path, video_out_path, wav_out_path):
    """单遍 demux 音画分离（流式）：视频轨 packet 级 remux（零解码零重编码），
    音频轨解码收 PCM 写 wav。内存 O(音频)（约 350KB/s），视频零内存。

    旧版先整片解码（长片 19GB 级 OOM 卡死整机）；现视频不碰像素。
    无音频轨时只产出视频（audio=False，调用方记 note=no-audio，与旧行为一致）。
    返回 {"video": True, "audio": bool, "sample_rate": int|None,
            "width": int, "height": int, "fps": float}，
    失败返回 None（last_error）。
    """
    global last_error
    try:
        import av
        import numpy
    except Exception as e:
        last_error = f"PyAV/numpy 导入失败：{type(e).__name__}: {e}"
        return None
    tmp_v = video_out_path + ".part"
    try:
        with av.open(src_path) as inp:
            vs = next((s for s in inp.streams if s.type == "video"), None)
            if vs is None:
                raise RuntimeError("文件里没有视频轨")
            au = next((s for s in inp.streams if s.type == "audio"), None)
            try:
                vw, vh = int(vs.codec_context.width or 0), int(vs.codec_context.height or 0)
            except Exception:
                vw = vh = 0
            out = av.open(tmp_v, mode="w", format="mp4")
            try:
                out_v = out.add_stream(template=vs)
                chunks = []
                audio_bytes = 0
                sr = None
                ch = 0
                streams = [vs] + ([au] if au is not None else [])
                for packet in inp.demux(*streams):
                    try:
                        is_v = packet.stream.index == vs.index
                    except Exception:
                        is_v = getattr(packet.stream, "type", "") == "video"
                    if is_v:
                        packet.stream = out_v
                        out.mux(packet)
                        continue
                    if au is None:
                        continue
                    try:
                        is_a = packet.stream.index == au.index
                    except Exception:
                        is_a = getattr(packet.stream, "type", "") == "audio"
                    if not is_a:
                        continue
                    for frame in packet.decode():
                        try:
                            arr = frame.to_ndarray()
                        except Exception:
                            continue
                        if getattr(arr, "ndim", 0) != 2 or not arr.shape[0]:
                            continue
                        if sr is None:
                            sr = int(getattr(frame, "sample_rate", 0) or 0) or None
                            ch = int(arr.shape[0])
                        if arr.shape[0] < ch:
                            continue
                        cut = arr[:ch]
                        chunks.append(numpy.ascontiguousarray(cut))
                        audio_bytes += cut.nbytes
                        if audio_bytes > (1 << 30):
                            raise RuntimeError(
                                "音轨超过 1GB（超长文件），请先裁剪再分离")
            finally:
                out.close()
        os.replace(tmp_v, video_out_path)
        has_audio = bool(chunks)
        if has_audio:
            import numpy as _np
            pcm = _np.concatenate(chunks, axis=1)
            if not save_wav_pcm16(wav_out_path, pcm, sr or 44100):
                raise RuntimeError(f"wav 写入失败：{last_error}")
        try:
            fps = float(probe_fps(src_path, 24.0))
        except Exception:
            fps = 24.0
        last_error = None
        return {"video": True, "audio": has_audio,
                "sample_rate": (int(sr) if has_audio and sr else None),
                "width": vw, "height": vh, "fps": fps}
    except Exception as e:
        last_error = f"{type(e).__name__}: {e}"
        print(f"[split_av_stream] 分离异常：{last_error}")
        import traceback
        traceback.print_exc()
        for p in (tmp_v,):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        return None
