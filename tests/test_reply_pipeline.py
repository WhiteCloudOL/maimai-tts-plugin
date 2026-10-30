"""按 Host 的工具及 Hook 载荷模拟 planner → replyer → TTS → 适配器边界。"""

from aiohttp import web
from copy import deepcopy
from io import BytesIO
from maibot_sdk.context import PluginContext, PluginPaths

import base64
import re

import pysilk
import pytest

from tts_under_test.domain.intonation import tone_capability, tone_prompt
from tts_under_test.infrastructure.voice_codec import SILK_HEADER


async def invoke_hook(plugin, name, **payload):
    handlers = [
        component
        for component in plugin.get_components()
        if component["type"] == "HOOK_HANDLER" and component["metadata"]["hook"] == name
    ]
    # Host 对 modified_kwargs 使用完整替换，模拟时也必须保留其他参数。
    for component in handlers:
        result = await getattr(plugin, component["metadata"]["handler_name"])(**payload)
        payload = result.get("modified_kwargs", payload)
    return payload


def planner_tools(plugin):
    tool = next(c for c in plugin.get_components() if c["name"] == "mmtts_speak")
    return [
        {
            "type": "function",
            "function": {"name": "mmtts_speak", "description": tool["metadata"]["description"], "parameters": {}},
        },
        {"type": "function", "function": {"name": "reply", "description": "生成回复", "parameters": {}}},
    ]


