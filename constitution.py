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

# Council model IDs: slug.social garden rank under this parent (bodies = OpenRouter URLs), then top-up from OpenRouter list.
SLUG_SOCIAL_BASE_URL = os.environ.get("SLUG_SOCIAL_BASE_URL", "https://slug.social").rstrip("/")
SLUG_MODEL_RANK_PARENT = os.environ.get(
    "SLUG_MODEL_RANK_PARENT", "slug/token/commit-ranking/model"
).strip()


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
    discovery_snapshot_id: str = ""  # empty only for pre-discovery ledger history


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


store = JsonlStore(JSONL_PATH)


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


async def _fetch_models_openrouter_only(
    client: httpx.AsyncClient, n: int, exclude: set[str]
) -> list[str]:
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
    out = []
    for m in chat_models:
        mid = m["id"]
        if mid in exclude:
            continue
        out.append(mid)
        if len(out) >= n:
            break
    return out


async def fetch_top_models(n=3):
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0)
    ) as client:
        got: list[str] = []
        if SLUG_MODEL_RANK_PARENT:
            try:
                got = await _fetch_models_from_slug_rank_parent(
                    client, SLUG_MODEL_RANK_PARENT, n
                )
            except Exception:
                got = []
        if len(got) < n and (OPENROUTER_API_KEY or "").strip():
            rest = await _fetch_models_openrouter_only(
                client, n - len(got), exclude=set(got)
            )
            got.extend(rest)
        return got[:n]


def _retry_llm_pairwise(exc: BaseException) -> bool:
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, TypeError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 425, 429, 500, 502, 503, 504)
    return isinstance(exc, httpx.RequestError)


@retry(
    retry=retry_if_exception(_retry_llm_pairwise),
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=1, min=1, max=120),
    reraise=True,
)
async def llm_pairwise_compare(model_id, side_a, side_b):
    prompt = f"""You are ranking contributions to an open source project.
Compare these two sides (each may be one or more commits). Decide which side contributed more.
Return ONLY a JSON object: {{"winner": "A" or "B", "ratio": "N:M", "explanation": "..."}}

Side A — commit messages:
{side_a['message']}

Side A — unified diffs (full patches):
{side_a['diff']}

Side B — commit messages:
{side_b['message']}

Side B — unified diffs (full patches):
{side_b['diff']}"""

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0)) as client:
        resp = await client.post(
            f"{OPENROUTER_BASE_URL}/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            json={"model": model_id, "messages": [{"role": "user", "content": prompt}]},
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)


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
            if appended:
                return appended
            return next(
                e for e in store.read()
                if isinstance(e, GitDiscovery) and e.epoch == epoch_n
            )
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


