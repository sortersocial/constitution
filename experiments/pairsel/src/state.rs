//! Read-only view of the simulation for strategies, with a lazily cached
//! production Rank Centrality score vector.

use std::cell::RefCell;
use std::collections::HashMap;

use crate::rank::{rank_scores_from, ranking_of};

#[derive(Debug, Clone, Copy)]
pub struct VoteRec {
    pub winner: usize,
    pub loser: usize,
    /// winner-strength weight (ratio numerator), e.g. 2.0 for "2:1"
    pub wr: f64,
    /// loser weight (ratio denominator), 1.0 in production data
    pub lr: f64,
}

pub struct SimState {
    n: usize,
    authors: Vec<usize>,
    n_contributors: usize,
    votes: Vec<VoteRec>,
    pair_votes: HashMap<(usize, usize), u32>,
    scores_cache: RefCell<Option<Vec<f64>>>,
    warm_start: RefCell<Option<Vec<f64>>>,
}

impl SimState {
    pub fn new(n: usize, authors: Vec<usize>, n_contributors: usize) -> Self {
        Self {
            n,
            authors,
            n_contributors,
            votes: Vec::new(),
            pair_votes: HashMap::new(),
            scores_cache: RefCell::new(None),
            warm_start: RefCell::new(None),
        }
    }

    pub fn n(&self) -> usize {
        self.n
    }

    /// Commit index -> contributor index.
    pub fn authors(&self) -> &[usize] {
        &self.authors
    }

    pub fn n_contributors(&self) -> usize {
        self.n_contributors
    }

    /// All votes so far, chronological.
    pub fn votes(&self) -> &[VoteRec] {
        &self.votes
    }

    /// Number of distinct pairs compared so far.
    pub fn comparisons_made(&self) -> usize {
        self.pair_votes.len()
    }

    /// Has this unordered pair been compared already?
    pub fn compared(&self, i: usize, j: usize) -> bool {
        self.pair_votes.contains_key(&key(i, j))
    }

    /// Votes cast on this unordered pair.
    pub fn pair_vote_count(&self, i: usize, j: usize) -> u32 {
        *self.pair_votes.get(&key(i, j)).unwrap_or(&0)
    }

    /// Distinct compared pairs (unordered, i < j).
    pub fn compared_pairs(&self) -> impl Iterator<Item = (usize, usize)> + '_ {
        self.pair_votes.keys().copied()
    }

    /// Current production Rank Centrality scores (lazily cached,
    /// warm-started from the previous solve for speed; the fixed point
    /// is the same on connected graphs).
    pub fn scores(&self) -> Vec<f64> {
        let mut cache = self.scores_cache.borrow_mut();
        if cache.is_none() {
            let warm = self.warm_start.borrow();
            let scores = rank_scores_from(
                self.n,
                self.votes.iter().copied(),
                warm.as_deref(),
            );
            drop(warm);
            self.warm_start.replace(Some(scores.clone()));
            *cache = Some(scores);
        }
        cache.as_ref().unwrap().clone()
    }

    /// Current ranking: indices sorted by score descending.
    pub fn ranking(&self) -> Vec<usize> {
        ranking_of(&self.scores())
    }

    /// Harness-only: record a vote and invalidate the score cache.
    pub(crate) fn push_vote(&mut self, v: VoteRec) {
        *self.pair_votes.entry(key(v.winner, v.loser)).or_insert(0) += 1;
        self.votes.push(v);
        self.scores_cache.replace(None);
    }
}

fn key(i: usize, j: usize) -> (usize, usize) {
    if i < j {
        (i, j)
    } else {
        (j, i)
    }
}
