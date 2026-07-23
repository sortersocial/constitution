//! Evaluation metrics. Primary: payout total variation.

use crate::world::World;

/// Estimated payout shares: sum of Rank Centrality scores by contributor.
pub fn payout_from_scores(scores: &[f64], world: &World) -> Vec<f64> {
    let mut p = vec![0.0f64; world.n_contributors];
    for (i, &a) in world.authors.iter().enumerate() {
        p[a] += scores[i];
    }
    let total: f64 = p.iter().sum();
    if total > 0.0 {
        for x in p.iter_mut() {
            *x /= total;
        }
    }
    p
}

/// Total variation distance between payout vectors: the fraction of the
/// emission sent to the wrong contributors.
pub fn payout_tv(estimated: &[f64], truth: &[f64]) -> f64 {
    0.5 * estimated
        .iter()
        .zip(truth)
        .map(|(a, b)| (a - b).abs())
        .sum::<f64>()
}

/// Kendall tau-b between estimated scores and true theta (O(n^2), fine
/// for n <= 200).
pub fn kendall_tau(scores: &[f64], theta: &[f64]) -> f64 {
    let n = scores.len();
    let mut concordant = 0i64;
    let mut discordant = 0i64;
    let mut ties_a = 0i64;
    let mut ties_b = 0i64;
    for i in 0..n {
        for j in (i + 1)..n {
            let da = scores[i] - scores[j];
            let db = theta[i] - theta[j];
            if da == 0.0 && db == 0.0 {
                continue;
            } else if da == 0.0 {
                ties_a += 1;
            } else if db == 0.0 {
                ties_b += 1;
            } else if (da > 0.0) == (db > 0.0) {
                concordant += 1;
            } else {
                discordant += 1;
            }
        }
    }
    let n0 = concordant + discordant;
    let denom = (((n0 + ties_a) as f64) * ((n0 + ties_b) as f64)).sqrt();
    if denom == 0.0 {
        0.0
    } else {
        (concordant - discordant) as f64 / denom
    }
}

/// Fraction of the true top-k found in the estimated top-k.
pub fn top_k_recall(scores: &[f64], theta: &[f64], k: usize) -> f64 {
    let top = |v: &[f64]| -> Vec<usize> {
        let mut idx: Vec<usize> = (0..v.len()).collect();
        idx.sort_by(|&a, &b| v[b].partial_cmp(&v[a]).unwrap().then(a.cmp(&b)));
        idx.truncate(k);
        idx
    };
    let est = top(scores);
    let tru = top(theta);
    let hits = est.iter().filter(|i| tru.contains(i)).count();
    hits as f64 / k as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tv_basics() {
        assert_eq!(payout_tv(&[0.5, 0.5], &[0.5, 0.5]), 0.0);
        assert!((payout_tv(&[1.0, 0.0], &[0.0, 1.0]) - 1.0).abs() < 1e-12);
    }

    #[test]
    fn kendall_perfect_and_inverted() {
        let theta = [3.0, 2.0, 1.0];
        assert!((kendall_tau(&[0.5, 0.3, 0.2], &theta) - 1.0).abs() < 1e-12);
        assert!((kendall_tau(&[0.2, 0.3, 0.5], &theta) + 1.0).abs() < 1e-12);
    }

    #[test]
    fn top_k() {
        let theta = [4.0, 3.0, 2.0, 1.0];
        assert_eq!(top_k_recall(&[0.4, 0.3, 0.2, 0.1], &theta, 2), 1.0);
        assert_eq!(top_k_recall(&[0.1, 0.2, 0.3, 0.4], &theta, 2), 0.0);
    }
}
