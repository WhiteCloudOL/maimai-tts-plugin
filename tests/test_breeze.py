"""Breeze 自托管与在线协议、可选鉴权、录音、目录和音色创建。"""

from aiohttp import web

import asyncio
import base64
import logging
import pytest

from tts_under_test.domain.models import Profile, TTSError
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.infrastructure.transport import Transport
from tts_under_test.providers.breeze import BreezeProvider
from tts_under_test.services.cache_keys import CacheKeys
from tts_under_test.services.tts import TTSService
from tts_under_test.settings.config import RuntimeSection


def profile_for(server, *, cloud=False, key="", options=None, **extra):
    return Profile.model_validate(
        {
            "id": "breeze",
            "name": "BreezeTTS",
            "kind": "breeze",
            "base_url": server.base + "/v1",
            "api_key": key,
            "model": "breeze-tts-2",
            "voice": "voc_test" if cloud else "reference",
            "response_format": "wav",
            "options": {
                "breeze_api_mode": "cloud" if cloud else "self_hosted",
                "reference_source": "file",
                "reference_file": "reference_audio/sample.wav",
                "reference_text": "录音文字",
                **(options or {}),
            },
            **extra,
        }
    )


@pytest.mark.parametrize("key", ["", "optional-test-key"])
@pytest.mark.parametrize("source", ["file", "url"])
async def test_local_multipart_reference_and_optional_bearer(tmp_path, server, wav_audio, key, source):
    (tmp_path / "reference_audio").mkdir()
    (tmp_path / "reference_audio/sample.wav").write_bytes(wav_audio)

    async def handle(request):
        if request.path == "/sample":
            assert "Authorization" not in request.headers
            return web.Response(body=wav_audio)
        assert request.path == "/v1/audio/speech"
        assert request.headers.get("Authorization") == ("Bearer " + key if key else None)
        fields = {}
        async for part in await request.multipart():
            fields[part.name] = bytes(await part.read())
        assert fields == {
            "text": "你好".encode(),
            "cfg_scale": b"4.0",
            "seed": b"43",
            "instruction": "温柔地说话".encode(),
            "ref_audio": wav_audio,
            "ref_text": "录音文字".encode(),
        }
        return web.Response(
            body=wav_audio[44:],
            content_type="audio/pcm",
            headers={"X-Sample-Rate": "24000", "X-Sample-Format": "s16le"},
        )

    server.handler = handle
    profile = profile_for(
        server,
        key=key,
        instructions="温柔地说话",
        options={
            "reference_source": source,
            "reference_url": server.base + "/sample",
            "seed": 43,
            "breeze_guidance_scale": 4,
        },
    )
    transport = Transport(RuntimeSection(), tmp_path)
    try:
        audio = await BreezeProvider(transport).synthesize(profile, "你好")
        assert audio.data == wav_audio and audio.response_format == "wav"
    finally:
        await transport.close()


@pytest.mark.parametrize("mode", ["design", "plain"])
async def test_local_reference_free_modes(tmp_path, server, wav_audio, mode):
    async def handle(request):
        values = dict(await request.post())
        assert "ref_audio" not in values and "ref_text" not in values
        assert ("instruction" in values) == (mode == "design")
        return web.Response(body=wav_audio[44:], content_type="audio/pcm")

    server.handler = handle
    profile = profile_for(
        server, instructions="声音温柔清晰" if mode == "design" else "", options={"breeze_mode": mode}
    )
    transport = Transport(RuntimeSection(), tmp_path)
    try:
        assert (await BreezeProvider(transport).synthesize(profile, "你好")).data == wav_audio
    finally:
        await transport.close()


async def test_local_parameter_errors_are_reported_before_request(tmp_path, server):
    transport = Transport(RuntimeSection(), tmp_path)
    provider = BreezeProvider(transport)
    try:
        profile = profile_for(server, options={"breeze_guidance_scale": 4})
        with pytest.raises(TTSError, match="CFG=1"):
            await provider.synthesize(profile, "你好")
        profile = profile_for(server, options={"reference_text": ""})
        with pytest.raises(TTSError, match="文字稿"):
            await provider.synthesize(profile, "你好")
        profile = profile_for(server, options={"breeze_mode": "design"})
        with pytest.raises(TTSError, match="声音或风格描述"):
            await provider.synthesize(profile, "你好")
    finally:
        await transport.close()


