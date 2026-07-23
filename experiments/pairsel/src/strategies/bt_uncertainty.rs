//! Bayesian Bradley-Terry with diagonal Laplace / D-optimal acquisition.
//!
//! Bootstrap with random near-perfect matchings until every item has been
//! compared at least once, then repeatedly fit a ridge MAP BT model and
//! pick unused pairs that maximize diagonal D-optimality gain.

use rand::rngs::SmallRng;
use rand::seq::SliceRandom;
use rand::Rng;

use crate::state::{SimState, VoteRec};
use crate::strategies::PairStrategy;

const TAU: f64 = 2.0;
const FIT_ITERS: usize = 30;

pub struct BtUncertainty {
    n: usize,
    /// Remaining pairs from the current bootstrap matching.
    matching_queue: Vec<(usize, usize)>,
    theta: Vec<f64>,
    sigma2: Vec<f64>,
    fitted: bool,
    selections_since_refit: usize,
    refit_interval: usize,
}

impl BtUncertainty {
    pub fn new(n: usize) -> Self {
        let refit_interval = (n / 8).max(8);
        Self {
            n,
            matching_queue: Vec::new(),
            theta: vec![0.0; n],
            sigma2: vec![TAU * TAU; n],
            fitted: false,
            selections_since_refit: 0,
            refit_interval,
        }
    }

    fn all_items_appeared(state: &SimState) -> bool {
        let n = state.n();
        if n == 0 {
            return true;
        }
        let mut seen = vec![false; n];
        for v in state.votes() {
            seen[v.winner] = true;
            seen[v.loser] = true;
        }
        seen.into_iter().all(|s| s)
    }

    fn refill_matching(&mut self, state: &SimState, rng: &mut SmallRng) {
        self.matching_queue.clear();
        let mut idxs: Vec<usize> = (0..self.n).collect();
        idxs.shuffle(rng);
        for chunk in idxs.chunks(2) {
            if chunk.len() == 2 {
                let (a, b) = (chunk[0], chunk[1]);
                if !state.compared(a, b) {
                    self.matching_queue.push((a, b));
                }
            }
        }
    }

    fn next_bootstrap_pair(
        &mut self,
        state: &SimState,
        rng: &mut SmallRng,
    ) -> Option<(usize, usize)> {
        for _ in 0..64 {
            while let Some((i, j)) = self.matching_queue.pop() {
                if !state.compared(i, j) {
                    return Some((i, j));
                }
            }
            self.refill_matching(state, rng);
            if self.matching_queue.is_empty() {
                // Matching produced only used pairs; try a direct unused pair.
                if let Some(p) = any_unused_pair(state, rng) {
                    return Some(p);
                }
                return None;
            }
        }
        any_unused_pair(state, rng)
    }

    fn refit(&mut self, votes: &[VoteRec]) {
        let inv_tau2 = 1.0 / (TAU * TAU);
        let n = self.n;

        // Warm-start: keep existing theta (zeros on first fit).
        for _ in 0..FIT_ITERS {
            let mut grad = vec![0.0; n];
            let mut hess = vec![inv_tau2; n];
            for i in 0..n {
                grad[i] = -self.theta[i] * inv_tau2;
            }
            for v in votes {
                let d = self.theta[v.winner] - self.theta[v.loser];
                let p = sigmoid(d);
                let w = p * (1.0 - p);
                // d loglik / d theta_w = 1 - p, / d theta_l = -(1 - p)
                grad[v.winner] += 1.0 - p;
                grad[v.loser] -= 1.0 - p;
                hess[v.winner] += w;
                hess[v.loser] += w;
            }
            for i in 0..n {
                let step = grad[i] / hess[i];
                if step.is_finite() {
                    self.theta[i] += step;
                }
            }
        }

        // Diagonal posterior variance from observed Fisher information.
        let mut info = vec![inv_tau2; n];
        for v in votes {
            let d = self.theta[v.winner] - self.theta[v.loser];
            let p = sigmoid(d);
            let w = p * (1.0 - p);
            info[v.winner] += w;
            info[v.loser] += w;
        }
        for i in 0..n {
            self.sigma2[i] = 1.0 / info[i];
            if !self.sigma2[i].is_finite() {
                self.sigma2[i] = TAU * TAU;
            }
        }
    }

