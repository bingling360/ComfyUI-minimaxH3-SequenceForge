"""参考视频压缩（Spark-VPack 内联版）的守卫。

上游 `zechengtang/Spark-H3` 的 Spark-VPack 是个独立节点（`positive` 往返接线）。
本仓把它内联进渲染链 —— 因为主节点是「cond 构建 + 采样」的合体，画布上没有
CONDITIONING 端口也没有 BasicGuider，而 cond_i 依赖 latent_{i-1}，硬接还会成环
（详见 vpack.py 模块头）。内联就意味着**没有接线可看**，所以这里全用源码级守卫钉住：

  ① 抽帧索引表与上游实现**逐值一致**（把上游那两层函数原样抄进测试当 oracle）
  ② 未生效（关 / 0/5）时逐字节透传：apply 原对象返回、不进指纹、不装补丁
  ③ apply 只改 minimax_refs 里的视频 latent，图/音引用与文本张量一根毫毛不动
  ④ RoPE 时间坐标真的被还原成「抽剩帧的原始时间」，且下游行按跨度差整体平移
  ⑤ 两个 cond 诞生点都接上（nodes.py 一采 + upscale.py 二采）—— 一采压了二采不压
     是两份不同形状的条件，比「语义漂移」更硬
  ⑥ 进逐段指纹（vp: 键）与整链参数快照（ckpt_params["vpack"]），且只在生效时加键
  ⑦ 路由两处都挂（ROUTES + handler 映射）

跑法（仓库根目录）：
    python -m pytest tests/test_vpack.py -q
"""

import ast
import os
import sys

import pytest
import torch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import vpack  # noqa: E402


