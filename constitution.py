#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi",
#   "uvicorn",
#   "httpx",
#   "tenacity",
#   "evaleval==0.2.7",
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
from fastapi.responses import PlainTextResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
import json, time, os, asyncio, httpx, pathlib, subprocess, hashlib, re, fcntl, base64
import sympy as sp  # type: ignore[reportMissingImports]
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from evaleval import (
    event, JsonlStore, to_dict, render, RawContent, Signer, SnippetExecutionError,
    exec_event, One, Two, Three, Selector, MORPH, PREPEND,
)

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


store = JsonlStore(JSONL_PATH)
_LEDGER_LOCK = asyncio.Lock()


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
    return base64.b64decode(blob["data"].encode("ascii"))


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


def _previous_event_sha256(events: list) -> str:
    for event_ in reversed(events):
        if isinstance(event_, Evidence):
            return event_.event_id.split("_", 1)[-1]
        if isinstance(event_, (GitDiscovery, Emission)):
            return _sha256_hex(_canonical_json(to_dict(event_)))
    return "0" * 64


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
    return pathlib.Path(str(store.path) + ".lock")


async def append_evidence(epoch: int, kind: str, payload: dict) -> Evidence:
    """Durably append one Evidence event under process + file locks."""
    # Logical identity ignores chain links / wall clock so restarts stay idempotent.
    event_id = _content_id("ev", {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "epoch": epoch,
        "kind": kind,
        "payload": payload,
    })
    async with _LEDGER_LOCK:
        lock_path = _ledger_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                events = store.read()
                existing = next(
                    (
                        e for e in events
                        if isinstance(e, Evidence) and e.event_id == event_id
                    ),
                    None,
                )
                if existing:
                    return existing
                recorded_at_ms = int(time.time() * 1000)
                previous = _previous_event_sha256(events)
                evidence = Evidence(
                    schema_version=EVIDENCE_SCHEMA_VERSION,
                    event_id=event_id,
                    epoch=epoch,
                    kind=kind,
                    recorded_at_ms=recorded_at_ms,
                    previous_event_sha256=previous,
                    payload=payload,
                )

                def append_once(current):
                    if any(
                        isinstance(e, Evidence) and e.event_id == event_id
                        for e in current
                    ):
                        return None
                    return evidence

                appended = await store.atomic(append_once)
                result = appended or next(
                    e for e in store.read()
                    if isinstance(e, Evidence) and e.event_id == event_id
                )
                try:
                    with open(store.path, "rb") as fh:
                        os.fsync(fh.fileno())
                except OSError:
                    pass
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    await broadcast_audit(
        kind,
        f"{kind}: {payload.get('summary') or event_id[:24]}",
        phase=PROCESS_STATE.get("phase"),
        progress=PROCESS_STATE.get("progress"),
        evidence_event_id=result.event_id,
        evidence_url=_evidence_url("event", result.event_id),
    )
    return result


def evidence_by_kind(kind: str | None = None) -> list[Evidence]:
    rows = [e for e in store.read() if isinstance(e, Evidence)]
    if kind is None:
        return rows
    return [e for e in rows if e.kind == kind]


def find_evidence(event_id: str) -> Evidence | None:
    return next(
        (e for e in store.read() if isinstance(e, Evidence) and e.event_id == event_id),
        None,
    )


def find_evidence_payload(kind: str, key: str, value: str) -> Evidence | None:
    for e in evidence_by_kind(kind):
        if e.payload.get(key) == value:
            return e
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


def _discovery_for_epoch(epoch: int) -> GitDiscovery | None:
    return next(
        (
            e for e in store.read()
            if isinstance(e, GitDiscovery) and e.epoch == epoch
        ),
        None,
    )


def _emission_for_epoch(epoch: int) -> Emission | None:
    return next(
        (
            e for e in store.read()
            if isinstance(e, Emission) and e.epoch == epoch
        ),
        None,
    )


def _epochs_in_ledger() -> list[int]:
    epochs: set[int] = set()
    for e in store.read():
        if isinstance(e, (GitDiscovery, Emission, Evidence)):
            epochs.add(e.epoch)
    return sorted(epochs)


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

j2000_unix_ms = sp.Integer(946728000000)
julian_century_ms = sp.Integer(36525 * 86400 * 1000)
day_ms = sp.Integer(86400000)

a0 = sp.Rational("365.2421896698")
a1 = sp.Rational("-6.15359e-6")
a2 = sp.Rational("-7.29e-10")
a3 = sp.Rational("2.64e-10")

genesis_ms = sp.Integer(GENESIS_MS)


def round_sympy_ms(x) -> int:
    return int(sp.floor(sp.sympify(x) + sp.Rational(1, 2)))


def T_from_unix_ms(ms):
    return (sp.sympify(ms) - j2000_unix_ms) / julian_century_ms


def tropical_epoch_ms(unix_ms):
    T = T_from_unix_ms(unix_ms)
    days = a0 + a1 * T + a2 * T**2 + a3 * T**3
    return sp.simplify(days * day_ms / sp.Integer(12))


def epoch_boundary(n):
    boundary = genesis_ms
    for _ in range(n):
        boundary += tropical_epoch_ms(boundary)
    return round_sympy_ms(boundary)


