"""MiniMax H3 Enhance-A-Video / FETA —— SequenceForge 的独立子模块。

原理
----
Enhance-A-Video（arXiv:2502.07508v3）的 FETA：在每层注意力的输出上乘一个全局标量 g。
g 由「跨帧注意力强度」CFI 决定 —— 把 target-video 的 Q/K 按 (帧, 空间位置) 重组成
T×T 的小矩阵，抹掉对角线后取均值：

    CFI = (frames - trace) / (frames * (frames - 1))      trace = 对角元素之和
    g   = max((frames + tau) * CFI, 1)

只对 packed 序列里的 target-video 段生效；text / cond / ref / audio 行原样不动。

本模块**不依赖本项目的其它模块**（只用 torch + comfy_api + 标准库），
也不修改任何既有文件；从 __init__.py 注册两个节点即可，移除时删掉本文件与那两行注册。

与 T8mars/comfyui-minimax-h3-audio-T8 的 H3 适配（Apache-2.0）的三点差异
--------------------------------------------------------------------
1. 不需要 sigmas 输入 —— 进度窗口直接读采样器写入的 transformer_options["sigmas"]。
2. 不做任务白名单 —— 不检查 keyframe 位置 / refs / denoise mask，只认 video 段。
3. 走 ComfyUI 官方的 optimized_attention_override 接入，因此可与任何最终调用
   optimized_attention 的注意力实现共存（例如 KJ 的 H3 省显存 Attention）。

参考实现（Apache-2.0）：NUS-HPC-AI-Lab/Enhance-A-Video
"""

from __future__ import annotations

import json
import logging
import math
import threading

import torch

from comfy_api.latest import io, ui

LOG = logging.getLogger("H3-EAV-FETA")

# 节点用常量
MODES = ("关闭", "仅报告", "应用")
MODE_DISABLED, MODE_REPORT, MODE_APPLY = MODES

_ROUTE_KEY = "h3_eav_feta_route"
_OVERRIDE_FLAG = "_h3_eav_feta_override"
_PATCH_VERSION = 1

# H3 的 2x2 patchify：每个 latent 帧的空间 token 数 = ceil(h/2) * ceil(w/2)
_PATCH_H = 2
_PATCH_W = 2


# --------------------------------------------------------------------------
# 运行期遥测（模块级单例，patch 节点写入、报告节点读取）
# --------------------------------------------------------------------------
def _log_forward(acc):
    """每个模型前向结束时打一行 —— 不接报告节点也能看到 CFI/g。"""
    if not acc or acc.get("logged"):
        return
    acc["logged"] = True
    parts = [f"前向 #{acc['index']}"]
    if acc["progress"] is not None:
        parts.append(f"进度={acc['progress']:.3f}")
    if acc["active"]:
        parts.append(f"测量块={acc['measured']}")
        if acc["cfi_n"]:
            parts.append(f"CFI={acc['cfi_min']:.6f}~{acc['cfi_max']:.6f}"
                         f"(均{acc['cfi_sum'] / acc['cfi_n']:.6f})")
        if acc["g_n"]:
            parts.append(f"g={acc['g_min']:.6f}~{acc['g_max']:.6f}"
                         f"(均{acc['g_sum'] / acc['g_n']:.6f})")
        else:
            parts.append("g 恒为 1（无放大）")
        if acc["overflows"]:
            parts.append(f"触及增益上限 {acc['overflows']} 次")
    else:
        parts.append("窗口外（未测量、未介入）")
    LOG.info("[H3-EAV-FETA] " + "，".join(parts))


def _log_summary(report):
    cfi = report["cfi"]
    g = report["g"]
    parts = [
        f"模式={report['config']['mode']}",
        f"模型前向={report['model_forwards']}",
        f"激活={report['active_forwards']}",
    ]
    if cfi:
        parts.append(f"CFI 均值={cfi['mean']}（{cfi['min']}~{cfi['max']}）")
    if g:
        parts.append(f"g 均值={g['mean']}（{g['min']}~{g['max']}）")
    else:
        parts.append("g 恒为 1（未产生放大）")
    if report["overflow_count"]:
        parts.append(f"触及增益上限 {report['overflow_count']} 次")
    LOG.info("[H3-EAV-FETA] " + "，".join(parts))


