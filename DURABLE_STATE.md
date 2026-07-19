# Durable constitution state

The live application reads and writes `/data/constitution.rocks`. The historical
`ledger.jsonl` remains the archival tape and migration contract; the running
application never appends to or replays it.

## Dependency

`pyproject.toml` pins `evaleval` to the immutable Git commit containing the
durable state API. Regenerate `uv.lock` normally when changing that pin; do not
use a local path dependency or hand-edit the lock.

## Import modes

The preferred rollout path is an opt-in synchronous first boot:

```sh
IMPORT_JSONL_ON_EMPTY=1 \
JSONL_PATH=/data/ledger.jsonl \
ROCKS_PATH=/data/constitution.rocks \
python constitution.py
```

Before the server starts accepting traffic, it closes its RocksDB probe,
streams the tape into an empty projection, verifies it, then opens the live
database. If the database is nonempty, it is opened unchanged and the tape is
not imported. A missing tape or failed import fails startup. During import the
HTTP health endpoint is not yet available; after startup `/api/health` reports
`ledger_ready`, `importing`, `error`, and the indexed event count.

The manual importer remains available in the image for maintenance downtime:

During maintenance downtime:

```sh
python scripts/import-ledger.py \
  /data/ledger.jsonl \
  /data/constitution.rocks \
  --batch-size 100
```

Use `--force` to destroy an existing projection and rebuild from byte zero.
Import reads one JSONL line at a time, commits bounded batches, verifies event
and type counts, chain head, and representative exact lookups, and deletes an
incomplete Rocks projection on failure.

Start the application with:

```sh
ROCKS_PATH=/data/constitution.rocks python constitution.py
```

Do not run the importer concurrently with the application.

## Rollout sequence

1. Verify the immutable `evaleval` pin, regenerate `uv.lock`, build the image,
   and run both repositories' complete suites.
2. Keep `DISABLE_EPOCH_LOOP=1`. Take a volume snapshot/backup containing
   `/data/ledger.jsonl`. Do not unpause writes during migration.
3. Add `IMPORT_JSONL_ON_EMPTY=1` and deploy with a non-overlapping/immediate
   strategy so the old process releases the volume before the new process
   probes RocksDB:

   ```sh
   fly deploy --strategy immediate
   ```

4. Wait for startup/import to finish. Require `/api/health` to report
   `ok=true`, `ledger_ready=true`, `importing=false`, `error=null`, and the
   expected `event_count`. Verify representative epoch, evidence hardlink,
   patch download, ranking, latest discovery, and latest emission routes.
5. Remove `IMPORT_JSONL_ON_EMPTY` and deploy again while
   `DISABLE_EPOCH_LOOP=1`. A nonempty RocksDB would be preserved either way;
   removing the flag makes normal operation explicit.
6. After the second health and route check, remove
   `DISABLE_EPOCH_LOOP` and deploy once more to unpause.

Rollback before step 6 is safe: stop the Rocks-backed process, deploy the prior
image with `DISABLE_EPOCH_LOOP=1`, and continue using the unchanged archival
JSONL tape. Keep the Rocks directory for diagnosis. After step 6, the JSONL
tape no longer contains new live writes, so rolling back to the JSONL-writing
version would lose or fork those events. After unpause, rollback must stay on a
Rocks-capable image or first perform an explicitly designed/exported
reconciliation; do not simply start the old writer.

## Direct schema

`constitution.py` defines one `Record` rooted at `constitution-v1`:

- `events`: canonical tape order
- `evidence_by_id`, `event_ids_by_kind`, `event_ids_by_epoch`
- exact entity hardlinks for commits, comparisons, attempts, judgments,
  ranking runs, snapshots, and OIDs
- judgments/attempts by comparison and comparisons by commit
- emissions and discoveries by epoch plus exact latest values
- durable Git OID and patch-identity discovery indexes
- content-addressed raw blobs by SHA-256
- chain head, emitted total, import count, and tape digest metadata

Application code selects these paths directly. `append_evidence` is intentionally
narrow: it exists because canonical append, chain head, blobs, and all secondary
indexes must change atomically.

Patch/prompt/request/response/diff bytes are stored once under their SHA-256,
including recursively nested fields in imported Evidence and GitDiscovery
rows. The JSONL tape remains unchanged and byte-authoritative. Its event IDs,
chain links, entity IDs, and semantic values are preserved, while RocksDB's
canonical and indexed copies use compact blob references. Detail/download
routes resolve either representation transparently; epoch cards never fetch
blob values.

## Rebuilding a flawed projection

The first-boot importer intentionally skips a nonempty database, so it cannot
repair an older projection containing embedded base64. For this one migration,
start the corrected image with both maintenance flags:

```sh
DISABLE_EPOCH_LOOP=1
REBUILD_ROCKS_FROM_JSONL=1
```

Use a non-overlapping deployment strategy so the prior process releases
RocksDB first. Before opening its live handle, startup requires the tape,
verifies and closes a probe of the nonempty projection, refuses to proceed if
`/data/constitution.rocks.pre-rebuild` already exists, then atomically renames
the old directory to that exact backup path. It imports and verifies a fresh
`/data/constitution.rocks`. On failure it destroys the partial fresh directory
and renames the backup back automatically. On success it serves from the new
projection and deliberately retains the backup.

While still paused, verify the health count and representative epoch,
comparison, commit, patch, prompt, and attempt routes. Then remove
`REBUILD_ROCKS_FROM_JSONL` before any restart or second deployment; leaving it
enabled will correctly fail startup because the deterministic backup exists.
Restart once with only `DISABLE_EPOCH_LOOP=1`, verify again, and only then
unpause. Delete `.pre-rebuild` manually only after the verification window.

For rollback after a successful rebuild, stop the process, move the fresh
directory aside, rename `.pre-rebuild` back to `/data/constitution.rocks`, and
start the prior image while still paused. The rebuild flag is exact and
inactive for every value except `1`.
