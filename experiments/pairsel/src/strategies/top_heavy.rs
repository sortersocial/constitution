//! Score-mass-weighted refinement: coverage floor of random matchings,
//! then spend most comparisons inside the top score-mass band.

use rand::rngs::SmallRng;
use rand::seq::SliceRandom;
use rand::Rng;

use crate::state::SimState;
use crate::strategies::PairStrategy;

pub struct TopHeavy {
    n: usize,
    /// Queued pairs from the coverage-floor matchings.
    coverage_queue: Vec<(usize, usize)>,
    /// Number of matching rounds already generated (target: 2).
    coverage_rounds: u8,
}

impl TopHeavy {
    pub fn new(n: usize) -> Self {
        Self {
            n,
            coverage_queue: Vec::new(),
            coverage_rounds: 0,
        }
    }

    fn build_matching_round(&mut self, state: &SimState, rng: &mut SmallRng) {
        let target = self.n / 2;
        let mut best: Vec<(usize, usize)> = Vec::new();
        // Retry shuffles so each coverage round is a near-perfect matching
        // of still-unused pairs when possible (odd-N: one bye).
        for _ in 0..64 {
            let mut perm: Vec<usize> = (0..self.n).collect();
            perm.shuffle(rng);
            let mut pairs = Vec::with_capacity(target);
            let mut i = 0;
            while i + 1 < perm.len() {
                let a = perm[i];
                let b = perm[i + 1];
                if !state.compared(a, b) {
                    pairs.push((a, b));
                }
                i += 2;
            }
            if pairs.len() > best.len() {
                best = pairs;
            }
            if best.len() == target {
                break;
            }
        }
        self.coverage_queue.extend(best);
        self.coverage_rounds += 1;
    }

    fn next_coverage_pair(
        &mut self,
        state: &SimState,
        rng: &mut SmallRng,
    ) -> Option<(usize, usize)> {
        while self.coverage_rounds < 2 {
            if let Some((a, b)) = self.coverage_queue.pop() {
                if !state.compared(a, b) {
                    return Some((a, b));
                }
                continue;
            }
            self.build_matching_round(state, rng);
        }
        while let Some((a, b)) = self.coverage_queue.pop() {
            if !state.compared(a, b) {
                return Some((a, b));
            }
        }
        None
    }

    fn top_band_size_of(ranking: &[usize], scores: &[f64]) -> usize {
        let n = ranking.len();
        if n == 0 {
            return 0;
        }
        let total: f64 = scores.iter().sum();
        let target = 0.75 * total;
        let mut cum = 0.0;
        let mut m = n;
        for (k, &idx) in ranking.iter().enumerate() {
            cum += scores[idx];
            if cum >= target {
                m = k + 1;
                break;
            }
        }
        let lo = 8.min(n);
        m.clamp(lo, n)
    }

    fn propose_band_pair(
        ranking: &[usize],
        scores: &[f64],
        m: usize,
        state: &SimState,
        rng: &mut SmallRng,
    ) -> Option<(usize, usize)> {
        if m < 2 {
            return None;
        }
        // Prefer uncovered adjacent band pairs with largest score product.
        let mut best: Option<(usize, usize, f64)> = None;
        for p in 0..(m - 1) {
            let a = ranking[p];
            let b = ranking[p + 1];
            if state.compared(a, b) {
                continue;
            }
            let product = scores[a] * scores[b];
            match best {
                None => best = Some((a, b, product)),
                Some((_, _, bp)) if product > bp => best = Some((a, b, product)),
                _ => {}
            }
        }
        if let Some((a, b, _)) = best {
            return Some((a, b));
        }
        // All band-adjacent covered: sample up to 64 unused band pairs,
        // pick smallest absolute score difference.
        let mut best_diff: Option<(usize, usize, f64)> = None;
        for _ in 0..64 {
            let i = rng.gen_range(0..m);
            let j = rng.gen_range(0..m);
            if i == j {
                continue;
            }
            let a = ranking[i];
            let b = ranking[j];
            if state.compared(a, b) {
                continue;
            }
            let diff = (scores[a] - scores[b]).abs();
            match best_diff {
                None => best_diff = Some((a, b, diff)),
                Some((_, _, bd)) if diff < bd => best_diff = Some((a, b, diff)),
                _ => {}
            }
        }
        best_diff.map(|(a, b, _)| (a, b))
    }

