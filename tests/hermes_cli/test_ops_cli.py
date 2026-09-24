from types import SimpleNamespace

from hermes_cli.main import _build_cli_parser
from hermes_cli.ops import ops_command
from operations import Action, PermissionClass, RunStatus, StructuredResult


def test_ops_parser_and_thin_dispatch_share_business_runtime(monkeypatch, capsys):
    parser, _ = _build_cli_parser()
    parsed = {
        tuple(argv): parser.parse_args(argv)
        for argv in (
            ["ops", "status"],
            ["ops", "jobs"],
            ["ops", "runs"],
            ["ops", "show-run", "r1"],
            ["ops", "run", "github-watch"],
            ["ops", "resume", "r1"],
            ["ops", "cancel", "r1"],
        )
    }
    assert parsed[("ops", "status")].ops_command == "status"
    assert parsed[("ops", "show-run", "r1")].target == "r1"
    assert parsed[("ops", "run", "github-watch")].target == "github-watch"

    captured = {}

    class Runtime:
        def handle(self, action, context, *, wait=False):
            captured["action"] = action
            captured["context"] = context
            captured["wait"] = wait
            return StructuredResult(
                ok=True,
                action=Action.STATUS,
                permission=PermissionClass.READ_ONLY,
                status=RunStatus.SUCCEEDED,
                title="ready",
            )

    monkeypatch.setattr("hermes_cli.ops.get_default_runtime", lambda: Runtime())
    assert ops_command(SimpleNamespace(ops_command="status", target=None)) == 0
    assert captured["action"].action is Action.STATUS
    assert captured["context"].is_local is True
    assert captured["wait"] is False
    assert "ready" in capsys.readouterr().out
    assert ops_command(SimpleNamespace(ops_command="run", target="github-watch")) == 0
    assert captured["wait"] is True
