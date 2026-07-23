//! Payout-variance-targeted active selection (c-optimal surrogate).
//!
//! Bootstrap with random near-perfect matchings, then select unused pairs
//! that maximize a Bradley–Terry / payout-Jacobian acquisition score.

use rand::rngs::SmallRng;
use rand::seq::SliceRandom;
use rand::Rng;

use crate::state::SimState;
use crate::strategies::PairStrategy;

const TAU: f64 = 2.0;
const PRIOR_PREC: f64 = 1.0 / (TAU * TAU); // 0.25
const GRAD_STEPS: usize = 30;
const GRAD_LR: f64 = 0.3;
const THETA_CLAMP: f64 = 30.0;

pub struct PayoutOpt {
    n: usize,
    bootstrap_done: bool,
    matching_queue: Vec<(usize, usize)>,
    theta: Vec<f64>,
    sigma2: Vec<f64>,
    fitted: bool,
    selections_since_refit: usize,
    refit_interval: usize,
}

impl PayoutOpt {
    pub fn new(n: usize) -> Self {
        let refit_interval = (n / 8).max(8);
        Self {
            n,
            bootstrap_done: n < 2,
            matching_queue: Vec::new(),
            theta: vec![0.0; n],
            sigma2: vec![1.0 / PRIOR_PREC; n],
            fitted: false,
            selections_since_refit: 0,
            refit_interval,
        }
    }

    fn all_pairs_used(state: &SimState) -> bool {
        let n = state.n();
        if n < 2 {
            return true;
        }
        state.comparisons_made() >= n * (n - 1) / 2
    }

    fn coverage(state: &SimState) -> Vec<bool> {
        let n = state.n();
        let mut covered = vec![false; n];
        for v in state.votes() {
            if v.winner < n {
                covered[v.winner] = true;
            }
            if v.loser < n {
                covered[v.loser] = true;
            }
        }
        covered
    }

    fn bootstrap_complete(state: &SimState) -> bool {
        let n = state.n();
        if n == 0 {
            return true;
        }
        Self::coverage(state).into_iter().all(|c| c)
    }

    fn fill_matching(&mut self, state: &SimState, rng: &mut SmallRng) {
        self.matching_queue.clear();
        let mut idx: Vec<usize> = (0..self.n).collect();
        idx.shuffle(rng);
        let mut k = 0;
        while k + 1 < idx.len() {
            let i = idx[k];
            let j = idx[k + 1];
            k += 2;
            if !state.compared(i, j) {
                self.matching_queue.push((i, j));
            }
        }
        // odd-N bye: last index unpaired — nothing to enqueue
    }

    fn next_bootstrap_pair(
        &mut self,
        state: &SimState,
        rng: &mut SmallRng,
    ) -> Option<(usize, usize)> {
        if Self::bootstrap_complete(state) {
            self.bootstrap_done = true;
            return None;
        }
        for _ in 0..self.n.saturating_mul(4).max(8) {
            if self.matching_queue.is_empty() {
                self.fill_matching(state, rng);
            }
            while let Some((i, j)) = self.matching_queue.pop() {
                if i != j && !state.compared(i, j) {
                    return Some((i, j));
                }
            }
            // Matching produced nothing usable; force a pair involving an uncovered item.
            let covered = Self::coverage(state);
            for i in 0..self.n {
                if covered[i] {
                    continue;
                }
                for _ in 0..32 {
                    let j = rng.gen_range(0..self.n);
                    if i != j && !state.compared(i, j) {
                        return Some((i, j));
                    }
                }
                for j in 0..self.n {
                    if i != j && !state.compared(i, j) {
                        return Some((i, j));
                    }
                }
            }
            break;
        }
        self.bootstrap_done = true;
        None
    }

