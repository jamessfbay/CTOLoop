import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctoloop.commands import expand_argv_globs
from ctoloop.backends import CodexBackend, ExecutionResult, GStackBackend
from ctoloop.config import load_settings
from ctoloop.github import (
    GitHub, Issue, PullRequest, PullRequestLifecycle, PullRequestStatus, WorkflowRun,
)
from ctoloop.service import AgentError, CTOLoopService, ValidationResult
from ctoloop.durable import LeaseLost
from ctoloop.workflows import WorkflowPlan


def settings():
    return load_settings(Path(__file__).parents[1] / "config.example.toml")


def test_parse_issue():
    issue = GitHub._parse_issue({
        "number": 12,
        "title": "Broken thing",
        "body": "details",
        "url": "https://example.test/12",
        "author": {"login": "alice"},
        "labels": [{"name": "agent:ready"}],
        "comments": [{"author": {"login": "bob"}, "body": "more"}],
    })
    assert issue.number == 12
    assert issue.labels == ("agent:ready",)
    assert issue.comments[0]["author"] == "bob"


def test_github_pr_lifecycle_reduces_checks_and_latest_reviews(monkeypatch):
    github = GitHub(settings().github, Path("/tmp"))
    monkeypatch.setattr(github, "_gh", lambda *_args, **_kwargs: json.dumps({
        "state": "OPEN",
        "headRefOid": "abc123",
        "mergeStateStatus": "CLEAN",
        "isDraft": False,
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "COMPLETED", "conclusion": "NEUTRAL"},
        ],
        "reviews": [
            {
                "author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-01-01T00:00:00Z",
            },
            {
                "author": {"login": "reviewer"}, "state": "APPROVED",
                "submittedAt": "2026-01-01T01:00:00Z",
            },
        ],
    }))

    lifecycle = github.pr_lifecycle(3)
    assert lifecycle.head_sha == "abc123"
    assert lifecycle.checks == "success"
    assert lifecycle.review == "approved"
    assert lifecycle.merge_state == "CLEAN"


@pytest.mark.parametrize("remote", (
    "https://github.com/your-org/your-repo.git",
    "git@github.com:your-org/your-repo.git",
    "ssh://git@github.com/your-org/your-repo",
))
def test_github_remote_repository_parsing(remote):
    assert CTOLoopService(settings())._github_repository_from_remote(remote) == "your-org/your-repo"


def test_protected_paths_are_rejected():
    service = CTOLoopService(settings())
    with pytest.raises(AgentError, match="protected paths"):
        service._check_protected_paths((".github/workflows/evil.yml",))


