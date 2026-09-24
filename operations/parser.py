"""Conservative, typed parsing for the Operations Runtime."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, cast

from pydantic import ValidationError

from .models import Action, RemoteAction, RequestContext

_CANDIDATE = re.compile(
    r"^(?:(?:/(?:ops?|operation)|ops?|operation)\b|"
    r"remote\s+(?:control|run|status)\b|"
    r"(?:run|resume|cancel|show|list|status|inventory|logs?|jobs?|runs?)\b|"
    r"状態を教えて$|ジョブ一覧$|実行履歴$|"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}を今すぐ実行$|"
    r"実行ID\s+[A-Za-z0-9_.:-]+の詳細$|"
    r"[A-Za-z0-9_.:-]+を(?:止めて|再開)$|"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}の続きを進めて$|"
    r".*(?:秘密|パスワード|認証情報|削除|全消去|送金))",
    re.IGNORECASE,
)
_ENGLISH_ACTIONS = {
    "status": Action.STATUS,
    "jobs": Action.LIST_JOBS,
    "job": Action.LIST_JOBS,
    "runs": Action.LIST_RUNS,
    "run": Action.RUN,
    "show": Action.SHOW_RUN,
    "show-run": Action.SHOW_RUN,
    "logs": Action.LOG_SUMMARY,
    "log": Action.LOG_SUMMARY,
    "resume": Action.RESUME,
    "cancel": Action.CANCEL,
    "inventory": Action.INVENTORY,
}
_CONFIRMATION_SUFFIX = re.compile(r"\s+確認コード\s+([A-Za-z0-9_-]{8,128})$")
Fallback = RemoteAction | Callable[[str], RemoteAction | dict[str, Any] | str | None]


def is_remote_control_candidate(text: str) -> bool:
    return isinstance(text, str) and bool(_CANDIDATE.search(text.strip()))


def _validated_fallback(text: str, fallback: Fallback | None) -> RemoteAction:
    if callable(fallback):
        classifier = cast(
            Callable[[str], RemoteAction | dict[str, Any] | str | None],
            fallback,
        )
        try:
            value = classifier(text)
            if isinstance(value, str):
                value = json.loads(value)
            return (
                value
                if isinstance(value, RemoteAction)
                else RemoteAction.model_validate(value)
            )
        except (TypeError, ValueError, ValidationError):
            pass
    elif isinstance(fallback, RemoteAction):
        return fallback
    return RemoteAction(action=Action.UNKNOWN, args={"text": text[:1000]})


def _action(
    kind: Action,
    *,
    raw: str,
    context: RequestContext | None,
    target: str | None = None,
    confirmation: str | None = None,
    **args: Any,
) -> RemoteAction:
    return RemoteAction(
        action=kind,
        target=target,
        confirmation=confirmation,
        args={"text": raw[:1000], **args},
        correlation_id=context.correlation_id if context else None,
    )


def parse_remote_action(
    text: str,
    context: RequestContext | None = None,
    fallback: Fallback | None = None,
) -> RemoteAction | None:
    """Parse deterministic Japanese/English operations text; ordinary chat is untouched."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.startswith("{"):
        # Transport callers never get a JSON escape hatch around the deterministic grammar.
        # A fallback is an explicitly injected, schema-validated classifier.
        return _validated_fallback(raw, fallback)
    if not is_remote_control_candidate(raw):
        return None

    if raw == "状態を教えて":
        return _action(Action.STATUS, raw=raw, context=context)
    if raw == "ジョブ一覧":
        return _action(Action.LIST_JOBS, raw=raw, context=context)
    if raw == "実行履歴":
        return _action(Action.LIST_RUNS, raw=raw, context=context)

    confirmation = None
    confirmation_match = _CONFIRMATION_SUFFIX.search(raw)
    command = raw
    if confirmation_match:
        confirmation = confirmation_match.group(1)
        command = raw[: confirmation_match.start()]

    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,80})を今すぐ実行", command)
    if match:
        target = match.group(1)
        return _action(
            Action.RUN,
            raw=raw,
            context=context,
            target=target,
            confirmation=confirmation,
            job_id=target,
        )
    match = re.fullmatch(r"実行ID\s+([A-Za-z0-9_.:-]+)の詳細", command)
    if match:
        return _action(
            Action.SHOW_RUN,
            raw=raw,
            context=context,
            target=match.group(1),
            run_id=match.group(1),
        )
    match = re.fullmatch(r"([A-Za-z0-9_.:-]+)を止めて", command)
    if match:
        return _action(
            Action.CANCEL,
            raw=raw,
            context=context,
            target=match.group(1),
            run_id=match.group(1),
        )
    match = re.fullmatch(r"([A-Za-z0-9_.:-]+)を再開", command)
    if match:
        return _action(
            Action.RESUME,
            raw=raw,
            context=context,
            target=match.group(1),
            run_id=match.group(1),
        )
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,80})の続きを進めて", command)
    if match:
        target = match.group(1)
        return _action(
            Action.RUN,
            raw=raw,
            context=context,
            target=target,
            confirmation=confirmation,
            workspace=target,
            task="登録済みワークスペースで前回の安全な作業を継続してください。",
        )

    normalized = re.sub(r"^(?:/)?(?:ops?|operation)\s+", "", raw, flags=re.IGNORECASE)
    tokens = normalized.split()
    if not tokens:
        return _validated_fallback(raw, fallback)
    verb = tokens[0].lower()
    if verb == "show" and len(tokens) > 1 and tokens[1].lower() == "run":
        verb = "show-run"
        tokens.pop(1)
    kind = _ENGLISH_ACTIONS.get(verb)
    if kind is None:
        return _validated_fallback(raw, fallback)
    target = tokens[1] if len(tokens) > 1 else None
    return _action(kind, raw=raw, context=context, target=target, target_id=target)
