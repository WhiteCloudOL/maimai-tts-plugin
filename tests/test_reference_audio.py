"""本地参考录音的路径、导入与各平台真实请求格式。"""

from aiohttp import web

import base64
import json
import logging

import pytest

from tts_under_test.domain.models import Profile, TTSError
from tts_under_test.infrastructure.reference_audio import ReferenceAudioStore
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.infrastructure.transport import Transport
from tts_under_test.providers.gpt_sovits import GPTSoVITSProvider
from tts_under_test.providers.siliconflow import SiliconFlowProvider
from tts_under_test.services.studio import Studio
from tts_under_test.services.tts import TTSService
from tts_under_test.settings.config import RuntimeSection


async def test_local_paths_and_import_preserve_source(tmp_path, wav_audio):
    library = ReferenceAudioStore(tmp_path / "plugin-data")
    await library.prepare()
    source = tmp_path / "清晰录音.wav"
    source.write_bytes(wav_audio)
    first = await library.import_file(str(source))
    second = await library.import_file(str(source))
    assert first["path"] != second["path"]
    assert first["path"].startswith("reference_audio/")
    assert source.read_bytes() == wav_audio
    assert await library.read(first["path"]) == wav_audio
    assert await library.read(str(source)) == wav_audio
    assert len(second["library"]["files"]) == 2
    again = await library.import_file(first["path"])
    assert again["path"] == first["path"]
    assert await library.local_path(first["path"]) == str((library.data_dir / first["path"]).resolve())


async def test_invalid_local_reference_fails_before_request(tmp_path, wav_audio):
    library = ReferenceAudioStore(tmp_path / "plugin-data")
    await library.prepare()
    (tmp_path / "outside.wav").write_bytes(wav_audio)
    with pytest.raises(TTSError, match="相对路径不能超出"):
        await library.read("../outside.wav")
    with pytest.raises(TTSError, match="不存在"):
        await library.read("reference_audio/sample.wav")
    with pytest.raises(TTSError, match="普通文件"):
        await library.read("reference_audio")
    invalid = library.directory / "fake.wav"
    invalid.write_text("not audio")
    with pytest.raises(TTSError, match="MP3 或 WAV"):
        await library.import_file(str(invalid))
    valid = library.directory / "sample.wav"
    valid.write_bytes(wav_audio)
    with pytest.raises(TTSError, match="大小上限"):
        await library.read("reference_audio/sample.wav", limit=100)


@pytest.mark.parametrize("kind", ["mimo", "qwen", "cosyvoice", "siliconflow"])
@pytest.mark.parametrize("path_mode", ["relative", "absolute"])
async def test_unsaved_local_reference_preview_or_clone(tmp_path, server, wav_audio, kind, path_mode):
    reference = tmp_path / "reference_audio" / "sample.wav"
    reference.parent.mkdir()
    reference.write_bytes(wav_audio)
    data_url = "data:audio/wav;base64," + base64.b64encode(wav_audio).decode()

    async def handle(request):
        # 本地模式无需 GET 下载，也不把本机路径传给云端。
        assert request.method == "POST"
        body = await request.json()
        if kind == "mimo":
            assert body["audio"]["voice"] == base64.b64encode(wav_audio).decode()
            return web.json_response(
                {
                    "choices": [
                        {"finish_reason": "stop", "message": {"audio": {"data": base64.b64encode(wav_audio).decode()}}}
                    ]
                }
            )
        if kind == "qwen":
            assert body["input"]["audio"]["data"] == data_url
            return web.json_response({"output": {"voice": "local-qwen-voice"}})
        if kind == "cosyvoice":
            assert body["input"]["audio"] == data_url
            assert "url" not in body["input"]
            return web.json_response({"output": {"voice_id": "local-cosy-voice"}})
        assert body["audio"] == data_url
        assert body["text"] == "参考录音"
        return web.json_response({"uri": "speech:local-voice"})

    server.handler = handle
    store = ConfigStore(tmp_path)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    studio = Studio(store, service)
    profile = {
        "id": "unsaved-local",
        "name": "本地参考",
        "kind": kind,
        "base_url": server.base + "/api/v1",
        "clone_url": server.base + "/clone",
        "api_key": "test-secret",
        "response_format": "wav",
        "model": "mimo-v2.5-tts-voiceclone"
        if kind == "mimo"
        else "qwen3-tts-vc-realtime-2026-01-15"
        if kind == "qwen"
        else "cosyvoice-v3-flash"
        if kind == "cosyvoice"
        else "FunAudioLLM/CosyVoice2-0.5B",
        "options": {
            "reference_source": "file",
            "reference_file": "reference_audio/sample.wav" if path_mode == "relative" else str(reference),
        },
    }
    if kind == "siliconflow":
        profile["options"]["sample_rate"] = 44100
    try:
        args = (
            {"text": "你好"}
            if kind == "mimo"
            else {"name": "local", "url": profile["options"]["reference_file"], "text": "参考录音"}
        )
        launched = studio.launch(
            "preview" if kind == "mimo" else "clone", profile["id"], draft=json.dumps(profile), **args
        )
        await studio.jobs[launched["job_id"]].task
        result = studio.job(launched["job_id"])
        assert result["state"] == "done", result["error"]
        if kind == "mimo":
            assert result["total"] == len(wav_audio)
        else:
            assert result["result"]["voice_id"]
        assert store.config.profiles == []
        assert not store.path.exists()
    finally:
        await studio.close()
        await service.close()


async def test_siliconflow_local_dynamic_reference_and_voice_switch(tmp_path, server, wav_audio):
    source = tmp_path / "voice.wav"
    source.write_bytes(wav_audio)
    received = []

    async def handle(request):
        received.append(await request.json())
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    profile = Profile(
        id="sf",
        name="硅基",
        kind="siliconflow",
        endpoint=server.base + "/tts",
        model="FunAudioLLM/CosyVoice2-0.5B",
        voice="speech:voice-id",
        options={
            "reference_source": "file",
            "reference_file": str(source),
            "reference_text": "参考录音",
            "sample_rate": 44100,
        },
    )
    transport = Transport(RuntimeSection(), tmp_path)
    try:
        await SiliconFlowProvider(transport).synthesize(profile, "你好")
        assert "voice" not in received[0]
        assert base64.b64decode(received[0]["references"][0]["audio"].split(",")[1]) == wav_audio
        profile.options.reference_enabled = False
        profile.options.reference_file = "reference_audio/not-present.wav"
        await SiliconFlowProvider(transport).synthesize(profile, "你好")
        assert received[1]["voice"] == "speech:voice-id"
        assert "references" not in received[1]
    finally:
        await transport.close()


async def test_gpt_sovits_distinguishes_local_and_remote_server_paths(tmp_path, server, wav_audio):
    source = tmp_path / "reference_audio" / "sample.wav"
    source.parent.mkdir()
    source.write_bytes(wav_audio)
    received = []

    async def handle(request):
        received.append(await request.json())
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    profile = Profile(
        id="gpt",
        name="GPT",
        kind="gpt_sovits",
        endpoint=server.base + "/tts",
        response_format="wav",
        options={"reference_source": "file"},
    )
    transport = Transport(RuntimeSection(), tmp_path)
    try:
        await GPTSoVITSProvider(transport).synthesize(profile, "你好")
        assert received[0]["ref_audio_path"] == str(source.resolve())
        profile.options.reference_source = "server"
        profile.options.reference_path = "/remote/reference.wav"
        await GPTSoVITSProvider(transport).synthesize(profile, "你好")
        assert received[1]["ref_audio_path"] == "/remote/reference.wav"
    finally:
        await transport.close()
