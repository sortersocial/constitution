//! Bootstrap inversion targeting: cover every item with random matchings,
//! then queue pairs by vote-bootstrap inversion probability weighted by
//! score gap and cross-author payout relevance.

use rand::rngs::SmallRng;
use rand::seq::SliceRandom;
use rand::Rng;

use crate::rank::rank_scores;
use crate::state::SimState;
use crate::strategies::PairStrategy;

const BOOTSTRAP_M: usize = 20;
const QUEUE_BATCH: usize = 8;
const RANDOM_COVERAGE: usize = 32;
const NEAR_WINDOW: usize = 3;
const WIDE_WINDOW: usize = 8;

pub struct BootstrapInversion {
    n: usize,
    /// Pending pairs to emit (bootstrap matching or refinement batch).
    queue: Vec<(usize, usize)>,
}

impl BootstrapInversion {
    pub fn new(n: usize) -> Self {
        Self {
            n,
            queue: Vec::new(),
        }
    }

    fn all_items_covered(state: &SimState) -> bool {
        let n = state.n();
        if n == 0 {
            return true;
        }
        let mut seen = vec![false; n];
        for (i, j) in state.compared_pairs() {
            seen[i] = true;
            seen[j] = true;
        }
        seen.iter().all(|&s| s)
    }

    fn any_unused(state: &SimState) -> bool {
        let n = state.n();
        for i in 0..n {
            for j in (i + 1)..n {
                if !state.compared(i, j) {
                    return true;
                }
            }
        }
        false
    }

    fn fill_matching_queue(&mut self, state: &SimState, rng: &mut SmallRng) {
        // A few shuffle attempts; used pairs are skipped (odd N gets a bye).
        for _ in 0..16 {
            let mut items: Vec<usize> = (0..self.n).collect();
            items.shuffle(rng);
            let mut added = 0;
            let mut k = 0;
            while k + 1 < items.len() {
                let a = items[k];
                let b = items[k + 1];
                if !state.compared(a, b) {
                    self.queue.push((a, b));
                    added += 1;
                }
                k += 2;
            }
            if added > 0 {
                return;
            }
        }
        // Fallback: pair uncovered items with any unused partner.
        let mut covered = vec![false; self.n];
        for (i, j) in state.compared_pairs() {
            covered[i] = true;
            covered[j] = true;
        }
        let mut uncovered: Vec<usize> = (0..self.n).filter(|&i| !covered[i]).collect();
        uncovered.shuffle(rng);
        for &a in &uncovered {
            let mut partners: Vec<usize> = (0..self.n)
                .filter(|&b| b != a && !state.compared(a, b))
                .collect();
            if partners.is_empty() {
                continue;
            }
            partners.shuffle(rng);
            let b = partners[0];
            // Avoid duplicate unordered pairs in this fill.
            let key = if a < b { (a, b) } else { (b, a) };
            if !self.queue.iter().any(|&(x, y)| {
                let q = if x < y { (x, y) } else { (y, x) };
                q == key
            }) {
                self.queue.push((a, b));
            }
        }
    }

    fn collect_near_adjacent(state: &SimState, pos: &[usize], window: usize) -> Vec<(usize, usize)> {
        let n = state.n();
        let mut out = Vec::new();
        for a in 0..n {
            for b in (a + 1)..n {
                if state.compared(a, b) {
                    continue;
                }
                let d = pos[a].abs_diff(pos[b]);
                if d > 0 && d <= window {
                    out.push((a, b));
                }
            }
        }
        out
    }

    fn all_unused_pairs(state: &SimState) -> Vec<(usize, usize)> {
        let n = state.n();
        let mut out = Vec::new();
        for a in 0..n {
            for b in (a + 1)..n {
                if !state.compared(a, b) {
                    out.push((a, b));
                }
            }
        }
        out
    }

