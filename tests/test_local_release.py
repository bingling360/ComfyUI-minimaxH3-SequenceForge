"""本地优化通道：用完即卸 + 思维链剥离（2026-09-20）。

两处「能跑但结果是错的」型问题，肉眼看不出，只能靠断言钉住：

1. **句柄只进不出**：`_GGUF_CACHE` / `_TF_CACHE` 是模块级字典，写进去就再没人删 →
   27B 权重带着 KV 常驻显存，而 ComfyUI 的 `unload_all_models()` 管不到 llama.cpp
   （ggml 自己的显存池）→ 后续 H3 采样的空闲显存凭空少一大块。
2. **思维链进正文**：云通道的思考在 `reasoning_content` 里，本地通道拿到的是
   「思考 + 正文」一整块 `content`，原样返回 → 编辑器里出现整段思考
   （实测 3100 字思考 / 1100 字正文）。

本文件用桩 `llama_cpp` 把 GGUF 路径整条跑通 —— 本机没装 llama-cpp-python，
也不该为了跑测试去装它。
"""
import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


optimizer = _load("h3optimizer_local", "optimizer.py")


class _FakeLlama:
    """桩句柄：记下构造参数与调用，close() 必须被调到（显存归还的唯一通路）。"""

    instances: list = []
    reply = ""
    boom = False
    # 模拟老轮子：构造时看到 flash_attn / type_k 这类新参数直接 TypeError
    reject_new_kwargs = False

    def __init__(self, **kw):
        if _FakeLlama.reject_new_kwargs and (kw.get("flash_attn") or "type_k" in kw):
            raise TypeError("unexpected keyword argument 'flash_attn'")
        self.kw = kw
        self.closed = False
        self.messages = None
        _FakeLlama.instances.append(self)

    def create_chat_completion(self, messages, max_tokens, temperature):
        self.messages = messages
        if _FakeLlama.boom:
            raise RuntimeError("模拟生成失败")
        return {"choices": [{"message": {"content": _FakeLlama.reply}}]}

    def close(self):
        self.closed = True


@pytest.fixture
def fake_llama(monkeypatch):
    pkg = types.ModuleType("llama_cpp")
    pkg.Llama = _FakeLlama
    pkg.GGML_TYPE_Q8_0 = 8
    fmt = types.ModuleType("llama_cpp.llama_chat_format")
    fmt.Llava15ChatHandler = lambda **kw: {"mmproj": kw}
    pkg.llama_chat_format = fmt
    monkeypatch.setitem(sys.modules, "llama_cpp", pkg)
    monkeypatch.setitem(sys.modules, "llama_cpp.llama_chat_format", fmt)
    _FakeLlama.instances = []
    _FakeLlama.reply = ""
    _FakeLlama.boom = False
    _FakeLlama.reject_new_kwargs = False
    return pkg


def _cfg(**over):
    d = {"mode": "local", "local_model": "qwen3.5-27b-Q4_K_M.gguf",
         "local_device": "cuda", "read_media": False, "max_tokens": 16384,
         "thinking": "disabled"}
    d.update(over)
    return optimizer.normalize_config(d)


def _user_text(inst):
    """取 user 消息里的正文（content 是 [{type:text,text:…}, …]）。"""
    return inst.messages[1]["content"][0]["text"]


def test_module_has_no_handle_cache():
    """句柄缓存整体删掉：模式 A（用完即卸）下它只会是"写一条、紧接着删掉"的死代码。"""
    assert not hasattr(optimizer, "_GGUF_CACHE")
    assert not hasattr(optimizer, "_TF_CACHE")


@pytest.mark.parametrize("raw,want", [
    ("<think>想很久</think>时长：5.2秒\n镜头一：特写", "时长：5.2秒\n镜头一：特写"),
    ("思考正文没开标签</think>时长：5秒", "时长：5秒"),
    ("<think>思考被截断、没有闭合标签", ""),
    ("时长：5.2秒（压根没思考）", "时长：5.2秒（压根没思考）"),
    ("<think>a</think>正文A<think>b</think>", "正文A"),
])
def test_strip_think_forms(raw, want):
    assert optimizer._strip_think(raw) == want


def test_local_gguf_releases_handle_and_strips_think(fake_llama):
    _FakeLlama.reply = "<think>想很久</think>时长：5.2秒\n镜头一：特写"
    out = optimizer._local_generate(_cfg(), "SYS", "USER", [])
    assert out == "时长：5.2秒\n镜头一：特写"
    inst = _FakeLlama.instances[0]
    assert inst.closed is True, "生成结束必须关句柄，否则显存不归还"


