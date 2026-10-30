"""小米 MiMo Chat Audio 合成及参考音色。"""

from typing import Any, Dict, List

import asyncio
import base64

from ..domain.models import Audio, Profile, TTSError
from .base import Provider, decode_audio, normalize_audio


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
        audio: Dict[str, Any] = {"format": profile.response_format}
        if "voiceclone" in profile.model:
            reference = await self.reference_bytes(profile)
            audio["voice"] = await asyncio.to_thread(lambda: base64.b64encode(reference).decode("ascii"))
        elif "voicedesign" in profile.model:
            if not profile.instructions:
                raise TTSError("MiMo voicedesign 模型需要填写音色设计指令")
            audio["optimize_text_preview"] = profile.options.optimize_text_preview
        else:
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
        return await normalize_audio(decoded, profile, result.get("id", ""))
