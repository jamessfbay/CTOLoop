import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctoloop.backends import BackendUnavailable, CodexBackend, ExecutionResult
from ctoloop.config import CodexConfig, SecurityAuditConfig, load_settings
from ctoloop.github import Issue
from ctoloop.security_audit import REQUIRED_ARTIFACTS, SecurityAuditBackend
from ctoloop.service import AgentError, CTOLoopService


def _skill_tree(root: Path) -> None:
    root.mkdir(parents=True)
    for name in (
        "SKILL.md", "report-schema.json", "validate-findings.cjs",
        "validate-coverage-ledger.cjs",
    ):
        (root / name).write_text("{}")


def test_security_audit_uses_external_writable_root_and_validates(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    skill_root = tmp_path / "skill"
    _skill_tree(skill_root)
    calls = []
    captured = {}

    def fake_run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if "rev-parse" in argv:
            return SimpleNamespace(stdout="abc123\n")
        if argv[0] == "codex":
            output_dir = Path(kwargs["cwd"])
            captured["snapshot"] = (
                output_dir.parent / f"source-{output_dir.name.removeprefix('run-')}"
            ).resolve()
            captured["prompt"] = kwargs["input_text"]
            captured["env"] = kwargs["env"]
            artifacts = {
                "run-metadata.json": json.dumps({
                    "run_status": "complete",
                    "source_ref": "abc123",
                    "profile": "quick",
                    "target": str(captured["snapshot"]),
                }),
                "architecture.md": "# Architecture",
                "coverage-ledger.json": "[]",
                "findings.json": json.dumps([{
                    "verdict": "confirmed",
                    "fingerprint": "auth/bypass",
                    "title": "Authorization bypass",
                    "severity": {"overall_severity": "high"},
                }]),
                "REPORT.md": "# Report\nNo findings.",
                "FINDINGS-DETAIL.md": "# Details",
                "NEEDS-VALIDATION.md": "# Needs validation",
            }
            for name, value in artifacts.items():
                (output_dir / name).write_text(value)
            return SimpleNamespace(stdout="audit stream")
        return SimpleNamespace(stdout="")

    def fake_run_bytes(argv, **kwargs):
        captured["git_env"] = kwargs["env"]
        if "ls-tree" in argv:
            return SimpleNamespace(
                stdout=b"100644 blob 0123456789012345678901234567890123456789 8\tREADME.md\0"
            )
        if "cat-file" in argv:
            return SimpleNamespace(stdout=b"snapshot")
        raise AssertionError(argv)

    monkeypatch.setenv("GH_TOKEN", "secret")
    backend = SecurityAuditBackend(
        SecurityAuditConfig(
            enabled=True,
            skill_root=skill_root,
            state_root=tmp_path / "audit-state",
            default_profile="quick",
            max_agent_invocations=12,
        ),
        CodexBackend(
            CodexConfig(), fake_run,
            (
                "mcp_servers={}",
                "shell_environment_policy.inherit=none",
                "sandbox_workspace_write.network_access=false",
                "sandbox_workspace_write.writable_roots=[]",
            ),
        ),
        "org/repo",
        (),
        fake_run_bytes,
    )

    result = backend.run(target=target, scope_paths=("src",), budget=8)

    assert result.backend == "security-audit"
    assert result.structured["counts"] == {
        "confirmed": 1, "needs_validation": 0, "rejected": 0,
    }
    assert result.structured["findings"] == [{
        "verdict": "confirmed",
        "fingerprint": "auth/bypass",
        "title": "Authorization bypass",
        "severity": "high",
    }]
    output_dir = Path(result.structured["output_dir"])
    assert output_dir.parent == tmp_path / "audit-state" / "org-repo"
    assert output_dir != target
    assert "static-only" in captured["prompt"]
    assert str(captured["snapshot"]) in captured["prompt"]
    assert not captured["snapshot"].exists()
    assert "symlinks are represented as regular files" in captured["prompt"]
    assert "GH_TOKEN" not in captured["env"]
    node_calls = [argv for argv, _kwargs in calls if argv[0] == "node"]
    assert len(node_calls) == 2
    codex_argv = next(argv for argv, _kwargs in calls if argv[0] == "codex")
    assert "mcp_servers={}" in codex_argv
    assert "shell_environment_policy.inherit=none" in codex_argv
    assert "sandbox_workspace_write.network_access=false" in codex_argv
    assert "sandbox_workspace_write.writable_roots=[]" in codex_argv
    assert captured["git_env"]["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_security_audit_rejects_missing_terminal_artifacts(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    skill_root = tmp_path / "skill"
    _skill_tree(skill_root)

    def fake_run(argv, **kwargs):
        if "rev-parse" in argv:
            return SimpleNamespace(stdout="abc123\n")
        return SimpleNamespace(stdout="audit stream")

    def fake_run_bytes(argv, **_kwargs):
        if "ls-tree" in argv:
            return SimpleNamespace(
                stdout=b"100644 blob 0123456789012345678901234567890123456789 8\tREADME.md\0"
            )
        return SimpleNamespace(stdout=b"snapshot")

    backend = SecurityAuditBackend(
        SecurityAuditConfig(
            enabled=True, skill_root=skill_root, state_root=tmp_path / "state",
        ),
        CodexBackend(CodexConfig(), fake_run),
        "org/repo",
        (),
        fake_run_bytes,
    )

    with pytest.raises(BackendUnavailable, match="missing"):
        backend.run(target=target)


def test_security_audit_rejects_explicit_zero_budget(tmp_path):
    skill_root = tmp_path / "skill"
    _skill_tree(skill_root)
    backend = SecurityAuditBackend(
        SecurityAuditConfig(
            enabled=True, skill_root=skill_root, state_root=tmp_path / "state",
        ),
        CodexBackend(CodexConfig(), lambda *_args, **_kwargs: None),
        "org/repo",
    )

    with pytest.raises(ValueError, match="at least 6"):
        backend.run(target=tmp_path, budget=0)


@pytest.mark.parametrize("profile,budget,message", (
    ("unbounded", None, "Unsupported"),
    ("quick", 25, "exceeds configured"),
))
def test_security_audit_rejects_invalid_profile_or_excess_budget(
    tmp_path, profile, budget, message,
):
    skill_root = tmp_path / "skill"
    _skill_tree(skill_root)
    backend = SecurityAuditBackend(
        SecurityAuditConfig(
            enabled=True, skill_root=skill_root, state_root=tmp_path / "state",
            max_agent_invocations=24,
        ),
        CodexBackend(CodexConfig(), lambda *_args, **_kwargs: None),
        "org/repo",
    )

    with pytest.raises(ValueError, match=message):
        backend.run(target=tmp_path, profile=profile, budget=budget)


@pytest.mark.parametrize("overrides,message", (
    ({"run_status": "in_progress"}, "terminal run_status"),
    ({"run_status": "incomplete"}, "incomplete_reason"),
    ({"source_ref": "wrong"}, "source_ref"),
    ({"profile": "deep"}, "profile"),
    ({"target": "/wrong"}, "target"),
))
def test_security_audit_rejects_mismatched_metadata(tmp_path, overrides, message):
    metadata = {
        "run_status": "complete",
        "source_ref": "abc123",
        "profile": "quick",
        "target": str(tmp_path),
        **overrides,
    }

    with pytest.raises(BackendUnavailable, match=message):
        SecurityAuditBackend._validate_metadata(
            metadata, source_ref="abc123", profile="quick", target=tmp_path,
        )


def test_security_audit_safe_snapshot_rejects_parent_traversal(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    snapshot = tmp_path / "snapshot"
    skill_root = tmp_path / "skill"
    _skill_tree(skill_root)

    def fake_run_bytes(argv, **_kwargs):
        assert "ls-tree" in argv
        return SimpleNamespace(
            stdout=b"100644 blob 0123456789012345678901234567890123456789 4\t../escape\0"
        )

    backend = SecurityAuditBackend(
        SecurityAuditConfig(
            enabled=True, skill_root=skill_root, state_root=tmp_path / "state",
        ),
        CodexBackend(CodexConfig(), lambda *_args, **_kwargs: None),
        "org/repo",
        (),
        fake_run_bytes,
    )

    with pytest.raises(BackendUnavailable, match="Unsafe path"):
        backend._create_snapshot(
            target=target, source_ref="abc123", snapshot=snapshot,
        )
    assert not snapshot.exists()
    assert not (tmp_path / "escape").exists()


def test_security_audit_snapshot_rejects_oversized_blob_before_reading(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    snapshot = tmp_path / "snapshot"
    skill_root = tmp_path / "skill"
    _skill_tree(skill_root)
    calls = []

    def fake_run_bytes(argv, **_kwargs):
        calls.append(tuple(argv))
        if "ls-tree" in argv:
            return SimpleNamespace(stdout=(
                b"100644 blob 0123456789012345678901234567890123456789 "
                b"52428801\tlarge.bin\0"
            ))
        raise AssertionError("oversized blobs must be rejected before cat-file")

    backend = SecurityAuditBackend(
        SecurityAuditConfig(
            enabled=True, skill_root=skill_root, state_root=tmp_path / "state",
        ),
        CodexBackend(CodexConfig(), lambda *_args, **_kwargs: None),
        "org/repo",
        (),
        fake_run_bytes,
    )

    with pytest.raises(BackendUnavailable, match="size limit"):
        backend._create_snapshot(
            target=target, source_ref="abc123", snapshot=snapshot,
        )
    assert len(calls) == 1
    assert not snapshot.exists()


def test_security_audit_issue_label_routes_without_implementation(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''
[security_audit]
enabled = true
skill_root = "{tmp_path / 'skill'}"
state_root = "{tmp_path / 'audits'}"
default_profile = "quick"

[[repositories]]
repository = "org/repo"
path = "{repository}"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]
    issue = Issue(
        number=9,
        title="Audit this repository",
        body="",
        url="https://github.com/org/repo/issues/9",
        author="alice",
        labels=("agent:ready", "security:audit"),
    )
    label_calls = []
    comments = []
    monkeypatch.setattr(worker.github, "get_issue", lambda _number: issue)
    monkeypatch.setattr(
        worker.github, "labels",
        lambda number, **kwargs: label_calls.append((number, kwargs)),
    )
    monkeypatch.setattr(
        worker.github, "comment", lambda number, body: comments.append((number, body)),
    )
    monkeypatch.setattr(
        worker, "security_audit", lambda **_kwargs: "validated audit report",
    )
    monkeypatch.setattr(
        worker, "_triage_issue",
        lambda _issue: pytest.fail("security audit events must bypass issue triage"),
    )

    assert worker.process_issue(issue) == issue.url
    assert comments[-1] == (9, "validated audit report")
    assert label_calls[0][1]["add"] == ("agent:working",)
    assert worker.state.recent(1)[0][3] == "security-audit-finished"


def test_required_security_audit_artifact_contract_is_complete():
    assert set(REQUIRED_ARTIFACTS) == {
        "run-metadata.json", "architecture.md", "coverage-ledger.json",
        "findings.json", "REPORT.md", "FINDINGS-DETAIL.md",
        "NEEDS-VALIDATION.md",
    }


def test_security_audit_rejects_symlink_artifact(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("[]")
    artifact = tmp_path / "findings.json"
    artifact.symlink_to(outside)

    with pytest.raises(BackendUnavailable, match="single-link regular file"):
        SecurityAuditBackend._read_json(artifact)


def test_security_audit_github_summary_neutralizes_mentions():
    worker = CTOLoopService(load_settings(
        Path(__file__).parents[1] / "config.example.toml"
    )).workers[0]
    value = worker._safe_markdown_inline("@ops **urgent**\nnext", 100)
    assert "@\u200bops" in value
    assert "\\*\\*urgent\\*\\*" in value
    assert "\n" not in value


def test_incomplete_security_audit_is_persisted_as_incomplete(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(f'''
[[repositories]]
repository = "org/repo"
path = "{tmp_path}"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]
    worker._record_execution(ExecutionResult(
        output="",
        backend="security-audit",
        requested_backend="security-audit",
        task_name="security-audit",
        skills=("security-audit",),
        structured={"status": "incomplete"},
    ))

    assert worker.state.recent_executions(1)[0][5] == "incomplete"


def test_security_audit_service_guards_disabled_publish_and_scope(tmp_path):
    disabled = tmp_path / "disabled.toml"
    disabled.write_text(f'''
[[repositories]]
repository = "org/repo"
path = "{tmp_path}"
''')
    worker = CTOLoopService(load_settings(disabled)).workers[0]
    with pytest.raises(AgentError, match="enabled must be true"):
        worker.security_audit()

    enabled = tmp_path / "enabled.toml"
    enabled.write_text(f'''
[security_audit]
enabled = true
skill_root = "{tmp_path / 'skill'}"
state_root = "{tmp_path / 'audits'}"

[[repositories]]
repository = "org/repo"
path = "{tmp_path}"
''')
    worker = CTOLoopService(load_settings(enabled)).workers[0]
    with pytest.raises(AgentError, match="requires an Issue"):
        worker.security_audit(publish=True)
    with pytest.raises(AgentError, match="repository-relative"):
        worker.security_audit(scope_paths=("../secrets",))


def test_security_audit_issue_failure_sets_failed_state(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''
[security_audit]
enabled = true
skill_root = "{tmp_path / 'skill'}"
state_root = "{tmp_path / 'audits'}"

[[repositories]]
repository = "org/repo"
path = "{repository}"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]
    issue = Issue(
        number=10, title="Audit", body="", url="https://example.test/10",
        author="alice", labels=("agent:ready", "security:audit"),
    )
    labels = []
    comments = []
    monkeypatch.setattr(worker.github, "get_issue", lambda _number: issue)
    monkeypatch.setattr(
        worker.github, "labels", lambda number, **kwargs: labels.append((number, kwargs)),
    )
    monkeypatch.setattr(
        worker.github, "comment", lambda number, body: comments.append((number, body)),
    )
    monkeypatch.setattr(
        worker, "security_audit",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("audit failed")),
    )

    with pytest.raises(RuntimeError, match="audit failed"):
        worker.process_issue(issue)
    assert worker.state.recent(1)[0][3] == "failed"
    assert labels[-1][1] == {
        "add": ("agent:failed",), "remove": ("agent:working",),
    }
    assert "did not produce a validated terminal artifact set" in comments[-1][1]


def test_doctor_reports_missing_repository_without_crashing(tmp_path):
    skill_root = tmp_path / "skill"
    _skill_tree(skill_root)
    config = tmp_path / "config.toml"
    config.write_text(f'''
[security_audit]
enabled = true
skill_root = "{skill_root}"
state_root = "{tmp_path / 'audits'}"

[[repositories]]
repository = "org/repo"
path = "{tmp_path / 'missing'}"
''')
    worker = CTOLoopService(load_settings(config)).workers[0]

    failures = worker.doctor()

    assert any("Not a Git repository" in item for item in failures)
