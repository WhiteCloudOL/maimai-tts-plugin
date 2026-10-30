"""OpenAI Speech API 及其兼容接口。"""

from aiohttp import ClientResponse, FormData
from typing import Any, Dict
from urllib.parse import quote, urlsplit, urlunsplit

import asyncio

from ..domain.models import Audio, Profile, TTSError
from ..infrastructure.reference_audio import reference_mime
from .base import Provider, decode_audio, normalize_audio
from .http_protocol import json_value, sse_events


class OpenAIProvider(Provider):
    clone_path = "audio/voices"

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> str:
        consent = profile.options.voice_consent_id.strip()
        if not consent:
            raise TTSError("创建 OpenAI 音色需要填写已有的授权记录 ID（consent ID）")
        data = await self.reference_bytes(profile, url)
        mime = reference_mime(data)
        form = FormData()
        form.add_field("type", "audio_sample")
        form.add_field("name", name)
        form.add_field("consent", consent)
        form.add_field(
            "audio_sample", data, filename="sample.wav" if mime == "audio/wav" else "sample.mp3", content_type=mime
        )
        result = await self.http.multipart(self.url(profile, "clone"), self.headers(profile), form)
        voice_id = result.get("id")
        if not isinstance(voice_id, str) or not voice_id:
            raise TTSError("OpenAI 创建音色响应缺少 id")
        return voice_id

    def payload(self, profile: Profile, text: str) -> Dict[str, Any]:
        official = profile.kind in {"openai", "compatible"}
        if official and len(text) > 4096:
            raise TTSError("OpenAI Speech 单次文本上限为 4096 字符")
        if official and len(profile.instructions) > 4096:
            raise TTSError("OpenAI Speech 风格与语调指令合计上限为 4096 字符")
        if official and profile.model in {"tts-1", "tts-1-hd"} and profile.instructions:
            raise TTSError("tts-1 / tts-1-hd 不支持风格指令，请清空指令或更换模型")
        voice: Any = {"id": profile.voice} if profile.options.custom_voice else profile.voice
        payload = {
            "model": profile.model,
            "input": text,
            "voice": voice,
            "response_format": profile.response_format,
            "speed": profile.speed,
        }
        if profile.instructions:
            payload["instructions"] = profile.instructions
        if official and profile.options.openai_stream_format == "sse":
            if profile.model in {"tts-1", "tts-1-hd"}:
                raise TTSError("tts-1 / tts-1-hd 请使用 audio 音频流返回方式")
            payload["stream_format"] = "sse"
        return payload

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        return await self.request_audio(profile, self.payload(profile, text))

    async def request_audio(self, profile: Profile, payload: Dict[str, Any]) -> Audio:
        async with self.http.session.post(
            self.url(profile),
            json=payload,
            headers=self.headers(profile),
            proxy=self.http.proxy,
            allow_redirects=False,
        ) as response:
            if profile.kind in {"openai", "compatible"} and profile.options.openai_stream_format == "sse":
                return await self.sse_audio(profile, response)
            data = await self.http.read(response)
            request_id = response.headers.get(
                "x-siliconcloud-trace-id" if profile.kind == "siliconflow" else "x-request-id", ""
            )
            media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if (
                media_type
                and not media_type.startswith("audio/")
                and media_type not in {"application/octet-stream", "binary/octet-stream", "application/ogg"}
            ):
                raise TTSError(f"上游返回非音频内容（{media_type}），请检查请求 URL 和接口协议")
        if profile.kind in {"openai", "compatible"} and profile.response_format == "pcm":
            profile = profile.model_copy(deep=True)
            profile.options.sample_rate = 24000
        return await normalize_audio(data, profile, request_id)

    async def sse_audio(self, profile: Profile, response: ClientResponse) -> Audio:
        await self.http.check(response)
        if response.content_type != "text/event-stream":
            raise TTSError("请求了 OpenAI SSE，但上游返回的 Content-Type 不是 text/event-stream")
        audio = bytearray()
        finished = False
        async for event, raw in sse_events(response, self.http.limit * 2 + 1024 * 1024):
            if raw == "[DONE]":
                finished = True
                break
            data = await json_value(raw.encode("utf-8"))
            if not isinstance(data, dict):
                raise TTSError("OpenAI SSE 事件应为 JSON 对象")
            kind = data.get("type") or event
            if kind in {"error", "speech.error"} or data.get("error"):
                raise TTSError(
                    "OpenAI SSE 返回错误："
                    + self.http.sanitize(
                        str(data.get("error") or data.get("message", "合成失败")), self.headers(profile)
                    )[:500]
                )
            if kind in {"speech.audio.done", "audio.done", "done"}:
                finished = True
                break
            if kind in {"speech.audio.delta", "audio.delta", "audio"}:
                encoded = data.get("data") if kind == "audio" else data.get("audio")
                chunk = await asyncio.to_thread(decode_audio, encoded, self.http.limit)
                if len(audio) + len(chunk) > self.http.limit:
                    raise TTSError("OpenAI SSE 音频超过大小上限")
                audio.extend(chunk)
        if not finished or not audio:
            raise TTSError("OpenAI SSE 尚未完整结束或返回空音频")
        audio_profile = profile.model_copy(deep=True)
        audio_profile.options.sample_rate = 24000
        return await normalize_audio(bytes(audio), audio_profile, response.headers.get("x-request-id", ""))

    async def delete_voice(self, profile: Profile, voice_id: str) -> None:
        parsed = urlsplit(self.url(profile, "clone"))
        url = urlunsplit(parsed._replace(path=parsed.path.rstrip("/") + "/" + quote(voice_id, safe="")))
        result = await self.http.json("DELETE", url, self.headers(profile))
        if result.get("id") != voice_id or result.get("deleted") is not True:
            raise TTSError("OpenAI 删除音色响应缺少匹配 ID 或成功标志")
