"""Breeze 局部事件与自由语气标记；保留朗读文本和聊天正文的独立表示。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import json
import re


# 官方标签表：https://docs.breezeblue.ai/guides/audio-tags
AUDIO_TAGS: Dict[str, List[str]] = json.loads(
    (Path(__file__).resolve().parents[1] / "assets" / "breeze_audio_tags.json").read_text(encoding="utf-8")
)
# 开源版 README 使用这些单数形式，与在线多语言标签一起识别。
LOCAL_ALIASES = ["(laugh)", "(cough)", "(sigh)", "[清嗓子]"]
ALL_TAGS = sorted({tag for tags in AUDIO_TAGS.values() for tag in tags} | set(LOCAL_ALIASES), key=len, reverse=True)
EVENT_PATTERN = re.compile(
    r"(?<!\\)(?:" + "|".join(re.escape(tag) + (r"(?!\s*\()" if tag.startswith("[") else "") for tag in ALL_TAGS) + ")"
)
DIRECTION_PATTERN = re.compile(
    r"(?<!\\)\[(?:(?:语气|style|話し方)\s*[:：](?P<explicit>[^\[\]\r\n]+)"
    r"|(?P<natural>用[^\[\]\r\n]+?语气(?:说话|说)?))\](?!\s*\()",
    re.IGNORECASE,
)
DIRECTION_CANDIDATE = re.compile(r"(?<!\\)\[(?:语气|style|話し方)\s*[:：][^\]\r\n]*(?:\]|$)(?!\s*\()", re.IGNORECASE)


@dataclass(frozen=True)
class BreezePart:
    text: str
    direction: str = ""


@dataclass(frozen=True)
class BreezeText:
    parts: List[BreezePart]
    display_text: str
    marked: bool
    error: str = ""


def clean_breeze_text(text: str) -> str:
    """只去掉已定义的控制标记；普通方括号、Markdown 链接和转义文本保持原样。"""
    return EVENT_PATTERN.sub("", DIRECTION_CANDIDATE.sub("", DIRECTION_PATTERN.sub("", text))).strip()


def parse_breeze_text(text: str) -> BreezeText:
    matches = list(DIRECTION_PATTERN.finditer(text))
    candidates = list(DIRECTION_CANDIDATE.finditer(text))
    error = ""
    if any(not any(match.span() == candidate.span() for match in matches) for candidate in candidates):
        error = "Breeze 自由语气请使用完整的 [语气:描述]，描述不能为空且不能跨行"
    if len(matches) > 8:
        error = "每条回复最多使用 8 个 Breeze 自由语气标记"
    parts: List[BreezePart] = []
    start, direction = 0, ""
    for match in matches:
        fragment = text[start : match.start()].strip()
        if fragment:
            parts.append(BreezePart(fragment, direction))
        elif direction:
            error = "每个 Breeze 自由语气标记后需要填写朗读文本或音频事件"
        direction = (match.group("explicit") or match.group("natural")).strip()
        if not direction or len(direction) > 200:
            error = "Breeze 自由语气描述需要 1–200 字符"
        start = match.end()
    fragment = text[start:].strip()
    if fragment:
        parts.append(BreezePart(fragment, direction))
    elif direction:
        error = "Breeze 自由语气标记后需要填写朗读文本或音频事件"
    return BreezeText(parts, clean_breeze_text(text), bool(matches or candidates or EVENT_PATTERN.search(text)), error)


def event_tags(language: str = "", *, local: bool = False) -> Dict[str, List[str]]:
    """自动语言只提示中英文；其余语言使用模型中明确配置的语言标签表。"""
    languages = [language] if language and not local else ["zh", "en"]
    result = {code: list(AUDIO_TAGS[code]) for code in languages}
    if local:
        result["zh"] = ["[清嗓子]" if tag == "[清嗓]" else tag for tag in result["zh"]]
        result["en"] = [
            {"(laughs)": "(laugh)", "(coughs)": "(cough)", "(sighs)": "(sigh)"}.get(tag, tag) for tag in result["en"]
        ]
    return result


def control_prompt(language: str, *, automatic: bool, tags: Dict[str, List[str]]) -> str:
    """三种提示语言使用相同的控制语法与标签集合。"""
    table = "\n".join(f"{code}: {' '.join(values)}" for code, values in tags.items())
    descriptions = {
        "zh": (
            "【Breeze 动态声音控制】只在本次语音正文中使用以下标记。"
            "可在合适的位置少量插入表内音频事件，按朗读语言精确使用标签，不要自造或翻译标签。"
            "自由语气使用 [语气:描述]，也支持 [用温柔的语气]；标记作用于后文，直到下一个自由语气标记。"
            "每段描述用与台词相同的语言，最多 200 字符，一条回复最多 8 个标记。"
            "插件将其提取为独立指令并逐段合成；它们不作为台词朗读。"
            "无需自由描述时可以沿用整体默认语调，或使用允许列表内的唯一 [mmtts:ID] 头部标签。"
            "局部自由语气优先于整体语调。正文要有实际台词，普通括号内容不会被当作指令。"
            "例如：[语气:轻声而温柔][轻笑]你终于来了。[语气:认真且坚定]我们一起面对。"
            "所有控制标记均从聊天保留文字中隐藏。"
        ),
        "en": (
            "[Breeze dynamic voice control] Use these controls only in speech input. Insert a few listed audio events "
            "at suitable positions, using the exact spelling for the spoken language; never invent or translate tags. "
            "Use [style:description] for free delivery directions; [语气:描述] and [用温柔的语气] are also accepted. "
            "A direction applies until the next direction marker. Write it in the dialogue language, at most 200 "
            "characters each and eight markers per reply. The plugin extracts separate instructions and synthesizes "
            "the sections in order. Without a free direction, use the configured default or one allowed [mmtts:ID] "
            "leading tag. Local directions take precedence over the overall tone. Include spoken dialogue; ordinary "
            "brackets are not instructions. Example: [style:soft and warm](laughs)You are here."
            "[style:serious and firm]We can face this together. Control markers are hidden from retained chat text."
        ),
        "ja": (
            "【Breeze の動的な音声制御】音声用の本文にだけ制御タグを使用してください。"
            "下表の音声イベントを適切な位置に少量挿入し、朗読言語に対応する表記を厳密に使ってください。"
            "タグの創作や独自翻訳は禁止です。自由な話し方は [話し方:説明] で指定し、"
            "[语气:描述] や [用温柔的语气] も使用できます。次の指定までの本文に適用されます。"
            "説明は台詞と同じ言語で各 200 文字以内、返信ごとに最大 8 個にしてください。"
            "プラグインが独立した指示に変換し、区間ごとに順番に合成します。"
            "自由指定がなければ設定済みの語調か、許可された唯一の先頭タグ [mmtts:ID] を使用できます。"
            "局所指定は全体の語調より優先されます。実際の台詞を含め、通常の括弧を指示として使わないでください。"
            "例：[話し方:優しく静かに]会えてうれしいです。[話し方:真剣に]一緒に考えましょう。"
            "制御タグはチャットに残す本文から除去されます。"
        ),
    }
    placement = {
        "zh": "用于当前回复正文或 mmtts_speak 的 text；"
        if automatic
        else "仅用于 mmtts_speak 的 text，普通文字回复不要添加；",
        "en": "Apply to this reply or mmtts_speak.text. "
        if automatic
        else "Apply only to mmtts_speak.text; keep ordinary replies plain. ",
        "ja": "今回の返信本文または mmtts_speak.text に適用します。"
        if automatic
        else "mmtts_speak.text だけに適用し、通常の返信には付けないでください。",
    }
    description = descriptions[language].replace("(laughs)", tags.get("en", [""])[0])
    return placement[language] + description + "\n" + table
