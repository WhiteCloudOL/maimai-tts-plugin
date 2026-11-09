# MaiBot 插件语音 API 对接说明

本文描述仓库 main 分支的 API 版本 1。麦麦 TTS 通过 MaiBot 插件 SDK 提供服务商与音色查询、参数描述、语气分析、音频合成、聊天投递和后台任务能力。调用方使用已配置的语音服务商与音色，通过自然语言偏好或平台专属参数控制本次表达。

## 1. 接入信息

| 项目 | 值 |
| --- | --- |
| 插件 ID | `WhiteCloudOL.maimai-tts-plugin` |
| API 版本 | `1` |
| 完整名称 | `WhiteCloudOL.maimai-tts-plugin.<接口名>` |
| SDK 范围 | 与插件 `_manifest.json` 的 `sdk` 范围一致 |
| 调用通道 | MaiBot SDK 的 `ctx.api.call` 或 `ctx.call_capability("api.call", ...)` |

在调用方 `_manifest.json` 的 `capabilities` 中加入 `api.call`，合并保留调用方已有的能力声明：

```json
{
  "capabilities": ["api.call"]
}
```

在麦麦 TTS 的「通用设置 → 插件 API」中开启「启用插件 API」，在「语音路由」中选择首选服务商与模型。调用通过 MaiBot Host 的插件 API 路由完成。

### 1.1 共用调用函数

合成、发送、账号音色同步和语气分析使用较长的 RPC 时间预算：

```python
async def call_tts(self, name: str, **params):
    response = await self.ctx.call_capability(
        "api.call",
        timeout_ms=600_000,
        api_name=f"WhiteCloudOL.maimai-tts-plugin.{name}",
        version="1",
        args=params,
    )
    if response.get("ok") is True:
        return response["data"]
    error = response.get("error", {})
    if isinstance(error, dict):
        raise RuntimeError(f"{error.get('code', 'HOST_ERROR')}: {error.get('message', response)}")
    raise RuntimeError(str(error))
```

该函数适合作为调用方插件的实例方法，后文示例使用 `self.call_tts(...)`。快速查询也可直接使用：

```python
response = await self.ctx.api.call(
    "WhiteCloudOL.maimai-tts-plugin.tts_status",
    version="1",
)
```

### 1.2 返回结构

成功结果：

```json
{
  "ok": true,
  "request_id": "weather:2026-11-08:morning",
  "data": {}
}
```

业务错误结果：

```json
{
  "ok": false,
  "request_id": "weather:2026-11-08:morning",
  "error": {
    "code": "INVALID_PARAMETER",
    "message": "参数修正说明"
  }
}
```

合成请求会返回 `request_id`。目录、状态和分块查询使用 `ok` 与 `data`。Host 的 API 解析与插件加载状态通过 SDK 调用结果提供。

## 2. 简单调用

`tts_simple` 根据正文与可选的 `style_hint` 自动选择表达，默认沿用整体语音路由。`output` 提供音频返回和聊天发送两种用法。

### 2.1 生成音频

```python
result = await self.call_tts(
    "tts_simple",
    text="晚安，今天也辛苦了。",
    style_hint="轻声、温柔，语速舒缓",
    output="audio",
    request_id="companion:goodnight:001",
)
audio_id = result["audio"]["audio_id"]
```

`result["audio"]` 提供音频编号、字节数、格式、真实时长、清理后的正文、实际服务商与模型，以及语调决定和缓存来源。音频数据通过 `tts_get_audio` 分块读取。

### 2.2 合成并发送

```python
result = await self.call_tts(
    "tts_simple",
    text="今天晴，适合出门走走。",
    output="send",
    stream_id=stream_id,
    keep_text=True,
    request_id="weather:morning:001",
)
receipt = result["send"]
```

`stream_id` 使用 MaiBot 注入到命令、工具或事件上下文的真实聊天流 ID，也可通过 SDK 聊天流查询能力获取。`keep_text=True` 时先投递清理后的文字，再投递语音。`send` 中包含 `sent` 和宿主返回的 `message_id`。

### 2.3 简单调用参数

