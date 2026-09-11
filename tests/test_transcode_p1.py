"""P1 后台转码队列回归（无 ComfyUI 可跑）。

跑法（仓库根目录）：
    python -m pytest tests/test_transcode_p1.py -q

覆盖：job 表状态机（提交校验/FIFO认领/进度/完成/失败/取消/过滤）、
projects.link_asset（追加/重指向/改名/乐观锁）、新路由挂载与 handler
（submit 400/404/ok、list/get/cancel、library_upload JSON 入库+链接），
H3MediaToLatent/主节点 wiring 字符串断言（执行需 Comfy 内验）。
"""

import asyncio
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3p1test_")


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


@pytest.fixture(scope="session", autouse=True)
def _env():
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = lambda: TMP
    fp.get_input_directory = lambda: TMP
    fp.get_annotated_filepath = lambda f: os.path.join(TMP, f)
    fp.get_user_directory = lambda: TMP
    sys.modules["folder_paths"] = fp
    aio = types.ModuleType("aiohttp")

    class _Resp:
        def __init__(self, data, status=200):
            self.data = data
            self.status = status

        async def json(self):
            return self.data

    class _Web:
        @staticmethod
        def json_response(data, status=200):
            return _Resp(data, status)

    aio.web = _Web()
    sys.modules["aiohttp"] = aio
    os.makedirs(os.path.join(TMP, "h3_projects"), exist_ok=True)
    yield


@pytest.fixture(scope="session")
def tq():
    return _load_top("transcode_queue", os.path.join(ROOT, "transcode_queue.py"))


@pytest.fixture(scope="session")
def store():
    return _load_top("asset_store", os.path.join(ROOT, "asset_store.py"))


@pytest.fixture(scope="session")
def checkpoint():
    return _load_top("checkpoint", os.path.join(ROOT, "checkpoint.py"))


@pytest.fixture(scope="session")
def projects(checkpoint):
    return _load_top("projects", os.path.join(ROOT, "projects.py"),
                     [("from . import checkpoint", "import checkpoint")])


@pytest.fixture(scope="session")
def routes(projects, tq, store, checkpoint):
    return _load_top("routes", os.path.join(ROOT, "routes.py"),
                     [("from . import projects", "import projects"),
                      ("from . import asset_hub", "import asset_hub"),
                      ("from . import asset_store", "import asset_store"),
                      ("from . import transcode_queue as _tq", "import transcode_queue as _tq"),
                      ("from . import transcode_queue", "import transcode_queue"),
                      ("from . import checkpoint as _ckpt", "import checkpoint as _ckpt"),
                      ("from . import prompts as _prompts", "import prompts as _prompts")])


@pytest.fixture(autouse=True)
def _clean(tq):
    tq.reset()
    yield
    tq.reset()


# ---- job 表 ----

def test_submit_validate(tq):
    j = tq.submit("p1", "finals/a.mp4", 0, 0, "图像+音频", "否", "a.pt")
    assert j["status"] == "queued" and j["progress"] == 0.0 and j["id"].startswith("tq_")
    with pytest.raises(ValueError):
        tq.submit("p1", "a.mp4", 0, 0, "图像+音频", "否", "bad.mp4x")
    with pytest.raises(ValueError):
        tq.submit("p1", "a.mp4", 2, 2, "图像+音频", "否", "a.pt")
    with pytest.raises(ValueError):
        tq.submit("p1", "a.mp4", 0, 0, "只要视频", "否", "a.pt")
    with pytest.raises(ValueError):
        tq.submit("", "a.mp4", 0, 0, "图像+音频", "否", "a.pt")


def test_lifecycle_fifo_progress(tq):
    a = tq.submit("p", "a.mp4", 0, 0, "图像+音频", "否", "a.pt")
    b = tq.submit("p", "b.mp4", 0, 0, "仅图像", "否", "b.pt")
    c = tq.claim("p")
    assert c["id"] == a["id"] and c["status"] == "running"
    tq.progress(a["id"], 0.5, "编码中")
    assert tq.get(a["id"])["progress"] == 0.5
    tq.complete(a["id"], "ok-report")
    done = tq.get(a["id"])
    assert done["status"] == "done" and done["progress"] == 1.0
    assert tq.claim("p")["id"] == b["id"]
    tq.fail(b["id"], "boom")
    assert tq.get(b["id"])["status"] == "error"
    assert tq.claim("p") is None


