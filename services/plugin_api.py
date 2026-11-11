"""SDK 插件 API 应用服务：目录、校验、语调分析、合成与投递。"""

from io import BytesIO
from pydantic import ValidationError
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

import asyncio
import av
import hashlib
import json

from ..domain.adaptive_tone import apply_breeze_events, direction_limit, parse_adaptive_tone
from ..domain.breeze_control import event_tags, parse_breeze_text
from ..domain.intonation import parse_tone_tag, tone_capability
from ..domain.models import Audio, Features, Profile, StudioConfig, TTSError, TTSOperationError, safe_validation_error
from ..api.models import API_TIMEOUT_SECONDS, API_VERSION, APIError, APIRequest, AudioRead, AudioSend, DescriptionQuery, JobQuery, VoicesQuery
from ..api.parameters import apply_parameters, parameter_schema, public_profile
from ..domain.voice_catalog import get_catalog
from ..infrastructure.api_runtime import APIAudioStore, APIJobs
from ..infrastructure.diagnostics import trace_scope
from ..infrastructure.storage import ConfigStore
from ..infrastructure.voice_codec import SILK_HEADER, voice_segment
from .adaptive_tone import ProbabilityToneAdapter
from .intonation import apply_tone, breeze_requests, clean_display_text
from .tts import TTSService


Sender = Callable[..., Awaitable[Dict[str, Any]]]


def audio_duration(audio: Audio) -> float:
    """读取最终音频的真实时长；SILK 按包计算，其他格式解码采样计时。"""
    if audio.data.startswith(SILK_HEADER):
        offset, frames = len(SILK_HEADER), 0
        while offset + 2 <= len(audio.data):
            size = int.from_bytes(audio.data[offset:offset + 2], "little", signed=True)
            offset += 2
            if size == -1:
                break
            if size <= 0 or offset + size > len(audio.data):
                raise TTSError("SILK 音频包长度异常")
            offset += size
            frames += 1
        return round(frames * 0.02, 3)
    try:
        with av.open(BytesIO(audio.data)) as source:
            return round(sum(frame.samples / frame.sample_rate for frame in source.decode(audio=0)), 6)
    except (av.FFmpegError, ValueError) as exc:
        raise TTSError(f"上游音频解码失败（{type(exc).__name__}），请核对平台返回的音频内容") from None


