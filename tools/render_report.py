"""把分镜表单 JSON 渲染成可读的 HTML 报告。

用法：python tools/render_report.py output/测试_form_full.json output/测试_report.html
"""
import html
import json
import re
import sys
from pathlib import Path

KEY_LABELS = [
    ("shot_number", "镜号"),
    ("time_range", "时间段"),
    ("shot_size", "景别"),
    ("angle_camera_composition", "角度 / 机位 / 构图"),
    ("camera_movement", "运镜"),
    ("content_info", "内容信息"),
    ("env_sound", "环境音 / 音效"),
    ("music", "音乐"),
    ("speech", "人声 / 台词"),
]


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def split_numbered(text: str) -> list:
    """把 "1.xxx 2.yyy" 或 "a.xxx b.yyy" 拆成条目，便于阅读。"""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[。；;])(?=\d{1,2}\.[^\d])", text)
    if len(parts) == 1:
        parts = re.split(r"(?<=[；;。])(?=[a-h]\.)", text)
    return [p.strip() for p in parts if p.strip()]


def split_semicolon(text: str) -> list:
    return [x.strip() for x in re.split(r"[；;]", text or "") if x.strip()]


def render_shot(s: dict, idx: int, total: int, hints: list = None) -> str:
    num = esc(s.get("shot_number", ""))
    tr = esc(s.get("time_range", ""))
    mine = [h for h in (hints or [])
            if h.get("shot") and h.get("shot") == s.get("shot_number")]
    notice = ""
    if mine:
        lis = []
        for h in mine:
            at = f" · {esc(h.get('at'))}" if h.get("at") else ""
            change = ""
            if h.get("before") and h.get("after"):
                change = f"　<b>{esc(h['before'])}</b> → <b>{esc(h['after'])}</b>"
            lis.append(f"<li>{esc(h.get('field'))}{at}：{esc(h.get('reason'))}{change}</li>")
        notice = "<div class='notice'><span class='notice-t'>建议复核</span><ul>" + \
                 "".join(lis) + "</ul></div>"
    rows = []
    for key, label in KEY_LABELS:
        if key in ("shot_number", "time_range"):
            continue
        val = s.get(key, "")
        if key in ("angle_camera_composition", "content_info"):
            items = split_numbered(val)
            if len(items) > 1:
                body = "<ul class='detail'>" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>"
            else:
                body = f"<p>{esc(val)}</p>"
        elif key == "env_sound":
            items = split_semicolon(val)
            if len(items) > 1:
                body = "<ul class='detail'>" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>"
            else:
                body = f"<p>{esc(val)}</p>"
        elif key == "speech":
            body = f"<p class='speech'>{esc(val)}</p>"
        else:
            body = f"<p>{esc(val)}</p>"
        rows.append(
            f"<div class='field'><div class='label'>{label}</div><div class='value'>{body}</div></div>"
        )
    return f"""
    <section class="shot">
      <header class="shot-head">
        <span class="idx">{idx:02d}</span>
        <span class="sid">{num}</span>
        <span class="tr">{tr}</span>
        <span class="chars">{len(str(s.get('content_info') or ''))} 字</span>
      </header>
      {notice}
      {''.join(rows)}
    </section>"""


CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px 60px;
  background: #f4f6f9; color: #1c2430;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  font-size: 15px; line-height: 1.75;
}
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 6px; letter-spacing: .5px; }
.sub { color: #5f6b7a; font-size: 13px; margin-bottom: 24px; }
.meta { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 26px; }
.chip {
  background: #fff; border: 1px solid #e2e7ee; border-radius: 6px;
  padding: 8px 14px; font-size: 13px; color: #39465a;
}
.chip b { color: #1c2430; font-weight: 600; }
.overview {
  background: #fff; border: 1px solid #e2e7ee; border-left: 3px solid #2f6fd0;
  border-radius: 6px; padding: 16px 20px; margin-bottom: 28px;
}
.overview h2 { font-size: 14px; margin: 0 0 8px; color: #2f6fd0; letter-spacing: 1px; }
.overview p { margin: 0; color: #2b3644; }
.issues {
  background: #fffaf0; border: 1px solid #efdfc0; border-left: 3px solid #c9922e;
  border-radius: 6px; padding: 14px 20px; margin: -12px 0 28px;
}
.issues h2 { font-size: 14px; margin: 0 0 8px; color: #9a6f16; letter-spacing: 1px; }
.issues ul { margin: 0; padding-left: 20px; color: #4a4132; font-size: 13.5px; line-height: 1.75; }
.hints {
  background: #fff; border: 1px solid #e2e7ee; border-left: 3px solid #d14343;
  border-radius: 6px; padding: 16px 20px; margin-bottom: 28px;
}
.hints h2 { font-size: 14px; margin: 0 0 12px; color: #b83333; letter-spacing: 1px; }
.hints h2 .cnt {
  display: inline-block; background: #fdeceb; color: #b83333; border-radius: 10px;
  padding: 1px 9px; font-size: 12px; margin-left: 4px;
}
.hints table { width: 100%; border-collapse: collapse; font-size: 13px; }
.hints th {
  text-align: left; font-weight: 600; color: #6b7686; font-size: 12px;
  padding: 0 10px 8px 0; border-bottom: 1px solid #eef1f5;
}
.hints td { padding: 9px 10px 9px 0; border-bottom: 1px solid #f4f6f9; vertical-align: top; color: #2b3644; line-height: 1.6; }
.hints td:first-child { white-space: nowrap; color: #1c2430; font-weight: 600; }
.hints td s { color: #9aa4b2; }
.hints td b { color: #b83333; }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 7px; vertical-align: 1px; }
.h-high { background: #d14343; }
.h-mid { background: #d9921e; }
.h-low { background: #9aa4b2; }
.hint-tip { margin: 12px 0 0; font-size: 12.5px; color: #6b7686; line-height: 1.7; }
.notice {
  background: #fff8e8; border: 1px dashed #e8cfa0; border-radius: 6px;
  margin: 0 20px 14px; padding: 10px 14px;
}
.notice-t { font-size: 12px; font-weight: 600; color: #a97a12; letter-spacing: .5px; }
.notice ul { margin: 6px 0 0; padding-left: 18px; }
.notice li { font-size: 12.5px; color: #5a4c2c; line-height: 1.7; }
.notice li b { color: #8a5a00; }
.shot {
  background: #fff; border: 1px solid #e2e7ee; border-radius: 8px;
  margin-bottom: 18px; overflow: hidden;
}
.shot-head {
  display: flex; align-items: center; gap: 14px;
  padding: 12px 20px; background: #eef2f8; border-bottom: 1px solid #e2e7ee;
}
.idx { font-size: 13px; font-weight: 700; color: #fff; background: #2f6fd0;
  border-radius: 4px; padding: 2px 8px; }
.sid { font-weight: 600; font-size: 15px; }
.tr { font-family: "SFMono-Regular", Consolas, monospace; font-size: 13px; color: #5f6b7a; }
.chars { margin-left: auto; font-size: 12px; color: #8b96a5; }
.field { display: flex; gap: 18px; padding: 14px 20px; border-top: 1px solid #f0f3f7; }
.field:first-of-type { border-top: none; }
.label { flex: 0 0 118px; font-size: 13px; color: #6b7787; font-weight: 600; }
.value { flex: 1; min-width: 0; }
.value p { margin: 0; }
.detail { margin: 0; padding-left: 20px; }
.detail li { margin-bottom: 4px; }
.speech { font-family: "SFMono-Regular", Consolas, monospace; font-size: 13.5px;
  background: #f6f8fb; border: 1px solid #eaeef4; border-radius: 4px;
  padding: 6px 10px; display: inline-block; }
details { background: #fff; border: 1px solid #e2e7ee; border-radius: 8px;
  padding: 14px 20px; margin-top: 26px; }
summary { cursor: pointer; font-weight: 600; color: #2f6fd0; }
details p { white-space: pre-wrap; color: #2b3644; margin: 12px 0 0; font-size: 14px; }
@media print {
  body { background: #fff; padding: 0; }
  .shot { break-inside: avoid; }
  details { display: block; }
}
"""


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "output/测试_form_full.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "output/测试_report.html"
    d = json.loads(Path(src).read_text(encoding="utf-8"))
    build(d, dst)
    return 0


def build(d: dict, dst: str) -> str:
    """把完整结果渲染成 HTML 报告。命令行和应用内共用同一份，避免两套样式。"""
    meta = d.get("video_meta") or {}
    shots = d.get("shots") or []
    flags = d.get("review_flags") or []
    hints = d.get("review_hints") or []
    total_chars = sum(len(str(s.get("content_info") or "")) for s in shots)

    chips = [
        f"<div class='chip'>文件 <b>{esc(meta.get('filename'))}</b></div>",
        f"<div class='chip'>时长 <b>{meta.get('duration_sec', 0):.3f}s</b></div>",
        f"<div class='chip'>镜头数 <b>{len(shots)}</b></div>",
        f"<div class='chip'>需复核 <b>{', '.join(esc(x) for x in flags) if flags else '无'}</b></div>",
        f"<div class='chip'>待人工看 <b>{len(hints)}</b> 处</div>",
        f"<div class='chip'>内容信息合计 <b>{total_chars}</b> 字</div>",
    ]

    body = "\n".join(render_shot(s, i + 1, len(shots), hints) for i, s in enumerate(shots))
    caption = esc(d.get("full_caption") or "")

    sc = d.get("self_check") or {}
    issues = [str(x).strip() for x in (sc.get("issues") or []) if str(x).strip()]
    issues_html = ""
    if issues:
        lis = "".join(f"<li>{esc(x)}</li>" for x in issues)
        issues_html = f"<div class='issues'><h2>自检提示</h2><ul>{lis}</ul></div>"

    hints_html = ""
    if hints:
        dot = {"high": "h-high", "mid": "h-mid", "low": "h-low"}
        trs = []
        for h in hints:
            pos = " · ".join(x for x in (h.get("shot") or "全片", h.get("field") or "",
                                         h.get("at") or "") if x)
            if h.get("before") and h.get("after"):
                change = f"<s>{esc(h['before'])}</s> → <b>{esc(h['after'])}</b>"
            elif h.get("before"):
                change = esc(h["before"])
            else:
                change = "—"
            lv = dot.get(h.get("level") or "mid", "h-mid")
            trs.append(f"<tr><td><span class='dot {lv}'></span>{esc(pos)}</td>"
                       f"<td>{change}</td><td>{esc(h.get('reason'))}</td></tr>")
        hints_html = (
            f"<div class='hints'><h2>待人工复核 <span class='cnt'>{len(hints)}</span></h2>"
            "<table><thead><tr><th>位置</th><th>原值与现用值</th><th>为什么需要看</th></tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table>"
            "<p class='hint-tip'>高亮处是机器判断把握最低的地方。其余字段为确定性拼装，可直接使用；"
            "如复核后发现问题，改这台表单对应的格子即可，不影响其他镜头。</p></div>")

    out = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>分镜标注报告 · {esc(meta.get('filename'))}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
  <h1>视频分镜标注报告</h1>
  <div class="sub">9 字段逐镜标注 · 由分镜标注流水线自动生成</div>
  <div class="meta">{''.join(chips)}</div>
  <div class="overview"><h2>总览</h2><p>{esc(d.get('overview'))}</p></div>
  {hints_html}
  {issues_html}
  {body}
  <details><summary>完整 Caption（{len(caption)} 字）</summary><p>{caption}</p></details>
</div></body></html>"""

    Path(dst).write_text(out, encoding="utf-8")
    print(f"OK -> {dst}  ({len(out)} bytes)")
    return dst


if __name__ == "__main__":
    raise SystemExit(main())
