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
        self.config = SimpleNamespace(plugin=SimpleNamespace(enabled=True), runtime=service.settings)
        self.ctx = SimpleNamespace(
            plugin_id="WhiteCloudOL.maimai-tts-plugin",
            call_capability=AsyncMock(),
            send=SimpleNamespace(
                hybrid=AsyncMock(return_value={"sent": True, "message_id": "sdk-msg"}),
                text=AsyncMock(return_value=True),
            ),
        )
        self._probability_reply_states = {}
        self._probability_reply_available_sessions = {"test-stream"}
        self._probability_prepare_scopes = {}
        self._voice_reply_states = {}
        self._voice_reply_prompted_ids = {}
        self._probability_voice_states = {}
        self._probability_attachment_states = {}


def _planner_tool_definitions(voice_ext: str, prob_ext: str) -> list:
    return [
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
                                    "description": "将本轮回复合成为语音发送（需在工作台开启“改用回复模型生成语音正文”；正文仍由回复模型按人设生成，默认直接传 {}）",
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
        {
            "type": "function",
            "function": {
                "name": "mmtts_speak",
                "description": "default",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "voice": {"type": "string"},
                        "speed": {"type": "number"},
                    },
                    "required": ["text"],
                },
            },
        },
        {"type": "function", "function": {"name": "mmtts_tone_options", "description": "", "parameters": {}}},
    ]


@pytest.mark.asyncio
async def test_default_mode_preserves_legacy_tools_custom_prompt_and_hides_voice_reply(serve, service):
    calls = []
    selected = profile("volcengine", await serve(tts_endpoint("volcengine", calls)))
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id="volcengine", model_id=selected.model),
        feature=Features(
            tool_enabled=True,
            probability_enabled=False,
            prompt_overrides={"volcengine": {"tool": {"zh": "自定义工具说明\n{{dynamic_tone}}"}}},
        ),
    )
    assert service.store._config.feature.tool_use_replyer is False
    bot = ReplyerPromptBot(service)
    voice_ext = bot._voice_reply_extension_name()
    prob_ext = bot._probability_extension_name()

    planner_result = await bot.prepare_planner_tone_prompt(
        tool_definitions=_planner_tool_definitions(voice_ext, prob_ext),
    )
    defs = planner_result["modified_kwargs"]["tool_definitions"]
    names = [item["function"]["name"] for item in defs]
    assert names == ["reply", "mmtts_speak", "mmtts_tone_options"]
    speak = next(item["function"] for item in defs if item["function"]["name"] == "mmtts_speak")
    assert "自定义工具说明" in speak["description"]
    assert "mmtts_tone_options" in speak["description"]
    assert "plugin_options" not in defs[0]["function"]["parameters"]["properties"]

    # 关闭模式下，即使并行补充器直接选中了 voice_reply，也保持安全透传而不合成。
    assert await bot.handle_voice_reply("prepare", "vr-off", session_id="test-stream") == {}
    msgs = [{"segments": [{"type": "text", "data": TEXT}], "quote_previous": False}]
    assert await bot.handle_voice_reply("before_send", "vr-off", session_id="test-stream", messages=msgs) == {}
    assert not calls


