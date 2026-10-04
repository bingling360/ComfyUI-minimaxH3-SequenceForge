import json
import urllib.request


def get(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "h3-deploy"})
        return json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as e:
        return {"__error__": str(e)}


for repo in ["Comfy-Org/MiniMax-H3", "lightx2v/Minimax-h3-Turbo", "LBH-123-AI/Minimax_h3_latent_Upscaler"]:
    print("=" * 10, repo)
    d = get("https://modelscope.cn/api/v1/models/%s/repo/files?Recursive=true&PageSize=200" % repo)
    if "__error__" in d:
        print("  ERR", d["__error__"])
        continue
    files = (d.get("Data") or {}).get("Files") or []
    for f in files:
        sz = f.get("Size") or 0
        name = f["Path"]
        if sz > 1e8:
            print("  %7.2fGB  %s" % (sz / 1e9, name))
        elif sz > 1e6:
            print("  %7.2fMB  %s" % (sz / 1e6, name))
        else:
            print("          %s" % name)