| 参数 | 类型 | 默认值 | 用途 |
| --- | --- | --- | --- |
| `text` | string | 必填 | 完整朗读正文 |
| `provider_id` | string | `""` | 选择目录中的服务商；省略时采用整体路由 |
| `model_id` | string | `""` | 选择该服务商模型库中的模型；配合 `provider_id` 使用 |
| `voice_id` | string | `""` | 指定音色 ID 或该模型下已收藏的音色名称 |
| `style_hint` | string | `""` | 自然语言表达偏好，最多 500 字符 |
| `output` | string | `"audio"` | `audio` 返回音频结果，`send` 合成后发送 |
| `stream_id` | string | `""` | 发送模式的真实聊天流 ID |
| `keep_text` | boolean | `false` | 发送模式同时投递文字 |
| `request_id` | string | 自动生成 | 追踪与去重编号，最多 128 字符 |

正文保持原样，表达由平台能力决定：

| 组合能力 | 自动表达 |
| --- | --- |
| Breeze | 整段风格、原文锚点事件、局部表演及停顿 |
| 自由指令型模型与音色 | 符合当前长度额度的整段风格指令 |
| 原生情感枚举型模型与音色 | 当前允许列表中的语调 ID |
| 原文朗读型模型与音色 | 按正文和已保存的合成设置生成音频，`llm_used=false` |

自动分析使用「通用设置 → 语音辅助 LLM」选定的模型任务、超时和重试次数。该任务对应的服务商与具体模型由 MaiBot 配置分配，推荐选择低延迟文本模型。概率语音与 API 自动模式共用这组 LLM 设置，各自的业务开关独立生效。

## 3. 服务商、模型和音色选择

### 3.1 `tts_catalog`

```python
catalog = await self.call_tts("tts_catalog")
provider = next(item for item in catalog["providers"] if item["enabled"])
model = provider["models"][0]
```

返回：

- `revision`：本次工作台配置版本。
- `primary`：首选路由，包含 `provider_id`、`model_id` 和音色覆盖。
- `fallbacks`：按执行顺序排列的备选路由。
- `providers`：服务商 `id`、`name`、`kind`、`enabled`、`default_model_id` 和已保存模型列表。
- 每个模型提供 `id`、`name` 和 `default_voice_id`。

指定 `provider_id` 后，调用采用该服务商中的所选模型。省略服务商时采用工作台的首选与备选路由。模型通过工作台模型库保存后可供 API 选择。

### 3.2 `tts_voices`

```python
voices = await self.call_tts(
    "tts_voices",
    provider_id=provider["id"],
    model_id=model["id"],
    source="saved",
)
```

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `provider_id` | `""` | 服务商选择器 |
| `model_id` | `""` | 模型选择器 |
| `source` | `"saved"` | `saved` 收藏与默认音色，`preset` 内置目录，`account` 平台账号音色 |
| `refresh` | `false` | 主动刷新账号音色缓存 |

返回 `provider_id`、`model_id`、`default_voice_id` 和 `voices`。音色条目提供 `id`、`name`、`model_id`；收藏条目还提供创建状态 `state`。

账号同步按所选平台的账号列表接口执行，沿用工作台已有的同步条件与列表缓存。Breeze 的目录语言和关键词条件可在工作台中配置。

## 4. 参数描述与静态校验

### 4.1 `tts_describe`

该接口根据服务商、模型、音色返回可调用能力与参数 JSON Schema，适合构建调用方的参数表单。

```python
description = await self.call_tts(
    "tts_describe",
    provider_id=provider["id"],
    model_id=model["id"],
    voice_id=voices["default_voice_id"],
)
parameter_properties = description["parameters"]["properties"]
```

返回内容：

| 字段 | 用途 |
| --- | --- |
| `provider_id`、`model_id`、`voice_id` | 实际解析的合成目标 |
| `tone_supported`、`tone_mode` | 语调控制能力与已配置模式 |
| `allowed_tones`、`tone_meanings` | 当前允许的语调 ID 与表达含义 |
| `style_supported`、`direction_limit` | 自由风格能力与本次可用字符额度 |
| `events` | Breeze 的实际接口与语种事件表 |
| `parameters` | 当前组合的参数 JSON Schema |

参数描述包含类型、枚举、取值范围、当前默认值及字段说明。字段集同时考虑模型、音色、资源、返回格式和传输方式。格式、资源、参考音频来源或传输方式调整后，再次查询可取得对应的字段集。

`parameters` 入参可预先应用格式或资源等调整，再查询对应的参数描述：

