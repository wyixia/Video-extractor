# Skill 1 · 音频解析 Skill（ASR 语音转写）

## 1. 能力说明
将预处理层 Demucs 分离出的**人声轨（vocals.wav）**转写为带时间戳的文本，供表单「人声/台词」字段。仅处理人声，不处理环境音/音乐（音画分离原则）。

## 2. 触发条件 / 依赖
- **触发**：预处理层完成 Demucs 分离，产出 `vocals.wav` 后。
- **依赖**：预处理音频产物；与 Skill 2/3/4 并行。
- **调用方式**：DashScope 异步 transcription API（`paraformer`），时间戳固定开启。

## 3. 入参 Schema
```json
{
  "audio_url": "https://.../vocals.wav",   // 或本地路径经上传后返回的 URL
  "model": "paraformer",
  "language": "zh",                         // 默认中文；多语种用 'auto'
  "enable_words": true,                    // 返回字级时间戳
  "sample_rate": 16000
}
```

## 4. 出参 JSON Schema
```json
{
  "$schema": "http://json.studiolinter.com/draft-07/schema#",
  "type": "object",
  "properties": {
    "task": {"const": "asr"},
    "language": {"type": "string"},
    "segments": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "start_ms": {"type": "integer"},
          "end_ms": {"type": "integer"},
          "text": {"type": "string"},
          "words": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "start_ms": {"type": "integer"},
                "end_ms": {"type": "integer"},
                "text": {"type": "string"}
              },
              "required": ["start_ms","end_ms","text"]
            }
          }
        },
        "required": ["start_ms","end_ms","text"]
      }
    }
  },
  "required": ["task","segments"]
}
```

## 5. 生产版 System Prompt
> 本 Skill 走固定 ASR API（无 LLM 自由文本 Prompt），以下为 API 调用约束说明（等价于 System 级约束），供封装函数与重试逻辑使用。
```
模型: paraformer
约束:
1. 仅转写人声轨，禁止把环境音/音乐当台词输出。
2. 时间戳固定开启，返回句级(sentences[].begin/end_time)与字级(words[])。
3. 输出必须是 JSON，结构见出参 Schema，无 markdown、无解释。
4. 无语音的片段不产出 segment。
5. 转写失败(空结果/异常) → 调用方降级为 {"task":"asr","segments":[],"needs_review":true}。
```

## 6. 精简 API 版（调试用）
```
# 调试：直接调用 paraformer，打印原始 sentences + words
# 目的：确认时间戳精度、语种识别、是否混入环境音
# 不强制 JSON，可看原始返回排查问题
```

## 7. 输入示例
```json
{"audio_url":"oss://bucket/vocals.wav","model":"paraformer","language":"zh","enable_words":true}
```

## 8. 输出示例（成功）
```json
{
  "task": "asr",
  "language": "zh",
  "segments": [
    {"start_ms": 1000, "end_ms": 2820, "text": "下一球注意落点",
     "words": [
       {"start_ms":1000,"end_ms":1400,"text":"下一球"},
       {"start_ms":1400,"end_ms":1820,"text":"注意"},
       {"start_ms":1820,"end_ms":2820,"text":"落点"}
     ]}
  ]
}
```

## 9. 失败样例 / 降级
- **场景 A**：音频为空 / 无语音 → `{"task":"asr","segments":[],"needs_review":false}`（人声台词字段填"无"）。
- **场景 B**：API 超时/返回异常 → `{"task":"asr","segments":[],"needs_review":true}`（merge 层将该 shot 的「人声/台词」标「无法识别」）。
- **说话人(S1/S2)说明**：`paraformer` 本身不做说话人分离。S1/S2 标签在 Skill 5 汇总层按策略赋值（默认策略：单说话人标 S1；多说话人时由 VL 结合画面人物或简易 diarization 推断，推断不出则标 S1 并置 `needs_review`）。*diarization 为扩展点，当前不强制。*

## 10. 配置项
```yaml
asr:
  model: paraformer
  api: dashscope_async_transcription
  language: zh
  enable_words: true
  timeout_sec: 30
  retry: 2
  region: cn-beijing
```
