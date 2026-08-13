"""OpenAI 兼容文本转语音 HTTP 客户端。"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Union
from urllib.parse import urlsplit

import json

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout


SUPPORTED_RESPONSE_FORMATS: Set[str] = {"aac", "flac", "mp3", "opus", "pcm", "wav"}
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_ERROR_MESSAGE_LENGTH = 1000


class TTSClientError(RuntimeError):
    """表示 TTS 请求无法完成。"""


class TTSConfigurationError(TTSClientError):
    """表示插件配置无效或不完整。"""


class TTSAPIError(TTSClientError):
    """表示上游 TTS 接口返回错误。"""

    def __init__(self, status: int, message: str, request_id: str = "") -> None:
        request_context = f"，request_id={request_id}" if request_id else ""
        super().__init__(f"TTS 接口请求失败（HTTP {status}{request_context}）：{message}")
        self.status = status
        self.request_id = request_id


class TTSTimeoutError(TTSClientError):
    """表示 TTS 请求超过配置的超时时间。"""


class TTSNetworkError(TTSClientError):
    """表示 TTS 请求发生网络层错误。"""


@dataclass(frozen=True)
class TTSClientSettings:
    """OpenAI TTS 客户端配置。"""

    base_url: str
    endpoint_path: str
    api_key: str
    model: str
    voice: str
    custom_voice_id: str
    response_format: str
    speed: float
    instructions: str
    timeout_seconds: float
    max_text_length: int
    max_audio_size_mb: int
    organization: str
    project: str
    proxy_url: str
    use_system_proxy: bool


@dataclass(frozen=True)
class SynthesizedAudio:
    """一次成功合成的音频。"""

    data: bytes
    content_type: str
    response_format: str
    request_id: str


class OpenAITTSClient:
    """调用 OpenAI 兼容的 ``/audio/speech`` 端点。"""

    def __init__(self, settings: TTSClientSettings) -> None:
        self._settings = settings
        self._session: Optional[ClientSession] = None
        self._validate_settings()

    async def close(self) -> None:
        """关闭复用的 HTTP 会话。"""

        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def build_payload(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
    ) -> Dict[str, Any]:
        """构造并校验 OpenAI TTS 请求体。"""

        if not isinstance(text, str):
            raise TTSConfigurationError("待合成文本必须是字符串")
        if voice is not None and not isinstance(voice, str):
            raise TTSConfigurationError("临时音色必须是字符串")
        if speed is not None and (isinstance(speed, bool) or not isinstance(speed, (int, float))):
            raise TTSConfigurationError("临时语速必须是数字")

        normalized_text = text.strip()
        if not normalized_text:
            raise TTSConfigurationError("待合成文本不能为空")
        if len(normalized_text) > self._settings.max_text_length:
            raise TTSConfigurationError(
                f"待合成文本长度为 {len(normalized_text)}，超过上限 {self._settings.max_text_length}"
            )

        selected_voice: Union[str, Dict[str, str]]
        if voice is not None:
            selected_voice = voice.strip()
            if not selected_voice:
                raise TTSConfigurationError("临时音色不能为空")
        elif self._settings.custom_voice_id.strip():
            selected_voice = {"id": self._settings.custom_voice_id.strip()}
        else:
            selected_voice = self._settings.voice.strip()

        selected_speed = speed if speed is not None else self._settings.speed
        if not 0.25 <= selected_speed <= 4.0:
            raise TTSConfigurationError("语速必须在 0.25 到 4.0 之间")

        payload: Dict[str, Any] = {
            "input": normalized_text,
            "model": self._settings.model.strip(),
            "response_format": self._settings.response_format.strip().lower(),
            "speed": selected_speed,
            "voice": selected_voice,
        }
        instructions = self._settings.instructions.strip()
        if instructions:
            payload["instructions"] = instructions
        return payload

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
    ) -> SynthesizedAudio:
        """合成语音并返回完整音频字节。"""

        api_key = self._settings.api_key.strip()
        if not api_key:
            raise TTSConfigurationError("尚未配置 API Key")

        session = await self._get_session()
        payload = self.build_payload(text, voice=voice, speed=speed)
        proxy_url = self._settings.proxy_url.strip()

        try:
            async with session.post(
                self._endpoint_url,
                allow_redirects=False,
                json=payload,
                proxy=proxy_url if proxy_url else None,
            ) as response:
                if response.status >= 300:
                    await self._raise_api_error(response)
                self._validate_audio_content_type(response)
                audio_data = await self._read_audio(response)
                content_type = response.headers.get("Content-Type", "application/octet-stream")
        except TTSClientError:
            raise
        except TimeoutError as exc:
            raise TTSTimeoutError(f"TTS 请求超过 {self._settings.timeout_seconds:g} 秒，已停止等待") from exc
        except ClientError as exc:
            raise TTSNetworkError(f"TTS 网络请求异常：{exc}") from exc

        return SynthesizedAudio(
            data=audio_data,
            content_type=content_type.split(";", maxsplit=1)[0].strip(),
            response_format=self._settings.response_format.strip().lower(),
            request_id=response.headers.get("x-request-id", "").strip(),
        )

    @property
    def _endpoint_url(self) -> str:
        return f"{self._settings.base_url.rstrip('/')}/{self._settings.endpoint_path.lstrip('/')}"

    async def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "Authorization": f"Bearer {self._settings.api_key.strip()}",
                "User-Agent": "maimai-openai-tts-plugin/1.0.0",
            }
            organization = self._settings.organization.strip()
            if organization:
                headers["OpenAI-Organization"] = organization
            project = self._settings.project.strip()
            if project:
                headers["OpenAI-Project"] = project

            timeout = ClientTimeout(total=self._settings.timeout_seconds)
            self._session = ClientSession(
                headers=headers,
                timeout=timeout,
                trust_env=self._settings.use_system_proxy,
            )
        return self._session

    async def _read_audio(self, response: ClientResponse) -> bytes:
        max_audio_bytes = self._settings.max_audio_size_mb * 1024 * 1024
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise self._response_error(response, "响应的 Content-Length 不是有效整数") from exc
            if declared_length < 0:
                raise self._response_error(response, "响应的 Content-Length 不能为负数")
            if declared_length > max_audio_bytes:
                raise self._response_error(response, "音频响应超过配置的大小上限")

        chunks = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            chunks.extend(chunk)
            if len(chunks) > max_audio_bytes:
                raise self._response_error(response, "音频响应超过配置的大小上限")
        if not chunks:
            raise self._response_error(response, "接口返回了空音频")
        return bytes(chunks)

    @staticmethod
    async def _raise_api_error(response: ClientResponse) -> None:
        response_body = await response.content.read(MAX_ERROR_BODY_BYTES)
        response_encoding = response.charset or "utf-8"
        try:
            response_text = response_body.decode(response_encoding, errors="replace")
        except LookupError:
            response_text = response_body.decode("utf-8", errors="replace")
        message = response_text.strip()
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError:
            payload = None

        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"].strip()
            elif isinstance(error, str):
                message = error.strip()
            elif isinstance(payload.get("message"), str):
                message = payload["message"].strip()
        if not message:
            message = "上游接口未返回错误详情"
        message = message[:MAX_ERROR_MESSAGE_LENGTH]
        raise TTSAPIError(
            response.status,
            message,
            response.headers.get("x-request-id", "").strip(),
        )

    @staticmethod
    def _validate_audio_content_type(response: ClientResponse) -> None:
        content_type = response.headers.get("Content-Type", "").split(";", maxsplit=1)[0].strip().lower()
        if content_type.startswith("audio/"):
            return
        if content_type in {"application/octet-stream", "binary/octet-stream"}:
            return
        displayed_type = content_type if content_type else "未提供"
        raise OpenAITTSClient._response_error(response, f"接口返回了非音频 Content-Type：{displayed_type}")

    @staticmethod
    def _response_error(response: ClientResponse, message: str) -> TTSAPIError:
        return TTSAPIError(
            response.status,
            message,
            response.headers.get("x-request-id", "").strip(),
        )

    def _validate_settings(self) -> None:
        base_url = self._settings.base_url.strip()
        if not base_url:
            raise TTSConfigurationError("Base URL 不能为空")
        parsed_base_url = urlsplit(base_url)
        if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
            raise TTSConfigurationError("Base URL 必须是有效的 HTTP 或 HTTPS 地址")
        if parsed_base_url.query or parsed_base_url.fragment:
            raise TTSConfigurationError("Base URL 不能包含查询参数或片段")

        endpoint_path = self._settings.endpoint_path.strip()
        if not endpoint_path:
            raise TTSConfigurationError("端点路径不能为空")
        parsed_endpoint = urlsplit(endpoint_path)
        if parsed_endpoint.scheme or parsed_endpoint.netloc or parsed_endpoint.query or parsed_endpoint.fragment:
            raise TTSConfigurationError("端点路径只能是站内路径，不能包含域名、查询参数或片段")
        if ".." in parsed_endpoint.path.split("/"):
            raise TTSConfigurationError("端点路径不能包含上级目录跳转")
        if not self._settings.model.strip():
            raise TTSConfigurationError("模型不能为空")
        if not self._settings.voice.strip() and not self._settings.custom_voice_id.strip():
            raise TTSConfigurationError("默认音色和自定义音色 ID 不能同时为空")

        response_format = self._settings.response_format.strip().lower()
        if response_format not in SUPPORTED_RESPONSE_FORMATS:
            supported = "、".join(sorted(SUPPORTED_RESPONSE_FORMATS))
            raise TTSConfigurationError(f"不支持的音频格式；可选值：{supported}")
        if not 0.25 <= self._settings.speed <= 4.0:
            raise TTSConfigurationError("默认语速必须在 0.25 到 4.0 之间")
        if self._settings.timeout_seconds <= 0:
            raise TTSConfigurationError("请求超时时间必须大于 0")
        if self._settings.max_text_length <= 0:
            raise TTSConfigurationError("文本长度上限必须大于 0")
        if self._settings.max_audio_size_mb <= 0:
            raise TTSConfigurationError("音频大小上限必须大于 0")
