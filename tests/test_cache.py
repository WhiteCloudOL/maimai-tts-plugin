"""缓存身份、并发复用、取消、失效与音色变更的回归验证。"""

from aiohttp import web
from dataclasses import replace
from unittest.mock import patch

import asyncio
import logging

import pytest

from tts_under_test.domain.models import Audio, Profile, StudioConfig, TTSError
from tts_under_test.domain.voice_catalog import get_catalog
from tts_under_test.infrastructure.cache import AsyncResultCache
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.services.cache_keys import CacheKeys
from tts_under_test.services.studio import Studio
from tts_under_test.services.tts import TTSService
from tts_under_test.settings.config import RuntimeSection


async def test_lru_ttl_budget_refresh_and_mutable_results():
    now = [10.0]
    cache = AsyncResultCache(4, max_entries=2, clock=lambda: now[0])
    count = 0

    async def load():
        nonlocal count
        count += 1
        return [count]

    async def get(key, refresh=False):
        return await cache.get("list", key, load, ttl=5, size=lambda _: 2, refresh=refresh)

    try:
        first = await get("a")
        first.value.append(100)
        assert (await get("a")).value == [1]
        await get("b")
        await get("a")
        await get("c")
        assert cache.bytes == 4 and (await get("b")).source == "miss"
        assert (await get("b", True)).value == [5]
        now[0] += 6
        assert (await get("b")).source == "miss"
        assert count == 6 and cache.stats()["evictions"] > 0
        assert (await cache.get("x", "huge", load, ttl=5, size=lambda _: 10)).source == "miss"
        assert ("x", "huge") not in cache.entries
    finally:
        await cache.close()


async def test_shared_request_survives_one_cancel_and_cancels_without_waiters():
    cache = AsyncResultCache(100)
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0
    cancelled = asyncio.Event()

    async def load():
        nonlocal calls
        calls += 1
        started.set()
        try:
            await finish.wait()
            return b"audio"
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def get():
        return await cache.get("speech", "same", load, ttl=10, size=len)

    try:
        first = asyncio.create_task(get())
        await started.wait()
        second = asyncio.create_task(get())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not cancelled.is_set()
        finish.set()
        assert (await second).value == b"audio" and calls == 1
        assert cache.stats()["shared"] == 1
        cache.invalidate("speech")
        started.clear()
        finish.clear()
        last = asyncio.create_task(get())
        await started.wait()
        last.cancel()
        with pytest.raises(asyncio.CancelledError):
            await last
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not cache.pending
    finally:
        await cache.close()


async def test_mutation_during_query_never_restores_stale_cache():
    cache = AsyncResultCache(100)
    started, finish = asyncio.Event(), asyncio.Event()

    async def old():
        started.set()
        await finish.wait()
        return b"old"

    async def new():
        return b"new"

    try:
        query = asyncio.create_task(cache.get("voices", "account", old, ttl=10, size=len))
        await started.wait()
        cache.invalidate("voices")
        assert (await cache.get("voices", "account", new, ttl=10, size=len)).value == b"new"
        finish.set()
        assert (await query).value == b"old"
        assert (await cache.get("voices", "account", new, ttl=10, size=len)).value == b"new"
    finally:
        await cache.close()


async def test_failure_is_not_cached_and_close_releases_pending_work():
    cache = AsyncResultCache(100)
    count = 0

    async def fail():
        nonlocal count
        count += 1
        raise TTSError("upstream error")

    for _ in range(2):
        with pytest.raises(TTSError):
            await cache.get("voices", "same", fail, ttl=10, size=len)
    assert count == 2 and not cache.entries
    pending = asyncio.create_task(cache.get("speech", "wait", lambda: asyncio.sleep(100), ttl=10, size=lambda _: 1))
    await asyncio.sleep(0)
    await cache.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not cache.tasks and not cache.entries


def profile(server, **values):
    return Profile(
        id="p",
        name="平台",
        kind="compatible",
        base_url=server.base + "/v1",
        model="tts-1",
        voice="alloy",
        response_format="wav",
        api_key="private-key",
        **values,
    )


