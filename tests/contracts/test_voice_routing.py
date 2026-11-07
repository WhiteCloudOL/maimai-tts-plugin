"""已保存的模型音色不能被路由或工具中的空选择覆盖。"""

from aiohttp import web
from typing import Optional

import pytest

from maimai_tts_contract.domain.models import Profile, RouteTarget, StudioConfig, TTSError
from maimai_tts_contract.services.studio import Studio


pytestmark = pytest.mark.asyncio
AUDIO = b"ID3voice-routing-contract"
CASES = {
    "breeze": ("breeze-tts-2", "voice-default", "voice-override", {"breeze_api_mode": "cloud", "breeze_mode": "plain"}),
    "openai": ("gpt-4o-mini-tts", "alloy", "coral", {}),
    "minimax": ("speech-2.8-hd", "male-qn-qingse", "female-tianmei", {}),
}


def config(base_url: str, kind: str, route_voice: Optional[str]) -> StudioConfig:
    model, default, _, options = CASES[kind]
    selected = Profile(
        id="voice-routing", name="音色路由测试", kind=kind, base_url=base_url + "/v1",
        api_key="test-only-not-a-real-key", model=model, voice="stale-top-level-voice",
        models=[{"id": model, "voice": default}], options=options,
    )
    return StudioConfig(profiles=[selected], primary=RouteTarget(provider_id=selected.id, model_id=model, voice=route_voice))


def upstream(kind, calls):
    async def handler(request):
        body = await request.json()
        if kind == "breeze":
            calls.append(request.path.rsplit("/", 1)[-1])
        elif kind == "minimax":
            calls.append(body["voice_setting"]["voice_id"])
            return web.json_response({"base_resp": {"status_code": 0}, "data": {"audio": AUDIO.hex(), "status": 2}})
        else:
            calls.append(body["voice"])
        return web.Response(body=AUDIO, content_type="audio/mpeg")
    return handler


@pytest.mark.parametrize("kind", CASES)
@pytest.mark.parametrize("blank", [None, "", " \t "])
async def test_blank_route_inherits_saved_model_voice(serve, service, kind, blank):
    calls = []
    saved = config(await serve(upstream(kind, calls)), kind, blank)
    audio = await service.synthesize("音色路由测试。", config=saved)
    assert calls == [CASES[kind][1]]
    assert audio.data == AUDIO


@pytest.mark.parametrize("kind", CASES)
@pytest.mark.parametrize("blank", ["", " \t "])
async def test_blank_optional_tool_voice_keeps_the_route_voice(serve, service, kind, blank):
    calls = []
    saved = config(await serve(upstream(kind, calls)), kind, CASES[kind][2])
    await service.synthesize("音色路由测试。", config=saved, voice=blank)
    assert calls == [CASES[kind][2]]


@pytest.mark.parametrize("kind", CASES)
async def test_explicit_tool_voice_overrides_inherited_model_voice(serve, service, kind):
    calls = []
    saved = config(await serve(upstream(kind, calls)), kind, "")
    await service.synthesize("音色路由测试。", config=saved, voice=CASES[kind][2])
    assert calls == [CASES[kind][2]]


@pytest.mark.parametrize("kind", CASES)
async def test_temporary_voice_does_not_change_model_defaults(kind):
    saved = config("https://example.test", kind, None)
    root = saved.profiles[0]
    selected = root.for_model(root.model).with_synthesis_overrides(voice=CASES[kind][2]).with_selected_voice()
    assert selected.voice == CASES[kind][2]
    assert selected.models[0].voice == root.models[0].voice == CASES[kind][1]


@pytest.mark.parametrize("entries", [None, [{"id": "breeze-tts-2"}]])
async def test_legacy_single_model_voice_is_preserved(entries):
    data = {
        "id": "legacy", "name": "历史配置", "kind": "breeze", "base_url": "https://example.test/v1",
        "model": "breeze-tts-2", "voice": "legacy-voice", "options": {"breeze_api_mode": "cloud"},
    }
    if entries is not None:
        data["models"] = entries
    saved = Profile.model_validate(data)
    assert saved.for_model(saved.model).voice == "legacy-voice"


async def test_explicit_empty_model_voice_does_not_restore_a_stale_top_level_voice():
    data = config("https://example.test", "breeze", None).profiles[0].model_dump()
    data["voice"] = "stale-voice"
    data["models"][0]["voice"] = ""
    saved = Profile.model_validate(data)
    assert saved.for_model(saved.model).voice == ""


async def test_inherited_voice_cache_tracks_the_effective_model_voice(serve, service):
    calls = []
    saved = config(await serve(upstream("breeze", calls)), "breeze", "")
    await service.synthesize("相同正文。", config=saved)
    inherited = saved.model_copy(deep=True)
    inherited.primary.voice = None
    await service.synthesize("相同正文。", config=inherited)
    assert calls == ["voice-default"]
    inherited.profiles[0].models[0].voice = "voice-new-default"
    await service.synthesize("相同正文。", config=inherited)
    assert calls == ["voice-default", "voice-new-default"]
    inherited.primary.voice = "voice-default"
    await service.synthesize("相同正文。", config=inherited)
    assert calls == ["voice-default", "voice-new-default"]


async def test_empty_legacy_route_still_protects_the_inherited_cloud_voice_from_deletion(serve, service):
    async def handler(request):
        pytest.fail("deletion protection should reject before any API request")
    saved = config(await serve(handler), "breeze", "")
    service.store._config = saved
    studio = Studio(service.store, service)
    try:
        with pytest.raises(TTSError, match="整体路由正在使用"):
            studio.launch("delete_voice", profile_id=saved.profiles[0].id, model_id=saved.profiles[0].model, voice_id="voice-default")
    finally:
        await studio.close()
