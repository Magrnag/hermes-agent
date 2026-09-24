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
