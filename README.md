# CTOLoop

CTOLoop is an event-driven, no-human-in-the-execution-loop software delivery
service. It continuously watches governed GitHub state, starts isolated Codex and
gstack workflows when an authorized event appears, and carries the work through
analysis, code changes, validation, publishing, and optional production
synchronization without someone supervising a terminal or chat session.

**No babysitting. No interactive agent session. GitHub is the control plane.**
Repository policy, labels, validation commands, and protected paths are configured
up front. Once an event satisfies that policy, CTOLoop executes the workflow,
records the result, updates GitHub, and continues watching for the next event.

CTOLoop supports four primary workflows:

1. Answer read-only questions raised in GitHub Issues.
2. Review pull requests without modifying repository or GitHub state.
3. Diagnose failed GitHub Actions runs from bounded log context.
4. Turn an approved Issue into a validated candidate pull request.

CTOLoop does not pass production credentials to Codex. Target repositories own
their CI and deployment policies; CTOLoop only invokes the commands explicitly
configured for each repository.

## No-human, event-driven operation

CTOLoop is designed to run continuously as a daemon. GitHub state changes are its
events and durable queue:

- An Issue receiving `agent:ready` triggers analysis and, when needed, an isolated
  implementation workflow.
- Validation success triggers commit, push, and pull-request creation.
- A merged CTOLoop pull request triggers the configured production synchronization
  on the next polling cycle.
- Failures are recorded in SQLite and reflected through GitHub labels and comments;
  the daemon keeps processing other configured repositories.

There is no human in the normal execution path. A human or another policy system
may authorize work by applying a label, but nobody needs to remain online while it
runs. If a task requires a genuine product decision, the pipeline stops safely and
reports that input is required instead of guessing.

## How event processing works

1. A maintainer or upstream policy automation adds `agent:ready` to an authorized Issue.
2. CTOLoop claims it with `agent:working` and removes `agent:ready`.
3. A read-only Codex run classifies the request.
4. Questions and analysis receive a direct Issue response without a branch or PR.
5. Code-change requests continue in a fresh worktree based on `origin/main`.
6. GitHub-hosted Issue images are downloaded, validated, and attached when present.
7. Codex implements the smallest requested change and leaves it in the worktree.
8. CTOLoop rejects protected paths and runs configured validation commands.
9. CTOLoop commits, pushes, and opens a pull request linked to the Issue.
10. Optional auto-merge and post-merge production synchronization remain policy-gated.

After step 1, the normal path is fully unattended.

## Requirements

- Python 3.11 or newer
- Git
- GitHub CLI (`gh`)
- Codex CLI (`codex`)
- `pytest` for the CTOLoop test suite
- Optional: gstack installed for the Codex host when `[gstack].enabled = true`

CTOLoop has no third-party Python runtime dependencies.

## Install

```bash
cd /opt/ctoloop
python3 -m venv .venv
.venv/bin/python -m pip install -e . pytest
cp config.example.toml config.toml
```

Edit `config.toml` before starting the service. The example intentionally uses
placeholder repository and account names so it can be adapted without inheriting
another deployment's paths or credentials.

Authenticate a configured repository and the Codex CLI:

```bash
./auth.sh
```

For non-default config locations, set `CTOLOOP_CONFIG`.

## Configuration

Shared GitHub labels, Codex settings, scheduling, and automation policy are
configured once. Add one `[[repositories]]` entry per target repository:

```toml
[github]
ready_label = "agent:ready"
working_label = "agent:working"
pr_label = "agent:pr-open"
failed_label = "agent:failed"

[scheduler]
poll_seconds = 60
max_parallel_repositories = 2

[[repositories]]
repository = "your-org/your-repo"
path = "state/repositories/your-org/your-repo"
base_branch = "main"
branch_prefix = "codex/issue-"
worktree_root = "/tmp/your-repo-ctoloop"
git_user_name = "CTOLoop"
git_user_email = "ctoloop@example.com"

[repositories.github]
auth_user = "your-github-user"
auth_config_dir = "state/github-auth/your-repo"

[[repositories.validation]]
name = "tests"
argv = ["python3", "-m", "pytest", "-q"]
timeout_seconds = 1200
```

