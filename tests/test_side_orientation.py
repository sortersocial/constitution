"""Deterministic side presentation for council comparisons.

Epoch 3 measured per-juror position bias (side-A rates from 43% to 62%
on the same comparisons) while the refine phase always presented the
currently-higher-ranked commit as side A — coupling presentation to the
standing order. Presentation is now a deterministic hash coin on the
oid pair: stable across restarts, decoupled from rank, and sticky to
pre-existing evidence so legacy judgments are never re-bought.
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


def _commits(n):
    return [
        {
            "contributor": f"person-{index % 3}",
            "oid": "sha1:" + format(index, "x").rjust(40, "0"),
            "message": f"commit {index}",
            "patch": f"patch {index}",
        }
        for index in range(n)
    ]


def _mock_council(monkeypatch, models):
    calls = []
    monkeypatch.setattr(c, "PREFERRED_COUNCIL_MODELS", [])
    monkeypatch.setattr(c, "OPENROUTER_API_KEY", "k")

    async def fetch(n=3):
        return list(models)

    async def compare(model_id, side_a, side_b, **kwargs):
        calls.append((side_a["oid"], side_b["oid"], model_id))
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


def test_orientation_is_mixed_and_deterministic(evidence_store, monkeypatch):
    calls = _mock_council(monkeypatch, ["mock/a"])
    commits = _commits(12)
    asyncio.run(c.rank_commits(commits, epoch=4))
    # Presentation must not be a pure function of oid order: with a hash
    # coin, both orientations occur across enough distinct pairs.
    lower_first = sum(1 for a, b, _ in calls if a < b)
    higher_first = sum(1 for a, b, _ in calls if a > b)
    assert lower_first > 0 and higher_first > 0, (
        f"orientation never flipped: {lower_first} vs {higher_first}"
    )
    # Deterministic across a resumed (identical) run: no new provider
    # calls, because every pair resolves to the same comparison id.
    before = len(calls)
    asyncio.run(c.rank_commits(commits, epoch=4))
    assert len(calls) == before


def test_legacy_swapped_orientation_judgments_are_reused(
    evidence_store, monkeypatch
):
    """Judgments recorded under the opposite presentation (pre-fix data)
    must satisfy the comparison without any new LLM call."""
    calls = _mock_council(monkeypatch, ["mock/a"])
    commits = _commits(2)
    ordered = sorted(commits, key=lambda r: r["oid"])
    sides = [c._commit_side_for_llm(row) for row in ordered]
    commit_ids = [c.commit_id_for_oid(row["oid"]) for row in ordered]
    ranking_run_id = c._content_id("rank", {
        "epoch": 5,
        "commit_ids": sorted(commit_ids),
    })

    def material_for(x, y):
        side_a, side_b = sides[x], sides[y]
        material = {
            "ranking_run_id": ranking_run_id,
            "side_a": {
                "contributor": side_a["contributor"],
                "commit_id": side_a["commit_id"],
                "commit_ids": [side_a["commit_id"]],
                "oid": side_a["oid"],
                "message": c._bytes_blob(side_a["message"]),
                "diff": c._bytes_blob(side_a["diff"]),
            },
            "side_b": {
                "contributor": side_b["contributor"],
                "commit_id": side_b["commit_id"],
                "commit_ids": [side_b["commit_id"]],
                "oid": side_b["oid"],
                "message": c._bytes_blob(side_b["message"]),
                "diff": c._bytes_blob(side_b["diff"]),
            },
            "prompt": c._bytes_blob(c.build_pairwise_prompt(side_a, side_b)),
        }
        return material, c.comparison_id_for(material)

    # Seed judgments under BOTH orientations so whichever presentation the
    # hash coin picks, the *other* one also exists — proving the sticky
    # lookup path never repays. (Production legacy data has one of them.)
    for x, y in ((0, 1), (1, 0)):
        _, cid = material_for(x, y)
        attempt_id = c.attempt_id_for(cid, "mock/a", 1)
        material = {
            "attempt_id": attempt_id,
            "comparison_id": cid,
            "model_id": "mock/a",
            "winner": "A",
            "ratio": "2:1",
            "explanation": "seeded",
        }
        asyncio.run(c.append_evidence(5, "llm.judgment", {
            "judgment_id": c.judgment_id_for(material),
            **material,
            "summary": "seeded",
        }))

    ranking, _, _ = asyncio.run(c.rank_commits(commits, epoch=5))
    assert calls == [], f"expected zero live calls, got {calls}"
    assert len(ranking) == 2
