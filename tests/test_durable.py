from datetime import datetime, timedelta, timezone
import time

import pytest

from ctoloop.durable import (
    Directive, DurableRuntime, LeaseLost, WatchRequest,
)
from ctoloop.state import State


class Clock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def runtime(state, clock, owner="worker-a", **overrides):
    options = {
        "heartbeat_seconds": 10,
        "lease_seconds": 30,
        "stalled_after_seconds": 60,
        "max_recovery_attempts": 2,
        "retry_backoff_seconds": 5,
        "poll_fallback_seconds": 10,
    }
    options.update(overrides)
    return DurableRuntime(state, owner=owner, clock=clock, **options)


def test_atomic_lease_recovery_fences_stale_owner(tmp_path):
    path = tmp_path / "state.sqlite3"
    clock = Clock()
    first = runtime(State(path), clock, owner="first")
    second = runtime(State(path), clock, owner="second")
    assert first.enqueue(
        "job-1", repository="org/repo", kind="example", state="start", payload={},
    )
    assert first.enqueue(
        "job-1", repository="org/repo", kind="example", state="start", payload={},
    ) is False

    stale_job = first.acquire_due()
    assert stale_job is not None
    assert second.acquire_due() is None
    clock.advance(31)
    recovered = second.acquire_due()
    assert recovered is not None
    assert recovered.id == stale_job.id
    assert recovered.recovery_attempts == 1

    with pytest.raises(LeaseLost):
        first.apply(stale_job, Directive.complete("done", result="stale"))
    second.apply(recovered, Directive.complete("done", result="fresh"))
    assert second.get("job-1").status == "completed"


def test_reacquired_lease_fences_stale_attempt_with_same_owner(tmp_path):
    path = tmp_path / "same-owner.sqlite3"
    clock = Clock()
    first = runtime(State(path), clock, owner="shared-process")
    second = runtime(State(path), clock, owner="shared-process")
    first.enqueue(
        "job-1", repository="org/repo", kind="example", state="start", payload={},
    )

    stale = first.acquire_due()
    assert stale is not None
    clock.advance(31)
    recovered = second.acquire_due()
    assert recovered is not None
    assert recovered.iteration == stale.iteration + 1

    with pytest.raises(LeaseLost):
        first.apply(stale, Directive.complete("done", result="stale"))
    with pytest.raises(LeaseLost):
        first.heartbeat("job-1", iteration=stale.iteration)
    second.apply(recovered, Directive.complete("done", result="fresh"))
    assert second.get("job-1").result == "fresh"


def test_issue_delivery_is_serialized_per_repository(tmp_path):
    path = tmp_path / "serialized.sqlite3"
    clock = Clock()
    first = runtime(State(path), clock, owner="first")
    second = runtime(State(path), clock, owner="second")
    for number in (1, 2):
        first.enqueue(
            f"issue:org/repo:{number}", repository="org/repo", issue_number=number,
            kind="issue-delivery", state="queued", payload={},
        )

    active = first.acquire_due()
    assert active is not None
    assert second.acquire_due() is None
    first.apply(active, Directive.complete("done"))
    following = second.acquire_due()
    assert following is not None
    assert following.id != active.id


def test_watcher_wakes_only_on_changed_observation_and_deduplicates(tmp_path):
    clock = Clock()
    durable = runtime(State(tmp_path / "state.sqlite3"), clock)
    durable.enqueue(
        "pr-job", repository="org/repo", kind="example", state="awaiting_ci",
        payload={"pr": 7},
    )
    job = durable.acquire_due()
    durable.apply(job, Directive.wait(
        "awaiting_ci", delay_seconds=10, progress_key="pending",
        watches=(WatchRequest("github-pr", "org/repo#7", "pending"),),
    ))

    clock.advance(10)
    assert durable.poll_watches(lambda *_args: ("pending", {"checks": "pending"})) == 0
    assert durable.get("pr-job").status == "waiting"
    clock.advance(10)
    assert durable.poll_watches(lambda *_args: ("success", {"checks": "success"})) == 1
    assert durable.get("pr-job").status == "pending"
    assert durable.notify(
        event_key="same", workflow_id="pr-job", event_type="github-pr",
        resource_id="org/repo#7", payload={"checks": "success"},
    ) is False


