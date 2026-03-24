#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi",
#   "uvicorn",
#   "httpx",
#   "tenacity",
#   "evaleval>=0.2.6",
#   "authlib",
#   "itsdangerous",
#   "starlette",
#   "numpy",
# ]
# ///
"""
constitution.py — the entire economic mechanism of slug in one file.

This file is the constitution. It runs on fly.io as a single process.
The code is auditable in a public GitHub repo. Every commit is a public diff.
A daily GitHub Action backs up the JSONL ledger to the same repo.

Run: uv run constitution.py
"""

from decimal import Decimal, getcontext
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
import json, time, os, asyncio, httpx, pathlib
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from evaleval import (
    event, JsonlStore, to_dict, render, RawContent, Signer, SnippetExecutionError,
    exec_event, One, Two, Three, Selector, MORPH, PREPEND,
)

getcontext().prec = 50

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.environ["SESSION_SECRET"])
signer = Signer()

# ===========================================================================
# §1. CONSTANTS — the two free parameters and everything derived from them
# ===========================================================================

HALF_LIFE_YEARS = Decimal("17.72577371892")

TOTAL_SUPPLY = Decimal("177600")
LP_TOKENS = Decimal("1776")
CONTRIBUTOR_POOL = TOTAL_SUPPLY - LP_TOKENS
EPOCHS_PER_HALFLIFE = HALF_LIFE_YEARS * 12
DECAY_RATE = 1 - (Decimal("0.5").ln() / EPOCHS_PER_HALFLIFE).exp()

GENESIS_MS = int(os.environ["GENESIS_MS"])
JSONL_PATH = pathlib.Path(os.environ.get("JSONL_PATH", "/data/ledger.jsonl"))

SOLANA_RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
MINT_ADDRESS = os.environ.get("MINT_ADDRESS", "")
TREASURY_ADDRESS = os.environ.get("TREASURY_ADDRESS", "")

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
REPO = os.environ.get("REPO", "tommy-mor/slug")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai").rstrip("/")
GITHUB_API_BASE_URL = os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/")

# Council model IDs: slug.social garden rank under this parent (bodies = OpenRouter URLs), then top-up from OpenRouter list.
SLUG_SOCIAL_BASE_URL = os.environ.get("SLUG_SOCIAL_BASE_URL", "https://slug.social").rstrip("/")
SLUG_MODEL_RANK_PARENT = os.environ.get(
    "SLUG_MODEL_RANK_PARENT", "slug/token/commit-ranking/model"
).strip()


# ===========================================================================
# §1b. LEDGER SCHEMA — typed events
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


store = JsonlStore(JSONL_PATH)


# ===========================================================================
# §2. LASKAR POLYNOMIAL — the planet decides when epochs turn
# ===========================================================================

J2000_UNIX_MS = Decimal("946728000000")
JULIAN_CENTURY_MS = Decimal("36525") * 86400 * 1000
DAY_MS = Decimal("86400000")

A0 = Decimal("365.2421896698")
A1 = Decimal("-6.15359E-6")
A2 = Decimal("-7.29E-10")
A3 = Decimal("2.64E-10")


def T_from_unix_ms(ms):
    return (Decimal(ms) - J2000_UNIX_MS) / JULIAN_CENTURY_MS


def tropical_epoch_ms(unix_ms):
    T = T_from_unix_ms(unix_ms)
    days = A0 + A1 * T + A2 * T**2 + A3 * T**3
    return days * DAY_MS / 12


def epoch_boundary(n):
    boundary = Decimal(GENESIS_MS)
    for e in range(n):
        boundary += tropical_epoch_ms(boundary)
    return int(round(boundary))


def current_epoch():
    now = int(time.time() * 1000)
    boundary = Decimal(GENESIS_MS)
    for e in range(10000):
        next_boundary = boundary + tropical_epoch_ms(boundary)
        if int(round(next_boundary)) > now:
            return e, int(round(boundary)), int(round(next_boundary))
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


def _github_headers():
    h = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def unified_diff_from_commit_payload(payload: dict) -> str:
    parts = []
    for f in payload.get("files") or []:
        name = f.get("filename", "?")
        patch = f.get("patch")
        if patch:
            parts.append(f"--- {name}\n{patch}")
        else:
            parts.append(f"--- {name}\n[no textual patch: binary, submodule, or too large]\n")
    return "\n\n".join(parts) if parts else "[no files in API response]"


