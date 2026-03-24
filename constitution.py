#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi",
#   "uvicorn",
#   "httpx",
#   "tenacity",
#   "evaleval",
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
from fastapi.responses import PlainTextResponse
from starlette.middleware.sessions import SessionMiddleware
import json, time, os, asyncio, hashlib, hmac, httpx, pathlib, threading
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

getcontext().prec = 50

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.environ["SESSION_SECRET"])

# ===========================================================================
# §1. CONSTANTS — the two free parameters and everything derived from them
# ===========================================================================

# Free parameter 1: Promethium-145 half-life (Wolfram ElementData)
HALF_LIFE_YEARS = Decimal("17.72577371892")

# Free parameter 2: epoch duration = 1/12 tropical year (Laskar 1986)
# Not a constant. Evaluated per epoch. See §2.

# Derived constants
TOTAL_SUPPLY = Decimal("177600")
LP_TOKENS = Decimal("1776")
CONTRIBUTOR_POOL = TOTAL_SUPPLY - LP_TOKENS
EPOCHS_PER_HALFLIFE = HALF_LIFE_YEARS * 12
DECAY_RATE = 1 - (Decimal("0.5").ln() / EPOCHS_PER_HALFLIFE).exp()

GENESIS_MS = int(os.environ["GENESIS_MS"])  # set once, never changed
JSONL_PATH = pathlib.Path(os.environ.get("JSONL_PATH", "/data/ledger.jsonl"))

# Solana / token config
SOLANA_RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
MINT_ADDRESS = os.environ.get("MINT_ADDRESS", "")
TREASURY_ADDRESS = os.environ.get("TREASURY_ADDRESS", "")

# GitHub OAuth
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
REPO = os.environ.get("REPO", "tommy-mor/slug")  # for commit log

# OpenRouter (override base URL for integration tests against a mock)
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai").rstrip("/")

# GitHub REST API (override for mocks; OAuth still uses github.com)
GITHUB_API_BASE_URL = os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/")


# ===========================================================================
# §2. LASKAR POLYNOMIAL — the planet decides when epochs turn
# ===========================================================================

J2000_UNIX_MS = Decimal("946728000000")
JULIAN_CENTURY_MS = Decimal("36525") * 86400 * 1000
DAY_MS = Decimal("86400000")

# Laskar (1986) coefficients for mean tropical year
A0 = Decimal("365.2421896698")
A1 = Decimal("-6.15359E-6")
A2 = Decimal("-7.29E-10")
A3 = Decimal("2.64E-10")


def T_from_unix_ms(ms):
    """Julian centuries from J2000.0"""
    return (Decimal(ms) - J2000_UNIX_MS) / JULIAN_CENTURY_MS


def tropical_epoch_ms(unix_ms):
    """Duration of one epoch (1/12 tropical year) at a given moment"""
    T = T_from_unix_ms(unix_ms)
    days = A0 + A1 * T + A2 * T**2 + A3 * T**3
    return days * DAY_MS / 12


def epoch_boundary(n):
    """Compute the exact millisecond of epoch boundary n from genesis"""
    boundary = Decimal(GENESIS_MS)
    for e in range(n):
        boundary += tropical_epoch_ms(boundary)
    return int(round(boundary))


def current_epoch():
    """Which epoch are we in right now?"""
    now = int(time.time() * 1000)
    boundary = Decimal(GENESIS_MS)
    for e in range(10000):  # safety limit
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
    """
    pairs: list of (winner_idx, loser_idx, w_ratio, l_ratio)
    returns: stationary distribution (scores that sum to 1)
    """
    items = set()
    for w, l, wr, lr in pairs:
        items.add(w)
        items.add(l)
    n = max(items) + 1
    W = np.zeros((n, n))
    for w, l, wr, lr in pairs:
        W[w][l] = wr
        W[l][w] = lr
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

