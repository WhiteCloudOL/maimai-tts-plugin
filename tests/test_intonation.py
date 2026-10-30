"""固定参数、动态标签、提示注入与平台能力边界。"""

from aiohttp import web
from maibot_sdk.context import PluginContext, PluginPaths
from unittest.mock import AsyncMock

import base64
import json
import logging

import pytest

from tts_under_test.domain.intonation import parse_tone_tag, tone_prompt
from tts_under_test.domain.models import Audio, Profile, TTSError
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.services.tts import TTSService
from tts_under_test.services.intonation import apply_tone
from tts_under_test.settings.config import RuntimeSection


@pytest.mark.parametrize(
    "text",
    [
        "[mmtts:wrong]你好",
        "[mmtts:happy][mmtts:sad]你好",
        "你好[mmtts:happy]",
        "[MMTTS:happy]你好",
        "[mmtts:happy evil=1]你好",
        "[mmtts:happy",
        "[mmtts:happy]",
        "<mmtts:happy>你好",
        "[mmtts:happy]你好[/mmtts]",
    ],
)
def test_invalid_tag_is_reported_and_never_kept_for_display(text):
    parsed = parse_tone_tag(text)
    assert parsed.error
    assert parsed.tagged
    assert "mmtts" not in parsed.text.lower()


def test_plain_text_and_valid_tag_are_distinct():
    assert parse_tone_tag("固定语调的正文").text == "固定语调的正文"
    assert not parse_tone_tag("固定语调的正文").tagged
    parsed = parse_tone_tag(" [mmtts:happy]你好！")
    assert parsed.text == "你好！"
    assert parsed.tone == "happy"
    assert not parsed.error
    assert parse_tone_tag("[S1]你好").text == "[S1]你好"


CASES = [
    ("openai", "gpt-4o-mini-tts", "alloy"),
    ("compatible", "gpt-4o-mini-tts", "alloy"),
    ("qwen", "qwen3-tts-instruct-flash", "Cherry"),
    ("qwen", "qwen3-tts-instruct-flash-realtime", "Cherry"),
    ("cosyvoice", "cosyvoice-v3-flash", "longanyang"),
    ("cosyvoice", "cosyvoice-v3.5-flash", "cosyvoice-v3.5-flash-test-voice"),
    ("siliconflow", "FunAudioLLM/CosyVoice2-0.5B", "speech:mine"),
    ("mimo", "mimo-v2.5-tts", "mimo_default"),
    ("volcengine", "seed-tts-2.0-standard", "zh_female_vv_uranus_bigtts"),
]


