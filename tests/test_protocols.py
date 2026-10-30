from aiohttp import web
from tts_under_test.domain.models import Profile, TTSError
from tts_under_test.infrastructure.transport import Transport
from tts_under_test.providers import PROVIDERS
from tts_under_test.providers.base import normalize_audio
from tts_under_test.settings.config import RuntimeSection

import base64
import io
import json
import pytest
import wave


@pytest.mark.parametrize("kind", list(PROVIDERS))
@pytest.mark.parametrize("address_mode", ["request", "base"])
async def test_native_synthesis_protocols(kind, server, wav_audio, address_mode):
    requests = []
    paths = {
        "breeze": "/proxy/v1/audio/speech",
        "openai": "/proxy/v1/audio/speech",
        "compatible": "/proxy/v1/audio/speech",
        "siliconflow": "/proxy/v1/audio/speech",
        "mimo": "/proxy/v1/chat/completions",
        "qwen": "/proxy/api/v1/services/aigc/multimodal-generation/generation",
        "bailian_minimax": "/proxy/api/v1/services/aigc/multimodal-generation/generation",
        "minimax": "/proxy/v1/t2a_v2",
        "cosyvoice": "/proxy/api-ws/v1/inference",
        "volcengine": "/proxy/api/v3/tts/unidirectional",
        "gpt_sovits": "/proxy/tts",
    }
    expected_path = paths[kind] if address_mode == "base" else "/exact-endpoint"

    async def handle(request):
        if request.path == "/download":
            assert "Authorization" not in request.headers
            return web.Response(body=wav_audio, content_type="audio/wav")
        assert request.path == expected_path
        if kind == "breeze":
            reader = await request.multipart()
            fields = {}
            async for part in reader:
                fields[part.name] = bytes(await part.read())
            assert fields["text"].decode() == "你好"
            assert fields["ref_audio"] == wav_audio and fields["ref_text"].decode() == "录音文字"
            return web.Response(body=wav_audio[44:], content_type="audio/pcm", headers={"X-Sample-Rate": "24000"})
        if kind == "cosyvoice":
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            start = await socket.receive_json()
            task_id = start["header"]["task_id"]
            assert start["header"]["action"] == "run-task"
            assert start["payload"]["parameters"]["rate"] == 1
            await socket.send_json({"header": {"task_id": task_id, "event": "task-started"}})
            continuation, finish = await socket.receive_json(), await socket.receive_json()
            assert continuation["payload"]["input"]["text"] == "你好"
            assert finish["header"]["action"] == "finish-task"
            assert finish["header"]["task_id"] == task_id
            await socket.send_bytes(wav_audio)
            await socket.send_json({"header": {"task_id": task_id, "event": "task-finished"}})
            return socket
        body = await request.json()
        requests.append((dict(request.headers), body, request.path))
        if kind == "minimax":
            assert body["text"] == "你好" and body["voice_setting"]["voice_id"] == "Cherry"
            return web.json_response({"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": wav_audio.hex()}})
        if kind == "bailian_minimax":
            assert body["input"]["voice_setting"]["voice_id"] == "Cherry"
            return web.json_response(
                {"output": {"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": wav_audio.hex()}}}
            )
        if kind == "qwen":
            assert body["input"] == {"text": "你好", "voice": "Cherry", "language_type": "Auto"}
            return web.json_response({"output": {"audio": {"url": server.base + "/download"}}})
        if kind == "mimo":
            assert body["messages"][-1] == {"role": "assistant", "content": "你好"}
            assert body["audio"] == {"format": "wav", "voice": "Cherry"}
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "audio": {
                                    "data": base64.b64encode(wav_audio).decode(),
                                }
                            }
                        }
                    ]
                }
            )
        if kind == "volcengine":
            assert request.headers["X-Api-Key"] == "test-key"
            assert request.headers["X-Api-Resource-Id"] == "seed-tts-2.0"
            assert body["req_params"]["speaker"] == "Cherry"
            lines = (
                json.dumps({"code": 0, "data": base64.b64encode(wav_audio).decode()})
                + "\n"
                + json.dumps({"code": 20000000, "message": "ok"})
            ).encode()
            response = web.StreamResponse()
            await response.prepare(request)
            for i in range(0, len(lines), 127):
                await response.write(lines[i : i + 127])
            return response
        if kind == "gpt_sovits":
            assert body["ref_audio_path"] == "/voices/reference.wav"
            assert body["text_lang"] == "zh"
            assert body["streaming_mode"] is False
        else:
            assert body["input"] == "你好"
            assert body["voice"] == "Cherry"
        return web.Response(body=wav_audio, content_type="application/octet-stream")

    server.handler = handle
    profile = Profile(
        id="test",
        name="测试",
        kind=kind,
        endpoint=(server.base.replace("http:", "ws:") if kind == "cosyvoice" else server.base) + "/exact-endpoint",
        api_key="test-key",
        model="MiniMax/speech-2.8-turbo" if kind == "bailian_minimax" else "test-model",
        voice="Cherry",
        response_format="wav" if kind not in {"volcengine"} else "mp3",
        options={
            "reference_path": "/voices/reference.wav",
            **({"reference_url": server.base + "/download", "reference_text": "录音文字"} if kind == "breeze" else {}),
        },
    )
    if address_mode == "base":
        suffix = (
            "/proxy/api/v1"
            if kind in {"qwen", "cosyvoice", "bailian_minimax"}
            else "/proxy/api/v3"
            if kind == "volcengine"
            else "/proxy"
            if kind == "gpt_sovits"
            else "/proxy/v1"
        )
        profile = Profile.model_validate(
            {**profile.model_dump(), "endpoint": "", "base_url": server.base + suffix + "/"}
        )
    transport = Transport(RuntimeSection())
    try:
        audio = await PROVIDERS[kind](transport).synthesize(profile, "你好")
        assert audio.data == wav_audio
        assert audio.response_format == "wav"
        if requests:
            assert requests[0][2] == expected_path  # 旧完整地址保留；BaseURL 自动生成平台路径。
    finally:
        await transport.close()


