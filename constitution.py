#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi",
#   "uvicorn",
#   "httpx",
#   "tenacity",
#   "evaleval @ git+https://github.com/tommy-mor/evaleval.git@e330d82a7e813e59b7e594f4c990a6c66d8fb0c2",
#   "rocksdict>=0.3.29",
#   "authlib",
#   "itsdangerous",
#   "starlette",
#   "numpy",
#   "sympy",
# ]
# ///
"""
constitution.py — the entire economic mechanism of slug in one file.

This file is the constitution. It runs on fly.io as a single process.
The code is auditable in a public GitHub repo. Every commit is a public diff.
A daily GitHub Action backs up the JSONL ledger to the same repo.

Run: uv run constitution.py
"""

from decimal import Decimal, getcontext, DefaultContext
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
import json, time, os, asyncio, httpx, pathlib, subprocess, hashlib, re, fcntl, base64
import sympy as sp  # type: ignore[reportMissingImports]
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from evaleval import (
    event, to_dict, from_dict, render, RawContent, Signer,
    SnippetExecutionError, exec_event, Selector, MORPH, PREPEND,
    Record, Leaf, Map, List, Deque, Sum, RocksDb, Durability,
)
from evaleval.depth import One, Two, Three

DefaultContext.prec = 50
getcontext().prec = 50

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.environ["SESSION_SECRET"])
signer = Signer()

# ===========================================================================
# §1. CONSTANTS — constitutional inputs and everything derived from them
# ===========================================================================

# Four constitutional inputs. These are the arbitrary choices.
total_supply = sp.Integer(1)          # your slug balance is the fraction of the slug that you own.
lp_usdc = sp.Integer(1331)            # Mathew 13:31
fdv = sp.Integer(177600)              # begins small
HALF_LIFE_YEARS = Decimal("17.72577371892")  # promethium

# The founder receives zero initial allocation. Tokens are earned only
# through contributions, same as everyone else.
FOUNDER_INITIAL_SHARE = Decimal("0")

# ---------------------------------------------------------------------------
# §1a. OWNERSHIP MATH — solve the system, derive everything else
# ---------------------------------------------------------------------------
#
# Solve this system for (price, lp_tokens, lp_pct):
#   fdv     = price * supply
#   lp_usdc = price * lp_tokens
#   lp_pct  = lp_tokens / supply
#
# The key derived identity: lp_pct = lp_usdc / fdv

# inputs
supply_s, lp_usdc_s, fdv_s = sp.symbols("supply lp_usdc fdv", positive=True)

# free
price_s, lp_tokens_s, lp_pct_s = sp.symbols("price lp_tokens lp_pct", positive=True)

ownership_solution = sp.solve([
    sp.Eq(fdv_s, price_s * supply_s),
    sp.Eq(lp_usdc_s, price_s * lp_tokens_s),
    sp.Eq(lp_pct_s, lp_tokens_s / supply_s),
], [price_s, lp_tokens_s, lp_pct_s], dict=True)[0]

assert sp.simplify(ownership_solution[price_s] - (fdv_s / supply_s)) == 0, "price must equal fdv / supply"
assert sp.simplify(ownership_solution[lp_pct_s] - (lp_usdc_s / fdv_s)) == 0, "lp_pct must equal lp_usdc / fdv"
assert sp.simplify(ownership_solution[lp_tokens_s] - (ownership_solution[lp_pct_s] * supply_s)) == 0, "lp_tokens must equal lp_pct * supply"

subs = {supply_s: total_supply, fdv_s: fdv, lp_usdc_s: lp_usdc}
price = sp.simplify(ownership_solution[price_s].subs(subs))
lp_pct = sp.simplify(ownership_solution[lp_pct_s].subs(subs))
lp_tokens = sp.simplify(ownership_solution[lp_tokens_s].subs(subs))

assert price == fdv, "constitutional price mismatch"
assert lp_pct == sp.Rational(1331, 177600), "constitutional LP percentage mismatch"
assert lp_tokens == sp.Rational(1331, 177600), "constitutional LP token allocation mismatch"

# ---------------------------------------------------------------------------
# §1b. RUNTIME DECIMALS — convert once, use everywhere below
# ---------------------------------------------------------------------------

def sympy_to_decimal(expr) -> Decimal:
    num, den = expr.as_numer_denom()
    return Decimal(str(num)) / Decimal(str(den))

TOTAL_SUPPLY = sympy_to_decimal(total_supply)
LP_USDC = sympy_to_decimal(lp_usdc)
FDV = sympy_to_decimal(fdv)
PRICE = sympy_to_decimal(price)
LP_PCT = sympy_to_decimal(lp_pct)
LP_TOKENS = sympy_to_decimal(lp_tokens)
CONTRIBUTOR_POOL = TOTAL_SUPPLY - LP_TOKENS

GENESIS_MS = int(os.environ["GENESIS_MS"])
JSONL_PATH = pathlib.Path(os.environ.get("JSONL_PATH", "/data/ledger.jsonl"))
ROCKS_PATH = pathlib.Path(
    os.environ.get("ROCKS_PATH", "/data/constitution.rocks")
)

# ===========================================================================
# §1c. CONFIGURATION — environment variables and constants
# ===========================================================================

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
GITHUB_API_BASE_URL = os.environ.get(
    "GITHUB_API_BASE_URL", "https://api.github.com"
).rstrip("/")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai").rstrip("/")

# Repositories, branches, and contributor identities are constitutional inputs.
# A ref pattern matches a complete Git ref: * stays within one path component,
# while ** crosses slashes, so refs/heads/** includes branches on branches.
# Environment overrides exist for deterministic integration tests and deployments
# using the exact same source; their normalized values are committed to every
# discovery event.
DEFAULT_REPOSITORIES = [
    {
        "id": "constitution",
        "url": "https://github.com/sortersocial/constitution.git",
        "refs": ["refs/heads/**"],
    },
    {
        "id": "slug",
        "url": "https://github.com/sortersocial/slug.git",
        "refs": ["refs/heads/**"],
    },
    {
        "id": "sorter",
        "url": "https://github.com/sorterisntonline/sorter.git",
        "refs": ["refs/heads/**"],
    },
    {
        "id": "sorter2",
        "url": "https://github.com/sortersocial/sorter2.git",
        "refs": ["refs/heads/**"],
    },
    {
        "id": "sorter-oldest",
        "url": "https://github.com/tommy-mor/sorter.git",
        "refs": ["refs/heads/**"],
    },
]
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://token.slug.social").rstrip("/")
EVIDENCE_SCHEMA_VERSION = 2
DEFAULT_CONTRIBUTORS = {
    "tommy-mor": ["thmorriss@gmail.com"],
    "christopher-whitman": [
        "chris@cwwhitman.com",
        "7566903+cwwhitman@users.noreply.github.com",
    ],
    "jake-chvatal": [
        "jake+github@uln.industries",
        "jakechvatal@gmail.com",
        "jake@isnt.online",
    ],
    "lara": ["me@lara.lv"],
    "nat-reid": ["nathanielreid@gmail.com"],
    "zod": ["jason.p.mcel@gmail.com", "me@zod.tf"],
    "jovan": ["jovan@slug.social", "jovan@getcivicai.com"],
}

