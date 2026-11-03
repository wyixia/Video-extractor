"""运行环境探测：定位 ffmpeg / ffprobe。

打包后用户机器上不一定装了 ffmpeg，所以不能写死从 PATH 里找。
查找顺序：项目自带 bin/ → PATH。自带优先，这样"完整包"即使系统没装也能直接跑，
而源码包用户自己装了 ffmpeg 也能用。
"""
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def project_root() -> Path:
    """项目根目录。

    打包成可执行文件后，代码被解到临时目录（_MEIPASS），但 config/ 和 bin/
    必须留在 exe 旁边——提示词要能让用户直接改，ffmpeg 也不能每次解压。
    所以冻结态用 exe 所在目录，源码态用本文件的上级目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _ROOT


def _names(stem: str):
    return [f"{stem}.exe", stem] if sys.platform == "win32" else [stem]


def _search(stem: str):
    """依次找 bin/、vendor/ffmpeg/bin/、PATH。"""
    root = project_root()
    for d in (root / "bin", root / "vendor" / "ffmpeg" / "bin"):
        for n in _names(stem):
            p = d / n
            if p.exists():
                return str(p)
    found = shutil.which(stem)
    return found


def ffmpeg_bin() -> str:
    return _search("ffmpeg") or "ffmpeg"


def ffprobe_bin() -> str:
    return _search("ffprobe") or "ffprobe"


def describe() -> dict:
    """给体检报告用的环境快照。"""
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": sys.platform,
        "ffmpeg": ffmpeg_bin(),
        "ffmpeg_bundled": Path(ffmpeg_bin()).is_absolute(),
        "ffprobe": ffprobe_bin(),
        "project_root": str(_ROOT),
    }
