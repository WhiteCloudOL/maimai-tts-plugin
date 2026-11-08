"""按服务商、模型和音色描述合成参数，并校验临时覆盖。"""

from copy import deepcopy
from functools import lru_cache
from pydantic import TypeAdapter
from typing import Any, Dict

from ..domain.bailian import MINIMAX_LANGUAGES, MINIMAX_RATES, QWEN_AUDIO_MODELS, SPEECH_RATES, VOICE_LANGUAGES
from ..domain.breeze import LANGUAGES as BREEZE_LANGUAGES
from ..domain.catalog import CATALOG
from ..domain.compatibility import template_variables
from ..domain.intonation import tone_capability, tone_choices
from ..domain.minimax import OLD_MODEL_LANGUAGES, emotions
from ..domain.models import Profile, ProviderOptions
from ..domain.style import supports_style
from .models import APIError


# 合成参数白名单与现有 ProviderOptions 共用类型约束，连接、账号和管理字段留在工作台。
SPEECH_HTTP_FIELDS = {"sample_rate", "volume", "pitch", "opus_bit_rate", "seed", "voice_language", "hot_fix", "enable_ssml"}
OPTION_FIELDS = {
    "openai": {"custom_voice", "openai_stream_format"},
    "compatible": {"custom_voice", "openai_stream_format"},
    "qwen": {"language", "optimize_instructions"} | SPEECH_HTTP_FIELDS,
    "cosyvoice": {"alibaba_transport"} | SPEECH_HTTP_FIELDS,
    "minimax": {"sample_rate", "minimax_vol", "minimax_pitch", "minimax_emotion", "minimax_bitrate", "minimax_channel",
                "language_boost", "text_normalization", "latex_read", "pronunciation_tone", "voice_modify_pitch",
                "voice_modify_intensity", "voice_modify_timbre", "sound_effects", "aigc_watermark", "timbre_weights",
                "minimax_output", "minimax_transport", "minimax_force_cbr", "minimax_normalization"},
    "bailian_minimax": {"sample_rate", "minimax_vol", "minimax_pitch", "minimax_emotion", "minimax_bitrate", "minimax_channel",
                        "language_boost", "text_normalization", "latex_read", "pronunciation_tone", "voice_modify_pitch",
                        "voice_modify_intensity", "voice_modify_timbre", "sound_effects", "aigc_watermark", "timbre_weights"},
    "siliconflow": {"sample_rate", "gain", "reference_enabled", "reference_source", "reference_file", "reference_url", "reference_text"},
    "volcengine": {"sample_rate", "resource_id", "volc_loudness_rate", "volc_bit_rate", "volc_instruction_mode", "volc_pitch",
                   "volc_language", "volc_dialect", "volc_silence_duration", "volc_tone_fidelity", "volc_emotion",
                   "volc_emotion_scale", "aigc_watermark", "latex_read", "pronunciation_tone", "enable_ssml"},
    "breeze": {"breeze_language_code", "breeze_guidance_scale", "breeze_volume", "breeze_speaker_boost", "breeze_transport",
               "seed", "breeze_mode", "reference_source", "reference_file", "reference_url", "reference_text"},
    "mimo": {"optimize_text_preview", "reference_source", "reference_file", "reference_url"},
    "gpt_sovits": {"text_language", "prompt_language", "text_split_method", "top_k", "top_p", "temperature",
                   "reference_source", "reference_file", "reference_path", "reference_text"},
}


@lru_cache(maxsize=1)
def option_schema() -> Dict[str, Any]:
    return ProviderOptions.model_json_schema()


@lru_cache(maxsize=128)
def parameter_type(name: str) -> TypeAdapter:
    """缓存字段的严格类型解析器，与参数 Schema 共用同一组字段定义。"""
    fields = Profile.model_fields if name in {"speed", "response_format"} else ProviderOptions.model_fields
    return TypeAdapter(fields[name].rebuild_annotation())


