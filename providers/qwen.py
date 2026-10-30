"""千问 HTTP / Realtime 合成与原生音色复刻。"""

from aiohttp import WSMsgType
from typing import Any, Dict, List, Union
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import asyncio

from ..domain.models import Audio, Profile, TTSError
from ..domain.bailian import QWEN_AUDIO_MODELS
from .base import CloneResult, decode_audio, normalize_audio, wrap_pcm
from .bailian import BailianVoiceProvider
from .endpoints import api_url
from .http_protocol import json_value


class QwenProvider(BailianVoiceProvider):
    api_prefix = "/api/v1"
    speech_path = "services/aigc/multimodal-generation/generation"
    models_path = ""
    clone_path = "services/audio/tts/customization"

    def url(self, profile: Profile, operation: str = "speech", *, websocket: bool = False, model: str = "") -> str:
        if operation == "speech" and profile.model in QWEN_AUDIO_MODELS:
            return self.speech_http_url(profile)
        if profile.base_url and operation == "speech" and "realtime" in profile.model:
            return api_url(profile.base_url, self.api_prefix, "realtime", websocket=True, model=profile.model)
        return super().url(profile, operation, websocket=websocket, model=model)

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        if profile.model in QWEN_AUDIO_MODELS:
            if profile.options.alibaba_transport == "websocket":
                raise TTSError("当前 Qwen-Audio-TTS 适配使用 HTTP，请选择自动或 HTTP 协议")
            return await self.speech_http(profile, text)
        if profile.model.startswith("qwen-audio-"):
            raise TTSError("请选择已支持的 Qwen-Audio 3.0/3.1 TTS HTTP 模型")
        if self.url(profile).startswith(("ws://", "wss://")):
            return await self._realtime(profile, text)
        if "realtime" in profile.model:
            raise TTSError("请在平台页填写千问 BaseURL，实时模型会自动使用 WebSocket 协议")
        if len(text) > 600:
            raise TTSError("千问 HTTP TTS 单次文本上限为 600 字符")
        if profile.speed != 1:
            raise TTSError("千问 HTTP 接口不提供语速倍率，请设为 1，或使用风格指令")
        inputs = {"text": text, "voice": profile.voice, "language_type": profile.options.language}
        if profile.instructions:
            if not profile.model.startswith("qwen3-tts-instruct-flash"):
                raise TTSError("此千问模型未提供风格指令，请选择 Qwen3-TTS-Instruct-Flash 系列")
            inputs["instructions"] = profile.instructions
            inputs["optimize_instructions"] = profile.options.optimize_instructions
        result = await self.http.json(
            "POST",
            self.url(profile),
            self.headers(profile),
            {
                "model": profile.model,
                "input": inputs,
            },
        )
        output = result.get("output")
        if not isinstance(output, dict) or not isinstance(output.get("audio"), dict):
            raise TTSError("千问响应缺少有效 output.audio 对象")
        audio = output["audio"]
        if audio.get("data"):
            data = await asyncio.to_thread(decode_audio, audio["data"], self.http.limit)
        elif audio.get("url"):
            data = await self.http.download(audio["url"])
        else:
            raise TTSError("千问响应缺少 output.audio.data/url")
        return await normalize_audio(data, profile, result.get("request_id", ""))

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> Union[str, CloneResult]:
        if profile.model in QWEN_AUDIO_MODELS:
            return await super().clone(profile, name, url, text)
        if not profile.model.startswith("qwen3-tts-vc-"):
            raise TTSError("请先在音色页切换到千问克隆模式并选择对应复刻模型")
        reference = await self.reference_audio(profile, url)
        result = await self.http.json(
            "POST",
            self.url(profile, "clone"),
            self.headers(profile),
            {
                "model": "qwen-voice-enrollment",
                "input": {
                    "action": "create",
                    "target_model": profile.model,
                    "preferred_name": name,
                    "audio": {"data": reference},
                },
            },
        )
        output = result.get("output")
        if not isinstance(output, dict) or not isinstance(output.get("voice"), str) or not output["voice"]:
            raise TTSError("千问复刻响应缺少 output.voice")
        return output["voice"]

    async def voices(self, profile: Profile) -> List[Dict[str, str]]:
        if profile.model not in QWEN_AUDIO_MODELS:
            raise TTSError("账号音色列表适用于 Qwen-Audio 3.0/3.1 TTS 模型")
        return await super().voices(profile)

    async def clone_status(self, profile: Profile, voice_id: str) -> CloneResult:
        if profile.model not in QWEN_AUDIO_MODELS:
            raise TTSError("此状态查询适用于 Qwen-Audio 3.0/3.1 TTS 音色")
        return await super().clone_status(profile, voice_id)

    async def _realtime(self, profile: Profile, text: str) -> Audio:
        """等待 session.updated 后输入文本，只有 session.finished 才视为完成。"""
        endpoint = self.url(profile)
        query_model = parse_qs(urlsplit(endpoint).query).get("model", [""])[0]
        if query_model != profile.model:
            raise TTSError("千问 WebSocket URL 的 model 参数必须与模型 ID 相同")
        if not profile.voice:
            raise TTSError("请先创建并选用千问克隆音色，或填写该模型支持的音色 ID")
        if profile.speed != 1:
            raise TTSError("千问实时接口请通过风格指令控制语速，倍率保持 1")
        session: Dict[str, Any] = {
            "voice": profile.voice,
            "mode": "server_commit",
            "language_type": profile.options.language,
            "response_format": "pcm",
            "sample_rate": 24000,
        }
        if profile.instructions:
            if not profile.model.startswith("qwen3-tts-instruct-flash-realtime"):
                raise TTSError("此千问实时模型未提供风格指令，请选择 Instruct-Flash-Realtime 系列")
            session["instructions"] = profile.instructions
            session["optimize_instructions"] = profile.options.optimize_instructions
        audio = bytearray()
        finished = False
        submitted = False
        session_id = ""
        async with self.http.session.ws_connect(
            endpoint,
            headers=self.headers(profile),
            proxy=self.http.proxy,
            max_msg_size=self.http.limit * 2,
            heartbeat=20,
        ) as socket:
            await socket.send_json({"event_id": uuid4().hex, "type": "session.update", "session": session})
            async for message in socket:
                if message.type == WSMsgType.ERROR:
                    raise TTSError("千问实时 WebSocket 连接失败")
                if message.type != WSMsgType.TEXT:
                    continue
                event = await json_value(message.data)
                if not isinstance(event, dict):
                    raise TTSError("千问实时事件应为 JSON 对象")
                if event.get("code") not in (None, "", 0, 200):
                    detail = self.http.sanitize(str(event.get("message", "接口失败")), self.headers(profile))
                    raise TTSError(f"千问实时接口错误（{event['code']}）：{detail}")
                event_type = event.get("type")
                if event_type == "error":
                    detail = str(event.get("error", "未知实时接口错误"))
                    raise TTSError("千问实时接口错误：" + self.http.sanitize(detail, self.headers(profile)))
                if event_type == "session.created":
                    # session/id 是关联信息；省略时仍可继续协议，显式提供时校验其类型。
                    created = event.get("session", {})
                    if not isinstance(created, dict) or ("id" in created and not isinstance(created["id"], str)):
                        raise TTSError("千问实时事件缺少有效 session.id")
                    session_id = created.get("id", "")
                elif event_type == "session.updated" and not submitted:
                    submitted = True
                    await socket.send_json({"event_id": uuid4().hex, "type": "input_text_buffer.append", "text": text})
                    await socket.send_json({"event_id": uuid4().hex, "type": "session.finish"})
                elif event_type == "response.audio.delta":
                    chunk = await asyncio.to_thread(decode_audio, event.get("delta", ""), self.http.limit)
                    if len(audio) + len(chunk) > self.http.limit:
                        raise TTSError("千问实时音频超过大小上限")
                    audio.extend(chunk)
                elif event_type == "response.done":
                    completed = event.get("response", {})
                    if not isinstance(completed, dict):
                        raise TTSError("千问实时完成事件缺少 response 对象")
                    if completed.get("status") in {"failed", "cancelled"}:
                        raise TTSError("千问实时合成未成功完成")
                elif event_type == "session.finished":
                    finished = True
                    break
        if not finished or not submitted or not audio:
            raise TTSError("千问实时会话未完整结束或返回空音频")
        wav = await asyncio.to_thread(wrap_pcm, bytes(audio), 24000)
        return Audio(wav, "audio/wav", "wav", profile.id, session_id)
