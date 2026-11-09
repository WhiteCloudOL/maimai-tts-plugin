"""C++ 原版与鉴权排队版的表单、音色库、取消、缓存和 SDK 控制协议。"""

from aiohttp import web
from io import BytesIO
from typing import Any, Dict
from unittest.mock import AsyncMock

import asyncio
import json
import wave

import av
import pytest

from maimai_tts_contract.api.parameters import apply_parameters, parameter_schema
from maimai_tts_contract.api.models import APIError
from maimai_tts_contract.domain.breeze import CPP_PARAMETERS
from maimai_tts_contract.domain.models import Features, Profile, RouteTarget, StudioConfig, TTSError, Voice
from maimai_tts_contract.infrastructure.reference_codec import reference_wav
from maimai_tts_contract.providers.breeze_local import BreezeLocalProvider
from maimai_tts_contract.services.plugin_api import PluginAPIService
from maimai_tts_contract.services.studio import Studio
from maimai_tts_contract.services.cache_keys import CacheKeys
from test_prompt_pipeline import PromptBot, wav


TOKEN = "test-only-cpp-token"
PCM = b"\x01\0" * 2400


def profile(base_url: str, *, key: str = TOKEN, voice: str = "narrator", **options: Any) -> Profile:
    return Profile(id="cpp", name="C++ 测试", kind="breeze", base_url=base_url + "/v1", api_key=key,
                   model="breeze-tts-2", voice=voice, response_format="wav",
                   options={"breeze_api_mode": "self_hosted", "breeze_mode": "plain", **options})


def config(selected: Profile) -> StudioConfig:
    return StudioConfig(profiles=[selected], primary=RouteTarget(provider_id=selected.id, model_id=selected.model),
                        feature=Features(delivery_format="source"))


class CPPServer:
    """按原生 C++ HTTP 契约返回分块 PCM，并模拟 fork 的鉴权和排队等待。"""

    def __init__(self, auth: bool) -> None:
        self.auth = auth
        self.calls = []
        self.voices: Dict[str, Dict[str, Any]] = {"narrator": {"id": "narrator", "description": "旁白"}}
        self.gate = None
        self.entered = asyncio.Event()
        self.lock = asyncio.Lock()
        self.version = 1

    async def __call__(self, request: web.Request) -> web.StreamResponse:
        if self.auth and request.headers.get("Authorization") != "Bearer " + TOKEN:
            return web.json_response({"detail": "Invalid or missing API key."}, status=401)
        if request.path == "/reference.mp3":
            return web.Response(body=mp3(), content_type="audio/mpeg")
        if request.path == "/v1/voices" and request.method == "GET":
            return web.json_response(list(self.voices.values()))
        if request.method == "DELETE":
            voice_id = request.path.rsplit("/", 1)[1]
            del self.voices[voice_id]
            return web.json_response({"deleted": voice_id, "file_kept": True})
        fields = dict(await request.post())
        if "ref_audio" in fields:
            fields["ref_audio"] = fields["ref_audio"].file.read()
            with wave.open(BytesIO(fields["ref_audio"]), "rb") as reference:
                assert reference.getparams()[:3] == (1, 2, 24000)
        self.calls.append((request.path, fields, request.headers.get("Authorization")))
        if request.path == "/v1/voices":
            item = {"id": fields["name"], "saved": True, "metadata": json.loads(fields["metadata"])}
            self.voices[item["id"]] = item
            return web.json_response(item)
        assert request.path == "/v1/audio/speech"
        if "voice_id" in fields and fields["voice_id"] not in self.voices:
            return web.json_response({"error": "unknown voice_id"}, status=404)
        async with self.lock:
            self.entered.set()
            if self.gate is not None:
                await self.gate.wait()
            if request.transport is None or request.transport.is_closing():
                return web.Response(status=499)
            response = web.StreamResponse(headers={"Content-Type": "audio/pcm", "X-Sample-Rate": "24000",
                                                    "X-Sample-Format": "s16le"})
            await response.prepare(request)
            payload = PCM if self.version == 1 else b"\x02\0" * 2400
            await response.write(payload[:111])
            await response.write(payload[111:])
            await response.write_eof()
            return response


