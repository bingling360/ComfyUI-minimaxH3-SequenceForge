"""ComfyUI 启动命令速查 —— 结构化数据 + 纯文本导出（**单一真源**）。

为什么要有这个模块
------------------
面板里的「📖 启动命令」弹窗、以及 `docs/ComfyUI启动命令速查.txt`，
都从**这一份** `SECTIONS` 生成 —— 改一处两处都变，不会一边新一边旧。
（本项目对「同一件事两处维护」的态度见 `perf.py` 里那批 ⛔ 注释。）

Linux 那几节写得特别细（systemd / 防火墙 / 反向代理 / Docker / 云平台端口），
因为云平台上「起不来」九成是这几件事，不是 ComfyUI 本身。

⚠ 纪律：`kind == "comfyui"` 的分区里出现的**每个 `--flag`** 都必须是真实存在的
ComfyUI 参数。`tests/test_launch_ref.py` 会解析 `comfy/cli_args.py` 逐个核对 ——
编造参数会直接测试红。非 ComfyUI 的命令（systemd / docker / nginx / ufw 那些）
统一放 `kind == "external"`，不参与那条核对。
"""
from __future__ import annotations

import re

# ============================ 数据 ============================
#
# 条目字段：
#   cmd   —— 命令原文（**逐字可复制**，不带提示符；提示符由渲染层加）
#   title —— 一句话标题
#   why   —— 为什么要这样 / 会发生什么（这是给人看的重点）
#   note  —— 可选，补充注意事项
#
# 分区字段：
#   id / title / note / kind("comfyui" | "external") / entries

