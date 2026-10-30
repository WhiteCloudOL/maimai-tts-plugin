"""复用异步连接、限制响应大小；下载音频时不携带平台密钥。"""

from aiohttp import ClientResponse, ClientSession, ClientTimeout, DummyCookieJar, FormData, TraceConfig
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any, Dict, Optional
from weakref import WeakValueDictionary

import asyncio
import json

from ..settings.config import RuntimeSection
from ..domain.models import TTSError, validate_url
from .reference_audio import ReferenceAudioStore
from .diagnostics import Diagnostics, redact_secrets


class Transport:
    def __init__(
        self, settings: RuntimeSection, data_dir: Optional[Path] = None, diagnostics: Optional[Diagnostics] = None
    ) -> None:
        self.settings = settings
        self.log = diagnostics
        self._reference_files = ReferenceAudioStore(data_dir) if data_dir is not None else None
        self._request_slots = WeakValueDictionary()
        trace = TraceConfig()
        trace.on_request_start.append(self._request_start)
        trace.on_request_end.append(self._request_end)
        trace.on_request_exception.append(self._request_exception)
        self.session = ClientSession(
            timeout=ClientTimeout(total=settings.timeout_seconds),
            # TTS 接口按每次请求鉴权；平台账号与无凭据下载之间不共享上游 Cookie。
            cookie_jar=DummyCookieJar(),
            trust_env=settings.use_system_proxy,
            trace_configs=[trace] if diagnostics is not None else [],
        )

    async def _request_start(self, session: ClientSession, context: SimpleNamespace, params: Any) -> None:
        context.started = monotonic()
        if self.log is not None:
            self.log.debug("HTTP 请求开始", method=params.method, host=params.url.host)

    async def _request_end(self, session: ClientSession, context: SimpleNamespace, params: Any) -> None:
        if self.log is not None:
            self.log.debug(
                "HTTP 响应到达",
                method=params.method,
                host=params.url.host,
                status=params.response.status,
                elapsed_ms=round((monotonic() - context.started) * 1000),
            )

    async def _request_exception(self, session: ClientSession, context: SimpleNamespace, params: Any) -> None:
        if self.log is not None:
            self.log.debug(
                "HTTP 请求异常",
                method=params.method,
                host=params.url.host,
                error_type=type(params.exception).__name__,
                elapsed_ms=round((monotonic() - context.started) * 1000),
            )

    @property
    def reference_files(self) -> ReferenceAudioStore:
        if self._reference_files is None:
            raise TTSError("本地参考音频需要插件数据目录")
        return self._reference_files

    @property
    def limit(self) -> int:
        return self.settings.max_audio_size_mb * 1024 * 1024

    @property
    def proxy(self) -> Optional[str]:
        return self.settings.proxy_url or None

    @staticmethod
    def sanitize(message: str, headers: Dict[str, str]) -> str:
        secrets = []
        for name, value in headers.items():
            if name.lower() in {"authorization", "x-api-key", "x-api-access-key", "api-key"}:
                secrets.append(value)
                if value.lower().startswith("bearer "):
                    secrets.append(value[7:])
        return redact_secrets(message, secrets)

    async def close(self) -> None:
        await self.session.close()

    def request_slot(self, key: str) -> asyncio.Lock:
        """按服务实例串行调用；无等待者后自动释放对应锁的索引。"""
        slot = self._request_slots.get(key)
        if slot is None:
            slot = asyncio.Lock()
            self._request_slots[key] = slot
        return slot

    async def check(self, response: ClientResponse) -> None:
        if response.status >= 300:
            # 错误响应同样有上限，不记录请求头、URL、原文或整个响应体。
            # 留出完整凭据的编码长度后再脱敏截断，防止只读到半个长密钥而失去匹配。
            raw = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                raw.extend(chunk[: 65536 - len(raw)])
                if len(raw) >= 65536:
                    break
            detail = self.sanitize(raw.decode("utf-8", errors="replace"), response.request_info.headers)[:500]
            raise TTSError(f"上游返回 HTTP {response.status}：{detail}")

    async def read(self, response: ClientResponse, limit: Optional[int] = None) -> bytes:
        await self.check(response)
        maximum = self.limit if limit is None else limit
        if response.content_length is not None and response.content_length > maximum:
            raise TTSError("上游响应超过大小上限")
        data = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            if len(data) + len(chunk) > maximum:
                raise TTSError("上游响应超过大小上限")
            data.extend(chunk)
        return bytes(data)

    async def json(
        self, method: str, url: str, headers: Dict[str, str], payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        validate_url(url)
        async with self.session.request(
            method, url, headers=headers, json=payload, proxy=self.proxy, allow_redirects=False
        ) as response:
            # Hex 音频恰好达到解码上限时，另给 JSON 外层状态与元数据预留有界空间。
            raw = await self.read(response, max(self.limit * 2 + 65536, 1024 * 1024))
        return await self.decode_json(raw, headers)

    async def multipart(self, url: str, headers: Dict[str, str], form: FormData) -> Dict[str, Any]:
        validate_url(url)
        async with self.session.post(
            url, headers=headers, data=form, proxy=self.proxy, allow_redirects=False
        ) as response:
            raw = await self.read(response, 1024 * 1024)
        return await self.decode_json(raw, headers)

    async def decode_json(self, raw: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
        try:
            data = await asyncio.to_thread(json.loads, raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise TTSError("上游未返回有效 JSON") from None
        if not isinstance(data, dict):
            raise TTSError("上游 JSON 必须是对象")
        if data.get("error") or data.get("code") not in (None, "", 0, 200):
            detail = data.get("message") or data.get("error") or data.get("code")
            raise TTSError(f"上游业务错误：{self.sanitize(str(detail), headers)[:500]}")
        return data

    async def download(self, url: str, limit: Optional[int] = None) -> bytes:
        validate_url(url)
        # 独立请求不继承任何服务商 Authorization。禁止重定向到未经确认的目的地。
        async with self.session.get(url, proxy=self.proxy, allow_redirects=False) as response:
            return await self.read(response, limit)
