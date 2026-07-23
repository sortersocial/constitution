//! Noisy randomized quicksort pair selection.
//!
//! Maintains an explicit partition stack (no recursion) so we can pause when a
//! needed (element, pivot) pair has no votes yet, then resume on the next call
//! once the harness has cast them. Pivots become hubs that help Rank Centrality
//! mix; restarts reuse resolved pairs and only spend budget on new edges.

use rand::rngs::SmallRng;
use rand::seq::SliceRandom;
use rand::Rng;

use crate::state::SimState;
use crate::strategies::PairStrategy;

/// Floor(log2(n)) for n >= 1; 0 for n == 0.
fn floor_log2(n: usize) -> u32 {
    if n <= 1 {
        0
    } else {
        (usize::BITS - 1 - n.leading_zeros()) as u32
    }
}

fn depth_cap(n: usize) -> u32 {
    2 * floor_log2(n) + 8
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Side {
    Left,
    Right,
}

/// Ratio-weighted majority on (elem, pivot). `None` = no votes yet (propose).
/// Tie → `current` (element stays on its side of the pivot in `order`).
fn classify(state: &SimState, elem: usize, pivot: usize, current: Side) -> Option<Side> {
    if state.pair_vote_count(elem, pivot) == 0 {
        return None;
    }
    let mut elem_wr = 0.0;
    let mut pivot_wr = 0.0;
    for v in state.votes() {
        if v.winner == elem && v.loser == pivot {
            elem_wr += v.wr;
        } else if v.winner == pivot && v.loser == elem {
            pivot_wr += v.wr;
        }
    }
    if elem_wr > pivot_wr {
        Some(Side::Left)
    } else if pivot_wr > elem_wr {
        Some(Side::Right)
    } else {
        Some(current) // tie → stay on current side of pivot
    }
}

/// Mid-partition pause state for one stack frame.
struct Partition {
    /// Inclusive/exclusive bounds into `order`.
    lo: usize,
    hi: usize,
    depth: u32,
    pivot: usize,
    /// Index in `order` where the pivot sat when the partition started.
    pivot_pos: usize,
    /// Next index in `[lo, hi)` to classify (skip `pivot_pos`).
    cursor: usize,
    left: Vec<usize>,
    right: Vec<usize>,
}

enum Frame {
    /// Segment not yet opened: pick pivot on entry (if depth allows).
    Pending { lo: usize, hi: usize, depth: u32 },
    Active(Partition),
}

pub struct Quicksort {
    n: usize,
    order: Vec<usize>,
    stack: Vec<Frame>,
    /// When set, the previous call proposed this unordered pair and is waiting
    /// for votes. On the next call we MUST resolve it from `state` before
    /// proposing anything else (pause/resume invariant).
    awaiting: Option<(usize, usize)>,
    /// Element currently awaiting classification (index into `order` equals
    /// the active frame's cursor at propose time).
    initialized: bool,
    /// True if the current sort pass has proposed at least one new pair.
    proposed_this_pass: bool,
    /// Completed full quicksort passes (for tests / restart bookkeeping).
    passes_completed: usize,
    /// After a no-progress restart, only ranking-adjacent fallback remains.
    fallback_only: bool,
}

impl Quicksort {
    pub fn new(n: usize) -> Self {
        Self {
            n,
            order: (0..n).collect(),
            stack: Vec::new(),
            awaiting: None,
            initialized: false,
            proposed_this_pass: false,
            passes_completed: 0,
            fallback_only: false,
        }
    }

    fn start_pass(&mut self, rng: &mut SmallRng, shuffle: bool) {
        if shuffle {
            self.order = (0..self.n).collect();
            self.order.shuffle(rng);
        }
        self.stack.clear();
        self.awaiting = None;
        self.proposed_this_pass = false;
        if self.n >= 2 {
            self.stack.push(Frame::Pending {
                lo: 0,
                hi: self.n,
                depth: 0,
            });
        }
        self.initialized = true;
    }

    fn fallback_adjacent(&self, state: &SimState) -> Option<(usize, usize)> {
        let ranking = state.ranking();
        for w in ranking.windows(2) {
            let (a, b) = (w[0], w[1]);
            if !state.compared(a, b) {
                return Some((a, b));
            }
        }
        None
    }

    /// Resolve `awaiting` against the active partition, then clear it.
    ///
    /// Invariant: `awaiting == Some((elem, pivot))` iff the top frame is
    /// `Active` with `order[cursor] == elem` (or cursor on pivot_pos skip),
    /// and we previously returned that pair. Votes for it must now exist.
    fn resume_awaiting(&mut self, state: &SimState) {
        let (elem, pivot) = match self.awaiting.take() {
            Some(p) => p,
            None => return,
        };
        let frame = self
            .stack
            .last_mut()
            .expect("awaiting set implies non-empty stack");
        let part = match frame {
            Frame::Active(p) => p,
            Frame::Pending { .. } => panic!("awaiting set implies Active frame"),
        };
        debug_assert_eq!(part.pivot, pivot);
        // Skip pivot slot if cursor landed on it.
        while part.cursor < part.hi && part.cursor == part.pivot_pos {
            part.cursor += 1;
        }
        debug_assert!(part.cursor < part.hi);
        debug_assert_eq!(self.order[part.cursor], elem);
        let current = if part.cursor < part.pivot_pos {
            Side::Left
        } else {
            Side::Right
        };
        let side = classify(state, elem, pivot, current)
            .expect("votes must exist after propose pause");
        match side {
            Side::Left => part.left.push(elem),
            Side::Right => part.right.push(elem),
        }
        part.cursor += 1;
    }

    /// Advance the state machine until we need a new proposal or the pass ends.
    /// Returns `Some(pair)` to propose, or `None` if the current pass finished
    /// (stack drained) without needing further votes.
    fn drive_pass(&mut self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        let cap = depth_cap(self.n);
        loop {
            let Some(frame) = self.stack.last_mut() else {
                return None; // pass complete
            };

            match frame {
                Frame::Pending { lo, hi, depth } => {
                    let lo = *lo;
                    let hi = *hi;
                    let depth = *depth;
                    self.stack.pop();
                    if hi - lo <= 1 || depth > cap {
                        // Trivial or depth-capped: leave segment as-is.
                        continue;
                    }
                    let pivot_pos = lo + rng.gen_range(0..(hi - lo));
                    let pivot = self.order[pivot_pos];
                    self.stack.push(Frame::Active(Partition {
                        lo,
                        hi,
                        depth,
                        pivot,
                        pivot_pos,
                        cursor: lo,
                        left: Vec::with_capacity(hi - lo),
                        right: Vec::with_capacity(hi - lo),
                    }));
                }
                Frame::Active(part) => {
                    // Skip pivot index.
                    while part.cursor < part.hi && part.cursor == part.pivot_pos {
                        part.cursor += 1;
                    }
                    if part.cursor >= part.hi {
                        // Finalize partition into order[lo..hi].
                        let lo = part.lo;
                        let hi = part.hi;
                        let depth = part.depth;
                        let pivot = part.pivot;
                        let left = std::mem::take(&mut part.left);
                        let right = std::mem::take(&mut part.right);
                        self.stack.pop();

                        let mid = lo + left.len();
                        for (k, &x) in left.iter().enumerate() {
                            self.order[lo + k] = x;
                        }
                        self.order[mid] = pivot;
                        for (k, &x) in right.iter().enumerate() {
                            self.order[mid + 1 + k] = x;
                        }

                        // Push right then left so left is processed first (DFS).
                        let right_lo = mid + 1;
                        let right_hi = hi;
                        let left_lo = lo;
                        let left_hi = mid;
                        if right_hi - right_lo > 1 {
                            self.stack.push(Frame::Pending {
                                lo: right_lo,
                                hi: right_hi,
                                depth: depth + 1,
                            });
                        }
                        if left_hi - left_lo > 1 {
                            self.stack.push(Frame::Pending {
                                lo: left_lo,
                                hi: left_hi,
                                depth: depth + 1,
                            });
                        }
                        continue;
                    }

                    let elem = self.order[part.cursor];
                    let pivot = part.pivot;
                    let current = if part.cursor < part.pivot_pos {
                        Side::Left
                    } else {
                        Side::Right
                    };
                    match classify(state, elem, pivot, current) {
                        Some(side) => {
                            match side {
                                Side::Left => part.left.push(elem),
                                Side::Right => part.right.push(elem),
                            }
                            part.cursor += 1;
                        }
                        None => {
                            // Pause: propose and wait for harness votes.
                            // Invariant: awaiting matches (elem, pivot); cursor
                            // still points at elem until resume_awaiting.
                            self.awaiting = Some((elem, pivot));
                            self.proposed_this_pass = true;
                            return Some((elem, pivot));
                        }
                    }
                }
            }
        }
    }
}

impl PairStrategy for Quicksort {
    fn name(&self) -> &'static str {
        "quicksort"
    }

    fn next_pair(&mut self, state: &SimState, rng: &mut SmallRng) -> Option<(usize, usize)> {
        if self.n < 2 {
            return None;
        }
        if self.fallback_only {
            return self.fallback_adjacent(state);
        }
        if !self.initialized {
            self.start_pass(rng, true);
        }

        // Resume: consume votes for the pair we proposed last call.
        self.resume_awaiting(state);

        loop {
            if let Some(pair) = self.drive_pass(state, rng) {
                // drive_pass only returns pairs with no votes yet.
                return Some(pair);
            }

            // Pass finished.
            self.passes_completed += 1;
            if !self.proposed_this_pass {
                // Restart found nothing new → ranking-adjacent fallback.
                self.fallback_only = true;
                return self.fallback_adjacent(state);
            }
            // Restart on produced order with fresh random pivots.
            self.start_pass(rng, false);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{SimState, VoteRec};
    use rand::SeedableRng;
    use std::collections::HashSet;

    fn feed_oracle(state: &mut SimState, i: usize, j: usize) {
        // Perfect oracle: lower index wins.
        let (winner, loser) = if i < j { (i, j) } else { (j, i) };
        state.push_vote(VoteRec {
            winner,
            loser,
            wr: 2.0,
            lr: 1.0,
        });
    }

    fn pair_key(i: usize, j: usize) -> (usize, usize) {
        if i < j {
            (i, j)
        } else {
            (j, i)
        }
    }

    #[test]
    fn oracle_first_sort_bound_n16() {
        let n = 16;
        let mut state = SimState::new(n, vec![0; n], 1);
        let mut strat = Quicksort::new(n);
        let mut rng = SmallRng::seed_from_u64(42);
        let mut seen: HashSet<(usize, usize)> = HashSet::new();
        // N * ceil(log2 N) * 2; ceil(log2 16) = 4
        let bound = n * 4 * 2;

        let mut proposals = 0usize;
        while strat.passes_completed < 1 {
            let Some((i, j)) = strat.next_pair(&state, &mut rng) else {
                panic!("strategy returned None before first sort completed");
            };
            let key = pair_key(i, j);
            assert!(
                !state.compared(i, j),
                "proposed already-compared pair {:?}",
                key
            );
            assert!(seen.insert(key), "proposed duplicate pair {:?}", key);
            feed_oracle(&mut state, i, j);
            proposals += 1;
            assert!(
                proposals <= bound,
                "first sort used {} proposals > bound {}",
                proposals,
                bound
            );
        }
        assert!(proposals <= bound);
        assert!(proposals > 0);
    }

    #[test]
    fn restart_proposes_only_new_pairs() {
        let n = 12;
        let mut state = SimState::new(n, vec![0; n], 1);
        let mut strat = Quicksort::new(n);
        let mut rng = SmallRng::seed_from_u64(7);

        // Finish first sort.
        while strat.passes_completed < 1 {
            let (i, j) = strat
                .next_pair(&state, &mut rng)
                .expect("pairs until first sort done");
            assert!(!state.compared(i, j));
            feed_oracle(&mut state, i, j);
        }
        let after_first: HashSet<_> = state.compared_pairs().collect();

        // Drive into / through a restart; every proposal must be new.
        let mut restart_proposals = 0usize;
        for _ in 0..500 {
            if strat.passes_completed >= 2 || strat.fallback_only {
                break;
            }
            match strat.next_pair(&state, &mut rng) {
                Some((i, j)) => {
                    let key = pair_key(i, j);
                    assert!(
                        !after_first.contains(&key),
                        "restart re-proposed first-pass pair {:?}",
                        key
                    );
                    assert!(!state.compared(i, j));
                    feed_oracle(&mut state, i, j);
                    restart_proposals += 1;
                }
                None => break,
            }
        }
        // Either we proposed new pairs on restart, or fell through to fallback
        // / None after a no-progress pass — both are valid.
        assert!(strat.passes_completed >= 1);
        let _ = restart_proposals;
    }

    #[test]
    fn edge_n1() {
        let state = SimState::new(1, vec![0], 1);
        let mut strat = Quicksort::new(1);
        let mut rng = SmallRng::seed_from_u64(1);
        assert!(strat.next_pair(&state, &mut rng).is_none());
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }

    #[test]
    fn edge_n2() {
        let n = 2;
        let mut state = SimState::new(n, vec![0; n], 1);
        let mut strat = Quicksort::new(n);
        let mut rng = SmallRng::seed_from_u64(2);
        let mut seen = HashSet::new();
        for _ in 0..20 {
            match strat.next_pair(&state, &mut rng) {
                Some((i, j)) => {
                    assert!(i < n && j < n && i != j);
                    assert!(!state.compared(i, j));
                    assert!(seen.insert(pair_key(i, j)));
                    feed_oracle(&mut state, i, j);
                }
                None => break,
            }
        }
        // Only one unordered pair exists; eventually exhausted.
        assert!(strat.next_pair(&state, &mut rng).is_none() || state.comparisons_made() == 1);
        // Drain until None.
        for _ in 0..10 {
            if let Some((i, j)) = strat.next_pair(&state, &mut rng) {
                assert!(!state.compared(i, j));
                feed_oracle(&mut state, i, j);
            } else {
                break;
            }
        }
        assert!(strat.next_pair(&state, &mut rng).is_none());
    }

    #[test]
    fn edge_n3() {
        let n = 3;
        let mut state = SimState::new(n, vec![0; n], 1);
        let mut strat = Quicksort::new(n);
        let mut rng = SmallRng::seed_from_u64(3);
        let mut seen = HashSet::new();
        for _ in 0..50 {
            match strat.next_pair(&state, &mut rng) {
                Some((i, j)) => {
                    assert!(!state.compared(i, j));
                    assert!(seen.insert(pair_key(i, j)), "duplicate {:?}", (i, j));
                    feed_oracle(&mut state, i, j);
                }
                None => break,
            }
        }
        assert!(!seen.is_empty());
        assert!(seen.len() <= 3); // at most C(3,2)
    }
}