def mp3() -> bytes:
    output = BytesIO()
    with av.open(BytesIO(wav())) as source, av.open(output, "w", format="mp3") as target:
        stream = target.add_stream("libmp3lame", rate=24000)
        for frame in source.decode(audio=0):
            for packet in stream.encode(frame):
                target.mux(packet)
        for packet in stream.encode(None):
            target.mux(packet)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", [False, True])
async def test_original_and_fork_accept_saved_voice_and_cpp_parameters(serve, service, auth):
    server = CPPServer(auth)
    selected = profile(await serve(server.__call__), key=TOKEN if auth else "", breeze_cpp_split_chars=0,
                       breeze_cpp_temperature=0.6, breeze_cpp_top_k=20, breeze_cpp_top_p=0.9,
                       breeze_cpp_repetition_penalty=1.1, breeze_cpp_max_new_tokens=500)
    audio = await service.synthesize("你好。", config=config(selected))
    fields = server.calls[0][1]
    assert fields["voice_id"] == "narrator" and fields["split_chars"] == "0"
    assert {key: fields[key] for key in ("temperature", "top_k", "top_p", "repetition_penalty", "max_new_tokens")} == {
        "temperature": "0.6", "top_k": "20", "top_p": "0.9", "repetition_penalty": "1.1", "max_new_tokens": "500"}
    assert server.calls[0][2] == ("Bearer " + TOKEN if auth else None)
    with wave.open(BytesIO(audio.data)) as stream:
        assert stream.getnframes() == 2400 and stream.readframes(2400) == PCM


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "wrong-test-token"])
async def test_fork_rejects_missing_or_wrong_key_without_retry(serve, service, key):
    server = CPPServer(True)
    with pytest.raises(TTSError, match="HTTP 401"):
        await service.synthesize("你好。", config=config(profile(await serve(server.__call__), key=key)))
    assert server.calls == []


@pytest.mark.asyncio
async def test_serial_waiter_cancellation_releases_the_instance_slot(serve, service):
    server = CPPServer(True)
    server.gate = asyncio.Event()
    selected = profile(await serve(server.__call__))
    first = asyncio.create_task(service.synthesize("第一句。", config=config(selected)))
    await asyncio.wait_for(server.entered.wait(), 1)
    cancelled = asyncio.create_task(service.synthesize("第二句。", config=config(selected)))
    await asyncio.sleep(0.02)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    server.gate.set()
    await first
    await service.synthesize("第三句。", config=config(selected))
    assert [call[1]["text"] for call in server.calls] == ["第一句。", "第三句。"]


@pytest.mark.asyncio
async def test_fork_waits_for_server_queue_once_and_timeout_stops_request(serve, service):
    server = CPPServer(True)
    server.gate = asyncio.Event()
    selected = profile(await serve(server.__call__))
    running = asyncio.create_task(service.synthesize("排队中的一句。", config=config(selected)))
    await asyncio.wait_for(server.entered.wait(), 1)
    await asyncio.sleep(0.03)
    assert not running.done() and len(server.calls) == 1
    server.gate.set()
    await running
    server.gate.clear()
    with pytest.raises(TTSError, match="超时"):
        await service.synthesize("超时的一句。", config=config(selected))
    server.gate.set()
    assert len(server.calls) == 2


