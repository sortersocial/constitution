//! Explore-then-refine, payout-aware pair selection.
//!
//! Phase A (first 3 rounds): random near-perfect matchings to build a
//! degree-3 expander-like comparison graph for Rank Centrality.
//! Phase B (remainder): walk the current ranking top-down and spend
//! budget on uncovered adjacent pairs that can move contributor payout
//! (prefer cross-author), falling back to near-tie unused pairs.

use rand::rngs::SmallRng;
use rand::seq::SliceRandom;
use rand::Rng;

use crate::state::SimState;
use crate::strategies::PairStrategy;

const EXPLORE_ROUNDS: usize = 3;
const NEAR_TIE_SAMPLE: usize = 64;

pub struct ExploreRefine {
    n: usize,
    /// Explore matching rounds completed (0..EXPLORE_ROUNDS).
    explore_rounds_done: usize,
    /// Remaining pairs from the current explore matching.
    queue: Vec<(usize, usize)>,
}

impl ExploreRefine {
    pub fn new(n: usize) -> Self {
        Self {
            n,
            explore_rounds_done: 0,
            queue: Vec::new(),
        }
    }

    fn norm(i: usize, j: usize) -> (usize, usize) {
        if i < j {
            (i, j)
        } else {
            (j, i)
        }
    }

    /// One shuffle → consecutive pairing → partner-swap repair attempt.
    fn try_matching(state: &SimState, n: usize, rng: &mut SmallRng) -> Vec<(usize, usize)> {
        let mut nodes: Vec<usize> = (0..n).collect();
        nodes.shuffle(rng);

        let mut matching: Vec<(usize, usize)> = Vec::with_capacity(n / 2);
        let mut k = 0;
        while k + 1 < nodes.len() {
            matching.push(Self::norm(nodes[k], nodes[k + 1]));
            k += 2;
        }
        // Odd N: nodes.last() sits out this round.

        const REPAIR_ROUNDS: usize = 8;
        const SWAP_TRIES: usize = 16;
        for _ in 0..REPAIR_ROUNDS {
            let conflict_idxs: Vec<usize> = matching
                .iter()
                .enumerate()
                .filter(|(_, &(i, j))| state.compared(i, j))
                .map(|(idx, _)| idx)
                .collect();
            if conflict_idxs.is_empty() {
                break;
            }
            // Single leftover conflict: try swapping against any other pair.
            let partners: Vec<usize> = if conflict_idxs.len() == 1 {
                (0..matching.len()).filter(|&j| j != conflict_idxs[0]).collect()
            } else {
                conflict_idxs.clone()
            };
            if partners.is_empty() {
                break;
            }

            let mut progressed = false;
            for &ci in &conflict_idxs {
                if !state.compared(matching[ci].0, matching[ci].1) {
                    continue;
                }
                for _ in 0..SWAP_TRIES {
                    let oj = partners[rng.gen_range(0..partners.len())];
                    if oj == ci {
                        continue;
                    }

                    let (a, b) = matching[ci];
                    let (c, d) = matching[oj];
                    let cur = (state.compared(a, b) as u8) + (state.compared(c, d) as u8);

                    let alts = [
                        (Self::norm(a, c), Self::norm(b, d)),
                        (Self::norm(a, d), Self::norm(b, c)),
                    ];
                    let order = if rng.gen_bool(0.5) {
                        [0usize, 1]
                    } else {
                        [1, 0]
                    };
                    for &ai in &order {
                        let (p1, p2) = alts[ai];
                        if p1.0 == p1.1 || p2.0 == p2.1 {
                            continue;
                        }
                        let newc =
                            (state.compared(p1.0, p1.1) as u8) + (state.compared(p2.0, p2.1) as u8);
                        if newc < cur {
                            matching[ci] = p1;
                            matching[oj] = p2;
                            progressed = true;
                            break;
                        }
                    }
                    if !state.compared(matching[ci].0, matching[ci].1) {
                        break;
                    }
                }
            }
            if !progressed {
                break;
            }
        }

        matching
            .into_iter()
            .filter(|&(i, j)| !state.compared(i, j))
            .collect()
    }