@pytest.mark.asyncio
async def test_replyer_mode_skips_plain_text_tone_and_routes_planner_to_voice_reply_extension(service):
    selected = profile("volcengine", "http://127.0.0.1:1")
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id="volcengine", model_id=selected.model),
        feature=Features(tool_enabled=True, tool_use_replyer=True, probability_enabled=False),
    )
    bot = ReplyerPromptBot(service)
    voice_ext = bot._voice_reply_extension_name()
    prob_ext = bot._probability_extension_name()

    # 普通文字回复零注入；显式 voice_reply 才通过 maisaka.replyer.before_request 注入动态语调提示。
    plain_result = await bot.prepare_dynamic_tone_prompt(
        extra_prompt="基础要求", session_id="test-stream", reply_tool_args={"chat_anlyze": "普通文字回复"},
    )
    assert plain_result == {"action": "continue"}
    voice_result = await bot.prepare_dynamic_tone_prompt(
        extra_prompt="基础要求",
        session_id="test-stream",
        reply_tool_args={"_reply_id": "vr-0", "plugin_options": {voice_ext: {}}},
    )
    assert "本轮回复将合成为语音发送" in voice_result["modified_kwargs"]["extra_prompt"]
    assert "【麦麦TTS动态语调】" in voice_result["modified_kwargs"]["extra_prompt"]
    assert voice_result["modified_kwargs"]["extra_prompt"].startswith("基础要求\n\n")

    for lang, expected_phrase in (
        ("en", "This reply will be synthesized as speech"),
        ("ja", "今回の返信は音声として合成・送信されます"),
    ):
        service.store._config.feature.tone_prompt_language = lang
        lang_result = await bot.prepare_dynamic_tone_prompt(
            session_id="test-stream",
            reply_tool_args={"_reply_id": f"vr-{lang}", "plugin_options": {voice_ext: {}}},
        )
        assert expected_phrase in lang_result["modified_kwargs"]["extra_prompt"]
    service.store._config.feature.tone_prompt_language = "zh"

    planner_result = await bot.prepare_planner_tone_prompt(
        tool_definitions=_planner_tool_definitions(voice_ext, prob_ext),
    )
    defs = planner_result["modified_kwargs"]["tool_definitions"]
    names = [item["function"]["name"] for item in defs]
    assert names == ["reply"]
    ext_props = defs[0]["function"]["parameters"]["properties"]["plugin_options"]["properties"]
    assert voice_ext in ext_props and prob_ext not in ext_props
    assert ext_props[voice_ext]["description"] == "将本轮回复合成为语音发送（正文仍由回复模型按人设生成，默认直接传 {}）"
    assert "speed" in ext_props[voice_ext]["properties"]

    # 开启 Replyer 模式后，即使 Planner 凭对话历史盲调旧工具也会被执行入口拦截并引导改用 reply.voice_reply。
    speak_blocked = await bot.handle_tts_tool(TEXT, stream_id="test-stream")
    assert speak_blocked["success"] is False
    assert "stop_after_execution" not in speak_blocked
    assert voice_ext in speak_blocked["error"]
    bot.ctx.send.hybrid.assert_not_awaited()
    tone_blocked = await bot.handle_tone_options()
    assert tone_blocked["success"] is False
    assert "voice_reply" in tone_blocked["error"]


