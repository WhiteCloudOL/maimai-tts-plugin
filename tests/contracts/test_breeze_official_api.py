"""按官方 OpenAPI 核对合成字段、音色路径与错误传播。

契约来源：https://docs.breezeblue.ai/openapi.json
鉴权说明：https://docs.breezeblue.ai/authentication
"""

from aiohttp import web

import base64
import pytest

from maimai_tts_contract.domain.models import Profile, TTSError
from maimai_tts_contract.providers.breeze_cloud import BreezeCloudProvider
from maimai_tts_contract.services.studio import Studio
from test_prompt_pipeline import wav


pytestmark = pytest.mark.asyncio
AUDIO = b"ID3official-contract"


@pytest.mark.parametrize("transport", ["sync", "stream", "async"])
@pytest.mark.parametrize("voice", ["voc_saved", "a2b76355-59c5-4ec4-98cf-6c523ce937fb", "opaque.id+value"])
async def test_official_synthesis_parameters_and_opaque_voice_ids(serve, service, transport, voice):
    calls = []

    async def handler(request):
        calls.append((request.method, request.path))
        assert request.headers["Authorization"] == "Bearer contract-secret"
        if request.method == "POST":
            assert request.path == "/v1/text-to-speech/" + voice + ("/stream" if transport == "stream" else "")
            assert dict(request.query) == {"output_format": "mp3", **({"delivery": transport} if transport != "stream" else {})}
            assert await request.json() == {
                "text": "官方请求测试。", "model_id": "breeze-tts-2", "language_code": "zh", "instructions": "自然轻快",
                "voice_settings": {"guidance_scale": 4.0, "speed": 1.2, "volume": 0.8, "use_speaker_boost": True},
            }
            if transport == "async":
                return web.json_response({"generation_job_id": "job_contract"}, status=202)
        elif request.path == "/v1/generation-jobs/job_contract":
            return web.json_response({"generation_job_id": "job_contract", "status": "ready"})
        else:
            assert request.path == "/v1/generation-jobs/job_contract/audio"
        return web.Response(body=AUDIO, content_type="audio/mpeg")

    selected = Profile(
        id="official", name="Breeze 官方", kind="breeze", base_url=(await serve(handler)) + "/v1", api_key="contract-secret",
        model="breeze-tts-2", voice=voice, speed=1.2, instructions="自然轻快",
        options={"breeze_api_mode": "cloud", "breeze_mode": "plain", "breeze_transport": transport,
                 "breeze_language_code": "zh", "breeze_guidance_scale": 4, "breeze_volume": 0.8, "breeze_speaker_boost": True},
    )
    assert (await BreezeCloudProvider(service.http).synthesize(selected, "官方请求测试。")).data == AUDIO
    assert len(calls) == (3 if transport == "async" else 1)


@pytest.mark.parametrize("padding", ["\ufeff voc_saved\u200b", "\t voc_saved\r\n", "\u3000voc_saved\u3000"])
async def test_copied_voice_id_padding_is_not_sent_to_the_official_api(serve, service, padding):
    calls = []

    async def handler(request):
        calls.append(request.path)
        assert request.path == "/v1/text-to-speech/voc_saved"
        return web.Response(body=AUDIO, content_type="audio/mpeg")

    selected = Profile(
        id="official", name="Breeze 官方", kind="breeze", base_url=(await serve(handler)) + "/v1", voice=padding,
        options={"breeze_api_mode": "cloud", "breeze_mode": "plain"},
    )
    before = selected.model_dump()
    assert (await BreezeCloudProvider(service.http).synthesize(selected, "你好。")).data == AUDIO
    assert selected.model_dump() == before and len(calls) == 1


async def test_invisible_empty_voice_is_rejected_before_http(serve, service):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.Response(body=AUDIO, content_type="audio/mpeg")

    selected = Profile(
        id="official", name="Breeze 官方", kind="breeze", base_url=(await serve(handler)) + "/v1", voice="\ufeff \t\u200b\r\n",
        options={"breeze_api_mode": "cloud", "breeze_mode": "plain"},
    )
    with pytest.raises(TTSError, match="未选中音色"):
        await BreezeCloudProvider(service.http).synthesize(selected, "你好。")
    assert calls == []


async def test_official_voice_not_found_is_not_retried_or_replaced(serve, service):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.json_response({"ok": False, "code": "RESOURCE_NOT_FOUND", "detail": "Voice not found.", "error": "Voice not found."}, status=404)

    selected = Profile(
        id="official", name="Breeze 官方", kind="breeze", base_url=(await serve(handler)) + "/v1", api_key="contract-secret",
        voice="missing-saved-voice", options={"breeze_api_mode": "cloud", "breeze_mode": "plain"},
    )
    with pytest.raises(TTSError) as failure:
        await BreezeCloudProvider(service.http).synthesize(selected, "你好。")
    assert "404" in str(failure.value) and "RESOURCE_NOT_FOUND" in str(failure.value)
    assert "Voice not found" in str(failure.value) and "contract-secret" not in str(failure.value)
    assert "voice_id='missing-saved-voice'" in str(failure.value)
    assert calls == ["/v1/text-to-speech/missing-saved-voice"]


