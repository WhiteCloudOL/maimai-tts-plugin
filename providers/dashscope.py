"""CosyVoice HTTP / WebSocket 合成，账号音色协议复用百炼适配。"""

from aiohttp import WSMsgType
from typing import Any, Dict
from uuid import uuid4

from ..domain.models import Audio, Profile, TTSError
from .base import normalize_audio
from .bailian import BailianVoiceProvider
from .http_protocol import json_value


class CosyVoiceProvider(BailianVoiceProvider):
    api_prefix = "/api/v1"
    speech_path = "inference"
    models_path = ""
    voices_path = "services/audio/tts/customization"
    clone_path = "services/audio/tts/customization"

    def url(self, profile: Profile, operation: str = "speech", *, websocket: bool = False, model: str = "") -> str:
        if operation == "speech" and profile.options.alibaba_transport == "http":
            return self.speech_http_url(profile)
        return super().url(profile, operation, websocket=operation == "speech", model=model)

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        if profile.options.alibaba_transport == "http":
            return await self.speech_http(profile, text)
        options = profile.options
        if (
            options.hot_fix
            or options.enable_ssml
            or options.seed is not None
            or options.voice_language
            or profile.response_format == "opus"
        ):
            raise TTSError("Opus、SSML、种子、语言提示与热修复参数请使用 HTTP 合成协议")
        task_id = uuid4().hex
        header = {"task_id": task_id, "streaming": "duplex"}
        parameters: Dict[str, Any] = {
            "text_type": "PlainText",
            "voice": profile.voice,
            "format": profile.response_format,
            "sample_rate": profile.options.sample_rate,
            "volume": profile.options.volume,
            "rate": profile.speed,
            "pitch": profile.options.pitch,
        }
        if profile.instructions:
            parameters["instruction"] = profile.instructions
        audio = bytearray()
        finished = False
        async with self.http.session.ws_connect(
            self.url(profile),
            headers=self.headers(profile),
            proxy=self.http.proxy,
            max_msg_size=self.http.limit,
            heartbeat=20,
        ) as socket:
            await socket.send_json(
                {
                    "header": {**header, "action": "run-task"},
                    "payload": {
                        "task_group": "audio",
                        "task": "tts",
                        "function": "SpeechSynthesizer",
                        "model": profile.model,
                        "parameters": parameters,
                        "input": {},
                    },
                }
            )
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    event = await json_value(message.data)
                    if not isinstance(event, dict) or not isinstance(event.get("header"), dict):
                        raise TTSError("CosyVoice 事件缺少有效 header 对象")
                    info = event.get("header", {})
                    if info.get("task_id") != task_id:
                        raise TTSError("CosyVoice 返回了不匹配的任务 ID")
                    if info.get("event") == "task-started":
                        await socket.send_json(
                            {"header": {**header, "action": "continue-task"}, "payload": {"input": {"text": text}}}
                        )
                        await socket.send_json(
                            {"header": {**header, "action": "finish-task"}, "payload": {"input": {}}}
                        )
                    elif info.get("event") == "task-failed":
                        detail = f"CosyVoice 任务失败：{info.get('error_code', '')} {info.get('error_message', '')}"
                        raise TTSError(self.http.sanitize(detail, self.headers(profile)))
                    elif info.get("event") == "task-finished":
                        finished = True
                        break
                elif message.type == WSMsgType.BINARY:
                    if len(audio) + len(message.data) > self.http.limit:
                        raise TTSError("CosyVoice 音频超过大小上限")
                    audio.extend(message.data)
                elif message.type == WSMsgType.ERROR:
                    raise TTSError("CosyVoice WebSocket 连接失败")
        if not finished:
            raise TTSError("CosyVoice 未完成任务就断开连接，拒绝发送不完整音频")
        return await normalize_audio(bytes(audio), profile, task_id)
