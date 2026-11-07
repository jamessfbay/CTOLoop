from __future__ import annotations

import dataclasses
import os
import pathlib
import tomllib
from typing import Any


@dataclasses.dataclass(frozen=True)
class GitHubConfig:
    repository: str
    auth_user: str = ""
    token_env: str = ""
    auth_config_dir: pathlib.Path | None = None
    ready_label: str = "agent:ready"
    working_label: str = "agent:working"
    pr_label: str = "agent:pr-open"
    failed_label: str = "agent:failed"
    poll_seconds: int = 60
    max_issues_per_poll: int = 1
    gh_executable: str = "gh"


@dataclasses.dataclass(frozen=True)
class RepositoryConfig:
    path: pathlib.Path
    base_branch: str = "main"
    branch_prefix: str = "codex/issue-"
    worktree_root: pathlib.Path = pathlib.Path("/tmp/ctoloop-worktrees")
    production_path: pathlib.Path | None = None
    production_checkout_path: pathlib.Path | None = None
    git_user_name: str = "CTOLoop"
    git_user_email: str = "ctoloop@localhost"


@dataclasses.dataclass(frozen=True)
class CodexConfig:
    executable: str = "codex"
    model: str = ""
    timeout_seconds: int = 3600
    sandbox: str = "workspace-write"
    approval_policy: str = "never"


GSTACK_SKILLS = frozenset({
    "review", "investigate", "health", "qa", "qa-only", "cso",
    "plan-eng-review", "plan-design-review", "plan-devex-review",
})


@dataclasses.dataclass(frozen=True)
class GStackConfig:
    enabled: bool = False
    skill_root: pathlib.Path = pathlib.Path("~/.codex/skills")
    state_root: pathlib.Path = pathlib.Path("state/gstack")
    fallback_to_codex: bool = True
    review_skills: tuple[str, ...] = ("review",)
    ci_skills: tuple[str, ...] = ("investigate",)
    implementation_skills: tuple[str, ...] = ("review", "health")


@dataclasses.dataclass(frozen=True)
class SecurityAuditConfig:
    enabled: bool = False
    skill_root: pathlib.Path = pathlib.Path("~/.codex/skills/security-audit")
    state_root: pathlib.Path = pathlib.Path("state/security-audits")
    default_profile: str = "standard"
    max_agent_invocations: int = 24
    timeout_seconds: int = 14400
    require_validated_output: bool = True
    issue_label: str = "security:audit"
    execution_policy: str = "static-only"


@dataclasses.dataclass(frozen=True)
class AutomationConfig:
    agent_name: str = "CTOLoop"
    agent_icon: str = "🤖"
    create_draft_pr: bool = False
    enable_auto_merge: bool = False
    merge_method: str = "squash"
    delete_branch_after_merge: bool = True
    allowed_issue_authors: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    max_issue_images: int = 4
    max_issue_image_bytes: int = 15_000_000


@dataclasses.dataclass(frozen=True)
class ValidationCommand:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: int = 1200
    expand_globs: bool = False


@dataclasses.dataclass(frozen=True)
class RepositoryTarget:
    github: GitHubConfig
    repository: RepositoryConfig
    validation: tuple[ValidationCommand, ...]
    production_commands: tuple[ValidationCommand, ...] = ()


@dataclasses.dataclass(frozen=True)
class SchedulerConfig:
    poll_seconds: int = 60
    max_parallel_repositories: int = 2


@dataclasses.dataclass(frozen=True)
class Settings:
    repositories: tuple[RepositoryTarget, ...]
    codex: CodexConfig
    gstack: GStackConfig
    security_audit: SecurityAuditConfig
    automation: AutomationConfig
    scheduler: SchedulerConfig
    config_path: pathlib.Path

    # Keep the original single-repository API available for integrations and tests.
    @property
    def github(self) -> GitHubConfig:
        return self.repositories[0].github

    @property
    def repository(self) -> RepositoryConfig:
        return self.repositories[0].repository

    @property
    def validation(self) -> tuple[ValidationCommand, ...]:
        return self.repositories[0].validation


def _required(section: dict[str, Any], key: str, section_name: str) -> Any:
    value = section.get(key)
    if value in (None, ""):
        raise ValueError(f"Missing required config: [{section_name}].{key}")
    return value


def _validation_commands(raw: list[dict[str, Any]]) -> tuple[ValidationCommand, ...]:
    return tuple(
        ValidationCommand(
            name=str(_required(item, "name", "validation.commands")),
            argv=tuple(str(arg) for arg in _required(item, "argv", "validation.commands")),
            timeout_seconds=max(10, int(item.get("timeout_seconds", 1200))),
            expand_globs=bool(item.get("expand_globs", False)),
        )
        for item in raw
    )


