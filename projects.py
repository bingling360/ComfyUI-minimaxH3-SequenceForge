"""游戏式项目存档：output/h3_projects/ 下一个项目一个文件夹。

list_projects 扫描磁盘实时列项目（不依赖索引文件或前端 localStorage，
永不失真）；delete_project 删项目 = 删整个文件夹（视频/提示词/latent 全删）；
merge_project 按序合并若干段/成片/外部视频 -> merged_*.mp4（只追加产物，
不动链、不动段存档）。
本模块不导入 routes（routes 单向导入本模块，避免循环依赖）。
"""

import json
import os
import shutil
import time

from . import checkpoint


MANIFEST_SCHEMA = "h3seamless/manifest-v2.1"


def _ensure_revision(manifest: dict) -> dict:
    """老 manifest 懒迁移：缺 revision/schema_version 则补 1，不重写 ID。"""
    if not isinstance(manifest, dict):
        return manifest
    if not isinstance(manifest.get("revision"), int):
        manifest["revision"] = 1
    if not manifest.get("manifest_schema"):
        manifest["manifest_schema"] = MANIFEST_SCHEMA
    return manifest


def safe_name(name) -> str:
    """项目目录名安全校验：拒空、路径分隔、盘符、与点开头（防目录穿越）。"""
    s = str(name or "").strip()
    if not s or s.startswith(".") or "/" in s or "\\" in s or ":" in s or ".." in s:
        return ""
    return s


def _cover(root: str, manifest: dict) -> str:
    """第一张实际存在的段缩略图文件名（列表里可能有空串占位）。"""
    for name in (manifest.get("thumbs") or []):
        if name and os.path.isfile(os.path.join(root, name)):
            return name
    return ""


def list_projects() -> list:
    """扫描项目根目录 -> 摘要列表（按 updated_at 倒序）。

    无 manifest.json 或 manifest 损坏的目录直接跳过（不是本项目系统的产物）。
    """
    root = checkpoint.projects_root()
    if not os.path.isdir(root):
        return []
    out = []
    for name in os.listdir(root):
        pdir = os.path.join(root, name)
        if not os.path.isdir(pdir) or not safe_name(name):
            continue
        try:
            with open(os.path.join(pdir, "manifest.json"), "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            continue
        if not isinstance(manifest, dict):
            continue
        updated = manifest.get("updated_at")
        if not isinstance(updated, (int, float)):
            try:
                updated = os.path.getmtime(os.path.join(pdir, "manifest.json"))
            except OSError:
                updated = 0
        out.append({
            "dir": name,
            "title": manifest.get("title") or name,
            "done": int(manifest.get("done") or 0),
            "total": int(manifest.get("total") or 0),
            "updated_at": updated,
            "cover": _cover(pdir, manifest),
            "finals": list(manifest.get("finals") or []),
            "merges": [m.get("file") for m in (manifest.get("merges") or [])
                       if isinstance(m, dict) and m.get("file")],
            "params": manifest.get("params") or {},
            "revision": int(manifest.get("revision") or 1),
            "manifest_schema": manifest.get("manifest_schema") or MANIFEST_SCHEMA,
            "seg_count": len(manifest.get("prompts") or []),
        })
    out.sort(key=lambda p: p["updated_at"] or 0, reverse=True)
    return out


def list_projects_summary(page: int = 1, size: int = 50) -> dict:
    """轻量摘要分页：M1 新增，前端列表只拿摘要，详情走 read_project。"""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        size = min(200, max(1, int(size)))
    except (TypeError, ValueError):
        size = 50
    all_items = list_projects()
    total = len(all_items)
    start = (page - 1) * size
    items = []
    for p in all_items[start:start + size]:
        items.append({
            "dir": p["dir"], "title": p["title"],
            "revision": p["revision"], "seg_count": p["seg_count"],
            "done": p["done"], "total": p["total"],
            "updated_at": p["updated_at"], "cover": p["cover"],
        })
    return {"items": items, "total": total, "page": page, "size": size}


def read_project(name: str):
    """读单个项目 manifest 全文；不存在/非法返回 None。"""
    if not safe_name(name):
        return None
    root = os.path.join(checkpoint.projects_root(), safe_name(name))
    manifest = checkpoint.load_manifest(root)
    if not isinstance(manifest, dict):
        return None
    return _ensure_revision(manifest)


_ASSET_KINDS = ("image", "video", "audio")
_ASSET_LABEL_MAX = 24
_ASSET_TOTAL_MAX = 999


def _clean_asset(raw) -> dict | None:
    """资产条目白名单清洗：{label,kind,file}；总量不限（999封顶防膨胀），单段上限执行期卡。"""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "image").strip()
    if kind not in _ASSET_KINDS:
        kind = "image"
    label = str(raw.get("label") or "").strip()[:_ASSET_LABEL_MAX]
    file = str(raw.get("file") or "").strip().replace("\\", "/")
    if not file or not label:
        return None
    # 文件名防穿越：允许至多两级子目录，每段过 safe_name
    parts = [p for p in file.split("/") if p and p != "."]
    if not parts or len(parts) > 2 or not all(safe_name(p) for p in parts):
        return None
    if not os.path.splitext(parts[-1])[1]:
        return None
    return {"label": label, "kind": kind, "file": "/".join(parts)}


