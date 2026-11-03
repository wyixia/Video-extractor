"""模型可用性实测。

改完 config 里的 models，怎么知道换上去的模型真能干活？
光看 HTTP 200 不够：模型可能接受请求、却忽略附件照常作答——表现为"能返回内容"，
其实音频一个音都没听、图片一张都没看。所以这里做四件事：

  1. 发一个最小请求，确认端点能连通、鉴权有效、模型名存在；
  2. 确认模型**接受该模态**（音频/图片），不支持会返回 4xx；
  3. 确认输入**真的进去了**——看 usage 里的 audio_tokens / image 计数，不是看有没有回复；
  4. 确认**按约定返回 JSON**，且带流程要用的键名。

三条都过才算可用。只过前两条的模型，配上去也能跑，但结果是猜的。

用法：
  python tools/check_models.py                    # 测全部 6 个角色
  python tools/check_models.py --roles asr,vl     # 只测指定角色
  python tools/check_models.py --config config/config.compat.yaml
"""
import argparse
import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.clients import PermanentError, build_client  # noqa: E402
from src.config import load_config  # noqa: E402
from src.env import ffmpeg_bin  # noqa: E402

# 角色 → (输入模态, 最少应出现的键, 说明)
ROLES = {
    "asr": ("audio", ["segments"], "语音转写：说话与演唱都要转，标 voice"),
    "sed": ("audio", ["events"], "环境音识别：声源、强度、情绪"),
    "music": ("audio", ["present"], "背景音乐判断：有无、曲风、乐器"),
    "vl": ("image", ["shot_size", "content_info"], "画面标注：景别、机位、运镜、口型"),
    "merge": ("text", ["overview"], "总览文案：只看文本"),
}

PASS, WARN, FAIL = "可用", "存疑", "不可用"


def _mp3(path: Path) -> Path:
    """1 秒 440Hz 正弦音。用真实声音而不是静音：静音下模型即使收到音频也可能无反应。"""
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libmp3lame", "-q:a", "9", str(path)],
                   check=True, capture_output=True)
    return path


def _jpg(path: Path) -> Path:
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i", "color=c=gray:s=96x96:d=1",
                    "-frames:v", "1", str(path)], check=True, capture_output=True)
    return path


def _data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _messages(modality: str, tmp: Path):
    if modality == "audio":
        return [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": _data_url(_mp3(tmp / "t.mp3"), "audio/mp3")}},
            {"type": "text", "text": '只输出 JSON：{"ok":true}'},
        ]}]
    if modality == "image":
        return [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": _data_url(_jpg(tmp / "t.jpg"), "image/jpeg")}},
            {"type": "text", "text": '只输出 JSON：{"ok":true}'},
        ]}]
    return [{"role": "user", "content": '只输出 JSON：{"ok":true}'}]


def _audio_tokens(usage: dict) -> int:
    d = usage.get("prompt_tokens_details") or {}
    if isinstance(d, dict) and d.get("audio_tokens"):
        return int(d["audio_tokens"])
    # 部分服务商把音频计入 prompt_tokens 明细的别处，退一步看 modality 字段
    for key in ("input_audio_tokens", "audio_tokens"):
        if usage.get(key):
            return int(usage[key])
    return 0


