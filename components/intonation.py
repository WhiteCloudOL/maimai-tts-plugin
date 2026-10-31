"""生成前按实际路由注入动态语调协议，固定模式保持正文。"""

from copy import deepcopy
from maibot_sdk import HookHandler, Tool
from maibot_sdk.types import HookMode
from typing import Any, Dict, List, Optional

from ..domain.intonation import tone_capability, tone_prompt
from ..domain.models import Profile, TTSError


class IntonationMixin:
    @Tool(
        "mmtts_tone_options",
        brief_description="查看当前 TTS 的语调模式与允许的动态语调",
        detailed_description="使用语音工具前可查询当前语调。固定模式直接传朗读正文；动态模式按返回的允许列表选择一个头部标签。",
        parameters=[],
    )
    async def handle_tone_options(self, **kwargs: Any) -> Dict[str, Any]:
        if not self.config.plugin.enabled or not self.features.tool_enabled:
            return {"success": False, "error": "语音工具当前未启用"}
        try:
            profile = self.primary_tone_profile()
            if profile is None:
                raise TTSError("请先在整体路由选择语音模型")
        except (ValueError, TTSError) as exc:
            return {"success": False, "error": self._safe_error_message(exc)}
        cap = tone_capability(profile.kind, profile.model, profile.voice, profile.options.resource_id)
        allowed = profile.tone.allowed or (cap["tones"] if cap else [])
        return {
            "success": True,
            "provider": profile.name,
            "model": profile.model,
            "voice": profile.voice,
            "mode": profile.tone.mode,
            "default_tone": profile.tone.emotion,
            "allowed_tones": allowed if profile.tone.mode == "dynamic" else [],
            "format": "[mmtts:语调ID]正文" if profile.tone.mode == "dynamic" else "直接填写朗读正文",
        }

    def primary_tone_profile(self) -> Optional[Profile]:
        config = self.store.config
        if config.primary is None:
            return None
        target = config.primary
        profile = self.store.profile(target.provider_id)
        if target.model_id:
            profile = profile.for_model(target.model_id)
        values = profile.model_dump()
        if target.voice is not None:
            values["voice"] = target.voice
        return Profile.model_validate(values).with_selected_voice()

    def dynamic_tone_prompt(self, *, automatic: bool) -> str:
        profile = self.primary_tone_profile()
        if profile is None or profile.tone.mode != "dynamic":
            return ""
        cap = tone_capability(profile.kind, profile.model, profile.voice, profile.options.resource_id)
        if cap is None:
            return ""
        self.service.log.debug(
            "添加动态语调提示",
            provider=profile.name,
            model=profile.model,
            tone_count=len(profile.tone.allowed or cap["tones"]),
        )
        return tone_prompt(
            profile.tone.allowed or cap["tones"],
            self.features.tone_prompt_language,
            automatic=automatic,
            default=profile.tone.emotion,
        )

    @HookHandler(
        "maisaka.planner.before_request",
        name="prepare_planner_tone_prompt",
        description="向规划器的语音工具说明注入当前动态语调允许列表",
        mode=HookMode.BLOCKING,
        timeout_ms=1000,
    )
    async def prepare_planner_tone_prompt(
        self, tool_definitions: Optional[List[Dict[str, Any]]] = None, hook_name: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        del hook_name
        if not self.config.plugin.enabled or not self.features.tool_enabled or not tool_definitions:
            return {"action": "continue"}
        prompt = self.dynamic_tone_prompt(automatic=False)
        # 固定或关闭模式的规划请求只保留朗读工具；动态模式才暴露语调查询与标签协议。
        definitions = [
            deepcopy(item)
            for item in tool_definitions
            if prompt
            or not (isinstance(item.get("function"), dict) and item["function"].get("name") == "mmtts_tone_options")
        ]
        if not prompt and len(definitions) == len(tool_definitions):
            return {"action": "continue"}
        for item in definitions:
            function = item.get("function")
            if prompt and isinstance(function, dict) and function.get("name") == "mmtts_speak":
                function["description"] = "\n\n".join(
                    value for value in (function.get("description", ""), prompt) if value
                )
        return {"action": "continue", "modified_kwargs": dict(kwargs, tool_definitions=definitions)}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="prepare_dynamic_tone_prompt",
        description="根据首选模型与音色的动态语调允许列表提示回复模型",
        mode=HookMode.BLOCKING,
        timeout_ms=1000,
    )
    async def prepare_dynamic_tone_prompt(
        self, extra_prompt: str = "", hook_name: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        del hook_name
        if not self.config.plugin.enabled or not (self.features.probability_enabled or self.features.tool_enabled):
            return {"action": "continue"}
        prompt = self.dynamic_tone_prompt(automatic=self.features.probability_enabled)
        if not prompt:
            return {"action": "continue"}
        modified = dict(kwargs)
        modified["extra_prompt"] = "\n\n".join(value for value in (extra_prompt, prompt) if value)
        return {"action": "continue", "modified_kwargs": modified}
