"""MiniMax 官方 HTTP/SSE/WS、录音上传、音色管理与模型限制的协议验证。"""

from aiohttp import web
from pydantic import ValidationError

import io
import json
import struct
import wave

import pytest

from tts_under_test.domain.catalog import CATALOG
from tts_under_test.domain.models import Profile, TTSError
from tts_under_test.domain.voice_catalog import get_catalog
from tts_under_test.infrastructure.transport import Transport
from tts_under_test.infrastructure.reference_audio import ReferenceAudioStore
from tts_under_test.providers import PROVIDERS
from tts_under_test.providers.minimax import MiniMaxProvider
from tts_under_test.services.intonation import apply_tone
from tts_under_test.settings.config import RuntimeSection


def profile(server, **updates):
    values = dict(
        id="minimax",
        name="MiniMax",
        kind="minimax",
        base_url=server.base + "/proxy/v1",
        api_key="test-key",
        model="speech-2.8-hd",
        voice="male-qn-qingse",
        response_format="wav",
    )
    values.update(updates)
    return Profile(**values)


@pytest.mark.parametrize("encoding", ["hex", "url"])
@pytest.mark.parametrize("mode", ["fixed", "dynamic"])
async def test_http_payload_tone_mixing_and_audio(server, wav_audio, encoding, mode):
    async def handle(request):
        if request.path == "/audio":
            assert "Authorization" not in request.headers
            return web.Response(body=wav_audio)
        assert request.path == "/proxy/v1/t2a_v2" and request.headers["Authorization"] == "Bearer test-key"
        body = await request.json()
        assert "input" not in body and body["stream"] is False
        assert body["voice_setting"]["voice_id"] == "" and body["voice_setting"]["emotion"] == "fluent"
        assert body["audio_setting"]["channel"] == 2 and body["text_normalization_mode"] == "quality"
        assert "text_normalization" not in body["voice_setting"]
        assert body["language_boost"] == "Chinese" and body["voice_modify"]["pitch"] == 10
        assert body["timbre_weights"] == [{"voice_id": "one", "weight": 30}, {"voice_id": "two", "weight": 70}]
        return web.json_response(
            {
                "base_resp": {"status_code": 0},
                "trace_id": "trace",
                "data": {"status": 2, "audio": wav_audio.hex() if encoding == "hex" else server.base + "/audio"},
            }
        )

    server.handler = handle
    p = profile(
        server,
        model="speech-2.6-hd",
        tone={"mode": mode, "emotion": "lively", "allowed": ["lively"]},
        options={
            "minimax_output": encoding,
            "minimax_normalization": "quality",
            "latex_read": True,
            "minimax_channel": 2,
            "voice_modify_pitch": 10,
            "timbre_weights": [{"voice_id": "one", "weight": 30}, {"voice_id": "two", "weight": 70}],
        },
    )
    selected, _ = apply_tone(p, "lively" if mode == "dynamic" else None)
    http = Transport(RuntimeSection())
    try:
        audio = await MiniMaxProvider(http).synthesize(selected, "你好")
        assert audio.data == wav_audio and audio.request_id == "trace"
    finally:
        await http.close()


@pytest.mark.parametrize("broken", [False, True])
async def test_sse_aggregated_final_audio_does_not_repeat_chunks(server, wav_audio, broken):
    async def handle(request):
        body = await request.json()
        assert body["stream_options"] == {"exclude_aggregated_audio": False}
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        events = [{"base_resp": {"status_code": 0}, "data": {"status": 1, "audio": wav_audio[:50].hex()}}]
        if not broken:
            events.append({"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": wav_audio.hex()}})
        for event in events:
            await response.write(("data: " + json.dumps(event) + "\n\n").encode())
        return response

    server.handler = handle
    http = Transport(RuntimeSection())
    try:
        task = MiniMaxProvider(http).synthesize(profile(server, options={"minimax_transport": "sse"}), "你好")
        if broken:
            with pytest.raises(TTSError, match="断开"):
                await task
        else:
            assert (await task).data == wav_audio
    finally:
        await http.close()


@pytest.mark.parametrize("broken", [False, True])
async def test_websocket_handshake_and_final_state(server, wav_audio, broken):
    async def handle(request):
        assert request.path == "/proxy/ws/v1/t2a_v2"
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json({"event": "connection_success"})
        start = await socket.receive_json()
        assert start["event"] == "task_start" and "text" not in start and "stream" not in start
        await socket.send_json({"event": "task_started", "base_resp": {"status_code": 0}})
        assert await socket.receive_json() == {"event": "task_continue", "text": "你好"}
        assert await socket.receive_json() == {"event": "task_finish"}
        await socket.send_json({"event": "task_continued", "data": {"audio": wav_audio[:100].hex()}})
        if not broken:
            await socket.send_json(
                {"event": "task_continued", "data": {"audio": wav_audio[100:].hex()}, "is_final": True}
            )
            await socket.send_json({"event": "task_finished", "trace_id": "ws-trace"})
        await socket.close()
        return socket

    server.handler = handle
    http = Transport(RuntimeSection())
    try:
        task = MiniMaxProvider(http).synthesize(profile(server, options={"minimax_transport": "websocket"}), "你好")
        if broken:
            with pytest.raises(TTSError, match="断开"):
                await task
        else:
            result = await task
            assert result.data == wav_audio and result.request_id == "ws-trace"
    finally:
        await http.close()