class _Run:
    def __init__(self):
        self.lock = threading.Lock()
        self.config = None
        self.forwards = []
        self.overflows = 0
        self._cur = None
        self._forward_no = 0
        self.blocks_per_forward = 0

    def reset(self, config=None, blocks_per_forward=0):
        with self.lock:
            _log_forward(self._cur)
            self.config = dict(config) if config else None
            self.blocks_per_forward = int(blocks_per_forward or 0)
            self.forwards = []
            self.overflows = 0
            self._cur = None
            self._forward_no = 0

    def begin_forward(self, *, progress, active, frames, spatial_tokens, seq_len, video_rows):
        """一次模型前向只调用一次（路由缓存建立时），顺便结掉上一个前向的日志。"""
        with self.lock:
            _log_forward(self._cur)
            self._forward_no += 1
            entry = {
                "index": self._forward_no,
                "progress": float(progress) if progress is not None else None,
                "active": bool(active),
                "frames": int(frames),
                "spatial_tokens": int(spatial_tokens),
                "seq_len": int(seq_len),
                "video_rows": int(video_rows),
                "measured": 0, "overflows": 0, "logged": False,
                "cfi_n": 0, "cfi_sum": 0.0, "cfi_min": math.inf, "cfi_max": -math.inf,
                "g_n": 0, "g_sum": 0.0, "g_min": math.inf, "g_max": -math.inf,
                "chunk_rows": None, "workspace_bytes": None,
            }
            self._cur = entry
            self.forwards.append(entry)

    def record(self, *, cfi=None, g=None, chunk_rows=None, workspace=None):
        with self.lock:
            cur = self._cur
            if cur is None:
                return
            cur["measured"] += 1
            if cfi is not None:
                cur["cfi_n"] += 1
                cur["cfi_sum"] += float(cfi)
                cur["cfi_min"] = min(cur["cfi_min"], float(cfi))
                cur["cfi_max"] = max(cur["cfi_max"], float(cfi))
            if g is not None:
                cur["g_n"] += 1
                cur["g_sum"] += float(g)
                cur["g_min"] = min(cur["g_min"], float(g))
                cur["g_max"] = max(cur["g_max"], float(g))
            if chunk_rows is not None:
                cur["chunk_rows"] = int(chunk_rows)
            if workspace is not None:
                cur["workspace_bytes"] = int(workspace)
            # 主块数已知时，数满即结账 —— 这样最后一次前向也能打出来，
            # 不会因为「等下一个前向开始」而丢掉。
            if self.blocks_per_forward and cur["measured"] >= self.blocks_per_forward:
                _log_forward(cur)

    def note_overflow(self):
        with self.lock:
            self.overflows += 1
            if self._cur is not None:
                self._cur["overflows"] += 1

    def report(self):
        with self.lock:
            forwards = [dict(f) for f in self.forwards]
            config = dict(self.config) if self.config else None
            overflows = self.overflows

        measured = [f for f in forwards if f["cfi_n"]]
        active = [f for f in measured if f["active"]]
        workspaces = [f["workspace_bytes"] for f in measured if f["workspace_bytes"]]

        def _agg(count_key, sum_key, min_key, max_key):
            sel = [f for f in forwards if f[count_key]]
            total = sum(f[count_key] for f in sel)
            if not sel or total <= 0:
                return None
            return {
                "n": total,
                "min": round(min(f[min_key] for f in sel), 6),
                "max": round(max(f[max_key] for f in sel), 6),
                "mean": round(sum(f[sum_key] for f in sel) / total, 6),
            }

        return {
            "patch_version": _PATCH_VERSION,
            "config": config,
            "model_forwards": len(forwards),
            "measured_forwards": len(measured),
            "active_forwards": len(active),
            "measured_blocks": sum(f["measured"] for f in forwards),
            "overflow_count": overflows,
            "cfi": _agg("cfi_n", "cfi_sum", "cfi_min", "cfi_max"),
            "g": _agg("g_n", "g_sum", "g_min", "g_max"),
            "workspace_bytes_max": max(workspaces) if workspaces else None,
            "note": ("仅报告模式不改变注意力输出；CFI 越低说明该层跨帧关联越弱。"
                     "g 恒等于 1 表示这些层本来就没有被放大。"),
            "forwards": forwards,
        }


