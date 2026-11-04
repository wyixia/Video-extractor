"""编排调度：P0 预处理(串行) → P1 并行(ASR/SED/Music + VL 逐shot) → P2 合并校验 → 返回最终 JSON。"""
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

from .clients import BaseClient, audio_data_url, build_client
from .config import resolve_path
from .preprocess import preprocess
from .skills import (Skill1ASR, Skill2SED, Skill3Music, Skill4VL, Skill5Merge,
                     calibrate_names)


class Pipeline:
    def __init__(self, cfg: dict, client: BaseClient = None, on_event=None):
        self.cfg = cfg
        self.client = client or build_client(cfg)

        # 每个角色可能落在不同端点上（音频一家、视觉另一家，密钥各管各的），
        # 所以按角色取客户端；单端点时拿到的都是同一个实例。
        # 这里不吞异常：端点写错、密钥缺失要在任务开始时就说清楚，而不是跑一半才报。
        def pick(role: str) -> BaseClient:
            fn = getattr(self.client, "for_role", None)
            return fn(role) if fn else self.client

        self.s1 = Skill1ASR(pick("asr"), cfg)
        self.s2 = Skill2SED(pick("sed"), cfg)
        self.s3 = Skill3Music(pick("music"), cfg)
        self.s4 = Skill4VL(pick("vl"), cfg)
        self.s5 = Skill5Merge(pick("merge"), cfg)
        self.calibrate_client = pick("calibrate")
        # 进度回调：图形界面靠它显示阶段与剩余量。不传就是空函数，行为与原来一致。
        self.on_event = on_event or (lambda e: None)

    def _emit(self, stage: str, progress: float, detail: str = ""):
        try:
            self.on_event({"stage": stage, "progress": round(min(1.0, progress), 3),
                           "detail": detail})
        except Exception:
            pass  # 界面出错不能影响标注本身

    def run(self, video: str, subtask_id: str = "") -> dict:
        video = str(video)
        workdir = resolve_path(self.cfg["paths"]["workdir"])

        # ---- P0 预处理 ----
        self._emit("preprocess", 0.02, "读取时长、切分镜头")
        shots, duration, audio_chunks = preprocess(video, self.cfg, workdir)
        self._emit("preprocess", 0.15, f"{len(shots)} 个镜头，{duration:.1f} 秒")

        # ---- P1 并行 ----
        # 分工原则：台词逐镜头切（转写必须能对齐到镜头边界）；
        # 环境音/音乐用大段音频（单镜 1-2s 缺少上下文，声源会判错）。
        raw: Dict = {"vl": [], "audio": {}}
        asr_segs: List[dict] = []
        sed_events: List[dict] = []
        music_parts: List[Tuple[int, dict]] = []
        asr_failed = False
        sed_failed = False
        max_workers = int(self.cfg["runtime"]["max_workers"])
        sk = self.cfg["skills"]

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            jobs = {}
            ctx_map = self._neighbor_ctx(shots)
            for sh in shots:
                if sk["vl"]["enabled"]:
                    jobs[pool.submit(self._vl, sh, ctx_map.get(sh.shot_id, ""))] = ("vl", sh.shot_id)
            if any(sk[k]["enabled"] for k in ("asr", "sed", "music")):
                for i, ch in enumerate(audio_chunks):
                    jobs[pool.submit(self._audio_chunk, ch)] = ("chunk", i)

            total_jobs = max(1, len(jobs))
            done_jobs = 0
            for fut in as_completed(jobs):
                done_jobs += 1
                kind, key = jobs[fut]
                self._emit("analyze", 0.15 + 0.65 * done_jobs / total_jobs,
                           f"画面与声音识别 {done_jobs}/{total_jobs}")
                try:
                    res = fut.result()
                except Exception:
                    res = None

                if kind == "vl":
                    raw["vl"].append(res if res
                                     else self._degrade(("vl", key), Exception("missing")))
                else:
                    ch = audio_chunks[key]
                    if not res:
                        if sk["sed"]["enabled"]:
                            sed_failed = True
                        continue
                    off = int(ch["start_ms"])
                    asr = res.get("asr")
                    if asr:
                        for seg in asr.get("segments") or []:
                            seg["start_ms"] = int(seg.get("start_ms", 0)) + off
                            seg["end_ms"] = int(seg.get("end_ms", 0)) + off
                            asr_segs.append(seg)
                    elif sk["asr"]["enabled"]:
                        asr_failed = True
                    sed = res.get("sed")
                    if sed:
                        for e in sed.get("events") or []:
                            e["start_ms"] = int(e.get("start_ms", 0)) + off
                            e["end_ms"] = int(e.get("end_ms", 0)) + off
                            sed_events.append(e)
                    elif sk["sed"]["enabled"]:
                        sed_failed = True
                    mus = res.get("music")
                    if mus:
                        music_parts.append((off, mus))

        for sh in shots:
            raw["audio"].setdefault(sh.shot_id, {})

        asr_segs.sort(key=lambda x: int(x.get("start_ms", 0)))
        raw["asr"] = {"task": "asr", "segments": asr_segs, "needs_review": asr_failed}
        raw["sed"] = {"task": "sed", "events": sed_events, "needs_review": sed_failed}
        raw["music"] = self._merge_music(music_parts)

        # ---- P1.5 补跑 ----
        # 端点限流多为瞬时：VL 缺字段的镜头串行补跑；音频整体失败则重跑一次大段识别。
        def _vl_of(sid):
            return next((x for x in raw["vl"]
                         if isinstance(x, dict) and x.get("shot_id") == sid), None)

        repair = [sh for sh in shots
                  if (lambda v: v is None or bool(v.get("needs_review")))(_vl_of(sh.shot_id))]

        if repair:
            self._emit("repair", 0.82, f"补跑 {len(repair)} 个镜头")
        for sh in repair:
            time.sleep(2)
            try:
                new_v = self._vl(sh, ctx_map.get(sh.shot_id, ""))
            except Exception:
                new_v = None
            if new_v and not new_v.get("needs_review"):
                raw["vl"] = [x for x in raw["vl"]
                             if not (isinstance(x, dict) and x.get("shot_id") == sh.shot_id)]
                raw["vl"].append(new_v)

        if (asr_failed or sed_failed) and audio_chunks:
            r_asr: List[dict] = []
            r_sed: List[dict] = []
            for ch in audio_chunks:
                off = int(ch["start_ms"])
                if asr_failed and sk["asr"]["enabled"]:
                    try:
                        time.sleep(2)
                        r = self.s1.run(ch["path"])
                        for seg in r.get("segments") or []:
                            seg["start_ms"] = int(seg.get("start_ms", 0)) + off
                            seg["end_ms"] = int(seg.get("end_ms", 0)) + off
                            r_asr.append(seg)
                    except Exception:
                        pass
                if sed_failed and sk["sed"]["enabled"]:
                    try:
                        time.sleep(2)
                        r = self.s2.run(ch["path"])
                        for e in r.get("events") or []:
                            e["start_ms"] = int(e.get("start_ms", 0)) + off
                            e["end_ms"] = int(e.get("end_ms", 0)) + off
                            r_sed.append(e)
                    except Exception:
                        pass
            if r_asr:
                r_asr.sort(key=lambda x: int(x.get("start_ms", 0)))
                raw["asr"] = {"task": "asr", "segments": r_asr, "needs_review": False}
            if r_sed:
                raw["sed"] = {"task": "sed", "events": r_sed, "needs_review": False}

        if repair:
            raw["repair_attempted"] = [sh.shot_id for sh in repair]

        # 补跑可能打乱顺序，按镜头顺序重排，保证下游按序取用
        order = {sh.shot_id: i for i, sh in enumerate(shots)}
        raw["vl"].sort(key=lambda x: order.get((x or {}).get("shot_id"), 999))

        # ---- P1.6 专有名词校正（自适应：有字幕就校准，没字幕自动跳过） ----
        # 是否需要校准由素材自己决定，不设开关：calibrate_names 内部判字幕行，
        # 抽不到字幕就直接返回，不调用模型、不改一个字。失败也不阻断主流程。
        if sk["asr"]["enabled"] and (raw.get("asr") or {}).get("segments"):
            self._emit("calibrate", 0.9, "核对画面字幕与转写")
            raw["asr"]["calibration"] = calibrate_names(self.calibrate_client, self.cfg, raw, shots)

        # ---- P2 合并校验 ----
        self._emit("merge", 0.94, "拼装字段、写总览")
        result = self.s5.run(shots, raw, duration, subtask_id, Path(video).name)
        self._emit("done", 1.0, f"{len(shots)} 个镜头")
        return result

    # ---- 单任务包装 ----
    def _audio_chunk(self, chunk: dict) -> dict:
        """一个大段音频块上依次跑 ASR / SED / Music，块内串行（避免同块互抢配额）。"""
        sk = self.cfg["skills"]
        out: Dict = {}
        if sk["asr"]["enabled"]:
            try:
                out["asr"] = self.s1.run(chunk["path"])
            except Exception as e:
                out["asr_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        if sk["sed"]["enabled"]:
            try:
                out["sed"] = self.s2.run(chunk["path"])
            except Exception as e:
                out["sed_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        if sk["music"]["enabled"]:
            try:
                out["music"] = self.s3.run(chunk["path"])
            except Exception as e:
                out["music_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return out

    @staticmethod
    def _merge_music(parts: List[Tuple[int, dict]]) -> dict:
        """多个音频块的音乐结果合并成一条：任一块有 BGM 即视为有。"""
        base = {"task": "music", "present": False, "style": "", "emotion": "",
                "instruments": "", "tempo": "", "dynamics": "", "segments": []}
        if not parts:
            return base
        hits = [(off, m) for off, m in parts if m.get("present")]
        if not hits:
            # 全部判无，但任一块调用出错要标"无法识别"，不能当成"无"
            if any("music_error" in m for _, m in parts):
                base["needs_review"] = True
            return base
        out = {"task": "music", "present": True}
        for k in ("style", "emotion", "instruments", "tempo", "dynamics"):
            v = next((m.get(k) for _, m in hits if m.get(k)), "")
            out[k] = v
        segs = []
        for off, m in hits:
            for s in m.get("segments") or []:
                if "start_ms" in s:
                    s["start_ms"] = int(s["start_ms"]) + off
                    s["end_ms"] = int(s.get("end_ms", 0)) + off
                segs.append(s)
        out["segments"] = segs
        return out

    @staticmethod
    def _neighbor_ctx(shots) -> Dict[str, str]:
        """给每个镜头拼一段相邻镜头信息。

        单镜只看自己的帧，判断不了"这是不是快切""处在段落什么位置"。
        补上前后镜时长与序号，模型才能把 0.79 秒的短镜识别为插入切镜。
        只给时长这类客观元数据，不给画面描述，避免先入为主地套用。
        """
        total = len(shots)
        if not total:
            return {}
        durs = [(s.end_ms - s.start_ms) / 1000.0 for s in shots]
        avg = sum(durs) / total
        out: Dict[str, str] = {}
        for i, sh in enumerate(shots):
            d = durs[i]
            lines = ["相邻镜头信息（客观时长，仅作节奏参考，不要写进画面描述）：",
                     f"全片共 {total} 个镜头，平均每镜 {avg:.2f} 秒。"]
            if i > 0:
                lines.append(f"上一镜 {shots[i - 1].shot_id}：{durs[i - 1]:.2f} 秒。")
            else:
                lines.append("本镜是全片第一个镜头。")
            lines.append(f"本镜是第 {i + 1} 个，时长 {d:.2f} 秒。")
            if i < total - 1:
                lines.append(f"下一镜 {shots[i + 1].shot_id}：{durs[i + 1]:.2f} 秒。")
            else:
                lines.append("本镜是全片最后一个镜头。")
            if avg > 0 and d < avg * 0.6:
                lines.append("本镜明显短于全片平均，可能是快切插入镜。")
            elif avg > 0 and d > avg * 1.8:
                lines.append("本镜明显长于全片平均，可能承担停顿或情绪铺陈。")
            out[sh.shot_id] = "\n".join(lines)
        return out

    def _vl(self, shot, context: str = "") -> dict:
        return self.s4.run(shot, context)

    # ---- 文件引用 ----
    def _audio_ref(self, path: str) -> str:
        """Omni 支持 data URL；若配了 OSS 则优先用公网 URL。"""
        if self.cfg.get("provider") == "mock":
            return "mock://audio.wav"
        if self.cfg.get("storage", {}).get("type") == "oss":
            return self._upload_oss(path)
        return audio_data_url(path)

    def _resolve_audio_url(self, path: str) -> str:
        """ASR 异步任务只接受公网 URL。"""
        if self.cfg.get("provider") == "mock":
            return "mock://vocals.wav"
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if self.cfg.get("storage", {}).get("type") == "oss":
            return self._upload_oss(path)
        raise RuntimeError(
            "ASR 需要公网可访问的音频 URL。请配置 storage.type=oss 并提供 OSS 凭据，"
            "或使用 provider=mock 做离线联调。"
        )

    def _upload_oss(self, path: str) -> str:
        try:
            import oss2
        except ImportError as e:
            raise RuntimeError("需要安装 oss2: pip install oss2") from e

        o = self.cfg["storage"]["oss"]
        ak = os.getenv(o["access_key_env"])
        sk = os.getenv(o["secret_key_env"])
        if not (ak and sk and o.get("bucket") and o.get("endpoint")):
            raise RuntimeError("OSS 配置不完整（bucket/endpoint/凭据）")

        auth = oss2.Auth(ak, sk)
        bucket = oss2.Bucket(auth, o["endpoint"], o["bucket"])
        key = f"vsa-tmp/{uuid.uuid4().hex}_{Path(path).name}"
        bucket.put_object_from_file(key, path)
        host = o["endpoint"].replace("https://", "").replace("http://", "").rstrip("/")
        return f"https://{o['bucket']}.{host}/{key}"

    # ---- 降级 ----
    @staticmethod
    def _degrade(key, err) -> dict:
        msg = str(err)
        if key == "asr":
            return {"task": "asr", "segments": [], "needs_review": True, "error": msg}
        if key == "sed":
            return {"task": "sed", "events": [], "needs_review": True, "error": msg}
        if key == "music":
            return {"task": "music", "present": False, "needs_review": True, "error": msg}
        if isinstance(key, tuple) and key[0] == "vl":
            d = {"task": "vl", "shot_id": key[1], "needs_review": True, "error": msg}
            d.update({k: "无法识别" for k in
                      ("shot_size", "angle_camera_composition", "camera_movement", "content_info")})
            return d
        return {"error": msg}
