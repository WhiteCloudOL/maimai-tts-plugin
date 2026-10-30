"""MiniMax 官方与百炼共用的合成参数和账号音色结构。"""

from typing import Any, Dict, List

from ..domain.models import Profile, TTSError


def speech_payload(profile: Profile, text: str) -> Dict[str, Any]:
    options = profile.options
    if len(text) >= 10000 or (not profile.voice and not options.timbre_weights):
        raise TTSError("MiniMax 需要选择音色或混合音色，单次文本长度应小于 10000 字符")
    if profile.instructions:
        raise TTSError("MiniMax 请使用固定或动态情感选项，自由风格描述请留空")
    voice = {
        "voice_id": "" if options.timbre_weights else profile.voice,
        "speed": profile.speed,
        "vol": options.minimax_vol,
        "pitch": options.minimax_pitch,
        "text_normalization": options.text_normalization,
        "latex_read": options.latex_read,
    }
    if options.minimax_emotion:
        voice["emotion"] = options.minimax_emotion
    audio = {"sample_rate": options.sample_rate, "format": profile.response_format, "channel": options.minimax_channel}
    if profile.response_format == "mp3":
        audio["bitrate"] = options.minimax_bitrate
    payload = {
        "text": text,
        "voice_setting": voice,
        "audio_setting": audio,
        "output_format": options.minimax_output,
        "language_boost": "Chinese" if options.latex_read else options.language_boost,
        "aigc_watermark": options.aigc_watermark,
    }
    if options.timbre_weights:
        payload["timbre_weights"] = [item.model_dump() for item in options.timbre_weights]
    if options.pronunciation_tone:
        payload["pronunciation_dict"] = {"tone": options.pronunciation_tone}
    if any(
        (options.voice_modify_pitch, options.voice_modify_intensity, options.voice_modify_timbre, options.sound_effects)
    ):
        payload["voice_modify"] = {
            "pitch": options.voice_modify_pitch,
            "intensity": options.voice_modify_intensity,
            "timbre": options.voice_modify_timbre,
        }
        if options.sound_effects:
            payload["voice_modify"]["sound_effects"] = options.sound_effects
    return payload


def account_voices(output: Dict[str, Any]) -> List[Dict[str, str]]:
    voices = []
    for category, origin in (("system_voice", "preset"), ("voice_cloning", "clone"), ("voice_generation", "design")):
        items = output.get(category, [])
        if not isinstance(items, list):
            raise TTSError("MiniMax 账号音色响应应为数组")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("voice_id"), str) or not item["voice_id"]:
                raise TTSError("MiniMax 账号音色缺少有效 voice_id")
            name = item.get("voice_name") or item["voice_id"]
            if not isinstance(name, str):
                raise TTSError("MiniMax 音色名称应为字符串")
            voices.append(
                {
                    "id": item["voice_id"],
                    "name": name[:80],
                    "model": "",
                    "origin": origin,
                }
            )
    return voices
