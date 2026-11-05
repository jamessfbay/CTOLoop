#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="${CTOLOOP_DEPLOY_ROOT:-}"
BASE_BRANCH="${CTOLOOP_BASE_BRANCH:-main}"

if [[ -z "$ROOT_DIR" || ! -d "$ROOT_DIR/.git" ]]; then
  echo "CTOLOOP_DEPLOY_ROOT must point to the target application's Git checkout" >&2
  exit 1
fi

cd "$ROOT_DIR"
git fetch origin "$BASE_BRANCH"
git merge --ff-only "origin/$BASE_BRANCH"

if [[ -z "${CTOLOOP_BUILD_COMMAND:-}" ]]; then
  echo "CTOLOOP_BUILD_COMMAND is required on the protected production runner" >&2
  exit 1
fi
/usr/bin/env bash -lc "$CTOLOOP_BUILD_COMMAND"

# Production process management is installation-specific. Configure this hook
# on the protected self-hosted runner, for example to invoke systemd or Forever.
if [[ -z "${CTOLOOP_RESTART_COMMAND:-}" ]]; then
  echo "CTOLOOP_RESTART_COMMAND is required on the protected production runner" >&2
  exit 1
fi

/usr/bin/env bash -lc "$CTOLOOP_RESTART_COMMAND"

if [[ -z "${CTOLOOP_HEALTHCHECK_URL:-}" ]]; then
  echo "CTOLOOP_HEALTHCHECK_URL is required on the protected production runner" >&2
  exit 1
fi

curl --fail --silent --show-error "$CTOLOOP_HEALTHCHECK_URL"
