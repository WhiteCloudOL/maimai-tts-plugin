"""聊天命令与显式 LLM 语音工具。"""

from dataclasses import dataclass
from maibot_sdk import Command, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType
from time import monotonic
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import re

from ..domain.models import Audio, Features, TTSError
from ..domain.adaptive_tone import ToneAdapter
from ..domain.prompts import DEFAULT_PROMPTS
from ..services.intonation import clean_display_text
from ..infrastructure.voice_codec import voice_segment
from ..infrastructure.diagnostics import trace_scope
from ..services.routing import RoutingService
from ..services.format_correction import correct_speech_arguments


@dataclass
class SpeechSendState:
    """同一次工具调用的已确认投递回执，跨异常与超时保留发送状态。"""

    text_sent: bool = False
    voice_sent: bool = False


# Host 将提及渲染为 @昵称；保留开头边界，只允许提及和空白出现在命令前。
COMMAND_PREFIX_PATTERN = r"^\s*(?:@[^\s@/]+\s*)*"


class MessagingMixin:
    @property
    def features(self) -> Features:
        return self.store.config.feature

    @Command(
        "mmtts_model",
        description="列出模型或切换整体首选 TTS 路由",
        pattern=COMMAND_PREFIX_PATTERN + r"/mmtts\s+model(?:\s+(?P<target>.+?))?\s*$",
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
        if not success:
            return False, message, True
        if success:
            primary = self.store.config.primary
            self.service.log.info(
                "模型操作完成",
                action="switch" if target and not page else "list",
                stream_id=stream_id,
                provider_id=primary.provider_id if primary else "",
                model=primary.model_id if primary else "",
            )
        try:
            sent = await self.ctx.send.text(message, stream_id)
        except Exception as exc:
            self.service.log.unexpected("模型状态消息发送异常", exc, stream_id=stream_id)
            return False, "模型状态消息发送失败，请在工作台核对当前路由", True
        if not sent:
            self.service.log.error("模型状态消息发送失败", stream_id=stream_id)
            return False, "模型操作已完成，状态消息发送失败，请在工作台核对", True
        return success, message, True

    @Command(
        "mmtts",
        description="把文本合成为语音消息",
        pattern=COMMAND_PREFIX_PATTERN + r"/(?:mmtts(?!\s+model(?:\s|$))|tts|语音)(?:\s+(?P<text>[\s\S]+))?\s*$",
        timeout_ms=600000,
    )
    async def handle_tts_command(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        """处理 ``/mmtts <文本>``、``/tts <文本>`` 与 ``/语音 <文本>``。"""

        self.service.log.info("收到语音命令", stream_id=stream_id)
        if not self.config.plugin.enabled:
            self.service.log.debug("跳过语音命令", reason="plugin_disabled", stream_id=stream_id)
            return await self._tts_command_failure(stream_id, "请先启用麦麦TTS语音插件")
        if not self.features.command_enabled:
            self.service.log.debug("跳过语音命令", reason="command_disabled", stream_id=stream_id)
            return await self._tts_command_failure(stream_id, "请在语音工作台的“通用设置”中启用聊天命令")
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
            return await self._tts_command_failure(stream_id, "用法：/mmtts <要合成的文本>，也支持 /tts 和 /语音")

        try:
            await self._synthesize_and_send(text=text, stream_id=stream_id)
        except TTSError as exc:
            error_message = self._safe_error_message(exc)
            self.service.log.warning("语音命令失败", stream_id=stream_id, error=error_message)
            return await self._tts_command_failure(stream_id, "语音发送失败：" + error_message)
        return True, "语音已发送", True

    async def _tts_command_failure(self, stream_id: str, message: str) -> Tuple[bool, str, bool]:
        """命令诊断供 Host 记录，发送链仅投递业务正文与音频。"""
        self.service.log.warning("语音命令结束", stream_id=stream_id, error=message)
        return False, message, True

    @Tool(
        "mmtts_speak",
        timeout_ms=600000,
        brief_description="把文本合成为语音并发送到当前聊天流",
        detailed_description=DEFAULT_PROMPTS["tool"]["zh"],
        parameters=[
            ToolParameterInfo(
                name="text",
                param_type=ToolParamType.STRING,
                description="完整朗读文本；语调控制语法以当前平台的工具说明为准",
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
                description="可选的临时语速，取值与可用性以当前平台和插件设置为准",
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
        self.service.log.info("收到语音工具调用", stream_id=stream_id,
                              text_length=len(text) if isinstance(text, str) else 0)
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
        send_state = SpeechSendState()
        try:
            return await asyncio.wait_for(
                self._execute_tts_tool(text, normalized_stream_id, selected_voice, selected_speed, send_state), timeout=540,
            )
        except asyncio.TimeoutError:
            error = "语音工具处理超过 540 秒总预算"
            self.service.log.warning("语音工具超时", stream_id=normalized_stream_id, error=error)
            return await self._tool_text_result(TTSError(error), text, normalized_stream_id, send_state)
        except TTSError as exc:
            return await self._tool_text_result(exc, text, normalized_stream_id, send_state)
        except Exception as exc:
            self.service.log.unexpected("语音工具执行异常", exc, stream_id=normalized_stream_id)
            return await self._tool_text_result(
                TTSError(f"语音工具执行异常（{type(exc).__name__}）"), text, normalized_stream_id, send_state,
            )

    async def _tool_text_result(
        self, error: TTSError, text: str, stream_id: str, send_state: SpeechSendState,
    ) -> Dict[str, Any]:
        """向 LLM 返回诊断，向聊天保留一次原始可见正文。"""
        message = self._safe_error_message(error)
        self.service.log.warning("语音工具失败", stream_id=stream_id, error=message)
        config = self.store.config
        primary = config.primary
        breeze = primary is not None and any(
            profile.id == primary.provider_id and profile.kind == "breeze" for profile in config.profiles
        )
        display_text = clean_display_text(text, breeze=breeze) if isinstance(text, str) else ""
        if display_text and not (send_state.text_sent or send_state.voice_sent):
            try:
                await self._send_tts_message([{"type": "text", "data": display_text}], stream_id, display_text)
                send_state.text_sent = True
            except TTSError as exc:
                self.service.log.warning("语音工具原文投递失败", stream_id=stream_id, error=self._safe_error_message(exc))
        sent = send_state.text_sent or send_state.voice_sent
        return {"success": False, "error": message, "sent": sent, "stop_after_execution": sent}

    async def _execute_tts_tool(
        self, text: str, stream_id: str, voice: Optional[str], speed: Optional[float], send_state: SpeechSendState,
    ) -> Dict[str, Any]:
        """先纠正可修正的输入格式，再执行一次合成与投递。"""
        config = self.store.config
        correction_error = ""
        if config.primary is not None:
            target = config.primary
            profile = self.store.profile(target.provider_id)
            if target.model_id:
                profile = profile.for_model(target.model_id)
            profile = profile.with_synthesis_overrides(voice=target.voice).with_selected_voice()
            arguments: Dict[str, Any] = {"text": text}
            if voice is not None:
                arguments["voice"] = voice
            if speed is not None:
                arguments["speed"] = speed
            result = await correct_speech_arguments(
                self.ctx, self.features, profile, arguments, self.service.log,
                automatic=False, max_text_length=self.service.settings.max_text_length,
            )
            if result.arguments is not None and self.store.config.revision == config.revision:
                text = result.arguments["text"]
                voice, speed = result.arguments.get("voice"), result.arguments.get("speed")
            else:
                correction_error = result.error or "语调纠正期间配置已变化，保留原文"
        if correction_error:
            raise TTSError(correction_error)
        audio = await self._synthesize_and_send(
            text=text, stream_id=stream_id, voice=voice, speed=speed,
            include_text=self.features.tool_keep_text, send_state=send_state,
        )
        return {
            "success": True,
            "message": "语音已发送",
            "content": f"语音已成功发送到当前聊天流，本轮回复已完成。朗读内容：{audio.display_text if audio.display_text is not None else clean_display_text(text)}",
            "sent": True,
            "stop_after_execution": True,
            "audio_bytes": len(audio.data),
        }

    async def _synthesize_and_send(
        self,
        *,
        text: str,
        stream_id: str,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        include_text: bool = False,
        send_state: Optional[SpeechSendState] = None,
    ) -> Audio:
        with trace_scope():
            synthesized = await self._synthesize_audio(text=text, voice=voice, speed=speed)
            display_text = synthesized.display_text if synthesized.display_text is not None else clean_display_text(text)
            segment = await asyncio.to_thread(voice_segment, synthesized)
            started = monotonic()
            self.service.log.info(
                "发送语音开始",
                stream_id=stream_id,
                format=synthesized.response_format,
                audio_bytes=len(synthesized.data),
                include_text=include_text,
            )
            # QQ 的语音消息可能隐藏同条消息中的文字，按顺序单独投递并核对两次回执。
            if include_text and display_text:
                await self._send_tts_message(
                    [{"type": "text", "data": display_text}], stream_id, display_text,
                )
                if send_state is not None:
                    send_state.text_sent = True
            await self._send_tts_message([segment], stream_id, display_text)
            if send_state is not None:
                send_state.voice_sent = True
            elapsed_ms = round((monotonic() - started) * 1000)
            self.service.log.info(
                "发送语音成功", stream_id=stream_id, audio_bytes=len(synthesized.data), elapsed_ms=elapsed_ms
            )
            return synthesized

    async def _send_tts_message(
        self,
        segments: List[Dict[str, Any]],
        stream_id: str,
        processed_plain_text: str,
        *,
        storage_message: bool = True,
        show_log: bool = True,
    ) -> Dict[str, Any]:
        """单独发送文字或语音，失败时让调用方终止后续投递。"""
        kind = "语音" if any(segment["type"] == "voice" for segment in segments) else "文字"
        try:
            sent = await self.ctx.send.hybrid(
                segments,
                stream_id,
                processed_plain_text=processed_plain_text,
                storage_message=storage_message,
                show_log=show_log,
                return_details=True,
                # 官方适配器无需依赖消息回流即可记录自身回复；分别记录文字与语音回执。
                sync_to_maisaka_history=True,
                maisaka_source_kind="guided_reply",
            )
        except Exception as exc:
            self.service.log.unexpected(f"发送{kind}调用异常", exc, stream_id=stream_id)
            raise TTSError(f"{kind}发送调用失败（{type(exc).__name__}），请查看适配器日志") from None
        success = sent.get("sent") is True if isinstance(sent, dict) else sent is True
        if not success:
            self.service.log.error(f"发送{kind}失败", stream_id=stream_id)
            raise TTSError(f"{kind}发送到聊天流失败")
        return {"sent": True, "message_id": sent.get("message_id") if isinstance(sent, dict) else None}

    async def _synthesize_audio(
        self,
        *,
        text: str,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        tone_adapter: Optional[ToneAdapter] = None,
    ) -> Audio:
        """合成音频并按工作台选项准备聊天语音格式。"""
        with trace_scope():
            delivery_format = self.features.delivery_format
            options: Dict[str, Any] = {}
            if tone_adapter is not None:
                options["tone_adapter"] = tone_adapter
            audio = await self.service.synthesize(
                text, voice=voice, speed=speed, delivery_format=delivery_format, **options
            )
            return await self.service.prepare_delivery(audio, delivery_format)

    def _safe_error_message(self, error: TTSError) -> str:
        """生成可记录、可展示且不包含本插件凭据的错误信息。"""
        return self.store.redact(str(error))
