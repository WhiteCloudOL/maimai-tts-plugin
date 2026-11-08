"""公开 API 的版本、请求结构和业务错误。"""

from pydantic import Field, model_validator
from typing import Any, Dict, List, Literal, Optional

from ..domain.models import Model, TTSOperationError


API_VERSION = "1"


class APIError(TTSOperationError):
    """携带稳定错误码的插件 API 业务异常。

    Attributes:
        code: 调用方用于分支处理的错误码。
    """


class Control(Model):
    """单次语音的表达策略；自动模式使用偏好，手动模式使用显式控制。"""

    mode: Literal["auto", "manual", "off"] = "manual"
    tone: Optional[str] = None
    style: str = Field(default="", max_length=200)
    style_hint: str = Field(default="", max_length=500)
    events: List[Dict[str, str]] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_control(self) -> "Control":
        if self.tone and self.style:
            raise ValueError("tone 与 style 请选择一项")
        if self.mode != "manual" and (self.tone is not None or self.style or self.events):
            raise ValueError("显式 tone、style、events 使用 manual 模式")
        if self.mode != "auto" and self.style_hint:
            raise ValueError("style_hint 使用 auto 模式")
        return self


class APIRequest(Model):
    """简单入口与高级入口共用的规范化合成请求。

    选择器解析已保存的服务商与模型；parameters 在实际音色解析后覆盖该次合成。
    request_id 关联任务和发送去重记录，output 决定返回音频或投递聊天流。
    """

    text: str = Field(min_length=1, max_length=4096, description="完整朗读正文")
    provider_id: str = Field(default="", max_length=64, description="目录中的服务商 ID；省略时采用整体路由")
    model_id: str = Field(default="", max_length=200, description="所选服务商模型库中的模型 ID")
    voice_id: str = Field(default="", max_length=500, description="所选模型的音色 ID 或已收藏名称")
    control: Control = Field(default_factory=Control)
    parameters: Dict[str, Any] = Field(default_factory=dict, max_length=64)
    output: Literal["audio", "send"] = "audio"
    stream_id: str = Field(default="", max_length=200)
    keep_text: bool = False
    delivery_format: Literal["default", "source", "silk"] = "default"
    refresh_cache: bool = False
    request_id: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def validate_request(self) -> "APIRequest":
        if not self.text.strip():
            raise ValueError("text 请填写朗读正文")
        if self.output == "send" and not self.stream_id.strip():
            raise ValueError("发送语音请填写真实 stream_id")
        if self.model_id and not self.provider_id:
            raise ValueError("指定 model_id 时同时填写 provider_id")
        return self


class AudioSend(Model):
    """已有音频结果的投递参数，供重复播报与重试发送共用。"""

    audio_id: str = Field(min_length=1, max_length=100)
    stream_id: str = Field(min_length=1, max_length=200)
    keep_text: bool = False
    request_id: str = Field(default="", max_length=128)
