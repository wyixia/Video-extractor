# Skill 4 · 核心画面 VL 标注 Skill（景别 / 角度机位构图 / 运镜 / 内容信息）

## 1. 能力说明
流水线的核心 Skill。仅看预处理抽出的关键帧（1~4 张），填写表单字段：景别、角度/机位/构图、运镜、内容信息。**严格音画分离：只看画面，不写声音（环境音/音乐/台词由对应子 Skill 负责）。**

## 2. 触发条件 / 依赖
- **触发**：预处理抽帧完成；**逐 shot 循环调用**（每个 shot 一次请求）。
- **依赖**：预处理帧产物；与 Skill 1/2/3 并行启动。
- **模型**：`qwen2.5-vl-72b-instruct`（可升级 `qwen3-vl-*`）。

## 3. 入参 Schema
```json
{
  "model": "qwen2.5-vl-72b-instruct",
  "frames": [
    {"url":"oss://bucket/shot001_frame0.jpg","order":0},
    {"url":"oss://bucket/shot001_frame1.jpg","order":1}
  ],
  "shot_time_window": {"start_ms":0,"end_ms":2250},   // 仅作上下文，不参与判断
  "shot_id": "shot 1"
}
```

## 4. 出参 JSON Schema（与表单字段 1:1，便于直接回填）
```json
{
  "$schema": "http://json.studiolinter.com/draft-07/schema#",
  "type": "object",
  "properties": {
    "task": {"const": "vl"},
    "shot_size": {"type":"string","description":"景别+镜头内动态变化，如 中景（由中景推至近景）"},
    "angle_camera_composition": {"type":"string","description":"拍摄角度、机位、构图、人物/物体空间关系"},
    "camera_movement": {"type":"string","description":"固定镜头/推/拉/摇/移/跟/变焦/甩镜/环绕/焦点变化，可组合"},
    "content_info": {"type":"string","description":"主体、动作、表情、入画出画、互动、转场，客观描述"}
  },
  "required": ["task","shot_size","angle_camera_composition","camera_movement","content_info"]
}
```

## 5. 生产版 System Prompt
```
你是专业影视分镜标注师。仅根据提供的画面关键帧（按顺序排列）进行客观标注，严格遵守：
1. 音画分离：禁止从画面推断或写出任何声音（环境音、音乐、台词）。声音字段由其他模块负责。
2. 逐字段填写，全部为客观描述，不加入主观评价。
3. 景别：从 {远景,全景,中景,近景,特写} 选一，并可在括号内描述镜头内景别动态变化。
4. 角度/机位/构图：说明拍摄角度(平/俯/仰/侧)、机位、主体在画面中的位置与空间关系。
5. 运镜：判断镜头运动。固定镜头明确写"固定镜头"；运动镜头从 {推,拉,摇,移,跟,变焦,甩镜,环绕,焦点变化} 选，可组合(如 推+跟)。需对比多帧判断。
6. 内容信息：描述主体外观、动作、表情、入画/出画、互动、转场。
7. 空值填"无"；无法判断填"无法识别"；禁止输出空字符串。
8. 输出必须是纯 JSON（键名见 Schema），禁止 markdown、代码块、任何解释文字。
```

## 6. 精简 API 版（调试用）
```
# 调试：先逐帧自由描述你看到的内容（帮助排查漏帧/误判），再输出 JSON。
# 可临时关闭"音画分离"约束以观察模型是否越界，用于回归测试。
```

## 7. 输入示例
```json
{
  "model":"qwen2.5-vl-72b-instruct",
  "frames":[
    {"url":"oss://bucket/shot001_f0.jpg","order":0},
    {"url":"oss://bucket/shot001_f1.jpg","order":1},
    {"url":"oss://bucket/shot001_f2.jpg","order":2}
  ],
  "shot_time_window":{"start_ms":0,"end_ms":2250},
  "shot_id":"shot 1"
}
```

## 8. 输出示例（成功）
```json
{
  "task":"vl",
  "shot_size":"中景",
  "angle_camera_composition":"平视正面机位，两名运动员分列球台两侧，人物居中，前景球网后景挡板",
  "camera_movement":"固定镜头",
  "content_info":"两名运动员持拍对峙，表情专注，无入画出画，互相观察，无转场"
}
```

## 9. 失败样例 / 降级
- **图片解析失败**：单帧损坏 → 自动重抽该帧；全部帧失败 → 该 shot 四个字段全标「无法识别」+ `needs_review=true`。
- **返回非 JSON / 缺字段** → 精简约束重试 2 次；仍失败 → 同上降级。
- **运镜判断不确定**：跨帧无位移 → 保守标"固定镜头"，不猜运动。

## 10. 配置项
```yaml
vl:
  model: qwen2.5-vl-72b-instruct
  frames_per_shot: 4          # 中间帧1 + 每2秒补抽，上限4
  api: openai_compatible
  timeout_sec: 30
  retry: 2
```
