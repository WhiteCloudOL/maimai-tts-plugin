"""仅在 LLM 回复扩展中执行整轮概率语音，按回复 ID 隔离状态。"""

from copy import deepcopy
from dataclasses import dataclass
from maibot_sdk import HookHandler, ReplyExtension
from maibot_sdk.types import ErrorPolicy, HookMode, ToolParameterInfo, ToolParamType
from random import random
from time import monotonic
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import asyncio
import math

from ..domain.breeze_control import parse_breeze_text
from ..domain.intonation import parse_tone_tag
from ..domain.models import TTSError
from ..domain.probability_delivery import batch_text, delivery_attachments, source_key, speech_text
from ..domain.speech_text import spoken_text_of
from ..infrastructure.voice_codec import voice_segment
from ..services.intonation import validate_requested_direction, validate_requested_tone
from ..services.format_correction import correct_speech_arguments
from ..services.speech_input import validate_speech_arguments
from .adaptive_tone import ProbabilityToneAdapter


@dataclass
class ProbabilityReplyState:
    """拆分前确认的整轮判定，只能由同一条 LLM 回复消费。"""

    stream_id: str
    should_convert: bool
    expires_at: float
    breeze_controls: bool = False
    operation_deadline: float = 0


@dataclass
class VoiceReplyState:
    """显式语音回复扩展在分段前校验与解析出的参数状态，按 reply_id 隔离。"""

    stream_id: str
    tone_prefix: str
    expires_at: float
    breeze_controls: bool = False
    should_convert: bool = True
    skip_reason: str = ""
    tag_error: str = ""
    voice: Optional[str] = None
    speed: Optional[float] = None
    operation_deadline: float = 0


@dataclass
class ProbabilityVoiceState:
    """等待 Host 确定引用方式的自有语音，保留对应的干净正文。"""

    reply_id: str
    text_segments: List[Dict[str, Any]]
    expires_at: float
    source_key: str
    keep_text: Optional[bool] = None
    quote_keep_voice: Optional[bool] = None


@dataclass
class ProbabilityAttachmentState:
    """语音发送成功后投递的原生附件，使用出站消息元数据独立关联。"""

    stream_id: str
    batches: List[List[Dict[str, Any]]]
    expires_at: float
    storage_message: bool
    show_log: bool


_DELIVERY_METADATA = "maimai_tts_probability_delivery"


