"""探测音频输入的正确报文格式，打印服务端错误体。"""
import base64
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = os.environ["LLM_BASE_URL"].rstrip("/")
KEY = os.environ["LLM_API_KEY"]
OUT = Path(__file__).resolve().parent.parent / "_probe"
OUT.mkdir(exist_ok=True)


def post(model, content, max_tokens=200):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}],
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(f"{BASE}/chat/completions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {KEY}",
                                          "Content-Type": "application/json"})
    try:
        j = json.loads(urllib.request.urlopen(req, timeout=90).read().decode())
        return "OK: " + ((j.get("choices", [{}])[0].get("message", {}) or {}).get("content", "") or "")[:300]
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: " + e.read().decode()[:300]
    except Exception as e:
        return f"ERR: {e}"


def main(video):
    audio = OUT / "a6.wav"
    subprocess.run(["ffmpeg", "-y", "-ss", "0", "-t", "6", "-i", video, "-vn", "-ac", "1",
                    "-ar", "16000", str(audio)], check=True, capture_output=True)
    raw_b64 = base64.b64encode(audio.read_bytes()).decode()
    data_url = "data:audio/wav;base64," + raw_b64

    variants = {
        "input_audio(data_url)": [{"type": "input_audio", "input_audio": {"data": data_url, "format": "wav"}}],
        "input_audio(raw_b64)": [{"type": "input_audio", "input_audio": {"data": raw_b64, "format": "wav"}}],
        "input_audio(string)": [{"type": "input_audio", "input_audio": data_url}],
        "audio_url(data_url)": [{"type": "audio_url", "audio_url": {"url": data_url}}],
        "audio(data_url)": [{"type": "audio", "audio": data_url}],
    }
    for model in ["qwen-audio-3.0-asr-flash", "qwen3.8-max"]:
        print(f"\n### {model}")
        for name, content in variants.items():
            c = content + [{"type": "text", "text": "转写这段音频并给每句起止秒数。"}]
            print(f"  {name}: {post(model, c)}")


if __name__ == "__main__":
    main(sys.argv[1])
