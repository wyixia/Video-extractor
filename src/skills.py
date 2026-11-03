"""5 个子 Skill 的薄封装。每个 Skill 独立、可单独开关，依赖 BaseClient 接口。"""
import difflib
import json
import re
from typing import Dict, List

from .clients import BaseClient, extract_json, image_data_url, audio_data_url, retry_call
from .config import read_prompt
from .models import Shot, format_range
from . import validate as V

VISUAL_KEYS = ("shot_size", "angle_camera_composition", "camera_movement", "content_info")


def _chat(client: BaseClient, cfg: dict, model: str, messages: List[dict], **kwargs) -> str:
    """统一带重试的对话调用。

    config.runtime.retry / retry_backoff_sec 在这里真正生效；此前 retry_call
    虽已定义但从未被调用，导致一次 429 就整镜降级。
    """
    rt = cfg.get("runtime", {}) or {}
    retries = int(rt.get("retry", 2))
    backoff = tuple(rt.get("retry_backoff_sec") or [2, 4])
    return retry_call(lambda: client.chat(model, messages, **kwargs), retries, backoff)


class Skill1ASR:
    def __init__(self, client: BaseClient, cfg: dict):
        self.c, self.cfg = client, cfg

    def run(self, audio_path: str) -> dict:
        messages = [
            {"role": "system", "content": read_prompt("asr")},
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": audio_data_url(audio_path)}},
                {"type": "text", "text": "转写这段音频中的人声/台词，按 Schema 只输出 JSON（时间用相对片段起点的秒，小数）。"},
            ]},
        ]
        text = _chat(self.c, self.cfg, self.cfg["models"]["asr"], messages,
                           timeout=self.cfg["runtime"]["call_timeout_sec"],
                           enable_thinking=bool(self.cfg["runtime"].get("audio_enable_thinking", False)))
        obj = extract_json(text)
        segs = []
        for s in obj.get("segments", []) or []:
            segs.append({
                "start_ms": int(round(float(s.get("start_time", 0)) * 1000)),
                "end_ms": int(round(float(s.get("end_time", 0)) * 1000)),
                "text": (s.get("text") or "").strip(),
                "voice": V.normalize_voice(s.get("voice")),
            })
        return {"task": "asr", "segments": segs}


class Skill2SED:
    def __init__(self, client: BaseClient, cfg: dict):
        self.c, self.cfg = client, cfg

    def run(self, audio_path: str) -> dict:
        messages = [
            {"role": "system", "content": read_prompt("sed")},
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": audio_data_url(audio_path)}},
                {"type": "text", "text": "识别这段音频中的环境音与特殊音效，按 Schema 只输出 JSON（时间用相对片段起点的秒）。"},
            ]},
        ]
        text = _chat(self.c, self.cfg, self.cfg["models"]["sed"], messages,
                           timeout=self.cfg["runtime"]["call_timeout_sec"],
                           enable_thinking=bool(self.cfg["runtime"].get("audio_enable_thinking", False)))
        obj = extract_json(text)
        events = []
        for e in obj.get("events", []) or []:
            if "start_time" in e:  # 模型按秒返回
                e["start_ms"] = int(round(float(e.pop("start_time")) * 1000))
                e["end_ms"] = int(round(float(e.pop("end_time")) * 1000))
            e["start_ms"] = int(e.get("start_ms", 0))
            e["end_ms"] = int(e.get("end_ms", 0))
            if e.get("type"):
                events.append(e)
        return {"task": "sed", "events": events}


class Skill3Music:
    def __init__(self, client: BaseClient, cfg: dict):
        self.c, self.cfg = client, cfg

    def run(self, audio_path: str) -> dict:
        messages = [
            {"role": "system", "content": read_prompt("music")},
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": audio_data_url(audio_path)}},
                {"type": "text", "text": "分析这段音频中的背景音乐，按 Schema 只输出 JSON。"},
            ]},
        ]
        text = _chat(self.c, self.cfg, self.cfg["models"]["music"], messages,
                           timeout=self.cfg["runtime"]["call_timeout_sec"],
                           enable_thinking=bool(self.cfg["runtime"].get("audio_enable_thinking", False)))
        obj = extract_json(text)
        obj["task"] = "music"
        obj["present"] = bool(obj.get("present"))
        for s in obj.get("segments", []) or []:
            if "start_time" in s:
                s["start_ms"] = int(round(float(s.pop("start_time")) * 1000))
                s["end_ms"] = int(round(float(s.pop("end_time")) * 1000))
        return obj