@pytest.mark.asyncio
async def test_explicit_voice_reply_splitter_switch_and_tone_per_segment(serve, service):
    assert Features().tool_replyer_disable_splitter is True
    calls = []
    selected = profile("volcengine", await serve(tts_endpoint("volcengine", calls)))
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id="volcengine", model_id=selected.model),
        feature=Features(
            delivery_format="source",
            tool_enabled=True,
            tool_use_replyer=True,
            tool_keep_text=False,
            probability_enabled=False,
        ),
    )
    bot = ReplyerPromptBot(service)
    voice_ext = bot._voice_reply_extension_name()

    # 1. prepare 永远返回 {}，确保兼容 MaiBot 1.4.8/1.4.6 并行回复补充器模式（不触发 ValueError）
    prep = await bot.handle_voice_reply("prepare", "vr-1", session_id="test-stream", parameters={"speed": 0.95})
    assert prep == {}

    # 2. 默认开启“语音回复不拆分分段”：仅关闭文字分段与错别字，保留 skip_post_process，整段合成一条语音
    reply_args = {"_reply_id": "vr-1", "plugin_options": {voice_ext: {"speed": 0.95}}}
    post_no_split = await bot.prepare_probability_tts_reply(
        response="[mmtts:happy]第一句台词。\n第二句台词。",
        session_id="test-stream",
        reply_tool_args=reply_args,
        skip_post_process=False,
        enable_splitter=True,
        enable_chinese_typo=True,
    )
    assert post_no_split["modified_kwargs"]["response"] == "第一句台词。\n第二句台词。"
    assert post_no_split["modified_kwargs"]["skip_post_process"] is False
    assert post_no_split["modified_kwargs"]["enable_splitter"] is False
    assert post_no_split["modified_kwargs"]["enable_chinese_typo"] is False
    out_single = await bot.handle_voice_reply(
        "before_send",
        "vr-1",
        session_id="test-stream",
        parameters={"speed": 0.95},
        messages=[{"segments": [{"type": "text", "data": "第一句台词。\n第二句台词。"}], "quote_previous": False}],
    )
    assert len(calls) == 1
    assert "以开心愉快" in json.dumps(calls[0], ensure_ascii=False)
    assert any(seg["type"] == "voice" for seg in out_single["messages"][0]["segments"])

    # 3. 关闭“语音回复不拆分分段”：保留宿主或前置 Hook 的分段配置，拆分为多段时逐段补回同一语调前缀
    calls.clear()
    service.store._config.feature.tool_replyer_disable_splitter = False
    reply_args_split = {"_reply_id": "vr-2", "plugin_options": {voice_ext: {"speed": 0.95}}}
    post_keep_split = await bot.prepare_probability_tts_reply(
        response="[mmtts:happy]第一句台词。\n第二句台词。",
        session_id="test-stream",
        reply_tool_args=reply_args_split,
        skip_post_process=False,
        enable_splitter=True,
        enable_chinese_typo=True,
    )
    assert post_keep_split["modified_kwargs"]["response"] == "第一句台词。\n第二句台词。"
    assert post_keep_split["modified_kwargs"]["skip_post_process"] is False
    assert post_keep_split["modified_kwargs"]["enable_splitter"] is True
    assert post_keep_split["modified_kwargs"]["enable_chinese_typo"] is False

    split_messages = [
        {"segments": [{"type": "text", "data": "第一句台词。"}], "quote_previous": False},
        {"segments": [{"type": "text", "data": "第二句台词。"}], "quote_previous": False},
    ]
    out_split = await bot.handle_voice_reply(
        "before_send", "vr-2", session_id="test-stream", parameters={"speed": 0.95}, messages=split_messages,
    )
    assert len(calls) == 2
    assert all("以开心愉快" in json.dumps(c, ensure_ascii=False) for c in calls)
    assert all(any(seg["type"] == "voice" for seg in msg["segments"]) for msg in out_split["messages"])
    assert not bot._voice_reply_states

    # 4. 已选 voice_reply 时，概率扩展不会重复挂载
    service.store._config.feature.probability_enabled = True
    att = await bot.attach_probability_reply_extension(
        session_id="test-stream",
        output_items=[{"item_type": "FunctionCallItem", "tool_call": {"func_name": "reply", "args": {"plugin_options": {voice_ext: {}}}}}],
    )
    assert att == {"action": "continue"}


@pytest.mark.parametrize("kind", CASES)
@pytest.mark.asyncio
async def test_voice_reply_reaches_all_platforms_and_strips_controls(serve, service, kind):
    calls = []
    selected = profile(kind, await serve(tts_endpoint(kind, calls)))
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id=kind, model_id=selected.model),
        feature=Features(
            delivery_format="source",
            tool_enabled=True,
            tool_use_replyer=True,
            tool_keep_text=False,
            probability_enabled=False,
        ),
    )
    bot = ReplyerPromptBot(service)
    voice_ext = bot._voice_reply_extension_name()
    reply_args = {"_reply_id": f"vr-{kind}", "plugin_options": {voice_ext: {}}}
    raw = TEXT if kind == "gpt_sovits" else "[mmtts:happy]" + TEXT

    assert await bot.handle_voice_reply("prepare", f"vr-{kind}", session_id="test-stream") == {}
    post = await bot.prepare_probability_tts_reply(
        response=raw,
        session_id="test-stream",
        reply_tool_args=reply_args,
        skip_post_process=False,
        enable_splitter=True,
        enable_chinese_typo=True,
    )
    body = post["modified_kwargs"]["response"]
    out = await bot.handle_voice_reply(
        "before_send",
        f"vr-{kind}",
        session_id="test-stream",
        messages=[{"segments": [{"type": "text", "data": body}], "quote_previous": False}],
    )
    assert len(calls) == 1
    assert "mmtts:" not in json.dumps(calls, ensure_ascii=False)
    voice_seg = next(seg for seg in out["messages"][0]["segments"] if seg["type"] == "voice")
    assert voice_seg["data"] == TEXT and voice_seg["binary_data_base64"]


