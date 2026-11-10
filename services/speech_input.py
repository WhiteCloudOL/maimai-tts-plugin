"""在请求平台前校验朗读参数，并复用实际路由的语调提示。"""

from pydantic import ValidationError
from typing import Any, Dict

import json
import math

from ..domain.adaptive_tone import direction_limit
from ..domain.breeze_control import EVENT_PATTERN, control_prompt, event_tags
from ..domain.intonation import parse_tone_tag, tone_capability, tone_choices, tone_prompt
from ..domain.models import Features, Profile, TTSError, safe_validation_error
from ..domain.prompts import has_variable, prompt_template, render_prompt
from ..api.parameters import parameter_schema
from .intonation import apply_tone, breeze_requests


def speech_tone_prompt(profile: Profile, features: Features, *, automatic: bool, adaptive: bool = False) -> str:
    """按实际模型、音色、语言与自定义模板生成同一份语调规则。"""
    cap = tone_capability(profile.kind, profile.model, profile.voice, profile.options.resource_id)
    if profile.tone.mode != "dynamic" or cap is None:
        return ""
    template = prompt_template(features.prompt_overrides, profile.kind, "dynamic", features.tone_prompt_language)
    allowed = profile.tone.allowed or cap["tones"]
    if profile.kind == "breeze":
        prompt = control_prompt(
            features.tone_prompt_language, automatic=automatic, adaptive=adaptive, template=template,
            tags=event_tags(profile.options.breeze_language_code, local=profile.options.breeze_api_mode == "self_hosted"),
        ) + ("" if adaptive else "\n[mmtts:ID]: " + tone_choices(allowed, profile.tone.labels, profile.tone.directions))
    else:
        prompt = tone_prompt(
            allowed, features.tone_prompt_language, automatic=automatic, default=profile.tone.emotion,
            direction_limit=direction_limit(profile), weighted=profile.kind == "cosyvoice", template=template,
            labels=profile.tone.labels, directions=profile.tone.directions,
        )
    return prompt + "\n" + {
        "zh": "控制标记与台词放在同一正文中，标签后紧跟完整朗读内容，不能只输出标签。ID 和事件原样取自当前列表，示例中的占位符须替换为实际值；拿不准时直接输出原台词，沿用默认语调。",
        "en": "Keep controls and complete dialogue together; never return a tag alone. Copy IDs and events exactly from the current lists and replace example placeholders with actual values. When uncertain, return the original plain dialogue using the default delivery.",
        "ja": "制御と台詞を同じ本文に置き、タグの直後に朗読内容全体を続ける。タグだけを返さない。IDとイベントは現在の一覧から完全に写し、例のプレースホルダーは実際の値に置き換える。迷う場合は元の台詞だけを返して既定の話し方を使う。",
    }[features.tone_prompt_language]


def speech_tool_parameters(profile: Profile, features: Features) -> Dict[str, Any]:
    """描述当前工具实际提供的参数，按功能开关与平台能力提供语速字段。"""
    properties: Dict[str, Any] = {"text": {"type": "string", "minLength": 1}}
    if features.allow_voice_override:
        properties["voice"] = {"type": "string"}
    speed = parameter_schema(profile)["properties"].get("speed")
    if features.allow_speed_override and speed is not None:
        properties["speed"] = dict(speed, default=float(speed["default"]))
    return properties


