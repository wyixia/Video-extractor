# 子 Skill 物料索引

> 配套主文档：`../docs/01-architecture-overview.md`（第 8 节含模型映射与验证结论）。
> 本目录每个文件是一个**完全解耦**的子 Skill，拥有独立 System Prompt、入参/出参 Schema，可单独调试、开关。

## 文件清单
| 文件 | 子 Skill | 云端模型 | 依赖 |
|------|----------|----------|------|
| `skill1_asr.md` | 音频解析（ASR 语音转写） | `paraformer` | 预处理 vocals.wav |
| `skill2_sed.md` | 音频事件识别（SED 环境音效） | `qwen3.8-omni-flash` | 预处理 no_vocals.wav |
| `skill3_music.md` | 音乐理解（BGM 曲风/情绪） | `qwen3.8-omni-flash` | 预处理 no_vocals.wav |
| `skill4_vl.md` | 核心画面标注（景别/机位/运镜/内容） | `qwen2.5-vl-72b-instruct` | 预处理 frames |
| `skill5_merge.md` | 汇总 & 校验（LLM） | `qwen-plus` / `qwen-max` | L1 四路结果 |

## 约定
1. **双版 Prompt**：每个子 Skill 提供 `生产版 System Prompt`（给云端 API，强制 JSON、无解释、无 markdown）与 `精简 API 版`（人工调试用，可带说明）。
2. **JSON 强约束**：LLM 类子 Skill 输出必须是纯 JSON，禁止多余解释 / markdown / 代码块标记；校验失败自动重试 2 次。
3. **空值/不确定**：空值统一填 `"无"`；模型无法判断填 `"无法识别"`；严禁空字段、严禁跨模态脑补（音画分离）。
4. **时间对齐**：所有时间戳内部用毫秒(ms)整数；对表单输出时按需要转 `HH:MM:SS,mmm`。
5. **失败留痕**：任一子 Skill 失败，对应字段标 `"无法识别"` 并置 `needs_review=true`，不阻塞整条流水线。

## 台词标记语义（全局统一）
- `<d>…</d>`：台词原文包裹。
- `<sceneRatio>`：在台词中标记"场景比例/镜头切换"发生点（由 merge 在 shot 边界处插入）。
- `<cutoff>`：标记该句 ASR 置信度低或被截断。
- 说话人：`S1` / `S2`（画内/画外 + 语气由文字描述）。
