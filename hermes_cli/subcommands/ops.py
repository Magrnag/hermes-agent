"""``hermes ops`` Operations Runtime command surface."""

from __future__ import annotations

from typing import Callable


def build_ops_parser(subparsers, *, cmd_ops: Callable) -> None:
    """Attach the read-only and safe lifecycle operations commands."""
    ops_parser = subparsers.add_parser(
        "ops",
        help="Inspect and control Hermes operations",
        description="Hermes Operations Runtime",
    )
    ops_subparsers = ops_parser.add_subparsers(dest="ops_command", required=True)

    ops_subparsers.add_parser("status", help="Show operations runtime status")
    ops_subparsers.add_parser("jobs", help="List registered operations jobs")
    ops_subparsers.add_parser("runs", help="List operations runs")

    show_run = ops_subparsers.add_parser("show-run", help="Show one operations run")
    show_run.add_argument("target", metavar="ID", help="Run ID")

    run = ops_subparsers.add_parser("run", help="Start an allowed operations job")
    run.add_argument("target", metavar="JOB", help="Job ID")

    resume = ops_subparsers.add_parser(
        "resume", help="Resume an allowed operations run"
    )
    resume.add_argument("target", metavar="RUN", help="Run ID")

    cancel = ops_subparsers.add_parser(
        "cancel", help="Cancel an allowed operations run"
    )
    cancel.add_argument("target", metavar="RUN", help="Run ID")

    ops_parser.set_defaults(func=cmd_ops)


__all__ = ["build_ops_parser"]
