"""统一分级日志与异步调用关联；记录元数据，隐藏凭据和音频内容。"""

from contextlib import contextmanager
from contextvars import ContextVar
from logging import DEBUG, ERROR, INFO, WARNING, Logger
from pathlib import Path
from traceback import extract_tb
from typing import Any, Callable, Iterable, Iterator, Tuple
from urllib.parse import quote, quote_plus, urlsplit
from uuid import uuid4

import json
import re


_TRACE: ContextVar[str] = ContextVar("tts_trace", default="")
_PRIVATE: ContextVar[Tuple[str, ...]] = ContextVar("tts_private_values", default=())
_LEVELS = {"DEBUG": DEBUG, "INFO": INFO, "WARNING": WARNING, "ERROR": ERROR}
_URL = re.compile(r"(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)


def redact_secrets(message: str, values: Iterable[str]) -> str:
    """先隐藏完整凭据及常见编码形式，再由调用方截断，避免泄露较长密钥的前缀。"""
    variants = set()
    for value in (*_PRIVATE.get(), *values):
        if value:
            variants.update((value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1]))
    # 多个账号的密钥可能互为前缀，先替换长值以免较短值破坏完整匹配。
    for value in sorted(variants, key=len, reverse=True):
        message = message.replace(value, "[已隐藏]")
    return message


@contextmanager
def trace_scope(private_values: Iterable[str] = ()) -> Iterator[str]:
    """子任务沿用当前关联编号，并发请求各自保留独立上下文。"""
    token = _TRACE.set(_TRACE.get() or uuid4().hex[:12])
    private_token = _PRIVATE.set(
        (*_PRIVATE.get(), *(value for value in private_values if isinstance(value, str) and value))
    )
    try:
        yield _TRACE.get()
    finally:
        _PRIVATE.reset(private_token)
        _TRACE.reset(token)


class Diagnostics:
    def __init__(self, logger: Logger, level: str, redact: Callable[[str], str]) -> None:
        self.logger = logger
        self.level = _LEVELS[level]
        self.redact = redact

    def _safe(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        value = self.redact(redact_secrets(value, _PRIVATE.get()))

        # URL 仅保留协议与主机，去掉用户信息、路径、签名和查询参数。
        def origin(match: re.Match) -> str:
            try:
                parsed = urlsplit(match.group())
                return f"{parsed.scheme}://{parsed.hostname or '[host]'}"
            except ValueError:
                return "[URL]"

        value = _URL.sub(origin, value)
        value = re.sub(r"data:audio/[^\s]+", "[音频已隐藏]", value, flags=re.IGNORECASE)
        return " ".join(value.split())[:600]

    def emit(self, level: int, event: str, **fields: Any) -> None:
        if level < self.level or not self.logger.isEnabledFor(level):
            return
        details = " ".join(
            f"{key}={json.dumps(self._safe(value), ensure_ascii=False)}" for key, value in fields.items()
        )
        self.logger.log(level, "[TTS] %s trace=%s %s", event, _TRACE.get() or "-", details)

    def debug(self, event: str, **fields: Any) -> None:
        self.emit(DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self.emit(INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self.emit(WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.emit(ERROR, event, **fields)

    def unexpected(self, event: str, error: Exception, **fields: Any) -> None:
        # 只记录堆栈位置和异常类型，避免异常消息或源码行带出密钥、原文。
        frames = extract_tb(error.__traceback__)
        stack = " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames)
        self.error(event, error_type=type(error).__name__, stack=stack, **fields)