async def test_local_single_instance_queues_requests_and_reference_cache_is_bypassed(tmp_path, server, wav_audio):
    active, maximum = 0, 0
    (tmp_path / "reference_audio").mkdir()
    (tmp_path / "reference_audio/sample.wav").write_bytes(wav_audio)

    async def handle(request):
        nonlocal active, maximum
        await request.post()
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return web.Response(body=wav_audio[44:], content_type="audio/pcm")

    server.handler = handle
    transport = Transport(RuntimeSection(), tmp_path)
    profile = profile_for(server)
    try:
        assert CacheKeys().speech(profile, "你好") is None
        await asyncio.gather(*(BreezeProvider(transport).synthesize(profile, "你好") for _ in range(2)))
        assert maximum == 1
        assert CacheKeys().speech(profile_for(server, options={"breeze_mode": "plain"}), "你好") is not None
    finally:
        await transport.close()


@pytest.mark.parametrize("mode", ["sync", "stream", "async"])
@pytest.mark.parametrize("key", ["", "optional-cloud-key"])
async def test_cloud_speech_delivery_modes_and_optional_bearer(server, wav_audio, mode, key):
    async def handle(request):
        assert request.headers.get("Authorization") == ("Bearer " + key if key else None)
        assert "xi-api-key" not in request.headers
        if request.method == "POST":
            assert request.path == "/v1/text-to-speech/voc_test" + ("/stream" if mode == "stream" else "")
            assert request.query["output_format"] == "wav"
            body = await request.json()
            assert body == {
                "text": "你好",
                "model_id": "breeze-tts-2",
                "language_code": "zh",
                "instructions": "温柔地说话",
                "voice_settings": {"guidance_scale": 4, "speed": 1.25, "volume": 0.8, "use_speaker_boost": True},
            }
            if mode == "async":
                return web.json_response({"generation_job_id": "job_test", "status": "queued"}, status=202)
            return web.Response(body=wav_audio, content_type="audio/wav")
        if request.path == "/v1/generation-jobs/job_test":
            return web.json_response(
                {"generation_job_id": "job_test", "status": "ready", "download_url": "http://untrusted.invalid/unused"}
            )
        assert request.path == "/v1/generation-jobs/job_test/audio"
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    profile = profile_for(
        server,
        cloud=True,
        key=key,
        speed=1.25,
        instructions="温柔地说话",
        options={
            "breeze_transport": mode,
            "breeze_language_code": "zh",
            "breeze_guidance_scale": 4,
            "breeze_volume": 0.8,
            "breeze_speaker_boost": True,
        },
    )
    transport = Transport(RuntimeSection())
    try:
        assert (await BreezeProvider(transport).synthesize(profile, "你好")).data == wav_audio
    finally:
        await transport.close()


async def test_cloud_models_array_and_paginated_voice_directory(server):
    async def handle(request):
        if request.path.endswith("/models"):
            return web.json_response([{"model_id": "breeze-tts-2"}, {"model_id": "breeze-tts-2-multilingual"}])
        assert request.query["voice_type"] == "personal"
        if request.query.get("next_page_token") == "page-two":
            return web.json_response(
                {"voices": [{"voice_id": "voc_design", "name": "设计音色", "origin": "designed"}], "has_more": False}
            )
        return web.json_response(
            {
                "voices": [{"voice_id": "voc_clone", "name": "克隆音色", "origin": "cloned"}],
                "has_more": True,
                "next_page_token": "page-two",
            }
        )

    server.handler = handle
    profile = profile_for(server, cloud=True, options={"breeze_voice_type": "personal"})
    transport = Transport(RuntimeSection())
    provider = BreezeProvider(transport)
    try:
        assert await provider.models(profile) == ["breeze-tts-2", "breeze-tts-2-multilingual"]
        voices = await provider.voices(profile)
        assert [(v["id"], v["origin"], v["model"]) for v in voices] == [
            ("voc_clone", "clone", ""),
            ("voc_design", "design", ""),
        ]
    finally:
        await transport.close()


