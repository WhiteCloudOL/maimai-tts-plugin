"""朗读正文清洗与 Breeze 事件锚点一致性回归。

PR #8 review：事件锚点必须用同一份最终朗读文本生成、校验、插入，
否则清洗会让锚点消失或变重复，导致事件丢失或合成失败。
"""

from unittest.mock import Mock

import json
import pytest

from aiohttp import web
from io import BytesIO
import wave

from maimai_tts_contract.domain.adaptive_tone import BreezeEvent, ToneDecision, apply_breeze_events, parse_adaptive_tone
from maimai_tts_contract.domain.breeze_control import event_tags
from maimai_tts_contract.domain.intonation import parse_tone_tag
from maimai_tts_contract.domain.models import Features, Profile, RouteTarget, StudioConfig, TTSError
from maimai_tts_contract.domain.speech_text import spoken_text_of, strip_stage_directions
from maimai_tts_contract.services.intonation import clean_display_text


TONES = ["neutral", "happy"]
TAGS = event_tags("zh")
TAG = TAGS["zh"][0]
RAW = "（凑近耳边，声音压低）今天不想说话。"


def wav_bytes():
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 24000, 0, "NONE", ""))
        stream.writeframes(b"\0" * 4800)
    return output.getvalue()


def decision_on(text: str, anchor: str) -> ToneDecision:
    """模拟语调 LLM：基于 text 生成事件，并由插件按 text 校验锚点。"""
    response = json.dumps({"events": [{"before": anchor, "tag": TAG}]}, ensure_ascii=False)
    return parse_adaptive_tone(response, TONES, breeze=True, free_direction=False, text=text, event_options=TAGS)


# --- 1. 清洗逻辑本身：纯括号、多个括号、中英文括号、方括号标签、嵌套 ---

@pytest.mark.parametrize("raw, expected", [
    ("（动作）台词", "台词"),
    ("（动作一）（动作二）台词", "台词"),
    ("台词（补充）", "台词"),
    ("（嵌套（内层）外层）台词", "台词"),
    ("[mmtts:lively]（动作）台词", "[mmtts:lively]台词"),
    ("这是 (ID) 编号", "这是 (ID) 编号"),
    ("（只有动作）", "（只有动作）"),
    ("（凑近耳边，声音压低）今天不想说话。", "今天不想说话。"),
])
def test_strip_stage_directions_cases(raw, expected):
    assert strip_stage_directions(raw) == expected


def test_spoken_text_of_parses_tone_tag_first():
    raw = "[mmtts:happy]（微笑）你好呀"
    assert spoken_text_of(raw, strip=True) == "你好呀"
    assert spoken_text_of(raw, strip=False) == "（微笑）你好呀"


# --- 2. 开关关闭时保持原有行为，显示文字始终不变 ---

def test_option_off_keeps_original_behavior():
    raws = ["（微笑）你好呀", "[mmtts:happy]（微笑）你好呀", "这是 (ID) 编号", "（只有动作）"]
    for raw in raws:
        assert spoken_text_of(raw, strip=False) == parse_tone_tag(raw.strip()).text
        assert spoken_text_of(raw, strip=True) == strip_stage_directions(parse_tone_tag(raw.strip()).text)
        assert clean_display_text(raw) == parse_tone_tag(raw).text
    assert Features().strip_stage_directions is False


def test_display_text_never_changes_when_stripping():
    assert clean_display_text(RAW) == RAW
    assert "凑近耳边" not in spoken_text_of(RAW, strip=True)


# --- 3. 锚点一致性：修复后的链路 vs 修复前的错位 ---

def test_anchors_from_spoken_text_insert_cleanly():
    """author review 的核心：生成、校验、插入都用同一份朗读文本。"""
    spoken = spoken_text_of(RAW, strip=True)
    decision = decision_on(spoken, "今天不想说话")
    assert len(decision.events) == 1
    result = apply_breeze_events(spoken, decision.events)
    assert result == TAG + spoken


def test_old_mismatch_would_silently_drop_event():
    """回归守卫：锚点取自原文，插入进清洗后文本 -> 事件被跳过。"""
    raw = "（微笑）你好呀"
    spoken = spoken_text_of(raw, strip=True)
    decision = decision_on(raw, "（微笑）你好呀")
    assert apply_breeze_events(spoken, decision.events) == spoken
    assert TAG not in apply_breeze_events(spoken, decision.events)


def test_old_mismatch_would_fail_on_duplicate_anchor():
    """回归守卫：清洗把原来唯一的锚点粘成重复锚点 -> 合成报歧义错误。"""
    raw = "你好（微笑）呀，你好呀"
    spoken = spoken_text_of(raw, strip=True)
    assert spoken == "你好呀，你好呀"
    decision = decision_on(raw, "你好呀")
    with pytest.raises(TTSError, match="不唯一"):
        apply_breeze_events(spoken, decision.events)


def test_new_chain_handles_duplicate_prone_text():
    """修复后 LLM 看到的是清洗后文本，会选一个仍唯一的锚点，不再报错。"""
    raw = "你好（微笑）呀，你好呀"
    spoken = spoken_text_of(raw, strip=True)
    decision = decision_on(spoken, "呀，你好呀")
    result = apply_breeze_events(spoken, decision.events)
    assert result == "你好" + TAG + "呀，你好呀"


# --- 4. 端到端：送进 TTS 的文本已清洗，显示文字仍是原文 ---

@pytest.mark.asyncio
async def test_synthesize_uses_one_spoken_text_and_preserves_display(serve, service):
    calls = []

    async def handler(request):
        raw = await request.read()
        text = raw.decode("utf-8", "replace")
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False)
        except ValueError:
            pass
        calls.append(text)
        return web.Response(body=wav_bytes(), content_type="audio/wav")

    selected = Profile(
        id="breeze", name="breeze", kind="breeze", base_url=await serve(handler),
        api_key="test-only-not-a-real-key", model="breeze-tts-2", voice="voice_test",
        response_format="wav", options={"breeze_api_mode": "cloud", "breeze_mode": "plain"},
        tone={"mode": "dynamic", "allowed": TONES},
    )
    config = StudioConfig(
        feature=Features(strip_stage_directions=True),
        profiles=[selected],
        primary=RouteTarget(provider_id="breeze", model_id=selected.model),
    )
    spoken = spoken_text_of(RAW, strip=True)

    async def adapter(profile):
        return decision_on(spoken, "今天不想说话")

    audio = await service.synthesize(RAW, config=config, tone_adapter=adapter)
    assert audio.display_text == RAW
    assert calls and "凑近耳边" not in calls[0]
    assert "今天不想说话" in calls[0]
    assert TAG in calls[0]
