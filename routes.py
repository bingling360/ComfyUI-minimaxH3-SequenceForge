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
import traceback

from aiohttp import web
from folder_paths import get_output_directory

from . import projects

_registered = False
_mounted = []  # [(目标对象, {(method, path)})]：按对象身份防重复挂载（持引用防 id 复用）

ROUTES = [
    ("GET", "/h3chain/ping"),
    ("GET", "/h3chain/projects"),
    ("GET", "/h3chain/project"),
    ("GET", "/h3chain/upscale_models"),
    ("GET", "/h3chain/experiments"),
    ("POST", "/h3chain/create_project"),
    ("POST", "/h3chain/save_prompts"),
    ("POST", "/h3chain/delete_project"),
    ("POST", "/h3chain/delete_file"),
    ("POST", "/h3chain/merge"),
    ("POST", "/h3chain/upscale_reset"),
    ("POST", "/h3chain/redo_cancel"),
]

_ROUTES_LOG = ", ".join(f"{m} {p}" for m, p in ROUTES)


def _routes_desc() -> list:
    return [{"method": m, "path": p} for m, p in ROUTES]


def add_routes(routes):
    """把路由挂到 routes 对象（aiohttp）。

    routes 可能是：
    - web.RouteTableDef（自定义节点标准写法，支持 routes.get/post 装饰器）
    - app.router（UrlDispatcher，运行中的真实路由表，用 add_get/add_post 方法）——
      ComfyUI 0.33.x 在 custom node 导入前已 app.add_routes(routes)，
      直接挂到 app.router 才能绕过挂载时序问题。
    """

    async def ping(request):
        return web.json_response({"ok": True, "version": "v3", "routes": _routes_desc()})

    async def list_projects(request):
        return web.json_response({"ok": True, "projects": projects.list_projects()})

    async def project_detail(request):
        manifest = projects.read_project(request.query.get("dir") or "")
        if manifest is None:
            return web.json_response({"error": "项目不存在"}, status=404)
        return web.json_response({"ok": True, "manifest": manifest})

    async def create_project(request):
        data = await request.json()
        manifest = projects.create_project(str(data.get("dir") or ""))
        if manifest is None:
            return web.json_response({"error": "无效的项目目录名"}, status=400)
        return web.json_response({"ok": True, "manifest": manifest})

    async def save_prompts(request):
        data = await request.json()
        manifest = projects.save_prompts(str(data.get("dir") or ""), data.get("prompts"),
                                         data.get("segments"))
        if manifest is None:
            return web.json_response(
                {"error": "项目不存在（未新建也未跑过，无 manifest 可写）"}, status=404)
        return web.json_response({"ok": True, "manifest": manifest})

    async def delete_project(request):
        data = await request.json()
        name = projects.safe_name(data.get("dir"))
        if not name:
            return web.json_response({"error": "无效的项目目录名"}, status=400)
        try:
            deleted = projects.delete_project(name)
        except OSError as e:
            return web.json_response(
                {"error": f"删除失败（文件可能被播放器/编码器占用）：{e}"}, status=500)
        if deleted is None:
            return web.json_response({"error": "项目不存在"}, status=404)
        return web.json_response({"ok": True, "deleted": deleted})

    async def delete_file(request):
        data = await request.json()
        rel = str(data.get("path") or "").strip().replace("\\", "/")
        parts = rel.split("/")
        if (len(parts) != 3 or parts[0] != "h3_projects"
                or not projects.safe_name(parts[1])
                or not projects.safe_name(parts[2])
                or not os.path.splitext(parts[2])[1]):
            return web.json_response(
                {"error": "路径必须是 h3_projects/<项目>/<文件名>"}, status=400)
        root = os.path.realpath(get_output_directory())
        target = os.path.realpath(os.path.join(root, *parts))
        if not target.startswith(root + os.sep) or not os.path.isfile(target):
            return web.json_response({"error": "文件不存在"}, status=404)
        os.remove(target)
        return web.json_response({"ok": True})

    async def merge(request):
        data = await request.json()
        items = data.get("items")
        try:
            # PyAV 流式编码是 CPU 密集长任务（分钟级），放线程池避免
            # 阻塞 aiohttp 事件循环拖死整个 ComfyUI 界面
            manifest = await asyncio.get_event_loop().run_in_executor(
                None, projects.merge_project, str(data.get("dir") or ""), items)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=500)
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

    async def experiment_defs(request):
        """实验定义与参数元数据（前端实验面板动态渲染唯一数据源；含后端硬开关状态）。"""
        try:
            from . import experiments
            payload = experiments.experiment_defs_payload()
        except Exception:
            payload = {"ok": False, "force_disabled": True, "experiments": []}
        return web.json_response(payload)

    async def upscale_reset(request):
        data = await request.json()
        try:
            manifest = projects.upscale_reset(str(data.get("dir") or ""), data.get("seg"))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, "manifest": manifest})

    async def redo_cancel(request):
        """撤销重摇标记：从 manifest 重摇队列移除该槽位（幂等）。"""
        data = await request.json()
        try:
            manifest = projects.redo_cancel(str(data.get("dir") or ""), data.get("slot"))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, "manifest": manifest})

    handlers = [
        ("GET", "/h3chain/ping", ping),
        ("GET", "/h3chain/projects", list_projects),
        ("GET", "/h3chain/project", project_detail),
        ("GET", "/h3chain/upscale_models", upscale_models),
        ("GET", "/h3chain/experiments", experiment_defs),
        ("POST", "/h3chain/create_project", create_project),
        ("POST", "/h3chain/save_prompts", save_prompts),
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