    fn build_candidates(state: &SimState, pos: &[usize], rng: &mut SmallRng) -> Vec<(usize, usize)> {
        let mut cands = Self::collect_near_adjacent(state, pos, NEAR_WINDOW);
        if cands.is_empty() {
            cands = Self::collect_near_adjacent(state, pos, WIDE_WINDOW);
        }
        if cands.is_empty() {
            return Self::all_unused_pairs(state);
        }

        // Coverage: up to 32 random unused pairs not already selected.
        let mut unused = Self::all_unused_pairs(state);
        unused.shuffle(rng);
        let mut added = 0;
        for (a, b) in unused {
            if added >= RANDOM_COVERAGE {
                break;
            }
            if cands.iter().any(|&(x, y)| x == a && y == b) {
                continue;
            }
            cands.push((a, b));
            added += 1;
        }
        cands
    }

    fn fill_refinement_queue(&mut self, state: &SimState, rng: &mut SmallRng) {
        let n = self.n;
        let votes = state.votes();
        if votes.is_empty() || !Self::any_unused(state) {
            return;
        }

        let full_scores = state.scores();
        let ranking = state.ranking();
        let mut pos = vec![0usize; n];
        for (p, &item) in ranking.iter().enumerate() {
            pos[item] = p;
        }

        let candidates = Self::build_candidates(state, &pos, rng);
        if candidates.is_empty() {
            return;
        }

        // M vote-level bootstrap resamples.
        let m_votes = votes.len();
        let mut boot_scores: Vec<Vec<f64>> = Vec::with_capacity(BOOTSTRAP_M);
        for _ in 0..BOOTSTRAP_M {
            let resampled = (0..m_votes).map(|_| votes[rng.gen_range(0..m_votes)]);
            boot_scores.push(rank_scores(n, resampled));
        }

        let authors = state.authors();
        let mut scored: Vec<(f64, usize, usize)> = Vec::with_capacity(candidates.len());
        for &(a, b) in &candidates {
            let full_a_ahead = full_scores[a] >= full_scores[b];
            let mut inversions = 0usize;
            for bs in &boot_scores {
                let boot_a_ahead = bs[a] >= bs[b];
                if boot_a_ahead != full_a_ahead {
                    inversions += 1;
                }
            }
            let q = inversions as f64 / BOOTSTRAP_M as f64;
            let gap = (full_scores[a] - full_scores[b]).abs() + 1e-9;
            let w_author = if authors[a] != authors[b] { 1.0 } else { 0.05 };
            let acq = q * gap * w_author;
            scored.push((acq, a, b));
        }

        scored.sort_by(|x, y| y.0.partial_cmp(&x.0).unwrap_or(std::cmp::Ordering::Equal));
        self.queue.clear();
        // Push worst→best so `pop` emits highest acquisition first.
        for &(_, a, b) in scored.iter().take(QUEUE_BATCH).rev() {
            self.queue.push((a, b));
        }
    }

    fn pop_valid(&mut self, state: &SimState) -> Option<(usize, usize)> {
        while let Some((i, j)) = self.queue.pop() {
            if i != j && !state.compared(i, j) {
                return Some((i, j));
            }
        }
        None
    }
}

