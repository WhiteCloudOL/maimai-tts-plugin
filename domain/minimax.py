"""MiniMax 官方语音模型与参数范围，和百炼转接版本分别维护。"""

from typing import Set

MODELS = [
    "speech-2.8-hd",
    "speech-2.8-turbo",
    "speech-2.6-hd",
    "speech-2.6-turbo",
    "speech-02-hd",
    "speech-02-turbo",
    "speech-01-hd",
    "speech-01-turbo",
]
FORMATS = ["mp3", "wav", "pcm", "flac", "opus", "pcmu_raw", "pcmu_wav"]
OLD_MODEL_LANGUAGES = {"Persian", "Filipino", "Tamil"}
BASE_EMOTIONS = {"happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm"}


def emotions(model: str) -> Set[str]:
    return BASE_EMOTIONS | ({"fluent", "whisper"} if model.startswith("speech-2.6-") else set())
