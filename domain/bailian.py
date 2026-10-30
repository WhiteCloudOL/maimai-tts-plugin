"""百炼各代语音模型的协议与能力边界。"""

QWEN_AUDIO_MODELS = {
    "qwen-audio-3.0-tts-flash",
    "qwen-audio-3.0-tts-plus",
    "qwen-audio-3.1-tts-flash",
    "qwen-audio-3.1-tts-plus",
}
DESIGN_MODELS = QWEN_AUDIO_MODELS | {
    "cosyvoice-v3.5-plus",
    "cosyvoice-v3.5-flash",
    "cosyvoice-v3-plus",
    "cosyvoice-v3-flash",
}
MINIMAX_MODELS = {
    "MiniMax/speech-2.8-hd",
    "MiniMax/speech-2.8-turbo",
    "MiniMax/speech-02-hd",
    "MiniMax/speech-02-turbo",
}
SPEECH_RATES = {8000, 12000, 16000, 22050, 24000, 44100, 48000}
MINIMAX_RATES = {8000, 16000, 22050, 24000, 32000, 44100}
VOICE_STATES = {"DEPLOYING": "training", "OK": "ready", "UNDEPLOYED": "failed"}
VOICE_LANGUAGES = {"zh", "en", "fr", "de", "ja", "ko", "ru", "pt", "th", "id", "vi", "es", "it", "ms", "fil", "ar"}
MINIMAX_LANGUAGES = [
    "auto",
    "Chinese",
    "Chinese,Yue",
    "English",
    "Arabic",
    "Russian",
    "Spanish",
    "French",
    "Portuguese",
    "German",
    "Turkish",
    "Dutch",
    "Ukrainian",
    "Vietnamese",
    "Indonesian",
    "Japanese",
    "Italian",
    "Korean",
    "Thai",
    "Polish",
    "Romanian",
    "Greek",
    "Czech",
    "Finnish",
    "Hindi",
    "Bulgarian",
    "Danish",
    "Hebrew",
    "Malay",
    "Persian",
    "Slovak",
    "Swedish",
    "Croatian",
    "Filipino",
    "Hungarian",
    "Norwegian",
    "Slovenian",
    "Catalan",
    "Nynorsk",
    "Tamil",
    "Afrikaans",
]
