<div align="center">

<img src="assets/icon.svg" width="88" height="88" alt="麦麦TTS语音图标">

# 麦麦多平台TTS语音

[![Plugin 2.0.3](https://img.shields.io/badge/Plugin-2.0.3-007ec6?style=flat)](https://github.com/WhiteCloudOL/maimai-tts-plugin/releases/tag/v2.0.3)
[![MaiBot 1.3.6–1.3.99](https://img.shields.io/badge/MaiBot-1.3.6--1.3.99-007ec6?style=flat)](#快速开始)
[![Plugin SDK 2.13.0–2.20.0](https://img.shields.io/badge/Plugin%20SDK-2.13.0--2.20.0-8A2BE2?style=flat)](#快速开始)
[![License AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-green?style=flat)](LICENSE)

**为麦麦赋予说话的能力！**

多平台语音合成 · 多模型与音色管理 · 参考录音克隆

[快速开始](#快速开始) · [支持平台](#支持平台) · [模型与音色](#模型与音色) · [聊天用法](#聊天用法) · [配置说明](#配置说明)

</div>


## 🩷 问题反馈&功能建议

| 方式 | 联系 |
| :--: | :--: |
| Github Issue | [跳转](https://github.com/WhiteCloudOL/maimai-tts-plugin/issues) |
| QQ群 | [637174573](https://qm.qq.com/q/3f2bdkDsyW) |

---

## 功能一览

**WebUI 与聊天**：通过聊天命令或麦麦的语音工具发送语音，也可以开启概率语音，将整轮文字回复转换为语音。在工作台中选择服务商、模型和音色，输入文字即可合成试听并播放。

**语音平台管理**：添加自己的 TTS 平台，同一平台可以创建多份独立命名的配置，分别填写 BaseURL、密钥、模型和参数；密钥按平台保存，平台配置可独立启停。

**模型与音色管理**：搜索、批量选择或手动添加模型，为各模型设置语速和说话风格；按模型收藏预设音色、自定义音色和克隆音色，也可以同步平台账号中的音色，集中编辑和试听。

**参考音频与音色克隆**：导入麦麦所在服务器的录音文件，或填写公开音频链接，将参考录音与对应文字保存到音色配置中；各平台的音色窗口提供相应的复刻、声音设计和账号音色管理功能。

**语音路由**：按 Provider/Model 选择实际使用的服务商、模型和音色，设置首选及有序备选链；首选服务调用失败时依次尝试备选。路由可在工作台保存，也可以通过 `/mmtts model` 按编号或名称切换。

**语速与语调**：在模型或音色中固定语速、情感和说话风格，也可以为支持语调控制的模型开启动态模式，让麦麦从允许列表中选择语调；插件校验语调标签后调用平台合成。

工作台沿用 MaiBot 的千禧风格，提供侧栏入口、浅色与深色主题和过渡动画。PC 使用并列表单与资源列表，手机使用单列表单、可滚动弹窗和触控操作区。

## 支持平台

| 平台 | 音色与合成能力 | 配置要点 |
| :--- | :--- | :--- |
| BreezeTTS | 本地API、官方在线、参考音色、声音设计、固定与动态语调 | 在同一服务商中选择接入方式；支持按需填写 Bearer 密钥 |
| OpenAI | 预设音色、本地录音克隆、自定义音色、说话风格 | 克隆时填写已有授权记录 ID |
| 阿里云_千问 | Qwen-Audio 3.0/3.1 TTS、Qwen3-TTS HTTP / Realtime、本地录音克隆、声音设计 | 账号音色按模型与地区管理 |
| 阿里云_CosyVoice | HTTP / WebSocket 合成、账号音色、本地录音与 URL 复刻、声音设计 | 在模型参数中选择协议，语速支持 0.5–2 倍 |
| 阿里云_MiniMax | Speech 2.8 / 02、原生情感、快速复刻、账号音色 | 采用北京 DashScope BaseURL，复刻录音使用公网 URL |
| MiniMax | 官方 Speech 2.8 / 2.6 / 02 / 01、HTTP / SSE / WebSocket、混合音色、录音复刻、声音设计 | 使用 MiniMax 官方 API Key，支持中国区与国际区地址 |
| 硅基流动 | CosyVoice、MOSS、账号音色、动态参考录音 | 预设音色使用 `模型ID:音色`，如 `FunAudioLLM/CosyVoice2-0.5B:anna` |
| 火山引擎 | 豆包语音、本地录音复刻、训练状态查询 | 选择鉴权方式、音色槽位与合成资源 |
| 小米MiMo | 预设音色、音频克隆、文字设计 | 按音色模式填写参考录音或声音描述 |
| GPT-SoVITS | API v2、参考录音与对应文字 | 填写同机录音路径或 GPT-SoVITS 服务端路径 |
| OpenAI兼容服务 | OpenAI Speech、自定义 JSON / 表单 / GET、音频与链接响应 | 每个模型单独选择请求协议与字段格式 |

每个平台均可配置多个模型和音色。同一平台也可添加多份服务商配置，分别设置账号、区域和用途。

## 快速开始
### 1 · 安装插件

插件市场搜索：`麦麦多平台TTS语音`，下载并安装

### 2 · 添加服务商

进入「服务商」，点击 **添加服务商**，选择平台，填写配置名称、API Key 和 **BaseURL**。平台配置名称使用连续字符，例如 `小米MiMo`、`阿里云_千问`；输入中的空白会自动移除，手动名称也支持 `·`。

默认BaseURL：

| 平台 | BaseURL |
| :--- | :--- |
| BreezeTTS 本地API | `http://127.0.0.1:7860/v1` |
| BreezeTTS 官方在线 | `https://api.breeze.blue/v1` |
| OpenAI / 兼容服务 | `https://api.openai.com/v1`，或服务商提供的 `https://example.com/v1` |
| 阿里云_千问 / 阿里云_CosyVoice / 阿里云_MiniMax | `https://dashscope.aliyuncs.com/api/v1` |
| 阿里云业务空间专属地址 | `https://<workspace_id>.cn-beijing.maas.aliyuncs.com/api/v1` |
| 硅基流动 | `https://api.siliconflow.cn/v1` |
| 火山引擎 | `https://openspeech.bytedance.com/api/v3` |
| MiMo | `https://api.xiaomimimo.com/v1` |
| MiniMax 官方中国区 | `https://api.minimax.cn/v1` |
| MiniMax 官方国际区 | `https://api.minimax.io/v1` |
| GPT-SoVITS-V2-API | `http://127.0.0.1:9880` |

### 3 · 选择模型与音色

点击 **获取模型列表并选择** 或 **添加 / 管理模型**，在弹窗中搜索并勾选常用模型。输入新的模型 ID 时，点击 **添加模型** 或按 **Enter**，即可添加并勾选该模型，再点击 **应用所选项**。每个模型旁提供 **模型设置** 和 **音色与克隆** 两个入口：前者调整参数，后者管理声音。配置完成后，在「语音路由」选择调用目标。

### 4 · 试听并保存

在音色窗口点击 **试听音色**，或前往「试听工作台」输入文字并选择模型合成。音色窗口中的 **保存音色**、**保存所选音色** 和模型窗口中的 **保存模型** 会直接保存到对应平台。平台启用开关旁的 **保存此平台** 用于连接配置，页面顶部的 **保存全部设置** 可同时保存各平台、路由和回复行为。

## 模型与音色

### BreezeTTS 接入

添加 **BreezeTTS**，在平台连接中选择 **本地API** 或 **Breeze 官方在线**，再填写对应的 BaseURL。

| 接入方式 | 声音配置 | 常用参数 |
| :--- | :--- | :--- |
| 本地API | 在「参考音色」中保存录音路径与准确的文字稿；「声音设计」使用文字描述创建声音 | CFG 指令强度、随机种子、默认声音来源、风格与语调 |
| 官方在线 | 同步公共与个人音色，或上传录音创建克隆、通过描述设计音色，保存后使用音色 ID | 模型、合成语言、语速、音量、说话人相似度、同步 / 流式 / 后台任务 |

#### 本地API · 从启动到第一次语音

**① 启动 Breeze 服务**

先按 [Breeze 开源项目说明](https://github.com/breezeblue-ai/breeze-tts#-streaming-api) 准备运行环境和模型权重。对于安装在 `C:\Apps\breeze-tts`、模型位于 `checkpoints\breeze-tts-2` 的服务，在 PowerShell 中执行：

```powershell
Set-Location C:\Apps\breeze-tts
& .\.venv\Scripts\python.exe -m breeze_infer.api .\checkpoints\breeze-tts-2 --host 127.0.0.1 --port 7860
```

保持此服务运行，再打开麦麦的语音工作台。使用其他 Python 环境时，将命令中的解释器替换为已安装 Breeze 依赖的 Python；模型路径填写实际权重目录。

**② 配置平台连接**

进入 **服务商 → 添加服务商 → BreezeTTS**，填写：

| 字段 | 同机示例 | 说明 |
| :--- | :--- | :--- |
| 配置名称 | `BreezeTTS_本地` | 用于整体路由和聊天命令 |
| Breeze 接入方式 | **本地API** | 适用于开源 Breeze 服务及采用相同协议的特化服务 |
| BaseURL | `http://127.0.0.1:7860/v1` | 插件自动生成 `/audio/speech` 请求路径 |
| API Key | 留空 | 默认开源服务使用匿名请求；启用鉴权的服务填写对应密钥 |

点击启用开关旁的 **保存此平台**。例如上述地址实际请求 `http://127.0.0.1:7860/v1/audio/speech`。部署在另一台机器时，将 `127.0.0.1` 换成 Breeze 机器的地址，同时为服务设置可访问的监听地址与端口。`127.0.0.1` 始终指 MaiBot 所在的运行环境；容器部署时请填写容器能访问的 Breeze 服务地址。

**③ 添加模型**

点击 **选择预设或手动添加模型**，选择 `breeze-tts-2`，再点击 **应用所选项**。打开该模型的 **模型设置**，第一次使用建议保留：

| 设置 | 建议值 |
| :--- | :--- |
| 输出格式 | `wav` |
| 语调模式 | **关闭语调控制** |
| CFG 指令强度 | `1` |
| 随机种子 | `42` |
| 模型默认声音来源 | **参考录音 · 原声复刻 / 语气引导** |

本地服务使用启动时加载的模型权重，工作台中的模型 ID 用于组织配置与路由。接入多个模型实例时，可在各模型的 **BaseURL** 中填写对应实例地址。点击 **保存模型** 保留设置。

**④ 保存参考音色**

打开该模型的 **音色与克隆 → 参考音色 → 新建音色**。填写音色名称（例如 `巫祝参考音色`），选择 **麦麦本地文件**，再填写参考音频路径与完整文字稿；收藏 ID 由工作台自动生成。

推荐将 MP3 / WAV 录音放入插件数据目录的 `reference_audio/` 文件夹。相对路径以插件数据目录为基准，例如：

```text
reference_audio/sample.wav
```

这对应 MaiBot 安装目录下的：

```text
data/plugins/WhiteCloudOL.maimai-tts-plugin/reference_audio/sample.wav
```

也可填写绝对路径，或通过路径旁的目录选择按钮选取录音。「本地文件」指 **运行 MaiBot 的机器上的文件**；插件读取后随请求发送给 Breeze，适用于同机服务和远程服务。音频来源也支持公开音频链接。

参考录音与文字稿可采用如下通用示例：

```text
你好，我是麦麦，很高兴和你聊天。
```

录音宜清晰、单人讲话、背景安静；文字稿覆盖录音中说出的全部内容，重复语句也按录音填写。点击 **保存音色**，每个收藏音色会分别保留参考录音、文字稿和语气设置。也可在 **模型设置 → 平台专属参数** 中填写模型默认参考录音，供沿用模型声音的路由使用。

**⑤ 试听并加入路由**

点击 **试听音色**，或进入 **试听工作台**，选择 `BreezeTTS_本地/breeze-tts-2` 和刚保存的音色，输入一句简短文字后合成。首次请求包含模型预热，等待时间通常较长。

听到声音后，进入 **语音路由**，将此 Provider/Model 设为首选并选择音色，点击顶部 **保存全部设置**。聊天中可用 `/mmtts model` 查看模型列表，使用 `/mmtts model BreezeTTS_本地/breeze-tts-2` 切换模型；音色以整体路由中保存的选择为准。

本地 API 返回的单声道 PCM 会自动封装为网页可播放的 WAV，聊天语音按发送设置转换为 SILK。同一 Breeze 实例的插件请求按顺序处理。

#### 本地API · 语气与声音设计

| 使用方式 | 配置方法 | CFG 建议 |
| :--- | :--- | :--- |
| 原声复刻 | 参考录音 + 完整文字稿，语调模式选择关闭，风格留空 | **1** |
| 固定语调 | 参考录音 + 文字稿，语调模式选择固定，选择温柔、开心等语气 | 从 **1** 开始，需要更强指令效果时选 **4** |
| 动态语调 | 参考录音 + 文字稿，语调模式选择动态，并设置允许语调列表 | 从 **1** 开始，确认风格或语调指令后可选 **4** |
| 声音设计 | 进入「声音设计」，填写声音描述，例如「年轻女性，声音清晰温柔，语速舒缓」 | **4** |

纯复刻与模型原生声音使用 **CFG=1**；风格或语调指令为更高 CFG 提供引导。固定语调直接作用于接口。动态语调由麦麦选择允许列表中的语气，插件校验并解析标签后调用合成；标签会从朗读正文中移除。语调可在模型统一设置，也可为某个音色单独保存。

| 遇到的情况 | 调整方法 |
| :--- | :--- |
| 连接服务失败 | 确认 Breeze 进程持续运行，核对 BaseURL、监听端口与网络可达性 |
| 提示参考录音或文字稿必填 | 在当前收藏音色中同时填写录音路径和完整文字稿，或补齐模型默认参考录音 |
| 提示纯复刻使用 CFG=1 | 将 CFG 调整为 `1`，或配置明确的风格与语调指令后使用更高值 |
| 声音与预期有差别 | 检查当前路由的模型和音色，核对录音文字稿，使用清晰的参考录音后再次试听 |

#### 官方在线 · 账号音色与鉴权

选择 **Breeze 官方在线**，BaseURL 填写 `https://api.breeze.blue/v1`，API Key 填写有效的 Breeze 开发者密钥。本地API 的 API Key 也支持按需填写；填写后统一发送：

```http
Authorization: Bearer 你的密钥
```

官方在线音色克隆使用至少 **3 秒**、最多 **5 MB** 的录音，由在线服务生成文字转写；声音设计填写 **3–500 字符**的描述与试听文本。选择音色语言后，创建结果自动保存为账号音色。在线模型包括 `breeze-tts-2` 与 `breeze-tts-2-multilingual`，也可从账号模型列表选择。

在线合成默认使用账号的 **1000 字符**上限；获得更高额度的账号可在模型参数中调整至 **2000**。固定语调直接作用于接口；动态语调沿用工作台的标签与允许列表设置。

官方说明：[开源服务](https://github.com/breezeblue-ai/breeze-tts#-streaming-api) · [在线接口](https://docs.breezeblue.ai/api-reference/introduction) · [音色与鉴权](https://docs.breezeblue.ai/authentication)

### 模型库

每个已配置模型旁提供 **移除模型** 按钮，确认后移除该模型及其音色收藏并保存。模型库可清空，后续通过 **添加 / 管理模型** 继续配置。整体路由正在使用的模型，可先在「语音路由」更换调用目标，再移除模型。

每个平台最多配置 **100 个模型**。模型选择弹窗的 **搜索或添加模型 ID** 同时用于查找和添加：输入完整 ID 后，点击出现的 **添加模型**，或按 **Enter** 添加并勾选。已有同名 ID 时，直接勾选对应条目即可。

弹窗还提供分页、多选和批量添加。展开 **手动批量添加**，每行填写一个 ID，也可添加显示名称：

```text
gpt-4o-mini-tts | 日常声音
tts-1-hd | 高质量朗读
```

模型列表中可查看音色数量，点击 **模型设置** 打开参数窗口，设置显示名称、模型 ID、BaseURL、输出格式、语速、风格和平台专属参数，点击 **保存模型** 即可保留。模型可沿用平台地址，也可使用独立地址。平台页负责连接与资源管理，整体路由负责首选与备选。

「从模型链接识别」支持硅基流动模型详情页、Hugging Face、ModelScope 和完整合成接口链接。模型详情页用于提取模型 ID；实际合成地址由服务提供方提供。

### OpenAI 与自定义接口

在 **OpenAI兼容服务 → 模型设置 → 请求协议** 中选择接口格式，每个模型独立保存。

| 协议 | 配置方式 |
| :--- | :--- |
| OpenAI 官方 Speech | 填写 BaseURL、模型与音色，使用官方参数格式 |
| 自定义 HTTP 格式 | 选择请求模板，调整路径、字段、鉴权和音频响应格式 |

**OpenAI 官方 Speech** 提供 13 个预设音色、音色 ID 对象、MP3 / WAV / Opus / AAC / FLAC / PCM、0.25–4 倍语速、模型风格与固定 / 动态语调。`tts-1`、`tts-1-hd` 使用各自的预设音色和音频流；`gpt-4o-mini-tts` 系列还可设置风格指令与 SSE 返回。高级参数中可填写 Organization、Project。具有相应权限的兼容服务可按 OpenAI 音色接口创建和删除账号音色，克隆时填写已有授权记录 ID。

**自定义 HTTP 格式** 提供 Speech JSON、Chat Audio JSON、文本字段 JSON、GET 参数与表单模板。BaseURL 例如 `https://example.com/v1`，合成路径例如 `audio/speech`，合成时使用 `https://example.com/v1/audio/speech`。

请求内容可按服务商格式填写，例如：

```json
{
  "model": "{{model}}",
  "text": "{{text}}",
  "speaker": "{{voice_id}}",
  "speed": "{{speed}}",
  "style": "{{instructions}}"
}
```

| 可用变量 | 内容 |
| :--- | :--- |
| `{{text}}`、`{{model}}` | 朗读正文、所选模型 ID |
| `{{voice}}`、`{{voice_id}}` | 音色值或音色 ID 对象、音色 ID 字符串 |
| `{{speed}}`、`{{response_format}}`、`{{sample_rate}}` | 语速、输出格式、PCM 采样率 |
| `{{instructions}}` | 合并后的风格与语调指令 |
| `{{api_key}}` | 平台中保存的 API Key |
| `{{reference_audio}}`、`{{reference_base64}}` | 参考录音的 Data URI、Base64 内容 |
| `{{reference_url}}`、`{{reference_text}}` | 参考录音链接、录音文字 |

单独占据字段值的变量会保留对应类型，例如 `"{{speed}}"` 会成为数字。JSON 可使用嵌套对象与数组；GET 和表单采用单层字段。鉴权可选择 Bearer、自定义请求头或开放接口；API Key 填在平台密钥框，请求模板与请求头通过 `{{api_key}}` 引用。风格与语调通过请求内容中的 `{{instructions}}` 传递。

| 响应类型 | 配置示例 |
| :--- | :--- |
| 二进制音频 | 直接接收音频流 |
| JSON Base64 / Hex / URL | 音频字段填写 `/data/audio` |
| Chat Audio JSON | 音频字段填写 `/choices/0/message/audio/data` |
| 纯文本 URL | 响应正文作为音频下载链接 |
| SSE Base64 | 分别填写音频事件、完成事件、事件类型字段和音频字段 |

响应字段使用 **JSON Pointer**：`/data/audio` 表示 `data` 中的 `audio`，`/choices/0` 表示 `choices` 中的第一项，留空表示整个响应。业务状态可配置字段与成功值，例如 `/code` 对应 `[0, 200]`；数字 `0` 和字符串 `"0"` 分别匹配。错误字段、模型列表和音色列表也可按服务商格式设置。接口能力与实际音色权限以对应服务商说明为准。

官方参考：[Speech 参数](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create) · [音色与合成指南](https://developers.openai.com/api/docs/guides/text-to-speech)

### 音色收藏

预设音色、账号目录与编辑窗口都会展示 **已收藏音色**，每项旁提供 **移除** 按钮。确认后移除本地收藏并保存，同时清理关联模型的音色选择。云端音色由独立的 **删除云端音色** 操作管理；路由正在使用的音色，可先在「语音路由」更换声音，再移除收藏。

在模型列表直接点击 **音色与克隆**。窗口按平台提供以下功能：

| 功能 | 操作 |
| :--- | :--- |
| 预设音色 | 按模型查看官方目录，搜索名称或 ID，分页勾选并保存 |
| 自定义音色 | 填写音色 ID、名称、风格与语速，或展开批量添加音色 ID |
| 账号音色 | 同步平台已有音色，勾选后保存到当前模型 |
| 音色克隆 | 准备参考录音，创建平台音色或保存动态参考音色 |
| 声音设计 | 在 BreezeTTS、千问、CosyVoice、MiniMax 或 MiMo 中描述声音，并保存为独立音色 |

自定义音色支持按行批量填写：

```text
Cherry | 清亮女声
Serena | 温柔女声
```

每个平台最多收藏 **100 个音色**。在同一个音色窗口选择已收藏的声音，即可编辑和试听，点击 **保存音色** 直接保留设置。OpenAI 的预设声音与自定义音色对象可以在同一模型下分别配置。整体路由中的音色选项决定实际发送时的声音。

点击预设列表旁的 **手动添加音色**，进入自定义音色编辑区，填写平台提供的完整音色 ID。账号复刻音色、自建服务的说话人 ID 和新发布音色均可自行收藏；输入示例随平台与模型切换。音色 ID 保留平台规定的大小写、标点和内部空格。

OpenAI 自定义音色可勾选 **使用 OpenAI 自定义音色对象（voice.id）**；兼容服务按其协议选择字符串或对象。GPT-SoVITS 使用收藏名称管理参考录音，MiMo 克隆与设计模型分别保存录音和声音描述。

### 预设目录

工作台内置以下官方目录，核对日期为 **2026-10-30**。音色名称与 ID 同时展示，模型之间分别管理。

| 平台 / 模型 | 目录内容 |
| :--- | :--- |
| OpenAI / 标准兼容模式 | gpt-4o-mini-tts 系列 13 个预设；tts-1 / tts-1-hd 各 9 个 |
| Qwen-Audio 3.1 | Plus 4 个，Flash 139 个 |
| Qwen-Audio 3.0 | Plus 599 个，Flash 609 个，包含官方基础音色表 |
| Qwen3-TTS Flash | HTTP 与 Realtime 各 48 个 |
| CosyVoice | v3 Flash 88 个，v3 Plus 2 个，v2 106 个；v3.5 使用复刻和设计音色 |
| MiniMax 官方 / 百炼 | 327 个系统音色，另可同步账号自定义音色 |
| 火山引擎 | 450 个豆包语音合成 2.0 音色，搭配对应资源使用 |
| 硅基流动 | CosyVoice2 的 8 个预设；MOSS 提供官方接口示例 alex，另可使用参考录音或账号音色 |
| MiMo | 9 个预设；克隆与设计模型使用各自的声音配置 |
| GPT-SoVITS / 自建服务 | 手动收藏服务端提供的说话人 ID 或参考录音 |

账号授权、服务地域和资源开通情况决定实际可调用范围。**账号音色 → 同步账号音色** 用于获取已有声音，**强制刷新** 可立即重新获取列表；克隆、更新录音和删除云端音色后，相关目录缓存自动更新。

官方音色参考：[OpenAI](https://developers.openai.com/api/docs/guides/text-to-speech) · [千问 Audio](https://docs.bailian.console.aliyun.com/zh/model-studio/qwen-audio-tts-voice-list) · [千问 Flash](https://docs.bailian.console.aliyun.com/zh/model-studio/qwen-tts-voice-list) · [CosyVoice](https://docs.bailian.console.aliyun.com/zh/model-studio/cosyvoice-voice-list) · [MiniMax](https://platform.minimax.cn/docs/faq/system-voice-id) · [火山引擎](https://docs.volcengine.com/docs/doubao-voice/tone-list-3?lang=zh) · [硅基流动](https://docs.siliconflow.cn/docs/userguide/capabilities/text-to-speech) · [MiMo](https://mimo.mi.com/docs/zh-CN/api/audio/tts)

### 克隆与声音设计

| 使用方式 | 操作步骤 |
| :--- | :--- |
| 千问克隆 | 选择 Qwen-Audio-TTS 模型 → 打开音色克隆 → 选择本地录音或公开 URL → 创建并保存；Qwen3-TTS 可切换到 VC 模型 |
| CosyVoice 复刻 | 打开音色克隆 → 选择本地录音或公开 URL，填写音色前缀 → 创建并保存克隆音色 |
| 阿里云声音设计 | 选择 Qwen-Audio 3.0/3.1 TTS 或 CosyVoice v3/v3.5 → 打开声音设计 → 填写声音描述、预览文本和音色前缀 → 设计并保存音色 |
| 百炼 MiniMax 复刻 | 填写自定义音色 ID、公开录音 URL 与克隆试听文本 → 创建并保存克隆音色 → 播放返回的预览 |
| MiniMax 官方复刻 | 选择本地 MP3 / M4A / WAV 或公开链接，填写音色 ID → 创建并保存；也可复用平台已上传文件 ID |
| MiniMax 官方设计 | 填写声音描述与试听文本 → 设计并保存；可指定音色 ID，或由平台生成 |
| 硅基流动复刻 | 打开音色克隆 → 选择创建账号音色或动态参考录音 → 填写录音及文字 → 保存 |
| OpenAI 克隆 | 开通自定义音色权限 → 填写已有授权记录 ID（consent ID）→ 选择本地录音或公开 URL → 创建并保存 |
| 火山引擎复刻 | 选择预付费槽位或后付费音色 → 填写目标音色 ID、录音语言与复刻资源 → 选择录音并提交 → 查询训练状态 |
| MiMo 音频克隆 | 打开音色克隆 → 配置参考录音 → 保存音色；克隆模型会一起加入平台 |
| MiMo 文字设计 | 打开声音设计 → 描述年龄、音色、口音与说话风格 → 试听并保存 |
| GPT-SoVITS | 配置参考录音、录音文字与语言；可收藏多段录音并在整体路由选择声音 |

阿里云 Qwen-Audio-TTS、CosyVoice 的克隆与设计音色均绑定创建时的目标模型、地区；音色前缀使用 1–10 位英文字母或数字。硅基流动的动态参考录音与账号音色分别保存。MiMo 设计模式可选择优化播报文本。

阿里云声音设计支持 1–500 字符的声音描述，以及 15–200 字符的预览文本，预览语言可选择中文或英文。创建完成后，工作台直接展示平台返回的 WAV 试听音频，声音描述和预览文字随每个音色分别保存。克隆与设计音色会显示「审核中 / 审核通过 / 审核失败」，点击 **查询音色状态** 更新结果，审核通过后即可用于合成。

已收藏的阿里云账号音色可查询状态、更新克隆录音或删除云端音色。列表中的 **×** 用于移出本地收藏；**删除云端音色** 会显示确认窗口，并同步处理平台账号与本地收藏。整体路由正在使用的音色可先切换路由再管理。

百炼 MiniMax 复刻录音使用公网 MP3 / M4A / WAV 链接，时长 10 秒至 5 分钟，文件最多 20 MB。自定义音色 ID 长度为 8–256 位，以英文字母开头、字母或数字结尾，中间可使用 `_` 和 `-`。克隆时可配置降噪、音量归一化和示例录音；创建请求会生成试听音频，按平台规则计算试听用量，首次正式使用复刻音色时计收音色费用。

### MiniMax 官方平台

在 **添加服务商 → MiniMax** 中填写官方 API Key 与 BaseURL，选择 `speech-2.8-hd`、`speech-2.8-turbo`、`speech-2.6-hd`、`speech-2.6-turbo`、`speech-02-hd`、`speech-02-turbo`、`speech-01-hd` 或 `speech-01-turbo`。

| 位置 | 可配置内容 |
| :--- | :--- |
| 模型设置 → 平台专属参数 | HTTP 完整音频、HTTP SSE、WebSocket；采样率、格式、声道、音量、音高、语言增强、发音词典、LaTeX 与 AIGC 水印 |
| 文本归一化模式 | 基础规则；Speech 2.8 / 2.6 的 HTTP 完整音频模式可选高质量处理 |
| 混合音色 | 添加最多 4 个音色 ID，各设置 1–100 的权重；列表清空后使用路由指定的声音 |
| 声音效果 | 调整音高、强度、音色与回响；完整音频支持 MP3 / WAV / FLAC，流式模式使用 MP3 |
| 音色克隆 | 本地文件、公开 URL、已上传文件 ID；示例录音、降噪、音量归一化、录音文字与匹配阈值 |
| 声音设计 | 声音描述、最多 500 字符的试听文本、自定义音色 ID 或平台生成 ID |
| 账号音色 | 同步系统、复刻和设计音色；收藏后可试听，复刻与设计音色可从对应窗口删除 |

官方平台支持 MP3、WAV、PCM、FLAC、Opus 与 G.711 μ-law。G.711 使用 8000 Hz 单声道，工作台接收后转换为 PCM WAV 播放。SSE 和 WebSocket 使用 Hex 音频返回方式；MP3 流式可启用恒定码率。模型与音色设置均可选择固定或动态情感，Speech 2.6 额外提供 **活泼** 与 **轻声**。

参考录音支持 MP3 / M4A / WAV，最多 20 MiB，复刻录音时长为 10 秒至 5 分钟，示例录音短于 8 秒。本地录音自动上传到 MiniMax，平台返回的文件 ID 保持完整精度。**已上传文件 ID** 可直接复用已有录音；示例录音框可填写本地路径、公开 URL 或已有示例文件 ID。

复刻试听文本按需填写，填写后会返回预览并计入试听用量。复刻与设计音色创建后，在 7 天内正式合成一次即可激活，并可从账号音色目录同步。首次正式使用音色时按平台规则计收音色费用。

官方说明：[同步合成](https://platform.minimax.cn/docs/api-reference/speech-t2a-http) · [录音复刻](https://platform.minimax.cn/docs/api-reference/voice-cloning-clone) · [声音设计](https://platform.minimax.cn/docs/api-reference/voice-design-design) · [账号音色](https://platform.minimax.cn/docs/api-reference/voice-management-get)

火山引擎的训练状态与音色 ID 一起保留；训练完成后可用于合成，使用“查询音色状态”更新结果。每个复刻音色单独记录合成资源 ID，默认采用 `seed-icl-2.0`。已有预付费槽位再次训练会更新对应音色；后付费音色首次调用合成（含工作台试听）按平台规则收取槽位费用。

OpenAI 的参考录音与授权记录需来自同一说话人，授权记录归属相同账号与项目。已开通自定义音色权限的账号可填写已有 `cons_…` 记录创建音色。

合成与音色创建按服务商规则计费，试听前可核对平台套餐和模型权限。

### 阿里云模型参数

| 模型 | 可配置内容 |
| :--- | :--- |
| Qwen-Audio 3.0/3.1 TTS | MP3 / WAV / PCM / Opus、采样率、音量、语速、音调、合成语言、随机种子、风格、SSML 与发音热修复 |
| Qwen3-TTS | WAV、预设 / VC 音色与语言；Instruct 系列可设置风格与指令优化，倍率保持 1 |
| CosyVoice | HTTP / WebSocket、音量、语速、音调；HTTP 可选 Opus、种子、语言提示与 SSML，v3/v3.5 支持发音热修复 |
| MiniMax | MP3 / WAV / PCM / FLAC、采样率、码率、声道、音量、音高、语言增强、发音词典、文本规范化、LaTeX 与 AIGC 水印 |

MiniMax 的声音效果可在 MP3 / WAV / FLAC 下调整音高、强度、音色和回响等音效。Qwen-Audio-TTS 与 CosyVoice 的录音语言在克隆表单设置，合成语言在模型参数中设置；支持的模型还可调整采用录音时长、预处理与音量归一化。

平台参考：[声音复刻](https://docs.bailian.console.aliyun.com/zh/model-studio/voice-clone-design-http-api) · [声音设计](https://docs.bailian.console.aliyun.com/zh/model-studio/voice-design-api-references) · [Qwen-Audio-TTS](https://docs.bailian.console.aliyun.com/zh/model-studio/qwen-audio-tts-http-api) · [Qwen3-TTS](https://docs.bailian.console.aliyun.com/zh/model-studio/qwen-tts-api) · [CosyVoice](https://docs.bailian.console.aliyun.com/zh/model-studio/cosyvoice-tts-http-api) · [MiniMax](https://docs.bailian.console.aliyun.com/zh/model-studio/minimax-synchronous-speech-synthesis-api)

## 语音语调

在 **模型设置 → 语音语调** 配置模型的表现，也可在 **音色与克隆** 中选中已收藏的声音，为此音色单独设置。音色设置优先于模型；选择「沿用模型设置」即可共用模型的语调。

| 模式 | 使用方式 |
| :--- | :--- |
| 关闭语调控制 | 使用平台原生表现和已有风格、语速参数 |
| 固定语调 | 选择开心、温柔、严肃等语调，插件直接应用到合成参数，正文保持原样 |
| 动态语调 | 勾选允许的语调，麦麦生成回复时根据上下文选择，插件校验后应用 |

### 平台与模型

| 平台 | 已适配的控制方式 |
| :--- | :--- |
| OpenAI / OpenAI兼容服务 | `gpt-4o-mini-tts` 系列使用 `instructions`；自定义 HTTP 可通过模板传递风格 |
| 阿里云_千问 | Qwen-Audio 3.0/3.1 TTS 使用 `instruction`；Qwen3 Instruct HTTP / Realtime 使用 `instructions`，可开启指令优化 |
| 阿里云_CosyVoice | 3.5 Flash / Plus 和 v3 Flash 的复刻、设计音色使用自然语言指令；龙安洋、龙安欢及 v3 Flash 龙呼呼采用专属情感格式 |
| 阿里云_MiniMax | 使用原生 `emotion`，平静对应 `calm`；Speech 02 额外支持轻声 `whisper` |
| 硅基流动 | `FunAudioLLM/CosyVoice2-0.5B` 使用官方指令前缀 |
| MiMo | V2.5 的预设、克隆与声音设计模型使用独立风格指令 |
| 火山引擎 | 豆包语音合成 2.0 使用 `context_texts`；`seed-icl-2.0` 复刻音色可在模型版本留空时使用语调 |

工作台按当前模型、音色和资源显示支持的选项。其他模型、GPT-SoVITS 与自定义 HTTP 接口可继续配置各自原生语速、参考录音等参数。

`qwen3-tts-flash` 采用原生音色朗读；需要固定或动态语调时，可选择 **Qwen-Audio 3.0/3.1 TTS** 或账号可用的 **Qwen3 Instruct**。CosyVoice 3.5 使用复刻、设计音色，先在「音色与克隆」创建或同步账号声音，再设置语调。CosyVoice v3 Plus 的龙安洋、龙安欢采用专属情感格式，其复刻音色使用原生朗读参数。页面会显示当前组合的能力说明，修改模型、音色或资源后同步更新语调选项。

### 动态语调

选择动态模式后，设置默认语调与允许列表。麦麦准备回复和调用语音工具时，会收到当前首选路由实际支持的标签规则；提示示例也从允许列表中选择，例如：

```text
[mmtts:happy]今天也很高兴见到你。
```

每条回复使用一个头部标签；`happy` 等 ID 精确匹配允许列表。插件依次校验标签格式、位置、数量、语调 ID 与平台能力，再转换为服务商参数。朗读、同时保留的文字及普通文字回复均使用处理后的正文。正文直接输入时采用配置的默认语调。动态请求会按各备选路由的允许列表逐一校验，匹配的路由执行合成。

概率语音会为带标签的当轮回复保留完整的一条消息。格式或语调校验触发错误时，自动回复以纯文本发送并记录 `WARNING`；试听、命令与工具调用会返回具体错误。配置选为固定模式时，插件直接应用所选语调，麦麦使用普通正文。

语音工具可先通过 `mmtts_tone_options` 查询当前模式与允许的语调，再按实际配置准备朗读正文。

「回复行为 → 动态语调提示语言」可选择中文、English 或日本語，三种提示使用同一组标签与校验规则。合成语调的实际表现由所选模型与音色决定，可用相同文字试听比较。

## 本地参考录音

| 平台 | 本地录音用途 | 提交方式 |
| :--- | :--- | :--- |
| BreezeTTS 本地API | 参考音色与语气引导 | 将录音文件和完整文字稿随请求发送给 Breeze |
| BreezeTTS 官方在线 | 创建账号克隆音色 | 上传录音文件，由在线服务转写并保存音色 |
| 阿里云_千问 | 创建克隆音色 | 读取文件，转换为平台要求的 Base64 音频 |
| 阿里云_CosyVoice | 创建复刻音色 | 读取文件，通过 `audio` 字段提交 Base64 Data URI |
| MiniMax 官方 | 创建克隆音色 | 上传文件后调用音色复刻，支持复用文件 ID |
| 硅基流动 | 创建账号音色、动态参考录音 | 读取文件并提交 Base64 音频 |
| MiMo | 每次合成使用参考录音克隆 | 读取文件并提交 Base64 音频 |
| OpenAI | 创建账号克隆音色 | 连同已有授权记录 ID 上传录音文件 |
| 火山引擎 | 注册或训练复刻音色 | 上传音频 Base64，保留音色 ID 与训练状态 |
| GPT-SoVITS | 使用参考录音合成 | 向同机服务传递绝对路径；异机服务使用服务端路径 |

录音目录位于：

```text
MaiBot/data/plugins/WhiteCloudOL.maimai-tts-plugin/reference_audio/
```

将 MP3/WAV 录音放在运行麦麦的设备上，选择「麦麦本地文件」。在音色窗口的 **参考音频文件** 中填写路径，或点击旁边的 **选择文件** 从录音目录中选取；选中后路径直接填入同一输入框。

| 路径形式 | 示例 | 路径基准 |
| :--- | :--- | :--- |
| 相对路径 | `reference_audio/sample.wav` | 插件数据目录 |
| Windows 绝对路径 | `C:\Audio\voice.wav` | 运行麦麦的 Windows 设备 |
| Linux 绝对路径 | `/srv/audio/voice.wav` | 运行麦麦的 Linux 设备 |

默认示例为 `reference_audio/sample.wav`，可将自己的录音放到这个位置。点击 **从服务器路径导入** 可将已有录音复制到录音目录，并自动使用生成的相对路径，原文件继续保留。

### 工作台导入步骤

1. 将录音复制或上传到 **运行 MaiBot 的设备或容器**，下面按部署方式给出示例。
2. 打开 **服务商 → 对应模型 → 音色与克隆**，选择音色克隆或参考音色，新增或选中一个音色。
3. 将 **参考音频来源** 设为 **麦麦本地文件**。文件已在录音目录时，点击 **选择文件** 选取；文件在其他可访问目录时，点击 **从服务器路径导入**，填写该文件的绝对路径并确认。导入完成后，工作台自动回填录音库中的相对路径。
4. 按平台填写参考录音文字、语言、授权记录或音色槽位等参数，点击 **保存音色** 或 **创建并保存克隆音色**。
5. 平台音色创建完成后试听，并在 **语音路由** 中选用该模型和音色。

工作台提供服务器路径导入与录音目录选择；浏览器所在设备与 MaiBot 分开部署时，可先通过远程桌面、SFTP 或 `scp` 将录音传到 MaiBot 设备，再执行以上步骤。页面显示的 **录音目录** 是当前部署环境的准确位置。

### Windows 部署

以 MaiBot 安装在 `C:\Apps\MaiBot`、原录音位于 `C:\Audio\sample.wav` 为例，在 PowerShell 中复制到录音目录：

```powershell
New-Item -ItemType Directory -Force -Path C:\Apps\MaiBot\data\plugins\WhiteCloudOL.maimai-tts-plugin\reference_audio
Copy-Item -LiteralPath C:\Audio\sample.wav -Destination C:\Apps\MaiBot\data\plugins\WhiteCloudOL.maimai-tts-plugin\reference_audio\sample.wav
```

然后在工作台点击 **选择文件** 选取 `sample.wav`，或直接填写 `reference_audio/sample.wav`。

也可在 **从服务器路径导入** 中填写 `C:\Audio\sample.wav`；插件会将文件复制到录音库，并生成带唯一后缀的文件名。Windows 绝对路径支持 `C:\Audio\sample.wav` 和 `C:/Audio/sample.wav` 两种写法。

### Linux 部署

以 MaiBot 安装在 `/opt/MaiBot`、录音位于 `/srv/audio/sample.wav` 为例：

```bash
mkdir -p /opt/MaiBot/data/plugins/WhiteCloudOL.maimai-tts-plugin/reference_audio
cp /srv/audio/sample.wav /opt/MaiBot/data/plugins/WhiteCloudOL.maimai-tts-plugin/reference_audio/sample.wav
```

在工作台填写 `reference_audio/sample.wav`，或点击 **选择文件** 选取。也可通过 **从服务器路径导入** 填写 `/srv/audio/sample.wav`。运行 MaiBot 的账户需要录音文件的读取权限、父目录的访问权限，以及插件录音目录的写入权限。

从另一台电脑上传时，可在录音所在电脑执行以下示例，将账号、主机名和目录替换为实际值：

```bash
scp ./sample.wav bot@example.com:/opt/MaiBot/data/plugins/WhiteCloudOL.maimai-tts-plugin/reference_audio/sample.wav
```

### Docker 部署

Docker 场景中，工作台的绝对路径以 **MaiBot 容器内的文件系统** 为准，相对路径仍以容器内的插件数据目录为基准。先查看工作台显示的录音目录，再对照 Compose 的数据挂载位置。

以容器名 `maim-bot-core`、MaiBot 目录 `/MaiMBot`，并采用以下插件数据挂载为例：

```yaml
volumes:
  - ./data/MaiMBot-plugin-data:/MaiMBot/data/plugins
```

宿主机文件与容器内文件的对应关系为：

| 位置 | 示例路径 |
| :--- | :--- |
| 宿主机，基于 Compose 所在目录 | `./data/MaiMBot-plugin-data/WhiteCloudOL.maimai-tts-plugin/reference_audio/sample.wav` |
| MaiBot 容器内 | `/MaiMBot/data/plugins/WhiteCloudOL.maimai-tts-plugin/reference_audio/sample.wav` |
| 工作台相对路径 | `reference_audio/sample.wav` |

将录音放入上表的宿主机目录后，工作台即可按相对路径读取。若采用整个 `data` 目录的挂载，例如 `./data:/MaiMBot/data`，宿主机录音位置对应为 `./data/plugins/WhiteCloudOL.maimai-tts-plugin/reference_audio/sample.wav`。数据挂载可让录音随容器重建持续保留。

也可从宿主机通过 `docker cp` 导入录音。在录音所在目录执行：

```bash
docker exec maim-bot-core mkdir -p /MaiMBot/data/plugins/WhiteCloudOL.maimai-tts-plugin/reference_audio
docker cp ./sample.wav maim-bot-core:/MaiMBot/data/plugins/WhiteCloudOL.maimai-tts-plugin/reference_audio/sample.wav
```

Windows 宿主机可将命令中的源文件写成 `C:\Audio\sample.wav`。工作台始终填写容器内路径或 `reference_audio/sample.wav`。通过 **从服务器路径导入** 导入其他位置的文件时，源文件也应位于容器可访问的目录，例如挂载到 `/reference_source` 后填写 `/reference_source/sample.wav`。

容器名、MaiBot 目录和挂载位置按实际部署填写；容器运行账户需要录音读取与录音库写入权限。GPT-SoVITS 分容器部署时，可将同一份录音目录挂载到两个服务，并在 **GPT-SoVITS 服务端路径** 中填写其容器内可访问的路径。

### 平台录音要求

常用参考录音格式为 **MP3 / WAV**。通用本地录音上限为 **7 MiB**，硅基流动最多 **4 MiB**；Breeze 官方在线克隆采用至少 **3 秒**、最多 **5 MB** 的录音，MiniMax 官方克隆支持 **MP3 / M4A / WAV**、最多 **20 MiB**。建议使用清晰、单人、背景安静的短录音，并按平台要求准备准确文字与合适时长。

阿里云_MiniMax 的录音来源采用 **公开音频 URL**。可先将录音上传到提供直链的存储服务，再将可读取的 MP3 / M4A / WAV 链接填写到音色克隆窗口；录音时长为 **10 秒至 5 分钟**，文件最多 **20 MB**。

GPT-SoVITS 与麦麦共用文件系统时，选择「麦麦本地文件」；异机部署时，选择「GPT-SoVITS 服务端路径」，填写该服务所在设备可访问的录音路径。

## 整体语音路由

平台页负责连接、启停和模型、音色资源管理；首选与备选统一在「语音路由」中选择。

每条路由以 **`Provider/Model`** 展示，例如：

```text
硅基流动/FunAudioLLM/CosyVoice2-0.5B
小米MiMo/mimo-v2.5-tts
```

点击 **选择服务商 / 模型**，在弹窗搜索目标并选用，再选择这条路由使用的音色。备选路由可以逐条添加、上移和下移；同一服务商的多个模型也可分别加入备选。

```text
首选 Provider/Model → 第一备选 Provider/Model → 第二备选 Provider/Model
```

当前目标请求失败时，系统记录原因并按顺序尝试下一条。每条路由使用选定的模型、音色与对应服务商的密钥。启用开关可控制服务商是否参与路由。

### 在聊天中切换模型

```text
/mmtts model
/mmtts model 2
/mmtts model 硅基流动/FunAudioLLM/CosyVoice2-0.5B
```

- **查看列表**：`/mmtts model` 显示已启用平台中的模型、编号及当前首选。
- **列表翻页**：模型较多时使用 `/mmtts model list 2` 查看第 2 页，各页沿用同一组模型编号。
- **按编号切换**：`/mmtts model 2` 选用列表中的第 2 项。
- **按名称切换**：`/mmtts model <Provider>/<Model>` 使用完整服务商名称与模型 ID；服务商 ID 也可用于匹配。

列表与切换命令由 MaiBot **操作者**执行。切换作用于整体首选路由并保存到工作台配置，已有备选保持顺序；选中的目标已在备选中时，会移到首选位置。工作台在编辑空闲时同步最新配置。

## 聊天用法

### 用命令发送语音

```text
/mmtts 你好呀，我是麦麦。
/tts 你好呀，我是麦麦。
/语音 今天也要好好休息。
@麦麦 /mmtts 今天也要好好休息。
```

在「回复行为」启用聊天命令后，可在群聊或私聊中使用。主命令为 `/mmtts <文本>`，同时支持 `/tts` 与 `/语音`。命令前可带 `@麦麦` 和空白，插件直接合成命令后的正文并发送到当前聊天；模型管理使用 `/mmtts model`。命令缺少正文、命令开关关闭或合成发送失败时，会在聊天中显示对应提示。

### 让麦麦主动使用语音

开启 **LLM 语音工具** 后，麦麦可在合适的场景调用语音工具。工具名称为 `mmtts_speak`，可配置临时音色、临时语速，以及同时保留文字。

### 按概率用声音回复

开启 **概率语音**，设置转换概率、字符范围及群聊/私聊开关。例如 `0.2` 表示约 20% 的回复使用语音。系统按整轮回复抽取一次，随后各文字分段沿用相同判定；图片、表情和 At 回复继续采用原有形式。

### QQ 官方机器人、SnowLuma 与 NapCat

「回复行为 → 聊天语音格式」默认选择 **QQ 原生语音（SILK）**，发送前会将服务商音频转换为 QQ 语音格式。转换能力随插件依赖安装，工作台使用可播放的音频格式试听。

| 适配器 | 使用方式 |
| :--- | :--- |
| QQ 官方机器人 | 搭配支持出站语音的官方适配器，本地版本 0.4.4 已提供群聊与单聊语音发送 |
| SnowLuma | 经统一 QQ 适配器发送原生语音，账号和群聊/私聊目标沿用现有聊天流 |
| NapCat | 可使用相同的 QQ 原生语音格式 |
| 其他聊天平台 | 选择「保留服务商原始格式」，搭配目标平台支持的输出格式 |

单条聊天语音转码上限为 **600 秒**。QQ 官方消息按平台的回复窗口与次数规则受理。发送结果以适配器回执为准；出现“投递待确认”时，可先核对聊天中的实际消息。

## 配置说明

### 工作台设置

| 页面 | 可配置内容 |
| :--- | :--- |
| 服务商 | 平台启停、API Key、BaseURL、多模型、音色与克隆参数 |
| 语音路由 | 首选 Provider/Model、备选顺序与各路由音色 |
| 回复行为 | 聊天语音格式、命令、LLM 工具、概率语音与文字保留 |
| 试听工作台 | 选择平台、模型与文字，直接播放合成结果 |

API Key 保存在服务端。已有密钥会显示保存状态；输入框留空时沿用已保存值。点击输入框右侧的 **清空密钥**，再保存即可移除；保存前可点击 **撤销清空** 恢复沿用已有密钥。调整服务请求地址后，填写新地址对应的密钥即可继续使用。

### 运行设置

`config.toml` 保留插件总开关与运行限制，可通过 MaiBot 的插件配置页面调整：

| 配置项 | 默认值 | 用途 |
| :--- | :--- | :--- |
| `plugin.enabled` | `true` | 插件总开关 |
| `runtime.log_level` | `INFO` | 插件日志最低级别，支持 DEBUG / INFO / WARNING / ERROR |
| `runtime.timeout_seconds` | `90` | 单个平台请求超时，单位秒 |
| `runtime.max_text_length` | `4096` | 单次合成的字符上限 |
| `runtime.max_audio_size_mb` | `20` | 单次音频大小上限，单位 MiB |
| `runtime.max_concurrency` | `2` | 同时执行的任务数量 |
| `runtime.list_cache_ttl_seconds` | `300` | 模型与账号音色列表缓存时长，单位秒；设为 0 可关闭 |
| `runtime.audio_cache_ttl_seconds` | `300` | 合成与转码音频缓存时长，单位秒；设为 0 可关闭 |
| `runtime.cache_max_size_mb` | `64` | 共享内存缓存容量，单位 MiB；设为 0 可关闭 |
| `runtime.proxy_url` | 留空 | 指定 HTTP 代理 |
| `runtime.use_system_proxy` | `false` | 使用系统代理环境设置 |

列表按平台接口、账号与模型归属复用，音频按实际正文、模型、音色、语调及合成参数复用。并发相同请求会合并处理，容量达到上限时优先保留最近使用的结果；修改显示名称、收藏和路由后，相同语音仍可命中缓存。参考文件与外部权重参与合成时逐次读取来源，重载插件后重新建立内存缓存。

### 日志与排查

在 MaiBot 日志页面搜索 **`[TTS]`**，可按 `trace` 编号串联合成、转码与发送过程。

| 级别 | 记录内容 |
| :--- | :--- |
| `DEBUG` | 整轮概率判定、跳过原因、并发等待、HTTP 状态与耗时、缓存命中与请求合并 |
| `INFO` | 启动状态、命令与工具触发、路由尝试、合成、转码、发送和设置保存 |
| `WARNING` | 服务商失败、切换备选、命令参数提示、自动语音转为文字 |
| `ERROR` | 合成链最终失败、转码失败、发送失败和内部异常位置 |

日志保留平台、模型、字符数、音频大小和耗时等诊断信息，凭据、签名地址与朗读原文会作隐藏处理。自然语言语音请求由麦麦回复调度与工具选择处理；`/tts 你好` 可直接检查语音链路。排查自动语音时，将 `runtime.log_level` 调为 `DEBUG`，同时查看「回复行为」中的概率语音开关、聊天范围与文本长度。

## 数据与备份

```text
MaiBot/data/plugins/WhiteCloudOL.maimai-tts-plugin/
├── studio.json       平台、模型、音色、路由与回复设置
└── reference_audio/  参考录音
```

实际路径以工作台显示的目录为准。备份时可保存整个数据目录，`studio.json` 按含密钥的私密配置妥善保管。

从原 `data/plugins/maimai-tts-plugin/` 目录升级时，先备份并停止 MaiBot，将其中的配置与参考录音复制到 `data/plugins/WhiteCloudOL.maimai-tts-plugin/`，再启动 MaiBot。工作台随后读取新插件 ID 对应的数据目录。

## 常见情况

| 遇到的情况 | 建议操作 |
| :--- | :--- |
| 模型列表中条目较多 | 在弹窗搜索关键词、勾选所需模型，再应用到模型库 |
| 模型或音色权限提示 | 核对账号、地区、模型 ID、音色 ID 与资源 ID |
| 本地录音路径提示 | 对照工作台显示的数据目录，检查文件位置和读取权限 |
| GPT-SoVITS 录音访问提示 | 按同机或异机场景选择路径来源，核对服务进程可访问的目录 |
| 硅基流动采样率提示 | MP3 选 32000/44100，Opus 选 48000，WAV/PCM 按平台支持的采样率选择 |
| 多页面保存冲突 | 保留当前编辑内容，刷新服务端设置后重新应用并保存 |
| 平台合成失败 | 在试听页指定平台与模型，按错误提示核对接口、密钥、权限和代理 |
| 自然语言请求后等待回复 | 查看主程序回复调度判定；使用 `/tts 你好` 检查合成与发送，结合 `[TTS]` 日志定位环节 |

---

<div align="center">

作者：[清蒸云鸭](https://github.com/WhiteCloudOL) · [AGPL-3.0](LICENSE)

</div>