@pytest.mark.parametrize("language", ["zh", "en", "ja"])
@pytest.mark.parametrize("mode", ["fixed", "dynamic"])
@pytest.mark.parametrize("flow", ["reply", "tool"])
async def test_planner_reply_voice_pipeline(plugin_module, tmp_path, server, wav_audio, language, mode, flow):
    requests = []
    sent = []

    async def provider(request):
        body = await request.json()
        requests.append(body)
        assert body["input"]["text"] == "今天有点失落，希望你能陪我聊聊。"
        assert "mmtts" not in body["input"]["text"]
        assert "悲伤" in body["input"]["instruction"]
        return web.json_response(
            {"output": {"finish_reason": "stop", "audio": {"data": base64.b64encode(wav_audio).decode()}}}
        )

    async def rpc(method, plugin_id, payload, **kwargs):
        assert method == "cap.call" and plugin_id == "maimai-tts-plugin"
        assert payload["capability"] == "send.hybrid"
        sent.append(payload["args"])
        return {"success": True, "sent": True, "message_id": "simulated-receipt"}

    server.handler = provider
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(PluginContext("maimai-tts-plugin", rpc_call=rpc, paths=PluginPaths(tmp_path, tmp_path)))
    await plugin.on_load()
    try:
        await plugin.store.save(
            {
                "profiles": [
                    {
                        "id": "qwen",
                        "name": "千问",
                        "kind": "qwen",
                        "base_url": server.base + "/api/v1",
                        "api_key": "test-key",
                        "model": "qwen-audio-3.1-tts-flash",
                        "voice": "longanhuan_v3.1",
                        "response_format": "wav",
                        "tone": {"mode": mode, "emotion": "sad", "allowed": ["sad"]},
                    }
                ],
                "primary": {"provider_id": "qwen", "model_id": "qwen-audio-3.1-tts-flash"},
                "feature": {
                    "probability_enabled": True,
                    "probability": 1,
                    "probability_keep_text": True,
                    "tool_keep_text": True,
                    "tone_prompt_language": language,
                },
            },
            0,
        )
        original = planner_tools(plugin)
        planner = await invoke_hook(
            plugin,
            "maisaka.planner.before_request",
            tool_definitions=original,
            items=[{"type": "message", "role": "user", "content": "陪我聊聊"}],
            item_schema_version=1,
            session_id="simulated-existing-stream",
        )
        assert original == planner_tools(plugin)
        assert planner["items"] == [{"type": "message", "role": "user", "content": "陪我聊聊"}]
        description = planner["tool_definitions"][0]["function"]["description"]
        assert planner["tool_definitions"][1] == original[1]
        if flow == "reply":
            # 模拟 planner 选择内置 reply 工具，replyer 发请求前经过实际注册的 Hook。
            reply = await invoke_hook(
                plugin,
                "maisaka.replyer.before_request",
                extra_prompt="请回应用户的失落。",
                request_type="maisaka.replyer",
                session_id="simulated-existing-stream",
                reply_tool_args={"msg_id": "m1"},
            )
            assert reply["request_type"] == "maisaka.replyer"
            assert reply["reply_tool_args"]["msg_id"] == "m1"
            prompt = reply["extra_prompt"]
        else:
            prompt = description
        body = "今天有点失落，希望你能陪我聊聊。"
        if mode == "dynamic":
            assert "[mmtts:happy]" not in prompt
            choice = re.search(r"\[mmtts:([a-z]+)\]", prompt)
            assert choice is not None and choice[1] == "sad"
            # 用受控 LLM 输出替代外部文本生成，标签取自实际传入模型的提示。
            generated = choice[0] + body
        else:
            assert re.search(r"\[mmtts:[a-z]+\]", prompt) is None
            assert description == original[0]["function"]["description"]
            generated = body
        if flow == "tool":
            component = next(c for c in plugin.get_components() if c["name"] == "mmtts_speak")
            result = await getattr(plugin, component["metadata"]["handler_name"])(
                text=generated,
                stream_id="simulated-existing-stream",
            )
            assert result["success"] and len(sent) == 1
            assert sent[0]["processed_plain_text"] == body
            assert sent[0]["segments"][0]["content"] == body
            segment = sent[0]["segments"][-1]
        else:
            prepared = await invoke_hook(
                plugin,
                "maisaka.reply.before_post_process",
                response=generated,
                session_id="simulated-existing-stream",
                reply_tool_args={"msg_id": "m1"},
            )
            assert prepared.get("skip_post_process", False) == (mode == "dynamic")
            message = {"raw_message": [{"type": "text", "data": prepared["response"]}], "message_info": {}}
            delivered = await invoke_hook(
                plugin,
                "send_service.after_build_message",
                message=message,
                stream_id="simulated-existing-stream",
                typing=False,
            )
            assert delivered["message"]["processed_plain_text"] == body
            assert delivered["message"]["raw_message"][0]["data"] == body
            assert message["raw_message"][0]["data"] == generated
            segment = delivered["message"]["raw_message"][-1]
        audio = base64.b64decode(segment["binary_data_base64"])
        assert audio.startswith(SILK_HEADER)
        decoded = BytesIO()
        pysilk.decode(BytesIO(audio), decoded, 24000)
        assert len(decoded.getvalue()) == 48000 * 2
        assert len(requests) == 1
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize(
    "generated", ["[mmtts:evil]原文", "[mmtts:happy]原文", "原文[mmtts:sad]", "[mmtts:sad][mmtts:sad]原文"]
)
async def test_invalid_llm_tone_never_reaches_provider_or_sender(plugin_module, tmp_path, generated):
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(PluginContext("maimai-tts-plugin", paths=PluginPaths(tmp_path, tmp_path)))
    await plugin.on_load()
    try:
        await plugin.store.save(
            {
                "profiles": [
                    {
                        "id": "q",
                        "name": "千问",
                        "kind": "qwen",
                        "base_url": "http://127.0.0.1:1/api/v1",
                        "model": "qwen-audio-3.1-tts-flash",
                        "voice": "longanhuan_v3.1",
                        "api_key": "test-key",
                        "tone": {"mode": "dynamic", "emotion": "sad", "allowed": ["sad"]},
                    }
                ],
                "primary": {"provider_id": "q", "model_id": "qwen-audio-3.1-tts-flash"},
                "feature": {"probability_enabled": True, "probability": 1},
            },
            0,
        )
        prepared = await invoke_hook(
            plugin, "maisaka.reply.before_post_process", response=generated, session_id="stream"
        )
        assert prepared["response"] == "原文"
        message = {"raw_message": [{"type": "text", "data": prepared["response"]}], "message_info": {}}
        copied = deepcopy(message)
        result = await invoke_hook(plugin, "send_service.after_build_message", message=message, stream_id="stream")
        assert result["message"] == copied
        tool = await plugin.handle_tts_tool(text=generated, stream_id="stream")
        assert not tool["success"]
        assert not plugin.service.history
    finally:
        await plugin.on_unload()


def test_voice_clone_tone_capabilities_follow_platform_boundaries():
    assert tone_capability("volcengine", "", "S_example", "seed-icl-2.0") is not None
    assert tone_capability("volcengine", "seed-tts-2.0-standard", "S_example", "seed-icl-2.0") is None
    assert tone_capability("cosyvoice", "cosyvoice-v3.5-flash", "longanyang", "") is None
    assert tone_capability("cosyvoice", "cosyvoice-v3.5-flash", "cosyvoice-v3.5-flash-test-123", "") is not None
    assert tone_capability("cosyvoice", "cosyvoice-v3-plus", "cosyvoice-v3-plus-test-123", "") is None


@pytest.mark.parametrize("language", ["zh", "en", "ja"])
def test_prompt_examples_always_use_allowed_tones(language):
    assert "[mmtts:sad]" in tone_prompt(["sad"], language, automatic=True, default="sad")
    assert "[mmtts:happy]" not in tone_prompt(["sad"], language, automatic=True, default="sad")