def speech_tool_prompt(profile: Profile, features: Features, *, query_available: bool = True) -> str:
    """为工具声明和参数纠正提供一致的原始工具说明。"""
    prompt = speech_tone_prompt(profile, features, automatic=False)
    template = prompt_template(features.prompt_overrides, profile.kind, "tool", features.tone_prompt_language)
    if prompt and query_available and "mmtts_tone_options" not in template and "mmtts_tone_options" not in prompt:
        prompt += "\n" + {
            "zh": "调用 mmtts_tone_options 可查看当前完整可用语调、音频事件及控制写法。",
            "en": "Call mmtts_tone_options for the complete current tone list, audio events and control syntax.",
            "ja": "現在の全語調、音声イベント、制御構文はmmtts_tone_optionsで確認できる。",
        }[features.tone_prompt_language]
    description = render_prompt(template, {"dynamic_tone": prompt})
    if prompt and not has_variable(template, "dynamic_tone"):
        description += "\n\n" + prompt
    return description.strip() + "\n" + {
        "zh": "参数仅为 text、可选 voice、可选 speed；text 写实际台词，voice 使用实际可用音色，speed 使用数字。语调只按当前规则填写。失败回执用于纠正调用，错误说明保留在工具交互中；聊天与朗读正文保持本来要说的内容。",
        "en": "Arguments are text and optional voice and speed only. text is the actual dialogue, voice an available voice, and speed a number. Follow the current delivery rules. Use failure receipts to repair calls; keep diagnostics in tool interactions and keep chat and spoken text as the intended dialogue.",
        "ja": "引数はtextと任意のvoice、speedのみ。textは実際の台詞、voiceは利用可能な音色、speedは数値。現在の語調規則に従う。失敗の応答は呼び出し修正に使い、診断はツール対話内に保ち、チャットと朗読本文には元の台詞を使う。",
    }[features.tone_prompt_language] + "\n" + {
        "zh": "当前可用参数（以此列表与范围为准）：",
        "en": "Currently available arguments and ranges: ",
        "ja": "現在利用できる引数と範囲：",
    }[features.tone_prompt_language] + json.dumps(speech_tool_parameters(profile, features), ensure_ascii=False)


def validate_speech_arguments(profile: Profile, arguments: Dict[str, Any], max_text_length: int) -> None:
    """校验可修正的调用参数与控制标记；本函数执行纯本地计算。"""
    if set(arguments) - {"text", "voice", "speed"}:
        raise TTSError("语音参数仅包含 text、voice、speed")
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TTSError("text 必须是包含完整朗读正文的非空字符串")
    voice, speed = arguments.get("voice"), arguments.get("speed")
    if voice is not None and not isinstance(voice, str):
        raise TTSError("voice 必须为音色 ID 或名称字符串")
    if speed is not None and (isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed)):
        raise TTSError("speed 必须为有限数字倍率")
    try:
        selected = profile.with_synthesis_overrides(
            voice=profile.resolve_voice_id(voice) if voice else None, speed=speed,
        ).with_selected_voice()
    except (ValidationError, ValueError) as exc:
        raise TTSError(safe_validation_error(exc)) from None
    if speed is not None:
        speed_rule = parameter_schema(selected)["properties"].get("speed")
        if speed_rule is None:
            raise TTSError("当前模型或音色沿用平台语速配置，请省略 speed 参数")
        if not speed_rule["minimum"] <= speed <= speed_rule["maximum"]:
            raise TTSError(f"当前平台 speed 范围为 {speed_rule['minimum']}–{speed_rule['maximum']}")
    parsed = parse_tone_tag(text.strip())
    if parsed.error:
        raise TTSError(parsed.error)
    if len(parsed.text) > max_text_length:
        raise TTSError(f"朗读正文超过 {max_text_length} 字符上限")
    if selected.kind == "breeze":
        events = event_tags(selected.options.breeze_language_code, local=selected.options.breeze_api_mode == "self_hosted")
        allowed_events = {tag for values in events.values() for tag in values}
        if any(match.group() not in allowed_events for match in EVENT_PATTERN.finditer(parsed.text)):
            raise TTSError("Breeze 音频事件须原样使用当前接口和语种的事件表：" + " ".join(sorted(allowed_events)))
        breeze_requests(selected, parsed.text, parsed.tone, direction=parsed.direction)
    else:
        apply_tone(selected, parsed.tone, direction=parsed.direction)
