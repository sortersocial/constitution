//! Reference incumbent: production spanning-chain + zip sort, mirroring
//! `pairwise_rank` in constitution.py.

use rand::rngs::SmallRng;

use crate::state::SimState;
use crate::strategies::PairStrategy;

pub struct Zip {
    n: usize,
    spanning_next: usize,
}

impl Zip {
    pub fn new(n: usize) -> Self {
        Self { n, spanning_next: 0 }
    }
}

impl PairStrategy for Zip {
    fn name(&self) -> &'static str {
        "zip"
    }

    fn next_pair(&mut self, state: &SimState, _rng: &mut SmallRng) -> Option<(usize, usize)> {
        if self.n < 2 {
            return None;
        }
        // Phase 1: spanning chain over index (oid) order.
        while self.spanning_next + 1 < self.n {
            let (i, j) = (self.spanning_next, self.spanning_next + 1);
            self.spanning_next += 1;
            if !state.compared(i, j) {
                return Some((i, j));
            }
        }
        // Phase 2: first uncovered adjacent pair in the current ranking.
        let ranking = state.ranking();
        for pos in 0..(self.n - 1) {
            let (a, b) = (ranking[pos], ranking[pos + 1]);
            if !state.compared(a, b) {
                return Some((a, b));
            }
        }
        None // zip terminated: every adjacent pair covered
    }
}

#[cfg(test)]
mod tests {
    use crate::calib::default_calibration;
    use crate::runner::{run_once, RunParams};
    use crate::world::WorldKind;

    #[test]
    fn zip_spans_chain_first() {
        let params = RunParams {
            strategy: "zip".into(),
            n: 10,
            contributors: 3,
            kappa: 0.5,
            world: WorldKind::Calibrated,
            votes_per_edge: 1,
            budget_votes: 9,
            checkpoints: vec![9],
            replicate: 1,
        };
        let rows = run_once(&params, &default_calibration());
        // after exactly n-1 votes at k=1, zip has made the 9 chain comparisons
        assert_eq!(rows.last().unwrap().comparisons, 9);
        assert_eq!(rows.last().unwrap().fallbacks, 0);
    }
}