SECTIONS = [
    # ---------------------------------------------------------------- 基础
    {
        "id": "basic",
        "title": "① 基础启动",
        "kind": "comfyui",
        "note": "先确认这几条能跑通，再谈性能。所有命令都在 ComfyUI 根目录执行。",
        "entries": [
            {
                "cmd": "python main.py",
                "title": "最小启动",
                "why": "只监听 127.0.0.1:8188，只有本机能访问。开发机上最常用。",
                "note": "用虚拟环境时把 `python` 换成 `venv/bin/python`（Linux）或 "
                        "`venv\\Scripts\\python.exe`（Windows）。",
            },
            {
                "cmd": "python main.py --port 8189",
                "title": "换端口",
                "why": "8188 被占了就换一个。**端口只是「监听哪个口」**，"
                       "和能不能被外部访问无关 —— 那取决于 --listen。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --port 8188",
                "title": "★ 允许外部访问（远程/云平台必加）",
                "why": "默认只听 127.0.0.1，**局域网和公网都连不上**。"
                       "云平台、Docker、反向代理后面一律要加 --listen 0.0.0.0。",
                "note": "⚠ 等于把这个口暴露出去。公网一定要配合云安全组限制来源 IP，"
                        "或加 --tls-keyfile/--tls-certfile 上 HTTPS。",
            },
            {
                "cmd": "python main.py --listen",
                "title": "listen 不带参数",
                "why": "--listen 后面不写值，等价于 `0.0.0.0,::` —— 同时听 IPv4 和 IPv6 全部网卡。",
            },
            {
                "cmd": "python main.py --listen 127.0.0.1,192.168.1.10 --port 8188",
                "title": "只听指定的几个地址",
                "why": "逗号分隔多地址。想「本机 + 内网某张网卡」但不想全开时用。",
            },
            {
                "cmd": "python main.py --auto-launch",
                "title": "启动后自动开浏览器",
                "why": "桌面环境方便；服务器上别加（没有浏览器，还会拖慢启动）。",
            },
            {
                "cmd": "python main.py --disable-auto-launch",
                "title": "明确禁止自动开浏览器",
                "why": "某些打包版（Windows 独立版）默认会开，服务器上要显式关掉。",
            },
        ],
    },

    # ---------------------------------------------------------------- 网络
    {
        "id": "network",
        "title": "② 网络与远程访问",
        "kind": "comfyui",
        "note": "连不上、白屏、上传失败，基本都在这一节。",
        "entries": [
            {
                "cmd": "python main.py --listen 0.0.0.0 --port 8188",
                "title": "最常用的远程组合",
                "why": "听全部网卡 + 指定端口。局域网内用 `http://<机器IP>:8188` 访问。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --enable-cors-header '*'",
                "title": "允许跨域（前端被拦时）",
                "why": "把 ComfyUI 嵌到别的网页 / 用外部工具调 API 时报 CORS 错误时加。",
                "note": "`*` 是允许所有来源，生产环境建议写具体来源（如 https://a.com）。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --tls-keyfile key.pem --tls-certfile cert.pem",
                "title": "上 HTTPS",
                "why": "两个参数**必须同时给**，只给一个不生效。之后用 `https://` 访问。",
                "note": "证书可以是自签的（浏览器会警告），也可以走反向代理（见 ⑭ nginx）。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --max-upload-size 500",
                "title": "放大上传上限（单位 MB）",
                "why": "默认 100MB。传大视频/大图报「上传失败」时调大。",
                "note": "⚠ 走 nginx 时还要同步改 `client_max_body_size`，否则会被代理层先掐掉。",
            },
            {
                "cmd": "python main.py --multi-user",
                "title": "多用户独立存储",
                "why": "每个用户各自的输入输出目录，多人共用一台机器时用。",
            },
            {
                "cmd": "python main.py --disable-api-nodes",
                "title": "断开前端的外网访问",
                "why": "禁止加载 api 节点，同时阻止前端联网。内网/离线部署时用。",
            },
        ],
    },

    # ---------------------------------------------------------------- 目录
    {
        "id": "paths",
        "title": "③ 目录与模型路径",
        "kind": "comfyui",
        "note": "把模型放系统盘之外（云平台数据盘 / 挂载盘）时全靠这一节。",
        "entries": [
            {
                "cmd": "python main.py --base-directory /data/comfyui",
                "title": "★ 一键改根目录",
                "why": "models / custom_nodes / input / output / temp / user **全部**"
                       "以它为基准。云平台上把整份数据挂到大盘上，只加这一个参数。",
            },
            {
                "cmd": "python main.py --output-directory /data/output",
                "title": "只改输出目录",
                "why": "成片落盘到别处。优先级高于 --base-directory。",
                "note": "⚠ 磁盘写满会让采样「跑到一半失败」且报错很不直观，"
                        "云平台上优先把 output 挂到大盘。",
            },
            {
                "cmd": "python main.py --input-directory /data/input",
                "title": "只改输入目录",
                "why": "上传的素材落这里。优先级高于 --base-directory。",
            },
            {
                "cmd": "python main.py --temp-directory /data/temp",
                "title": "只改临时目录",
                "why": "中间产物落这里。临时目录和输出目录**别放同一个盘**，"
                       "否则大任务下两边一起涨、一起写满。",
            },
            {
                "cmd": "python main.py --extra-model-paths-config /data/extra_model_paths.yaml",
                "title": "★ 追加模型搜索路径",
                "why": "把已有的一堆模型目录接进来，**不用搬文件**。"
                       "可重复给多个 yaml（`--extra-model-paths-config a.yaml b.yaml`）。",
                "note": "本机就是用它把 D:\\Comfy-Desktop\\ComfyUI-Shared\\models 接进来的。",
            },
            {
                "cmd": "python main.py --models-directory /data/models",
                "title": "只改 models 目录",
                "why": "等价于把 models 从 base-directory 下挪走。",
            },
            {
                "cmd": "python main.py --user-directory /data/user",
                "title": "只改 user 目录",
                "why": "设置、工作流缓存、本插件的 perf.json 都在这下面。"
                       "容器里想让设置持久化，挂它。",
            },
        ],
    },

    # ---------------------------------------------------------------- 显存
    {
        "id": "vram",
        "title": "④ 显存与性能（本插件最相关）",
        "kind": "comfyui",
        "note": "小显存卡上二采 OOM、显存吃到 100%，处方都在这一节。"
                "面板「⚡ 性能优化」里的「显存预留」是**运行时**版，不用重启；"
                "这一节是**启动参数**版，重启后生效。",
        "entries": [
            {
                "cmd": "python main.py --vram-headroom 1",
                "title": "★ 多留 1GB 显存（二采 OOM 首选）",
                "why": "让 aimdo 在总显存里硬留出 1GB 完全不碰。0.35 起 ComfyUI 的定位是"
                       "「把显存吃满」，实测精化峰值能到 100.0% —— 于是最后一步高清解码"
                       "连 112MB 都拿不到，regular / tiled 两条路一起死。",
                "note": "插件实测：显存 100% → ~77%，代价约 5% 耗时。"
                        "⚠ 这是**每设备**预留，只在启动时定死、运行时代码里没有 setter → "
                        "面板改不了，必须重启。面板的「显存预留」写的是另一项"
                        "（进程级 simple budget），两者可叠加。",
            },
            {
                "cmd": "python main.py --vram-headroom 2",
                "title": "留 2GB（更保险）",
                "why": "1GB 还不够时加码。6GB 卡上不建议超过 2 —— 留太多权重只剩一点点地方，"
                       "采样会退化成每步全量重读，比 OOM 还慢。",
            },
            {
                "cmd": "python main.py --disable-comfy-compiler",
                "title": "★ 关掉 Comfy 编译器（1GB 还不够时）",
                "why": "0.35 新增的编译器，官方定位就是拉高 H3 显存利用率。"
                       "实测显存 100% → ~77%，代价约 5% 耗时。",
                "note": "只在余量真紧张时加（<12GB 卡）。24GB 卡上有余量，加它只是白付 5%。",
            },
            {
                "cmd": "python main.py --reserve-vram 1",
                "title": "老开关：给系统留 1GB",
                "why": "同时喂 Python 侧 EXTRA_RESERVED_VRAM 与 aimdo 的 simple_vram_headroom。"
                       "后者**运行时可写**，所以它的效果能在面板里调 —— 不必重启。",
            },
            {
                "cmd": "python main.py --fast-disk",
                "title": "优先用磁盘而不是内存做换页",
                "why": "模型远大于显存、又装了快 NVMe 时更快：把权重页缓存放盘上，"
                       "省下宝贵的主机内存给别的东西。",
                "note": "⚠ 本机那种「内存只剩 2GB + swap 已用 10/20GB」的情形，"
                        "加它反而更糟（磁盘 IO 才是瓶颈）。",
            },
            {
                "cmd": "python main.py --disable-dynamic-vram",
                "title": "关掉 DynamicVRAM（退回估算式加载）",
                "why": "DynamicVRAM 出诡异行为时的兜底。退回后走 ComfyUI 自己的"
                       "按模块流动（粒度≈block）。",
                "note": "⚠ 通常更慢。只在确认是 DynamicVRAM 引起的问题时才用。",
            },
            {
                "cmd": "python main.py --highvram",
                "title": "模型用完不卸载（大显存卡）",
                "why": "默认模型用完会被挪到 CPU 内存；这个参数让它们常驻显存，"
                       "连续跑多个任务时省掉反复加载。",
                "note": "⚠ 显存装不下就别加 —— 会直接 OOM。",
            },
            {
                "cmd": "python main.py --lowvram",
                "title": "低显存模式",
                "why": "开了 DynamicVRAM 时**本参数不做事**；没开时让文本编码器跑在 CPU 上。",
            },
            {
                "cmd": "python main.py --novram",
                "title": "低显存还不够时的下一档",
                "why": "--lowvram 仍然 OOM 时用。很慢。",
            },
            {
                "cmd": "python main.py --cpu",
                "title": "全部走 CPU（极慢）",
                "why": "没有可用 GPU / 驱动坏了时的最后手段。H3 这种规模基本不具备可用性，"
                       "只用来验证「到底是 GPU 的问题还是别的问题」。",
            },
            {
                "cmd": "python main.py --gpu-only",
                "title": "全部放显存（含文本编码器）",
                "why": "把 TE 也常驻显存。大显存卡上能省掉反复换页。",
                "note": "⚠ 还有一个副作用：`intermediate_device()` 会返回 cuda 而不是 CPU。"
                        "本插件二采里有一处「intermediate_device 返回 CPU 但空闲显存充足"
                        "——放大模型强制上 cuda」的自愈日志，就是给这种情形兜底的。",
            },
            {
                "cmd": "python main.py --disable-smart-memory",
                "title": "更激进地把模型挪回内存",
                "why": "ComfyUI 的智能内存管理在某些卡上会让显存碎片化，"
                       "关掉后行为更可预测。",
            },
            {
                "cmd": "python main.py --cache-none",
                "title": "不缓存节点结果（最省内存）",
                "why": "每次运行都重算所有节点。省内存/显存，代价是慢。",
            },
            {
                "cmd": "python main.py --cache-lru 10",
                "title": "只缓存最近 10 个节点结果",
                "why": "比 --cache-none 温和，比默认省。",
            },
            {
                "cmd": "python main.py --async-offload 2",
                "title": "异步权重卸载（默认开，2 条流）",
                "why": "搬运权重与计算重叠，NVIDIA 卡上默认就开着。"
                       "数字是流的条数。",
            },
            {
                "cmd": "python main.py --disable-pinned-memory",
                "title": "关掉 pinned memory",
                "why": "pinned 内存能加速 CPU↔GPU 拷贝，但会**锁住**一部分主机内存。"
                       "内存本来就紧张时关掉它换回可换页内存。",
            },
            {
                "cmd": "python main.py --disable-cuda-graphs",
                "title": "关掉 CUDA graphs",
                "why": "CUDA graphs 能省 kernel 启动开销，但显存占用会更高。"
                       "显存紧时关掉它腾地方。",
            },
        ],
    },

    # ---------------------------------------------------------------- 精度
    {
        "id": "precision",
        "title": "⑤ 精度与量化",
        "kind": "comfyui",
        "note": "「显存不够」和「出来是黑图/花屏」两个方向都会用到。",
        "entries": [
            {
                "cmd": "python main.py --fp16-unet",
                "title": "扩散模型用 fp16",
                "why": "比 bf16/fp32 省显存。老卡（没有 bf16 加速）上还能更快。",
            },
            {
                "cmd": "python main.py --bf16-unet",
                "title": "扩散模型用 bf16",
                "why": "数值范围比 fp16 大，不容易溢出。30/40 系默认就是它。",
            },
            {
                "cmd": "python main.py --fp8_e4m3fn-unet",
                "title": "扩散模型用 fp8（最省显存）",
                "why": "显存再省一档。**需要 sm89+（40 系及以上）**，老卡会退化成模拟实现、很慢。",
                "note": "本机 1660 SUPER 是 sm75，别开。",
            },
            {
                "cmd": "python main.py --fp16-vae",
                "title": "VAE 用 fp16",
                "why": "VAE 解码在 fp32 下很慢（sm75 上没有 fp32 tensor core / TF32）。"
                       "fp16 能明显提速，但**少数 VAE 会出黑图**。",
                "note": "本机 VAE 日志是 `dtype: torch.float32` —— 解码 386s 的一大原因。"
                        "值得试 fp16，出黑图再退回去。",
            },
            {
                "cmd": "python main.py --cpu-vae",
                "title": "VAE 放 CPU 跑",
                "why": "解码阶段显存最紧张时，把 VAE 挪到 CPU 换出显存。很慢。",
            },
            {
                "cmd": "python main.py --fp32-vae",
                "title": "VAE 强制 fp32",
                "why": "fp16 出黑图时的解法（回到慢但稳）。",
            },
            {
                "cmd": "python main.py --force-fp16",
                "title": "全局强制 fp16",
                "why": "老卡（如 1660/2080）上整体提速。",
            },
            {
                "cmd": "python main.py --force-fp32",
                "title": "全局强制 fp32",
                "why": "fp16 出各种诡异数值问题时的排查手段。非常慢。",
            },
            {
                "cmd": "python main.py --fp16-text-enc",
                "title": "文本编码器用 fp16",
                "why": "TE 很大（本机那个 48GB）时省显存。",
            },
            {
                "cmd": "python main.py --fp8_e4m3fn-text-enc",
                "title": "文本编码器用 fp8",
                "why": "TE 显存再省一档。同样需要 sm89+。",
            },
        ],
    },

    # ---------------------------------------------------------------- 注意力
    {
        "id": "attention",
        "title": "⑥ 注意力后端",
        "kind": "comfyui",
        "note": "这一组是**互斥**的，同时给多个只有第一个生效（ComfyUI 用 mutually_exclusive_group）。",
        "entries": [
            {
                "cmd": "python main.py --use-ck-attention",
                "title": "★ 用 comfy-kitchen 的 int8 注意力",
                "why": "本插件「注意力头分块」的收益**完全取决于后端内核**："
                       "默认 sdpa 下几乎只有「多 n 次 kernel 调用」的开销，"
                       "int8 kitchen 内核下才实测省 25.7%。",
                "note": "⚠ 面板的「Attention 后端」只在**已加载**的后端之间切，切不到 kitchen "
                        "→ 要拿这份收益只能加启动参数并重启。"
                        "需要环境里装了 comfy_kitchen（本机 0.2.35 已装）。",
            },
            {
                "cmd": "python main.py --use-sage-attention",
                "title": "SageAttention",
                "why": "量化注意力，显存和速度都有收益。30 系上有失败报告，需实测。",
            },
            {
                "cmd": "python main.py --use-flash-attention",
                "title": "FlashAttention",
                "why": "经典高效注意力。需要装 flash-attn 且算力匹配。",
            },
            {
                "cmd": "python main.py --use-pytorch-cross-attention",
                "title": "PyTorch 2.0 的 SDPA",
                "why": "不需要额外依赖，通用性好。默认就常走这条。",
            },
            {
                "cmd": "python main.py --use-split-cross-attention",
                "title": "切分式注意力（省显存）",
                "why": "显存很紧时的老方案，速度慢。装了 xformers 时会被忽略。",
            },
            {
                "cmd": "python main.py --disable-xformers",
                "title": "禁用 xformers",
                "why": "xformers 版本不匹配导致崩溃时的排查手段。",
            },
            {
                "cmd": "python main.py --force-upcast-attention",
                "title": "强制注意力升精度",
                "why": "**出黑图时先试这个** —— 官方 help 原文就是「please report if it fixes black images」。",
            },
            {
                "cmd": "python main.py --dont-upcast-attention",
                "title": "禁止注意力升精度",
                "why": "换一点速度，代价是可能出黑图。只在明确知道自己在干什么时用。",
            },
        ],
    },

    # ---------------------------------------------------------------- 多卡
    {
        "id": "devices",
        "title": "⑦ 多卡与设备选择",
        "kind": "comfyui",
        "note": "多张卡时把实例钉在某一张上，避免和别的进程抢。",
        "entries": [
            {
                "cmd": "python main.py --cuda-device 0",
                "title": "只用第 0 张卡",
                "why": "其余卡对本进程**不可见**（等价 CUDA_VISIBLE_DEVICES=0，但更彻底）。"
                       "多实例各占一张卡时必用。",
            },
            {
                "cmd": "python main.py --cuda-device 0,1",
                "title": "只用第 0、1 张卡",
                "why": "逗号分隔。",
            },
            {
                "cmd": "python main.py --default-device 1",
                "title": "默认用第 1 张卡，其余仍可见",
                "why": "和 --cuda-device 的区别：其他卡还在，只是默认不往上放。",
            },
            {
                "cmd": "CUDA_VISIBLE_DEVICES=0 python main.py",
                "title": "环境变量法（更通用）",
                "why": "不改命令行的写法。容器/云平台上常由平台预设。",
                "note": "本机日志里 `Enabled pinned memory 6488.0` 这类行就是启动后自报的环境状态。",
            },
        ],
    },

    # ---------------------------------------------------------------- 排障
    {
        "id": "debug",
        "title": "⑧ 排障、日志与安全隔离",
        "kind": "comfyui",
        "note": "「跑不起来 / 不知道卡在哪」时从这一节挑。",
        "entries": [
            {
                "cmd": "python main.py --verbose DEBUG",
                "title": "★ 打开调试日志",
                "why": "看真正的报错。也可以 `--verbose DEBUG /var/log/comfy.log` "
                       "把该级别写进文件（可重复给多个）。",
                "note": "日志里 `** Log path:` 那行会告诉你默认日志落在哪。",
            },
            {
                "cmd": "python main.py --log-stdout",
                "title": "日志走 stdout 而不是 stderr",
                "why": "容器/云平台只收 stdout 时，不加这个会**什么都看不到**。",
            },
            {
                "cmd": "python main.py --dont-print-server",
                "title": "不打印服务端输出",
                "why": "日志刷屏时的静音开关。",
            },
            {
                "cmd": "python main.py --debug-hang",
                "title": "Ctrl-C 时打印栈（查卡死）",
                "why": "「跑到一半不动了」时按 Ctrl-C，能看到卡在哪一行。",
            },
            {
                "cmd": "python main.py --disable-metadata",
                "title": "不把提示词写进产物",
                "why": "产物文件里不再带 prompt 元数据。**对外交付/公开素材时建议加**。",
            },
            {
                "cmd": "python main.py --disable-all-custom-nodes",
                "title": "★ 全部禁用第三方节点",
                "why": "排查「是不是某个插件搞坏的」的第一招。"
                       "启动干净了再逐个放回来。",
            },
            {
                "cmd": "python main.py --disable-all-custom-nodes --whitelist-custom-nodes ComfyUI-minimaxH3-SequenceForge",
                "title": "只放行指定插件",
                "why": "最小可用集。排障时把嫌疑插件单独放行来二分。",
            },
            {
                "cmd": "python main.py --disable-api-nodes",
                "title": "禁掉 api 节点 + 前端联网",
                "why": "内网隔离部署。",
            },
            {
                "cmd": "python main.py --deterministic",
                "title": "尽量用确定性算法",
                "why": "排查「同样的种子为什么结果不同」。会变慢。",
            },
            {
                "cmd": "python main.py --quick-test-for-ci",
                "title": "CI 快速自检",
                "why": "启动完就退出，用来验证环境装好没有。",
            },
        ],
    },

    # ---------------------------------------------------------------- Manager
    {
        "id": "manager",
        "title": "⑨ Manager 与插件管理",
        "kind": "comfyui",
        "note": "面板截图里的 `--enable-manager` 就在这一节。",
        "entries": [
            {
                "cmd": "python main.py --enable-manager",
                "title": "启用 ComfyUI-Manager",
                "why": "开插件管理面板（安装/更新节点）。新版本默认不带，要显式开。",
            },
            {
                "cmd": "python main.py --disable-manager-ui",
                "title": "只关 Manager 界面",
                "why": "后台任务（计划安装等）照旧跑，只是界面没了。",
            },
            {
                "cmd": "python main.py --enable-manager-legacy-ui",
                "title": "用 Manager 的旧版界面",
                "why": "新界面不习惯时。隐含 --enable-manager。",
            },
        ],
    },

    # ---------------------------------------------------------------- 其它
    {
        "id": "misc",
        "title": "⑩ 其它实用开关",
        "kind": "comfyui",
        "note": "",
        "entries": [
            {
                "cmd": "python main.py --preview-method auto",
                "title": "采样过程显示预览图",
                "why": "长任务时能看到「现在长什么样」，不必等跑完。",
                "note": "默认是 NoPreviews（不预览）。预览会占一点显存和时间。",
            },
            {
                "cmd": "python main.py --preview-size 512",
                "title": "预览图最大边长",
                "why": "默认 512。调小更省。",
            },
            {
                "cmd": "python main.py --force-non-blocking",
                "title": "强制非阻塞拷贝",
                "why": "某些非 N 卡平台上有提速，但**可能让部分工作流出问题**。",
            },
            {
                "cmd": "python main.py --disable-cuda-malloc",
                "title": "关掉 cudaMallocAsync",
                "why": "默认开启（torch 2.0+）。显存碎片异常时关掉试试。",
            },
            {
                "cmd": "python main.py --default-hashing-function md5",
                "title": "换去重用的哈希算法",
                "why": "默认 sha256。大目录扫描慢时可以换 md5/sha1 提速。",
            },
            {
                "cmd": "python main.py --mmap-torch-files",
                "title": "ckpt/pt 用 mmap 加载",
                "why": "加载大文件时省内存峰值。",
            },
            {
                "cmd": "python main.py --disable-mmap",
                "title": "safetensors 不用 mmap",
                "why": "网络盘/NFS 上 mmap 有问题时关掉。",
            },
        ],
    },

    # ================================================================ LINUX
    {
        "id": "linux-bg",
        "title": "⑪ Linux：后台运行",
        "kind": "external",
        "note": "SSH 一断开进程就被杀，是云平台上最常见的「跑一半没了」。"
                "三种活法：nohup / screen(tmux) / systemd（见下一节，生产推荐）。",
        "entries": [
            {
                "cmd": "nohup python main.py --listen 0.0.0.0 --port 8188 > comfy.log 2>&1 &",
                "title": "nohup 最简单",
                "why": "断开 SSH 后继续跑，输出进 comfy.log。",
                "note": "⚠ `--listen 0.0.0.0` 不能省，否则外网连不上。"
                        "⚠ 结尾的 `&` 不能省，否则还是前台。",
            },
            {
                "cmd": "tail -f comfy.log",
                "title": "看实时日志",
                "why": "nohup 之后日志只在文件里，用它跟。",
            },
            {
                "cmd": "ps -ef | grep main.py",
                "title": "确认还活着",
                "why": "找不到就说明已经被杀了。",
            },
            {
                "cmd": "kill $(pgrep -f 'main.py')",
                "title": "停掉它",
                "why": "按命令行匹配进程号再杀，比手抄 PID 稳。",
                "note": "⚠ 别用 `kill -9`，ComfyUI 来不及落盘（本插件有分段 mp4 落盘，"
                        "硬杀可能留下半个文件）。先 `kill`，等几秒，再考虑 -9。",
            },
            {
                "cmd": "screen -dmS comfy bash -c 'cd /opt/ComfyUI && python main.py --listen 0.0.0.0 --port 8188 2>&1 | tee comfy.log'",
                "title": "screen 起一个可回看的会话",
                "why": "断开后还能 `screen -r comfy` 回到现场看输出。",
            },
            {
                "cmd": "tmux new -d -s comfy 'cd /opt/ComfyUI && python main.py --listen 0.0.0.0'",
                "title": "tmux 同上（更现代）",
                "why": "`tmux attach -t comfy` 回看，`Ctrl-b d` 脱离。",
            },
            {
                "cmd": "setsid python main.py --listen 0.0.0.0 > comfy.log 2>&1 < /dev/null &",
                "title": "彻底脱离终端（脚本里用）",
                "why": "setsid 新建会话组，比 nohup 更彻底，适合被别的脚本拉起。",
            },
        ],
    },
    {
        "id": "linux-systemd",
        "title": "⑫ ★ Linux：开机自启（systemd，生产推荐）",
        "kind": "external",
        "note": "开机自启 + 崩溃自动拉起 + 日志归集，一次配好一劳永逸。"
                "下面每一步都是独立命令，逐条复制即可。",
        "entries": [
            {
                "cmd": "sudo nano /etc/systemd/system/comfyui.service",
                "title": "第 1 步：新建 service 文件",
                "why": "把下面的内容整段贴进去，然后按路径改三处：User、WorkingDirectory、ExecStart。",
            },
            {
                "cmd": "[Unit]\nDescription=ComfyUI\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser=comfy\nGroup=comfy\nWorkingDirectory=/opt/ComfyUI\nEnvironment=\"PYTHONUNBUFFERED=1\"\nExecStart=/opt/ComfyUI/venv/bin/python -u main.py --listen 0.0.0.0 --port 8188\nRestart=always\nRestartSec=5\nOOMScoreAdjust=-500\nStandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=multi-user.target",
                "title": "第 2 步：service 文件内容（整段复制）",
                "why": "`Restart=always` + `RestartSec=5` = 崩了 5 秒后自动拉起；"
                       "`OOMScoreAdjust=-500` = 被 OOM killer 盯上的概率调低"
                       "（大模型加载吃内存，不给它这个分容易被一刀切）。",
                "note": "⚠ 三处必改：`User=`（跑 ComfyUI 的账号）、"
                        "`WorkingDirectory=`（ComfyUI 根目录）、`ExecStart=`（python 绝对路径）。"
                        "⚠ 用 venv 就写 venv 里的 python，别写系统 python。",
            },
            {
                "cmd": "sudo systemctl daemon-reload",
                "title": "第 3 步：让 systemd 读到新文件",
                "why": "改完 service 文件必须执行，否则不认。",
            },
            {
                "cmd": "sudo systemctl enable --now comfyui",
                "title": "第 4 步：开机自启 + 立刻启动",
                "why": "`enable` 写进开机自启，`--now` 顺手现在就起。两步合一。",
            },
            {
                "cmd": "systemctl status comfyui",
                "title": "看状态",
                "why": "绿点 active (running) 就是好了。起不来时这里会直接给失败原因。",
            },
            {
                "cmd": "sudo journalctl -u comfyui -f",
                "title": "★ 跟实时日志",
                "why": "systemd 把 stdout/stderr 都收进 journal，**这是唯一看日志的地方**"
                       "（不再有 comfy.log）。",
            },
            {
                "cmd": "sudo journalctl -u comfyui -n 200 --no-pager",
                "title": "看最近 200 行",
                "why": "排查刚崩的那次。",
            },
            {
                "cmd": "sudo systemctl restart comfyui",
                "title": "重启（改完启动参数后）",
                "why": "⚠ 改了 ExecStart 里的参数，要先 daemon-reload 再 restart。",
            },
            {
                "cmd": "sudo systemctl stop comfyui && sudo systemctl disable comfyui",
                "title": "停掉并取消自启",
                "why": "不想开机自动跑了。",
            },
            {
                "cmd": "sudo systemctl edit comfyui",
                "title": "不改原文件地覆盖配置",
                "why": "生成一个 override 片段（改端口/环境变量时比直接改原文件干净，"
                       "升级不会被覆盖）。",
            },
            {
                "cmd": "sudo systemctl edit comfyui --full",
                "title": "直接改完整文件",
                "why": "和 edit 的区别：--full 给你整份内容。",
            },
            {
                "cmd": "sudo loginctl enable-linger comfy",
                "title": "用户级 systemd 常驻",
                "why": "用 `systemctl --user` 配的服务，默认登录后才起；"
                       "加 linger 让它**不登录也自启**。",
            },
            {
                "cmd": "systemctl --user status comfyui",
                "title": "用户级服务状态",
                "why": "没有 root、或不想用 sudo 时走用户级（service 放 ~/.config/systemd/user/）。",
            },
        ],
    },
    {
        "id": "linux-port",
        "title": "⑬ ★ Linux：端口与防火墙",
        "kind": "external",
        "note": "「本机能打开、外面打不开」= 九成是这一节 + 云平台安全组。"
                "注意顺序：**安全组 → 系统防火墙 → 进程有没有听对地址**。",
        "entries": [
            {
                "cmd": "ss -lntp | grep 8188",
                "title": "第 1 步：确认进程听在哪个地址",
                "why": "关键看 `Local Address`：`127.0.0.1:8188` = 只有本机能连；"
                       "`0.0.0.0:8188` 或 `*:8188` = 全都能连。",
                "note": "⚠ 如果是 127.0.0.1，问题在启动参数，不是防火墙 —— 回去加 `--listen 0.0.0.0`。",
            },
            {
                "cmd": "lsof -i :8188",
                "title": "谁占着这个端口（备用）",
                "why": "没装 ss 时用。起不来报 `Address already in use` 时找占用者。",
            },
            {
                "cmd": "sudo kill -15 $(lsof -t -i:8188)",
                "title": "释放被占的端口",
                "why": "先 SIGTERM 让旧进程体面退出。",
                "note": "⚠ 确认那个进程真的是旧的 ComfyUI 再杀。",
            },
            {
                "cmd": "sudo ufw status verbose",
                "title": "第 2 步：看防火墙（Ubuntu/Debian）",
                "why": "先看状态。`inactive` 说明 ufw 没开，问题不在它。",
            },
            {
                "cmd": "sudo ufw allow 8188/tcp",
                "title": "放行端口",
                "why": "ufw 生效中时必须加，否则外面连不上。",
            },
            {
                "cmd": "sudo ufw allow from 203.0.113.7 to any port 8188 proto tcp",
                "title": "★ 只放行指定来源 IP（更安全）",
                "why": "把 ComfyUI 暴露到公网风险不小（能跑任意工作流/读写文件）。"
                       "**强烈建议**只放行你自己的出口 IP，而不是全网开放。",
            },
            {
                "cmd": "sudo ufw delete allow 8188/tcp",
                "title": "撤销放行",
                "why": "不再需要时收回来。",
            },
            {
                "cmd": "sudo firewall-cmd --permanent --add-port=8188/tcp && sudo firewall-cmd --reload",
                "title": "放行端口（CentOS/RHEL/firewalld）",
                "why": "CentOS 系用 firewalld。`--permanent` 才会持久化，加完必须 reload。",
            },
            {
                "cmd": "sudo firewall-cmd --list-all",
                "title": "看 firewalld 当前规则",
                "why": "确认端口真的进了 rules。",
            },
            {
                "cmd": "sudo iptables -I INPUT -p tcp --dport 8188 -j ACCEPT",
                "title": "iptables 直接放行",
                "why": "没装 ufw/firewalld 时。⚠ **重启会丢**，要持久化得装 iptables-persistent。",
            },
            {
                "cmd": "sudo netfilter-persistent save",
                "title": "持久化 iptables 规则",
                "why": "先把 iptables-persistent 装上再跑这条，否则重启就没了。",
            },
            {
                "cmd": "curl -I http://127.0.0.1:8188",
                "title": "第 3 步：本机自测",
                "why": "本机通、外面不通 → 一定是防火墙或安全组。本机就不通 → 进程没起来。",
            },
            {
                "cmd": "curl -I http://<你的公网IP>:8188",
                "title": "从外面自测（在另一台机器上跑）",
                "why": "换一台机器（手机热点也行）跑这条，能立刻区分「是网络层还是应用层」。",
            },
            {
                "cmd": "sudo tcpdump -i any -n port 8188",
                "title": "看包有没有到（终极手段）",
                "why": "外面 curl 时在这台机器上跑：**看得到包 = 防火墙没挡，问题在应用**；"
                       "看不到包 = 被云安全组或上游挡了。",
            },
            {
                "cmd": "nmap -p 8188 <你的公网IP>",
                "title": "从外部探端口开不开",
                "why": "比 curl 更直接地看端口是否 open。",
            },
        ],
    },
    {
        "id": "linux-proxy",
        "title": "⑭ Linux：nginx 反向代理（域名 + HTTPS）",
        "kind": "external",
        "note": "想让 ComfyUI 走 80/443、挂域名、上证书，就用它。"
                "⚠ 三个必改点：**WebSocket 升级头、上传体积、读超时**——漏一个就出怪问题。",
        "entries": [
            {
                "cmd": "sudo nano /etc/nginx/conf.d/comfyui.conf",
                "title": "新建 nginx 站点配置",
                "why": "内容见下一条。",
            },
            {
                "cmd": "server {\n    listen 80;\n    server_name comfy.example.com;\n\n    client_max_body_size 500m;\n\n    location / {\n        proxy_pass http://127.0.0.1:8188;\n        proxy_http_version 1.1;\n        proxy_set_header Upgrade $http_upgrade;\n        proxy_set_header Connection \"upgrade\";\n        proxy_set_header Host $host;\n        proxy_set_header X-Real-IP $remote_addr;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n        proxy_set_header X-Forwarded-Proto $scheme;\n        proxy_read_timeout 3600s;\n        proxy_send_timeout 3600s;\n        proxy_buffering off;\n    }\n}",
                "title": "★ nginx 配置内容（整段复制）",
                "why": "四个关键行：`proxy_http_version 1.1` + `Upgrade`/`Connection` "
                       "= **WebSocket 必需**（少了进度条不动、执行状态不同步）；"
                       "`client_max_body_size` = 上传上限（要和 `--max-upload-size` 对齐）；"
                       "`proxy_read_timeout 3600s` = 长任务别被 60s 默认值掐断；"
                       "`proxy_buffering off` = 进度推送要实时。",
                "note": "⚠ ComfyUI 那侧要配合 `--listen 0.0.0.0`（或至少让 nginx 能连上）。",
            },
            {
                "cmd": "sudo nginx -t",
                "title": "检查配置语法",
                "why": "**改完必跑**。写错了 nginx 会拒绝加载，但老进程还在跑，"
                       "你会以为改生效了其实没有。",
            },
            {
                "cmd": "sudo systemctl reload nginx",
                "title": "热加载（不断连接）",
                "why": "比 restart 温和。",
            },
            {
                "cmd": "sudo certbot --nginx -d comfy.example.com",
                "title": "★ 一键上 Let's Encrypt 证书",
                "why": "自动改 nginx 配置 + 自动续期。比手搓自签证书省事得多。",
            },
            {
                "cmd": "sudo certbot renew --dry-run",
                "title": "验证证书自动续期",
                "why": "证书 90 天过期，续期坏了会在某天突然全站不可用。配好就跑一次验证。",
            },
            {
                "cmd": "sudo tail -f /var/log/nginx/error.log",
                "title": "看 nginx 错误日志",
                "why": "502/504 的原因在这里，不在 ComfyUI 日志里。",
            },
        ],
    },
    {
        "id": "linux-docker",
        "title": "⑮ Linux：Docker / 容器",
        "kind": "external",
        "note": "容器里三个坑：**要 --listen 0.0.0.0、要 --gpus all、数据要挂出来**。",
        "entries": [
            {
                "cmd": "docker run -d --name comfy --gpus all -p 8188:8188 -v /data/models:/opt/ComfyUI/models -v /data/output:/opt/ComfyUI/output comfyui:latest --listen 0.0.0.0 --port 8188",
                "title": "★ 最小可用 docker run",
                "why": "`--gpus all` 不给就看不到卡；`-p 8188:8188` 把端口映射出来；"
                       "两个 `-v` 把模型和输出挂到宿主机（**不挂就等于每次重建都白跑**）；"
                       "最后那串 `--listen 0.0.0.0` 是**传给 ComfyUI 的**，不是给 docker 的。",
                "note": "⚠ 容器内不听 0.0.0.0 的话，端口映射了也连不上 —— 这是容器里最常见的坑。",
            },
            {
                "cmd": "docker logs -f comfy",
                "title": "看容器日志",
                "why": "容器里看不到 journalctl，只能看这个。",
            },
            {
                "cmd": "docker exec -it comfy bash",
                "title": "进容器",
                "why": "进去查文件、跑 nvidia-smi。",
            },
            {
                "cmd": "services:\n  comfyui:\n    image: comfyui:latest\n    container_name: comfy\n    restart: unless-stopped\n    ports:\n      - \"8188:8188\"\n    volumes:\n      - /data/models:/opt/ComfyUI/models\n      - /data/output:/opt/ComfyUI/output\n      - /data/input:/opt/ComfyUI/input\n    environment:\n      - PYTHONUNBUFFERED=1\n    command: [\"--listen\", \"0.0.0.0\", \"--port\", \"8188\", \"--vram-headroom\", \"1\"]\n    deploy:\n      resources:\n        reservations:\n          devices:\n            - driver: nvidia\n              count: all\n              capabilities: [gpu]",
                "title": "★ docker-compose 完整版（推荐）",
                "why": "`restart: unless-stopped` = 崩了/重启机器都自动拉起（等于容器版的开机自启）；"
                       "`command` 里可以正常带 ComfyUI 参数。",
            },
            {
                "cmd": "docker compose up -d && docker compose logs -f",
                "title": "起 + 跟日志",
                "why": "改完 compose 文件后重新 up 即可。",
            },
            {
                "cmd": "docker compose down",
                "title": "停掉",
                "why": "挂载的数据不会丢。",
            },
            {
                "cmd": "docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi",
                "title": "验证容器能看到 GPU",
                "why": "起 ComfyUI 之前先跑这个。看不到卡就别在 ComfyUI 里找了。",
            },
        ],
    },
    {
        "id": "cloud",
        "title": "⑯ ★ 云平台部署要点",
        "kind": "external",
        "note": "各家平台（AutoDL / RunPod / Vast.ai / 阿里云 / 腾讯云 / 华为云…）"
                "做法不同，但**失败原因就那么几条**。按这个顺序查，一次到位。",
        "entries": [
            {
                "cmd": "python main.py --listen 0.0.0.0 --port 8188",
                "title": "第 1 条：一定要 --listen 0.0.0.0",
                "why": "云平台的访问都是**从外面打进来**的，只听 127.0.0.1 永远连不上。"
                       "这一条能解决大半「部署了但打不开」。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --port 6006",
                "title": "第 2 条：端口要用平台允许的那个",
                "why": "很多平台**只转发特定端口**（AutoDL 的 6006、部分平台只开 "
                       "8080/8888/3000 之类）。ComfyUI 默认 8188 常常不在白名单里。",
                "note": "先去平台的「自定义服务 / 端口映射」页面看它允许哪些端口，再挑一个。",
            },
            {
                "cmd": "python main.py --base-directory /root/autodl-tmp/comfyui",
                "title": "第 3 条：数据放**数据盘**，别放系统盘",
                "why": "系统盘通常很小（30–50GB），模型几百 GB 一放就满。"
                       "满了之后报错五花八门（「采样跑到一半失败」），很难联想到磁盘。",
                "note": "用 `df -h` 确认哪个目录是数据盘（AutoDL 上是 /root/autodl-tmp）。",
            },
            {
                "cmd": "df -h",
                "title": "第 4 条：先确认磁盘",
                "why": "出任何「莫名其妙失败」都先看这个。",
            },
            {
                "cmd": "nvidia-smi",
                "title": "第 5 条：确认卡和驱动",
                "why": "看不到卡 → 是平台/驱动问题，不是 ComfyUI。"
                       "⚠ 本机（WDDM）nvidia-smi 不可用是特例，Linux 云平台上是可用的。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --port 8188 --vram-headroom 1",
                "title": "第 6 条：小显存实例先加余量",
                "why": "云上租的常常是 8/12/16GB 卡，默认策略会把显存吃到 100%，"
                       "二采这种流程最先炸。开局就带上。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --enable-cors-header '*'",
                "title": "第 7 条：平台套了前置页面时",
                "why": "有些平台用 iframe / 独立域名嵌进来，会撞 CORS。",
            },
            {
                "cmd": "setsid nohup python main.py --listen 0.0.0.0 --port 8188 > comfy.log 2>&1 < /dev/null &",
                "title": "第 8 条：让它在会话断开后继续跑",
                "why": "云平台的 Web 终端关掉页面 = 断 SSH = 前台进程被杀。"
                       "这条能活下来（更正规的用 systemd，见 ⑫）。",
            },
            {
                "cmd": "sudo ufw allow 8188/tcp",
                "title": "第 9 条：系统防火墙",
                "why": "平台镜像里常自带 ufw/firewalld 且开着。**平台安全组 + 系统防火墙**"
                       "两层都要放行，只放一层照样连不上。",
            },
            {
                "cmd": "sudo shutdown -h +60",
                "title": "⚠ 收工前记得关机（按量计费）",
                "why": "云上按小时计费，跑完不关就一直在烧钱。这条是 60 分钟后关。",
            },
        ],
    },
    {
        "id": "troubleshoot",
        "title": "⑰ 常见故障速查表",
        "kind": "external",
        "note": "按「现象 → 先查什么」组织。",
        "entries": [
            {
                "cmd": "ss -lntp | grep 8188",
                "title": "浏览器打不开 / 连接被拒绝",
                "why": "先看听的是 127.0.0.1 还是 0.0.0.0。前者 → 加 --listen 0.0.0.0。",
            },
            {
                "cmd": "sudo ufw status && sudo iptables -L -n | grep 8188",
                "title": "本机能开、外面不能开",
                "why": "防火墙/安全组。注意**两层**都要放行。",
            },
            {
                "cmd": "lsof -i :8188 && sudo kill -15 $(lsof -t -i:8188)",
                "title": "启动报 Address already in use",
                "why": "端口被占。杀掉旧的，或换 --port。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --vram-headroom 1 --disable-comfy-compiler",
                "title": "跑一会儿就 OOM / 显存 100%",
                "why": "先加余量，还不行再关编译器。",
            },
            {
                "cmd": "python main.py --verbose DEBUG",
                "title": "界面显示跑完了但结果不对",
                "why": "打开调试日志，然后去看**插件自己的报告行**"
                       "（本插件会把「二采失败：…本段按基础分辨率保存」写进报告）。",
                "note": "⚠ prompt 报 success **不等于**你要的东西出来了。"
                        "产物文件的修改时间没更新 = 那一步其实没落盘。",
            },
            {
                "cmd": "python main.py --disable-all-custom-nodes",
                "title": "启动就崩 / 卡在导入节点",
                "why": "先把第三方节点全禁了。能起来就逐个放回来二分。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --dont-print-server --log-stdout",
                "title": "容器里什么都看不到",
                "why": "容器只收 stdout，不加 --log-stdout 日志会走 stderr 丢掉。",
            },
            {
                "cmd": "du -sh /data/output && df -h",
                "title": "跑到一半莫名失败",
                "why": "先排除**磁盘写满**。云平台系统盘小，输出攒几次就满了。",
            },
            {
                "cmd": "sudo chown -R $USER:$USER /opt/ComfyUI",
                "title": "Permission denied",
                "why": "之前用 sudo 跑过留下的 root 属主文件。"
                       "⚠ 别用 `chmod -R 777`，那是在关安全。",
            },
            {
                "cmd": "python main.py --listen 0.0.0.0 --preview-method auto",
                "title": "想看采样过程长什么样",
                "why": "长任务时开预览，不必干等。",
            },
        ],
    },
]


