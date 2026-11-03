"""数据模型与时间工具。内部统一用毫秒(ms)整数，输出表单时转 HH:MM:SS,mmm。"""
from dataclasses import dataclass, field, asdict
from typing import List, Optional


def ms_to_hhmmssms(ms: int) -> str:
    """毫秒 -> HH:MM:SS,mmm（表单要求逗号毫秒）。"""
    ms = max(0, int(ms))
    h = ms // 3_600_000
    m = (ms % 3_600_000) // 60_000
    s = (ms % 60_000) // 1000
    msec = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"


def format_range(start_ms: int, end_ms: int) -> str:
    return f"{ms_to_hhmmssms(start_ms)}-{ms_to_hhmmssms(end_ms)}"


def hhmmssms_to_ms(t: str) -> int:
    """HH:MM:SS,mmm 或 HH:MM:SS.mmm -> 毫秒。"""
    t = t.strip().replace(".", ",")
    hms, msec = t.split(",")
    h, m, s = hms.split(":")
    return int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1000 + int(msec)


@dataclass
class Shot:
    """预处理产出的一个镜头。"""
    shot_id: str
    start_ms: int
    end_ms: int
    frames: List[str] = field(default_factory=list)
    motion: Optional[dict] = None  # CPU 全局运动测量结果（尺度/位移/候选运镜）

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass
class ShotRecord:
    """对齐前端表单的单镜记录（9 字段）。"""
    shot_number: str
    time_range: str
    shot_size: str = "无法识别"
    angle_camera_composition: str = "无法识别"
    camera_movement: str = "无法识别"
    content_info: str = "无法识别"
    env_sound: str = "无"
    music: str = "无"
    speech: str = "无"
    needs_review: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("needs_review", None)  # needs_review 是内部标记，不直接进表单
        return d
