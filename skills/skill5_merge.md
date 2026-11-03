# Skill 5 · 汇总 & 校验 Skill（LLM）

## 1. 能力说明
流水线的收口 Skill。合并 Skill 1~4 的结果，按时间轴对齐到每个 shot，执行 Schema 对齐、时序校验、时间区间校验、内容合理性自检，补全【总览信息】与【完整 Caption】，输出直接适配前端表单的结构化 JSON。

## 2. 触发条件 / 依赖
- **触发**：L1 四路全部返回（成功或降级）后启动。
- **依赖**：Skill 1~4 原始结果 + `shot_manifest`（含每个 shot 的精确时间边界）。
- **模型**：`qwen-plus`（成本）或 `qwen-max`（质量优先）。

## 3. 入参 Schema
```json
{
  "model": "qwen-plus",
  "shot_manifest": [
    {"shot_id":"shot 1","start_ms":0,"end_ms":2250,"start_frame":0,"end_frame":56}
  ],
  "raw": {
    "asr":   {"task":"asr","segments":[...]},
    "sed":   {"task":"sed","events":[...]},
    "music": {"task":"music","present":false},
    "vl":    [{"shot_id":"shot 1","shot_size":"中景",...}]
  }
}
```

## 4. 出参 JSON Schema（最终对接前端）
```json
{
  "$schema": "http://json.studiolinter.com/draft-07/schema#",
  "type": "object",
  "properties": {
    "video_meta": {
      "type":"object",
      "properties":{
        "filename":{"type":"string"},
        "duration_sec":{"type":"number"},
        "total_shots":{"type":"integer"},
        "subtask_id":{"type":"string"}
      }
    },
    "shots": {
      "type":"array",
      "items":{
        "type":"object",
        "properties":{
          "shot_number":{"type":"string"},
          "time_range":{"type":"string","pattern":"^\\d{2}:\\d{2}:\\d{2},\\d{3}-\\d{2}:\\d{2}:\\d{2},\\d{3}$"},
          "shot_size":{"type":"string"},
          "angle_camera_composition":{"type":"string"},
          "camera_movement":{"type":"string"},
          "content_info":{"type":"string"},
          "env_sound":{"type":"string"},
          "music":{"type":"string"},
          "speech":{"type":"string"}
        },
        "required":["shot_number","time_range","shot_size","angle_camera_composition","camera_movement","content_info","env_sound","music","speech"]
      }
    },
    "overview":{"type":"string"},
    "full_caption":{"type":"string"},
    "raw_subskills":{"type":"object"}
  },
  "required":["video_meta","shots","overview","full_caption"]
}
```

## 5. 生产版 System Prompt
```
你是分镜标注汇总与校验引擎。输入是各子模块的原始结果和镜头时间清单，输出最终结构化分镜 JSON。
必须执行：
1. 对齐：把 ASR/SED/Music 的时间戳按区间归属到对应 shot；VL 结果按 shot_id 直接对应。
2. 台词处理：ASR 文本按 shot 归属，包裹 <d>…</d>；在 shot 边界插入 <sceneRatio>；低置信/截断加 <cutoff>；说话人标 S1/S2（默认单说话人 S1，多说话人无法判别时 S1 并标 needs_review）。
3. 字段规则：空值填"无"；子模块标"无法识别"或 needs_review 的字段原样保留"无法识别"。
4. 时间区间校验：shot 时间不可重叠、不可越界(须落在视频时长内)、须按播放顺序排列；若与 manifest 冲突以 manifest 为准并记 interval_fixed。
5. 音画分离自检：content_info/shot_size/angle/camera_movement 不得含声音描述；speech/env_sound/music 不得含画面描述。发现越界则清空该越界内容并标 needs_review。
6. 补全：overview 用一句话概括整片基调/场景/节奏；full_caption 按"总体信息：\n分镜表：\n  shot N\n    时间段：…\n    景别：…\n    …"格式拼接。
7. 输出必须是纯 JSON（键名见 Schema），禁止 markdown、代码块、任何解释文字。
```

## 6. 精简 API 版（调试用）
```
# 调试：先逐 shot 复述你如何对齐各子模块结果、发现哪些冲突，再输出 JSON。
# 可要求模型输出 needs_review 字段清单，方便定位人工复核点。
```

## 7. 输入示例（节选）
```json
{
  "model":"qwen-plus",
  "shot_manifest":[{"shot_id":"shot 1","start_ms":0,"end_ms":2250}],
  "raw":{
    "asr":{"task":"asr","segments":[{"start_ms":1000,"end_ms":2820,"text":"下一球注意落点"}]},
    "sed":{"task":"sed","events":[{"type":"乒乓球撞击声","start_ms":1200,"end_ms":1350}]},
    "music":{"task":"music","present":false},
    "vl":[{"shot_id":"shot 1","shot_size":"中景","angle_camera_composition":"平视正面机位","camera_movement":"固定镜头","content_info":"两名运动员持拍对峙"}]
  }
}
```

## 8. 输出示例（成功）
```json
{
  "video_meta":{"filename":"pingpong.mp4","duration_sec":13.46,"total_shots":7,"subtask_id":"864689235027272"},
  "shots":[
    {
      "shot_number":"shot 1",
      "time_range":"00:00:00,000-00:00:02,250",
      "shot_size":"中景",
      "angle_camera_composition":"平视正面机位，两名运动员分列球台两侧，人物居中",
      "camera_movement":"固定镜头",
      "content_info":"两名运动员持拍对峙，表情专注，无入画出画，互相观察，无转场",
      "env_sound":"乒乓球撞击声",
      "music":"无",
      "speech":"画外 S1 <d>下一球注意落点</d>"
    }
  ],
  "overview":"13秒乒乓球对攻片段，室内球馆，中近景为主，节奏紧凑。",
  "full_caption":"总体信息：13秒乒乓球对攻片段，室内球馆，中近景为主，节奏紧凑。\n分镜表：\n  shot 1\n    时间段：00:00:00,000-00:00:02,250\n    景别：中景\n    角度/机位/构图：平视正面机位，两名运动员分列球台两侧，人物居中\n    运镜：固定镜头\n    内容信息：两名运动员持拍对峙，表情专注，无入画出画，互相观察，无转场\n    环境音/特殊音效：乒乓球撞击声\n    音乐：无\n    人声/台词：画外 S1 <d>下一球注意落点</d>",
  "raw_subskills":{"asr":{/*原样*/},"sed":{/*原样*/},"music":{/*原样*/},"vl":[/*原样*/]}
}
```

## 9. 失败样例 / 降级
- **LLM 返回非 JSON / 缺必填字段** → 精简约束重试 2 次；仍失败 → 触发"保底合并"：用确定性代码（不调 LLM）按 manifest 拼接各子模块原始结果生成 JSON，全部 `needs_review=true`，保证前端至少有可编辑草稿。
- **某子模块缺失**：对应字段填"无法识别"，不阻断。
- **时间冲突**：以 manifest 为准修正并记录 `interval_fixed=true`。

## 10. 配置项
```yaml
merge:
  model: qwen-plus
  quality_model: qwen-max
  api: openai_compatible
  timeout_sec: 60
  retry: 2
  fallback_to_deterministic: true   # LLM 失败时用代码保底合并
```
