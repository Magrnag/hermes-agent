"""Durable operations run ledger and one-time confirmations."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from ..models import ConcurrencyPolicy, RunStatus

_TRANSITIONS: dict[str, set[str]] = {
    RunStatus.QUEUED.value: {
        RunStatus.RUNNING.value,
        RunStatus.COALESCED.value,
        RunStatus.DENIED.value,
        RunStatus.WAITING_HUMAN.value,
        RunStatus.CANCELLED.value,
    },
    RunStatus.RUNNING.value: {
        RunStatus.SUCCEEDED.value,
        RunStatus.FAILED.value,
        RunStatus.RETRY.value,
        RunStatus.QUOTA.value,
        RunStatus.BLOCKED.value,
        RunStatus.CANCELLED.value,
    },
    RunStatus.RETRY.value: {
        RunStatus.QUEUED.value,
        RunStatus.RUNNING.value,
        RunStatus.FAILED.value,
        RunStatus.QUOTA.value,
        RunStatus.BLOCKED.value,
    },
    RunStatus.QUOTA.value: {
        RunStatus.QUEUED.value,
        RunStatus.RETRY.value,
        RunStatus.FAILED.value,
    },
    RunStatus.BLOCKED.value: {
        RunStatus.QUEUED.value,
        RunStatus.WAITING_HUMAN.value,
        RunStatus.FAILED.value,
    },
    RunStatus.WAITING_HUMAN.value: {
        RunStatus.QUEUED.value,
        RunStatus.DENIED.value,
        RunStatus.CANCELLED.value,
    },
    RunStatus.SUCCEEDED.value: set(),
    RunStatus.FAILED.value: set(),
    RunStatus.CANCELLED.value: set(),
    RunStatus.DENIED.value: set(),
    RunStatus.COALESCED.value: set(),
}


class Ledger:
    def __init__(self, home: str | Path):
        self.root = Path(home).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "runtime.db"
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                  run_id TEXT PRIMARY KEY, action TEXT NOT NULL, target TEXT,
                  permission TEXT NOT NULL, status TEXT NOT NULL, title TEXT NOT NULL,
                  message TEXT NOT NULL DEFAULT '', details_json TEXT NOT NULL DEFAULT '{}',
                  user_id TEXT, source TEXT, channel_id TEXT, correlation_id TEXT,
                  omp_session_id TEXT, pid INTEGER, pid_start_fingerprint TEXT,
                  heartbeat REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  concurrency TEXT NOT NULL DEFAULT 'queue', confirmation_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS runs_status_idx ON runs(status, updated_at);
                CREATE INDEX IF NOT EXISTS runs_dedupe_idx ON runs(action, target, status, user_id);
                CREATE TABLE IF NOT EXISTS confirmations (
                  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, action TEXT NOT NULL,
                  expires_at REAL NOT NULL, used_at REAL
                );
            """)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        details_raw = result.pop("details_json", "{}")
        try:
            result["details"] = json.loads(str(details_raw or "{}"))
        except ValueError:
            result["details"] = {}
        return result

    def create_run(
        self,
        *,
        action: str,
        target: str | None,
        permission: str,
        status: str = RunStatus.QUEUED.value,
        title: str = "Operations",
        message: str = "",
        details: dict[str, Any] | None = None,
        user_id: str | None = None,
        source: str | None = None,
        channel_id: str | None = None,
        correlation_id: str | None = None,
        omp_session_id: str | None = None,
        pid: int | None = None,
        pid_start_fingerprint: str | None = None,
        concurrency: ConcurrencyPolicy | str = ConcurrencyPolicy.QUEUE,
        confirmation_hash: str | None = None,
        dedupe: bool = True,
    ) -> dict[str, Any]:
        now = time.time()
        run_id = uuid.uuid4().hex
        policy = ConcurrencyPolicy(concurrency).value
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if dedupe and policy in {
                    ConcurrencyPolicy.COALESCE.value,
                    ConcurrencyPolicy.REJECT.value,
                }:
                    old = db.execute(
                        "SELECT * FROM runs WHERE action=? AND target IS ? AND user_id IS ? AND status IN ('queued','running') ORDER BY created_at LIMIT 1",
                        (action, target, user_id),
                    ).fetchone()
                    if old is not None:
                        if policy == ConcurrencyPolicy.REJECT.value:
                            raise RuntimeError("duplicate operation rejected")
                        existing = self._row(old)
                        if existing is None:
                            raise RuntimeError("duplicate operation disappeared")
                        db.commit()
                        return {**existing, "coalesced": True}
                db.execute(
                    "INSERT INTO runs(run_id,action,target,permission,status,title,message,details_json,user_id,source,channel_id,correlation_id,omp_session_id,pid,pid_start_fingerprint,heartbeat,created_at,updated_at,concurrency,confirmation_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        action,
                        target,
                        permission,
                        status,
                        title,
                        message,
                        json.dumps(details or {}, sort_keys=True),
                        user_id,
                        source,
                        channel_id,
                        correlation_id,
                        omp_session_id,
                        pid,
                        pid_start_fingerprint,
                        now,
                        now,
                        now,
                        policy,
                        confirmation_hash,
                    ),
                )
                created = self._row(
                    db.execute(
                        "SELECT * FROM runs WHERE run_id=?", (run_id,)
                    ).fetchone()
                )
                db.commit()
                return created or {}
            except Exception:
                db.rollback()
                raise

    def get_run(self, run_id: str | None) -> dict[str, Any] | None:
        if not run_id or not isinstance(run_id, str) or len(run_id) > 128:
            return None
        with self._connect() as db:
            return self._row(
                db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            )

    def list_runs(
        self, *, limit: int = 50, status: str | None = None
    ) -> list[dict[str, Any]]:
        # Deliberately lock-free: SQLite snapshot reads are safe while workers write.
        limit = max(1, min(int(limit), 200))
        with self._connect() as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM runs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            result = [self._row(row) for row in rows]
            return [row for row in result if row is not None]

    def transition(
        self,
        run_id: str,
        status: RunStatus | str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        target = RunStatus(status).value
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                current = self._row(
                    db.execute(
                        "SELECT * FROM runs WHERE run_id=?", (run_id,)
                    ).fetchone()
                )
                if current is None:
                    db.commit()
                    return None
                current_status = str(current["status"])
                if target != current_status and target not in _TRANSITIONS.get(
                    current_status, set()
                ):
                    raise ValueError(
                        f"illegal run transition {current_status} -> {target}"
                    )
                updates = {"status": target, "updated_at": now}
                if message is not None:
                    updates["message"] = str(message)[:2000]
                if details is not None:
                    updates["details_json"] = json.dumps(details, sort_keys=True)
                for name in (
                    "pid",
                    "pid_start_fingerprint",
                    "heartbeat",
                    "omp_session_id",
                ):
                    if name in fields:
                        updates[name] = fields[name]
                cur = db.execute(
                    f"UPDATE runs SET {', '.join(f'{key}=?' for key in updates)} "
                    "WHERE run_id=? AND status=?",
                    tuple(updates.values()) + (run_id, current_status),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("concurrent run transition")
                result = self._row(
                    db.execute(
                        "SELECT * FROM runs WHERE run_id=?", (run_id,)
                    ).fetchone()
                )
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

    def heartbeat(
        self,
        run_id: str,
        *,
        pid: int | None = None,
        pid_start_fingerprint: str | None = None,
    ) -> bool:
        fields: dict[str, Any] = {"heartbeat": time.time()}
        if pid is not None:
            fields["pid"] = pid
        if pid_start_fingerprint is not None:
            fields["pid_start_fingerprint"] = pid_start_fingerprint
        with self._connect() as db:
            sets = ", ".join(f"{key}=?" for key in fields)
            cur = db.execute(
                f"UPDATE runs SET {sets}, updated_at=? WHERE run_id=? AND status='running'",
                tuple(fields.values()) + (time.time(), run_id),
            )
            return cur.rowcount > 0

    def recover_stale(self, *, max_age: float = 900.0) -> list[str]:
        cutoff = time.time() - max(1.0, max_age)
        recovered: list[str] = []
        with self._connect() as db:
            rows = db.execute(
                "SELECT run_id,pid,pid_start_fingerprint FROM runs WHERE status IN ('queued','running') AND COALESCE(heartbeat,updated_at) < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                pid, fingerprint = row[1], row[2]
                if pid and fingerprint:
                    try:
                        import psutil

                        proc = psutil.Process(int(pid))
                        if proc.is_running() and str(proc.create_time()) == str(
                            fingerprint
                        ):
                            continue
                    except Exception:
                        pass
                run_id = str(row[0])
                db.execute(
                    "UPDATE runs SET status='retry', message='Recovered stale operation', updated_at=? WHERE run_id=? AND status IN ('queued','running')",
                    (time.time(), run_id),
                )
                recovered.append(run_id)
        return recovered

    def issue_confirmation(
        self, user_id: str, action: str, *, ttl: float = 120.0
    ) -> str:
        token = secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            db.execute(
                "INSERT INTO confirmations(token_hash,user_id,action,expires_at) VALUES(?,?,?,?)",
                (
                    digest,
                    str(user_id),
                    str(action),
                    time.time() + min(max(ttl, 1.0), 600.0),
                ),
            )
        return token

    def consume_confirmation(self, token: str, user_id: str, action: str) -> bool:
        if not token:
            return False
        digest = hashlib.sha256(str(token).encode()).hexdigest()
        with self._connect() as db:
            row = db.execute(
                "SELECT expires_at,used_at FROM confirmations WHERE token_hash=? AND user_id=? AND action=?",
                (digest, str(user_id), str(action)),
            ).fetchone()
            if row is None or row[1] is not None or float(row[0]) < time.time():
                return False
            cur = db.execute(
                "UPDATE confirmations SET used_at=? WHERE token_hash=? AND used_at IS NULL",
                (time.time(), digest),
            )
            return cur.rowcount == 1


RunLedger = Ledger
