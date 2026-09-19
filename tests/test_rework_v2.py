"""rework-director-v2 一键回归（无 ComfyUI 可跑）。

跑法（仓库根目录）：
    python -m pytest tests/test_rework_v2.py -q

覆盖：M1 manifest/revision/原子写，M2 资产Hub/总量不限/单段上限，
M2.5 提示词编译校验迁移，M3 latent切片/策略/trim参数校验，
M4 单入口/新模块存在，routes 16路由双挂载（aiohttp stub），
expander 离线 validate。trim 真编码与 Comfy 节点执行需在 Comfy 内验。
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types

import pytest
import torch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="h3v2test_")


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
    sys.modules["folder_paths"] = fp
    # stub aiohttp.web（仅够 routes.add_routes 挂载与 handler 冒烟）
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
def checkpoint():
    return _load_top("checkpoint", os.path.join(ROOT, "checkpoint.py"))


@pytest.fixture(scope="session")
def grid():
    return _load_top("grid", os.path.join(ROOT, "grid.py"))


@pytest.fixture(scope="session")
def asset_hub():
    return _load_top("asset_hub", os.path.join(ROOT, "asset_hub.py"))


@pytest.fixture(scope="session")
def prompts():
    return _load_top("prompts", os.path.join(ROOT, "prompts.py"))


@pytest.fixture(scope="session")
def latent_tools():
    return _load_top("latent_tools", os.path.join(ROOT, "latent_tools.py"))


@pytest.fixture(scope="session")
def projects(checkpoint):
    return _load_top("projects", os.path.join(ROOT, "projects.py"),
                     [("from . import checkpoint", "import checkpoint")])


@pytest.fixture(scope="session")
def library():
    return _load_top("library", os.path.join(ROOT, "library.py"))


@pytest.fixture(scope="session")
def routes(projects, library):
    return _load_top("routes", os.path.join(ROOT, "routes.py"),
                     [("from . import projects", "import projects"),
                      ("from . import library as h3lib", "import library as h3lib"),
                      ("from . import asset_hub", "import asset_hub"),
                      ("from . import prompts as _prompts", "import prompts as _prompts")])


# ---- M1 ----
def test_atomic_write_no_part_left(checkpoint):
    p = os.path.join(TMP, "h3_projects", "atomic.json")
    checkpoint.save_manifest(os.path.join(TMP, "h3_projects"), {"a": 1})
    left = [f for f in os.listdir(os.path.join(TMP, "h3_projects")) if f.endswith(".part")]
    assert left == []
    assert checkpoint.load_manifest(os.path.join(TMP, "h3_projects"))["a"] == 1


def test_revision_cas(projects):
    m = projects.create_project("t_cas")
    r0 = m["revision"]
    m2 = projects.save_prompts("t_cas", ["p1"], None, base_revision=r0)
    assert m2["revision"] == r0 + 1
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        projects.save_prompts("t_cas", ["x"], None, base_revision=r0)


def test_summary_and_legacy_migrate(projects):
    legacy = os.path.join(TMP, "h3_projects", "t_legacy")
    os.makedirs(legacy, exist_ok=True)
    json.dump({"title": "old", "prompts": ["a"], "total": 1, "done": 0, "updated_at": 1.0},
              open(os.path.join(legacy, "manifest.json"), "w", encoding="utf-8"))
    assert projects.read_project("t_legacy")["revision"] == 1
    s = projects.list_projects_summary(1, 50)
    assert s["total"] >= 2 and all("revision" in i and "seg_count" in i for i in s["items"])


# ---- M2 ----
def _mkfile(name):
    open(os.path.join(TMP, name), "wb").write(b"x")
    return name


def test_total_unlimited_seg_cap(asset_hub):
    big = [{"label": f"图{i}", "kind": "image", "file": _mkfile(f"u{i}.png")} for i in range(20)]
    assert asset_hub.validate_pack(big)["ok"] is True
    bad = asset_hub.validate_pack(big, [{"refs": [f"图{i}" for i in range(10)]}])
    assert bad["ok"] is False and bad["errors"][0]["code"] == "E_MEDIA_LIMIT"
    ok9 = asset_hub.validate_pack(big, [{"refs": [f"图{i}" for i in range(9)]}])
    assert ok9["ok"] is True
    # roles 标注透传（非法 role 丢弃）
    items, _w = asset_hub.normalize_pack([
        {"label": "H", "kind": "image", "file": "h.png", "roles": ["首帧图", "野标注"]},
        {"label": "T", "kind": "image", "file": "t.png", "roles": ["尾帧图"]},
    ])
    assert items[0]["roles"] == ["首帧图"] and items[1]["roles"] == ["尾帧图"]


def test_seg_tail_src_passthrough(projects):
    m = projects.create_project("t_tail")
    m2 = projects.save_prompts("t_tail", ["p1"], [{
        "scene_prompt": "", "character_prompt": "", "seconds": 5, "refs": [],
        "tail_src": {"asset": "角色1"}}], base_revision=m["revision"])
    assert m2["seg_fields"][0]["tail_src"] == {"asset": "角色1"}
    m3 = projects.save_prompts("t_tail", ["p1"], [{
        "scene_prompt": "", "character_prompt": "", "seconds": 5, "refs": [],
        "tail_src": {"latent": "latent/x.pt"}}], base_revision=m2["revision"])
    assert m3["seg_fields"][0]["tail_src"] == {"latent": "latent/x.pt"}
    # 非法 tail_src 丢弃
    m4 = projects.save_prompts("t_tail", ["p1"], [{
        "scene_prompt": "", "character_prompt": "", "seconds": 5, "refs": [],
        "tail_src": {"latent": "../evil.pt"}}], base_revision=m3["revision"])
    assert "tail_src" not in m4["seg_fields"][0]


def test_seg_latent_ref_src_passthrough(projects):
    m = projects.create_project("t_lrsrc")
    m2 = projects.save_prompts("t_lrsrc", ["p1"], [{
        "scene_prompt": "", "character_prompt": "", "seconds": 5, "refs": [],
        "latent_ref": {"on": True, "frames": 22, "src": {"file": "latent/bridge.pt"}}}],
        base_revision=m["revision"])
    assert m2["seg_fields"][0]["latent_ref"]["src"] == {"file": "latent/bridge.pt"}
    # 非法 src 丢弃（其余字段保留）
    m3 = projects.save_prompts("t_lrsrc", ["p1"], [{
        "scene_prompt": "", "character_prompt": "", "seconds": 5, "refs": [],
        "latent_ref": {"frames": 5, "src": {"file": "../evil.pt"}}}],
        base_revision=m2["revision"])
    assert "src" not in m3["seg_fields"][0]["latent_ref"]
    assert m3["seg_fields"][0]["latent_ref"]["frames"] == 5


def test_import_asset_roundtrip(projects):
    import folder_paths
    _mkfile("imp_src.png")
    m = projects.create_project("t_imp")
    res = projects.import_asset("t_imp", "imp_src.png", label="入1", kind="image")
    assert res["file"] == "assets/imp_src.png"
    assert res["label"] == "入1"
    root = os.path.join(folder_paths.get_output_directory(), "h3_projects", "t_imp")
    assert os.path.isfile(os.path.join(root, "assets", "imp_src.png"))
    mf = projects.read_project("t_imp")
    assert any(a["file"] == "assets/imp_src.png" for a in mf["assets"])
    # 同名再入 → _2 后缀不覆盖
    res2 = projects.import_asset("t_imp", "imp_src.png", label="入1", kind="image")
    assert res2["file"] == "assets/imp_src_2.png"
    assert res2["label"] == "入12"
    # 非法路径拒绝
    with pytest.raises(ValueError):
        projects.import_asset("t_imp", "../evil.png")


def test_unknown_and_missing(asset_hub):
    a = [{"label": "A", "kind": "image", "file": _mkfile("a1.png")}]
    r = asset_hub.validate_pack(a, [{"refs": ["不存在"]}])
    assert r["ok"] is False and any(e["code"] == "E_REF_UNKNOWN" for e in r["errors"])
    r2 = asset_hub.validate_pack([{"label": "M", "kind": "image", "file": "nope.png"}])
    assert r2["ok"] is False and r2["errors"][0]["code"] == "E_FILE_MISSING"


def test_save_assets_revision(projects):
    m = projects.create_project("t_assets")
    items = [{"label": f"图{i}", "kind": "image", "file": f"u{i}.png"} for i in range(20)]
    m2 = projects.save_assets("t_assets", items, base_revision=m["revision"])
    assert len(m2["assets"]) == 20
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        projects.save_assets("t_assets", items, base_revision=1)


def test_nodes_wiring():
    src = open(os.path.join(ROOT, "nodes.py"), encoding="utf-8").read()
    assert "资产包" in src and "总量不限" in src
    assert "引用了未知素材标签" in src and "加载失败" in src
    # 总量不限：库再大也不逼每段显式勾选（缺省=文本[[标签]]驱动），只卡单段上限
    assert "该段图片没选" not in src and "素材库共" not in src
    assert "总量不限、按段按需" in src
    assert "超过官方单段上限" in src
    # 段锚「外源桥」必须透传：旧写法（_ent["src"] = {"file": ...} 从 latent_ref.src 桥进
    # seg_latent_ref）已随「手动锚定」重构删掉，现在由 anchors 建条目带 src.kind/ref、
    # nodes._inject_guide 认 src 非 prev_tail 时改取外源 —— 钉住这条新链路，
    # 否则「设了源却不生效、静默回落上段尾」那类最坏 bug 会悄悄回潮。
    anc = open(os.path.join(ROOT, "anchors.py"), encoding="utf-8").read()
    assert '"kind": "prev_tail"' in anc and '"kind": "library"' in anc
    assert '_a["src"]["kind"] != "prev_tail"' in src
    assert "_resolve_anchor_latent" in src


# ---- M2.5 ----
def test_prompts_t2va_ref(prompts):
    p = prompts.default_prompt()
    p.update({"medium_style": "Live-action", "environment": "a bakery",
              "characters": "a baker", "soundscape": "Shutters scrape.",
              "non_diegetic_music": ""})
    p["shots"] = [{"index": 1, "start_seconds": None, "description": "he opens shutters",
                   "camera_move": "Push In", "camera_amplitude": "small", "camera_speed": "slow",
                   "dialogues": [], "screen_texts": [], "diegetic_music": "", "ref_usage": []}]
    c = prompts.compile_segment(p, seconds=5.0)
    assert c["mode"] == "T2VA" and prompts.validate_compiled(c)["ok"] is True
    assert "non_diegetic_music: N/A" in c["prompt_text"]
    p2 = prompts.default_prompt()
    p2["references"] = [{"label": "<Picture 1>", "note": "first frame"}]
    p2["subjects"] = [{"definition": "the baker"}]
    p2["retention"] = [{"label": "<Picture 1>", "marker": "fully_preserved", "shots": [], "note": ""},
                       {"label": "<Subject 1>", "marker": "fully_preserved", "shots": [], "note": ""}]
    c2 = prompts.compile_segment(p2, seconds=6.0)
    assert c2["mode"] == "Ref2VA" and prompts.validate_compiled(c2)["ok"] is True
    old = {"scene_prompt": "教室", "character_prompt": "少女", "prompt": "她写字", "refs": ["角色1"]}
    assert prompts.migrate_legacy_seg(old)["environment"] == "教室"


def test_prompts_passthrough(projects):
    m = projects.create_project("t_pv")
    m2 = projects.save_prompts("t_pv", ["p1"], [{
        "scene_prompt": "s", "character_prompt": "c", "seconds": 5,
        "refs": [], "prompt_v2": {"environment": "E", "shots": [{"description": "d"}]},
        "latent_save": {"mode": "range", "start_f": 0, "end_f": 48, "tail_f": 0}}],
        base_revision=m["revision"])
    sf = m2["seg_fields"][0]
    assert sf["prompt_v2"]["environment"] == "E"
    assert sf["latent_save"] == {"mode": "range", "start_f": 0, "end_f": 48, "tail_f": 0,
                                 "split_av": False, "save_seg": True, "save_all": True}


# ---- M3 ----
def test_latent_slice_roundtrip(projects, grid):
    m = projects.create_project("t_lat")
    root = os.path.join(TMP, "h3_projects", "t_lat")
    torch.save({"video": torch.randn(1, 4, 7, 8, 8), "audio": torch.randn(1, 32, 2, 50)},
               os.path.join(root, "seg_000.pt"))
    m2 = projects.slice_latent("t_lat", {"seg": 0}, 0, 5, "head.pt", base_revision=m["revision"])
    assert m2["latents"][-1]["file"] == "latent/head.pt"
    pay = torch.load(os.path.join(root, "latent", "head.pt"), map_location="cpu", weights_only=True)
    assert pay["video"].shape[2] == 2  # 5 像素帧 -> 2 tokens
    with pytest.raises(ValueError):
        projects.slice_latent("t_lat", {"seg": 0}, 5, 5, "bad.pt")
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        projects.slice_latent("t_lat", {"seg": 0}, 0, 5, "c.pt", base_revision=1)
    m3 = projects.delete_latent("t_lat", "latent/head.pt")
    assert all(x["file"] != "latent/head.pt" for x in m3["latents"])


def test_trim_validation(projects):
    import folder_paths
    m = projects.create_project("t_trim")
    with pytest.raises(ValueError, match="源文件不存在"):
        projects.trim_asset("t_trim", "seg_000.mp4", 0, 2)
    open(os.path.join(folder_paths.get_output_directory(), "h3_projects", "t_trim",
                      "seg_000.mp4"), "wb").write(b"\x00")
    with pytest.raises(ValueError, match="裁剪区间为空"):
        projects.trim_asset("t_trim", "seg_000.mp4", 2, 2)


def test_latent_tools_pure(latent_tools):
    assert latent_tools.clamp_window(2, 10, 124) == (2, 10)
    with pytest.raises(ValueError):
        latent_tools.clamp_window(5, 5, 10)
    assert latent_tools.clean_latent_name("a.pt") == "a.pt"
    assert latent_tools.clean_latent_name("../x.pt") == "x.pt"


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


def test_routes_mount_and_handlers(routes, projects):
    r = _Router()
    routes.add_routes(r)
    for p in ["/h3chain/create_project", "/h3chain/compile", "/h3chain/assets",
              "/h3chain/asset_check", "/h3chain/latent_slice", "/h3chain/latent_delete",
              "/h3chain/trim", "/h3chain/probe", "/h3chain/move_media", "/h3chain/split_av",
              "/h3chain/import_asset", "/h3chain/merge"]:
        assert ("POST", p) in r.paths and ("POST", "/api" + p) in r.paths
    assert ("GET", "/h3chain/ping") in r.paths
    assert ("GET", "/h3chain/busy") in r.paths
    assert ("GET", "/api/h3chain/busy") in r.paths
    # handler 冒烟：ping + create + assets + asset_check + compile
    h = {}
    r2 = types.SimpleNamespace(
        add_get=lambda p, fn: h.setdefault(("GET", p), fn),
        add_post=lambda p, fn: h.setdefault(("POST", p), fn))
    routes.add_routes(r2)
    assert asyncio.run(h[("GET", "/h3chain/ping")](_Req())).data["ok"] is True
    res = asyncio.run(h[("POST", "/h3chain/create_project")](_Req({"dir": "t_route"})))
    assert res.data["ok"] is True
    res = asyncio.run(h[("POST", "/h3chain/assets")](
        _Req({"dir": "t_route", "assets": [{"label": "A", "kind": "image", "file": "a1.png"}]})))
    assert res.data["ok"] is True
    res = asyncio.run(h[("POST", "/h3chain/asset_check")](
        _Req({"assets": [{"label": "A", "kind": "image", "file": "a1.png"}]})))
    assert res.status in (200, 422)
    res = asyncio.run(h[("POST", "/h3chain/compile")](
        _Req({"prompt": {"environment": "room", "shots": [{"description": "a man walks"}]},
              "seconds": 5.0})))
    assert res.status in (200, 422) and "compiled" in res.data


# ---- M4 / expander ----
def test_web_single_entry():
    web = os.listdir(os.path.join(ROOT, "web"))
    assert "h3_director.js" in web and "entry.js" not in web
    for f in ["h3_api.js", "h3_prompts.js", "h3_assets.js", "h3_latent.js"]:
        assert f in web
    d = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    assert "h3chain-director" in d and "__H3_V2_MODULES__" in d
    for f in ["h3_api.js", "h3_prompts.js", "h3_assets.js", "h3_latent.js"]:
        t = open(os.path.join(ROOT, "web", f), encoding="utf-8").read()
        assert "registerExtension" not in t and t.strip()


def test_v2_section_wired():
    d = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    assert "function renderV2Section(sec, data)" in d
    assert "renderV2Section(sec, data);" in d
    # 端点符号按**名字全域查**，不要按「名字含 latent 就只去 h3_latent.js」猜
    # —— latent_slice 一直在 h3_api.js，猜错文件就假红（掩盖真回归）。
    web_all = ""
    for f in sorted(os.listdir(os.path.join(ROOT, "web"))):
        if f.endswith(".js"):
            web_all += open(os.path.join(ROOT, "web", f), encoding="utf-8").read()
    for ep in ["compilePreview", "saveAssets", "assetCheck", "latent_slice", "trim"]:
        assert ep in d or ep.replace("_", "/") in d or ep in web_all


def test_transcode_jobs_normalized():
    """内联转码 UI 已按设计下线（见 test_frontend_p3::test_director_no_inline_transcode）。

    保留此用例是为了钉死「不再回潮」：getDs 不得再归一化 transcode_jobs，
    后台任务区/转码子面板也不得再出现。后端 latent_tools/nodes 仍支持，
    但前端只做引用与打标，不再内联跑转码。
    """
    d = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    for gone in ["transcode_jobs", "renderServerJobs", "renderTranscodeJobs", "h3d-jstat"]:
        assert gone not in d, gone
    # 转码分支口径仍在后端生效（latent_tools/routes/nodes）
    lt = open(os.path.join(ROOT, "latent_tools.py"), encoding="utf-8").read()
    assert '分支="图像+音频"' in lt


def test_expander_offline():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "h3_prompt_expander", "validate.py"),
                        os.path.join(ROOT, "tools", "h3_prompt_expander", "examples", "envelope_ok.json")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout)["ok"] is True


# ---- 5.1 prompt_v2 分组表单 ----
def test_v2_group_form_wired():
    d = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    p = open(os.path.join(ROOT, "web", "h3_prompts.js"), encoding="utf-8").read()
    # helper 存在且挂载
    for sym in ["ensurePromptV2", "hasPromptV2", "detectMode", "CAMERA_MOVES",
                "CAMERA_AMPS", "CAMERA_SPEEDS", "RETENTION_MARKERS"]:
        assert sym in p, sym
    # 数据链路：默认/还原/归一/落盘/签名全部透存 prompt_v2（防洗掉）
    assert "prompt_v2: null" in d
    assert "prompt_v2: (s.prompt_v2" in d or "prompt_v2: (raw.prompt_v2" in d
    assert "prompt_v2: (s.prompt_v2" in d
    assert "prompt_v2: (s.prompt_v2" in d and "latent_save" in d
    assert "JSON.stringify(s.prompt_v2)" in d
    assert "function renderPromptV2Panel(body, node, data, segIdx)" in d or \
        "function renderPromptV2Panel(" in d
    # 结构化**只**在弹窗里渲染（页内不再预渲染一份 —— 同一份 prompt_v2 渲染两处，
    # 改哪边都容易让人以为另一边才是真相）
    assert "renderPromptV2Panel(bodyBox, node, data, idx)" in d
    assert "paneV2" not in d, "页内 paneV2 应已随「结构化」迁进弹窗一起删掉"
    assert "function setPromptV2Field(node, idx, mutate" in d
    assert "function debouncePromptV2Write" in d
    assert "function getSegPromptV2" in d
    # 各组标题**必须写官方字段名**（用户明说"一定要符合 h3 官方提示词 skill 的格式"）：
    # 六段式 ①subject_definitions ②summary ③retention_analysis ④detailed_description
    # ⑤overall_soundscape ⑥non_diegetic_music；三段式 ④=integrated_multimodal_description。
    # 画面设定已并进 ④ 一组（它们编译出去是同一个官方字段，分成两组看着像官方有两个）。
    # 源码覆盖已迁出到主框结果区。
    for g in ["subject_definitions", "summary", "retention_analysis",
              "detailed_description", "integrated_multimodal_description",
              "overall_soundscape", "non_diegetic_music"]:
        assert g in d, f"结构化组标题缺官方字段名 {g}"
        assert g in d, g
    assert "高级 · 源码覆盖" not in d, "源码覆盖应已移除"
    assert "源码覆盖 · 直接改写最终结果" not in d, "源码覆盖应已彻底移除"
    assert "清除覆盖" in d, "残留 override_text 应可一键清除"
    # 模式改为按本段数据自动判定（不再给手动下拉），符号保留供兼容；编译仍透传 mode
    for sym in ["V2_MODES", "effV2Mode", "defaultV2Mode", "setV2Mode",
                "v2InstrPreview", "v2mode", "assignV2FromText"]:
        assert sym in d or sym in p, sym
    # 具象化精简：画面合并为 visual 单框；每镜低频项收进「更多」
    for sym in ["pv0.visual", '"visual"', "更多 · 换镜时间"]:
        assert sym in d, sym
    # 三框合一：段卡只剩**一个**提示词框（意图 / 剧本 已从 UI 撤下）
    for sym in ["提示词（最终进模型）", "✨ AI扩写+优化", "✨ 提示词优化", "引用素材"]:
        assert sym in d, sym
    for old in ["① 中文意图（不进模型 · 可用 @素材）", "② 剧本（扩写产物 · 可手工改）",
                "③ 结果（最终进模型）", "① 意图", "② 剧本", "③ 结果"]:
        assert old not in d, f"旧三框文案应已下线：{old}"
    assert "gMore.append(gMoreBody)" in d, "镜头「更多」内容没挂进 details"
    # 复位后开合状态要记下来（否则加对白/重建会被 details 默认收起打断）
    assert "const _v2Open = new Map();" in d, "缺 details 开合记忆"
    assert 'function v2Details(key, cls, summaryHtml, defOpen)' in d, "缺 v2Details 工厂"
    assert '_v2Open.set(key, g.open)' in d, "toggle 未记录开合"
    # 组 key：官方六段的前三段拆成 sub/sum/ret 三组（不再塞在一个 ref 里），
    # 素材调度单独成组（它不是官方字段）；「画面」组（pic）已并入整体描述组。
    for k in ["v2${segIdx}", "shot${segIdx}", "snd${segIdx}", "sub${segIdx}",
              "sum${segIdx}", "ret${segIdx}", "sched${segIdx}",
              "more${segIdx}_${si}"]:
        assert k in d, k
    assert "pic${segIdx}" not in d, "画面组应已并入整体描述组"
    assert "ref${segIdx}" not in d, "旧的「参考」大组应已拆成官方前三段"
    # 挂载顺序 = 官方字段顺序（在函数末尾统一 append，不是想到哪挂到哪）
    assert "vbody.append(gSub, gSum, gRet, gShot, gSnd, gSched);" in d
    # 重建只在会丢东西时才问（否则 confirm 抢焦点导致输入框卡住）
    assert "hasNote" in d and "按当前素材调度重建引用列表" in d, "重建应改为条件确认"
    # 模式必须按本段数据自动判定，不能写死
    for m in ['"T2VA"', '"I2VA"', '"L2VA"', '"FL2VA"']:
        assert "return " + m + ";" in d, "defaultV2Mode 缺分支 " + m
    assert "未检测到首帧/尾帧图" in d, "无锚时应给说明而非报错"
    assert "需要首帧图＋尾帧图" not in d, "旧的强制文案应移除"
    # 官方引用**由素材调度派生**（不再手填标签）：参考组渲染时现算 want，
    # pv.references 里只存用户写的说明（note），改勾选不用同步、也不会打架。
    # 早先这里断言过一个 syncV2RefsFromSchedule() —— 那个函数会把已写的 note
    # 一起抹掉，最后没做（改为「↻ 按素材调度重建」显式按钮 + 有说明时确认），
    # 断言因此改钉现在这套口径。
    assert "function v2RefsFromSchedule(" in d
    assert "const want = v2RefsFromSchedule(data.ds, segIdx);" in d
    assert "按素材调度重建" in d and "已写的说明会清空" in d, "缺显式重建入口/提示"
    for t in ["<Picture ", "<Video ", "<Audio "]:
        assert t in d, t
    # 中文显示 ⇄ 英文存储：运镜/说话人/语言/任务类型映射齐全
    for sym in ["V2_CAM_ZH", "V2_AMP_ZH", "V2_SPD_ZH", "V2_TASK_ZH", "V2_MARKER_ZH",
                "zhSpeaker", "enSpeaker", "zhLang", "enLang", "zhTasks", "enTasks", "mkMapSel"]:
        assert sym in d, sym
    # 校验入口：编译预览+模式徽
    assert "编译预览+校验" in d and "detectMode" in d
    # 5.3 最小接线：AI扩写复制命令行
    assert "AI扩写" in d and "h3_prompt_expander" in d
    # 焦点守卫：v2 输入聚焦不重建（防丢焦）
    assert "input:focus, select:focus" in d


def test_prompts_full_groups(prompts):
    pv = prompts.default_prompt()
    pv.update({"intent_zh": "雨夜独行", "medium_style": "Live-action",
               "composition": "medium shot", "environment": "rainy alley",
               "lighting": "neon", "characters": "a woman", "props": "umbrella",
               "soundscape": "Rain falls.", "non_diegetic_music": "N/A"})
    pv["shots"] = [
        {"index": 1, "start_seconds": None, "description": "she opens umbrella",
         "camera_move": "Push In", "camera_amplitude": "small", "camera_speed": "slow",
         "dialogues": [{"speaker": "S1", "language": "Chinese", "text": "走吧",
                        "delivery": "", "voiceover": False}],
         "screen_texts": ["OPEN 24H"], "diegetic_music": "", "ref_usage": []},
        {"index": 2, "start_seconds": 2.5, "description": "she walks away",
         "camera_move": "Tracking Shot", "camera_amplitude": "", "camera_speed": "",
         "dialogues": [], "screen_texts": [], "diegetic_music": "", "ref_usage": []},
    ]
    c = prompts.compile_segment(pv, seconds=5.0)
    assert c["mode"] == "T2VA"
    assert prompts.validate_compiled(c)["ok"] is True
    # 非法运镜词被清洗为空，不炸链
    pv["shots"][0]["camera_move"] = "乱写"
    assert prompts.clean_prompt(pv)["shots"][0]["camera_move"] == ""


# ---- 切换式段卡 + 自研优化后端 ----
def test_optimizer_backend():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "h3optimizer", os.path.join(ROOT, "optimizer.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    # 规则文件齐全
    files = m.load_rule_files()
    assert len(files) >= 3 and any("custom" in k for k in files)
    # 规则选择
    assert m.pick_rule_text({"rule_file": "none"}, files) is None
    assert m.pick_rule_text({"rule_file": "auto", "output_language": "中文"}, files)
    # 配置归一+脱敏
    cfg = m.normalize_config({"provider": "openai", "api_key": "sk-x"})
    pub = m.public_config(cfg)
    assert pub["has_api_key"] is True and pub["api_key"] == ""
    # 空提示词/无key报清晰错误（不抛 Traceback 穿透）
    with pytest.raises(ValueError, match="为空"):
        m.optimize_once({"mode": "api", "provider": "openai", "api_key": "sk-x"}, {"prompt": "  "})
    with pytest.raises(ValueError, match="API Key"):
        m.optimize_once({"mode": "api", "provider": "openai", "api_key": ""}, {"prompt": "hi"})
    # 系统提示词双模式
    assert "subject_definitions" in m.build_system_prompt("Ref2VA", 5.0, [], "中文")
    assert "integrated_multimodal_description" in m.build_system_prompt("T2VA", 5.0, [], "中文")


def test_optimizer_routes_mount(routes):
    r = _Router()
    routes.add_routes(r)
    for p in ["/h3chain/prompt-rules", "/h3chain/optimizer-config", "/h3chain/optimize",
              # 流式那三条（进度条用）：必须**两份都挂**，新版前端 api.fetchApi()
              # 会给所有不以 /api 开头的路径强制加前缀，只挂根路径会 404。
              "/h3chain/optimize_stream", "/h3chain/expand_optimize_stream",
              "/h3chain/optimize_multi_stream"]:
        assert ("GET", p) in r.paths or ("POST", p) in r.paths
        assert ("GET", "/api" + p) in r.paths or ("POST", "/api" + p) in r.paths


def test_segment_tabs_and_optimizer_ui():
    d = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    a = open(os.path.join(ROOT, "web", "h3_api.js"), encoding="utf-8").read()
    # 切换式两页（「具象化/结构化」那一页已迁成工具条弹窗，不再占一个 pane）
    assert "h3d-tabs" in d and "paneMain" in d and "paneSet" in d
    assert "paneV2" not in d
    assert "_segTab" in d
    # 旧四框 UI 已删（后端仍兼容旧键，仅前端不编辑）
    assert "场景提示词" not in d and "角色提示词" not in d
    # 段片瘦身：无大 thumb 网格，全屏入口保留
    assert "segMediaInfo" in d and "openSegViewer" in d and "h3d-viewer" in d
    assert "▶ 预览" in d
    assert "grid-template-columns:minmax(0,1fr)" in d
    # 结构化已收进工具条弹窗（不再是独立 tab、也不再是页内切换）
    assert "⇄ 结构化提示词" in d and "function openStructuredModal(" in d
    assert "_segStructView" not in d, "页内切换的视图记忆应随弹窗化一起删掉"
    assert "从具象化同步" not in d and "同步到具象化" not in d, "旧的双向同步按钮应已下线"
    assert "function applyAiToV2(" not in d, "旧 applyAiToV2（整段塞进 shots[0]）应已删除"
    assert "function applyH3TextToSeg(" in d and "function splitH3Sections(" in d
    # AI优化条（自研后端）
    for sym in ["paintOptbar", "runOptForSegment", "openOptSettings", "opt_hist",
                "optimizer-config", "/h3chain/optimize"]:
        assert sym in d or sym in a, sym
    assert "optimize:" in a and "getOptimizerConfig" in a and "getPromptRules" in a
    # 进度条 + 思考强度（2026-09-19）：两条都必须真的接上，不能只留后端接口。
    # 前端少了 optimizeStream 就退回整包（没有进度）；少了 rebuildThinking
    # 「思考强度」就退化成只能开/关（用户报的"强度不能选"会复现）。
    assert "optimizeStream" in a and "/h3chain/optimize_stream" in a
    assert "expandOptimizeStream" in a and "/h3chain/expand_optimize_stream" in a
    assert "optimizeMultiStream" in a and "/h3chain/optimize_multi_stream" in a
    # 多段那条也得真的接上流式（总提示词框的「AI 分段优化」）：
    # 少了 stream 名就退回整包，N 段串行跑起来就是"点完盯着不动好几分钟"。
    assert 'stream: "optimizeMultiStream"' in d
    assert 'fallback: "optimizeMulti"' in d
    for sym in ["optProgressStart", "optCallStream", "rebuildThinking",
                "optModelCaps", "optCapsLoad", "思考强度", "h3d-opt-prog"]:
        assert sym in d, sym
