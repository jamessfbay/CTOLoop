import pytest

from ctoloop.workflows import DEFAULT_EXIT_CRITERIA, PLAYBOOKS, build_plan


def test_build_plan_uses_bounded_agent_criteria():
    plan = build_plan({
        "playbook": "bug_fix",
        "reason": "Regression report",
        "exit_criteria": ["Reproduction passes", "Tests pass"],
    }, enabled=True)

    assert plan.playbook == "bug_fix"
    assert plan.reason == "Regression report"
    assert plan.exit_criteria == ("Reproduction passes", "Tests pass")


def test_build_plan_uses_safe_defaults_when_workflow_is_disabled():
    plan = build_plan({"playbook": "unknown", "exit_criteria": []}, enabled=False)
    assert plan.playbook == "maintenance"
    assert plan.exit_criteria == DEFAULT_EXIT_CRITERIA["maintenance"]


def test_build_plan_rejects_unbounded_playbook():
    with pytest.raises(ValueError, match="Unsupported workflow playbook"):
        build_plan({"playbook": "ship_and_deploy"}, enabled=True)


def test_all_playbooks_have_default_exit_criteria():
    assert set(DEFAULT_EXIT_CRITERIA) == set(PLAYBOOKS)