    fn next_model_pair(
        &mut self,
        state: &SimState,
        rng: &mut SmallRng,
    ) -> Option<(usize, usize)> {
        if !self.fitted || self.selections_since_refit >= self.refit_interval {
            self.refit(state.votes());
            self.fitted = true;
            self.selections_since_refit = 0;
        }

        let candidates = sample_unused_pairs(state, rng, 4 * self.n);
        if candidates.is_empty() {
            return None;
        }

        let mut best: Option<(usize, usize)> = None;
        let mut best_gain = f64::NEG_INFINITY;
        for (i, j) in candidates {
            let p = sigmoid(self.theta[i] - self.theta[j]);
            let gain = p * (1.0 - p) * (self.sigma2[i] + self.sigma2[j]);
            if gain.is_finite() && gain > best_gain {
                best_gain = gain;
                best = Some((i, j));
            }
        }

        if best.is_some() {
            self.selections_since_refit += 1;
        }
        best
    }

    #[cfg(test)]
    fn theta_for_test(&self) -> &[f64] {
        &self.theta
    }

    #[cfg(test)]
    fn refit_for_test(&mut self, votes: &[VoteRec]) {
        self.refit(votes);
    }
}

impl PairStrategy for BtUncertainty {
    fn name(&self) -> &'static str {
        "bt_uncertainty"
    }

    fn next_pair(&mut self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        if self.n < 2 {
            return None;
        }

        let total_pairs = self.n * (self.n - 1) / 2;
        if state.comparisons_made() >= total_pairs {
            return None;
        }

        if !Self::all_items_appeared(state) {
            return self.next_bootstrap_pair(state, rng);
        }

        self.next_model_pair(state, rng)
    }
}

fn sigmoid(x: f64) -> f64 {
    let x = x.clamp(-30.0, 30.0);
    1.0 / (1.0 + (-x).exp())
}

fn any_unused_pair(state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
    let n = state.n();
    if n < 2 {
        return None;
    }
    for _ in 0..400 {
        let i = rng.gen_range(0..n);
        let j = rng.gen_range(0..n);
        if i != j && !state.compared(i, j) {
            return Some((i, j));
        }
    }
    for i in 0..n {
        for j in (i + 1)..n {
            if !state.compared(i, j) {
                return Some((i, j));
            }
        }
    }
    None
}

