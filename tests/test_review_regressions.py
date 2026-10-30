"""路由覆盖、云端删除保护、会话隔离、异常边界和子任务回收。"""

from aiohttp import web
from maibot_sdk.context import PluginContext, PluginPaths
from unittest.mock import AsyncMock
from urllib.parse import quote

import asyncio
import json
import logging
import pytest

from tts_under_test.domain.catalog import CATALOG
from tts_under_test.domain.models import TTSError
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.infrastructure.transport import Transport
from tts_under_test.infrastructure.voice_codec import SILK_HEADER
from tts_under_test.providers.bailian import BailianVoiceProvider
from tts_under_test.providers.base import normalize_audio
from tts_under_test.providers.compatible import CompatibleProvider
from tts_under_test.providers.http_protocol import json_value
from tts_under_test.providers.openai import OpenAIProvider
from tts_under_test.services.studio import Studio
from tts_under_test.services.tts import TTSService
from tts_under_test.settings.config import RuntimeSection


def platform(kind, server, **overrides):
    preset = CATALOG[kind]
    return {
        "id": kind,
        "name": preset["name"],
        "kind": kind,
        "base_url": server.base + "/v1",
        "api_key": "test-secret",
        "model": preset["model"],
        "voice": preset["voice"],
        "response_format": preset["formats"][0],
        **overrides,
    }


async def test_temporary_speed_wins_over_saved_voice_speed(tmp_path, server, wav_audio):
    speeds = []

    async def handle(request):
        speeds.append((await request.json())["speed"])
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    store = ConfigStore(tmp_path)
    data = platform("compatible", server, voices=[{"id": "alloy", "name": "音色", "speed": 0.75}])
    await store.save({"profiles": [data], "primary": {"provider_id": "compatible", "model_id": "tts-1"}}, 0)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    try:
        await service.synthesize("保存的语速")
        await service.synthesize("临时语速", speed=1.25)
        assert speeds == [0.75, 1.25]
        assert store.profile("compatible").voices[0].speed == 0.75
    finally:
        await service.close()


async def test_fake_audio_content_type_enters_backup_instead_of_caching_text(tmp_path, server, wav_audio):
    async def handle(request):
        return web.Response(
            body=b"not-an-audio-file" if request.path.startswith("/bad/") else wav_audio,
            content_type="audio/mpeg",
        )

    server.handler = handle
    store = ConfigStore(tmp_path)
    first = platform("compatible", server, base_url=server.base + "/bad")
    other = platform("openai", server)
    await store.save(
        {
            "profiles": [first, other],
            "primary": {"provider_id": first["id"], "model_id": first["model"]},
            "fallbacks": [{"provider_id": other["id"], "model_id": other["model"]}],
        }, 0,
    )
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    try:
        audio = await service.synthesize("检查音频内容")
        assert audio.provider_id == "openai" and audio.data == wav_audio
        assert "文件标识" in service.history[1]["error"]
    finally:
        await service.close()


@pytest.mark.parametrize("case", ["draft", "model", "profile", "mix"])
async def test_cloud_delete_protects_saved_routes_across_same_account(tmp_path, server, case):
    store = ConfigStore(tmp_path)
    data = platform("minimax", server, model="speech-2.8-hd", voice="active-voice")
    models = [{"id": "speech-2.8-hd", "voice": "active-voice"}, {"id": "speech-2.6-hd", "voice": "active-voice"}]
    data["models"] = models
    if case == "mix":
        data["options"] = {"timbre_weights": [{"voice_id": "active-voice", "weight": 50}]}
        data["voice"] = models[0]["voice"] = "other-voice"
    profiles = [data]
    route = {"provider_id": "minimax", "model_id": "speech-2.8-hd"}
    if case == "model":
        route["model_id"] = "speech-2.6-hd"
    if case == "profile":
        profiles.append({**data, "id": "other-profile", "name": "同账号另一配置"})
        route["provider_id"] = "other-profile"
    await store.save({"profiles": profiles, "primary": route}, 0)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    studio = Studio(store, service)
    try:
        draft = store.snapshot()["profiles"][0]
        if case == "draft":
            draft["voice"] = draft["models"][0]["voice"] = "unsaved-voice"
        with pytest.raises(TTSError, match="整体路由正在使用"):
            studio.launch("delete_voice", "minimax", voice_id="active-voice", draft=json.dumps(draft))
        assert not studio.jobs
    finally:
        await studio.close()
        await service.close()