def test_cancel_paths(tq):
    q = tq.submit("p", "a.mp4", 0, 0, "图像+音频", "否", "a.pt")
    assert tq.cancel(q["id"])["status"] == "cancelled"
    r = tq.submit("p", "b.mp4", 0, 0, "图像+音频", "否", "b.pt")
    tq.begin(r["id"])
    assert tq.cancel(r["id"])["status"] == "running"  # running 只标记
    assert tq.is_cancel_requested(r["id"]) is True
    assert tq.interrupted_checker(r["id"])() is True
    assert tq.finish_cancelled(r["id"])["status"] == "cancelled"
    assert tq.cancel("tq_missing") is None and tq.get("tq_missing") is None


def test_list_filter(tq):
    tq.submit("p1", "a.mp4", 0, 0, "图像+音频", "否", "a.pt")
    tq.submit("p2", "b.mp4", 0, 0, "图像+音频", "否", "b.pt")
    assert len(tq.list_jobs()) == 2 and len(tq.list_jobs("p1")) == 1


def test_has_queued(tq):
    assert tq.has_queued("p9") is False
    j = tq.submit("p9", "a.mp4", 0, 0, "图像+音频", "否", "a.pt")
    assert tq.has_queued("p9") is True and tq.has_queued("other") is False
    assert tq.has_queued() is True
    tq.cancel(j["id"])
    assert tq.has_queued("p9") is False


# ---- link_asset ----

def test_link_asset_roundtrip(projects):
    m = projects.create_project("t_link")
    m2 = projects.link_asset("t_link", "a_1234567890ab", "主角", "image",
                             base_revision=m["revision"])
    assert m2["revision"] == m["revision"] + 1
    assert m2["asset_links"] == [{"asset_id": "a_1234567890ab", "alias": "主角", "kind": "image"}]
    # 同 alias 重指向
    m3 = projects.link_asset("t_link", "a_abcdefabcdef", "主角", "video",
                             base_revision=m2["revision"])
    assert m3["asset_links"] == [{"asset_id": "a_abcdefabcdef", "alias": "主角", "kind": "video"}]
    # 同 id 改名
    m4 = projects.link_asset("t_link", "a_abcdefabcdef", "主角2", "video",
                             base_revision=m3["revision"])
    assert m4["asset_links"][0]["alias"] == "主角2"
    # 非法 id / 乐观锁
    assert projects.link_asset("t_link", "bad-id", "x") is None
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        projects.link_asset("t_link", "a_1234567890ab", "y", base_revision=1)
    assert projects.link_asset("no_such_proj", "a_1234567890ab", "y") is None


# ---- routes ----

class _Router:
    def __init__(self):
        self.paths = []

    def add_get(self, path, handler):
        self.paths.append(("GET", path))

    def add_post(self, path, handler):
        self.paths.append(("POST", path))


class _Req:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


def _handlers(routes):
    h = {}
    r2 = types.SimpleNamespace(
        add_get=lambda p, fn: h.setdefault(("GET", p), fn),
        add_post=lambda p, fn: h.setdefault(("POST", p), fn))
    routes.add_routes(r2)
    return h


def test_new_routes_mount(routes):
    r = _Router()
    routes.add_routes(r)
    for method, p in [("POST", "/h3chain/transcode_submit"),
                      ("GET", "/h3chain/transcode_jobs"),
                      ("GET", "/h3chain/transcode_job"),
                      ("POST", "/h3chain/transcode_cancel"),
                      ("POST", "/h3chain/library_upload")]:
        assert (method, p) in r.paths, (method, p)
        assert (method, "/api" + p) in r.paths, (method, "/api" + p)


def test_submit_handler(routes, projects):
    h = _handlers(routes)
    fn = h[("POST", "/h3chain/transcode_submit")]
    bad = asyncio.run(fn(_Req({"dir": "..", "src": "a.mp4", "save_name": "a.pt"})))
    assert bad.status == 400
    missing = asyncio.run(fn(_Req({"dir": "t_noproj", "src": "a.mp4", "save_name": "a.pt"})))
    assert missing.status == 404
    projects.create_project("t_tq")
    badsrc = asyncio.run(fn(_Req({"dir": "t_tq", "src": "../evil.mp4", "save_name": "a.pt"})))
    assert badsrc.status == 400
    nosrc = asyncio.run(fn(_Req({"dir": "t_tq", "src": "nope.mp4", "save_name": "a.pt"})))
    assert nosrc.status == 404
    open(os.path.join(TMP, "h3_projects", "t_tq", "seg_000.mp4"), "wb").write(b"\x00")
    badname = asyncio.run(fn(_Req({"dir": "t_tq", "src": "seg_000.mp4", "save_name": "x.mp4x"})))
    assert badname.status == 400
    ok = asyncio.run(fn(_Req({"dir": "t_tq", "src": "seg_000.mp4", "save_name": "head.pt",
                              "branch": "仅图像"})))
    assert ok.status == 200 and ok.data["job"]["status"] == "queued"
    assert ok.data["job"]["branch"] == "仅图像"


