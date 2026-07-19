"""Streaming JSONL projection and direct Rocks path equivalence."""

from __future__ import annotations

import asyncio
import json

import pytest

import constitution as c


def _sample_tape(path):
    emission = c.Emission(
        epoch=0,
        timestamp_ms=100,
        pool_before=str(c.CONTRIBUTOR_POOL),
        total_emitted="1",
        pool_after=str(c.CONTRIBUTOR_POOL - c.Decimal("1")),
        decay_rate="0.1",
        distributions={"alice": "1"},
        ranking={"alice": "1"},
        models_used=["model/a"],
        discovery_snapshot_id="snap-0",
        evidence_schema_version=c.EVIDENCE_SCHEMA_VERSION,
        ranking_run_id="rank-0",
        ranking_event_id="rank-event-0",
    )
    emission_row = c.to_dict(emission)
    chain = c._chain_digest(emission_row)

    patch = b"@@ -1 +1 @@\n-old\n+new\n"
    commit_id = "c_imported"
    commit = c.Evidence(
        schema_version=2,
        event_id="ev_commit",
        epoch=1,
        kind="git.commit",
        recorded_at_ms=101,
        previous_event_sha256=chain,
        payload={
            "commit_id": commit_id,
            "oid": "sha1:" + "a" * 40,
            "contributor": "alice",
            "message": c._bytes_blob("imported commit"),
            "patch": c._bytes_blob(patch),
            "summary": "legacy embedded patch",
        },
    )
    commit_row = c.to_dict(commit)
    chain = c._chain_digest(commit_row)

    comparison = c.Evidence(
        schema_version=2,
        event_id="ev_comparison",
        epoch=1,
        kind="comparison.input",
        recorded_at_ms=102,
        previous_event_sha256=chain,
        payload={
            "comparison_id": "cmp_imported",
            "side_a": {"commit_id": commit_id, "contributor": "alice"},
            "side_b": {"commit_id": "c_other", "contributor": "bob"},
            "prompt": c._bytes_blob("which is better?"),
            "summary": "imported comparison",
        },
    )
    comparison_row = c.to_dict(comparison)
    chain = c._chain_digest(comparison_row)

    judgment = c.Evidence(
        schema_version=2,
        event_id="ev_judgment",
        epoch=1,
        kind="llm.judgment",
        recorded_at_ms=103,
        previous_event_sha256=chain,
        payload={
            "judgment_id": "jud_imported",
            "comparison_id": "cmp_imported",
            "attempt_id": "att_imported",
            "model_id": "model/a",
            "winner": "A",
            "ratio": "2:1",
            "explanation": "A was better.",
            "summary": "imported judgment",
        },
    )
    rows = [emission_row, commit_row, comparison_row, c.to_dict(judgment)]
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    return rows, patch


def test_streaming_import_preserves_contract_and_direct_indexes(tmp_path, monkeypatch):
    tape = tmp_path / "ledger.jsonl"
    rocks = tmp_path / "ledger.rocks"
    rows, patch = _sample_tape(tape)

    report = c.import_jsonl_tape(tape, rocks, batch_size=2)
    assert report["ok"] is True
    assert report["event_count"] == len(rows)
    assert report["evidence_count"] == 3
    assert report["emission_count"] == 1
    assert report["chain_mismatches"] == 0

    db = c.RocksDb.open(rocks)
    monkeypatch.setattr(c, "state_db", db)
    try:
        assert c.ROOT.events.len(db) == len(rows)
        stored_row = c.ROOT.events.get(db, 1)
        assert stored_row["event_id"] == rows[1]["event_id"]
        assert stored_row["previous_event_sha256"] == rows[1]["previous_event_sha256"]
        assert c.find_evidence("ev_commit").payload["commit_id"] == "c_imported"
        assert (
            c.find_evidence_payload(
                "comparison.input", "comparison_id", "cmp_imported"
            ).event_id
            == "ev_comparison"
        )
        judgments = c._judgments_for_comparison("cmp_imported")
        assert [row.event_id for row in judgments] == ["ev_judgment"]
        assert c._emission_for_epoch(0).ranking == {"alice": "1"}

        # The immutable tape remains byte-authoritative. Its identity and
        # hardlinks survive projection compaction while heavy bytes move once.
        imported = c.find_evidence("ev_commit")
        assert imported.event_id == rows[1]["event_id"]
        assert imported.payload["patch"] == {
            "encoding": "blob",
            "sha256": c._sha256_hex(patch),
            "byte_length": len(patch),
        }
        response = asyncio.run(c.commit_patch_download("c_imported"))
        assert response.body == patch
        prompt = asyncio.run(c.comparison_prompt_download("cmp_imported"))
        assert prompt.body == b"which is better?"

        # Runtime selectors do not consult the archival tape.
        tape.unlink()
        assert asyncio.run(c.get_ranking()) == {
            "ranking": {"alice": "1"},
            "epoch": 0,
        }
        page = asyncio.run(c.get_ledger(offset=1, limit=2, full=1))
        assert [row["event_id"] for row in page] == [
            "ev_commit",
            "ev_comparison",
        ]
    finally:
        db.close()