def parameter_schema(profile: Profile) -> Dict[str, Any]:
    source = option_schema()
    names = set(OPTION_FIELDS[profile.kind])
    formats = list(CATALOG[profile.kind]["formats"])
    speed = {"type": "number", "minimum": 0.25, "maximum": 4}
    speed_enabled = True
    if profile.kind in {"openai", "compatible"} and profile.model in {"tts-1", "tts-1-hd"}:
        names.discard("openai_stream_format")
    if profile.kind in {"minimax", "bailian_minimax", "cosyvoice", "volcengine", "breeze"}:
        speed.update(minimum=0.5, maximum=2)
    if profile.kind == "qwen":
        if profile.model in QWEN_AUDIO_MODELS:
            names = set(SPEECH_HTTP_FIELDS)
            if tone_capability(profile.kind, profile.model, profile.voice, "") is None:
                names.discard("enable_ssml")
            speed.update(minimum=0.5, maximum=2)
        else:
            names = {"language", "optimize_instructions"}
            speed_enabled = False
            formats = ["wav"]
            if not profile.model.startswith("qwen3-tts-instruct-flash"):
                names.discard("optimize_instructions")
    if profile.kind == "mimo":
        speed_enabled = False
        if "voicedesign" not in profile.model:
            names.discard("optimize_text_preview")
        if "voiceclone" not in profile.model:
            names -= {"reference_source", "reference_file", "reference_url"}
    if profile.kind == "cosyvoice":
        if profile.options.alibaba_transport != "http":
            names -= {"seed", "voice_language", "hot_fix", "enable_ssml"}
            formats = [fmt for fmt in formats if fmt != "opus"]
        if profile.model == "cosyvoice-v2":
            names.discard("hot_fix")
    if profile.kind == "cosyvoice" or (profile.kind == "qwen" and profile.model in QWEN_AUDIO_MODELS):
        if profile.response_format != "opus":
            names.discard("opus_bit_rate")
    if profile.kind == "compatible" and profile.options.compatibility is not None and profile.options.compatibility.mode == "custom":
        used = template_variables(profile.options.compatibility.body)
        speed_enabled = "speed" in used
        names &= {"custom_voice"} if "voice" in used else set()
        if "response_format" not in used:
            formats = [profile.response_format]
    if profile.kind == "breeze":
        if profile.options.breeze_api_mode == "self_hosted":
            speed_enabled = False
            formats = ["wav"]
            names = {"breeze_guidance_scale", "seed", "breeze_mode"}
            if profile.options.breeze_mode == "reference":
                names |= {"reference_source", "reference_file", "reference_url", "reference_text"}
        else:
            names -= {"seed", "breeze_mode", "reference_source", "reference_file", "reference_url", "reference_text"}
            if profile.options.breeze_transport == "stream":
                formats = ["wav", "mp3", "pcm"]
    if "reference_source" in names:
        if profile.kind == "siliconflow" and not profile.options.reference_enabled:
            names -= {"reference_source", "reference_file", "reference_url", "reference_text"}
        elif profile.options.reference_source == "file":
            names -= {"reference_url", "reference_path"}
        else:
            names.discard("reference_file")
    if profile.kind in {"minimax", "bailian_minimax"}:
        if profile.response_format != "mp3":
            names.discard("minimax_bitrate")
        if profile.response_format not in {"mp3", "wav", "flac"}:
            names -= {"voice_modify_pitch", "voice_modify_intensity", "voice_modify_timbre", "sound_effects"}
        if profile.kind == "minimax" and profile.options.minimax_transport != "http":
            names.discard("aigc_watermark")
        if profile.kind == "minimax" and (profile.options.minimax_transport == "http" or profile.response_format != "mp3"):
            names.discard("minimax_force_cbr")
        if profile.kind == "minimax" and profile.options.minimax_transport != "http" and profile.response_format != "mp3":
            names -= {"voice_modify_pitch", "voice_modify_intensity", "voice_modify_timbre", "sound_effects"}
    if profile.kind == "volcengine":
        if profile.response_format not in {"mp3", "opus"}:
            names.discard("volc_bit_rate")
        if profile.options.resource_id != "seed-icl-2.0":
            names -= {"volc_tone_fidelity", "volc_instruction_mode"}
        if not supports_style(profile.kind, profile.model, profile.voice, profile.options.resource_id):
            names.discard("volc_instruction_mode")
        if not profile.options.volc_emotion:
            names.discard("volc_emotion_scale")
    properties = {name: deepcopy(source["properties"][name]) for name in sorted(names)}
    option_values = profile.options.model_dump()
    for name, value in properties.items():
        value["default"] = option_values[name]
        value.setdefault("description", value.get("title", name))
    if "sample_rate" in properties:
        rates = {
            "cosyvoice": {8000, 12000, 16000, 24000, 48000} if profile.response_format == "opus" else SPEECH_RATES,
            "qwen": {8000, 12000, 16000, 24000, 48000} if profile.response_format == "opus" else SPEECH_RATES,
            "minimax": MINIMAX_RATES, "bailian_minimax": MINIMAX_RATES,
            "volcengine": {48000} if profile.response_format == "opus" else {8000, 16000, 22050, 24000, 32000, 44100, 48000},
            "siliconflow": {"mp3": {32000, 44100}, "opus": {48000}, "wav": {8000, 16000, 24000, 32000, 44100}, "pcm": {8000, 16000, 24000, 32000, 44100}}.get(profile.response_format, set()),
        }
        properties["sample_rate"]["enum"] = sorted(rates[profile.kind])
    if "language_boost" in properties:
        properties["language_boost"]["enum"] = sorted(set(MINIMAX_LANGUAGES) - (
            OLD_MODEL_LANGUAGES if profile.kind == "minimax" and profile.model.startswith(("speech-01-", "speech-02-")) else set()
        ))
    if "voice_language" in properties:
        properties["voice_language"]["enum"] = ["", *sorted(VOICE_LANGUAGES | (
            {"other"} if profile.model == "qwen-audio-3.1-tts-flash" else set()
        ))]
    if "reference_source" in properties:
        properties["reference_source"]["enum"] = ["server", "file"] if profile.kind == "gpt_sovits" else ["url", "file"]
    if profile.kind == "minimax":
        quality = profile.options.minimax_transport == "http" and profile.model.startswith(("speech-2.8-", "speech-2.6-"))
        properties["minimax_normalization"]["enum"] = ["", "basic", "quality"] if quality else ["", "basic"]
        if profile.options.minimax_transport != "http":
            properties["minimax_output"]["enum"] = ["hex"]
        if profile.response_format.startswith("pcmu_"):
            properties["sample_rate"]["enum"] = [8000]
            properties["minimax_channel"]["enum"] = [1]
    if "breeze_language_code" in properties:
        properties["breeze_language_code"]["enum"] = ["", *sorted(BREEZE_LANGUAGES)]
    if "minimax_emotion" in properties:
        allowed = emotions(profile.model) if profile.kind == "minimax" else {"", "happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm"}
        if profile.kind == "bailian_minimax" and "/speech-02-" in profile.model:
            allowed.add("whisper")
        properties["minimax_emotion"]["enum"] = sorted(allowed | {""})
    if speed_enabled:
        properties["speed"] = {**speed, "default": profile.speed, "description": "语速倍率"}
    properties["response_format"] = {"type": "string", "enum": formats, "default": profile.response_format, "description": "服务商音频格式"}
    return {"type": "object", "properties": properties, "additionalProperties": False, "$defs": source.get("$defs", {})}