def recording(seconds):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000 * seconds)
    return output.getvalue()


async def test_local_clone_upload_prompt_design_and_voice_management(tmp_path, server, wav_audio):
    file = tmp_path / "sample.wav"
    file.write_bytes(recording(10))
    prompt = tmp_path / "prompt.wav"
    prompt.write_bytes(recording(1))
    uploads, deleted = [], []

    async def handle(request):
        path = request.path.rsplit("/", 1)[-1]
        if path == "upload":
            reader = await request.multipart()
            fields = {}
            async for part in reader:
                fields[part.name] = bytes(await part.read())
            uploads.append(fields)
            assert fields["file"].startswith(b"RIFF")
            return web.json_response(
                {"base_resp": {"status_code": 0}, "file": {"file_id": 9007199254740993 + len(uploads)}}
            )
        body = await request.json()
        if path == "voice_clone":
            assert body["file_id"] == 9007199254740995
            assert body["clone_prompt"] == {"prompt_audio": 9007199254740994, "prompt_text": "示例"}
            assert body["text_validation"] == "参考文本" and body["accuracy"] == 0.8
            assert "text" not in body  # 音色创建可独立完成，试听文本按需填写。
            return web.json_response({"base_resp": {"status_code": 0}, "input_sensitive": {"type": 0}})
        if path == "voice_design":
            assert body["prompt"] == "温柔女声" and body["preview_text"] == "你好"
            return web.json_response(
                {"base_resp": {"status_code": 0}, "voice_id": "ttv-designed", "trial_audio": wav_audio.hex()}
            )
        if path == "get_voice":
            return web.json_response(
                {
                    "base_resp": {"status_code": 0},
                    "system_voice": [{"voice_id": f"preset-{i}", "voice_name": f"预设{i}"} for i in range(350)],
                    "voice_cloning": [{"voice_id": "my_voice"}],
                    "voice_generation": [{"voice_id": "ttv-designed"}],
                }
            )
        if path == "delete_voice":
            deleted.append(body)
            return web.json_response({"base_resp": {"status_code": 0}, "voice_id": body["voice_id"]})
        raise AssertionError(request.path)

    server.handler = handle
    p = profile(
        server,
        options={
            "reference_source": "file",
            "clone_speaker_id": "my_voice",
            "minimax_prompt_audio": str(prompt),
            "prompt_text": "示例",
            "minimax_validation_text": "参考文本",
            "minimax_validation_accuracy": 0.8,
        },
    )
    http = Transport(RuntimeSection(), tmp_path)
    provider = MiniMaxProvider(http)
    try:
        clone = await provider.clone(p, "我的声音", str(file), "")
        assert clone.voice_id == "my_voice" and clone.preview is None
        assert [entry["purpose"] for entry in uploads] == [b"prompt_audio", b"voice_clone"]
        p.options.voice_prompt, p.options.design_preview_text, p.options.clone_speaker_id = "温柔女声", "你好", ""
        design = await provider.design(p, "设计声音")
        assert design.voice_id == "ttv-designed" and design.preview.data == wav_audio
        voices = await provider.voices(p)
        assert len(voices) == 352 and voices[-1]["origin"] == "design" and voices[-2]["origin"] == "clone"
        await provider.delete_voice(p, "ttv-designed")
        assert deleted[-1]["voice_type"] == "voice_generation"
        with pytest.raises(TTSError, match="系统音色"):
            await provider.delete_voice(p, "preset-0")
    finally:
        await http.close()


@pytest.mark.parametrize("fmt", ["pcmu_raw", "pcmu_wav"])
async def test_telephony_formats_become_playable_pcm_wav(server, fmt):
    data = b"\xff" * 8000
    if fmt == "pcmu_wav":
        fmt_chunk = struct.pack("<HHIIHH", 7, 1, 8000, 8000, 1, 8)
        data = (
            b"RIFF"
            + struct.pack("<I", 36 + len(data))
            + b"WAVEfmt "
            + struct.pack("<I", 16)
            + fmt_chunk
            + b"data"
            + struct.pack("<I", len(data))
            + data
        )

    async def handle(request):
        return web.json_response({"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": data.hex()}})

    server.handler = handle
    http = Transport(RuntimeSection())
    try:
        result = await MiniMaxProvider(http).synthesize(
            profile(server, response_format=fmt, options={"sample_rate": 8000}), "你好"
        )
        assert result.response_format == "wav"
        with wave.open(io.BytesIO(result.data)) as stream:
            assert stream.getframerate() == 8000 and stream.getsampwidth() == 2 and stream.getnframes() == 8000
    finally:
        await http.close()


