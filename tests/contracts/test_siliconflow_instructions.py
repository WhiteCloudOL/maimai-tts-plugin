"""CosyVoice2 的建议、实际指令、继承与缓存同时按最终请求核对。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from maimai_tts_contract.api.parameters import public_profile
from maimai_tts_contract.domain.intonation import tone_capability
from maimai_tts_contract.domain.adaptive_tone import direction_limit
from maimai_tts_contract.domain.models import Features, Profile, RouteTarget, StudioConfig, Voice
from maimai_tts_contract.domain.siliconflow import short_instruction_guidance
from maimai_tts_contract.providers.siliconflow import SiliconFlowProvider
from maimai_tts_contract.services import adaptive_tone
from maimai_tts_contract.services.adaptive_tone import ProbabilityToneAdapter
from maimai_tts_contract.services.intonation import apply_tone
from maimai_tts_contract.services.speech_input import speech_tone_prompt, speech_tool_prompt
from test_prompt_pipeline import CASES, TEXT, PromptBot, profile, tts_endpoint


@pytest.mark.parametrize("language", ["zh", "en", "ja"])
@pytest.mark.asyncio
async def test_cosvoice2_guidance_reaches_three_prompt_paths_with_custom_templates(language):
    selected = profile("siliconflow", "http://127.0.0.1:1")
    features = Features(tone_prompt_language=language, prompt_overrides={"siliconflow": {
        "tool": {language: "My tool template. {{dynamic_tone}}"},
        "dynamic": {language: "My tone template."},
        "adaptive": {language: "My adaptive template."},
    }})
    ctx = SimpleNamespace(call_capability=AsyncMock(return_value={"success": True, "response": '{"tone":"happy"}'}))
    await ProbabilityToneAdapter(ctx, features, TEXT, Mock())(selected)
    prompts = [speech_tone_prompt(selected, features, automatic=True), speech_tool_prompt(selected, features),
               ctx.call_capability.await_args.kwargs["prompt"][0]["content"]]
    for prompt in prompts:
        assert "CosyVoice2" in prompt and "20" in prompt
        assert "{{" not in prompt
        assert "开心愉快。" in prompt
    assert "My tone template." in prompts[0]
    assert "My tool template." in prompts[1]
    assert "My adaptive template." in prompts[2]
    assert "JSON" in prompts[2] and '{"tone": "neutral"}' in prompts[2]


@pytest.mark.parametrize("kind", [key for key in CASES if key != "siliconflow"])
def test_short_instruction_advice_is_specific_to_siliconflow_cosvoice2(kind):
    selected = profile(kind, "http://127.0.0.1:1")
    assert "CosyVoice2" not in speech_tone_prompt(selected, Features(), automatic=True)
    assert "CosyVoice2" not in speech_tool_prompt(selected, Features())
    cap = tone_capability(selected.kind, selected.model, selected.voice, selected.options.resource_id)
    assert cap is None or "recommended_instruction_length" not in cap
    if kind not in {"minimax", "bailian_minimax", "gpt_sovits"}:
        effective, _ = apply_tone(selected, "happy")
        assert effective.instructions == "以开心愉快的语气说话。"


def test_moss_model_in_same_platform_keeps_its_own_control_rules():
    data = profile("siliconflow", "http://127.0.0.1:1").model_dump()
    data.update(model="fnlp/MOSS-TTSD-v0.5", voice="fnlp/MOSS-TTSD-v0.5:anna", models=[], tone={"mode": "off"})
    selected = Profile.model_validate(data)
    assert speech_tone_prompt(selected, Features(), automatic=True) == ""
    assert "CosyVoice2" not in speech_tool_prompt(selected, Features())
    assert short_instruction_guidance(selected.kind, selected.model, "zh") == ""


@pytest.mark.parametrize("path", ["fixed", "dynamic", "tool", "adaptive"])
@pytest.mark.asyncio
async def test_short_default_direction_reaches_upstream_and_retained_text_is_clean(serve, service, path):
    calls = []
    selected = profile("siliconflow", await serve(tts_endpoint("siliconflow", calls)))
    selected.instructions = "声音清甜。"
    selected.tone.allowed = ["neutral", "happy", "lively"]
    selected.tone.emotion = "lively"
    if path == "fixed":
        selected.tone.mode = "fixed"
    service.store._config = StudioConfig(
        profiles=[selected], primary=RouteTarget(provider_id=selected.id, model_id=selected.model),
        feature=Features(delivery_format="source", tool_keep_text=True),
    )
    tagged = "[mmtts:lively]" + TEXT
    if path == "tool":
        bot = PromptBot(service)
        result = await bot.handle_tts_tool(tagged, stream_id="isolated-contract")
        assert result["success"] is True
        assert bot.ctx.send.hybrid.await_args_list[0].args[0] == [{"type": "text", "data": TEXT}]
    else:
        ctx = SimpleNamespace(call_capability=AsyncMock(return_value={"success": True, "response": '{"tone":"lively"}'}))
        adapter = ProbabilityToneAdapter(ctx, Features(), TEXT, Mock()) if path == "adaptive" else None
        audio = await service.synthesize(tagged if path == "dynamic" else TEXT, tone_adapter=adapter)
        assert audio.display_text == TEXT
    assert len(calls) == 1
    assert calls[0]["input"] == "声音清甜。\n活泼轻快。<|endofprompt|>" + TEXT
    assert "instructions" not in calls[0] and "references" not in calls[0]
    assert calls[0]["voice"] == selected.voice


@pytest.mark.asyncio
async def test_short_rules_partition_synthesis_cache_and_keep_explicit_directions(serve, service):
    calls = []
    selected = profile("siliconflow", await serve(tts_endpoint("siliconflow", calls)))
    selected.instructions = "声音清甜。"
    config = StudioConfig(profiles=[selected], primary=RouteTarget(provider_id=selected.id, model_id=selected.model))
    short = await service.synthesize("[mmtts:happy]" + TEXT, config=config)
    repeat = await service.synthesize("[mmtts:happy]" + TEXT, config=config)
    assert short.cache_status == "miss" and repeat.cache_status == "hit"
    selected.tone.directions = {"happy": "以开心愉快的语气说话。"}
    custom_config = StudioConfig(profiles=[selected], primary=config.primary)
    custom = await service.synthesize("[mmtts:happy]" + TEXT, config=custom_config)
    assert custom.cache_status == "miss" and len(calls) == 2
    assert calls[0]["input"] == "声音清甜。\n开心愉快。<|endofprompt|>" + TEXT
    assert calls[1]["input"] == "声音清甜。\n以开心愉快的语气说话。<|endofprompt|>" + TEXT
    selected.tone.labels = {"happy": "喜悦"}
    same_request = await service.synthesize("[mmtts:happy]" + TEXT, config=StudioConfig(profiles=[selected], primary=config.primary))
    assert same_request.cache_status == "hit" and len(calls) == 2


def test_long_saved_style_and_voice_overrides_are_preserved_without_truncation():
    selected = profile("siliconflow", "http://127.0.0.1:1")
    selected.instructions = "一位二十岁的中国女孩，声音清甜，语速自然，像在跟朋友聊天。"
    selected.tone.directions = {"happy": "以开心愉快的语气说话。"}
    saved = selected.model_dump()
    effective, _ = apply_tone(selected.for_model(selected.model).with_selected_voice(), "happy")
    payload = SiliconFlowProvider(Mock()).payload(effective, TEXT)
    assert payload["input"] == selected.instructions + "\n以开心愉快的语气说话。<|endofprompt|>" + TEXT
    assert selected.model_dump() == saved
    selected.voices = [Voice(id=selected.voice, name="测试音色", model=selected.model, instructions="轻柔自然。",
                             tone={"mode": "fixed", "emotion": "gentle", "directions": {"gentle": "温暖地轻声安慰。"}})]
    effective, _ = apply_tone(selected.for_model(selected.model).with_selected_voice(), None)
    assert effective.instructions == "轻柔自然。\n温暖地轻声安慰。"
    assert "温暖地轻声安慰。" in public_profile(selected.for_model(selected.model).with_selected_voice())["tone_meanings"]


@pytest.mark.asyncio
async def test_adaptive_prefix_is_stable_and_policy_change_invalidates_same_turn_decision(monkeypatch):
    selected = profile("siliconflow", "http://127.0.0.1:1")
    ctx = SimpleNamespace(call_capability=AsyncMock(return_value={"success": True, "response": '{"tone":"happy"}'}))
    adapter = ProbabilityToneAdapter(ctx, Features(), TEXT, Mock())
    assert (await adapter(selected)).tone == "happy"
    await adapter(selected)
    assert ctx.call_capability.await_count == 1
    await ProbabilityToneAdapter(ctx, Features(), "另一段正文。", Mock())(selected)
    messages = [call.kwargs["prompt"] for call in ctx.call_capability.await_args_list]
    assert messages[0][0] == messages[1][0] and messages[0][1] != messages[1][1]
    monkeypatch.setattr(adaptive_tone, "short_instruction_guidance", lambda *args, **kwargs: "Updated policy: prefer the neutral ID.")
    ctx.call_capability.return_value = {"success": True, "response": '{"tone":"neutral"}'}
    assert (await adapter(selected)).tone == "neutral"
    assert ctx.call_capability.await_count == 3


@pytest.mark.asyncio
async def test_cosvoice2_retry_preserves_original_system_and_rejected_output():
    selected = profile("siliconflow", "http://127.0.0.1:1")
    ctx = SimpleNamespace(call_capability=AsyncMock(side_effect=[
        {"success": True, "response": '{"tone":"开心"}'},
        {"success": True, "response": '{"direction":"轻声安慰。"}'},
    ]))
    result = await ProbabilityToneAdapter(ctx, Features(), TEXT, Mock())(selected)
    assert result.direction == "轻声安慰。"
    messages = [call.kwargs["prompt"] for call in ctx.call_capability.await_args_list]
    assert messages[0][:2] == messages[1][:2]
    assert messages[1][-2] == {"role": "assistant", "content": '{"tone":"开心"}'}
    assert "允许列表" in messages[1][-1]["content"]


@pytest.mark.asyncio
async def test_style_budget_changes_same_turn_adaptive_decision_but_keeps_advisory_length():
    selected = profile("siliconflow", "http://127.0.0.1:1")
    selected.instructions = "声音清甜。"
    ctx = SimpleNamespace(call_capability=AsyncMock(return_value={"success": True, "response": '{}'}))
    adapter = ProbabilityToneAdapter(ctx, Features(), TEXT, Mock())
    await adapter(selected)
    selected.instructions = "一位二十岁的中国女孩，声音清甜，语速自然，像在跟朋友聊天。"
    await adapter(selected)
    assert ctx.call_capability.await_count == 2
    first, second = [call.kwargs["prompt"][0]["content"] for call in ctx.call_capability.await_args_list]
    assert "当前风格占 5 字符，自由描述建议余量 14 字符" in first
    assert "自由描述建议余量 0 字符" in second
    assert direction_limit(selected) == 200


def test_reference_audio_keeps_voice_exclusive_and_uses_current_actual_style():
    selected = profile("siliconflow", "http://127.0.0.1:1")
    selected.options.reference_enabled = True
    selected.options.reference_source = "url"
    selected.options.reference_url = "https://example.org/reference.wav"
    selected.options.reference_text = "参考录音的文字。"
    effective, _ = apply_tone(selected, "happy")
    payload = SiliconFlowProvider(Mock()).payload(effective, TEXT)
    assert "voice" not in payload
    assert payload["references"] == [{"audio": selected.options.reference_url, "text": selected.options.reference_text}]
    assert payload["input"] == "开心愉快。<|endofprompt|>" + TEXT