def test_agent_identity_uses_configured_name_and_icon(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('''
[automation]
agent_name = "Fixer"
agent_icon = "🛠️"

[[repositories]]
repository = "org/repo"
path = "/srv/repo"
''')
    service = CTOLoopService(load_settings(config))
    assert service.workers[0].agent_identity == "🛠️ Fixer"


def test_merged_pr_updates_configured_production_checkout(monkeypatch, tmp_path):
    production_path = tmp_path / "production"
    config = tmp_path / "config.toml"
    config.write_text(f'''
[[repositories]]
repository = "org/repo"
path = "/srv/repo"
production_path = "{production_path}"
''')
    service = CTOLoopService(load_settings(config))
    worker = service.workers[0]
    run_id = service.state.start("org/repo", 7, "codex/issue-7")
    pr_url = "https://github.com/org/repo/pull/8"
    service.state.finish(run_id, "pr-open", pr_url)
    comments = []

    monkeypatch.setattr(
        worker.github, "pr_status", lambda _url: PullRequestStatus("MERGED", "main"),
    )
    monkeypatch.setattr(worker, "_update_production_checkout", lambda path: "abc123")
    monkeypatch.setattr(worker.github, "comment", lambda issue, body: comments.append((issue, body)))

    assert worker.sync_production_updates() == 1
    assert service.state.recent(1)[0][3] == "production-updated"
    assert comments == [(
        7,
        "### Step: Synchronize production\n\n"
        "🚀 Production files synchronized to `abc123` from `origin/main`.",
    )]


def test_production_commands_run_after_sync_in_order(monkeypatch, tmp_path):
    production_path = tmp_path / "production"
    production_path.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''
[[repositories]]
repository = "org/repo"
path = "/srv/repo"
production_path = "{production_path}"

[[repositories.production_commands]]
name = "build"
argv = ["npm", "run", "build"]

[[repositories.production_commands]]
name = "restart"
argv = ["systemctl", "restart", "example.service"]
''')
    worker = CTOLoopService(load_settings(config)).workers[0]
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((tuple(argv), kwargs["cwd"]))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr("ctoloop.service.run", fake_run)

    assert worker._run_production_commands(production_path) == ("build", "restart")
    assert calls == [
        (("npm", "run", "build"), production_path),
        (("systemctl", "restart", "example.service"), production_path),
    ]


def test_manual_update_uses_checkout_and_copies_tracked_files(monkeypatch, tmp_path):
    production_path = tmp_path / "production"
    checkout_path = tmp_path / "production-checkout"
    production_path.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''
[[repositories]]
repository = "org/repo"
path = "/srv/repo"
base_branch = "main"
production_path = "{production_path}"
production_checkout_path = "{checkout_path}"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]
    calls = []

    monkeypatch.setattr(
        worker, "_ensure_production_checkout",
        lambda path: calls.append(("ensure", path)),
    )

    def fake_run(argv, **kwargs):
        calls.append((tuple(argv), kwargs["cwd"]))
        return SimpleNamespace(stdout="old123\n")

    monkeypatch.setattr("ctoloop.service.run", fake_run)
    monkeypatch.setattr(
        worker, "_update_production_checkout",
        lambda path: calls.append(("update", path)) or "new123",
    )
    monkeypatch.setattr(
        worker, "_copy_tracked_files",
        lambda checkout, production, old, new: calls.append(
            ("copy", checkout, production, old, new)
        ),
    )

    assert worker.update_production() == "new123"
    assert calls == [
        ("ensure", checkout_path),
        (("git", "rev-parse", "HEAD"), checkout_path),
        (("git", "checkout", "main"), checkout_path),
        ("update", checkout_path),
        ("copy", checkout_path, production_path, "old123", "new123"),
    ]


def test_step_message_has_consistent_heading():
    worker = CTOLoopService(settings()).workers[0]
    assert worker.step_message("Validate changes", "Running tests.") == (
        "### Step: Validate changes\n\nRunning tests."
    )


def test_step_log_includes_repository_issue_and_step(caplog):
    worker = CTOLoopService(settings()).workers[0]
    with caplog.at_level("INFO", logger="ctoloop.service"):
        worker.log_step(12, "Validate changes", "running api-tests")
    assert "your-org/your-repo issue #12 — Step: Validate changes — running api-tests" in caplog.text


def test_issue_triage_uses_read_only_structured_codex(monkeypatch):
    worker = CTOLoopService(settings()).workers[0]
    issue = Issue(
        number=21,
        title="How does authentication work?",
        body="Please explain the current design.",
        url="https://github.com/your-org/your-repo/issues/21",
        author="alice",
        labels=(worker.target.github.ready_label,),
        comments=({"author": "bob", "body": "Focus on GitHub auth."},),
    )
    captured = {}

    def fake_codex(prompt, schema, **kwargs):
        captured.update(prompt=prompt, schema=schema, kwargs=kwargs)
        return {
            "intent": "conversation",
            "reason": "The Issue asks for an explanation only.",
            "answer": "Authentication is isolated per repository.",
        }

    monkeypatch.setattr(worker, "_run_structured_codex", fake_codex)

    assert worker._triage_issue(issue) == {
        "intent": "conversation",
        "reason": "The Issue asks for an explanation only.",
        "answer": "Authentication is isolated per repository.",
    }
    assert captured["kwargs"] == {"task_name": "issue-triage"}
    assert captured["schema"]["properties"]["intent"]["enum"] == [
        "code_change", "conversation",
    ]
    assert "playbook" in captured["schema"]["required"]
    assert "exit_criteria" in captured["schema"]["required"]
    assert "Work read-only" in captured["prompt"]
    assert "Comment by bob" in captured["prompt"]


def test_conversation_issue_posts_answer_without_worktree_or_pr(monkeypatch, tmp_path):
    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''
[[repositories]]
repository = "org/repo"
path = "{repository_path}"
worktree_root = "{tmp_path / 'worktrees'}"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]
    issue = Issue(
        number=22,
        title="What does this service do?",
        body="Give me a short overview.",
        url="https://github.com/org/repo/issues/22",
        author="alice",
        labels=(worker.target.github.ready_label,),
    )
    labels = []
    comments = []
    monkeypatch.setattr(worker.github, "get_issue", lambda _number: issue)
    monkeypatch.setattr(
        worker.github, "labels",
        lambda number, **kwargs: labels.append((number, kwargs)),
    )
    monkeypatch.setattr(
        worker.github, "comment",
        lambda number, body: comments.append((number, body)),
    )
    monkeypatch.setattr(worker, "_triage_issue", lambda _issue: {
        "intent": "conversation",
        "reason": "This is a question.",
        "answer": "It turns reviewed Issues into code changes or read-only answers.",
    })
    monkeypatch.setattr(
        worker, "_prepare_worktree",
        lambda *_args: pytest.fail("conversation must not create a worktree"),
    )
    monkeypatch.setattr(
        worker.github, "create_pr",
        lambda *_args, **_kwargs: pytest.fail("conversation must not create a PR"),
    )

    assert worker.process_issue(issue) == issue.url
    assert labels == [
        (22, {
            "add": (worker.target.github.working_label,),
            "remove": (worker.target.github.ready_label,),
        }),
        (22, {"remove": (worker.target.github.working_label,)}),
    ]
    assert len(comments) == 2
    assert "determining whether it needs a code change" in comments[0][1]
    assert "### Step: Answer question" in comments[1][1]
    assert "read-only Codex run" in comments[1][1]
    assert worker.state.recent(1)[0][3] == "answered"


def test_conversation_triage_requires_an_answer(monkeypatch):
    worker = CTOLoopService(settings()).workers[0]
    issue = Issue(23, "Question", "Why?", "https://example.test/23", "alice", ())
    monkeypatch.setattr(worker, "_run_structured_codex", lambda *_args, **_kwargs: {
        "intent": "conversation", "reason": "A question.", "answer": "",
    })

    with pytest.raises(AgentError, match="returned no answer"):
        worker._triage_issue(issue)


def test_code_change_triage_continues_to_pull_request_flow(monkeypatch, tmp_path):
    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''
[[repositories]]
repository = "org/repo"
path = "{repository_path}"
worktree_root = "{tmp_path / 'worktrees'}"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]
    issue = Issue(
        number=24,
        title="Fix the greeting",
        body="Change the greeting to hello.",
        url="https://github.com/org/repo/issues/24",
        author="alice",
        labels=(worker.target.github.ready_label,),
    )
    calls = []
    monkeypatch.setattr(worker.github, "get_issue", lambda _number: issue)
    monkeypatch.setattr(worker.github, "labels", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.github, "comment", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "_triage_issue", lambda _issue: {
        "intent": "code_change", "reason": "A file must change.", "answer": "",
    })
    monkeypatch.setattr(
        worker, "_prepare_worktree",
        lambda branch, worktree, **_kwargs: calls.append(("prepare", branch, worktree)),
    )
    def fake_run_codex(_issue, _worktree, **kwargs):
        calls.append(("codex", kwargs.get("run_id")))
        return "implementation complete"

    monkeypatch.setattr(worker, "_run_codex", fake_run_codex)
    monkeypatch.setattr(worker, "_changed_paths", lambda _worktree: ("app.py",))
    monkeypatch.setattr(worker, "_validate", lambda _worktree: ())
    monkeypatch.setattr(
        worker, "_commit_and_push",
        lambda *_args: calls.append(("commit",)),
    )
    monkeypatch.setattr(
        worker.github, "create_pr",
        lambda *_args: "https://github.com/org/repo/pull/25",
    )
    monkeypatch.setattr(
        worker, "_remove_worktree",
        lambda worktree: calls.append(("cleanup", worktree)),
    )

    assert worker.process_issue(issue) == "https://github.com/org/repo/pull/25"
    assert calls[0][0] == "prepare"
    assert any(item[0] == "codex" and item[1] > 0 for item in calls)
    assert ("commit",) in calls
    assert calls[-1][0] == "cleanup"


def test_workflow_uses_role_specific_models(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('''
[workflow]
enabled = true
implementation_model = "builder"
verifier_model = "checker"

[[repositories]]
repository = "org/repo"
path = "/srv/repo"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]
    assert isinstance(worker.execution_backend, CodexBackend)
    assert worker.execution_backend.config.model == "builder"
    assert worker.execution_backend.config_overrides == ("mcp_servers={}",)
    assert worker.verifier_backend.config.model == "checker"
    assert worker.verifier_backend.config.sandbox == "read-only"
    assert worker.verifier_backend.config.approval_policy == "never"
    assert "mcp_servers={}" in worker.verifier_backend.config_overrides


