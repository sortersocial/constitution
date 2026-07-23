//! Noisy bottom-up merge sort pair selection.
//!
//! Maintains a permutation of commit indices and runs iterative bottom-up
//! merge sort. Each merge comparison that lacks votes is proposed to the
//! harness; once votes exist, direction is ratio-weighted majority.
//! After a full pass, restarts on the produced order to spend remaining
//! budget on uncertain regions; if a pass proposes nothing new, falls
//! back to uncovered adjacent pairs in the Rank Centrality ranking.

use rand::rngs::SmallRng;

use crate::state::SimState;
use crate::strategies::PairStrategy;

/// One in-progress merge of `order[lo..mid)` with `order[mid..hi)`.
///
/// Invariants:
/// - `left` / `right` are snapshots of those runs at merge start.
/// - `out` holds the merged prefix; `li` / `ri` are the next unread indices.
/// - When paused for a vote, `li < left.len()` and `ri < right.len()` and
///   the needed pair is `(left[li], right[ri])`.
struct ActiveMerge {
    lo: usize,
    hi: usize,
    left: Vec<usize>,
    right: Vec<usize>,
    li: usize,
    ri: usize,
    out: Vec<usize>,
}

/// Stack frame describing a pending merge range into `order`.
/// `(lo, mid, hi)` with `lo < mid < hi`.
type MergeTask = (usize, usize, usize);

pub struct MergeSort {
    n: usize,
    /// Current permutation being sorted (initially `0..n`).
    order: Vec<usize>,
    /// Bottom-up run width for the current level.
    width: usize,
    /// Pending merges at this width (stack: pop = next task).
    /// Invariant: tasks are pushed right-to-left so pops run left-to-right.
    pending: Vec<MergeTask>,
    /// Merge currently paused or in progress.
    active: Option<ActiveMerge>,
    /// Whether this sort pass has proposed at least one fresh pair.
    proposed_this_pass: bool,
    /// Sort passes exhausted; only ranking-adjacent fallback remains.
    fallback: bool,
}

impl MergeSort {
    pub fn new(n: usize) -> Self {
        let mut s = Self {
            n,
            order: (0..n).collect(),
            width: 1,
            pending: Vec::new(),
            active: None,
            proposed_this_pass: false,
            fallback: false,
        };
        if n >= 2 {
            s.schedule_level();
        } else {
            s.fallback = true;
        }
        s
    }

    /// Fill `pending` with merges for the current `width`.
    fn schedule_level(&mut self) {
        self.pending.clear();
        self.active = None;
        let w = self.width;
        let n = self.n;
        let mut starts = Vec::new();
        let mut start = 0;
        while start < n {
            starts.push(start);
            start += 2 * w;
        }
        // Push right-to-left so stack pops left-to-right.
        for &start in starts.iter().rev() {
            let mid = (start + w).min(n);
            let hi = (start + 2 * w).min(n);
            if start < mid && mid < hi {
                self.pending.push((start, mid, hi));
            }
        }
    }

    fn start_merge(&mut self, lo: usize, mid: usize, hi: usize) {
        let left = self.order[lo..mid].to_vec();
        let right = self.order[mid..hi].to_vec();
        self.active = Some(ActiveMerge {
            lo,
            hi,
            left,
            right,
            li: 0,
            ri: 0,
            out: Vec::with_capacity(hi - lo),
        });
    }

    fn finish_active(&mut self) {
        let m = self.active.take().expect("active merge");
        debug_assert_eq!(m.out.len(), m.hi - m.lo);
        self.order[m.lo..m.hi].copy_from_slice(&m.out);
    }