def _dedupe_assets(items: list) -> list:
    """按 label 去重（首个为准），保持入库顺序。"""
    seen, out = set(), []
    for a in items:
        c = _clean_asset(a)
        if c is None or c["label"] in seen:
            continue
        seen.add(c["label"])
        out.append(c)
    return out[:_ASSET_TOTAL_MAX]


def save_assets(name: str, assets, base_revision=None):
    """资产库持久化：manifest["assets"] 全量覆盖写，revision+1，乐观锁可选。

    总量不限（999封顶），单段 9/3/3 不在这里卡（执行期按段卡）。
    目录或 manifest 不存在返回 None；base_revision 不一致抛 ValueError(REVISION_CONFLICT)。
    """
    name = safe_name(name)
    if not name or not isinstance(assets, list):
        return None
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        return None
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    manifest["assets"] = _dedupe_assets(assets)
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest


_SEG_STR_FIELDS = ("scene_prompt", "character_prompt", "soundscape", "music")


def _clean_seg_field(raw) -> dict | None:
    """前端分段字段白名单清洗：未知键剔除、类型收敛（防脏 JSON 膨胀 manifest）。

    M2.5：透存 prompt_v2（具象化结构，经 prompts.clean_prompt 收敛，失败则丢弃该键不炸链）。
    """
    if not isinstance(raw, dict):
        return None
    out = {k: (str(raw[k]) if isinstance(raw.get(k), str) else "") for k in _SEG_STR_FIELDS}
    sec = raw.get("seconds")
    out["seconds"] = float(sec) if isinstance(sec, (int, float)) and sec > 0 else None
    out["unlink"] = bool(raw.get("unlink"))
    out["disabled"] = bool(raw.get("disabled"))
    refs = raw.get("refs")
    out["refs"] = [str(x) for x in refs if isinstance(x, (str, int))][:64] \
        if isinstance(refs, list) else []
    fr = raw.get("frame_refs")
    out["frame_refs"] = [str(x) for x in fr if isinstance(x, (str, int))][:4] \
        if isinstance(fr, list) else None
    pv = raw.get("prompt_v2")
    if isinstance(pv, dict):
        try:
            from . import prompts as _prompts
        except ImportError:
            import prompts as _prompts
        try:
            out["prompt_v2"] = _prompts.clean_prompt(pv)
        except Exception:
            pass
    # v2 手动模式（null=跟随导演台）：白名单透存，未知值归 null
    vm = raw.get("v2mode")
    out["v2mode"] = vm if isinstance(vm, str) and vm in (
        "T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA") else None
    # M3：每段 latent 保存策略透存 {mode: all|range|tail|off, start_f, end_f, tail_f}
    ls = raw.get("latent_save")
    if isinstance(ls, dict):
        mode = str(ls.get("mode") or "all").strip()
        if mode not in ("all", "range", "tail", "off"):
            mode = "all"
        try:
            sf = int(ls.get("start_f", 0))
        except (TypeError, ValueError):
            sf = 0
        try:
            ef = int(ls.get("end_f", 0))
        except (TypeError, ValueError):
            ef = 0
        try:
            tf = int(ls.get("tail_f", 0))
        except (TypeError, ValueError):
            tf = 0
        out["latent_save"] = {"mode": mode, "start_f": max(0, sf),
                              "end_f": max(0, ef), "tail_f": max(0, tf)}
    return out