def test_independent_workflow_verifier_records_structured_result(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('''
[workflow]
enabled = true

[[repositories]]
repository = "org/repo"
path = "/srv/repo"
''')
    service = CTOLoopService(load_settings(config))
    worker = service.workers[0]
    issue = Issue(
        number=31, title="Fix retry", body="Retry once after a timeout.",
        url="https://github.com/org/repo/issues/31", author="alice", labels=(),
    )
    plan = WorkflowPlan("bug_fix", "Regression", ("Retry test passes",))
    run_id = service.state.start("org/repo", 31, "codex/issue-31")
    captured = {}

    def fake_verify(prompt, schema, **kwargs):
        captured.update(prompt=prompt, schema=schema, kwargs=kwargs)
        return ExecutionResult(
            "audit stream", "codex", "codex", "workflow-verification",
            structured={
                "verdict": "pass",
                "summary": "Retry behavior is covered.",
                "criteria": [{
                    "criterion": "Retry test passes",
                    "status": "satisfied",
                    "evidence": "retry test passed",
                }],
                "findings": [],
            },
        )

    monkeypatch.setattr(worker.verifier_backend, "run_structured", fake_verify)
    monkeypatch.setattr(worker, "_changed_paths", lambda _path: ("retry.py",))
    payload = worker._verify_workflow(
        issue, tmp_path, plan, (ValidationResult("tests", "1 passed"),),
        run_id=run_id, iteration=1,
    )

    assert payload["verdict"] == "pass"
    assert captured["kwargs"]["read_only"] is True
    assert "You did not author this" in captured["prompt"]
    assert service.state.recent_executions(1)[0][2] == "workflow-verification"


def test_independent_verifier_cannot_pass_inconclusive_criteria(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('''
[workflow]
enabled = true

[[repositories]]
repository = "org/repo"
path = "/srv/repo"
''')
    service = CTOLoopService(load_settings(config))
    worker = service.workers[0]
    issue = Issue(32, "Add feature", "Do it", "url", "alice", ())
    plan = WorkflowPlan("feature", "Feature", ("Behavior works",))
    run_id = service.state.start("org/repo", 32, "codex/issue-32")
    monkeypatch.setattr(worker, "_changed_paths", lambda _path: ("feature.py",))
    monkeypatch.setattr(worker.verifier_backend, "run_structured", lambda *args, **kwargs: ExecutionResult(
        "", "codex", "codex", "workflow-verification",
        structured={
            "verdict": "pass", "summary": "Looks good", "findings": [],
            "criteria": [{
                "criterion": "Behavior works", "status": "inconclusive", "evidence": "",
            }],
        },
    ))

    with pytest.raises(AgentError, match="without satisfying every exit criterion"):
        worker._verify_workflow(
            issue, tmp_path, plan, (), run_id=run_id, iteration=1,
        )


def test_workflow_retries_with_verifier_evidence_then_opens_pr(monkeypatch, tmp_path):
    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''
[workflow]
enabled = true
independent_verifier = true
max_iterations = 2

[[repositories]]
repository = "org/repo"
path = "{repository_path}"
worktree_root = "{tmp_path / 'worktrees'}"
''')
    service = CTOLoopService(load_settings(config))
    worker = service.workers[0]
    issue = Issue(
        41, "Fix retry", "Retry after timeout", "https://example.test/41",
        "alice", (worker.target.github.ready_label,),
    )
    implementation_calls = []
    verifier_calls = []
    monkeypatch.setattr(worker.github, "get_issue", lambda _number: issue)
    monkeypatch.setattr(worker.github, "labels", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.github, "comment", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "_triage_issue", lambda _issue: {
        "intent": "code_change", "reason": "Regression", "answer": "",
        "playbook": "bug_fix", "exit_criteria": ["Retry test passes"],
    })
    monkeypatch.setattr(worker, "_prepare_worktree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "_remove_worktree", lambda *_args: None)
    monkeypatch.setattr(worker, "_changed_paths", lambda _path: ("retry.py",))
    monkeypatch.setattr(
        worker, "_validate", lambda _path: (ValidationResult("tests", "passed"),),
    )

    def fake_implementation(_issue, _worktree, **kwargs):
        implementation_calls.append(kwargs)
        return f"iteration {kwargs['iteration']}"

    def fake_verifier(*_args, **kwargs):
        verifier_calls.append(kwargs["iteration"])
        if kwargs["iteration"] == 1:
            return {
                "verdict": "needs_changes", "summary": "Missing timeout assertion",
                "findings": ["Add a timeout assertion"], "criteria": [],
            }
        return {
            "verdict": "pass", "summary": "Verified", "findings": [],
            "criteria": [{
                "criterion": "Retry test passes", "status": "satisfied",
                "evidence": "tests passed",
            }],
        }

    monkeypatch.setattr(worker, "_run_codex", fake_implementation)
    monkeypatch.setattr(worker, "_verify_workflow", fake_verifier)
    monkeypatch.setattr(worker, "_commit_and_push", lambda *_args: None)
    monkeypatch.setattr(
        worker.github, "create_pr", lambda *_args: "https://github.com/org/repo/pull/42",
    )

    assert worker.process_issue(issue) == "https://github.com/org/repo/pull/42"
    assert verifier_calls == [1, 2]
    assert implementation_calls[0]["verifier_feedback"] == ""
    assert implementation_calls[1]["verifier_feedback"] == "Add a timeout assertion"
    run_id = service.state.connection.execute(
        "SELECT id FROM runs WHERE issue_number=41"
    ).fetchone()[0]
    assert [row[4] for row in service.state.decisions(run_id) if row[0] == "verification"] == [
        "needs_changes", "pass",
    ]
    lifecycle = service.durable.get("pr:org/repo:42")
    assert lifecycle is not None
    assert lifecycle.state == "awaiting_ci"


def test_durable_pr_lifecycle_wakes_on_events_until_merged(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('''
[workflow]
enabled = true

[[repositories]]
repository = "org/repo"
path = "/srv/repo"
''')
    service = CTOLoopService(load_settings(config))
    worker = service.workers[0]
    states = iter((
        PullRequestLifecycle("OPEN", "sha-1", "BLOCKED", "pending", "pending", False),
        PullRequestLifecycle("OPEN", "sha-1", "CLEAN", "success", "approved", False),
        PullRequestLifecycle("MERGED", "sha-1", "UNKNOWN", "success", "approved", False),
    ))
    monkeypatch.setattr(worker.github, "pr_lifecycle", lambda _number: next(states))
    service.durable.enqueue(
        "pr:org/repo:7", repository="org/repo", issue_number=6,
        kind="pr-lifecycle", state="awaiting_ci",
        payload={"pr_number": 7, "pr_url": "https://github.com/org/repo/pull/7"},
    )

    assert service.run_durable_once() == 1
    assert service.durable.get("pr:org/repo:7").state == "awaiting_ci"
    service.durable.notify(
        event_key="webhook:checks", workflow_id="pr:org/repo:7",
        event_type="github-pr", resource_id="org/repo#7", payload={"changed": True},
    )
    assert service.run_durable_once() == 1
    assert service.durable.get("pr:org/repo:7").state == "merge_ready"
    service.durable.notify(
        event_key="webhook:merged", workflow_id="pr:org/repo:7",
        event_type="github-pr", resource_id="org/repo#7", payload={"merged": True},
    )
    assert service.run_durable_once() == 1
    final = service.durable.get("pr:org/repo:7")
    assert final.status == "completed"
    assert final.state == "completed"


def test_ready_issue_is_durably_enqueued_before_execution(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('''
[workflow]
enabled = true

[[repositories]]
repository = "org/repo"
path = "/srv/repo"
''')
    service = CTOLoopService(load_settings(config))
    worker = service.workers[0]
    issue = Issue(
        51, "Durable change", "Change it", "https://example.test/51",
        "alice", (worker.target.github.ready_label,),
    )
    calls = []
    monkeypatch.setattr(worker, "sync_production_updates", lambda: 0)
    monkeypatch.setattr(worker.github, "list_ready", lambda: [issue])
    monkeypatch.setattr(worker.github, "get_issue", lambda _number: issue)
    monkeypatch.setattr(worker.github, "find_pr_by_head", lambda _branch: None)
    monkeypatch.setattr(
        worker, "process_issue",
        lambda _issue, **kwargs: calls.append(kwargs) or issue.url,
    )

    assert service.run_once() == 1
    queued = service.durable.get("issue:org/repo:51")
    assert queued.status == "pending"
    assert calls == []
    assert service.run_durable_once(limit=1) == 1
    assert calls == [{"allow_claimed": True}]
    assert service.durable.get("issue:org/repo:51").status == "completed"


def test_push_checkpoint_recovers_pr_after_daemon_crash(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''
[workflow]
enabled = true

[[repositories]]
repository = "org/repo"
path = "{repository}"
''')
    service = CTOLoopService(load_settings(config))
    worker = service.workers[0]
    issue = Issue(52, "Recover push", "Fix", "issue-url", "alice", ())
    labels = []
    finds = iter((None, None))
    monkeypatch.setattr(worker.github, "get_issue", lambda _number: issue)
    monkeypatch.setattr(worker.github, "find_pr_by_head", lambda _branch: next(finds))
    monkeypatch.setattr(
        "ctoloop.service.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="expected-sha\trefs/heads/codex/issue-52-recover-push\n"
        ),
    )
    monkeypatch.setattr(
        worker.github, "create_pr",
        lambda *_args, **_kwargs: "https://github.com/org/repo/pull/53",
    )
    monkeypatch.setattr(
        worker.github, "labels", lambda *args, **kwargs: labels.append((args, kwargs)),
    )
    service.durable.enqueue(
        "issue:org/repo:52", repository="org/repo", issue_number=52,
        kind="issue-delivery", state="committed",
        payload={
            "branch": "codex/issue-52-recover-push",
            "commit_sha": "expected-sha", "delivery_phase": "committed",
            "run_id": 0, "playbook": "maintenance", "exit_criteria": [],
        },
    )

    assert service.run_durable_once(limit=1) == 1
    recovered = service.durable.get("issue:org/repo:52")
    assert recovered.status == "completed"
    assert service.durable.get("pr:org/repo:53") is not None
    assert labels


