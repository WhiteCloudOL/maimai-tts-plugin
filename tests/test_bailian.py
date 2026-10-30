"""按百炼官方 HTTP 请求与响应验证各代语音协议。"""

from aiohttp import web
from pydantic import ValidationError

import base64
import io
import json
import logging
import wave

import pytest

from tts_under_test.domain.models import Profile, TTSError
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.infrastructure.transport import Transport
from tts_under_test.providers import PROVIDERS
from tts_under_test.providers.minimax import decode_hex
from tts_under_test.providers.base import normalize_audio
from tts_under_test.services.intonation import apply_tone
from tts_under_test.services.studio import Studio
from tts_under_test.services.tts import TTSService
from tts_under_test.settings.config import RuntimeSection


def profile_for(server, kind="qwen", **values):
    return Profile.model_validate(
        {
            "id": "bailian",
            "name": "阿里云",
            "kind": kind,
            "api_key": "sample-secret",
            "base_url": server.base + "/proxy/api/v1",
            "voice": "longanhuan_v3.1",
            "response_format": "wav",
            "model": "qwen-audio-3.1-tts-flash"
            if kind == "qwen"
            else "cosyvoice-v3.5-flash"
            if kind == "cosyvoice"
            else "MiniMax/speech-2.8-turbo",
            **values,
        }
    )


@pytest.mark.parametrize("kind", ["qwen", "cosyvoice"])
@pytest.mark.parametrize("audio_mode", ["url", "data"])
async def test_speech_synthesizer_http_contract(server, tmp_path, wav_audio, kind, audio_mode):
    voice = "longanhuan_v3.1" if kind == "qwen" else "cosyvoice-v3.5-flash-test-voice"

    async def handle(request):
        if request.path == "/download":
            assert "Authorization" not in request.headers
            return web.Response(body=wav_audio)
        assert request.path == "/proxy/api/v1/services/audio/tts/SpeechSynthesizer"
        body = await request.json()
        assert body["input"] == {
            "text": "今天很开心",
            "voice": voice,
            "format": "wav",
            "sample_rate": 24000,
            "volume": 65,
            "rate": 1.2,
            "pitch": 0.9,
            "instruction": "温柔地说话",
            "seed": 42,
            "language_hints": ["zh"],
            "hot_fix": {"pronunciation": [{"麦麦": "mai4 mai4"}]},
        }
        audio = (
            {"url": server.base + "/download"}
            if audio_mode == "url"
            else {"data": base64.b64encode(wav_audio).decode()}
        )
        return web.json_response({"output": {"finish_reason": "stop", "audio": audio}})

    server.handler = handle
    profile = profile_for(
        server,
        kind,
        voice=voice,
        speed=1.2,
        instructions="温柔地说话",
        options={
            "alibaba_transport": "http",
            "volume": 65,
            "pitch": 0.9,
            "seed": 42,
            "voice_language": "zh",
            "hot_fix": {"pronunciation": [{"麦麦": "mai4 mai4"}]},
        },
    )
    transport = Transport(RuntimeSection(), tmp_path)
    try:
        audio = await PROVIDERS[kind](transport).synthesize(profile, "今天很开心")
        assert audio.data == wav_audio
    finally:
        await transport.close()


@pytest.mark.parametrize("finish_reason", [None, "length", "error"])
async def test_bailian_rejects_partial_audio(server, wav_audio, finish_reason):
    async def handle(request):
        return web.json_response(
            {"output": {"finish_reason": finish_reason, "audio": {"data": base64.b64encode(wav_audio).decode()}}}
        )

    server.handler = handle
    transport = Transport(RuntimeSection())
    try:
        with pytest.raises(TTSError, match="完整结束"):
            await PROVIDERS["qwen"](transport).synthesize(profile_for(server), "你好")
    finally:
        await transport.close()


