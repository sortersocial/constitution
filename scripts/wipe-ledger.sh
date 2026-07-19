#!/usr/bin/env bash
# Truncate the production ledger and restart so the in-memory store reloads empty.
set -euo pipefail

APP="${FLY_APP:-slug-constitution}"
REMOTE="${JSONL_PATH:-/data/ledger.jsonl}"

if [[ "${1:-}" != "--yes" ]]; then
  echo "This will erase ${REMOTE} on ${APP} and restart the machine." >&2
  echo "Re-run with --yes to confirm." >&2
  exit 1
fi

fly ssh console -a "$APP" -C "sh -c 'truncate -s 0 ${REMOTE} && wc -c ${REMOTE}'"
fly apps restart "$APP"
echo "wiped ${REMOTE} on ${APP} and restarted"
