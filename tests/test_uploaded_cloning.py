"""本地录音的上传协议、授权字段及火山训练状态。"""

from aiohttp import web

import base64
import json
import logging

import pytest

from tts_under_test.domain.models import Profile, TTSError
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.infrastructure.transport import Transport
from tts_under_test.providers.openai import OpenAIProvider
from tts_under_test.providers.volcengine import VolcengineProvider
from tts_under_test.services.studio import Studio
from tts_under_test.services.tts import TTSService
from tts_under_test.settings.config import RuntimeSection


@pytest.mark.parametrize("source", ["file", "url"])
async def test_openai_multipart_reference_and_consent(tmp_path, server, wav_audio, source):
    recording = tmp_path / "sample.wav"
    recording.write_bytes(wav_audio)
    received = []

    async def handle(request):
        if request.path == "/sample.wav":
            assert "Authorization" not in request.headers
            return web.Response(body=wav_audio)
        assert request.path == "/proxy/v1/audio/voices"
        assert request.headers["Authorization"] == "Bearer sample-key"
        assert request.headers["OpenAI-Project"] == "proj_test"
        reader = await request.multipart()
        parts = {}
        async for part in reader:
            if part.name == "audio_sample":
                assert part.filename == "sample.wav"
                assert part.headers["Content-Type"] == "audio/wav"
                parts[part.name] = bytes(await part.read())
            else:
                parts[part.name] = await part.text()
        received.append(parts)
        return web.json_response({"id": "voice_uploaded", "object": "audio.voice"})

    server.handler = handle
    transport = Transport(RuntimeSection(), tmp_path)
    profile = Profile(
        id="oa",
        name="OpenAI",
        kind="openai",
        base_url=server.base + "/proxy/v1",
        api_key="sample-key",
        model="gpt-4o-mini-tts",
        options={"reference_source": source, "voice_consent_id": "cons_test", "project": "proj_test"},
    )
    try:
        voice = await OpenAIProvider(transport).clone(
            profile, "温柔声音", "sample.wav" if source == "file" else server.base + "/sample.wav", ""
        )
        assert voice == "voice_uploaded"
        assert received == [
            {"type": "audio_sample", "name": "温柔声音", "consent": "cons_test", "audio_sample": wav_audio}
        ]
        profile.options.voice_consent_id = ""
        with pytest.raises(TTSError, match="授权记录 ID"):
            await OpenAIProvider(transport).clone(profile, "声音", "missing.wav", "")
        assert len(received) == 1
    finally:
        await transport.close()


@pytest.mark.parametrize("billing", ["prepaid", "postpaid"])
@pytest.mark.parametrize("auth_mode", ["api_key", "access_token"])
async def test_volcengine_upload_training_query_and_voice_resource(tmp_path, server, wav_audio, billing, auth_mode):
    library = tmp_path / "reference_audio"
    library.mkdir()
    (library / "sample.wav").write_bytes(wav_audio)
    voice_id = "S_123456" if billing == "prepaid" else "custom_maimai"
    paths = []
    status = 1

    async def handle(request):
        paths.append(request.path)
        body = await request.json()
        if request.path.endswith("unidirectional"):
            assert request.headers["X-Api-Resource-Id"] == "seed-icl-2.0"
            assert body["req_params"]["speaker"] == voice_id
            assert body["req_params"]["model"] == "seed-tts-2.0-standard"
            return web.Response(
                text=json.dumps({"code": 20000000, "data": base64.b64encode(wav_audio).decode()}) + "\n"
            )
        assert "X-Api-Resource-Id" not in request.headers
        if auth_mode == "access_token":
            assert request.headers["X-Api-App-Key"] == "app-test"
            assert "X-Api-App-Id" not in request.headers
            assert request.headers["X-Api-Access-Key"] == "sample-key"
        else:
            assert request.headers["X-Api-Key"] == "sample-key"
        assert request.headers["X-Api-Request-Id"]
        if billing == "prepaid":
            assert body["speaker_id"] == voice_id
            assert "custom_speaker_id" not in body
        else:
            assert body["speaker_id"] == "custom_speaker_id"
            assert body["custom_speaker_id"] == voice_id
        if request.path.endswith("voice_clone"):
            assert body["audio"]["format"] == "wav"
            assert base64.b64decode(body["audio"]["data"]) == wav_audio
            assert body["text"] == "参考录音文字"
            assert body["language"] == 0
        else:
            assert request.path.endswith("tts/get_voice")
            assert "audio" not in body
        return web.json_response({"speaker_id": voice_id, "status": status})

    server.handler = handle
    store = ConfigStore(tmp_path)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    studio = Studio(store, service)
    profile = Profile(
        id="volc",
        name="火山",
        kind="volcengine",
        base_url=server.base + "/proxy/api/v3",
        api_key="sample-key",
        model="seed-tts-2.0-standard",
        voice="native-voice",
        options={
            "reference_source": "file",
            "clone_speaker_id": voice_id,
            "clone_billing": billing,
            "auth_mode": auth_mode,
            "app_id": "app-test",
        },
    )
    try:
        launched = studio.launch(
            "clone",
            profile.id,
            name="本地复刻",
            url="reference_audio/sample.wav",
            text="参考录音文字",
            draft=profile.model_dump_json(),
            model_id=profile.model,
        )
        await studio.jobs[launched["job_id"]].task
        job = studio.job(launched["job_id"])
        assert job["state"] == "done", job["error"]
        assert job["result"] == {"voice_id": voice_id, "name": "本地复刻", "state": "training"}
        profile = Profile.model_validate(
            {
                **profile.model_dump(),
                "voice": voice_id,
                "voices": [
                    {
                        "id": voice_id,
                        "name": "本地复刻",
                        "model": profile.model,
                        "resource_id": "seed-icl-2.0",
                        "clone_state": "training",
                    }
                ],
            }
        )
        with pytest.raises(TTSError, match="正在训练"):
            profile.with_selected_voice()
        status = 2
        # Querying the saved voice works even if the form currently has another billing selection.
        profile.options.clone_billing = "postpaid" if billing == "prepaid" else "prepaid"
        launched = studio.launch("clone_status", profile.id, url=voice_id, draft=profile.model_dump_json())
        await studio.jobs[launched["job_id"]].task
        job = studio.job(launched["job_id"])
        assert job["state"] == "done", job["error"]
        assert job["result"] == {"voice_id": voice_id, "state": "ready"}
        profile.voices[0].clone_state = "ready"
        selected = profile.with_selected_voice()
        assert selected.options.resource_id == "seed-icl-2.0"
        assert profile.options.resource_id == "seed-tts-2.0"
        await service.provider(selected).synthesize(selected, "你好")
        assert paths == [
            "/proxy/api/v3/tts/voice_clone",
            "/proxy/api/v3/tts/get_voice",
            "/proxy/api/v3/tts/unidirectional",
        ]
        assert store.config.profiles == []
        assert not store.path.exists()
    finally:
        await studio.close()
        await service.close()


@pytest.mark.parametrize("status", [0, 3, 999, None])
async def test_volcengine_training_failure_is_not_ready(server, status):
    async def handle(request):
        return web.json_response({"speaker_id": "S_test", "status": status})

    server.handler = handle
    transport = Transport(RuntimeSection())
    profile = Profile(id="v", name="火山", kind="volcengine", base_url=server.base + "/api/v3")
    try:
        if status == 3:
            result = await VolcengineProvider(transport).clone_status(profile, "S_test")
            assert result.state == "failed"
        else:
            with pytest.raises(TTSError):
                await VolcengineProvider(transport).clone_status(profile, "S_test")
    finally:
        await transport.close()
