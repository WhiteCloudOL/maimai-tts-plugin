from aiohttp import web

import asyncio
import base64
import io
import json
import logging
import wave

import pytest

from tts_under_test.domain.models import Profile, TTSError
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.infrastructure.transport import Transport
from tts_under_test.providers.mimo import MiMoProvider
from tts_under_test.providers.qwen import QwenProvider
from tts_under_test.services.studio import Studio
from tts_under_test.services.tts import TTSService
from tts_under_test.settings.config import RuntimeSection


async def wait_job(studio, job_id):
    for _ in range(100):
        result = studio.job(job_id)
        if result["state"] != "running":
            return result
        await asyncio.sleep(0.01)
    pytest.fail("Local task did not finish")


async def test_platform_actions_accept_unsaved_drafts(tmp_path, server, wav_audio):
    async def handle(request):
        if request.method == "GET":
            return web.json_response({"data": [{"id": "draft-model"}]})
        body = await request.json()
        assert body["model"] == "draft-model"
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    store = ConfigStore(tmp_path)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    studio = Studio(store, service)
    profile = {
        "id": "new",
        "name": "未保存平台",
        "kind": "compatible",
        "endpoint": server.base + "/tts",
        "models_url": server.base + "/models",
        "model": "draft-model",
        "enabled": False,
    }
    try:
        task = studio.launch("models", "new", draft=json.dumps(profile))
        result = await wait_job(studio, task["job_id"])
        assert result["result"] == ["draft-model"]
        task = studio.launch("preview", "new", text="你好", draft=json.dumps(profile))
        result = await wait_job(studio, task["job_id"])
        assert result["state"] == "done"
        assert result["total"] == len(wav_audio)
        assert store.config.profiles == []
        assert not store.path.exists()
    finally:
        await studio.close()
        await service.close()


async def test_save_profile_does_not_commit_other_drafts(tmp_path):
    store = ConfigStore(tmp_path)
    data = {
        "profiles": [
            {"id": "first", "name": "甲", "kind": "compatible", "endpoint": "http://localhost/tts"},
            {"id": "second", "name": "乙", "kind": "compatible", "endpoint": "http://localhost/tts"},
        ],
        "default_provider": "first",
    }
    snapshot = await store.save(data, 0)
    edited = snapshot["profiles"][0]
    edited["name"] = "只保存甲"
    await store.save_profile(edited, 1)
    assert store.profile("first").name == "只保存甲"
    assert store.profile("second").name == "乙"
    assert store.config.feature.probability_enabled is False


@pytest.mark.parametrize("scope", ["platform", "model"])
@pytest.mark.parametrize("save_mode", ["draft", "profile", "all"])
async def test_base_url_changes_use_saved_key_for_preview(tmp_path, server, wav_audio, scope, save_mode):
    requests = []

    async def handle(request):
        assert request.path == "/v1/audio/speech"
        assert request.headers.get("Authorization") == "Bearer saved-test-key"
        body = await request.json()
        assert body["model"] == "draft-model"
        requests.append(request.path)
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    store = ConfigStore(tmp_path)
    snapshot = await store.save(
        {"profiles": [{"id": "saved", "name": "平台", "kind": "compatible",
                       "base_url": "https://original.test/v1", "model": "draft-model",
                       "api_key": "saved-test-key"}]}, 0,
    )
    draft = snapshot["profiles"][0]
    owner = draft if scope == "platform" else draft["models"][0]
    owner["base_url"] = server.base + "/v1"
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    studio = Studio(store, service)
    try:
        if save_mode == "profile":
            snapshot = await store.save_profile(draft, 1)
        elif save_mode == "all":
            snapshot = await store.save(snapshot, 1)
        task = studio.launch("preview", "saved", text="你好", draft=json.dumps(draft) if save_mode == "draft" else "")
        result = await wait_job(studio, task["job_id"])
        assert result["state"] == "done"
        assert result["total"] == len(wav_audio)
        assert requests == ["/v1/audio/speech"]
        assert store.profile("saved").api_key == "saved-test-key"
        assert "saved-test-key" not in json.dumps(store.snapshot())
        assert "api_key" not in snapshot["profiles"][0]
        if save_mode == "draft":
            assert store.config.revision == 1
            assert store.profile("saved").for_model("draft-model").base_url == "https://original.test/v1"
        else:
            restored = ConfigStore(tmp_path)
            await restored.load()
            assert restored.profile("saved").for_model("draft-model").base_url == server.base + "/v1"
            assert restored.profile("saved").api_key == "saved-test-key"
    finally:
        await studio.close()
        await service.close()


