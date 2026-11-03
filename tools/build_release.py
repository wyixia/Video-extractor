"""打发行包。

两种形态，解决的是两个不同问题：

  lite —— 只发代码。体积几 MB，用户机器上要有 Python 3.9+，ffmpeg 由启动脚本
          自动检测并下载。适合愿意装 Python、想自己改代码的人。
  full —— 连解释器一起打进去，exe 双击就能跑，ffmpeg 也带上。体积一两百 MB。
          适合只想用、不想配环境的人。

两者用的是同一份代码、同一份配置、同一套提示词，功能没有差别。

用法：
  python tools/build_release.py --flavor lite --version 1.0.0
  python tools/build_release.py --flavor full --version 1.0.0
  python tools/build_release.py --flavor both
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DIST = ROOT / "dist"
# 打进去的源码目录；_work/output 是运行产物，绝不能进包
INCLUDE_DIRS = ["app", "src", "tools", "config", "docs", "skills", "tests"]
SKIP_NAMES = {"__pycache__", ".pytest_cache", ".git", ".idea", ".vscode", "dist", "_probe",
              "_work", "output"}
SKIP_FILES = {"config/config.local.yaml"}

BAT = """@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Video Extractor - starting
echo ============================================
where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python not found. Install Python 3.9+ from https://www.python.org/downloads/
  echo     Remember to tick "Add python.exe to PATH" during installation.
  pause
  exit /b 1
)
python tools\\doctor.py --fix
if errorlevel 1 (
  echo.
  echo [!] Environment check did not pass. Fix the items above and run again.
  pause
  exit /b 1
)
echo.
echo Starting application, the browser will open automatically...
python -m app.server
if errorlevel 1 pause
"""

SH = """#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
echo "============================================"
echo "  Video Extractor"
echo "============================================"
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "[X] 需要 Python 3.9+，先安装：https://www.python.org/downloads/"
  exit 1
fi
$PY tools/doctor.py --fix || { echo "[!] 环境检查未通过"; exit 1; }
echo
echo "启动应用，浏览器会自动打开…"
exec $PY -m app.server
"""

ENV_EXAMPLE = """# 复制成 .env 后填自己的值（或直接在系统里设同名环境变量）。
# 密钥只从环境变量读，不会写进配置文件，也不会随结果一起保存。

# OpenAI 兼容端点
LLM_BASE_URL=https://your-endpoint/v1
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx

