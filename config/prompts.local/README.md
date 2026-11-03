# 本地提示词覆盖

把同名 `.txt` 放在这个目录，运行时会**优先使用这里的版本**，内置的 `config/prompts/` 不动。
删掉这里的文件就回到内置默认。

```
config/prompts.local/vl.txt     →  覆盖 config/prompts/vl.txt
config/prompts.local/sed.txt    →  覆盖 config/prompts/sed.txt
```

## 建议的改法

先从内置目录复制一份，再改：

```bash
cp config/prompts/vl.txt config/prompts.local/vl.txt
```

## 哪些能改，哪些不能改

| 可以改 | 不能改 |
|---|---|
| 措辞、语气、示例 | 输出 JSON 的键名（`shot_size`、`content_info`、`voice`…） |
| 子项顺序与详略 | 时间字段名（`start_time`、`end_time`） |
| 要求的字数和详细程度 | "只输出 JSON"这类硬约束 |
| 情绪基调的取值范围 | 各字段的枚举值（如 `mouth_state` 只能是 none/speaking/singing/unknown） |

改坏键名不会报错，而是**静默产出"无法识别"**——代码取不到值，字段全部降级。
所以每次运行前会自动检查一遍，缺键名会直接中止并指出缺哪个。
确认要强行运行可以加 `--skip-prompt-check`。

## 结果里会留痕

`--full` 输出的完整 JSON 里有 `prompt_sources` 字段：

```json
"prompt_sources": {"asr": "builtin", "vl": "local"}
```

事后对比两次结果时，能分清差异是来自提示词改动还是模型波动。
