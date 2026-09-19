"""探针：GLM 流式响应里 usage 什么时候到？stream_options 收不收？

决定进度条是「真实百分比」还是「跑马灯」—— 只有中途能拿到 completion_tokens，
才谈得上按 token/max_tokens 画比例。
"""
import json
import os
import sys
import time
import urllib.request

URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def _api_key():
    """取 Key：优先环境变量，其次仓库根目录的 optimizer.local.json（已 gitignore）。

    ⚠ 绝不把 Key 写进源码 —— 本仓库是公开的。
    """
    env = os.environ.get("GLM_API_KEY") or os.environ.get("H3_OPT_API_KEY")
    if env:
        return env.strip()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("optimizer.local.json",):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    cfg = json.load(fh)
            except Exception:
                continue
            keys = cfg.get("api_keys") or {}
            for k in (cfg.get("provider") or "glm", "glm"):
                if isinstance(keys.get(k), str) and keys[k].strip():
                    return keys[k].strip()
            if isinstance(cfg.get("api_key"), str) and cfg["api_key"].strip():
                return cfg["api_key"].strip()
    return ""


KEY = _api_key()


def probe(model, stream_options, prompt, max_tokens=256):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
    }
    if stream_options is not None:
        payload["stream_options"] = stream_options
    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {KEY}"}, method="POST")
    t0 = time.monotonic()
    frames = 0
    with_usage = 0
    first = None
    last_usage = None
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                frames += 1
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue
                u = obj.get("usage")
                if isinstance(u, dict) and u:
                    with_usage += 1
                    last_usage = u
                    if first is None:
                        first = (round(time.monotonic() - t0, 1), frames)
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return (f"frames={frames} usage_frames={with_usage} "
            f"first_usage={first} last={last_usage} "
            f"elapsed={round(time.monotonic() - t0, 1)}s")


if __name__ == "__main__":
    if not KEY:
        sys.exit("未找到 API Key：请设 GLM_API_KEY 环境变量，"
                 "或在仓库根放 optimizer.local.json（已 gitignore，内含 api_keys.glm）")
    long_prompt = "请写一段 200 字左右的电影分镜描述，镜头缓慢推近。"
    print("A) glm-4.6v 不带 stream_options:", probe("glm-4.6v", None, long_prompt), flush=True)
    print("B) glm-4.6v 带 include_usage:", probe("glm-4.6v", {"include_usage": True}, long_prompt), flush=True)
    print("C) glm-5.3-flash 带 include_usage:",
          probe("glm-5.3-flash", {"include_usage": True}, long_prompt), flush=True)
    sys.stdout.flush()
