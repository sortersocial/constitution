"""
Unit tests for the two-phase pairwise ranking algorithm.

Phase 1 — spanning tree: union-find, bridge all disconnected components.
Phase 2 — zip sort: before every step, re-derive the current ranking and
  compare the first adjacent pair that has no direct comparison result yet.
  Terminates when every adjacent slot is covered.  No pair is ever compared
  more than once.

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


def test_spanning_tree_terminates_without_zip_when_fully_covers_adjacent_pairs():
    """
    When the spanning-tree comparisons already cover every adjacent pair in
    the rank_centrality ordering, phase 2 makes zero additional comparisons.

    With higher_wins the spanning tree compares (0,1),(1,2),(2,3) and the
    resulting ranking is [3,2,1,0] whose adjacent pairs {2,3},{1,2},{0,1}
    are exactly the spanning-tree pairs — all covered, so zip exits at once.
    """
    for n in range(2, 7):
        cmp = make_compare_fn(higher_wins)
        run(pairwise_rank(n, cmp))
        assert len(cmp.calls) == n - 1, (
            f"n={n}: expected only spanning-tree comparisons ({n-1}), "
            f"got {len(cmp.calls)}"
        )


def test_zip_bounded_by_n_passes():
    """Total comparisons bounded: n-1 (spanning) + n*(n-1) (max zip pairs)."""
    n = 5
    cmp = make_compare_fn(lower_wins)
    run(pairwise_rank(n, cmp))
    max_comparisons = (n - 1) + n * (n - 1)
    assert len(cmp.calls) <= max_comparisons


def test_zip_rescan_from_top_after_ranking_shift():
    """
    Exemplifies the bug in the old algorithm.

    Old behaviour: the zip loop iterated step=0,1,…,n-2 within a pass,
    updating `ranking` after each comparison but never going back.  When a
    comparison at step k shifted the global rank_centrality scores so that
    position k-1 now held a brand-new, never-compared pair, the old code
    skipped it until the next full pass — re-comparing already-settled pairs
    (including spanning-tree pairs) along the way.

    New behaviour: before every comparison the full ranking is re-derived and
    the *first* uncovered adjacent slot is chosen.  Each pair is compared at
    most once; the algorithm terminates as soon as every adjacent slot is
    covered.

    Verification: across all deterministic total orders and small n, no pair
    is ever compared more than once.  The old algorithm violated this for
    lower_wins (where the spanning-tree chain already establishes the correct
    order, yet the old zip pass re-compared every spanning-tree pair).
    """
    for n in range(2, 7):
        for winner_fn in [lower_wins, higher_wins]:
            cmp = make_compare_fn(winner_fn)
            run(pairwise_rank(n, cmp))

            seen: set = set()
            for i, j in cmp.calls:
                pair = frozenset({i, j})
                assert pair not in seen, (
                    f"Bug: {winner_fn.__name__} n={n}: "
                    f"pair ({i},{j}) compared more than once.\n"
                    f"All calls: {cmp.calls}"
                )
                seen.add(pair)


def test_zip_stable_compare_fn_spanning_tree_only():
    """
    When lower index always wins, the spanning-tree chain (0,1),(1,2),…
    establishes ranking [0,1,2,…] whose adjacent pairs are already covered.
    Phase 2 makes zero additional comparisons.
    """
    n = 3
    comparisons = []

    async def stable_compare(i, j):
        comparisons.append((i, j))
        return [(min(i, j), max(i, j), 2.0, 1.0)]

    run(pairwise_rank(n, stable_compare))
    # Only the spanning tree's n-1 comparisons; zip exits immediately.
    assert len(comparisons) == n - 1


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
    """Zip events carry pass/step/total keys; pass is always 1; step increases."""
    events = []

    async def progress_fn(event):
        events.append(event)

    # With lower_wins the spanning tree alone covers all adjacencies, so use a
    # compare_fn that leaves one adjacent pair uncovered: (0,1)→0 wins 1.1:1,
    # (1,2)→2 wins 100:1.  rank_centrality then gives ranking [2,0,1] whose
    # pair {0,2} is not in the spanning tree → exactly one zip comparison.
    async def one_zip_cmp(i, j):
        if frozenset({i, j}) == frozenset({0, 1}):
            return [(0, 1, 1.1, 1.0)]
        if frozenset({i, j}) == frozenset({1, 2}):
            return [(2, 1, 100.0, 1.0)]
        return [(min(i, j), max(i, j), 2.0, 1.0)]

    run(pairwise_rank(3, one_zip_cmp, progress_fn))
    zip_events = [e for e in events if e["phase"] == "zip"]
    assert len(zip_events) > 0
    for e in zip_events:
        assert "pass" in e
        assert "step" in e
        assert "total" in e
        assert e["pass"] == 1          # exactly one logical pass
        assert e["step"] >= 1
        assert e["total"] == 2         # n-1 = 2


def test_progress_zip_step_is_position_in_ranking():
    """step is how far down the ranking we scanned to find the first uncovered pair."""
    events = []

    async def progress_fn(event):
        events.append(event)

    async def one_zip_cmp(i, j):
        if frozenset({i, j}) == frozenset({0, 1}):
            return [(0, 1, 1.1, 1.0)]
        if frozenset({i, j}) == frozenset({1, 2}):
            return [(2, 1, 100.0, 1.0)]
        return [(min(i, j), max(i, j), 2.0, 1.0)]

    run(pairwise_rank(3, one_zip_cmp, progress_fn))
    zip_events = [e for e in events if e["phase"] == "zip"]
    for e in zip_events:
        assert 1 <= e["step"] <= e["total"]


def test_progress_no_duplicate_spanning_steps():
    events = []

    async def progress_fn(event):
        events.append(event)

    run(pairwise_rank(5, make_compare_fn(lower_wins), progress_fn))
    spanning_steps = [e["step"] for e in events if e["phase"] == "spanning_tree"]
    assert spanning_steps == sorted(set(spanning_steps))


def test_cached_comparisons_yield_to_other_event_loop_work():
    async def scenario():
        heartbeat_ticks = 0
        ranking_done = False

        async def cached_compare(i, j):
            # Deliberately contains no await, matching a RocksDB cache hit.
            return [(min(i, j), max(i, j), 2.0, 1.0)]

        async def heartbeat():
            nonlocal heartbeat_ticks
            while not ranking_done:
                heartbeat_ticks += 1
                await asyncio.sleep(0)

        heartbeat_task = asyncio.create_task(heartbeat())
        await pairwise_rank(20, cached_compare)
        ranking_done = True
        await heartbeat_task
        return heartbeat_ticks

    assert run(scenario()) >= 19


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
