"""分离/裁剪流式回归（无真实 PyAV，用 fake-av 桩；numpy/torch 真实）。

跑法（仓库根目录）：
    python -m pytest tests/test_media_stream.py -q

核心断言（防 19GB 级 OOM 回归）：
- trim 长片只解码窗口附近帧（计数器证明），而非整片；
- split 视频轨零解码（packet 直通 remux）；
- 深窗口触发 seek；空窗/坏窗清晰失败；
- wav 内容正确（声道/采样率/帧数）。
"""

import os
import sys
import tempfile
import types
import wave

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3streamtest_")


# ---------- fake av ----------

class _FakeState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.decoded_video = 0
        self.decoded_audio = 0
        self.muxed_video = 0
        self.video_encode_calls = 0
        self.audio_encode_calls = 0
        self.seeks = []


STATE = _FakeState()


class _FakeCodecCtx:
    def __init__(self, width=0, height=0, sample_rate=0, channels=0):
        self.width = width
        self.height = height
        self.sample_rate = sample_rate
        self.channels = channels


class _FakeStream:
    def __init__(self, type, index, **kw):
        self.type = type
        self.index = index
        self.time_base = kw.get("tb", 1 / 24)
        self.average_rate = kw.get("rate", 24.0)
        self.frames = kw.get("frames", 0)
        self.duration = kw.get("duration", None)
        self.codec_context = _FakeCodecCtx(
            kw.get("width", 0), kw.get("height", 0),
            kw.get("sample_rate", 0), kw.get("channels", 0))


class _FakeVideoFrame:
    def __init__(self, pts, w, h):
        self.pts = pts
        self.width = w
        self.height = h

    def to_ndarray(self, format="rgb24"):
        import numpy
        return numpy.zeros((self.height, self.width, 3), dtype="uint8")


class _FakeAudioFrame:
    def __init__(self, data, sample_rate):
        self._data = data
        self.sample_rate = sample_rate
        self.samples = int(data.shape[1])

    def to_ndarray(self):
        return self._data


class _FakePacket:
    def __init__(self, stream, frames):
        self.stream = stream
        self._frames = frames

    def decode(self):
        if self.stream.type == "video":
            STATE.decoded_video += len(self._frames)
        else:
            STATE.decoded_audio += len(self._frames)
        return iter(self._frames)


