"""Breeze 官方在线服务：合成、后台任务、音色目录及预览保存。"""

from aiohttp import FormData
from typing import Any, Dict, List
from urllib.parse import quote, urlsplit, urlunsplit

import asyncio

from ..domain.models import Audio, Profile, TTSError
from ..infrastructure.reference_audio import reference_mime
from .base import CloneResult, Provider, decode_audio, normalize_audio
from .endpoints import api_url
from .http_protocol import json_value


class BreezeCloudProvider(Provider):
    voices_path = "voices"
    clone_path = "voice-previews/clone"

    def operation_url(self, profile: Profile, path: str) -> str:
        if not profile.base_url:
            raise TTSError("Breeze 在线服务请填写 BaseURL，例如 https://api.breeze.blue/v1")
        return api_url(profile.base_url, "/v1", path)

    def url(self, profile: Profile, operation: str = "speech", *, websocket: bool = False, model: str = "") -> str:
        if operation != "speech" or not profile.base_url:
            return super().url(profile, operation, websocket=websocket, model=model)
        path = "text-to-speech/" + quote(profile.voice, safe="")
        if profile.options.breeze_transport == "stream":
            path += "/stream"
        return self.operation_url(profile, path)

    def deletion_parameters(self, profile: Profile) -> Dict[str, Any]:
        return {"url": self.url(profile, "voices"), "headers": self.headers(profile)}

    def list_cache_parameters(self, profile: Profile, operation: str) -> Dict[str, Any]:
        values = super().list_cache_parameters(profile, operation)
        values["model"] = ""
        if operation == "voices":
            values["voice_type"] = profile.options.breeze_voice_type
        return values

    async def models(self, profile: Profile) -> List[str]:
        async with self.http.session.get(
            self.url(profile, "models"), headers=self.headers(profile), proxy=self.http.proxy, allow_redirects=False
        ) as response:
            raw = await self.http.read(response, 1024 * 1024)
        values = await json_value(raw)
        if not isinstance(values, list) or len(values) > 1000:
            raise TTSError("Breeze 在线模型列表应为 JSON 数组")
        models = []
        for item in values:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("model_id"), str)
                or not 1 <= len(item["model_id"]) <= 120
            ):
                raise TTSError("Breeze 在线模型列表缺少有效 model_id")
            models.append(item["model_id"])
        return models

    async def voices(self, profile: Profile) -> List[Dict[str, str]]:
        output: List[Dict[str, str]] = []
        token, seen = "", set()
        for page in range(1, 11):
            params = {"page_size": 100, "voice_type": profile.options.breeze_voice_type}
            params["next_page_token" if token else "page"] = token if token else page
            async with self.http.session.get(
                self.url(profile, "voices"),
                params=params,
                headers=self.headers(profile),
                proxy=self.http.proxy,
                allow_redirects=False,
            ) as response:
                raw = await self.http.read(response, 1024 * 1024)
            data = await self.http.decode_json(raw, self.headers(profile))
            items = data.get("voices")
            if not isinstance(items, list) or len(items) > 100:
                raise TTSError("Breeze 账号音色响应缺少 voices 数组")
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("voice_id"), str) or not item["voice_id"]:
                    raise TTSError("Breeze 账号音色缺少有效 voice_id")
                voice_id = item["voice_id"]
                name = item.get("name")
                if not isinstance(name, str) or not name or len(voice_id) > 500:
                    raise TTSError("Breeze 账号音色名称或 ID 无效")
                if voice_id in seen:
                    continue
                seen.add(voice_id)
                output.append(
                    {
                        "id": voice_id,
                        "name": name[:80],
                        "model": "",
                        "origin": {"cloned": "clone", "designed": "design"}.get(item.get("origin"), "account"),
                    }
                )
            if type(data.get("has_more")) is not bool:
                raise TTSError("Breeze 音色分页响应缺少布尔 has_more 标记")
            if not data["has_more"]:
                return output
            next_token = data.get("next_page_token")
            if next_token is not None and not isinstance(next_token, str):
                raise TTSError("Breeze 音色分页标记无效")
            if next_token and next_token == token:
                raise TTSError("Breeze 音色分页标记重复，请检查服务端响应")
            token = next_token or ""
        raise TTSError("Breeze 音色目录超过 1000 项，请在平台参数中选择公共或个人目录后重新同步")

    async def audio(self, profile: Profile, path: str, fmt: str = "wav") -> Audio:
        async with self.http.session.get(
            self.operation_url(profile, path),
            params={"output_format": fmt},
            headers=self.headers(profile),
            proxy=self.http.proxy,
            allow_redirects=False,
        ) as response:
            raw = await self.http.read(response)
        return await self.normalize(raw, profile, fmt)

    async def normalize(self, raw: bytes, profile: Profile, fmt: str, request_id: str = "") -> Audio:
        audio_profile = profile.model_copy(deep=True, update={"response_format": fmt})
        audio_profile.options.sample_rate = 24000
        return await normalize_audio(raw, audio_profile, request_id)

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        options = profile.options
        if not profile.voice:
            raise TTSError("Breeze 在线合成需要已保存的音色 ID，请同步账号音色或创建克隆、设计音色")
        if len(text) > options.breeze_max_characters:
            raise TTSError(f"Breeze 在线文本超过当前账号配置的 {options.breeze_max_characters} 字符上限")
        body: Dict[str, Any] = {
            "text": text,
            "voice_settings": {
                "guidance_scale": options.breeze_guidance_scale,
                "speed": profile.speed,
                "volume": options.breeze_volume,
                "use_speaker_boost": options.breeze_speaker_boost,
            },
        }
        if profile.model:
            body["model_id"] = profile.model
        if profile.instructions:
            body["instructions"] = profile.instructions
        if options.breeze_language_code:
            body["language_code"] = options.breeze_language_code
        params = {"output_format": profile.response_format}
        if options.breeze_transport != "stream":
            params["delivery"] = options.breeze_transport
        async with self.http.session.post(
            self.url(profile),
            params=params,
            json=body,
            headers=self.headers(profile),
            proxy=self.http.proxy,
            allow_redirects=False,
        ) as response:
            await self.http.check(response)
            if response.status == 202:
                job = await self.http.decode_json(await self.http.read(response, 65536), self.headers(profile))
            else:
                if not (
                    response.content_type.startswith("audio/") or response.content_type == "application/octet-stream"
                ):
                    raise TTSError("Breeze 合成响应应为音频，或包含后台任务 ID 的 HTTP 202 响应")
                raw = await self.http.read(response)
                return await self.normalize(
                    raw, profile, profile.response_format, response.headers.get("X-Request-Id", "")
                )
        job_id = job.get("generation_job_id")
        if not isinstance(job_id, str) or not job_id or len(job_id) > 200:
            raise TTSError("Breeze 后台合成响应缺少有效 generation_job_id")
        path = "generation-jobs/" + quote(job_id, safe="")
        # 外层 execute 的平台超时覆盖提交、轮询与音频读取；只请求同一 BaseURL 下的受控路径。
        while True:
            data = await self.http.json("GET", self.operation_url(profile, path), self.headers(profile))
            if data.get("generation_job_id") != job_id:
                raise TTSError("Breeze 后台合成返回了不匹配的任务 ID")
            status = data.get("status")
            if status == "ready":
                return await self.audio(profile, path + "/audio", profile.response_format)
            if status not in {"queued", "pending", "running", "processing"}:
                raise TTSError("Breeze 后台合成尚未成功完成，状态：" + str(status))
            await asyncio.sleep(0.5)

    async def save_preview(self, profile: Profile, preview_id: str, name: str) -> str:
        result = await self.http.json(
            "POST",
            self.operation_url(profile, "voice-previews/" + quote(preview_id, safe="") + "/save"),
            self.headers(profile),
            {"voice_name": name, "language_code": profile.options.breeze_voice_language},
        )
        voice_id = result.get("voice_id")
        if not isinstance(voice_id, str) or not voice_id or len(voice_id) > 500:
            raise TTSError("Breeze 保存音色响应缺少有效 voice_id")
        return voice_id

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> CloneResult:
        # 在线克隆由平台转写，ref_text 仅用于自托管接口；files 始终只上传一段录音。
        data = await self.reference_bytes(profile, url, limit=5_000_000)
        mime = reference_mime(data)
        form = FormData()
        form.add_field("name", name)
        form.add_field(
            "files", data, filename="reference.wav" if mime == "audio/wav" else "reference.mp3", content_type=mime
        )
        form.add_field("remove_background_noise", str(profile.options.need_noise_reduction).lower())
        if profile.options.breeze_voice_language == "zh":
            form.add_field("reference_language_code", "zh")
        result = await self.http.multipart(self.url(profile, "clone"), self.headers(profile), form)
        preview_id = result.get("generated_voice_id")
        if not isinstance(preview_id, str) or not preview_id or len(preview_id) > 200:
            raise TTSError("Breeze 克隆预览缺少有效 generated_voice_id")
        if result.get("requires_verification", False) is not False:
            raise TTSError("Breeze 克隆预览需要在官方控制台完成验证；完成后同步账号音色，预览 ID：" + preview_id)
        preview = await self.audio(profile, "voice-previews/" + quote(preview_id, safe="") + "/stream")
        voice_id = await self.save_preview(profile, preview_id, name)
        return CloneResult(voice_id, "ready", preview)

    async def design(self, profile: Profile, name: str) -> CloneResult:
        options = profile.options
        if len(options.voice_prompt.strip()) < 3 or len(options.design_preview_text.strip()) < 3:
            raise TTSError("Breeze 声音设计描述与预览文本需要各填写 3–500 字符")
        body = {
            "voice_description": options.voice_prompt,
            "text": options.design_preview_text,
            "guidance_scale": options.breeze_guidance_scale,
            "preview_count": 1,
        }
        if profile.model:
            body["model_id"] = profile.model
        body["language_code"] = options.breeze_voice_language
        async with self.http.session.post(
            self.operation_url(profile, "voice-previews/design"),
            params={"output_format": "wav"},
            headers=self.headers(profile),
            json=body,
            proxy=self.http.proxy,
            allow_redirects=False,
        ) as response:
            result = await self.http.decode_json(
                await self.http.read(response, self.http.limit * 2), self.headers(profile)
            )
        previews = result.get("previews")
        if not isinstance(previews, list) or len(previews) != 1 or not isinstance(previews[0], dict):
            raise TTSError("Breeze 声音设计响应应包含一个预览")
        item = previews[0]
        preview_id = item.get("generated_voice_id")
        if not isinstance(preview_id, str) or not preview_id or len(preview_id) > 200:
            raise TTSError("Breeze 声音设计预览缺少有效 ID")
        raw = await asyncio.to_thread(decode_audio, item.get("audio_base_64"), self.http.limit)
        preview = await self.normalize(raw, profile, "wav")
        voice_id = await self.save_preview(profile, preview_id, name)
        return CloneResult(voice_id, "ready", preview)

    async def delete_voice(self, profile: Profile, voice_id: str) -> None:
        parsed = urlsplit(self.url(profile, "voices"))
        endpoint = urlunsplit(parsed._replace(path=parsed.path.rstrip("/") + "/" + quote(voice_id, safe="")))
        result = await self.http.json("DELETE", endpoint, self.headers(profile))
        if result.get("status") not in {"ok", "deleted"}:
            raise TTSError("Breeze 删除音色返回了未确认的状态，请同步账号音色核对结果")
