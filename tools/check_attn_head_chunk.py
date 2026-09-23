"""注意力头分块等价性验证（真 torch 环境独立跑，不依赖 pytest）。

为什么是独立脚本：本仓库没有「pytest + torch 双全」的环境（见 MEMORY）：
  · managed python 3.13.12 有 pytest 但**没 torch**
  · <根>/.venv 有 torch 但**没 pytest**
所以真 torch 的断言走独立 runner，退出码就是结论。

跑法：
    D:/.../ComfyUI/ComfyUI/.venv/Scripts/python.exe tools/check_attn_head_chunk.py
退出码 0 = 全部通过。

钉的是「头分块 = 精确无损」这条架构断言：按 head 分组算与整段算必须逐元素一致。
`upscale._attn_head_selfcheck` 在装之前会跑同样的对比（运行时守卫），本脚本让
这条性质**可复现、可回归**，而不是只在日志里一闪而过。
"""

import os
import sys
import types

import torch
import torch.nn as nn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---- 桩掉 comfy 家族（upscale.py 导入期需要） ----

def _stub_comfy():
    def mk(name):
        m = types.ModuleType(name)
        m.__path__ = []
        sys.modules[name] = m
        return m

    for sub in ("", ".model_management", ".quant_ops", ".ops",
                ".ldm", ".ldm.modules", ".ldm.modules.attention",
                ".ldm.minimax", ".ldm.minimax.model"):
        mk("comfy" + sub)
    attn = sys.modules["comfy.ldm.modules.attention"]

    def optimized_attention(q, k, v, heads, mask=None, skip_reshape=False,
                            transformer_options=None):
        # q/k/v: [1, H, S, D]（skip_reshape=True 口径）
        # 真 kernel 契约（skip_reshape=True，skip_output_reshape=False）：
        # 输入 [1, H, S, D] -> 输出 [1, S, H*D]
        att = torch.softmax(q @ k.transpose(-1, -2) / (q.shape[-1] ** 0.5), dim=-1)
        return (att @ v).transpose(1, 2).reshape(q.shape[0], q.shape[2], -1)

    attn.optimized_attention = optimized_attention

    # rope 分支要用的两处：`cast_to` 是纯搬运（返原张量即可），
    # `comfy.quant_ops.ck` 指向**真** comfy_kitchen（.venv 里装了 eager 后端，
    # 纯 torch 可跑）—— 故意**不桩**它，这样 rope 断言跑的是真 rope kernel。
    mm = sys.modules["comfy.model_management"]
    # ⚠ `cast_to` 必须 detach：桩返回的是 `nn.Parameter`（`requires_grad=True`），
    # 而 rope 写版本的 `check_rope_inplace` 一见 `requires_grad` 就抛
    # `in-place RoPE operations are inference-only`。真 `cast_to` 会 `.to(device)`
    # 产出新张量、且真模型权重是 `requires_grad=False`（Comfy 加载口径），故此处
    # detach 才与生产同构。
    mm.cast_to = lambda t, device=None: t.detach() if isinstance(t, torch.Tensor) else t
    mm.in_training = False
    try:
        import comfy_kitchen as _ck
        sys.modules["comfy.quant_ops"].ck = _ck
    except ImportError:
        pass


class StubAttention(nn.Module):
    """结构对齐 comfy.ldm.minimax.model.Attention 的最小同构体。"""

    def __init__(self, hidden=64, heads=8, head_dim=8, eps=1e-6):
        super().__init__()
        self.heads = heads
        self.head_dim = head_dim
        inner = heads * head_dim
        self.qkv_proj = nn.Linear(hidden, inner * 3, bias=False)
        # ⚠ eps 必须**显式给值**：torch 的 nn.RMSNorm 默认 eps=None，而 rope kernel
        # （`ck.rms_rope_split_half`）的 C++ 签名要求 float eps，收到 None 会当场抛
        # `Expected a value of type 'float' … found type 'NoneType'`。官方
        # `comfy.ldm.minimax.model.Attention` 建 q_norm 时是 `RMSNorm(head_dim, eps=eps)`
        # —— 显式传值，故真模型不踩这个坑，桩必须同构。
        self.q_norm = nn.RMSNorm(head_dim, eps=eps)
        self.k_norm = nn.RMSNorm(head_dim, eps=eps)
        self.out_proj = nn.Linear(inner, hidden, bias=False)

    def forward(self, x, rope_freqs=None, transformer_options={}, **k):
        s = x.shape[0]
        q, kk, v = self.qkv_proj(x).split(self.heads * self.head_dim, dim=-1)
        q = self.q_norm(q.view(s, self.heads, self.head_dim)).transpose(0, 1)
        kk = self.k_norm(kk.view(s, self.heads, self.head_dim)).transpose(0, 1)
        v = v.view(s, self.heads, self.head_dim).transpose(0, 1)
        att = torch.softmax(q @ kk.transpose(-1, -2) / (self.head_dim ** 0.5), dim=-1)
        out = (att @ v).transpose(0, 1).reshape(s, self.heads * self.head_dim)
        return self.out_proj(out)