def apply_parameters(profile: Profile, parameters: Dict[str, Any]) -> Profile:
    # 格式、资源和枚举等控制字段先参与能力解析，其余参数按最终组合校验。
    unknown = set(parameters) - OPTION_FIELDS[profile.kind] - {"speed", "response_format"}
    if unknown:
        raise APIError("INVALID_PARAMETER", "请按 tts_describe 选择参数：" + ", ".join(sorted(unknown)))
    data = profile.model_dump()
    options = data["options"]
    for key, value in parameters.items():
        value = parameter_type(key).validate_python(value, strict=True)
        if key in {"speed", "response_format"}:
            data[key] = value
        else:
            options[key] = value
    prospective = profile._runtime_profile(data)
    schema = parameter_schema(prospective)
    unknown = set(parameters) - set(schema["properties"])
    if unknown:
        raise APIError("INVALID_PARAMETER", "当前组合的参数：" + ", ".join(sorted(unknown)) + "，请按 tts_describe 选择")
    for key, value in parameters.items():
        spec = schema["properties"][key]
        if "enum" in spec and value not in spec["enum"]:
            raise APIError("INVALID_PARAMETER", f"{key} 请使用描述中的枚举值")
    if "hot_fix" in schema["properties"] and set(prospective.options.hot_fix) - {"pronunciation", "replace"}:
        raise APIError("INVALID_PARAMETER", "hot_fix 请使用 pronunciation 和 replace 字段")
    if prospective.kind == "volcengine" and prospective.options.enable_ssml and prospective.options.latex_read:
        raise APIError("INVALID_PARAMETER", "SSML 与 LaTeX 请分别启用")
    return prospective


def public_profile(profile: Profile) -> Dict[str, Any]:
    capability = tone_capability(profile.kind, profile.model, profile.voice, profile.options.resource_id)
    tones = (profile.tone.allowed or capability["tones"]) if capability else []
    return {"provider_id": profile.id, "provider_name": profile.name, "kind": profile.kind,
            "model_id": profile.model, "voice_id": profile.voice,
            "tone_supported": capability is not None, "tone_mode": profile.tone.mode, "default_tone": profile.tone.emotion,
            "allowed_tones": tones, "tone_meanings": tone_choices(tones, profile.tone.labels, profile.tone.directions),
            "style_supported": supports_style(profile.kind, profile.model, profile.voice, profile.options.resource_id, profile.options.compatibility)}
