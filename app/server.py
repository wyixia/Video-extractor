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

from src.clients import (default_endpoint_name, endpoint_for_role, endpoint_key,
                         endpoint_key_source, resolve_endpoints, role_endpoint_map)  # noqa: E402
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
            # 中间结果：切完镜先把清单摆出来，之后逐镜回填，界面能边跑边看
            self.plan = []              # [{id,start,end,dur}]
            self.seen = {}              # shot_id -> {id,size,move,content,review,error}
            self.counts = {"asr": 0, "sed": 0, "music": 0}

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
                # 过程结果：已切出的镜头 + 已回填的单镜结论 + 声音侧计数
                "plan": self.plan,
                "seen": list(self.seen.values()),
                "counts": dict(self.counts),
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
            if e.get("shots"):
                self.plan = list(e["shots"])
            shot = e.get("shot")
            if isinstance(shot, dict) and shot.get("id"):
                self.seen[shot["id"]] = shot     # 同一镜补跑会覆盖，不重复
            if isinstance(e.get("audio"), dict):
                for k, v in e["audio"].items():
                    self.counts[k] = int(v or 0)


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


def _yaml_str(v) -> str:
    """标量统一用双引号包：API Key 里可能有 : # @ 等字符，裸写会改变 YAML 结构。"""
    return json.dumps(str(v), ensure_ascii=False)


