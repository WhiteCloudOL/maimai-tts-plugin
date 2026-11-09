"""Breeze-TTS-2.cpp 原版及 WhiteCloudOL 串行排队、Bearer 鉴权版。"""

from aiohttp import ClientResponse, FormData
from typing import Any, Dict, List
from urllib.parse import quote
from uuid import uuid4

import asyncio
import json

from ..domain.breeze import CPP_PARAMETERS
from ..domain.models import Audio, Profile, TTSError
from ..infrastructure.reference_codec import reference_wav
from .base import CloneResult, Provider, normalize_audio
from .http_protocol import json_value


class BreezeLocalProvider(Provider):
    """通过原生表单合成，复用实例串行调度、PCM 接收及服务端音色库。

    两个服务端使用相同请求格式；鉴权头随 API Key 配置发送，服务端 FIFO
    等待包含在平台请求预算中。工作台在音色注册和移除后更新目录缓存。
    """

    voices_path = "voices"
    clone_path = "voices"

    async def reference_wav(self, profile: Profile, source: str = "") -> bytes:
        data = await self.reference_bytes(profile, source)
        return await asyncio.to_thread(reference_wav, data, 7 * 1024 * 1024)

    async def speech_form(self, profile: Profile, text: str) -> FormData:
        options = profile.options
        form = FormData()
        form.add_field("text", text)
        form.add_field("cfg_scale", str(options.breeze_guidance_scale))
        form.add_field("seed", str(options.seed if options.seed is not None else 42))
        values = options.model_dump()
        for option, field in CPP_PARAMETERS.items():
            value = values[option]
            if value is not None:
                form.add_field(field, str(value))
        if profile.instructions:
            form.add_field("instruction", profile.instructions)
        if options.breeze_mode == "saved":
            if not profile.voice:
                raise TTSError("请填写 Breeze C++ 服务端音色 ID，或从服务端音色目录同步")
            form.add_field("voice_id", profile.voice)
        elif options.breeze_mode == "reference":
            if not options.reference_text.strip():
                raise TTSError("Breeze C++ 参考录音需要准确的文字稿")
            form.add_field("ref_audio", await self.reference_wav(profile), filename="reference.wav", content_type="audio/wav")
            form.add_field("ref_text", options.reference_text.strip())
        elif options.breeze_mode == "design" and not profile.instructions.strip():
            raise TTSError("Breeze C++ 声音设计请填写风格描述")
        return form

    async def check_response(self, response: ClientResponse) -> None:
        if response.status == 401:
            raise TTSError("Breeze C++ 鉴权失败（HTTP 401），请在平台 API Key 中填写有效的 Bearer 密钥")
        if response.status == 409:
            raise TTSError("Breeze C++ 服务端并发槽位已满（HTTP 409），请等待当前任务完成后再调用")
        await self.http.check(response)

    async def voices(self, profile: Profile) -> List[Dict[str, str]]:
        async with self.http.session.get(self.url(profile, "voices"), headers=self.headers(profile),
                                        proxy=self.http.proxy, allow_redirects=False) as response:
            await self.check_response(response)
            values = await json_value(await self.http.read(response))
        if not isinstance(values, list):
            raise TTSError("Breeze C++ 音色目录应返回 JSON 数组")
        voices = []
        for item in values[:1000]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise TTSError("Breeze C++ 音色目录条目缺少有效 id")
            metadata = item.get("metadata", {})
            name = metadata.get("name", "") if isinstance(metadata, dict) else ""
            name = name or item.get("description") or item["id"]
            if not isinstance(name, str):
                raise TTSError("Breeze C++ 音色名称应为字符串")
            voices.append({"id": item["id"], "name": name[:80], "model": profile.model})
        return voices

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> CloneResult:
        if not text.strip():
            raise TTSError("创建 Breeze C++ 服务端音色请填写准确的录音文字稿")
        form = FormData()
        form.add_field("name", profile.options.breeze_cpp_voice_id or "mmtts_" + uuid4().hex)
        form.add_field("metadata", json.dumps({"name": name}, ensure_ascii=False))
        form.add_field("ref_text", text.strip())
        form.add_field("ref_audio", await self.reference_wav(profile, url), filename="reference.wav", content_type="audio/wav")
        # 编码参考录音与合成共用 GPU，同一实例的管理任务沿用串行名额。
        async with self.http.request_slot(self.url(profile)):
            async with self.http.session.post(self.url(profile, "clone"), headers=self.headers(profile), data=form,
                                             proxy=self.http.proxy, allow_redirects=False) as response:
                await self.check_response(response)
                result = await json_value(await self.http.read(response))
        if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
            raise TTSError("Breeze C++ 创建音色响应缺少有效 id")
        return CloneResult(result["id"], "ready", name=name)

    def deletion_parameters(self, profile: Profile) -> Dict[str, Any]:
        # 同一服务实例的多把 Bearer 密钥共用一份音色库。
        return {"kind": "breeze_cpp", "url": self.url(profile, "clone")}

    async def delete_voice(self, profile: Profile, voice_id: str) -> None:
        async with self.http.session.delete(self.url(profile, "clone").rstrip("/") + "/" + quote(voice_id, safe=""),
                                           headers=self.headers(profile), proxy=self.http.proxy, allow_redirects=False) as response:
            await self.check_response(response)
            result = await json_value(await self.http.read(response))
        if not isinstance(result, dict) or result.get("deleted") != voice_id:
            raise TTSError("Breeze C++ 移除音色响应缺少对应的 deleted 音色 ID")

    async def models(self, profile: Profile) -> List[str]:
        raise TTSError("自托管 Breeze 由服务端加载模型，请手动添加模型标识，并为不同实例填写对应 BaseURL")

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        if profile.speed != 1:
            raise TTSError("Breeze 自托管语速倍率保持 1，请用风格描述控制朗读节奏")
        form = await self.speech_form(profile, text)
        slot = self.http.request_slot(self.url(profile))
        if slot.locked() and self.http.log is not None:
            self.http.log.debug("等待 Breeze 实例空闲", provider_id=profile.id, model=profile.model)
        async with (
            slot,
            self.http.session.post(
                self.url(profile),
                data=form,
                headers=self.headers(profile),
                proxy=self.http.proxy,
                allow_redirects=False,
            ) as response,
        ):
            await self.check_response(response)
            if response.content_type not in {"audio/pcm", "application/octet-stream", "binary/octet-stream"}:
                raise TTSError("Breeze 自托管接口应返回 audio/pcm，请检查接入方式与服务地址")
            if response.headers.get("X-Sample-Format", "s16le") != "s16le":
                raise TTSError("Breeze 返回的 PCM 采样格式应为 s16le")
            rate = response.headers.get("X-Sample-Rate", "24000")
            if not rate.isascii() or not rate.isdigit() or not 8000 <= int(rate) <= 48000:
                raise TTSError("Breeze 返回了无效的 X-Sample-Rate")
            data = await self.http.read(response)
            request_id = response.headers.get("X-Request-Id", "")
        audio_profile = profile.model_copy(deep=True, update={"response_format": "pcm"})
        audio_profile.options.sample_rate = int(rate)
        return await normalize_audio(data, audio_profile, request_id)
