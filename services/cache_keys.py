"""以实际请求身份生成内存缓存键，编辑名称、收藏和路由调整保持复用。"""

from typing import Any, List, Optional, Tuple

import hashlib
import hmac
import json
import secrets

from ..domain.compatibility import template_variables
from ..domain.breeze_control import boundary_pauses
from ..domain.models import Profile


class CacheKeys:
    def __init__(self) -> None:
        self.salt = secrets.token_bytes(32)

    def digest(self, value: Any) -> str:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hmac.new(self.salt, encoded, hashlib.sha256).hexdigest()

    def scope(self, profile: Profile) -> str:
        options = profile.options
        return self.digest(
            {
                "kind": profile.kind,
                "address": profile.base_url.rstrip("/") if profile.base_url else profile.endpoint,
                "api_key": profile.api_key,
                "organization": options.organization,
                "project": options.project,
                "auth_mode": options.auth_mode,
                "app_id": options.app_id,
            }
        )

    def speech(self, profile: Profile, text: str) -> Optional[str]:
        options = profile.options
        if profile.kind == "breeze" and options.breeze_api_mode == "self_hosted" and options.breeze_mode == "reference":
            return None
        # 这些请求依赖动态文件、远端参考音频或外部加载的权重，逐次读取实际来源。
        if profile.kind == "gpt_sovits" or (profile.kind == "mimo" and "voiceclone" in profile.model):
            return None
        if (
            profile.kind == "siliconflow"
            and options.reference_enabled
            and (options.reference_source == "file" or options.reference_url)
        ):
            return None
        if profile.kind == "compatible" and options.compatibility is not None:
            protocol = options.compatibility
            if protocol.mode == "custom" and template_variables(protocol.body) & {
                "reference_audio",
                "reference_base64",
                "reference_url",
                "reference_text",
            }:
                return None
        values = profile.model_dump(
            exclude={
                "id", "name", "enabled", "models", "voices", "models_url", "voices_url", "clone_url", "tone", "network",
            }
        )
        management = {
            "breeze_voice_type",
            "breeze_voice_language",
            "breeze_max_characters",
            "voice_consent_id",
            "clone_speaker_id",
            "clone_billing",
            "clone_language",
            "clone_resource_id",
            "clone_language_hint",
            "max_prompt_audio_length",
            "enable_preprocess",
            "enable_volume_normalization",
            "need_noise_reduction",
            "need_volume_normalization",
            "prompt_audio_url",
            "prompt_text",
            "voice_prompt",
            "design_preview_text",
            "design_language",
            "volc_design_image_url",
            "volc_clone_demo_text",
            "volc_clone_denoise",
            "volc_clone_normalize",
            "minimax_clone_file_id",
            "minimax_prompt_audio",
            "minimax_validation_text",
            "minimax_validation_accuracy",
        }
        if profile.kind not in {"siliconflow", "mimo", "compatible"}:
            management.update(key for key in values["options"] if key.startswith("reference_"))
        for key in management:
            values["options"].pop(key, None)
        return self.digest({"profile": values, "text": text})

    def breeze(self, requests: List[Tuple[Profile, str]]) -> Optional[str]:
        """按实际区段、独立指令和事件位置区分完整语音，沿用动态参考录音的禁缓存规则。"""
        keys = [self.speech(profile, text) for profile, text in requests]
        if any(key is None for key in keys):
            return None
        return self.digest({"breeze_sections_v2": keys, "boundary_pauses_ms": boundary_pauses([text for _, text in requests])})
