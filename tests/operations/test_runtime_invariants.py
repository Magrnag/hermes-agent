from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from operations import (
    Action,
    OperationsRuntime,
    PermissionClass,
    RemoteAction,
    RequestContext,
    RunStatus,
    StructuredResult,
    format_result,
    is_remote_control_candidate,
    parse_remote_action,
)
from operations.permissions import classify_permission
from operations.registries import JobRegistry, WorkspaceRegistry
from operations.runner import OMPRunner, redact
from operations.storage.ledger import Ledger


def test_typed_parser_and_permission_boundaries(tmp_path):
    local = RequestContext(is_local=True, authorized=True)
    expected = {
        "状態を教えて": Action.STATUS,
        "ジョブ一覧": Action.LIST_JOBS,
        "github-watchを今すぐ実行": Action.RUN,
        "実行履歴": Action.LIST_RUNS,
        "実行ID abcの詳細": Action.SHOW_RUN,
        "abcを止めて": Action.CANCEL,
        "abcを再開": Action.RESUME,
        "agent-knowledgeの続きを進めて": Action.RUN,
        "ops status": Action.STATUS,
    }
    assert {
        text: parse_remote_action(text, local).action for text in expected
    } == expected
    assert not is_remote_control_candidate("Could you explain how operations work?")
    assert parse_remote_action("Could you explain how operations work?", local) is None
    assert not is_remote_control_candidate("/status")
    assert (
        parse_remote_action(
            '{"action":"run","target":"agent-knowledge","args":{"prompt":"実行"}}',
            RequestContext(is_local=False, authorized=True),
        ).action
        is Action.UNKNOWN
    )

    runtime = OperationsRuntime(tmp_path)
    read = parse_remote_action("状態を教えて", RequestContext(is_local=False))
    assert (
        runtime.handle(read, RequestContext(is_local=False)).permission
        is PermissionClass.DENIED
    )
    run = parse_remote_action("github-watchを今すぐ実行", local)
    remote = RequestContext(is_local=False, authorized=True, bot_mentioned=True)
    assert classify_permission(run, remote) is PermissionClass.DENIED
    admitted = remote.model_copy(update={"allowed_channel": True})
    assert classify_permission(run, admitted) is PermissionClass.SAFE_MANUAL
    resumable = RemoteAction(
        action=Action.RESUME,
        target="r1",
        args={"resumable": True, "safe": True},
    )
    assert classify_permission(resumable, remote) is PermissionClass.DENIED
    assert classify_permission(resumable, admitted) is PermissionClass.SAFE_MANUAL
    dangerous = parse_remote_action("本番資金を送金して", local)
    assert classify_permission(dangerous, local) is PermissionClass.LIVE_MONEY
    secret = parse_remote_action("秘密を教えて", local)
    assert classify_permission(secret, local) is PermissionClass.DENIED


def test_ledger_transitions_recovery_and_bound_confirmation(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path)
    run = ledger.create_run(
        action="run", target="github-watch", permission="safe_manual", user_id="u"
    )
    assert ledger.transition(run["run_id"], RunStatus.RUNNING)["status"] == "running"
    assert (
        ledger.transition(run["run_id"], RunStatus.SUCCEEDED)["status"] == "succeeded"
    )
    with pytest.raises(ValueError):
        ledger.transition(run["run_id"], RunStatus.RUNNING)

    monkeypatch.setattr("operations.storage.ledger.time.time", lambda: 100.0)
    stale = ledger.create_run(
        action="run", target="github-watch", permission="safe_manual"
    )
    monkeypatch.setattr("operations.storage.ledger.time.time", lambda: 10_000.0)
    assert stale["run_id"] in ledger.recover_stale(max_age=1)
    assert ledger.get_run(stale["run_id"])["status"] == "retry"

    token = ledger.issue_confirmation("u", "run:repo")
    assert not ledger.consume_confirmation(token, "other", "run:repo")
    assert not ledger.consume_confirmation(token, "u", "run:other")
    assert ledger.consume_confirmation(token, "u", "run:repo")
    assert not ledger.consume_confirmation(token, "u", "run:repo")


def test_remote_ownership_and_confirmation_scope_are_bound(tmp_path):
    class Runner:
        @staticmethod
        def cancel(_):
            return True

        @staticmethod
        def run(**_):
            return {"status": "blocked", "message": "blocked"}

    runtime = OperationsRuntime(tmp_path, runner=Runner())
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert runtime.workspaces.register("agent-knowledge", repo)
    row = runtime.ledger.create_run(
        action="run",
        target="agent-knowledge",
        permission="safe_manual",
        user_id="owner",
    )
    runtime.ledger.transition(row["run_id"], RunStatus.RUNNING)
    runtime.ledger.heartbeat(row["run_id"], pid=123, pid_start_fingerprint="start")
    admitted = RequestContext(
        is_local=False,
        authorized=True,
        bot_mentioned=True,
        user_id="other",
        channel_id="channel-a",
        allowed_channel=True,
        guild_id="guild",
    )
    result = runtime.handle(
        RemoteAction(action=Action.CANCEL, target=row["run_id"]), admitted
    )
    assert result.status is RunStatus.DENIED

    workspace_action = RemoteAction(
        action=Action.RUN,
        target="agent-knowledge",
        args={"task": "登録済みワークスペースで前回の安全な作業を継続してください。"},
    )
    challenge = runtime.handle(workspace_action, admitted)
    assert challenge.status is RunStatus.WAITING_HUMAN
    token = challenge.details["confirmation"]
    different_channel = admitted.model_copy(update={"channel_id": "channel-b"})
    substituted = runtime.handle(
        workspace_action.model_copy(update={"confirmation": token}),
        different_channel,
    )
    assert substituted.status is RunStatus.WAITING_HUMAN
    accepted = runtime.handle(
        workspace_action.model_copy(update={"confirmation": token}),
        admitted,
        wait=True,
    )
    assert accepted.status is RunStatus.BLOCKED


