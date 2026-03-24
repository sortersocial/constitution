"""
Unit tests for the two-phase pairwise ranking algorithm.

Phase 1 — spanning tree: union-find, bridge all disconnected components.
Phase 2 — zip sort: bubble-sort passes over current ranking until stable.

compare_fn(i, j) -> list of (winner_idx, loser_idx, w_ratio, l_ratio)
  Returns a list so multiple model votes per comparison are supported.
progress_fn(event: dict) -> None
  Called after each comparison with phase/step info.
"""

import asyncio
import pytest
from constitution import UnionFind, pairwise_rank, rank_centrality


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# UnionFind
# ---------------------------------------------------------------------------

def test_union_find_initially_all_separate():
    uf = UnionFind(4)
    assert uf.num_components() == 4
    for i in range(4):
        for j in range(4):
            assert uf.connected(i, j) == (i == j)


def test_union_find_union_reduces_components():
    uf = UnionFind(4)
    uf.union(0, 1)
    assert uf.num_components() == 3
    assert uf.connected(0, 1)
    uf.union(2, 3)
    assert uf.num_components() == 2
    uf.union(1, 2)
    assert uf.num_components() == 1
    assert uf.connected(0, 3)


def test_union_find_idempotent():
    uf = UnionFind(3)
    uf.union(0, 1)
    uf.union(0, 1)
    assert uf.num_components() == 2


def test_union_find_path_compression():
    uf = UnionFind(5)
    uf.union(0, 1)
    uf.union(1, 2)
    uf.union(2, 3)
    uf.union(3, 4)
    assert uf.num_components() == 1
    assert uf.connected(0, 4)


# ---------------------------------------------------------------------------
# helpers for building mock compare functions
# ---------------------------------------------------------------------------

def make_compare_fn(winner_fn):
    """
    winner_fn(i, j) -> winner index (i or j).
    Returns an async compare_fn that always reports ratio 2:1.
    Records all (i, j) calls in `calls`.
    """
    calls = []

    async def compare_fn(i, j):
        calls.append((i, j))
        w = winner_fn(i, j)
        l = j if w == i else i
        return [(w, l, 2.0, 1.0)]

    compare_fn.calls = calls
    return compare_fn


def lower_wins(i, j):
    """Lower index always wins — deterministic total order."""
    return min(i, j)


def higher_wins(i, j):
    return max(i, j)


def identity_order(n):
    """lower_wins already produces 0 > 1 > 2 ... in rank_centrality terms."""
    return list(range(n))


# ---------------------------------------------------------------------------
# Phase 1 — spanning tree
# ---------------------------------------------------------------------------

def test_spanning_tree_connects_all_n_minus_1_comparisons():
    """Exactly n-1 comparisons needed to span n items."""
    for n in range(2, 8):
        cmp = make_compare_fn(lower_wins)
        run(pairwise_rank(n, cmp))
        # Phase 1 produces exactly n-1 spanning-tree comparisons.
        # Phase 2 adds more, but the first n-1 are the spanning tree.
        assert len(cmp.calls) >= n - 1


def test_spanning_tree_single_item():
    cmp = make_compare_fn(lower_wins)
    pairs = run(pairwise_rank(1, cmp))
    assert cmp.calls == []
    assert pairs == []


def test_spanning_tree_zero_items():
    cmp = make_compare_fn(lower_wins)
    pairs = run(pairwise_rank(0, cmp))
    assert cmp.calls == []
    assert pairs == []


def test_spanning_tree_two_items():
    cmp = make_compare_fn(lower_wins)
    pairs = run(pairwise_rank(2, cmp))
    # One spanning-tree comparison + at least one zip comparison.
    assert (0, 1) in cmp.calls or (1, 0) in cmp.calls


def test_spanning_tree_covers_all_items():
    """After phase 1, rank_centrality must produce scores for every item."""
    n = 5
    cmp = make_compare_fn(lower_wins)
    pairs = run(pairwise_rank(n, cmp))
    scores = rank_centrality(pairs)
    assert len(scores) == n
    assert all(s > 0 for s in scores)


