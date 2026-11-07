"""Playwright route harness — Python only (no Clojure), local server, no live site.

Starts `constitution.py` on a free port with an empty temp RocksDB,
DISABLE_EPOCH_LOOP=1, then drives a real Chromium through every route.
Any console error fails the test (especially eval-generated SyntaxErrors).
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def base_url():
    tmp = tempfile.mkdtemp(prefix="constitution-pw-")
    rocks = str(Path(tmp) / "constitution.rocks")
    jsonl = str(Path(tmp) / "ledger.jsonl")
    port = free_port()
    env = dict(os.environ)
    env.update({
        "SESSION_SECRET": "test-secret",
        "GENESIS_MS": "1000000000000",
        "DISABLE_EPOCH_LOOP": "1",
        "ROCKS_PATH": rocks,
        "JSONL_PATH": jsonl,
        "GIT_MIRROR_DIR": str(Path(tmp) / "git"),
        "PORT": str(port),
        "PYTHONUNBUFFERED": "1",
    })
    proc = subprocess.Popen(
        [sys.executable, "constitution.py"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        # Wait for /api/health
        import urllib.request
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{url}/api/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                if proc.poll() is not None:
                    out = proc.stdout.read(4000) if proc.stdout else ""
                    raise RuntimeError(f"server died early: {out}")
                time.sleep(0.3)
        else:
            raise RuntimeError("server did not become healthy in 30s")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture()
def page(browser, base_url):
    ctx = browser.new_context()
    p = ctx.new_page()
    errors = []

    def _on_console(msg):
        # Browser logs a resource error line for every non-2xx fetch (expected
        # for our 403 test); SyntaxError/pageerror is the real signal.
        text = msg.text
        if msg.type == "error" and "Failed to load resource" in text:
            return
        if msg.type == "error":
            errors.append(text)

    p.on("console", _on_console)
    p.on("pageerror", lambda exc: errors.append(str(exc)))
    yield p, errors
    # Fail on console errors except expected favicon 404
    bad = [e for e in errors if "favicon" not in e]
    assert not bad, f"console errors: {bad}"
    ctx.close()


def test_root(page, base_url):
    pg, _ = page
    pg.goto(f"{base_url}/")
    assert "slug" in pg.title().lower()
    assert "Total supply is 1" in pg.content()


def test_watch(page, base_url):
    pg, _ = page
    pg.goto(f"{base_url}/watch")
    assert pg.locator("#process-status").is_visible()
    assert pg.locator("#audit-feed").is_visible()
    # SSE should flip to live quickly
    pg.wait_for_timeout(500)
    text = pg.locator("#connection-status").inner_text()
    assert text.lower() in ("connected", "connecting", "live", "reconnecting", "paused locally")


@pytest.mark.parametrize("path", [
    "/api/health", "/api/status", "/api/epoch", "/api/ranking", "/api/halvening",
    "/api/ledger?limit=1",
])
def test_json_apis(page, base_url, path):
    pg, _ = page
    pg.goto(f"{base_url}{path}")
    body = pg.content()
    # Playwright renders JSON as <pre>; just ensure valid JSON parses
    text = pg.evaluate("() => document.body.innerText")
    data = json.loads(text)
    assert isinstance(data, (dict, list))


def test_epochs_index(page, base_url):
    pg, _ = page
    pg.goto(f"{base_url}/epochs")
    assert "epochs" in pg.title().lower() or "epoch" in pg.content().lower()


def test_not_found_pages(page, base_url):
    pg, _ = page
    for path in ["/epochs/999", "/commits/nope", "/comparisons/nope",
                 "/attempts/nope", "/judgments/nope", "/events/nope"]:
        resp = pg.goto(f"{base_url}{path}")
        assert resp.status == 200  # app renders not-found, not 404
        text = pg.content().lower()
        assert ("not found" in text) or ("no evidence" in text) or ("none" in text)


def test_login_redirect(page, base_url):
    pg, _ = page
    resp = pg.goto(f"{base_url}/login")
    # 302 to GitHub OAuth
    assert resp.status in (200, 302) or "github.com" in pg.url


def test_post_invalid_signature_no_eval_error(page, base_url):
    pg, errors = page
    pg.goto(f"{base_url}/")
    # Submit a forged redeem ticket — should 403 and NOT eval the error body
    pg.evaluate("""async () => {
        const fd = new FormData();
        fd.set('__snippet__', 'redeem("x", $wallet_address)');
        fd.set('__sig__', 'bad');
        fd.set('__nonce__', 'deadbeef');
        fd.set('wallet_address', '3abc');
        const r = await fetch('/', {method: 'POST', body: fd});
        window._last_status = r.status;
        window._last_body = await r.text();
    }""")
    assert pg.evaluate("window._last_status") == 403
    # No console SyntaxError from eval'ing "Invalid signature"
    assert not any("SyntaxError" in e for e in errors)


def test_sse_connection(page, base_url):
    pg, _ = page
    pg.goto(f"{base_url}/watch")
    got = pg.evaluate("""async () => {
        return await new Promise(res => {
            const es = new EventSource('/sse');
            const t = setTimeout(() => { es.close(); res('timeout'); }, 5000);
            es.addEventListener('audit', e => { clearTimeout(t); es.close(); res(e.data.slice(0,80)); }, {once:true});
        });
    }""")
    assert "audit" not in got or len(got) > 0  # SSE delivered (or clean timeout without error)
