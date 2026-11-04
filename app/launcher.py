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


class _Tee:
    """把输出同时写到原流和日志文件。任一失败都不影响另一个。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


def _ensure_streams(log_file: Path):
    """无控制台时 stdout/stderr 是 None，print 会抛异常；
    有控制台时也抄一份到文件，事后能查启动过程。"""
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        f = open(log_file, "a", encoding="utf-8", buffering=1)
    except Exception:
        import io
        f = io.StringIO()
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
        return
    sys.stdout = _Tee(sys.stdout, f)
    sys.stderr = _Tee(sys.stderr, f)


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

    port, provider, browser = 0, "", True
    force_browser = False
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a == "--provider" and i + 1 < len(argv):
            provider = argv[i + 1]
        elif a == "--browser":
            force_browser = True
        elif a == "--no-browser":
            browser = False

    # 默认开原生窗口；拿不到窗口环境就回退浏览器，功能不变
    if not force_browser:
        try:
            from app.desktop import run
            return run(port=port, provider=provider)
        except Exception as e:
            print(f"[应用] 打不开窗口（{type(e).__name__}: {e}），改用浏览器")

    try:
        from app.server import serve
        serve(port=port, open_browser=browser, provider=provider)
        return 0
    except Exception as e:
        _msgbox(f"启动失败：{type(e).__name__}: {e}\n\n日志：{log_file}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