def _latent_dir(root: str) -> str:
    d = os.path.join(root, "latent")
    os.makedirs(d, exist_ok=True)
    return d


def _clean_latent_name(name) -> str:
    s = str(name or "").strip().replace("\\", "/").split("/")[-1]
    if not safe_name(s) or not os.path.splitext(s)[1]:
        return ""
    if not s.endswith(".pt"):
        s += ".pt"
    return s


def slice_latent(name, src, start_f, end_f, save_name, base_revision=None):
    """latent-to-latent 剪辑：段存档 .pt / 已登记 latent 按像素帧窗切片 -> latent/<save_name>.pt。

    M3 三源之一（段范围/尾帧/已存 latent 再剪），纯 torch 无需 VAE：
    video [B,C,T,H,W] 按 frames_to_latent_t 换算 token 窗整 token 切，
    audio [1,32,2,T] 按 audio_tokens_for_frames 比例切。越界/切空抛 ValueError。
    src: {"seg": 0-based段文件号} | {"file": "latent/x.pt"}。登记 manifest["latents"] 并 revision+1。
    """
    import torch
    try:
        from .grid import frames_to_latent_t, latent_t_to_frames, audio_tokens_for_frames
    except ImportError:
        from grid import frames_to_latent_t, latent_t_to_frames, audio_tokens_for_frames
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    if not isinstance(src, dict):
        raise ValueError("src 须为 {seg: 段号} 或 {file: latent/x.pt}")
    if "seg" in src:
        try:
            idx = int(src["seg"])
        except (TypeError, ValueError):
            raise ValueError(f"段号必须是整数：{src.get('seg')!r}") from None
        if idx < 0:
            raise ValueError("段号从 0 开始（0-based 段文件号）")
        src_path = os.path.join(root, f"seg_{idx:03d}.pt")
        src_desc = f"seg_{idx:03d}.pt"
    else:
        f = str(src.get("file") or "").strip().replace("\\", "/")
        parts = [p for p in f.split("/") if p and p != "."]
        if len(parts) != 2 or parts[0] != "latent" or not safe_name(parts[1]):
            raise ValueError(f"非法 latent 文件：{f!r}（须为 latent/<名>.pt）")
        src_path = os.path.join(root, *parts)
        src_desc = "/".join(parts)
    if not os.path.isfile(src_path):
        raise ValueError(f"源 latent 不存在：{src_desc}（该段还没生成存档？）")
    try:
        sf, ef = int(start_f), int(end_f)
    except (TypeError, ValueError):
        raise ValueError("start_f/end_f 须为整数像素帧") from None
    if ef <= sf or sf < 0:
        raise ValueError(f"帧窗为空（[{sf}, {ef})，须 0 <= start < end）")
    clean = _clean_latent_name(save_name)
    if not clean:
        raise ValueError(f"非法保存名：{save_name!r}")
    with open(src_path, "rb") as fh:
        payload = torch.load(fh, map_location="cpu", weights_only=True)
    video, audio = payload.get("video"), payload.get("audio")
    if video is None or getattr(video, "dim", lambda: 0)() != 5:
        raise ValueError("源 latent 视频张量形态异常（须 5 维 [B,C,T,H,W]）")
    total_t = int(video.shape[2])
    total_frames = latent_t_to_frames(total_t)
    if sf >= total_frames:
        raise ValueError(f"start_f {sf} 越界（源共约 {total_frames} 帧）")
    ef = min(ef, total_frames)
    t0 = max(0, frames_to_latent_t(sf, up=False))
    t1 = min(total_t, frames_to_latent_t(ef, up=True))
    if t1 <= t0:
        # 帧窗落在同一 token 内：按整 token 取 1 个（含目标帧），不返回空
        t0 = max(0, min(total_t - 1, frames_to_latent_t(sf, up=True) - 1))
        t1 = min(total_t, t0 + 1)
    cut_v = video[:, :, t0:t1].contiguous().clone()
    cut_a = None
    if audio is not None and getattr(audio, "dim", lambda: 0)() == 4:
        a_total = int(audio.shape[-1])
        a0 = max(0, int(round(sf / max(1, total_frames) * a_total)))
        a1 = max(a0 + 1, min(a_total, int(round(ef / max(1, total_frames) * a_total))))
        cut_a = audio[..., a0:a1].contiguous().clone()
    _ = audio_tokens_for_frames(ef - sf)  # 换算自检（比例异常时不断言，只记录）
    out_payload = {"video": cut_v}
    if cut_a is not None:
        out_payload["audio"] = cut_a
    elif audio is not None:
        out_payload["audio"] = audio
    ldir = _latent_dir(root)
    tmp_path = os.path.join(ldir, clean + ".part")
    final_path = os.path.join(ldir, clean)
    import tempfile
    buf = tempfile.SpooledTemporaryFile(max_size=64 << 20)
    torch.save(out_payload, buf)
    buf.seek(0)
    with open(tmp_path, "wb") as fh:
        fh.write(buf.read())
    buf.close()
    os.replace(tmp_path, final_path)
    manifest.setdefault("latents", [])
    manifest["latents"] = [x for x in manifest["latents"]
                           if isinstance(x, dict) and x.get("file") != f"latent/{clean}"]
    manifest["latents"].append({"file": f"latent/{clean}", "src": src_desc,
                                "start_f": sf, "end_f": ef,
                                "tokens": [t0, t1], "updated_at": time.time()})
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest


