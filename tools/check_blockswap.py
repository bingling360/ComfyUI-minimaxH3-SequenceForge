"""块交换接线的真环境验证（真 comfy + 真 aimdo，独立 runner，退出码即结论）。

为什么必须有这个脚本：块交换**没有自己的搬权重代码** —— 机制在 ComfyUI 那边
（DynamicVRAM 的 vbar 换入 + `comfy/ldm/minimax/model.py:751-765` 的块级预取队列）。
本插件只做两件可验证的事：
  ① `blocks_to_swap` → aimdo 的**显存预留**（`set_simple_vram_headroom`）
  ② `blocks_prefetch` → 官方预取队列的开关（包 DiT 的 `_forward` 钉
     `transformer_options["prefetch_dynamic_vbars"]`）
前者能在这台机器上**真调真读回**（不需要加载 20GB 模型），所以有真实证据；后者用
假 DiT 验「装/拆/幂等/签名守卫」。

跑法：
    D:/.../ComfyUI/ComfyUI/.venv/Scripts/python.exe tools/check_blockswap.py
退出码 0 = 全部通过。

⚠ 本脚本会**在本进程内**初始化 aimdo 并改显存预留 —— 它是个一次性进程，跑完把预留
还原、`deinit()` 收尾；正在运行的 ComfyUI 实例不受影响（进程级状态各自独立）。

★ 为什么这里要自己复刻 main.py 那两步：`comfy.memory_management.aimdo_enabled` 与
`CoreModelPatcher = ModelPatcherDynamic` **只有 main.py 会设**（`main.py:300-301`），
独立进程里默认为 False/ModelPatcher —— 不设就会把「有 aimdo 的机器」误判成 legacy。
这就是探测函数 `probe_blockswap()` 的前提，先讲清楚再断言。
"""

import os
import sys

