"""真 torch 环境下的语义桥数值验证（跑法见文末）。

为什么单独一个脚本、而不并进 pytest：本机没有「pytest + torch 双全」的环境
（managed python 有 pytest 没 torch；ComfyUI 的 .venv 有 torch 没 pytest）。
纯逻辑用例在 `tests/test_semantic_bridge.py`（桩化 torch），
涉及真实张量运算的断言放这里 —— 用 .venv 直跑，退出码即结论。

它验的是**行为契约**，不是"跑通就行"：
  ① 权重真能加载，且维度就是 5120→512→512→5120；
  ② 关闭时**原对象原样返回**（不是等值新对象）—— 关闭必须零成本、零副作用；
  ③ scope="text" 时**非文本位置逐字节不变**（这是「保护参考图/首帧图」的全部依据，
     必须是 bit 级而不是 allclose）；
  ④ scope="text" 时文本位置**确实变了**（否则开关是假的：都没改还叫增强？）；
  ⑤ scope="all" 时视觉位置**也会变**（证明两类 token 的差别是真的）；
  ⑥ alpha=0 时输出与原值逐字节相等（0 = 等价不启用）；
  ⑦ 上面每一条在 **fp32/CPU 与 fp16/CUDA 两条精度路径上都成立** ——
     真机跑的是 cuda+fp16，只验 cpu+fp32 等于没验到真实路径。

跑法（仓库根目录）：
    <ComfyUI>/.venv/Scripts/python.exe custom_nodes/ComfyUI-minimaxH3-SequenceForge/tools/check_semantic_bridge.py
"""

import importlib.util
import os
import sys

import torch

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN)

spec = importlib.util.spec_from_file_location(
    "semantic_bridge", os.path.join(PLUGIN, "semantic_bridge.py"))
BR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BR)

DIM = BR.DIM
TAGS = [0, 0, 0, 1, 1, 1, 1, 1]          # 前 3 个是视觉块（参考图/首帧图）
VIS = [i for i, v in enumerate(TAGS) if v == 0]
TXT = [i for i, v in enumerate(TAGS) if v == 1]
failures = []


def check(name, cond, detail=""):
    mark = "OK  " if cond else "FAIL"
    print("  %s %s%s" % (mark, name, (" :: " + detail) if detail else ""))
    if not cond:
        failures.append(name)


def make_cond(seed=0):
    """[1,T,DIM] 假 cond + 逐 token 标签。

    用固定 seed 的随机数而不是常量：常量会让 RMS 配平的除法恒等，
    有些错误（比如把「文本/视觉」两段弄反）就验不出来。
    """
    g = torch.Generator().manual_seed(seed)
    t = torch.randn(1, len(TAGS), DIM, generator=g, dtype=torch.float32)
    meta = {"minimax_token_tags": torch.tensor(TAGS, dtype=torch.long)}
    return [[t, meta]], t


names = BR.scan_adapters()
print("\n== 语义桥真机数值验证 ==")
print("models/ 扫描结果：%s" % names)
check("scan_adapters 扫到权重", bool(names))
if not names:
    print("\nmodels/ 下没有权重，后续无法验证。")
    sys.exit(1)
adapter = names[0]

# ── ① 权重加载与维度（与设备无关，只跑一次）─────────────────────
net = BR.load(adapter, torch.device("cpu"), torch.float32)
layers = {n: tuple(p.shape) for n, p in net.named_parameters()}
print("权重形状：%s" % layers)
check("维度 5120→hidden→hidden→5120",
      layers["fc1.weight"][1] == DIM and layers["fc3.weight"][0] == DIM
      and layers["fc2.weight"][0] == layers["fc1.weight"][0], str(layers))
check("第二次 load 命中缓存（同一对象）",
      BR.load(adapter, torch.device("cpu"), torch.float32) is net)

# 显式替换设备解析器：生产代码里的 compute_target() 走 comfy.model_management，
# 那需要 ComfyUI 运行时；本脚本在运行时之外，所以直接钉死被测目标（下面的 suite
# 会按精度路径再覆盖一次）。`apply` 在调用时按模块属性查这个名字，替换即生效。
BR.compute_target = lambda: (torch.device("cpu"), torch.float32)