@pytest.mark.parametrize("kind", ["qwen", "cosyvoice"])
async def test_local_clone_query_update_delete_and_bound_voice_listing(server, tmp_path, wav_audio, kind):
    folder = tmp_path / "reference_audio"
    folder.mkdir()
    (folder / "sample.wav").write_bytes(wav_audio)
    profile = profile_for(
        server,
        kind,
        options={
            "reference_source": "file",
            "max_prompt_audio_length": 12,
            "enable_preprocess": True,
            "enable_volume_normalization": False,
            "clone_language_hint": "zh",
        },
    )
    state = "DEPLOYING"
    actions = []

    async def handle(request):
        nonlocal state
        assert request.path == "/proxy/api/v1/services/audio/tts/customization"
        body = await request.json()
        assert body["model"] == "voice-enrollment"
        inputs = body["input"]
        actions.append(inputs["action"])
        if inputs["action"] == "create_voice":
            assert inputs["target_model"] == profile.model
            assert inputs["audio"].startswith("data:audio/wav;base64,")
            assert base64.b64decode(inputs["audio"].split(",")[1]) == wav_audio
            assert inputs["language_hints"] == ["zh"]
            assert inputs["max_prompt_audio_length"] == 12
            assert inputs["enable_preprocess"] is True
            assert inputs["enable_volume_normalization"] == "false"
            return web.json_response({"output": {"voice_id": "mine"}})
        if inputs["action"] == "query_voice":
            return web.json_response(
                {
                    "output": {
                        "target_model": profile.model if inputs["voice_id"] == "mine" else "another-model",
                        "status": state,
                    }
                }
            )
        if inputs["action"] == "list_voice":
            return web.json_response({"output": {"voice_list": [{"voice_id": "mine"}, {"voice_id": "other"}]}})
        if inputs["action"] == "update_voice":
            assert inputs["audio"].startswith("data:audio/wav;base64,")
        return web.json_response({"output": {}})

    server.handler = handle
    transport = Transport(RuntimeSection(), tmp_path)
    provider = PROVIDERS[kind](transport)
    try:
        result = await provider.clone(profile, "maimai", "reference_audio/sample.wav", "")
        assert result.voice_id == "mine" and result.state == "training"
        assert (await provider.clone_status(profile, "mine")).state == "training"
        state = "OK"
        assert (await provider.clone_status(profile, "mine")).state == "ready"
        voices = await provider.voices(profile)
        assert [v["id"] for v in voices] == ["mine"]
        assert voices[0]["clone_state"] == "ready"
        with pytest.raises(TTSError, match="绑定的模型"):
            await provider.clone_status(profile, "other")
        assert (await provider.update_voice(profile, "mine", "reference_audio/sample.wav")).state == "training"
        await provider.delete_voice(profile, "mine")
        assert "delete_voice" in actions
        state = "UNDEPLOYED"
        assert (await provider.clone_status(profile, "mine")).state == "failed"
    finally:
        await transport.close()


@pytest.mark.parametrize("kind", ["qwen", "cosyvoice"])
async def test_voice_design_returns_preview_in_job_and_keeps_model_binding(server, tmp_path, wav_audio, kind):
    profile = profile_for(
        server,
        kind,
        options={
            "voice_prompt": "年轻清亮的女性，语气温柔自然。",
            "design_preview_text": "欢迎来到麦麦的语音工作台，很高兴与你分享今天的故事。",
        },
    )
    calls = []

    async def handle(request):
        body = await request.json()
        calls.append(body)
        assert body["input"] == {
            "action": "create_voice",
            "target_model": profile.model,
            "prefix": "maimai",
            "voice_prompt": profile.options.voice_prompt,
            "preview_text": profile.options.design_preview_text,
            "language_hints": ["zh"],
        }
        assert body["parameters"] == {"sample_rate": 24000, "response_format": "wav"}
        return web.json_response(
            {
                "output": {
                    "voice_id": "designed",
                    "target_model": profile.model,
                    "preview_audio": {
                        "data": base64.b64encode(wav_audio).decode(),
                        "sample_rate": 24000,
                        "response_format": "wav",
                    },
                }
            }
        )

    server.handler = handle
    service = TTSService(ConfigStore(tmp_path), RuntimeSection(), logging.getLogger("test"))
    studio = Studio(service.store, service)
    try:
        started = studio.launch(
            "design", profile.id, name="maimai", draft=profile.model_dump_json(), model_id=profile.model
        )
        await studio.jobs[started["job_id"]].task
        result = studio.job(started["job_id"])
        assert result["state"] == "done", result["error"]
        assert result["result"] == {"voice_id": "designed", "name": "maimai", "state": "training"}
        assert result["total"] == len(wav_audio)
        assert result["format"] == "wav"
        assert len(calls) == 1  # 使用创建结果的试听，而非再发起一次计费合成。
        assert studio.job(started["job_id"], 65536)["result"]["voice_id"] == "designed"
        assert not service.store.path.exists()
    finally:
        await studio.close()
        await service.close()


