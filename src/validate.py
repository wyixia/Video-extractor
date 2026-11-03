"""合并、校验与最终 Schema 构造。

设计原则：时间对齐与字段兜底用确定性代码完成（比 LLM 更可靠），
LLM 只负责 overview 文案；任何缺失都降级为"无法识别"/"无"，绝不空字段。
"""
import json
from typing import Dict, List

from .models import Shot, format_range


def normalize_voice(v) -> str:
    """把人声形式归一到 speech / singing / unknown。

    模型可能回中文（"演唱""念白"）或带解释的短句，这里按关键词归类；
    归不进去就是 unknown，宁可判不出，也不硬套成某一类。
    """
    s = (v if isinstance(v, str) else str(v or "")).strip().lower()
    if not s:
        return "unknown"
    if any(k in s for k in ("sing", "演唱", "唱", "歌")):
        return "singing"
    if any(k in s for k in ("speech", "speak", "talking", "说", "念", "解说", "对白")):
        return "speech"
    return "unknown"


def normalize_mouth(v) -> str:
    """把口型字段归一到 none / speaking / singing / unknown。"""
    s = (v if isinstance(v, str) else str(v or "")).strip().lower()
    if not s:
        return "unknown"
    if any(k in s for k in ("sing", "演唱")):
        return "singing"
    if any(k in s for k in ("speak", "说话口型", "对白")):
        return "speaking"
    if any(k in s for k in ("none", "无", "未开口", "没有人物")):
        return "none"
    return "unknown"


def _speech_label(voice, mouths: List[str]) -> str:
    """给一句话定类：歌词 / 对话 / 画外解说；返回空串表示证据不足。

    依据两路独立证据，只在证据支持时下结论：
      1. 音频判出演唱 → 歌词（声音本身有旋律、贴节拍，比口型可靠）；
      2. 画面口型在演唱 → 歌词；在说话 → 对话；
      3. 句子覆盖到的镜头里明确无人张口 → 画外解说；
      4. 全部是 unclear/unknown → 返回空串，交给全片多数去补，不在这里猜。
    mouths 传该句时间上覆盖到的所有镜头的口型：取最明确的那个，
    这样跨镜的一句话不会因为某一镜看不清就整句判不出来。
    """
    if normalize_voice(voice) == "singing":
        return "歌词"
    if "singing" in mouths:
        return "歌词"
    if "speaking" in mouths:
        return "对话"
    if "none" in mouths:
        return "画外解说"
    return ""


def _flatten_dict(d: dict) -> str:
    """把 {'a_拍摄高度': '...'} 展平成 '拍摄高度：...；水平角度：...'。"""
    import re as _re
    items = []
    for k, val in d.items():
        key = _re.sub(r"^[a-zA-Z][_\\.、]?\s*", "", str(k)).strip()
        txt = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        txt = txt.strip()
        if txt:
            items.append(f"{key}：{txt}" if key else txt)
    return "；".join(items)


def normalize_text(v, default: str = "无") -> str:
    """统一成字符串；模型偶尔把字段返回成对象或对象的字符串形式，这里展平。"""
    if v is None:
        return default
    if isinstance(v, dict):
        v = _flatten_dict(v)
    elif isinstance(v, list):
        v = "；".join(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in v)
    elif not isinstance(v, str):
        v = str(v)
    v = v.strip()
    # 对象被 str() 过的形态：{'a_拍摄高度': '...'}
    if v.startswith("{") and v.endswith("}") and ":" in v:
        try:
            import ast
            parsed = ast.literal_eval(v)
            if isinstance(parsed, dict):
                v = _flatten_dict(parsed)
        except Exception:
            pass
    v = v.strip()
    return v if v else default


def _slice_text(txt: str, s0: int, s1: int, w0: int, w1: int) -> str:
    """把一句话按时间重叠比例切给某个镜头，切点尽量对齐标点。

    模型只给整句起止，没有词级时间戳；跨镜的句子若整句照抄到每个镜头，
    表单会变成多镜重复同一句话。这里按重叠时长占比切字符，
    使每镜只承担该句属于自己的那一段。
    """
    ov_s, ov_e = max(s0, w0), min(s1, w1)
    if ov_e <= ov_s:
        return ""
    total = max(1, s1 - s0)
    n = len(txt)
    i0 = int(round(n * (ov_s - s0) / total))
    i1 = int(round(n * (ov_e - s0) / total))
    i0, i1 = max(0, min(i0, n)), max(0, min(i1, n))
    if i1 <= i0:
        return ""
    # 起点向前找最近的标点，避免把词切两半
    if i0 > 0:
        for k in range(i0, max(i0 - 4, 0), -1):
            if txt[k - 1] in "，。、；：！？,.!?;: ":
                i0 = k
                break
    out = txt[i0:i1].strip()
    out = out.lstrip("，。、；：,.!?;: ")
    return out.strip()