class _FakeInputContainer:
    """长片桩：n_video 帧（pts 连续）+ n_audio 音频块；seek 按关键帧余量回退。"""

    def __init__(self, path, n_video=2400, n_audio=0, w=8, h=8,
                 rate=24.0, sr=44100, ch=2):
        self.path = path
        self._w, self._h = w, h
        self._rate = rate
        self._sr = sr
        self._ch = ch
        self.vs = _FakeStream("video", 0, width=w, height=h, rate=rate,
                             frames=n_video, duration=n_video / rate,
                             tb=1 / rate)
        self.streams = [self.vs]
        self.au = None
        if n_audio:
            self.au = _FakeStream("audio", 1, sample_rate=sr, channels=ch,
                                  tb=1 / sr, rate=rate)
            self.streams.append(self.au)
        import numpy
        self._vpackets = [_FakePacket(self.vs, [_FakeVideoFrame(i, w, h)])
                          for i in range(n_video)]
        self._apackets = []
        per = 1024
        for j in range(n_audio):
            import numpy as _np
            self._apackets.append(_FakePacket(
                self.au, [_FakeAudioFrame(
                    _np.zeros((ch, per), dtype="float32"), sr)]))
        self._seek_v = 0
        self._seek_a = 0

    def seek(self, pos_us, stream=None):
        STATE.seeks.append(pos_us)
        fno = int(pos_us / 1000000 * self._rate)
        self._seek_v = max(0, fno - 17)  # seek 落点只保证附近（关键帧量化）
        self._seek_a = max(0, int((fno - 17) / self._rate * self._sr) // 1024)

    def demux(self, *streams):
        vps = [p for p in self._vpackets
               if p._frames[0].pts >= self._seek_v]
        aps = self._apackets[self._seek_a:]
        i = j = 0
        while i < len(vps) or j < len(aps):
            if i < len(vps):
                yield vps[i]
                i += 1
            if j < len(aps):
                yield aps[j]
                j += 1

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class _FakeOutStream:
    def __init__(self):
        self.options = {}
        self.width = self.height = 0
        self.pix_fmt = ""
        self.encode_calls = 0

    def encode(self, frame=None):
        if frame is not None:
            self.encode_calls += 1
            if getattr(frame, "_is_video", False):
                STATE.video_encode_calls += 1
            else:
                STATE.audio_encode_calls += 1
        return []


class _FakeOutContainer:
    def __init__(self, path):
        self.path = path
        self.muxed = 0

    def add_stream(self, *args, **kwargs):
        return _FakeOutStream()

    def mux(self, packet):
        self.muxed += 1
        STATE.muxed_video += 1

    def close(self):
        with open(self.path, "wb") as fh:
            fh.write(b"fake-mp4")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class _FakeVideoFrameOut:
    _is_video = True

    def __init__(self):
        self.pts = 0

    @staticmethod
    def from_ndarray(arr, format="rgb24"):
        return _FakeVideoFrameOut()


class _FakeAudioFrameOut:
    _is_video = False

    def __init__(self, samples):
        self.samples = samples
        self.pts = 0
        self.sample_rate = 0

    @staticmethod
    def from_ndarray(arr, format="fltp", layout="stereo"):
        import numpy
        return _FakeAudioFrameOut(int(numpy.asarray(arr).shape[1]))


def _make_fake_av(container):
    mod = types.ModuleType("av")
    mod.open = lambda path, mode="r", format=None: (
        _FakeOutContainer(path) if mode == "w" else container)
    mod.VideoFrame = _FakeVideoFrameOut
    mod.AudioFrame = _FakeAudioFrameOut
    return mod


def _load_top(name, path, patches=None):
    src = open(path, encoding="utf-8").read()
    if patches:
        for a, b in patches:
            src = src.replace(a, b)
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    sys.modules[name] = mod
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


@pytest.fixture()
def fake_av():
    STATE.reset()
    container = _FakeInputContainer("x.mp4", n_video=2400, n_audio=4307)
    sys.modules["av"] = _make_fake_av(container)
    yield container
    sys.modules.pop("av", None)


@pytest.fixture(scope="module", autouse=True)
def _env():
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = lambda: TMP
    fp.get_input_directory = lambda: TMP
    fp.get_annotated_filepath = lambda f: os.path.join(TMP, f)
    fp.get_user_directory = lambda: TMP
    sys.modules["folder_paths"] = fp
    os.makedirs(os.path.join(TMP, "h3_projects"), exist_ok=True)
    yield


@pytest.fixture()
def mods():
    checkpoint = _load_top("checkpoint", os.path.join(ROOT, "checkpoint.py"))
    media = _load_top("media", os.path.join(ROOT, "media.py"))
    projects = _load_top("projects", os.path.join(ROOT, "projects.py"),
                         [("from . import checkpoint", "import checkpoint")])
    return types.SimpleNamespace(checkpoint=checkpoint, media=media, projects=projects)


# ---------- trim ----------

def test_trim_window_bounded(fake_av, mods, tmp_path):
    out = str(tmp_path / "clip.mp4")
    assert mods.media.trim_av_mp4("src.mp4", out, 100 / 24, 124 / 24) is True
    assert os.path.isfile(out)
    # 2400 帧长片只解了窗口附近（旧版整片 2400）
    assert STATE.decoded_video <= 200, STATE.decoded_video
    assert STATE.decoded_audio <= 400, STATE.decoded_audio
    assert STATE.video_encode_calls == 24, STATE.video_encode_calls
    assert STATE.seeks == []  # 浅窗口不 seek


def test_trim_deep_seek(fake_av, mods, tmp_path):
    out = str(tmp_path / "clip2.mp4")
    assert mods.media.trim_av_mp4("src.mp4", out, 1000 / 24, 1024 / 24) is True
    assert len(STATE.seeks) == 1
    assert STATE.decoded_video <= 150, STATE.decoded_video
    assert STATE.video_encode_calls == 24


def test_trim_bad_window(fake_av, mods, tmp_path):
    out = str(tmp_path / "bad.mp4")
    assert mods.media.trim_av_mp4("src.mp4", out, 5.0, 5.0) is False
    assert "为空" in (mods.media.last_error or "")
    assert not os.path.exists(out)


def test_trim_no_video_track(mods, tmp_path):
    c = _FakeInputContainer("x.mp4", n_video=0)
    c.vs = None
    c.streams = []
    sys.modules["av"] = _make_fake_av(c)
    out = str(tmp_path / "nov.mp4")
    assert mods.media.trim_av_mp4("src.mp4", out, 0.0, 1.0) is False
    assert "视频轨" in (mods.media.last_error or "")


# ---------- split ----------

def test_split_zero_decode(fake_av, mods, tmp_path):
    container = fake_av
    container._vpackets = container._vpackets[:300]
    out_v = str(tmp_path / "s_v.mp4")
    out_a = str(tmp_path / "s_a.wav")
    info = mods.media.split_av_stream("src.mp4", out_v, out_a)
    assert info and info["video"] is True and info["audio"] is True
    assert STATE.decoded_video == 0  # 视频零解码（remux 直通）
    assert STATE.muxed_video == 300
    assert os.path.isfile(out_v) and os.path.isfile(out_a)
    with wave.open(out_a, "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getframerate() == 44100
        assert wf.getnframes() == 4307 * 1024
    assert info["sample_rate"] == 44100


def test_split_no_audio(mods, tmp_path):
    sys.modules["av"] = _make_fake_av(_FakeInputContainer("x.mp4", n_video=50, n_audio=0))
    out_v = str(tmp_path / "n_v.mp4")
    out_a = str(tmp_path / "n_a.wav")
    info = mods.media.split_av_stream("src.mp4", out_v, out_a)
    assert info and info["video"] is True and info["audio"] is False
    assert os.path.isfile(out_v) and not os.path.exists(out_a)


def test_save_wav_roundtrip(mods, tmp_path):
    import numpy
    p = str(tmp_path / "t.wav")
    assert mods.media.save_wav_pcm16(p, numpy.zeros((2, 4410), dtype="float32"), 44100) is True
    with wave.open(p, "rb") as wf:
        assert (wf.getnchannels(), wf.getframerate(), wf.getnframes()) == (2, 44100, 4410)


# ---------- projects 集成 ----------

def test_projects_trim_bounded(fake_av, mods):
    projects = mods.projects
    m = projects.create_project("t_stream")
    root = os.path.join(TMP, "h3_projects", "t_stream")
    open(os.path.join(root, "seg_000.mp4"), "wb").write(b"\x00")
    m2 = projects.trim_asset("t_stream", "seg_000.mp4", 100 / 24, 124 / 24,
                             "cut.mp4", base_revision=m["revision"])
    clips = [c for c in m2["clips"] if c["file"].endswith("cut.mp4")]
    assert len(clips) == 1 and clips[0]["start_s"] == 100 / 24
    assert STATE.decoded_video <= 200
    assert os.path.isfile(os.path.join(root, "finals", "cut.mp4"))


def test_projects_split_manifest(fake_av, mods):
    projects = mods.projects
    container = fake_av
    container._vpackets = container._vpackets[:120]
    m = projects.create_project("t_split")
    root = os.path.join(TMP, "h3_projects", "t_split")
    open(os.path.join(root, "seg_000.mp4"), "wb").write(b"\x00")
    m2 = projects.split_av("t_split", "seg_000.mp4", base_revision=m["revision"])
    files = [c["file"] for c in m2["clips"]]
    assert any(f.endswith("_v.mp4") for f in files)
    assert any(f.endswith("_a.wav") for f in files)
    assert STATE.decoded_video == 0
    assert os.path.isfile(os.path.join(root, "finals", "seg_000_v.mp4"))
