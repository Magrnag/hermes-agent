"""Fail-closed permission classification for operations actions."""

from __future__ import annotations

from .models import Action, PermissionClass, RequestContext, RemoteAction

# Public policy tables make the categories auditable and keep adapters from inventing policy.
HUMAN_GATE_PHRASES = {
    PermissionClass.HUMAN_GATE: frozenset({
        "approve",
        "approval",
        "human",
        "confirm",
        "publish",
    }),
    PermissionClass.DESTRUCTIVE: frozenset({
        "delete",
        "destroy",
        "drop",
        "wipe",
        "terminate",
        "kill",
        "削除",
        "全消去",
        "破棄",
    }),
    PermissionClass.LIVE_MONEY: frozenset({
        "charge",
        "pay",
        "purchase",
        "refund",
        "transfer",
        "invoice",
        "購入",
        "返金",
        "送金",
    }),
    PermissionClass.CREDENTIAL: frozenset({
        "credential",
        "secret",
        "token",
        "password",
        "api_key",
        "rotate",
        "認証情報",
        "鍵をローテーション",
    }),
    PermissionClass.SOURCE_OF_TRUTH: frozenset({
        "cron",
        "canonical",
        "jobs.json",
        "rewrite",
        "migrate",
        "schedule",
        "cron編集",
        "スケジュール変更",
        "ジョブ定義変更",
    }),
}
DENY_PHRASES = frozenset({
    "sudo",
    "shell",
    "exec",
    "subprocess",
    "arbitrary",
    "eval",
    "rm -rf",
    "秘密を教えて",
    "パスワードを教えて",
    "トークンを表示",
    "任意コマンド",
})
SAFE_WORKSPACE_TARGETS = frozenset({
    "agent-knowledge",
    "uma-research-runtime",
    "fitness-os",
    "hermes-agent",
    "dont-worry-ill-warn-you",
})
SAFE_MANUAL_TARGETS = frozenset({"ai-oss-daily-watch", "github-watch"})


def _text(action: RemoteAction) -> str:
    return " ".join([
        action.action.value,
        action.target or "",
        *(str(v) for v in action.args.values()),
    ]).lower()


def safe_remote_admitted(context: RequestContext) -> bool:
    if not context.remote:
        return True
    allowed = bool((context.model_extra or {}).get("allowed_channel", False))
    return bool(context.authorized and context.bot_mentioned and allowed)


def classify_permission(
    action: RemoteAction, context: RequestContext
) -> PermissionClass:
    text = _text(action)
    if any(phrase in text for phrase in DENY_PHRASES):
        return PermissionClass.DENIED
    for category in (
        PermissionClass.LIVE_MONEY,
        PermissionClass.CREDENTIAL,
        PermissionClass.SOURCE_OF_TRUTH,
        PermissionClass.DESTRUCTIVE,
        PermissionClass.HUMAN_GATE,
    ):
        if any(phrase in text for phrase in HUMAN_GATE_PHRASES[category]):
            return category
    if action.action is Action.UNKNOWN:
        return PermissionClass.DENIED
    if action.action in {
        Action.STATUS,
        Action.LIST_JOBS,
        Action.LIST_RUNS,
        Action.SHOW_RUN,
        Action.LOG_SUMMARY,
        Action.INVENTORY,
    }:
        return PermissionClass.READ_ONLY
    if action.action is Action.RESUME:
        if (
            action.args.get("resumable") is not True
            or action.args.get("safe") is not True
            or not safe_remote_admitted(context)
        ):
            return PermissionClass.DENIED
        return PermissionClass.SAFE_MANUAL
    if action.action is Action.CANCEL:
        if action.args.get("owned") is True:
            return (
                PermissionClass.SAFE_MANUAL
                if safe_remote_admitted(context)
                else PermissionClass.DENIED
            )
    if action.action is Action.RUN:
        target = (action.target or action.args.get("job_id") or "").strip()
        if target not in SAFE_MANUAL_TARGETS and target not in SAFE_WORKSPACE_TARGETS:
            return PermissionClass.DENIED
        if not safe_remote_admitted(context):
            return PermissionClass.DENIED
        return PermissionClass.SAFE_MANUAL
    return PermissionClass.DENIED


def permission_for(action: RemoteAction, context: RequestContext) -> PermissionClass:
    return classify_permission(action, context)
