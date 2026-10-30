"""工作台响应的完整音色映射与 Host JSON 响应预算。"""

import json
import logging

from tts_under_test.domain.catalog import CATALOG
from tts_under_test.domain.voice_catalog import get_catalog, get_catalog_payload
from tts_under_test.infrastructure.storage import ConfigStore
from tts_under_test.services.studio import Studio
from tts_under_test.services.tts import TTSService
from tts_under_test.settings.config import RuntimeSection


def test_wire_catalog_preserves_all_model_voices_and_names():
    payload = json.loads(json.dumps(get_catalog_payload(), ensure_ascii=False))
    assert payload["catalog"].keys() == get_catalog().keys()
    for kind, source in get_catalog().items():
        wire = payload["catalog"][kind]
        assert "model_voices" not in wire and "voice_details" not in wire
        assert wire["model_voice_sets"].keys() == source.get("model_voices", {}).keys()
        for key, value in source.items():
            if key not in ("model_voices", "voice_details"):
                assert wire[key] == value
        for model, voices in source.get("model_voices", {}).items():
            labels = payload["voice_sets"][wire["model_voice_sets"][model]]
            assert list(labels) == voices
            for voice_id in voices:
                details = source.get("voice_details", {}).get(model, {}).get(voice_id, {})
                assert labels[voice_id] == details.get("name", voice_id)


def test_shared_presets_reuse_one_set_and_clone_models_keep_empty_sets():
    payload = get_catalog_payload()
    native = payload["catalog"]["minimax"]["model_voice_sets"]
    bailian = payload["catalog"]["bailian_minimax"]["model_voice_sets"]
    assert len(set(native.values()) | set(bailian.values())) == 1
    assert len(payload["voice_sets"][native["speech-2.8-hd"]]) == 327
    for kind, model in (("cosyvoice", "cosyvoice-v3.5-flash"), ("mimo", "mimo-v2.5-tts-voiceclone")):
        reference = payload["catalog"][kind]["model_voice_sets"][model]
        assert payload["voice_sets"][reference] == {}


async def test_full_studio_snapshot_fits_host_response_budget(tmp_path):
    store = ConfigStore(tmp_path)
    profiles = []
    for index, (kind, platform) in enumerate((list(CATALOG.items()) * 2)[:20]):
        profiles.append(
            {
                "id": f"provider{index}",
                "kind": kind,
                "name": f"{platform['name']}_{index}",
                "base_url": platform["base_url"],
                "api_key": "private-test-key",
                "model": platform["model"],
                "voice": platform["voice"],
                "response_format": platform["formats"][0],
                "options": {"sample_rate": 44100 if kind == "siliconflow" else 24000},
                "voices": [
                    {"id": f"custom-{voice}", "name": f"自定义音色{voice}", "model": platform["model"]}
                    for voice in range(3)
                ],
            }
        )
    await store.save({"profiles": profiles}, 0)
    service = TTSService(store, RuntimeSection(), logging.getLogger("test"))
    try:
        for _ in range(30):
            service.record(store.config.profiles[0], True, audio_bytes=1024)
        snapshot = await Studio(store, service).snapshot()
        wire = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        assert "private-test-key" not in wire
        # Host 对整个响应限制为 20,000 个 JSON 值、12 层和 512 KiB；按序列化后的树计数。
        pending = [(json.loads(wire), 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            assert depth <= 12
            children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else []
            pending.extend((child, depth + 1) for child in children)
        assert count <= 20000
        assert len(wire.encode("utf-8")) <= 524288
        assert len(snapshot["config"]["profiles"]) == 20
    finally:
        await service.close()
