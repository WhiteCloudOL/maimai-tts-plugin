"""MiniMax 官方 TTS：HTTP、SSE、WebSocket、文件复刻、声音设计和音色管理。"""

from aiohttp import FormData, WSMsgType
from typing import Any, Dict, List
from urllib.parse import urlsplit, urlunsplit

import asyncio
import re

from ..domain.models import Audio, Profile, TTSError, validate_url
from ..infrastructure.minimax_audio import recording_info, telephony_wav
from .base import CloneResult, Provider, decode_hex, normalize_audio
from .endpoints import api_url
from .http_protocol import json_value, sse_events
from .minimax_protocol import account_voices, speech_payload


class MiniMaxProvider(Provider):
    speech_path = "t2a_v2"
    models_path = "models"
    voices_path = "get_voice"
    clone_path = "voice_clone"

    def check_result(self, result: Dict[str, Any], profile: Profile) -> Dict[str, Any]:
        if not isinstance(result, dict):
            raise TTSError("MiniMax 响应应为 JSON 对象")
        status = result.get("base_resp")
        if not isinstance(status, dict) or type(status.get("status_code")) is not int:
            raise TTSError("MiniMax 响应缺少有效 base_resp.status_code")
        if status["status_code"] != 0:
            detail = self.http.sanitize(str(status.get("status_msg", "请求失败")), self.headers(profile))[:500]
            raise TTSError(f"MiniMax 接口错误（{status['status_code']}）：{detail}")
        return result

    def operation_url(self, profile: Profile, path: str) -> str:
        if not profile.base_url:
            raise TTSError("MiniMax 音色管理请填写平台 BaseURL，例如 https://api.minimax.cn/v1")
        return api_url(profile.base_url, "/v1", path)

    async def request(self, profile: Profile, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        endpoint = self.url(profile, "clone") if path == "voice_clone" else self.operation_url(profile, path)
        result = await self.http.json("POST", endpoint, self.headers(profile), body)
        return self.check_result(result, profile)

    def list_cache_parameters(self, profile: Profile, operation: str) -> Dict[str, Any]:
        values = super().list_cache_parameters(profile, operation)
        values.pop("model")
        return values

    def deletion_parameters(self, profile: Profile) -> Dict[str, Any]:
        # 仅填写完整合成端点的配置没有管理地址，身份检查本身不发请求、不阻断其他平台。
        return {
            "url": self.operation_url(profile, "delete_voice") if profile.base_url else "",
            "headers": self.headers(profile),
        }

    def payload(self, profile: Profile, text: str) -> Dict[str, Any]:
        payload = speech_payload(profile, text)
        opts = profile.options
        payload["voice_setting"].pop("text_normalization")
        if opts.minimax_normalization:
            payload["text_normalization_mode"] = opts.minimax_normalization
        if opts.minimax_force_cbr:
            payload["audio_setting"]["force_cbr"] = True
        return payload

    async def audio_result(self, profile: Profile, data: bytes, request_id: str = "") -> Audio:
        if profile.response_format.startswith("pcmu_"):
            data = await asyncio.to_thread(telephony_wav, data, profile.response_format, self.http.limit)
            profile = profile.model_copy(update={"response_format": "wav"})
        return await normalize_audio(data, profile, request_id)

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        payload = self.payload(profile, text)
        if profile.options.minimax_transport == "websocket":
            return await self.websocket(profile, payload)
        if profile.options.minimax_transport == "sse":
            return await self.streaming(profile, payload)
        payload["stream"] = False
        result = self.check_result(
            await self.http.json("POST", self.url(profile), self.headers(profile), payload),
            profile,
        )
        data = result.get("data")
        if (
            not isinstance(data, dict)
            or data.get("status") != 2
            or not isinstance(data.get("audio"), str)
            or not data["audio"]
        ):
            raise TTSError("MiniMax 缺少完整合成结果 data.audio / data.status=2")
        binary = (
            await self.http.download(validate_url(data["audio"]))
            if profile.options.minimax_output == "url"
            else await asyncio.to_thread(decode_hex, data["audio"], self.http.limit)
        )
        return await self.audio_result(profile, binary, str(result.get("trace_id", "")))

    async def streaming(self, profile: Profile, payload: Dict[str, Any]) -> Audio:
        # 最终帧明确请求聚合音频，避免把完整音频再次附加到增量片段后造成重复播报。
        payload.update(stream=True, stream_options={"exclude_aggregated_audio": False})
        async with self.http.session.post(
            self.url(profile),
            json=payload,
            headers=self.headers(profile),
            proxy=self.http.proxy,
            allow_redirects=False,
        ) as response:
            await self.http.check(response)
            if response.content_type != "text/event-stream":
                raise TTSError("MiniMax SSE 请求需要 text/event-stream 响应")
            received = 0
            async for _, raw in sse_events(response, self.http.limit * 2 + 1024 * 1024):
                result = self.check_result(await json_value(raw.encode("utf-8")), profile)
                data = result.get("data")
                if not isinstance(data, dict) or type(data.get("status")) is not int or data["status"] not in {1, 2}:
                    raise TTSError("MiniMax SSE 缺少有效音频状态")
                chunk = await asyncio.to_thread(decode_hex, data.get("audio"), self.http.limit)
                if data["status"] == 2:
                    return await self.audio_result(profile, chunk, str(result.get("trace_id", "")))
                received += len(chunk)
                if received > self.http.limit:
                    raise TTSError("MiniMax SSE 音频超过大小上限")
        raise TTSError("MiniMax SSE 在完整合成状态返回前断开")

    def websocket_url(self, profile: Profile) -> str:
        if not profile.base_url and profile.endpoint.startswith(("ws://", "wss://")):
            return profile.endpoint
        parsed = urlsplit(profile.base_url or self.url(profile).rsplit("/", 1)[0])
        root = parsed.path.rstrip("/")
        root = root[:-3] + "/ws/v1" if root.endswith("/v1") else root + "/ws/v1"
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
        return urlunsplit(parsed._replace(scheme=scheme, path=root + "/t2a_v2"))

    async def websocket(self, profile: Profile, payload: Dict[str, Any]) -> Audio:
        text = payload.pop("text")
        for key in ("output_format", "aigc_watermark"):
            payload.pop(key, None)
        payload["event"] = "task_start"
        audio, started, finished, request_id = bytearray(), False, False, ""
        async with self.http.session.ws_connect(
            self.websocket_url(profile),
            headers=self.headers(profile),
            proxy=self.http.proxy,
            max_msg_size=self.http.limit * 2 + 1024 * 1024,
        ) as socket:
            async for message in socket:
                if message.type != WSMsgType.TEXT:
                    if message.type == WSMsgType.ERROR:
                        raise TTSError("MiniMax WebSocket 连接错误")
                    continue
                result = await json_value(message.data.encode("utf-8"))
                if not isinstance(result, dict):
                    raise TTSError("MiniMax WebSocket 消息应为对象")
                if "base_resp" in result:
                    self.check_result(result, profile)
                event = result.get("event")
                request_id = str(result.get("trace_id", request_id))
                if event == "connection_success":
                    await socket.send_json(payload)
                elif event == "task_started":
                    if started:
                        raise TTSError("MiniMax 重复开始合成任务")
                    started = True
                    await socket.send_json({"event": "task_continue", "text": text})
                    await socket.send_json({"event": "task_finish"})
                elif event == "task_continued":
                    if not started:
                        raise TTSError("MiniMax 在任务开始前返回音频")
                    data = result.get("data", {})
                    if isinstance(data, dict) and data.get("audio"):
                        chunk = await asyncio.to_thread(decode_hex, data["audio"], self.http.limit)
                        if len(audio) + len(chunk) > self.http.limit:
                            raise TTSError("MiniMax WebSocket 音频超过大小上限")
                        audio.extend(chunk)
                    finished = result.get("is_final") is True or finished
                elif event == "task_finished":
                    if not started or not finished or not audio:
                        raise TTSError("MiniMax 任务结束但缺少完整音频")
                    return await self.audio_result(profile, bytes(audio), request_id)
                elif event in {"task_failed", "error"}:
                    raise TTSError("MiniMax WebSocket 合成任务失败")
        raise TTSError("MiniMax WebSocket 在任务完成前断开")

    @staticmethod
    def voice_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{6,254}[A-Za-z0-9]", value):
            raise TTSError("MiniMax 自定义音色 ID 使用 8–256 位字母、数字、_ 或 -，以字母开头、字母或数字结尾")
        return value

    async def upload(self, profile: Profile, source: str, purpose: str) -> int:
        if source.startswith(("https://", "http://")):
            data = await self.http.download(validate_url(source), 20 * 1024 * 1024)
        else:
            data = await self.http.reference_files.read(source, 20 * 1024 * 1024, allow_m4a=True)
        extension, mime = await asyncio.to_thread(recording_info, data, purpose)
        form = FormData()
        form.add_field("purpose", purpose)
        form.add_field("file", data, filename=f"recording.{extension}", content_type=mime)
        result = self.check_result(
            await self.http.multipart(self.operation_url(profile, "files/upload"), self.headers(profile), form),
            profile,
        )
        file = result.get("file")
        if not isinstance(file, dict) or not str(file.get("file_id", "")).isdigit() or int(file["file_id"]) <= 0:
            raise TTSError("MiniMax 上传响应缺少有效 file.file_id")
        return int(file["file_id"])

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> CloneResult:
        opts = profile.options
        voice_id = self.voice_id(opts.clone_speaker_id)
        if len(text) > 1000:
            raise TTSError("MiniMax 复刻试听文本最多 1000 字符")
        body: Dict[str, Any] = {
            "voice_id": voice_id,
            "language_boost": opts.language_boost,
            "need_noise_reduction": opts.need_noise_reduction,
            "need_volume_normalization": opts.need_volume_normalization,
            "aigc_watermark": opts.aigc_watermark,
        }
        if opts.minimax_prompt_audio or opts.prompt_text:
            if not opts.minimax_prompt_audio or not opts.prompt_text.strip():
                raise TTSError("示例录音与对应文字需要同时填写")
            source = opts.minimax_prompt_audio
            prompt_id = int(source) if source.isdigit() else await self.upload(profile, source, "prompt_audio")
            body["clone_prompt"] = {"prompt_audio": prompt_id, "prompt_text": opts.prompt_text}
        if opts.minimax_validation_text:
            body.update(text_validation=opts.minimax_validation_text, accuracy=opts.minimax_validation_accuracy)
        if text.strip():
            body.update(text=text, model=profile.model)
        body["file_id"] = (
            int(opts.minimax_clone_file_id)
            if opts.minimax_clone_file_id
            else await self.upload(profile, url, "voice_clone")
        )
        result = await self.request(profile, "voice_clone", body)
        # 官方文档同时给出布尔示例与 type 对象；仅接受明确的“正常”状态。
        sensitive = result.get("input_sensitive")
        if not (
            sensitive is False
            or (isinstance(sensitive, dict) and type(sensitive.get("type")) is int and sensitive["type"] == 0)
        ):
            raise TTSError("MiniMax 录音未通过内容检查，或响应缺少检查结果")
        preview = None
        if text.strip():
            demo = result.get("demo_audio")
            if not isinstance(demo, str) or not demo:
                raise TTSError("MiniMax 创建音色后缺少试听地址；可通过自定义音色保存所填 ID")
            preview = await normalize_audio(
                await self.http.download(validate_url(demo)), profile.model_copy(update={"response_format": "mp3"})
            )
        return CloneResult(voice_id, "ready", preview)

    async def design(self, profile: Profile, name: str) -> CloneResult:
        opts = profile.options
        if not opts.voice_prompt.strip() or not opts.design_preview_text.strip():
            raise TTSError("请填写声音设计描述与试听文本")
        body = {
            "prompt": opts.voice_prompt,
            "preview_text": opts.design_preview_text,
            "aigc_watermark": opts.aigc_watermark,
        }
        if opts.clone_speaker_id:
            body["voice_id"] = self.voice_id(opts.clone_speaker_id)
        result = await self.request(profile, "voice_design", body)
        voice_id = result.get("voice_id")
        if (
            not isinstance(voice_id, str)
            or not voice_id
            or (opts.clone_speaker_id and voice_id != opts.clone_speaker_id)
        ):
            raise TTSError("MiniMax 设计响应缺少匹配的音色 ID")
        data = await asyncio.to_thread(decode_hex, result.get("trial_audio"), self.http.limit)
        preview = await normalize_audio(data, profile.model_copy(update={"response_format": "mp3"}))
        return CloneResult(voice_id, "ready", preview)

    async def voices(self, profile: Profile) -> List[Dict[str, str]]:
        result = self.check_result(
            await self.http.json("POST", self.url(profile, "voices"), self.headers(profile), {"voice_type": "all"}),
            profile,
        )
        return account_voices(result)

    async def models(self, profile: Profile) -> List[str]:
        # 官方通用列表也包含语言模型，工作台仅导入其中的语音模型。
        return [model for model in await super().models(profile) if model.startswith("speech-")]

    async def delete_voice(self, profile: Profile, voice_id: str) -> None:
        known = next((v.origin for v in profile.voices if v.id == voice_id and v.origin in {"clone", "design"}), None)
        if known is None:
            known = next((v["origin"] for v in await self.voices(profile) if v["id"] == voice_id), None)
        if known not in {"clone", "design"}:
            raise TTSError("请选择通过此账号复刻或设计的音色；系统音色保留在平台中")
        result = await self.request(
            profile,
            "delete_voice",
            {"voice_id": voice_id, "voice_type": "voice_cloning" if known == "clone" else "voice_generation"},
        )
        if result.get("voice_id") != voice_id:
            raise TTSError("MiniMax 删除响应的音色 ID 与目标不同")
