"""生成前按实际路由注入动态语调协议，固定模式保持正文。"""

from copy import deepcopy
from maibot_sdk import HookHandler, Tool
from maibot_sdk.types import HookMode
from time import monotonic
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
        if self.features.tool_use_replyer:
            return {
                "success": False,
                "error": "当前已开启「改用回复模型生成语音正文」，语调规则会在 voice_reply 轮次自动注入回复模型，无需调用 mmtts_tone_options。",
            }
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
        description="按配置维护规划器语音工具说明或 reply 语音回复扩展参数",
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
        tool_enabled = plugin_enabled and bool(self.features.tool_enabled)
        use_replyer = tool_enabled and bool(self.features.tool_use_replyer)
        probability_active = (
            plugin_enabled
            and bool(self.features.probability_enabled)
            and self.features.probability > 0
            and bool(self.features.probability_group_enabled or self.features.probability_private_enabled)
        )
        profile = self.primary_tone_profile() if tool_enabled else None
        voice_ext = self._voice_reply_extension_name()
        prob_ext = self._probability_extension_name()

        if use_replyer:
            # 开启 Replyer 语音回复模式时，对规划器隐藏旧直发工具和语调查询工具，改走 reply.plugin_options.voice_reply。
            definitions = [
                deepcopy(item)
                for item in tool_definitions
                if not (
                    isinstance(item.get("function"), dict)
                    and item["function"].get("name") in {"mmtts_speak", "mmtts_tone_options"}
                )
            ]
        elif tool_enabled:
            # 默认模式沿用原版 mmtts_speak、自定义工具提示词与动态语调下的 mmtts_tone_options。
            prompt = self.dynamic_tone_prompt(automatic=False)
            tool_prompt = (
                prompt_template(self.features.prompt_overrides, profile.kind, "tool", self.features.tone_prompt_language)
                if profile is not None else DEFAULT_PROMPTS["tool"][self.features.tone_prompt_language]
            )
            query_available = any(
                isinstance(item.get("function"), dict) and item["function"].get("name") == "mmtts_tone_options"
                for item in tool_definitions
            )
            if prompt and query_available and "mmtts_tone_options" not in tool_prompt and "mmtts_tone_options" not in prompt:
                query_hint = {
                    "zh": "调用 mmtts_tone_options 可查看当前完整可用语调、音频事件及控制写法。",
                    "en": "Call mmtts_tone_options for the complete current tone list, audio events and control syntax.",
                    "ja": "現在の全語調、音声イベント、制御構文はmmtts_tone_optionsで確認できる。",
                }
                prompt += "\n" + query_hint[self.features.tone_prompt_language]
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
                    if profile is not None:
                        function["description"] = speech_tool_prompt(profile, self.features, query_available=query_available)
                        parameters = function.get("parameters")
                        if isinstance(parameters, dict) and isinstance(parameters.get("properties"), dict):
                            allowed = speech_tool_parameters(profile, self.features)
                            parameters["properties"] = {
                                key: dict(value, **allowed[key]) for key, value in parameters["properties"].items() if key in allowed
                            }
                            if "required" in parameters:
                                parameters["required"] = [key for key in parameters["required"] if key in allowed]
                        continue
                    description = render_prompt(tool_prompt, {"dynamic_tone": prompt})
                    # 旧的自定义说明未引用变量时，仍提供当前路由的完整规则；显式引用时只展开一次。
                    if prompt and not has_variable(tool_prompt, "dynamic_tone"):
                        description += "\n\n" + prompt
                    function["description"] = description.strip()
        else:
            definitions = [deepcopy(item) for item in tool_definitions]

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
                if use_replyer:
                    ext_schema = ext_props[voice_ext]
                    if isinstance(ext_schema, dict):
                        desc = ext_schema.get("description")
                        if isinstance(desc, str):
                            ext_schema["description"] = desc.replace("需在工作台开启“改用回复模型生成语音正文”；", "")
                        if isinstance(ext_schema.get("properties"), dict):
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
        if definitions == tool_definitions:
            return {"action": "continue"}
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
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        reply_tool_args = kwargs.get("reply_tool_args")
        voice_active = (
            bool(self.features.tool_enabled)
            and bool(self.features.tool_use_replyer)
            and self._is_voice_reply(reply_tool_args)
        )
        probability_active = (
            not voice_active
            and bool(self.features.probability_enabled)
            and self.features.probability > 0
            and bool(self.features.probability_group_enabled or self.features.probability_private_enabled)
            and self._is_probability_reply(reply_tool_args)
        )
        if not (voice_active or probability_active):
            return {"action": "continue"}
        if self._uses_legacy_native_voice(reply_tool_args):
            return {"action": "continue"}
        if voice_active and isinstance(reply_tool_args, dict):
            reply_id = reply_tool_args.get("_reply_id", "")
            if isinstance(reply_id, str) and reply_id:
                self._remove_expired_probability_states()
                self._voice_reply_prompted_map()[reply_id] = monotonic() + 600
        if probability_active:
            if self._has_rich_reply_attachment(reply_tool_args):
                return {"action": "continue"}
            session_id = str(kwargs.get("session_id") or "")
            if not self._is_prepared_probability_scope_allowed(reply_tool_args, session_id):
                return {"action": "continue"}
        adaptive = self.features.probability_tone_enabled if probability_active else False
        if adaptive:
            # 其他平台由后置分类选择语调；Breeze 继续提示局部表演，避免丢失事件与停顿能力。
            profile = self.primary_tone_profile()
            if profile is None or profile.kind != "breeze":
                return {"action": "continue"}
        prompt = self.dynamic_tone_prompt(automatic=True, adaptive=adaptive)
        if not prompt:
            return {"action": "continue"}
        if voice_active:
            voice_notice = {
                "zh": "本轮回复将合成为语音发送；开头附带的控制标签（如 [mmtts:...]）会由插件自动识别并移除，不属于多余前后缀或括号，请按当下情绪自然选用。",
                "en": "This reply will be synthesized as speech; any leading control tag (such as [mmtts:...]) is automatically parsed and stripped by the plugin and does not count as extra prefix or brackets, so feel free to use one when it fits the mood.",
                "ja": "今回の返信は音声として合成・送信されます。先頭の制御タグ（[mmtts:...] など）はプラグインが自動で認識・除去するため余分な前後綴りや括弧には当たりません。場面の感情に合わせて自然に活用してください。",
            }[self.features.tone_prompt_language]
            prompt = voice_notice + "\n" + prompt
        modified = dict(kwargs)
        modified["extra_prompt"] = "\n\n".join(value for value in (extra_prompt, prompt) if value)
        return {"action": "continue", "modified_kwargs": modified}