async def test_upstream_cookies_are_not_shared_with_download_or_other_accounts(server, wav_audio):
    observed = []

    async def handle(request):
        observed.append(request.headers.get("Cookie"))
        if request.path == "/account":
            response = web.json_response({"ok": True})
            response.set_cookie("private_account", "account-a")
            return response
        assert "Authorization" not in request.headers
        return web.Response(body=wav_audio)

    server.handler = handle
    base = server.base.replace("127.0.0.1", "localhost")
    transport = Transport(RuntimeSection())
    try:
        await transport.json("GET", base + "/account", {"Authorization": "Bearer account-a"})
        await transport.download(base + "/audio")
        await transport.json("GET", base + "/account", {"Authorization": "Bearer account-b"})
        assert observed == [None, None, None]
    finally:
        await transport.close()


@pytest.mark.parametrize("fail", [True, False])
async def test_bailian_detail_failure_and_cancellation_collect_all_children(tmp_path, server, monkeypatch, fail):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def enrollment(profile, inputs, parameters=None):
        if inputs["action"] == "list_voice":
            return {"voice_list": [{"voice_id": "bad"}, {"voice_id": "slow"}]}
        if inputs["voice_id"] == "bad":
            await started.wait()
            if fail:
                raise TTSError("详情查询失败")
            await asyncio.Event().wait()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    transport = Transport(RuntimeSection(), tmp_path)
    provider = BailianVoiceProvider(transport)
    monkeypatch.setattr(provider, "enrollment", enrollment)
    task = asyncio.create_task(provider.voices(ConfigStore(tmp_path).prepare_profile(platform("cosyvoice", server))))
    try:
        await asyncio.wait_for(started.wait(), 1)
        if not fail:
            task.cancel()
        with pytest.raises(TTSError if fail else asyncio.CancelledError):
            await task
        assert cancelled.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await transport.close()


@pytest.mark.parametrize(
    "kind,broken",
    [
        ("mimo", {"choices": [[]]}),
        ("mimo", {"choices": [{"message": []}]}),
        ("cosyvoice", []),
        ("cosyvoice", {"header": []}),
        ("qwen", []),
        ("qwen", {"type": "session.created", "session": []}),
        ("qwen", {"type": "response.done", "response": []}),
        ("volcengine", []),
        ("volcengine", {"code": False}),
    ],
)
async def test_malformed_provider_responses_reach_configured_backup(tmp_path, server, wav_audio, kind, broken):
    async def handle(request):
        if request.path == "/v1/audio/speech":
            return web.Response(body=wav_audio, content_type="audio/wav")
        if kind in {"qwen", "cosyvoice"}:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            await socket.send_json(broken)
            await socket.close()
            return socket
        if kind == "volcengine":
            return web.Response(text=json.dumps(broken) + "\n", content_type="application/json")
        return web.json_response(broken)

    server.handler = handle
    first = platform(kind, server)
    if kind == "qwen":
        first.update(model="qwen3-tts-flash-realtime", voice="Cherry", response_format="wav")
    elif kind == "cosyvoice":
        first["options"] = {"alibaba_transport": "websocket"}
    backup = platform("compatible", server)
    store = ConfigStore(tmp_path)
    await store.save(
        {
            "profiles": [first, backup],
            "primary": {"provider_id": first["id"], "model_id": first["model"]},
            "fallbacks": [{"provider_id": backup["id"], "model_id": backup["model"]}],
        },
        0,
    )
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    try:
        audio = await service.synthesize("检查备选链")
        assert audio.provider_id == "compatible"
        assert [item["success"] for item in service.history] == [True, False]
    finally:
        await service.close()


