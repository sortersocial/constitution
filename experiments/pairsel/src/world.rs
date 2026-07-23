//! Synthetic worlds: true commit values and contributor structure.

use rand::rngs::SmallRng;
use rand::{Rng, SeedableRng};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WorldKind {
    /// Normal log-worths, scaled to the calibrated theta sd.
    Calibrated,
    /// Heavy-tail log-worths (standardized exponential), same sd — a few
    /// commits are much more valuable, the realistic commit-value shape.
    HeavyTail,
}

impl WorldKind {
    pub fn parse(s: &str) -> Self {
        match s {
            "calibrated" => Self::Calibrated,
            "heavytail" => Self::HeavyTail,
            other => panic!("unknown world {other} (use calibrated|heavytail)"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct World {
    /// True log-worths; Rank Centrality with complete noiseless data
    /// recovers softmax(theta).
    pub theta: Vec<f64>,
    /// Commit index -> contributor index.
    pub authors: Vec<usize>,
    pub n_contributors: usize,
}

impl World {
    /// True payout share per contributor: sum of exp(theta) by author.
    pub fn true_payout(&self) -> Vec<f64> {
        let mut p = vec![0.0f64; self.n_contributors];
        let mut total = 0.0;
        for (i, &a) in self.authors.iter().enumerate() {
            let w = self.theta[i].exp();
            p[a] += w;
            total += w;
        }
        for x in p.iter_mut() {
            *x /= total;
        }
        p
    }
}

/// Zipf-ish contributor sizes (share of contributor c+1 proportional to
/// 1/(c+1)), commits assigned randomly.
pub fn generate(
    kind: WorldKind,
    n: usize,
    n_contributors: usize,
    kappa: f64,
    theta_sd: f64,
    seed: u64,
) -> World {
    let mut rng = SmallRng::seed_from_u64(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15));
    let c = n_contributors.max(1);

    // contributor sizes ~ Zipf(1)
    let weights: Vec<f64> = (1..=c).map(|k| 1.0 / k as f64).collect();
    let wsum: f64 = weights.iter().sum();
    let mut authors = Vec::with_capacity(n);
    for k in 0..c {
        let target = ((weights[..=k].iter().sum::<f64>() / wsum) * n as f64).round() as usize;
        while authors.len() < target.min(n) {
            authors.push(k);
        }
    }
    while authors.len() < n {
        authors.push(c - 1);
    }
    // shuffle assignment
    for i in (1..n).rev() {
        let j = rng.gen_range(0..=i);
        authors.swap(i, j);
    }

    // contributor means and per-commit residuals
    let mu: Vec<f64> = (0..c).map(|_| gauss(&mut rng)).collect();
    let resid: Vec<f64> = (0..n)
        .map(|_| match kind {
            WorldKind::Calibrated => gauss(&mut rng),
            WorldKind::HeavyTail => {
                // standardized Exponential(1): mean 0, sd 1, right-skewed
                let u: f64 = rng.gen_range(1e-12..1.0);
                -(u.ln()) - 1.0
            }
        })
        .collect();

    let raw: Vec<f64> = (0..n)
        .map(|i| kappa.sqrt() * mu[authors[i]] + (1.0 - kappa).sqrt() * resid[i])
        .collect();
    let mean = raw.iter().sum::<f64>() / n as f64;
    let var = raw.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / n as f64;
    let scale = if var > 0.0 { theta_sd / var.sqrt() } else { 1.0 };
    let theta = raw.iter().map(|x| (x - mean) * scale).collect();

    World { theta, authors, n_contributors: c }
}

fn gauss(rng: &mut SmallRng) -> f64 {
    // Box-Muller
    let u1: f64 = rng.gen_range(1e-12..1.0);
    let u2: f64 = rng.gen_range(0.0..1.0);
    (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn payout_sums_to_one_and_theta_scaled() {
        let w = generate(WorldKind::HeavyTail, 100, 5, 0.5, 0.749, 42);
        assert_eq!(w.theta.len(), 100);
        assert_eq!(w.authors.len(), 100);
        let p = w.true_payout();
        assert!((p.iter().sum::<f64>() - 1.0).abs() < 1e-9);
        let sd = (w.theta.iter().map(|x| x * x).sum::<f64>() / 100.0).sqrt();
        assert!((sd - 0.749).abs() < 0.02);
    }

    #[test]
    fn single_contributor_gets_everything() {
        let w = generate(WorldKind::Calibrated, 20, 1, 0.0, 0.749, 7);
        let p = w.true_payout();
        assert!((p[0] - 1.0).abs() < 1e-12);
    }
}