@pytest.mark.parametrize("mode", ["voice_only", "with_text"])
@pytest.mark.parametrize("supplement", [False, True])
@pytest.mark.parametrize("host_prefers_plugin", [False, True])
@pytest.mark.asyncio
async def test_voice_reply_respects_maibot_148_native_voice_precedence(serve, service, mode, supplement, host_prefers_plugin):
    calls = []
    selected = profile("breeze", await serve(tts_endpoint("breeze", calls)))
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id="breeze", model_id=selected.model),
        feature=Features(delivery_format="source", tool_enabled=True, tool_use_replyer=True, probability_enabled=False),
    )
    bot = ReplyerPromptBot(service)
    voice_ext = bot._voice_reply_extension_name()
    args = {"_reply_id": "vr-native", "plugin_options": {voice_ext: {}}}
    if supplement:
        args["reply_supplement"] = {"attach_voice": mode}
    else:
        args["attach_voice"] = mode
    if host_prefers_plugin:
        args["_prefer_reply_extension_voice"] = True

    before_req = await bot.prepare_dynamic_tone_prompt(session_id="test-stream", reply_tool_args=args)
    assert ("modified_kwargs" in before_req) is host_prefers_plugin
    assert await bot.handle_voice_reply("prepare", "vr-native", session_id="test-stream") == {}

    tone = "happy" if host_prefers_plugin else "unknown"
    raw = f"[mmtts:{tone}][轻笑]" + TEXT
    prepared = await bot.prepare_probability_tts_reply(
        response=raw,
        session_id="test-stream",
        reply_tool_args=args,
        skip_post_process=False,
        enable_splitter=True,
        enable_chinese_typo=True,
    )
    response = prepared["modified_kwargs"]["response"]
    assert response == (raw if host_prefers_plugin else TEXT)
    if not host_prefers_plugin:
        assert prepared["modified_kwargs"]["enable_splitter"] is True
        assert prepared["modified_kwargs"]["enable_chinese_typo"] is True
        assert prepared["modified_kwargs"]["skip_post_process"] is False
    messages = [{"segments": [{"type": "text", "data": response}], "quote_previous": False}]
    transformed = await bot.handle_voice_reply("before_send", "vr-native", session_id="test-stream", messages=messages)

    if host_prefers_plugin:
        assert [call["text"] for call in calls] == ["[轻笑]" + TEXT]
        assert [part["type"] for part in transformed["messages"][0]["segments"]] == ["text", "voice"]
    else:
        assert not calls
        assert not any(
            part["type"] == "voice"
            for msg in transformed.get("messages", messages)
            for part in msg["segments"]
        )
    bot.ctx.call_capability.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_reply_decouples_adaptive_tone_and_falls_back_in_parallel_mode(serve, service):
    calls = []
    selected = profile("volcengine", await serve(tts_endpoint("volcengine", calls)))
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id="volcengine", model_id=selected.model),
        feature=Features(
            delivery_format="source",
            tool_enabled=True,
            tool_use_replyer=True,
            probability_enabled=False,
            probability_tone_enabled=True,
        ),
    )
    bot = ReplyerPromptBot(service)
    voice_ext = bot._voice_reply_extension_name()
    bot.ctx.call_capability.return_value = {"success": True, "response": json.dumps({"direction": "轻柔"})}

    # 1. 常规模式 + 关闭概率语音 + 开启 probability_tone_enabled：
    #    普通文字仍 0 注入；voice_reply 仍直接向 Replyer 注入【麦麦TTS动态语调】，且 before_send 不触发后置辅助 LLM。
    assert await bot.prepare_dynamic_tone_prompt(session_id="test-stream", reply_tool_args={"chat_anlyze": "普通回复"}) == {"action": "continue"}
    reg_off_args = {"_reply_id": "vr-reg-off", "plugin_options": {voice_ext: {}}}
    reg_off_req = await bot.prepare_dynamic_tone_prompt(session_id="test-stream", reply_tool_args=reg_off_args)
    assert "【麦麦TTS动态语调】" in reg_off_req["modified_kwargs"]["extra_prompt"]
    assert "vr-reg-off" in bot._voice_reply_prompted_ids
    await bot.prepare_probability_tts_reply(response=TEXT, session_id="test-stream", reply_tool_args=reg_off_args)
    await bot.handle_voice_reply(
        "before_send", "vr-reg-off", session_id="test-stream",
        messages=[{"segments": [{"type": "text", "data": TEXT}], "quote_previous": False}],
    )
    assert not bot._voice_reply_prompted_ids
    bot.ctx.call_capability.assert_not_awaited()

    # 2. 常规模式 + 同时开启概率语音与 probability_tone_enabled：
    #    voice_reply 仍向 Replyer 注入动态语调且不回退后置辅助 LLM（即使 Replyer 主动省略标签也沿用默认语调）。
    service.store._config.feature.probability_enabled = True
    reg_on_args = {"_reply_id": "vr-reg-on", "plugin_options": {voice_ext: {}}}
    reg_on_req = await bot.prepare_dynamic_tone_prompt(session_id="test-stream", reply_tool_args=reg_on_args)
    assert "【麦麦TTS动态语调】" in reg_on_req["modified_kwargs"]["extra_prompt"]
    await bot.prepare_probability_tts_reply(response=TEXT, session_id="test-stream", reply_tool_args=reg_on_args)
    await bot.handle_voice_reply(
        "before_send", "vr-reg-on", session_id="test-stream",
        messages=[{"segments": [{"type": "text", "data": TEXT}], "quote_previous": False}],
    )
    assert not bot._voice_reply_prompted_ids
    bot.ctx.call_capability.assert_not_awaited()

    # 3. 1.4.8 并行补充器模式 + 关闭概率语音 + 开启 probability_tone_enabled：
    #    Replyer 阶段未看到 voice_reply，但因概率语音关闭，probability_tone_enabled 不生效，直接沿用默认语调。
    service.store._config.feature.probability_enabled = False
    assert await bot.prepare_dynamic_tone_prompt(session_id="test-stream", reply_tool_args={"chat_anlyze": "并行模式关闭概率"}) == {"action": "continue"}
    assert await bot.handle_voice_reply("prepare", "vr-par-off", session_id="test-stream") == {}
    par_off_args = {
        "_reply_id": "vr-par-off",
        "plugin_options": {voice_ext: {}},
        "reply_supplement": {"plugin_options": {voice_ext: {}}, "attach_voice": None},
    }
    await bot.prepare_probability_tts_reply(response=TEXT, session_id="test-stream", reply_tool_args=par_off_args)
    await bot.handle_voice_reply(
        "before_send", "vr-par-off", session_id="test-stream",
        messages=[{"segments": [{"type": "text", "data": TEXT}], "quote_previous": False}],
    )
    bot.ctx.call_capability.assert_not_awaited()

    # 4. 1.4.8 并行补充器模式 + 同时开启概率语音与 probability_tone_enabled：
    #    Replyer 未被预先注入动态语调，before_send 自动回退调用 ProbabilityToneAdapter 补充语调。
    calls.clear()
    service.store._config.feature.probability_enabled = True
    assert await bot.prepare_dynamic_tone_prompt(session_id="test-stream", reply_tool_args={"chat_anlyze": "安慰对方"}) == {"action": "continue"}
    assert await bot.handle_voice_reply("prepare", "vr-parallel", session_id="test-stream") == {}
    par_on_args = {
        "_reply_id": "vr-parallel",
        "plugin_options": {voice_ext: {}},
        "reply_supplement": {"plugin_options": {voice_ext: {}}, "attach_voice": None},
    }
    post = await bot.prepare_probability_tts_reply(
        response=TEXT,
        session_id="test-stream",
        reply_tool_args=par_on_args,
        skip_post_process=False,
        enable_splitter=True,
        enable_chinese_typo=True,
    )
    assert post["modified_kwargs"]["response"] == TEXT
    out = await bot.handle_voice_reply(
        "before_send",
        "vr-parallel",
        session_id="test-stream",
        messages=[{"segments": [{"type": "text", "data": TEXT}], "quote_previous": False}],
    )
    assert len(calls) == 1
    bot.ctx.call_capability.assert_awaited_once()
    assert "轻柔" in json.dumps(calls, ensure_ascii=False)
    assert any(seg["type"] == "voice" for seg in out["messages"][0]["segments"])


