"""穷举测试该兼容端点对音频输入的接受方式。

用法：
    LLM_API_KEY=xxx python tools/probe_audio_matrix.py

只打印结果，不改任何配置，不写日志文件。
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("LLM_BASE_URL", "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1").rstrip("/")
KEY = os.environ["LLM_API_KEY"]
WAV = sys.argv[1] if len(sys.argv) > 1 else "_probe/t3.wav"

raw = open(WAV, "rb").read()
b64 = base64.b64encode(raw).decode()
data_url = "data:audio/wav;base64," + b64


def post(path, body, headers=None, timeout=180):
    # 必须显式声明 JSON，否则 urllib 默认发 form-urlencoded，
    # 网关按表单解析就取不到 model 字段，会误报 "Model not exist"
    h = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(BASE + path, data=body, method="POST", headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def chat(model, content, extra=None, note=""):
    body = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 300}
    if extra:
        body.update(extra)
    code, txt = post("/chat/completions", json.dumps(body).encode())
    ok = code == 200
    print(f"  [{'OK  ' if ok else 'FAIL'}] {note:<38} {code} {txt[:420]}".replace("\n", " "))
    return ok, txt


def audio_var():
    return [
        ("input_audio.data=url", [{"type": "input_audio", "input_audio": {"data": data_url, "format": "wav"}},
                                  {"type": "text", "text": "转写这段音频"}]),
        ("input_audio.data=raw_b64", [{"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
                                      {"type": "text", "text": "转写这段音频"}]),
        ("input_audio.url", [{"type": "input_audio", "input_audio": {"url": data_url}},
                             {"type": "text", "text": "转写这段音频"}]),
        ("input_audio.data only", [{"type": "input_audio", "input_audio": {"data": data_url}},
                                   {"type": "text", "text": "转写这段音频"}]),
        ("audio_url (dashscope old)", [{"type": "audio_url", "audio_url": {"url": data_url}},
                                       {"type": "text", "text": "转写这段音频"}]),
        ("audio url (gemini style)", [{"type": "audio", "audio": {"url": data_url, "mime_type": "audio/wav"}},
                                      {"type": "text", "text": "转写这段音频"}]),
        ("input_audio mime only", [{"type": "input_audio", "input_audio": {"data": b64, "mime_type": "audio/wav"}},
                                   {"type": "text", "text": "转写这段音频"}]),
    ]


print("=" * 100)
print("第一步：这些模型到底存不存在 / 纯文本是否可用")
print("=" * 100)
models = ["qwen-audio-3.0-asr-flash", "qwen-audio-3.0-realtime-plus",
          "qwen-audio-3.0-tts-plus", "qwen3.8-flash"]
for m in models:
    chat(m, "回复两个字：收到", note="纯文本")

print()
print("=" * 100)
print("第二步：音频输入的 7 种传法（每个模型都试）")
print("=" * 100)
for m in models:
    print(f"\n-- {m} --")
    for note, content in audio_var():
        chat(m, content, note=note)

print()
print("=" * 100)
print("第三步：OpenAI 标准转写/翻译接口")
print("=" * 100)


def multipart(path, model, extra_fields):
    bnd = "----wb" + uuid.uuid4().hex
    body = b""
    for k, v in extra_fields.items():
        body += (f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    body += (f"--{bnd}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n").encode()
    body += (f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.wav\"\r\n"
             f"Content-Type: audio/wav\r\n\r\n").encode() + raw + b"\r\n"
    body += (f"--{bnd}--\r\n").encode()
    code, txt = post(path, body, {"Content-Type": "multipart/form-data; boundary=" + bnd})
    print(f"  [{'OK  ' if code == 200 else 'FAIL'}] {path:<24} {code} {txt[:300]}".replace("\n", " "))


multipart("/audio/transcriptions", "qwen-audio-3.0-asr-flash", {"response_format": "verbose_json"})
multipart("/audio/translations", "qwen-audio-3.0-asr-flash", {})

print()
print("=" * 100)
print("第四步：是否支持文件上传（大音频必须靠它）")
print("=" * 100)
for path in ("/files", "/uploads", "/v1/files"):
    code, txt = post(path, b"", {"Content-Type": "application/json"})
    print(f"  POST {path:<12} -> {code} {txt[:150]}".replace("\n", " "))
