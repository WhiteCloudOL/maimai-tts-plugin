"""仅在 LLM 回复扩展中执行整轮概率语音，按回复 ID 隔离状态。"""

from copy import deepcopy
from dataclasses import dataclass
from maibot_sdk import HookHandler, ReplyExtension
from maibot_sdk.types import HookMode
from random import random
from time import monotonic
from typing import Any, Dict, List, Optional

import asyncio

from ..domain.intonation import parse_tone_tag
from ..domain.models import TTSError
from ..infrastructure.voice_codec import voice_segment
from ..services.intonation import validate_requested_tone


@dataclass
class ProbabilityReplyState:
    """拆分前确认的整轮判定，只能由同一条 LLM 回复消费。"""

    stream_id: str
    should_convert: bool
    expires_at: float


class ProbabilityMixin:
    def _probability_extension_name(self) -> str:
        return f"{self.ctx.plugin_id}.probability_tts"

    def _is_probability_reply(self, arguments: Any) -> bool:
        if not isinstance(arguments, dict):
            return False
        options = arguments.get("plugin_options")
        return isinstance(options, dict) and self._probability_extension_name() in options

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
        timeout_ms=1000,
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
        parsed = parse_tone_tag(response)
        reply_id = reply_tool_args.get("_reply_id", "") if isinstance(reply_tool_args, dict) else ""
        targeted = self._is_probability_reply(reply_tool_args) and isinstance(reply_id, str) and bool(reply_id)
        tag_error = parsed.error
        if parsed.tone and not tag_error:
            profile = self.primary_tone_profile()
            if profile is None:
                tag_error = "整体语音路由尚待配置"
            else:
                try:
                    validate_requested_tone(profile, parsed.tone)
                except TTSError as exc:
                    tag_error = str(exc)
        if tag_error:
            self.service.log.warning("动态语调标签校验失败，保留纯文本", stream_id=session_id, error=tag_error)

        should_convert = False
        reason = "probability_disabled" if targeted else "not_probability_reply"
        if self.config.plugin.enabled and self.features.probability_enabled and targeted and session_id:
            self._remove_expired_probability_states()
            existing = self._probability_reply_states.get(reply_id)
            if existing is not None:
                # 同一回复的重复 Hook 调用继续使用已经确认的结果。
                should_convert = existing.stream_id == session_id and existing.should_convert
                reason = "existing_reply_decision"
            else:
                if tag_error:
                    reason = "invalid_tone_tag"
                elif not parsed.text.strip():
                    reason = "empty_response"
                elif self._has_rich_reply_attachment(reply_tool_args):
                    reason = "rich_attachment"
                elif not self._is_probability_text_length_allowed(parsed.text.strip()):
                    reason = "text_length"
                else:
                    should_convert = self._sample_probability()
                    reason = "selected" if should_convert else "probability_miss"
                self._probability_reply_states[reply_id] = ProbabilityReplyState(
                    stream_id=session_id,
                    should_convert=should_convert,
                    expires_at=monotonic() + 600,
                )
        self.service.log.debug(
            "整轮语音判定",
            stream_id=session_id,
            reply_id=reply_id,
            convert=should_convert,
            text_length=len(parsed.text.strip()),
            reason=reason,
            probability=self.features.probability,
        )
        if not parsed.tagged:
            return {"action": "continue"}
        modified = dict(kwargs, session_id=session_id, reply_tool_args=reply_tool_args)
        modified["response"] = response if should_convert else (parsed.text or "语音语调标签格式有误，请检查语调设置。")
        if should_convert:
            # 动态语调标签随这一条回复交给扩展；正文保持完整，避免拆分或错字处理损坏控制标签。
            modified["skip_post_process"] = True
        return {"action": "continue", "modified_kwargs": modified}

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
            return {}
        if phase != "before_send":
            raise TTSError("未知的概率语音回复阶段")
        if not messages:
            raise TTSError("概率语音回复缺少待发送消息")
        self._remove_expired_probability_states()
        # 先消费本次回复的判定，再执行耗时合成；并发回复与普通发送都无法借用这个结果。
        state = self._probability_reply_states.pop(reply_id, None)
        cleaned = self._clean_probability_messages(messages)
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
        group = chat.get("is_group_chat") if isinstance(chat, dict) else None
        scope_allowed = self.features.probability_group_enabled if group is True else (
            self.features.probability_private_enabled if group is False else False
        )
        original_texts = [self._extract_probability_text(item["segments"]) for item in messages]
        if not scope_allowed or any(value is None for value in original_texts):
            self.service.log.debug(
                "跳过 LLM 回复语音转换",
                reply_id=reply_id,
                reason="chat_scope" if not scope_allowed else "rich_attachment",
            )
            return {"messages": cleaned} if cleaned != messages else {}

        converted = deepcopy(cleaned)
        try:
            # 整组消息共享有界等待；全部合成完成后才交回 Host，失败时整轮保留文字。
            await asyncio.wait_for(self._convert_probability_messages(converted, original_texts), timeout=540)
        except (TTSError, asyncio.TimeoutError) as exc:
            error = "整轮语音合成超时" if isinstance(exc, asyncio.TimeoutError) else self._safe_error_message(exc)
            self.service.log.warning("自动语音失败，发送原文字", stream_id=session_id, reply_id=reply_id, error=error)
            return {"messages": cleaned} if cleaned != messages else {}
        self.service.log.info(
            "已将 LLM 语音回复交给发送链", stream_id=session_id, reply_id=reply_id, segments=len(converted)
        )
        return {"messages": converted}

    async def _convert_probability_messages(
        self, messages: List[Dict[str, Any]], original_texts: List[Optional[str]]
    ) -> None:
        keep_text = self.features.probability_keep_text
        for message, original in zip(messages, original_texts, strict=True):
            if original is None:
                raise TTSError("概率语音只处理纯文本回复")
            audio = await self._synthesize_audio(text=original)
            segment = await asyncio.to_thread(voice_segment, audio)
            # voice 的 data 是已知朗读正文，二进制由独立字段传递；Host 据此记录语音内容。
            segment["data"] = parse_tone_tag(original).text
            if keep_text:
                message["segments"].append(segment)
            else:
                message["segments"] = [segment]

    @staticmethod
    def _extract_probability_text(segments: Any) -> Optional[str]:
        if not isinstance(segments, list) or not segments:
            return None
        texts: List[str] = []
        for segment in segments:
            if (
                not isinstance(segment, dict)
                or segment.get("type") != "text"
                or not isinstance(segment.get("data"), str)
            ):
                return None
            texts.append(segment["data"])
        return "".join(texts).strip() or None

    @staticmethod
    def _clean_probability_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        cleaned = deepcopy(messages)
        for message in cleaned:
            for segment in message["segments"]:
                if segment.get("type") == "text":
                    parsed = parse_tone_tag(segment["data"])
                    if parsed.tagged:
                        segment["data"] = parsed.text or "语音语调标签格式有误，请检查语调设置。"
        return cleaned

    def _is_probability_text_length_allowed(self, text: str) -> bool:
        maximum = min(self.features.probability_max_text_length, self.config.runtime.max_text_length)
        return self.features.probability_min_text_length <= len(text) <= maximum

    def _sample_probability(self) -> bool:
        probability = self.features.probability
        return probability >= 1 or (probability > 0 and random() < probability)

    @staticmethod
    def _has_rich_reply_attachment(arguments: Any) -> bool:
        return isinstance(arguments, dict) and any(
            arguments.get(key) for key in ("attach_pic", "attach_emoji", "attach_at")
        )

    def _remove_expired_probability_states(self) -> None:
        now = monotonic()
        expired = [key for key, value in self._probability_reply_states.items() if value.expires_at <= now]
        for key in expired:
            del self._probability_reply_states[key]
