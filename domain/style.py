"""风格描述只按非空覆盖继承，不改变语速、资源和其他参数的覆盖规则。"""

from typing import Optional

from .compatibility import CompatibilityOptions, template_variables
from .intonation import tone_capability


def supports_style(
    kind: str, model: str, voice: str, resource_id: str, protocol: Optional[CompatibilityOptions] = None
) -> bool:
    if kind == "compatible" and protocol is not None and protocol.mode == "custom":
        return "instructions" in template_variables(protocol.body)
    capability = tone_capability(kind, model, voice, resource_id)
    return capability is not None and capability["protocol"] not in {"cosy_emotion", "minimax_emotion"}


def inherit_instructions(parent: str, override: Optional[str]) -> str:
    if override is None or not override.strip():
        return parent if parent.strip() else ""
    return override
