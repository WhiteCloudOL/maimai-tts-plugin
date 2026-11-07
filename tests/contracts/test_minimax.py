"""MiniMax 官方三种传输与百炼包装的离线契约，不访问真实平台。

协议来源及命令见同目录 README.md。请求体预期独立定义，避免用被测
payload helper 生成预期值，掩盖必填字段遗漏。
"""

from aiohttp import WSMsgType, web
from copy import deepcopy
from typing import Any, Dict, List

import asyncio
import json

import pytest

from maimai_tts_contract.domain.models import Profile, TTSError
from maimai_tts_contract.providers.bailian_minimax import BailianMiniMaxProvider
from maimai_tts_contract.providers.minimax import MiniMaxProvider


pytestmark = pytest.mark.asyncio
TEXT = "你好，这是协议回归测试。"
# 容器标识有效的内存音频；本组验证协议和字节聚合，不验证音频解码器。
AUDIO = b"ID3\x04\x00\x00contract-audio"
STATUS = {"status_code": 0, "status_msg": "success"}
MODELS = [f"speech-{version}-{quality}" for version in ("2.8", "2.6", "02", "01") for quality in ("hd", "turbo")]


def profile(base_url: str, transport: str = "http", *, model: str = "speech-2.8-hd", kind: str = "minimax") -> Profile:
    return Profile(
        id="test-minimax",
        name="协议测试",
        kind=kind,
        base_url=base_url + ("/api/v1" if kind == "bailian_minimax" else "/v1"),
        api_key="test-only-not-a-real-key",
        model=model,
        voice="male-qn-qingse",
        options={"minimax_transport": transport},
    )


def expected_body(selected: Profile) -> Dict[str, Any]:
    return {
        "model": selected.model,
        "text": TEXT,
        "voice_setting": {"voice_id": "male-qn-qingse", "speed": 1, "vol": 1, "pitch": 0, "latex_read": False},
        "audio_setting": {"sample_rate": 24000, "format": "mp3", "channel": 1, "bitrate": 128000},
        "output_format": "hex",
        "language_boost": "auto",
        "aigc_watermark": False,
    }


def result(audio: bytes = AUDIO, status: int = 2) -> Dict[str, Any]:
    return {"base_resp": dict(STATUS), "data": {"audio": audio.hex(), "status": status}, "trace_id": "test-trace"}


def frame(event: str, **fields: Any) -> Dict[str, Any]:
    return {"event": event, "base_resp": dict(STATUS), "trace_id": "test-trace", **fields}


def sse(values: List[Dict[str, Any]]) -> bytes:
    return "".join(f"data: {json.dumps(value, ensure_ascii=False)}\r\n\r\n" for value in values).encode("utf-8")


async def websocket_exchange(request, sent, *, audio_event="task_continued"):
    socket = web.WebSocketResponse()
    await socket.prepare(request)
    await socket.send_json(frame("connected_success"))
    # 每条回复都先等客户端发出对应消息，验证握手顺序而非一次性塞入所有帧。
    for index in range(3):
        message = await socket.receive()
        if message.type != WSMsgType.TEXT:
            return socket
        sent.append(json.loads(message.data))
        if index == 0:
            await socket.send_json(frame("task_started"))
    for part, final in ((AUDIO[:7], False), (AUDIO[7:], True)):
        value = frame("task_continued", data={"audio": part.hex()}, is_final=final)
        if audio_event is None:
            value.pop("event")
        await socket.send_json(value)
    await socket.send_json(frame("task_finished"))
    await socket.close()
    return socket


@pytest.mark.parametrize("model", MODELS)
async def test_http_required_model_and_complete_request(serve, service, model):
    calls = []

    async def handler(request):
        calls.append((request.path, request.headers.get("Authorization"), await request.json()))
        return web.json_response(result())

    selected = profile(await serve(handler), model=model)
    audio = await MiniMaxProvider(service.http).synthesize(selected, TEXT)
    assert calls == [("/v1/t2a_v2", "Bearer test-only-not-a-real-key", {**expected_body(selected), "stream": False})]
    assert audio.data == AUDIO and audio.request_id == "test-trace"


async def test_sse_required_model_and_aggregated_audio_is_not_duplicated(serve, service):
    calls = []

    async def handler(request):
        calls.append((request.path, request.headers.get("Authorization"), await request.json()))
        return web.Response(
            body=sse([result(AUDIO[:7], 1), result(AUDIO[7:], 1), result()]), content_type="text/event-stream"
        )

    selected = profile(await serve(handler), "sse")
    audio = await MiniMaxProvider(service.http).synthesize(selected, TEXT)
    expected = {**expected_body(selected), "stream": True, "stream_options": {"exclude_aggregated_audio": False}}
    assert calls == [("/v1/t2a_v2", "Bearer test-only-not-a-real-key", expected)]
    assert audio.data == AUDIO and audio.request_id == "test-trace"