def test_real_git_checkpoint_push_recovery(monkeypatch, tmp_path):
    remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    subprocess.run(
        ["git", "checkout", "-b", "main"], cwd=repository,
        check=True, capture_output=True,
    )
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com")):
        subprocess.run(
            ["git", "config", key, value], cwd=repository, check=True, capture_output=True,
        )
    (repository / "app.txt").write_text("base\n")
    subprocess.run(["git", "add", "app.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repository, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repository, check=True, capture_output=True)
    branch = "codex/issue-60-real-recovery"
    subprocess.run(["git", "checkout", "-b", branch], cwd=repository, check=True, capture_output=True)
    (repository / "app.txt").write_text("recovered\n")
    subprocess.run(["git", "commit", "-am", "candidate"], cwd=repository, check=True, capture_output=True)
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "main"], cwd=repository, check=True, capture_output=True)

    config = tmp_path / "config.toml"
    config.write_text(f'''
[workflow]
enabled = true

[[repositories]]
repository = "org/repo"
path = "{repository}"
''')
    service = CTOLoopService(load_settings(config))
    worker = service.workers[0]
    issue = Issue(60, "Real recovery", "Recover", "issue-url", "alice", ())
    monkeypatch.setattr(worker.github, "get_issue", lambda _number: issue)
    monkeypatch.setattr(worker.github, "find_pr_by_head", lambda _branch: None)
    monkeypatch.setattr(
        worker.github, "create_pr",
        lambda *_args, **_kwargs: "https://github.com/org/repo/pull/61",
    )
    monkeypatch.setattr(worker.github, "labels", lambda *_args, **_kwargs: None)
    service.durable.enqueue(
        "issue:org/repo:60", repository="org/repo", issue_number=60,
        kind="issue-delivery", state="committed",
        payload={
            "branch": branch, "commit_sha": expected_sha,
            "delivery_phase": "committed", "run_id": 0,
            "playbook": "maintenance", "exit_criteria": [],
        },
    )

    assert service.run_durable_once(limit=1) == 1
    remote_sha = subprocess.run(
        ["git", "ls-remote", str(remote), f"refs/heads/{branch}"],
        check=True, capture_output=True, text=True,
    ).stdout.split()[0]
    assert remote_sha == expected_sha
    assert service.durable.get("issue:org/repo:60").status == "completed"


