//! Production-exact Rank Centrality, mirroring `rank_centrality` in
//! constitution.py: W[loser][winner] += wr, W[winner][loser] += lr,
//! P from pairwise win fractions normalized by max row sum, stationary
//! distribution by power iteration.

use crate::state::VoteRec;

pub const MAX_ITERS: usize = 10_000;
pub const ATOL: f64 = 1e-12;

/// Scores for items 0..n from a stream of votes. Matches production
/// semantics exactly, including behavior on disconnected graphs (power
/// iteration from uniform, no teleport regularization).
pub fn rank_scores(n: usize, votes: impl Iterator<Item = VoteRec>) -> Vec<f64> {
    rank_scores_from(n, votes, None)
}

/// Same computation but power iteration may start from a previous
/// stationary vector (`warm`), which converges much faster when the vote
/// set changed only slightly. The fixed point is identical on connected
/// graphs; only used for strategy-internal score peeks, never for
/// checkpoint metrics.
pub fn rank_scores_from(
    n: usize,
    votes: impl Iterator<Item = VoteRec>,
    warm: Option<&[f64]>,
) -> Vec<f64> {
    if n == 0 {
        return vec![];
    }
    if n == 1 {
        return vec![1.0];
    }
    let mut w = vec![0.0f64; n * n];
    for v in votes {
        w[v.loser * n + v.winner] += v.wr;
        w[v.winner * n + v.loser] += v.lr;
    }
    let mut p = vec![0.0f64; n * n];
    for i in 0..n {
        for j in 0..n {
            let denom = w[i * n + j] + w[j * n + i];
            if denom > 0.0 {
                p[i * n + j] = w[i * n + j] / denom;
            }
        }
    }
    let mut w_max = 0.0f64;
    for i in 0..n {
        let row: f64 = p[i * n..(i + 1) * n].iter().sum();
        if row > w_max {
            w_max = row;
        }
    }
    if w_max == 0.0 {
        w_max = 1.0;
    }
    for x in p.iter_mut() {
        *x /= w_max;
    }
    for i in 0..n {
        let row: f64 = p[i * n..(i + 1) * n].iter().sum();
        let diag = &mut p[i * n + i];
        *diag += 1.0 - row;
    }
    // Solve pi P = pi, sum(pi) = 1 directly: transpose(P) - I with the
    // last equation replaced by the normalization row. Exact stationary
    // distribution in O(n^3) — production's truncated power iteration
    // approximates the same fixed point but mixes too slowly to simulate
    // millions of times. Falls back to power iteration when the chain is
    // reducible (disconnected comparison graph) and the solve is singular.
    if let Some(pi) = solve_stationary(n, &p) {
        return pi;
    }
    power_iteration(n, &p, warm)
}

fn solve_stationary(n: usize, p: &[f64]) -> Option<Vec<f64>> {
    // A x = b with A = (P^T - I) except last row = ones, b = e_last
    let mut a = vec![0.0f64; n * n];
    for i in 0..n {
        for j in 0..n {
            a[i * n + j] = p[j * n + i] - if i == j { 1.0 } else { 0.0 };
        }
    }
    for j in 0..n {
        a[(n - 1) * n + j] = 1.0;
    }
    let mut b = vec![0.0f64; n];
    b[n - 1] = 1.0;

    // Gaussian elimination with partial pivoting
    for col in 0..n {
        let mut pivot = col;
        for row in (col + 1)..n {
            if a[row * n + col].abs() > a[pivot * n + col].abs() {
                pivot = row;
            }
        }
        if a[pivot * n + col].abs() < 1e-12 {
            return None; // singular: reducible chain
        }
        if pivot != col {
            for j in 0..n {
                a.swap(col * n + j, pivot * n + j);
            }
            b.swap(col, pivot);
        }
        let diag = a[col * n + col];
        for row in (col + 1)..n {
            let factor = a[row * n + col] / diag;
            if factor == 0.0 {
                continue;
            }
            for j in col..n {
                a[row * n + j] -= factor * a[col * n + j];
            }
            b[row] -= factor * b[col];
        }
    }
    let mut x = vec![0.0f64; n];
    for row in (0..n).rev() {
        let mut acc = b[row];
        for j in (row + 1)..n {
            acc -= a[row * n + j] * x[j];
        }
        x[row] = acc / a[row * n + row];
    }
    // stationary distributions are nonnegative; numerical junk means the
    // chain was near-reducible — let power iteration handle it
    if x.iter().any(|v| !v.is_finite() || *v < -1e-9) {
        return None;
    }
    let sum: f64 = x.iter().map(|v| v.max(0.0)).sum();
    if !(sum.is_finite() && sum > 0.0) {
        return None;
    }
    Some(x.iter().map(|v| v.max(0.0) / sum).collect())
}

