//! Pair-selection strategies. One file per strategy; each implements
//! `PairStrategy` and registers in `make()` below.
//!
//! Contract for implementors:
//! - `next_pair` returns the next unordered pair to compare, or `None`
//!   when the strategy has nothing more to propose (the harness then
//!   falls back to a random unused pair, and counts the fallback).
//! - Never propose an already-compared pair (`state.compared(i, j)`);
//!   the harness rejects them (20 rejects in a row are treated as None).
//! - All randomness must come from the provided `rng` so runs stay
//!   reproducible. No global state, no I/O, no threads.
//! - `state.scores()` / `state.ranking()` recompute production Rank
//!   Centrality lazily (cached until the next vote). They are cheap to
//!   call repeatedly between votes but cost ~1ms after each vote at
//!   N=161 — call once per selection, not once per candidate.
//! - Strategies see votes, not truth: nothing from `world` is exposed.

use rand::rngs::SmallRng;

use crate::state::SimState;

pub mod bootstrap_inversion;
pub mod bt_uncertainty;
pub mod cycle_chords;
pub mod explore_refine;
pub mod merge_sort;
pub mod payout_opt;
pub mod quicksort;
pub mod random_matching;
pub mod random_pairs;
pub mod swiss;
pub mod top_heavy;
pub mod zip;

pub trait PairStrategy: Send {
    fn name(&self) -> &'static str;
    fn next_pair(&mut self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)>;
}

pub const ALL: &[&str] = &[
    "random_pairs",
    "zip",
    "random_matching",
    "swiss",
    "merge_sort",
    "quicksort",
    "bt_uncertainty",
    "bootstrap_inversion",
    "explore_refine",
    "payout_opt",
    "top_heavy",
    "cycle_chords",
];

pub fn make(name: &str, n: usize) -> Box<dyn PairStrategy> {
    match name {
        "random_pairs" => Box::new(random_pairs::RandomPairs::new(n)),
        "zip" => Box::new(zip::Zip::new(n)),
        "random_matching" => Box::new(random_matching::RandomMatching::new(n)),
        "swiss" => Box::new(swiss::Swiss::new(n)),
        "merge_sort" => Box::new(merge_sort::MergeSort::new(n)),
        "quicksort" => Box::new(quicksort::Quicksort::new(n)),
        "bt_uncertainty" => Box::new(bt_uncertainty::BtUncertainty::new(n)),
        "bootstrap_inversion" => Box::new(bootstrap_inversion::BootstrapInversion::new(n)),
        "explore_refine" => Box::new(explore_refine::ExploreRefine::new(n)),
        "payout_opt" => Box::new(payout_opt::PayoutOpt::new(n)),
        "top_heavy" => Box::new(top_heavy::TopHeavy::new(n)),
        "cycle_chords" => Box::new(cycle_chords::CycleChords::new(n)),
        other => panic!("unknown strategy: {other}"),
    }
}
