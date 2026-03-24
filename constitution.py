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
from evaleval import event, JsonlStore, to_dict, render, RawContent, Signer, SnippetExecutionError

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
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
        )
        models = resp.json()["data"]
        chat_models = [m for m in models if "chat" in m.get("id", "")]
        chat_models.sort(key=lambda m: m.get("created", 0), reverse=True)
        return [m["id"] for m in chat_models[:n]]


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
            "https://openrouter.ai/api/v1/chat/completions",
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
            f"https://api.github.com/repos/{REPO}/commits",
            params={"since": since_iso, "per_page": 100},
            headers=_github_headers(),
        )
        return resp.json()


async def fetch_commit_unified_diff(client: httpx.AsyncClient, sha: str) -> str:
    resp = await client.get(
        f"https://api.github.com/repos/{REPO}/commits/{sha}",
        headers=_github_headers(),
    )
    resp.raise_for_status()
    return unified_diff_from_commit_payload(resp.json())


SSE_CLIENTS = []


async def broadcast_sse(event_type, data):
    for queue in SSE_CLIENTS:
        await queue.put(f"event: {event_type}\ndata: {json.dumps(data)}\n\n")


async def rank_commits(since_ms):
    commits = await fetch_commits_since(since_ms)
    if not commits:
        return {}

    async with httpx.AsyncClient() as gh:
        commit_diffs = await asyncio.gather(*[fetch_commit_unified_diff(gh, c["sha"]) for c in commits])

    models = await fetch_top_models(n=3)
    await broadcast_sse("council", {"models": models, "commits": len(commits)})

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

    pairs = []
    for i, a1 in enumerate(authors):
        for j, a2 in enumerate(authors):
            if i >= j:
                continue
            for model in models:
                await broadcast_sse("comparing", {"model": model, "a": a1, "b": a2})
                try:
                    result = await llm_pairwise_compare(model, author_side_for_llm(a1), author_side_for_llm(a2))
                    w, l = (a1, a2) if result["winner"] == "A" else (a2, a1)
                    ratio = result["ratio"].split(":")
                    pairs.append((author_idx[w], author_idx[l], float(ratio[0]), float(ratio[1])))
                    await broadcast_sse("vote", {"model": model, "winner": w, "loser": l,
                                                 "ratio": result["ratio"], "explanation": result["explanation"]})
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

def pool_remaining(events: list) -> Decimal:
    emitted = sum(Decimal(e.total_emitted) for e in events if isinstance(e, Emission))
    return CONTRIBUTOR_POOL - emitted


async def run_emission(epoch_n, boundary_ms):
    await broadcast_sse("emission_start", {"epoch": epoch_n, "boundary_ms": boundary_ms})

    pool = pool_remaining(store.read())
    emission = pool * DECAY_RATE
    await broadcast_sse("emission_amount", {
        "pool_before": str(pool), "emission": str(emission),
        "pool_after": str(pool - emission), "decay_rate": str(DECAY_RATE),
    })

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
        await broadcast_sse("emission_complete", to_dict(entry))
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
            await broadcast_sse("waiting", {
                "next_epoch": epoch_n + 1,
                "boundary_ms": next_boundary,
                "wait_seconds": wait_ms / 1000,
            })
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
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}"},
        )
        request.session["github_user"] = user_resp.json()["login"]
    return Response(status_code=302, headers={"Location": "/"})


# ===========================================================================
# §12. STARTUP
# ===========================================================================

@app.on_event("startup")
async def startup():
    asyncio.create_task(epoch_loop())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