def delete_latent(name, file, base_revision=None):
    """删除 latent 登记文件（幂等）。"""
    name = safe_name(name)
    f = str(file or "").strip().replace("\\", "/")
    parts = [p for p in f.split("/") if p and p != "."]
    if not name or len(parts) != 2 or parts[0] != "latent" or not safe_name(parts[1]):
        raise ValueError(f"非法 latent 文件：{file!r}")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在")
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    try:
        os.remove(os.path.join(root, *parts))
    except OSError:
        pass
    manifest["latents"] = [x for x in (manifest.get("latents") or [])
                           if not (isinstance(x, dict) and x.get("file") == f)]
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    checkpoint.save_manifest(root, manifest)
    return manifest


def trim_asset(name, src_file, start_s, end_s, save_name=None, base_revision=None,
               fps=24, crf=20):
    """资产/成片入出点裁剪：项目内 mp4 [start_s, end_s) -> 新文件（重编码）。

    M3 最小剪辑集：资产库行内剪 + 成片库片段截取共用。长耗时放调用方线程池。
    src_file 须为项目内 mp4（seg_/final_/merged_/clip_*.mp4）；save_name 缺省
    clip_<stamp>.mp4。登记 manifest["clips"] 并 revision+1。失败抛 ValueError/RuntimeError。
    """
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    f = str(src_file or "").strip().replace("\\", "/")
    parts = [p for p in f.split("/") if p and p != "."]
    if len(parts) != 1 or not safe_name(parts[0]) or not parts[0].endswith(".mp4"):
        raise ValueError(f"非法源文件：{src_file!r}（须为项目内 xxx.mp4）")
    src_path = os.path.join(root, parts[0])
    if not os.path.isfile(src_path):
        raise ValueError(f"源文件不存在：{parts[0]}")
    try:
        ss, es = float(start_s), float(end_s)
    except (TypeError, ValueError):
        raise ValueError("start_s/end_s 须为数字秒") from None
    if es <= ss or ss < 0:
        raise ValueError(f"裁剪区间为空（[{ss}s, {es}s)，须 0 <= start < end）")
    if save_name:
        clean = str(save_name).strip().replace("\\", "/").split("/")[-1]
        if not safe_name(clean):
            raise ValueError(f"非法保存名：{save_name!r}")
        if not clean.endswith(".mp4"):
            clean += ".mp4"
    else:
        clean = f"clip_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        k = 2
        while os.path.exists(os.path.join(root, clean)):
            clean = f"clip_{time.strftime('%Y%m%d_%H%M%S')}_{k}.mp4"
            k += 1
    try:
        from . import media
    except ImportError:
        import media
    ok = media.trim_av_mp4(src_path, os.path.join(root, clean), ss, es,
                           fps=fps, crf=crf)
    if not ok:
        raise RuntimeError(f"裁剪编码失败：{media.last_error}")
    fresh = checkpoint.load_manifest(root)
    if isinstance(fresh, dict):
        manifest = fresh
        _ensure_revision(manifest)
    manifest.setdefault("clips", []).append({"file": clean, "src": parts[0],
                                             "start_s": ss, "end_s": es,
                                             "updated_at": time.time()})
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest


