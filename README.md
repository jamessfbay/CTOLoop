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

For code changes, an optional native workflow layer adds bounded engineering
playbooks, observable exit criteria, independent verification, and an append-only
decision trail. This design absorbs the strongest ideas from pstack without
depending on Cursor-only `/loop`, cloud-agent, sticky-chat, or interactive prompt
primitives.

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
2. With the durable runtime enabled, CTOLoop first persists an idempotent Issue
   delivery job; a leased worker then claims it with `agent:working` and removes
   `agent:ready`.
3. A read-only Codex run classifies the request and selects its playbook.
4. Questions and analysis receive a direct Issue response without a branch or PR.
5. Code-change requests continue in a fresh worktree based on `origin/main`.
6. GitHub-hosted Issue images are downloaded, validated, and attached when present.
7. Codex implements the smallest requested change and leaves it in the worktree.
8. CTOLoop rejects protected paths and runs configured validation commands.
9. CTOLoop commits, pushes, and opens a pull request linked to the Issue.
10. A durable watcher follows HEAD, CI, reviews, mergeability, and terminal PR
    state across daemon restarts.
11. Optional auto-merge and post-merge production synchronization remain policy-gated.

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

### Native engineering workflow layer

CTOLoop can classify each code-change Issue into one bounded playbook:
`bug_fix`, `feature`, `refactor`, `performance`, `documentation`, or
`maintenance`. The classification includes observable exit criteria. CTOLoop
then owns the iteration loop and decides whether those criteria were actually
met; an implementation agent cannot declare its own run successful.

```toml
[workflow]
enabled = true
independent_verifier = true
decision_log = true
max_iterations = 2
implementation_model = "" # empty inherits [codex].model
verifier_model = ""       # may be a different model

[workflow.runtime]
enabled = true
heartbeat_seconds = 300
lease_seconds = 900
stalled_after_seconds = 1800
max_recovery_attempts = 3
max_execution_attempts = 6
retry_backoff_seconds = 120
poll_fallback_seconds = 600
max_wait_hours = 72
```

The resulting code-change flow is:

1. A read-only triage run chooses a playbook and at most eight exit criteria.
2. The implementation model makes the smallest evidence-backed change in the
   CTOLoop-owned worktree.
3. CTOLoop checks protected paths and runs the repository's configured validation
   commands itself.
4. A separate read-only verifier inspects the final diff and validation evidence.
5. `needs_changes` feedback starts another bounded implementation iteration.
6. `pass` is accepted only when every criterion is explicitly satisfied. A
   `needs_input` verdict stops safely instead of guessing.
7. Only CTOLoop may commit, push, create the PR, enable auto-merge, or synchronize
   production.

`max_iterations` must be between 1 and 8. Exhausting the budget is a failed run,
not a relaxed success. The verifier uses `sandbox = "read-only"`, approval policy
`never`, and never receives authority to publish or mutate GitHub state.

When `decision_log = true`, CTOLoop records routing, implementation checkpoints,
validation evidence, verifier verdicts, and terminal delivery decisions in the
SQLite `run_decisions` table. The trail is append-only; corrections create new
records instead of rewriting history:

```bash
.venv/bin/ctoloop history --run-id 42
```

This layer complements gstack rather than replacing it. The native workflow owns
routing, iteration, evidence, and exit decisions. gstack remains a collection of
bounded implementation/review/health stages inside an iteration. The Cloudflare
security audit remains a separate static-only workflow.

The design is inspired by the MIT-licensed
[pstack project](https://github.com/cursor/plugins/tree/main/pstack), especially
its playbooks, verifiable exit conditions, author/verifier separation, and
decision trail. CTOLoop does not vendor or execute `poteto-mode`; pstack's Cursor
model names, `/loop`, `Task` subagents, force-push/merge behaviors, and unrestricted
MCP assumptions are deliberately excluded.

#### Durable heartbeat and watcher runtime

When `[workflow]` and `[workflow.runtime]` are enabled, ready Issues are first
inserted as durable `issue-delivery` workflows instead of being executed directly
by the GitHub polling thread. Commit, push, and PR creation are fenced checkpoints.
After PR creation, a separate durable `pr-lifecycle` workflow takes over. Both are
independent from the Codex conversation that created them:

```text
queued → claimed → planned → committed → pushed → pr-created
  ↓
awaiting_ci
  → ci_failed | awaiting_review | review_changes_requested
  → merge_blocked | merge_ready
  → completed | closed | failed
```

Every execution attempt obtains an atomic, expiring lease. A background heartbeat
renews the lease while its handler is alive. Heartbeats and actual progress are
tracked separately: a process that remains alive but produces no new progress is
still recoverable after `stalled_after_seconds`. Once another worker takes the
lease, the old owner is fenced and cannot commit a state transition.

If the daemon dies after `git commit` or `git push`, the checkpoint contains the
expected branch and commit SHA. Recovery compares the remote branch to that SHA,
rejects mismatches, finds an existing PR idempotently, or creates the missing PR
without rerunning implementation. A stale unpublished worktree is removed only
while the recovering worker owns the matching Issue lease.

Watchers store the last authoritative GitHub PR observation. A changed HEAD,
check result, review state, mergeability state, or PR state creates one idempotent
event and wakes the workflow immediately. When no external event arrives,
`poll_fallback_seconds` provides the heartbeat-style polling fallback. Events are
deduplicated by their key, and all verifier/CI conclusions remain associated with
the observed PR HEAD.

The daemon reconstructs the runtime entirely from SQLite after a restart. An
expired lease is recovered automatically; repeated recoveries stop at
`max_recovery_attempts`, while ordinary handler failures stop at
`max_execution_attempts`. The original workflow deadline is not extended by a
restart or a new watch, so a stuck PR cannot consume resources forever.

The daemon runs a dedicated watcher thread and a durable worker pool sized by
`scheduler.max_parallel_repositories`. Long Codex execution therefore does not
block CI/review watchers. Repository write workflows still take the repository
lock, while independent repositories can progress concurrently.

External webhook or CI adapters can wake a registered watch without granting the
event sender direct workflow control:

```bash
.venv/bin/ctoloop workflow-event \
  --id 'pr:your-org/your-repo:123' \
  --key 'github-delivery-id' \
  --type github-pr \
  --resource 'your-org/your-repo#123' \
  --payload-json '{"action":"synchronize"}'

.venv/bin/ctoloop workflow-status --id 'pr:your-org/your-repo:123'
```

The event payload is only a wake signal. The handler re-reads the authoritative
PR state through GitHub before deciding a transition, so a forged payload cannot
claim that CI passed or a PR merged.

Run the complete local example to exercise real SQLite persistence, CI and review
wake events, daemon reconstruction, and terminal merge completion:

```bash
.venv/bin/python scripts/durable-workflow-example.py
```

The example must end with `"result": "PASS"` and four durable attempts spanning
the pre-restart and post-restart owners.

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