```python
description = await self.call_tts(
    "tts_describe",
    provider_id=provider["id"],
    model_id=model["id"],
    parameters={"response_format": "wav"},
)
```

常用参数按平台分配，具体字段和枚举以接口返回的 Schema 为准：

| 平台 | 常用合成参数 |
| --- | --- |
| OpenAI / OpenAI 兼容 | `speed`、`response_format`、`custom_voice` |
| 千问 | `language`、`optimize_instructions`；Qwen-Audio 模型提供采样率、语速、音量、音调、种子等参数 |
| CosyVoice | `speed`、`sample_rate`、`volume`、`pitch`，以及对应模型和协议的种子、语言、SSML 与 Opus 码率 |
| MiniMax / 百炼 MiniMax | `minimax_vol`、`minimax_pitch`、`minimax_emotion`、`minimax_channel`、`sample_rate`、音色混合和声音效果；MiniMax 原生接口还提供传输方式、返回方式与文本归一化 |
| 火山引擎 | `resource_id`、`volc_loudness_rate`、`volc_pitch`、`volc_emotion`、`volc_instruction_mode` 等模型专属参数 |
| 硅基流动 | `speed`、`sample_rate`、`gain` |
| Breeze | 在线模型的语速、音量、语言、引导强度和声音相似度；本地API 的引导强度、随机种子、声音来源、参考录音及 `breeze_cpp_*` 采样与分段参数 |
| MiMo | `response_format`；表达通过风格控制设置 |
| GPT-SoVITS | `speed`、文本与提示语言、分段方法、`top_k`、`top_p`、`temperature` |

参考录音按来源配置：`reference_source="url"` 搭配 `reference_url`；`reference_source="file"` 搭配已导入插件录音目录中的相对路径 `reference_file`；GPT-SoVITS 的 `reference_source="server"` 搭配 TTS 服务端路径 `reference_path`。Breeze 参考声音使用 `breeze_mode="reference"` 和准确的 `reference_text`；硅基流动使用 `reference_enabled=True` 与录音文字稿。MiMo voiceclone 提供对应的参考录音字段。

Breeze 本地API 对接 Breeze-TTS-2.cpp 原版及鉴权排队版，声音来源可选 `reference`、`design`、`plain`、`saved`。填写服务端 `voice_id` 时自动采用保存的音色；已收藏的参考录音与文字设计按各自配置解析。风格通过 `control.style` 或自动模式设置，`seed` 控制随机种子；`breeze_cpp_split_chars=null` 沿用服务端分段配置，0 使用整段生成。参数与连接步骤见 [BreezeTTS 本地API配置](breeze-local-api.md)。MiniMax 的 `minimax_normalization="quality"` 对应 Speech 2.6 / 2.8 的 HTTP 模式，流式模式提供 `basic` 归一化。各组合的枚举和范围以当次 `tts_describe` 为准。

### 4.2 `tts_validate`

```python
validated = await self.call_tts(
    "tts_validate",
    request={
        "text": "欢迎回来。",
        "provider_id": provider["id"],
        "model_id": model["id"],
        "control": {"mode": "auto", "style_hint": "温暖、自然"},
        "parameters": {"response_format": "wav"},
        "output": "audio",
    },
)
```

该接口完成本地参数解析与能力校验，返回配置 `revision`、实际 `targets`、`output` 和 `llm_required`。`targets` 中的参数默认值体现本次临时覆盖。自动表达的最终决定由后续分析或合成请求完成。

整体路由逐一校验首选与备选目标。服务商专属参数配合 `provider_id` 与 `model_id` 使用，可让调用精确选择该平台的模型能力。

## 5. 高级调用

`tts_advanced` 接收一个 `request` 对象，提供模型、音色、平台参数和表达策略控制。音频返回与聊天发送均通过 `output` 选择。

```python
request = {
    "text": "欢迎回来，今天过得怎么样？",
    "provider_id": provider["id"],
    "model_id": model["id"],
    "voice_id": voices["default_voice_id"],
    "control": {"mode": "auto", "style_hint": "温暖亲切"},
    "parameters": {"response_format": "wav"},
    "output": "audio",
    "request_id": "companion:welcome:001",
}
result = await self.call_tts("tts_advanced", request=request)
```

发送模式使用相同请求结构：