def test_jobs_poll_cancel_handlers(routes, projects):
    h = _handlers(routes)
    projects.create_project("t_tq2")
    open(os.path.join(TMP, "h3_projects", "t_tq2", "seg_000.mp4"), "wb").write(b"\x00")
    sub = h[("POST", "/h3chain/transcode_submit")]
    jid = asyncio.run(sub(_Req({"dir": "t_tq2", "src": "seg_000.mp4", "save_name": "a.pt"}))).data["job"]["id"]
    lst = asyncio.run(h[("GET", "/h3chain/transcode_jobs")](_Req(query={"dir": "t_tq2"})))
    assert lst.data["ok"] and len(lst.data["jobs"]) == 1
    one = asyncio.run(h[("GET", "/h3chain/transcode_job")](_Req(query={"id": jid})))
    assert one.data["job"]["id"] == jid
    gone = asyncio.run(h[("GET", "/h3chain/transcode_job")](_Req(query={"id": "tq_nope"})))
    assert gone.status == 404
    cancelled = asyncio.run(h[("POST", "/h3chain/transcode_cancel")](_Req({"id": jid})))
    assert cancelled.data["job"]["status"] == "cancelled"
    miss = asyncio.run(h[("POST", "/h3chain/transcode_cancel")](_Req({"id": "tq_nope"})))
    assert miss.status == 404


def test_library_upload_json(routes, projects):
    h = _handlers(routes)
    fn = h[("POST", "/h3chain/library_upload")]
    # 非 JSON/无 multipart（stub）-> 400 而非崩溃
    r = asyncio.run(fn(_Req({})))
    assert r.status == 400
    # 非法路径
    r = asyncio.run(fn(_Req({"src": "../evil.png"})))
    assert r.status == 400
    # input 缺文件
    r = asyncio.run(fn(_Req({"src": "ghost.png"})))
    assert r.status == 404
    # 正常入库 + 项目链接
    open(os.path.join(TMP, "up_hero.png"), "wb").write(b"png-bytes")
    projects.create_project("t_uplink")
    r = asyncio.run(fn(_Req({"src": "up_hero.png", "kind": "image",
                             "tags": ["角色"], "link_dir": "t_uplink", "alias": "英雄"})))
    assert r.status == 200 and r.data["entry"]["asset_id"].startswith("a_")
    assert r.data["alias"] == "英雄"
    assert any(x["alias"] == "英雄" for x in r.data["manifest"]["asset_links"])
    # 类别不符
    r = asyncio.run(fn(_Req({"src": "up_hero.png", "kind": "audio"})))
    assert r.status == 400


# ---- wiring 字符串（执行需 Comfy 内验） ----

def test_shim_wiring_strings():
    lt = open(os.path.join(ROOT, "latent_tools.py"), encoding="utf-8").read()
    # P4d：H3MediaToLatent 节点类已删除（转码走主节点自动专跑）
    assert "class H3MediaToLatent" not in lt and "任务ID" not in lt
    assert "def run_transcode_job(" in lt  # 纯函数保留，主节点共用
    nd = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert "_tq.submit" in nd and "_tq.complete" in nd and "_tq.fail" in nd
    # 提交即执行：后台表有排队即进专跑（无需 ds 任务）
    assert "_server_pending" in nd and "has_queued" in nd
    assert "if _jobs or _server_pending:" in nd
    assert "共完成" in nd
    api = open(os.path.join(ROOT, "web", "h3_api.js"), encoding="utf-8").read()
    for sym in ["transcodeSubmit", "transcodeJobs", "transcodeJob",
                "transcodeCancel", "libraryUploadJson", "libraryUpload"]:
        assert sym in api, sym
    d = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    # 提交后自动排队执行（两按钮走 autoQueueTranscode，最小图失败回落整图）
    assert d.count("await autoQueueTranscode(") >= 2
    assert "自动排队执行" in d


