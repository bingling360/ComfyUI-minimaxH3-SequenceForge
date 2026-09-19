# -*- coding: utf-8 -*-
"""优化器 HTTP 端到端冒烟：把 routes.py 挂到**真 aiohttp** 上打一遍。

和 `tests/test_rework_v2.py::test_optimizer_routes_mount` 的区别：
那边 `aiohttp.web` 是 stub，只验"路由表里有没有这条"；这里跑真服务器，
验的是「前端 JSON → 路由 → optimize_once → 响应」整条链，并且**同时**打
根路径与 `/api` 前缀两份（新版 ComfyUI 前端 `api.fetchApi()` 会给所有不
以 /api 开头的路径强制加前缀，只挂根路径会 404 —— 这个坑只在这里能验到）。

为什么必须是独立脚本、不能进 pytest：
- 需要真 aiohttp（pytest 那套 env 夹具把 aiohttp.web 换成了 stub）；
- `--live` 会真的调 LLM 花钱、耗时长，CI 里不能跑。

跑法（仓库根目录）：
    # 只验配置下发 + SSE 路由帧格式（不花钱）
    python tools/optimizer_http_smoke.py

    # 连真实 LLM 一起验（会调一次优化，约 30 秒）
    python tools/optimizer_http_smoke.py --live --task REF2VA --duration 10

    # 额外验一遍流式（进度帧真的在中途到达，而不是全堆在最后）
    python tools/optimizer_http_smoke.py --live --stream

    # 带参考图（最多 8 张）
    python tools/optimizer_http_smoke.py --live --image a.jpg --image b.png

退出码：0 = 全通，1 = 有失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_folder_paths_stub() -> str:
    """真 `folder_paths` 桩。注意 routes.py 顶部是 `from folder_paths import
    get_output_directory`（**导入期绑定**）——只改假模块不够，得在 exec routes 之前装好。"""
    tmp = tempfile.mkdtemp(prefix="h3http_")
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = lambda: tmp
    fp.get_input_directory = lambda: tmp
    fp.get_annotated_filepath = lambda f: os.path.join(tmp, f)
    sys.modules["folder_paths"] = fp
    return tmp


def _load_top(name: str, filename: str, patches=None):
    """按 tests 的同款方式把顶层模块跑起来（把 `from . import x` 换成 `import x`）。"""
    path = os.path.join(ROOT, filename)
    src = open(path, encoding="utf-8").read()
    for a, b in (patches or []):
        src = src.replace(a, b)
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    mod.__dict__["__file__"] = path      # routes 靠 __file__ 定位 tools/h3_prompt_expander
    sys.modules[name] = mod
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


def _load_routes():
    """装载 routes.py（及其依赖）。

    routes.py 里的相对导入有两种形态，都要处理：
    - 模块顶层：`from . import library as h3lib`（在源码里替换即可）
    - **handler 函数体内**：`from . import optimizer as _opt`（带 try/except 回落到
      `import optimizer as _opt`）—— 这种必须在 exec 前就把同名顶层模块塞进
      sys.modules，否则第一次请求才炸 ImportError（500），本脚本看起来"服务起来了"
      其实一条路由都没验到。
    所以：先预装模块，再把**所有** `from . import ` 统一替换成 `import `。
    """
    _load_top("checkpoint", "checkpoint.py")
    _load_top("projects", "projects.py", [("from . import checkpoint", "import checkpoint")])
    _load_top("library", "library.py")
    _load_top("prompts", "prompts.py")
    _load_top("optimizer", "optimizer.py")
    return _load_top("routes", "routes.py", [("from . import ", "import ")])


def _data_url(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    with open(path, "rb") as fh:
        return "data:%s;base64,%s" % (mime, base64.b64encode(fh.read()).decode())


def _frontend_config(overrides: dict | None = None) -> dict:
    """前端 `optGetSettings()` 迁移后、点过一次「保存」的真实形状。

    特意保留 `api_keys: {"glm": ""}` —— 空串必须与"缺失"同权，否则服务端内置
    Key 的兜底会被这个空串顶掉（历史 bug，见 tests/test_optimizer_glm.py）。
    """
    cfg = {
        "mode": "api", "provider": "glm",
        "api_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "", "api_keys": {"glm": ""},
        "model": "glm-5.3-flashx", "provider_models": {"glm": "glm-5.3-flashx"},
        "protocol": "openai", "read_media": True, "output_language": "中文",
        "local_model": "", "local_mmproj": "", "local_device": "cuda",
        "max_tokens": 8192, "timeout": 300, "thinking": "disabled",
        "reasoning_effort": "",
        "rule_file": "auto", "cfg_ver": 2,
        "expand": {"style": "balanced", "seconds_min": 4, "seconds_max": 15,
                   "then_optimize": True},
    }
    cfg.update(overrides or {})
    return cfg


async def _read_sse(resp) -> tuple:
    """边收边解析 SSE（与前端 `_postStream` 同口径：缓冲到空行才成帧）。

    返回 (events, first_progress_secs)。first_progress_secs 是**第一帧 progress
    到达的时刻** —— 进度条的意义全在这上面：如果所有帧都堆在最后一起到，
    前端画出来就是"一直不动，然后突然完成"，等于没做。
    """
    import time as _t
    t0 = _t.time()
    first_progress = None
    events = []
    buf = ""
    async for raw in resp.content:
        buf += raw.decode("utf-8", "replace")
        while True:
            i = buf.find("\n\n")
            if i < 0:
                break
            frame, buf = buf[:i], buf[i + 2:]
            kind, data = "message", []
            for line in frame.replace("\r", "").split("\n"):
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
            if not data:
                continue
            try:
                obj = json.loads("\n".join(data))
            except ValueError:
                continue
            obj["type"] = kind
            if kind == "progress" and first_progress is None:
                first_progress = _t.time() - t0
            events.append(obj)
    return events, first_progress


async def _run(args) -> int:
    _install_folder_paths_stub()
    routes = _load_routes()

    from aiohttp import web
    import aiohttp

    app = web.Application()
    routes.add_routes(app.router)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    base = "http://127.0.0.1:%d" % runner.addresses[0][1]
    print("服务已起：%s\n" % base)

    failed = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        print("  %s %s%s" % ("OK  " if ok else "FAIL", label, ("  :: " + detail) if detail else ""))
        if not ok:
            failed.append(label)

    async def read_json(resp):
        """路由出错时 aiohttp 回的是 text/plain，直接 .json() 会抛 —— 包一层，
        让失败变成一条 FAIL 而不是把整个脚本炸掉。"""
        try:
            return await resp.json()
        except Exception:
            return {"_raw": (await resp.text())[:400]}

    try:
        async with aiohttp.ClientSession() as sess:
            print("[1] 配置下发（GET optimizer-config）")
            for path in ("/h3chain/optimizer-config", "/api/h3chain/optimizer-config"):
                async with sess.get(base + path) as r:
                    j = await read_json(r)
                check(r.status == 200 and j.get("ok") is True, "GET %s 200" % path,
                      str(j.get("_raw") or ""))
                check(j.get("api_key") == "", "GET %s Key 已脱敏" % path)
                print("        provider=%s model=%s timeout=%s thinking=%s "
                      "has_default_key=%s"
                      % (j.get("provider"), j.get("model"), j.get("timeout"),
                         j.get("thinking"), j.get("has_default_key")))
                if path == "/h3chain/optimizer-config":
                    check(j.get("provider") == "glm", "默认服务商是 glm")
                    check(j.get("model") == "glm-5.3-flashx", "默认模型是 glm-5.3-flashx")
                    check(int(j.get("timeout") or 0) >= 120, "超时不低于 120s")
                    # 前端「思考强度」下拉完全靠这三张表渲染，缺一张就退化成
                    # 只能开/关（用户报的"强度不能选"就会复现）。
                    for k in ("glm_force_thinking", "glm_effort_prefixes",
                              "glm_effort_values", "model_caps"):
                        check(bool(j.get(k)), "配置下发 %s" % k)
                    check("glm-5.3" in (j.get("glm_force_thinking") or []),
                          "glm-5.3 在强制思考名单里")
                    # 默认型号本身是「始终思考」型号 → 它的「关闭」会被翻译成 low
                    check((j.get("model_caps") or {}).get("disabled_effect") == "low",
                          "默认型号（glm-5.3-flashx）的「关闭」= low")
                    check((j.get("model_caps") or {}).get("supports_effort") is True,
                          "默认型号支持强度分级")

            print("\n[2] SSE 流式路由（不花钱：故意指向死地址，验帧格式与错误路径）")
            dead = _frontend_config({"api_url": "http://127.0.0.1:1/v1", "timeout": 5,
                                     "max_tokens": 512})
            for route in ("/h3chain/optimize_stream", "/h3chain/expand_optimize_stream",
                          "/h3chain/optimize_multi_stream"):
                for path in (route, "/api" + route):
                    payload = {"prompt": "测试", "task": "REF2VA", "duration": 5,
                               "config": dead, "media": []}
                    if route.endswith("optimize_multi_stream"):
                        # 多段那条吃的是 segments：不带的话会先在**参数校验**上失败，
                        # 那就验不到"连不上 LLM"这条错误路径了（末帧同样是 error，
                        # 但 message 不一样，等于这条断言变成假阳性）。
                        payload["segments"] = [{"prompt": "测试", "seconds": 5,
                                                "task": "REF2VA"}]
                    t0 = time.time()
                    async with sess.post(base + path, json=payload) as r:
                        ctype = r.headers.get("Content-Type", "")
                        events, _fp = await _read_sse(r)
                    kinds = [e.get("type") for e in events]
                    check(r.status == 200, "POST %s HTTP 200" % path,
                          "status=%s" % r.status)
                    check("text/event-stream" in ctype, "%s 是 SSE 内容类型" % path, ctype)
                    check(kinds and kinds[-1] == "error", "%s 末帧是 error（连接失败）" % path,
                          str(kinds))
                    msg = (events[-1].get("message") if events else "") or ""
                    check(bool(msg), "%s error 帧带可读 message" % path, msg[:120])
                    print("        %.2fs  %d 帧  %s" % (time.time() - t0, len(events), kinds))

            if not args.live:
                print("\n[3] 真实优化调用 —— 跳过（未给 --live）")
                return 1 if failed else 0

            prompt = (open(args.prompt_file, encoding="utf-8").read()
                      if args.prompt_file else
                      (args.prompt or "一个少女在石柱廊里回头看向镜头，肩上立着一个微缩的自己。"))
            media = []
            for i, img in enumerate(args.image or [], 1):
                media.append({"label": "图片%d" % i, "images": [_data_url(img)],
                              "note": "参考图 %d" % i})
            body = {
                "prompt": prompt, "task": args.task, "duration": args.duration,
                "temperature": 0.2, "context": {"main_mode": args.task},
                "config": _frontend_config({"read_media": bool(media),
                                            "thinking": args.thinking}),
                "media": media,
            }

            print("\n[3] 真实优化调用（POST optimize）")
            for path in ("/h3chain/optimize", "/api/h3chain/optimize"):
                t0 = time.time()
                async with sess.post(base + path, json=body) as r:
                    j = await read_json(r)
                dt = time.time() - t0
                if r.status != 200 or not j.get("ok"):
                    check(False, "POST %s" % path,
                          json.dumps(j, ensure_ascii=False)[:300])
                    continue
                txt = str(j.get("prompt") or "")
                check(True, "POST %s HTTP 200  %.1fs  %d 字" % (path, dt, len(txt)))
                check(all(k in txt for k in ("subject_definitions", "summary",
                                             "retention_analysis", "detailed_description",
                                             "overall_soundscape", "non_diegetic_music"))
                      if args.task.upper() in ("REF2VA", "HYBRID")
                      else "integrated_multimodal_description" in txt,
                      "%s 字段齐全" % path)
                print("        首 80 字：%s" % txt[:80].replace("\n", " "))

            print("\n[4] 真实流式优化（POST optimize_stream，验进度帧）")
            if args.stream:
                sbody = dict(body)
                sbody["config"] = _frontend_config({
                    "model": args.stream_model, "thinking": "disabled",
                    "reasoning_effort": "", "max_tokens": 16384,
                    "read_media": bool(media)})
                for path in ("/h3chain/optimize_stream", "/api/h3chain/optimize_stream"):
                    t0 = time.time()
                    async with sess.post(base + path, json=sbody) as r:
                        events, first = await _read_sse(r)
                    dt = time.time() - t0
                    kinds = [e.get("type") for e in events]
                    prog = [e for e in events if e.get("type") == "progress"]
                    last = events[-1] if events else {}
                    check(r.status == 200 and last.get("type") == "done",
                          "POST %s 以 done 收尾" % path,
                          json.dumps(last, ensure_ascii=False)[:200])
                    check(len(prog) >= 3, "%s 进度帧够多（%d）" % (path, len(prog)),
                          "帧序列=%s" % kinds[:6])
                    check(first is not None and first < dt * 0.8,
                          "%s 首帧进度早于结束（%.1fs / 共 %.1fs）" % (path, first or -1, dt))
                    txt = str(last.get("prompt") or "")
                    check(len(txt) > 200, "%s 正文长度 %d" % (path, len(txt)))
                    print("        %.1fs  首帧 %.1fs  %d 进度帧  %d 字"
                          % (dt, first or -1, len(prog), len(txt)))

                print("\n[5] 真实流式「扩写+优化」（POST expand_optimize_stream，验两段进度）")
                ebody = {
                    "config": _frontend_config({"model": args.stream_model,
                                                "thinking": "disabled",
                                                "max_tokens": 16384,
                                                "read_media": bool(media)}),
                    "prompt": args.prompt or "一个少女在石柱廊里回头看向镜头，肩上立着一个微缩的自己。",
                    "style": "balanced", "seconds_min": 4, "seconds_max": 15,
                    "segment_count": 1, "task": args.task, "duration": args.duration,
                    "media": media,
                }
                for path in ("/h3chain/expand_optimize_stream",
                             "/api/h3chain/expand_optimize_stream"):
                    t0 = time.time()
                    async with sess.post(base + path, json=ebody) as r:
                        events, first = await _read_sse(r)
                    dt = time.time() - t0
                    prog = [e for e in events if e.get("type") == "progress"]
                    stages = []
                    for e in prog:
                        s = e.get("stage") or "-"
                        if not stages or stages[-1] != s:
                            stages.append(s)
                    last = events[-1] if events else {}
                    check(r.status == 200 and last.get("type") == "done",
                          "POST %s 以 done 收尾" % path,
                          json.dumps(last, ensure_ascii=False)[:200])
                    check("expand" in stages, "%s 有扩写阶段进度帧" % path, str(stages))
                    check("optimize" in stages, "%s 有优化阶段进度帧" % path, str(stages))
                    check(first is not None and first < dt * 0.8,
                          "%s 首帧进度早于结束（%.1fs / 共 %.1fs）" % (path, first or -1, dt))
                    txt = str(last.get("prompt") or "")
                    check(len(txt) > 200, "%s 成品长度 %d" % (path, len(txt)))
                    print("        %.1fs  首帧 %.1fs  %d 进度帧  阶段=%s  %d 字"
                          % (dt, first or -1, len(prog), stages, len(txt)))

                print("\n[6] 真实流式「多段优化」（POST optimize_multi_stream，验「第 i/N 段」）")
                mbody = {
                    "config": _frontend_config({"model": args.stream_model,
                                                "thinking": "disabled",
                                                "max_tokens": 16384,
                                                "read_media": bool(media)}),
                    "task": args.task, "media": media,
                    "segments": [
                        {"prompt": "少女在石柱廊里回头看向镜头。",
                         "seconds": 5, "task": args.task},
                        {"prompt": "她肩上的微缩自己抬手挥了挥。",
                         "seconds": 5, "task": args.task},
                    ],
                }
                for path in ("/h3chain/optimize_multi_stream",
                             "/api/h3chain/optimize_multi_stream"):
                    t0 = time.time()
                    async with sess.post(base + path, json=mbody) as r:
                        events, first = await _read_sse(r)
                    dt = time.time() - t0
                    prog = [e for e in events if e.get("type") == "progress"]
                    nos = []
                    for e in prog:
                        n = e.get("seg_no")
                        if n and (not nos or nos[-1] != n):
                            nos.append(n)
                    last = events[-1] if events else {}
                    check(r.status == 200 and last.get("type") == "done",
                          "POST %s 以 done 收尾" % path,
                          json.dumps(last, ensure_ascii=False)[:200])
                    check(nos == [1, 2], "%s 段序号按 1→2 推进" % path, str(nos))
                    check(bool(prog) and all(e.get("total") == 2 for e in prog),
                          "%s total 恒为 2" % path,
                          str(sorted({e.get("total") for e in prog})))
                    check(first is not None and first < dt * 0.8,
                          "%s 首帧进度早于结束（%.1fs / 共 %.1fs）" % (path, first or -1, dt))
                    segs = last.get("segments") or []
                    check(len(segs) == 2, "%s 回传 2 段结果" % path, str(len(segs)))
                    print("        %.1fs  首帧 %.1fs  %d 进度帧  段=%s  %d 段结果"
                          % (dt, first or -1, len(prog), nos, len(segs)))
            else:
                print("     跳过（未给 --stream）")
    finally:
        await runner.cleanup()

    print("\n%s" % ("全部通过" if not failed else "失败 %d 项：%s" % (len(failed), failed)))
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="优化器 HTTP 端到端冒烟（真 aiohttp）")
    ap.add_argument("--live", action="store_true", help="真调一次 LLM（花钱、约 30 秒）")
    ap.add_argument("--task", default="REF2VA",
                    choices=["T2VA", "I2VA", "FL2VA", "L2VA", "REF2VA", "HYBRID"])
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--prompt", default="", help="待优化提示词（不填用内置短例）")
    ap.add_argument("--prompt-file", default="", help="从文件读待优化提示词")
    ap.add_argument("--image", action="append", help="参考图路径，可重复（最多 8 张）")
    ap.add_argument("--thinking", default="disabled", choices=["auto", "enabled", "disabled"])
    ap.add_argument("--stream", action="store_true",
                    help="额外跑一遍 SSE 流式（配合 --live，验进度帧真的在中途到达）")
    ap.add_argument("--stream-model", default="glm-5.3-flashx",
                    help="流式那两遍用的模型（默认就是插件的默认型号）")
    args = ap.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
