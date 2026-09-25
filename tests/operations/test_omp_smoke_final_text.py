"""Tests for OMP assistant final_text extraction and the semantic success gate.

Covers three layers:
- low-level JSONL extraction in ``operations.runner.OMPRunner.run``
- the semantic success gate + safe Discord message in
  ``operations.runtime.OperationsRuntime._background``
- the formatter's rendering of a semantically-validated success
"""

from __future__ import annotations

from types import SimpleNamespace

from operations import (
    Action,
    OperationsRuntime,
    RemoteAction,
    RequestContext,
    RunStatus,
    StructuredResult,
    format_result,
)
from operations.models import OMP_SMOKE_TARGET
from operations.runner import OMPRunner


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


def _make_runner(tmp_path, lines: list[str]) -> OMPRunner:
    registry = SimpleNamespace(
        resolve=lambda target: tmp_path if target == "<id>" else None
    )
    return OMPRunner(tmp_path / "ops", registry)


def _run_with_stdout(tmp_path, monkeypatch, lines: list[str]) -> dict[str, object]:
    """Run ``OMPRunner.run`` against a fake OMP process whose stdout is ``lines``."""

    runner = _make_runner(tmp_path, lines)

    class FakeStdout:
        def __init__(self):
            self._lines = iter([*lines, ""])

        def readline(self):
            return next(self._lines)

    class FakeProc:
        pid = 999
        returncode = 0
        stdout = FakeStdout()

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("operations.runner.shutil.which", lambda _: "C:/bin/omp.exe")
    monkeypatch.setattr(
        "operations.runner.process_start_fingerprint", lambda pid: "start"
    )
    monkeypatch.setattr(
        "operations.runner.subprocess.Popen",
        lambda argv, **kwargs: FakeProc(),
    )
    return runner.run(run_id="r1", target="<id>", prompt="hello")


# --- Low-level extraction tests (operations/runner.py::OMPRunner.run) ---


def test_single_assistant_message_end_extracts_text(tmp_path, monkeypatch):
    lines = [
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"text","text":"OMP_DISCORD_SMOKE_OK"}]}}'
    ]
    result = _run_with_stdout(tmp_path, monkeypatch, lines)
    assert result["final_text"] == "OMP_DISCORD_SMOKE_OK"


def test_user_role_message_end_is_never_selected(tmp_path, monkeypatch):
    lines = [
        '{"type":"message_end","message":{"role":"user","content":'
        '[{"type":"text","text":"OMP_DISCORD_SMOKE_OK"}]}}',
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"text","text":"something else"}]}}',
    ]
    result = _run_with_stdout(tmp_path, monkeypatch, lines)
    assert result["final_text"] == "something else"


def test_extraction_preserves_exact_text_without_normalization(tmp_path, monkeypatch):
    lines = [
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"text","text":"OMP_DISCORD_SMOKE_OK\u3067\u3059"}]}}'
    ]
    result = _run_with_stdout(tmp_path, monkeypatch, lines)
    assert result["final_text"] == "OMP_DISCORD_SMOKE_OK\u3067\u3059"


def test_no_message_end_event_yields_none_final_text(tmp_path, monkeypatch):
    lines = ['{"status": "success"}']
    result = _run_with_stdout(tmp_path, monkeypatch, lines)
    assert result["final_text"] is None


def test_malformed_lines_interleaved_do_not_raise(tmp_path, monkeypatch):
    lines = [
        "not json at all",
        "{broken",
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"text","text":"OMP_DISCORD_SMOKE_OK"}]}}',
    ]
    result = _run_with_stdout(tmp_path, monkeypatch, lines)
    assert result["final_text"] == "OMP_DISCORD_SMOKE_OK"


def test_last_of_two_assistant_message_ends_wins(tmp_path, monkeypatch):
    lines = [
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"text","text":"first"}]}}',
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"text","text":"second"}]}}',
    ]
    result = _run_with_stdout(tmp_path, monkeypatch, lines)
    assert result["final_text"] == "second"


def test_non_text_content_blocks_are_ignored(tmp_path, monkeypatch):
    lines = [
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"tool_use","id":"x"},{"type":"text","text":"OMP_DISCORD_SMOKE_OK"}]}}'
    ]
    result = _run_with_stdout(tmp_path, monkeypatch, lines)
    assert result["final_text"] == "OMP_DISCORD_SMOKE_OK"


# --- Runtime-level semantic-gate tests ---


class _ConfigurableFakeRunner:
    """A fake OMP runner whose ``run()`` response is configurable per test."""

    def __init__(self, response: dict[str, object]) -> None:
        self._response = response
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
        return dict(self._response)


def _confirmed_smoke_result(tmp_path, runner) -> StructuredResult:
    runtime = OperationsRuntime(tmp_path, runner=runner)
    context = _admitted_context()
    base_action = RemoteAction(action=Action.OMP_SMOKE, target=OMP_SMOKE_TARGET)

    challenge = runtime.handle(base_action, context, wait=True)
    token = challenge.details["confirmation"]

    accepted = RemoteAction(
        action=Action.OMP_SMOKE,
        target=OMP_SMOKE_TARGET,
        confirmation=token,
    )
    return runtime.handle(accepted, context, wait=True)


