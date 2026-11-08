"""各平台已核实的语调能力与统一标签协议；规则同时提供给工作台。"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import re

from .prompts import DEFAULT_PROMPTS, has_variable, render_prompt
from .bailian import instruction_length
from .volcengine import cot_text


TONE_LABELS = {
    "neutral": "平静",
    "happy": "开心",
    "sad": "悲伤",
    "angry": "生气",
    "fearful": "恐惧",
    "surprised": "惊讶",
    "disgusted": "厌恶",
    "excited": "兴奋",
    "gentle": "温柔",
    "serious": "严肃",
    "whisper": "轻声",
    "lively": "活泼",
    "relaxed": "放松",
}
TONE_DIRECTIONS = {
    "neutral": "以平静自然的语气说话。",
    "happy": "以开心愉快的语气说话。",
    "sad": "以悲伤低落的语气说话。",
    "angry": "以生气愤怒的语气说话。",
    "fearful": "以害怕恐惧的语气说话。",
    "surprised": "以惊讶的语气说话。",
    "disgusted": "以厌恶的语气说话。",
    "excited": "以兴奋激动的语气说话。",
    "gentle": "以温柔轻柔的语气说话。",
    "serious": "以严肃认真的语气说话。",
    "whisper": "压低声音，轻声耳语。",
    "lively": "以活泼轻快的语气说话。",
    "relaxed": "以放松舒缓的语气说话。",
}
COMMON = list(TONE_LABELS)
COSY_V3 = ["neutral", "happy", "sad", "angry", "fearful", "surprised", "disgusted"]
TAG_CANDIDATE = re.compile(r"\[/?mmtts[^\]\r\n]*(?:\]|(?=\r?\n)|$)|</?mmtts[^>\r\n]*(?:>|(?=\r?\n)|$)", re.IGNORECASE)
# 这里只声明已核对的模型协议；同名平台下的其他模型保持独立能力边界。
TONE_RULES: List[Dict[str, Any]] = [
    {
        "kind": "breeze",
        "model_pattern": r".*",
        "protocol": "instructions",
        "tones": COMMON,
        "note": "Breeze 支持风格与节奏指令；动态模式还支持局部音频事件与 [语气:描述]，控制标记不显示在聊天保留文字中。",
    },
    {
        "kind": "minimax",
        "model_pattern": r"speech-2\.6-(?:hd|turbo)",
        "protocol": "minimax_emotion",
        "tones": COSY_V3 + ["lively", "whisper"],
        "note": "MiniMax 官方 Speech 2.6 支持原生情感，活泼映射为 fluent，轻声映射为 whisper。",
    },
    {
        "kind": "minimax",
        "model_pattern": r"speech-(?:2\.8|02|01)-(?:hd|turbo)",
        "protocol": "minimax_emotion",
        "tones": COSY_V3,
        "note": "MiniMax 官方接口使用原生 emotion 参数，平静映射为 calm。",
    },
    {
        "kind": "compatible",
        "model_pattern": r"gpt-4o-mini-tts(?:-\d{4}-\d{2}-\d{2})?",
        "protocol": "instructions",
        "tones": COMMON,
        "note": "OpenAI 兼容模式下，此模型使用官方 instructions 控制语调。",
    },
    {
        "kind": "qwen",
        "model_pattern": r"qwen-audio-3\.0-tts-(?:plus|flash)",
        "excluded_voices": ["loongmary", "loongeva_v3.6", "loongjohn"],
        "protocol": "instructions",
        "tones": COMMON,
        "note": "Qwen-Audio 3.0 TTS 使用 instruction；精品英文音色按各自支持范围提供指令。",
    },
    {
        "kind": "qwen",
        "model_pattern": r"qwen-audio-3\.1-tts-(?:plus|flash)",
        "protocol": "instructions",
        "tones": COMMON,
        "note": "Qwen-Audio 3.1 TTS 音色使用 instruction 控制情感与说话方式。",
    },
    {
        "kind": "bailian_minimax",
        "model_pattern": r"MiniMax/speech-2\.8-(?:hd|turbo)",
        "protocol": "minimax_emotion",
        "tones": COSY_V3,
        "note": "MiniMax 2.8 使用原生 emotion 参数，平静映射为 calm。",
    },
    {
        "kind": "bailian_minimax",
        "model_pattern": r"MiniMax/speech-02-(?:hd|turbo)",
        "protocol": "minimax_emotion",
        "tones": COSY_V3 + ["whisper"],
        "note": "MiniMax 02 使用原生 emotion 参数，支持 whisper 轻声。",
    },
    {
        "kind": "openai",
        "model_pattern": r"gpt-4o-mini-tts(?:-\d{4}-\d{2}-\d{2})?",
        "protocol": "instructions",
        "tones": COMMON,
        "note": "此模型支持情绪、语气和轻声表达。",
    },
    {
        "kind": "qwen",
        "model_pattern": r"qwen3-tts-instruct-flash(?:-realtime)?(?:-\d{4}-\d{2}-\d{2})?",
        "protocol": "instructions",
        "tones": COMMON,
        "note": "千问 Instruct 模型可按指令调整情绪与语调。",
    },
    {
        "kind": "cosyvoice",
        "model_pattern": r"cosyvoice-v3\.5-(?:flash|plus)",
        "voice_pattern": r"cosyvoice-v3\.5-(?:flash|plus)-.+",
        "protocol": "instructions",
        "tones": COMMON,
        "note": "CosyVoice 3.5 的复刻、设计音色使用自然语言指令；请先创建或同步对应模型音色。",
    },
    {
        "kind": "cosyvoice",
        "model_pattern": r"cosyvoice-v3-(?:flash|plus)",
        "voices": ["longanyang", "longanhuan"],
        "protocol": "cosy_emotion",
        "tones": COSY_V3,
        "note": "此模型的龙安洋、龙安欢使用固定格式情感指令。",
    },
    {
        "kind": "cosyvoice",
        "model_pattern": r"cosyvoice-v3-flash",
        "voices": ["longhuhu_v3"],
        "protocol": "cosy_emotion",
        "tones": COSY_V3,
        "note": "CosyVoice v3 Flash 的龙呼呼使用固定格式情感指令。",
    },
    {
        "kind": "cosyvoice",
        "model_pattern": r"cosyvoice-v3-flash",
        "voice_pattern": r"cosyvoice-v3-flash-.+",
        "protocol": "instructions",
        "tones": COMMON,
        "note": "CosyVoice v3 Flash 的复刻与设计音色使用自然语言指令。",
    },
    {
        "kind": "siliconflow",
        "model_pattern": r"FunAudioLLM/CosyVoice2-0\.5B",
        "protocol": "silicon_prompt",
        "tones": COMMON,
        "note": "CosyVoice2 支持情绪、语速、角色与方言描述。",
    },
    {
        "kind": "mimo",
        "model_pattern": r"mimo-v2\.5-tts(?:-voiceclone|-voicedesign)?",
        "protocol": "instructions",
        "tones": COMMON,
        "note": "MiMo 支持情绪、语气与轻声表达，预设、克隆和声音设计可分别配置。",
    },
    {
        "kind": "volcengine",
        "model_pattern": r"(?:seed-tts-2\.0(?:-expressive)?)?",
        "resource_id": "seed-icl-2.0",
        "protocol": "instructions",
        "tones": COMMON,
        "note": "ICL 2.0 的 expressive 效果版本支持语音指令和 COT；通用模型在启用指令时使用 expressive。",
    },
    {
        "kind": "volcengine",
        "model_pattern": r"(?:seed-tts-2\.0(?:-(?:standard|expressive))?)?",
        "resource_id": "seed-tts-2.0",
        "protocol": "instructions",
        "tones": COMMON,
        "note": "豆包语音合成 2.0 音色支持语气控制，需搭配对应的合成资源。",
    },
]


def tone_capability(kind: str, model: str, voice: str, resource_id: str) -> Optional[Dict[str, Any]]:
    for rule in TONE_RULES:
        if rule["kind"] != kind or not re.fullmatch(rule["model_pattern"], model):
            continue
        if "voices" in rule and voice not in rule["voices"]:
            continue
        if voice in rule.get("excluded_voices", []) or (
            "voice_pattern" in rule and not re.fullmatch(rule["voice_pattern"], voice)
        ):
            continue
        if "resource_id" in rule and resource_id != rule["resource_id"]:
            continue
        return rule
    return None


@dataclass(frozen=True)
class TaggedText:
    text: str
    tone: Optional[str] = None
    error: str = ""
    tagged: bool = False
    direction: str = ""


def parse_tone_tag(text: str) -> TaggedText:
    """只解析一条回复开头的保留标签，重复、未知、嵌入正文及破损标签均报告错误。"""
    markers = list(TAG_CANDIDATE.finditer(text))
    if not markers:
        return TaggedText(text)
    clean = TAG_CANDIDATE.sub("", text).strip()
    match = re.match(r"^\s*\[mmtts:(?:([a-z]+)|style=([^\]\r\n]*))\]", text)
    if len(markers) != 1 or match is None or match.end() != markers[0].end():
        return TaggedText(clean, error="语调标签应为正文开头唯一的 [mmtts:语调ID]", tagged=True)
    tone, direction = match.groups()
    if direction is not None:
        direction = direction.strip()
        if not direction or len(direction) > 200 or "[" in direction:
            return TaggedText(clean, error="自由语气须为 200 字以内、不含控制标记的单行描述", tagged=True)
        if not clean:
            return TaggedText(clean, error="语调标签后需要填写朗读正文", tagged=True)
        return TaggedText(clean, tagged=True, direction=direction)
    if tone not in TONE_LABELS:
        return TaggedText(clean, error="语调标签包含未知的语调 ID", tagged=True)
    if not clean:
        return TaggedText(clean, error="语调标签后需要填写朗读正文", tagged=True)
    return TaggedText(clean, tone, tagged=True)


def validate_tone_settings(
    tone: Any, kind: str, model: str, voice: str, resource_id: str, instructions: str,
    *, instruction_mode: str = "context",
) -> None:
    cap = tone_capability(kind, model, voice, resource_id)
    if (tone.labels or tone.directions) and (cap is None or cap["protocol"] in {"cosy_emotion", "minimax_emotion"}):
        raise ValueError("此组合不开放语调名称和表达指令编辑，请恢复系统默认语调")
    if tone.mode == "off":
        return
    if cap is None:
        raise ValueError("此模型、音色或资源尚未确认支持受控语调，请选择平台已支持的组合")
    if tone.emotion not in cap["tones"] or (
        tone.mode == "dynamic" and any(value not in cap["tones"] for value in tone.allowed)
    ):
        raise ValueError("所选语调超出当前模型与音色的支持范围")
    if tone.mode == "dynamic" and tone.allowed and tone.emotion not in tone.allowed:
        raise ValueError("动态模式的默认语调需要包含在允许列表中")
    if cap["protocol"] == "cosy_emotion" and instructions:
        raise ValueError("CosyVoice 3.0 采用固定格式情感指令，启用语调时请清空自由风格描述")
    if cap["protocol"] == "minimax_emotion" and instructions:
        raise ValueError("MiniMax 使用原生情感选项，请清空自由风格描述")
    for direction in tone.directions.values():
        combined = "\n".join(value for value in (instructions, direction) if value)
        if kind == "cosyvoice" and instruction_length(combined) > 100:
            raise ValueError("CosyVoice 风格与语调指令合计上限 100 字符，汉字计 2，其余计 1")
        if kind in {"openai", "compatible"} and len(combined) > 4096:
            raise ValueError("风格与语调指令合计上限 4096 字符")
        if kind == "volcengine" and instruction_mode == "cot":
            cot_text("&", combined)


def tone_choices(allowed: List[str], labels: Dict[str, str], directions: Dict[str, str]) -> str:
    """稳定 ID 与用户编辑的含义同时供模型选择，原生枚举由配置校验禁止覆盖。"""
    return ", ".join(
        f"{key}({labels.get(key, TONE_LABELS[key])}" + (f": {directions[key]}" if key in directions else "") + ")"
        for key in allowed
    )


def tone_prompt(
    allowed: List[str],
    language: str,
    *,
    automatic: bool,
    default: str = "neutral",
    direction_limit: int = 0,
    weighted: bool = False,
    template: Optional[str] = None,
    labels: Optional[Dict[str, str]] = None,
    directions: Optional[Dict[str, str]] = None,
) -> str:
    choices = tone_choices(allowed, labels or {}, directions or {})
    prompts = DEFAULT_PROMPTS["dynamic"]
    placements = {
        "zh": "在回复正文开头添加标签；调用 mmtts_speak 时放在 text 参数开头。"
        if automatic
        else "仅在调用 mmtts_speak 的 text 参数开头添加标签，普通文字回复保持正文。",
        "en": "Add the tag at the start of the reply body, or the text argument of mmtts_speak. "
        if automatic
        else "Add the tag only to the text argument of mmtts_speak; keep ordinary replies as plain text. ",
        "ja": "返信本文、または mmtts_speak の text 引数の先頭にタグを付けてください。"
        if automatic
        else "タグは mmtts_speak の text 引数の先頭にだけ付け、通常のテキスト返信は本文をそのまま返してください。",
    }
    example = "happy" if "happy" in allowed else default if default in allowed else allowed[0]
    selected = template if template is not None else prompts[language]
    prompt = render_prompt(
        selected,
        {"allowed_tones": choices, "placement": placements[language],
         "default_tone": default, "example": example, "direction_limit": str(direction_limit)},
    )
    if not has_variable(selected, "placement"):
        prompt = placements[language] + "\n" + prompt
    if not has_variable(selected, "allowed_tones"):
        prompt += "\n[mmtts:ID]: " + choices
    controls = {
        "zh": "也可改用 [mmtts:style=说话方式] 指定整段自由语气，描述最多 {limit} 字符，单行且不含方括号；与 ID 标签二选一。正文保持纯文本，不能插入局部语气或音频事件。",
        "en": "Alternatively, use [mmtts:style=delivery description] for whole-utterance delivery, at most {limit} single-line characters without square brackets. Choose either this or the tone ID tag. Keep the body plain, without local directions or audio events.",
        "ja": "代わりに [mmtts:style=話し方の説明] で全体の話し方を指定できる。説明は一行、角括弧を含まず{limit}文字以内。IDタグとは併用せず、本文は台詞のみで、局所的な話し方や音声イベントは入れない。",
    }
    strict = {
        "zh": "当前模型采用限定语调，不能使用 style、自由语气或局部事件标签。",
        "en": "This model accepts only the listed tones: do not use style, free-form delivery or local event tags.",
        "ja": "このモデルは許可された語調のみを受け付ける。style、自由な話し方、局所的な音声イベントタグは使わない。",
    }
    counts = {
        "zh": "汉字计 2 字符，其他计 1。",
        "en": " Han characters count as 2; others as 1.",
        "ja": "漢字は2文字、他は1文字として数える。",
    }
    protocol = {
        "zh": "推荐按需加语调标签；不加时沿用默认。使用时最多一个开头标签：[mmtts:允许的ID]正文，ID 须精确匹配列表。",
        "en": "Tone tags are recommended when useful, not required; otherwise use the default. Use at most one leading [mmtts:allowed_ID] tag with an exact listed ID.",
        "ja": "語調タグは必要に応じた推奨で、必須ではない。省略時は既定を使う。使う場合は先頭に[mmtts:許可ID]を最大一つ、IDは一覧と完全一致させる。",
    }
    prompt += "\n" + protocol[language] + "\n"
    example_direction, example_text = {"zh": ("轻柔", "你好。"), "en": ("Softly", "Hello."), "ja": ("優しく", "こんにちは。")}[language]
    example_size = instruction_length(example_direction) if weighted else len(example_direction)
    style_example = (
        "\ntext: [mmtts:style=" + example_direction + "]" + example_text
        if direction_limit >= example_size else ""
    )
    return (
        prompt + controls[language].format(limit=direction_limit) + (counts[language] if weighted else "") + style_example
        if direction_limit
        else prompt + strict[language]
    )
