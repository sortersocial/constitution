//! Calibration constants fitted from real prod votes (see calibrate.py).

use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct RatioModel {
    pub gap_terciles: [f64; 2],
    pub tables: std::collections::HashMap<String, Vec<(f64, f64)>>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Calibration {
    pub epsilon: f64,
    pub council_tau: f64,
    pub theta_sd: f64,
    pub ratio_model: RatioModel,
}

impl Calibration {
    pub fn load(path: &str) -> Self {
        let text = std::fs::read_to_string(path)
            .unwrap_or_else(|e| panic!("cannot read calibration {path}: {e}"));
        serde_json::from_str(&text)
            .unwrap_or_else(|e| panic!("bad calibration json {path}: {e}"))
    }

    /// Sample a declared ratio (winner-strength : 1) for a vote.
    /// `gap` is |theta_i - theta_j|; `agree` is whether the vote direction
    /// matches the sign of the true gap.
    pub fn sample_ratio(&self, gap: f64, agree: bool, u: f64) -> f64 {
        let tercile = if gap <= self.ratio_model.gap_terciles[0] {
            0
        } else if gap <= self.ratio_model.gap_terciles[1] {
            1
        } else {
            2
        };
        let key = format!(
            "tercile{}_{}",
            tercile,
            if agree { "agree" } else { "disagree" }
        );
        let table = match self.ratio_model.tables.get(&key) {
            Some(t) if !t.is_empty() => t,
            _ => return 2.0,
        };
        let mut acc = 0.0;
        for (ratio, prob) in table {
            acc += prob;
            if u <= acc {
                return *ratio;
            }
        }
        table.last().map(|(r, _)| *r).unwrap_or(2.0)
    }
}

/// A fixed default matching the fitted prod values, so tests and agents
/// don't need the JSON file on disk.
pub fn default_calibration() -> Calibration {
    Calibration {
        epsilon: 0.0,
        council_tau: 2.0,
        theta_sd: 0.749,
        ratio_model: RatioModel {
            gap_terciles: [0.35, 0.85],
            tables: [
                ("tercile0_agree", vec![(1.5, 0.35), (2.0, 0.40), (3.0, 0.25)]),
                ("tercile0_disagree", vec![(1.5, 0.45), (2.0, 0.40), (3.0, 0.15)]),
                ("tercile1_agree", vec![(2.0, 0.40), (3.0, 0.35), (5.0, 0.25)]),
                ("tercile1_disagree", vec![(1.5, 0.40), (2.0, 0.40), (3.0, 0.20)]),
                ("tercile2_agree", vec![(3.0, 0.35), (5.0, 0.35), (10.0, 0.30)]),
                ("tercile2_disagree", vec![(1.5, 0.40), (2.0, 0.40), (3.0, 0.20)]),
            ]
            .into_iter()
            .map(|(k, v)| (k.to_string(), v))
            .collect(),
        },
    }
}
