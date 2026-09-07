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
def routes(projects):
    return _load_top("routes", os.path.join(ROOT, "routes.py"),
                     [("from . import projects", "import projects"),
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
    assert "请在该段卡片勾选本段要用的素材" in src


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
    assert sf["latent_save"] == {"mode": "range", "start_f": 0, "end_f": 48, "tail_f": 0}


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
              "/h3chain/trim", "/h3chain/merge"]:
        assert ("POST", p) in r.paths and ("POST", "/api" + p) in r.paths
    assert ("GET", "/h3chain/ping") in r.paths
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
    for ep in ["compilePreview", "saveAssets", "assetCheck", "latent_slice", "trim"]:
        assert ep in d or ep.replace("_", "/") in d or ep in open(
            os.path.join(ROOT, "web", "h3_latent.js" if "latent" in ep or ep == "trim" else "h3_api.js"),
            encoding="utf-8").read()


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
    assert "renderPromptV2Panel(paneV2, node, data, it.idx)" in d or \
        "renderPromptV2Panel(body, node, data, it.idx)" in d
    assert "function setPromptV2Field(node, idx, mutate" in d
    assert "function debouncePromptV2Write" in d
    assert "function getSegPromptV2" in d
    # 五组中文标题齐全（官方字段名口径）
    for g in ["画面 · 风格/构图/环境/光照/角色/道具", "镜头 ×", "声音 · overall_soundscape",
              "参考 · subject_definitions", "高级 · 源码覆盖"]:
        assert g in d, g
    # 五模式：模式选择 + 手动覆写 + 对齐指令预览 + 编译透传 mode
    for sym in ["V2_MODES", "effV2Mode", "defaultV2Mode", "setV2Mode",
                "v2InstrPreview", "v2mode", "assignV2FromText"]:
        assert sym in d or sym in p, sym
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
    for p in ["/h3chain/prompt-rules", "/h3chain/optimizer-config", "/h3chain/optimize"]:
        assert ("GET", p) in r.paths or ("POST", p) in r.paths
        assert ("GET", "/api" + p) in r.paths or ("POST", "/api" + p) in r.paths


def test_segment_tabs_and_optimizer_ui():
    d = open(os.path.join(ROOT, "web", "h3_director.js"), encoding="utf-8").read()
    a = open(os.path.join(ROOT, "web", "h3_api.js"), encoding="utf-8").read()
    # 切换式三页
    assert "h3d-tabs" in d and "paneMain" in d and "paneV2" in d and "paneSet" in d
    assert "_segTab" in d
    # 旧四框 UI 已删（后端仍兼容旧键，仅前端不编辑）
    assert "场景提示词" not in d and "角色提示词" not in d
    # 段片瘦身：无大 thumb 网格，全屏入口保留
    assert "segMediaInfo" in d and "openSegViewer" in d and "h3d-viewer" in d
    assert "▶ 预览" in d
    assert "grid-template-columns:minmax(0,1fr)" in d
    # 双写同步
    assert "同步到主框" in d and "从v2同步" in d
    # AI优化条（自研后端）
    for sym in ["paintOptbar", "runOptForSegment", "openOptSettings", "opt_hist",
                "optimizer-config", "/h3chain/optimize"]:
        assert sym in d or sym in a, sym
    assert "optimize:" in a and "getOptimizerConfig" in a and "getPromptRules" in a
