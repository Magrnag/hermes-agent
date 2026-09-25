"""Operations runtime facade: policy, ledger, and asynchronous execution."""

from __future__ import annotations
import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

_LOG = logging.getLogger(__name__)

from .models import (
    Action,
    PermissionClass,
    RequestContext,
    RemoteAction,
    RunStatus,
    StructuredResult,
)
from .permissions import classify_permission, safe_remote_admitted
from .registries import JobRegistry, WorkspaceRegistry
from .runner import OMPRunner, redact
from .storage.ledger import Ledger
from .locks import OperationLocks


class OperationsRuntime:
    def __init__(self, home: str | Path | None = None, runner: Any = None):
        if home is None:
            try:
                from hermes_constants import get_hermes_home

                home = get_hermes_home()
            except Exception:
                home = Path.home() / ".hermes"
        self.home = Path(home).expanduser().resolve()
        self.operations_dir = self.home / "operations"
        self.operations_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(self.operations_dir)
        self.recovered_run_ids = self.ledger.recover_stale()
        self.workspaces = WorkspaceRegistry(self.home)
        self.jobs = JobRegistry(self.home, self.operations_dir)
        self.locks = OperationLocks(self.operations_dir)
        self.runner = (
            runner
            if runner is not None
            else OMPRunner(self.operations_dir, self.workspaces)
        )

    @staticmethod
    def _start_worker(target: Callable[[], Any], *, name: str) -> None:
        from agent.memory_provider import spawn_context_thread

        worker = spawn_context_thread(target, name=name)
        worker.start()

    @staticmethod
    def _notify(
        notifier: Callable[[StructuredResult], None] | None,
        result: StructuredResult,
    ) -> None:
        if notifier is None:
            return
        try:
            notifier(result)
        except Exception:
            _LOG.debug("operations notifier failed", exc_info=True)

    @staticmethod
    def _workspace_prompt(_target: str) -> str:
        return "登録済みワークスペースで前回の安全な作業を継続してください。"

    @staticmethod
    def _confirmation_scope(
        action: RemoteAction, context: RequestContext, task: str
    ) -> str:
        extra = context.model_extra or {}
        canonical = json.dumps(
            {
                "action": action.action.value,
                "target": action.target,
                "task": task,
                "user_id": context.user_id,
                "source": context.source,
                "profile": context.profile,
                "guild_id": extra.get("guild_id"),
                "channel_id": context.channel_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _transition_terminal(
        self,
        run_id: str,
        status: RunStatus,
        *,
        message: str,
        details: dict[str, Any],
        **fields: Any,
    ) -> RunStatus:
        current = self.ledger.get_run(run_id)
        if current and current.get("status") == RunStatus.CANCELLED.value:
            return RunStatus.CANCELLED
        try:
            self.ledger.transition(
                run_id, status, message=message, details=details, **fields
            )
            return status
        except (RuntimeError, ValueError):
            current = self.ledger.get_run(run_id)
            if current and current.get("status") == RunStatus.CANCELLED.value:
                return RunStatus.CANCELLED
            raise

    def _result(
        self,
        action: RemoteAction,
        permission: PermissionClass,
        *,
        ok: bool = False,
        status: RunStatus | None = None,
        title: str = "Operations",
        message: str = "",
        run_id: str | None = None,
        details: dict[str, Any] | None = None,
        requires_human: bool = False,
        resumable: bool = False,
    ) -> StructuredResult:
        return StructuredResult(
            handled=True,
            ok=ok,
            action=action.action,
            permission=permission,
            status=status,
            title=title,
            run_id=run_id,
            message=message,
            details=details or {},
            requires_human=requires_human,
            resumable=resumable,
        )

    def _read_only(
        self, action: RemoteAction, permission: PermissionClass
    ) -> StructuredResult:
        if action.action is Action.STATUS:
            runs = self.ledger.list_runs(limit=200)
            return self._result(
                action,
                permission,
                ok=True,
                status=RunStatus.SUCCEEDED,
                title="Operations status",
                message="Operations runtime is ready",
                details={
                    "running": sum(run["status"] == "running" for run in runs),
                    "runs": len(runs),
                    "workspaces": self.workspaces.inventory(),
                    "recovered": self.recovered_run_ids,
                },
            )
        if action.action in {Action.LIST_JOBS, Action.INVENTORY}:
            inventory = self.jobs.inventory()
            return self._result(
                action,
                permission,
                ok=True,
                status=RunStatus.SUCCEEDED,
                title="Cron inventory",
                message=f"{len(inventory['jobs'])} jobs",
                details=inventory,
            )
        if action.action is Action.LIST_RUNS:
            rows = self.ledger.list_runs(
                limit=int(action.args.get("limit", 50) or 50),
                status=action.args.get("status"),
            )
            return self._result(
                action,
                permission,
                ok=True,
                status=RunStatus.SUCCEEDED,
                title="Operation runs",
                message=f"{len(rows)} runs",
                details={"runs": rows},
            )
        run_id = action.target or action.args.get("run_id")
        if action.action is Action.SHOW_RUN:
            row = self.ledger.get_run(str(run_id) if run_id else None)
            if not row:
                return self._result(
                    action,
                    permission,
                    status=RunStatus.BLOCKED,
                    title="Run not found",
                    message="No matching operation run",
                )
            row.pop("details_json", None)
            return self._result(
                action,
                permission,
                ok=True,
                status=RunStatus(row["status"]),
                title="Operation run",
                run_id=row["run_id"],
                message=redact(row.get("message", "")),
                details=row,
                resumable=bool(row.get("details", {}).get("resumable")),
            )
        if action.action is Action.LOG_SUMMARY:
            row = self.ledger.get_run(str(run_id) if run_id else None)
            if not row:
                return self._result(
                    action,
                    permission,
                    status=RunStatus.BLOCKED,
                    title="Log not found",
                    message="No matching operation run",
                )
            return self._result(
                action,
                permission,
                ok=True,
                status=RunStatus(row["status"]),
                title="Log summary",
                run_id=row["run_id"],
                message=redact(row.get("message", "")),
                details={
                    "status": row["status"],
                    "log_available": bool(row.get("details", {}).get("log_path")),
                },
            )
        return self._result(
            action,
            PermissionClass.DENIED,
            status=RunStatus.DENIED,
            title="Operation denied",
            message="Unsupported read operation",
        )

    def _background(
        self,
        action: RemoteAction,
        context: RequestContext,
        run_id: str,
        notifier: Callable[[StructuredResult], None] | None,
    ) -> StructuredResult:
        target = str(action.target or action.args.get("job_id") or "")
        extra = context.model_extra or {}
        source_details = {
            "thread_id": extra.get("thread_id"),
            "message_id": extra.get("message_id"),
            "session_id": context.session_id,
        }
        result: StructuredResult
        try:
            with self.locks.workspace(target, timeout=None):
                self.ledger.transition(run_id, RunStatus.RUNNING)
                if target in self.jobs.ALLOWLIST:
                    dispatched = self.jobs.dispatch(target)
                    if dispatched is None:
                        mapped = self._transition_terminal(
                            run_id,
                            RunStatus.BLOCKED,
                            message="Cron job dispatch failed",
                            details={**source_details, "job_id": target},
                        )
                        result = self._result(
                            action,
                            PermissionClass.SAFE_MANUAL,
                            status=mapped,
                            title="Job dispatch failed",
                            run_id=run_id,
                            message="Canonical scheduler did not accept the job",
                            details={"job_id": target},
                        )
                    else:
                        mapped = self._transition_terminal(
                            run_id,
                            RunStatus.SUCCEEDED,
                            message="Cron job dispatched",
                            details={**source_details, "job_id": target},
                        )
                        result = self._result(
                            action,
                            PermissionClass.SAFE_MANUAL,
                            ok=mapped is RunStatus.SUCCEEDED,
                            status=mapped,
                            title="Job dispatched",
                            run_id=run_id,
                            message=(
                                "Operation cancelled"
                                if mapped is RunStatus.CANCELLED
                                else "Cron job queued through canonical scheduler"
                            ),
                            details={"job_id": target},
                        )
                else:

                    def started(pid: int, fingerprint: str | None) -> None:
                        self.ledger.heartbeat(
                            run_id, pid=pid, pid_start_fingerprint=fingerprint
                        )

                    result_data = self.runner.run(
                        run_id=run_id,
                        target=target,
                        prompt=self._workspace_prompt(target),
                        started_callback=started,
                    )
                    requested = {
                        "final": RunStatus.SUCCEEDED,
                        "retry": RunStatus.RETRY,
                        "quota": RunStatus.QUOTA,
                        "blocked": RunStatus.BLOCKED,
                    }.get(str(result_data.get("status") or ""), RunStatus.FAILED)
                    details = {
                        **source_details,
                        "workspace": target,
                        "resumable": requested in {RunStatus.RETRY, RunStatus.QUOTA},
                        "omp_session_id": result_data.get("session_id"),
                        "log_path": result_data.get("log_path"),
                    }
                    mapped = self._transition_terminal(
                        run_id,
                        requested,
                        message=(
                            "OMP completed"
                            if requested is RunStatus.SUCCEEDED
                            else "OMP did not complete"
                        ),
                        details=details,
                        omp_session_id=result_data.get("session_id"),
                        pid=result_data.get("pid"),
                        pid_start_fingerprint=result_data.get("pid_start_fingerprint"),
                    )
                    result = self._result(
                        action,
                        PermissionClass.SAFE_MANUAL,
                        ok=mapped is RunStatus.SUCCEEDED,
                        status=mapped,
                        title="Workspace operation",
                        run_id=run_id,
                        message=(
                            "Operation cancelled"
                            if mapped is RunStatus.CANCELLED
                            else (
                                "OMP completed"
                                if mapped is RunStatus.SUCCEEDED
                                else "OMP did not complete"
                            )
                        ),
                        details={"workspace": target},
                        resumable=mapped in {RunStatus.RETRY, RunStatus.QUOTA},
                    )
        except (TimeoutError, OSError, RuntimeError, TypeError, ValueError):
            current = self.ledger.get_run(run_id)
            if current and current.get("status") == RunStatus.CANCELLED.value:
                mapped = RunStatus.CANCELLED
            else:
                mapped = self._transition_terminal(
                    run_id,
                    RunStatus.FAILED,
                    message="Operation failed safely",
                    details=source_details,
                )
            result = self._result(
                action,
                PermissionClass.SAFE_MANUAL,
                status=mapped,
                title=(
                    "Operation cancelled"
                    if mapped is RunStatus.CANCELLED
                    else "Operation failed"
                ),
                run_id=run_id,
                message=(
                    "Operation cancelled"
                    if mapped is RunStatus.CANCELLED
                    else "Operation failed safely"
                ),
            )
        self._notify(notifier, result)
        return result

    def _resume(
        self,
        action: RemoteAction,
        context: RequestContext,
        permission: PermissionClass,
        notifier: Callable[[StructuredResult], None] | None,
        *,
        wait: bool,
    ) -> StructuredResult:
        run_id = action.target or action.args.get("run_id")
        row = self.ledger.get_run(str(run_id) if run_id else None)
        if (
            not row
            or row.get("status")
            not in {
                RunStatus.RETRY.value,
                RunStatus.QUOTA.value,
                RunStatus.BLOCKED.value,
            }
            or not row.get("details", {}).get("resumable")
        ):
            return self._result(
                action,
                PermissionClass.DENIED,
                status=RunStatus.DENIED,
                title="Resume refused",
                message="Dangerous or non-resumable run",
            )
        session_id = row.get("omp_session_id") or row.get("details", {}).get(
            "session_id"
        )
        target = row.get("target") or row.get("details", {}).get("workspace")
        if not session_id or not target:
            return self._result(
                action,
                PermissionClass.DENIED,
                status=RunStatus.DENIED,
                title="Resume refused",
                message="Resume metadata is incomplete",
            )
        new = self.ledger.create_run(
            action=Action.RESUME.value,
            target=str(target),
            permission=permission.value,
            status=RunStatus.QUEUED.value,
            title="Resume operation",
            user_id=context.user_id,
            source=context.source,
            channel_id=context.channel_id,
            correlation_id=context.correlation_id,
            omp_session_id=str(session_id),
            details={
                "resumable": True,
                "workspace": target,
                "thread_id": (context.model_extra or {}).get("thread_id"),
                "message_id": (context.model_extra or {}).get("message_id"),
            },
            concurrency=action.args.get("concurrency", "queue"),
        )
        if new.get("coalesced"):
            return self._result(
                action,
                permission,
                ok=True,
                status=RunStatus.COALESCED,
                title="Resume coalesced",
                run_id=new["run_id"],
                message="An equivalent resume is already queued or running",
                resumable=True,
            )
        new_id = new["run_id"]

        def work() -> StructuredResult:
            result: StructuredResult
            try:
                with self.locks.workspace(str(target), timeout=None):
                    self.ledger.transition(new_id, RunStatus.RUNNING)

                    def started(pid: int, fingerprint: str | None) -> None:
                        self.ledger.heartbeat(
                            new_id, pid=pid, pid_start_fingerprint=fingerprint
                        )

                    runner_result = self.runner.run(
                        run_id=new_id,
                        target=str(target),
                        resume_session=str(session_id),
                        prompt="",
                        started_callback=started,
                    )
                    requested = {
                        "final": RunStatus.SUCCEEDED,
                        "retry": RunStatus.RETRY,
                        "quota": RunStatus.QUOTA,
                        "blocked": RunStatus.BLOCKED,
                        "denied": RunStatus.DENIED,
                    }.get(str(runner_result.get("status") or ""), RunStatus.FAILED)
                    details = {
                        "resumable": requested in {RunStatus.RETRY, RunStatus.QUOTA},
                        "workspace": target,
                        "session_id": runner_result.get("session_id") or session_id,
                    }
                    mapped = self._transition_terminal(
                        new_id,
                        requested,
                        message=(
                            "OMP completed"
                            if requested is RunStatus.SUCCEEDED
                            else "OMP did not complete"
                        ),
                        details=details,
                    )
                    result = self._result(
                        action,
                        permission,
                        ok=mapped is RunStatus.SUCCEEDED,
                        status=mapped,
                        title="Resume complete",
                        run_id=new_id,
                        message=(
                            "Operation cancelled"
                            if mapped is RunStatus.CANCELLED
                            else (
                                "OMP completed"
                                if mapped is RunStatus.SUCCEEDED
                                else "OMP did not complete"
                            )
                        ),
                        resumable=mapped in {RunStatus.RETRY, RunStatus.QUOTA},
                    )
            except Exception:
                current = self.ledger.get_run(new_id)
                if current and current.get("status") == RunStatus.CANCELLED.value:
                    mapped = RunStatus.CANCELLED
                else:
                    mapped = self._transition_terminal(
                        new_id,
                        RunStatus.FAILED,
                        message="Resume failed safely",
                        details={"workspace": target, "resumable": False},
                    )
                result = self._result(
                    action,
                    permission,
                    status=mapped,
                    title=(
                        "Operation cancelled"
                        if mapped is RunStatus.CANCELLED
                        else "Resume failed"
                    ),
                    run_id=new_id,
                    message=(
                        "Operation cancelled"
                        if mapped is RunStatus.CANCELLED
                        else "Resume failed safely"
                    ),
                )
            self._notify(notifier, result)
            return result

        if wait:
            return work()
        self._start_worker(work, name=f"operations-resume-{new_id[:8]}")
        return self._result(
            action,
            permission,
            ok=True,
            status=RunStatus.QUEUED,
            title="Resume queued",
            run_id=new_id,
            message="Resume accepted",
            resumable=True,
        )

    def issue_confirmation(
        self, user_id: str, action: str, *, ttl: float = 120.0
    ) -> str:
        return self.ledger.issue_confirmation(user_id, action, ttl=ttl)

    def consume_confirmation(self, token: str, user_id: str, action: str) -> bool:
        return self.ledger.consume_confirmation(token, user_id, action)

    def handle(
        self,
        action: RemoteAction,
        context: RequestContext,
        notifier: Callable[[StructuredResult], None] | None = None,
        *,
        wait: bool = False,
    ) -> StructuredResult:
        # Bind cancel/resume authorization to a ledger-owned record before classification.
        target_id = action.target or action.args.get("run_id")
        existing = (
            self.ledger.get_run(str(target_id) if target_id else None)
            if action.action in {Action.RESUME, Action.CANCEL}
            else None
        )
        same_owner = bool(
            existing
            and (
                not context.remote
                or (context.user_id and existing.get("user_id") == context.user_id)
            )
        )
        policy_action = action
        if (
            action.action is Action.RESUME
            and same_owner
            and existing
            and existing.get("details", {}).get("resumable") is True
        ):
            policy_action = action.model_copy(
                update={"args": {**action.args, "resumable": True, "safe": True}}
            )
        elif (
            action.action is Action.CANCEL
            and same_owner
            and existing
            and existing.get("pid")
        ):
            policy_action = action.model_copy(
                update={"args": {**action.args, "owned": True}}
            )
        permission = classify_permission(policy_action, context)
        if (
            permission is PermissionClass.READ_ONLY
            and context.remote
            and not (
                context.authorized
                and bool((context.model_extra or {}).get("allowed_guild", False))
            )
        ):
            return self._result(
                action,
                PermissionClass.DENIED,
                status=RunStatus.DENIED,
                title="Operation denied",
                message="Authorization required",
            )
        if permission is PermissionClass.READ_ONLY:
            return self._read_only(action, permission)
        if permission is PermissionClass.DENIED:
            return self._result(
                action,
                permission,
                status=RunStatus.DENIED,
                title="Operation denied",
                message="Operation is not authorized",
            )
        if permission is PermissionClass.SAFE_MANUAL and not safe_remote_admitted(
            context
        ):
            return self._result(
                action,
                PermissionClass.DENIED,
                status=RunStatus.DENIED,
                title="Operation denied",
                message="Explicit mention and allowed channel are required",
            )
        if permission in {
            PermissionClass.HUMAN_GATE,
            PermissionClass.DESTRUCTIVE,
            PermissionClass.LIVE_MONEY,
            PermissionClass.CREDENTIAL,
            PermissionClass.SOURCE_OF_TRUTH,
        }:
            row = self.ledger.create_run(
                action=action.action.value,
                target=action.target,
                permission=permission.value,
                status=RunStatus.WAITING_HUMAN.value,
                title="Human approval required",
                message="This operation will not execute automatically",
                user_id=context.user_id,
                source=context.source,
                channel_id=context.channel_id,
                correlation_id=context.correlation_id,
                details={"requires_human": True},
            )
            return self._result(
                action,
                permission,
                status=RunStatus.WAITING_HUMAN,
                title="Human approval required",
                run_id=row["run_id"],
                message="This operation requires a human and was not executed",
                requires_human=True,
            )
        if action.action is Action.RESUME:
            # Permission is elevated only by an owned, explicitly resumable ledger record.
            return self._resume(action, context, permission, notifier, wait=wait)
        if action.action is Action.CANCEL:
            if not target_id:
                return self._result(
                    action,
                    permission,
                    status=RunStatus.BLOCKED,
                    title="Cancel refused",
                    message="No matching ledger-owned process",
                )
            cancelled = self.runner.cancel(str(target_id))
            if not cancelled and existing and existing.get("pid"):
                cancel_process = getattr(self.runner, "cancel_process", None)
                if callable(cancel_process):
                    cancelled = bool(
                        cancel_process(
                            int(existing["pid"]), existing.get("pid_start_fingerprint")
                        )
                    )
            if not cancelled:
                return self._result(
                    action,
                    permission,
                    status=RunStatus.BLOCKED,
                    title="Cancel refused",
                    message="No matching ledger-owned process",
                )
            try:
                self.ledger.transition(
                    str(target_id),
                    RunStatus.CANCELLED,
                    message="Cancelled by operator",
                )
            except (RuntimeError, ValueError):
                return self._result(
                    action,
                    permission,
                    status=RunStatus.BLOCKED,
                    title="Cancel raced with completion",
                    message="The operation reached a terminal state",
                )
            return self._result(
                action,
                permission,
                ok=True,
                status=RunStatus.CANCELLED,
                title="Operation cancelled",
                run_id=str(target_id),
                message="Operation cancelled",
            )
        target = str(action.target or action.args.get("job_id") or "")
        is_workspace = target in self.workspaces.list()
        workspace_task = self._workspace_prompt(target)
        if is_workspace and str(action.args.get("task") or "") != workspace_task:
            return self._result(
                action,
                PermissionClass.DENIED,
                status=RunStatus.DENIED,
                title="Workspace task denied",
                message="Only the registered workspace operation is accepted",
            )
        if is_workspace and context.remote:
            confirmation_action = self._confirmation_scope(
                action, context, workspace_task
            )
            token = action.confirmation or context.confirmation_token
            if not context.user_id or not self.ledger.consume_confirmation(
                token or "",
                context.user_id,
                confirmation_action,
            ):
                issued = self.ledger.issue_confirmation(
                    context.user_id or "local",
                    confirmation_action,
                )
                return self._result(
                    action,
                    permission,
                    status=RunStatus.WAITING_HUMAN,
                    title="Confirmation required",
                    message="確認コードを付けて同じ操作を再送してください",
                    details={"confirmation": issued},
                    requires_human=True,
                )
        details = {
            "workspace": target,
            "thread_id": (context.model_extra or {}).get("thread_id"),
            "message_id": (context.model_extra or {}).get("message_id"),
            "session_id": context.session_id,
        }
        try:
            row = self.ledger.create_run(
                action=action.action.value,
                target=target,
                permission=permission.value,
                status=RunStatus.QUEUED.value,
                title="Operation queued",
                user_id=context.user_id,
                source=context.source,
                channel_id=context.channel_id,
                correlation_id=context.correlation_id,
                omp_session_id=context.session_id,
                details=details,
                concurrency=action.args.get("concurrency", "queue"),
            )
        except RuntimeError:
            return self._result(
                action,
                permission,
                status=RunStatus.BLOCKED,
                title="Operation rejected",
                message="An equivalent operation is already queued or running",
            )
        if row.get("coalesced"):
            return self._result(
                action,
                permission,
                ok=True,
                status=RunStatus.COALESCED,
                title="Operation coalesced",
                run_id=row["run_id"],
                message="An equivalent operation is already queued or running",
            )
        if wait:
            return self._background(action, context, row["run_id"], notifier)
        self._start_worker(
            lambda: self._background(action, context, row["run_id"], notifier),
            name=f"operations-run-{row['run_id'][:8]}",
        )
        return self._result(
            action,
            permission,
            ok=True,
            status=RunStatus.QUEUED,
            title="Operation queued",
            run_id=row["run_id"],
            message="Operation accepted",
        )


_DEFAULTS: dict[str, OperationsRuntime] = {}
_DEFAULTS_LOCK = threading.Lock()


def get_default_runtime() -> OperationsRuntime:
    try:
        from hermes_constants import get_hermes_home, hermes_home_key

        home = get_hermes_home()
        key = hermes_home_key(home)
    except Exception:
        home = Path.home() / ".hermes"
        key = str(home.resolve()).lower()
    with _DEFAULTS_LOCK:
        runtime = _DEFAULTS.get(key)
        if runtime is None:
            runtime = _DEFAULTS[key] = OperationsRuntime(home)
        return runtime