@pytest.mark.parametrize("mode", ["fixed", "dynamic"])
@pytest.mark.parametrize("output_format", ["hex", "url"])
async def test_minimax_native_emotion_and_audio_encoding(server, wav_audio, mode, output_format):
    async def handle(request):
        if request.path == "/audio":
            assert "Authorization" not in request.headers
            return web.Response(body=wav_audio)
        body = await request.json()
        inputs = body["input"]
        assert inputs["voice_setting"] == {
            "voice_id": "male-qn-qingse",
            "speed": 1.1,
            "vol": 1.5,
            "pitch": -2,
            "emotion": "sad" if mode == "dynamic" else "calm",
            "text_normalization": True,
            "latex_read": True,
        }
        assert inputs["audio_setting"] == {"sample_rate": 24000, "format": "wav", "channel": 1}
        assert inputs["pronunciation_dict"] == {"tone": ["处理/(chu3)(li3)"]}
        assert inputs["voice_modify"] == {"pitch": 5, "intensity": 0, "timbre": 0, "sound_effects": "robotic"}
        assert "instruction" not in inputs and "instructions" not in inputs
        return web.json_response(
            {
                "output": {
                    "base_resp": {"status_code": 0},
                    "data": {
                        "status": 2,
                        "audio": wav_audio.hex() if output_format == "hex" else server.base + "/audio",
                    },
                }
            }
        )

    server.handler = handle
    profile = profile_for(
        server,
        "bailian_minimax",
        voice="male-qn-qingse",
        speed=1.1,
        tone={"mode": mode, "emotion": "neutral", "allowed": ["neutral", "sad"]},
        options={
            "minimax_output": output_format,
            "minimax_vol": 1.5,
            "minimax_pitch": -2,
            "text_normalization": True,
            "latex_read": True,
            "pronunciation_tone": ["处理/(chu3)(li3)"],
            "voice_modify_pitch": 5,
            "sound_effects": "robotic",
        },
    )
    selected, _ = apply_tone(profile, "sad" if mode == "dynamic" else None)
    transport = Transport(RuntimeSection())
    try:
        assert (await PROVIDERS["bailian_minimax"](transport).synthesize(selected, "你好")).data == wav_audio
        assert profile.options.minimax_emotion == ""
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "response",
    [
        {"base_resp": {"status_code": 2013, "status_msg": "bad parameter"}},
        {"base_resp": {"status_code": 0}, "data": None},
        {"base_resp": {"status_code": 0}, "data": {"status": 1, "audio": "ab"}},
        {"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": "not-base64-or-hex"}},
    ],
)
async def test_minimax_nested_errors_and_partial_audio_are_rejected(server, response):
    async def handle(request):
        return web.json_response({"output": response})

    server.handler = handle
    transport = Transport(RuntimeSection())
    try:
        with pytest.raises(TTSError):
            await PROVIDERS["bailian_minimax"](transport).synthesize(profile_for(server, "bailian_minimax"), "你好")
    finally:
        await transport.close()


