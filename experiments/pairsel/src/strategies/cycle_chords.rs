//! Hamiltonian cycle + degree-balanced random chords (score-free).
//!
//! Phase 1 lays down a random Hamilton cycle (N edges) for connectivity.
//! Phase 2 grows a near-regular graph by always preferring unused pairs
//! among current minimum-degree vertices (with a cheap random fallback).
//!
//! Degrees are recomputed from `state.compared_pairs()` on each chord
//! selection so they always match the real comparison graph.

use rand::rngs::SmallRng;
use rand::seq::SliceRandom;
use rand::Rng;

use crate::state::SimState;
use crate::strategies::PairStrategy;

pub struct CycleChords {
    n: usize,
    /// Shuffled Hamilton-cycle order; built lazily on first proposal.
    order: Vec<usize>,
    /// Number of cycle ring slots already consumed (0..n).
    cycle_pos: usize,
}

impl CycleChords {
    pub fn new(n: usize) -> Self {
        Self {
            n,
            order: Vec::new(),
            cycle_pos: 0,
        }
    }

    fn ensure_order(&mut self, rng: &mut SmallRng) {
        if self.order.is_empty() && self.n > 0 {
            self.order = (0..self.n).collect();
            self.order.shuffle(rng);
        }
    }

    fn next_cycle_pair(&mut self, state: &SimState) -> Option<(usize, usize)> {
        while self.cycle_pos < self.n {
            let a = self.order[self.cycle_pos];
            let b = self.order[(self.cycle_pos + 1) % self.n];
            self.cycle_pos += 1;
            if a != b && !state.compared(a, b) {
                return Some((a, b));
            }
        }
        None
    }

    fn degrees(state: &SimState, n: usize) -> Vec<usize> {
        let mut deg = vec![0usize; n];
        for (i, j) in state.compared_pairs() {
            deg[i] += 1;
            deg[j] += 1;
        }
        deg
    }

    /// Random unused pair among `nodes` (all pairs within the set), if any.
    fn random_unused_within(
        nodes: &[usize],
        state: &SimState,
        rng: &mut SmallRng,
    ) -> Option<(usize, usize)> {
        if nodes.len() < 2 {
            return None;
        }
        let mut candidates: Vec<(usize, usize)> = Vec::new();
        for a in 0..nodes.len() {
            for b in (a + 1)..nodes.len() {
                let i = nodes[a];
                let j = nodes[b];
                if !state.compared(i, j) {
                    candidates.push((i, j));
                }
            }
        }
        candidates.choose(rng).copied()
    }

    /// Random unused edge between `a` and a node in `others`.
    fn random_unused_from(
        a: usize,
        others: &[usize],
        state: &SimState,
        rng: &mut SmallRng,
    ) -> Option<(usize, usize)> {
        let mut candidates: Vec<(usize, usize)> = Vec::new();
        for &b in others {
            if a != b && !state.compared(a, b) {
                candidates.push((a, b));
            }
        }
        candidates.choose(rng).copied()
    }

    /// Among up to `samples` random unused pairs, return the one with
    /// minimal degree sum (ties broken by first found among minima).
    fn min_degree_sum_sample(
        n: usize,
        deg: &[usize],
        state: &SimState,
        rng: &mut SmallRng,
        samples: usize,
    ) -> Option<(usize, usize)> {
        let mut best: Option<(usize, usize)> = None;
        let mut best_sum = usize::MAX;
        let mut found = 0usize;
        // Cap attempts so dense graphs don't spin forever.
        let max_attempts = samples.saturating_mul(32).max(256);
        for _ in 0..max_attempts {
            if found >= samples {
                break;
            }
            let i = rng.gen_range(0..n);
            let j = rng.gen_range(0..n);
            if i == j || state.compared(i, j) {
                continue;
            }
            found += 1;
            let sum = deg[i] + deg[j];
            if sum < best_sum {
                best_sum = sum;
                best = Some((i, j));
            }
        }
        best
    }

    fn next_chord(&self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        let n = self.n;
        if n < 2 {
            return None;
        }
        let max_pairs = n * (n - 1) / 2;
        if state.comparisons_made() >= max_pairs {
            return None;
        }

        let deg = Self::degrees(state, n);
        let d_min = *deg.iter().min().unwrap_or(&0);
        let tier1: Vec<usize> = (0..n).filter(|&i| deg[i] == d_min).collect();

        if tier1.len() >= 2 {
            if let Some(p) = Self::random_unused_within(&tier1, state, rng) {
                return Some(p);
            }
        } else if tier1.len() == 1 {
            let a = tier1[0];
            let d2 = (0..n)
                .filter(|&i| i != a)
                .map(|i| deg[i])
                .min()
                .unwrap_or(d_min);
            let tier2: Vec<usize> = (0..n).filter(|&i| i != a && deg[i] == d2).collect();
            if let Some(p) = Self::random_unused_from(a, &tier2, state, rng) {
                return Some(p);
            }
        }

        Self::min_degree_sum_sample(n, &deg, state, rng, 64)
    }
}