def test_local_gguf_closes_handle_even_on_failure(fake_llama):
    _FakeLlama.boom = True
    with pytest.raises(RuntimeError, match="模拟生成失败"):
        optimizer._local_generate(_cfg(), "SYS", "USER", [])
    assert _FakeLlama.instances[0].closed is True


def test_local_gguf_rejects_think_only_reply(fake_llama):
    """思考吃光预算、正文一个字没出来 → 必须报错，不能把半段思考当提示词填进编辑器。"""
    _FakeLlama.reply = "<think>想啊想啊想啊"
    with pytest.raises(RuntimeError, match="思维链"):
        optimizer._local_generate(_cfg(), "SYS", "USER", [])
    assert _FakeLlama.instances[0].closed is True


def test_local_thinking_disabled_injects_no_think(fake_llama):
    """本地没有 API 那种 thinking 字段，关思考只能靠 Qwen 系的 /no_think 软开关。"""
    _FakeLlama.reply = "正文"
    optimizer._local_generate(_cfg(thinking="disabled"), "SYS", "USER", [])
    assert _user_text(_FakeLlama.instances[-1]).endswith("/no_think")

    optimizer._local_generate(_cfg(thinking="enabled"), "SYS", "USER", [])
    assert "/no_think" not in _user_text(_FakeLlama.instances[-1])


def test_local_n_ctx_covers_budget_and_device_auto_uses_gpu(fake_llama):
    _FakeLlama.reply = "正文"
    optimizer._local_generate(_cfg(max_tokens=16384), "SYS", "USER", [])
    kw = _FakeLlama.instances[-1].kw
    assert kw["n_ctx"] >= 16384 + 8192, "窗口必须装得下提示词 + 生成"
    assert kw["n_ctx"] % 512 == 0
    assert kw["n_gpu_layers"] == -1

    # auto 也要上 GPU：llama-cpp-python 不设 n_gpu_layers 就是 0 层 = 纯 CPU
    optimizer._local_generate(_cfg(local_device="auto"), "SYS", "USER", [])
    assert _FakeLlama.instances[-1].kw["n_gpu_layers"] == -1

    optimizer._local_generate(_cfg(local_device="cpu"), "SYS", "USER", [])
    assert "n_gpu_layers" not in _FakeLlama.instances[-1].kw


def test_local_gguf_speed_kwargs_present(fake_llama):
    """通用加速参数必须在位：FA 开、KV 缓存量化 q8_0、prefill 大批。
    这些全是 llama-cpp-python 原生参数 —— 24G 卡上 27B 能不能整个塞进显存
    （塞不下会静默掉层到 CPU，速度掉一个数量级）就看 type_k/type_v。"""
    _FakeLlama.reply = "正文"
    optimizer._local_generate(_cfg(), "SYS", "USER", [])
    kw = _FakeLlama.instances[-1].kw
    assert kw["flash_attn"] is True
    assert kw["n_batch"] == 2048
    # 物理微批必须单独压住：n_batch 只管一次喂多少 token，n_ubatch 才决定
    # 计算缓冲区按多大批的最坏情况分配（2048 会白吃掉 1~2G 显存）
    assert kw["n_ubatch"] == 512
    assert kw["type_k"] == 8
    assert kw["type_v"] == 8
    # CPU 模式不铺层，但其余加速项照给
    optimizer._local_generate(_cfg(local_device="cpu"), "SYS", "USER", [])
    kw = _FakeLlama.instances[-1].kw
    assert "n_gpu_layers" not in kw and kw["flash_attn"] is True


def test_local_gguf_falls_back_when_new_kwargs_rejected(fake_llama):
    """老轮子不认识新参数（构造抛 TypeError）→ 剥掉加速项重试一次，
    生成照常完成、句柄照常关闭 —— 加速项绝不能把整次生成弄挂。"""
    _FakeLlama.reply = "正文"
    _FakeLlama.reject_new_kwargs = True
    try:
        out = optimizer._local_generate(_cfg(), "SYS", "USER", [])
    finally:
        _FakeLlama.reject_new_kwargs = False
    assert out == "正文"
    inst = _FakeLlama.instances[-1]
    assert "flash_attn" not in inst.kw
    assert "type_k" not in inst.kw and "type_v" not in inst.kw
    assert "n_ubatch" not in inst.kw
    # 老轮子的 n_batch 同时是物理批，必须跟着收回来，否则按 2048 撑缓冲区
    assert inst.kw["n_batch"] == 512
    assert inst.closed is True