async def test_redaction_handles_prefixes_encodings_and_long_draft_keys(tmp_path, server, caplog):
    caplog.set_level(logging.DEBUG)
    store = ConfigStore(tmp_path)
    prefix, full = "key/with+space ", "key/with+space suffix-secret"
    await store.save(
        {"profiles": [platform("compatible", server, api_key=prefix), platform("openai", server, api_key=full)]}, 0
    )
    assert "suffix-secret" not in store.redact(full + " " + quote(full, safe=""))
    assert "with%2Bspace" not in Transport.sanitize(quote(full, safe=""), {"authorization": "bearer " + full})
    service = TTSService(store, RuntimeSection(log_level="DEBUG"), logging.getLogger("test"))
    studio = Studio(store, service)
    secret = "long-draft-secret-" * 150

    async def fail():
        service.log.info("上游标识", request_id=secret)
        raise TTSError(secret + " 请求失败")

    async def unexpected():
        raise RuntimeError("upstream-private-payload")

    try:
        result = studio.start(fail, private_values=(secret,))
        await studio.jobs[result["job_id"]].task
        assert "long-draft-secret" not in studio.job(result["job_id"])["error"]
        assert "long-draft-secret" not in caplog.text
        result = studio.start(unexpected)
        await studio.jobs[result["job_id"]].task
        assert "RuntimeError" in studio.job(result["job_id"])["error"]
        assert "upstream-private-payload" not in studio.job(result["job_id"])["error"]
        assert "upstream-private-payload" not in caplog.text
        assert "工作台任务内部异常" in caplog.text and "stack=" in caplog.text
    finally:
        await studio.close()
        await service.close()


async def test_invalid_draft_address_returns_actionable_error(tmp_path, server):
    store = ConfigStore(tmp_path)
    await store.save({"profiles": [platform("compatible", server)]}, 0)
    draft = store.snapshot()["profiles"][0]
    draft["base_url"] = {"invalid": "type"}
    with pytest.raises(TTSError, match="地址必须是字符串"):
        store.prepare_profile(draft)


async def test_cloud_delete_compares_management_endpoint_across_provider_types(tmp_path, server):
    store = ConfigStore(tmp_path)
    first = platform("openai", server, clone_url=server.base + "/shared-voices", voice="active-voice")
    other = platform(
        "compatible", server, base_url=server.base + "/another/v1", clone_url=first["clone_url"], voice="active-voice"
    )
    await store.save(
        {"profiles": [first, other], "primary": {"provider_id": "compatible", "model_id": other["model"]}}, 0
    )
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    studio = Studio(store, service)
    try:
        with pytest.raises(TTSError, match="整体路由正在使用"):
            studio.launch("delete_voice", "openai", voice_id="active-voice")
    finally:
        await studio.close()
        await service.close()


async def test_unused_voice_deletion_is_not_blocked_by_unrelated_legacy_route(tmp_path, server, monkeypatch):
    store = ConfigStore(tmp_path)
    first = platform("openai", server)
    other = platform("minimax", server, base_url="", endpoint=server.base + "/legacy/t2a_v2")
    await store.save(
        {"profiles": [first, other], "primary": {"provider_id": "minimax", "model_id": other["model"]}}, 0
    )
    delete = AsyncMock()
    monkeypatch.setattr(OpenAIProvider, "delete_voice", delete)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    studio = Studio(store, service)
    try:
        job = studio.launch("delete_voice", "openai", voice_id="unused-voice")
        await studio.jobs[job["job_id"]].task
        assert studio.job(job["job_id"])["result"]["deleted"] is True
        delete.assert_awaited_once()
    finally:
        await studio.close()
        await service.close()


async def test_http_error_redacts_long_credentials_across_network_chunks(server):
    secret = "k" * 3000

    async def handle(request):
        response = web.StreamResponse(status=401)
        await response.prepare(request)
        await response.write(("Authorization: Bearer " + secret[:1000]).encode())
        await asyncio.sleep(0)
        await response.write(secret[1000:].encode())
        await response.write_eof()
        return response

    server.handler = handle
    transport = Transport(RuntimeSection())
    try:
        with pytest.raises(TTSError) as failure:
            await transport.json("GET", server.base, {"Authorization": "Bearer " + secret})
        assert "HTTP 401" in str(failure.value) and "k" * 100 not in str(failure.value)
    finally:
        await transport.close()


