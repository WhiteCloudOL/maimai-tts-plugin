"""官方音色快照与展示目录，账号同步和手动音色保持独立。"""

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Tuple

import json

from .catalog import CATALOG


@lru_cache(maxsize=1)
def get_catalog() -> Dict[str, Dict[str, Any]]:
    # 由工作台在工作线程首次读取，后续复用只读的资源目录快照。
    path = Path(__file__).resolve().parents[1] / "assets" / "voice_presets.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    catalog = deepcopy(CATALOG)
    # 两个接入平台使用同一组 MiniMax 官方系统音色，模型和账号配置仍独立。
    data["models"]["minimax"] = data["models"]["bailian_minimax"]
    data["sources"]["minimax"] = data["sources"]["bailian_minimax"]
    for kind, models in data["models"].items():
        item = catalog[kind]
        item["voice_details"] = {}
        item.setdefault("model_voices", {})
        for model, voices in models.items():
            targets = [*item["models"], ""] if model == "*" else [model]
            for target in targets:
                item["model_voices"][target] = list(voices)
                item["voice_details"][target] = voices
        item["voice_source"] = data["sources"][kind]
        item["voice_verified_on"] = data["verified_on"]
    for kind in ("openai", "compatible"):
        catalog[kind]["voice_source"] = data["sources"]["openai"]
        catalog[kind]["voice_verified_on"] = data["verified_on"]
    return catalog


@lru_cache(maxsize=1)
def get_catalog_payload() -> Dict[str, Any]:
    """按音色集合去重工作台响应，保留每个模型的完整音色 ID、顺序与名称。"""
    catalog: Dict[str, Dict[str, Any]] = {}
    voice_sets: Dict[str, Dict[str, str]] = {}
    references: Dict[Tuple[Tuple[str, str], ...], str] = {}
    for kind, source in get_catalog().items():
        item = {key: value for key, value in source.items() if key not in ("model_voices", "voice_details")}
        item["model_voice_sets"] = {}
        for model, voices in source.get("model_voices", {}).items():
            details = source.get("voice_details", {}).get(model, {})
            labels = {voice_id: details.get(voice_id, {}).get("name", voice_id) for voice_id in voices}
            # 多模型、多个平台共用的预设只传一次；显式空集合仍表示该模型使用自定义音色。
            signature = tuple(labels.items())
            if signature not in references:
                reference = f"v{len(voice_sets)}"
                references[signature] = reference
                voice_sets[reference] = labels
            item["model_voice_sets"][model] = references[signature]
        catalog[kind] = item
    return {"catalog": catalog, "voice_sets": voice_sets}
