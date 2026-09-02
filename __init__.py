import traceback

WEB_DIRECTORY = "./web"  # 导演台前端（h3_director.js）随插件分发

try:
    from comfy_api.latest import ComfyExtension

    from .nodes import H3SeamlessChainSampler
    from .seam_doctor import H3SeamDoctor

    class H3SeamlessChainExtension(ComfyExtension):
        async def get_node_list(self):
            return [H3SeamlessChainSampler, H3SeamDoctor]

        async def add_routes(self, routes):
            # 新版 ComfyUI / Comfy Desktop 官方路径：框架直接传入 routes 对象
            try:
                from .routes import add_routes as _add_routes
                _add_routes(routes)
                print("[ComfyUI_H3_SeamlessChain] 路由已注册（扩展钩子，含 /api 前缀副本）："
                      "GET /h3chain/ping, /h3chain/projects, /h3chain/project, "
                      "/h3chain/upscale_models, /h3chain/experiments, "
                      "POST /h3chain/create_project, /h3chain/save_prompts, "
                      "/h3chain/delete_project, /h3chain/delete_file, /h3chain/merge, "
                      "/h3chain/upscale_reset, /h3chain/redo_cancel")
            except Exception:
                print("[ComfyUI_H3_SeamlessChain] 扩展钩子路由注册失败，回退到 PromptServer。详细错误：")
                traceback.print_exc()
                try:
                    from .routes import register
                    register()
                except Exception:
                    print("[ComfyUI_H3_SeamlessChain] 回退路由注册也失败。详细错误：")
                    traceback.print_exc()

    async def comfy_entrypoint() -> H3SeamlessChainExtension:
        return H3SeamlessChainExtension()

    # 路由注册主路径：PromptServer.instance.routes（custom nodes 加载时 instance 已就绪）。
    # ComfyExtension 基类在现行 ComfyUI 没有 add_routes 钩子，靠扩展钩子注册的旧写法
    # 导致路由从未挂上（前端删除请求 405）。
    try:
        from .routes import register as _register_routes
        _register_routes()
    except Exception:
        print("[ComfyUI_H3_SeamlessChain] 路由注册入口调用失败。详细错误：")
        traceback.print_exc()
except Exception:
    print("[ComfyUI_H3_SeamlessChain] 加载失败：本插件需要 ComfyUI v0.30.0 及以上（含官方 MiniMax H3 节点与 comfy_api.latest）。")
    print("[ComfyUI_H3_SeamlessChain] 请升级 ComfyUI 后重启。详细错误：")
    traceback.print_exc()
