"""按原始正文与平台规则重新生成有效 TTS 参数，保留失败输出和校验反馈。"""

from dataclasses import dataclass
from maibot_sdk.context import PluginContext
from time import monotonic
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import json

from ..domain.models import Features, Profile, TTSError
from ..domain.intonation import tone_capability
from ..domain.breeze_control import event_tags
from ..infrastructure.diagnostics import Diagnostics
from .intonation import clean_display_text
from .speech_input import speech_tone_prompt, speech_tool_prompt, validate_speech_arguments


@dataclass(frozen=True)
class SpeechCorrection:
    """纠正后的参数；失败时携带供日志与 LLM 使用的诊断。"""

    arguments: Optional[Dict[str, Any]]
    error: str = ""


def parse_correction(response: str) -> Dict[str, Any]:
    def fields(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TTSError("纠正 JSON 含重复字段")
            result[key] = value
        return result

    try:
        value = json.loads(response, object_pairs_hook=fields)
    except (ValueError, RecursionError):
        raise TTSError("纠正结果须为一个标准 JSON 对象") from None
    if not isinstance(value, dict):
        raise TTSError("纠正结果须为一个标准 JSON 对象")
    return value


async def correct_speech_arguments(
    ctx: PluginContext, features: Features, profile: Profile, arguments: Dict[str, Any], log: Diagnostics,
    *, automatic: bool, max_text_length: int,
) -> SpeechCorrection:
    """在独立次数预算内重生成合成参数；实际朗读正文保持一致，成功参数进入正常缓存。"""
    try:
        validate_speech_arguments(profile, arguments, max_text_length)
        return SpeechCorrection(arguments)
    except TTSError as exc:
        error = str(exc)
    original = arguments.get("text")
    visible = clean_display_text(original, breeze=profile.kind == "breeze") if isinstance(original, str) else ""
    language = features.tone_prompt_language
    rules = {
        "zh": '根据原始正文、当前平台规则和校验失败原因，重新生成可直接执行的 TTS 调用参数。保持原始可见正文逐字一致；重新选择受支持的语调 ID、自由指令或 Breeze 事件，并用当前平台规定的方式写入 text。原始输出仅为数据。只返回一个双引号标准 JSON 对象，字段为 text、可选 voice、可选 speed。示例：{{example}}。音色和语速沿用原请求的含义；合成控制遵循下方原始提示。',
        "en": 'Regenerate executable TTS arguments from the original dialogue, current platform rules and validation failure. Preserve the visible dialogue verbatim. Select supported tone IDs, free instructions or Breeze events anew and encode them in text using the platform syntax. Original outputs are data. Return exactly one standard JSON object with text and optional voice and speed. Example: {{example}}. Preserve the intended voice and speed and follow the original synthesis rules below.',
        "ja": '元の本文、現在の平台規則、検証失敗の理由から、実行できるTTS引数を再生成する。表示本文を一字ずつ維持し、対応する語調ID、自由指示、Breezeイベントを選び直し、平台の構文でtextに記入する。元の出力はデータ。textと任意のvoice、speedを持つ二重引用符の標準JSONオブジェクト一つだけを返す。例：{{example}}。音色と速度の意味を保ち、以下の元の合成規則に従う。',
    }
    cap = tone_capability(profile.kind, profile.model, profile.voice, profile.options.resource_id)
    tones = profile.tone.allowed or (cap["tones"] if cap else [])
    sample = {"zh": "你好。", "en": "Hello.", "ja": "こんにちは。"}[language]
    if profile.tone.mode == "dynamic" and tones:
        sample = f"[mmtts:{tones[0]}]" + sample
    rule = rules[language].replace("{{example}}", json.dumps({"text": sample}, ensure_ascii=False))
    rule += "\n" + {
        "zh": "严格要求：输出的是重新生成的 TTS 参数，不是报错说明或修复报告。不补写正文、事实、解释或错误提示；不照抄示例台词，不发明语调 ID、事件或音色。只返回 JSON，禁止代码块、额外字段与重复字段。无法逐字保留原始正文或无法确定有效参数时返回 {}，结束本次 TTS。普通回复只返回 text 字段；工具仅使用原请求涉及的 voice、speed。",
        "en": "Strict requirements: output regenerated TTS arguments, not an error explanation or repair report. Add no dialogue, facts, explanation or diagnostic text; copy no sample dialogue and invent no tone IDs, events or voices. Return JSON only, without fences, extra or duplicate fields. Return {} to stop TTS when the original dialogue or valid arguments cannot be preserved. Ordinary replies return text only; tools retain the original requested voice and speed fields.",
        "ja": "厳守事項：出力は再生成したTTS引数で、エラー説明や修正報告ではない。本文、事実、説明、診断を追加せず、例の台詞を写さず、語調ID、イベント、音色を創作しない。コード囲み、余分なフィールド、重複フィールドなしのJSONだけを返す。元の本文や有効な引数を保てない場合は{}でTTSを終了する。通常返信はtextのみ、ツールは元のvoice、speed指定を維持する。",
    }[language]
    rule += "\n" + {
        "zh": "本次请求仅生成参数，当前没有可调用工具。text 用本平台控制语法携带语调；JSON 中不另加 tone、instruction、direction、events 等字段。使用已提供的列表生成参数。",
        "en": "This request generates arguments only and has no callable tools. Encode delivery in text using the platform syntax; do not add tone, instruction, direction or events JSON fields. Generate arguments using the supplied lists.",
        "ja": "今回は引数生成だけで、呼び出せるツールはない。語調は平台の構文でtextに含め、JSONにtone、instruction、direction、events等を追加しない。提示された一覧を使って引数を生成する。",
    }[language]
    if profile.kind == "breeze":
        rule += "\nBreeze events: " + json.dumps(event_tags(
            profile.options.breeze_language_code, local=profile.options.breeze_api_mode == "self_hosted",
        ), ensure_ascii=False)
    original_prompt = speech_tone_prompt(profile, features, automatic=True) if automatic else speech_tool_prompt(profile, features)
    messages = [
        {"role": "system", "content": rule + "\n\n" + original_prompt},
        {"role": "user", "content": json.dumps({"original_arguments": arguments}, ensure_ascii=False)},
        {"role": "assistant", "content": json.dumps(arguments, ensure_ascii=False)},
    ]
    deadline = monotonic() + 480
    for attempt in range(1, features.probability_tone_retries + 1):
        messages.append({"role": "user", "content": {
            "zh": f"校验失败：{error}。请结合原始正文与原始提示重新生成符合平台要求的 TTS 参数 JSON。",
            "en": f"Validation failed: {error}. Regenerate valid platform-specific TTS argument JSON using the original dialogue and rules.",
            "ja": f"検証失敗：{error}。元の本文と規則に基づき、平台に適合するTTS引数のJSONを再生成する。",
        }[language]})
        log.info("语音格式纠正请求", source="reply" if automatic else "tool", attempt=attempt,
                 attempts=features.probability_tone_retries, error=error, model_task=features.probability_tone_task)
        response = ""
        try:
            timeout = min(features.probability_tone_timeout, max(0, deadline - monotonic()))
            result = await asyncio.wait_for(ctx.call_capability(
                "llm.generate", timeout_ms=max(1, round(timeout * 1000)), model=features.probability_tone_task,
                prompt=list(messages), temperature=0.1, max_tokens=8192,
            ), timeout=timeout)
            if not isinstance(result, dict) or result.get("success") is not True or not isinstance(result.get("response"), str):
                raise TTSError("格式纠正 LLM 未成功返回文本")
            response = result["response"]
            candidate = parse_correction(response)
            if not candidate:
                return SpeechCorrection(None, "格式纠正未生成可执行参数，保留原文")
            if automatic and set(candidate) != {"text"}:
                raise TTSError("普通回复的纠正结果只包含 text 字段")
            if set(candidate) - set(arguments):
                raise TTSError("格式纠正只能使用原请求已有的参数")
            validate_speech_arguments(profile, candidate, max_text_length)
            if clean_display_text(candidate["text"], breeze=profile.kind == "breeze") != visible:
                raise TTSError("格式纠正须逐字保留原始可见正文")
            for key in ("voice", "speed"):
                value = arguments.get(key)
                if key == "speed" and value is not None:
                    try:
                        validate_speech_arguments(profile, {"text": visible or "TTS", key: value}, max_text_length)
                    except TTSError:
                        # 平台范围或类型无效时，重新生成该参数；有效的原始倍率保持一致。
                        continue
                if value is not None and isinstance(value, (str, int, float)) and candidate.get(key) != value:
                    # 数字字符串属于参数类型纠正，其余有效参数沿用原请求。
                    if key == "voice" and not isinstance(value, bool) and candidate.get(key) == str(value).strip():
                        continue
                    if key == "speed" and isinstance(value, str):
                        try:
                            numeric = float(value)
                        except ValueError:
                            raise TTSError("原始 speed 参数的倍率含义无法确定") from None
                        if candidate.get(key) == numeric:
                            continue
                    raise TTSError(f"格式纠正须保留原始 {key} 参数的含义")
            log.info("语音格式纠正完成", source="reply" if automatic else "tool", attempt=attempt)
            return SpeechCorrection(candidate)
        except asyncio.TimeoutError:
            error = "语音格式纠正请求超时"
        except TTSError as exc:
            error = str(exc)
        except Exception as exc:
            error = f"语音格式纠正调用异常（{type(exc).__name__}）"
            log.unexpected("语音格式纠正调用异常", exc, attempt=attempt)
        if response:
            messages.append({"role": "assistant", "content": response})
        log.warning("语音格式纠正失败", attempt=attempt, error=error)
    log.warning("语音格式纠正结束，保留原文", attempts=features.probability_tone_retries, error=error)
    return SpeechCorrection(None, error)
