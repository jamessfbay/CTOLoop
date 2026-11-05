from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def _write_executable(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("arguments", "expected_suffix"),
    (
        ((), "update"),
        (("your-org/your-repo",), "update --repository your-org/your-repo"),
    ),
)
def test_update_script_supports_single_and_multi_repository_configs(
    tmp_path: Path,
    arguments: tuple[str, ...],
    expected_suffix: str,
) -> None:
    script = tmp_path / "update.sh"
    script.write_text((ROOT / "update.sh").read_text())
    script.chmod(0o755)
    (tmp_path / "config.toml").write_text("# test config\n")
    calls = tmp_path / "calls.log"
    _write_executable(
        tmp_path / ".venv/bin/ctoloop",
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"$SCRIPT_TEST_LOG\"\n",
    )

    result = subprocess.run(
        [str(script), *arguments],
        cwd=tmp_path,
        env={**os.environ, "SCRIPT_TEST_LOG": str(calls)},
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().strip() == (
        f"--config {tmp_path / 'config.toml'} {expected_suffix}"
    )


def test_deploy_script_runs_configured_steps_in_order(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    calls = tmp_path / "calls.log"
    logger = shlex.quote(str(calls))
    _write_executable(
        fake_bin / "git",
        f"#!/usr/bin/env bash\nprintf 'git %s\\n' \"$*\" >> {logger}\n",
    )
    _write_executable(
        fake_bin / "curl",
        f"#!/usr/bin/env bash\nprintf 'curl %s\\n' \"$*\" >> {logger}\n",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CTOLOOP_DEPLOY_ROOT": str(checkout),
        "CTOLOOP_BASE_BRANCH": "release",
        "CTOLOOP_BUILD_COMMAND": f"printf 'build\\n' >> {logger}",
        "CTOLOOP_RESTART_COMMAND": f"printf 'restart\\n' >> {logger}",
        "CTOLOOP_HEALTHCHECK_URL": "https://example.test/healthz",
    }

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-production.sh")],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "git fetch origin release",
        "git merge --ff-only origin/release",
        "build",
        "restart",
        "curl --fail --silent --show-error https://example.test/healthz",
    ]


def test_deploy_script_requires_a_git_checkout() -> None:
    env = {key: value for key, value in os.environ.items() if key != "CTOLOOP_DEPLOY_ROOT"}
    result = subprocess.run(
        [str(ROOT / "scripts/deploy-production.sh")],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "CTOLOOP_DEPLOY_ROOT must point to" in result.stderr