@pytest.mark.asyncio
async def test_voice_reply_validates_and_repairs_temporary_voice_and_speed(serve, service):
    calls = []
    selected = profile("openai", await serve(tts_endpoint("openai", calls)))
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id="openai", model_id=selected.model),
        feature=Features(
            delivery_format="source",
            tool_enabled=True,
            tool_use_replyer=True,
            allow_voice_override=True,
            allow_speed_override=True,
            probability_enabled=False,
        ),
    )
    bot = ReplyerPromptBot(service)
    voice_ext = bot._voice_reply_extension_name()
    bot.ctx.call_capability.return_value = {
        "success": True,
        "response": json.dumps({"text": "[mmtts:happy]" + TEXT, "voice": "alloy", "speed": 1.25}),
    }
    reply_args = {
        "_reply_id": "vr-repair",
        "plugin_options": {voice_ext: {"voice": " alloy ", "speed": 9.9}},
    }
    post = await bot.prepare_probability_tts_reply(
        response="[mmtts:unknown]" + TEXT,
        session_id="test-stream",
        reply_tool_args=reply_args,
        skip_post_process=False,
        enable_splitter=True,
        enable_chinese_typo=True,
    )
    assert bot.ctx.call_capability.await_count == 1
    assert post["modified_kwargs"]["response"] == TEXT
    out = await bot.handle_voice_reply(
        "before_send",
        "vr-repair",
        session_id="test-stream",
        parameters={"voice": " alloy ", "speed": 9.9},
        messages=[{"segments": [{"type": "text", "data": TEXT}], "quote_previous": False}],
    )
    assert len(calls) == 1
    assert calls[0]["voice"] == "alloy"
    assert calls[0]["speed"] == 1.25
    assert "以开心愉快" in json.dumps(calls, ensure_ascii=False)
    assert any(seg["type"] == "voice" for seg in out["messages"][0]["segments"])