class CrossHeadAttention(StubAttention):
    """人为掺入跨 head 耦合 → 分组不再等价（用来验自测会拒绝）。"""

    def forward(self, x, rope_freqs=None, transformer_options={}, **k):
        s = x.shape[0]
        q, kk, v = self.qkv_proj(x).split(self.heads * self.head_dim, dim=-1)
        q = q.view(s, self.heads, self.head_dim)
        q = q * q.mean(dim=(1, 2), keepdim=True)          # ← 跨 head
        kk = self.k_norm(kk.view(s, self.heads, self.head_dim))
        v = v.view(s, self.heads, self.head_dim)
        att = torch.softmax(q @ kk.transpose(-1, -2) / (self.head_dim ** 0.5), dim=-1)
        out = (att @ v).transpose(0, 1).reshape(s, self.heads * self.head_dim)
        return self.out_proj(out)


def _rope_ref(attn, x, rope_freqs):
    """参考实现：**先整段做 rope，再整段 attention**（rope 分支的「整段算」基线）。

    与 `upscale._attn_forward_chunked` 的 rope 分支逐句对齐（同一份 kernel 调用），
    差别只在**不分组**。用它当参照，才能验「分组后仍与整段一致」。
    """
    import comfy.model_management as mm
    import comfy.quant_ops as qo

    s = x.shape[0]
    heads, head_dim = attn.heads, attn.head_dim
    q, k, v = attn.qkv_proj(x).split(heads * head_dim, dim=-1)
    v = v.view(s, heads, head_dim)
    q = q.view(1, s, heads, head_dim)
    k = k.view(1, s, heads, head_dim)
    qw = mm.cast_to(attn.q_norm.weight, device=v.device)
    kw = mm.cast_to(attn.k_norm.weight, device=v.device)
    rot = rope_freqs.shape[-3] * 2
    q, k = qo.ck.rms_rope_split_half(q, k, rope_freqs, qw, kw,
                                     epsilon=attn.q_norm.eps, rot_dim=rot)
    q = q[0].transpose(0, 1).unsqueeze(0)
    k = k[0].transpose(0, 1).unsqueeze(0)
    v = v.transpose(0, 1).unsqueeze(0)
    att = torch.softmax(q @ k.transpose(-1, -2) / (head_dim ** 0.5), dim=-1)
    out = (att @ v).transpose(1, 2).reshape(s, heads * head_dim)
    return attn.out_proj(out)


def _load_upscale():
    """把插件目录造成合成包再 import upscale（它有相对导入 `.checkpoint` 等）。

    目录名含连字符 → 不能当正规包名，所以手工登记一个包名 + __path__，
    让 `from . import xxx` 能解析（与 tests/test_anchors.py::_load 同套路）。
    """
    import importlib

    pkgname = "h3seqforge"
    if pkgname not in sys.modules:
        pkg = types.ModuleType(pkgname)
        pkg.__path__ = [ROOT]
        pkg.__package__ = pkgname
        sys.modules[pkgname] = pkg
    return importlib.import_module(pkgname + ".upscale")


