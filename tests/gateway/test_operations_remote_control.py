import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.remote_control import maybe_handle_remote_control
from operations import (
    Action,
    PermissionClass,
    RunStatus,
    StructuredResult,
)


def _event(*, is_bot=False, text="状態を教えて"):
    return SimpleNamespace(
        text=text,
        message_id="message-1",
        raw_message=SimpleNamespace(author=SimpleNamespace(bot=is_bot)),
        metadata={
            "discord_remote_control": {
                "explicit_mention": True,
                "explicit_channel": True,
                "allowed_guild": True,
                "is_dm": False,
                "message_id": "message-1",
                "chat_id": "channel-1",
                "thread_id": "thread-1",
                "guild_id": "guild-1",
                "user_id": "user-1",
                "is_bot": is_bot,
            }
        },
    )


def _facts(**overrides):
    base = {
        "explicit_mention": True,
        "explicit_channel": True,
        "allowed_guild": True,
        "is_dm": False,
        "message_id": "message-1",
        "chat_id": "channel-1",
        "thread_id": "thread-1",
        "guild_id": "guild-1",
        "user_id": "user-1",
        "is_bot": False,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_bridge_preserves_admission_identity_and_completion_correlation(
    monkeypatch,
):
    adapter = SimpleNamespace(send=AsyncMock())
    gateway = SimpleNamespace(_adapter_for_source=lambda source: adapter)
    source = SimpleNamespace(platform="discord", chat_id="channel-1", user_id="user-1")
    captured = {}

    class Runtime:
        def handle(self, action, context, notifier=None):
            captured["action"] = action
            captured["context"] = context
            assert notifier is not None
            notifier(
                StructuredResult(
                    ok=True,
                    action=Action.STATUS,
                    permission=PermissionClass.READ_ONLY,
                    status=RunStatus.SUCCEEDED,
                    title="完了",
                )
            )
            return StructuredResult(
                ok=True,
                action=Action.STATUS,
                permission=PermissionClass.READ_ONLY,
                status=RunStatus.SUCCEEDED,
                title="受付",
            )

    monkeypatch.setattr("operations.get_default_runtime", lambda: Runtime())
    reply = await maybe_handle_remote_control(gateway, _event(), source)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "受付" in reply
    assert captured["context"].user_id == "user-1"
    assert captured["context"].bot_mentioned is True
    assert captured["context"].allowed_guild is True
    adapter.send.assert_awaited_once()
    args = adapter.send.await_args
    assert args.args[0] == "channel-1"
    assert args.kwargs["reply_to"] == "message-1"
    assert args.kwargs["metadata"]["thread_id"] == "thread-1"

    adapter.send.reset_mock()
    assert await maybe_handle_remote_control(gateway, _event(is_bot=True), source) == ""
    adapter.send.assert_not_awaited()

    assert (
        await maybe_handle_remote_control(gateway, _event(text="/status"), source)
        is None
    )
    assert (
        await maybe_handle_remote_control(
            gateway,
            _event(text="github-watchを今すぐ実行"),
            source,
            control_only=True,
        )
        is None
    )


def test_context_allowed_guild_fails_closed_on_dm_unset_and_mismatch():
    from gateway.remote_control import _context

    source = SimpleNamespace(
        platform="discord", chat_id="channel-1", user_id="user-1", profile=None
    )
    event = SimpleNamespace(message_id="message-1")

    admitted = _context(source, event, _facts())
    assert admitted.allowed_guild is True

    mismatched = _context(source, event, _facts(allowed_guild=False))
    assert mismatched.allowed_guild is False

    dm_no_guild = _context(
        source, event, _facts(is_dm=True, allowed_guild=False, guild_id=None)
    )
    assert dm_no_guild.allowed_guild is False

    unset_facts = _facts()
    del unset_facts["allowed_guild"]
    unset = _context(source, event, unset_facts)
    assert unset.allowed_guild is False


def test_facts_context_classify_permission_guild_gate_boundaries():
    from gateway.remote_control import _context
    from operations import Action, PermissionClass, RemoteAction
    from operations.permissions import classify_permission

    source = SimpleNamespace(
        platform="discord", chat_id="channel-1", user_id="user-1", profile=None
    )
    event = SimpleNamespace(message_id="message-1")
    run_action = RemoteAction(action=Action.RUN, target="github-watch", args={})

    admitted = _context(source, event, _facts())
    assert classify_permission(run_action, admitted) is PermissionClass.SAFE_MANUAL

    guild_mismatch = _context(source, event, _facts(allowed_guild=False))
    assert classify_permission(run_action, guild_mismatch) is PermissionClass.DENIED

    channel_mismatch = _context(source, event, _facts(explicit_channel=False))
    assert classify_permission(run_action, channel_mismatch) is PermissionClass.DENIED

    dm_no_guild = _context(
        source,
        event,
        _facts(is_dm=True, allowed_guild=False, explicit_channel=False, guild_id=None),
    )
    assert classify_permission(run_action, dm_no_guild) is PermissionClass.DENIED
