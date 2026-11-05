import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctoloop.backends import BackendUnavailable, CodexBackend, GStackBackend
from ctoloop.commands import CommandError
from ctoloop.config import CodexConfig, GStackConfig


def test_gstack_runs_configured_stage_after_codex(tmp_path, monkeypatch):
    skill_root = tmp_path / "skills"
    (skill_root / "gstack-review").mkdir(parents=True)
    (skill_root / "gstack-review" / "SKILL.md").write_text("review instructions")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if "--output-last-message" in argv:
            result_path = Path(argv[argv.index("--output-last-message") + 1])
            result_path.write_text(json.dumps({
                "status": "completed",
                "summary": "reviewed",
                "findings": [],
                "tests": ["unit tests"],
                "changed_files": [],
            }))
        return SimpleNamespace(stdout=f"output-{len(calls)}")

    codex = CodexBackend(CodexConfig(), fake_run)
    monkeypatch.setenv("CUSTOM_REPO_TOKEN", "secret")
    backend = GStackBackend(
        GStackConfig(
            enabled=True,
            skill_root=skill_root,
            state_root=tmp_path / "state",
        ),
        codex,
        "org/repo",
        ("CUSTOM_REPO_TOKEN",),
    )

    result = backend.run_text(
        "implement safely",
        cwd=tmp_path,
        task_name="implementation",
        skills=("review",),
    )

    assert result.backend == "gstack"
    assert result.skills == ("review",)
    assert result.structured == {"stages": [{
        "skill": "review",
        "status": "completed",
        "summary": "reviewed",
        "findings": [],
        "tests": ["unit tests"],
        "changed_files": [],
    }]}
    assert result.fell_back is False
    assert len(calls) == 2
    assert calls[0][1]["input_text"] == "implement safely"
    assert "Use the gstack `review` skill" in calls[1][1]["input_text"]
    assert calls[1][1]["env"]["GSTACK_HOME"].startswith(str(tmp_path / "state"))
    assert "GH_TOKEN" not in calls[1][1]["env"]
    assert "GITHUB_TOKEN" not in calls[1][1]["env"]
    assert "CUSTOM_REPO_TOKEN" not in calls[1][1]["env"]


def test_gstack_falls_back_when_skill_is_missing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("GH_TOKEN", "secret")

    def fake_run(_argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout="plain codex")

    backend = GStackBackend(
        GStackConfig(
            enabled=True,
            skill_root=tmp_path / "missing",
            state_root=tmp_path / "state",
            fallback_to_codex=True,
        ),
        CodexBackend(CodexConfig(), fake_run),
        "org/repo",
    )

    result = backend.run_text(
        "fix it", cwd=tmp_path, task_name="implementation", skills=("review",),
    )

    assert result.output == "plain codex"
    assert result.backend == "codex"
    assert result.requested_backend == "gstack"
    assert result.fell_back is True
    assert "not installed" in result.fallback_reason
    assert len(calls) == 1
    assert "GH_TOKEN" not in calls[0]["env"]


def test_gstack_falls_back_when_isolated_state_cannot_be_created(tmp_path):
    skill_root = tmp_path / "skills"
    (skill_root / "gstack-review").mkdir(parents=True)
    (skill_root / "gstack-review" / "SKILL.md").write_text("review")
    blocked_state = tmp_path / "blocked-state"
    blocked_state.write_text("not a directory")

    backend = GStackBackend(
        GStackConfig(
            enabled=True,
            skill_root=skill_root,
            state_root=blocked_state,
            fallback_to_codex=True,
        ),
        CodexBackend(
            CodexConfig(),
            lambda _argv, **_kwargs: SimpleNamespace(stdout="plain codex"),
        ),
        "org/repo",
    )

    result = backend.run_text(
        "fix it", cwd=tmp_path, task_name="implementation", skills=("review",),
    )

    assert result.output == "plain codex"
    assert result.fell_back is True
    assert "blocked-state" in result.fallback_reason


def test_gstack_structured_run_uses_skill_and_schema(tmp_path):
    skill_root = tmp_path / "skills"
    (skill_root / "gstack-investigate").mkdir(parents=True)
    (skill_root / "gstack-investigate" / "SKILL.md").write_text("investigate")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["prompt"] = kwargs["input_text"]
        captured["env"] = kwargs["env"]
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(json.dumps({"summary": "root cause"}))
        return SimpleNamespace(stdout="audit")

    backend = GStackBackend(
        GStackConfig(
            enabled=True,
            skill_root=skill_root,
            state_root=tmp_path / "state",
        ),
        CodexBackend(CodexConfig(), fake_run),
        "org/repo",
    )
    result = backend.run_structured(
        "diagnose",
        {"type": "object"},
        cwd=tmp_path,
        task_name="ci-diagnosis",
        skills=("investigate",),
    )

    assert result.structured == {"summary": "root cause"}
    assert result.backend == "gstack"
    assert result.skills == ("investigate",)
    assert "gstack `investigate`" in captured["prompt"]
    assert captured["env"]["OPENCLAW_SESSION"].startswith("ctoloop-")