REPOSITORIES = json.loads(
    os.environ.get("REPOSITORIES_JSON", json.dumps(DEFAULT_REPOSITORIES))
)
CONTRIBUTORS = json.loads(
    os.environ.get("CONTRIBUTORS_JSON", json.dumps(DEFAULT_CONTRIBUTORS))
)
GIT_MIRROR_DIR = pathlib.Path(os.environ.get("GIT_MIRROR_DIR", "/data/git"))
GIT_TIMEOUT_SECONDS = int(os.environ.get("GIT_TIMEOUT_SECONDS", "120"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Council model IDs: always include preferred seats, then slug.social garden rank
# under this parent (bodies = OpenRouter URLs), then top-up from OpenRouter list.
SLUG_SOCIAL_BASE_URL = os.environ.get("SLUG_SOCIAL_BASE_URL", "https://slug.social").rstrip("/")
SLUG_MODEL_RANK_PARENT = os.environ.get(
    "SLUG_MODEL_RANK_PARENT", "slug/token/commit-ranking/model"
).strip()
# OpenRouter "~…/…-latest" aliases resolve to the newest concrete model in-family.
DEFAULT_PREFERRED_COUNCIL_MODELS = [
    "~anthropic/claude-sonnet-latest",
    "~x-ai/grok-latest",
]
PREFERRED_COUNCIL_MODELS = json.loads(
    os.environ.get(
        "PREFERRED_COUNCIL_MODELS_JSON",
        json.dumps(DEFAULT_PREFERRED_COUNCIL_MODELS),
    )
)


# ===========================================================================
# §1d. LEDGER SCHEMA — typed events
# ===========================================================================

@event
class Emission:
    epoch: int
    timestamp_ms: int
    pool_before: str
    total_emitted: str
    pool_after: str
    decay_rate: str
    distributions: dict   # author -> amount str
    ranking: dict         # author -> score str
    models_used: list
    discovery_snapshot_id: str
    evidence_schema_version: int
    ranking_run_id: str
    ranking_event_id: str


@event
class UsdcDistribution:
    timestamp_ms: int
    treasury_balance: str
    distributions: dict   # wallet -> amount str


@event
class Redemption:
    timestamp_ms: int
    github_user: str
    wallet_address: str
    amount: str


@event
class GitDiscovery:
    schema_version: int
    epoch: int
    snapshot_id: str
    timestamp_ms: int
    config_digest: str
    initial_snapshot: bool
    configuration: dict
    repositories: list
    observations: list
    commits: list


@event
class Evidence:
    """Versioned, content-addressed constitutional evidence envelope."""
    schema_version: int
    event_id: str
    epoch: int
    kind: str
    recorded_at_ms: int
    previous_event_sha256: str
    payload: dict


# Direct application schema. Paths are the query/mutation interface; values are
# plain dictionaries. JSONL is authoritative; projected heavy values use refs.
STATE = Record(
    events=List(Leaf()),                         # canonical tape order
    evidence_by_id=Map(Leaf()),                  # event_id -> evidence dict
    event_ids_by_kind=Map(List(Leaf())),         # kind -> ordered event ids
    event_ids_by_epoch=Map(List(Leaf())),        # epoch -> ordered event ids
    evidence_count_by_epoch=Map(Sum()),
    discovery_evidence_by_epoch=Map(Leaf()),
    entity_event_ids=Map(Map(Leaf())),            # entity field -> id -> event id
    evidence_by_entity=Map(Leaf()),               # (kind, field, id) -> event id
    judgment_ids_by_comparison=Map(List(Leaf())),
    judgment_id_by_attempt=Map(Leaf()),
    attempt_ids_by_comparison=Map(List(Leaf())),
    comparison_ids_by_commit=Map(List(Leaf())),
    emissions_by_epoch=Map(Leaf()),
    emission_epochs=Map(Leaf()),
    latest_emission=Leaf(),
    discoveries_by_epoch=Map(Leaf()),
    discovery_epochs=Map(Leaf()),
    latest_discovery=Leaf(),
    epochs=Map(Leaf()),
    blobs=Map(Leaf()),                            # sha256 -> raw bytes
    seen_git_oids=Map(Leaf()),
    canonical_patch_oids=Map(Leaf()),
    meta=Record(
        chain_head=Leaf(),
        total_emitted=Leaf(),
        imported_count=Leaf(),
        tape_sha256=Leaf(),
    ),
)
ROOT = STATE.root("constitution-v1")
state_db: RocksDb | None = None
_LEDGER_LOCK = asyncio.Lock()
STATE_STATUS = {
    "ready": False,
    "importing": False,
    "error": None,
    "event_count": 0,
}


def _db() -> RocksDb:
    global state_db
    if state_db is None:
        state_db = RocksDb.open(ROCKS_PATH)
    return state_db


def prepare_state_sync() -> dict:
    """Open live state, optionally importing an archival tape first.

    The probe handle is always closed before the importer opens RocksDB. A
    nonempty projection is opened as-is and is never overwritten, even when
    ``IMPORT_JSONL_ON_EMPTY=1``.
    """
    global state_db
    should_rebuild = os.environ.get("REBUILD_ROCKS_FROM_JSONL") == "1"
    if should_rebuild:
        if state_db is not None:
            raise RuntimeError(
                "cannot rebuild RocksDB after the live state handle is open"
            )
        rocks_path = pathlib.Path(ROCKS_PATH)
        backup_path = pathlib.Path(str(rocks_path) + ".pre-rebuild")
        if not JSONL_PATH.is_file():
            message = f"archival tape not found: {JSONL_PATH}"
            STATE_STATUS.update(
                ready=False, importing=False, error=message, event_count=0
            )
            raise RuntimeError(message)
        if backup_path.exists():
            message = f"rebuild backup already exists: {backup_path}"
            STATE_STATUS.update(
                ready=False, importing=False, error=message, event_count=0
            )
            raise RuntimeError(message)
        if not rocks_path.exists():
            message = f"Rocks projection does not exist: {rocks_path}"
            STATE_STATUS.update(
                ready=False, importing=False, error=message, event_count=0
            )
            raise RuntimeError(message)

        probe = RocksDb.open(rocks_path)
        try:
            existing_count = ROOT.events.len(probe)
        finally:
            probe.close()
        if existing_count == 0:
            message = f"Rocks projection is empty; refusing rebuild: {rocks_path}"
            STATE_STATUS.update(
                ready=False, importing=False, error=message, event_count=0
            )
            raise RuntimeError(message)

        rocks_path.rename(backup_path)
        STATE_STATUS.update(
            ready=False, importing=True, error=None, event_count=0
        )
        try:
            import_jsonl_tape(JSONL_PATH, rocks_path)
        except BaseException as exc:
            try:
                if rocks_path.exists():
                    RocksDb.open(rocks_path).destroy()
                if rocks_path.exists():
                    raise RuntimeError(
                        f"incomplete projection could not be removed: {rocks_path}"
                    )
                backup_path.rename(rocks_path)
            except BaseException as rollback_exc:
                message = (
                    f"rebuild failed ({exc}); automatic rollback failed "
                    f"({rollback_exc}); backup retained at {backup_path}"
                )
                STATE_STATUS.update(
                    ready=False,
                    importing=False,
                    error=message,
                    event_count=0,
                )
                raise RuntimeError(message) from rollback_exc
            STATE_STATUS.update(
                ready=False,
                importing=False,
                error=str(exc),
                event_count=existing_count,
            )
            raise
        finally:
            STATE_STATUS["importing"] = False

    if state_db is not None:
        count = ROOT.events.len(state_db)
        STATE_STATUS.update(
            ready=True, importing=False, error=None, event_count=count
        )
        return dict(STATE_STATUS)

    STATE_STATUS.update(ready=False, importing=False, error=None, event_count=0)
    probe = RocksDb.open(ROCKS_PATH)
    count = ROOT.events.len(probe)
    probe.close()

    should_import = os.environ.get("IMPORT_JSONL_ON_EMPTY") == "1"
    if count == 0 and should_import:
        if not JSONL_PATH.is_file():
            message = f"archival tape not found: {JSONL_PATH}"
            STATE_STATUS["error"] = message
            raise RuntimeError(message)
        STATE_STATUS["importing"] = True
        try:
            import_jsonl_tape(JSONL_PATH, ROCKS_PATH)
        except BaseException as exc:
            STATE_STATUS.update(importing=False, error=str(exc))
            raise
        finally:
            STATE_STATUS["importing"] = False

    state_db = RocksDb.open(ROCKS_PATH)
    count = ROOT.events.len(state_db)
    STATE_STATUS.update(
        ready=True, importing=False, error=None, event_count=count
    )
    return dict(STATE_STATUS)


# ===========================================================================
# §1e. EVIDENCE — content-addressed, append-only, publicly linkable
# ===========================================================================
#
# Every epoch, commit, comparison input, provider attempt, and judgment is
# recorded as Evidence. Authoritative payloads keep exact bytes as base64 plus
# SHA-256; decoded text is for display only.


def _canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bytes_blob(data: bytes | str) -> dict:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return {
        "encoding": "base64",
        "data": base64.b64encode(raw).decode("ascii"),
        "byte_length": len(raw),
        "sha256": _sha256_hex(raw),
        "text": raw.decode("utf-8", "replace"),
    }


def _decode_blob(blob: dict | None) -> bytes:
    if not blob:
        return b""
    if blob.get("encoding") == "blob" and blob.get("sha256"):
        return ROOT.blobs.key(blob["sha256"]).get(_db()) or b""
    return base64.b64decode(blob["data"].encode("ascii"))


def _externalize_blob(blob: dict) -> tuple[dict, tuple[str, bytes] | None]:
    """Replace an inline base64 blob with a content-addressed reference."""
    if blob.get("encoding") != "base64" or not isinstance(blob.get("data"), str):
        return blob, None
    raw = base64.b64decode(blob["data"].encode("ascii"))
    sha = blob.get("sha256") or _sha256_hex(raw)
    return {
        "encoding": "blob",
        "sha256": sha,
        "byte_length": len(raw),
    }, (sha, raw)


_HEAVY_BLOB_FIELDS = {"patch", "prompt", "request", "response", "diff"}


def _externalize_payload(payload: dict) -> tuple[dict, list[tuple[str, bytes]]]:
    """Replace recursively nested heavy fields with content-addressed refs."""
    blobs: list[tuple[str, bytes]] = []

    def walk(value, field: str | None = None):
        if field in _HEAVY_BLOB_FIELDS and isinstance(value, (str, bytes)):
            raw = value.encode("utf-8") if isinstance(value, str) else value
            sha = _sha256_hex(raw)
            blobs.append((sha, raw))
            return {
                "encoding": "blob",
                "sha256": sha,
                "byte_length": len(raw),
            }
        if isinstance(value, dict):
            if field in _HEAVY_BLOB_FIELDS:
                ref, blob = _externalize_blob(value)
                if blob:
                    blobs.append(blob)
                    return ref
            return {key: walk(child, key) for key, child in value.items()}
        if isinstance(value, list):
            return [walk(child) for child in value]
        return value

    return walk(payload), blobs


def _hydrate_heavy_fields(value, field: str | None = None):
    """Restore ref-backed text fields for computation-heavy domain objects."""
    if (
        field in _HEAVY_BLOB_FIELDS
        and isinstance(value, dict)
        and value.get("encoding") == "blob"
    ):
        return _decode_blob(value).decode("utf-8", "replace")
    if isinstance(value, dict):
        return {
            key: _hydrate_heavy_fields(child, key)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_hydrate_heavy_fields(child) for child in value]
    return value


def _content_id(prefix: str, material) -> str:
    digest = _sha256_hex(_canonical_json(material) if not isinstance(material, bytes) else material)
    return f"{prefix}_{digest}"


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _evidence_path(kind: str, entity_id: str) -> str:
    routes = {
        "epoch": f"/epochs/{entity_id}",
        "commit": f"/commits/{entity_id}",
        "comparison": f"/comparisons/{entity_id}",
        "attempt": f"/attempts/{entity_id}",
        "judgment": f"/judgments/{entity_id}",
        "event": f"/events/{entity_id}",
    }
    return routes[kind]


def _evidence_url(kind: str, entity_id: str) -> str:
    return PUBLIC_BASE_URL + _evidence_path(kind, entity_id)


def _chain_digest(row: dict) -> str | None:
    if row.get("type") == "evidence":
        return str(row["event_id"]).split("_", 1)[-1]
    if row.get("type") in {"gitdiscovery", "emission"}:
        return _sha256_hex(_canonical_json(row))
    return None


def _public_repo_row(repo: dict) -> dict:
    """Strip credential-bearing clone URLs from published config."""
    url = repo["url"]
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        url = f"{scheme}://{rest.split('@', 1)[-1]}"
    return {"id": repo["id"], "url": url, "refs": repo["refs"]}


def _ledger_lock_path() -> pathlib.Path:
    env = os.environ.get("LEDGER_LOCK_PATH")
    if env:
        return pathlib.Path(env)
    return pathlib.Path(str(ROCKS_PATH) + ".lock")


_ENTITY_FIELDS = (
    "commit_id", "comparison_id", "attempt_id", "judgment_id",
    "ranking_run_id", "snapshot_id", "oid",
)


def _event_writes(
    row: dict,
    *,
    blobs: list[tuple[str, bytes]] | None = None,
    chain_digest: str | None = None,
) -> list:
    """Lower one tape row to canonical storage plus all direct indexes."""
    writes = [ROOT.events.push(row)]
    event_type = row.get("type")
    epoch = row.get("epoch")
    if isinstance(epoch, int):
        writes.append(ROOT.epochs.key(epoch).set(True))

    if event_type == "evidence":
        event_id = row["event_id"]
        kind = row["kind"]
        payload = row.get("payload") or {}
        writes.extend([
            ROOT.evidence_by_id.key(event_id).set(row),
            ROOT.event_ids_by_kind.key(kind).push(event_id),
            ROOT.event_ids_by_epoch.key(int(row["epoch"])).push(event_id),
            ROOT.evidence_count_by_epoch.key(int(row["epoch"])).add(1),
        ])
        if kind == "git.discovery_completed":
            writes.append(
                ROOT.discovery_evidence_by_epoch
                .key(int(row["epoch"])).set(event_id)
            )
        for field in _ENTITY_FIELDS:
            value = payload.get(field)
            if value is not None:
                writes.extend([
                    ROOT.entity_event_ids.key(field).key(str(value)).set(event_id),
                    ROOT.evidence_by_entity
                    .key((kind, field, str(value))).set(event_id),
                ])
        comparison_id = payload.get("comparison_id")
        if kind == "llm.judgment" and comparison_id:
            writes.append(
                ROOT.judgment_ids_by_comparison
                .key(str(comparison_id)).push(event_id)
            )
            if payload.get("attempt_id"):
                writes.append(
                    ROOT.judgment_id_by_attempt
                    .key(str(payload["attempt_id"])).set(event_id)
                )
        if kind.startswith("llm.attempt_") and comparison_id:
            writes.append(
                ROOT.attempt_ids_by_comparison
                .key(str(comparison_id)).push(event_id)
            )
        if kind == "comparison.input":
            comparison_id = payload.get("comparison_id")
            if comparison_id:
                for side_name in ("side_a", "side_b"):
                    side = payload.get(side_name) or {}
                    commit_ids = {
                        side.get("commit_id"),
                        *(side.get("commit_ids") or []),
                    }
                    for commit_id in commit_ids - {None}:
                        writes.append(
                            ROOT.comparison_ids_by_commit
                            .key(str(commit_id)).push(str(comparison_id))
                        )

    elif event_type == "emission":
        epoch = int(row["epoch"])
        writes.extend([
            ROOT.emissions_by_epoch.key(epoch).set(row),
            ROOT.emission_epochs.key(epoch).set(True),
            ROOT.latest_emission.set(row),
            ROOT.meta.total_emitted.set(
                str(CONTRIBUTOR_POOL - Decimal(row["pool_after"]))
            ),
        ])

    elif event_type == "gitdiscovery":
        epoch = int(row["epoch"])
        writes.extend([
            ROOT.discoveries_by_epoch.key(epoch).set(row),
            ROOT.discovery_epochs.key(epoch).set(True),
            ROOT.latest_discovery.set(row),
        ])
        for observation in row.get("observations") or []:
            oid = observation.get("oid")
            if oid:
                writes.append(ROOT.seen_git_oids.key(oid).set(True))
            patch_identity = observation.get("patch_identity")
            canonical_oid = observation.get("canonical_patch_oid")
            if patch_identity and canonical_oid:
                writes.append(
                    ROOT.canonical_patch_oids.key(patch_identity).set(canonical_oid)
                )

    digest = chain_digest if chain_digest is not None else _chain_digest(row)
    if digest:
        writes.append(ROOT.meta.chain_head.set(digest))
    for sha, raw in blobs or []:
        writes.append(ROOT.blobs.key(sha).set(raw))
    return writes


def _typed(row: dict | None):
    return from_dict(row) if row else None


def _evidence_from_ids(ids: list[str]) -> list[Evidence]:
    rows = []
    db = _db()
    for event_id in ids:
        event_ = _typed(ROOT.evidence_by_id.key(event_id).get(db))
        if isinstance(event_, Evidence):
            rows.append(event_)
    return rows


def _evidence_for_kind(kind: str) -> list[Evidence]:
    path = ROOT.event_ids_by_kind.key(kind)
    return _evidence_from_ids(path.iter(_db()) if path.len(_db()) else [])


def _evidence_for_epoch(epoch: int) -> list[Evidence]:
    path = ROOT.event_ids_by_epoch.key(epoch)
    return _evidence_from_ids(path.iter(_db()) if path.len(_db()) else [])


def verify_rocks_projection(db: RocksDb, expected: dict | None = None) -> dict:
    """Verify counts, chain end, and representative direct lookups."""
    count = ROOT.events.len(db)
    report = {
        "event_count": count,
        "chain_head": ROOT.meta.chain_head.get(db),
        "evidence_count": ROOT.evidence_by_id.len(db),
        "emission_count": ROOT.emissions_by_epoch.len(db),
        "discovery_count": ROOT.discoveries_by_epoch.len(db),
        "epoch_count": ROOT.epochs.len(db),
        "representative_lookups": {},
    }
    if count:
        first = ROOT.events.get(db, 0)
        last = ROOT.events.get(db, count - 1)
        report["first_type"] = first.get("type") if first else None
        report["last_type"] = last.get("type") if last else None
    for label, path in (
        ("latest_emission", ROOT.latest_emission),
        ("latest_discovery", ROOT.latest_discovery),
    ):
        row = path.get(db)
        report["representative_lookups"][label] = bool(row)
    evidence_page = ROOT.evidence_by_id.keys_page(db, limit=1)
    if evidence_page.items:
        event_id = evidence_page.items[0]
        report["representative_lookups"]["evidence_event_id"] = event_id
        report["representative_lookups"]["evidence_found"] = bool(
            ROOT.evidence_by_id.key(event_id).get(db)
        )

    if expected:
        checks = {
            "event_count": report["event_count"] == expected["event_count"],
            "chain_head": report["chain_head"] == expected["chain_head"],
            "evidence_count": (
                report["evidence_count"] == expected["evidence_count"]
            ),
            "emission_count": (
                report["emission_count"] == expected["emission_count"]
            ),
            "discovery_count": (
                report["discovery_count"] == expected["discovery_count"]
            ),
            "chain_links": expected.get("chain_mismatches", 0) == 0,
        }
        report["checks"] = checks
        report["ok"] = all(checks.values())
    else:
        report["ok"] = True
    return report


def import_jsonl_tape(
    tape_path: pathlib.Path | str,
    rocks_path: pathlib.Path | str,
    *,
    batch_size: int = 100,
    force: bool = False,
) -> dict:
    """One-shot streaming projection import.

    Reads one JSON object per line and commits bounded groups. On any failure the
    incomplete Rocks projection is destroyed; rerun from zero.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    tape_path = pathlib.Path(tape_path)
    rocks_path = pathlib.Path(rocks_path)
    if force and rocks_path.exists():
        RocksDb.open(rocks_path).destroy()

    db = RocksDb.open(rocks_path)
    if ROOT.events.len(db):
        db.close()
        raise RuntimeError(
            f"Rocks projection is not empty: {rocks_path}; use --force to rebuild"
        )

    digest = hashlib.sha256()
    expected = {
        "event_count": 0,
        "evidence_count": 0,
        "emission_count": 0,
        "discovery_count": 0,
        "chain_head": None,
        "chain_mismatches": 0,
    }
    batch = db.batch()
    pending = 0
    pending_evidence_ids: set[str] = set()
    pending_emission_epochs: set[int] = set()
    pending_discovery_epochs: set[int] = set()
    pending_blob_bytes = 0
    try:
        with tape_path.open("rb") as tape:
            for line_number, raw_line in enumerate(tape, start=1):
                digest.update(raw_line)
                if not raw_line.strip():
                    continue
                try:
                    row = json.loads(raw_line)
                except Exception as exc:
                    raise ValueError(
                        f"invalid JSONL at line {line_number}: {exc}"
                    ) from exc
                if not isinstance(row, dict) or not isinstance(row.get("type"), str):
                    raise ValueError(f"invalid tape event at line {line_number}")

                if row["type"] == "evidence":
                    event_id = row.get("event_id")
                    if not isinstance(event_id, str):
                        raise ValueError(
                            f"evidence missing event_id at line {line_number}"
                        )
                    if (
                        event_id in pending_evidence_ids
                        or ROOT.evidence_by_id.contains(db, event_id)
                    ):
                        raise ValueError(
                            f"duplicate evidence event_id {event_id} "
                            f"at line {line_number}"
                        )
                    pending_evidence_ids.add(event_id)
                    expected["evidence_count"] += 1
                    previous = row.get("previous_event_sha256")
                    expected_previous = expected["chain_head"] or "0" * 64
                    if previous != expected_previous:
                        expected["chain_mismatches"] += 1
                elif row["type"] == "emission":
                    epoch = int(row["epoch"])
                    if (
                        epoch in pending_emission_epochs
                        or ROOT.emissions_by_epoch.contains(db, epoch)
                    ):
                        raise ValueError(
                            f"duplicate emission epoch {epoch} "
                            f"at line {line_number}"
                        )
                    pending_emission_epochs.add(epoch)
                    expected["emission_count"] += 1
                elif row["type"] == "gitdiscovery":
                    epoch = int(row["epoch"])
                    if (
                        epoch in pending_discovery_epochs
                        or ROOT.discoveries_by_epoch.contains(db, epoch)
                    ):
                        raise ValueError(
                            f"duplicate discovery epoch {epoch} "
                            f"at line {line_number}"
                        )
                    pending_discovery_epochs.add(epoch)
                    expected["discovery_count"] += 1

                chain = _chain_digest(row)
                if chain:
                    expected["chain_head"] = chain
                stored_row, blobs = _externalize_payload(row)
                pending_blob_bytes += sum(len(raw) for _, raw in blobs)
                batch.extend(
                    _event_writes(
                        stored_row,
                        blobs=blobs,
                        chain_digest=chain,
                    )
                )
                expected["event_count"] += 1
                pending += 1
                if pending >= batch_size or pending_blob_bytes >= 16 * 1024 * 1024:
                    batch.commit(Durability.WAL_ONLY)
                    batch = db.batch()
                    pending = 0
                    pending_blob_bytes = 0
                    pending_evidence_ids.clear()
                    pending_emission_epochs.clear()
                    pending_discovery_epochs.clear()

        if pending:
            batch.commit(Durability.WAL_ONLY)
        db.apply(
            [
                ROOT.meta.imported_count.set(expected["event_count"]),
                ROOT.meta.tape_sha256.set(digest.hexdigest()),
            ],
            Durability.SYNC_WAL,
        )
        report = verify_rocks_projection(db, expected)
        report.update({
            "tape_path": str(tape_path),
            "rocks_path": str(rocks_path),
            "tape_sha256": digest.hexdigest(),
            "chain_mismatches": expected["chain_mismatches"],
        })
        if not report["ok"]:
            raise RuntimeError(f"projection verification failed: {report['checks']}")
        db.close()
        return report
    except BaseException:
        try:
            db.destroy()
        finally:
            pass
        raise


async def append_evidence(epoch: int, kind: str, payload: dict) -> Evidence:
    """Atomically update canonical event, chain head, blobs, and indexes."""
    # Logical identity ignores chain links / wall clock so restarts stay idempotent.
    event_id = _content_id("ev", {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "epoch": epoch,
        "kind": kind,
        "payload": payload,
    })
    async with _LEDGER_LOCK:
        db = _db()
        existing = _typed(ROOT.evidence_by_id.key(event_id).get(db))
        if isinstance(existing, Evidence):
            return existing
        stored_payload, blobs = _externalize_payload(payload)
        evidence = Evidence(
            schema_version=EVIDENCE_SCHEMA_VERSION,
            event_id=event_id,
            epoch=epoch,
            kind=kind,
            recorded_at_ms=int(time.time() * 1000),
            previous_event_sha256=(
                ROOT.meta.chain_head.get(db) or "0" * 64
            ),
            payload=stored_payload,
        )
        row = to_dict(evidence)
        db.apply(_event_writes(row, blobs=blobs), Durability.SYNC_WAL)
        result = evidence
    await broadcast_audit(
        kind,
        f"{kind}: {payload.get('summary') or event_id[:24]}",
        phase=PROCESS_STATE.get("phase"),
        progress=PROCESS_STATE.get("progress"),
        evidence_event_id=result.event_id,
        evidence_url=_evidence_url("event", result.event_id),
    )
    return result


async def _append_typed_event(event_) -> object:
    """Append a non-Evidence tape event and its indexes atomically."""
    async with _LEDGER_LOCK:
        if isinstance(event_, Emission):
            existing = _emission_for_epoch(event_.epoch)
            if existing is not None:
                return existing
        if isinstance(event_, GitDiscovery):
            existing = _discovery_for_epoch(event_.epoch)
            if existing is not None:
                return existing
        _db().apply(_event_writes(to_dict(event_)), Durability.SYNC_WAL)
    return event_


def find_evidence(event_id: str) -> Evidence | None:
    row = _typed(ROOT.evidence_by_id.key(event_id).get(_db()))
    return row if isinstance(row, Evidence) else None


def find_evidence_payload(kind: str, key: str, value: str) -> Evidence | None:
    event_id = ROOT.evidence_by_entity.key((kind, key, value)).get(_db())
    if event_id:
        event_ = find_evidence(event_id)
        if event_ and event_.kind == kind:
            return event_
    return None


def commit_id_for_oid(oid: str) -> str:
    return _content_id("c", {"oid": oid})


def comparison_id_for(material: dict) -> str:
    return _content_id("cmp", material)


def attempt_id_for(comparison_id: str, model_id: str, attempt_number: int) -> str:
    return _content_id("att", {
        "comparison_id": comparison_id,
        "model_id": model_id,
        "attempt_number": attempt_number,
    })


def judgment_id_for(material: dict) -> str:
    return _content_id("jud", material)


def _blob_text(blob) -> str:
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob
    if isinstance(blob, dict):
        if isinstance(blob.get("text"), str):
            return blob["text"]
        try:
            return _decode_blob(blob).decode("utf-8", "replace")
        except Exception:
            return ""
    return str(blob)


def _blob_preview(blob, limit: int = 12_000) -> str:
    text = _blob_text(blob)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n… preview truncated; {len(text) - limit:,} characters omitted"


def _discovery_for_epoch(epoch: int) -> GitDiscovery | None:
    stored = ROOT.discoveries_by_epoch.key(epoch).get(_db())
    row = _typed(_hydrate_heavy_fields(stored) if stored else None)
    return row if isinstance(row, GitDiscovery) else None


def _emission_for_epoch(epoch: int) -> Emission | None:
    row = _typed(ROOT.emissions_by_epoch.key(epoch).get(_db()))
    return row if isinstance(row, Emission) else None


def _epochs_in_ledger() -> list[int]:
    return sorted(int(epoch) for epoch in ROOT.epochs.keys(_db()))


def build_pairwise_prompt(side_a: dict, side_b: dict) -> str:
    return f"""You are a constitutional council ranking individual git commits for ownership allocation.

Compare these two commits. Decide which contributed more lasting value to the project.

Judge substance, not spectacle:
- Prefer correct, lasting design and real bugfixes over churn, formatting, renames, or generated noise.
- Prefer clarity and necessity over sheer line count. A small precise change can beat a large diffuse one.
- Do not favor a side merely because its patch is longer or noisier.
- Weight what the change does for the project, not the contributor's name.

Return ONLY a JSON object: {{"winner": "A" or "B", "ratio": "N:M", "explanation": "..."}}
The explanation must cite concrete differences in the patches (1-3 sentences).

Side A — contributor: {side_a.get('contributor', '?')}
Side A — commit message:
{side_a['message']}

Side A — unified diff (full patch):
{side_a['diff']}

Side B — contributor: {side_b.get('contributor', '?')}
Side B — commit message:
{side_b['message']}

Side B — unified diff (full patch):
{side_b['diff']}"""


# ===========================================================================
# §2. LASKAR POLYNOMIAL — the planet decides when epochs turn
# ===========================================================================
# NOTE: implemented with Decimal, not SymPy Rational. Iterating the polynomial
# with exact rationals makes denominators grow multiplicatively each epoch
# (10 iterations ~10s, 300 iterations never finishes). Decimal with prec=50
# matches the Laskar coefficients to ~1e-30 days and is stable for thousands
# of epochs. Round-half-up to the nearest ms preserves the old API.

_J2000_UNIX_MS = Decimal("946728000000")
_JULIAN_CENTURY_MS = Decimal("3155760000000")  # 36525 days, in ms
_DAY_MS = Decimal("86400000")
_A0 = Decimal("365.2421896698")
_A1 = Decimal("-6.15359e-6")
_A2 = Decimal("-7.29e-10")
_A3 = Decimal("2.64e-10")

# Back-compat aliases for tests/other modules that imported these names.
j2000_unix_ms = sp.Integer(946728000000)
julian_century_ms = sp.Integer(36525 * 86400 * 1000)
day_ms = sp.Integer(86400000)
a0 = sp.Rational("365.2421896698")
a1 = sp.Rational("-6.15359e-6")
a2 = sp.Rational("-7.29e-10")
a3 = sp.Rational("2.64e-10")
genesis_ms = sp.Integer(GENESIS_MS)


def _round_half_up(x: Decimal) -> int:
    return int((x + Decimal("0.5")).to_integral_value(rounding="ROUND_FLOOR"))


def round_sympy_ms(x) -> int:
    return _round_half_up(Decimal(str(x)) if not isinstance(x, Decimal) else x)


def T_from_unix_ms(ms) -> Decimal:
    return (Decimal(str(ms)) - _J2000_UNIX_MS) / _JULIAN_CENTURY_MS


def tropical_epoch_ms(unix_ms) -> Decimal:
    T = T_from_unix_ms(unix_ms)
    days = _A0 + _A1 * T + _A2 * T * T + _A3 * T * T * T
    return days * _DAY_MS / Decimal("12")


def epoch_boundary(n):
    boundary = Decimal(str(genesis_ms))
    for _ in range(n):
        boundary += tropical_epoch_ms(boundary)
    return _round_half_up(boundary)


def current_epoch():
    now = int(time.time() * 1000)
    boundary = Decimal(str(genesis_ms))
    for e in range(10000):
        next_boundary = boundary + tropical_epoch_ms(boundary)
        if _round_half_up(next_boundary) > now:
            return e, _round_half_up(boundary), _round_half_up(next_boundary)
        boundary = next_boundary
    return -1, 0, 0


# ===========================================================================
# §3. RANK CENTRALITY — 10 lines, same algorithm at every layer
# ===========================================================================

import numpy as np


def rank_centrality(pairs):
    items = set()
    for w, l, wr, lr in pairs:
        items.add(w)
        items.add(l)
    n = max(items) + 1
    W = np.zeros((n, n))
    for w, l, wr, lr in pairs:
        W[l][w] += wr   # A[loser][winner] = preference for winner over loser
        W[w][l] += lr   # A[winner][loser] = preference for loser over winner
    P = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            if W[i][j] + W[j][i] > 0:
                P[i][j] = W[i][j] / (W[i][j] + W[j][i])
    w_max = max(P.sum(axis=1)) or 1
    P = P / w_max
    for i in range(n):
        P[i][i] = 1 - P[i].sum()
    pi = np.ones(n) / n
    for _ in range(10000):
        pi_next = pi @ P
        if np.allclose(pi, pi_next, atol=1e-12):
            break
        pi = pi_next
    return pi / pi.sum()


# ===========================================================================
# §3b. PAIR SELECTION — spanning tree + zip sort
#
# Two-phase algorithm so we make the minimum useful comparisons:
#
# Phase 1 — spanning tree: union-find, compare only pairs that bridge
#   disconnected components. Exactly N-1 comparisons for N authors.
#
# Phase 2 — zip sort: repeatedly find the first adjacent pair in the
#   current ranking that has no direct comparison result yet and compare
#   it.  The ranking is re-derived from scratch before every step, so a
#   comparison at position k cannot silently skip a newly-adjacent
#   uncovered pair at position k-1.
#   Terminates when every adjacent slot in the current ranking already
#   has at least one LLM-reasoned comparison result.  No pair is ever
#   compared more than once.
#
# Progress is reported in terms of phase/pass/step, not total pairs,
# because the total is unknown until the zip sort converges.
# ===========================================================================

class UnionFind:
    def __init__(self, n: int):
        self._parent = list(range(n))
        self._rank = [0] * n

    def find(self, x: int) -> int:
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])  # path compression
        return self._parent[x]

    def union(self, x: int, y: int) -> None:
        px, py = self.find(x), self.find(y)
        if px == py:
            return
        if self._rank[px] < self._rank[py]:
            px, py = py, px
        self._parent[py] = px
        if self._rank[px] == self._rank[py]:
            self._rank[px] += 1

    def connected(self, x: int, y: int) -> bool:
        return self.find(x) == self.find(y)

    def num_components(self) -> int:
        return sum(1 for i in range(len(self._parent)) if self.find(i) == i)


async def pairwise_rank(n: int, compare_fn, progress_fn=None) -> list:
    """
    Two-phase pairwise ranking. Returns list of (w, l, wr, lr) pairs
    suitable for rank_centrality().

    compare_fn: async (i, j) -> list[(winner_idx, loser_idx, w_ratio, l_ratio)]
      Returns a list so multiple model votes per comparison are supported.

    progress_fn: async (event: dict) -> None  (optional)
      event has keys: phase, step, total, and for zip: pass (always 1).

    # @b1050ff3-000c-4a8f-8b46-c5ef91d696c7:cursor:anthropic/claude-sonnet-4-5
    """
    if n <= 1:
        return []

    pairs = []
    compared: set = set()  # frozensets of already-compared index pairs

    async def compare(i, j):
        results = await compare_fn(i, j)
        pairs.extend(results)
        compared.add(frozenset((i, j)))
        # Resumed comparisons may be satisfied entirely from RocksDB without
        # an I/O await. Yield explicitly so ranking never starves HTTP/SSE.
        await asyncio.sleep(0)
        return results

    # --- Phase 1: spanning tree ---
    uf = UnionFind(n)
    for i in range(n - 1):
        if not uf.connected(i, i + 1):
            await compare(i, i + 1)
            uf.union(i, i + 1)
        if progress_fn:
            await progress_fn({
                "phase": "spanning_tree",
                "step": i + 1,
                "total": n - 1,
                "ranking": list(range(n)),
                "voted_count": i + 2,
            })

    # --- Phase 2: zip sort ---
    # Re-derive the ranking from scratch before every step.  A comparison at
    # position k can shift rank_centrality scores so that a brand-new,
    # never-compared pair appears at position k-1; the old pass-based loop
    # would skip back to catch it only on the next full pass, re-comparing
    # already-settled pairs along the way.
    #
    # Termination: every adjacent slot in the current ranking is occupied by
    # a pair that already has at least one direct comparison result.
    # There is exactly one logical pass; step is how far down the ranking we
    # had to scan to find the first uncovered adjacent pair (1 = top of zip).
    while True:
        scores = await asyncio.to_thread(rank_centrality, pairs)
        ranking = sorted(range(n), key=lambda idx: scores[idx], reverse=True)

        target = None
        for pos in range(n - 1):
            a, b = ranking[pos], ranking[pos + 1]
            if frozenset((a, b)) not in compared:
                target = (pos, a, b)
                break

        if target is None:
            if progress_fn:
                await progress_fn({
                    "phase": "zip",
                    "pass": 1,
                    "step": n - 1,
                    "total": n - 1,
                    "ranking": ranking,
                    "voted_count": n,
                    "complete": True,
                })
            break  # every adjacent edge has at least one LLM-reasoned result

        pos, a, b = target
        await compare(a, b)

        if progress_fn:
            await progress_fn({
                "phase": "zip",
                "pass": 1,
                "step": pos + 1,
                "total": n - 1,
                "ranking": ranking,
                "voted_count": pos + 2,
            })

    return pairs


# ===========================================================================
# §4. COMMIT RANKING — council of LLMs, pairwise comparisons
#
# A note on security and prompt injection:
# Because the LLM council reads raw commit messages and full unified diffs
# (code, comments, strings in the patch), the system is technically vulnerable
# to prompt injection (e.g., instructions in commit messages or in diff text).
#
# The defense mechanism against this is the Benevolent Dictator For Life (BDFL)
# of the repository. Commits with injected prompts simply will not be merged.
# While relying on human curation might seem to contradict "decentralization,"
# this bottleneck is an unavoidable reality of all FOSS projects: a human
# maintainer must ultimately decide what code is worthy of the master branch.
# The LLMs don't decide what gets merged; they only price what the BDFL accepts.
# ===========================================================================

def _openrouter_id_from_item_body(body: str) -> str | None:
    """Item body is often `https://openrouter.ai/<provider>/<model>`."""
    if not body:
        return None
    b = body.strip()
    for prefix in ("https://openrouter.ai/", "http://openrouter.ai/"):
        if b.startswith(prefix):
            return b[len(prefix) :].strip().strip("/") or None
    return None


async def _slug_item_body(client: httpx.AsyncClient, item_path: str) -> str | None:
    p = item_path.lstrip("/")
    r = await client.get(f"{SLUG_SOCIAL_BASE_URL}/api/v0/item", params={"item": p})
    if r.status_code != 200:
        return None
    return r.json().get("body")


async def _fetch_models_from_slug_rank_parent(
    client: httpx.AsyncClient, parent: str, n: int
) -> list[str]:
    r = await client.get(
        f"{SLUG_SOCIAL_BASE_URL}/api/v0/rank", params={"parent": parent}
    )
    r.raise_for_status()
    data = r.json()
    if data.get("ok") is False:
        return []

    seen: set[str] = set()
    out: list[str] = []

    ranked_rows: list[tuple[float, str]] = []
    for comp in data.get("components") or []:
        for row in comp.get("ranking") or []:
            ranked_rows.append((float(row["score"]), row["item"]))
    ranked_rows.sort(key=lambda x: -x[0])

    for _, item_path in ranked_rows:
        body = await _slug_item_body(client, item_path)
        oid = _openrouter_id_from_item_body(body or "")
        if oid and oid not in seen:
            seen.add(oid)
            out.append(oid)
            if len(out) >= n:
                return out

    for item_path in data.get("unranked_items") or []:
        body = await _slug_item_body(client, item_path)
        oid = _openrouter_id_from_item_body(body or "")
        if oid and oid not in seen:
            seen.add(oid)
            out.append(oid)
            if len(out) >= n:
                break
    return out


def _model_provider(model_id: str) -> str:
    return (model_id or "").split("/", 1)[0] or model_id


async def _fetch_models_openrouter_only(
    client: httpx.AsyncClient, n: int, exclude: set[str]
) -> list[str]:
    """Top up council seats, preferring provider diversity over same-lab clones."""
    key = (OPENROUTER_API_KEY or "").strip()
    if not key or n <= 0:
        return []
    resp = await client.get(
        f"{OPENROUTER_BASE_URL}/api/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    resp.raise_for_status()
    models = resp.json()["data"]
    chat_models = [m for m in models if "chat" in m.get("id", "")]
    chat_models.sort(key=lambda m: m.get("created", 0), reverse=True)
    out: list[str] = []
    used_providers = {_model_provider(m) for m in exclude}
    # Pass 1: one seat per unused provider.
    for m in chat_models:
        mid = m["id"]
        if mid in exclude or mid in out:
            continue
        provider = _model_provider(mid)
        if provider in used_providers:
            continue
        out.append(mid)
        used_providers.add(provider)
        if len(out) >= n:
            return out
    # Pass 2: fill remaining seats with newest chat models.
    for m in chat_models:
        mid = m["id"]
        if mid in exclude or mid in out:
            continue
        out.append(mid)
        if len(out) >= n:
            break
    return out


def _preferred_council_models() -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for model_id in PREFERRED_COUNCIL_MODELS or []:
        mid = str(model_id).strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def _with_preferred_council(models: list[str]) -> list[str]:
    """Preferred seats first, then any additional council members."""
    out: list[str] = []
    seen: set[str] = set()
    for model_id in _preferred_council_models() + list(models):
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        out.append(model_id)
    return out


async def fetch_top_models(n=3):
    preferred = _preferred_council_models()
    target = max(n, len(preferred))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0)
    ) as client:
        got: list[str] = list(preferred)
        exclude = set(got)
        if len(got) < target and SLUG_MODEL_RANK_PARENT:
            try:
                from_slug = await _fetch_models_from_slug_rank_parent(
                    client, SLUG_MODEL_RANK_PARENT, target - len(got)
                )
                for mid in from_slug:
                    if mid in exclude:
                        continue
                    got.append(mid)
                    exclude.add(mid)
                    if len(got) >= target:
                        break
            except Exception:
                pass
        if len(got) < target and (OPENROUTER_API_KEY or "").strip():
            rest = await _fetch_models_openrouter_only(
                client, target - len(got), exclude=exclude
            )
            got.extend(rest)
        return _with_preferred_council(got)[:target]


def _parse_pairwise_json(content: str) -> dict:
    """Parse council JSON; tolerate markdown fences and leading prose."""
    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        result = json.loads(text[start : end + 1])
    if not isinstance(result, dict):
        raise ValueError("pairwise response must be a JSON object")
    return result


def _retry_llm_pairwise(exc: BaseException) -> bool:
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 425, 429, 500, 502, 503, 504)
    return isinstance(exc, httpx.RequestError)


def _retry_wait_seconds(attempt_number: int) -> float:
    return min(120.0, float(2 ** (attempt_number - 1)))


async def llm_pairwise_compare(
    model_id,
    side_a,
    side_b,
    *,
    epoch: int | None = None,
    comparison_id: str | None = None,
    persist: bool = False,
):
    """Pairwise LLM compare. When persist=True, every attempt is ledger evidence."""
    prompt = build_pairwise_prompt(side_a, side_b)
    request_obj = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
    }
    request_bytes = _canonical_json(request_obj)
    max_attempts = 6
    last_error = None
    for attempt_number in range(1, max_attempts + 1):
        attempt_id = attempt_id_for(
            comparison_id or "ad-hoc", model_id, attempt_number
        )
        if persist and epoch is not None and comparison_id is not None:
            await append_evidence(epoch, "llm.attempt_started", {
                "attempt_id": attempt_id,
                "comparison_id": comparison_id,
                "model_id": model_id,
                "attempt_number": attempt_number,
                "summary": f"{model_id} attempt {attempt_number}",
                "request": _bytes_blob(request_bytes),
            })
        started = time.monotonic()
        raw_response = b""
        http_status = None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0)) as client:
                resp = await client.post(
                    f"{OPENROUTER_BASE_URL}/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    content=request_bytes,
                )
            http_status = resp.status_code
            raw_response = resp.content
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            result = _parse_pairwise_json(content)
            if result.get("winner") not in {"A", "B"}:
                raise ValueError("winner must be A or B")
            ratio_parts = str(result["ratio"]).split(":")
            if len(ratio_parts) != 2:
                raise ValueError("ratio must be N:M")
            winner_weight, loser_weight = float(ratio_parts[0]), float(ratio_parts[1])
            if winner_weight <= 0 or loser_weight <= 0:
                raise ValueError("ratio weights must be positive")
            if persist and epoch is not None and comparison_id is not None:
                await append_evidence(epoch, "llm.attempt_finished", {
                    "attempt_id": attempt_id,
                    "comparison_id": comparison_id,
                    "model_id": model_id,
                    "attempt_number": attempt_number,
                    "ok": True,
                    "http_status": http_status,
                    "response": _bytes_blob(raw_response),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "summary": f"{model_id} attempt {attempt_number} ok",
                })
                await append_evidence(epoch, "llm.judgment", {
                    "judgment_id": judgment_id_for({
                        "attempt_id": attempt_id,
                        "comparison_id": comparison_id,
                        "model_id": model_id,
                        "winner": result["winner"],
                        "ratio": result["ratio"],
                        "explanation": result.get("explanation", ""),
                    }),
                    "attempt_id": attempt_id,
                    "comparison_id": comparison_id,
                    "model_id": model_id,
                    "winner": result["winner"],
                    "ratio": result["ratio"],
                    "explanation": result.get("explanation", ""),
                    "summary": f"{model_id}: {result['winner']} ({result['ratio']})",
                })
            return result
        except Exception as exc:
            last_error = exc
            retryable = _retry_llm_pairwise(exc)
            if persist and epoch is not None and comparison_id is not None:
                await append_evidence(epoch, "llm.attempt_finished", {
                    "attempt_id": attempt_id,
                    "comparison_id": comparison_id,
                    "model_id": model_id,
                    "attempt_number": attempt_number,
                    "ok": False,
                    "retryable": retryable,
                    "http_status": http_status,
                    "response": _bytes_blob(raw_response) if raw_response else None,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "summary": f"{model_id} attempt {attempt_number} failed",
                })
            if retryable and attempt_number < max_attempts:
                await asyncio.sleep(_retry_wait_seconds(attempt_number))
                continue
            raise
    raise RuntimeError(f"council model failed: {model_id}") from last_error


# ===========================================================================
# §4b. GIT DISCOVERY — immutable reachability snapshots across repositories
# ===========================================================================
#
# Git timestamps cannot prove when a branch first reached a commit. The first
# snapshot therefore bootstraps history by committer time at GENESIS_MS. Every
# later snapshot uses the stronger rule: a commit enters exactly once, when it
# first becomes reachable from the union of configured refs.
#
# OIDs are deduplicated globally, then equivalent cherry-picks are deduplicated
# by Git's stable patch identity. Merges and empty commits are graph structure,
# not separately priced contributions. Discovery is all-or-nothing: if any
# repository cannot be mirrored and verified, no snapshot is appended.

GIT_DISCOVERY_SCHEMA_VERSION = 1
PATCH_IDENTITY_VERSION = "git-patch-id-stable-v1"
_DISCOVERY_LOCK = asyncio.Lock()


def _normalized_discovery_config() -> dict:
    repositories = []
    seen_ids = set()
    for raw in REPOSITORIES:
        repo_id = str(raw.get("id", ""))
        url = str(raw.get("url", ""))
        refs = sorted(set(str(x) for x in raw.get("refs", [])))
        if not re.fullmatch(r"[A-Za-z0-9._-]+", repo_id):
            raise ValueError(f"invalid repository id: {repo_id!r}")
        if repo_id in seen_ids:
            raise ValueError(f"duplicate repository id: {repo_id}")
        if not url or not refs or any(not r.startswith("refs/") for r in refs):
            raise ValueError(f"repository {repo_id} requires a URL and full ref patterns")
        seen_ids.add(repo_id)
        repositories.append({"id": repo_id, "url": url, "refs": refs})

    email_to_contributor = {}
    contributors = {}
    for contributor, emails in sorted(CONTRIBUTORS.items()):
        contributor = str(contributor)
        normalized = sorted(set(str(e).strip().lower() for e in emails))
        if not contributor or not normalized:
            raise ValueError("contributors require an id and at least one email")
        for email in normalized:
            if email in email_to_contributor:
                raise ValueError(f"email belongs to multiple contributors: {email}")
            email_to_contributor[email] = contributor
        contributors[contributor] = normalized

    repositories.sort(key=lambda r: r["id"])
    return {"repositories": repositories, "contributors": contributors}


def _config_digest(config: dict) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _ref_pattern_regex(pattern: str) -> re.Pattern:
    out = ""
    i = 0
    while i < len(pattern):
        if pattern[i:i + 2] == "**":
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}$")


def _git(repo: pathlib.Path | None, *args: str, input_bytes: bytes | None = None) -> bytes:
    command = [
        "git",
        "--no-replace-objects",
        "-c", "core.quotepath=true",
        "-c", "core.attributesFile=/dev/null",
        "-c", "diff.external=",
        "-c", "diff.renames=false",
        "-c", "diff.algorithm=myers",
        "-c", "diff.context=3",
    ]
    if repo is not None:
        command += ["-C", str(repo)]
    command += list(args)
    git_env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    if GITHUB_TOKEN:
        credential = base64.b64encode(
            f"x-access-token:{GITHUB_TOKEN}".encode()
        ).decode()
        git_env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
        })
    try:
        result = subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_env,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git command timed out: {args[0]}") from exc
    if result.returncode:
        error = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {args[0]} failed: {error}")
    return result.stdout


def _ensure_mirror(repo: dict) -> pathlib.Path:
    GIT_MIRROR_DIR.mkdir(parents=True, exist_ok=True)
    mirror = GIT_MIRROR_DIR / f"{repo['id']}.git"
    if not mirror.exists():
        _git(None, "clone", "--mirror", "--", repo["url"], str(mirror))
    else:
        actual_url = _git(mirror, "remote", "get-url", "origin").decode().strip()
        if actual_url != repo["url"]:
            raise RuntimeError(
                f"mirror URL mismatch for {repo['id']}: {actual_url!r}"
            )
    _git(mirror, "fetch", "--prune", "origin", "+refs/*:refs/*")
    _git(mirror, "fsck", "--connectivity-only", "--no-dangling")
    return mirror


def _matching_refs(mirror: pathlib.Path, patterns: list[str]) -> list[dict]:
    regexes = [_ref_pattern_regex(p) for p in patterns]
    lines = _git(
        mirror, "for-each-ref", "--format=%(refname)%00%(objectname)"
    ).decode("utf-8", "replace").splitlines()
    selected = []
    for line in lines:
        if not line:
            continue
        ref_name, direct_oid = line.split("\x00", 1)
        if not any(r.fullmatch(ref_name) for r in regexes):
            continue
        commit_oid = _git(
            mirror, "rev-parse", "--verify", f"{ref_name}^{{commit}}"
        ).decode().strip()
        selected.append({
            "name": ref_name,
            "direct_oid": direct_oid,
            "commit_oid": commit_oid,
        })
    if not selected:
        raise RuntimeError(f"no refs matched patterns {patterns!r}")
    return sorted(selected, key=lambda r: r["name"])


def _commit_metadata(mirror: pathlib.Path, oid: str) -> dict:
    raw = _git(
        mirror,
        "show",
        "-s",
        "--format=%H%x00%T%x00%P%x00%an%x00%ae%x00%at%x00%cn%x00%ce%x00%ct%x00%B",
        oid,
    ).decode("utf-8", "replace")
    fields = raw.split("\x00", 9)
    if len(fields) != 10:
        raise RuntimeError(f"could not parse commit metadata for {oid}")
    return {
        "oid": fields[0],
        "tree_oid": fields[1],
        "parent_oids": fields[2].split() if fields[2] else [],
        "author_name": fields[3],
        "author_email": fields[4].strip().lower(),
        "author_timestamp_ms": int(fields[5]) * 1000,
        "committer_name": fields[6],
        "committer_email": fields[7].strip().lower(),
        "committer_timestamp_ms": int(fields[8]) * 1000,
        "message": fields[9].rstrip("\n"),
    }


def _commit_patch(mirror: pathlib.Path, metadata: dict) -> tuple[str, str | None]:
    parents = metadata["parent_oids"]
    if len(parents) > 1:
        return "", None
    if parents:
        args = ("diff", "--patch", "--binary", "--full-index", "--no-renames",
                "--no-ext-diff", "--no-textconv", "--src-prefix=a/",
                "--dst-prefix=b/", parents[0], metadata["oid"], "--")
    else:
        args = ("diff-tree", "--root", "--patch", "--binary", "--full-index",
                "--no-renames", "--no-ext-diff", "--no-textconv",
                "--src-prefix=a/", "--dst-prefix=b/", "--no-commit-id",
                metadata["oid"], "--")
    patch_bytes = _git(mirror, *args)
    if not patch_bytes.strip():
        return "", None
    # Run patch-id outside the repository so SHA-1 and SHA-256 repositories use
    # the same canonical patch hash algorithm.
    patch_id_out = _git(
        None, "patch-id", "--stable", input_bytes=patch_bytes
    ).decode().strip()
    if patch_id_out:
        stable_id = patch_id_out.split()[0]
    else:
        stable_id = hashlib.sha256(patch_bytes).hexdigest()
    return patch_bytes.decode("utf-8", "replace"), f"{PATCH_IDENTITY_VERSION}:{stable_id}"


def _replayed_discovery_state(events: list) -> tuple[set[str], dict[str, str]]:
    seen_oids = set()
    seen_patches = {}
    for event_ in events:
        if not isinstance(event_, GitDiscovery):
            continue
        for observation in event_.observations:
            seen_oids.add(observation["oid"])
            patch_identity = observation.get("patch_identity")
            canonical_oid = observation.get("canonical_patch_oid")
            if patch_identity and canonical_oid:
                seen_patches.setdefault(patch_identity, canonical_oid)
    return seen_oids, seen_patches


def _build_discovery(
    epoch_n: int, boundary_ms: int, events: list | None = None
) -> GitDiscovery:
    config = _normalized_discovery_config()
    digest = _config_digest(config)
    if events is None:
        db = _db()
        initial = ROOT.latest_discovery.get(db) is None
        pending_patches: dict[str, str] = {}

        def oid_seen(oid: str) -> bool:
            return ROOT.seen_git_oids.contains(db, oid)

        def canonical_patch(patch_identity: str) -> str | None:
            return (
                pending_patches.get(patch_identity)
                or ROOT.canonical_patch_oids.key(patch_identity).get(db)
            )
    else:
        prior_discoveries = [e for e in events if isinstance(e, GitDiscovery)]
        initial = not prior_discoveries
        seen_oids, seen_patches = _replayed_discovery_state(events)

        def oid_seen(oid: str) -> bool:
            return oid in seen_oids

        def canonical_patch(patch_identity: str) -> str | None:
            return seen_patches.get(patch_identity)
    email_to_contributor = {
        email: contributor
        for contributor, emails in config["contributors"].items()
        for email in emails
    }

    repository_rows = []
    locations: dict[str, list[tuple[str, str, pathlib.Path, str]]] = {}
    for repo in config["repositories"]:
        mirror = _ensure_mirror(repo)
        object_format = _git(
            mirror, "rev-parse", "--show-object-format"
        ).decode().strip()
        refs = _matching_refs(mirror, repo["refs"])
        repo_reachable = set()
        for ref in refs:
            oids = _git(mirror, "rev-list", ref["commit_oid"]).decode().splitlines()
            for oid in oids:
                qualified = f"{object_format}:{oid}"
                repo_reachable.add(qualified)
                locations.setdefault(qualified, []).append(
                    (repo["id"], ref["name"], mirror, oid)
                )
        repository_rows.append({
            "id": repo["id"],
            "url": repo["url"],
            "object_format": object_format,
            "refs": refs,
            "reachable_commit_count": len(repo_reachable),
            "reachable_set_sha256": hashlib.sha256(
                "\n".join(sorted(repo_reachable)).encode()
            ).hexdigest(),
        })

    new_oids = sorted(oid for oid in locations if not oid_seen(oid))
    pending = []
    for qualified_oid in new_oids:
        source_rows = sorted({
            (repo_id, ref_name) for repo_id, ref_name, _, _ in locations[qualified_oid]
        })
        canonical_location = min(
            locations[qualified_oid], key=lambda x: (x[0], x[1])
        )
        # One commit may be reachable from dozens of refs in the same mirror.
        # Verify its object once per repository, not once per source ref.
        object_locations = {
            (str(m), raw_oid): (m, raw_oid)
            for _, _, m, raw_oid in locations[qualified_oid]
        }
        object_hashes = {
            hashlib.sha256(_git(m, "cat-file", "commit", raw_oid)).hexdigest()
            for m, raw_oid in object_locations.values()
        }
        if len(object_hashes) != 1:
            raise RuntimeError(f"conflicting Git objects share OID {qualified_oid}")
        _, _, mirror, oid = canonical_location
        metadata = _commit_metadata(mirror, oid)
        patch, patch_identity = _commit_patch(mirror, metadata)
        pending.append({
            **metadata,
            "oid": qualified_oid,
            "commit_object_sha256": next(iter(object_hashes)),
            "tree_oid": f"{qualified_oid.split(':', 1)[0]}:{metadata['tree_oid']}",
            "parent_oids": [
                f"{qualified_oid.split(':', 1)[0]}:{p}"
                for p in metadata["parent_oids"]
            ],
            "patch": patch,
            "patch_sha256": hashlib.sha256(patch.encode()).hexdigest() if patch else None,
            "patch_identity_version": PATCH_IDENTITY_VERSION,
            "patch_identity": patch_identity,
            "first_sources": [
                {"repository_id": repo_id, "ref_name": ref_name}
                for repo_id, ref_name in source_rows
            ],
            "contributor": email_to_contributor.get(metadata["author_email"]),
        })

    # Select patch representatives independently of repository/ref iteration order.
    # Every observed patch consumes its identity, even when it predates genesis or
    # has no registered contributor: copying already-observed work later must not
    # turn it into a newly payable contribution.
    pending.sort(key=lambda c: (c["committer_timestamp_ms"], c["oid"]))
    observations = []
    commits = []
    for commit in pending:
        reason = None
        canonical_patch_oid = None
        patch_identity = commit["patch_identity"]
        duplicate_patch = False
        if patch_identity:
            prior_canonical = canonical_patch(patch_identity)
            if prior_canonical:
                canonical_patch_oid = prior_canonical
                duplicate_patch = True
            else:
                canonical_patch_oid = commit["oid"]
                if events is None:
                    pending_patches[patch_identity] = canonical_patch_oid
                else:
                    seen_patches[patch_identity] = canonical_patch_oid

        if initial and commit["committer_timestamp_ms"] < GENESIS_MS:
            reason = "before_genesis"
        elif len(commit["parent_oids"]) > 1:
            reason = "merge_commit"
        elif not patch_identity:
            reason = "empty_commit"
        elif duplicate_patch:
            reason = "duplicate_patch"
        else:
            if commit["contributor"] is None:
                reason = "unknown_contributor"

        eligible = reason is None
        observation = {
            "oid": commit["oid"],
            "first_sources": commit["first_sources"],
            "committer_timestamp_ms": commit["committer_timestamp_ms"],
            "patch_identity": patch_identity,
            "canonical_patch_oid": canonical_patch_oid,
            "eligible": eligible,
            "exclusion_reason": reason,
        }
        observations.append(observation)
        if eligible:
            commits.append(commit)

    snapshot_material = {
        "schema_version": GIT_DISCOVERY_SCHEMA_VERSION,
        "epoch": epoch_n,
        "timestamp_ms": boundary_ms,
        "config_digest": digest,
        "repositories": repository_rows,
        "observations": observations,
    }
    snapshot_id = hashlib.sha256(
        json.dumps(snapshot_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return GitDiscovery(
        schema_version=GIT_DISCOVERY_SCHEMA_VERSION,
        epoch=epoch_n,
        snapshot_id=snapshot_id,
        timestamp_ms=boundary_ms,
        config_digest=digest,
        initial_snapshot=initial,
        configuration=config,
        repositories=repository_rows,
        observations=observations,
        commits=commits,
    )


def _acquire_discovery_file_lock():
    GIT_MIRROR_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = (GIT_MIRROR_DIR / ".discovery.lock").open("a+b")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return lock_file


def _release_discovery_file_lock(lock_file) -> None:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    lock_file.close()


async def _persist_discovery_evidence(discovery: GitDiscovery) -> None:
    """Record commit + discovery-completed evidence for a snapshot (idempotent)."""
    public_config = {
        "repositories": [
            _public_repo_row(r) for r in discovery.configuration["repositories"]
        ],
        "contributors": discovery.configuration["contributors"],
    }
    for commit in discovery.commits:
        commit_id = commit_id_for_oid(commit["oid"])
        await append_evidence(discovery.epoch, "git.commit", {
            "commit_id": commit_id,
            "oid": commit["oid"],
            "contributor": commit["contributor"],
            "message": _bytes_blob(commit["message"]),
            "patch": _bytes_blob(commit["patch"] or ""),
            "patch_sha256": commit.get("patch_sha256"),
            "patch_identity": commit.get("patch_identity"),
            "first_sources": commit.get("first_sources", []),
            "committer_timestamp_ms": commit.get("committer_timestamp_ms"),
            "summary": f"{commit['oid'][:20]} {commit['contributor']}",
            "urls": {
                "commit": _evidence_url("commit", commit_id),
                "epoch": _evidence_url("epoch", str(discovery.epoch)),
            },
        })
    await append_evidence(discovery.epoch, "git.discovery_completed", {
        "snapshot_id": discovery.snapshot_id,
        "config_digest": discovery.config_digest,
        "configuration": public_config,
        "observation_count": len(discovery.observations),
        "eligible_count": len(discovery.commits),
        "commit_ids": [commit_id_for_oid(c["oid"]) for c in discovery.commits],
        "observations": [
            {
                "oid": o["oid"],
                "eligible": o["eligible"],
                "exclusion_reason": o.get("exclusion_reason"),
                "commit_id": commit_id_for_oid(o["oid"]),
            }
            for o in discovery.observations
        ],
        "summary": (
            f"Discovered {len(discovery.observations)} commits; "
            f"{len(discovery.commits)} eligible"
        ),
        "urls": {"epoch": _evidence_url("epoch", str(discovery.epoch))},
    })


async def discover_repositories(epoch_n: int, boundary_ms: int) -> GitDiscovery:
    async with _DISCOVERY_LOCK:
        lock_file = await asyncio.to_thread(_acquire_discovery_file_lock)
        try:
            existing = _discovery_for_epoch(epoch_n)
            if existing:
                await _persist_discovery_evidence(existing)
                return existing
            candidate = await asyncio.to_thread(
                _build_discovery, epoch_n, boundary_ms
            )
            async with _LEDGER_LOCK:
                discovery = _discovery_for_epoch(epoch_n)
                if discovery is None:
                    stored_row, blobs = _externalize_payload(to_dict(candidate))
                    _db().apply(
                        _event_writes(stored_row, blobs=blobs),
                        Durability.SYNC_WAL,
                    )
                    discovery = candidate
            await _persist_discovery_evidence(discovery)
            return discovery
        finally:
            await asyncio.to_thread(_release_discovery_file_lock, lock_file)


SSE_CLIENTS = []
AUDIT_HISTORY = []
AUDIT_SEQUENCE = 0
PROCESS_STATE = {
    "running": False,
    "phase": "idle",
    "progress": 100,
    "message": "Waiting for the next epoch",
}
RANKING_VIEW_STATE = {
    "epoch": None,
    "phase": "idle",
    "models": [],
    "voted_count": 0,
    "rows": [],
}
RANKING_COMPARISONS: dict[str, list[str]] = {}


def _sse_event(event_name: str, payload: dict) -> str:
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    )


async def broadcast_audit(
    kind: str,
    message: str,
    *,
    progress: int | None = None,
    phase: str | None = None,
    evidence_event_id: str | None = None,
    evidence_url: str | None = None,
    links: dict | None = None,
) -> dict:
    global AUDIT_SEQUENCE
    AUDIT_SEQUENCE += 1
    if progress is not None:
        PROCESS_STATE["progress"] = max(0, min(100, int(progress)))
    if phase is not None:
        PROCESS_STATE["phase"] = phase
    PROCESS_STATE["message"] = message
    payload = {
        "id": AUDIT_SEQUENCE,
        "timestamp_ms": int(time.time() * 1000),
        "kind": kind,
        "message": message,
        **PROCESS_STATE,
    }
    if evidence_event_id:
        payload["evidence_event_id"] = evidence_event_id
    if evidence_url:
        payload["evidence_url"] = evidence_url
    if links:
        payload["links"] = links
    AUDIT_HISTORY.append(payload)
    del AUDIT_HISTORY[:-200]
    wire = _sse_event("audit", payload)
    for queue in list(SSE_CLIENTS):
        await queue.put(wire)
    return payload


async def broadcast_js(js: str):
    """Send a JS snippet to all connected SSE clients."""
    for queue in list(SSE_CLIENTS):
        await queue.put(js)


async def broadcast_ranking_state():
    wire = _sse_event("ranking_state", RANKING_VIEW_STATE)
    for queue in list(SSE_CLIENTS):
        await queue.put(wire)


def _comparison_ids_for_commit_id(commit_id: str) -> list[str]:
    path = ROOT.comparison_ids_by_commit.key(commit_id)
    return path.iter(_db()) if path.len(_db()) else []


def _set_ranking_view(
    ordered: list[dict],
    *,
    epoch: int,
    models: list[str],
    phase: str,
    ranking: list[int] | None = None,
    voted_count: int = 0,
) -> None:
    order = ranking if ranking is not None else list(range(len(ordered)))
    rows = []
    for position, index in enumerate(order):
        row = ordered[index]
        commit_id = commit_id_for_oid(row["oid"])
        rows.append({
            "position": position + 1,
            "commit_id": commit_id,
            "contributor": row.get("contributor") or "?",
            "message": row.get("message") or "",
            "voted": position < voted_count,
            "comparison_ids": list(RANKING_COMPARISONS.get(commit_id, ())),
        })
    RANKING_VIEW_STATE.update({
        "epoch": epoch,
        "phase": phase,
        "models": list(models),
        "voted_count": min(voted_count, len(rows)),
        "rows": rows,
    })


def _commit_side_for_llm(row: dict) -> dict:
    oid = row["oid"]
    short = oid.split(":", 1)[1][:8] if ":" in oid else oid[:8]
    return {
        "message": f"[{short}] {row['message']}",
        "diff": row["patch"] or "",
        "commit_id": commit_id_for_oid(oid),
        "contributor": row["contributor"],
        "oid": oid,
    }


def _rollup_contributor_scores(
    ordered: list[dict], commit_scores: list[Decimal]
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for row, score in zip(ordered, commit_scores):
        contributor = row["contributor"]
        totals[contributor] = totals.get(contributor, Decimal("0")) + score
    return totals


def _find_judgment(comparison_id: str, model_id: str) -> dict | None:
    path = ROOT.judgment_ids_by_comparison.key(comparison_id)
    ids = path.iter(_db()) if path.len(_db()) else []
    for e in _evidence_from_ids(ids):
        p = e.payload
        if p.get("comparison_id") == comparison_id and p.get("model_id") == model_id:
            return p
    return None


def _find_ranking_models(ranking_run_id: str) -> list[str] | None:
    event_ = find_evidence_payload(
        "ranking.started", "ranking_run_id", ranking_run_id
    )
    if event_:
        models = event_.payload.get("models")
        if isinstance(models, list) and models:
            return [str(m) for m in models]
    return None


async def rank_commits(commits: list[dict], *, epoch: int = -1):
    """Pairwise-rank every eligible commit; roll scores up to contributors."""
    if not commits:
        RANKING_COMPARISONS.clear()
        RANKING_VIEW_STATE.update({
            "epoch": epoch,
            "phase": "complete",
            "models": [],
            "voted_count": 0,
            "rows": [],
        })
        await broadcast_ranking_state()
        return {}, [], {"ranking_run_id": "", "ranking_event_id": ""}

    ordered = sorted(commits, key=lambda r: r["oid"])
    commit_ids = [commit_id_for_oid(row["oid"]) for row in ordered]
    RANKING_COMPARISONS.clear()
    _set_ranking_view(
        ordered,
        epoch=epoch,
        models=[],
        phase="spanning_tree",
    )
    await broadcast_ranking_state()
    ranking_run_id = _content_id("rank", {
        "epoch": epoch,
        "commit_ids": sorted(commit_ids),
    })
    contributors = sorted({c["contributor"] for c in ordered})

    # Nothing to compare: a single commit (not a single contributor).
    if len(ordered) == 1:
        _set_ranking_view(
            ordered,
            epoch=epoch,
            models=[],
            phase="complete",
            voted_count=1,
        )
        await broadcast_ranking_state()
        await append_evidence(epoch, "ranking.started", {
            "ranking_run_id": ranking_run_id,
            "commit_ids": commit_ids,
            "contributors": contributors,
            "models": [],
            "summary": f"ranking epoch {epoch}: single commit",
        })
        commit_ranking = {commit_ids[0]: "1"}
        contributor_ranking = {ordered[0]["contributor"]: Decimal("1")}
        completed = await append_evidence(epoch, "ranking.completed", {
            "ranking_run_id": ranking_run_id,
            "models": [],
            "commit_ranking": commit_ranking,
            "contributor_ranking": {ordered[0]["contributor"]: "1"},
            "ranking": {ordered[0]["contributor"]: "1"},
            "judgment_ids": [],
            "summary": f"Only one eligible commit; {ordered[0]['contributor']} rank 1.0",
        })
        await broadcast_audit(
            "ranking",
            f"Only one eligible commit; {ordered[0]['contributor']} rank 1.0",
            progress=90,
            phase="finalizing",
            evidence_event_id=completed.event_id,
            evidence_url=_evidence_url("event", completed.event_id),
            links={"epoch": _evidence_url("epoch", str(epoch))},
        )
        return contributor_ranking, [], {
            "ranking_run_id": ranking_run_id,
            "ranking_event_id": completed.event_id,
        }

    if not (OPENROUTER_API_KEY or "").strip():
        raise RuntimeError(
            "OPENROUTER_API_KEY is required when multiple commits need ranking"
        )

    existing_comparisons = await asyncio.to_thread(
        lambda: {
            commit_id: _comparison_ids_for_commit_id(commit_id)
            for commit_id in commit_ids
        }
    )
    RANKING_COMPARISONS.update(existing_comparisons)
    models = _find_ranking_models(ranking_run_id)
    if models is None:
        models = await fetch_top_models(n=3)
    # Preferred seats always sit (incl. resume of runs that predate them).
    models = _with_preferred_council(models)
    if not models:
        raise RuntimeError("no council models available for commit ranking")
    _set_ranking_view(
        ordered,
        epoch=epoch,
        models=models,
        phase="spanning_tree",
    )
    await broadcast_ranking_state()
    await append_evidence(epoch, "ranking.started", {
        "ranking_run_id": ranking_run_id,
        "commit_ids": commit_ids,
        "contributors": contributors,
        "models": models,
        "summary": (
            f"Council selected: {', '.join(models)} — "
            f"{len(ordered)} commits"
        ),
    })
    await broadcast_audit(
        "council",
        f"Council selected: {', '.join(models)} — ranking {len(ordered)} commits",
        progress=35,
        phase="ranking",
    )
    await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
        ["div.log-council",
            f"Council: {', '.join(models)} — {len(ordered)} commits"]
    ]))

    sides = [_commit_side_for_llm(row) for row in ordered]
    judgment_ids: list[str] = []
    # A dead juror must not halt the court. Retire after hard failure; continue
    # with remaining models. Abort only when a comparison gets zero votes.
    retired_models: set[str] = set()
    models_that_voted: set[str] = set()

    async def compare_fn(i, j):
        side_a, side_b = sides[i], sides[j]
        label_a = f"{side_a['commit_id'][:16]} ({side_a['contributor']})"
        label_b = f"{side_b['commit_id'][:16]} ({side_b['contributor']})"
        prompt = build_pairwise_prompt(side_a, side_b)
        comparison_material = {
            "ranking_run_id": ranking_run_id,
            "side_a": {
                "contributor": side_a["contributor"],
                "commit_id": side_a["commit_id"],
                "commit_ids": [side_a["commit_id"]],
                "oid": side_a["oid"],
                "message": _bytes_blob(side_a["message"]),
                "diff": _bytes_blob(side_a["diff"]),
            },
            "side_b": {
                "contributor": side_b["contributor"],
                "commit_id": side_b["commit_id"],
                "commit_ids": [side_b["commit_id"]],
                "oid": side_b["oid"],
                "message": _bytes_blob(side_b["message"]),
                "diff": _bytes_blob(side_b["diff"]),
            },
            "prompt": _bytes_blob(prompt),
        }
        comparison_id = comparison_id_for(comparison_material)
        comparison_material = {
            **comparison_material,
            "comparison_id": comparison_id,
            "summary": f"Comparing {label_a} with {label_b}",
        }
        cmp_ev = await append_evidence(epoch, "comparison.input", comparison_material)
        for commit_id in (side_a["commit_id"], side_b["commit_id"]):
            comparison_ids = RANKING_COMPARISONS.setdefault(commit_id, [])
            if comparison_id not in comparison_ids:
                comparison_ids.append(comparison_id)
        await broadcast_audit(
            "comparison",
            f"Comparing commits {label_a} vs {label_b}",
            phase="ranking",
            evidence_event_id=cmp_ev.event_id,
            evidence_url=_evidence_url("comparison", comparison_id),
            links={
                "comparison": _evidence_url("comparison", comparison_id),
                "commit_a": _evidence_url("commit", side_a["commit_id"]),
                "commit_b": _evidence_url("commit", side_b["commit_id"]),
                "epoch": _evidence_url("epoch", str(epoch)),
            },
        )
        await broadcast_js(exec_event(Three[Selector("#emission-status")][MORPH][
            ["div#emission-status", f"Comparing {label_a} vs {label_b}…"]
        ]))
        results = []
        for model in models:
            if model in retired_models:
                continue
            try:
                existing = _find_judgment(comparison_id, model)
                if existing:
                    result = {
                        "winner": existing["winner"],
                        "ratio": existing["ratio"],
                        "explanation": existing.get("explanation", ""),
                    }
                    judgment_ids.append(existing["judgment_id"])
                else:
                    result = await llm_pairwise_compare(
                        model,
                        side_a,
                        side_b,
                        epoch=epoch,
                        comparison_id=comparison_id,
                        persist=True,
                    )
                    judgment = _find_judgment(comparison_id, model)
                    if judgment:
                        judgment_ids.append(judgment["judgment_id"])
                w, l = (i, j) if result["winner"] == "A" else (j, i)
                ratio = result["ratio"].split(":")
                winner_weight, loser_weight = float(ratio[0]), float(ratio[1])
                results.append((w, l, winner_weight, loser_weight))
                models_that_voted.add(model)
                jud_id = (existing or _find_judgment(comparison_id, model) or {}).get(
                    "judgment_id"
                )
                win_label = (
                    f"{sides[w]['commit_id'][:16]} ({sides[w]['contributor']})"
                )
                lose_label = (
                    f"{sides[l]['commit_id'][:16]} ({sides[l]['contributor']})"
                )
                await broadcast_audit(
                    "vote",
                    f"{model}: {win_label} over {lose_label} ({result['ratio']})",
                    phase="ranking",
                    evidence_url=(
                        _evidence_url("judgment", jud_id) if jud_id else None
                    ),
                    links={
                        "judgment": (
                            _evidence_url("judgment", jud_id) if jud_id else None
                        ),
                        "comparison": _evidence_url("comparison", comparison_id),
                    },
                )
                await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
                    ["div.log-vote",
                        ["span.model", model], " — ",
                        ["span.winner", win_label], f" beat ",
                        ["span.loser", lose_label], f" ({result['ratio']}) ",
                        ["span.explanation", result["explanation"]],
                    ]
                ]))
            except Exception as e:
                retired_models.add(model)
                await append_evidence(epoch, "llm.council_member_failed", {
                    "ranking_run_id": ranking_run_id,
                    "comparison_id": comparison_id,
                    "model_id": model,
                    "error": {"type": type(e).__name__, "message": str(e)},
                    "summary": (
                        f"Retired {model} from this ranking run after hard failure"
                    ),
                })
                await broadcast_audit(
                    "error",
                    f"{model} failed and was retired from this run: {e}",
                    phase="ranking",
                    evidence_url=_evidence_url("comparison", comparison_id),
                    links={"comparison": _evidence_url("comparison", comparison_id)},
                )
                await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
                    ["div.log-error",
                        f"⚠ {model} retired after failure: {e}"]
                ]))
        if not results:
            raise RuntimeError(
                f"council model failed: no votes for comparison {comparison_id}"
            )
        return results

    async def progress_fn(ev):
        if ev["phase"] == "spanning_tree":
            label = f"Spanning tree: {ev['step']}/{ev['total']}"
            percent = 35 + round(35 * ev["step"] / max(ev["total"], 1))
        else:
            label = f"Zip pass {ev['pass']}: {ev['step']}/{ev['total']}"
            percent = 70 + round(20 * ev["step"] / max(ev["total"], 1))
        _set_ranking_view(
            ordered,
            epoch=epoch,
            models=models,
            phase=ev["phase"],
            ranking=ev.get("ranking"),
            voted_count=ev.get("voted_count", 0),
        )
        await broadcast_ranking_state()
        await broadcast_audit(
            "progress", label, progress=percent, phase="ranking"
        )
        await broadcast_js(exec_event(Three[Selector("#emission-status")][MORPH][
            ["div#emission-status", label]
        ]))

    pairs = await pairwise_rank(len(ordered), compare_fn, progress_fn)

    if not pairs:
        commit_score_list = [Decimal("1")]
    else:
        scores = await asyncio.to_thread(rank_centrality, pairs)
        commit_score_list = [Decimal(str(scores[i])) for i in range(len(ordered))]

    final_order = sorted(
        range(len(ordered)),
        key=lambda index: commit_score_list[index],
        reverse=True,
    )
    _set_ranking_view(
        ordered,
        epoch=epoch,
        models=models,
        phase="complete",
        ranking=final_order,
        voted_count=len(ordered),
    )
    await broadcast_ranking_state()
    commit_ranking = {
        commit_ids[i]: str(commit_score_list[i]) for i in range(len(ordered))
    }
    contributor_totals = _rollup_contributor_scores(ordered, commit_score_list)
    contrib_rows = sorted(
        contributor_totals.items(), key=lambda x: x[1], reverse=True
    )
    commit_rows = sorted(
        ((commit_ids[i], commit_score_list[i], ordered[i]["contributor"])
         for i in range(len(ordered))),
        key=lambda x: x[1],
        reverse=True,
    )
    models_voted = sorted(models_that_voted) or list(models)
    completed = await append_evidence(epoch, "ranking.completed", {
        "ranking_run_id": ranking_run_id,
        "models": models,
        "models_voted": models_voted,
        "models_failed": sorted(retired_models),
        "commit_ranking": commit_ranking,
        "contributor_ranking": {a: str(s) for a, s in contrib_rows},
        "ranking": {a: str(s) for a, s in contrib_rows},
        "judgment_ids": judgment_ids,
        "summary": (
            "Commit ranking: "
            + ", ".join(
                f"{cid[:16]}={float(s):.4f}" for cid, s, _ in commit_rows[:12]
            )
            + ("…" if len(commit_rows) > 12 else "")
            + (
                f" (retired: {', '.join(sorted(retired_models))})"
                if retired_models else ""
            )
        ),
    })
    await broadcast_audit(
        "ranking",
        "Contributor rollup: "
        + ", ".join(f"{a} {s:.4f}" for a, s in contrib_rows),
        progress=90,
        phase="finalizing",
        evidence_event_id=completed.event_id,
        evidence_url=_evidence_url("event", completed.event_id),
        links={"epoch": _evidence_url("epoch", str(epoch))},
    )
    await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
        ["div.log-ranking",
            ["b", "Ranking: "],
            *[["span.rank-entry", f"{a} {float(s):.3f}  "] for a, s in contrib_rows],
        ]
    ]))
    return contributor_totals, models_voted, {
        "ranking_run_id": ranking_run_id,
        "ranking_event_id": completed.event_id,
    }


# ===========================================================================
# §5. EMISSION — the pool decays, contributors receive
# ===========================================================================

DECAY_RATE = 1 - (Decimal("0.5").ln() / (HALF_LIFE_YEARS * 12)).exp()

def pool_remaining(events: list | None = None) -> Decimal:
    if events is None:
        emitted = Decimal(ROOT.meta.total_emitted.get(_db()) or "0")
    else:
        emitted = sum(
            Decimal(e.total_emitted) for e in events if isinstance(e, Emission)
        )
    return CONTRIBUTOR_POOL - emitted


async def run_emission(epoch_n, boundary_ms):
    PROCESS_STATE["running"] = True
    started = await append_evidence(epoch_n, "epoch.started", {
        "boundary_ms": boundary_ms,
        "summary": f"Epoch {epoch_n} emission started",
        "urls": {"epoch": _evidence_url("epoch", str(epoch_n))},
    })
    await broadcast_audit(
        "start",
        f"Epoch {epoch_n} emission started",
        progress=2,
        phase="starting",
        evidence_event_id=started.event_id,
        evidence_url=_evidence_url("epoch", str(epoch_n)),
        links={"epoch": _evidence_url("epoch", str(epoch_n))},
    )
    await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
        ["div.log-start", f"⚡ Epoch {epoch_n} emission started"]
    ]))

    await broadcast_audit(
        "discovery",
        "Fetching configured repositories and snapshotting refs",
        progress=8,
        phase="discovery",
    )
    discovery = await discover_repositories(epoch_n, boundary_ms)
    await broadcast_audit(
        "discovery",
        (
            f"Discovered {len(discovery.observations)} new commits; "
            f"{len(discovery.commits)} are eligible"
        ),
        progress=30,
        phase="discovery",
        links={
            "epoch": _evidence_url("epoch", str(epoch_n)),
            **{
                f"commit_{i}": _evidence_url("commit", commit_id_for_oid(c["oid"]))
                for i, c in enumerate(discovery.commits[:12])
                if isinstance(c, dict) and c.get("oid")
            },
        },
    )
    ranked = await rank_commits(discovery.commits, epoch=epoch_n)
    if len(ranked) == 2:
        ranking, models = ranked
        ranking_info = {}
    else:
        ranking, models, ranking_info = ranked

    async with _LEDGER_LOCK:
        existing = _emission_for_epoch(epoch_n)
        if existing:
            return existing
        pool_now = pool_remaining()
        emission_now = pool_now * DECAY_RATE if ranking else Decimal("0")
        normalized_ranking = {}
        distributions = {}
        if ranking:
            score_total = sum(ranking.values())
            normalized_ranking = {
                contributor: score / score_total
                for contributor, score in sorted(ranking.items())
            }
            contributors = list(normalized_ranking)
            allocated = Decimal("0")
            for contributor in contributors[:-1]:
                amount = emission_now * normalized_ranking[contributor]
                distributions[contributor] = amount
                allocated += amount
            distributions[contributors[-1]] = emission_now - allocated
        entry = Emission(
            epoch=epoch_n,
            timestamp_ms=boundary_ms,
            discovery_snapshot_id=discovery.snapshot_id,
            pool_before=str(pool_now),
            total_emitted=str(emission_now),
            pool_after=str(pool_now - emission_now),
            decay_rate=str(DECAY_RATE),
            distributions={a: str(amount) for a, amount in distributions.items()},
            ranking={a: str(s) for a, s in normalized_ranking.items()},
            models_used=models,
            evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
            ranking_run_id=ranking_info.get("ranking_run_id", ""),
            ranking_event_id=ranking_info.get("ranking_event_id", ""),
        )
        _db().apply(_event_writes(to_dict(entry)), Durability.SYNC_WAL)
    if entry:
        PROCESS_STATE["running"] = False
        await broadcast_audit(
            "complete",
            f"Epoch {entry.epoch} complete; emitted {entry.total_emitted} SLG",
            progress=100,
            phase="idle",
        )
        await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
            ["div.log-amount",
                f"Pool {entry.pool_before} → emit {entry.total_emitted} → {entry.pool_after}"]
        ]))
        await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
            ["div.log-complete",
                ["b", f"✓ Epoch {entry.epoch} complete — emitted {entry.total_emitted} SLUG"]]
        ]))
    return entry


# ===========================================================================
# §6. FOUR FUNCTIONS — query holdings, rankings, treasury, distribute USDC
# ===========================================================================

async def query_token_holdings():
    return {}


async def query_treasury_balance():
    return Decimal("0")


async def distribute_usdc(holdings, treasury_balance):
    if treasury_balance == 0 or not holdings:
        return
    total = sum(holdings.values())
    entry = UsdcDistribution(
        timestamp_ms=int(time.time() * 1000),
        treasury_balance=str(treasury_balance),
        distributions={w: str(treasury_balance * b / total) for w, b in holdings.items()},
    )
    await _append_typed_event(entry)
    return entry


# ===========================================================================
# §7. EPOCH TIMER — sleep until the exact millisecond
# ===========================================================================

async def epoch_loop():
    while True:
        epoch_n, current_start, next_boundary = current_epoch()
        if epoch_n >= 0 and not ROOT.emissions_by_epoch.contains(_db(), epoch_n):
            try:
                await run_emission(epoch_n, current_start)
            except Exception as exc:
                PROCESS_STATE["running"] = False
                await broadcast_audit(
                    "error",
                    f"Epoch {epoch_n} failed: {exc}; retrying in 60 seconds",
                    phase="error",
                )
                print(f"epoch {epoch_n} emission failed: {exc}", flush=True)
                await asyncio.sleep(60)
                continue

        now = int(time.time() * 1000)
        wait_ms = next_boundary - now
        if wait_ms <= 0:
            await asyncio.sleep(60)
        elif wait_ms < 86_400_000:
            await broadcast_js(exec_event(Three[Selector("#emission-status")][MORPH][
                ["div#emission-status",
                    f"Next epoch {epoch_n + 1} in {wait_ms / 1000:.0f}s"]
            ]))
            await asyncio.sleep(wait_ms / 1000)
            await run_emission(epoch_n + 1, next_boundary)
        else:
            await asyncio.sleep(3600)


# ===========================================================================
# §8. API — 2-line read forwards + computed endpoints
# ===========================================================================

def _evidence_summary(event_: Evidence) -> dict:
    payload = event_.payload
    summary = {
        "type": "evidence",
        "schema_version": event_.schema_version,
        "event_id": event_.event_id,
        "epoch": event_.epoch,
        "kind": event_.kind,
        "recorded_at_ms": event_.recorded_at_ms,
        "previous_event_sha256": event_.previous_event_sha256,
        "summary": payload.get("summary"),
        "urls": {"event": _evidence_url("event", event_.event_id)},
    }
    for key in (
        "commit_id", "comparison_id", "attempt_id", "judgment_id",
        "ranking_run_id", "snapshot_id", "oid", "model_id",
    ):
        if key in payload:
            summary[key] = payload[key]
    return summary


def _strip_heavy_fields(obj: dict) -> dict:
    """Bounded listing: drop multi-megabyte blobs unless full=1."""
    out = {}
    for key, value in obj.items():
        if key in {"patch", "message", "prompt", "request", "response", "diff"}:
            if isinstance(value, dict) and "sha256" in value:
                out[key] = {
                    "sha256": value["sha256"],
                    "byte_length": value.get("byte_length"),
                    "encoding": value.get("encoding"),
                }
            elif isinstance(value, str) and len(value) > 256:
                out[key] = value[:256] + "…"
            else:
                out[key] = value
        elif key == "commits" and isinstance(value, list):
            out[key] = [
                {
                    k: v for k, v in row.items()
                    if k != "patch"
                } | (
                    {"patch_sha256": row.get("patch_sha256")}
                    if isinstance(row, dict) else {}
                )
                for row in value
            ]
        else:
            out[key] = value
    return out


@app.get("/api/ledger")
async def get_ledger(offset: int = 0, limit: int = 100, full: int = 0):
    """List of ledger dicts. Heavy blobs stripped unless full=1."""
    limit = max(1, min(limit, 500))
    rows = []
    db = _db()
    total = ROOT.events.len(db)
    for index in range(max(0, offset), min(total, max(0, offset) + limit)):
        e = _typed(ROOT.events.get(db, index))
        if e is None:
            continue
        if isinstance(e, Evidence) and not full:
            rows.append(_evidence_summary(e))
        else:
            d = to_dict(e)
            rows.append(d if full else _strip_heavy_fields(d))
    return rows


@app.middleware("http")
async def ledger_ready_gate(request: Request, call_next):
    return await call_next(request)


@app.get("/api/health")
async def get_health():
    """Report readiness using only the exact list-length metadata key."""
    error = STATE_STATUS["error"]
    event_count = STATE_STATUS["event_count"]
    if STATE_STATUS["ready"] and state_db is not None:
        try:
            event_count = ROOT.events.len(state_db)
        except Exception as exc:
            error = str(exc)
    ready = bool(STATE_STATUS["ready"] and not error)
    return {
        "ok": ready,
        "ledger_ready": ready,
        "importing": bool(STATE_STATUS["importing"]),
        "error": error,
        "event_count": event_count,
    }


@app.get("/api/epoch")
async def get_epoch():
    epoch_n, start, next_b = current_epoch()
    pool = pool_remaining()
    return {
        "epoch": epoch_n, "start_ms": start, "next_boundary_ms": next_b,
        "total_supply": str(TOTAL_SUPPLY),
        "fdv": str(FDV),
        "price": str(PRICE),
        "lp_usdc": str(LP_USDC),
        "lp_pct": str(LP_PCT),
        "lp_tokens": str(LP_TOKENS),
        "pool_remaining": str(pool), "pool_pct": str(pool / CONTRIBUTOR_POOL * 100),
        "total_emitted": str(CONTRIBUTOR_POOL - pool), "decay_rate_per_epoch": str(DECAY_RATE),
    }


@app.get("/api/ranking")
async def get_ranking():
    latest = _typed(ROOT.latest_emission.get(_db()))
    if not isinstance(latest, Emission):
        return {"ranking": {}, "epoch": -1}
    return {"ranking": latest.ranking, "epoch": latest.epoch}


@app.get("/api/status")
async def get_status():
    latest_discovery = _typed(ROOT.latest_discovery.get(_db()))
    latest_emission = _typed(ROOT.latest_emission.get(_db()))
    return {
        **PROCESS_STATE,
        "epoch": current_epoch()[0],
        "openrouter_configured": bool((OPENROUTER_API_KEY or "").strip()),
        "sse_clients": len(SSE_CLIENTS),
        "latest_discovery": (
            {
                "epoch": latest_discovery.epoch,
                "snapshot_id": latest_discovery.snapshot_id,
                "observations": len(latest_discovery.observations),
                "eligible_commits": len(latest_discovery.commits),
            }
            if isinstance(latest_discovery, GitDiscovery) else None
        ),
        "latest_emission": (
            {
                "epoch": latest_emission.epoch,
                "total_emitted": latest_emission.total_emitted,
                "ranking": latest_emission.ranking,
            }
            if isinstance(latest_emission, Emission) else None
        ),
    }


@app.get("/api/contributor/{github_username}")
async def get_contributor(github_username: str):
    emissions = [
        _typed(row) for _, row in ROOT.emissions_by_epoch.iter(_db())
    ]
    history = [
        {"epoch": e.epoch, "amount": e.distributions[github_username], "rank_score": e.ranking.get(github_username)}
        for e in emissions
        if isinstance(e, Emission) and github_username in e.distributions
    ]
    return {"contributor": github_username, "total_earned": str(sum(Decimal(h["amount"]) for h in history)), "history": history}


@app.get("/api/halvening")
async def get_halvening():
    # Iterating symbolic rationals recursively causes expression-size explosion
    # after hundreds of epochs. Fifty-digit Decimal arithmetic is far beyond the
    # millisecond precision exposed by this endpoint and remains deterministic.
    boundary = Decimal(GENESIS_MS)
    j2000 = Decimal(946728000000)
    century = Decimal(36525 * 86400 * 1000)
    day = Decimal(86400000)

    def decimal_epoch_ms(at_ms: Decimal) -> Decimal:
        T = (at_ms - j2000) / century
        days = (
            Decimal("365.2421896698")
            + Decimal("-6.15359e-6") * T
            + Decimal("-7.29e-10") * T**2
            + Decimal("2.64e-10") * T**3
        )
        return days * day / Decimal(12)

    total_epochs = HALF_LIFE_YEARS * Decimal(12)
    whole_epochs = int(total_epochs)
    fraction = total_epochs - whole_epochs
    for _ in range(whole_epochs):
        boundary += decimal_epoch_ms(boundary)
    jubilee_ms = int(
        (boundary + fraction * decimal_epoch_ms(boundary))
        .to_integral_value(rounding="ROUND_HALF_UP")
    )
    dt = datetime.fromtimestamp(jubilee_ms / 1000, tz=timezone.utc)
    return {
        "jubilee_ms": jubilee_ms,
        "jubilee_utc": dt.isoformat(),
        "epoch": float(total_epochs),
        "half_life_years": str(HALF_LIFE_YEARS),
    }


@app.post("/test/emit")
async def test_emit():
    """Integration tests only: run the next unprocessed emission."""
    if os.environ.get("ALLOW_TEST_TRIGGERS") != "1":
        return Response(status_code=404)
    processed = set(int(epoch) for epoch in ROOT.emission_epochs.keys(_db()))
    n = 0
    while n in processed:
        n += 1
    entry = await run_emission(n, epoch_boundary(n))
    return to_dict(entry) if entry else {"ok": False, "error": "no emission appended"}


# ===========================================================================
# §10. SIGNED SNIPPET POST (UI rendering lives in views.py)
# ===========================================================================


async def redeem(github_user: str, wallet_address: str):
    await _append_typed_event(Redemption(
        timestamp_ms=int(time.time() * 1000),
        github_user=github_user,
        wallet_address=wallet_address,
        amount="0",  # TODO: compute unclaimed, execute Solana transfer
    ))
    return PlainTextResponse("location.reload()")



@app.post("/")
async def do(request: Request):
    form = await request.form()
    try:
        # verify_snippet binds each $slot's value as an eval local rather than
        # splicing it into the source, so request data can never become code.
        result = signer.verify_snippet(form).eval(globals())
        if asyncio.iscoroutine(result):
            result = await result
        return result
    except SnippetExecutionError as e:
        return PlainTextResponse(e.message, status_code=e.status_code)


# ===========================================================================
# §11. OAUTH — GitHub login
# ===========================================================================

@app.get("/login")
async def login():
    return Response(
        status_code=302,
        headers={"Location": f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&scope=read:user"},
    )


@app.get("/callback")
async def callback(request: Request, code: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={"client_id": GITHUB_CLIENT_ID, "client_secret": GITHUB_CLIENT_SECRET, "code": code},
            headers={"Accept": "application/json"},
        )
        token = resp.json().get("access_token")
        user_resp = await client.get(
            f"{GITHUB_API_BASE_URL}/user",
            headers={"Authorization": f"Bearer {token}"},
        )
        request.session["github_user"] = user_resp.json()["login"]
    return Response(status_code=302, headers={"Location": "/"})


# ===========================================================================
# §12. STARTUP
# ===========================================================================

@app.on_event("startup")
async def startup():
    status = await asyncio.to_thread(prepare_state_sync)
    print(f"rocks ledger ready: {status['event_count']} events", flush=True)
    if os.environ.get("DISABLE_EPOCH_LOOP") != "1":
        asyncio.create_task(epoch_loop())


# ---------------------------------------------------------------------------
# HTML/SSE views (views.py). Registered here at import bottom so the core
# module stays the single importable entry point and ``import constitution``
# never needs uvicorn. Pattern: ``views.register(app, core)`` where ``core``
# is the already-loaded constitution module; views.py defines routes closing
# over ``core`` and uses ``core.<name>`` at call time. No circular import at
# module top (views.py never imports constitution).
# ---------------------------------------------------------------------------
import sys as _sys

import views as _views

_view_handlers = _views.register(app, _sys.modules[__name__])
for _handler_name, _handler in _view_handlers.items():
    globals()[_handler_name] = _handler
del _view_handlers, _handler_name, _handler

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
