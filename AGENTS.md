# AGENTS.md

## Cursor Cloud specific instructions

This repo is a single product, **slug constitution** — one FastAPI process defined
entirely in `constitution.py` (token economics + Git-commit discovery + LLM council
ranking + an auditable RocksDB ledger). Supporting scripts (`mint.py`,
`launchtime.py`, `scripts/import-ledger.py`) are standalone utilities, not separate
services. See `DURABLE_STATE.md` for ledger/migration internals and
`.github/workflows/deploy.yml` for the canonical CI commands.

### Toolchain (already installed in the snapshot)
- Python deps are managed by **uv** (`uv.lock` + `pyproject.toml`). `uv` lives in
  `~/.local/bin` and is on `PATH` via `~/.bashrc`. The update script runs
  `uv sync --frozen`.
- **Babashka** (`bb`) is installed in `/usr/local/bin` and is required only for the
  integration suite. It is NOT restored by the update script; if a fresh VM lacks it,
  reinstall with the command in `.github/workflows/deploy.yml`.

### Run (dev)
- `SESSION_SECRET=dev-secret GENESIS_MS=1775364391260 DISABLE_EPOCH_LOOP=1 uv run constitution.py`
- Binds `0.0.0.0:$PORT` (default **8080**). Health at `/api/health`.
- `SESSION_SECRET` and `GENESIS_MS` are mandatory at import time (tests get defaults
  from `tests/conftest.py`, but the app process does not).
- Set `DISABLE_EPOCH_LOOP=1` in dev: otherwise the background epoch loop tries to
  fetch the real `DEFAULT_REPOSITORIES` from GitHub and (for multi-commit epochs)
  calls OpenRouter.

### Test
- Unit tests: `uv run pytest -q` (~95s; `testpaths = ["tests"]`).
- Process integration suite: `bb TEST.sh`. It self-contains everything — spins up a
  mock OpenRouter, local bare Git remotes, imports JSONL→Rocks, starts the server,
  and checks the API/SSE/emission/replay paths. No external network or secrets needed.
- **There is no lint step / lint config** in this repo (no ruff/mypy/flake8). Quality
  gates are pytest + the Babashka integration suite only.

### Non-obvious gotchas
- Ranking only calls OpenRouter when an epoch has **≥2 eligible commits**; a
  single-commit epoch is ranked locally and needs no `OPENROUTER_API_KEY`.
- The `/test/emit` trigger endpoint returns 404 unless `ALLOW_TEST_TRIGGERS=1`.
- To exercise a real emission end-to-end locally without any external service: create
  a local bare Git repo with one commit, point `REPOSITORIES_JSON` at it, map the
  author email via `CONTRIBUTORS_JSON`, set `GENESIS_MS` to ~35 days ago (so you are at
  epoch 1+), set `ALLOW_TEST_TRIGGERS=1`, then `POST /test/emit`.
- Persistent paths (`ROCKS_PATH`, `GIT_MIRROR_DIR`, `JSONL_PATH`) default to `/data/*`
  in `fly.toml`; override them to a writable temp dir in dev.