def test_import_externalizes_nested_discovery_and_attempt_blobs(
    tmp_path, monkeypatch
):
    tape = tmp_path / "nested.jsonl"
    rocks = tmp_path / "nested.rocks"
    patch = b"nested discovery patch"
    request = b'{"messages":["large request"]}'
    response = b'{"choices":["large response"]}'
    discovery = {
        "type": "gitdiscovery",
        "schema_version": 1,
        "epoch": 4,
        "snapshot_id": "snap-nested",
        "timestamp_ms": 1,
        "config_digest": "cfg",
        "initial_snapshot": False,
        "configuration": {},
        "repositories": [],
        "observations": [{"oid": "sha1:abc", "patch": c._bytes_blob(patch)}],
        "commits": [{"oid": "sha1:abc", "patch": c._bytes_blob(patch)}],
    }
    attempt = {
        "type": "evidence",
        "schema_version": 2,
        "event_id": "ev_attempt",
        "epoch": 4,
        "kind": "llm.attempt_finished",
        "recorded_at_ms": 2,
        "previous_event_sha256": c._chain_digest(discovery),
        "payload": {
            "attempt_id": "att-nested",
            "comparison_id": "cmp-nested",
            "request": c._bytes_blob(request),
            "response": c._bytes_blob(response),
        },
    }
    tape.write_text(
        json.dumps(discovery, separators=(",", ":")) + "\n"
        + json.dumps(attempt, separators=(",", ":")) + "\n"
    )

    c.import_jsonl_tape(tape, rocks)
    db = c.RocksDb.open(rocks)
    monkeypatch.setattr(c, "state_db", db)
    try:
        stored_discovery = c.ROOT.discoveries_by_epoch.key(4).get(db)
        observation_ref = stored_discovery["observations"][0]["patch"]
        commit_ref = stored_discovery["commits"][0]["patch"]
        assert observation_ref == commit_ref
        assert observation_ref["encoding"] == "blob"
        assert c.ROOT.blobs.key(observation_ref["sha256"]).get(db) == patch
        hydrated = c._discovery_for_epoch(4)
        assert hydrated.observations[0]["patch"] == patch.decode()

        stored_attempt = c.find_evidence("ev_attempt")
        assert stored_attempt.event_id == attempt["event_id"]
        assert stored_attempt.payload["request"]["encoding"] == "blob"
        assert stored_attempt.payload["response"]["encoding"] == "blob"
        assert (
            asyncio.run(c.attempt_blob_download("att-nested", "request")).body
            == request
        )
        assert (
            asyncio.run(c.attempt_blob_download("att-nested", "response")).body
            == response
        )
    finally:
        db.close()


