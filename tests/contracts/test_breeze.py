"""Breeze 官方目录筛选、缓存和后台合成状态的离线契约。"""

from aiohttp import web
from typing import Any

import pytest

from maimai_tts_contract.domain.models import Profile, RouteTarget, StudioConfig, TTSError
from maimai_tts_contract.providers.breeze_cloud import BreezeCloudProvider


pytestmark = pytest.mark.asyncio
AUDIO = b"ID3breeze-contract-audio"


def profile(base_url: str, **options: Any) -> Profile:
    return Profile(
        id="breeze-contract", name="Breeze 协议测试", kind="breeze", base_url=base_url + "/v1",
        model="breeze-tts-2", voice="test-voice", api_key="test-only-not-a-real-key",
        options={"breeze_api_mode": "cloud", "breeze_mode": "plain", **options},
    )


@pytest.mark.parametrize("pending", ["queued", "pending", "running", "processing", "streaming"])
async def test_background_job_waits_for_ready(serve, service, pending):
    polls = []
    calls = []

    async def handler(request):
        calls.append((request.method, request.path, dict(request.query)))
        if request.method == "POST":
            return web.json_response({"generation_job_id": "job_test"}, status=202)
        if request.path.endswith("/audio"):
            return web.Response(body=AUDIO, content_type="audio/mpeg")
        polls.append(request.path)
        return web.json_response({"generation_job_id": "job_test", "status": pending if len(polls) == 1 else "ready"})

    selected = profile(await serve(handler), breeze_transport="async")
    audio = await BreezeCloudProvider(service.http).synthesize(selected, "后台任务测试。")
    assert audio.data == AUDIO
    assert len(polls) == 2
    assert calls[0] == ("POST", "/v1/text-to-speech/test-voice", {"output_format": "mp3", "delivery": "async"})
    assert calls[-1] == ("GET", "/v1/generation-jobs/job_test/audio", {"output_format": "mp3"})


@pytest.mark.parametrize("state", ["failed", "cancelled", "unrecognized"])
async def test_background_job_rejects_failure_or_unknown_state(serve, service, state):
    async def handler(request):
        if request.method == "POST":
            return web.json_response({"generation_job_id": "job_test"}, status=202)
        return web.json_response({"generation_job_id": "job_test", "status": state})

    selected = profile(await serve(handler), breeze_transport="async")
    with pytest.raises(TTSError, match=state):
        await BreezeCloudProvider(service.http).synthesize(selected, "后台任务测试。")


async def test_voice_search_and_language_are_preserved_across_pages(serve, service):
    queries = []

    async def handler(request):
        queries.append(dict(request.query))
        second = "next_page_token" in request.query
        return web.json_response({
            "voices": [{"voice_id": "voice-second" if second else "voice-first", "name": "测试音色"}],
            "total": 2, "has_more": not second, "next_page_token": None if second else "cursor-2",
        })

    selected = profile(await serve(handler), breeze_voice_type="default", breeze_voice_search="  温柔  ")
    voices = await BreezeCloudProvider(service.http).voices(selected)
    common = {"page_size": "100", "voice_type": "default", "language_code": "zh", "search": "温柔"}
    assert queries == [{**common, "page": "1"}, {**common, "next_page_token": "cursor-2"}]
    assert [voice["id"] for voice in voices] == ["voice-first", "voice-second"]


async def test_unfiltered_catalog_can_explicitly_omit_language_and_search(serve, service):
    queries = []

    async def handler(request):
        queries.append(dict(request.query))
        return web.json_response({"voices": [], "total": 0, "has_more": False})

    selected = profile(await serve(handler), breeze_voice_filter_language="", breeze_voice_search="  ")
    assert await BreezeCloudProvider(service.http).voices(selected) == []
    assert queries == [{"page_size": "100", "voice_type": "all", "page": "1"}]


async def test_large_catalog_requests_refinement_after_first_page(serve, service):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.json_response({"voices": [], "total": 15572, "has_more": True})

    selected = profile(await serve(handler), breeze_voice_filter_language="")
    with pytest.raises(TTSError, match="同步音色语言"):
        await BreezeCloudProvider(service.http).voices(selected)
    assert len(calls) == 1


async def test_voice_filters_partition_only_the_voice_catalog_cache(serve, service):
    queries = []

    async def handler(request):
        queries.append(dict(request.query))
        language = request.query.get("language_code")
        return web.json_response({"voices": [{"voice_id": language, "name": language}], "total": 1, "has_more": False})

    selected = profile(await serve(handler))
    english = selected.model_copy(deep=True)
    english.options.breeze_voice_filter_language = "en"
    keyword = selected.model_copy(deep=True)
    keyword.options.breeze_voice_search = "轻声"
    first = await service.catalog_list(selected, "voices")
    second = await service.catalog_list(english, "voices")
    await service.catalog_list(keyword, "voices")
    assert await service.catalog_list(selected, "voices") == first
    assert first[0]["id"] == "zh" and second[0]["id"] == "en"
    assert len(queries) == 3
    client = BreezeCloudProvider(service.http)
    assert client.list_cache_parameters(selected, "models") == client.list_cache_parameters(english, "models")


async def test_changing_catalog_filters_reuses_identical_speech_audio(serve, service):
    calls = []

    async def handler(request):
        calls.append(await request.json())
        return web.Response(body=AUDIO, content_type="audio/mpeg")

    selected = profile(await serve(handler))

    def config(value: Profile) -> StudioConfig:
        return StudioConfig(profiles=[value], primary=RouteTarget(provider_id=value.id, model_id=value.model))

    first = await service.synthesize("相同正文。", config=config(selected))
    edited = selected.model_copy(deep=True)
    edited.options.breeze_voice_filter_language = "en"
    edited.options.breeze_voice_search = "新的目录关键词"
    second = await service.synthesize("相同正文。", config=config(edited))
    assert first.data == second.data == AUDIO
    assert len(calls) == 1
    assert "breeze_voice_search" not in str(calls)
