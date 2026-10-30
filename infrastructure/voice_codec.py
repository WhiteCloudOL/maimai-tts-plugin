"""将服务商音频转换为 QQ 原生语音；不改变工作台试听文件。"""

from dataclasses import replace
from io import BytesIO
from time import monotonic
from typing import Any, Dict

import av
import base64
import pysilk

from ..domain.models import Audio, TTSError


VOICE_SAMPLE_RATE = 24000
MAX_VOICE_SECONDS = 600
SILK_HEADER = b"\x02#!SILK_V3"


def encode_silk(audio: Audio, limit: int) -> Audio:
    """有界解码、重采样到单声道 PCM16，再编码腾讯 SILK；由工作线程调用。"""
    if not audio.data or len(audio.data) > limit:
        raise TTSError("待发送音频为空或超过大小上限")
    if audio.data.startswith(SILK_HEADER):
        return replace(audio, content_type="audio/silk", response_format="silk")
    formats = {"wav": "wav", "mp3": "mp3", "opus": "ogg", "flac": "flac", "aac": "aac"}
    if audio.response_format not in formats:
        raise TTSError(f"不能将 {audio.response_format} 转换为 QQ 语音")
    pcm = BytesIO()
    maximum = VOICE_SAMPLE_RATE * 2 * MAX_VOICE_SECONDS
    deadline = monotonic() + 30

    def write_frame(frame: av.AudioFrame) -> None:
        if monotonic() > deadline:
            raise TTSError("QQ 语音转码超过 30 秒")
        if pcm.tell() + frame.samples * 2 > maximum:
            raise TTSError(f"QQ 语音超过插件允许的 {MAX_VOICE_SECONDS} 秒上限")
        # 音频平面可能含对齐填充，只写实际采样，避免插入静音和累积时长误差。
        pcm.write(bytes(frame.planes[0])[: frame.samples * 2])

    try:
        with av.open(BytesIO(audio.data), format=formats[audio.response_format]) as container:
            if not container.streams.audio:
                raise TTSError("待发送文件没有音轨")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=VOICE_SAMPLE_RATE)
            for frame in container.decode(container.streams.audio[0]):
                for converted in resampler.resample(frame):
                    write_frame(converted)
            for converted in resampler.resample(None):
                write_frame(converted)
        if not pcm.tell():
            raise TTSError("待发送音频没有可编码的采样")
        # SILK 每包 20ms；补齐尾包，避免丢掉最后不足一包的声音。
        pcm.write(b"\0" * (-pcm.tell() % (VOICE_SAMPLE_RATE // 50 * 2)))
        pcm.seek(0)
        output = BytesIO()
        pysilk.encode(pcm, output, VOICE_SAMPLE_RATE, 24000, tencent=True)
    except (av.FFmpegError, pysilk.SilkError, ValueError) as exc:
        raise TTSError(f"QQ 语音转码失败（{type(exc).__name__}），请检查上游音频内容") from exc
    data = output.getvalue()
    if not data.startswith(SILK_HEADER) or len(data) <= len(SILK_HEADER) or len(data) > limit:
        raise TTSError("QQ 语音编码结果无效或超过大小上限")
    return replace(audio, data=data, content_type="audio/silk", response_format="silk")


def voice_segment(audio: Audio) -> Dict[str, Any]:
    """SDK hybrid 与发送 Hook 共用同一个二进制语音段契约。"""
    return {"type": "voice", "data": "", "binary_data_base64": base64.b64encode(audio.data).decode("ascii")}