import torch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 本脚本与 check_attn_head_chunk.py 的关键区别：那边**桩掉** comfy，这边要**真** comfy
# （要真 aimdo 才能验显存预留）—— 所以必须把 ComfyUI 根也放进 sys.path，否则
# `import comfy` 直接 ModuleNotFoundError（插件目录本身不含 comfy 包）。
COMFY_ROOT = os.path.dirname(os.path.dirname(ROOT))     # custom_nodes/.. = ComfyUI 根
for _p in (ROOT, COMFY_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import perf  # noqa: E402  （零 ComfyUI 依赖的纯函数模块）

GB = float(1024 ** 3)

_fails = []


def _assert(cond, msg):
    if cond:
        print(f"  OK  {msg}")
    else:
        print(f"  ✗   {msg}")
        _fails.append(msg)


class _FakeDiT:
    """最小 DiT 替身：只要 `_forward(self, x, timestep, context, transformer_options={}, ...)`。

    签名与官方 `MiniMaxH3Model._forward` 同形 —— `install_block_prefetch` 靠
    `inspect.signature` 找 `transformer_options` 的位置，签名不对它就是「版本变了、
    不干预」，所以假件必须同构才有验证价值。
    """

    def __init__(self):
        self.seen = []

    def _forward(self, x, timestep, context, transformer_options={}, minimax_payload=None,
                 denoise_mask=None, audio_denoise_mask=None, **kwargs):
        self.seen.append(dict(transformer_options))
        return transformer_options


class _NoFlagDiT:
    """签名里**没有** transformer_options（模拟上游改签名）。"""

    def _forward(self, x, timestep, context, **kwargs):
        return None


class _FakeModel:
    def __init__(self, diff):
        self.diffusion_model = diff


print("块交换接线验证（真 comfy / 真 aimdo）")

# ---------------- ① 纯函数：换算与夹取 ----------------
print("\n① 纯函数：N 块 → 显存预留")
r = perf.blockswap_headroom(10, 20.0, 32.0)
_assert(abs(r["extra_gb"] - 4.0) < 1e-9 and r["blocks_eff"] == 10 and not r["clamped"],
        "10 块 × (20GB/50) = 4.00GB，不触发夹取")
n6 = perf.suggest_blocks(20.0, 6.0)
r6 = perf.blockswap_headroom(n6, 20.0, 6.0)
_assert(r6["clamped"] and r6["extra_gb"] <= 6.0 * perf.BLOCKSWAP_HEADROOM_CAP + 1e-9,
        f"6GB 卡：suggest_blocks={n6} 块被夹到 {r6['blocks_eff']} 块"
        f"（{r6['extra_gb']:.2f}GB ≤ 显存一半）")
_assert("未探明" in perf.blockswap_headroom(25, None, 16.0)["note"],
        "体量未知 → 明确「未探明」，不编数字")

# ---------------- ② main.py 之前 / 之后，探测怎么说 ----------------
print("\n② 探测：aimdo 未初始化 vs 已初始化")
before = perf.probe_blockswap()
_assert(before["aimdo"] is False and before["path"] == "legacy",
        "未初始化时如实报 legacy（aimdo_enabled 只有 main.py 会设）")

import comfy_aimdo.control as ctl  # noqa: E402
import comfy.memory_management as mem  # noqa: E402
import comfy.model_management as mm  # noqa: E402
import comfy.model_patcher as mp  # noqa: E402

_assert(ctl.init() is True, "comfy_aimdo.control.init() 成功（真库）")
devs = list(mm.get_all_torch_devices())
try:
    ok = ctl.init_devices((d.index, 0) for d in devs)
except TypeError:                       # comfy-aimdo 0.4.9 协议
    ok = ctl.init_devices(d.index for d in devs)
_assert(ok is True, f"init_devices 成功（设备 {devs}）")
# ★ 复刻 main.py:300-301 的两步（不设就会把 aimdo 机器误判成 legacy）
mem.aimdo_enabled = True
mp.CoreModelPatcher = mp.ModelPatcherDynamic

perf._BS_BASELINE.clear()              # 基线必须在「已初始化」之后读一次
base = perf._blockswap_baseline()
_assert(base["aimdo_gb"] is not None,
        f"读到 aimdo 预留基线 {base['aimdo_gb']}GB（读回接口可用）")
st = perf.probe_blockswap()
_assert(st["path"] == "aimdo" and st["headroom_gb"] is not None,
        f"初始化后判为 aimdo 路径（streams={st['streams']}、"
        f"non_blocking={st['non_blocking']}、pinned={st['pinned_gb']}GB）")
_assert("DynamicVRAM 在线" in perf.blockswap_line(st, st["headroom_gb"]),
        "状态行含「DynamicVRAM 在线」")

# ---------------- ③ 真设真读回（本脚本的核心证据） ----------------
print("\n③ 真设 aimdo 显存预留并读回")
got = perf._set_aimdo_headroom(0.75)
_assert(got is not None and abs(got - 0.75) < 1e-6,
        f"set_simple_vram_headroom(0.75GB) → get 读回 {got}GB（真调用，不是记账）")
got2 = perf._set_aimdo_headroom(base["aimdo_gb"])
_assert(got2 is not None and abs(got2 - base["aimdo_gb"]) < 1e-6,
        f"还原到基线 {base['aimdo_gb']}GB → 读回 {got2}GB")

# ---------------- ④ apply_blockswap 端到端（真写进程状态） ----------------
print("\n④ apply_blockswap：开 → 抬预留；关 → 恢复基线")
hw = {"unet_gb": 20.0, "vram_total_gb": 32.0}
out_on = perf.apply_blockswap({"blocks_swap_on": True, "blocks_to_swap": 10}, hw)
want = base["aimdo_gb"] + 4.0
_assert(out_on["applied"] is True and out_on["blocks"] == 10,
        f"开：applied={out_on['applied']}、{out_on['blocks']} 块、"
        f"预留应到 {want:.2f}GB")
_assert(abs(out_on["headroom_gb"] - want) < 1e-6,
        f"读回核验：{out_on['headroom_gb']}GB == 基线 + 4.00GB")
_assert(abs(ctl.get_simple_vram_headroom() / GB - want) < 1e-6,
        "直接问 aimdo 原生接口，值一致（不是只更新了我们自己的变量）")
out_off = perf.apply_blockswap({"blocks_swap_on": False, "blocks_to_swap": 10}, hw)
_assert(out_off["applied"] is True
        and abs(out_off["headroom_gb"] - base["aimdo_gb"]) < 1e-6,
        f"关：预留已恢复基线（{out_off['headroom_gb']}GB）")
_assert("恢复" in out_off["note"], "关的说明里写清「已恢复」，别让用户以为还开着")
out_unk = perf.apply_blockswap({"blocks_swap_on": True, "blocks_to_swap": 10},
                               {"vram_total_gb": 32.0})
_assert(out_unk["applied"] is False and abs(ctl.get_simple_vram_headroom() / GB
                                           - base["aimdo_gb"]) < 1e-6,
        "★ 体量未知时**不写**（面板打开那种场景：读接口不许改进程状态）")

# ---------------- ⑤ 预取开关：装 / 幂等 / 拆 / 签名守卫 ----------------
print("\n⑤ install_block_prefetch：装 → 幂等 → 拆")
# 目录名含连字符（ComfyUI-minimaxH3-SequenceForge）**不能当正规包名**，而 upscale.py
# 顶层就 `from . import checkpoint` → 必须手工登记一个合成包再按子模块导入
# （setup.py 那套在这里用不了）。⚠ 只造顶层包 + `__path__`，**别**预造
# `h3seqforge.upscale` 之类的空模块 —— 那会顶掉真文件路径（本仓库踩过）。
import types  # noqa: E402

if "h3seqforge" not in sys.modules:
    _pkg = types.ModuleType("h3seqforge")
    _pkg.__path__ = [ROOT]
    sys.modules["h3seqforge"] = _pkg
from h3seqforge import upscale  # noqa: E402
dit = _FakeDiT()
model = _FakeModel(dit)
orig = dit._forward
k, why = upscale.install_block_prefetch(model, False)
_assert(k == 1 and "预取已关闭" in why, f"关：装上包装（{why}）")
to = {"prefetch_dynamic_vbars": True}
dit._forward(1, 2, 3, to)
_assert(to["prefetch_dynamic_vbars"] is False,
        "★ 前向时那个 flag 真被钉成 False（官方 block 循环就是读它）")
k2, why2 = upscale.install_block_prefetch(model, False)
_assert(k2 == 0 and why2 == "", "幂等：重复关不再包一层")
k3, why3 = upscale.install_block_prefetch(model, True)
_assert(k3 == 1 and dit._forward.__func__ is orig.__func__,
        f"开：拆掉包装、交回官方默认（{why3}）")
_assert(not hasattr(dit, "_h3_block_prefetch"), "拆干净了，没留标记")
k4, why4 = upscale.install_block_prefetch(model, True)
_assert(k4 == 0 and why4 == "", "开（且本来就没装）→ 不干预，返回空说明")

dit2 = _FakeDiT()
m2 = _FakeModel(dit2)
upscale.install_block_prefetch(m2, False)
to2 = {"prefetch_dynamic_vbars": True}
dit2._forward(1, 2, 3, transformer_options=to2)     # 关键字传入这条路径也要覆盖
_assert(to2["prefetch_dynamic_vbars"] is False, "flag 用关键字传进来时也能钉住")

noflag = _NoFlagDiT()
k5, why5 = upscale.install_block_prefetch(_FakeModel(noflag), False)
_assert(k5 == 0 and "transformer_options" in why5,
        f"上游签名变了 → 不干预并说明（{why5}）")
k6, why6 = upscale.install_block_prefetch(object(), False)
_assert(k6 == 0 and "diffusion_model" in why6, f"非 H3 结构 → 拒绝（{why6}）")

# ---------------- 收尾 ----------------
print("\n⑥ 收尾：还原预留 + deinit")
perf._set_aimdo_headroom(base["aimdo_gb"])
_assert(abs(ctl.get_simple_vram_headroom() / GB - base["aimdo_gb"]) < 1e-6,
        "预留已还原到出厂基线")
ctl.deinit()
mem.aimdo_enabled = False
perf._BS_BASELINE.clear()

print()
if _fails:
    print(f"check_blockswap 失败：{len(_fails)} 条")
    for f in _fails:
        print("  - " + f)
    sys.exit(1)
print("check_blockswap 全部通过")
sys.exit(0)
