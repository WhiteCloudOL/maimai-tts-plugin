"""服务商调度、显式备选链与有界运行资源。"""

from aiohttp import ClientError
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from logging import Logger
from pydantic import ValidationError
from time import monotonic
from typing import Any, Callable, Coroutine, Deque, Dict, List, Optional, Set, Tuple, TypeVar

import asyncio
import hashlib
import json

from ..settings.config import RuntimeSection
from ..domain.breeze_control import boundary_pauses, parse_breeze_text
from ..domain.adaptive_tone import ToneAdapter
from ..domain.models import Audio, Profile, RouteTarget, StudioConfig, TTSError, safe_validation_error
from ..providers import PROVIDERS
from ..providers.base import Provider
from ..infrastructure.storage import ConfigStore
from ..infrastructure.diagnostics import Diagnostics, trace_scope
from ..infrastructure.transport import Transport
from ..infrastructure.voice_codec import SILK_HEADER, encode_silk
from ..infrastructure.cache import AsyncResultCache
from ..infrastructure.breeze_audio import join_breeze_audio
from ..domain.intonation import parse_tone_tag
from .intonation import apply_tone, breeze_requests, clean_display_text, validate_requested_tone
from .cache_keys import CacheKeys


T = TypeVar("T")


class TTSService:
    def __init__(self, store: ConfigStore, settings: RuntimeSection, logger: Logger) -> None:
        self.store = store
        self.settings = settings
        self.logger = logger
        self.log = Diagnostics(logger, settings.log_level, store.redact)
        self.http = Transport(settings, store.path.parent, self.log)
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._active: Set[asyncio.Task] = set()
        self.history: Deque[Dict[str, Any]] = deque(maxlen=30)
        self.cache = AsyncResultCache(settings.cache_max_size_mb * 1024 * 1024)
        self.cache_keys = CacheKeys()
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await self.cache.close()
        active = [task for task in self._active if task is not asyncio.current_task()]
        self.log.debug("关闭服务资源", active_tasks=len(active))
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        await self.http.close()

    async def execute(self, operation: Callable[[], Coroutine[Any, Any, T]]) -> T:
        if self.closed:
            raise TTSError("TTS 服务正在重载，请稍后重试")
        task = asyncio.current_task()
        self._active.add(task)
        try:

            async def guarded() -> T:
                queued = monotonic()
                if self._semaphore.locked():
                    self.log.debug("任务等待并发名额", limit=self.settings.max_concurrency)
                async with self._semaphore:
                    self.log.debug("任务取得并发名额", wait_ms=round((monotonic() - queued) * 1000))
                    return await operation()

            return await asyncio.wait_for(guarded(), timeout=self.settings.timeout_seconds)
        except asyncio.TimeoutError:
            raise TTSError(f"平台请求超时（{self.settings.timeout_seconds:g} 秒）") from None
        except ClientError as exc:
            # aiohttp 异常字符串可能包含 URL 中的秘密，只保留异常类型。
            raise TTSError(f"平台网络请求失败（{type(exc).__name__}），请检查地址、代理和网络") from None
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise TTSError(f"平台响应或参数无效：{safe_validation_error(exc)}") from None
        finally:
            self._active.discard(task)

    def provider(self, profile: Profile) -> Provider:
        if profile.kind not in {"compatible", "gpt_sovits", "breeze"} and not profile.api_key:
            raise TTSError("尚未配置 API Key / Access Token")
        self.log.debug("平台网络设置", provider_id=profile.id, mode=profile.network.mode)
        return PROVIDERS[profile.kind](self.http.for_profile(profile))

    async def catalog_list(self, profile: Profile, operation: str, *, refresh: bool = False) -> Any:
        provider = self.provider(profile)
        if operation not in {"models", "voices"}:
            raise TTSError("列表类型需要选择 models 或 voices")
        scope = self.cache_keys.scope(profile)
        key = self.cache_keys.digest(provider.list_cache_parameters(profile, operation))
        result = await self.cache.get(
            f"{operation}:{scope}",
            key,
            lambda: self.execute(
                lambda: provider.models(profile) if operation == "models" else provider.voices(profile)
            ),
            ttl=self.settings.list_cache_ttl_seconds,
            size=lambda value: len(json.dumps(value, ensure_ascii=False).encode("utf-8")),
            refresh=refresh,
        )
        self.log.debug(
            "平台列表缓存", operation=operation, provider_id=profile.id, model=profile.model, result=result.source
        )
        return result.value

    def invalidate_voices(self, profile: Profile) -> None:
        scope = self.cache_keys.scope(profile)
        self.cache.invalidate(f"voices:{scope}")
        self.cache.invalidate(f"speech:{scope}")
        self.log.debug("音色相关缓存已失效", provider_id=profile.id)

    async def prepare_delivery(self, audio: Audio, delivery_format: str) -> Audio:
        if delivery_format == "source":
            self.log.debug("使用源音频发送", format=audio.response_format, audio_bytes=len(audio.data))
            return audio
        if delivery_format != "silk":
            raise TTSError("不支持的聊天语音格式")
        if (
            audio.response_format == "silk" and audio.data.startswith(SILK_HEADER)
            and len(SILK_HEADER) < len(audio.data) <= self.http.limit
        ):
            return audio
        started = monotonic()
        self.log.info("语音转码开始", source=audio.response_format, target="silk", audio_bytes=len(audio.data))
        try:
            digest = await asyncio.to_thread(lambda: hashlib.sha256(audio.data).hexdigest())
            cached = await self.cache.get(
                "delivery",
                self.cache_keys.digest({"audio": digest, "format": audio.response_format}),
                lambda: self.execute(lambda: asyncio.to_thread(encode_silk, audio, self.http.limit)),
                ttl=self.settings.audio_cache_ttl_seconds,
                size=lambda value: len(value.data),
            )
            result = replace(
                cached.value, provider_id=audio.provider_id, request_id=audio.request_id, display_text=audio.display_text
            )
            self.log.debug("转码结果缓存", result=cached.source)
        except TTSError as exc:
            self.log.error("语音转码失败", error=str(exc), elapsed_ms=round((monotonic() - started) * 1000))
            raise
        self.log.info(
            "语音转码完成",
            format="silk",
            audio_bytes=len(result.data),
            elapsed_ms=round((monotonic() - started) * 1000),
        )
        return result

    async def synthesize(
        self,
        text: str,
        *,
        profile_id: str = "",
        model_id: str = "",
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        config: Optional[StudioConfig] = None,
        refresh_cache: bool = False,
        delivery_format: Optional[str] = None,
        tone_adapter: Optional[ToneAdapter] = None,
    ) -> Audio:
        if self.closed:
            raise TTSError("TTS 服务正在重载，请稍后重试")
        # 整条备选链必须在聊天 Hook / Tool 的 600 秒预算内结束。
        if config is None:
            config = self.store.config
        with trace_scope((text, *(profile.api_key for profile in config.profiles))):
            try:
                return await asyncio.wait_for(
                    self._synthesize_route(
                        text,
                        profile_id=profile_id,
                        model_id=model_id,
                        voice=voice,
                        speed=speed,
                        config=config,
                        refresh_cache=refresh_cache,
                        delivery_format=delivery_format,
                        tone_adapter=tone_adapter,
                    ),
                    timeout=540,
                )
            except asyncio.TimeoutError:
                self.log.error("合成链超时", budget_seconds=540)
                raise TTSError("默认与备选链已超过 540 秒总预算，请缩短平台超时或减少备选服务商") from None
            except TTSError as exc:
                detail = self.store.redact(str(exc), (text,) if isinstance(text, str) else ())
                self.log.error("合成链失败", error=detail)
                raise
            except asyncio.CancelledError:
                self.log.info("合成任务取消")
                raise
            except Exception as exc:
                self.log.unexpected("合成内部异常", exc)
                raise

    async def _synthesize_route(
        self,
        text: str,
        *,
        profile_id: str,
        model_id: str,
        voice: Optional[str],
        speed: Optional[float],
        config: Optional[StudioConfig],
        refresh_cache: bool,
        delivery_format: Optional[str],
        tone_adapter: Optional[ToneAdapter] = None,
    ) -> Audio:
        if not isinstance(text, str) or not text.strip():
            raise TTSError("待合成文本不能为空")
        if delivery_format not in {None, "silk", "source"}:
            raise TTSError("不支持的聊天语音格式")
        parsed = parse_tone_tag(text.strip())
        if parsed.error:
            raise TTSError(parsed.error)
        text = parsed.text
        if model_id and not profile_id:
            raise TTSError("选择模型时请同时指定平台")
        if len(text) > self.settings.max_text_length:
            raise TTSError(f"文本超过 {self.settings.max_text_length} 字符上限")
        if config is None:
            config = self.store.config
        profiles = {profile.id: profile for profile in config.profiles}
        if profile_id:
            profile = profiles.get(profile_id)
            if profile is None:
                raise TTSError("所选服务商不存在")
            route = [RouteTarget(provider_id=profile_id, model_id=model_id or profile.model)]
        else:
            if config.primary is None:
                raise TTSError("请先在整体路由选择服务商/模型")
            route = [config.primary, *config.fallbacks]
        failures: List[str] = []
        breeze_input = parse_breeze_text(text) if profiles[route[0].provider_id].kind == "breeze" else None
        if breeze_input is not None and breeze_input.error:
            raise TTSError(breeze_input.error)
        free_directions = breeze_input is not None and any(part.direction for part in breeze_input.parts)
        if parsed.tone is not None or free_directions:
            first = route[0]
            initial = profiles[first.provider_id]
            if first.model_id:
                initial = initial.for_model(first.model_id)
            try:
                initial = initial.with_synthesis_overrides(
                    voice=initial.resolve_voice_id(voice) if voice is not None else first.voice
                ).with_selected_voice()
                validate_requested_tone(initial, parsed.tone)
                if free_directions and initial.tone.mode != "dynamic":
                    raise TTSError("Breeze 自由语气需要在当前模型或音色中启用动态语调")
            except ValidationError as exc:
                raise TTSError(safe_validation_error(exc)) from None
        self.log.info(
            "合成开始",
            source="explicit" if profile_id else "route",
            revision=config.revision,
            text_length=len(text),
            route_count=len(route),
        )
        for index, target in enumerate(route, 1):
            provider_id = target.provider_id
            profile = profiles.get(provider_id)
            if profile is None or not profile.enabled:
                raise TTSError("所选服务商不存在或已停用")
            started = monotonic()
            self.log.info(
                "尝试服务商",
                provider=profile.name,
                provider_id=profile.id,
                kind=profile.kind,
                model=target.model_id,
                attempt=index,
                total=len(route),
            )
            try:
                model_profile = profile.for_model(target.model_id) if target.model_id else profile
                selected = model_profile.with_synthesis_overrides(
                    voice=model_profile.resolve_voice_id(voice) if voice is not None else target.voice
                ).with_selected_voice()
                self.log.info(
                    "本次语音路由",
                    provider=selected.name,
                    model=selected.model,
                    voice=selected.voice,
                    tone_mode=selected.tone.mode,
                    revision=config.revision,
                )
                if speed is not None:
                    # 临时语速是本次调用的最终覆盖，优先于模型及音色保存的语速。
                    selected = selected.with_synthesis_overrides(speed=speed)
                requested = parsed.tone
                route_text = text
                if tone_adapter is not None and selected.tone.mode == "dynamic" and not (requested or free_directions):
                    decision = await tone_adapter(selected)
                    if decision is not None:
                        requested = decision.tone
                        if selected.kind == "breeze" and decision.direction:
                            route_text = f"[语气:{decision.direction}]" + text
                requests = breeze_requests(selected, route_text, requested) if selected.kind == "breeze" else None
                selected, emotion = apply_tone(selected, requested)
                if requests is not None and free_directions:
                    self.log.info("应用 Breeze 分段语气", provider=selected.name, model=selected.model, sections=len(requests))
                elif emotion:
                    self.log.info(
                        "应用语音语调",
                        provider=selected.name,
                        model=selected.model,
                        mode=selected.tone.mode,
                        tone=emotion,
                    )
                client = self.provider(selected)
                # 首选 Breeze 的备选平台使用干净台词，避免把已识别的控制标记读给用户。
                speech_text = breeze_input.display_text if breeze_input is not None and selected.kind != "breeze" else text
                request_audio = partial(
                    self.execute,
                    partial(self._synthesize_breeze, client, requests) if requests is not None
                    else partial(client.synthesize, selected, speech_text),
                )
                key = (
                    self.cache_keys.breeze(requests) if requests is not None
                    else self.cache_keys.speech(selected, speech_text)
                )
                namespace = f"speech:{self.cache_keys.scope(selected)}"
                cached = await self.cache.get(
                    namespace,
                    key or "",
                    request_audio,
                    ttl=self.settings.audio_cache_ttl_seconds if key else 0,
                    size=lambda value: len(value.data),
                    refresh=refresh_cache,
                )
                audio = cached.value
                self.log.debug("合成结果缓存", provider_id=provider_id, model=selected.model, result=cached.source)
                if delivery_format is not None:
                    try:
                        audio = await self.prepare_delivery(audio, delivery_format)
                    except TTSError:
                        if key:
                            self.cache.evict(namespace, key)
                        raise
            except (TTSError, ValidationError) as exc:
                error = self.store.redact(safe_validation_error(exc), (text, profile.api_key))
                failures.append(f"{profile.name}/{target.model_id}：{error}")
                self.record(profile, False, error=error)
                self.log.warning(
                    "服务商合成失败",
                    provider=profile.name,
                    model=target.model_id,
                    attempt=index,
                    error=error,
                    elapsed_ms=round((monotonic() - started) * 1000),
                )
                if index < len(route):
                    self.log.warning("切换备选路由", next_attempt=index + 1, total=len(route))
                continue
            self.record(profile, True, audio_bytes=len(audio.data))
            self.log.info(
                "合成完成",
                provider=profile.name,
                model=selected.model,
                format=audio.response_format,
                audio_bytes=len(audio.data),
                elapsed_ms=round((monotonic() - started) * 1000),
                request_id=audio.request_id,
            )
            return replace(
                audio, provider_id=provider_id,
                display_text=clean_display_text(text, breeze=breeze_input is not None or selected.kind == "breeze"),
            )
        raise TTSError("所有已选服务商均合成失败：" + "；".join(failures))

    async def _synthesize_breeze(self, client: Provider, requests: List[Tuple[Profile, str]]) -> Audio:
        parts: List[Audio] = []
        total = 0
        for profile, text in requests:
            audio = await client.synthesize(profile, text)
            total += len(audio.data)
            if total > self.http.limit:
                raise TTSError("Breeze 分段合成音频超过大小上限")
            parts.append(audio)
        if not parts:
            raise TTSError("Breeze 控制标记后需要填写朗读内容")
        if len(parts) == 1:
            return parts[0]
        pauses = boundary_pauses([text for _, text in requests])
        return await asyncio.to_thread(join_breeze_audio, parts, self.http.limit, pauses)

    def record(self, profile: Profile, success: bool, *, error: str = "", audio_bytes: int = 0) -> None:
        self.history.appendleft(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "provider": profile.name,
                "success": success,
                "error": error,
                "audio_bytes": audio_bytes,
            }
        )
