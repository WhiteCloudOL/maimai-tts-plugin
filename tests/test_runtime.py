from aiohttp import web
from maibot_sdk.components import collect_components
from maibot_sdk.context import PluginContext, PluginPaths
from tts_under_test.domain.catalog import resolve_model_url
from tts_under_test.domain.models import Audio, TTSError
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.services.studio import Studio
from tts_under_test.services.tts import TTSService
from tts_under_test.settings.config import RuntimeSection
from unittest.mock import AsyncMock

import asyncio
import json
import logging
import pytest


def config_payload(endpoint="http://127.0.0.1/tts"):
    return {
        "profiles": [
            {
                "id": "main",
                "name": "主服务",
                "kind": "compatible",
                "endpoint": endpoint,
                "model": "tts",
                "voice": "test",
                "api_key": "unit-test-secret",
            }
        ],
        "default_provider": "main",
    }


async def test_atomic_store_secrets_revisions_and_origin_change(tmp_path):
    store = ConfigStore(tmp_path)
    snapshot = await store.save(config_payload(), 0)
    assert snapshot["profiles"][0]["has_api_key"] is True
    assert "unit-test-secret" not in json.dumps(snapshot)
    restored = ConfigStore(tmp_path)
    await restored.load()
    assert restored.profile("main").api_key == "unit-test-secret"
    with pytest.raises(TTSError, match="其他页面"):
        await store.save(snapshot, 0)
    snapshot["profiles"][0]["voice"] = "new-voice"
    snapshot = await store.save(snapshot, 1)
    assert store.profile("main").api_key == "unit-test-secret"
    snapshot["profiles"][0]["models_url"] = "http://another-host/models"
    with pytest.raises(TTSError, match="重新填写密钥"):
        await store.save(snapshot, 2)
    assert store.config.revision == 2
    assert store.profile("main").models_url == ""