def _gstack_skills(raw: dict[str, Any], key: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    skills = tuple(str(item).strip() for item in raw.get(key, defaults))
    invalid = sorted({skill for skill in skills if skill not in GSTACK_SKILLS})
    if invalid:
        raise ValueError(
            f"[gstack].{key} contains unsupported skills: {', '.join(invalid)}"
        )
    if len(skills) != len(set(skills)):
        raise ValueError(f"[gstack].{key} must not contain duplicate skills")
    return skills


def _github_config(
    raw: dict[str, Any], defaults: dict[str, Any], config_root: pathlib.Path,
) -> GitHubConfig:
    merged = {**defaults, **raw}
    auth_config_dir = str(merged.get("auth_config_dir", "")).strip()
    auth_config_path = pathlib.Path(auth_config_dir).expanduser() if auth_config_dir else None
    if auth_config_path and not auth_config_path.is_absolute():
        auth_config_path = config_root / auth_config_path
    return GitHubConfig(
        repository=str(_required(merged, "repository", "repositories")),
        auth_user=str(merged.get("auth_user", "")).strip(),
        token_env=str(merged.get("token_env", "")).strip(),
        auth_config_dir=auth_config_path.resolve() if auth_config_path else None,
        ready_label=str(merged.get("ready_label", "agent:ready")),
        working_label=str(merged.get("working_label", "agent:working")),
        pr_label=str(merged.get("pr_label", "agent:pr-open")),
        failed_label=str(merged.get("failed_label", "agent:failed")),
        poll_seconds=max(10, int(merged.get("poll_seconds", 60))),
        max_issues_per_poll=max(1, int(merged.get("max_issues_per_poll", 1))),
        gh_executable=str(merged.get("gh_executable", "gh")),
    )


def _repository_config(raw: dict[str, Any]) -> RepositoryConfig:
    production_path = str(raw.get("production_path", "")).strip()
    production_checkout_path = str(raw.get("production_checkout_path", "")).strip()
    return RepositoryConfig(
        path=pathlib.Path(_required(raw, "path", "repositories")).expanduser().resolve(),
        base_branch=str(raw.get("base_branch", "main")),
        branch_prefix=str(raw.get("branch_prefix", "codex/issue-")),
        worktree_root=pathlib.Path(raw.get("worktree_root", "/tmp/ctoloop-worktrees")).expanduser().resolve(),
        production_path=pathlib.Path(production_path).expanduser().resolve() if production_path else None,
        production_checkout_path=(
            pathlib.Path(production_checkout_path).expanduser().resolve()
            if production_checkout_path else None
        ),
        git_user_name=str(raw.get("git_user_name", "CTOLoop")),
        git_user_email=str(raw.get("git_user_email", "ctoloop@localhost")),
    )


def load_settings(path: str | os.PathLike[str]) -> Settings:
    config_path = pathlib.Path(path).expanduser().resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    github_raw = raw.get("github", {})
    codex_raw = raw.get("codex", {})
    gstack_raw = raw.get("gstack", {})
    security_audit_raw = raw.get("security_audit", {})
    auto_raw = raw.get("automation", {})
    scheduler_raw = raw.get("scheduler", {})
    codex = CodexConfig(
        executable=str(codex_raw.get("executable", "codex")),
        model=str(codex_raw.get("model", "")),
        timeout_seconds=max(60, int(codex_raw.get("timeout_seconds", 3600))),
        sandbox=str(codex_raw.get("sandbox", "workspace-write")),
        approval_policy=str(codex_raw.get("approval_policy", "never")),
    )
    skill_root = pathlib.Path(
        gstack_raw.get("skill_root", "~/.codex/skills")
    ).expanduser()
    if not skill_root.is_absolute():
        skill_root = config_path.parent / skill_root
    state_root = pathlib.Path(gstack_raw.get("state_root", "state/gstack")).expanduser()
    if not state_root.is_absolute():
        state_root = config_path.parent / state_root
    gstack = GStackConfig(
        enabled=bool(gstack_raw.get("enabled", False)),
        skill_root=skill_root.resolve(),
        state_root=state_root.resolve(),
        fallback_to_codex=bool(gstack_raw.get("fallback_to_codex", True)),
        review_skills=_gstack_skills(gstack_raw, "review_skills", ("review",)),
        ci_skills=_gstack_skills(gstack_raw, "ci_skills", ("investigate",)),
        implementation_skills=_gstack_skills(
            gstack_raw, "implementation_skills", ("review", "health"),
        ),
    )
    security_skill_root = pathlib.Path(
        security_audit_raw.get("skill_root", "~/.codex/skills/security-audit")
    ).expanduser()
    if not security_skill_root.is_absolute():
        security_skill_root = config_path.parent / security_skill_root
    security_state_root = pathlib.Path(
        security_audit_raw.get("state_root", "state/security-audits")
    ).expanduser()
    if not security_state_root.is_absolute():
        security_state_root = config_path.parent / security_state_root
    security_profile = str(
        security_audit_raw.get("default_profile", "standard")
    ).strip()
    if security_profile not in {"quick", "standard", "deep"}:
        raise ValueError(
            "[security_audit].default_profile must be quick, standard, or deep"
        )
    execution_policy = str(
        security_audit_raw.get("execution_policy", "static-only")
    ).strip()
    if execution_policy != "static-only":
        raise ValueError(
            "[security_audit].execution_policy currently supports only static-only"
        )
    security_audit = SecurityAuditConfig(
        enabled=bool(security_audit_raw.get("enabled", False)),
        skill_root=security_skill_root.resolve(),
        state_root=security_state_root.resolve(),
        default_profile=security_profile,
        max_agent_invocations=max(
            6, int(security_audit_raw.get("max_agent_invocations", 24))
        ),
        timeout_seconds=max(
            300, int(security_audit_raw.get("timeout_seconds", 14400))
        ),
        require_validated_output=bool(
            security_audit_raw.get("require_validated_output", True)
        ),
        issue_label=(
            str(security_audit_raw.get("issue_label", "security:audit")).strip()
            or "security:audit"
        ),
        execution_policy=execution_policy,
    )
    automation = AutomationConfig(
        agent_name=str(auto_raw.get("agent_name", "CTOLoop")).strip() or "CTOLoop",
        agent_icon=str(auto_raw.get("agent_icon", "🤖")).strip(),
        create_draft_pr=bool(auto_raw.get("create_draft_pr", False)),
        enable_auto_merge=bool(auto_raw.get("enable_auto_merge", False)),
        merge_method=str(auto_raw.get("merge_method", "squash")),
        delete_branch_after_merge=bool(auto_raw.get("delete_branch_after_merge", True)),
        allowed_issue_authors=tuple(str(item) for item in auto_raw.get("allowed_issue_authors", [])),
        protected_paths=tuple(str(item) for item in auto_raw.get("protected_paths", [])),
        max_issue_images=max(0, int(auto_raw.get("max_issue_images", 4))),
        max_issue_image_bytes=max(1, int(auto_raw.get("max_issue_image_bytes", 15_000_000))),
    )
    targets: list[RepositoryTarget] = []
    repositories_raw = raw.get("repositories", [])
    if repositories_raw:
        if not isinstance(repositories_raw, list):
            raise ValueError("[[repositories]] must be an array of tables")
        for item in repositories_raw:
            item = dict(item)
            github_overrides = dict(item.pop("github", {}))
            github_overrides["repository"] = item.pop("repository", github_overrides.get("repository", ""))
            validation_raw = item.pop("validation", [])
            production_commands_raw = item.pop("production_commands", [])
            targets.append(RepositoryTarget(
                github=_github_config(github_overrides, github_raw, config_path.parent),
                repository=_repository_config(item),
                validation=_validation_commands(validation_raw),
                production_commands=_validation_commands(production_commands_raw),
            ))
    else:
        repo_raw = raw.get("repository", {})
        validation_raw = raw.get("validation", {}).get("commands", [])
        production_commands_raw = raw.get("production", {}).get("commands", [])
        targets.append(RepositoryTarget(
            github=_github_config(github_raw, {}, config_path.parent),
            repository=_repository_config(repo_raw),
            validation=_validation_commands(validation_raw),
            production_commands=_validation_commands(production_commands_raw),
        ))

    names = [target.github.repository for target in targets]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate GitHub repository in [[repositories]]")
    paths = [target.repository.path for target in targets]
    if len(paths) != len(set(paths)):
        raise ValueError("Each [[repositories]] entry must use a distinct local path")
    worktree_roots = [target.repository.worktree_root for target in targets]
    if len(worktree_roots) != len(set(worktree_roots)):
        raise ValueError("Each [[repositories]] entry must use a distinct worktree_root")
    for target in targets:
        repo = target.repository
        if repo.production_checkout_path and not repo.production_path:
            raise ValueError("production_checkout_path requires production_path")
        if (
            repo.production_checkout_path
            and repo.production_path
            and repo.production_checkout_path.is_relative_to(repo.production_path)
        ):
            raise ValueError("production_checkout_path must be outside production_path")

    scheduler = SchedulerConfig(
        poll_seconds=max(10, int(scheduler_raw.get("poll_seconds", github_raw.get("poll_seconds", 60)))),
        max_parallel_repositories=max(1, int(scheduler_raw.get("max_parallel_repositories", 2))),
    )
    return Settings(
        tuple(targets), codex, gstack, security_audit, automation, scheduler,
        config_path,
    )
