#!/usr/bin/env python3
"""Run a real SQLite-backed PR lifecycle through CI, review, restart, and merge."""

from __future__ import annotations

import argparse
import json
import pathlib
import tempfile

from ctoloop.durable import Directive, DurableRuntime, WatchRequest
from ctoloop.state import State


WORKFLOW_ID = "pr:example/ctoloop:101"
RESOURCE = "example/ctoloop#101"


def handler(job, events):
    current = events[-1]["payload"] if events else job.payload
    if current.get("state") == "MERGED":
        return Directive.complete("completed", result="PR #101 merged")
    if current.get("checks") != "success":
        state = "awaiting_ci"
    elif current.get("review") != "approved":
        state = "awaiting_review"
    else:
        state = "merge_ready"
    return Directive.wait(
        state, payload=current, progress_key=json.dumps(current, sort_keys=True),
        delay_seconds=600,
        watches=(WatchRequest(
            "github-pr", RESOURCE, json.dumps(current, sort_keys=True),
        ),),
    )


def snapshot(runtime):
    job = runtime.get(WORKFLOW_ID)
    return {
        "state": job.state,
        "status": job.status,
        "iteration": job.iteration,
        "recovery_attempts": job.recovery_attempts,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=pathlib.Path)
    args = parser.parse_args()
    temporary = None
    if args.database is None:
        temporary = tempfile.TemporaryDirectory(prefix="ctoloop-durable-example-")
        database = pathlib.Path(temporary.name) / "runs.sqlite3"
    else:
        database = args.database.expanduser().resolve()

    timeline = []
    before_restart = DurableRuntime(
        State(database), owner="example-before-restart",
        heartbeat_seconds=10, lease_seconds=30, stalled_after_seconds=60,
        poll_fallback_seconds=600,
    )
    before_restart.enqueue(
        WORKFLOW_ID, repository="example/ctoloop", issue_number=100,
        kind="example-pr", state="awaiting_ci",
        payload={"state": "OPEN", "checks": "pending", "review": "pending"},
    )
    before_restart.run_one(handler)
    timeline.append({"event": "PR opened", **snapshot(before_restart)})
    before_restart.notify(
        event_key="ci-success", workflow_id=WORKFLOW_ID,
        event_type="github-pr", resource_id=RESOURCE,
        payload={"state": "OPEN", "checks": "success", "review": "pending"},
    )
    before_restart.run_one(handler)
    timeline.append({"event": "CI passed", **snapshot(before_restart)})

    after_restart = DurableRuntime(
        State(database), owner="example-after-restart",
        heartbeat_seconds=10, lease_seconds=30, stalled_after_seconds=60,
        poll_fallback_seconds=600,
    )
    timeline.append({"event": "daemon restarted", **snapshot(after_restart)})
    after_restart.notify(
        event_key="review-approved", workflow_id=WORKFLOW_ID,
        event_type="github-pr", resource_id=RESOURCE,
        payload={"state": "OPEN", "checks": "success", "review": "approved"},
    )
    after_restart.run_one(handler)
    timeline.append({"event": "review approved", **snapshot(after_restart)})
    after_restart.notify(
        event_key="merged", workflow_id=WORKFLOW_ID,
        event_type="github-pr", resource_id=RESOURCE,
        payload={"state": "MERGED", "checks": "success", "review": "approved"},
    )
    after_restart.run_one(handler)
    timeline.append({"event": "PR merged", **snapshot(after_restart)})

    attempts = after_restart.state.connection.execute(
        "SELECT iteration, owner, state, status FROM workflow_attempts "
        "WHERE workflow_id=? ORDER BY id",
        (WORKFLOW_ID,),
    ).fetchall()
    print(json.dumps({
        "database": str(database),
        "timeline": timeline,
        "attempts": [list(row) for row in attempts],
        "result": "PASS" if timeline[-1]["status"] == "completed" else "FAIL",
    }, ensure_ascii=False, indent=2))
    if temporary is not None:
        temporary.cleanup()


if __name__ == "__main__":
    main()
