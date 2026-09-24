"""Thin gateway bridge for the profile-scoped operations runtime."""

from __future__ import annotations

import logging
import asyncio
import inspect
from typing import Any, Optional, cast

logger = logging.getLogger(__name__)


def _context(source: Any, event: Any, facts: dict) -> Any:
    from operations import RequestContext

    values = {
        "source": "discord",
        "user_id": str(facts.get("user_id") or getattr(source, "user_id", "")),
        "channel_id": str(facts.get("chat_id") or getattr(source, "chat_id", "")),
        "authorized": True,
        "bot_mentioned": bool(facts.get("explicit_mention")),
        "is_local": False,
        "correlation_id": facts.get("message_id") or getattr(event, "message_id", None),
        "session_id": facts.get("thread_id")
        or facts.get("chat_id")
        or getattr(source, "chat_id", ""),
        "profile": getattr(source, "profile", None),
        "allowed_channel": bool(facts.get("explicit_channel")),
        "thread_id": facts.get("thread_id"),
        "message_id": facts.get("message_id"),
        "guild_id": facts.get("guild_id"),
    }
    return RequestContext.model_validate(values)


async def maybe_handle_remote_control(
    gateway: Any,
    event: Any,
    source: Any,
    *,
    control_only: bool = False,
) -> Optional[str]:
    """Handle an admitted Discord operations request, or return None for ordinary chat."""
    platform = getattr(source, "platform", None)
    platform_name = getattr(platform, "value", platform)
    if platform_name != "discord":
        return None
    metadata = getattr(event, "metadata", {}) or {}
    facts = metadata.get("discord_remote_control")
    text = getattr(event, "text", "") or ""
    if not isinstance(facts, dict) or not text:
        return None
    author = getattr(getattr(event, "raw_message", None), "author", None)
    if bool(facts.get("is_bot")) or bool(getattr(author, "bot", False)):
        return ""
    from operations import (
        format_result,
        get_default_runtime,
        is_remote_control_candidate,
        parse_remote_action,
    )

    if not is_remote_control_candidate(text):
        return None
    context = _context(source, event, facts)
    action = parse_remote_action(text, context)
    if action is None:
        return "操作を認識できません。"
    if control_only and action.action.value not in {
        "status",
        "list_jobs",
        "list_runs",
        "show_run",
        "log_summary",
        "inventory",
        "cancel",
    }:
        return None
    runtime = get_default_runtime()
    loop = asyncio.get_running_loop()
    raw_message = getattr(event, "raw_message", None)

    async def _send_update(result: Any) -> None:
        adapter = None
        try:
            adapter = gateway._adapter_for_source(source)
        except Exception:
            adapter = None
        if adapter is None:
            return
        chat_id = str(facts.get("chat_id") or getattr(source, "chat_id", ""))
        metadata = {
            "thread_id": facts.get("thread_id"),
            "operations": True,
            "source_message_id": facts.get("message_id"),
        }
        await adapter.send(
            chat_id,
            format_result(result),
            reply_to=facts.get("message_id"),
            metadata=metadata,
        )

    def notify(result: Any) -> None:
        def report_failure(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            try:
                task.result()
            except Exception:
                logger.warning(
                    "operations completion notification failed", exc_info=True
                )

        def schedule() -> None:
            task = loop.create_task(_send_update(result))
            task.add_done_callback(report_failure)

        try:
            loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            logger.warning("operations event loop closed before notification")

    result = runtime.handle(action, context, notifier=notify)
    if inspect.isawaitable(result):
        result = await result
    return format_result(cast(Any, result))
