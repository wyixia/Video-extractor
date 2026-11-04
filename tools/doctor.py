"""环境体检与自动修复。

打包后的程序要能在别人的机器上跑起来，缺什么得说清楚、能补就自动补。
检查 Python 版本、依赖包、ffmpeg/ffprobe、配置文件、提示词、API 凭据；
--fix 时自动装缺失的 pip 包，Windows 上还会下载 ffmpeg 到 bin/。

用法：
  python tools/doctor.py            # 只检查，打印报告
  python tools/doctor.py --fix      # 缺什么装什么
  python tools/doctor.py --fix --no-download   # 只装 pip 包，不下载 ffmpeg
"""
import argparse
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY_MIN = (3, 9)
# 依赖不是一概必需：compat 端点走标准库 urllib，零第三方依赖也能跑；
# 只有 dashscope 客户端要用 openai/requests。PyYAML 缺了会退化成简化解析器。
DS_DEPS = [("openai", "openai>=1.52.0"), ("requests", "requests>=2.31.0")]
NICE_DEPS = [("yaml", "PyYAML>=6.0"), ("webview", "pywebview>=5.0")]

FFMPEG_WIN = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_LINUX = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"

OK, BAD, WARN = "√", "×", "!"


class Report:
    def __init__(self):
        self.items = []
        self.failed = False

    def add(self, mark, title, detail="", fixable=False):
        self.items.append((mark, title, detail, fixable))
        if mark == BAD:
            self.failed = True

    def render(self):
        width = max(len(t) for _, t, _, _ in self.items) + 2
        for mark, title, detail, _ in self.items:
            print(f" {mark} {title.ljust(width)}{('：' + detail) if detail else ''}")


def _python_ok(rep: Report):
    v = sys.version_info
    cur = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= PY_MIN:
        rep.add(OK, "Python", cur + f"（{platform.system()}）")
        return
    rep.add(BAD, "Python", f"{cur}，需要 {PY_MIN[0]}.{PY_MIN[1]} 及以上")


def _needs_dashscope(cfg) -> bool:
    """只要有任意一个端点走 dashscope，就得装 openai/requests。"""
    try:
        from src.clients import resolve_endpoints
        return any((e.get("type") or "").lower() == "dashscope"
                   for e in resolve_endpoints(cfg or {}).values())
    except Exception:
        return str((cfg or {}).get("provider", "")).lower() == "dashscope"


def _deps(rep: Report, fix: bool, provider: str):
    need = DS_DEPS if provider == "dashscope" else []
    missing_need = [pkg for mod, pkg in need if importlib.util.find_spec(mod) is None]
    missing_nice = [pkg for mod, pkg in NICE_DEPS if importlib.util.find_spec(mod) is None]

    if not missing_need and not missing_nice:
        rep.add(OK, "Python 依赖", "齐全" if need else "无需第三方包（标准库直连）")
        return
    if not fix:
        if missing_need:
            rep.add(BAD, "Python 依赖", f"缺 {'、'.join(missing_need)}（加 --fix 自动安装）", True)
        if missing_nice:
            rep.add(WARN, "可选依赖", f"缺 {'、'.join(missing_nice)}"
                                       f"（PyYAML 影响配置解析，pywebview 影响原生窗口；加 --fix 装）")
        return
    todo = missing_need + missing_nice
    print(f"   正在安装：{'、'.join(todo)} …")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "--quiet", *todo])
    still_need = [pkg for mod, pkg in need if importlib.util.find_spec(mod) is None]
    still_nice = [pkg for mod, pkg in NICE_DEPS if importlib.util.find_spec(mod) is None]
    if r.returncode == 0 and not still_need:
        rep.add(OK, "Python 依赖", f"已安装 {'、'.join(todo)}")
    elif still_need:
        rep.add(BAD, "Python 依赖", f"安装失败，请手动执行 pip install {' '.join(still_need)}")
    else:
        rep.add(OK, "Python 依赖", f"必需项已装；可选项仍缺 {'、'.join(still_nice)}")