# ============================ 导出 ============================

TITLE = "ComfyUI 启动命令速查"

# 基础命令。`kind == "comfyui"` 的分区里，条目都是**加在它后面的参数** ——
# 展示时把这段前缀剥掉，只留「要加的东西」。
#
# ⚠ 2026-09-27 用户反馈：「咋所有命令前面都有 `python main.py` 这一行，
# 把要加的东西列出来不就得了」—— 对，几十条命令每条都重复一遍基础命令是纯噪音。
# 数据里仍存**完整命令**（无歧义、可被 all_comfy_flags 直接扫），
# 剥前缀只发生在展示层（`_entry_cmd`），改一处两处都变。
BASE_CMD = "python main.py"


def _entry_cmd(sec, e):
    """返回 `(展示/复制的内容, 是否是完整命令)`。

    **按命令本身判断，不按分区类型**（2026-09-27 修正）：云平台/故障速查那两节
    标着 `external`（里面混着 `df -h`、`nvidia-smi` 这类 shell 命令），
    但它们的条目里也有 `python main.py --listen ...` —— 那些同样该只留参数。
    一开始按「分区 kind」判，结果那两节的 10 条漏剥了。

    两种例外原样返回并标成完整命令：
      · 那行**本身就是基础命令**（「最小启动」讲的就是它）；
      · 带环境变量前缀（`CUDA_VISIBLE_DEVICES=0 python main.py`）。
    """
    cmd = str(e["cmd"])
    if cmd == BASE_CMD:
        return cmd, True
    if cmd.startswith(BASE_CMD + " "):
        return cmd[len(BASE_CMD) + 1:], False
    return cmd, True


