"""将受控语调映射为平台参数，固定配置直接作用于接口。"""

from typing import List, Optional, Tuple

from ..domain.bailian import instruction_length
from ..domain.breeze_control import clean_breeze_text, parse_breeze_text
from ..domain.intonation import TONE_DIRECTIONS, parse_tone_tag, tone_capability, validate_tone_settings
from ..domain.models import Profile, TTSError


def validate_requested_tone(profile: Profile, requested: Optional[str]) -> None:
    settings = profile.tone
    if requested is None:
        return
    if settings.mode != "dynamic":
        raise TTSError("当前模型或音色采用固定语调或已关闭语调，请直接提供正文")
    cap = tone_capability(profile.kind, profile.model, profile.voice, profile.options.resource_id)
    if cap is None or requested not in (settings.allowed or cap["tones"]):
        allowed = settings.allowed or (cap["tones"] if cap else [])
        raise TTSError("动态标签中的语调未获当前模型或音色允许；允许的 ID：" + "、".join(allowed))


def apply_tone(profile: Profile, requested: Optional[str]) -> Tuple[Profile, str]:
    settings = profile.tone
    if requested is not None:
        validate_requested_tone(profile, requested)
    if settings.mode == "off":
        return profile, ""
    try:
        validate_tone_settings(
            settings, profile.kind, profile.model, profile.voice, profile.options.resource_id, profile.instructions
        )
    except ValueError as exc:
        raise TTSError(str(exc)) from None
    emotion = requested if settings.mode == "dynamic" and requested else settings.emotion
    cap = tone_capability(profile.kind, profile.model, profile.voice, profile.options.resource_id)
    if cap is None:
        raise TTSError("当前模型与音色尚未确认支持语调控制")
    if cap["protocol"] == "minimax_emotion":
        selected = profile.model_copy(deep=True)
        selected.options.minimax_emotion = {"neutral": "calm", "lively": "fluent"}.get(emotion, emotion)
        return selected, emotion
    direction = f"你说话的情感是{emotion}。" if cap["protocol"] == "cosy_emotion" else TONE_DIRECTIONS[emotion]
    selected = profile.model_copy(
        deep=True, update={"instructions": "\n".join(value for value in (profile.instructions, direction) if value)}
    )
    if selected.kind == "cosyvoice" and instruction_length(selected.instructions) > 100:
        raise TTSError("CosyVoice 风格与语调合计超过 100 字符上限（中文按 2 字符计算）")
    return selected, emotion


def breeze_requests(profile: Profile, text: str, requested: Optional[str]) -> List[Tuple[Profile, str]]:
    """自由语气替代所在区段的固定语调；未标记区段继续沿用模型、音色与头部语调。"""
    parsed = parse_breeze_text(text)
    if parsed.error:
        raise TTSError(parsed.error)
    if requested is not None:
        validate_requested_tone(profile, requested)
    if any(part.direction for part in parsed.parts) and profile.tone.mode != "dynamic":
        raise TTSError("Breeze 自由语气需要在当前模型或音色中启用动态语调")
    requests: List[Tuple[Profile, str]] = []
    for part in parsed.parts:
        if part.direction:
            instructions = "\n".join(value for value in (profile.instructions, part.direction) if value)
            selected = profile.model_copy(deep=True, update={"instructions": instructions})
        else:
            selected, _ = apply_tone(profile, requested)
        if len(parsed.parts) > 1:
            selected = selected.model_copy(deep=True, update={"response_format": "wav"})
        requests.append((selected, part.text))
    return requests


def clean_display_text(text: str, *, breeze: bool = False) -> str:
    text = parse_tone_tag(text).text
    return clean_breeze_text(text) if breeze else text
