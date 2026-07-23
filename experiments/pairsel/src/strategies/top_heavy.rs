//! STUB — to be implemented by a strategy subagent.
//! Currently falls back to uniform random unused pairs.

use rand::rngs::SmallRng;
use rand::Rng;

use crate::state::SimState;
use crate::strategies::PairStrategy;

pub struct TopHeavy {
    n: usize,
}

impl TopHeavy {
    pub fn new(n: usize) -> Self {
        Self { n }
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
        for _ in 0..200 {
            let i = rng.gen_range(0..self.n);
            let j = rng.gen_range(0..self.n);
            if i != j && !state.compared(i, j) {
                return Some((i, j));
            }
        }
        None
    }
}