@pytest.mark.parametrize("kind,model,voice", CASES)
@pytest.mark.parametrize("mode", ["fixed", "dynamic"])
async def test_platform_tone_wire_contract(tmp_path, server, wav_audio, kind, model, voice, mode):
    requests = []

    async def handle(request):
        if kind == "cosyvoice":
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            run = await socket.receive_json()
            task_id = run["header"]["task_id"]
            instruction = run["payload"]["parameters"]["instruction"]
            if model == "cosyvoice-v3-flash":
                assert instruction == "你说话的情感是happy。"
            else:
                assert "开心" in instruction
            await socket.send_json({"header": {"task_id": task_id, "event": "task-started"}})
            content = await socket.receive_json()
            assert content["payload"]["input"]["text"] == "你好！"
            await socket.receive_json()
            await socket.send_bytes(wav_audio)
            await socket.send_json({"header": {"task_id": task_id, "event": "task-finished"}})
            requests.append("cosy")
            return socket
        if "realtime" in model:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json({"type": "session.created"})
            update = await socket.receive_json()
            assert "开心" in update["session"]["instructions"]
            assert update["session"]["optimize_instructions"] is False
            await socket.send_json({"type": "session.updated"})
            assert (await socket.receive_json())["text"] == "你好！"
            await socket.receive_json()
            await socket.send_json({"type": "response.audio.delta", "delta": base64.b64encode(b"\0\0" * 2400).decode()})
            await socket.send_json({"type": "session.finished"})
            requests.append("qwen-realtime")
            return socket
        body = await request.json()
        requests.append(body)
        assert "mmtts" not in json.dumps(body, ensure_ascii=False)
        if kind in {"openai", "compatible"}:
            assert body["input"] == "你好！"
            assert "开心" in body["instructions"]
        elif kind == "qwen":
            assert body["input"]["text"] == "你好！"
            assert "开心" in body["input"]["instructions"]
            return web.json_response({"output": {"audio": {"data": base64.b64encode(wav_audio).decode()}}})
        elif kind == "siliconflow":
            assert body["input"].endswith("<|endofprompt|>你好！")
            assert "开心" in body["input"]
            assert "instructions" not in body
        elif kind == "mimo":
            assert body["messages"][-1] == {"role": "assistant", "content": "你好！"}
            assert "开心" in body["messages"][0]["content"]
            return web.json_response(
                {"choices": [{"message": {"audio": {"data": base64.b64encode(wav_audio).decode()}}}]}
            )
        elif kind == "volcengine":
            params = body["req_params"]
            assert params["text"] == "你好！"
            assert "开心" in json.loads(params["additions"])["context_texts"][0]
            return web.Response(
                text=json.dumps({"code": 20000000, "data": base64.b64encode(wav_audio).decode()}) + "\n"
            )
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    store = ConfigStore(tmp_path)
    profile = dict(
        id="p",
        name="平台",
        api_key="sample-key",
        kind=kind,
        model=model,
        voice=voice,
        base_url=server.base,
        response_format="mp3" if kind == "volcengine" else "wav",
        options={"sample_rate": 44100 if kind == "siliconflow" else 24000},
        tone={"mode": mode, "emotion": "happy" if mode == "fixed" else "neutral", "allowed": ["neutral", "happy"]},
    )
    await store.save({"profiles": [profile], "primary": {"provider_id": "p", "model_id": model}}, 0)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    try:
        audio = await service.synthesize("你好！" if mode == "fixed" else "[mmtts:happy]你好！")
        assert audio.data
        assert len(requests) == 1
        assert store.config.profiles[0].instructions == ""
        with pytest.raises(TTSError):
            await service.synthesize("[mmtts:wrong]你好")
        with pytest.raises(TTSError):
            await service.synthesize("[mmtts:sad]你好")
        assert len(requests) == 1
    finally:
        await service.close()


@pytest.mark.parametrize(
    "kind,model,voice",
    [
        ("openai", "tts-1", "alloy"),
        ("qwen", "qwen3-tts-flash", "Cherry"),
        ("qwen", "qwen3-tts-vc-realtime-2026-01-15", "cloned"),
        ("cosyvoice", "cosyvoice-v3-flash", "cloned"),
        ("siliconflow", "fnlp/MOSS-TTSD-v0.5", "alex"),
        ("gpt_sovits", "local", "reference"),
        ("compatible", "tts-1", "alloy"),
    ],
)
def test_unsupported_tone_combination_is_rejected_when_saving(kind, model, voice):
    with pytest.raises(ValueError, match="受控语调"):
        Profile(
            id="p",
            name="平台",
            kind=kind,
            model=model,
            voice=voice,
            base_url="https://example.com",
            response_format="wav",
            tone={"mode": "fixed", "emotion": "happy"},
            options={"sample_rate": 44100},
        )