    fn comparison_degrees(state: &SimState, n: usize) -> Vec<u32> {
        let mut deg = vec![0u32; n];
        for (i, j) in state.compared_pairs() {
            deg[i] += 1;
            deg[j] += 1;
        }
        deg
    }

    fn propose_coverage_pair(
        state: &SimState,
        n: usize,
        rng: &mut SmallRng,
    ) -> Option<(usize, usize)> {
        let deg = Self::comparison_degrees(state, n);
        let mut best: Option<(usize, usize, u32)> = None;
        for _ in 0..64 {
            let i = rng.gen_range(0..n);
            let j = rng.gen_range(0..n);
            if i == j || state.compared(i, j) {
                continue;
            }
            let dsum = deg[i] + deg[j];
            match best {
                None => best = Some((i, j, dsum)),
                Some((_, _, bd)) if dsum < bd => best = Some((i, j, dsum)),
                _ => {}
            }
        }
        best.map(|(i, j, _)| (i, j))
    }

    fn any_unused(state: &SimState, n: usize, rng: &mut SmallRng) -> Option<(usize, usize)> {
        for _ in 0..400 {
            let i = rng.gen_range(0..n);
            let j = rng.gen_range(0..n);
            if i != j && !state.compared(i, j) {
                return Some((i, j));
            }
        }
        // Exhaustive fallback for small N.
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

impl PairStrategy for TopHeavy {
    fn name(&self) -> &'static str {
        "top_heavy"
    }

    fn next_pair(&mut self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        if self.n < 2 {
            return None;
        }

        // Coverage floor: 2 rounds of random near-perfect matchings.
        if self.coverage_rounds < 2 || !self.coverage_queue.is_empty() {
            if let Some(p) = self.next_coverage_pair(state, rng) {
                return Some(p);
            }
        }

        if state.comparisons_made() >= self.n * (self.n - 1) / 2 {
            return None;
        }

        let ranking = state.ranking();
        let scores = state.scores();
        let m = Self::top_band_size_of(&ranking, &scores);

        let pair = if rng.gen_bool(0.75) {
            Self::propose_band_pair(&ranking, &scores, m, state, rng)
                .or_else(|| Self::propose_coverage_pair(state, self.n, rng))
        } else {
            Self::propose_coverage_pair(state, self.n, rng)
                .or_else(|| Self::propose_band_pair(&ranking, &scores, m, state, rng))
        };

        pair.or_else(|| Self::any_unused(state, self.n, rng))
    }
}

#[cfg(test)]
mod tests {
    use rand::rngs::SmallRng;
    use rand::SeedableRng;

    use super::TopHeavy;
    use crate::state::{SimState, VoteRec};
    use crate::strategies::PairStrategy;

    fn authors(n: usize) -> Vec<usize> {
        (0..n).map(|i| i % 3).collect()
    }

    fn feed(state: &mut SimState, a: usize, b: usize) {
        state.push_vote(VoteRec {
            winner: a,
            loser: b,
            wr: 2.0,
            lr: 1.0,
        });
    }