def check_role(client, cfg, role: str, model: str, tmp: Path) -> dict:
    modality, need_keys, desc = ROLES[role]
    out = {"role": role, "model": model, "status": FAIL, "detail": "", "desc": desc}
    detail_fn = getattr(client, "chat_detail", None)
    try:
        if detail_fn:
            content, usage = detail_fn(model, _messages(modality, tmp), timeout=120,
                                       max_tokens=64, enable_thinking=False)
        else:
            content, usage = client.chat(model, _messages(modality, tmp), timeout=120,
                                         max_tokens=64), {}
    except PermanentError as e:
        msg = str(e)
        if any(c in msg for c in ("400", "404", "415", "422")):
            out["detail"] = f"不接受这种输入或模型名不存在：{msg[:90]}"
        else:
            out["detail"] = msg[:110]
        return out
    except Exception as e:
        out["detail"] = f"{type(e).__name__}: {str(e)[:100]}"
        return out

    # 模态是否真被接收：这一条最容易被忽略，也最容易造成"看着能跑其实是猜"
    if modality == "audio":
        tok = _audio_tokens(usage)
        if tok == 0:
            out["status"] = WARN
            out["detail"] = "有回复，但 usage 里没有音频 token，音频可能被忽略（结论不可信）"
            return out
        out["audio_tokens"] = tok

    try:
        obj = json.loads(content.strip().strip("`").removeprefix("json").strip())
    except Exception:
        out["status"] = WARN
        out["detail"] = f"回复不是 JSON，流程无法解析：{content[:70]!r}"
        return out

    if not isinstance(obj, dict):
        out["status"] = WARN
        out["detail"] = "回复是 JSON 但不是对象"
        return out

    missing = [k for k in need_keys if k not in obj and "ok" not in obj]
    if missing:
        out["status"] = WARN
        out["detail"] = f"缺少流程要用的键 {missing}，请确认提示词与模型是否配套"
        return out

    out["status"] = PASS
    if modality == "audio":
        out["detail"] = f"连通、音频已进入（audio_tokens={out.get('audio_tokens', 0)}）、返回 JSON"
    else:
        pt = usage.get("prompt_tokens")
        tail = f"prompt_tokens={pt}、" if pt else ""
        out["detail"] = f"连通、{tail}返回 JSON"
    return out


def run_check(cfg: dict, roles=None, quiet: bool = False) -> int:
    """跑一轮实测，返回 0=可用 / 1=有不可用 / 2=参数错。供 run.py 复用。"""
    models = cfg.get("models") or {}
    roles = roles or list(ROLES)
    unknown = [r for r in roles if r not in ROLES]
    if unknown:
        print(f"未知角色：{unknown}，可选：{list(ROLES)}")
        return 2

    try:
        client = build_client(cfg)
    except Exception as e:
        print(f"客户端初始化失败：{type(e).__name__}: {e}")
        print("先跑 python tools/doctor.py 看环境与凭据。")
        return 1

    if not quiet:
        print(f"模型实测（provider={cfg.get('provider')}）")
        print("-" * 62)
    results = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for role in roles:
            model = models.get(role) or models.get("merge")
            if not model:
                results.append({"role": role, "model": "-", "status": FAIL,
                                "detail": "配置里没有该角色的模型名", "desc": ROLES[role][2]})
                continue
            if not quiet:
                print(f"  测试 {role:<8} {model} …", end="", flush=True)
            r = check_role(client, cfg, role, model, tmp)
            results.append(r)
            if not quiet:
                print(f" {r['status']}")

    if not quiet:
        print("-" * 62)
    for r in results:
        print(f" {r['status']:<4} {r['role']:<9} {r['model']:<28} {r['detail']}")

    bad = [r for r in results if r["status"] == FAIL]
    warn = [r for r in results if r["status"] == WARN]
    if not quiet:
        print("-" * 62)
    if bad:
        print(f"有 {len(bad)} 个角色不可用，直接跑会降级成“无法识别”。先换模型或改配置。")
        return 1
    if warn:
        print(f"有 {len(warn)} 个角色存疑，能跑但结果可能不可信，建议先小样片验证。")
        return 0
    print("全部可用，可以正式跑。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="实测配置里的模型能否用于本流程")
    ap.add_argument("--config", default="config/config.compat.yaml")
    ap.add_argument("--roles", default="", help="只测这些角色，逗号分隔，如 asr,vl")
    args = ap.parse_args()

    cfg = load_config(args.config)
    roles = [r.strip() for r in args.roles.split(",") if r.strip()] or None
    return run_check(cfg, roles)


if __name__ == "__main__":
    raise SystemExit(main())
