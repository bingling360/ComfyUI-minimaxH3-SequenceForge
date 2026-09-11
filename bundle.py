"""H3 资产包（AssetBundle，P4）：冻结工作流的单线资产入口。

画布上只连一次：H3AssetBundle.资产包 -> H3SeamlessChainSampler.资产包。
之后增删素材全在导演台三库面板点选，工作流图永不变（RefPack 式“连一次、
终身不动”；旧 autogrow 拉线与 H3AssetHub JSON 串口保留兼容）。

执行期从服务端组装：项目 manifest asset_links（全局库回填 file）优先，
legacy manifest.assets 补位；输出规范包 [{label,kind,file,asset_id?,roles?}]，
主节点 pool 照单加载（三级寻址 + compile_refs 段卡点，P2 已打通）。
总量不限，单段 9/3/3 执行期按段卡；缺文件在排队前早爆点名。

项目名空 = 空包（走导演台状态/画布旧路径，旧链行为不变）。
无 torch 依赖，组装纯函数无 ComfyUI 可单测。
"""

import json
import os

_ROLE_WHITELIST = ("首帧图", "尾帧图")


def build_bundle_pack(legacy_assets=None, asset_links=None, global_assets=None):
    """组装资产包 -> (entries, warnings)。

    entries 保序：链接优先（alias 冲突时链接胜出，与 asset_store.by_alias
    首胜口径一致），legacy 按 label 去重补位。entry 形态
    {label, kind, file, asset_id?, roles?}，主节点 pool 可直接消费。
    全局 file 缺失的链接跳过并记 warning（执行期按未知引用报错点名）。
    """
    entries, warnings, seen = [], [], set()
    by_global = {}
    for e in global_assets or []:
        if isinstance(e, dict) and e.get("asset_id"):
            by_global[str(e["asset_id"])] = e

    def _kind(k):
        k = str(k or "image").strip()
        return k if k in ("image", "video", "audio") else "image"

    for x in asset_links or []:
        if not isinstance(x, dict):
            continue
        alias = str(x.get("alias") or "").strip()[:24]
        aid = str(x.get("asset_id") or "").strip()
        if not alias or not aid or alias in seen:
            continue
        g = by_global.get(aid) or {}
        f = str(g.get("file") or "").strip().replace("\\", "/")
        if not f:
            warnings.append(f"链接「{alias}」在全局库无文件（asset_id={aid}），已跳过")
            continue
        ent = {"label": alias, "kind": _kind(x.get("kind") or g.get("kind")),
               "file": f, "asset_id": aid}
        rl = x.get("roles")
        if isinstance(rl, list):
            kept = [str(r).strip() for r in rl if str(r).strip() in _ROLE_WHITELIST]
            if kept:
                ent["roles"] = kept[:2]
        entries.append(ent)
        seen.add(alias)
    for a in legacy_assets or []:
        if not isinstance(a, dict):
            continue
        label = str(a.get("label") or "").strip()[:24]
        f = str(a.get("file") or "").strip().replace("\\", "/")
        if not label or not f or label in seen:
            continue
        ent = {"label": label, "kind": _kind(a.get("kind")), "file": f}
        rl = a.get("roles")
        if isinstance(rl, list):
            kept = [str(r).strip() for r in rl if str(r).strip() in _ROLE_WHITELIST]
            if kept:
                ent["roles"] = kept[:2]
        if isinstance(a.get("asset_id"), str) and a["asset_id"].strip():
            ent["asset_id"] = a["asset_id"].strip()
        entries.append(ent)
        seen.add(label)
    return entries, warnings


try:
    from comfy_api.latest import io

    class H3AssetBundle(io.ComfyNode):
        """冻结资产入口：项目名进，规范包出（连一次主节点，终身不动）。"""

        @classmethod
        def define_schema(cls):
            return io.Schema(
                node_id="H3AssetBundle",
                display_name="H3 Asset Bundle (资产包)",
                category="MiniMaxH3",
                description="冻结工作流资产入口：从服务端组装本项目资产包"
                            "（全局库链接优先 + 本地资产补位），输出连主节点「资产包」。"
                            "之后增删素材全在导演台三库面板，画布永不动。项目名空=空包"
                            "（走导演台状态/画布旧路径）。缺文件排队前早爆点名。",
                inputs=[
                    io.String.Input("项目名", default="",
                                    tooltip="output/h3_projects/<项目名>；空=空包走旧路径"),
                ],
                outputs=[
                    io.String.Output("资产包", tooltip="规范包 JSON，连主节点「资产包」"),
                    io.String.Output("报告", tooltip="组装结果人读报告"),
                ],
            )

        @classmethod
        def execute(cls, 项目名=""):
            proj = str(项目名 or "").strip()
            if not proj:
                return ("[]", "资产包：未指定项目，空包（走导演台状态/画布旧路径）")
            try:
                from . import checkpoint as _ckpt
            except ImportError:
                import checkpoint as _ckpt
            try:
                from . import projects as _proj
            except ImportError:
                import projects as _proj
            if _proj.safe_name(proj) != proj:
                raise ValueError(f"非法项目名：{proj!r}")
            root = os.path.join(_ckpt.projects_root(), proj)
            manifest = _ckpt.load_manifest(root)
            if manifest is None:
                raise ValueError(f"项目不存在：{proj}（先新建或跑一段）")
            try:
                from . import asset_store as _as
            except ImportError:
                import asset_store as _as
            try:
                _libroot = _as.try_library_root()
                _globals = _as.load_library(_libroot).get("assets") or [] if _libroot else []
            except Exception:
                _globals = []
            entries, warns = build_bundle_pack(
                manifest.get("assets"), manifest.get("asset_links"), _globals)
            # 缺文件早爆（与 H3AssetHub 同口径，不等主节点跑一半才炸）
            try:
                from .asset_hub import check_files as _check
            except ImportError:
                from asset_hub import check_files as _check
            errors = _check([{"label": e["label"], "file": e["file"]} for e in entries], root)
            if errors:
                raise ValueError(errors[0]["message"])
            counts = {k: sum(1 for e in entries if e["kind"] == k)
                      for k in ("image", "video", "audio")}
            report = (f"资产包[{proj}]：共 {len(entries)}（图{counts['image']}/"
                      f"视{counts['video']}/音{counts['audio']}），总量不限、"
                      f"单段 9/3/3（执行期按段卡）。")
            if warns:
                report += f"警告 {len(warns)}（首条：{warns[0]}）"
            else:
                report += "组装通过。"
            return (json.dumps(entries, ensure_ascii=False), report)

except ImportError:
    pass