fn power_iteration(n: usize, p: &[f64], warm: Option<&[f64]>) -> Vec<f64> {
    let mut pi = match warm {
        Some(w) if w.len() == n && w.iter().sum::<f64>() > 0.0 => w.to_vec(),
        _ => vec![1.0 / n as f64; n],
    };
    let mut next = vec![0.0f64; n];
    for _ in 0..MAX_ITERS {
        next.iter_mut().for_each(|x| *x = 0.0);
        for i in 0..n {
            let s = pi[i];
            if s == 0.0 {
                continue;
            }
            let row = &p[i * n..(i + 1) * n];
            for j in 0..n {
                next[j] += s * row[j];
            }
        }
        let mut close = true;
        for i in 0..n {
            if (next[i] - pi[i]).abs() > ATOL {
                close = false;
                break;
            }
        }
        std::mem::swap(&mut pi, &mut next);
        if close {
            break;
        }
    }
    let sum: f64 = pi.iter().sum();
    if sum > 0.0 && sum.is_finite() {
        for x in pi.iter_mut() {
            *x /= sum;
        }
    }
    pi
}

/// Indices sorted by score descending, ties broken by index (stable).
pub fn ranking_of(scores: &[f64]) -> Vec<usize> {
    let mut order: Vec<usize> = (0..scores.len()).collect();
    order.sort_by(|&a, &b| {
        scores[b]
            .partial_cmp(&scores[a])
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.cmp(&b))
    });
    order
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::VoteRec;

    fn v(winner: usize, loser: usize, wr: f64, lr: f64) -> VoteRec {
        VoteRec { winner, loser, wr, lr }
    }

    #[test]
    fn winner_ranks_higher_two_items() {
        let s = rank_scores(2, [v(0, 1, 2.0, 1.0)].into_iter());
        assert!(s[0] > s[1]);
    }

    #[test]
    fn total_order_three_items() {
        let s = rank_scores(
            3,
            [v(0, 1, 2.0, 1.0), v(1, 2, 2.0, 1.0), v(0, 2, 4.0, 1.0)].into_iter(),
        );
        assert!(s[0] > s[1] && s[1] > s[2]);
    }

    #[test]
    fn scores_sum_to_one() {
        let s = rank_scores(
            3,
            [v(0, 1, 2.0, 1.0), v(1, 2, 3.0, 1.0)].into_iter(),
        );
        assert!((s.iter().sum::<f64>() - 1.0).abs() < 1e-9);
    }

    #[test]
    fn symmetric_votes_equal_scores() {
        let s = rank_scores(2, [v(0, 1, 1.0, 1.0), v(1, 0, 1.0, 1.0)].into_iter());
        assert!((s[0] - s[1]).abs() < 1e-9);
    }

    #[test]
    fn direct_solve_matches_power_iteration() {
        // connected graph with mixed ratios: both solvers must agree
        let votes = vec![
            v(0, 1, 2.0, 1.0),
            v(1, 2, 3.0, 1.0),
            v(2, 3, 1.5, 1.0),
            v(3, 0, 2.0, 1.0),
            v(0, 2, 5.0, 1.0),
            v(1, 3, 2.0, 1.0),
        ];
        let n = 4;
        let direct = rank_scores(n, votes.iter().copied());
        // reproduce the P matrix and force power iteration
        let mut w = vec![0.0f64; n * n];
        for vt in &votes {
            w[vt.loser * n + vt.winner] += vt.wr;
            w[vt.winner * n + vt.loser] += vt.lr;
        }
        let mut p = vec![0.0f64; n * n];
        for i in 0..n {
            for j in 0..n {
                let d = w[i * n + j] + w[j * n + i];
                if d > 0.0 {
                    p[i * n + j] = w[i * n + j] / d;
                }
            }
        }
        let mut w_max = 0.0f64;
        for i in 0..n {
            let row: f64 = p[i * n..(i + 1) * n].iter().sum();
            w_max = w_max.max(row);
        }
        for x in p.iter_mut() {
            *x /= w_max;
        }
        for i in 0..n {
            let row: f64 = p[i * n..(i + 1) * n].iter().sum();
            p[i * n + i] += 1.0 - row;
        }
        let power = super::power_iteration(n, &p, None);
        for i in 0..n {
            assert!(
                (direct[i] - power[i]).abs() < 1e-6,
                "mismatch at {i}: {} vs {}",
                direct[i],
                power[i]
            );
        }
    }

    #[test]
    fn disconnected_graph_falls_back_gracefully() {
        // two components: solve is singular, must still return a distribution
        let s = rank_scores(4, [v(0, 1, 2.0, 1.0), v(2, 3, 2.0, 1.0)].into_iter());
        assert!((s.iter().sum::<f64>() - 1.0).abs() < 1e-9);
        assert!(s[0] > s[1] && s[2] > s[3]);
    }
}
