"""COUNCIL_VOTES_PER_EDGE: each comparison edge gets a configurable number
of council votes (default 3, floor 1), seated by a deterministic per-edge
rotation. Cached judgments claim seats before any paid live call, so resumed
runs never re-pay for an edge that already has enough votes.
"""

import asyncio

import pytest

import constitution as c


@pytest.fixture
def evidence_store(tmp_path, monkeypatch):
    db = c.RocksDb.open(tmp_path / "ledger.rocks")
    monkeypatch.setattr(c, "state_db", db)
    monkeypatch.setattr(c, "PUBLIC_BASE_URL", "http://test.local")
    yield tmp_path
    db.close()


def _commits(n: int) -> list[dict]:
    return [
        {
            "contributor": f"person-{index}",
            "oid": "sha1:" + format(index, "x").rjust(40, "0"),
            "message": f"commit {index}",
            "patch": f"patch {index}",
        }
        for index in range(n)
    ]


def _mock_council(monkeypatch, models: list[str], fail_models: set[str] = frozenset()):
    """Patch model selection + LLM calls; return the list of live calls made."""
    calls = []
    monkeypatch.setattr(c, "PREFERRED_COUNCIL_MODELS", [])
    monkeypatch.setattr(c, "OPENROUTER_API_KEY", "k")

    async def fetch(n=3):
        return list(models)

    async def compare(model_id, side_a, side_b, **kwargs):
        calls.append((kwargs["comparison_id"], model_id))
        if model_id in fail_models:
            raise RuntimeError(f"{model_id} is down")
        attempt_id = c.attempt_id_for(kwargs["comparison_id"], model_id, 1)
        material = {
            "attempt_id": attempt_id,
            "comparison_id": kwargs["comparison_id"],
            "model_id": model_id,
            "winner": "A",
            "ratio": "2:1",
            "explanation": "ok",
        }
        await c.append_evidence(kwargs["epoch"], "llm.judgment", {
            "judgment_id": c.judgment_id_for(material),
            **material,
            "summary": "ok",
        })
        return {"winner": "A", "ratio": "2:1", "explanation": "ok"}

    monkeypatch.setattr(c, "fetch_top_models", fetch)
    monkeypatch.setattr(c, "llm_pairwise_compare", compare)
    return calls


# ---------------------------------------------------------------------------
# _edge_jurors — deterministic, fair rotation
# ---------------------------------------------------------------------------

def test_edge_jurors_deterministic():
    models = ["mock/a", "mock/b", "mock/c"]
    assert c._edge_jurors("cmp_x", models) == c._edge_jurors("cmp_x", models)
    assert sorted(c._edge_jurors("cmp_x", models)) == sorted(models)


def test_edge_jurors_rotate_across_edges():
    """Over many edges, every council model gets first seat at least once."""
    models = ["mock/a", "mock/b", "mock/c"]
    leaders = {c._edge_jurors(f"cmp_{i}", models)[0] for i in range(64)}
    assert leaders == set(models)


# ---------------------------------------------------------------------------
# votes per edge
# ---------------------------------------------------------------------------

def test_votes_per_edge_one_makes_single_call_per_comparison(
    evidence_store, monkeypatch
):
    monkeypatch.setattr(c, "COUNCIL_VOTES_PER_EDGE", 1)
    calls = _mock_council(monkeypatch, ["mock/a", "mock/b", "mock/c"])
    ranking, models_voted, info = asyncio.run(c.rank_commits(_commits(2), epoch=5))
    per_comparison = {}
    for comparison_id, _ in calls:
        per_comparison[comparison_id] = per_comparison.get(comparison_id, 0) + 1
    assert per_comparison  # at least one comparison happened
    assert all(count == 1 for count in per_comparison.values())
    assert len(ranking) == 2
    started = c.find_evidence_payload(
        "ranking.started", "ranking_run_id", info["ranking_run_id"]
    )
    assert started.payload["votes_per_edge"] == 1


def test_votes_per_edge_default_uses_three_votes(evidence_store, monkeypatch):
    calls = _mock_council(monkeypatch, ["mock/a", "mock/b", "mock/c"])
    asyncio.run(c.rank_commits(_commits(2), epoch=6))
    per_comparison = {}
    for comparison_id, _ in calls:
        per_comparison[comparison_id] = per_comparison.get(comparison_id, 0) + 1
    assert all(count == 3 for count in per_comparison.values())


def test_votes_per_edge_clamped_to_council_size(evidence_store, monkeypatch):
    monkeypatch.setattr(c, "COUNCIL_VOTES_PER_EDGE", 99)
    calls = _mock_council(monkeypatch, ["mock/a", "mock/b"])
    _, _, info = asyncio.run(c.rank_commits(_commits(2), epoch=7))
    per_comparison = {}
    for comparison_id, _ in calls:
        per_comparison[comparison_id] = per_comparison.get(comparison_id, 0) + 1
    assert all(count == 2 for count in per_comparison.values())
    started = c.find_evidence_payload(
        "ranking.started", "ranking_run_id", info["ranking_run_id"]
    )
    assert started.payload["votes_per_edge"] == 2


def test_resume_uses_cached_votes_before_paying(evidence_store, monkeypatch):
    monkeypatch.setattr(c, "COUNCIL_VOTES_PER_EDGE", 1)
    calls = _mock_council(monkeypatch, ["mock/a", "mock/b", "mock/c"])
    commits = _commits(3)
    asyncio.run(c.rank_commits(commits, epoch=8))
    paid = len(calls)
    assert paid >= 2  # spanning tree alone needs n-1 comparisons
    # Resume: every edge already has one cached judgment; no new calls.
    asyncio.run(c.rank_commits(commits, epoch=8))
    assert len(calls) == paid


def test_retired_model_does_not_reduce_votes(evidence_store, monkeypatch):
    """A dead juror's seat passes down the rotation; edges still get full votes."""
    monkeypatch.setattr(c, "COUNCIL_VOTES_PER_EDGE", 2)
    calls = _mock_council(
        monkeypatch, ["mock/a", "mock/b", "mock/dead"], fail_models={"mock/dead"}
    )
    asyncio.run(c.rank_commits(_commits(2), epoch=9))
    ok_per_comparison = {}
    for comparison_id, model in calls:
        if model != "mock/dead":
            ok_per_comparison[comparison_id] = (
                ok_per_comparison.get(comparison_id, 0) + 1
            )
    assert all(count == 2 for count in ok_per_comparison.values())