class ProbabilityMixin:
    def _probability_extension_name(self) -> str:
        return f"{self.ctx.plugin_id}.probability_tts"

    def _voice_reply_extension_name(self) -> str:
        return f"{self.ctx.plugin_id}.voice_reply"

    def _is_probability_reply(self, arguments: Any) -> bool:
        if not isinstance(arguments, dict):
            return False
        options = arguments.get("plugin_options")
        return isinstance(options, dict) and self._probability_extension_name() in options

    def _is_voice_reply(self, arguments: Any) -> bool:
        if not isinstance(arguments, dict):
            return False
        options = arguments.get("plugin_options")
        return isinstance(options, dict) and self._voice_reply_extension_name() in options

    def _voice_reply_state_map(self) -> Dict[str, VoiceReplyState]:
        states = getattr(self, "_voice_reply_states", None)
        if states is None:
            states = {}
            self._voice_reply_states = states
        return states

    def _voice_reply_prompted_map(self) -> Dict[str, float]:
        prompted = getattr(self, "_voice_reply_prompted_ids", None)
        if prompted is None:
            prompted = {}
            self._voice_reply_prompted_ids = prompted
        return prompted

    def _voice_reply_parameters(self, arguments: Any) -> Dict[str, Any]:
        if not isinstance(arguments, dict):
            return {}
        options = arguments.get("plugin_options")
        if not isinstance(options, dict):
            return {}
        params = options.get(self._voice_reply_extension_name())
        return params if isinstance(params, dict) else {}

    def _extract_voice_reply_overrides(self, parameters: Any) -> Tuple[Any, Any]:
        params = parameters if isinstance(parameters, dict) else {}
        raw_voice = params.get("voice")
        selected_voice: Any = None
        if self.features.allow_voice_override and raw_voice is not None:
            selected_voice = raw_voice.strip() or None if isinstance(raw_voice, str) else raw_voice
        raw_speed = params.get("speed")
        selected_speed: Any = raw_speed if self.features.allow_speed_override else None
        return selected_voice, selected_speed

    @staticmethod
    def _normalize_voice_override(voice: Any) -> Optional[str]:
        if isinstance(voice, str):
            return voice.strip() or None
        return None

    @staticmethod
    def _normalize_speed_override(speed: Any) -> Optional[float]:
        if isinstance(speed, (int, float)) and not isinstance(speed, bool) and math.isfinite(speed):
            return float(speed)
        return None

    def _is_probability_scope_allowed(self, chat: Any) -> bool:
        group = chat.get("is_group_chat") if isinstance(chat, dict) else None
        return (
            self.features.probability_group_enabled
            if group is True
            else (self.features.probability_private_enabled if group is False else False)
        )

    def _probability_prepare_scope_map(self) -> Dict[str, Tuple[str, bool, float]]:
        scopes = getattr(self, "_probability_prepare_scopes", None)
        if scopes is None:
            scopes = {}
            self._probability_prepare_scopes = scopes
        return scopes

    def _is_prepared_probability_scope_allowed(self, reply_tool_args: Any, session_id: str) -> bool:
        if not isinstance(reply_tool_args, dict):
            return True
        reply_id = reply_tool_args.get("_reply_id", "")
        if not isinstance(reply_id, str) or not reply_id:
            return True
        self._remove_expired_probability_states()
        entry = self._probability_prepare_scope_map().get(reply_id)
        if entry is None:
            return True
        saved_session_id, allowed, _ = entry
        return allowed if (not session_id or saved_session_id == session_id) else True

    @HookHandler(
        "maisaka.planner.before_request",
        name="prepare_probability_reply_availability",
        description="核对当前聊天中 reply 工具实际提供的概率语音扩展",
        mode=HookMode.BLOCKING,
        timeout_ms=1000,
    )
    async def prepare_probability_reply_availability(
        self, session_id: str = "", tool_definitions: Optional[List[Dict[str, Any]]] = None, **kwargs: Any
    ) -> Dict[str, Any]:
        del kwargs
        self._probability_reply_available_sessions.discard(session_id)
        if self.config.plugin.enabled and self.features.probability_enabled:
            for tool in tool_definitions or []:
                function = tool.get("function", {})
                if function.get("name") != "reply":
                    continue
                options = function.get("parameters", {}).get("properties", {}).get("plugin_options", {})
                if self._probability_extension_name() in options.get("properties", {}):
                    self._probability_reply_available_sessions.add(session_id)
                    break
        return {"action": "continue"}

    @HookHandler(
        "maisaka.planner.after_response",
        name="attach_probability_reply_extension",
        description="按工作台开关为 LLM 的 reply 调用附加概率语音扩展",
        mode=HookMode.BLOCKING,
        timeout_ms=1000,
    )
    async def attach_probability_reply_extension(
        self,
        output_items: Optional[List[Dict[str, Any]]] = None,
        item_schema_version: int = 1,
        session_id: str = "",
        hook_name: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del hook_name
        if (
            not self.config.plugin.enabled
            or not self.features.probability_enabled
            or self.features.probability <= 0
            or not (self.features.probability_group_enabled or self.features.probability_private_enabled)
            or session_id not in self._probability_reply_available_sessions
            or not output_items
        ):
            return {"action": "continue"}
        items = deepcopy(output_items)
        changed = False
        for item in items:
            if item.get("item_type") != "FunctionCallItem":
                continue
            call = item.get("tool_call")
            if not isinstance(call, dict) or call.get("func_name") != "reply":
                continue
            arguments = call.get("args")
            if not isinstance(arguments, dict):
                continue
            if (
                (self.features.tool_enabled and self.features.tool_use_replyer and self._is_voice_reply(arguments))
                or self._has_rich_reply_attachment(arguments)
                or self._uses_legacy_native_voice(arguments)
            ):
                continue
            options = arguments.get("plugin_options", {})
            if not isinstance(options, dict):
                # 畸形工具参数交由宿主校验，保留原来的错误信息。
                continue
            if self._probability_extension_name() not in options:
                options[self._probability_extension_name()] = {}
                arguments["plugin_options"] = options
                changed = True
        if not changed:
            return {"action": "continue"}
        return {
            "action": "continue",
            "modified_kwargs": dict(
                kwargs, output_items=items, item_schema_version=item_schema_version, session_id=session_id
            ),
        }

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="prepare_probability_tts_reply",
        description="按整轮 LLM 正文检查长度并抽取一次语音概率",
        mode=HookMode.BLOCKING,
        timeout_ms=600000,
    )
    async def prepare_probability_tts_reply(
        self,
        response: str = "",
        session_id: str = "",
        reply_tool_args: Any = None,
        hook_name: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del hook_name
        started = monotonic()
        original_response = response
        parsed = parse_tone_tag(response)
        features = self.features
        reply_id = reply_tool_args.get("_reply_id", "") if isinstance(reply_tool_args, dict) else ""
        explicit_voice = (
            self.config.plugin.enabled
            and features.tool_enabled
            and features.tool_use_replyer
            and self._is_voice_reply(reply_tool_args)
            and isinstance(reply_id, str)
            and bool(reply_id)
        )
        targeted = (not explicit_voice) and self._is_probability_reply(reply_tool_args) and isinstance(reply_id, str) and bool(reply_id)
        legacy_native_voice = self._uses_legacy_native_voice(reply_tool_args)
        tag_error = parsed.error
        breeze = parse_breeze_text(parsed.text)
        breeze_controls = False
        # 控制标记属于语音表演，不随纯文字回复外发；未挂概率扩展的回复也须清理。
        if breeze.marked:
            profile = self.primary_tone_profile()
            breeze_controls = profile is not None and profile.kind == "breeze"
            if breeze_controls and not breeze.display_text:
                tag_error = "自动语音的控制标记后需要填写朗读正文"
        display_text = breeze.display_text if breeze_controls else parsed.text

        if explicit_voice:
            selected_voice, selected_speed = self._extract_voice_reply_overrides(
                self._voice_reply_parameters(reply_tool_args)
            )
            if not legacy_native_voice:
                profile = self.primary_tone_profile()
                arguments: Dict[str, Any] = {"text": response}
                if selected_voice is not None:
                    arguments["voice"] = selected_voice
                if selected_speed is not None:
                    arguments["speed"] = selected_speed
                if profile is None:
                    if parsed.tone or parsed.direction or selected_voice is not None or selected_speed is not None:
                        tag_error = "整体语音路由尚待配置"
                else:
                    if not tag_error:
                        try:
                            validate_speech_arguments(
                                profile, arguments, self.service.settings.max_text_length,
                                strip_parentheticals=features.strip_stage_directions,
                            )
                        except TTSError as exc:
                            tag_error = str(exc)
                    if tag_error:
                        revision = self.store.config.revision
                        result = await correct_speech_arguments(
                            self.ctx, features, profile, arguments, self.service.log,
                            automatic=False, max_text_length=self.service.settings.max_text_length,
                        )
                        if result.arguments is not None and self.store.config.revision == revision:
                            response = result.arguments["text"]
                            selected_voice = result.arguments.get("voice")
                            selected_speed = result.arguments.get("speed")
                            parsed = parse_tone_tag(response)
                            breeze = parse_breeze_text(parsed.text)
                            breeze_controls = profile.kind == "breeze" and breeze.marked
                            display_text = breeze.display_text if breeze_controls else parsed.text
                            tag_error = ""
                        else:
                            tag_error = result.error or "语调纠正期间配置已变化，保留原文"
            if tag_error and not legacy_native_voice:
                self.service.log.warning("显式语音参数校验失败，保留纯文本", stream_id=session_id, error=tag_error)

            should_convert = not legacy_native_voice and not tag_error and bool(display_text.strip())
            skip_reason = (
                "legacy_native_voice" if legacy_native_voice else
                "invalid_arguments" if tag_error else
                "empty_response" if not display_text.strip() else ""
            )
            tone_prefix = (
                f"[mmtts:{parsed.tone}]" if parsed.tone
                else (f"[mmtts:style={parsed.direction}]" if parsed.direction else "")
            ) if should_convert else ""
            if session_id:
                self._remove_expired_probability_states()
                self._voice_reply_state_map()[reply_id] = VoiceReplyState(
                    stream_id=session_id,
                    tone_prefix=tone_prefix,
                    expires_at=monotonic() + 600,
                    breeze_controls=breeze_controls,
                    should_convert=should_convert,
                    skip_reason=skip_reason,
                    tag_error=tag_error,
                    voice=self._normalize_voice_override(selected_voice) if should_convert else None,
                    speed=self._normalize_speed_override(selected_speed) if should_convert else None,
                    operation_deadline=started + 540,
                )
            self.service.log.debug(
                "显式语音回复前处理",
                stream_id=session_id,
                reply_id=reply_id,
                convert=should_convert,
                reason=skip_reason or "selected",
                tone_prefix=tone_prefix,
                breeze_controls=breeze_controls,
                tag_error=tag_error,
            )
            if not display_text.strip():
                return {"action": "abort", "custom_result": {"reason": tag_error or "控制标记后缺少可见正文"}}
            if not should_convert:
                if not parsed.tagged and not breeze_controls and response == original_response:
                    return {"action": "continue"}
                modified = dict(kwargs, session_id=session_id, reply_tool_args=reply_tool_args, response=display_text)
                return {"action": "continue", "modified_kwargs": modified}
            modified = dict(kwargs, session_id=session_id, reply_tool_args=reply_tool_args)
            if breeze_controls:
                # Breeze 局部事件标记不能被拆分或错字破坏，保留整段交给扩展合成。
                modified["response"] = response
                modified["skip_post_process"] = True
                modified["enable_chinese_typo"] = False
            else:
                # 先剥离头部语调标签，仅关闭错别字避免语音读出错字；默认关闭宿主文字分段，避免多段语音割裂；
                # 关闭“语音回复不拆分分段”后沿用宿主或前置 Hook 的分段配置，合成阶段按段补回同一语调前缀。
                modified["response"] = display_text
                if features.tool_replyer_disable_splitter:
                    modified["enable_splitter"] = False
                modified["enable_chinese_typo"] = False
            return {"action": "continue", "modified_kwargs": modified}

        if (parsed.tone or parsed.direction) and not (tag_error or legacy_native_voice):
            profile = self.primary_tone_profile()
            if profile is None:
                tag_error = "整体语音路由尚待配置"
            else:
                try:
                    validate_requested_tone(profile, parsed.tone)
                    if parsed.direction:
                        validate_requested_direction(profile, parsed.direction)
                except TTSError as exc:
                    tag_error = str(exc)
        if not (tag_error or legacy_native_voice) and (parsed.tagged or breeze_controls):
            profile = self.primary_tone_profile()
            if profile is not None:
                try:
                    validate_speech_arguments(
                        profile, {"text": response}, self.service.settings.max_text_length,
                        strip_parentheticals=features.strip_stage_directions,
                    )
                except TTSError as exc:
                    tag_error = str(exc)
        if tag_error and not legacy_native_voice and self.config.plugin.enabled:
            profile = self.primary_tone_profile()
            if profile is not None:
                revision = self.store.config.revision
                result = await correct_speech_arguments(
                    self.ctx, features, profile, {"text": response}, self.service.log,
                    automatic=True, max_text_length=self.service.settings.max_text_length,
                )
                if result.arguments is not None and self.store.config.revision == revision:
                    response = result.arguments["text"]
                    parsed = parse_tone_tag(response)
                    breeze = parse_breeze_text(parsed.text)
                    breeze_controls = profile.kind == "breeze" and breeze.marked
                    display_text = breeze.display_text if breeze_controls else parsed.text
                    tag_error = ""
                else:
                    tag_error = result.error or "语调纠正期间配置已变化，保留原文"
        if tag_error and not legacy_native_voice:
            self.service.log.warning("动态语调标签校验失败，保留纯文本", stream_id=session_id, error=tag_error)

        should_convert = False
        reason = "probability_disabled" if targeted else "not_probability_reply"
        if self.config.plugin.enabled and self.features.probability_enabled and targeted and session_id:
            self._remove_expired_probability_states()
            existing = self._probability_reply_states.get(reply_id)
            if existing is not None and not legacy_native_voice:
                # 同一回复的重复 Hook 调用继续使用已经确认的结果。
                should_convert = existing.stream_id == session_id and existing.should_convert
                reason = "existing_reply_decision"
            else:
                if legacy_native_voice:
                    reason = "legacy_native_voice"
                elif tag_error:
                    reason = "invalid_tone_tag"
                elif not display_text.strip():
                    reason = "empty_response"
                elif self._has_rich_reply_attachment(reply_tool_args):
                    reason = "rich_attachment"
                elif not self._is_probability_text_length_allowed(display_text.strip()):
                    reason = "text_length"
                else:
                    should_convert = self._sample_probability()
                    reason = "selected" if should_convert else "probability_miss"
                self._probability_reply_states[reply_id] = ProbabilityReplyState(
                    stream_id=session_id,
                    should_convert=should_convert,
                    expires_at=monotonic() + 600,
                    breeze_controls=breeze_controls,
                    operation_deadline=started + 540,
                )
        self.service.log.debug(
            "整轮语音判定",
            stream_id=session_id,
            reply_id=reply_id,
            convert=should_convert,
            text_length=len(display_text.strip()),
            reason=reason,
            probability=self.features.probability,
        )
        if not parsed.tagged and not breeze_controls and response == original_response:
            return {"action": "continue"}
        modified = dict(kwargs, session_id=session_id, reply_tool_args=reply_tool_args)
        if not should_convert and not display_text.strip():
            return {"action": "abort", "custom_result": {"reason": tag_error or "控制标记后缺少可见正文"}}
        modified["response"] = response if should_convert else display_text
        if should_convert and (parsed.tagged or breeze_controls):
            # 语调与局部事件随这一条回复交给扩展；正文保持完整，避免拆分或错字处理损坏控制标签。
            modified["skip_post_process"] = True
        return {"action": "continue", "modified_kwargs": modified}

    @ReplyExtension(
        "voice_reply",
        description="将本轮回复合成为语音发送（需在工作台开启“改用回复模型生成语音正文”；正文仍由回复模型按人设生成，默认直接传 {}）",
        parameters=[
            ToolParameterInfo(
                name="voice",
                param_type=ToolParamType.STRING,
                description="可选的临时音色名称；仅在对方明确指定音色时填写，否则省略",
                required=False,
            ),
            ToolParameterInfo(
                name="speed",
                param_type=ToolParamType.FLOAT,
                description="可选的临时语速倍率；仅在需要调整语速时填写，否则省略",
                required=False,
            ),
        ],
        timeout_ms=600000,
    )
    async def handle_voice_reply(
        self,
        phase: str,
        reply_id: str,
        session_id: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        text: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
        chat: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del text, chat, kwargs
        if phase == "prepare":
            # 平行回复补充器模式下宿主禁止在正文生成后经 prepare 返回 extra_prompt；
            # 动态语调提示统一在 maisaka.replyer.before_request 中按本轮参数条件注入。
            return {}
        if phase != "before_send":
            raise TTSError("未知的显式语音回复阶段")
        if not messages:
            raise TTSError("显式语音回复缺少待发送消息")
        self._remove_expired_probability_states()
        state = self._voice_reply_state_map().pop(reply_id, None)
        prompted = self._voice_reply_prompted_map().pop(reply_id, None) is not None
        pairs = [
            (original, clean)
            for original in messages
            for clean in self._clean_probability_messages([original], breeze=state is not None and state.breeze_controls)
        ]
        cleaned = [clean for _, clean in pairs]
        if not cleaned:
            raise TTSError("显式语音回复清理控制标记后缺少可见内容")
        if (
            not self.config.plugin.enabled
            or not self.features.tool_enabled
            or not self.features.tool_use_replyer
            or (state is not None and state.stream_id != session_id)
            or (state is not None and not state.should_convert)
        ):
            reason = (
                "plugin_disabled" if not self.config.plugin.enabled else
                "tool_disabled" if not self.features.tool_enabled else
                "voice_reply_disabled" if not self.features.tool_use_replyer else
                "reply_session_mismatch" if (state is not None and state.stream_id != session_id) else
                (state.skip_reason or "invalid_arguments")
            )
            if reason in {"plugin_disabled", "tool_disabled", "voice_reply_disabled", "legacy_native_voice"}:
                self.service.log.debug("跳过显式语音回复转换，保留原文字", stream_id=session_id, reply_id=reply_id, reason=reason)
            else:
                self.service.log.warning("跳过显式语音回复转换，保留原文字", stream_id=session_id, reply_id=reply_id, reason=reason)
            return {"messages": cleaned} if cleaned != messages else {}

        if state is not None:
            selected_voice = state.voice
            selected_speed = state.speed
        else:
            raw_voice, raw_speed = self._extract_voice_reply_overrides(parameters)
            selected_voice = self._normalize_voice_override(raw_voice)
            selected_speed = self._normalize_speed_override(raw_speed)
        tone_prefix = state.tone_prefix if state is not None else ""
        original_texts: List[Optional[str]] = []
        for original, _ in pairs:
            value = self._extract_probability_text(original["segments"])
            if value is not None and tone_prefix and not parse_tone_tag(value).tagged:
                value = tone_prefix + value
            original_texts.append(value)
        if not any(value is not None for value in original_texts):
            self.service.log.debug("跳过显式语音回复转换", stream_id=session_id, reply_id=reply_id, reason="no_convertible_text")
            return {"messages": cleaned} if cleaned != messages else {}

        converted = deepcopy(cleaned)
        try:
            budget = max(0, state.operation_deadline - monotonic()) if (state is not None and state.operation_deadline) else 540
            source_keys = await asyncio.wait_for(
                self._convert_probability_messages(
                    converted,
                    original_texts,
                    voice=selected_voice,
                    speed=selected_speed,
                    use_adaptive_tone=(
                        not prompted and self.features.probability_enabled and self.features.probability_tone_enabled
                    ),
                ),
                timeout=budget,
            )
        except (TTSError, asyncio.TimeoutError) as exc:
            error = "整轮语音合成超时" if isinstance(exc, asyncio.TimeoutError) else self._safe_error_message(exc)
            self.service.log.warning("显式语音回复失败，发送原文字", stream_id=session_id, reply_id=reply_id, error=error)
            return {"messages": cleaned} if cleaned != messages else {}
        except Exception as exc:
            self.service.log.unexpected("显式语音回复执行异常，保留原文", exc, stream_id=session_id, reply_id=reply_id)
            return {"messages": cleaned} if cleaned != messages else {}
        self.service.log.info(
            "已将显式语音回复交给发送链", stream_id=session_id, reply_id=reply_id, segments=len(converted)
        )
        for index, (original, message, text_value) in enumerate(zip(cleaned, converted, original_texts, strict=True)):
            if text_value is None:
                continue
            voice = next(segment for segment in message["segments"] if segment["type"] == "voice")
            key = (session_id, voice["hash"], voice["data"])
            self._probability_voice_states.setdefault(key, []).append(ProbabilityVoiceState(
                reply_id=reply_id,
                text_segments=deepcopy([segment for segment in original["segments"] if segment.get("type") == "text"]),
                expires_at=monotonic() + 600,
                source_key=source_keys[index],
                keep_text=self.features.tool_keep_text,
                quote_keep_voice=True,
            ))
        return {"messages": converted}

    @ReplyExtension(
        "probability_tts",
        description="按工作台概率将 LLM 回复转换为语音，保留整轮长度判定与引用关系",
        parameters=[],
        timeout_ms=600000,
    )
    async def handle_probability_tts(
        self,
        phase: str,
        reply_id: str,
        session_id: str = "",
        text: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
        chat: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del text, kwargs
        if phase == "prepare":
            self._remove_expired_probability_states()
            if isinstance(reply_id, str) and reply_id:
                self._probability_prepare_scope_map()[reply_id] = (
                    session_id,
                    self._is_probability_scope_allowed(chat),
                    monotonic() + 600,
                )
            return {}
        if phase != "before_send":
            raise TTSError("未知的概率语音回复阶段")
        if not messages:
            raise TTSError("概率语音回复缺少待发送消息")
        self._remove_expired_probability_states()
        self._probability_prepare_scope_map().pop(reply_id, None)
        # 先消费本次回复的判定，再执行耗时合成；并发回复与普通发送都无法借用这个结果。
        state = self._probability_reply_states.pop(reply_id, None)
        pairs = [
            (original, clean)
            for original in messages
            for clean in self._clean_probability_messages([original], breeze=state is not None and state.breeze_controls)
        ]
        cleaned = [clean for _, clean in pairs]
        if not cleaned:
            raise TTSError("语音回复清理控制标记后缺少可见内容")
        if (
            not self.config.plugin.enabled
            or not self.features.probability_enabled
            or state is None
            or state.stream_id != session_id
            or not state.should_convert
        ):
            reason = (
                "plugin_disabled" if not self.config.plugin.enabled else
                "probability_disabled" if not self.features.probability_enabled else
                "missing_reply_state" if state is None else
                "reply_session_mismatch" if state.stream_id != session_id else "probability_miss"
            )
            self.service.log.debug("跳过 LLM 回复语音转换", stream_id=session_id, reply_id=reply_id, reason=reason)
            return {"messages": cleaned} if cleaned != messages else {}
        scope_allowed = self._is_probability_scope_allowed(chat)
        if not scope_allowed:
            self.service.log.debug(
                "跳过 LLM 回复语音转换",
                reply_id=reply_id,
                reason="chat_scope",
            )
            return {"messages": cleaned} if cleaned != messages else {}

        # 概率只在正文生成后抽取一次；附件与引用限制按宿主已经拆好的每条消息处理。
        original_texts: List[Optional[str]] = []
        for index, (original, clean) in enumerate(pairs):
            value = self._extract_probability_text(original["segments"])
            explicitly_quoted = any(segment.get("type") == "reply" for segment in clean["segments"])
            # quote_previous 是宿主的引用意图，实际引用仍在发送阶段按聊天配置确定。
            if value is not None and not self.features.probability_quote_keep_text and explicitly_quoted:
                self.service.log.debug("保留引用段原文", reply_id=reply_id, segment_index=index, reason="quoted_text_only")
                value = None
            original_texts.append(value)
        if not any(value is not None for value in original_texts):
            self.service.log.debug("跳过 LLM 回复语音转换", reply_id=reply_id, reason="no_convertible_text")
            return {"messages": cleaned} if cleaned != messages else {}

        converted = deepcopy(cleaned)
        try:
            # 整组消息共享有界等待；全部合成完成后才交回 Host，失败时整轮保留文字。
            budget = max(0, state.operation_deadline - monotonic()) if state.operation_deadline else 540
            source_keys = await asyncio.wait_for(self._convert_probability_messages(converted, original_texts), timeout=budget)
        except (TTSError, asyncio.TimeoutError) as exc:
            error = "整轮语音合成超时" if isinstance(exc, asyncio.TimeoutError) else self._safe_error_message(exc)
            self.service.log.warning("自动语音失败，发送原文字", stream_id=session_id, reply_id=reply_id, error=error)
            return {"messages": cleaned} if cleaned != messages else {}
        except Exception as exc:
            self.service.log.unexpected("自动语音执行异常，保留原文", exc, stream_id=session_id, reply_id=reply_id)
            return {"messages": cleaned} if cleaned != messages else {}
        self.service.log.info(
            "已将 LLM 语音回复交给发送链", stream_id=session_id, reply_id=reply_id, segments=len(converted)
        )
        # Host 在回复扩展结束后才确定引用目标，因此在发送前按实际引用状态整理正文。
        # 仅登记整轮成功合成的自有音频，普通指令和其他插件的发送保持独立。
        for index, (original, message, text_value) in enumerate(zip(cleaned, converted, original_texts, strict=True)):
            if text_value is None:
                continue
            voice = next(segment for segment in message["segments"] if segment["type"] == "voice")
            key = (session_id, voice["hash"], voice["data"])
            self._probability_voice_states.setdefault(key, []).append(ProbabilityVoiceState(
                reply_id=reply_id,
                text_segments=deepcopy([segment for segment in original["segments"] if segment.get("type") == "text"]),
                expires_at=monotonic() + 600,
                source_key=source_keys[index],
            ))
        return {"messages": converted}

    @HookHandler(
        "send_service.before_send",
        name="arrange_probability_reply_quote",
        description="按实际引用方式分开发送概率语音的文字和语音，避免正文丢失或空引用",
        mode=HookMode.BLOCKING,
        timeout_ms=45000,
        error_policy=ErrorPolicy.ABORT,
    )
    async def arrange_probability_reply_quote(
        self, message: Optional[Dict[str, Any]] = None, set_reply: bool = False, **kwargs: Any
    ) -> Dict[str, Any]:
        if not isinstance(message, dict) or not isinstance(message.get("raw_message"), list):
            return {"action": "continue"}
        segments = message["raw_message"]
        voices = [segment for segment in segments if isinstance(segment, dict) and segment.get("type") == "voice"]
        if len(voices) != 1:
            return {"action": "continue"}
        voice = voices[0]
        key = (message.get("session_id"), voice.get("hash"), voice.get("data"))
        if not all(isinstance(value, str) for value in key):
            return {"action": "continue"}
        if key not in self._probability_voice_states:
            return {"action": "continue"}
        incoming = await asyncio.to_thread(source_key, segments)
        self._remove_expired_probability_states()
        pending = self._probability_voice_states.get(key)
        if not pending:
            return {"action": "continue"}
        index = next((index for index, state in enumerate(pending) if state.source_key == incoming), None)
        if index is None:
            return {"action": "continue"}
        state = pending.pop(index)
        if not pending:
            del self._probability_voice_states[key]
        quoted = set_reply or any(
            isinstance(segment, dict) and segment.get("type") == "reply" for segment in segments
        )
        quote_keep_voice = self.features.probability_quote_keep_text if state.quote_keep_voice is None else state.quote_keep_voice
        keep_text = self.features.probability_keep_text if state.keep_text is None else state.keep_text
        keep_voice = not quoted or quote_keep_voice
        if not keep_voice:
            # 自动引用与 quote_previous 由宿主稍后判定，沿用原来只发送引用正文的处理。
            arranged = [deepcopy(segment) for segment in segments if segment is not voice]
            if not any(segment.get("type") == "text" for segment in arranged):
                arranged.extend(deepcopy(state.text_segments))
            return {"action": "continue", "modified_kwargs": dict(
                kwargs, message=dict(message, raw_message=arranged, processed_plain_text=""), set_reply=set_reply,
            )}

        before, after = delivery_attachments(segments)
        if quoted or keep_text:
            arranged = deepcopy([segment for segment in segments if segment.get("type") == "text"])
            if not arranged:
                arranged = deepcopy(state.text_segments)
            references = [deepcopy(segment) for segment in segments if segment.get("type") == "reply"]
            if set_reply and not references:
                target = kwargs.get("reply_message_id")
                if not isinstance(target, str) or not target:
                    raise TTSError("引用语音回复缺少被引用消息 ID")
                references = [{"type": "reply", "data": {"target_message_id": target}}]
            before.append(references + arranged)
        # 自有音频状态已消费，附件与正文独立发送不会再次转换；语音保持宿主原始发送上下文。
        for batch in before:
            await self._send_tts_message(
                batch, key[0], batch_text(batch),
                storage_message=kwargs.get("storage_message", True),
                show_log=kwargs.get("show_log", True),
            )
        updated = dict(message, raw_message=[deepcopy(voice)], processed_plain_text=voice["data"],
                       is_at=False, is_emoji=False, is_picture=False)
        if after:
            token = uuid4().hex
            info = deepcopy(message.get("message_info", {}))
            additional = dict(info.get("additional_config") or {})
            additional[_DELIVERY_METADATA] = token
            info["additional_config"] = additional
            updated["message_info"] = info
            self._probability_attachment_states[token] = ProbabilityAttachmentState(
                key[0], after, monotonic() + 600,
                kwargs.get("storage_message", True), kwargs.get("show_log", True),
            )
        # reply_to 仍是 QQ 官方被动回复的发送上下文；原语音的目标和路由字段沿用宿主消息。
        self.service.log.debug(
            "已整理概率语音回复", stream_id=key[0], reply_id=state.reply_id,
            mode="text_and_voice" if quoted or keep_text else "voice_only",
        )
        return {"action": "continue", "modified_kwargs": dict(kwargs, message=updated, set_reply=False)}

    @HookHandler(
        "send_service.after_send", name="send_probability_reply_attachments",
        description="语音成功后继续发送同段原有的提及与表情，字段与顺序保持一致",
        mode=HookMode.BLOCKING, timeout_ms=45000,
    )
    async def send_probability_reply_attachments(
        self, message: Optional[Dict[str, Any]] = None, sent: bool = False, **kwargs: Any,
    ) -> Dict[str, Any]:
        del kwargs
        if not isinstance(message, dict):
            return {"action": "continue"}
        info = message.get("message_info", {})
        additional = info.get("additional_config", {}) if isinstance(info, dict) else {}
        token = additional.get(_DELIVERY_METADATA) if isinstance(additional, dict) else None
        if not isinstance(token, str):
            return {"action": "continue"}
        self._remove_expired_probability_states()
        state = self._probability_attachment_states.pop(token, None)
        if state is None or state.stream_id != message.get("session_id") or not sent:
            return {"action": "continue"}
        for batch in state.batches:
            await self._send_tts_message(
                batch, state.stream_id, batch_text(batch),
                storage_message=state.storage_message, show_log=state.show_log,
            )
        return {"action": "continue"}

    async def _convert_probability_messages(
        self,
        messages: List[Dict[str, Any]],
        original_texts: List[Optional[str]],
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        use_adaptive_tone: bool = True,
    ) -> Dict[int, str]:
        """合成目标正文并计算附件身份，全部完成后由调用方一次登记发送状态。"""
        source_keys: Dict[int, str] = {}
        adapter = None
        config = self.store.config
        features = config.feature
        if use_adaptive_tone and features.probability_tone_enabled:
            # 锚点的生成、校验与插入共用清洗后的正文，避免朗读文本与原文不一致时事件错位。
            originals = [value for value in original_texts if value is not None]
            joined_spoken = "\n".join(
                spoken_text_of(value, strip=features.strip_stage_directions) for value in originals
            )
            source_text = "\n".join(originals) if features.strip_stage_directions else ""
            adapter = ProbabilityToneAdapter(self.ctx, features, joined_spoken, self.service.log, source_text=source_text)
        for index, (message, original) in enumerate(zip(messages, original_texts, strict=True)):
            if original is None:
                continue
            audio = await self._synthesize_audio(
                text=original, voice=voice, speed=speed, tone_adapter=adapter, config=config,
            )
            # voice 的 data 是已知朗读正文，二进制由独立字段传递；Host 据此记录语音内容。
            display_text = audio.display_text if audio.display_text is not None else parse_tone_tag(original).text
            segment = await asyncio.to_thread(voice_segment, audio, display_text)
            source_keys[index] = await asyncio.to_thread(source_key, message["segments"])
            # 正文暂留在宿主分段中，供宿主决定首段引用；发送 Hook 再拆为独立文字与语音。
            message["segments"].append(segment)
        return source_keys

    @staticmethod
    def _extract_probability_text(segments: Any) -> Optional[str]:
        if not isinstance(segments, list) or not segments:
            return None
        return speech_text(segments)

    @staticmethod
    def _clean_probability_messages(messages: List[Dict[str, Any]], *, breeze: bool = False) -> List[Dict[str, Any]]:
        cleaned = deepcopy(messages)
        result: List[Dict[str, Any]] = []
        for message in cleaned:
            segments: List[Dict[str, Any]] = []
            pending: List[Dict[str, Any]] = []

            def flush() -> None:
                if not pending:
                    return
                text = "".join(segment["data"] for segment in pending)
                parsed = parse_tone_tag(text)
                display_text = parse_breeze_text(parsed.text).display_text if breeze else parsed.text
                if parsed.tagged or display_text != text:
                    if display_text.strip():
                        segments.append(dict(pending[0], data=display_text))
                else:
                    segments.extend(pending)
                pending.clear()

            for segment in message["segments"]:
                if segment.get("type") == "text":
                    pending.append(segment)
                else:
                    flush()
                    segments.append(segment)
            flush()
            if any(segment.get("type") != "reply" for segment in segments):
                message["segments"] = segments
                result.append(message)
        return result

    def _is_probability_text_length_allowed(self, text: str) -> bool:
        maximum = min(self.features.probability_max_text_length, self.config.runtime.max_text_length)
        return self.features.probability_min_text_length <= len(text) <= maximum

    def _sample_probability(self) -> bool:
        probability = self.features.probability
        return probability >= 1 or (probability > 0 and random() < probability)

    @staticmethod
    def _has_rich_reply_attachment(arguments: Any) -> bool:
        return isinstance(arguments, dict) and bool(arguments.get("attach_pic"))

    @staticmethod
    def _uses_legacy_native_voice(arguments: Any) -> bool:
        """判断宿主是否会在回复扩展之后强制执行本轮原生合成。"""
        if not isinstance(arguments, dict) or arguments.get("_prefer_reply_extension_voice") is True:
            return False
        modes = ("voice_only", "with_text")
        supplement = arguments.get("reply_supplement")
        return arguments.get("attach_voice") in modes or (
            isinstance(supplement, dict) and supplement.get("attach_voice") in modes
        )

    def _remove_expired_probability_states(self) -> None:
        now = monotonic()
        expired = [key for key, value in self._probability_reply_states.items() if value.expires_at <= now]
        for key in expired:
            del self._probability_reply_states[key]
        prepare_scopes = self._probability_prepare_scope_map()
        expired_scopes = [key for key, (_, _, expires_at) in prepare_scopes.items() if expires_at <= now]
        for key in expired_scopes:
            del prepare_scopes[key]
        voice_replies = self._voice_reply_state_map()
        expired_voice_replies = [key for key, value in voice_replies.items() if value.expires_at <= now]
        for key in expired_voice_replies:
            del voice_replies[key]
        prompted_ids = self._voice_reply_prompted_map()
        expired_prompted = [key for key, expires_at in prompted_ids.items() if expires_at <= now]
        for key in expired_prompted:
            del prompted_ids[key]
        expired_attachments = [key for key, value in self._probability_attachment_states.items() if value.expires_at <= now]
        for key in expired_attachments:
            del self._probability_attachment_states[key]
        for key, states in list(self._probability_voice_states.items()):
            active = [state for state in states if state.expires_at > now]
            if active:
                self._probability_voice_states[key] = active
            else:
                del self._probability_voice_states[key]

    def _invalidate_probability_decisions(self) -> None:
        # 配置保存后停用旧判定，但保留本轮标记识别状态，直到发送扩展清理正文。
        self._remove_expired_probability_states()
        for state in self._probability_reply_states.values():
            state.should_convert = False
        for state in self._voice_reply_state_map().values():
            state.should_convert = False
            if not state.skip_reason:
                state.skip_reason = "config_changed"