```python
request.update(
    output="send",
    stream_id=stream_id,
    keep_text=True,
    request_id="companion:welcome:send:001",
)
result = await self.call_tts("tts_advanced", request=request)
```

### 5.1 请求字段

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `text` | 必填 | 完整正文，最多 4096 字符，同时遵循插件运行设置的字符上限 |
| `provider_id`、`model_id`、`voice_id` | `""` | 与简单调用相同的目标选择器 |
| `control` | `{"mode":"manual"}` | 表达策略 |
| `parameters` | `{}` | `tts_describe` 返回的参数字段与本次取值 |
| `output` | `"audio"` | 音频返回或聊天发送 |
| `stream_id` | `""` | 发送目标 |
| `keep_text` | `false` | 发送清理后的文字 |
| `delivery_format` | `"default"` | `default`、`source` 或 `silk` |
| `refresh_cache` | `false` | 为本次请求重新生成上游音频 |
| `request_id` | 自动生成 | 请求追踪与去重编号 |

音频返回模式的默认格式为服务商原始音频。发送模式的默认格式沿用「通用设置 → 语音输出」，SILK 转码沿用整数秒输出设置。

覆盖顺序为：本次 `parameters` → 音色设置 → 模型设置 → 服务商设置。本次选择与覆盖保存在请求上下文中。`tts_describe` 和结果中的 `parameters` 提供生效值。

### 5.2 `control` 字段

| 字段 | 用途 |
| --- | --- |
| `mode="auto"` | 按正文与 `style_hint` 调用语音辅助 LLM，选择平台适用的表达 |
| `mode="manual"` | 使用显式控制；省略显式控制时沿用已保存的表达设置 |
| `mode="off"` | 使用原生合成参数与已配置的基础风格 |
| `tone` | 手动模式的允许列表语调 ID |
| `style` | 手动模式的整段自由风格，最多 200 字符，同时遵循平台剩余额度 |
| `events` | Breeze 手动模式的原文锚点事件，最多 3 处 |
| `style_hint` | 自动模式的自然语言表达偏好，最多 500 字符 |

`tone` 与 `style` 选用一种。语速、资源、音量等原生合成字段放到 `parameters`。MiniMax 的 `minimax_emotion` 可在手动模式作为原生参数使用，也可通过 `control.tone` 统一选择。

自由风格示例：

```json
{"mode":"manual","style":"轻声温柔，语速舒缓"}
```

原生情感示例：

```json
{"mode":"manual","tone":"happy"}
```

Breeze 示例，配套正文为「你终于来了。我们一起面对。」：

```json
{
  "mode": "manual",
  "style": "温暖而坚定",
  "events": [{"before":"你终于来了","tag":"[轻笑]"}]
}
```

`before` 使用正文中唯一的连续短语，长度为 1–80 字符。`tag` 从 `tts_describe.events` 复制，全部事件使用同一个语言表。正文中的 Breeze 局部语气标记继续按平台语法解析。

### 5.3 结果元信息

`data.audio` 包含：

- `audio_id`、`ttl_seconds`、`size_bytes`：音频引用、保留时间与字节数。
- `response_format`、`content_type`、`duration_seconds`：最终音频格式和真实时长。
- `provider_id`、`provider_name`、`model_id`、`voice_id`：实际完成合成的目标。
- `display_text`：用于聊天文字与语音记录的清理后正文。
- `parameters`：本次目标的生效合成参数。
- `control`：控制模式、语调、自由风格、事件及 `llm_used`。
- `cache.synthesis`、`cache.delivery`：合成与转码缓存来源，取值为 `hit`、`miss`、`shared`、`bypass` 或空字符串。

`parameters.response_format` 选择服务商请求格式，`data.audio.response_format` 标识最终结果格式。Breeze 多语气区段通过 WAV 拼接，原生 PCM 通过 WAV 封装；调用方依据结果格式读取音频。SILK 转码保留完整尾音，并按通用设置在末尾补静音至整数秒。`duration_seconds` 读取最终音频的实际时长。

## 6. 先分析表达，再合成

`tts_analyze` 使用语音辅助 LLM 返回表达决定，适合调用方展示分析结果或分阶段组织语音流程。

