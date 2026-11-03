import src.orchestrator as orch
import src.skills as skills
from src.config import load_config
from src.orchestrator import Pipeline
from src.models import Shot


def test_pipeline_mock_end_to_end(monkeypatch):
    cfg = load_config("config/config.yaml", {"provider": "mock"})
    shots = [
        Shot("shot 1", 0, 2250, ["f0.jpg"]),
        Shot("shot 2", 2250, 4567, ["f1.jpg"]),
    ]

    # 不依赖真实视频/ffmpeg；图片读取打桩
    monkeypatch.setattr(orch, "preprocess", lambda v, c, w: (shots, "v.wav", "n.wav", 13.46))
    monkeypatch.setattr(skills, "image_data_url", lambda p: "data:image/jpeg;base64,AAAA")

    final = Pipeline(cfg).run("pingpong.mp4", "864689235027272")

    keys = ["shot_number", "time_range", "shot_size", "angle_camera_composition",
            "camera_movement", "content_info", "env_sound", "music", "speech"]
    assert final["video_meta"]["total_shots"] == 2
    assert len(final["shots"]) == 2
    for s in final["shots"]:
        assert set(s.keys()) == set(keys)
        for k in keys:
            assert s[k], f"{k} 不应为空"
    assert final["shots"][0]["time_range"] == "00:00:00,000-00:00:02,250"
    assert final["shots"][0]["speech"].startswith("画外 S1 <d>")
    assert "总体信息：" in final["full_caption"]
