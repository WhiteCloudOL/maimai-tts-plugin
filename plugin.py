"""为 MaiBot 提供 OpenAI 兼容的文本转语音能力。"""

from dataclasses import dataclass
from random import random
from time import monotonic
from typing import Any, Dict, List, Optional, Tuple

import base64

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType
from pydantic import model_validator

from .core.client import (
    OpenAITTSClient,
    SynthesizedAudio,
    TTSClientError,
    TTSClientSettings,
    TTSConfigurationError,
)


PROBABILITY_REPLY_STATE_MIN_TTL_SECONDS = 60.0
PROBABILITY_REPLY_STATE_TIMEOUT_BUFFER_SECONDS = 30.0


@dataclass
class ProbabilityReplyState:
    """同一轮回复在分段发送期间复用的概率判定。"""

    should_convert: bool
    expires_at: float
    prepared_from_reply: bool
    started: bool = False


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用 OpenAI TTS 插件",
        json_schema_extra={"label": "启用插件", "order": 0},
    )
    config_version: str = Field(
        default="1.2.1",
        description="配置版本",
        json_schema_extra={"label": "配置版本", "order": 1, "readonly": True},
    )


class APIConfig(PluginConfigBase):
    """OpenAI 兼容接口配置。"""

    __ui_label__ = "接口"
    __ui_icon__ = "cloud"
    __ui_order__ = 1

    base_url: str = Field(
        default="https://api.openai.com/v1",
        description="OpenAI 兼容服务的 Base URL",
        json_schema_extra={
            "label": "Base URL",
            "hint": "示例：https://api.openai.com/v1；结尾斜杠会自动处理",
            "order": 0,
        },
    )
    endpoint_path: str = Field(
        default="/audio/speech",
        description="文本转语音端点路径",
        json_schema_extra={"label": "端点路径", "hint": "标准 OpenAI 路径为 /audio/speech", "order": 1},
    )
    api_key: str = Field(
        default="",
        description="OpenAI 或兼容服务的 API Key",
        json_schema_extra={"label": "API Key", "input_type": "password", "order": 2},
    )
    organization: str = Field(
        default="",
        description="可选的 OpenAI Organization ID",
        json_schema_extra={"label": "Organization", "hint": "留空时不发送 OpenAI-Organization 请求头", "order": 3},
    )
    project: str = Field(
        default="",
        description="可选的 OpenAI Project ID",
        json_schema_extra={"label": "Project", "hint": "留空时不发送 OpenAI-Project 请求头", "order": 4},
    )
    timeout_seconds: float = Field(
        default=90.0,
        ge=1.0,
        le=600.0,
        description="单次合成请求的总超时时间（秒）",
        json_schema_extra={"label": "请求超时", "hint": "建议设置为 30 到 120 秒", "order": 5},
    )
    proxy_url: str = Field(
        default="",
        description="可选的 HTTP/HTTPS 代理地址",
        json_schema_extra={"label": "代理地址", "hint": "示例：http://127.0.0.1:7890；留空表示不指定代理", "order": 6},
    )
    use_system_proxy: bool = Field(
        default=False,
        description="是否读取 HTTP_PROXY、HTTPS_PROXY 和 NO_PROXY 环境变量",
        json_schema_extra={"label": "使用系统代理", "order": 7},
    )


