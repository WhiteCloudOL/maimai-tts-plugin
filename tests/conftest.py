"""以 Runner 相同的包模式加载插件，使用本地 HTTP 服务验证协议。"""

from aiohttp import web
from pathlib import Path
from types import SimpleNamespace

import importlib.util
import io
import pytest
import sys
import wave


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "tts_under_test", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


@pytest.fixture
def plugin_module():
    return module


@pytest.fixture
def wav_audio():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\0\0" * 48000)
    return stream.getvalue()


@pytest.fixture
async def server():
    state = SimpleNamespace(handler=None, base="")

    async def dispatch(request):
        return await state.handler(request)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", dispatch)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        yield state
    finally:
        await runner.cleanup()