def _ffmpeg(rep: Report, fix: bool, allow_download: bool):
    from src.env import ffmpeg_bin, ffprobe_bin
    ff, fp = ffmpeg_bin(), ffprobe_bin()
    have_ff = shutil.which(ff) or Path(ff).exists()
    have_fp = shutil.which(fp) or Path(fp).exists()
    bundled = Path(ff).resolve().parent == (ROOT / "bin").resolve()
    where = "项目内置" if bundled else "系统 PATH"

    if have_ff and have_fp:
        rep.add(OK, "ffmpeg / ffprobe", f"{where} · {ff}")
        return
    if have_ff and not have_fp:
        # 完整包只带 ffmpeg：ffprobe 的活可以用 ffmpeg -i 的输出来顶，
        # 少了它不影响运行，只是读元数据的方式不同。
        rep.add(OK, "ffmpeg", f"{where}（未装 ffprobe，改用 ffmpeg 读时长与分辨率）")
        return
    if not fix:
        rep.add(BAD, "ffmpeg / ffprobe", "未找到（加 --fix 自动下载，或手动安装）", True)
        return
    if not allow_download:
        rep.add(BAD, "ffmpeg / ffprobe", "未找到，且已禁用下载")
        return
    print("   正在安装 ffmpeg（首次需下载，几十 MB）…")
    try:
        got = _install_ffmpeg()
        tail = "" if len(got) > 1 else "（只有 ffmpeg，未带 ffprobe）"
        rep.add(OK, "ffmpeg / ffprobe", f"已安装到 {ROOT / 'bin'}{tail}")
    except Exception as e:
        rep.add(BAD, "ffmpeg / ffprobe", f"自动安装失败：{type(e).__name__}: {str(e)[:120]}")
        rep.add(WARN, "手动安装", _manual_hint())


def _manual_hint() -> str:
    s = platform.system()
    if s == "Windows":
        return "下载 gyan.dev 的 essentials 版，把 bin\\ffmpeg.exe 与 ffprobe.exe 放进本程序的 bin 目录"
    if s == "Darwin":
        return "brew install ffmpeg"
    return "sudo apt install ffmpeg  （或把静态版解压到本程序的 bin 目录）"


def _install_ffmpeg() -> list:
    """装 ffmpeg 到 bin/。先试 wheel（快），再退回官方压缩包（慢但完整）。

    gyan.dev 走国际线路，实测只有几 KB/s，国内用户等不起；
    imageio-ffmpeg 的 wheel 走 PyPI 镜像，几十秒就下完。
    代价是 wheel 里只有 ffmpeg、没有 ffprobe —— 读时长/分辨率已经能用
    `ffmpeg -i` 的输出顶替，所以这个缺口不影响运行。
    """
    bin_dir = ROOT / "bin"
    bin_dir.mkdir(exist_ok=True)
    errors = []
    for name, fn in (("imageio-ffmpeg", _install_ffmpeg_wheel),
                     ("官方压缩包", _install_ffmpeg_archive)):
        try:
            print(f"   尝试用{name}安装…")
            got = fn(bin_dir)
            if got:
                return got
            errors.append(f"{name}：没取到可执行文件")
        except Exception as e:
            errors.append(f"{name}：{type(e).__name__}: {str(e)[:100]}")
    raise RuntimeError("；".join(errors))


def _install_ffmpeg_wheel(bin_dir: Path) -> list:
    """从 PyPI 装 imageio-ffmpeg，把里面的静态 ffmpeg 复制进 bin/。"""
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "--quiet", "--upgrade", "imageio-ffmpeg"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[:160] or "pip 返回非零")

    importlib.invalidate_caches()
    mod = importlib.import_module("imageio_ffmpeg")
    if not hasattr(mod, "get_ffmpeg_exe"):
        raise RuntimeError("装上了但没有 get_ffmpeg_exe")
    src = Path(mod.get_ffmpeg_exe())
    if not src.exists():
        raise RuntimeError(f"找不到可执行文件：{src}")
    dst = bin_dir / ("ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg")
    shutil.copy2(src, dst)
    if platform.system() != "Windows":
        dst.chmod(0o755)
    return [dst.name]


def _install_ffmpeg_archive(bin_dir: Path) -> list:
    """下载官方压缩包并解压到 bin/。只处理 Windows 与 Linux；macOS 建议用 brew。"""
    system = platform.system()
    if system == "Windows":
        url, mode = FFMPEG_WIN, "zip"
    elif system == "Linux":
        url, mode = FFMPEG_LINUX, "tar"
    else:
        raise RuntimeError(f"{system} 不支持自动下载，请用包管理器安装")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        archive = tmp / ("ffmpeg.zip" if mode == "zip" else "ffmpeg.tar.xz")
        urllib.request.urlretrieve(url, archive)  # noqa: S310 固定官方地址
        if mode == "zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp / "x")
        else:
            subprocess.run(["tar", "-xf", str(archive), "-C", str(tmp)], check=True)
            (tmp / "x").mkdir(exist_ok=True)
            for d in tmp.glob("ffmpeg-*-static"):
                shutil.move(str(d), str(tmp / "x" / d.name))

        wanted = ["ffmpeg.exe", "ffprobe.exe"] if system == "Windows" else ["ffmpeg", "ffprobe"]
        copied = []
        for name in wanted:
            hit = next((p for p in (tmp / "x").rglob(name)), None)
            if hit:
                dst = bin_dir / name
                shutil.copy2(hit, dst)
                if system != "Windows":
                    dst.chmod(0o755)
                copied.append(name)
        if not copied:
            raise RuntimeError("压缩包里没找到可执行文件")
        return copied


