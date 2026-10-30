"""Breeze 官方开源服务：multipart 参考录音、文字设计与 PCM 流。"""

from aiohttp import FormData
from typing import List

from ..domain.models import Audio, Profile, TTSError
from ..infrastructure.reference_audio import reference_mime
from .base import Provider, normalize_audio


class BreezeLocalProvider(Provider):
    async def models(self, profile: Profile) -> List[str]:
        raise TTSError("自托管 Breeze 由服务端加载模型，请手动添加模型标识，并为不同实例填写对应 BaseURL")

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        options = profile.options
        if profile.speed != 1:
            raise TTSError("Breeze 自托管语速倍率保持 1，请用风格描述控制朗读节奏")
        if not profile.instructions.strip() and options.breeze_guidance_scale != 1:
            raise TTSError("Breeze 纯音色复刻与普通朗读使用 CFG=1；风格指令或声音设计可使用更高 CFG")
        form = FormData()
        form.add_field("text", text)
        form.add_field("cfg_scale", str(options.breeze_guidance_scale))
        form.add_field("seed", str(options.seed if options.seed is not None else 42))
        if profile.instructions:
            form.add_field("instruction", profile.instructions)
        if options.breeze_mode == "reference":
            if not options.reference_text.strip():
                raise TTSError("Breeze 参考音色需要同时填写参考录音与准确的录音文字稿")
            data = await self.reference_bytes(profile)
            mime = reference_mime(data)
            form.add_field(
                "ref_audio",
                data,
                filename="reference.wav" if mime == "audio/wav" else "reference.mp3",
                content_type=mime,
            )
            form.add_field("ref_text", options.reference_text.strip())
        elif options.breeze_mode == "design" and not profile.instructions.strip():
            raise TTSError("Breeze 声音设计需要填写声音或风格描述")
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
            await self.http.check(response)
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