def _save_local_config(values: dict) -> Path:
    """用户改的配置写到 config/config.local.yaml，主配置不动。

    叠加而不是覆盖：升级时主配置被替换，用户的模型选择仍然生效；
    出问题删掉这个文件就回到默认。唯独 endpoints 例外——它是整体替换的，
    否则界面上删掉的端点会在下次启动时又冒出来。
    """
    p = DATA / "config" / "config.local.yaml"
    old = {}
    if p.exists():
        try:
            old = load_config(str(p))
        except Exception:
            old = {}
    old_eps = dict(old.get("endpoints") or {})
    # 老版本把密钥写在 compat / dashscope 段，这里接住，免得用户重填一次
    if not old_eps:
        for seg in ("compat", "dashscope"):
            s = old.get(seg) or {}
            if s.get("api_key"):
                old_eps["default"] = {"api_key": s["api_key"]}
                break

    eps_in = values.get("endpoints")
    if eps_in is None and (values.get("api_key") or "").strip():
        # 只传了一个总密钥：当作 default 端点的密钥
        eps_in = {"default": {"api_key": values["api_key"]}}

    lines = [
        "# 由应用界面写入，覆盖 config.compat.yaml 中的同名项。删掉即恢复默认。",
        "# 密钥存在这里，已在 .gitignore 里排除；环境变量优先于这里的值。",
    ]

    models = {k: v for k, v in (values.get("models") or {}).items() if v}
    if models:
        lines.append("models:")
        for k, v in models.items():
            lines.append(f"  {k}: {_yaml_str(v)}")

    if eps_in is not None:
        eps_in = {str(k): dict(v or {}) for k, v in eps_in.items() if str(k).strip()}
        if not eps_in:
            lines.append("endpoints: {}")   # 显式清空，不然会回落到主配置的端点
        else:
            lines.append("endpoints:")
            for name, ep in eps_in.items():
                lines.append(f"  {name}:")
                key = (ep.pop("api_key", "") or "").strip()
                if not key:
                    # 留空表示"不修改"：沿用上次保存的值，不能因为没重填就清掉
                    key = str((old_eps.get(name) or {}).get("api_key") or "")
                for f in ("type", "base_url", "asr_base_url", "base_url_env", "api_key_env"):
                    if ep.get(f):
                        lines.append(f"    {f}: {_yaml_str(ep[f])}")
                if key:
                    lines.append(f"    api_key: {_yaml_str(key)}")

    if values.get("role_endpoints") is not None:
        re_ = {k: v for k, v in (values["role_endpoints"] or {}).items() if v}
        if re_:
            lines.append("role_endpoints:")
            for k, v in re_.items():
                lines.append(f"  {k}: {_yaml_str(v)}")
        else:
            lines.append("role_endpoints: {}")

    rt = {k: v for k, v in (values.get("runtime") or {}).items() if v not in (None, "")}
    if rt:
        lines.append("runtime:")
        for k, v in rt.items():
            lines.append(f"  {k}: {v}")

    if values.get("speech_assign"):
        lines.append("speech:")
        lines.append(f"  assign: {values['speech_assign']}")

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _pick_video_dialog() -> dict:
    """系统文件对话框。返回 {"available": bool, "path": str, "reason": str}。

    不用异常表达"这台机器没有对话框"：tkinter 不是标准库必装项，
    精简版 Python、打包运行时、Linux 上没装 python3-tk 都会缺，
    这属于正常情况，前端应当退回浏览器自带的文件选择而不是弹错误。
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="选择视频",
            filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("全部文件", "*.*")],
        )
        root.destroy()
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}
    return {"available": True, "path": path or ""}


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
        if u.path == "/api/history":
            return self._json(self._history())
        if u.path == "/api/history/report":
            return self._hist_report(q)
        if u.path == "/api/history/form":
            return self._hist_form(q)
        if u.path == "/api/history/media":
            return self._hist_media(q)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/pick-video":
            return self._json(_pick_video_dialog())
        if u.path == "/api/start":
            return self._start(self._body())
        if u.path == "/api/config":
            return self._save_config(self._body())
        if u.path == "/api/prompt":
            return self._save_prompt(self._body())
        if u.path == "/api/check-models":
            return self._check_models(self._body())
        if u.path == "/api/ping":
            self.server.last_seen = time.time()
            return self._json({"ok": True})
        if u.path == "/api/quit":
            return self._quit()
        if u.path == "/api/prompt-check":
            name = (self._body() or {}).get("name", "vl")
            return self._json({"name": name, "problems": check_prompt(name)})
        if u.path == "/api/history/delete":
            return self._hist_delete(self._body())
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
        eps = resolve_endpoints(cfg)
        default_ep = default_endpoint_name(cfg)
        rmap = role_endpoint_map(cfg)
        view_eps = []
        for name, ep in eps.items():
            key = endpoint_key(ep)
            view_eps.append({
                "name": name,
                "type": ep.get("type") or "compat",
                "base_url": ep.get("base_url", ""),
                "base_url_env": ep.get("base_url_env", ""),
                "asr_base_url": ep.get("asr_base_url", ""),
                "api_key_env": ep.get("api_key_env", ""),
                "has_key": bool(key),
                "key_source": endpoint_key_source(ep),
                "key_hint": f"…{key[-4:]}" if key else "",
                "is_default": name == default_ep,
            })
        d = eps.get(default_ep) or {}
        return {
            "models": cfg.get("models") or {},
            # 端点池：每个端点一份地址一份密钥，界面按这个渲染
            "endpoints": view_eps,
            "role_endpoints": {r: endpoint_for_role(cfg, r) for r in
                               ("asr", "sed", "music", "vl", "merge", "calibrate")},
            "default_endpoint": default_ep,
            "runtime": {k: cfg.get("runtime", {}).get(k) for k in
                        ("max_workers", "vl_max_tokens", "retry")},
            "speech_assign": (cfg.get("speech") or {}).get("assign", "whole"),
            "shot": {k: cfg.get("preprocess", {}).get("shot_split", {}).get(k)
                     for k in ("scene_threshold", "min_shot_sec", "max_shot_sec")},
            "base_url": d.get("base_url", ""),
            "provider": getattr(self.server, "provider", "") or cfg.get("provider", ""),
            # 下面三项是"默认端点"的汇总，给旧前端和顶部状态条用
            "has_api_key": any(e["has_key"] for e in view_eps),
            # 具体是哪个端点缺密钥：只报"未配置"用户不知道去改哪一条
            "missing_key_endpoints": [e["name"] for e in view_eps if not e["has_key"]],
            "api_key_source": next((e["key_source"] for e in view_eps
                                    if e["is_default"] and e["key_source"]), ""),
            "api_key_hint": next((e["key_hint"] for e in view_eps if e["is_default"]), ""),
            "local_file": str(local) if local.exists() else "",
        }

    def _save_config(self, body: dict):
        try:
            p = _save_local_config(body)
            view = self._config_view()
            # 传了密钥就必须真的能读回来：之前的表现正是"提示已保存、实际没存上"，
            # 用户填完一直显示未配置。存不上当场说，别让人自己发现。
            sent = (body.get("api_key") or "").strip()
            if sent and not any((e.get("key_hint") or "").endswith(sent[-4:])
                                for e in view.get("endpoints") or []):
                return self._json({"error": "密钥没能写入配置文件，请检查 config 目录的写权限",
                                   "config": view}, 500)
            return self._json({"ok": True, "file": str(p), "config": view})
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _check_models(self, body: dict):
        """界面上的「测试模型」：真跑一轮，逐个角色给结论。

        不是只看 HTTP 200 —— 音频角色要看到 audio_tokens 才算真听进去了，
        图像角色要能返回约定的 JSON 键。跑一次几十秒，前端要转圈。
        """
        try:
            payload = body.get("config") or body
            if body.get("save", True) and (payload.get("models") or payload.get("api_key")
                                           or payload.get("endpoints")):
                _save_local_config(payload)      # 允许"边测边存"，省一步
            cfg = load_config(CFG_DEFAULT)
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "check_models", CODE / "tools" / "check_models.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            results, fatal = mod.collect(cfg)
            bad = [r for r in results if r.get("status") == getattr(mod, "FAIL", "不可用")]
            return self._json({"fatal": fatal, "results": results,
                               "bad": len(bad),
                               "ok": bool(results) and not bad and not fatal})
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _quit(self):
        """显式退出。任务在跑时拒绝，避免把用户正在跑的活掐掉。"""
        if self.server.job.snapshot()["status"] == "running":
            return self._json({"error": "任务还在跑，等它结束再关"}, 409)
        # 桌面模式下由窗口接管：关窗口比停服务更符合直觉
        on_quit = getattr(self.server, "on_quit", None)
        self._json({"ok": True})
        if on_quit:
            threading.Thread(target=on_quit, daemon=True).start()
        else:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        return None

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
        # 缺密钥就别开跑了：跑下去要先把视频切镜、抽帧、抽音轨，几分钟后才在调用模型时失败
        view = self._config_view()
        if view.get("provider") != "mock":
            missing = set(view.get("missing_key_endpoints") or [])
            if missing:
                # 只拦真正会用到的端点：没启用的技能不必配密钥。
                # 按角色查，比"总有没有密钥"准：音频有密钥不代表视觉那家也配好了。
                try:
                    cfg = load_config(CFG_DEFAULT)
                    sk = cfg.get("skills") or {}
                    roles = [r for r in ("asr", "sed", "music", "vl") if (sk.get(r) or {}).get("enabled", True)]
                    roles += ["merge", "calibrate"]
                    used = {endpoint_for_role(cfg, r) for r in roles}
                except Exception:
                    used = missing
                need = sorted(missing & used)
                if need:
                    return self._json({
                        "error": f"端点「{'、'.join(need)}」还没配 Key。"
                                 f"右侧「配置 → 端点」里，对应的那一行填一个密钥。"}, 400)
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

    def _report(self, path: str = ""):
        job: Job = self.server.job
        p = path or job.report_path
        if not p or not Path(p).exists():
            return self._send(404, b"<h1>\xe8\xbf\x98\xe6\xb2\xa1\xe6\x9c\x89\xe6\x8a\xa5\xe5\x91\x8a</h1>",
                              "text/html; charset=utf-8")
        return self._send(200, Path(p).read_bytes(), "text/html; charset=utf-8")

    def _form(self, path: str = ""):
        job: Job = self.server.job
        p = path or job.form_path
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

    # ---------- 历史分析 ----------
    # 每次跑完都会在 _work/app_state.json 里记一条（最多 20 条）。
    # 这里把它变成可浏览、可删除的列表：报告、原片、表单都能重新打开。
    @staticmethod
    def _hist_raw() -> list:
        try:
            return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else []
        except Exception:
            return []

    def _history(self) -> dict:
        """全部已分析的视频，不只是应用记录的。

        应用只记得自己跑过的那些；输出目录里可能还有命令行或旧版本留下的报告。
        两边合起来按时间倒序，才叫"全部"。
        """
        try:
            out = resolve_path(load_config(CFG_DEFAULT)["paths"]["output"])
        except Exception:
            out = DATA / "output"
        items, known = [], set()
        for h in self._hist_raw():
            rep = Path(h.get("report") or "")
            if not rep.name or not rep.exists():
                continue            # 报告文件被删了就不摆出来，点开会是空页
            known.add(rep.name)
            video = h.get("video") or ""
            form = Path(h.get("form") or "")
            items.append({
                "file": rep.name,
                "at": h.get("at") or "",
                "shots": h.get("shots") or None,
                "status": h.get("status") or "",
                "video": video,
                "video_name": Path(video).name if video else "",
                "has_video": bool(video and Path(video).exists()),
                "has_form": bool(form.name and form.exists()),
                "size": rep.stat().st_size,
                "mtime": rep.stat().st_mtime,
            })
        if out.exists():
            ups = DATA / "_work" / "uploads"
            for p in out.glob("*_report.html"):
                if p.name in known:
                    continue
                stem = p.name[:-len("_report.html")]
                vid = next((q for q in ups.glob(stem + ".*")), None) if ups.exists() else None
                full = p.with_name(stem + "_full.json")
                n = None
                if full.exists() and full.stat().st_size < (4 << 20):
                    try:
                        n = len(json.loads(full.read_text(encoding="utf-8")).get("shots") or [])
                    except Exception:
                        n = None
                items.append({
                    "file": p.name, "at": "",
                    "shots": n, "status": "done",
                    "video": str(vid) if vid else "",
                    "video_name": vid.name if vid else "",
                    "has_video": bool(vid),
                    "has_form": p.with_name(stem + "_form.json").exists(),
                    "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime,
                })
        items.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
        for x in items:
            x.pop("mtime", None)
            if x["at"]:
                continue
            # 文件名里带 MMDD-HHMMSS，命令行或旧版本留下的记录靠它还原时间
            m = re.search(r"_(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})_report\.html$", x["file"])
            x["at"] = (f"{time.strftime('%Y')}-{m.group(1)}-{m.group(2)} "
                       f"{m.group(3)}:{m.group(4)}") if m else ""
        return {"items": items}

    def _hist_entry(self, name: str):
        name = Path(name or "").name
        if not name:
            return None
        return next((h for h in self._hist_raw()
                     if Path(h.get("report") or "").name == name), None)

    def _hist_report(self, q):
        p = Path((self._hist_entry((q.get("f") or [""])[0]) or {}).get("report") or "")
        if not p.name or not p.exists():
            return self._send(404, b"<h1>\xe6\x8a\xa5\xe5\x91\x8a\xe4\xb8\x8d\xe5\xad\x98\xe5\x9c\xa8</h1>",
                              "text/html; charset=utf-8")
        return self._report(str(p))

    def _hist_form(self, q):
        h = self._hist_entry((q.get("f") or [""])[0]) or {}
        p = Path(h.get("form") or "")
        if not p.name or not p.exists():
            return self._json({"error": "这次运行没有留下表单文件"}, 404)
        return self._form(str(p))

    def _hist_media(self, q):
        h = self._hist_entry((q.get("f") or [""])[0]) or {}
        video = h.get("video") or ""
        if not video or not Path(video).exists():
            return self._json({"error": "原视频已经不在了（可能被移动或删除）"}, 404)
        return self._media(video)

    def _hist_delete(self, body: dict):
        """删一条历史，连同这次运行生成的报告/表单/完整 JSON。

        不动原视频：那可能是用户自己的文件，只是被引用，删了会误伤。
        """
        name = Path((body.get("f") or "")).name
        if not name:
            return self._json({"error": "缺少 f 参数"}, 400)
        entries = self._hist_raw()
        target = next((h for h in entries if Path(h.get("report") or "").name == name), None)
        if not target:
            return self._json({"error": "没有这条记录"}, 404)
        keep = [h for h in entries if Path(h.get("report") or "").name != name]
        removed = []
        cand = []
        for key in ("report", "form"):
            if target.get(key):
                cand.append(Path(target[key]))
        rep = Path(target.get("report") or "")
        if rep.name.endswith("_report.html"):
            stem = rep.name[:-len("_report.html")]
            cand.append(rep.with_name(stem + "_full.json"))
            cand.append(rep.with_name(stem + "_form.json"))
        for p in cand:
            try:
                if p.name and p.exists():
                    p.unlink()
                    removed.append(p.name)
            except Exception as e:
                return self._json({"error": f"文件删不掉（{p.name}）：{type(e).__name__}: {e}"}, 500)
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            return self._json({"error": f"记录写不回去：{type(e).__name__}: {e}"}, 500)
        return self._json({"ok": True, "removed": removed})

    def _media(self, src: str = ""):
        """视频流。支持 Range，否则浏览器不能拖动进度条。"""
        job: Job = self.server.job
        src = src or job.video
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
            # 同一个视频反复跑也是各自一条：这份记录就是"分析过哪些"，不能互相覆盖
            entry = {"video": job.video, "at": time.strftime("%Y-%m-%d %H:%M"),
                     "report": job.report_path, "form": job.form_path,
                     "shots": job.shots, "status": job.status}
            hist = [h for h in hist if h.get("report") != job.report_path or not job.report_path]
            hist.insert(0, entry)
        STATE.write_text(json.dumps(hist[:50], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _watchdog(srv, grace: float = 30.0):
    """界面关掉之后自己退出，不留下孤儿进程。

    以页面心跳为准：前端每几秒打一次 /api/ping，超过 grace 秒没动静就退。
    任务在跑时不退——关掉页面不该把正在跑的标注杀掉。
    """
    while True:
        time.sleep(3)
        if time.time() - getattr(srv, "last_seen", time.time()) <= grace:
            continue
        if srv.job.snapshot()["status"] == "running":
            srv.last_seen = time.time()
            continue
        print("[应用] 界面已关闭，自动退出")
        srv.shutdown()
        return


def build_server(port: int = 0, provider: str = ""):
    """建好服务但不启动。桌面窗口和浏览器两种模式共用这一份。"""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.job = Job()
    srv.provider = provider
    srv.last_seen = time.time()
    srv.on_quit = None          # 桌面模式里由窗口接管"退出"
    return srv


def serve(port: int = 0, open_browser: bool = True, provider: str = "",
          watchdog: bool = True):
    srv = build_server(port, provider)
    real = srv.server_address[1]
    url = f"http://127.0.0.1:{real}/"
    print(f"[应用] {url}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    if watchdog:
        threading.Thread(target=_watchdog, args=(srv,), daemon=True).start()
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
    ap.add_argument("--no-watchdog", action="store_true",
                    help="界面关掉后不自动退出（调试或脚本调用时用）")
    ap.add_argument("--desktop", action="store_true",
                    help="开原生窗口（需要 pywebview；没有就回退浏览器）")
    a = ap.parse_args()
    if a.desktop:
        from app.desktop import run
        try:
            raise SystemExit(run(a.port, a.provider))
        except RuntimeError as e:
            print(f"[应用] 打不开窗口（{e}），改用浏览器")
    serve(a.port, not a.no_browser, a.provider, watchdog=not a.no_watchdog)
