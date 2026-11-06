"""抽中概率语音后按实际路由分类；一次回复内复用决定。"""

from maibot_sdk.context import PluginContext
from typing import Any, Dict, List, Optional

import asyncio
import json

from ..domain.adaptive_tone import (
    ToneDecision,
    adaptive_prompt,
    direction_limit,
    parse_adaptive_tone,
    validate_direction,
)
from ..domain.intonation import tone_capability
from ..domain.models import Features, Profile, TTSError
from ..infrastructure.diagnostics import Diagnostics


class ProbabilityToneAdapter:
    def __init__(self, ctx: PluginContext, features: Features, text: str, log: Diagnostics) -> None:
        self.ctx = ctx
        self.features = features.model_copy(deep=True)
        self.text = text
        self.log = log
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
        key = json.dumps(
            [tones, profile.kind == "breeze", direction_limit(profile), profile.kind == "cosyvoice", xml_instructions]
        )
        if key in self.decisions:
            return self.decisions[key]
        decision = await self._classify(profile, tones)
        self.decisions[key] = decision
        return decision

    async def _classify(self, profile: Profile, tones: List[str]) -> Optional[ToneDecision]:
        timeout = self.features.probability_tone_timeout
        limit = direction_limit(profile)
        feedback = ""
        attempts = self.features.probability_tone_retries + 1
        for attempt in range(1, attempts + 1):
            prompt = adaptive_prompt(
                self.features.tone_prompt_language,
                tones,
                breeze=profile.kind == "breeze",
                free_direction=limit > 0,
                max_direction_length=limit,
                feedback=feedback,
                weighted=profile.kind == "cosyvoice",
            )
            try:
                # 同时设置 RPC 与外层预算，避免 SDK 默认 RPC 超时提前截断用户配置。
                result: Any = await asyncio.wait_for(
                    self.ctx.call_capability(
                        "llm.generate",
                        timeout_ms=round(timeout * 1000),
                        model=self.features.probability_tone_task,
                        prompt=[{"role": "system", "content": prompt}, {"role": "user", "content": self.text}],
                        temperature=0.1,
                        max_tokens=256,
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
                        response, tones, breeze=profile.kind == "breeze", free_direction=limit > 0
                    )
                    if decision.direction:
                        validate_direction(profile, decision.direction)
                    return decision
                except TTSError as exc:
                    # 重试附带协议校验原因，避免对同一格式错误重复发送相同提示；不回传或记录模型原文。
                    feedback = str(exc)
                    raise
            except asyncio.TimeoutError:
                self.log.warning("概率语音语调自适应超时", timeout_seconds=timeout, attempt=attempt, attempts=attempts)
            except TTSError as exc:
                self.log.warning("概率语音语调自适应失败", error=str(exc), attempt=attempt, attempts=attempts)
            except Exception as exc:
                # SDK/RPC 故障保留异常位置；取消仍向上传播，模型正文不进入日志。
                self.log.unexpected("概率语音语调 LLM 调用异常", exc, attempt=attempt, attempts=attempts)
            if attempt < attempts:
                await asyncio.sleep(0.25 * attempt)
        self.log.warning("概率语音语调自适应重试已耗尽，使用路由默认语调", attempts=attempts)
        return None