def current_epoch():
    now = int(time.time() * 1000)
    boundary = genesis_ms
    for e in range(10000):
        next_boundary = boundary + tropical_epoch_ms(boundary)
        if round_sympy_ms(next_boundary) > now:
            return e, round_sympy_ms(boundary), round_sympy_ms(next_boundary)
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
        return results

    # --- Phase 1: spanning tree ---
    uf = UnionFind(n)
    for i in range(n - 1):
        if not uf.connected(i, i + 1):
            await compare(i, i + 1)
            uf.union(i, i + 1)
        if progress_fn:
            await progress_fn({"phase": "spanning_tree", "step": i + 1, "total": n - 1})

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
        scores = rank_centrality(pairs)
        ranking = sorted(range(n), key=lambda idx: scores[idx], reverse=True)

        target = None
        for pos in range(n - 1):
            a, b = ranking[pos], ranking[pos + 1]
            if frozenset((a, b)) not in compared:
                target = (pos, a, b)
                break

        if target is None:
            break  # every adjacent edge has at least one LLM-reasoned result

        pos, a, b = target
        await compare(a, b)

        if progress_fn:
            await progress_fn({
                "phase": "zip",
                "pass": 1,
                "step": pos + 1,
                "total": n - 1,
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


def _build_discovery(epoch_n: int, boundary_ms: int, events: list) -> GitDiscovery:
    config = _normalized_discovery_config()
    digest = _config_digest(config)
    prior_discoveries = [e for e in events if isinstance(e, GitDiscovery)]
    initial = not prior_discoveries
    seen_oids, seen_patches = _replayed_discovery_state(events)
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

    new_oids = sorted(set(locations) - seen_oids)
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
            if patch_identity in seen_patches:
                canonical_patch_oid = seen_patches[patch_identity]
                duplicate_patch = True
            else:
                canonical_patch_oid = commit["oid"]
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
            events = store.read()
            existing = next(
                (
                    e for e in events
                    if isinstance(e, GitDiscovery) and e.epoch == epoch_n
                ),
                None,
            )
            if existing:
                await _persist_discovery_evidence(existing)
                return existing
            candidate = await asyncio.to_thread(
                _build_discovery, epoch_n, boundary_ms, events
            )

            def append_if_new(current_events):
                if any(
                    isinstance(e, GitDiscovery) and e.epoch == epoch_n
                    for e in current_events
                ):
                    return None
                return candidate

            appended = await store.atomic(append_if_new)
            discovery = appended or next(
                e for e in store.read()
                if isinstance(e, GitDiscovery) and e.epoch == epoch_n
            )
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
    for e in evidence_by_kind("llm.judgment"):
        p = e.payload
        if p.get("comparison_id") == comparison_id and p.get("model_id") == model_id:
            return p
    return None


def _find_ranking_models(ranking_run_id: str) -> list[str] | None:
    for e in evidence_by_kind("ranking.started"):
        if e.payload.get("ranking_run_id") == ranking_run_id:
            models = e.payload.get("models")
            if isinstance(models, list) and models:
                return [str(m) for m in models]
    return None


async def rank_commits(commits: list[dict], *, epoch: int = -1):
    """Pairwise-rank every eligible commit; roll scores up to contributors."""
    if not commits:
        return {}, [], {"ranking_run_id": "", "ranking_event_id": ""}

    ordered = sorted(commits, key=lambda r: r["oid"])
    commit_ids = [commit_id_for_oid(row["oid"]) for row in ordered]
    ranking_run_id = _content_id("rank", {
        "epoch": epoch,
        "commit_ids": sorted(commit_ids),
    })
    contributors = sorted({c["contributor"] for c in ordered})

    # Nothing to compare: a single commit (not a single contributor).
    if len(ordered) == 1:
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

    models = _find_ranking_models(ranking_run_id)
    if models is None:
        models = await fetch_top_models(n=3)
    # Preferred seats always sit (incl. resume of runs that predate them).
    models = _with_preferred_council(models)
    if not models:
        raise RuntimeError("no council models available for commit ranking")
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
        scores = rank_centrality(pairs)
        commit_score_list = [Decimal(str(scores[i])) for i in range(len(ordered))]

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

def pool_remaining(events: list) -> Decimal:
    emitted = sum(Decimal(e.total_emitted) for e in events if isinstance(e, Emission))
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

    def make_emission(events):
        if epoch_n in {e.epoch for e in events if isinstance(e, Emission)}:
            return None
        pool_now = pool_remaining(events)
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
        return Emission(
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

    entry = await store.atomic(make_emission)
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
    await store.append(entry)
    return entry


# ===========================================================================
# §7. EPOCH TIMER — sleep until the exact millisecond
# ===========================================================================

async def epoch_loop():
    while True:
        epoch_n, current_start, next_boundary = current_epoch()
        processed = {e.epoch for e in store.read() if isinstance(e, Emission)}
        if epoch_n >= 0 and epoch_n not in processed:
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
    for e in store.read()[offset:offset + limit]:
        if isinstance(e, Evidence) and not full:
            rows.append(_evidence_summary(e))
        else:
            d = to_dict(e)
            rows.append(d if full else _strip_heavy_fields(d))
    return rows


@app.get("/api/health")
async def get_health():
    """Liveness only — must not touch the ledger (health checks during ranking)."""
    return {"ok": True}


@app.get("/api/epoch")
async def get_epoch():
    epoch_n, start, next_b = current_epoch()
    pool = pool_remaining(store.read())
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
    emissions = [e for e in store.read() if isinstance(e, Emission)]
    if not emissions:
        return {"ranking": {}, "epoch": -1}
    latest = emissions[-1]
    return {"ranking": latest.ranking, "epoch": latest.epoch}


@app.get("/api/status")
async def get_status():
    events = store.read()
    discoveries = [e for e in events if isinstance(e, GitDiscovery)]
    emissions = [e for e in events if isinstance(e, Emission)]
    return {
        **PROCESS_STATE,
        "epoch": current_epoch()[0],
        "openrouter_configured": bool((OPENROUTER_API_KEY or "").strip()),
        "sse_clients": len(SSE_CLIENTS),
        "latest_discovery": (
            {
                "epoch": discoveries[-1].epoch,
                "snapshot_id": discoveries[-1].snapshot_id,
                "observations": len(discoveries[-1].observations),
                "eligible_commits": len(discoveries[-1].commits),
            }
            if discoveries else None
        ),
        "latest_emission": (
            {
                "epoch": emissions[-1].epoch,
                "total_emitted": emissions[-1].total_emitted,
                "ranking": emissions[-1].ranking,
            }
            if emissions else None
        ),
    }


@app.get("/api/contributor/{github_username}")
async def get_contributor(github_username: str):
    history = [
        {"epoch": e.epoch, "amount": e.distributions[github_username], "rank_score": e.ranking.get(github_username)}
        for e in store.read()
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
    processed = {e.epoch for e in store.read() if isinstance(e, Emission)}
    n = 0
    while n in processed:
        n += 1
    entry = await run_emission(n, epoch_boundary(n))
    return to_dict(entry) if entry else {"ok": False, "error": "no emission appended"}


# ===========================================================================
# §9. SSE — live audit stream of the pairwise voting process
# ===========================================================================

@app.get("/sse")
async def sse_stream(request: Request):
    queue = asyncio.Queue()
    SSE_CLIENTS.append(queue)

    async def generate():
        try:
            yield _sse_event("audit", {
                "id": AUDIT_SEQUENCE,
                "timestamp_ms": int(time.time() * 1000),
                "kind": "connection",
                "message": f"Connected to epoch {current_epoch()[0]}",
                **PROCESS_STATE,
            })
            yield exec_event(Three[Selector("#emission-status")][MORPH][
                ["div#emission-status", f"Connected — epoch {current_epoch()[0]}"]
            ])
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield data
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in SSE_CLIENTS:
                SSE_CLIENTS.remove(queue)

    from starlette.responses import StreamingResponse
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ===========================================================================
# §9b. EVIDENCE HTML — pure indexes/details (no JSON evidence APIs)
# ===========================================================================

def _a(href: str, label: str) -> list:
    return ["a", {"href": href}, label]


def _pre_blob(text: str, *, cls: str = "blob") -> list:
    return [f"pre.{cls}", text if text else "(empty)"]


def _fold(summary: str, *body) -> list:
    return ["details.fold", ["summary", summary], *body]


def _short_id(value: str | None, n: int = 12) -> str:
    if not value:
        return "?"
    return value if len(value) <= n else value[:n]


def _side_label(side: dict) -> str:
    cid = side.get("commit_id") or (side.get("commit_ids") or ["?"])[0]
    who = side.get("contributor") or "?"
    return f"{_short_id(cid, 14)} ({who})"


def _judgments_by_comparison(
    judgments: list[Evidence] | None = None,
) -> dict[str, list[Evidence]]:
    rows = judgments if judgments is not None else evidence_by_kind("llm.judgment")
    out: dict[str, list[Evidence]] = {}
    for e in rows:
        cid = e.payload.get("comparison_id")
        if cid:
            out.setdefault(str(cid), []).append(e)
    return out


def _judgments_for_comparison(comparison_id: str) -> list[Evidence]:
    return _judgments_by_comparison().get(comparison_id, [])


def _judgment_blocks(judgments: list[Evidence]) -> list:
    if not judgments:
        return [["p.note", "No judgments yet."]]
    blocks: list = []
    for j in judgments:
        p = j.payload
        jid = p.get("judgment_id") or j.event_id
        blocks.append(["article.judgment",
            ["div.judgment-meta",
                ["strong", p.get("model_id") or "?"],
                " · winner ",
                ["span.winner", str(p.get("winner") or "?")],
                " · ",
                ["span.ratio", str(p.get("ratio") or "?")],
                " · ",
                _a(_evidence_path("judgment", jid), "permalink"),
            ],
            ["p.explanation", p.get("explanation") or "(no explanation)"],
        ])
    return blocks


def _evidence_page(title: str, body: list) -> HTMLResponse:
    return HTMLResponse(render(["html",
        ["head",
            ["meta", {"charset": "utf-8"}],
            ["meta", {"name": "viewport", "content": "width=device-width, initial-scale=1"}],
            ["title", title],
            ["style", RawContent(_WATCH_CSS)],
        ],
        ["body.evidence-doc",
            ["main", body],
            ["script", RawContent(_FORM_INTERCEPT_JS)],
        ],
    ]))


def _evidence_nav(*extra: list) -> list:
    crumbs = [
        _a("/", "constitution"), " · ",
        _a("/epochs", "epochs"), " · ",
        _a("/watch", "watch"),
    ]
    for item in extra:
        crumbs.extend([" · ", item])
    return ["p.note", *crumbs]


def _dl_rows(rows: list[tuple[str, object]]) -> list:
    items = []
    for key, value in rows:
        if value is None or value == "":
            continue
        items.append(["div.kv",
            ["span.k", key],
            ["span.v", value if isinstance(value, list) else str(value)],
        ])
    return ["div.kv-list", *items] if items else ["p.note", "(none)"]


def _link_list(pairs: list[tuple[str, str]]) -> list:
    if not pairs:
        return ["p.note", "(none)"]
    out: list = ["ul"]
    for label, href in pairs:
        out.append(["li", _a(href, label)])
    return out


@app.get("/epochs")
async def epochs_index():
    epochs = _epochs_in_ledger()
    rows = []
    for epoch in epochs:
        emission = _emission_for_epoch(epoch)
        evidence_n = sum(1 for e in evidence_by_kind() if e.epoch == epoch)
        disc = next(
            (
                e for e in evidence_by_kind("git.discovery_completed")
                if e.epoch == epoch
            ),
            None,
        )
        detail = []
        if disc:
            detail.append(
                f"{disc.payload.get('eligible_count', 0)} eligible / "
                f"{disc.payload.get('observation_count', 0)} observed"
            )
        if emission:
            detail.append(f"emitted {emission.total_emitted}")
        if evidence_n:
            detail.append(f"{evidence_n} evidence events")
        rows.append(["li",
            _a(_evidence_path("epoch", str(epoch)), f"epoch {epoch}"),
            " — ",
            ", ".join(detail) if detail else "recorded",
        ])
    return _evidence_page("epochs", [
        _evidence_nav(),
        ["div.eyebrow", "transparent constitutional evidence"],
        ["h1", "epochs"],
        ["p", "Each epoch indexes discovery, commits, comparisons, judgments, ranking, and emission."],
        ["ul", *rows] if rows else ["p.note", "No epochs in the ledger yet."],
    ])


@app.get("/epochs/{epoch}")
async def epoch_detail(epoch: int):
    emission = _emission_for_epoch(epoch)
    evidence_rows = [e for e in evidence_by_kind() if e.epoch == epoch]
    if not evidence_rows and emission is None:
        return _evidence_page(f"epoch {epoch}", [
            _evidence_nav(),
            ["h1", f"epoch {epoch}"],
            ["p.note", "No evidence for this epoch."],
        ])

    commit_evs = [e for e in evidence_rows if e.kind == "git.commit"]
    comparison_evs = [e for e in evidence_rows if e.kind == "comparison.input"]
    judgment_evs = [e for e in evidence_rows if e.kind == "llm.judgment"]
    discovery_ev = next(
        (e for e in evidence_rows if e.kind == "git.discovery_completed"), None
    )
    ranking_started = next(
        (e for e in evidence_rows if e.kind == "ranking.started"), None
    )
    ranking_completed = next(
        (e for e in evidence_rows if e.kind == "ranking.completed"), None
    )

    commit_by_id = {
        e.payload["commit_id"]: e
        for e in commit_evs
        if e.payload.get("commit_id")
    }

    # Dense comparison cards with inline reasoning (permalinks kept).
    # Index judgments once — never rescan the full ledger per comparison.
    judgments_by_cmp = _judgments_by_comparison(judgment_evs)
    comparison_cards: list = []
    disagreement_cards: list = []
    for cmp in comparison_evs:
        cid = cmp.payload.get("comparison_id")
        if not cid:
            continue
        side_a = cmp.payload.get("side_a") or {}
        side_b = cmp.payload.get("side_b") or {}
        juds = judgments_by_cmp.get(cid, [])
        reason_lines = []
        winners: set[str] = set()
        for j in juds:
            p = j.payload
            if p.get("winner"):
                winners.add(str(p["winner"]))
            expl = (p.get("explanation") or "").strip()
            if len(expl) > 220:
                expl = expl[:220] + "…"
            reason_lines.append(["li",
                ["strong", p.get("model_id") or "?"],
                f" → {p.get('winner')} ({p.get('ratio')}) — {expl} ",
                _a(_evidence_path("judgment", p.get("judgment_id") or j.event_id), "↗"),
            ])
        card = ["article.dense-card",
            ["div.card-head",
                _a(_evidence_path("comparison", cid), "comparison"),
                " · ",
                _side_label(side_a),
                " vs ",
                _side_label(side_b),
                *([" · ", ["span.disagree", "disagreement"]]
                  if len(winners) > 1 else []),
            ],
            ["ul.reason-list", *reason_lines] if reason_lines else ["p.note", "No judgments yet."],
        ]
        comparison_cards.append(card)
        if len(winners) > 1:
            disagreement_cards.append(card)

    ranking_nodes: list = []
    if ranking_completed:
        commit_ranking = ranking_completed.payload.get("commit_ranking") or {}
        contrib_ranking = (
            ranking_completed.payload.get("contributor_ranking")
            or ranking_completed.payload.get("ranking")
            or {}
        )
        rank_rows = []
        for i, (cid, score) in enumerate(sorted(
            commit_ranking.items(),
            key=lambda kv: Decimal(str(kv[1])),
            reverse=True,
        ), start=1):
            ev = commit_by_id.get(cid)
            who = (ev.payload.get("contributor") if ev else "?")
            msg = ""
            if ev:
                msg = _blob_text(ev.payload.get("message")).split("\n", 1)[0][:80]
            rank_rows.append(["tr",
                ["td", str(i)],
                ["td", ["code", f"{float(score):.4f}"]],
                ["td", _a(_evidence_path("commit", cid), _short_id(cid, 16))],
                ["td", who or "?"],
                ["td.msg", msg],
            ])
        models_failed = ranking_completed.payload.get("models_failed") or []
        ranking_nodes = [
            ["p.note", ranking_completed.payload.get("summary") or "ranking completed"],
            ["p", "Models: ", ", ".join(ranking_completed.payload.get("models") or []) or "—"],
            *(
                [["p.note", "Retired mid-run: ", ", ".join(models_failed)]]
                if models_failed else []
            ),
            ["h3", "commit ranking"],
            ["table.dense",
                ["thead", ["tr",
                    ["th", "#"], ["th", "score"], ["th", "commit"],
                    ["th", "author"], ["th", "message"],
                ]],
                ["tbody", *rank_rows],
            ] if rank_rows else ["p.note", "(none)"],
            ["h3", "contributor rollup"],
            ["pre.blob.compact", json.dumps(contrib_ranking, indent=2, sort_keys=True)],
        ]
    elif ranking_started:
        ranking_nodes = [["p.note", f"Ranking started: {ranking_started.event_id}"]]
    else:
        ranking_nodes = [["p.note", "No ranking evidence."]]

    if emission:
        emission_node = _dl_rows([
            ("total_emitted", emission.total_emitted),
            ("pool", f"{emission.pool_before} → {emission.pool_after}"),
            ("models_used", ", ".join(emission.models_used or [])),
            ("distributions", json.dumps(emission.distributions, sort_keys=True)),
        ])
    else:
        emission_node = ["p.note", "No emission for this epoch."]

    excluded = []
    if discovery_ev:
        for obs in discovery_ev.payload.get("observations") or []:
            if obs.get("eligible"):
                continue
            excluded.append(["li",
                f"{(obs.get('oid') or '?')[:28]} — {obs.get('exclusion_reason') or 'excluded'}"
            ])

    event_links = [
        (f"{e.kind} · {e.event_id[:28]}", _evidence_path("event", e.event_id))
        for e in evidence_rows
    ]
    judgment_links = [
        (
            e.payload.get("summary") or e.payload.get("judgment_id", e.event_id),
            _evidence_path("judgment", e.payload["judgment_id"]),
        )
        for e in judgment_evs
        if e.payload.get("judgment_id")
    ]
    commit_links = [
        (
            f"{e.payload.get('oid', e.payload.get('commit_id', ''))[:24]} "
            f"({e.payload.get('contributor', '?')})",
            _evidence_path("commit", e.payload["commit_id"]),
        )
        for e in commit_evs
        if e.payload.get("commit_id")
    ]
    comparison_links = [
        (
            e.payload.get("summary") or e.payload.get("comparison_id", e.event_id),
            _evidence_path("comparison", e.payload["comparison_id"]),
        )
        for e in comparison_evs
        if e.payload.get("comparison_id")
    ]

    no_comparisons_note = "No comparisons."
    if len(commit_evs) <= 1:
        no_comparisons_note += " Fewer than two eligible commits — nothing to pairwise-rank."

    disc_line = ""
    if discovery_ev:
        disc_line = (
            f"{discovery_ev.payload.get('eligible_count', 0)} eligible / "
            f"{discovery_ev.payload.get('observation_count', 0)} observed"
        )

    body = [
        _evidence_nav(),
        ["div.eyebrow", f"epoch {epoch}"],
        ["h1", f"epoch {epoch}"],
        ["p.lede",
            disc_line + (" · " if disc_line and emission else ""),
            (f"emitted {emission.total_emitted}" if emission else ""),
        ],
        ["h2", "emission"],
        emission_node,
        ["h2", "ranking"],
        *ranking_nodes,
        ["h2", "council disagreements"],
        *(
            disagreement_cards
            if disagreement_cards
            else [["p.note", "No split votes yet — council unanimous where judged."]]
        ),
        ["h2", "comparisons"],
        *(comparison_cards if comparison_cards else [["p.note", no_comparisons_note]]),
        _fold("All commits", _link_list(commit_links)),
        _fold("Excluded observations",
              ["ul", *excluded] if excluded else ["p.note", "(none)"]),
        _fold("Judgment permalinks",
              _link_list(judgment_links) if judgment_links else ["p.note", "(none)"]),
        _fold("Comparison permalinks",
              _link_list(comparison_links) if comparison_links else ["p.note", "(none)"]),
        _fold("Raw evidence events",
              _link_list(event_links) if event_links else ["p.note", "(none)"]),
    ]
    return _evidence_page(f"epoch {epoch}", body)


@app.get("/commits/{commit_id}")
async def commit_detail(commit_id: str):
    ev = find_evidence_payload("git.commit", "commit_id", commit_id)
    if not ev:
        return _evidence_page("commit not found", [
            _evidence_nav(),
            ["h1", "commit not found"],
            ["p", commit_id],
        ])
    p = ev.payload
    epoch = ev.epoch
    judgments_by_cmp = _judgments_by_comparison()
    related_cmp = []
    for cmp in evidence_by_kind("comparison.input"):
        if cmp.epoch != epoch:
            continue
        sa, sb = cmp.payload.get("side_a") or {}, cmp.payload.get("side_b") or {}
        ids = {
            sa.get("commit_id"),
            *(sa.get("commit_ids") or []),
            sb.get("commit_id"),
            *(sb.get("commit_ids") or []),
        }
        if commit_id not in ids:
            continue
        cid = cmp.payload.get("comparison_id")
        if not cid:
            continue
        related_cmp.append(["article.dense-card",
            ["div.card-head",
                _a(_evidence_path("comparison", cid), "comparison"),
                " · ",
                _side_label(sa),
                " vs ",
                _side_label(sb),
            ],
            *_judgment_blocks(judgments_by_cmp.get(cid, [])),
        ])
    return _evidence_page(f"commit {commit_id[:24]}", [
        _evidence_nav(_a(_evidence_path("epoch", str(epoch)), f"epoch {epoch}")),
        ["div.eyebrow", "commit"],
        ["h1", _short_id(commit_id, 20)],
        ["p.lede", p.get("contributor") or "?", " · ", ["code", p.get("oid", "")]],
        ["p", _a(f"/commits/{commit_id}/patch", "download patch"), " · ",
              _a(_evidence_path("event", ev.event_id), "raw event")],
        ["h2", "message"],
        _pre_blob(_blob_text(p.get("message")), cls="blob compact"),
        ["h2", "comparisons involving this commit"],
        *(related_cmp if related_cmp else [["p.note", "None yet."]]),
        _fold("Full patch",
              _pre_blob(_blob_text(p.get("patch")))),
        _fold("Metadata", _dl_rows([
            ("commit_id", commit_id),
            ("patch_sha256", p.get("patch_sha256")),
            ("patch_identity", p.get("patch_identity")),
            ("committer_timestamp_ms", p.get("committer_timestamp_ms")),
        ])),
    ])


@app.get("/commits/{commit_id}/patch")
async def commit_patch_download(commit_id: str):
    ev = find_evidence_payload("git.commit", "commit_id", commit_id)
    if not ev:
        return PlainTextResponse("not found", status_code=404)
    raw = _decode_blob(ev.payload.get("patch")) or _blob_text(ev.payload.get("patch")).encode()
    return Response(
        content=raw,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{commit_id}.patch"'
        },
    )


@app.get("/comparisons/{comparison_id}")
async def comparison_detail(comparison_id: str):
    ev = find_evidence_payload("comparison.input", "comparison_id", comparison_id)
    if not ev:
        return _evidence_page("comparison not found", [
            _evidence_nav(),
            ["h1", "comparison not found"],
            ["p", comparison_id],
        ])
    p = ev.payload
    side_a = p.get("side_a") or {}
    side_b = p.get("side_b") or {}
    judgments = _judgments_for_comparison(comparison_id)
    attempt_links = []
    for a in evidence_by_kind("llm.attempt_started"):
        if a.payload.get("comparison_id") == comparison_id:
            aid = a.payload.get("attempt_id")
            if aid:
                attempt_links.append((
                    f"{a.payload.get('model_id')} #{a.payload.get('attempt_number')}",
                    _evidence_path("attempt", aid),
                ))
    judgment_links = [
        (
            j.payload.get("summary") or j.payload.get("judgment_id", j.event_id),
            _evidence_path("judgment", j.payload["judgment_id"]),
        )
        for j in judgments
        if j.payload.get("judgment_id")
    ]
    cid_a = side_a.get("commit_id") or (side_a.get("commit_ids") or [None])[0]
    cid_b = side_b.get("commit_id") or (side_b.get("commit_ids") or [None])[0]
    return _evidence_page(f"comparison {comparison_id[:24]}", [
        _evidence_nav(_a(_evidence_path("epoch", str(ev.epoch)), f"epoch {ev.epoch}")),
        ["div.eyebrow", "comparison"],
        ["h1", f"{_side_label(side_a)} vs {_side_label(side_b)}"],
        ["p.lede",
            _a(f"/comparisons/{comparison_id}/prompt", "download prompt"),
            " · ",
            _a(_evidence_path("event", ev.event_id), "raw event"),
            " · ",
            ["code", _short_id(comparison_id, 18)],
        ],
        ["h2", "council reasoning"],
        *_judgment_blocks(judgments),
        ["h2", "sides"],
        ["div.sides",
            ["section.side",
                ["h3", "A — ",
                    (_a(_evidence_path("commit", cid_a), _side_label(side_a))
                     if cid_a else _side_label(side_a))],
                ["h4", "message"],
                _pre_blob(_blob_text(side_a.get("message")), cls="blob compact"),
                _fold("Full diff A",
                      _pre_blob(_blob_text(side_a.get("diff")))),
            ],
            ["section.side",
                ["h3", "B — ",
                    (_a(_evidence_path("commit", cid_b), _side_label(side_b))
                     if cid_b else _side_label(side_b))],
                ["h4", "message"],
                _pre_blob(_blob_text(side_b.get("message")), cls="blob compact"),
                _fold("Full diff B",
                      _pre_blob(_blob_text(side_b.get("diff")))),
            ],
        ],
        _fold("Hardlinks — judgments / attempts / prompt",
              ["p", _a(f"/comparisons/{comparison_id}/prompt", "prompt download")],
              ["h3", "judgments"],
              _link_list(judgment_links) if judgment_links else ["p.note", "(none)"],
              ["h3", "attempts"],
              _link_list(attempt_links) if attempt_links else ["p.note", "(none)"],
              _fold("Full prompt text",
                    _pre_blob(_blob_text(p.get("prompt")))),
        ),
    ])


@app.get("/comparisons/{comparison_id}/prompt")
async def comparison_prompt_download(comparison_id: str):
    ev = find_evidence_payload("comparison.input", "comparison_id", comparison_id)
    if not ev:
        return PlainTextResponse("not found", status_code=404)
    raw = _decode_blob(ev.payload.get("prompt")) or _blob_text(ev.payload.get("prompt")).encode()
    return Response(
        content=raw,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{comparison_id}.prompt.txt"'
        },
    )


@app.get("/attempts/{attempt_id}")
async def attempt_detail(attempt_id: str):
    started = find_evidence_payload("llm.attempt_started", "attempt_id", attempt_id)
    finished = find_evidence_payload("llm.attempt_finished", "attempt_id", attempt_id)
    if not started and not finished:
        return _evidence_page("attempt not found", [
            _evidence_nav(),
            ["h1", "attempt not found"],
            ["p", attempt_id],
        ])
    base = started or finished
    assert base is not None
    p = {**(started.payload if started else {}), **(finished.payload if finished else {})}
    judgment = None
    for j in evidence_by_kind("llm.judgment"):
        if j.payload.get("attempt_id") == attempt_id:
            judgment = j
            break
    comparison_id = p.get("comparison_id")
    return _evidence_page(f"attempt {attempt_id[:24]}", [
        _evidence_nav(
            _a(_evidence_path("epoch", str(base.epoch)), f"epoch {base.epoch}"),
            *(
                [_a(_evidence_path("comparison", comparison_id), "comparison")]
                if comparison_id else []
            ),
        ),
        ["div.eyebrow", "llm attempt"],
        ["h1", attempt_id],
        _dl_rows([
            ("model_id", p.get("model_id")),
            ("attempt_number", p.get("attempt_number")),
            ("ok", p.get("ok")),
            ("http_status", p.get("http_status")),
            ("duration_ms", p.get("duration_ms")),
            ("error", json.dumps(p["error"]) if p.get("error") else None),
            ("started_event",
             _a(_evidence_path("event", started.event_id), started.event_id)
             if started else None),
            ("finished_event",
             _a(_evidence_path("event", finished.event_id), finished.event_id)
             if finished else None),
            ("judgment",
             _a(
                 _evidence_path("judgment", judgment.payload["judgment_id"]),
                 judgment.payload["judgment_id"],
             ) if judgment and judgment.payload.get("judgment_id") else None),
        ]),
        ["h2", "request"],
        _pre_blob(_blob_text(p.get("request"))),
        ["h2", "response"],
        _pre_blob(_blob_text(p.get("response"))),
    ])


@app.get("/judgments/{judgment_id}")
async def judgment_detail(judgment_id: str):
    ev = find_evidence_payload("llm.judgment", "judgment_id", judgment_id)
    if not ev:
        return _evidence_page("judgment not found", [
            _evidence_nav(),
            ["h1", "judgment not found"],
            ["p", judgment_id],
        ])
    p = ev.payload
    return _evidence_page(f"judgment {judgment_id[:24]}", [
        _evidence_nav(
            _a(_evidence_path("epoch", str(ev.epoch)), f"epoch {ev.epoch}"),
            *(
                [_a(_evidence_path("comparison", p["comparison_id"]), "comparison")]
                if p.get("comparison_id") else []
            ),
            *(
                [_a(_evidence_path("attempt", p["attempt_id"]), "attempt")]
                if p.get("attempt_id") else []
            ),
        ),
        ["div.eyebrow", "judgment"],
        ["h1", f"{p.get('model_id') or '?'} → {p.get('winner')} ({p.get('ratio')})"],
        ["p.lede", ["code", _short_id(judgment_id, 18)], " · ",
              _a(_evidence_path("event", ev.event_id), "raw event")],
        ["article.judgment",
            ["p.explanation", p.get("explanation") or "(no explanation)"],
        ],
        _fold("Metadata", _dl_rows([
            ("judgment_id", judgment_id),
            ("model_id", p.get("model_id")),
            ("winner", p.get("winner")),
            ("ratio", p.get("ratio")),
            ("comparison_id", p.get("comparison_id")),
            ("attempt_id", p.get("attempt_id")),
        ])),
    ])


@app.get("/events/{event_id}")
async def event_detail(event_id: str):
    ev = find_evidence(event_id)
    if not ev:
        return _evidence_page("event not found", [
            _evidence_nav(),
            ["h1", "event not found"],
            ["p", event_id],
        ])
    p = ev.payload
    related = []
    for key, kind in (
        ("commit_id", "commit"),
        ("comparison_id", "comparison"),
        ("attempt_id", "attempt"),
        ("judgment_id", "judgment"),
    ):
        if p.get(key):
            related.append((f"{key}: {p[key]}", _evidence_path(kind, p[key])))
    related.append((f"epoch {ev.epoch}", _evidence_path("epoch", str(ev.epoch))))
    display_payload = {}
    for key, value in p.items():
        if isinstance(value, dict) and value.get("encoding") == "base64":
            display_payload[key] = {
                "sha256": value.get("sha256"),
                "byte_length": value.get("byte_length"),
                "text_preview": (_blob_text(value)[:2000]
                                 + ("…" if value.get("byte_length", 0) > 2000 else "")),
            }
        else:
            display_payload[key] = value
    return _evidence_page(f"event {event_id[:24]}", [
        _evidence_nav(_a(_evidence_path("epoch", str(ev.epoch)), f"epoch {ev.epoch}")),
        ["div.eyebrow", ev.kind],
        ["h1", event_id],
        _dl_rows([
            ("kind", ev.kind),
            ("epoch", ev.epoch),
            ("recorded_at_ms", ev.recorded_at_ms),
            ("previous_event_sha256", ev.previous_event_sha256),
            ("schema_version", ev.schema_version),
        ]),
        ["h2", "links"],
        _link_list(related),
        ["h2", "payload"],
        _pre_blob(json.dumps(display_payload, indent=2, sort_keys=True, default=str)),
    ])


# ===========================================================================
# §10. UI — hiccup pages + signed snippet POST handler
# ===========================================================================

_FORM_INTERCEPT_JS = """
document.addEventListener('submit', async e => {
    e.preventDefault();
    const r = await fetch('/', {method: 'POST', body: new FormData(e.target)});
    const js = await r.text();
    if (js.trim()) eval(js);
});
"""

def _page(title: str, body: list) -> HTMLResponse:
    return HTMLResponse(render(["html",
        ["head",
            ["meta", {"charset": "utf-8"}],
            ["meta", {"name": "viewport", "content": "width=device-width, initial-scale=1"}],
            ["title", title],
            ["style", RawContent(_WATCH_CSS)],
        ],
        ["body",
            body,
            ["script", RawContent(_FORM_INTERCEPT_JS)],
        ],
    ]))


_WATCH_CSS = """
/* ================================================================
   ZIGGURAT — bevel-first dark theme
   --spread (0→1) controls bevel depth. 0 = flat. 1 = full relief.
   Light source: top-left. Shadow: bottom-right.
   Platforms nest. Each level is raised. Nothing is rounded.
   ================================================================ */

:root {
  color-scheme: dark;
  --spread: 1;

  --g0: #080808;
  --g1: #131313;
  --g2: #1c1c1c;
  --g3: #252525;
  --g4: #2e2e2e;
  --g5: #383838;

  --hi: #5e5e5e;
  --lo: #050505;
  --bv: calc(var(--spread) * 4px + 1px);
  --bv-lg: calc(var(--spread) * 6px + 2px);

  --signal: #f0f0f0;
  --prose: #c2c2c2;
  --ui: #888;
  --meta: #4a4a4a;
  --link: #8899ee;
  --code-fg: #c8dda0;

  --font-prose: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
  --font-ui: system-ui, -apple-system, sans-serif;
  --font-code: ui-monospace, "Cascadia Code", "SF Mono", Menlo, monospace;
}

*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }

body {
  background: var(--g0);
  color: var(--prose);
  font-family: var(--font-ui);
  font-size: 14px;
  line-height: 1.6;
  margin: 0 auto;
  max-width: 560px;
  min-height: 100vh;
  padding: 0 16px 48px;
}
main { width: 100%; padding: 18px 0 48px; }

h1, h2, h3 {
  color: var(--signal);
  font-size: 11px;
  font-weight: bold;
  letter-spacing: 0.12em;
  margin: 14px 0 6px;
  text-transform: uppercase;
}
a { color: var(--link); text-decoration: none; }
a:hover { color: var(--signal); }
.eyebrow {
  background: var(--g2);
  border: var(--bv) solid;
  border-color: var(--hi) var(--lo) var(--lo) var(--hi);
  color: var(--ui);
  font-size: 11px;
  letter-spacing: 0.12em;
  padding: 4px 10px;
  text-transform: uppercase;
  width: fit-content;
}

/* Every dashboard section is a raised platform. */
.panel {
  background: var(--g2);
  border: var(--bv-lg) solid;
  border-color: var(--hi) var(--lo) var(--lo) var(--hi);
  margin: 8px 0;
  padding: 10px;
  width: 100%;
}
.status-row {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: space-between;
}
#process-status { color: var(--signal); font-family: var(--font-code); font-weight: bold; }
.badge {
  align-items: center;
  background: var(--g3);
  border: var(--bv) solid;
  border-color: var(--hi) var(--lo) var(--lo) var(--hi);
  color: var(--ui);
  display: inline-flex;
  font-size: 11px;
  gap: 7px;
  padding: 3px 8px;
}
.dot { background: var(--meta); height: 8px; width: 8px; }
.live .dot { background: #7acc7a; }
.warn .dot { background: #cc9955; }

/* The progress track is inset; its signal is raised inside it. */
.progress-shell {
  background: var(--g1);
  border: var(--bv-lg) solid;
  border-color: var(--lo) var(--hi) var(--hi) var(--lo);
  height: 58px;
  margin: 14px 0 10px;
  overflow: hidden;
  position: relative;
}
#progress-fill {
  background: var(--link);
  border: var(--bv) solid;
  border-color: var(--hi) var(--lo) var(--lo) var(--hi);
  height: 100%;
  transition: width .35s steps(8, end);
  width: 0;
}
#progress-label {
  color: var(--signal);
  display: grid;
  font-family: var(--font-code);
  font-size: 18px;
  font-weight: bold;
  inset: 0;
  place-items: center;
  position: absolute;
  text-shadow: 1px 1px var(--lo);
}

.controls { align-items: center; display: flex; flex-wrap: wrap; gap: 8px; }
button {
  background: var(--g5);
  border: var(--bv) solid;
  border-color: var(--hi) var(--lo) var(--lo) var(--hi);
  color: var(--signal);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  padding: 4px 10px;
}
button:hover { background: #404040; }
button:active {
  background: var(--g4);
  border-color: var(--lo) var(--hi) var(--hi) var(--lo);
  transform: translate(1px, 1px);
}
button:disabled { cursor: default; opacity: .4; }
.note { color: var(--meta); font-size: 11px; margin: 4px 0; }

.feed-head { align-items: baseline; display: flex; justify-content: space-between; }
#audit-feed {
  background: var(--g1);
  border: var(--bv) solid;
  border-color: var(--lo) var(--hi) var(--hi) var(--lo);
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-top: 8px;
  padding: 6px;
}
.event {
  background: var(--g3);
  border: var(--bv) solid;
  border-color: var(--hi) var(--lo) var(--lo) var(--hi);
  display: grid;
  gap: 6px;
  grid-template-columns: 82px 88px 1fr;
  padding: 5px 8px;
}
.event[data-kind="error"] { border-left-color: #cc5555; }
.event[data-kind="complete"], .event[data-kind="ranking"] { border-left-color: #7acc7a; }
.event[data-kind="vote"] { border-left-color: var(--link); }
.event time, .event-kind { color: var(--meta); font-family: var(--font-code); font-size: 10px; }
.event-kind { text-transform: uppercase; }
.event-message { color: var(--prose); font-family: var(--font-prose); }
.event-links {
  font-family: var(--font-code);
  font-size: 10px;
  grid-column: 1 / -1;
}
.event-links a { margin-right: 10px; }

code {
  background: var(--g1);
  border: 2px solid;
  border-color: var(--lo) var(--hi) var(--hi) var(--lo);
  color: var(--code-fg);
  font-family: var(--font-code);
  font-size: 12px;
  padding: 1px 4px;
}

body.evidence-doc { max-width: 960px; }
.kv-list { display: flex; flex-direction: column; gap: 4px; margin: 8px 0; }
.kv { display: grid; gap: 8px; grid-template-columns: 140px 1fr; }
.kv .k { color: var(--meta); font-family: var(--font-code); font-size: 11px; }
.kv .v { color: var(--prose); word-break: break-word; }
pre.blob {
  background: var(--g1);
  border: var(--bv) solid;
  border-color: var(--lo) var(--hi) var(--hi) var(--lo);
  color: var(--code-fg);
  font-family: var(--font-code);
  font-size: 11px;
  line-height: 1.45;
  margin: 8px 0 16px;
  max-height: 70vh;
  overflow: auto;
  padding: 10px;
  white-space: pre-wrap;
  word-break: break-word;
}
pre.blob.compact { max-height: 12em; margin-bottom: 8px; }
ul { padding-left: 1.2em; }
li { margin: 4px 0; }
p.lede { color: var(--ui); margin: 0 0 1em; }
.dense-card {
  background: var(--g2);
  border: var(--bv) solid;
  border-color: var(--hi) var(--lo) var(--lo) var(--hi);
  margin: 0 0 10px;
  padding: 8px 10px;
}
.card-head { color: var(--ui); font-family: var(--font-code); font-size: 12px; margin-bottom: 6px; }
.reason-list { margin: 0; padding-left: 1.1em; }
.reason-list li { color: var(--prose); font-family: var(--font-prose); line-height: 1.45; }
.disagree { color: #c9a227; font-family: var(--font-code); font-size: 12px; }
.judgment {
  background: var(--g1);
  border-left: 3px solid var(--link);
  margin: 0 0 10px;
  padding: 8px 10px;
}
.judgment-meta { color: var(--ui); font-family: var(--font-code); font-size: 12px; margin-bottom: 6px; }
.judgment .winner { color: #7acc7a; }
.judgment .ratio { color: var(--meta); }
.judgment .explanation {
  color: var(--prose);
  font-family: var(--font-prose);
  font-size: 15px;
  line-height: 1.5;
  margin: 0;
  white-space: pre-wrap;
}
.sides {
  display: grid;
  gap: 12px;
  grid-template-columns: 1fr 1fr;
  margin-bottom: 12px;
}
.side {
  background: var(--g2);
  border: var(--bv) solid;
  border-color: var(--hi) var(--lo) var(--lo) var(--hi);
  padding: 8px 10px;
}
.side h3, .side h4 { margin: 0 0 6px; }
details.fold {
  background: var(--g2);
  border: 1px solid var(--g4);
  margin: 8px 0;
  padding: 6px 10px;
}
details.fold > summary {
  color: var(--ui);
  cursor: pointer;
  font-family: var(--font-code);
  font-size: 12px;
  list-style: disclosure-closed;
}
details.fold[open] > summary { margin-bottom: 8px; }
table.dense {
  border-collapse: collapse;
  font-size: 12px;
  margin: 8px 0 16px;
  width: 100%;
}
table.dense th, table.dense td {
  border-bottom: 1px solid var(--g4);
  padding: 4px 6px;
  text-align: left;
  vertical-align: top;
}
table.dense th { color: var(--meta); font-family: var(--font-code); font-weight: 500; }
table.dense td.msg { color: var(--prose); font-family: var(--font-prose); }

@media (max-width: 720px) {
  .sides { grid-template-columns: 1fr; }
}
@media (max-width: 520px) {
  .event { grid-template-columns: 72px 1fr; }
  .event-message { grid-column: 1 / -1; }
  .kv { grid-template-columns: 1fr; }
}
"""


def _watch_initial_state() -> dict:
    events = store.read()
    feed = []
    for event_ in events[-40:]:
        if isinstance(event_, GitDiscovery):
            feed.append({
                "id": f"discovery-{event_.snapshot_id}",
                "timestamp_ms": event_.timestamp_ms,
                "kind": "discovery",
                "message": (
                    f"Epoch {event_.epoch}: observed {len(event_.observations)} commits; "
                    f"{len(event_.commits)} eligible"
                ),
                "evidence_url": _evidence_path("epoch", str(event_.epoch)),
                "links": {"epoch": _evidence_path("epoch", str(event_.epoch))},
            })
        elif isinstance(event_, Emission):
            feed.append({
                "id": f"emission-{event_.epoch}",
                "timestamp_ms": event_.timestamp_ms,
                "kind": "complete",
                "message": (
                    f"Epoch {event_.epoch}: emitted {event_.total_emitted} SLG; "
                    f"ranking {event_.ranking}"
                ),
                "evidence_url": _evidence_path("epoch", str(event_.epoch)),
                "links": {"epoch": _evidence_path("epoch", str(event_.epoch))},
            })
        elif isinstance(event_, Evidence):
            feed.append({
                "id": event_.event_id,
                "timestamp_ms": event_.recorded_at_ms,
                "kind": event_.kind,
                "message": event_.payload.get("summary") or event_.kind,
                "evidence_url": _evidence_path("event", event_.event_id),
                "links": {"epoch": _evidence_path("epoch", str(event_.epoch))},
            })
    feed.extend(AUDIT_HISTORY)
    return {
        "process": dict(PROCESS_STATE),
        "openrouter_configured": bool((OPENROUTER_API_KEY or "").strip()),
        "epoch": current_epoch()[0],
        "feed": feed[-200:],
    }


_WATCH_JS = """
const initial = __INITIAL__;
const feed = document.querySelector('#audit-feed');
const processStatus = document.querySelector('#process-status');
const connection = document.querySelector('#connection-status');
const fill = document.querySelector('#progress-fill');
const progressLabel = document.querySelector('#progress-label');
const play = document.querySelector('#play');
const pause = document.querySelector('#pause');
const seen = new Set();
let source = null;

function setProgress(value) {
  const n = Math.max(0, Math.min(100, Number(value ?? 0)));
  fill.style.width = `${n}%`;
  progressLabel.textContent = `${Math.round(n)}%`;
  document.querySelector('.progress-shell').setAttribute('aria-valuenow', String(n));
}

function addEvent(event) {
  const id = String(event.id);
  if (seen.has(id)) return;
  seen.add(id);
  const row = document.createElement('div');
  row.className = 'event';
  row.dataset.kind = event.kind || 'event';
  const when = document.createElement('time');
  when.dateTime = new Date(event.timestamp_ms).toISOString();
  when.textContent = new Date(event.timestamp_ms).toLocaleTimeString();
  const kind = document.createElement('span');
  kind.className = 'event-kind';
  kind.textContent = event.kind || 'event';
  const message = document.createElement('span');
  message.className = 'event-message';
  message.textContent = event.message;
  row.append(when, kind, message);
  const linkPairs = [];
  if (event.evidence_url) linkPairs.push(['evidence', event.evidence_url]);
  if (event.links && typeof event.links === 'object') {
    for (const [label, href] of Object.entries(event.links)) {
      if (href) linkPairs.push([label, href]);
    }
  }
  if (linkPairs.length) {
    const links = document.createElement('span');
    links.className = 'event-links';
    for (const [label, href] of linkPairs) {
      const a = document.createElement('a');
      a.href = href;
      a.textContent = label;
      links.appendChild(a);
      links.appendChild(document.createTextNode(' '));
    }
    row.appendChild(links);
  }
  feed.prepend(row);
  while (feed.children.length > 200) feed.lastElementChild.remove();
}

function applyState(event) {
  processStatus.textContent = event.message || 'Waiting for the next epoch';
  setProgress(event.progress);
  if (event.kind !== 'connection') addEvent(event);
}

function connect() {
  if (source) return;
  source = new EventSource('/sse');
  connection.classList.remove('warn');
  connection.classList.add('live');
  connection.querySelector('span:last-child').textContent = 'connecting';
  play.disabled = true;
  pause.disabled = false;
  source.onopen = () => {
    connection.querySelector('span:last-child').textContent = 'live';
  };
  source.addEventListener('audit', event => applyState(JSON.parse(event.data)));
  source.onerror = () => {
    connection.classList.remove('live');
    connection.classList.add('warn');
    connection.querySelector('span:last-child').textContent = 'reconnecting';
  };
}

function disconnect() {
  if (source) source.close();
  source = null;
  connection.classList.remove('live');
  connection.classList.add('warn');
  connection.querySelector('span:last-child').textContent = 'paused locally';
  play.disabled = false;
  pause.disabled = true;
}

play.addEventListener('click', connect);
pause.addEventListener('click', disconnect);
initial.feed.forEach(addEvent);
processStatus.textContent = initial.process.message;
setProgress(initial.process.progress);
connect();
"""


@app.get("/watch")
async def watch():
    initial = json.dumps(
        _watch_initial_state(), separators=(",", ":")
    ).replace("</", "<\\/")
    script = _WATCH_JS.replace("__INITIAL__", initial)
    key_ok = bool((OPENROUTER_API_KEY or "").strip())
    return HTMLResponse(render(["html",
        ["head",
            ["meta", {"charset": "utf-8"}],
            ["meta", {"name": "viewport", "content": "width=device-width, initial-scale=1"}],
            ["title", "slug — live constitution"],
            ["style", RawContent(_WATCH_CSS)],
        ],
        ["body", ["main",
            ["div.eyebrow", f"epoch {current_epoch()[0]} · constitutional audit"],
            ["h1", "watch the process"],
            ["section.panel",
                ["div.status-row",
                    ["div#process-status", PROCESS_STATE["message"]],
                    ["div#connection-status.badge", ["span.dot"], ["span", "connecting"]],
                ],
                ["div.progress-shell", {
                    "role": "progressbar", "aria-label": "Emission progress",
                    "aria-valuemin": "0", "aria-valuemax": "100",
                    "aria-valuenow": str(PROCESS_STATE["progress"]),
                },
                    ["div#progress-fill"],
                    ["div#progress-label", f"{PROCESS_STATE['progress']}%"],
                ],
                ["div.controls",
                    ["button#play", {"type": "button", "disabled": "true"}, "▶ Play live feed"],
                    ["button#pause", {"type": "button"}, "Ⅱ Pause feed"],
                    ["span.note",
                     "Play/pause only affect this browser feed — they do not run or stop emission. "
                     "The server emits at each epoch boundary; status above is that process."],
                ],
            ],
            ["section.panel",
                ["div.status-row",
                    ["strong", "Council readiness"],
                    ["div.badge" + (".live" if key_ok else ".warn"),
                        ["span.dot"],
                        ["span", "OpenRouter configured" if key_ok else "OpenRouter key missing"],
                    ],
                ],
                ["p.note",
                    (
                        "Pairwise council ranks every eligible commit."
                        if key_ok else
                        "Epochs with two or more eligible commits require OPENROUTER_API_KEY."
                    )
                ],
            ],
            ["section.panel",
                ["div.feed-head", ["h2", "event feed"], ["a", {"href": "/sse"}, "raw SSE"]],
                ["div#audit-feed"],
            ],
            ["p", ["a", {"href": "/"}, "← constitution"], " · ",
                  ["a", {"href": "/epochs"}, "epochs"], " · ",
                  ["a", {"href": "/api/status"}, "status JSON"], " · ",
                  ["a", {"href": "/api/ledger"}, "ledger"]],
        ]],
        ["script", {"type": "module"}, RawContent(script)],
    ]))


async def redeem(github_user: str, wallet_address: str):
    await store.append(Redemption(
        timestamp_ms=int(time.time() * 1000),
        github_user=github_user,
        wallet_address=wallet_address,
        amount="0",  # TODO: compute unclaimed, execute Solana transfer
    ))
    return PlainTextResponse("location.reload()")


@app.get("/")
async def index(request: Request):
    user = request.session.get("github_user")
    if not user:
        return _page("slug constitution", ["div",
            ["h1", "slug constitution"],
            ["p", "Total supply is 1.0 SLUG ownership unit. FDV is 177,600 USDC. Initial LP seed is 1,331 USDC."],
            ["p", ["a", {"href": "/login"}, "Login with GitHub to check your emissions"]],
            ["p",
                ["a", {"href": "/watch"}, "Watch emission process live"], " | ",
                ["a", {"href": "/epochs"}, "Epoch evidence"], " | ",
                ["a", {"href": "/api/epoch"}, "Current epoch"], " | ",
                ["a", {"href": "/api/ledger"}, "Full ledger"], " | ",
                ["a", {"href": "/api/halvening"}, "Jubilee countdown"],
            ],
        ])

    events = store.read()
    history = [e for e in events if isinstance(e, Emission) and user in e.distributions]
    total_earned = sum(Decimal(e.distributions[user]) for e in history)
    emissions = [e for e in events if isinstance(e, Emission)]
    latest_rank = emissions[-1].ranking.get(user) if emissions else None

    redeem_form = ["form", {"method": "post"},
        *signer.snippet_hidden(f"redeem({json.dumps(user)}, $wallet_address)"),
        ["input", {"name": "wallet_address", "placeholder": "Solana wallet address", "required": "true"}],
        ["button", {"type": "submit"}, "Redeem"],
    ]

    return _page(f"slug — {user}", ["div",
        ["h1", f"Welcome, {user}"],
        ["p", f"Total earned: {total_earned:.12f} SLUG ownership"],
        ["p", f"Latest rank score: {latest_rank or 'no emissions yet'}"],
        ["h2", "Redeem to Solana wallet"],
        redeem_form,
        ["p",
            ["a", {"href": "/api/epoch"}, "Current epoch"], " | ",
            ["a", {"href": "/epochs"}, "Epoch evidence"], " | ",
            ["a", {"href": "/api/ledger"}, "Full ledger"], " | ",
            ["a", {"href": "/watch"}, "Watch emission live"],
        ],
    ])


@app.post("/")
async def do(request: Request):
    form = await request.form()
    try:
        snippet = signer.verify_snippet(form)
        result = eval(snippet)
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
    if os.environ.get("DISABLE_EPOCH_LOOP") != "1":
        asyncio.create_task(epoch_loop())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
