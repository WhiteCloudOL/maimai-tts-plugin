"""OpenAI TTS 插件核心模块。"""

from .client import (
    OpenAITTSClient,
    SynthesizedAudio,
    TTSAPIError,
    TTSClientError,
    TTSClientSettings,
    TTSConfigurationError,
    TTSNetworkError,
    TTSTimeoutError,
)

__all__ = [
    "OpenAITTSClient",
    "SynthesizedAudio",
    "TTSAPIError",
    "TTSClientError",
    "TTSClientSettings",
    "TTSConfigurationError",
    "TTSNetworkError",
    "TTSTimeoutError",
]
