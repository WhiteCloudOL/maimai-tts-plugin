"""官方 Speech 字段一致性、自定义 HTTP 映射和响应完成性验证。"""

from aiohttp import web
from pydantic import ValidationError

import base64
import io
import json
import wave

import pytest

from tts_under_test.domain.compatibility import CompatibilityOptions
from tts_under_test.domain.models import Profile, TTSError
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.infrastructure.transport import Transport
from tts_under_test.providers import PROVIDERS
from tts_under_test.providers.compatible import CompatibleProvider
from tts_under_test.providers.http_protocol import at_pointer, render_template
from tts_under_test.settings.config import RuntimeSection


def profile_for(server, kind="compatible", **values):
    return Profile.model_validate(
        {
            "id": "gateway",
            "name": "兼容服务",
            "kind": kind,
            "api_key": "sample-secret",
            "base_url": server.base + "/proxy/v1",
            "model": "gpt-4o-mini-tts",
            "voice": "alloy",
            "response_format": "wav",
            **values,
        }
    )


@pytest.mark.parametrize("kind", ["openai", "compatible"])
@pytest.mark.parametrize("custom_voice", [False, True])
async def test_official_speech_fields_headers_and_custom_voice_are_identical(server, wav_audio, kind, custom_voice):
    async def handle(request):
        assert request.path == "/proxy/v1/audio/speech"
        assert request.headers["Authorization"] == "Bearer sample-secret"
        assert request.headers["OpenAI-Organization"] == "org_demo"
        assert request.headers["OpenAI-Project"] == "proj_demo"
        assert await request.json() == {
            "model": "gpt-4o-mini-tts",
            "input": "你好",
            "voice": {"id": "voice_demo"} if custom_voice else "voice_demo",
            "response_format": "wav",
            "speed": 1.25,
            "instructions": "温柔自然地说话",
        }
        return web.Response(body=wav_audio, content_type="audio/wav", headers={"x-request-id": "req_test"})

    server.handler = handle
    profile = profile_for(
        server,
        kind,
        voice="voice_demo",
        speed=1.25,
        instructions="温柔自然地说话",
        options={"custom_voice": custom_voice, "organization": "org_demo", "project": "proj_demo"},
    )
    transport = Transport(RuntimeSection())
    try:
        audio = await PROVIDERS[kind](transport).synthesize(profile, "你好")
        assert audio.data == wav_audio and audio.request_id == "req_test"
    finally:
        await transport.close()


@pytest.mark.parametrize("kind", ["openai", "compatible"])
async def test_official_clone_and_delete_contract(server, tmp_path, wav_audio, kind):
    (tmp_path / "sample.wav").write_bytes(wav_audio)

    async def handle(request):
        if request.method == "DELETE":
            assert request.path == "/proxy/v1/audio/voices/voice_test"
            return web.json_response({"id": "voice_test", "deleted": True, "object": "audio.voice.deleted"})
        assert request.path == "/proxy/v1/audio/voices"
        parts = {}
        async for part in await request.multipart():
            parts[part.name] = bytes(await part.read())
        assert parts == {"type": b"audio_sample", "name": b"maimai", "consent": b"cons_test", "audio_sample": wav_audio}
        return web.json_response({"id": "voice_test"})

    server.handler = handle
    transport = Transport(RuntimeSection(), tmp_path)
    profile = profile_for(server, kind, options={"voice_consent_id": "cons_test", "reference_source": "file"})
    try:
        client = PROVIDERS[kind](transport)
        assert await client.clone(profile, "maimai", "sample.wav", "") == "voice_test"
        await client.delete_voice(profile, "voice_test")
    finally:
        await transport.close()


