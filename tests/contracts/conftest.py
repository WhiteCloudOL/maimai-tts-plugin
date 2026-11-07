"""独立加载插件，并通过本机 HTTP/WebSocket 服务核对真实网络请求。"""

from aiohttp import web
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, List
from unittest.mock import Mock

import sys

import pytest_asyncio


# 插件目录名含连字符，不导入宿主或读取宿主配置。
PACKAGE = "maimai_tts_contract"
spec = spec_from_loader(PACKAGE, loader=None, is_package=True)
package = module_from_spec(spec)
package.__path__ = [str(Path(__file__).resolve().parents[2])]
sys.modules[PACKAGE] = package

from maimai_tts_contract.infrastructure.storage import ConfigStore  # noqa: E402
from maimai_tts_contract.services.tts import TTSService  # noqa: E402
from maimai_tts_contract.settings.config import RuntimeSection  # noqa: E402


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@pytest_asyncio.fixture
async def serve() -> AsyncIterator[Callable[[Handler], Awaitable[str]]]:
    runners: List[web.AppRunner] = []

    async def start(handler: Handler) -> str:
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        runner = web.AppRunner(app, shutdown_timeout=0.1)
        await runner.setup()
        runners.append(runner)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{runner.addresses[0][1]}"

    try:
        yield start
    finally:
        for runner in runners:
            await runner.cleanup()


@pytest_asyncio.fixture
async def service(tmp_path: Path) -> AsyncIterator[TTSService]:
    instance = TTSService(ConfigStore(tmp_path), RuntimeSection(timeout_seconds=1), Mock())
    try:
        yield instance
    finally:
        await instance.close()