Each repository must have a unique `path` and `worktree_root`, and its `origin`
must resolve to the configured `owner/repository`. Work within a repository is
serialized to avoid Git lock conflicts. Different repositories run concurrently
up to `scheduler.max_parallel_repositories`.

The legacy single-repository TOML shape remains readable, but new installations
should use `[[repositories]]`.

### Optional gstack workflow layer

CTOLoop can use gstack as a bounded workflow and quality layer while retaining
exclusive control of GitHub writes, worktrees, protected paths, validation,
commits, pushes, pull requests, and deployment.

Install gstack for the Codex host, then configure:

```toml
[gstack]
enabled = true
skill_root = "~/.codex/skills"
state_root = "state/gstack"
fallback_to_codex = true
review_skills = ["review"]
ci_skills = ["investigate"]
implementation_skills = ["review", "health"]
```

The default gstack mapping is:

- Pull request review: `review`
- CI diagnosis: `investigate`
- After implementation: `review`, then `health`

The implementation pipeline also accepts `qa`, `qa-only`, `cso`,
`plan-eng-review`, `plan-design-review`, and `plan-devex-review`. These longer
stages are opt-in because they may require a browser, project services, more
runtime, or human product decisions. `ship` and `land-and-deploy` are deliberately
not accepted: CTOLoop owns those state-changing operations.

Each stage receives an isolated `GSTACK_HOME`, no `GH_TOKEN` or `GITHUB_TOKEN`,
and an explicit non-interactive instruction. If a configured skill is missing or
fails and `fallback_to_codex` is true, CTOLoop records the fallback and continues
with the plain Codex result. Execution metadata and bounded output tails are stored
in `state/runs.sqlite3` under `execution_results`.

### Cloudflare security audit layer

CTOLoop can run Cloudflare's `security-audit` skill as a separate, heavyweight
security workflow. It is intentionally independent from gstack: gstack remains
the routine implementation/review layer, while this workflow produces a
coverage ledger, independently verified findings, and a formal report.

Install the skill for the Codex host:

```bash
npx skills add https://github.com/cloudflare/security-audit-skill \
  --skill security-audit --global
```

Configure its installed directory (adjust `skill_root` if the installer uses a
different agent skill directory):

```toml
[security_audit]
enabled = true
skill_root = "~/.codex/skills/security-audit"
state_root = "state/security-audits"
default_profile = "standard"       # quick, standard, or deep
max_agent_invocations = 24
timeout_seconds = 14400
require_validated_output = true
issue_label = "security:audit"
execution_policy = "static-only"
```

Run an audit directly:

```bash
.venv/bin/ctoloop security-audit --repository your-org/your-repo
.venv/bin/ctoloop security-audit --profile quick --scope src/ --json
```

For unattended event-driven operation, add both `security:audit` and
`agent:ready` to an open Issue. CTOLoop claims the Issue, runs the audit, validates
`coverage-ledger.json` and `findings.json` using the skill's own zero-dependency
Node validators, stores the full artifacts below `state/security-audits/`, and
posts a bounded summary back to the Issue.

The current execution policy is deliberately `static-only`. The audit agent's
writable root is the external run directory; the target repository stays outside
that root and is read-only. Target-controlled builds, tests, scripts, package
managers, browsers, fuzzers, and network access are forbidden. Findings that
need runtime proof remain `needs_validation` instead of being overstated as
confirmed. A future OS-isolated runner can add dynamic validation without
weakening this default.

### Repository-specific GitHub authentication

Each target can use an isolated GitHub CLI credential directory:

```toml
[repositories.github]
auth_user = "your-github-user"
auth_config_dir = "state/github-auth/your-repo"
```

Select a project interactively or by repository name:

```bash
./auth.sh --list
./auth.sh your-org/your-repo
./auth.sh --force your-org/your-repo
```

When the configured checkout is missing, `auth.sh` clones it after authentication.
A non-empty directory that is not a Git checkout is rejected rather than overwritten.

For service deployments, a token can be supplied through a named environment
variable. Store the variable name in TOML, never the token value:

```toml
[repositories.github]
token_env = "YOUR_REPO_GITHUB_TOKEN"
```

## Commands

