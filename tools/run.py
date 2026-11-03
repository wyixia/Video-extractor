"""启动器：先体检环境和模型，再跑标注。

为什么要有这一层：模型配错不会报错，只会静默产出"无法识别"。
所以把"环境能不能跑""模型能不能干活"这两件事前置成必过项，
而不是等跑完十分钟才发现全是空字段。

模型验证有缓存：配置里的模型与端点没变、上次验证通过，就不再重测。
改了模型名或换了端点，缓存自动失效并重新实测。

用法：
  python tools/run.py --video a.mp4
  python tools/run.py --video a.mp4 --full --out output/x.json    # 其余参数原样透传给主程序
  python tools/run.py --video a.mp4 --skip-model-check            # 跳过模型实测（不建议）
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.check_models as cm  # noqa: E402
import tools.doctor as doc  # noqa: E402
from src.config import load_config  # noqa: E402

CACHE = ROOT / "_work" / ".models_verified.json"


def _fingerprint(cfg: dict) -> str:
    """模型名 + 端点 + 提供商 的指纹。任何一项变动都要求重新验证。"""
    c = cfg.get("compat") or {}
    raw = json.dumps({
        "provider": cfg.get("provider"),
        "models": cfg.get("models") or {},
        "base": os.getenv(c.get("base_url_env", "LLM_BASE_URL"), c.get("base_url", "")),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cached_ok(fp: str) -> bool:
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        return d.get("fingerprint") == fp and d.get("ok") is True
    except Exception:
        return False


def _save(fp: str, ok: bool):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({
        "fingerprint": fp, "ok": ok,
        "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="标注启动器：先体检，再跑")
    ap.add_argument("--config", default="config/config.compat.yaml")
    ap.add_argument("--skip-model-check", action="store_true")
    ap.add_argument("--skip-doctor", action="store_true")
    ap.add_argument("--force-model-check", action="store_true", help="忽略缓存，重新实测")
    args, rest = ap.parse_known_args()

    if not args.skip_doctor:
        print("[1/2] 环境体检")
        if doc.quick_check(args.config) != 0:
            print("\n环境未通过，先看上面的提示；自动修复：python tools/doctor.py --fix")
            return 1
    else:
        print("[1/2] 环境体检：已跳过")

    cfg = load_config(args.config)
    provider = str(cfg.get("provider", "")).lower()
    if provider == "mock":
        print("[2/2] 模型实测：provider=mock，跳过")
    elif args.skip_model_check:
        print("[2/2] 模型实测：已跳过（结果可能不可信）")
    else:
        fp = _fingerprint(cfg)
        if not args.force_model_check and _cached_ok(fp):
            print("[2/2] 模型实测：配置未变，沿用上次通过的验证结果")
        else:
            print("[2/2] 模型实测（首次或配置有变，需要实测）")
            rc = cm.run_check(cfg, quiet=False)
            _save(fp, rc == 0)
            if rc != 0:
                print("\n模型不可用，已中止。没有跑视频，也没有产生任何结果文件。")
                return rc

    print("-" * 46)
    print("开始标注")
    return subprocess.call([sys.executable, "-m", "src.main", "--config", args.config, *rest],
                           cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