class PluginAPIService:
    """协调公开语音 API 的配置解析、平台能力、分析、合成与发送。

    Attributes:
        audio: 音频引用池，保存音频和公开结果元信息。
        jobs: 请求去重与后台任务注册表。
        sender: 共用的聊天投递入口，返回发送回执。
    """
    def __init__(self, store: ConfigStore, tts: TTSService, ctx: Any, sender: Sender, enabled: Callable[[], bool]) -> None:
        self.store, self.tts, self.ctx = store, tts, ctx
        self.sender, self.enabled = sender, enabled
        self.audio = APIAudioStore()
        self.jobs = APIJobs()

    async def close(self) -> None:
        await self.jobs.close()
        self.audio.clear()

    async def settings_changed(self) -> None:
        self.audio.trim(self.store.features.api_audio_max_size_mb * 1024 * 1024)
        if not self.store.features.api_enabled:
            active = [job.task for job in self.jobs.jobs.values() if job.task is not None and not job.task.done()]
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            self.tts.log.info("API 调用已关闭", cancelled_jobs=len(active))

    def require_enabled(self) -> None:
        if not self.enabled() or not self.store.features.api_enabled:
            raise APIError("API_DISABLED", "请在通用设置中启用插件 API")

    async def call(self, operation: str, /, **kwargs: Any) -> Dict[str, Any]:
        request_id = kwargs.get("request_id", "")
        started = monotonic()
        with trace_scope():
            try:
                if operation == "status":
                    self.audio.expire()
                    self.jobs.expire()
                    return {"ok": True, "data": {
                        "api_version": API_VERSION, "enabled": self.enabled() and self.store.features.api_enabled,
                        "configured": self.store.config.primary is not None,
                        "running_jobs": sum(job.state == "running" for job in self.jobs.jobs.values()),
                        "audio_results": len(self.audio.entries), "audio_bytes": self.audio.size,
                        "cache": self.tts.cache.stats(),
                    }}
                self.require_enabled()
                if operation in {"simple", "advanced", "submit"}:
                    if operation == "simple":
                        kwargs["control"] = {"mode": "auto", "style_hint": kwargs.pop("style_hint", "")}
                    request = APIRequest.model_validate(kwargs)
                    return await self._start(request, background=operation == "submit")
                if operation == "catalog":
                    return {"ok": True, "data": self.catalog()}
                if operation == "voices":
                    query = VoicesQuery.model_validate(kwargs)
                    return {"ok": True, "data": await self.voices(**query.model_dump())}
                if operation == "describe":
                    query = DescriptionQuery.model_validate(kwargs)
                    selected = self.select(self.store.config, query.provider_id, query.model_id, query.voice_id)[0]
                    selected = apply_parameters(selected, query.parameters)
                    return {"ok": True, "data": self.describe(selected)}
                if operation in {"validate", "analyze"}:
                    if operation == "analyze":
                        kwargs.setdefault("control", {"mode": "auto"})
                    request = APIRequest.model_validate(kwargs)
                    if operation == "analyze" and request.control.mode != "auto":
                        raise APIError("INVALID_PARAMETER", "语气分析请使用 auto 模式和可选 style_hint")
                    config, selected = self.prepare(request)
                    if operation == "validate":
                        return {"ok": True, "data": {"revision": config.revision,
                                "targets": [self.describe(profile) for profile in selected],
                                "output": request.output,
                                "llm_required": request.control.mode == "auto" and self._needs_analysis(request, selected[0])}}
                    return await self._start(request, background=False, analysis_profile=selected[0])
                if operation == "get_audio":
                    query = AudioRead.model_validate(kwargs)
                    return {"ok": True, "data": self.audio.read(**query.model_dump())}
                if operation in {"get_job", "cancel_job"}:
                    query = JobQuery.model_validate(kwargs)
                    job = self.jobs.get(query.job_id)
                    if operation == "cancel_job" and job.task is not None and not job.task.done():
                        job.task.cancel()
                        await asyncio.gather(job.task, return_exceptions=True)
                        self.tts.log.info("API 任务取消", api_request_id=job.request_id, job_id=job.job_id)
                    return {"ok": True, "data": {"job_id": job.job_id, "request_id": job.request_id,
                            "state": job.state, "result": job.result}}
                raise APIError("INVALID_OPERATION", "请使用文档中的 API 名称")
            except asyncio.CancelledError:
                raise
            except (APIError, TTSError, ValidationError) as exc:
                return self.failure(exc, request_id=request_id)
            except Exception as exc:
                self.tts.log.unexpected("API 入口异常", exc, operation=operation, api_request_id=request_id)
                return {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "请按请求编号查看插件日志"}}
            finally:
                self.tts.log.debug("API 调用结束", operation=operation, api_request_id=request_id,
                                   job_id=kwargs.get("job_id", ""), audio_id=kwargs.get("audio_id", ""),
                                   elapsed_ms=round((monotonic() - started) * 1000))

    def failure(self, exc: Exception, *, request_id: str = "", private: Tuple[str, ...] = ()) -> Dict[str, Any]:
        code = exc.code if isinstance(exc, TTSOperationError) else "INVALID_PARAMETER" if isinstance(exc, ValidationError) else "TTS_ERROR"
        message = self.store.redact(safe_validation_error(exc), private)
        self.tts.log.warning("API 请求失败", api_request_id=request_id, code=code, error=message)
        return {"ok": False, "request_id": request_id, "error": {"code": code, "message": message}}

    def select(
        self, config: StudioConfig, provider_id: str = "", model_id: str = "", voice_id: str = "", *, voice_settings: bool = True,
    ) -> List[Profile]:
        if model_id and not provider_id:
            raise APIError("INVALID_PARAMETER", "model_id 请与 provider_id 一起填写")
        roots = {profile.id: profile for profile in config.profiles}
        if provider_id:
            if provider_id not in roots:
                raise APIError("PROVIDER_MISSING", "请从 tts_catalog 选择 provider_id")
            targets = [(provider_id, model_id or roots[provider_id].model, None)]
        else:
            if config.primary is None:
                raise APIError("ROUTE_MISSING", "请在语音路由中选择首选模型")
            targets = [(target.provider_id, target.model_id, target.voice) for target in [config.primary, *config.fallbacks]]
        output = []
        for provider, model, route_voice in targets:
            root = roots[provider]
            if not root.enabled:
                raise APIError("PROVIDER_DISABLED", "请启用所选服务商")
            selected = root.for_model(model or root.model)
            selected = selected.with_synthesis_overrides(
                voice=selected.resolve_voice_id(voice_id) if voice_id else route_voice,
            )
            if voice_settings:
                selected = selected.with_selected_voice()
            output.append(selected)
        return output

    def transform(self, request: APIRequest, profile: Profile) -> Profile:
        selected = apply_parameters(profile, request.parameters)
        cap = tone_capability(selected.kind, selected.model, selected.voice, selected.options.resource_id)
        if request.control.mode == "off" or "minimax_emotion" in request.parameters:
            if "minimax_emotion" in request.parameters and (request.control.tone or request.control.mode == "auto"):
                raise APIError("INVALID_PARAMETER", "minimax_emotion 与语调控制请选择一种")
            selected.tone.mode = "off"
        elif (cap is not None or (request.control.style and direction_limit(selected) > 0)) and (
            request.control.mode == "auto" or request.control.tone is not None or request.control.style
            or request.control.events or parse_tone_tag(request.text).tone is not None
            or parse_tone_tag(request.text).direction
            or (selected.kind == "breeze" and parse_breeze_text(request.text).marked)
        ):
            selected.tone.mode = "dynamic"
        return selected

    def prepare(self, request: APIRequest, config: Optional[StudioConfig] = None) -> Tuple[StudioConfig, List[Profile]]:
        if config is None:
            config = self.store.config
        if len(request.text) > self.tts.settings.max_text_length:
            raise APIError("INVALID_PARAMETER", "正文请按运行设置的字符上限分段")
        try:
            selected = [self.transform(request, profile) for profile in self.select(
                config, request.provider_id, request.model_id, request.voice_id,
            )]
            for profile in selected:
                self._speech_text(request, profile)
        except TTSOperationError:
            raise
        except TTSError as exc:
            raise APIError("INVALID_PARAMETER", str(exc)) from None
        return config, selected

    def _speech_text(self, request: APIRequest, profile: Profile) -> str:
        parsed = parse_tone_tag(request.text)
        if parsed.error:
            raise APIError("INVALID_PARAMETER", parsed.error)
        text = request.text
        explicit = request.control.tone is not None or bool(request.control.style or request.control.events)
        if explicit and (parsed.tone is not None or parsed.direction):
            raise APIError("INVALID_PARAMETER", "结构化语调与正文头部控制请选择一种")
        if explicit:
            # 结构化事件与普通动态语调使用同一套类型、锚点、语言及长度校验。
            decision = parse_adaptive_tone(json.dumps({
                **({"tone": request.control.tone} if request.control.tone is not None else {}),
                "direction": request.control.style,
                **({"events": request.control.events} if profile.kind == "breeze" else {}),
            }), profile.tone.allowed or (tone_capability(profile.kind, profile.model, profile.voice, profile.options.resource_id) or {}).get("tones", []),
                breeze=profile.kind == "breeze", free_direction=direction_limit(profile) > 0, text=request.text,
                event_options=event_tags(profile.options.breeze_language_code, local=profile.options.breeze_api_mode == "self_hosted") if profile.kind == "breeze" else {})
            if request.control.events and profile.kind != "breeze":
                raise APIError("INVALID_PARAMETER", "events 请用于 Breeze 音频事件")
            apply_tone(profile, decision.tone, direction=decision.direction)
            if profile.kind == "breeze":
                text = apply_breeze_events(text, decision.events)
            prefix = "[mmtts:" + decision.tone + "]" if decision.tone else ""
            if decision.direction:
                prefix = "[mmtts:style=" + decision.direction + "]"
            text = prefix + text
        tagged = parse_tone_tag(text)
        if profile.kind == "breeze":
            breeze_requests(profile, tagged.text, tagged.tone, direction=tagged.direction)
        else:
            effective, _ = apply_tone(profile, tagged.tone, direction=tagged.direction)
            if (effective.kind == "volcengine" and effective.instructions and effective.options.enable_ssml
                    and effective.options.volc_instruction_mode == "cot"):
                raise APIError("INVALID_PARAMETER", "SSML 与 COT 请分别启用")
        return text

    @staticmethod
    def describe(profile: Profile) -> Dict[str, Any]:
        result = public_profile(profile)
        result.update(parameters=parameter_schema(profile), direction_limit=direction_limit(profile))
        if profile.kind == "breeze" and result["tone_supported"]:
            result["events"] = event_tags(profile.options.breeze_language_code, local=profile.options.breeze_api_mode == "self_hosted")
        return result

    def catalog(self) -> Dict[str, Any]:
        config = self.store.config
        return {"revision": config.revision, "primary": config.primary.model_dump() if config.primary else None,
                "fallbacks": [target.model_dump() for target in config.fallbacks],
                "providers": [{"id": profile.id, "name": profile.name, "kind": profile.kind, "enabled": profile.enabled,
                               "default_model_id": profile.model,
                               "models": [{"id": model.id, "name": model.name or model.id, "default_voice_id": model.voice}
                                          for model in profile.models]} for profile in config.profiles]}

    async def voices(self, provider_id: str = "", model_id: str = "", source: str = "saved", refresh: bool = False) -> Dict[str, Any]:
        # 音色目录按模型查询；默认音色训练期间仍可查看其他音色与创建状态。
        profile = self.select(self.store.config, provider_id, model_id, voice_settings=False)[0]
        if source == "account":
            values = await self.tts.catalog_list(profile, "voices", refresh=refresh)
            # 账号接口的附加字段由各平台管理，公共目录只返回跨插件选择所需的字段。
            voices = [{"id": item["id"], "name": item.get("name", item["id"]), "model_id": item.get("model", "")}
                      for item in values if item.get("model", "") in ("", profile.model)]
        elif source == "preset":
            catalog = await asyncio.to_thread(get_catalog)
            item = catalog[profile.kind]
            values = item.get("model_voices", {}).get(profile.model, item.get("voices", []))
            details = item.get("voice_details", {}).get(profile.model, {})
            voices = [{"id": voice, "name": details.get(voice, {}).get("name", voice), "model_id": profile.model} for voice in values]
        elif source == "saved":
            voices = [{"id": voice.id, "name": voice.name, "model_id": voice.model, "state": voice.clone_state}
                      for voice in profile.voices if voice.model in ("", profile.model)]
            if profile.voice and all(voice["id"] != profile.voice for voice in voices):
                voices.insert(0, {"id": profile.voice, "name": profile.voice, "model_id": profile.model, "state": "ready"})
        else:
            raise APIError("INVALID_PARAMETER", "source 请选择 saved、preset 或 account")
        return {"provider_id": profile.id, "model_id": profile.model, "default_voice_id": profile.voice, "voices": voices}

    def _needs_analysis(self, request: APIRequest, profile: Profile) -> bool:
        parsed = parse_tone_tag(request.text)
        return (tone_capability(profile.kind, profile.model, profile.voice, profile.options.resource_id) is not None
                and parsed.tone is None and not parsed.direction
                and not (profile.kind == "breeze" and any(part.direction for part in parse_breeze_text(parsed.text).parts)))

    async def analyze(self, request: APIRequest, profile: Profile, features: Features) -> Dict[str, Any]:
        result = {**public_profile(profile), "text": request.text,
                  "parameters": request.parameters,
                  "display_text": clean_display_text(request.text, breeze=profile.kind == "breeze")}
        if not self._needs_analysis(request, profile):
            return dict(result, control_status="existing" if result["tone_supported"] else "unsupported",
                        control={"mode": "manual" if result["tone_supported"] else "off"}, llm_used=False)
        adapter = ProbabilityToneAdapter(self.ctx, features, request.text, self.tts.log,
                                        style_hint=request.control.style_hint, required=True, purpose="api")
        try:
            decision = await adapter(profile)
        except TTSError as exc:
            raise APIError("LLM_ERROR", str(exc)) from None
        if decision is None:
            return dict(result, control_status="default", control={"mode": "manual"}, llm_used=False)
        return dict(result, control_status="selected", control={"mode": "manual", "tone": None if decision.direction else decision.tone,
                                    "style": decision.direction, "events": [{"before": event.before, "tag": event.tag} for event in decision.events]}, llm_used=True)

    async def _guard(self, request: APIRequest, operation: Callable[[], Awaitable[Dict[str, Any]]]) -> Dict[str, Any]:
        private = (request.text, request.control.style, request.control.style_hint)
        with trace_scope(private):
            try:
                data = await asyncio.wait_for(operation(), timeout=API_TIMEOUT_SECONDS)
                return {"ok": True, "request_id": request.request_id, "data": data}
            except asyncio.TimeoutError:
                return self.failure(APIError("TIMEOUT", f"API 任务已达到 {API_TIMEOUT_SECONDS:g} 秒执行上限，请调整语音辅助 LLM 或平台请求预算"),
                                    request_id=request.request_id, private=private)
            except asyncio.CancelledError:
                self.tts.log.info("API 请求取消", api_request_id=request.request_id)
                raise
            except (APIError, TTSError, ValidationError) as exc:
                return self.failure(exc, request_id=request.request_id, private=private)
            except Exception as exc:
                self.tts.log.unexpected("API 任务异常", exc, api_request_id=request.request_id)
                return {"ok": False, "request_id": request.request_id,
                        "error": {"code": "INTERNAL_ERROR", "message": "请按请求编号查看插件日志"}}

    async def _start(self, request: APIRequest, *, background: bool, analysis_profile: Optional[Profile] = None) -> Dict[str, Any]:
        if not request.request_id:
            request.request_id = uuid4().hex
        try:
            encoded = json.dumps(
                ["analyze" if analysis_profile is not None else "generate", request.model_dump(exclude={"request_id"})],
                sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            )
        except (TypeError, ValueError):
            raise APIError("INVALID_PARAMETER", "parameters 请使用标准 JSON 类型与有限数值") from None
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        config = self.store.config
        feature = config.feature
        job = self.jobs.start(request.request_id, fingerprint,
                              lambda job_id: self._guard(request, lambda: self.analyze(request, analysis_profile, feature)
                                                        if analysis_profile is not None else self.generate(request, config, job_id)),
                              limit=feature.api_max_jobs, ttl=feature.api_audio_ttl_seconds)
        self.tts.log.info("API 请求接收", api_request_id=request.request_id, job_id=job.job_id,
                          output=request.output, control=request.control.mode, text_length=len(request.text))
        if background:
            return {"ok": True, "data": {"job_id": job.job_id, "request_id": job.request_id, "state": job.state}}
        return await self.jobs.wait(job)

    async def generate(self, request: APIRequest, config: StudioConfig, job_id: str) -> Dict[str, Any]:
        started = monotonic()
        # 在任务边界完成静态校验，相同 request_id 始终复用该次任务的结果。
        _, prepared = self.prepare(request, config)
        selected: Dict[str, Profile] = {}
        decisions: Dict[str, Any] = {}
        adapter = ProbabilityToneAdapter(self.ctx, config.feature, request.text, self.tts.log,
                                        style_hint=request.control.style_hint, required=True, purpose="api")

        def transform(profile: Profile) -> Profile:
            self.require_enabled()
            result = self.transform(request, profile)
            selected[profile.id] = result
            return result

        async def adapt(profile: Profile):
            decision = await adapter(profile)
            decisions[profile.id] = decision
            return decision

        # 结构化手动控制已按全部目标校验，统一序列化为现有合成链路识别的控制文本。
        initial = prepared[0]
        text = self._speech_text(request, self.transform(request, initial))
        if request.output == "send":
            delivery = config.feature.delivery_format if request.delivery_format == "default" else request.delivery_format
        else:
            delivery = "source" if request.delivery_format == "default" else request.delivery_format
        self.tts.log.info("API 合成开始", api_request_id=request.request_id, job_id=job_id,
                          provider_id=initial.id, model=initial.model, voice=initial.voice, delivery=delivery)
        audio = await self.tts.synthesize(text, config=config, profile_id=request.provider_id,
                                          model_id=request.model_id, voice=request.voice_id or None,
                                          delivery_format=delivery, refresh_cache=request.refresh_cache,
                                          tone_adapter=adapt if request.control.mode == "auto" else None,
                                          profile_transform=transform)
        self.require_enabled()
        actual = selected[audio.provider_id]
        decision = decisions.get(audio.provider_id)
        tagged = parse_tone_tag(text.strip())
        requested_tone = decision.tone if decision is not None else tagged.tone
        requested_style = decision.direction if decision is not None else tagged.direction
        effective, emotion = apply_tone(actual, requested_tone, direction=requested_style)
        duration = await asyncio.to_thread(audio_duration, audio)
        self.require_enabled()
        metadata = {**public_profile(actual), "display_text": audio.display_text, "duration_seconds": duration,
                    "parameters": {key: spec["default"] for key, spec in parameter_schema(effective)["properties"].items()},
                    "cache": {"synthesis": audio.cache_status, "delivery": audio.delivery_cache_status},
                    "control": {"mode": request.control.mode, "llm_used": decision is not None,
                                "tone": emotion,
                                "style": requested_style,
                                "events": [{"before": event.before, "tag": event.tag} for event in decision.events] if decision is not None else request.control.events}}
        feature = self.store.features
        info = self.audio.put(audio, metadata, ttl=feature.api_audio_ttl_seconds,
                              max_bytes=feature.api_audio_max_size_mb * 1024 * 1024)
        result = {"audio": info}
        if request.output == "send":
            result["send"] = await self.send(audio, request.stream_id, request.keep_text)
        self.tts.log.info("API 请求完成", api_request_id=request.request_id, job_id=job_id, provider_id=actual.id,
                          model=actual.model, voice=actual.voice, audio_bytes=len(audio.data),
                          cache=audio.cache_status, elapsed_ms=round((monotonic() - started) * 1000))
        return result

    async def send(self, audio: Audio, stream_id: str, keep_text: bool) -> Dict[str, Any]:
        self.require_enabled()
        display = audio.display_text or ""
        self.tts.log.info("API 发送开始", stream_id=stream_id, audio_bytes=len(audio.data), keep_text=keep_text)
        segment = await asyncio.to_thread(voice_segment, audio, display)
        try:
            self.require_enabled()
            if keep_text and display:
                await self.sender([{"type": "text", "data": display}], stream_id, display)
                self.require_enabled()
            receipt = await self.sender([segment], stream_id, display)
        except TTSOperationError:
            raise
        except TTSError as exc:
            raise APIError("SEND_ERROR", str(exc)) from None
        self.tts.log.info("API 发送完成", stream_id=stream_id, sent=True, message_id=receipt.get("message_id"))
        return receipt

    async def send_audio(self, audio_id: str, stream_id: str, keep_text: bool = False, request_id: str = "") -> Dict[str, Any]:
        # 发送复用结果也通过同一请求注册表去重；同一音频可面向不同聊天流创建独立请求。
        try:
            self.require_enabled()
            params = AudioSend(audio_id=audio_id, stream_id=stream_id, keep_text=keep_text, request_id=request_id)
            audio_id, stream_id, keep_text, request_id = params.audio_id, params.stream_id, params.keep_text, params.request_id
            if not params.stream_id.strip():
                raise APIError("INVALID_PARAMETER", "请填写真实 stream_id，并使用 128 字以内 request_id")
            request_id = request_id or uuid4().hex
            fingerprint = hashlib.sha256(json.dumps([audio_id, stream_id, keep_text]).encode()).hexdigest()
            existing = self.jobs.find(request_id, fingerprint)
            if existing is not None:
                self.tts.log.info("API 复用发送结果", api_request_id=request_id, job_id=existing.job_id, audio_id=audio_id)
                return await self.jobs.wait(existing)
            entry = self.audio.get(audio_id)
            request = APIRequest(text=entry.audio.display_text or "语音", output="send", stream_id=stream_id,
                                 keep_text=keep_text, request_id=request_id)

            async def deliver() -> Dict[str, Any]:
                self.tts.log.info("API 复用音频发送", api_request_id=request_id, audio_id=audio_id, stream_id=stream_id)
                feature = self.store.features
                audio = await self.tts.prepare_delivery(entry.audio, feature.delivery_format,
                                                        integer_seconds=feature.silk_integer_seconds)
                return {"send": await self.send(audio, stream_id, keep_text)}

            feature = self.store.features
            job = self.jobs.start(request.request_id, fingerprint, lambda _: self._guard(request, deliver),
                                  limit=feature.api_max_jobs, ttl=feature.api_audio_ttl_seconds)
            return await self.jobs.wait(job)
        except (APIError, TTSError, ValidationError) as exc:
            return self.failure(exc, request_id=request_id)
