"""Web views — HTML rendering and SSE route for the constitution.

All HTML/CSS/JS rendering lives here; the economic mechanism stays in
constitution.py. ``constitution.py`` at import time calls
``views.register(app, core)`` where ``core`` is the already-loaded
constitution module. ``register`` defines the HTML helpers and route
handlers closing over ``core`` and uses ``core.<name>`` for every
constitution-dependent lookup (``core._db()``, ``core.ROOT``, etc.) at
call time, so test monkeypatching of ``constitution`` attributes is
honored. This module never imports ``constitution`` at top level, so
there is no circular import.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

from fastapi import Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.requests import Request

from evaleval import MORPH, RawContent, Selector, exec_event, render
from evaleval.depth import Three

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
body.watch-doc { max-width: 1180px; }
.watch-grid {
  align-items: start;
  display: grid;
  gap: 12px;
  grid-template-columns: minmax(0, 1fr) minmax(360px, .8fr);
}
.watch-main, .ranking-column { min-width: 0; }
.model-list {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 8px;
}
.model-list code { color: var(--link); }
.commit-set {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-top: 8px;
}
.commit-row {
  background: var(--g1);
  border: var(--bv) solid;
  border-color: var(--lo) var(--hi) var(--hi) var(--lo);
  display: grid;
  gap: 3px 8px;
  grid-template-columns: 32px minmax(0, 1fr);
  padding: 6px 8px;
}
.commit-row .rank {
  color: var(--meta);
  font-family: var(--font-code);
  grid-row: 1 / span 3;
}
.commit-row .commit-main { min-width: 0; }
.commit-row .commit-message {
  color: var(--prose);
  font-family: var(--font-prose);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.commit-row .comparison-links {
  font-family: var(--font-code);
  font-size: 10px;
}
.commit-row .comparison-links a { margin-right: 8px; }
.set-label {
  color: var(--ui);
  font-family: var(--font-code);
  font-size: 11px;
  margin: 12px 0 4px;
  text-transform: uppercase;
}
.set-label.unvoted { color: var(--meta); }
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
pre.blob.diff-preview {
  box-shadow: inset 1px 1px 6px #000;
  max-height: 22em;
  min-height: 8em;
}
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
  .watch-grid { grid-template-columns: 1fr; }
}
@media (max-width: 520px) {
  .event { grid-template-columns: 72px 1fr; }
  .event-message { grid-column: 1 / -1; }
  .kv { grid-template-columns: 1fr; }
}
"""


_WATCH_JS = """
const initial = __INITIAL__;
const feed = document.querySelector('#audit-feed');
const processStatus = document.querySelector('#process-status');
const connection = document.querySelector('#connection-status');
const fill = document.querySelector('#progress-fill');
const progressLabel = document.querySelector('#progress-label');
const play = document.querySelector('#play');
const pause = document.querySelector('#pause');
const councilModels = document.querySelector('#council-models');
const commitSet = document.querySelector('#commit-set');
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

function renderRanking(state) {
  councilModels.replaceChildren();
  for (const model of state.models || []) {
    const code = document.createElement('code');
    code.textContent = model;
    councilModels.appendChild(code);
  }
  if (!(state.models || []).length) {
    const empty = document.createElement('span');
    empty.className = 'note';
    empty.textContent = 'Council not selected yet.';
    councilModels.appendChild(empty);
  }

  commitSet.replaceChildren();
  const rows = state.rows || [];
  const groups = [
    ['voted set', rows.filter(row => row.voted), 'voted'],
    ['unvoted set', rows.filter(row => !row.voted), 'unvoted'],
  ];
  for (const [label, group, className] of groups) {
    const heading = document.createElement('div');
    heading.className = `set-label ${className}`;
    heading.textContent = `${label} · ${group.length}`;
    commitSet.appendChild(heading);
    for (const item of group) {
      const row = document.createElement('div');
      row.className = 'commit-row';
      const rank = document.createElement('span');
      rank.className = 'rank';
      rank.textContent = `#${item.position}`;
      const main = document.createElement('div');
      main.className = 'commit-main';
      const commit = document.createElement('a');
      commit.href = `/commits/${item.commit_id}`;
      commit.textContent = item.commit_id.slice(0, 16);
      main.append(commit, document.createTextNode(` · ${item.contributor}`));
      const message = document.createElement('div');
      message.className = 'commit-message';
      message.textContent = item.message || '(no message)';
      const comparisons = document.createElement('div');
      comparisons.className = 'comparison-links';
      for (const [index, comparisonId] of (item.comparison_ids || []).entries()) {
        const link = document.createElement('a');
        link.href = `/comparisons/${comparisonId}`;
        link.textContent = `comparison ${index + 1}`;
        comparisons.appendChild(link);
      }
      if (!(item.comparison_ids || []).length) {
        comparisons.textContent = 'no comparisons yet';
      }
      row.append(rank, main, message, comparisons);
      commitSet.appendChild(row);
    }
  }
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
  source.addEventListener('ranking_state', event => renderRanking(JSON.parse(event.data)));
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
renderRanking(initial.ranking || {models: [], rows: []});
processStatus.textContent = initial.process.message;
setProgress(initial.process.progress);
connect();
"""