def test_semantic_ok_final_text_yields_success_and_clean_message(tmp_path):
    runner = _ConfigurableFakeRunner(
        {
            "ok": True,
            "status": "final",
            "message": "OMP completed",
            "output": '{"type":"message_end","raw":"jsonl"}',
            "log_path": "/tmp/log",
            "session_id": "sess-1",
            "final_text": "OMP_DISCORD_SMOKE_OK",
            "pid": 1,
            "pid_start_fingerprint": "fp",
        }
    )
    result = _confirmed_smoke_result(tmp_path, runner)
    assert result.status is RunStatus.SUCCEEDED
    assert result.ok is True
    assert result.message == "OMP_DISCORD_SMOKE_OK"


def test_missing_final_text_downgrades_success_to_failed(tmp_path):
    runner = _ConfigurableFakeRunner(
        {
            "ok": True,
            "status": "final",
            "message": "OMP completed",
            "output": '{"type":"message_end","raw":"jsonl-leak"}',
            "log_path": "/tmp/log",
            "session_id": "sess-1",
            "final_text": None,
            "pid": 1,
            "pid_start_fingerprint": "fp",
        }
    )
    result = _confirmed_smoke_result(tmp_path, runner)
    assert result.status is RunStatus.FAILED
    assert result.message == "OMP smoke test did not complete"
    assert result.message is not None


def test_mismatched_final_text_downgrades_success_to_failed(tmp_path):
    runner = _ConfigurableFakeRunner(
        {
            "ok": True,
            "status": "final",
            "message": "OMP completed",
            "output": '{"type":"message_end","raw":"jsonl-leak"}',
            "log_path": "/tmp/log",
            "session_id": "sess-1",
            "final_text": "something else entirely",
            "pid": 1,
            "pid_start_fingerprint": "fp",
        }
    )
    result = _confirmed_smoke_result(tmp_path, runner)
    assert result.status is RunStatus.FAILED
    assert result.message == "OMP smoke test did not complete"


def test_trailing_characters_fail_exact_match(tmp_path):
    runner = _ConfigurableFakeRunner(
        {
            "ok": True,
            "status": "final",
            "message": "OMP completed",
            "output": '{"type":"message_end","raw":"jsonl-leak"}',
            "log_path": "/tmp/log",
            "session_id": "sess-1",
            "final_text": "OMP_DISCORD_SMOKE_OK\u3067\u3059",
            "pid": 1,
            "pid_start_fingerprint": "fp",
        }
    )
    result = _confirmed_smoke_result(tmp_path, runner)
    assert result.status is RunStatus.FAILED
    assert result.message == "OMP smoke test did not complete"


def test_failure_cases_never_leak_raw_jsonl_output(tmp_path):
    cases = [
        {
            "ok": True,
            "status": "final",
            "output": '{"type":"message_end","message":{"role":"assistant"}}',
            "final_text": None,
        },
        {
            "ok": True,
            "status": "final",
            "output": '{"type":"message_end","message":{"role":"assistant"}}',
            "final_text": "wrong value",
        },
    ]
    for index, base in enumerate(cases):
        response = {
            **base,
            "message": "OMP completed",
            "log_path": "/tmp/log",
            "session_id": "sess-1",
            "pid": 1,
            "pid_start_fingerprint": "fp",
        }
        case_dir = tmp_path / f"case{index}"
        case_dir.mkdir()
        runner = _ConfigurableFakeRunner(response)
        result = _confirmed_smoke_result(case_dir, runner)
        assert result.status is RunStatus.FAILED
        raw_output = str(response["output"])
        assert result.details == {"workspace": OMP_SMOKE_TARGET}
        for leaked in ("{", '"type"', "message_end", raw_output):
            assert leaked not in result.message


def test_details_never_leak_session_id_or_log_path_on_success(tmp_path):
    runner = _ConfigurableFakeRunner(
        {
            "ok": True,
            "status": "final",
            "message": "OMP completed",
            "output": "OMP_DISCORD_SMOKE_OK",
            "log_path": "/tmp/very/secret/log/path",
            "session_id": "sess-secret",
            "final_text": "OMP_DISCORD_SMOKE_OK",
            "pid": 1,
            "pid_start_fingerprint": "fp",
        }
    )
    result = _confirmed_smoke_result(tmp_path, runner)
    assert result.details == {"workspace": OMP_SMOKE_TARGET}


def test_details_never_leak_session_id_or_log_path_on_failure(tmp_path):
    runner = _ConfigurableFakeRunner(
        {
            "ok": True,
            "status": "final",
            "message": "OMP completed",
            "output": "not the expected string",
            "log_path": "/tmp/very/secret/log/path",
            "session_id": "sess-secret",
            "final_text": "not the expected string",
            "pid": 1,
            "pid_start_fingerprint": "fp",
        }
    )
    result = _confirmed_smoke_result(tmp_path, runner)
    assert result.details == {"workspace": OMP_SMOKE_TARGET}


# --- Formatter-level test ---


def test_format_result_renders_semantic_success_without_raw_fields():
    result = StructuredResult(
        ok=True,
        status=RunStatus.SUCCEEDED,
        title="OMP smoke test",
        run_id="run-123",
        message="OMP_DISCORD_SMOKE_OK",
        details={"workspace": OMP_SMOKE_TARGET},
    )
    text = format_result(result)
    assert text == (
        "成功: OMP smoke test\n"
        "OMP_DISCORD_SMOKE_OK\n"
        "実行ID: run-123\n"
        "状態: 完了\n"
        f"workspace: {OMP_SMOKE_TARGET}"
    )
    for leaked in ("{", '"type"', "session", "pid"):
        assert leaked not in text