# ---------------------------------------------------------------------------
# Phase 2 — zip sort
# ---------------------------------------------------------------------------

def test_zip_terminates_when_already_sorted():
    """If every comparison confirms the current order, one zip pass suffices."""
    n = 4
    cmp = make_compare_fn(lower_wins)
    pairs = run(pairwise_rank(n, cmp))
    scores = rank_centrality(pairs)
    ranking = sorted(range(n), key=lambda i: scores[i], reverse=True)
    assert ranking == list(range(n))  # item 0 wins everything → highest score


def test_zip_corrects_reversed_order():
    """higher_wins produces the inverse order; zip sort should converge to it."""
    n = 4
    cmp = make_compare_fn(higher_wins)
    pairs = run(pairwise_rank(n, cmp))
    scores = rank_centrality(pairs)
    ranking = sorted(range(n), key=lambda i: scores[i], reverse=True)
    assert ranking == [3, 2, 1, 0]  # item 3 wins everything → highest score


def test_zip_single_pass_minimum():
    """At least one full zip pass (n-1 comparisons) after spanning tree."""
    n = 4
    cmp = make_compare_fn(lower_wins)
    run(pairwise_rank(n, cmp))
    # Phase 1: n-1 = 3 comparisons. Phase 2 at least one pass: n-1 = 3 more.
    assert len(cmp.calls) >= 2 * (n - 1)


def test_zip_bounded_by_n_passes():
    """Total comparisons bounded: n-1 (spanning) + n*(n-1) (max zip passes)."""
    n = 5
    cmp = make_compare_fn(lower_wins)
    run(pairwise_rank(n, cmp))
    max_comparisons = (n - 1) + n * (n - 1)
    assert len(cmp.calls) <= max_comparisons


def test_zip_stable_compare_fn_one_pass():
    """
    A compare_fn consistent with the initial ranking should trigger exactly
    one zip pass (the pass finds no swaps and exits).
    """
    n = 3
    comparisons = []

    async def stable_compare(i, j):
        comparisons.append((i, j))
        return [(min(i, j), max(i, j), 2.0, 1.0)]

    run(pairwise_rank(n, stable_compare))
    # Spanning tree: n-1 = 2. One zip pass: n-1 = 2. Total = 4.
    assert len(comparisons) == 2 * (n - 1)


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------

def test_progress_reports_spanning_tree_phase():
    events = []

    async def progress_fn(event):
        events.append(event)

    run(pairwise_rank(4, make_compare_fn(lower_wins), progress_fn))
    spanning_events = [e for e in events if e["phase"] == "spanning_tree"]
    assert len(spanning_events) == 3  # n-1 = 3
    for i, e in enumerate(spanning_events):
        assert e["step"] == i + 1
        assert e["total"] == 3


def test_progress_reports_zip_phase():
    events = []

    async def progress_fn(event):
        events.append(event)

    run(pairwise_rank(3, make_compare_fn(lower_wins), progress_fn))
    zip_events = [e for e in events if e["phase"] == "zip"]
    assert len(zip_events) > 0
    for e in zip_events:
        assert "pass" in e
        assert "step" in e
        assert "total" in e
        assert e["pass"] >= 1
        assert 1 <= e["step"] <= e["total"]


def test_progress_pass_numbers_increase():
    events = []

    async def progress_fn(event):
        events.append(event)

    # higher_wins causes swaps, triggering multiple passes.
    run(pairwise_rank(4, make_compare_fn(higher_wins), progress_fn))
    zip_events = [e for e in events if e["phase"] == "zip"]
    passes_seen = sorted(set(e["pass"] for e in zip_events))
    assert passes_seen[0] == 1
    assert passes_seen == list(range(1, len(passes_seen) + 1))


