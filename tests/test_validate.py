from src.models import ms_to_hhmmssms, hhmmssms_to_ms, format_range, Shot
from src import validate as V


def test_time_roundtrip():
    assert ms_to_hhmmssms(0) == "00:00:00,000"
    assert ms_to_hhmmssms(3661234) == "01:01:01,234"
    assert hhmmssms_to_ms("01:01:01,234") == 3661234
    assert format_range(0, 2250) == "00:00:00,000-00:00:02,250"


def test_align_and_finalize():
    shots = [Shot("shot 1", 0, 2250), Shot("shot 2", 2250, 4567)]
    raw = {
        "asr": {"segments": [{"start_ms": 1000, "end_ms": 2820, "text": "下一球注意落点"}]},
        "sed": {"events": [{"type": "乒乓球撞击声", "start_ms": 1200, "end_ms": 1350}]},
        "music": {"present": False},
        "vl": [{"shot_id": "shot 1", "shot_size": "中景",
                "angle_camera_composition": "平视", "camera_movement": "固定镜头",
                "content_info": "对峙"}],
    }
    recs = V.align_raw_to_shots(shots, raw)
    assert recs[0]["speech"].startswith("画外 S1 <d>")
    assert recs[0]["env_sound"] == "乒乓球撞击声"
    assert recs[1]["shot_size"] == "无法识别"
    assert recs[1]["needs_review"] is True

    final = V.finalize("测试总览", recs, 13.46, "id", "x.mp4", raw)
    assert "总体信息：测试总览" in final["full_caption"]
    for s in final["shots"]:
        for k, v in s.items():
            assert isinstance(v, str) and v.strip(), f"{k} 不能为空"