async def rank_commits(commits: list[dict]):
    if not commits:
        return {}, []

    contributors = sorted(set(c["contributor"] for c in commits))
    if len(contributors) == 1:
        await broadcast_audit(
            "ranking",
            f"Only {contributors[0]} is eligible; rank is 1.0",
            progress=90,
            phase="finalizing",
        )
        return {contributors[0]: Decimal("1")}, []
    if not (OPENROUTER_API_KEY or "").strip():
        raise RuntimeError(
            "OPENROUTER_API_KEY is required when multiple contributors need ranking"
        )

    models = await fetch_top_models(n=3)
    if not models:
        raise RuntimeError("no council models available for contributor ranking")
    await broadcast_audit(
        "council",
        f"Council selected: {', '.join(models)}",
        progress=35,
        phase="ranking",
    )
    await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
        ["div.log-council", f"Council: {', '.join(models)} — {len(commits)} commits"]
    ]))

    authors = contributors
    author_idx = {a: i for i, a in enumerate(authors)}

    author_commits = {a: [] for a in authors}
    for c in sorted(commits, key=lambda row: row["oid"]):
        author_commits[c["contributor"]].append({
            "message": c["message"],
            "sha": c["oid"].split(":", 1)[1][:8],
            "diff": c["patch"],
        })

    # TODO do we want ot coagulate the commits into a single block? or rank the many commits
    def author_side_for_llm(author):
        cs = author_commits[author]
        return {
            "message": "\n".join(f"[{c['sha']}] {c['message']}" for c in cs),
            "diff": "\n\n".join(f"=== {c['sha']} ===\n{c['diff']}" for c in cs),
        }

    async def compare_fn(i, j):
        a1, a2 = authors[i], authors[j]
        await broadcast_audit(
            "comparison",
            f"Comparing {a1} with {a2}",
            phase="ranking",
        )
        await broadcast_js(exec_event(Three[Selector("#emission-status")][MORPH][
            ["div#emission-status", f"Comparing {a1} vs {a2}…"]
        ]))
        results = []
        for model in models:
            try:
                result = await llm_pairwise_compare(model, author_side_for_llm(a1), author_side_for_llm(a2))
                if result["winner"] not in {"A", "B"}:
                    raise ValueError("winner must be A or B")
                w, l = (i, j) if result["winner"] == "A" else (j, i)
                ratio = result["ratio"].split(":")
                winner_weight, loser_weight = float(ratio[0]), float(ratio[1])
                if winner_weight <= 0 or loser_weight <= 0:
                    raise ValueError("ratio weights must be positive")
                results.append((w, l, winner_weight, loser_weight))
                await broadcast_audit(
                    "vote",
                    f"{model}: {authors[w]} over {authors[l]} ({result['ratio']})",
                    phase="ranking",
                )
                await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
                    ["div.log-vote",
                        ["span.model", model], " — ",
                        ["span.winner", authors[w]], f" beat ",
                        ["span.loser", authors[l]], f" ({result['ratio']}) ",
                        ["span.explanation", result["explanation"]],
                    ]
                ]))
            except Exception as e:
                await broadcast_audit(
                    "error",
                    f"{model} failed: {e}",
                    phase="error",
                )
                await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
                    ["div.log-error", f"⚠ {model}: {e}"]
                ]))
                raise RuntimeError(f"council model failed: {model}") from e
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

    pairs = await pairwise_rank(len(authors), compare_fn, progress_fn)

    if not pairs:
        ranking = {authors[0]: Decimal("1")} if authors else {}
        return ranking, models

    scores = rank_centrality(pairs)
    ranking = {authors[i]: Decimal(str(scores[i])) for i in range(len(authors))}
    ranking_rows = sorted(ranking.items(), key=lambda x: x[1], reverse=True)
    await broadcast_audit(
        "ranking",
        "Ranking: " + ", ".join(f"{a} {s:.4f}" for a, s in ranking_rows),
        progress=90,
        phase="finalizing",
    )
    await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
        ["div.log-ranking",
            ["b", "Ranking: "],
            *[["span.rank-entry", f"{a} {float(s):.3f}  "] for a, s in ranking_rows],
        ]
    ]))
    return ranking, models


# ===========================================================================
# §5. EMISSION — the pool decays, contributors receive
# ===========================================================================

DECAY_RATE = 1 - (Decimal("0.5").ln() / (HALF_LIFE_YEARS * 12)).exp()

def pool_remaining(events: list) -> Decimal:
    emitted = sum(Decimal(e.total_emitted) for e in events if isinstance(e, Emission))
    return CONTRIBUTOR_POOL - emitted


async def run_emission(epoch_n, boundary_ms):
    PROCESS_STATE["running"] = True
    await broadcast_audit(
        "start",
        f"Epoch {epoch_n} emission started",
        progress=2,
        phase="starting",
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
    )
    ranking, models = await rank_commits(discovery.commits)

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

@app.get("/api/ledger")
async def get_ledger(offset: int = 0, limit: int = 100):
    return [to_dict(e) for e in store.read()[offset:offset + limit]]


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

code {
  background: var(--g1);
  border: 2px solid;
  border-color: var(--lo) var(--hi) var(--hi) var(--lo);
  color: var(--code-fg);
  font-family: var(--font-code);
  font-size: 12px;
  padding: 1px 4px;
}

@media (max-width: 520px) {
  .event { grid-template-columns: 72px 1fr; }
  .event-message { grid-column: 1 / -1; }
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
                    ["span.note", "Pause stops local updates; the constitutional process continues."],
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
                        "Pairwise council voting is ready."
                        if key_ok else
                        "Single-contributor epochs can finalize, but contested rankings require OPENROUTER_API_KEY."
                    )
                ],
            ],
            ["section.panel",
                ["div.feed-head", ["h2", "event feed"], ["a", {"href": "/sse"}, "raw SSE"]],
                ["div#audit-feed"],
            ],
            ["p", ["a", {"href": "/"}, "← constitution"], " · ",
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
