"""提示协议、缓存前缀和从工具/自适应到真实 HTTP TTS 请求的回归。"""

from aiohttp import web
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import base64
import json
import wave

import pytest

from maimai_tts_contract.components.adaptive_tone import ProbabilityToneAdapter
from maimai_tts_contract.components.intonation import IntonationMixin
from maimai_tts_contract.components.messaging import MessagingMixin
from maimai_tts_contract.domain.adaptive_tone import adaptive_prompt, parse_adaptive_tone
from maimai_tts_contract.domain.models import Features, Profile, RouteTarget, StudioConfig, TTSError
from maimai_tts_contract.domain.prompts import PLATFORM_DEFAULTS, validate_prompt


CASES = {
    "openai": ("gpt-4o-mini-tts", "alloy", {}),
    "compatible": ("gpt-4o-mini-tts", "alloy", {}),
    "qwen": ("qwen3-tts-instruct-flash", "Cherry", {}),
    "cosyvoice": ("cosyvoice-v3.5-flash", "cosyvoice-v3.5-flash-test", {"alibaba_transport": "http"}),
    "bailian_minimax": ("MiniMax/speech-02-hd", "female-shaonv", {}),
    "minimax": ("speech-2.6-hd", "female-shaonv", {}),
    "siliconflow": ("FunAudioLLM/CosyVoice2-0.5B", "FunAudioLLM/CosyVoice2-0.5B:claire", {}),
    "volcengine": ("seed-tts-2.0-expressive", "S_test", {"resource_id": "seed-icl-2.0"}),
    "mimo": ("mimo-v2.5-tts", "mimo_default", {}),
    "gpt_sovits": ("gpt-sovits", "", {"reference_source": "server", "reference_path": "sample.wav"}),
    "breeze": ("breeze-tts-2", "voice_test", {"breeze_api_mode": "cloud", "breeze_mode": "plain"}),
}
TEXT = "你终于来了。我们一起面对。"


def profile(kind, base_url):
    model, voice, options = CASES[kind]
    return Profile(
        id=kind, name=kind, kind=kind, base_url=base_url, api_key="test-only-not-a-real-key",
        model=model, voice=voice, response_format="wav", options=options,
        tone={"mode": "off" if kind == "gpt_sovits" else "dynamic", "allowed": ["neutral", "happy"]},
    )


def wav():
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 24000, 0, "NONE", ""))
        stream.writeframes(b"\0" * 4800)
    return output.getvalue()


def tts_endpoint(kind, calls):
    async def handler(request):
        body = await request.json()
        calls.append(body)
        audio = wav()
        encoded = base64.b64encode(audio).decode("ascii")
        if kind in {"openai", "compatible", "breeze", "siliconflow", "gpt_sovits"}:
            return web.Response(body=audio, content_type="audio/wav")
        if kind == "volcengine":
            return web.Response(text=json.dumps({"code": 0, "data": encoded}) + "\n"
                                + json.dumps({"code": 20000000}) + "\n", content_type="application/json")
        return web.json_response({
            "request_id": "test", "id": "test", "base_resp": {"status_code": 0},
            "data": {"status": 2, "audio": audio.hex()},
            "output": {"finish_reason": "stop", "audio": {"data": encoded},
                       "base_resp": {"status_code": 0}, "data": {"status": 2, "audio": audio.hex()}},
            "choices": [{"message": {"audio": {"data": encoded}}, "finish_reason": "stop"}],
        })
    return handler


class PromptBot(MessagingMixin, IntonationMixin):
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.config = SimpleNamespace(plugin=SimpleNamespace(enabled=True))
        self.ctx = SimpleNamespace(send=SimpleNamespace(hybrid=AsyncMock(return_value={"sent": True})))


@pytest.mark.parametrize("kind", CASES)
@pytest.mark.asyncio
async def test_tool_labels_reach_platform_request_and_not_retained_text(serve, service, kind):
    calls = []
    selected = profile(kind, await serve(tts_endpoint(kind, calls)))
    service.store._config = StudioConfig(
        profiles=[selected], primary=RouteTarget(provider_id=kind, model_id=selected.model),
        feature=Features(delivery_format="source", tool_keep_text=True),
    )
    bot = PromptBot(service)
    text = TEXT if kind == "gpt_sovits" else "[mmtts:happy]" + TEXT
    result = await bot.handle_tts_tool(text, stream_id="test-local-only")
    assert result["success"] is True
    assert calls and "mmtts:" not in json.dumps(calls, ensure_ascii=False)
    sent = bot.ctx.send.hybrid.await_args_list
    assert sent[0].args[0] == [{"type": "text", "data": TEXT}]
    assert sent[1].args[0][0]["binary_data_base64"]
    if kind in {"minimax", "bailian_minimax"}:
        payload = calls[0]["input"] if kind == "bailian_minimax" else calls[0]
        assert payload["voice_setting"]["emotion"] == "happy"
    elif kind != "gpt_sovits":
        assert "以开心愉快" in json.dumps(calls, ensure_ascii=False)


