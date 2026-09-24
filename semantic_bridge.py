"""BUNNY H3 语义桥：对 H3 文本 conditioning 做小网络增强（内联版，不装原版节点）。

来源：FourBunny / JOKER141 的 BUNNY H3 Conditioning Bridge。原版是插在「H3 文本编码
之后、下游节点之前」的 CONDITIONING 线上的独立节点；本项目画布的 CONDITIONING 入口
已删除（P4d），cond 在节点内部现编，故把同一套计算内联进渲染链。

计算：cond 张量 [B, T, 5120] 逐 token 过一次 5120→512→512→5120 的瓶颈 MLP，再按 alpha
与原值混合：

    out = t + alpha * (mlp(rms(t)) - t)

用途：让 H3 更稳地维持「谁在做什么 / 道具归属 / 攻击者与目标 / 遮挡后的身份与状态」
这类关系。**不是动作修复工具**，也不改写提示词。（作者自述：约 60% 案例有改善、
20% 无差别、10% 出现新错误 —— 强度不是越高越好，建议同 seed 对比。）

═══ 作用范围（scope）：为什么需要这个开关 ═══

cond 张量里**同时混着文本 token 和视觉 token** —— H3 不走 chat template，参考图/首帧图
以 vision block 形式直接拼进序列（见 comfy/text_encoders/minimax.py 文件头）。逐 token
标签在 cond 元数据的 `minimax_token_tags` 里（官方 MiniMaxH3ClipModel.encode_token_weights
产出，`token_tags_from_embeds_info`）：1 = 文本，0 = 视觉块（含两侧 vision_start/end 各一个）。

    scope="all"    全量过桥 —— 视觉 token 一起改（原版行为，默认）
    scope="text"   仅文本过桥 —— 只改 tag==1，视觉 token **逐字节保留**

哪些图会变成视觉 token（这决定了 scope 的实际覆盖面）：
  · 正文里 `@` 引用的素材 —— 包括**同时被当尾帧图/首帧图用的那些**（nodes 把段引用标签
    无条件写进 ref_images，那里不过滤帧锚）
  · 段 0 且该段无素材引用时，走官方 `first_frame=` 的首帧图
帧锚**本身**（尾帧图 / 每段尾帧锚定 / 头锚 / 手动锚）只做 VAE latent keyframe，不进这个
张量 —— 语义桥碰不到它们。别把 scope 理解成「增强 latent 锚」。

═══ 与锚定、TE 缓存、negative 的关系 ═══

· 只改 cond[i][0]（张量），不动 cond[i][1]（元数据）；锚定走 conditioning_set_values
  写的是元数据 —— 两者正交，所以在锚定前还是锚定后调用**结果逐字节相同**。
· cond_cache.CachedClipProxy 缓存的是纯文本 encode 结果（本模块在它之外），不污染缓存。
· negative 不接：空串纯文本、无视觉 token、全链只建一次。

═══ 适配器权重 ═══

放本插件 `models/` 目录（任意 .safetensors）。目录名不依赖原版节点 —— 内联后不需要装它。
"""

import os

import torch
import torch.nn as nn

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(PLUGIN_DIR, "models")

DIM = 5120
"""H3 文本编码器输出维：comfy/text_encoders/minimax.py 的 MiniMaxH3Tokenizer
`embedding_size=5120`。权重与 cond 末维都必须是它 —— 不符时明确报错，别静默放行。"""

TEXT_TAG = 1
"""minimax_token_tags 里代表「文本 token」的值（0 = 视觉块）。"""

MAGNITUDE_MATCH = "per_token"
"""原值的三种 RMS 对齐口径（per_token / global / none）里作者推荐的默认，固定不暴露：
上游 README 的推荐起始配置就是它，多一个旋钮只会多一个说不清的变量。"""

DEFAULT_ALPHA = 0.15
DEFAULT_SCOPE = "all"
SCOPES = ("all", "text")


class SemanticBridgeMLP(nn.Module):
    """5120 → hidden → hidden → 5120（SiLU）。hidden 由权重形状推断，不写死 512。"""

    def __init__(self, hidden, dim=DIM):
        super().__init__()
        self.fc1 = nn.Linear(dim, hidden)
        self.fc2 = nn.Linear(hidden, hidden)
        self.fc3 = nn.Linear(hidden, dim)
        self.act = nn.SiLU()

    def forward(self, x):
        return self.fc3(self.act(self.fc2(self.act(self.fc1(x)))))