@pytest.mark.parametrize("kind", ["openai", "compatible"])
@pytest.mark.parametrize("complete", [True, False])
async def test_official_sse_requires_complete_event(server, wav_audio, kind, complete):
    async def handle(request):
        assert (await request.json())["stream_format"] == "sse"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        packet = (
            "data: "
            + json.dumps({"type": "speech.audio.delta", "audio": base64.b64encode(wav_audio).decode()})
            + "\r\n\r\n"
        ).encode()
        # 跨任意网络分块，包括 UTF-8 / CRLF 边界。
        for offset in range(0, len(packet), 97):
            await response.write(packet[offset : offset + 97])
        if complete:
            await response.write(b'data: {"type":"speech.audio.done"}\r\n\r\n')
        return response

    server.handler = handle
    transport = Transport(RuntimeSection())
    profile = profile_for(server, kind, options={"openai_stream_format": "sse"})
    try:
        if complete:
            assert (await PROVIDERS[kind](transport).synthesize(profile, "你好")).data == wav_audio
        else:
            with pytest.raises(TTSError, match="完整结束"):
                await PROVIDERS[kind](transport).synthesize(profile, "你好")
    finally:
        await transport.close()


@pytest.mark.parametrize("kind", ["openai", "compatible"])
async def test_official_pcm_is_always_24khz_mono(server, kind):
    async def handle(request):
        return web.Response(body=b"\0\0" * 48, content_type="audio/pcm")

    server.handler = handle
    transport = Transport(RuntimeSection())
    try:
        profile = profile_for(server, kind, response_format="pcm", options={"sample_rate": 44100})
        audio = await PROVIDERS[kind](transport).synthesize(profile, "你好")
        with wave.open(io.BytesIO(audio.data), "rb") as stream:
            assert stream.getframerate() == 24000 and stream.getnchannels() == 1
    finally:
        await transport.close()


@pytest.mark.parametrize("method,encoding", [("POST", "json"), ("POST", "form"), ("GET", "json")])
@pytest.mark.parametrize("output", ["binary", "json_base64", "json_url", "json_hex", "text_url"])
async def test_custom_http_request_and_audio_response_mapping(server, wav_audio, method, encoding, output):
    text = '你好 "麦麦"\n{{unknown_is_spoken_text}}'

    async def handle(request):
        if request.path == "/download":
            assert "Authorization" not in request.headers and "X-Secret" not in request.headers
            return web.Response(body=wav_audio)
        assert request.path == "/proxy/v1/tts/generate" and request.method == method
        assert request.headers["X-Secret"] == "Key sample-secret"
        assert request.headers["X-Model"] == "gpt-4o-mini-tts"
        assert "Authorization" not in request.headers
        body = (
            dict(request.query)
            if method == "GET"
            else await request.json()
            if encoding == "json"
            else dict(await request.post())
        )
        assert body["text"] == text and body["speaker"] == "alloy"
        assert body["rate"] == (1.25 if method == "POST" and encoding == "json" else "1.25")
        if output == "binary":
            return web.Response(body=wav_audio, content_type="audio/wav")
        if output == "text_url":
            return web.Response(text=server.base + "/download")
        value = (
            base64.b64encode(wav_audio).decode()
            if output == "json_base64"
            else wav_audio.hex()
            if output == "json_hex"
            else server.base + "/download"
        )
        return web.json_response({"code": 1000, "result": [{"audio/value": value}]})

    server.handler = handle
    protocol = {
        "mode": "custom",
        "path": "tts/generate",
        "method": method,
        "body_encoding": encoding,
        "auth": "header",
        "auth_header": "X-Secret",
        "auth_prefix": "Key ",
        "headers": {"X-Model": "{{model}}"},
        "body": {"text": "{{text}}", "speaker": "{{voice_id}}", "rate": "{{speed}}"},
        "response": output,
        "audio_pointer": "/result/0/audio~1value",
        "status_pointer": "/code",
        "success_values": [1000],
    }
    profile = profile_for(server, speed=1.25, options={"compatibility": protocol})
    transport = Transport(RuntimeSection())
    try:
        assert (await CompatibleProvider(transport).synthesize(profile, text)).data == wav_audio
    finally:
        await transport.close()


