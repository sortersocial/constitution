//! Swiss-tournament pair selection: bootstrap with a random near-perfect
//! matching, then each round pair near-adjacent items in the current ranking.

use std::collections::VecDeque;

use rand::rngs::SmallRng;
use rand::seq::SliceRandom;
use rand::Rng;

use crate::state::SimState;
use crate::strategies::PairStrategy;

pub struct Swiss {
    n: usize,
    /// Pairs remaining in the current round (FIFO).
    queue: VecDeque<(usize, usize)>,
    /// False until the bootstrap (round 0) matching has been built.
    bootstrapped: bool,
}

impl Swiss {
    pub fn new(n: usize) -> Self {
        Self {
            n,
            queue: VecDeque::new(),
            bootstrapped: false,
        }
    }

    fn refill(&mut self, state: &SimState, rng: &mut SmallRng) {
        debug_assert!(self.queue.is_empty());

        if !self.bootstrapped {
            self.bootstrapped = true;
            let mut idx: Vec<usize> = (0..self.n).collect();
            idx.shuffle(rng);
            for pair in idx.chunks_exact(2) {
                let (a, b) = (pair[0], pair[1]);
                if !state.compared(a, b) {
                    self.queue.push_back((a, b));
                }
            }
            // Odd N: last element is a bye (chunks_exact drops it).
            if self.queue.is_empty() {
                self.push_any_unused(state);
            }
            return;
        }

        let ranking = state.ranking();
        let mut paired = vec![false; self.n];

        for i in 0..ranking.len() {
            let a = ranking[i];
            if paired[a] {
                continue;
            }
            let mut partner = None;
            for off in 1..=8 {
                let j = i + off;
                if j >= ranking.len() {
                    break;
                }
                let b = ranking[j];
                if paired[b] {
                    continue;
                }
                if !state.compared(a, b) {
                    partner = Some(b);
                    break;
                }
            }
            if let Some(b) = partner {
                paired[a] = true;
                paired[b] = true;
                self.queue.push_back((a, b));
            }
        }

        // Leftovers: pair uniformly at random among themselves.
        let mut leftover: Vec<usize> = (0..self.n).filter(|&x| !paired[x]).collect();
        leftover.shuffle(rng);
        let mut taken = vec![false; leftover.len()];
        for i in 0..leftover.len() {
            if taken[i] {
                continue;
            }
            let mut candidates: Vec<usize> = (i + 1..leftover.len())
                .filter(|&j| !taken[j] && !state.compared(leftover[i], leftover[j]))
                .collect();
            if candidates.is_empty() {
                continue;
            }
            let pick = candidates.swap_remove(rng.gen_range(0..candidates.len()));
            taken[i] = true;
            taken[pick] = true;
            self.queue.push_back((leftover[i], leftover[pick]));
        }

        if self.queue.is_empty() {
            self.push_any_unused(state);
        }
    }

    /// Late-round fallback: queue one unused pair, if any remain.
    fn push_any_unused(&mut self, state: &SimState) {
        for i in 0..self.n {
            for j in (i + 1)..self.n {
                if !state.compared(i, j) {
                    self.queue.push_back((i, j));
                    return;
                }
            }
        }
    }
}

