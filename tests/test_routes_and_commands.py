"""Provider/Model 整体路由、平台编辑隔离与切换命令。"""

from aiohttp import web
from maibot_sdk.components import collect_components
from maibot_sdk.context import PluginContext, PluginPaths
from unittest.mock import AsyncMock

import logging
import re

import pytest

from tts_under_test.domain.models import Audio, TTSError
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.services.routing import RoutingService
from tts_under_test.services.tts import TTSService
from tts_under_test.settings.config import RuntimeSection


def payload(endpoint="http://localhost/tts"):
    return {
        "profiles": [
            {
                "id": "vendor",
                "name": "Vendor",
                "kind": "compatible",
                "endpoint": endpoint,
                "model": "editor",
                "voice": "editor-voice",
                "models": [
                    {"id": "editor", "voice": "editor-voice"},
                    {"id": "Family/selected", "voice": "stored-voice"},
                ],
            }
        ],
        "primary": {"provider_id": "vendor", "model_id": "Family/selected", "voice": "route-voice"},
        "fallbacks": [{"provider_id": "vendor", "model_id": "editor", "voice": "backup-voice"}],
    }


async def test_route_owns_model_and_voice_independently_of_editor(tmp_path, server, wav_audio):
    received = []

    async def handle(request):
        body = await request.json()
        received.append(body)
        if body["model"] == "Family/selected":
            return web.Response(status=503)
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    store = ConfigStore(tmp_path)
    await store.save(payload(server.base + "/tts"), 0)
    editor = store.snapshot()["profiles"][0]
    editor["voice"] = "changed-editor-voice"
    await store.save_profile(editor, 1)
    assert store.config.primary.voice == "route-voice"
    assert store.config.primary.model_id == "Family/selected"
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    try:
        await service.synthesize("你好")
        assert [(item["model"], item["voice"]) for item in received] == [
            ("Family/selected", "route-voice"),
            ("editor", "backup-voice"),
        ]
    finally:
        await service.close()


async def test_platform_save_does_not_assign_a_primary_route(tmp_path):
    store = ConfigStore(tmp_path)
    await store.save_profile(payload()["profiles"][0], 0)
    assert store.config.primary is None
    assert store.config.fallbacks == []


async def test_route_save_is_atomic_and_preserves_platforms_and_features(tmp_path):
    store = ConfigStore(tmp_path)
    data = payload()
    data["profiles"][0]["api_key"] = "test-private-value"
    data["feature"] = {"probability": 0.75, "tool_keep_text": True}
    await store.save(data, 0)
    before = store.config
    routing = {"primary": data["fallbacks"][0], "fallbacks": [data["primary"]]}
    snapshot = await store.save_routing(routing, 1)
    assert store.config.profiles == before.profiles
    assert store.config.feature == before.feature
    assert "api_key" not in snapshot["profiles"][0]
    assert snapshot["revision"] == 2
    restored = ConfigStore(tmp_path)
    await restored.load()
    assert restored.config == store.config
    for invalid, revision in (
        (routing, 1),
        ({"primary": {"provider_id": "vendor", "model_id": "missing"}, "fallbacks": []}, 2),
        ({**routing, "profiles": []}, 2),
    ):
        with pytest.raises(TTSError):
            await store.save_routing(invalid, revision)
    assert store.snapshot() == snapshot


async def test_saved_route_switch_takes_effect_with_warm_audio_cache(tmp_path, server, wav_audio):
    received = []

    async def handle(request):
        body = await request.json()
        received.append((body["model"], body["voice"]))
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    store = ConfigStore(tmp_path)
    data = payload(server.base + "/tts")
    data["fallbacks"] = []
    await store.save(data, 0)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    try:
        await service.synthesize("同一句话")
        for model, voice in (("editor", "backup-voice"), ("editor", "new-voice")):
            await store.save_routing(
                {"primary": {"provider_id": "vendor", "model_id": model, "voice": voice}, "fallbacks": []},
                store.config.revision,
            )
            await service.synthesize("同一句话")
            await service.synthesize("同一句话")
        assert received == [
            ("Family/selected", "route-voice"),
            ("editor", "backup-voice"),
            ("editor", "new-voice"),
        ]
    finally:
        await service.close()


