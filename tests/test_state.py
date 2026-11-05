import json
import sqlite3

from ctoloop.state import State


def test_state_migrates_legacy_database_and_records_repository(tmp_path):
    path = tmp_path / "runs.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE runs (
        id INTEGER PRIMARY KEY, issue_number INTEGER NOT NULL, branch TEXT,
        status TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    connection.commit()
    connection.close()

    state = State(path)
    run_id = state.start("org/repo", 3, "codex/issue-3")
    state.finish(run_id, "pr-open", "https://example.test/pr/1")

    row = state.recent(1)[0]
    assert row[:4] == ("org/repo", 3, "codex/issue-3", "pr-open")


def test_state_records_execution_artifacts(tmp_path):
    state = State(tmp_path / "runs.sqlite3")
    run_id = state.start("org/repo", 4, "codex/issue-4")
    result_id = state.record_execution(
        run_id=run_id,
        repository="org/repo",
        issue_number=4,
        task_name="implementation",
        backend="gstack",
        requested_backend="gstack",
        status="completed",
        skills=("review", "health"),
        payload={"summary": "ok"},
    )

    assert result_id > 0
    row = state.recent_executions(1)[0]
    assert row[:6] == (
        "org/repo", 4, "implementation", "gstack", "gstack", "completed",
    )
    assert json.loads(row[6]) == ["review", "health"]


def test_state_keeps_truncated_execution_payload_as_valid_json(tmp_path):
    state = State(tmp_path / "runs.sqlite3")
    state.record_execution(
        repository="org/repo",
        task_name="review",
        backend="gstack",
        requested_backend="gstack",
        status="completed",
        skills=("review",),
        payload={"output": "x" * 60000},
    )

    payload = state.connection.execute(
        "SELECT payload FROM execution_results ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    parsed = json.loads(payload)
    assert parsed["truncated"] is True
    assert parsed["payload_tail"].endswith('"}')
