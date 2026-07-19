#!/usr/bin/env bash
# Download the production constitutional ledger via flyctl.
set -euo pipefail

APP="${FLY_APP:-slug-constitution}"
REMOTE="${JSONL_PATH:-/data/ledger.jsonl}"
OUT="${1:-ledger.jsonl}"

mkdir -p "$(dirname "$OUT")"
fly sftp get "$REMOTE" "$OUT" -a "$APP"
wc -c "$OUT" | awk -v f="$OUT" '{print "wrote", $1, "bytes to", f}'
