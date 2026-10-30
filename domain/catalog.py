"""平台预设与模型链接解析；不会访问模型页面或执行页面脚本。"""

from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlsplit

from .models import TTSError, validate_url
from .bailian import DESIGN_MODELS, MINIMAX_LANGUAGES, MINIMAX_MODELS, QWEN_AUDIO_MODELS
from .minimax import FORMATS as MINIMAX_FORMATS, MODELS as NATIVE_MINIMAX_MODELS
from .breeze import (
    CLOUD_BASE_URL as BREEZE_CLOUD_URL,
    LANGUAGES as BREEZE_LANGUAGES,
    LOCAL_BASE_URL as BREEZE_LOCAL_URL,
    MODELS as BREEZE_MODELS,
)


OPENAI_MODELS = ["gpt-4o-mini-tts", "gpt-4o-mini-tts-2025-12-15", "gpt-4o-mini-tts-2025-03-20", "tts-1", "tts-1-hd"]
OPENAI_VOICES = [
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
]
OPENAI_LEGACY_VOICES = ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"]


CATALOG: Dict[str, Dict[str, Any]] = {
    "breeze": {
        "name": "BreezeTTS",
        "tag": "本地API / 官方在线",
        "mark": "BZ",
        "color": "#558e99",
        "endpoint": BREEZE_LOCAL_URL + "/audio/speech",
        "base_url": BREEZE_LOCAL_URL,
        "cloud_base_url": BREEZE_CLOUD_URL,
        "model": "breeze-tts-2",
        "voice": "",
        "models": BREEZE_MODELS,
        "voices": [],
        "formats": ["wav", "mp3", "flac", "pcm", "aac", "opus"],
        "languages": BREEZE_LANGUAGES,
        "models_url": "",
        "voices_url": "",
        "clone_url": "",
        "note": (
            "本地API 使用参考录音与准确文字稿；官方在线使用已保存音色，支持账号音色、克隆和设计。"
            "API Key 选填，填写后使用 Bearer 鉴权。"
        ),
        "tone_hint": "支持自然语言风格与语调指令；固定语调直接使用配置，动态语调按允许列表解析。",
        "docs": "https://docs.breezeblue.ai/",
    },
    "openai": {
        "name": "OpenAI",
        "tone_hint": "gpt-4o-mini-tts 系列支持风格与语调；tts-1 / tts-1-hd 使用音色和语速等原生参数。",
        "tag": "Speech API",
        "mark": "OA",
        "color": "#558570",
        "endpoint": "https://api.openai.com/v1/audio/speech",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini-tts",
        "voice": "alloy",
        "models": OPENAI_MODELS,
        "voices": OPENAI_VOICES,
        "model_voices": {"tts-1": OPENAI_LEGACY_VOICES, "tts-1-hd": OPENAI_LEGACY_VOICES},
        "formats": ["mp3", "wav", "opus", "aac", "flac", "pcm"],
        "models_url": "https://api.openai.com/v1/models",
        "voices_url": "",
        "clone_url": "https://api.openai.com/v1/audio/voices",
        "note": "支持预设与自定义音色；可用本地录音和已有授权记录 ID 创建克隆音色。风格指令适用于 gpt-4o-mini-tts。",
        "docs": "https://platform.openai.com/docs/guides/text-to-speech",
    },
    "qwen": {
        "name": "阿里云_千问",
        "tone_hint": (
            "Qwen3-Flash 使用原生朗读；语调控制可选择 Qwen-Audio 3.0/3.1 或 Qwen3 Instruct。"
            "精品英文音色按各自支持范围提供指令。"
        ),
        "tag": "Qwen TTS",
        "mark": "QW",
        "color": "#8a77ad",
        "endpoint": "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer",
        "base_url": "https://dashscope.aliyuncs.com/api/v1",
        "model": "qwen-audio-3.1-tts-flash",
        "voice": "longanhuan_v3.1",
        "audio_models": sorted(QWEN_AUDIO_MODELS),
        "design_models": sorted(QWEN_AUDIO_MODELS),
        "models": [
            "qwen-audio-3.1-tts-flash",
            "qwen-audio-3.1-tts-plus",
            "qwen-audio-3.0-tts-flash",
            "qwen-audio-3.0-tts-plus",
            "qwen3-tts-flash",
            "qwen3-tts-instruct-flash",
            "qwen3-tts-instruct-flash-realtime",
            "qwen-tts",
            "qwen3-tts-vc-realtime-2026-01-15",
            "qwen3-tts-flash-realtime",
        ],
        "voices": ["Cherry", "Serena", "Ethan", "Chelsie"],
        "model_voices": {
            "qwen-audio-3.1-tts-flash": [
                "longanhuan_v3.1",
                "longanlingxin_v3.1",
                "longanfengyue_v3.1",
                "xunanchuan_v3.1",
                "yuxiaoyun_v3.1",
                "qiaoxiaojiao_v3.1",
                "xiaxiaochen_v3.1",
                "anmingyuan_v3.1",
                "wenhuaiqing_v3.1",
                "longanyang_v3.1",
                "longanya_v3.1",
                "longwan_v3.1",
                "longhua_v3.1",
                "Emily_v3.1",
                "Luna_v3.1",
                "Eric_v3.1",
                "Abby_v3.1",
                "Andy_v3.1",
            ],
            "qwen-audio-3.1-tts-plus": [
                "longanhuan_v3.1",
                "longanlingxin_v3.1",
                "longanfengyue_v3.1",
                "xunanchuan_v3.1",
            ],
            "qwen-audio-3.0-tts-plus": ["longanlingxin", "longanlufeng"],
            "qwen-audio-3.0-tts-flash": [
                "longanhuan_v3.6",
                "longanfengyue",
                "longanyuanfei",
                "longanlingxi",
                "longanxiaoxin",
                "longjielidou_v3.6",
                "longpaopao_v3.6",
                "longhuohuo_v3.6",
                "longchuanshu_v3.6",
                "loongmary",
                "loongeva_v3.6",
                "loongjohn",
            ],
        },
        "formats": ["wav", "mp3", "pcm", "opus"],
        "models_url": "",
        "voices_url": "",
        "clone_url": "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
        "modes": {
            "preset": {
                "model": "qwen3-tts-flash",
                "voice": "Cherry",
                "endpoint": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
            },
            "clone": {
                "model": "qwen3-tts-vc-realtime-2026-01-15",
                "voice": "",
                "endpoint": "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-vc-realtime-2026-01-15",
            },
        },
        "note": (
            "支持 Qwen-Audio 3.0/3.1 TTS 的预设、克隆和声音设计，"
            "以及 Qwen3-TTS HTTP / Realtime。音色绑定创建时的模型与地域。"
        ),
        "docs": "https://docs.bailian.console.aliyun.com/zh/model-studio/qwen-audio-tts-http-api",
    },
    "cosyvoice": {
        "name": "阿里云_CosyVoice",
        "tone_hint": (
            "v3 Plus 的龙安洋、龙安欢和 v3 Flash 的对应情感音色支持语调；"
            "v3.5 先创建或同步复刻、设计音色。龙安欢_v3 的专属指令用于方言。"
        ),
        "tag": "HTTP / WebSocket",
        "mark": "CV",
        "color": "#b48a5e",
        "endpoint": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        "base_url": "https://dashscope.aliyuncs.com/api/v1",
        "model": "cosyvoice-v3-flash",
        "voice": "longanyang",
        "design_models": sorted(DESIGN_MODELS - QWEN_AUDIO_MODELS),
        "models": [
            "cosyvoice-v3-flash",
            "cosyvoice-v3-plus",
            "cosyvoice-v3.5-flash",
            "cosyvoice-v3.5-plus",
            "cosyvoice-v2",
        ],
        "model_voices": {
            "cosyvoice-v3-flash": ["longanyang", "longanhuan", "longhuhu_v3", "longanhuan_v3"],
            "cosyvoice-v3-plus": ["longanyang", "longanhuan"],
            "cosyvoice-v3.5-flash": [],
            "cosyvoice-v3.5-plus": [],
            "cosyvoice-v2": ["longxiaochun_v2", "longxiaoxia_v2", "longcheng_v2"],
        },
        "voices": ["longanyang", "longanhuan", "longxiaochun"],
        "formats": ["mp3", "wav", "pcm", "opus"],
        "models_url": "",
        "voices_url": "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
        "clone_url": "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
        "note": (
            "支持 HTTP 与 WebSocket 合成、本地 MP3/WAV 或公开 URL 复刻，以及 v3/v3.5 声音设计；账号音色按绑定模型管理。"
        ),
        "docs": "https://docs.bailian.console.aliyun.com/zh/model-studio/cosyvoice-tts-http-api",
    },
    "minimax": {
        "name": "MiniMax",
        "tag": "官方 Speech API",
        "mark": "MX",
        "color": "#a47783",
        "base_url": "https://api.minimax.cn/v1",
        "endpoint": "https://api.minimax.cn/v1/t2a_v2",
        "model": "speech-2.8-hd",
        "voice": "male-qn-qingse",
        "models": NATIVE_MINIMAX_MODELS,
        "design_models": NATIVE_MINIMAX_MODELS,
        "language_boosts": MINIMAX_LANGUAGES,
        "voices": ["male-qn-qingse"],
        "formats": MINIMAX_FORMATS,
        "models_url": "",
        "voices_url": "",
        "clone_url": "",
        "tone_hint": "各代模型支持原生情感；Speech 2.6 额外支持活泼与轻声。",
        "note": (
            "MiniMax 官方 HTTP / SSE / WebSocket 合成、混合音色、文件复刻、声音设计和账号音色管理；"
            "本地录音支持 MP3 / M4A / WAV。"
        ),
        "docs": "https://platform.minimax.cn/docs/api-reference/speech-t2a-http",
    },
    "bailian_minimax": {
        "name": "阿里云_MiniMax",
        "tag": "Speech 2.8 / 02",
        "mark": "MM",
        "color": "#a47783",
        "base_url": "https://dashscope.aliyuncs.com/api/v1",
        "endpoint": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        "model": "MiniMax/speech-2.8-turbo",
        "language_boosts": MINIMAX_LANGUAGES,
        "voice": "male-qn-qingse",
        "models": sorted(MINIMAX_MODELS),
        "voices": ["male-qn-qingse"],
        "formats": ["mp3", "wav", "pcm", "flac"],
        "models_url": "",
        "voices_url": "",
        "clone_url": "",
        "note": (
            "百炼 MiniMax 同步合成、原生情感、账号音色与快速复刻。"
            "复刻录音使用公网 URL，BaseURL 使用北京 DashScope 地址。"
        ),
        "docs": "https://docs.bailian.console.aliyun.com/zh/model-studio/minimax-synchronous-speech-synthesis-api",
    },
    "siliconflow": {
        "name": "硅基流动",
        "tone_hint": "CosyVoice2 支持指令前缀控制语调；MOSS-TTSD 按正文与参考录音表现情感，使用原生合成参数。",
        "tag": "CosyVoice / MOSS",
        "mark": "SF",
        "color": "#657ec4",
        "endpoint": "https://api.siliconflow.cn/v1/audio/speech",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "FunAudioLLM/CosyVoice2-0.5B",
        "voice": "FunAudioLLM/CosyVoice2-0.5B:anna",
        "models": ["FunAudioLLM/CosyVoice2-0.5B", "fnlp/MOSS-TTSD-v0.5"],
        "voices": ["alex", "benjamin", "charles", "david", "anna", "bella", "claire", "diana"],
        "formats": ["mp3", "wav", "opus", "pcm"],
        "models_url": "https://api.siliconflow.cn/v1/models",
        "voices_url": "https://api.siliconflow.cn/v1/audio/voice/list",
        "clone_url": "https://api.siliconflow.cn/v1/uploads/audio/voice",
        "note": "预设音色使用 模型:音色；支持同步账号音色、上传本地录音或公开音频 URL 创建音色及动态参考音频。",
        "docs": "https://docs.siliconflow.cn/cn/userguide/capabilities/text-to-speech",
    },
    "volcengine": {
        "name": "火山引擎",
        "tone_hint": "语调控制请搭配 seed-tts-2.0；使用 seed-icl-2.0 复刻音色时，将模型版本留空可启用语调。",
        "tag": "豆包语音",
        "mark": "DB",
        "color": "#bc7965",
        "endpoint": "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
        "base_url": "https://openspeech.bytedance.com/api/v3",
        "model": "",
        "voice": "zh_female_vv_uranus_bigtts",
        "models": ["seed-tts-2.0-standard", "seed-tts-2.0-expressive"],
        "voices": ["zh_female_vv_uranus_bigtts"],
        "formats": ["mp3", "wav", "pcm", "opus"],
        "models_url": "",
        "voices_url": "",
        "clone_url": "https://openspeech.bytedance.com/api/v3/tts/voice_clone",
        "note": (
            "V3 HTTP 单向流式协议；支持 API Key 或 App ID + Access Token。可用本地录音创建复刻音色，并查询训练状态。"
        ),
        "docs": "https://docs.volcengine.com/docs/doubao-voice/unidirectional-streaming-text-to-speech-http?lang=zh",
    },
    "mimo": {
        "name": "小米MiMo",
        "tag": "Chat Audio",
        "mark": "MI",
        "color": "#c18b50",
        "endpoint": "https://api.xiaomimimo.com/v1/chat/completions",
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5-tts",
        "voice": "mimo_default",
        "models": ["mimo-v2.5-tts", "mimo-v2.5-tts-voicedesign", "mimo-v2.5-tts-voiceclone"],
        "voices": ["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"],
        "formats": ["wav", "mp3", "pcm"],
        "models_url": "https://api.xiaomimimo.com/v1/models",
        "voices_url": "",
        "clone_url": "",
        "modes": {
            "preset": {"model": "mimo-v2.5-tts", "voice": "mimo_default"},
            "clone": {"model": "mimo-v2.5-tts-voiceclone", "voice": ""},
            "design": {"model": "mimo-v2.5-tts-voicedesign", "voice": ""},
        },
        "note": (
            "音色页可选择预设、音频克隆或文字设计。克隆录音自动转换为 audio.voice 的 Base64；设计模式支持播报文本优化。"
        ),
        "docs": "https://mimo.mi.com/docs/zh-CN/api/audio/tts",
    },
    "gpt_sovits": {
        "name": "GPT-SoVITS",
        "tone_hint": "API v2 通过参考录音的表现传递语气，模型参数提供语速等原生控制。",
        "tag": "API v2",
        "mark": "GS",
        "color": "#76969b",
        "endpoint": "http://127.0.0.1:9880/tts",
        "base_url": "http://127.0.0.1:9880",
        "model": "",
        "voice": "",
        "models": [],
        "voices": [],
        "formats": ["wav"],
        "models_url": "",
        "voices_url": "",
        "clone_url": "",
        "note": "适配官方 api_v2.py /tts；参考音频路径指 GPT-SoVITS 服务端路径，须已加载对应权重。",
        "docs": "https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py",
    },
    "compatible": {
        "name": "OpenAI兼容服务",
        "tone_hint": "官方 Speech 的 gpt-4o-mini-tts 系列支持 instructions；其他模型可按服务文档配置自定义字段与风格。",
        "tag": "OpenAI / 自定义 HTTP",
        "mark": "API",
        "color": "#7a8580",
        "endpoint": "https://api.openai.com/v1/audio/speech",
        "base_url": "https://api.openai.com/v1",
        "model": "tts-1",
        "voice": "alloy",
        "models": OPENAI_MODELS,
        "voices": OPENAI_VOICES,
        "model_voices": {"tts-1": OPENAI_LEGACY_VOICES, "tts-1-hd": OPENAI_LEGACY_VOICES},
        "formats": ["mp3", "wav", "opus", "aac", "flac", "pcm"],
        "models_url": "",
        "voices_url": "",
        "clone_url": "",
        "note": "默认完整使用 OpenAI Speech 协议；也可为每个模型选择自定义 HTTP 请求模板、鉴权和响应字段。",
        "docs": "https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create",
    },
}


