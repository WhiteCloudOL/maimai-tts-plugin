"""实际 SILK 编解码应保留输入末尾声音，发送和缓存不能复用截尾结果。"""

from aiohttp import web
from array import array
from io import BytesIO

import av
import base64
import math
import sys
import wave

import pysilk
import pytest

from maimai_tts_contract.domain.models import Audio, Profile, RouteTarget, StudioConfig, TTSError
from maimai_tts_contract.infrastructure.voice_codec import encode_silk, voice_segment


RATE = 24000
LIMIT = 1024 * 1024


def samples(raw: bytes) -> array:
    values = array("h")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def pcm_tail(milliseconds: int) -> bytes:
    # 最后一包仍有明确有效声音，不能依赖上游恰好自带尾部静音。
    count = milliseconds * RATE // 1000
    values = array("h", [0] * (RATE // 2))
    values.extend(
        round(12000 * math.sin(2 * math.pi * (700 * i / RATE + 750 * (i / RATE) ** 2 / (count / RATE))))
        for i in range(count)
    )
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def wav_audio(raw: bytes, rate: int = RATE, channels: int = 1) -> Audio:
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((channels, 2, rate, 0, "NONE", ""))
        stream.writeframes(raw)
    return Audio(output.getvalue(), "audio/wav", "wav", "tail-test", "request-test", "完整正文。")


def decode(data: bytes) -> bytes:
    output = BytesIO()
    pysilk.decode(BytesIO(data), output, RATE)
    return output.getvalue()


def complete_reference(raw: bytes) -> bytes:
    # 使用独立 SDK 编码并留足观察区间，找出所有显著尾音实际出现的位置。
    # 预期不依赖插件补包/排空的具体实现。
    output = BytesIO()
    pysilk.encode(BytesIO(raw + b"\0" * (RATE * 2 // 5)), output, RATE, 24000, tencent=True)
    return decode(output.getvalue())


@pytest.mark.parametrize("milliseconds", [20, 40, 100])
def test_actual_codec_preserves_audible_tail_on_exact_packet_boundaries(milliseconds):
    raw = pcm_tail(milliseconds)
    original = wav_audio(raw)
    converted = encode_silk(original, LIMIT)
    actual = decode(converted.data)
    reference = complete_reference(raw)
    last_voice = max(index for index, value in enumerate(samples(reference)) if abs(value) > 500)
    assert len(actual) // 2 > last_voice, "编码器缓冲中的有效尾音被截断"
    assert actual == reference[:len(actual)], "排空尾部不能改写此前的声音"
    assert len(raw) <= len(actual) < len(raw) + RATE * 2 * 60 // 1000
    assert converted.display_text == original.display_text
    segment = voice_segment(converted)
    assert base64.b64decode(segment["binary_data_base64"]) == converted.data


@pytest.mark.parametrize("rate", [8000, 16000, 24000, 44100, 48000])
@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("integer_seconds", [False, True])
def test_resampling_and_partial_packets_keep_full_duration(rate, channels, integer_seconds):
    count = rate * 123 // 1000
    values = array("h")
    for index in range(count):
        value = round(12000 * math.sin(2 * math.pi * 700 * index / rate))
        values.extend([value] * channels)
    if sys.byteorder != "little":
        values.byteswap()
    actual = decode(encode_silk(
        wav_audio(values.tobytes(), rate, channels), LIMIT, integer_seconds=integer_seconds,
    ).data)
    expected_samples = math.ceil(count * RATE / rate)
    if integer_seconds:
        assert len(actual) == RATE * 2
    else:
        assert expected_samples < len(actual) // 2 < expected_samples + RATE * 60 // 1000


@pytest.mark.parametrize("response_format,container_format,codec", [
    ("mp3", "mp3", "libmp3lame"), ("opus", "ogg", "libopus"),
    ("flac", "flac", "flac"), ("aac", "adts", "aac"),
])
def test_compressed_inputs_preserve_decoded_voice_when_padding_to_integer_seconds(response_format, container_format, codec):
    compressed = BytesIO()
    with av.open(BytesIO(wav_audio(pcm_tail(20)).data)) as source:
        with av.open(compressed, mode="w", format=container_format) as destination:
            stream = destination.add_stream(codec, rate=48000)
            stream.layout = "stereo"
            for frame in source.decode(audio=0):
                destination.mux(stream.encode(frame))
            destination.mux(stream.encode(None))
    original = Audio(compressed.getvalue(), "application/octet-stream", response_format)
    regular = decode(encode_silk(original, LIMIT).data)
    rounded = decode(encode_silk(original, LIMIT, integer_seconds=True).data)
    assert rounded[:len(regular)] == regular
    assert len(rounded) == RATE * 2
    assert max(abs(value) for value in samples(rounded[-RATE * 2 // 100:])) < 500


def test_silk_passthrough_does_not_append_another_tail():
    converted = encode_silk(wav_audio(pcm_tail(20)), LIMIT)
    assert encode_silk(converted, LIMIT).data == converted.data


@pytest.mark.parametrize("milliseconds,seconds", [(20, 1), (1980, 3), (2460, 3), (2500, 4)])
def test_integer_seconds_extend_only_the_tail_and_preserve_audible_content(milliseconds, seconds):
    original = wav_audio(pcm_tail(milliseconds))
    regular = decode(encode_silk(original, LIMIT).data)
    rounded = encode_silk(original, LIMIT, integer_seconds=True)
    actual = decode(rounded.data)
    assert len(actual) == seconds * RATE * 2
    assert actual[:len(regular)] == regular
    assert max(abs(value) for value in samples(actual[-RATE * 2 // 100:])) < 500
    assert rounded.display_text == original.display_text
    assert encode_silk(rounded, LIMIT, integer_seconds=True).data == rounded.data


def test_source_duration_limit_is_checked_before_codec_drain(monkeypatch):
    import maimai_tts_contract.infrastructure.voice_codec as codec
    monkeypatch.setattr(codec, "MAX_VOICE_SECONDS", 1)
    assert decode(encode_silk(wav_audio(b"\0" * RATE * 2), LIMIT).data)
    with pytest.raises(TTSError, match="秒上限"):
        encode_silk(wav_audio(b"\0" * (RATE + 1) * 2), LIMIT)


@pytest.mark.asyncio
async def test_source_delivery_is_unchanged_and_legacy_delivery_cache_is_not_reused(service):
    raw = pcm_tail(20)
    original = wav_audio(raw)
    assert await service.prepare_delivery(original, "source") is original
    # 模拟升级前留下的转码缓存；新策略必须重新编码，后续相同请求仍命中缓存。
    import hashlib
    legacy_key = service.cache_keys.digest({"audio": hashlib.sha256(original.data).hexdigest(), "format": "wav"})
    stale = Audio(b"\x02#!SILK_V3stale", "audio/silk", "silk")
    async def old_result():
        return stale
    await service.cache.get("delivery", legacy_key, old_result, ttl=300, size=lambda value: len(value.data))
    first = await service.prepare_delivery(original, "silk")
    assert first.data != stale.data
    misses = service.cache.misses
    second = await service.prepare_delivery(original, "silk")
    assert second.data == first.data and service.cache.misses == misses
    reference = samples(complete_reference(raw))
    last_voice = max(index for index, value in enumerate(reference) if abs(value) > 500)
    assert len(decode(first.data)) // 2 > last_voice


@pytest.mark.asyncio
async def test_saved_integer_seconds_switch_partitions_delivery_cache(service):
    original = wav_audio(pcm_tail(20))
    first = await service.prepare_delivery(original, "silk")
    assert len(decode(first.data)) == RATE * 2
    misses = service.cache.misses
    for enabled in (False, True):
        saved = service.store.snapshot()
        saved["feature"]["silk_integer_seconds"] = enabled
        await service.store.save(saved, saved["revision"])
        result = await service.prepare_delivery(original, "silk")
        if enabled:
            assert result.data == first.data
        else:
            assert len(decode(result.data)) < RATE * 2
        assert result.display_text == original.display_text
        assert await service.prepare_delivery(original, "source") is original
    assert service.cache.misses == misses + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,model,voice", [("breeze", "breeze-tts-2", "test-voice"), ("openai", "gpt-4o-mini-tts", "alloy")])
async def test_delivery_option_uses_config_snapshot_and_reuses_source_cache(serve, service, kind, model, voice):
    original = wav_audio(pcm_tail(20))
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.Response(body=original.data, content_type="audio/wav")

    selected = Profile(
        id="integer-seconds", name="整数秒语音测试", kind=kind, base_url=await serve(handler) + "/v1",
        api_key="test-only-not-a-real-key", model=model, voice=voice, response_format="wav",
        options={"breeze_api_mode": "cloud", "breeze_mode": "plain"} if kind == "breeze" else {},
    )
    saved = StudioConfig(profiles=[selected], primary=RouteTarget(provider_id=selected.id, model_id=model))
    first = await service.synthesize("完整正文。", config=saved, delivery_format="silk")
    assert len(decode(first.data)) == RATE * 2
    assert (await service.prepare_delivery(first, "silk")).data == first.data

    saved.feature.silk_integer_seconds = False
    regular = await service.synthesize("完整正文。", config=saved, delivery_format="silk")
    assert service.store.config.feature.silk_integer_seconds is True
    assert len(decode(regular.data)) < RATE * 2

    saved.feature.silk_integer_seconds = True
    repeated = await service.synthesize("完整正文。", config=saved, delivery_format="silk")
    source = await service.synthesize("完整正文。", config=saved, delivery_format="source")
    preview = await service.synthesize("完整正文。", config=saved)
    assert repeated.data == first.data
    assert source.data == preview.data == original.data
    assert len(calls) == 1
