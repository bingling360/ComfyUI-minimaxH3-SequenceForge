"""把 `launch_ref.to_text()` 写成 docs/ 下的 txt。

**单一真源**：txt 内容完全由 `launch_ref.py` 生成，本脚本只负责落盘。
`tests/test_launch_ref.py` 有一条守卫会比对「磁盘上那份」与「实时生成的」是否一致 ——
改了 launch_ref.py 却忘了重跑本脚本，测试会直接红。

用法（仓库根目录）：
    python tools/make_launch_ref_txt.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import launch_ref  # noqa: E402


def main():
    out = os.path.join(ROOT, "docs", launch_ref.TXT_NAME)
    n = launch_ref.write_text(out)
    print(f"写出 {out}（{n} 字节 · {len(launch_ref.to_text().splitlines())} 行 · LF）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
