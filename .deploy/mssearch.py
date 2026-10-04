import json
import urllib.request


def post(url, payload):
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "h3-deploy"},
        )
        return json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as e:
        return {"__error__": str(e)}


for q in ["minimax_h3_hybrid", "hybrid_fl2va", "Minimax-H3-fl2va"]:
    print("=" * 10, "ModelScope search:", q)
    d = post(
        "https://modelscope.cn/api/v1/dolphin/models",
        {"Query": q, "PageSize": 20, "PageNumber": 1},
    )
    if "__error__" in d:
        print("  ERR", d["__error__"])
        continue
    data = d.get("Data") or {}
    models = ((data.get("Model") or {}).get("Models")) or []
    for m in models:
        print("  ", (m.get("Path") or "") + "/" + (m.get("Name") or ""))
    if not models:
        print("  (no results)", json.dumps(d)[:200])
