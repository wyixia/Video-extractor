"""云端客户端封装。

- BaseClient       统一接口，业务代码只依赖 chat() / transcribe()
- DashScopeClient  阿里云百炼实现（VL/Omni/Merge 走 OpenAI 兼容；ASR 走原生异步任务）
- MockClient       离线测试实现，不调云端

切换服务商/本地部署：新增一个 BaseClient 子类，改 config.provider 即可，业务零改动。
"""
import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from typing import Any, List


class ClientError(Exception):
    """可重试错误（超时/连接/5xx/JSON 解析失败）。"""


class PermanentError(ClientError):
    """不可重试错误（鉴权/参数错/4xx），直接降级。"""


def retry_call(fn, retries: int = 2, backoff=(1, 2)):
    last = None
    for i in range(retries + 1):
        try:
            return fn()
        except PermanentError:
            raise
        except Exception as e:  # 网络/超时/解析
            last = e
            if i < retries:
                time.sleep(backoff[min(i, len(backoff) - 1)])
    raise ClientError(f"retry exhausted: {last}")


def extract_json(text: str) -> Any:
    """从模型输出中提取 JSON，兼容误加的 ```json 围栏与前后废话。"""
    if text is None:
        raise ClientError("empty response")
    s = text.strip()
    if s.startswith("```"):
        # 去掉首行围栏与结尾 ```
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 截取第一个 { 或 [ 到匹配结尾
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i, j = s.find(open_ch), s.rfind(close_ch)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ClientError(f"invalid json: {text[:200]}")


# ---------- 文件引用 ----------

def file_to_data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def image_data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def video_data_url(path: str) -> str:
    """短视频片段转 data URL。兼容端点通过 chat 的 video_url 类型接收。"""
    mime = mimetypes.guess_type(path)[0] or "video/mp4"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def audio_data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "audio/wav"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


# ---------- 接口 ----------

class BaseClient:
    def chat(self, model: str, messages: List[dict], timeout: int = 30, **kwargs) -> str:
        raise NotImplementedError

    def transcribe(self, audio_url: str, model: str, language: str = "zh",
                   enable_words: bool = True, timeout: int = 120) -> dict:
        raise NotImplementedError


class DashScopeClient(BaseClient):
    def __init__(self, ep: dict):
        try:
            from openai import OpenAI
            import requests
        except ImportError as e:
            raise RuntimeError("需要安装 openai>=1.52.0 / requests: pip install openai requests") from e

        self.name = ep.get("name", "default")
        # 环境变量优先；界面上保存的密钥作为兜底（存在本机配置里，不进 git）
        api_key = endpoint_key(ep)
        if not api_key:
            raise RuntimeError(
                f"端点「{self.name}」没配密钥：在应用「配置 → 密钥」里填，"
                f"或设环境变量 {ep.get('api_key_env') or 'DASHSCOPE_API_KEY'}")
        self.api_key = api_key
        self.base_url = ep["base_url"]
        self.asr_base = (ep.get("asr_base_url") or ep["base_url"]).rstrip("/")
        self.http = requests.Session()
        # 尊重环境代理（HTTP_PROXY/HTTPS_PROXY）
        self.client = OpenAI(api_key=api_key, base_url=ep["base_url"])

    # --- 通用对话（VL / Omni / Merge） ---
    def chat(self, model: str, messages: List[dict], timeout: int = 30, **kwargs) -> str:
        try:
            resp = self.client.chat.completions.create(
                model=model, messages=messages, timeout=timeout, **kwargs
            )
            return resp.choices[0].message.content
        except Exception as e:
            msg = str(e)
            # 4xx 鉴权/参数错不重试
            if any(k in msg for k in ("401", "403", "400", "invalid", "Unauthorized")):
                raise PermanentError(msg) from e
            raise ClientError(msg) from e

    # --- ASR 异步转写 ---
    def transcribe(self, audio_url: str, model: str, language: str = "zh",
                   enable_words: bool = True, timeout: int = 120) -> dict:
        if not (audio_url.startswith("http://") or audio_url.startswith("https://")):
            raise PermanentError(
                "ASR 需要公网可访问的音频 URL；请配置 storage.type=oss，或先用 mock provider 离线联调"
            )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        body = {
            "model": model,
            "input": {"file_urls": [audio_url]},
            "parameters": {"language": language, "enable_words": enable_words},
        }
        r = self.http.post(f"{self.asr_base}/services/audio/asr/transcription",
                           headers=headers, json=body, timeout=30)
        if r.status_code >= 500:
            raise ClientError(f"ASR submit 5xx: {r.text[:200]}")
        if r.status_code >= 400:
            raise PermanentError(f"ASR submit {r.status_code}: {r.text[:200]}")
        task_id = r.json()["output"]["task_id"]

        deadline = time.time() + timeout
        while time.time() < deadline:
            q = self.http.get(f"{self.asr_base}/tasks/{task_id}",
                              headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30)
            j = q.json()
            out = j.get("output", {})
            status = out.get("task_status")
            if status == "SUCCEEDED":
                results = out.get("results") or []
                if not results:
                    return {"task": "asr", "segments": []}
                turl = results[0].get("transcription_url")
                data = self.http.get(turl, timeout=30).json()
                return self._parse_transcription(data)
            if status == "FAILED":
                raise ClientError(f"ASR task failed: {j}")
            time.sleep(2)
        raise ClientError("ASR polling timeout")

    @staticmethod
    def _parse_transcription(data: dict) -> dict:
        segments = []
        for tr in data.get("transcripts", []):
            for s in tr.get("sentences", []):
                words = [
                    {"start_ms": int(w.get("begin_time", 0)),
                     "end_ms": int(w.get("end_time", 0)),
                     "text": w.get("text", "")}
                    for w in s.get("words", [])
                ]
                segments.append({
                    "start_ms": int(s.get("begin_time", 0)),
                    "end_ms": int(s.get("end_time", 0)),
                    "text": s.get("text", ""),
                    "words": words,
                })
        return {"task": "asr", "segments": segments}


class HttpCompatClient(BaseClient):
    """任意 OpenAI 兼容端点（标准库 urllib，无需 openai/requests）。

    支持文本 + 图像(image_url 的 base64 data URL)；
    音频模态若端点不支持，会抛 PermanentError 并由编排层降级。
    一个实例绑定一个端点（一份地址 + 一份密钥）。
    """

    def __init__(self, ep: dict):
        self.name = ep.get("name", "default")
        # 地址与密钥都可以来自环境变量，环境变量优先
        self.base = (os.getenv(ep.get("base_url_env") or "__none__", "")
                     or ep.get("base_url", "")).rstrip("/")
        self.key = endpoint_key(ep)
        # 分开报，缺哪个说哪个：混成一句"需要环境变量"让人不知道该改哪里
        if not self.base:
            raise RuntimeError(
                f"端点「{self.name}」没配地址：在应用「配置 → 端点」里填，"
                f"或设环境变量 {ep.get('base_url_env') or '（无）'}")
        if not self.key:
            raise RuntimeError(
                f"端点「{self.name}」没配密钥：在应用「配置 → 密钥」里填，"
                f"或设环境变量 {ep.get('api_key_env') or 'LLM_API_KEY'}")

    def chat(self, model: str, messages: List[dict], timeout: int = 30, **kwargs) -> str:
        return self.chat_detail(model, messages, timeout=timeout, **kwargs)[0]

    def chat_detail(self, model: str, messages: List[dict], timeout: int = 30, **kwargs):
        """返回 (content, usage)。usage 用来确认音频/图片真的被模型收到。

        只看 HTTP 200 判断不了：模型可能接受请求却忽略音频附件照常作答，
        表现为"能返回内容"，其实一个声音都没听。audio_tokens 才是硬证据。
        """
        body_dict = {
            "model": model, "messages": messages,
            "max_tokens": kwargs.get("max_tokens", 1024),
        }
        # 透传额外参数（如 enable_thinking / temperature），未知键不写入
        for k in ("enable_thinking", "temperature", "top_p"):
            if k in kwargs and kwargs[k] is not None:
                body_dict[k] = kwargs[k]
        body = json.dumps(body_dict).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=body, method="POST",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        try:
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8")
            j = json.loads(raw)
            content = (j.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
            return content, (j.get("usage") or {})
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")[:300]
            # 429 限流与 5xx 服务端错误均可重试；其余 4xx（鉴权/参数）不重试
            if e.code == 429 or e.code >= 500:
                raise ClientError(f"HTTP {e.code}: {msg}") from e
            raise PermanentError(f"HTTP {e.code}: {msg}") from e
        except Exception as e:
            raise ClientError(str(e)) from e

    def transcribe(self, audio_url: str, model: str, language: str = "zh",
                   enable_words: bool = True, timeout: int = 120) -> dict:
        raise PermanentError("compat 端点不支持 ASR 音频输入")


class MockClient(BaseClient):
    """离线测试：按 prompt 关键词返回固定结果，不联网。"""

    def chat(self, model: str, messages: List[dict], timeout: int = 30, **kwargs) -> str:
        text = json.dumps(messages, ensure_ascii=False)
        # 顺序很重要：VL 的 prompt 也含"环境音"，必须优先匹配"分镜标注师"
        if "分镜标注师" in text:
            return json.dumps({
                "task": "vl", "shot_size": "中景",
                "angle_camera_composition": "平视正面机位，两名运动员分列球台两侧",
                "camera_movement": "固定镜头",
                "mouth_state": "none",
                "content_info": "两名运动员持拍对峙，表情专注，无入画出画",
            }, ensure_ascii=False)
        if "人声转写" in text or "台词转写" in text or "语音/台词" in text:
            return json.dumps({"task": "asr", "segments": [
                {"text": "这是一句测试台词", "start_time": 0.2, "end_time": 1.0,
                 "voice": "speech"}
            ]}, ensure_ascii=False)
        if "背景音乐" in text:
            return json.dumps({"task": "music", "present": False}, ensure_ascii=False)
        if "环境音效" in text or "环境音" in text:
            return json.dumps({"task": "sed", "events": [
                {"type": "乒乓球撞击声", "start_time": 1.2, "end_time": 1.35, "confidence": 0.9}
            ]}, ensure_ascii=False)
        if "汇总与校验" in text:
            return json.dumps({"shots": []}, ensure_ascii=False)
        return "{}"

    def transcribe(self, audio_url: str, model: str, language: str = "zh",
                   enable_words: bool = True, timeout: int = 120) -> dict:
        return {"task": "asr", "segments": [
            {"start_ms": 1000, "end_ms": 2820, "text": "下一球注意落点",
             "words": [{"start_ms": 1000, "end_ms": 2820, "text": "下一球注意落点"}]}
        ]}


# ---------- 端点（一份地址 + 一份密钥） ----------

# 角色固定为这几个。calibrate 不配就并入 merge。
ROLES = ("asr", "sed", "music", "vl", "merge", "calibrate")


def endpoint_key(ep: dict) -> str:
    """一份端点的密钥：环境变量优先，其次界面保存的值。

    两个来源都行是为了兼顾两种用法：CI/服务器用环境变量，桌面用户用界面。
    """
    env_name = ep.get("api_key_env") or ""
    return ((os.getenv(env_name) if env_name else "") or ep.get("api_key") or "").strip()


def endpoint_key_source(ep: dict) -> str:
    """密钥来自哪里：env / local / 空。界面要显示这个，否则用户不知道哪个生效。"""
    env_name = ep.get("api_key_env") or ""
    if env_name and os.getenv(env_name):
        return "env"
    if (ep.get("api_key") or "").strip():
        return "local"
    return ""


def resolve_endpoints(cfg: dict) -> dict:
    """把配置归一成 {名字: 端点}。

    新配置用 endpoints 段显式声明；老配置只有 compat / dashscope 一段，
    这里自动折成一个名为 default 的端点，旧配置不用改就能继续跑。
    """
    raw = cfg.get("endpoints") or {}
    if raw:
        out = {}
        default_type = (cfg.get("provider") or "compat").strip()
        for name, ep in raw.items():
            ep = dict(ep or {})
            ep.setdefault("type", default_type if default_type != "mock" else "compat")
            ep["name"] = str(name)
            out[str(name)] = ep
        return out

    prov = (cfg.get("provider") or "compat").strip()
    if prov == "dashscope":
        ds = cfg.get("dashscope") or {}
        return {"default": {
            "name": "default", "type": "dashscope",
            "base_url": ds.get("base_url", ""),
            "asr_base_url": ds.get("asr_base_url", ""),
            "api_key_env": ds.get("api_key_env", "DASHSCOPE_API_KEY"),
            "api_key": ds.get("api_key", ""),
        }}
    c = cfg.get("compat") or {}
    return {"default": {
        "name": "default", "type": "compat",
        "base_url": c.get("base_url", ""),
        "base_url_env": c.get("base_url_env", "LLM_BASE_URL"),
        "api_key_env": c.get("api_key_env", "LLM_API_KEY"),
        "api_key": c.get("api_key", ""),
    }}


def default_endpoint_name(cfg: dict) -> str:
    eps = resolve_endpoints(cfg)
    if "default" in eps:
        return "default"
    return next(iter(eps), "default")


def role_endpoint_map(cfg: dict) -> dict:
    """角色 → 端点名。没写的角色回落到 default。"""
    raw = cfg.get("role_endpoints") or {}
    return {str(k): str(v) for k, v in raw.items() if v}


def endpoint_for_role(cfg: dict, role: str) -> str:
    m = role_endpoint_map(cfg)
    return m.get(role) or m.get("*") or default_endpoint_name(cfg)


def _make_client(ep: dict) -> BaseClient:
    t = (ep.get("type") or "compat").strip()
    if t == "dashscope":
        return DashScopeClient(ep)
    if t == "compat":
        return HttpCompatClient(ep)
    raise ValueError(f"端点「{ep.get('name')}」类型不认识：{t}（可选 compat / dashscope）")


class ClientRouter(BaseClient):
    """按角色把请求分到不同端点。

    一个角色 = 一个 (地址, 密钥, 模型) 组合。这样音频走一家、视觉走另一家也成立：
    各自的密钥、各自的配额，互不影响。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.endpoints = resolve_endpoints(cfg)
        self.role_map = role_endpoint_map(cfg)
        self._clients: dict = {}

    def endpoint_name(self, role: str) -> str:
        name = self.role_map.get(role) or self.role_map.get("*") or default_endpoint_name(self.cfg)
        if name not in self.endpoints:
            raise ValueError(
                f"角色 {role} 指向端点「{name}」，但配置里只有 {list(self.endpoints)}")
        return name

    def for_role(self, role: str) -> BaseClient:
        name = self.endpoint_name(role)
        if name not in self._clients:
            self._clients[name] = _make_client(self.endpoints[name])
        return self._clients[name]

    # 站不住脚就直接报错的兜底：单端点场景下调用方不用关心角色
    def chat(self, model: str, messages: List[dict], **kwargs) -> str:
        return self.for_role("merge").chat(model, messages, **kwargs)

    def transcribe(self, audio_url: str, model: str, **kwargs) -> dict:
        return self.for_role("asr").transcribe(audio_url, model, **kwargs)


def build_client(cfg: dict) -> BaseClient:
    provider = (cfg.get("provider") or "").strip()
    if provider == "mock":
        return MockClient()
    return ClientRouter(cfg)