    fn refit(&mut self, state: &SimState) {
        let n = self.n;
        if n == 0 {
            return;
        }
        let votes = state.votes();
        // Warm-start from current theta; 30 gradient steps on MAP objective.
        for _ in 0..GRAD_STEPS {
            let mut grad = vec![0.0; n];
            for i in 0..n {
                grad[i] -= PRIOR_PREC * self.theta[i];
            }
            for v in votes {
                let w = v.winner;
                let l = v.loser;
                if w >= n || l >= n {
                    continue;
                }
                let p = sigmoid(self.theta[w] - self.theta[l]);
                // d/dθ_w log σ(θ_w - θ_l) = 1 - p
                grad[w] += 1.0 - p;
                grad[l] -= 1.0 - p;
            }
            for i in 0..n {
                self.theta[i] = (self.theta[i] + GRAD_LR * grad[i]).clamp(-THETA_CLAMP, THETA_CLAMP);
            }
        }
        // Diagonal Fisher at fitted theta.
        let mut fisher = vec![PRIOR_PREC; n];
        for v in votes {
            let w = v.winner;
            let l = v.loser;
            if w >= n || l >= n {
                continue;
            }
            let p = sigmoid(self.theta[w] - self.theta[l]);
            let vinfo = (p * (1.0 - p)).clamp(1e-15, 0.25);
            fisher[w] += vinfo;
            fisher[l] += vinfo;
        }
        for i in 0..n {
            self.sigma2[i] = 1.0 / fisher[i].max(PRIOR_PREC);
        }
        self.fitted = true;
        self.selections_since_refit = 0;
    }

    fn maybe_refit(&mut self, state: &SimState) {
        if !self.fitted || self.selections_since_refit >= self.refit_interval {
            self.refit(state);
        }
    }

    fn next_acquisition_pair(
        &mut self,
        state: &SimState,
        rng: &mut SmallRng,
    ) -> Option<(usize, usize)> {
        if Self::all_pairs_used(state) {
            return None;
        }
        self.maybe_refit(state);

        let n = self.n;
        let authors = state.authors();
        let n_contrib = state.n_contributors();
        let single_contrib = n_contrib <= 1;

        let leverage = if single_contrib {
            vec![1.0; n] // unused; fallback path ignores leverage
        } else {
            payout_leverages(&self.theta, authors, n_contrib)
        };

        let sample_cap = (4 * n).max(1);
        let mut best: Option<(usize, usize)> = None;
        let mut best_score = f64::NEG_INFINITY;

        for _ in 0..sample_cap {
            let i = rng.gen_range(0..n);
            let j = rng.gen_range(0..n);
            if i == j || state.compared(i, j) {
                continue;
            }
            let p = sigmoid(self.theta[i] - self.theta[j]);
            let uncertain = (p * (1.0 - p)).clamp(0.0, 0.25);
            let score = if single_contrib {
                uncertain * (self.sigma2[i] + self.sigma2[j])
            } else {
                uncertain
                    * (leverage[i] * self.sigma2[i] + leverage[j] * self.sigma2[j])
            };
            if score > best_score && score.is_finite() {
                best_score = score;
                best = Some(if i < j { (i, j) } else { (j, i) });
            }
        }

        // Exhaustive fallback if random sampling found nothing (dense graphs / bad luck).
        if best.is_none() {
            for i in 0..n {
                for j in (i + 1)..n {
                    if state.compared(i, j) {
                        continue;
                    }
                    let p = sigmoid(self.theta[i] - self.theta[j]);
                    let uncertain = (p * (1.0 - p)).clamp(0.0, 0.25);
                    let score = if single_contrib {
                        uncertain * (self.sigma2[i] + self.sigma2[j])
                    } else {
                        uncertain
                            * (leverage[i] * self.sigma2[i] + leverage[j] * self.sigma2[j])
                    };
                    if score > best_score && score.is_finite() {
                        best_score = score;
                        best = Some((i, j));
                    }
                }
            }
        }

        if best.is_some() {
            self.selections_since_refit += 1;
        }
        best
    }
}

impl PairStrategy for PayoutOpt {
    fn name(&self) -> &'static str {
        "payout_opt"
    }

    fn next_pair(&mut self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        if self.n < 2 {
            return None;
        }
        if Self::all_pairs_used(state) {
            return None;
        }
        if !self.bootstrap_done {
            if let Some(p) = self.next_bootstrap_pair(state, rng) {
                return Some(p);
            }
            // bootstrap just finished (or aborted); fall through
        }
        self.next_acquisition_pair(state, rng)
    }
}

