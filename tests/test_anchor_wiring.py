"""手动锚定重构的**结构守卫**：钉住契约，防回退。

跑法（仓库根目录）：
    python -m pytest tests/test_anchor_wiring.py -q

为什么用源码/结构断言而不是行为断言：这套改动的核心是「七处碎片 → 一个 anchor」，
而真正的行为验证需要 ComfyUI + GPU（本机跑不了，见规划 §11）。行为验证落在规划
§10 的现场验收 6 条；这里守住的是**结构契约**——代码形状一旦退回旧结构立刻红，
比等到现场跑一遍才发现便宜得多。
"""

import ast
import os
import re as _re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


NODES = _src("nodes.py")
ANCHORS_SRC = _src("anchors.py")


# ---- 唯一入口：seg.anchors[] ----

def test_old_fragments_are_gone_from_nodes():
    """latent_ref / tail_src 的解析与消费必须全部消失（碎片收敛）。"""
    for dead in ("seg_latent_ref", "seg_tail_src", "_seg_tail_anchor", "_insert_hash",
                 "proj_inserts", "insert_specs"):
        assert dead not in NODES, f"旧碎片 {dead} 仍在 nodes.py 里"


def test_anchors_are_parsed_through_migration_and_validated():
    """读旧档走 migrate_legacy_seg，读新档走 anchors[]，且**硬校验**。"""
    assert "anchors.migrate_legacy_seg(" in NODES
    assert "anchors.validate_anchors(" in NODES
    assert "seg_anchors" in NODES


def test_anchors_are_serialised_into_segment_hash():
    """anchor 是内容变更：不进哈希就会出现「改了 anchor 不重做」。"""
    assert 'tag = ("anc:' in NODES


def test_change_intervals_drives_rebuild_instead_of_cascade():
    """内容变更走区间模型（标进重摇通道），不再用 reroll_start 截断整链。"""
    assert "anchors.change_intervals(" in NODES
    assert "checkpoint.reroll_start" not in NODES


# ---- 静默降级必须绝迹 ----

def test_load_library_latent_no_longer_falls_back():
    """latent 外源取不到必须抛，不能回落上段尾（设了锚不生效还不说 = 最坏一类 bug）。"""
    fn = ast.parse(NODES)
    node = _find_func_anywhere(fn, "_load_library_latent")
    body = ast.unparse(_without_docstring(node))
    assert "回落" not in body
    assert "return None" not in body
    assert "raise ValueError" in body


def test_manual_anchor_range_check_blocks_instead_of_reporting():
    """组装侧对手动锚做硬拦；旧的 audit_keyframes「只报不拦」不再用于主路径。"""
    assert "guides.audit_keyframes(" not in NODES
    assert 'raise ValueError(f"段{g + 1} 锚点越界' in NODES


# ---- _apply_guide 只收 keyframe 列表 ----

def test_apply_guide_takes_keyframe_list():
    fn = ast.parse(NODES)
    target = _find_func_anywhere(fn, "_apply_guide")
    args = [a.arg for a in target.args.args]
    assert args == ["cond", "keyframes", "sampled_fc"], args


def test_apply_guide_has_no_fragment_parameters():
    """guide/e1_windows/memory_kfs/head_kf/mid_kfs 这些碎片参数不该回来。"""
    fn = ast.parse(NODES)
    target = _find_func_anywhere(fn, "_apply_guide")
    assert target.args.defaults == []
    assert target.args.kwonlyargs == []


def test_apply_guide_call_sites_all_pass_three_args():
    tree = ast.parse(NODES)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_apply_guide"]
    assert calls, "主路径应当还在调 _apply_guide"
    for c in calls:
        assert len(c.args) == 3 and not c.keywords, ast.unparse(c)[:80]


# ---- 实验性功能框架已整体移除 ----

def test_experiment_framework_is_gone():
    """实验性功能已从前端到后端整体移除；只留「响度对齐强度」这一个普通控件。"""
    for dead in ("experiments.py", "bridge.py"):
        assert not os.path.exists(os.path.join(ROOT, dead)), f"{dead} 应当已删除"
    for dead in ("ExperimentContext", "experiments.resolve", "exp.has(", "soft_bridge",
                 "e3_motion_gate", "e4_transition_res", "audio_seam"):
        assert dead not in NODES, f"nodes.py 仍有实验残留：{dead}"
    assert '"响度对齐强度"' in NODES, "响度对齐强度 应当保留为普通控件"