impl PairStrategy for BootstrapInversion {
    fn name(&self) -> &'static str {
        "bootstrap_inversion"
    }

    fn next_pair(&mut self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        if self.n < 2 {
            return None;
        }
        if !Self::any_unused(state) {
            return None;
        }

        if let Some(p) = self.pop_valid(state) {
            return Some(p);
        }

        if !Self::all_items_covered(state) {
            self.fill_matching_queue(state, rng);
            if let Some(p) = self.pop_valid(state) {
                return Some(p);
            }
            // Still uncovered but matching found nothing usable.
            return None;
        }

        self.fill_refinement_queue(state, rng);
        self.pop_valid(state)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::VoteRec;
    use rand::SeedableRng;

    fn push(state: &mut SimState, winner: usize, loser: usize) {
        state.push_vote(VoteRec {
            winner,
            loser,
            wr: 2.0,
            lr: 1.0,
        });
    }

    fn covered(state: &SimState) -> bool {
        BootstrapInversion::all_items_covered(state)
    }

    #[test]
    fn bootstrap_phase_covers_all_items_first() {
        let n = 6;
        let authors: Vec<usize> = (0..n).collect();
        let mut state = SimState::new(n, authors, n);
        let mut strat = BootstrapInversion::new(n);
        let mut rng = SmallRng::seed_from_u64(7);

        // First matching should cover every item (perfect matching of 3 pairs).
        let mut seen = vec![false; n];
        for _ in 0..3 {
            assert!(!covered(&state));
            let (i, j) = strat
                .next_pair(&state, &mut rng)
                .expect("bootstrap should propose");
            assert!(!state.compared(i, j));
            assert!(!seen[i] && !seen[j], "matching pairs must be disjoint");
            seen[i] = true;
            seen[j] = true;
            push(&mut state, i, j);
        }
        assert!(seen.iter().all(|&s| s));
        assert!(covered(&state));
    }

    #[test]
    fn never_proposes_used_pair() {
        let n = 8;
        let authors: Vec<usize> = (0..n).map(|i| i % 3).collect();
        let mut state = SimState::new(n, authors, 3);
        let mut strat = BootstrapInversion::new(n);
        let mut rng = SmallRng::seed_from_u64(99);

        for _ in 0..40 {
            let Some((i, j)) = strat.next_pair(&state, &mut rng) else {
                break;
            };
            assert!(i != j);
            assert!(
                !state.compared(i, j),
                "proposed already-compared pair ({i},{j})"
            );
            push(&mut state, i, j);
        }
    }

    #[test]
    fn refinement_prefers_cross_author_near_ties() {
        // Item 0 dominates; leaves 1..8 are near-tied on a star. Authors:
        // 1..4 share author 0 (same-author pairs are payout-irrelevant);
        // 5..8 have distinct authors (cross-author pairs move the payout).
        let n = 9;
        let authors = vec![0, 0, 0, 0, 0, 1, 2, 3, 4];
        let mut state = SimState::new(n, authors, 5);
        for loser in 1..n {
            for _ in 0..4 {
                push(&mut state, 0, loser);
            }
        }
        assert!(covered(&state));

        let mut strat = BootstrapInversion::new(n);
        let mut rng = SmallRng::seed_from_u64(123);

        let mut cross = 0usize;
        let mut same = 0usize;
        let mut proposed = Vec::new();
        for _ in 0..QUEUE_BATCH {
            let (i, j) = strat
                .next_pair(&state, &mut rng)
                .expect("refinement should propose while unused pairs remain");
            assert!(!state.compared(i, j));
            proposed.push((i, j));
            if state.authors()[i] == state.authors()[j] {
                same += 1;
            } else {
                cross += 1;
            }
            push(&mut state, i, j);
        }
        assert_eq!(proposed.len(), QUEUE_BATCH);
        assert!(
            cross > same,
            "expected cross-author preference, got cross={cross} same={same} pairs={proposed:?}"
        );
        assert!(
            same <= 2,
            "same-author pairs should be down-weighted: {proposed:?}"
        );
    }

    #[test]
    fn edge_n1_returns_none() {
        let mut state = SimState::new(1, vec![0], 1);
        let mut strat = BootstrapInversion::new(1);
        let mut rng = SmallRng::seed_from_u64(1);
        assert!(strat.next_pair(&state, &mut rng).is_none());
        let _ = &mut state;
    }

    #[test]
    fn edge_n2_single_pair_then_none() {
        let mut state = SimState::new(2, vec![0, 1], 2);
        let mut strat = BootstrapInversion::new(2);
        let mut rng = SmallRng::seed_from_u64(2);
        let (i, j) = strat.next_pair(&state, &mut rng).expect("one pair");
        assert!((i == 0 && j == 1) || (i == 1 && j == 0));
        push(&mut state, i, j);
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }
}