    /// Advance until we need a fresh pair, finish a pass, or enter fallback.
    /// Returns `Some((i, j))` to propose, or `None` when the caller should
    /// use ranking-adjacent fallback (pass proposed nothing new).
    fn drive(&mut self, state: &SimState) -> Option<(usize, usize)> {
        loop {
            if self.active.is_none() {
                if let Some((lo, mid, hi)) = self.pending.pop() {
                    self.start_merge(lo, mid, hi);
                } else {
                    // Level complete: widen or finish the pass.
                    self.width = self.width.saturating_mul(2);
                    if self.width >= self.n {
                        if self.proposed_this_pass {
                            // Restart on the produced order.
                            self.width = 1;
                            self.proposed_this_pass = false;
                            self.schedule_level();
                            continue;
                        }
                        return None;
                    }
                    self.schedule_level();
                    continue;
                }
            }

            // Progress the active merge; may pause for a vote.
            if let Some(pair) = self.step_active(state) {
                self.proposed_this_pass = true;
                return Some(pair);
            }
            // Active merge finished (or drained); loop to next task.
        }
    }

    /// Step the active merge. `Some` = propose this pair; `None` = merge
    /// finished (active cleared) or advanced without needing a new vote.
    fn step_active(&mut self, state: &SimState) -> Option<(usize, usize)> {
        loop {
            let (a, b) = {
                let m = self.active.as_ref().unwrap();
                if m.li < m.left.len() && m.ri < m.right.len() {
                    (m.left[m.li], m.right[m.ri])
                } else {
                    // Drain remainder into out and write back.
                    let m = self.active.as_mut().unwrap();
                    while m.li < m.left.len() {
                        m.out.push(m.left[m.li]);
                        m.li += 1;
                    }
                    while m.ri < m.right.len() {
                        m.out.push(m.right[m.ri]);
                        m.ri += 1;
                    }
                    self.finish_active();
                    return None;
                }
            };

            match decide_before(state, a, b) {
                None => return Some((a, b)),
                Some(a_first) => {
                    let m = self.active.as_mut().unwrap();
                    if a_first {
                        m.out.push(a);
                        m.li += 1;
                    } else {
                        m.out.push(b);
                        m.ri += 1;
                    }
                }
            }
        }
    }
}

/// Whether `a` should come before `b` in the sorted order (higher rank).
/// `None` if the pair has no votes yet.
///
/// Decision: direction with larger sum of `wr` wins; ties keep current
/// order (prefer `a`, the left-run element — stable merge).
fn decide_before(state: &SimState, a: usize, b: usize) -> Option<bool> {
    if !state.compared(a, b) {
        return None;
    }
    let mut a_w = 0.0;
    let mut b_w = 0.0;
    for v in state.votes() {
        if v.winner == a && v.loser == b {
            a_w += v.wr;
        } else if v.winner == b && v.loser == a {
            b_w += v.wr;
        }
    }
    if a_w > b_w {
        Some(true)
    } else if b_w > a_w {
        Some(false)
    } else {
        Some(true) // tie: keep current order
    }
}

fn fallback_adjacent(state: &SimState) -> Option<(usize, usize)> {
    let n = state.n();
    if n < 2 {
        return None;
    }
    let ranking = state.ranking();
    for pos in 0..(n - 1) {
        let (a, b) = (ranking[pos], ranking[pos + 1]);
        if !state.compared(a, b) {
            return Some((a, b));
        }
    }
    None
}

