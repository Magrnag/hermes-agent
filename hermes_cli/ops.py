"""Thin local CLI adapter for the Hermes Operations Runtime."""

from __future__ import annotations

from typing import Any

from operations import (
    Action,
    RemoteAction,
    RequestContext,
    format_result,
    get_default_runtime,
)


_COMMAND_ACTIONS = {
    "status": "STATUS",
    "jobs": "LIST_JOBS",
    "runs": "LIST_RUNS",
    "show-run": "SHOW_RUN",
    "run": "RUN",
    "resume": "RESUME",
    "cancel": "CANCEL",
}


def _local_context() -> RequestContext:
    """Build the deliberately narrow, already-authorized local CLI context."""
    values: dict[str, Any] = {
        "platform": "local_cli",
        "source": "local_cli",
        "user_id": "local_cli",
        "channel_id": "local_cli",
        "authorized": True,
        "bot_mentioned": True,
        "is_local": True,
    }
    return RequestContext(**values)


def _action(command: str, target: str | None) -> RemoteAction:
    try:
        action_type = Action[_COMMAND_ACTIONS[command]]
    except KeyError as exc:  # pragma: no cover - parser constrains this table
        raise ValueError(f"Unknown operations command: {command}") from exc
    return RemoteAction(action=action_type, target=target)


def dispatch(command: str, target: str | None = None) -> int:
    """Dispatch one parsed local operation and render its structured result."""
    action = _action(command, target)
    result = get_default_runtime().handle(
        action,
        _local_context(),
        wait=command in {"run", "resume"},
    )
    rendered = format_result(result)
    if rendered:
        print(rendered)
    return 0 if bool(getattr(result, "ok", False)) else 1


def ops_command(args) -> int:
    """Handle ``hermes ops ...`` after argparse has validated its shape."""
    command = str(getattr(args, "ops_command", "") or "")
    target = getattr(args, "target", None)
    return dispatch(command, target)


__all__ = ["dispatch", "ops_command"]