@pytest.mark.parametrize("voice", ["voc_saved", "a2b76355-59c5-4ec4-98cf-6c523ce937fb", "opaque.id+value"])
async def test_import_checks_the_exact_voice_with_current_account(serve, service, voice):
    calls = []

    async def handler(request):
        calls.append((request.method, request.path))
        assert request.headers["Authorization"] == "Bearer contract-secret"
        return web.json_response({"voice_id": voice, "name": "官方声音", "origin": "designed"})

    selected = Profile(id="official", name="Breeze 官方", kind="breeze", base_url=(await serve(handler)) + "/v1",
                       api_key="contract-secret", options={"breeze_api_mode": "cloud"})
    result = await BreezeCloudProvider(service.http).voice_info(selected, "\ufeff " + voice + "\u200b")
    assert result == {"id": voice, "name": "官方声音", "model": "", "origin": "design"}
    assert calls == [("GET", "/v1/voices/" + voice)]


async def test_import_checks_are_fresh_and_do_not_synthesize_or_change_config(serve, service):
    calls = []

    async def handler(request):
        calls.append((request.method, request.path))
        assert request.method == "GET" and request.path == "/v1/voices/voc_saved"
        if len(calls) == 1:
            return web.json_response({"voice_id": "voc_saved", "name": "官方声音", "origin": "cloned"})
        return web.json_response({"code": "RESOURCE_NOT_FOUND", "detail": "Voice not found."}, status=404)

    selected = Profile(id="official", name="Breeze 官方", kind="breeze", base_url=(await serve(handler)) + "/v1",
                       api_key="contract-secret", options={"breeze_api_mode": "cloud"})
    before = service.store.snapshot()
    studio = Studio(service.store, service)
    try:
        for expected in ("done", "error"):
            job = studio.launch("voice_info", selected.id, voice_id="voc_saved", draft=selected.model_dump_json())
            await studio.jobs[job["job_id"]].task
            result = studio.job(job["job_id"])
            assert result["state"] == expected
            if expected == "done":
                assert result["result"]["id"] == "voc_saved"
            else:
                assert "voc_saved" in result["error"] and "404" in result["error"]
                assert "contract-secret" not in result["error"]
        assert service.store.snapshot() == before and len(calls) == 2
    finally:
        await studio.close()


async def test_import_rejects_mismatched_voice_identity(serve, service):
    async def handler(request):
        return web.json_response({"voice_id": "another-voice", "name": "错误音色"})

    selected = Profile(id="official", name="Breeze 官方", kind="breeze", base_url=(await serve(handler)) + "/v1",
                       options={"breeze_api_mode": "cloud"})
    with pytest.raises(TTSError, match="匹配的 voice_id"):
        await BreezeCloudProvider(service.http).voice_info(selected, "voc_requested")


@pytest.mark.parametrize("operation", ["clone", "design"])
async def test_created_previews_are_saved_and_synthesis_uses_final_voice_id(serve, service, operation):
    calls = []
    audio = wav()

    async def handler(request):
        calls.append((request.method, request.path))
        if request.path == "/reference.wav":
            assert "Authorization" not in request.headers
            return web.Response(body=audio, content_type="audio/wav")
        assert request.headers["Authorization"] == "Bearer contract-secret"
        if request.path == "/v1/voice-previews/clone":
            form = await request.post()
            assert form["name"] == "测试音色" and form["reference_language_code"] == "zh"
            assert form["files"].file.read() == audio
            form["files"].file.close()
            return web.json_response({"generated_voice_id": "gvi_temporary", "requires_verification": False})
        if request.path == "/v1/voice-previews/design":
            body = await request.json()
            assert body["voice_description"] == "自然明亮的年轻女声" and body["preview_count"] == 1
            return web.json_response({"previews": [{"generated_voice_id": "gvi_temporary", "audio_base_64": base64.b64encode(audio).decode()}]})
        if request.path == "/v1/voice-previews/gvi_temporary/stream":
            return web.Response(body=audio, content_type="audio/wav")
        if request.path == "/v1/voice-previews/gvi_temporary/save":
            assert await request.json() == {"voice_name": "测试音色", "language_code": "zh"}
            return web.json_response({"voice_id": "voc_final", "name": "测试音色"})
        assert request.path == "/v1/text-to-speech/voc_final"
        return web.Response(body=audio, content_type="audio/wav")

    base = await serve(handler)
    selected = Profile(id="official", name="Breeze 官方", kind="breeze", base_url=base + "/v1", api_key="contract-secret",
                       model="breeze-tts-2", voice="old-default", response_format="wav",
                       options={"breeze_api_mode": "cloud", "reference_source": "url", "voice_prompt": "自然明亮的年轻女声",
                                "design_preview_text": "你好，这是设计试听。"})
    provider = BreezeCloudProvider(service.http)
    result = await provider.design(selected, "测试音色") if operation == "design" else await provider.clone(selected, "测试音色", base + "/reference.wav", "")
    assert result.voice_id == "voc_final" and selected.voice == "old-default"
    await provider.synthesize(selected.model_copy(update={"voice": result.voice_id}), "你好。")
    assert calls[-2:] == [("POST", "/v1/voice-previews/gvi_temporary/save"), ("POST", "/v1/text-to-speech/voc_final")]
