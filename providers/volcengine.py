"""火山引擎 V3 HTTP 单向流式协议。"""

from typing import Any, Dict
from uuid import uuid4

import asyncio
import base64
import json
import re

from ..domain.models import Audio, Profile, TTSError
from ..infrastructure.reference_audio import reference_mime
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
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{6,254}[A-Za-z0-9]", voice_id):
                raise TTSError("后付费音色 ID 使用 8–256 位字母、数字、下划线或连字符，以字母开头、字母或数字结尾")
            return {"speaker_id": "custom_speaker_id", "custom_speaker_id": voice_id}
        if not re.fullmatch(r"S_[A-Za-z0-9_-]+", voice_id):
            raise TTSError("请填写控制台提供的预付费音色槽位 ID（S_ 开头），或选择后付费音色")
        return {"speaker_id": voice_id}

    @staticmethod
    def clone_result(result: Dict[str, Any], voice_id: str) -> CloneResult:
        states = {1: "training", 2: "ready", 3: "failed", 4: "ready"}
        status = result.get("status")
        if status == 0:
            raise TTSError("火山引擎尚未找到此音色的训练记录，请核对音色 ID、账号和区域")
        if type(status) is not int or status not in states:
            raise TTSError("火山引擎复刻响应缺少有效训练状态 status")
        returned_id = result.get("custom_speaker_id") or result.get("speaker_id")
        if returned_id not in (voice_id, "custom_speaker_id"):
            raise TTSError("火山引擎复刻响应返回了不匹配的音色 ID")
        return CloneResult(voice_id, states[status])

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> CloneResult:
        voice_id = profile.options.clone_speaker_id.strip()
        payload: Dict[str, Any] = self.speaker_payload(profile, voice_id)
        if profile.options.clone_resource_id not in {"seed-icl-1.0", "seed-icl-1.0-concurr", "seed-icl-2.0"}:
            raise TTSError("请为复刻音色选择声音复刻资源 ID（seed-icl 系列）")
        data = await self.reference_bytes(profile, url)
        encoded = await asyncio.to_thread(lambda: base64.b64encode(data).decode("ascii"))
        payload.update(
            audio={"data": encoded, "format": "wav" if reference_mime(data) == "audio/wav" else "mp3"},
            language=profile.options.clone_language,
        )
        if text.strip():
            payload["text"] = text.strip()
        result = await self.http.json("POST", self.url(profile, "clone"), self.clone_headers(profile), payload)
        return self.clone_result(result, voice_id)

    async def clone_status(self, profile: Profile, voice_id: str) -> CloneResult:
        if not profile.base_url:
            raise TTSError("查询训练状态请填写平台 BaseURL，接口使用同一区域的 tts/get_voice")
        # 预付费槽位固定使用 S_ 前缀，后付费命名规则排除此类前缀。
        status_profile = profile.model_copy(deep=True)
        status_profile.options.clone_billing = "prepaid" if voice_id.startswith("S_") else "postpaid"
        result = await self.http.json(
            "POST",
            api_url(profile.base_url, self.api_prefix, "tts/get_voice"),
            self.clone_headers(profile),
            self.speaker_payload(status_profile, voice_id),
        )
        return self.clone_result(result, voice_id)

    async def synthesize(self, profile: Profile, text: str) -> Audio:
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
        if profile.model:
            params["model"] = profile.model
        if profile.instructions:
            if profile.options.resource_id.startswith("seed-icl-") and profile.model:
                raise TTSError("火山引擎复刻音色指定模型版本时不支持风格指令，请在模型或音色设置中清空风格描述")
            params["additions"] = json.dumps({"context_texts": [profile.instructions]}, ensure_ascii=False)
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
