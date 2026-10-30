"""多模型配置、音色归属、默认发送与未保存模型试听。"""

from aiohttp import web
from pydantic import ValidationError

import json
import logging

import pytest

from tts_under_test.domain.catalog import CATALOG
from tts_under_test.domain.models import Profile, TTSError
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.services.studio import Studio
from tts_under_test.services.tts import TTSService
from tts_under_test.settings.config import RuntimeSection


@pytest.mark.parametrize("kind", list(CATALOG))
async def test_every_platform_persists_multiple_models_and_voices(tmp_path, kind):
    cat = CATALOG[kind]
    profile = {
        "id": "platform",
        "name": cat["name"],
        "kind": kind,
        "endpoint": cat["endpoint"],
        "model": "first",
        "voice": "first-a",
        "response_format": cat["formats"][0],
        "options": {"sample_rate": 44100 if kind == "siliconflow" else 24000},
        "models": [{"id": "first", "voice": "first-a"}, {"id": "second", "voice": "second-b"}],
        "voices": [
            {"id": voice, "name": voice, "model": model}
            for model, voices in [("first", ["first-a", "shared"]), ("second", ["second-b", "shared"])]
            for voice in voices
        ],
    }
    store = ConfigStore(tmp_path)
    await store.save({"profiles": [profile], "default_provider": "platform"}, 0)
    restored = ConfigStore(tmp_path)
    await restored.load()
    original = restored.profile("platform")
    selected = original.for_model("second")
    assert (original.model, original.voice) == ("first", "first-a")
    assert (selected.model, selected.voice) == ("second", "second-b")
    assert len(selected.voices) == 4
    assert len(restored.snapshot()["profiles"][0]["models"]) == 2


async def test_preview_uses_selected_model_and_preserves_platform_default(tmp_path, server, wav_audio):
    received = []

    async def handle(request):
        received.append(await request.json())
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    store = ConfigStore(tmp_path)
    profile = {
        "id": "draft",
        "name": "多模型平台",
        "kind": "compatible",
        "endpoint": server.base + "/tts",
        "model": "first",
        "voice": "voice-a",
        "models": [{"id": "first", "voice": "voice-a"}, {"id": "second", "voice": "voice-b"}],
    }
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    studio = Studio(store, service)
    try:
        started = studio.launch("preview", "draft", text="你好", draft=json.dumps(profile), model_id="second")
        await studio.jobs[started["job_id"]].task
        assert studio.job(started["job_id"])["state"] == "done"
        assert (received[0]["model"], received[0]["voice"]) == ("second", "voice-b")
        assert not store.path.exists()
        await store.save({"profiles": [profile], "default_provider": "draft"}, 0)
        await service.synthesize("默认回复")
        assert (received[1]["model"], received[1]["voice"]) == ("first", "voice-a")
    finally:
        await studio.close()
        await service.close()


def test_recording_voices_are_isolated_by_model():
    profile = Profile(
        id="mimo",
        name="MiMo",
        kind="mimo",
        endpoint=CATALOG["mimo"]["endpoint"],
        model="mimo-v2.5-tts-voiceclone",
        voice="record-a",
        response_format="wav",
        models=[
            {"id": "mimo-v2.5-tts-voiceclone", "voice": "record-a"},
            {"id": "second-voiceclone", "voice": "record-b"},
        ],
        voices=[
            {
                "id": "record-a",
                "name": "音色甲",
                "model": "mimo-v2.5-tts-voiceclone",
                "reference": {"source": "file", "path": "reference_audio/a.wav"},
            },
            {
                "id": "record-b",
                "name": "音色乙",
                "model": "second-voiceclone",
                "reference": {"source": "file", "path": "reference_audio/b.wav"},
            },
        ],
    )
    assert profile.with_selected_voice().options.reference_file == "reference_audio/a.wav"
    assert (
        profile.for_model("second-voiceclone").with_selected_voice().options.reference_file == "reference_audio/b.wav"
    )
    assert profile.options.reference_file == "reference_audio/sample.wav"


async def test_model_endpoint_changes_require_explicit_credentials(tmp_path):
    store = ConfigStore(tmp_path)
    profile = {
        "id": "one",
        "name": "平台",
        "kind": "compatible",
        "endpoint": "https://trusted.test/tts",
        "api_key": "secret",
        "model": "first",
    }
    saved = await store.save({"profiles": [profile]}, 0)
    draft = saved["profiles"][0]
    draft["models"].append({"id": "second", "endpoint": "https://other.test/tts"})
    with pytest.raises(TTSError, match="重新填写密钥"):
        store.prepare_profile(draft)
    draft["api_key"] = "new-secret"
    assert store.prepare_profile(draft).for_model("second").endpoint == "https://other.test/tts"


def test_qwen_realtime_model_selection_updates_its_query():
    profile = Profile(
        id="qw",
        name="千问",
        kind="qwen",
        endpoint="https://gateway.test/generation",
        response_format="wav",
        model="qwen3-tts-flash",
        voice="Cherry",
        models=[
            {"id": "qwen3-tts-flash"},
            {
                "id": "qwen3-tts-vc-realtime-next",
                "voice": "cloned-id",
                "endpoint": "wss://gateway.test/realtime?model=old&region=cn",
            },
        ],
    )
    selected = profile.for_model("qwen3-tts-vc-realtime-next")
    assert selected.endpoint == "wss://gateway.test/realtime?region=cn&model=qwen3-tts-vc-realtime-next"
    assert selected.voice == "cloned-id"
    assert profile.model == "qwen3-tts-flash"
    with pytest.raises(TTSError, match="模型库"):
        profile.for_model("unknown")
    with pytest.raises(ValidationError, match="唯一"):
        Profile.model_validate({**profile.model_dump(), "models": [{"id": profile.model}, {"id": profile.model}]})


def test_independent_model_and_voice_dialog_parameters():
    profile = Profile(
        id="models",
        name="模型库",
        kind="compatible",
        endpoint="https://shared.test/tts",
        model="first",
        voice="voice-a",
        models=[
            {"id": "first", "voice": "voice-a"},
            {
                "id": "second",
                "voice": "voice-b",
                "endpoint": "https://model.test/tts",
                "parameters": {
                    "response_format": "wav",
                    "speed": 1.25,
                    "instructions": "模型风格",
                    "options": {"sample_rate": 22050},
                },
            },
        ],
        voices=[{"id": "voice-b", "name": "模型乙音色", "model": "second", "instructions": "音色风格", "speed": 0.75}],
    )
    selected = profile.for_model("second").with_selected_voice()
    assert selected.endpoint == "https://model.test/tts"
    assert selected.response_format == "wav"
    assert selected.options.sample_rate == 22050
    assert selected.instructions == "音色风格"
    assert selected.speed == 0.75
    assert profile.for_model("first").response_format == "mp3"
    assert profile.instructions == ""


def test_openai_native_and_custom_voice_objects_can_share_a_model():
    profile = Profile(
        id="openai",
        name="OpenAI",
        kind="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini-tts",
        voice="custom-id",
        voices=[
            {"id": "custom-id", "name": "自定义声音", "model": "gpt-4o-mini-tts", "custom_voice": True},
            {"id": "alloy", "name": "Alloy", "model": "gpt-4o-mini-tts", "custom_voice": False},
        ],
    )
    assert profile.with_selected_voice().options.custom_voice is True
    native = Profile.model_validate({**profile.model_dump(), "voice": "alloy"}).with_selected_voice()
    assert native.options.custom_voice is False
    assert profile.options.custom_voice is False