_RUN = _Run()


def _reset_run(config, blocks_per_forward=0):
    _RUN.reset(config, blocks_per_forward)


# --------------------------------------------------------------------------
# 核心数学：分块 CFI
# --------------------------------------------------------------------------
def _chunked_cfi(q, k, frames, spatial_tokens, max_workspace_mib):
    """精确计算论文 CFI，按空间位置分块以限制峰值显存。

    q / k: [1, heads, frames * spatial_tokens, head_dim]，要求行序为「帧优先」。
    返回 (cfi, chunk_rows, 估算工作区字节数)，任何形状不符都返回 None。
    """
    if q.ndim != 4 or k.ndim != 4 or q.shape != k.shape:
        return None
    batch, heads, rows, head_dim = q.shape
    if batch != 1 or heads < 1 or head_dim < 1:
        return None
    if frames < 2 or rows != int(frames) * int(spatial_tokens):
        return None

    # 存活张量：原始 logits + 它的 fp32 副本 + softmax 结果
    bytes_per_spatial = int(heads) * int(frames) * int(frames) * 12
    budget = max(int(max_workspace_mib), 1) * 1024 * 1024
    chunk_rows = max(1, min(int(spatial_tokens), budget // max(bytes_per_spatial, 1)))
    workspace = chunk_rows * bytes_per_spatial

    scale = float(head_dim) ** -0.5
    # [1, H, T*S, D] -> [H, T, S, D] -> [S, H, T, D]
    q_grid = q[0].reshape(heads, frames, spatial_tokens, head_dim).permute(2, 0, 1, 3)
    k_grid = k[0].reshape(heads, frames, spatial_tokens, head_dim).permute(2, 0, 1, 3)

    # 每行 softmax 之和恒为 1，故矩阵总和 = frames，非对角和 = frames - 对角和。
    # 这样就不必 materialize 对角掩码，比参考实现少一次拷贝。
    trace = torch.zeros((), device=q.device, dtype=torch.float64)
    for start in range(0, int(spatial_tokens), chunk_rows):
        end = min(start + chunk_rows, int(spatial_tokens))
        logits = torch.matmul(
            q_grid[start:end] * scale, k_grid[start:end].transpose(-2, -1)
        ).to(torch.float32)
        probabilities = torch.softmax(logits, dim=-1)
        trace = trace + torch.diagonal(probabilities, dim1=-2, dim2=-1).sum(
            dtype=torch.float64
        )
        del logits, probabilities

    matrix_count = float(int(spatial_tokens) * int(heads))
    numerator = matrix_count * float(frames) - trace
    denominator = matrix_count * float(frames) * float(frames - 1)
    if not math.isfinite(float(denominator)) or denominator <= 0:
        return None
    cfi = numerator / denominator
    if not torch.isfinite(cfi):
        return None
    return cfi.to(torch.float32), chunk_rows, workspace


# --------------------------------------------------------------------------
# 路由解析：只依赖 ComfyUI 上游写好的 layout，不需要 latent
# --------------------------------------------------------------------------
def _resolve_route(transformer_options):
    """从 transformer_options["minimax_h3_layout"] 解出 target-video 段几何。

    上游 MiniMaxH3Model._forward 会把 layout 塞进 transformer_options
    （comfy/ldm/minimax/model.py 的 `transformer_options["minimax_h3_layout"] = layout`），
    其中 segments 是 [(start, stop, kind)]、signature 是
    (text_len, latent_t, latent_h, latent_w, audio_t)。
    """
    layout = transformer_options.get("minimax_h3_layout")
    if layout is None or not hasattr(layout, "segments"):
        return None

    video_segments = [s for s in layout.segments if len(s) >= 3 and s[2] == "video"]
    if len(video_segments) != 1:
        return None
    video_start, video_end = int(video_segments[0][0]), int(video_segments[0][1])

    signature = getattr(layout, "signature", None)
    if signature is None or len(signature) < 5:
        return None
    frames = int(signature[1])
    lat_h = int(signature[2])
    lat_w = int(signature[3])

    spatial_tokens = math.ceil(lat_h / _PATCH_H) * math.ceil(lat_w / _PATCH_W)
    video_rows = video_end - video_start
    if frames < 2 or spatial_tokens < 1 or video_rows != frames * spatial_tokens:
        return None

    return {
        "video_start": video_start,
        "video_end": video_end,
        "video_rows": video_rows,
        "frames": frames,
        "spatial_tokens": spatial_tokens,
        "seq_len": int(getattr(layout, "seq_len", video_end)),
    }


def _sigma_state(transformer_options):
    """返回 (缓存 token, 采样进度)。采样器写 transformer_options["sigmas"] = sigma*1000。"""
    value = transformer_options.get("sigmas")
    if value is None:
        return None, None
    try:
        scalar = float(torch.as_tensor(value).flatten()[0])
    except Exception:
        return None, None
    if not math.isfinite(scalar):
        return None, None
    progress = 1.0 - scalar / 1000.0
    # 量纲意外时不要误判成「不激活」：进度给 None，调用方按「激活」处理
    if progress < -0.05 or progress > 1.05:
        return round(scalar, 6), None
    return round(scalar, 6), progress


# --------------------------------------------------------------------------
# 注意力覆盖
# --------------------------------------------------------------------------
def _apply_gain(q, k, output, transformer_options, config):
    """测量 CFI 并只放大 target-video 行；任何异常都原样返回 output。"""
    if config["mode"] == MODE_DISABLED:
        return output

    sigma_key, progress = _sigma_state(transformer_options)
    # token = (options 字典身份, sigma)。上游每次采样步都会新建 options 字典，
    # 所以 token 变化 == 新的模型前向；万一上游复用了同一个字典，sigma 也会兜住。
    token = (id(transformer_options), sigma_key)

    route = transformer_options.get(_ROUTE_KEY)
    if route is not None and route.get("token") != token:
        route = None          # 缓存陈旧，丢弃重算
    if route is None:
        route = _resolve_route(transformer_options)
        if route is None:
            return output
        route["token"] = token
        route["progress"] = progress
        route["active"] = True if progress is None else (
            float(config["start"]) <= progress <= float(config["end"]))
        transformer_options[_ROUTE_KEY] = route
        # 路由按 token 缓存，所以这里每次模型前向恰好执行一次，
        # 正好可以作为前向边界：结掉上一个前向的日志、开新的统计。
        _RUN.begin_forward(progress=progress, active=route["active"],
                           frames=route["frames"],
                           spatial_tokens=route["spatial_tokens"],
                           seq_len=route["seq_len"], video_rows=route["video_rows"])

    if not route["active"]:
        return output

    # 主 DiT block 的序列长度必须等于 packed 全序列；token_refiner 等其它注意力跳过
    if q.ndim != 4 or q.shape[0] != 1 or int(q.shape[2]) != route["seq_len"]:
        return output
    if k.ndim != 4 or k.shape != q.shape:
        return output

    video_start = route["video_start"]
    video_end = route["video_end"]

    measured = _chunked_cfi(
        q[:, :, video_start:video_end],
        k[:, :, video_start:video_end],
        frames=route["frames"],
        spatial_tokens=route["spatial_tokens"],
        max_workspace_mib=config["workspace"],
    )
    if measured is None:
        return output
    cfi, chunk_rows, workspace = measured

    gain = max((float(route["frames"]) + float(config["tau"])) * float(cfi), 1.0)
    if not math.isfinite(gain) or gain < 1.0:
        return output
    if gain > float(config["g_limit"]):
        _RUN.note_overflow()
        gain = float(config["g_limit"])

    _RUN.record(cfi=float(cfi), g=float(gain),
                chunk_rows=chunk_rows, workspace=workspace)

    if config["mode"] == MODE_REPORT:
        return output

    # 只动 target-video 行：文本 / 条件 / 参考 / 音频行保持原样。
    # 就地相乘，避免为整条 packed 序列做一次额外拷贝（显存敏感）。
    try:
        if output.ndim == 3:
            output[:, video_start:video_end].mul_(gain)
        elif output.ndim == 4:
            output[:, :, video_start:video_end].mul_(gain)
        else:
            return output
    except Exception:
        LOG.exception("[H3-EAV-FETA] 就地缩放失败，本层按原样返回")
    return output


def _make_override(config, previous):
    """构造 optimized_attention_override。

    ComfyUI 的 wrap_attn 会以 (func, q, k, v, heads, ...) 调用它，其中 func 是
    已解析出的真实后端（attention_basic / attention_pytorch / ...）。若之前已有
    覆盖（例如 sage / xformers / 其它插件），按同样的签名委托给它。
    """

    def override(func, q, k, v, heads, mask=None, attn_precision=None,
                 skip_reshape=False, skip_output_reshape=False,
                 transformer_options=None, **kwargs):
        call_kwargs = dict(kwargs)
        # 防止委托对象内部再次进入 wrap_attn 的覆盖分支而递归
        call_kwargs["_inside_attn_wrapper"] = True
        common = dict(
            mask=mask,
            attn_precision=attn_precision,
            skip_reshape=skip_reshape,
            skip_output_reshape=skip_output_reshape,
            transformer_options=transformer_options,
        )
        if previous is not None:
            output = previous(func, q, k, v, heads, **common, **call_kwargs)
        else:
            output = func(q, k, v, heads, **common, **call_kwargs)

        options = transformer_options if isinstance(transformer_options, dict) else {}
        try:
            output = _apply_gain(q, k, output, options, config)
        except Exception:
            LOG.exception("[H3-EAV-FETA] 增益计算失败，本层按原样返回")
        return output

    setattr(override, _OVERRIDE_FLAG, _PATCH_VERSION)
    return override


# --------------------------------------------------------------------------
# 节点
# --------------------------------------------------------------------------
class H3EAVFetaPatch(io.ComfyNode):
    """把 Enhance-A-Video / FETA 挂到 H3 的注意力上。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3EAVFetaPatch",
            display_name="H3 Enhance-A-Video / FETA (时序注意力增强)",
            category="MiniMaxH3",
            description=("Enhance-A-Video / FETA 对 MiniMax H3 的适配：只测量并缩放 packed "
                         "序列里的 target-video 注意力行，text/条件/参考/音频行不动。"
                         "论文 arXiv:2502.07508v3。默认「仅报告」——先看控制台日志里的 "
                         "CFI/g 再决定是否开「应用」。参考项目在 H3 上实测 g 平均仅 1.00034，"
                         "增益极小，开之前务必先测。"),
            inputs=[
                io.Model.Input("模型",
                               tooltip="接在链尾：LoRA 与加速节点之后、采样器「模型」之前。"
                                       "不需要 sigmas 输入（进度直接读采样器写入的 sigma）。"),
                io.Combo.Input("模式", options=list(MODES), default=MODE_REPORT,
                               tooltip="关闭=完全不介入；仅报告=只测量 CFI 与 g、不改输出"
                                       "（先用这个）；应用=按 g 缩放 target-video 注意力行。"
                                       "注意：「关闭」才是严格旁路，强度=0 不是关闭。"),
                io.Float.Input("强度", default=8.0, min=-32.0, max=32.0, step=0.25,
                               tooltip="论文的增强权重 tau。放大项是 tau*CFI，所以 tau 越大放大越强。"
                                       "参考项目工作流用 4（上游候选值），发布说明的已审值是 8；"
                                       "T8 负证据：tau=12/0%~100% 因效果过强被否决。"),
                io.Float.Input("起始进度", default=0.15, min=0.0, max=0.99, step=0.01,
                               tooltip="生效窗口下界，单位是采样进度（进度 = 1 - sigma），"
                                       "与步数无关。窗口外完全不测量、不介入，所以窗口越窄越省时间。"
                                       "参考项目已审窗口 0.15~0.90。"),
                io.Float.Input("结束进度", default=0.90, min=0.01, max=1.0, step=0.01,
                               tooltip="生效窗口上界。"),
                io.Int.Input("工作区上限MiB", default=32, min=4, max=512, step=4,
                             tooltip="分块计算 CFI 的临时张量预算，只约束本节点的分数缓冲，"
                                     "不代表整套工作流显存。参考项目实测真实占用约 7.90MiB。"
                                     "调低只会增加分块数（更慢），不会省多少。"),
                io.Float.Input("增益上限", default=1.5, min=1.0, max=3.0, step=0.01,
                               tooltip="g 的安全上限。理论上限 g_max=(frames+tau)/(frames-1)，"
                                       "H3 上约 1.25，所以 1.5 正常不会触发。超限时截断并计数"
                                       "（不中断生成），次数会写进日志与报告。"),
            ],
            outputs=[
                io.Model.Output("模型", tooltip="接采样器的「模型」输入"),
            ],
        )

    @classmethod
    def execute(cls, 模型, 模式, 强度, 起始进度, 结束进度, 工作区上限MiB, 增益上限):
        config = {
            "mode": 模式,
            "tau": float(强度),
            "start": float(起始进度),
            "end": float(结束进度),
            "workspace": int(工作区上限MiB),
            "g_limit": float(增益上限),
        }

        if 模式 == MODE_DISABLED:
            _reset_run(None)
            LOG.info("[H3-EAV-FETA] 已选择「关闭」，模型原样直通")
            return io.NodeOutput(模型)

        if not 0.0 <= config["start"] < config["end"] <= 1.0:
            raise ValueError("H3-EAV-FETA：需要满足 0 <= 起始进度 < 结束进度 <= 1")

        model = 模型.clone()
        options = model.model_options.get("transformer_options")
        if options is None:
            options = {}
            model.model_options["transformer_options"] = options

        previous = options.get("optimized_attention_override")
        if previous is not None and getattr(previous, _OVERRIDE_FLAG, None) == _PATCH_VERSION:
            # 同一个覆盖重复挂载：把上一次的委托目标接过来，避免自我递归
            previous = getattr(previous, "_h3_eav_feta_previous", None)

        override = _make_override(config, previous)
        setattr(override, "_h3_eav_feta_previous", previous)
        options["optimized_attention_override"] = override

        # 主块数：用来判断一个模型前向何时结束（数满即打日志），
        # 这样最后一个前向的统计也不会丢。取不到就退化为按前向边界结账。
        blocks_per_forward = 0
        try:
            diffusion = model.get_model_object("diffusion_model")
            blocks_per_forward = len(getattr(diffusion, "blocks", ()) or ())
        except Exception:
            LOG.debug("[H3-EAV-FETA] 未能读取 blocks 数量，改用前向边界结账", exc_info=True)

        _reset_run(config, blocks_per_forward)
        LOG.info(
            "[H3-EAV-FETA] 已挂载：模式=%s，tau=%s，窗口=%.2f~%.2f，工作区=%dMiB，主块数=%d",
            模式, config["tau"], config["start"], config["end"], config["workspace"],
            blocks_per_forward,
        )
        return io.NodeOutput(model)


class H3EAVFetaReport(io.ComfyNode):
    """读取上一次采样累积的 CFI / g 报告。可选节点：不接也不影响生成。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3EAVFetaReport",
            display_name="H3 EAV/FETA 报告 (可选)",
            category="MiniMaxH3",
            description=("把 H3-EAV-FETA 上一次运行的 CFI / g 统计输出为 JSON，并原样透传图像。"
                         "纯读取端，接不接都不影响生成；不接的话看控制台的逐前向日志也一样。"
                         "接在采样之后的任意图像上即可。"),
            inputs=[
                io.Image.Input("图像", tooltip="采样器「图像」输出，原样透传给下游"),
            ],
            outputs=[
                io.Image.Output("图像", tooltip="输入原样透传"),
                io.String.Output("报告", tooltip="CFI / g 统计 JSON"),
            ],
        )

    @classmethod
    def execute(cls, 图像):
        data = _RUN.report()
        text = json.dumps(data, ensure_ascii=False, indent=2)
        _log_summary(data)
        return io.NodeOutput(图像, text, ui=ui.PreviewText(text))
