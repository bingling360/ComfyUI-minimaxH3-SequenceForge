"""Minimax H3 latent 神经放大网络（2D 残差骨干 + 纯 3D 卷积两种）。

逐行移植自 LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler 的
nodes/minimax_h3_latent_upscaler_2d.py 与 minimax_h3_latent_upscaler_3d.py
（推理节点部分不移植，只保留网络与权重加载）。与上游的差异：
- 两种骨干合并进一个模块（同名组件 2D/3D 各一份，互不干扰）
- folder_paths / 模型目录注册延迟到首次扫描（无 ComfyUI 环境可 import 本模块做前向单测）
- 模型缓存键加入 arch（同一权重文件按 2D/3D 分别加载，不串味）

网络口径（上游训练代码一致，勿改）：
- 24 通道 H3 VAE latent，LATENTS_MEAN/STD 归一化（放大前 (x-μ)/σ，放大后反变换）
- 时间维 T 绝对不变（H3 的 17k+5 token 网格决定），只放大 H×W
- scale 1.0-4.0 进 scale embedding；推理强制 attn=False（上游同款优化）
- 2D 权重 load_state_dict(strict=False)（attn 强制关闭会缺键），3D strict=True
- 权重来源：HuggingFace LBH-123-AI/Minimax_h3_latent_Upscaler，放入
  ComfyUI/models/latent_upscale_models/（.pth/.safetensors）
"""

import glob
import os
import re

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange

LATENT_UPSCALE_FOLDER = "latent_upscale_models"
_FOLDER_REGISTERED = False

LATENTS_MEAN = [
    0.858090341091156, -0.9606591463088989, 1.0661640167236328, -0.5090325474739075,
    -0.2727581858634949, -1.3675414323806763, -0.2553254961967468, -0.26907554268836975,
    -0.5376840829849243, -0.0464097298681736, 0.6657370328903198, 0.19690127670764923,
    -0.5460608005523682, -0.4035342037677765, -0.23683024942874908, 0.25928452610969543,
    -0.30133944749832153, 0.211341992020607, -1.1206848621368408, 0.3581933379173279,
    -0.04225143790245056, 0.2604829967021942, 0.22864092886447906, 0.7056031823158264,
]
LATENTS_STD = [
    1.2223774194717407, 1.2767263650894165, 1.6831774711608887, 1.7549455165863037,
    1.5636216402053833, 2.194143533706665, 0.9653137922286987, 1.0569885969161987,
    0.841948926448822, 0.7729952931404114, 1.8955937623977661, 0.946841835975647,
    0.7996809482574463, 0.44988900423049927, 0.7197399735450745, 0.6936293244361877,
    2.961095094680786, 2.7694199085235596, 3.0496184825897217, 2.1088054180145264,
    3.276226282119751, 3.1627357006073, 2.2816812992095947, 2.6127843856811523,
]


def _make_norm_tensors(device, dtype):
    mean = torch.tensor(LATENTS_MEAN, dtype=dtype, device=device).view(1, -1, 1, 1, 1)
    std = torch.tensor(LATENTS_STD, dtype=dtype, device=device).view(1, -1, 1, 1, 1)
    return mean, std


def normalization(channels):
    return nn.GroupNorm(32, channels)


def zero_module(module):
    for p in module.parameters():
        p.detach().zero_()
    return module


# ---- 2D 主干组件（channels=640 + TemporalConv，F.interpolate bilinear） ----