#[inline]
fn sigmoid(x: f64) -> f64 {
    let x = x.clamp(-THETA_CLAMP, THETA_CLAMP);
    1.0 / (1.0 + (-x).exp())
}

/// Softmax-aggregation payout shares P_c from BT worths.
fn payout_shares(theta: &[f64], authors: &[usize], n_contrib: usize) -> Vec<f64> {
    let n = theta.len();
    let mut w = vec![0.0; n];
    let mut total = 0.0;
    for i in 0..n {
        let t = theta[i].clamp(-THETA_CLAMP, THETA_CLAMP);
        w[i] = t.exp();
        total += w[i];
    }
    total = total.max(1e-300);
    let mut p = vec![0.0; n_contrib];
    for i in 0..n {
        let a = authors[i];
        if a < n_contrib {
            p[a] += w[i] / total;
        }
    }
    p
}

/// dP_c / dθ_i = (w_i / W) * (1{a(i)=c} - P_c)
#[cfg(test)]
fn payout_jacobian_col(
    theta: &[f64],
    authors: &[usize],
    n_contrib: usize,
    i: usize,
) -> Vec<f64> {
    let n = theta.len();
    let mut w = vec![0.0; n];
    let mut total = 0.0;
    for k in 0..n {
        let t = theta[k].clamp(-THETA_CLAMP, THETA_CLAMP);
        w[k] = t.exp();
        total += w[k];
    }
    total = total.max(1e-300);
    let shares = payout_shares(theta, authors, n_contrib);
    let a = authors[i];
    let scale = w[i] / total;
    let mut jac = vec![0.0; n_contrib];
    for c in 0..n_contrib {
        let indicator = if c == a { 1.0 } else { 0.0 };
        jac[c] = scale * (indicator - shares[c]);
    }
    jac
}