```python
analysis = await self.call_tts(
    "tts_analyze",
    request={
        "text": "太好了，我们终于成功了！",
        "provider_id": provider["id"],
        "model_id": model["id"],
        "control": {"mode":"auto", "style_hint":"兴奋、开心"},
    },
)
result = await self.call_tts(
    "tts_advanced",
    request={
        "text": analysis["text"],
        "provider_id": analysis["provider_id"],
        "model_id": analysis["model_id"],
        "voice_id": analysis["voice_id"],
        "parameters": analysis["parameters"],
        "control": analysis["control"],
        "output": "audio",
    },
)
```

返回的 `control`、目标选择器和 `parameters` 可一起用于高级调用。`parameters` 保留本次传入的参数覆盖，使模型能力、资源和格式与分析时保持一致。`text` 保留本次输入，`display_text` 提供聊天展示正文，`llm_used` 表示分析调用情况，`control_status` 标识本次表达来源。原文已有控制时使用原有表达；原文朗读型组合返回原文合成策略。

分析请求复用平台专属提示词。固定规则与参数能力放在 system 前缀，正文、表达偏好和重试反馈放在后续消息，便于复用 LLM 前缀缓存。

## 7. 读取与复用音频

### 7.1 `tts_get_audio`

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `audio_id` | 必填 | 合成结果中的音频编号 |
| `offset` | `0` | 起始字节偏移 |
| `limit` | `65536` | 每块字节数，范围 1–262144 |

完整读取示例：

```python
import base64

async def read_tts_audio(self, audio_id: str) -> bytes:
    offset = 0
    blocks = []
    while True:
        block = await self.call_tts(
            "tts_get_audio", audio_id=audio_id, offset=offset, limit=65536,
        )
        blocks.append(base64.b64decode(block["data_base64"]))
        if block["eof"]:
            return b"".join(blocks)
        offset = block["next_offset"]
```

结果包含音频元信息、`data_base64`、`offset`、`next_offset` 与 `eof`。偏移量针对解码后的原始音频字节。

### 7.2 `tts_send_audio`

```python
result = await self.call_tts(
    "tts_send_audio",
    audio_id=audio_id,
    stream_id=stream_id,
    keep_text=True,
    request_id="broadcast:group-a:001",
)
```

该接口复用已有音频，按通用设置准备聊天语音格式，并返回 `data.send` 发送回执。已有 SILK 结果按原有编码投递，新编码结果采用当前整数秒设置。面向不同聊天流的投递使用各自的 `request_id`。

## 8. 后台任务

后台任务适合较长合成和分阶段执行流程。`tts_submit` 使用与 `tts_advanced` 相同的请求结构，也支持 `output="audio"` 与 `output="send"`。

```python
import asyncio

submitted = await self.call_tts(
    "tts_submit",
    request={
        "text": "这是一次后台播报。",
        "control": {"mode":"auto"},
        "output": "audio",
        "request_id": "broadcast:job:001",
    },
)
job_id = submitted["job_id"]

while True:
    job = await self.call_tts("tts_get_job", job_id=job_id)
    if job["state"] == "running":
        await asyncio.sleep(0.5)
    else:
        completed = job["result"]
        break
```

| 状态 | 含义 |
| --- | --- |
| `running` | 正在分析、合成、转码或发送 |
| `done` | `result` 中提供成功结果 |
| `error` | `result.error` 中提供错误码与处理说明 |
| `cancelled` | 任务已取消，结果代码为 `CANCELLED` |

取消任务：

```python
job = await self.call_tts("tts_cancel_job", job_id=job_id)
```

任务保留时长从完成时开始计时，沿用「插件 API」中的结果保留时间。任务历史最多保留 128 项。相同 `request_id` 与相同参数复用同一任务和结果；新的一次业务操作使用新的编号。

语气分析、合成、转码及发送合计使用 540 秒执行预算。调用方的 RPC 等待结束后，任务继续执行；以原 `request_id` 重试可等待同一任务，也可通过 `job_id` 查询。`tts_cancel_job` 显式取消插件任务，取消结果参与请求编号去重。已交给宿主的消息由宿主与适配器执行投递，发送回执提供 `message_id`。

## 9. 开关、容量与缓存

「通用设置 → 插件 API」提供：

| 设置 | 默认值 | 用途 |
| --- | --- | --- |
| 启用插件 API | 开启 | 控制公开业务接口的调用 |
| 音频与任务结果保留 | 600 秒 | 范围 30–86400 秒 |
| API 音频容量 | 64 MiB | 范围 1–512 MiB，按最近访问顺序回收 |
| API 活动任务上限 | 8 | 范围 1–32，覆盖语调分析、合成、转码和发送，合成并发沿用插件运行设置 |