async def test_list_cache_keys_share_accounts_but_respect_auth_endpoint_and_mapping(tmp_path, server):
    calls = []

    async def handle(request):
        calls.append((request.path, request.headers.get("Authorization")))
        return web.json_response({"data": [{"id": "tts-1"}], "items": [{"key": "new-model"}]})

    server.handler = handle
    service = TTSService(ConfigStore(tmp_path), RuntimeSection(), logging.getLogger("test"))
    p = profile(server)
    try:
        assert await service.catalog_list(p, "models") == ["tts-1"]
        renamed = p.model_copy(update={"id": "renamed", "name": "另一个名称", "voice": "nova", "model": "tts-1-hd"})
        assert await service.catalog_list(renamed, "models") == ["tts-1"] and len(calls) == 1
        await service.catalog_list(p, "models", refresh=True)
        changed = p.model_copy(update={"api_key": "another-key"})
        await service.catalog_list(changed, "models")
        moved = p.model_copy(update={"models_url": server.base + "/other-models"})
        await service.catalog_list(moved, "models")
        custom = p.model_copy(deep=True)
        custom.options.compatibility = {"mode": "custom", "models_pointer": "/items", "model_id_pointer": "/key"}
        custom = Profile.model_validate(custom.model_dump())
        assert await service.catalog_list(custom, "models") == ["new-model"]
        assert len(calls) == 5
        assert "private-key" not in repr(service.cache.entries)
    finally:
        await service.close()


async def test_synthesis_cache_reuses_resolved_voice_but_respects_model_tone_and_credentials(
    tmp_path, server, wav_audio
):
    calls = []

    async def handle(request):
        calls.append(await request.json())
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    service = TTSService(ConfigStore(tmp_path), RuntimeSection(), logging.getLogger("test"))
    p = profile(server)

    async def synth(p, **kwargs):
        return await service.synthesize(
            "你好", config=StudioConfig(profiles=[p], primary={"provider_id": p.id, "model_id": p.model}), **kwargs
        )

    try:
        first = await synth(p)
        second = await synth(p.model_copy(update={"id": "other", "name": "重命名"}))
        assert first.data == second.data and second.provider_id == "other" and len(calls) == 1
        await synth(p, refresh_cache=True)
        await synth(p.model_copy(update={"voice": "nova"}))
        await synth(p.model_copy(update={"api_key": "other-key"}))
        assert len(calls) == 4
        service.invalidate_voices(p)
        await synth(p)
        assert len(calls) == 5
        with patch(
            "tts_under_test.services.tts.encode_silk", return_value=Audio(b"silk", "audio/silk", "silk")
        ) as codec:
            a = await service.prepare_delivery(first, "silk")
            b = await service.prepare_delivery(replace(first, provider_id="other", request_id="new-request"), "silk")
            assert (
                codec.call_count == 1
                and a.data == b.data
                and b.provider_id == "other"
                and b.request_id == "new-request"
            )
    finally:
        await service.close()


def test_reference_sources_bypass_speech_cache(server):
    keys = CacheKeys()
    p = profile(server)
    p.kind = "gpt_sovits"
    assert keys.speech(p, "你好") is None
    p = p.model_copy(update={"kind": "mimo", "model": "mimo-v2.5-tts-voiceclone"})
    assert keys.speech(p, "你好") is None
    p = p.model_copy(update={"kind": "siliconflow", "model": "FunAudioLLM/CosyVoice2-0.5B"})
    p.options.reference_source = "file"
    assert keys.speech(p, "你好") is None


def test_official_voice_catalog_is_model_scoped_and_custom_ids_remain_valid(server):
    catalog = get_catalog()
    assert len(catalog["qwen"]["model_voices"]["qwen-audio-3.1-tts-flash"]) == 139
    assert len(catalog["qwen"]["model_voices"]["qwen-audio-3.0-tts-plus"]) == 599
    assert catalog["cosyvoice"]["model_voices"]["cosyvoice-v3.5-flash"] == []
    assert "longanyang" in catalog["cosyvoice"]["model_voices"]["cosyvoice-v3-plus"]
    assert len(catalog["qwen"]["model_voices"]["qwen3-tts-flash"]) == 48
    assert catalog["siliconflow"]["model_voices"]["fnlp/MOSS-TTSD-v0.5"] == ["alex"]
    custom = profile(server).model_dump()
    custom.update(
        voice="my-custom-voice",
        voices=[{"id": "my-custom-voice", "name": "我的声音", "model": custom["model"], "speed": 1.2}],
    )
    resolved = Profile.model_validate(custom).with_selected_voice()
    assert resolved.voice == "my-custom-voice" and resolved.speed == 1.2