@pytest.mark.asyncio
async def test_voice_reply_quote_keeps_voice_and_text_while_unquoted_follows_tool_keep_text(serve, service):
    calls = []
    selected = profile("openai", await serve(tts_endpoint("openai", calls)))
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id="openai", model_id=selected.model),
        feature=Features(
            delivery_format="source",
            tool_enabled=True,
            tool_use_replyer=True,
            tool_keep_text=False,
            probability_enabled=False,
            probability_quote_keep_text=False,
        ),
    )
    bot = ReplyerPromptBot(service)
    voice_ext = bot._voice_reply_extension_name()
    reply_args = {"_reply_id": "vr-quote", "plugin_options": {voice_ext: {}}}
    await bot.prepare_probability_tts_reply(response="第一段。\n第二段。", session_id="test-stream", reply_tool_args=reply_args)
    converted = (
        await bot.handle_voice_reply(
            "before_send",
            "vr-quote",
            session_id="test-stream",
            messages=[
                {"segments": [{"type": "text", "data": "第一段。"}], "quote_previous": False},
                {"segments": [{"type": "text", "data": "第二段。"}], "quote_previous": False},
            ],
        )
    )["messages"]

    # 首段被宿主自动引用：即便 probability_quote_keep_text=False，显式语音仍先发引用文字再发语音。
    sent_events = []

    async def capture_hybrid(segments, stream_id, **kwargs):
        sent_events.append(("sdk", segments))
        return {"sent": True, "message_id": "sdk-1"}

    bot.ctx.send.hybrid = AsyncMock(side_effect=capture_hybrid)
    first_res = await bot.arrange_probability_reply_quote(
        message={"session_id": "test-stream", "raw_message": converted[0]["segments"]},
        set_reply=True,
        reply_message_id="incoming-1",
    )
    assert sent_events == [("sdk", [{"type": "reply", "data": {"target_message_id": "incoming-1"}}, {"type": "text", "data": "第一段。"}])]
    assert first_res["modified_kwargs"]["set_reply"] is False
    assert [seg["type"] for seg in first_res["modified_kwargs"]["message"]["raw_message"]] == ["voice"]

    # 第二段未引用且 tool_keep_text=False：仅发送语音。
    second_res = await bot.arrange_probability_reply_quote(
        message={"session_id": "test-stream", "raw_message": converted[1]["segments"]},
        set_reply=False,
    )
    assert len(sent_events) == 1
    assert [seg["type"] for seg in second_res["modified_kwargs"]["message"]["raw_message"]] == ["voice"]
    assert not bot._probability_voice_states


