"""聊天命令、LLM 工具与整轮概率转换；保留原有触发语义。"""

from dataclasses import dataclass
from maibot_sdk import Command, HookHandler, Tool
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType
from random import random
from time import monotonic
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import re

from ..domain.models import Audio, Features, TTSError
from ..domain.intonation import parse_tone_tag
from ..services.intonation import clean_display_text, validate_requested_tone
from ..infrastructure.voice_codec import voice_segment
from ..infrastructure.diagnostics import trace_scope
from ..services.routing import RoutingService


PROBABILITY_REPLY_STATE_MIN_TTL_SECONDS = 60.0
PROBABILITY_REPLY_STATE_TIMEOUT_BUFFER_SECONDS = 30.0


@dataclass
class ProbabilityReplyState:
    """同一轮回复在分段发送期间复用的概率判定。"""

    should_convert: bool
    expires_at: float
    prepared_from_reply: bool
    started: bool = False


class MessagingMixin:
    @property
    def features(self) -> Features:
        return self.store.config.feature

    @Command(
        "mmtts_model",
        description="列出模型或切换整体首选 TTS 路由",
        pattern=r"^/mmtts\s+model(?:\s+(?P<target>.+?))?\s*$",
        permission="operator",
    )
    async def handle_model_command(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        self.service.log.info("收到模型管理命令", stream_id=stream_id)
        if not self.config.plugin.enabled:
            return False, "麦麦TTS语音 插件当前未启用", True
        if not stream_id:
            return False, "命令上下文缺少聊天流 ID", True
        groups = kwargs.get("matched_groups")
        target = groups.get("target", "") if isinstance(groups, dict) else ""
        target = target.strip() if isinstance(target, str) else ""
        routing = RoutingService(self.store)
        success = True
        try:
            page = re.fullmatch(r"(?:list|列表)(?:\s+(\d+))?", target)
            if page:
                page_value = page.group(1) or "1"
                if len(page_value) > 6:
                    raise TTSError("请使用模型列表中的页码")
                message = routing.describe(int(page_value))
            else:
                message = await routing.switch(target) if target else routing.describe()
        except TTSError as exc:
            success, message = False, self._safe_error_message(exc)
            self.service.log.warning("模型操作失败", stream_id=stream_id, error=message)
        if success:
            primary = self.store.config.primary
            self.service.log.info(
                "模型操作完成",
                action="switch" if target and not page else "list",
                stream_id=stream_id,
                provider_id=primary.provider_id if primary else "",
                model=primary.model_id if primary else "",
            )
        guard = (stream_id, message)
        self._control_reply_guards.add(guard)
        try:
            try:
                sent = await self.ctx.send.text(message, stream_id)
            except Exception as exc:
                self.service.log.unexpected("模型状态消息发送异常", exc, stream_id=stream_id)
                return False, "模型状态消息发送失败，请在工作台核对当前路由", True
        finally:
            self._control_reply_guards.discard(guard)
        if not sent:
            self.service.log.error("模型状态消息发送失败", stream_id=stream_id)
            return False, "模型操作已完成，状态消息发送失败，请在工作台核对", True
        return success, message, True

    @Command(
        "openai_tts",
        description="把文本合成为语音消息",
        pattern=r"^/(?:tts|语音)(?:\s+(?P<text>[\s\S]+))?\s*$",
        aliases=["tts", "语音"],
    )
    async def handle_tts_command(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        """处理 ``/tts <文本>`` 与 ``/语音 <文本>`` 命令。"""

        self.service.log.info("收到语音命令", stream_id=stream_id)
        if not self.config.plugin.enabled:
            self.service.log.debug("跳过语音命令", reason="plugin_disabled", stream_id=stream_id)
            return False, "麦麦TTS语音 插件当前未启用", True
        if not self.features.command_enabled:
            self.service.log.debug("跳过语音命令", reason="command_disabled", stream_id=stream_id)
            return False, "TTS 命令当前未启用", True
        if not stream_id:
            self.service.log.error("语音命令缺少聊天流")
            return False, "命令上下文缺少聊天流 ID", True

        matched_groups = kwargs.get("matched_groups")
        text = ""
        if isinstance(matched_groups, dict):
            matched_text = matched_groups.get("text")
            if isinstance(matched_text, str):
                text = matched_text.strip()
        if not text:
            self.service.log.warning("语音命令缺少文本", stream_id=stream_id)
            return False, "用法：/tts <要合成的文本>", True

        try:
            await self._synthesize_and_send(text=text, stream_id=stream_id)
        except TTSError as exc:
            error_message = self._safe_error_message(exc)
            self.service.log.warning("语音命令失败", stream_id=stream_id, error=error_message)
            return False, error_message, True
        return True, "语音已发送", True

    @Tool(
        "mmtts_speak",
        timeout_ms=600000,
        brief_description="把文本合成为语音并发送到当前聊天流",
        detailed_description=(
            "需要把完整回复文本转换为语音时调用；text 应传入原本要回复给用户的完整正文。参数说明：\n"
            "- text：string，必填，要转换为语音的完整回复文本。\n"
            "- voice：string，可选，用户明确指定临时音色时填写，日常回复沿用整体路由。\n"
            "- speed：number，可选，0.25 到 4.0；是否允许由插件配置决定。\n"
            "直接传入朗读正文，语调按当前路由配置及本次工具说明执行。\n"
            "成功结果表示语音已经发送到聊天流，本轮回复随即完成。\n"
            "聊天流 ID 由 MaiBot 工具执行上下文自动注入，不需要也不允许模型自行填写。"
        ),
        parameters=[
            ToolParameterInfo(
                name="text",
                param_type=ToolParamType.STRING,
                description="要合成为语音的文本",
                required=True,
            ),
            ToolParameterInfo(
                name="voice",
                param_type=ToolParamType.STRING,
                description="可选的临时音色名称",
                required=False,
            ),
            ToolParameterInfo(
                name="speed",
                param_type=ToolParamType.FLOAT,
                description="可选的临时语速，范围 0.25 到 4.0",
                required=False,
            ),
        ],
    )
    async def handle_tts_tool(
        self,
        text: str,
        stream_id: str = "",
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """供 LLM 调用文本转语音能力。"""

        del kwargs
        if not isinstance(text, str):
            self.service.log.warning("语音工具文本类型无效", stream_id=stream_id)
            return {"success": False, "error": "text 参数应为朗读文本字符串"}
        self.service.log.info("收到语音工具调用", stream_id=stream_id, text_length=len(text))
        if not self.config.plugin.enabled:
            self.service.log.debug("跳过语音工具", reason="plugin_disabled")
            return {"success": False, "error": "麦麦TTS语音 插件当前未启用"}
        if not self.features.tool_enabled:
            self.service.log.debug("跳过语音工具", reason="tool_disabled")
            return {"success": False, "error": "麦麦TTS语音 工具当前未启用"}
        normalized_stream_id = stream_id.strip()
        if not normalized_stream_id:
            self.service.log.error("语音工具缺少真实聊天流")
            return {"success": False, "error": "工具执行上下文缺少真实聊天流 ID"}

        selected_voice = voice if self.features.allow_voice_override else None
        selected_speed = speed if self.features.allow_speed_override else None
        try:
            audio = await self._synthesize_and_send(
                text=text,
                stream_id=normalized_stream_id,
                voice=selected_voice,
                speed=selected_speed,
                include_text=self.features.tool_keep_text,
            )
        except TTSError as exc:
            error_message = self._safe_error_message(exc)
            self.service.log.warning("语音工具失败", stream_id=normalized_stream_id, error=error_message)
            return {"success": False, "error": error_message}
        return {
            "success": True,
            "message": "语音已发送",
            "content": f"语音已成功发送到当前聊天流，本轮回复已完成。朗读内容：{clean_display_text(text)}",
            "sent": True,
            "stop_after_execution": True,
            "audio_bytes": len(audio.data),
        }

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="prepare_probability_tts_reply",
        description="在 MaiBot 拆分回复前生成整轮一致的概率 TTS 判定",
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
        """在回复拆分前缓存一次判定，供随后所有文本分段复用。"""

        del hook_name
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            self.service.log.debug("跳过整轮语音判定", reason="missing_stream")
            return {"action": "continue"}

        parsed = parse_tone_tag(response)
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
            self.service.log.warning(
                "动态语调标签校验失败，保留纯文本", stream_id=normalized_session_id, error=tag_error
            )
        self._remove_expired_probability_states()
        should_convert = False
        reason = "plugin_disabled" if not self.config.plugin.enabled else "probability_disabled"
        if self.config.plugin.enabled and self.features.probability_enabled:
            normalized_response = parsed.text.strip()
            has_rich_attachment = self._has_rich_reply_attachment(reply_tool_args)
            if tag_error:
                reason = "invalid_tone_tag"
            elif not normalized_response:
                reason = "empty_response"
            elif has_rich_attachment:
                reason = "rich_attachment"
            elif not self._is_probability_text_length_allowed(normalized_response):
                reason = "text_length"
            else:
                should_convert = self._sample_probability()
                reason = "selected" if should_convert else "probability_miss"

        self._probability_reply_states[normalized_session_id] = ProbabilityReplyState(
            should_convert=should_convert,
            expires_at=monotonic() + self._probability_state_ttl_seconds(),
            prepared_from_reply=True,
        )
        self.service.log.debug(
            "整轮语音判定",
            stream_id=normalized_session_id,
            convert=should_convert,
            text_length=len(response.strip()),
            reason=reason,
            probability=self.features.probability,
        )
        if parsed.tagged:
            modified = dict(kwargs)
            modified.update(
                response=response if should_convert else (parsed.text or "语音语调标签格式有误，请检查语调设置。"),
                session_id=session_id,
                reply_tool_args=reply_tool_args,
            )
            if should_convert:
                # 头部控制标签只在本次单条出站消息中存活，免于分段/错字处理，也无需跨会话缓存语调。
                modified["skip_post_process"] = True
            return {"action": "continue", "modified_kwargs": modified}
        return {"action": "continue"}

    @HookHandler(
        "send_service.after_build_message",
        name="probability_tts",
        description="按配置概率把纯文本出站消息原位转换为语音消息",
        mode=HookMode.BLOCKING,
        timeout_ms=600000,
    )
    async def handle_probability_tts(
        self,
        message: Any = None,
        stream_id: str = "",
        typing: bool = False,
        hook_name: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """在消息进入 adapter 前，按整轮一致的判定替换纯文本。"""

        del hook_name
        original_text = self._extract_probability_text(message)
        parsed = parse_tone_tag(original_text) if original_text is not None else None
        continuation = {"action": "continue"}
        if parsed is not None and parsed.tagged:
            message = dict(message)
            message["raw_message"] = [{"type": "text", "data": parsed.text or "语音语调标签格式有误，请检查语调设置。"}]
            message["processed_plain_text"] = parsed.text
            modified = dict(kwargs, message=message, stream_id=stream_id, typing=typing)
            continuation["modified_kwargs"] = modified
            if parsed.error:
                self.service.log.warning("出站语调标签校验失败，保留纯文本", stream_id=stream_id, error=parsed.error)
                return continuation
        if not self.config.plugin.enabled or not self.features.probability_enabled:
            self.service.log.debug(
                "跳过出站语音转换",
                stream_id=stream_id,
                reason="plugin_disabled" if not self.config.plugin.enabled else "probability_disabled",
            )
            return continuation

        normalized_stream_id = stream_id.strip()
        if not normalized_stream_id and isinstance(message, dict):
            message_session_id = message.get("session_id")
            if isinstance(message_session_id, str):
                normalized_stream_id = message_session_id.strip()
        if not normalized_stream_id:
            self.service.log.debug("跳过出站语音转换", reason="missing_stream")
            return continuation

        text = self._extract_probability_text(message)
        if text is not None and (normalized_stream_id, text) in self._control_reply_guards:
            self.service.log.debug("跳过出站语音转换", stream_id=normalized_stream_id, reason="control_reply")
            return continuation
        if text is None or not self._is_probability_chat_scope_allowed(message):
            self.service.log.debug(
                "跳过出站语音转换",
                stream_id=normalized_stream_id,
                reason="non_plain_text" if text is None else "chat_scope",
            )
            self._disable_probability_state_for_segment(normalized_stream_id, typing)
            return continuation

        state = self._get_probability_state(normalized_stream_id)
        prepared_from_reply = state is not None and state.prepared_from_reply and not state.started
        if not prepared_from_reply and not self._is_probability_text_length_allowed(text):
            self.service.log.debug(
                "跳过出站语音转换", stream_id=normalized_stream_id, reason="text_length", text_length=len(text)
            )
            self._disable_probability_state_for_segment(normalized_stream_id, typing)
            return continuation

        if not self._resolve_probability_decision(normalized_stream_id, typing):
            self.service.log.debug(
                "跳过出站语音转换",
                stream_id=normalized_stream_id,
                reason="probability_miss",
                probability=self.features.probability,
            )
            return continuation

        self.service.log.info(
            "自动语音命中", stream_id=normalized_stream_id, text_length=len(text), probability=self.features.probability
        )
        try:
            synthesized = await self._synthesize_audio(
                text=original_text if parsed is not None and parsed.tagged else text
            )
        except TTSError as exc:
            self._set_probability_state_to_text(normalized_stream_id)
            self.service.log.warning(
                "自动语音失败，发送原文字", stream_id=normalized_stream_id, error=self._safe_error_message(exc)
            )
            return continuation

        segment = await asyncio.to_thread(voice_segment, synthesized)
        modified_message = dict(message)
        if self.features.probability_keep_text:
            modified_message["raw_message"] = [dict(segment) for segment in message["raw_message"]]
            modified_message["raw_message"].append(segment)
        else:
            modified_message["raw_message"] = [segment]
        modified_message["processed_plain_text"] = text
        modified_message["is_emoji"] = False
        modified_message["is_picture"] = False
        modified_message["is_command"] = False

        modified_kwargs = dict(kwargs)
        modified_kwargs.update(
            {
                "message": modified_message,
                "stream_id": normalized_stream_id,
                "typing": typing,
            }
        )
        self.service.log.info(
            "已将语音交给发送链",
            stream_id=normalized_stream_id,
            text_length=len(text),
            audio_bytes=len(synthesized.data),
            format=synthesized.response_format,
        )
        return {
            "action": "continue",
            "modified_kwargs": modified_kwargs,
        }

    async def _synthesize_and_send(
        self,
        *,
        text: str,
        stream_id: str,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        include_text: bool = False,
    ) -> Audio:
        with trace_scope():
            synthesized = await self._synthesize_audio(text=text, voice=voice, speed=speed)
            message_segments: List[Dict[str, Any]] = []
            if include_text:
                message_segments.append({"type": "text", "content": clean_display_text(text)})
            message_segments.append(await asyncio.to_thread(voice_segment, synthesized))
            started = monotonic()
            self.service.log.info(
                "发送语音开始",
                stream_id=stream_id,
                format=synthesized.response_format,
                audio_bytes=len(synthesized.data),
                include_text=include_text,
            )
            try:
                sent = await self.ctx.send.hybrid(
                    message_segments,
                    stream_id,
                    processed_plain_text=clean_display_text(text),
                    return_details=True,
                    # 官方适配器无需依赖消息回流即可记录自己的语音；沿用宿主的自身回复标识。
                    sync_to_maisaka_history=True,
                    maisaka_source_kind="guided_reply",
                )
            except Exception as exc:
                self.service.log.unexpected("发送语音调用异常", exc, stream_id=stream_id)
                raise TTSError(f"语音发送调用失败（{type(exc).__name__}），请查看适配器日志") from None
            success = sent.get("sent") is True if isinstance(sent, dict) else sent is True
            elapsed_ms = round((monotonic() - started) * 1000)
            if not success:
                self.service.log.error("发送语音失败", stream_id=stream_id, elapsed_ms=elapsed_ms)
                raise TTSError("语音已合成，但发送到聊天流失败")
            self.service.log.info(
                "发送语音成功", stream_id=stream_id, audio_bytes=len(synthesized.data), elapsed_ms=elapsed_ms
            )
            return synthesized

    async def _synthesize_audio(
        self,
        *,
        text: str,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
    ) -> Audio:
        """合成音频并按工作台选项准备聊天语音格式。"""
        with trace_scope():
            delivery_format = self.features.delivery_format
            audio = await self.service.synthesize(
                text, voice=voice, speed=speed, delivery_format=delivery_format
            )
            return await self.service.prepare_delivery(audio, delivery_format)

    @staticmethod
    def _extract_probability_text(message: Any) -> Optional[str]:
        """合并一条出站消息中的全部纯文本段。"""

        if not isinstance(message, dict):
            return None
        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list) or not raw_message:
            return None
        text_segments: List[str] = []
        for segment in raw_message:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                return None
            text = segment.get("data")
            if not isinstance(text, str):
                return None
            text_segments.append(text)
        normalized_text = "".join(text_segments).strip()
        return normalized_text or None

    def _is_probability_text_length_allowed(self, text: str) -> bool:
        """检查整轮回复或独立出站文本是否处于概率模式长度范围。"""

        minimum_length = self.features.probability_min_text_length
        maximum_length = min(
            self.features.probability_max_text_length,
            self.config.runtime.max_text_length,
        )
        return minimum_length <= len(text) <= maximum_length

    def _is_probability_chat_scope_allowed(self, message: Any) -> bool:
        """检查当前出站消息所属的群聊或私聊范围是否已启用。"""

        message_info = message.get("message_info") if isinstance(message, dict) else None
        group_info = message_info.get("group_info") if isinstance(message_info, dict) else None
        if group_info is None:
            return self.features.probability_private_enabled
        return self.features.probability_group_enabled

    def _sample_probability(self) -> bool:
        """按配置概率生成一次布尔判定。"""

        probability = self.features.probability
        if probability <= 0.0:
            return False
        if probability >= 1.0:
            return True
        return random() < probability

    @staticmethod
    def _has_rich_reply_attachment(reply_tool_args: Any) -> bool:
        """判断本轮回复是否请求了图片、表情或 At 等非纯文本附件。"""

        if not isinstance(reply_tool_args, dict):
            return False
        return any(reply_tool_args.get(key) for key in ("attach_pic", "attach_emoji", "attach_at"))

    def _remove_expired_probability_states(self) -> None:
        """清理已经超过分段发送窗口的概率判定。"""

        current_time = monotonic()
        expired_stream_ids = [
            stream_id for stream_id, state in self._probability_reply_states.items() if state.expires_at <= current_time
        ]
        for stream_id in expired_stream_ids:
            del self._probability_reply_states[stream_id]

    def _probability_state_ttl_seconds(self) -> float:
        """让分段判定至少覆盖一次完整 TTS 请求及额外调度时间。"""

        return max(
            PROBABILITY_REPLY_STATE_MIN_TTL_SECONDS,
            min(540, self.config.runtime.timeout_seconds * (1 + len(self.store.config.fallback_providers)))
            + PROBABILITY_REPLY_STATE_TIMEOUT_BUFFER_SECONDS,
        )

    def _get_probability_state(self, stream_id: str) -> Optional[ProbabilityReplyState]:
        """取得当前聊天流仍有效的整轮概率判定。"""

        self._remove_expired_probability_states()
        return self._probability_reply_states.get(stream_id)

    def _resolve_probability_decision(self, stream_id: str, typing: bool) -> bool:
        """为当前分段取得或创建整轮一致的概率判定。"""

        state = self._get_probability_state(stream_id)
        if state is not None and state.prepared_from_reply and not state.started:
            state.started = True
            return state.should_convert
        if typing and state is not None and state.started:
            return state.should_convert

        state = ProbabilityReplyState(
            should_convert=self._sample_probability(),
            expires_at=monotonic() + self._probability_state_ttl_seconds(),
            prepared_from_reply=False,
            started=True,
        )
        self._probability_reply_states[stream_id] = state
        return state.should_convert

    def _disable_probability_state_for_segment(self, stream_id: str, typing: bool) -> None:
        """遇到不支持的分段时让本轮后续分段继续使用文字。"""

        state = self._get_probability_state(stream_id)
        if state is None and not typing:
            return
        self._probability_reply_states[stream_id] = ProbabilityReplyState(
            should_convert=False,
            expires_at=monotonic() + self._probability_state_ttl_seconds(),
            prepared_from_reply=False,
            started=True,
        )

    def _set_probability_state_to_text(self, stream_id: str) -> None:
        """合成或转码失败后阻止本轮剩余分段继续尝试语音。"""

        self._probability_reply_states[stream_id] = ProbabilityReplyState(
            should_convert=False,
            expires_at=monotonic() + self._probability_state_ttl_seconds(),
            prepared_from_reply=False,
            started=True,
        )

    def _safe_error_message(self, error: TTSError) -> str:
        """生成可记录、可展示且不包含本插件凭据的错误信息。"""
        return self.store.redact(str(error))