def test_vae_files_route(routes):
    r = _Router()
    routes.add_routes(r)
    assert ("GET", "/h3chain/vae_files") in r.paths
    assert ("GET", "/api/h3chain/vae_files") in r.paths
    h = _handlers(routes)
    fp = sys.modules["folder_paths"]
    fp.get_filename_list = lambda _folder: [
        "minimax_h3_video_vae_fp16.safetensors",
        "minimax_h3_audio_vae_fp32.safetensors",
        "other.safetensors",
    ]
    try:
        res = asyncio.run(h[("GET", "/h3chain/vae_files")](_Req()))
    finally:
        del fp.get_filename_list
    assert res.status == 200 and len(res.data["files"]) == 3


def test_minimal_graph_wiring():
    nd = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert 'io.Model.Input("模型", optional=True' in nd
    assert 'io.Clip.Input("文本编码器", optional=True' in nd
    assert 'kwargs.setdefault("模型", None)' in nd
    assert 'kwargs.setdefault("文本编码器", None)' in nd
    d = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    for sym in ["transcodeVaeFiles", "queueTranscodeRun", "autoQueueTranscode",
                '"/prompt"', "H3SeamlessChainSampler", "vaeFiles",
                "仅加载 VAE", "回落整图排队"]:
        assert sym in d, sym
    api = open(os.path.join(ROOT, "web", "h3_api.js"), encoding="utf-8").read()
    assert "vaeFiles" in api
    # 最小图必须带 OUTPUT_NODE 终端，否则整包以 prompt_no_outputs 被拒
    assert "PreviewAny" in d and '["92", 3]' in d
    # 官方要求所有 widget 输入带值（有默认值也不行）：29 个一个不能少
    for name in ["宽高比", "百万像素", "宽度", "高度", "每段时长", "引导帧数",
                 "种子", "步数", "CFG", "采样器", "调度器", "自动存档", "存档目录",
                 "桥帧门控", "清晰度阈值", "回退上限", "锚定加噪", "审片模式",
                 "自动保存", "重跑起始段", "接缝重摇", "重摇阈值", "重摇上限",
                 "递减锚定", "生成模式", "自动成片", "导演台状态", "一采编码",
                 "资产包", "视频VAE", "音频VAE"]:
        assert '"%s"' % name in d, name
    # 转码失败打控制台堆栈（报告只留一行）
    assert nd.count("_tb.print_exc()") >= 2


def test_encode_audio_latent_normalizes_dims():
    """_encode_audio_latent 真跑（AST 抽取 shipped 代码）：2D/1D 波形必须补 batch。

    回归：decode_av 给 [C,T]，旧代码 [:1] 切到声道，VAE 内 tuple index out of range。
    """
    import ast
    import torch
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_encode_audio_latent")
    fake_ta = types.ModuleType("torchaudio")
    fake_func = types.ModuleType("torchaudio.functional")
    fake_func.resample = lambda w, _a, _b: w
    fake_ta.functional = fake_func
    sys.modules.setdefault("torchaudio", fake_ta)
    sys.modules.setdefault("torchaudio.functional", fake_func)
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]),
                 "nodes.py:_encode_audio_latent", "exec"), ns)
    shapes = {}

    class FakeVAE:
        audio_sample_rate = 32000

        def encode(self, x):
            shapes["in"] = tuple(x.shape)
            return torch.zeros(1, 32, 2, 4)

    enc = ns["_encode_audio_latent"]
    vae = FakeVAE()
    out = enc(vae, {"waveform": torch.zeros(2, 960), "sample_rate": 32000}, 2)
    assert shapes["in"] == (1, 960, 2), shapes
    assert tuple(out.shape) == (1, 32, 2, 2)
    out = enc(vae, {"waveform": torch.zeros(1, 2, 960), "sample_rate": 32000}, 4)
    assert shapes["in"] == (1, 960, 2), shapes
    out = enc(vae, {"waveform": torch.zeros(960), "sample_rate": 32000}, 4)
    assert shapes["in"] == (1, 960, 1), shapes


@pytest.fixture(scope="session")
def latent_tools():
    return _load_top("latent_tools", os.path.join(ROOT, "latent_tools.py"))


def test_transcode_env_note_no_crash(latent_tools):
    """转码前诊断：假 VAE/假帧不抛错（只打印）；cuda 缺席也安全。"""
    import torch

    class FakeDev:
        type = "cpu"

    class FakeVAE:
        device = FakeDev()

    latent_tools._transcode_env_note(FakeVAE(), torch.zeros(24, 64, 64, 3))
    latent_tools._transcode_env_note(None, None)
    assert "_transcode_env_note" in dir(latent_tools)
