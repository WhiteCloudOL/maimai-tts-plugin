"""硅基流动 CosyVoice2 的简短表达建议与平台专属语调默认值。"""

from typing import Dict


COSYVOICE2_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
# 经验建议用于配置预览和模型引导；请求校验继续使用平台实际参数范围。
RECOMMENDED_INSTRUCTION_LENGTH = 20
SHORT_TONE_DIRECTIONS: Dict[str, str] = {
    "neutral": "平静自然。",
    "happy": "开心愉快。",
    "sad": "悲伤低落。",
    "angry": "生气愤怒。",
    "fearful": "害怕恐惧。",
    "surprised": "惊讶。",
    "disgusted": "厌恶。",
    "excited": "兴奋激动。",
    "gentle": "温柔轻柔。",
    "serious": "严肃认真。",
    "whisper": "轻声耳语。",
    "lively": "活泼轻快。",
    "relaxed": "放松舒缓。",
}


def short_instruction_guidance(kind: str, model: str, language: str, *, instructions: str = "") -> str:
    """将短语建议附加到实际路由提示，保留中英日模板和正文各自的稳定前缀。"""
    if kind != "siliconflow" or model != COSYVOICE2_MODEL:
        return ""
    style_length = len(instructions)
    remaining = max(0, RECOMMENDED_INSTRUCTION_LENGTH - style_length - bool(instructions))
    return {
        "zh": "CosyVoice2 的自由语气优先用简短说话方式，例如“轻声安慰。”；中文通常 5–10 字即可。"
        "既有风格与语调合计建议约 20 字，这是表达建议；使用 ID 标签时由插件套用该语调的表达指令。"
        "当前风格占 {style_length} 字符，自由描述建议余量 {remaining} 字符；余量为 0 时优先选语调 ID 或沿用默认。"
        "语气描述只写说话方式，省去人物背景设定；完整台词仍保留在朗读正文中。",
        "en": "For CosyVoice2, prefer one brief delivery phrase such as 'Speak softly.' "
        "Aim for around 20 characters for the configured style plus delivery as a recommendation. "
        "The configured style uses {style_length} characters; the recommended delivery budget is {remaining}. "
        "If that budget is 0, prefer a tone ID or the default. A tone ID applies its configured direction. "
        "Describe how to speak and keep character biographies out of the direction; "
        "keep the complete dialogue in the speech text.",
        "ja": "CosyVoice2の自由な話し方は「優しく話す。」など短い句を推奨する。"
        "既存のスタイルと語調の合計は20文字程度を目安にする。スタイルは{style_length}文字、説明の推奨残り枠は{remaining}文字。"
        "残り枠が0なら語調IDか既定を優先する。IDタグでは設定済みの表現指示を使う。"
        "説明は話し方に絞り、人物の背景設定を加えず、台詞全体は朗読本文に置く。",
    }[language].format(style_length=style_length, remaining=remaining)
