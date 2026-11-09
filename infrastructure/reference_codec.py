"""将参考录音规范化为 C++ 服务端使用的单声道 PCM16 WAV。"""

from io import BytesIO
from time import monotonic

import av
import wave

from ..domain.models import TTSError


def reference_wav(data: bytes, limit: int) -> bytes:
    """有界解码和重采样参考录音，由工作线程执行并保留完整采样。"""
    output = BytesIO()
    deadline = monotonic() + 20
    samples = 0
    try:
        with av.open(BytesIO(data)) as source, wave.open(output, "wb") as target:
            if not source.streams.audio:
                raise TTSError("参考录音没有音轨")
            target.setparams((1, 2, 24000, 0, "NONE", ""))
            resampler = av.AudioResampler(format="s16", layout="mono", rate=24000)

            def write(frame: av.AudioFrame) -> None:
                nonlocal samples
                if monotonic() > deadline:
                    raise TTSError("参考录音转换超过 20 秒")
                samples += frame.samples
                if samples * 2 + 44 > limit:
                    raise TTSError("转换后的参考 WAV 超过录音容量上限")
                target.writeframesraw(bytes(frame.planes[0])[:frame.samples * 2])

            for frame in source.decode(audio=0):
                for converted in resampler.resample(frame):
                    write(converted)
            for converted in resampler.resample(None):
                write(converted)
        if not samples:
            raise TTSError("参考录音没有可解码的音频采样")
    except (av.FFmpegError, ValueError, OverflowError) as exc:
        raise TTSError(f"参考录音转换失败（{type(exc).__name__}），请检查录音内容") from None
    return output.getvalue()
