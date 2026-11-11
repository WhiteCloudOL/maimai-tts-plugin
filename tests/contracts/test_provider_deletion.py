"""服务商删除独立提交、持久化、路由清理与并发写入的回归。"""

from types import SimpleNamespace
from unittest.mock import Mock

import asyncio
import pytest

from maimai_tts_contract.domain.models import Profile, RouteTarget, StudioConfig, TTSError
from maimai_tts_contract.infrastructure.storage import ConfigStore
from maimai_tts_contract.plugin import TTSPlugin


async def saved_store(tmp_path):
    profiles = [Profile(
        id=name, name=name, kind="openai", base_url="https://example.test/v1",
        api_key="test-only-" + name, model="gpt-4o-mini-tts", voice="alloy",
        models=[{"id": "gpt-4o-mini-tts", "voice": "alloy"}, {"id": "tts-1", "voice": "echo"}],
    ) for name in ("first", "second", "third")]
    config = StudioConfig(
        profiles=profiles, primary=RouteTarget(provider_id="first", model_id="gpt-4o-mini-tts"),
        fallbacks=[RouteTarget(provider_id="first", model_id="tts-1"),
                   RouteTarget(provider_id="second", model_id="gpt-4o-mini-tts"),
                   RouteTarget(provider_id="third", model_id="gpt-4o-mini-tts")],
    )
    store = ConfigStore(tmp_path)
    await store.save(config.model_dump(), 0)
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["first", "second"])
async def test_deletion_persists_only_target_and_cleans_its_routes(tmp_path, provider_id):
    store = await saved_store(tmp_path)
    before = store.config
    result = await store.delete_profile(provider_id, before.revision)
    after = store.config
    assert after.revision == before.revision + 1
    assert {p.id for p in after.profiles} == {p.id for p in before.profiles} - {provider_id}
    assert after.primary.provider_id == ("second" if provider_id == "first" else "first")
    assert all(target.provider_id != provider_id for target in after.fallbacks)
    assert after.feature == before.feature
    for profile in after.profiles:
        assert profile == next(saved for saved in before.profiles if saved.id == profile.id)
    assert all("api_key" not in profile for profile in result["profiles"])
    loaded = ConfigStore(tmp_path)
    await loaded.load()
    assert loaded.config == after


@pytest.mark.asyncio
async def test_removing_last_provider_leaves_empty_routes(tmp_path):
    store = await saved_store(tmp_path)
    for provider_id in ("first", "second", "third"):
        await store.delete_profile(provider_id, store.config.revision)
    assert store.config.profiles == [] and store.config.primary is None and store.config.fallbacks == []


@pytest.mark.asyncio
async def test_conflicting_or_missing_delete_keeps_saved_state(tmp_path):
    store = await saved_store(tmp_path)
    before = store.config
    for provider_id, revision in (("first", 0), ("missing", before.revision)):
        with pytest.raises(TTSError):
            await store.delete_profile(provider_id, revision)
        assert store.config == before
    loaded = ConfigStore(tmp_path)
    await loaded.load()
    assert loaded.config == before


@pytest.mark.asyncio
async def test_write_failure_keeps_provider_and_routes(tmp_path, monkeypatch):
    store = await saved_store(tmp_path)
    before = store.config
    monkeypatch.setattr(store, "_write", Mock(side_effect=OSError("test disk failure")))
    with pytest.raises(OSError):
        await store.delete_profile("first", before.revision)
    assert store.config == before
    loaded = ConfigStore(tmp_path)
    await loaded.load()
    assert loaded.config == before


@pytest.mark.asyncio
async def test_concurrent_deletions_preserve_revision_order(tmp_path):
    store = await saved_store(tmp_path)
    before = store.config
    results = await asyncio.gather(
        store.delete_profile("first", before.revision), store.delete_profile("second", before.revision),
        return_exceptions=True,
    )
    assert sum(isinstance(result, TTSError) for result in results) == 1
    assert len(store.config.profiles) == 2 and store.config.revision == before.revision + 1
    loaded = ConfigStore(tmp_path)
    await loaded.load()
    assert loaded.config == store.config


@pytest.mark.asyncio
async def test_delete_api_updates_live_decisions_after_persisting(tmp_path):
    store = await saved_store(tmp_path)
    invalidate = Mock()
    harness = SimpleNamespace(store=store, service=SimpleNamespace(log=Mock()), _invalidate_probability_decisions=invalidate)
    result = await TTSPlugin.studio_delete_profile(harness, "first", store.config.revision)
    assert "first" not in {profile["id"] for profile in result["profiles"]}
    invalidate.assert_called_once_with()
