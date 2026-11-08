"""面向其他 MaiBot 插件的版本化 SDK API 入口。"""

from maibot_sdk import API
from typing import Any, Dict, Optional


class PluginAPIMixin:
    """声明版本 1 的公开 SDK API，并将调用交给 PluginAPIService。

    入口维护调用签名与组件元信息，业务规则、任务状态和音频资源由应用服务管理。
    """
    @API("tts_status", version="1", public=True, description="读取语音 API 就绪状态与运行概况")
    async def tts_status(self) -> Dict[str, Any]:
        """返回开关、配置就绪情况、活动任务和缓存概况。"""
        return await self.public_api.call("status")

    @API("tts_catalog", version="1", public=True, description="读取已配置的语音服务商、模型与路由")
    async def tts_catalog(self) -> Dict[str, Any]:
        """返回用于选择服务商、模型和默认路由的公开目录。"""
        return await self.public_api.call("catalog")

    @API("tts_voices", version="1", public=True, timeout_ms=600000, description="查询收藏、预设或账号音色")
    async def tts_voices(self, provider_id: str = "", model_id: str = "", source: str = "saved", refresh: bool = False) -> Dict[str, Any]:
        """按 saved、preset、account 来源查询音色；refresh 控制账号列表刷新。"""
        return await self.public_api.call("voices", provider_id=provider_id, model_id=model_id, source=source, refresh=refresh)

    @API("tts_describe", version="1", public=True, description="读取具体模型音色的参数描述与控制能力")
    async def tts_describe(self, provider_id: str = "", model_id: str = "", voice_id: str = "", parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """解析实际模型音色，返回参数 JSON Schema、可用语调和风格额度。"""
        return await self.public_api.call("describe", provider_id=provider_id, model_id=model_id, voice_id=voice_id,
                                          parameters={} if parameters is None else parameters)

    @API("tts_validate", version="1", public=True, description="校验高级调用并读取实际目标与参数")
    async def tts_validate(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """静态校验高级请求，返回目标解析结果及 LLM 分析需求。"""
        return await self.public_api.call("validate", **request)

    @API("tts_analyze", version="1", public=True, timeout_ms=600000, description="按所选平台分析语气并返回结构化表达")
    async def tts_analyze(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """使用共享的语音辅助 LLM 返回与平台能力匹配的表达决定。"""
        return await self.public_api.call("analyze", **request)

    @API("tts_simple", version="1", public=True, timeout_ms=600000, description="自动适配语气并生成或发送语音")
    async def tts_simple(
        self, text: str, provider_id: str = "", model_id: str = "", voice_id: str = "", style_hint: str = "",
        output: str = "audio", stream_id: str = "", keep_text: bool = False, request_id: str = "",
    ) -> Dict[str, Any]:
        """自动分析正文与 style_hint，返回音频引用或按 output 投递语音。"""
        return await self.public_api.call("simple", text=text, provider_id=provider_id, model_id=model_id, voice_id=voice_id,
                                          style_hint=style_hint, output=output, stream_id=stream_id,
                                          keep_text=keep_text, request_id=request_id)

    @API("tts_advanced", version="1", public=True, timeout_ms=600000, description="使用具体模型、音色和合成参数生成语音")
    async def tts_advanced(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """执行 APIRequest 结构的高级请求，支持模型、音色和参数覆盖。"""
        return await self.public_api.call("advanced", **request)

    @API("tts_get_audio", version="1", public=True, description="按字节偏移分块读取生成的音频")
    async def tts_get_audio(self, audio_id: str, offset: int = 0, limit: int = 65536) -> Dict[str, Any]:
        """返回 Base64 音频块、下一字节偏移与 eof，用于下载和自定义处理。"""
        return await self.public_api.call("get_audio", audio_id=audio_id, offset=offset, limit=limit)

    @API("tts_send_audio", version="1", public=True, timeout_ms=600000, description="将已有音频结果发送到真实聊天流")
    async def tts_send_audio(self, audio_id: str, stream_id: str, keep_text: bool = False, request_id: str = "") -> Dict[str, Any]:
        """按聊天音频格式准备已有结果，并通过真实 stream_id 投递。"""
        return await self.public_api.send_audio(audio_id, stream_id, keep_text, request_id)

    @API("tts_submit", version="1", public=True, description="提交后台语音任务并返回任务编号")
    async def tts_submit(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """立即返回 job_id；request 使用与高级调用相同的结构。"""
        return await self.public_api.call("submit", **request)

    @API("tts_get_job", version="1", public=True, description="读取语音任务状态及结果")
    async def tts_get_job(self, job_id: str) -> Dict[str, Any]:
        """查询 running、done、error、cancelled 状态及最终 API 结果。"""
        return await self.public_api.call("get_job", job_id=job_id)

    @API("tts_cancel_job", version="1", public=True, description="取消正在运行的语音任务")
    async def tts_cancel_job(self, job_id: str) -> Dict[str, Any]:
        """取消活动任务并返回任务当前状态。"""
        return await self.public_api.call("cancel_job", job_id=job_id)
