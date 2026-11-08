# 服务商协议回归

此目录纳入版本控制。测试使用真实 `Transport` 连接临时启动的
`127.0.0.1` HTTP/WebSocket 服务，检查请求字段、握手顺序、响应聚合和异常处理。
不读取宿主配置、不使用真实密钥、不访问外部 TTS 服务。

## 运行

在插件仓库根目录安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，运行：

```sh
uv run --isolated --no-project --python 3.12 --with-requirements requirements-test.txt --with maibot-plugin-sdk==2.21.0 python -m pytest tests/contracts -q --tb=short -W error::ResourceWarning -p no:cacheprovider -o asyncio_default_fixture_loop_scope=function
```

将 SDK 版本改为 `2.13.0` 可运行相同的最低兼容版本回归。
GitHub Actions 对 Windows、Linux 和两个 SDK 端点版本执行同一命令。
测试依赖只用于开发，不加入插件运行时依赖。

## 覆盖

- MiniMax 官方 HTTP：8 个声明模型的完整请求，包含必填 `model`。
- MiniMax 官方 SSE：独立完整请求、最终聚合音频不重复拼接、完整结束状态。
- MiniMax 官方 WebSocket：`connected_success` → `task_start` → `task_started`
  → `task_continue` / `task_finish` → 音频 → `task_finished`；支持具名音频事件
  和官方示例中不带 `event` 的音频帧。
- 三种传输的业务错误、HTTP 错误、非法音频、超时、取消与连接关闭；
  SSE 截断、WebSocket 乱序、重复事件与不完整音频。
- 百炼 MiniMax 外层 `model` 与内层 `input` 的独立包装，防止原生修复影响百炼。
- Breeze 后台任务包含 `streaming` 的正常进度继续轮询，失败或未知状态明确报错；
  音色语言与关键词在翻页时保持一致，筛选条件隔离目录缓存、保持合成缓存复用。
- 空路由或工具参数继承模型音色，临时覆盖保留模型默认值；缓存按实际音色区分，
  云端音色删除保护使用继承后的真实音色。
- SILK 使用真实编码器和解码器核对尾音排空、重采样与分包时长，转码缓存区分编码版本，
  覆盖 WAV、MP3、Opus、FLAC、AAC 输入；重复准备发送不会再次追加静音，源音频发送保持原样。
- 整数秒输出核对实际解码时长与原有声音一致性；开关隔离转码缓存、复用上游源音频，
  配置快照、源格式发送与工作台试听分别按所选设置处理。
- 工具和语调自适应经过本地 HTTP 端点核对全部平台的最终请求字段，保留文字清理控制标记；
  JSON 字段、格式示例、重复字段拒绝及重试前缀复用分别回归。
- 公开插件 API 使用 SDK 和 MessagePack 往返，覆盖简单/高级调用的音频返回与投递、全平台参数和语调、
  开关、幂等、缓存、分块读取、任务取消、日志脱敏与生命周期清理。

请求预期独立于被测 payload helper，测试音频为内存字节样本。
这些测试核对协议与资源清理，不代表真实服务商账号、宿主语音投递或音质验收。

## 协议来源

- [MiniMax HTTP / SSE](https://platform.minimax.io/docs/api-reference/speech-t2a-http)
- [MiniMax WebSocket](https://platform.minimax.io/docs/api-reference/speech-t2a-websocket)
- [百炼 MiniMax](https://help.aliyun.com/zh/model-studio/minimax-synchronous-speech-synthesis-api)

本地实验测试继续保持忽略；可在此目录新增不依赖密钥、账号和付费调用的协议回归。
