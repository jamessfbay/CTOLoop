#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${CTOLOOP_CONFIG:-$ROOT_DIR/config.toml}"
AGENT_BIN="$ROOT_DIR/.venv/bin/ctoloop"

if [[ ! -x "$AGENT_BIN" ]]; then
  echo "Update failed: $AGENT_BIN was not found. Install CTOLoop first." >&2
  exit 1
fi
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Update failed: config file $CONFIG_FILE was not found." >&2
  exit 1
fi

cd -- "$ROOT_DIR"
if [[ -n "${1:-}" ]]; then
  exec "$AGENT_BIN" --config "$CONFIG_FILE" update --repository "$1"
fi
exec "$AGENT_BIN" --config "$CONFIG_FILE" update
