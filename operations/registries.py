"""Explicit workspace allowlist and read-only shadow inventory of canonical cron jobs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, List, Mapping

from .permissions import SAFE_MANUAL_TARGETS

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_WORKSPACE_NAMES = {
    "agent-knowledge": "agent-knowledge",
    "uma-research-runtime": "uma-research-runtime",
    "fitness-os": "fitness-os",
    "hermes-agent": "hermes-agent",
    "dont-worry-ill-warn-you": "DON'T WORRY.I'LL WARN YOU",
}


class WorkspaceRegistry:
    """Resolve stable IDs to pre-registered repositories; never interpret user text as a path."""

    def __init__(
        self,
        home: str | Path,
        candidates: Mapping[str, str | Path] | None = None,
    ):
        self.home = Path(home).expanduser().resolve()
        self._paths: dict[str, Path] = {}
        for workspace_id, path in (candidates or {}).items():
            if _SAFE_ID.fullmatch(str(workspace_id)):
                self._add(str(workspace_id), Path(path))
        roots = (Path.home(), Path.home() / "OneDrive")
        for workspace_id, dirname in _WORKSPACE_NAMES.items():
            for root in roots:
                for parent in (root / "Desktop", root / "desktop"):
                    candidate = parent / dirname
                    if candidate.is_dir():
                        self._add(workspace_id, candidate)
                        break
                if workspace_id in self._paths:
                    break

    def _add(self, workspace_id: str, path: Path) -> None:
        try:
            resolved = path.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return
        if resolved.is_dir() and (resolved / ".git").exists():
            self._paths[workspace_id] = resolved

    def register(self, workspace_id: str, path: str | Path) -> bool:
        if not _SAFE_ID.fullmatch(workspace_id):
            return False
        self._add(workspace_id, Path(path))
        return workspace_id in self._paths

    def resolve(self, workspace_id: str | None) -> Path | None:
        if not workspace_id or not _SAFE_ID.fullmatch(str(workspace_id)):
            return None
        return self._paths.get(str(workspace_id))

    def list(self) -> dict[str, str]:
        return {key: str(value) for key, value in sorted(self._paths.items())}

    @staticmethod
    def _git_metadata(path: Path) -> tuple[str | None, str | None]:
        branch = None
        remote = None
        try:
            head = (path / ".git" / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref: refs/heads/"):
                branch = head.removeprefix("ref: refs/heads/")
            config = (path / ".git" / "config").read_text(encoding="utf-8")
            match = re.search(
                r'(?ms)^\[remote "origin"\].*?^\s*url\s*=\s*(.+?)\s*$', config
            )
            if match:
                remote = match.group(1)
        except OSError:
            pass
        return remote, branch

    def inventory(self) -> List[dict[str, Any]]:
        rows = []
        for workspace_id, path in sorted(self._paths.items()):
            remote, branch = self._git_metadata(path)
            rows.append({
                "workspace_id": workspace_id,
                "repo_path": str(path),
                "remote": remote,
                "default_branch": branch,
                "allowed_operations": [
                    "start_task",
                    "resume_owned_run",
                    "cancel_owned_run",
                ],
            })
        return rows


class JobRegistry:
    """Read cron definitions without invoking normalizers that may rewrite canonical storage."""

    ALLOWLIST = SAFE_MANUAL_TARGETS

    def __init__(self, home: str | Path, operations_dir: str | Path | None = None):
        self.home = Path(home).expanduser().resolve()
        self.operations_dir = Path(operations_dir or self.home / "operations")
        self.operations_dir.mkdir(parents=True, exist_ok=True)
        self.baseline_path = self.operations_dir / "cron-baseline.json"

    def _load(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(
                (self.home / "cron" / "jobs.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return []
        if isinstance(raw, list):
            jobs = raw
        elif isinstance(raw, dict) and isinstance(raw.get("jobs"), list):
            jobs = raw["jobs"]
        elif isinstance(raw, dict):
            jobs = [
                {"job_id": job_id, **value}
                for job_id, value in raw.items()
                if isinstance(value, dict)
            ]
        else:
            return []
        return [dict(job) for job in jobs if isinstance(job, dict)]

    @staticmethod
    def _job_id(job: Mapping[str, Any]) -> str | None:
        value = job.get("job_id") or job.get("id")
        return str(value) if value else None

    @classmethod
    def _public(cls, job: Mapping[str, Any]) -> dict[str, Any]:
        job_id = cls._job_id(job)
        name = str(job.get("name") or "")
        allow_manual = bool(job_id in cls.ALLOWLIST or name in cls.ALLOWLIST)
        return {
            "job_id": job_id,
            "id": job_id,
            "name": name or None,
            "description": name or None,
            "enabled": job.get("enabled"),
            "state": job.get("state"),
            "schedule": job.get("schedule"),
            "schedule_display": job.get("schedule_display"),
            "provider": job.get("model_provider") or job.get("provider"),
            "model": job.get("model"),
            "next_run_at": job.get("next_run_at"),
            "workdir": job.get("workdir"),
            "source": job.get("source") or job.get("origin"),
            "allow_manual_run": allow_manual,
            "timeout_policy": "canonical_cron",
            "retry_policy": "canonical_cron",
            "concurrency_policy": "coalesce",
            "expected_output": "canonical delivery result",
            "human_gate": False,
        }

    def inventory(self) -> dict[str, Any]:
        jobs = [self._public(job) for job in self._load()]
        current = {job["job_id"]: job for job in jobs if job.get("job_id")}
        try:
            baseline = json.loads(self.baseline_path.read_text(encoding="utf-8"))
            if not isinstance(baseline, dict):
                baseline = {}
        except (OSError, ValueError):
            baseline = {}
        if not baseline:
            try:
                self.baseline_path.write_text(
                    json.dumps(current, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                baseline = current
            except OSError:
                pass
        drift = set(current) ^ set(baseline)
        drift.update(
            key for key in set(current) & set(baseline) if current[key] != baseline[key]
        )
        return {
            "jobs": jobs,
            "allowlisted": sorted(self.ALLOWLIST),
            "drift": sorted(drift),
        }

    def list_jobs(self) -> list[dict[str, Any]]:
        return self.inventory()["jobs"]

    def get(self, target: str | None) -> dict[str, Any] | None:
        if not target or not _SAFE_ID.fullmatch(target):
            return None
        matches = [
            job
            for job in self._load()
            if self._job_id(job) == target or str(job.get("name") or "") == target
        ]
        return matches[0] if len(matches) == 1 else None

    def dispatch(self, target: str) -> dict[str, Any] | None:
        if target not in self.ALLOWLIST:
            return None
        try:
            from cron.jobs import trigger_job, use_cron_store

            with use_cron_store(self.home):
                result = trigger_job(target)
            return self._public(result) if result else None
        except (ImportError, OSError, ValueError):
            return None
