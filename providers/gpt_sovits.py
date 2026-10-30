"""GPT-SoVITS 官方 API v2。"""

from typing import Any, Dict

from ..domain.models import Audio, Profile, TTSError
from .openai import OpenAIProvider


class GPTSoVITSProvider(OpenAIProvider):
    api_prefix = ""
    speech_path = "tts"
    models_path = ""

    def payload(self, profile: Profile, text: str) -> Dict[str, Any]:
        opts = profile.options
        if opts.reference_source != "file" and not opts.reference_path:
            raise TTSError("请填写 GPT-SoVITS 服务端参考音频路径")
        return {
            "text": text,
            "text_lang": opts.text_language,
            "ref_audio_path": opts.reference_path,
            "prompt_text": opts.reference_text,
            "prompt_lang": opts.prompt_language,
            "text_split_method": opts.text_split_method,
            "speed_factor": profile.speed,
            "top_k": opts.top_k,
            "top_p": opts.top_p,
            "temperature": opts.temperature,
            "media_type": "wav",
            "streaming_mode": False,
        }

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        payload = self.payload(profile, text)
        if profile.options.reference_source == "file":
            payload["ref_audio_path"] = await self.http.reference_files.local_path(profile.options.reference_file)
        return await self.request_audio(profile, payload)
