"""config.toml 只保留运行边界，平台与功能设置由工作台维护。"""

from maibot_sdk import Field, PluginConfigBase
from typing import Literal


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    enabled: bool = Field(default=True, description="启用麦麦TTS语音", json_schema_extra={"label": "启用插件"})
    config_version: str = Field(
        default="2.0.2", description="配置版本", json_schema_extra={"label": "配置版本", "readonly": True}
    )


class RuntimeSection(PluginConfigBase):
    __ui_label__ = "运行限制"
    __ui_icon__ = "settings"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="插件日志最低级别；DEBUG 包含跳过原因、请求状态和耗时，INFO 记录主要操作",
        json_schema_extra={"label": "日志级别"},
    )
    timeout_seconds: float = Field(
        default=90,
        ge=1,
        le=600,
        description="每个服务商单次请求超时（秒）",
        json_schema_extra={"label": "平台请求超时（秒）"},
    )
    max_text_length: int = Field(
        default=4096, ge=1, le=4096, description="单次合成字符上限", json_schema_extra={"label": "文本字符上限"}
    )
    max_audio_size_mb: int = Field(
        default=20,
        ge=1,
        le=100,
        description="单次音频大小上限（MiB）",
        json_schema_extra={"label": "音频大小上限（MiB）"},
    )
    max_concurrency: int = Field(
        default=2, ge=1, le=8, description="同时执行的合成或平台任务数", json_schema_extra={"label": "最大并发任务"}
    )
    list_cache_ttl_seconds: int = Field(
        default=300,
        ge=0,
        le=86400,
        description="模型与账号音色列表缓存时长，0 表示每次请求平台",
        json_schema_extra={"label": "列表缓存时长（秒）"},
    )
    audio_cache_ttl_seconds: int = Field(
        default=300,
        ge=0,
        le=86400,
        description="相同合成参数与文本的音频缓存时长，0 表示每次重新合成",
        json_schema_extra={"label": "音频缓存时长（秒）"},
    )
    cache_max_size_mb: int = Field(
        default=64,
        ge=0,
        le=512,
        description="列表、合成音频与转码共用的内存缓存上限，0 表示关闭结果缓存",
        json_schema_extra={"label": "缓存内存上限（MiB）"},
    )
    proxy_url: str = Field(default="", description="可选 HTTP 代理 URL", json_schema_extra={"label": "HTTP 代理 URL"})
    use_system_proxy: bool = Field(
        default=False, description="读取系统代理环境变量", json_schema_extra={"label": "使用系统代理"}
    )


class TTSConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