@pytest.mark.parametrize("kind", ["cosyvoice", "siliconflow"])
@pytest.mark.parametrize("address_mode", ["request", "base"])
async def test_clone_and_voice_listing(kind, server, wav_audio, address_mode):
    async def handle(request):
        if request.path == "/sample.wav":
            assert "Authorization" not in request.headers
            return web.Response(body=wav_audio)
        if request.path == "/voices" or request.path.endswith("/audio/voice/list"):
            if kind == "siliconflow":
                return web.json_response({"results": [{"uri": "speech:mine", "customName": "mine", "model": "test"}]})
            body = await request.json()
            assert body["input"]["action"] == "list_voice"
            return web.json_response({"output": {"voice_list": [{"voice_id": "mine", "target_model": "test"}]}})
        body = await request.json()
        if kind == "cosyvoice":
            if body["input"]["action"] == "list_voice":
                assert request.path == "/proxy/api/v1/services/audio/tts/customization"
                return web.json_response({"output": {"voice_list": [{"voice_id": "mine", "target_model": "test"}]}})
            if body["input"]["action"] == "query_voice":
                return web.json_response({"output": {"voice_id": "mine", "target_model": "test", "status": "OK"}})
            assert body["input"]["target_model"] == "test"
            assert body["input"]["audio"].endswith("sample.wav")
            return web.json_response({"output": {"voice_id": "mine"}})
        assert body["text"] == "录音文字"
        assert body["audio"].startswith("data:audio/wav;base64,")
        return web.json_response({"uri": "speech:mine"})

    server.handler = handle
    profile = Profile(
        id="test",
        name="测试",
        kind=kind,
        endpoint=(server.base.replace("http:", "ws:") if kind == "cosyvoice" else server.base) + "/speech",
        api_key="test-key",
        model="test",
        voices_url=server.base + "/voices",
        clone_url=server.base + "/clone",
        response_format="wav",
    )
    if address_mode == "base":
        profile = Profile.model_validate(
            {
                **profile.model_dump(),
                "endpoint": "",
                "base_url": server.base + ("/proxy/api/v1" if kind == "cosyvoice" else "/proxy/v1"),
                "voices_url": "",
                "clone_url": "",
            }
        )
    transport = Transport(RuntimeSection())
    try:
        provider = PROVIDERS[kind](transport)
        voice = await provider.clone(profile, "mine", server.base + "/sample.wav", "录音文字")
        voice_id = voice.voice_id if kind == "cosyvoice" else voice
        if kind == "cosyvoice":
            assert voice.state == "training"
        assert voice_id.endswith("mine")
        voices = await provider.voices(profile)
        assert voices[0]["id"] == voice_id
    finally:
        await transport.close()