@pytest.mark.asyncio
async def test_voice_reply_fallbacks_cleanly_on_synthesis_repair_and_config_change_failures(serve, service):
    calls = []
    selected = profile("openai", await serve(tts_endpoint("openai", calls)))
    service.store._config = StudioConfig(
        profiles=[selected],
        primary=RouteTarget(provider_id="openai", model_id=selected.model),
        feature=Features(
            delivery_format="source",
            tool_enabled=True,
            tool_use_replyer=True,
            allow_speed_override=True,
            probability_enabled=False,
            probability_tone_retries=1,
        ),
    )
    bot = ReplyerPromptBot(service)
    voice_ext = bot._voice_reply_extension_name()

    # 1. 参数纠错失败：剥离非法标签、不覆写后处理参数、不发起 TTS、保留纯文本
    bot.ctx.call_capability.return_value = {"success": True, "response": "{}"}
    bad_args = {"_reply_id": "vr-bad", "plugin_options": {voice_ext: {"speed": 9.9}}}
    bad_post = await bot.prepare_probability_tts_reply(
        response="[mmtts:unknown]" + TEXT,
        session_id="test-stream",
        reply_tool_args=bad_args,
        enable_splitter=True,
        enable_chinese_typo=True,
    )
    assert bad_post["modified_kwargs"]["response"] == TEXT
    assert bad_post["modified_kwargs"]["enable_splitter"] is True
    assert bad_post["modified_kwargs"]["enable_chinese_typo"] is True
    msgs = [{"segments": [{"type": "text", "data": TEXT}], "quote_previous": False}]
    assert await bot.handle_voice_reply("before_send", "vr-bad", session_id="test-stream", messages=msgs) == {}
    assert not calls

    # 2. 中途保存配置导致失效：不发起 TTS，安全保留原文
    valid_args = {"_reply_id": "vr-cfg", "plugin_options": {voice_ext: {}}}
    await bot.prepare_probability_tts_reply(response=TEXT, session_id="test-stream", reply_tool_args=valid_args)
    bot._invalidate_probability_decisions()
    assert await bot.handle_voice_reply("before_send", "vr-cfg", session_id="test-stream", messages=msgs) == {}
    assert not calls

    # 3. 合成阶段报错：回退发送清理后的纯文本且不残留状态
    fail_args = {"_reply_id": "vr-err", "plugin_options": {voice_ext: {}}}
    await bot.prepare_probability_tts_reply(response="[mmtts:happy]" + TEXT, session_id="test-stream", reply_tool_args=fail_args)
    bot._synthesize_audio = AsyncMock(side_effect=TTSError("upstream error"))
    res = await bot.handle_voice_reply("before_send", "vr-err", session_id="test-stream", messages=msgs)
    assert res == {}
    assert not bot._voice_reply_states and not bot._probability_voice_states


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
    assert "本轮回复将合成为语音发送" not in injected["modified_kwargs"]["extra_prompt"]
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