@pytest.mark.parametrize(
    "values",
    [
        {"options": {"minimax_emotion": "whisper"}},
        {"options": {"minimax_emotion": "fluent"}},
        {"options": {"minimax_force_cbr": True}},
        {"options": {"minimax_transport": "sse", "minimax_output": "url"}},
        {"options": {"minimax_transport": "sse", "minimax_normalization": "quality"}},
        {"model": "speech-01-hd", "options": {"language_boost": "Persian"}},
        {"response_format": "pcmu_raw"},
        {"options": {"timbre_weights": [{"voice_id": "one", "weight": 50}, {"voice_id": "one", "weight": 50}]}},
    ],
)
def test_native_model_parameter_boundaries(server, values):
    with pytest.raises(ValidationError):
        profile(server, **values)


def test_native_platform_keeps_distinct_models_and_shared_system_catalog():
    assert len(CATALOG["minimax"]["models"]) == 8
    assert len(get_catalog()["minimax"]["model_voices"]["speech-2.8-hd"]) == 327
    assert PROVIDERS["minimax"] is not PROVIDERS["bailian_minimax"]


@pytest.mark.parametrize(
    "response",
    [
        {"base_resp": {"status_code": 1008, "status_msg": "balance"}},
        {"base_resp": {"status_code": False}},
        {"base_resp": {"status_code": 0}, "data": {"status": 1, "audio": "0000"}},
        {"base_resp": {"status_code": 0}, "data": None},
    ],
)
async def test_errors_and_partial_audio_are_rejected(server, response):
    async def handle(request):
        return web.json_response(response)

    server.handler = handle
    http = Transport(RuntimeSection())
    try:
        with pytest.raises(TTSError):
            await MiniMaxProvider(http).synthesize(profile(server), "你好")
    finally:
        await http.close()


async def test_exact_audio_size_limit_allows_json_envelope(server):
    # 生成大小恰好为 1 MiB 的有效 WAV，JSON 元数据与 Hex 开销单独计算。
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\0" * (1024 * 1024 - 44))
    data = output.getvalue()

    async def handle(request):
        return web.json_response(
            {"base_resp": {"status_code": 0}, "trace_id": "size-boundary", "data": {"status": 2, "audio": data.hex()}}
        )

    server.handler = handle
    http = Transport(RuntimeSection(max_audio_size_mb=1))
    try:
        assert len((await MiniMaxProvider(http).synthesize(profile(server), "你好")).data) == 1024 * 1024
    finally:
        await http.close()


async def test_native_m4a_is_imported_uploaded_and_short_recording_is_rejected(tmp_path, server):
    import av

    output = io.BytesIO()
    with av.open(io.BytesIO(recording(10))) as source, av.open(output, mode="w", format="ipod") as target:
        encoder = target.add_stream("aac", rate=16000)
        encoder.layout = "mono"
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
        for frame in source.decode(source.streams.audio[0]):
            for converted in resampler.resample(frame):
                for packet in encoder.encode(converted):
                    target.mux(packet)
        for packet in encoder.encode(None):
            target.mux(packet)
    m4a = tmp_path / "voice.m4a"
    m4a.write_bytes(output.getvalue())
    library = ReferenceAudioStore(tmp_path / "data")
    imported = await library.import_file(str(m4a), allow_m4a=True)
    assert imported["path"].endswith(".m4a")
    with pytest.raises(TTSError, match="MP3 或 WAV"):
        await library.read(imported["path"])

    async def handle(request):
        form = await request.multipart()
        async for part in form:
            data = await part.read()
            if part.name == "file":
                assert part.filename == "recording.m4a" and data[4:8] == b"ftyp"
        return web.json_response({"base_resp": {"status_code": 0}, "file": {"file_id": 123456}})

    server.handler = handle
    http = Transport(RuntimeSection(), tmp_path / "data")
    try:
        assert await MiniMaxProvider(http).upload(profile(server), imported["path"], "voice_clone") == 123456
        short = tmp_path / "short.wav"
        short.write_bytes(recording(1))
        with pytest.raises(TTSError, match="10 秒"):
            await MiniMaxProvider(http).upload(profile(server), str(short), "voice_clone")
    finally:
        await http.close()