音频引用池最多保存 256 项。引用到期、容量回收或插件重载后，调用方通过新合成请求获取新的 `audio_id`。关闭 API 时，活动 API 任务进入取消流程；`tts_status` 始终提供开关和运行概况。

合成缓存按实际服务商身份、模型、音色、正文和合成参数区分，转码缓存按源音频、编码版本及整数秒选项区分。不同 `request_id` 的相同语音内容可复用合成缓存。`refresh_cache=True` 用于主动重新合成。

每次任务采用接收时的路由、平台参数和语音辅助 LLM 配置快照。开关与音频容量按最新设置生效。参考文件、远端参考录音和 GPT-SoVITS 等动态来源按现有平台规则逐次读取；并发的相同可缓存合成共享平台请求，各任务分别维护取消状态。

## 10. 请求编号与发送回执

建议调用方使用带插件前缀的业务编号，例如 `weather:group123:morning:20261108`。相同编号配合相同参数返回同一执行结果，发送结果也参与去重。参数调整使用新的编号。

`request_id` 的去重记录与任务结果共用有效期和历史容量。调用方可持有 `job_id` 查询最终结果，面向新的业务播报生成新的编号。

`tts_send_audio` 优先复用已记录的发送结果，音频引用回收后仍可在任务记录保留期间取回原回执。`refresh_cache=True` 与新的请求编号搭配，产生一次新的合成；同一编号的重复调用继续返回该次执行结果。

## 11. 错误码与处理

| 错误码 | 处理方式 |
| --- | --- |
| `API_DISABLED` | 在通用设置中开启插件 API |
| `ROUTE_MISSING` | 配置首选语音路由 |
| `PROVIDER_MISSING` | 重新读取服务商目录并选择 ID |
| `PROVIDER_DISABLED` | 启用所选服务商 |
| `INVALID_PARAMETER` | 按 `tts_describe` 或 `tts_validate` 的说明调整参数 |
| `REQUEST_CONFLICT` | 为参数调整分配新的 `request_id` |
| `BUSY` | 等待活动任务完成后提交 |
| `LLM_ERROR` | 检查语音辅助 LLM 的任务配置与调用日志 |
| `TIMEOUT` | 调整语音辅助 LLM 的超时、重试次数与平台请求预算，使任务在 540 秒内完成 |
| `TTS_ERROR` | 按平台错误信息检查模型、音色、资源和平台额度 |
| `SEND_ERROR` | 检查聊天流与适配器的发送回执 |
| `AUDIO_CAPACITY` | 调整 API 音频容量后重新合成 |
| `AUDIO_EXPIRED` | 合成新的音频结果并更新引用 |
| `JOB_EXPIRED` | 为本次业务提交新的任务 |
| `CANCELLED` | 按当前业务需求创建新的请求 |
| `API_RELOADING` | 等待插件完成加载后调用 |
| `INTERNAL_ERROR` | 根据请求编号检查插件异常日志 |

## 12. 日志关联

API 日志使用 `api_request_id` 关联业务请求，`job_id` 关联后台任务，`audio_id` 关联结果读取。共同的 `trace` 串联合成、语调分析、平台请求、缓存与投递过程。

INFO 记录请求接收、目标路由、音频字节数、耗时、缓存来源和发送回执；DEBUG 提供查询操作与分块读取元信息；WARNING 与 ERROR 提供参数、平台或调用异常信息。正文、表达偏好和凭据在日志中使用脱敏表示。

## 13. 代码模块

| 模块 | 职责 |
| --- | --- |
| `api/endpoints.py` | 公开 SDK API 的名称、版本、签名和入口注释 |
| `api/models.py` | 请求类型、控制结构和业务错误码 |
| `api/parameters.py` | 平台参数描述、当前能力与逐次覆盖 |
| `services/plugin_api.py` | 目录、校验、语调、合成和发送的应用流程 |
| `services/adaptive_tone.py` | 概率语音与 API 共用的语气分析服务 |
| `infrastructure/api_runtime.py` | 有界音频引用、分块读取和可取消任务 |

SDK 调用约定参见 [MaiBot 插件开发指南](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/main/docs/guide.md#api)。
