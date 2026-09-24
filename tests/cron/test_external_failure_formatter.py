from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cron.scheduler as scheduler


def test_external_formatter_receives_bounded_structured_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = tmp_path / "agent-knowledge" / "scripts" / "hermes_discord_notification.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test seam\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(
            returncode=0,
            stdout="【❌ 失敗】GitHub Watch\n・原因: 外部サービスがタイムアウト\n・状態: 自動再試行なし\n・run: run-1\n",
            stderr="",
        )

    monkeypatch.setattr(scheduler, "_external_failure_formatter_script", lambda: script)
    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    job = {
        "id": "github-watch",
        "name": "GitHub Watch",
        "workdir": str(tmp_path / "work"),
        "execution_id": "run-1",
    }

    message = scheduler._format_failure_delivery(
        job,
        "TimeoutError: Traceback token=must-stay-local " + "stderr" * 5000,
    )

    assert message.startswith("【❌ 失敗】GitHub Watch")
    assert len(calls) == 1
    payload = json.loads(str(calls[0]["input"]))
    assert payload["status"] == "FAILURE"
    assert payload["job_id"] == "github-watch"
    assert payload["error_class"] == "TimeoutError"
    assert payload["attempt"] == payload["max_attempts"] == 1
    assert payload["run_id"] == "run-1"
    assert len(payload["error_summary"]) <= 64_000
    assert "must-stay-local" not in message


def test_external_formatter_failure_uses_japanese_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = tmp_path / "agent-knowledge" / "scripts" / "hermes_discord_notification.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test seam\n", encoding="utf-8")
    monkeypatch.setattr(scheduler, "_external_failure_formatter_script", lambda: script)

    def broken_run(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("Traceback secret=must-not-leak")

    monkeypatch.setattr(scheduler.subprocess, "run", broken_run)
    message = scheduler._format_failure_delivery(
        {"id": "github-watch", "name": "GitHub Watch", "workdir": str(tmp_path)},
        "raw stderr",
    )

    assert message == (
        "【❌ 失敗】GitHub Watch\n"
        "・通知生成に失敗\n"
        "・ジョブ: github-watch\n"
        "・状態: 要確認"
    )
    assert "must-not-leak" not in message
