"""火山引擎 V3 HTTP 单向流式协议。"""

from typing import Any, Dict, List
from uuid import uuid4

import asyncio
import base64
import json
import re

from ..domain.models import Audio, Profile, TTSError, validate_url
from ..domain.volcengine import (
    CLONE_RESOURCES,
    EXPRESSIVE_MODEL,
    RESERVED_SPEAKER,
    cot_text,
    supports_instructions,
    synthesis_model,
)
from ..infrastructure.reference_audio import volc_reference_format
from .base import CloneResult, Provider, decode_audio, normalize_audio
from .endpoints import api_url


class VolcengineProvider(Provider):
    api_prefix = "/api/v3"
    speech_path = "tts/unidirectional"
    models_path = ""
    clone_path = "tts/voice_clone"

    def clone_headers(self, profile: Profile) -> Dict[str, str]:
        headers = {"X-Api-Request-Id": str(uuid4())}
        if profile.options.auth_mode == "api_key":
            headers["X-Api-Key"] = profile.api_key
        else:
            if not profile.options.app_id:
                raise TTSError("Access Token 鉴权需要 App ID")
            # V3 复刻接口使用 App-Key，合成接口使用 App-Id，字段按各自协议发送。
            headers.update({"X-Api-App-Key": profile.options.app_id, "X-Api-Access-Key": profile.api_key})
        return headers

    @staticmethod
    def speaker_payload(profile: Profile, voice_id: str) -> Dict[str, str]:
        if profile.options.clone_billing == "postpaid":
            if (
                not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{6,254}[A-Za-z0-9]", voice_id)
                or RESERVED_SPEAKER.search(voice_id)
            ):
                raise TTSError("后付费音色 ID 使用 8–256 位字母、数字、下划线或连字符，以字母开头、字母或数字结尾")
            return {"speaker_id": "custom_speaker_id", "custom_speaker_id": voice_id}
        if not re.fullmatch(r"S_[A-Za-z0-9_-]+", voice_id):
            raise TTSError("请填写控制台提供的预付费音色槽位 ID（S_ 开头），或选择后付费音色")
        return {"speaker_id": voice_id}

    @staticmethod
    def clone_result(result: Dict[str, Any], voice_id: str, resource: str = "seed-icl-2.0") -> CloneResult:
        states = {1: "training", 2: "ready", 3: "failed", 4: "ready"}
        status = result.get("status")
        if status == 0:
            raise TTSError("火山引擎尚未找到此音色的训练记录，请核对音色 ID、账号和区域")
        if type(status) is not int or status not in states:
            raise TTSError("火山引擎复刻响应缺少有效训练状态 status")
        returned_id = result.get("custom_speaker_id") or result.get("speaker_id")
        if returned_id not in (voice_id, "custom_speaker_id"):
            raise TTSError("火山引擎复刻响应返回了不匹配的音色 ID")
        remaining = result.get("available_training_times")
        if remaining is not None and (type(remaining) is not int or remaining < 0):
            raise TTSError("火山音色响应的剩余次数无效")
        entries = result.get("speaker_status", [])
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise TTSError("火山音色响应的版本列表无效")
        model_types: List[int] = []
        previews: Dict[int, str] = {}
        for item in entries:
            model_type = item.get("model_type")
            if type(model_type) is not int:
                raise TTSError("火山音色响应缺少有效的 model_type")
            model_types.append(model_type)
            if item.get("demo_audio"):
                validate_url(item["demo_audio"])
                previews[model_type] = item["demo_audio"]
        preview_url = result.get("demo_audio", "")
        if preview_url:
            validate_url(preview_url)
        else:
            versions = (5, 4) if resource == "seed-icl-2.0" else (1, 2, 3)
            preview_url = next((previews[version] for version in versions if version in previews), "")
        return CloneResult(
            voice_id, states[status], preview_url=preview_url,
            available_training_times=remaining, model_types=tuple(model_types),
        )

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> CloneResult:
        voice_id = profile.options.clone_speaker_id.strip()
        payload: Dict[str, Any] = self.speaker_payload(profile, voice_id)
        if profile.options.clone_resource_id not in CLONE_RESOURCES:
            raise TTSError("请为复刻音色选择声音复刻资源 ID（seed-icl 系列）")
        data = await self.reference_bytes(profile, url, limit=10 * 1024 * 1024, allow_extended=True)
        encoded = await asyncio.to_thread(lambda: base64.b64encode(data).decode("ascii"))
        payload.update(
            audio={"data": encoded, "format": volc_reference_format(data, url or profile.options.reference_file)},
            language=profile.options.clone_language,
        )
        if text.strip():
            payload["text"] = text.strip()
        extra: Dict[str, Any] = {
            "enable_audio_denoise": profile.options.volc_clone_denoise,
            "disable_volume_normalization": not profile.options.volc_clone_normalize,
        }
        demo_text = profile.options.volc_clone_demo_text.strip()
        if demo_text:
            if len(demo_text) < 4:
                raise TTSError("火山复刻的试听文本需要 4–300 字符")
            extra["demo_text"] = demo_text
        payload["extra_params"] = extra
        result = await self.http.json("POST", self.url(profile, "clone"), self.clone_headers(profile), payload)
        return self.clone_result(result, voice_id, profile.options.clone_resource_id)

    async def design(self, profile: Profile, name: str) -> CloneResult:
        options = profile.options
        voice_id = options.clone_speaker_id.strip()
        if not re.fullmatch(r"S_[A-Za-z0-9_-]+", voice_id):
            raise TTSError("音色设计请填写控制台已购买的 S_ 开头音色槽位")
        if options.clone_resource_id not in CLONE_RESOURCES:
            raise TTSError("设计音色请绑定对应的 ICL 合成资源")
        prompt: Dict[str, Any] = {}
        if options.voice_prompt.strip():
            if len(options.voice_prompt.strip()) > 200:
                raise TTSError("火山音色设计的文字描述最多 200 字符")
            prompt["text_prompt"] = options.voice_prompt.strip()
        if options.volc_design_image_url:
            validate_url(options.volc_design_image_url)
            prompt["image_prompt"] = {"image_url": options.volc_design_image_url}
        if not prompt:
            raise TTSError("请填写音色设计描述或参考图片 URL")
        if not 1 <= len(options.design_preview_text.strip()) <= 300:
            raise TTSError("火山音色设计的试听文本需要 1–300 字符")
        payload = {
            "speaker_id": voice_id, "prompt": prompt,
            "text": options.design_preview_text.strip(),
            "language": 0 if options.design_language == "zh" else 1,
        }
        result = await self.http.json(
            "POST", self.management_url(profile, "voice_design"), self.clone_headers(profile), payload,
        )
        # 创建结果先交给工作台保存；试听单独读取，下载失败不会让用户重复设计同一槽位。
        return self.clone_result(result, voice_id, options.clone_resource_id)

    def management_url(self, profile: Profile, operation: str) -> str:
        if not profile.base_url:
            raise TTSError("火山音色管理请填写 BaseURL，接口需要与合成服务位于同一区域")
        return api_url(profile.base_url, self.api_prefix, f"tts/{operation}")

    async def clone_status(self, profile: Profile, voice_id: str) -> CloneResult:
        # 预付费槽位固定使用 S_ 前缀，后付费命名规则排除此类前缀。
        status_profile = profile.model_copy(deep=True)
        status_profile.options.clone_billing = "prepaid" if voice_id.startswith("S_") else "postpaid"
        result = await self.http.json(
            "POST",
            self.management_url(profile, "get_voice"),
            self.clone_headers(profile),
            self.speaker_payload(status_profile, voice_id),
        )
        return self.clone_result(result, voice_id, profile.options.clone_resource_id)

    async def voice_preview(self, profile: Profile, voice_id: str) -> Audio:
        result = await self.clone_status(profile, voice_id)
        if result.state != "ready" or not result.preview_url:
            raise TTSError("此音色暂未提供可用的平台试听，请先查询状态")
        data = await self.http.download(result.preview_url)
        # 平台 demo 通常为 MP3；normalize_audio 同时识别 WAV、Opus 等容器标识。
        return await normalize_audio(data, profile.model_copy(update={"response_format": "mp3"}))

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        if profile.options.latex_read and profile.options.enable_ssml:
            raise TTSError("LaTeX 与 SSML 需要不同的 Markdown 解析参数，请分别启用")
        request_id = str(uuid4())
        headers = {"X-Api-Resource-Id": profile.options.resource_id, "X-Api-Request-Id": request_id}
        if profile.options.auth_mode == "api_key":
            headers["X-Api-Key"] = profile.api_key
        else:
            if not profile.options.app_id:
                raise TTSError("Access Token 鉴权需要 App ID")
            headers.update({"X-Api-App-Id": profile.options.app_id, "X-Api-Access-Key": profile.api_key})
        params: Dict[str, Any] = {
            "text": text,
            "speaker": profile.voice,
            "audio_params": {
                "format": "ogg_opus" if profile.response_format == "opus" else profile.response_format,
                "sample_rate": profile.options.sample_rate,
                "speech_rate": round((profile.speed - 1) * 100),
                "loudness_rate": profile.options.volc_loudness_rate,
            },
        }
        if profile.options.volc_bit_rate:
            params["audio_params"]["bit_rate"] = profile.options.volc_bit_rate
        if profile.options.volc_emotion:
            params["audio_params"]["emotion"] = profile.options.volc_emotion
            params["audio_params"]["emotion_scale"] = profile.options.volc_emotion_scale
        model = synthesis_model(profile.model, profile.options.resource_id, profile.instructions)
        if model:
            params["model"] = model
        additions: Dict[str, Any] = {}
        if profile.instructions:
            if not supports_instructions(profile.model, profile.options.resource_id):
                raise TTSError("当前火山资源或模型不支持语音指令，ICL 2.0 请使用 expressive 效果版本")
            if profile.options.volc_instruction_mode == "cot":
                if profile.options.resource_id != "seed-icl-2.0" or model != EXPRESSIVE_MODEL:
                    raise TTSError("COT 标签仅支持 ICL 2.0 的 expressive 效果版本")
                params["text"] = cot_text(text, profile.instructions)
                additions["use_tag_parser"] = True
            else:
                additions["context_texts"] = [profile.instructions]
        if profile.options.volc_bit_rate and profile.options.volc_bit_rate < 64000:
            additions["disable_default_bit_rate"] = True
        if profile.options.volc_pitch:
            additions["post_process"] = {"pitch": profile.options.volc_pitch}
        if profile.options.volc_language:
            additions["explicit_language"] = profile.options.volc_language
        if profile.options.volc_dialect:
            additions["explicit_dialect"] = profile.options.volc_dialect
        if profile.options.volc_silence_duration:
            additions["silence_duration"] = profile.options.volc_silence_duration
        if profile.options.volc_tone_fidelity:
            if profile.options.resource_id != "seed-icl-2.0":
                raise TTSError("还原模式只适用于 ICL 2.0 复刻音色")
            additions["tone_fidelity"] = True
        if profile.options.aigc_watermark:
            additions["aigc_watermark"] = True
        if profile.options.latex_read:
            additions["latex_parser"] = "v2"
            additions["disable_markdown_filter"] = True
        if profile.options.pronunciation_tone:
            additions["pronunciation_dict"] = {"tone": profile.options.pronunciation_tone}
        if profile.options.enable_ssml:
            if additions.get("use_tag_parser"):
                raise TTSError("SSML 与 COT 标签不能同时启用")
            params["ssml"] = text
            additions["disable_markdown_filter"] = False
        if additions:
            params["additions"] = json.dumps(additions, ensure_ascii=False)
        audio = bytearray()
        finished = False
        async with self.http.session.post(
            self.url(profile),
            json={"user": {"uid": "maimai-tts"}, "req_params": params},
            headers=headers,
            proxy=self.http.proxy,
            allow_redirects=False,
        ) as response:
            await self.http.check(response)
            # HTTP chunk 边界与 JSON 行边界无关；按行累计，并限制单行和总解码大小。
            pending = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                pending.extend(chunk)
                if len(pending) > self.http.limit * 2:
                    raise TTSError("火山响应行超过大小上限")
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    if line.strip():
                        finished = await asyncio.to_thread(self._consume, line, audio) or finished
            if pending.strip():
                finished = await asyncio.to_thread(self._consume, pending, audio) or finished
            request_id = response.headers.get("X-Tt-Logid", request_id)
        if not finished:
            raise TTSError("火山引擎流未返回完成状态，拒绝发送不完整音频")
        return await normalize_audio(bytes(audio), profile, request_id)

    def _consume(self, line: bytes, audio: bytearray) -> bool:
        if line.startswith((b"event:", b":", b"id:")):
            return False
        if line.startswith(b"data:"):
            line = line[5:].strip()
        item = json.loads(line)
        if not isinstance(item, dict):
            raise TTSError("火山引擎音频事件应为 JSON 对象")
        code = item.get("code")
        if type(code) is not int or code not in (0, 20000000):
            raise TTSError(f"火山引擎错误 {code}：{str(item.get('message', ''))[:500]}")
        if item.get("data"):
            chunk = decode_audio(item["data"], self.http.limit)
            if len(audio) + len(chunk) > self.http.limit:
                raise TTSError("火山引擎音频超过大小上限")
            audio.extend(chunk)
        return code == 20000000
