"""打包入口（供 PyInstaller 使用）。

源码运行时用 `python -m app.server`，打包后从 exe 进来，
两条路径最后都落到 serve()。

打包版是无控制台（--windowed）的：双击不弹黑窗。代价是 stdout/stderr 为 None，
print 会直接崩，所以先把它们兜到 _work/app.log；启动失败也用系统弹窗说出来，
否则窗口一闪用户什么都看不到。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ensure_streams(log_file: Path):
    """windowed 模式下 sys.stdout / sys.stderr 是 None，print 会抛异常。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        f = open(log_file, "a", encoding="utf-8", buffering=1)
    except Exception:
        import io
        f = io.StringIO()
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def _msgbox(text: str):
    """没有控制台时的错误出口。非 Windows 就打到 stderr。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "视频分镜标注", 0x10)
        return
    except Exception:
        pass
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass


def main() -> int:
    from src.env import project_root
    log_file = project_root() / "_work" / "app.log"
    _ensure_streams(log_file)

    port, provider = 0, ""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a == "--provider" and i + 1 < len(argv):
            provider = argv[i + 1]

    try:
        from app.server import serve
        serve(port=port, open_browser=True, provider=provider)
        return 0
    except Exception as e:
        _msgbox(f"启动失败：{type(e).__name__}: {e}\n\n日志：{log_file}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
