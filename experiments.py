"""实验性功能开关中枢（第二阶段优化方案的框架地基）。

四项生成期 in-process 干预都以「实验性功能」形态交付，统一挂靠本模块：

- e1_bridge_shard  强化引导桥 + 段内分片（路线图批次1：时间滑窗/重叠条件采样、段内漂移抑制）
- e2_memory_anchor 全局记忆锚 / 长记忆多锚（批次3前半：首段关键帧沿链恒常注入）
- e3_motion_gate   运动感知闭环门控（批次3后半：复用五维 z-score 做量化反馈触发重摇/重锚）
- e4_transition_res 双向过渡重生成（批次2：past|transition|future 双锚 + 缝区独占噪声）

设计硬约束（保证 A/B 对照干净）：
1. 全部默认关闭；前端导演台「实验性功能」面板可单独/任意组合开启，一键全关。
2. **完全关闭的后端**：`ExperimentContext` 为空（或 FORCE_DISABLED）时，nodes.py 走现状
   逐字节一致路径——所有实验逻辑一律以 `ctx.has("enN_xxx")` 包裹，分支外不触碰任何
   已有变量的默认值，seed 序列默认不受实验影响（同起点可对比）。
3. `ExperimentContext.fingerprint()` 参与存档参数指纹：**切换实验组合即触发整链重做**，
   杜绝不同实验复用同一缓存污染结果。
4. 后端硬开关：环境变量 `H3_EXPERIMENTS=0` 强制全关。
"""

import os

# 实验定义与参数槽位（前端面板据此渲染；放顺序即面板展示顺序）
# 双键结构：params = 参数名元组（后端兼容视图，零改动）；params_meta = 全量参数元数据
# （GET /h3chain/experiments 下发给前端动态渲染的唯一权威数据源，数值与前端的渲染逻辑一一对应）。
EXPERIMENT_DEFS = {
    "e1_bridge_shard": dict(
        name="强化引导桥+段内分片",
        group="生成结构",
        desc="滑窗/重叠条件采样强化段间桥，并把单段内部拆子片抑制中段漂移",
        default=False,
        params=("滑窗token", "子片帧数", "重叠token"),
        params_meta=[
            {"key": "滑窗token", "type": "num", "def": 6, "min": 1, "max": 128, "step": 1},
            {"key": "子片帧数", "type": "num", "def": 0, "min": 0, "max": 512, "step": 17},
            {"key": "重叠token", "type": "num", "def": 2, "min": 0, "max": 16, "step": 1},
        ],
    ),
    "e2_memory_anchor": dict(
        name="全局记忆锚",
        group="长链记忆",
        desc="提取首段关键帧沿整链恒定注入，抑制长链逐段累积漂移",
        default=False,
        params=("记忆帧数", "注入位置"),
        params_meta=[
            {"key": "记忆帧数", "type": "num", "def": 2, "min": 1, "max": 8, "step": 1},
            {"key": "注入位置", "type": "enum", "def": "段首", "opts": ["段首", "全程"]},
        ],
    ),
    "e3_motion_gate": dict(
        name="运动感知闭环门控",
        group="闭环调控",
        desc="把接缝重摇的触发信号从『帧差』扩展为帧差+光流/相机 z-score",
        default=False,
        params=("运动z阈值", "触发动作"),
        params_meta=[
            {"key": "运动z阈值", "type": "num", "def": 2.0, "min": 0.5, "max": 6.0, "step": 0.1},
            {"key": "触发动作", "type": "enum", "def": "重摇", "opts": ["重摇", "重锚"]},
        ],
    ),
    "e4_transition_res": dict(
        name="双向过渡重生成",
        group="过渡重采样",
        desc="对超阈值缝区做 past|transition|future 双锚 + 缝区独占噪声的定向重采样",
        default=False,
        params=("过渡窗帧数", "重生成步数", "双锚强度"),
        params_meta=[
            {"key": "过渡窗帧数", "type": "num", "def": 17, "min": 5, "max": 512, "step": 17},
            {"key": "重生成步数", "type": "num", "def": 20, "min": 1, "max": 100, "step": 1},
            {"key": "双锚强度", "type": "num", "def": 1.0, "min": 0.0, "max": 2.0, "step": 0.1},
        ],
    ),
    "soft_bridge": dict(
        name="桥区软着陆",
        group="生成结构",
        desc="段首直接钉住上一段的真实尾巴（逐 token 噪声掩码），替代「多烧 ctx 帧再裁掉」；"
             "接缝处模型是接着上段末帧画，而不是照条件重画一遍。需 ComfyUI v0.34+",
        default=False,
        params=("钉住帧数", "释放曲线"),
        params_meta=[
            {"key": "钉住帧数", "type": "num", "def": 9, "min": 5, "max": 22, "step": 4,
             "desc": "段首被钉住的帧数（自动对齐到 token 网格：5/9/13/17/18/22）。"
                     "越大上文越完整、省得越少；默认 9 帧 = 3 token"},
            {"key": "释放曲线", "type": "enum", "def": "hold",
             "opts": ["hold", "linear", "smoothstep", "ease_in"],
             "desc": "hold=整窗钉死（默认，上文逐帧精确）；其余为对照档：钉住窗内提前放开生成"},
        ],
    ),
    "audio_seam": dict(
        name="音频接缝软过渡",
        group="生成结构",
        desc="软桥下音频头部与上段尾同帧钉住，接缝逐帧连续；本开关控制段首响度对齐的残留强度"
             "（0=完全不做，1=现状）。需同时开启「桥区软着陆」才有钉住效果",
        default=False,
        params=("响度对齐强度",),
        params_meta=[
            {"key": "响度对齐强度", "type": "num", "def": 0.0, "min": 0.0, "max": 1.0, "step": 0.1},
        ],
    ),
    "mid_anchor": dict(
        name="段中锚点",
        group="长链记忆",
        desc="在段中部再钉一个锚（复用本段已有的头锚/记忆锚素材），抑制长段内部漂移。"
             "对齐官方 MiniMaxH3AddGuide 的任意帧锚定语义",
        default=False,
        params=("锚点位置",),
        params_meta=[
            {"key": "锚点位置", "type": "num", "def": 0.5, "min": 0.25, "max": 0.75, "step": 0.05,
             "desc": "锚点在本段中的相对位置（0.5=正中）"},
        ],
    ),
}

