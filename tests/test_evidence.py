"""HTML evidence graph: byte fidelity, resume, legacy projection, private ack."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

import constitution as c


@pytest.fixture
def evidence_store(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "store", c.JsonlStore(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(c, "PUBLIC_BASE_URL", "http://test.local")
    return tmp_path


def test_bytes_blob_roundtrip_crlf_nul_invalid_utf8():
    raw = b"line\r\n\x00<script>alert(1)</script>\xff"
    blob = c._bytes_blob(raw)
    assert blob["encoding"] == "base64"
    assert blob["byte_length"] == len(raw)
    assert blob["sha256"] == hashlib.sha256(raw).hexdigest()
    assert c._decode_blob(blob) == raw
    assert "<script>" in blob["text"]  # display text may replace invalid bytes


def test_append_evidence_is_idempotent_and_hash_chained(evidence_store):
    first = asyncio.run(c.append_evidence(1, "epoch.started", {
        "summary": "start", "boundary_ms": 1,
    }))
    again = asyncio.run(c.append_evidence(1, "epoch.started", {
        "summary": "start", "boundary_ms": 1,
    }))
    assert first.event_id == again.event_id
    second = asyncio.run(c.append_evidence(1, "git.discovery_completed", {
        "summary": "done", "snapshot_id": "abc",
    }))
    assert second.previous_event_sha256 == first.event_id.split("_", 1)[-1]
    events = [e for e in c.store.read() if isinstance(e, c.Evidence)]
    assert len(events) == 2


def test_public_repo_row_strips_credentials():
    row = c._public_repo_row({
        "id": "x",
        "url": "https://x-access-token:secret@github.com/org/repo.git",
        "refs": ["refs/heads/main"],
    })
    assert "secret" not in row["url"]
    assert "github.com/org/repo.git" in row["url"]


def test_html_evidence_pages_escape_and_link(evidence_store):
    patch = b"@@\n+<script>evil()</script>\n"
    commit_id = c.commit_id_for_oid("sha1:" + "a" * 40)
    asyncio.run(c.append_evidence(3, "git.commit", {
        "commit_id": commit_id,
        "oid": "sha1:" + "a" * 40,
        "contributor": "alice",
        "message": c._bytes_blob("msg <b>x</b>"),
        "patch": c._bytes_blob(patch),
        "summary": "commit",
    }))
    cmp_id = c.comparison_id_for({"x": 1})
    asyncio.run(c.append_evidence(3, "comparison.input", {
        "comparison_id": cmp_id,
        "summary": "cmp",
        "prompt": c._bytes_blob("prompt <img>"),
        "side_a": {"contributor": "alice", "commit_ids": [commit_id]},
        "side_b": {"contributor": "bob", "commit_ids": []},
    }))
    commit_html = asyncio.run(c.commit_detail(commit_id)).body.decode()
    assert "<script>evil()" not in commit_html
    assert "&lt;script&gt;evil()" in commit_html
    assert f"/commits/{commit_id}/patch" in commit_html
    epoch_html = asyncio.run(c.epoch_detail(3)).body.decode()
    assert f"/commits/{commit_id}" in epoch_html
    assert f"/comparisons/{cmp_id}" in epoch_html
    patch_resp = asyncio.run(c.commit_patch_download(commit_id))
    assert patch_resp.body == patch
    assert patch_resp.headers["content-disposition"].startswith("attachment")


def test_legacy_epoch_projects_from_gitdiscovery_without_evidence(evidence_store):
    oid = "sha1:" + "b" * 40
    discovery = c.GitDiscovery(
        schema_version=1,
        epoch=3,
        snapshot_id="legacy-snap",
        timestamp_ms=1,
        config_digest="d",
        initial_snapshot=True,
        configuration={"repositories": [], "contributors": {}},
        repositories=[],
        observations=[{
            "oid": oid,
            "eligible": False,
            "exclusion_reason": "before_genesis",
            "first_sources": [],
        }, {
            "oid": "sha1:" + "c" * 40,
            "eligible": True,
            "exclusion_reason": None,
            "first_sources": [],
        }],
        commits=[{
            "oid": "sha1:" + "c" * 40,
            "contributor": "tommy-mor",
            "message": "eligible",
            "patch": "diff --git a/x b/x\n",
        }],
    )
    emission = c.Emission(
        epoch=3,
        timestamp_ms=2,
        pool_before="1",
        total_emitted="0.1",
        pool_after="0.9",
        decay_rate="0.1",
        distributions={"tommy-mor": "0.1"},
        ranking={"tommy-mor": "1"},
        models_used=[],
        discovery_snapshot_id="legacy-snap",
    )
    asyncio.run(c.store.append(discovery))
    asyncio.run(c.store.append(emission))
    html = asyncio.run(c.epoch_detail(3)).body.decode()
    assert "legacy" in html.lower()
    assert "Single-contributor" in html or "no LLM" in html
    excluded_id = c.commit_id_for_oid(oid)
    excluded_html = asyncio.run(c.commit_detail(excluded_id)).body.decode()
    assert "legacy evidence unavailable" in excluded_html.lower()


def test_ranking_resume_skips_duplicate_provider_calls(evidence_store, monkeypatch):
    calls = {"n": 0}

    async def models(n=3):
        return ["mock/a", "mock/b", "mock/c"]

    async def compare(model_id, side_a, side_b, **kwargs):
        calls["n"] += 1
        # Persist judgment the real path would write.
        if kwargs.get("persist"):
            attempt_id = c.attempt_id_for(kwargs["comparison_id"], model_id, 1)
            await c.append_evidence(kwargs["epoch"], "llm.judgment", {
                "judgment_id": c.judgment_id_for({
                    "attempt_id": attempt_id,
                    "comparison_id": kwargs["comparison_id"],
                    "model_id": model_id,
                    "winner": "A",
                    "ratio": "2:1",
                    "explanation": "ok",
                }),
                "attempt_id": attempt_id,
                "comparison_id": kwargs["comparison_id"],
                "model_id": model_id,
                "winner": "A",
                "ratio": "2:1",
                "explanation": "ok",
                "summary": "ok",
            })
        return {"winner": "A", "ratio": "2:1", "explanation": "ok"}

    monkeypatch.setattr(c, "fetch_top_models", models)
    monkeypatch.setattr(c, "llm_pairwise_compare", compare)
    monkeypatch.setattr(c, "OPENROUTER_API_KEY", "k")
    commits = [
        {
            "contributor": "alice",
            "oid": "sha1:" + "a" * 40,
            "message": "a",
            "patch": "pa",
        },
        {
            "contributor": "bob",
            "oid": "sha1:" + "b" * 40,
            "message": "b",
            "patch": "pb",
        },
    ]
    asyncio.run(c.rank_commits(commits, epoch=7))
    first_calls = calls["n"]
    assert first_calls >= 3
    asyncio.run(c.rank_commits(commits, epoch=7))
    assert calls["n"] == first_calls


def test_epochs_index_lists_epochs(evidence_store):
    asyncio.run(c.store.append(c.Emission(
        epoch=3,
        timestamp_ms=1,
        pool_before="1",
        total_emitted="0",
        pool_after="1",
        decay_rate="0",
        distributions={},
        ranking={},
        models_used=[],
    )))
    html = asyncio.run(c.epochs_index()).body.decode()
    assert "/epochs/3" in html


def test_api_ledger_remains_a_list(evidence_store):
    asyncio.run(c.append_evidence(1, "epoch.started", {"summary": "x", "boundary_ms": 1}))
    rows = asyncio.run(c.get_ledger())
    assert isinstance(rows, list)
    assert rows[0]["type"] == "evidence"