def test_large_imported_patch_is_not_decoded_by_epoch_or_detail_cards(
    tmp_path, monkeypatch
):
    tape = tmp_path / "large.jsonl"
    rocks = tmp_path / "large.rocks"
    rows, _ = _sample_tape(tape)
    patch = b"diff --git a/x b/x\n" + b"+" * (8 * 1024 * 1024)
    rows[1]["payload"]["patch"] = c._bytes_blob(patch)
    rows[2]["payload"]["side_a"]["diff"] = c._bytes_blob(patch)
    rows[2]["payload"]["side_b"]["diff"] = c._bytes_blob(patch)
    with tape.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")

    c.import_jsonl_tape(tape, rocks)
    db = c.RocksDb.open(rocks)
    monkeypatch.setattr(c, "state_db", db)
    original_decode = c._decode_blob
    decoded_refs = []

    def record_ref_decodes(value):
        if value and value.get("encoding") == "blob":
            decoded_refs.append(value["sha256"])
        return original_decode(value)

    monkeypatch.setattr(c, "_decode_blob", record_ref_decodes)
    try:
        stored = c.ROOT.evidence_by_id.key("ev_commit").get(db)
        assert len(json.dumps(stored)) < 4_000
        assert c.ROOT.blobs.len(db) == 2  # patch/diffs dedupe, plus prompt

        epoch_response = asyncio.run(c.epoch_detail(1))
        commit_response = asyncio.run(c.commit_detail("c_imported"))
        comparison_response = asyncio.run(c.comparison_detail("cmp_imported"))
        assert len(epoch_response.body) < 1_000_000
        assert len(commit_response.body) < 1_000_000
        assert len(comparison_response.body) < 1_000_000
        assert decoded_refs == []

        assert asyncio.run(c.commit_patch_download("c_imported")).body == patch
        assert decoded_refs == [c._sha256_hex(patch)]
        assert (
            asyncio.run(c.comparison_diff_download("cmp_imported", "a")).body
            == patch
        )
    finally:
        db.close()


def test_new_heavy_bytes_are_stored_once_and_routes_resolve_refs(
    tmp_path, monkeypatch
):
    db = c.RocksDb.open(tmp_path / "live.rocks")
    monkeypatch.setattr(c, "state_db", db)
    try:
        patch = b"large patch bytes\n" * 100
        logical_payload = {
            "commit_id": "c_new",
            "oid": "sha1:" + "b" * 40,
            "message": c._bytes_blob("new commit"),
            "patch": c._bytes_blob(patch),
            "summary": "new ref-backed patch",
        }
        expected_event_id = c._content_id("ev", {
            "schema_version": c.EVIDENCE_SCHEMA_VERSION,
            "epoch": 2,
            "kind": "git.commit",
            "payload": logical_payload,
        })
        event = asyncio.run(
            c.append_evidence(2, "git.commit", logical_payload)
        )
        assert event.event_id == expected_event_id
        ref = event.payload["patch"]
        assert ref == {
            "encoding": "blob",
            "sha256": c._sha256_hex(patch),
            "byte_length": len(patch),
        }
        assert c.ROOT.blobs.key(ref["sha256"]).get(db) == patch
        assert asyncio.run(c.commit_patch_download("c_new")).body == patch

        # Idempotency does not append or duplicate the canonical blob.
        again = asyncio.run(
            c.append_evidence(2, "git.commit", logical_payload)
        )
        assert again.event_id == event.event_id
        assert c.ROOT.events.len(db) == 1
        assert c.ROOT.blobs.len(db) == 1
    finally:
        db.close()


def test_failed_import_destroys_partial_projection(tmp_path):
    tape = tmp_path / "bad.jsonl"
    rocks = tmp_path / "bad.rocks"
    tape.write_text(
        json.dumps({"type": "redemption", "timestamp_ms": 1}) + "\n"
        + "{not-json}\n"
    )
    with pytest.raises(ValueError, match="line 2"):
        c.import_jsonl_tape(tape, rocks, batch_size=1)

    db = c.RocksDb.open(rocks)
    try:
        assert c.ROOT.events.len(db) == 0
    finally:
        db.destroy()


def test_import_refuses_nonempty_projection_without_force(tmp_path):
    tape = tmp_path / "ledger.jsonl"
    rocks = tmp_path / "ledger.rocks"
    _sample_tape(tape)
    c.import_jsonl_tape(tape, rocks)
    with pytest.raises(RuntimeError, match="not empty"):
        c.import_jsonl_tape(tape, rocks)

    report = c.import_jsonl_tape(tape, rocks, force=True)
    assert report["event_count"] == 4


def _configure_first_boot(monkeypatch, tape, rocks):
    monkeypatch.setattr(c, "JSONL_PATH", tape)
    monkeypatch.setattr(c, "ROCKS_PATH", rocks)
    monkeypatch.setattr(c, "state_db", None)
    monkeypatch.setenv("IMPORT_JSONL_ON_EMPTY", "1")
    c.STATE_STATUS.update(
        ready=False, importing=False, error=None, event_count=0
    )