# 后端硬开关：H3_EXPERIMENTS=0 强制全关（优先级最高，忽略一切前端开关）
FORCE_DISABLED = os.environ.get("H3_EXPERIMENTS", "1").strip().lower() == "0"


class ExperimentContext:
    """从导演台状态 ds.experiments 归一化出的实验开关集。

    - 全关 / 缺失 / FORCE_DISABLED => 空 context（`has()` 恒 False，`active_list()` 空）。
    - 只认 EXPERIMENT_DEFS 中的合法 id，未知 id 忽略（防脏数据）。
    - `param()` 读取每项内嵌的参数字典，未提供则返回默认值。
    - `fingerprint()` 供存档指纹比对：不同组合产生不同指纹 -> 整链重做。
    """

    def __init__(self, ds=None):
        self._on = {}
        self._params = {}
        if FORCE_DISABLED:
            return
        if not isinstance(ds, dict):
            return
        ex = ds.get("experiments")
        if not isinstance(ex, dict):
            return
        p = ex.get("params") if isinstance(ex.get("params"), dict) else {}
        for exp_id, meta in EXPERIMENT_DEFS.items():
            if ex.get(exp_id) is True:
                self._on[exp_id] = True
                if isinstance(p.get(exp_id), dict):
                    self._params[exp_id] = dict(p[exp_id])

    @property
    def enabled(self):
        return bool(self._on)

    def has(self, exp_id):
        return exp_id in self._on

    def param(self, exp_id, key, default=None):
        d = self._params.get(exp_id)
        return d.get(key, default) if isinstance(d, dict) else default

    def active_list(self):
        return sorted(self._on)

    def fingerprint(self):
        if not self._on:
            return ""
        parts = []
        for exp_id in sorted(self._on):
            parts.append(exp_id)
            pd = self._params.get(exp_id)
            if isinstance(pd, dict) and pd:
                kv = ",".join(f"{k}={pd[k]}" for k in sorted(pd))
                parts.append(f"[{kv}]")
        return "|".join(parts)

    def describe(self):
        if not self._on:
            return "实验性功能：全部关闭"
        names = "、".join(EXPERIMENT_DEFS[i]["name"] for i in self.active_list())
        return f"实验性功能：开启 {names}"


def resolve(ds=None):
    """便捷工厂：外部统一用 experiments.resolve(ds) 构建 context。"""
    return ExperimentContext(ds)


def experiment_defs_payload():
    """GET /h3chain/experiments 的响应体（JSON 安全；前端实验面板唯一数据源）。

    前端不再硬编码 EXPERIMENT_DEFS 镜像，面板定义（名称/分组/描述/参数元数据）
    与后端硬开关 force_disabled 一律以本载荷为准。
    """
    return {
        "ok": True,
        "force_disabled": FORCE_DISABLED,
        "experiments": [
            {
                "id": exp_id,
                "name": meta["name"],
                "group": meta["group"],
                "desc": meta["desc"],
                "default": bool(meta.get("default", False)),
                "params": [dict(p) for p in meta.get("params_meta", ())],
            }
            for exp_id, meta in EXPERIMENT_DEFS.items()
        ],
    }


# ---- E1 强化引导桥：段首引导 latent 的滑窗/重叠窗口布局（纯数学，可无 torch 单测） ----