class AttnBlock(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.norm = normalization(in_channels)
        self.q = nn.Conv2d(in_channels, in_channels, 1)
        self.k = nn.Conv2d(in_channels, in_channels, 1)
        self.v = nn.Conv2d(in_channels, in_channels, 1)
        self.proj_out = nn.Conv2d(in_channels, in_channels, 1)

    def forward(self, x):
        h = self.norm(x)
        q = rearrange(self.q(h), "b c h w -> b 1 (h w) c")
        k = rearrange(self.k(h), "b c h w -> b 1 (h w) c")
        v = rearrange(self.v(h), "b c h w -> b 1 (h w) c")
        h = F.scaled_dot_product_attention(q, k, v)
        h = rearrange(h, "b 1 (h w) c -> b c h w", h=x.shape[-2], w=x.shape[-1])
        return x + self.proj_out(h)


class ResBlockEmb(nn.Module):
    def __init__(self, channels, emb_channels, dropout=0, out_channels=None):
        super().__init__()
        self.out_channels = out_channels or channels
        self.in_layers = nn.Sequential(
            normalization(channels), nn.SiLU(),
            nn.Conv2d(channels, self.out_channels, 3, padding=1),
        )
        self.emb_layers = nn.Sequential(
            nn.SiLU(), nn.Linear(emb_channels, 2 * self.out_channels),
        )
        self.out_norm = normalization(self.out_channels)
        self.out_layers = nn.Sequential(
            nn.SiLU(), nn.Dropout(p=dropout),
            zero_module(nn.Conv2d(self.out_channels, self.out_channels, 3, padding=1)),
        )
        self.skip = (
            nn.Conv2d(channels, self.out_channels, 1)
            if self.out_channels != channels else nn.Identity()
        )

    def forward(self, x, emb):
        h = self.in_layers(x)
        emb_out = self.emb_layers(emb).type(h.dtype)
        while len(emb_out.shape) < len(h.shape):
            emb_out = emb_out[..., None]
        scale, shift = torch.chunk(emb_out, 2, dim=1)
        h = self.out_norm(h) * (1 + scale) + shift
        h = self.out_layers(h)
        return self.skip(x) + h


class TemporalConv(nn.Module):
    def __init__(self, channels, kernel_size=5):
        super().__init__()
        padding = kernel_size // 2
        self.norm = normalization(channels)
        self.dwconv = nn.Conv3d(channels, channels,
                                kernel_size=(kernel_size, 1, 1),
                                padding=(padding, 0, 0),
                                groups=channels)
        self.pwconv = nn.Conv3d(channels, channels, kernel_size=1)
        nn.init.zeros_(self.pwconv.weight)
        nn.init.zeros_(self.pwconv.bias)

    def forward(self, x):
        identity = x
        B, C, T, H, W = x.shape
        h = rearrange(x, "b c t h w -> (b t) c h w")
        h = self.norm(h)
        h = rearrange(h, "(b t) c h w -> b c t h w", b=B, t=T)
        h = F.silu(h)
        h = self.dwconv(h)
        h = self.pwconv(h)
        return identity + h


# ↑ upstream/nodes/minimax_h3_latent_upscaler_2d.py:137（同步于 d7c01b9）
class LatentResizer(nn.Module):
    """2D 主干（与训练代码完全一致）。"""

    def __init__(self, in_channels=24, in_blocks=12, out_blocks=12,
                 channels=640, dropout=0.1, attn=False):
        super().__init__()
        self.conv_in = nn.Conv2d(in_channels, channels, 3, padding=1)
        embed_dim = 64
        self.embed = nn.Sequential(
            nn.Linear(1, embed_dim), nn.SiLU(), nn.Linear(embed_dim, embed_dim))
        self.in_blocks = nn.ModuleList()
        for b in range(in_blocks):
            if (b == 1 or b == in_blocks - 1) and attn:
                self.in_blocks.append(AttnBlock(channels))
            self.in_blocks.append(ResBlockEmb(channels, embed_dim, dropout))
        self.out_blocks = nn.ModuleList()
        for b in range(out_blocks):
            if (b == 1 or b == out_blocks - 1) and attn:
                self.out_blocks.append(AttnBlock(channels))
            self.out_blocks.append(ResBlockEmb(channels, embed_dim, dropout))
        self.norm_out = normalization(channels)
        self.conv_out = nn.Conv2d(channels, in_channels, 3, padding=1)

    def forward(self, x, scale=None, target_hw=None):
        if target_hw is not None:
            size = target_hw
        elif scale is not None:
            size = tuple(int(round(s * scale)) for s in x.shape[-2:])
        else:
            return x
        if size == x.shape[-2:]:
            return x
        scale_emb = torch.tensor(
            [scale - 1 if scale is not None else 0.0],
            dtype=x.dtype, device=x.device).unsqueeze(0)
        emb = self.embed(scale_emb)
        x = self.conv_in(x)
        for b in self.in_blocks:
            if isinstance(b, ResBlockEmb):
                x = b(x, emb)
            else:
                x = b(x)
        x = F.interpolate(x, size=size, mode="bilinear")
        for b in self.out_blocks:
            if isinstance(b, ResBlockEmb):
                x = b(x, emb)
            else:
                x = b(x)
        x = self.norm_out(x)
        x = F.silu(x)
        x = self.conv_out(x)
        return x


# ↑ upstream/nodes/minimax_h3_latent_upscaler_2d.py:191（同步于 d7c01b9）
class VideoLatentResizer(nn.Module):
    """5D 包装器（含 Temporal 块），与训练代码完全一致。"""

    def __init__(self, in_channels=24, in_blocks=12, out_blocks=12,
                 channels=640, dropout=0.1, attn=False,
                 temporal_every=2, temporal_kernel=5):
        super().__init__()
        self.resizer = LatentResizer(
            in_channels=in_channels,
            in_blocks=in_blocks,
            out_blocks=out_blocks,
            channels=channels,
            dropout=dropout,
            attn=attn,
        )
        self.temporal_blocks = nn.ModuleList()
        if temporal_every > 0:
            self.temporal_blocks.append(TemporalConv(channels, temporal_kernel))
            self.temporal_blocks.append(TemporalConv(channels, temporal_kernel))
        self.temporal_every = temporal_every
        self.temporal_kernel = temporal_kernel

    def forward(self, x, scale=None, target_hw=None):
        B, C, T, H, W = x.shape
        if target_hw is not None:
            size = target_hw
        elif scale is not None:
            size = (int(round(H * scale)), int(round(W * scale)))
        else:
            size = (H, W)
        if len(self.temporal_blocks) == 0:
            x_flat = rearrange(x, "b c t h w -> (b t) c h w")
            out = self.resizer(x_flat, scale=scale, target_hw=size)
            return rearrange(out, "(b t) c h w -> b c t h w", b=B, t=T)
        x_flat = rearrange(x, "b c t h w -> (b t) c h w")
        emb = torch.tensor(
            [scale - 1 if scale is not None else 0.0],
            dtype=x.dtype, device=x.device).unsqueeze(0)
        emb = self.resizer.embed(emb)
        out = self.resizer.conv_in(x_flat)
        for i, block in enumerate(self.resizer.in_blocks):
            if isinstance(block, ResBlockEmb):
                emb_t = emb.expand(B * T, -1)
                out = block(out, emb_t)
            else:
                out = block(out)
            if i % self.temporal_every == 0:
                out_3d = rearrange(out, "(b t) c h w -> b c t h w", b=B, t=T)
                out_3d = self.temporal_blocks[0](out_3d)
                out = rearrange(out_3d, "b c t h w -> (b t) c h w")
        out = F.interpolate(out, size=size, mode="bilinear")
        for i, block in enumerate(self.resizer.out_blocks):
            if isinstance(block, ResBlockEmb):
                emb_t = emb.expand(B * T, -1)
                out = block(out, emb_t)
            else:
                out = block(out)
            if i % self.temporal_every == 0:
                out_3d = rearrange(out, "(b t) c h w -> b c t h w", b=B, t=T)
                out_3d = self.temporal_blocks[1](out_3d)
                out = rearrange(out_3d, "b c t h w -> (b t) c h w")
        out = self.resizer.norm_out(out)
        out = F.silu(out)
        out = self.resizer.conv_out(out)
        out = rearrange(out, "(b t) c h w -> b c t h w", b=B, t=T)
        return out


# ---- 纯 3D 主干组件（channels=512，trilinear） ----

class AttnBlock3D(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.norm = normalization(in_channels)
        self.q = nn.Conv3d(in_channels, in_channels, 1)
        self.k = nn.Conv3d(in_channels, in_channels, 1)
        self.v = nn.Conv3d(in_channels, in_channels, 1)
        self.proj_out = nn.Conv3d(in_channels, in_channels, 1)

    def forward(self, x):
        h = self.norm(x)
        q = rearrange(self.q(h), "b c t h w -> b 1 (t h w) c")
        k = rearrange(self.k(h), "b c t h w -> b 1 (t h w) c")
        v = rearrange(self.v(h), "b c t h w -> b 1 (t h w) c")
        h = F.scaled_dot_product_attention(q, k, v)
        h = rearrange(h, "b 1 (t h w) c -> b c t h w", t=x.shape[2], h=x.shape[3], w=x.shape[4])
        return x + self.proj_out(h)


class ResBlockEmb3D(nn.Module):
    def __init__(self, channels, emb_channels, dropout=0, out_channels=None):
        super().__init__()
        self.out_channels = out_channels or channels
        self.in_layers = nn.Sequential(
            normalization(channels), nn.SiLU(),
            nn.Conv3d(channels, self.out_channels, 3, padding=1),
        )
        self.emb_layers = nn.Sequential(
            nn.SiLU(), nn.Linear(emb_channels, 2 * self.out_channels),
        )
        self.out_norm = normalization(self.out_channels)
        self.out_layers = nn.Sequential(
            nn.SiLU(), nn.Dropout(p=dropout),
            zero_module(nn.Conv3d(self.out_channels, self.out_channels, 3, padding=1)),
        )
        self.skip = (
            nn.Conv3d(channels, self.out_channels, 1)
            if self.out_channels != channels else nn.Identity()
        )

    def forward(self, x, emb):
        h = self.in_layers(x)
        emb_out = self.emb_layers(emb).type(h.dtype)
        while len(emb_out.shape) < len(h.shape):
            emb_out = emb_out[..., None]
        scale, shift = torch.chunk(emb_out, 2, dim=1)
        h = self.out_norm(h) * (1 + scale) + shift
        h = self.out_layers(h)
        return self.skip(x) + h


class TemporalConv3D(nn.Module):
    """纯 3D 主干里的时序块（上游 3D 文件版本：norm 直接吃 5D）。"""

    def __init__(self, channels, kernel_size=5):
        super().__init__()
        padding = kernel_size // 2
        self.norm = normalization(channels)
        self.dwconv = nn.Conv3d(channels, channels,
                                kernel_size=(kernel_size, 1, 1),
                                padding=(padding, 0, 0),
                                groups=channels)
        self.pwconv = nn.Conv3d(channels, channels, kernel_size=1)
        nn.init.zeros_(self.pwconv.weight)
        nn.init.zeros_(self.pwconv.bias)

    def forward(self, x):
        identity = x
        h = self.norm(x)
        h = F.silu(h)
        h = self.dwconv(h)
        h = self.pwconv(h)
        return identity + h


# ↑ upstream/nodes/minimax_h3_latent_upscaler_3d.py:215（同步于 d7c01b9）
class LatentResizer3D(nn.Module):
    """纯 3D 主干（与训练代码一致）。"""

    def __init__(self, in_channels=24, in_blocks=12, out_blocks=12,
                 channels=512, dropout=0.1, attn=False,
                 temporal_every=2, temporal_kernel=5):
        super().__init__()
        self.conv_in = nn.Conv3d(in_channels, channels, 3, padding=1)
        embed_dim = 64
        self.embed = nn.Sequential(
            nn.Linear(1, embed_dim), nn.SiLU(), nn.Linear(embed_dim, embed_dim))
        self.in_blocks = nn.ModuleList()
        for b in range(in_blocks):
            if (b == 1 or b == in_blocks - 1) and attn:
                self.in_blocks.append(AttnBlock3D(channels))
            self.in_blocks.append(ResBlockEmb3D(channels, embed_dim, dropout))
            if temporal_every > 0 and b % temporal_every == 0:
                self.in_blocks.append(TemporalConv3D(channels, temporal_kernel))
        self.out_blocks = nn.ModuleList()
        for b in range(out_blocks):
            if (b == 1 or b == out_blocks - 1) and attn:
                self.out_blocks.append(AttnBlock3D(channels))
            self.out_blocks.append(ResBlockEmb3D(channels, embed_dim, dropout))
            if temporal_every > 0 and b % temporal_every == 0:
                self.out_blocks.append(TemporalConv3D(channels, temporal_kernel))
        self.norm_out = normalization(channels)
        self.conv_out = nn.Conv3d(channels, in_channels, 3, padding=1)

    def forward(self, x, scale=None, target_size=None):
        if target_size is not None:
            size = target_size
        elif scale is not None:
            size = tuple(int(round(s * scale)) for s in x.shape[-3:])
        else:
            return x
        if size == x.shape[-3:]:
            return x
        scale_emb = torch.tensor(
            [scale - 1 if scale is not None else 0.0],
            dtype=x.dtype, device=x.device).unsqueeze(0)
        emb = self.embed(scale_emb)
        x = self.conv_in(x)
        for b in self.in_blocks:
            if isinstance(b, ResBlockEmb3D):
                emb_t = emb.expand(x.shape[0], -1)
                x = b(x, emb_t)
            else:
                x = b(x)
        x = F.interpolate(x, size=size, mode="trilinear", align_corners=False)
        for b in self.out_blocks:
            if isinstance(b, ResBlockEmb3D):
                emb_t = emb.expand(x.shape[0], -1)
                x = b(x, emb_t)
            else:
                x = b(x)
        x = self.norm_out(x)
        x = F.silu(x)
        x = self.conv_out(x)
        return x


# ---- 模型目录与权重加载 ----

MODEL_CACHE = {}
_SUFFIXES = (".pth", ".safetensors")


def register_folder():
    """注册 latent_upscale_models 模型目录（幂等；无 folder_paths 时跳过）。"""
    global _FOLDER_REGISTERED
    if _FOLDER_REGISTERED:
        return
    try:
        import folder_paths
        if LATENT_UPSCALE_FOLDER not in folder_paths.folder_names_and_paths:
            folder_paths.add_model_folder_path(
                LATENT_UPSCALE_FOLDER,
                os.path.join(folder_paths.models_dir, LATENT_UPSCALE_FOLDER))
        _FOLDER_REGISTERED = True
    except Exception:
        pass


def get_models_dir():
    register_folder()
    import folder_paths
    return folder_paths.get_folder_paths(LATENT_UPSCALE_FOLDER)[0]


_MODEL_DIRS_OVERRIDE = None   # 离线/单测注入的目录（None=走 folder_paths 口径）


def set_model_dirs(dirs):
    """临时覆盖模型根目录（离线单测 / 离线工具用；传 None 恢复 folder_paths 口径）。"""
    global _MODEL_DIRS_OVERRIDE
    _MODEL_DIRS_OVERRIDE = [str(d) for d in dirs] if dirs else None


def _model_dirs():
    """全部已注册的模型根目录（无 ComfyUI 时退化成单个目录）。"""
    if _MODEL_DIRS_OVERRIDE:
        return list(_MODEL_DIRS_OVERRIDE)
    try:
        register_folder()
        import folder_paths
        return list(folder_paths.get_folder_paths(LATENT_UPSCALE_FOLDER))
    except Exception:
        try:
            return [get_models_dir()]
        except Exception:
            return []


def scan_models():
    """模型目录里的权重（递归子目录，返回目录内相对路径，排序后）。

    优先走 folder_paths.get_filename_list（ComfyUI 自带递归扫描 + 缓存）；
    无 ComfyUI 环境（离线单测）或列表为空（缓存未刷新）时回退 glob 递归。
    统一把分隔符规范成 "/"，存档里存的名字跨平台可读——旧存档存的纯文件名
    （无子目录）天然兼容。空目录返回空列表（占位提示由前端负责）。
    """
    names = []
    try:
        register_folder()
        import folder_paths
        names = list(folder_paths.get_filename_list(LATENT_UPSCALE_FOLDER) or [])
    except Exception:
        names = []
    if not names:
        model_dir = None
        for d in _model_dirs():
            model_dir = d
            break
        if not model_dir:
            return []
        for ext in _SUFFIXES:
            for p in glob.glob(os.path.join(model_dir, "**", "*" + ext),
                               recursive=True):
                names.append(os.path.relpath(p, model_dir))
    return sorted(n.replace(os.sep, "/") for n in names
                  if os.path.splitext(n)[1].lower() in _SUFFIXES)


def resolve_model_path(name):
    """模型名（可含子目录相对路径）-> 绝对路径；找不到抛 FileNotFoundError。

    优先 folder_paths.get_full_path_or_raise：路径穿越防护由 ComfyUI 保证
    （内部先按 relpath 归一化再拼目录）。无 ComfyUI（离线单测）或旧版
    ComfyUI（无该 API）时本地解析，并显式校验结果落在模型目录内（防 ../）。
    """
    register_folder()
    try:
        import folder_paths
        getter = getattr(folder_paths, "get_full_path_or_raise", None)
        if getter is not None:
            try:
                return getter(LATENT_UPSCALE_FOLDER, name)
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"模型文件不存在: {name}（已搜索目录：{_model_dirs()}）") from None
    except ImportError:
        pass
    norm = os.path.normpath(str(name).replace("/", os.sep).lstrip(os.sep))
    for d in _model_dirs():
        root = os.path.abspath(d)
        p = os.path.abspath(os.path.join(root, norm))
        if os.path.commonpath([root, p]) == root and os.path.isfile(p):
            return p
    raise FileNotFoundError(f"模型文件不存在: {name}（已搜索目录：{_model_dirs()}）")


def _load_raw_sd(path):
    if path.endswith(".safetensors"):
        from safetensors.torch import load_file
        sd = load_file(path, device="cpu")
    else:
        sd = torch.load(path, map_location="cpu", weights_only=False)
    if isinstance(sd, dict) and "model" in sd:
        sd = sd["model"]
    # FP8 权重转 FP16 方便处理（上游同款）
    return {k: v.to(torch.float16) if v.dtype == torch.float8_e4m3fn else v
            for k, v in sd.items()}


# 第三方 / 自训练权重的常见包装前缀（上游只剥 upscaler.，这里做防御性扩展）
_SD_PREFIXES = ("model.", "module.", "net.", "state_dict.", "upscaler.")
# 骨干键标识：剥到出现这些键就停
_SD_MARKERS = ("conv_in.weight", "in_blocks.", "resizer.")


def _extract_upscaler_sd(sd):
    """循环剥离包装前缀（model./module./net./state_dict./upscaler.），直到出现骨干键。

    上游只剥 upscaler.（其官方合并权重的前缀）；DDP 训练存 module.、
    Lightning 存 state_dict.、自训练脚本存 model./net.——循环剥离这些前缀
    最多 5 层，命中哪个剥哪个、只剥带前缀的键（其余键原样保留，不全量丢弃
    防误伤）。剥到出现 conv_in.weight / in_blocks. / resizer. 即停。
    """
    for _ in range(len(_SD_PREFIXES)):
        if any(k.startswith(_SD_MARKERS) for k in sd):
            break
        for p in _SD_PREFIXES:
            if any(k.startswith(p) for k in sd):
                sd = {k[len(p):] if k.startswith(p) else k: v
                      for k, v in sd.items()}
                break
        else:
            break        # 一个前缀都没命中——再剥也没用
    return sd


def _detect_kind(sd, arch):
    """state_dict + 面板架构 -> "2D" / "3D" / None（None=判不出，交给试装择优）。

    判定顺序（面板显式指定最优先，其次两条廉价特征，都判不出才 None）：
    ① 面板 arch ∈ {2D, 3D} → 直接采用（用户显式指定，不猜）；
    ② 有 resizer. 前缀键 → 2D（上游 2D 权重是 VideoLatentResizer 的包装键）；
    ③ conv_in.weight（剥离前缀后的键名）的 ndim：5 → 3D（Conv3d）、
       4 → 2D（Conv2d，能救不带 resizer. 前缀的裸 2D 骨干）；
    ④ 都没有 → None。
    """
    a = str(arch or "").strip().upper()
    if a in ("2D", "3D"):
        return a
    if any(k.startswith("resizer.") for k in sd):
        return "2D"
    w = sd.get("conv_in.weight")
    if w is not None and hasattr(w, "dim"):
        if w.dim() == 5:
            return "3D"
        if w.dim() == 4:
            return "2D"
    return None


def _normalize_kind_sd(sd, kind):
    """把裸 2D 骨干权重补齐 resizer. 前缀，使其能装进 VideoLatentResizer。

    2D 网络本体是 VideoLatentResizer（内部 resizer=LatentResizer + 两块
    TemporalConv），官方权重带 resizer. 前缀；裸骨干（LatentResizer 直接
    state_dict）需要补前缀才能装上。temporal_blocks.* 本就在顶层，不补。
    """
    if kind != "2D" or any(k.startswith("resizer.") for k in sd):
        return sd
    return {(k if k.startswith("temporal_blocks.") else f"resizer.{k}"): v
            for k, v in sd.items()}


# ↑ upstream/nodes/minimax_h3_latent_upscaler_2d.py:298
#   upstream/nodes/minimax_h3_latent_upscaler_3d.py:379（同步于 d7c01b9）
#   本地合并版：按 arch 参数同时覆盖 2D/3D 两套键约定（上游是两个独立文件）
def _detect_arch(sd, arch):
    """从 state_dict 推断网络结构（与上游训练配置一致；attn 推理强制关闭）。"""
    if arch == "3D":
        cfg = {"in_channels": 24, "in_blocks": 12, "out_blocks": 12,
               "channels": 512, "dropout": 0.1, "attn": False,
               "temporal_every": 2, "temporal_kernel": 5}
        conv_key = "conv_in.weight"
        if conv_key in sd:
            cfg["in_channels"] = sd[conv_key].shape[1]
            cfg["channels"] = sd[conv_key].shape[0]
        in_ids, out_ids = set(), set()
        temporal_in, temporal_out = set(), set()
        for k in sd.keys():
            m = re.match(r"in_blocks\.(\d+)\.in_layers\.", k)
            if m:
                in_ids.add(int(m.group(1)))
            m = re.match(r"out_blocks\.(\d+)\.in_layers\.", k)
            if m:
                out_ids.add(int(m.group(1)))
            m = re.match(r"in_blocks\.(\d+)\.dwconv\.weight", k)
            if m:
                temporal_in.add(int(m.group(1)))
            m = re.match(r"out_blocks\.(\d+)\.dwconv\.weight", k)
            if m:
                temporal_out.add(int(m.group(1)))
        if in_ids:
            cfg["in_blocks"] = len(in_ids)
        if out_ids:
            cfg["out_blocks"] = len(out_ids)
        if temporal_in or temporal_out:
            cfg["temporal_every"] = 2
            for k in sd.keys():
                if k.endswith("dwconv.weight"):
                    cfg["temporal_kernel"] = sd[k].shape[2]
                    break
        else:
            cfg["temporal_every"] = 0
        cfg["attn"] = False   # 推理强制关闭（上游同款）
        return cfg
    # 2D：键（通常）带 resizer. 前缀；正则统一加可选前缀，裸骨干权重也能读
    cfg = {"in_channels": 24, "in_blocks": 12, "out_blocks": 12,
           "channels": 640, "dropout": 0.1, "attn": False,
           "temporal_every": 2, "temporal_kernel": 5}
    for ck in ("resizer.conv_in.weight", "conv_in.weight"):
        if ck in sd:
            w = sd[ck]
            cfg["in_channels"] = w.shape[1]
            cfg["channels"] = w.shape[0]
            break
    in_ids, out_ids = set(), set()
    for k in sd.keys():
        m = re.match(r"(?:resizer\.)?in_blocks\.(\d+)\.in_layers\.", k)
        if m:
            in_ids.add(int(m.group(1)))
        m = re.match(r"(?:resizer\.)?out_blocks\.(\d+)\.in_layers\.", k)
        if m:
            out_ids.add(int(m.group(1)))
    if in_ids:
        cfg["in_blocks"] = len(in_ids)
    if out_ids:
        cfg["out_blocks"] = len(out_ids)
    if any("temporal_blocks" in k for k in sd):
        for k in sd.keys():
            if "temporal_blocks.0.dwconv.weight" in k:
                cfg["temporal_kernel"] = sd[k].shape[2]
                break
        cfg["temporal_every"] = 2
    else:
        cfg["temporal_every"] = 0
    cfg["attn"] = False
    return cfg


_MISMATCH = 1 << 30      # 试装择优里「装不上」的比分（形状不符）


def _is_attn_key(k):
    """attn 相关键（推理强制 attn=False → 这些键缺失属正常，不算结构不匹配）。"""
    return ".q." in k or ".k." in k or ".v." in k or "proj_out" in k


def _build_model(kind, cfg):
    """按判定出的架构构造网络（两种骨干参数名不同，构造与装权必须同 kind）。"""
    if kind == "3D":
        return LatentResizer3D(
            in_channels=cfg["in_channels"], in_blocks=cfg["in_blocks"],
            out_blocks=cfg["out_blocks"], channels=cfg["channels"],
            dropout=cfg["dropout"], attn=cfg["attn"],
            temporal_every=cfg["temporal_every"],
            temporal_kernel=cfg["temporal_kernel"])
    return VideoLatentResizer(
        in_channels=cfg["in_channels"], in_blocks=cfg["in_blocks"],
        out_blocks=cfg["out_blocks"], channels=cfg["channels"],
        dropout=cfg["dropout"], attn=cfg["attn"],
        temporal_every=cfg["temporal_every"],
        temporal_kernel=cfg["temporal_kernel"])


def kind_of(model):
    """网络对象 -> "2D" / "3D"（upscale.py 据此决定 forward 的调用口径）。"""
    return "3D" if isinstance(model, LatentResizer3D) else "2D"


# ↑ upstream/nodes/minimax_h3_latent_upscaler_2d.py:358
#   upstream/nodes/minimax_h3_latent_upscaler_3d.py:417（同步于 d7c01b9）
def load_model(name, device, precision, arch="auto"):
    """按 名字::架构::设备::精度 缓存加载放大网络（eval 模式）。

    arch：面板值，"2D" / "3D" / "auto"（默认）。auto 时按权重自动判定：
    resizer. 前缀 → 2D；conv_in.weight 是 5 维 → 3D、4 维 → 2D；三条都判不出
    时走「试装择优」——按 2D/3D 各构造一次、strict=False 试装，取缺键最少的
    一种（命中即停，最多构造两次）。面板显式指定时不猜：装不上直接报可操作
    的错误（提示改回「自动」）。缓存键用解析后的架构（auto 与显式同架构
    共享一份缓存）。
    """
    path = resolve_model_path(name)
    cache_key_probe = f"{name}::{arch}::{device}::{precision}"
    if cache_key_probe in MODEL_CACHE:
        return MODEL_CACHE[cache_key_probe]
    raw_sd = _load_raw_sd(path)
    up_sd = _extract_upscaler_sd(raw_sd)
    kind = _detect_kind(up_sd, arch)
    # 只解出一种 → 直接装；判不出 → 2D/3D 依次试装择优（先试 2D：上游默认架构）
    candidates = [kind] if kind in ("2D", "3D") else ["2D", "3D"]
    best = None
    for cand in candidates:
        sd = _normalize_kind_sd(up_sd, cand)
        cfg = _detect_arch(sd, cand)
        if cfg["in_channels"] != 24 and any(k.endswith("conv_in.weight") for k in sd):
            raise ValueError(
                f"放大权重输入通道为 {cfg['in_channels']}——本插件只支持 H3 的 "
                f"24 通道 latent 放大模型：{name}")
        model = _build_model(cand, cfg)
        try:
            missing, unexpected = model.load_state_dict(sd, strict=False)
            hard = [m for m in missing if not _is_attn_key(m)]
            score = (len(hard), len(unexpected))
        except (RuntimeError, KeyError) as e:
            # strict=False 只容忍缺键/多键，不容忍形状不符——形状不符=架构真装错了
            if len(candidates) == 1:
                raise ValueError(
                    f"放大权重与网络架构不匹配：{name} 按「{cand}」装不上（权重形状与"
                    f"网络不符）——请把「网络架构」设为「自动」重试，或确认该权重就是"
                    f"{cand} 架构的 H3 latent 放大模型。原始错误：{e}") from None
            model, missing, unexpected, hard = None, [], [], []
            score = (_MISMATCH, _MISMATCH)
        if best is None or score < best[0]:
            best = (score, cand, model, cfg, missing, unexpected, hard)
        if score == (0, 0):
            break
    score, kind, model, cfg, missing, unexpected, hard = best
    if model is None:
        raise ValueError(
            f"放大权重既装不进 2D 也装不进 3D 网络：{name}——请确认这是 H3 的 "
            f"24 通道 latent 放大模型（LBH-123-AI/Minimax_h3_latent_Upscaler）")
    if len(candidates) == 1 and hard:
        raise ValueError(
            f"放大权重与网络架构不匹配：{name} 按「{kind}」装不上，缺 {len(hard)} 个"
            f"必需键（{'、'.join(hard[:5])}…）——请把「网络架构」设为「自动」重试，"
            f"或确认该权重就是 {kind} 架构的 H3 latent 放大模型")
    if missing:
        print(f"[H3二采] 放大权重缺少键（attn 推理强制关闭属正常）: {missing[:5]}…")
    if unexpected:
        print(f"[H3二采] 放大权重多余键（可能来自合并文件）: {unexpected[:5]}…")
    dtype = {"fp32": torch.float32, "fp16": torch.float16,
             "bf16": torch.bfloat16}.get(precision, torch.float32)
    model = model.to(device).eval()
    if dtype != torch.float32:
        model = model.to(dtype)
    MODEL_CACHE[cache_key_probe] = model
    MODEL_CACHE[f"{name}::{kind}::{device}::{precision}"] = model
    print(f"[H3二采] 加载放大模型（{kind}"
          f"{'' if kind == arch else f'，面板 {arch}'}）: {name} | 参数量 "
          f"{sum(p.numel() for p in model.parameters()):,} | 设备 {device} | Temporal "
          f"{'开' if cfg['temporal_every'] > 0 else '关'}"
          f"(every={cfg['temporal_every']}, kernel={cfg['temporal_kernel']})", flush=True)
    return model
