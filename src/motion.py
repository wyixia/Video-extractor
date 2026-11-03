"""CPU 全局运动测量：估计镜头内的尺度变化与平移，用于运镜判定。

方法：边缘投影 + 一维对齐。
  1. 每帧灰度化、模糊、取边缘，把边缘能量按列/按行投影成 1D 序列
     （列投影对广告牌、看台、球台等竖向结构敏感；一两名人物的动作只影响局部几列）
  2. 相邻帧做一维对齐：横向序列搜索「尺度 s + 位移 dx」，纵向序列搜索 dy
  3. 按约 0.2s 密采样累加，避免长基线被主体动作带偏

为什么不用二维模板匹配：纯 PIL 下二维 MAE 在动态画面里噪声太大，
尺度估计会在搜索边界来回跳；1D 投影把整幅画面的结构聚合起来，稳得多。

依赖 PIL；缺失时跳过（Shot.motion 留空，VL 退回目测）。
"""
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .env import ffmpeg_bin
from .models import Shot

_W = 192                     # 分析宽度
_BLUR = 1.5                  # 模糊半径，压掉细节噪声
_MAX_SHIFT = 14              # 单对帧最大平移（分析分辨率像素）
_MAX_SHIFT_WIDE = 28         # 快速摇镜时的宽搜索
_SCALES = [round(0.80 + 0.01 * i, 2) for i in range(41)]   # 0.80 ~ 1.20
_MIN_EDGE = 1e-3


def _pil():
    try:
        from PIL import Image, ImageFilter
        return Image, ImageFilter
    except ImportError:
        return None, None


def _grab(video: str, shot: Shot, outdir: Path, max_frames: int = 20) -> List[object]:
    """按约 0.2s 间隔抽灰度小图（密采样）。"""
    Image, ImageFilter = _pil()
    outdir.mkdir(parents=True, exist_ok=True)
    for f in outdir.glob("m*.png"):
        f.unlink()

    dur = max(0.1, shot.duration_ms / 1000.0)
    k = int(min(max_frames, max(4, round(dur / 0.2) + 1)))
    cmd = [ffmpeg_bin(), "-y", "-ss", f"{shot.start_ms / 1000.0:.3f}",
           "-t", f"{dur:.3f}", "-i", video,
           "-vf", f"fps={k / dur:.4f},scale={_W}:-2,format=gray",
           "-q:v", "2", str(outdir / "m%02d.png")]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   text=True, encoding="utf-8", errors="replace", timeout=90)

    imgs = []
    for f in sorted(outdir.glob("m*.png")):
        try:
            imgs.append(Image.open(f).convert("L").filter(ImageFilter.GaussianBlur(_BLUR)))
        except Exception:
            continue
    return imgs


def _norm(v: List[float]) -> List[float]:
    if not v:
        return v
    m = sum(v) / len(v)
    var = sum((x - m) ** 2 for x in v) / max(1, len(v))
    sd = var ** 0.5
    if sd < _MIN_EDGE:
        return [0.0] * len(v)
    return [(x - m) / sd for x in v]


def _profiles(img) -> Tuple[List[float], List[float]]:
    """返回 (列投影, 行投影)：边缘能量在列/行上的分布。"""
    Image, ImageFilter = _pil()
    w, h = img.size
    # FIND_EDGES 在图像四边会产生伪边缘（外框一圈极亮），必须裁掉，
    # 否则归一化后整条投影被边框尖峰主导，任何两帧都会高度相关。
    m = 3
    core = img.crop((m, m, w - m, h - m)) if w > 2 * m + 8 and h > 2 * m + 8 else img
    ed = core.filter(ImageFilter.FIND_EDGES)
    cw, ch = ed.size
    col = list(ed.resize((cw, 1), Image.BOX).getdata())
    row = list(ed.resize((1, ch), Image.BOX).getdata())
    return _norm(col), _norm(row)


def _resample(p: List[float], s: float) -> List[float]:
    """按尺度 s 重采样 1D 序列（线性插值），用于模拟画面放大/缩小。"""
    n = len(p)
    out = []
    for i in range(n):
        x = i / s
        i0 = int(x)
        if i0 >= n - 1:
            out.append(p[-1])
            continue
        t = x - i0
        out.append(p[i0] * (1 - t) + p[i0 + 1] * t)
    return out