def _config(rep: Report, path: str):
    p = Path(path)
    p = p if p.is_absolute() else ROOT / p
    if not p.exists():
        rep.add(BAD, "配置文件", f"{p} 不存在")
        return None
    try:
        from src.config import load_config
        cfg = load_config(str(p))
        rep.add(OK, "配置文件", f"{p.name} · provider={cfg.get('provider')} · "
                                f"模型 {cfg.get('models', {}).get('vl', '?')}")
        return cfg
    except Exception as e:
        rep.add(BAD, "配置文件", f"解析失败：{type(e).__name__}: {str(e)[:100]}")
        return None


def _prompts(rep: Report):
    try:
        from src.prompt_guard import check_all
        rep_ = check_all()
        bad = {k: v["problems"] for k, v in rep_.items() if v["problems"]}
        custom = [k for k, v in rep_.items() if v["source"] == "local"]
        tail = f"，本地覆盖 {'、'.join(custom)}" if custom else ""
        if bad:
            first = next(iter(bad.items()))
            rep.add(BAD, "提示词", f"{first[0]}.txt 缺 {len(first[1])} 项：{first[1][0][:60]}")
        else:
            rep.add(OK, "提示词", f"6 份齐全{tail}")
    except Exception as e:
        rep.add(BAD, "提示词", f"{type(e).__name__}: {str(e)[:100]}")


def _credentials(rep: Report, cfg):
    if not cfg:
        return
    provider = str(cfg.get("provider", "")).lower()
    if provider == "mock":
        rep.add(OK, "API 凭据", "provider=mock，无需凭据")
        return
    # 按端点检查：每个端点一份地址一份密钥，缺哪个说哪个，不要一句"未设置"糊过去
    try:
        from src.clients import endpoint_key, resolve_endpoints
        eps = resolve_endpoints(cfg)
    except Exception as e:
        rep.add(WARN, "API 凭据", f"端点解析失败：{type(e).__name__}: {e}")
        return
    if not eps:
        rep.add(WARN, "API 凭据", "配置里没有端点")
        return
    bad = [n for n, ep in eps.items() if not endpoint_key(ep)]
    if bad:
        rep.add(WARN, "API 凭据",
                f"端点 {'、'.join(bad)} 没有密钥（界面里填，或设对应的环境变量）")
    else:
        rep.add(OK, "API 凭据", f"{len(eps)} 个端点都已配密钥")


def quick_check(config_path: str) -> int:
    """只检查不修复，供 tools/run.py 在启动时调用。返回 0 表示可以开跑。"""
    rep = Report()
    _python_ok(rep)
    cfg = _config(rep, config_path)
    _deps(rep, False, "dashscope" if _needs_dashscope(cfg) else "compat")
    _ffmpeg(rep, False, False)
    _prompts(rep)
    _credentials(rep, cfg)
    rep.render()
    return 0 if not rep.failed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="环境体检")
    ap.add_argument("--fix", action="store_true", help="缺什么装什么")
    ap.add_argument("--no-download", action="store_true", help="不自动下载 ffmpeg")
    ap.add_argument("--config", default="config/config.compat.yaml")
    args = ap.parse_args()

    print("环境体检")
    print("-" * 46)
    rep = Report()
    _python_ok(rep)
    cfg = _config(rep, args.config)
    provider = "dashscope" if _needs_dashscope(cfg) else "compat"
    _deps(rep, args.fix, provider)
    _ffmpeg(rep, args.fix, not args.no_download)
    _prompts(rep)
    _credentials(rep, cfg)
    rep.render()
    print("-" * 46)

    if rep.failed:
        print("有项目未通过。按上面的提示处理后重跑；--fix 可自动修复大部分。")
        return 1
    print("环境可用。模型是否真的能干活，用 --check-models 实测一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
