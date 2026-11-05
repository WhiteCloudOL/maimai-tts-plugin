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


def control_prompt(language: str, *, automatic: bool, tags: Dict[str, List[str]]) -> str:
    """只注入常用事件和停顿规则；完整标签表由语调查询工具按需返回。"""
    table = "\n".join(f"{code}: {' '.join(values[index] for index in COMMON_EVENT_INDICES)}" for code, values in tags.items())
    descriptions = {
        "zh": (
            "Breeze：用 [语气:描述]（也支持 [用温柔的语气]）控制后文，至下一标记前；每段 1–200 字，一条最多 8 个。"
            "描述与台词同语言；可省略标记沿用默认语调，或在开头使用一个允许的 [mmtts:ID]。"
            "用标点组织停连，段间停顿由插件补齐；段内留白按需插入表内停顿标签，一处一个，不堆叠或自造时长写法。"
            "事件留在合成正文；自由语气提取成指令，聊天文字隐藏标记。台词必须完整，普通括号保留。"
            "下面是常用事件；其他事件先查询 mmtts_tone_options。"
        ),
        "en": (
            "Breeze: [style:direction] (also [语气:描述] or [用温柔的语气]) applies until the next marker; "
            "1–200 characters each, eight markers per reply. Match the dialogue language. Omit markers for the "
            "default tone, or use one allowed leading [mmtts:ID]. The plugin supplies gaps between sections. "
            "Use punctuation and one listed pause where needed within a section; do not stack pauses or invent duration syntax. "
            "Events stay in speech input; directions become separate instructions. Chat text hides controls. "
            "Include complete dialogue; preserve ordinary brackets. Common events follow; query mmtts_tone_options for others."
        ),
        "ja": (
            "Breeze：[話し方:説明]（[语气:描述]、[用温柔的语气] も可）は次の指定まで適用。"
            "説明は台詞と同じ言語で各 1–200 文字、返信ごとに最大 8 個。省略時は既定の語調、"
            "または許可された [mmtts:ID] を先頭に一つ使えます。区間の間はプラグインが間合いを確保します。"
            "区間内では句読点と必要に応じて表の間合いタグを一つ使い、連続挿入や独自の秒数指定は禁止です。"
            "イベントは合成本文に残し、話し方は独立した指示に変換、チャットではタグを隠します。"
            "台詞を省略せず、通常の括弧は保持。下表以外は mmtts_tone_options で確認してください。"
        ),
    }
    placement = {
        "zh": "仅用于本次语音回复；" if automatic else "仅用于 mmtts_speak.text，普通文字不加标记；",
        "en": "For this speech reply only. " if automatic else "Only in mmtts_speak.text; keep ordinary replies plain. ",
        "ja": "今回の音声返信だけに使用。" if automatic else "mmtts_speak.text だけに使用し、通常の返信には付けないでください。",
    }
    return placement[language] + descriptions[language] + "\n" + table