@pytest.mark.asyncio
async def test_cpp_reference_mp3_becomes_wav_and_voice_library_uses_bearer(serve, service):
    server = CPPServer(False)
    base = await serve(server.__call__)
    selected = profile(base, voice="", breeze_mode="reference", reference_source="url",
                       reference_url=base + "/reference.mp3", reference_text="参考录音文字。")
    await service.synthesize("新的正文。", config=config(selected))
    assert server.calls[0][1]["ref_audio"].startswith(b"RIFF")
    client = BreezeLocalProvider(service.http)
    created = await client.clone(selected, "温柔旁白", base + "/reference.mp3", "参考录音文字。")
    assert created.voice_id.startswith("mmtts_") and created.state == "ready"
    voices = await client.voices(selected)
    assert next(item for item in voices if item["id"] == created.voice_id)["name"] == "温柔旁白"
    await client.delete_voice(selected, created.voice_id)
    assert created.voice_id not in {item["id"] for item in await client.voices(selected)}
    assert all(call[2] == "Bearer " + TOKEN for call in server.calls)


@pytest.mark.asyncio
async def test_mutable_saved_voice_bypasses_audio_cache(serve, service):
    server = CPPServer(True)
    selected = profile(await serve(server.__call__))
    first = await service.synthesize("相同正文。", config=config(selected))
    server.version = 2
    second = await service.synthesize("相同正文。", config=config(selected))
    assert first.data != second.data and len(server.calls) == 2
    assert first.cache_status == second.cache_status == "bypass"


@pytest.mark.asyncio
async def test_tool_and_sdk_auto_keep_local_tags_and_hide_them_from_chat(serve, service):
    server = CPPServer(True)
    selected = profile(await serve(server.__call__), breeze_guidance_scale=2)
    selected.tone.mode = "dynamic"
    service.store._config = config(selected)
    bot = PromptBot(service)
    tool = await bot.handle_tts_tool("[mmtts:style=轻柔][轻笑]你来了。", stream_id="test-stream")
    assert tool["success"]
    assert server.calls[0][1]["instruction"] == "轻柔" and server.calls[0][1]["text"] == "[轻笑]你来了。"
    assert bot.ctx.send.hybrid.await_args.kwargs["processed_plain_text"] == "你来了。"
    bot.ctx.call_capability = AsyncMock(return_value={"success": True, "response": '{"direction":"温柔","events":[{"before":"你来了","tag":"[轻笑]"}]}'})
    api = PluginAPIService(service.store, service, bot.ctx, bot._send_tts_message, lambda: True)
    try:
        result = await api.call("simple", text="你来了。", output="send", stream_id="test-stream", request_id="cpp-auto")
        assert result["ok"], result
        assert server.calls[-1][1]["voice_id"] == "narrator"
        assert server.calls[-1][1]["instruction"] == "温柔" and server.calls[-1][1]["text"] == "[轻笑]你来了。"
        assert result["data"]["audio"]["display_text"] == "你来了。"
        again = await api.call("simple", text="你来了。", output="send", stream_id="test-stream", request_id="cpp-auto")
        assert again == result and len(server.calls) == 2
    finally:
        await api.close()


def test_cpp_parameters_and_local_voice_modes_resolve_before_schema():
    selected = profile("http://127.0.0.1:1").for_model("breeze-tts-2").with_selected_voice()
    assert selected.options.breeze_mode == "saved"
    assert set(CPP_PARAMETERS).issubset(parameter_schema(selected)["properties"])
    assert "speed" not in parameter_schema(selected)["properties"]
    assert "reference_file" not in parameter_schema(selected)["properties"]
    assert apply_parameters(selected, {"breeze_cpp_split_chars": None}).options.breeze_cpp_split_chars is None
    cloud = profile("http://127.0.0.1:1", voice="")
    cloud.options.breeze_api_mode = "cloud"
    with pytest.raises(APIError):
        apply_parameters(cloud, {"breeze_cpp_top_k": 10})
    selected.voices = [Voice(id="designed", name="设计", origin="design", instructions="温柔女声")]
    design = selected.with_synthesis_overrides(voice="designed").with_selected_voice()
    assert design.options.breeze_mode == "design" and design.instructions == "温柔女声"