def _section_has_args(sec):
    """这一节里有没有「只列参数」的条目 —— 决定要不要印那句「都加在 main.py 后面」。"""
    return any(not _entry_cmd(sec, e)[1] for e in sec["entries"])


def has_args():
    """整份文档里有没有「只列参数」的条目。"""
    return any(_section_has_args(s) for s in SECTIONS)


def base_example():
    """基础命令的一个**完整写法示例**。

    从数据里拼出来，**不写死** —— 写死就是第二份命令副本，改了数据它不会跟着变
    （与前端「不硬编码命令」同一条纪律）。

    ⚠ 优先挑**带 ≥2 个参数**的条目：只挑第一条会落到「换端口」那种单参数条目上
    （`python main.py --port 8189`），当示例读起来像在教人改端口，不具代表性。
    挑不到就退回第一条纯参数的。
    """
    first = None
    for s in SECTIONS:
        for e in s["entries"]:
            show, full = _entry_cmd(s, e)
            if full:
                continue
            if first is None:
                first = show
            if show.count("--") >= 2:
                return f"{BASE_CMD} {show}"
    return f"{BASE_CMD} {first}" if first else BASE_CMD


def _comfy_flags_in(cmd):
    """从一段命令里抠出所有 `--flag`（长参数），去掉 `=value` 部分。

    只认以 `--` 开头、后面跟字母/数字/连字符/下划线的 token。
    """
    out = []
    for tok in str(cmd or "").replace("\n", " ").split():
        if not tok.startswith("--"):
            continue
        name = tok.split("=", 1)[0].strip("\"'")
        if len(name) > 2 and all(c.isalnum() or c in "-_" for c in name[2:]):
            out.append(name)
    return out


