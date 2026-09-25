"""Bounded, fixed-argv OMP process runner."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable
import logging

_LOG = logging.getLogger(__name__)

_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SECRET_KEY = re.compile(
    r"(?i)(api[_ -]?key|token|password|secret|authorization|cookie|credential)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (["']?(?:api[_ -]?key|token|password|secret|authorization|cookie|credential)["']?)
    (\s*[:=]\s*)
    ("[^"]*"|'[^']*'|[^\s,;}]+)
    """
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_ENV_ALLOWLIST = frozenset({
    "APPDATA",
    "COLORTERM",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "NO_COLOR",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TZ",
    "USERPROFILE",
    "WINDIR",
})
_TERMINAL_STATUSES = {
    "blocked": "blocked",
    "denied": "blocked",
    "done": "final",
    "failed": "failed",
    "failure": "failed",
    "final": "final",
    "quota": "quota",
    "rate_limited": "quota",
    "retry": "retry",
    "success": "final",
    "succeeded": "final",
}
_MAX_OUTPUT = 24_000


def _redact_structure(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _SECRET_KEY.search(str(key))
                else _redact_structure(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_structure(item) for item in value]
    return value


def redact(text: str) -> str:
    raw = str(text)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        raw = json.dumps(_redact_structure(parsed), ensure_ascii=False)
    else:
        raw = _SECRET_ASSIGNMENT.sub(
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", raw
        )
    raw = _BEARER.sub("Bearer [REDACTED]", raw)
    raw = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", raw)
    return raw[:_MAX_OUTPUT]


def classify_output(output: str, returncode: int | None) -> str:
    low = output.lower()
    if any(x in low for x in ("quota", "rate limit", "insufficient_quota")):
        return "quota"
    if any(x in low for x in ("retry", "temporarily unavailable", "timed out")):
        return "retry"
    if any(x in low for x in ("blocked", "policy", "refused", "denied")):
        return "blocked"
    return "final" if returncode == 0 else "failed"


def _extract_assistant_text(parsed: dict) -> str | None:
    """Return the assistant's text from a single ``message_end`` JSONL event, or None if
    this event isn't an assistant message_end. Non-text content blocks are ignored; text
    blocks are concatenated in original list order (never reversed within one event)."""
    if parsed.get("type") != "message_end":
        return None
    message = parsed.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return None


def process_start_fingerprint(pid: int | None) -> str | None:
    if not pid:
        return None
    try:
        import psutil

        return str(psutil.Process(pid).create_time())
    except Exception:
        return None


def _terminate_process_tree(pid: int, fingerprint: str | None) -> bool:
    if not fingerprint or process_start_fingerprint(pid) != fingerprint:
        return False
    try:
        import psutil

        parent = psutil.Process(pid)
        processes = list(reversed(parent.children(recursive=True))) + [parent]
        for process in processes:
            try:
                process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _, alive = psutil.wait_procs(processes, timeout=3)
        for process in alive:
            try:
                process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _, alive = psutil.wait_procs(alive, timeout=3)
        return not alive
    except Exception:
        return False


class OMPRunner:
    def __init__(self, operations_dir: str | Path, workspace_registry: Any):
        self.operations_dir = Path(operations_dir)
        self.workspace_registry = workspace_registry
        self.operations_dir.mkdir(parents=True, exist_ok=True)
        self._owned: dict[str, tuple[subprocess.Popen, str | None]] = {}
        self._guard = threading.Lock()

    def smoke_test_cwd(self) -> Path:
        """Ops-owned, non-repo scratch directory for the OMP connectivity smoke test.

        Never registered as a workspace and never resolved via ``WorkspaceRegistry``, so
        the smoke run structurally cannot read/write a real repository.
        """
        path = self.operations_dir / "smoke"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _envelope(self, run_id: str, prompt: str, target: str) -> Path:
        path = self.operations_dir / "envelopes" / f"{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "target": target,
                    "prompt": redact(str(prompt)[:8000]),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return path

    def run(
        self,
        *,
        run_id: str,
        target: str,
        prompt: str = "",
        resume_session: str | None = None,
        output_callback: Callable[[str], None] | None = None,
        started_callback: Callable[[int, str | None], None] | None = None,
        cwd_override: Path | None = None,
    ) -> dict[str, Any]:
        executable = shutil.which("omp")
        cwd = (
            cwd_override
            if cwd_override is not None
            else self.workspace_registry.resolve(target)
        )
        if not executable:
            return {
                "ok": False,
                "status": "blocked",
                "message": "OMP executable is unavailable",
            }
        if cwd is None:
            return {
                "ok": False,
                "status": "blocked",
                "message": "workspace is not allowlisted",
            }
        if resume_session is not None and not _SESSION.fullmatch(resume_session):
            return {
                "ok": False,
                "status": "denied",
                "message": "invalid resume session",
            }
        envelope = self._envelope(run_id, prompt, target)
        argv = [executable, "--mode=json", "-p"]
        if resume_session:
            argv.extend(["--resume", resume_session])
        argv.append(f"@{envelope}")
        try:
            from tools.environments.local import build_subprocess_env

            scrubbed_env = build_subprocess_env(scrub_secrets=True)
        except (ImportError, OSError, TypeError):
            return {
                "ok": False,
                "status": "blocked",
                "message": "unable to construct a safe OMP environment",
            }
        child_env = {
            key: value
            for key, value in scrubbed_env.items()
            if key.upper() in _ENV_ALLOWLIST
        }
        popen_options: dict[str, Any] = {}
        if os.name == "nt":
            from hermes_cli._subprocess_compat import windows_hide_flags

            popen_options["creationflags"] = windows_hide_flags() | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            popen_options["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                shell=False,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_options,
            )
        except OSError:
            return {"ok": False, "status": "blocked", "message": "unable to start OMP"}
        fingerprint = process_start_fingerprint(proc.pid)
        with self._guard:
            self._owned[run_id] = (proc, fingerprint)
        if started_callback:
            started_callback(proc.pid, fingerprint)
        chunks: list[str] = []
        try:
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                safe = redact(line.rstrip())
                chunks.append(safe)
                del chunks[:-1000]
                if output_callback:
                    output_callback(safe)
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.cancel(run_id)
            proc.wait(timeout=5)
        finally:
            with self._guard:
                self._owned.pop(run_id, None)
        metadata: dict[str, Any] = {}
        protocol_status = None
        final_text: str | None = None
        for line in reversed(chunks):
            try:
                parsed = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(parsed, dict):
                continue
            session = parsed.get("session")
            session_id = parsed.get("session_id") or parsed.get("sessionId")
            if session_id is None and isinstance(session, dict):
                session_id = session.get("id")
            if session_id is not None and metadata.get("session_id") is None:
                metadata["session_id"] = session_id
            if final_text is None:
                candidate_text = _extract_assistant_text(parsed)
                if candidate_text is not None:
                    final_text = candidate_text
            candidate = str(parsed.get("status") or "").lower()
            if candidate in _TERMINAL_STATUSES and protocol_status is None:
                protocol_status = _TERMINAL_STATUSES[candidate]
        output = "\n".join(chunks)[-_MAX_OUTPUT:]
        try:
            log_path = self.operations_dir / "logs" / f"{run_id}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(output, encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(log_path, 0o600)
        except OSError:
            log_path = None
        status = protocol_status or classify_output(output, proc.returncode)
        if status == "final" and proc.returncode != 0:
            status = "failed"
        return {
            "ok": status == "final",
            "status": status,
            "message": "OMP completed" if status == "final" else "OMP did not complete",
            "output": output,
            "log_path": str(log_path) if log_path else None,
            "session_id": metadata.get("session_id"),
            "final_text": final_text,
            "pid": proc.pid,
            "pid_start_fingerprint": fingerprint,
        }

    @staticmethod
    def cancel_process(pid: int, fingerprint: str | None) -> bool:
        """Terminate a persisted ledger-owned OMP tree after identity verification."""
        try:
            import psutil

            proc = psutil.Process(pid)
            command = " ".join(proc.cmdline() or []).lower()
            if "omp" not in command and "omp" not in (proc.name() or "").lower():
                return False
        except Exception:
            return False
        return _terminate_process_tree(pid, fingerprint)

    def cancel(self, run_id: str) -> bool:
        with self._guard:
            owned = self._owned.get(run_id)
        if owned is None:
            return False
        proc, fingerprint = owned
        return _terminate_process_tree(proc.pid, fingerprint)


OMPController = OMPRunner


RunController = OMPRunner
