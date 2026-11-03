# Skill 3 · 音乐理解 Skill（BGM 曲风 / 情绪 / 起止变化）

## 1. 能力说明
识别视频背景音乐的**曲风、情绪、起止与变化**，供表单「音乐」字段。输入为 `no_vocals.wav`（含伴奏，比纯人声轨更适合识别 BGM）。

## 2. 触发条件 / 依赖
- **触发**：预处理 Demucs 完成，产出 `no_vocals.wav` 后，与 Skill 1/2/4 并行。
- **依赖**：预处理音频产物；使用 `qwen3.8-omni-flash` 音频理解（备选 `qwen3-omni-30b-a3b-captioner` 整体音频描述后抽取音乐部分）。

## 3. 入参 Schema
```json
{
  "audio_url": "https://.../no_vocals.wav",
  "model": "qwen3.8-omni-flash",
  "task": "music_understanding"
}
```

## 4. 出参 JSON Schema
```json
{
  "$schema": "http://json.studiolinter.com/draft-07/schema#",
  "type": "object",
  "properties": {
    "task": {"const": "music"},
    "present": {"type": "boolean", "description": "是否检测到背景音乐"},
    "style": {"type": "string", "description": "曲风，如 流行/电子/古典；无则空"},
    "emotion": {"type": "string", "description": "情绪，如 紧张/欢快/舒缓；无则空"},
    "segments": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "start_ms": {"type": "integer"},
          "end_ms": {"type": "integer"},
          "change": {"type": "string", "description": "该段相对前段的变化，如 进入副歌/淡出"}
        },
        "required": ["start_ms","end_ms"]
      }
    }
  },
  "required": ["task","present"]
}
```

## 5. 生产版 System Prompt
```
你是背景音乐分析引擎。仅依据提供的音频，判断其中的背景音乐。
规则：
1. 音画分离：只听音频，不根据画面脑补音乐。
2. 未检测到 BGM → {"task":"music","present":false}，对应表单填"无"。
3. 检测到则给出 style(曲风)、emotion(情绪)、segments(按起止时间与变化分段)。
4. 时间用秒(小数)，封装层转毫秒。
5. 输出必须是纯 JSON，禁止 markdown、代码块、解释。
6. 不确定不编造；置信度低标注 present=false。
```

## 6. 精简 API 版（调试用）
```
# 调试：先自由描述这段音频的音乐特征（曲风/乐器/情绪/有无人声残留），再输出 JSON。
```

## 7. 输入示例
```json
{"audio_url":"oss://bucket/no_vocals.wav","model":"qwen3.8-omni-flash","task":"music_understanding"}
```

## 8. 输出示例（成功）
```json
{
  "task":"music","present":true,
  "style":"电子节奏","emotion":"紧张激烈",
  "segments":[{"start_ms":0,"end_ms":7000,"change":"全程铺底节奏"}]
}
```
无音乐：
```json
{"task":"music","present":false}
```

## 9. 失败样例 / 降级
- **返回非 JSON** → 重试 2 次；仍失败 → `{"task":"music","present":false,"needs_review":true}`，merge 层「音乐」标「无法识别」。
- **人声残留干扰**：因输入是 no_vocals（已去人声），理论上只剩伴奏；若模型误把残留当音乐，merge 层结合 ASR 结果交叉校验。

## 10. 配置项
```yaml
music:
  model: qwen3.8-omni-flash
  fallback_model: qwen3-omni-30b-a3b-captioner
  api: openai_compatible
  timeout_sec: 30
  retry: 2
```
