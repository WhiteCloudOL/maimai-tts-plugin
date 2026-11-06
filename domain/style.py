"""风格描述只按非空覆盖继承，不改变语速、资源和其他参数的覆盖规则。"""

from typing import Optional


def inherit_instructions(parent: str, override: Optional[str]) -> str:
    if override is None or not override.strip():
        return parent if parent.strip() else ""
    return override
