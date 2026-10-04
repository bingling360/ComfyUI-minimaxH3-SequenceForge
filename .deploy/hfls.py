import json
import urllib.request


def get(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "h3-deploy"})
        return json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as e:
        return {"__error__": str(e)}


repos = [
    "smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models",
    "Comfy-Org/MiniMax-H3",
]
for repo in repos:
    print("=" * 10, repo)
    d = get("https://huggingface.co/api/models/%s?blobs=true" % repo)
    if "__error__" in d or "siblings" not in d:
        print("  ERROR:", d.get("__error__", d))
        continue
    for s in d["siblings"]:
        sz = s.get("size") or 0
        name = s["rfilename"]
        if sz > 1e8:
            print("  %7.2fGB  %s" % (sz / 1e9, name))
        elif sz > 0:
            print("  %7.2fMB  %s" % (sz / 1e6, name))
        else:
            print("          %s" % name)
