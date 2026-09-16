"""实验性功能开关中枢（第二阶段优化方案的框架地基）。

生成期 in-process 干预都以「实验性功能」形态交付，统一挂靠本模块：

- e3_motion_gate   运动感知闭环门控（批次3后半：复用五维 z-score 做量化反馈触发重摇/重锚）
- e4_transition_res 双向过渡重生成（批次2：past|transition|future 双锚 + 缝区独占噪声）
- soft_bridge      桥区软着陆
- audio_seam       音频接缝处理

已下线（2026-09-16，手动锚定重构）：`e1_bridge_shard` / `e2_memory_anchor` / `mid_anchor`
——三者本质都是「把某段 latent 钉到本段某位置」，已并入 `seg.anchors[]`
（e1 = 同源多窗，e2 = `src.kind="segment"` 指首段，mid = `at.mode="mid"`）。
实验面板不再渲染它们；旧存档里这几个开关的取值不再影响生成。

设计硬约束（保证 A/B 对照干净）：
1. 全部默认关闭；前端导演台「实验性功能」面板可单独/任意组合开启，一键全关。
2. **完全关闭的后端**：`ExperimentContext` 为空（或 FORCE_DISABLED）时，nodes.py 走现状
   逐字节一致路径——所有实验逻辑一律以 `ctx.has("enN_xxx")` 包裹，分支外不触碰任何
   已有变量的默认值，seed 序列默认不受实验影响（同起点可对比）。
3. `ExperimentContext.fingerprint()` 参与存档参数指纹。**2026-09-16 起它不再触发重做**：
   实验开关与 steps/cfg 同类，只影响此后新采样的段（已完成段是盘上张量，与参数无耦合）。
4. 后端硬开关：环境变量 `H3_EXPERIMENTS=0` 强制全关。
"""

import os

# 实验定义与参数槽位（前端面板据此渲染；放顺序即面板展示顺序）
# 双键结构：params = 参数名元组（后端兼容视图，零改动）；params_meta = 全量参数元数据
# （GET /h3chain/experiments 下发给前端动态渲染的唯一权威数据源，数值与前端的渲染逻辑一一对应）。
EXPERIMENT_DEFS = {
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