def test_retry_survives_runtime_reconstruction(tmp_path):
    path = tmp_path / "state.sqlite3"
    clock = Clock()
    first = runtime(State(path), clock, owner="before-restart")
    first.enqueue(
        "restart-job", repository="org/repo", kind="example", state="build", payload={},
    )

    assert first.run_one(
        lambda job, events: Directive.retry(job.state, "temporary failure", 5)
    )
    assert first.get("restart-job").status == "pending"
    clock.advance(5)

    after_restart = runtime(State(path), clock, owner="after-restart")
    assert after_restart.run_one(
        lambda job, events: Directive.complete("done", result="resumed")
    )
    completed = after_restart.get("restart-job")
    assert completed.status == "completed"
    assert completed.iteration == 2


def test_execution_error_budget_stops_infinite_retry(tmp_path):
    clock = Clock()
    durable = runtime(
        State(tmp_path / "error-budget.sqlite3"), clock,
        max_execution_attempts=2,
    )
    durable.enqueue(
        "error-job", repository="org/repo", kind="example", state="work", payload={},
    )

    def broken(_job, _events):
        raise RuntimeError("still broken")

    assert durable.run_one(broken)
    assert durable.get("error-job").status == "pending"
    clock.advance(5)
    assert durable.run_one(broken)
    failed = durable.get("error-job")
    assert failed.status == "failed"
    assert failed.iteration == 2


def test_new_authorized_event_can_restart_terminal_workflow(tmp_path):
    clock = Clock()
    durable = runtime(State(tmp_path / "restart-terminal.sqlite3"), clock)
    durable.enqueue(
        "issue:org/repo:4", repository="org/repo", issue_number=4,
        kind="issue-delivery", state="queued", payload={"attempt": 1},
    )
    assert durable.run_one(
        lambda job, events: Directive.fail("failed", "first attempt failed")
    )
    assert durable.get("issue:org/repo:4").status == "failed"

    assert durable.enqueue(
        "issue:org/repo:4", repository="org/repo", issue_number=4,
        kind="issue-delivery", state="queued", payload={"attempt": 2},
        restart_terminal=True,
    )
    restarted = durable.get("issue:org/repo:4")
    assert restarted.status == "pending"
    assert restarted.payload == {"attempt": 2}


def test_background_heartbeat_renews_long_handler(tmp_path):
    clock = Clock()
    state = State(tmp_path / "heartbeat.sqlite3")
    durable = runtime(
        state, clock, heartbeat_seconds=0.01, lease_seconds=30,
        stalled_after_seconds=60,
    )
    durable.enqueue(
        "heartbeat-job", repository="org/repo", kind="example", state="work", payload={},
    )

    def slow_handler(job, events):
        clock.advance(5)
        time.sleep(0.04)
        return Directive.complete("done", result="heartbeat observed")

    assert durable.run_one(slow_handler)
    started, heartbeat = state.connection.execute(
        "SELECT started_at, heartbeat_at FROM workflow_attempts WHERE workflow_id=?",
        ("heartbeat-job",),
    ).fetchone()
    assert heartbeat > started


def test_progress_stall_is_recovered_even_when_lease_heartbeats_continue(tmp_path):
    path = tmp_path / "stall.sqlite3"
    clock = Clock()
    first = runtime(State(path), clock, owner="stalled")
    second = runtime(State(path), clock, owner="replacement")
    first.enqueue(
        "stall-job", repository="org/repo", kind="example", state="work", payload={},
    )
    acquired = first.acquire_due()
    assert acquired is not None
    for _ in range(3):
        clock.advance(20)
        first.heartbeat("stall-job", iteration=acquired.iteration)

    recovered = second.acquire_due()
    assert recovered is not None
    assert recovered.recovery_attempts == 1
    assert recovered.lease_owner == "replacement"


