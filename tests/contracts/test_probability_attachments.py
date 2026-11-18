"""抽样后按段转换正文，保留原生附件字段、引用正文与发送次序。"""

from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import asyncio
import base64
import hashlib
import json

import pytest

from maimai_tts_contract.domain.breeze_control import event_tags
from maimai_tts_contract.domain.models import Audio, TTSError
from test_prompt_pipeline import CASES, TEXT
from test_speech_correction import ReplyBot, prepare_reply, setup_bot


pytestmark = pytest.mark.asyncio


def text(value: str = TEXT) -> Dict[str, Any]:
    return {"type": "text", "data": value}


def mention(value: str = "member") -> Dict[str, Any]:
    return {"type": "at", "data": {"target_user_id": value, "target_user_nickname": "昵称", "target_user_cardname": "群名片"}}


def emoji(value: bytes = b"GIF89a-test") -> Dict[str, Any]:
    return {"type": "emoji", "data": "表情包描述", "hash": hashlib.sha256(value).hexdigest(),
            "binary_data_base64": base64.b64encode(value).decode(), "mime_type": "image/gif", "file_name": "reaction.gif"}


def message(*parts: Dict[str, Any], quote_previous: bool = False) -> Dict[str, Any]:
    return {"segments": list(parts), "quote_previous": quote_previous}


async def convert(
    bot: ReplyBot, messages: List[Dict[str, Any]], *, reply_id: str = "reply-1", response: str = TEXT,
) -> List[Dict[str, Any]]:
    await prepare_reply(bot, response, reply_id)
    result = await bot.handle_probability_tts(
        "before_send", reply_id, session_id="test-stream", chat={"is_group_chat": False}, messages=messages,
    )
    return result.get("messages", messages)


