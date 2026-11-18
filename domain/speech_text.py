"""从原文提取朗读正文，保留独立的显示与语气参考文本。"""

from typing import List, Tuple

import re

from .intonation import parse_tone_tag


# 朗读正文里的括号动作描写与对应闭合符号；方括号是插件的语调控制标签，不参与清理。
_STAGE_DIRECTION_PAIRS = {"（": "）", "(": ")"}
_STAGE_DIRECTION_CLOSERS = {closing: opening for opening, closing in _STAGE_DIRECTION_PAIRS.items()}
_HAS_CHINESE = re.compile(r"[一-鿿]")
_BRACKETED_TEXT = re.compile(r"\[[^\[\]\r\n]*\]")
_SPACES_OUTSIDE_BRACKETS = re.compile(_BRACKETED_TEXT.pattern + r"|[ \t]{2,}")


def strip_stage_directions(text: str) -> str:
    """去掉朗读正文里的括号动作描写，只保留要说出口的话。

    角色用括号写环境与动作描写时，这些内容只应出现在文字消息里；
    朗读时直接读出来会显得僵硬，因此合成前剔除含中文的圆括号内容。

    方括号是插件的语调控制标签（形如 ``[mmtts:ID]``），不参与清理；
    仅删除含中文的圆括号内容，英文括号等正常内容原样保留；
    删除后若正文为空则保留原文，避免合成出空音频。
    """
    if not text:
        return text

    spans: List[Tuple[int, int]] = []
    stack: List[Tuple[str, int]] = []
    # 屏蔽方括号内的内容并保留位置，避免将语气描述里的圆括号识别成台词。
    body = _BRACKETED_TEXT.sub(lambda match: " " * len(match[0]), text)
    for index, char in enumerate(body):
        if char in _STAGE_DIRECTION_PAIRS:
            stack.append((char, index))
        elif char in _STAGE_DIRECTION_CLOSERS:
            for depth in range(len(stack) - 1, -1, -1):
                if stack[depth][0] == _STAGE_DIRECTION_CLOSERS[char]:
                    _, open_index = stack.pop(depth)
                    if _HAS_CHINESE.search(text[open_index + 1:index]):
                        spans.append((open_index, index + 1))
                    break

    if not spans:
        return text

    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    pieces = []
    cursor = 0
    for start, end in merged:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    cleaned = _SPACES_OUTSIDE_BRACKETS.sub(
        lambda match: match[0] if match[0].startswith("[") else " ", "".join(pieces),
    ).strip()
    return cleaned or text


def spoken_text_of(raw_text: str, *, strip: bool) -> str:
    """合成链路唯一的朗读正文。

    语调 LLM 生成事件锚点、校验锚点、插入锚点都必须基于同一份文本，
    否则清洗前后的锚点会错位（丢失事件或判定为重复锚点而报错）。
    语调分析和预校验使用与合成相同的清理规则，原文仍用于显示与语气参考。
    """
    parsed = parse_tone_tag(raw_text.strip())
    base = parsed.text if not parsed.error else raw_text.strip()
    return strip_stage_directions(base) if strip else base