async def test_account_voice_cache_and_clone_mutation(tmp_path, server, wav_audio):
    calls, revision = [], [1]

    async def handle(request):
        calls.append(request.path)
        if request.path == "/reference.wav":
            return web.Response(body=wav_audio, content_type="audio/wav")
        if request.path.endswith("/uploads/audio/voice"):
            revision[0] += 1
            return web.json_response({"uri": "speech:created"})
        return web.json_response(
            {"results": [{"uri": f"speech:{revision[0]}", "customName": "账号音色", "model": p.model}]}
        )

    server.handler = handle
    store = ConfigStore(tmp_path)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    studio = Studio(store, service)
    p = Profile(
        id="sf",
        name="硅基流动",
        kind="siliconflow",
        base_url=server.base + "/v1",
        api_key="key",
        model="FunAudioLLM/CosyVoice2-0.5B",
        voice="FunAudioLLM/CosyVoice2-0.5B:anna",
        response_format="wav",
    )
    await store.save({"profiles": [p.model_dump()]}, 0)
    try:
        assert (await service.catalog_list(p, "voices"))[0]["id"] == "speech:1"
        other = p.model_copy(update={"model": "fnlp/MOSS-TTSD-v0.5"})
        assert await service.catalog_list(other, "voices") == await service.catalog_list(p, "voices")
        assert len(calls) == 1
        # 经过工作台真实任务分发创建音色；完成后同账号列表应立即重新获取。
        job = studio.launch("clone", p.id, name="新音色", url=server.base + "/reference.wav", text="参考文字")
        await studio.jobs[job["job_id"]].task
        assert studio.jobs[job["job_id"]].state == "done"
        assert (await service.catalog_list(other, "voices"))[0]["id"] == "speech:2"
        assert len(calls) == 4
    finally:
        await studio.close()
        await service.close()


async def test_concurrent_speech_is_shared_and_tone_changes_request(tmp_path, server, wav_audio):
    calls, started, finish = [], asyncio.Event(), asyncio.Event()

    async def handle(request):
        calls.append(await request.json())
        started.set()
        await finish.wait()
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    service = TTSService(ConfigStore(tmp_path), RuntimeSection(), logging.getLogger("test"))
    p = Profile(
        id="oa",
        name="OpenAI",
        kind="openai",
        base_url=server.base + "/v1",
        api_key="key",
        model="gpt-4o-mini-tts",
        voice="alloy",
        response_format="wav",
        tone={"mode": "dynamic", "emotion": "neutral", "allowed": ["neutral", "happy", "sad"]},
    )
    config = StudioConfig(profiles=[p], primary={"provider_id": p.id, "model_id": p.model})
    try:
        first = asyncio.create_task(service.synthesize("[mmtts:happy]你好", config=config))
        await started.wait()
        fixed = p.model_copy(update={"tone": p.tone.model_copy(update={"mode": "fixed", "emotion": "happy"})})
        fixed_config = StudioConfig(profiles=[fixed], primary=config.primary)
        second = asyncio.create_task(service.synthesize("你好", config=fixed_config))
        # 第二个请求进入共享等待后释放响应，验证按解析后的正文和语调复用。
        for _ in range(100):
            if service.cache.shared:
                break
            await asyncio.sleep(0)
        assert service.cache.shared == 1
        finish.set()
        a, b = await asyncio.gather(first, second)
        assert a.data == b.data and len(calls) == 1
        await service.synthesize("[mmtts:sad]你好", config=config)
        assert len(calls) == 2 and calls[0]["instructions"] != calls[1]["instructions"]
        with pytest.raises(TTSError):
            await service.synthesize("[mmtts:invalid]你好", config=config)
        assert len(calls) == 2
    finally:
        finish.set()
        await service.close()


async def test_disabled_cache_runs_each_request():
    cache = AsyncResultCache(0)
    calls = []

    async def load():
        calls.append(1)
        return b"audio"

    try:
        for _ in range(2):
            assert (await cache.get("voices", "same", load, ttl=30, size=len)).source == "bypass"
        assert len(calls) == 2 and not cache.entries
    finally:
        await cache.close()
