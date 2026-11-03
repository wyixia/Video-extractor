# Skill 2 · 音频事件识别 Skill（SED 环境音效）

## 1. 能力说明
识别视频中的**环境音 / 特殊音效**（如乒乓球撞击、脚步、掌声、门铃），输出带时间戳的事件列表，供表单「环境音/特殊音效」字段。输入为去掉人声后的 `no_vocals.wav`，避免人声干扰。

## 2. 触发条件 / 依赖
- **触发**：预处理 Demucs 完成，产出 `no_vocals.wav` 后，与 Skill 1/3/4 并行。
- **依赖**：预处理音频产物；使用 `qwen3.8-omni-flash` 的声音事件检测能力（官方支持 JSON 时间戳输出）。

## 3. 入参 Schema
```json
{
  "audio_url": "https://.../no_vocals.wav",
  "model": "qwen3.8-omni-flash",
  "mode": "detect_all",          // detect_all=列出全部事件；detect_list=仅检测给定事件
  "event_labels": [],            // mode=detect_list 时填写，如 ["乒乓球撞击声","脚步声"]
  "time_unit": "second"          // 模型返回秒(小数)
}
```

## 4. 出参 JSON Schema
```json
{
  "$schema": "http://json.studiolinter.com/draft-07/schema#",
  "type": "object",
  "properties": {
    "task": {"const": "sed"},
    "events": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "type": {"type": "string", "description": "事件名，如 乒乓球撞击声"},
          "start_ms": {"type": "integer"},
          "end_ms": {"type": "integer"},
          "confidence": {"type": "number", "minimum": 0, "maximum": 1}
        },
        "required": ["type","start_ms","end_ms"]
      }
    }
  },
  "required": ["task","events"]
}
```

## 5. 生产版 System Prompt
```
你是环境音效识别引擎。仅依据提供的音频，识别其中的环境音与特殊音效。
规则：
1. 音画分离：只识别"听到的声音"，不得根据画面脑补音效。
2. 若未检测到任何环境音/特殊音效，输出 {"task":"sed","events":[]}（对应表单填"无"）。
3. 对每个事件给出 type（简洁中文名）、start_time、end_time（秒，小数，如 4.5）。
4. 输出必须是纯 JSON 数组/对象，禁止 markdown、代码块、解释性文字。
5. 不确定或无法识别的内容不要编造；置信度低可不输出。
```

## 6. 精简 API 版（调试用）
```
# 调试 Prompt（可看模型原话排查漏检/误检）
"请听这段音频，列出里面出现的所有环境音和特殊音效，给出开始/结束时间（秒）。
先自由描述你听到什么，再输出 JSON。"
```

## 7. 输入示例
```json
{"audio_url":"oss://bucket/no_vocals.wav","model":"qwen3.8-omni-flash","mode":"detect_all"}
```

## 8. 输出示例（成功）
```json
{
  "task": "sed",
  "events": [
    {"type":"乒乓球撞击声","start_ms":1200,"end_ms":1350,"confidence":0.92},
    {"type":"脚步声","start_ms":3000,"end_ms":3400,"confidence":0.78}
  ]
}
```

## 9. 失败样例 / 降级
- **模型返回非 JSON / 格式错** → 重试 2 次（精简约束）；仍失败 → `{"task":"sed","events":[],"needs_review":true}`，merge 层该 shot「环境音」标「无法识别」。
- **音频为空/无环境音** → `{"task":"sed","events":[]}`，表单填"无"。
- **注意**：`qwen3.8-omni-flash` 事件检测返回秒，封装函数需 ×1000 转毫秒对齐。

## 10. 配置项
```yaml
sed:
  model: qwen3.8-omni-flash
  mode: detect_all
  api: openai_compatible
  base_url_env: DASHSCOPE_BASE_URL
  api_key_env: DASHSCOPE_API_KEY
  timeout_sec: 30
  retry: 2
```