```bash
.venv/bin/ctoloop doctor
.venv/bin/ctoloop bootstrap
.venv/bin/ctoloop once
.venv/bin/ctoloop once --repository your-org/your-repo
.venv/bin/ctoloop patch-issue --repository your-org/your-repo --issue 123
.venv/bin/ctoloop review-pr --repository your-org/your-repo --pr 123
.venv/bin/ctoloop diagnose-ci --repository your-org/your-repo --run-id 456
.venv/bin/ctoloop diagnose-ci --repository your-org/your-repo --pr 123
.venv/bin/ctoloop security-audit --repository your-org/your-repo
.venv/bin/ctoloop update --repository your-org/your-repo
.venv/bin/ctoloop history
.venv/bin/ctoloop watch
```

`bootstrap` verifies prerequisites, creates or updates the four workflow labels,
and prepares worktree roots.

`review-pr` and `diagnose-ci` always force a read-only sandbox and disable
approvals, regardless of the implementation settings. Use `--json` for the
schema-validated result or `--publish` to add the rendered report to a PR.

`patch-issue` runs the same isolated implementation path used by automatic Issue
polling. The Issue must carry the configured ready label.

## Continuous operation

For a foreground-independent local process:

```bash
./start.sh
./stop.sh
```

Runtime paths can be overridden with:

- `CTOLOOP_CONFIG`
- `CTOLOOP_PID_FILE`
- `CTOLOOP_LOG_FILE`
- `CTOLOOP_STOP_TIMEOUT`
- `CTOLOOP_SYSTEMD_UNIT`

For systemd, review the installation paths and writable directories in the unit,
then install it:

```bash
sudo cp systemd/ctoloop.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ctoloop
sudo journalctl -u ctoloop -f
```

The supplied unit expects CTOLoop at `/opt/ctoloop`. Its privileged
`ExecStartPre` disables Ubuntu's AppArmor unprivileged-user-namespace restriction
so the Codex `workspace-write` sandbox can initialize. This host-wide setting is
appropriate only on a dedicated host or VM.

## Production synchronization

A repository can optionally synchronize merged code into a local production
directory:

```toml
[[repositories]]
repository = "your-org/your-repo"
path = "state/repositories/your-org/your-repo"
production_path = "/srv/your-repo"
production_checkout_path = "/srv/your-repo-checkout"
```

CTOLoop fast-forwards the dedicated checkout and copies only Git-tracked files.
Untracked runtime content such as environment files, dependency directories, and
build caches remains untouched. Files removed from Git are removed from the
production destination.

Optional post-sync commands run in order after files are copied:

```toml
[[repositories.production_commands]]
name = "build"
argv = ["npm", "run", "build"]
timeout_seconds = 1800

[[repositories.production_commands]]
name = "restart"
argv = ["systemctl", "--user", "restart", "your-app.service"]
timeout_seconds = 120
```

Commands are argv arrays, not shell strings. They should be idempotent because a
failed production update is retried on a later polling cycle.

The optional protected-runner helper at `scripts/deploy-production.sh` uses these
environment variables:

- `CTOLOOP_DEPLOY_ROOT`
- `CTOLOOP_BASE_BRANCH` (defaults to `main`)
- `CTOLOOP_BUILD_COMMAND`
- `CTOLOOP_RESTART_COMMAND`
- `CTOLOOP_HEALTHCHECK_URL`

## Security model

- Issue, PR, workflow-log, and attachment content is treated as untrusted input.
- Analysis workflows run read-only with approval requests disabled.
- Implementation runs are isolated in fresh Git worktrees.
- Only the orchestrator performs GitHub writes, commits, pushes, and PR creation.
- Protected path patterns are checked before validation and publishing.
- Validation and post-sync commands use argv arrays without shell expansion.
- GitHub attachments are restricted by host, HTTPS, size, count, and file signature.
- Cross-host image redirects do not receive the GitHub authorization header.
- Every local `origin` is verified against its configured GitHub repository.
- Production updates are fast-forward-only and require the expected base branch.
- Run state is stored in `state/runs.sqlite3` with repository-qualified records.

Use a dedicated machine account or narrowly scoped GitHub App/token for long-term
operation. Keep auto-merge disabled until branch protection and required checks
are configured in each target repository.

## License

CTOLoop is open source under the [MIT License](LICENSE). You may use, modify,
distribute, sublicense, and sell copies of the software as long as the copyright
and license notice remains included.