@pytest.mark.parametrize("selector", ["2", "Vendor/Family/selected", "vendor/Family/selected"])
async def test_index_and_provider_model_switch_the_same_route(tmp_path, selector):
    store = ConfigStore(tmp_path)
    data = payload()
    data["primary"], data["fallbacks"] = data["fallbacks"][0], [data["primary"]]
    await store.save(data, 0)
    routing = RoutingService(store)
    assert "2. Vendor/Family/selected" in routing.describe()
    assert "Vendor/Family/selected" in await routing.switch(selector)
    assert store.config.primary.model_id == "Family/selected"
    assert store.config.primary.voice == "route-voice"
    assert store.config.fallbacks == []
    restored = ConfigStore(tmp_path)
    await restored.load()
    assert restored.config.primary == store.config.primary


async def test_invalid_selection_keeps_routes_unchanged(tmp_path):
    store = ConfigStore(tmp_path)
    await store.save(payload(), 0)
    before = store.snapshot()
    for selector in ("0", "99", "unknown/model"):
        with pytest.raises(TTSError):
            await RoutingService(store).switch(selector)
    assert store.snapshot() == before


async def test_model_command_lists_and_switches_with_plain_text_reply(plugin_module, tmp_path):
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    rpc = AsyncMock(return_value={"success": True})
    plugin._set_context(
        PluginContext("WhiteCloudOL.maimai-tts-plugin", rpc_call=rpc, paths=PluginPaths(tmp_path, tmp_path))
    )
    await plugin.on_load()
    try:
        data = payload()
        data["feature"] = {"probability_enabled": True, "probability": 1}
        await plugin.store.save(data, 0)
        plugin.service.synthesize = AsyncMock()

        async def reply_through_hook(*args, **kwargs):
            text = args[2]["args"]["text"]
            hook = await plugin.handle_probability_tts(
                message={"raw_message": [{"type": "text", "data": text}], "message_info": {}}, stream_id="real-stream"
            )
            assert hook == {"action": "continue"}
            return {"success": True}

        rpc.side_effect = reply_through_hook
        result = await plugin.handle_model_command(stream_id="real-stream", matched_groups={})
        assert result[0] is True
        assert "1. Vendor/editor" in result[1]
        assert rpc.call_args.args[2]["capability"] == "send.text"
        result = await plugin.handle_model_command(stream_id="real-stream", matched_groups={"target": "1"})
        assert result[0] is True
        assert plugin.store.config.primary.model_id == "editor"
        assert plugin._control_reply_guards == set()
        plugin.service.synthesize.assert_not_called()
        command = next(item for item in collect_components(plugin) if item["name"] == "mmtts_model")
        assert command["metadata"]["permission"] == "operator"
    finally:
        await plugin.on_unload()


async def test_long_model_list_keeps_global_indexes_across_pages(tmp_path):
    store = ConfigStore(tmp_path)
    data = payload()
    profile = data["profiles"][0]
    profile["models"] = [{"id": f"model-{i:03d}-" + "x" * 180} for i in range(1, 101)]
    profile["model"] = profile["models"][0]["id"]
    data["primary"] = {"provider_id": "vendor", "model_id": profile["model"]}
    data["fallbacks"] = []
    await store.save(data, 0)
    routing = RoutingService(store)
    first = routing.describe()
    count = int(re.search(r"（1/(\d+)）", first).group(1))
    assert count > 1
    assert len(first) < 2000
    assert "100. Vendor/" in routing.describe(count)
    await routing.switch("100")
    assert store.config.primary.model_id == profile["models"][-1]["id"]


async def test_provider_names_are_continuous_and_manual_dots_remain_allowed(tmp_path):
    store = ConfigStore(tmp_path)
    data = payload()
    data["profiles"][0]["name"] = "  Provider With\tSpaces　"
    await store.save(data, 0)
    assert store.profile("vendor").name == "ProviderWithSpaces"
    await RoutingService(store).switch("ProviderWithSpaces/Family/selected")
    edited = store.snapshot()["profiles"][0]
    edited["name"] = "阿里云·千问"
    await store.save_profile(edited, store.config.revision)
    assert store.profile("vendor").name == "阿里云·千问"


