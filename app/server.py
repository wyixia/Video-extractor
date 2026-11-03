"""本地 Web 应用服务端（标准库，零第三方依赖）。

为什么用本地 Web 而不是原生窗口：报告本身就是 HTML，界面直接嵌进去，
视频、分镜表、复核提示可以在同一屏对照；换主题、加交互都不用重编译界面代码。

服务只监听 127.0.0.1，随机端口，启动后自动打开浏览器。
"""
import base64
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CODE = Path(__file__).resolve().parent.parent   # 代码与静态资源所在位置
sys.path.insert(0, str(CODE))

from src.config import load_config, prompt_paths, resolve_path  # noqa: E402
from src.env import project_root  # noqa: E402
from src.export import frontend_payload, write_json  # noqa: E402
from src.orchestrator import Pipeline  # noqa: E402
from src.prompt_guard import check_prompt  # noqa: E402

# 用户数据目录：源码态是项目根，打包后是 exe 所在目录（配置要能改）
DATA = project_root()
# 静态资源跟着代码走：打包后会被解到临时目录，不能和 DATA 混为一谈
WEB = Path(__file__).resolve().parent / "web"
STATE = DATA / "_work" / "app_state.json"
CFG_DEFAULT = "config/config.compat.yaml"


class Job:
    """同一时间只跑一个任务：多任务并发会互相抢接口配额，结果反而更慢。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.id = ""
            self.video = ""
            self.status = "idle"        # idle | running | done | error
            self.stage = ""
            self.progress = 0.0
            self.detail = ""
            self.log = []
            self.error = ""
            self.started = 0.0
            self.ended = 0.0
            self.form_path = ""
            self.full_path = ""
            self.report_path = ""
            self.shots = 0

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id, "video": self.video, "status": self.status,
                "stage": self.stage, "progress": self.progress, "detail": self.detail,
                "error": self.error, "started": self.started, "ended": self.ended,
                "shots": self.shots, "elapsed": round((self.ended or time.time()) - self.started, 1)
                if self.started else 0,
                "has_report": bool(self.report_path and Path(self.report_path).exists()),
                "has_form": bool(self.form_path and Path(self.form_path).exists()),
                "log": self.log[-200:],
            }

    def push(self, line: str):
        with self.lock:
            self.log.append(line.rstrip())
            if len(self.log) > 800:
                del self.log[:300]

    def event(self, e: dict):
        with self.lock:
            self.stage = e.get("stage", self.stage)
            self.progress = float(e.get("progress", self.progress))
            self.detail = e.get("detail", self.detail)


class _Tee(io.TextIOBase):
    """把主程序的 print 同时收进任务日志，界面能看见真实流程。"""

    def __init__(self, job: Job, echo=None):
        self.job, self.echo = job, echo

    def write(self, s):
        if s.strip():
            self.job.push(s)
        if self.echo:
            try:
                self.echo.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        if self.echo:
            try:
                self.echo.flush()
            except Exception:
                pass


def _save_local_config(values: dict) -> Path:
    """用户改的配置写到 config/config.local.yaml，主配置不动。

    叠加而不是覆盖：升级时主配置被替换，用户的模型选择仍然生效；
    出问题删掉这个文件就回到默认。
    """
    p = DATA / "config" / "config.local.yaml"
    lines = ["# 由应用界面写入，覆盖 config.compat.yaml 中的同名项。删掉即恢复默认。"]
    models = {k: v for k, v in (values.get("models") or {}).items() if v}
    if models:
        lines.append("models:")
        for k, v in models.items():
            lines.append(f"  {k}: {v}")
    rt = {k: v for k, v in (values.get("runtime") or {}).items() if v not in (None, "")}
    if rt:
        lines.append("runtime:")
        for k, v in rt.items():
            lines.append(f"  {k}: {v}")
    if values.get("speech_assign"):
        lines.append("speech:")
        lines.append(f"  assign: {values['speech_assign']}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _pick_video_dialog() -> str:
    """系统文件对话框。tkinter 在请求线程里创建，串行调用不会冲突。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="选择视频",
            filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("全部文件", "*.*")],
        )
        root.destroy()
        return path or ""
    except Exception as e:
        raise RuntimeError(f"打不开文件对话框：{type(e).__name__}: {e}")


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoExtractor/1.0"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass  # 不往控制台刷访问日志，控制台留给任务日志

    def _send(self, code=200, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._json(self.server.job.snapshot())
        if u.path == "/api/config":
            return self._json(self._config_view())
        if u.path == "/api/prompt":
            return self._json(self._prompt_view(q.get("name", ["vl"])[0]))
        if u.path == "/api/report":
            return self._report()
        if u.path == "/api/media":
            return self._media()
        if u.path == "/api/form":
            return self._form()
        if u.path == "/api/log":
            return self._send(200, "\n".join(self.server.job.snapshot()["log"]).encode(),
                              "text/plain; charset=utf-8")
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/pick-video":
            try:
                p = _pick_video_dialog()
                return self._json({"path": p})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if u.path == "/api/start":
            return self._start(self._body())
        if u.path == "/api/config":
            return self._save_config(self._body())
        if u.path == "/api/prompt":
            return self._save_prompt(self._body())
        if u.path == "/api/prompt-check":
            name = (self._body() or {}).get("name", "vl")
            return self._json({"name": name, "problems": check_prompt(name)})
        return self._json({"error": "not found"}, 404)

    def do_PUT(self):
        """拖拽上传：body 直接是文件字节，比 multipart 解析简单。"""
        u = urlparse(self.path)
        if u.path != "/api/upload":
            return self._json({"error": "not found"}, 404)
        q = parse_qs(u.query)
        name = Path(q.get("name", ["video.mp4"])[0]).name
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return self._json({"error": "空文件"}, 400)
        dst_dir = DATA / "_work" / "uploads"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / name
        left = n
        with open(dst, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        return self._json({"path": str(dst)})

    # ---------- 具体处理 ----------
    def _config_view(self) -> dict:
        try:
            cfg = load_config(CFG_DEFAULT)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        local = DATA / "config" / "config.local.yaml"
        return {
            "models": cfg.get("models") or {},
            "runtime": {k: cfg.get("runtime", {}).get(k) for k in
                        ("max_workers", "vl_max_tokens", "retry")},
            "speech_assign": (cfg.get("speech") or {}).get("assign", "whole"),
            "shot": {k: cfg.get("preprocess", {}).get("shot_split", {}).get(k)
                     for k in ("scene_threshold", "min_shot_sec", "max_shot_sec")},
            "base_url": os.getenv("LLM_BASE_URL") or (cfg.get("compat") or {}).get("base_url", ""),
            "has_api_key": bool(os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")),
            "local_file": str(local) if local.exists() else "",
        }

    def _save_config(self, body: dict):
        try:
            p = _save_local_config(body)
            return self._json({"ok": True, "file": str(p)})
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _prompt_view(self, name: str) -> dict:
        paths = prompt_paths(name)
        local, builtin = paths[0], paths[1]
        src = "local" if local.exists() else "builtin"
        p = local if local.exists() else builtin
        if not p.exists():
            return {"error": f"没有找到 {name}.txt"}
        return {"name": name, "source": src, "text": p.read_text(encoding="utf-8"),
                "builtin": builtin.read_text(encoding="utf-8") if builtin.exists() else "",
                "local_path": str(local)}

    def _save_prompt(self, body: dict):
        name = (body.get("name") or "").strip()
        text = body.get("text")
        if not name or not isinstance(text, str):
            return self._json({"error": "缺少 name 或 text"}, 400)
        p = prompt_paths(name)[0]
        try:
            if body.get("reset"):
                if p.exists():
                    p.unlink()
                return self._json({"ok": True, "source": "builtin"})
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            return self._json({"ok": True, "source": "local", "problems": check_prompt(name)})
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _start(self, body: dict):
        job: Job = self.server.job
        if job.snapshot()["status"] == "running":
            return self._json({"error": "已有任务在跑，等它结束再开新的"}, 409)
        video = (body.get("video") or "").strip().strip('"')
        if not video:
            return self._json({"error": "没有选择视频"}, 400)
        if not Path(video).exists():
            return self._json({"error": f"文件不存在：{video}"}, 400)
        if (body.get("config") or {}).get("models"):
            _save_local_config(body["config"])
        t = threading.Thread(target=_run_job,
                             args=(self.server.job, video, body, getattr(self.server, "provider", "")),
                             daemon=True)
        job.reset()
        with job.lock:
            job.id = time.strftime("%Y%m%d%H%M%S")
            job.video = video
            job.status = "running"
            job.started = time.time()
        t.start()
        return self._json({"ok": True, "id": job.id})

    def _report(self):
        job: Job = self.server.job
        p = job.report_path
        if not p or not Path(p).exists():
            return self._send(404, b"<h1>\xe8\xbf\x98\xe6\xb2\xa1\xe6\x9c\x89\xe6\x8a\xa5\xe5\x91\x8a</h1>",
                              "text/html; charset=utf-8")
        return self._send(200, Path(p).read_bytes(), "text/html; charset=utf-8")

    def _form(self):
        job: Job = self.server.job
        p = job.form_path
        if not p or not Path(p).exists():
            return self._json({"error": "还没有结果"}, 404)
        data = Path(p).read_bytes()
        # 文件名可能含中文：HTTP 头只能放 ASCII，必须按 RFC 5987 编码，
        # 直接塞中文会让服务端在写响应头时抛异常，表现为连接被重置、下载 0 字节。
        from urllib.parse import quote
        name = quote(Path(p).name, safe="")
        self._send(200, data, "application/json; charset=utf-8", {
            "Content-Disposition": f"attachment; filename*=UTF-8''{name}"})
        return None

    def _media(self):
        """视频流。支持 Range，否则浏览器不能拖动进度条。"""
        job: Job = self.server.job
        src = job.video
        if not src or not Path(src).exists():
            return self._json({"error": "没有视频"}, 404)
        path = Path(src)
        size = path.stat().st_size
        ctype = mimetypes.guess_type(str(path))[0] or "video/mp4"
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if not m:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    self.wfile.write(b)
            return None
        start = int(m.group(1) or 0)
        end = int(m.group(2) or size - 1)
        end = min(end, size - 1)
        length = max(0, end - start + 1)
        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                b = f.read(min(1 << 20, left))
                if not b:
                    break
                self.wfile.write(b)
                left -= len(b)
        return None


def _run_job(job: Job, video: str, body: dict, provider: str = ""):
    """独立线程里跑：任务失败只改状态，不让服务端崩。"""
    tee = _Tee(job)
    try:
        cfg = load_config(CFG_DEFAULT)
        overrides = {}
        if provider:
            overrides["provider"] = provider
        if body.get("speech_assign"):
            overrides["speech"] = {"assign": body["speech_assign"]}
        if overrides:
            from src.config import _deep_update
            _deep_update(cfg, overrides)

        with redirect_stdout(tee):
            pipe = Pipeline(cfg, on_event=job.event)
            final = pipe.run(video, body.get("subtask_id", ""))

        out_dir = resolve_path(cfg["paths"]["output"])
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(video).stem
        ts = time.strftime("%m%d-%H%M%S")
        form = out_dir / f"{stem}_{ts}_form.json"
        full = out_dir / f"{stem}_{ts}_full.json"
        write_json(str(form), frontend_payload(final))
        from src.config import prompt_sources as _ps
        final["prompt_sources"] = _ps()
        write_json(str(full), final)

        with job.lock:
            job.form_path, job.full_path = str(form), str(full)
            job.shots = len(final.get("shots") or [])

        # 报告：复用命令行那套渲染，界面里直接嵌
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "render_report", CODE / "tools" / "render_report.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            report = out_dir / f"{stem}_{ts}_report.html"
            with redirect_stdout(tee):
                mod.build(final, str(report))
            with job.lock:
                job.report_path = str(report)
        except Exception as e:
            job.push(f"[警告] 报告渲染失败：{type(e).__name__}: {e}")

        with job.lock:
            job.status = "done"
            job.stage = "done"
            job.progress = 1.0
            job.ended = time.time()
            job.detail = f"{job.shots} 个镜头，用时 {job.ended - job.started:.0f} 秒"
    except Exception as e:
        import traceback
        job.push(traceback.format_exc())
        with job.lock:
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            job.ended = time.time()
    finally:
        _save_history(job)


def _save_history(job: Job):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        hist = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else []
        if job.video:
            hist = [h for h in hist if h.get("video") != job.video]
            hist.insert(0, {"video": job.video, "at": time.strftime("%Y-%m-%d %H:%M"),
                            "report": job.report_path, "form": job.form_path,
                            "shots": job.shots, "status": job.status})
        STATE.write_text(json.dumps(hist[:20], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def serve(port: int = 0, open_browser: bool = True, provider: str = ""):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.job = Job()
    srv.provider = provider
    real = srv.server_address[1]
    url = f"http://127.0.0.1:{real}/"
    print(f"[应用] {url}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[应用] 已退出")
    finally:
        srv.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="视频分镜标注 · 本地应用")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--provider", default="", choices=["", "compat", "dashscope", "mock"],
                    help="覆盖配置的 provider；mock 用假数据跑通界面，不联网")
    a = ap.parse_args()
    serve(a.port, not a.no_browser, a.provider)
