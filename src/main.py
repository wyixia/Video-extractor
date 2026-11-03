"""命令行入口。

用法：
  export DASHSCOPE_API_KEY=sk-xxx
  python -m src.main --video ./samples/pingpong.mp4 --subtask-id 864689235027272
  # 离线联调（不调云端）：
  python -m src.main --video ./samples/pingpong.mp4 --provider mock
"""
import argparse
import json
from pathlib import Path

from .config import load_config, prompt_sources, resolve_path
from .export import frontend_payload, write_json
from .orchestrator import Pipeline
from .prompt_guard import check_all, summarize


def main() -> None:
    ap = argparse.ArgumentParser(description="视频自动分镜标注流水线")
    ap.add_argument("--video", required=True, help="视频文件路径")
    ap.add_argument("--subtask-id", default="", help="前端表单顶部子任务 ID")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--provider", default=None, choices=["dashscope", "mock"],
                    help="覆盖 config.provider")
    ap.add_argument("--out", default=None, help="输出 JSON 路径")
    ap.add_argument("--full", action="store_true", help="额外输出含 raw_subskills 的完整结果")
    ap.add_argument("--speech-assign", default=None, choices=["slice", "whole"],
                    help="台词归属：slice=按时间比例切给各镜（默认）；whole=整句挂句首镜")
    ap.add_argument("--skip-prompt-check", action="store_true",
                    help="跳过提示词完整性检查（提示词改坏了要自己承担后果）")
    args = ap.parse_args()

    overrides = {}
    if args.provider:
        overrides["provider"] = args.provider
    if args.speech_assign:
        overrides["speech"] = {"assign": args.speech_assign}
    cfg = load_config(args.config, overrides)

    # 提示词被改坏不会报错，只会静默产出"无法识别"。运行前先挡一道。
    if not args.skip_prompt_check:
        report = check_all()
        bad = {k: v["problems"] for k, v in report.items() if v["problems"]}
        if bad:
            print("[提示词检查未通过]")
            print(summarize(report))
            print("\n改回默认：删掉 config/prompts.local 下对应文件，或从 config/prompts 复制一份重改。")
            print("确认要用当前版本强行运行，加 --skip-prompt-check。")
            raise SystemExit(2)
        custom = [k for k, v in report.items() if v["source"] == "local"]
        if custom:
            print(f"[INFO] 使用本地覆盖提示词：{'、'.join(custom)}")

    pipeline = Pipeline(cfg)
    final = pipeline.run(args.video, args.subtask_id)

    out = Path(args.out) if args.out else (
        resolve_path(cfg["paths"]["output"]) / f"{Path(args.video).stem}_form.json"
    )
    write_json(str(out), frontend_payload(final))
    print(f"[OK] 表单草稿 -> {out}")

    if args.full:
        full_path = out.with_name(out.stem + "_full.json")
        # 记下这次用的是哪份提示词，事后比对结果差异时能分清是改了提示词还是换了模型
        final["prompt_sources"] = prompt_sources()
        write_json(str(full_path), final)
        print(f"[OK] 完整结果 -> {full_path}")

    print(f"[INFO] shots={len(final['shots'])} review={final.get('review_flags', [])}")
    print(json.dumps(frontend_payload(final)["shots"][:1], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