async def test_truncated_volcengine_audio_is_rejected(server, wav_audio):
    async def handle(request):
        return web.Response(text=json.dumps({"code": 0, "data": base64.b64encode(wav_audio).decode()}) + "\n")

    server.handler = handle
    profile = Profile(id="test", name="测试", kind="volcengine", endpoint=server.base, api_key="key")
    transport = Transport(RuntimeSection())
    try:
        with pytest.raises(TTSError, match="完成状态"):
            await PROVIDERS["volcengine"](transport).synthesize(profile, "你好")
    finally:
        await transport.close()


async def test_volcengine_opus_rate_volume_and_bitrate(server, wav_audio):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="48000"):
        Profile(id="v", name="火山", kind="volcengine", base_url=server.base, response_format="opus")

    async def handle(request):
        body = await request.json()
        assert body["req_params"]["audio_params"] == {
            "format": "ogg_opus",
            "sample_rate": 48000,
            "speech_rate": 50,
            "loudness_rate": 25,
            "bit_rate": 160000,
        }
        return web.Response(text=json.dumps({"code": 20000000, "data": base64.b64encode(wav_audio).decode()}) + "\n")

    server.handler = handle
    http = Transport(RuntimeSection())
    p = Profile(
        id="v",
        name="火山",
        kind="volcengine",
        base_url=server.base,
        api_key="key",
        response_format="opus",
        speed=1.5,
        options={"sample_rate": 48000, "volc_loudness_rate": 25, "volc_bit_rate": 160000},
    )
    try:
        assert (await PROVIDERS["volcengine"](http).synthesize(p, "你好")).data == wav_audio
    finally:
        await http.close()


async def test_pcm_gets_a_playable_wav_header():
    profile = Profile(id="test", name="测试", kind="compatible", endpoint="http://localhost/tts", response_format="pcm")
    audio = await normalize_audio(b"\x00\x01" * 100, profile)
    with wave.open(io.BytesIO(audio.data)) as stream:
        assert stream.getframerate() == 24000
        assert stream.getnframes() == 100


async def test_mimo_clone_uses_reference_audio_not_a_speech_payload(server, wav_audio):
    async def handle(request):
        if request.method == "GET":
            assert "Authorization" not in request.headers
            return web.Response(body=wav_audio)
        body = await request.json()
        assert base64.b64decode(body["audio"]["voice"], validate=True) == wav_audio
        assert body["messages"][0] == {"role": "user", "content": "温柔地说"}
        assert body["messages"][1]["role"] == "assistant"
        return web.json_response({"choices": [{"message": {"audio": {"data": base64.b64encode(wav_audio).decode()}}}]})

    server.handler = handle
    p = Profile(
        id="mimo",
        name="MiMo",
        kind="mimo",
        endpoint=server.base,
        api_key="key",
        model="mimo-v2.5-tts-voiceclone",
        response_format="wav",
        instructions="温柔地说",
        options={"reference_url": server.base + "/sample.wav"},
    )
    transport = Transport(RuntimeSection())
    try:
        assert (await PROVIDERS["mimo"](transport).synthesize(p, "你好")).data == wav_audio
    finally:
        await transport.close()
