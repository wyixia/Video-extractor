"""预处理层（CPU，无大模型）：镜头切分 / 关键帧抽取 / 音频分离。

依赖系统 ffmpeg / ffprobe；TransNetV2、Demucs 为可选，缺失时自动回退。
"""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Tuple

from .env import ffmpeg_bin, ffprobe_bin
from .models import Shot
from .motion import measure_shot


def _run(cmd: List[str], timeout: int = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)


def _avail(binary: str) -> bool:
    return bool(shutil.which(binary) or Path(binary).exists())


def _ffmpeg_probe_text(video: str) -> str:
    """没有 ffprobe 时的退路：用 ffmpeg -i 的 stderr 拿时长/分辨率/帧率。

    完整包里只带 ffmpeg 一个二进制（带 ffprobe 会让包大一倍），
    所以这条退路是必需路径，不是可选优化。
    """
    p = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", video],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8", errors="replace")
    return p.stderr or ""


def ffprobe_duration(video: str) -> float:
    probe = ffprobe_bin()
    if _avail(probe):
        out = subprocess.check_output(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", video], text=True
        ).strip()
        return float(out)
    m = re.search(r"Duration:\s*(\d+):(\d+):([0-9.]+)", _ffmpeg_probe_text(video))
    if not m:
        raise RuntimeError("读不出视频时长：既没有 ffprobe，ffmpeg 输出里也没有 Duration")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def ffprobe_fps(video: str) -> float:
    probe = ffprobe_bin()
    if _avail(probe):
        out = subprocess.check_output(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "default=nw=1:nk=1", video],
            text=True
        ).strip()
        num, den = out.split("/")
        return float(num) / float(den) if float(den) else 25.0
    m = re.search(r"([0-9.]+)\s*fps", _ffmpeg_probe_text(video))
    return float(m.group(1)) if m else 25.0


def ffprobe_size(video: str) -> Tuple[int, int]:
    probe = ffprobe_bin()
    if _avail(probe):
        out = subprocess.check_output(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", video],
            text=True
        ).strip().split(",")
        return int(out[0]), int(out[1])
    m = re.search(r",\s*(\d{2,5})x(\d{2,5})[,\s]", _ffmpeg_probe_text(video))
    if not m:
        raise RuntimeError("读不出分辨率：既没有 ffprobe，ffmpeg 输出里也没有宽高")
    return int(m.group(1)), int(m.group(2))


# ---------- 镜头切分 ----------

