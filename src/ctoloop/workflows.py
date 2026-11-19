"""Native, platform-independent engineering playbooks for CTOLoop."""

from __future__ import annotations

from dataclasses import dataclass


PLAYBOOKS = (
    "bug_fix",
    "feature",
    "refactor",
    "performance",
    "documentation",
    "maintenance",
)


DEFAULT_EXIT_CRITERIA: dict[str, tuple[str, ...]] = {
    "bug_fix": (
        "The reported failure is covered by a regression check or equivalent evidence.",
        "The configured validation commands pass.",
        "An independent verifier finds no blocking correctness regression.",
    ),
    "feature": (
        "The requested observable behavior is implemented.",
        "The configured validation commands pass.",
        "An independent verifier confirms the change matches the Issue.",
    ),
    "refactor": (
        "Existing observable behavior is preserved.",
        "The configured validation commands pass.",
        "An independent verifier finds no unintended blast radius.",
    ),
    "performance": (
        "The performance claim has reproducible before/after evidence when practical.",
        "The configured validation commands pass.",
        "An independent verifier confirms correctness was preserved.",
    ),
    "documentation": (
        "The documentation matches the current implementation and configuration.",
        "Configured documentation or repository validation passes.",
        "An independent verifier finds no misleading instructions.",
    ),
    "maintenance": (
        "The requested repository maintenance is complete.",
        "The configured validation commands pass.",
        "An independent verifier finds no blocking regression.",
    ),
}


@dataclass(frozen=True)
class WorkflowPlan:
    playbook: str
    reason: str
    exit_criteria: tuple[str, ...]


def build_plan(payload: dict[str, object], *, enabled: bool) -> WorkflowPlan:
    """Parse untrusted routing output into a bounded native workflow plan."""
    playbook = str(payload.get("playbook", "maintenance")).strip()
    if not enabled:
        playbook = "maintenance"
    if playbook not in PLAYBOOKS:
        raise ValueError(f"Unsupported workflow playbook: {playbook!r}")
    def single_line(value: object, limit: int) -> str:
        return " ".join(str(value).replace("\x00", "").split())[:limit]

    reason = single_line(payload.get("reason", ""), 1000)
    raw_criteria = payload.get("exit_criteria", [])
    criteria = tuple(
        single_line(item, 500)
        for item in raw_criteria if isinstance(item, str) and item.strip()
    ) if isinstance(raw_criteria, list) else ()
    if not criteria:
        criteria = DEFAULT_EXIT_CRITERIA[playbook]
    return WorkflowPlan(playbook, reason, criteria[:8])


VERIFICATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["pass", "needs_changes", "needs_input"],
        },
        "summary": {"type": "string"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["satisfied", "unsatisfied", "inconclusive"],
                    },
                    "evidence": {"type": "string"},
                },
                "required": ["criterion", "status", "evidence"],
                "additionalProperties": False,
            },
        },
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "summary", "criteria", "findings"],
    "additionalProperties": False,
}
