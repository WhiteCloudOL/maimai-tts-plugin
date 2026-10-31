"""独立配置与服务边界的数据契约。"""

from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .intonation import TONE_LABELS, validate_tone_settings
from .bailian import MINIMAX_LANGUAGES, MINIMAX_RATES, QWEN_AUDIO_MODELS, SPEECH_RATES
from .compatibility import CompatibilityOptions, template_variables
from .minimax import FORMATS as MINIMAX_FORMATS, OLD_MODEL_LANGUAGES, emotions as minimax_emotions
from .breeze import LANGUAGES as BREEZE_LANGUAGES

ProviderKind = Literal[
    "openai",
    "qwen",
    "cosyvoice",
    "bailian_minimax",
    "minimax",
    "siliconflow",
    "volcengine",
    "mimo",
    "gpt_sovits",
    "compatible",
    "breeze",
]
AudioFormat = Literal["mp3", "wav", "opus", "aac", "flac", "pcm", "pcmu_raw", "pcmu_wav"]


class TTSError(RuntimeError):
    """可安全向用户展示的服务错误。"""


def validate_url(value: str, *, websocket: bool = False) -> str:
    """只接受完整服务地址，不隐式补路径，不允许内嵌凭据。"""
    if value != value.strip() or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("URL 不能包含空白或控制字符，请对特殊字符进行 URL 编码")
    parsed = urlsplit(value)
    schemes = {"ws", "wss"} if websocket else {"http", "https"}
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("请输入完整 URL（包含协议和请求路径），不能包含账号、密码或片段")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("URL 端口无效") from exc
    return value


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class TimbreWeight(Model):
    voice_id: str = Field(min_length=1, max_length=500)
    weight: int = Field(ge=1, le=100)


