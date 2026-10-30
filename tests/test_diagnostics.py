"""日志关联、级别过滤、失败定位与敏感数据隐藏。"""

from aiohttp import web
from maibot_sdk.context import PluginContext, PluginPaths
from unittest.mock import AsyncMock

import asyncio
import logging
import re

from tts_under_test.domain.models import TTSError
from tts_under_test.infrastructure.diagnostics import Diagnostics, trace_scope


async def test_concurrent_traces_remain_isolated_and_values_are_redacted(caplog):
    caplog.set_level(logging.DEBUG)
    log = Diagnostics(
        logging.getLogger("tts.diagnostics.test"), "INFO", lambda value: value.replace("private-key", "[隐藏]")
    )

    async def work(label):
        with trace_scope() as trace:
            log.debug("filtered", label=label)
            log.info(
                "start",
                label=label,
                endpoint="https://user:password@example.com/private/path?token=signed-secret",
                key="private-key",
            )
            await asyncio.sleep(0)
            with trace_scope() as nested:
                assert nested == trace
                log.warning("end", label=label)
            return trace

    traces = await asyncio.gather(work("a"), work("b"))
    assert traces[0] != traces[1]
    for trace in traces:
        assert sum(f"trace={trace}" in record.message for record in caplog.records) == 2
    for value in ("filtered", "private-key", "password", "private/path", "signed-secret"):
        assert value not in caplog.text
    assert "https://example.com" in caplog.text


async def test_route_http_transcode_and_send_share_trace_and_report_failures(
    plugin_module, tmp_path, server, wav_audio, caplog
):
    caplog.set_level(logging.DEBUG)
    text = "PRIVATE-SPOKEN-TEXT"

    async def handle(request):
        assert request.path.endswith("/audio/speech")
        body = await request.json()
        if body["model"] == "bad":
            return web.Response(status=503, text="key-for-test " + text)
        return web.Response(body=wav_audio, content_type="audio/wav")

    server.handler = handle
    plugin = plugin_module.create_plugin()
    config = plugin.get_default_config()
    config["runtime"]["log_level"] = "DEBUG"
    plugin.set_plugin_config(config)
    rpc = AsyncMock(return_value={"success": True, "sent": True})
    plugin._set_context(PluginContext("maimai-tts-plugin", rpc_call=rpc, paths=PluginPaths(tmp_path, tmp_path)))
    await plugin.on_load()
    try:
        await plugin.store.save(
            {
                "profiles": [
                    {
                        "id": "vendor",
                        "name": "Vendor",
                        "kind": "compatible",
                        "base_url": server.base + "/v1",
                        "api_key": "key-for-test",
                        "models": [{"id": "bad"}, {"id": "good"}],
                    }
                ],
                "primary": {"provider_id": "vendor", "model_id": "bad"},
                "fallbacks": [{"provider_id": "vendor", "model_id": "good"}],
            },
            0,
        )
        caplog.clear()
        result = await plugin.handle_tts_tool(text, stream_id="real-stream")
        assert result["success"] is True
        chain = [
            record
            for record in caplog.records
            if any(
                term in record.message
                for term in ("合成开始", "HTTP 响应到达", "服务商合成失败", "语音转码完成", "发送语音成功")
            )
        ]
        assert {record.levelno for record in chain} >= {logging.DEBUG, logging.INFO, logging.WARNING}
        assert len({re.search(r"trace=(\w+)", record.message).group(1) for record in chain}) == 1
        assert "status=503" in caplog.text
        assert "切换备选路由" in caplog.text
        assert text not in caplog.text
        assert "key-for-test" not in caplog.text

        rpc.return_value = {"success": False, "sent": False}
        caplog.clear()
        result = await plugin.handle_tts_tool(text, stream_id="real-stream")
        assert result["success"] is False
        assert any(record.levelno == logging.ERROR and "发送语音失败" in record.message for record in caplog.records)

        caplog.clear()
        await plugin.handle_probability_tts(
            message={"raw_message": [{"type": "text", "data": text}]}, stream_id="real-stream"
        )
        assert 'reason="probability_disabled"' in caplog.text
    finally:
        await plugin.on_unload()


async def test_background_failure_hides_draft_key_and_text(plugin_module, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(PluginContext("maimai-tts-plugin", paths=PluginPaths(tmp_path, tmp_path)))
    await plugin.on_load()
    try:

        async def fail():
            raise TTSError("draft-secret spoken-private https://user:password@example.com/audio?signature=secret")

        result = plugin.studio.start(fail, operation_name="models", private_values=("draft-secret", "spoken-private"))
        await asyncio.sleep(0)
        job = plugin.studio.job(result["job_id"])
        assert job["state"] == "error"
        for secret in ("draft-secret", "spoken-private"):
            assert secret not in job["error"]
            assert secret not in caplog.text
        assert "signature=secret" not in caplog.text
        assert any(record.levelno == logging.ERROR and "工作台任务失败" in record.message for record in caplog.records)
    finally:
        await plugin.on_unload()
