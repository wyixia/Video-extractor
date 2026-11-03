# Video Extractor · 视频自动分镜标注

给一段视频，输出逐镜的 9 字段标注表：景别、机位/角度/构图、运镜、内容信息、环境音、音乐、人声台词，以及总览与复核提示。

带图形界面：选视频 → 跑 → 在应用里对着视频看报告。

**下载**：[Releases](https://github.com/wyixia/Video-extractor/releases/latest) 里有两个包——`full` 双击即用（Python 与 ffmpeg 都打进去了，58 MB），`lite` 只有代码（需要自备 Python 3.9+，0.1 MB）。功能完全一致。

---

## 它是什么，不是什么

这是一个**独立的桌面应用**，不是某个助手里的 skill。

- 它自己调用大模型：程序内部按角色把音频、图片、文本发给配置好的端点，拿回 JSON，再用确定性代码拼装字段。
- 提示词由程序自己发，存在 `config/prompts/` 里，可以直接改。
- 也可以被脚本调用（`python tools/run.py --video x.mp4`），但那是外部调用，不改变它自身是独立程序这件事。

模型调用发生在程序内部，不依赖任何外部 agent 在场。

## 架构

```
视频
 ├─ P0 本机预处理（CPU，不联网）
 │    切分镜头 → 逐镜抽 3–6 帧 → 整段抽一条音轨
 │
 ├─ P1 云端识别（并行）
 │    画面：每镜送 3–6 张图 → 景别/机位/运镜/内容/口型
 │    声音：整段音频送 3 路 → 台词 / 环境音 / 音乐
 │
 ├─ P1.5 补跑与校对
 │    失败镜头重跑；用画面字幕双向校正台词里的专有名词
 │
 └─ P2 拼装（纯代码，不调模型）
      时间戳归属 → 台词定类 → 空值兜底 → 复核提示 → 报告
```

三条设计原则，都是踩坑换来的：

1. **声音只依据声音判断**。音视频一起送，模型会拿画面解释声音——看到球场就把所有声音写成"运球"。实测 7 镜错 5 镜。
2. **但语音不能切太碎**。逐镜切 1–2 秒音频，模型没有上下文，把击球声判成"齿轮转动"。现在整段送，按时间戳归属到镜头。
3. **拼装不交给模型**。9 个字段全部由代码按时间轴拼，模型只写总览文案，避免它编出不存在的内容。

## 两种发行包

| | lite | full |
|---|---|---|
| 内容 | 只有代码 | 代码 + Python 运行时 + ffmpeg |
| 体积 | 约 0.1 MB | 约 200 MB |
| 前提 | 自己装 Python 3.9+ | 双击即用 |
| ffmpeg | 启动时自动检测，缺了自动下载 | 已内置 |
| 适合 | 想改代码、机器上已有 Python | 只想用 |

两者功能一致，用的是同一份配置和提示词。

## 运行

### lite

```bash
unzip VideoExtractor-1.0.0-lite.zip
cd VideoExtractor-1.0.0-lite
python tools/doctor.py --fix      # 缺什么装什么
python -m app.server              # 浏览器会自动打开
```

Windows 双击 `start.bat`；macOS / Linux 用 `./start.sh`。

### full

解压后双击 `VideoExtractor.exe`（Windows）或 `start.sh`。

### 设置密钥

密钥只从环境变量读，不写进配置、不随结果保存：

```powershell
# Windows PowerShell
$env:LLM_BASE_URL="https://your-endpoint/v1"; $env:LLM_API_KEY="sk-xxx"
```

```bash
# macOS / Linux
export LLM_BASE_URL=... LLM_API_KEY=...
```

没有密钥时也能先看界面：`python -m app.server --provider mock`（假数据，不联网）。

## 配置模型

改 `config/config.compat.yaml`，或在应用界面里改（保存到 `config/config.local.yaml`，不覆盖主配置）。

| 角色 | 用途 | 对模型的要求 |
|---|---|---|
| `asr` | 台词转写 | **必须能接音频**，并真的听 |
| `sed` | 环境音 | 同上 |
| `music` | 背景音乐 | 同上 |
| `vl` | 画面标注 | **必须能接图片**，单次能吃多张（3–6 帧） |
| `merge` | 总览文案 | 普通文本模型 |
| `calibrate` | 字幕校对 | 可选，不配就用 `merge` |

共同前提：OpenAI 兼容的 `/chat/completions`，且能被要求着输出 JSON。

`asr/sed/music` 要的是 omni 或 audio 系列。纯文本模型会返回 400（`invalid_parameter_error`）；**也有些模型接受请求却忽略音频照常作答**——那种"能返回内容"其实一个音都没听。

### 配完必须验

```bash
python tools/check_models.py
```

它会为每个角色发一个最小请求，检查三件事：

1. 端点连通、模型名存在；
2. 模型接受该模态（音频 / 图片）；
3. **输入真的进去了**——看 `usage` 里的 `audio_tokens`，不是看有没有回复；
4. 返回的是 JSON，且带流程要用的键名。

```
 可用   asr    qwen3.5-omni-plus   连通、音频已进入（audio_tokens=9）、返回 JSON
 不可用 sed    qwen3.8-flash       不接受这种输入或模型名不存在：HTTP 400 …
```

只通过前两条的模型配上去也能跑，但结果是猜的。`tools/run.py` 会在正式任务前自动做这个检查，配置没变则沿用上次结果。

## 改提示词

不用动内置文件：把同名 `.txt` 放进 `config/prompts.local/`，运行时优先用你的版本。

```bash
cp config/prompts/vl.txt config/prompts.local/vl.txt
```

也可以直接在应用界面里编辑。

| 可以改 | 不能改 |
|---|---|
| 措辞、语气、示例 | JSON 键名（`shot_size`、`voice`…） |
| 子项顺序与详略 | 时间字段名（`start_time`、`end_time`） |
| 要求多详细、多少字 | "只输出 JSON"这类硬约束 |

改坏键名不会报错，只会**静默产出"无法识别"**。所以每次运行前会检查一遍，缺键名直接中止并指出缺哪个。完整结果里 `prompt_sources` 记录这次用的是内置还是本机版本。

## 命令行

不想要界面时：

```bash
python -m src.main --config config/config.compat.yaml --video a.mp4 --out out.json --full
python tools/render_report.py out_full.json report.html
```

## 输出

- `<名称>_form.json` — 9 字段表单，可直接回填
- `<名称>_full.json` — 含各子技能原始结果、校准记录、复核提示
- `<名称>_report.html` — 可读报告，卡片式，顶部有待人工复核清单

## 目录

```
app/            应用（本地 Web 服务 + 单页界面）
src/            流水线核心
  preprocess.py   切镜、抽帧、抽音频
  skills.py       5 个子技能的封装
  validate.py     时间对齐、字段兜底、复核提示
  orchestrator.py 编排与进度上报
  prompt_guard.py 提示词完整性校验
  env.py          ffmpeg 定位、项目根目录
config/
  config.compat.yaml   主配置
  config.local.yaml    应用界面写入的覆盖（可选）
  prompts/             内置提示词
  prompts.local/       本机覆盖（可选）
tools/
  doctor.py        环境体检与自动修复
  check_models.py  模型可用性实测
  run.py           体检 + 验模型 + 跑，一条命令
  build_release.py 打包
docs/           设计与踩坑记录
```

## 已知限制

- 单次一个任务，不做队列（并发跑会互抢接口配额，反而更慢）。
- 台词按句归属到镜头，一句话只出现一次；后镜标"承上句"。
- 环境音常常整片一致：底噪、观众声本来就贯穿全片，不是没分析。报告里会标明。
- 远景下人物口型看不清，"对话"判不出来，会落到"画外解说"，需要人复核。
- 复核提示只标机器把握最低的地方，其余字段是确定性拼装，可直接用。
