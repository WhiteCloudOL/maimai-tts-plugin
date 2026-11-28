"""生成前按实际路由注入动态语调协议，固定模式保持正文。"""

from copy import deepcopy
from maibot_sdk import HookHandler, Tool
from maibot_sdk.types import HookMode
from typing import Any, Dict, List, Optional

from ..domain.breeze_control import PAUSE_EVENT_INDEX, event_tags
from ..domain.adaptive_tone import direction_limit
from ..domain.intonation import tone_capability, tone_choices
from ..domain.models import Profile, TTSError
from ..domain.prompts import DEFAULT_PROMPTS, has_variable, prompt_template, render_prompt
from ..services.speech_input import speech_tone_prompt, speech_tool_parameters, speech_tool_prompt


class IntonationMixin:
    @Tool(
        "mmtts_tone_options",
        brief_description="查看当前 TTS 的语调模式与允许的动态语调",
        detailed_description="查看当前可用语调 ID、表达含义和控制写法；Breeze 还返回完整音频事件与停顿标签表。",
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
            "tone_meanings": tone_choices(
                allowed, profile.tone.labels, profile.tone.directions, kind=profile.kind, model=profile.model,
            ),
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
        return speech_tone_prompt(profile, self.features, automatic=automatic, adaptive=adaptive)

    @HookHandler(
        "maisaka.planner.before_request",
        name="prepare_planner_tone_prompt",
        description="对规划器隐藏旧直发语音工具并按开关整理 reply 语音回复扩展参数",
        mode=HookMode.BLOCKING,
        timeout_ms=1000,
    )
    async def prepare_planner_tone_prompt(
        self, tool_definitions: Optional[List[Dict[str, Any]]] = None, hook_name: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        del hook_name
        if not tool_definitions:
            return {"action": "continue"}
        plugin_enabled = bool(self.config.plugin.enabled)
        voice_reply_enabled = plugin_enabled and bool(self.features.tool_enabled)
        probability_active = (
            plugin_enabled
            and bool(self.features.probability_enabled)
            and self.features.probability > 0
            and bool(self.features.probability_group_enabled or self.features.probability_private_enabled)
        )
        profile = self.primary_tone_profile() if voice_reply_enabled else None
        voice_ext = self._voice_reply_extension_name()
        prob_ext = self._probability_extension_name()

        # 显式语音回复统一改走 reply 的 voice_reply 扩展经 Replyer 生成，对规划器隐藏旧直发工具和语调查询工具。
        definitions = [
            deepcopy(item)
            for item in tool_definitions
            if not (
                isinstance(item.get("function"), dict)
                and item["function"].get("name") in {"mmtts_speak", "mmtts_tone_options"}
            )
        ]
        for item in definitions:
            function = item.get("function")
            if not (isinstance(function, dict) and function.get("name") == "reply"):
                continue
            parameters = function.get("parameters")
            root_props = parameters.get("properties") if isinstance(parameters, dict) else None
            plugin_options = root_props.get("plugin_options") if isinstance(root_props, dict) else None
            ext_props = plugin_options.get("properties") if isinstance(plugin_options, dict) else None
            if not isinstance(ext_props, dict):
                continue
            if voice_ext in ext_props:
                if voice_reply_enabled:
                    ext_schema = ext_props[voice_ext]
                    if isinstance(ext_schema, dict) and isinstance(ext_schema.get("properties"), dict):
                        allowed = (
                            {k: v for k, v in speech_tool_parameters(profile, self.features).items() if k != "text"}
                            if profile is not None
                            else {
                                k: {}
                                for k, ok in (
                                    ("voice", self.features.allow_voice_override),
                                    ("speed", self.features.allow_speed_override),
                                )
                                if ok
                            }
                        )
                        ext_schema["properties"] = {
                            key: dict(value, **allowed[key])
                            for key, value in ext_schema["properties"].items()
                            if key in allowed
                        }
                        if "required" in ext_schema:
                            ext_schema["required"] = [key for key in ext_schema["required"] if key in allowed]
                else:
                    ext_props.pop(voice_ext, None)
            if not probability_active:
                ext_props.pop(prob_ext, None)
            if not ext_props:
                root_props.pop("plugin_options", None)
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
        if (
            not self.config.plugin.enabled
            or not self.features.probability_enabled
            or self.features.probability <= 0
            or not (self.features.probability_group_enabled or self.features.probability_private_enabled)
        ):
            return {"action": "continue"}
        reply_tool_args = kwargs.get("reply_tool_args")
        if not self._is_probability_reply(reply_tool_args):
            return {"action": "continue"}
        if self._has_rich_reply_attachment(reply_tool_args) or self._uses_legacy_native_voice(reply_tool_args):
            return {"action": "continue"}
        session_id = str(kwargs.get("session_id") or "")
        if not self._is_prepared_probability_scope_allowed(reply_tool_args, session_id):
            return {"action": "continue"}
        adaptive = self.features.probability_tone_enabled
        if adaptive:
            # 其他平台由后置分类选择语调；Breeze 继续提示局部表演，避免丢失事件与停顿能力。
            profile = self.primary_tone_profile()
            if profile is None or profile.kind != "breeze":
                return {"action": "continue"}
        prompt = self.dynamic_tone_prompt(automatic=True, adaptive=adaptive)
        if not prompt:
            return {"action": "continue"}
        modified = dict(kwargs)
        modified["extra_prompt"] = "\n\n".join(value for value in (extra_prompt, prompt) if value)
        return {"action": "continue", "modified_kwargs": modified}