@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("address_mode", ["request", "base"])
async def test_qwen_clone_then_realtime_synthesis(server, wav_audio, complete, address_mode):
    model = "qwen3-tts-vc-realtime-2026-01-15"
    pcm = b"\x01\x00" * 400

    async def handle(request):
        if request.path == "/sample.wav":
            assert "Authorization" not in request.headers
            return web.Response(body=wav_audio)
        if request.path in {"/clone", "/proxy/api/v1/services/audio/tts/customization"}:
            body = await request.json()
            assert body["model"] == "qwen-voice-enrollment"
            assert body["input"]["action"] == "create"
            assert body["input"]["target_model"] == model
            assert body["input"]["audio"]["data"].startswith("data:audio/wav;base64,")
            return web.json_response({"output": {"voice": "qwen-tts-vc-unit-test"}})
        if address_mode == "base":
            assert request.path == "/proxy/api-ws/v1/realtime"
            assert request.query["model"] == model
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json({"type": "session.created", "session": {"id": "test-session"}})
        event = await socket.receive_json()
        assert event["type"] == "session.update"
        assert event["session"]["voice"] == "qwen-tts-vc-unit-test"
        assert event["session"]["response_format"] == "pcm"
        await socket.send_json({"type": "session.updated"})
        event = await socket.receive_json()
        assert event["type"] == "input_text_buffer.append"
        assert event["text"] == "你好"
        assert (await socket.receive_json())["type"] == "session.finish"
        await socket.send_json({"type": "response.audio.delta", "delta": base64.b64encode(pcm).decode()})
        if complete:
            await socket.send_json({"type": "session.finished"})
        else:
            await socket.close()
        return socket

    server.handler = handle
    profile = Profile(
        id="qwen",
        name="千问克隆",
        kind="qwen",
        api_key="test-key",
        model=model,
        endpoint=server.base.replace("http:", "ws:") + f"/realtime?model={model}",
        clone_url=server.base + "/clone",
        response_format="wav",
    )
    if address_mode == "base":
        profile = Profile.model_validate(
            {**profile.model_dump(), "endpoint": "", "base_url": server.base + "/proxy/api/v1", "clone_url": ""}
        )
    transport = Transport(RuntimeSection())
    try:
        provider = QwenProvider(transport)
        voice_id = await provider.clone(profile, "maimai", server.base + "/sample.wav", "")
        profile.voice = voice_id
        if not complete:
            with pytest.raises(TTSError, match="未完整结束"):
                await provider.synthesize(profile, "你好")
        else:
            audio = await provider.synthesize(profile, "你好")
            with wave.open(io.BytesIO(audio.data)) as reader:
                assert reader.getframerate() == 24000
                assert reader.readframes(400) == pcm
    finally:
        await transport.close()


@pytest.mark.parametrize("fmt", ["wav", "mp3", "pcm"])
async def test_mimo_reference_doc_design_formats(server, wav_audio, fmt):
    raw = wav_audio if fmt == "wav" else b"ID3example" if fmt == "mp3" else b"\0\0" * 200

    async def handle(request):
        assert request.headers["api-key"] == "test-key"
        body = await request.json()
        assert body["audio"]["format"] == fmt
        assert "voice" not in body["audio"]
        assert body["audio"]["optimize_text_preview"] is True
        assert body["messages"][0]["role"] == "user"
        return web.json_response(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "audio": {"data": base64.b64encode(raw).decode()},
                        },
                    }
                ]
            }
        )

    server.handler = handle
    profile = Profile(
        id="mimo",
        name="MiMo",
        kind="mimo",
        endpoint=server.base,
        api_key="test-key",
        model="mimo-v2.5-tts-voicedesign",
        response_format=fmt,
        instructions="温柔女声",
        options={"optimize_text_preview": True},
    )
    transport = Transport(RuntimeSection())
    try:
        audio = await MiMoProvider(transport).synthesize(profile, "你好")
        assert audio.response_format == ("wav" if fmt == "pcm" else fmt)
    finally:
        await transport.close()
