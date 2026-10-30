"""将受控语调映射为平台参数，固定配置直接作用于接口。"""

from typing import Optional, Tuple

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
    if selected.kind == "cosyvoice" and sum(2 if ord(c) > 127 else 1 for c in selected.instructions) > 100:
        raise TTSError("CosyVoice 风格与语调合计超过 100 字符上限（中文按 2 字符计算）")
    return selected, emotion


def clean_display_text(text: str) -> str:
    return parse_tone_tag(text).text
