"""统一协议边界与音频校验。"""

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Union

import asyncio
import base64
import binascii
import io
import wave

from ..domain.models import Audio, Profile, TTSError
from ..infrastructure.transport import Transport
from ..infrastructure.reference_audio import reference_mime
from .endpoints import api_url


def decode_audio(value: str, limit: int) -> bytes:
    if not isinstance(value, str) or len(value) > (limit + 2) // 3 * 4:
        raise TTSError("上游音频编码无效或超过大小上限")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise TTSError("上游音频不是有效 Base64") from None
    if len(data) > limit:
        raise TTSError("上游音频超过大小上限")
    return data


def decode_hex(value: str, limit: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > limit * 2 or len(value) % 2:
        raise TTSError("音频 Hex 编码无效或超过大小上限")
    try:
        return binascii.unhexlify(value)
    except (ValueError, binascii.Error):
        raise TTSError("音频不是有效 Hex 编码") from None


def wrap_pcm(data: bytes, sample_rate: int, channels: int = 1) -> bytes:
    if len(data) % (2 * channels):
        raise TTSError("PCM16 音频长度无效")
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(data)
    return output.getvalue()


async def normalize_audio(data: bytes, profile: Profile, request_id: str = "") -> Audio:
    if not data:
        raise TTSError("上游返回了空音频")
    fmt = profile.response_format
    if data.startswith(b"RIFF"):
        fmt = "wav"
    elif data.startswith(b"OggS"):
        fmt = "opus"
    elif data.startswith(b"fLaC"):
        fmt = "flac"
    elif fmt == "pcm":
        channels = profile.options.minimax_channel if profile.kind in {"bailian_minimax", "minimax"} else 1
        data = await asyncio.to_thread(wrap_pcm, data, profile.options.sample_rate, channels)
        fmt = "wav"
    elif data.lstrip().startswith((b"{", b"[", b"<")):
        raise TTSError("上游返回了文本或错误页面，而不是音频")
    # Content-Type 由上游声明，仍需核对容器标识；完整解码与时长限制由转码阶段负责。
    valid_header = {
        "wav": data.startswith(b"RIFF") and len(data) >= 44 and data[8:12] == b"WAVE",
        "opus": data.startswith(b"OggS"),
        "flac": data.startswith(b"fLaC"),
        "mp3": data.startswith(b"ID3") or (len(data) > 1 and data[0] == 255 and data[1] & 224 == 224),
        "aac": data.startswith(b"ADIF") or (len(data) > 1 and data[0] == 255 and data[1] & 240 == 240),
    }
    if not valid_header.get(fmt, False):
        raise TTSError(f"上游音频缺少有效的 {fmt.upper()} 文件标识，请检查实际返回内容与输出格式")
    mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "opus": "audio/ogg", "flac": "audio/flac", "aac": "audio/aac"}
    return Audio(data, mime[fmt], fmt, profile.id, request_id)


@dataclass(frozen=True)
class CloneResult:
    """异步训练的平台保留音色 ID 和真实状态，避免重复提交训练。"""

    voice_id: str
    state: Literal["ready", "training", "failed"]
    preview: Optional[Audio] = None
    name: str = ""


class Provider:
    api_prefix = "/v1"
    speech_path = "audio/speech"
    models_path = "models"
    voices_path = ""
    clone_path = ""

    def __init__(self, transport: Transport) -> None:
        self.http = transport

    def url(self, profile: Profile, operation: str = "speech", *, websocket: bool = False, model: str = "") -> str:
        paths = {
            "speech": self.speech_path,
            "models": self.models_path,
            "voices": self.voices_path,
            "clone": self.clone_path,
        }
        overrides = {
            "speech": profile.endpoint,
            "models": profile.models_url,
            "voices": profile.voices_url,
            "clone": profile.clone_url,
        }
        # 旧配置继续按原完整地址请求；BaseURL 配置则由各平台声明路径。
        if not profile.base_url or (operation != "speech" and overrides[operation]):
            return overrides[operation]
        path = paths[operation]
        return api_url(profile.base_url, self.api_prefix, path, websocket=websocket, model=model) if path else ""

    async def reference_bytes(self, profile: Profile, source: str = "", limit: int = 7 * 1024 * 1024) -> bytes:
        """在工作线程读取本地录音，或通过独立无凭据请求下载录音。"""
        if profile.options.reference_source == "file":
            data = await self.http.reference_files.read(source or profile.options.reference_file, limit)
        elif profile.options.reference_source == "url":
            data = await self.http.download(source or profile.options.reference_url, limit)
        else:
            raise TTSError("此平台的参考音频来源应选择本地文件或公开 URL")
        reference_mime(data)
        return data

    async def reference_audio(self, profile: Profile, source: str = "", limit: int = 7 * 1024 * 1024) -> str:
        """转换为平台要求的 MP3/WAV data URL。"""
        data = await self.reference_bytes(profile, source, limit)
        mime = reference_mime(data)
        encoded = await asyncio.to_thread(lambda: base64.b64encode(data).decode("ascii"))
        return f"data:{mime};base64,{encoded}"

    def headers(self, profile: Profile) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {profile.api_key}"} if profile.api_key else {}
        if profile.kind in {"openai", "compatible"}:
            if profile.options.organization:
                headers["OpenAI-Organization"] = profile.options.organization
            if profile.options.project:
                headers["OpenAI-Project"] = profile.options.project
        return headers

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        raise NotImplementedError

    async def models(self, profile: Profile) -> List[str]:
        url = self.url(profile, "models")
        if not url:
            raise TTSError("该平台没有配置模型列表 URL，可直接填写模型 ID")
        result = await self.http.json("GET", url, self.headers(profile))
        data = result.get("data")
        if not isinstance(data, list):
            raise TTSError("模型列表响应缺少 data 数组")
        return [item["id"] for item in data[:200] if isinstance(item, dict) and isinstance(item.get("id"), str)]

    def list_cache_parameters(self, profile: Profile, operation: str) -> Dict[str, Any]:
        return {
            "kind": profile.kind,
            "url": self.url(profile, operation),
            "headers": self.headers(profile),
            "model": profile.model if operation == "voices" else "",
        }

    async def voices(self, profile: Profile) -> List[Dict[str, str]]:
        raise TTSError("该平台不提供账号音色列表；请选择预设或手动添加音色 ID")

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> Union[str, CloneResult]:
        raise TTSError("该平台使用合成时参考音频或控制台音色 ID，无独立创建音色接口")

    async def clone_status(self, profile: Profile, voice_id: str) -> CloneResult:
        raise TTSError("此平台的音色创建接口直接返回结果，请使用账号音色或音色 ID")

    async def design(self, profile: Profile, name: str) -> CloneResult:
        raise TTSError("此模型尚未提供声音设计接口")

    async def update_voice(self, profile: Profile, voice_id: str, source: str) -> CloneResult:
        raise TTSError("此平台尚未提供音色录音更新接口")

    async def delete_voice(self, profile: Profile, voice_id: str) -> None:
        raise TTSError("此平台尚未提供云端音色删除接口")

    def deletion_parameters(self, profile: Profile) -> Dict[str, Any]:
        """实际删除接口及鉴权身份，用于保护同一云端账号正在使用的音色。"""
        return {"url": self.url(profile, "clone"), "headers": self.headers(profile)}