async def test_custom_nested_json_reference_and_data_uri(server, tmp_path, wav_audio):
    (tmp_path / "sample.wav").write_bytes(wav_audio)

    async def handle(request):
        payload = await request.json()
        assert payload["request"]["messages"] == [{"content": "正文"}]
        assert payload["reference"].startswith("data:audio/wav;base64,")
        assert payload["ref_text"] == "参考录音文字"
        return web.json_response({"audio": "data:audio/wav;base64," + base64.b64encode(wav_audio).decode()})

    server.handler = handle
    profile = profile_for(
        server,
        options={
            "reference_source": "file",
            "reference_file": "sample.wav",
            "reference_text": "参考录音文字",
            "compatibility": {
                "mode": "custom",
                "body": {
                    "request": {"messages": [{"content": "{{text}}"}]},
                    "reference": "{{reference_audio}}",
                    "ref_text": "{{reference_text}}",
                },
                "response": "json_base64",
                "audio_pointer": "/audio",
            },
        },
    )
    transport = Transport(RuntimeSection(), tmp_path)
    try:
        assert (await CompatibleProvider(transport).synthesize(profile, "正文")).data == wav_audio
    finally:
        await transport.close()


@pytest.mark.parametrize("complete", [True, False])
async def test_custom_sse_event_mapping_and_truncation(server, wav_audio, complete):
    async def handle(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(
            (
                "event: voice-part\ndata: " + json.dumps({"chunk": base64.b64encode(wav_audio).decode()}) + "\n\n"
            ).encode()
        )
        if complete:
            await response.write(b"event: finish\ndata: {}\n\n")
        return response

    server.handler = handle
    profile = profile_for(
        server,
        options={
            "compatibility": {
                "mode": "custom",
                "response": "sse_base64",
                "audio_pointer": "/chunk",
                "sse_type_pointer": "",
                "sse_audio_event": "voice-part",
                "sse_done_event": "finish",
            }
        },
    )
    transport = Transport(RuntimeSection())
    try:
        if complete:
            assert (await CompatibleProvider(transport).synthesize(profile, "正文")).data == wav_audio
        else:
            with pytest.raises(TTSError, match="完成事件"):
                await CompatibleProvider(transport).synthesize(profile, "正文")
    finally:
        await transport.close()


async def test_custom_error_status_and_header_credentials_are_not_exposed(server):
    async def handle(request):
        return web.Response(status=400, text="bad key sample-secret")

    server.handler = handle
    profile = profile_for(
        server, options={"compatibility": {"mode": "custom", "auth": "header", "auth_header": "X-Secret"}}
    )
    transport = Transport(RuntimeSection())
    try:
        with pytest.raises(TTSError) as result:
            await CompatibleProvider(transport).synthesize(profile, "正文")
        assert "sample-secret" not in str(result.value) and "HTTP 400" in str(result.value)
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 500, "data": {"audio": "AA=="}},
        {"code": 0, "error": "quota exhausted", "data": {"audio": "AA=="}},
        {"code": 0, "audio": "AA=="},
    ],
)
async def test_custom_business_errors_and_missing_fields_never_become_audio(server, payload):
    async def handle(request):
        return web.json_response(payload)

    server.handler = handle
    profile = profile_for(
        server,
        options={
            "compatibility": {
                "mode": "custom",
                "response": "json_base64",
                "status_pointer": "/code",
                "success_values": [0],
            }
        },
    )
    transport = Transport(RuntimeSection())
    try:
        with pytest.raises(TTSError):
            await CompatibleProvider(transport).synthesize(profile, "正文")
    finally:
        await transport.close()


@pytest.mark.parametrize("kind", ["openai", "compatible"])
def test_legacy_models_reject_sse_and_instructions_before_http(server, kind):
    # payload 校验在创建网络请求前完成。
    client = object.__new__(PROVIDERS[kind])
    for options, instructions in [({"openai_stream_format": "sse"}, ""), ({}, "开心地说")]:
        profile = profile_for(server, kind, model="tts-1", options=options, instructions=instructions)
        with pytest.raises(TTSError):
            client.payload(profile, "正文")


