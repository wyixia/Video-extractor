"""导出：把最终结果转成前端标注表单可直接回填的 JSON。"""
import json
from pathlib import Path
from typing import List

FORM_KEYS: List[str] = [
    "shot_number", "time_range", "shot_size", "angle_camera_composition",
    "camera_movement", "content_info", "env_sound", "music", "speech",
]


def frontend_payload(final: dict) -> dict:
    """仅保留表单需要的字段，去掉 raw_subskills 等内部数据。"""
    return {
        "subtask_id": final.get("video_meta", {}).get("subtask_id", ""),
        "overview": final.get("overview", ""),
        "shots": [{k: s.get(k, "") for k in FORM_KEYS} for s in final.get("shots", [])],
        "full_caption": final.get("full_caption", ""),
        "review_flags": final.get("review_flags", []),
    }


def write_json(path: str, obj: dict) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)