def test_reference_wav_checks_decoding_and_capacity():
    with pytest.raises(TTSError):
        reference_wav(b"invalid audio", 1024)
    with pytest.raises(TTSError, match="容量"):
        reference_wav(wav(), 100)


def test_local_sampling_options_partition_only_the_local_synthesis_cache():
    local = profile("http://127.0.0.1:1", voice="")
    cloud = local.model_copy(deep=True)
    cloud.options.breeze_api_mode = "cloud"
    cloud.voice = "cloud-voice"
    keys = CacheKeys()
    local_key = keys.speech(local, "正文。")
    cloud_key = keys.speech(cloud, "正文。")
    local.options.breeze_cpp_top_k = cloud.options.breeze_cpp_top_k = 17
    assert keys.speech(local, "正文。") != local_key
    assert keys.speech(cloud, "正文。") == cloud_key


@pytest.mark.asyncio
async def test_sdk_advanced_cpp_parameters_reach_request_and_cache_identity(serve, service):
    server = CPPServer(True)
    service.store._config = config(profile(await serve(server.__call__), voice=""))
    bot = PromptBot(service)
    api = PluginAPIService(service.store, service, bot.ctx, bot._send_tts_message, lambda: True)
    try:
        first = await api.call("advanced", text="相同正文。", parameters={"breeze_cpp_top_k": 17, "breeze_cpp_split_chars": 0})
        same = await api.call("advanced", text="相同正文。", parameters={"breeze_cpp_top_k": 17, "breeze_cpp_split_chars": 0})
        changed = await api.call("advanced", text="相同正文。", parameters={"breeze_cpp_top_k": 18})
        assert first["ok"] and same["ok"] and changed["ok"] and len(server.calls) == 2
        assert same["data"]["audio"]["cache"]["synthesis"] == "hit"
        assert server.calls[0][1]["top_k"] == "17" and server.calls[0][1]["split_chars"] == "0"
        assert server.calls[1][1]["top_k"] == "18" and "split_chars" not in server.calls[1][1]
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_voice_catalog_cache_keeps_credentials_separate_and_names_fit_saved_voices(serve, service):
    server = CPPServer(True)
    server.voices["long-description"] = {"id": "long-description", "description": "温柔清晰" * 40}
    selected = profile(await serve(server.__call__))
    values = await service.catalog_list(selected, "voices")
    assert len(next(item for item in values if item["id"] == "long-description")["name"]) == 80
    invalid = selected.model_copy(update={"api_key": "invalid-test-token"})
    with pytest.raises(TTSError, match="HTTP 401"):
        await service.catalog_list(invalid, "voices")


@pytest.mark.asyncio
async def test_original_busy_error_is_exposed_once_then_releases_instance_slot(serve, service):
    calls = []
    server = CPPServer(False)

    async def busy_then_ready(request):
        calls.append(request.path)
        if len(calls) == 1:
            return web.json_response({"error": "busy"}, status=409)
        return await server(request)

    selected = profile(await serve(busy_then_ready), key="")
    with pytest.raises(TTSError, match="HTTP 409"):
        await service.synthesize("第一句。", config=config(selected))
    assert len(calls) == 1
    assert (await service.synthesize("第二句。", config=config(selected))).data.startswith(b"RIFF")


@pytest.mark.asyncio
async def test_route_voice_protection_covers_multiple_keys_of_one_cpp_server(serve, service):
    server = CPPServer(True)
    first = profile(await serve(server.__call__))
    other = first.model_copy(update={"id": "other-key", "api_key": "another-test-key"})
    service.store._config = StudioConfig(profiles=[first, other],
                                        primary=RouteTarget(provider_id=other.id, model_id=other.model))
    studio = Studio(service.store, service)
    try:
        with pytest.raises(TTSError, match="整体路由正在使用"):
            studio.launch("delete_voice", profile_id=first.id, model_id=first.model, voice_id="narrator")
        assert "narrator" in server.voices
    finally:
        await studio.close()