async def fetch_top_models(n=3):
    """Query OpenRouter for top recent models"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{OPENROUTER_BASE_URL}/api/v1/models",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
        )
        models = resp.json()["data"]
        # Filter for chat models, sort by creation date, take top n
        chat_models = [m for m in models if "chat" in m.get("id", "")]
        # TODO: better filtering — by quality rankings, context length, etc.
        chat_models.sort(key=lambda m: m.get("created", 0), reverse=True)
        return [m["id"] for m in chat_models[:n]]


def _retry_llm_pairwise(exc: BaseException) -> bool:
    """Retry transient OpenRouter/network failures and flaky model output."""
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
    """Ask one LLM: which author's work (messages + full diffs) weighed more?"""
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
        result = json.loads(content)
        return result


def _github_headers():
    h = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def unified_diff_from_commit_payload(payload: dict) -> str:
    """Full textual diff from a GitHub GET /commits/{sha} JSON body (files[].patch)."""
    parts = []
    for f in payload.get("files") or []:
        name = f.get("filename", "?")
        patch = f.get("patch")
        if patch:
            parts.append(f"--- {name}\n{patch}")
        else:
            parts.append(
                f"--- {name}\n[no textual patch from GitHub: binary, submodule, or too large]\n"
            )
    return "\n\n".join(parts) if parts else "[no files in API response]"


async def fetch_commits_since(since_ms):
    """Pull commits from the repo since last epoch (SHAs only; no per-file patches)."""
    since_iso = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).isoformat()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API_BASE_URL}/repos/{REPO}/commits",
            params={"since": since_iso, "per_page": 100},
            headers=_github_headers(),
        )
        return resp.json()


async def fetch_commit_unified_diff(client: httpx.AsyncClient, sha: str) -> str:
    """One commit's full unified diff via the single-commit API."""
    resp = await client.get(
        f"{GITHUB_API_BASE_URL}/repos/{REPO}/commits/{sha}",
        headers=_github_headers(),
    )
    resp.raise_for_status()
    return unified_diff_from_commit_payload(resp.json())


SSE_CLIENTS = []  # list of asyncio.Queue for live streaming


async def broadcast_sse(event_type, data):
    """Send an SSE event to all connected clients"""
    for queue in SSE_CLIENTS:
        await queue.put(f"event: {event_type}\ndata: {json.dumps(data)}\n\n")


async def rank_commits(since_ms):
    """
    Full commit ranking pipeline:
    1. Fetch commits since last epoch
    2. Query OpenRouter for top models
    3. Do pairwise comparisons across council
    4. Run rank centrality
    5. Stream everything via SSE
    """
    commits = await fetch_commits_since(since_ms)
    if not commits:
        return {}

    async with httpx.AsyncClient() as gh:
        diff_tasks = [fetch_commit_unified_diff(gh, c["sha"]) for c in commits]
        commit_diffs = await asyncio.gather(*diff_tasks)

    models = await fetch_top_models(n=3)
    await broadcast_sse("council", {"models": models, "commits": len(commits)})

    # Map commit authors to indices
    authors = list(set(c["commit"]["author"]["name"] for c in commits))
    author_idx = {a: i for i, a in enumerate(authors)}

    # Group commits by author with full unified diff per commit (GitHub files[].patch)
    author_commits = {a: [] for a in authors}
    for c, diff_text in zip(commits, commit_diffs, strict=True):
        author_commits[c["commit"]["author"]["name"]].append({
            "message": c["commit"]["message"],
            "sha": c["sha"][:8],
            "diff": diff_text,
        })

    def author_side_for_llm(author: str) -> dict:
        lines_msg = []
        blocks_diff = []
        for c in author_commits[author]:
            lines_msg.append(f"[{c['sha']}] {c['message']}")
            blocks_diff.append(f"=== {c['sha']} ===\n{c['diff']}")
        return {
            "message": "\n".join(lines_msg),
            "diff": "\n\n".join(blocks_diff),
        }

    # Pairwise comparisons: each pair of authors, each model votes
    pairs = []
    for i, a1 in enumerate(authors):
        for j, a2 in enumerate(authors):
            if i >= j:
                continue
            for model in models:
                await broadcast_sse("comparing", {
                    "model": model, "a": a1, "b": a2
                })
                try:
                    result = await llm_pairwise_compare(
                        model,
                        author_side_for_llm(a1),
                        author_side_for_llm(a2),
                    )
                    w, l = (a1, a2) if result["winner"] == "A" else (a2, a1)
                    ratio = result["ratio"].split(":")
                    pairs.append((author_idx[w], author_idx[l],
                                  float(ratio[0]), float(ratio[1])))
                    await broadcast_sse("vote", {
                        "model": model, "winner": w, "loser": l,
                        "ratio": result["ratio"],
                        "explanation": result["explanation"]
                    })
                except Exception as e:
                    await broadcast_sse("error", {"model": model, "error": str(e)})

    if not pairs:
        return {authors[0]: Decimal("1")} if authors else {}

    scores = rank_centrality(pairs)
    ranking = {authors[i]: Decimal(str(scores[i])) for i in range(len(authors))}
    await broadcast_sse("ranking", {a: float(s) for a, s in ranking.items()})
    return ranking