_FORM_INTERCEPT_JS = """
document.addEventListener('submit', async e => {
    e.preventDefault();
    const r = await fetch('/', {method: 'POST', body: new FormData(e.target)});
    const js = await r.text();
    if (r.ok && js.trim()) eval(js);
});
"""


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



def register(app, core):
    """Register HTML/SSE routes on ``app``. ``core`` is constitution module."""

    def _judgments_by_comparison(
        judgments: list | None = None,
    ) -> dict:
        if judgments is None:
            out: dict = {}
            for comparison_id in core.ROOT.judgment_ids_by_comparison.keys(core._db()):
                out[str(comparison_id)] = _judgments_for_comparison(
                    str(comparison_id)
                )
            return out
        rows = judgments
        out: dict = {}
        for e in rows:
            cid = e.payload.get("comparison_id")
            if cid:
                out.setdefault(str(cid), []).append(e)
        return out

    def _judgments_for_comparison(comparison_id: str) -> list:
        path = core.ROOT.judgment_ids_by_comparison.key(comparison_id)
        ids = path.iter(core._db()) if path.len(core._db()) else []
        return core._evidence_from_ids(ids)

    def _judgment_blocks(judgments: list) -> list:
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
                    _a(core._evidence_path("judgment", jid), "permalink"),
                ],
                ["p.explanation", p.get("explanation") or "(no explanation)"],
            ])
        return blocks

    def _watch_initial_state() -> dict:
        db = core._db()
        total = core.ROOT.events.len(db)
        events = [
            core._typed(core.ROOT.events.get(db, index))
            for index in range(max(0, total - 40), total)
        ]
        feed = []
        for event_ in events:
            if isinstance(event_, core.GitDiscovery):
                feed.append({
                    "id": f"discovery-{event_.snapshot_id}",
                    "timestamp_ms": event_.timestamp_ms,
                    "kind": "discovery",
                    "message": (
                        f"Epoch {event_.epoch}: observed {len(event_.observations)} commits; "
                        f"{len(event_.commits)} eligible"
                    ),
                    "evidence_url": core._evidence_path("epoch", str(event_.epoch)),
                    "links": {"epoch": core._evidence_path("epoch", str(event_.epoch))},
                })
            elif isinstance(event_, core.Emission):
                feed.append({
                    "id": f"emission-{event_.epoch}",
                    "timestamp_ms": event_.timestamp_ms,
                    "kind": "complete",
                    "message": (
                        f"Epoch {event_.epoch}: emitted {event_.total_emitted} SLG; "
                        f"ranking {event_.ranking}"
                    ),
                    "evidence_url": core._evidence_path("epoch", str(event_.epoch)),
                    "links": {"epoch": core._evidence_path("epoch", str(event_.epoch))},
                })
            elif isinstance(event_, core.Evidence):
                feed.append({
                    "id": event_.event_id,
                    "timestamp_ms": event_.recorded_at_ms,
                    "kind": event_.kind,
                    "message": event_.payload.get("summary") or event_.kind,
                    "evidence_url": core._evidence_path("event", event_.event_id),
                    "links": {"epoch": core._evidence_path("epoch", str(event_.epoch))},
                })
        feed.extend(core.AUDIT_HISTORY)
        ranking = {
            **core.RANKING_VIEW_STATE,
            "models": list(core.RANKING_VIEW_STATE["models"]),
            "rows": [
                {**row, "comparison_ids": list(row["comparison_ids"])}
                for row in core.RANKING_VIEW_STATE["rows"]
            ],
        }
        if not ranking["models"]:
            latest = core._typed(core.ROOT.latest_emission.get(db))
            if isinstance(latest, core.Emission):
                ranking["models"] = list(latest.models_used or [])
        return {
            "process": dict(core.PROCESS_STATE),
            "openrouter_configured": bool((core.OPENROUTER_API_KEY or "").strip()),
            "epoch": core.current_epoch()[0],
            "feed": feed[-200:],
            "ranking": ranking,
        }


    @app.get("/sse")
    async def sse_stream(request: Request):
        queue = asyncio.Queue()
        core.SSE_CLIENTS.append(queue)

        async def generate():
            try:
                yield core._sse_event("audit", {
                    "id": core.AUDIT_SEQUENCE,
                    "timestamp_ms": int(time.time() * 1000),
                    "kind": "connection",
                    "message": f"Connected to epoch {core.current_epoch()[0]}",
                    **core.PROCESS_STATE,
                })
                yield exec_event(Three[Selector("#emission-status")][MORPH][
                    ["div#emission-status", f"Connected — epoch {core.current_epoch()[0]}"]
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
                if queue in core.SSE_CLIENTS:
                    core.SSE_CLIENTS.remove(queue)

        from starlette.responses import StreamingResponse
        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/epochs")
    async def epochs_index():
        epochs = core._epochs_in_ledger()
        rows = []
        for epoch in epochs:
            emission = core._emission_for_epoch(epoch)
            evidence_n = int(
                core.ROOT.evidence_count_by_epoch.key(epoch).get(core._db())
            )
            discovery_event_id = core.ROOT.discovery_evidence_by_epoch.key(epoch).get(core._db())
            disc = core.find_evidence(discovery_event_id) if discovery_event_id else None
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
                _a(core._evidence_path("epoch", str(epoch)), f"epoch {epoch}"),
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
        emission = core._emission_for_epoch(epoch)
        evidence_rows = core._evidence_for_epoch(epoch)
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
                    _a(core._evidence_path("judgment", p.get("judgment_id") or j.event_id), "↗"),
                ])
            card = ["article.dense-card",
                ["div.card-head",
                    _a(core._evidence_path("comparison", cid), "comparison"),
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
                    msg = core._blob_text(ev.payload.get("message")).split("\n", 1)[0][:80]
                rank_rows.append(["tr",
                    ["td", str(i)],
                    ["td", ["code", f"{float(score):.4f}"]],
                    ["td", _a(core._evidence_path("commit", cid), _short_id(cid, 16))],
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
            (f"{e.kind} · {e.event_id[:28]}", core._evidence_path("event", e.event_id))
            for e in evidence_rows
        ]
        judgment_links = [
            (
                e.payload.get("summary") or e.payload.get("judgment_id", e.event_id),
                core._evidence_path("judgment", e.payload["judgment_id"]),
            )
            for e in judgment_evs
            if e.payload.get("judgment_id")
        ]
        commit_links = [
            (
                f"{e.payload.get('oid', e.payload.get('commit_id', ''))[:24]} "
                f"({e.payload.get('contributor', '?')})",
                core._evidence_path("commit", e.payload["commit_id"]),
            )
            for e in commit_evs
            if e.payload.get("commit_id")
        ]
        comparison_links = [
            (
                e.payload.get("summary") or e.payload.get("comparison_id", e.event_id),
                core._evidence_path("comparison", e.payload["comparison_id"]),
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
        ev = core.find_evidence_payload("git.commit", "commit_id", commit_id)
        if not ev:
            return _evidence_page("commit not found", [
                _evidence_nav(),
                ["h1", "commit not found"],
                ["p", commit_id],
            ])
        p = ev.payload
        epoch = ev.epoch
        related_cmp = []
        comparison_path = core.ROOT.comparison_ids_by_commit.key(commit_id)
        comparison_ids = (
            comparison_path.iter(core._db()) if comparison_path.len(core._db()) else []
        )
        for comparison_id in comparison_ids:
            cmp = core.find_evidence_payload(
                "comparison.input", "comparison_id", comparison_id
            )
            if not cmp:
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
            cid = comparison_id
            if not cid:
                continue
            related_cmp.append(["article.dense-card",
                ["div.card-head",
                    _a(core._evidence_path("comparison", cid), "comparison"),
                    " · ",
                    _side_label(sa),
                    " vs ",
                    _side_label(sb),
                ],
                *_judgment_blocks(_judgments_for_comparison(cid)),
            ])
        return _evidence_page(f"commit {commit_id[:24]}", [
            _evidence_nav(_a(core._evidence_path("epoch", str(epoch)), f"epoch {epoch}")),
            ["div.eyebrow", "commit"],
            ["h1", _short_id(commit_id, 20)],
            ["p.lede", p.get("contributor") or "?", " · ", ["code", p.get("oid", "")]],
            ["p", _a(f"/commits/{commit_id}/patch", "download patch"), " · ",
                  _a(core._evidence_path("event", ev.event_id), "raw event")],
            ["h2", "message"],
            _pre_blob(core._blob_text(p.get("message")), cls="blob compact"),
            ["h2", "comparisons involving this commit"],
            *(related_cmp if related_cmp else [["p.note", "None yet."]]),
            ["p.note", "The full patch is loaded only by the download route: ",
             _a(f"/commits/{commit_id}/patch", "download patch")],
            _fold("Metadata", _dl_rows([
                ("commit_id", commit_id),
                ("patch_sha256", p.get("patch_sha256")),
                ("patch_identity", p.get("patch_identity")),
                ("committer_timestamp_ms", p.get("committer_timestamp_ms")),
            ])),
        ])

    @app.get("/commits/{commit_id}/patch")
    async def commit_patch_download(commit_id: str):
        ev = core.find_evidence_payload("git.commit", "commit_id", commit_id)
        if not ev:
            return PlainTextResponse("not found", status_code=404)
        raw = core._decode_blob(ev.payload.get("patch")) or core._blob_text(ev.payload.get("patch")).encode()
        return Response(
            content=raw,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{commit_id}.patch"'
            },
        )

    @app.get("/comparisons/{comparison_id}")
    async def comparison_detail(comparison_id: str):
        ev = core.find_evidence_payload("comparison.input", "comparison_id", comparison_id)
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
        attempt_path = core.ROOT.attempt_ids_by_comparison.key(comparison_id)
        attempt_event_ids = (
            attempt_path.iter(core._db()) if attempt_path.len(core._db()) else []
        )
        for a in core._evidence_from_ids(attempt_event_ids):
            if a.kind != "llm.attempt_started":
                continue
            aid = a.payload.get("attempt_id")
            if aid:
                attempt_links.append((
                    f"{a.payload.get('model_id')} #{a.payload.get('attempt_number')}",
                    core._evidence_path("attempt", aid),
                ))
        judgment_links = [
            (
                j.payload.get("summary") or j.payload.get("judgment_id", j.event_id),
                core._evidence_path("judgment", j.payload["judgment_id"]),
            )
            for j in judgments
            if j.payload.get("judgment_id")
        ]
        cid_a = side_a.get("commit_id") or (side_a.get("commit_ids") or [None])[0]
        cid_b = side_b.get("commit_id") or (side_b.get("commit_ids") or [None])[0]
        return _evidence_page(f"comparison {comparison_id[:24]}", [
            _evidence_nav(_a(core._evidence_path("epoch", str(ev.epoch)), f"epoch {ev.epoch}")),
            ["div.eyebrow", "comparison"],
            ["h1", f"{_side_label(side_a)} vs {_side_label(side_b)}"],
            ["p.lede",
                _a(f"/comparisons/{comparison_id}/prompt", "download prompt"),
                " · ",
                _a(core._evidence_path("event", ev.event_id), "raw event"),
                " · ",
                ["code", _short_id(comparison_id, 18)],
            ],
            ["h2", "council reasoning"],
            *_judgment_blocks(judgments),
            ["h2", "sides"],
            ["div.sides",
                ["section.side",
                    ["h3", "A — ",
                        (_a(core._evidence_path("commit", cid_a), _side_label(side_a))
                         if cid_a else _side_label(side_a))],
                    ["h4", "message"],
                    _pre_blob(core._blob_text(side_a.get("message")), cls="blob compact"),
                    ["h4", "diff preview"],
                    ["pre.blob.diff-preview", core._blob_preview(side_a.get("diff"))],
                    ["p", _a(
                        f"/comparisons/{comparison_id}/diff/a",
                        "download full diff A",
                    )],
                ],
                ["section.side",
                    ["h3", "B — ",
                        (_a(core._evidence_path("commit", cid_b), _side_label(side_b))
                         if cid_b else _side_label(side_b))],
                    ["h4", "message"],
                    _pre_blob(core._blob_text(side_b.get("message")), cls="blob compact"),
                    ["h4", "diff preview"],
                    ["pre.blob.diff-preview", core._blob_preview(side_b.get("diff"))],
                    ["p", _a(
                        f"/comparisons/{comparison_id}/diff/b",
                        "download full diff B",
                    )],
                ],
            ],
            _fold("Hardlinks — judgments / attempts / prompt",
                  ["p", _a(f"/comparisons/{comparison_id}/prompt", "prompt download")],
                  ["h3", "judgments"],
                  _link_list(judgment_links) if judgment_links else ["p.note", "(none)"],
                  ["h3", "attempts"],
                  _link_list(attempt_links) if attempt_links else ["p.note", "(none)"],
                  ["p.note", "Prompt text is loaded only by the download route."],
            ),
        ])

    @app.get("/comparisons/{comparison_id}/prompt")
    async def comparison_prompt_download(comparison_id: str):
        ev = core.find_evidence_payload("comparison.input", "comparison_id", comparison_id)
        if not ev:
            return PlainTextResponse("not found", status_code=404)
        raw = core._decode_blob(ev.payload.get("prompt")) or core._blob_text(ev.payload.get("prompt")).encode()
        return Response(
            content=raw,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{comparison_id}.prompt.txt"'
            },
        )

    @app.get("/comparisons/{comparison_id}/diff/{side}")
    async def comparison_diff_download(comparison_id: str, side: str):
        ev = core.find_evidence_payload("comparison.input", "comparison_id", comparison_id)
        if not ev or side not in {"a", "b"}:
            return PlainTextResponse("not found", status_code=404)
        value = (ev.payload.get(f"side_{side}") or {}).get("diff")
        raw = core._decode_blob(value) if isinstance(value, dict) else str(value or "").encode()
        return Response(
            content=raw,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{comparison_id}-{side}.diff"'
                )
            },
        )

    @app.get("/attempts/{attempt_id}")
    async def attempt_detail(attempt_id: str):
        started = core.find_evidence_payload("llm.attempt_started", "attempt_id", attempt_id)
        finished = core.find_evidence_payload("llm.attempt_finished", "attempt_id", attempt_id)
        if not started and not finished:
            return _evidence_page("attempt not found", [
                _evidence_nav(),
                ["h1", "attempt not found"],
                ["p", attempt_id],
            ])
        base = started or finished
        assert base is not None
        p = {**(started.payload if started else {}), **(finished.payload if finished else {})}
        judgment_event_id = core.ROOT.judgment_id_by_attempt.key(attempt_id).get(core._db())
        judgment = core.find_evidence(judgment_event_id) if judgment_event_id else None
        comparison_id = p.get("comparison_id")
        return _evidence_page(f"attempt {attempt_id[:24]}", [
            _evidence_nav(
                _a(core._evidence_path("epoch", str(base.epoch)), f"epoch {base.epoch}"),
                *(
                    [_a(core._evidence_path("comparison", comparison_id), "comparison")]
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
                 _a(core._evidence_path("event", started.event_id), started.event_id)
                 if started else None),
                ("finished_event",
                 _a(core._evidence_path("event", finished.event_id), finished.event_id)
                 if finished else None),
                ("judgment",
                 _a(
                     core._evidence_path("judgment", judgment.payload["judgment_id"]),
                     judgment.payload["judgment_id"],
                 ) if judgment and judgment.payload.get("judgment_id") else None),
            ]),
            ["h2", "request / response"],
            ["p",
             _a(f"/attempts/{attempt_id}/request", "download request"),
             " · ",
             _a(f"/attempts/{attempt_id}/response", "download response")],
        ])

    @app.get("/attempts/{attempt_id}/{part}")
    async def attempt_blob_download(attempt_id: str, part: str):
        if part not in {"request", "response"}:
            return PlainTextResponse("not found", status_code=404)
        started = core.find_evidence_payload("llm.attempt_started", "attempt_id", attempt_id)
        finished = core.find_evidence_payload("llm.attempt_finished", "attempt_id", attempt_id)
        if not started and not finished:
            return PlainTextResponse("not found", status_code=404)
        payload = {
            **(started.payload if started else {}),
            **(finished.payload if finished else {}),
        }
        value = payload.get(part)
        raw = core._decode_blob(value) if isinstance(value, dict) else str(value or "").encode()
        return Response(
            content=raw,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{attempt_id}-{part}.txt"'
                )
            },
        )

    @app.get("/judgments/{judgment_id}")
    async def judgment_detail(judgment_id: str):
        ev = core.find_evidence_payload("llm.judgment", "judgment_id", judgment_id)
        if not ev:
            return _evidence_page("judgment not found", [
                _evidence_nav(),
                ["h1", "judgment not found"],
                ["p", judgment_id],
            ])
        p = ev.payload
        return _evidence_page(f"judgment {judgment_id[:24]}", [
            _evidence_nav(
                _a(core._evidence_path("epoch", str(ev.epoch)), f"epoch {ev.epoch}"),
                *(
                    [_a(core._evidence_path("comparison", p["comparison_id"]), "comparison")]
                    if p.get("comparison_id") else []
                ),
                *(
                    [_a(core._evidence_path("attempt", p["attempt_id"]), "attempt")]
                    if p.get("attempt_id") else []
                ),
            ),
            ["div.eyebrow", "judgment"],
            ["h1", f"{p.get('model_id') or '?'} → {p.get('winner')} ({p.get('ratio')})"],
            ["p.lede", ["code", _short_id(judgment_id, 18)], " · ",
                  _a(core._evidence_path("event", ev.event_id), "raw event")],
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
        ev = core.find_evidence(event_id)
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
                related.append((f"{key}: {p[key]}", core._evidence_path(kind, p[key])))
        related.append((f"epoch {ev.epoch}", core._evidence_path("epoch", str(ev.epoch))))
        display_payload = {}
        for key, value in p.items():
            if isinstance(value, dict) and value.get("encoding") == "base64":
                display_payload[key] = {
                    "sha256": value.get("sha256"),
                    "byte_length": value.get("byte_length"),
                    "text_preview": (core._blob_text(value)[:2000]
                                     + ("…" if value.get("byte_length", 0) > 2000 else "")),
                }
            else:
                display_payload[key] = value
        return _evidence_page(f"event {event_id[:24]}", [
            _evidence_nav(_a(core._evidence_path("epoch", str(ev.epoch)), f"epoch {ev.epoch}")),
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

    @app.get("/watch")
    async def watch():
        initial = json.dumps(
            _watch_initial_state(), separators=(",", ":")
        ).replace("</", "<\\/")
        script = _WATCH_JS.replace("__INITIAL__", initial)
        key_ok = bool((core.OPENROUTER_API_KEY or "").strip())
        return HTMLResponse(render(["html",
            ["head",
                ["meta", {"charset": "utf-8"}],
                ["meta", {"name": "viewport", "content": "width=device-width, initial-scale=1"}],
                ["title", "slug — live constitution"],
                ["style", RawContent(_WATCH_CSS)],
            ],
            ["body.watch-doc", ["main",
                ["div.eyebrow", f"epoch {core.current_epoch()[0]} · constitutional audit"],
                ["h1", "watch the process"],
                ["div.watch-grid",
                    ["div.watch-main",
                        ["section.panel",
                            ["div.status-row",
                                ["div#process-status", core.PROCESS_STATE["message"]],
                                ["div#connection-status.badge", ["span.dot"], ["span", "connecting"]],
                            ],
                            ["div.progress-shell", {
                                "role": "progressbar", "aria-label": "core.Emission progress",
                                "aria-valuemin": "0", "aria-valuemax": "100",
                                "aria-valuenow": str(core.PROCESS_STATE["progress"]),
                            },
                                ["div#progress-fill"],
                                ["div#progress-label", f"{core.PROCESS_STATE['progress']}%"],
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
                                    "Epochs with two or more eligible commits require core.OPENROUTER_API_KEY."
                                )
                            ],
                            ["div#council-models.model-list"],
                        ],
                        ["section.panel",
                            ["div.feed-head", ["h2", "event feed"], ["a", {"href": "/sse"}, "raw SSE"]],
                            ["div#audit-feed"],
                        ],
                    ],
                    ["aside.ranking-column",
                        ["section.panel",
                            ["div.feed-head",
                                ["h2", "current commit set"],
                                ["span.note", "live Rank Centrality order"],
                            ],
                            ["p.note",
                                "Voted is the currently confirmed top-down zip prefix; "
                                "unvoted remains below the first uncovered adjacent pair."],
                            ["div#commit-set.commit-set"],
                        ],
                    ],
                ],
                ["p", ["a", {"href": "/"}, "← constitution"], " · ",
                      ["a", {"href": "/epochs"}, "epochs"], " · ",
                      ["a", {"href": "/api/status"}, "status JSON"], " · ",
                      ["a", {"href": "/api/ledger"}, "ledger"]],
            ]],
            ["script", {"type": "module"}, RawContent(script)],
        ]))

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

        emissions = [
            core._typed(row) for _, row in core.ROOT.emissions_by_epoch.iter(core._db())
        ]
        history = [
            e for e in emissions
            if isinstance(e, core.Emission) and user in e.distributions
        ]
        total_earned = sum(Decimal(e.distributions[user]) for e in history)
        latest = core._typed(core.ROOT.latest_emission.get(core._db()))
        latest_rank = (
            latest.ranking.get(user) if isinstance(latest, core.Emission) else None
        )

        redeem_form = ["form", {"method": "post"},
            *core.signer.snippet_hidden(f"redeem({json.dumps(user)}, $wallet_address)"),
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


    return {
        "sse_stream": sse_stream,
        "epochs_index": epochs_index,
        "epoch_detail": epoch_detail,
        "commit_detail": commit_detail,
        "commit_patch_download": commit_patch_download,
        "comparison_detail": comparison_detail,
        "comparison_prompt_download": comparison_prompt_download,
        "comparison_diff_download": comparison_diff_download,
        "attempt_detail": attempt_detail,
        "attempt_blob_download": attempt_blob_download,
        "judgment_detail": judgment_detail,
        "event_detail": event_detail,
        "watch": watch,
        "index": index,
        "_judgments_by_comparison": _judgments_by_comparison,
        "_judgments_for_comparison": _judgments_for_comparison,
        "_judgment_blocks": _judgment_blocks,
        "_watch_initial_state": _watch_initial_state,
    }