def test_waiting_workflow_fails_at_durable_deadline(tmp_path):
    clock = Clock()
    durable = runtime(State(tmp_path / "state.sqlite3"), clock)
    durable.enqueue(
        "deadline-job", repository="org/repo", kind="example", state="wait",
        payload={}, deadline_at=clock() + timedelta(seconds=20),
    )
    job = durable.acquire_due()
    durable.apply(job, Directive.wait(
        "wait", delay_seconds=10,
        watches=(WatchRequest("event", "resource", "same"),),
    ))
    clock.advance(21)

    assert durable.acquire_due() is None
    expired = durable.get("deadline-job")
    assert expired.status == "failed"


def test_broken_watcher_does_not_starve_other_workflows(tmp_path):
    clock = Clock()
    durable = runtime(State(tmp_path / "watchers.sqlite3"), clock)
    for job_id, resource in (("broken", "bad"), ("healthy", "good")):
        durable.enqueue(
            job_id, repository="org/repo", kind="example", state="wait", payload={},
        )
        job = durable.acquire_due()
        durable.apply(job, Directive.wait(
            "wait", delay_seconds=10,
            watches=(WatchRequest("source", resource, "old"),),
        ))
    clock.advance(10)

    def observer(_event_type, resource_id):
        if resource_id == "bad":
            raise RuntimeError("source unavailable")
        return "new", {"value": "new"}

    assert durable.poll_watches(observer) == 1
    assert durable.get("broken").status == "waiting"
    assert durable.get("healthy").status == "pending"


def test_complete_pr_ci_review_merge_example(tmp_path):
    """Real SQLite example: CI and review events wake a workflow through merge."""
    clock = Clock()
    durable = runtime(State(tmp_path / "example.sqlite3"), clock)
    durable.enqueue(
        "pr:org/repo:88", repository="org/repo", kind="pr-example",
        state="awaiting_ci", payload={"checks": "pending", "review": "pending"},
    )

    def handler(job, events):
        latest = events[-1]["payload"] if events else job.payload
        if latest.get("state") == "MERGED":
            return Directive.complete("completed", result="PR merged")
        if latest.get("checks") != "success":
            state = "awaiting_ci"
        elif latest.get("review") != "approved":
            state = "awaiting_review"
        else:
            state = "merge_ready"
        return Directive.wait(
            state, payload=latest, delay_seconds=10,
            progress_key=str(latest),
            watches=(WatchRequest("github-pr", "org/repo#88", str(latest)),),
        )

    assert durable.run_one(handler)
    assert durable.get("pr:org/repo:88").state == "awaiting_ci"
    durable.notify(
        event_key="ci-success", workflow_id="pr:org/repo:88",
        event_type="github-pr", resource_id="org/repo#88",
        payload={"checks": "success", "review": "pending", "state": "OPEN"},
    )
    assert durable.run_one(handler)
    assert durable.get("pr:org/repo:88").state == "awaiting_review"
    durable.notify(
        event_key="review-approved", workflow_id="pr:org/repo:88",
        event_type="github-pr", resource_id="org/repo#88",
        payload={"checks": "success", "review": "approved", "state": "OPEN"},
    )
    assert durable.run_one(handler)
    assert durable.get("pr:org/repo:88").state == "merge_ready"
    durable.notify(
        event_key="merged", workflow_id="pr:org/repo:88",
        event_type="github-pr", resource_id="org/repo#88",
        payload={"checks": "success", "review": "approved", "state": "MERGED"},
    )
    assert durable.run_one(handler)
    final = durable.get("pr:org/repo:88")
    assert final.status == "completed"
    assert final.state == "completed"
    attempts = durable.state.connection.execute(
        "SELECT status FROM workflow_attempts WHERE workflow_id=? ORDER BY id",
        ("pr:org/repo:88",),
    ).fetchall()
    assert [row[0] for row in attempts] == [
        "waiting", "waiting", "waiting", "completed",
    ]