@pytest.mark.parametrize("audio_event", ["task_continued", None], ids=["named-audio-event", "official-eventless-example"])
async def test_official_websocket_handshake_and_complete_request(serve, service, audio_event):
    sent = []
    connections = []

    async def handler(request):
        connections.append((request.path, request.headers.get("Authorization")))
        return await websocket_exchange(request, sent, audio_event=audio_event)

    selected = profile(await serve(handler), "websocket")
    audio = await service.execute(lambda: MiniMaxProvider(service.http).synthesize(selected, TEXT))
    start = expected_body(selected)
    for key in ("text", "output_format", "aigc_watermark"):
        start.pop(key)
    assert connections == [("/ws/v1/t2a_v2", "Bearer test-only-not-a-real-key")]
    assert sent == [{"event": "task_start", **start}, {"event": "task_continue", "text": TEXT}, {"event": "task_finish"}]
    assert audio.data == AUDIO and audio.request_id == "test-trace"


async def test_bailian_keeps_model_in_outer_wrapper(serve, service):
    calls = []

    async def handler(request):
        calls.append((request.path, await request.json()))
        return web.json_response({"output": result()})

    selected = profile(await serve(handler), model="MiniMax/speech-2.8-turbo", kind="bailian_minimax")
    original = deepcopy(selected.model_dump())
    audio = await BailianMiniMaxProvider(service.http).synthesize(selected, TEXT)
    inputs = expected_body(selected)
    inputs.pop("model")
    inputs["voice_setting"]["text_normalization"] = False
    assert calls == [("/api/v1/services/aigc/multimodal-generation/generation", {"model": selected.model, "input": inputs})]
    assert audio.data == AUDIO
    assert selected.model_dump() == original


@pytest.mark.parametrize("transport", ["http", "sse", "websocket"])
async def test_platform_errors_are_reported_and_credentials_are_redacted(serve, service, transport):
    error = frame("task_failed")
    error["base_resp"] = {"status_code": 1004, "status_msg": "rejected test-only-not-a-real-key"}

    async def handler(request):
        if transport == "websocket":
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json(error)
            await socket.close()
            return socket
        if transport == "sse":
            return web.Response(body=sse([error]), content_type="text/event-stream")
        return web.json_response(error)

    selected = profile(await serve(handler), transport)
    with pytest.raises(TTSError, match="1004") as raised:
        await MiniMaxProvider(service.http).synthesize(selected, TEXT)
    assert "test-only-not-a-real-key" not in str(raised.value)


@pytest.mark.parametrize("transport", ["http", "sse", "websocket"])
async def test_http_error_responses_do_not_become_audio(serve, service, transport):
    async def handler(request):
        return web.Response(status=403, text="forbidden test-only-not-a-real-key")

    selected = profile(await serve(handler), transport)
    with pytest.raises(TTSError) as raised:
        await service.execute(lambda: MiniMaxProvider(service.http).synthesize(selected, TEXT))
    assert "test-only-not-a-real-key" not in str(raised.value)


@pytest.mark.parametrize("transport", ["http", "sse"])
@pytest.mark.parametrize("body", [{}, {"base_resp": {"status_code": "0"}}, []])
async def test_http_and_sse_reject_invalid_business_status(serve, service, transport, body):
    async def handler(request):
        if transport == "sse":
            return web.Response(body=sse([body]), content_type="text/event-stream")
        return web.json_response(body)

    selected = profile(await serve(handler), transport)
    with pytest.raises(TTSError):
        await MiniMaxProvider(service.http).synthesize(selected, TEXT)


@pytest.mark.parametrize("transport", ["http", "sse", "websocket"])
@pytest.mark.parametrize("encoded", ["not-hex!", "123", True])
async def test_invalid_audio_encoding_is_rejected(serve, service, transport, encoded):
    body = result()
    body["data"]["audio"] = encoded

    async def handler(request):
        if transport == "websocket":
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json(frame("connected_success"))
            await socket.receive()
            await socket.send_json(frame("task_started"))
            await socket.receive()
            await socket.receive()
            await socket.send_json(frame("task_continued", data={"audio": encoded}, is_final=True))
            await socket.close()
            return socket
        if transport == "sse":
            return web.Response(body=sse([body]), content_type="text/event-stream")
        return web.json_response(body)

    selected = profile(await serve(handler), transport)
    with pytest.raises(TTSError, match="Hex|完整合成结果"):
        await MiniMaxProvider(service.http).synthesize(selected, TEXT)