class ProviderOptions(Model):
    """平台专属字段；客户端只发送对应平台支持的参数。"""

    organization: str = Field(default="", max_length=200)
    project: str = Field(default="", max_length=200)
    custom_voice: bool = False
    voice_consent_id: str = Field(default="", max_length=200)
    openai_stream_format: Literal["audio", "sse"] = "audio"
    compatibility: Optional[CompatibilityOptions] = None
    breeze_api_mode: Literal["self_hosted", "cloud"] = "self_hosted"
    breeze_mode: Literal["reference", "design", "plain"] = "reference"
    breeze_transport: Literal["sync", "stream", "async"] = "sync"
    breeze_guidance_scale: float = Field(default=1, ge=1, le=10)
    breeze_language_code: str = Field(default="", max_length=2)
    breeze_voice_language: str = Field(default="zh", max_length=2)
    breeze_speaker_boost: bool = False
    breeze_volume: float = Field(default=1, ge=0.01, le=2)
    breeze_voice_type: Literal["all", "default", "personal"] = "all"
    breeze_max_characters: int = Field(default=1000, ge=1, le=2000)
    optimize_text_preview: bool = False
    optimize_instructions: bool = False
    language: str = Field(default="Auto", max_length=40)
    sample_rate: int = Field(default=24000, ge=8000, le=48000)
    gain: float = Field(default=0, ge=-10, le=10)
    volume: int = Field(default=50, ge=0, le=100)
    pitch: float = Field(default=1, ge=0.5, le=2)
    alibaba_transport: Literal["auto", "http", "websocket"] = "auto"
    opus_bit_rate: int = Field(default=64, ge=6, le=510)
    seed: Optional[int] = Field(default=None, ge=0, le=65535)
    enable_ssml: bool = False
    hot_fix: Dict[str, List[Dict[str, str]]] = Field(default_factory=dict)
    voice_language: str = Field(default="", max_length=10)
    clone_language_hint: str = Field(default="", max_length=10)
    max_prompt_audio_length: Optional[float] = Field(default=None, ge=3, le=30)
    enable_preprocess: Optional[bool] = None
    enable_volume_normalization: Optional[bool] = None
    voice_prompt: str = Field(default="", max_length=500)
    design_preview_text: str = Field(default="", max_length=500)
    design_language: Literal["zh", "en"] = "zh"
    minimax_vol: float = Field(default=1, gt=0, le=10)
    minimax_pitch: int = Field(default=0, ge=-12, le=12)
    minimax_emotion: Literal[
        "", "happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm", "whisper", "fluent"
    ] = ""
    minimax_bitrate: Literal[32000, 64000, 128000, 256000] = 128000
    minimax_channel: Literal[1, 2] = 1
    minimax_output: Literal["hex", "url"] = "hex"
    minimax_transport: Literal["http", "sse", "websocket"] = "http"
    minimax_force_cbr: bool = False
    minimax_normalization: Literal["", "basic", "quality"] = ""
    timbre_weights: List[TimbreWeight] = Field(default_factory=list, max_length=4)
    minimax_clone_file_id: str = Field(default="", max_length=30, pattern=r"^\d*$")
    minimax_prompt_audio: str = Field(default="", max_length=2000)
    minimax_validation_text: str = Field(default="", max_length=200)
    minimax_validation_accuracy: float = Field(default=0.7, ge=0, le=1)
    language_boost: str = Field(default="auto", max_length=40)
    text_normalization: bool = False
    latex_read: bool = False
    pronunciation_tone: List[str] = Field(default_factory=list, max_length=100)
    voice_modify_pitch: int = Field(default=0, ge=-100, le=100)
    voice_modify_intensity: int = Field(default=0, ge=-100, le=100)
    voice_modify_timbre: int = Field(default=0, ge=-100, le=100)
    sound_effects: Literal["", "spacious_echo", "auditorium_echo", "lofi_telephone", "robotic"] = ""
    aigc_watermark: bool = False
    need_noise_reduction: bool = False
    need_volume_normalization: bool = False
    prompt_audio_url: str = Field(default="", max_length=2000)
    prompt_text: str = Field(default="", max_length=1000)
    app_id: str = Field(default="", max_length=200)
    resource_id: str = Field(default="seed-tts-2.0", max_length=200)
    volc_loudness_rate: int = Field(default=0, ge=-50, le=100)
    volc_bit_rate: Literal[0, 64000, 160000] = 0
    auth_mode: Literal["api_key", "access_token"] = "api_key"
    clone_speaker_id: str = Field(default="", max_length=256)
    clone_billing: Literal["prepaid", "postpaid"] = "prepaid"
    clone_language: int = Field(default=0, ge=0, le=21)
    clone_resource_id: str = Field(default="seed-icl-2.0", max_length=200)
    reference_url: str = Field(default="", max_length=2000)
    reference_source: Literal["url", "file", "server"] = "url"
    reference_enabled: bool = True
    reference_file: str = Field(default="reference_audio/sample.wav", max_length=2000)
    reference_text: str = Field(default="", max_length=4000)
    reference_path: str = Field(default="", max_length=2000)
    prompt_language: str = Field(default="zh", max_length=40)
    text_language: str = Field(default="zh", max_length=40)
    text_split_method: str = Field(default="cut5", pattern=r"^cut[0-5]$")
    top_k: int = Field(default=15, ge=1, le=100)
    top_p: float = Field(default=1, gt=0, le=1)
    temperature: float = Field(default=1, gt=0, le=2)