def test_first_boot_imports_before_opening_live_state(tmp_path, monkeypatch):
    tape = tmp_path / "ledger.jsonl"
    rocks = tmp_path / "constitution.rocks"
    rows, _ = _sample_tape(tape)
    _configure_first_boot(monkeypatch, tape, rocks)

    original_import = c.import_jsonl_tape
    observed = {}

    def import_while_closed(*args, **kwargs):
        observed["state_db_closed"] = c.state_db is None
        observed["importing"] = c.STATE_STATUS["importing"]
        return original_import(*args, **kwargs)

    monkeypatch.setattr(c, "import_jsonl_tape", import_while_closed)
    status = c.prepare_state_sync()
    try:
        assert observed == {"state_db_closed": True, "importing": True}
        assert status == {
            "ready": True,
            "importing": False,
            "error": None,
            "event_count": len(rows),
        }
        assert c.ROOT.events.len(c.state_db) == len(rows)
        assert asyncio.run(c.get_health())["ok"] is True
    finally:
        c.state_db.close()
        c.state_db = None


def test_first_boot_never_overwrites_nonempty_rocks(tmp_path, monkeypatch):
    tape = tmp_path / "ledger.jsonl"
    rocks = tmp_path / "constitution.rocks"
    rows, _ = _sample_tape(tape)
    c.import_jsonl_tape(tape, rocks)
    tape.write_text("{this would fail if imported}\n")
    _configure_first_boot(monkeypatch, tape, rocks)

    status = c.prepare_state_sync()
    try:
        assert status["event_count"] == len(rows)
        assert c.ROOT.events.get(c.state_db, 1)["event_id"] == "ev_commit"
    finally:
        c.state_db.close()
        c.state_db = None


def test_startup_opens_existing_rocks_and_reports_ready(tmp_path, monkeypatch):
    tape = tmp_path / "ledger.jsonl"
    rocks = tmp_path / "constitution.rocks"
    rows, _ = _sample_tape(tape)
    c.import_jsonl_tape(tape, rocks)
    _configure_first_boot(monkeypatch, tape, rocks)
    monkeypatch.setenv("DISABLE_EPOCH_LOOP", "1")

    asyncio.run(c.startup())
    try:
        health = asyncio.run(c.get_health())
        assert health == {
            "ok": True,
            "ledger_ready": True,
            "importing": False,
            "error": None,
            "event_count": len(rows),
        }
    finally:
        c.state_db.close()
        c.state_db = None


def test_typed_emission_retry_does_not_duplicate_canonical_row(
    tmp_path, monkeypatch
):
    db = c.RocksDb.open(tmp_path / "live.rocks")
    monkeypatch.setattr(c, "state_db", db)
    emission = c.Emission(
        epoch=7,
        timestamp_ms=1,
        pool_before="10",
        total_emitted="1",
        pool_after=str(c.CONTRIBUTOR_POOL - c.Decimal("1")),
        decay_rate="0.1",
        distributions={},
        ranking={},
        models_used=[],
        discovery_snapshot_id="snap",
        evidence_schema_version=2,
        ranking_run_id="rank",
        ranking_event_id="event",
    )
    try:
        first = asyncio.run(c._append_typed_event(emission))
        second = asyncio.run(c._append_typed_event(emission))
        assert first == second
        assert c.ROOT.events.len(db) == 1
        assert c.ROOT.emissions_by_epoch.len(db) == 1
    finally:
        db.close()


@pytest.mark.parametrize("duplicate_type", ["evidence", "emission"])
def test_import_rejects_duplicate_unique_events(tmp_path, duplicate_type):
    tape = tmp_path / "ledger.jsonl"
    rocks = tmp_path / "constitution.rocks"
    rows, _ = _sample_tape(tape)
    duplicate = rows[1] if duplicate_type == "evidence" else rows[0]
    with tape.open("a") as fh:
        fh.write(json.dumps(duplicate) + "\n")

    with pytest.raises(ValueError, match=f"duplicate {duplicate_type}"):
        c.import_jsonl_tape(tape, rocks, batch_size=100)
    db = c.RocksDb.open(rocks)
    try:
        assert c.ROOT.events.len(db) == 0
    finally:
        db.destroy()