@pytest.mark.parametrize("body", [result(AUDIO[:7], 1), result(b""), {**result(), "data": None}])
async def test_http_rejects_incomplete_or_empty_result(serve, service, body):
    async def handler(request):
        return web.json_response(body)

    selected = profile(await serve(handler))
    with pytest.raises(TTSError, match="完整合成结果"):
        await MiniMaxProvider(service.http).synthesize(selected, TEXT)


@pytest.mark.parametrize(
    "body,content_type,message",
    [
        (sse([result(AUDIO[:7], 1)]), "text/event-stream", "完整合成状态"),
        (sse([result()])[:-2], "text/event-stream", "完整事件"),
        (b"data: not-json\n\n", "text/event-stream", "有效 JSON"),
        (sse([result()]), "application/json", "text/event-stream"),
        (sse([result(b"")]), "text/event-stream", "Hex"),
    ],
    ids=["missing-final-status", "truncated-event", "invalid-json", "wrong-content-type", "empty-final-audio"],
)
async def test_sse_rejects_truncated_or_invalid_responses(serve, service, body, content_type, message):
    async def handler(request):
        return web.Response(body=body, content_type=content_type)

    selected = profile(await serve(handler), "sse")
    with pytest.raises(TTSError, match=message):
        await MiniMaxProvider(service.http).synthesize(selected, TEXT)


@pytest.mark.parametrize(
    "frames,message",
    [
        ([frame("task_started")], "建连确认前"),
        ([frame("connected_success"), frame("connected_success")], "重复返回建连"),
        ([frame("connected_success"), frame("task_started"), frame("task_started")], "重复开始"),
        ([frame("connected_success"), frame("task_continued", data={"audio": AUDIO.hex()})], "任务开始前"),
        ([frame("connected_success"), {"data": {"audio": AUDIO.hex()}}], "任务开始前"),
        ([frame("task_finished")], "缺少完整音频"),
        ([frame("connected_success"), frame("task_started"), frame("task_finished")], "缺少完整音频"),
        (
            [frame("connected_success"), frame("task_started"), frame("task_continued", data=None)],
            "音频数据应为对象",
        ),
        ([frame("connected_success"), frame("task_started"), frame("task_failed")], "合成任务失败"),
        ([frame("error")], "合成任务失败"),
    ],
    ids=[
        "start-before-connect", "duplicate-connect", "duplicate-start", "audio-before-start",
        "eventless-audio-before-start", "finish-before-start", "finish-without-audio", "invalid-audio-object",
        "task-failed", "error-event",
    ],
)
async def test_websocket_rejects_invalid_state_transitions(serve, service, frames, message):
    async def handler(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        for value in frames:
            await socket.send_json(value)
        await socket.close()
        return socket

    selected = profile(await serve(handler), "websocket")
    with pytest.raises(TTSError, match=message):
        await MiniMaxProvider(service.http).synthesize(selected, TEXT)


@pytest.mark.parametrize("final,finish", [(False, False), (True, False), (False, True)])
async def test_websocket_never_returns_truncated_audio(serve, service, final, finish):
    async def handler(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json(frame("connected_success"))
        await socket.receive()
        await socket.send_json(frame("task_started"))
        await socket.receive()
        await socket.receive()
        await socket.send_json(frame("task_continued", data={"audio": AUDIO.hex()}, is_final=final))
        if finish:
            await socket.send_json(frame("task_finished"))
        await socket.close()
        return socket

    selected = profile(await serve(handler), "websocket")
    with pytest.raises(TTSError, match="断开|缺少完整音频"):
        await MiniMaxProvider(service.http).synthesize(selected, TEXT)


@pytest.mark.parametrize("transport", ["http", "sse", "websocket"])
@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_timeout_and_cancellation_release_connections(serve, service, transport, stop):
    entered = asyncio.Event()
    release = asyncio.Event()
    ws_closed = asyncio.Event()

    async def handler(request):
        if transport == "websocket":
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            entered.set()
            async for _ in socket:
                pass
            ws_closed.set()
            return socket
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream" if transport == "sse" else "application/json"})
        await response.prepare(request)
        entered.set()
        await release.wait()
        return response

    selected = profile(await serve(handler), transport)
    task = asyncio.create_task(service.execute(lambda: MiniMaxProvider(service.http).synthesize(selected, TEXT)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TTSError, match="请求超时"):
                await asyncio.wait_for(task, 2)
        if transport == "websocket":
            await asyncio.wait_for(ws_closed.wait(), 2)
        assert not service._active
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        sessions = list(service.http._sessions.values())
        await service.close()
        assert all(session.closed for session in sessions)