# ===========================================================================
# §5. EMISSION — the pool decays, contributors receive
# ===========================================================================

def read_ledger():
    """Read the full JSONL ledger"""
    if not JSONL_PATH.exists():
        return []
    with open(JSONL_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def append_ledger(entry):
    """Append one entry to the JSONL ledger"""
    with open(JSONL_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def pool_remaining():
    """Compute current pool balance from ledger"""
    emitted = sum(
        Decimal(str(e["total_emitted"]))
        for e in read_ledger()
        if e["type"] == "emission"
    )
    return CONTRIBUTOR_POOL - emitted


async def run_emission(epoch_n, boundary_ms):
    """Execute one epoch's emission"""
    await broadcast_sse("emission_start", {"epoch": epoch_n, "boundary_ms": boundary_ms})

    pool = pool_remaining()
    emission = pool * DECAY_RATE
    await broadcast_sse("emission_amount", {
        "pool_before": str(pool),
        "emission": str(emission),
        "pool_after": str(pool - emission),
        "decay_rate": str(DECAY_RATE),
    })

    # Find the previous epoch boundary for commit window
    prev_boundary_ms = epoch_boundary(epoch_n - 1) if epoch_n > 0 else GENESIS_MS

    # Rank contributors for this epoch
    ranking = await rank_commits(prev_boundary_ms)

    # Distribute emission by ranking scores
    distributions = {}
    for author, score in ranking.items():
        amount = emission * score
        distributions[author] = str(amount)

    entry = {
        "type": "emission",
        "epoch": epoch_n,
        "timestamp_ms": boundary_ms,
        "pool_before": str(pool),
        "total_emitted": str(emission),
        "pool_after": str(pool - emission),
        "decay_rate": str(DECAY_RATE),
        "distributions": distributions,
        "ranking": {a: str(s) for a, s in ranking.items()},
        "models_used": [],  # filled during rank_commits
    }
    append_ledger(entry)
    await broadcast_sse("emission_complete", entry)
    return entry


# ===========================================================================
# §6. FOUR FUNCTIONS — query holdings, rankings, treasury, distribute USDC
# ===========================================================================

async def query_token_holdings():
    """§6.1 — Query $SLUG token ownership on Solana"""
    # TODO: query Solana RPC for token accounts of MINT_ADDRESS
    # Returns: {wallet_address: Decimal(balance), ...}
    return {}


async def query_commit_ranking_diff(since_ms):
    """§6.2 — Query commit-ranking diff since last epoch"""
    # This is done in §4 during emission, results stored in ledger
    pass


async def query_treasury_balance():
    """§6.3 — Query treasury USDC balance"""
    # TODO: query Solana RPC for USDC balance of TREASURY_ADDRESS
    return Decimal("0")


async def distribute_usdc(holdings, treasury_balance):
    """§6.4 — Distribute USDC to all $SLUG holders proportionally
    The contributor pool's share flows through to contributors by score.
    """
    if treasury_balance == 0:
        return

    total_supply_held = sum(holdings.values())
    if total_supply_held == 0:
        return

    distributions = {}
    for wallet, balance in holdings.items():
        share = balance / total_supply_held
        usdc_amount = treasury_balance * share
        distributions[wallet] = str(usdc_amount)

    entry = {
        "type": "usdc_distribution",
        "timestamp_ms": int(time.time() * 1000),
        "treasury_balance": str(treasury_balance),
        "distributions": distributions,
    }
    append_ledger(entry)
    # TODO: execute Solana transfers
    return entry


# ===========================================================================
# §7. EPOCH TIMER — sleep until the exact millisecond
# ===========================================================================

async def epoch_loop():
    """Background task: check for epoch boundaries, sleep until exact ms"""
    while True:
        epoch_n, current_start, next_boundary = current_epoch()
        now = int(time.time() * 1000)
        wait_ms = next_boundary - now

        if wait_ms <= 0:
            # Epoch boundary has passed, check if we already processed it
            ledger = read_ledger()
            processed_epochs = {e["epoch"] for e in ledger if e["type"] == "emission"}
            if epoch_n not in processed_epochs and epoch_n >= 0:
                await run_emission(epoch_n, current_start)
            await asyncio.sleep(60)  # check again in a minute

        elif wait_ms < 86_400_000:  # within 24 hours
            await broadcast_sse("waiting", {
                "next_epoch": epoch_n + 1,
                "boundary_ms": next_boundary,
                "wait_seconds": wait_ms / 1000,
            })
            # Sleep until the exact millisecond
            await asyncio.sleep(wait_ms / 1000)
            await run_emission(epoch_n + 1, next_boundary)

        else:
            # More than 24 hours away, check again in an hour
            await asyncio.sleep(3600)


# ===========================================================================
# §8. API — read the ledger, query state
# ===========================================================================

@app.get("/api/ledger")
async def get_ledger(offset: int = 0, limit: int = 100):
    """Full ledger, paginated. Passwords and secrets are never in the JSONL."""
    ledger = read_ledger()
    return ledger[offset : offset + limit]


@app.get("/api/epoch")
async def get_epoch():
    """Current epoch info"""
    epoch_n, start, next_b = current_epoch()
    pool = pool_remaining()
    return {
        "epoch": epoch_n,
        "start_ms": start,
        "next_boundary_ms": next_b,
        "pool_remaining": str(pool),
        "pool_pct": str(pool / CONTRIBUTOR_POOL * 100),
        "total_emitted": str(CONTRIBUTOR_POOL - pool),
        "decay_rate_per_epoch": str(DECAY_RATE),
    }


@app.get("/api/ranking")
async def get_ranking():
    """Current contributor rankings from most recent emission"""
    ledger = read_ledger()
    emissions = [e for e in ledger if e["type"] == "emission"]
    if not emissions:
        return {"ranking": {}, "epoch": -1}
    latest = emissions[-1]
    return {"ranking": latest["ranking"], "epoch": latest["epoch"]}


@app.get("/api/contributor/{github_username}")
async def get_contributor(github_username: str):
    """A contributor's full history: emissions received, rank per epoch"""
    ledger = read_ledger()
    history = []
    for entry in ledger:
        if entry["type"] == "emission":
            if github_username in entry.get("distributions", {}):
                history.append({
                    "epoch": entry["epoch"],
                    "amount": entry["distributions"][github_username],
                    "rank_score": entry["ranking"].get(github_username),
                })
    total = sum(Decimal(h["amount"]) for h in history)
    return {"contributor": github_username, "total_earned": str(total), "history": history}


@app.get("/api/halvening")
async def get_halvening():
    """When does the pool cross 50%? The jubilee."""
    boundary = Decimal(GENESIS_MS)
    elapsed_years = Decimal("0")
    for e in range(300):
        epoch_dur = tropical_epoch_ms(boundary)
        epoch_years = Decimal("1") / 12
        if elapsed_years + epoch_years >= HALF_LIFE_YEARS:
            fraction = (HALF_LIFE_YEARS - elapsed_years) / epoch_years
            jubilee_ms = int(boundary + fraction * epoch_dur)
            dt = datetime.fromtimestamp(jubilee_ms / 1000, tz=timezone.utc)
            return {
                "jubilee_ms": jubilee_ms,
                "jubilee_utc": dt.isoformat(),
                "epoch": e + float(fraction),
                "half_life_years": str(HALF_LIFE_YEARS),
            }
        elapsed_years += epoch_years
        boundary += epoch_dur


@app.post("/test/emit")
async def test_emit():
    """Run the next unprocessed emission (integration tests only)."""
    if os.environ.get("ALLOW_TEST_TRIGGERS") != "1":
        return Response(status_code=404)
    ledger = read_ledger()
    processed = {e["epoch"] for e in ledger if e["type"] == "emission"}
    n = 0
    while n in processed:
        n += 1
    entry = await run_emission(n, epoch_boundary(n))
    return entry


# ===========================================================================
# §9. SSE — live audit stream of the pairwise voting process
# ===========================================================================

@app.get("/sse")
async def sse_stream(request: Request):
    """Server-sent events stream. Watch the emission process live."""
    queue = asyncio.Queue()
    SSE_CLIENTS.append(queue)

    async def generate():
        try:
            yield f"event: connected\ndata: {json.dumps({'epoch': current_epoch()[0]})}\n\n"
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
# §10. REDEMPTION UI — GitHub OAuth + evaleval
# ===========================================================================

# TODO: import evaleval Signer, Three, Two, Selector, MORPH, APPEND, etc.
# For now, sketch the flow:

@app.get("/")
async def index(request: Request):
    """Landing page — login or show dashboard"""
    user = request.session.get("github_user")
    if not user:
        return PlainTextResponse(f"""
<html><body>
<h1>slug constitution</h1>
<p>177,600 tokens. $1 each. Promethium half-life. Laskar polynomial epochs.</p>
<a href="/login">Login with GitHub to check your emissions</a>
<br><br>
<a href="/sse">Watch the emission process live</a>
<br>
<a href="/api/epoch">Current epoch</a> |
<a href="/api/ledger">Full ledger</a> |
<a href="/api/halvening">Jubilee countdown</a>
</body></html>
""", media_type="text/html")

    # Logged in — show dashboard with evaleval
    # TODO: evaleval signed snippets for:
    #   - Show contributor rank & score
    #   - Show unclaimed emissions
    #   - Redemption form: paste Solana wallet, submit
    #   - SSE viewer for live emission audit
    return PlainTextResponse(f"Welcome {user}. Dashboard TODO.", media_type="text/html")


@app.get("/login")
async def login():
    """Redirect to GitHub OAuth"""
    return Response(
        status_code=302,
        headers={"Location": f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&scope=read:user"},
    )


@app.get("/callback")
async def callback(request: Request, code: str):
    """GitHub OAuth callback"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        token = resp.json().get("access_token")
        user_resp = await client.get(
            f"{GITHUB_API_BASE_URL}/user",
            headers={"Authorization": f"Bearer {token}"},
        )
        user = user_resp.json()
        request.session["github_user"] = user["login"]
    return Response(status_code=302, headers={"Location": "/"})


@app.post("/redeem")
async def redeem(request: Request):
    """Redeem unclaimed emissions to a Solana wallet"""
    user = request.session.get("github_user")
    if not user:
        return PlainTextResponse("Not logged in", status_code=401)

    form = await request.form()
    wallet = form.get("wallet_address")
    if not wallet:
        return PlainTextResponse("No wallet address", status_code=400)

    # TODO: compute unclaimed emissions for this user
    # TODO: execute Solana transfer
    # TODO: append redemption to JSONL

    entry = {
        "type": "redemption",
        "timestamp_ms": int(time.time() * 1000),
        "github_user": user,
        "wallet_address": wallet,
        "amount": "0",  # TODO
    }
    append_ledger(entry)
    return PlainTextResponse(f"Redeemed to {wallet}. Entry appended to ledger.")


# ===========================================================================
# §11. STARTUP
# ===========================================================================

@app.on_event("startup")
async def startup():
    """Start the epoch timer background task"""
    if os.environ.get("DISABLE_EPOCH_LOOP") != "1":
        asyncio.create_task(epoch_loop())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
