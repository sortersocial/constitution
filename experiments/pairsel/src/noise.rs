//! Calibrated vote generator with counter-based common random numbers.
//!
//! Every random quantity is a pure function of (replicate seed, pair,
//! vote index, tag), so two strategies that query the same vote in the
//! same replicate receive identical outcomes — the correct coupling
//! discipline for paired comparison of adaptive policies.

use crate::calib::Calibration;
use crate::state::VoteRec;
use crate::world::World;

/// SplitMix64 as a counter-based bit mixer.
fn mix(mut z: u64) -> u64 {
    z = z.wrapping_add(0x9E37_79B9_7F4A_7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

fn key_u64(seed: u64, lo: usize, hi: usize, t: u32, tag: u64) -> u64 {
    let mut z = seed;
    z = mix(z ^ (lo as u64).wrapping_mul(0x8531_7A4B));
    z = mix(z ^ (hi as u64).wrapping_mul(0xC2B2_AE35));
    z = mix(z ^ (t as u64).wrapping_mul(0x1656_67B1));
    mix(z ^ tag)
}

fn uniform(seed: u64, lo: usize, hi: usize, t: u32, tag: u64) -> f64 {
    (key_u64(seed, lo, hi, t, tag) >> 11) as f64 / (1u64 << 53) as f64
}

fn gauss(seed: u64, lo: usize, hi: usize, t: u32, tag: u64) -> f64 {
    let u1 = uniform(seed, lo, hi, t, tag).max(1e-15);
    let u2 = uniform(seed, lo, hi, t, tag ^ 0xDEAD_BEEF);
    (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos()
}

fn sigmoid(x: f64) -> f64 {
    1.0 / (1.0 + (-x).exp())
}

/// Generate the t-th juror vote on unordered pair {i, j}.
///
/// Model (fitted to 3,061 real prod votes):
///   P(lo wins | eta) = sigmoid(theta_lo - theta_hi + eta)
/// with a shared per-comparison latent eta ~ N(0, tau^2) — the council
/// reads a given pair of diffs the same (possibly wrong) way — plus
/// epsilon contamination, and declared ratios sampled from empirical
/// tables conditioned on gap size and agreement.
pub fn gen_vote(
    seed: u64,
    world: &World,
    calib: &Calibration,
    i: usize,
    j: usize,
    t: u32,
) -> VoteRec {
    let (lo, hi) = if i < j { (i, j) } else { (j, i) };
    let gap = world.theta[lo] - world.theta[hi];
    // shared latent: t=0 tag so every vote on this pair shares it
    let eta = calib.council_tau * gauss(seed, lo, hi, 0, 1);
    let p_lo = sigmoid(gap + eta);
    let mut lo_wins = uniform(seed, lo, hi, t, 2) < p_lo;
    if uniform(seed, lo, hi, t, 3) < calib.epsilon {
        lo_wins = !lo_wins;
    }
    let (winner, loser) = if lo_wins { (lo, hi) } else { (hi, lo) };
    let agree = (world.theta[winner] - world.theta[loser]) >= 0.0;
    let ratio = calib.sample_ratio(gap.abs(), agree, uniform(seed, lo, hi, t, 4));
    VoteRec { winner, loser, wr: ratio, lr: 1.0 }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::calib::default_calibration;
    use crate::world::{generate, WorldKind};

    #[test]
    fn votes_are_deterministic_given_keys() {
        let w = generate(WorldKind::Calibrated, 10, 3, 0.5, 0.749, 1);
        let c = default_calibration();
        let a = gen_vote(99, &w, &c, 2, 7, 0);
        let b = gen_vote(99, &w, &c, 7, 2, 0); // order-insensitive
        assert_eq!(a.winner, b.winner);
        assert_eq!(a.wr, b.wr);
        let c2 = gen_vote(99, &w, &c, 2, 7, 1);
        // different vote index may differ (not guaranteed, but keys differ)
        let _ = c2;
    }

    #[test]
    fn strong_gap_wins_most_of_the_time() {
        let mut w = generate(WorldKind::Calibrated, 2, 1, 0.0, 0.749, 2);
        w.theta = vec![3.0, -3.0];
        let mut c = default_calibration();
        c.council_tau = 0.5;
        let wins = (0..2000)
            .filter(|&s| gen_vote(s, &w, &c, 0, 1, 0).winner == 0)
            .count();
        assert!(wins > 1700, "got {wins}");
    }
}