impl PairStrategy for Swiss {
    fn name(&self) -> &'static str {
        "swiss"
    }

    fn next_pair(&mut self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        if self.n < 2 {
            return None;
        }
        loop {
            while let Some((a, b)) = self.queue.pop_front() {
                if !state.compared(a, b) {
                    return Some((a, b));
                }
            }
            self.refill(state, rng);
            if self.queue.is_empty() {
                return None;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{SimState, VoteRec};
    use rand::SeedableRng;

    fn mark(state: &mut SimState, i: usize, j: usize) {
        state.push_vote(VoteRec {
            winner: i,
            loser: j,
            wr: 2.0,
            lr: 1.0,
        });
    }

    #[test]
    fn round0_emits_floor_n_over_2_disjoint_pairs() {
        for n in [2usize, 3, 10, 11, 50] {
            let mut swiss = Swiss::new(n);
            let state = SimState::new(n, vec![0; n], 1);
            let mut rng = SmallRng::seed_from_u64(7);
            let want = n / 2;
            let mut seen = vec![false; n];
            let mut pairs = Vec::with_capacity(want);
            for _ in 0..want {
                let (a, b) = swiss
                    .next_pair(&state, &mut rng)
                    .expect("round 0 should emit a pair");
                assert_ne!(a, b);
                assert!(!seen[a] && !seen[b], "pairs must be disjoint");
                seen[a] = true;
                seen[b] = true;
                pairs.push((a, b));
            }
            assert_eq!(pairs.len(), want);
            // Odd N: exactly one bye.
            let matched = seen.iter().filter(|&&x| x).count();
            assert_eq!(matched, want * 2);
            if n % 2 == 1 {
                assert_eq!(seen.iter().filter(|&&x| !x).count(), 1);
            }
        }
    }

    #[test]
    fn never_proposes_an_already_compared_pair() {
        let n = 12;
        let mut swiss = Swiss::new(n);
        let mut state = SimState::new(n, vec![0; n], 1);
        let mut rng = SmallRng::seed_from_u64(99);
        let max_pairs = n * (n - 1) / 2;
        let mut proposed = 0;
        while proposed < max_pairs {
            let Some((a, b)) = swiss.next_pair(&state, &mut rng) else {
                break;
            };
            assert!(
                !state.compared(a, b),
                "proposed already-compared pair ({a},{b})"
            );
            mark(&mut state, a.min(b), a.max(b));
            proposed += 1;
        }
        assert_eq!(proposed, max_pairs);
        assert!(swiss.next_pair(&state, &mut rng).is_none());
    }

    #[test]
    fn later_round_pairs_are_near_adjacent_in_ranking() {
        let n = 16;
        let mut swiss = Swiss::new(n);
        let mut state = SimState::new(n, (0..n).collect(), n);
        let mut rng = SmallRng::seed_from_u64(123);

        // Drain bootstrap round; stronger (lower) index wins each match.
        for _ in 0..(n / 2) {
            let (a, b) = swiss.next_pair(&state, &mut rng).unwrap();
            let (w, l) = if a < b { (a, b) } else { (b, a) };
            mark(&mut state, w, l);
        }

        // Reinforce a clear total order 0 ≻ 1 ≻ … ≻ n-1 via the chain.
        for i in 0..n - 1 {
            state.push_vote(VoteRec {
                winner: i,
                loser: i + 1,
                wr: 5.0,
                lr: 1.0,
            });
        }

        let ranking = state.ranking();
        let mut pos = vec![0usize; n];
        for (p, &idx) in ranking.iter().enumerate() {
            pos[idx] = p;
        }

        // Block far pairs so leftover matching cannot violate the window.
        for i in 0..n {
            for j in (i + 1)..n {
                if pos[i].abs_diff(pos[j]) > 8 && !state.compared(i, j) {
                    mark(&mut state, i.min(j), i.max(j));
                }
            }
        }

        // Collect one Swiss round's worth of proposals (until queue would refill
        // after we mark them — grab until we have the round's pairs).
        let mut round_pairs = Vec::new();
        let round_target = (n / 2).max(1);
        for _ in 0..round_target {
            let Some((a, b)) = swiss.next_pair(&state, &mut rng) else {
                break;
            };
            assert!(!state.compared(a, b));
            let d = pos[a].abs_diff(pos[b]);
            assert!(
                d <= 8,
                "pair ({a},{b}) has ranking distance {d} > 8 (pos {} vs {})",
                pos[a],
                pos[b]
            );
            round_pairs.push((a, b));
            mark(&mut state, a, b);
        }
        assert!(
            !round_pairs.is_empty(),
            "expected at least one near-adjacent pair in the later round"
        );
    }
}
