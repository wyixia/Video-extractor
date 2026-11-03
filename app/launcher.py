"""打包入口（供 PyInstaller 使用）。

源码运行时用 `python -m app.server`，打包后从 exe 进来，
两条路径最后都落到 serve()。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.server import serve  # noqa: E402


def main() -> int:
    port = 0
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
    serve(port=port, open_browser=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