def test_lost_issue_lease_stops_before_push(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(f'''
[workflow]
enabled = true

[[repositories]]
repository = "org/repo"
path = "{tmp_path}"
''')
    service = CTOLoopService(load_settings(config))
    worker = service.workers[0]
    issue = Issue(62, "Fence push", "Fix", "issue-url", "alice", ())
    service.durable.enqueue(
        "issue:org/repo:62", repository="org/repo", issue_number=62,
        kind="issue-delivery", state="queued", payload={},
    )
    commands = []

    def fake_run(argv, **_kwargs):
        commands.append(tuple(argv))
        return SimpleNamespace(stdout="candidate-sha\n", returncode=0)

    monkeypatch.setattr("ctoloop.service.run", fake_run)

    with pytest.raises(LeaseLost):
        worker._commit_and_push(issue, "codex/issue-62-fence-push", tmp_path)

    assert not any("push" in command for command in commands)


def test_lost_lease_after_pr_creation_does_not_report_failure(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''
[workflow]
enabled = true
independent_verifier = false

[[repositories]]
repository = "org/repo"
path = "{repository}"
worktree_root = "{tmp_path / 'worktrees'}"
''')
    service = CTOLoopService(load_settings(config))
    worker = service.workers[0]
    issue = Issue(63, "Fence PR", "Fix", "issue-url", "alice", ())
    labels = []
    comments = []
    cleanups = []
    checkpoints = []
    original_checkpoint = worker._durable_checkpoint

    monkeypatch.setattr(worker.github, "get_issue", lambda _number: issue)
    monkeypatch.setattr(
        worker.github, "labels",
        lambda *args, **kwargs: labels.append((args, kwargs)),
    )
    monkeypatch.setattr(
        worker.github, "comment",
        lambda _number, body: comments.append(body),
    )
    monkeypatch.setattr(worker, "_triage_issue", lambda _issue: {
        "intent": "code_change", "reason": "A file must change.", "answer": "",
    })
    monkeypatch.setattr(worker, "_prepare_worktree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "_run_codex", lambda *_args, **_kwargs: "implemented")
    monkeypatch.setattr(worker, "_changed_paths", lambda _path: ("app.py",))
    monkeypatch.setattr(worker, "_validate", lambda _path: ())
    monkeypatch.setattr(worker, "_commit_and_push", lambda *_args: "candidate-sha")
    monkeypatch.setattr(
        worker.github, "create_pr",
        lambda *_args: "https://github.com/org/repo/pull/64",
    )
    monkeypatch.setattr(
        worker, "_remove_worktree", lambda path: cleanups.append(path),
    )

    def checkpoint(issue_number, **kwargs):
        checkpoints.append(kwargs["state"])
        if kwargs["state"] == "pr-created":
            raise LeaseLost("lease taken after create_pr")
        return original_checkpoint(issue_number, **kwargs)

    monkeypatch.setattr(worker, "_durable_checkpoint", checkpoint)
    service.durable.enqueue(
        "issue:org/repo:63", repository="org/repo", issue_number=63,
        kind="issue-delivery", state="queued", payload={},
    )

    with pytest.raises(LeaseLost):
        service.durable.run_one(
            lambda _job, _events: worker.process_issue(issue, allow_claimed=True)
        )

    assert "pr-created" in checkpoints
    assert not any(
        worker.target.github.failed_label in kwargs.get("add", ())
        for _args, kwargs in labels
    )
    assert not any("processing failed" in body for body in comments)
    assert cleanups == []


def test_automatic_poll_skips_issue_claimed_after_listing(monkeypatch, caplog):
    worker = CTOLoopService(settings()).workers[0]
    listed_issue = Issue(
        number=17,
        title="Claim race",
        body="",
        url="https://github.com/your-org/your-repo/issues/17",
        author="alice",
        labels=(worker.target.github.ready_label,),
    )
    claimed_issue = Issue(
        number=17,
        title="Claim race",
        body="",
        url="https://github.com/your-org/your-repo/issues/17",
        author="alice",
        labels=(worker.target.github.ready_label, worker.target.github.working_label),
    )
    monkeypatch.setattr(worker, "sync_production_updates", lambda: 0)
    monkeypatch.setattr(worker.github, "list_ready", lambda: [listed_issue])
    monkeypatch.setattr(worker.github, "get_issue", lambda _number: claimed_issue)

    with caplog.at_level("INFO", logger="ctoloop.service"):
        assert worker.run_once() == 0

    assert "issue #17 is already claimed; skipping" in caplog.text


def test_specific_claimed_issue_still_reports_error(monkeypatch):
    worker = CTOLoopService(settings()).workers[0]
    claimed_issue = Issue(
        number=17,
        title="Claimed issue",
        body="",
        url="https://github.com/your-org/your-repo/issues/17",
        author="alice",
        labels=(worker.target.github.working_label,),
    )
    monkeypatch.setattr(worker, "sync_production_updates", lambda: 0)
    monkeypatch.setattr(worker.github, "get_issue", lambda _number: claimed_issue)

    with pytest.raises(AgentError, match="Issue #17 is already claimed"):
        worker.run_once(17)


def test_specific_issue_requires_ready_label(monkeypatch):
    worker = CTOLoopService(settings()).workers[0]
    issue = Issue(
        number=18,
        title="Unreviewed issue",
        body="",
        url="https://github.com/your-org/your-repo/issues/18",
        author="alice",
        labels=(),
    )
    monkeypatch.setattr(worker, "sync_production_updates", lambda: 0)
    monkeypatch.setattr(worker.github, "get_issue", lambda _number: issue)

    with pytest.raises(AgentError, match="required 'agent:ready' label"):
        worker.run_once(18)


def test_prepare_worktree_removes_stale_local_branch(monkeypatch, tmp_path):
    repository_path = tmp_path / "repository"
    worktree_root = tmp_path / "worktrees"
    repository_path.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''
[[repositories]]
repository = "org/repo"
path = "{repository_path}"
worktree_root = "{worktree_root}"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        stdout = "* codex/issue-30-fix\n" if tuple(argv) == (
            "git", "branch", "--list", "codex/issue-30-fix",
        ) else ""
        return SimpleNamespace(stdout=stdout, returncode=0)

    monkeypatch.setattr("ctoloop.service.run", fake_run)
    worker._prepare_worktree("codex/issue-30-fix", worktree_root / "issue-30")

    prune_index = calls.index(("git", "worktree", "prune"))
    delete_index = calls.index(("git", "branch", "-D", "codex/issue-30-fix"))
    add_index = calls.index((
        "git", "worktree", "add", "-b", "codex/issue-30-fix",
        str(worktree_root / "issue-30"), "origin/main",
    ))
    assert prune_index < delete_index < add_index


def test_doctor_reports_blocked_codex_user_namespace(monkeypatch):
    service = CTOLoopService(settings())
    real_exists = Path.exists
    real_read_text = Path.read_text

    def fake_exists(path):
        if str(path) == "/proc/sys/kernel/apparmor_restrict_unprivileged_userns":
            return True
        return real_exists(path)

    def fake_read_text(path, *args, **kwargs):
        if str(path) == "/proc/sys/kernel/apparmor_restrict_unprivileged_userns":
            return "1\n"
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(service.workers[0], "doctor", lambda: [])

    assert any("ExecStartPre" in failure for failure in service.doctor())


def test_production_update_requires_clean_expected_branch(monkeypatch, tmp_path):
    production_path = tmp_path / "production"
    (production_path / ".git").mkdir(parents=True)
    config = tmp_path / "config.toml"
    config.write_text(f'''
[[repositories]]
repository = "org/repo"
path = "/srv/repo"
production_path = "{production_path}"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]

    def fake_run(argv, **_kwargs):
        outputs = {
            ("git", "remote", "get-url", "origin"): "git@github.com:org/repo.git\n",
            ("git", "branch", "--show-current"): "main\n",
            ("git", "status", "--porcelain", "--untracked-files=no"): " M api/app.py\n",
        }
        return SimpleNamespace(stdout=outputs.get(tuple(argv), ""))

    monkeypatch.setattr("ctoloop.service.run", fake_run)
    with pytest.raises(AgentError, match="tracked local changes"):
        worker._update_production_checkout(production_path)


def test_production_update_fast_forwards_from_origin(monkeypatch, tmp_path):
    production_path = tmp_path / "production"
    (production_path / ".git").mkdir(parents=True)
    config = tmp_path / "config.toml"
    config.write_text(f'''
[[repositories]]
repository = "org/repo"
path = "/srv/repo"
production_path = "{production_path}"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        outputs = {
            ("git", "remote", "get-url", "origin"): "git@github.com:org/repo.git\n",
            ("git", "branch", "--show-current"): "main\n",
            ("git", "status", "--porcelain", "--untracked-files=no"): "",
            ("git", "rev-parse", "HEAD"): "abc123\n",
        }
        return SimpleNamespace(stdout=outputs.get(tuple(argv), ""))

    monkeypatch.setattr("ctoloop.service.run", fake_run)
    assert worker._update_production_checkout(production_path) == "abc123"
    assert any(call[-3:] == ("fetch", "origin", "main") for call in calls)
    assert ("git", "merge", "--ff-only", "origin/main") in calls


def test_copy_tracked_files_preserves_runtime_files_and_removes_deleted_files(
    monkeypatch, tmp_path,
):
    checkout_path = tmp_path / "checkout"
    production_path = tmp_path / "production"
    (checkout_path / "app").mkdir(parents=True)
    production_path.mkdir()
    (checkout_path / "app" / "page.ts").write_text("new")
    (production_path / "app").mkdir()
    (production_path / "app" / "page.ts").write_text("old")
    (production_path / "removed.txt").write_text("remove me")
    (production_path / ".env.local").write_text("keep me")
    service = CTOLoopService(settings())
    worker = service.workers[0]

    def fake_run(argv, **_kwargs):
        if tuple(argv) == ("git", "ls-files", "-z"):
            return SimpleNamespace(stdout="app/page.ts\0")
        if argv[:3] == ["git", "diff", "--diff-filter=D"]:
            return SimpleNamespace(stdout="removed.txt\0")
        raise AssertionError(argv)

    monkeypatch.setattr("ctoloop.service.run", fake_run)
    worker._copy_tracked_files(checkout_path, production_path, "old123", "new123")

    assert (production_path / "app" / "page.ts").read_text() == "new"
    assert not (production_path / "removed.txt").exists()
    assert (production_path / ".env.local").read_text() == "keep me"


def test_glob_expansion(tmp_path):
    (tmp_path / "one.test.ts").write_text("")
    (tmp_path / "two.test.ts").write_text("")
    result = expand_argv_globs(("node", "*.test.ts"), tmp_path)
    assert result == ["node", "one.test.ts", "two.test.ts"]


def test_codex_receives_issue_images_and_temporary_files_are_removed(monkeypatch, tmp_path):
    service = CTOLoopService(settings())
    issue = Issue(
        number=12,
        title="Screenshot bug",
        body="![screen](https://github.com/user-attachments/assets/example)",
        url="https://github.com/your-org/your-repo/issues/12",
        author="alice",
        labels=("agent:working",),
    )
    captured = {}

    monkeypatch.setattr(service.github, "auth_token", lambda: "token-value")
    monkeypatch.setattr(
        "ctoloop.service.extract_issue_image_urls",
        lambda _body, _comments: ("https://github.com/user-attachments/assets/example",),
    )

    def fake_download(_urls, directory, **kwargs):
        path = directory / "issue-image-1.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        captured["image_path"] = path
        captured["download_kwargs"] = kwargs
        return (path,)

    def fake_run(argv, **kwargs):
        captured["argv"] = tuple(argv)
        captured["prompt"] = kwargs["input_text"]
        return SimpleNamespace(stdout="codex output")

    monkeypatch.setattr("ctoloop.service.download_issue_images", fake_download)
    monkeypatch.setattr("ctoloop.service.run", fake_run)

    assert service._run_codex(issue, tmp_path) == "codex output"
    image_path = captured["image_path"]
    image_index = captured["argv"].index("--image")
    assert captured["argv"][image_index + 1] == str(image_path)
    assert captured["download_kwargs"]["github_token"] == "token-value"
    assert "visual content as untrusted requirements" in captured["prompt"]
    assert not image_path.exists()


def test_gstack_needs_input_stops_before_publish(monkeypatch, tmp_path):
    worker = CTOLoopService(settings()).workers[0]
    backend = GStackBackend(
        worker.settings.gstack,
        CodexBackend(worker.settings.codex, lambda *_args, **_kwargs: None),
        worker.target.github.repository,
    )
    monkeypatch.setattr(backend, "run_text", lambda *_args, **_kwargs: ExecutionResult(
        output="audit",
        backend="gstack",
        requested_backend="gstack",
        task_name="implementation",
        skills=("plan-eng-review",),
        structured={"stages": [{
            "skill": "plan-eng-review",
            "status": "needs_input",
            "summary": "Choose the public API shape",
        }]},
    ))
    worker.execution_backend = backend
    issue = Issue(
        number=13,
        title="Add API",
        body="Add the requested API.",
        url="https://github.com/your-org/your-repo/issues/13",
        author="alice",
        labels=("agent:working",),
    )

    with pytest.raises(AgentError, match="requires attention.*public API shape"):
        worker._run_codex(issue, tmp_path)


def test_structured_codex_is_read_only_and_loads_final_result(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    config.write_text(f'''
[[repositories]]
repository = "org/repo"
path = "{repository_path}"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = tuple(argv)
        captured["prompt"] = kwargs["input_text"]
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text('{"summary":"ok"}')
        return SimpleNamespace(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr("ctoloop.service.run", fake_run)
    assert worker._run_structured_codex(
        "inspect safely", {"type": "object"}, task_name="test",
    ) == {"summary": "ok"}
    assert captured["argv"][captured["argv"].index("--sandbox") + 1] == "read-only"
    assert captured["argv"][captured["argv"].index("--ask-for-approval") + 1] == "never"
    assert "--output-schema" in captured["argv"]
    assert captured["prompt"] == "inspect safely"


@pytest.mark.parametrize(("task_name", "expected_skills"), (
    ("pr-review", ("review",)),
    ("ci-diagnosis", ("investigate",)),
))
def test_gstack_structured_tasks_use_configured_skills_and_record_audit(
    monkeypatch, task_name, expected_skills,
):
    worker = CTOLoopService(settings()).workers[0]
    backend = GStackBackend(
        worker.settings.gstack,
        CodexBackend(worker.settings.codex, lambda *_args, **_kwargs: None),
        worker.target.github.repository,
    )
    captured = {}

    def fake_structured(_prompt, _schema, **kwargs):
        captured.update(kwargs)
        return ExecutionResult(
            output="audit",
            backend="gstack",
            requested_backend="gstack",
            task_name=task_name,
            skills=tuple(kwargs["skills"]),
            structured={"summary": "ok"},
        )

    monkeypatch.setattr(backend, "run_structured", fake_structured)
    worker.execution_backend = backend

    assert worker._run_structured_codex(
        "inspect", {"type": "object"}, task_name=task_name,
    ) == {"summary": "ok"}
    assert captured["skills"] == expected_skills
    audit = worker.state.recent_executions(1)[0]
    assert audit[2:7] == (
        task_name, "gstack", "gstack", "completed", json.dumps(expected_skills),
    )


def test_review_pr_generates_report_and_only_publishes_when_requested(monkeypatch):
    worker = CTOLoopService(settings()).workers[0]
    pull = PullRequest(
        number=42, title="Fix race", body="details", url="https://example.test/pr/42",
        author="alice", base_ref_name="main", head_ref_name="fix-race",
        head_ref_oid="abc123",
    )
    published = []
    captured = {}
    monkeypatch.setattr(worker.github, "get_pr", lambda _number: pull)
    monkeypatch.setattr(worker.github, "pr_diff", lambda _number: "diff --git a/a.py b/a.py")

    def fake_codex(prompt, schema, **kwargs):
        captured.update(prompt=prompt, schema=schema, kwargs=kwargs)
        return {
            "verdict": "request_changes",
            "summary": "One regression found.",
            "findings": [{
                "severity": "high", "title": "Race remains", "file": "a.py",
                "line": 12, "detail": "The write is not synchronized.",
                "suggestion": "Protect it with the existing lock.",
            }],
        }

    monkeypatch.setattr(worker, "_run_structured_codex", fake_codex)
    monkeypatch.setattr(
        worker.github, "comment_pr", lambda number, body: published.append((number, body)),
    )

    report = worker.review_pr(42)
    assert "request_changes" in report
    assert "`a.py:12`" in report
    assert published == []
    assert "Work read-only" in captured["prompt"]

    worker.review_pr(42, publish=True)
    assert published[0][0] == 42
    assert "One regression found." in published[0][1]
    assert json.loads(worker.review_pr(42, json_output=True))["verdict"] == "request_changes"


def test_diagnose_ci_uses_latest_failed_pr_run_and_can_publish(monkeypatch):
    worker = CTOLoopService(settings()).workers[0]
    pull = PullRequest(
        number=7, title="Change API", body="", url="https://example.test/pr/7",
        author="alice", base_ref_name="main", head_ref_name="api-change",
        head_ref_oid="def456",
    )
    workflow = WorkflowRun(
        database_id=99, name="CI", display_title="Change API", conclusion="failure",
        status="completed", url="https://example.test/runs/99", head_sha="def456",
    )
    published = []
    captured = {}
    monkeypatch.setattr(worker.github, "get_pr", lambda _number: pull)
    monkeypatch.setattr(worker.github, "latest_failed_run", lambda sha: workflow)
    monkeypatch.setattr(worker.github, "failed_run_logs", lambda _run: "FAILED test_api")

    def fake_codex(prompt, _schema, **_kwargs):
        captured["prompt"] = prompt
        return {
            "classification": "test_failure", "summary": "Assertion failed.",
            "root_causes": ["Expected status changed"],
            "evidence": ["FAILED test_api"],
            "recommended_actions": ["Update the implementation, then rerun test_api"],
            "suspected_files": ["api/routes.py"],
        }

    monkeypatch.setattr(worker, "_run_structured_codex", fake_codex)
    monkeypatch.setattr(
        worker.github, "comment_pr", lambda number, body: published.append((number, body)),
    )

    report = worker.diagnose_ci(pr_number=7, publish=True)
    assert "test_failure" in report
    assert "FAILED test_api" in report
    assert "def456" in captured["prompt"]
    assert published == [(7, report)]


def test_diagnose_ci_publish_requires_pr(monkeypatch):
    worker = CTOLoopService(settings()).workers[0]
    workflow = WorkflowRun(
        database_id=99, name="CI", display_title="Build", conclusion="failure",
        status="completed", url="https://example.test/runs/99", head_sha="abc",
    )
    monkeypatch.setattr(worker.github, "get_workflow_run", lambda _run: workflow)
    monkeypatch.setattr(worker.github, "failed_run_logs", lambda _run: "failed")
    monkeypatch.setattr(worker, "_run_structured_codex", lambda *_args, **_kwargs: {
        "classification": "unknown", "summary": "Unknown.", "root_causes": [],
        "evidence": [], "recommended_actions": [], "suspected_files": [],
    })

    with pytest.raises(AgentError, match="--publish requires --pr"):
        worker.diagnose_ci(99, publish=True)


def test_multiple_repository_dispatch_requires_repository_for_specific_issue(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("""
[[repositories]]
repository = "org/one"
path = "/srv/one"
worktree_root = "/tmp/one"

[[repositories]]
repository = "org/two"
path = "/srv/two"
worktree_root = "/tmp/two"
""")
    service = CTOLoopService(load_settings(config))
    calls = []
    monkeypatch.setattr(service.workers[1], "run_once", lambda issue=None: calls.append(issue) or 1)

    with pytest.raises(AgentError, match="requires --repository"):
        service.run_once(7)
    assert service.run_once(7, "org/two") == 1
    assert calls == [7]