/// leverage_i = sum_c (dP_c/dθ_i)^2
fn payout_leverages(theta: &[f64], authors: &[usize], n_contrib: usize) -> Vec<f64> {
    let n = theta.len();
    let mut w = vec![0.0; n];
    let mut total = 0.0;
    for i in 0..n {
        let t = theta[i].clamp(-THETA_CLAMP, THETA_CLAMP);
        w[i] = t.exp();
        total += w[i];
    }
    total = total.max(1e-300);
    let shares = payout_shares(theta, authors, n_contrib);
    let mut sum_p2 = 0.0;
    for c in 0..n_contrib {
        sum_p2 += shares[c] * shares[c];
    }
    let mut lev = vec![0.0; n];
    for i in 0..n {
        let a = authors[i];
        let pa = if a < n_contrib { shares[a] } else { 0.0 };
        // (w_i/W)^2 * ((1-P_a)^2 + sum_{c≠a} P_c^2)
        let inner = (1.0 - pa) * (1.0 - pa) + (sum_p2 - pa * pa);
        let scale = w[i] / total;
        lev[i] = (scale * scale) * inner;
        if !lev[i].is_finite() {
            lev[i] = 0.0;
        }
    }
    lev
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{SimState, VoteRec};
    use crate::strategies::PairStrategy;
    use rand::SeedableRng;

    fn feed(state: &mut SimState, winner: usize, loser: usize) {
        state.push_vote(VoteRec {
            winner,
            loser,
            wr: 2.0,
            lr: 1.0,
        });
    }

    #[test]
    fn bootstrap_covers_all_items_first() {
        let n = 9;
        let authors: Vec<usize> = (0..n).map(|i| i % 3).collect();
        let mut state = SimState::new(n, authors, 3);
        let mut strat = PayoutOpt::new(n);
        let mut rng = SmallRng::seed_from_u64(42);

        // Collect bootstrap proposals until strategy leaves bootstrap.
        let mut proposed = Vec::new();
        for _ in 0..200 {
            if strat.bootstrap_done {
                break;
            }
            let Some((i, j)) = strat.next_pair(&state, &mut rng) else {
                break;
            };
            assert!(!state.compared(i, j));
            feed(&mut state, i, j);
            proposed.push((i, j));
            if PayoutOpt::bootstrap_complete(&state) {
                // One more call should flip bootstrap_done and move on.
                let _ = strat.next_pair(&state, &mut rng);
                break;
            }
        }
        assert!(PayoutOpt::bootstrap_complete(&state));
        let covered = PayoutOpt::coverage(&state);
        assert!(covered.iter().all(|&c| c), "every item must be covered");
        // All proposals so far were during coverage building (no acquisition yet
        // before full coverage). Count votes == proposed edges fed.
        assert_eq!(state.comparisons_made(), proposed.len());
        assert!(proposed.len() >= (n + 1) / 2); // at least one matching round
    }

    #[test]
    fn never_proposes_used_pair() {
        let n = 6;
        let authors = vec![0, 0, 1, 1, 2, 2];
        let mut state = SimState::new(n, authors, 3);
        let mut strat = PayoutOpt::new(n);
        let mut rng = SmallRng::seed_from_u64(7);
        let max_pairs = n * (n - 1) / 2;
        for _ in 0..max_pairs {
            let Some((i, j)) = strat.next_pair(&state, &mut rng) else {
                break;
            };
            assert!(i != j);
            assert!(
                !state.compared(i, j),
                "proposed already-compared pair ({i},{j})"
            );
            feed(&mut state, i, j);
        }
        assert!(PayoutOpt::all_pairs_used(&state) || strat.next_pair(&state, &mut rng).is_none());
    }

    #[test]
    fn leverage_jacobian_matches_finite_difference() {
        // 4 items, 2 contributors: [0,0,1,1]
        let authors = vec![0usize, 0, 1, 1];
        let theta = vec![0.5, -0.2, 1.0, -0.5];
        let n_contrib = 2;
        let eps = 1e-6;
        for i in 0..theta.len() {
            let jac = payout_jacobian_col(&theta, &authors, n_contrib, i);
            let mut theta_plus = theta.clone();
            let mut theta_minus = theta.clone();
            theta_plus[i] += eps;
            theta_minus[i] -= eps;
            let p_plus = payout_shares(&theta_plus, &authors, n_contrib);
            let p_minus = payout_shares(&theta_minus, &authors, n_contrib);
            for c in 0..n_contrib {
                let fd = (p_plus[c] - p_minus[c]) / (2.0 * eps);
                assert!(
                    (jac[c] - fd).abs() < 1e-6,
                    "jac mismatch item {i} contrib {c}: jac={} fd={}",
                    jac[c],
                    fd
                );
            }
            // Closed-form leverage vs sum of squared jac entries.
            let lev = payout_leverages(&theta, &authors, n_contrib)[i];
            let lev_sum: f64 = jac.iter().map(|x| x * x).sum();
            assert!((lev - lev_sum).abs() < 1e-12);
        }
    }

    #[test]
    fn single_contributor_fallback_proposes_valid_pairs() {
        let n = 5;
        let authors = vec![0; n];
        let mut state = SimState::new(n, authors, 1);
        let mut strat = PayoutOpt::new(n);
        let mut rng = SmallRng::seed_from_u64(99);
        for _ in 0..n * (n - 1) / 2 {
            let pair = strat.next_pair(&state, &mut rng);
            let Some((i, j)) = pair else {
                break;
            };
            assert!(i < n && j < n && i != j);
            assert!(!state.compared(i, j));
            feed(&mut state, i, j);
        }
        assert!(PayoutOpt::all_pairs_used(&state));
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }

    #[test]
    fn edge_n1_and_n2() {
        let mut rng = SmallRng::seed_from_u64(1);
        let mut s1 = PayoutOpt::new(1);
        let state1 = SimState::new(1, vec![0], 1);
        assert!(s1.next_pair(&state1, &mut rng).is_none());

        let mut s2 = PayoutOpt::new(2);
        let mut state2 = SimState::new(2, vec![0, 1], 2);
        let p = s2.next_pair(&state2, &mut rng).expect("N=2 should propose");
        assert!(matches!(p, (0, 1) | (1, 0)));
        feed(&mut state2, p.0, p.1);
        assert!(s2.next_pair(&state2, &mut rng).is_none());
    }
}
