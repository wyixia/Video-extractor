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
    def __init__(self, cfg: dict):
        try:
            from openai import OpenAI
            import requests
        except ImportError as e:
            raise RuntimeError("需要安装 openai>=1.52.0 / requests: pip install openai requests") from e

        ds = cfg["dashscope"]
        # 环境变量优先；界面上保存的密钥作为兜底（存在本机配置里，不进 git）
        api_key = (os.getenv(ds.get("api_key_env", "")) or ds.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError(
                f"没配密钥：在应用「配置 → API Key」里填，或设环境变量 {ds.get('api_key_env')}")
        self.api_key = api_key
        self.base_url = ds["base_url"]
        self.asr_base = ds["asr_base_url"].rstrip("/")
        self.http = requests.Session()
        # 尊重环境代理（HTTP_PROXY/HTTPS_PROXY）
        self.client = OpenAI(api_key=api_key, base_url=ds["base_url"])

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
    """

    def __init__(self, cfg: dict):
        c = cfg.get("compat", {})
        self.base = os.getenv(c.get("base_url_env", "LLM_BASE_URL"), c.get("base_url", "")).rstrip("/")
        # 环境变量优先；界面上保存的密钥作为兜底（存在本机配置里，不进 git）
        self.key = (os.getenv(c.get("api_key_env", "LLM_API_KEY")) or c.get("api_key") or "").strip()
        # 分开报，缺哪个说哪个：混成一句"需要环境变量"让人不知道该改哪里
        if not self.base:
            raise RuntimeError("没配接口地址：在应用「配置 → 接口地址」里填，或设环境变量 "
                               f"{c.get('base_url_env', 'LLM_BASE_URL')}")
        if not self.key:
            raise RuntimeError("没配密钥：在应用「配置 → API Key」里填，或设环境变量 "
                               f"{c.get('api_key_env', 'LLM_API_KEY')}")

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


def build_client(cfg: dict) -> BaseClient:
    provider = cfg.get("provider", "dashscope")
    if provider == "mock":
        return MockClient()
    if provider == "dashscope":
        return DashScopeClient(cfg)
    if provider == "compat":
        return HttpCompatClient(cfg)
    raise ValueError(f"unknown provider: {provider}")
