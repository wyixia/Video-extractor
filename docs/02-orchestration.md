# 编排调度逻辑（Orchestration）

> 配套主文档 `01-architecture-overview.md`，子 Skill 物料见 `../skills/`。

## 1. 阶段与依赖

```
P0 预处理（串行，CPU）
   split_shots ──► extract_keyframes ──► separate_audio
        │                 │                    │
        ▼                 ▼                    ▼
   shot_manifest      frames/<shot>/*.jpg   vocals.wav / no_vocals.wav
        │
        ▼
P1 并行（云端 API）
   ┌─ ASR   (vocals.wav)        ──┐
   ├─ SED   (no_vocals.wav)      ──┤  3 个整视频任务
   ├─ Music (no_vocals.wav)      ──┘
   └─ VL    (frames, 逐 shot 循环) ── N 个 shot 任务
        │
        ▼
P2 汇总&校验（串行，LLM）
   merge → align → validate → overview + full_caption
        │
        ▼
P3 导出
   form_ready.json（9 字段/镜 + overview + full_caption）
```

## 2. 并发策略

| 任务 | 粒度 | 并发 | 说明 |
|------|------|------|------|
| ASR | 整视频 1 次 | 与 SED/Music/VL 并行 | 依赖 vocals.wav |
| SED | 整视频 1 次 | 同上 | 依赖 no_vocals.wav |
| Music | 整视频 1 次 | 同上 | 依赖 no_vocals.wav |
| VL | 每 shot 1 次 | 线程池内并发，受 `max_workers` 限制 | 依赖 frames |
| Merge | 1 次 | **必须等 P1 全部结束** | 依赖 4 路结果 |

- 实现：`concurrent.futures.ThreadPoolExecutor(max_workers=cfg.runtime.max_workers)`。
- P1 一次提交 `3 + N` 个 future；`as_completed` 收集。
- 单个 future 异常**就地捕获并降级**，不向主流程抛出，保证其余任务继续。

## 3. 逐 shot 循环逻辑

```python
for shot in manifest.shots:
    futures[pool.submit(vl_skill.run, shot)] = shot.shot_id
for fut in as_completed(futures):
    sid = futures[fut]
    try:    vl_results[sid] = fut.result()
    except: vl_results[sid] = degraded_vl(shot)   # 无法识别
```

## 4. 重试与超时

- 统一 `retry_call(fn, retries=2, backoff=[1,2])`：
  - 捕获 `TimeoutError / ConnectionError / 5xx / JSON 解析失败` 才重试；
  - 4xx（鉴权/参数错）**不重试**，直接降级并告警。
- 每次调用独立超时：普通 30s，merge 60s（可配）。
- 重试仍失败 → 该字段「无法识别」+ `needs_review=true`。

## 5. 开关与降级矩阵

| 配置 | 关闭/失败时字段 |
|------|----------------|
| `skills.asr.enabled=false` | `speech="无"` |
| `skills.sed.enabled=false` | `env_sound="无"` |
| `skills.music.enabled=false` | `music="无"` |
| `skills.vl.enabled=false` / 失败 | 景别/机位/运镜/内容 = `无法识别` |
| merge LLM 失败 | 代码保底合并（确定性拼接），全量 `needs_review=true` |

## 6. 幂等 / 缓存（扩展点）

- 预留 `CacheBackend` 接口：key = `sha1(shot_id + 输入帧hash + prompt版本)`。
- 当前默认关闭；后续可接 Redis/本地磁盘，避免重复计费。

## 7. 下沉本地部署扩展点

- 所有云端调用收敛到 `BaseClient.chat()/transcribe()` 两个接口。
- 未来新增 `LocalClient`（vLLM / Ollama / 本地 Qwen-VL）只需实现同一接口，`config.provider` 切换，业务代码零改动。