async def test_corrupt_config_is_not_overwritten(tmp_path):
    path = tmp_path / "studio.json"
    path.write_text('{"profiles": INVALID}', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(TTSError, match="读取失败"):
        await ConfigStore(tmp_path).load()
    assert path.read_bytes() == before


async def test_invalid_routing_is_not_persisted(tmp_path):
    store = ConfigStore(tmp_path)
    data = config_payload()
    data["fallback_providers"] = ["main"]
    with pytest.raises(TTSError, match="重复"):
        await store.save(data, 0)
    assert not store.path.exists()


async def test_ordered_failover_and_forced_profile_does_not_fallback(tmp_path, server, wav_audio):
    attempts = []

    async def handle(request):
        attempts.append(request.path)
        if request.path != "/good":
            return web.json_response({"error": "unavailable"}, status=503)
        return web.Response(body=wav_audio)

    server.handler = handle
    data = config_payload(server.base + "/main")
    for profile_id in ["backup1", "backup2"]:
        data["profiles"].append(
            {
                "id": profile_id,
                "name": profile_id,
                "kind": "compatible",
                "endpoint": server.base + ("/good" if profile_id == "backup2" else "/bad"),
            }
        )
    data["fallback_providers"] = ["backup1", "backup2"]
    store = ConfigStore(tmp_path)
    await store.save(data, 0)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    try:
        result = await service.synthesize("你好")
        assert result.provider_id == "backup2"
        assert attempts == ["/main", "/bad", "/good"]
        assert len(service.history) == 3
        attempts.clear()
        with pytest.raises(TTSError):
            await service.synthesize("你好", profile_id="main")
        assert attempts == ["/main"]
    finally:
        await service.close()


async def test_background_audio_chunking_and_cancellation(tmp_path):
    store = ConfigStore(tmp_path)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    studio = Studio(store, service)
    try:

        async def audio():
            return Audio(b"x" * 140000, "audio/mpeg", "mp3")

        started = studio.start(audio)
        await asyncio.sleep(0)
        part1 = studio.job(started["job_id"])
        part2 = studio.job(started["job_id"], part1["next_offset"])
        assert part1["total"] == 140000
        assert part1["next_offset"] == 65536
        assert part2["next_offset"] == 131072
        assert len(json.dumps(part1)) < 100000
        started = studio.start(lambda: asyncio.sleep(100))
        await asyncio.sleep(0)
        studio.cancel(started["job_id"])
        await asyncio.sleep(0)
        assert studio.job(started["job_id"])["state"] == "cancelled"
    finally:
        await studio.close()
        await service.close()


async def test_lifecycle_component_collection_and_real_stream_send(plugin_module, tmp_path, wav_audio):
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    rpc = AsyncMock(return_value={"success": True})
    ctx = PluginContext(
        "maimai-tts-plugin",
        rpc_call=rpc,
        paths=PluginPaths(data_dir=tmp_path / "data", runtime_dir=tmp_path / "runtime"),
    )
    plugin._set_context(ctx)
    await plugin.on_load()
    try:
        components = collect_components(plugin)
        assert {component["name"] for component in components} >= {
            "openai_tts",
            "mmtts_speak",
            "studio_state",
            "studio_save",
            "studio_save_profile",
            "studio_reference_files",
            "studio_import_reference",
            "probability_tts",
        }
        assert plugin.store.path == tmp_path / "data" / "studio.json"
        snapshot = await plugin.studio_state()
        assert snapshot["reference_audio"]["example"] == "reference_audio/sample.wav"
        assert (tmp_path / "data" / "reference_audio").is_dir()
        plugin.service.synthesize = AsyncMock(return_value=Audio(wav_audio, "audio/wav", "wav"))
        result = await plugin.handle_tts_tool("你好", stream_id="real-stream-id")
        assert result["success"]
        call = rpc.call_args
        assert call.args[2]["args"]["stream_id"] == "real-stream-id"
        await plugin.on_config_update("self", {}, "2.0.0")
        assert plugin.studio.jobs == {}
    finally:
        service = plugin.service
        await plugin.on_unload()
        assert service.http.session.closed


async def test_probability_segments_share_one_decision_and_failure_keeps_text(plugin_module, tmp_path, wav_audio):
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(PluginContext("maimai-tts-plugin", paths=PluginPaths(tmp_path, tmp_path)))
    await plugin.on_load()
    try:
        data = config_payload()
        data["feature"] = {"probability_enabled": True, "probability": 1}
        await plugin.store.save(data, 0)
        await plugin.prepare_probability_tts_reply(response="你好，再见", session_id="real-stream")
        plugin.service.synthesize = AsyncMock(return_value=Audio(wav_audio, "audio/wav", "wav"))
        message = {"raw_message": [{"type": "text", "data": "你好"}], "message_info": {}}
        result = await plugin.handle_probability_tts(message=message, stream_id="real-stream")
        assert result["modified_kwargs"]["message"]["raw_message"][0]["type"] == "voice"
        plugin.service.synthesize.side_effect = TTSError("真实错误")
        assert await plugin.handle_probability_tts(message=message, stream_id="real-stream", typing=True) == {
            "action": "continue",
        }
        plugin.service.synthesize.reset_mock()
        await plugin.handle_probability_tts(message=message, stream_id="real-stream", typing=True)
        plugin.service.synthesize.assert_not_called()
    finally:
        await plugin.on_unload()


def test_model_links_are_parsed_without_inventing_an_inference_url():
    result = resolve_model_url("https://huggingface.co/FunAudioLLM/CosyVoice2-0.5B/tree/main", "siliconflow")
    assert result["model"] == "FunAudioLLM/CosyVoice2-0.5B"
    result = resolve_model_url("https://cloud.siliconflow.cn/models?model=FunAudioLLM%2FCosyVoice2-0.5B", "siliconflow")
    assert result["endpoint"].endswith("/audio/speech")
    with pytest.raises(TTSError, match="无法识别"):
        resolve_model_url("https://unknown.example/models/a", "compatible")
