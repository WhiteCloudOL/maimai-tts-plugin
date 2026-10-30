"""阿里云百炼 MiniMax：同步合成、快速复刻与账号音色管理。"""

from typing import Any, Dict, List
from urllib.parse import urlsplit

import asyncio
import re

from ..domain.models import Audio, Profile, TTSError, validate_url
from .base import CloneResult, Provider, decode_hex, normalize_audio
from .minimax_protocol import account_voices, speech_payload


class BailianMiniMaxProvider(Provider):
    api_prefix = "/api/v1"
    speech_path = "services/aigc/multimodal-generation/generation"
    models_path = ""
    voices_path = speech_path
    clone_path = speech_path

    def list_cache_parameters(self, profile: Profile, operation: str) -> Dict[str, Any]:
        values = super().list_cache_parameters(profile, operation)
        values.pop("model")
        return values

    def deletion_parameters(self, profile: Profile) -> Dict[str, Any]:
        return {"url": self.url(profile, "voices"), "headers": self.headers(profile)}

    async def request(self, profile: Profile, inputs: Dict[str, Any], *, management: bool = False) -> Dict[str, Any]:
        operation = {"get_voice": "voices", "voice_clone": "clone", "delete_voice": "voices"}.get(
            inputs.get("action"), "speech"
        )
        endpoint = self.url(profile, operation)
        if (urlsplit(endpoint).hostname or "").endswith(".maas.aliyuncs.com"):
            raise TTSError("百炼 MiniMax 请使用 https://dashscope.aliyuncs.com/api/v1 作为 BaseURL")
        result = await self.http.json(
            "POST",
            endpoint,
            self.headers(profile),
            {
                "model": "MiniMax/speech-2.8-turbo" if management else profile.model,
                "input": inputs,
            },
        )
        output = result.get("output")
        if not isinstance(output, dict) or not isinstance(output.get("base_resp"), dict):
            raise TTSError("MiniMax 响应缺少 output.base_resp")
        status = output["base_resp"]
        if type(status.get("status_code")) is not int:
            raise TTSError("MiniMax 响应缺少有效的整数状态码")
        if status.get("status_code") != 0:
            detail = self.http.sanitize(str(status.get("status_msg", "接口失败")), self.headers(profile))
            raise TTSError(f"MiniMax 接口错误（{status.get('status_code')}）：{detail}")
        return output

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        options = profile.options
        inputs = speech_payload(profile, text)
        output = await self.request(profile, inputs)
        data = output.get("data")
        if not isinstance(data, dict) or data.get("status") != 2:
            raise TTSError("MiniMax 合成尚未完整结束（output.data.status 应为 2）")
        encoded = data.get("audio")
        if not isinstance(encoded, str) or not encoded:
            raise TTSError("MiniMax 响应缺少 output.data.audio")
        binary = (
            await self.http.download(validate_url(encoded))
            if options.minimax_output == "url"
            else await asyncio.to_thread(decode_hex, encoded, self.http.limit)
        )
        return await normalize_audio(binary, profile, str(output.get("trace_id", "")))

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> CloneResult:
        options = profile.options
        voice_id = options.clone_speaker_id
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{6,254}[A-Za-z0-9]", voice_id):
            raise TTSError("MiniMax 音色 ID 需要 8–256 位，以字母开头、字母或数字结尾，中间可使用 _ 和 -")
        if options.reference_source != "url":
            raise TTSError("百炼 MiniMax 复刻接口需要公网录音 URL，请选择公开音频 URL")
        if not text.strip() or len(text) > 1000:
            raise TTSError("MiniMax 克隆需要填写 1–1000 字符的试听文本")
        inputs = {
            "action": "voice_clone",
            "voice_id": voice_id,
            "audio_url": validate_url(url),
            "text": text,
            "language_boost": options.language_boost,
            "need_noise_reduction": options.need_noise_reduction,
            "need_volume_normalization": options.need_volume_normalization,
            "aigc_watermark": options.aigc_watermark,
        }
        if options.prompt_audio_url or options.prompt_text:
            if not options.prompt_text:
                raise TTSError("使用示例录音时请同时填写对应示例文字")
            inputs["clone_prompt"] = {
                "prompt_audio": validate_url(options.prompt_audio_url),
                "prompt_text": options.prompt_text,
            }
        output = await self.request(profile, inputs)
        if output.get("input_sensitive") is not False:
            raise TTSError("MiniMax 克隆未通过内容校验，请检查录音和试听文字")
        demo = output.get("demo_audio")
        if not isinstance(demo, str) or not demo:
            raise TTSError("MiniMax 克隆响应缺少 demo_audio 预览地址")
        data = await self.http.download(validate_url(demo))
        preview = await normalize_audio(data, profile.model_copy(update={"response_format": "mp3"}))
        return CloneResult(voice_id, "ready", preview)

    async def voices(self, profile: Profile) -> List[Dict[str, str]]:
        output = await self.request(profile, {"action": "get_voice", "voice_type": "all"}, management=True)
        return account_voices(output)

    async def delete_voice(self, profile: Profile, voice_id: str) -> None:
        output = await self.request(
            profile, {"action": "delete_voice", "voice_type": "voice_cloning", "voice_id": voice_id}, management=True
        )
        if output.get("voice_id") != voice_id:
            raise TTSError("MiniMax 删除响应的音色 ID 与目标不同")
