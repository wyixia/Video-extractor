"""探测新端点 maas.qianwenaiapi.com 的能力：鉴权 / 模型列表 / 文本对话 / 音频转写。

密钥与 base_url 一律从环境变量读取，不落盘。
用法（在 Bash 中导出后运行）：
  export LLM_BASE_URL=https://maas.qianwenaiapi.com/compatible-mode/v1
  export LLM_API_KEY=sk-xxx
  python tools/probe_new_endpoint.py [音频文件路径(可选)]
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("LLM_BASE_URL", "").rstrip("/")
KEY = os.getenv("LLM_API_KEY", "")


def req(method, path, *, data=None, headers=None, timeout=60, raw=False):
    url = f"{BASE}{path}" if path.startswith("/") else path
    h = {"Authorization": f"Bearer {KEY}"}
    if headers:
        h.update(headers)
    body = data
    if isinstance(data, (dict, list)):
        body = json.dumps(data).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw_b = resp.read()
            return resp.status, raw_b if raw else raw_b.decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:500]
    except Exception as e:  # noqa
        return -1, f"{type(e).__name__}: {e}"


def main():
    print(f"BASE = {BASE}")
    print(f"KEY  = {KEY[:8]}...{KEY[-4:]} (len={len(KEY)})")
    if not (BASE and KEY):
        print("缺少环境变量 LLM_BASE_URL / LLM_API_KEY"); sys.exit(1)

    # 1) 模型列表
    print("\n=== 1) GET /models ===")
    code, txt = req("GET", "/models", timeout=30)
    print(f"status={code}")
    try:
        m = json.loads(txt) if isinstance(txt, str) else json.loads(txt)
        ids = [x.get("id") for x in m.get("data", [])]
        print(f"model count = {len(ids)}")
        print("\n".join(f"  - {i}") for i in ids)
        models = ids
    except Exception as e:  # noqa
        print("解析失败:", txt[:300])
        models = []

    # 2) 基础文本对话（挑第一个疑似文本模型）
    text_model = None
    for cand in ("qwen3.8-flash", "qwen3.8-max", "qwen3.6-flash", "qwen3.7-plus", "qwen-plus"):
        if cand in models:
            text_model = cand; break
    if not text_model:
        # 取第一个不含 audio/image 字样的
        for i in models:
            if "asr" not in i and "tts" not in i and "image" not in i and "audio" not in i:
                text_model = i; break
    print(f"\n=== 2) chat (model={text_model}) ===")
    if text_model:
        code, txt = req("POST", "/chat/completions", data={
            "model": text_model, "messages": [{"role": "user", "content": "回复两个字：可用"}],
            "max_tokens": 32,
        }, timeout=60)
        print(f"status={code}\n{txt[:400]}")

    # 3) 音频转写（OpenAI 兼容 /audio/transcriptions）
    audio = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"\n=== 3) audio transcription (file={audio}) ===")
    if not audio or not os.path.exists(audio):
        print("未提供音频文件，跳过。可附参数：python probe_new_endpoint.py path/to/a.wav")
    else:
        asr_model = None
        for cand in ("qwen-audio-3.0-asr-flash", "qwen3-asr-flash", "qwen3.8-omni-flash", "qwen-omni"):
            if cand in models:
                asr_model = cand; break
        if not asr_model:
            asr_model = "qwen-audio-3.0-asr-flash"  # 试探
        print(f"试探 ASR model = {asr_model}")
        # multipart/form-data
        boundary = "----wbprobe" + str(int(time.time()))
        with open(audio, "rb") as f:
            raw_b = f.read()
        parts = []
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="model"'.encode())
        parts.append(b"")
        parts.append(asr_model.encode())
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="file"; filename="a.wav"'.encode())
        parts.append(b"Content-Type: audio/wav")
        parts.append(b"")
        parts.append(raw_b)
        parts.append(f"--{boundary}--".encode())
        parts.append(b"")
        body = b"\r\n".join(parts)
        code, txt = req("POST", "/audio/transcriptions", data=body,
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=120)
        print(f"status={code}\n{txt[:500]}")


if __name__ == "__main__":
    main()