    /// Build one explore round. Retries reshuffles so we usually get a
    /// full near-perfect matching even after prior rounds used edges.
    fn refill_explore(&mut self, state: &SimState, rng: &mut SmallRng) {
        let target = self.n / 2;
        let mut best = Vec::new();
        for _ in 0..48 {
            let candidate = Self::try_matching(state, self.n, rng);
            if candidate.len() > best.len() {
                best = candidate;
            }
            if best.len() >= target {
                break;
            }
        }
        self.queue = best;
    }

    fn refine(&self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        let ranking = state.ranking();
        let scores = state.scores();
        let authors = state.authors();

        // Prefer first uncovered cross-author adjacent pair (top-down).
        for p in 0..ranking.len().saturating_sub(1) {
            let i = ranking[p];
            let j = ranking[p + 1];
            if !state.compared(i, j) && authors[i] != authors[j] {
                return Some(Self::norm(i, j));
            }
        }

        // Else first uncovered adjacent pair of any kind.
        for p in 0..ranking.len().saturating_sub(1) {
            let i = ranking[p];
            let j = ranking[p + 1];
            if !state.compared(i, j) {
                return Some(Self::norm(i, j));
            }
        }

        // All adjacent covered: smallest |score diff| among a sample of 64 unused.
        Self::pick_near_tie(state, &scores, rng)
    }

    fn pick_near_tie(
        state: &SimState,
        scores: &[f64],
        rng: &mut SmallRng,
    ) -> Option<(usize, usize)> {
        let n = state.n();
        let mut unused: Vec<(usize, usize)> = Vec::new();
        for i in 0..n {
            for j in (i + 1)..n {
                if !state.compared(i, j) {
                    unused.push((i, j));
                }
            }
        }
        if unused.is_empty() {
            return None;
        }
        unused.shuffle(rng);
        let take = unused.len().min(NEAR_TIE_SAMPLE);
        let mut best_pair = unused[0];
        let mut best_diff = (scores[best_pair.0] - scores[best_pair.1]).abs();
        for &(i, j) in &unused[1..take] {
            let diff = (scores[i] - scores[j]).abs();
            if diff < best_diff {
                best_diff = diff;
                best_pair = (i, j);
            }
        }
        Some(best_pair)
    }
}

