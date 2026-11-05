"""拼接同一 Breeze 音色的 WAV 区段；只在工作线程处理二进制音频。"""

from dataclasses import replace
from io import BytesIO
from typing import List

import wave

from ..domain.models import Audio, TTSError


def join_breeze_audio(parts: List[Audio], limit: int) -> Audio:
    if not parts:
        raise TTSError("Breeze 没有可合并的音频")
    if any(audio.response_format != "wav" for audio in parts):
        raise TTSError("Breeze 分段合成必须返回 WAV 音频")
    output = BytesIO()
    try:
        with wave.open(BytesIO(parts[0].data), "rb") as first:
            parameters = (first.getnchannels(), first.getsampwidth(), first.getframerate(), first.getcomptype())
        with wave.open(output, "wb") as target:
            target.setnchannels(parameters[0])
            target.setsampwidth(parameters[1])
            target.setframerate(parameters[2])
            for audio in parts:
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
                    if output.tell() + len(frames) + 44 > limit:
                        raise TTSError("Breeze 合并音频超过大小上限")
                    target.writeframesraw(frames)
    except (wave.Error, EOFError) as exc:
        raise TTSError("Breeze 分段音频不是有效的 PCM WAV") from exc
    return replace(parts[0], data=output.getvalue(), content_type="audio/wav", response_format="wav")