def _overlap(a_s: int, a_e: int, b_s: int, b_e: int) -> bool:
    return max(a_s, b_s) < min(a_e, b_e)


def validate_intervals(shots: List[Shot], duration_ms: int) -> List[str]:
    """校验并返回问题清单（预处理产物一般已合规，这里做兜底）。"""
    logs = []
    prev_end = 0
    for sh in shots:
        if sh.start_ms < 0:
            logs.append(f"{sh.shot_id}: start<0")
            sh.start_ms = 0
        if sh.end_ms > duration_ms:
            logs.append(f"{sh.shot_id}: end 越界")
            sh.end_ms = duration_ms
        if sh.start_ms < prev_end:
            logs.append(f"{sh.shot_id}: 与前镜重叠，已顺延")
            sh.start_ms = prev_end
        if sh.end_ms <= sh.start_ms:
            logs.append(f"{sh.shot_id}: 非法区间，已修正")
            sh.end_ms = sh.start_ms + 1
        prev_end = sh.end_ms
    return logs


def align_raw_to_shots(shots: List[Shot], raw: Dict, cfg: Dict = None) -> List[dict]:
    """把子技能结果拼装成 9 字段记录。

    分工：台词与环境音/音乐都用整段音频识别（带绝对时间戳），按时间窗归属到各镜头；
    画面逐镜头走 VL。台词归属方式由 speech.assign 控制：
      slice = 按重叠比例把句子切给各镜（每镜承担自己那一段，但会切断词）
      whole = 整句只挂在句首镜，后镜标"承上句"（不断词，但后镜信息少）
    """
    speech_mode = str(((cfg or {}).get("speech") or {}).get("assign", "whole"))
    vl_map = {v.get("shot_id"): v for v in (raw.get("vl") or []) if isinstance(v, dict)}
    audio_map = raw.get("audio") or {}
    asr_obj = raw.get("asr") or {}
    asr_all = asr_obj.get("segments") or []
    asr_review_all = bool(asr_obj.get("needs_review"))
    sed_obj = raw.get("sed") or {}
    sed_events = sed_obj.get("events") or []
    music_obj = raw.get("music") or {}
    sed_review = bool(sed_obj.get("needs_review"))
    music_present = bool(music_obj.get("present"))
    music_review = bool(music_obj.get("needs_review"))

    # 音乐描述（不含时间），全片同一段 BGM 就复用同一句
    if music_present:
        parts = []
        for key, label in (("style", "曲风"), ("emotion", "情绪"),
                           ("instruments", "乐器"), ("tempo", "节奏"),
                           ("dynamics", "强弱")):
            val = normalize_text(music_obj.get(key), "")
            if val and val != "无":
                parts.append(f"{label}{val}")
        music_desc = "，".join(parts) or "有背景音乐"
    else:
        music_desc = ""
    music_segs = music_obj.get("segments") or []

    def _music_for(sh: Shot) -> str:
        if music_review:
            return "无法识别"
        if not music_present:
            return "无"
        # 有分段时按段时间归属；无分段视为全片持续
        if music_segs and not any(_overlap(int(g.get("start_ms", 0)), int(g.get("end_ms", 0)),
                                           sh.start_ms, sh.end_ms) for g in music_segs):
            return "无"
        return music_desc

    # 句首镜归属（whole 模式用）：一句话的起点落在哪个镜头，整句就挂在哪里
    owner_of = {}
    for idx, seg in enumerate(asr_all):
        s0 = int(seg.get("start_ms", 0))
        sid = next((sh.shot_id for sh in shots if sh.start_ms <= s0 < sh.end_ms), None)
        owner_of[idx] = sid or (shots[-1].shot_id if shots else "")

    # 台词定类：先按句子判，再用全片多数补齐"口型看不清"的句子。
    # 按句而不是按镜判定，否则同一句解说跨镜时会被不同镜头的口型判成两种标签。
    seg_labels: List[str] = []
    for seg in asr_all:
        s0, s1 = int(seg.get("start_ms", 0)), int(seg.get("end_ms", 0))
        mouths = [normalize_mouth((vl_map.get(st.shot_id) or {}).get("mouth_state"))
                  for st in shots if _overlap(s0, s1, st.start_ms, st.end_ms)]
        seg_labels.append(_speech_label(seg.get("voice"), mouths))
    known = [x for x in seg_labels if x]
    default_label = max(set(known), key=known.count) if known else "人声"
    seg_labels = [x or default_label for x in seg_labels]

    recs = []
    for sh in shots:
        asr_review = asr_review_all

        # 台词：按全局时间戳归属到镜头
        lines = []
        labels: List[str] = []
        for idx, seg in enumerate(asr_all):
            txt = (seg.get("text") or "").strip()
            if not txt:
                continue
            s0, s1 = int(seg.get("start_ms", 0)), int(seg.get("end_ms", 0))
            if not _overlap(s0, s1, sh.start_ms, sh.end_ms):
                continue
            labels.append(seg_labels[idx] if idx < len(seg_labels) else "人声")
            if speech_mode == "whole":
                # 整句不切：只挂在句首镜，跨到的后镜标延续，避免断词与重复
                if owner_of.get(idx) == sh.shot_id:
                    inner = f"<d>{txt}</d>"
                    if s1 > sh.end_ms + 80:
                        inner += "<cutoff>"
                    lines.append(inner)
                else:
                    lines.append(
                        f"<sceneRatio><d>（承上句，见 {owner_of.get(idx)}）</d>")
                continue
            # slice：只取落在本镜时间段内的那一段文字
            part = _slice_text(txt, s0, s1, sh.start_ms, sh.end_ms)
            if not part:
                continue
            inner = f"<d>{part}</d>"
            if s0 < sh.start_ms - 80:
                inner = "<sceneRatio>" + inner   # 句子从上一镜延续进来
            if s1 > sh.end_ms + 80:
                inner += "<cutoff>"              # 句子的后半段落到下一镜
            lines.append(inner)
        if asr_review:
            speech = "无法识别"
        elif lines:
            label = max(set(labels), key=labels.count) if labels else "人声"
            speech = f"{label} " + " / ".join(lines)
        else:
            speech = "无"

        # 环境音：按全局时间戳归属：写成"声源（强度、远近、断续、细节）"
        if sed_review:
            env = "无法识别"
        else:
            items, seen = [], set()
            for e in sed_events:
                t = (e.get("type") or "").strip()
                if not t or t in seen:
                    continue
                if not _overlap(int(e.get("start_ms", 0)), int(e.get("end_ms", 0)),
                                sh.start_ms, sh.end_ms):
                    continue
                seen.add(t)
                tags = []
                for key in ("intensity", "distance", "pattern"):
                    val = (e.get(key) or "").strip()
                    if val:
                        tags.append(val)
                detail = (e.get("detail") or "").strip()
                if detail:
                    tags.append(detail)
                inner = "、".join(tags)
                emo = (e.get("emotion") or "").strip()
                if emo:
                    inner = f"{inner}；情绪{emo}" if inner else f"情绪{emo}"
                items.append(f"{t}（{inner}）" if inner else t)
            env = "；".join(items) if items else "无"

        music_str = _music_for(sh)

        # 画面
        v = vl_map.get(sh.shot_id, {})
        size = normalize_text(v.get("shot_size"), "无法识别")
        angle = normalize_text(v.get("angle_camera_composition"), "无法识别")
        move = normalize_text(v.get("camera_movement"), "无法识别")
        content = normalize_text(v.get("content_info"), "无法识别")
        review = (any(x == "无法识别" for x in (size, angle, move, content))
                  or bool(v.get("needs_review"))
                  or asr_review or sed_review or music_review)

        recs.append({
            "shot_number": sh.shot_id,
            "time_range": format_range(sh.start_ms, sh.end_ms),
            "shot_size": size,
            "angle_camera_composition": angle,
            "camera_movement": move,
            "content_info": content,
            "env_sound": env,
            "music": music_str,
            "speech": speech,
            "needs_review": review,
        })
    return recs