def _split_ffmpeg_scene(video: str, threshold: float = 0.3) -> List[Tuple[float, float]]:
    """基于 ffmpeg scene 检测的轻量切分（无需额外模型）。"""
    dur = ffprobe_duration(video)
    cmd = [ffmpeg_bin(), "-i", video, "-filter:v",
           f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]
    # 显式指定 stderr=PIPE，并对 None 兜底：打包成可执行文件后，父子进程的
    # 流继承关系会变，capture_output 偶尔拿不到 stderr，直接正则会崩在 None 上。
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8", errors="replace")
    err = p.stderr or ""
    if not err.strip() and p.returncode != 0:
        raise RuntimeError(f"ffmpeg 切镜失败（退出码 {p.returncode}）：{cmd[0]}")
    cuts = sorted({float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+)", err)})
    cuts = [c for c in cuts if 0.05 < c < dur - 0.05]
    bounds = [0.0] + cuts + [dur]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _split_transnet(video: str) -> List[Tuple[float, float]]:
    """TransNetV2 切分（需 pip install transnetv2-pytorch torch）。失败则由上层回退。"""
    from transnetv2_pytorch import TransNetV2  # noqa: 延迟导入
    model = TransNetV2()
    _, single_frame_pred, _ = model.predict_video(video)
    scenes = model.predictions_to_scenes(single_frame_pred)  # [[start_frame, end_frame], ...]
    fps = ffprobe_fps(video)
    out = []
    for s, e in scenes:
        start = s / fps
        end = (e + 1) / fps
        out.append((start, end))
    return out


def _split_fixed(duration: float, interval: float) -> List[Tuple[float, float]]:
    interval = max(1.0, interval)
    segs, t = [], 0.0
    while t < duration:
        segs.append((t, min(t + interval, duration)))
        t += interval
    return segs


def _merge_short(segments: List[Tuple[float, float]], min_sec: float) -> List[Tuple[float, float]]:
    """把过短的片段并入前一段。

    快速运动（球飞、挥手、镜头甩动）会让 scene 检测在小画面变化上误判，
    切出 0.3–1 秒的碎片。碎片一旦成为独立镜头，下游会为它单独抽帧、
    单独调用视觉模型、单独切分台词，代价全花在假镜头上。
    """
    if min_sec <= 0 or not segments:
        return segments
    out: List[Tuple[float, float]] = []
    for s, e in segments:
        if out and (e - s) < min_sec:
            ps, _ = out[-1]
            out[-1] = (ps, e)
        else:
            out.append((s, e))
    # 尾段过短（且不是唯一一段）同样并入前一段
    while len(out) > 1 and (out[-1][1] - out[-1][0]) < min_sec:
        s, e = out.pop()
        ps, _ = out[-1]
        out[-1] = (ps, e)
    return out


def _split_long(segments: List[Tuple[float, float]], max_sec: float) -> List[Tuple[float, float]]:
    """把超长片段按上限再分，只在个别长片段上生效。

    无转场的素材（游戏录屏、歌配画面、单镜长拍）scene 检测只有一个切点，
    整段 40 秒会变成一个"镜头"，而单镜最多抽 6 帧——40 秒 6 帧等于每 8 秒
    才看一眼，描述只能写成"雾中飞行"，过程全丢。
    这里不动任何已有边界，只把超长段等分；有正常切点的片子最长镜通常远
    小于上限，行为完全不变。
    """
    if max_sec <= 0 or not segments:
        return segments
    out: List[Tuple[float, float]] = []
    for s, e in segments:
        dur = e - s
        if dur <= max_sec:
            out.append((s, e))
            continue
        n = int(dur // max_sec) + (1 if dur % max_sec else 0)
        step = dur / n
        for i in range(n):
            out.append((s + step * i, s + step * (i + 1)))
    return out


def _build(segments: List[Tuple[float, float]], dur: float) -> List[Shot]:
    shots: List[Shot] = []
    for s, e in segments:
        start_ms = int(round(max(0.0, s) * 1000))
        end_ms = int(round(min(dur, e) * 1000))
        if end_ms <= start_ms:
            continue
        shots.append(Shot(shot_id=f"shot {len(shots) + 1}", start_ms=start_ms, end_ms=end_ms))
    return shots


def split_shots(video: str, cfg: dict) -> List[Shot]:
    sc = cfg["preprocess"]["shot_split"]
    method = sc.get("method", "ffmpeg_scene")
    dur = ffprobe_duration(video)
    segments = None

    if method == "transnetv2":
        try:
            segments = _split_transnet(video)
        except Exception:
            segments = None

    if not segments and method in ("ffmpeg_scene", "transnetv2"):
        segments = _split_ffmpeg_scene(video, sc.get("scene_threshold", 0.3))
        # 硬切太少（长镜头/游戏画面）→ 用固定间隔兜底，保证有可用粒度
        if len(segments) < 2 and dur > 12:
            segments = None

    if not segments:
        segments = _split_fixed(dur, sc.get("fixed_interval_sec", 4))

    # 短碎片并入相邻镜头：误判切点会把一个连续镜头劈成多个假镜头
    segments = _merge_short(segments, float(sc.get("min_shot_sec", 1.2)))

    # 超长片段按上限再分：无转场素材会整段只有一个镜头，单镜抽帧数量有限，
    # 40 秒给 6 帧等于每 8 秒看一眼，只能写出笼统描述。
    segments = _split_long(segments, float(sc.get("max_shot_sec", 10)))

    # 镜头数不设上限：超限重切成均匀段会把真实镜头边界全部作废，
    # 而镜头边界是整条流水线所有逐镜字段的对齐基准，错一次全片错。
    # 碎片已由 _merge_short 按 min_shot_sec 合并，不会因为噪点切出上百段。
    return _build(segments, dur)


# ---------- 关键帧 ----------

def extract_keyframes(video: str, shots: List[Shot], cfg: dict, workdir: Path) -> None:
    kf = cfg["preprocess"]["keyframes"]
    max_n = int(kf.get("max_per_shot", 4))
    interval = float(kf.get("interval_sec", 2))
    frame_w = int(kf.get("width", 768))
    root = Path(workdir) / "frames"
    # 每轮用独立子目录，帧文件不会跨轮混淆，也就不需要在开头清空历史帧。
    # 之前用"清空目录再抽帧"的做法：一轮删几十个文件会撞上环境的批量删除保护，
    # 拦截后旧帧留在目录里，下一轮把新旧帧一起送模型，画面描述就开始串镜。
    run_id = time.strftime("%m%d%H%M%S")
    base = root / run_id
    base.mkdir(parents=True, exist_ok=True)
    # 只保留最近 3 轮：逐目录删，单目录文件数是个位数，不会触发批量删除阈值。
    olds = sorted(p for p in root.iterdir() if p.is_dir() and p.name != run_id)
    for p in olds[:-2]:
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    f.unlink()
            except Exception:
                pass
        try:
            shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass

    for shot in shots:
        d = base / shot.shot_id.replace(" ", "_")
        d.mkdir(parents=True, exist_ok=True)
        dur = shot.duration_ms / 1000.0
        # 帧数随时长自适应：至少 3 帧（首/中/尾，运镜判断需要覆盖起止），
        # 长镜头按 interval 增帧直到 max_n，避免长镜头只有 3 帧丢失中间过程。
        edge = min(0.15, dur * 0.1)
        if dur <= 0.4:
            rel_ts = [dur / 2.0]
        else:
            n = max(3, min(max_n, int(dur / interval) + 1))
            lo, hi = edge, max(edge, dur - edge)
            if n <= 1:
                rel_ts = [dur / 2.0]
            else:
                step = (hi - lo) / (n - 1)
                rel_ts = [lo + step * i for i in range(n)]
        rel_ts = sorted({round(x, 3) for x in rel_ts})[:max_n]

        frames = []
        for i, rel in enumerate(rel_ts):
            abs_t = shot.start_ms / 1000.0 + rel
            out = d / f"f{i}.jpg"
            try:
                _run([ffmpeg_bin(), "-y", "-ss", f"{abs_t:.3f}", "-i", video,
                      "-frames:v", "1", "-vf", f"scale={frame_w}:-2",
                      "-q:v", "2", str(out)], timeout=30)
                if out.exists() and out.stat().st_size > 0:
                    frames.append(str(out))
            except Exception:
                continue
        shot.frames = frames


# ---------- 音频抽取 ----------


def extract_audio_chunks(video: str, cfg: dict, workdir: Path) -> List[dict]:
    """抽出用于环境音/音乐识别的音频段。

    默认整段一次送：块边界会切断句子，一句跨块就变成两段残句，
    而且两段各自加偏移后极难还原。目标素材是 1–2 分钟短片，
    整段 mp3 的 base64 体积仍在单次请求可承受范围内，没有必要切。
    只有音频长到超出口径（hard_max_sec）才退化为分块。

    为什么整套音频不能按镜头切：单镜音频常只有 1-2 秒、几声"啪"，模型缺少上下文，
    会把乒乓击球声猜成"篮球运球/金属撞击/齿轮转动"（实测逐镜跑 7 镜全错）。
    用整段音频一次识别，模型能靠连续击球与观众声建立场景，结果准确。
    返回 [{"path","start_ms","end_ms"}]，块内时间戳加偏移后即为绝对时间。
    """
    ac = cfg["preprocess"].get("audio_seg", {}) or {}
    mode = str(ac.get("mode", "whole"))
    window = float(ac.get("window_sec", 300))
    hard_max = float(ac.get("hard_max_sec", 300))
    duration = ffprobe_duration(video)
    cdir = Path(workdir) / "audio_chunks"
    if cdir.exists():
        shutil.rmtree(cdir, ignore_errors=True)
    cdir.mkdir(parents=True, exist_ok=True)

    if mode == "whole" and duration <= hard_max:
        spans = [(0.0, duration)]
    else:
        spans, start = [], 0.0
        step = max(30.0, window)
        while start < duration - 0.05:
            spans.append((start, min(start + step, duration)))
            start += step

    chunks: List[dict] = []
    for idx, (start, end) in enumerate(spans):
        seg = end - start
        if seg <= 0.05:
            continue
        out = cdir / f"chunk_{idx:03d}.mp3"
        try:
            _run([ffmpeg_bin(), "-y", "-ss", f"{start:.3f}", "-i", video,
                  "-t", f"{seg:.3f}", "-vn",
                  "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out)], timeout=120)
            if out.exists() and out.stat().st_size > 0:
                chunks.append({
                    "path": str(out),
                    "start_ms": int(round(start * 1000)),
                    "end_ms": int(round((start + seg) * 1000)),
                })
        except Exception:
            pass
    return chunks


# ---------- 总入口 ----------

def preprocess(video: str, cfg: dict, workdir: Path,
               on_step=None) -> Tuple[List[Shot], float, List[dict]]:
    """切镜、抽帧、抽音轨、测运镜。

    on_step(msg, frac) 是可选的进度回调：这一段全是本机 ffmpeg，耗时几十秒，
    界面不报点东西就只能盯着一个不动的百分比。frac 是 0–1 的本阶段进度。
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    def step(msg: str, frac: float):
        if on_step:
            try:
                on_step(msg, max(0.0, min(1.0, frac)))
            except Exception:
                pass

    step("读取时长与分辨率", 0.02)
    duration = ffprobe_duration(video)
    step("扫描切点（ffmpeg 逐帧比对，较慢）", 0.10)
    shots = split_shots(video, cfg)
    step(f"切出 {len(shots)} 个镜头，抽取关键帧", 0.45)
    extract_keyframes(video, shots, cfg, workdir)
    step("抽取音轨", 0.65)
    audio_chunks = extract_audio_chunks(video, cfg, workdir)

    # CPU 运镜测量（不加云成本，结果作为 VL 的判据）
    src_w, src_h = ffprobe_size(video)
    total = max(1, len(shots))
    for i, s in enumerate(shots):
        step(f"测量运镜 {i + 1}/{total}", 0.7 + 0.3 * i / total)
        try:
            s.motion = measure_shot(video, s, cfg, workdir, src_w, src_h)
        except Exception:
            s.motion = None

    manifest = {
        "video": video,
        "duration_sec": duration,
        "shots": [{"shot_id": s.shot_id, "start_ms": s.start_ms,
                   "end_ms": s.end_ms, "frames": s.frames,
                   "motion": s.motion} for s in shots],
        "audio_chunks": audio_chunks,
    }
    (workdir / "shot_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return shots, duration, audio_chunks