class ToneSettings(Model):
    mode: Literal["off", "fixed", "dynamic"] = "off"
    emotion: str = "neutral"
    allowed: List[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_ids(self) -> "ToneSettings":
        if self.emotion not in TONE_LABELS or any(value not in TONE_LABELS for value in self.allowed):
            raise ValueError("请选择列表中的语调 ID")
        if len(self.allowed) != len(set(self.allowed)):
            raise ValueError("动态语调允许列表需要保持唯一")
        return self


class VoiceReference(Model):
    source: Literal["url", "file", "server"]
    path: str = Field(default="", max_length=2000)
    url: str = Field(default="", max_length=2000)
    text: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def validate_reference(self) -> "VoiceReference":
        if self.source == "url":
            validate_url(self.url)
        elif not self.path.strip():
            raise ValueError("参考音色需要填写录音路径")
        return self


class Voice(Model):
    id: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=80)
    model: str = Field(default="", max_length=200)
    reference: Optional[VoiceReference] = None
    instructions: Optional[str] = Field(default=None, max_length=4096)
    speed: Optional[float] = Field(default=None, ge=0.25, le=4)
    custom_voice: Optional[bool] = None
    resource_id: Optional[str] = Field(default=None, max_length=200)
    clone_state: Literal["ready", "training", "failed"] = "ready"
    origin: Literal["custom", "preset", "clone", "design", "account"] = "custom"
    creation_reference: Optional[VoiceReference] = None
    design_prompt: str = Field(default="", max_length=500)
    design_preview_text: str = Field(default="", max_length=500)
    design_language: Literal["zh", "en"] = "zh"
    tone: Optional[ToneSettings] = None


class ModelParameters(Model):
    response_format: AudioFormat
    speed: float = Field(default=1, ge=0.25, le=4)
    instructions: str = Field(default="", max_length=4096)
    options: ProviderOptions = Field(default_factory=ProviderOptions)
    tone: ToneSettings = Field(default_factory=ToneSettings)


def validate_custom_instructions(options: ProviderOptions, tone: ToneSettings, instructions: str) -> None:
    protocol = options.compatibility
    if protocol is not None and protocol.mode == "custom" and (instructions or tone.mode != "off"):
        if "instructions" not in template_variables(protocol.body):
            raise ValueError("风格或语调设置需要在自定义请求模板中引用 {{instructions}}")


class ConfiguredModel(Model):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=200)
    voice: str = Field(default="", max_length=500)
    endpoint: str = Field(default="", max_length=2000)
    base_url: str = Field(default="", max_length=2000)
    parameters: Optional[ModelParameters] = None


