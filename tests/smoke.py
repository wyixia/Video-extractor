"""标准库冒烟测试：不依赖第三方包、不联网、不读真实视频，验证整条编排链路。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.orchestrator as orch
import src.skills as skills
from src import validate as V
from src.models import Shot
from src.orchestrator import Pipeline

CFG = {
    "provider": "mock",
    "dashscope": {"api_key_env": "DASHSCOPE_API_KEY",
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "asr_base_url": "https://dashscope.aliyuncs.com/api/v1"},
    "models": {"asr": "paraformer", "sed": "qwen3.8-omni-flash",
               "music": "qwen3.8-omni-flash", "vl": "qwen2.5-vl-72b-instruct",
               "merge": "qwen-plus"},
    "preprocess": {"shot_split": {"method": "ffmpeg_scene", "scene_threshold": 0.3},
                   "keyframes": {"max_per_shot": 4, "interval_sec": 2},
                   "audio": {"method": "demucs", "two_stems": "vocals"}},
    "runtime": {"max_workers": 4, "call_timeout_sec": 30, "merge_timeout_sec": 60,
                "retry": 2, "retry_backoff_sec": [1, 2]},
    "skills": {"asr": {"enabled": True}, "sed": {"enabled": True},
               "music": {"enabled": True}, "vl": {"enabled": True}},
    "paths": {"workdir": "./_work", "output": "./output"},
    "storage": {"type": "local"},
}

KEYS = ["shot_number", "time_range", "shot_size", "angle_camera_composition",
        "camera_movement", "content_info", "env_sound", "music", "speech"]


def main():
    shots = [Shot("shot 1", 0, 2250, ["f0.jpg"]), Shot("shot 2", 2250, 4567, ["f1.jpg"])]
    orch.preprocess = lambda v, c, w: (shots, 13.46,
                                      [{"path": "chunk.mp3", "start_ms": 0, "end_ms": 13460}])
    skills.image_data_url = lambda p: "data:image/jpeg;base64,AAAA"
    skills.audio_data_url = lambda p: "data:audio/mp3;base64,AAAA"

    final = Pipeline(CFG).run("pingpong.mp4", "864689235027272")

    assert final["video_meta"]["total_shots"] == 2
    assert len(final["shots"]) == 2
    for s in final["shots"]:
        assert set(s.keys()) == set(KEYS)
        for k in KEYS:
            assert isinstance(s[k], str) and s[k].strip(), f"{k} 不应为空"
    assert final["shots"][0]["time_range"] == "00:00:00,000-00:00:02,250"
    # 画面口型 none + 音频判定说话 → 只能是画外解说，不能写成"对话"
    assert final["shots"][0]["speech"].startswith("画外解说 <d>"), final["shots"][0]["speech"]
    assert "总体信息：" in final["full_caption"]

    # 台词定类：两路证据各自独立；证据不足返回空串交给全片多数
    lbl = V._speech_label
    assert lbl("singing", ["none"]) == "歌词"            # 音频判演唱，优先于口型
    assert lbl("speech", ["singing"]) == "歌词"           # 画面在唱
    assert lbl("speech", ["speaking"]) == "对话"
    assert lbl("speech", ["none"]) == "画外解说"           # 画面无人张口
    assert lbl("speech", ["unknown"]) == ""               # 口型看不清不硬判
    assert lbl("speech", ["unknown", "none"]) == "画外解说"  # 跨镜取最明确的证据

    print(json.dumps(final["shots"], ensure_ascii=False, indent=2))
    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
