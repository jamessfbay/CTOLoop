"""Durable heartbeat, lease, event, and watcher runtime for long workflows."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .state import State


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class LeaseLost(RuntimeError):
    """The caller no longer owns the workflow lease and must not publish effects."""


@dataclass(frozen=True)
class DurableJob:
    id: str
    repository: str
    issue_number: int | None
    kind: str
    state: str
    status: str
    payload: dict[str, object]
    iteration: int
    recovery_attempts: int
    lease_owner: str
    progress_key: str
    lease_expires_at: str
    heartbeat_at: str
    progress_at: str
    next_wake_at: str
    deadline_at: str
    result: str
    last_error: str
    updated_at: str


@dataclass(frozen=True)
class WatchRequest:
    event_type: str
    resource_id: str
    observation: str = ""
    deadline_at: datetime | None = None


@dataclass(frozen=True)
class Directive:
    action: str
    state: str
    payload: dict[str, object] = field(default_factory=dict)
    result: str = ""
    error: str = ""
    delay_seconds: int = 0
    progress_key: str = ""
    watches: tuple[WatchRequest, ...] = ()

    @classmethod
    def wait(
        cls, state: str, *, payload: dict[str, object] | None = None,
        watches: tuple[WatchRequest, ...] = (), delay_seconds: int = 0,
        progress_key: str = "",
    ) -> "Directive":
        return cls(
            "wait", state, payload or {}, delay_seconds=delay_seconds,
            progress_key=progress_key, watches=watches,
        )

    @classmethod
    def complete(cls, state: str, result: str = "") -> "Directive":
        return cls("complete", state, result=result)

    @classmethod
    def retry(cls, state: str, error: str, delay_seconds: int) -> "Directive":
        return cls("retry", state, error=error, delay_seconds=delay_seconds)

    @classmethod
    def fail(cls, state: str, error: str, *, needs_input: bool = False) -> "Directive":
        return cls("needs_input" if needs_input else "fail", state, error=error)


class Observer(Protocol):
    def __call__(self, event_type: str, resource_id: str) -> tuple[str, dict[str, object]]: ...


class DurableRuntime:
    """SQLite-backed scheduler with atomic leases and change-triggered watches."""

    def __init__(
        self, state: State, *, owner: str | None = None,
        heartbeat_seconds: int = 300, lease_seconds: int = 900,
        stalled_after_seconds: int = 1800, max_recovery_attempts: int = 3,
        max_execution_attempts: int = 6,
        retry_backoff_seconds: int = 120, poll_fallback_seconds: int = 600,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.state = state
        self.owner = owner or f"ctoloop-{uuid.uuid4().hex}"
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_seconds = lease_seconds
        self.stalled_after_seconds = stalled_after_seconds
        self.max_recovery_attempts = max_recovery_attempts
        self.max_execution_attempts = max_execution_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.poll_fallback_seconds = poll_fallback_seconds
        self.clock = clock
        self._lease_context = threading.local()

    def _iteration(self, workflow_id: str, iteration: int | None) -> int:
        if iteration is not None:
            return iteration
        if getattr(self._lease_context, "workflow_id", None) == workflow_id:
            active = getattr(self._lease_context, "iteration", None)
            if active is not None:
                return int(active)
        raise LeaseLost(f"No active lease attempt for workflow: {workflow_id}")

    def owns_lease(self, workflow_id: str, *, iteration: int | None = None) -> bool:
        """Return whether this exact execution attempt still owns the lease."""
        expected = self._iteration(workflow_id, iteration)
        with self.state.lock:
            row = self.state.connection.execute(
                """SELECT 1 FROM durable_workflows WHERE id=? AND status='running'
                    AND lease_owner=? AND iteration=?""",
                (workflow_id, self.owner, expected),
            ).fetchone()
        return row is not None

    def enqueue(
        self, workflow_id: str, *, repository: str, kind: str, state: str,
        payload: dict[str, object], issue_number: int | None = None,
        delay_seconds: int = 0, deadline_at: datetime | None = None,
        restart_terminal: bool = False,
    ) -> bool:
        now = self.clock()
        with self.state.lock:
            cursor = self.state.connection.execute(
                """INSERT OR IGNORE INTO durable_workflows(
                    id, repository, issue_number, kind, state, status, payload,
                    iteration, recovery_attempts, next_wake_at, deadline_at,
                    created_at, updated_at, progress_at
                ) VALUES(?, ?, ?, ?, ?, 'pending', ?, 0, 0, ?, ?, ?, ?, ?)""",
                (
                    workflow_id, repository, issue_number, kind, state, _json(payload),
                    _iso(now + timedelta(seconds=delay_seconds)),
                    _iso(deadline_at) if deadline_at else None,
                    _iso(now), _iso(now), _iso(now),
                ),
            )
            if cursor.rowcount == 0 and restart_terminal:
                cursor = self.state.connection.execute(
                    """UPDATE durable_workflows SET repository=?, issue_number=?,
                        kind=?, state=?, status='pending', payload=?,
                        recovery_attempts=0, lease_owner=NULL, lease_expires_at=NULL,
                        heartbeat_at=NULL, progress_key='', progress_at=?,
                        next_wake_at=?, deadline_at=?, result='', last_error='', updated_at=?
                        WHERE id=? AND status IN (
                            'completed', 'failed', 'needs_input', 'cancelled'
                        )""",
                    (
                        repository, issue_number, kind, state, _json(payload), _iso(now),
                        _iso(now + timedelta(seconds=delay_seconds)),
                        _iso(deadline_at) if deadline_at else None,
                        _iso(now), workflow_id,
                    ),
                )
                if cursor.rowcount == 1:
                    self.state.connection.execute(
                        "UPDATE workflow_watches SET active=0 WHERE workflow_id=?",
                        (workflow_id,),
                    )
            self.state.connection.commit()
        return cursor.rowcount == 1

    def get(self, workflow_id: str) -> DurableJob | None:
        with self.state.lock:
            row = self.state.connection.execute(
                """SELECT id, repository, issue_number, kind, state, status,
                    payload, iteration, recovery_attempts, COALESCE(lease_owner, ''),
                    COALESCE(progress_key, ''), COALESCE(lease_expires_at, ''),
                    COALESCE(heartbeat_at, ''), progress_at, next_wake_at,
                    COALESCE(deadline_at, ''), result, last_error, updated_at
                    FROM durable_workflows WHERE id=?""",
                (workflow_id,),
            ).fetchone()
        return self._job(row) if row else None

    @staticmethod
    def _job(row: tuple[object, ...]) -> DurableJob:
        return DurableJob(
            id=str(row[0]), repository=str(row[1]),
            issue_number=int(row[2]) if row[2] is not None else None,
            kind=str(row[3]), state=str(row[4]), status=str(row[5]),
            payload=json.loads(str(row[6])), iteration=int(row[7]),
            recovery_attempts=int(row[8]), lease_owner=str(row[9]),
            progress_key=str(row[10]),
            lease_expires_at=str(row[11]), heartbeat_at=str(row[12]),
            progress_at=str(row[13]), next_wake_at=str(row[14]),
            deadline_at=str(row[15]), result=str(row[16]),
            last_error=str(row[17]), updated_at=str(row[18]),
        )

    def acquire_due(self) -> DurableJob | None:
        now = self.clock()
        stalled_before = now - timedelta(seconds=self.stalled_after_seconds)
        lease_until = now + timedelta(seconds=self.lease_seconds)
        with self.state.lock:
            connection = self.state.connection
            connection.execute("BEGIN IMMEDIATE")
            timed_out = connection.execute(
                """SELECT id FROM durable_workflows
                    WHERE status IN ('pending', 'waiting')
                    AND deadline_at IS NOT NULL AND deadline_at <= ?""",
                (_iso(now),),
            ).fetchall()
            for (workflow_id,) in timed_out:
                connection.execute(
                    """UPDATE durable_workflows SET status='failed',
                        last_error='workflow deadline exceeded', updated_at=?
                        WHERE id=?""",
                    (_iso(now), str(workflow_id)),
                )
                connection.execute(
                    "UPDATE workflow_watches SET active=0 WHERE workflow_id=?",
                    (str(workflow_id),),
                )
            expired = connection.execute(
                """SELECT id, recovery_attempts FROM durable_workflows
                    WHERE status='running' AND (
                        lease_expires_at <= ? OR progress_at <= ?
                    )""",
                (_iso(now), _iso(stalled_before)),
            ).fetchall()
            for workflow_id, recoveries in expired:
                next_recovery = int(recoveries) + 1
                terminal = next_recovery > self.max_recovery_attempts
                connection.execute(
                    """UPDATE durable_workflows SET status=?, recovery_attempts=?,
                        lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                        next_wake_at=?, last_error=?, updated_at=? WHERE id=?""",
                    (
                        "failed" if terminal else "pending", next_recovery, _iso(now),
                        "stalled workflow exceeded recovery budget" if terminal
                        else "recovered expired or stalled lease",
                        _iso(now), str(workflow_id),
                    ),
                )
                connection.execute(
                    """UPDATE workflow_attempts SET status='abandoned', finished_at=?,
                        detail='lease expired or progress stalled'
                        WHERE workflow_id=? AND status='running'""",
                    (_iso(now), str(workflow_id)),
                )
            row = connection.execute(
                """SELECT candidate.id FROM durable_workflows AS candidate
                    WHERE candidate.status='pending' AND candidate.next_wake_at <= ?
                    AND (candidate.deadline_at IS NULL OR candidate.deadline_at > ?)
                    AND (
                        candidate.kind != 'issue-delivery' OR NOT EXISTS (
                            SELECT 1 FROM durable_workflows AS active
                            WHERE active.repository=candidate.repository
                            AND active.kind='issue-delivery' AND active.status='running'
                        )
                    )
                    ORDER BY candidate.next_wake_at, candidate.created_at LIMIT 1""",
                (_iso(now), _iso(now)),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            workflow_id = str(row[0])
            updated = connection.execute(
                """UPDATE durable_workflows SET status='running', lease_owner=?,
                    lease_expires_at=?, heartbeat_at=?, progress_at=?,
                    iteration=iteration+1, updated_at=?
                    WHERE id=? AND status='pending'""",
                (
                    self.owner, _iso(lease_until), _iso(now), _iso(now),
                    _iso(now), workflow_id,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            current = connection.execute(
                """SELECT id, repository, issue_number, kind, state, status,
                    payload, iteration, recovery_attempts, COALESCE(lease_owner, ''),
                    COALESCE(progress_key, ''), COALESCE(lease_expires_at, ''),
                    COALESCE(heartbeat_at, ''), progress_at, next_wake_at,
                    COALESCE(deadline_at, ''), result, last_error, updated_at
                    FROM durable_workflows WHERE id=?""",
                (workflow_id,),
            ).fetchone()
            connection.execute(
                """INSERT INTO workflow_attempts(
                    workflow_id, iteration, owner, state, status, started_at,
                    heartbeat_at, progress_key
                ) VALUES(?, ?, ?, ?, 'running', ?, ?, ?)""",
                (
                    workflow_id, int(current[7]), self.owner, str(current[4]),
                    _iso(now), _iso(now), str(current[10]),
                ),
            )
            connection.commit()
        return self._job(current)

    def heartbeat(
        self, workflow_id: str, progress_key: str = "", *,
        iteration: int | None = None,
    ) -> None:
        expected = self._iteration(workflow_id, iteration)
        now = self.clock()
        lease_until = now + timedelta(seconds=self.lease_seconds)
        with self.state.lock:
            current = self.state.connection.execute(
                "SELECT progress_key FROM durable_workflows WHERE id=?",
                (workflow_id,),
            ).fetchone()
            changed = bool(progress_key and (not current or str(current[0] or "") != progress_key))
            cursor = self.state.connection.execute(
                """UPDATE durable_workflows SET heartbeat_at=?, lease_expires_at=?,
                    progress_key=CASE WHEN ? != '' THEN ? ELSE progress_key END,
                    progress_at=CASE WHEN ? THEN ? ELSE progress_at END,
                    updated_at=? WHERE id=? AND status='running' AND lease_owner=?
                    AND iteration=?""",
                (
                    _iso(now), _iso(lease_until), progress_key, progress_key,
                    changed, _iso(now), _iso(now), workflow_id, self.owner, expected,
                ),
            )
            self.state.connection.execute(
                """UPDATE workflow_attempts SET heartbeat_at=?,
                    progress_key=CASE WHEN ? != '' THEN ? ELSE progress_key END
                    WHERE workflow_id=? AND owner=? AND iteration=?
                    AND status='running'""",
                (
                    _iso(now), progress_key, progress_key, workflow_id,
                    self.owner, expected,
                ),
            )
            self.state.connection.commit()
        if cursor.rowcount != 1:
            raise LeaseLost(f"Workflow lease lost: {workflow_id}")

    def checkpoint(
        self, workflow_id: str, *, state: str,
        payload: dict[str, object], progress_key: str,
        iteration: int | None = None,
    ) -> None:
        """Persist resumable progress while retaining and fencing the active lease."""
        expected = self._iteration(workflow_id, iteration)
        now = self.clock()
        lease_until = now + timedelta(seconds=self.lease_seconds)
        with self.state.lock:
            connection = self.state.connection
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT payload FROM durable_workflows
                    WHERE id=? AND status='running' AND lease_owner=? AND iteration=?""",
                (workflow_id, self.owner, expected),
            ).fetchone()
            if not row:
                connection.rollback()
                raise LeaseLost(f"Workflow lease lost before checkpoint: {workflow_id}")
            merged = {**json.loads(str(row[0])), **payload}
            cursor = connection.execute(
                """UPDATE durable_workflows SET state=?, payload=?, progress_key=?,
                    progress_at=?, heartbeat_at=?, lease_expires_at=?, updated_at=?
                    WHERE id=? AND status='running' AND lease_owner=? AND iteration=?""",
                (
                    state, _json(merged), progress_key, _iso(now), _iso(now),
                    _iso(lease_until), _iso(now), workflow_id, self.owner, expected,
                ),
            )
            connection.execute(
                """UPDATE workflow_attempts SET heartbeat_at=?, progress_key=?, state=?
                    WHERE workflow_id=? AND owner=? AND iteration=?
                    AND status='running'""",
                (
                    _iso(now), progress_key, state, workflow_id,
                    self.owner, expected,
                ),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise LeaseLost(f"Workflow lease lost before checkpoint: {workflow_id}")

    def apply(self, job: DurableJob, directive: Directive) -> None:
        if directive.action not in {"wait", "retry", "complete", "fail", "needs_input"}:
            raise ValueError(f"Unsupported durable workflow action: {directive.action}")
        now = self.clock()
        status = {
            "wait": "waiting", "retry": "pending", "complete": "completed",
            "fail": "failed", "needs_input": "needs_input",
        }[directive.action]
        wake_delay = directive.delay_seconds
        if directive.action == "retry" and wake_delay <= 0:
            wake_delay = self.retry_backoff_seconds
        with self.state.lock:
            connection = self.state.connection
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT payload FROM durable_workflows
                    WHERE id=? AND status='running' AND lease_owner=? AND iteration=?""",
                (job.id, self.owner, job.iteration),
            ).fetchone()
            if not current:
                connection.rollback()
                raise LeaseLost(f"Workflow lease lost before transition: {job.id}")
            payload = {**json.loads(str(current[0])), **directive.payload}
            cursor = connection.execute(
                """UPDATE durable_workflows SET state=?, status=?, payload=?,
                    next_wake_at=?, result=?, last_error=?, progress_key=?,
                    progress_at=CASE WHEN ? != '' THEN ? ELSE progress_at END,
                    lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                    updated_at=? WHERE id=? AND status='running' AND lease_owner=?
                    AND iteration=?""",
                (
                    directive.state, status, _json(payload),
                    _iso(now + timedelta(seconds=wake_delay)), directive.result,
                    directive.error, directive.progress_key, directive.progress_key,
                    _iso(now), _iso(now), job.id, self.owner, job.iteration,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise LeaseLost(f"Workflow lease lost before transition: {job.id}")
            connection.execute(
                """UPDATE workflow_attempts SET status=?, finished_at=?, detail=?
                    WHERE workflow_id=? AND owner=? AND iteration=?
                    AND status='running'""",
                (
                    status, _iso(now), directive.result or directive.error,
                    job.id, self.owner, job.iteration,
                ),
            )
            connection.execute(
                "UPDATE workflow_watches SET active=0 WHERE workflow_id=? AND active=1",
                (job.id,),
            )
            if directive.action == "wait":
                for watch in directive.watches:
                    connection.execute(
                        """INSERT INTO workflow_watches(
                            workflow_id, event_type, resource_id, last_observation,
                            next_check_at, deadline_at, active, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                        (
                            job.id, watch.event_type, watch.resource_id,
                            watch.observation,
                            _iso(now + timedelta(seconds=max(1, wake_delay or self.poll_fallback_seconds))),
                            _iso(watch.deadline_at) if watch.deadline_at else None,
                            _iso(now), _iso(now),
                        ),
                    )
            connection.commit()

    def notify(
        self, *, event_key: str, event_type: str, resource_id: str,
        payload: dict[str, object], workflow_id: str | None = None,
    ) -> bool:
        now = self.clock()
        with self.state.lock:
            connection = self.state.connection
            connection.execute("BEGIN IMMEDIATE")
            if workflow_id is None:
                matches = connection.execute(
                    """SELECT DISTINCT workflow_id FROM workflow_watches
                        WHERE active=1 AND event_type=? AND resource_id=?""",
                    (event_type, resource_id),
                ).fetchall()
                workflow_ids = [str(row[0]) for row in matches]
            else:
                active = connection.execute(
                    """SELECT 1 FROM workflow_watches WHERE workflow_id=?
                        AND event_type=? AND resource_id=? AND active=1 LIMIT 1""",
                    (workflow_id, event_type, resource_id),
                ).fetchone()
                workflow_ids = [workflow_id] if active else []
            inserted = False
            for matched_id in workflow_ids:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO workflow_events(
                        event_key, workflow_id, event_type, resource_id, payload,
                        created_at, processed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, NULL)""",
                    (
                        f"{event_key}:{matched_id}", matched_id, event_type,
                        resource_id, _json(payload), _iso(now),
                    ),
                )
                if cursor.rowcount == 1:
                    inserted = True
                    connection.execute(
                        """UPDATE durable_workflows SET status='pending', next_wake_at=?,
                            updated_at=? WHERE id=? AND status='waiting'""",
                        (_iso(now), _iso(now), matched_id),
                    )
                    connection.execute(
                        """UPDATE workflow_watches SET active=0, updated_at=?
                            WHERE workflow_id=? AND active=1 AND event_type=? AND resource_id=?""",
                        (_iso(now), matched_id, event_type, resource_id),
                    )
            connection.commit()
        return inserted

    def events(self, workflow_id: str, *, unprocessed_only: bool = False) -> list[dict[str, object]]:
        where = " AND processed_at IS NULL" if unprocessed_only else ""
        with self.state.lock:
            rows = self.state.connection.execute(
                "SELECT event_key, event_type, resource_id, payload, created_at, processed_at "
                f"FROM workflow_events WHERE workflow_id=?{where} ORDER BY id",
                (workflow_id,),
            ).fetchall()
        return [{
            "event_key": row[0], "event_type": row[1], "resource_id": row[2],
            "payload": json.loads(str(row[3])), "created_at": row[4],
            "processed_at": row[5],
        } for row in rows]

    def mark_events_processed(self, workflow_id: str) -> None:
        now = self.clock()
        with self.state.lock:
            self.state.connection.execute(
                """UPDATE workflow_events SET processed_at=?
                    WHERE workflow_id=? AND processed_at IS NULL""",
                (_iso(now), workflow_id),
            )
            self.state.connection.commit()

    def poll_watches(self, observer: Observer, *, limit: int = 20) -> int:
        now = self.clock()
        with self.state.lock:
            rows = self.state.connection.execute(
                """SELECT id, workflow_id, event_type, resource_id,
                    last_observation, deadline_at FROM workflow_watches
                    WHERE active=1 AND next_check_at <= ? ORDER BY next_check_at LIMIT ?""",
                (_iso(now), limit),
            ).fetchall()
        changes = 0
        for watch_id, workflow_id, event_type, resource_id, previous, deadline in rows:
            try:
                if deadline and str(deadline) <= _iso(now):
                    observation, payload = "deadline", {"deadline": str(deadline)}
                else:
                    observation, payload = observer(str(event_type), str(resource_id))
            except Exception as exc:
                with self.state.lock:
                    self.state.connection.execute(
                        """UPDATE workflow_watches SET next_check_at=?, updated_at=?
                            WHERE id=? AND active=1""",
                        (
                            _iso(now + timedelta(seconds=self.poll_fallback_seconds)),
                            _iso(now), int(watch_id),
                        ),
                    )
                    self.state.connection.execute(
                        """UPDATE durable_workflows SET last_error=?, updated_at=?
                            WHERE id=? AND status='waiting'""",
                        (
                            f"watch observation failed: {exc}"[:4000],
                            _iso(now), str(workflow_id),
                        ),
                    )
                    self.state.connection.commit()
                continue
            if observation != str(previous or ""):
                digest = hashlib.sha256(
                    f"{watch_id}:{observation}".encode("utf-8")
                ).hexdigest()
                if self.notify(
                    event_key=f"watch:{digest}", event_type=str(event_type),
                    resource_id=str(resource_id), payload=payload,
                    workflow_id=str(workflow_id),
                ):
                    changes += 1
            else:
                with self.state.lock:
                    self.state.connection.execute(
                        """UPDATE workflow_watches SET next_check_at=?, updated_at=?
                            WHERE id=? AND active=1""",
                        (
                            _iso(now + timedelta(seconds=self.poll_fallback_seconds)),
                            _iso(now), int(watch_id),
                        ),
                    )
                    self.state.connection.commit()
        return changes

    def run_one(self, handler: Callable[[DurableJob, tuple[dict[str, object], ...]], Directive]) -> bool:
        job = self.acquire_due()
        if job is None:
            return False
        stop = threading.Event()

        def pulse() -> None:
            while not stop.wait(self.heartbeat_seconds):
                try:
                    self.heartbeat(job.id, iteration=job.iteration)
                except LeaseLost:
                    return

        thread = threading.Thread(target=pulse, name=f"heartbeat-{job.id}", daemon=True)
        thread.start()
        self._lease_context.workflow_id = job.id
        self._lease_context.iteration = job.iteration
        try:
            events = tuple(self.events(job.id, unprocessed_only=True))
            directive = handler(job, events)
            self.apply(job, directive)
            self.mark_events_processed(job.id)
        except LeaseLost:
            raise
        except Exception as exc:
            directive = (
                Directive.fail(
                    "failed", f"execution attempt budget exhausted: {exc}",
                )
                if job.iteration >= self.max_execution_attempts
                else Directive.retry(
                    job.state, str(exc), self.retry_backoff_seconds,
                )
            )
            self.apply(job, directive)
        finally:
            self._lease_context.workflow_id = None
            self._lease_context.iteration = None
            stop.set()
            thread.join(timeout=1)
        return True
