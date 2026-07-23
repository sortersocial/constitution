//! Random near-perfect matching rounds: each round is a random matching
//! on the N commits so every commit gets degree ≈ r after r rounds
//! (near-regular / expander-like). Already-compared edges are repaired
//! by local partner swaps; unrepaired edges are dropped for the round.

use rand::rngs::SmallRng;
use rand::seq::SliceRandom;
use rand::Rng;

use crate::state::SimState;
use crate::strategies::PairStrategy;

pub struct RandomMatching {
    n: usize,
    /// Remaining pairs from the current matching round (unordered, i < j).
    queue: Vec<(usize, usize)>,
    /// Once a refill yields zero valid pairs, stop matching and scan.
    matching_exhausted: bool,
}

impl RandomMatching {
    pub fn new(n: usize) -> Self {
        Self {
            n,
            queue: Vec::new(),
            matching_exhausted: false,
        }
    }

    fn norm(i: usize, j: usize) -> (usize, usize) {
        if i < j {
            (i, j)
        } else {
            (j, i)
        }
    }

    /// Shuffle commits, pair consecutive entries (bye if N odd), then
    /// locally repair already-compared edges via partner swaps among
    /// conflicting pairs. Returns how many valid pairs were queued.
    fn refill(&mut self, state: &SimState, rng: &mut SmallRng) -> usize {
        let mut nodes: Vec<usize> = (0..self.n).collect();
        nodes.shuffle(rng);

        let mut matching: Vec<(usize, usize)> = Vec::with_capacity(self.n / 2);
        let mut k = 0;
        while k + 1 < nodes.len() {
            matching.push(Self::norm(nodes[k], nodes[k + 1]));
            k += 2;
        }
        // Odd N: nodes.last() sits out this round.

        // Repair: swap partners between conflicting edges a few times.
        const REPAIR_ROUNDS: usize = 6;
        const SWAP_TRIES: usize = 8;
        for _ in 0..REPAIR_ROUNDS {
            let conflict_idxs: Vec<usize> = matching
                .iter()
                .enumerate()
                .filter(|(_, &(i, j))| state.compared(i, j))
                .map(|(idx, _)| idx)
                .collect();
            if conflict_idxs.len() < 2 {
                break;
            }

            let mut progressed = false;
            for &ci in &conflict_idxs {
                if !state.compared(matching[ci].0, matching[ci].1) {
                    continue;
                }
                for _ in 0..SWAP_TRIES {
                    let oj = conflict_idxs[rng.gen_range(0..conflict_idxs.len())];
                    if oj == ci {
                        continue;
                    }
                    // Other endpoint may already have been fixed.
                    if !state.compared(matching[oj].0, matching[oj].1)
                        && !state.compared(matching[ci].0, matching[ci].1)
                    {
                        break;
                    }

                    let (a, b) = matching[ci];
                    let (c, d) = matching[oj];
                    let cur = (state.compared(a, b) as u8) + (state.compared(c, d) as u8);

                    // Two alternate perfect matchings on {a,b,c,d}.
                    let alts = [
                        (Self::norm(a, c), Self::norm(b, d)),
                        (Self::norm(a, d), Self::norm(b, c)),
                    ];
                    // Randomize which alternate we try first.
                    let order = if rng.gen_bool(0.5) {
                        [0usize, 1]
                    } else {
                        [1, 0]
                    };
                    for &ai in &order {
                        let (p1, p2) = alts[ai];
                        // Degenerate if a swap collapses to a self-loop (shouldn't).
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

        // Drop unrepaired conflicts — those commits rest this round.
        self.queue = matching
            .into_iter()
            .filter(|&(i, j)| !state.compared(i, j))
            .collect();
        self.queue.shuffle(rng);
        self.queue.len()
    }

    fn scan_unused(n: usize, state: &SimState) -> Option<(usize, usize)> {
        for i in 0..n {
            for j in (i + 1)..n {
                if !state.compared(i, j) {
                    return Some((i, j));
                }
            }
        }
        None
    }
}

impl PairStrategy for RandomMatching {
    fn name(&self) -> &'static str {
        "random_matching"
    }

    fn next_pair(&mut self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        if self.n < 2 {
            return None;
        }

        let max_pairs = self.n * (self.n - 1) / 2;
        if state.comparisons_made() >= max_pairs {
            return None;
        }

        if self.matching_exhausted {
            return Self::scan_unused(self.n, state);
        }

        loop {
            while let Some((i, j)) = self.queue.pop() {
                if !state.compared(i, j) {
                    return Some((i, j));
                }
            }

            if self.refill(state, rng) == 0 {
                // A whole matching round found nothing usable.
                self.matching_exhausted = true;
                return Self::scan_unused(self.n, state);
            }
        }
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

    #[test]
    fn first_round_is_near_perfect_matching() {
        let n = 10;
        let mut state = SimState::new(n, vec![0; n], 1);
        let mut strat = RandomMatching::new(n);
        let mut rng = SmallRng::seed_from_u64(42);

        let expect = n / 2;
        let mut pairs = Vec::new();
        let mut seen = HashSet::new();
        for _ in 0..expect {
            let (i, j) = strat
                .next_pair(&state, &mut rng)
                .expect("first round should yield floor(N/2) pairs");
            assert!(i < j);
            assert!(seen.insert(i), "commit {i} appeared twice in round");
            assert!(seen.insert(j), "commit {j} appeared twice in round");
            pairs.push((i, j));
            mark(&mut state, i, j);
        }
        assert_eq!(pairs.len(), expect);
        assert_eq!(seen.len(), n); // even N: every commit paired
        // Next call starts a new round (queue empty); should still succeed.
        assert!(strat.next_pair(&state, &mut rng).is_some());
    }

    #[test]
    fn never_proposes_already_compared_across_rounds() {
        let n = 8;
        let mut state = SimState::new(n, vec![0; n], 1);
        let mut strat = RandomMatching::new(n);
        let mut rng = SmallRng::seed_from_u64(7);

        // At least 3 full matching rounds worth of pairs.
        let target = 3 * (n / 2);
        for _ in 0..target {
            let (i, j) = strat
                .next_pair(&state, &mut rng)
                .expect("should propose while unused pairs remain");
            assert!(
                !state.compared(i, j),
                "proposed already-compared pair ({i},{j})"
            );
            mark(&mut state, i, j);
        }
        assert_eq!(state.comparisons_made(), target);
    }

    #[test]
    fn odd_n_first_round_has_bye() {
        let n = 7;
        let mut state = SimState::new(n, vec![0; n], 1);
        let mut strat = RandomMatching::new(n);
        let mut rng = SmallRng::seed_from_u64(99);

        let expect = n / 2; // floor
        let mut seen = HashSet::new();
        for _ in 0..expect {
            let (i, j) = strat
                .next_pair(&state, &mut rng)
                .expect("odd-N first round should yield floor(N/2) pairs");
            assert!(seen.insert(i));
            assert!(seen.insert(j));
            mark(&mut state, i, j);
        }
        assert_eq!(seen.len(), expect * 2);
        assert_eq!(seen.len(), n - 1); // exactly one bye
    }

    #[test]
    fn returns_none_when_complete() {
        let n = 4;
        let mut state = SimState::new(n, vec![0; n], 1);
        let mut strat = RandomMatching::new(n);
        let mut rng = SmallRng::seed_from_u64(1);
        let max = n * (n - 1) / 2;
        for _ in 0..max {
            let (i, j) = strat.next_pair(&state, &mut rng).unwrap();
            assert!(!state.compared(i, j));
            mark(&mut state, i, j);
        }
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }
}