impl PairStrategy for ExploreRefine {
    fn name(&self) -> &'static str {
        "explore_refine"
    }

    fn next_pair(&mut self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        if self.n < 2 {
            return None;
        }

        let max_pairs = self.n * (self.n - 1) / 2;
        if state.comparisons_made() >= max_pairs {
            return None;
        }

        // Phase A: emit up to EXPLORE_ROUNDS random near-perfect matchings.
        while self.explore_rounds_done < EXPLORE_ROUNDS {
            while let Some((i, j)) = self.queue.pop() {
                if !state.compared(i, j) {
                    return Some((i, j));
                }
            }
            self.refill_explore(state, rng);
            self.explore_rounds_done += 1;
            // If this round queued nothing, loop to the next explore round.
        }

        // Drain any leftover from the final explore round.
        while let Some((i, j)) = self.queue.pop() {
            if !state.compared(i, j) {
                return Some((i, j));
            }
        }

        // Phase B: payout-aware refine on the current ranking.
        self.refine(state, rng)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{SimState, VoteRec};
    use rand::SeedableRng;
    use std::collections::HashSet;

    fn mark(state: &mut SimState, i: usize, j: usize) {
        state.push_vote(VoteRec {
            winner: i,
            loser: j,
            wr: 2.0,
            lr: 1.0,
        });
    }

    /// Test-only: skip explore and enter refine immediately.
    fn new_in_refine(n: usize) -> ExploreRefine {
        ExploreRefine {
            n,
            explore_rounds_done: EXPLORE_ROUNDS,
            queue: Vec::new(),
        }
    }

    #[test]
    fn phase_a_three_rounds_disjoint_no_reuse() {
        let n = 12;
        let mut state = SimState::new(n, vec![0; n], 1);
        let mut strat = ExploreRefine::new(n);
        let mut rng = SmallRng::seed_from_u64(42);

        let per_round = n / 2;
        let mut all: HashSet<(usize, usize)> = HashSet::new();

        for _round in 0..EXPLORE_ROUNDS {
            let mut seen = HashSet::new();
            for _ in 0..per_round {
                let (i, j) = strat
                    .next_pair(&state, &mut rng)
                    .expect("explore round should yield floor(N/2) pairs");
                let pair = (i.min(j), i.max(j));
                assert!(seen.insert(i), "commit {i} twice in one round");
                assert!(seen.insert(j), "commit {j} twice in one round");
                assert!(all.insert(pair), "pair {pair:?} reused across rounds");
                mark(&mut state, i, j);
            }
            assert_eq!(seen.len(), n);
        }
        assert_eq!(all.len(), EXPLORE_ROUNDS * per_round);
    }

    #[test]
    fn phase_b_prefers_cross_author_adjacent() {
        // Ranking will be 0 ≻ 1 ≻ 2 ≻ 3 ≻ 4 ≻ 5.
        // Adjacent (0,1) same author; (1,2) cross-author — both uncovered.
        let n = 6;
        let authors = vec![0, 0, 1, 1, 2, 2];
        let mut state = SimState::new(n, authors.clone(), 3);

        // Chain wins to establish a clear total order without covering
        // the critical adjacent pairs (0,1) and (1,2).
        for i in 0..n {
            for j in (i + 1)..n {
                if (i, j) == (0, 1) || (i, j) == (1, 2) {
                    continue;
                }
                state.push_vote(VoteRec {
                    winner: i,
                    loser: j,
                    wr: 3.0,
                    lr: 1.0,
                });
            }
        }

        let ranking = state.ranking();
        assert_eq!(
            ranking,
            vec![0, 1, 2, 3, 4, 5],
            "expected total order 0>1>2>3>4>5, got {ranking:?}"
        );
        assert!(!state.compared(0, 1));
        assert!(!state.compared(1, 2));
        assert_eq!(authors[0], authors[1]);
        assert_ne!(authors[1], authors[2]);

        let mut strat = new_in_refine(n);
        let mut rng = SmallRng::seed_from_u64(1);
        let (a, b) = strat
            .next_pair(&state, &mut rng)
            .expect("refine should propose a pair");
        let pair = (a.min(b), a.max(b));
        assert_eq!(
            pair,
            (1, 2),
            "should skip same-author (0,1) and pick cross-author (1,2), got {pair:?}"
        );
    }

    #[test]
    fn never_proposes_already_compared_pair() {
        let n = 10;
        let mut state = SimState::new(n, (0..n).map(|i| i % 3).collect(), 3);
        let mut strat = ExploreRefine::new(n);
        let mut rng = SmallRng::seed_from_u64(7);
        let max_pairs = n * (n - 1) / 2;

        for _ in 0..max_pairs {
            let Some((i, j)) = strat.next_pair(&state, &mut rng) else {
                break;
            };
            assert!(
                !state.compared(i, j),
                "proposed already-compared pair ({i},{j})"
            );
            mark(&mut state, i, j);
        }
        assert_eq!(state.comparisons_made(), max_pairs);
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }

    #[test]
    fn edge_n_equals_1() {
        let state = SimState::new(1, vec![0], 1);
        let mut strat = ExploreRefine::new(1);
        let mut rng = SmallRng::seed_from_u64(0);
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }

    #[test]
    fn edge_n_equals_2() {
        let mut state = SimState::new(2, vec![0, 1], 2);
        let mut strat = ExploreRefine::new(2);
        let mut rng = SmallRng::seed_from_u64(0);

        let (i, j) = strat
            .next_pair(&state, &mut rng)
            .expect("N=2 should propose the only pair");
        assert_eq!((i.min(j), i.max(j)), (0, 1));
        mark(&mut state, i, j);

        // After the sole pair is used, further proposals must be None
        // (explore rounds 2–3 find nothing; refine finds nothing).
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }
}