def main():
    _stub_comfy()
    up = _load_upscale()

    results = []

    def check(name, fn):
        try:
            fn()
            results.append(("OK  ", name))
        except AssertionError as e:
            results.append(("FAIL", f"{name} :: {e}"))
        except Exception as e:                        # noqa: BLE001
            results.append(("ERR ", f"{name} :: {type(e).__name__}: {e}"))

    torch.manual_seed(0)

    # ① 核心：分组 == 整段（逐元素）
    def t_equiv():
        attn = StubAttention()
        x = torch.randn(12, 64)
        ref = attn(x)
        for n in (2, 4, 8):
            got = up._attn_forward_chunked(attn, x, None, n)
            assert got.shape == ref.shape, f"n={n} 形状变了"
            assert torch.allclose(got, ref, atol=1e-5, rtol=1e-5), f"n={n} 不等价"
    check("分组算 == 整段算（n=2/4/8，逐元素）", t_equiv)

    # ② 分组真的生效（防「忽略 n 悄悄走整段」的假通过）
    def t_used():
        calls = []
        import comfy.ldm.modules.attention as am

        def spy(q, k, v, heads, mask=None, skip_reshape=False, transformer_options=None):
            calls.append(int(heads))
            att = torch.softmax(q @ k.transpose(-1, -2) / (q.shape[-1] ** 0.5), dim=-1)
            return (att @ v).transpose(1, 2).reshape(q.shape[0], q.shape[2], -1)

        old = am.optimized_attention
        am.optimized_attention = spy
        try:
            up._attn_forward_chunked(StubAttention(), torch.randn(6, 64), None, 4)
        finally:
            am.optimized_attention = old
        assert calls == [2, 2, 2, 2], f"分组数没生效，实际 head 分组 = {calls}"
    check("分组真的传给 kernel（4 组 → [2,2,2,2]）", t_used)

    # ③ heads 不整除 n：全覆盖、不重不漏
    def t_uneven():
        seen = []
        import comfy.ldm.modules.attention as am

        def spy(q, k, v, heads, mask=None, skip_reshape=False, transformer_options=None):
            seen.append(int(heads))
            att = torch.softmax(q @ k.transpose(-1, -2) / (q.shape[-1] ** 0.5), dim=-1)
            return (att @ v).transpose(1, 2).reshape(q.shape[0], q.shape[2], -1)

        old = am.optimized_attention
        am.optimized_attention = spy
        try:
            attn = StubAttention()
            x = torch.randn(6, 64)
            ref = attn(x)
            got = up._attn_forward_chunked(attn, x, None, 3)
        finally:
            am.optimized_attention = old
        assert sum(seen) == 8, f"head 总数不是 8：{seen}"
        assert torch.allclose(got, ref, atol=1e-5, rtol=1e-5), "8 头分 3 组不等价"
    check("8 头分 3 组：全覆盖且仍等价", t_uneven)

    # ④ n > heads 被夹住
    def t_clamp():
        attn = StubAttention(heads=4, head_dim=8)
        x = torch.randn(5, 64)
        got = up._attn_forward_chunked(attn, x, None, 99)
        assert torch.allclose(got, attn(x), atol=1e-5, rtol=1e-5), "n>heads 没夹住"
    check("n > heads 时按 heads 夹住", t_clamp)

    # ⑤ 自测：干净的通过
    check("装前自测：干净 attn 通过", lambda: (
        _assert(up._attn_head_selfcheck(StubAttention(), 4) is True, "自测该通过")))

    # ⑥ 自测：跨 head 的拒绝
    check("装前自测：跨 head 耦合被拒绝", lambda: (
        _assert(up._attn_head_selfcheck(CrossHeadAttention(), 4) is False, "自测该拒绝")))

    # ⑦ 关闭语义
    check("n<=1 / None = 关（返回 (0, \"\")）", lambda: (
        _assert(up.install_low_vram_attention(None, 1) == (0, ""), "n=1 该关"),
        _assert(up.install_low_vram_attention(None, 0) == (0, ""), "n=0 该关"),
        _assert(up.install_low_vram_attention(None, None) == (0, ""), "None 该关")))

    # ⑧ 非 H3 结构
    def t_nonh3():
        outer = nn.Module()
        outer.diffusion_model = nn.Module()
        n, why = up.install_low_vram_attention(outer, 4)
        assert n == 0 and why, "非 H3 结构该拒绝并说明理由"
    check("非 H3 结构：拒绝并有理由", t_nonh3)

    # ⑨ 装到所有 block + 幂等
    def t_install():
        inner = nn.Module()
        inner.blocks = nn.ModuleList([nn.Module() for _ in range(3)])
        for b in inner.blocks:
            b.attn = StubAttention()
        outer = nn.Module()
        outer.diffusion_model = inner
        n, why = up.install_low_vram_attention(outer, 4)
        assert n == 3, f"该装 3 个，实际 {n}（{why}）"
        for b in inner.blocks:
            assert getattr(b.attn, "_h3_attn_head_chunks", 0) == 4, "没打标记"
        n2, _ = up.install_low_vram_attention(outer, 4)
        assert n2 == 3, "重复装该仍是 3（覆盖不叠加）"
        x = torch.randn(5, 64)
        c = StubAttention()
        c.load_state_dict(inner.blocks[0].attn.state_dict())
        assert torch.allclose(inner.blocks[0].attn(x), c(x), atol=1e-5, rtol=1e-5), \
            "装完 forward 结果变了"
    check("装上全部 blocks、幂等、结果不变", t_install)

    # ⑩ 结构缺失 → 拒绝
    def t_missing():
        inner = nn.Module()
        inner.blocks = nn.ModuleList([nn.Module()])
        inner.blocks[0].attn = nn.Module()          # 缺 qkv_proj 等
        outer = nn.Module()
        outer.diffusion_model = inner
        n, why = up.install_low_vram_attention(outer, 4)
        assert n == 0 and "结构不匹配" in why, f"该报结构不匹配，实际：{why}"
    check("attn 结构缺字段：报「结构不匹配」", t_missing)

    # ⑪ rope 分支（隐患 3）：装了 rope_freqs 时分组仍必须与整段逐元素一致。
    #    为什么单列一条：`_attn_forward_chunked` 走 rope 分支时**换了一条 kernel 路径**
    #    （`rms_rope_split_half` 融合 RMSNorm + 部分 split-half rope），前面的断言
    #    全传 rope_freqs=None，等于这条分支从未被验证过。
    #    rope 在**最后一维**（head_dim）上做，与 head 无关 → 分组必然可分离，
    #    这条断言就是把这个「必然」钉成可回归的事实。
    def t_rope():
        import comfy_kitchen  # noqa: F401  （无则在下面显式跳过）
        attn = StubAttention(heads=8, head_dim=8)
        s = 12
        x = torch.randn(s, 64)
        rot = 8                                   # 全旋转（= head_dim）
        freqs = torch.randn(1, s, 1, rot // 2, 2, 2)
        # ⚠ 必须在 no_grad 下：rope 分支在生产里走 `rms_rope_split_half_`（写版本），
        # 该 kernel 明确 `inference-only`，带 grad 会抛
        # `in-place RoPE operations are inference-only and do not support autograd`。
        # 推理场景本就无 grad，这里如实对齐。
        with torch.no_grad():
            ref = _rope_ref(attn, x, freqs)
            for n in (2, 4, 8):
                got = up._attn_forward_chunked(attn, x, freqs, n)
                assert got.shape == ref.shape, f"rope 分支 n={n} 形状变了"
                assert torch.allclose(got, ref, atol=1e-5, rtol=1e-5), \
                    f"rope 分支 n={n} 分组 != 整段"
    try:
        import comfy_kitchen  # noqa: F401
        check("rope 分支：分组 == 整段（n=2/4/8，真 rope kernel）", t_rope)
    except ImportError:
        results.append(("SKIP", "rope 分支：环境无 comfy_kitchen"))

    # ⑫ partial rotary：rot_dim < head_dim 时未旋转部分原样透传，分组仍等价。
    def t_rope_partial():
        attn = StubAttention(heads=8, head_dim=8)
        s = 10
        x = torch.randn(s, 64)
        rot = 4                                   # 只旋转前 4 维（head_dim 的一半）
        freqs = torch.randn(1, s, 1, rot // 2, 2, 2)
        with torch.no_grad():
            ref = _rope_ref(attn, x, freqs)
            got = up._attn_forward_chunked(attn, x, freqs, 4)
        assert torch.allclose(got, ref, atol=1e-5, rtol=1e-5), \
            "partial rotary 下分组 != 整段"
    try:
        import comfy_kitchen  # noqa: F401
        check("rope 分支：partial rotary（rot=4<8）分组 == 整段", t_rope_partial)
    except ImportError:
        results.append(("SKIP", "rope 分支 partial：环境无 comfy_kitchen"))

    print()
    bad = 0
    for tag, msg in results:
        print(f"  {tag} {msg}")
        if tag != "OK  ":
            bad += 1
    print()
    print("check_attn_head_chunk：全部通过" if not bad
          else f"check_attn_head_chunk：{bad} 项失败")
    return 0 if not bad else 1


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


if __name__ == "__main__":
    sys.exit(main())
