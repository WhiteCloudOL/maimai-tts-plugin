"""小米 MiMo Chat Audio 合成及参考音色。"""

from typing import Any, Dict, List

import asyncio

from ..domain.models import Audio, Profile, TTSError
from .base import Provider, decode_audio, normalize_audio


MAX_REFERENCE_BASE64_BYTES = 10_000_000


class MiMoProvider(Provider):
    speech_path = "chat/completions"

    def headers(self, profile: Profile) -> Dict[str, str]:
        return {"api-key": profile.api_key} if profile.api_key else {}

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        if profile.speed != 1:
            raise TTSError("MiMo 请通过风格指令控制语速，倍率保持 1")
        messages: List[Dict[str, Any]] = []
        if profile.instructions:
            messages.append({"role": "user", "content": profile.instructions})
        messages.append({"role": "assistant", "content": text})
        audio: Dict[str, Any] = {"format": "pcm16" if profile.response_format == "pcm" else profile.response_format}
        if "voiceclone" in profile.model:
            # MiMo 要求带实际 MIME 的 Data URL，编码后的参考录音最多 10 MB。
            # 为 Data URL 的 MIME 前缀预留空间，使整个字段也落在文档的编码上限内。
            maximum = (MAX_REFERENCE_BASE64_BYTES - 32) // 4 * 3
            audio["voice"] = await self.reference_audio(profile, limit=min(maximum, self.http.limit))
        elif "voicedesign" in profile.model:
            if not profile.instructions:
                raise TTSError("MiMo voicedesign 模型需要填写音色设计指令")
            audio["optimize_text_preview"] = profile.options.optimize_text_preview
        else:
            if profile.voice:
                audio["voice"] = profile.voice
        result = await self.http.json(
            "POST",
            self.url(profile),
            self.headers(profile),
            {
                "model": profile.model,
                "messages": messages,
                "audio": audio,
                "stream": False,
            },
        )
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise TTSError("MiMo 响应缺少有效 choices[0] 对象")
        choice = choices[0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise TTSError(f"MiMo 未完整生成音频：{choice['finish_reason']}")
        message = choice.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("audio"), dict):
            raise TTSError("MiMo 响应缺少 choices[0].message.audio 对象")
        data = message["audio"].get("data")
        decoded = await asyncio.to_thread(decode_audio, data, self.http.limit)
        if profile.response_format == "pcm":
            # 官方 PCM 固定为 24 kHz、16-bit、小端单声道，独立于其他平台的采样率设置。
            profile = profile.model_copy(update={
                "options": profile.options.model_copy(update={"sample_rate": 24000}),
            })
        return await normalize_audio(decoded, profile, result.get("id", ""))
