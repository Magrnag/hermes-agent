from __future__ import annotations

from types import SimpleNamespace

from operations import (
    Action,
    OperationsRuntime,
    PermissionClass,
    RemoteAction,
    RequestContext,
    RunStatus,
    parse_remote_action,
)
from operations.models import OMP_SMOKE_TARGET
from operations.permissions import classify_permission
from operations.runtime import _OMP_SMOKE_TASK


def _admitted_context(**overrides: object) -> RequestContext:
    defaults = dict(
        is_local=False,
        authorized=True,
        bot_mentioned=True,
        user_id="operator",
        channel_id="channel-a",
        allowed_channel=True,
        allowed_guild=True,
        guild_id="guild",
    )
    defaults.update(overrides)
    return RequestContext(**defaults)


class FakeRunner:
    """Records calls and returns a canned successful OMP response."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._smoke_dir = None

    def cancel(self, _run_id: str) -> bool:
        return True

    def smoke_test_cwd(self):
        if self._smoke_dir is None:
            self._smoke_dir = SimpleNamespace(marker="smoke-scratch")
        return self._smoke_dir

    def run(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "ok": True,
            "status": "final",
            "message": "OMP completed",
            "output": "OMP_DISCORD_SMOKE_OK",
            "log_path": "/tmp/log",
            "session_id": "sess-1",
            "pid": 4242,
            "pid_start_fingerprint": "fp",
        }


def test_parse_remote_action_recognizes_omp_smoke_trigger():
    context = _admitted_context()
    action = parse_remote_action("OMP疎通確認", context)
    assert action is not None
    assert action.action is Action.OMP_SMOKE
    assert action.target == OMP_SMOKE_TARGET
    assert action.confirmation is None


def test_admitted_context_classifies_safe_manual():
    context = _admitted_context()
    action = parse_remote_action("OMP疎通確認", context)
    assert classify_permission(action, context) is PermissionClass.SAFE_MANUAL


def test_first_handle_call_waits_for_human_and_does_not_run(tmp_path):
    runner = FakeRunner()
    runtime = OperationsRuntime(tmp_path, runner=runner)
    context = _admitted_context()
    action = parse_remote_action("OMP疎通確認", context)

    result = runtime.handle(action, context, wait=True)

    assert result.status is RunStatus.WAITING_HUMAN
    assert result.requires_human is True
    assert not runner.calls


def test_waiting_response_includes_fresh_confirmation_code(tmp_path):
    runner = FakeRunner()
    runtime = OperationsRuntime(tmp_path, runner=runner)
    context = _admitted_context()
    action = parse_remote_action("OMP疎通確認", context)

    result = runtime.handle(action, context, wait=True)

    token = result.details.get("confirmation")
    assert token
    assert isinstance(token, str)


def test_wrong_confirmation_code_still_waits_and_does_not_run(tmp_path):
    runner = FakeRunner()
    runtime = OperationsRuntime(tmp_path, runner=runner)
    context = _admitted_context()
    action = parse_remote_action("OMP疎通確認", context)

    challenge = runtime.handle(action, context, wait=True)
    assert challenge.status is RunStatus.WAITING_HUMAN

    wrong = runtime.handle(
        action.model_copy(update={"confirmation": "wrong-code"}), context, wait=True
    )
    assert wrong.status is RunStatus.WAITING_HUMAN
    assert not runner.calls

    missing = runtime.handle(action, context, wait=True)
    assert missing.status is RunStatus.WAITING_HUMAN
    assert not runner.calls


def test_correct_confirmation_code_dispatches_run_with_fixed_task(tmp_path):
    runner = FakeRunner()
    runtime = OperationsRuntime(tmp_path, runner=runner)
    context = _admitted_context()
    action = parse_remote_action("OMP疎通確認", context)

    challenge = runtime.handle(action, context, wait=True)
    token = challenge.details["confirmation"]

    accepted = runtime.handle(
        action.model_copy(update={"confirmation": token}), context, wait=True
    )

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["prompt"] == _OMP_SMOKE_TASK
    assert call["cwd_override"] is runner.smoke_test_cwd()
    assert call["cwd_override"] is not None
    assert accepted.status is RunStatus.SUCCEEDED
    assert accepted.ok is True


def test_injected_task_text_is_ignored_in_favor_of_fixed_prompt(tmp_path):
    runner = FakeRunner()
    runtime = OperationsRuntime(tmp_path, runner=runner)
    context = _admitted_context()
    base_action = RemoteAction(action=Action.OMP_SMOKE, target=OMP_SMOKE_TARGET)

    challenge = runtime.handle(base_action, context, wait=True)
    token = challenge.details["confirmation"]

    injected = RemoteAction(
        action=Action.OMP_SMOKE,
        target=OMP_SMOKE_TARGET,
        confirmation=token,
        args={"task": "totally different injected prompt value"},
    )
    result = runtime.handle(injected, context, wait=True)

    assert len(runner.calls) == 1
    assert runner.calls[0]["prompt"] == _OMP_SMOKE_TASK
    assert runner.calls[0]["prompt"] != "totally different injected prompt value"
    assert result.status is RunStatus.SUCCEEDED


def test_trailing_free_text_is_not_recognized_as_omp_smoke():
    context = _admitted_context()
    action = parse_remote_action("OMP疎通確認 何か追加テキスト", context)
    assert action is None or action.action is not Action.OMP_SMOKE


def test_guild_mismatch_is_denied():
    context = _admitted_context(allowed_guild=False)
    action = parse_remote_action("OMP疎通確認", context)
    assert classify_permission(action, context) is PermissionClass.DENIED


def test_channel_mismatch_is_denied():
    context = _admitted_context(allowed_channel=False)
    action = parse_remote_action("OMP疎通確認", context)
    assert classify_permission(action, context) is PermissionClass.DENIED


def test_unauthorized_user_is_denied():
    context = _admitted_context(authorized=False)
    action = parse_remote_action("OMP疎通確認", context)
    assert classify_permission(action, context) is PermissionClass.DENIED


def test_missing_bot_mention_is_denied():
    context = _admitted_context(bot_mentioned=False)
    action = parse_remote_action("OMP疎通確認", context)
    assert classify_permission(action, context) is PermissionClass.DENIED