def test_local_mmproj_missing_file_clear_error(fake_llama):
    """mmproj 选了但文件不在 → 一句话报清楚去哪补，别让用户面对
    llama.cpp 的 CLIP 加载栈。"""
    cfg = _cfg(local_mmproj="no-such-mmproj.gguf")
    with pytest.raises(ValueError, match="视觉投影文件不存在"):
        optimizer._local_generate(cfg, "SYS", "USER", [])
    assert _FakeLlama.instances == [], "文件校验失败时连模型都不该开始加载"


class _FakeTorch:
    """torch 桩：只提供 _pick_hf_dtype 用到的东西（本机没有 CUDA torch，别真装）。"""

    float16 = "fp16"
    bfloat16 = "bf16"
    float32 = "fp32"

    def __init__(self, cuda=True, bf16=True):
        class _Cuda:
            @staticmethod
            def is_available():
                return cuda

            @staticmethod
            def is_bf16_supported():
                return bf16
        self.cuda = _Cuda()

    @staticmethod
    def device(name):
        return ("device", name)


@pytest.mark.parametrize("cfg_dtype,cuda,bf16,device,want", [
    ("bfloat16", True, True, "cuda", "bf16"),     # 模型自带 bf16 + 卡支持 → bf16
    ("bfloat16", True, False, "cuda", "fp16"),    # 老卡无硬件 bf16 → 回落 fp16
    ("float16", True, True, "cuda", "fp16"),      # 模型自带 fp16 → 照它来，不强改
    (None, True, True, "cuda", "bf16"),           # config 没标 → 默认 bf16
    (None, True, False, "cuda", "fp16"),          # 没标 + 老卡 → fp16
    ("float32", True, True, "cuda", "bf16"),      # 标 fp32 也不能真用 fp32（显存翻倍）
    ("bfloat16", True, True, "cpu", "fp32"),      # CPU 保持 fp32（bf16 没收益）
])
def test_pick_hf_dtype_follows_config(cfg_dtype, cuda, bf16, device, want):
    """精度**不能硬写死**：照模型 config 走，config 没标才默认 bf16，老卡回落 fp16。"""
    got = optimizer._pick_hf_dtype(_FakeTorch(cuda, bf16), cfg_dtype, device)
    assert got == want


def test_pick_hf_dtype_survives_missing_bf16_probe():
    """老 torch 没有 is_bf16_supported() 也要能跑（探测不到就按"支持"处理，
    别因为探测函数不存在就把新卡误判成老卡、白回落 fp16）。"""

    class _CudaNoProbe:
        @staticmethod
        def is_available():
            return True

    torch_stub = _FakeTorch(cuda=True, bf16=True)
    torch_stub.cuda = _CudaNoProbe()
    assert optimizer._pick_hf_dtype(torch_stub, "bfloat16", "cuda") == "bf16"


def test_llm_env_info_reports_dir_and_deps():
    """环境信息：目录来自 _llm_roots，依赖探测只给 installed/version 两个字段。"""
    info = optimizer.llm_env_info()
    assert info["llm_dir"] == optimizer._llm_roots()[0]
    assert set(info["deps"]) == {"llama_cpp", "transformers", "torch"}
    for dep in info["deps"].values():
        assert set(dep) == {"installed", "version"}


def test_llm_env_info_dir_matches_this_machine(monkeypatch):
    """目录提示必须是**当前这台机器**现算的路径，不能是写死的常量。

    换台电脑（ComfyUI 装在别的盘）时 _llm_roots 会跟着 folder_paths 走，
    这里用桩 folder_paths 换一个假安装路径来验证它确实是在跟着走的。
    """
    fake = types.ModuleType("folder_paths")
    fake.get_input_directory = lambda: os.path.join("Z:", "elsewhere", "ComfyUI", "input")
    monkeypatch.setitem(sys.modules, "folder_paths", fake)
    info = optimizer.llm_env_info()
    want = os.path.join("Z:", "elsewhere", "ComfyUI", "models", "llm")
    assert info["llm_dir"] == want
    # 目录不存在时也要把路径给出来（用户要照着它新建），只是 exists 为 False
    assert info["llm_dir_exists"] is (False if not os.path.isdir(want) else True)


def test_llm_env_info_exists_flag_true_for_real_dir(tmp_path):
    """目录真存在时 exists 必须为 True（前端据此决定要不要提示"先建目录"）。"""
    root = tmp_path / "ComfyUI" / "models" / "llm"
    root.mkdir(parents=True)
    fake = types.ModuleType("folder_paths")
    fake.get_input_directory = lambda: os.path.join(str(tmp_path), "ComfyUI", "input")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setitem(sys.modules, "folder_paths", fake)
        info = optimizer.llm_env_info()
    finally:
        monkeypatch.undo()
    assert info["llm_dir"] == str(root)
    assert info["llm_dir_exists"] is True
