"""OpenAI Speech 原生兼容与可配置的自定义 HTTP 协议。"""

from aiohttp import ClientResponse
from typing import Any, Dict, List

import asyncio

from ..domain.compatibility import CompatibilityOptions, template_variables
from ..domain.models import Audio, Profile, TTSError, validate_url
from ..infrastructure.diagnostics import redact_secrets, trace_scope
from .base import decode_audio, decode_hex, normalize_audio
from .endpoints import api_url
from .http_protocol import at_pointer, json_value, render_template, sse_events
from .openai import OpenAIProvider


class CompatibleProvider(OpenAIProvider):
    @staticmethod
    def protocol(profile: Profile) -> CompatibilityOptions:
        return profile.options.compatibility or CompatibilityOptions()

    @staticmethod
    def private_error(profile: Profile, error: TTSError) -> TTSError:
        return TTSError(redact_secrets(str(error), (profile.api_key,)))

    def url(self, profile: Profile, operation: str = "speech", *, websocket: bool = False, model: str = "") -> str:
        protocol = self.protocol(profile)
        if protocol.mode == "openai" or operation == "clone":
            return super().url(profile, operation, websocket=websocket, model=model)
        path = {"speech": protocol.path, "models": protocol.models_path, "voices": protocol.voices_path}[operation]
        override = {"speech": profile.endpoint, "models": profile.models_url, "voices": profile.voices_url}[operation]
        if (operation != "speech" and override) or not profile.base_url:
            return override
        return api_url(profile.base_url, self.api_prefix, path) if path else ""

    def headers(self, profile: Profile) -> Dict[str, str]:
        protocol = self.protocol(profile)
        headers = super().headers(profile)
        if protocol.mode == "openai":
            return headers
        headers.pop("Authorization", None)
        if profile.api_key:
            if protocol.auth == "bearer":
                headers["Authorization"] = f"Bearer {profile.api_key}"
            elif protocol.auth == "header":
                headers[protocol.auth_header] = protocol.auth_prefix + profile.api_key
        return headers

    def list_cache_parameters(self, profile: Profile, operation: str) -> Dict[str, Any]:
        values = super().list_cache_parameters(profile, operation)
        protocol = self.protocol(profile)
        if protocol.mode == "custom" or (operation == "voices" and profile.voices_url):
            values["headers"].update(
                render_template(
                    protocol.headers, {"api_key": profile.api_key, "model": profile.model, "voice_id": profile.voice}
                )
            )
            fields = {"status_pointer", "success_values", "error_pointer"}
            fields.update(
                {"models_pointer", "model_id_pointer"}
                if operation == "models"
                else {"voices_pointer", "voice_id_pointer", "voice_name_pointer"}
            )
            values["mapping"] = protocol.model_dump(include=fields)
        return values

    async def variables(self, profile: Profile, text: str) -> Dict[str, Any]:
        protocol = self.protocol(profile)
        used = template_variables(protocol.body) | template_variables(protocol.headers)
        values = {
            "text": text,
            "model": profile.model,
            "voice": {"id": profile.voice} if profile.options.custom_voice else profile.voice,
            "voice_id": profile.voice,
            "speed": profile.speed,
            "response_format": profile.response_format,
            "instructions": profile.instructions,
            "sample_rate": profile.options.sample_rate,
            "api_key": profile.api_key,
            "reference_text": profile.options.reference_text,
            "reference_url": profile.options.reference_url,
        }
        if used & {"reference_audio", "reference_base64"}:
            audio = await self.reference_audio(profile)
            values["reference_audio"] = audio
            values["reference_base64"] = audio.split(",", 1)[1]
        if profile.instructions and "instructions" not in used:
            raise TTSError("使用风格或语调时，请在自定义请求模板中引用 {{instructions}}")
        return values

    def check_json(self, payload: Any, protocol: CompatibilityOptions, headers: Dict[str, str]) -> None:
        if protocol.status_pointer:
            status = at_pointer(payload, protocol.status_pointer)
            if not any(type(status) is type(value) and status == value for value in protocol.success_values):
                raise TTSError("自定义接口业务状态未匹配成功值，请检查服务响应与成功状态配置")
        if protocol.error_pointer:
            error = at_pointer(payload, protocol.error_pointer, required=False)
            if error:
                raise TTSError("自定义接口返回错误：" + self.http.sanitize(str(error), headers)[:500])

    async def response_audio(self, response: ClientResponse, profile: Profile, headers: Dict[str, str]) -> Audio:
        await self.http.check(response)
        protocol = self.protocol(profile)
        if protocol.response == "sse_base64":
            data = await self.custom_sse(response, profile, headers)
        elif protocol.response == "binary":
            media = response.content_type
            if not (
                media.startswith("audio/")
                or media in {"application/octet-stream", "binary/octet-stream", "application/ogg"}
            ):
                raise TTSError(f"自定义接口返回 {media}，请按实际结果选择 JSON 或音频链接响应类型")
            data = await self.http.read(response)
        else:
            raw = await self.http.read(response, self.http.limit * 2 + 1024 * 1024)
            if protocol.response == "text_url":
                try:
                    value = raw.decode("utf-8").strip()
                except UnicodeDecodeError:
                    raise TTSError("音频链接响应应为 UTF-8 文本") from None
            else:
                payload = await json_value(raw)
                self.check_json(payload, protocol, headers)
                value = at_pointer(payload, protocol.audio_pointer)
            if not isinstance(value, str) or not value:
                raise TTSError("配置的音频响应字段应为非空字符串")
            if protocol.response in {"json_url", "text_url"}:
                data = await self.http.download(validate_url(value))
            elif protocol.response == "json_hex":
                data = await asyncio.to_thread(decode_hex, value, self.http.limit)
            else:
                if value.startswith("data:"):
                    metadata, separator, value = value.partition(",")
                    if not separator or not metadata.startswith("data:audio/") or not metadata.endswith(";base64"):
                        raise TTSError("音频 Data URI 需要使用 data:audio/...;base64 格式")
                data = await asyncio.to_thread(decode_audio, value, self.http.limit)
        return await normalize_audio(data, profile, response.headers.get("x-request-id", ""))

    async def custom_sse(self, response: ClientResponse, profile: Profile, headers: Dict[str, str]) -> bytes:
        await self.http.check(response)
        if response.content_type != "text/event-stream":
            raise TTSError("自定义 SSE 接口应返回 text/event-stream")
        protocol = self.protocol(profile)
        output = bytearray()
        done = False
        async for event, raw in sse_events(response, self.http.limit * 2 + 1024 * 1024):
            if raw == "[DONE]" and protocol.sse_done_event == "[DONE]":
                done = True
                break
            payload = await json_value(raw.encode("utf-8"))
            self.check_json(payload, protocol, headers)
            event_type = (
                at_pointer(payload, protocol.sse_type_pointer, required=False) if protocol.sse_type_pointer else event
            )
            if event_type is None:
                event_type = event
            if event_type == protocol.sse_done_event:
                done = True
                break
            if event_type != protocol.sse_audio_event:
                continue
            value = at_pointer(payload, protocol.audio_pointer)
            chunk = await asyncio.to_thread(decode_audio, value, self.http.limit)
            if len(output) + len(chunk) > self.http.limit:
                raise TTSError("自定义 SSE 音频超过大小上限")
            output.extend(chunk)
        if not done or not output:
            raise TTSError("自定义 SSE 尚未收到完成事件或返回空音频")
        return bytes(output)

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        if self.protocol(profile).mode == "openai":
            return await super().synthesize(profile, text)
        protocol = self.protocol(profile)
        values = await self.variables(profile, text)
        if self.http.log is not None:
            self.http.log.debug(
                "自定义 HTTP 协议",
                method=protocol.method,
                encoding=protocol.body_encoding,
                response_type=protocol.response,
                provider_id=profile.id,
                model=profile.model,
            )
        headers = self.headers(profile)
        headers.update(render_template(protocol.headers, values))
        body = render_template(protocol.body, values)
        if (protocol.method == "GET" or protocol.body_encoding == "form") and any(
            isinstance(v, (dict, list)) for v in body.values()
        ):
            raise TTSError("GET / 表单请求需要单层字段，自定义音色请使用 {{voice_id}} 变量")
        if protocol.method == "GET" or protocol.body_encoding == "form":
            body = {
                key: "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
                for key, value in body.items()
            }
        args = (
            {"params": body}
            if protocol.method == "GET"
            else {"json" if protocol.body_encoding == "json" else "data": body}
        )
        with trace_scope((profile.api_key, text)):
            try:
                async with self.http.session.request(
                    protocol.method,
                    self.url(profile),
                    headers=headers,
                    proxy=self.http.proxy,
                    allow_redirects=False,
                    **args,
                ) as response:
                    return await self.response_audio(response, profile, headers)
            except TTSError as exc:
                raise self.private_error(profile, exc) from None

    async def listing(self, profile: Profile, operation: str) -> List[Dict[str, str]]:
        protocol = self.protocol(profile)
        url = self.url(profile, operation)
        if not url:
            raise TTSError("请填写此服务的列表接口路径，或直接添加模型与音色 ID")
        headers = self.headers(profile)
        headers.update(
            render_template(
                protocol.headers, {"api_key": profile.api_key, "model": profile.model, "voice_id": profile.voice}
            )
        )
        with trace_scope((profile.api_key,)):
            try:
                async with self.http.session.get(
                    url, headers=headers, proxy=self.http.proxy, allow_redirects=False
                ) as response:
                    raw = await self.http.read(response, 1024 * 1024)
                payload = await json_value(raw)
                self.check_json(payload, protocol, headers)
            except TTSError as exc:
                raise self.private_error(profile, exc) from None
        pointer = protocol.models_pointer if operation == "models" else protocol.voices_pointer
        id_pointer = protocol.model_id_pointer if operation == "models" else protocol.voice_id_pointer
        items = at_pointer(payload, pointer)
        if not isinstance(items, list):
            raise TTSError("列表响应字段应为 JSON 数组")
        output = []
        for item in items[:200]:
            value = at_pointer(item, id_pointer)
            if not isinstance(value, str) or not value or len(value) > (200 if operation == "models" else 500):
                raise TTSError("列表中的 ID 应为有效字符串，请核对 ID 字段路径")
            name = at_pointer(item, protocol.voice_name_pointer, required=False) if operation == "voices" else None
            output.append(
                {
                    "id": value,
                    "name": name[:80] if isinstance(name, str) and name else value[:80],
                    "model": profile.model,
                }
            )
        return output

    async def models(self, profile: Profile) -> List[str]:
        if self.protocol(profile).mode == "openai":
            return await super().models(profile)
        return [item["id"] for item in await self.listing(profile, "models")]

    async def voices(self, profile: Profile) -> List[Dict[str, str]]:
        if self.protocol(profile).mode == "openai" and not profile.voices_url:
            return await super().voices(profile)
        return await self.listing(profile, "voices")

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> str:
        if self.protocol(profile).mode != "openai":
            raise TTSError("自定义协议请使用已有音色 ID，OpenAI 标准模式支持官方音色创建接口")
        return await super().clone(profile, name, url, text)

    async def delete_voice(self, profile: Profile, voice_id: str) -> None:
        if self.protocol(profile).mode != "openai":
            raise TTSError("云端音色删除使用 OpenAI 标准模式，请按自定义服务文档管理其账号音色")
        await super().delete_voice(profile, voice_id)