def _src(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


# ============================ ① 索引表 = 上游口径 ============================
#
# 下面是上游 `comfyui_ref2va.py` 的 `_chunk_phase_indices` +
# `_compression_dense_indices` **原样抄写**（含它的两段式结构）。测试拿它当 oracle：
# 内联版一旦把分块口径写歪，这里立刻炸 —— 而这个口径错了，画面会悄悄坏掉。

_UP_FPT = (1, 4, 4, 4, 4)
_UP_PROFILES = {"0/5": (0, 1, 2, 3), "1/5": (0, 1, 2), "2/5": (0, 2), "3/5": (2,)}


def _upstream_chunk_phase_indices(dense_latent_frames, normal_tokens,
                                  single_normal_index=0, normal_indices="",
                                  head_tokens=1):
    if normal_indices.strip():
        phases = [int(value.strip()) for value in normal_indices.split(",")]
    elif normal_tokens == 1:
        phases = [single_normal_index]
    elif normal_tokens == 2:
        phases = [0, 2]
    else:
        phases = list(range(normal_tokens))
    phases.sort()
    selected = []
    for chunk_start in range(0, dense_latent_frames, len(_UP_FPT)):
        if head_tokens:
            selected.append(chunk_start)
        normal = list(range(chunk_start + 1,
                            min(chunk_start + len(_UP_FPT), dense_latent_frames)))
        selected.extend(normal[phase] for phase in phases if phase < len(normal))
    return selected


def _upstream_compression_indices(dense_latent_frames, ratio):
    phases = _UP_PROFILES[ratio]
    return _upstream_chunk_phase_indices(
        dense_latent_frames, len(phases),
        normal_indices=",".join(str(p) for p in phases), head_tokens=1)


def test_compression_schedule_matches_upstream_for_all_lengths():
    for t in range(1, 301):
        for ratio in vpack.RATIOS:
            assert vpack.compression_indices(t, ratio) \
                == _upstream_compression_indices(t, ratio), (t, ratio)


def test_compression_schedule_shape():
    """H3 的参考视频 latent_t = 5n+2（17k+5 像素帧 → 5n+2 latent 帧）。"""
    for n in range(0, 40):
        t = 5 * n + 2 if n else 2
        for ratio in vpack.RATIOS:
            idx = vpack.compression_indices(t, ratio)
            assert idx == sorted(set(idx)), "下标必须严格升序且不重复"
            assert idx[0] == 0, "每块 head 恒保留 → 首下标恒为 0"
            assert idx[-1] < t, "下标不得越界"
    # 档位名就是「压 N/5」，实测保留比例应当单调递减
    t = 52
    kept = [len(vpack.compression_indices(t, r)) for r in vpack.RATIOS]
    assert kept == sorted(kept, reverse=True), kept
    assert kept[0] == t, "0/5 = 不压缩"


def test_compression_indices_rejects_unknown_ratio():
    with pytest.raises(ValueError):
        vpack.compression_indices(12, "9/9")
    with pytest.raises(ValueError):
        vpack.compression_indices(0, "2/5")
    with pytest.raises(ValueError):
        vpack.chunk_phase_indices(12, (0, 9))


# ============================ ② 配置与指纹判据 ============================

def test_parse_state_and_enabled_and_signature():
    assert vpack.parse_state(None) == vpack.DEFAULT_VPACK
    assert vpack.parse_state({"on": "false"})["on"] is False, "非真 True 一律判关（防 JSON 手改成字符串）"
    assert vpack.parse_state({"ratio": "9/9"})["ratio"] == vpack.DEFAULT_RATIO
    assert vpack.parse_state({"zz": 1}) == vpack.DEFAULT_VPACK, "未知键丢弃"

    # 出厂默认 / 0/5 都不生效：不进指纹、不装补丁、不动 cond
    assert vpack.enabled({"on": False, "ratio": "2/5"}) is False
    assert vpack.enabled({"on": True, "ratio": "0/5"}) is False
    assert vpack.enabled({"on": True, "ratio": "2/5"}) is True
    assert vpack.enabled(None) is False

    assert vpack.signature({"on": False, "ratio": "2/5"}) is None
    assert vpack.signature({"on": True, "ratio": "0/5"}) is None
    assert vpack.signature({"on": True, "ratio": "3/5"}) == "3/5"
    assert vpack.report_line({"on": False}) is None
    assert "3/5" in vpack.report_line({"on": True, "ratio": "3/5"})


def test_vpack_module_has_no_comfy_dependency():
    """托管 venv 里只有 torch/pytest —— 本模块不许碰 comfy（与 refvideo.py 同一条要求）。"""
    tree = ast.parse(_src("vpack.py"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods.add(n.module.split(".")[0])
    assert "comfy" not in mods
    assert not any(m.startswith("comfy") for m in mods)


# ============================ ③ cond 变换 ============================

def _mk_ref(kind="video", dense_t=12, ref_audio_t=0, rows=3, channels=2, h=2, w=2):
    return {
        "kind": kind,
        "latent_t": dense_t,
        "latent_h": h, "latent_w": w,
        "ref_audio_t": ref_audio_t,
        "latent": torch.arange(dense_t * channels * h * w, dtype=torch.float32)
                         .reshape(1, channels, dense_t, h, w),
        "audio_latent": (torch.zeros(1, 4, ref_audio_t) if ref_audio_t else None),
    }


def _mk_cond(refs):
    return [[torch.zeros(1, 5, vpack_smoke_dim()), {"minimax_refs": refs, "minimax_token_tags": [1]}]]


def vpack_smoke_dim():
    """cond 张量末维（语义桥要求 5120；VPack 不碰它，随便给个小值省内存）。"""
    return 8


def test_apply_is_passthrough_when_off():
    cond = _mk_cond([_mk_ref()])
    for cfg in ({"on": False, "ratio": "2/5"}, {"on": True, "ratio": "0/5"}, None):
        assert vpack.apply(cond, cfg) is cond, f"{cfg} 必须原对象返回（逐字节透传）"


def test_apply_passthrough_when_no_video_refs():
    """本段没有参考视频 → 静默原样返回（上游在这里抛错；本仓是多段链，见模块头）。"""
    for refs in ([], [_mk_ref(kind="image")], [_mk_ref(kind="audio", ref_audio_t=6)]):
        cond = _mk_cond(refs)
        assert vpack.apply(cond, {"on": True, "ratio": "2/5"}) is cond


def test_apply_compresses_only_video_refs_and_keeps_others():
    vid = _mk_ref(dense_t=12)
    img = _mk_ref(kind="image")
    aud = _mk_ref(kind="audio", ref_audio_t=6)
    cond = _mk_cond([img, vid, aud])
    info = {}
    out = vpack.apply(cond, {"on": True, "ratio": "2/5"}, info)

    assert out is not cond and out[0][1] is not cond[0][1], "元数据必须是新 dict（不改上游）"
    refs = out[0][1]["minimax_refs"]
    assert refs[0] is img and refs[2] is aud, "图/音引用必须原对象透传"
    assert refs[1] is not vid, "被压的视频引用必须是副本"
    assert cond[0][1]["minimax_refs"][1] is vid, "原始 ref 不得被就地改写"

    idx = vpack.compression_indices(12, "2/5")
    assert refs[1]["latent_t"] == len(idx)
    assert refs[1]["latent"].shape[2] == len(idx)
    assert torch.equal(refs[1]["latent"], vid["latent"].index_select(
        2, torch.tensor(idx, dtype=torch.long)))
    assert refs[1][vpack.ROPE_MODE_KEY] == vpack.MODE_CHUNK_PHASE
    assert refs[1][vpack.ROPE_DENSE_LATENT_T_KEY] == 12
    assert refs[1][vpack.ROPE_DENSE_INDICES_KEY] == idx
    assert refs[1][vpack.ROPE_SCALE_KEY] == 1.0
    assert info == {"videos": 1, "before": 12, "after": len(idx)}
    # 文本张量（语义桥的地盘）一根毫毛不动
    assert out[0][0] is cond[0][0]
    assert out[0][1]["minimax_token_tags"] is cond[0][1]["minimax_token_tags"]


def test_apply_raises_on_inconsistent_latent():
    bad = _mk_ref(dense_t=12)
    bad["latent_t"] = 7                      # 与 latent.shape[2] 不符
    with pytest.raises(ValueError):
        vpack.apply(_mk_cond([bad]), {"on": True, "ratio": "2/5"})
    bad2 = dict(_mk_ref(dense_t=12))
    bad2["latent"] = torch.zeros(1, 2, 12, 2)     # 不是 5D
    with pytest.raises(ValueError):
        vpack.apply(_mk_cond([bad2]), {"on": True, "ratio": "2/5"})


def test_apply_info_untouched_when_nothing_packed():
    info = {}
    cond = _mk_cond([_mk_ref(kind="image")])
    vpack.apply(cond, {"on": True, "ratio": "2/5"}, info)
    assert info == {}, "没压任何东西就不该写 info（调用方据此决定要不要写报告行）"


# ============================ ④ RoPE 时间坐标还原 ============================

ROWS = 3          # 每个 latent 帧占几行（layout 里 (h/2)*(w/2)）


def _ref_span(ref):
    """镜像官方 `_ref_t_span`：图=1.0、音=ref_audio_t、视频=max(音频, 视频跨度)。"""
    kind = ref["kind"]
    if kind == "image":
        return 1.0
    if kind == "audio":
        return float(ref.get("ref_audio_t", 0))
    if kind in vpack.VIDEO_KINDS:
        return max(float(ref.get("ref_audio_t", 0)),
                   vpack.video_rope_span(ref["latent_t"]))
    return 0.0


def _mk_layout(refs, text_len=4, target_t=6, target_rows=2, audio_t=3,
               kf_video=0, kf_audio=0):
    """造一个与 PackedLayout 同构的假 layout（段表 + position_ids）。

    **时间原点严格按官方口径推**：refs 的时间轴从 `text_len` 起算，每个 ref 占
    `_ref_span(ref)`；keyframes 与目标音频/视频都坐在「所有 refs 之后」的那个
    cursor 上。这样多视频引用时，第二个 ref 的原点真的受第一个 ref 的影响 ——
    `apply_rope` 那段「逐 ref 累加 delta」的正确性才测得到。
    """
    segs = [("text", text_len)]
    pos_rows = []
    pos_rows.append(torch.stack([
        torch.arange(text_len, dtype=torch.float64),
        torch.zeros(text_len, dtype=torch.float64),
        torch.zeros(text_len, dtype=torch.float64)], dim=1))
    # keyframes：官方先扫一遍 refs 算出「refs 之后」的 cursor，再放锚点
    kf_cursor = float(text_len) + sum(_ref_span(r) for r in refs)
    for kind, count in (("cond", kf_video), ("cond_audio", kf_audio)):
        if not count:
            continue
        segs.append((kind, count))
        pos_rows.append(torch.full((count, 3), kf_cursor, dtype=torch.float64))
    # refs：从 text_len 重新开始，逐块推进 cursor
    cursor = float(text_len)
    for ref in refs:
        kind = ref["kind"]
        if kind == "image":
            n = 4
            segs.append(("ref_img", n))
            pos_rows.append(torch.full((n, 3), cursor, dtype=torch.float64))
            cursor += _ref_span(ref)
        elif kind == "audio":
            n = int(ref["ref_audio_t"]) * 2
            if n:
                segs.append(("ref_audio", n))
                pos_rows.append(torch.full((n, 3), cursor, dtype=torch.float64))
            cursor += _ref_span(ref)
        elif kind in vpack.VIDEO_KINDS:
            rt = int(ref["ref_audio_t"])
            if rt:
                segs.append(("ref_audio", rt * 2))
                pos_rows.append(torch.full((rt * 2, 3), cursor, dtype=torch.float64))
            vt = int(ref["latent_t"])
            n = vt * ROWS
            segs.append(("ref_img", n))
            times = (cursor + vpack.video_rope_offsets(vt)).repeat_interleave(ROWS)
            pos_rows.append(torch.stack([times,
                                         torch.zeros(n, dtype=torch.float64),
                                         torch.zeros(n, dtype=torch.float64)], dim=1))
            cursor += _ref_span(ref)
    # 目标音频/视频：坐在 refs 全部扫完之后的 cursor 上
    segs.append(("audio", audio_t * 2))
    pos_rows.append(torch.full((audio_t * 2, 3), cursor, dtype=torch.float64))
    segs.append(("video", target_t * target_rows))
    pos_rows.append(torch.full((target_t * target_rows, 3), cursor, dtype=torch.float64))

    layout = type("FakeLayout", (), {})()
    layout.segments = []
    o = 0
    for kind, count in segs:
        layout.segments.append((o, o + count, kind))
        o += count
    layout.position_ids = torch.cat(pos_rows)
    assert layout.position_ids.shape[0] == o
    return layout


def _mk_payload(refs, **kw):
    return {"layout": _mk_layout(refs, **kw), "refs": refs}


def test_apply_rope_restores_dense_time_and_shifts_tail():
    vid = _mk_ref(dense_t=12)
    info = {}
    cond = vpack.apply(_mk_cond([vid]), {"on": True, "ratio": "2/5"}, info)
    packed_ref = cond[0][1]["minimax_refs"][0]
    payload = _mk_payload([packed_ref])

    seg = [s for s in payload["layout"].segments if s[2] == "ref_img"][0]
    start, stop = seg[0], seg[1]
    before = payload["layout"].position_ids.clone()
    tail_before = payload["layout"].position_ids[stop:, 0].clone()

    assert vpack.apply_rope(payload) is True
    after = payload["layout"].position_ids

    idx = vpack.compression_indices(12, "2/5")
    dense_times = vpack.video_rope_offsets(12).index_select(
        0, torch.tensor(idx, dtype=torch.long))
    # 首个 ref 的原点 = text_len（它前面没有别的 ref，压缩前后同一个值）
    origin = float(before[start, 0].item())
    want = origin + dense_times.repeat_interleave(ROWS)
    assert torch.allclose(after[start:stop, 0], want), "抽剩帧必须坐回原始时间坐标"
    assert torch.allclose(after[start:stop, 1:], before[start:stop, 1:]), "h/w 列不许动"

    delta = vpack.video_rope_span(12) - vpack.video_rope_span(len(idx))
    assert delta > 0
    assert torch.allclose(after[stop:, 0], tail_before + delta), "下游行整体平移（保持相对时间）"
    assert torch.allclose(after[:start, 0], before[:start, 0]), "text 段不受影响"


def test_apply_rope_multiple_video_refs_accumulate():
    """两个视频引用：第 2 个的时间原点受第 1 个的跨度影响，必须**逐 ref 累加** delta。

    这是 `corrected[stop:, 0] += delta` 那段累积逻辑唯一的真实检验点 —— 单个 ref
    时它退化成一次平移，看不出第二个 ref 的 `origin` 是不是已经带上前一次的 delta。
    """
    cfg = {"on": True, "ratio": "2/5"}
    v1, v2 = _mk_ref(dense_t=12), _mk_ref(dense_t=7)
    cond = vpack.apply(_mk_cond([v1, v2]), cfg)
    refs = cond[0][1]["minimax_refs"]
    payload = _mk_payload(refs)
    vis = [s for s in payload["layout"].segments if s[2] == "ref_img"]
    assert len(vis) == 2
    assert vpack.apply_rope(payload) is True
    after = payload["layout"].position_ids

    def dense_span(r):
        return max(float(r.get("ref_audio_t", 0)),
                   vpack.video_rope_span(int(r[vpack.ROPE_DENSE_LATENT_T_KEY])))

    origin = 4.0     # text_len：refs 的时间轴从它起算
    for ref, (a, b, _kind) in zip(refs, vis):
        dt = int(ref[vpack.ROPE_DENSE_LATENT_T_KEY])
        idx = vpack.compression_indices(dt, "2/5")
        want = origin + vpack.video_rope_offsets(dt).index_select(
            0, torch.tensor(idx, dtype=torch.long)).repeat_interleave(ROWS)
        assert torch.allclose(after[a:b, 0], want), \
            f"ref 段 {(a, b)} 没坐回 dense 时间坐标（原点应为 {origin}）"
        origin += dense_span(ref)
    # 目标音频/视频：必须落在「全部 dense 跨度之后」，也就是未压缩时的那个原点
    for a, b, kind in [s for s in payload["layout"].segments if s[2] in ("audio", "video")]:
        got = float(after[a, 0].item())
        assert abs(got - origin) < 1e-9, f"{kind} 段没平移回 dense 原点 {origin}，实际 {got}"


def test_apply_rope_is_identity_when_nothing_is_dropped():
    """latent_t=2 时 2/5 一个 token 都没抽掉 → 修正必须落在恒等（否则凭空改了时间轴）。"""
    vid = _mk_ref(dense_t=2)
    cond = vpack.apply(_mk_cond([vid]), {"on": True, "ratio": "2/5"})
    idx = vpack.compression_indices(2, "2/5")
    assert idx == [0, 1], "2/5 在 2 帧上抽不掉任何 token"
    payload = _mk_payload([cond[0][1]["minimax_refs"][0]])
    before = payload["layout"].position_ids.clone()
    assert vpack.apply_rope(payload) is True
    assert torch.allclose(payload["layout"].position_ids, before), "没抽帧就不该动时间轴"


def test_apply_rope_shifts_anchor_rows_to_the_target_origin():
    """锚点行（keyframes）与目标视频**同一个时间原点** —— 官方 PackedLayout 注释原话
    「anchors count from the target timeline origin」。压缩把原点提前了，锚点行排在
    refs 之前、不在 `corrected[stop:]` 覆盖范围内，必须单独补（本仓第三处刻意分歧：
    上游没做，因为它的典型用法里没有 cond 段）。

    这里钉的是**真正的不变量**：修正后锚点时间 == 目标段原点。不钉这个，
    锚点就会与它要锚的那一帧错开。"""
    vid = _mk_ref(dense_t=12)
    cond = vpack.apply(_mk_cond([vid]), {"on": True, "ratio": "2/5"})
    ref = cond[0][1]["minimax_refs"][0]
    payload = _mk_payload([ref], kf_video=8, kf_audio=4)

    kf_segs = [s for s in payload["layout"].segments if s[2] in ("cond", "cond_audio")]
    assert len(kf_segs) == 2
    before = payload["layout"].position_ids.clone()
    assert vpack.apply_rope(payload) is True
    after = payload["layout"].position_ids

    idx = vpack.compression_indices(12, "2/5")
    delta = vpack.video_rope_span(12) - vpack.video_rope_span(len(idx))
    assert delta > 0
    for a, b, kind in kf_segs:
        assert torch.allclose(after[a:b, 0], before[a:b, 0] + delta), \
            f"{kind} 锚点行必须与目标原点一起平移，否则与它锚的目标帧错开 {delta}"
    # 不变量：修正后锚点时间 == 目标段原点
    for a, b, kind in [s for s in payload["layout"].segments if s[2] == "video"]:
        for ka, kb, kk in kf_segs:
            assert abs(float(after[ka, 0].item()) - float(after[a, 0].item())) < 1e-9, \
                f"{kk} 锚点原点 {float(after[ka, 0].item())} 与 {kind} 原点 {float(after[a, 0].item())} 不一致"
    # text 段不是时间轴（它是一条独立的索引轴）—— 一根毫毛都不许动
    text_a, text_b, _ = payload["layout"].segments[0]
    assert torch.allclose(after[text_a:text_b, 0], before[text_a:text_b, 0])


def test_apply_rope_is_idempotent_via_marker():
    vid = _mk_ref(dense_t=12)
    cond = vpack.apply(_mk_cond([vid]), {"on": True, "ratio": "2/5"})
    payload = _mk_payload([cond[0][1]["minimax_refs"][0]])
    assert vpack.apply_rope(payload) is True
    snapshot = payload["layout"].position_ids.clone()
    assert vpack.apply_rope(payload) is False, "第二次必须被 marker 拦住（否则会越修越歪）"
    assert torch.equal(payload["layout"].position_ids, snapshot)


def test_apply_rope_skips_refs_without_the_keys():
    """没被 VPack 压过的 ref（scale=1.0 / 无 mode）不参与 —— 图/音引用天然豁免。"""
    vid = _mk_ref(dense_t=12)
    payload = _mk_payload([vid])
    before = payload["layout"].position_ids.clone()
    assert vpack.apply_rope(payload) is False
    assert torch.equal(payload["layout"].position_ids, before)


def test_apply_rope_rejects_unknown_mode():
    vid = _mk_ref(dense_t=12)
    vid[vpack.ROPE_MODE_KEY] = "hybrid_tail_dense"   # 上游的消融模式，本仓没移植
    payload = _mk_payload([vid])
    with pytest.raises(ValueError):
        vpack.apply_rope(payload)


def test_apply_rope_rejects_broken_index_table():
    vid = _mk_ref(dense_t=12)
    cond = vpack.apply(_mk_cond([vid]), {"on": True, "ratio": "2/5"})
    ref = cond[0][1]["minimax_refs"][0]
    ref[vpack.ROPE_DENSE_INDICES_KEY] = [0, 1, 3]     # 长度与 latent_t 不符
    with pytest.raises(ValueError):
        vpack.apply_rope(_mk_payload([ref]))


# ============================ ⑤ install（模型补丁） ============================

class MiniMaxH3Model:
    """名字必须是这个 —— install 按 MRO 名字认原生 H3（与上游同口径）。"""


class _FakeDit(MiniMaxH3Model):
    pass


class _FakeModel:
    def __init__(self):
        self.model = _FakeDit()
        self.model_options = {}

    def get_model_object(self, name):
        assert name == "diffusion_model"
        return self.model

    def clone(self):
        n = _FakeModel()
        n.model = self.model
        n.model_options = dict(self.model_options)
        return n

    def set_model_unet_function_wrapper(self, fn):
        self.model_options["model_function_wrapper"] = fn


def test_install_is_noop_when_off_or_unsupported():
    fm = _FakeModel()
    assert vpack.install(fm, {"on": False, "ratio": "2/5"}) is fm
    assert vpack.install(fm, {"on": True, "ratio": "0/5"}) is fm
    assert vpack.install(None, {"on": True, "ratio": "2/5"}) is None
    plain = object()
    assert vpack.install(plain, {"on": True, "ratio": "2/5"}) is plain, \
        "缺 clone / get_model_object 等接口 → 原样返回（不抛）"

    class NotH3:
        pass

    class Other:
        def __init__(self):
            self.model = NotH3()
            self.model_options = {}
        def get_model_object(self, _n):
            return self.model
        def clone(self):
            return self
        def set_model_unet_function_wrapper(self, _fn):
            raise AssertionError("非原生 H3 不该被装补丁")

    other = Other()
    assert vpack.install(other, {"on": True, "ratio": "2/5"}) is other, \
        "非原生 MiniMaxH3Model → 不装补丁、原样返回"


def test_install_returns_patched_clone_and_chains_previous_wrapper():
    fm = _FakeModel()
    order = []
    seen = {}

    def prev(apply_model, args):
        order.append("prev")
        seen["args"] = args
        return "PREV"

    fm.model_options["model_function_wrapper"] = prev
    patched = vpack.install(fm, {"on": True, "ratio": "2/5"})
    assert patched is not fm, "必须 clone（`模型` 是 loader 缓存对象，原地改会跨执行残留）"
    assert fm.model_options["model_function_wrapper"] is prev, "原件不许被改"
    wrap = patched.model_options["model_function_wrapper"]

    vid = _mk_ref(dense_t=12)
    cond = vpack.apply(_mk_cond([vid]), {"on": True, "ratio": "2/5"})
    payload = _mk_payload([cond[0][1]["minimax_refs"][0]])

    out = wrap(lambda *a, **k: "APPLIED",
               {"input": "X", "timestep": "T", "c": {"minimax_payload": payload}})
    assert out == "PREV", "已有 wrapper 必须被串联（别人先装的补丁不能被顶掉）"
    assert order == ["prev"]
    assert seen["args"]["input"] == "X" and seen["args"]["timestep"] == "T", \
        "其余 args 必须原样透传"
    assert seen["args"]["c"]["minimax_payload"] is payload, "同一个 payload 对象（就地修）"
    assert getattr(payload["layout"], vpack._MARKER, False) is True, \
        "wrapper 里必须真的修了 layout（不然抽了帧却没还原时间坐标）"


def test_install_wrapper_without_previous_calls_apply_model_directly():
    patched = vpack.install(_FakeModel(), {"on": True, "ratio": "2/5"})
    wrap = patched.model_options["model_function_wrapper"]
    got = {}

    def apply_model(x, t, **kw):
        got["x"], got["t"] = x, t
        return "OK"

    assert wrap(apply_model, {"input": 1, "timestep": 2, "c": {}}) == "OK"
    assert (got["x"], got["t"]) == (1, 2)


def test_patchable_guard():
    assert vpack.patchable(_FakeModel(), {"on": True, "ratio": "2/5"}) is True
    assert vpack.patchable(object(), {"on": True, "ratio": "2/5"}) is False
    assert vpack.patchable(None, {"on": True, "ratio": "2/5"}) is True, "模型未接不算问题"
    assert vpack.patchable(object(), {"on": False, "ratio": "2/5"}) is True, "未生效时恒 True（护栏不误报）"

    class NotH3:
        pass

    class Other:
        def __init__(self):
            self.model = NotH3()
        def get_model_object(self, _n):
            return self.model

    assert vpack.patchable(Other(), {"on": True, "ratio": "2/5"}) is False, \
        "非原生 MiniMaxH3Model → 装不上，报告必须点名（否则压了 token 却没修 RoPE）"


# ============================ ⑥ 设置读写 ============================

def test_settings_round_trip(tmp_path, monkeypatch):
    p = str(tmp_path / "vpack.json")
    monkeypatch.setattr(vpack, "settings_path", lambda: p)
    assert vpack.load_settings() == vpack.DEFAULT_VPACK, "文件不存在 → 出厂默认"
    assert vpack.save_settings({"on": True, "ratio": "3/5"}) == {"on": True, "ratio": "3/5"}
    assert vpack.load_settings() == {"on": True, "ratio": "3/5"}
    vpack.save_settings({"on": True, "ratio": "9/9", "zz": 1})
    assert vpack.load_settings() == {"on": True, "ratio": vpack.DEFAULT_RATIO}, "非法档位回落默认"
    open(p, "w", encoding="utf-8").write("{ 坏 JSON")
    assert vpack.load_settings() == vpack.DEFAULT_VPACK, "坏文件 → 默认，不抛"
    monkeypatch.setattr(vpack, "settings_path", lambda: "")
    assert vpack.save_settings({"on": True}) is None, "路径不可用 → None"


def test_settings_file_name_is_separate_from_refvid():
    assert vpack.FILE_NAME == "vpack.json"
    import refvideo
    assert refvideo.FILE_NAME != vpack.FILE_NAME, "两份设置必须分开存（重置一个不能顺手清掉另一个）"


# ============================ ⑦ 接线守卫（源码级） ============================

def test_nodes_applies_at_cond_birth_and_hands_cfg_to_upscale():
    src = _src("nodes.py")
    assert "cond = vpack.apply(cond, _vp_cfg, _vp_info)" in src, \
        "一采 cond 诞生点必须过 VPack"
    assert "vpack.install(模型, _vp_cfg)" in src, "一采采样模型必须装 RoPE 修正"
    assert "_vp_sample_model()" in src, "采样必须真的用装了补丁的那个模型对象"
    assert "vpack_cfg=_vp_cfg" in src, "二采必须拿到同一份配置（否则两份不同形状的条件）"


def test_upscale_applies_and_installs():
    src = _src("upscale.py")
    assert "cond = vpack.apply(cond, vpack_cfg)" in src, "二采 cond 诞生点必须过同一档位"
    assert "ks_model = vpack.install(ks_model, vpack_cfg)" in src, "二采采样模型也要装 RoPE 修正"


def test_seg_hash_and_ckpt_params_gated_on_effective():
    src = _src("nodes.py")
    assert 'tag = f"vp:{_vp_sig}|{tag}"' in src, "逐段指纹必须带 vp: 键（改档位→引用视频的段重做）"
    assert 'ckpt_params["vpack"] = {"ratio": _vp_ratio}' in src
    # 两道闸都必须走 vpack.signature / vpack.enabled（未生效不加键）
    assert "_vp_sig = vpack.signature(_vp_cfg)" in src
    assert "_vp_on = vpack.enabled(_vp_cfg)" in src


def test_nodes_reports_when_model_is_not_patchable():
    """非原生 H3 时 install 会原样返回（不抛）→ 必须用 patchable 在报告里点名，
    否则「压了 token 却没修 RoPE」是静默的。一采与二采两个槽位都要查。"""
    src = _src("nodes.py")
    assert "vpack.patchable" in src
    assert '"二采模型"' in src and "_up_model is not 模型" in src, \
        "二采模型是独立槽位，必须单独查（不能只查一采模型）"


def test_routes_registered_both_ways():
    src = _src("routes.py")
    assert '("GET", "/h3chain/vpack")' in src and '("POST", "/h3chain/vpack")' in src
    assert '"/h3chain/vpack", vpack_get' in src and '"/h3chain/vpack", vpack_set' in src


def test_frontend_api_surface():
    src = _src(os.path.join("web", "h3_api.js"))
    assert "vpackGet:" in src and "vpackSet:" in src