class VoiceConfig(PluginConfigBase):
    """语音合成参数。"""

    __ui_label__ = "语音"
    __ui_icon__ = "audio-lines"
    __ui_order__ = 2

    model: str = Field(
        default="tts-1",
        description="OpenAI TTS 请求中的模型名称",
        json_schema_extra={"label": "模型", "hint": "OpenAI 常用模型：tts-1、tts-1-hd", "order": 0},
    )
    voice: str = Field(
        default="alloy",
        description="默认音色名称",
        json_schema_extra={"label": "默认音色", "hint": "OpenAI 常用音色：alloy", "order": 1},
    )
    custom_voice_id: str = Field(
        default="",
        description="可选的 OpenAI 自定义音色 ID；填写后会优先于默认音色",
        json_schema_extra={
            "label": "自定义音色 ID",
            "hint": 'OpenAI 自定义音色使用 {"id": "voice_xxx"} 形式；普通兼容端点通常留空',
            "order": 2,
        },
    )
    response_format: str = Field(
        default="mp3",
        description="接口返回的音频格式",
        json_schema_extra={
            "label": "音频格式",
            "hint": "平台语音消息优先推荐 mp3",
            "options": ["mp3", "opus", "aac", "flac", "wav", "pcm"],
            "order": 3,
        },
    )
    speed: float = Field(
        default=1.0,
        ge=0.25,
        le=4.0,
        description="默认语速倍率",
        json_schema_extra={"label": "默认语速", "hint": "OpenAI 标准范围为 0.25 到 4.0", "order": 4},
    )
    instructions: str = Field(
        default="",
        description="可选的说话风格指令；仅支持该参数的模型会生效",
        json_schema_extra={
            "label": "风格指令",
            "hint": "例如：用温柔、自然的语气说话。tts-1 和 tts-1-hd 不支持此参数，请保持为空",
            "input_type": "textarea",
            "order": 5,
        },
    )
    max_text_length: int = Field(
        default=4096,
        ge=1,
        le=4096,
        description="单次合成允许的最大字符数",
        json_schema_extra={"label": "文本长度上限", "hint": "OpenAI Speech API 上限为 4096 字符", "order": 6},
    )
    max_audio_size_mb: int = Field(
        default=20,
        ge=1,
        le=100,
        description="单次响应允许的最大音频体积（MiB）",
        json_schema_extra={"label": "音频大小上限", "order": 7},
    )


class FeatureConfig(PluginConfigBase):
    """功能入口配置。"""

    __ui_label__ = "功能"
    __ui_icon__ = "wrench"
    __ui_order__ = 3

    command_enabled: bool = Field(
        default=True,
        description="是否启用 /tts 和 /语音 命令",
        json_schema_extra={"label": "启用命令", "order": 0},
    )
    tool_enabled: bool = Field(
        default=True,
        description="是否允许 LLM 调用 openai_tts_speak 工具",
        json_schema_extra={"label": "启用 LLM 工具", "order": 1},
    )
    probability_enabled: bool = Field(
        default=False,
        description="是否按概率把纯文本出站消息转换为语音",
        json_schema_extra={"label": "启用概率 TTS", "order": 2},
    )
    probability: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="每条符合条件的纯文本消息转换为语音的概率",
        json_schema_extra={"label": "转换概率", "hint": "0 表示从不转换，1 表示总是转换", "order": 3},
    )
    probability_min_text_length: int = Field(
        default=1,
        ge=1,
        le=4096,
        description="概率 TTS 处理的最小文本字符数",
        json_schema_extra={"label": "概率模式最短文本", "order": 4},
    )
    probability_max_text_length: int = Field(
        default=300,
        ge=1,
        le=4096,
        description="概率 TTS 处理的最大文本字符数",
        json_schema_extra={"label": "概率模式最长文本", "hint": "较短的上限可避免长回复产生高延迟", "order": 5},
    )
    probability_group_enabled: bool = Field(
        default=True,
        description="是否允许概率 TTS 处理群聊出站消息",
        json_schema_extra={"label": "群聊概率 TTS", "order": 6},
    )
    probability_private_enabled: bool = Field(
        default=True,
        description="是否允许概率 TTS 处理私聊出站消息",
        json_schema_extra={"label": "私聊概率 TTS", "order": 7},
    )
    probability_keep_text: bool = Field(
        default=False,
        description="概率 TTS 转换成功后，是否把原始文字与语音放在同一条出站消息中",
        json_schema_extra={"label": "概率模式保留文字", "order": 8},
    )
    tool_keep_text: bool = Field(
        default=False,
        description="LLM 工具合成成功后，是否把回复文字与语音放在同一条出站消息中",
        json_schema_extra={"label": "工具模式保留文字", "order": 9},
    )
    allow_voice_override: bool = Field(
        default=True,
        description="是否允许 LLM 工具临时指定音色",
        json_schema_extra={"label": "允许临时音色", "order": 10},
    )
    allow_speed_override: bool = Field(
        default=True,
        description="是否允许 LLM 工具临时指定语速",
        json_schema_extra={"label": "允许临时语速", "order": 11},
    )

    @model_validator(mode="after")
    def validate_probability_text_length(self) -> "FeatureConfig":
        """确保概率模式的文本长度区间有效。"""

        if self.probability_min_text_length > self.probability_max_text_length:
            raise ValueError("概率 TTS 的最小文本长度不能大于最大文本长度")
        return self