impl PairStrategy for MergeSort {
    fn name(&self) -> &'static str {
        "merge_sort"
    }

    fn next_pair(&mut self, state: &SimState, _rng: &mut SmallRng) -> Option<(usize, usize)> {
        if self.n < 2 {
            return None;
        }
        if self.fallback {
            return fallback_adjacent(state);
        }
        match self.drive(state) {
            Some(pair) => Some(pair),
            None => {
                self.fallback = true;
                fallback_adjacent(state)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{SimState, VoteRec};
    use rand::SeedableRng;
    use std::collections::HashSet;

    fn key(i: usize, j: usize) -> (usize, usize) {
        if i < j {
            (i, j)
        } else {
            (j, i)
        }
    }

    /// Lower index always wins (perfect ranking 0 ≻ 1 ≻ … ≻ n-1).
    fn perfect_oracle(i: usize, j: usize) -> (usize, usize) {
        if i < j {
            (i, j)
        } else {
            (j, i)
        }
    }

    fn drive_with_oracle<F>(n: usize, mut oracle: F) -> (MergeSort, SimState, Vec<(usize, usize)>)
    where
        F: FnMut(usize, usize) -> (usize, usize),
    {
        let mut strat = MergeSort::new(n);
        let authors: Vec<usize> = (0..n).map(|i| i % 3).collect();
        let mut state = SimState::new(n, authors, 3);
        let mut rng = SmallRng::seed_from_u64(1);
        let mut proposed = Vec::new();
        let mut seen = HashSet::new();

        while let Some((i, j)) = strat.next_pair(&state, &mut rng) {
            assert!(i != j);
            assert!(
                !state.compared(i, j),
                "proposed already-compared pair ({i},{j})"
            );
            assert!(
                seen.insert(key(i, j)),
                "proposed pair ({i},{j}) twice"
            );
            proposed.push((i, j));
            let (winner, loser) = oracle(i, j);
            state.push_vote(VoteRec {
                winner,
                loser,
                wr: 2.0,
                lr: 1.0,
            });
            // Safety: avoid infinite loops if strategy misbehaves.
            assert!(proposed.len() <= n * n, "too many proposals");
        }
        (strat, state, proposed)
    }

    #[test]
    fn proposes_approx_n_log_n_and_never_reuses() {
        let n = 32;
        let (_strat, _state, proposed) = drive_with_oracle(n, perfect_oracle);
        let count = proposed.len();
        // Merge-sort Θ(N log N) plus at most N-1 ranking-adjacent fallbacks.
        let nlogn = (n as f64) * (n as f64).log2();
        assert!(
            (count as f64) >= 0.5 * nlogn,
            "too few pairs: {count} vs ~{nlogn}"
        );
        assert!(
            (count as f64) <= 2.5 * nlogn + (n as f64),
            "too many pairs: {count} vs ~{nlogn}"
        );
        // Distinctness already checked inside drive_with_oracle.
        assert_eq!(count, proposed.iter().map(|&(i, j)| key(i, j)).collect::<HashSet<_>>().len());
    }

    #[test]
    fn n8_perfect_oracle_sorted_and_no_dupes() {
        let n = 8;
        let (strat, _state, proposed) = drive_with_oracle(n, perfect_oracle);
        assert_eq!(strat.order, (0..n).collect::<Vec<_>>());
        let mut seen = HashSet::new();
        for &(i, j) in &proposed {
            assert!(seen.insert(key(i, j)));
        }
        // With a perfect lower-index-wins oracle, every merge decision
        // prefers the smaller index — final order must be identity.
        assert!(proposed.len() >= n - 1);
    }

    #[test]
    fn edge_n1_returns_none() {
        let mut strat = MergeSort::new(1);
        let state = SimState::new(1, vec![0], 1);
        let mut rng = SmallRng::seed_from_u64(0);
        assert_eq!(strat.next_pair(&state, &mut rng), None);
    }

    #[test]
    fn edge_n2_proposes_once_then_exhausts() {
        let n = 2;
        let (strat, state, proposed) = drive_with_oracle(n, perfect_oracle);
        assert_eq!(proposed.len(), 1);
        assert_eq!(key(proposed[0].0, proposed[0].1), (0, 1));
        assert_eq!(strat.order, vec![0, 1]);
        assert!(state.compared(0, 1));
        let mut strat2 = MergeSort::new(2);
        let mut rng = SmallRng::seed_from_u64(0);
        // Fresh strategy on already-complete state: sort resolves from
        // votes instantly and fallback finds nothing uncovered.
        let authors = vec![0, 1];
        let mut state2 = SimState::new(2, authors, 2);
        state2.push_vote(VoteRec {
            winner: 0,
            loser: 1,
            wr: 2.0,
            lr: 1.0,
        });
        assert_eq!(strat2.next_pair(&state2, &mut rng), None);
    }
}