class Skill4VL:
    def __init__(self, client: BaseClient, cfg: dict):
        self.c, self.cfg = client, cfg

    def run(self, shot: Shot, context: str = "") -> dict:
        if not shot.frames:
            return self._degraded(shot, "no frames")

        content = [{"type": "image_url", "image_url": {"url": image_data_url(f)}} for f in shot.frames]
        extra = f"\n镜头时间段(仅上下文，不参与判断): {format_range(shot.start_ms, shot.end_ms)}"
        if context:
            extra += "\n" + context
        if shot.motion:
            extra += ("\n运镜测量证据（CPU 全局运动估计，判断运镜时以此为准，"
                      "不要凭主观感觉改判；若与画面明显矛盾再说明）:\n  " + shot.motion["evidence"])
        content.append({"type": "text", "text": read_prompt("vl") + extra})
        messages = [
            {"role": "system", "content": "你是影视分镜标注师，只输出 JSON。"},
            {"role": "user", "content": content},
        ]
        text = _chat(self.c, self.cfg, self.cfg["models"]["vl"], messages,
                           timeout=self.cfg["runtime"]["call_timeout_sec"],
                           max_tokens=int(self.cfg["runtime"].get("vl_max_tokens", 1600)))
        try:
            obj = extract_json(text)
        except Exception:
            # 输出被截断时，加大 token 预算重试一次；只允许保守精简，不允许丢关键信息
            messages[-1]["content"][-1]["text"] += (
                "\n上次输出未闭合。请输出完整闭合的 JSON；若篇幅紧张，"
                "可精简修饰语，但必须保留主体动作、构图分层、可见文字三类信息。")
            text = _chat(self.c, self.cfg, self.cfg["models"]["vl"], messages,
                               timeout=self.cfg["runtime"]["call_timeout_sec"],
                               max_tokens=int(self.cfg["runtime"].get("vl_max_tokens", 2600)) + 1200)
            obj = extract_json(text)
        for k in VISUAL_KEYS:
            obj[k] = V.normalize_text(obj.get(k), "无法识别")
        obj["mouth_state"] = V.normalize_mouth(obj.get("mouth_state"))
        obj["task"] = "vl"
        obj["shot_id"] = shot.shot_id
        if shot.motion:
            obj["motion"] = shot.motion
        return obj

    @staticmethod
    def _degraded(shot: Shot, reason: str) -> dict:
        d = {"task": "vl", "shot_id": shot.shot_id, "needs_review": True, "error": reason,
             "mouth_state": "unknown"}
        d.update({k: "无法识别" for k in VISUAL_KEYS})
        return d