@pytest.mark.parametrize("kind", [kind for kind in CASES if kind != "gpt_sovits"])
@pytest.mark.asyncio
async def test_adaptive_controls_reach_real_http_request(serve, service, kind):
    calls = []
    selected = profile(kind, await serve(tts_endpoint(kind, calls)))
    service.store._config = StudioConfig(profiles=[selected], primary=RouteTarget(provider_id=kind, model_id=selected.model))
    response = {"tone": "happy"} if kind in {"minimax", "bailian_minimax"} else {"direction": "轻柔"}
    ctx = SimpleNamespace(call_capability=AsyncMock(return_value={"success": True, "response": json.dumps(response)}))
    adapter = ProbabilityToneAdapter(ctx, Features(), TEXT, Mock())
    audio = await service.synthesize(TEXT, tone_adapter=adapter)
    assert audio.display_text == TEXT and calls
    if "direction" in response:
        assert "轻柔" in json.dumps(calls, ensure_ascii=False)
    else:
        payload = calls[0]["input"] if kind == "bailian_minimax" else calls[0]
        assert payload["voice_setting"]["emotion"] == "happy"


@pytest.mark.asyncio
async def test_retry_and_new_dialogue_keep_system_prompt_identical():
    selected = profile("minimax", "http://127.0.0.1:1")
    ctx = SimpleNamespace(call_capability=AsyncMock(side_effect=[
        {"success": True, "response": '{"direction":"轻柔"}'},
        {"success": True, "response": '{"tone":"happy"}'},
        {"success": True, "response": '{}'},
    ]))
    assert (await ProbabilityToneAdapter(ctx, Features(), TEXT, Mock())(selected)).tone == "happy"
    await ProbabilityToneAdapter(ctx, Features(), "另一段台词。", Mock())(selected)
    messages = [call.kwargs["prompt"] for call in ctx.call_capability.await_args_list]
    assert messages[0][0] == messages[1][0] == messages[2][0]
    assert messages[0][1] == messages[1][1]
    assert "移除 direction" in messages[1][-1]["content"]
    assert messages[1][-1]["role"] == "user"


@pytest.mark.parametrize("language", ["zh", "en", "ja"])
@pytest.mark.parametrize("breeze,free", [(False, False), (False, True), (True, True)])
def test_json_protocol_is_explicit_and_examples_have_valid_types(language, breeze, free):
    prompt = adaptive_prompt(language, ["neutral", "happy"], breeze=breeze, free_direction=free,
                             event_options={"zh": ["[轻笑]"]} if breeze else {})
    assert "JSON" in prompt and "Markdown" in prompt and "{}" in prompt
    assert "表内标签\"" not in prompt and "unique exact phrase\"" not in prompt
    assert "{{" not in prompt
    if breeze:
        line = next(line for line in prompt.splitlines() if "; output=" in line)
        example_input, example_output = line.split("; output=", 1)
        source = json.loads(example_input.split("input=", 1)[1])
        decision = parse_adaptive_tone(example_output, ["neutral", "happy"], breeze=True, text=source,
                                       event_options={"zh": ["[轻笑]"]})
        assert decision.direction and len(decision.events) == 1
    for kind, translations in PLATFORM_DEFAULTS.items():
        for name, values in translations.items():
            validate_prompt(kind, name, values[language])


@pytest.mark.asyncio
async def test_breeze_existing_controls_keep_cached_protocol_but_disable_extra_events():
    selected = profile("breeze", "http://127.0.0.1:1")
    ctx = SimpleNamespace(call_capability=AsyncMock(return_value={"success": True, "response": '{}'}))
    for text in (TEXT, "[轻笑]" + TEXT):
        await ProbabilityToneAdapter(ctx, Features(), text, Mock())(selected)
    messages = [call.kwargs["prompt"] for call in ctx.call_capability.await_args_list]
    assert messages[0][0] == messages[1][0]
    assert len(messages[1]) == len(messages[0]) + 1
    assert "events 必须省略或为空数组" in messages[1][-1]["content"]


@pytest.mark.parametrize("response", ['{"tone":"happy","tone":"neutral"}',
                                     '{"events":[{"before":"你终于来了","tag":"[轻笑]","tag":"[轻笑]"}]}'])
def test_duplicate_json_fields_are_rejected(response):
    with pytest.raises(TTSError, match="重复字段"):
        parse_adaptive_tone(response, ["happy", "neutral"], breeze=True, text=TEXT, event_options={"zh": ["[轻笑]"]})
