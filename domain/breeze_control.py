"""Breeze 局部事件与自由语气标记；保留朗读文本和聊天正文的独立表示。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import json
import re

from .prompts import DEFAULT_PROMPTS, render_prompt


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


# 官方各语言表按相同事件顺序排列：笑、轻笑、叹气、耳语、唱歌、哼歌、停顿。
COMMON_EVENT_INDICES = (0, 1, 8, 13, 16, 17, 19)
PAUSE_EVENT_INDEX = 19
PAUSE_TAGS = tuple({values[PAUSE_EVENT_INDEX] for values in AUDIO_TAGS.values()})


def boundary_pauses(texts: List[str]) -> List[int]:
    """语气区段的停顿由拼接阶段落实；原生边界停顿至少留出一个句末间隔。"""
    pauses: List[int] = []
    for left, right in zip(texts, texts[1:]):
        ending = clean_breeze_text(left).rstrip(" \t\r\n\"'”’»」』】）》）)]")
        explicit = left.rstrip().endswith(PAUSE_TAGS) or right.lstrip().startswith(PAUSE_TAGS)
        if explicit or ending.endswith(("。", "！", "？", ".", "!", "?", "…")):
            pauses.append(300)
        elif ending.endswith(("，", ",", "；", ";", "：", ":")):
            pauses.append(150)
        else:
            pauses.append(100)
    return pauses


def control_prompt(
    language: str,
    *,
    automatic: bool,
    tags: Dict[str, List[str]],
    adaptive: bool = False,
    template: Optional[str] = None,
) -> str:
    """只注入常用事件和停顿规则；完整标签表由语调查询工具按需返回。"""
    table = "\n".join(f"{code}: {' '.join(values[index] for index in COMMON_EVENT_INDICES)}" for code, values in tags.items())
    descriptions = DEFAULT_PROMPTS["breeze"]
    placement = {
        "zh": "仅用于本次语音回复；" if automatic else "仅用于 mmtts_speak.text，普通文字不加标记；",
        "en": "For this speech reply only. " if automatic else "Only in mmtts_speak.text; keep ordinary replies plain. ",
        "ja": "今回の音声返信だけに使用。" if automatic else "mmtts_speak.text だけに使用し、通常の返信には付けないでください。",
    }
    adaptive_rules = {
        "zh": "整段基调由后置自适应补充；仍按需生成局部语气、事件与停顿，无需生成 [mmtts:ID] 头标签。",
        "en": "Post-generation adaptation supplies the overall delivery; still add fitting local directions, events and pauses, without a leading [mmtts:ID]. ",
        "ja": "全体の基調は生成後の自動調整が補う。局所的な話し方、イベント、間合いは引き続き指定し、先頭の [mmtts:ID] は不要。",
    }
    selected = template if template is not None else descriptions[language]
    prompt = render_prompt(selected, {"placement": placement[language], "event_tags": table})
    constraints = {
        "zh": "控制格式：[语气:描述]作用于后文，到下一标记前；描述同台词语言、单行1–200字，每条最多8个。事件从表内选，整条使用同一种语言标签；一处一个停顿，不自造时长。完整保留台词。",
        "en": "Controls: [style:direction] applies until the next marker; use the dialogue language, 1–200 single-line characters, at most 8 markers. Use one listed tag language per reply and one pause per position; invent no durations. Preserve all dialogue.",
        "ja": "構文：[話し方:説明]は次の指定まで適用。同じ言語の一行1–200文字、最大8個。イベントは一覧の同じ言語で選び、一か所一つの間合いにし、秒数を自作しない。台詞は省略しない。",
    }
    if not re.search(r"\{\{\s*placement\s*\}\}", selected):
        prompt = placement[language] + prompt
    if not re.search(r"\{\{\s*event_tags\s*\}\}", selected):
        prompt += "\n" + table
    return prompt + "\n" + constraints[language] + ("\n" + adaptive_rules[language] if adaptive else "")