async def test_minimax_clone_management_contract_and_public_url_requirement(server, wav_audio):
    actions = []

    async def handle(request):
        if request.path == "/preview":
            assert "Authorization" not in request.headers
            return web.Response(body=wav_audio)
        body = await request.json()
        inputs = body["input"]
        actions.append(inputs["action"])
        if inputs["action"] == "voice_clone":
            assert inputs["voice_id"] == "maimai_voice"
            assert inputs["audio_url"] == "https://example.com/sample.wav"
            assert inputs["text"] == "这是一段克隆试听文本。"
            return web.json_response(
                {
                    "output": {
                        "base_resp": {"status_code": 0},
                        "input_sensitive": False,
                        "demo_audio": server.base + "/preview",
                    }
                }
            )
        assert body["model"] == "MiniMax/speech-2.8-turbo"
        output = {"base_resp": {"status_code": 0}}
        if inputs["action"] == "get_voice":
            output.update(
                system_voice=[{"voice_id": f"system-{i}", "voice_name": "预设"} for i in range(350)],
                voice_cloning=[{"voice_id": "maimai_voice"}],
            )
        else:
            assert inputs["voice_type"] == "voice_cloning"
            output["voice_id"] = "maimai_voice"
        return web.json_response({"output": output})

    server.handler = handle
    transport = Transport(RuntimeSection())
    profile = profile_for(
        server, "bailian_minimax", model="MiniMax/speech-02-hd", options={"clone_speaker_id": "maimai_voice"}
    )
    provider = PROVIDERS["bailian_minimax"](transport)
    try:
        result = await provider.clone(profile, "我的音色", "https://example.com/sample.wav", "这是一段克隆试听文本。")
        assert result.voice_id == "maimai_voice" and result.state == "ready"
        assert result.preview.data == wav_audio
        voices = await provider.voices(profile)
        assert len(voices) == 351 and voices[-1]["id"] == "maimai_voice" and voices[-1]["origin"] == "clone"
        await provider.delete_voice(profile, "maimai_voice")
        profile.options.reference_source = "file"
        with pytest.raises(TTSError, match="公网录音"):
            await provider.clone(profile, "我的音色", "sample.wav", "你好")
        assert actions == ["voice_clone", "get_voice", "delete_voice"]
    finally:
        await transport.close()


@pytest.mark.parametrize("text", ["abc", "a b c d", "GG", "00" * 11])
def test_hex_size_and_encoding_are_bounded(text):
    with pytest.raises(TTSError):
        decode_hex(text, 10)


def test_minimax_model_and_emotion_validation(server):
    with pytest.raises(ValidationError, match="whisper"):
        profile_for(server, "bailian_minimax", options={"minimax_emotion": "whisper"})
    with pytest.raises(ValidationError, match="支持范围"):
        profile_for(server, "bailian_minimax", tone={"mode": "fixed", "emotion": "gentle"})
    profile = profile_for(
        server, "bailian_minimax", model="MiniMax/speech-02-hd", tone={"mode": "fixed", "emotion": "whisper"}
    )
    assert apply_tone(profile, None)[0].options.minimax_emotion == "whisper"


def test_launch_schema_exposes_all_voice_operations():
    from pathlib import Path

    spec = json.loads((Path(__file__).resolve().parents[1] / "webui.json").read_text(encoding="utf-8"))
    page = spec["pages"][0]
    operations = page["actions"]["launch"]["parameters"]["operation"]["choices"]
    assert {"design", "clone_status", "update_voice", "delete_voice"} <= set(operations)


async def test_minimax_pcm_preserves_stereo_channels(server):
    profile = profile_for(server, "bailian_minimax", response_format="pcm", options={"minimax_channel": 2})
    audio = await normalize_audio(b"\0\0" * 8, profile)
    with wave.open(io.BytesIO(audio.data), "rb") as stream:
        assert stream.getnchannels() == 2
        assert stream.getnframes() == 4
        assert stream.getframerate() == 24000


def test_known_voice_instruction_and_effect_limits(server):
    with pytest.raises(ValidationError, match="尚未确认支持"):
        profile_for(server, voice="loongmary", model="qwen-audio-3.0-tts-flash", tone={"mode": "fixed"})
    clone = profile_for(
        server,
        "cosyvoice",
        model="cosyvoice-v3-flash",
        voice="cosyvoice-v3-flash-vd-maimai-123",
        tone={"mode": "fixed", "emotion": "gentle"},
    )
    assert "温柔" in apply_tone(clone, None)[0].instructions
    with pytest.raises(ValidationError, match="声音效果适用于"):
        profile_for(server, "bailian_minimax", response_format="pcm", options={"voice_modify_pitch": 5})
    with pytest.raises(ValidationError, match="语言增强"):
        profile_for(server, "bailian_minimax", options={"language_boost": "invalid"})


async def test_qwen_realtime_exposes_business_error_before_socket_closes(server):
    async def handle(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.receive_json()
        await socket.send_json({"code": "InvalidParameter", "message": "Model not exist.", "request_id": "req_test"})
        await socket.close()
        return socket

    server.handler = handle
    profile = profile_for(server, model="qwen3-tts-instruct-flash-realtime", voice="Cherry")
    transport = Transport(RuntimeSection())
    try:
        with pytest.raises(TTSError, match="InvalidParameter.*Model not exist"):
            await PROVIDERS["qwen"](transport).synthesize(profile, "你好")
    finally:
        await transport.close()