# 用阿里云百炼时改为（配置文件里 provider 也要改成 dashscope）：
# DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxx
"""


def _clean_dist():
    """只建目录，不删历史产物。

    一轮删几十个文件会撞上环境的批量删除保护（实测 50 个即触发），
    删除失败后再打包会拿到半新半旧的内容。产物名带版本号，旧包留着不碍事。
    """
    DIST.mkdir(parents=True, exist_ok=True)


def _iter_files():
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if any(part in SKIP_NAMES for part in p.parts):
                continue
            if p.is_dir():
                continue
            rel = p.relative_to(ROOT).as_posix()
            if rel in SKIP_FILES:
                continue
            # 本地提示词覆盖不随包发布，保留说明文件
            if rel.startswith("config/prompts.local/") and not rel.endswith("README.md"):
                continue
            yield p, rel
    for name in ("README.md", "requirements.txt"):
        p = ROOT / name
        if p.exists():
            yield p, name


def _write_common(target: Path):
    """两种包都有的东西：源码、配置、启动脚本、说明。"""
    for src, rel in _iter_files():
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    (target / "start.bat").write_text(BAT, encoding="utf-8", newline="\r\n")
    for name in ("start.command", "start.sh"):
        p = target / name
        p.write_text(SH, encoding="utf-8", newline="\n")
        p.chmod(0o755)
    (target / ".env.example").write_text(ENV_EXAMPLE, encoding="utf-8")
    (target / "bin").mkdir(exist_ok=True)
    (target / "bin" / "README.txt").write_text(
        "ffmpeg 放在这里。启动脚本会自动检测：没有就先找系统 PATH，再没有就自动下载到本目录。\n"
        "手动安装也行：把 ffmpeg 与 ffprobe 两个可执行文件直接放进来。\n",
        encoding="utf-8")


def _zip(src_dir: Path, out: Path):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in src_dir.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(src_dir.parent).as_posix())
    return out


def build_lite(version: str) -> Path:
    name = f"VideoExtractor-{version}-lite"
    target = DIST / name
    target.mkdir(parents=True, exist_ok=True)
    _write_common(target)
    out = DIST / f"{name}.zip"
    _zip(target, out)
    return out


def _need_ffmpeg() -> bool:
    return not ((ROOT / "bin" / "ffmpeg.exe").exists() or (ROOT / "bin" / "ffmpeg").exists())


def _ensure_bin_ffmpeg():
    """完整包要自带 ffmpeg。项目 bin/ 里没有就下载 essentials 版。"""
    if not _need_ffmpeg():
        return
    print("   完整包需要内置 ffmpeg，正在准备…")
    import tools.doctor as doc
    doc._install_ffmpeg()


def build_full(version: str) -> Path:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit("完整包需要 PyInstaller：pip install pyinstaller")
    _ensure_bin_ffmpeg()

    stamp = time.strftime("%m%d%H%M%S")
    work = DIST / f"_build_{version}_{stamp}"
    pyout = DIST / f"_py_{version}_{stamp}"
    name = f"VideoExtractor-{version}-full"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", "VideoExtractor", "--onedir", "--console",
        "--distpath", str(pyout), "--workpath", str(work),
        "--specpath", str(work),
        # 静态资源跟着代码走；config 不打包，放 exe 旁边让用户能改
        "--add-data", f"{ROOT / 'app' / 'web'}{os.pathsep}app/web",
        "--add-data", f"{ROOT / 'tools' / 'render_report.py'}{os.pathsep}tools",
        "--hidden-import", "src.prompt_guard",
        str(ROOT / "app" / "launcher.py"),
    ]
    print("   正在打包可执行文件（首次较慢）…")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit("PyInstaller 打包失败，见上方输出")

    target = DIST / name
    if target.exists():   # 不删，改名留档，避免批量删除
        target.rename(DIST / f"{name}.old_{stamp}")
    shutil.move(str(pyout / "VideoExtractor"), str(target))

    # config 与 bin 放 exe 旁边
    _write_common(target)
    exe_suffix = ".exe" if os.name == "nt" else ""
    for f in target.glob(f"VideoExtractor{exe_suffix}"):
        pass
    # 启动脚本改成直接跑 exe
    (target / "start.bat").write_text(
        "@echo off\r\nchcp 65001 >nul\r\ncd /d \"%~dp0\"\r\n"
        "echo Starting...\r\nVideoExtractor.exe\r\nif errorlevel 1 pause\r\n",
        encoding="utf-8", newline="\r\n")
    sh = ("#!/usr/bin/env bash\ncd \"$(dirname \"$0\")\"\n"
          "./VideoExtractor\n")
    p = target / "start.command"
    p.write_text(sh, encoding="utf-8")
    p.chmod(0o755)

    for f in ("ffmpeg.exe", "ffprobe.exe", "ffmpeg", "ffprobe"):
        src = ROOT / "bin" / f
        if src.exists():
            shutil.copy2(src, target / "bin" / f)

    out = DIST / f"{name}.zip"
    _zip(target, out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="打发行包")
    ap.add_argument("--flavor", default="lite", choices=["lite", "full", "both"])
    ap.add_argument("--version", default="1.0.0")
    args = ap.parse_args()

    _clean_dist()
    outs = []
    if args.flavor in ("lite", "both"):
        print("打包 lite（源码包）…")
        outs.append(build_lite(args.version))
    if args.flavor in ("full", "both"):
        print("打包 full（含运行时）…")
        outs.append(build_full(args.version))

    print("\n产物：")
    for o in outs:
        size = o.stat().st_size / 1e6
        print(f"  {o}  ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