def test_progress_no_duplicate_spanning_steps():
    events = []

    async def progress_fn(event):
        events.append(event)

    run(pairwise_rank(5, make_compare_fn(lower_wins), progress_fn))
    spanning_steps = [e["step"] for e in events if e["phase"] == "spanning_tree"]
    assert spanning_steps == sorted(set(spanning_steps))


# ---------------------------------------------------------------------------
# rank_centrality accumulates (no overwrite bug)
# ---------------------------------------------------------------------------

def test_rank_centrality_accumulates_multiple_votes():
    """Two identical votes double the weights but preserve the ratio → same pi."""
    pairs_double = [(0, 1, 2.0, 1.0), (0, 1, 2.0, 1.0)]
    pairs_single = [(0, 1, 2.0, 1.0)]
    scores_d = rank_centrality(pairs_double)
    scores_s = rank_centrality(pairs_single)
    # Item 0 wins → higher score.
    assert scores_d[0] > scores_d[1]
    assert scores_s[0] > scores_s[1]
    # Doubling identical votes preserves the ratio → same pi.
    assert abs(scores_d[0] - scores_s[0]) < 1e-9


def test_rank_centrality_conflicting_votes_not_erased():
    """With +=, both models' votes accumulate.

    Model A: 0 beats 1 strongly (3:1). Model B: 1 beats 0 weakly (1.5:1).
    Net: 0 is still the stronger winner → scores[0] > scores[1].

    With the old overwrite bug, model B's vote would erase model A entirely.
    """
    pairs = [(0, 1, 3.0, 1.0), (1, 0, 1.5, 1.0)]
    scores = rank_centrality(pairs)
    assert scores[0] > scores[1]


# ---------------------------------------------------------------------------
# rank_centrality correctness — winners should win
# ---------------------------------------------------------------------------

def test_rank_centrality_two_items_winner_ranks_higher():
    scores = rank_centrality([(0, 1, 2.0, 1.0)])
    assert scores[0] > scores[1]


def test_rank_centrality_scores_sum_to_one():
    import numpy as np
    scores = rank_centrality([(0, 1, 2.0, 1.0), (1, 2, 3.0, 1.0), (0, 2, 4.0, 1.0)])
    assert abs(scores.sum() - 1.0) < 1e-9


def test_rank_centrality_total_order_three_items():
    """0 beats 1 beats 2, and 0 beats 2 directly — strict ranking expected."""
    pairs = [(0, 1, 2.0, 1.0), (1, 2, 2.0, 1.0), (0, 2, 4.0, 1.0)]
    scores = rank_centrality(pairs)
    assert scores[0] > scores[1] > scores[2]


def test_rank_centrality_unanimous_council_strengthens_winner():
    """Three models all agree: 0 beats 1. Each vote stacks; 0 still wins."""
    pairs = [(0, 1, 2.0, 1.0)] * 3
    scores = rank_centrality(pairs)
    assert scores[0] > scores[1]


def test_rank_centrality_dominant_item_ranks_first():
    """Item 0 beats everyone. It should have the highest score."""
    n = 5
    pairs = [(0, i, 3.0, 1.0) for i in range(1, n)]
    scores = rank_centrality(pairs)
    assert scores[0] == max(scores)


def test_rank_centrality_symmetric_vote_equal_scores():
    """If 0 beats 1 and 1 beats 0 equally, scores should be equal."""
    pairs = [(0, 1, 1.0, 1.0), (1, 0, 1.0, 1.0)]
    scores = rank_centrality(pairs)
    assert abs(scores[0] - scores[1]) < 1e-9


def test_rank_centrality_stronger_ratio_gives_higher_score():
    """0 beats 1 with 9:1 vs 0 beats 1 with 2:1 — 9:1 winner scores higher."""
    scores_strong = rank_centrality([(0, 1, 9.0, 1.0)])
    scores_weak   = rank_centrality([(0, 1, 2.0, 1.0)])
    assert scores_strong[0] > scores_weak[0]
