"""有界 SSE 读取、JSON 字段定位与声明式模板渲染。"""

from aiohttp import ClientResponse
from typing import Any, AsyncIterator, Dict, Tuple, Union

import asyncio
import json

from ..domain.compatibility import TOKEN
from ..domain.models import TTSError


def render_template(value: Any, variables: Dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: render_template(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [render_template(item, variables) for item in value]
    if not isinstance(value, str):
        return value
    exact = TOKEN.fullmatch(value)
    if exact:
        return variables[exact[1]]
    return TOKEN.sub(lambda match: str(variables[match[1]]), value)


def at_pointer(value: Any, pointer: str, *, required: bool = True) -> Any:
    """按 RFC 6901 精确读取；可选字段缺失返回 None，不尝试其他字段。"""
    current = value
    for raw in pointer.split("/")[1:] if pointer else []:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and key in current:
            current = current[key]
        elif isinstance(current, list) and key.isdecimal() and int(key) < len(current):
            current = current[int(key)]
        elif required:
            raise TTSError(f"响应缺少配置的 JSON 字段：{pointer}")
        else:
            return None
    return current


async def json_value(raw: Union[bytes, str]) -> Any:
    try:
        return await asyncio.to_thread(json.loads, raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise TTSError("上游未返回有效 JSON，请核对响应类型与字段路径") from None


async def sse_events(response: ClientResponse, limit: int) -> AsyncIterator[Tuple[str, str]]:
    """支持跨网络分块的 UTF-8、CRLF 和多行 data，限制整个流与事件大小。"""
    buffer = bytearray()
    event = ""
    lines = []
    total = 0
    count = 0
    scan = 0
    async for chunk in response.content.iter_chunked(8192):
        total += len(chunk)
        if total > limit:
            raise TTSError("SSE 响应超过大小上限")
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n", scan)
            if newline < 0:
                scan = len(buffer)
                break
            raw = buffer[:newline]
            del buffer[: newline + 1]
            scan = 0
            try:
                line = raw.rstrip(b"\r").decode("utf-8")
            except UnicodeDecodeError:
                raise TTSError("SSE 响应不是有效 UTF-8") from None
            if not line:
                if lines:
                    count += 1
                    if count > 10000:
                        raise TTSError("SSE 事件数量超过上限")
                    yield event, "\n".join(lines)
                event, lines = "", []
            elif line.startswith("event:"):
                event = line[6:].removeprefix(" ")
            elif line.startswith("data:"):
                lines.append(line[5:].removeprefix(" "))
    # 未以空行结束的帧不视为已完成；调用方必须等到协议完成事件。
    if buffer or lines:
        raise TTSError("SSE 连接在完整事件结束前断开")