async def fetch_commits_since(since_ms):
    since_iso = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).isoformat()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API_BASE_URL}/repos/{REPO}/commits",
            params={"since": since_iso, "per_page": 100},
            headers=_github_headers(),
        )
        return resp.json()


async def fetch_commit_unified_diff(client: httpx.AsyncClient, sha: str) -> str:
    resp = await client.get(
        f"{GITHUB_API_BASE_URL}/repos/{REPO}/commits/{sha}",
        headers=_github_headers(),
    )
    resp.raise_for_status()
    return unified_diff_from_commit_payload(resp.json())


SSE_CLIENTS = []


async def broadcast_js(js: str):
    """Send a JS snippet to all connected SSE clients."""
    for queue in SSE_CLIENTS:
        await queue.put(js)


async def rank_commits(since_ms):
    commits = await fetch_commits_since(since_ms)
    if not commits:
        return {}

    async with httpx.AsyncClient() as gh:
        commit_diffs = await asyncio.gather(*[fetch_commit_unified_diff(gh, c["sha"]) for c in commits])

    models = await fetch_top_models(n=3)
    await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
        ["div.log-council", f"Council: {', '.join(models)} — {len(commits)} commits"]
    ]))

    authors = list(set(c["commit"]["author"]["name"] for c in commits))
    author_idx = {a: i for i, a in enumerate(authors)}

    author_commits = {a: [] for a in authors}
    for c, diff_text in zip(commits, commit_diffs, strict=True):
        author_commits[c["commit"]["author"]["name"]].append({
            "message": c["commit"]["message"],
            "sha": c["sha"][:8],
            "diff": diff_text,
        })

    def author_side_for_llm(author):
        cs = author_commits[author]
        return {
            "message": "\n".join(f"[{c['sha']}] {c['message']}" for c in cs),
            "diff": "\n\n".join(f"=== {c['sha']} ===\n{c['diff']}" for c in cs),
        }

    async def compare_fn(i, j):
        a1, a2 = authors[i], authors[j]
        await broadcast_js(exec_event(Three[Selector("#emission-status")][MORPH][
            ["div#emission-status", f"Comparing {a1} vs {a2}…"]
        ]))
        results = []
        for model in models:
            try:
                result = await llm_pairwise_compare(model, author_side_for_llm(a1), author_side_for_llm(a2))
                w, l = (i, j) if result["winner"] == "A" else (j, i)
                ratio = result["ratio"].split(":")
                results.append((w, l, float(ratio[0]), float(ratio[1])))
                await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
                    ["div.log-vote",
                        ["span.model", model], " — ",
                        ["span.winner", authors[w]], f" beat ",
                        ["span.loser", authors[l]], f" ({result['ratio']}) ",
                        ["span.explanation", result["explanation"]],
                    ]
                ]))
            except Exception as e:
                await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
                    ["div.log-error", f"⚠ {model}: {e}"]
                ]))
        return results

    async def progress_fn(ev):
        if ev["phase"] == "spanning_tree":
            label = f"Spanning tree: {ev['step']}/{ev['total']}"
        else:
            label = f"Zip pass {ev['pass']}: {ev['step']}/{ev['total']}"
        await broadcast_js(exec_event(Three[Selector("#emission-status")][MORPH][
            ["div#emission-status", label]
        ]))

    pairs = await pairwise_rank(len(authors), compare_fn, progress_fn)

    if not pairs:
        return {authors[0]: Decimal("1")} if authors else {}

    scores = rank_centrality(pairs)
    ranking = {authors[i]: Decimal(str(scores[i])) for i in range(len(authors))}
    ranking_rows = sorted(ranking.items(), key=lambda x: x[1], reverse=True)
    await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
        ["div.log-ranking",
            ["b", "Ranking: "],
            *[["span.rank-entry", f"{a} {float(s):.3f}  "] for a, s in ranking_rows],
        ]
    ]))
    return ranking


# ===========================================================================
# §5. EMISSION — the pool decays, contributors receive
# ===========================================================================

