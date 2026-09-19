"""项目存档与成片的 HTTP 路由（官方 ComfyExtension.add_routes 钩子挂载）。

游戏式存档接口（前缀 /h3chain/）：
- GET  /ping            诊断：确认路由已注册（前端开面板时探测）
- GET  /projects        扫描磁盘列出全部项目（导演台项目列表数据源）
- GET  /project?dir=x   读单个项目 manifest（提示词/参数/进度）
- POST /create_project  新建项目：当场建文件夹+初始 manifest（游戏存档槽语义）
- POST /delete_project  删除项目 = 删除整个项目文件夹
- POST /delete_file     删除项目内单个文件（成片等，限 h3_projects/<项目>/<文件>）
- POST /merge           按序合并若干段/成片/外部视频 -> merged_*.mp4（流式编码）
- GET  /upscale_models  列出 latent_upscale_models 目录里的放大权重（二采面板下拉）
- POST /upscale_reset   清掉某段的二采记录与高清产物（下次运行重做该段）

均受限于输出目录内、防目录穿越；无新依赖。
注册有运行期兜底：ensure_registered() 幂等重试，节点执行时会再调一次，
根治「导入期注册失败 -> 前端 404/405」的问题。

每条路由同时挂「根路径」与「/api 前缀」两份：ComfyUI 0.33.x 的
PromptServer.add_routes() 只在启动时给当时已知的路由生成 /api 副本，
自定义节点加载晚于该时点；而新版前端 api.fetchApi() 会给所有不以
/api 开头的路径强制加 /api 前缀（comfyui-frontend 的 ComfyApi.apiURL）。
只挂根路径 => 新前端全部 404；两份都挂 => 新旧前端与浏览器直访全通。
"""

import asyncio
import json
import os
import time
import traceback

from aiohttp import web
from folder_paths import get_output_directory

from . import library as h3lib
from . import projects

_registered = False
_mounted = []  # [(目标对象, {(method, path)})]：按对象身份防重复挂载（持引用防 id 复用）

ROUTES = [
    ("GET", "/h3chain/ping"),
    ("GET", "/h3chain/busy"),
    ("GET", "/h3chain/projects"),
    ("GET", "/h3chain/project"),
    ("GET", "/h3chain/upscale_models"),
    ("GET", "/h3chain/vae_files"),
    ("GET", "/h3chain/experiments"),
    ("GET", "/h3chain/prompt-rules"),
    ("GET", "/h3chain/optimizer-config"),
    ("POST", "/h3chain/optimize"),
    ("POST", "/h3chain/optimize_stream"),
    ("POST", "/h3chain/expand_optimize_stream"),
    ("POST", "/h3chain/create_project"),
    ("POST", "/h3chain/save_prompts"),
    ("POST", "/h3chain/compile"),
    ("POST", "/h3chain/latent_slice"),
    ("POST", "/h3chain/latent_delete"),
    ("POST", "/h3chain/trim"),
    ("POST", "/h3chain/probe"),
    ("POST", "/h3chain/move_media"),
    ("POST", "/h3chain/split_av"),
    ("POST", "/h3chain/import_asset"),
    ("POST", "/h3chain/assets"),
    ("POST", "/h3chain/asset_check"),
    ("POST", "/h3chain/compile_refs"),
    ("POST", "/h3chain/transcode_submit"),
    ("GET", "/h3chain/transcode_jobs"),
    ("GET", "/h3chain/transcode_job"),
    ("POST", "/h3chain/transcode_cancel"),
    ("POST", "/h3chain/library_upload"),
    ("GET", "/h3chain/library_file"),
    ("GET", "/h3chain/asset_links"),
    ("POST", "/h3chain/asset_link"),
    ("POST", "/h3chain/asset_mark"),
    ("POST", "/h3chain/asset_unlink"),
    ("POST", "/h3chain/asset_mirror"),
    ("POST", "/h3chain/delete_project"),
    ("POST", "/h3chain/delete_file"),
    ("POST", "/h3chain/merge"),
    ("POST", "/h3chain/upscale_reset"),
    ("POST", "/h3chain/redo_cancel"),
    ("POST", "/h3chain/expand"),
    ("POST", "/h3chain/expand_multi"),
    ("POST", "/h3chain/expand_optimize"),
    ("POST", "/h3chain/optimize_multi"),
    ("POST", "/h3chain/expand_validate"),
    ("GET", "/h3chain/lib_list"),
    ("GET", "/h3chain/lib_item"),
    ("GET", "/h3chain/lib_thumb"),
    ("GET", "/h3chain/lib_raw"),
    ("GET", "/h3chain/lib_status"),
    ("GET", "/h3chain/lib_collections"),
    ("GET", "/h3chain/lib_zip_file"),
    ("POST", "/h3chain/lib_scan"),
    ("POST", "/h3chain/lib_rate"),
    ("POST", "/h3chain/lib_tag"),
    ("POST", "/h3chain/lib_alias"),
    ("POST", "/h3chain/lib_mirror"),
    ("POST", "/h3chain/lib_archive"),
    ("POST", "/h3chain/lib_collection_save"),
    ("POST", "/h3chain/lib_collection_delete"),
    ("POST", "/h3chain/lib_delete"),
    ("POST", "/h3chain/lib_stage"),
    ("POST", "/h3chain/lib_zip"),
    ("POST", "/h3chain/lib_ref"),
    ("POST", "/h3chain/lib_role"),
    ("GET", "/h3chain/grid_spec"),
    ("GET", "/h3chain/anchor_sources"),
    ("GET", "/h3chain/anchor_sheet"),
    ("POST", "/h3chain/anchor_sheet_build"),
]

_ROUTES_LOG = ", ".join(f"{m} {p}" for m, p in ROUTES)


def _routes_desc() -> list:
    return [{"method": m, "path": p} for m, p in ROUTES]


def _err(message, code="BAD_REQUEST", status=400, **extra):
    body = {"ok": False, "code": code, "message": message}
    body.update(extra)
    return web.json_response(body, status=status)


