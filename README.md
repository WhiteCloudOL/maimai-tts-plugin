# 麦麦 OpenAI TTS

为 MaiBot 提供 OpenAI 兼容的文本转语音能力。插件通过 `POST /audio/speech` 合成音频，并以真实语音消息发送到当前聊天流。

- 插件 ID：`maimai-openai_tts_plugin`
- 作者：[清蒸云鸭](https://github.com/WhiteCloudOL)
- 许可证：AGPL-3.0
- MaiBot Plugin SDK：2.x

## 功能

- 支持 OpenAI 官方及兼容的 TTS 端点。
- 提供 `/tts <文本>` 和 `/语音 <文本>` 命令。
- 提供概率 TTS，在出站消息进入 adapter 前按概率把单段纯文本原位转换为语音。
- 提供 `openai_tts_speak` LLM 工具，把麦麦原本要回复的完整文本转换为语音。
- 概率模式和工具模式拥有独立开关，并可分别决定是否同时保留可见文字。
- 支持配置模型、音色、语速、返回格式、风格指令、代理、超时和响应大小限制。
- 请求体只使用 OpenAI Speech API 标准字段，不包含 CosyVoice 专用适配逻辑。
- API Key 仅保存在运行时生成且被仓库忽略的 `config.toml` 中，不会写入源码或日志。

## 安装

将仓库克隆到 MaiBot 的 `plugins` 目录：

```bash
cd plugins
git clone https://github.com/WhiteCloudOL/maimai-openai-tts-plugin
```

重启 MaiBot 或在 WebUI 中重新加载插件。Runner 会根据配置模型生成 `config.toml`。

## 最小配置

在 WebUI 插件配置中填写 API Key。默认配置使用常见的 OpenAI TTS 模型与音色：

```toml
[plugin]
enabled = true
config_version = "1.1.0"

[api]
base_url = "https://api.openai.com/v1"
endpoint_path = "/audio/speech"
api_key = "你的 API Key"
timeout_seconds = 90.0

[voice]
model = "tts-1"
voice = "alloy"
response_format = "mp3"
speed = 1.0
instructions = ""
custom_voice_id = ""
max_text_length = 4096
max_audio_size_mb = 20

[feature]
command_enabled = true
tool_enabled = true
probability_enabled = false
probability = 0.2
probability_min_text_length = 1
probability_max_text_length = 300
probability_group_enabled = true
probability_private_enabled = true
probability_keep_text = false
tool_keep_text = false
allow_voice_override = true
allow_speed_override = true
```

配置中的 `base_url` 已包含 `/v1`，插件会与 `endpoint_path` 正确拼接为：

```text
https://api.openai.com/v1/audio/speech
```

## 配置说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `api.base_url` | `https://api.openai.com/v1` | OpenAI 官方或兼容服务的 Base URL |
| `api.endpoint_path` | `/audio/speech` | TTS 端点路径 |
| `api.api_key` | 空 | Bearer Token；必须填写 |
| `api.organization` | 空 | 可选 `OpenAI-Organization` 请求头 |
| `api.project` | 空 | 可选 `OpenAI-Project` 请求头 |
| `api.timeout_seconds` | `90.0` | 单次请求总超时 |
| `api.proxy_url` | 空 | 可选 HTTP/HTTPS 代理 |
| `api.use_system_proxy` | `false` | 是否读取系统代理环境变量 |
| `voice.model` | `tts-1` | 上游模型名称 |
| `voice.voice` | `alloy` | 默认音色 |
| `voice.custom_voice_id` | 空 | OpenAI 自定义音色 ID；填写后按 `{ "id": "voice_xxx" }` 发送并优先于默认音色 |
| `voice.response_format` | `mp3` | 可选 `mp3/opus/aac/flac/wav/pcm`；聊天平台优先推荐 `mp3` |
| `voice.speed` | `1.0` | 语速，范围 `0.25` 到 `4.0` |
| `voice.instructions` | 空 | OpenAI 部分模型支持的风格指令；`tts-1` 和 `tts-1-hd` 不支持 |
| `voice.max_text_length` | `4096` | 单次文本字符上限，与 OpenAI Speech API 上限一致 |
| `voice.max_audio_size_mb` | `20` | 单次响应体积上限 |
| `feature.command_enabled` | `true` | 是否开放聊天命令 |
| `feature.tool_enabled` | `true` | 是否开放 LLM 工具 |
| `feature.probability_enabled` | `false` | 是否启用概率 TTS；默认关闭以避免意外消耗额度 |
| `feature.probability` | `0.2` | 每条符合条件的纯文本出站消息转为语音的概率，范围 `0` 到 `1` |
| `feature.probability_min_text_length` | `1` | 概率模式处理的最短文本 |
| `feature.probability_max_text_length` | `300` | 概率模式处理的最长文本，同时受 `voice.max_text_length` 限制 |
| `feature.probability_group_enabled` | `true` | 是否处理群聊出站文本 |
| `feature.probability_private_enabled` | `true` | 是否处理私聊出站文本 |
| `feature.probability_keep_text` | `false` | 概率转换后是否在同一条消息中同时保留原文字 |
| `feature.tool_keep_text` | `false` | 工具调用后是否在同一条消息中同时发送回复文字与语音 |
| `feature.allow_voice_override` | `true` | 是否允许工具临时指定音色 |
| `feature.allow_speed_override` | `true` | 是否允许工具临时指定语速 |

## 使用

聊天命令：

```text
/tts 你好，这是一段测试语音。
/语音 今天天气真不错。
```

启用 LLM 工具后，麦麦可以把原本要回复的完整文本作为 `text` 参数交给 `openai_tts_speak`，由工具合成并发送语音。工具需要当前真实的 `stream_id`，插件不会自行计算会话 ID。

概率模式只处理恰好包含一个文本段的出站消息；图片、表情、已有语音、转发消息以及混合消息不会被转换。合成失败时，Hook 会保留原文字消息，避免回复丢失。

## adapter 发送链路

插件不直接连接 QQ 或其他平台。命令和工具通过 MaiBot Host 的 `send.hybrid` 发送 `voice` 段；概率模式则通过 `send_service.after_build_message` Hook 在发送前把文本段改为 `voice` 段。两种方式最终都会进入 Platform IO，并由当前聊天流匹配到的 adapter 发往平台。

当 `probability_keep_text` 或 `tool_keep_text` 开启时，插件把 `text` 与 `voice` 放在同一条 hybrid 出站消息中；关闭时 adapter 只收到 `voice` 段。无论可见文字是否保留，内部 `processed_plain_text` 都保留原回复正文，确保聊天历史仍有可读语义。

## 接口兼容性

请求字段以 [OpenAI Create speech API](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create) 为准。

插件发送的标准请求体如下：

```json
{
  "model": "tts-1",
  "voice": "alloy",
  "input": "你好",
  "response_format": "mp3",
  "speed": 1.0
}
```

开发时另使用 OpenAI 兼容测试端点 `https://api.meowyun.cn/v1`、模型 `cosyvoice-v3-flash`、音色 `longyan_v3` 验证：`POST /audio/speech` 能返回 `audio/mpeg` 音频。这些值仅用于测试，不是插件默认值；测试令牌未写入仓库。

插件按 OpenAI Speech API 实现 `input`、`model`、`voice`、`instructions`、`response_format` 和 `speed`。`stream_format` 使用接口默认的 `audio`，不启用 SSE，因为 MaiBot 需要完整音频后才能发送语音消息。

开发与运行均复用 MaiBot 主程序环境，不需要在插件目录创建独立虚拟环境。真实接口调试只应通过本地环境变量注入临时 API Key，不要把令牌提交到仓库。
