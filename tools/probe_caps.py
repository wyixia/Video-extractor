"""探测自定义 OpenAI 兼容端点的模态能力（图像/音频）。仅用标准库。"""
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


def post(model: str, content: list, max_tokens: int = 300) -> str:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}],
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(f"{BASE}/chat/completions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {KEY}",
                                          "Content-Type": "application/json"})
    try:
        raw = urllib.request.urlopen(req, timeout=90).read().decode()
        j = json.loads(raw)
        return (j.get("choices", [{}])[0].get("message", {}) or {}).get("content", "") or raw
    except Exception as e:
        return f"ERR: {e}"


def data_url(path: Path) -> str:
    mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "audio/wav"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def main(video: str):
    OUT.mkdir(exist_ok=True)
    frame = OUT / "frame.jpg"
    audio = OUT / "audio.wav"
    subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", video, "-frames:v", "1", "-q:v", "2", str(frame)],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-ss", "0", "-t", "6", "-i", video, "-vn", "-ac", "1",
                    "-ar", "16000", str(audio)], check=True, capture_output=True)

    img = data_url(frame)
    au = data_url(audio)
    print("== VISION ==")
    for m in ["qwen3.8-max", "qwen3.8-flash", "qwen3.7-plus", "qwen3.7-max"]:
        r = post(m, [{"type": "image_url", "image_url": {"url": img}},
                     {"type": "text", "text": "用一句话客观描述这张画面。"}], 120)
        print(m, "->", r[:160].replace("\n", " "))

    print("\n== AUDIO ASR ==")
    for m in ["qwen-audio-3.0-asr-flash", "qwen3.8-max"]:
        r = post(m, [{"type": "input_audio", "input_audio": {"data": au, "format": "wav"}},
                     {"type": "text", "text": "转写这段音频，并给出每句话的起止秒数。"}], 400)
        print(m, "->", r[:400].replace("\n", " "))

    print("\n== AUDIO SED/MUSIC ==")
    for m in ["qwen3.8-max", "qwen3.8-flash"]:
        r = post(m, [{"type": "input_audio", "input_audio": {"data": au, "format": "wav"}},
                     {"type": "text", "text": "列出这段音频中的环境音/音效与背景音乐，给起止秒数，只输出JSON。"}], 400)
        print(m, "->", r[:400].replace("\n", " "))


if __name__ == "__main__":
    main(sys.argv[1])