def test_memory_anchor_storage_is_retired():
    ck = _src("checkpoint.py")
    for dead in ("memory_anchor_path", "save_memory_anchor", "load_memory_anchor"):
        assert f"def {dead}(" not in ck
    assert "memory_anchor" not in NODES


def test_experiment_field_no_longer_in_ckpt_params():
    """存档指纹里不再有 experiments 键（实验框架已删）。"""
    ck = _src("checkpoint.py")
    assert "experiments" not in ck
    assert 'ckpt_params["experiments"]' not in NODES


# ---- 源归一化的三段路 ----

def test_resolve_anchor_source_covers_three_outcomes():
    for token in ('"reuse"', '"encode"', "_REENCODE_HINT"):
        assert token in ANCHORS_SRC
    assert "分辨率不匹配" in ANCHORS_SRC


# ---- 跨模块依赖：不许按全局名调宿主的模块级函数 ----

def test_anchor_js_has_no_cross_module_global_calls():
    """h3d_anchor.js 不得按全局名调用 h3_director.js 的模块级函数。

    2026-09-16 线上故障：h3d_anchor.js 里 `typeof getDirValue !== "function"` 当场抛错，
    整块「⚠ 段落卡片渲染失败 · getDirValue 未加载」。
    根因：`getDirValue` / `setSegmentField` 都是 h3_director.js 的**模块级**函数，
    并没有挂到 window 上（公开面只有 H3Api / H3Assets / H3Latent / H3Lib /
    H3Prompts / H3Anchor），跨模块按全局名调必然失败。
    正确做法：宿主通过 `buildAnchorPanel({ dir, setAnchors, refresh })` 注入访问器。
    """
    code = _js_code(_src("web/h3d_anchor.js"))
    for name in ("getDirValue", "setSegmentField", "scheduleRefresh",
                 "projDir", "setSegAnchors"):
        assert name not in code, f"h3d_anchor.js 又在按全局名调 {name}（应改为宿主注入）"


def test_director_injects_anchor_host_accessors():
    """接线必须真的传 dir / setAnchors / frameLen，否则面板一渲染就抛。"""
    src = _src("web/h3_director.js")
    i = src.index("window.H3Anchor.buildAnchorPanel(")
    call = src[i:i + 1200]
    assert "dir:" in call, "buildAnchorPanel 未注入 dir"
    assert "setAnchors:" in call, "buildAnchorPanel 未注入 setAnchors"
    assert "frameLen:" in call, "buildAnchorPanel 未注入 frameLen（本段总帧数）"


def test_anchor_js_has_no_load_order_requirement():
    """模块不得要求「必须先于/后于某文件加载」——那是设计缺陷，不是约定。"""
    src = _src("web/h3d_anchor.js")
    for bad in ("必须先于本模块", "后于本模块"):
        assert bad not in src


def test_anchor_js_exports_only_namespaced_api():
    """只挂 window.H3Anchor，不往全局摊函数（否则又是一套隐式契约）。"""
    src = _src("web/h3d_anchor.js")
    assert "window.H3Anchor = {" in src
    assert not _re.search(r"^\s*window\.\w+\s*=\s*function", src, _re.M)


def _js_code(src):
    """粗剥 JS 注释（块注释 + 行注释），用于「这个名字只许出现在注释里」这类静态守卫。

    实现刻意简单：本仓库 web/ 下的脚本没把 `//` 写进字符串（URL 都是 /h3chain/…
    单斜杠），所以按行剥即可。若哪天出现 `http://` 这类字面量，这里会多剥一点
    ——只会让守卫变宽松，不会误报。
    """
    out, in_block = [], False
    for ln in src.split("\n"):
        s = ln.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block = True
            continue
        if s.startswith("*"):
            continue
        out.append(ln.split("//")[0] if "//" in ln and "://" not in ln else ln)
    return "\n".join(out)


def _func_src(src, name):
    """按缩进切出一个函数体（routes.py 是嵌套注册，def 都在 4 空格缩进下）。

    用来做「这个函数里不许出现 X」这类守卫：不切范围的话，routes.py 别处
    合法用到的同名符号会把守卫变成误报。
    """
    for head in (f"    async def {name}(", f"    def {name}("):
        i = src.find(head)
        if i < 0:
            continue
        nxt = min(x for x in (src.find("\n    async def ", i + 1),
                              src.find("\n    def ", i + 1)) if x > 0)
        return src[i:nxt]
    raise AssertionError(f"routes.py 里找不到 {name}")