async def deliver(
    bot: ReplyBot, messages: List[Dict[str, Any]], *, quotes: Optional[List[bool]] = None,
    after_twice: bool = False, sent: bool = True,
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    events = []

    async def sdk_send(segments: List[Dict[str, Any]], stream_id: str, **kwargs: Any) -> Dict[str, Any]:
        events.append(("sdk", deepcopy(segments)))
        return {"sent": True, "message_id": "sdk-delivered"}

    bot.ctx.send.hybrid = AsyncMock(side_effect=sdk_send)
    for index, item in enumerate(messages):
        outgoing = {"session_id": "test-stream", "message_id": "outgoing-" + str(index),
                    "raw_message": deepcopy(item["segments"]),
                    "message_info": {"additional_config": {"adapter_context": "preserved"}}}
        quoted = quotes[index] if quotes else item.get("quote_previous", False)
        result = await bot.arrange_probability_reply_quote(
            message=outgoing, set_reply=quoted, reply_message_id="incoming-target", storage_message=True, show_log=True,
        )
        changed = result.get("modified_kwargs", {})
        outgoing = changed.get("message", outgoing)
        quoted = changed.get("set_reply", quoted)
        if quoted and not any(part["type"] == "reply" for part in outgoing["raw_message"]):
            outgoing["raw_message"].insert(0, {"type": "reply", "data": {"target_message_id": "incoming-target"}})
        if sent:
            events.append(("host", deepcopy(outgoing["raw_message"])))
        assert outgoing["message_info"]["additional_config"]["adapter_context"] == "preserved"
        await bot.send_probability_reply_attachments(message=outgoing, sent=sent)
        if after_twice:
            await bot.send_probability_reply_attachments(message=outgoing, sent=sent)
    return events


async def test_probability_inserts_events_in_their_cleaned_segments(serve, service):
    bot, calls, _ = await setup_bot(
        serve, service, kind="breeze", strip_stage_directions=True, probability_tone_enabled=True,
    )
    first, second = "你好（微笑）呀。", "今天（点头）很开心。"
    tag = event_tags("zh")["zh"][1]
    bot.ctx.call_capability.return_value = {
        "success": True, "response": json.dumps({"events": [{"before": "你好呀", "tag": tag}]}),
    }
    result = await convert(bot, [message(text(first)), message(text(second))], response=first + second)
    assert [call["text"] for call in calls] == [tag + "你好呀。", "今天很开心。"]
    assert [item["segments"][-1]["data"] for item in result] == [first, second]
    bot.ctx.call_capability.assert_awaited_once()
    prompt = bot.ctx.call_capability.await_args.kwargs["prompt"]
    assert prompt[1]["content"] == "你好呀。\n今天很开心。"
    assert first + "\n" + second in prompt[2]["content"]


@pytest.mark.parametrize("kind", CASES)
async def test_platform_text_converts_while_standalone_emoji_stays_in_host_flow(serve, service, kind):
    bot, calls, _ = await setup_bot(serve, service, kind=kind)
    sticker = emoji()
    messages = [message(sticker), message(text()), message(emoji(b"another-emoji"))]
    original = deepcopy(messages)
    converted = await convert(bot, messages)
    assert messages == original and len(calls) == 1
    assert converted[0] == original[0] and converted[2] == original[2]
    events = await deliver(bot, converted)
    assert [parts[0]["type"] for _, parts in events] == ["emoji", "voice", "emoji"]
    assert events[0] == ("host", original[0]["segments"])
    assert events[2] == ("host", original[2]["segments"])
    bot.ctx.send.hybrid.assert_not_awaited()


async def test_inline_mentions_and_emoji_keep_all_fields_and_original_order(serve, service):
    bot, calls, _ = await setup_bot(serve, service)
    first, last, sticker = mention("first"), mention("last"), emoji()
    messages = [message(first, text(), last, sticker)]
    converted = await convert(bot, messages)
    events = await deliver(bot, converted, after_twice=True)
    assert len(calls) == 1
    assert [parts[0]["type"] for _, parts in events] == ["at", "voice", "at", "emoji"]
    assert events[0][1] == [first] and events[2][1] == [last] and events[3][1] == [sticker]
    assert all(len(parts) == 1 for _, parts in events)
    assert not bot._probability_voice_states and not bot._probability_attachment_states


@pytest.mark.parametrize("declared_hash", [None, "", "absent"])
async def test_host_computed_emoji_hash_matches_original_binary_attachment(serve, service, declared_hash):
    bot, calls, _ = await setup_bot(serve, service)
    sticker = emoji()
    expected_hash = sticker.pop("hash")
    if declared_hash != "absent":
        sticker["hash"] = declared_hash
    converted = await convert(bot, [message(mention(), text(), sticker)])
    # 宿主把回复扩展字典还原为原生组件时，为二进制表情补齐哈希。
    converted[0]["segments"][2]["hash"] = expected_hash
    events = await deliver(bot, converted, after_twice=True)
    assert len(calls) == 1
    assert [parts[0]["type"] for _, parts in events] == ["at", "voice", "emoji"]
    assert events[-1][1][0] == dict(sticker, hash=expected_hash)
    assert not bot._probability_voice_states and not bot._probability_attachment_states


@pytest.mark.parametrize("field,value", [("attach_emoji", {"emotion": "开心"}), ("attach_at", ["source-message"])])
async def test_declared_emoji_and_mentions_do_not_cancel_whole_reply(serve, service, field, value):
    bot, _, _ = await setup_bot(serve, service)
    args = {"_reply_id": "declared", "plugin_options": {bot._probability_extension_name(): {}}, field: value}
    await bot.prepare_probability_tts_reply(response=TEXT, session_id="test-stream", reply_tool_args=args)
    assert bot._probability_reply_states["declared"].should_convert


async def test_quote_keeps_text_and_reference_then_sends_voice_and_attachments_once(serve, service):
    bot, _, _ = await setup_bot(serve, service, probability_quote_keep_text=True)
    at, sticker = mention(), emoji()
    converted = await convert(bot, [message(at, text(), sticker)])
    events = await deliver(bot, converted, quotes=[True], after_twice=True)
    assert [parts[0]["type"] for _, parts in events] == ["at", "reply", "voice", "emoji"]
    assert events[1][1] == [{"type": "reply", "data": {"target_message_id": "incoming-target"}}, text()]
    assert sum(part["type"] == "at" for _, parts in events for part in parts) == 1
    assert events[-1][1] == [sticker]


async def test_quote_option_off_uses_host_resolved_quote_without_canceling_others(serve, service):
    bot, calls, _ = await setup_bot(serve, service, probability_quote_keep_text=False)
    messages = [message(text("第一段。")), message(mention(), text("引用段。"), quote_previous=True),
                message(text("最后一段。")), message(emoji())]
    converted = await convert(bot, messages, response="第一段。引用段。最后一段。")
    assert len(calls) == 3
    assert {body["input"] for body in calls} == {"第一段。", "引用段。", "最后一段。"}
    events = await deliver(bot, converted, quotes=[False, True, False, False])
    assert [parts[0]["type"] for _, parts in events] == ["voice", "reply", "voice", "emoji"]
    assert events[1][1][1:] == messages[1]["segments"]


async def test_first_automatic_quote_keeps_existing_late_decision(serve, service):
    bot, calls, _ = await setup_bot(serve, service, probability_quote_keep_text=False)
    converted = await convert(bot, [message(text())])
    assert len(calls) == 1
    events = await deliver(bot, converted, quotes=[True])
    assert events == [("host", [{"type": "reply", "data": {"target_message_id": "incoming-target"}}, text()])]


async def test_explicit_reply_segment_skips_tts_when_quote_option_is_off(serve, service):
    bot, calls, _ = await setup_bot(serve, service, probability_quote_keep_text=False)
    original = message({"type": "reply", "data": {"target_message_id": "prior"}}, text())
    assert await convert(bot, [original]) == [original]
    assert not calls


@pytest.mark.parametrize("enabled,probability", [(True, 0), (False, 1)])
async def test_scope_or_probability_miss_keeps_every_native_field(serve, service, enabled, probability):
    bot, calls, _ = await setup_bot(serve, service, probability_private_enabled=enabled)
    service.store._config.feature.probability = probability
    messages = [message(mention(), text()), message(emoji())]
    assert await convert(bot, messages) == messages
    assert not calls and not bot._probability_voice_states


async def test_later_synthesis_failure_keeps_complete_original_group(serve, service):
    bot, _, _ = await setup_bot(serve, service)
    synthesize = bot._synthesize_audio
    count = 0

    async def fail_second(**kwargs: Any) -> Audio:
        nonlocal count
        count += 1
        if count == 2:
            raise TTSError("test synthesis failure")
        return await synthesize(**kwargs)

    bot._synthesize_audio = fail_second
    messages = [message(mention(), text()), message(emoji()), message(text("另一段。"))]
    assert await convert(bot, messages, response=TEXT + "另一段。") == messages
    assert not bot._probability_voice_states and not bot._probability_attachment_states
    bot.ctx.send.hybrid.assert_not_awaited()


async def test_audio_cache_reuse_keeps_concurrent_attachment_identity(serve, service):
    bot, calls, _ = await setup_bot(serve, service)
    left = [message(mention("left"), text(), emoji(b"left"))]
    right = [message(mention("right"), text(), emoji(b"right"))]
    a, b = await asyncio.gather(convert(bot, left, reply_id="left"), convert(bot, right, reply_id="right"))
    assert len(calls) == 1
    right_events = await deliver(bot, b, quotes=[True], after_twice=True)
    left_events = await deliver(bot, a, quotes=[True], after_twice=True)
    assert right_events[0][1] == [mention("right")] and right_events[-1][1] == [emoji(b"right")]
    assert left_events[0][1] == [mention("left")] and left_events[-1][1] == [emoji(b"left")]
    assert not bot._probability_voice_states and not bot._probability_attachment_states


async def test_failed_voice_delivery_discards_deferred_attachments(serve, service):
    bot, _, _ = await setup_bot(serve, service)
    converted = await convert(bot, [message(text(), emoji())])
    assert await deliver(bot, converted, sent=False) == []
    assert not bot._probability_attachment_states
    bot.ctx.send.hybrid.assert_not_awaited()


async def test_unsupported_media_segment_stays_native_while_other_text_converts(serve, service):
    bot, calls, _ = await setup_bot(serve, service)
    mixed = message(text("图片说明。"), {**emoji(), "type": "image"})
    converted = await convert(bot, [mixed, message(text())], response="图片说明。" + TEXT)
    assert converted[0] == mixed and len(calls) == 1


async def test_whole_reply_samples_once_and_repeated_hook_reuses_decision(serve, service):
    bot, calls, _ = await setup_bot(serve, service)
    samples = 0

    def sample() -> bool:
        nonlocal samples
        samples += 1
        return True

    bot._sample_probability = sample
    response = "第一段。第二段。"
    await prepare_reply(bot, response)
    await prepare_reply(bot, response)
    result = await bot.handle_probability_tts(
        "before_send", "reply-1", session_id="test-stream", chat={"is_group_chat": False},
        messages=[message(text("第一段。")), message(emoji()), message(text("第二段。"))],
    )
    assert samples == 1 and len(calls) == 2
    assert [parts[0]["type"] for _, parts in await deliver(bot, result["messages"])] == ["voice", "emoji", "voice"]


async def test_keep_text_sends_complete_body_once_between_separate_mentions_and_voice(serve, service):
    bot, _, _ = await setup_bot(serve, service, probability_keep_text=True)
    converted = await convert(bot, [message(mention(), text("第一句。"), text("第二句。"), emoji())], response="第一句。第二句。")
    events = await deliver(bot, converted)
    assert [parts[0]["type"] for _, parts in events] == ["at", "text", "voice", "emoji"]
    assert events[1][1] == [text("第一句。"), text("第二句。")]


async def test_known_quote_after_leading_emoji_keeps_first_body_for_late_host_decision(serve, service):
    bot, calls, _ = await setup_bot(serve, service, probability_quote_keep_text=False)
    messages = [message(emoji()), message(text(), quote_previous=True)]
    converted = await convert(bot, messages)
    assert len(calls) == 1 and converted[0] == messages[0]
    events = await deliver(bot, converted, quotes=[False, True])
    assert [parts[0]["type"] for _, parts in events] == ["emoji", "reply"]
    assert events[1][1][-1] == text()


async def test_quote_previous_intent_does_not_skip_voice_when_host_cancels_quote(serve, service):
    bot, calls, _ = await setup_bot(serve, service, probability_quote_keep_text=False)
    converted = await convert(bot, [message(text("第一段。")), message(text("第二段。"), quote_previous=True)],
                              response="第一段。第二段。")
    events = await deliver(bot, converted, quotes=[False, False])
    assert len(calls) == 2
    assert [parts[0]["type"] for _, parts in events] == ["voice", "voice"]


async def test_other_send_with_same_audio_does_not_consume_probability_state(serve, service):
    bot, _, _ = await setup_bot(serve, service)
    converted = await convert(bot, [message(mention(), text(), emoji())])
    voice = converted[0]["segments"][-1]
    result = await bot.arrange_probability_reply_quote(
        message={"session_id": "test-stream", "raw_message": [voice]}, set_reply=False,
    )
    assert result == {"action": "continue"} and bot._probability_voice_states
    events = await deliver(bot, converted)
    assert [parts[0]["type"] for _, parts in events] == ["at", "voice", "emoji"]


async def test_deferred_attachments_respect_storage_flags_and_expire(serve, service):
    bot, _, _ = await setup_bot(serve, service)
    converted = await convert(bot, [message(text(), emoji())])
    result = await bot.arrange_probability_reply_quote(
        message={"session_id": "test-stream", "raw_message": converted[0]["segments"]},
        storage_message=False, show_log=False,
    )
    outgoing = result["modified_kwargs"]["message"]
    await bot.send_probability_reply_attachments(message=outgoing, sent=True)
    assert bot.ctx.send.hybrid.await_args.kwargs["storage_message"] is False
    assert bot.ctx.send.hybrid.await_args.kwargs["show_log"] is False
    converted = await convert(bot, [message(text(), emoji())], reply_id="expired")
    result = await bot.arrange_probability_reply_quote(
        message={"session_id": "test-stream", "raw_message": converted[0]["segments"]},
    )
    for state in bot._probability_attachment_states.values():
        state.expires_at = 0
    count = bot.ctx.send.hybrid.await_count
    await bot.send_probability_reply_attachments(message=result["modified_kwargs"]["message"], sent=True)
    assert bot.ctx.send.hybrid.await_count == count and not bot._probability_attachment_states