    #[test]
    fn coverage_floor_two_rounds_disjoint() {
        let n = 10;
        let mut state = SimState::new(n, authors(n), 3);
        let mut strat = TopHeavy::new(n);
        let mut rng = SmallRng::seed_from_u64(42);

        let pairs_per_round = n / 2;
        for r in 0..2 {
            let mut round = Vec::with_capacity(pairs_per_round);
            for _ in 0..pairs_per_round {
                let (a, b) = strat.next_pair(&state, &mut rng).expect("coverage pair");
                assert_ne!(a, b);
                assert!(!state.compared(a, b), "round {r} reused a compared pair");
                round.push((a, b));
            }
            let mut seen = vec![false; n];
            for &(a, b) in &round {
                assert!(!seen[a] && !seen[b], "round {r} not a matching");
                seen[a] = true;
                seen[b] = true;
            }
            for (a, b) in round {
                feed(&mut state, a, b);
            }
        }
    }

    #[test]
    fn never_proposes_used_pair() {
        let n = 12;
        let mut state = SimState::new(n, authors(n), 3);
        let mut strat = TopHeavy::new(n);
        let mut rng = SmallRng::seed_from_u64(7);
        let max_pairs = n * (n - 1) / 2;
        for _ in 0..max_pairs {
            let Some((a, b)) = strat.next_pair(&state, &mut rng) else {
                break;
            };
            assert!(!state.compared(a, b), "proposed already-used pair");
            feed(&mut state, a, b);
        }
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }

    fn band_size(ranking: &[usize], scores: &[f64]) -> usize {
        let n = ranking.len();
        let total: f64 = scores.iter().sum();
        let mut cum = 0.0;
        let mut m = n;
        for (k, &idx) in ranking.iter().enumerate() {
            cum += scores[idx];
            if cum >= 0.75 * total {
                m = k + 1;
                break;
            }
        }
        m.clamp(8.min(n), n)
    }

    #[test]
    fn after_coverage_majority_touch_top_band() {
        let n = 20;
        let mut state = SimState::new(n, authors(n), 3);
        let mut strat = TopHeavy::new(n);
        let mut rng = SmallRng::seed_from_u64(99);

        // Drain coverage floor (2 * floor(n/2) pairs).
        let coverage = 2 * (n / 2);
        for _ in 0..coverage {
            let (a, b) = strat.next_pair(&state, &mut rng).unwrap();
            feed(&mut state, a, b);
        }

        // Skewed vote set: item 0 dominates every other item.
        for other in 1..n {
            if !state.compared(0, other) {
                feed(&mut state, 0, other);
            }
        }
        // Reinforce dominance so scores are heavy-tailed toward the top.
        for i in 1..n {
            for j in (i + 1)..n.min(i + 3) {
                if !state.compared(i, j) {
                    feed(&mut state, i, j);
                }
            }
        }

        let ranking = state.ranking();
        let scores = state.scores();
        let m = band_size(&ranking, &scores);
        let band: std::collections::HashSet<usize> =
            ranking[..m].iter().copied().collect();

        let mut touch = 0;
        for _ in 0..20 {
            let (a, b) = strat.next_pair(&state, &mut rng).expect("proposal");
            assert!(!state.compared(a, b));
            if band.contains(&a) && band.contains(&b) {
                touch += 1;
            }
            feed(&mut state, a, b);
        }
        assert!(
            touch > 10,
            "expected majority of proposals inside top band, got {touch}/20 (m={m})"
        );
    }

    #[test]
    fn n1_returns_none() {
        let state = SimState::new(1, vec![0], 1);
        let mut strat = TopHeavy::new(1);
        let mut rng = SmallRng::seed_from_u64(1);
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }

    #[test]
    fn n2_proposes_only_pair_then_none() {
        let mut state = SimState::new(2, vec![0, 1], 2);
        let mut strat = TopHeavy::new(2);
        let mut rng = SmallRng::seed_from_u64(2);
        let (a, b) = strat.next_pair(&state, &mut rng).unwrap();
        assert!((a == 0 && b == 1) || (a == 1 && b == 0));
        feed(&mut state, a, b);
        // Second coverage round may try the same pair (skip) then main loop → None.
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }
}
