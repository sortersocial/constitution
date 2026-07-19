#!/usr/bin/env python3
"""Project archival JSONL into a fresh RocksDB database."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

os.environ.setdefault("SESSION_SECRET", "offline-migration")
os.environ.setdefault("GENESIS_MS", "0")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from constitution import import_jsonl_tape  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stream ledger.jsonl into a fresh Rocks projection. "
            "An incomplete projection is deleted on failure."
        )
    )
    parser.add_argument("tape", type=pathlib.Path)
    parser.add_argument("rocks", type=pathlib.Path)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument(
        "--force",
        action="store_true",
        help="destroy an existing projection and rebuild from zero",
    )
    args = parser.parse_args()
    report = import_jsonl_tape(
        args.tape,
        args.rocks,
        batch_size=args.batch_size,
        force=args.force,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