def resolve_model_url(value: str, kind: str) -> Dict[str, str]:
    """识别受支持的模型详情页或完整接口地址，不猜测未知站点的 API。"""
    validate_url(value)
    if kind not in CATALOG:
        raise TTSError("未知服务商类型")
    parsed = urlsplit(value)
    path = unquote(parsed.path).strip("/")
    host = parsed.hostname
    preset = CATALOG[kind]
    result: Dict[str, str] = {}
    if host in {"cloud.siliconflow.cn", "cloud.siliconflow.com"}:
        query = parse_qs(parsed.query)
        model = query.get("model", [""])[0]
        if "/" in model:
            result = {"kind": "siliconflow", "model": model, "endpoint": CATALOG["siliconflow"]["endpoint"]}
        elif path.startswith("models/") and "/" in path[7:]:
            result = {"kind": "siliconflow", "model": path[7:], "endpoint": CATALOG["siliconflow"]["endpoint"]}
    elif host in {"huggingface.co", "www.modelscope.cn", "modelscope.cn"}:
        model_path = path.removeprefix("models/")
        parts = model_path.split("/")
        if len(parts) >= 2 and all(parts[:2]):
            result = {"model": "/".join(parts[:2])}
    elif path.endswith(
        ("audio/speech", "chat/completions", "multimodal-generation/generation", "tts", "unidirectional", "t2a_v2")
    ):
        result = {"endpoint": value}
    if not result:
        raise TTSError("无法识别该链接。请粘贴完整合成接口 URL，或填写模型 ID；模型托管页不一定提供推理接口。")
    if "endpoint" not in result:
        result["endpoint"] = preset["endpoint"]
    return result
