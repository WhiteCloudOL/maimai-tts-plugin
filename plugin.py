"""麦麦TTS语音：框架生命周期与工作台 API 的薄适配。"""

from maibot_sdk import API, MaiBotPlugin
from typing import Any, Dict, Optional, Set, Tuple

import json

from .settings.config import TTSConfig
from .components.messaging import MessagingMixin, ProbabilityReplyState
from .components.intonation import IntonationMixin
from .domain.models import TTSError
from .services.tts import TTSService
from .infrastructure.storage import ConfigStore
from .infrastructure.reference_audio import ReferenceAudioStore
from .services.studio import Studio


class TTSPlugin(IntonationMixin, MessagingMixin, MaiBotPlugin):
    config_model = TTSConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: Optional[ConfigStore] = None
        self._service: Optional[TTSService] = None
        self._studio: Optional[Studio] = None
        self._probability_reply_states: Dict[str, ProbabilityReplyState] = {}
        self._control_reply_guards: Set[Tuple[str, str]] = set()

    @property
    def store(self) -> ConfigStore:
        if self._store is None:
            raise TTSError("工作台配置尚未加载")
        return self._store

    @property
    def service(self) -> TTSService:
        if self._service is None:
            raise TTSError("TTS 服务尚未加载")
        return self._service

    @property
    def studio(self) -> Studio:
        if self._studio is None:
            raise TTSError("工作台尚未加载")
        return self._studio

    async def on_load(self) -> None:
        self._store = ConfigStore(self.ctx.paths.data_dir)
        await self.store.load()
        await ReferenceAudioStore(self.ctx.paths.data_dir).prepare()
        self._service = TTSService(self.store, self.config.runtime, self.ctx.logger)
        self._studio = Studio(self.store, self.service)
        self._log_configuration("插件已加载")

    async def on_unload(self) -> None:
        if self._service is not None:
            self.service.log.info("插件正在卸载")
        if self._studio is not None:
            await self._studio.close()
        if self._service is not None:
            await self._service.close()
        self._studio = None
        self._service = None
        self._store = None
        self._probability_reply_states.clear()
        self._control_reply_guards.clear()

    def _log_configuration(self, event: str) -> None:
        config = self.store.config
        primary = config.primary
        self.service.log.info(
            event,
            enabled=self.config.plugin.enabled,
            providers=len(config.profiles),
            primary=f"{primary.provider_id}/{primary.model_id}" if primary else "",
            voice=primary.voice if primary else "",
            revision=config.revision,
            backups=len(config.fallbacks),
            command=config.feature.command_enabled,
            tool=config.feature.tool_enabled,
            auto_voice=config.feature.probability_enabled,
            probability=config.feature.probability,
            log_level=self.config.runtime.log_level,
        )
        if primary is None:
            self.service.log.warning("整体语音路由待配置", entry="插件工作台 → 语音路由")
        self.service.log.debug(
            "语音触发方式",
            command="/tts <文本>",
            tool="mmtts_speak",
            automatic="由出站文本 Hook 判定",
            natural_language="由主程序生成回复并调用语音工具",
        )

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        del config_data, version
        if scope != "self":
            return
        old_studio, old_service = self._studio, self._service
        self._service = TTSService(self.store, self.config.runtime, self.ctx.logger)
        self._studio = Studio(self.store, self.service)
        if old_studio is not None:
            await old_studio.close()
        if old_service is not None:
            await old_service.close()
        self._probability_reply_states.clear()
        self._log_configuration("运行配置已更新")

    @API("studio_state", description="读取平台、路由与工作台概览")
    async def studio_state(self) -> Dict[str, Any]:
        return await self.studio.snapshot()

    @API("studio_reference_files", description="列出插件数据目录中的参考录音")
    async def studio_reference_files(self) -> Dict[str, Any]:
        return await self.service.http.reference_files.snapshot()

    @API("studio_import_reference", description="将服务器上的录音复制到插件参考音频目录")
    async def studio_import_reference(self, path: str, kind: str = "") -> Dict[str, Any]:
        result = await self.service.http.reference_files.import_file(path, allow_m4a=kind == "minimax")
        self.service.log.info("参考录音已导入", file=result["path"])
        return result

    @API("studio_save", description="原子保存工作台配置")
    async def studio_save(self, payload: str, revision: int) -> Dict[str, Any]:
        try:
            data = json.loads(payload)
        except (ValueError, RecursionError):
            raise TTSError("配置不是有效 JSON") from None
        if not isinstance(data, dict):
            raise TTSError("配置必须是对象")
        result = await self.store.save(data, revision)
        self._probability_reply_states.clear()
        self._log_configuration("工作台全部设置已保存")
        return result

    @API("studio_save_profile", description="仅保存当前平台")
    async def studio_save_profile(self, payload: str, revision: int) -> Dict[str, Any]:
        try:
            data = json.loads(payload)
        except (ValueError, RecursionError):
            raise TTSError("平台配置不是有效 JSON") from None
        if not isinstance(data, dict):
            raise TTSError("平台配置必须是对象")
        result = await self.store.save_profile(data, revision)
        self._probability_reply_states.clear()
        self.service.log.info("平台设置已保存", provider_id=data.get("id"), revision=result["revision"])
        return result

    @API("studio_save_routing", description="保存整体语音路由并立即生效")
    async def studio_save_routing(self, payload: str, revision: int) -> Dict[str, Any]:
        try:
            data = json.loads(payload)
        except (ValueError, RecursionError):
            raise TTSError("路由配置不是有效 JSON") from None
        if not isinstance(data, dict):
            raise TTSError("路由配置必须是对象")
        result = await self.store.save_routing(data, revision)
        self._probability_reply_states.clear()
        self._log_configuration("整体语音路由已保存并生效")
        return result

    @API("studio_launch", description="试听、模型与账号音色管理、声音复刻和设计")
    async def studio_launch(
        self,
        operation: str,
        profile_id: str = "",
        text: str = "",
        name: str = "",
        url: str = "",
        draft: str = "",
        model_id: str = "",
        voice_id: str = "",
        refresh: bool = False,
    ) -> Dict[str, str]:
        if not self.config.plugin.enabled:
            raise TTSError("插件当前未启用")
        return self.studio.launch(operation, profile_id, text, name, url, draft, model_id, voice_id, refresh)

    @API("studio_job", description="读取后台任务与分块试听音频")
    async def studio_job(self, job_id: str, offset: int = 0) -> Dict[str, Any]:
        return self.studio.job(job_id, offset)

    @API("studio_cancel", description="取消平台任务")
    async def studio_cancel(self, job_id: str) -> Dict[str, bool]:
        return self.studio.cancel(job_id)

    @API("studio_resolve", description="从模型详情页链接提取模型与接口地址")
    async def studio_resolve(self, url: str, kind: str) -> Dict[str, str]:
        return self.studio.resolve(url, kind)


def create_plugin() -> TTSPlugin:
    return TTSPlugin()
