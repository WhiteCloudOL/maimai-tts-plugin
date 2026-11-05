"""拼接同一 Breeze 音色的 WAV 区段；只在工作线程处理二进制音频。"""

from dataclasses import replace
from io import BytesIO
from typing import List

import wave

from ..domain.models import Audio, TTSError


def quiet_edge_frames(frames: bytes, width: int, channels: int, maximum: int, *, reverse: bool) -> int:
    """只检查边界附近约 -60dB 以下的采样，不剪切或覆盖任何原始音频。"""
    stride = width * channels
    count = min(len(frames) // stride, maximum)
    threshold = max(1, (1 << (width * 8 - 1)) // 1024)
    for index in range(count):
        start = len(frames) - (index + 1) * stride if reverse else index * stride
        for channel in range(channels):
            offset = start + channel * width
            sample = frames[offset] - 128 if width == 1 else int.from_bytes(frames[offset:offset + width], "little", signed=True)
            if abs(sample) > threshold:
                return index
    return count


def join_breeze_audio(parts: List[Audio], limit: int, pauses_ms: List[int]) -> Audio:
    if not parts:
        raise TTSError("Breeze 没有可合并的音频")
    if any(audio.response_format != "wav" for audio in parts):
        raise TTSError("Breeze 分段合成必须返回 WAV 音频")
    if len(pauses_ms) != len(parts) - 1 or any(not 0 <= value <= 1000 for value in pauses_ms):
        raise TTSError("Breeze 分段停顿参数无效")
    output = BytesIO()
    try:
        with wave.open(BytesIO(parts[0].data), "rb") as first:
            parameters = (first.getnchannels(), first.getsampwidth(), first.getframerate(), first.getcomptype())
        with wave.open(output, "wb") as target:
            target.setnchannels(parameters[0])
            target.setsampwidth(parameters[1])
            target.setframerate(parameters[2])
            previous_tail, total_bytes = 0, 44
            for index, audio in enumerate(parts):
                with wave.open(BytesIO(audio.data), "rb") as source:
                    current = (
                        source.getnchannels(),
                        source.getsampwidth(),
                        source.getframerate(),
                        source.getcomptype(),
                    )
                    if current != parameters:
                        raise TTSError("Breeze 分段音频的采样率或声道不一致")
                    frames = source.readframes(source.getnframes())
                    if len(frames) != source.getnframes() * current[0] * current[1]:
                        raise TTSError("Breeze 分段 WAV 音频不完整")
                    # 静音计入前段尾部与本段头部；仅补足缺少的间隔，原有停顿较长时不再叠加。
                    gap_frames = pauses_ms[index - 1] * current[2] // 1000 if index else 0
                    leading = quiet_edge_frames(frames, current[1], current[0], gap_frames, reverse=False)
                    missing = max(0, gap_frames - previous_tail - leading)
                    gap_bytes = missing * current[0] * current[1]
                    if total_bytes + gap_bytes + len(frames) > limit:
                        raise TTSError("Breeze 合并音频超过大小上限")
                    silence = b"\x80" if current[1] == 1 else b"\0"
                    target.writeframesraw(silence * gap_bytes)
                    target.writeframesraw(frames)
                    total_bytes += gap_bytes + len(frames)
                    next_gap = pauses_ms[index] * current[2] // 1000 if index < len(pauses_ms) else 0
                    previous_tail = quiet_edge_frames(frames, current[1], current[0], next_gap, reverse=True)
    except (wave.Error, EOFError) as exc:
        raise TTSError("Breeze 分段音频不是有效的 PCM WAV") from exc
    return replace(parts[0], data=output.getvalue(), content_type="audio/wav", response_format="wav")
