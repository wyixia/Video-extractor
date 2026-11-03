"""提示词完整性校验。

用户可以直接改内置提示词或放一份覆盖版，但有几样东西不能动：
输出 JSON 的键名、时间字段名、以及"只输出 JSON"这类硬约束。
改了它们不会报错，而是**静默产出错误结果**——模型返回的键对不上，代码取不到值，
字段全部降级成"无法识别"，看起来像模型不行。所以这里在运行前先检查一遍。

检查的是"还能不能工作"，不是"写得好不好"：措辞、字数、风格随便改，
只守住会让结果出错的锚点。
"""
from typing import Dict, List

from .config import read_prompt, prompt_sources

# 每个提示词必须保留的锚点：JSON 键名与时间字段
REQUIRED_KEYS = {
    "asr": ("segments", "text", "start_time", "end_time", "voice"),
    "sed": ("events", "type", "start_time"),
    "music": ("present",),
    "vl": ("shot_size", "angle_camera_composition", "camera_movement",
           "mouth_state", "content_info"),
    "merge": ("overview",),
    "calibrate": ("pairs",),
}

# 篇幅下限：删到几十字说明多半是清空或误覆盖
MIN_LEN = {"asr": 120, "sed": 120, "music": 120, "vl": 600, "merge": 200, "calibrate": 200}


def check_prompt(name: str) -> List[str]:
    """检查单个提示词，返回问题列表（空列表表示通过）。"""
    try:
        text = read_prompt(name)
    except FileNotFoundError as e:
        return [str(e)]
    problems = []
    for key in REQUIRED_KEYS.get(name, ()):
        if key not in text:
            problems.append(f"缺少必需键名 {key!r}（代码靠它取值，删掉后该字段会全部变成“无法识别”）")
    low = MIN_LEN.get(name, 80)
    if len(text) < low:
        problems.append(f"只有 {len(text)} 字，短于 {low} 字，可能被清空或误覆盖")
    if "JSON" not in text and "json" not in text:
        problems.append("没有提到 JSON，模型可能返回自然语言导致解析失败")
    return problems


def check_all() -> Dict[str, dict]:
    """检查全部提示词，返回 {name: {source, problems}}。"""
    out = {}
    for name, src in prompt_sources().items():
        out[name] = {"source": src, "problems": check_prompt(name)}
    return out


def summarize(report: Dict[str, dict]) -> str:
    lines = []
    for name, item in report.items():
        tag = "本地覆盖" if item["source"] == "local" else "内置"
        if item["problems"]:
            lines.append(f"[×] {name}.txt（{tag}）")
            for p in item["problems"]:
                lines.append(f"      - {p}")
        else:
            lines.append(f"[√] {name}.txt（{tag}）")
    return "\n".join(lines)
