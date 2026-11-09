"""公开 SDK API 的目录、参数、语气、投递、幂等、任务和资源生命周期。"""

from aiohttp import web
from maibot_sdk.context import PluginContext
from unittest.mock import AsyncMock

import asyncio
import base64
import json

import msgpack
import pytest

from maimai_tts_contract.api.parameters import apply_parameters, parameter_schema
from maimai_tts_contract.api.endpoints import PluginAPIMixin
from maimai_tts_contract.domain.bailian import QWEN_AUDIO_MODELS
from maimai_tts_contract.domain.models import Audio, Features, Profile, RouteTarget, StudioConfig, Voice
from maimai_tts_contract.api.models import APIError
from maimai_tts_contract.infrastructure.api_runtime import APIAudioStore, APIJobs
from maimai_tts_contract.services.plugin_api import PluginAPIService
from test_prompt_pipeline import CASES, TEXT, PromptBot, profile, tts_endpoint, wav


async def setup_api(serve, service, kind="openai"):
    calls = []
    selected = profile(kind, await serve(tts_endpoint(kind, calls)))
    selected.tone.mode = "off"
    service.store._config = StudioConfig(profiles=[selected], primary=RouteTarget(provider_id=kind, model_id=selected.model),
                                        feature=Features(delivery_format="source"))
    bot = PromptBot(service)
    response = '{"tone":"happy"}' if kind in {"minimax", "bailian_minimax"} else '{"direction":"轻柔"}'
    bot.ctx.call_capability = AsyncMock(return_value={"success": True, "response": response})
    api = PluginAPIService(service.store, service, bot.ctx, bot._send_tts_message, lambda: True)
    return api, calls, bot


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", CASES)
async def test_simple_auto_uses_real_platform_and_preserves_saved_mode(serve, service, kind):
    api, calls, bot = await setup_api(serve, service, kind)
    try:
        result = await api.call("simple", text=TEXT)
        assert result["ok"], result
        info = result["data"]["audio"]
        assert info["provider_id"] == kind and info["display_text"] == TEXT
        assert info["duration_seconds"] == 0.1
        assert calls and service.store.config.profiles[0].tone.mode == "off"
        assert bot.ctx.call_capability.await_count == (0 if kind == "gpt_sovits" else 1)
        chunk = await api.call("get_audio", audio_id=info["audio_id"], offset=0, limit=262144)
        assert chunk["ok"] and base64.b64decode(chunk["data"]["data_base64"]) == wav()
    finally:
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [kind for kind in CASES if kind != "gpt_sovits"])
async def test_manual_control_reaches_http_with_original_route_mode_off(serve, service, kind):
    api, calls, bot = await setup_api(serve, service, kind)
    control = {"mode": "manual", "tone": "happy"} if kind in {"minimax", "bailian_minimax"} else {"mode": "manual", "style": "轻柔"}
    try:
        result = await api.call("advanced", text=TEXT, control=control)
        assert result["ok"], result
        assert calls and result["data"]["audio"]["display_text"] == TEXT
        bot.ctx.call_capability.assert_not_awaited()
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_parameter_validation_and_voice_overrides_are_per_request(serve, service):
    api, calls, _ = await setup_api(serve, service)
    try:
        data = await api.call("catalog")
        assert data["ok"] and data["data"]["providers"][0]["models"]
        assert "api_key" not in str(data) and "base_url" not in str(data)
        schema = await api.call("describe", provider_id="openai")
        assert schema["ok"] and "speed" in schema["data"]["parameters"]["properties"]
        bad = await api.call("validate", text=TEXT, parameters={"minimax_pitch": 2})
        assert not bad["ok"] and bad["error"]["code"] == "INVALID_PARAMETER"
        assert calls == []
        result = await api.call("advanced", text=TEXT, provider_id="openai", model_id="gpt-4o-mini-tts", voice_id="coral", parameters={"speed": 1.2})
        assert result["ok"], result
        assert calls[0]["voice"] == "coral" and calls[0]["speed"] == 1.2
        assert service.store.config.profiles[0].voice == "alloy"
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_api_switch_gates_calls_and_status_reports_state(serve, service):
    api, calls, bot = await setup_api(serve, service)
    try:
        service.store._config.feature.api_enabled = False
        status = await api.call("status")
        assert status["ok"] and status["data"]["enabled"] is False
        for name, options in (("catalog", {}), ("simple", {"text": TEXT}), ("get_audio", {"audio_id": "old", "offset": 0, "limit": 100})):
            result = await api.call(name, **options)
            assert result["error"]["code"] == "API_DISABLED"
        assert calls == []
        bot.ctx.call_capability.assert_not_awaited()
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_repeated_send_request_is_deduplicated_and_conflicts_are_exposed(serve, service):
    api, calls, bot = await setup_api(serve, service)
    try:
        request = dict(text=TEXT, output="send", stream_id="test-stream", request_id="test:one")
        first, second = await asyncio.gather(api.call("simple", **request), api.call("simple", **request))
        assert first["ok"] and first == second
        assert len(calls) == bot.ctx.send.hybrid.await_count == 1
        conflict = await api.call("simple", **dict(request, text="另一句话。"))
        assert conflict["error"]["code"] == "REQUEST_CONFLICT"
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_async_task_cancellation_and_close_release_resources(serve, service):
    api, _, bot = await setup_api(serve, service)
    started = asyncio.Event()
    async def waiting(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    bot.ctx.call_capability.side_effect = waiting
    try:
        response = await api.call("submit", text=TEXT, control={"mode": "auto"})
        await asyncio.wait_for(started.wait(), 2)
        job_id = response["data"]["job_id"]
        cancelled = await api.call("cancel_job", job_id=job_id)
        assert cancelled["data"]["state"] == "cancelled"
        assert cancelled["data"]["result"]["error"]["code"] == "CANCELLED"
        assert (await api.call("get_job", job_id=job_id))["data"]["state"] == "cancelled"
    finally:
        await api.close()
    assert api.audio.size == 0 and not api.jobs.jobs


@pytest.mark.asyncio
async def test_synthesis_cache_remains_shared_across_api_requests(serve, service):
    api, calls, _ = await setup_api(serve, service)
    try:
        first = await api.call("advanced", text=TEXT)
        second = await api.call("advanced", text=TEXT)
        assert first["ok"] and second["ok"] and len(calls) == 1
        assert second["data"]["audio"]["cache"]["synthesis"] == "hit"
        audio_id = first["data"]["audio"]["audio_id"]
        send = await api.send_audio(audio_id, "test-stream", request_id="existing-audio")
        assert send["ok"] and send["data"]["send"]["sent"] is True
    finally:
        await api.close()


@pytest.mark.parametrize("kind", CASES)
def test_described_defaults_are_valid_for_every_platform(kind):
    selected = profile(kind, "http://127.0.0.1:1").for_model(CASES[kind][0]).with_selected_voice()
    schema = parameter_schema(selected)
    defaults = {key: spec["default"] for key, spec in schema["properties"].items()}
    assert apply_parameters(selected, defaults).speed == selected.speed


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["simple", "advanced"])
@pytest.mark.parametrize("output", ["audio", "send"])
async def test_both_entries_support_both_output_modes(serve, service, entry, output):
    api, calls, bot = await setup_api(serve, service)
    try:
        result = await api.call(entry, text=TEXT, output=output, stream_id="test-stream", keep_text=True)
        assert result["ok"], result
        assert "audio" in result["data"]
        assert ("send" in result["data"]) == (output == "send")
        assert bot.ctx.send.hybrid.await_count == (2 if output == "send" else 0)
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_analysis_result_can_be_used_in_advanced_without_second_llm_call(serve, service):
    api, _, bot = await setup_api(serve, service, "breeze")
    bot.ctx.call_capability.return_value = {"success": True, "response": '{"tone":"happy","direction":"轻柔"}'}
    try:
        analyzed = await api.call("analyze", text=TEXT, control={"mode": "auto", "style_hint": "轻声安慰"})
        assert analyzed["ok"], analyzed
        analysis = analyzed["data"]
        assert analysis["llm_used"] and analysis["control"]["style"] == "轻柔"
        result = await api.call("advanced", text=analysis["text"], control=analysis["control"])
        assert result["ok"], result
        assert bot.ctx.call_capability.await_count == 1
        assert any("轻声安慰" in part["content"] for part in bot.ctx.call_capability.await_args.kwargs["prompt"])
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_switch_cancels_running_api_jobs(serve, service):
    api, _, bot = await setup_api(serve, service)
    started = asyncio.Event()
    async def wait(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    bot.ctx.call_capability.side_effect = wait
    response = await api.call("submit", text=TEXT, control={"mode": "auto"})
    await asyncio.wait_for(started.wait(), 2)
    service.store._config.feature.api_enabled = False
    await api.settings_changed()
    assert api.jobs.get(response["data"]["job_id"]).state == "cancelled"
    await api.close()


@pytest.mark.asyncio
async def test_llm_exhaustion_is_exposed_as_an_api_error(serve, service):
    api, calls, bot = await setup_api(serve, service)
    service.store._config.feature.probability_tone_retries = 0
    bot.ctx.call_capability.return_value = {"success": True, "response": "broken JSON"}
    try:
        result = await api.call("simple", text=TEXT)
        assert result["error"]["code"] == "LLM_ERROR"
        assert calls == []
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_public_entry_dispatch_uses_versioned_api_service(serve, service):
    api, _, _ = await setup_api(serve, service)
    class APIHarness(PluginAPIMixin):
        public_api = api
    try:
        harness = APIHarness()
        assert (await harness.tts_catalog())["ok"]
        assert (await harness.tts_simple(TEXT))["ok"]
        assert (await harness.tts_advanced({"text": TEXT}))["ok"]
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_sdk_public_api_roundtrip_with_msgpack_and_both_entries(serve, service):
    api, _, _ = await setup_api(serve, service)
    class APIHarness(PluginAPIMixin):
        public_api = api
    harness = APIHarness()
    methods = {"tts_simple": harness.tts_simple, "tts_advanced": harness.tts_advanced}

    async def rpc(method, plugin_id, payload, timeout_ms=None):
        assert method == "cap.call" and plugin_id == "contract.caller"
        decoded = msgpack.unpackb(msgpack.packb(payload, use_bin_type=True), raw=False)
        args = decoded["args"]
        assert decoded["capability"] == "api.call" and args["version"] == "1"
        handler = methods[args["api_name"].rsplit(".", 1)[1]]
        metadata = handler.__func__.__maibot_component_info__
        assert metadata.public is True and metadata.version == "1"
        result = await handler(**args["args"])
        return msgpack.unpackb(msgpack.packb({"success": True, "result": result}, use_bin_type=True), raw=False)
    ctx = PluginContext("contract.caller", rpc_call=rpc)
    try:
        simple = await ctx.api.call("WhiteCloudOL.maimai-tts-plugin.tts_simple", version="1", text=TEXT)
        advanced = await ctx.call_capability("api.call", timeout_ms=600000,
                                            api_name="WhiteCloudOL.maimai-tts-plugin.tts_advanced", version="1",
                                            args={"request": {"text": TEXT, "output": "send", "stream_id": "test-stream"}})
        assert simple["ok"] and advanced["ok"] and advanced["data"]["send"]["sent"]
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_parameter_changes_partition_audio_cache_and_equivalent_keys_deduplicate(serve, service):
    api, calls, _ = await setup_api(serve, service)
    try:
        first = await api.call("advanced", text=TEXT, parameters={"speed": 1.1, "response_format": "wav"}, request_id="ordered")
        same = await api.call("advanced", text=TEXT, parameters={"response_format": "wav", "speed": 1.1}, request_id="ordered")
        changed = await api.call("advanced", text=TEXT, parameters={"speed": 1.2})
        assert first["ok"] and same == first and changed["ok"]
        assert len(calls) == 2 and [body["speed"] for body in calls] == [1.1, 1.2]
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_read_offsets_expiry_and_audio_capacity(serve, service):
    api, _, _ = await setup_api(serve, service)
    try:
        result = await api.call("advanced", text=TEXT)
        audio_id = result["data"]["audio"]["audio_id"]
        one = await api.call("get_audio", audio_id=audio_id, offset=0, limit=127)
        two = await api.call("get_audio", audio_id=audio_id, offset=one["data"]["next_offset"], limit=262144)
        assert base64.b64decode(one["data"]["data_base64"]) + base64.b64decode(two["data"]["data_base64"]) == wav()
        bad = await api.call("get_audio", audio_id=audio_id, offset=-1, limit=10)
        assert bad["error"]["code"] == "INVALID_PARAMETER"
        api.audio.entries[audio_id].expires = 0
        assert (await api.call("get_audio", audio_id=audio_id, offset=0, limit=10))["error"]["code"] == "AUDIO_EXPIRED"
        assert api.audio.size == 0
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_silk_send_reports_integer_duration_and_full_audio(serve, service):
    api, _, bot = await setup_api(serve, service)
    service.store._config.feature.delivery_format = "silk"
    try:
        result = await api.call("simple", text=TEXT, output="send", stream_id="test-stream")
        assert result["ok"], result
        info = result["data"]["audio"]
        assert info["duration_seconds"] == 1 and info["response_format"] == "silk"
        sent = base64.b64decode(bot.ctx.send.hybrid.await_args.args[0][0]["binary_data_base64"])
        assert sent == api.audio.get(info["audio_id"]).audio.data
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_logs_contain_request_metadata_and_hide_spoken_text(serve, service):
    api, _, _ = await setup_api(serve, service)
    try:
        result = await api.call("simple", text="这是本次 API 的私有朗读正文。", style_hint="私有表达偏好", request_id="log-case")
        assert result["ok"]
        logs = json.dumps(service.log.logger.mock_calls, ensure_ascii=False, default=str)
        assert "log-case" in logs and "API 请求完成" in logs
        assert "私有朗读正文" not in logs and "私有表达偏好" not in logs
        assert "test-only-not-a-real-key" not in logs
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_model_selection_uses_its_voice_and_skips_llm_for_plain_model(serve, service):
    api, calls, bot = await setup_api(serve, service)
    data = service.store.config.profiles[0].model_dump()
    data["models"].append({"id": "tts-1", "voice": "echo"})
    service.store._config.profiles[0] = Profile.model_validate(data)
    try:
        result = await api.call("simple", text=TEXT, provider_id="openai", model_id="tts-1")
        assert result["ok"], result
        assert calls[0]["model"] == "tts-1" and calls[0]["voice"] == "echo"
        assert result["data"]["audio"]["allowed_tones"] == []
        bot.ctx.call_capability.assert_not_awaited()
        assert service.store.config.primary.model_id == "gpt-4o-mini-tts"
    finally:
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("parameters", [{"speed": True}, {"speed": "1.1"}, {"api_key": "test-key"}])
async def test_strict_parameter_types_and_connection_fields_are_validated_locally(serve, service, parameters):
    api, calls, bot = await setup_api(serve, service)
    try:
        result = await api.call("validate", text=TEXT, parameters=parameters)
        assert result["error"]["code"] == "INVALID_PARAMETER"
        assert calls == []
        bot.ctx.call_capability.assert_not_awaited()
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_busy_slot_and_expired_job_errors(serve, service):
    api, _, bot = await setup_api(serve, service)
    service.store._config.feature.api_max_jobs = 1
    started = asyncio.Event()
    async def wait(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    bot.ctx.call_capability.side_effect = wait
    try:
        first = await api.call("submit", text=TEXT, control={"mode": "auto"})
        await asyncio.wait_for(started.wait(), 2)
        second = await api.call("submit", text=TEXT, control={"mode": "auto"})
        assert second["error"]["code"] == "BUSY"
        job_id = first["data"]["job_id"]
        await api.call("cancel_job", job_id=job_id)
        api.jobs.get(job_id).expires = 0
        assert (await api.call("get_job", job_id=job_id))["error"]["code"] == "JOB_EXPIRED"
    finally:
        await api.close()


def test_audio_result_pool_reclaims_entries_by_capacity():
    store = APIAudioStore()
    audio = Audio(wav(), "audio/wav", "wav")
    first = store.put(audio, {}, ttl=600, max_bytes=len(audio.data) + 1)
    second = store.put(audio, {}, ttl=600, max_bytes=len(audio.data) + 1)
    with pytest.raises(APIError, match="audio_id"):
        store.get(first["audio_id"])
    assert store.get(second["audio_id"]).audio == audio
    assert store.size == len(audio.data)


@pytest.mark.asyncio
async def test_cancel_before_task_starts_releases_slot_and_keeps_receipt():
    jobs = APIJobs()
    operation = AsyncMock(return_value={"ok": True, "data": {}})
    try:
        job = jobs.start("immediate", "same", operation, limit=1, ttl=600)
        job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)
        assert job.state == "cancelled" and job.expires > 0
        assert (await jobs.wait(job))["error"]["code"] == "CANCELLED"
        assert jobs.start("immediate", "same", operation, limit=1, ttl=600) is job
        jobs.start("next", "same", operation, limit=1, ttl=600)
        operation.assert_not_awaited()
    finally:
        await jobs.close()


@pytest.mark.asyncio
async def test_send_retry_reuses_receipt_after_audio_eviction(serve, service):
    api, _, bot = await setup_api(serve, service)
    try:
        audio = (await api.call("advanced", text=TEXT))["data"]["audio"]
        first = await api.send_audio(audio["audio_id"], "test-stream", request_id="send-once")
        api.audio.clear()
        again = await api.send_audio(audio["audio_id"], "test-stream", request_id="send-once")
        assert first["ok"] and again == first
        conflict = await api.send_audio(audio["audio_id"], "other-stream", request_id="send-once")
        assert conflict["error"]["code"] == "REQUEST_CONFLICT"
        assert bot.ctx.send.hybrid.await_count == 1
    finally:
        await api.close()


def test_qwen_audio_described_parameters_can_all_be_applied():
    selected = Profile(id="qwen", name="qwen", kind="qwen", base_url="http://127.0.0.1:1",
                       model="qwen-audio-3.1-tts-flash", voice="Cherry", response_format="wav")
    selected = selected.for_model(selected.model).with_selected_voice()
    defaults = {key: spec["default"] for key, spec in parameter_schema(selected)["properties"].items()}
    assert apply_parameters(selected, defaults).options.sample_rate == 24000
    changed = apply_parameters(selected, {"response_format": "opus", "sample_rate": 16000, "opus_bit_rate": 48})
    assert changed.options.opus_bit_rate == 48


def test_schema_matches_transport_and_reference_constraints():
    selected = profile("minimax", "http://127.0.0.1:1").for_model("speech-2.6-hd").with_selected_voice()
    assert apply_parameters(selected, {"minimax_normalization": "quality"}).options.minimax_normalization == "quality"
    streaming = apply_parameters(selected, {"minimax_transport": "sse"})
    assert parameter_schema(streaming)["properties"]["minimax_normalization"]["enum"] == ["", "basic"]
    assert parameter_schema(streaming)["properties"]["minimax_output"]["enum"] == ["hex"]
    reference = profile("siliconflow", "http://127.0.0.1:1").for_model("FunAudioLLM/CosyVoice2-0.5B").with_selected_voice()
    assert parameter_schema(reference)["properties"]["reference_source"]["enum"] == ["url", "file"]


@pytest.mark.asyncio
async def test_adaptive_metadata_reports_native_emotion_sent_to_platform(serve, service):
    api, calls, _ = await setup_api(serve, service, "minimax")
    try:
        result = await api.call("simple", text=TEXT)
        assert result["ok"], result
        assert result["data"]["audio"]["parameters"]["minimax_emotion"] == calls[0]["voice_setting"]["emotion"] == "happy"
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_waiter_timeout_keeps_shared_send_and_reuses_final_receipt(serve, service):
    api, calls, bot = await setup_api(serve, service)
    entered, release = asyncio.Event(), asyncio.Event()

    async def sending(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"sent": True, "message_id": "voice-001"}

    bot.ctx.send.hybrid.side_effect = sending
    request = dict(text=TEXT, output="send", stream_id="test-stream", request_id="timeout-send")
    waiting = asyncio.create_task(api.call("simple", **request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        retry = asyncio.create_task(api.call("simple", **request))
        release.set()
        result = await retry
        assert result["ok"] and result["data"]["send"]["message_id"] == "voice-001"
        assert bot.ctx.send.hybrid.await_count == len(calls) == 1
    finally:
        release.set()
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,kwargs", [
    ("get_audio", {"audio_id": []}), ("get_audio", {"audio_id": "a", "offset": True}),
    ("get_job", {"job_id": []}), ("describe", {"provider_id": []}),
    ("voices", {"source": "unknown"}), ("advanced", {"text": TEXT, "operation": "status"}),
    ("advanced", {"text": TEXT, "parameters": {"speed": b"1.1"}}),
    ("advanced", {"text": TEXT, "parameters": {"speed": float("nan")}}),
])
async def test_invalid_queries_return_parameter_errors(serve, service, operation, kwargs):
    api, calls, bot = await setup_api(serve, service)
    try:
        result = await api.call(operation, **kwargs)
        assert result["error"]["code"] == "INVALID_PARAMETER"
        assert calls == []
        bot.ctx.call_capability.assert_not_awaited()
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_cancel_one_of_concurrent_syntheses_preserves_other_cache_waiter(serve, service):
    api, calls, _ = await setup_api(serve, service)
    entered, release = asyncio.Event(), asyncio.Event()
    upstream = service.execute

    async def slow(operation):
        entered.set()
        await release.wait()
        return await upstream(operation)

    service.execute = slow
    try:
        first = await api.call("submit", text=TEXT, request_id="cancel-one")
        second = await api.call("submit", text=TEXT, request_id="keep-one")
        await asyncio.wait_for(entered.wait(), 2)
        # 两个调用已进入共享合成；取消一个任务只释放自己的等待者。
        await asyncio.sleep(0)
        await api.call("cancel_job", job_id=first["data"]["job_id"])
        release.set()
        result = await api.jobs.wait(api.jobs.get(second["data"]["job_id"]))
        assert result["ok"] and len(calls) == 1
        assert result["data"]["audio"]["cache"]["synthesis"] == "shared"
    finally:
        release.set()
        await api.close()


@pytest.mark.asyncio
async def test_analysis_keeps_parameter_context_for_advanced(serve, service):
    api, calls, bot = await setup_api(serve, service, "minimax")
    try:
        analysis = (await api.call("analyze", text=TEXT, parameters={"speed": 1.3}))["data"]
        assert analysis["parameters"] == {"speed": 1.3}
        result = await api.call("advanced", text=analysis["text"], control=analysis["control"], parameters=analysis["parameters"])
        assert result["ok"] and calls[0]["voice_setting"]["speed"] == 1.3
        assert bot.ctx.call_capability.await_count == 1
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_api_deadline_releases_slot_and_records_timeout(serve, service, monkeypatch):
    api, calls, bot = await setup_api(serve, service)
    monkeypatch.setattr("maimai_tts_contract.services.plugin_api.API_TIMEOUT_SECONDS", 0.01)

    async def waiting(*args, **kwargs):
        await asyncio.Event().wait()

    bot.ctx.call_capability.side_effect = waiting
    try:
        result = await api.call("analyze", text=TEXT, request_id="deadline")
        assert result["error"]["code"] == "TIMEOUT" and calls == []
        job = api.jobs.jobs[api.jobs.requests["deadline"]]
        assert job.state == "error" and job.expires > 0
        assert (await api.call("status"))["data"]["running_jobs"] == 0
    finally:
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", sorted(QWEN_AUDIO_MODELS))
async def test_qwen_audio_parameters_and_auto_instruction_reach_http(serve, service, model):
    api, calls, _ = await setup_api(serve, service, "qwen")
    root = service.store.config.profiles[0]
    selected = Profile(id="qwen", name="qwen", kind="qwen", base_url=root.base_url, api_key=root.api_key,
                       model=model, voice="Cherry", response_format="wav")
    service.store._config = StudioConfig(profiles=[selected], primary=RouteTarget(provider_id="qwen", model_id=model))
    try:
        result = await api.call("advanced", text=TEXT, control={"mode": "auto"},
                                parameters={"sample_rate": 16000, "speed": 1.3, "volume": 35, "pitch": 1.1,
                                            "seed": 17, "voice_language": "zh"})
        assert result["ok"], result
        inputs = calls[0]["input"]
        assert inputs["sample_rate"] == 16000 and inputs["seed"] == 17 and inputs["rate"] == 1.3
        assert inputs["volume"] == 35 and inputs["pitch"] == 1.1 and inputs["language_hints"] == ["zh"]
        assert inputs["instruction"] == "轻柔"
    finally:
        await api.close()


def test_qwen_http_schema_contains_consumed_fields_and_opus_rates_are_exact():
    selected = profile("qwen", "http://127.0.0.1:1").for_model(CASES["qwen"][0]).with_selected_voice()
    assert set(parameter_schema(selected)["properties"]) == {"language", "optimize_instructions", "response_format"}
    cosy = profile("cosyvoice", "http://127.0.0.1:1").for_model(CASES["cosyvoice"][0]).with_selected_voice()
    opus = apply_parameters(cosy, {"response_format": "opus", "sample_rate": 16000})
    assert parameter_schema(opus)["properties"]["sample_rate"]["enum"] == [8000, 12000, 16000, 24000, 48000]
    with pytest.raises(APIError):
        apply_parameters(opus, {"sample_rate": 22050})


@pytest.mark.asyncio
async def test_breeze_local_seed_and_style_reach_multipart(serve, service):
    calls = []

    async def local(request):
        calls.append(dict(await request.post()))
        return web.Response(body=b"\0" * 4800, content_type="audio/pcm", headers={"X-Sample-Rate": "24000"})

    selected = Profile(id="local", name="local", kind="breeze", base_url=await serve(local), model="breeze-tts-2",
                       response_format="wav", options={"breeze_mode": "plain"})
    service.store._config = StudioConfig(profiles=[selected], primary=RouteTarget(provider_id="local", model_id=selected.model))
    bot = PromptBot(service)
    api = PluginAPIService(service.store, service, bot.ctx, bot._send_tts_message, lambda: True)
    try:
        result = await api.call("advanced", text=TEXT, control={"style": "轻柔"},
                                parameters={"seed": 17, "breeze_mode": "design", "breeze_guidance_scale": 2})
        assert result["ok"], result
        assert {key: calls[0][key] for key in ("text", "cfg_scale", "seed", "instruction")} == {"text": TEXT, "cfg_scale": "2.0", "seed": "17", "instruction": "轻柔"}
        assert result["data"]["audio"]["parameters"]["seed"] == 17
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_refresh_cache_and_request_id_have_independent_scopes(serve, service):
    api, calls, _ = await setup_api(serve, service)
    try:
        first = await api.call("advanced", text=TEXT, request_id="original")
        hit = await api.call("advanced", text=TEXT, request_id="reuse")
        fresh = await api.call("advanced", text=TEXT, request_id="refresh", refresh_cache=True)
        repeat = await api.call("advanced", text=TEXT, request_id="refresh", refresh_cache=True)
        assert first["ok"] and hit["ok"] and fresh["ok"] and repeat == fresh
        assert len(calls) == 2 and hit["data"]["audio"]["cache"]["synthesis"] == "hit"
    finally:
        await api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,parameters,control", [
    ("volcengine", {"enable_ssml": True, "latex_read": True}, {"mode": "auto"}),
    ("volcengine", {"enable_ssml": True, "volc_instruction_mode": "cot"}, {"mode": "auto"}),
    ("cosyvoice", {"hot_fix": {"unknown": []}}, {"mode": "auto"}),
])
async def test_incompatible_parameter_combinations_are_rejected_before_llm(serve, service, kind, parameters, control):
    api, calls, bot = await setup_api(serve, service, kind)
    try:
        result = await api.call("validate", text=TEXT, parameters=parameters, control=control)
        assert result["error"]["code"] == "INVALID_PARAMETER"
        assert calls == []
        bot.ctx.call_capability.assert_not_awaited()
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_voice_catalog_is_available_while_default_voice_is_training(serve, service):
    api, calls, bot = await setup_api(serve, service)
    service.store._config.profiles[0].voices = [Voice(id="alloy", name="克隆中", clone_state="training")]
    try:
        result = await api.call("voices", source="saved")
        assert result["ok"] and result["data"]["voices"][0]["state"] == "training"
        result = await api.call("simple", text=TEXT)
        assert not result["ok"] and calls == []
        bot.ctx.call_capability.assert_not_awaited()
    finally:
        await api.close()
