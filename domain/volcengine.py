"""火山 V3 的资源、模型版本与语音指令约束。"""

from html import escape
from typing import List

import re


TTS_RESOURCES = ["seed-tts-2.0", "seed-tts-1.0", "seed-tts-1.0-concurr"]
CLONE_RESOURCES = ["seed-icl-2.0", "seed-icl-1.0", "seed-icl-1.0-concurr"]
RESOURCES = TTS_RESOURCES + CLONE_RESOURCES
STANDARD_MODEL = "seed-tts-2.0-standard"
EXPRESSIVE_MODEL = "seed-tts-2.0-expressive"
CLONE_LANGUAGE_NAMES = {
    0: "中文", 1: "英文", 2: "日语", 3: "西班牙语", 4: "印尼语", 5: "葡萄牙语",
    6: "德语", 7: "法语", 8: "韩语", 9: "意大利语", 10: "泰语", 11: "越南语",
    12: "俄语", 13: "菲律宾语", 14: "马来语", 15: "阿拉伯语", 16: "墨西哥西班牙语",
    17: "巴西葡萄牙语", 19: "波兰语", 20: "土耳其语", 21: "瑞典语",
}
CLONE_LANGUAGES = list(CLONE_LANGUAGE_NAMES)
RESERVED_SPEAKER = re.compile(
    r"^(?:(?i:S_|ICL_|MIX_|DiT_|BV)|[a-z]{2}_|(?i:wvae|moon|mercury|venus|earth|mars|jupiter|saturn|uranus|neptune|pluto|umm)_)"
    r"|_(?i:bigtts|bigtts_cc|tob|cs_tob|streaming)$",
)


def synthesis_model(model: str, resource: str, instructions: str) -> str:
    """公版资源不传复刻效果版本；通用模型配置按请求能力明确选择版本。"""
    if resource != "seed-icl-2.0":
        return ""
    if model not in ("", "seed-tts-2.0"):
        return model
    return EXPRESSIVE_MODEL if instructions else STANDARD_MODEL


def supports_instructions(model: str, resource: str) -> bool:
    return resource == "seed-tts-2.0" or (
        resource == "seed-icl-2.0" and synthesis_model(model, resource, "instructions") == EXPRESSIVE_MODEL
    )


def cot_text(text: str, instructions: str) -> str:
    """把受控指令限定在合成请求中，按官方建议控制每组 COT 的长度。"""
    opening = f'<cot text="{escape(instructions, quote=True)}">'
    closing = "</cot>"
    available = 64 - len(opening) - len(closing)
    if available < 1:
        raise ValueError("COT 风格与语调指令过长，请缩短指令或使用整段语音指令")
    pieces: List[str] = []
    fragment = ""
    for character in text:
        encoded = escape(character, quote=False)
        if len(encoded) > available:
            raise ValueError("COT 指令过长，无法容纳转义后的正文字符，请缩短指令")
        if len(fragment) + len(encoded) > available:
            pieces.append(opening + fragment + closing)
            fragment = ""
        fragment += encoded
    if fragment:
        pieces.append(opening + fragment + closing)
    return "".join(pieces)