def _align(pa: List[float], pb: List[float], scales: List[float],
           max_shift: int) -> Tuple[float, int, float]:
    """返回 (最佳尺度, 最佳位移, 残差)。残差越小越可信。"""
    n = len(pa)
    best = (1.0, 0, None)
    for s in scales:
        qa = _resample(pa, s) if abs(s - 1) > 1e-6 else pa
        for dx in range(-max_shift, max_shift + 1, 2):
            x0, x1 = max(0, dx), min(n, n + dx)
            if x1 - x0 < n * 0.5:
                continue
            tot, cnt = 0.0, 0
            for i in range(x0, x1):
                tot += abs(qa[i - dx] - pb[i])
                cnt += 1
            if cnt:
                r = tot / cnt
                if best[2] is None or r < best[2]:
                    best = (s, dx, r)
    # 位移细化（步长 1）
    s, dx0, r0 = best
    qa = _resample(pa, s) if abs(s - 1) > 1e-6 else pa
    for dx in (dx0 - 1, dx0 + 1):
        x0, x1 = max(0, dx), min(n, n + dx)
        if x1 - x0 < n * 0.5:
            continue
        tot, cnt = 0.0, 0
        for i in range(x0, x1):
            tot += abs(qa[i - dx] - pb[i])
            cnt += 1
        r = tot / max(1, cnt)
        if r < r0:
            r0, dx0 = r, dx
    return s, dx0, r0


def _pair(a, b) -> Tuple[float, int, int, float]:
    """(尺度, dx, dy, 残差)"""
    ca, ra = _profiles(a)
    cb, rb = _profiles(b)
    s, dx, rh = _align(ca, cb, _SCALES, _MAX_SHIFT)
    if rh > 1.4:  # 快速摇镜，放宽位移搜索
        s2, dx2, rh2 = _align(ca, cb, [1.0], _MAX_SHIFT_WIDE)
        if rh2 < rh:
            s, dx, rh = s2, dx2, rh2
    _, dy, rv = _align(ra, rb, [1.0], _MAX_SHIFT)
    return s, dx, dy, max(rh, rv)


# ---------- 转场扫描（硬切 + 溶解） ----------

def _all_frames(video: str, outdir: Path, fps: float = 10.0, width: int = 96) -> List[object]:
    Image, ImageFilter = _pil()
    outdir.mkdir(parents=True, exist_ok=True)
    for f in outdir.glob("t*.png"):
        f.unlink()
    cmd = [ffmpeg_bin(), "-y", "-i", video, "-vf",
           f"fps={fps},scale={width}:-2,format=gray", "-q:v", "3",
           str(outdir / "t%04d.png")]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   text=True, encoding="utf-8", errors="replace", timeout=300)
    imgs = []
    for f in sorted(outdir.glob("t*.png")):
        try:
            imgs.append(Image.open(f).convert("L").filter(ImageFilter.GaussianBlur(1)))
        except Exception:
            continue
    return imgs


