//! Budget loop: strategies pick pairs, the harness casts k calibrated
//! votes per pair, metrics are logged at vote-budget checkpoints.

use rand::rngs::SmallRng;
use rand::{Rng, SeedableRng};

use crate::calib::Calibration;
use crate::metrics::{kendall_tau, payout_from_scores, payout_tv, top_k_recall};
use crate::noise::gen_vote;
use crate::state::SimState;
use crate::strategies;
use crate::world::{generate, World, WorldKind};

#[derive(Debug, Clone)]
pub struct RunParams {
    pub strategy: String,
    pub n: usize,
    pub contributors: usize,
    pub kappa: f64,
    pub world: WorldKind,
    pub votes_per_edge: u32,
    pub budget_votes: usize,
    pub checkpoints: Vec<usize>,
    pub replicate: u64,
}

#[derive(Debug, Clone)]
pub struct CheckpointRow {
    pub votes_spent: usize,
    pub comparisons: usize,
    pub payout_tv: f64,
    pub kendall: f64,
    pub top10_recall: f64,
    pub fallbacks: usize,
}

/// Run one replicate; returns one row per checkpoint.
pub fn run_once(params: &RunParams, calib: &Calibration) -> Vec<CheckpointRow> {
    let world: World = generate(
        params.world,
        params.n,
        params.contributors,
        params.kappa,
        calib.theta_sd,
        params.replicate,
    );
    let true_payout = world.true_payout();
    let mut state = SimState::new(params.n, world.authors.clone(), world.n_contributors);
    let mut strategy = strategies::make(&params.strategy, params.n);
    let mut rng = SmallRng::seed_from_u64(
        params.replicate ^ hash_name(&params.strategy),
    );

    let mut rows = Vec::with_capacity(params.checkpoints.len());
    let mut votes_spent = 0usize;
    let mut fallbacks = 0usize;
    let mut next_checkpoint = 0usize;
    let max_pairs = params.n * (params.n - 1) / 2;

    while votes_spent < params.budget_votes {
        let pair = match propose(&mut *strategy, &state, &mut rng) {
            Some(p) => p,
            None => {
                if state.comparisons_made() >= max_pairs {
                    break; // every pair compared; nothing left to learn
                }
                fallbacks += 1;
                match random_unused(&state, &mut rng) {
                    Some(p) => p,
                    None => break,
                }
            }
        };
        let t0 = state.pair_vote_count(pair.0, pair.1);
        let k = params.votes_per_edge.min((params.budget_votes - votes_spent) as u32);
        for t in 0..k {
            let v = gen_vote(params.replicate, &world, calib, pair.0, pair.1, t0 + t);
            state.push_vote(v);
            votes_spent += 1;
            while next_checkpoint < params.checkpoints.len()
                && votes_spent >= params.checkpoints[next_checkpoint]
            {
                rows.push(measure(
                    &state, &world, &true_payout, votes_spent, fallbacks,
                ));
                next_checkpoint += 1;
            }
        }
    }
    // final row if the run ended between checkpoints (e.g. all pairs used)
    if rows.last().map(|r| r.votes_spent) != Some(votes_spent) && votes_spent > 0 {
        rows.push(measure(&state, &world, &true_payout, votes_spent, fallbacks));
    }
    rows
}

fn measure(
    state: &SimState,
    world: &World,
    true_payout: &[f64],
    votes_spent: usize,
    fallbacks: usize,
) -> CheckpointRow {
    let scores = state.scores();
    CheckpointRow {
        votes_spent,
        comparisons: state.comparisons_made(),
        payout_tv: payout_tv(&payout_from_scores(&scores, world), true_payout),
        kendall: kendall_tau(&scores, &world.theta),
        top10_recall: top_k_recall(&scores, &world.theta, 10.min(world.theta.len())),
        fallbacks,
    }
}

/// Ask the strategy for a pair; reject already-compared or invalid pairs
/// (up to 20 attempts) so no strategy can silently re-spend an edge.
fn propose(
    strategy: &mut dyn strategies::PairStrategy,
    state: &SimState,
    rng: &mut SmallRng,
) -> Option<(usize, usize)> {
    for _ in 0..20 {
        match strategy.next_pair(state, rng) {
            Some((i, j)) if i < state.n() && j < state.n() && i != j => {
                if !state.compared(i, j) {
                    return Some((i.min(j), i.max(j)));
                }
            }
            Some(_) => {}
            None => return None,
        }
    }
    None
}

fn random_unused(state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
    let n = state.n();
    for _ in 0..2000 {
        let i = rng.gen_range(0..n);
        let j = rng.gen_range(0..n);
        if i != j && !state.compared(i, j) {
            return Some((i.min(j), i.max(j)));
        }
    }
    // dense fallback: scan
    for i in 0..n {
        for j in (i + 1)..n {
            if !state.compared(i, j) {
                return Some((i, j));
            }
        }
    }
    None
}

fn hash_name(name: &str) -> u64 {
    name.bytes()
        .fold(0xcbf2_9ce4_8422_2325u64, |h, b| {
            (h ^ b as u64).wrapping_mul(0x1000_0000_01b3)
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::calib::default_calibration;

    #[test]
    fn random_reference_runs_to_budget() {
        let params = RunParams {
            strategy: "random_pairs".into(),
            n: 30,
            contributors: 5,
            kappa: 0.5,
            world: WorldKind::HeavyTail,
            votes_per_edge: 1,
            budget_votes: 200,
            checkpoints: vec![30, 60, 120, 200],
            replicate: 3,
        };
        let rows = run_once(&params, &default_calibration());
        assert_eq!(rows.len(), 4);
        assert!(rows.iter().all(|r| r.payout_tv >= 0.0 && r.payout_tv <= 1.0));
        // error should broadly decrease with budget
        assert!(rows.last().unwrap().payout_tv <= rows[0].payout_tv + 0.15);
    }

    #[test]
    fn zip_reference_runs_to_budget() {
        let params = RunParams {
            strategy: "zip".into(),
            n: 20,
            contributors: 4,
            kappa: 0.5,
            world: WorldKind::Calibrated,
            votes_per_edge: 3,
            budget_votes: 150,
            checkpoints: vec![50, 100, 150],
            replicate: 11,
        };
        let rows = run_once(&params, &default_calibration());
        assert!(!rows.is_empty());
    }
}