fn sample_unused_pairs(
    state: &SimState,
    rng: &mut SmallRng,
    max_count: usize,
) -> Vec<(usize, usize)> {
    let n = state.n();
    if n < 2 || max_count == 0 {
        return Vec::new();
    }
    let total = n * (n - 1) / 2;
    let unused = total.saturating_sub(state.comparisons_made());
    if unused == 0 {
        return Vec::new();
    }
    let target = max_count.min(unused);

    // Enumerate when the pool is small enough to be cheap.
    if unused <= target || total <= target.saturating_mul(3) {
        let mut out = Vec::with_capacity(unused);
        for i in 0..n {
            for j in (i + 1)..n {
                if !state.compared(i, j) {
                    out.push((i, j));
                }
            }
        }
        if out.len() > target {
            out.shuffle(rng);
            out.truncate(target);
        }
        return out;
    }

    let mut out = Vec::with_capacity(target);
    let mut seen = std::collections::HashSet::with_capacity(target * 2);
    let mut attempts = 0usize;
    let max_attempts = target.saturating_mul(40).max(200);
    while out.len() < target && attempts < max_attempts {
        attempts += 1;
        let i = rng.gen_range(0..n);
        let j = rng.gen_range(0..n);
        if i == j {
            continue;
        }
        let (a, b) = if i < j { (i, j) } else { (j, i) };
        if state.compared(a, b) {
            continue;
        }
        if seen.insert((a, b)) {
            out.push((a, b));
        }
    }

    if out.is_empty() {
        // Rejection sampling failed; fall back to a full scan.
        for i in 0..n {
            for j in (i + 1)..n {
                if !state.compared(i, j) {
                    out.push((i, j));
                    if out.len() >= target {
                        return out;
                    }
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use rand::rngs::SmallRng;
    use rand::SeedableRng;

    use super::BtUncertainty;
    use crate::state::{SimState, VoteRec};
    use crate::strategies::PairStrategy;

    fn blank_state(n: usize) -> SimState {
        SimState::new(n, vec![0; n], 1)
    }

    #[test]
    fn bootstrap_covers_all_items() {
        let n = 11; // odd: one bye per matching
        let mut strat = BtUncertainty::new(n);
        let mut state = blank_state(n);
        let mut rng = SmallRng::seed_from_u64(7);

        let mut appeared = vec![false; n];
        // Successive near-perfect matchings: floor(n/2) pairs each round.
        let mut rounds = 0;
        while !appeared.iter().all(|&x| x) {
            rounds += 1;
            assert!(rounds <= n, "bootstrap failed to cover items");
            let mut proposed_this_round = 0;
            let round_budget = n / 2;
            while proposed_this_round < round_budget && !appeared.iter().all(|&x| x) {
                let (i, j) = strat
                    .next_pair(&state, &mut rng)
                    .expect("bootstrap should propose");
                assert!(!state.compared(i, j));
                state.push_vote(VoteRec {
                    winner: i,
                    loser: j,
                    wr: 2.0,
                    lr: 1.0,
                });
                appeared[i] = true;
                appeared[j] = true;
                proposed_this_round += 1;
            }
        }
        assert!(appeared.iter().all(|&x| x));
        // Still in / just leaving bootstrap: every item has a vote.
        assert!(BtUncertainty::all_items_appeared(&state));
    }

    #[test]
    fn fitted_theta_orders_strong_winner() {
        let n = 6;
        let mut strat = BtUncertainty::new(n);
        let mut state = blank_state(n);

        // Strong total order: lower index always beats higher index.
        for i in 0..n {
            for j in (i + 1)..n {
                for _ in 0..4 {
                    state.push_vote(VoteRec {
                        winner: i,
                        loser: j,
                        wr: 2.0,
                        lr: 1.0,
                    });
                }
            }
        }

        strat.refit_for_test(state.votes());
        let theta = strat.theta_for_test();
        // Item 0 always wins → highest worth; strictly decreasing along the order.
        let best = (0..n).max_by(|a, b| theta[*a].partial_cmp(&theta[*b]).unwrap()).unwrap();
        assert_eq!(best, 0, "always-winner should have highest theta; got {theta:?}");
        for i in 0..(n - 1) {
            assert!(
                theta[i] > theta[i + 1],
                "expected theta[{i}] > theta[{}]; got {:?}",
                i + 1,
                theta
            );
        }
    }

    #[test]
    fn acquisition_never_proposes_used_pair() {
        let n = 8;
        let mut strat = BtUncertainty::new(n);
        let mut state = blank_state(n);
        let mut rng = SmallRng::seed_from_u64(123);

        let total = n * (n - 1) / 2;
        for _ in 0..total {
            let Some((i, j)) = strat.next_pair(&state, &mut rng) else {
                break;
            };
            assert!(i != j);
            assert!(
                !state.compared(i, j),
                "proposed already-compared pair ({i},{j})"
            );
            state.push_vote(VoteRec {
                winner: i,
                loser: j,
                wr: 2.0,
                lr: 1.0,
            });
        }
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }

    #[test]
    fn edge_case_n1_returns_none() {
        let mut strat = BtUncertainty::new(1);
        let state = blank_state(1);
        let mut rng = SmallRng::seed_from_u64(1);
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }

    #[test]
    fn edge_case_n2_proposes_only_pair_then_none() {
        let mut strat = BtUncertainty::new(2);
        let mut state = blank_state(2);
        let mut rng = SmallRng::seed_from_u64(2);
        let p = strat.next_pair(&state, &mut rng);
        assert!(p == Some((0, 1)) || p == Some((1, 0)));
        let (i, j) = p.unwrap();
        state.push_vote(VoteRec {
            winner: i,
            loser: j,
            wr: 2.0,
            lr: 1.0,
        });
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }
}