class Skill5Merge:
    def __init__(self, client: BaseClient, cfg: dict):
        self.c, self.cfg = client, cfg

    def run(self, shots: List[Shot], raw: Dict, duration_sec: float,
            subtask_id: str, filename: str) -> dict:
        # 1) 确定性对齐（时间归属、非空兜底、台词标记）先做，不依赖 LLM
        recs = V.align_raw_to_shots(shots, raw, self.cfg)

        # 2) LLM 补三块代码判断不了的内容：全片总览、每镜叙事作用、音画综合情绪
        overview = None
        emotion_arc = ""
        pacing = ""
        shot_notes: Dict[str, dict] = {}
        self_check = {}
        merge_error = None
        try:
            payload = {
                "duration_sec": round(duration_sec, 3),
                "shots": [{
                    "shot_number": r["shot_number"],
                    "time_range": r["time_range"],
                    "shot_size": r["shot_size"],
                    "camera_movement": r["camera_movement"],
                    "content": ((r["content_info"] or "")[:600]
                                + "……【此处仅为节选，原字段更长，不要据此判断内容被截断】"),
                    "env_sound": (r["env_sound"] or "")[:200],
                    "music": (r["music"] or "")[:120],
                    "speech": (r["speech"] or "")[:200],
                } for r in recs],
            }
            messages = [
                {"role": "system", "content": read_prompt("merge")},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            text = _chat(self.c, self.cfg, self.cfg["models"]["merge"], messages,
                               timeout=self.cfg["runtime"]["merge_timeout_sec"],
                               max_tokens=int(self.cfg["runtime"].get("merge_max_tokens", 2500)),
                               enable_thinking=bool(self.cfg["runtime"].get("merge_enable_thinking", False)))
            obj = extract_json(text)
            overview = (obj.get("overview") or "").strip() or None
            emotion_arc = (obj.get("emotion_arc") or "").strip()
            pacing = (obj.get("pacing") or "").strip()
            for s in obj.get("shots") or []:
                if isinstance(s, dict) and s.get("shot_number"):
                    shot_notes[str(s["shot_number"]).strip()] = s
            if isinstance(obj.get("self_check"), dict):
                self_check = obj["self_check"]
                # 过滤假的"截断"报警：payload 里 content 只放了 600 字节选，
                # 模型看到句尾断在半个词上就判"截断"。原字段足够长的一律不算。
                long_ids = {r["shot_number"] for r in recs
                            if len(r.get("content_info") or "") > 700}
                kept_issues = []
                for it in self_check.get("issues") or []:
                    s = str(it)
                    if "截断" in s and any(sid in s for sid in long_ids):
                        continue
                    kept_issues.append(it)
                self_check["issues"] = kept_issues
        except Exception as e:  # 降级：代码默认总览，不阻断
            merge_error = f"{type(e).__name__}: {str(e)[:200]}"

        # 3) 把叙事作用与音画综合追加到内容信息末尾（编号接在画面子项 11 之后）
        filled = 0
        for r in recs:
            note = shot_notes.get(r["shot_number"])
            if not note:
                continue
            extra = []
            role = (note.get("narrative_role") or "").strip().rstrip("。；; ")
            syn = (note.get("emotion_synthesis") or "").strip().rstrip("。；; ")
            if role:
                extra.append(f"12. 叙事功能：{role}")
            if syn:
                extra.append(f"13. 音画综合：{syn}")
            if not extra:
                continue
            base = (r["content_info"] or "").rstrip("。；; ")
            r["content_info"] = (base + "。" if base else "") + "；".join(extra) + "。"
            filled += 1

        if not overview:
            overview = f"共 {len(shots)} 个镜头，总时长 {duration_sec:.2f} 秒。"
            if merge_error:
                self_check.setdefault("issues", []).append(f"总览生成失败，已用兜底文案（{merge_error}）")

        for part in (emotion_arc, pacing):
            if part:
                overview = overview.rstrip("。 ") + "。" + part

        # 专有名词校准结果落进自检，报告底部能看到改了什么、哪条需要人复核
        cal = (raw.get("asr") or {}).get("calibration") or {}
        if cal.get("applied"):
            detail = "；".join(
                f"{a['asr']}→{a['final']}" + ("（低置信，需复核）" if a.get("low_confidence") else "")
                for a in cal["applied"])
            self_check.setdefault("issues", []).append(f"专有名词按画面字幕校正：{detail}")
        elif cal.get("error"):
            self_check.setdefault("issues", []).append(f"台词未做字幕校准：{cal['error']}")

        raw["merge"] = {
            "overview": overview,
            "emotion_arc": emotion_arc,
            "pacing": pacing,
            "shots_filled": filled,
            "self_check": self_check,
            "error": merge_error,
        }
        return V.finalize(overview, recs, duration_sec, subtask_id, filename, raw, self_check)


# ---------- 专有名词双向校准（画面字幕 ⇄ 语音转写） ----------



_SUB_NOISE = ("淡入", "淡出", "切换", "无字幕", "字幕层", "字幕位置", "字幕不可")


def _has_sub_text(p: str) -> bool:
    """判断这句是不是"真的带着字幕文字"，而不是在描述字幕的行为。

    实测踩过：只看关键词会把"无字幕、无中英文台词""字幕由右上淡出"
    "第3帧无字幕"这类句子当成字幕内容，喂给校对模型后它只能配出空结果。
    """
    if any(k in p for k in _SUB_NOISE):
        return False
    if re.search(r"[“\"'「『][^”\"'」』]{2,}", p):
        return True
    # 没有引号时，要求冒号后还有实际文字，避免"可见文字：无"这类空值
    m = re.search(r"[：:]\s*(.{4,})", p)
    return bool(m)


# 子项切分：编号写法不固定（`7.` `7_` `7、`），名称统一是 2–10 个汉字。
# 位置不可靠，但"编号 + 中文名 + 冒号"这个形状是稳定的，用它切段最可靠。
_ITEM_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[\.、_\-]\s*([\u4e00-\u9fa5]{2,10})\s*[：:]")


def _split_items(ci: str) -> List[str]:
    """按编号把 content_info 切成子项正文，返回名称为「可见文字」的那些。"""
    ms = list(_ITEM_RE.finditer(ci))
    out: List[str] = []
    for i, m in enumerate(ms):
        if "可见文字" not in m.group(2):
            continue
        end = ms[i + 1].start() if i + 1 < len(ms) else len(ci)
        body = ci[m.end():end].strip()
        if body:
            out.append(body)
    return out


def collect_on_screen_text(raw: Dict) -> Dict[str, str]:
    """取出画面里的文字，拆成"字幕/歌词"与"旁证文字"两路。

    三条实测教训：
    1. 按"7. 可见文字"硬匹配会抽空——VL 的编号写法每次都变；
    2. 只按"字幕"关键词也会抽空——歌词类素材它常写成"右上角白色字"；
    3. 含"字幕"二字的句子不一定是字幕，可能是"无字幕""字幕淡出"这类描述。
    所以两路并行：先按「可见文字」子项抽（不依赖编号），再用关键词兜底，
    每条都过 _has_sub_text 判定，描述的句子一律不要。
    """
    subtitle: List[str] = []
    other: List[str] = []
    for v in raw.get("vl") or []:
        if not isinstance(v, dict):
            continue
        ci = (v.get("content_info") or "").strip()
        if not ci:
            continue

        # 路径 A：可见文字子项里的引号文字，基本就是画面上真实存在的字
        for block in _split_items(ci):
            for part in re.split(r"[；;。\n]", block):
                p = part.strip()
                if len(p) >= 3 and _has_sub_text(p):
                    subtitle.append(p)

        # 路径 B：关键词兜底（VL 没写编号、把歌词说成"字幕"时用）
        for part in re.split(r"[；;。\n]", ci):
            p = part.strip()
            if len(p) < 3:
                continue
            if any(k in p for k in ("字幕", "歌词")) and _has_sub_text(p):
                subtitle.append(p)
            elif (any(k in p for k in ("号码", "背号", "记分牌", "比分"))
                  and len(p) <= 45 and not p.startswith("无")):
                other.append(p)

    def _core(x: str) -> str:
        """取引号内的实际内容做去重键，去掉"底部字幕：""英文字幕："这类前缀。"""
        q = re.findall(r"[“\"'「『]([^”\"'」』]{2,80})[”\"'」』]", x)
        return "".join(q) if q else x

    def _uniq(xs: List[str]) -> str:
        out: List[str] = []
        keys: List[str] = []
        for x in xs:
            x = x.strip()
            if len(x) < 3:
                continue
            k = _core(x)
            # 同一句字幕会在多个镜头重复：内容已被包含就丢掉，包含旧内容则替换
            if any(k in o for o in keys):
                continue
            keep = [i for i, o in enumerate(keys) if o not in k]
            keys = [keys[i] for i in keep]
            out = [out[i] for i in keep]
            keys.append(k)
            out.append(x[:400])
        return "\n".join(out)

    return {"subtitle": _uniq(subtitle), "other": _uniq(other)}


def _replace_blocks(asr_term: str, screen_term: str) -> List[tuple]:
    """列出两版文字里真正被替换的片段，用来判断这次改动有多"重"。

    整句相似度会骗人：'张怡宁对瓦尔德内尔' 与 '白民和对瓦尔德内' 相似度 0.59，
    看着像小改，实际是把一个人名整个换掉。只有看差异块才知道改的是哪个词。
    """
    sm = difflib.SequenceMatcher(None, asr_term, screen_term)
    return [(asr_term[i1:i2], screen_term[j1:j2])
            for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag == "replace"]


def _is_risky_change(asr_term: str, screen_term: str) -> bool:
    """差异块里有整词替换（相似度低于 0.4）就算需要人工复核。"""
    blocks = _replace_blocks(asr_term, screen_term)
    if not blocks:
        return False
    for a, b in blocks:
        r = difflib.SequenceMatcher(None, a, b).ratio()
        if r < 0.4:
            return True
    return False


def _merge_pair(asr_term: str, screen_term: str) -> str:
    """把配对的两版文字合并成一份，而不是拿字幕整句覆盖转写。

    直接整串替换会丢字：转写"白民和对瓦尔德内尔"、字幕"白民和对瓦尔德内"
    （字幕漏了"尔"），整串替换的结果是丢掉"尔"。按差异块合并则是——
      replace 块用字幕（错的字换成对的）
      insert 块补上（字幕多出的字，通常是转写漏了）
      delete 块保留（转写多出的字，通常是字幕省略了）
    这样两边更完整的信息都留得下来。
    """
    sm = difflib.SequenceMatcher(None, asr_term, screen_term)
    out: List[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("equal", "replace", "insert"):
            out.append(screen_term[j1:j2])
        else:  # delete：转写有、字幕无，保留
            out.append(asr_term[i1:i2])
    return "".join(out)


def _decide(asr_term: str, screen_term: str):
    """裁决一对候选：怎么处理、最终写法、理由、相似度。

    规则来自实测数据：字幕比语音可靠，但字幕也会读漏字——本片字幕把
    "瓦尔德内尔"读成"瓦尔德内"，而 ASR 的写法反而是对的。所以不能无脑
    采用字幕：
      一方是另一方子串 → 取更长的一边（补全，不丢字）
      相似度较高       → 按字幕（字形比听音可靠）
      完全不同         → 按字幕但标低置信（至少一边严重出错，留复核痕迹）
    """
    if asr_term == screen_term:
        return ("same", asr_term, "两边一致", 1.0)
    ratio = difflib.SequenceMatcher(None, asr_term, screen_term).ratio()
    if screen_term in asr_term:
        return ("keep_asr", asr_term, "字幕疑漏字，保留转写更完整的写法", ratio)
    if asr_term in screen_term:
        return ("use_screen", screen_term, "转写漏字，按字幕补全", ratio)
    if ratio >= 0.4:
        return ("use_screen", screen_term, "音近或漏字，按字幕", ratio)
    return ("use_screen_lowconf", screen_term, "与转写完全不同，按字幕但需复核", ratio)


def calibrate_names(client: BaseClient, cfg: dict, raw: Dict, shots: List[Shot] = None) -> dict:
    """用画面字幕校正转写里的专有名词，双向比对后再裁决。

    只做整词替换，不重写句子。三道校验：错词必须真在转写里、正词必须真在
    画面文字里、差异性质必须判得出来（子串则取长）。任一不满足都不改，
    避免模型"顺手纠正"出不存在的名字。
    画面没有字幕时直接跳过：此时唯一依据是 ASR 自己，改与不改都无法验证。
    """
    segs = [s for s in ((raw.get("asr") or {}).get("segments") or [])
            if isinstance(s, dict) and (s.get("text") or "").strip()]
    log: Dict = {"pairs": [], "applied": [], "kept": [], "rejected": [],
                 "unresolved": [], "source": {}, "error": None}
    if not segs:
        log["error"] = "无转写内容"
        return log

    on = collect_on_screen_text(raw)
    log["source"] = {"subtitle": on["subtitle"][:1200], "other": on["other"][:600]}
    if not on["subtitle"]:
        log["error"] = "画面无字幕，跳过校准（不猜写人名）"
        return log

    plain = "\n".join((s.get("text") or "") for s in segs)
    on_all = on["subtitle"] + "\n" + on["other"]
    payload = {
        "on_screen_subtitle": on["subtitle"][:2500],
        "on_screen_other": on["other"][:800],
        "transcript": "\n".join(f"[{i}] {s.get('text', '')}" for i, s in enumerate(segs))[:4000],
    }
    try:
        messages = [
            {"role": "system", "content": read_prompt("calibrate")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        text = _chat(client, cfg,
                     cfg["models"].get("calibrate") or cfg["models"]["merge"],
                     messages, timeout=cfg["runtime"]["call_timeout_sec"],
                     max_tokens=900, enable_thinking=False)
        obj = extract_json(text)
    except Exception as e:
        log["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return log

    items = obj.get("pairs") or obj.get("names") or []
    # 未能在字幕里找到依据的名字：模型可能给字符串，也可能给 {asr, reason}，统一成结构
    un: List[dict] = []
    for x in (obj.get("unresolved") or [])[:10]:
        if isinstance(x, dict):
            term = str(x.get("asr") or x.get("term") or "").strip()
            why = str(x.get("reason") or "").strip()[:150]
        else:
            term, why = str(x).strip(), ""
        if term:
            un.append({"term": term, "why": why})
    log["unresolved"] = un

    cands = []
    for it in items:
        if not isinstance(it, dict):
            continue
        wrong = _clean_term(it.get("asr"))
        right = _clean_term(it.get("on_screen"))
        item = {"asr": wrong, "on_screen": right,
                "kind": (it.get("kind") or "").strip(),
                "evidence": (it.get("evidence") or "").strip()[:120]}
        log["pairs"].append(item)
        if len(wrong) < 2 or len(right) < 2:
            log["rejected"].append({**item, "why": "术语过短"})
            continue
        if wrong not in plain:
            log["rejected"].append({**item, "why": "转写中未出现该写法"})
            continue
        if _norm(right) not in _norm(on_all):
            log["rejected"].append({**item, "why": "画面文字中无该写法"})
            continue
        cands.append(item)

    # 长词先处理，避免短词先把长词拆开（"瓦尔德" 先于 "瓦尔德内尔"）
    cands.sort(key=lambda x: len(x["asr"]), reverse=True)
    for item in cands:
        action, final, note, ratio = _decide(item["asr"], item["on_screen"])
        # 两边差太多说明模型在硬配（把两句不同的话凑成一对）。
        # 只有它明确标注"听错"（读音相近、字不同）时才放行，否则一律不改。
        if ratio < 0.35 and (item.get("kind") or "") != "听错":
            log["rejected"].append({**item, "why": f"两边差异过大（相似度 {ratio:.2f}），疑非同一处内容"})
            continue
        if action == "same":
            continue
        if action == "keep_asr":
            log["kept"].append({**item, "final": final, "note": note,
                                "similarity": round(ratio, 2)})
            continue
        # 短词整串替换；长句按差异块合并，避免覆盖掉转写里更完整的字
        target = final if len(item["asr"]) <= 6 else _merge_pair(item["asr"], item["on_screen"])
        hit, hit_ms, changed = 0, 0, False
        for s in segs:
            t = s.get("text") or ""
            if item["asr"] in t:
                if not hit:
                    hit_ms = int(s.get("start_ms") or 0)
                new = t.replace(item["asr"], target)
                if new != t:
                    changed = True
                s["text"] = new
                hit += 1
        if hit and not changed:
            # 合并后与原文完全相同（只差标点），不算改动，也不该进复核清单
            log["kept"].append({**item, "final": target, "note": "仅标点或空格差异，未改动",
                                "similarity": round(ratio, 2)})
            continue
        if hit:
            shot_id = ""
            for sh in (shots or []):
                if sh.start_ms <= hit_ms < sh.end_ms:
                    shot_id = sh.shot_id
                    break
            log["applied"].append({**item, "final": target, "note": note,
                                   "low_confidence": (action == "use_screen_lowconf"
                                                      or _is_risky_change(item["asr"],
                                                                          item["on_screen"])),
                                   "similarity": round(ratio, 2), "segments": hit,
                                   "shot": shot_id, "time": f"{hit_ms / 1000:.2f}s"})
    return log


def _norm(s: str) -> str:
    """去掉空格与标点，只留实义字符。校验"画面里到底有没有这个写法"时用。"""
    return re.sub(r"[\s，。、；：,.!?;:·…—\-—\"'“”‘’()（）]", "", str(s or ""))


def _clean_term(t) -> str:
    """清洗候选用词：去括号注音、去引号书名号、去空白。"""
    t = str(t or "").strip()
    t = re.sub(r"[（(][^）)]*[）)]", "", t)
    t = re.sub(r"[\s\"'“”‘’《》<>「」]", "", t)
    return t
