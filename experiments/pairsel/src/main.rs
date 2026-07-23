//! CLI: run Monte Carlo sweeps and emit CSV.
//!
//! Usage:
//!   pairsel run --strategies zip,random_pairs --k 1,3,5 --n 161 \
//!     --contributors 10 --kappa 0.5 --world heavytail --reps 200 \
//!     --budget-mult 32 --calib calibration.json --out results.csv
//!   pairsel list

use std::collections::HashMap;
use std::io::Write;

use rayon::prelude::*;

use pairsel::calib::{default_calibration, Calibration};
use pairsel::runner::{run_once, RunParams};
use pairsel::strategies;
use pairsel::world::WorldKind;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(|s| s.as_str()) {
        Some("list") => {
            for s in strategies::ALL {
                println!("{s}");
            }
        }
        Some("run") => run(parse_flags(&args[2..])),
        _ => {
            eprintln!("usage: pairsel run [flags] | pairsel list");
            std::process::exit(2);
        }
    }
}

fn parse_flags(rest: &[String]) -> HashMap<String, String> {
    let mut flags = HashMap::new();
    let mut i = 0;
    while i < rest.len() {
        let key = rest[i]
            .strip_prefix("--")
            .unwrap_or_else(|| panic!("expected --flag, got {}", rest[i]));
        let value = rest.get(i + 1).unwrap_or_else(|| panic!("missing value for --{key}"));
        flags.insert(key.to_string(), value.clone());
        i += 2;
    }
    flags
}

fn run(flags: HashMap<String, String>) {
    let get = |k: &str, d: &str| flags.get(k).cloned().unwrap_or_else(|| d.to_string());
    let strategy_list: Vec<String> = match get("strategies", "all").as_str() {
        "all" => strategies::ALL.iter().map(|s| s.to_string()).collect(),
        list => list.split(',').map(|s| s.trim().to_string()).collect(),
    };
    let ks: Vec<u32> = get("k", "1")
        .split(',')
        .map(|s| s.trim().parse().expect("bad --k"))
        .collect();
    let n: usize = get("n", "161").parse().expect("bad --n");
    let contributors: usize = get("contributors", "10").parse().expect("bad --contributors");
    let kappa: f64 = get("kappa", "0.5").parse().expect("bad --kappa");
    let world = WorldKind::parse(&get("world", "heavytail"));
    let reps: u64 = get("reps", "100").parse().expect("bad --reps");
    let budget_mult: usize = get("budget-mult", "32").parse().expect("bad --budget-mult");
    let seed0: u64 = get("seed0", "0").parse().expect("bad --seed0");
    let out_path = get("out", "results.csv");
    let calib: Calibration = match flags.get("calib") {
        Some(path) => Calibration::load(path),
        None => default_calibration(),
    };

    let budget = budget_mult * n;
    let mults = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48];
    let checkpoints: Vec<usize> = mults
        .iter()
        .map(|m| m * n)
        .filter(|&v| v <= budget)
        .collect();

    let mut jobs: Vec<(String, u32, u64)> = Vec::new();
    for s in &strategy_list {
        for &k in &ks {
            for r in 0..reps {
                jobs.push((s.clone(), k, seed0 + r));
            }
        }
    }
    eprintln!(
        "{} jobs: {} strategies x {} k x {} reps, n={n} C={contributors} kappa={kappa} budget={budget} votes",
        jobs.len(),
        strategy_list.len(),
        ks.len(),
        reps
    );

    let started = std::time::Instant::now();
    let rows: Vec<String> = jobs
        .par_iter()
        .flat_map(|(strategy, k, rep)| {
            let params = RunParams {
                strategy: strategy.clone(),
                n,
                contributors,
                kappa,
                world,
                votes_per_edge: *k,
                budget_votes: budget,
                checkpoints: checkpoints.clone(),
                replicate: *rep,
            };
            run_once(&params, &calib)
                .into_iter()
                .map(|row| {
                    format!(
                        "{strategy},{k},{n},{contributors},{kappa},{rep},{},{},{:.6},{:.6},{:.4},{}",
                        row.votes_spent,
                        row.comparisons,
                        row.payout_tv,
                        row.kendall,
                        row.top10_recall,
                        row.fallbacks
                    )
                })
                .collect::<Vec<_>>()
        })
        .collect();

    let mut f = std::fs::File::create(&out_path).expect("cannot create out file");
    writeln!(
        f,
        "strategy,k,n,contributors,kappa,rep,votes,comparisons,payout_tv,kendall,top10,fallbacks"
    )
    .unwrap();
    for r in &rows {
        writeln!(f, "{r}").unwrap();
    }
    eprintln!(
        "wrote {} rows to {} in {:.1}s",
        rows.len(),
        out_path,
        started.elapsed().as_secs_f64()
    );
}
