"""格式纠正、诊断隔离、真实平台请求和音频缓存的联合回归。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import json
import logging

import pytest

from maimai_tts_contract.components.probability import ProbabilityMixin
from maimai_tts_contract.domain.adaptive_tone import adaptive_prompt
from maimai_tts_contract.domain.models import Features, RouteTarget, StudioConfig, TTSError, TTSOperationError
from maimai_tts_contract.infrastructure.diagnostics import Diagnostics, trace_scope
from maimai_tts_contract.services.adaptive_tone import ProbabilityToneAdapter
from maimai_tts_contract.services.format_correction import correct_speech_arguments
from maimai_tts_contract.services.speech_input import speech_tool_prompt
from test_breeze_cpp import CPPServer, config as cpp_config, profile as cpp_profile
from test_prompt_pipeline import CASES, TEXT, PromptBot, profile, tts_endpoint


class ReplyBot(PromptBot, ProbabilityMixin):
    def __init__(self, service):
        super().__init__(service)
        self.config.runtime = service.settings
        self.ctx.plugin_id = "audit.tts"
        self.ctx.call_capability = AsyncMock()
        self.ctx.send.text = AsyncMock(return_value=True)
        self._probability_reply_states = {}
        self._probability_voice_states = {}
        self._probability_reply_available_sessions = set()


async def setup_bot(serve, service, *, kind="openai", **features):
    calls = []
    selected = profile(kind, await serve(tts_endpoint(kind, calls)))
    service.store._config = StudioConfig(
        profiles=[selected], primary=RouteTarget(provider_id=kind, model_id=selected.model),
        feature=Features(delivery_format="source", probability_enabled=True, probability=1, **features),
    )
    return ReplyBot(service), calls, selected


async def prepare_reply(bot, response, reply_id="reply-1"):
    return await bot.prepare_probability_tts_reply(
        response=response, session_id="test-stream", reply_tool_args={
            "_reply_id": reply_id, "plugin_options": {bot._probability_extension_name(): {}},
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", CASES)
async def test_tool_format_repair_is_validated_before_actual_tts(serve, service, kind):
    bot, calls, selected = await setup_bot(serve, service, kind=kind)
    fixed = TEXT if kind == "gpt_sovits" else "[mmtts:happy]" + TEXT
    bot.ctx.call_capability.return_value = {"success": True, "response": json.dumps({"text": fixed})}
    result = await bot.handle_tts_tool("[mmtts:unknown]" + TEXT, stream_id="test-stream")
    assert result["success"] and len(calls) == 1
    assert bot.ctx.call_capability.await_count == 1
    messages = bot.ctx.call_capability.await_args.kwargs["prompt"]
    assert speech_tool_prompt(selected, bot.features) in messages[0]["content"]
    assert "unknown" in messages[2]["content"] and "未知" in messages[3]["content"]
    assert "mmtts:" not in json.dumps(calls, ensure_ascii=False)


@pytest.mark.asyncio
async def test_repair_failure_keeps_original_text_without_tts_or_error_messages(serve, service):
    bot, calls, _ = await setup_bot(serve, service)
    bot.ctx.call_capability.return_value = {"success": True, "response": "broken JSON"}
    result = await bot.handle_tts_tool("[mmtts:unknown]" + TEXT, stream_id="test-stream")
    assert result["success"] is False and result["sent"] is True
    assert not calls and bot.ctx.call_capability.await_count == bot.features.probability_tone_retries
    assert bot.ctx.send.hybrid.await_args.args[0] == [{"type": "text", "data": TEXT}]
    bot.ctx.send.text.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_disables_format_repair_for_tools_and_normal_replies(serve, service):
    bot, calls, _ = await setup_bot(serve, service, probability_tone_retries=0)
    broken = "[mmtts:unknown]" + TEXT
    result = await bot.handle_tts_tool(broken, stream_id="test-stream")
    assert result["success"] is False and not calls
    prepared = await prepare_reply(bot, broken)
    assert prepared["modified_kwargs"]["response"] == TEXT
    assert not bot._probability_reply_states["reply-1"].should_convert
    bot.ctx.call_capability.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_reply_repair_then_probability_conversion(serve, service):
    bot, calls, _ = await setup_bot(serve, service)
    bot.ctx.call_capability.return_value = {"success": True, "response": json.dumps({"text": "[mmtts:happy]" + TEXT})}
    prepared = await prepare_reply(bot, "[mmtts:unknown]" + TEXT)
    response = prepared["modified_kwargs"]["response"]
    assert response.startswith("[mmtts:happy]") and prepared["modified_kwargs"]["skip_post_process"]
    result = await bot.handle_probability_tts(
        "before_send", "reply-1", session_id="test-stream", chat={"is_group_chat": False},
        messages=[{"segments": [{"type": "text", "data": response}]}],
    )
    assert result["messages"][0]["segments"][0]["type"] == "voice" and len(calls) == 1
    assert bot.ctx.call_capability.await_count == 1


@pytest.mark.asyncio
async def test_normal_reply_repair_failure_and_empty_controls_stay_out_of_chat(serve, service):
    bot, calls, _ = await setup_bot(serve, service)
    bot.ctx.call_capability.return_value = {"success": True, "response": '{}'}
    result = await prepare_reply(bot, "[mmtts:unknown]" + TEXT)
    assert result["modified_kwargs"]["response"] == TEXT and not calls
    empty = await prepare_reply(bot, "[mmtts:happy]", "empty")
    assert empty["action"] == "abort" and "modified_kwargs" not in empty
    bot.ctx.send.hybrid.assert_not_awaited()
    bot.ctx.send.text.assert_not_awaited()


def test_segmented_tags_are_cleaned_together_and_empty_segments_are_removed():
    messages = [{"segments": [{"type": "text", "data": "[mmtts:happy]"}, {"type": "text", "data": TEXT}]}]
    assert ProbabilityMixin._clean_probability_messages(messages) == [{"segments": [{"type": "text", "data": TEXT}]}]
    assert ProbabilityMixin._clean_probability_messages([{"segments": [{"type": "text", "data": "[mmtts:happy]"}]}]) == []
    assert "mmtts" in messages[0]["segments"][0]["data"]


@pytest.mark.asyncio
async def test_command_errors_are_logged_and_returned_without_chat_delivery(serve, service):
    bot, _, _ = await setup_bot(serve, service)
    assert (await bot.handle_tts_command(stream_id="test-stream"))[0] is False
    assert (await bot.handle_model_command(stream_id="test-stream", matched_groups={"target": "unknown-model"}))[0] is False
    bot.ctx.send.text.assert_not_awaited()
    bot.ctx.send.hybrid.assert_not_awaited()


@pytest.mark.asyncio
async def test_audio_failure_after_text_delivery_keeps_single_text_receipt(serve, service):
    bot, calls, _ = await setup_bot(serve, service, tool_keep_text=True)
    bot.ctx.send.hybrid.side_effect = [{"sent": True}, {"sent": False}]
    result = await bot.handle_tts_tool(TEXT, stream_id="test-stream")
    assert result["success"] is False and result["sent"] is True and len(calls) == 1
    assert bot.ctx.send.hybrid.await_count == 2
    assert bot.ctx.send.hybrid.await_args_list[0].args[0] == [{"type": "text", "data": TEXT}]


@pytest.mark.asyncio
async def test_adaptive_repair_keeps_full_rejected_output_and_stable_original_prompt(serve, service):
    bot, _, selected = await setup_bot(serve, service, kind="minimax", probability_tone_retries=2)
    wrong = '{"direction":"温柔"}'
    bot.ctx.call_capability.side_effect = [
        {"success": True, "response": wrong}, {"success": True, "response": 'broken JSON'},
        {"success": True, "response": '{"tone":"happy"}'},
    ]
    decision = await ProbabilityToneAdapter(bot.ctx, bot.features, TEXT, service.log)(selected)
    assert decision.tone == "happy"
    messages = [call.kwargs["prompt"] for call in bot.ctx.call_capability.await_args_list]
    assert messages[0][0] == messages[1][0] == messages[2][0]
    assert messages[0][1] == messages[1][1] == messages[2][1]
    assert messages[2][2] == {"role": "assistant", "content": wrong}
    assert messages[2][4] == {"role": "assistant", "content": 'broken JSON'}
    assert "移除 direction" in messages[2][3]["content"] and "JSON" in messages[2][5]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 1, 2, 5])
async def test_adaptive_exhaustion_stops_tts_and_does_not_cache_failure(serve, service, retries):
    bot, calls, selected = await setup_bot(serve, service, probability_tone_retries=retries)
    bot.ctx.call_capability.return_value = {"success": True, "response": "broken JSON"}
    adapter = ProbabilityToneAdapter(bot.ctx, bot.features, TEXT, service.log)
    with pytest.raises(TTSOperationError, match="停止"):
        await service.synthesize(TEXT, tone_adapter=adapter)
    assert not calls and bot.ctx.call_capability.await_count == retries + 1 and adapter.decisions == {}
    bot.ctx.call_capability.return_value = {"success": True, "response": '{}'}
    await service.synthesize(TEXT, tone_adapter=adapter)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_repaired_requests_use_final_audio_cache_without_caching_llm_decisions(serve, service):
    bot, calls, _ = await setup_bot(serve, service)
    bot.ctx.call_capability.return_value = {"success": True, "response": json.dumps({"text": "[mmtts:happy]" + TEXT})}
    for _ in range(2):
        assert (await bot.handle_tts_tool("[mmtts:unknown]" + TEXT, stream_id="test-stream"))["success"]
    assert len(calls) == 1 and bot.ctx.call_capability.await_count == 2
    bot.ctx.call_capability.return_value = {"success": True, "response": json.dumps({"text": "[mmtts:style=轻柔]" + TEXT})}
    assert (await bot.handle_tts_tool("[mmtts:unknown]" + TEXT, stream_id="test-stream"))["success"]
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    '{"text":"我已完成额外操作。"}', '{"text":"语音语调标签格式有误，请检查语调设置。"}',
    '{"text":"你好。","text":"再见。"}',
    '{"text":"你好。","instructions":"温柔"}', '```json\n{"text":"你好。"}\n```', '{}',
])
async def test_correction_rejects_hallucinated_content_and_json_protocol_violations(serve, service, response):
    bot, calls, _ = await setup_bot(serve, service)
    bot.ctx.call_capability.return_value = {"success": True, "response": response}
    result = await bot.handle_tts_tool("[mmtts:unknown]" + TEXT, stream_id="test-stream")
    assert not result["success"] and not calls
    assert bot.ctx.call_capability.await_count == (1 if response == "{}" else bot.features.probability_tone_retries)
    assert bot.ctx.send.hybrid.await_args.args[0] == [{"type": "text", "data": TEXT}]


@pytest.mark.asyncio
async def test_config_change_during_repair_preserves_text_instead_of_changing_tts_route(serve, service):
    bot, calls, _ = await setup_bot(serve, service)

    async def respond(*args, **kwargs):
        service.store._config.revision += 1
        return {"success": True, "response": json.dumps({"text": TEXT})}

    bot.ctx.call_capability.side_effect = respond
    result = await bot.handle_tts_tool("[mmtts:unknown]" + TEXT, stream_id="test-stream")
    assert not result["success"] and not calls
    assert bot.ctx.send.hybrid.await_args.args[0] == [{"type": "text", "data": TEXT}]


@pytest.mark.asyncio
async def test_cancellation_during_repair_performs_no_tts_or_chat_delivery(serve, service):
    bot, calls, _ = await setup_bot(serve, service)
    waiting = asyncio.Event()

    async def respond(*args, **kwargs):
        waiting.set()
        await asyncio.Event().wait()

    bot.ctx.call_capability.side_effect = respond
    task = asyncio.create_task(bot.handle_tts_tool("[mmtts:unknown]" + TEXT, stream_id="test-stream"))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not calls
    bot.ctx.send.hybrid.assert_not_awaited()


@pytest.mark.asyncio
async def test_info_request_logs_full_effective_text_and_style_only_on_cache_miss(serve, service, caplog):
    bot, calls, selected = await setup_bot(serve, service)
    logger = logging.getLogger("tts-request-contract")
    caplog.set_level(logging.INFO, logger=logger.name)
    service.log = Diagnostics(logger, "INFO", service.store.redact, service.store.redact_text)
    secret = service.store._config.profiles[0].api_key
    text = "完整正文" * 500 + "最后两个字。"
    with trace_scope((text, secret)):
        await service.synthesize("[mmtts:happy]" + text)
        await service.synthesize("[mmtts:happy]" + text)
    requests = [record.getMessage() for record in caplog.records if "TTS 平台请求 trace=" in record.getMessage()]
    assert len(requests) == 1 and len(calls) == 1
    assert 'text=' + json.dumps(text, ensure_ascii=False) in requests[0]
    assert 'tone="happy"' in requests[0] and "以开心愉快" in requests[0]
    assert secret not in "\n".join(record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("language", ["zh", "en", "ja"])
@pytest.mark.asyncio
async def test_correction_prompts_use_actual_allowed_examples_and_explicit_data_rules(serve, service, language):
    bot, _, selected = await setup_bot(serve, service, kind="minimax", tone_prompt_language=language)
    bot.ctx.call_capability.return_value = {"success": True, "response": '{}'}
    await correct_speech_arguments(bot.ctx, bot.features, selected, {"text": "[mmtts:unknown]" + TEXT}, service.log,
                                   automatic=True, max_text_length=4096)
    prompt = bot.ctx.call_capability.await_args.kwargs["prompt"][0]["content"]
    assert "JSON" in prompt and "{}" in prompt and "{{example}}" not in prompt
    assert {"zh": "原始可见正文逐字一致", "en": "visible dialogue verbatim", "ja": "表示本文を一字ずつ維持"}[language] in prompt
    assert {"zh": "重新生成", "en": "Regenerate", "ja": "再生成"}[language] in prompt
    adaptive = adaptive_prompt(language, ["neutral"], breeze=False)
    assert "{}" in adaptive


@pytest.mark.asyncio
async def test_tool_regenerates_platform_valid_speed_before_tts_request(serve, service):
    bot, calls, _ = await setup_bot(serve, service, kind="minimax")
    bot.ctx.call_capability.return_value = {"success": True, "response": json.dumps({"text": TEXT, "speed": 2})}
    result = await bot.handle_tts_tool(TEXT, speed=3, stream_id="test-stream")
    assert result["success"] and bot.ctx.call_capability.await_count == 1 and len(calls) == 1
    assert calls[0]["voice_setting"]["speed"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"text": TEXT, "voice": "invented"}, {"text": TEXT, "speed": 0.8},
])
async def test_regeneration_uses_only_original_argument_fields(serve, service, response):
    bot, calls, _ = await setup_bot(serve, service)
    bot.ctx.call_capability.return_value = {"success": True, "response": json.dumps(response)}
    result = await bot.handle_tts_tool("[mmtts:unknown]" + TEXT, stream_id="test-stream")
    assert not result["success"] and not calls


@pytest.mark.asyncio
async def test_breeze_local_regenerates_unsupported_cloud_event_as_native_event(serve, service):
    server = CPPServer(auth=False)

    async def handle(request):
        return await server(request)

    selected = cpp_profile(await serve(handle), key="", voice="").model_copy(update={
        "tone": profile("breeze", "http://127.0.0.1:1").tone,
    })
    service.store._config = cpp_config(selected)
    bot = ReplyBot(service)
    bot.ctx.call_capability.return_value = {"success": True, "response": json.dumps({"text": "(laugh)" + TEXT})}
    result = await bot.handle_tts_tool("(laughs)" + TEXT, stream_id="test-stream")
    assert result["success"] and bot.ctx.call_capability.await_count == 1
    assert len(server.calls) == 1 and server.calls[0][1]["text"] == "(laugh)" + TEXT


@pytest.mark.asyncio
async def test_each_breeze_section_has_its_own_info_request(serve, service, caplog):
    bot, calls, _ = await setup_bot(serve, service, kind="breeze")
    logger = logging.getLogger("tts-breeze-sections")
    caplog.set_level(logging.INFO, logger=logger.name)
    service.log = Diagnostics(logger, "INFO", service.store.redact, service.store.redact_text)
    await service.synthesize("[语气:温柔]你来了。[语气:坚定]一起面对。")
    requests = [record.getMessage() for record in caplog.records if "TTS 平台请求 trace=" in record.getMessage()]
    assert len(calls) == 2 and len(requests) == 2
    assert 'text="你来了。"' in requests[0] and 'instructions="温柔"' in requests[0]
    assert 'text="一起面对。"' in requests[1] and 'instructions="坚定"' in requests[1]


@pytest.mark.asyncio
async def test_request_logs_redact_credentials_inside_explicit_text_and_instructions(serve, service, caplog):
    bot, calls, _ = await setup_bot(serve, service)
    logger = logging.getLogger("tts-credential-redaction")
    caplog.set_level(logging.INFO, logger=logger.name)
    service.log = Diagnostics(logger, "INFO", service.store.redact, service.store.redact_text)
    secret = service.store._config.profiles[0].api_key
    await service.synthesize(TEXT + secret)
    requests = [record.getMessage() for record in caplog.records if "TTS 平台请求 trace=" in record.getMessage()]
    assert len(requests) == 1 and secret not in requests[0] and "[已隐藏]" in requests[0]


@pytest.mark.asyncio
async def test_tool_regeneration_limit_and_original_error_history(serve, service):
    bot, calls, _ = await setup_bot(serve, service, probability_tone_retries=2)
    bot.ctx.call_capability.side_effect = [
        {"success": True, "response": "broken JSON"},
        {"success": True, "response": json.dumps({"text": "[mmtts:happy]" + TEXT})},
    ]
    result = await bot.handle_tts_tool("[mmtts:unknown]" + TEXT, stream_id="test-stream")
    assert result["success"] and len(calls) == 1
    messages = [call.kwargs["prompt"] for call in bot.ctx.call_capability.await_args_list]
    assert messages[1][:4] == messages[0]
    assert messages[1][4] == {"role": "assistant", "content": "broken JSON"}
    assert "JSON" in messages[1][5]["content"]


@pytest.mark.asyncio
async def test_concurrent_parameter_regeneration_keeps_dialogue_and_budgets_separate(serve, service):
    bot, calls, _ = await setup_bot(serve, service)

    async def respond(*args, **kwargs):
        original = json.loads(kwargs["prompt"][1]["content"])["original_arguments"]["text"]
        await asyncio.sleep(0)
        return {"success": True, "response": json.dumps({"text": original.replace("unknown", "happy")})}

    bot.ctx.call_capability.side_effect = respond
    results = await asyncio.gather(*[
        bot.handle_tts_tool("[mmtts:unknown]" + text, stream_id="test-stream") for text in ("你好。", "再见。")
    ])
    assert all(result["success"] for result in results)
    assert bot.ctx.call_capability.await_count == 2 and len(calls) == 2
    assert {call["input"] for call in calls} == {"你好。", "再见。"}


@pytest.mark.asyncio
@pytest.mark.parametrize("last_valid", [False, True])
async def test_adaptive_mixed_errors_share_one_retry_budget(serve, service, last_valid):
    bot, calls, selected = await setup_bot(serve, service)
    bot.ctx.call_capability.side_effect = [
        {"success": True, "response": "broken JSON"}, asyncio.TimeoutError(),
        {"success": True, "response": '{"tone":"happy"}' if last_valid else "broken JSON"},
        {"success": True, "response": '{"tone":"happy"}'},
    ]
    adapter = ProbabilityToneAdapter(bot.ctx, bot.features, TEXT, service.log)
    if last_valid:
        assert (await adapter(selected)).tone == "happy"
    else:
        with pytest.raises(TTSOperationError):
            await adapter(selected)
    assert bot.ctx.call_capability.await_count == 3 and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("last_valid", [False, True])
async def test_tool_mixed_errors_share_one_regeneration_budget(serve, service, last_valid):
    bot, calls, _ = await setup_bot(serve, service)
    bot.ctx.call_capability.side_effect = [
        asyncio.TimeoutError(),
        {"success": True, "response": json.dumps({"text": TEXT}) if last_valid else "broken JSON"},
        {"success": True, "response": json.dumps({"text": TEXT})},
    ]
    result = await bot.handle_tts_tool("[mmtts:unknown]" + TEXT, stream_id="test-stream")
    assert result["success"] is last_valid
    assert len(calls) == int(last_valid) and bot.ctx.call_capability.await_count == 2