@pytest.mark.parametrize("operation", ["models", "voices"])
@pytest.mark.parametrize("status", [200, 400])
async def test_custom_listing_errors_hide_nonstandard_auth_headers(server, operation, status):
    async def handle(request):
        assert request.headers["X-Secret"] == "sample-secret"
        return web.json_response({"error": "invalid key sample-secret"}, status=status)

    server.handler = handle
    profile = profile_for(
        server,
        options={
            "compatibility": {"mode": "custom", "auth": "header", "auth_header": "X-Secret", "voices_path": "voices"}
        },
    )
    transport = Transport(RuntimeSection())
    try:
        with pytest.raises(TTSError) as result:
            await CompatibleProvider(transport).listing(profile, operation)
        assert "sample-secret" not in str(result.value) and "[已隐藏]" in str(result.value)
    finally:
        await transport.close()


async def test_custom_model_and_voice_arrays_are_mapped_explicitly(server):
    async def handle(request):
        assert request.headers["Authorization"] == "Bearer sample-secret"
        if request.path.endswith("models"):
            return web.json_response({"items": [{"model_id": "model/one"}, {"model_id": "model/two"}]})
        return web.json_response({"speakers": [{"code": "speaker1", "label": "声音一"}]})

    server.handler = handle
    profile = profile_for(
        server,
        options={
            "compatibility": {
                "mode": "custom",
                "models_pointer": "/items",
                "model_id_pointer": "/model_id",
                "voices_path": "speakers",
                "voices_pointer": "/speakers",
                "voice_id_pointer": "/code",
                "voice_name_pointer": "/label",
            }
        },
    )
    transport = Transport(RuntimeSection())
    try:
        client = CompatibleProvider(transport)
        assert await client.models(profile) == ["model/one", "model/two"]
        assert (await client.voices(profile))[0]["name"] == "声音一"
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "settings",
    [
        {"path": "https://another.example/tts"},
        {"path": "../tts"},
        {"path": "//another.example/tts"},
        {"headers": {"X-Key": "x\r\nInjected:yes"}},
        {"headers": {"Authorization": "raw-secret"}},
        {"body": {"text": "{{unknown}}"}},
        {"body": {"text": "{{text"}},
        {"body": {"text": "static"}},
        {"method": "GET", "body": {"text": "{{text}}", "config": {}}},
        {"audio_pointer": "data.audio"},
    ],
)
def test_invalid_protocol_configuration_is_rejected(settings):
    with pytest.raises(ValidationError):
        CompatibilityOptions(mode="custom", **settings)


def test_templates_preserve_types_and_json_pointer_escaping():
    body = {"rate": "{{speed}}", "text": "prefix {{text}}", "voice": "{{voice}}"}
    rendered = render_template(body, {"speed": 1.25, "text": "{{not_reparsed}}", "voice": {"id": "voice_x"}})
    assert rendered == {"rate": 1.25, "text": "prefix {{not_reparsed}}", "voice": {"id": "voice_x"}}
    assert at_pointer({"a/b": {"~key": ["value"]}}, "/a~1b/~0key/0") == "value"
    with pytest.raises(TTSError):
        at_pointer({"audio": "wrong-shape"}, "/data/audio")


async def test_protocol_is_persisted_per_model_and_api_key_remains_server_side(server, tmp_path):
    profile = profile_for(server)
    data = profile.model_dump()
    data["models"].append(
        {
            "id": "custom-model",
            "voice": "voice_x",
            "parameters": {"response_format": "wav", "options": {"compatibility": {"mode": "custom", "path": "tts"}}},
        }
    )
    store = ConfigStore(tmp_path)
    await store.save({"profiles": [data]}, 0)
    copy = ConfigStore(tmp_path)
    await copy.load()
    saved = copy.profile(profile.id)
    assert saved.for_model("custom-model").options.compatibility.mode == "custom"
    assert saved.for_model(profile.model).options.compatibility is None
    assert "sample-secret" not in json.dumps(copy.snapshot())
