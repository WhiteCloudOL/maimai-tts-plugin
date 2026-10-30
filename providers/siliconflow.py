"""硅基流动合成、动态参考音频和账号音色。"""

from typing import Any, Dict, List

from ..domain.models import Audio, Profile, TTSError
from .openai import OpenAIProvider


class SiliconFlowProvider(OpenAIProvider):
    voices_path = "audio/voice/list"
    clone_path = "uploads/audio/voice"

    def list_cache_parameters(self, profile: Profile, operation: str) -> Dict[str, Any]:
        values = super().list_cache_parameters(profile, operation)
        # 硅基流动一次返回整个账号的音色，各条记录自带绑定模型。
        values.pop("model")
        return values

    def payload(self, profile: Profile, text: str) -> Dict[str, Any]:
        payload = super().payload(profile, text)
        payload.pop("instructions", None)
        if profile.instructions:
            if profile.model != "FunAudioLLM/CosyVoice2-0.5B":
                raise TTSError("此硅基流动模型尚未确认支持风格指令；CosyVoice2 支持 endofprompt 指令控制")
            if "<|endofprompt|>" in text:
                raise TTSError("朗读正文含有平台控制标记，请在风格或语调配置中设置控制内容")
            payload["input"] = f"{profile.instructions}<|endofprompt|>{text}"
        payload["voice"] = profile.voice
        payload["gain"] = profile.options.gain
        payload["sample_rate"] = profile.options.sample_rate
        if (
            profile.options.reference_enabled
            and profile.options.reference_source == "url"
            and profile.options.reference_url
        ):
            payload.pop("voice")
            payload["references"] = [{"audio": profile.options.reference_url, "text": profile.options.reference_text}]
        return payload

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        payload = self.payload(profile, text)
        if profile.options.reference_enabled and profile.options.reference_source == "file":
            audio = await self.reference_audio(profile, limit=4 * 1024 * 1024)
            payload.pop("voice", None)
            payload["references"] = [{"audio": audio, "text": profile.options.reference_text}]
        return await self.request_audio(profile, payload)

    async def voices(self, profile: Profile) -> List[Dict[str, str]]:
        url = self.url(profile, "voices")
        if not url:
            raise TTSError("请先填写账号音色列表 URL")
        result = await self.http.json("GET", url, self.headers(profile))
        values = result.get("results")
        if not isinstance(values, list):
            raise TTSError("音色列表响应缺少 results 数组")
        return [
            {"id": item["uri"], "name": item.get("customName", item["uri"]), "model": item.get("model", "")}
            for item in values[:100]
            if isinstance(item, dict) and isinstance(item.get("uri"), str)
        ]

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> str:
        if not text.strip():
            raise TTSError("硅基流动创建音色需要参考音频对应的文字")
        audio = await self.reference_audio(profile, url, limit=4 * 1024 * 1024)
        result = await self.http.json(
            "POST",
            self.url(profile, "clone"),
            self.headers(profile),
            {
                "model": profile.model,
                "customName": name,
                "text": text,
                "audio": audio,
            },
        )
        if not isinstance(result.get("uri"), str) or not result["uri"]:
            raise TTSError("创建音色响应缺少 uri")
        return result["uri"]