@pytest.mark.parametrize("result_content", (None, "not-json", "[]"))
def test_codex_structured_run_rejects_invalid_results(tmp_path, result_content):
    def fake_run(argv, **_kwargs):
        if result_content is not None:
            result_path = Path(argv[argv.index("--output-last-message") + 1])
            result_path.write_text(result_content)
        return SimpleNamespace(stdout="audit")

    backend = CodexBackend(CodexConfig(), fake_run)
    with pytest.raises(BackendUnavailable):
        backend.run_structured(
            "inspect", {"type": "object"}, cwd=tmp_path, task_name="review",
        )


def test_gstack_runs_multiple_stages_in_configured_order(tmp_path):
    skill_root = tmp_path / "skills"
    for skill in ("review", "health"):
        (skill_root / f"gstack-{skill}").mkdir(parents=True)
        (skill_root / f"gstack-{skill}" / "SKILL.md").write_text(skill)
    prompts = []

    def fake_run(argv, **kwargs):
        prompts.append(kwargs["input_text"])
        if "--output-last-message" in argv:
            result_path = Path(argv[argv.index("--output-last-message") + 1])
            result_path.write_text(json.dumps({
                "status": "completed", "summary": "ok", "findings": [],
                "tests": [], "changed_files": [],
            }))
        return SimpleNamespace(stdout="audit")

    backend = GStackBackend(
        GStackConfig(enabled=True, skill_root=skill_root, state_root=tmp_path / "state"),
        CodexBackend(CodexConfig(), fake_run),
        "org/repo",
    )
    result = backend.run_text(
        "implement", cwd=tmp_path, task_name="implementation",
        skills=("review", "health"),
    )

    assert result.skills == ("review", "health")
    assert "gstack `review`" in prompts[1]
    assert "gstack `health`" in prompts[2]
    assert [item["skill"] for item in result.structured["stages"]] == [
        "review", "health",
    ]


def test_gstack_returns_completed_stages_when_later_stage_fails(tmp_path):
    skill_root = tmp_path / "skills"
    for skill in ("review", "health"):
        (skill_root / f"gstack-{skill}").mkdir(parents=True)
        (skill_root / f"gstack-{skill}" / "SKILL.md").write_text(skill)
    calls = 0

    def fake_run(argv, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise CommandError(argv, 1, "health failed")
        if "--output-last-message" in argv:
            result_path = Path(argv[argv.index("--output-last-message") + 1])
            result_path.write_text(json.dumps({
                "status": "completed", "summary": "ok", "findings": [],
                "tests": [], "changed_files": [],
            }))
        return SimpleNamespace(stdout="audit")

    backend = GStackBackend(
        GStackConfig(enabled=True, skill_root=skill_root, state_root=tmp_path / "state"),
        CodexBackend(CodexConfig(), fake_run),
        "org/repo",
    )
    result = backend.run_text(
        "implement", cwd=tmp_path, task_name="implementation",
        skills=("review", "health"),
    )

    assert result.fell_back is True
    assert result.skills == ("review",)
    assert result.structured["stages"][0]["skill"] == "review"
    assert "health failed" in result.fallback_reason


def test_gstack_missing_skill_raises_when_fallback_is_disabled(tmp_path):
    backend = GStackBackend(
        GStackConfig(
            enabled=True, skill_root=tmp_path / "missing",
            state_root=tmp_path / "state", fallback_to_codex=False,
        ),
        CodexBackend(CodexConfig(), lambda *_args, **_kwargs: None),
        "org/repo",
    )

    with pytest.raises(BackendUnavailable, match="not installed"):
        backend.run_text(
            "implement", cwd=tmp_path, task_name="implementation", skills=("review",),
        )


def test_gstack_structured_missing_skill_falls_back_with_sanitized_env(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(prompt=kwargs["input_text"], env=kwargs["env"])
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text('{"summary":"plain"}')
        return SimpleNamespace(stdout="audit")

    backend = GStackBackend(
        GStackConfig(
            enabled=True, skill_root=tmp_path / "missing", state_root=tmp_path / "state",
        ),
        CodexBackend(CodexConfig(), fake_run),
        "org/repo",
    )
    result = backend.run_structured(
        "diagnose", {"type": "object"}, cwd=tmp_path,
        task_name="ci-diagnosis", skills=("investigate",),
    )

    assert result.fell_back is True
    assert result.structured == {"summary": "plain"}
    assert captured["prompt"] == "diagnose"
    assert "GITHUB_TOKEN" not in captured["env"]
