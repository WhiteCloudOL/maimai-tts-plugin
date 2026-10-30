"""MiniMax 录音上传前校验与电话音频转换，均由工作线程调用。"""

from fractions import Fraction
from io import BytesIO
from time import monotonic
from typing import Tuple

import av
import wave

from ..domain.models import TTSError


def recording_info(data: bytes, purpose: str) -> Tuple[str, str]:
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        extension, mime = "wav", "audio/wav"
    elif len(data) > 12 and data[4:8] == b"ftyp":
        extension, mime = "m4a", "audio/mp4"
    elif data.startswith(b"ID3") or (len(data) > 1 and data[0] == 255 and data[1] & 224 == 224):
        extension, mime = "mp3", "audio/mpeg"
    else:
        raise TTSError("MiniMax 录音请使用 MP3、M4A 或 WAV")
    deadline, seconds = monotonic() + 15, Fraction(0)
    try:
        with av.open(BytesIO(data)) as container:
            if not container.streams.audio:
                raise TTSError("录音文件没有音轨")
            if container.streams.video:
                raise TTSError("参考录音请使用纯音频文件")
            for frame in container.decode(container.streams.audio[0]):
                seconds += Fraction(frame.samples, frame.sample_rate)
                if seconds > 300 or monotonic() > deadline:
                    raise TTSError("录音超过 5 分钟或校验处理时间上限")
    except (av.FFmpegError, ValueError) as exc:
        raise TTSError("参考录音解码失败，请检查文件内容") from exc
    if purpose == "voice_clone" and not 10 <= seconds <= 300:
        raise TTSError("MiniMax 复刻录音时长需要 10 秒至 5 分钟")
    if purpose == "prompt_audio" and not 0 < seconds < 8:
        raise TTSError("MiniMax 示例录音时长需要小于 8 秒")
    return extension, mime


def telephony_wav(data: bytes, fmt: str, limit: int) -> bytes:
    output = BytesIO()
    try:
        with av.open(
            BytesIO(data), format="mulaw" if fmt == "pcmu_raw" else "wav", options={"sample_rate": "8000"}
        ) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
            with wave.open(output, "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(8000)
                for frame in container.decode(container.streams.audio[0]):
                    for converted in resampler.resample(frame):
                        if output.tell() + converted.samples * 2 > limit:
                            raise TTSError("G.711 转换音频超过大小上限")
                        stream.writeframesraw(bytes(converted.planes[0])[: converted.samples * 2])
    except (av.FFmpegError, ValueError, IndexError) as exc:
        raise TTSError("G.711 音频转换失败，请检查平台输出格式") from exc
    return output.getvalue()