def e1_windows(total_tokens, window_tokens, overlap_tokens):
    """把整段引导 latent（total_tokens）稀疏成若干带重叠的子 token 窗。

    返回 (start, end) token 闭区间有序列表，满足「完整覆盖 + 相邻重叠」：
    - 首窗从 0 起（接缝位置精确对齐）；末窗贴到 T（真正交接到新段内容）。
    - 每窗相对前一窗前移 step = window_tokens - overlap_tokens（重叠）。
    - window_tokens<=0 或 >= total 时退化为单窗 [(0,T)]（= 现状，零影响）。
    E1 关闭 / 单窗时调用方不做任何改动。
    """
    T = int(total_tokens)
    if T <= 0:
        return []
    win = int(window_tokens)
    if win <= 0 or win >= T:
        return [(0, T)]
    ov = max(0, int(overlap_tokens))
    step = max(1, win - ov)
    spans = []
    start = 0
    while start + win <= T:
        spans.append((start, start + win))
        start += step
    if spans and spans[-1][1] < T:
        spans.append((T - win, T))
    elif not spans:
        spans.append((0, T))
    uniq = []
    for s in spans:
        if not uniq or s != uniq[-1]:
            uniq.append(s)
    return uniq


def e1_window_kf(guide, window_tokens, overlap_tokens, latent_t_to_frames):
    """E1 用：把单个引导 keyframe 展开为多窗滑窗 keyframe 序列。

    guide: _tail_keyframe 产出的关键帧 dict（latent [B,C,T,H,W]，可选 audio_latent）。
    window_tokens/overlap_tokens: 见 e1_windows。
    latent_t_to_frames: token->帧 映射函数（注入，便于无 torch 单测）。
    返回产物 keyframe 列表；退化（<2 窗 / 无 latent / 引导 token<=1）时返回原 guide 单元素，
    保证 E1 对现状零打扰、接缝读到的东西语义不变。
    每个滑窗 keyframe 的 resolved_frame_index 取该窗起点 token 对应帧数：
    首窗在 0（接缝精确），后续窗重叠前移，末窗贴到 T（真正交接到新段）。音频只附首窗。
    """
    kf0 = dict(guide) if isinstance(guide, dict) else guide
    if not isinstance(guide, dict) or guide.get("latent") is None \
            or guide["latent"].dim() < 3 or guide["latent"].shape[2] <= 1:
        return [kf0]
    spans = e1_windows(guide["latent"].shape[2], window_tokens, overlap_tokens)
    if len(spans) < 2:
        return [kf0]
    out = []
    has_audio = "audio_latent" in guide
    for start, end in spans:
        kf = {
            "resolved_frame_index": latent_t_to_frames(start),
            "latent": guide["latent"][:, :, start:end, :, :].clone(),
        }
        if has_audio and start == 0:
            kf["audio_latent"] = guide["audio_latent"]
        out.append(kf)
    return out


# ---- E2 全局记忆锚：记忆 token 数 + 注入位置（纯数学，可无 torch 单测） ----

def memory_tokens(mem_frames, frames_to_latent_t):
    """记忆锚帧数 -> 视频 latent token 数；<=0 或换算为 0 时兜底 1 token。"""
    mf = int(mem_frames)
    if mf <= 0:
        return 1
    return max(1, int(frames_to_latent_t(mf, True)))


def memory_anchor_positions(mode, sampled_fc):
    """E2 记忆锚注入位置（帧索引列表）：段首=0；全程=段首 + 段中。
    只在 E2 开启时由调用方把同一记忆 latent 放到这些位置作为全局 reference。"""
    if str(mode) == "全程" and int(sampled_fc) > 2:
        return [0, int(sampled_fc) // 2]
    return [0]


# ---- E3 运动感知闭环门控：多信号判定纯函数（无 torch，可单测） ----

def e3_motion_trigger(z_row, motion_z_th=2.0, action="重摇"):
    """E3：基于缝的五维 z-score 判定是否触发闭环干预，及触发动作。

    z_row: metrics.evaluate_local 返回的 dict（至少含 flow_z/cam_z，可为空/None）。
    motion_z_th: 运动 z 阈值（对齐验收线 2.0σ）。
    action: 触发动作名（"重摇" / "重锚"），未知兜底 "重摇"。

    返回 (triggered: bool, action: str)。triggered=False 时 action 无意义。
    触发条件：flow_z 或 cam_z 任一 > motion_z_th（|z|>th 视为异常）。
    任一维度缺失（=None）时该维度不参与判定，避免缺依赖时误触发。
    """
    if not isinstance(z_row, dict):
        return False, "重摇"
    th = float(motion_z_th)
    fz = z_row.get("flow_z")
    cz = z_row.get("cam_z")
    triggered = False
    if fz is not None and abs(float(fz)) > th:
        triggered = True
    if cz is not None and abs(float(cz)) > th:
        triggered = True
    act = str(action) if str(action) in ("重摇", "重锚") else "重摇"
    return triggered, act