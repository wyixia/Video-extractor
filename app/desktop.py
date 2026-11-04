"""原生窗口模式：把界面装进系统窗口，不跳浏览器。

用 pywebview + 系统自带的 WebView2（Win10/11 一般都有），不额外打包浏览器内核。
窗口关掉就等于退出：服务线程跟着收，不留后台进程。

拿不到窗口环境时（缺 pywebview、缺 WebView2、无图形界面）抛 RuntimeError，
调用方回退到浏览器模式 —— 功能一样，只是显示位置不同。
"""
import threading
import time
from pathlib import Path

from app.server import build_server

TITLE = "视频分镜标注"


def available() -> tuple:
    """能不能开原生窗口。返回 (能否, 原因)。"""
    try:
        import webview  # noqa: F401
    except Exception as e:
        return False, f"没装 pywebview（{type(e).__name__}）"
    if not _has_gui():
        return False, "当前环境没有图形界面"
    return True, ""


def _has_gui() -> bool:
    import sys
    if sys.platform.startswith("win"):
        try:
            import ctypes
            return bool(ctypes.windll.user32.GetSystemMetrics(0))
        except Exception:
            return False
    import os
    return bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")) or \
        sys.platform == "darwin"


def run(port: int = 0, provider: str = "", log_file: Path = None) -> int:
    ok, why = available()
    if not ok:
        raise RuntimeError(why)

    import webview

    srv = build_server(port, provider)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"[应用] {url}（窗口模式）")
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    window = webview.create_window(
        TITLE, url, width=1360, height=900, min_size=(1020, 660),
        background_color="#0f1115",
    )

    def _stop(*_a):
        # 窗口已关：让服务退出，主线程的 webview.start() 也会返回
        threading.Thread(target=srv.shutdown, daemon=True).start()

    window.events.closed += _stop
    # 页面加载成功/失败都记一笔：打包后最常见的故障就是窗口起来但一片空白
    window.events.loaded += lambda *a: print("[应用] 界面已加载")
    # 界面上的「退出」也走这里：关窗口，而不是只停服务
    srv.on_quit = window.destroy

    try:
        webview.start()          # 阻塞到窗口关闭
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
        srv.server_close()
    return 0