def text_slots(tags):
    """逐 token 标签 -> 需要过桥的下标表（tag == 1）。

    纯 Python（不碰张量），便于无 torch 环境下直接单测掩码口径 —— 这是
    「仅文本过桥」唯一的行为定义处，值得独立可测。
    """
    return [i for i, v in enumerate(tags) if int(v) == TEXT_TAG]


def scan_adapters():
    """本插件 models/ 下的语义桥权重文件名（无目录 = 空表）。"""
    if not os.path.isdir(MODEL_DIR):
        return []
    return sorted((n for n in os.listdir(MODEL_DIR)
                   if n.lower().endswith(".safetensors")), key=str.lower)


def path_of(name):
    """权重名 -> 绝对路径。只认 models/ 下的**裸文件名**（带路径分隔符直接拒）。

    为什么拒绝子路径：一是防目录穿越，二是避免同一个文件在报告里显示成两种路径、
    让「换了权重」的判定自相矛盾。
    """
    raw = str(name or "").strip()
    if not raw:
        raise ValueError("语义桥未选择权重文件：请在导演台右栏「语义桥」里选一个 models/ 下的 .safetensors")
    if os.path.basename(raw) != raw:
        raise ValueError(f"语义桥权重名非法（应为 models/ 下的文件名）：{name!r}")
    path = os.path.join(MODEL_DIR, raw)
    if not os.path.isfile(path):
        raise ValueError(f"语义桥权重不存在：{path}")
    return path


