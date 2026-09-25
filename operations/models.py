"""Typed models exchanged by the operations runtime and transport adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Action(StrEnum):
    UNKNOWN = "unknown"
    STATUS = "status"
    LIST_JOBS = "list_jobs"
    LIST_RUNS = "list_runs"
    SHOW_RUN = "show_run"
    LOG_SUMMARY = "log_summary"
    RUN = "run"
    RESUME = "resume"
    CANCEL = "cancel"
    INVENTORY = "inventory"
    OMP_SMOKE = "omp_smoke"


# Fixed, ops-owned target name for the OMP connectivity smoke test. Never registered as a
# workspace and never a cron job id.
OMP_SMOKE_TARGET = "omp-discord-smoke"


class PermissionClass(StrEnum):
    READ_ONLY = "read_only"
    SAFE_MANUAL = "safe_manual"
    HUMAN_GATE = "human_gate"
    DESTRUCTIVE = "destructive"
    LIVE_MONEY = "live_money"
    CREDENTIAL = "credential"
    SOURCE_OF_TRUTH = "source_of_truth"
    DENIED = "denied"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY = "retry"
    QUOTA = "quota"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    WAITING_HUMAN = "waiting_human"
    DENIED = "denied"
    COALESCED = "coalesced"


class ConcurrencyPolicy(StrEnum):
    QUEUE = "queue"
    COALESCE = "coalesce"
    REJECT = "reject"


class RemoteAction(BaseModel):
    model_config = ConfigDict(extra="allow", use_enum_values=False)

    action: Action
    target: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    confirmation: str | None = None
    correlation_id: str | None = None

    @field_validator("target", "confirmation", "correlation_id", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> Any:
        if value is None:
            return None
        value = str(value).strip()
        return value or None


class RequestContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: str | None = None
    source: str = "local"
    channel_id: str | None = None
    authorized: bool = False
    bot_mentioned: bool = False
    is_local: bool = True
    correlation_id: str | None = None
    session_id: str | None = None
    confirmation_token: str | None = None
    profile: str | None = None
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def remote(self) -> bool:
        return not self.is_local


class StructuredResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    handled: bool = True
    ok: bool = False
    action: Action | None = None
    permission: PermissionClass = PermissionClass.DENIED
    status: RunStatus | None = None
    title: str = "Operations"
    run_id: str | None = None
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    requires_human: bool = False
    resumable: bool = False

    @classmethod
    def unhandled(cls, message: str = "") -> "StructuredResult":
        return cls(
            handled=False, ok=False, permission=PermissionClass.DENIED, message=message
        )
