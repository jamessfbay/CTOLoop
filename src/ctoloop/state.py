"""Persistent run state for CTOLoop."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
from datetime import datetime, timezone


class State:
    def __init__(self, path: pathlib.Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.lock = threading.Lock()
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, repository TEXT NOT NULL DEFAULT '',
                issue_number INTEGER NOT NULL, branch TEXT,
                status TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(runs)")}
        if "repository" not in columns:
            self.connection.execute(
                "ALTER TABLE runs ADD COLUMN repository TEXT NOT NULL DEFAULT ''"
            )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS execution_results (
                id INTEGER PRIMARY KEY, run_id INTEGER,
                repository TEXT NOT NULL, issue_number INTEGER,
                task_name TEXT NOT NULL, backend TEXT NOT NULL,
                requested_backend TEXT NOT NULL, status TEXT NOT NULL,
                skills TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(id)
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS run_decisions (
                id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL,
                phase TEXT NOT NULL, decision TEXT NOT NULL,
                reason TEXT NOT NULL, evidence TEXT NOT NULL,
                result TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(id)
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS durable_workflows (
                id TEXT PRIMARY KEY, repository TEXT NOT NULL,
                issue_number INTEGER, kind TEXT NOT NULL, state TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL,
                iteration INTEGER NOT NULL DEFAULT 0,
                recovery_attempts INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT, lease_expires_at TEXT, heartbeat_at TEXT,
                progress_key TEXT NOT NULL DEFAULT '', progress_at TEXT NOT NULL,
                next_wake_at TEXT NOT NULL, deadline_at TEXT,
                result TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS workflow_watches (
                id INTEGER PRIMARY KEY, workflow_id TEXT NOT NULL,
                event_type TEXT NOT NULL, resource_id TEXT NOT NULL,
                last_observation TEXT NOT NULL DEFAULT '', next_check_at TEXT NOT NULL,
                deadline_at TEXT, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(workflow_id) REFERENCES durable_workflows(id)
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS workflow_events (
                id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                workflow_id TEXT NOT NULL, event_type TEXT NOT NULL,
                resource_id TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL, processed_at TEXT,
                FOREIGN KEY(workflow_id) REFERENCES durable_workflows(id)
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS workflow_attempts (
                id INTEGER PRIMARY KEY, workflow_id TEXT NOT NULL,
                iteration INTEGER NOT NULL, owner TEXT NOT NULL,
                state TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
                progress_key TEXT NOT NULL DEFAULT '', finished_at TEXT,
                detail TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(workflow_id) REFERENCES durable_workflows(id)
            )"""
        )
        self.connection.execute(
            """CREATE INDEX IF NOT EXISTS durable_workflows_due
                ON durable_workflows(status, next_wake_at)"""
        )
        self.connection.execute(
            """CREATE INDEX IF NOT EXISTS workflow_watches_due
                ON workflow_watches(active, next_check_at)"""
        )
        self.connection.execute(
            """CREATE INDEX IF NOT EXISTS workflow_events_pending
                ON workflow_events(workflow_id, processed_at)"""
        )
        self.connection.execute(
            """CREATE INDEX IF NOT EXISTS workflow_attempts_by_workflow
                ON workflow_attempts(workflow_id, id)"""
        )
        self.connection.commit()

    def start(self, repository: str, issue_number: int, branch: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            cursor = self.connection.execute(
                "INSERT INTO runs(repository, issue_number, branch, status, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (repository, issue_number, branch, "running", now, now),
            )
            self.connection.commit()
        return int(cursor.lastrowid)

    def finish(self, run_id: int, status: str, detail: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            self.connection.execute(
                "UPDATE runs SET status=?, detail=?, updated_at=? WHERE id=?",
                (status, detail[-20000:], now, run_id),
            )
            self.connection.commit()

    def pending_prs(self, repository: str) -> list[tuple[int, int, str]]:
        with self.lock:
            return [
                (int(run_id), int(issue_number), str(detail or ""))
                for run_id, issue_number, detail in self.connection.execute(
                    "SELECT id, issue_number, detail FROM runs "
                    "WHERE repository=? AND status='pr-open' ORDER BY id",
                    (repository,),
                )
            ]

    def record_execution(
        self,
        *,
        repository: str,
        task_name: str,
        backend: str,
        requested_backend: str,
        status: str,
        skills: tuple[str, ...],
        payload: dict[str, object],
        run_id: int | None = None,
        issue_number: int | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload, ensure_ascii=False)
        if len(payload_json) > 50000:
            payload_json = json.dumps({
                "truncated": True,
                "payload_tail": payload_json[-48000:],
            }, ensure_ascii=False)
        with self.lock:
            cursor = self.connection.execute(
                "INSERT INTO execution_results("
                "run_id, repository, issue_number, task_name, backend, "
                "requested_backend, status, skills, payload, created_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, repository, issue_number, task_name, backend,
                    requested_backend, status, json.dumps(skills),
                    payload_json, now,
                ),
            )
            self.connection.commit()
        return int(cursor.lastrowid)

    def recent_executions(self, limit: int = 20) -> list[tuple[object, ...]]:
        with self.lock:
            return list(self.connection.execute(
                "SELECT repository, issue_number, task_name, backend, "
                "requested_backend, status, skills, created_at "
                "FROM execution_results ORDER BY id DESC LIMIT ?",
                (limit,),
            ))

    def record_decision(
        self, run_id: int, *, phase: str, decision: str, reason: str,
        evidence: str = "", result: str = "open",
    ) -> int:
        """Append one immutable, reviewable workflow decision."""
        now = datetime.now(timezone.utc).isoformat()
        values = tuple(
            str(value).replace("\x00", "").replace("\r", " ").replace("\n", " ")[:4000]
            for value in (phase, decision, reason, evidence, result)
        )
        with self.lock:
            cursor = self.connection.execute(
                "INSERT INTO run_decisions("
                "run_id, phase, decision, reason, evidence, result, created_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                (run_id, *values, now),
            )
            self.connection.commit()
        return int(cursor.lastrowid)

    def decisions(self, run_id: int) -> list[tuple[object, ...]]:
        with self.lock:
            return list(self.connection.execute(
                "SELECT phase, decision, reason, evidence, result, created_at "
                "FROM run_decisions WHERE run_id=? ORDER BY id",
                (run_id,),
            ))

    def recent(self, limit: int = 20) -> list[tuple[object, ...]]:
        with self.lock:
            return list(self.connection.execute(
                "SELECT repository, issue_number, branch, status, updated_at, detail "
                "FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ))
