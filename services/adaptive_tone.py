"""抽中概率语音后按实际路由分类；一次回复内复用决定。"""

from maibot_sdk.context import PluginContext
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import json

from ..domain.adaptive_tone import (
    ToneDecision,
    adaptive_feedback,
    adaptive_prompt,
    breeze_event_options,
    direction_limit,
    parse_adaptive_tone,
    validate_direction,
)
from ..domain.intonation import tone_capability, tone_choices
from ..domain.breeze_control import parse_breeze_text
from ..domain.models import Features, Profile, TTSError, TTSOperationError
from ..domain.prompts import prompt_template
from ..infrastructure.diagnostics import Diagnostics


class ProbabilityToneAdapter:
    def __init__(
        self, ctx: PluginContext, features: Features, text: str, log: Diagnostics, *, style_hint: str = "",
        required: bool = False, purpose: str = "probability",
    ) -> None:
        self.ctx = ctx
        self.features = features.model_copy(deep=True)
        self.text = text
        self.log = log
        self.style_hint = style_hint
        self.required = required
        self.purpose = purpose
        self.decisions: Dict[str, Optional[ToneDecision]] = {}

    async def __call__(self, profile: Profile) -> Optional[ToneDecision]:
        if profile.tone.mode != "dynamic":
            return None
        cap = tone_capability(profile.kind, profile.model, profile.voice, profile.options.resource_id)
        if cap is None:
            return None
        tones = profile.tone.allowed or cap["tones"]
        # 只在同一轮内复用兼容决定；不同允许列表、自由指令能力、剩余长度和计数规则分别分类。
        # 不持久缓存 LLM 分类，避免模型/提示/语调配置变更后沿用旧决定。
        xml_instructions = profile.kind == "volcengine" and profile.options.volc_instruction_mode == "cot"
        events = (
            breeze_event_options(profile)
            if profile.kind == "breeze" and not parse_breeze_text(self.text).marked
            else {}
        )
        key = json.dumps(
            [
                tones,
                profile.tone.labels,
                profile.tone.directions,
                profile.kind,
                prompt_template(
                    self.features.prompt_overrides, profile.kind, "adaptive", self.features.tone_prompt_language
                ),
                direction_limit(profile),
                profile.kind == "cosyvoice",
                xml_instructions,
                events,
            ]
        )
        if key in self.decisions:
            return self.decisions[key]
        decision = await self._classify(profile, tones)
        self.decisions[key] = decision
        return decision

    async def _classify(self, profile: Profile, tones: List[str]) -> Optional[ToneDecision]:
        timeout = self.features.probability_tone_timeout
        limit = direction_limit(profile)
        # 已有局部表演标记优先保留；未标记正文才由自适应补充原文锚点事件。
        events = (
            breeze_event_options(profile)
            if profile.kind == "breeze" and not parse_breeze_text(self.text).marked
            else {}
        )
        failures: List[Tuple[str, str]] = []
        attempts = 1 + self.features.probability_tone_retries
        # 所有原文共用同一路由的 system 前缀；已有标签的约束和重试原因追加在 user 消息后。
        prompt = adaptive_prompt(
            self.features.tone_prompt_language,
            tones,
            breeze=profile.kind == "breeze",
            free_direction=limit > 0,
            max_direction_length=limit,
            weighted=profile.kind == "cosyvoice",
            event_options=breeze_event_options(profile) if profile.kind == "breeze" else {},
            tone_meanings=tone_choices(tones, profile.tone.labels, profile.tone.directions),
            template=prompt_template(
                self.features.prompt_overrides,
                profile.kind,
                "adaptive",
                self.features.tone_prompt_language,
            ),
        )
        for attempt in range(1, attempts + 1):
            messages = [{"role": "system", "content": prompt}, {"role": "user", "content": self.text}]
            if self.style_hint:
                messages.append({"role": "user", "content": {
                    "zh": "本次表达偏好（作为语气参考）：",
                    "en": "Delivery preference for this request: ",
                    "ja": "今回の話し方の希望：",
                }[self.features.tone_prompt_language] + self.style_hint})
            if profile.kind == "breeze" and not events:
                messages.append({"role": "user", "content": {
                    "zh": "正文已有局部控制标记，保留现有控制，events 必须省略或为空数组。",
                    "en": "The dialogue already has local controls. Preserve them; omit events or return an empty array.",
                    "ja": "本文には局所的な制御があるため維持し、eventsは省略するか空配列にする。",
                }[self.features.tone_prompt_language]})
            for rejected, feedback in failures:
                messages.append({"role": "assistant", "content": rejected})
                messages.append({"role": "user", "content": adaptive_feedback(self.features.tone_prompt_language, feedback)})
            try:
                # 同时设置 RPC 与外层预算，避免 SDK 默认 RPC 超时提前截断用户配置。
                result: Any = await asyncio.wait_for(
                    self.ctx.call_capability(
                        "llm.generate",
                        timeout_ms=round(timeout * 1000),
                        model=self.features.probability_tone_task,
                        prompt=messages,
                        temperature=0.1,
                        # 200 字自由描述及 Breeze 的原文事件锚点，在中日文分词下可超过 256/512 token。
                        max_tokens=2048 if profile.kind == "breeze" else 1024 if limit > 0 else 256,
                    ),
                    timeout=timeout,
                )
                if not isinstance(result, dict) or result.get("success") is not True:
                    raise TTSError("语调 LLM 调用失败，请查看 MaiBot 模型调用日志")
                response = result.get("response")
                if not isinstance(response, str):
                    raise TTSError("语调 LLM 未返回文本")
                try:
                    decision = parse_adaptive_tone(
                        response,
                        tones,
                        breeze=profile.kind == "breeze",
                        free_direction=limit > 0,
                        text=self.text,
                        event_options=events,
                    )
                    if decision.direction:
                        validate_direction(profile, decision.direction)
                    return decision
                except TTSError as exc:
                    # 原始规则与正文保持稳定，失败输出及校验原因形成后续纠正消息。
                    failures.append((response, str(exc)))
                    raise
            except asyncio.TimeoutError:
                self.log.warning("语调自适应超时", purpose=self.purpose, timeout_seconds=timeout, attempt=attempt, attempts=attempts)
            except TTSError as exc:
                self.log.warning("语调自适应失败", purpose=self.purpose, error=str(exc), attempt=attempt, attempts=attempts)
            except Exception as exc:
                # SDK/RPC 故障保留异常位置；取消仍向上传播，模型正文不进入日志。
                self.log.unexpected("语调 LLM 调用异常", exc, purpose=self.purpose, attempt=attempt, attempts=attempts)
            if attempt < attempts:
                await asyncio.sleep(0.25 * attempt)
        self.log.warning("语调自适应重试结束，停止本次 TTS", purpose=self.purpose,
                         retries=attempt - 1, max_retries=self.features.probability_tone_retries)
        raise TTSOperationError("LLM_ERROR", "语调自适应未完成，已停止本次语音合成并保留正文")