def save_prompts(name: str, prompts, segments=None, base_revision=None):
    """把导演台当前提示词组回写进项目 manifest（提示词的持久源=项目文件夹）。

    只更新 prompts / seg_fields / total / updated_at / revision（及 done 超界钳制），不动
    params / seeds / prompt_hashes / finals / merges——运行时的重做判定依旧按
    节点控件提示词 vs prompt_hashes 逐段比对，改词段落照常自动重做。
    manifest.prompts 与磁盘段文件按全局槽位对齐（前端卡片/合并按此索引）：
    序章项目自动补回「序章」占位头；已有插入视频段在对应槽位补回
    「[插入视频] 文件名」占位行并计入 total——纯提示词回写不能让槽位错位。
    segments 为分段处理字段（场景/角色/环境音/配乐/时长/独立镜头/不上链/
    参考标签/首尾帧引用），与 prompts 同序（仅提示词段），存为
    manifest["seg_fields"] 并按全局槽位对齐（序章/插入槽为 null）——
    切换项目时前端据此还原各段卡片，不再丢分段字段。
    目录或 manifest 不存在（未跑过的指纹目录）返回 None，调用方按无项目跳过。
    base_revision 非空时做乐观锁：与当前 revision 不一致抛 ValueError(REVISION_CONFLICT)，
    调用方映射 409。M1 新增，向后兼容（不传即不检查）。
    """
    name = safe_name(name)
    if not name or not isinstance(prompts, list):
        return None
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        return None
    _ensure_revision(manifest)
    if base_revision is not None:
        try:
            br = int(base_revision)
        except (TypeError, ValueError):
            raise ValueError("无效的 base_revision（须为整数）")
        if int(manifest.get("revision") or 1) != br:
            raise ValueError(
                f"REVISION_CONFLICT: server={int(manifest.get('revision') or 1)} client={br}")
    seg_prompts = [str(p) for p in prompts][:64]
    seg_meta = [_clean_seg_field(x) for x in segments] \
        if isinstance(segments, list) else []
    while len(seg_meta) < len(seg_prompts):
        seg_meta.append(None)
    off = 1 if manifest.get("has_prologue") else 0
    ins_map = {}
    for x in (manifest.get("inserts") or []):
        if isinstance(x, dict) and x.get("file"):
            try:
                ins_map[int(x["slot"])] = str(x["file"])
            except (TypeError, ValueError):
                continue
    rows, fields, si = [], [], 0
    for g in range(off + len(seg_prompts) + len(ins_map)):
        if g == 0 and off:
            rows.append("「序章（上传视频）」")
            fields.append(None)
        elif g in ins_map:
            rows.append(f"[插入视频] {ins_map[g]}")
            fields.append(None)
        else:
            rows.append(seg_prompts[si])
            fields.append(seg_meta[si])
            si += 1
    manifest["prompts"] = rows
    # 全空（无任何分段字段）不写键：旧 manifest 结构零变化，体积零增长
    if any(x is not None for x in fields):
        manifest["seg_fields"] = fields
    else:
        manifest.pop("seg_fields", None)
    manifest["total"] = len(rows)
    manifest["done"] = min(int(manifest.get("done") or 0), manifest["total"])
    manifest["updated_at"] = time.time()
    manifest["revision"] = int(manifest.get("revision") or 1) + 1
    manifest["manifest_schema"] = MANIFEST_SCHEMA
    checkpoint.save_manifest(root, manifest)
    return manifest


def create_project(name: str):
    """新建项目：当场建文件夹 + 写初始 manifest（0 段草稿态），列表立即可见。

    游戏存档槽语义——此前文件夹要等首次运行 ckpt_dir() 才建、manifest 要等
    首段采样完才写，点完「新建项目」磁盘上什么都没有，项目也不在列表里。
    params 留空：首跑 assert_match 对空旧档按「沿用当前值」放行（旧档缺键
    视为一致），跑完由真实参数覆写；title/created_at 首跑会被继承保留。
    已存在（含跑过一段以上的正式项目）直接幂等返回现 manifest，不重写。
    """
    name = safe_name(name)
    if not name:
        return None
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is not None:
        return _ensure_revision(manifest)
    os.makedirs(root, exist_ok=True)
    now = time.time()
    manifest = {
        "schema": checkpoint.SCHEMA, "manifest_schema": MANIFEST_SCHEMA,
        "revision": 1, "done": 0, "total": 0,
        "title": name, "created_at": now, "updated_at": now,
        "prompts": [], "params": {}, "finals": [],
    }
    checkpoint.save_manifest(root, manifest)
    return manifest