def build_caption(overview: str, recs: List[dict]) -> str:
    lines = [f"总体信息：{overview}", "分镜表："]
    for r in recs:
        lines += [
            f"  {r['shot_number']}",
            f"    时间段：{r['time_range']}",
            f"    景别：{r['shot_size']}",
            f"    角度/机位/构图：{r['angle_camera_composition']}",
            f"    运镜：{r['camera_movement']}",
            f"    内容信息：{r['content_info']}",
            f"    环境音/特殊音效：{r['env_sound']}",
            f"    音乐：{r['music']}",
            f"    人声/台词：{r['speech']}",
        ]
    return "\n".join(lines)


REQUIRED_FIELDS = [
    "shot_number", "time_range", "shot_size", "angle_camera_composition",
    "camera_movement", "content_info", "env_sound", "music", "speech",
]


def build_review_hints(recs: List[dict], raw: Dict) -> List[dict]:
    """汇总"建议人工复核"的位置。

    只在机器判断确实不可靠的地方打标，其余内容视为可信，避免满屏提示：
      1. 台词里的专有名词被字幕覆盖，且两边写法完全不同（改得最多，最需人看）；
      2. 台词里出现名字但画面字幕找不到依据，没能校正；
      3. 全片只有一两条声音事件覆盖整段——各镜环境音必然相同，属整片声场；
      4. 内容信息明显短于其他镜头，可能被截断。
    """
    hints: List[dict] = []
    cal = (raw.get("asr") or {}).get("calibration") or {}

    for a in cal.get("applied") or []:
        if not a.get("low_confidence"):
            continue
        hints.append({
            "shot": a.get("shot") or "", "field": "台词", "at": a.get("time") or "",
            "before": a.get("asr") or "", "after": a.get("final") or "",
            "level": "high",
            "reason": "语音转写与画面字幕写法完全不同，已按字幕覆盖",
        })
    for u in cal.get("unresolved") or []:
        u = u if isinstance(u, dict) else {"term": str(u), "why": ""}
        hints.append({
            "shot": "", "field": "台词", "at": "",
            "before": u.get("term") or "", "after": "", "level": "mid",
            "reason": u.get("why") or "画面字幕中找不到对应写法，未做校正",
        })
    if cal.get("error") and "字幕" in str(cal["error"]):
        hints.append({"shot": "", "field": "台词", "at": "", "before": "", "after": "",
                      "level": "mid", "reason": "本片无字幕可校准，人名按语音原样保留"})

    evs = (raw.get("sed") or {}).get("events") or []
    if len(evs) <= 2 and len(recs) >= 3:
        hints.append({
            "shot": "", "field": "环境音", "at": "", "before": "", "after": "",
            "level": "low",
            "reason": (f"全片仅识别到 {len(evs)} 条声音事件且覆盖整段，"
                       "各镜环境音相同，属整片声场而非逐镜差异"),
        })

    for r in recs:
        ci = str(r.get("content_info") or "")
        n = len(ci)
        if n >= 400:
            continue
        # 黑场、纯色、空镜本来就没有可写的内容，短是正常的，不该报"可能截断"
        if ci.count("无") >= 10 or "纯黑" in ci or "黑场" in ci:
            continue
        hints.append({"shot": r.get("shot_number") or "", "field": "内容信息",
                      "at": "", "before": "", "after": "", "level": "mid",
                      "reason": f"本镜描述仅 {n} 字，明显短于其他镜头，可能被截断"})
    return hints


def finalize(overview: str, recs: List[dict], duration_sec: float,
             subtask_id: str, filename: str, raw: Dict, self_check: Dict = None) -> dict:
    # 强制非空
    for r in recs:
        for k in REQUIRED_FIELDS:
            if not isinstance(r.get(k), str) or not r[k].strip():
                r[k] = "无法识别" if k in (
                    "shot_size", "angle_camera_composition", "camera_movement", "content_info"
                ) else "无"

    review_flags = [r["shot_number"] for r in recs if r.get("needs_review")]
    sc = self_check or {}
    for sid in sc.get("suspects") or []:
        if sid not in review_flags:
            review_flags.append(sid)
    shots_out = [{k: r[k] for k in REQUIRED_FIELDS} for r in recs]
    return {
        "video_meta": {
            "filename": filename,
            "duration_sec": round(float(duration_sec), 3),
            "total_shots": len(recs),
            "subtask_id": subtask_id,
        },
        "shots": shots_out,
        "overview": overview,
        "full_caption": build_caption(overview, recs),
        "raw_subskills": raw,
        "review_flags": review_flags,
        "review_hints": build_review_hints(recs, raw),
        "self_check": sc,
    }