def all_comfy_flags():
    """文档里出现的全部 ComfyUI 参数（去重、保序）—— 给「不许编造参数」的测试用。"""
    seen = []
    for sec in SECTIONS:
        if sec.get("kind") != "comfyui":
            continue
        for e in sec["entries"]:
            for f in _comfy_flags_in(e["cmd"]):
                if f not in seen:
                    seen.append(f)
    return seen


def payload():
    """给前端弹窗用的 JSON（纯 dict，可直接 json.dumps）。

    ⚠ 每条 entry 除原始 `cmd` 外，另给两个**展示层**字段（2026-09-27 加）：
      · `show` —— 要展示/复制的内容。comfyui 分区里已剥掉 `python main.py`
        前缀，只剩要加的参数（见 `_entry_cmd`）。
      · `full` —— True 表示 `show` 本身就是完整命令（外部工具、或带环境变量前缀）。
    前端**只读 `show` / `full`**，别自己去剥 —— 剥的规则只有一处。

    `base_cmd` / `base_example` / `has_args` 放**顶层**（2026-09-27 改）：
    原先挂在每个分区上、由前端逐节渲染，结果 17 个分区把那句「参数都加在
    python main.py 后面」重复了 11 遍 —— 与「每条都重复基础命令」是同一类噪音。
    现在前端只在正文开头印一次。
    """
    secs = []
    for s in SECTIONS:
        out = dict(s)
        out["entries"] = []
        for e in s["entries"]:
            show, full = _entry_cmd(s, e)
            out["entries"].append(dict(e, show=show, full=full))
        secs.append(out)
    n = sum(len(s["entries"]) for s in SECTIONS)
    return {
        "title": TITLE,
        "base_cmd": BASE_CMD,
        "base_example": base_example(),
        "has_args": has_args(),
        "sections": secs,
        "section_count": len(SECTIONS),
        "entry_count": n,
        "comfy_flag_count": len(all_comfy_flags()),
        "txt_name": TXT_NAME,
    }