async def test_custom_header_auth_hides_long_key_before_error_truncation(tmp_path, server):
    secret = "custom-header-secret-" * 150

    async def handle(request):
        return web.Response(status=401, text="failure " + secret)

    server.handler = handle
    profile = ConfigStore(tmp_path).prepare_profile(
        platform(
            "compatible", server, api_key=secret,
            options={"compatibility": {"mode": "custom", "auth": "header", "auth_header": "CustomToken"}},
        )
    )
    transport = Transport(RuntimeSection())
    try:
        with pytest.raises(TTSError) as failure:
            await CompatibleProvider(transport).synthesize(profile, "你好")
        assert "HTTP 401" in str(failure.value)
        assert "custom-header-secret" not in str(failure.value)
    finally:
        await transport.close()


async def test_tool_input_and_model_status_send_errors_are_observed(plugin_module, tmp_path, server, caplog):
    caplog.set_level(logging.DEBUG)
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    rpc = AsyncMock(side_effect=RuntimeError("private-rpc-payload"))
    plugin._set_context(
        PluginContext("WhiteCloudOL.maimai-tts-plugin", rpc_call=rpc, paths=PluginPaths(tmp_path, tmp_path))
    )
    await plugin.on_load()
    try:
        await plugin.store.save({"profiles": [platform("compatible", server)]}, 0)
        result = await plugin.handle_tts_tool(None, stream_id="real-stream")
        assert result["success"] is False and "text" in result["error"]
        assert rpc.await_count == 0
        result = await plugin.handle_model_command(stream_id="real-stream")
        assert result[0] is False and "发送失败" in result[1]
        assert not plugin._control_reply_guards
        assert "模型状态消息发送异常" in caplog.text
        assert "private-rpc-payload" not in caplog.text
        assert plugin.primary_tone_profile() is None
        payload = plugin.store.config.model_dump()
        payload["primary"] = {"provider_id": "compatible"}
        await plugin.store.save(payload, 1)
        assert plugin.primary_tone_profile().model == "tts-1"
    finally:
        await plugin.on_unload()


async def test_deep_upstream_json_is_reported_as_invalid_response(server):
    # CPython 3.12 的 C JSON 解码器与 Python 栈计数使用不同的深度预算。
    depth = 10000
    raw = b'{"nested":' * depth + b'0' + b'}' * depth

    async def handle(request):
        return web.Response(body=raw, content_type="application/json")

    server.handler = handle
    transport = Transport(RuntimeSection())
    try:
        with pytest.raises(TTSError, match="有效 JSON"):
            await transport.json("GET", server.base, {})
        with pytest.raises(TTSError, match="有效 JSON"):
            await json_value(raw)
    finally:
        await transport.close()


async def test_wav_container_returned_for_pcm_request_is_not_wrapped_twice(tmp_path, server, wav_audio):
    profile = ConfigStore(tmp_path).prepare_profile(platform("compatible", server, response_format="pcm"))
    audio = await normalize_audio(wav_audio, profile)
    assert audio.response_format == "wav" and audio.data == wav_audio


async def test_transcode_failure_uses_backup_and_evicts_only_invalid_speech(tmp_path, server, wav_audio):
    broken = [True]
    calls = []

    async def handle(request):
        calls.append(request.path)
        raw = b"ID3broken_audio" if request.path.startswith("/bad/") and broken[0] else wav_audio
        return web.Response(body=raw, content_type="audio/mpeg")

    server.handler = handle
    store = ConfigStore(tmp_path)
    first = platform("compatible", server, base_url=server.base + "/bad")
    backup = platform("openai", server)
    await store.save({
        "profiles": [first, backup],
        "primary": {"provider_id": first["id"], "model_id": first["model"]},
        "fallbacks": [{"provider_id": backup["id"], "model_id": backup["model"]}],
    }, 0)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    try:
        audio = await service.synthesize("语音转码备选", delivery_format="silk")
        assert audio.provider_id == "openai" and audio.data.startswith(SILK_HEADER)
        broken[0] = False
        audio = await service.synthesize("语音转码备选", delivery_format="silk")
        assert audio.provider_id == "compatible" and audio.data.startswith(SILK_HEADER)
        assert calls == ["/bad/audio/speech", "/v1/audio/speech", "/bad/audio/speech"]
    finally:
        await service.close()
