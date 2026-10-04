import json
import os
import struct
import sys


def verify(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        hdr = json.loads(f.read(n).decode("utf-8"))
    hdr.pop("__metadata__", None)
    max_end = 0
    for v in hdr.values():
        max_end = max(max_end, v["data_offsets"][0] + v["data_offsets"][1])
    expected = 8 + n + max_end
    ok = expected == size
    print(
        "%-75s tensors=%-4d expected=%d actual=%d %s"
        % (os.path.basename(path), len(hdr), expected, size, "OK" if ok else "MISMATCH")
    )
    return ok


for p in sys.argv[1:]:
    verify(p)
