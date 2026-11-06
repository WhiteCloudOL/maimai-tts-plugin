"""生成前按实际路由注入动态语调协议，固定模式保持正文。"""

from copy import deepcopy
from maibot_sdk import HookHandler, Tool
from maibot_sdk.types import HookMode
from typing import Any, Dict, List, Optional

from ..domain.breeze_control import PAUSE_EVENT_INDEX, control_prompt, event_tags
from ..domain.adaptive_tone import direction_limit
from ..domain.intonation import tone_capability, tone_choices, tone_prompt
from ..domain.models import Profile, TTSError
from ..domain.prompts import DEFAULT_PROMPTS, prompt_template


class IntonationMixin:
    @Tool(
        "mmtts_tone_options",
        brief_description="查看当前 TTS 的语调模式与允许的动态语调",
        detailed_description="查询当前模型、音色与资源支持的语调和控制语法。指令型平台可使用整段自由语气；情感枚举型平台仅使用允许的语调 ID。Breeze 另提供局部音频事件与停顿标签。",
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
        result = {
            "success": True,
            "provider": profile.name,
            "model": profile.model,
            "voice": profile.voice,
            "mode": profile.tone.mode,
            "default_tone": profile.tone.emotion,
            "allowed_tones": allowed if profile.tone.mode == "dynamic" else [],
            "tone_meanings": tone_choices(allowed, profile.tone.labels, profile.tone.directions),
            "format": "[mmtts:语调ID]正文" if profile.tone.mode == "dynamic" else "直接填写朗读正文",
        }
        limit = direction_limit(profile)
        if profile.tone.mode == "dynamic" and limit:
            result.update(
                format="[mmtts:语调ID]正文 或 [mmtts:style=说话方式]正文，任选一种头部标签",
                free_direction={
                    "scope": "整段语音", "max_characters": limit,
                    "han_character_weight": 2 if profile.kind == "cosyvoice" else 1,
                },
            )
        if profile.kind == "breeze" and profile.tone.mode == "dynamic":
            tags = event_tags(
                profile.options.breeze_language_code, local=profile.options.breeze_api_mode == "self_hosted"
            )
            result.update(
                format="[语气:描述][音频事件]正文；也支持 [用温柔的语气] 和可选的 [mmtts:语调ID] 头部标签",
                audio_events=tags,
                pause_events={code: values[PAUSE_EVENT_INDEX] for code, values in tags.items()},
                free_direction={"scope": "到下一个自由语气标记前", "max_characters": 200, "max_markers": 8},
                retained_text="移除控制标记后的正文",
            )
        return result

    def primary_tone_profile(self) -> Optional[Profile]:
        config = self.store.config
        if config.primary is None:
            return None
        target = config.primary
        profile = self.store.profile(target.provider_id)
        if target.model_id:
            profile = profile.for_model(target.model_id)
        return profile.with_synthesis_overrides(voice=target.voice).with_selected_voice()

    def dynamic_tone_prompt(self, *, automatic: bool, adaptive: bool = False) -> str:
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
        if profile.kind == "breeze":
            return control_prompt(
                self.features.tone_prompt_language, automatic=automatic,
                tags=event_tags(
                    profile.options.breeze_language_code, local=profile.options.breeze_api_mode == "self_hosted"
                ),
                adaptive=adaptive,
                template=prompt_template(
                    self.features.prompt_overrides, profile.kind, "dynamic", self.features.tone_prompt_language
                ),
            ) + ("" if adaptive else "\n[mmtts:ID]: " + tone_choices(
                profile.tone.allowed or cap["tones"], profile.tone.labels, profile.tone.directions
            ))
        return tone_prompt(
            profile.tone.allowed or cap["tones"],
            self.features.tone_prompt_language,
            automatic=automatic,
            default=profile.tone.emotion,
            direction_limit=direction_limit(profile),
            weighted=profile.kind == "cosyvoice",
            labels=profile.tone.labels,
            directions=profile.tone.directions,
            template=prompt_template(
                self.features.prompt_overrides, profile.kind, "dynamic", self.features.tone_prompt_language
            ),
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
        profile = self.primary_tone_profile()
        tool_prompt = (
            prompt_template(self.features.prompt_overrides, profile.kind, "tool", self.features.tone_prompt_language)
            if profile is not None else DEFAULT_PROMPTS["tool"][self.features.tone_prompt_language]
        )
        # 固定或关闭模式的规划请求只保留朗读工具；动态模式才暴露语调查询与标签协议。
        definitions = [
            deepcopy(item)
            for item in tool_definitions
            if prompt
            or not (isinstance(item.get("function"), dict) and item["function"].get("name") == "mmtts_tone_options")
        ]
        for item in definitions:
            function = item.get("function")
            if isinstance(function, dict) and function.get("name") == "mmtts_speak":
                function["description"] = "\n\n".join(
                    value for value in (tool_prompt, prompt) if value
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
        automatic = self.features.probability_enabled and self._is_probability_reply(kwargs.get("reply_tool_args"))
        adaptive = automatic and self.features.probability_tone_enabled
        if adaptive:
            # 其他平台由后置分类选择语调；Breeze 继续提示局部表演，避免丢失事件与停顿能力。
            profile = self.primary_tone_profile()
            if profile is None or profile.kind != "breeze":
                return {"action": "continue"}
        if not automatic and not self.features.tool_enabled:
            return {"action": "continue"}
        prompt = self.dynamic_tone_prompt(automatic=automatic, adaptive=adaptive)
        if not prompt:
            return {"action": "continue"}
        modified = dict(kwargs)
        modified["extra_prompt"] = "\n\n".join(value for value in (extra_prompt, prompt) if value)
        return {"action": "continue", "modified_kwargs": modified}