class Profile(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=80)
    kind: ProviderKind
    enabled: bool = True
    endpoint: str = Field(default="", max_length=2000)
    base_url: str = Field(default="", max_length=2000)
    api_key: str = Field(default="", max_length=4000)
    model: str = Field(default="", max_length=200)
    voice: str = Field(default="", max_length=500)
    response_format: AudioFormat = "mp3"
    speed: float = Field(default=1, ge=0.25, le=4)
    instructions: str = Field(default="", max_length=4096)
    models_url: str = Field(default="", max_length=2000)
    voices_url: str = Field(default="", max_length=2000)
    clone_url: str = Field(default="", max_length=2000)
    options: ProviderOptions = Field(default_factory=ProviderOptions)
    tone: ToneSettings = Field(default_factory=ToneSettings)
    voices: List[Voice] = Field(default_factory=list, max_length=100)
    models: List[ConfiguredModel] = Field(default_factory=list, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_provider_name(cls, value: Any) -> Any:
        if isinstance(value, str):
            return "".join(value.split())
        return value

    @model_validator(mode="before")
    @classmethod
    def retain_current_model(cls, value: Any) -> Any:
        """历史单模型字段保留为编辑上下文，模型库分别保存试听音色。"""
        if isinstance(value, dict) and value.get("model") and not value.get("models"):
            value = dict(value)
            value["models"] = [{"id": value["model"], "voice": value.get("voice", "")}]
        return value

    @model_validator(mode="after")
    def validate_addresses_and_format(self) -> "Profile":
        selected_voice = next((v for v in self.voices if v.id == self.voice and v.model in ("", self.model)), None)
        effective_tone = (
            selected_voice.tone if selected_voice is not None and selected_voice.tone is not None else self.tone
        )
        resource_id = (
            selected_voice.resource_id
            if selected_voice is not None and selected_voice.resource_id
            else self.options.resource_id
        )
        active_instructions = (
            selected_voice.instructions
            if selected_voice is not None and selected_voice.instructions is not None
            else self.instructions
        )
        validate_tone_settings(effective_tone, self.kind, self.model, self.voice, resource_id, active_instructions)
        if self.kind == "compatible":
            validate_custom_instructions(self.options, effective_tone, active_instructions)
        ids = [item.id for item in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("平台内的模型 ID 需要保持唯一")
        if self.model and self.model not in ids:
            raise ValueError("当前编辑模型需要加入平台的模型库")
        keys = [(voice.model, voice.id) for voice in self.voices]
        if len(keys) != len(set(keys)):
            raise ValueError("同一模型下的音色 ID 需要保持唯一")
        for item in self.models:
            if item.parameters is not None:
                parameters = item.parameters
                voice_settings = next((v for v in self.voices if v.id == item.voice and v.model in ("", item.id)), None)
                policy = (
                    voice_settings.tone
                    if voice_settings is not None and voice_settings.tone is not None
                    else parameters.tone
                )
                resource = (
                    voice_settings.resource_id
                    if voice_settings is not None and voice_settings.resource_id
                    else parameters.options.resource_id
                )
                instructions = (
                    voice_settings.instructions
                    if voice_settings is not None and voice_settings.instructions is not None
                    else parameters.instructions
                )
                validate_tone_settings(policy, self.kind, item.id, item.voice, resource, instructions)
                if self.kind == "compatible":
                    validate_custom_instructions(parameters.options, policy, instructions)
            if item.base_url:
                validate_url(item.base_url)
            if item.endpoint:
                validate_url(item.endpoint, websocket=item.endpoint.startswith(("ws://", "wss://")))
            if item.id == self.model:
                item.voice = self.voice
                if not item.endpoint and not item.base_url:
                    item.endpoint = self.endpoint
        if self.options.reference_source == "server" and self.kind != "gpt_sovits":
            raise ValueError("只有 GPT-SoVITS 支持服务端参考音频路径")
        if self.base_url:
            validate_url(self.base_url)
        else:
            websocket = (self.kind == "cosyvoice" and self.options.alibaba_transport != "http") or (
                self.kind in {"qwen", "minimax"} and self.endpoint.startswith(("ws://", "wss://"))
            )
            validate_url(self.endpoint, websocket=websocket)
        for value in (self.models_url, self.voices_url, self.clone_url):
            if value:
                validate_url(value)
        for voice in self.voices:
            if voice.tone is not None:
                model = next((m for m in self.models if m.id == (voice.model or self.model)), None)
                parameters = model.parameters if model is not None else None
                resource = voice.resource_id or (
                    parameters.options.resource_id if parameters else self.options.resource_id
                )
                instructions = (
                    voice.instructions
                    if voice.instructions is not None
                    else (parameters.instructions if parameters else self.instructions)
                )
                validate_tone_settings(
                    voice.tone, self.kind, voice.model or self.model, voice.id, resource, instructions
                )
                if self.kind == "compatible":
                    validate_custom_instructions(
                        parameters.options if parameters else self.options, voice.tone, instructions
                    )
        if self.options.reference_source == "url" and self.options.reference_url:
            validate_url(self.options.reference_url)
        formats = {
            "qwen": {"mp3", "wav", "pcm", "opus"} if self.model in QWEN_AUDIO_MODELS else {"wav"},
            "cosyvoice": {"mp3", "wav", "pcm", "opus"},
            "bailian_minimax": {"mp3", "wav", "pcm", "flac"},
            "minimax": set(MINIMAX_FORMATS),
            "siliconflow": {"mp3", "wav", "opus", "pcm"},
            "volcengine": {"mp3", "pcm", "opus", "wav"},
            "mimo": {"wav", "mp3", "pcm"},
            "gpt_sovits": {"wav"},
        }
        if self.response_format not in formats.get(self.kind, {"mp3", "wav", "opus", "aac", "flac", "pcm"}):
            raise ValueError("当前平台不支持所选音频格式")
        if self.kind == "cosyvoice" and not 0.5 <= self.speed <= 2:
            raise ValueError("CosyVoice 语速范围为 0.5–2")
        if self.kind == "cosyvoice" or (self.kind == "qwen" and self.model in QWEN_AUDIO_MODELS):
            if self.options.sample_rate not in SPEECH_RATES or not 0.5 <= self.speed <= 2:
                raise ValueError("百炼语速范围为 0.5–2，采样率请选择平台支持的值")
        if self.kind in {"bailian_minimax", "minimax"}:
            if self.options.sample_rate not in MINIMAX_RATES or not 0.5 <= self.speed <= 2:
                raise ValueError("MiniMax 语速范围为 0.5–2，采样率请选择平台支持的值")
            if self.kind == "bailian_minimax":
                if self.options.minimax_emotion == "fluent":
                    raise ValueError("百炼 MiniMax 请使用当前转接接口支持的情感")
                if self.options.minimax_emotion == "whisper" and "/speech-02-" not in self.model:
                    raise ValueError("百炼 whisper 语调适用于 MiniMax speech-02 系列")
            if self.options.language_boost not in MINIMAX_LANGUAGES:
                raise ValueError("请选择 MiniMax 支持的语言增强选项")
            effects = self.options
            if self.response_format not in {"mp3", "wav", "flac"} and any(
                (
                    effects.voice_modify_pitch,
                    effects.voice_modify_intensity,
                    effects.voice_modify_timbre,
                    effects.sound_effects,
                )
            ):
                raise ValueError("MiniMax 声音效果适用于 MP3、WAV 或 FLAC 格式")
            ids = [item.voice_id for item in self.options.timbre_weights]
            if len(ids) != len(set(ids)):
                raise ValueError("混合音色请为每个音色 ID 设置一项权重")
        if self.kind == "minimax":
            opts = self.options
            if opts.minimax_clone_file_id and int(opts.minimax_clone_file_id) <= 0:
                raise ValueError("MiniMax 文件 ID 请填写正整数")
            streaming = opts.minimax_transport != "http"
            if opts.minimax_emotion and opts.minimax_emotion not in minimax_emotions(self.model):
                raise ValueError("此 MiniMax 模型尚未支持该情感；fluent / whisper 请使用 speech-2.6 系列")
            if self.model.startswith(("speech-01-", "speech-02-")) and opts.language_boost in OLD_MODEL_LANGUAGES:
                raise ValueError("此旧版 MiniMax 模型尚未支持 Persian / Filipino / Tamil 语言增强")
            if opts.minimax_normalization == "quality" and (
                streaming or not self.model.startswith(("speech-2.8-", "speech-2.6-"))
            ):
                raise ValueError("高质量文本归一化请使用 speech-2.8 / 2.6 的 HTTP 非流式合成")
            if opts.minimax_force_cbr and (not streaming or self.response_format != "mp3"):
                raise ValueError("恒定码率仅用于 MP3 流式合成")
            if streaming and opts.minimax_output != "hex":
                raise ValueError("MiniMax 流式合成请使用 Hex 音频返回方式")
            if streaming and opts.aigc_watermark:
                raise ValueError("MiniMax AIGC 水印用于 HTTP 非流式合成")
            if (
                streaming
                and self.response_format != "mp3"
                and any(
                    (opts.voice_modify_pitch, opts.voice_modify_intensity, opts.voice_modify_timbre, opts.sound_effects)
                )
            ):
                raise ValueError("流式声音效果仅用于 MP3 格式")
            if self.response_format.startswith("pcmu_") and (opts.sample_rate != 8000 or opts.minimax_channel != 1):
                raise ValueError("G.711 μ-law 请使用 8000 Hz 单声道")
            if opts.minimax_validation_text and not opts.minimax_validation_text.strip():
                raise ValueError("录音校验文本请填写实际说出的文字")
        if self.kind == "volcengine" and not 0.5 <= self.speed <= 2:
            raise ValueError("火山引擎语速范围为 0.5–2")
        if self.kind == "volcengine":
            if self.response_format == "opus" and self.options.sample_rate != 48000:
                raise ValueError("火山引擎 Opus 仅支持 48000 Hz")
            if self.options.sample_rate not in {8000, 16000, 22050, 24000, 32000, 44100, 48000}:
                raise ValueError("请选择火山引擎支持的采样率")
            if self.options.volc_bit_rate and self.response_format not in {"mp3", "opus"}:
                raise ValueError("火山引擎比特率设置适用于 MP3 / Opus")
        if self.kind == "siliconflow":
            rates = {
                "mp3": {32000, 44100},
                "opus": {48000},
                "wav": {8000, 16000, 24000, 32000, 44100},
                "pcm": {8000, 16000, 24000, 32000, 44100},
            }
            if self.options.sample_rate not in rates[self.response_format]:
                raise ValueError("硅基流动采样率与格式不匹配，请在平台参数中调整")
        if self.kind == "breeze":
            if self.options.breeze_language_code and self.options.breeze_language_code not in BREEZE_LANGUAGES:
                raise ValueError("请选择 Breeze 支持的语言代码")
            if self.options.breeze_voice_language not in BREEZE_LANGUAGES:
                raise ValueError("请选择 Breeze 在线音色的语言")
            if self.options.breeze_api_mode == "self_hosted":
                if self.response_format != "wav" or self.speed != 1:
                    raise ValueError("Breeze 自托管返回 WAV，语速倍率保持 1，可通过风格描述控制节奏")
            else:
                if len(self.model) > 120 or not 0.5 <= self.speed <= 2:
                    raise ValueError("Breeze 在线模型 ID 最多 120 字符，语速范围为 0.5–2")
                if self.options.breeze_transport == "stream" and self.response_format not in {"pcm", "mp3", "wav"}:
                    raise ValueError("Breeze 在线流式输出支持 PCM、MP3 和 WAV")
        return self

    def for_model(self, model_id: str) -> "Profile":
        """从模型库解析操作目标，保持平台编辑上下文和其他模型独立。"""
        selected = next((item for item in self.models if item.id == model_id), None)
        if selected is None:
            raise TTSError("请先将目标模型加入当前平台的模型库")
        data = self.model_dump()
        data.update(model=selected.id, voice=selected.voice, endpoint=selected.endpoint or self.endpoint)
        data["base_url"] = selected.base_url or ("" if selected.endpoint else self.base_url)
        if selected.parameters is not None:
            data.update(selected.parameters.model_dump())
        if self.kind == "breeze":
            # 接入方式属于平台连接设置，各模型沿用同一个自托管或在线协议。
            data["options"]["breeze_api_mode"] = self.options.breeze_api_mode
        if not data["base_url"] and self.kind == "qwen" and data["endpoint"].startswith(("ws://", "wss://")):
            parsed = urlsplit(data["endpoint"])
            query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "model"]
            query.append(("model", selected.id))
            data["endpoint"] = urlunsplit(parsed._replace(query=urlencode(query)))
        return Profile.model_validate(data)

    def with_selected_voice(self) -> "Profile":
        selected = next(
            (item for item in self.voices if item.id == self.voice and item.model in ("", self.model)), None
        )
        if selected is None:
            return self
        if selected.clone_state != "ready":
            raise TTSError("此音色正在训练或训练失败，请在音色克隆页面查询状态后再使用")
        data = self.model_dump()
        if selected.instructions is not None:
            data["instructions"] = selected.instructions
        if selected.speed is not None:
            data["speed"] = selected.speed
        if selected.custom_voice is not None and self.kind in {"openai", "compatible"}:
            data["options"]["custom_voice"] = selected.custom_voice
        if selected.resource_id is not None and self.kind == "volcengine":
            data["options"]["resource_id"] = selected.resource_id
        if selected.tone is not None:
            data["tone"] = selected.tone.model_dump()
        if self.kind == "breeze" and self.options.breeze_api_mode == "self_hosted":
            if selected.reference is not None:
                data["options"]["breeze_mode"] = "reference"
            elif selected.origin == "design":
                data["options"]["breeze_mode"] = "design"
        if selected.reference is None:
            return Profile.model_validate(data)
        reference = selected.reference
        data["options"].update(reference_source=reference.source, reference_text=reference.text)
        if self.kind == "siliconflow":
            data["options"]["reference_enabled"] = True
        data["options"][
            {"url": "reference_url", "file": "reference_file", "server": "reference_path"}[reference.source]
        ] = reference.url if reference.source == "url" else reference.path
        return Profile.model_validate(data)

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("API Key 不能包含换行")
        return value.strip()


class Features(Model):
    delivery_format: Literal["silk", "source"] = "silk"
    command_enabled: bool = True
    tool_enabled: bool = True
    probability_enabled: bool = False
    probability: float = Field(default=0.2, ge=0, le=1)
    probability_min_text_length: int = Field(default=1, ge=1, le=4096)
    probability_max_text_length: int = Field(default=300, ge=1, le=4096)
    probability_group_enabled: bool = True
    probability_private_enabled: bool = True
    probability_keep_text: bool = False
    tool_keep_text: bool = False
    allow_voice_override: bool = False
    allow_speed_override: bool = True
    tone_prompt_language: Literal["zh", "en", "ja"] = "zh"

    @model_validator(mode="after")
    def validate_lengths(self) -> "Features":
        if self.probability_min_text_length > self.probability_max_text_length:
            raise ValueError("概率 TTS 最短文本不能大于最长文本")
        return self


class RouteTarget(Model):
    provider_id: str = Field(min_length=1, max_length=64)
    model_id: str = Field(default="", max_length=200)
    voice: Optional[str] = Field(default=None, max_length=500)


class StudioConfig(Model):
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    primary: Optional[RouteTarget] = None
    fallbacks: List[RouteTarget] = Field(default_factory=list, max_length=20)
    profiles: List[Profile] = Field(default_factory=list, max_length=20)
    feature: Features = Field(default_factory=Features)

    @model_validator(mode="before")
    @classmethod
    def read_saved_provider_routes(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        previous = data.pop("default_provider", "")
        backups = data.pop("fallback_providers", [])
        profiles = {item.get("id"): item for item in data.get("profiles", []) if isinstance(item, dict)}

        def target(provider_id: str) -> Dict[str, Any]:
            profile = profiles.get(provider_id, {})
            return {"provider_id": provider_id, "model_id": profile.get("model", ""), "voice": profile.get("voice", "")}

        if "primary" not in data and previous:
            data["primary"] = target(previous)
        if "fallbacks" not in data and backups:
            data["fallbacks"] = [target(provider_id) for provider_id in backups]
        return data

    @property
    def default_provider(self) -> str:
        return self.primary.provider_id if self.primary is not None else ""

    @property
    def fallback_providers(self) -> List[str]:
        return [target.provider_id for target in self.fallbacks]

    @model_validator(mode="after")
    def validate_routing(self) -> "StudioConfig":
        ids = [profile.id for profile in self.profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("服务商 ID 重复")
        route = ([self.primary] if self.primary is not None else []) + self.fallbacks
        keys = [(target.provider_id, target.model_id) for target in route]
        if len(keys) != len(set(keys)):
            raise ValueError("首选与备选路由的服务商/模型不能重复")
        enabled = {profile.id for profile in self.profiles if profile.enabled}
        if any(target.provider_id not in enabled for target in route):
            raise ValueError("默认与备选列表只能选择已启用的服务商")
        profiles = {profile.id: profile for profile in self.profiles}
        for target in route:
            profile = profiles[target.provider_id]
            if target.model_id and target.model_id not in {item.id for item in profile.models}:
                raise ValueError("路由中的模型需要存在于对应服务商的模型库，请先调整整体路由")
        if self.fallbacks and self.primary is None:
            raise ValueError("请先选择首选路由")
        return self


@dataclass(frozen=True)
class Audio:
    data: bytes
    content_type: str
    response_format: str
    provider_id: str = ""
    request_id: str = ""


def safe_validation_error(error: Exception) -> str:
    """不把 Pydantic 的 input_value（可能含密钥）带入错误提示。"""
    if isinstance(error, ValidationError):
        return "；".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
    return str(error)