impl PairStrategy for CycleChords {
    fn name(&self) -> &'static str {
        "cycle_chords"
    }

    fn next_pair(&mut self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        if self.n < 2 {
            return None;
        }
        self.ensure_order(rng);
        if self.cycle_pos < self.n {
            if let Some(p) = self.next_cycle_pair(state) {
                return Some(p);
            }
        }
        self.next_chord(state, rng)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{SimState, VoteRec};
    use rand::SeedableRng;

    fn fresh_state(n: usize) -> SimState {
        SimState::new(n, vec![0; n], 1)
    }

    fn push_pair(state: &mut SimState, i: usize, j: usize) {
        state.push_vote(VoteRec {
            winner: i,
            loser: j,
            wr: 2.0,
            lr: 1.0,
        });
    }

    fn connected(n: usize, edges: &[(usize, usize)]) -> bool {
        if n == 0 {
            return true;
        }
        let mut parent: Vec<usize> = (0..n).collect();
        fn find(parent: &mut [usize], mut x: usize) -> usize {
            while parent[x] != x {
                parent[x] = parent[parent[x]];
                x = parent[x];
            }
            x
        }
        let mut components = n;
        for &(a, b) in edges {
            let ra = find(&mut parent, a);
            let rb = find(&mut parent, b);
            if ra != rb {
                parent[ra] = rb;
                components -= 1;
            }
        }
        components == 1
    }

    fn graph_degrees(n: usize, state: &SimState) -> (usize, usize) {
        let mut deg = vec![0usize; n];
        for (i, j) in state.compared_pairs() {
            deg[i] += 1;
            deg[j] += 1;
        }
        let min = *deg.iter().min().unwrap();
        let max = *deg.iter().max().unwrap();
        (min, max)
    }

    #[test]
    fn first_n_form_connected_ring() {
        let n = 12;
        let mut state = fresh_state(n);
        let mut strat = CycleChords::new(n);
        let mut rng = SmallRng::seed_from_u64(42);
        let mut edges = Vec::new();
        for _ in 0..n {
            let (i, j) = strat
                .next_pair(&state, &mut rng)
                .expect("cycle should yield N edges");
            assert!(!state.compared(i, j));
            push_pair(&mut state, i, j);
            edges.push((i, j));
        }
        assert_eq!(edges.len(), n);
        assert!(connected(n, &edges), "first N edges must connect all items");
        // Hamilton cycle: every vertex degree exactly 2.
        let mut deg = vec![0usize; n];
        for &(a, b) in &edges {
            deg[a] += 1;
            deg[b] += 1;
        }
        assert!(deg.iter().all(|&d| d == 2), "ring degrees must be 2: {deg:?}");
    }

    #[test]
    fn chord_phase_degree_balance() {
        let n = 20;
        let mut state = fresh_state(n);
        let mut strat = CycleChords::new(n);
        let mut rng = SmallRng::seed_from_u64(7);
        // Cycle
        for _ in 0..n {
            let (i, j) = strat.next_pair(&state, &mut rng).unwrap();
            push_pair(&mut state, i, j);
        }
        // 3N chords
        for _ in 0..3 * n {
            let (i, j) = strat
                .next_pair(&state, &mut rng)
                .expect("chord proposal");
            assert!(!state.compared(i, j));
            push_pair(&mut state, i, j);
            let (dmin, dmax) = graph_degrees(n, &state);
            assert!(
                dmax - dmin <= 2,
                "max-min degree > 2: min={dmin} max={dmax}"
            );
        }
    }

    #[test]
    fn never_proposes_used_pair() {
        let n = 10;
        let mut state = fresh_state(n);
        let mut strat = CycleChords::new(n);
        let mut rng = SmallRng::seed_from_u64(99);
        let max_pairs = n * (n - 1) / 2;
        let mut seen = std::collections::HashSet::new();
        for _ in 0..max_pairs {
            let Some((i, j)) = strat.next_pair(&state, &mut rng) else {
                break;
            };
            let key = if i < j { (i, j) } else { (j, i) };
            assert!(seen.insert(key), "duplicate pair {key:?}");
            assert!(!state.compared(i, j));
            push_pair(&mut state, i, j);
        }
        assert_eq!(state.comparisons_made(), max_pairs);
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }

    #[test]
    fn small_n_edges() {
        // N=1: no pairs
        {
            let state = fresh_state(1);
            let mut strat = CycleChords::new(1);
            let mut rng = SmallRng::seed_from_u64(1);
            assert!(strat.next_pair(&state, &mut rng).is_none());
        }
        // N=2: exactly one edge
        {
            let mut state = fresh_state(2);
            let mut strat = CycleChords::new(2);
            let mut rng = SmallRng::seed_from_u64(2);
            let (i, j) = strat.next_pair(&state, &mut rng).unwrap();
            assert_ne!(i, j);
            push_pair(&mut state, i, j);
            // Second cycle slot is the same undirected edge — skip, then no chords left.
            assert!(strat.next_pair(&state, &mut rng).is_none());
        }
        // N=3: cycle of 3, then chords until K_3
        {
            let mut state = fresh_state(3);
            let mut strat = CycleChords::new(3);
            let mut rng = SmallRng::seed_from_u64(3);
            let mut edges = Vec::new();
            for _ in 0..3 {
                let (i, j) = strat.next_pair(&state, &mut rng).unwrap();
                push_pair(&mut state, i, j);
                edges.push((i, j));
            }
            assert!(connected(3, &edges));
            // K_3 has 3 edges; cycle already complete — no more pairs.
            assert!(strat.next_pair(&state, &mut rng).is_none());
        }
    }
}
