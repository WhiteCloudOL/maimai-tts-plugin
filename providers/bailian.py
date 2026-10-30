"""Qwen-Audio-TTS / CosyVoice 共用的 HTTP 合成和账号音色协议。"""

from typing import Any, Dict, List, Optional

import asyncio
import re

from ..domain.bailian import DESIGN_MODELS, QWEN_AUDIO_MODELS, VOICE_LANGUAGES, VOICE_STATES
from ..domain.models import Audio, Profile, TTSError, validate_url
from ..domain.intonation import tone_capability
from .base import CloneResult, Provider, decode_audio, normalize_audio
from .endpoints import api_url


class BailianVoiceProvider(Provider):
    api_prefix = "/api/v1"
    voices_path = "services/audio/tts/customization"
    clone_path = voices_path

    def list_cache_parameters(self, profile: Profile, operation: str) -> Dict[str, Any]:
        values = super().list_cache_parameters(profile, operation)
        if operation == "voices":
            values["details_url"] = self.enrollment_url(profile)
        return values

    async def speech_http(self, profile: Profile, text: str) -> Audio:
        """非流式接口只有 finish_reason=stop 才产生可发送的完整音频。"""
        options = profile.options
        inputs: Dict[str, Any] = {
            "text": text,
            "voice": profile.voice,
            "format": profile.response_format,
            "sample_rate": options.sample_rate,
            "volume": options.volume,
            "rate": profile.speed,
            "pitch": options.pitch,
        }
        if not profile.voice:
            raise TTSError("请选择此模型支持的预设、复刻或设计音色")
        if profile.response_format == "opus":
            if options.sample_rate not in {8000, 12000, 16000, 24000, 48000}:
                raise TTSError("Opus 采样率请选择 8000、12000、16000、24000 或 48000")
            inputs["bit_rate"] = options.opus_bit_rate
        if profile.instructions:
            if tone_capability(profile.kind, profile.model, profile.voice, "") is None:
                raise TTSError("此模型与音色使用原生语速等参数，请选择支持指令的组合后设置风格")
            inputs["instruction"] = profile.instructions
        if options.seed is not None:
            inputs["seed"] = options.seed
        if options.voice_language:
            languages = VOICE_LANGUAGES | ({"other"} if profile.model == "qwen-audio-3.1-tts-flash" else set())
            if options.voice_language not in languages:
                raise TTSError("请选择当前模型支持的合成语言代码")
            inputs["language_hints"] = [options.voice_language]
        if options.enable_ssml:
            if profile.kind == "qwen" and tone_capability(profile.kind, profile.model, profile.voice, "") is None:
                raise TTSError("此千问音色采用原生文本朗读，请选择支持 SSML 的音色后开启")
            inputs["enable_ssml"] = True
        if options.hot_fix:
            if profile.model == "cosyvoice-v2":
                raise TTSError("CosyVoice v2 尚未提供文本热修复参数")
            if set(options.hot_fix) - {"pronunciation", "replace"}:
                raise TTSError("文本热修复仅接受 pronunciation 和 replace 字段")
            inputs["hot_fix"] = options.hot_fix
        result = await self.http.json(
            "POST", self.url(profile), self.headers(profile), {"model": profile.model, "input": inputs}
        )
        output = result.get("output")
        if not isinstance(output, dict) or output.get("finish_reason") != "stop":
            raise TTSError("百炼 HTTP 合成尚未完整结束（finish_reason 应为 stop）")
        audio = output.get("audio")
        if not isinstance(audio, dict):
            raise TTSError("百炼响应缺少 output.audio")
        if audio.get("data"):
            data = await asyncio.to_thread(decode_audio, audio["data"], self.http.limit)
        elif isinstance(audio.get("url"), str) and audio["url"]:
            data = await self.http.download(audio["url"])
        else:
            raise TTSError("百炼响应缺少音频 data/url")
        return await normalize_audio(data, profile, result.get("request_id", ""))

    def enrollment_url(self, profile: Profile) -> str:
        return self.url(profile, "clone")

    async def enrollment(
        self, profile: Profile, inputs: Dict[str, Any], parameters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        payload = {"model": "voice-enrollment", "input": inputs}
        if parameters is not None:
            payload["parameters"] = parameters
        endpoint = self.url(profile, "voices") if inputs["action"] == "list_voice" else self.enrollment_url(profile)
        result = await self.http.json("POST", endpoint, self.headers(profile), payload)
        output = result.get("output")
        if not isinstance(output, dict):
            raise TTSError("百炼音色响应缺少 output 对象")
        return output

    @staticmethod
    def prefix(name: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9]{1,10}", name):
            raise TTSError("阿里云音色前缀请使用 1–10 位英文字母或数字")
        return name

    @staticmethod
    def voice_result(output: Dict[str, Any], profile: Profile) -> CloneResult:
        voice_id = output.get("voice_id")
        if not isinstance(voice_id, str) or not voice_id:
            raise TTSError("百炼音色响应缺少 output.voice_id")
        if output.get("target_model") and output["target_model"] != profile.model:
            raise TTSError("此账号音色绑定的模型与当前模型不同，请选择其绑定模型")
        status = output.get("status", "DEPLOYING")
        if status not in VOICE_STATES:
            raise TTSError("百炼返回了未知音色状态：" + str(status))
        return CloneResult(voice_id, VOICE_STATES[status])

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> CloneResult:
        # 现行 HTTP 复刻接口通过 audio 接收 URL 或 Data URI；本机路径始终只在麦麦侧读取。
        audio = (
            await self.reference_audio(profile, url)
            if profile.options.reference_source == "file"
            else validate_url(url)
        )
        inputs: Dict[str, Any] = {
            "action": "create_voice",
            "target_model": profile.model,
            "prefix": self.prefix(name),
            "audio": audio,
        }
        options = profile.options
        if options.clone_language_hint:
            if profile.model == "cosyvoice-v2":
                raise TTSError("CosyVoice v2 复刻使用平台自动识别录音语言，请将录音语言留空")
            languages = {"zh", "en", "fr", "de", "ja", "ko", "ru", "it"}
            if profile.model != "cosyvoice-v3-plus":
                languages |= {"pt", "th", "id", "vi"}
            if profile.model in QWEN_AUDIO_MODELS:
                languages = VOICE_LANGUAGES
            if options.clone_language_hint not in languages:
                raise TTSError("录音语言超出此复刻模型支持范围")
            inputs["language_hints"] = [options.clone_language_hint]
        advanced = profile.model in QWEN_AUDIO_MODELS or profile.model in {
            "cosyvoice-v3.5-plus",
            "cosyvoice-v3.5-flash",
            "cosyvoice-v3-flash",
        }
        for key in ("max_prompt_audio_length", "enable_preprocess", "enable_volume_normalization"):
            value = options.model_dump()[key]
            if value is not None:
                if key != "enable_volume_normalization" and not advanced:
                    raise TTSError("当前复刻模型尚未提供录音预处理参数，请沿用平台默认值")
                inputs[key] = str(value).lower() if key == "enable_volume_normalization" else value
        return self.voice_result(await self.enrollment(profile, inputs), profile)

    async def design(self, profile: Profile, name: str) -> CloneResult:
        if profile.model not in DESIGN_MODELS:
            raise TTSError("声音设计适用于 Qwen-Audio 3.0/3.1 TTS 与 CosyVoice v3/v3.5 模型")
        options = profile.options
        if not options.voice_prompt.strip() or not 15 <= len(options.design_preview_text) <= 200:
            raise TTSError("请填写 1–500 字符声音描述与 15–200 字符预览文本")
        output = await self.enrollment(
            profile,
            {
                "action": "create_voice",
                "target_model": profile.model,
                "prefix": self.prefix(name),
                "voice_prompt": options.voice_prompt,
                "preview_text": options.design_preview_text,
                "language_hints": [options.design_language],
            },
            {"sample_rate": 24000, "response_format": "wav"},
        )
        if output.get("target_model") != profile.model:
            raise TTSError("声音设计返回的绑定模型与当前模型不同")
        result = self.voice_result(output, profile)
        preview = output.get("preview_audio")
        if (
            not isinstance(preview, dict)
            or preview.get("response_format") != "wav"
            or preview.get("sample_rate") != 24000
        ):
            raise TTSError("声音设计响应缺少请求格式的 WAV 预览音频")
        data = await asyncio.to_thread(decode_audio, preview.get("data"), self.http.limit)
        audio = await normalize_audio(data, profile.model_copy(update={"response_format": "wav"}))
        return CloneResult(result.voice_id, result.state, audio)

    async def clone_status(self, profile: Profile, voice_id: str) -> CloneResult:
        output = await self.enrollment(profile, {"action": "query_voice", "voice_id": voice_id})
        if (
            ("voice_id" in output and output["voice_id"] != voice_id)
            or not output.get("status")
            or not output.get("target_model")
        ):
            raise TTSError("音色查询响应中的 ID 不匹配，或缺少状态与绑定模型")
        # 复刻查询协议不回显 ID；设计查询协议会回显，二者均以本次请求 ID 关联。
        return self.voice_result({"voice_id": voice_id, **output}, profile)

    async def voices(self, profile: Profile) -> List[Dict[str, str]]:
        voices = []
        semaphore = asyncio.Semaphore(6)

        async def detail(item: Dict[str, Any]) -> Dict[str, str]:
            async with semaphore:
                output = await self.enrollment(profile, {"action": "query_voice", "voice_id": item["voice_id"]})
            if (
                ("voice_id" in output and output["voice_id"] != item["voice_id"])
                or not output.get("target_model")
                or output.get("status") not in VOICE_STATES
            ):
                raise TTSError("账号音色详情缺少匹配 ID、模型或有效状态")
            return {
                "id": item["voice_id"],
                "name": item["voice_id"][:80],
                "model": output["target_model"],
                "clone_state": VOICE_STATES[output["status"]],
                "origin": "design" if output.get("voice_prompt") else "account",
                "design_prompt": output.get("voice_prompt", ""),
                "design_preview_text": output.get("preview_text", ""),
            }

        # 列表未返回 target_model，逐项查详情后过滤，避免把别的模型音色误用于合成。
        for page in range(10):
            output = await self.enrollment(profile, {"action": "list_voice", "page_index": page, "page_size": 100})
            items = output.get("voice_list")
            if not isinstance(items, list) or any(not isinstance(v, dict) or not v.get("voice_id") for v in items):
                raise TTSError("账号音色响应缺少有效 voice_list")
            tasks = [asyncio.create_task(detail(item)) for item in items]
            try:
                details = await asyncio.gather(*tasks)
            except (Exception, asyncio.CancelledError):
                # gather 的单项异常不会停止其他查询；结束本次任务前收回所有子请求。
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            voices.extend(item for item in details if item["model"] == profile.model)
            if len(voices) >= 100 or len(items) < 100:
                break
        return voices[:100]

    async def update_voice(self, profile: Profile, voice_id: str, source: str) -> CloneResult:
        # 先核实模型归属，更新后重新进入审核状态。
        await self.clone_status(profile, voice_id)
        audio = (
            await self.reference_audio(profile, source)
            if profile.options.reference_source == "file"
            else validate_url(source)
        )
        await self.enrollment(profile, {"action": "update_voice", "voice_id": voice_id, "audio": audio})
        return CloneResult(voice_id, "training")

    async def delete_voice(self, profile: Profile, voice_id: str) -> None:
        await self.clone_status(profile, voice_id)
        await self.enrollment(profile, {"action": "delete_voice", "voice_id": voice_id})

    def speech_http_url(self, profile: Profile) -> str:
        if not profile.base_url and not profile.endpoint.startswith(("http://", "https://")):
            raise TTSError("HTTP 合成请填写平台 BaseURL，或为此模型设置 HTTP 基础地址")
        return (
            api_url(profile.base_url, self.api_prefix, "services/audio/tts/SpeechSynthesizer")
            if profile.base_url
            else profile.endpoint
        )