@pytest.mark.parametrize("command", ["/mmtts", "/tts", "/语音"])
@pytest.mark.parametrize("prefix", ["", "  ", "@麦麦 ", "@麦麦", "@麦麦\u00a0"])
def test_speech_command_accepts_leading_mentions(plugin_module, command, prefix):
    components = collect_components(plugin_module.create_plugin())
    declared = next(c for c in components if c["name"] == "mmtts")
    assert declared["metadata"]["timeout_ms"] == 600000
    assert declared["metadata"]["aliases"] == []
    match = re.search(declared["metadata"]["command_pattern"], prefix + command + " 你好\n第二行")
    assert match is not None
    assert match.group("text") == "你好\n第二行"


@pytest.mark.parametrize(
    "text",
    ["你试试 /tts 你好", "@麦麦 你试试 /tts 你好", "/tts2 你好", "tts 你好", "/mmtts model", "/mmtts model 2"],
)
def test_speech_command_keeps_explicit_boundaries_and_model_namespace(plugin_module, text):
    components = collect_components(plugin_module.create_plugin())
    command = next(c for c in components if c["name"] == "mmtts")
    assert re.search(command["metadata"]["command_pattern"], text) is None
    assert all(c["name"] != "openai_tts" for c in components)


def test_model_switch_command_accepts_leading_mention(plugin_module):
    command = next(c for c in collect_components(plugin_module.create_plugin()) if c["name"] == "mmtts_model")
    match = re.search(command["metadata"]["command_pattern"], "@麦麦 /mmtts model Vendor/Family/selected")
    assert match is not None
    assert match.group("target") == "Vendor/Family/selected"
    assert command["metadata"]["permission"] == "operator"


async def test_mentioned_speech_command_sends_only_captured_body(plugin_module, tmp_path, wav_audio):
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    rpc = AsyncMock(return_value={"success": True, "sent": True})
    plugin._set_context(
        PluginContext("WhiteCloudOL.maimai-tts-plugin", rpc_call=rpc, paths=PluginPaths(tmp_path, tmp_path))
    )
    await plugin.on_load()
    try:
        plugin.service.synthesize = AsyncMock(return_value=Audio(wav_audio, "audio/wav", "wav"))
        command = next(c for c in collect_components(plugin) if c["name"] == "mmtts")
        match = re.search(command["metadata"]["command_pattern"], "@麦麦 /tts 111111")
        result = await plugin.handle_tts_command(stream_id="real-stream", matched_groups=match.groupdict())
        assert result == (True, "语音已发送", True)
        assert plugin.service.synthesize.call_args.args == ("111111",)
        sent = rpc.call_args.args[2]
        assert sent["capability"] == "send.hybrid"
        assert sent["args"]["processed_plain_text"] == "111111"
        assert sent["args"]["stream_id"] == "real-stream"
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize("reason", ["disabled", "empty", "synthesis"])
async def test_speech_command_failure_is_sent_to_chat_as_plain_text(plugin_module, tmp_path, reason):
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    sent = []

    async def rpc(method, plugin_id, payload, **kwargs):
        assert payload["capability"] == "send.text"
        args = payload["args"]
        hook = await plugin.handle_probability_tts(
            message={"raw_message": [{"type": "text", "data": args["text"]}], "message_info": {}},
            stream_id=args["stream_id"],
        )
        assert hook == {"action": "continue"}
        sent.append(args["text"])
        return {"success": True}

    plugin._set_context(
        PluginContext("WhiteCloudOL.maimai-tts-plugin", rpc_call=rpc, paths=PluginPaths(tmp_path, tmp_path))
    )
    await plugin.on_load()
    try:
        await plugin.store.save(
            {"feature": {"command_enabled": reason != "disabled", "probability_enabled": True, "probability": 1}}, 0
        )
        plugin.service.synthesize = AsyncMock(side_effect=TTSError("测试平台暂不可用"))
        result = await plugin.handle_tts_command(
            stream_id="real-stream", matched_groups={} if reason == "empty" else {"text": "你好"}
        )
        assert result[0] is False and result[2] is True
        assert sent == [result[1]]
        assert plugin._control_reply_guards == set()
        if reason != "synthesis":
            plugin.service.synthesize.assert_not_awaited()
        else:
            assert "测试平台暂不可用" in sent[0]
    finally:
        await plugin.on_unload()
