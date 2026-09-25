"""Regression tests for discord_remote_control["explicit_mention"] metadata.

``_handle_message`` strips the literal ``<@BOT_ID>`` token out of
``message.content`` (and ``_auto_create_thread`` may further mutate it) before
building the ``discord_remote_control`` metadata dict. The dict's
``explicit_mention`` value must reflect whether the RAW, unmutated message
content contained a literal ``<@BOT_ID>``/``<@!BOT_ID>`` mention token — not a
re-derived value read from the already-mutated ``message.content`` (which
would always be False, since the token was already stripped out).
"""

from unittest.mock import AsyncMock

import pytest
from .test_discord_channel_controls import (  # noqa: E402
    FakeTextChannel,
    FakeThread,
    adapter,  # noqa: F401  (fixture)
    make_message,
)


@pytest.mark.asyncio
async def test_inline_literal_mention_sets_explicit_mention_true(adapter, monkeypatch):
    """A literal ``<@BOT_ID>`` token in the raw content must be reported as an
    explicit mention even though ``_handle_message`` strips that token out of
    ``message.content`` before building the metadata dict."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    adapter._auto_create_thread = AsyncMock(return_value=FakeThread(channel_id=999))

    bot_user = adapter._client.user
    message = make_message(
        channel=FakeTextChannel(channel_id=700),
        content=f"<@{bot_user.id}> hello",
        mentions=[bot_user],
    )
    await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.metadata["discord_remote_control"]["explicit_mention"] is True


@pytest.mark.asyncio
async def test_reply_ping_only_sets_explicit_mention_false(adapter, monkeypatch):
    """A reply-ping (bot present in ``message.mentions`` with no literal ``<@id>``
    token anywhere in the raw content) must NOT be reported as an explicit
    mention: ``mention_prefix``/``_self_is_explicitly_mentioned`` treats reply
    pings as "mentioned" for the unrelated bare-ping-drop/thread-routing
    heuristic, but that signal must never be substituted for the raw-mention
    check that gates remote control."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    adapter._auto_create_thread = AsyncMock(return_value=FakeThread(channel_id=999))

    bot_user = adapter._client.user
    message = make_message(
        channel=FakeTextChannel(channel_id=701),
        content="hello",
        mentions=[bot_user],
    )
    await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.metadata["discord_remote_control"]["explicit_mention"] is False


@pytest.mark.asyncio
async def test_thread_parent_channel_allowlist_sets_explicit_channel_true(adapter, monkeypatch):
    """A message sent inside a thread whose parent channel is allow-listed
    still resolves ``explicit_channel`` True via the (unmodified) thread->parent
    channel-key resolution."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "900")
    monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)
    adapter._fetch_channel_context = AsyncMock(return_value="")

    parent = FakeTextChannel(channel_id=900)
    thread = FakeThread(channel_id=901, parent=parent)
    message = make_message(channel=thread, content="hello")
    await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.metadata["discord_remote_control"]["explicit_channel"] is True