def add_routes(routes):
    """把路由挂到 routes 对象（aiohttp）。

    routes 可能是：
    - web.RouteTableDef（自定义节点标准写法，支持 routes.get/post 装饰器）
    - app.router（UrlDispatcher，运行中的真实路由表，用 add_get/add_post 方法）——
      ComfyUI 0.33.x 在 custom node 导入前已 app.add_routes(routes)，
      直接挂到 app.router 才能绕过挂载时序问题。
    """

    async def ping(request):
        return web.json_response({"ok": True, "version": "v4-manifest-v2.2", "routes": _routes_desc()})

    async def list_projects(request):
        # ?summary=1 走轻量分页（M1 新增）；默认保持旧 {ok,projects} 兼容
        if str(request.query.get("summary") or "").lower() in ("1", "true", "yes"):
            data = projects.list_projects_summary(
                request.query.get("page") or 1, request.query.get("size") or 50)
            return web.json_response({"ok": True, **data})
        return web.json_response({"ok": True, "projects": projects.list_projects()})

    async def project_detail(request):
        manifest = projects.read_project(request.query.get("dir") or "")
        if manifest is None:
            return _err("项目不存在", code="NOT_FOUND", status=404)
        return web.json_response({"ok": True, "manifest": manifest})

    async def create_project(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        manifest = projects.create_project(str(data.get("dir") or ""))
        if manifest is None:
            return _err("无效的项目目录名", code="BAD_NAME", status=400)
        return web.json_response({"ok": True, "manifest": manifest})

    async def save_prompts(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            manifest = projects.save_prompts(
                str(data.get("dir") or ""), data.get("prompts"),
                data.get("segments"), data.get("base_revision"))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                # 乐观锁冲突：带回 server_revision，前端据此刷新重试
                try:
                    cur = projects.read_project(str(data.get("dir") or ""))
                    srv = int((cur or {}).get("revision") or 0)
                except Exception:
                    srv = 0
                return _err(msg, code="REVISION_CONFLICT", status=409,
                            server_revision=srv)
            return _err(msg, code="BAD_REQUEST", status=400)
        if manifest is None:
            return _err("项目不存在（未新建也未跑过，无 manifest 可写）",
                        code="NOT_FOUND", status=404)
        return web.json_response({"ok": True, "manifest": manifest})

    async def delete_project(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        name = projects.safe_name(data.get("dir"))
        if not name:
            return _err("无效的项目目录名", code="BAD_NAME", status=400)
        try:
            deleted = projects.delete_project(name)
        except OSError as e:
            return _err(f"删除失败（文件可能被播放器/编码器占用）：{e}",
                        code="DELETE_FAILED", status=500)
        if deleted is None:
            return _err("项目不存在", code="NOT_FOUND", status=404)
        return web.json_response({"ok": True, "deleted": deleted})

    async def delete_file(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        rel = str(data.get("path") or "").strip().replace("\\", "/")
        parts = rel.split("/")
        # 四库兼容：h3_projects/<项目>/<文件名>（旧）或
        # h3_projects/<项目>/<库>/<文件名>（新，库=assets/finals/latent/texts）
        if not (3 <= len(parts) <= 4 and parts[0] == "h3_projects"
                and projects.safe_name(parts[1])
                and all(projects.safe_name(p) for p in parts[2:])
                and os.path.splitext(parts[-1])[1]):
            return _err("路径必须是 h3_projects/<项目>/<文件名> 或 h3_projects/<项目>/<库>/<文件名>",
                        code="BAD_PATH", status=400)
        if len(parts) == 4 and parts[2] not in ("assets", "finals", "latent", "texts",
                                                "keyframes"):
            return _err("未知子文件夹（须为 assets/finals/latent/texts）",
                        code="BAD_PATH", status=400)
        root = os.path.realpath(get_output_directory())
        target = os.path.realpath(os.path.join(root, *parts))
        if not target.startswith(root + os.sep) or not os.path.isfile(target):
            return _err("文件不存在", code="NOT_FOUND", status=404)
        os.remove(target)
        # 文件没了，manifest 里的历史记录也要少一条：右栏「成片 / 合并片段」是照
        # manifest["finals"] / ["merges"] 渲染的，不摘就永远挂着一张放不出来的卡
        # （与素材库删除同一根因）。videos 是链状态，不在"历史"范围，故意不动。
        rel = "/".join(parts[2:])
        try:
            projects.forget_media(parts[1], rel)
        except (ValueError, OSError):
            pass
        h3lib.invalidate(parts[1])
        return web.json_response({"ok": True})

    async def merge(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        items = data.get("items")
        try:
            # PyAV 流式编码是 CPU 密集长任务（分钟级），放线程池避免
            # 阻塞 aiohttp 事件循环拖死整个 ComfyUI 界面
            manifest = await asyncio.get_event_loop().run_in_executor(
                None, projects.merge_project, str(data.get("dir") or ""), items)
        except ValueError as e:
            return _err(str(e), code="BAD_REQUEST", status=400)
        except RuntimeError as e:
            return _err(str(e), code="MERGE_FAILED", status=500)
        return web.json_response({
            "ok": True, "manifest": manifest,
            "file": manifest["merges"][-1]["file"]})

    async def upscale_models(request):
        """模型目录里的放大权重列表（面板下拉数据源；无 torch/目录时返回空列表）。"""
        try:
            from . import upscale_net
            models = upscale_net.scan_models()
        except Exception:
            models = []
        return web.json_response({"ok": True, "models": models})

    async def vae_files(request):
        """VAE 文件列表（转码最小图选 VAE 用）：models/vae 下文件名。只读。"""
        try:
            import folder_paths
            files = folder_paths.get_filename_list("vae")
        except Exception as e:
            return _err(f"无法列出 VAE 目录：{e}", code="VAE_LIST_FAILED", status=500)
        return web.json_response({"ok": True, "files": list(files or [])})

    async def experiment_defs(request):
        """实验定义与参数元数据（前端实验面板动态渲染唯一数据源；含后端硬开关状态）。"""
        try:
            from . import experiments
            payload = experiments.experiment_defs_payload()
        except Exception:
            payload = {"ok": False, "force_disabled": True, "experiments": []}
        return web.json_response(payload)

    async def upscale_reset(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            manifest = projects.upscale_reset(str(data.get("dir") or ""), data.get("seg"))
        except ValueError as e:
            return _err(str(e), code="BAD_REQUEST", status=400)
        return web.json_response({"ok": True, "manifest": manifest})

    async def redo_cancel(request):
        """撤销重摇标记：从 manifest 重摇队列移除该槽位（幂等）。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            manifest = projects.redo_cancel(str(data.get("dir") or ""), data.get("slot"))
        except ValueError as e:
            return _err(str(e), code="BAD_REQUEST", status=400)
        return web.json_response({"ok": True, "manifest": manifest})

    async def save_assets(request):
        """资产库全量覆盖写：总量不限，revision 乐观锁可选。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            manifest = projects.save_assets(
                str(data.get("dir") or ""), data.get("assets"),
                data.get("base_revision"))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                try:
                    cur = projects.read_project(str(data.get("dir") or ""))
                    srv = int((cur or {}).get("revision") or 0)
                except Exception:
                    srv = 0
                return _err(msg, code="REVISION_CONFLICT", status=409,
                            server_revision=srv)
            return _err(msg, code="BAD_REQUEST", status=400)
        if manifest is None:
            return _err("项目不存在或资产列表非法", code="NOT_FOUND", status=404)
        return web.json_response({"ok": True, "manifest": manifest})

    async def asset_check(request):
        """资产包校验（不落盘）：缺文件/重标签/单段上限早爆，点名标签。

        可选传 dir：给了就按项目解析 assets/finals/latent/texts/ 前缀（output 可用）；
        不给则全部按 input 目录校验（画布 Hub 节点口径）。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            from . import asset_hub
        except ImportError:
            import asset_hub
        proot = None
        if isinstance(data.get("dir"), str) and projects.safe_name(data["dir"]):
            try:
                from folder_paths import get_output_directory
                cand = os.path.join(get_output_directory(), "h3_projects", data["dir"])
                if os.path.isdir(cand):
                    proot = cand
            except Exception:
                proot = None
        res = asset_hub.validate_pack(data.get("assets"), data.get("segments"), proot)
        status = 200 if res["ok"] else 422
        return web.json_response({"ok": res["ok"], **res}, status=status)

    async def compile_refs(request):
        """段引用干跑编译（不落盘）：refs/segments 内 asset_id 或 alias 混排 ->
        每段 blocks + tag_map（<Picture k>/<Video k>/<Audio j>），9/3/3 与未知引用早爆。

        请求体：{dir?, assets?, asset_links?, global_assets?, refs?, segments?}
        - dir：给了就叠加该项目 manifest 的 asset_links + 旧 assets；
          全局库能读则叠加（读不到按空库，不炸）。
        - refs：单段数组；segments：多段（每段为 refs 数组或 {refs}）。
        新旧兼容：旧 label 与新 alias 同一命名空间，asset_id 直引亦可。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            from . import asset_store
        except ImportError:
            import asset_store
        links = list(data.get("asset_links") or [])
        legacy = list(data.get("assets") or [])
        globals_ = list(data.get("global_assets") or [])
        if isinstance(data.get("dir"), str) and projects.safe_name(data["dir"]):
            mf = projects.read_project(data["dir"])
            if isinstance(mf, dict):
                if isinstance(mf.get("asset_links"), list):
                    links = list(mf["asset_links"]) + links
                if isinstance(mf.get("assets"), list):
                    legacy = list(mf["assets"]) + legacy
                # 项目 manifest 里的 asset_links 条目若无 asset_id（手写脏数据）直接忽略
                links = [x for x in links if isinstance(x, dict) and x.get("asset_id")]
            try:
                lib = asset_store.load_library(asset_store.library_root())
                globals_ = list(lib.get("assets") or []) + globals_
            except Exception:
                pass
        try:
            reg = asset_store.build_registry(globals_, links, legacy)
        except Exception as e:
            return _err(f"资产注册表构建失败：{e}", code="BAD_REQUEST", status=400)
        if isinstance(data.get("segments"), list):
            res = asset_store.compile_segments(reg, data["segments"])
            status = 200 if res["ok"] else 422
            return web.json_response({"ok": res["ok"], **res}, status=status)
        res = asset_store.compile_refs(reg, data.get("refs") or [], 1)
        status = 200 if res["ok"] else 422
        return web.json_response({"ok": res["ok"], **res}, status=status)

    async def transcode_submit(request):
        """提交后台转码任务（随时可提交，不受生成锁影响；执行在队列侧认领）。

        体 {dir, src, start_s?, end_s?, branch?, split?, save_name?} ->
        {ok, job}。参数非法/项目或源文件不存在回 400/404。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            from . import transcode_queue as _tq
        except ImportError:
            import transcode_queue as _tq
        name = projects.safe_name(str(data.get("dir") or ""))
        if not name:
            return _err("无效的项目目录名", code="BAD_NAME", status=400)
        if projects.read_project(name) is None:
            return _err("项目不存在（没有 manifest，先新建或跑一段）",
                         code="NOT_FOUND", status=404)
        src = str(data.get("src") or "").strip().replace("\\", "/")
        parts = [p for p in src.split("/") if p and p != "."]
        if not parts or len(parts) > 2 or not all(projects.safe_name(p) for p in parts):
            return _err(f"非法源文件：{data.get('src')!r}（须为项目内相对路径）",
                         code="BAD_REQUEST", status=400)
        try:
            from . import checkpoint as _ckpt
        except ImportError:
            import checkpoint as _ckpt
        root = os.path.join(_ckpt.projects_root(), name)
        if not os.path.isfile(_ckpt.resolve_project_file(root, "/".join(parts))):
            return _err(f"源文件不存在：{'/'.join(parts)}", code="NOT_FOUND", status=404)
        try:
            job = _tq.submit(name, "/".join(parts), data.get("start_s", 0.0),
                             data.get("end_s", 0.0), data.get("branch", "图像+音频"),
                             data.get("split", "否"), data.get("save_name", ""))
        except ValueError as e:
            return _err(str(e), code="BAD_REQUEST", status=400)
        return web.json_response({"ok": True, "job": job})

    async def transcode_jobs(request):
        """任务列表（?dir= 可按项目过滤）。轮询不受生成锁影响。"""
        try:
            from . import transcode_queue as _tq
        except ImportError:
            import transcode_queue as _tq
        proj = request.query.get("dir") if hasattr(request, "query") else None
        proj = str(proj).strip() if proj else None
        if proj and not projects.safe_name(proj):
            return _err("无效的项目目录名", code="BAD_NAME", status=400)
        return web.json_response({"ok": True, "jobs": _tq.list_jobs(proj)})

    async def transcode_job(request):
        """单个任务（?id=）。不存在回 404。"""
        try:
            from . import transcode_queue as _tq
        except ImportError:
            import transcode_queue as _tq
        q = request.query if hasattr(request, "query") else {}
        jid = q.get("id") if hasattr(q, "get") else None
        job = _tq.get(jid)
        if job is None:
            return _err("任务不存在（进程重启会清空任务表，请重新提交）",
                         code="NOT_FOUND", status=404)
        return web.json_response({"ok": True, "job": job})

    async def transcode_cancel(request):
        """取消任务：queued 直接取消；running 标记后由执行侧中断收尾。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            from . import transcode_queue as _tq
        except ImportError:
            import transcode_queue as _tq
        job = _tq.cancel(data.get("id"))
        if job is None:
            return _err("任务不存在（进程重启会清空任务表，请重新提交）",
                         code="NOT_FOUND", status=404)
        return web.json_response({"ok": True, "job": job})

    _UPLOAD_EXT = {"image": ("png", "jpg", "jpeg", "webp", "bmp"),
                   "video": ("mp4", "mov", "mkv", "webm"),
                   "audio": ("wav", "mp3", "flac", "ogg", "m4a")}

    _LIB_CT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "webp": "image/webp", "bmp": "image/bmp",
               "mp4": "video/mp4", "mov": "video/quicktime",
               "mkv": "video/x-matroska", "webm": "video/webm",
               "wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac",
               "ogg": "audio/ogg", "m4a": "audio/mp4"}

    async def library_file(request):
        """全局库文件直读（?asset_id=）：资产瓦片预览/播放用。

        只读，不受生成锁影响；路径禁闭在 library_root 内（realpath 复核防穿越）。
        """
        q = request.query if hasattr(request, "query") else {}
        aid = q.get("asset_id") if hasattr(q, "get") else None
        aid = str(aid or "").strip()
        import re as _re
        if not _re.fullmatch(r"a_[0-9a-f]{12}", aid):
            return _err("非法的 asset_id", code="BAD_REQUEST", status=400)
        try:
            from . import asset_store
        except ImportError:
            import asset_store
        try:
            libroot = asset_store.try_library_root()
        except Exception:
            libroot = None
        if not libroot:
            return _err("全局库不可用", code="NOT_FOUND", status=404)
        entry = None
        try:
            for e in (asset_store.load_library(libroot).get("assets") or []):
                if isinstance(e, dict) and e.get("asset_id") == aid:
                    entry = e
                    break
        except Exception:
            entry = None
        if entry is None:
            return _err("资产不存在", code="NOT_FOUND", status=404)
        rel = str(entry.get("file") or "").replace("\\", "/")
        base = os.path.realpath(libroot)
        target = os.path.realpath(os.path.join(base, *rel.split("/")))
        if not target.startswith(base + os.sep) or not os.path.isfile(target):
            return _err("资产文件缺失", code="NOT_FOUND", status=404)
        ext = os.path.splitext(target)[1].lower().lstrip(".")
        ctype = _LIB_CT.get(ext, "application/octet-stream")
        if hasattr(web, "FileResponse"):
            return web.FileResponse(target, headers={"Content-Type": ctype})
        return web.json_response({"ok": True, "asset_id": aid, "file": rel})

    async def asset_links(request):
        """项目链接表（?dir=）：asset_links + 全局库回填 file/bytes，直供前端池子合并。

        只读语义；全局库不可用时 file 缺省（执行期按旧路径）。
        副手：老项目首次读到时按池序补发**标注**（mark）并落盘，之后号固定不变 ——
        补号幂等，只在确有缺失时才写。
        """
        q = request.query if hasattr(request, "query") else {}
        name = projects.safe_name(str((q.get("dir") if hasattr(q, "get") else None) or ""))
        if not name:
            return _err("无效的项目目录名", code="BAD_NAME", status=400)
        manifest = projects.read_project(name)
        if manifest is None:
            return _err("项目不存在", code="NOT_FOUND", status=404)
        migrated, changed = projects.normalize_marks(name)
        if changed and migrated is not None:
            manifest = migrated
        try:
            from . import asset_store
        except ImportError:
            import asset_store
        lib = {}
        try:
            for e in (asset_store.load_library(asset_store.try_library_root() or "").get("assets") or []):
                if isinstance(e, dict) and e.get("asset_id"):
                    lib[e["asset_id"]] = e
        except Exception:
            lib = {}
        out = []
        for x in (manifest.get("asset_links") or []):
            if not isinstance(x, dict) or not x.get("asset_id"):
                continue
            ent = {"asset_id": x["asset_id"], "alias": x.get("alias") or "",
                   "kind": x.get("kind") or "image"}
            mk = asset_store.clean_mark(x.get("mark"))
            if mk:
                ent["mark"] = mk
                # 缺省即自动（只在手动改过时才存 False，这里回显成确定布尔值）
                ent["mark_auto"] = x.get("mark_auto") is not False
            if isinstance(x.get("roles"), list):
                ent["roles"] = [str(r) for r in x["roles"]
                                if str(r) in ("首帧图", "尾帧图")][:2]
            g = lib.get(x["asset_id"]) or {}
            for k in ("file", "bytes", "orig_name"):
                if g.get(k) is not None:
                    ent[k] = g[k]
            out.append(ent)
        return web.json_response({"ok": True, "links": out})

    async def asset_mark(request):
        """手动改标注：{dir, mark, asset_id?|alias?, base_revision?} -> manifest。

        mark 传空串 = 清除该素材的标注（下次自动补号）。标注项目内唯一，
        与别的素材重复回 400。改过的条目 mark_auto=False，不再被自动发号覆盖。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        name = projects.safe_name(str(data.get("dir") or ""))
        if not name:
            return _err("无效的项目目录名", code="BAD_NAME", status=400)
        try:
            manifest = projects.set_asset_mark(
                name, data.get("mark"), data.get("asset_id"), data.get("alias"),
                data.get("base_revision"))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _rev_conflict(name, msg)
            return _err(msg, code="BAD_REQUEST", status=400)
        if manifest is None:
            return _err("项目不存在", code="NOT_FOUND", status=404)
        return web.json_response({"ok": True, "manifest": manifest})

    async def asset_link(request):
        """写链接：{dir, asset_id, alias, kind?, roles?, mark?, base_revision?} -> manifest。

        改名/改标即重调本接口（同 alias 重指向；roles 显式传列表覆盖，缺省不动；
        mark 缺省按池序自动发号，显式传则手动覆盖并锁定）。revision 冲突回 409。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        name = projects.safe_name(str(data.get("dir") or ""))
        if not name:
            return _err("无效的项目目录名", code="BAD_NAME", status=400)
        try:
            # 必须**关键字传参**：link_asset 的形参在 (roles, mark) 之间插过
            # ref_name / orig_name 两个新参数，按位置传的话 `mark` 会被
            # ref_name 吃掉 —— 表现为"手动传了标注却总是被自动发号覆盖"，
            # 且不报错，是最难查的那类 bug。
            manifest = projects.link_asset(
                name, data.get("asset_id"), data.get("alias"),
                data.get("kind") or "image", data.get("base_revision"),
                roles=(data.get("roles") if isinstance(data.get("roles"), list) else None),
                ref_name=data.get("ref_name"),
                orig_name=data.get("orig_name"),
                mark=data.get("mark"))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _rev_conflict(name, msg)
            return _err(msg, code="BAD_REQUEST", status=400)
        if manifest is None:
            return _err("项目不存在或链接非法", code="NOT_FOUND", status=404)
        return web.json_response({"ok": True, "manifest": manifest})


    async def asset_unlink(request):
        """解链：{dir, asset_id?, alias?, base_revision?} -> manifest（幂等）。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        name = projects.safe_name(str(data.get("dir") or ""))
        if not name:
            return _err("无效的项目目录名", code="BAD_NAME", status=400)
        try:
            manifest = projects.unlink_asset(
                name, data.get("asset_id"), data.get("alias"), data.get("base_revision"))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _rev_conflict(name, msg)
            return _err(msg, code="BAD_REQUEST", status=400)
        if manifest is None:
            return _err("项目不存在或参数非法", code="NOT_FOUND", status=404)
        return web.json_response({"ok": True, "manifest": manifest})

    async def asset_mirror(request):
        """全局库调入项目：{dir, asset_id, save_name?} -> assets/<名> 拷贝。

        只拷文件不写 manifest（前端推池条目后走 persistPool 照常登记）。
        同名加 _2 后缀不覆盖；不受生成锁影响（纯文件拷贝）。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        name = projects.safe_name(str(data.get("dir") or ""))
        if not name:
            return _err("无效的项目目录名", code="BAD_NAME", status=400)
        import re as _re2
        aid = str(data.get("asset_id") or "").strip()
        if not _re2.fullmatch(r"a_[0-9a-f]{12}", aid):
            return _err("非法的 asset_id", code="BAD_REQUEST", status=400)
        try:
            from . import asset_store
        except ImportError:
            import asset_store
        try:
            libroot = asset_store.try_library_root()
            entry = None
            if libroot:
                for e in (asset_store.load_library(libroot).get("assets") or []):
                    if isinstance(e, dict) and e.get("asset_id") == aid:
                        entry = e
                        break
        except Exception:
            entry, libroot = None, None
        if entry is None or not libroot:
            return _err("资产不存在", code="NOT_FOUND", status=404)
        rel = str(entry.get("file") or "").replace("\\", "/")
        base = os.path.realpath(libroot)
        src = os.path.realpath(os.path.join(base, *rel.split("/")))
        if not src.startswith(base + os.sep) or not os.path.isfile(src):
            return _err("资产文件缺失", code="NOT_FOUND", status=404)
        try:
            from . import checkpoint as _ckpt
        except ImportError:
            import checkpoint as _ckpt
        if projects.read_project(name) is None:
            return _err("项目不存在（没有 manifest，先新建或跑一段）",
                         code="NOT_FOUND", status=404)
        adir = _ckpt.assets_dir(os.path.join(_ckpt.projects_root(), name))
        want = str(data.get("save_name") or "").strip().replace("\\", "/").split("/")[-1]
        if not want:
            want = entry.get("orig_name") or os.path.basename(src)
        if not projects.safe_name(want) or not os.path.splitext(want)[1]:
            return _err(f"非法保存名：{data.get('save_name')!r}", code="BAD_REQUEST", status=400)
        import shutil as _sh
        stem, ext = os.path.splitext(want)
        cand, k = want, 2
        while os.path.exists(os.path.join(adir, cand)):
            cand = f"{stem}_{k}{ext}"
            k += 1
        try:
            _sh.copy2(src, os.path.join(adir, cand))
        except OSError as e:
            return _err(f"拷贝失败：{e}", code="MIRROR_FAILED", status=500)
        return web.json_response({"ok": True, "file": f"assets/{cand}"})

    def _upload_kind(kind, filename):
        k = str(kind or "").strip() or "image"
        if k not in ("image", "video", "audio"):
            k = "image"
        ext = os.path.splitext(str(filename or ""))[1].lower().lstrip(".")
        if not ext:
            raise ValueError(f"上传文件缺扩展名：{filename!r}")
        if ext not in _UPLOAD_EXT[k]:
            raise ValueError(f"扩展名 .{ext} 与类别 {k} 不符（允许 {list(_UPLOAD_EXT[k])}）")
        return k

    async def library_upload(request):
        """上传即入库（全局库 + 可选项目链接）：multipart（浏览器拖拽）或 JSON。

        JSON 体 {src, kind?, tags?, desc?, link_dir?, alias?, base_revision?}：
        src 为 input 内相对路径（headless/API 用）。multipart 字段：
        file（文件）+ kind/tags（逗号分隔）/desc/link_dir/alias 文本。
        纯文件拷贝 + 登记，不受生成锁影响；revision 冲突回 409。
        """
        try:
            from . import asset_store
        except ImportError:
            import asset_store
        # 先看 Content-Type 再读 body：aiohttp 的 body 流只能消费一次，
        # 先 json() 再 multipart() 会报 Could not find starting boundary。
        ctype = ""
        try:
            hdrs = getattr(request, "headers", {}) or {}
            ctype = str(hdrs.get("Content-Type", "") if hasattr(hdrs, "get") else "")
        except Exception:
            ctype = ""
        data = None
        if "multipart/form-data" not in ctype.lower():
            try:
                maybe = await request.json()
            except Exception:
                return _err("请求体不是合法 JSON（须为 JSON {src} 或 multipart 文件）",
                             code="BAD_JSON", status=400)
            if not isinstance(maybe, dict) or not maybe.get("src"):
                return _err("JSON 体须含 src（input 内相对路径）",
                             code="BAD_REQUEST", status=400)
            data = maybe
        fields = {}
        tmp_path, tmp_name = None, ""
        if data is None:
            # multipart：文件流式落临时文件
            try:
                reader = await request.multipart()
            except Exception:
                return _err("不支持的请求体（须为 multipart 文件或 JSON {src}）",
                             code="BAD_REQUEST", status=400)
            size = 0
            while True:
                try:
                    field = await reader.next()
                except Exception as e:
                    return _err(f"multipart 解析失败：{e}", code="BAD_REQUEST", status=400)
                if field is None:
                    break
                if field.name == "file" and field.filename:
                    tmp_name = os.path.basename(field.filename)
                    import tempfile as _tf
                    fd, tmp_path = _tf.mkstemp(prefix="h3lib_")
                    try:
                        with os.fdopen(fd, "wb") as f:
                            while True:
                                chunk = await field.read_chunk()
                                if not chunk:
                                    break
                                size += len(chunk)
                                f.write(chunk)
                    except Exception as e:
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
                        return _err(f"文件接收失败：{e}", code="UPLOAD_FAILED", status=500)
                else:
                    try:
                        fields[field.name] = (await field.read(decode=True)).decode(
                            "utf-8", "replace") if field.name else ""
                    except Exception:
                        pass
            if not tmp_path:
                return _err("multipart 里没有 file 文件字段", code="BAD_REQUEST", status=400)
            try:
                kind = _upload_kind(fields.get("kind"), tmp_name)
            except ValueError as e:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return _err(str(e), code="BAD_REQUEST", status=400)
            tags = [t.strip() for t in str(fields.get("tags") or "").split(",") if t.strip()]
            desc, link_dir = str(fields.get("desc") or ""), str(fields.get("link_dir") or "")
            alias = str(fields.get("alias") or "")
            # 落点与镜像开关：multipart 走文本字段（旧实现只读 JSON 分支的 data，
            # 于是浏览器上传永远拿不到 mirror —— 「上传只会进全局库」的根因）
            mirror_raw = str(fields.get("mirror") or "")
            dest = str(fields.get("dest") or "")
        else:
            # JSON：input 内文件（防穿越 + realpath 复核，与 import_asset 同口径）
            f = str(data.get("src") or "").strip().replace("\\", "/")
            parts = [p for p in f.split("/") if p and p != "."]
            if not parts or len(parts) > 2 or not all(projects.safe_name(p) for p in parts) \
                    or not os.path.splitext(parts[-1])[1]:
                return _err(f"非法源文件：{data.get('src')!r}", code="BAD_REQUEST", status=400)
            try:
                from folder_paths import get_input_directory
                in_root = os.path.realpath(get_input_directory())
            except Exception:
                in_root = None
            if not in_root:
                return _err("无法定位 input 目录", code="BAD_REQUEST", status=400)
            src_abs = os.path.realpath(os.path.join(in_root, *parts))
            if not src_abs.startswith(in_root + os.sep) or not os.path.isfile(src_abs):
                return _err(f"源文件不存在（input 目录没有）：{'/'.join(parts)}",
                             code="NOT_FOUND", status=404)
            try:
                kind = _upload_kind(data.get("kind"), parts[-1])
            except ValueError as e:
                return _err(str(e), code="BAD_REQUEST", status=400)
            tmp_path, tmp_name = src_abs, parts[-1]
            tags = data.get("tags") if isinstance(data.get("tags"), list) else []
            tags = [str(t).strip() for t in tags if str(t).strip()]
            desc, link_dir = str(data.get("desc") or ""), str(data.get("link_dir") or "")
            alias = str(data.get("alias") or "")
            mirror_raw = str(data.get("mirror") or "")
            dest = str(data.get("dest") or "")
        # 落点 dest（前端按当前 scope 传）——**上传到哪里就是哪里，不顺手复制**：
        #   global  —— 只进全局库（跨项目复用）
        #   project —— 只落项目 assets/ 并登记清单（可用 @别名 引用）
        #   finals  —— 只落项目 finals/（成片库，目录扫描即见）
        # 缺省（空）= 给了 link_dir 就按 project（兼容旧调用）。
        # 注：早期实现是"项目落点也往全局库复制一份"，用户明确反馈那样不对 ——
        # 跨项目复用改成显式动作（项目瓦片上的「存入全局库」→ /h3chain/lib_archive）。
        dest = str(dest or "").strip().lower()
        if dest not in ("global", "project", "finals"):
            dest = "project" if link_dir else "global"
        out = {"ok": True, "dest": dest}
        import shutil as _sh
        lib_root = None
        staged = tmp_path
        cleanup = []
        if tmp_path and data is None:
            # multipart 已落盘：按真实文件名再拷一份（register_content 内秒传去重）。
            # 中转名挂在唯一 tmp 路径上（同名并发不互踩）；真实文件名透传 orig_name，
            # 否则库内/调入/转码起名全被 h3lib_ 前缀污染。
            staged = tmp_path + "_stage_" + "".join(
                c for c in tmp_name if c.isalnum() or c in ("-", "_", "."))[-64:]
            try:
                _sh.copy2(tmp_path, staged)
            except OSError as e:
                return _err(f"文件接收失败：{e}", code="UPLOAD_FAILED", status=500)
            cleanup = [staged, tmp_path]
        try:
            if dest == "global":
                lib_root = asset_store.library_root()
                entry = asset_store.register_content(
                    lib_root, staged, kind, tags, desc,
                    orig_name=(tmp_name if data is None else None))
                out["entry"] = entry
                out["asset_id"] = entry.get("asset_id")
                out["file"] = entry.get("file")
            else:
                if not link_dir:
                    return _err("项目落点必须给 link_dir（项目目录名）",
                                code="BAD_REQUEST", status=400)
                if not projects.safe_name(link_dir):
                    return _err("无效的 link_dir（项目目录名）", code="BAD_NAME", status=400)
                if projects.read_project(link_dir) is None:
                    return _err("项目不存在（没有 manifest，先新建或跑一段）",
                                code="NOT_FOUND", status=404)
                if dest == "finals":
                    # 成片库是目录扫描型：拷进 finals/ 即可见，不登记进资产清单
                    out["stored"] = h3lib.store_to_finals(
                        link_dir, staged, tmp_name or None)
                else:
                    out["stored"] = h3lib.store_to_project(
                        link_dir, staged, tmp_name or None, kind, alias)
                h3lib.invalidate(link_dir)
        except ValueError as e:
            return _err(str(e), code="BAD_REQUEST", status=400)
        except Exception as e:
            return _err(f"入库失败：{type(e).__name__}: {e}", code="UPLOAD_FAILED", status=500)
        finally:
            for p in cleanup:
                try:
                    os.remove(p)
                except OSError:
                    pass
        status = 200
        return web.json_response(out, status=status)

    def _rev_conflict(request_dir, msg):
        try:
            cur = projects.read_project(request_dir)
            srv = int((cur or {}).get("revision") or 0)
        except Exception:
            srv = 0
        return _err(msg, code="REVISION_CONFLICT", status=409, server_revision=srv)

    def _busy():
        try:
            from . import checkpoint as _ckpt
        except ImportError:
            import checkpoint as _ckpt
        return _ckpt.is_busy()

    async def busy_state(request):
        """生成互斥状态：{busy}——剪辑/转码/移动入口遇忙回 423，前端据此置灰。"""
        return web.json_response({"ok": True, "busy": _busy()})

    async def latent_slice(request):
        """latent 切片：段存档/已登记 latent 按帧窗 -> latent/<save>.pt（无 VAE）。

        kind: av（默认）| video（只留图像）| audio（只留音频）。
        生成中 423（与采样互斥，按钮置灰）。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        if _busy():
            return _err("正在生成中，剪辑/转码入口已锁定（完成后自动解锁）",
                        code="BUSY", status=423)
        try:
            manifest = projects.slice_latent(
                str(data.get("dir") or ""), data.get("src"),
                data.get("start_f"), data.get("end_f"),
                data.get("save_name"), data.get("base_revision"),
                data.get("kind") or "av")
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _rev_conflict(str(data.get("dir") or ""), msg)
            return _err(msg, code="BAD_REQUEST", status=400)
        except RuntimeError as e:
            return _err(str(e), code="SLICE_FAILED", status=500)
        return web.json_response({"ok": True, "manifest": manifest,
                                  "file": (manifest.get("latents") or [{}])[-1].get("file")})

    async def latent_delete(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            manifest = projects.delete_latent(
                str(data.get("dir") or ""), data.get("file"), data.get("base_revision"))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _rev_conflict(str(data.get("dir") or ""), msg)
            return _err(msg, code="BAD_REQUEST", status=400)
        return web.json_response({"ok": True, "manifest": manifest})

    async def trim(request):
        """入出点裁剪：项目内 mp4 -> 新文件（线程池重编码）。生成中 423。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        if _busy():
            return _err("正在生成中，剪辑/转码入口已锁定（完成后自动解锁）",
                        code="BUSY", status=423)
        try:
            manifest = await asyncio.get_event_loop().run_in_executor(
                None, projects.trim_asset, str(data.get("dir") or ""),
                data.get("src"), data.get("start_s"), data.get("end_s"),
                data.get("save_name"), data.get("base_revision"))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _rev_conflict(str(data.get("dir") or ""), msg)
            return _err(msg, code="BAD_REQUEST", status=400)
        except RuntimeError as e:
            return _err(str(e), code="TRIM_FAILED", status=500)
        return web.json_response({"ok": True, "manifest": manifest,
                                  "file": (manifest.get("clips") or [{}])[-1].get("file")})

    async def probe(request):
        """媒体探针：项目内 mp4 -> {fps, frames, duration_s, width, height, has_audio}。

        只读元数据（不解码），供裁剪子面板填帧窗。不受生成锁影响。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        name = projects.safe_name(str(data.get("dir") or ""))
        if not name:
            return _err("无效的项目目录名", code="BAD_NAME", status=400)
        f = str(data.get("src") or "").strip().replace("\\", "/")
        parts = [p for p in f.split("/") if p and p != "."]
        if not parts or len(parts) > 2 or not all(projects.safe_name(p) for p in parts):
            return _err(f"非法源文件：{data.get('src')!r}", code="BAD_REQUEST", status=400)
        try:
            from . import checkpoint as _ckpt
        except ImportError:
            import checkpoint as _ckpt
        root = os.path.join(_ckpt.projects_root(), name)
        src_path = _ckpt.resolve_project_file(root, "/".join(parts))
        if not os.path.isfile(src_path):
            return _err(f"源文件不存在：{'/'.join(parts)}", code="NOT_FOUND", status=404)
        try:
            from . import media as _media
        except ImportError:
            import media as _media
        try:
            info = await asyncio.get_event_loop().run_in_executor(
                None, _media.probe_media, src_path)
        except RuntimeError as e:
            return _err(str(e), code="PROBE_FAILED", status=500)
        return web.json_response({"ok": True, **info})

    async def prompt_rules(request):
        """提示词撰写规则下发（只读）：prompt/*.txt，供优化时注入 LLM。"""
        try:
            from . import optimizer as _opt
        except ImportError:
            import optimizer as _opt
        return web.json_response({"ok": True, "files": _opt.load_rule_files()})

    async def optimizer_config(request):
        """优化器配置（脱敏）+ 本地模型扫描，供设置面板使用。"""
        try:
            from . import optimizer as _opt
        except ImportError:
            import optimizer as _opt
        try:
            cfg = _opt.public_config(_opt.normalize_config(None))
        except Exception:
            cfg = {"ok": False}
        return web.json_response({"ok": True, **cfg})

    async def optimize(request):
        """提示词优化（长耗时，放线程池）：自研后端，云/本地双通道。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            from . import optimizer as _opt
        except ImportError:
            import optimizer as _opt
        try:
            text = await asyncio.get_event_loop().run_in_executor(
                None, _opt.optimize_once, data.get("config"), data)
        except ValueError as e:
            return _err(str(e), code="BAD_REQUEST", status=400)
        except RuntimeError as e:
            return _err(str(e), code="OPTIMIZE_FAILED", status=502)
        except Exception as e:
            return _err(f"优化失败：{e}", code="OPTIMIZE_FAILED", status=500)
        return web.json_response({"ok": True, "prompt": text})

    async def _sse_stream(request, work, error_code="STREAM_FAILED"):
        """把一段**同步长耗时工作**包成 SSE：worker 线程里跑，事件实时推给前端。

        `work(data, emit)` 在**线程池**里执行（同步阻塞代码，不能占事件循环），
        用 `emit(kind, payload)` 推帧；返回值合并进收尾帧 `{"ok": true, **结果}`。

        帧格式（前端 `_postStream` 按此解析）：
            event: progress  data: {...}          # 0..N 帧，随时可到
            event: done      data: {ok: true, ...}
            event: error     data: {ok: false, code, message}

        进度事件由工作线程经 `loop.call_soon_threadsafe` 塞进 asyncio 队列，
        所以 worker 里不需要任何 asyncio 知识；客户端中途断开时
        `call_soon_threadsafe` 会抛 RuntimeError，吞掉即可（结果没人要了）。

        为什么抽成公共函数：优化和「扩写+优化」是同一套需求（都是 30~90 秒的
        黑箱 + 都要进度条 + 都要能取消），各写一遍必然只修好一条。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _push(kind, payload):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))
            except RuntimeError:
                pass

        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)

        async def _write(kind, payload):
            body = "event: %s\ndata: %s\n\n" % (
                kind, json.dumps(payload, ensure_ascii=False))
            await resp.write(body.encode("utf-8"))

        async def _flush():
            while not queue.empty():
                kind, payload = queue.get_nowait()
                await _write(kind, payload)

        fut = loop.run_in_executor(None, lambda: work(data, _push))
        try:
            while True:
                done, _ = await asyncio.wait({fut}, timeout=0.25)
                await _flush()
                if done:
                    break
            await _flush()
            try:
                result = fut.result()
                payload = {"ok": True}
                payload.update(result if isinstance(result, dict) else {"result": result})
                await _write("done", payload)
            except ValueError as e:
                await _write("error", {"ok": False, "code": "BAD_REQUEST",
                                       "message": str(e)})
            except Exception as e:
                await _write("error", {"ok": False, "code": error_code,
                                       "message": str(e)})
        except (ConnectionResetError, asyncio.CancelledError):
            pass          # 客户端关了页面/取消：不用报错，让它安静地结束
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
        return resp

    async def optimize_stream(request):
        """提示词优化 + **实时进度**（SSE）。

        与 /h3chain/optimize 的区别：那条是"发完等一整包"，而思考模型一次改写
        动辄 30~90 秒，期间前端只能干等（用户体感就是"卡死了"）。这条边跑边推。

        进度帧字段：{phase, content_chars, reasoning_chars, tokens, max_tokens,
        elapsed}（详见 optimizer._api_generate_stream）。
        """
        try:
            from . import optimizer as _opt
        except ImportError:
            import optimizer as _opt

        def _work(data, emit):
            return {"prompt": _opt.optimize_once(
                data.get("config"), data, lambda ev: emit("progress", ev))}

        return await _sse_stream(request, _work, error_code="OPTIMIZE_FAILED")

    async def expand_optimize_stream(request):
        """扩写 + 优化一步到位 + **实时进度**（SSE）。

        与 /h3chain/expand_optimize 同一条链，只是把两次 LLM 调用（扩写、优化）
        的进度都推出来。两次调用各 20~40 秒，串起来最长能到一分半 —— 不给进度
        的话，用户点完按钮除了"扩写中…"三个字什么都看不到。

        进度帧多一个 `stage` 字段："expand"（第①步）/ "optimize"（第②步），
        前端据此把进度条画成两段。
        """
        try:
            svc = _load_expander()
        except Exception as e:
            return _err(f"扩写模块未就绪：{e}", code="EXPAND_UNAVAILABLE", status=500)

        def _work(data, emit):
            return svc.expand_optimize_via_config(
                data.get("config"), data,
                on_progress=lambda ev: emit("progress", ev))

        return await _sse_stream(request, _work, error_code="EXPAND_FAILED")

    def _load_expander():
        """加载 tools/h3_prompt_expander/service.py（非包目录，走 sys.path 注入）。"""
        import importlib
        import sys as _sys
        root = os.path.dirname(os.path.abspath(__file__))
        exp_dir = os.path.join(root, "tools", "h3_prompt_expander")
        if exp_dir not in _sys.path:
            _sys.path.insert(0, exp_dir)
        return importlib.import_module("service")

    async def expand(request):
        """提示词扩写（长耗时，放线程池）：意图理解 + 官方编译 + 确认卡，一次返回。

        与 /h3chain/optimize 的区别：optimize 返回纯文本（快速改写），
        expand 返回结构化信封（intent/pe/确认卡/校验），供导演台确认环使用。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            svc = _load_expander()
        except Exception as e:
            return _err(f"扩写模块未就绪：{e}", code="EXPAND_UNAVAILABLE", status=500)
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, svc.expand_via_config, data.get("config"), data)
        except ValueError as e:
            return _err(str(e), code="BAD_REQUEST", status=400)
        except RuntimeError as e:
            return _err(str(e), code="EXPAND_FAILED", status=502)
        except Exception as e:
            return _err(f"扩写失败：{e}", code="EXPAND_FAILED", status=500)
        return web.json_response(result)

    async def expand_multi(request):
        """剧本扩写（多段）：总意图 -> N 段中文剧本（内容发散，不带官方格式）。

        与 /h3chain/expand 的区别：expand 是「意图 -> 官方格式」的编译器（单段），
        这里是「意图 -> 一大段剧本」的内容发散器，支持时长范围与多段。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            svc = _load_expander()
        except Exception as e:
            return _err(f"扩写模块未就绪：{e}", code="EXPAND_UNAVAILABLE", status=500)
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, svc.screenplay_via_config, data.get("config"), data)
        except ValueError as e:
            return _err(str(e), code="BAD_REQUEST", status=400)
        except RuntimeError as e:
            return _err(str(e), code="EXPAND_FAILED", status=502)
        except Exception as e:
            return _err(f"剧本扩写失败：{e}", code="EXPAND_FAILED", status=500)
        return web.json_response(result)

    async def expand_optimize(request):
        """扩写 + 优化**一步到位**：中文意图 -> 剧本 -> H3 官方格式文本。

        三框合一后段卡只有一个框，前端不再有中间稿可点，所以两步在后端串起来：
        一次请求、一次回填，中间剧本不落库（只在响应里带回一份便于排查）。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            svc = _load_expander()
        except Exception as e:
            return _err(f"扩写模块未就绪：{e}", code="EXPAND_UNAVAILABLE", status=500)
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, svc.expand_optimize_via_config, data.get("config"), data)
        except ValueError as e:
            return _err(str(e), code="BAD_REQUEST", status=400)
        except RuntimeError as e:
            return _err(str(e), code="EXPAND_FAILED", status=502)
        except Exception as e:
            return _err(f"扩写+优化失败：{e}", code="EXPAND_FAILED", status=500)
        return web.json_response(result)

    async def optimize_multi(request):
        """多段提示词优化：把 N 段剧本逐段压成 H3 官方格式并校验。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            from . import optimizer as _opt
        except ImportError:
            import optimizer as _opt
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _opt.optimize_multi_once, data.get("config"), data)
        except ValueError as e:
            return _err(str(e), code="BAD_REQUEST", status=400)
        except RuntimeError as e:
            return _err(str(e), code="OPTIMIZE_FAILED", status=502)
        except Exception as e:
            return _err(f"多段优化失败：{e}", code="OPTIMIZE_FAILED", status=500)
        return web.json_response({"ok": bool(result.get("ok")), **result})

    async def expand_validate(request):
        """只校验已有信封（不调 LLM）：给前端"校验"按钮用。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            svc = _load_expander()
        except Exception as e:
            return _err(f"扩写模块未就绪：{e}", code="EXPAND_UNAVAILABLE", status=500)
        try:
            return web.json_response(svc.validate_only(data))
        except Exception as e:
            return _err(f"校验失败：{e}", code="VALIDATE_FAILED", status=500)

    async def compile_prompt(request):
        """结构化 prompt 编译预览（不落盘）：返回官方英文 + 校验，供段卡分组调用。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            from . import prompts as _prompts
        except ImportError:
            import prompts as _prompts
        prompt = data.get("prompt")
        if prompt is None and isinstance(data.get("segment"), dict):
            seg = data["segment"]
            prompt = seg.get("prompt_v2") or _prompts.migrate_legacy_seg(seg)
        try:
            seconds = float(data.get("seconds") or 5.0)
        except (TypeError, ValueError):
            seconds = 5.0
        has_start = bool(data.get("has_start"))
        has_end = bool(data.get("has_end"))
        mode = data.get("mode")
        mode = mode if isinstance(mode, str) and mode else None
        try:
            compiled = _prompts.compile_segment(prompt, seconds=seconds,
                                                has_start=has_start, has_end=has_end,
                                                mode=mode)
            verdict = _prompts.validate_compiled(compiled)
        except Exception as e:
            return _err(f"编译失败：{e}", code="COMPILE_FAILED", status=400)
        status = 200 if verdict["ok"] else 422
        return web.json_response({"ok": verdict["ok"], "compiled": compiled,
                                  "errors": verdict["errors"],
                                  "warnings": verdict["warnings"]}, status=status)

    async def move_media(request):
        """库间互调：assets <-> finals 搬家 + manifest 引用改写。生成中 423。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        if _busy():
            return _err("正在生成中，剪辑/转码入口已锁定（完成后自动解锁）",
                        code="BUSY", status=423)
        try:
            manifest = projects.move_media(
                str(data.get("dir") or ""), data.get("src"),
                str(data.get("dest") or ""), data.get("save_name"),
                data.get("base_revision"), bool(data.get("register_asset")),
                str(data.get("label") or ""), str(data.get("kind") or "video"))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _rev_conflict(str(data.get("dir") or ""), msg)
            return _err(msg, code="BAD_REQUEST", status=400)
        # 文件在两个库之间搬了家：素材库索引缓存必须失效，否则成片/资产两侧的
        # 瓦片与「调入」按钮（同名检测）都还是旧状态。
        h3lib.invalidate(str(data.get("dir") or ""))
        return web.json_response({"ok": True, "manifest": manifest})

    async def split_av(request):
        """音画分离：项目内 mp4 -> 画面 mp4 + 音频 wav（线程池）。生成中 423。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        if _busy():
            return _err("正在生成中，剪辑/转码入口已锁定（完成后自动解锁）",
                        code="BUSY", status=423)
        try:
            manifest = await asyncio.get_event_loop().run_in_executor(
                None, projects.split_av, str(data.get("dir") or ""),
                data.get("src"), data.get("save_video"), data.get("save_audio"),
                data.get("base_revision"))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _rev_conflict(str(data.get("dir") or ""), msg)
            return _err(msg, code="BAD_REQUEST", status=400)
        except RuntimeError as e:
            return _err(str(e), code="SPLIT_FAILED", status=500)
        return web.json_response({"ok": True, "manifest": manifest,
                                  "files": [(manifest.get("clips") or [{}])[-2:]]})

    async def import_asset(request):
        """入库：input 文件拷贝进项目 assets/ + 登记 manifest["assets"]。

        纯文件拷贝（无 GPU），生成中也可用；revision 冲突回 409 前端刷新重试。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            res = projects.import_asset(
                str(data.get("dir") or ""), data.get("src"),
                str(data.get("label") or ""), str(data.get("kind") or "image"),
                data.get("roles"), data.get("base_revision"))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _rev_conflict(str(data.get("dir") or ""), msg)
            return _err(msg, code="BAD_REQUEST", status=400)
        return web.json_response({"ok": True, **{k: v for k, v in res.items() if k != "manifest"},
                                  "manifest": res["manifest"]})

    # ---------- 素材库（Library）：一个浏览器 + 四个 scope ----------
    # 蓝本 Majoor Assets Manager 的 API 形状；索引/查询在 library.py，
    # 这里只做「HTTP 进出 + 本插件语义（段引用 / 角色标记）」的接线。

    def _indexed(dir_name):
        """构建索引并合入本插件语义：别名、角色标记、段引用、评分标签。"""
        manifest = projects.read_project(dir_name) if dir_name else None
        items = h3lib.build_index(dir_name)
        h3lib.apply_aliases(items, manifest)
        h3lib.apply_roles(items, manifest)
        h3lib.compute_refs(manifest, items)
        return h3lib.apply_meta(items, dir_name)

    def _find_item(dir_name, item_id):
        for e in _indexed(dir_name):
            if e["id"] == item_id:
                return e
        return None

    async def lib_list(request):
        q = request.query
        dir_name = str(q.get("dir") or "")
        items = _indexed(dir_name)
        coll = None
        cid = str(q.get("collection") or "")
        if cid:
            coll = []
            for c in h3lib.list_collections(dir_name):
                if str(c.get("id")) == cid:
                    coll = [str(x) for x in (c.get("items") or [])]
                    break
        data = h3lib.query(
            items, q=q.get("q"), scope=q.get("scope"), kind=q.get("kind"),
            sort=q.get("sort"), order=q.get("order"),
            page=q.get("page"), page_size=q.get("page_size"),
            collection=coll, seg=q.get("seg"), min_rating=q.get("min_rating"))
        # 目标库同名检测：用**完整索引**算（前端只有当前页，看不到目标库全貌），
        # 命中后前端直接不画那个方向的「调入」按钮。
        blocked = h3lib.blocked_targets(items)
        for e in data.get("items") or []:
            e["blocked"] = blocked.get(e["id"]) or []
        return web.json_response({"ok": True, "data": data,
                                  "counters": h3lib.counters(items)})

    async def lib_item(request):
        q = request.query
        dir_name = str(q.get("dir") or "")
        it = _find_item(dir_name, str(q.get("id") or ""))
        if it is None:
            return _err("素材不存在", code="NOT_FOUND", status=404)
        out = dict(it)
        out["exists"] = bool(h3lib.resolve_item_path(it, dir_name))
        return web.json_response({"ok": True, "item": out})

    async def lib_thumb(request):
        q = request.query
        dir_name = str(q.get("dir") or "")
        it = _find_item(dir_name, str(q.get("id") or ""))
        if it is None:
            return _err("素材不存在", code="NOT_FOUND", status=404)
        src = h3lib.resolve_item_path(it, dir_name)
        if not src:
            return _err("文件缺失", code="NOT_FOUND", status=404)
        p = h3lib.make_thumb(src, None if it["scope"] == "global" else dir_name,
                             it["id"], it["kind"])
        if not p:
            return _err("无缩略图（非图片或缺 Pillow）", code="NO_THUMB", status=404)
        return web.FileResponse(p)

    async def lib_raw(request):
        """原文件流：默认 inline（预览用）；`download=1` 带 attachment 头触发另存为。"""
        q = request.query
        dir_name = str(q.get("dir") or "")
        it = _find_item(dir_name, str(q.get("id") or ""))
        if it is None:
            return _err("素材不存在", code="NOT_FOUND", status=404)
        src = h3lib.resolve_item_path(it, dir_name)
        if not src:
            return _err("文件缺失", code="NOT_FOUND", status=404)
        if str(q.get("download") or "").lower() in ("1", "true", "yes"):
            fn = os.path.basename(src)
            return web.FileResponse(src, headers={
                "Content-Disposition": f'attachment; filename="{fn}"'})
        return web.FileResponse(src)

    async def lib_status(request):
        dir_name = str(request.query.get("dir") or "")
        items = _indexed(dir_name)
        return web.json_response({"ok": True, "counters": h3lib.counters(items),
                                  "total": len(items),
                                  "collections": h3lib.list_collections(dir_name)})

    async def lib_scan(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        dir_name = str(data.get("dir") or "")
        if dir_name:
            h3lib.invalidate(dir_name)
        else:
            h3lib.invalidate()
        items = _indexed(dir_name)
        return web.json_response({"ok": True, "counters": h3lib.counters(items),
                                  "total": len(items)})

    async def lib_rate(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        dir_name = str(data.get("dir") or "")
        it = _find_item(dir_name, str(data.get("id") or ""))
        if it is None:
            return _err("素材不存在", code="NOT_FOUND", status=404)
        scope, proj = h3lib.meta_target(it, dir_name)
        try:
            r = h3lib.set_rating(scope, proj, it["id"], data.get("rating"))
        except ValueError as e:
            return _err(str(e), code="BAD_ARGS", status=400)
        return web.json_response({"ok": True, **r})

    async def lib_tag(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        dir_name = str(data.get("dir") or "")
        it = _find_item(dir_name, str(data.get("id") or ""))
        if it is None:
            return _err("素材不存在", code="NOT_FOUND", status=404)
        scope, proj = h3lib.meta_target(it, dir_name)
        try:
            r = h3lib.set_tags(scope, proj, it["id"], data.get("tags"))
        except ValueError as e:
            return _err(str(e), code="BAD_ARGS", status=400)
        return web.json_response({"ok": True, **r})

    async def lib_alias(request):
        """改显示名（项目内的叫法）。

        查找顺序与显示名来源一致：项目链接（asset_links.alias）→ 项目资产
        （manifest.assets[].label）→ 全局库显示名（orig_name，仅限没链接的全局条目）。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            from . import asset_store
        except ImportError:
            import asset_store
        dir_name = str(data.get("dir") or "")
        # 别名 = `@别名` 引用语法的载体，唯一权威规则在 asset_store.clean_alias
        # （去扩展名 / 空白与标点压成 `_` / 截 24）——这里不再各写一份截断。
        alias = asset_store.clean_alias(data.get("alias"))
        if not alias:
            return _err("别名不能为空（只有空格或标点？改成中英文、数字或下划线）",
                        code="BAD_ARGS", status=400)
        it = _find_item(dir_name, str(data.get("id") or ""))
        if it is None:
            return _err("素材不存在", code="NOT_FOUND", status=404)
        mf = projects.read_project(dir_name) if dir_name else None
        old = str(it.get("name") or "").strip()

        def _rev_err(e):
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _err(msg, code="REVISION_CONFLICT", status=409)
            return _err(msg, code="BAD_ARGS", status=400)

        # 0) 重名预检：别名是引用命名空间（@别名），撞名会让引用指向错素材
        if mf is not None and old != alias:
            for a in (mf.get("assets") or []):
                if isinstance(a, dict) and str(a.get("label")) == alias:
                    return _err(f"已有同名素材「{alias}」：@别名 会指向它，请换个名字",
                                code="DUP_ALIAS", status=400)
            for L in (mf.get("asset_links") or []):
                if isinstance(L, dict) and str(L.get("alias")) == alias:
                    return _err(f"已有同名素材「{alias}」：@别名 会指向它，请换个名字",
                                code="DUP_ALIAS", status=400)

        # 1) 项目链接（asset_links.alias）+ 项目资产（assets[].label）**两处都改**。
        #    只改一处时显示名（apply_aliases 取 assets.label）纹丝不动 ——
        #    「点了改名没反应」的根因就在这里（上传即镜像的素材两处同名）。
        revision = None
        touched = False
        link = None
        for L in (mf or {}).get("asset_links") or []:
            if isinstance(L, dict) and str(L.get("alias")) == old:
                link = L
                break
        assets = None
        hit_asset = False
        if mf is not None:
            assets = [dict(a) if isinstance(a, dict) else a for a in (mf.get("assets") or [])]
            for a in assets:
                if isinstance(a, dict) and str(a.get("label")) == old:
                    a["label"] = alias
                    hit_asset = True
        if link is not None:
            try:
                out = projects.link_asset(
                    dir_name, link.get("asset_id"), alias,
                    link.get("kind") or it.get("kind") or "image",
                    data.get("base_revision"), None)
            except ValueError as e:
                return _rev_err(e)
            if out is None:
                return _err("改名失败（链接条目）", code="BAD_ARGS", status=400)
            revision = out.get("revision")
            touched = True
        if hit_asset:
            try:
                # 链接那步已经 +1 revision，这里不能再带旧 base_revision 去比对
                out = projects.save_assets(dir_name, assets, None)
            except ValueError as e:
                return _rev_err(e)
            if out is None:
                return _err("改名失败", code="BAD_ARGS", status=400)
            revision = out.get("revision")
            touched = True
        if touched:
            h3lib.invalidate(dir_name)
            return web.json_response({"ok": True, "alias": alias,
                                      "renamed": {"link": link is not None,
                                                  "asset": hit_asset},
                                      "revision": revision})

        # 2) 没登记进项目的全局库条目：改库内显示名
        if it["scope"] == "global" and it.get("asset_id"):
            root = h3lib._library_root()
            if not root:
                return _err("全局库不可用", code="NO_LIBRARY", status=400)
            lib = asset_store.load_library(root)
            hit = False
            for a in lib.get("assets") or []:
                if isinstance(a, dict) and str(a.get("asset_id")) == it["asset_id"]:
                    a["orig_name"] = alias
                    a["updated_at"] = time.time()
                    hit = True
                    break
            if not hit:
                return _err("全局库条目不存在", code="NOT_FOUND", status=404)
            asset_store.save_library(root, lib)
            h3lib.invalidate(dir_name)
            return web.json_response({"ok": True, "alias": alias})

        return _err("这个素材没登记进项目（先「调入项目」或上传时入库），改不了名",
                    code="NOT_IN_PROJECT", status=400)

    async def lib_mirror(request):
        """全局库条目 → 项目。两种模式：

        - `mode="link"`（默认）：**只写 asset_links**（链接引用，文件不复制）——
          双层存储的原意就是"一次入库、多项目链接"，也不会出现"项目里多一份、
          全局里还是那份"的困惑。项目资产视图会列出链接条目（带「链接」徽标）。
        - `mode="copy"`：拷一份进项目 assets/ 并登记 manifest.assets
          （项目自包含：删项目不连累全局库，但会多占一份磁盘）。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        dir_name = str(data.get("dir") or "")
        h3lib.invalidate(dir_name)          # 先刷索引：刚上传的条目要能立刻被找到
        it = _find_item(dir_name, str(data.get("id") or ""))
        if it is None:
            return _err("素材不存在（索引没刷到，可点「重新扫描」再试）",
                        code="NOT_FOUND", status=404)
        if it["scope"] != "global":
            return _err("只有全局库条目需要「调入项目」（项目资产已经在项目里了）",
                        code="NOT_GLOBAL", status=400)
        mode = str(data.get("mode") or "link").strip().lower()
        if mode == "copy":
            # 同名闸门：项目资产里已经有同名条目 -> 拒绝（前端连按钮都不画，
            # 这里兜底，避免绕过界面直接打接口复制出第二份）
            blocked = h3lib.blocked_targets(_indexed(dir_name)).get(it["id"]) or []
            if "project" in blocked:
                return _err(f"项目资产里已经有同名文件「{it['name']}」，不再调入",
                            code="DUP_NAME", status=409)
            try:
                res = h3lib.mirror_to_project(dir_name, it,
                                              str(data.get("label") or "") or None)
            except ValueError as e:
                return _err(str(e), code="BAD_ARGS", status=400)
            h3lib.invalidate(dir_name)
            return web.json_response({"ok": True, "mode": "copy", **res})
        aid = str(it.get("asset_id") or "")
        if not aid:
            return _err("这个全局条目没有 asset_id（旧数据）：请用「调入项目（复制文件）」",
                        code="NO_ASSET_ID", status=400)
        lbl = str(data.get("label") or "").strip() or str(it.get("name") or "")
        try:
            # orig_name：原始文件名（含后缀）——引用名由它生成，不用带 sha 前缀的
            # 落盘名（全局库 file 形如 images/<sha12>_猫.png）
            mf = projects.link_asset(dir_name, aid, lbl, it.get("kind") or "image",
                                     data.get("base_revision"),
                                     orig_name=it.get("orig_name") or it.get("name") or "")
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _rev_conflict(dir_name, msg)
            return _err(msg, code="BAD_ARGS", status=400)
        if mf is None:
            return _err("链接失败（项目不存在或参数非法）", code="BAD_ARGS", status=400)
        h3lib.invalidate(dir_name)
        return web.json_response({"ok": True, "mode": "link", "link": True,
                                  "alias": lbl, "asset_id": aid,
                                  "revision": mf.get("revision")})

    async def lib_collections(request):
        return web.json_response({"ok": True,
                                  "collections": h3lib.list_collections(
                                      str(request.query.get("dir") or ""))})

    async def lib_collection_save(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        try:
            c = h3lib.save_collection(str(data.get("dir") or ""), data.get("name"),
                                      data.get("items"), data.get("id"))
        except ValueError as e:
            return _err(str(e), code="BAD_ARGS", status=400)
        return web.json_response({"ok": True, "collection": c})

    async def lib_collection_delete(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        if not h3lib.delete_collection(str(data.get("dir") or ""), data.get("id")):
            return _err("集合不存在", code="NOT_FOUND", status=404)
        return web.json_response({"ok": True})

    async def lib_delete(request):
        """删除物理文件 **+ 同步清单**（三条清理，缺一条就出"删不掉的幽灵条目"）。

        以前只删文件、不动任何清单，于是同一个根因长出两个症状：
          - **全局库**：`scan_scope("global")` 以全局库 manifest 为唯一源 —— 文件删了
            条目还在，瓦片永远留在库里，用户看到的就是「全局库根本删除不了」
            （点确定后弹窗关了，什么都没变）；
          - **项目资产**：瓦片扫的是真目录会消失（所以看起来"能删"），但导演台
            「资产引用」栏以 `manifest["assets"]` 为唯一真相 —— 那一栏永远少不掉，
            留着一条点不亮的幽灵素材。
        现在：global 摘全局库登记、project 摘项目 assets/asset_links、linked 只解链，
        三者都顺带清掉 seg.refs 与正文里的 @别名；另附"还有别的项目在引用"提醒。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        dir_name = str(data.get("dir") or "")
        raw = data.get("ids")
        ids = [str(x) for x in raw] if isinstance(raw, list) else \
            ([str(data["id"])] if data.get("id") else [])
        paths = []
        unlinked = []
        notes = []
        drop_labels, drop_ids, drop_files = set(), set(), set()
        latent_files = []
        global_drops = []          # [(asset_id, file)]
        for i in ids:
            it = _find_item(dir_name, i)
            if it is None:
                continue
            if it.get("linked"):
                # 链接条目（asset_links）：只解链 —— 文件是全局库那份，绝不能删
                try:
                    if projects.unlink_asset(dir_name, asset_id=it.get("asset_id"),
                                             alias=it.get("name")) is not None:
                        unlinked.append(it["name"])
                except ValueError as e:
                    unlinked.append(f"{it['name']}（{e}）")
                continue
            p = h3lib.resolve_item_path(it, dir_name)
            if p:
                paths.append(p)
            scope = str(it.get("scope") or "")
            if scope == "global":
                global_drops.append((it.get("asset_id"), it.get("file")))
            elif scope == "project":
                if it.get("name"):
                    drop_labels.add(str(it["name"]))
                if it.get("file"):
                    drop_files.add(str(it["file"]))
                if it.get("asset_id"):
                    drop_ids.add(str(it["asset_id"]))
            elif scope == "latent":
                latent_files.append(str(it.get("file") or ""))
        # ① 全局库登记（manifest 是 scan_scope("global") 的唯一源）
        removed_global = []
        if global_drops:
            try:
                from . import asset_store
            except ImportError:
                import asset_store
            lib_root = h3lib._library_root()
            if not lib_root:
                notes.append("全局库不可用：文件已删，但库内登记没能清掉（刷新后会重新出现）")
            else:
                for aid, f in global_drops:
                    got = asset_store.remove_asset(lib_root, asset_id=aid, file=f)
                    if got.get("removed"):
                        removed_global.append(got["removed"])
        # ①b 本项目也链着它吗？那就顺手解开 —— 用户正站在这个项目里删它，
        #     留一条指向已删文件的链接只会让项目编译报"文件缺失"。
        #     （别的项目不动，只在下面出提醒：不替用户改别的项目的存档。）
        if removed_global and dir_name:
            cur_ids = set()
            try:
                cur_mf = projects.read_project(dir_name) or {}
                for x in (cur_mf.get("asset_links") or []):
                    if isinstance(x, dict) and x.get("asset_id"):
                        cur_ids.add(str(x["asset_id"]))
            except Exception:
                cur_ids = set()
            for e in removed_global:
                aid = str(e.get("asset_id") or "")
                if not aid or aid not in cur_ids:
                    continue
                try:
                    projects.unlink_asset(dir_name, asset_id=aid)
                    unlinked.append(str(e.get("orig_name") or aid))
                    notes.append("本项目里指向它的链接已一并解开"
                                 f"（{e.get('orig_name') or aid}）")
                except (ValueError, OSError) as ex:
                    notes.append(f"本项目解链失败：{ex}")
        # ② 项目清单（导演台「资产引用」栏的唯一真相）
        if drop_labels or drop_ids or drop_files:
            try:
                projects.remove_assets(dir_name, labels=drop_labels, asset_ids=drop_ids,
                                       files=drop_files)
            except (ValueError, OSError) as e:
                notes.append(f"项目清单清理失败：{e}")
        if latent_files:
            try:
                projects.remove_latents(dir_name, latent_files)
            except (ValueError, OSError) as e:
                notes.append(f"latent 清单清理失败：{e}")
        res = h3lib.delete_items(paths)
        # 全局库缓存键是 "global|"（不含项目名），invalidate(dir_name) 清不到它
        if global_drops:
            h3lib.invalidate()
        else:
            h3lib.invalidate(dir_name)
        # ③ 删掉的全局资产还有别的项目在链吗？断了要说出来，别让人过两天才发现
        if removed_global:
            aids = [str(e.get("asset_id") or "") for e in removed_global]
            try:
                hit = projects.projects_linking(aids, exclude=dir_name)
            except Exception:
                hit = {}
            for e in removed_global:
                users = hit.get(str(e.get("asset_id") or "")) or []
                if users:
                    notes.append(
                        f"⚠「{e.get('orig_name') or e.get('file')}」仍被 {len(users)} 个项目引用"
                        f"（{'、'.join(sorted(users))}）：那边的引用已失效，"
                        f"请重开项目重新挑素材")
        return web.json_response({"ok": True, **res, "unlinked": unlinked,
                                  "detached": len(removed_global) + len(drop_labels),
                                  "notes": notes, "requested": len(ids)})

    async def lib_archive(request):
        """把非全局库的条目存一份进全局库（成片产物自动备份，跨项目复用）。

        幂等：已带 asset_id（即已在全局库）的条目直接跳过，不重复入库。
        """
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        dir_name = str(data.get("dir") or "")
        raw = data.get("ids")
        ids = [str(x) for x in raw] if isinstance(raw, list) else \
            ([str(data["id"])] if data.get("id") else [])
        root = h3lib._library_root()
        if not root:
            return _err("全局库不可用", code="NO_LIBRARY", status=400)
        try:
            from . import asset_store
        except ImportError:
            import asset_store
        done, skipped = [], []
        blocked = h3lib.blocked_targets(_indexed(dir_name))
        for i in ids:
            it = _find_item(dir_name, i)
            if it is None or it["scope"] == "global":
                continue
            if it.get("asset_id"):
                continue                      # 已在全局库（幂等）
            if "global" in (blocked.get(it["id"]) or []):
                # 同名闸门：全局库已有同名条目 -> 不入库（前端按钮也不显示）
                skipped.append(f"{it['name']}（全局库已有同名）")
                continue
            src = h3lib.resolve_item_path(it, dir_name)
            if not src:
                skipped.append(it["name"])
                continue
            try:
                asset_store.register_content(root, src, it["kind"], [],
                                             "", orig_name=it["name"])
                done.append(it["name"])
            except Exception as e:
                skipped.append(f"{it['name']}（{e}）")
        # 无论是否真的新入库都要刷索引：按钮的显示与否取决于"另一端有没有同名"，
        # 缓存不失效的话，刚点完「存入全局库」按钮不会消失。
        h3lib.invalidate(dir_name)
        return web.json_response({"ok": True, "archived": done, "skipped": skipped})

    async def lib_stage(request):
        """把素材复制进 ComfyUI input 目录（原生 LoadImage/LoadVideo 只认 input）。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        dir_name = str(data.get("dir") or "")
        it = _find_item(dir_name, str(data.get("id") or ""))
        if it is None:
            return _err("素材不存在", code="NOT_FOUND", status=404)
        src = h3lib.resolve_item_path(it, dir_name)
        if not src:
            return _err("文件缺失", code="NOT_FOUND", status=404)
        try:
            res = h3lib.stage_to_input(src, data.get("subdir") or "h3_staged")
        except Exception as e:
            return _err(f"暂存失败：{e}", code="STAGE_FAILED", status=500)
        return web.json_response({"ok": True, **res})

    async def lib_zip(request):
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        dir_name = str(data.get("dir") or "")
        raw = data.get("ids")
        ids = [str(x) for x in raw] if isinstance(raw, list) else []
        paths = []
        for i in ids:
            it = _find_item(dir_name, i)
            if it is None:
                continue
            p = h3lib.resolve_item_path(it, dir_name)
            if p:
                paths.append(p)
        try:
            res = h3lib.make_zip(paths, dir_name or None)
        except ValueError as e:
            return _err(str(e), code="BAD_ARGS", status=400)
        return web.json_response({"ok": True, **res})

    async def lib_zip_file(request):
        q = request.query
        name = os.path.basename(str(q.get("name") or ""))
        dir_name = str(q.get("dir") or "")
        if not name or not dir_name:
            return _err("参数非法", code="BAD_ARGS", status=400)
        p = os.path.join(get_output_directory(), "h3_projects", dir_name, "_h3_zips", name)
        if not os.path.isfile(p):
            return _err("压缩包不存在", code="NOT_FOUND", status=404)
        return web.FileResponse(p, headers={
            "Content-Disposition": f'attachment; filename="{name}"'})

    async def lib_ref(request):
        """把素材引用到某段（写段 refs）。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        dir_name = str(data.get("dir") or "")
        it = _find_item(dir_name, str(data.get("id") or ""))
        if it is None:
            return _err("素材不存在", code="NOT_FOUND", status=404)
        try:
            seg_no = int(data.get("seg") or 0)
        except (TypeError, ValueError):
            seg_no = 0
        if seg_no < 1:
            return _err("段号非法", code="BAD_ARGS", status=400)
        mf = projects.read_project(dir_name)
        if mf is None:
            return _err("项目不存在（先新建项目或跑一段）", code="NOT_FOUND", status=404)
        # 全局库条目：先确认项目里有它的链接，否则 refs 里的别名编译不出 <Picture N>
        if it["scope"] == "global" and it.get("asset_id"):
            linked = any(isinstance(L, dict) and str(L.get("asset_id")) == it["asset_id"]
                         for L in (mf.get("asset_links") or []))
            if not linked:
                try:
                    out = projects.link_asset(
                        dir_name, it["asset_id"], it["name"],
                        it.get("kind") or "image", None, None,
                        orig_name=it.get("orig_name") or it.get("name") or "")
                except ValueError as e:
                    return _err(str(e), code="BAD_ARGS", status=400)
                if out is None:
                    return _err("把全局素材链接进项目失败", code="BAD_ARGS", status=400)
                mf = out
        segs = [dict(s) if isinstance(s, dict) else {} for s in (mf.get("segments") or [])]
        while len(segs) < seg_no:
            segs.append({})
        key = it["asset_id"] if it.get("asset_id") else it["name"]
        cur = list(segs[seg_no - 1].get("refs") or [])
        if key not in cur:
            cur.append(key)
        segs[seg_no - 1]["refs"] = cur
        try:
            out = projects.save_prompts(dir_name, mf.get("prompts") or [], segs,
                                        data.get("base_revision"))
        except ValueError as e:
            return _err(str(e), code="REVISION_CONFLICT", status=409)
        if out is None:
            return _err("保存失败", code="BAD_ARGS", status=400)
        h3lib.invalidate(dir_name)
        return web.json_response({"ok": True, "refs": cur, "revision": out.get("revision")})

    async def lib_role(request):
        """首帧图 / 尾帧图打标（全库唯一，改标自动让位）。"""
        try:
            data = await request.json()
        except Exception:
            return _err("请求体不是合法 JSON", code="BAD_JSON", status=400)
        dir_name = str(data.get("dir") or "")
        role = str(data.get("role") or "")
        if role not in ("首帧图", "尾帧图"):
            return _err("role 非法", code="BAD_ARGS", status=400)
        it = _find_item(dir_name, str(data.get("id") or ""))
        if it is None:
            return _err("素材不存在", code="NOT_FOUND", status=404)
        mf = projects.read_project(dir_name)
        if mf is None:
            return _err("项目不存在（先新建项目或跑一段）", code="NOT_FOUND", status=404)
        # 全局库条目：写进 asset_links 的 roles（不复制文件，链接即参与编译）
        if it["scope"] == "global" and it.get("asset_id"):
            cur = []
            for L in mf.get("asset_links") or []:
                if isinstance(L, dict) and str(L.get("asset_id")) == it["asset_id"]:
                    cur = [str(r) for r in (L.get("roles") or [])]
            nxt = [r for r in cur if r != role] if role in cur else (cur + [role])
            try:
                out = projects.link_asset(dir_name, it["asset_id"], it["name"],
                                          it.get("kind") or "image",
                                          data.get("base_revision"), nxt)
            except ValueError as e:
                msg = str(e)
                if msg.startswith("REVISION_CONFLICT"):
                    return _err(msg, code="REVISION_CONFLICT", status=409)
                return _err(msg, code="BAD_ARGS", status=400)
            if out is None:
                return _err("保存失败（全局条目链接不上）", code="BAD_ARGS", status=400)
            h3lib.invalidate(dir_name)
            return web.json_response({"ok": True, "roles": nxt,
                                      "revision": out.get("revision")})
        assets = [dict(a) if isinstance(a, dict) else a for a in (mf.get("assets") or [])]
        hit = None
        for a in assets:
            if isinstance(a, dict) and str(a.get("label")) == it["name"]:
                hit = a
                break
        if hit is None:
            return _err("该素材还没登记进项目（先「调入项目」，或上传时勾选入库）",
                        code="NOT_IN_PROJECT", status=400)
        cur = [str(r) for r in (hit.get("roles") or [])]
        if role in cur:
            cur = [r for r in cur if r != role]
        else:
            for a in assets:
                if isinstance(a, dict) and isinstance(a.get("roles"), list) and role in a["roles"]:
                    a["roles"] = [r for r in a["roles"] if r != role]
            cur.append(role)
        hit["roles"] = cur
        try:
            out = projects.save_assets(dir_name, assets, data.get("base_revision"))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("REVISION_CONFLICT"):
                return _err(msg, code="REVISION_CONFLICT", status=409)
            return _err(msg, code="BAD_ARGS", status=400)
        if out is None:
            return _err("保存失败", code="BAD_ARGS", status=400)
        h3lib.invalidate(dir_name)
        return web.json_response({"ok": True, "roles": cur, "revision": out.get("revision")})

    # ---- 手动锚定（Anchor Studio）：常量来源 / 源清单 / contact sheet ----
    # grid / anchors 延迟导入：与本文件其余可选依赖同口径，也让 routes.py 在无包
    # 上下文的单测里能按文件路径加载（模块级相对导入会直接 ImportError）。

    def _proj_root(dir_name):
        """项目目录（已过 safe_rel 防穿越）；不存在返回 None。"""
        rel = h3lib.safe_rel(str(dir_name or ""))
        if not rel:
            return None
        root = os.path.join(get_output_directory(), "h3_projects", *rel.split("/"))
        return root if os.path.isdir(root) else None

    def _latent_shape(abs_path):
        """latent .pt -> (frames, tokens, w, h)；读不出来返回 None。

        只有 torch 读得出形状。读不出来时调用方标 meta_ok=False，前端据此降到
        「刻度条 + 数字」——不给一个猜的时长（规划 §8：latent 无 fps 强显秒数
        会把时长显示错）。
        """
        from . import grid as h3grid
        try:
            import torch
        except ImportError:
            return None
        try:
            with open(abs_path, "rb") as fh:
                pay = torch.load(fh, map_location="cpu", weights_only=True)
            v = pay.get("video")
            if v is None or getattr(v, "dim", lambda: 0)() != 5:
                return None
            t = int(v.shape[2])
            return (h3grid.latent_t_to_frames(t), t, int(v.shape[-1]), int(v.shape[-2]))
        except Exception:
            return None

    def _seg_sources(root, upto=None):
        """项目里已落盘的 seg_NNN.pt -> 段源条目（按段号升序）。"""
        out = []
        for name in sorted(os.listdir(root)):
            if not (name.startswith("seg_") and name.endswith(".pt")):
                continue
            num = name[4:-3]
            if not num.isdigit():
                continue
            slot = int(num)
            if upto is not None and slot >= upto:
                continue
            abs_path = os.path.join(root, name)
            info = _latent_shape(abs_path)
            sheet = h3lib.sheet_path_for(abs_path)
            out.append({
                "slot": slot, "kind": "segment", "ref": f"seg_{slot:03d}",
                "label": f"段 {slot + 1}",
                "frames": info[0] if info else None,
                "tokens": info[1] if info else None,
                "w": info[2] if info else None, "h": info[3] if info else None,
                "fps": 24.0,          # 段 latent 恒 24fps（save_segment_mp4 默认）
                "sheet": (os.path.relpath(sheet, root).replace("\\", "/")
                          if os.path.isfile(sheet) else None),
                "meta_ok": info is not None,
            })
        return out

    async def grid_spec(request):
        """锚定常量的唯一来源：前端据此画 token 刻度、锁窗宽档位，不硬编码数字。"""
        from . import anchors as h3anchors
        from . import grid as h3grid
        return web.json_response({
            "ok": True,
            "frame_per_token": list(h3grid.FRAME_PER_TOKEN),
            "frame_rescale": float(h3grid.FRAME_RESCALE),
            "snap_windows": list(h3grid.SNAP_WINDOWS),
            "min_window_frames": h3grid.MIN_WINDOW_FRAMES,
            "max_window_frames": h3grid.MAX_WINDOW_FRAMES,
            "at_modes": list(h3grid.AT_MODES),
            "src_kinds": list(h3anchors.SRC_KINDS),
            "branches": list(h3anchors.BRANCHES),
        })

    def _source_meta(abs_path, item_kind, root):
        """单个素材文件 -> 源轨元信息 {frames,tokens,w,h,fps,sheet,meta_ok}；读不出 None。

        只探测用户点选的**那一个**文件，不扫目录、不建索引：帧数/尺寸/帧率是源轨刻度
        与越界体检的输入，只能由后端按同一套安全解析规则给出。编一个假值把时长显示错，
        比不显示更糟（规划 §8）。
        """
        if item_kind == "latent":
            info = _latent_shape(abs_path)
            if not info:
                return None
            sheet = h3lib.sheet_path_for(abs_path)
            return {"frames": info[0], "tokens": info[1], "w": info[2], "h": info[3],
                    "fps": None, "meta_ok": True,
                    "sheet": (os.path.relpath(sheet, root).replace("\\", "/")
                              if os.path.isfile(sheet) else None)}
        if item_kind == "image":
            try:
                from PIL import Image
                with Image.open(abs_path) as im:
                    w, h = im.size
            except Exception:
                return None
            return {"frames": 1, "tokens": None, "w": int(w), "h": int(h),
                    "fps": None, "sheet": None, "meta_ok": True}
        if item_kind == "audio":
            # 音频没有帧/宽高，但有**时长**——源轨要按它画时间线（按 24fps 折算成
            # 等效帧，与窗宽「像素帧」同一刻度；窗宽仍是唯一的取用契约）。
            # 探测失败不报错：帧数回退 None，面板按窗宽画轨，生成不受影响。
            dur = 0.0
            try:
                import av
                with av.open(abs_path) as c:
                    st = next((x for x in c.streams if x.type == "audio"), None)
                    if st is not None and st.duration and st.time_base:
                        dur = float(st.duration * st.time_base)
            except Exception:
                dur = 0.0
            return {"frames": int(round(dur * 24)) if dur > 0 else None,
                    "tokens": None, "w": None, "h": None,
                    "fps": None, "sheet": None, "meta_ok": dur > 0,
                    "duration": (round(dur, 2) if dur > 0 else None)}
        if item_kind == "video":
            try:
                import av
                with av.open(abs_path) as c:
                    vs = next((x for x in c.streams if x.type == "video"), None)
                    if vs is None:
                        return None
                    fps = float(vs.average_rate) if vs.average_rate else None
                    n = int(vs.frames) if vs.frames else 0
                    if n <= 0 and vs.duration and vs.time_base and fps:
                        n = int(float(vs.duration * vs.time_base) * fps)
                    return {"frames": (n or None), "tokens": None,
                            "w": int(vs.width or 0) or None,
                            "h": int(vs.height or 0) or None,
                            "fps": fps, "sheet": None, "meta_ok": n > 0}
            except Exception:
                return None
        return None

    def _find_lib_item(dir_name, ref):
        """库条目 id / asset_id / 别名 / 相对 file -> (条目, 失败原因)。"""
        r = str(ref or "")
        for e in _indexed(dir_name):
            if r and r in (str(e.get("id") or ""), str(e.get("asset_id") or ""),
                           str(e.get("name") or ""), str(e.get("file") or "")):
                return e, ""
        return None, f"素材库里找不到「{ref}」（可能已被删除或改名）"

    def _anchor_ref_of(item):
        """库条目 -> anchor.src.ref（执行期 nodes.py 能寻址的标识）。

        latent 用 `latent/<名>.pt`（_load_library_latent 认这个形式）；其余优先 asset_id
        —— 全局库与项目链接在**执行期只按 asset_id 注册**（注册表 by_alias 里只有旧
        label / 链接别名），拿 orig_name 当标签会查不到。项目目录直接扫到的文件没有
        asset_id 时才回落别名（name 就是清单里的 label）。
        """
        if item.get("kind") == "latent":
            return "latent/" + os.path.basename(str(item.get("file") or ""))
        return str(item.get("asset_id") or "").strip() or str(item.get("name") or "").strip()

    async def _anchor_sources_impl(request):
        """源轨源清单：已完成段 + 项目 latent；给了 `ref=`（可多个）则把对应素材条目并进来。

        **不枚举素材**：图片/视频/音频一律从素材库（web/h3_library.js 的挑选模式）选，
        本接口只回答「你选的那个素材是多少帧 / 多大 / 多少 fps」。此前在这里扫
        ComfyUI input 目录枚举视频、从 manifest["assets"] 枚举图片，方向是错的：
        那等于另造一个素材库，与全局库/项目库两套数据必然漂移。
        找不到的 ref 不报错（进 `missing`）：刷新合并旧锚时素材可能确已删除，
        面板要的是「哪条失效了」，不是整个清单 404。
        """
        q = request.query
        dir_name = str(q.get("dir") or "")
        root = _proj_root(dir_name)
        man = projects.read_project(h3lib.safe_rel(dir_name)) if dir_name else None
        if root is None or man is None:
            return _err("项目不存在（先在顶部填「存档目录」并跑过至少一段，"
                        "才有段/latent 可作源）", code="NOT_FOUND", status=404)
        try:
            seg_no = int(q.get("seg") or 0)      # 1-based 段号
        except (TypeError, ValueError):
            seg_no = 0
        segs = _seg_sources(root)
        srcs = []
        # 上段尾：本段的前一个**全局槽位**。有「序章」时槽号整体后移一位（序章占 0 号），
        # 少算这个偏移就会把「上段尾」指到序章上（前端据此预填「上一段」）。
        # 这个条目保留只为**旧存档回显**：UI 已不提供该选项（用户明确说冗余——
        # 「有上段的选择就行了」，选上一段拿到的就是同一份 latent）；后端仍把
        # prev_tail 当隐式默认段首桥（没有显式 head anchor 时才走）。
        prev_slot = seg_no - 2 + (1 if man.get("has_prologue") else 0)
        for e in segs:
            if e["slot"] == prev_slot:
                srcs.append({**e, "kind": "prev_tail", "ref": "",
                             "label": f"上段尾（{e['label']}）"})
                break
        # 段列表给全（含上一段）：用户要选上一段时，它必须真的在「段」下拉里，
        # 否则会出现"说是让我选上一段，可列表里根本没有"。
        srcs.extend(segs)
        for rec in (man.get("latents") or []):
            if not isinstance(rec, dict) or not rec.get("file"):
                continue
            rel = str(rec["file"]).replace("\\", "/")
            parts = [p for p in rel.split("/") if p and p != "."]
            if len(parts) != 2 or parts[0] != "latent":
                continue
            abs_path = os.path.join(root, "latent", parts[1])
            if not os.path.isfile(abs_path):
                continue
            info = _latent_shape(abs_path)
            sheet = h3lib.sheet_path_for(abs_path)
            srcs.append({
                "slot": None, "kind": "library", "ref": rel, "label": parts[1],
                "frames": info[0] if info else rec.get("frames"),
                "tokens": info[1] if info else None,
                "w": info[2] if info else rec.get("w"),
                "h": info[3] if info else rec.get("h"),
                "fps": rec.get("fps"),
                "sheet": (os.path.relpath(sheet, root).replace("\\", "/")
                          if os.path.isfile(sheet) else None),
                "meta_ok": info is not None,
            })
        # ---- 面板里（本段所有锚）已选中的素材（可从全局库/项目库/成片/latent 任一处来）----
        # 支持**多个** ref：刷新/重开面板时前端会把存档里已有锚的 ref 一并带来合并元信息
        # ——否则已选素材不在清单里，面板误报「源不在源清单里（旧档）」，用户被迫重选。
        missing = []
        # 注意：multidict 的 getall(key) **缺 key 直接抛 KeyError**（不是返回 []），
        # 未捕获异常在 aiohttp 里就是一个空 500 —— 必须显式给默认值。
        want_refs = [str(x) for x in q.getall("ref", []) if str(x or "").strip()] \
            if hasattr(q, "getall") else []
        if not want_refs:
            want_refs = [str(q.get("ref") or "")] if str(q.get("ref") or "").strip() else []
        for want_ref in want_refs:
            item, why = _find_lib_item(dir_name, want_ref)
            if item is None:
                missing.append(want_ref)
                continue
            item_kind = str(item.get("kind") or "")
            if item_kind not in ("image", "video", "latent", "audio"):
                missing.append(want_ref)
                continue
            abs_p = h3lib.resolve_item_path(item, h3lib.safe_rel(dir_name))
            if not abs_p:
                missing.append(want_ref)
                continue
            meta = _source_meta(abs_p, item_kind, root)
            if meta is None:
                missing.append(want_ref)
                continue
            # 锚的源类型只有四个（library/video/image/audio），latent 归 library
            kind = "library" if item_kind == "latent" else item_kind
            ref = _anchor_ref_of(item)
            labels = {str(a.get("label") or "")
                      for a in (man.get("assets") or []) if isinstance(a, dict)}
            srcs = [s for s in srcs if not (s["kind"] == kind and s.get("ref") == ref)]
            srcs.append({
                "slot": None, "kind": kind, "ref": ref,
                "label": str(item.get("name") or ref), "item_id": item.get("id"),
                **meta,
                # 执行期注册表只认 asset_id 与清单里的别名：两者都没有时节点侧寻址不到，
                # 面板据此提前提示（比等到跑生成才报「未知素材标签」好得多）。
                "resolvable": bool(str(item.get("asset_id") or "").strip())
                              or (item.get("scope") == "project" and ref in labels),
            })

        params = man.get("params") or {}
        return web.json_response({
            "ok": True, "sources": srcs, "missing": missing,
            "seg_length": int(params.get("length") or 0),
            "parse": man.get("parse") or {},
        })

    async def anchor_sources(request):
        """anchor_sources 的异常兜底外壳。

        未捕获异常在 aiohttp 里只回一个空 500，前端只能显示「HTTP 500 · path」，
        用户既不知道原因、也没法自己处理（这是 2026-09-17 用户实际撞到的界面）。
        这里统一转成**带原因的** JSON：面板能显示「读取源清单失败：XXXError: …」，
        服务端控制台同时留全栈。
        """
        try:
            return await _anchor_sources_impl(request)
        except Exception as e:
            try:
                traceback.print_exc()
            except Exception:
                pass
            return _err(f"读取源清单失败：{type(e).__name__}: {e}"
                        f"（项目 {request.query.get('dir') or '?'}）",
                        code="ANCHOR_SOURCES_FAILED", status=500)

    def _sheet_rel_ok(rel):
        """只认 latent/<名>.sheet.png（防穿越）。"""
        parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p and p != "."]
        if len(parts) != 2 or parts[0] != "latent" or not parts[1].endswith(".sheet.png"):
            return None
        name = parts[1]
        if name.startswith(".") or ":" in name or ".." in name:
            return None
        return f"latent/{name}"

    async def anchor_sheet(request):
        q = request.query
        root = _proj_root(str(q.get("dir") or ""))
        rel = _sheet_rel_ok(q.get("file"))
        if root is None or rel is None:
            return _err("参数非法（file 须为 latent/<名>.sheet.png）", code="BAD_ARGS", status=400)
        p = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(p):
            return _err("缩略图不存在", code="NOT_FOUND", status=404)
        return web.FileResponse(p)

    async def anchor_sheet_build(request):
        """按需补生成 contact sheet（规划 §5 第三级降级：老条目无 sheet）。

        **不加载 VAE**：像素来源优先取同段已落盘的 finals/seg_NNN.mp4
        （帧序与 latent 严格一一对应），其次取 registry 记的源素材文件。
        两者都取不到（例如源视频已删）时明确报错让用户重跑该段，
        而不是给一张假图——时间线的缩略图错位比没有缩略图更糟。
        """
        try:
            data = await request.json()
        except Exception:
            data = {}
        # dir_name 必须先取出来：下面 read_project(safe_rel(dir_name)) 要用它，
        # 而此前这里只解了 root、没定义 dir_name —— 该路由必然 NameError 500。
        dir_name = str((data or {}).get("dir") or "")
        root = _proj_root(dir_name)
        src_rel = str((data or {}).get("file") or "").replace("\\", "/")
        parts = [p for p in src_rel.split("/") if p and p != "."]
        if root is None or len(parts) != 2 or parts[0] != "latent" \
                or not parts[1].endswith(".pt"):
            return _err("参数非法（file 须为 latent/<名>.pt）", code="BAD_ARGS", status=400)
        sheet_abs = h3lib.sheet_path_for(os.path.join(root, "latent", parts[1]))
        rel = f"latent/{parts[1][:-3]}.sheet.png"
        if os.path.isfile(sheet_abs):
            return web.json_response({"ok": True, "sheet": rel, "cached": True})
        man = projects.read_project(h3lib.safe_rel(dir_name)) or {}
        entry = next((r for r in (man.get("latents") or [])
                      if isinstance(r, dict) and str(r.get("file") or "").replace("\\", "/")
                      == src_rel), None)
        cands = []
        src = str((entry or {}).get("src") or "")
        if src.startswith("seg_") and src.endswith(".pt"):
            cands.append(os.path.join(root, "finals",
                                      src[:-3] + ".mp4"))          # 分段视频（帧序一一对应）
        if src and not src.endswith(".pt"):
            cands.append(os.path.join(root, src))
        video = next((c for c in cands if c and os.path.isfile(c)), None)
        if video is None:
            return _err("该条目没有可用像素源（源素材或分段视频已不在项目里）——"
                        "重跑该段即可顺带生成缩略图",
                        code="NO_SOURCE", status=409)
        try:
            from . import media
            frames, _wav, _sr = media.decode_av(video, None, None, None)
        except Exception as e:
            return _err(f"抽帧失败：{type(e).__name__}: {e}", code="DECODE_FAILED", status=500)
        out = h3lib.make_sheet(frames, sheet_abs)
        if not out:
            return _err("拼图失败（缺 Pillow 或帧为空）", code="NO_SHEET", status=500)
        return web.json_response({"ok": True, "sheet": rel, "cached": False})

    handlers = [
        ("GET", "/h3chain/ping", ping),
        ("GET", "/h3chain/lib_list", lib_list),
        ("GET", "/h3chain/lib_item", lib_item),
        ("GET", "/h3chain/lib_thumb", lib_thumb),
        ("GET", "/h3chain/lib_raw", lib_raw),
        ("GET", "/h3chain/lib_status", lib_status),
        ("GET", "/h3chain/lib_collections", lib_collections),
        ("GET", "/h3chain/lib_zip_file", lib_zip_file),
        ("POST", "/h3chain/lib_scan", lib_scan),
        ("POST", "/h3chain/lib_rate", lib_rate),
        ("POST", "/h3chain/lib_tag", lib_tag),
        ("POST", "/h3chain/lib_alias", lib_alias),
        ("POST", "/h3chain/lib_mirror", lib_mirror),
        ("POST", "/h3chain/lib_archive", lib_archive),
        ("POST", "/h3chain/lib_collection_save", lib_collection_save),
        ("POST", "/h3chain/lib_collection_delete", lib_collection_delete),
        ("POST", "/h3chain/lib_delete", lib_delete),
        ("POST", "/h3chain/lib_stage", lib_stage),
        ("POST", "/h3chain/lib_zip", lib_zip),
        ("POST", "/h3chain/lib_ref", lib_ref),
        ("POST", "/h3chain/lib_role", lib_role),
        ("GET", "/h3chain/grid_spec", grid_spec),
        ("GET", "/h3chain/anchor_sources", anchor_sources),
        ("GET", "/h3chain/anchor_sheet", anchor_sheet),
        ("POST", "/h3chain/anchor_sheet_build", anchor_sheet_build),
        ("GET", "/h3chain/busy", busy_state),
        ("GET", "/h3chain/projects", list_projects),
        ("GET", "/h3chain/project", project_detail),
        ("GET", "/h3chain/upscale_models", upscale_models),
        ("GET", "/h3chain/vae_files", vae_files),
        ("GET", "/h3chain/experiments", experiment_defs),
        ("GET", "/h3chain/prompt-rules", prompt_rules),
        ("GET", "/h3chain/optimizer-config", optimizer_config),
        ("POST", "/h3chain/optimize", optimize),
        ("POST", "/h3chain/optimize_stream", optimize_stream),
        ("POST", "/h3chain/expand", expand),
        ("POST", "/h3chain/expand_multi", expand_multi),
        ("POST", "/h3chain/expand_optimize", expand_optimize),
        ("POST", "/h3chain/expand_optimize_stream", expand_optimize_stream),
        ("POST", "/h3chain/optimize_multi", optimize_multi),
        ("POST", "/h3chain/expand_validate", expand_validate),
        ("POST", "/h3chain/create_project", create_project),
        ("POST", "/h3chain/save_prompts", save_prompts),
        ("POST", "/h3chain/compile", compile_prompt),
        ("POST", "/h3chain/latent_slice", latent_slice),
        ("POST", "/h3chain/latent_delete", latent_delete),
        ("POST", "/h3chain/trim", trim),
        ("POST", "/h3chain/probe", probe),
        ("POST", "/h3chain/move_media", move_media),
        ("POST", "/h3chain/split_av", split_av),
        ("POST", "/h3chain/import_asset", import_asset),
        ("POST", "/h3chain/assets", save_assets),
        ("POST", "/h3chain/asset_check", asset_check),
        ("POST", "/h3chain/compile_refs", compile_refs),
        ("POST", "/h3chain/transcode_submit", transcode_submit),
        ("GET", "/h3chain/transcode_jobs", transcode_jobs),
        ("GET", "/h3chain/transcode_job", transcode_job),
        ("POST", "/h3chain/transcode_cancel", transcode_cancel),
        ("POST", "/h3chain/library_upload", library_upload),
        ("GET", "/h3chain/library_file", library_file),
        ("GET", "/h3chain/asset_links", asset_links),
        ("POST", "/h3chain/asset_link", asset_link),
        ("POST", "/h3chain/asset_mark", asset_mark),
        ("POST", "/h3chain/asset_unlink", asset_unlink),
        ("POST", "/h3chain/asset_mirror", asset_mirror),
        ("POST", "/h3chain/delete_project", delete_project),
        ("POST", "/h3chain/delete_file", delete_file),
        ("POST", "/h3chain/merge", merge),
        ("POST", "/h3chain/upscale_reset", upscale_reset),
        ("POST", "/h3chain/redo_cancel", redo_cancel),
    ]

    def _mount(target, method, path, handler):
        keys = None
        for obj, ks in _mounted:
            if obj is target:
                keys = ks
                break
        if keys is None:
            keys = set()
            _mounted.append((target, keys))
        if (method, path) in keys:
            return
        if hasattr(target, "add_post"):
            # 运行中真实路由表（UrlDispatcher：app.router 的 add_get/add_post）
            getattr(target, f"add_{method.lower()}")(path, handler)
        elif hasattr(target, "post"):
            # RouteTableDef 装饰器写法（旧版 ComfyUI，加载后才统一装载）
            getattr(target, method.lower())(path)(handler)
        else:
            raise TypeError("add_routes: 不支持的 routes 类型 %r" % type(target))
        keys.add((method, path))

    for method, path, handler in handlers:
        _mount(routes, method, path, handler)             # 根路径：浏览器直访 / 旧前端
        _mount(routes, method, "/api" + path, handler)    # /api 副本：新版前端 fetchApi 强制前缀


def ensure_registered() -> bool:
    """运行期兜底：未注册成功时重试一次（幂等）。

    节点每次 execute 前调用——即使导入期的注册因时序问题全部失败，
    第一次排队执行后路由也会就位，前端删除/列表不再 404/405。
    """
    global _registered
    if _registered:
        return True
    try:
        return register()
    except Exception:
        return False


def register(routes=None):
    """把路由挂到 PromptServer.instance.routes（自定义节点标准方式）。

    兼容多种运行环境：
    - 标准 ComfyUI：custom nodes 加载晚于 PromptServer 构造，instance 已就绪。
    - Comfy Desktop / 新版 ComfyUI：可能通过 ComfyExtension.add_routes(routes)
      直接把 routes 对象传进来，此时用 routes 参数注册。
    - PromptServer.instance 尚未构造：给 PromptServer.__init__ 安装钩子，
      在实例化瞬间自动注册。
    - 测试环境：无 server 模块，返回 False。
    """
    global _registered
    if _registered:
        return True

    # 扩展钩子直接传入了 routes 对象（Comfy Desktop / 新版 ComfyUI 官方路径）
    if routes is not None:
        try:
            add_routes(routes)
            _registered = True
            print(f"[ComfyUI_H3_SeamlessChain] 路由已注册（扩展钩子，含 /api 前缀副本）：{_ROUTES_LOG}")
            return True
        except Exception:
            print("[ComfyUI_H3_SeamlessChain] 路由注册失败（删除功能不可用）。详细错误：")
            traceback.print_exc()
            return False

    PromptServer = _import_promptserver()
    if PromptServer is None:
        print("[ComfyUI_H3_SeamlessChain] 未找到 PromptServer，跳过路由注册（测试环境正常）")
        return False

    inst = getattr(PromptServer, "instance", None)
    if inst is None:
        # PromptServer 尚未构造：装钩子，实例化后自动注册
        _install_promptserver_hook(PromptServer)
        return False

    # 优先直接挂到运行中的 app.router：绕过 RouteTableDef 的挂载时序问题。
    # ComfyUI 部分版本（如 0.33.x）在 custom node 导入前已执行
    # app.add_routes(PromptServer.instance.routes)，导入期加进 RouteTableDef
    # 的路由不会被装载，导致代码里"注册成功"但浏览器访问 404。
    app = getattr(inst, "app", None)
    router = getattr(app, "router", None) if app is not None else None
    target = router if router is not None else getattr(inst, "routes", None)
    if target is not None:
        try:
            add_routes(target)
            _registered = True
            where = "app.router" if router is not None else "PromptServer.instance.routes"
            print(f"[ComfyUI_H3_SeamlessChain] 路由已注册（{where}，含 /api 前缀副本）：{_ROUTES_LOG}")
            return True
        except Exception:
            # app.router 失败则回退 RouteTableDef（保留旧版兼容）
            routes2 = getattr(inst, "routes", None)
            if routes2 is not None and routes2 is not target:
                try:
                    add_routes(routes2)
                    _registered = True
                    print(f"[ComfyUI_H3_SeamlessChain] 路由已注册（RouteTableDef，含 /api 前缀副本）：{_ROUTES_LOG}")
                    return True
                except Exception:
                    traceback.print_exc()
                    return False
            print("[ComfyUI_H3_SeamlessChain] 路由注册失败（删除功能不可用）。详细错误：")
            traceback.print_exc()
            return False

    # app 还没建好：装钩子，等 __init__ 完成后重试（此时 app.router 应已就绪）
    _install_promptserver_hook(PromptServer)
    return False


def _import_promptserver():
    """尝试多种 PromptServer 导入路径（标准 ComfyUI / Comfy Desktop / 打包版）。"""
    import importlib
    for path in ("server", "comfy.server", "comfy_api.server"):
        try:
            return importlib.import_module(path).PromptServer
        except Exception:
            continue
    return None


def _install_promptserver_hook(PromptServer):
    """当 PromptServer 实例化时自动注册路由（处理加载顺序不一致的 Desktop 环境）。"""
    if getattr(PromptServer, "_h3_route_hook_installed", False):
        return
    orig_init = PromptServer.__init__

    def _h3_init(self, *args, **kwargs):
        result = orig_init(self, *args, **kwargs)
        try:
            register()
        except Exception:
            pass
        return result

    PromptServer.__init__ = _h3_init
    PromptServer._h3_route_hook_installed = True
    print("[ComfyUI_H3_SeamlessChain] PromptServer 尚未实例化，已安装实例化后自动注册路由的钩子")