def config(ds):
    """ds.bridge -> 生效配置。缺键/旧档一律收敛到「关闭 + alpha 0.15 + 全量」。

    关闭时 apply 直接返回原 cond —— 不加载权重、不碰张量，也不进任何指纹，
    所以对没开语义桥的项目零影响。
    """
    raw = ds.get("bridge") if isinstance(ds, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    scope = str(raw.get("scope") or DEFAULT_SCOPE)
    alpha = min(1.0, max(0.0, float(raw.get("alpha", DEFAULT_ALPHA))))
    return {
        # **只认真 True**（`is True`，与前端 `=== true` 同口径）。不能用 `bool()`：
        # JSON 里手改成 `"enabled": "false"` 的档会因非空字符串为真而**静默开启**整个
        # 桥 —— 一次「以为关了」的渲染，代价是整链重做。
        "enabled": raw.get("enabled") is True,
        "adapter": str(raw.get("adapter") or "").strip(),
        "alpha": alpha,
        "scope": scope if scope in SCOPES else DEFAULT_SCOPE,
    }


def compute_target():
    """(设备, 精度)：跟随 ComfyUI 当前设备，cuda 用 fp16（原版口径）。

    comfy 走函数内延迟导入 —— 本模块的单测要在无 ComfyUI 环境下加载。
    """
    import comfy.model_management
    dev = comfy.model_management.get_torch_device()
    return dev, (torch.float16 if dev.type == "cuda" else torch.float32)


_CACHE = {}


def load(name, device, dtype):
    """按 (路径, mtime, 设备, 精度) 缓存加载权重；权重文件被换掉时自动重载。

    同文件的旧条目在重载时清掉：它们再也不可能被命中，留着只是白占 21MB 显存。
    """
    path = path_of(name)
    key = (path, os.path.getmtime(path), str(device), str(dtype))
    net = _CACHE.get(key)
    if net is not None:
        return net
    for stale in [k for k in _CACHE if k[0] == path and k != key]:
        _CACHE.pop(stale, None)

    from safetensors.torch import load_file
    weights = load_file(path)
    missing = [k for k in ("fc1.weight", "fc1.bias", "fc2.weight", "fc2.bias",
                           "fc3.weight", "fc3.bias") if k not in weights]
    if missing:
        raise ValueError(f"语义桥权重缺少张量 {missing}：{path}")
    fc1, fc3 = weights["fc1.weight"].shape, weights["fc3.weight"].shape
    if fc1[1] != DIM or fc3[0] != DIM:
        raise ValueError(
            f"语义桥权重维度不符（期望 {DIM} → hidden → hidden → {DIM}）："
            f"fc1={tuple(fc1)}，fc3={tuple(fc3)}：{path}")

    net = SemanticBridgeMLP(int(fc1[0]), int(fc1[1]))
    with torch.no_grad():
        net.fc1.weight.copy_(weights["fc1.weight"].float())
        net.fc1.bias.copy_(weights["fc1.bias"].float())
        net.fc2.weight.copy_(weights["fc2.weight"].float())
        net.fc2.bias.copy_(weights["fc2.bias"].float())
        net.fc3.weight.copy_(weights["fc3.weight"].float())
        net.fc3.bias.copy_(weights["fc3.bias"].float())
    net = net.to(device=device, dtype=dtype).eval().requires_grad_(False)
    _CACHE[key] = net
    return net


def _rms(x, dtype):
    """逐 token RMS 归一（权重的 input_normalization=rms_per_token）。

    RMS **在 fp32 里算**（与上游实现同口径）：fp16 下 `x.pow(2)` 在 |x| > 256 时直接
    溢出成 inf，而 hidden state 的幅度没有上界保证 —— 这不是防御性写法，是数值前提。
    """
    xf = x.float()
    return (xf / torch.sqrt(xf.pow(2).mean(dim=-1, keepdim=True) + 1e-6)).to(dtype)


def _match_rms(value, ref, dtype):
    """把 value 的逐 token RMS 缩放到 ref 的逐 token RMS（MAGNITUDE_MATCH 口径）。

    模型输出与原始 cond 的能量尺度本来就不一样，不配一次会把整体响度带偏；
    配的是**逐 token** 而不是全局，所以只对齐幅度、不搬运 token 之间的差异。
    同样在 fp32 里算，理由见 `_rms`。
    """
    vf, rf = value.float(), ref.float()
    scaled = vf * (torch.sqrt(rf.pow(2).mean(dim=-1, keepdim=True) + 1e-8)
                   / torch.sqrt(vf.pow(2).mean(dim=-1, keepdim=True) + 1e-8))
    return scaled.to(dtype)


def _project(net, h, dtype):
    """已在计算设备/精度上的张量 -> 过桥结果（同 device/dtype）。"""
    with torch.inference_mode():
        out = net(_rms(h, dtype))
    return _match_rms(out, h, dtype)


def apply(cond, cfg):
    """对 conditioning 逐项过桥。cfg 为 config() 的返回值；关闭时原样返回。

    scope="text" 时**只写回文本 token 的位置**（在原始张量的副本上就地覆盖），
    视觉 token 与原件逐字节相同 —— 不是「算出等值再放回去」，是根本没碰。
    """
    if not cfg["enabled"]:
        return cond
    device, dtype = compute_target()
    net = load(cfg["adapter"], device, dtype)
    alpha = cfg["alpha"]
    only_text = cfg["scope"] == "text"

    out = []
    for item in cond:
        tensor, meta = item[0], item[1]
        if tensor.ndim != 3 or tensor.shape[-1] != DIM:
            raise ValueError(
                f"语义桥的输入不是 H3 conditioning [B,T,{DIM}]，实为 {tuple(tensor.shape)}："
                "语义桥必须作用在 H3 文本编码之后的 cond 上")
        h = tensor.to(device=device, dtype=dtype)
        delta = (_project(net, h, dtype) - h).to(tensor.dtype)
        if only_text:
            tags = meta.get("minimax_token_tags")
            if tags is None:
                raise ValueError(
                    "『仅文本过桥』需要 cond 元数据里的 minimax_token_tags（H3 文本编码器产出），"
                    "当前 cond 没有这个键 —— 请改用『全量过桥』")
            slots = text_slots(tags.tolist())
            blended = tensor.clone()
            if slots:
                idx = torch.as_tensor(slots, device=tensor.device)
                blended[:, idx, :] = tensor[:, idx, :] + alpha * delta[:, idx, :].to(tensor.device)
        else:
            blended = tensor + alpha * delta.to(tensor.device)
        # meta 不复制：ComfyUI 的 conditioning_set_values 是写时复制，本模块也只读它。
        out.append([blended, meta])
    return out