class OpenAITTSPluginConfig(PluginConfigBase):
    """OpenAI TTS 插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    feature: FeatureConfig = Field(default_factory=FeatureConfig)


class OpenAITTSPlugin(MaiBotPlugin):
    """MaiBot OpenAI TTS 插件。"""

    config_model = OpenAITTSPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[OpenAITTSClient] = None
        self._probability_reply_states: Dict[str, ProbabilityReplyState] = {}

    async def on_load(self) -> None:
        """创建 HTTP 客户端并检查关键配置。"""

        try:
            self._client = self._create_client()
        except TTSConfigurationError as exc:
            self.ctx.logger.error("OpenAI TTS 配置无效，插件加载失败：%s", exc)
            raise
        if not self.config.api.api_key.strip():
            self.ctx.logger.warning("OpenAI TTS 插件尚未配置 API Key，调用功能前请先填写")
        self.ctx.logger.info(
            "OpenAI TTS 插件已加载：model=%s voice=%s format=%s",
            self.config.voice.model,
            self.config.voice.voice,
            self.config.voice.response_format,
        )

    async def on_unload(self) -> None:
        """释放 HTTP 会话。"""

        self._probability_reply_states.clear()
        if self._client is not None:
            await self._client.close()
        self._client = None
        self.ctx.logger.info("OpenAI TTS 插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """在插件配置热更新后重建 HTTP 客户端。"""

        del config_data
        if scope != "self":
            return

        try:
            new_client = self._create_client()
        except TTSConfigurationError as exc:
            old_client = self._client
            self._client = None
            if old_client is not None:
                await old_client.close()
            self.ctx.logger.error("OpenAI TTS 新配置无效，客户端已停用：%s", exc)
            raise
        old_client = self._client
        self._client = new_client
        self._probability_reply_states.clear()
        if old_client is not None:
            await old_client.close()
        self.ctx.logger.info("OpenAI TTS 配置已热更新：version=%s", version)

    @Command(
        "openai_tts",
        description="把文本合成为语音消息",
        pattern=r"^/(?:tts|语音)(?:\s+(?P<text>[\s\S]+))?\s*$",
        aliases=["tts", "语音"],
    )
    async def handle_tts_command(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        """处理 ``/tts <文本>`` 与 ``/语音 <文本>`` 命令。"""

        if not self.config.plugin.enabled:
            return False, "OpenAI TTS 插件当前未启用", True
        if not self.config.feature.command_enabled:
            return False, "TTS 命令当前未启用", True
        if not stream_id:
            return False, "命令上下文缺少聊天流 ID", True

        matched_groups = kwargs.get("matched_groups")
        text = ""
        if isinstance(matched_groups, dict):
            matched_text = matched_groups.get("text")
            if isinstance(matched_text, str):
                text = matched_text.strip()
        if not text:
            return False, "用法：/tts <要合成的文本>", True

        try:
            await self._synthesize_and_send(text=text, stream_id=stream_id)
        except TTSClientError as exc:
            error_message = self._safe_error_message(exc)
            self.ctx.logger.warning("TTS 命令执行失败：%s", error_message)
            return False, error_message, True
        return True, "语音已发送", True

    @Tool(
        "openai_tts_speak",
        brief_description="把文本合成为语音并发送到当前聊天流",
        detailed_description=(
            "需要把完整回复文本转换为语音时调用；text 应传入原本要回复给用户的完整正文。参数说明：\n"
            "- text：string，必填，要转换为语音的完整回复文本。\n"
            "- voice：string，可选，临时音色；是否允许由插件配置决定。\n"
            "- speed：number，可选，0.25 到 4.0；是否允许由插件配置决定。\n"
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
        if not self.config.plugin.enabled:
            return {"success": False, "error": "OpenAI TTS 插件当前未启用"}
        if not self.config.feature.tool_enabled:
            return {"success": False, "error": "OpenAI TTS 工具当前未启用"}
        normalized_stream_id = stream_id.strip()
        if not normalized_stream_id:
            self.ctx.logger.error("TTS 工具执行上下文未注入真实聊天流 ID")
            return {"success": False, "error": "工具执行上下文缺少真实聊天流 ID"}

        selected_voice = voice if self.config.feature.allow_voice_override else None
        selected_speed = speed if self.config.feature.allow_speed_override else None
        try:
            audio = await self._synthesize_and_send(
                text=text,
                stream_id=normalized_stream_id,
                voice=selected_voice,
                speed=selected_speed,
                include_text=self.config.feature.tool_keep_text,
            )
        except TTSClientError as exc:
            error_message = self._safe_error_message(exc)
            self.ctx.logger.warning("TTS 工具执行失败：%s", error_message)
            return {"success": False, "error": error_message}
        return {
            "success": True,
            "message": "语音已发送",
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

        del hook_name, kwargs
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return {"action": "continue"}

        self._remove_expired_probability_states()
        should_convert = False
        if self.config.plugin.enabled and self.config.feature.probability_enabled:
            normalized_response = response.strip()
            has_rich_attachment = self._has_rich_reply_attachment(reply_tool_args)
            should_convert = (
                bool(normalized_response)
                and not has_rich_attachment
                and self._is_probability_text_length_allowed(normalized_response)
                and self._sample_probability()
            )

        self._probability_reply_states[normalized_session_id] = ProbabilityReplyState(
            should_convert=should_convert,
            expires_at=monotonic() + self._probability_state_ttl_seconds(),
            prepared_from_reply=True,
        )
        self.ctx.logger.debug(
            "已准备概率 TTS 整轮判定：stream_id=%s convert=%s text_length=%d",
            normalized_session_id,
            should_convert,
            len(response.strip()),
        )
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
        if not self.config.plugin.enabled or not self.config.feature.probability_enabled:
            return {"action": "continue"}

        normalized_stream_id = stream_id.strip()
        if not normalized_stream_id and isinstance(message, dict):
            message_session_id = message.get("session_id")
            if isinstance(message_session_id, str):
                normalized_stream_id = message_session_id.strip()
        if not normalized_stream_id:
            return {"action": "continue"}

        text = self._extract_probability_text(message)
        if text is None or not self._is_probability_chat_scope_allowed(message):
            self._disable_probability_state_for_segment(normalized_stream_id, typing)
            return {"action": "continue"}

        state = self._get_probability_state(normalized_stream_id)
        prepared_from_reply = state is not None and state.prepared_from_reply and not state.started
        if not prepared_from_reply and not self._is_probability_text_length_allowed(text):
            self._disable_probability_state_for_segment(normalized_stream_id, typing)
            return {"action": "continue"}

        if not self._resolve_probability_decision(normalized_stream_id, typing):
            return {"action": "continue"}

        try:
            synthesized = await self._synthesize_audio(text=text)
        except TTSClientError as exc:
            self._set_probability_state_to_text(normalized_stream_id)
            self.ctx.logger.warning(
                "概率 TTS 合成失败，保留原文字消息：stream_id=%s error=%s",
                normalized_stream_id,
                self._safe_error_message(exc),
            )
            return {"action": "continue"}

        voice_segment = {
            "type": "voice",
            "data": "",
            "binary_data_base64": base64.b64encode(synthesized.data).decode("ascii"),
        }
        modified_message = dict(message)
        if self.config.feature.probability_keep_text:
            modified_message["raw_message"] = [dict(segment) for segment in message["raw_message"]]
            modified_message["raw_message"].append(voice_segment)
        else:
            modified_message["raw_message"] = [voice_segment]
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
        self.ctx.logger.info(
            "概率 TTS 已替换出站文字消息：stream_id=%s text_length=%d audio_bytes=%d",
            normalized_stream_id,
            len(text),
            len(synthesized.data),
        )
        return {
            "action": "continue",
            "modified_kwargs": modified_kwargs,
        }

    def _create_client(self) -> OpenAITTSClient:
        settings = TTSClientSettings(
            base_url=self.config.api.base_url,
            endpoint_path=self.config.api.endpoint_path,
            api_key=self.config.api.api_key,
            model=self.config.voice.model,
            voice=self.config.voice.voice,
            custom_voice_id=self.config.voice.custom_voice_id,
            response_format=self.config.voice.response_format,
            speed=self.config.voice.speed,
            instructions=self.config.voice.instructions,
            timeout_seconds=self.config.api.timeout_seconds,
            max_text_length=self.config.voice.max_text_length,
            max_audio_size_mb=self.config.voice.max_audio_size_mb,
            organization=self.config.api.organization,
            project=self.config.api.project,
            proxy_url=self.config.api.proxy_url,
            use_system_proxy=self.config.api.use_system_proxy,
        )
        return OpenAITTSClient(settings)

    async def _synthesize_and_send(
        self,
        *,
        text: str,
        stream_id: str,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        include_text: bool = False,
    ) -> SynthesizedAudio:
        synthesized = await self._synthesize_audio(text=text, voice=voice, speed=speed)
        audio_base64 = base64.b64encode(synthesized.data).decode("ascii")
        message_segments: List[Dict[str, str]] = []
        if include_text:
            message_segments.append({"type": "text", "content": text})
        message_segments.append({"type": "voice", "content": audio_base64})
        sent = await self.ctx.send.hybrid(
            message_segments,
            stream_id,
            processed_plain_text=text,
        )
        if not sent:
            raise TTSClientError("语音已合成，但发送到聊天流失败")
        self.ctx.logger.info("TTS 语音消息发送成功：bytes=%d", len(synthesized.data))
        return synthesized

    async def _synthesize_audio(
        self,
        *,
        text: str,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
    ) -> SynthesizedAudio:
        """合成音频并输出不包含原文和凭据的运行日志。"""

        client = self._client
        if client is None:
            raise TTSConfigurationError("TTS 客户端尚未初始化")

        request_started_at = monotonic()
        if voice is not None:
            selected_voice = voice
        elif self.config.voice.custom_voice_id:
            selected_voice = f"custom:{self.config.voice.custom_voice_id}"
        else:
            selected_voice = self.config.voice.voice
        self.ctx.logger.info(
            "开始 TTS 合成：model=%s voice=%s text_length=%d",
            self.config.voice.model,
            selected_voice,
            len(text),
        )
        synthesized = await client.synthesize(text, voice=voice, speed=speed)
        elapsed_seconds = monotonic() - request_started_at
        self.ctx.logger.info(
            "TTS 合成完成：format=%s content_type=%s bytes=%d elapsed=%.2fs request_id=%s",
            synthesized.response_format,
            synthesized.content_type,
            len(synthesized.data),
            elapsed_seconds,
            synthesized.request_id,
        )
        return synthesized

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

        minimum_length = self.config.feature.probability_min_text_length
        maximum_length = min(
            self.config.feature.probability_max_text_length,
            self.config.voice.max_text_length,
        )
        return minimum_length <= len(text) <= maximum_length

    def _is_probability_chat_scope_allowed(self, message: Any) -> bool:
        """检查当前出站消息所属的群聊或私聊范围是否已启用。"""

        message_info = message.get("message_info") if isinstance(message, dict) else None
        group_info = message_info.get("group_info") if isinstance(message_info, dict) else None
        if group_info is None:
            return self.config.feature.probability_private_enabled
        return self.config.feature.probability_group_enabled

    def _sample_probability(self) -> bool:
        """按配置概率生成一次布尔判定。"""

        probability = self.config.feature.probability
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
            self.config.api.timeout_seconds + PROBABILITY_REPLY_STATE_TIMEOUT_BUFFER_SECONDS,
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
        """合成失败后阻止本轮剩余分段继续尝试语音。"""

        self._probability_reply_states[stream_id] = ProbabilityReplyState(
            should_convert=False,
            expires_at=monotonic() + self._probability_state_ttl_seconds(),
            prepared_from_reply=False,
            started=True,
        )

    def _safe_error_message(self, error: TTSClientError) -> str:
        """生成可记录、可展示且不包含本插件凭据的错误信息。"""

        message = str(error)
        secrets = [self.config.api.api_key.strip(), self.config.api.proxy_url.strip()]
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[已隐藏]")
        return message


def create_plugin() -> OpenAITTSPlugin:
    """创建 OpenAI TTS 插件实例。"""

    return OpenAITTSPlugin()
