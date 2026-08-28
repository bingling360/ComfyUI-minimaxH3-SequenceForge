"""PyAV 编解码共享：mp4 落盘与上传视频解码。

分镜段视频（checkpoint.save_segment_mp4）、采样器自动保存（nodes 自动成片）
两处共用，编码逻辑只维护一份。
PyAV 是 ComfyUI 新视频栈（CreateVideo/SaveVideo）的既有依赖，无新依赖。
"""

import os
from fractions import Fraction

last_error = None

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
            vstream = container.add_stream("libx264", rate=fps)
            vstream.width, vstream.height = w, h
            vstream.pix_fmt = "yuv420p"
            options = {"crf": str(int(crf)), "preset": str(preset),
                       "threads": str(max(1, int(threads)))}
            if aq_mode:
                options["aq-mode"] = str(int(aq_mode))
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


def decode_av(path):
    """上传视频 -> (帧 tensor[N,H,W,3] float 0-1, 波形[C,T] float32, 采样率 int|None)。

    不做音频重采样：采样器 _encode_audio_latent 会按音频 VAE 采样率重采样。
    无音频轨时返回 (帧, zeros(1,0), None)。文件无视频轨或 PyAV 缺失抛
    RuntimeError——序章上传路径失败应明确报错而非静默降级。
    """
    try:
        import av
        import numpy
        import torch
    except Exception as e:
        raise RuntimeError("解码上传视频需要 PyAV（ComfyUI 新视频栈自带）；缺失请安装 av 后重试") from e
    with av.open(path) as container:
        vstream = next((s for s in container.streams if s.type == "video"), None)
        if vstream is None:
            raise RuntimeError("上传的文件里没有视频轨")
        frames = [f.to_ndarray(format="rgb24") for f in container.decode(vstream)]
        if not frames:
            raise RuntimeError("上传的视频里没有可解码的帧")
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
            vstream = container.add_stream("libx264", rate=fps)
            vstream.width, vstream.height = w, h
            vstream.pix_fmt = "yuv420p"
            options = {"crf": str(int(crf)), "preset": str(preset),
                       "threads": str(max(1, int(threads)))}
            if aq_mode:
                options["aq-mode"] = str(int(aq_mode))
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
