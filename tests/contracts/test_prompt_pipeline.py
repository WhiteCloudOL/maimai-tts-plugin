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
from maimai_tts_contract.components.probability import ProbabilityMixin
from maimai_tts_contract.domain.adaptive_tone import adaptive_prompt, parse_adaptive_tone
from maimai_tts_contract.domain.models import Features, Profile, RouteTarget, StudioConfig, TTSError
from maimai_tts_contract.domain.prompts import PLATFORM_DEFAULTS, validate_prompt
from maimai_tts_contract.plugin import TTSPlugin


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


def test_sdk_registration_exports_speech_tool_as_visible():
    components = json.loads(json.dumps(TTSPlugin().get_components()))
    tool = next(component for component in components if component["name"] == "mmtts_speak")
    assert tool["metadata"]["visibility"] == "visible"


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
    assert sent[1].args[0][0]["data"] == TEXT
    assert sent[1].args[0][0]["binary_data_base64"]
    if kind in {"minimax", "bailian_minimax"}:
        payload = calls[0]["input"] if kind == "bailian_minimax" else calls[0]
        assert payload["voice_setting"]["emotion"] == "happy"
    elif kind == "siliconflow":
        assert calls[0]["input"] == "开心愉快。<|endofprompt|>" + TEXT
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


class ReplyerPromptBot(MessagingMixin, IntonationMixin, ProbabilityMixin):
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.config = SimpleNamespace(plugin=SimpleNamespace(enabled=True))
        self.ctx = SimpleNamespace(
            plugin_id="WhiteCloudOL.maimai-tts-plugin",
            send=SimpleNamespace(hybrid=AsyncMock(return_value={"sent": True})),
        )
        self._probability_reply_states = {}
        self._probability_reply_available_sessions = {"test-stream"}
        self._probability_prepare_scopes = {}
        self._voice_reply_states = {}
        self._probability_voice_states = {}
        self._probability_attachment_states = {}


@pytest.mark.asyncio
async def test_tool_only_skips_replyer_dynamic_tone_and_routes_planner_to_voice_reply_extension(service):
    selected = profile("volcengine", "http://127.0.0.1:1")
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id="volcengine", model_id=selected.model),
        feature=Features(tool_enabled=True, probability_enabled=False),
    )
    bot = ReplyerPromptBot(service)
    voice_ext = bot._voice_reply_extension_name()
    prob_ext = bot._probability_extension_name()

    replyer_result = await bot.prepare_dynamic_tone_prompt(
        extra_prompt="基础要求", session_id="test-stream", reply_tool_args={"chat_anlyze": "普通文字回复"},
    )
    assert replyer_result == {"action": "continue"}

    planner_result = await bot.prepare_planner_tone_prompt(
        tool_definitions=[
            {
                "type": "function",
                "function": {
                    "name": "reply",
                    "description": "reply",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "plugin_options": {
                                "type": "object",
                                "properties": {
                                    voice_ext: {
                                        "type": "object",
                                        "properties": {
                                            "voice": {"type": "string"},
                                            "speed": {"type": "number"},
                                        },
                                    },
                                    prob_ext: {"type": "object", "properties": {}},
                                },
                            },
                        },
                    },
                },
            },
            {"type": "function", "function": {"name": "mmtts_speak", "description": "", "parameters": {}}},
            {"type": "function", "function": {"name": "mmtts_tone_options", "description": "", "parameters": {}}},
        ],
    )
    defs = planner_result["modified_kwargs"]["tool_definitions"]
    names = [item["function"]["name"] for item in defs]
    assert names == ["reply"]
    ext_props = defs[0]["function"]["parameters"]["properties"]["plugin_options"]["properties"]
    assert voice_ext in ext_props and prob_ext not in ext_props
    assert "speed" in ext_props[voice_ext]["properties"]


@pytest.mark.asyncio
async def test_explicit_voice_reply_splits_segments_and_preserves_tone_per_segment(serve, service):
    calls = []
    selected = profile("volcengine", await serve(tts_endpoint("volcengine", calls)))
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id="volcengine", model_id=selected.model),
        feature=Features(delivery_format="source", tool_enabled=True, tool_keep_text=False, probability_enabled=False),
    )
    bot = ReplyerPromptBot(service)
    voice_ext = bot._voice_reply_extension_name()

    # 1. prepare 仅在显式 voice_reply 轮次返回动态语调提示
    prep = await bot.handle_voice_reply("prepare", "vr-1", session_id="test-stream", parameters={"speed": 0.95})
    assert "【麦麦TTS动态语调】" in prep["extra_prompt"]

    # 2. before_post_process 剥离头部标签，允许宿主正常分段并关闭错别字
    reply_args = {"_reply_id": "vr-1", "plugin_options": {voice_ext: {"speed": 0.95}}}
    post = await bot.prepare_probability_tts_reply(
        response="[mmtts:happy]第一句台词。\n第二句台词。",
        session_id="test-stream",
        reply_tool_args=reply_args,
    )
    assert post["modified_kwargs"]["response"] == "第一句台词。\n第二句台词。"
    assert post["modified_kwargs"]["skip_post_process"] is False
    assert post["modified_kwargs"]["enable_splitter"] is True
    assert post["modified_kwargs"]["enable_chinese_typo"] is False

    # 3. 宿主拆分成两段后，before_send 逐段合成语音且每段均携带 happy 语调
    split_messages = [
        {"segments": [{"type": "text", "data": "第一句台词。"}], "quote_previous": False},
        {"segments": [{"type": "text", "data": "第二句台词。"}], "quote_previous": False},
    ]
    out = await bot.handle_voice_reply(
        "before_send", "vr-1", session_id="test-stream", parameters={"speed": 0.95}, messages=split_messages,
    )
    assert len(calls) == 2
    assert all("以开心愉快" in json.dumps(c, ensure_ascii=False) for c in calls)
    assert all(any(seg["type"] == "voice" for seg in msg["segments"]) for msg in out["messages"])
    assert not bot._voice_reply_states

    # 4. 已选 voice_reply 时，概率扩展不会重复挂载
    service.store._config.feature.probability_enabled = True
    att = await bot.attach_probability_reply_extension(
        session_id="test-stream",
        output_items=[{"item_type": "FunctionCallItem", "tool_call": {"func_name": "reply", "args": {"plugin_options": {voice_ext: {}}}}}],
    )
    assert att == {"action": "continue"}