def delete_project(name: str):
    """删除项目 = 删除整个项目文件夹；被删的是当前链时清掉状态指针。

    返回被删目录的相对路径（output 内）；目录不存在返回 None。
    抛出的 OSError（如文件被占用）由调用方（路由层）捕获反馈。
    """
    name = safe_name(name)
    if not name or name == "h3chain_state.json":
        return None
    root = checkpoint.projects_root()
    target = os.path.join(root, name)
    if not os.path.isdir(target):
        return None
    shutil.rmtree(target)
    state = checkpoint.load_state()
    if isinstance(state, dict) and state.get("dir") == name:
        try:
            os.remove(os.path.join(root, "h3chain_state.json"))
        except OSError:
            pass
    return f"h3_projects/{name}"


def upscale_reset(name, seg):
    """清掉某段的二采记录与产物文件（下次开启二采运行时该段重做）。

    新方案高清分段与基础段同名合并存储（seg_NNN.mp4 即二采结果），故重置
    连同 seg mp4/缩略图/尾帧锚一起删（下次运行按记录缺失自愈重建：二采开=
    重渲染高清，二采关=回放段重编码基础分辨率）。不碰基础链的段 latent 存档
    （seg_NNN.pt）、finals 与 merges；段号是 1-based 全局槽位（含序章/插入
    视频段，与前端段落卡片链位一致）。项目/段号非法抛 ValueError。
    """
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    try:
        g = int(seg) - 1
    except (TypeError, ValueError):
        raise ValueError(f"段号必须是整数：{seg!r}") from None
    if g < 0:
        raise ValueError("段号从 1 开始（1-based 全局槽位）")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    total = int(manifest.get("total") or 0)
    if total and g >= total:
        raise ValueError(f"段号 {g + 1} 越界（有效范围 1-{total}）")
    up = dict(manifest.get("upscale") or {})
    segs = [x if isinstance(x, dict) else None for x in (up.get("segs") or [])]
    while len(segs) <= g:
        segs.append(None)
    segs[g] = None
    up["segs"] = segs
    manifest["upscale"] = up
    manifest["updated_at"] = time.time()
    checkpoint.save_manifest(root, manifest)
    for f in (*checkpoint.upscale_files(g).values(), *checkpoint.upscale_legacy_files(g)):
        try:
            os.remove(os.path.join(root, f))
        except OSError:
            pass
    return manifest


def redo_cancel(name, slot):
    """从 manifest 重摇队列移除某段（撤销已提交未执行的重摇标记）。

    slot 是 0-based 全局槽位（与 redo_queue 条目、前端 ds.redo_segs 同口径）。
    幂等：槽位不在队列时同样返回成功（前端「取消重摇标记」统一走此口，
    覆盖 ds 标记与已入队条目两种来源）。只改 manifest，不动段 latent 存档
    与已成产物。项目/槽位非法抛 ValueError（路由层映射 400）。
    """
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    try:
        s = int(slot)
    except (TypeError, ValueError):
        raise ValueError(f"槽位必须是整数：{slot!r}") from None
    if s < 0:
        raise ValueError("槽位从 0 开始（0-based 全局槽位）")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    kept = []
    for x in (manifest.get("redo_queue") or []):
        try:
            if int(x[0]) == s:
                continue
        except (TypeError, ValueError, IndexError):
            pass
        kept.append(x)
    manifest["redo_queue"] = kept
    manifest["updated_at"] = time.time()
    checkpoint.save_manifest(root, manifest)
    return manifest