@pytest.mark.parametrize("operation", ["clone", "design"])
async def test_cloud_voice_preview_is_saved_before_use(tmp_path, server, wav_audio, operation):
    actions = []
    (tmp_path / "reference_audio").mkdir()
    (tmp_path / "reference_audio/sample.wav").write_bytes(wav_audio)

    async def handle(request):
        actions.append((request.method, request.path))
        assert request.headers["Authorization"] == "Bearer test-key"
        if request.path == "/v1/voice-previews/clone":
            fields = {}
            async for part in await request.multipart():
                fields[part.name] = bytes(await part.read())
            assert fields["files"] == wav_audio and fields["name"].decode() == "我的声音"
            assert "ref_text" not in fields
            return web.json_response({"generated_voice_id": "gvi_test", "requires_verification": False})
        if request.path == "/v1/voice-previews/design":
            body = await request.json()
            assert body["voice_description"] == "清晰温柔的年轻女声"
            assert body["preview_count"] == 1 and body["language_code"] == "zh"
            return web.json_response(
                {
                    "previews": [
                        {
                            "generated_voice_id": "gvi_test",
                            "audio_base_64": base64.b64encode(wav_audio).decode(),
                            "media_type": "audio/wav",
                        }
                    ]
                }
            )
        if request.path == "/v1/voice-previews/gvi_test/stream":
            return web.Response(body=wav_audio, content_type="audio/wav")
        if request.path.endswith("/save"):
            assert await request.json() == {"voice_name": "我的声音", "language_code": "zh"}
            return web.json_response({"voice_id": "voc_saved", "name": "我的声音"})
        assert request.method == "DELETE" and request.path == "/v1/voices/voc_saved"
        return web.json_response({"status": "ok"})

    server.handler = handle
    profile = profile_for(
        server,
        cloud=True,
        key="test-key",
        options={"voice_prompt": "清晰温柔的年轻女声", "design_preview_text": "晚上好呀，我是麦麦。"},
    )
    transport = Transport(RuntimeSection(), tmp_path)
    provider = BreezeProvider(transport)
    try:
        result = (
            await provider.clone(profile, "我的声音", "reference_audio/sample.wav", "")
            if operation == "clone"
            else await provider.design(profile, "我的声音")
        )
        assert result.voice_id == "voc_saved" and result.state == "ready" and result.preview.data == wav_audio
        assert actions[-1] == ("POST", "/v1/voice-previews/gvi_test/save")
        await provider.delete_voice(profile, result.voice_id)
    finally:
        await transport.close()


async def test_dynamic_tone_is_removed_before_local_multipart_and_reference_is_model_scoped(
    tmp_path, server, wav_audio
):
    (tmp_path / "reference_audio").mkdir()
    (tmp_path / "reference_audio/sample.wav").write_bytes(wav_audio)

    async def handle(request):
        data = dict(await request.post())
        assert data["text"] == "你好" and "开心" in data["instruction"]
        assert data["ref_text"] == "准确的参考文字"
        return web.Response(body=wav_audio[44:], content_type="audio/pcm")

    server.handler = handle
    store = ConfigStore(tmp_path)
    profile = profile_for(
        server,
        options={"breeze_guidance_scale": 4},
        tone={"mode": "dynamic", "emotion": "neutral", "allowed": ["neutral", "happy"]},
        voices=[
            {
                "id": "reference",
                "name": "参考录音",
                "model": "breeze-tts-2",
                "reference": {"source": "file", "path": "reference_audio/sample.wav", "text": "准确的参考文字"},
            },
        ],
    )
    await store.save(
        {"profiles": [profile.model_dump()], "primary": {"provider_id": "breeze", "model_id": profile.model}}, 0
    )
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    try:
        audio = await service.synthesize("[mmtts:happy]你好")
        assert audio.data == wav_audio
        with pytest.raises(TTSError, match="未获当前模型"):
            await service.synthesize("[mmtts:sad]你好")
    finally:
        await service.close()


async def test_cloud_verification_preview_is_not_misreported_as_ready(tmp_path, server, wav_audio):
    (tmp_path / "reference_audio").mkdir()
    (tmp_path / "reference_audio/sample.wav").write_bytes(wav_audio)

    async def handle(request):
        assert request.path == "/v1/voice-previews/clone"
        return web.json_response({"generated_voice_id": "gvi_needs_verification", "requires_verification": True})

    server.handler = handle
    transport = Transport(RuntimeSection(), tmp_path)
    try:
        with pytest.raises(TTSError, match="官方控制台完成验证"):
            await BreezeProvider(transport).clone(
                profile_for(server, cloud=True), "名称", "reference_audio/sample.wav", ""
            )
    finally:
        await transport.close()