# ── 尺寸/缺键报错（一次即可）─────────────────────────────────────
cfg_all = {"enabled": True, "adapter": adapter, "alpha": 0.15, "scope": "all"}
cfg_text = {"enabled": True, "adapter": adapter, "alpha": 0.15, "scope": "text"}
try:
    BR.apply([[torch.zeros(1, 4, 768), {"minimax_token_tags": torch.ones(4, dtype=torch.long)}]],
             cfg_all)
    check("非 [B,T,5120] 输入必须报错", False, "居然没抛错")
except ValueError as e:
    check("非 [B,T,5120] 输入必须报错", "5120" in str(e), str(e)[:80])
try:
    BR.apply([[torch.zeros(1, 4, DIM), {}]], cfg_text)
    check("仅文本档缺 minimax_token_tags 必须报错", False, "居然没抛错")
except ValueError as e:
    check("仅文本档缺 minimax_token_tags 必须报错",
          "minimax_token_tags" in str(e), str(e)[:80])


def suite(label, device, dtype):
    """在指定设备/精度上跑一遍全部行为断言。"""
    print("\n-- %s（device=%s dtype=%s）--" % (label, device, dtype))
    BR.compute_target = lambda: (device, dtype)

    off = {"enabled": False, "adapter": adapter, "alpha": 0.15, "scope": "all"}
    cond_off, _ = make_cond()
    check("[%s] 关闭时原对象原样返回（零成本）" % label, BR.apply(cond_off, off) is cond_off)

    cond_t, orig_t = make_cond()
    out_t = BR.apply(cond_t, cfg_text)
    new_t = out_t[0][0]
    check("[%s] scope=text：输出是新张量（没原地改写输入）" % label, new_t is not orig_t)
    check("[%s] scope=text：视觉 token 逐字节不变" % label,
          torch.equal(new_t[:, VIS, :], orig_t[:, VIS, :]),
          "最大差 %.3e" % float((new_t[:, VIS, :] - orig_t[:, VIS, :]).abs().max()))
    check("[%s] scope=text：文本 token 确实被改（开关不是假的）" % label,
          not torch.equal(new_t[:, TXT, :], orig_t[:, TXT, :]),
          "最大差 %.3e" % float((new_t[:, TXT, :] - orig_t[:, TXT, :]).abs().max()))
    check("[%s] scope=text：元数据对象原样带过（不复制、不改动）" % label,
          out_t[0][1] is cond_t[0][1])

    cond_a, orig_a = make_cond()
    new_a = BR.apply(cond_a, cfg_all)[0][0]
    check("[%s] scope=all：视觉 token 也被改" % label,
          not torch.equal(new_a[:, VIS, :], orig_a[:, VIS, :]),
          "最大差 %.3e" % float((new_a[:, VIS, :] - orig_a[:, VIS, :]).abs().max()))
    check("[%s] 两类 token 的改动确实不同（scope 有实际区分度）" % label,
          not torch.allclose(new_a[:, VIS, :], new_t[:, VIS, :]))

    cond_z, orig_z = make_cond()
    cfg_zero = {"enabled": True, "adapter": adapter, "alpha": 0.0, "scope": "all"}
    new_z = BR.apply(cond_z, cfg_zero)[0][0]
    check("[%s] alpha=0：输出与原值逐字节相等（0 = 等价不启用）" % label,
          torch.equal(new_z, orig_z),
          "最大差 %.3e" % float((new_z - orig_z).abs().max()))

    devs = []
    for a in (0.05, 0.15, 0.4):
        c, o = make_cond()
        n = BR.apply(c, {"enabled": True, "adapter": adapter, "alpha": a, "scope": "all"})[0][0]
        devs.append(float((n - o.to(n.dtype)).abs().mean()))
    print("  alpha → 平均绝对偏离：%s" % ["%.5f" % d for d in devs])
    check("[%s] alpha 越大偏离越大（单调）" % label, devs[0] < devs[1] < devs[2], str(devs))


check("text_slots 与期望的文本下标一致", BR.text_slots(TAGS) == TXT, str(BR.text_slots(TAGS)))

suite("CPU/fp32", torch.device("cpu"), torch.float32)
if torch.cuda.is_available():
    suite("CUDA/fp16（真机路径）", torch.device("cuda:0"), torch.float16)
else:
    print("\n-- 无 CUDA，跳过 fp16 路径（真机正是这条，务必在有卡机器上再跑一次）--")

print()
if failures:
    print("语义桥真机验证失败 %d 项：" % len(failures))
    for f in failures:
        print("  - %s" % f)
    sys.exit(1)
print("语义桥真机验证全部通过")
sys.exit(0)