def _merge_sources(root: str, manifest: dict, items):
    """合并清单 -> 按序绝对路径列表。非法/缺失抛 ValueError。

    - {"seg": n}：n 为 1-based 全局槽位（含序章/插入视频段），映射
      manifest.videos[n-1]（seg_NNN.mp4 文件名）——前端段落卡片链位即此编号。
    - {"file": f}：先查项目目录（final_* / merged_* / 任意 mp4），
      再查 input 目录（上传外部素材）。f 允许至多两级子目录，
      每段过 safe_name 防穿越；input 命中再以 realpath+startswith 复核。
    """
    if not isinstance(items, list) or not items:
        raise ValueError("合并清单为空（至少勾选一个来源）")
    videos = list(manifest.get("videos") or [])
    sources = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError(f"清单项必须是对象：{it!r}")
        if "seg" in it:
            try:
                n = int(it["seg"])
            except (TypeError, ValueError):
                raise ValueError(f"段号必须是整数：{it.get('seg')!r}") from None
            if not 1 <= n <= len(videos):
                raise ValueError(f"段号 {n} 越界（有效范围 1-{len(videos)}）")
            fname = videos[n - 1]
            if not fname:
                raise ValueError(f"段 {n} 还没有生成视频文件（先跑完该段）")
            path = os.path.join(root, fname)
            if not os.path.isfile(path):
                raise ValueError(f"段 {n} 的视频文件缺失：{fname}")
            sources.append(path)
            continue
        f = str(it.get("file") or "").strip().replace("\\", "/")
        parts = [p for p in f.split("/") if p and p != "."]
        if not parts or len(parts) > 2 or not all(safe_name(p) for p in parts):
            raise ValueError(f"非法文件名：{f!r}")
        if not os.path.splitext(parts[-1])[1]:
            raise ValueError(f"文件名缺少扩展名：{f!r}")
        cand = os.path.join(root, *parts)
        if os.path.isfile(cand):
            sources.append(cand)
            continue
        try:
            from folder_paths import get_input_directory
            in_root = os.path.realpath(get_input_directory())
        except Exception:
            in_root = None
        if in_root:
            cand2 = os.path.realpath(os.path.join(in_root, *parts))
            if cand2.startswith(in_root + os.sep) and os.path.isfile(cand2):
                sources.append(cand2)
                continue
        raise ValueError(f"文件不存在（项目目录与 input 目录都没有）：{f}")
    return sources


def merge_project(name, items, fps=24, crf=20):
    """按序合并 -> 项目目录 merged_%Y%m%d_%H%M%S.mp4，返回更新后的 manifest。

    只追加产物与 manifest.merges 记录，不动链、不动 latent、不动段存档；
    画幅按 manifest.params.width/height（缺失回退首个源实际尺寸，不缩放）。
    长耗时编码（PyAV 流式）交由调用方放线程池；这里同步执行便于单测。
    失败抛 ValueError（清单/缺文件）/RuntimeError（编码失败，详情见
    media.last_error），.part 临时文件由 concat_av_mp4 自清理。
    """
    name = safe_name(name)
    if not name:
        raise ValueError("无效的项目目录名")
    root = os.path.join(checkpoint.projects_root(), name)
    manifest = checkpoint.load_manifest(root)
    if manifest is None:
        raise ValueError("项目不存在（没有 manifest，先新建或跑一段）")
    sources = _merge_sources(root, manifest, items)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_name = f"merged_{stamp}.mp4"
    k = 2
    while os.path.exists(os.path.join(root, out_name)):
        out_name = f"merged_{stamp}_{k}.mp4"
        k += 1
    params = manifest.get("params") or {}
    try:
        from . import media                  # 包内（ComfyUI 运行时）
    except ImportError:
        import media                         # 顶层导入（无 ComfyUI 的单测环境）
    ok = media.concat_av_mp4(sources, os.path.join(root, out_name),
                             width=params.get("width"), height=params.get("height"),
                             fps=fps, crf=crf)
    if not ok:
        raise RuntimeError(f"合并编码失败：{media.last_error}")

    # 编码耗时较长：写回前重读 manifest，只追加 merges，避免覆盖期间
    # 生成主循环落盘的新段进度/成片（残余竞态窗口缩到毫秒级）
    fresh = checkpoint.load_manifest(root)
    if isinstance(fresh, dict):
        manifest = fresh
    manifest.setdefault("merges", []).append({
        "file": out_name, "items": items, "updated_at": time.time()})
    manifest["updated_at"] = time.time()
    checkpoint.save_manifest(root, manifest)
    return manifest