TXT_NAME = "ComfyUI启动命令速查.txt"

_RULE = "=" * 78
_THIN = "-" * 78


def _plain(s):
    """把面板用的 markdown 轻标记洗成纯文本：`**粗**` → 粗，`` `代码` `` → 代码。

    这份 txt 的用途是拿到终端 / 编辑器里读，`**` 和反引号在那儿只是噪音
    （弹窗里由前端 `mdBold()` 渲染成真加粗，两边各用各的表示法）。
    **只洗标记、保留内容** —— 不改成 `【】` 之类，那只是换一种噪音。
    """
    s = str(s if s is not None else "")
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    return s


def to_text():
    """生成纯文本版（LF 换行 + 轻标记已洗，见 `_plain`）。

    ⚠ **显式用 LF**：这份 txt 的主要用途是拿到 Linux 机器上边看边敲，
    CRLF 在 vim/less 里会多出 ^M，复制到 shell 还会带上 \\r 导致命令报错。
    """
    lines = [
        _RULE,
        f"  {TITLE}",
        "  由 ComfyUI-minimaxH3-SequenceForge 生成（单一真源：launch_ref.py）",
        "  —— 面板里的「📖 启动命令」与本文件内容同源，改一处两处都变。",
        _RULE,
        "",
        "说明：",
        f"  · 标「参数：」的行是「要加在 {BASE_CMD} 后面的东西」，只列参数本身；",
        "    标「$」的行是完整命令，可直接跑。",
        "  · 不重复写基础命令（每条都带一遍 python main.py 是纯噪音）。完整写法示例：",
        f"      {base_example()}",
        "  · 「⚠」是踩过的坑，建议先看。",
        "  · 「★」是本插件场景下最常用的几条。",
        "  · 标 (Linux) 的段落与 ComfyUI 本身无关，是部署环境的事。",
        "",
    ]
    for i, sec in enumerate(SECTIONS, 1):
        lines.append("")
        lines.append(_RULE)
        lines.append(f"  {_plain(sec['title'])}")
        lines.append(_RULE)
        if sec.get("note"):
            lines.append("")
            lines.append(f"  {_plain(sec['note'])}")
        for e in sec["entries"]:
            show, full = _entry_cmd(sec, e)
            lines.append("")
            lines.append(f"  ▸ {_plain(e['title'])}")
            for j, cl in enumerate(show.split("\n")):
                if full:
                    lines.append(("    $ " if j == 0 else "      ") + cl)
                else:
                    lines.append(("    参数：" if j == 0 else "          ") + cl)
            # ⚠ 不再写「为什么要这样：」/「注意：」这类标签（2026-09-27 用户反馈：
            # 「咋有一堆为什么要这样，你直接说明不就得了」）—— 直接说事。
            # why 与 note 在展示上就是一前一后两段，区别只在数据模型里。
            lines.append(f"    {_plain(e['why'])}")
            if e.get("note"):
                lines.append(f"    {_plain(e['note'])}")
    lines += [
        "",
        _RULE,
        "  完",
        _RULE,
        "",
    ]
    return "\n".join(lines)


def write_text(path):
    """把 txt 写到 `path`（显式 LF，见 `to_text` 注释）。返回写入的字节数。"""
    data = to_text()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)
    return len(data.encode("utf-8"))