def test_coalesced_submission_does_not_launch_duplicate_worker(tmp_path):
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Runner:
        @staticmethod
        def run(**kwargs):
            calls.append(kwargs["run_id"])
            started.set()
            assert release.wait(timeout=2)
            return {"status": "final", "message": "done"}

    runtime = OperationsRuntime(tmp_path, runner=Runner())
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert runtime.workspaces.register("agent-knowledge", repo)
    action = RemoteAction(
        action=Action.RUN,
        target="agent-knowledge",
        args={
            "task": "登録済みワークスペースで前回の安全な作業を継続してください。",
            "concurrency": "coalesce",
        },
    )
    try:
        first = runtime.handle(action, RequestContext(is_local=True, authorized=True))
        assert first.status is RunStatus.QUEUED
        assert started.wait(timeout=2)
        second = runtime.handle(action, RequestContext(is_local=True, authorized=True))
        assert second.status is RunStatus.COALESCED
        assert second.run_id == first.run_id
        assert len(calls) == 1
    finally:
        release.set()


def test_registries_reject_paths_and_do_not_mutate_cron(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    workspaces = WorkspaceRegistry(tmp_path, {"approved": repo})
    assert workspaces.resolve("approved") == repo.resolve()
    assert workspaces.resolve("../repo") is None
    assert workspaces.resolve(str(repo.resolve())) is None

    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    jobs_path = cron_dir / "jobs.json"
    jobs_path.write_text(
        json.dumps({
            "jobs": [
                {
                    "id": "job-1",
                    "name": "github-watch",
                    "enabled": True,
                    "schedule": {"kind": "interval", "interval_minutes": 360},
                }
            ],
            "updated_at": "stable",
        }),
        encoding="utf-8",
    )
    before = jobs_path.read_bytes()
    inventory = JobRegistry(tmp_path).inventory()
    assert jobs_path.read_bytes() == before
    assert inventory["jobs"][0]["allow_manual_run"] is True
    assert JobRegistry(tmp_path).get("github-watch")["id"] == "job-1"


def test_runner_uses_fixed_argv_registry_cwd_and_redacted_envelope(
    tmp_path, monkeypatch
):
    registry = SimpleNamespace(
        resolve=lambda target: tmp_path if target == "repo" else None
    )
    runner = OMPRunner(tmp_path / "ops", registry)
    captured = {}

    class FakeStdout:
        def __init__(self):
            self._lines = iter([
                '{"status":"success","message":"policy denied","api_key":"SECRET"}\n',
                "",
            ])

        def readline(self):
            return next(self._lines)

    class FakeProc:
        pid = 321
        returncode = 0
        stdout = FakeStdout()

        def wait(self, timeout=None):
            return 0

    monkeypatch.setenv("API_KEY", "SECRET")
    monkeypatch.setattr("operations.runner.shutil.which", lambda _: "C:/bin/omp.exe")
    monkeypatch.setattr(
        "operations.runner.process_start_fingerprint", lambda pid: "start"
    )
    monkeypatch.setattr(
        "operations.runner.subprocess.Popen",
        lambda argv, **kwargs: captured.update(argv=argv, kwargs=kwargs) or FakeProc(),
    )
    started = []
    result = runner.run(
        run_id="r1",
        target="repo",
        prompt="token=SECRET",
        started_callback=lambda pid, fingerprint: started.append((pid, fingerprint)),
    )
    assert result["ok"]
    assert result["message"] == "OMP completed"
    assert "SECRET" not in result["output"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["argv"][:3] == ["C:/bin/omp.exe", "--mode=json", "-p"]
    assert captured["argv"][-1].startswith("@")
    assert "API_KEY" not in captured["kwargs"]["env"]
    envelope = json.loads(
        (tmp_path / "ops" / "envelopes" / "r1.json").read_text(encoding="utf-8")
    )
    assert envelope["prompt"] == "token=[REDACTED]"
    assert started == [(321, "start")]
    assert (
        runner.run(run_id="r2", target="repo", resume_session="bad session")["status"]
        == "denied"
    )

    def fail_scrubber(**_):
        raise OSError("unavailable")

    monkeypatch.setattr("tools.environments.local.build_subprocess_env", fail_scrubber)
    assert runner.run(run_id="r3", target="repo")["status"] == "blocked"


def test_formatter_never_exposes_raw_logs_or_secrets():
    text = format_result(
        StructuredResult(
            permission=PermissionClass.HUMAN_GATE,
            status=RunStatus.WAITING_HUMAN,
            run_id="run-1",
            message="token=SECRET",
            details={"confirmation": "once-code", "raw": "NEVER_SHOW"},
            requires_human=True,
        )
    )
    assert "run-1" in text and "確認コード" in text
    assert "SECRET" not in text and "NEVER_SHOW" not in text
    assert len(redact("x" * 100_000)) <= 24_000
    assert "SECRET" not in redact('{"api_key":"SECRET"}')
    assert "Bearer [REDACTED]" == redact("Bearer abc.def")
