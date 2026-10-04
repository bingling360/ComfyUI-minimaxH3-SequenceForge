"""H3 运行报告 —— **免连线**的报告预览节点（2026-10-04）。

## 为什么单独做一个节点

导演台的「报告」是一个**连线输出**，而 ComfyUI 的连线是**按位次**存的
（workflow JSON 的 links 里存 `origin_slot`）。只要输出端口的顺序一变，
老存档里那根线就会静默落到别的槽上 —— 画布上看着还连着、下游实际收不到。
历史上「帧率」被插到「报告」前面就是一次：rev3 之前存的档写的是
`origin_slot=2`，而新 schema 的 2 号位是隐藏的「帧率」，于是每次重启
ComfyUI 都是「导演台和 PreviewAny（运行报告）断连」。

（schema 的输出顺序已在 2026-10-04 一并修正：停机场槽「帧率」挪到末位、
「报告」钉回 2 号位，见 nodes.py 的 outputs 注释。）

这个节点是**兜底方案**：它**一个输入端口都没有**，所以在结构上就不存在
「断连」这个故障模式。报告文本由前端直接从插件自己的接口取
（`GET /h3chain/projects` → `state.report`），而这份报告本来就是后端每跑一段
就落盘的（`checkpoint.save_state` 写 `output/h3_projects/h3chain_state.json`
的 `"report"` 字段），所以这里不需要任何接线。

## 执行语义

- **不是** OUTPUT_NODE，也**没有输入** → 默认不进执行图、不参与校验、纯展示；
- 仍然留一个「报告」输出：万一你想把它当普通节点接进图里，`execute` 会给出
  同一份文本（`IS_CHANGED` 恒为 nan，接进图里每跑一次都会重读最新报告）。
"""

try:
    from comfy_api.latest import io

    def _latest_report():
        """最新一份运行报告（链状态指针里的 "report"）。取不到就返回空串。"""
        try:
            from . import checkpoint
        except ImportError:                     # 非包导入（单测直接加载本模块）
            import checkpoint
        state = checkpoint.load_state() or {}
        return str(state.get("report") or "")

    class H3RunReport(io.ComfyNode):
        @classmethod
        def define_schema(cls):
            return io.Schema(
                node_id="H3RunReport",
                display_name="H3 运行报告 (免连线)",
                category="MiniMaxH3",
                description=("导演台上一次运行的报告，**无需连线**：文本由前端直接从插件接口读"
                             "（报告本来就已经落盘在 output/h3_projects/h3chain_state.json），"
                             "所以不存在「重启后连到 PreviewAny 的线断掉」这个问题。"
                             "节点上没有输入端口，也没有任何参数。"),
                inputs=[],
                outputs=[
                    io.String.Output("报告", tooltip="最新一份运行报告全文（接进图里当普通节点用时才有值）"),
                ],
            )

        @classmethod
        def IS_CHANGED(cls, **kwargs):
            # 报告每次运行都会变，且不在任何输入里 —— 接进图里必须强制重读
            return float("nan")

        @classmethod
        def execute(cls):
            return io.NodeOutput(_latest_report())

    NODES = [H3RunReport]
except Exception:                                # pragma: no cover - 老 ComfyUI 无 comfy_api
    NODES = []