@pytest.mark.asyncio
async def test_probability_replyer_prompt_respects_extension_attachments_and_scope(service):
    selected = profile("volcengine", "http://127.0.0.1:1")
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id="volcengine", model_id=selected.model),
        feature=Features(
            tool_enabled=True,
            probability_enabled=True,
            probability=0.2,
            probability_group_enabled=False,
            probability_private_enabled=True,
            probability_tone_enabled=False,
        ),
    )
    bot = ReplyerPromptBot(service)
    ext = bot._probability_extension_name()

    # 未挂载扩展 -> 跳过
    assert await bot.prepare_dynamic_tone_prompt(session_id="test-stream", reply_tool_args={}) == {"action": "continue"}

    # 带图片附件 / 旧原生语音 -> 跳过且不挂载扩展
    for extra in ({"attach_pic": "img.png"}, {"attach_voice": "voice_only"}):
        args = {"_reply_id": "r-skip", "plugin_options": {ext: {}}, **extra}
        assert await bot.prepare_dynamic_tone_prompt(session_id="test-stream", reply_tool_args=args) == {"action": "continue"}
        attach_res = await bot.attach_probability_reply_extension(
            session_id="test-stream",
            output_items=[{"item_type": "FunctionCallItem", "tool_call": {"func_name": "reply", "args": dict(extra)}}],
        )
        assert attach_res == {"action": "continue"}

    # 群聊范围未开启 -> prepare 登记后跳过
    await bot.handle_probability_tts("prepare", "r-group", session_id="test-stream", chat={"is_group_chat": True})
    group_args = {"_reply_id": "r-group", "plugin_options": {ext: {}}}
    assert await bot.prepare_dynamic_tone_prompt(session_id="test-stream", reply_tool_args=group_args) == {"action": "continue"}

    # 私聊范围开启 -> 正常注入
    await bot.handle_probability_tts("prepare", "r-private", session_id="test-stream", chat={"is_group_chat": False})
    private_args = {"_reply_id": "r-private", "plugin_options": {ext: {}}}
    injected = await bot.prepare_dynamic_tone_prompt(
        extra_prompt="原有提示", session_id="test-stream", reply_tool_args=private_args,
    )
    assert "【麦麦TTS动态语调】" in injected["modified_kwargs"]["extra_prompt"]
    assert injected["modified_kwargs"]["extra_prompt"].startswith("原有提示\n\n")

    # 概率为 0 -> 跳过注入且不挂载扩展
    service.store._config.feature.probability = 0.0
    assert await bot.prepare_dynamic_tone_prompt(session_id="test-stream", reply_tool_args=private_args) == {"action": "continue"}
    attach_zero = await bot.attach_probability_reply_extension(
        session_id="test-stream",
        output_items=[{"item_type": "FunctionCallItem", "tool_call": {"func_name": "reply", "args": {}}}],
    )
    assert attach_zero == {"action": "continue"}


@pytest.mark.parametrize("kind,expect_injected", [("volcengine", False), ("breeze", True)])
@pytest.mark.asyncio
async def test_probability_replyer_prompt_respects_adaptive_mode_for_breeze_and_non_breeze(service, kind, expect_injected):
    selected = profile(kind, "http://127.0.0.1:1")
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id=kind, model_id=selected.model),
        feature=Features(
            tool_enabled=True,
            probability_enabled=True,
            probability=0.5,
            probability_group_enabled=False,
            probability_private_enabled=True,
            probability_tone_enabled=True,
        ),
    )
    bot = ReplyerPromptBot(service)
    ext = bot._probability_extension_name()
    await bot.handle_probability_tts("prepare", "r-adapt", session_id="test-stream", chat={"is_group_chat": False})
    res = await bot.prepare_dynamic_tone_prompt(
        session_id="test-stream", reply_tool_args={"_reply_id": "r-adapt", "plugin_options": {ext: {}}},
    )
    assert ("modified_kwargs" in res) is expect_injected