async def test_only_dynamic_mode_adds_prompt_and_keeps_visible_text_clean(plugin_module, tmp_path, wav_audio):
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(PluginContext("maimai-tts-plugin", paths=PluginPaths(tmp_path, tmp_path)))
    await plugin.on_load()
    try:
        profile = dict(
            id="p",
            name="平台",
            kind="mimo",
            model="mimo-v2.5-tts",
            voice="mimo_default",
            base_url="https://example.com/v1",
            response_format="wav",
            tone={"mode": "fixed", "emotion": "happy"},
        )
        payload = dict(
            profiles=[profile],
            primary={"provider_id": "p", "model_id": profile["model"]},
            feature={"probability_enabled": True, "probability": 1, "probability_keep_text": True},
        )
        await plugin.store.save(payload, 0)
        fixed = await plugin.prepare_dynamic_tone_prompt(extra_prompt="已有提示", session_id="real-stream")
        assert fixed == {"action": "continue"}
        options = await plugin.handle_tone_options()
        assert options["mode"] == "fixed"
        assert options["allowed_tones"] == []
        profile["tone"] = {"mode": "dynamic", "allowed": ["neutral", "happy"]}
        await plugin.store.save(payload, 1)
        options = await plugin.handle_tone_options()
        assert options["allowed_tones"] == ["neutral", "happy"]
        prompt = await plugin.prepare_dynamic_tone_prompt(extra_prompt="已有提示", session_id="real-stream")
        assert prompt["modified_kwargs"]["extra_prompt"].startswith("已有提示")
        assert "happy(开心)" in prompt["modified_kwargs"]["extra_prompt"]
        assert "sad(" not in prompt["modified_kwargs"]["extra_prompt"]
        plugin._synthesize_audio = AsyncMock(return_value=Audio(wav_audio, "audio/wav", "wav"))
        prepared = await plugin.prepare_probability_tts_reply(response="[mmtts:happy]你好", session_id="real-stream")
        assert prepared["modified_kwargs"]["skip_post_process"] is True
        message = {
            "raw_message": [{"type": "text", "data": prepared["modified_kwargs"]["response"]}],
            "message_info": {},
        }
        sent = await plugin.handle_probability_tts(message=message, stream_id="real-stream")
        result = sent["modified_kwargs"]["message"]
        assert result["raw_message"][0]["data"] == "你好"
        assert result["processed_plain_text"] == "你好"
        plugin._synthesize_audio.assert_awaited_once_with(text="[mmtts:happy]你好")
        plugin._synthesize_audio.reset_mock()
        invalid = await plugin.prepare_probability_tts_reply(response="[mmtts:evil]原文", session_id="real-stream")
        assert invalid["modified_kwargs"]["response"] == "原文"
        assert plugin._probability_reply_states["real-stream"].should_convert is False
        raw = {"raw_message": [{"type": "text", "data": "[mmtts:evil]原文"}], "message_info": {}}
        cleaned = await plugin.handle_probability_tts(message=raw, stream_id="real-stream")
        assert cleaned["modified_kwargs"]["message"]["raw_message"][0]["data"] == "原文"
        plugin._synthesize_audio.assert_not_awaited()
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize("language", ["zh", "en", "ja"])
def test_prompt_locales_share_protocol(language):
    prompt = tone_prompt(["happy"], language, automatic=True, default="happy")
    assert "[mmtts:happy]" in prompt
    assert "{default}" not in prompt


@pytest.mark.parametrize("voice_mode", ["off", "fixed", "dynamic"])
def test_voice_policy_overrides_saved_model_parameters(voice_mode):
    profile = Profile.model_validate(
        {
            "id": "m",
            "name": "MiMo",
            "kind": "mimo",
            "model": "mimo-v2.5-tts",
            "voice": "mimo_default",
            "base_url": "https://example.com/v1",
            "response_format": "wav",
            "models": [
                {
                    "id": "mimo-v2.5-tts",
                    "voice": "mimo_default",
                    "parameters": {"response_format": "wav", "tone": {"mode": "fixed", "emotion": "sad"}},
                }
            ],
            "voices": [
                {
                    "id": "mimo_default",
                    "model": "mimo-v2.5-tts",
                    "name": "声音",
                    "tone": {"mode": voice_mode, "emotion": "happy", "allowed": ["happy"]},
                }
            ],
        }
    )
    selected = profile.for_model(profile.model).with_selected_voice()
    assert selected.tone.mode == voice_mode
    rendered, emotion = apply_tone(selected, "happy" if voice_mode == "dynamic" else None)
    assert emotion == ("" if voice_mode == "off" else "happy")
    assert ("开心" in rendered.instructions) == (voice_mode != "off")
    assert profile.models[0].parameters.tone.emotion == "sad"
    if voice_mode != "dynamic":
        with pytest.raises(TTSError, match="直接提供正文"):
            apply_tone(selected, "happy")