def _find_func_anywhere(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _without_docstring(node):
    """剥掉 docstring——注释里可以解释"曾经怎样"，代码里不行。"""
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body

# ---- 渲染所有权：面板必须自己刷新自己 ----

def test_anchor_panel_owns_its_own_rendering():
    """本地改动只许走本地重建，**不许**触发宿主全量重刷。

    2026-09-16 症状：点「新增锚定」后轨道不出现，要退出导演台再进才有。
    根因：commit() 每次都调宿主的 refresh()，而它**全量重刷导演台**，把本面板刚
    渲染的 DOM 连同正在进行的异步填充一起推倒——异步结果落在已丢弃的节点上。
    rebuildCard() 本地重建逻辑本来就是对的，只是没被用在这条路径上。

    现在只保留 1 处宿主 refresh 调用：rebuildCard 找不到卡片时的兜底。
    """
    code = _js_code(_src("web/h3d_anchor.js"))
    n = code.count("(ctx.refresh || (() => {}))()")
    assert n == 1, f"面板又有多余的宿主全量重刷调用（{n} 处，应只有 1 处兜底）"
    assert "ctx.__rebuildCard" in code, "本地重建能力未挂到 ctx，commit 无法自行刷新"


# ---- 来源只有两类：段 / 素材（prev_tail 退出 UI） ----

def test_anchor_source_kinds_are_only_segment_and_asset():
    """来源下拉只许两类：「段」= 本项目落盘的 seg_NNN；「素材」= 素材库里挑。

    用户 2026-09-16 明确：prev_tail 冗余——「有上段的选择就行了」。后端保留
    prev_tail 作隐式默认段首桥（没有显式 head anchor 时才走），只是 UI 不再暴露。
    旧存档里已有的 prev_tail 锚作为只读项回显，不得再变回一个可选项。
    """
    src = _js_code(_src("web/h3d_anchor.js"))
    assert '[["segment", "段"], ["asset", "素材"]]' in src, "来源下拉的两类选项被改动"
    assert '"prev_tail（旧格式）"' in src or "上段尾（旧格式）" in src, \
        "旧存档的 prev_tail 锚需要只读回显（否则用户看不懂那一行是什么）"
    assert 'kind: "prev_tail"' not in src, "新建锚又默认成 prev_tail 了（应默认「段」+ 上一段）"


def test_anchor_default_prefers_previous_segment():
    """新建锚预填「上一段」：拿到的就是 prev_tail 那份 latent，不填则用户自己挑。"""
    src = _js_code(_src("web/h3d_anchor.js"))
    assert 'kind: "segment", ref: (prev && prev.ref) || ""' in src
    assert "prevSegment" in src


# ---- 素材一律从素材库选，不自己枚举 ----

def test_anchor_assets_come_from_the_library_module():
    """锚源素材必须走现有素材库（h3_library.js 的挑选模式），不另造枚举。"""
    src = _js_code(_src("web/h3d_anchor.js"))
    assert "window.H3Lib" in src and "pickKinds" in src, "锚定面板没有接素材库挑选模式"
    assert "/h3chain/anchor_sources?dir=" in src, "锚源清单接口被改名/改形"
    # 只看 anchor_sources 这一个函数的函数体：routes.py 别处用 get_input_directory 是
    # 另外的接口（素材上传等），不该被这条守卫牵连。
    body = _func_src(_src("routes.py"), "anchor_sources")
    for dead in ("_probe_media_meta", "_VIDEO_EXTS", "get_input_directory", "os.listdir("):
        assert dead not in body, f"anchor_sources 又在自己枚举素材（{dead}）"


def test_anchor_sources_resolves_one_ref_meta():
    """选完素材要由后端确认那**一个**文件的帧数/尺寸/帧率（面板刻度与体检的输入）。

    这里只允许单文件元信息探测：整目录枚举是错的（那等于另造一个素材库，
    与全局库/项目库两套数据必然漂移）。
    """
    routes = _src("routes.py")
    assert "def _source_meta(" in routes
    assert 'q.get("ref")' in routes
    assert "def _anchor_ref_of(" in routes
    assert "resolve_item_path(" in routes


def test_library_module_has_a_pick_mode():
    """素材库要能当选择器复用（一个浏览器，不是两个）。"""
    lib = _js_code(_src("web/h3_library.js"))
    assert "typeof o.onPick === \"function\"" in lib
    assert "S.pick" in lib
    assert "onPick:" in _src("web/h3d_anchor.js")


# ---- 本段总帧数：一个数只有一个算法 ----

def test_segment_frames_has_a_single_source():
    """段卡标题的「≈N帧」与锚定面板的目标轨刻度必须同源。

    面板原先自己又算一遍，而且用的是**向上对齐**——后端 nodes._snap_seconds 是
    **就近**吸附，于是目标轨总帧数和分段时长对不上，用户以为面板坏了。
    """
    d = _src("web/h3_director.js")
    a = _js_code(_src("web/h3d_anchor.js"))
    assert "function segmentFrames(node, seg)" in d, "唯一换算函数不见了"
    assert "frameLen: () => segmentFrames(node, data.ds.segments[it.idx])" in d, \
        "宿主没有把本段帧数注入锚定面板"
    assert "frameLen" in a and "snapFramesUp" not in a, \
        "面板又开始自己算帧数（向上对齐）了"
    assert "segFramesUp" not in a and "× 24" not in a.replace("secs * 24", ""), \
        "面板里出现第二套秒→帧换算"


def test_segment_metadata_is_written_with_the_prologue_offset():
    """「上段尾」槽位必须带上序章偏移，否则有序章的项目会指到序章上。"""
    routes = _src("routes.py")
    assert "prev_slot = seg_no - 2 + (1 if man.get(\"has_prologue\") else 0)" in routes


# ---- 段源 ref 的规范形式 ----

def test_segment_source_ref_form_is_seg_nnn():
    """段源 ref 的规范形式是 `seg_NNN`（anchors.py 文档 / routes / 测试三处一致）。

    nodes.py 曾要求以 `.pt` 结尾、并按 `_pn[4:-3]` 切片取段号，于是面板里
    **选出来的段源必然报错**——ref 从哪儿来都对不上。
    """
    assert "须为 seg_NNN 形式" in NODES
    assert "int(_pn[4:-3])" not in NODES, "又按 .pt 后缀切片取段号了"
    assert "seg_003" in _src("tests/test_anchors.py")


# ---- 「设置」页：按作用面分区 + 落盘策略收敛成人话 ----

def test_settings_pane_is_grouped_by_effect():
    """设置页分两区并给小标题：影响本段生成 / 只影响落盘。

    原先这几块平铺、权重一样，看不出改哪个会改变出片、改哪个只是省磁盘。
    """
    d = _src("web/h3_director.js")
    assert "影响本段生成" in d and "只影响落盘" in d
    assert "h3d-setsec-title" in d and ".h3d-setsec{" in d, "分区样式没跟上"
    assert "secGen.append(anchorWrap)" in d, "锚定面板没挂进「影响本段生成」区"
    assert "secDisk.append(lsBox)" in d, "latent 保存没挂进「只影响落盘」区"


def test_latent_save_is_three_human_options():
    """latent 落盘策略收敛成三选，后端字段不平铺给用户看。

    底层是 {mode: all|range|tail|off, start_f, end_f, tail_f, split_av, save_seg,
    save_all}；界面上只该有「存全段 / 只存尾部 N 帧 / 不存」。
    旧档的 range / split_av 不再有编辑入口，但必须留提示行说清它还在生效——
    否则用户会以为改选项才生效、旧设置被静默丢弃。
    """
    d = _src("web/h3_director.js")
    assert '["follow", "存全段（默认）"], ["tail", "只存尾部"]' in d
    assert '"off", "不存（用完即弃）"' in d
    # 旧控件（起始帧 / 结束帧 / 分开存开关）不得作为控件回归
    assert d.count("起始帧") == 0 and d.count("结束帧") == 0, "后端帧号字段又铺回界面了"
    assert d.count("图像/音频分开存") == 1, "分开存开关又回来了（只该在旧设置提示里出现一次）"
    assert "界面上不再提供这两项" in d, "旧设置提示行被删，用户会以为旧配置失效了"


def test_video_anchor_goes_through_the_asset_registry():
    """视频锚源必须与图片同一条寻址通路（注册表 -> 绝对路径）。

    原先 kind="video" 写死走 _load_input_video（只认 ComfyUI input 目录），
    项目 assets/ 与素材库里的视频根本接不进来。
    """
    assert "def _anchor_asset(" in NODES
    assert "def _anchor_video(" in NODES
    assert "_anchor_video(ref)" in NODES
    assert "_imgs, _aud = _load_input_video(ref" not in NODES


# ---- 2026-09-17 修复回归：跟随全局反向化 / 音频锚源 / 刷新合并 / 分段时间同步 ----

def test_follow_global_buttons_are_replaced_by_skip_checkboxes():
    """「跟随全局」按钮 + 正向勾选的三件套反向成「跳过」勾选：勾=跳过，不勾=跟全局。"""
    d = _src("web/h3_director.js")
    assert "跳过自动引用上段" in d and "跳过自动按序生成" in d
    assert 'el("button", "h3d-btn", "跟随全局")' not in d, "「跟随全局」按钮应已删除"
    assert '"auto_ref", refCb.checked ? false : null)' in d, "不勾应写 null（跟随全局）"
    assert '"auto_seq", seqCb.checked ? false : null)' in d


def test_segment_seconds_change_rebuilds_anchor_pane():
    """分段时间变化必须写透 ds 快照并重建锚定面板，否则目标轨总帧数停在旧值。"""
    d = _src("web/h3_director.js")
    assert "rebuildAnchorPane" in d
    assert "rebuildAnchorPane()" in d, "seconds change 后没有重建锚定面板"
    assert "wasOpen" in d and "anchorBox.open = true" in d, "重建后要继承展开态（否则改秒数就自动收起）"
    assert "number spinners 应隐藏" or True
    assert "::-webkit-inner-spin-button" in d, "数字输入的上下小箭头没隐藏"


def test_anchor_sources_merge_existing_refs_and_support_audio():
    """刷新后已有锚的 ref 要回传后端合并元信息（修「源不在源清单里」误报）；音频可作锚源。"""
    a = _src("web/h3d_anchor.js")
    assert "ASSET_REF_KINDS" in a and 'refs.forEach' in a, "loadSources 没有合并已有锚 ref"
    assert '"image", "video", "latent", "audio"' in a, "素材库挑选没放开音频"
    r = _src("routes.py")
    assert 'q.getall("ref", [])' in r and '"missing": missing' in r
    # multidict 的 getall 缺 key 直接抛 KeyError → aiohttp 里就是一个空 500
    assert 'q.getall("ref")' not in r, "getall 必须给默认值，否则首次加载（无 ref）直接 500"
    assert "_anchor_sources_impl" in r and "读取源清单失败" in r, "缺异常兜底外壳"
    # 音频元信息：时长折成等效帧，前端才能画源轨时间线
    assert 'x.type == "audio"' in r and '"duration"' in r
    assert 'item_kind not in ("image", "video", "latent", "audio")' in r


def test_segment_source_is_prev_only_without_item_dropdown():
    """「段」源固定上一段：去掉条目下拉，无上段在体检区硬提示。"""
    a = _src("web/h3d_anchor.js")
    assert "不存在可用的「上段」" in a
    assert 'el("span", "h3d-secs-hint", "条目")' not in a, "段源的条目下拉应已删除"


def test_audio_source_has_its_own_timeline_in_src_track():
    """音频源没有画面：源轨要有波形占位 + 播放器，信息行按等效帧/秒显示（不是 fps）。"""
    a = _src("web/h3d_anchor.js")
    assert ".h3d-wave" in a and "h3d-audio" in a
    assert "isAudio" in a and 'secTxt = (span / 24).toFixed(2)' in a
    assert 'fps == null && !isAudio' in a, "音频不该再弹 fps 手填框"


def test_segment_settings_tab_is_named_anchor_settings():
    d = _src("web/h3_director.js")
    assert '["set", "锚定设置"]' in d
    assert "🎬 锚定设置" in d
    """段中/段尾锚的分支三态真实生效（此前只有段首桥尊重）；纯音频锚有独立通路。"""
    n = _src("nodes.py")
    assert "_anchor_audio" in n
    assert '_want_v = _a["branches"]["av"] in ("both", "video")' in n
    assert "video_latent=_av if _want_v else None" in n
    assert "音频源只能作「仅音频」" in n or "音频源只支持「仅音频」" in n