def detect_transitions(video: str, cfg: dict, workdir: Path) -> List[float]:
    """检测硬切与溶解转场，返回边界时间（秒）。

    ffmpeg 的 scene 分数对溶解/淡入淡出几乎无反应（单帧变化小），
    这里用帧间差曲线：孤立尖峰=硬切，连续多帧中等差值=渐变转场。
    """
    if _pil()[0] is None:
        return []
    mc = (cfg.get("preprocess", {}).get("motion", {}) or {})
    if not mc.get("enabled", True):
        return []

    fps = float(mc.get("scan_fps", 10))
    imgs = _all_frames(video, Path(workdir) / "transition", fps)
    if len(imgs) < 3:
        return []
    _, ImageFilter = _pil()
    from PIL import ImageChops, ImageStat
    diffs = []
    for a, b in zip(imgs[:-1], imgs[1:]):
        diffs.append(ImageStat.Stat(ImageChops.difference(a, b)).mean[0])

    srt = sorted(diffs)
    med = srt[len(srt) // 2] or 1e-6
    # 手机/低分辨率素材帧间差基线很高（噪点+动作），阈值必须相对基线定
    cut_th = max(float(mc.get("cut_threshold", 25.0)), med * 2.5)
    dis_lo = max(float(mc.get("dissolve_lo", 1.3)) * med, 3.0)
    dis_hi = cut_th
    min_run = int(mc.get("dissolve_min_frames", 4))

    bounds: List[float] = []
    for i, d in enumerate(diffs):
        if d >= cut_th:
            bounds.append((i + 1) / fps)

    # 溶解/快速推拉：连续多帧高于基线，两端各记一个边界
    run_start, run = None, 0
    for i, d in enumerate(diffs):
        if dis_lo <= d < dis_hi:
            if run == 0:
                run_start = i
            run += 1
        else:
            if run >= min_run and run_start is not None:
                bounds.append(run_start / fps)
                bounds.append((run_start + run + 1) / fps)
            run = 0
    if run >= min_run and run_start is not None:
        bounds.append(run_start / fps)
        bounds.append((run_start + run + 1) / fps)

    return sorted({round(b, 2) for b in bounds if b > 0.1})


def classify(zoom_pct: float, pan_x: float, pan_y: float,
             src_w: int, src_h: int, cfg: dict,
             zoom_consistent: bool = True, pan_consistent: bool = True) -> Tuple[str, float]:
    th = (cfg.get("preprocess", {}).get("motion", {}) or {})
    z_th = float(th.get("zoom_threshold_pct", 3.0))
    p_th = float(th.get("pan_threshold_ratio", 0.02))

    parts, conf = [], 0.0
    if zoom_consistent and zoom_pct >= z_th:
        parts.append("推")
        conf = max(conf, min(0.9, 0.45 + zoom_pct / 25.0))
    elif zoom_consistent and zoom_pct <= -z_th:
        parts.append("拉")
        conf = max(conf, min(0.9, 0.45 + abs(zoom_pct) / 25.0))

    if pan_consistent and abs(pan_x) >= p_th * src_w:
        parts.append("摇")
        conf = max(conf, min(0.9, 0.45 + abs(pan_x) / (src_w * 0.5)))
    elif pan_consistent and abs(pan_y) >= p_th * src_h:
        parts.append("移")
        conf = max(conf, min(0.9, 0.45 + abs(pan_y) / (src_h * 0.5)))

    if not parts:
        # 没有任何一致性信号 → 判固定，但置信度按残差衰减
        return "固定镜头", max(0.35, 0.85 - abs(zoom_pct) / 12.0 - abs(pan_x) / (src_w * 2))
    return "+".join(parts), conf


def measure_shot(video: str, shot: Shot, cfg: dict, workdir: Path,
                 src_w: int, src_h: int) -> Optional[dict]:
    if _pil()[0] is None:
        return None
    mc = (cfg.get("preprocess", {}).get("motion", {}) or {})
    if not mc.get("enabled", True):
        return None

    d = Path(workdir) / "motion" / shot.shot_id.replace(" ", "_")
    imgs = _grab(video, shot, d, int(mc.get("max_frames", 20)))
    if len(imgs) < 2:
        return None

    cum_scale, pan_x, pan_y = 1.0, 0.0, 0.0
    res, s_signs, x_signs = [], [], []
    for a, b in zip(imgs[:-1], imgs[1:]):
        s, dx, dy, r = _pair(a, b)
        cum_scale *= s
        pan_x += dx
        pan_y += dy
        res.append(r)
        if abs(s - 1.0) >= 0.01:
            s_signs.append(1 if s > 1 else -1)
        if abs(dx) >= 2:
            x_signs.append(1 if dx > 0 else -1)

    to_src = src_w / float(_W)
    zoom_pct = (cum_scale - 1.0) * 100.0
    px, py = pan_x * to_src, pan_y * to_src

    # 一致性门控：只有多数帧对同向变化时才承认缩放/平移，否则视为噪声
    n = max(1, len(imgs) - 1)
    zoom_consistent = bool(s_signs) and abs(sum(s_signs)) / len(s_signs) >= 0.7 and len(s_signs) >= n * 0.5
    pan_consistent = bool(x_signs) and abs(sum(x_signs)) / len(x_signs) >= 0.7 and len(x_signs) >= n * 0.5

    label, conf = classify(zoom_pct, px, py, src_w, src_h, cfg,
                           zoom_consistent=zoom_consistent, pan_consistent=pan_consistent)
    mean_res = round(sum(res) / len(res), 2) if res else 0.0

    weak = conf < 0.55 or mean_res > 2.0
    if weak:
        tail = "测量信号弱，请依据画面自行判断"
    else:
        tail = f"测得候选运镜：{label}"
    evidence = (f"全局运动测量（{len(imgs)} 帧，约 0.2s 间隔，对齐残差 {mean_res}）："
                f"画面累计尺度 {zoom_pct:+.1f}%，累计水平位移 {px:+.0f}px、垂直 {py:+.0f}px"
                f"（正=画面内容向右/下移动）；{tail}")
    return {
        "zoom_pct": round(zoom_pct, 1),
        "pan_x": round(px, 0),
        "pan_y": round(py, 0),
        "residual": mean_res,
        "label": label if not weak else "无法判定",
        "confidence": round(conf, 2),
        "weak": weak,
        "evidence": evidence,
    }
