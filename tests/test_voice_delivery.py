"""聊天发送的实际 SILK 编解码及 SDK/Hook 载荷契约。"""

from io import BytesIO
from maibot_sdk.context import PluginContext, PluginPaths
from unittest.mock import AsyncMock

import av
import base64
import pysilk
import pytest

from tts_under_test.domain.models import Audio, TTSError
from tts_under_test.infrastructure.voice_codec import SILK_HEADER, encode_silk, voice_segment


def test_silk_roundtrip_preserves_tail_and_duration(wav_audio):
    encoded = encode_silk(Audio(wav_audio, "audio/wav", "wav"), 1024 * 1024)
    assert encoded.data.startswith(SILK_HEADER)
    assert encoded.response_format == "silk"
    decoded = BytesIO()
    pysilk.decode(BytesIO(encoded.data), decoded, 24000)
    assert len(decoded.getvalue()) == 48000 * 2
    assert base64.b64decode(voice_segment(encoded)["binary_data_base64"]) == encoded.data


@pytest.mark.parametrize(
    "format_name, codec", [("mp3", "libmp3lame"), ("flac", "flac"), ("ogg", "libopus"), ("adts", "aac")]
)
def test_provider_compressed_formats_become_silk(wav_audio, format_name, codec):
    compressed = BytesIO()
    with av.open(BytesIO(wav_audio)) as source, av.open(compressed, "w", format_name) as target:
        stream = target.add_stream(codec, rate=48000)
        for frame in source.decode(audio=0):
            for packet in stream.encode(frame):
                target.mux(packet)
        for packet in stream.encode(None):
            target.mux(packet)
    fmt = {"ogg": "opus", "adts": "aac"}.get(format_name, format_name)
    result = encode_silk(Audio(compressed.getvalue(), "audio/" + fmt, fmt), 1024 * 1024)
    assert result.data.startswith(SILK_HEADER)
    decoded = BytesIO()
    pysilk.decode(BytesIO(result.data), decoded, 24000)
    assert 48000 * 2 <= len(decoded.getvalue()) < 52000 * 2


def test_invalid_audio_is_not_sent_as_voice(wav_audio):
    with pytest.raises(TTSError, match="转码失败"):
        encode_silk(Audio(b"not an audio file", "audio/mpeg", "mp3"), 1024 * 1024)
    with pytest.raises(TTSError, match="大小上限"):
        encode_silk(Audio(wav_audio, "audio/wav", "wav"), 100)


@pytest.mark.parametrize("keep_text", [False, True])
async def test_command_tool_and_probability_share_native_voice(plugin_module, tmp_path, wav_audio, keep_text):
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    rpc = AsyncMock(return_value={"success": True, "sent": True, "message_id": "receipt"})
    plugin._set_context(PluginContext("maimai-tts-plugin", rpc_call=rpc, paths=PluginPaths(tmp_path, tmp_path)))
    await plugin.on_load()
    try:
        await plugin.store.save(
            {
                "feature": {
                    "tool_keep_text": keep_text,
                    "probability_keep_text": keep_text,
                    "probability_enabled": True,
                    "probability": 1,
                }
            },
            0,
        )
        plugin.service.synthesize = AsyncMock(return_value=Audio(wav_audio, "audio/wav", "wav"))
        result = await plugin.handle_tts_tool("你好", stream_id="real-stream")
        assert result["success"]
        args = rpc.call_args.args[2]["args"]
        assert args["stream_id"] == "real-stream"
        assert args["return_details"] is True
        sent_voice = args["segments"][-1]
        assert base64.b64decode(sent_voice["binary_data_base64"]).startswith(SILK_HEADER)
        assert len(args["segments"]) == (2 if keep_text else 1)
        await plugin.prepare_probability_tts_reply(response="你好", session_id="real-stream")
        message = {"raw_message": [{"type": "text", "data": "你好"}], "message_info": {}}
        result = await plugin.handle_probability_tts(message=message, stream_id="real-stream")
        segments = result["modified_kwargs"]["message"]["raw_message"]
        assert segments[-1] == sent_voice
        assert len(segments) == (2 if keep_text else 1)
        assert message["raw_message"] == [{"type": "text", "data": "你好"}]
        command = await plugin.handle_tts_command(stream_id="real-stream", matched_groups={"text": "你好"})
        assert command[0] is True
    finally:
        await plugin.on_unload()


async def test_negative_receipt_never_claims_success(plugin_module, tmp_path, wav_audio):
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    rpc = AsyncMock(return_value={"success": False, "sent": False, "message_id": None})
    plugin._set_context(PluginContext("maimai-tts-plugin", rpc_call=rpc, paths=PluginPaths(tmp_path, tmp_path)))
    await plugin.on_load()
    try:
        plugin.service.synthesize = AsyncMock(return_value=Audio(wav_audio, "audio/wav", "wav"))
        result = await plugin.handle_tts_tool("你好", stream_id="real-stream")
        assert result["success"] is False
        assert "发送到聊天流失败" in result["error"]
    finally:
        await plugin.on_unload()