def pool_remaining(events: list) -> Decimal:
    emitted = sum(Decimal(e.total_emitted) for e in events if isinstance(e, Emission))
    return CONTRIBUTOR_POOL - emitted


async def run_emission(epoch_n, boundary_ms):
    await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
        ["div.log-start", f"⚡ Epoch {epoch_n} emission started"]
    ]))

    pool = pool_remaining(store.read())
    emission = pool * DECAY_RATE
    await broadcast_js(exec_event(Three[Selector("#emission-log")][PREPEND][
        ["div.log-amount",
            f"Pool {pool:.4f} → emit {emission:.4f} → {pool - emission:.4f}"]
    ]))

    prev_boundary_ms = epoch_boundary(epoch_n - 1) if epoch_n > 0 else GENESIS_MS
    ranking = await rank_commits(prev_boundary_ms)

    def make_emission(events):
        if epoch_n in {e.epoch for e in events if isinstance(e, Emission)}:
            return None
        pool_now = pool_remaining(events)
        emission_now = pool_now * DECAY_RATE
        return Emission(
            epoch=epoch_n,
            timestamp_ms=boundary_ms,
            pool_before=str(pool_now),
            total_emitted=str(emission_now),
            pool_after=str(pool_now - emission_now),
            decay_rate=str(DECAY_RATE),
            distributions={a: str(emission_now * s) for a, s in ranking.items()},
            ranking={a: str(s) for a, s in ranking.items()},
            models_used=[],
        )

    entry = await store.atomic(make_emission)
    if entry:
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
        now = int(time.time() * 1000)
        wait_ms = next_boundary - now

        if wait_ms <= 0:
            processed = {e.epoch for e in store.read() if isinstance(e, Emission)}
            if epoch_n not in processed and epoch_n >= 0:
                await run_emission(epoch_n, current_start)
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
    boundary = Decimal(GENESIS_MS)
    elapsed_years = Decimal("0")
    for e in range(300):
        epoch_dur = tropical_epoch_ms(boundary)
        epoch_years = Decimal("1") / 12
        if elapsed_years + epoch_years >= HALF_LIFE_YEARS:
            fraction = (HALF_LIFE_YEARS - elapsed_years) / epoch_years
            jubilee_ms = int(boundary + fraction * epoch_dur)
            dt = datetime.fromtimestamp(jubilee_ms / 1000, tz=timezone.utc)
            return {"jubilee_ms": jubilee_ms, "jubilee_utc": dt.isoformat(),
                    "epoch": e + float(fraction), "half_life_years": str(HALF_LIFE_YEARS)}
        elapsed_years += epoch_years
        boundary += epoch_dur


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
#
# TODO: the /sse emission audit page needs a real SSE-driven UI. votes arrive
# incrementally during rank_commits(), and the client should show a live
# progress bar and per-vote results as they stream in. this requires a
# dedicated page that connects to /sse and updates the DOM on each event
# (council, comparing, vote, ranking, emission_complete). defer until we
# have playwright tests to cover it — the incremental rendering is fiddly.
# ===========================================================================

@app.get("/sse")
async def sse_stream(request: Request):
    queue = asyncio.Queue()
    SSE_CLIENTS.append(queue)

    async def generate():
        try:
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
            SSE_CLIENTS.remove(queue)

    from starlette.responses import StreamingResponse
    return StreamingResponse(generate(), media_type="text/event-stream")


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
        ],
        ["body",
            body,
            ["script", RawContent(_FORM_INTERCEPT_JS)],
        ],
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
            ["p", "177,600 tokens. $1 each. Promethium half-life. Laskar polynomial epochs."],
            ["p", ["a", {"href": "/login"}, "Login with GitHub to check your emissions"]],
            ["p",
                ["a", {"href": "/sse"}, "Watch emission process live"], " | ",
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
        ["p", f"Total earned: {total_earned:.6f} SLUG"],
        ["p", f"Latest rank score: {latest_rank or 'no emissions yet'}"],
        ["h2", "Redeem to Solana wallet"],
        redeem_form,
        ["p",
            ["a", {"href": "/api/epoch"}, "Current epoch"], " | ",
            ["a", {"href": "/api/ledger"}, "Full ledger"], " | ",
            ["a", {"href": "/sse"}, "Watch emission live"],
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
